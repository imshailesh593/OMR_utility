"""Scan pages straight from a Windows scanner (WIA), e.g. the Canon G2000 flatbed.

The G2000 has a flatbed only, so "scan all pages" means: put a page on the glass, scan it,
repeat, then read all of them together. WIA is the standard Windows scanner interface and is
installed with the Canon driver; no extra Canon software is needed.
"""
from pathlib import Path

WIA_FORMAT_JPEG = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
WIA_FORMAT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"

INTENT = {"color": 1, "gray": 2}
# Item property ids: current intent, x/y resolution, start x/y and extent (in pixels at that dpi)
P_INTENT, P_XRES, P_YRES, P_XPOS, P_YPOS, P_XEXT, P_YEXT = 6146, 6147, 6148, 6149, 6150, 6151, 6152
PAGE_SIZES = {"a4": (8.27, 11.69), "letter": (8.5, 11.0), "legal": (8.5, 14.0)}  # inches


class ScannerError(Exception):
    pass


def _com():
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        raise ScannerError("Scanner support needs Windows with the pywin32 package.")
    pythoncom.CoInitialize()  # each Flask request thread needs its own COM apartment
    return win32com.client


def list_devices() -> list[dict]:
    wc = _com()
    try:
        mgr = wc.Dispatch("WIA.DeviceManager")
        out = []
        for i in range(1, mgr.DeviceInfos.Count + 1):
            info = mgr.DeviceInfos.Item(i)
            if info.Type == 1:  # 1 = scanner
                out.append({"id": info.DeviceID, "name": str(info.Properties("Name").Value)})
        return out
    except Exception as e:
        raise ScannerError(f"Could not query Windows scanners: {e}")


def _set(item, prop_id, value):
    try:
        item.Properties(str(prop_id)).Value = value
    except Exception:
        pass  # not every driver exposes every property; the scanner then uses its own default


def scan_page(device_id: str | None, out_dir: Path, name: str, dpi: int = 300, mode: str = "color",
              page: str = "a4") -> Path:
    """Scan one page from the glass and save it as a JPEG. Returns the file path."""
    wc = _com()
    devices = list_devices()
    if not devices:
        raise ScannerError("No scanner found. Switch the Canon G2000 on, check the USB cable, "
                           "and make sure no other program (such as Canon IJ Scan Utility) is using it.")
    dev_id = device_id or devices[0]["id"]
    if dev_id not in [d["id"] for d in devices]:
        raise ScannerError("The chosen scanner is no longer available. Refresh the list.")
    try:
        mgr = wc.Dispatch("WIA.DeviceManager")
        device = next(mgr.DeviceInfos.Item(i).Connect() for i in range(1, mgr.DeviceInfos.Count + 1)
                      if mgr.DeviceInfos.Item(i).DeviceID == dev_id)
        item = device.Items[1]
        w_in, h_in = PAGE_SIZES.get(page, PAGE_SIZES["a4"])
        _set(item, P_INTENT, INTENT.get(mode, 1))
        _set(item, P_XRES, dpi); _set(item, P_YRES, dpi)
        _set(item, P_XPOS, 0); _set(item, P_YPOS, 0)
        _set(item, P_XEXT, int(w_in * dpi)); _set(item, P_YEXT, int(h_in * dpi))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}.jpg"
        try:
            image = item.Transfer(WIA_FORMAT_JPEG)
        except Exception:
            image = item.Transfer(WIA_FORMAT_BMP)  # some drivers only hand out BMP
            path = out_dir / f"{name}.bmp"
        if path.exists():
            path.unlink()
        image.SaveFile(str(path))
        if path.suffix == ".bmp":  # convert so everything downstream deals with JPEG
            import cv2
            im = cv2.imread(str(path)); jpg = path.with_suffix(".jpg")
            cv2.imwrite(str(jpg), im, [cv2.IMWRITE_JPEG_QUALITY, 92]); path.unlink(); path = jpg
        return path
    except ScannerError:
        raise
    except Exception as e:
        msg = str(e)
        if "0x80210006" in msg or "busy" in msg.lower():
            raise ScannerError("The scanner is busy. Close other scanning programs and try again.")
        if "0x80210005" in msg or "offline" in msg.lower():
            raise ScannerError("The scanner is switched off or disconnected.")
        if "0x80210003" in msg:
            raise ScannerError("The scanner lid is open or there is a paper problem.")
        raise ScannerError(f"Scan failed: {msg}")
