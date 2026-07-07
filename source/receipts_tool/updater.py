from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from . import __version__
from .paths import AppPaths


APP_EXE_NAME = "Payment Receipt Generator Tool.exe"
UPDATE_URL_ENV = "RECEIPTS_TOOL_UPDATE_URL"
UPDATE_MANIFEST_URL_ENV = "RECEIPTS_TOOL_UPDATE_MANIFEST_URL"
DEFAULT_UPDATE_URL = (
    "https://raw.githubusercontent.com/sepsster/"
    "Receipts-Tool/master/Payment%20Receipt%20Generator%20Tool.zip"
)
DEFAULT_UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/sepsster/Receipts-Tool/master/update.json"
MIN_EXE_BYTES = 1_000_000
MIN_ZIP_BYTES = 1_000_000
CONTENTS_DIR = "_app"


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
            manifest = download_update_manifest()
            latest_version = manifest["version"]
            update_url = manifest["download_url"]
            package_type = manifest["package_type"]
            if not is_newer_version(latest_version, __version__):
                self._set_status(
                    state="current",
                    message=f"This app is up to date at v{__version__}.",
                    update_available=False,
                    checking=False,
                    latest_version=latest_version,
                    source_url=update_url,
                )
                return

            updates_dir = self.paths.app_dir / "tmp" / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            suffix = "zip" if package_type == "zip" else "exe"
            downloaded_path = updates_dir / f"checked-{APP_EXE_NAME.removesuffix('.exe')}-{stamp}.{suffix}"
            download_executable(
                update_url,
                downloaded_path,
                expected_sha256=manifest.get("sha256"),
                expected_size=manifest.get("size"),
                expected_kind=package_type,
            )

            # For a single-exe package we can short-circuit if the bytes already
            # match the running exe. A zip can't be compared to the exe, so the
            # is_newer_version() gate above is what governs there.
            if package_type != "zip" and sha256_file(downloaded_path) == sha256_file(current_exe):
                downloaded_path.unlink(missing_ok=True)
                self._set_status(
                    state="current",
                    message=f"This app is up to date at v{__version__}.",
                    update_available=False,
                    checking=False,
                    latest_version=latest_version,
                    source_url=update_url,
                )
                return

            with self._lock:
                self._downloaded_path = downloaded_path
            self._set_status(
                state="available",
                message=f"Version {latest_version} is available on GitHub.",
                update_available=True,
                checking=False,
                latest_version=latest_version,
                source_url=update_url,
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
        latest_version: str | None = None,
        source_url: str | None = None,
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
        if latest_version is not None:
            status["latestVersion"] = latest_version
        if source_url is not None:
            status["sourceUrl"] = source_url
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


def prepare_self_update(paths: AppPaths, downloaded_file: Path | None = None) -> dict[str, object]:
    current_exe = current_executable()
    manifest = download_update_manifest()
    update_url = manifest["download_url"]
    latest_version = manifest["version"]
    package_type = manifest["package_type"]
    updates_dir = paths.app_dir / "tmp" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)

    if downloaded_file is None and not is_newer_version(latest_version, __version__):
        return {
            "alreadyCurrent": True,
            "restartRequired": False,
            "message": f"This app is already up to date at v{__version__}.",
            "sourceUrl": update_url,
            "latestVersion": latest_version,
        }

    if package_type == "zip":
        return _prepare_zip_self_update(paths, current_exe, manifest, downloaded_file)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    downloaded_exe = downloaded_file
    if downloaded_exe is None or not downloaded_exe.exists():
        downloaded_exe = updates_dir / f"{APP_EXE_NAME.removesuffix('.exe')}-{stamp}.exe"
        download_executable(
            update_url,
            downloaded_exe,
            expected_sha256=manifest.get("sha256"),
            expected_size=manifest.get("size"),
        )
    else:
        validate_downloaded_exe(
            downloaded_exe,
            expected_sha256=manifest.get("sha256"),
            expected_size=manifest.get("size"),
        )

    if sha256_file(downloaded_exe) == sha256_file(current_exe):
        downloaded_exe.unlink(missing_ok=True)
        return {
            "alreadyCurrent": True,
            "restartRequired": False,
            "message": f"This app is already up to date at v{__version__}.",
            "sourceUrl": update_url,
            "latestVersion": latest_version,
        }

    script_path = updates_dir / f"apply-update-{stamp}.ps1"
    backup_path = updates_dir / f"{current_exe.stem}-backup-{stamp}.exe"
    log_path = updates_dir / f"update-{stamp}.log"
    write_update_script(script_path)
    launch_update_script(script_path, downloaded_exe, current_exe, backup_path, log_path)
    return {
        "alreadyCurrent": False,
        "restartRequired": True,
        "message": f"Version {latest_version} downloaded. The app will close, install the update, and reopen.",
        "sourceUrl": update_url,
        "latestVersion": latest_version,
        "logPath": str(log_path),
    }


