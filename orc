#!/usr/bin/env bash
set -euo pipefail

ORC_HOME="${ORC_HOME:-$HOME/.config/orc}"
CONFIG="$ORC_HOME/config.json"
MODELS_CACHE="$ORC_HOME/models.json"
CLAUDE_STATE="$ORC_HOME/claude-state"
API="https://openrouter.ai/api/v1"
KEYCHAIN_SERVICE="orc-openrouter"
CACHE_TTL=86400

err()  { printf '\033[31morc: %s\033[0m\n' "$*" >&2; }
info() { printf '\033[2m%s\033[0m\n' "$*" >&2; }
bold() { printf '\033[1m%s\033[0m\n' "$*" >&2; }

need() { command -v "$1" >/dev/null 2>&1 || { err "missing dependency: $1"; exit 1; }; }
need jq; need curl

cfg() { [ -f "$CONFIG" ] && jq -r "$1 // empty" "$CONFIG" 2>/dev/null || true; }

save_cfg() {
  mkdir -p "$ORC_HOME"
  local tmp="$CONFIG.tmp"
  if [ -f "$CONFIG" ]; then
    jq --arg v "$2" ".$1 = \$v" "$CONFIG" > "$tmp"
  else
    jq -n --arg v "$2" "{$1: \$v}" > "$tmp"
  fi
  mv "$tmp" "$CONFIG"
}

key_env_name() {
  local n
  n="$(cfg .key_env)"
  if [[ "$n" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
    printf '%s' "$n"
  else
    printf 'OPENROUTER_API_KEY'
  fi
}

resolve_key() {
  local envname v
  envname="$(key_env_name)"
  v="${!envname:-}"
  if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  security find-generic-password -ws "$KEYCHAIN_SERVICE" 2>/dev/null || true
}

key_source() {
  local envname
  envname="$(key_env_name)"
  if [ -n "${!envname:-}" ]; then printf 'env $%s' "$envname"
  elif security find-generic-password -ws "$KEYCHAIN_SERVICE" >/dev/null 2>&1; then printf 'macOS keychain (%s)' "$KEYCHAIN_SERVICE"
  else printf 'none'
  fi
}

require_key() {
  local key
  key="$(resolve_key)"
  if [ -z "$key" ]; then
    err "no OpenRouter API key found"
    info "looked in: env \$$(key_env_name), then macOS keychain service '$KEYCHAIN_SERVICE'"
    info "fix: export $(key_env_name)=sk-or-... in your shell, or run: orc key"
    exit 1
  fi
  printf '%s' "$key"
}

fetch_models() {
  mkdir -p "$ORC_HOME"
  if [ -f "$MODELS_CACHE" ] && [ "${1:-}" != "force" ]; then
    local age=$(( $(date +%s) - $(stat -f %m "$MODELS_CACHE") ))
    [ "$age" -lt "$CACHE_TTL" ] && return 0
  fi
  info "fetching model list from OpenRouter..."
  if curl -sf --max-time 30 "$API/models" -o "$MODELS_CACHE.tmp"; then
    mv "$MODELS_CACHE.tmp" "$MODELS_CACHE"
  elif [ -f "$MODELS_CACHE" ]; then
    info "fetch failed; using cached list"
  else
    err "could not fetch model list from $API/models"
    exit 1
  fi
}

model_rows() {
  jq -r '
    .data
    | sort_by(.id)[]
    | [ .id,
        (if ((.pricing.prompt // "0")|tonumber) == 0 and ((.pricing.completion // "0")|tonumber) == 0
         then "free"
         else "$\((((.pricing.prompt // "0")|tonumber) * 100000000 | round) / 100)/M in  $\((((.pricing.completion // "0")|tonumber) * 100000000 | round) / 100)/M out"
         end),
        "\(((.context_length // 0) / 1000) | round)k ctx"
      ] | @tsv' "$MODELS_CACHE"
}

model_exists() { jq -e --arg id "$1" '.data[] | select(.id == $id)' "$MODELS_CACHE" >/dev/null 2>&1; }

pick_model() {
  need fzf
  fetch_models
  local sel
  sel="$(model_rows | column -t -s "$(printf '\t')" \
        | fzf --prompt="${2:-model}> " --query="${1:-}" --height=20 --reverse)" || return 1
  printf '%s' "${sel%% *}"
}

key_wizard() {
  local envname
  envname="$(key_env_name)"
  bold "orc key setup"
  info "key lookup order: env \$$envname, then macOS keychain ('$KEYCHAIN_SERVICE')"
  info "current source: $(key_source)"
  printf '  [1] change which env var orc reads\n  [2] paste a key -> store in macOS keychain\n  [Enter] keep as is\n' >&2
  printf '> ' >&2
  local ans; IFS= read -r ans
  case "$ans" in
    1)
      printf 'env var name [%s]: ' "$envname" >&2
      local n; IFS= read -r n
      if [ -n "$n" ]; then
        if [[ ! "$n" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
          err "invalid environment variable name: $n"
          return 1
        fi
        save_cfg key_env "$n"
        info "orc will now read \$$n"
        [ -z "${!n:-}" ] && info "note: \$$n is not set in this shell — add 'export $n=sk-or-...' to your ~/.zshrc"
      fi
      ;;
    2)
      printf 'paste key (input hidden): ' >&2
      local k; IFS= read -rs k; printf '\n' >&2
      if [ -n "$k" ]; then
        security add-generic-password -U -s "$KEYCHAIN_SERVICE" -a "$USER" -w "$k"
        info "stored in keychain; orc will use it whenever \$$(key_env_name) is unset"
      fi
      ;;
  esac
}

pick_mode() {
  bold "permission mode:"
  printf '  [1] default          normal permission prompting\n' >&2
  printf '  [2] auto             auto-approve safe actions\n' >&2
  printf '  [3] acceptEdits      auto-accept file edits\n' >&2
  printf '  [4] plan             read-only planning mode\n' >&2
  printf '  [5] dontAsk          never prompt (denies what would need asking)\n' >&2
  printf '  [6] yolo             bypass ALL permission checks\n' >&2
  printf '> ' >&2
  local ans; IFS= read -r ans
  local m=""
  case "$ans" in
    1) m="default" ;;
    2) m="auto" ;;
    3) m="acceptEdits" ;;
    4) m="plan" ;;
    5) m="dontAsk" ;;
    6) m="yolo" ;;
    *) return 1 ;;
  esac
  save_cfg mode "$m"
  info "mode saved: $m"
}

