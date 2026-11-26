#!/bin/bash
# Build compactlab.app from the Swift package.
#
#   app/scripts/package_app.sh [--universal] [--out DIR]
#
# Bundles the translations (app/Resources/*.lproj) and an icon drawn by make_icon.swift. A disk image:
# app/scripts/make_dmg.sh.
#
# Signing: with CODESIGN_IDENTITY set (a "Developer ID Application: …" identity in the keychain) the app is signed
# with the hardened runtime and a secure timestamp, ready for notarization; without it, ad-hoc (runs on this Mac only).
# Notarization: app/scripts/notarize.sh.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
root="$(cd "$here/.." && pwd)"
out="$here/build"
arch_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --universal) arch_args=(--arch arm64 --arch x86_64) ;;
    --out) out="$2"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

bundle_id="${BUNDLE_ID:-io.github.lemoncandy42.compactlab}"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)"
build_number="$(git -C "$root" rev-list --count HEAD 2>/dev/null || echo 1)"

cd "$here"
swift build -c release --product compactlab-app ${arch_args[@]+"${arch_args[@]}"}
bin="$(swift build -c release --product compactlab-app ${arch_args[@]+"${arch_args[@]}"} --show-bin-path)/compactlab-app"

app="$out/compactlab.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin" "$app/Contents/MacOS/compactlab"
cp -R "$here"/Resources/*.lproj "$app/Contents/Resources/"
icon="$out/AppIcon.icns"
if [ ! -f "$icon" ] || [ "$here/scripts/make_icon.swift" -nt "$icon" ]; then
  swift "$here/scripts/make_icon.swift" "$icon"
fi
cp "$icon" "$app/Contents/Resources/AppIcon.icns"
langs="$(cd "$here/Resources" && ls -d *.lproj | sed 's/\.lproj$//' | sed 's/.*/    <string>&<\/string>/')"
cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleLocalizations</key>
  <array>
${langs}
  </array>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleExecutable</key><string>compactlab</string>
  <key>CFBundleIdentifier</key><string>${bundle_id}</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>compactlab</string>
  <key>CFBundleDisplayName</key><string>compactlab</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${version}</string>
  <key>CFBundleVersion</key><string>${build_number}</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHumanReadableCopyright</key><string>MIT License</string>
</dict>
</plist>
PLIST
plutil -lint "$app/Contents/Info.plist" >/dev/null

if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$app"
else
  codesign --force --sign - "$app"
fi
codesign --verify --strict --verbose=2 "$app"
echo "$app"
