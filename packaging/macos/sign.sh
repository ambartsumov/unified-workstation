#!/usr/bin/env bash
# Sign, package and notarize the macOS build. Runs in the release workflow; can be run by a
# maintainer on a Mac with a Developer ID certificate in the keychain.
#
#   DEVELOPER_ID="Developer ID Application: Name (TEAMID)" \
#   NOTARY_PROFILE=uw-notary \            # created once: xcrun notarytool store-credentials
#   packaging/macos/sign.sh <arch>        # arm64 | x86_64
#
# Without DEVELOPER_ID the script stops: an unsigned build is never produced as a release
# artifact. Use `make package` for a local, unsigned development build.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARCH="${1:-$(uname -m)}"
NAME="$(python3 "$ROOT/scripts/product.py" --get NAME)"
SLUG="$(python3 "$ROOT/scripts/product.py" --get SLUG)"
VERSION="$(python3 "$ROOT/scripts/product.py" --get VERSION)"
APP="$ROOT/dist/$NAME.app"
DMG="$ROOT/dist/$SLUG-$VERSION-macos-$ARCH.dmg"
: "${DEVELOPER_ID:?set DEVELOPER_ID to a Developer ID Application identity; release builds are never unsigned}"
[ -d "$APP" ] || { echo "run pyinstaller first (make package)" >&2; exit 1; }

# Inside-out: every nested binary, then the bundle, with the hardened runtime and a timestamp.
find "$APP" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 |
    xargs -0 -n 20 codesign --force --options runtime --timestamp --entitlements "$ROOT/packaging/macos/entitlements.plist" --sign "$DEVELOPER_ID"
codesign --force --options runtime --timestamp --entitlements "$ROOT/packaging/macos/entitlements.plist" --sign "$DEVELOPER_ID" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
codesign --force --timestamp --sign "$DEVELOPER_ID" "$DMG"

if [ -n "${NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
else
    echo "NOTARY_PROFILE not set: the image is signed but NOT notarized. Do not publish it." >&2
    exit 2
fi
echo "built and notarized $DMG"
