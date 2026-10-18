@echo off
rem Builds the standalone desktop app into dist\OMR Utility\
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt pyinstaller
)
.venv\Scripts\pyinstaller --noconfirm --windowed --name "OMR Utility" --add-data "static;static" ^
  --collect-all rapidocr_onnxruntime --collect-all webview --collect-all anthropic --collect-all openpyxl ^
  --collect-all google.genai --collect-all google.auth ^
  --hidden-import=cv2 --hidden-import=win32com.client --hidden-import=pythoncom --hidden-import=pywintypes desktop.py
echo.
echo Done. Run: dist\OMR Utility\OMR Utility.exe
