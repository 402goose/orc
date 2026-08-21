#!/usr/bin/env bash
set -euo pipefail

DEST="${DEST:-$HOME/.local/bin}"
SRC="$(cd "$(dirname "$0")" && pwd)/orc"

missing=""
for dep in jq curl fzf; do
  command -v "$dep" >/dev/null 2>&1 || missing="$missing $dep"
done
if [ -n "$missing" ]; then
  echo "missing dependencies:$missing"
  echo "install with: brew install$missing"
  exit 1
fi
command -v claude >/dev/null 2>&1 || {
  echo "claude not found on PATH — install Claude Code first:"
  echo "  npm i -g @anthropic-ai/claude-code   (or see https://claude.com/claude-code)"
  exit 1
}

mkdir -p "$DEST"
cp "$SRC" "$DEST/orc"
chmod +x "$DEST/orc"
echo "installed: $DEST/orc"

case ":$PATH:" in
  *":$DEST:"*) ;;
  *) echo "note: $DEST is not on your PATH — add: export PATH=\"$DEST:\$PATH\"" ;;
esac

echo "next: run 'orc' to start the setup wizard"
