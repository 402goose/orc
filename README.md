# 🧌 ORC — Orchestrate · Review · Commit

> Big tusks. Small diffs. Show your work.

ORC combines the `orc` launcher, which runs Claude Code through OpenRouter with isolated Claude state, model selection and a cost HUD, with the `fusion` harness, which delegates bounded tasks to Claude Code, Codex, Antigravity and Grok. Fusion records worker evidence, runs persisted workflow graphs, checks declared acceptance criteria, and supports independent review and optional PR publishing. The CLI, MCP server and browser control room share those receipts. Orchestration and the UI run locally; workers use your configured provider accounts, and reduced remote telemetry is **on by default**. Optional Laya decisions add local routing and acceptance advice, initially in shadow mode.

## Capability map

| Surface | What it is for | Entrypoint |
| --- | --- | --- |
| Fusion CLI | Leads, bounded workers, builds, resumable graphs and evidence | `fusion` or `orc fusion`; [CLI reference](docs/cli.md) |
| MCP server | Drive ORC from Claude Code, Codex or another configured chat client | `fusion mcp-serve`; [MCP tools](docs/mcp.md) |
| Control room | Launch work, watch stages, inspect reports, review labels and queue fixes | `fusion ui`; [UI guide](docs/workflows.md#control-room) |
| orc launcher | Run Claude Code on an OpenRouter model with separate state and pricing | `orc`; [launcher reference](docs/launcher.md) |

Start with the CLI for one task, MCP for a lead that delegates during a
conversation, or the control room for inspecting and managing saved work.
All three Fusion surfaces use the same workflow and run artifacts.

| Detail you need | Guide |
| --- | --- |
| Installation, permissions, storage and config precedence | [Configuration](docs/configuration.md) |
| Automatic workers, routes, accounts, model/effort pairs and quota | [Routing](docs/routing.md) |
| Builds, acceptance, resume, reports and publishing | [Workflows](docs/workflows.md) |
| GitHub backlog surveys and selected issue fixes | [Truffle pig](docs/truffle.md) |
| Outcomes, labels, Laya, training and gym benchmarks | [Learning](docs/learning.md) |
| Local usage, historical headroom and outbound telemetry | [Telemetry](docs/telemetry.md) |
| Every CLI command / every MCP tool input | [CLI](docs/cli.md) · [MCP](docs/mcp.md) |

## Requirements and install

Use macOS or Linux with Python 3. The base harness and control-room server use
Python's standard library; their runtime needs no npm installation. Optional
Laya setup uses `uv` to create a Python 3.12 environment and install its dependencies.

The installer requires `claude`, `jq` and `curl` on PATH, even if you only intend
to use Fusion. Add Git and the workers you want: `codex`, `agy`, or `grok`.
Authenticate those CLIs with their own accounts. Truffle pig and PR publishing
also require an authenticated `gh` CLI. The launcher needs an OpenRouter key;
its interactive model picker needs `fzf` (the installer only warns if absent).

From this checkout:

```sh
./install.sh
# Or choose the installation directory:
DEST=/somewhere ./install.sh
```

The installer copies `orc`, `fusion`, the Fusion modules, UI assets and bundled
quality data to `~/.local/bin` by default. Add the destination to PATH.
`fusion` and `orc fusion` reach the same harness; examples below use `fusion`.

See [installation and configuration](docs/configuration.md) and
[launcher setup](docs/launcher.md#setup-and-launch-menu).

## 60-second quickstart

From the repository you want worked on, with an authenticated worker installed:

```sh
fusion doctor
# Persistently disable remote reporting if that is your preference:
fusion telemetry off
# One bounded task; choose another installed agent if needed:
fusion delegate --agent codex --read-only --role reviewer \
  --success 'Concrete findings with file references' \
  'Review the current diff for regressions. Do not change files.'
fusion status
fusion usage
```

`doctor` checks local prerequisites; it does not prove provider authentication
or remaining quota. A worker call uses its configured account and can incur
charges. `status` lists saved runs; `usage` reads local records offline.

To prepare a build without starting coding workers:

```sh
fusion build --plan-only 'Add CSV export with tests for filtering and escaping'
```

Inspect the saved brief and workflow before executing. For an interactive build,
use `fusion build 'Add CSV export with tests'`; for the browser, use `fusion ui`.
See [build behavior](docs/workflows.md#builds) and [acceptance](docs/workflows.md#the-guild).

## Three ways to run it

### Terminal

Use this for a bounded delegation, scripted runs, or a workflow you can inspect
and resume by ID. `build` opens a Codex lead by default; `lead` defaults to Claude.

```sh
fusion build 'Let users export their filtered dashboard as CSV'
fusion build --agent claude 'Add saved searches with names and deletion'
fusion --progress build --execute 'Implement CSV export and regression tests'
fusion workflow watch
fusion workflow report WORKFLOW_ID
```

Global flags such as `--workspace`, `--json`, `--progress` and `--quiet` normally
go before the subcommand. Progress uses stderr so JSON stdout stays parseable.
`workflow watch` observes saved progress; Ctrl-C detaches only the viewer.
See [workflows and terminal progress](docs/workflows.md).

### From a chat client via MCP

Use this when the conversational lead should delegate work and inspect durable
results. Fusion can start Claude Code or Codex with its MCP configuration:

```sh
fusion lead
fusion lead --agent codex
```

To connect an existing client, register `fusion` as a stdio MCP server with
arguments `--workspace /absolute/path/to/repository mcp-serve`. The corresponding
server command is:

```sh
fusion --workspace /absolute/path/to/repository mcp-serve
```

`fusion_here` reads workspace orientation without launching workers.
`fusion_run_start` returns a durable `workflow_id`; poll `fusion_run_status`,
and use `fusion_run_cancel` to request cancellation. `fusion_delegate` runs a
bounded worker, `fusion_outcome` records a verdict, and `fusion_decisions` and
`fusion_status` inspect advice and runs. Prompts and evidence resources are also
available. Client registration syntax depends on the client.

See [MCP connection and lifecycle](docs/workflows.md#driving-orc-from-a-chat-client-mcp)
and the generated [tool descriptions and input schemas](docs/mcp.md).

### Control room

Use this for workspace/history navigation, live worker updates, Markdown plans
and reports, finding-to-build actions, publishing previews, and Laya's review lab.

```sh
fusion ui
fusion --workspace /path/to/repository ui --port 0 --no-open
```

The server binds to `127.0.0.1`; connect with the complete capability URL printed
in the terminal. `--port 0` selects a free port; the default is 8765.
UI launches use the CLI's permissions, writer locks, attempt limits and acceptance
checks. Build/debug launches require the edit-consent checkbox. Detached jobs
continue after the browser or server closes; restart against the same workspace
to reconnect. Terminal runs are visible too; stop those from their original terminal.

The guild names identify actual surfaces: Gruk is the control room, Fusion the
forge, Snout the issue scout, Laya the optional classifier, the council the
label-review workers, and the garden the label-drafting and curation loop.
See [control room, shortcuts and reconnection](docs/workflows.md#control-room).

## Routing and auto mode

```sh
fusion delegate --agent auto --read-only 'Review the persistence boundaries'
fusion delegate --agent claude --route orc-free --read-only 'Inspect the diff'
```

`--agent auto` selects an eligible lane. Automatic workflow stages can fall back
when a provider reaches quota, within existing attempt and budget limits.
Explicit worker/route selections stay pinned. A named route can choose a harness,
model, permission settings and environment; direct delegation still requires
`--agent` when a route is supplied. Automatic ORC selection requires passing
`orc probe --fit` evidence unless an explicit `allow_untested` override is set.

Model and reasoning effort form a pair. Use `--model` with `--reasoning-effort`
for an explicit worker choice; see [supported pairs and evidence limits](docs/model-effort.md).
A requested pair is distinct from what a provider actually reports as applied.

Route `env` values override inherited worker variables and expand `~` and `$HOME`.
An explicit `account`, or the route's `CLAUDE_CONFIG_DIR` / `CODEX_HOME`, gives
per-account lanes independent cooldowns. Routes sharing an account share cooldowns.
Recorded quota windows demote tight lanes and exclude exhausted automatic lanes;
`fusion usage` exposes historical headroom, not a live provider guarantee.

Gym exports can supply bounded lane priors through `decisions.priors`: weight
`0.5` and cap `10` by default, read from `$ORC_HOME/lane_priors.json`.
See [routing, account examples, quota thresholds and priors](docs/routing.md).

## Workflows and builds

A build can prepare artifacts (`--plan-only`), open an interactive lead, or run a
bounded graph (`--execute`). Authored `fusion workflow` graphs support dependencies,
read-only fan-out, retries, budgets, persisted receipts and targeted resume.

```sh
fusion --json workflow run workflow.json
fusion workflow resume WORKFLOW_ID --node review --agent codex --max-attempts 3
fusion workflow report WORKFLOW_ID --finding 3
```

Acceptance checks worker status, declared artifacts and configured checks;
writers must change the repository unless explicitly allowed not to. Required
files with saved baselines must change too. A worker's successful handoff alone
is insufficient. Individual failed shell commands are retained as observations;
they do not independently make a later successful stage fail.

Resume rechecks acceptance and definition/dependency digests before reusing
receipts. Use separate Git worktrees for concurrent writers; one checkout has
one writer lock. Acceptance commands run with your local privileges.

`fusion ultra` runs a bounded explore/plan/implement/review/synthesize pipeline
with fresh stage contexts. `fusion truffle survey` maps and grades a GitHub
backlog; `fusion truffle hunt` scouts a smaller pool and `truffle run` queues
selected fixes. Grading and implementation consume worker usage.

Publishing is off by default. `manual` and `auto` publication modes prepare
isolated worktrees from the fetched target branch; auto publishes after successful
implementation and downstream review. Existing uncommitted changes are not copied.
See [workflows, acceptance and publishing](docs/workflows.md) and
[Truffle pig](docs/truffle.md).

## Evidence and learning

Review the diff and checks before recording a delegated run's outcome:

```sh
fusion outcome RUN_ID --accepted --stage verify --reason 'Regression checks and diff reviewed'
fusion outcome RUN_ID --withdraw --reason 'The grader used the wrong fixture'
fusion outcome RUN_ID --unmeasured --reason 'The grader could not run'
```

`--stage gate|verify|land` records lifecycle metadata; the latest measured verdict
wins. Withdrawal removes external verdicts and their labels, preserving independent
gate evidence. Unmeasured outcomes leave ranking and labels unchanged. A reasoned
verdict on a reported success can become an acceptance label.

Laya starts in **shadow** mode: advice is recorded without applying learned
actions. Active decisions need explicit activation and matching calibration;
missing models and uncertain/truncated input fall back to deterministic policies.
`FUSION_DECISIONS_MODE=off` disables the classifier. Draft labels do not enter
training exports. Training creates candidates; it does not promote them automatically.

`fusion learn status` inspects the loop; `fusion learn tick` advances enabled
labeling/training without the UI. `fusion learn schedule` provides periodic ticks.
`fusion gym` extracts merged fixes, compares lanes on replayed tasks with hidden
tests, and exports routing priors. Gym runs can consume paid provider usage.

See [outcomes, labels, Laya and gym](docs/learning.md), the
[learning roadmap](docs/learning-roadmap.md), and [decision design](FUSION_DECISIONS.md).

## Usage and telemetry

```sh
fusion usage --since 24h --by session
fusion usage --since 7d --by model --top 10 --json
fusion --control-workspace /path/to/controller usage
fusion trace --limit 50
```

`usage` reads Fusion traces plus local Claude and Codex transcripts offline.
Cross-source totals can describe the same work twice; unreported cost is unknown,
not zero. `--record` saves at most one full snapshot per UTC day. A separate
`--control-workspace` keeps receipts after disposable worker checkouts disappear.

**Remote telemetry is on by default**, sent to `https://orc-telemetry.fly.dev/v1/ingest`
with a first-send notice. Disable it persistently or for one invocation:

```sh
fusion telemetry off
FUSION_TELEMETRY=0 fusion delegate --agent codex --read-only 'Review the diff'
```

The reduced payload includes schema/install ID, trace/span IDs, agent, role,
route, model, write/status/failure class, timing and normalized token/cost usage.
It omits dedicated prompt/output, file-path, test-command and raw-blocker fields;
configured identifiers such as role/route/model strings are copied directly.
Sending is best-effort but synchronous, with a three-second network timeout.
Local traces retain more evidence; `telemetry.enabled: false` disables both.

`fusion telemetry status` reports configuration, not effective environment opt-out.
`fusion --json telemetry report` reads this installation's remote summary.
See [payload details, reporting, quota and accounting](docs/telemetry.md).

## Launcher summary

Run `orc` for first-time key/model/permission setup and the saved launch menu.
`orc models --tools` lists catalog tool support; `orc probe --fit MODEL` measures
a tool loop. `orc @PROFILE` and `orc -m MODEL` override a launch; `orc status`
shows resolution, and `orc save NAME` snapshots it. Project `.orc.json` can pin it.

`orc stats` aggregates isolated Claude transcripts. The HUD prices parent and
subagent usage with the cached OpenRouter catalog, including resumed-session
spend. These are estimates, not a billing statement. `orc env` prints resolved
exports **including your key**. See [commands, profiles, keys, HUD and config](docs/launcher.md).

## Configuration summary

Fusion layers defaults, `$ORC_HOME/fusion.json` (normally `~/.config/orc/fusion.json`),
and `FUSION_CONFIG` or the upward-discovered project `.fusion.json`. An explicit
control workspace can override `routes`, `decisions`, `learning`, `quota`, `cache`
and `gym`; other worker settings stay worker-scoped.

`execution_mode` defaults to `restricted`. `yolo` applies worker permission
bypasses; read-only roles then describe intent rather than runtime isolation.
AGY has an explicit per-lane bypass setting even in restricted mode.
The launcher has separate `config.json`, profiles and `.orc.json` precedence.
See [configuration and native worker permissions](docs/configuration.md).

## Development

```sh
python3 scripts/gen_docs.py
python3 scripts/gen_docs.py --check
make test dogfood
./test/run.sh
make test-ui
```

The [generator](scripts/gen_docs.py) derives [CLI](docs/cli.md) and [MCP](docs/mcp.md)
references from the runtime definitions. The [drift test](test/docs_generated_test.py)
runs in `make test`; regenerate both files after changing those interfaces.
The Python runner rejects unexpected skips and expected failures, with one
explicit allowance for an unavailable real Codex sandbox probe.

Launcher sources are `orc`, `hud.sh.in` and `pricing.jq`; `./build.sh` assembles
the shipped scripts. See [launcher development](docs/launcher.md#development)
and [browser verification](docs/workflows.md#development-verification).
`FUSION_REAL=1 make dogfood-real` is an opt-in provider smoke that consumes quota;
exit 2 means provider availability blocked the turn. Architecture lives in
[FUSION_RESEARCH.md](FUSION_RESEARCH.md).

## Caveats

Tool support advertised by a catalog is not proof of a working tool loop.
Provider/model availability, caching, thinking and retention policies must be
checked with the selected provider. Example model IDs and tables are illustrative.
Quota observations are historical; absent cost and usage must remain unknown.
Verification proves only the checks actually run; review worker evidence before
publishing or turning an outcome into a label.

See [launcher caveats](docs/launcher.md#caveats), [evidence limits](docs/learning.md),
and the [migration audit and corrected claims](docs/documentation-audit.md).
