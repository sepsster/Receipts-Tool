# Spring Flowers Local Receipt Generator Source

Source code and build files for the portable Spring Flowers Childcare payment receipt generator.

## Run During Development

From the repository root:

```powershell
$env:PYTHONPATH = ".\source"
& ".\.venv\Scripts\python.exe" -m spring_flowers_receipts.web_app
```

This starts a local-only server and opens the app interface in your default browser. Nothing is uploaded anywhere.

The app creates these folders beside the source tree during development, or beside the executable in portable mode:

- `data/receipts.sqlite`
- `receipts/{year}/...pdf`
- `backups/receipts-{timestamp}.sqlite`
- `assets/logo.png`

## Build The Portable App

Install PyInstaller into a local virtual environment, then run this from the repository root:

```powershell
& ".\.venv\Scripts\python.exe" ".\source\scripts\build_exe.py"
```

The finished executable will be in the repository root:

```text
Payment Receipt Generator Tool.exe
```

Move `Payment Receipt Generator Tool.exe` anywhere you want. Data and generated receipts stay beside the executable.

## App Updates

The built Windows app has a `Settings` -> `Update From GitHub` button. It downloads the latest top-level `Payment Receipt Generator Tool.exe` from the configured GitHub URL, closes the app, replaces the running executable, and reopens it.

By default, the updater downloads from the `master` branch of `sepsster/Spring-Flowers-Childcare-Receipts`. That file must be publicly reachable for an end user without GitHub credentials. To point a build at another download URL, set `SPRING_FLOWERS_UPDATE_URL` before launching the app.

## Smoke Test

```powershell
& ".\.venv\Scripts\python.exe" ".\source\scripts\smoke_test.py"
```
