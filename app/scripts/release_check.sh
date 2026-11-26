#!/bin/bash
# Check that an installed engine and app make a working pair: the engine is this checkout's version and can change
# settings, review → apply → undo through the app restores the settings file byte for byte, and the app (when given)
# opens against that engine with settings writable. Run it on a clean Homebrew install before announcing a release.
#
#   app/scripts/release_check.sh                                   # $(brew --prefix)/bin/compactlab, no app
#   app/scripts/release_check.sh ENGINE [/Applications/compactlab.app]
#   app/scripts/release_check.sh --source [APP]                    # before tagging: install this checkout the
#                                                                  # way the formula does (a git archive, a venv)
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
want="$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

if [ "${1:-}" = "--source" ]; then
    shift
    # The release tarball is `git archive` of the tag; install it into a fresh venv as the formula does.
    git -C "$root" archive --format=tar.gz --prefix="compactlab-$want/" HEAD > "$scratch/src.tar.gz"
    python="$(command -v python3.13 || command -v python3)"
    "$python" -m venv "$scratch/venv"
    "$scratch/venv/bin/pip" install -q "$scratch/src.tar.gz"
    engine="$scratch/venv/bin/compactlab"
else
    engine="${1:-$(brew --prefix)/bin/compactlab}"
    shift || true
fi
app="${1:-}"

say() { printf '%s\n' "$*"; }
fail() { say "FAIL: $*"; exit 1; }

got="$("$engine" --version | awk '{print $2}')"
[ "$got" = "$want" ] || fail "engine $engine is $got, this checkout is $want"
"$engine" engine hello | grep -q '"plan"' || fail "engine $got has no 'engine plan': the app would be read-only"
say "ok: engine $got at $engine"

(cd "$root/app" && COMPACTLAB_TEST_ENGINE="$engine" swift test --filter RealEngineTests 2>&1 | tail -3)
say "ok: review → apply → undo through the app restores the settings file"

if [ -n "$app" ]; then
    home="$scratch/home"
    mkdir -p "$home"
    HOME="$home" "$engine" demo --out "$scratch/demo" --no-open >/dev/null
    cp -R "$scratch/demo/claude" "$home/.claude"
    cp -R "$scratch/demo/codex" "$home/.codex"
    printf '{\n  "theme": "dark"\n}\n' > "$home/.claude/settings.json"  # no threshold set: something to suggest
    HOME="$home" "$engine" engine calibrate --since-days 0 --offline-pricing --reason release-check >/dev/null
    diag="$(HOME="$home" COMPACTLAB_ENGINE="$engine" "$app/Contents/MacOS/compactlab" \
        --render-panel "$scratch/panel.png" --render-review "$scratch/review.png" --wait 30)"
    say "$diag"
    grep -q "^compactlab app $want" <<<"$diag" || fail "the app is not $want"
    grep -q "result: loaded" <<<"$diag" || fail "the app did not load the result"
    grep -q "^settings: writable, [1-9]" <<<"$diag" || fail "the app cannot change settings with this engine"
    codesign --verify --deep --strict "$app" || fail "the app's signature does not verify"
    spctl --assess --type execute "$app" 2>/dev/null && say "ok: Gatekeeper accepts the app" \
        || say "note: Gatekeeper rejects the app (expected for an ad hoc build)"
    say "ok: the app $want opens against engine $got with settings writable"
fi
say "release check passed"