def _prepare_zip_self_update(
    paths: AppPaths,
    current_exe: Path,
    manifest: dict[str, object],
    downloaded_zip: Path | None,
) -> dict[str, object]:
    update_url = str(manifest["download_url"])
    latest_version = str(manifest["version"])
    entry_exe = str(manifest["entry_exe"])
    updates_dir = paths.app_dir / "tmp" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    if downloaded_zip is None or not downloaded_zip.exists():
        downloaded_zip = updates_dir / f"{APP_EXE_NAME.removesuffix('.exe')}-{stamp}.zip"
        download_executable(
            update_url,
            downloaded_zip,
            expected_sha256=manifest.get("sha256"),
            expected_size=manifest.get("size"),
            expected_kind="zip",
        )
    else:
        validate_downloaded_zip(
            downloaded_zip,
            expected_sha256=manifest.get("sha256"),
            expected_size=manifest.get("size"),
        )

    staging_dir = updates_dir / f"staging-{stamp}"
    staged_root = extract_update_zip(downloaded_zip, staging_dir, entry_exe)

    backup_dir = updates_dir / f"backup-{stamp}"
    log_path = updates_dir / f"update-{stamp}.log"
    script_path = updates_dir / f"apply-update-{stamp}.ps1"
    write_zip_update_script(script_path)
    launch_zip_update_script(
        script_path,
        staged_root=staged_root,
        install_dir=current_exe.parent,
        entry_exe=entry_exe,
        backup_dir=backup_dir,
        log_path=log_path,
        downloaded_zip=downloaded_zip,
        staging_dir=staging_dir,
    )
    return {
        "alreadyCurrent": False,
        "restartRequired": True,
        "message": f"Version {latest_version} downloaded. The app will close, install the update, and reopen.",
        "sourceUrl": update_url,
        "latestVersion": latest_version,
        "logPath": str(log_path),
    }


