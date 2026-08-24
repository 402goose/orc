#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

for f in orc hud.sh.in pricing.jq; do
  [ -f "$f" ] || { echo "build.sh: $f not found" >&2; exit 1; }
done

if command -v shasum >/dev/null 2>&1; then
  ver="$(cat pricing.jq hud.sh.in | shasum -a 256 | cut -c1-12)"
else
  ver="$(cat pricing.jq hud.sh.in | sha256sum | cut -c1-12)"
fi

expand_includes() {
  awk '
    /^[[:space:]]*#INCLUDE / {
      lib = $2
      while ((getline line < lib) > 0) print line
      close(lib)
      next
    }
    { print }
  '
}

expand_includes < hud.sh.in > hud.sh
chmod 755 hud.sh
bash -n hud.sh

expand_includes < orc > orc.stage1

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
' orc.stage1 > orc.tmp

rm -f orc.stage1
bash -n orc.tmp
mv orc.tmp orc
chmod +x orc
echo "orc assembled — hud version $ver"
