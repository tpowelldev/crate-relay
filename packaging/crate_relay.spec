# PyInstaller spec for "Crate Relay.app". Build with:  ./scripts/build.sh
# -*- mode: python -*-
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
init = (SRC / "crate_relay" / "__init__.py").read_text()
VERSION = re.search(r'__version__ = "([^"]+)"', init).group(1)
BUNDLE_ID = re.search(r'BUNDLE_ID = "([^"]+)"', init).group(1)

hidden = (
    collect_submodules("pyrekordbox")
    + collect_submodules("sqlalchemy.dialects.sqlite")
    + ["sqlcipher3", "sqlcipher3.dbapi2", "UserNotifications", "ServiceManagement"]
)

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(SRC)],
    datas=[(str(SRC / "crate_relay" / "resources"), "crate_relay/resources")],
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Crate Relay",
    console=False,
    target_arch=None,
    codesign_identity=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Crate Relay")
app = BUNDLE(
    coll,
    name="Crate Relay.app",
    icon=str(ROOT / "assets" / "AppIcon.icns"),
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": "Crate Relay",
        "CFBundleDisplayName": "Crate Relay",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,  # menu bar only, no Dock icon
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHumanReadableCopyright": "MIT License. Not affiliated with MIXO or AlphaTheta / Pioneer DJ.",
    },
)
