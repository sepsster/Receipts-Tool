from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SOURCE_ROOT.parent
APP_NAME = "Payment Receipt Generator Tool"
CONTENTS_DIR = "_app"
# Gitignored copy for day-to-day local use. Rebuilds swap only the exe and
# _app (same swap the updater performs), so the data/, receipts/, backups/,
# and assets/ folders beside the local exe survive every rebuild.
LOCAL_APP_DIR = PROJECT_ROOT / "Local App"
# Folder-based (onedir) release, shipped as a zip. The updater downloads this
# zip, extracts it, and swaps the app binaries in place. See updater.py.
UPDATE_DOWNLOAD_URL = (
    "https://raw.githubusercontent.com/sepsster/"
    "Receipts-Tool/master/Payment%20Receipt%20Generator%20Tool.zip"
)


def main() -> None:
    local_only = "--local" in sys.argv
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                "PyInstaller is not installed. Install it in a local virtual environment, "
                "then rerun source/scripts/build_exe.py."
            ) from exc

    add_data_separator = ";" if sys.platform.startswith("win") else ":"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        # onedir keeps python3xx.dll on disk beside the exe instead of unpacking
        # it into %TEMP% on every launch, which antivirus (Bitdefender) blocks.
        "--onedir",
        "--contents-directory",
        CONTENTS_DIR,
        # UPX-compressed DLLs raise antivirus false positives and are unneeded here.
        "--noupx",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(PROJECT_ROOT),
        "--add-data",
        f"{SOURCE_ROOT / 'assets' / 'logo.png'}{add_data_separator}assets",
        str(SOURCE_ROOT / "receipts_tool_launcher.py"),
    ]
    subprocess.run(cmd, cwd=SOURCE_ROOT, check=True)

    onedir_path = PROJECT_ROOT / APP_NAME
    entry_exe = onedir_path / f"{APP_NAME}.exe"
    if not entry_exe.exists():
        raise SystemExit(f"Build did not produce the expected executable: {entry_exe}")

    local_exe = refresh_local_app(onedir_path)
    print(f"Refreshed local app: {local_exe}")

    if local_only:
        print("Local-only build: release zip and update.json were not touched.")
        return

    zip_path = build_release_zip(onedir_path)
    write_update_manifest(zip_path)
    print(f"Built portable app: {onedir_path}")
    print(f"Packaged release zip: {zip_path}")


def refresh_local_app(onedir_path: Path) -> Path:
    """Copy the freshly built exe and ``_app`` into the gitignored local copy,
    leaving any data/, receipts/, backups/, and assets/ folders there intact."""
    LOCAL_APP_DIR.mkdir(parents=True, exist_ok=True)
    local_contents = LOCAL_APP_DIR / CONTENTS_DIR
    if local_contents.exists():
        shutil.rmtree(local_contents)
    shutil.copytree(onedir_path / CONTENTS_DIR, local_contents)
    local_exe = LOCAL_APP_DIR / f"{APP_NAME}.exe"
    shutil.copy2(onedir_path / f"{APP_NAME}.exe", local_exe)
    return local_exe


def build_release_zip(onedir_path: Path) -> Path:
    """Zip the *contents* of the onedir folder so the archive root holds the
    entry exe and the ``_app`` directory (matching ``entryExe`` in the manifest)."""
    archive_base = PROJECT_ROOT / APP_NAME
    zip_path = archive_base.with_suffix(".zip")
    zip_path.unlink(missing_ok=True)
    shutil.make_archive(str(archive_base), "zip", root_dir=str(onedir_path))
    return zip_path


def write_update_manifest(zip_path: Path) -> None:
    version = project_version()
    manifest_path = PROJECT_ROOT / "update.json"
    manifest = {
        "version": version,
        "packageType": "zip",
        "entryExe": f"{APP_NAME}.exe",
        "downloadUrl": f"{UPDATE_DOWNLOAD_URL}?v={version}",
        "sha256": sha256_file(zip_path),
        "size": zip_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote update manifest: {manifest_path}")


def project_version() -> str:
    pyproject = tomllib.loads((SOURCE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
