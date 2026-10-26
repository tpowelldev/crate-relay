"""
Sync engine: mirror the playlists from a MIXO "Rekordbox XML" export into
Rekordbox's own library, linking each entry to the track Rekordbox already has.

Only playlists and folders under one top-level folder (default "MIXO") are ever
created, changed or deleted. Analysis (BPM, beatgrids, cue points, key) is never
touched, so Rekordbox stays in charge of it.

Optionally ("track info"), the title, artist, album and genre MIXO has for each
track are copied onto the matching track in Rekordbox. The music files themselves
are never changed.
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
from typing import Any

log = logging.getLogger("crate_relay")

PLAYLIST, FOLDER = 0, 1  # DjmdPlaylist.Attribute values
TRACK_FIELDS = ("title", "artist", "album", "genre")  # what "track info" sync copies from MIXO
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
    kind: str  # folder | playlist | track
    path: str  # playlist path, or "Artist – Title" for a track
    detail: str = ""

    def __str__(self) -> str:
        if self.kind == "track":
            return f"Update track info for {self.path}: {self.detail}"
        verb = {"create": "Add", "delete": "Remove", "update": "Update", "reorder": "Reorder"}[self.action]
        text = f"{verb} {self.kind} {self.path}"
        return f"{text} ({self.detail})" if self.detail else text


@dataclass
class SyncResult:
    changes: list[Change] = field(default_factory=list)
    missing: dict[str, list[str]] = field(default_factory=dict)  # file path -> playlists
    playlists: int = 0
    entries: int = 0
    tracks_updated: int = 0
    applied: bool = False
    backup: str | None = None
    finished_at: str = ""

    @property
    def in_sync(self) -> bool:
        return not self.changes

    def to_dict(self) -> dict[str, Any]:
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
@dataclass
class MixoExport:
    tree: list[dict[str, Any]]  # playlist tree, see read_mixo_export
    tracks: dict[str, dict[str, str]]  # file path -> {"title", "artist", "album", "genre"}


def read_mixo(xml_path: Path) -> MixoExport:
    """Parse a MIXO Rekordbox-format XML export: playlists plus each track's info."""
    xml_path = Path(xml_path)
    if not xml_path.exists():
        raise SyncError(f"MIXO export not found: {xml_path}")
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as e:
        raise SyncError(f"Couldn't read the MIXO export ({e}). It may still be being written.") from e

    by_id: dict[str, str] = {}
    tracks: dict[str, dict[str, str]] = {}
    collection = root.find("COLLECTION")
    if collection is not None:
        for t in collection.findall("TRACK"):
            loc = t.get("Location")
            if not loc:
                continue
            path = location_to_path(loc)
            by_id[t.get("TrackID", "")] = path
            tracks[path] = {
                "title": t.get("Name") or "",
                "artist": t.get("Artist") or "",
                "album": t.get("Album") or "",
                "genre": t.get("Genre") or "",
            }

    def walk(node: ET.Element) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for child in node.findall("NODE"):
            name = child.get("Name") or "Untitled"
            if child.get("Type") == "0":
                items.append({"name": name, "folder": True, "children": walk(child)})
                continue
            by_location = child.get("KeyType", "0") == "1"
            paths = []
            for t in child.findall("TRACK"):
                key = t.get("Key", "")
                p = location_to_path(key) if by_location else by_id.get(key)
                if p:
                    paths.append(p)
            items.append({"name": name, "folder": False, "paths": paths})
        return items

    top = root.find("PLAYLISTS/NODE")
    if top is None:
        raise SyncError("The MIXO export contains no playlists.")
    return MixoExport(tree=walk(top), tracks=tracks)


def read_mixo_export(xml_path: Path) -> list[dict[str, Any]]:
    """Parse the playlist tree from a Rekordbox-format XML file.

    Returns nested dicts: {"name", "folder": True, "children": [...]} or
    {"name", "folder": False, "paths": [file paths in playlist order]}.
    """
    return read_mixo(xml_path).tree


