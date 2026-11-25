#!/bin/bash
# Notarize and staple a Developer ID-signed compactlab.app, then zip it for distribution.
#
#   app/scripts/notarize.sh path/to/compactlab.app
#
# Credentials, as an App Store Connect API key: NOTARY_KEY (path to the .p8 file), NOTARY_KEY_ID, NOTARY_ISSUER.
set -euo pipefail

app="$1"
: "${NOTARY_KEY:?}" "${NOTARY_KEY_ID:?}" "${NOTARY_ISSUER:?}"
dir="$(dirname "$app")"
zip="$dir/compactlab-notarize.zip"

ditto -c -k --keepParent "$app" "$zip"
xcrun notarytool submit "$zip" --key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER" --wait
xcrun stapler staple "$app"
xcrun stapler validate "$app"
spctl --assess --type execute --verbose=2 "$app"
rm -f "$zip"
ditto -c -k --keepParent "$app" "$dir/compactlab-app.zip"
echo "$dir/compactlab-app.zip"
