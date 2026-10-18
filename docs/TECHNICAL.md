# OMR Utility: Technical Notes

For developers who want to understand, build, or extend the app. User-facing documentation is in the [README](../README.md) and the [User Guide](USER_GUIDE.md).

## 1. Architecture

```
 desktop.py (pywebview window)  ──►  app.py (Flask, 127.0.0.1, random port)
                                        │
        static/index.html  ◄─ JSON API ─┤  omr/pages.py     load + normalise pages
        (one-file UI, no build)         │  omr/table.py     table reader
                                        │  omr/scan.py      pen marks + fallback reader
                                        │  omr/ai.py        Claude / Gemini vision
                                        │  omr/scanner.py   Windows WIA scanner
                                        ▼
                      per-project folder: SQLite + images + scans
```

- **UI**: a single `static/index.html` using plain JavaScript (small `h()` DOM helper, no framework, no build step).
- **Server**: Flask. Long work (reading pages) runs in background threads; the UI polls `/api/jobs/<id>` and `/api/autoread`.
- **Desktop shell**: `desktop.py` starts the server on a free local port and opens it in a pywebview window. It also exposes a small JavaScript API (`save_export`, `choose_save_path`, `choose_open_path`) for native file dialogs.
- **Storage**: SQLite plus image files, one folder per project. Global settings and the project list live beside the project folders.

## 2. The reading pipeline

### 2.1 Loading (`omr/pages.py`)
PDFs are rendered with PyMuPDF at 200 dpi; images are loaded with Pillow. Every page is resized to **1700 px wide** so that all pixel constants in the later steps are comparable. (This also means scanning above ~300 dpi currently adds no accuracy.)

### 2.2 Table detection and straightening (`omr/table.py: rectify`)
1. Adaptive threshold, then morphological opening with long horizontal and vertical kernels isolates ruling lines.
2. Vertical line pieces are clustered by x position; the leftmost and rightmost clusters become the table's left and right edges. Horizontal pieces are grown into the top and bottom edges.
3. The four edge lines are fitted (`cv2.fitLine`, Huber loss) and intersected to give four corners, and the table is warped to a rectangle with a perspective transform.

Residual page curl is small after this; no mesh dewarping is done.

### 2.3 Grid (`table.grid`)
In the straightened image, column boundaries are peaks of the vertical-line projection; **each column gets its own list of row rules** (merged cells have fewer rules). The master row grid is the merge of every column's rules (within 10 px), so a rule seen in any column starts a new row.

### 2.4 Reading cells
- **Local**: `read_strips` cuts each column into tall strips at row rules, removes ruling lines, and runs **RapidOCR** (detection + recognition) on every strip. Each recognised text box is assigned to a master row by its y position and to a column by the strip it came from. This keeps text from crossing column lines while the recogniser still sees whole words in context. (A per-line recognition-only reader, `read_cells`, is kept but off: it was faster and measurably less accurate.)
- **AI** (`omr/ai.py`): the straightened table is cut into tiles of 14 rows. Each tile gets a red `[n]` label for every row in a white left margin and is sent as a JPEG with a prompt and a JSON schema (`rows[{row, cells[], mark, unsure}]`). Tiles run in parallel (4 workers). The grid, not the model, decides which row and column a value belongs to. Tiles that fail fall back to local OCR; if no tile works the page raises `AIError`.
  - Claude: `anthropic` SDK, structured output through `output_config.format`, effort `low`. Default model `claude-opus-5-5` (Sonnet / Haiku selectable).
  - Gemini: `google-genai` SDK, `response_mime_type=application/json` with `response_schema`. Default model `gemini-2.5-flash` (any model id can be typed).
- Header detection: a first row whose cells match *first / middle / last / contact*, or a dark title bar.

### 2.5 Row assembly
- Roles: by default columns map left to right to *first, middle, last, number*; header text overrides this when recognised.
- **List mode** (`list_rows`): when there is no header and the column count is not 4 (for example two side-by-side numbered village lists), columns are grouped into panels separated by empty columns; each entry becomes a row with the name in *first* and `#serial (panel n)` in *other*.
- **Phones** (`table.phones`): split on `|`, `:`, `:::`, `/` and newlines, digit runs of 20 / 22 / 24 are split into two numbers, mobiles written `91XXXXXXXXXX` (or with a misread `+`) become `+91XXXXXXXXXX`, and numbers are de-duplicated by their last 10 digits. The original text is stored separately and never altered.
- If no table is found the page goes to `scan_lines` (`omr/scan.py`): full-page OCR clustered into lines, with a heuristic name/number split.

### 2.6 Pen marks (`omr/scan.py`)
- **Mask**: HSV hue 95 to 140, saturation at least 50. Candidate blobs must be 14 to 160 px, at least 120 px of ink, and have **median brightness at least 60 and saturation at least 55**. The brightness test separates ballpoint ink from black print that looks bluish in cool-toned photos.
- **Classification** (`classify_letter`): shape, not OCR. A **straight upright stem** is required for D or L (found by testing near-vertical lines through the left half of the glyph). With a stem: a closed bowl or ink at mid-right means **D**; a foot and nothing at the upper right means **L**. Without a stem: a short-then-long stroke (`_is_tick`) means a **tick**, otherwise `?`.
- Blue ink is erased before text OCR so marks don't leak into names. A mark is assigned to the row containing its centre. Marks on a header bar are ignored.
- In AI mode the model's `mark` value is used; if the model sees none but the local detector found a confident D/L, the local mark is kept with low confidence so it is flagged.

## 3. Data model (per project, SQLite `omr2.db`)

