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
UPDATE_DOWNLOAD_URL = (
    "https://raw.githubusercontent.com/sepsster/"
    "Receipts-Tool/master/Payment%20Receipt%20Generator%20Tool.exe"
)


def main() -> None:
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
        "--onefile",
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

    output_path = PROJECT_ROOT / f"{APP_NAME}.exe"
    write_update_manifest(output_path)
    print(f"Built portable app: {output_path}")


def write_update_manifest(output_path: Path) -> None:
    version = project_version()
    manifest_path = PROJECT_ROOT / "update.json"
    manifest = {
        "version": version,
        "downloadUrl": f"{UPDATE_DOWNLOAD_URL}?v={version}",
        "sha256": sha256_file(output_path),
        "size": output_path.stat().st_size,
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
