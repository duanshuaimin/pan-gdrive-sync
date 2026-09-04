import logging
import os
import shutil
from pathlib import Path

from .config import CONFIG_DIR

logger = logging.getLogger(__name__)

LEGACY_CONFIG_DIR = Path.home() / ".config" / "pangdrive"


def tasks_db_path() -> Path:
    return CONFIG_DIR / "tasks.db"


def service_account_path() -> Path:
    return CONFIG_DIR / "service_account.json"


def migrate_legacy_artifacts() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    pairs = [
        (LEGACY_CONFIG_DIR / "tasks.db", tasks_db_path()),
        (LEGACY_CONFIG_DIR / "service_account.json", service_account_path()),
    ]
    for src, dst in pairs:
        if dst.exists() or not src.is_file():
            continue
        shutil.copy2(src, dst)
        if dst.name == "service_account.json":
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass
        logger.info("Migrated %s -> %s", src, dst)
