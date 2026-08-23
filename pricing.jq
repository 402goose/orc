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
