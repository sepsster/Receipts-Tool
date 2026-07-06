# Spring Flowers Local Receipt Generator

A portable Windows desktop app for creating standardized Spring Flowers Childcare payment receipt PDFs from saved parent/child profiles.

## Run During Development

```powershell
& "C:\Users\sepsa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m spring_flowers_receipts.web_app
```

This starts a local-only server and opens the app interface in your default browser. Nothing is uploaded anywhere.

The app creates these folders beside the project or executable:

- `data/receipts.sqlite`
- `receipts/{year}/...pdf`
- `backups/receipts-{timestamp}.sqlite`
- `assets/logo.png`

## Build The Portable App

Install PyInstaller into a local virtual environment, then run:

```powershell
& ".\.venv\Scripts\python.exe" scripts\build_exe.py
```

The finished executable will be in:

```text
SpringFlowersReceipts.exe
```

Move `SpringFlowersReceipts.exe` anywhere you want. Data and generated receipts stay beside the executable.

## Smoke Test

```powershell
& "C:\Users\sepsa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\smoke_test.py
```
