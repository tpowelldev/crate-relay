"""
Sync engine: mirror the playlists from a MIXO "Rekordbox XML" export into
Rekordbox's own library, linking each entry to the track Rekordbox already has.

Only playlists and folders under one top-level folder (default "MIXO") are ever
created, changed or deleted. Track data (BPM, beatgrids, cue points, key, tags,
ratings) is never touched, so Rekordbox stays in charge of analysis.
"""
from __future__ import annotations

import logging
import shutil
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("crate_relay")

PLAYLIST, FOLDER = 0, 1  # DjmdPlaylist.Attribute values
BACKUP_FILES = ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml")


class SyncError(Exception):
    """A problem the user needs to know about (bad paths, unreadable export...)."""


class RekordboxRunning(SyncError):
    def __init__(self) -> None:
        super().__init__("Rekordbox is open. Quit Rekordbox to sync.")


# --------------------------------------------------------------------------- results
@dataclass
class Change:
    action: str  # create | delete | update | reorder
    kind: str  # folder | playlist
    path: str
    detail: str = ""

    def __str__(self) -> str:
        verb = {"create": "Add", "delete": "Remove", "update": "Update", "reorder": "Reorder"}[self.action]
        text = f"{verb} {self.kind} {self.path}"
        return f"{text} ({self.detail})" if self.detail else text


@dataclass
class SyncResult:
    changes: List[Change] = field(default_factory=list)
    missing: Dict[str, List[str]] = field(default_factory=dict)  # file path -> playlists
    playlists: int = 0
    entries: int = 0
    applied: bool = False
    backup: Optional[str] = None
    finished_at: str = ""

    @property
    def in_sync(self) -> bool:
        return not self.changes

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["changes"] = [str(c) for c in self.changes]
        d["in_sync"] = self.in_sync
        return d


# --------------------------------------------------------------------------- helpers
def norm_path(p: str) -> str:
    """Compare paths in NFC form; macOS file names can be stored NFD or NFC."""
    return unicodedata.normalize("NFC", p)


def location_to_path(loc: str) -> str:
    for prefix in ("file://localhost", "file://"):
        if loc.startswith(prefix):
            loc = loc[len(prefix):]
            break
    return norm_path(urllib.parse.unquote(loc))


def rekordbox_running() -> bool:
    """True if the Rekordbox app (not its background agent) is running."""
    import psutil

    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").rsplit(".", 1)[0]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name.lower() == "rekordbox":
            return True
    return False


def _alive(row: Any) -> bool:
    return not getattr(row, "rb_local_deleted", 0)


# --------------------------------------------------------------------------- MIXO side
def read_mixo_export(xml_path: Path) -> List[Dict[str, Any]]:
    """Parse the playlist tree from a Rekordbox-format XML file.

    Returns nested dicts: {"name", "folder": True, "children": [...]} or
    {"name", "folder": False, "paths": [file paths in playlist order]}.
    """
    xml_path = Path(xml_path)
    if not xml_path.exists():
        raise SyncError(f"MIXO export not found: {xml_path}")
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as e:
        raise SyncError(f"Couldn't read the MIXO export ({e}). It may still be being written.") from e

    tracks: Dict[str, str] = {}
    collection = root.find("COLLECTION")
    if collection is not None:
        for t in collection.findall("TRACK"):
            loc = t.get("Location")
            if loc:
                tracks[t.get("TrackID", "")] = location_to_path(loc)

    def walk(node: ET.Element) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for child in node.findall("NODE"):
            name = child.get("Name") or "Untitled"
            if child.get("Type") == "0":
                items.append({"name": name, "folder": True, "children": walk(child)})
                continue
            by_location = child.get("KeyType", "0") == "1"
            paths = []
            for t in child.findall("TRACK"):
                key = t.get("Key", "")
                p = location_to_path(key) if by_location else tracks.get(key)
                if p:
                    paths.append(p)
            items.append({"name": name, "folder": False, "paths": paths})
        return items

    top = root.find("PLAYLISTS/NODE")
    if top is None:
        raise SyncError("The MIXO export contains no playlists.")
    return walk(top)


