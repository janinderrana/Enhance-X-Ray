#!/bin/bash
# Build the plugin from this repo, install it into calibre, and copy the zip to the NAS folder,
# so the repo, calibre and the NAS all hold the same build.
#
#   ./deploy.sh              build + install + copy to NAS
#   ./deploy.sh --no-nas     build + install only
#   ./deploy.sh --no-install build + copy to NAS only
#
# NAS_DIR (destination folder) comes from the environment or from a git-ignored .deploy.env file.
# calibre must be closed: it rewrites its plugin registry when it quits.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .deploy.env ] && . ./.deploy.env

DO_INSTALL=1; DO_NAS=1
for a in "$@"; do
  case "$a" in
    --no-install) DO_INSTALL=0 ;;
    --no-nas) DO_NAS=0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

PLUGINS="$HOME/Library/Preferences/calibre/plugins"
BACKUP="${BACKUP_DIR:-$HOME/.claude-xray/backup}"
VER=$(sed -nE 's/^VERSION = \(([0-9]+), ([0-9]+), ([0-9]+)\)/\1.\2.\3/p' __init__.py)
REV=$(git describe --always --dirty --tags 2>/dev/null || echo unknown)
case "$REV" in *-dirty) echo "warning: uncommitted changes in the working tree ($REV)" >&2 ;; esac

if [ "$DO_INSTALL" = 1 ] && pgrep -x calibre >/dev/null; then
  echo "calibre is running; quit it first (it rewrites the plugin registry on exit)." >&2
  exit 1
fi

./build.sh | head -1
ZIP="dist/ClaudeXRay_mac_v$VER.zip"
SHA=$(shasum -a 256 "$ZIP" | cut -d' ' -f1)

if [ "$DO_INSTALL" = 1 ]; then
  if [ -f "$PLUGINS/Claude X-Ray.zip" ]; then
    mkdir -p "$BACKUP"
    cp -p "$PLUGINS/Claude X-Ray.zip" "$BACKUP/Claude X-Ray.zip.pre-deploy-$(date +%Y%m%d-%H%M%S)"
    echo "backed up the installed plugin to $BACKUP"
  fi
  /Applications/calibre.app/Contents/MacOS/calibre-customize -a "$PWD/$ZIP"
  GOT=$(unzip -p "$PLUGINS/Claude X-Ray.zip" __init__.py | sed -nE 's/^VERSION = \(([0-9]+), ([0-9]+), ([0-9]+)\)/\1.\2.\3/p')
  [ "$GOT" = "$VER" ] || { echo "installed version is $GOT, expected $VER" >&2; exit 1; }
  echo "installed in calibre: $VER"
fi

if [ "$DO_NAS" = 1 ]; then
  [ -n "${NAS_DIR:-}" ] || { echo "NAS_DIR is not set (environment or .deploy.env)" >&2; exit 1; }
  [ -d "$NAS_DIR" ] || { echo "NAS folder not found (is the share mounted?): $NAS_DIR" >&2; exit 1; }
  OUT="$NAS_DIR/ClaudeXRay_mac_v${VER}_repo.zip"
  cp -X "$ZIP" "$OUT"
  printf '%s  %s\nbuilt from %s\n' "$SHA" "$(basename "$OUT")" "$REV" > "$OUT.sha256.txt"
  [ "$(shasum -a 256 "$OUT" | cut -d' ' -f1)" = "$SHA" ] || { echo "NAS copy does not match the build" >&2; exit 1; }
  echo "copied to the NAS: $OUT"
fi
echo "sha256 $SHA  ($REV)"
