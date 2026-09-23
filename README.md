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
orc quality             dump the Artificial Analysis quality cache as a table
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

**Ranking.** The picker is sorted by the Artificial Analysis
Intelligence Index, a 0–~63 score for general model capability.
Within a score, ties break on prompt price (cheaper first), then
`FIT` over `UNTESTED` over `FAIL`, then model id. A bundled snapshot
of the leaderboard ships with orc at `data/quality.json`; on first
launch it is copied into `$ORC_HOME/quality.json` and used for
ranking without any network call. `orc refresh` re-fetches both
the OpenRouter catalog and the quality snapshot; `make refresh-quality`
(from the orc source) re-runs the cmndcntr fetcher and copies a
fresh snapshot into `data/quality.json` for the next release.

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
to force). The Artificial Analysis quality cache lives 24h too;
`orc refresh` re-checks it but won't re-fetch the leaderboard (the
scraper is in cmndcntr; run `make refresh-quality` from the orc source
to update the bundled snapshot, then `./build.sh` and reinstall).
`orc env` is `launch` without the `exec`.

## Fusion: Claude lead + Codex sidekick

`fusion` adds a small lead/sidekick harness beside `orc`. It keeps the lead agent in charge of the user conversation and final review, then delegates bounded work to Claude Code, Codex, or Antigravity through a shared task contract. Each run records its task, JSONL events, stdout, stderr, result, and reusable session id under `.fusion/` in the workspace.

The lead can call the other agent through an MCP server:

```sh
fusion lead                 # Claude leads by default
fusion lead --agent codex   # Codex leads and can call Claude
fusion doctor
fusion trace --limit 50
fusion usage --limit 1000
```

To build a feature, start from the target project's directory and describe the
outcome in one sentence:

```sh
orc fusion build "Let users export their filtered dashboard as CSV"
fusion build --agent claude "Add saved searches with names and deletion"
```

`build` opens an interactive lead session (Codex by default) with the build
instructions included. The lead is asked to inspect the repo, derive a brief
and acceptance criteria, save its plan under `.fusion/builds/`, implement,
run checks, delegate an independent review, and fix verified findings. It
asks focused questions when a missing product decision changes the scope;
you do not need to write the orchestration prompt or choose each worker.
`build` also accepts a GitHub issue URL, preserves the full request and saves
a runnable workflow. Planning-only requests keep the lead and workers in
read-only/plan mode. Use `--plan-only` to prepare artifacts without starting
an agent, or `--execute` to run the bounded workflow. The lead and workers
use their configured accounts.
Use `fusion status` and `fusion usage` to inspect delegated work.

Optional local [Laya decisions](FUSION_DECISIONS.md) cover intake, automatic
worker routing, bounded recovery, specialist review, a semantic acceptance
check on structurally-passing workflow nodes, and learning from reviewed
outcomes. They start in shadow mode, recording advice without applying it:

```sh
orc fusion decisions setup
orc fusion decisions probe "Build a CSV export with tests"
orc fusion build --execute "Add CSV export and test filtering and escaping"
orc fusion decisions list
```

Learned actions require explicit activation and matching calibration from
held-out reviewed tasks. Missing models or uncertain/truncated inputs fall
back to deterministic policies. `FUSION_DECISIONS_MODE=off` disables the
classifier; see the [setup and learning guide](FUSION_DECISIONS.md).

Terminal runs show live progress: Laya startup and recommendations, stage and
worker selection, public worker updates, acceptance results, and elapsed-time
heartbeats every ten seconds. Worker stdout/stderr logs are written while the
worker runs. Progress goes to stderr; `--json` keeps stdout machine-readable.
Use `--progress` to force updates when redirecting output, or `--quiet` to
hide them. Both are global flags, before the subcommand.

```sh
orc fusion --progress build --kind review --execute "Review opportunities to simplify this repository"
orc fusion workflow watch                 # latest workflow/build --execute, including intake
orc fusion workflow watch WORKFLOW_ID     # attach to a specific workflow or build ID
orc fusion workflow watch --once          # one snapshot; no new workers
```

Builds register before fetching issues or loading Laya, so `watch` shows intake
immediately and follows the resulting workflow automatically. Automatic attachment
uses the most recently started run, even if an older run has updated since then.
Ctrl-C in `watch` only detaches the viewer. Runs started before this update
can show saved stages and blockers but cannot gain live worker logs retroactively.

For a direct worker call:

```sh
fusion delegate --agent codex --role implementation \
  --success 'tests pass' \
  'Add the requested feature and run the narrowest meaningful test suite.'
fusion ultra 'Add the requested feature and ship the smallest tested change.'
fusion ultra --cheap-only 'Explore and review this without spending on a strong route.'
fusion ultra --harness codex 'Run the full bounded pipeline through Codex.'
```

`fusion` uses a single writer lock for a workspace, so two write tasks cannot edit the same checkout at once. Use separate Git worktrees when you want parallel write tasks. Read-only tasks can run independently. The default Codex sidekick uses `codex exec --json`; the default Claude sidekick uses Claude Code print mode with structured JSON output.