# --------------------------------------------------------------------------- Rekordbox side
class _Mirror:
    def __init__(self, db: Any, folder_name: str, dry_run: bool) -> None:
        self.db = db
        self.folder_name = folder_name
        self.dry = dry_run
        self.result = SyncResult()
        self.by_path: Dict[str, Any] = {}
        for c in db.get_content():
            if _alive(c) and c.FolderPath:
                self.by_path.setdefault(norm_path(c.FolderPath), c)

    def children(self, parent_id: str) -> List[Any]:
        rows = [p for p in self.db.get_playlist(ParentID=parent_id) if _alive(p)]
        return sorted(rows, key=lambda p: p.Seq or 0)

    def songs(self, playlist_id: str) -> List[Any]:
        rows = [s for s in self.db.get_playlist_songs(PlaylistID=playlist_id) if _alive(s)]
        return sorted(rows, key=lambda s: s.TrackNo or 0)

    def note(self, action: str, is_folder: bool, path: str, detail: str = "") -> None:
        self.result.changes.append(Change(action, "folder" if is_folder else "playlist", path, detail))

    def run(self, tree: List[Dict[str, Any]]) -> SyncResult:
        root = next(
            (p for p in self.children("root") if p.Name == self.folder_name and p.Attribute == FOLDER),
            None,
        )
        if root is None:
            self.note("create", True, self.folder_name)
            if not self.dry:
                root = self.db.create_playlist_folder(self.folder_name)
                self.db.flush()
        self.level(root, tree, self.folder_name)
        return self.result

    def level(self, parent: Any, desired: List[Dict[str, Any]], label: str) -> None:
        existing = self.children(parent.ID) if parent is not None else []
        pool: Dict[tuple, List[Any]] = {}
        for p in existing:
            pool.setdefault((p.Name, p.Attribute == FOLDER), []).append(p)

        matched = []
        keep = set()
        for item in desired:
            candidates = pool.get((item["name"], item["folder"]), [])
            node = candidates.pop(0) if candidates else None
            if node is not None:
                keep.add(node.ID)
            matched.append((item, node))

        for p in existing:  # delete first so sequence numbers stay consistent
            if p.ID not in keep:
                self.note("delete", p.Attribute == FOLDER, f"{label}/{p.Name}")
                if not self.dry:
                    self.db.delete_playlist(p)
                    self.db.flush()

        for seq, (item, node) in enumerate(matched, start=1):
            path = f"{label}/{item['name']}"
            if not item["folder"]:
                self.result.playlists += 1
            created = None
            if node is None:
                self.note("create", item["folder"], path)
                created = self.result.changes[-1]
                if not self.dry:
                    make = self.db.create_playlist_folder if item["folder"] else self.db.create_playlist
                    node = make(item["name"], parent=parent, seq=seq)
                    self.db.flush()
            elif (node.Seq or 0) != seq and not self.dry:
                self.db.move_playlist(node, seq=seq)
                self.db.flush()

            if item["folder"]:
                self.level(node, item["children"], path)
            else:
                self.tracks(node, item["paths"], path, created)

    def tracks(self, playlist: Any, paths: List[str], label: str, created: Optional[Change] = None) -> None:
        wanted = []
        for p in paths:
            c = self.by_path.get(p)
            if c is None:
                self.result.missing.setdefault(p, []).append(label)
            else:
                wanted.append(str(c.ID))
        self.result.entries += len(wanted)

        rows = self.songs(playlist.ID) if playlist is not None else []
        current = [str(s.ContentID) for s in rows]
        if current == wanted:
            return
        if created is not None:
            created.detail = f"{len(wanted)} tracks"
        else:
            added = len(set(wanted) - set(current))
            removed = len(set(current) - set(wanted))
            if added or removed:
                parts = [f"+{added}" if added else "", f"−{removed}" if removed else ""]
                detail = " ".join(x for x in parts if x) + f", {len(wanted)} tracks"
                self.note("update", False, label, detail)
            else:
                self.note("reorder", False, label)
        if self.dry:
            return
        for s in reversed(rows):
            self.db.remove_from_playlist(playlist, s)
        self.db.flush()
        for cid in wanted:
            self.db.add_to_playlist(playlist, cid)
        self.db.flush()