# --------------------------------------------------------------------------- Rekordbox side
class _Mirror:
    def __init__(self, db: Any, folder_name: str, dry_run: bool) -> None:
        self.db = db
        self.folder_name = folder_name
        self.dry = dry_run
        self.result = SyncResult()
        self.by_path: dict[str, Any] = {}
        for c in db.get_content():
            if _alive(c) and c.FolderPath:
                self.by_path.setdefault(norm_path(c.FolderPath), c)

    def children(self, parent_id: str) -> list[Any]:
        rows = [p for p in self.db.get_playlist(ParentID=parent_id) if _alive(p)]
        return sorted(rows, key=lambda p: p.Seq or 0)

    def songs(self, playlist_id: str) -> list[Any]:
        rows = [s for s in self.db.get_playlist_songs(PlaylistID=playlist_id) if _alive(s)]
        return sorted(rows, key=lambda s: s.TrackNo or 0)

    def note(self, action: str, is_folder: bool, path: str, detail: str = "") -> None:
        self.result.changes.append(Change(action, "folder" if is_folder else "playlist", path, detail))

    def run(self, export: MixoExport, track_info: bool = False) -> SyncResult:
        self.playlists(export.tree)
        if track_info:
            self.update_track_info(export.tracks)
        return self.result

    def playlists(self, tree: list[dict[str, Any]]) -> None:
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

    def level(self, parent: Any, desired: list[dict[str, Any]], label: str) -> None:
        existing = self.children(parent.ID) if parent is not None else []
        pool: dict[tuple, list[Any]] = {}
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

    def tracks(self, playlist: Any, paths: list[str], label: str, created: Change | None = None) -> None:
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


    # ------------------------------------------------------------------ track info
    def update_track_info(self, tracks: dict[str, dict[str, str]]) -> None:
        """Copy title/artist/album/genre from MIXO onto matching Rekordbox tracks.

        Empty values in MIXO are ignored, so a blank field never wipes Rekordbox's.
        """
        names = {
            "artist": _Names(self.db.get_artist(), self.db.add_artist),
            "album": _Names(self.db.get_album(), self.db.add_album),
            "genre": _Names(self.db.get_genre(), self.db.add_genre),
        }
        id_column = {"artist": "ArtistID", "album": "AlbumID", "genre": "GenreID"}
        now = datetime.now()
        for path in sorted(tracks):
            content = self.by_path.get(path)
            if content is None:
                continue
            want = {f: (tracks[path].get(f) or "").strip() for f in TRACK_FIELDS}
            have = {"title": (content.Title or "").strip()}
            for f, col in id_column.items():
                have[f] = (names[f].name(getattr(content, col)) or "").strip()
            changed = [f for f in TRACK_FIELDS if want[f] and want[f] != have[f]]
            if not changed:
                continue
            artist = want["artist"] or have["artist"]
            title = want["title"] or have["title"]
            label = f"{artist} – {title}" if artist else title
            self.result.changes.append(Change("update", "track", label, ", ".join(changed)))
            self.result.tracks_updated += 1
            if self.dry:
                continue
            for f in changed:
                if f == "title":
                    content.Title = want["title"]
                else:
                    setattr(content, id_column[f], names[f].id_for(want[f]))
            try:
                content.TrackInfoUpdated = str(int(content.TrackInfoUpdated or 0) + 1)
            except (TypeError, ValueError):
                pass
            content.updated_at = now
        if not self.dry:
            self.db.flush()


class _Names:
    """Name <-> ID lookups for one of Rekordbox's artist/album/genre tables."""

    def __init__(self, rows: Any, add: Any) -> None:
        self.add = add
        self.by_id: dict[str, str] = {}
        self.by_name: dict[str, str] = {}
        for r in rows:
            self.by_id[str(r.ID)] = r.Name
            if _alive(r) and r.Name is not None:
                self.by_name.setdefault(r.Name, str(r.ID))

    def name(self, id_: Any) -> str | None:
        return self.by_id.get(str(id_)) if id_ is not None else None

    def id_for(self, name: str) -> str:
        """The ID of an existing entry with exactly this name, or a newly created one."""
        if name not in self.by_name:
            row = self.add(name)
            self.by_id[str(row.ID)] = name
            self.by_name[name] = str(row.ID)
        return self.by_name[name]


def _open_db(db_path: Path) -> Any:
    db_path = Path(db_path)
    if not db_path.exists():
        raise SyncError(f"Rekordbox library not found: {db_path}")
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)
    from pyrekordbox import Rekordbox6Database

    return Rekordbox6Database(path=db_path)


# --------------------------------------------------------------------------- public API
def plan(xml_path: Path, db_path: Path, folder_name: str = "MIXO", track_info: bool = False) -> SyncResult:
    """Work out what a sync would change, without writing anything."""
    export = read_mixo(xml_path)
    db = _open_db(db_path)
    try:
        result = _Mirror(db, folder_name, dry_run=True).run(export, track_info)
    finally:
        db.close()
    result.finished_at = datetime.now().isoformat(timespec="seconds")
    return result


def sync(
    xml_path: Path,
    db_path: Path,
    backup_dir: Path | None = None,
    folder_name: str = "MIXO",
    keep_backups: int = 10,
    track_info: bool = False,
) -> SyncResult:
    """Apply the MIXO playlists (and optionally track info) to Rekordbox. Rekordbox must be closed."""
    export = read_mixo(xml_path)
    if rekordbox_running():
        raise RekordboxRunning()
    db = _open_db(db_path)
    try:
        preview = _Mirror(db, folder_name, dry_run=True).run(export, track_info)
        if preview.in_sync:
            preview.finished_at = datetime.now().isoformat(timespec="seconds")
            return preview
        backup_path = backup(db_path, backup_dir, keep_backups) if backup_dir else None
        result = _Mirror(db, folder_name, dry_run=False).run(export, track_info)
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


def list_backups(backup_dir: Path) -> list[Path]:
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