A worker's overall status (`STATUS: success|partial|blocked|error`) is still a
self-reported label, but Fusion doesn't trust it blindly: `turn.failed`/`error`
events (Codex) and `is_error` (Claude) are structural, process-level signals
that override a self-reported "success" claim. On top of that, Codex's
`command_execution` items carry their own per-command exit code — if a
command a worker actually ran fails but the worker's final text still claims
success, that contradiction is surfaced as a blocker (`command exited N:
<command>`) rather than silently trusted. This doesn't override the
self-reported status on its own — a nonzero exit isn't always a real failure
(`grep` returning 1 for "no matches" is routine) — it makes the evidence
visible for a human or an acceptance gate to weigh, the same way permission
denials already are.

### Antigravity (agy) workers

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

Headless `agy` cannot answer tool-permission prompts, so it auto-denies tools
outside its allow-rules; plan mode is safe for read-only work, and write routes
should use `accept-edits` with the workspace sandbox rather than
`--dangerously-skip-permissions`. `agy` has no per-call budget flag, so cap its
cost with `timeout_seconds` and the workflow's `budget_usd`. OpenRouter models
are not in the `agy` host list — route those through the `orc` path instead.
`agy` is not (yet) a lead candidate; `fusion lead` remains Claude or Codex.

### Ultra without the token fire

