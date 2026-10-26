"""Thin wrappers around the macOS APIs the app needs (via PyObjC)."""
from __future__ import annotations

import logging
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSBezelBorder,
    NSFont,
    NSMakeRect,
    NSModalResponseOK,
    NSOpenPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSViewWidthSizable,
    NSWorkspace,
)
from Foundation import NSURL, NSBundle, NSObject

from . import BUNDLE_ID

log = logging.getLogger("crate_relay")


# --------------------------------------------------------------------------- app bundle
def is_bundled() -> bool:
    """True when running as the packaged .app rather than from source."""
    return bool(getattr(sys, "frozen", False)) and bool(NSBundle.mainBundle().bundleIdentifier())


def resource(name: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for candidate in (base / "crate_relay" / "resources" / name, Path(__file__).parent / "resources" / name):
        if candidate.exists():
            return str(candidate)
    return str(Path(__file__).parent / "resources" / name)


def activate() -> None:
    """Bring our (Dock-less) app to the front so dialogs aren't hidden behind other windows."""
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


# --------------------------------------------------------------------------- dialogs
def alert(title: str, message: str, ok: str = "OK", cancel: str | None = None) -> bool:
    """Standard alert. Returns True if the first (default) button was clicked."""
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(message)
    a.addButtonWithTitle_(ok)
    if cancel:
        a.addButtonWithTitle_(cancel)
    activate()
    return a.runModal() == NSAlertFirstButtonReturn


def text_dialog(title: str, message: str, text: str, ok: str, cancel: str | None = None,
                editable: bool = False, width: int = 480, height: int = 200) -> tuple[bool, str]:
    """Alert with a scrollable text box (or a one-line field if height is 0)."""
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(message)
    a.addButtonWithTitle_(ok)
    if cancel:
        a.addButtonWithTitle_(cancel)
    if height == 0:
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, width, 24))
        field.setStringValue_(text)
        a.setAccessoryView_(field)
        a.window().setInitialFirstResponder_(field)
        value = field.stringValue
    else:
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        view.setMinSize_((0, height))
        view.setMaxSize_((1e7, 1e7))
        view.setVerticallyResizable_(True)
        view.setHorizontallyResizable_(False)
        view.setAutoresizingMask_(NSViewWidthSizable)
        view.textContainer().setWidthTracksTextView_(True)
        view.setFont_(NSFont.systemFontOfSize_(12))
        view.setTextContainerInset_((4, 6))
        view.setString_(text)
        view.setEditable_(editable)
        view.setSelectable_(True)
        scroll.setDocumentView_(view)
        a.setAccessoryView_(scroll)
        value = view.string
    activate()
    clicked = a.runModal() == NSAlertFirstButtonReturn
    return clicked, str(value())


# --------------------------------------------------------------------------- Rekordbox process
def _rekordbox_apps() -> list:
    found = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        name = (app.localizedName() or "").lower()
        bundle = (app.bundleIdentifier() or "").lower()
        if "agent" in name or "agent" in bundle:
            continue  # rekordboxAgent (cloud sync helper) doesn't lock the library the same way
        if name == "rekordbox" or bundle.startswith("com.pioneerdj.rekordbox"):
            found.append(app)
    return found


def rekordbox_is_running() -> bool:
    return bool(_rekordbox_apps())


def quit_rekordbox() -> None:
    """Ask Rekordbox to quit normally (it can still prompt the user to save)."""
    for app in _rekordbox_apps():
        app.terminate()


# --------------------------------------------------------------------------- Finder & files
def reveal(path: Path) -> None:
    path = Path(path)
    if path.exists():
        NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(str(path), "")
    elif path.parent.exists():
        open_path(path.parent)


def open_path(path: Path) -> None:
    subprocess.run(["/usr/bin/open", str(path)], check=False)


def open_url(url: str) -> None:
    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))


