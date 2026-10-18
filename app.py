import csv
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

import cv2
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

from omr import ai as ai_mod
from omr import pages, scan, scanner, table

if getattr(sys, "frozen", False):  # packaged: bundled files are read-only, user data goes to AppData
    ROOT = Path(sys._MEIPASS)
    DATA = Path(os.environ.get("APPDATA", Path.home())) / "OMR Utility"
else:
    ROOT = Path(__file__).parent
    DATA = ROOT / "data"
# Everything about one job (database, page images, scanned images) lives in its own project folder, so a
# project can be switched, saved to one file and restored. Global things (API keys, options) stay in DATA.
PROJ = {"id": None, "dir": None}
IMG = SCAN_DIR = None  # set by _open_project()

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

COLS = ["first", "middle", "last", "number", "phones", "meal", "other", "raw"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, name TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP, only_marked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pages(id INTEGER PRIMARY KEY, batch_id INTEGER, source TEXT, page_no INTEGER, image TEXT);
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, file TEXT, scanned_at TEXT DEFAULT (datetime('now','localtime')),
  status TEXT DEFAULT 'scanned', batch_id INTEGER, page_id INTEGER, rows_found INTEGER, note TEXT, dpi INTEGER, mode TEXT,
  device TEXT, read_at TEXT);
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS rows(id INTEGER PRIMARY KEY, batch_id INTEGER, page_id INTEGER, y REAL, h REAL,
  first TEXT, middle TEXT DEFAULT '', last TEXT, number TEXT, phones TEXT DEFAULT '', meal TEXT, other TEXT, raw TEXT, conf REAL,
  status TEXT DEFAULT 'pending', group_id INTEGER);
"""


def db():
    con = sqlite3.connect(PROJ["dir"] / "omr2.db")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(rows)")}
    for col in ("middle", "phones"):  # databases created before these columns existed
        if col not in have:
            con.execute(f"ALTER TABLE rows ADD COLUMN {col} TEXT DEFAULT ''")
    if "only_marked" not in {r[1] for r in con.execute("PRAGMA table_info(batches)")}:
        con.execute("ALTER TABLE batches ADD COLUMN only_marked INTEGER DEFAULT 0")
    return con


def rows(sql, *a):
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, a)]
    finally:
        con.close()


def one(sql, *a):
    r = rows(sql, *a)
    return r[0] if r else None


def run(sql, *a):
    con = db()
    try:
        with con:
            cur = con.execute(sql, a)
        return cur.lastrowid
    finally:
        con.close()


# ---------- settings (AI engine) ----------
SETTINGS = DATA / "settings.json"


def load_settings():
    try:
        d = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    return {"engine": d.get("engine", "local"), "model": d.get("model", ai_mod.DEFAULT_MODEL), "api_key": d.get("api_key", ""),
            "only_marked": bool(d.get("only_marked", False)), "provider": d.get("provider", "claude"),
            "gemini_key": d.get("gemini_key", ""), "gemini_model": d.get("gemini_model", ai_mod.GEMINI_DEFAULT),
            "autoread": bool(d.get("autoread", False))}


def ai_config(s):
    """The credentials and model for whichever AI provider is selected."""
    if s["provider"] == "gemini":
        return {"provider": "gemini", "key": s["gemini_key"] or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "",
                "model": s["gemini_model"]}
    return {"provider": "claude", "key": s["api_key"], "model": s["model"]}


def save_settings(d):
    SETTINGS.write_text(json.dumps(d), encoding="utf-8")


@app.get("/api/settings")
def settings_get():
    s = load_settings()  # the key itself is never sent back to the page
    return jsonify(engine=s["engine"], model=s["model"], key_set=bool(s["api_key"]), models=ai_mod.MODELS, only_marked=s["only_marked"],
                   env_key=bool(os.environ.get("ANTHROPIC_API_KEY")), provider=s["provider"],
                   gemini_key_set=bool(s["gemini_key"]), gemini_model=s["gemini_model"], gemini_models=ai_mod.GEMINI_MODELS,
                   gemini_env_key=bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")), autoread=s["autoread"])


@app.put("/api/settings")
def settings_put():
    b = request.get_json()
    s = load_settings()
    if b.get("engine") in ("local", "ai"):
        s["engine"] = b["engine"]
    if b.get("model"):
        s["model"] = b["model"]
    if "only_marked" in b:
        s["only_marked"] = bool(b["only_marked"])
    if "autoread" in b:
        s["autoread"] = bool(b["autoread"])
    if b.get("provider") in ("claude", "gemini"):
        s["provider"] = b["provider"]
    if b.get("gemini_model"):
        s["gemini_model"] = b["gemini_model"].strip()
    if "gemini_key" in b:
        s["gemini_key"] = b["gemini_key"].strip()
    if "api_key" in b:  # "" clears it, omitted leaves it alone
        s["api_key"] = b["api_key"].strip()
    save_settings(s)
    if s["autoread"] and one("SELECT 1 x FROM scans WHERE status='scanned'"):
        _autoread_kick()  # switching it on also reads the pages already waiting
    return jsonify(ok=True)


@app.post("/api/settings/test")
def settings_test():
    s = load_settings()
    b = request.get_json(silent=True) or {}
    cfg = ai_config(s)
    try:
        ai_mod.check(cfg["key"], cfg["model"], cfg["provider"])
        return jsonify(ok=True)
    except ai_mod.AIError as e:
        return jsonify(error=str(e)), 400


def _phone_keys(phones):
    """Last 10 digits of each cleaned number, so +91 98765 43210 and 9876543210 count as the same."""
    return [re.sub(r"\D", "", p)[-10:] for p in (phones or "").split(";") if len(re.sub(r"\D", "", p)) >= 7]


def marks(n):
    return ",".join("?" * n)


def _startup():
    run("UPDATE scans SET status='scanned', note='re-queued after the app was closed while reading' WHERE status='reading'")



@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/img/<path:name>")
def img(name):
    return send_from_directory(IMG, name)


# ---------- batches / scanning ----------
@app.get("/api/batches")
def batches_list():
    return jsonify(rows("""SELECT b.*,
        (SELECT COUNT(*) FROM pages p WHERE p.batch_id=b.id) pages,
        (SELECT COUNT(*) FROM rows r WHERE r.batch_id=b.id) total,
        (SELECT COUNT(*) FROM rows r WHERE r.batch_id=b.id AND status='confirmed') confirmed
        FROM batches b ORDER BY b.id DESC"""))


JOBS = {}


def _save_page(bid, fname, i, shown, found, sid, only_marked):
    """Store one read page and its rows; keep the scanned-pages log in step. Returns the number of rows kept."""
    if only_marked:  # dinner/lunch sheets: keep just the rows that carry a D, L or check mark
        found = [r for r in found if r["meal"] and r["status"] != "header"]
    name = f"pg_{uuid.uuid4().hex[:10]}.jpg"
    pages.save(shown, IMG / name)
    pid = run("INSERT INTO pages(batch_id,source,page_no,image) VALUES(?,?,?,?)", bid, fname, i, name)
    con = db()  # one transaction for all the rows of a page (fast, and a page is never half-saved)
    try:
        with con:
            for r in found:
                con.execute("""INSERT INTO rows(batch_id,page_id,y,h,first,middle,last,number,phones,meal,other,raw,conf,status)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (bid, pid, r["y"], r["h"], r["first"], r["middle"], r["last"], r["number"], r["phones"], r["meal"],
                             r["other"], r["raw"], r["conf"], r["status"]))
    finally:
        con.close()
    if sid:
        run("UPDATE scans SET status='scraped', batch_id=?, page_id=?, rows_found=?, note=?, read_at=datetime('now','localtime') WHERE id=?",
            bid, pid, len(found), "no D, L or check mark found" if only_marked and not found else "", sid)
    return len(found)


