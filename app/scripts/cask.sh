#!/bin/bash
# Print the Homebrew cask for a release's disk image (to commit to LemonCANDY42/homebrew-tap as Casks/compactlab-app.rb).
#
#   app/scripts/cask.sh path/to/compactlab-X.Y.Z.dmg > Casks/compactlab-app.rb
set -euo pipefail

dmg="$1"
version="$(basename "$dmg" .dmg | sed 's/^compactlab-//')"
sha="$(shasum -a 256 "$dmg" | cut -d' ' -f1)"
sed -e "s/@VERSION@/$version/" -e "s/@SHA256@/$sha/" "$(dirname "$0")/../packaging/compactlab-app.rb"
