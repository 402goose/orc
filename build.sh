#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f hud.sh ] || { echo "build.sh: hud.sh not found" >&2; exit 1; }
[ -f orc ] || { echo "build.sh: orc not found" >&2; exit 1; }

ver="$(shasum -a 256 hud.sh | cut -c1-12)"

awk -v ver="$ver" '
  /^ORC_HUD_VERSION=/ { print "ORC_HUD_VERSION=\"" ver "\""; next }
  /^[[:space:]]*cat <<.ORC_HUD_BODY.$/ {
    print
    n = 0
    while ((getline line < "hud.sh") > 0) { n++; if (n > 1) print line }
    close("hud.sh")
    skip = 1
    next
  }
  skip && /^ORC_HUD_BODY$/ { print; skip = 0; next }
  skip { next }
  { print }
' orc > orc.tmp

bash -n orc.tmp
mv orc.tmp orc
chmod +x orc
echo "orc assembled — hud version $ver"
