# OMR Utility

A Windows desktop tool that reads printed contact / guest lists from scans, phone photos or PDFs into an editable table, then sorts the confirmed entries into groups and exports them.

Built for lists where some rows are marked in pen with **D** (dinner), **L** (lunch) or a tick.

## Features

- **Read pages** from a flatbed scanner (Windows WIA, e.g. Canon G2000), PDFs or images.
- **Table-aware reader**: finds the table, straightens the page, reads cell by cell (First / Middle / Last name, contact numbers), detects blue-pen **D / L / tick** marks.
- **Two reading engines**: local OCR (offline, free) or cloud AI vision (Claude or Gemini; needs your own API key).
- **Review & edit everything**: confidence highlighting, duplicate-phone flags, search and filters, bulk actions, undo, paginated for thousands of rows.
- **Groups & export**: Dinner / Lunch auto-groups or your own groups; export CSV or Excel with the columns you choose.
- **Page log**: every scanned page is tracked (scanned / reading / scraped / failed).
- **Auto-read while scanning**, a "marked rows only" mode for dinner/lunch sheets.
- **Projects**: each job is kept separate; save a project to one `.omrproject` file and restore it later.

## Run

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python desktop.py     # desktop window
.venv\Scripts\python app.py         # or in a browser at http://localhost:5000
```

## Build the standalone app

```
build.bat
```

The app is written to `dist\OMR Utility\`. Data is stored in `%APPDATA%\OMR Utility`.

## Notes

- API keys are stored locally in `%APPDATA%\OMR Utility\settings.json` and are never part of a saved project file.
- AI modes send page images (names and phone numbers) to the provider you choose. Use a paid / billing-enabled key if privacy matters.
- `make_sample.py` generates a small sample guest list in `sample/` for trying the app.
