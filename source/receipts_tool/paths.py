from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_DIR_ENV = "RECEIPTS_TOOL_APP_DIR"


@dataclass(frozen=True)
class AppPaths:
    app_dir: Path
    data_dir: Path
    backup_dir: Path
    receipts_dir: Path
    assets_dir: Path
    db_path: Path
    logo_path: Path


def get_app_dir() -> Path:
    override = os.environ.get(APP_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parents[1]


def get_resource_path(relative_path: str) -> Path:
    app_path = get_app_dir() / relative_path
    if app_path.exists():
        return app_path

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundled = Path(bundle_root) / relative_path
        if bundled.exists():
            return bundled

    return app_path


def get_paths() -> AppPaths:
    app_dir = get_app_dir()
    return AppPaths(
        app_dir=app_dir,
        data_dir=app_dir / "data",
        backup_dir=app_dir / "backups",
        receipts_dir=app_dir / "receipts",
        assets_dir=app_dir / "assets",
        db_path=app_dir / "data" / "receipts.sqlite",
        logo_path=get_resource_path("assets/logo.png"),
    )


def ensure_base_dirs(paths: AppPaths) -> None:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    paths.receipts_dir.mkdir(parents=True, exist_ok=True)
