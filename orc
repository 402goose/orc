#!/usr/bin/env bash
set -euo pipefail

ORC_HOME="${ORC_HOME:-$HOME/.config/orc}"
CONFIG="$ORC_HOME/config.json"
MODELS_CACHE="$ORC_HOME/models.json"
CLAUDE_STATE="$ORC_HOME/claude-state"
API="https://openrouter.ai/api/v1"
KEYCHAIN_SERVICE="orc-openrouter"
CACHE_TTL=86400
HUD_SCRIPT="$ORC_HOME/hud.sh"
ORC_HUD_VERSION="4"

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
  local filter="${1:-all}"
  jq -r --arg filter "$filter" '
    def price($name): ((.pricing[$name] // "0") | tonumber);
    def is_free:
      price("prompt") == 0
      and price("completion") == 0
      and price("request") == 0
      and price("image") == 0
      and price("web_search") == 0
      and price("internal_reasoning") == 0;
    .data
    | sort_by(.id)[]
    | select($filter != "free" or is_free)
    | [ .id,
        (if is_free
         then "FREE"
         else "$\((((.pricing.prompt // "0")|tonumber) * 100000000 | round) / 100)/M in  $\((((.pricing.completion // "0")|tonumber) * 100000000 | round) / 100)/M out"
         end),
        "\(((.context_length // 0) / 1000) | round)k ctx"
      ] | @tsv' "$MODELS_CACHE"
}

model_exists() { jq -e --arg id "$1" '.data[] | select(.id == $id)' "$MODELS_CACHE" >/dev/null 2>&1; }

pick_model() {
  need fzf
  fetch_models
  local sel filter header
  filter="${3:-all}"
  if [ "$filter" = "free" ]; then
    header="currently free on OpenRouter · live catalog"
  else
    header="live OpenRouter price per 1M tokens · type FREE for free models"
  fi
  sel="$(model_rows "$filter" | column -t -s "$(printf '\t')" \
        | fzf --prompt="${2:-model}> " --query="${1:-}" --header="$header" --height=20 --reverse)" || return 1
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

orc_mode() {
  local m="${ORC_MODE:-$(cfg .mode)}"
  case "$m" in
    ""|default|auto|acceptEdits|plan|dontAsk|yolo|bypassPermissions) printf '%s' "$m" ;;
    *) err "invalid ORC_MODE: $m"; return 1 ;;
  esac
}

hud_enabled() {
  [ "${ORC_HUD:-}" = "0" ] && return 1
  [ "${ORC_HUD:-}" = "1" ] && return 0
  [ "$(cfg .hud)" = "off" ] && return 1
  return 0
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
    printf '  [Enter] launch   [m] model   [f] free model   [p] permission mode   [k] key   [q] quit\n' >&2
    local ans; IFS= read -rsn1 ans || true
    case "$ans" in
      m|M)
        local mm
        if mm="$(pick_model "" "model")"; then
          save_cfg model "$mm"
          info "model saved: $mm"
        fi
        ;;
      f|F)
        local fm
        if fm="$(pick_model "" "free model" "free")"; then
          save_cfg model "$fm"
          info "model saved: $fm"
        fi
        ;;
      p|P) pick_mode || true ;;
      k|K) key_wizard ;;
      q|Q) exit 0 ;;
      *) return 0 ;;
    esac
  done
}

