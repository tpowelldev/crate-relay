"""Settings and locations."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import APP_NAME

HOME = Path.home()
SUPPORT_DIR = HOME / "Library" / "Application Support" / APP_NAME
LOG_DIR = HOME / "Library" / "Logs" / APP_NAME
BACKUP_DIR = SUPPORT_DIR / "Backups"
SETTINGS_FILE = SUPPORT_DIR / "settings.json"
LOG_FILE = LOG_DIR / "crate-relay.log"

DEFAULT_XML = HOME / "Music" / "MIXO" / "Exports" / "REKORDBOX" / "rekordbox.xml"
DEFAULT_DB = HOME / "Library" / "Pioneer" / "rekordbox" / "master.db"


@dataclass
class Settings:
    xml_path: str = str(DEFAULT_XML)
    db_path: str = str(DEFAULT_DB)
    folder_name: str = "MIXO"
    auto_sync: bool = True
    notifications: bool = True
    keep_backups: int = 10
    welcomed: bool = False
    last_sync: str = ""  # ISO time of the last sync that changed Rekordbox

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(SETTINGS_FILE)

    @property
    def xml(self) -> Path:
        return Path(self.xml_path).expanduser()

    @property
    def db(self) -> Path:
        return Path(self.db_path).expanduser()


def setup_logging(to_file: bool = True, verbose: bool = False) -> None:
    handlers: list = []
    if to_file:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3))
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)
