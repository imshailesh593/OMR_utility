"""Read ruled tables (like the contact sheets) cell by cell.

1. Find the table's four border lines and rectify the page so rules are straight.
2. Find the column rules and each column's own row rules (merged cells have fewer rules).
3. Take blue-ink marks out of the picture, then OCR each cell on its own, so text can never
   cross into a neighbouring column and multi-line cells stay together.
"""
import re

import cv2
import numpy as np

from . import scan

WARP_W = 1700
LINE_DIL = 3
READER = "strips"
OPTS = {"tries": [(72, False)]}


def _pieces(mask, min_len, axis):
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, _a = stats[i]
        if (w if axis == "h" else h) < min_len:
            continue
        ys, xs = np.nonzero(lab[y:y + h, x:x + w] == i)
        out.append({"pts": np.column_stack([xs + x, ys + y]).astype(np.float32),
                    "x0": x, "x1": x + w, "y0": y, "y1": y + h, "ym": y + h / 2, "xm": x + w / 2})
    return out


def _fit(pts):
    vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
    return float(vx), float(vy), float(x0), float(y0)


def _grow(seed, pieces, tol=6.0):
    """Collect every piece lying on the same (slightly slanted) line as the seed."""
    group = [seed]
    for _ in range(4):
        vx, vy, x0, y0 = _fit(np.vstack([p["pts"] for p in group]))
        nxt = []
        for p in pieces:
            # perpendicular distance of piece points to the fitted line
            d = np.abs((p["pts"][:, 0] - x0) * vy - (p["pts"][:, 1] - y0) * vx)
            if np.median(d) < tol:
                nxt.append(p)
        if len(nxt) == len(group):
            break
        group = nxt
    return _fit(np.vstack([p["pts"] for p in group])), group


def _intersect(l1, l2):
    (vx1, vy1, x1, y1), (vx2, vy2, x2, y2) = l1, l2
    a = np.array([[vx1, -vx2], [vy1, -vy2]])
    t = np.linalg.solve(a, np.array([x2 - x1, y2 - y1]))
    return [x1 + t[0] * vx1, y1 + t[0] * vy1]


def _binary(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)


