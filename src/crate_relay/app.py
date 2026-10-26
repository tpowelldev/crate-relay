"""Crate Relay menu bar app."""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import rumps

from . import APP_NAME, REPO_URL, __version__, engine, macos
from .config import BACKUP_DIR, LOG_FILE, Settings, setup_logging

log = logging.getLogger("crate_relay")

POLL_SECONDS = 2.0        # how often to look for MIXO exports and Rekordbox quitting
SETTLE_SECONDS = 4.0      # let MIXO finish writing the XML before reading it
AFTER_QUIT_SECONDS = 3.0  # let Rekordbox finish saving after it quits

ICON = {
    "idle": "menubar.png",
    "off": "menubar-off.png",
    "waiting": "menubar-waiting.png",
    "attention": "menubar-attention.png",
}
SYNC_FRAMES = [f"menubar-sync-{i}.png" for i in range(8)]


def file_signature(path: Path) -> tuple[float, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def ago(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    secs = (datetime.now() - then).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if then.date() == datetime.now().date():
        return f"at {then.strftime('%H:%M')}"
    if secs < 7 * 86400:
        return then.strftime("%a %H:%M")
    return then.strftime("%-d %b")


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


class CrateRelay(rumps.App):
    def __init__(self) -> None:
        super().__init__(APP_NAME, icon=macos.resource(ICON["idle"]), template=True, quit_button=None)
        self.settings = Settings.load()
        self.results: queue.Queue[tuple] = queue.Queue()

        self.busy: str | None = None          # "check" | "sync" | "preview" while a job runs
        self.result: engine.SyncResult | None = None
        self.error: str | None = None
        self.planned_sig = None                  # XML signature the current result is based on
        self.needs_check = True
        self.rb_open = macos.rekordbox_is_running()
        self.rb_closed_at: float | None = None
        self.sync_when_closed = False
        self.in_modal = False
        self.frame = 0
        self.icon_name = ICON["idle"]
        self.notified_pending = False

        self._build_menu()
        macos.setup_notifications()
        rumps.Timer(self.poll, POLL_SECONDS).start()
        rumps.Timer(self.drain, 0.2).start()
        self.anim = rumps.Timer(self.animate, 0.08)
        rumps.Timer(self.first_run, 1.0).start()
        self.refresh()

    # ------------------------------------------------------------------ menu
    def _build_menu(self) -> None:
        self.status_item = rumps.MenuItem("Starting…")
        self.detail_item = rumps.MenuItem("")
        self.sync_item = rumps.MenuItem("Sync Now", callback=self.on_sync_now, key="s")
        self.preview_item = rumps.MenuItem("Preview Changes…", callback=self.on_preview)
        self.missing_item = rumps.MenuItem("All tracks found in Rekordbox")

        self.auto_item = rumps.MenuItem("Sync Automatically", callback=self.on_toggle_auto)
        self.login_item = rumps.MenuItem("Open at Login", callback=self.on_toggle_login)

        self.xml_item = rumps.MenuItem("MIXO Export…", callback=self.on_choose_xml)
        self.db_item = rumps.MenuItem("Rekordbox Library…", callback=self.on_choose_db)
        self.folder_item = rumps.MenuItem("Rekordbox Folder Name…", callback=self.on_folder_name)
        self.track_info_item = rumps.MenuItem("Update Track Info from MIXO", callback=self.on_toggle_track_info)
        self.notify_item = rumps.MenuItem("Show Notifications", callback=self.on_toggle_notifications)
        settings = rumps.MenuItem("Settings")
        for item in (self.xml_item, self.db_item, self.folder_item, rumps.separator,
                     self.track_info_item, self.notify_item):
            settings.add(item)

        backups = rumps.MenuItem("Backups")
        backups.add(rumps.MenuItem("Show Backups in Finder", callback=self.on_show_backups))
        backups.add(rumps.MenuItem("Restore a Backup…", callback=self.on_restore))

        self.menu = [
            self.status_item,
            self.detail_item,
            rumps.separator,
            self.sync_item,
            self.preview_item,
            self.missing_item,
            rumps.separator,
            self.auto_item,
            self.login_item,
            settings,
            backups,
            rumps.separator,
            rumps.MenuItem("Show Log", callback=self.on_show_log),
            rumps.MenuItem(f"About {APP_NAME}", callback=self.on_about),
            rumps.separator,
            rumps.MenuItem(f"Quit {APP_NAME}", callback=rumps.quit_application, key="q"),
        ]

    def set_icon(self, name: str) -> None:
        if name != self.icon_name:
            self.icon_name = name
            self.icon = macos.resource(name)

    def refresh(self) -> None:
        """Update the menu and icon to match the current state."""
        s = self.settings
        r = self.result
        pending = bool(r and not r.applied and r.changes)

        if self.busy in ("check", "preview"):
            status = "Checking for changes…"
        elif self.busy == "sync":
            status = "Syncing to Rekordbox…"
        elif not s.xml.exists():
            status = "MIXO export not found"
        elif not s.db.exists():
            status = "Rekordbox library not found"
        elif self.error:
            status = "Couldn’t sync"
        elif pending and self.rb_open:
            status = f"{plural(len(r.changes), 'change')} waiting for Rekordbox to close"
        elif pending:
            status = f"{plural(len(r.changes), 'change')} ready to sync"
        elif r:
            status = "Up to date"
        else:
            status = "Waiting to check…"
        self.status_item.title = status

        if not s.xml.exists():
            detail = "In MIXO, turn on Auto-Export to Rekordbox XML"
        elif not s.db.exists():
            detail = "Choose your master.db in Settings"
        elif self.error and not self.busy:
            detail = self.error if len(self.error) <= 70 else self.error[:67] + "…"
        elif r:
            parts = [plural(r.playlists, "playlist"), plural(r.entries, "track")]
            if s.last_sync:
                parts.append(f"synced {ago(s.last_sync)}")
            detail = " · ".join(parts)
        else:
            detail = f"Syncing into the “{s.folder_name}” folder"
        self.detail_item.title = detail

        missing = len(r.missing) if r else 0
        if missing:
            self.missing_item.title = f"{plural(missing, 'track')} not in Rekordbox yet…"
            self.missing_item.set_callback(self.on_missing)
        else:
            self.missing_item.title = "All tracks found in Rekordbox"
            self.missing_item.set_callback(None)

        self.auto_item.state = int(s.auto_sync)
        self.notify_item.state = int(s.notifications)
        self.track_info_item.state = int(s.track_info)
        if macos.login_item_supported():
            self.login_item.state = int(macos.login_item_enabled())
        else:
            self.login_item.set_callback(None)
        self.xml_item.title = f"MIXO Export: {s.xml.name}…"
        self.db_item.title = f"Rekordbox Library: {s.db.name}…"
        self.folder_item.title = f"Rekordbox Folder: {s.folder_name}…"

        if self.busy:
            return  # the animation owns the icon
        if not s.xml.exists() or not s.db.exists() or self.error:
            self.set_icon(ICON["attention"])
        elif pending:
            self.set_icon(ICON["waiting"])
        elif not s.auto_sync:
            self.set_icon(ICON["off"])
        else:
            self.set_icon(ICON["idle"])

    # ------------------------------------------------------------------ background work
    def start_job(self, mode: str) -> None:
        if self.busy:
            return
        s = self.settings
        self.busy = mode
        self.error = None if mode != "preview" else self.error
        self.anim.start()
        sig = file_signature(s.xml)
        args = (mode, s.xml, s.db, s.folder_name, s.keep_backups, s.auto_sync, s.track_info, sig)
        threading.Thread(target=self._work, args=args, daemon=True).start()
        self.refresh()

    def _work(self, mode, xml, db, folder, keep, auto, track_info, sig) -> None:
        try:
            result = engine.plan(xml, db, folder, track_info=track_info)
            wants_apply = mode == "sync" or (mode == "check" and auto)
            if wants_apply and not result.in_sync:
                if engine.rekordbox_running():
                    if mode == "sync":
                        raise engine.RekordboxRunning()
                else:
                    self.results.put(("progress", "sync"))
                    result = engine.sync(
                        xml, db, backup_dir=BACKUP_DIR, folder_name=folder, keep_backups=keep, track_info=track_info
                    )
            self.results.put(("done", mode, result, None, sig))
        except engine.SyncError as e:
            self.results.put(("done", mode, None, str(e), sig))
        except Exception as e:  # never let the worker die silently
            log.exception("Sync failed")
            self.results.put(("done", mode, None, f"Unexpected error: {e}", sig))

    def drain(self, _timer=None) -> None:
        while True:
            try:
                msg = self.results.get_nowait()
            except queue.Empty:
                return
            if msg[0] == "progress":
                self.busy = msg[1]
                self.refresh()
                continue
            _, mode, result, error, sig = msg
            self.busy = None
            self.anim.stop()
            self.planned_sig = sig
            if error:
                self.on_error(mode, error)
            else:
                self.on_result(mode, result)
            self.refresh()
            if mode == "preview" and not error:
                self.show_preview(result)

    def on_result(self, mode: str, result: engine.SyncResult) -> None:
        had_result = self.result is not None
        before_missing = set(self.result.missing) if self.result else set()
        self.result = result
        self.error = None
        s = self.settings
        if result.applied:
            s.last_sync = result.finished_at
            s.save()
            self.notified_pending = False
            self.notify("Synced to Rekordbox", self.describe(result))
        elif result.changes and self.rb_open and s.auto_sync:
            if not self.notified_pending:
                self.notified_pending = True
                self.notify(
                    "Playlist changes waiting",
                    f"{plural(len(result.changes), 'change')} will sync when you quit Rekordbox.",
                )
        new_missing = set(result.missing) - before_missing
        if had_result and new_missing:
            self.notify(
                "Tracks need adding to Rekordbox",
                f"{plural(len(new_missing), 'track')} in your MIXO playlists "
                f"{'is' if len(new_missing) == 1 else 'are'}n’t in your Rekordbox collection yet.",
            )

    def on_error(self, mode: str, error: str) -> None:
        if mode == "sync" and "Rekordbox is open" in error:
            self.ask_to_quit_rekordbox()
            return
        if error != self.error:
            self.notify("Couldn’t sync", error)
        self.error = error
        log.warning("%s", error)

    @staticmethod
    def describe(result: engine.SyncResult) -> str:
        n = len(result.changes)
        if n <= 3:
            text = "; ".join(str(c) for c in result.changes)
        elif result.tracks_updated:
            others = n - result.tracks_updated
            text = f"Updated info for {plural(result.tracks_updated, 'track')}"
            if others:
                text += f" and made {plural(others, 'playlist change')}"
        else:
            text = f"{plural(n, 'change')} made in Rekordbox"
        if result.missing:
            text += f". {plural(len(result.missing), 'track')} still missing from Rekordbox."
        return text

    def notify(self, title: str, body: str) -> None:
        if self.settings.notifications:
            macos.notify(title, body)

    # ------------------------------------------------------------------ timers
    def poll(self, _timer=None) -> None:
        rb_open = macos.rekordbox_is_running()
        if self.rb_open and not rb_open:
            self.rb_closed_at = time.time()
        self.rb_open = rb_open

        if not self.busy and not self.in_modal:
            s = self.settings
            sig = file_signature(s.xml)
            xml_changed = sig is not None and sig != self.planned_sig and time.time() - sig[0] > SETTLE_SECONDS
            closed_recently = self.rb_closed_at and time.time() - self.rb_closed_at > AFTER_QUIT_SECONDS
            if closed_recently:
                self.rb_closed_at = None
                if self.sync_when_closed:
                    self.sync_when_closed = False
                    self.start_job("sync")
                    return
                self.needs_check = True  # Rekordbox may have changed the library
            if (xml_changed or self.needs_check) and s.xml.exists() and s.db.exists():
                self.needs_check = False
                self.start_job("check")
                return
        self.refresh()

    def animate(self, _timer=None) -> None:
        self.frame = (self.frame + 1) % len(SYNC_FRAMES)
        self.icon_name = SYNC_FRAMES[self.frame]
        self.icon = macos.resource(self.icon_name)

    def first_run(self, timer) -> None:
        timer.stop()
        s = self.settings
        if s.welcomed:
            return
        s.welcomed = True
        s.save()
        self.modal_alert(
            f"Welcome to {APP_NAME}",
            "Crate Relay keeps your MIXO playlists mirrored in Rekordbox.\n\n"
            "1. In MIXO, open Settings → Advanced, turn on Auto-Export and choose Rekordbox XML.\n"
            f"2. Your playlists appear in Rekordbox inside a folder called “{s.folder_name}”, "
            "linked to the tracks already in your collection.\n\n"
            "Rekordbox stays in charge of BPM, beatgrids and cue points — Crate Relay never changes them. "
            "It only updates the library while Rekordbox is closed, and backs it up first.",
            ok="Get Started",
        )
        if not s.xml.exists():
            self.on_choose_xml(None)

    # ------------------------------------------------------------------ dialogs
    def modal_alert(self, title: str, message: str, ok: str = "OK", cancel: str | None = None) -> bool:
        self.in_modal = True
        try:
            return macos.alert(title, message, ok=ok, cancel=cancel)
        finally:
            self.in_modal = False

    def modal_text(self, title: str, message: str, text: str, ok: str, cancel: str | None,
                   editable: bool = False, size=(480, 200)) -> tuple[bool, str]:
        self.in_modal = True
        try:
            return macos.text_dialog(title, message, text, ok, cancel, editable, *size)
        finally:
            self.in_modal = False

    def show_preview(self, result: engine.SyncResult) -> None:
        if result.in_sync:
            self.modal_alert(
                "Rekordbox is up to date",
                f"The “{self.settings.folder_name}” folder in Rekordbox already matches MIXO "
                f"({plural(result.playlists, 'playlist')}, {plural(result.entries, 'track')}).",
            )
            return
        clicked, _ = self.modal_text(
            f"{plural(len(result.changes), 'change')} to sync",
            f"Playlist changes are made inside the “{self.settings.folder_name}” folder in Rekordbox"
            + (", and track info updates change only title, artist, album and genre" if result.tracks_updated else "")
            + ". BPM, beatgrids and cue points are never changed.",
            "\n".join(str(c) for c in result.changes),
            ok="Sync Now",
            cancel="Close",
        )
        if clicked:
            self.on_sync_now(None)

    def ask_to_quit_rekordbox(self) -> None:
        if self.modal_alert(
            "Rekordbox is open",
            "Crate Relay can only update your library while Rekordbox is closed. "
            "Quit Rekordbox now? Your playlists will sync as soon as it has closed.",
            ok="Quit Rekordbox",
            cancel="Not Now",
        ):
            self.sync_when_closed = True
            macos.quit_rekordbox()

    # ------------------------------------------------------------------ menu actions
    def on_sync_now(self, _sender) -> None:
        if self.busy:
            return
        if macos.rekordbox_is_running():
            self.ask_to_quit_rekordbox()
        else:
            self.start_job("sync")

    def on_preview(self, _sender) -> None:
        self.start_job("preview")

    def on_missing(self, _sender) -> None:
        r = self.result
        if not r or not r.missing:
            return
        lines = []
        for path, playlists in sorted(r.missing.items()):
            names = ", ".join(sorted({p.split("/")[-1] for p in playlists}))
            lines.append(f"{path}\n    in: {names}")
        clicked, _ = self.modal_text(
            f"{plural(len(r.missing), 'track')} not in Rekordbox yet",
            "These tracks are in your MIXO playlists but not in your Rekordbox collection. "
            "Add them to Rekordbox (drag them into Collection so they get analysed) and they’ll "
            "join their playlists on the next sync.",
            "\n".join(lines),
            ok="Close",
            cancel="Copy List",
            size=(560, 240),
        )
        if not clicked:
            macos.copy_text("\n".join(sorted(r.missing)))

    def on_toggle_auto(self, _sender) -> None:
        self.settings.auto_sync = not self.settings.auto_sync
        self.settings.save()
        self.needs_check = self.settings.auto_sync
        self.refresh()

    def on_toggle_track_info(self, _sender) -> None:
        s = self.settings
        if not s.track_info and not self.modal_alert(
            "Update track info from MIXO?",
            "Crate Relay will also copy the title, artist, album and genre you’ve set in MIXO onto the "
            "matching tracks in your Rekordbox collection, on every sync.\n\n"
            "• BPM, beatgrids, cue points and key are never changed.\n"
            "• Your music files aren’t changed, only Rekordbox’s library.\n"
            "• Fields that are empty in MIXO are left as they are in Rekordbox.\n\n"
            "Edits you make to these fields in Rekordbox will be replaced by MIXO’s on future syncs. "
            "You’ll see the changes before anything is updated.",
            ok="Turn On",
            cancel="Cancel",
        ):
            return
        s.track_info = not s.track_info
        s.save()
        if s.track_info and not self.busy:
            # Show what would change straight away, rather than letting auto-sync apply it unseen.
            self.result = None
            self.error = None
            self.needs_check = False
            self.start_job("preview")
        else:
            self.reset_state()

    def on_toggle_notifications(self, _sender) -> None:
        self.settings.notifications = not self.settings.notifications
        self.settings.save()
        self.refresh()

    def on_toggle_login(self, _sender) -> None:
        want = not macos.login_item_enabled()
        if not macos.set_login_item(want):
            self.modal_alert(
                "Couldn’t change the login item",
                "macOS didn’t allow it. You can add Crate Relay yourself in "
                "System Settings → General → Login Items.",
            )
        self.refresh()

    def on_choose_xml(self, _sender) -> None:
        self.in_modal = True
        try:
            path = macos.choose_file("Choose the Rekordbox XML file that MIXO exports", self.settings.xml, ["xml"])
        finally:
            self.in_modal = False
        if path:
            self.settings.xml_path = str(path)
            self.settings.save()
            self.reset_state()

    def on_choose_db(self, _sender) -> None:
        self.in_modal = True
        try:
            path = macos.choose_file("Choose Rekordbox’s master.db", self.settings.db, ["db"])
        finally:
            self.in_modal = False
        if path:
            self.settings.db_path = str(path)
            self.settings.save()
            self.reset_state()

    def on_folder_name(self, _sender) -> None:
        old = self.settings.folder_name
        clicked, text = self.modal_text(
            "Rekordbox folder name",
            "Your MIXO playlists are kept inside this top-level folder in Rekordbox. "
            "If you rename it, the old folder stays in Rekordbox until you delete it.",
            old,
            ok="Save",
            cancel="Cancel",
            editable=True,
            size=(300, 0),
        )
        name = (text or "").strip()
        if clicked and name and name != old:
            self.settings.folder_name = name
            self.settings.save()
            self.reset_state()

    def reset_state(self) -> None:
        self.result = None
        self.error = None
        self.planned_sig = None
        self.needs_check = True
        self.refresh()

    def on_show_backups(self, _sender) -> None:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        macos.open_path(BACKUP_DIR)

    def on_restore(self, _sender) -> None:
        backups = engine.list_backups(BACKUP_DIR)
        if not backups:
            self.modal_alert("No backups yet", "Crate Relay backs up your Rekordbox library before every sync.")
            return
        self.in_modal = True
        try:
            chosen = macos.choose_folder("Choose a backup to restore", BACKUP_DIR)
        finally:
            self.in_modal = False
        if not chosen:
            return
        if not self.modal_alert(
            f"Restore the backup from {chosen.name}?",
            "Your Rekordbox library will be put back exactly as it was at that time, including any "
            "changes you made in Rekordbox since. This can’t be undone."
            + ("\n\nAutomatic sync is on, so the MIXO folder will then be brought back in line with MIXO."
               if self.settings.auto_sync else ""),
            ok="Restore",
            cancel="Cancel",
        ):
            return
        try:
            engine.restore(chosen, self.settings.db)
        except engine.SyncError as e:
            self.modal_alert("Couldn’t restore", str(e))
            return
        self.notify("Backup restored", f"Rekordbox library restored from {chosen.name}.")
        self.reset_state()

    def on_show_log(self, _sender) -> None:
        if LOG_FILE.exists():
            macos.open_path(LOG_FILE)
        else:
            self.modal_alert("No log yet", "Nothing has been logged yet.")

    def on_about(self, _sender) -> None:
        if not self.modal_alert(
            f"{APP_NAME} {__version__}",
            "Keeps your MIXO playlists (and, optionally, track info) mirrored in Rekordbox, while "
            "Rekordbox stays in charge of BPM, beatgrids and cue points.\n\n"
            "Crate Relay is an independent project and isn’t affiliated with MIXO or AlphaTheta / Pioneer DJ. "
            "It changes Rekordbox’s library file directly, so it backs it up before every change.",
            ok="OK",
            cancel="View on GitHub",
        ):
            macos.open_url(REPO_URL)


def main() -> None:
    setup_logging(to_file=True)
    log.info("%s %s starting", APP_NAME, __version__)
    CrateRelay().run()


if __name__ == "__main__":
    main()
