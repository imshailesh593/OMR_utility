"""Scrape a whole page: every text line, plus anything written in blue ink."""
import re

import cv2
import numpy as np

_engine = None


def ocr():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def blue_mask(img: np.ndarray) -> np.ndarray:
    """Pixels that look like blue-pen ink: bright blue, or the dark navy a ballpen leaves when
    scanned/photographed. Neutral black print and grey shadows have no saturation and are left out."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (95, 50, 15), (140, 255, 255))


def _is_tick(xs, ys, w, h) -> bool:
    """A check mark: a short stroke down to a low point near the left, then a long stroke up to the
    upper right. Crosses, circles and scribbles also have ink in the top-left / bottom-right."""
    tot = len(xs)
    if w < 0.7 * h:
        return False
    low = np.argmax(ys)
    fx = xs[low] / w
    tl = np.count_nonzero((xs < 0.3 * w) & (ys < 0.4 * h)) / tot
    tr = np.count_nonzero((xs > 0.65 * w) & (ys < 0.45 * h)) / tot
    br = np.count_nonzero((xs > 0.65 * w) & (ys > 0.6 * h)) / tot
    return 0.08 <= fx <= 0.6 and tr >= 0.08 and tl <= 0.06 and br <= 0.06


def classify_letter(glyph: np.ndarray) -> tuple[str, float]:
    """glyph: binary mask (255 = ink) of one blue mark -> (letter, confidence).

    Judged by shape, not OCR (which is unreliable on a lone handwritten letter). A D has a bowl:
    its ink reaches out to the right at mid height, or encloses a hole. An L is a vertical stroke
    with a foot and nothing on the right above the foot. Anything else is '?' for a human to check.
    """
    ys, xs = np.nonzero(glyph)
    if len(xs) < 20:
        return "?", 0.2
    h, w = glyph.shape
    tot = len(xs)
    right_mid = np.count_nonzero((xs > 0.55 * w) & (ys > 0.15 * h) & (ys < 0.7 * h)) / tot
    left = np.count_nonzero(xs < 0.4 * w) / tot
    foot = np.count_nonzero(ys > 0.6 * h) / tot
    closed = cv2.morphologyEx(glyph, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    closed = cv2.copyMakeBorder(closed, 3, 3, 3, 3, cv2.BORDER_CONSTANT, value=0)
    cnts, hier = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    holes = [cv2.contourArea(c) for c, hh in zip(cnts, hier[0]) if hh[3] != -1] if hier is not None else []
    # a real D / L has a straight, roughly upright stem down its left side. A tick, a cross, or a
    # circle has no such stem (a circle's curved side only touches a straight line for a short stretch).
    stem = 0.0
    lmask = xs < 0.55 * w
    for deg in range(-25, 26, 5):
        xp = xs[lmask] + ys[lmask] * np.tan(np.radians(deg))
        rows_ = ys[lmask]
        bins = np.floor(xp / 3).astype(int)
        for b in np.unique(bins):
            stem = max(stem, len(set(rows_[bins == b])) / h)
    if stem < 0.6:
        if _is_tick(xs, ys, w, h):
            return "✓", 0.85
        return "?", 0.3
    if (holes and max(holes) >= 0.05 * glyph.size) or right_mid >= 0.12:
        return "D", 0.9
    if left > 0.5 and foot > 0.4 and right_mid < 0.05 and w >= 0.3 * h:
        return "L", 0.85
    return "?", 0.3


def find_marks(img: np.ndarray) -> tuple[list[dict], np.ndarray]:
    """Blue-ink marks anywhere on the page -> [{cx, cy, letter, conf}], and the blue mask."""
    mask = blue_mask(img)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    joined = cv2.dilate(mask, np.ones((9, 9), np.uint8))  # merge strokes of one letter
    n, labels, stats, cents = cv2.connectedComponentsWithStats(joined)
    marks = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 120 or min(w, h) < 14 or max(w, h) > 160:  # specks, or large things that are not a handwritten letter
            continue
        glyph = mask[y:y + h, x:x + w] * (labels[y:y + h, x:x + w] == i)
        if np.count_nonzero(glyph) < 120:
            continue
        # Ballpen over pale paper is lighter than the black print, which in a cool-toned photo also
        # looks bluish; telling them apart by brightness keeps print and shadows from becoming "marks".
        sel = (glyph > 0)
        hsv_b = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
        if np.median(hsv_b[..., 2][sel]) < 60 or np.median(hsv_b[..., 1][sel]) < 55:
            continue
        letter, conf = classify_letter(glyph)
        marks.append({"cx": x + w / 2, "cy": y + h / 2, "letter": letter, "conf": conf})
    return marks, cv2.dilate(mask, np.ones((5, 5), np.uint8))


def _is_number(t: str) -> bool:
    digits = re.sub(r"\D", "", t)
    body = re.sub(r"[\s+\-().]", "", t)
    return len(digits) >= 5 and len(digits) >= 0.6 * max(1, len(body))


def _lines(items: list[dict]) -> list[list[dict]]:
    if not items:
        return []
    med = float(np.median([i["h"] for i in items]))
    items = sorted(items, key=lambda i: i["cy"])
    lines = [[items[0]]]
    for it in items[1:]:
        cur = lines[-1]
        if it["cy"] - np.mean([c["cy"] for c in cur]) > 0.6 * med:
            lines.append([it])
        else:
            cur.append(it)
    return [sorted(l, key=lambda i: i["x0"]) for l in lines]


def scan_lines(img: np.ndarray) -> list[dict]:
    """Fallback for pages without a ruled table: one dict per text line, top to bottom."""
    from .pages import deskew
    img = deskew(img)
    H = img.shape[0]
    marks, ink = find_marks(img)
    clean = img.copy()
    clean[ink > 0] = 255  # take blue ink out so letters don't leak into names
    result, _ = ocr()(clean)
    items = []
    for box, text, conf in result or []:
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        items.append({"text": text, "conf": float(conf), "x0": min(xs), "cy": float(np.mean(ys)), "h": max(ys) - min(ys)})
    lines = _lines(items)
    rows = []
    for ln in lines:
        cy = float(np.mean([i["cy"] for i in ln]))
        h = float(np.max([i["h"] for i in ln]))
        rows.append({"cy": cy, "h": h, "items": ln, "mark": None})
    # attach each blue mark to the text line it sits on (or nearest within ~1.5 lines)
    med = float(np.median([r["h"] for r in rows])) if rows else 40
    for m in marks:
        best = min(rows, key=lambda r: abs(r["cy"] - m["cy"]), default=None)
        if best is not None and abs(best["cy"] - m["cy"]) <= max(best["h"], med) * 1.5:
            if best["mark"] is None or m["conf"] > best["mark"]["conf"]:
                best["mark"] = m
        else:
            rows.append({"cy": m["cy"], "h": med, "items": [], "mark": m})
    rows.sort(key=lambda r: r["cy"])

    out = []
    for r in rows:
        nums, alpha, small = [], [], []
        for it in r["items"]:
            t = it["text"].strip()
            if _is_number(t):
                nums.append(t)
            elif re.fullmatch(r"[\d.\s]+", t):
                small.append(t)
            else:
                alpha.append(t)
        first = last = ""
        other = list(small)
        if len(alpha) >= 2:
            first, last, other = alpha[0], alpha[1], other + alpha[2:]
        elif len(alpha) == 1:
            parts = alpha[0].split()
            first, last = parts[0], " ".join(parts[1:])
        raw = " | ".join(i["text"] for i in r["items"])
        header = bool(re.search(r"\bname\b", raw, re.I)) and not nums
        confs = [i["conf"] for i in r["items"]] + ([r["mark"]["conf"]] if r["mark"] else [])
        out.append({
            "y": round(r["cy"] / H, 4), "h": round(max(r["h"], 20) / H, 4),
            "first": first, "last": last, "number": " / ".join(nums),
            "meal": r["mark"]["letter"] if r["mark"] else "",
            "other": " ".join(other), "raw": raw,
            "conf": round(min(confs), 3) if confs else 0.0,
            "status": "header" if header else "pending",
        })
    return out


def scan_page(img: np.ndarray, ai: dict | None = None) -> tuple[list[dict], np.ndarray]:
    """-> (rows, image the row positions refer to). Ruled tables are read cell by cell."""
    from . import table
    try:
        res = table.scan_table(img, ai)
    except Exception as e:  # never lose a page because the table reader tripped, but do report AI problems
        from .ai import AIError
        if isinstance(e, AIError):
            raise
        res = None
    if res:
        return res
    rows = scan_lines(img)
    for r in rows:
        r.setdefault("middle", ""); r.setdefault("phones", "")
    return rows, img
