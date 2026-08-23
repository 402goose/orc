#!/usr/bin/env bash
# shellcheck disable=SC2016
set -uo pipefail

cd "$(dirname "$0")" || exit 1
ROOT="$(cd .. && pwd)"
FIX="$PWD/fixtures"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0

strip_ansi() { sed $'s/\x1b\\[[0-9;]*m//g'; }

t() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
    printf '  ok  %s\n' "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL  %s\n  expected: %s\n  actual:   %s\n' "$name" "$expected" "$actual"
  fi
}

t_contains() {
  local name="$1" needle="$2" haystack="$3"
  case "$haystack" in
    *"$needle"*)
      PASS=$((PASS + 1))
      printf '  ok  %s\n' "$name" ;;
    *)
      FAIL=$((FAIL + 1))
      printf 'FAIL  %s\n  missing:  %s\n  in:       %s\n' "$name" "$needle" "$haystack" ;;
  esac
}

echo "== hud =="
export MODELS_CACHE="$FIX/models.json"

hud() { "$ROOT/hud.sh" | strip_ansi; }
payload() { jq -n --arg tp "$FIX/$1" "$2"; }

t "paid transcript, ctx estimated from transcript" \
  'paid│↑ 20.0k ↓ 300│cache 45%│ctx ░░░░░░░░░░░░ 7%│$0.0149' \
  "$(payload paid.jsonl '{transcript_path:$tp,model:{id:"test/paid",display_name:"paid"},context_window:null}' | hud)"

t "used_percentage wins over estimates" \
  'paid│↑ 20.0k ↓ 300│cache 45%│ctx ▓▓▓▓▓░░░░░░░ 42%│$0.0149' \
  "$(payload paid.jsonl '{transcript_path:$tp,model:{id:"test/paid",display_name:"paid"},context_window:{used_percentage:42.7,context_window_size:128000}}' | hud)"

t "current_usage object computes ctx pct" \
  'paid│↑ 20.0k ↓ 300│cache 45%│ctx ▓▓▓▓▓▓░░░░░░ 50%│$0.0149' \
  "$(payload paid.jsonl '{transcript_path:$tp,model:{id:"test/paid",display_name:"paid"},context_window:{context_window_size:128000,current_usage:{input_tokens:50000,cache_read_input_tokens:14000,cache_creation:{ephemeral_5m_input_tokens:0,ephemeral_1h_input_tokens:0}}}}' | hud)"

t "free model shows FREE" \
  'free│↑ 2.0k ↓ 100│cache 0%│ctx ░░░░░░░░░░░░ 3%│FREE' \
  "$(payload free.jsonl '{transcript_path:$tp,model:{id:"test/free",display_name:"free"},context_window:null}' | hud)"

t "unknown model: name from transcript, cost unknown, synthetic filtered" \
  'test/mystery│↑ 1.0k ↓ 10│cache 0%│$—' \
  "$(payload unknown.jsonl '{transcript_path:$tp,context_window:null}' | hud)"

t "empty stdin renders placeholder" "—" "$(printf '' | hud)"

t "missing transcript renders zeros without failing" \
  '?│↑ 0 ↓ 0│$0.0000' \
  "$(jq -n '{transcript_path:"/nonexistent/x.jsonl",context_window:null}' | hud)"

unset MODELS_CACHE

echo "== stats =="
export ORC_HOME="$TMP/stats-home"
mkdir -p "$ORC_HOME/claude-state/projects/-proj-a" "$ORC_HOME/claude-state/projects/-proj-b"
cp "$FIX/models.json" "$ORC_HOME/models.json"
cp "$FIX/paid.jsonl" "$ORC_HOME/claude-state/projects/-proj-a/s1.jsonl"
cp "$FIX/free.jsonl" "$ORC_HOME/claude-state/projects/-proj-b/s2.jsonl"
cp "$FIX/unknown.jsonl" "$ORC_HOME/claude-state/projects/-proj-b/s3.jsonl"

