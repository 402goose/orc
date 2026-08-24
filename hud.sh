#!/usr/bin/env bash
ORC_HOME="${ORC_HOME:-$HOME/.config/orc}"
MODELS_CACHE="${MODELS_CACHE:-$ORC_HOME/models.json}"
SESSION_DIR="${ORC_SESSION_CACHE:-$ORC_HOME/sessions}"
LAST_LAUNCH="${ORC_LAST_LAUNCH:-$ORC_HOME/last-launch}"

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
# Constraint: no single quotes here (consumer programs are bash single-quoted).

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

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    catalog_map' "$MODELS_CACHE" 2>/dev/null || true)"
  [ -z "${P:-}" ] && P="{}"
fi

Z='{"i":0,"o":0,"r":0,"w5":0,"w1":0,"cost":0,"unknown":false,"msgs":0}'
EMPTY_ST='{"size":0,"off":0,"ids":{},"agg":'"$Z"',"last":null}'

file_bytes() { wc -c < "$1" 2>/dev/null | tr -d ' \t\n'; }

complete_chunk() {
  local src="$1" dest="$2" hex
  if [ ! -s "$src" ]; then
    : > "$dest"
    printf '0'
    return 0
  fi
  hex="$(tail -c 1 "$src" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  if [ "$hex" = "0a" ]; then
    cp "$src" "$dest" 2>/dev/null || : > "$dest"
  else
    sed '$d' "$src" > "$dest" 2>/dev/null || : > "$dest"
  fi
  file_bytes "$dest"
}

