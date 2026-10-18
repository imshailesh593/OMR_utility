"""Turn uploaded PDFs / images into normalised page images."""
import io
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image

PAGE_WIDTH = 1700  # all pages are scaled to this width so template fractions line up


def _normalise(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    scale = PAGE_WIDTH / w
    img = cv2.resize(img, (PAGE_WIDTH, round(h * scale)), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    return img


def deskew(img: np.ndarray, max_angle: float = 5.0) -> np.ndarray:
    """Straighten slightly rotated scans using the dominant text angle."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    pts = cv2.findNonZero(bw)
    if pts is None or len(pts) < 500:
        return img
    angle = cv2.minAreaRect(pts)[-1]
    if angle > 45:
        angle -= 90
    elif angle < -45:
        angle += 90
    if abs(angle) < 0.2 or abs(angle) > max_angle:
        return img
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


def load_pages(data: bytes, filename: str) -> list[np.ndarray]:
    """Return a list of BGR page images from a PDF or image upload."""
    if filename.lower().endswith(".pdf"):
        doc = pymupdf.open(stream=data, filetype="pdf")
        pages = []
        for p in doc:
            pix = p.get_pixmap(dpi=200)
            arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
            pages.append(cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR))
        return [_normalise(p) for p in pages]
    pil = Image.open(io.BytesIO(data))
    frames = []
    for i in range(getattr(pil, "n_frames", 1)):
        pil.seek(i)
        frames.append(cv2.cvtColor(np.array(pil.convert("RGB")), cv2.COLOR_RGB2BGR))
    return [_normalise(f) for f in frames]


def save(img: np.ndarray, path: Path) -> None:
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
