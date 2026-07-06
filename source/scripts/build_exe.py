from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SOURCE_ROOT.parent
APP_NAME = "Payment Receipt Generator Tool"


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
    print(f"Built portable app: {output_path}")


if __name__ == "__main__":
    main()