ingest_file() {
  local path="$1" state="$2" tmp chunk complete consumed size off newstate
  if [ ! -f "$path" ]; then
    printf '%s' "$EMPTY_ST"
    return 0
  fi
  size="$(file_bytes "$path")"
  [ -z "$size" ] && size=0
  state="$(printf '%s' "$state" | jq -c --argjson z "$Z" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

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

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    if type == "object" then
      . + {size:(.size // 0), off:(.off // 0),
           ids:(.ids // {}), agg:(.agg // empty_agg), last:(.last // null)}
    else empty_file_state end' 2>/dev/null)" || state=""
  [ -z "$state" ] && state="$EMPTY_ST"
  off="$(printf '%s' "$state" | jq -r '.off // 0')"
  case "$off" in ''|*[!0-9]*) off=0 ;; esac
  if [ "$size" -lt "$off" ]; then
    state="$EMPTY_ST"
    off=0
  fi
  tmp="$(mktemp "${TMPDIR:-/tmp}/orc-hud.XXXXXX")" || {
    printf '%s' "$state"
    return 0
  }
  chunk="$tmp.chunk"
  complete="$tmp.ok"
  if [ "$size" -ne "$(printf '%s' "$state" | jq -r '.size // 0')" ] || [ "$size" -ne "$off" ]; then
    if [ "$off" -eq 0 ]; then
      cp "$path" "$chunk" 2>/dev/null || : > "$chunk"
    else
      tail -c +"$((off + 1))" "$path" > "$chunk" 2>/dev/null || : > "$chunk"
    fi
    consumed="$(complete_chunk "$chunk" "$complete")"
    case "$consumed" in ''|*[!0-9]*) consumed=0 ;; esac
    if [ "$consumed" -gt 0 ]; then
      newstate="$(jq -nc --argjson st "$state" --argjson P "$P" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

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

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
        $st | ingest_lines($P)
      ' "$complete" 2>/dev/null)" || newstate=""
      if [ -n "$newstate" ]; then
        state="$(printf '%s' "$newstate" | jq -c --argjson size "$size" --argjson off "$((off + consumed))" '
          .size = $size | .off = $off' 2>/dev/null || printf '%s' "$newstate")"
      fi
    else
      state="$(printf '%s' "$state" | jq -c --argjson size "$size" '
        .size = $size' 2>/dev/null || printf '%s' "$state")"
    fi
  fi
  state="$(printf '%s' "$state" | jq -c --argjson P "$P" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

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

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    reprice($P)
  ' 2>/dev/null || printf '%s' "$state")"
  rm -f "$tmp" "$chunk" "$complete"
  printf '%s' "$state"
}

A="$Z"
LAST_U="null"
LAST_M=""
AGENTS_COST="0"
AGENTS_ON=0
SESSION="$Z"
HAVE_SESSION=0

if [ -n "$P_TX" ] && [ -f "$P_TX" ]; then
  sid="$(basename "$P_TX" .jsonl)"
  cache_path="$SESSION_DIR/$sid.json"
  cache="{}"
  [ -f "$cache_path" ] && cache="$(cat "$cache_path" 2>/dev/null || printf '{}')"
  main_st="$(printf '%s' "$cache" | jq -c '.main // {}' 2>/dev/null || printf '{}')"
  main_st="$(ingest_file "$P_TX" "$main_st")"
  agent_map="$(printf '%s' "$cache" | jq -c '.agents // {}' 2>/dev/null || printf '{}')"
  new_agents="{}"
  agent_dir="${P_TX%.jsonl}/subagents"
  if [ -d "$agent_dir" ]; then
    while IFS= read -r -d '' af; do
      an="$(basename "$af")"
      ast="$(printf '%s' "$agent_map" | jq -c --arg n "$an" '.[$n] // {}' 2>/dev/null || printf '{}')"
      ast="$(ingest_file "$af" "$ast")"
      new_agents="$(printf '%s' "$new_agents" | jq -c --arg n "$an" --argjson st "$ast" '.[$n] = $st' 2>/dev/null || printf '%s' "$new_agents")"
    done < <(find "$agent_dir" -type f -name 'agent-*.jsonl' -print0 2>/dev/null)
  fi
  A="$(printf '%s' "$main_st" | jq -c --argjson z "$Z" '.agg // $z' 2>/dev/null || printf '%s' "$Z")"
  LAST_U="$(printf '%s' "$main_st" | jq -c '.last.u // null' 2>/dev/null || printf 'null')"
  LAST_M="$(printf '%s' "$main_st" | jq -r '.last.m // empty' 2>/dev/null || true)"
  agent_agg="$(printf '%s' "$new_agents" | jq -c --argjson z "$Z" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

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

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    reduce (to_entries[] | .value.agg // empty_agg) as $g ($z; merge_agg($g))
  ' 2>/dev/null || printf '%s' "$Z")"
  AGENTS_COST="$(printf '%s' "$agent_agg" | jq -r '.cost // 0')"
  AGENTS_ON="$(printf '%s' "$agent_agg" | jq -r 'if (.msgs // 0) > 0 then 1 else 0 end')"
  launch_ts=""
  [ -f "$LAST_LAUNCH" ] && launch_ts="$(tr -d '[:space:]' < "$LAST_LAUNCH" 2>/dev/null || true)"
  case "$launch_ts" in ''|*[!0-9]*) launch_ts="" ;; esac
  prev_launch="$(printf '%s' "$cache" | jq -r '.launch_ts // empty' 2>/dev/null || true)"
  baseline="$(printf '%s' "$cache" | jq -c --argjson z "$Z" '.baseline // $z' 2>/dev/null || printf '%s' "$Z")"
  if [ -n "$launch_ts" ]; then
    if [ "$launch_ts" != "$prev_launch" ]; then
      baseline="$A"
    fi
    SESSION="$(jq -nc --argjson a "$A" --argjson b "$baseline" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

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

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
      $a | sub_agg($b)
    ' 2>/dev/null || printf '%s' "$Z")"
    HAVE_SESSION=1
  fi
  mkdir -p "$SESSION_DIR" 2>/dev/null || true
  if jq -nc --argjson main "$main_st" --argjson agents "$new_agents" \
        --argjson base "$baseline" --arg lts "$launch_ts" \
    '{main:$main, agents:$agents, baseline:$base, launch_ts:$lts}' \
    > "$cache_path.tmp" 2>/dev/null
  then
    mv "$cache_path.tmp" "$cache_path" 2>/dev/null || rm -f "$cache_path.tmp"
  else
    rm -f "$cache_path.tmp"
  fi
fi

jq -rn --argjson A "$A" --argjson P "$P" --argjson cu "$P_CU" \
   --argjson S "$SESSION" --argjson agents_cost "$AGENTS_COST" \
   --argjson agents_on "$AGENTS_ON" --argjson have_session "$HAVE_SESSION" \
   --argjson lastu "$LAST_U" --arg lastm "$LAST_M" \
   --arg mid "$P_MID" --arg mname "$P_MNAME" \
   --arg upct "$P_UPCT" --arg csize "$P_CSIZE" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

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

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
  def num($s): if ($s|type)=="string" and ($s|length)>0 then (($s|tonumber?) // -1) else -1 end;
  def pct($t;$d): if $t >= 0 and $d > 0 then ((($t/$d)*100)|floor) else -1 end;
  def cents($c): (($c*10000)|round);
  $A as $a
  | (if $mname != "" then $mname elif $mid != "" then $mid
     elif $lastm != "" then $lastm else "?" end) as $name
  | (if ($a.unknown|not) and $a.cost == 0
       and (($a.i+$a.o+$a.r+$a.w5+$a.w1) > 0)
     then 1 else 0 end) as $free
  | (num($upct)|if . >= 0 then floor else -1 end) as $p1
  | (if ($cu|type)=="object" then ($cu|ctx_tokens) else -1 end) as $cutok
  | num($csize) as $cs
  | (if ($lastu|type)=="object" then ($lastu|ctx_tokens) else 0 end) as $ctx_est
  | n2(($P[(if $lastm != "" then $lastm else $mid end)] // {}) | (.ctx // 0)) as $mc
  | (if $cs > $mc then $cs else $mc end) as $den
  | (if $p1 >= 0 then $p1 else pct($cutok;$cs) end) as $p2
  | (if $p2 >= 0 then $p2 else pct($ctx_est;$den) end) as $p3
  | (if $p3 > 100 then 100 elif $p3 < 0 then -1 else $p3 end) as $ctxp
  | (($a.i+$a.r+$a.w5+$a.w1)|round) as $tin
  | (($a.i+$a.r+$a.w5+$a.w1)) as $cden
  | (if $cden > 0 then ((($a.r/$cden)*100)|round) else -1 end) as $cachep
  | cents($a.cost) as $c4
  | cents($S.cost) as $sc4
  | cents($agents_cost) as $ac4
  | (if $a.unknown then "u" elif $free == 1 then "f" else "n" end) as $flag
  | (if $have_session == 1 and $flag == "n" and $sc4 > 0 and ($c4 - $sc4) > 1
     then 1 else 0 end) as $show_sess
  | [$name, $tin, ($a.o|round), $cachep, $ctxp, $c4, $sc4, $ac4,
     $flag, $show_sess, $agents_on]
  | map(tostring) | join("")
' 2>/dev/null | {
  IFS=$'\037' read -r R_NAME R_IN R_OUT R_CACHE R_CTX R_COST R_SESS R_AGENTS R_FLAGS R_SHOW_SESS R_AGENTS_ON || true
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
  money() {
    local c4="${1:-0}"
    case "$c4" in ''|*[!0-9-]*) c4=0 ;; esac
    if [ "$c4" -lt 0 ]; then c4=0; fi
    printf '$%d.%04d' $(( c4 / 10000 )) $(( c4 % 10000 ))
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
      if [ "${R_SHOW_SESS:-0}" = "1" ]; then
        segs+=("${BOLD}$(money "${R_SESS:-0}") this${RST} ${DIM}$(money "${R_COST:-0}") file${RST}")
      else
        segs+=("${BOLD}$(money "${R_COST:-0}")${RST}")
      fi
      ;;
  esac
  if [ "${R_AGENTS_ON:-0}" = "1" ]; then
    segs+=("${DIM}+$(money "${R_AGENTS:-0}") agents${RST}")
  fi
  line=""
  sep="${DIM}│${RST}"
  for s in "${segs[@]}"; do
    [ -n "$line" ] && line+="$sep"
    line+="$s"
  done
  printf '%s\n' "$line"
}
exit 0
