#!/usr/bin/env bash
set -euo pipefail

DEST="${DEST:-$HOME/.local/bin}"
SRC="$(cd "$(dirname "$0")" && pwd)/orc"
FUSION_SRC="$(cd "$(dirname "$0")" && pwd)/fusion"
FUSION_CORE_SRC="$(cd "$(dirname "$0")" && pwd)/fusion_core.py"
FUSION_WORKFLOW_SRC="$(cd "$(dirname "$0")" && pwd)/fusion_workflow.py"

missing=""
for dep in jq curl; do
  command -v "$dep" >/dev/null 2>&1 || missing="$missing $dep"
done
if [ -n "$missing" ]; then
  echo "missing dependencies:$missing"
  case "$(uname -s)" in
    Darwin)
      echo "install with: brew install$missing" ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then echo "install with: sudo apt-get install$missing"
      elif command -v dnf >/dev/null 2>&1; then echo "install with: sudo dnf install$missing"
      elif command -v pacman >/dev/null 2>&1; then echo "install with: sudo pacman -S --needed$missing"
      elif command -v zypper >/dev/null 2>&1; then echo "install with: sudo zypper install$missing"
      else echo "install jq, curl and fzf with your package manager"
      fi ;;
    *)
      echo "install jq, curl and fzf with your package manager" ;;
  esac
  exit 1
fi
if ! command -v fzf >/dev/null 2>&1; then
  echo "warning: fzf is not installed; 'fusion' works, but the interactive orc model picker needs it"
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
mkdir -p "$DEST/data"
cp "$(cd "$(dirname "$0")" && pwd)/data/quality.json" "$DEST/data/quality.json"
cp "$FUSION_SRC" "$DEST/fusion"
cp "$FUSION_CORE_SRC" "$DEST/fusion_core.py"
cp "$FUSION_WORKFLOW_SRC" "$DEST/fusion_workflow.py"
for module in fusion_decisions fusion_laya fusion_policy fusion_build fusion_decision_cli fusion_progress fusion_report fusion_ui fusion_labeling fusion_publish fusion_learning fusion_garden fusion_quality fusion_truffle fusion_truffle_survey fusion_training_loop fusion_mcp fusion_reasoning; do
  cp "$(dirname "$FUSION_CORE_SRC")/$module.py" "$DEST/$module.py"
done
mkdir -p "$DEST/fusion_ui_assets"
cp -R "$(dirname "$FUSION_CORE_SRC")/fusion_ui_assets/." "$DEST/fusion_ui_assets/"
chmod +x "$DEST/fusion"
echo "installed: $DEST/fusion"

case ":$PATH:" in
  *":$DEST:"*) ;;
  *) echo "note: $DEST is not on your PATH — add: export PATH=\"$DEST:\$PATH\"" ;;
esac

echo "next: run 'orc' to start the setup wizard or 'fusion doctor' to check the agent CLIs"