def copy_text(text: str) -> None:
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def choose_file(title: str, start: Path, extensions: list[str]) -> Path | None:
    panel = NSOpenPanel.openPanel()
    panel.setTitle_(title)
    panel.setMessage_(title)
    panel.setPrompt_("Choose")
    panel.setCanChooseFiles_(True)
    panel.setCanChooseDirectories_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setShowsHiddenFiles_(True)
    panel.setAllowedFileTypes_(extensions)
    start = Path(start)
    folder = start if start.is_dir() else start.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(folder)))
    activate()
    if panel.runModal() == NSModalResponseOK and panel.URLs():
        return Path(panel.URLs()[0].path())
    return None


def choose_folder(title: str, start: Path) -> Path | None:
    panel = NSOpenPanel.openPanel()
    panel.setTitle_(title)
    panel.setMessage_(title)
    panel.setPrompt_("Choose")
    panel.setCanChooseFiles_(False)
    panel.setCanChooseDirectories_(True)
    panel.setAllowsMultipleSelection_(False)
    if Path(start).exists():
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(start)))
    activate()
    if panel.runModal() == NSModalResponseOK and panel.URLs():
        return Path(panel.URLs()[0].path())
    return None


# --------------------------------------------------------------------------- notifications
try:
    from UserNotifications import (
        UNAuthorizationOptionAlert,
        UNAuthorizationOptionSound,
        UNMutableNotificationContent,
        UNNotificationPresentationOptionBanner,
        UNNotificationPresentationOptionList,
        UNNotificationRequest,
        UNUserNotificationCenter,
    )

    class _NotificationDelegate(NSObject):
        # Show banners even though a menu bar app counts as "frontmost" while its menu is open.
        def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, handler):
            handler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList)

    _HAS_UN = True
except Exception:  # pragma: no cover - older PyObjC / macOS
    _HAS_UN = False

_delegate = None


def setup_notifications() -> None:
    global _delegate
    if not (_HAS_UN and is_bundled()):
        return
    try:
        center = UNUserNotificationCenter.currentNotificationCenter()
        _delegate = _NotificationDelegate.alloc().init()
        center.setDelegate_(_delegate)
        center.requestAuthorizationWithOptions_completionHandler_(
            UNAuthorizationOptionAlert | UNAuthorizationOptionSound, lambda granted, error: None
        )
    except Exception:
        log.exception("Couldn't set up notifications")


def notify(title: str, body: str) -> None:
    if _HAS_UN and is_bundled():
        try:
            content = UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(body)
            request = UNNotificationRequest.requestWithIdentifier_content_trigger_(str(uuid.uuid4()), content, None)
            UNUserNotificationCenter.currentNotificationCenter().addNotificationRequest_withCompletionHandler_(
                request, None
            )
            return
        except Exception:
            log.exception("Notification failed")
    # Running from source: fall back to AppleScript (shows as "Script Editor").
    script = f"display notification {_as_str(body)} with title {_as_str(title)}"
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False)


def _as_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- open at login
_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"


def _app_service():
    try:
        from ServiceManagement import SMAppService  # macOS 13+

        return SMAppService.mainAppService()
    except Exception:
        return None


def login_item_supported() -> bool:
    return is_bundled()


def login_item_enabled() -> bool:
    svc = _app_service()
    if svc is not None:
        return svc.status() == 1  # SMAppServiceStatusEnabled
    return _AGENT.exists()


def set_login_item(enabled: bool) -> bool:
    """Returns True on success."""
    svc = _app_service()
    if svc is not None:
        if enabled:
            ok, err = svc.registerAndReturnError_(None)
        else:
            ok, err = svc.unregisterAndReturnError_(None)
        if not ok:
            log.warning("Login item change failed: %s", err)
        return bool(ok)
    # macOS 11-12 fallback: a LaunchAgent that opens the app at login
    if enabled:
        app_path = NSBundle.mainBundle().bundlePath()
        _AGENT.parent.mkdir(parents=True, exist_ok=True)
        _AGENT.write_bytes(plistlib.dumps({
            "Label": BUNDLE_ID,
            "ProgramArguments": ["/usr/bin/open", "-g", app_path],
            "RunAtLoad": True,
        }))
    else:
        _AGENT.unlink(missing_ok=True)
    return True
