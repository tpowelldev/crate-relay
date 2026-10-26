"""Command-line interface: `crate-relay [--apply] [--json]`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, engine
from .config import BACKUP_DIR, Settings, setup_logging


def main(argv: list | None = None) -> int:
    s = Settings.load()
    ap = argparse.ArgumentParser(
        prog="crate-relay",
        description="Mirror MIXO playlists (and optionally track info) into Rekordbox. "
        "BPM, beatgrids and cue points are never touched.",
    )
    ap.add_argument("--apply", action="store_true", help="write the changes (default: preview only)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--xml", type=Path, default=s.xml, help=f"MIXO Rekordbox XML export (default: {s.xml})")
    ap.add_argument("--db", type=Path, default=s.db, help=f"Rekordbox master.db (default: {s.db})")
    ap.add_argument("--folder", default=s.folder_name, help=f"Rekordbox folder to sync into (default: {s.folder_name})")
    ap.add_argument(
        "--track-info", action=argparse.BooleanOptionalAction, default=s.track_info,
        help="also copy title, artist, album and genre from MIXO onto Rekordbox tracks "
             f"(default: {'on' if s.track_info else 'off'}, from the app's settings)",
    )
    ap.add_argument("--no-backup", action="store_true", help="skip the backup before writing")
    ap.add_argument("--version", action="version", version=f"crate-relay {__version__}")
    args = ap.parse_args(argv)

    setup_logging(to_file=False)
    try:
        if args.apply:
            result = engine.sync(
                args.xml, args.db,
                backup_dir=None if args.no_backup else BACKUP_DIR,
                folder_name=args.folder, keep_backups=s.keep_backups, track_info=args.track_info,
            )
        else:
            result = engine.plan(args.xml, args.db, folder_name=args.folder, track_info=args.track_info)
    except engine.SyncError as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    for c in result.changes:
        print(f"  {c}")
    summary = f"{result.playlists} playlists, {result.entries} tracks."
    if result.tracks_updated:
        summary += f" Track info {'updated' if result.applied else 'to update'}: {result.tracks_updated} tracks."
    if result.in_sync:
        print(f"Rekordbox matches MIXO. {summary}")
    elif result.applied:
        print(f"Applied {len(result.changes)} change(s). {summary}")
        if result.backup:
            print(f"Backup: {result.backup}")
    else:
        print(f"{len(result.changes)} change(s) to make. Run again with --apply to write them.")
    if result.missing:
        print(f"\n{len(result.missing)} track(s) in MIXO playlists aren't in your Rekordbox collection:")
        for p in sorted(result.missing):
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