S="$("$ROOT/orc" stats --json 2>/dev/null)"
t "stats: message count" "4" "$(printf '%s' "$S" | jq -r .messages)"
t "stats: total input tokens" "4500" "$(printf '%s' "$S" | jq -r .total.i)"
t "stats: total output tokens" "410" "$(printf '%s' "$S" | jq -r .total.o)"
t "stats: total cache reads" "9000" "$(printf '%s' "$S" | jq -r .total.r)"
t "stats: total cache writes" "9500" "$(printf '%s' "$S" | jq -r .total.w)"
t "stats: total cost" "0.014875" "$(printf '%s' "$S" | jq -r .total.cost)"
t "stats: unpriced count" "1" "$(printf '%s' "$S" | jq -r .total.unpriced)"
t "stats: models grouped" "3" "$(printf '%s' "$S" | jq -r '.by_model | length')"
t "stats: projects grouped" "2" "$(printf '%s' "$S" | jq -r '.by_project | length')"
t "stats: days grouped" "2" "$(printf '%s' "$S" | jq -r '.by_day | length')"
t "stats: paid model cost" "0.014875" "$(printf '%s' "$S" | jq -r '.by_model[] | select(.key == "test/paid") | .cost')"
t "stats: project attribution" "1500" "$(printf '%s' "$S" | jq -r '.by_project[] | select(.key == "-proj-a") | .i')"

TABLE="$("$ROOT/orc" stats 2>/dev/null | strip_ansi)"
t_contains "stats table: header" "MODEL" "$TABLE"
t_contains "stats table: total row" "TOTAL" "$TABLE"
t_contains "stats table: paid row priced" '$0.0149' "$TABLE"
t_contains "stats table: unpriced marker" '+?' "$TABLE"

TABLE_DAY="$("$ROOT/orc" stats --by day 2>/dev/null | strip_ansi)"
t_contains "stats --by day: day key" "2026-08-20" "$TABLE_DAY"

EMPTY_HOME="$TMP/empty-home"
mkdir -p "$EMPTY_HOME"
cp "$FIX/models.json" "$EMPTY_HOME/models.json"
OUT="$(ORC_HOME="$EMPTY_HOME" "$ROOT/orc" stats 2>&1 | strip_ansi)"
t_contains "stats: no transcripts message" "no transcripts" "$OUT"

echo "== profiles + project config =="
export ORC_HOME="$TMP/prof-home"
mkdir -p "$ORC_HOME"
cp "$FIX/models.json" "$ORC_HOME/models.json"
export OPENROUTER_API_KEY="sk-or-test-dummy"

"$ROOT/orc" model --set test/paid >/dev/null 2>&1
t "model --set writes config" "test/paid" "$(jq -r .model "$ORC_HOME/config.json")"

"$ROOT/orc" model --set nope/nope >/dev/null 2>&1
t "model --set rejects unknown model" "1" "$?"

"$ROOT/orc" small --set test/free >/dev/null 2>&1
t "small --set writes config" "test/free" "$(jq -r .small_model "$ORC_HOME/config.json")"

"$ROOT/orc" save work >/dev/null 2>&1
t "save snapshots model into profile" "test/paid" "$(jq -r .profiles.work.model "$ORC_HOME/config.json")"
t "save snapshots small model into profile" "test/free" "$(jq -r .profiles.work.small_model "$ORC_HOME/config.json")"

"$ROOT/orc" save 'bad name!' >/dev/null 2>&1
t "save rejects invalid profile name" "1" "$?"

LIST="$("$ROOT/orc" profiles 2>/dev/null | strip_ansi)"
t_contains "profiles lists saved profile" "@work" "$LIST"

PROJ="$TMP/proj"
mkdir -p "$PROJ"
printf '{"model":"test/free"}\n' > "$PROJ/.orc.json"
ENVOUT="$(cd "$PROJ" && "$ROOT/orc" env 2>/dev/null)"
t_contains ".orc.json model overrides global config" 'ANTHROPIC_MODEL="test/free"' "$ENVOUT"

ENVOUT="$(cd "$PROJ" && ORC_PROFILE=work "$ROOT/orc" env 2>/dev/null)"
t_contains "ORC_PROFILE beats .orc.json" 'ANTHROPIC_MODEL="test/paid"' "$ENVOUT"

printf '{"profile":"work"}\n' > "$PROJ/.orc.json"
"$ROOT/orc" model --set test/free >/dev/null 2>&1
ENVOUT="$(cd "$PROJ" && "$ROOT/orc" env 2>/dev/null)"
t_contains ".orc.json profile reference resolves" 'ANTHROPIC_MODEL="test/paid"' "$ENVOUT"

"$ROOT/orc" profiles rm work >/dev/null 2>&1
t "profiles rm deletes profile" "null" "$(jq -r '.profiles.work' "$ORC_HOME/config.json")"

echo
if [ "$FAIL" -gt 0 ]; then
  printf '%d passed, %d FAILED\n' "$PASS" "$FAIL"
  exit 1
fi
printf 'all %d tests passed\n' "$PASS"