def _scan_job(jid, bid, files, ai, only_marked=False):
    """files: [(filename, bytes)] or [(filename, bytes, scan_id)]. scan_id ties a page to the scanned-pages log."""
    job = JOBS[jid]
    sids = [f[2] for f in files if len(f) > 2]
    try:
        todo = []  # (filename, page number, image, scan_id)
        for f in files:
            fname, data, sid = f[0], f[1], (f[2] if len(f) > 2 else None)
            try:
                for i, page in enumerate(pages.load_pages(data, fname), 1):
                    todo.append((fname, i, page, sid))
            except Exception as e:
                job["warnings"].append(f"{fname}: could not be opened ({e})")
                if sid:
                    run("UPDATE scans SET status='failed', note=? WHERE id=?", f"could not be opened: {e}", sid)
        job["pages"] = len(todo)
        if not todo:
            raise RuntimeError("No readable pages in the selected files.")
        done = 0
        for fname, i, page, sid in todo:
            job["page"] = done + 1
            try:
                found, shown = scan.scan_page(page, ai)  # shown = the (possibly straightened) image the row positions refer to
            except ai_mod.AIError as e:
                if done == 0:  # nothing saved yet: most likely a key/connection problem, so stop and say so
                    raise
                job["warnings"].append(f"{fname} p{i}: AI failed ({e}); read with local OCR instead")
                found, shown = scan.scan_page(page, None)
            kept = _save_page(bid, fname, i, shown, found, sid, only_marked)
            if only_marked and not kept:
                job["warnings"].append(f"{fname} p{i}: no D, L or check mark found")
            done += 1
            job["done"] = done
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = ("AI scan failed: " if isinstance(e, ai_mod.AIError) else "") + str(e)
        if not job.get("done"):
            run("DELETE FROM batches WHERE id=?", bid)
            for sid in sids:  # nothing was read: put the pages back in the queue so the user can retry
                run("UPDATE scans SET status='scanned', note=? WHERE id=? AND status='reading'", f"last attempt failed: {job['error']}", sid)
        else:
            for sid in sids:
                run("UPDATE scans SET status='failed', note=? WHERE id=? AND status='reading'", job["error"], sid)


# ---------- auto-reader: reads each page as soon as it is scanned, into one running batch ----------
AUTO = {"batch_id": None, "running": False, "error": "", "lock": threading.Lock()}


def _autoread_kick():
    with AUTO["lock"]:
        if AUTO["running"]:
            return
        AUTO["running"] = True
        AUTO["error"] = ""
    threading.Thread(target=_autoread_loop, daemon=True).start()