def rectify(img):
    """Return the table cropped and straightened, or None if no ruled table is found."""
    h, w = img.shape[:2]
    bw = _binary(img)
    hor = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (w // 25, 1)))
    ver = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, h // 60)))
    vp = sorted(_pieces(ver, h // 60, "v"), key=lambda p: p["xm"])
    clusters = []
    for p in vp:
        if clusters and p["xm"] - clusters[-1][-1]["xm"] < 40:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    clusters = [c for c in clusters if sum(p["y1"] - p["y0"] for p in c) > 0.2 * h]
    if len(clusters) < 2:
        return None
    left, right = clusters[0], clusters[-1]
    Lf, Rf = _fit(np.vstack([p["pts"] for p in left])), _fit(np.vstack([p["pts"] for p in right]))
    ytop = min(p["y0"] for c in (left, right) for p in c) - 40
    ybot = max(p["y1"] for c in (left, right) for p in c) + 40
    lx = min(p["x0"] for p in left) - 60
    rx = max(p["x1"] for p in right) + 60
    hp = [p for p in _pieces(hor, w // 12, "h") if p["x0"] >= lx and p["x1"] <= rx and ytop <= p["ym"] <= ybot]
    if len(hp) < 3:
        return None
    Tf, _ = _grow(min(hp, key=lambda p: p["ym"]), hp)
    Bf, _ = _grow(max(hp, key=lambda p: p["ym"]), hp)
    try:
        tl, tr, br, bl = _intersect(Tf, Lf), _intersect(Tf, Rf), _intersect(Bf, Rf), _intersect(Bf, Lf)
    except np.linalg.LinAlgError:
        return None
    src = np.float32([tl, tr, br, bl])
    wid = (np.linalg.norm(src[1] - src[0]) + np.linalg.norm(src[2] - src[3])) / 2
    hei = (np.linalg.norm(src[3] - src[0]) + np.linalg.norm(src[2] - src[1])) / 2
    if wid < w * 0.3 or hei < h * 0.1:
        return None
    Hd = int(WARP_W * hei / wid)
    M = cv2.getPerspectiveTransform(src, np.float32([[0, 0], [WARP_W, 0], [WARP_W, Hd], [0, Hd]]))
    return cv2.warpPerspective(img, M, (WARP_W, Hd), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


def _peaks(profile, thr, gap):
    idx = np.nonzero(profile >= thr)[0]
    if not len(idx):
        return []
    groups, cur = [], [idx[0]]
    for i in idx[1:]:
        if i - cur[-1] <= gap:
            cur.append(i)
        else:
            groups.append(cur); cur = [i]
    groups.append(cur)
    return [float(np.mean(g)) for g in groups]


def grid(warped):
    """Column boundaries and, per column, its own row boundaries (x/y in warped pixels)."""
    Hd, W = warped.shape[:2]
    bw = _binary(warped)
    hor = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (W // 40, 1)))
    ver = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, Hd // 120)))
    horD = cv2.dilate(hor, np.ones((7, 1), np.uint8))
    verD = cv2.dilate(ver, np.ones((1, 7), np.uint8))
    xs = _peaks(verD.sum(axis=0) / 255, 0.3 * Hd, 12)
    xs = [x for x in xs if 8 < x < W - 8]
    cols = [0.0] + xs + [float(W)]
    # drop slivers (double rules)
    cols = [c for i, c in enumerate(cols) if i == 0 or c - cols[i - 1] > 40]
    rows = []
    for a, b in zip(cols[:-1], cols[1:]):
        sl = horD[:, int(a) + 6:int(b) - 6]
        ys = _peaks(sl.sum(axis=1) / 255, 0.4 * sl.shape[1], 6)
        if not ys or ys[0] > 14:
            ys = [0.0] + ys
        if Hd - ys[-1] > 14:
            ys = ys + [float(Hd)]
        rows.append(ys)
    line_mask = cv2.dilate(cv2.bitwise_or(hor, ver), np.ones((LINE_DIL, LINE_DIL), np.uint8))
    return cols, rows, line_mask


def _digits(t):
    return re.sub(r"\D", "", t)


def _canonical(d):
    """Indian mobile numbers: 91XXXXXXXXXX (or a misread '+' in front of it) -> +91XXXXXXXXXX."""
    if len(d) == 13 and d[1:3] == "91":  # '+' read as a digit
        d = d[1:]
    return "+" + d if len(d) == 12 and d.startswith("91") else d


def phones(text):
    """Cleaned contact numbers: split on | : ::: / newline, one entry per distinct number.

    The cell as scanned is kept separately; this column is only the tidy version for sorting/export.
    """
    out, seen = [], set()
    for tok in re.split(r"\s*(?:\||:+|：+|/|\n)\s*", text):
        parts = tok.split()
        # two full numbers run together with just a space
        if len(_digits(tok)) > 13 and len(parts) > 1 and all(len(_digits(p)) >= 10 for p in parts):
            toks = parts
        else:
            toks = [tok]
        for t in toks:
            d = _digits(t)
            if len(d) < 3:
                continue
            if len(d) == 20:  # the same cell read twice with the separator lost
                ds = [d[:10], d[10:]]
            elif len(d) == 24:
                ds = [d[:12], d[12:]]
            elif len(d) == 22:
                ds = [d[:12], d[12:]] if d.startswith("91") else [d[:10], d[10:]]
            else:
                ds = [d]
            for d in ds:
                key = d[-10:]
                if key in seen:
                    continue
                seen.add(key)
                out.append(_canonical(d))
    return " ; ".join(out)


HEAD = {"first": r"first", "middle": r"middle", "last": r"last", "number": r"contact|number|phone|mobile"}
DEFAULT = ["first", "middle", "last", "number"]


def _text_lines(gray):
    """Split a cell into its text lines by ink projection -> [(y0, y1, x0, x1)]."""
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    bw[gray > 175] = 0  # ignore faint paper texture
    h, w = bw.shape
    prof = bw.sum(axis=1) / 255
    on = prof >= max(1.5, 0.004 * w)
    lines, start = [], None
    for y in range(h + 1):
        v = on[y] if y < h else False
        if v and start is None:
            start = y
        elif not v and start is not None:
            if lines and start - lines[-1][1] <= 3:  # bridge tiny gaps (i-dots, accents)
                lines[-1] = (lines[-1][0], y)
            else:
                lines.append((start, y))
            start = None
    out = []
    for y0, y1 in lines:
        if y1 - y0 < 8:
            continue
        cols = np.nonzero(bw[y0:y1].sum(axis=0))[0]
        if len(cols):
            out.append((y0, y1, int(cols[0]), int(cols[-1]) + 1))
    return out


def _read_line(piece):
    """Recognise one text line. Tries a few enlargements and keeps the most confident reading."""
    best = ("", 0.0)
    for target, clahe in OPTS["tries"]:  # target = line height to enlarge to
        scale = float(np.clip(target / max(1, piece.shape[0] - 8), 1.0, 4.0))
        p = piece
        if clahe:
            p = cv2.cvtColor(cv2.createCLAHE(2.0, (4, 4)).apply(cv2.cvtColor(p, cv2.COLOR_BGR2GRAY)), cv2.COLOR_GRAY2BGR)
        p = cv2.resize(p, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        p = cv2.copyMakeBorder(p, 8, 8, 10, 10, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        res, _ = scan.ocr()(p, use_det=False, use_cls=False, use_rec=True)
        if res and float(res[0][1]) > best[1]:
            best = (str(res[0][0]), float(res[0][1]))
    return best


def read_strips(clean, cols, master, line_mask, tile=1100):
    """Full text detection + recognition on tall column strips (cut at row rules).

    Each column is read on its own so text can never run across a column line, but the
    recogniser still sees whole words in context, which reads far better than isolated crops.
    """
    Hd = clean.shape[0]
    white = clean.copy()
    white[line_mask > 0] = 255
    # tile boundaries: row rules, at most `tile` pixels apart
    cuts, start = [0], 0
    for m in master[1:]:
        if m - cuts[-1] > tile:
            cuts.append(start)
        start = m
    cuts = sorted(set(cuts + [Hd]))
    items = []
    for ci, (a, b) in enumerate(zip(cols[:-1], cols[1:])):
        xa, xb = int(a) + 4, int(b) - 4
        for y0, y1 in zip(cuts[:-1], cuts[1:]):
            if y1 - y0 < 20:
                continue
            strip = white[int(y0):int(y1), xa:xb]
            if np.count_nonzero(cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY) < 140) < 30:
                continue
            if np.median(cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)) < 110:
                strip = 255 - strip
            strip = cv2.copyMakeBorder(strip, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            res, _ = scan.ocr()(strip)
            for box, text, conf in res or []:
                px = [q[0] for q in box]; py = [q[1] for q in box]
                items.append({"col": ci, "text": str(text), "conf": float(conf), "x": xa + min(px) - 20,
                              "y": y0 + float(np.mean(py)) - 20, "h": max(py) - min(py)})
    return items


def read_cells(clean, cols, rows, line_mask):
    """OCR every cell, one text line at a time. -> [{col, x, y, text, conf}] in warped coordinates."""
    Hd, W = clean.shape[:2]
    white = clean.copy()
    white[line_mask > 0] = 255
    gray_all = cv2.cvtColor(white, cv2.COLOR_BGR2GRAY)
    items = []
    for ci, (a, b) in enumerate(zip(cols[:-1], cols[1:])):
        ys = rows[ci]
        if len(ys) < 4:
            ys = sorted(set(ys) | set(rows[-1]))
        for y0, y1 in zip(ys[:-1], ys[1:]):
            if y1 - y0 < 14:
                continue
            xa, xb, ya, yb = int(a) + 5, int(b) - 5, int(y0) + 3, int(y1) - 3
            if yb <= ya or xb <= xa:
                continue
            gray = gray_all[ya:yb, xa:xb]
            for ly0, ly1, lx0, lx1 in _text_lines(gray):
                piece = white[ya + max(0, ly0 - 4):ya + ly1 + 4, xa + max(0, lx0 - 6):xa + lx1 + 6]
                if np.median(cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)) < 110:  # light text on a dark bar
                    piece = 255 - piece
                text, conf = _read_line(piece)
                if not text.strip():
                    continue
                items.append({"col": ci, "text": text, "conf": conf,
                              "x": xa + lx0, "y": ya + (ly0 + ly1) / 2, "h": ly1 - ly0})
    return items


def _merge_lines(ys, tol=10.0):
    """Rules found in several columns are the same line: merge those within tol pixels."""
    out = []
    for y in ys:
        if out and y - out[-1][-1] <= tol:
            out[-1].append(y)
        else:
            out.append([y])
    merged = [float(np.mean(g)) for g in out]
    return [m for i, m in enumerate(merged) if i == 0 or m - merged[i - 1] > 14]


def _is_serial(t):
    return bool(re.fullmatch(r"\s*\d{1,3}[.)]?\s*", t))


def list_rows(texts, confs, master, Hd, ncol):
    """Side-by-side numbered lists (e.g. village lists): one record per entry, panel by panel."""
    nonempty = [sum(1 for r in texts if r[c].strip()) for c in range(ncol)]
    live = [c for c in range(ncol) if nonempty[c] > 2]
    panels, cur = [], []
    for c in range(ncol):
        if c in live:
            cur.append(c)
        elif cur:
            panels.append(cur); cur = []
    if cur:
        panels.append(cur)
    out = []
    for pi, cs in enumerate(panels, 1):
        serial = cs[0] if len(cs) > 1 and sum(_is_serial(texts[r][cs[0]]) for r in range(len(texts)) if texts[r][cs[0]].strip()) >= 0.6 * max(1, nonempty[cs[0]]) else None
        for r in range(len(texts)):
            num = texts[r][serial].strip() if serial is not None else ""
            name = " ".join(texts[r][c].strip() for c in cs if c != serial and texts[r][c].strip())
            if not name and not num:
                continue
            c = confs[r] or [0.0]
            out.append({"y": round((master[r] + master[r + 1]) / 2 / Hd, 5), "h": round((master[r + 1] - master[r]) / Hd, 5),
                        "first": name, "middle": "", "last": "", "number": "", "phones": "", "meal": "",
                        "other": (f"#{num} " if num else "") + f"(panel {pi})",
                        "raw": " | ".join(texts[r][c2] for c2 in cs), "conf": round(min(c), 3),
                        "status": "pending"})
    return out


def scan_table(img, ai=None):
    """-> (rows, rectified_image) or None when the page has no usable table.

    ai = {"key": ..., "model": ...} makes Claude read the text; the grid still decides which row
    and column each value belongs to. Tiles Claude could not read fall back to the local OCR.
    """
    warped = rectify(img)
    if warped is None:
        return None
    cols, rows, line_mask = grid(warped)
    if len(cols) < 3:  # need at least 2 columns to be a table
        return None
    Hd = warped.shape[0]
    master = _merge_lines(sorted(y for r in rows for y in r))  # a rule seen in any column starts a new row
    if len(master) < 4:
        return None
    nrows, ncol = len(master) - 1, len(cols) - 1

    marks, ink = scan.find_marks(warped)
    meal = [None] * nrows
    for m in marks:
        if master[0] <= m["cy"] <= master[-1]:
            r = int(np.clip(np.searchsorted(master, m["cy"]) - 1, 0, nrows - 1))
            if meal[r] is None or m["conf"] > meal[r]["conf"]:
                meal[r] = m

    ai_rows, failed = {}, [(0, nrows)]
    if ai:
        from . import ai as ai_mod
        ai_rows, failed = ai_mod.read_rows(warped, master, ncol, ai.get("key"),
                                         ai.get("model") or (ai_mod.GEMINI_DEFAULT if ai.get("provider") == "gemini" else ai_mod.DEFAULT_MODEL),
                                         ai.get("provider", "claude"))

    # local OCR, only where Claude was not used (or failed)
    cells = [[[] for _ in range(ncol)] for _ in range(nrows)]
    if failed:
        clean = warped.copy()
        clean[ink > 0] = 255
        items = (read_strips(clean, cols, master, line_mask) if READER == "strips"
                 else read_cells(clean, cols, rows, line_mask))
        for it in items:
            r = int(np.clip(np.searchsorted(master, it["y"]) - 1, 0, nrows - 1))
            cells[r][it["col"]].append(it)

    def in_failed(r):
        return any(a <= r < b for a, b in failed)

    texts, confs, mark_of = [], [], []  # per row: [cell strings], [confidences], (letter, conf) or None
    for r in range(nrows):
        if not in_failed(r):
            a = ai_rows.get(r)
            if a:
                texts.append(a["cells"]); confs.append([0.5 if a["unsure"] else 0.95])
                if a["mark"]:
                    mark_of.append((a["mark"], 0.5 if a["unsure"] else 0.95))
                elif meal[r] and meal[r]["letter"] in ("D", "L") and meal[r]["conf"] >= 0.8:
                    mark_of.append((meal[r]["letter"], 0.5))  # Claude saw no pen mark but the local detector did: flag it
                else:
                    mark_of.append(None)
            else:  # Claude listed nothing for this row = blank
                texts.append([""] * ncol); confs.append([]); mark_of.append(None)
            continue
        row_texts, row_conf = [], []
        for ci in range(ncol):
            its = sorted(cells[r][ci], key=lambda i: (round(i["y"] / 12), i["x"]))
            row_texts.append((" | " if ci == ncol - 1 else " ").join(i["text"] for i in its))
            row_conf += [i["conf"] for i in its]
        texts.append(row_texts); confs.append(row_conf)
        mark_of.append((meal[r]["letter"], meal[r]["conf"]) if meal[r] else None)

    # column roles: by header text if it can be recognised, otherwise left-to-right
    roles = (DEFAULT + ["other"] * ncol)[:ncol]
    found = {}
    for ci, t in enumerate(texts[0]):
        for role, pat in HEAD.items():
            if role not in found.values() and re.search(pat, t, re.I):
                found[ci] = role
    if len(found) >= 2:
        roles = [found.get(ci, "other") for ci in range(ncol)]
    if roles.count("number") != 1 and "number" in roles:
        roles = (DEFAULT + ["other"] * ncol)[:ncol]

    band = cv2.cvtColor(warped[int(master[0]) + 4:int(master[1]) - 4], cv2.COLOR_BGR2GRAY)
    dark_head = band.size > 0 and float(np.mean(band)) < 130  # white-on-dark title bar
    head_row = len(found) >= 2 or dark_head
    if not head_row and ncol != 4:
        return list_rows(texts, confs, master, Hd, ncol), warped
    out = []
    for r in range(nrows):
        rec = {k: "" for k in ("first", "middle", "last", "number", "other")}
        for ci in range(ncol):
            role = roles[ci]
            rec[role] = (rec[role] + " " + texts[r][ci]).strip() if rec[role] else texts[r][ci]
        m = None if (r == 0 and head_row) else mark_of[r]  # nothing handwritten counts on the title bar
        c = list(confs[r]) + ([m[1]] if m else [])
        if not any(texts[r]) and not m:
            continue
        out.append({
            "y": round((master[r] + master[r + 1]) / 2 / Hd, 5), "h": round((master[r + 1] - master[r]) / Hd, 5),
            "first": rec["first"], "middle": rec["middle"], "last": rec["last"], "number": rec["number"],
            "phones": phones(rec["number"]), "meal": m[0] if m else "", "other": rec["other"],
            "raw": " | ".join(texts[r]), "conf": round(min(c), 3) if c else 0.0,
            "status": "header" if (r == 0 and head_row) else "pending"})
    return out, warped
