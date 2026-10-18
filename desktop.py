"""Standalone desktop window around the OMR app."""
import re
import socket
import threading
import urllib.request

import webview

from app import app

PORT = 0
WINDOW = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    """Exposed to the page as window.pywebview.api. Lets exports use a normal Save As dialog."""

    def save_export(self, query: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/export?{query}") as r:
            data = r.read()
            disp = r.headers.get("Content-Disposition", "")
        m = re.search(r"filename=([^;]+)", disp)
        name = m.group(1).strip('"') if m else "export.csv"
        chosen = WINDOW.create_file_dialog(webview.SAVE_DIALOG, save_filename=name)
        if not chosen:
            return None
        path = chosen if isinstance(chosen, str) else chosen[0]
        with open(path, "wb") as f:
            f.write(data)
        return path

    def choose_save_path(self, suggested: str):
        r = WINDOW.create_file_dialog(webview.SAVE_DIALOG, save_filename=suggested,
                                      file_types=("OMR project (*.omrproject)",))
        return (r if isinstance(r, str) else r[0]) if r else None

    def choose_open_path(self):
        r = WINDOW.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                      file_types=("OMR project (*.omrproject;*.zip)",))
        return r[0] if r else None


if __name__ == "__main__":
    PORT = free_port()
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=PORT, threaded=True), daemon=True).start()
    WINDOW = webview.create_window("OMR Utility", f"http://127.0.0.1:{PORT}", js_api=Api(), width=1400, height=900,
                                   min_size=(900, 600))
    webview.start()
