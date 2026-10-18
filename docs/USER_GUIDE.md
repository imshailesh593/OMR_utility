# OMR Utility: User Guide

This guide walks through using the app from start to finish. For an overview see the [README](../README.md).

## 1. Before you start

- Windows 10 or 11 with the app installed (see the README for building it).
- Decide how you will capture pages:
  - **Flatbed scanner** (best quality): switch it on and connect USB.
  - **PDFs or photos**: have the files ready on the PC. Avoid pictures sent through WhatsApp (heavily compressed). Phones' built-in document scanners (iPhone: Notes or Files → *Scan Documents*) work much better than the plain camera.
- Scan or photograph pages **flat, upright, evenly lit and in colour**. Blue-pen marks are only detected in colour.

## 2. Projects

A **project** is one job, for example "Dinner list October". Its scanned pages, readings, edits and groups are kept apart from other projects.

1. Open the **Projects** tab.
2. Type a name and click **Create and open**.
3. The open project's name shows in the top-right corner. Click it to return to this tab.

Other actions: **Open** (switch), rename (edit the name box), **Save to file…** (backup), **Delete** (click twice to confirm; the open project can't be deleted) and **Open a saved project file…** (restore).

You cannot switch projects while pages are being read.

## 3. Choose how pages are read

On the **Scan** tab, *Reading options*:

- **What to keep**
  - *Keep every row (full scan)* reads and stores everything.
  - *Dinner / lunch sheets* keeps **only** rows with a pen **D**, **L** or tick. See section 9.
- **Engine and provider**
  - *Local OCR*: free, offline, slower, less accurate on names.
  - *AI - cloud vision*: choose **Claude** or **Gemini**, pick the model, paste your API key and click **Save & test**. "Connected" means it works.
- AI mode sends page images to the provider. See *Privacy and cost* in the README.

The choice is remembered and applies to all pages you read afterwards.

## 4. Getting pages in

### A. From the scanner

In *Scan from your scanner*:

1. Click **Refresh**. Your scanner should appear in the first box. If it says *No scanner found*, see Troubleshooting below.
2. Choose resolution (300 dpi is recommended; 600 is slow and doesn't help reading yet), **Colour**, and paper size.
3. Put the page **face-down on the glass**, close the lid, click **Scan a page**. Wait for "Done".
4. Repeat for every page. A thumbnail appears for each page waiting. Click one to enlarge; **remove** takes a bad scan out of the waiting list (it stays in the log as *Discarded*).
5. Click **Read N scanned pages** to read them all into one batch.

**Auto-read (recommended for many pages):** tick *Read each page automatically as it is scanned*. Pages are then read in the background while you keep scanning, all into one batch. The line beside it shows how many are waiting or being read. **Start a new batch** makes the next scanned page begin a new batch.

### B. From PDFs or images

1. In *Open files (PDF or images)* click **Choose Files** and select all the files (Ctrl or Shift-click for several; multi-page PDFs are fine).
2. Optionally type a batch name.
3. Click **Scan**. A progress bar shows *Reading page X of Y*.

When reading finishes the app opens **Review & edit**.

## 5. The Scanned pages log

The **Scanned pages** tab lists every page scanned from the scanner:

| Status | Meaning |
|---|---|
| Scanned | Captured and waiting to be read |
| Reading | Being read now |
| Scraped | Read; the batch it went into is linked, with the number of rows found |
| Failed | Reading failed; the reason is in *Notes* |
| Discarded | You removed it from the waiting queue |

Buttons: **Read the N waiting pages now**, **Read again** (re-read a scraped page, for example with AI, into a new batch), **Discard**, **Try again** (failed pages) and **Restore** (discarded pages). Click a preview to see the full page.

Pages opened from files are not listed here; they appear as batches on the Scan tab.

## 6. Review and correct

Open **Review & edit**. The heading dropdown switches between a single batch and **All batches together**.

Each row shows a strip of the original page, then the fields:

| Column | Contains |
|---|---|
| First name / Middle name / Last name | Read from the three name columns of the sheet |
| Contact (as read) | The contact cell exactly as read |
| Phones (cleaned) | Distinct numbers, `+91` form for Indian mobiles, repeats removed |
| Meal (D/L) | `D`, `L`, `✓` (tick) or `?` (unrecognised mark) |
| Other text | Extra text (for village lists, the entry number and panel) |
| Full scanned row | All cells joined, as a safety net |

### Colours and flags
- **Yellow** cell: low confidence. Check it.
- **Red outline and "dup"**: the same phone number appears on another row.
- **Green** row: confirmed. **Grey** row: looks like a heading (*Keep as data* if it is real).

### Editing
- Click any cell, type, and press Enter or click away. It saves immediately, even on confirmed or grouped rows.
- **Undo** reverts your last 100 edits (button at the top).
- **+** inserts an empty row below this one (for a row the reader missed). **x** deletes a row.
- Click the strip image to enlarge it; **view page** opens the whole page.

### Finding things
- **Search** matches any text in any column.
- **Filter**: All, Pending, Confirmed, Low confidence, No phone number, Duplicate phone, No meal mark, Has meal mark, Not in a group.
- The grid shows 100 rows per screen. Use **Prev / Next** and **Select all N matching**.

### Confirming
- **Confirm** one row, or **Confirm all high-confidence** (rows with no yellow cells), or **Confirm everything**.
- Only confirmed rows can be added to groups or exported.

### Bulk actions
Tick rows (or **Select all N matching**), then: **Confirm**, **Reopen**, **Set meal…** (D, L, check mark, clear), **Delete** (click twice), or add to a group.

## 7. Groups and export

### Making groups
- In Review, tick confirmed rows, choose an existing group or type a new name, and click **Add to group**.
- Or click **Auto-group Dinner (D) / Lunch (L)**: confirmed rows marked `D` go to *Dinner*, `L` to *Lunch*, and `✓` to *Ticked (meal not specified)*. Set ticked rows to D or L with **Set meal…** and auto-group again.
- A person can be in **one** group at a time. Adding them to another group moves them.

### Managing groups
On **Groups & export**: select a group to see and edit its members, **Remove** a person, rename the group (edit its name), or delete it (the people stay in the batch).

### Exporting
1. Tick the columns to include (and optionally *source file + page*).
2. Choose **CSV** or **Excel (.xlsx)**.
3. With *All groups* selected you can tick **One file/sheet per group** (a ZIP of CSVs, or an Excel sheet per group).
4. Click the export button. In the desktop app a Save As window opens.

Only confirmed rows that are in a group are exported. **Export this batch** (in Review) exports every confirmed row of a batch, grouped or not.

## 8. Backups and moving to another computer

Everything is saved as you work, but only on this computer. To make a backup:

1. **Projects** tab → **Save to file…** → choose where to save the `.omrproject` file.
2. Copy it to a USB drive or cloud folder.

To restore: **Projects** → **Open a saved project file…**, pick the file, then click **Open** on the new project. API keys are not in the file, so re-enter them on the other computer.

## 9. Dinner / lunch mode in detail

Use it when you only need people who were marked in pen.

1. Set *What to keep* to **Dinner / lunch sheets**.
2. Scan or open pages **in colour**.
3. Each page is read in full, then only rows with a D, L or tick are kept. Pages with none finish with 0 rows and a note.
4. In Review, check each kept row. A `?` means a mark was found but not recognised (a cross, circle, or untidy letter): set it to D or L by hand, or delete the row.
5. Confirm, then **Auto-group**, then export the *Dinner* and *Lunch* groups.

To go back to normal, set *What to keep* to *Keep every row*. Batches created in dinner/lunch mode are tagged "marked rows only" in the batch list.

## 10. A recommended workflow for a very large job (for example 180 pages)

1. **Projects** → create one project for the job.
2. Try the whole workflow on **3 to 5 pages first**: scan, read, review, group, export. Compare local OCR and AI mode on the same pages and check the cost.
3. Tick **auto-read** and scan all pages. Glance at the *Scanned pages* tab occasionally for *Failed* pages.
4. When scanning ends, open **Review & edit → All batches together**.
5. Work through the filters: *Low confidence*, *No phone number*, *Duplicate phone*, then **Confirm all high-confidence**.
6. Group, export, and **Save to file…** as a backup.

For about 180 pages with a flatbed expect roughly 90 minutes of scanning (about 30 seconds a side) and about the same time of reading, mostly overlapping.

## 11. Troubleshooting

**No scanner found / scanner busy.** Switch the scanner on, check the cable, close Canon IJ Scan Utility and any other scanning software, click *Refresh*. If Windows still doesn't list it (Settings → Bluetooth & devices → Printers & scanners), reinstall the Canon driver. As a fallback, scan with the Canon software to JPEG or PDF and use *Open files*.

**"The API key was rejected."** Paste the key again and click *Save & test*. Gemini keys also need the *Generative Language API* enabled.

**A page is Failed.** Open the *Scanned pages* tab, read the note, and click *Try again*.

**Rows are missing, merged or shifted.** Rescan the page flat and straight in colour at 300 dpi. Heavily creased or low-resolution pages read worse. Try AI mode for difficult pages.

**Wrong names.** The local reader is weak on names. Correct them in Review or try AI mode.

**The app closed while reading.** Reopen it. Pages already read are kept; the rest wait in the queue. Click *Read the waiting pages*.

**I can't switch projects.** Pages are being read. Wait for it to finish.

**Where is my data?** `%APPDATA%\OMR Utility\` (paste that into the Windows Explorer address bar).
