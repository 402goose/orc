#!/usr/bin/env bash
set -euo pipefail
export FUSION_DECISIONS_MODE=off

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
WORKSPACE="$TMP/workspace"
BIN="$TMP/bin"
mkdir -p "$WORKSPACE" "$BIN"
printf '%s\n' '# dogfood fixture' > "$WORKSPACE/README.md"

cat > "$BIN/claude" <<'PY'
#!/usr/bin/env python3
import json, pathlib, sys
prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
if "FINAL_HANDOFF.md" in prompt:
    pathlib.Path("FINAL_HANDOFF.md").write_text("dogfood handoff\\n", encoding="utf-8")
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "dogfood-claude",
    "result": "STATUS: success\nSUMMARY: Claude stage completed\nCHANGED: none\nTESTS: none\nBLOCKERS: none",
}))
PY

cat > "$BIN/codex" <<'PY'
#!/usr/bin/env python3
import json
print(json.dumps({"type": "thread.started", "thread_id": "dogfood-codex"}))
print(json.dumps({
    "type": "item.completed",
    "item": {
        "type": "agent_message",
        "text": "STATUS: success\nSUMMARY: Codex stage completed\nCHANGED: src/app.py\nTESTS: python -m unittest\nBLOCKERS: none",
    },
}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 8}}))
PY
cat > "$BIN/agy" <<'PY'
#!/usr/bin/env python3
import json
print(json.dumps({
    "conversation_id": "dogfood-agy",
    "status": "SUCCESS",
    "response": "STATUS: success\nSUMMARY: Antigravity stage completed\nCHANGED: none\nTESTS: none\nBLOCKERS: none",
    "usage": {"input_tokens": 7, "output_tokens": 3},
}))
PY
chmod +x "$BIN/claude" "$BIN/codex" "$BIN/agy"

cat > "$WORKSPACE/.fusion.json" <<JSON
{
  "claude": {"command": "$BIN/claude"},
  "codex": {"command": "$BIN/codex"},
  "agy": {"command": "$BIN/agy"},
  "routes": {
    "fixture-claude": {"agent": "claude", "command": "$BIN/claude"}
  },
  "ultra": {
    "max_stages": 3,
    "stages": {
      "explore": {"agent": "claude", "route": "fixture-claude", "write": false},
      "implement": {"agent": "codex", "write": true},
      "review": {"agent": "claude", "route": "fixture-claude", "write": false}
    }
  }
}
JSON

RESULT="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" --json ultra 'dogfood the bounded pipeline')"
printf '%s\n' "$RESULT" | jq -e '
  .schema == "fusion.ultra.v1"
  and .status == "success"
  and (.stages | length) == 3
  and .stages[0].result.agent == "claude"
  and .stages[1].result.agent == "codex"
  and (.stages[1].result.changed | index("src/app.py")) != null
  and (.artifacts.manifest | type) == "string"
' >/dev/null

STATUS="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" --json status --limit 10)"
printf '%s\n' "$STATUS" | jq -e 'length == 3 and all(.[]; .result.status == "success")' >/dev/null

TRACE="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" trace --limit 10)"
printf '%s\n' "$TRACE" | jq -e 'length == 3 and all(.[]; .schema == "fusion.trace.v1")' >/dev/null

USAGE="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" usage --limit 10)"
printf '%s\n' "$USAGE" | jq -e '.spans == 3 and .total.input_tokens == 12 and .total.output_tokens == 8' >/dev/null

cat > "$WORKSPACE/workflow.json" <<'JSON'
{
  "task": "dogfood the persisted fan-out workflow",
  "max_parallel": 2,
  "max_attempts": 1,
  "nodes": [
    {
      "id": "inventory",
      "items": ["one", "two", "three"],
      "task_template": "Inventory {item}",
      "agent": "claude",
      "role": "investigator"
    },
    {
      "id": "verify",
      "needs": ["inventory"],
      "task": "Verify the inventory",
      "agent": "codex",
      "route": "codex-read",
      "role": "verifier"
    },
    {
      "id": "agycheck",
      "needs": ["inventory"],
      "task": "Countercheck the inventory from a second model family",
      "agent": "agy",
      "role": "countercheck"
    },
    {
      "id": "synthesize",
      "needs": ["verify", "agycheck"],
      "task": "Write FINAL_HANDOFF.md from the verified evidence",
      "agent": "claude",
      "role": "synthesizer",
      "write": true,
      "required_files": ["FINAL_HANDOFF.md"]
    }
  ],
  "acceptance": {
    "required_files": ["FINAL_HANDOFF.md"],
    "required_nodes": ["synthesize"]
  }
}
JSON

WORKFLOW_RESULT="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" --json workflow run "$WORKSPACE/workflow.json")"
printf '%s\n' "$WORKFLOW_RESULT" | jq -e '
  .schema == "fusion.workflow.v1"
  and .status == "success"
  and (.nodes | length) == 6
  and all(.nodes[]; .status == "success")
  and any(.nodes[]; .agent == "agy")
  and (.acceptance.ok == true)
' >/dev/null
printf '%s\n' "$WORKFLOW_RESULT" | jq -r '.artifacts.manifest' | xargs test -f
test -f "$WORKSPACE/FINAL_HANDOFF.md"

AGY_DELEGATE="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" --json delegate --agent agy --read-only --fresh --role countercheck 'Countercheck the inventory independently')"
printf '%s\n' "$AGY_DELEGATE" | jq -e '
  .status == "success"
  and .agent == "agy"
  and .summary == "Antigravity stage completed"
  and .usage.input_tokens == 7
' >/dev/null

printf 'fusion dogfood passed: claude/codex/agy subprocesses, handoffs, traces, usage, ledger, and fan-out workflow\n'