setup_wizard() {
  bold "orc — OpenRouter x Claude Code setup"
  if [ "$(key_source)" = "none" ]; then
    info "no API key found"
    key_wizard
    [ -z "$(resolve_key)" ] && { err "still no key; aborting"; exit 1; }
  else
    info "API key found via $(key_source)"
  fi
  local m
  if m="$(pick_model "" "pick a model")"; then
    save_cfg model "$m"
    info "model saved: $m"
  else
    err "no model selected; run 'orc setup' again"
    exit 1
  fi
  printf 'also pick a small/fast model for background tasks? [y/N] ' >&2
  local ans; IFS= read -r ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    local s
    if s="$(pick_model "" "small model")"; then
      save_cfg small_model "$s"
      info "small model saved: $s"
    fi
  fi
  printf 'pick a launch permission mode? [y/N] ' >&2
  local mans; IFS= read -r mans
  if [ "$mans" = "y" ] || [ "$mans" = "Y" ]; then pick_mode || true; fi
  info "setup complete — config: $CONFIG"
}

confirm_or_change() {
  [ -t 0 ] || return 0
  [ "${ORC_YES:-}" = "1" ] && return 0
  while true; do
    local mode; mode="$(cfg .mode)"; mode="${mode:-default}"
    bold "orc → $(cfg .model) · mode: $mode"
    printf '  [Enter] launch   [m] model   [p] permission mode   [k] key   [q] quit\n' >&2
    local ans; IFS= read -rsn1 ans || true
    case "$ans" in
      m|M)
        local mm
        if mm="$(pick_model "" "model")"; then
          save_cfg model "$mm"
          info "model saved: $mm"
        fi
        ;;
      p|P) pick_mode || true ;;
      k|K) key_wizard ;;
      q|Q) exit 0 ;;
      *) return 0 ;;
    esac
  done
}

