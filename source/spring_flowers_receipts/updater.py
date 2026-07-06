from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .paths import AppPaths


APP_EXE_NAME = "Payment Receipt Generator Tool.exe"
UPDATE_URL_ENV = "SPRING_FLOWERS_UPDATE_URL"
DEFAULT_UPDATE_URL = (
    "https://raw.githubusercontent.com/sepsster/"
    "Spring-Flowers-Childcare-Receipts/master/Payment%20Receipt%20Generator%20Tool.exe"
)
MIN_EXE_BYTES = 1_000_000


class UpdateError(RuntimeError):
    pass


def get_update_info() -> dict[str, object]:
    available = sys.platform.startswith("win") and getattr(sys, "frozen", False)
    if available:
        message = "Ready to download the latest app from GitHub."
    else:
        message = "Updates are available after the app is built as the Windows executable."

    return {
        "available": available,
        "sourceUrl": get_update_url(),
        "message": message,
    }


def prepare_self_update(paths: AppPaths) -> dict[str, object]:
    current_exe = current_executable()
    update_url = get_update_url()
    updates_dir = paths.app_dir / "tmp" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    downloaded_exe = updates_dir / f"{APP_EXE_NAME.removesuffix('.exe')}-{stamp}.exe"
    download_executable(update_url, downloaded_exe)

    if sha256_file(downloaded_exe) == sha256_file(current_exe):
        downloaded_exe.unlink(missing_ok=True)
        return {
            "alreadyCurrent": True,
            "restartRequired": False,
            "message": "This app is already up to date.",
            "sourceUrl": update_url,
        }

    script_path = updates_dir / f"apply-update-{stamp}.cmd"
    backup_path = updates_dir / f"{current_exe.stem}-backup-{stamp}.exe"
    log_path = updates_dir / f"update-{stamp}.log"
    write_update_script(script_path)
    launch_update_script(script_path, downloaded_exe, current_exe, backup_path, log_path)
    return {
        "alreadyCurrent": False,
        "restartRequired": True,
        "message": "Update downloaded. The app will close, install the update, and reopen.",
        "sourceUrl": update_url,
        "logPath": str(log_path),
    }


def get_update_url() -> str:
    return os.environ.get(UPDATE_URL_ENV, DEFAULT_UPDATE_URL).strip() or DEFAULT_UPDATE_URL


def current_executable() -> Path:
    if not sys.platform.startswith("win"):
        raise UpdateError("Updates are only supported in the Windows app.")
    if not getattr(sys, "frozen", False):
        raise UpdateError("Updates are only available after running the built Windows executable.")
    exe_path = Path(sys.executable).resolve()
    if not exe_path.exists():
        raise UpdateError("The current app executable could not be found.")
    return exe_path


def download_executable(update_url: str, output_path: Path) -> None:
    request = urllib.request.Request(
        update_url,
        headers={"User-Agent": "SpringFlowersReceiptsUpdater/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            with output_path.open("wb") as destination:
                shutil.copyfileobj(response, destination)
    except urllib.error.HTTPError as exc:
        output_path.unlink(missing_ok=True)
        if exc.code in {401, 403, 404}:
            raise UpdateError(
                "GitHub would not allow the update download. Make the repo or release asset public, then try again."
            ) from exc
        raise UpdateError(f"GitHub returned HTTP {exc.code} while downloading the update.") from exc
    except urllib.error.URLError as exc:
        output_path.unlink(missing_ok=True)
        raise UpdateError(f"Could not reach GitHub: {exc.reason}") from exc
    except OSError as exc:
        output_path.unlink(missing_ok=True)
        raise UpdateError(f"Could not save the downloaded update: {exc}") from exc

    validate_downloaded_exe(output_path)


def validate_downloaded_exe(path: Path) -> None:
    if path.stat().st_size < MIN_EXE_BYTES:
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded update looked incomplete.")
    with path.open("rb") as file:
        if file.read(2) != b"MZ":
            path.unlink(missing_ok=True)
            raise UpdateError("The downloaded update was not a Windows executable.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_update_script(path: Path) -> None:
    path.write_text(
        """@echo off
setlocal
set "NEW_EXE=%~1"
set "TARGET=%~2"
set "BACKUP=%~3"
set "LOG=%~4"

echo Starting update at %date% %time% > "%LOG%"
timeout /t 2 /nobreak >nul

if exist "%TARGET%" (
  copy /Y "%TARGET%" "%BACKUP%" >> "%LOG%" 2>>&1
)

for /L %%I in (1,1,90) do (
  copy /Y "%NEW_EXE%" "%TARGET%" >> "%LOG%" 2>>&1
  if not errorlevel 1 goto updated
  timeout /t 1 /nobreak >nul
)

echo Update failed. Restarting the existing app. >> "%LOG%"
start "" "%TARGET%"
exit /b 1

:updated
echo Update complete. Restarting app. >> "%LOG%"
start "" "%TARGET%"
del "%NEW_EXE%" >nul 2>nul
exit /b 0
""",
        encoding="utf-8",
        newline="\r\n",
    )


def launch_update_script(
    script_path: Path,
    downloaded_exe: Path,
    current_exe: Path,
    backup_path: Path,
    log_path: Path,
) -> None:
    subprocess.Popen(
        [
            "cmd.exe",
            "/c",
            "start",
            "",
            "/min",
            str(script_path),
            str(downloaded_exe),
            str(current_exe),
            str(backup_path),
            str(log_path),
        ],
        cwd=str(current_exe.parent),
        close_fds=True,
    )
