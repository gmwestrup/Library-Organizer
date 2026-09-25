# Windows edition

Runs the exact same application as the Docker edition, locally on your
PC. `LibraryOrganizer.pyw` starts the web server on `127.0.0.1:8765`
(nothing exposed to your network) and opens it in your browser.

## Setup

1. Install Python 3.9+ from [python.org](https://www.python.org/downloads/)
   — tick **"Add Python to PATH"** during install.
2. Double-click `install-requirements.bat` (installs the packages in
   `requirements.txt`).
3. Optional: `py -m pip install faster-whisper` to let it listen to
   audiobook intros, and [ffmpeg](https://ffmpeg.org) on your PATH for the
   most accurate audio measurements.
4. Double-click `LibraryOrganizer.pyw`.

A small status window appears and your browser opens automatically.
Close the status window to stop the server.

## Using it

- **Browse...** next to Source picks your messy library — any drive,
  including mapped network shares (`Z:\Audiobooks`) or UNC paths
  (`\\NAS\share\...`).
- **Browse...** next to Destination picks an output folder (must not be
  inside the source).
- Press **Autopilot** (scan, enrich, safe author merges, AI for uncertain
  books if configured, duplicates) — or run **1 Scan → 2 Enrich → 3 AI
  identify → 4 Clean up** yourself — review anything marked low confidence,
  then **5 Copy**.
- Your remembered decisions, covers and settings live in
  `%LOCALAPPDATA%\LibraryOrganizer`. With **Microsoft Store Python** that
  folder is redirected to
  `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.xx_...\LocalCache\Local\LibraryOrganizer`.
  To choose your own location, set a `DATA_DIR` environment variable.

Every feature documented in [`../docs/CHANGELOG.md`](../docs/CHANGELOG.md)
and referenced from the main [`../README.md`](../README.md) works
identically here — output-structure choices, duplicate policies, skip,
bulk edit, filters, Stop, metadata sync, the works.

## Network libraries

Pointing Source at a mapped drive or UNC path organizes a NAS library
from Windows, but copying then travels over the network twice (read +
write). For a large library already living on a NAS, the Docker edition
running *on* the NAS is faster — the output is identical either way.

## Files in this folder

| File | Purpose |
|---|---|
| `LibraryOrganizer.pyw` | double-click to run |
| `install-requirements.bat` | one-time package install |
| `requirements.txt` | the packages it needs |
| `core.py`, `app.py`, `enrich.py`, `ai.py`, `dedupe.py`, `authors.py`, `library_db.py`, `templates/` | the application (identical to the Docker edition's files) |