def _open_db(db_path: Path) -> Any:
    db_path = Path(db_path)
    if not db_path.exists():
        raise SyncError(f"Rekordbox library not found: {db_path}")
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)
    from pyrekordbox import Rekordbox6Database

    return Rekordbox6Database(path=db_path)


# --------------------------------------------------------------------------- public API
def plan(xml_path: Path, db_path: Path, folder_name: str = "MIXO") -> SyncResult:
    """Work out what a sync would change, without writing anything."""
    tree = read_mixo_export(xml_path)
    db = _open_db(db_path)
    try:
        result = _Mirror(db, folder_name, dry_run=True).run(tree)
    finally:
        db.close()
    result.finished_at = datetime.now().isoformat(timespec="seconds")
    return result


def sync(
    xml_path: Path,
    db_path: Path,
    backup_dir: Optional[Path] = None,
    folder_name: str = "MIXO",
    keep_backups: int = 10,
) -> SyncResult:
    """Apply the MIXO playlists to Rekordbox. Rekordbox must be closed."""
    tree = read_mixo_export(xml_path)
    if rekordbox_running():
        raise RekordboxRunning()
    db = _open_db(db_path)
    try:
        preview = _Mirror(db, folder_name, dry_run=True).run(tree)
        if preview.in_sync:
            preview.finished_at = datetime.now().isoformat(timespec="seconds")
            return preview
        backup_path = backup(db_path, backup_dir, keep_backups) if backup_dir else None
        result = _Mirror(db, folder_name, dry_run=False).run(tree)
        db.commit()
        result.applied = True
        result.backup = str(backup_path) if backup_path else None
        log.info("Applied %d change(s): %s", len(result.changes), "; ".join(map(str, result.changes)))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    result.finished_at = datetime.now().isoformat(timespec="seconds")
    return result


# --------------------------------------------------------------------------- backups
def backup(db_path: Path, backup_dir: Path, keep: int = 10) -> Path:
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    dest = backup_dir / datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    dest.mkdir(parents=True, exist_ok=True)
    for name in BACKUP_FILES:
        src = db_path.parent / name
        if src.exists():
            shutil.copy2(src, dest / name)
    for old in list_backups(backup_dir)[keep:]:
        shutil.rmtree(old, ignore_errors=True)
    log.info("Backed up Rekordbox library to %s", dest)
    return dest


def list_backups(backup_dir: Path) -> List[Path]:
    """Backups, newest first."""
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []
    found = [p for p in backup_dir.iterdir() if p.is_dir() and (p / "master.db").exists()]
    return sorted(found, reverse=True)


def restore(backup_path: Path, db_path: Path) -> None:
    """Put a backup back in place. Rekordbox must be closed."""
    backup_path, db_path = Path(backup_path), Path(db_path)
    if not (backup_path / "master.db").exists():
        raise SyncError(f"Not a Crate Relay backup: {backup_path}")
    if rekordbox_running():
        raise RekordboxRunning()
    for name in ("master.db-wal", "master.db-shm"):
        (db_path.parent / name).unlink(missing_ok=True)
    for name in BACKUP_FILES:
        src = backup_path / name
        if src.exists():
            shutil.copy2(src, db_path.parent / name)
    log.info("Restored Rekordbox library from %s", backup_path)
