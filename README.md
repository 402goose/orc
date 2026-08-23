# 🧌 orc — OpenRouter × Claude

Run Claude Code against any OpenRouter model — GPT, Gemini, Kimi, DeepSeek, free
stealth previews like `stealth/ox-alpha` — with one command. `orc` handles the
env wiring, model selection, key management, and keeps its Claude state fully
isolated from your normal Anthropic login.

## Requirements

- macOS (uses the Keychain and BSD `stat`)
- [Claude Code](https://claude.com/claude-code) (`claude` on PATH)
- `jq`, `curl`, `fzf` — `brew install jq fzf` covers the non-default ones
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
Choices are saved; the next run shows the saved model + mode and lets you
launch with Enter or change either one first:

```
orc → stealth/ox-alpha · mode: auto
  [Enter] launch   [m] model   [f] free model   [p] permission mode   [k] key   [q] quit
```

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
orc save <name>         snapshot current model/small/mode as profile @<name>
orc profiles [rm <n>]   list saved profiles / remove one
orc stats               token + cost totals across all orc transcripts
     [--json] [--by model|project|day]
orc hud [on|off|demo]   toggle the statusline HUD / preview it on the newest transcript
orc models [query]      list models with pricing + context window
orc models --free [...] list only currently free models
orc key                 configure key source (env var name or macOS keychain)
orc env                 print the export lines orc uses (contains your key)
orc refresh             force-refresh the cached model list
orc doctor              check everything end to end
orc config              open config in $EDITOR
```

The model picker shows live OpenRouter input/output prices per million tokens.
Free models are marked `FREE`; type `FREE` in the regular picker or use
`orc free` to search only models whose current usage prices are all zero.

## Profiles

A profile is a named snapshot of `model` + `small_model` + `mode`. Keep one
combo for real work and one for throwaway experiments, and switch per launch
without touching your saved default:

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

Resolution order for each of `model` / `small_model` / `mode`:

1. one-off flags: `-m` / `ORC_MODE`
2. `orc @<profile>` / `ORC_PROFILE`
3. `.orc.json` inline keys
4. the profile named by `.orc.json`'s `"profile"`
5. `~/.config/orc/config.json`

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
2. The macOS Keychain (service `orc-openrouter`), where the key wizard stores
   pasted keys. Nothing is ever written to a plaintext config file.

If neither is found, **orc refuses to launch** (fail closed) and tells you how
to fix it.

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
stealth/ox-alpha │ ↑ 3.50M ↓ 45.8k │ cache 89% │ ctx ▓▓░░░░░░░░░ 11% │ $0.8412
```

- **↑ / ↓** — total tokens in (including cached) and out this session
- **cache** — share of input tokens served from prompt cache
- **ctx** — context gauge; green under 60%, yellow under 85%, red at the top.
  Prefers Claude Code's own context reading, falls back to the last API call's
  usage over the model's catalog context length
- **cost** — session cost estimated from transcript usage × live OpenRouter
  catalog prices, joined per response (mid-session model switches price
  correctly). Shows `FREE` for zero-priced models, `$—` when a model isn't in
  the cached catalog

Claude Code's built-in cost figure is deliberately ignored: it prices tokens
at Anthropic list rates, which is wrong when you're billed OpenRouter rates.

The script is generated at `~/.config/orc/hud.sh` on launch (self-contained
bash + jq, no extra dependencies); its source of truth is `hud.sh` in this
repo. Toggle with `orc hud off` / `orc hud on`, preview against your newest
transcript with `orc hud demo`, or disable for a single launch with
`ORC_HUD=0`.

Two known limits: on `/resume` the figure covers the whole conversation file,
not just since you rejoined; and tokens spent inside subagent transcripts are
not counted (they are in `orc stats`).

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
`ORC_MODE` overrides the launch permission mode for one invocation
(`default` / `auto` / `acceptEdits` / `plan` / `dontAsk` / `yolo`) without
touching the saved config — this is how wrappers like cmndcntr launch orc with
their own per-run policy. `ORC_PROFILE` does the same for profiles. The model
catalog is cached for 24h (`orc refresh` to force).

## Development

`orc` is shipped as a single self-contained script, but the HUD's source of
truth is `hud.sh` — edit that, then run `./build.sh`, which splices it into
`orc`'s embedded heredoc and stamps `ORC_HUD_VERSION` with a content hash (so
installed HUDs regenerate automatically on the next launch).

`./test/run.sh` runs the test suite: HUD rendering against fixture
transcripts and a fixture catalog (paid / free / unknown-model pricing,
legacy and current cache-usage shapes, all three context-percentage sources),
`orc stats` aggregation, and profile / `.orc.json` resolution. CI runs
shellcheck on every script plus the test suite, and fails if `orc` was edited
without being reassembled from `hud.sh`.

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
