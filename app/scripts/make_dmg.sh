#!/bin/bash
# Put compactlab.app in a compressed disk image with a link to /Applications.
#
#   app/scripts/make_dmg.sh path/to/compactlab.app   # prints the .dmg path (next to the app)
#
# Signed with CODESIGN_IDENTITY when it is set (notarize the image with notarize.sh afterwards).
set -euo pipefail

app="$1"
dir="$(cd "$(dirname "$app")" && pwd)"
version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist")"
dmg="$dir/compactlab-$version.dmg"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

ditto "$app" "$stage/compactlab.app"
ln -s /Applications "$stage/Applications"
rm -f "$dmg"
hdiutil create -quiet -volname "compactlab $version" -srcfolder "$stage" -fs HFS+ -format UDZO -ov "$dmg"
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$dmg"
fi
hdiutil verify -quiet "$dmg"
echo "$dmg"
