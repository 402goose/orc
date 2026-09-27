# The orc launcher

[Start here](../README.md) · [CLI reference](cli.md) · [MCP reference](mcp.md)

- [Setup and launch menu](#setup-and-launch-menu)
- [Command reference](#command-reference)
- [Choosing a model](#choosing-a-model)
- [Profiles](#profiles)
- [Per-project config: `.orc.json`](#per-project-config-orcjson)
- [Stats](#stats)
- [How the key is resolved](#how-the-key-is-resolved)
- [What it sets for Claude Code](#what-it-sets-for-claude-code)
- [The HUD](#the-hud)
- [Config](#config)
- [Development](#development)
- [Caveats](#caveats)

The original `orc`: one command to run Claude Code against any OpenRouter model
supported by the current catalog and your account. Model IDs and prices below
are illustrative snapshots, not availability guarantees. It
handles env wiring, model selection and key management, and keeps its Claude
state fully isolated from your normal Anthropic login, so you never have to
`/logout` between Anthropic and OpenRouter sessions.

The guild uses it too: the `orc-free` and `orc-best` workflow routes resolve
through this catalog at run time.

## Setup and launch menu

```bash
orc
```

First run launches the setup wizard: it finds your API key (or helps you set
one up), fetches the live OpenRouter model catalog, and gives you an fzf
picker with per-model pricing and context sizes. It also offers a launch
permission mode (default / auto / acceptEdits / plan / dontAsk / yolo).
Choices are saved; the next run shows the resolved model + mode (and
`@profile` / `.orc.json` when those apply) and lets you launch with Enter
or change this invocation first:

```
orc → stealth/ox-alpha · mode: auto · @work · .orc.json
  [Enter] launch   [m] model   [a] profile   [f] free   [p] mode   [s] save   [k] key   [q]
```

`[m]` / `[p]` write the global default unless a profile or `.orc.json` is
in effect — then they override this launch only. `[s]` snapshots the
resolved combo as a named profile.

## Command reference

```
orc                     launch (first run starts the setup wizard)
orc [claude args...]    pass args through to claude (orc -c, orc -p "...", orc --resume)
orc -m <model> [...]    one-off model override (not saved)
orc @<profile> [...]    launch with a saved profile (one-off, not saved as default)
orc setup               re-run the setup wizard (key + model)
orc model [query]       pick + save a new default model
orc model --set <id>    set the default model non-interactively
orc free [query]        pick + save a currently free model
orc mode                pick + save the launch permission mode
orc small [query]       pick + save a small/fast model for background tasks
orc small --set <id>    set the small model non-interactively
orc save <name>         snapshot the resolved model/small/mode as profile @<name>
orc profiles [rm <n>]   list saved profiles / remove one
orc status [--json]     print the resolved launch (model/mode/profile/source)
orc stats               token + cost totals across all orc transcripts
     [--json] [--by model|project|day]
orc hud [on|off|demo]   toggle the statusline HUD / preview it on the newest transcript
orc models [query]      list models with pricing + context + tool + fit
orc models --free [...] list only currently free models
orc models --tools [..] list only models advertising tool support
orc models --fit [...]  list only models that passed orc probe --fit
orc quality             dump the Artificial Analysis quality cache as a table
orc key                 configure key source (env var name or key store)
orc env                 print the export lines launch uses (contains your key)
orc refresh             force-refresh the cached model list
orc doctor              check everything end to end (resolved model + fit)
orc probe [model]       smoke-test the launch path (1-token request)
orc probe --fit [model] tool-loop smoke test; result cached 24h as FIT/FAIL
orc config              open config in $EDITOR
orc fusion ui           open the local browser control room
```

## Choosing a model

The model picker shows live OpenRouter input/output prices per million tokens.
Free models are marked `FREE`; type `FREE` in the regular picker or use
`orc free` to search only models whose current usage prices are all zero.
Every row also carries a tool-support flag read from the catalog's
`supported_parameters`: models marked `NO TOOLS` tend to feel broken under
Claude Code, which leans hard on tools. `orc models --tools` lists only
tool-capable models, and `orc doctor` warns when the *resolved* model
(not just the global default) lacks tool support.

**Ranking.** The picker is sorted by the Artificial Analysis
Intelligence Index, a 0–~63 score for general model capability.
Within a score, ties break on prompt price (cheaper first), then
`FIT` first, then tool-capable models, then everything else, then model id. A bundled snapshot
of the leaderboard ships with orc at `data/quality.json`; on first
launch it is copied into `$ORC_HOME/quality.json` and used for
ranking without any network call. `orc refresh` re-fetches the OpenRouter
catalog; quality handling only seeds a missing user cache or warns if it is
stale. `make refresh-quality` (from the source) re-runs the cmndcntr fetcher
and updates `data/quality.json` for the next release. Reinstalling does not
overwrite an existing `$ORC_HOME/quality.json`; replace that cache explicitly
with the reviewed snapshot if you want the new rankings.

Use `orc quality` to inspect the current cache as a sorted table:

```
OR_ID                            II    CODE   AGENT  CREATOR          SLUG
anthropic/claude-opus-5          63.05  77.98  59.17  Anthropic        claude-opus-5
anthropic/claude-fable-5         62.07  76.49  56.59  Anthropic        claude-fable-5
openai/gpt-5.6-sol               60.92  77.38  57.78  OpenAI           gpt-5.6-sol
x-ai/grok-4.6                    60.92  76.78  58.67  SpaceXAI         grok-4-6
...
```

A second column, `FIT` / `FAIL` / `UNTESTED`, is orc's own measurement:
`orc probe --fit` forces a one-tool round trip through OpenRouter's
Anthropic-compatible endpoint and caches the result for 24h. The picker
prefers last-known-good models only after quality and prompt-price ordering; `orc models --fit` lists only those.
`orc doctor` runs the fit probe only after a successful launch probe and when
the resolved model's fit is `UNTESTED`. `ORC_NO_FIT=1` skips it;
`ORC_NO_PROBE=1` skips the enclosing launch-probe branch too. Catalog `tools`
is an advertisement; FIT is whether the model
survived a Claude Code-shaped loop.

## Profiles

A profile is a named snapshot of the *resolved* `model` + `small_model` +
`mode` — including a one-off `-m`, `ORC_MODE`, or `.orc.json` pin. Keep one
combo for real work and one for throwaway experiments, and switch per launch
without touching your saved default. `orc status` prints the combo that
would launch from this directory. `orc env` prints the same exports
`launch` would set (including context window and gateway discovery).

```bash
orc status              # what would launch right now
orc status --json       # same object, for wrappers
```

Resolution order for each of `model` / `small_model` / `mode`:

1. one-off flags: `-m` / `ORC_MODEL_OVERRIDE` / `ORC_MODE`
2. `orc @<profile>` / `ORC_PROFILE`
3. `.orc.json` inline keys
4. the profile named by `.orc.json`'s `"profile"`
5. `~/.config/orc/config.json`

```bash
orc save work           # snapshot the current setup as @work
orc @work               # launch with it (default config unchanged)
orc @work -c            # profile + claude args compose
orc profiles            # list; orc profiles rm work removes
```

`ORC_PROFILE=work orc` is equivalent to `orc @work` — useful for wrappers.

## Per-project config: `.orc.json`

Drop a `.orc.json` in a repo (found by walking up from the current directory)
to pin settings for that project:

```json
{ "profile": "work" }
```

or inline, without needing a profile:

```json
{ "model": "moonshotai/kimi-k2", "mode": "plan" }
```

Resolution is the same stack `orc status` prints — see above. The launch
menu, `orc env`, `orc save`, `orc doctor`, and `orc probe` all consume
that object, so a project pin or `@work` is never silently ignored.

## Stats

`orc stats` aggregates every transcript orc has ever produced (they all live
under orc's isolated state dir) and prices them against the cached catalog —
the same math as the HUD, across all sessions, subagent transcripts included:

```
MODEL                     MSGS  IN       OUT     CACHE  COST
anthropic/claude-opus-5   185   11.74M   143.7k  93%    $13.8608
stealth/ox-alpha          1024  184.86M  533.3k  94%    $0
TOTAL                     1246  200.48M  737.5k  94%    $24.5181
```

`--by project` or `--by day` regroups the table; `--json` emits the full
structured breakdown (totals plus all three groupings) for scripts. Responses
from models missing from the cached catalog are flagged `+?` rather than
silently priced at zero. Unlike the OpenRouter dashboard, this splits spend
per project and per model as seen from your machine.

## How the key is resolved

1. The env var named in your config — `OPENROUTER_API_KEY` by default,
   changeable via `orc key`.
2. The system key store, where the key wizard stores pasted keys: the macOS
   Keychain (service `orc-openrouter`) on macOS; on Linux a file at
   `$ORC_HOME/key` (normally `~/.config/orc/key`). A new file is created under
   `umask 077`; overwriting an existing file does not repair its permissions.
   The warning checks world readability, not all group/world permissions.
   The key is not placed in JSON configuration; the Linux key file is plaintext.

If neither is found, **orc refuses to launch** (fail closed) and tells you how
to fix it.

`orc doctor` goes further than static checks: as a final step it probes the
real launch path — a 1-token request to OpenRouter's Anthropic-compatible
endpoint (`/api/v1/messages`) with your resolved key and resolved model (including project/profile/override choices) — and
reports HTTP status and latency, so breakage surfaces before you are inside a
session. The one-token probe can incur provider charges; skip it with
`ORC_NO_PROBE=1`, or run it standalone against any model with `orc probe <id>`.

## What it sets for Claude Code

- `ANTHROPIC_BASE_URL` → OpenRouter's Anthropic-compatible endpoint
- `ANTHROPIC_AUTH_TOKEN` → your resolved key (`ANTHROPIC_API_KEY` is set to
  an empty string to avoid conflicts)
- `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` → your resolved models
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` → the model's real context window from the
  OpenRouter catalog (when a context length is present)
- `ANTHROPIC_DEFAULT_HAIKU_MODEL` → your resolved small model
- `CLAUDE_CONFIG_DIR` → `~/.config/orc/claude-state`, so orc never touches
  your real `~/.claude` login and you never have to `/logout` between
  Anthropic and OpenRouter sessions

The base URL and model are additionally forced via `claude --settings` (CLI
settings outrank directory settings), so a project-level
`.claude/settings.json` with its own `env.ANTHROPIC_BASE_URL` — a proxy, a
gateway — can't silently hijack an orc session.

## The HUD

Every orc launch injects a Claude Code statusline (on by default, your real
`~/.claude` settings are never touched). It renders one line:

```
stealth/ox-alpha │ ↑ 3.50M ↓ 45.8k │ cache 89% │ ctx ▓▓░░░░░░░░░ 11% │ $0.8412 │ +$0.31 agents
```

- **↑ / ↓** — total tokens in (including cached) and out on the parent
  transcript. Subagent tokens are priced separately so the parent window
  stays honest.
- **cache** — share of parent input tokens served from prompt cache
- **ctx** — context gauge of the parent window; green under 60%, yellow
  under 85%, red at the top. Prefers Claude Code's own context reading,
  falls back to the last parent API call over the catalog context length
- **cost** — parent-transcript cost from usage × live OpenRouter catalog
  prices, joined per response (mid-session model switches price correctly).
  Shows `FREE` for zero-priced models, `$—` when a model isn't in the
  cached catalog. After `orc -c` / `/resume`, a second figure appears:
  `$0.12 this $1.24 file` — spend since this join vs the whole file
- **+agents** — sibling spend from `<session>/subagents/agent-*.jsonl`,
  same math as `orc stats`. Shown only when a subagent has billed tokens

The HUD keeps an incremental cache at `~/.config/orc/sessions/<id>.json`
and only reads new bytes on each statusline tick, so a multi-megabyte
transcript does not get fully reparsed every time. `orc` stamps
`~/.config/orc/last-launch` at exec so a resume can split "this join"
from "the file".

The HUD deliberately ignores Claude Code's built-in cost figure and computes
an OpenRouter catalog estimate instead; it does not establish your actual bill.

The script is generated at `~/.config/orc/hud.sh` on launch (self-contained
bash + jq, no extra dependencies); its editable sources are `hud.sh.in` and
`pricing.jq`. Repository `hud.sh` is generated by `build.sh`. Toggle with `orc hud off` / `orc hud on`, preview against your newest
transcript with `orc hud demo`, or disable for a single launch with
`ORC_HUD=0`.

The HUD now folds sibling subagent transcripts and splits resume spend
as `$this` vs `$file`. Compacted / rewritten transcript files reset the
byte-offset cache automatically.

## Config

`~/.config/orc/config.json`:

```json
{
  "key_env": "OPENROUTER_API_KEY",
  "model": "stealth/ox-alpha",
  "small_model": "google/gemini-2.5-flash",
  "hud": "on",
  "profiles": {
    "work": { "model": "anthropic/claude-opus-5", "mode": "plan" }
  }
}
```

`ORC_YES=1` skips the launch confirmation (for scripts). `ORC_HOME` moves the
config dir. `ORC_HUD=0` hides the statusline HUD for one invocation.
`ORC_NO_PROBE=1` skips doctor's launch probe. `ORC_NO_FIT=1` skips doctor's
tool-loop fit probe. `ORC_MODE` overrides the launch permission mode for
one invocation (`default` / `auto` / `acceptEdits` / `plan` / `dontAsk` /
`yolo`) without touching the saved config — this is how wrappers like
cmndcntr launch orc with their own per-run policy. `ORC_PROFILE` does the
same for profiles. `ORC_MODEL_OVERRIDE` is the env form of `-m`. The model
catalog and fit cache both live 24h (`orc refresh` / `orc probe --fit`
to force). The Artificial Analysis quality cache lives 24h too;
`orc refresh` re-checks it but won't re-fetch the leaderboard (the
scraper is in cmndcntr; run `make refresh-quality` from the orc source
to update the bundled snapshot, then explicitly replace
`$ORC_HOME/quality.json` with the reviewed `data/quality.json`).
`orc env` is `launch` without the `exec`.

## Development

The shipped scripts are assembled. Sources of truth:

- `pricing.jq` — the shared jq math behind the HUD, `orc stats`, and the model
  picker (token normalization, transcript validation, pricing, free/tool
  flags, incremental session aggregates). The HUD and stats are guaranteed
  to price identically because they run the same definitions.
- `hud.sh.in` — the statusline HUD body.
- `orc` — everything else.

`./build.sh` expands the `#INCLUDE pricing.jq` markers, writes the generated
`hud.sh`, splices it into `orc`'s embedded heredoc, and stamps `ORC_HUD_VERSION`
with a content hash over both sources plus `data/quality.json` — installed
HUDs regenerate automatically on the next launch. Edit the sources, then run
`./build.sh`; CI fails if `orc` or `hud.sh` have drifted from them.

The quality cache is a static JSON snapshot shipped at `data/quality.json`
(generated by cmndcntr's `scripts/fetch-artificial-analysis-leaderboard.mjs`,
output passed through `make refresh-quality`). Records on the leaderboard
that lack an `openrouterApiId` are routed through `SLUG_TO_OR_ID` inside
`orc` (an inlined JSON map of effort variants and ambiguous slugs) so
they still rank. New slugs the fetcher can't auto-join are listed in the
`unmappedSlugs` field of the output; add them to the map after deciding
which OpenRouter id they belong to, then rerun `make build`.

`./test/run.sh` runs the test suite: HUD rendering against fixture
transcripts and a fixture catalog (paid / free / unknown-model pricing,
legacy and current cache-usage shapes, all three context-percentage
sources, subagent sibling spend, resume this-vs-file split), `orc stats`
aggregation, model tool-support and fit flags, profile / `.orc.json`
resolution, and `orc status` / `orc env` / `orc save` consuming the same
resolved object. CI runs shellcheck on every script plus the test suite.

`make test` runs the Python suite and rejects unexpected skips and expected failures.
The real Codex sandbox probe is explicitly reported as unavailable on Linux, on
machines without Codex, or when the installed CLI lacks named permission profiles.
The other permission tests still run; a failing sandbox probe is never ignored.

## Caveats

- Tool-calling quality varies by model — Claude Code leans hard on tools, so
  weaker models will feel broken. Reasoning models with solid tool support
  work best.

- The HUD cost is an estimate: it trusts the catalog's per-token price fields,
  including OpenRouter's separate 5-minute/1-hour cache-write multipliers,
  which may not match what your route actually serves.


Provider support for thinking, caching and retention is not established by this
repository. Check the selected provider's current policies. Catalog/tool-fit data
and example quality scores do not guarantee present-day model availability.
