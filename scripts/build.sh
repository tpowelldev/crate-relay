#!/bin/bash
# Build "dist/Crate Relay.app" and a zip for GitHub releases.
# Uses its own virtual environment (.venv), so your system Python is left alone.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/crate_relay/__init__.py)"
ARCH="$(uname -m)"

if [ ! -x .venv/bin/python ]; then
  echo "→ Creating virtual environment (.venv)"
  "$PYTHON" -m venv .venv
fi
echo "→ Installing build dependencies"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[build]"

if command -v iconutil >/dev/null; then
  iconutil -c icns assets/AppIcon.iconset -o assets/AppIcon.icns  # Apple's tool makes the most complete .icns
fi

echo "→ Building Crate Relay $VERSION ($ARCH)"
rm -rf build "dist/Crate Relay.app" "dist/Crate Relay"
.venv/bin/pyinstaller --noconfirm --clean --log-level WARN --distpath dist --workpath build packaging/crate_relay.spec

APP="dist/Crate Relay.app"
codesign --verify --deep "$APP" 2>/dev/null || codesign --force --deep --sign - "$APP"

ZIP="dist/Crate-Relay-$VERSION-macOS-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

echo
echo "✓ Built $APP"
echo "✓ Release zip: $ZIP"
echo "  Try it:  open \"$APP\""
