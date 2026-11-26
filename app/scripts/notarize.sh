#!/bin/bash
# Notarize and staple a Developer ID-signed compactlab.app or disk image.
#
#   app/scripts/notarize.sh path/to/compactlab.app      # then zips it: compactlab-app.zip next to it
#   app/scripts/notarize.sh path/to/compactlab-X.Y.Z.dmg
#
# Credentials, as an App Store Connect API key: NOTARY_KEY (path to the .p8 file), NOTARY_KEY_ID, NOTARY_ISSUER.
set -euo pipefail

target="$1"
: "${NOTARY_KEY:?}" "${NOTARY_KEY_ID:?}" "${NOTARY_ISSUER:?}"
dir="$(dirname "$target")"
submit() {
  xcrun notarytool submit "$1" --key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER" --wait
}

case "$target" in
  *.dmg)
    submit "$target"
    xcrun stapler staple "$target"
    xcrun stapler validate "$target"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$target"
    echo "$target"
    ;;
  *.app)
    zip="$dir/compactlab-notarize.zip"
    ditto -c -k --keepParent "$target" "$zip"
    submit "$zip"
    rm -f "$zip"
    xcrun stapler staple "$target"
    xcrun stapler validate "$target"
    spctl --assess --type execute --verbose=2 "$target"
    ditto -c -k --keepParent "$target" "$dir/compactlab-app.zip"
    echo "$dir/compactlab-app.zip"
    ;;
  *) echo "notarize.sh: expected a .app or a .dmg" >&2; exit 2 ;;
esac