launch() {
  local key model small
  key="$(require_key)"
  model="${ORC_MODEL_OVERRIDE:-$(cfg .model)}"
  [ -z "$model" ] && { err "no model configured — run: orc setup"; exit 1; }
  small="$(cfg .small_model)"; small="${small:-$model}"
  mkdir -p "$CLAUDE_STATE"
  local ctx=""
  if [ -f "$MODELS_CACHE" ]; then
    ctx="$(jq -r --arg id "$model" '.data[] | select(.id == $id) | .context_length // empty' "$MODELS_CACHE" 2>/dev/null || true)"
  fi
  local envargs=( ANTHROPIC_API_KEY=""
    ANTHROPIC_BASE_URL="https://openrouter.ai/api"
    ANTHROPIC_AUTH_TOKEN="$key"
    ANTHROPIC_MODEL="$model"
    ANTHROPIC_SMALL_FAST_MODEL="$small"
    ANTHROPIC_DEFAULT_HAIKU_MODEL="$small"
    CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
    CLAUDE_CONFIG_DIR="$CLAUDE_STATE" )
  [ -n "$ctx" ] && envargs+=( CLAUDE_CODE_MAX_CONTEXT_TOKENS="$ctx" )
  local forced
  forced="$(jq -nc --arg burl "https://openrouter.ai/api" --arg model "$model" --arg small "$small" --arg ctx "$ctx" \
    '{env: ({ANTHROPIC_BASE_URL: $burl, ANTHROPIC_API_KEY: "", ANTHROPIC_MODEL: $model, ANTHROPIC_SMALL_FAST_MODEL: $small, ANTHROPIC_DEFAULT_HAIKU_MODEL: $small}
      + (if $ctx != "" then {CLAUDE_CODE_MAX_CONTEXT_TOKENS: $ctx} else {} end))}')"
  local mode; mode="$(cfg .mode)"
  local modeargs=()
  case "$mode" in
    ""|default) ;;
    yolo|bypassPermissions) modeargs+=( --dangerously-skip-permissions ) ;;
    *) modeargs+=( --permission-mode "$mode" ) ;;
  esac
  info "launching claude via OpenRouter — model: $model · mode: ${mode:-default}${ctx:+ · ctx: $ctx}"
  exec env "${envargs[@]}" claude --settings "$forced" ${modeargs[@]+"${modeargs[@]}"} "$@"
}

doctor() {
  bold "orc doctor"
  local ok=0
  if command -v claude >/dev/null 2>&1; then
    printf '  ✓ claude: %s (%s)\n' "$(command -v claude)" "$(claude --version 2>/dev/null | head -1)"
  else
    printf '  ✗ claude not found on PATH\n'; ok=1
  fi
  for dep in jq curl fzf; do
    if command -v "$dep" >/dev/null 2>&1; then printf '  ✓ %s\n' "$dep"; else printf '  ✗ %s missing\n' "$dep"; ok=1; fi
  done
  local src; src="$(key_source)"
  if [ "$src" = "none" ]; then
    printf '  ✗ API key: not found (env $%s or keychain) — run: orc key\n' "$(key_env_name)"; ok=1
  else
    printf '  ✓ API key: %s\n' "$src"
    local resp
    if resp="$(curl -sf --max-time 15 -H "Authorization: Bearer $(resolve_key)" "$API/key" 2>/dev/null)"; then
      printf '  ✓ key valid — label: %s, usage: $%s\n' \
        "$(printf '%s' "$resp" | jq -r '.data.label // "?"')" \
        "$(printf '%s' "$resp" | jq -r '.data.usage // 0')"
    else
      printf '  ✗ key rejected by OpenRouter (%s/key)\n' "$API"; ok=1
    fi
  fi
  local model; model="$(cfg .model)"
  if [ -z "$model" ]; then
    printf '  ✗ no model configured — run: orc setup\n'; ok=1
  else
    fetch_models
    if model_exists "$model"; then
      printf '  ✓ model: %s\n' "$model"
    else
      printf '  ! model %s not in current OpenRouter list (may be delisted) — run: orc model\n' "$model"
    fi
  fi
  [ -n "$(cfg .small_model)" ] && printf '  ✓ small model: %s\n' "$(cfg .small_model)"
  printf '  ✓ claude state dir: %s (isolated from ~/.claude)\n' "$CLAUDE_STATE"
  exit "$ok"
}

