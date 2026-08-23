#!/usr/bin/env bash
ORC_HOME="${ORC_HOME:-$HOME/.config/orc}"
MODELS_CACHE="${MODELS_CACHE:-$ORC_HOME/models.json}"

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
  P="$(jq -c '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;
    catalog_map' "$MODELS_CACHE" 2>/dev/null || true)"
  [ -z "${P:-}" ] && P="{}"
fi

Z='{"i":0,"o":0,"r":0,"w5":0,"w1":0,"cost":0,"unknown":false,"last_model":"","ctx_est":0}'
A="$Z"
if [ -n "$P_TX" ] && [ -f "$P_TX" ]; then
  A="$(jq -nc --argjson P "$P" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;
    reduce inputs as $r ({};
      ($r|response_ok) as $ok
      | if $ok
        then .[$r.message.id] = {m:$r.message.model, u:$r.message.usage}
           | ._last = {m:$r.message.model, u:$r.message.usage}
        else . end)
    | . as $all
    | [($all | to_entries[] | select(.key != "_last") | .value)]
    | map(usage_row)
    | reduce .[] as $x (
        {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false};
        (.i += $x.i) | (.o += $x.o) | (.r += $x.r)
        | (.w5 += $x.w5) | (.w1 += $x.w1)
        | ($x|row_cost($P)) as $c
        | if $c == null
          then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
          else .cost += $c
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
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;
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
