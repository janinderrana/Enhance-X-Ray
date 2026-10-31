#!/bin/bash
# Build the calibre plugin zip: ./build.sh  ->  dist/ClaudeXRay_mac_v<version>.zip
set -euo pipefail
cd "$(dirname "$0")"
VER=$(sed -nE 's/^VERSION = \(([0-9]+), ([0-9]+), ([0-9]+)\)/\1.\2.\3/p' __init__.py)
[ -n "$VER" ] || { echo "could not read VERSION from __init__.py" >&2; exit 1; }
OUT="dist/ClaudeXRay_mac_v$VER.zip"
mkdir -p dist
rm -f "$OUT"
zip -X -q "$OUT" __init__.py __main__.py enrich.py ui.py dialog.py cast_pass.py \
    kin_words.py book_brief.py ipa_pass.py plugin-import-name-claude_xray.txt
echo "built $OUT"
echo "install (calibre closed): /Applications/calibre.app/Contents/MacOS/calibre-customize -a \"$PWD/$OUT\""