def extract_update_zip(zip_path: Path, staging_dir: Path, entry_exe: str) -> Path:
    """Extract the update zip and return the directory that holds ``entry_exe``."""
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(staging_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise UpdateError(f"Could not extract the downloaded update: {exc}") from exc

    if (staging_dir / entry_exe).exists():
        return staging_dir
    found = next((path for path in staging_dir.rglob(entry_exe)), None)
    if found is None:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise UpdateError("The downloaded update did not contain the application.")
    return found.parent


def get_update_url() -> str:
    return os.environ.get(UPDATE_URL_ENV, DEFAULT_UPDATE_URL).strip() or DEFAULT_UPDATE_URL


def get_update_manifest_url() -> str:
    return os.environ.get(UPDATE_MANIFEST_URL_ENV, DEFAULT_UPDATE_MANIFEST_URL).strip() or DEFAULT_UPDATE_MANIFEST_URL


def _base_update_status() -> dict[str, object]:
    supported = update_supported()
    return {
        "supported": supported,
        "available": supported,
        "updateAvailable": False,
        "checking": False,
        "state": "idle" if supported else "unavailable",
        "sourceUrl": get_update_url(),
        "manifestUrl": get_update_manifest_url(),
        "installedVersion": __version__,
        "latestVersion": "",
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


def download_update_manifest() -> dict[str, object]:
    request = urllib.request.Request(
        cache_busted_url(get_update_manifest_url()),
        headers={"User-Agent": "ReceiptsToolUpdater/1.0", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_manifest = json.loads(response.read(256 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404}:
            raise UpdateError(
                "GitHub would not allow the update manifest download. Make the repo public, then try again."
            ) from exc
        raise UpdateError(f"GitHub returned HTTP {exc.code} while checking the latest version.") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"Could not reach GitHub: {exc.reason}") from exc
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateError(f"Could not read the update manifest: {exc}") from exc

    if not isinstance(raw_manifest, dict):
        raise UpdateError("The update manifest was not valid.")

    version = str(raw_manifest.get("version", "")).strip()
    download_url = str(raw_manifest.get("downloadUrl", "")).strip() or get_update_url()
    sha256 = str(raw_manifest.get("sha256", "")).strip().lower()
    size = raw_manifest.get("size")
    package_type = str(raw_manifest.get("packageType", "exe")).strip().lower() or "exe"
    entry_exe = str(raw_manifest.get("entryExe", "")).strip() or APP_EXE_NAME
    if not version:
        raise UpdateError("The update manifest did not include a version.")
    if not download_url:
        raise UpdateError("The update manifest did not include a download URL.")
    if package_type not in {"exe", "zip"}:
        raise UpdateError(f"The update manifest used an unsupported package type: {package_type}")
    if size is not None:
        try:
            size = int(size)
        except (TypeError, ValueError) as exc:
            raise UpdateError("The update manifest had an invalid file size.") from exc

    return {
        "version": version,
        "download_url": download_url,
        "sha256": sha256 or None,
        "size": size,
        "package_type": package_type,
        "entry_exe": entry_exe,
    }


def is_newer_version(candidate: str, current: str) -> bool:
    candidate_parts = version_parts(candidate)
    current_parts = version_parts(current)
    length = max(len(candidate_parts), len(current_parts))
    candidate_parts += (0,) * (length - len(candidate_parts))
    current_parts += (0,) * (length - len(current_parts))
    return candidate_parts > current_parts


def version_parts(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in version.strip().split("."):
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        parts.append(int(digits or "0"))
    return tuple(parts or [0])


def cache_busted_url(url: str) -> str:
    split = urlsplit(url)
    query = split.query
    separator = "&" if query else ""
    query = f"{query}{separator}{urlencode({'_': str(int(time.time()))})}"
    return urlunsplit((split.scheme, split.netloc, split.path, query, split.fragment))


def download_executable(
    update_url: str,
    output_path: Path,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    expected_kind: str = "exe",
) -> None:
    request = urllib.request.Request(
        cache_busted_url(update_url),
        headers={"User-Agent": "ReceiptsToolUpdater/1.0", "Cache-Control": "no-cache"},
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

    if expected_kind == "zip":
        validate_downloaded_zip(output_path, expected_sha256=expected_sha256, expected_size=expected_size)
    else:
        validate_downloaded_exe(output_path, expected_sha256=expected_sha256, expected_size=expected_size)


def validate_downloaded_exe(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> None:
    actual_size = path.stat().st_size
    if actual_size < MIN_EXE_BYTES:
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded update looked incomplete.")
    if expected_size is not None and actual_size != expected_size:
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded update size did not match the latest version manifest.")
    with path.open("rb") as file:
        if file.read(2) != b"MZ":
            path.unlink(missing_ok=True)
            raise UpdateError("The downloaded update was not a Windows executable.")
    if expected_sha256 and sha256_file(path).lower() != expected_sha256.lower():
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded update did not match the latest version manifest.")


def validate_downloaded_zip(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> None:
    actual_size = path.stat().st_size
    if actual_size < MIN_ZIP_BYTES:
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded update looked incomplete.")
    if expected_size is not None and actual_size != expected_size:
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded update size did not match the latest version manifest.")
    with path.open("rb") as file:
        if file.read(2) != b"PK":
            path.unlink(missing_ok=True)
            raise UpdateError("The downloaded update was not a zip archive.")
    if expected_sha256 and sha256_file(path).lower() != expected_sha256.lower():
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded update did not match the latest version manifest.")


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
    [switch]$NoRestart,
    [switch]$SkipSelfTest
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

function Test-UpdateExecutable {
    param([string]$ExePath)

    if ($SkipSelfTest) {
        Write-UpdateLog "Self-test skipped."
        return $true
    }

    try {
        $process = Start-Process -FilePath $ExePath -ArgumentList "--self-test" -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -eq 0) {
            Write-UpdateLog "Self-test passed for '$ExePath'."
            return $true
        }
        Write-UpdateLog "Self-test failed for '$ExePath' with exit code $($process.ExitCode)."
    } catch {
        Write-UpdateLog "Self-test could not start for '$ExePath': $($_.Exception.Message)"
    }

    return $false
}

Write-UpdateLog "Starting update."
Start-Sleep -Seconds 2

Write-UpdateLog "Checking downloaded update."
if (-not (Test-UpdateExecutable -ExePath $NewExe)) {
    Write-UpdateLog "Downloaded update failed. Restarting existing app without changing files."
    if (-not $NoRestart) {
        Start-Process -FilePath $Target
    }
    exit 1
}

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

Write-UpdateLog "Checking installed update."
if (-not (Test-UpdateExecutable -ExePath $Target)) {
    Write-UpdateLog "Installed update failed. Restoring backup."
    if (Test-Path -LiteralPath $Backup) {
        Copy-Item -LiteralPath $Backup -Destination $Target -Force
        Write-UpdateLog "Backup restored."
    }
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


def write_zip_update_script(path: Path) -> None:
    path.write_text(
        """param(
    [Parameter(Mandatory=$true)][string]$StagedDir,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$EntryExe,
    [Parameter(Mandatory=$true)][string]$Backup,
    [Parameter(Mandatory=$true)][string]$Log,
    [string]$Staging = "",
    [string]$DownloadedZip = "",
    [switch]$NoRestart,
    [switch]$SkipSelfTest
)

$ErrorActionPreference = "Stop"
$logDir = Split-Path -Parent $Log
if ($logDir) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

$targetExe = Join-Path $InstallDir $EntryExe
$targetApp = Join-Path $InstallDir "_app"
$stagedExe = Join-Path $StagedDir $EntryExe
$stagedApp = Join-Path $StagedDir "_app"

function Write-UpdateLog {
    param([string]$Message)
    Add-Content -LiteralPath $Log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
}

function Test-UpdateExecutable {
    param([string]$ExePath)

    if ($SkipSelfTest) {
        Write-UpdateLog "Self-test skipped."
        return $true
    }

    try {
        $process = Start-Process -FilePath $ExePath -ArgumentList "--self-test" -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -eq 0) {
            Write-UpdateLog "Self-test passed for '$ExePath'."
            return $true
        }
        Write-UpdateLog "Self-test failed for '$ExePath' with exit code $($process.ExitCode)."
    } catch {
        Write-UpdateLog "Self-test could not start for '$ExePath': $($_.Exception.Message)"
    }

    return $false
}

function Restore-Backup {
    $backupExe = Join-Path $Backup $EntryExe
    $backupApp = Join-Path $Backup "_app"
    try {
        if (Test-Path -LiteralPath $backupApp) {
            if (Test-Path -LiteralPath $targetApp) { Remove-Item -LiteralPath $targetApp -Recurse -Force }
            Copy-Item -LiteralPath $backupApp -Destination $targetApp -Recurse -Force
        }
        if (Test-Path -LiteralPath $backupExe) {
            Copy-Item -LiteralPath $backupExe -Destination $targetExe -Force
        }
        Write-UpdateLog "Backup restored."
    } catch {
        Write-UpdateLog "Restore failed: $($_.Exception.Message)"
    }
}

Write-UpdateLog "Starting update."
Start-Sleep -Seconds 2

Write-UpdateLog "Checking downloaded update."
if (-not (Test-UpdateExecutable -ExePath $stagedExe)) {
    Write-UpdateLog "Downloaded update failed. Restarting existing app without changing files."
    if (-not $NoRestart) { Start-Process -FilePath $targetExe }
    exit 1
}

try {
    New-Item -ItemType Directory -Force -Path $Backup | Out-Null
    if (Test-Path -LiteralPath $targetExe) {
        Copy-Item -LiteralPath $targetExe -Destination (Join-Path $Backup $EntryExe) -Force
    }
    if (Test-Path -LiteralPath $targetApp) {
        Copy-Item -LiteralPath $targetApp -Destination (Join-Path $Backup "_app") -Recurse -Force
    }
    Write-UpdateLog "Backed up existing application."
} catch {
    Write-UpdateLog "Backup failed: $($_.Exception.Message)"
}

$installed = $false
for ($attempt = 1; $attempt -le 90; $attempt++) {
    try {
        if (Test-Path -LiteralPath $targetApp) { Remove-Item -LiteralPath $targetApp -Recurse -Force }
        Copy-Item -LiteralPath $stagedApp -Destination $targetApp -Recurse -Force
        Copy-Item -LiteralPath $stagedExe -Destination $targetExe -Force
        $installed = $true
        Write-UpdateLog "Update installed on attempt $attempt."
        break
    } catch {
        Write-UpdateLog "Attempt $attempt failed: $($_.Exception.Message)"
        Start-Sleep -Seconds 1
    }
}

if (-not $installed) {
    Write-UpdateLog "Update failed to install. Restoring backup."
    Restore-Backup
    if (-not $NoRestart) { Start-Process -FilePath $targetExe }
    exit 1
}

Write-UpdateLog "Checking installed update."
if (-not (Test-UpdateExecutable -ExePath $targetExe)) {
    Write-UpdateLog "Installed update failed. Restoring backup."
    Restore-Backup
    if (-not $NoRestart) { Start-Process -FilePath $targetExe }
    exit 1
}

Write-UpdateLog "Update complete. Restarting app."
if (-not $NoRestart) { Start-Process -FilePath $targetExe }
if ($Staging -and (Test-Path -LiteralPath $Staging)) {
    Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
}
if ($DownloadedZip -and (Test-Path -LiteralPath $DownloadedZip)) {
    Remove-Item -LiteralPath $DownloadedZip -Force -ErrorAction SilentlyContinue
}
exit 0
""",
        encoding="utf-8",
        newline="\r\n",
    )


def launch_zip_update_script(
    script_path: Path,
    *,
    staged_root: Path,
    install_dir: Path,
    entry_exe: str,
    backup_dir: Path,
    log_path: Path,
    downloaded_zip: Path,
    staging_dir: Path,
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
                "-StagedDir",
                str(staged_root),
                "-InstallDir",
                str(install_dir),
                "-EntryExe",
                entry_exe,
                "-Backup",
                str(backup_dir),
                "-Log",
                str(log_path),
                "-Staging",
                str(staging_dir),
                "-DownloadedZip",
                str(downloaded_zip),
            ],
            cwd=str(install_dir),
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise UpdateError(f"Could not start the updater helper: {exc}") from exc
