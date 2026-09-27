# Routing and auto mode

[Start here](../README.md) · [CLI reference](cli.md) · [MCP reference](mcp.md)

- [Automatic and explicit choices](#automatic-and-explicit-choices)
- [Accounts and quota headroom](#accounts-and-quota-headroom)
- [Antigravity lanes](#antigravity-lanes)
- [Prompt cache and sessions](#prompt-cache-and-sessions)
- [Gym lane priors](#gym-lane-priors)

## Automatic and explicit choices

Use `fusion delegate --agent auto --read-only "Review the current diff"` to let
the router choose an eligible lane. Authored workflow nodes can likewise set
`"agent": "auto"`. Named routes live under `routes` in `.fusion.json`; a direct
CLI delegation still requires `--agent`, even when `--route` names the worker.

```sh
fusion delegate --agent claude --route orc-free --read-only "Review the diff"
fusion delegate --agent codex --read-only --model gpt-6-astra --reasoning-effort high "Inspect persistence boundaries"
```

Model names in examples are illustrative; installed CLI capabilities and account
access determine availability. Treat model and reasoning effort as a pair, not a
single universal quality scale. Explicit effort requires a resolved model; see
[model/effort validation, session identity and observed evidence](model-effort.md).

When an **Auto** stage hits a provider quota, Fusion excludes that route and
tries another installed, permitted worker within the existing attempt and budget
limits. This works with Laya off or in shadow mode. Explicitly selected workers
stay pinned. A quota notice identifies the exhausted worker and route separately
from Fusion's spend budget. **Resume workflow** lets you select the unfinished
stage, worker or named route, and an explicit attempt limit; accepted stages are
reused when their inputs still match. For example:

```sh
orc fusion workflow resume RUN_ID --node review --agent codex --max-attempts 3
```

Native workers are Codex, Claude, Antigravity (`agy`), and Grok Build (`grok`).
Local CLIs still use their own remote-provider accounts and quotas. Grok uses
headless `streaming-json` output, a fresh session, and no subagents; its default
`plan` permission mode makes it an automatic read-only review/discovery option
in restricted mode. The adapter reads model, usage and nonpartial cost when
reported. The older `plain` fallback lacks structured tool/usage receipts. Set
`grok.permission_mode` to `acceptEdits` for explicitly authorized writer work.
Named ORC routes can use other configured models; automatic ORC selection still

## Accounts and quota headroom

requires passing tool-fit evidence.

Named routes in `.fusion.json` can set `env` to an object of string environment
variables. These override the inherited worker environment; values expand `~`
and `$HOME` (including `${HOME}`) before the worker starts. For example:

```json
{"routes": {"claude-second": {
  "agent": "claude",
  "env": {"CLAUDE_CONFIG_DIR": "~/.claude-second"},
  "account": "acct2"
}}}
```

The optional non-empty string `account` identifies the subscription for lane
health and quota/permission cooldowns, giving this route the lane `claude@acct2`.
Without it, Fusion uses the expanded `CLAUDE_CONFIG_DIR`, then `CODEX_HOME`, from
the route environment as the account identity. Routes using the same account
share cooldowns; a second account remains available for automatic fallback.
ORC keeps its separate launcher lane (`claude@orc@acct2` with an account).
Routes without account settings retain their existing lane keys and cooldowns.

Automatic routing also uses the latest recorded quota for each lane, including
its account. Claude runs use `stream-json --verbose`; the final result retains
the existing result fields, with an additional `quota` when reported. Claude
rate-limit events and Codex `rate_limits` are normalized into `quota.windows`
with `used` fractions (0–1), `resets_at` Unix seconds, and Codex window duration
in `window_minutes`, and saved in the run result and trace. The existing
`fusion usage` headroom reader also exposes these trace observations.

Configure the thresholds in `.fusion.json`:

```json
{"quota": {"pace_margin": 0.15, "soft": 0.85, "hard": 0.97}}
```

A lane is **tight** when any active window's usage exceeds `soft`, or exceeds
the elapsed fraction of that window plus `pace_margin`. Elapsed time uses
Claude's five-hour/seven-day windows or Codex's reported duration. Tight lanes
rank after other eligible lanes for the same work class, including after
outcome and cache ranking. A lane is **exhausted** above `hard`, or when its
status is `rejected` before its most-used window resets; automatic routing
excludes it. Comparisons are strict, thresholds must be fractions, and `soft`
cannot exceed `hard`. Expired windows stop constraining routing. Unknown
duration disables only the pacing comparison; missing quota preserves the
existing order.

Explicit routes stay pinned, and existing authorized exploration still applies.
Quota-free traces do not erase an earlier observation, and observations without
a recorded lane key cannot constrain unrelated accounts. Routing logs and
`fusion decisions routing-report` include quota windows, classifications,
thresholds, and reasons for demotions and exclusions, even before an outcome
is recorded. These quota decisions are logged even with decision advice off.

## Antigravity lanes

`agy` — the Google Antigravity CLI — is a third worker harness. It runs as a
sidekick (`--agent agy`), an Ultra stage/harness, or a workflow node, and its
host model list covers Gemini, Claude, and GPT-OSS under a separate quota pool
(`agy models`). Sessions resume through `--conversation` the same way Codex
threads do.

```sh
fusion delegate --agent agy --read-only --role countercheck \
  --success 'structured handoff' 'Re-verify the diff against the spec.'
fusion --json ultra --harness agy 'Explore and review this change.'
```

Restricted-mode automatic AGY routing requires explicit
`dangerously_skip_permissions: true` on the lane; sandbox/native command settings
alone do not qualify it. Explicit AGY selection can use a narrower setup.
See [AGY permissions and setup](configuration.md#antigravity-settings).

## Prompt cache and sessions

Trace spans record the session key, whether the run resumed, idle time since the
session's last run, and cache-read ratio. Optional `cache` configuration can
start fresh instead of cold-resuming and let warm lanes break routing ties.
See [Prompt cache and sessions](../FUSION_DECISIONS.md#prompt-cache-and-sessions).

## Gym lane priors

`fusion gym priors GYM_DIR` exports measured per-lane, per-work-class outcomes to
`$ORC_HOME/lane_priors.json` (normally `~/.config/orc/lane_priors.json`). Routing
uses these as bounded pseudo-counts, with weight `0.5` per gym attempt and cap
`10` per lane/work class by default. Local verified outcomes accumulate alongside
them. A missing file contributes no prior; malformed data is rejected.

```json
{"decisions": {"priors": {"weight": 0.5, "cap": 10}}}
```

Set `decisions.priors` to `false` to disable them, or set its `path` to a different
export. Priors do not confer permissions or bypass availability, fit or quota
checks. See [gym evidence](learning.md#gym) and
[configuration](configuration.md).