usage() {
  cat >&2 <<'EOF'
orc — run Claude Code against OpenRouter models

usage:
  orc                     launch (first run starts the setup wizard)
  orc [claude args...]    launch and pass args through to claude (e.g. orc -c, orc -p "...")
  orc -m <model> [...]    one-off model override (not saved)
  orc setup               re-run the setup wizard (key + model)
  orc model [query]       pick + save a new default model (fzf)
  orc mode                pick + save the launch permission mode
  orc small [query]       pick + save the small/fast background model
  orc models [query]      list models with pricing + context
  orc key                 configure key source (env var name or keychain)
  orc env                 print the export lines orc uses (contains your key)
  orc refresh             force-refresh the cached model list
  orc doctor              check everything end to end
  orc config              open config in $EDITOR

env:
  ORC_YES=1               skip the launch confirmation prompt
  ORC_HOME                config dir (default ~/.config/orc)
EOF
}

case "${1:-}" in
  help|-h|--help) usage; exit 0 ;;
  setup) setup_wizard; exit 0 ;;
  key) key_wizard; exit 0 ;;
  doctor) doctor ;;
  refresh) fetch_models force; info "model list refreshed"; exit 0 ;;
  config) mkdir -p "$ORC_HOME"; [ -f "$CONFIG" ] || printf '{}\n' > "$CONFIG"; exec "${EDITOR:-vi}" "$CONFIG" ;;
  model)
    shift
    if m="$(pick_model "${1:-}" "model")"; then save_cfg model "$m"; info "model saved: $m"; fi
    exit 0 ;;
  mode) pick_mode || info "mode unchanged"; exit 0 ;;
  small)
    shift
    if s="$(pick_model "${1:-}" "small model")"; then save_cfg small_model "$s"; info "small model saved: $s"; fi
    exit 0 ;;
  models)
    shift
    fetch_models
    if [ -n "${1:-}" ]; then model_rows | grep -i -- "$1" | column -t -s "$(printf '\t')"
    else model_rows | column -t -s "$(printf '\t')"
    fi
    exit 0 ;;
  env)
    key="$(require_key)"
    model="$(cfg .model)"; small="$(cfg .small_model)"; small="${small:-$model}"
    printf 'export ANTHROPIC_BASE_URL="https://openrouter.ai/api"\n'
    printf 'export ANTHROPIC_AUTH_TOKEN="%s"\n' "$key"
    printf 'export ANTHROPIC_MODEL="%s"\n' "$model"
    printf 'export ANTHROPIC_SMALL_FAST_MODEL="%s"\n' "$small"
    printf 'export CLAUDE_CONFIG_DIR="%s"\n' "$CLAUDE_STATE"
    printf 'export ANTHROPIC_API_KEY=""\n'
    exit 0 ;;
  -m)
    shift
    [ -z "${1:-}" ] && { err "-m requires a model id"; exit 1; }
    ORC_MODEL_OVERRIDE="$1"; shift
    launch "$@" ;;
  "")
    if [ -z "$(cfg .model)" ]; then setup_wizard; fi
    confirm_or_change
    launch ;;
  *)
    launch "$@" ;;
esac
