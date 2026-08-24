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