install_hud() {
  local cur=""
  [ -f "$HUD_SCRIPT" ] && cur="$(sed -n 's/^# ORC_HUD_VERSION=//p' "$HUD_SCRIPT" 2>/dev/null | head -1)"
  [ "$cur" = "$ORC_HUD_VERSION" ] && return 0
  mkdir -p "$ORC_HOME" || return 1
  {
    printf '#!/usr/bin/env bash\n'
    printf '# ORC_HUD_VERSION=%s\n' "$ORC_HUD_VERSION"
    printf '# generated by orc — edit the launcher, not this file\n'
    cat <<EOF
ORC_HOME="$ORC_HOME"
MODELS_CACHE="$MODELS_CACHE"
CLAUDE_STATE="$CLAUDE_STATE"
EOF
    cat <<'ORC_HUD_BODY'

payload="$(cat 2>/dev/null || true)"
DIM=$'\033[2m'; RST=$'\033[0m'; BOLD=$'\033[1m'
GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'

if [ -z "$payload" ]; then
  printf '%s—%s\n' "$DIM" "$RST"
  exit 0
fi

eval "$(printf '%s' "$payload" | jq -r '
  "P_TX="    + (@sh "\(.transcript_path // "")"),
  "P_MID="   + (@sh "\(.model.id // "")"),
  "P_MNAME=" + (@sh "\(.model.display_name // "")"),
  "P_UPCT="  + (@sh "\(.context_window.used_percentage // "")"),
  "P_CSIZE=" + (@sh "\(.context_window.context_window_size // "")"),
  "P_CU="    + (@sh "\(.context_window.current_usage // null | tostring)")
' 2>/dev/null)" || true
: "${P_TX:=}" "${P_MID:=}" "${P_MNAME:=}" "${P_UPCT:=}" "${P_CSIZE:=}"
[ -z "${P_CU:-}" ] && P_CU="null"

P="{}"
if [ -f "$MODELS_CACHE" ]; then
  P="$(jq -c '.data
        | map({key:.id,
               value:{ctx:(.context_length // 0),
                      p:.pricing.prompt, c:.pricing.completion,
                      cr:.pricing.input_cache_read,
                      w5:.pricing.input_cache_write,
                      w1h:.pricing.input_cache_write_1h}})
        | from_entries' "$MODELS_CACHE" 2>/dev/null || true)"
  [ -z "${P:-}" ] && P="{}"
fi

Z='{"i":0,"o":0,"r":0,"w5":0,"w1":0,"cost":0,"unknown":false,"last_model":"","ctx_est":0}'
A="$Z"
if [ -n "$P_TX" ] && [ -f "$P_TX" ]; then
  A="$(jq -nc --argjson P "$P" '
    def n2($x):
      if $x == null then 0
      elif ($x|type)=="number" then $x
      elif ($x|type)=="string" then (($x|tonumber?) // 0)
      else 0 end;
    reduce inputs as $r ({};
      (((($r|type)=="object")
        and (($r.message // null)|type=="object")
        and (($r.message.id // null)|type=="string")
        and (($r.message.usage // null)|type=="object")
        and (($r.message.model // null)|type=="string")
        and (($r.message.model|startswith("<"))|not)) as $ok
      | if $ok
        then .[$r.message.id] = {m:$r.message.model, u:$r.message.usage}
           | ._last = {m:$r.message.model, u:$r.message.usage}
        else . end))
    | . as $all
    | [($all | to_entries[] | select(.key != "_last") | .value)]
    | map(. as $row | {
        m:  $row.m,
        i:  n2($row.u.input_tokens),
        o:  n2($row.u.output_tokens),
        r:  n2($row.u.cache_read_input_tokens),
        w5: (if (($row.u.cache_creation // null)|type)=="object"
             then n2($row.u.cache_creation.ephemeral_5m_input_tokens)
             else n2($row.u.cache_creation_input_tokens) end),
        w1: (if (($row.u.cache_creation // null)|type)=="object"
             then n2($row.u.cache_creation.ephemeral_1h_input_tokens)
             else 0 end)})
    | reduce .[] as $x (
        {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false};
        (.i += $x.i) | (.o += $x.o) | (.r += $x.r)
        | (.w5 += $x.w5) | (.w1 += $x.w1)
        | ($P[$x.m] // null) as $pr
        | if ($pr|type)=="object"
          then .cost += (($x.i*n2($pr.p)) + ($x.o*n2($pr.c)) + ($x.r*n2($pr.cr))
                       + ($x.w5*n2($pr.w5)) + ($x.w1*n2($pr.w1h)))
          else (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
          end)
    | . + {
        last_model: ($all._last.m // ""),
        ctx_est: (if ($all._last.u // null) == null then 0 else
          ((n2($all._last.u.input_tokens) + n2($all._last.u.cache_read_input_tokens))
           + (if (($all._last.u.cache_creation // null)|type)=="object"
              then n2($all._last.u.cache_creation.ephemeral_5m_input_tokens)
                 + n2($all._last.u.cache_creation.ephemeral_1h_input_tokens)
              else n2($all._last.u.cache_creation_input_tokens) end)) end)}
  ' "$P_TX" 2>/dev/null)" || true
  [ -z "$A" ] && A="$Z"
fi

jq -rn --argjson A "$A" --argjson P "$P" --argjson cu "$P_CU" \
   --arg mid "$P_MID" --arg mname "$P_MNAME" \
   --arg upct "$P_UPCT" --arg csize "$P_CSIZE" '
  def n2($x):
    if $x == null then 0
    elif ($x|type)=="number" then $x
    elif ($x|type)=="string" then (($x|tonumber?) // 0)
    else 0 end;
  def num($s): if ($s|type)=="string" and ($s|length)>0 then (($s|tonumber?) // -1) else -1 end;
  def pct($t;$d): if $t >= 0 and $d > 0 then ((($t/$d)*100)|floor) else -1 end;
  $A as $a
  | (if $mname != "" then $mname elif $mid != "" then $mid
     elif $a.last_model != "" then $a.last_model else "?" end) as $name
  | (if ($a.unknown|not) and $a.cost == 0
       and (($a.i+$a.o+$a.r+$a.w5+$a.w1) > 0)
     then 1 else 0 end) as $free
  | (num($upct)|if . >= 0 then floor else -1 end) as $p1
  | (if ($cu|type)=="object"
     then ((n2($cu.input_tokens) + n2($cu.cache_read_input_tokens))
          + (if ($cu.cache_creation|type)=="object"
             then n2($cu.cache_creation.ephemeral_5m_input_tokens)
                + n2($cu.cache_creation.ephemeral_1h_input_tokens)
             else n2($cu.cache_creation_input_tokens) end))
     else -1 end) as $cutok
  | num($csize) as $cs
  | n2(($P[$a.last_model] // {}) | (.ctx // 0)) as $mc
  | (if $cs > $mc then $cs else $mc end) as $den
  | (if $p1 >= 0 then $p1 else pct($cutok;$cs) end) as $p2
  | (if $p2 >= 0 then $p2 else pct($a.ctx_est;$den) end) as $p3
  | (if $p3 > 100 then 100 elif $p3 < 0 then -1 else $p3 end) as $ctxp
  | (($a.i+$a.r+$a.w5+$a.w1)|round) as $tin
  | (($a.i+$a.r+$a.w5+$a.w1)) as $cden
  | (if $cden > 0 then ((($a.r/$cden)*100)|round) else -1 end) as $cachep
  | (($a.cost*10000)|round) as $c4
  | [$name, $tin, ($a.o|round), $cachep, $ctxp, $c4,
     (if $a.unknown then "u" elif $free == 1 then "f" else "n" end)]
  | map(tostring) | join("\u001f")
' 2>/dev/null | {
  IFS=$'\037' read -r R_NAME R_IN R_OUT R_CACHE R_CTX R_COST R_FLAGS || true
  human() {
    case "${1:-}" in ''|*[!0-9]*) printf '?'; return ;; esac
    local t="$1"
    if [ "$t" -ge 1000000 ]; then
      printf '%d.%02dM' $(( t / 1000000 )) $(( (t % 1000000) / 10000 ))
    elif [ "$t" -ge 1000 ]; then
      printf '%d.%dk' $(( t / 1000 )) $(( (t % 1000) / 100 ))
    else
      printf '%d' "$t"
    fi
  }
  bar() {
    local p="$1" fill="" rem="" i fn
    if [ "$p" -lt 0 ] 2>/dev/null; then printf '░░░░░░░░░░░░'; return; fi
    fn=$(( p * 12 / 100 ))
    [ "$fn" -gt 12 ] && fn=12
    for ((i=0; i<fn; i++)); do fill+='▓'; done
    for ((i=fn; i<12; i++)); do rem+='░'; done
    printf '%s%s' "$fill" "$rem"
  }
  segs=()
  segs+=("${BOLD}${R_NAME:-?}${RST}")
  segs+=("↑ $(human "${R_IN:-0}") ↓ $(human "${R_OUT:-0}")")
  if [ "${R_CACHE:--1}" -ge 0 ] 2>/dev/null; then
    segs+=("cache ${R_CACHE}%")
  fi
  if [ "${R_CTX:--1}" -ge 0 ] 2>/dev/null; then
    c=$GRN
    [ "$R_CTX" -ge 60 ] && c=$YLW
    [ "$R_CTX" -ge 85 ] && c=$RED
    segs+=("ctx ${c}$(bar "$R_CTX")${RST} ${DIM}${R_CTX}%${RST}")
  fi
  case "${R_FLAGS:-n}" in
    u) segs+=("${DIM}\$—${RST}") ;;
    f) segs+=("${GRN}FREE${RST}") ;;
    *)
      c4="${R_COST:-0}"
      case "$c4" in ''|*[!0-9]*) c4=0 ;; esac
      segs+=("${BOLD}$(printf '$%d.%04d' $(( c4 / 10000 )) $(( c4 % 10000 )))${RST}") ;;
  esac
  line=""
  sep="${DIM}│${RST}"
  for s in "${segs[@]}"; do
    [ -n "$line" ] && line+="$sep"
    line+="$s"
  done
  printf '%s\n' "$line"
}
exit 0
ORC_HUD_BODY
  } > "$HUD_SCRIPT.tmp" || { rm -f "$HUD_SCRIPT.tmp"; return 1; }
  chmod 755 "$HUD_SCRIPT.tmp" || { rm -f "$HUD_SCRIPT.tmp"; return 1; }
  mv "$HUD_SCRIPT.tmp" "$HUD_SCRIPT" || return 1
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
  local forced sl_args=( --arg sl "" )
  if hud_enabled; then
    if install_hud; then
      sl_args=( --arg sl "\"$HUD_SCRIPT\"" )
    else
      info "hud unavailable — launching without statusline"
    fi
  fi
  forced="$(jq -nc "${sl_args[@]}" --arg burl "https://openrouter.ai/api" --arg model "$model" --arg small "$small" --arg ctx "$ctx" \
    '{env: ({ANTHROPIC_BASE_URL: $burl, ANTHROPIC_API_KEY: "", ANTHROPIC_MODEL: $model, ANTHROPIC_SMALL_FAST_MODEL: $small, ANTHROPIC_DEFAULT_HAIKU_MODEL: $small}
      + (if $ctx != "" then {CLAUDE_CODE_MAX_CONTEXT_TOKENS: $ctx} else {} end))}
     + (if $sl != "" then {statusLine:{type:"command",command:$sl,padding:0}} else {} end)')"
  local mode; mode="$(orc_mode)" || exit 1
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
  orc free [query]        pick + save a currently free model (fzf)
  orc mode                pick + save the launch permission mode
  orc small [query]       pick + save the small/fast background model
  orc models [query]      list models with pricing + context
  orc models --free [...] list only currently free models
  orc hud [on|off|demo]   toggle the statusline HUD / preview it on newest transcript
  orc key                 configure key source (env var name or keychain)
  orc env                 print the export lines orc uses (contains your key)
  orc refresh             force-refresh the cached model list
  orc doctor              check everything end to end
  orc config              open config in $EDITOR

env:
  ORC_YES=1               skip the launch confirmation prompt
  ORC_HOME                config dir (default ~/.config/orc)
  ORC_MODE                per-launch permission-mode override (does not change saved config)
  ORC_HUD=0               disable the statusline HUD for one launch
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
  free)
    shift
    if m="$(pick_model "${1:-}" "free model" "free")"; then save_cfg model "$m"; info "model saved: $m"; fi
    exit 0 ;;
  mode) pick_mode || info "mode unchanged"; exit 0 ;;
  hud)
    case "${2:-}" in
      on) save_cfg hud on; info "hud on" ;;
      off) save_cfg hud off; info "hud off" ;;
      demo)
        install_hud || { err "could not generate $HUD_SCRIPT"; exit 1; }
        tp="$(ls -t "$CLAUDE_STATE"/projects/*/*.jsonl 2>/dev/null | head -1 || true)"
        [ -z "$tp" ] && { err "no transcripts found under $CLAUDE_STATE/projects"; exit 1; }
        info "demo — rendering newest transcript: $tp"
        jq -nc --arg tp "$tp" --arg cwd "$PWD" \
          '{session_id:"demo",transcript_path:$tp,
            model:{id:"demo-model",display_name:"(demo)"},
            workspace:{current_dir:$cwd},context_window:null}' \
          | "$HUD_SCRIPT"
        ;;
      "") if hud_enabled; then bold "hud: on"; else bold "hud: off"; fi
          info "usage: orc hud [on|off|demo]" ;;
      *) err "unknown option: hud ${2:-}"; info "usage: orc hud [on|off|demo]"; exit 1 ;;
    esac
    exit 0 ;;
  small)
    shift
    if s="$(pick_model "${1:-}" "small model")"; then save_cfg small_model "$s"; info "small model saved: $s"; fi
    exit 0 ;;
  models)
    shift
    fetch_models
    model_filter="all"
    if [ "${1:-}" = "--free" ] || [ "${1:-}" = "free" ]; then
      model_filter="free"
      shift
    fi
    if [ -n "${1:-}" ]; then model_rows "$model_filter" | grep -i -- "$1" | column -t -s "$(printf '\t')"
    else model_rows "$model_filter" | column -t -s "$(printf '\t')"
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
