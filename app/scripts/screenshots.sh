#!/bin/bash
# Render the menu bar panel and the review window from synthetic demo data (never your own history) into
# docs/assets/app-panel*.png and app-review*.png, in English and Chinese. Uses the app in app/build and an engine
# (default: this checkout's .venv) in a scratch home, as the App workflow's smoke test does.
#
#   app/scripts/package_app.sh && app/scripts/screenshots.sh [ENGINE]
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
engine="${1:-$root/.venv/bin/compactlab}"
app="$root/app/build/compactlab.app/Contents/MacOS/compactlab"
out="$root/docs/assets"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

[ -x "$app" ] || { echo "build the app first: app/scripts/package_app.sh" >&2; exit 1; }
home="$scratch/you"
mkdir -p "$home"
"$engine" demo --out "$scratch/demo" --no-open >/dev/null
cp -R "$scratch/demo/claude" "$home/.claude"
cp -R "$scratch/demo/codex" "$home/.codex"
printf '{\n  "theme": "dark"\n}\n' > "$home/.claude/settings.json"  # no threshold set: something to suggest
HOME="$home" "$engine" engine calibrate --since-days 0 --offline-pricing --reason screenshots >/dev/null 2>&1

for lang in en zh-Hans; do
    suffix=""; [ "$lang" = en ] || suffix=".zh-CN"
    HOME="$home" COMPACTLAB_ENGINE="$engine" "$app" --render-panel "$out/app-panel$suffix.png" \
        --render-review "$out/app-review$suffix.png" --wait 30 -AppleLanguages "($lang)" >/dev/null
done
ls -1 "$out"/app-*.png
