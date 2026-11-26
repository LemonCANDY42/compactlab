#!/bin/bash
# Store the signing and notarization credentials as this repository's Actions secrets, for the `App` workflow.
# Run it yourself in a terminal: it asks for the .p12 password (nothing in the shell history), checks both files
# locally the way the workflow will use them, and hands every value to `gh secret set` on standard input, never in its
# arguments or a file.
#
#   app/scripts/set_signing_secrets.sh DeveloperID.p12 AuthKey_XXXXXXXXXX.p8 ISSUER_ID [--check]
#
# DeveloperID.p12  your "Developer ID Application" certificate and its private key, exported from Keychain Access
#                  (My Certificates → right-click the certificate → Export → .p12, with a password)
# AuthKey_*.p8     an App Store Connect API key (Users and Access → Integrations → Team Keys, role Developer); the key
#                  ID is read from the file name
# ISSUER_ID        the issuer ID shown above that list of keys
# --check          check the files and stop before storing anything
set -euo pipefail

[ $# -ge 3 ] || { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
p12="$1" p8="$2" issuer="$3" check="${4:-}"
repo="$(cd "$(dirname "$0")" && gh repo view --json nameWithOwner --jq .nameWithOwner)"

fail() { echo "error: $*" >&2; exit 1; }
[ -f "$p12" ] || fail "no such file: $p12"
[ -f "$p8" ] || fail "no such file: $p8"
key_id="$(basename "$p8" .p8)"
key_id="${key_id#AuthKey_}"
[[ "$key_id" =~ ^[A-Z0-9]{10}$ ]] || fail "$p8: expected the name App Store Connect gives it, AuthKey_<10-character key ID>.p8"
[[ "$issuer" =~ ^[0-9a-f-]{36}$ ]] || fail "the issuer ID looks like 12345678-90ab-cdef-1234-567890abcdef"
grep -q "BEGIN PRIVATE KEY" "$p8" || fail "$p8 is not a private key"

IFS= read -r -s -p "Password of $(basename "$p12"): " password
echo

# The same import the workflow does, into a throwaway keychain: the password opens the .p12 and it holds a
# Developer ID Application identity with its private key.
tmp="$(mktemp -d)"
kc="$tmp/check.keychain-db"
trap 'security delete-keychain "$kc" 2>/dev/null || true; rm -rf "$tmp"' EXIT
kcpw="$(uuidgen)"
security create-keychain -p "$kcpw" "$kc"
security unlock-keychain -p "$kcpw" "$kc"
security import "$p12" -k "$kc" -P "$password" -T /usr/bin/codesign >/dev/null 2>&1 ||
    fail "$p12 does not open with that password (a .p12 made by OpenSSL 3 or later does not import either: export it from Keychain Access)"
identity="$(security find-identity -v -p codesigning "$kc" | sed -n 's/.*"\(Developer ID Application:.*\)"/\1/p' | head -1)"
[ -n "$identity" ] || fail "$p12 holds no Developer ID Application certificate with its private key"
echo "ok: $identity"
echo "ok: notary key $key_id, issuer $issuer"

if [ "$check" = "--check" ]; then
    echo "checked; nothing stored"
    exit 0
fi

echo "storing 5 secrets in $repo"
base64 -i "$p12" | gh secret set DEVELOPER_ID_CERT_P12 --repo "$repo"
printf '%s' "$password" | gh secret set DEVELOPER_ID_CERT_PASSWORD --repo "$repo"
base64 -i "$p8" | gh secret set NOTARY_KEY_P8 --repo "$repo"
printf '%s' "$key_id" | gh secret set NOTARY_KEY_ID --repo "$repo"
printf '%s' "$issuer" | gh secret set NOTARY_ISSUER --repo "$repo"
gh secret list --repo "$repo"
cat <<EOF

Done. To sign, notarize and attach the disk image and cask to the latest release, re-run its App workflow:
  gh run rerun --failed \$(gh run list --repo $repo --workflow App --event release --limit 1 --json databaseId --jq '.[0].databaseId') --repo $repo
EOF
