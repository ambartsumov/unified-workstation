#!/usr/bin/env bash
# Build an AppImage from the PyInstaller output.
#   packaging/linux/appimage.sh <arch>      (x86_64 | aarch64)
# Needs `appimagetool` on PATH (https://github.com/AppImage/appimagetool).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARCH="${1:-$(uname -m)}"
GEN="$ROOT/packaging/linux/generated"
APP_ID="$(python3 "$ROOT/scripts/product.py" --get APP_ID)"
VERSION="$(python3 "$ROOT/scripts/product.py" --get VERSION)"
SLUG="$(python3 "$ROOT/scripts/product.py" --get SLUG)"
DIR="$ROOT/build/AppDir"
[ -d "$ROOT/dist/UnifiedWorkstation" ] || { echo "run pyinstaller first (make package)" >&2; exit 1; }
[ -f "$GEN/$APP_ID.desktop" ] || python3 "$ROOT/scripts/product.py" --render
mkdir -p "$DIR/usr/lib" "$DIR/usr/share/applications" "$DIR/usr/share/metainfo" "$DIR/usr/share/icons/hicolor/scalable/apps"
cp -a "$ROOT/dist/UnifiedWorkstation" "$DIR/usr/lib/$SLUG"
cp "$GEN/$APP_ID.desktop" "$DIR/usr/share/applications/"
cp "$GEN/$APP_ID.desktop" "$DIR/"
cp "$GEN/$APP_ID.metainfo.xml" "$DIR/usr/share/metainfo/"
cp "$ROOT/suw/app/static/icon.svg" "$DIR/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
cp "$ROOT/suw/app/static/icon.svg" "$DIR/$APP_ID.svg"
cat > "$DIR/AppRun" <<APPRUN
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
exec "\$HERE/usr/lib/$SLUG/unified-workstation" "\$@"
APPRUN
chmod +x "$DIR/AppRun"
mkdir -p "$ROOT/dist"
ARCH="$ARCH" appimagetool "$DIR" "$ROOT/dist/$SLUG-$VERSION-$ARCH.AppImage"
echo "built dist/$SLUG-$VERSION-$ARCH.AppImage"
