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
  [Enter] launch   [m] model   [p] permission mode   [k] key   [q] quit
```

## Usage

```
orc                     launch (first run starts the setup wizard)
orc [claude args...]    pass args through to claude (orc -c, orc -p "...", orc --resume)
orc -m <model> [...]    one-off model override (not saved)
orc setup               re-run the setup wizard (key + model)
orc model [query]       pick + save a new default model
orc mode                pick + save the launch permission mode
orc small [query]       pick + save a small/fast model for background tasks
orc models [query]      list models with pricing + context window
orc key                 configure key source (env var name or macOS keychain)
orc env                 print the export lines orc uses (contains your key)
orc refresh             force-refresh the cached model list
orc doctor              check everything end to end
orc config              open config in $EDITOR
```

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

## Config

`~/.config/orc/config.json`:

```json
{
  "key_env": "OPENROUTER_API_KEY",
  "model": "stealth/ox-alpha",
  "small_model": "google/gemini-2.5-flash"
}
```

`ORC_YES=1` skips the launch confirmation (for scripts). `ORC_HOME` moves the
config dir. The model catalog is cached for 24h (`orc refresh` to force).

## Caveats

- Tool-calling quality varies by model — Claude Code leans hard on tools, so
  weaker models will feel broken. Reasoning models with solid tool support
  work best.
- Extended thinking and prompt caching only work fully on Anthropic models;
  costs on other models may be higher than the same workflow on Anthropic
  first-party.
- Stealth/preview models (`stealth/*`) come from anonymous providers that
  retain prompts and completions — don't point them at anything sensitive.
