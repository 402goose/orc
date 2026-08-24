# 🧌 orc — OpenRouter × Claude

Run Claude Code against any OpenRouter model — GPT, Gemini, Kimi, DeepSeek, free
stealth previews like `stealth/ox-alpha` — with one command. `orc` handles the
env wiring, model selection, key management, and keeps its Claude state fully
isolated from your normal Anthropic login.

## Requirements

- macOS or Linux
- [Claude Code](https://claude.com/claude-code) (`claude` on PATH)
- `jq`, `curl`, `fzf` — `brew install jq fzf` on macOS, e.g.
  `sudo apt-get install jq curl fzf` on Debian/Ubuntu
- An [OpenRouter](https://openrouter.ai) API key

## Install

```bash
./install.sh
```

Copies `orc` to `~/.local/bin` (override with `DEST=/somewhere ./install.sh`)
and checks dependencies. Then:

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

## Usage

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
orc key                 configure key source (env var name or key store)
orc env                 print the export lines launch uses (contains your key)
orc refresh             force-refresh the cached model list
orc doctor              check everything end to end (resolved model + fit)
orc probe [model]       smoke-test the launch path (1-token request)
orc probe --fit [model] tool-loop smoke test; result cached 24h as FIT/FAIL
orc config              open config in $EDITOR
```

The model picker shows live OpenRouter input/output prices per million tokens.
Free models are marked `FREE`; type `FREE` in the regular picker or use
`orc free` to search only models whose current usage prices are all zero.
Every row also carries a tool-support flag read from the catalog's
`supported_parameters`: models marked `NO TOOLS` tend to feel broken under
Claude Code, which leans hard on tools. `orc models --tools` lists only
tool-capable models, and `orc doctor` warns when the *resolved* model
(not just the global default) lacks tool support.

A second column, `FIT` / `FAIL` / `UNTESTED`, is orc's own measurement:
`orc probe --fit` forces a one-tool round trip through OpenRouter's
Anthropic-compatible endpoint and caches the result for 24h. The picker
sorts last-known-good models first; `orc models --fit` lists only those.
`orc doctor` runs the fit probe when the cache is empty (`ORC_NO_FIT=1`
skips it). Catalog `tools` is an advertisement; FIT is whether the model
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

## Per-project config: .orc.json

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
   `~/.config/orc/key`, created with `0600` permissions and never made
   group/world readable (orc warns if it is). Nothing is ever written to a
   plaintext config file.

If neither is found, **orc refuses to launch** (fail closed) and tells you how
to fix it.

`orc doctor` goes further than static checks: as a final step it probes the
real launch path — a 1-token request to OpenRouter's Anthropic-compatible
endpoint (`/api/v1/messages`) with your resolved key and saved model — and
reports HTTP status and latency, so breakage surfaces before you are inside a
session. The probe costs a fraction of a cent on paid models; skip it with
`ORC_NO_PROBE=1`, or run it standalone against any model with `orc probe <id>`.

## What it sets for Claude Code

- `ANTHROPIC_BASE_URL` → OpenRouter's Anthropic-compatible endpoint
- `ANTHROPIC_AUTH_TOKEN` → your resolved key (`ANTHROPIC_API_KEY` is set to
  an empty string to avoid conflicts)
- `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` → your saved models
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` → the model's real context window from the
  OpenRouter catalog (Claude Code otherwise assumes 200k for unknown models)
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

Claude Code's built-in cost figure is deliberately ignored: it prices tokens
at Anthropic list rates, which is wrong when you're billed OpenRouter rates.

The script is generated at `~/.config/orc/hud.sh` on launch (self-contained
bash + jq, no extra dependencies); its source of truth is `hud.sh` in this
repo. Toggle with `orc hud off` / `orc hud on`, preview against your newest
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
to force). `orc env` is `launch` without the `exec`.

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
with a content hash over both sources — installed HUDs regenerate automatically
on the next launch. Edit the sources, then run `./build.sh`; CI fails if `orc`
or `hud.sh` have drifted from them.

`./test/run.sh` runs the test suite: HUD rendering against fixture
transcripts and a fixture catalog (paid / free / unknown-model pricing,
legacy and current cache-usage shapes, all three context-percentage
sources, subagent sibling spend, resume this-vs-file split), `orc stats`
aggregation, model tool-support and fit flags, profile / `.orc.json`
resolution, and `orc status` / `orc env` / `orc save` consuming the same
resolved object. CI runs shellcheck on every script plus the test suite.

## Caveats

- Tool-calling quality varies by model — Claude Code leans hard on tools, so
  weaker models will feel broken. Reasoning models with solid tool support
  work best.
- Extended thinking and prompt caching only work fully on Anthropic models;
  costs on other models may be higher than the same workflow on Anthropic
  first-party.
- Stealth/preview models (`stealth/*`) come from anonymous providers that
  retain prompts and completions — don't point them at anything sensitive.
- The HUD cost is an estimate: it trusts the catalog's per-token price fields,
  including OpenRouter's separate 5-minute/1-hour cache-write multipliers,
  which may not match what your route actually serves.