Ultra is an explicit bounded pipeline modeled after the useful part of
[UltraCode](https://github.com/diepquynh/ultracode): explore, plan, implement,
review, and synthesize. It uses fresh stage contexts and JSON handoff files in
`.fusion/ultra/`, so later stages read evidence instead of inheriting every
earlier transcript. The stage count is capped, writes are serialized, and the
example routes cap each ORC-backed Claude call with `--max-budget-usd`.

Copy `.fusion.json.example` to `.fusion.json` in a project, then make sure ORC
has a current model catalog and key. The `orc-free` route selects the highest
ranked currently free tool-capable model; `orc-best` selects the highest ranked
tool-capable model from ORC's live catalog. The IDs are resolved at run time so
the pipeline does not pin a stale model name. Both routes require a model that
already passed `orc probe --fit`; set `allow_untested: true` on the route,
task, or workflow node to fall back to the highest-ranked tool-capable model
regardless of fit. Override either route with an explicit `model`, `profile`,
or `launcher_args` when you want a fixed lane.

```sh
cp .fusion.json.example .fusion.json
orc refresh
fusion ultra 'Refactor the cache layer and keep the existing tests green.'
orc fusion ultra --cheap-only 'Review the current diff for regressions.'
```

`fusion ultra` is opt-in because a multi-stage workflow can spend more tokens
than a direct lead/sidekick run. Use `fusion delegate --route orc-free ...`
for one bounded cheap worker, or `--route orc-best` when the stage needs a
stronger model. `--harness codex` runs every configured stage through Codex;
`fusion lead --agent codex` makes Codex the interactive lead and exposes the
same MCP delegation tools for Claude workers.

### Persisted fan-out workflows

For real orchestration, use `fusion workflow` with a JSON graph. Unlike the
linear Ultra preset, a workflow can map one node over many items, fan in on
explicit dependencies, run independent read-only nodes concurrently, retry
invalid handoffs, pause on provider quota, and resume from the persisted
manifest without repeating accepted nodes.

```sh
cp .fusion.workflow.example.json workflow.json
fusion --workspace . --json workflow run workflow.json
fusion --workspace . --json workflow status WORKFLOW_ID
fusion --workspace . --json workflow resume WORKFLOW_ID
fusion --workspace . --json workflow resume WORKFLOW_ID --spec workflow.json
fusion --workspace . workflow report WORKFLOW_ID
```

`workflow report` shows the final worker deliverable, including findings,
acceptance criteria, verification commands and caveats. It recovers full answers
from older provider logs; new runs save a separate `answer.md`. The report shows
actual workers, durations and blockers, and distinguishes unreported cost from
an explicitly reported zero. Reporting starts no agents and leaves receipts intact.

```sh
orc 'fusion' workflow report WORKFLOW_ID                # final output
orc 'fusion' workflow report WORKFLOW_ID --finding 3    # one recommendation + next command
orc 'fusion' workflow report WORKFLOW_ID --node explore # supporting investigation
orc 'fusion' workflow report WORKFLOW_ID --all          # every stage's full answer
orc 'fusion' workflow report WORKFLOW_ID --brief        # compact status
orc 'fusion' workflow report WORKFLOW_ID --output report.md
orc 'fusion' --json workflow report WORKFLOW_ID         # structured outputs and provenance
orc 'fusion' --progress build --from-workflow WORKFLOW_ID --finding 3 --plan-only
```

`--finding` recognizes numbered, bold Markdown recommendation headings. When
multiple final stages have answers, select one with `--node` (or `--from-node`
on `build`). Build preparation carries a reference to the original evidence and
scopes the new request to the chosen recommendation. `--plan-only` saves a brief
and workflow without launching coding agents; normal build execution options
also apply. Follow the target repository's worktree and ownership rules before
execution. Markdown export refuses to overwrite an existing file.

Every node gets a durable artifact under `.fusion/workflows/WORKFLOW_ID/` and
every worker still gets the existing `.fusion/runs/` trace. A node is accepted
only when its worker reports success and its declared `required_files` and
acceptance checks pass. `max_parallel`, `max_attempts`, `budget_usd`, and the
single-writer limit are enforced by the scheduler. Keep fan-out nodes
read-only; put repository writes behind a final writer or use separate Git
worktrees for independent implementations.

Before dispatching, the runner preflights each agent lane used in the graph:
it checks the resolved command is on PATH, and (on a fresh `run`, not a
`resume`) checks whether the most recent trace for that agent in
`.fusion/traces.jsonl` reported a quota or session limit in the last 15
minutes. A blocked or cooling-down lane pauses every pending node for that
agent immediately instead of dispatching a full wave into a lane that is
already known to be dead; the manifest's top-level `lanes` field records why.
The same in-run cooldown kicks in reactively the first time any node reports
a quota failure, so the rest of that wave does not repeat the same failure in
parallel.

Every accepted node's receipt carries a content digest: a hash of the node's
own definition (task, role, agent, route, required files, acceptance) plus
its dependencies' digests, chained the way a build cache key would be. A
plain resume replays the persisted spec unchanged, so every digest still
matches and nothing is redispatched. `fusion workflow resume WORKFLOW_ID
--spec workflow.json` instead re-validates against a workflow JSON you may
have edited; only nodes whose own definition changed, or whose dependency
evidence changed, lose their cached digest match and rerun — everything else
is reused as-is. The resolved command/model actually used is recorded on the
receipt for provenance but deliberately excluded from the digest itself, so
`orc-free`/`orc-best` re-resolving to a different model over time does not
by itself invalidate a cached node.

For the Saloon integration smoke, use the included read-only graph against a
disposable checkout or the existing `~/saloon` workspace:

```sh
fusion --workspace ~/saloon --json workflow run \
  /path/to/orc/examples/saloon-readonly.workflow.json
```

Start with this graph before adding implementation writers. It intentionally
uses three Claude inventory nodes, two Codex counterchecks, and one Claude
reviewer. If a provider reaches a session or usage limit, the command exits
with status 2 and leaves a resumable manifest under
`~/saloon/.fusion/workflows/`.

### Traces and dogfood

Every dispatched worker writes a metadata-only span to
`.fusion/traces.jsonl`. Spans include the trace and parent IDs, agent, route,
resolved model when the provider reports it, duration, status, token usage,
tests, blockers, and links to the raw run artifacts. Prompts and model output
are not copied into telemetry; inspect the run's `stdout.log` when you need
that detail. Set `telemetry.enabled` to `false` in `.fusion.json` to disable
the trace ledger.

```sh
make test                 # deterministic unit tests
make dogfood               # actual fusion CLI + fake Claude/Codex subprocesses
fusion trace --limit 50   # inspect spans
fusion usage              # aggregate tokens, latency, and reported costs
FUSION_REAL=1 make dogfood-real  # opt-in provider smoke; consumes quota
```

The real smoke command returns exit code 2 when the CLI was reached but a
provider blocked the turn for quota, authentication, or session limits. That
keeps provider availability separate from harness regressions.

### Remote telemetry (on by default)

Fusion sends a small, deliberately reduced copy of each span to
`https://orc-telemetry.fly.dev/v1/ingest` by default, with a notice before
the first send. Reporting needs no token. The shared collector shows
aggregate agent/route/model/failure patterns across installations.

To disable sending while keeping local traces:

```sh
export FUSION_TELEMETRY=0
```

Or set this in the project's `.fusion.json`:

```json
{
  "telemetry": {
    "remote": {
      "enabled": false
    }
  }
}
```

What gets sent, per dispatch: agent, role, route, model, whether it was a
write, status, a coarse `failure_class` (`quota` / `permission_denied` /
`timeout` / `missing_executable` / `worker_error` — never the raw blocker
text), timing, and token/cost usage, tagged with a random per-machine
`install_id` that isn't tied to identity. A workflow node reused from a
digest-matched receipt on resume (see digests above) never actually
dispatches, so it emits its own `cache_hit` status rather than `success` —
otherwise "how much is caching actually saving the group" would be
invisible in the exact data source built to answer that.

Never sent: prompts, model output, changed file paths, test commands, raw
blocker text, or any local filesystem or workspace path. Run `fusion
telemetry status` any time to see exactly
what is currently configured to send. A send is always best-effort with a
short timeout — a down or misconfigured collector never blocks or fails the
actual dispatch.

`fusion telemetry report [--hours N]` (default 168, i.e. 7 days) fetches
the group's aggregate patterns back from the collector — calls, cost, and
average duration grouped by agent/route/model/status/failure_class, plus
how many distinct installs contributed. Reading aggregates requires the
shared token in `.fusion.json` under `telemetry.remote.token`; distribute
it privately and never commit it. Add `--json` for the machine-readable form.

The collector itself (a small Fly + Postgres app) lives in
[`telemetry/`](telemetry/).

The architecture and source map are in [FUSION_RESEARCH.md](FUSION_RESEARCH.md).

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