def _autoread_loop():
    try:
        while True:
            row = one("SELECT * FROM scans WHERE status='scanned' ORDER BY id LIMIT 1")
            if not row:
                break
            st = load_settings()
            ai = ai_config(st) if st["engine"] == "ai" else None
            if AUTO["batch_id"] is None or not one("SELECT 1 x FROM batches WHERE id=?", AUTO["batch_id"]):
                AUTO["batch_id"] = run("INSERT INTO batches(name, only_marked) VALUES(?,?)",
                                       "Scanner " + time.strftime("%Y-%m-%d %H:%M"), int(st["only_marked"]))
            run("UPDATE scans SET status='reading', note='' WHERE id=?", row["id"])
            try:
                f = SCAN_DIR / row["file"]
                page = pages.load_pages(f.read_bytes(), row["file"])[0]
                try:
                    found, shown = scan.scan_page(page, ai)
                except ai_mod.AIError as e:
                    note = f"AI failed ({e}); read with local OCR"
                    found, shown = scan.scan_page(page, None)
                    run("UPDATE scans SET note=? WHERE id=?", note, row["id"])
                kept = _save_page(AUTO["batch_id"], f"Scan #{row['id']}", 1, shown, found, row["id"], st["only_marked"])
                if st["only_marked"] and not kept:
                    run("UPDATE scans SET note='no D, L or check mark found' WHERE id=?", row["id"])
            except Exception as e:
                run("UPDATE scans SET status='failed', note=? WHERE id=?", str(e)[:300], row["id"])
    except Exception as e:
        AUTO["error"] = str(e)
    finally:
        AUTO["running"] = False
    if load_settings()["autoread"] and one("SELECT 1 x FROM scans WHERE status='scanned'") and not AUTO["error"]:
        _autoread_kick()  # a page arrived while we were finishing


@app.get("/api/autoread")
def autoread_status():
    return jsonify(enabled=load_settings()["autoread"], running=AUTO["running"], batch_id=AUTO["batch_id"], error=AUTO["error"],
                   waiting=one("SELECT COUNT(*) n FROM scans WHERE status='scanned'")["n"],
                   reading=one("SELECT COUNT(*) n FROM scans WHERE status='reading'")["n"])


@app.post("/api/autoread/new-batch")
def autoread_new_batch():
    AUTO["batch_id"] = None
    return jsonify(ok=True)


def _start_batch(files, name, engine):
    st = load_settings()
    use_ai = (engine or st["engine"]) == "ai"
    ai = ai_config(st) if use_ai else None
    only_marked = st["only_marked"]
    bid = run("INSERT INTO batches(name, only_marked) VALUES(?,?)", name or files[0][0], int(only_marked))
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "page": 0, "pages": 0, "done": 0, "batch_id": bid, "warnings": [], "error": None,
                 "engine": "ai" if ai else "local", "provider": ai["provider"] if ai else ""}
    threading.Thread(target=_scan_job, args=(jid, bid, files, ai, only_marked), daemon=True).start()
    return jsonify(job=jid, id=bid)


@app.post("/api/batches")
def batch_create():
    files = [(f.filename, f.read()) for f in request.files.getlist("files")]
    if not files:
        return jsonify(error="No files"), 400
    return _start_batch(files, request.form.get("name"), request.form.get("engine"))


# ---------- scanner (Windows WIA, e.g. Canon G2000 flatbed) ----------


def _scan_row(r, n=None):
    return {"id": r["id"], "n": n, "file": r["file"], "url": "/scan/" + r["file"], "thumb": "/scan_thumb/" + r["file"],
            "status": r["status"], "scanned_at": r["scanned_at"], "read_at": r["read_at"], "batch_id": r["batch_id"],
            "rows_found": r["rows_found"], "note": r["note"] or "", "dpi": r["dpi"], "mode": r["mode"]}


def _queue():
    """Pages scanned but not yet read, oldest first."""
    q = rows("SELECT * FROM scans WHERE status='scanned' ORDER BY id")
    return [_scan_row(r, i + 1) for i, r in enumerate(q)]


@app.get("/scan/<path:name>")
def scan_file(name):
    return send_from_directory(SCAN_DIR, name)


@app.get("/scan_thumb/<path:name>")
def scan_thumb(name):
    """Small cached preview so the page log stays light even with hundreds of pages."""
    src = SCAN_DIR / name
    if not src.exists():
        abort(404)
    th = SCAN_DIR / "thumbs" / name
    if not th.exists():
        th.parent.mkdir(parents=True, exist_ok=True)
        im = cv2.imread(str(src))
        if im is None:
            abort(404)
        w = 260
        cv2.imwrite(str(th), cv2.resize(im, (w, max(1, int(im.shape[0] * w / im.shape[1]))), interpolation=cv2.INTER_AREA),
                    [cv2.IMWRITE_JPEG_QUALITY, 80])
    return send_from_directory(th.parent, name)


@app.get("/api/scanner/devices")
def scanner_devices():
    try:
        return jsonify(devices=scanner.list_devices())
    except scanner.ScannerError as e:
        return jsonify(error=str(e)), 400


@app.get("/api/scanner/pages")
def scanner_pages():
    return jsonify(pages=_queue())