| Table | Purpose / key columns |
|---|---|
| `batches` | One reading run: `name`, `created`, `only_marked` |
| `pages` | A page that was read: `batch_id`, `source`, `page_no`, `image` (the straightened page) |
| `rows` | One person/entry: `batch_id`, `page_id`, `y`, `h` (position on the page), `first`, `middle`, `last`, `number`, `phones`, `meal`, `other`, `raw`, `conf`, `status` (`pending` / `confirmed` / `header`), `group_id` |
| `groups` | `name` (unique) |
| `scans` | The scanned-pages log: `file`, `scanned_at`, `status` (`scanned` / `reading` / `scraped` / `failed` / `discarded`), `batch_id`, `page_id`, `rows_found`, `note`, `dpi`, `mode`, `device`, `read_at` |

Schema is created and migrated in `db()` (`CREATE TABLE IF NOT EXISTS`, plus `ALTER TABLE` for columns added later). Every write commits immediately. A page's rows are inserted in a single transaction.

Global files in the data directory: `settings.json` (engine, provider, models, keys, options) and `projects.json` (project registry). The data directory is `%APPDATA%\OMR Utility` when frozen and `./data` when run from source.

## 4. Projects

`PROJ`, `IMG` and `SCAN_DIR` are module globals re-pointed by `_open_project(pid)`, which also clears the page cache, resets the auto-read batch and re-queues scans left in `reading`. Switching is refused while any job or the auto-reader is running. A project package (`.omrproject`) is a zip with `manifest.json` (`format: omr-project`, `version: 1`), a consistent database copy made with the SQLite backup API, and `images/*.jpg`, `scans/*.jpg`. Import accepts only those member names (zip-slip safe), validates that the database opens, and rolls back on failure. Settings and keys are never packaged. On first start after upgrading from the single-workspace version, existing data is moved into a project named *My first project*.

## 5. Scanner (`omr/scanner.py`)

Uses WIA automation through `pywin32` (`WIA.DeviceManager`): enumerate scanners (device type 1), connect, set intent / resolution / extents on the first item, `Transfer` as JPEG (BMP fallback converted to JPEG) and save. Each Flask request thread calls `CoInitialize`. Errors are mapped to readable messages (offline, busy, lid open). `scans` rows are inserted as soon as the image is saved.

**Auto-read** (`app.py: _autoread_*`): a single worker thread drains `scans` rows with status `scanned` into one running batch (`AUTO["batch_id"]`), reusing `_save_page`. It restarts itself if a page arrives while it finishes.

## 6. HTTP API (local, JSON)

| Area | Endpoints |
|---|---|
| Projects | `GET/POST /api/projects`, `POST /api/projects/<id>/open`, `PUT/DELETE /api/projects/<id>`, `GET /api/projects/<id>/package`, `POST /api/projects/<id>/save-to`, `POST /api/projects/import` |
| Batches and jobs | `GET/POST /api/batches`, `GET/PUT/DELETE /api/batches/<id>` (id `0` = all batches), `POST /api/batches/<id>/confirm`, `POST /api/batches/<id>/auto-meal`, `GET /api/jobs/<id>` |
| Rows | `PUT/DELETE /api/rows/<id>`, `POST /api/rows/<id>/insert-after`, `POST /api/rows/bulk`, `POST /api/pages/<id>/rows`, `GET /api/rows/<id>/crop` |
| Groups | `GET/POST /api/groups`, `GET/PUT/DELETE /api/groups/<id>`, `POST /api/groups/assign` |
| Scanner and log | `GET /api/scanner/devices`, `POST /api/scanner/scan`, `GET /api/scanner/pages`, `POST /api/scanner/process`, `GET /api/scans`, `POST /api/scans/<id>/discard \| requeue \| reread`, `GET/POST /api/autoread`, `POST /api/autoread/new-batch` |
| Settings | `GET/PUT /api/settings`, `POST /api/settings/test` |
| Export | `GET /api/export?scope=group:<id> \| all-groups \| batch:<id>&cols=…&fmt=csv\|xlsx&split=0\|1&meta=0\|1` |

The server binds to `127.0.0.1` only and has no authentication. Do not expose it.

## 7. Building

```
build.bat
```

runs PyInstaller (`--windowed`, bundles `static/`, and collects RapidOCR's models, pywebview, `anthropic`, `openpyxl`, `google.genai`, `google.auth`, with hidden imports for `cv2` and the pywin32 modules). Output: `dist\OMR Utility\`. The frozen app keeps data in `%APPDATA%\OMR Utility`.

## 8. Development tips

- Run `python app.py` and open <http://localhost:5000> for fast iteration on the UI. `index.html` is served from disk, so a browser refresh picks up edits. Deep links like `#review/3` and `#projects` open a tab directly.
- Quick UI syntax check: extract the `<script>` block and run `node --check` on it.
- The reader can be tried without the UI: `omr.pages.load_pages(...)` then `omr.table.scan_table(image, ai=None)` returns `(rows, straightened_image)`.
- Tuning constants live at the top of `omr/table.py` (`WARP_W`, `LINE_DIL`, `READER`) and in `omr/scan.py` (mark thresholds). They were tuned on a small set of phone photos; retune on scanner output when available.
- `make_sample.py` generates a synthetic guest list for smoke tests.

## 9. Known gaps and ideas

- Test suite (none yet); real-hardware tests for the scanner path; measured AI accuracy and cost.
- Per-row (rather than per-tile) AI calls only for low-confidence rows, or only for tiles containing marks, to cut cost.
- Use more of a high-dpi scan (currently everything is reduced to 1700 px wide).
- Tick recognition by *position* under printed D / L columns.
- Usage / cost counter for AI calls.
- Automatic project backups; a Mac/Linux build without the scanner feature.
- Mesh dewarping for badly curled pages.
