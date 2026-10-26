# Changelog

## 1.1.0 — 2026-10-25

- New, optional: **Update Track Info from MIXO** (Settings menu, or `--track-info` on the command line) copies the title, artist, album and genre from MIXO onto the matching tracks in Rekordbox on every sync. BPM, beatgrids, cue points, key and the music files are never changed; empty MIXO fields are ignored.
- Preview Changes and notifications include track info updates.
- About window links to the GitHub repo.
- CI pins ruff so new ruff releases can't break the build.

## 1.0.0 — 2026-09-26

First release.

- Menu bar app that mirrors MIXO's Rekordbox XML export into a "MIXO" folder in Rekordbox, linking to tracks already in the collection.
- Automatic sync when MIXO re-exports and when Rekordbox quits; Sync Now and Preview Changes.
- Backup before every change, with restore from the menu.
- List of tracks missing from the Rekordbox collection.
- Notifications, Open at Login, configurable export path, library path and folder name.
- `crate-relay` command-line tool.