@app.post("/api/scanner/scan")
def scanner_scan():
    b = request.get_json(silent=True) or {}
    name = f"scan_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    dpi, mode = int(b.get("dpi", 300)), b.get("mode", "color")
    try:
        path = scanner.scan_page(b.get("device") or None, SCAN_DIR, name, dpi, mode, b.get("page", "a4"))
    except scanner.ScannerError as e:
        return jsonify(error=str(e)), 400
    run("INSERT INTO scans(file,dpi,mode,device) VALUES(?,?,?,?)", path.name, dpi, mode, b.get("device") or "")
    if load_settings()["autoread"]:
        _autoread_kick()
    return jsonify(pages=_queue())


@app.post("/api/scanner/process")
def scanner_process():
    q = rows("SELECT * FROM scans WHERE status='scanned' ORDER BY id")
    if not q:
        return jsonify(error="Scan at least one page first"), 400
    b = request.get_json(silent=True) or {}
    stamp = time.strftime("%Y-%m-%d_%H%M")
    files = []
    for i, r in enumerate(q, 1):
        f = SCAN_DIR / r["file"]
        if not f.exists():
            run("UPDATE scans SET status='failed', note='image file is missing' WHERE id=?", r["id"])
            continue
        files.append((f"Scanner {stamp} p{i}.jpg", f.read_bytes(), r["id"]))
        run("UPDATE scans SET status='reading', note='' WHERE id=?", r["id"])
    if not files:
        return jsonify(error="The scanned image files are missing"), 400
    return _start_batch(files, b.get("name") or f"Scanner {stamp}", b.get("engine"))


# ---------- scanned-pages log ----------
@app.get("/api/scans")
def scans_list():
    """Every page ever scanned, newest first, with its status and (once read) the batch it went into."""
    out = rows("""SELECT s.*, b.name batch_name FROM scans s LEFT JOIN batches b ON b.id=s.batch_id ORDER BY s.id DESC""")
    counts = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return jsonify(scans=[dict(_scan_row(r), batch_name=r["batch_name"]) for r in out], counts=counts, total=len(out))


@app.post("/api/scans/<int:sid>/discard")
def scan_discard(sid):
    """Take a page out of the queue (kept in the log as 'discarded')."""
    run("UPDATE scans SET status='discarded' WHERE id=? AND status IN ('scanned','failed')", sid)
    return jsonify(pages=_queue())


@app.post("/api/scans/<int:sid>/requeue")
def scan_requeue(sid):
    """Put a failed or discarded page back in the queue to be read again."""
    run("UPDATE scans SET status='scanned', note='' WHERE id=? AND status IN ('failed','discarded')", sid)
    return jsonify(pages=_queue())


@app.post("/api/scans/<int:sid>/reread")
def scan_reread(sid):
    """Read an already-scraped page again (e.g. with the AI engine) into a new batch."""
    r = one("SELECT * FROM scans WHERE id=?", sid)
    if not r or not (SCAN_DIR / r["file"]).exists():
        return jsonify(error="The scanned image is no longer on disk"), 404
    b = request.get_json(silent=True) or {}
    run("UPDATE scans SET status='reading', note='' WHERE id=?", sid)
    return _start_batch([(f"Scanner re-read #{sid}.jpg", (SCAN_DIR / r["file"]).read_bytes(), sid)],
                        b.get("name") or f"Re-read of scan #{sid}", b.get("engine"))


@app.get("/api/jobs/<jid>")
def job_get(jid):
    if jid not in JOBS:
        abort(404)
    return jsonify(JOBS[jid])


