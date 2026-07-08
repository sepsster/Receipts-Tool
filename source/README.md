# Receipts Tool Source

Source code and build files for the portable payment receipt generator.

## Run During Development

From the repository root:

```powershell
$env:PYTHONPATH = ".\source"
& ".\.venv\Scripts\python.exe" -m receipts_tool.web_app
```

This starts a local-only server and opens the app interface in your default browser. Nothing is uploaded anywhere.

The app creates these folders beside the source tree during development, or beside the executable in portable mode:

- `data/receipts.sqlite`
- `receipts/{year}/...pdf`
- `backups/receipts-{timestamp}.sqlite`
- `assets/logo.png`

Generated receipt PDFs use PDF owner-password permissions so they open normally but standard PDF editors cannot change or annotate them.

Users can upload a custom receipt logo from `Settings`. On first run, the app copies its bundled stock logo to `assets/logo.png` beside the executable if no logo is already there. App updates replace only the executable, so they preserve whatever logo is in that file.

## Build The Portable App

Install PyInstaller into a local virtual environment, then run this from the repository root:

```powershell
& ".\.venv\Scripts\python.exe" ".\source\scripts\build_exe.py"
```

One build refreshes everything so the artifacts never drift from the source:

- `Payment Receipt Generator Tool\` — the onedir build output (staging, gitignored)
- `Payment Receipt Generator Tool.zip` — the release package the updater downloads
- `update.json` — release manifest (version, sha256, size)
- `Local App\Payment Receipt Generator Tool.exe` — your local runnable copy (gitignored)

## Run The Local Exe

Double-click `Local App\Payment Receipt Generator Tool.exe`. This copy is completely separate from the release zip: its `data\`, `receipts\`, and `backups\` live inside `Local App\` and survive rebuilds (only the exe and `_app\` are swapped, the same way the updater works).

To rebuild only the local exe without touching the release zip or `update.json`:

```powershell
& ".\.venv\Scripts\python.exe" ".\source\scripts\build_exe.py" --local
```

## App Updates

The built Windows app checks GitHub for updates in the background each time it opens. If the top-level `Payment Receipt Generator Tool.exe` on GitHub differs from the running executable, the app marks the `Settings` tab and shows an in-app notification. The `Settings` -> `Update From GitHub` button downloads the latest executable, closes the app, replaces the running executable, and reopens it.

By default, the updater downloads from the `master` branch of `sepsster/Receipts-Tool`. That file must be publicly reachable for an end user without GitHub credentials. To point a build at another download URL, set `RECEIPTS_TOOL_UPDATE_URL` before launching the app.

## Smoke Test

```powershell
& ".\.venv\Scripts\python.exe" ".\source\scripts\smoke_test.py"
```
