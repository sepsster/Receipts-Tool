from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .paths import AppPaths


APP_EXE_NAME = "Payment Receipt Generator Tool.exe"
UPDATE_URL_ENV = "RECEIPTS_TOOL_UPDATE_URL"
DEFAULT_UPDATE_URL = (
    "https://raw.githubusercontent.com/sepsster/"
    "Receipts-Tool/master/Payment%20Receipt%20Generator%20Tool.exe"
)
MIN_EXE_BYTES = 1_000_000


class UpdateError(RuntimeError):
    pass


class UpdateCheckState:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()
        self._downloaded_path: Path | None = None
        self._status = _base_update_status()

    def start(self) -> None:
        self.check_now()

    def check_now(self) -> dict[str, object]:
        if not update_supported():
            self._set_status(
                state="unavailable",
                message="Updates are available after the app is built as the Windows executable.",
                update_available=False,
                checking=False,
            )
            return self.snapshot()

        with self._lock:
            if self._status.get("checking"):
                return self._status.copy()
            old_downloaded_path = self._downloaded_path
            self._downloaded_path = None
            status = _base_update_status()
            status.update(
                {
                    "state": "checking",
                    "message": "Checking GitHub for updates...",
                    "updateAvailable": False,
                    "checking": True,
                    "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            self._status = status

        if old_downloaded_path:
            old_downloaded_path.unlink(missing_ok=True)

        threading.Thread(target=self._check_for_update, daemon=True).start()
        return status.copy()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._status.copy()

    def take_downloaded_update(self) -> Path | None:
        with self._lock:
            path = self._downloaded_path
            self._downloaded_path = None
        if path and path.exists():
            return path
        return None

    def _check_for_update(self) -> None:
        downloaded_exe: Path | None = None
        try:
            current_exe = current_executable()
            updates_dir = self.paths.app_dir / "tmp" / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            downloaded_exe = updates_dir / f"checked-{APP_EXE_NAME.removesuffix('.exe')}-{stamp}.exe"
            download_executable(get_update_url(), downloaded_exe)

            if sha256_file(downloaded_exe) == sha256_file(current_exe):
                downloaded_exe.unlink(missing_ok=True)
                self._set_status(
                    state="current",
                    message="This app is up to date.",
                    update_available=False,
                    checking=False,
                )
                return

            with self._lock:
                self._downloaded_path = downloaded_exe
            self._set_status(
                state="available",
                message="A newer version is available on GitHub.",
                update_available=True,
                checking=False,
            )
        except UpdateError as exc:
            if downloaded_exe:
                downloaded_exe.unlink(missing_ok=True)
            self._set_status(
                state="error",
                message=f"Could not check for updates: {exc}",
                update_available=False,
                checking=False,
            )
        except Exception as exc:
            if downloaded_exe:
                downloaded_exe.unlink(missing_ok=True)
            self._set_status(
                state="error",
                message=f"Could not check for updates: {exc}",
                update_available=False,
                checking=False,
            )

    def _set_status(
        self,
        *,
        state: str,
        message: str,
        update_available: bool,
        checking: bool,
    ) -> None:
        status = _base_update_status()
        status.update(
            {
                "state": state,
                "message": message,
                "updateAvailable": update_available,
                "checking": checking,
                "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        with self._lock:
            self._status = status


def update_supported() -> bool:
    return sys.platform.startswith("win") and getattr(sys, "frozen", False)


def get_update_info() -> dict[str, object]:
    status = _base_update_status()
    if status["supported"]:
        message = "Ready to download the latest app from GitHub."
    else:
        message = "Updates are available after the app is built as the Windows executable."

    status["message"] = message
    return status


def prepare_self_update(paths: AppPaths, downloaded_exe: Path | None = None) -> dict[str, object]:
    current_exe = current_executable()
    update_url = get_update_url()
    updates_dir = paths.app_dir / "tmp" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if downloaded_exe is None or not downloaded_exe.exists():
        downloaded_exe = updates_dir / f"{APP_EXE_NAME.removesuffix('.exe')}-{stamp}.exe"
        download_executable(update_url, downloaded_exe)
    else:
        validate_downloaded_exe(downloaded_exe)

    if sha256_file(downloaded_exe) == sha256_file(current_exe):
        downloaded_exe.unlink(missing_ok=True)
        return {
            "alreadyCurrent": True,
            "restartRequired": False,
            "message": "This app is already up to date.",
            "sourceUrl": update_url,
        }

    script_path = updates_dir / f"apply-update-{stamp}.ps1"
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


def _base_update_status() -> dict[str, object]:
    supported = update_supported()
    return {
        "supported": supported,
        "available": supported,
        "updateAvailable": False,
        "checking": False,
        "state": "idle" if supported else "unavailable",
        "sourceUrl": get_update_url(),
        "message": "",
        "checkedAt": "",
    }


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
        headers={"User-Agent": "ReceiptsToolUpdater/1.0"},
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
        """param(
    [Parameter(Mandatory=$true)][string]$NewExe,
    [Parameter(Mandatory=$true)][string]$Target,
    [Parameter(Mandatory=$true)][string]$Backup,
    [Parameter(Mandatory=$true)][string]$Log,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$logDir = Split-Path -Parent $Log
if ($logDir) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

function Write-UpdateLog {
    param([string]$Message)
    Add-Content -LiteralPath $Log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
}

Write-UpdateLog "Starting update."
Start-Sleep -Seconds 2

try {
    if (Test-Path -LiteralPath $Target) {
        Copy-Item -LiteralPath $Target -Destination $Backup -Force
        Write-UpdateLog "Backed up existing executable."
    }
} catch {
    Write-UpdateLog "Backup failed: $($_.Exception.Message)"
}

$updated = $false
for ($attempt = 1; $attempt -le 90; $attempt++) {
    try {
        Copy-Item -LiteralPath $NewExe -Destination $Target -Force
        $updated = $true
        Write-UpdateLog "Update copied on attempt $attempt."
        break
    } catch {
        Write-UpdateLog "Attempt $attempt failed: $($_.Exception.Message)"
        Start-Sleep -Seconds 1
    }
}

if (-not $updated) {
    Write-UpdateLog "Update failed. Restarting existing app."
    if (-not $NoRestart) {
        Start-Process -FilePath $Target
    }
    exit 1
}

Write-UpdateLog "Update complete. Restarting app."
if (-not $NoRestart) {
    Start-Process -FilePath $Target
}
Remove-Item -LiteralPath $NewExe -Force -ErrorAction SilentlyContinue
exit 0
""",
        encoding="utf-8",
        newline="\r\n",
    )


def powershell_executable() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.exists():
        return str(candidate)
    return "powershell.exe"


def launch_update_script(
    script_path: Path,
    downloaded_exe: Path,
    current_exe: Path,
    backup_path: Path,
    log_path: Path,
) -> subprocess.Popen:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
    try:
        return subprocess.Popen(
            [
                powershell_executable(),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-NewExe",
                str(downloaded_exe),
                "-Target",
                str(current_exe),
                "-Backup",
                str(backup_path),
                "-Log",
                str(log_path),
            ],
            cwd=str(current_exe.parent),
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise UpdateError(f"Could not start the updater helper: {exc}") from exc