@app.put("/api/batches/<int:bid>")
def batch_rename(bid):
    name = (request.get_json().get("name") or "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    run("UPDATE batches SET name=? WHERE id=?", name, bid)
    return jsonify(ok=True)


@app.get("/api/batches/<int:bid>")
def batch_get(bid):
    if bid == 0:  # every batch together, for reviewing a whole job at once
        b = {"id": 0, "name": "All batches", "only_marked": 0}
        b["pages"] = rows("SELECT p.*, b.name batch_name FROM pages p JOIN batches b ON b.id=p.batch_id ORDER BY p.batch_id, p.id")
        b["rows"] = rows("""SELECT r.*, g.name group_name FROM rows r LEFT JOIN groups g ON g.id=r.group_id
                            ORDER BY r.batch_id, r.page_id, r.y, r.id""")
    else:
        b = one("SELECT * FROM batches WHERE id=?", bid)
        if not b:
            abort(404)
        b["pages"] = rows("SELECT p.*, ? batch_name FROM pages p WHERE p.batch_id=? ORDER BY p.id", b["name"], bid)
        b["rows"] = rows("""SELECT r.*, g.name group_name FROM rows r LEFT JOIN groups g ON g.id=r.group_id
                            WHERE r.batch_id=? ORDER BY r.page_id, r.y, r.id""", bid)
    # a phone number that appears on more than one row (in any batch) is flagged on each of them
    seen = {}
    for x in rows("SELECT id, phones FROM rows WHERE phones IS NOT NULL AND phones!='' AND status!='header'"):
        for t in set(_phone_keys(x["phones"])):
            seen[t] = seen.get(t, 0) + 1
    for r in b["rows"]:
        r["dup"] = " ; ".join(t for t in dict.fromkeys(_phone_keys(r["phones"])) if seen.get(t, 0) > 1)
    return jsonify(b)


@app.delete("/api/batches/<int:bid>")
def batch_delete(bid):
    run("DELETE FROM rows WHERE batch_id=?", bid)
    run("DELETE FROM pages WHERE batch_id=?", bid)
    run("DELETE FROM batches WHERE id=?", bid)
    return jsonify(ok=True)


# ---------- rows ----------
@app.put("/api/rows/<int:rid>")
def row_update(rid):
    """Edit any field of a scanned row. Editing never changes confirmed/pending or the group."""
    body = request.get_json()
    if "number" in body and "phones" not in body:  # keep the cleaned phone list in step with the edited contact text
        body["phones"] = table.phones(body["number"] or "")
    sets, args = [], []
    for k in COLS + ["status"]:
        if k in body:
            sets.append(f"{k}=?"); args.append(body[k])
    if any(k in body for k in COLS):
        sets.append("conf=1")  # human-verified
    if sets:
        run(f"UPDATE rows SET {','.join(sets)} WHERE id=?", *args, rid)
    return jsonify(phones=body.get("phones"))


@app.post("/api/rows/<int:rid>/insert-after")
def row_insert_after(rid):
    r = one("SELECT * FROM rows WHERE id=?", rid)
    if not r:
        abort(404)
    nxt = one("SELECT MIN(y) y FROM rows WHERE page_id=? AND (y>? OR (y=? AND id>?))", r["page_id"], r["y"], r["y"], rid)["y"]
    y = (r["y"] + nxt) / 2 if nxt is not None and nxt > r["y"] else r["y"] + 0.0002
    new = run("INSERT INTO rows(batch_id,page_id,y,h,first,middle,last,number,phones,meal,other,raw,conf) VALUES(?,?,?,?,'','','','','','','','',1)",
              r["batch_id"], r["page_id"], y, r["h"])
    return jsonify(id=new)


@app.post("/api/rows/bulk")
def rows_bulk():
    """Apply one action to many rows: delete | confirm | pending | set (field + value)."""
    b = request.get_json()
    ids = b.get("ids") or []
    if not ids:
        return jsonify(changed=0)
    ph = marks(len(ids))
    act = b.get("action")
    if act == "delete":
        run(f"DELETE FROM rows WHERE id IN ({ph})", *ids)
    elif act in ("confirm", "pending"):
        run(f"UPDATE rows SET status=? WHERE status!='header' AND id IN ({ph})", "confirmed" if act == "confirm" else "pending", *ids)
    elif act == "set" and b.get("field") in COLS and b["field"] != "phones":
        run(f"UPDATE rows SET {b['field']}=?, conf=1 WHERE id IN ({ph})", b.get("value", ""), *ids)
        if b["field"] == "number":
            for r in rows(f"SELECT id, number FROM rows WHERE id IN ({ph})", *ids):
                run("UPDATE rows SET phones=? WHERE id=?", table.phones(r["number"] or ""), r["id"])
    else:
        return jsonify(error="Unknown action"), 400
    return jsonify(changed=len(ids))


@app.post("/api/pages/<int:pid>/rows")
def row_add(pid):
    p = one("SELECT * FROM pages WHERE id=?", pid)
    if not p:
        abort(404)
    last = one("SELECT MAX(y) y FROM rows WHERE page_id=?", pid)["y"] or 0
    rid = run("INSERT INTO rows(batch_id,page_id,y,h,first,middle,last,number,phones,meal,other,raw,conf) VALUES(?,?,?,0.03,'','','','','','','','',1)",
              p["batch_id"], pid, min(last + 0.001, 1))
    return jsonify(id=rid)


@app.delete("/api/rows/<int:rid>")
def row_delete(rid):
    run("DELETE FROM rows WHERE id=?", rid)
    return jsonify(ok=True)


@app.post("/api/batches/<int:bid>/confirm")
def confirm_many(bid):
    ids = (request.get_json(silent=True) or {}).get("ids")
    if bid == 0 and not ids:
        run("UPDATE rows SET status='confirmed' WHERE status!='header'")
    elif ids:
        run(f"UPDATE rows SET status='confirmed' WHERE batch_id=? AND status!='header' AND id IN ({marks(len(ids))})", bid, *ids)
    else:
        run("UPDATE rows SET status='confirmed' WHERE batch_id=? AND status!='header'", bid)
    return jsonify(ok=True)


_PAGE_CACHE = {}


def _page_image(name):
    """Decoded page images, a few at a time: reviewing thousands of rows asks for many crops of the same page."""
    if name not in _PAGE_CACHE:
        if len(_PAGE_CACHE) >= 6:
            _PAGE_CACHE.pop(next(iter(_PAGE_CACHE)))
        _PAGE_CACHE[name] = cv2.imread(str(IMG / name))
    return _PAGE_CACHE[name]


@app.get("/api/rows/<int:rid>/crop")
def row_crop(rid):
    r = one("SELECT r.y, r.h, p.image FROM rows r JOIN pages p ON p.id=r.page_id WHERE r.id=?", rid)
    if not r:
        abort(404)
    im = _page_image(r["image"])
    H = im.shape[0]
    half = max(r["h"] * H * 0.9, 26)
    y0, y1 = int(max(0, r["y"] * H - half)), int(min(H, r["y"] * H + half))
    ok, buf = cv2.imencode(".jpg", im[y0:y1], [cv2.IMWRITE_JPEG_QUALITY, 80])
    return Response(buf.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


# ---------- groups ----------
def _group_id(name):
    name = name.strip()
    g = one("SELECT id FROM groups WHERE name=?", name)
    return g["id"] if g else run("INSERT INTO groups(name) VALUES(?)", name)


@app.get("/api/groups")
def groups_list():
    return jsonify(rows("""SELECT g.*, (SELECT COUNT(*) FROM rows r WHERE r.group_id=g.id) count
                           FROM groups g ORDER BY g.name"""))


@app.post("/api/groups")
def group_create():
    name = (request.get_json().get("name") or "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    return jsonify(id=_group_id(name))


@app.put("/api/groups/<int:gid>")
def group_rename(gid):
    name = (request.get_json().get("name") or "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    if one("SELECT 1 x FROM groups WHERE name=? AND id!=?", name, gid):
        return jsonify(error="A group with that name already exists"), 400
    run("UPDATE groups SET name=? WHERE id=?", name, gid)
    return jsonify(ok=True)


@app.delete("/api/groups/<int:gid>")
def group_delete(gid):
    run("UPDATE rows SET group_id=NULL WHERE group_id=?", gid)
    run("DELETE FROM groups WHERE id=?", gid)
    return jsonify(ok=True)


@app.post("/api/groups/assign")
def group_assign():
    """Add confirmed rows to a group (existing id, or new name). Neither given = remove from any group."""
    b = request.get_json()
    ids = b.get("ids") or []
    gid = b.get("group_id")
    if b.get("new_name") and b["new_name"].strip():
        gid = _group_id(b["new_name"])
    if not ids:
        return jsonify(moved=0, skipped=0)
    if gid is None:
        run(f"UPDATE rows SET group_id=NULL WHERE id IN ({marks(len(ids))})", *ids)
        return jsonify(moved=len(ids), skipped=0)
    n = len(rows(f"SELECT id FROM rows WHERE status='confirmed' AND id IN ({marks(len(ids))})", *ids))
    run(f"UPDATE rows SET group_id=? WHERE status='confirmed' AND id IN ({marks(len(ids))})", gid, *ids)
    return jsonify(moved=n, skipped=len(ids) - n, group_id=gid)


@app.post("/api/batches/<int:bid>/auto-meal")
def auto_meal(bid):  # bid 0 = all batches
    """Create/fill 'Dinner' (D) and 'Lunch' (L) groups from the confirmed rows of a batch."""
    out = {}
    for letter, name in (("D", "Dinner"), ("L", "Lunch"), ("\u2713", "Ticked (meal not specified)")):
        ids = [r["id"] for r in rows("SELECT id FROM rows WHERE (batch_id=? OR ?=0) AND status='confirmed' AND UPPER(TRIM(meal))=?", bid, bid, letter)]
        if ids:
            run(f"UPDATE rows SET group_id=? WHERE id IN ({marks(len(ids))})", _group_id(name), *ids)
        out[name] = len(ids)
    return jsonify(out)


@app.get("/api/groups/<int:gid>")
def group_members(gid):
    return jsonify(rows("""SELECT r.*, b.name batch, p.source, p.page_no FROM rows r JOIN pages p ON p.id=r.page_id
                           JOIN batches b ON b.id=r.batch_id WHERE r.group_id=? ORDER BY r.batch_id, r.page_id, r.y""", gid))


# ---------- export ----------
def _csv(recs, cols, meta):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["group"] + (["source", "page"] if meta else []) + cols)
    for r in recs:
        w.writerow([r.get("group_name") or ""] + ([r["source"], r["page_no"]] if meta else []) + [r[c] for c in cols])
    return "﻿" + buf.getvalue()


def _xlsx(by_group, cols, meta):
    from openpyxl import Workbook
    wb = Workbook(); wb.remove(wb.active)
    used = set()
    for name, recs in by_group.items():
        title = re.sub(r"[\\/*?:\[\]]+", "_", name)[:31] or "Sheet"
        base, n = title, 2
        while title.lower() in used:
            title = f"{base[:28]}_{n}"; n += 1
        used.add(title.lower())
        ws = wb.create_sheet(title)
        ws.append(["group"] + (["source", "page"] if meta else []) + cols)
        for r in recs:
            ws.append([r.get("group_name") or ""] + ([r["source"], r["page_no"]] if meta else []) + [str(r[c] or "") for c in cols])
        for col in ws.columns:  # readable widths
            ws.column_dimensions[col[0].column_letter].width = min(60, max(10, max(len(str(c.value or "")) for c in col) + 2))
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


@app.get("/api/export")
def export():
    cols = [c for c in request.args.get("cols", ",".join(COLS)).split(",") if c in COLS]
    meta = request.args.get("meta") == "1"
    fmt = request.args.get("fmt", "csv")
    where, args = "r.status='confirmed'", []
    scope = request.args.get("scope", "all-groups")
    if scope.startswith("group:"):
        where += " AND r.group_id=?"; args.append(int(scope[6:]))
    elif scope.startswith("batch:"):
        if int(scope[6:]) != 0:  # batch:0 = every batch
            where += " AND r.batch_id=?"; args.append(int(scope[6:]))
    else:
        where += " AND r.group_id IS NOT NULL"
    recs = rows(f"""SELECT r.*, g.name group_name, p.source, p.page_no FROM rows r JOIN pages p ON p.id=r.page_id
                    LEFT JOIN groups g ON g.id=r.group_id WHERE {where} ORDER BY g.name, r.batch_id, r.page_id, r.y""", *args)
    split = request.args.get("split") == "1"
    by = {}
    for r in recs:
        by.setdefault((r["group_name"] or "Ungrouped") if split else "Export", []).append(r)
    if fmt == "xlsx":
        return Response(_xlsx(by or {"Export": []}, cols, meta),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=export.xlsx"})
    if split:  # one CSV per group, zipped
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, rs in by.items():
                z.writestr(re.sub(r'[\\/:*?"<>|]+', "_", name) + ".csv", _csv(rs, cols, meta))
        return Response(buf.getvalue(), mimetype="application/zip",
                        headers={"Content-Disposition": "attachment; filename=groups.zip"})
    return Response(_csv(recs, cols, meta), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=export.csv"})


# ---------- projects ----------
PROJECTS_FILE = DATA / "projects.json"
PROJECT_FORMAT = "omr-project"


def _reg():
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"current": None, "projects": []}


def _save_reg(reg):
    PROJECTS_FILE.write_text(json.dumps(reg, indent=1), encoding="utf-8")


def _pdir(pid):
    return DATA / "projects" / pid


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _new_project(name):
    reg = _reg()
    pid = "p" + uuid.uuid4().hex[:8]
    _pdir(pid).mkdir(parents=True, exist_ok=True)
    reg["projects"].append({"id": pid, "name": name, "created": _now(), "opened": _now()})
    _save_reg(reg)
    return pid


def _busy():
    return AUTO["running"] or any(j["status"] == "running" for j in JOBS.values())


def _open_project(pid):
    global IMG, SCAN_DIR
    reg = _reg()
    meta = next((p for p in reg["projects"] if p["id"] == pid), None)
    if not meta:
        raise KeyError(pid)
    PROJ.update(id=pid, dir=_pdir(pid))
    IMG, SCAN_DIR = PROJ["dir"] / "images", PROJ["dir"] / "scans"
    IMG.mkdir(parents=True, exist_ok=True)
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    _PAGE_CACHE.clear()
    AUTO["batch_id"], AUTO["error"] = None, ""
    _startup()  # creates the tables, and re-queues pages that were mid-read when the app last closed
    meta["opened"] = _now()
    reg["current"] = pid
    _save_reg(reg)


def _init_projects():
    DATA.mkdir(parents=True, exist_ok=True)
    reg = _reg()
    if not reg["projects"]:
        pid = _new_project("My first project")
        for n in ("omr2.db", "images", "scans"):  # data from before projects existed becomes the first project
            if (DATA / n).exists():
                shutil.move(str(DATA / n), str(_pdir(pid) / n))
        reg = _reg()
        reg["current"] = pid
        _save_reg(reg)
    cur = reg["current"] if any(p["id"] == reg["current"] for p in reg["projects"]) else reg["projects"][0]["id"]
    _open_project(cur)


def _folder_mb(d):
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1048576, 1)


def _stats(pid):
    d = _pdir(pid)
    out = {"scans": 0, "waiting": 0, "scraped": 0, "failed": 0, "batches": 0, "rows": 0, "confirmed": 0, "groups": 0,
           "size_mb": _folder_mb(d) if d.exists() else 0}
    f = d / "omr2.db"
    if f.exists():
        try:
            con = sqlite3.connect(f)
            q = lambda sql: con.execute(sql).fetchone()[0]
            out.update(scans=q("SELECT COUNT(*) FROM scans"), waiting=q("SELECT COUNT(*) FROM scans WHERE status='scanned'"),
                       scraped=q("SELECT COUNT(*) FROM scans WHERE status='scraped'"), failed=q("SELECT COUNT(*) FROM scans WHERE status='failed'"),
                       batches=q("SELECT COUNT(*) FROM batches"), rows=q("SELECT COUNT(*) FROM rows"),
                       confirmed=q("SELECT COUNT(*) FROM rows WHERE status='confirmed'"), groups=q("SELECT COUNT(*) FROM groups"))
            con.close()
        except sqlite3.Error:
            pass
    return out


@app.get("/api/projects")
def projects_list():
    reg = _reg()
    return jsonify(current=reg["current"], busy=_busy(),
                   projects=[dict(p, current=p["id"] == reg["current"], stats=_stats(p["id"])) for p in reg["projects"]])


def _need_idle():
    if _busy():
        return jsonify(error="Pages are being read right now. Wait for that to finish before switching or changing projects."), 409
    return None


@app.post("/api/projects")
def project_create():
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name:
        return jsonify(error="Give the project a name"), 400
    if (r := _need_idle()):
        return r
    pid = _new_project(name)
    _open_project(pid)
    return jsonify(id=pid)


@app.post("/api/projects/<pid>/open")
def project_open(pid):
    if (r := _need_idle()):
        return r
    try:
        _open_project(pid)
    except KeyError:
        abort(404)
    return jsonify(ok=True)


@app.put("/api/projects/<pid>")
def project_rename(pid):
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    reg = _reg()
    meta = next((p for p in reg["projects"] if p["id"] == pid), None)
    if not meta:
        abort(404)
    meta["name"] = name
    _save_reg(reg)
    return jsonify(ok=True)


@app.delete("/api/projects/<pid>")
def project_delete(pid):
    reg = _reg()
    if pid == reg["current"]:
        return jsonify(error="Open another project first: the open project cannot be deleted."), 400
    if not any(p["id"] == pid for p in reg["projects"]):
        abort(404)
    shutil.rmtree(_pdir(pid), ignore_errors=True)
    reg["projects"] = [p for p in reg["projects"] if p["id"] != pid]
    _save_reg(reg)
    return jsonify(ok=True)


def _write_package(pid, dest):
    """One .omrproject file (a zip): manifest, a consistent copy of the database, page images and scans.
    API keys and options are global and are never included."""
    d = _pdir(pid)
    meta = next(p for p in _reg()["projects"] if p["id"] == pid)
    with tempfile.TemporaryDirectory() as tmp:
        dbcopy = Path(tmp) / "omr2.db"
        if (d / "omr2.db").exists():  # sqlite backup API: safe even while the app is writing
            src, dst = sqlite3.connect(d / "omr2.db"), sqlite3.connect(dbcopy)
            src.backup(dst)
            dst.close(); src.close()
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({"format": PROJECT_FORMAT, "version": 1, "name": meta["name"],
                                                    "created": meta["created"], "saved": _now(), "app": "OMR Utility"}))
            if dbcopy.exists():
                z.write(dbcopy, "omr2.db")
            for sub in ("images", "scans"):
                for f in sorted((d / sub).glob("*.jpg")):
                    z.write(f, f"{sub}/{f.name}", compress_type=zipfile.ZIP_STORED)  # JPEGs are already compressed


@app.get("/api/projects/<pid>/package")
def project_package(pid):
    meta = next((p for p in _reg()["projects"] if p["id"] == pid), None)
    if not meta:
        abort(404)
    tmp = Path(tempfile.mkdtemp()) / "project.omrproject"
    _write_package(pid, tmp)
    name = re.sub(r'[\\/:*?"<>|]+', "_", meta["name"]) + ".omrproject"
    return send_file(tmp, as_attachment=True, download_name=name, mimetype="application/zip")


@app.post("/api/projects/<pid>/save-to")
def project_save_to(pid):
    """Desktop app: write the project file to a path chosen in a Save As dialog."""
    path = (request.get_json(silent=True) or {}).get("path", "")
    if not path:
        return jsonify(error="No path given"), 400
    if not path.lower().endswith(".omrproject"):
        path += ".omrproject"
    if not any(p["id"] == pid for p in _reg()["projects"]):
        abort(404)
    try:
        _write_package(pid, path)
    except OSError as e:
        return jsonify(error=f"Could not write the file: {e}"), 400
    return jsonify(path=path, size_mb=round(os.path.getsize(path) / 1048576, 1))


_SAFE_NAME = re.compile(r"[\w\-. ()]+\.jpg", re.I)


def _import_package(fileobj):
    try:
        z = zipfile.ZipFile(fileobj)
        manifest = json.loads(z.read("manifest.json"))
    except Exception:
        raise ValueError("That is not an OMR Utility project file.")
    if manifest.get("format") != PROJECT_FORMAT:
        raise ValueError("That is not an OMR Utility project file.")
    if manifest.get("version", 1) > 1:
        raise ValueError("This project file was saved by a newer version of the app.")
    names = {p["name"] for p in _reg()["projects"]}
    name = manifest.get("name") or "Imported project"
    if name in names:
        name += " (imported)"
        while name in names:
            name += " 2"
    pid = _new_project(name)
    d = _pdir(pid)
    try:
        (d / "images").mkdir(exist_ok=True)
        (d / "scans").mkdir(exist_ok=True)
        for info in z.infolist():
            if info.is_dir():
                continue
            member = info.filename.replace("\\", "/")
            parts = member.split("/")
            if member == "omr2.db":
                target = d / "omr2.db"
            elif len(parts) == 2 and parts[0] in ("images", "scans") and _SAFE_NAME.fullmatch(parts[1]):
                target = d / parts[0] / parts[1]
            else:
                continue  # ignore anything unexpected (also blocks path tricks)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        con = sqlite3.connect(d / "omr2.db")  # must be a readable database of ours
        con.execute("SELECT COUNT(*) FROM rows").fetchone(); con.execute("SELECT COUNT(*) FROM scans").fetchone()
        con.close()
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        reg = _reg()
        reg["projects"] = [p for p in reg["projects"] if p["id"] != pid]
        _save_reg(reg)
        raise ValueError(f"The project file is damaged or incomplete ({e}).")
    return pid, name


@app.post("/api/projects/import")
def project_import():
    try:
        if "file" in request.files:
            pid, name = _import_package(request.files["file"].stream)
        else:
            path = (request.get_json(silent=True) or {}).get("path", "")
            if not path or not os.path.isfile(path):
                return jsonify(error="File not found"), 400
            with open(path, "rb") as f:
                pid, name = _import_package(f)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(id=pid, name=name)


_init_projects()


if __name__ == "__main__":
    app.run(port=5000, debug=False)
