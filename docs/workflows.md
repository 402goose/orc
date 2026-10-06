# Workflows, builds and the control room

[Start here](../README.md) · [CLI reference](cli.md) · [MCP reference](mcp.md)

- [The guild](#the-guild)
- [Control room](#control-room)
- [Publishing reviewed work](#publishing-reviewed-work)
- [Fusion — the forge](#fusion--the-forge)
- [Driving ORC from a chat client (MCP)](#driving-orc-from-a-chat-client-mcp)
- [Builds](#builds)
- [Progress and watching](#progress-and-watching)
- [Direct delegation and worker evidence](#direct-delegation-and-worker-evidence)
- [Ultra without the token fire](#ultra-without-the-token-fire)
- [Persisted fan-out workflows](#persisted-fan-out-workflows)
- [Development verification](#development-verification)

## The guild

Six characters, one pipeline. They appear by name in the control room under
**Meet the guild**.

| Who | Role | What it actually is |
| --- | --- | --- |
| **Gruk** | the forgekeeper | the control room — watches every run and keeps the receipts |
| **Fusion** | the forge | the harness: leads, workers, the workflow graph, acceptance gates |
| **Snout** | the truffle scout | reads open GitHub issues and shortlists the tractable ones |
| **Laya** | the lorekeeper | optional on-device classifier that learns routing and acceptance |
| **The council** | keepers of the seal | two to four workers that must agree unanimously before a label is approved |
| **The garden** | where lessons grow | continuous label drafting, quality checks, and example curation |

A run moves left to right, and nothing advances on a worker's say-so alone:

```
 request or issue
        │
     intake ──► routing ──► workers ──► acceptance ──► review ──► publish
                            (parallel)   (declared     (separate   (opt-in,
                                           files +       worker)     off by
                                           checks)                  default)
```

`STATUS: success` from a worker is a self-report. A stage is accepted only when
its declared `required_files` exist (and differ from their baseline when one is recorded),
— an entry may name a directory, which counts as present when it holds at least one file and
as changed when any file under it was added or edited; the comparison is by content, so a worker
(or a hook in its environment) committing its own output does not hide the change; symlinks
under a required directory are skipped, so a link never makes it present and its target is never
read; a directory is hashed in full on every check —
its acceptance commands exit zero, and — for
a node that may write — the repository actually changed. Structural signals
(`turn.failed`, `is_error`) override a "success" claim. Individual nonzero
`command_execution` exits are observations, not automatic stage failures. The gap between what a worker says it did and what the
run can actually prove is the point of the whole thing.

The change check reads Git, not the worker's `CHANGED` line, because a worker
can misreport that. A writer with nothing to do can opt out with
`acceptance.allow_no_changes`; a workspace without Git abstains rather than
failing every write.

Acceptance checks may be plain argv arrays or objects with a pinned evaluator:

```json
{"acceptance": {"checks": [{"argv": ["python3", "/opt/evaluators/acceptance.py"], "sha256": "<64 hexadecimal characters>", "min_tests": 15}]}}
```

`sha256` pins the first existing file named by an argv element when the runner
initializes, or the file named by an explicit `path` key. Relative paths resolve
against the workspace. A bare command name in argv[0], such as `python3`, is
looked up through PATH and is excluded from this file selection. Use `path` when
an absolute interpreter path would otherwise be selected, or when the evaluator
does not exist yet (for example, an acceptance fixture). Without an identifiable
file or explicit `path`, validation fails before dispatch. The selected path is
saved in node state and reused across retries and resumes, so a worker cannot
redirect the digest check by planting a file named after an earlier argument.
Older runs without saved paths must provide an explicit `path` to resume a
declared hash check; Fusion does not infer one from the already modified tree.
The coordinator checks the digest after applying acceptance fixtures and before
launching the command. A missing or changed evaluator fails with
`check_contract_changed`: the acceptance contract changed and needs explicit
review/versioning. Optional `min_tests` requires an observed unittest `Ran N tests`
or pytest terminal summary with at least that many executed tests; skipped and
deselected pytest tests do not count. A shortfall or unrecognized count fails with
`check_tests_missing`. Objects also work with `before` and `fail_to_pass`.

For write nodes, Fusion saves hashes of existing workspace files named by check
arguments and files selected by `python3 -m unittest discover -s DIR -p PATTERN`
(default pattern `test*.py`) before the first attempt. Retries and resumes retain
those hashes. Editing or deleting an input records `check_inputs_changed` in the
node and its check receipts. Passing checks still accept the node, but the run
cannot supply positive structural labels or verified routing evidence. New test
files do not trigger this flag. Plain argv checks remain supported; workspace
inputs produce a dispatch warning and a report line. Keep trusted evaluators
outside the worker's writable workspace and pin them explicitly when their
integrity must be a hard gate. Hashes detect changes; they do not sandbox workers.
Automatic pins do not expand pytest directory arguments. A check that rewrites
its own inputs during the before-phase run will also trigger the integrity flag.
Test counts come from evaluator output, so the evaluator must be trusted to
report them honestly.

In a generated `build`, the planning node declares what the implementation must
produce, as one fenced `acceptance-contract` block:

```acceptance-contract
{"required_files": ["src/export.py", "tests/test_export.py"], "verification": [["python3", "-m", "pytest", "tests/test_export.py"]]}
```

Those files become `required_files` on the implementing node, so it is gated on
producing them. Paths are workspace-relative and validated — absolute paths,
`~`, and `..` are refused — and a malformed contract is ignored rather than
widening what is enforced. `verification` is recorded for the reviewer to rerun,
and the commands that pass a conservative policy also run as the implementing
node's acceptance checks: argv only (no shell), an allowlisted test runner by
bare name, no installs, publishing or inline code, offline where the tool
allows. Each runs on the tree before the change and again after it; a check
that already passed before is `vacuous` and proves nothing. They run
unsandboxed with your privileges, like the tests the worker runs. See
[executed plan verification](../FUSION_DECISIONS.md#executed-plan-verification).
Planning contracts cannot introduce executable `checks`; only authored workflow
specifications may declare them.

## Control room

The control room wears the ORC guild identity: the supplied tusked logo, a
forgekeeper and truffle-scout illustration, and local vector sigils across
navigation, workflows and Laya. **Meet the guild** opens the characters' field
guide. All ten palettes tint the artwork and icons, including the favicon;
light/dark/system settings still persist locally. Decorative motion respects
reduced-motion preferences. Assets and the illustration prompt live in
[`fusion_ui_assets/brand`](../fusion_ui_assets/brand/README.md).

```sh
cd /path/to/your/repository
orc fusion ui
# Or select the repository explicitly:
orc fusion --workspace /path/to/repository ui --port 8765
```

The control room opens in your browser and reads the same `.fusion` artifacts
as the CLI. Existing terminal runs appear automatically. It includes:

- Workspace switching, searchable workflow history, live stage activity and worker updates.
- Rendered Markdown plans and reports, tables, copyable code blocks, evidence,
  report downloads, and **Implement this** actions on numbered findings.
- Discovery, build, debug, review, single-worker and custom JSON workflow launches;
  preparation without execution; resume and cancellation of UI-launched jobs.
- **Truffle pig** scouts a chosen pool of open GitHub issues, checks source citations,
  and ranks a target number of tractable fixes. Inspect the shortlist, select issues,
  and queue separate implementation/review workflows with a chosen PR target branch.
- A Laya lab for probes, probabilities, actual policy actions, reviewed labels,
  dataset exports, training, evaluation with a shuffled-state control, and calibration.
- Project Fusion/ORC settings, worker availability, ORC model listings and profiles.
- Ten palettes (Grove, Ocean, Iris, Rose, Amber, Glacier, Mint, Sand, Slate, Ember)
  with Light, Dark, and System modes. Open **Appearance** in the sidebar or
  **Settings → Appearance**. Preferences persist in localStorage and sync across tabs.
- Activity entries subtly animate only when new, respect reduced motion, and
  follow output smoothly while preserving your position when you scroll up.
  Worker updates render as Markdown cards; consecutive commands collapse into
  groups with expandable command/output receipts. **Latest** catches up to the
  live tail. Grok uses structured message/tool updates; older plain-text runs
  remain visible, and **Last output** shows when worker logs last changed.
  Technical logs and stage history live under **Run details**, closed
  by default; expanded details stay open across refreshes.

UI launches use the existing CLI and its permissions, writer lock, attempt limits
and acceptance checks. Build/debug runs require the **Allow this run to edit workspace files**
checkbox (worded per surface: "this workflow" for custom graphs, "the selected
fixes" for Snout's queue); preparation starts no coding workers. Jobs continue when the browser
or UI server closes. Restart the UI against the same workspace to reconnect.
Runs started outside the UI are observable; stop those from their original terminal.
Training and calibration never promote a model or enable active decisions automatically.

If a restart expires a browser connection, click **Reconnect** and paste the full
URL printed by `orc fusion ui`. Successful credentials are remembered for that
local origin; opening the current URL in one tab reconnects other tabs in the same
browser. Unsaved label edits remain intact, and failed actions are never replayed.

The server binds only to `127.0.0.1`. Use the complete URL printed in your terminal
to connect a browser; its fragment carries a per-machine access capability, stored at
`$ORC_HOME/ui-token` and reused across restarts.
`--no-open` prints that URL without opening a browser. `--port 0` chooses a free
port. The runtime needs only Python's standard library; Markdown rendering and
sanitization assets ship locally. There is no npm install, CDN, or hosted UI service.

Keyboard shortcuts: **N** new run, **1–5** sections, **/** workflow search,
**Esc** close dialog, **?** shortcut reference.

For the lab, council and garden, see [Evidence and learning](learning.md).
For Snout’s field guide, see [Truffle pig](truffle.md).

## Publishing reviewed work

Completed implementation workflows have an **Open PR** action. Choose a target
branch and remote, preview the exact diff, edit the title and Markdown body, and
publish a draft or ready PR. Older runs require confirmation of the preview
because they predate saved Git review snapshots. Only their recorded implementation
paths are carried into the publication worktree; unrelated local changes and
`.fusion` artifacts stay out of the commit. PR publication uses Git and the local
GitHub CLI account, with no additional coding-worker calls.

Configure **Settings → GitHub publishing**, or set project `.fusion.json`:

```json
{
  "publish": {
    "mode": "auto",
    "base": "staging",
    "remote": "origin",
    "draft": true
  }
}
```

`off` keeps ordinary workflow execution in the current workspace. `manual` and
`auto` start bounded implementation workflows in a clean Git worktree from the
fetched target branch. Existing uncommitted changes in the original checkout are
not copied into new runs. Dependencies may need installation in the new worktree.
Manual runs wait for **Open PR**; automatic runs commit, push a feature branch and
create the PR after successful implementation and downstream review. Reviews
record their Git tree; changed files invalidate publication until reviewed again.
These modes apply to persisted workflows (`build --execute` / `workflow run`).

```sh
# Override publishing defaults for one bounded implementation run:
orc fusion --progress build --kind build --execute \
  --publish auto --base staging --draft 'Implement the requested feature with tests'

# Preview a completed workflow without committing, pushing, or opening a PR:
orc fusion --json workflow publish WORKFLOW_ID --base staging --preview

# Publish a run with a saved review snapshot, or retry its failed publication:
orc fusion --progress workflow publish WORKFLOW_ID
# Older runs additionally require --accept-legacy-diff after inspecting the preview.
```

Publication state is stored separately in the workflow directory. A failed push
or GitHub call leaves accepted stages intact; retries retain the branch and target
and recover an existing PR instead of creating duplicates. Commit hooks run normally.
The UI displays the PR URL, commit, target and worktree; **Refresh PR checks** fetches
current GitHub checks. Changing the target of an isolated run requires a new run
against that target. Automatic publication failures return a nonzero CLI exit code
while the implementation workflow remains successful.

Runtime permissions and configuration layering are in [Configuration](configuration.md).

## Fusion — the forge

Fusion is the harness the rest of the guild is built on. It keeps a lead agent
in charge of the conversation and the final review, then delegates bounded work
to Claude Code, Codex, Antigravity or Grok through a shared task contract. Each
run records its task, JSONL events, stdout, stderr, result, and reusable session
id under `.fusion/` in the workspace — so every claim in a report has a receipt
behind it. Trace spans also record the session key, whether the run resumed,
idle time since that session's last run and the cache-read ratio; an optional
`cache` config can start fresh instead of cold-resuming and let warm lanes break
ties in routing (see [Prompt cache and sessions](../FUSION_DECISIONS.md#prompt-cache-and-sessions)).

The lead can call the other agent through an MCP server:

```sh
fusion lead                 # Claude leads by default
fusion lead --agent codex   # Codex leads and can call Claude
fusion doctor
fusion trace --limit 50
fusion usage --limit 1000
```

## Driving ORC from a chat client (MCP)

`fusion mcp-serve` exposes ORC over the Model Context Protocol, so an agent in a
chat client drives the same `.fusion/` artifacts the CLI and control room read.
Three primitives, mapped onto what ORC already is:

| Primitive | What it exposes |
| --- | --- |
| **Prompts** | the verbs — `where-am-i`, `ship-feature`, `review-changes`, `explain-run`; client UI determines how they appear |
| **Resources** | the evidence — `orc://here`, `orc://workflows`, `orc://workflow/{id}/manifest`, `.../report` |
| **Tools** | the operations — `fusion_here`, `fusion_run_start`, `fusion_run_status`, `fusion_run_cancel`, plus `fusion_delegate`, `fusion_outcome` (the lead's verdict), `fusion_decisions`, and `fusion_status` |

Long-running work uses **durable handles rather than blocking calls**.
`fusion_run_start` returns a `workflow_id` immediately; poll `fusion_run_status`
with it. The handle is the workflow directory on disk, so it survives a restart
of both the server and the client, and a result hands back `resource_link`
evidence URIs instead of inlining artifacts.

`fusion_here` answers "where am I" for a workspace — active workflows, what
failed, what it cost, Laya's mode, and the next command — reading artifacts only.
It starts nothing and contacts no provider.

Start an interactive client with `fusion lead` (Claude by default) or
`fusion lead --agent codex`; Fusion supplies the MCP configuration. To attach
an existing client, register a stdio server whose executable is `fusion` and
whose arguments are `--workspace /absolute/path/to/repository mcp-serve`.
The client's configuration syntax depends on that client; the server uses
ordinary MCP prompts, resources and tools, with persisted workflow handles.
See [all tool descriptions and input schemas](mcp.md).

## Builds

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

## Progress and watching

Terminal runs show live progress: Laya startup and recommendations, stage and
worker selection, public worker updates, acceptance results, and elapsed-time
heartbeats every ten seconds. Worker stdout/stderr logs are written while the
worker runs. Progress goes to stderr; `--json` keeps stdout machine-readable.
MCP clients can opt into the same heartbeat by supplying
`params._meta.progressToken` (a string or number) on a `tools/call` request.
While the call has active work, ORC sends `notifications/progress` about every
10 seconds, with the token, elapsed request seconds as `progress`, and the
current activity and latest public worker event as `message`. Each notification
is flushed as a complete JSON-RPC line before the final response. No token
means no notifications. Clients must support renewing their request timeout on
progress; the worker timeout is a separate limit. Asynchronous
`fusion_run_start` returns immediately and uses `fusion_run_status` polling.

For a single delegation, use `fusion delegate --timeout 7200 ...` or set
`timeout_seconds: 7200` in the MCP `fusion_delegate` arguments. Overrides must
be integers from 60 through 14400 seconds; invalid values are rejected before
launching a worker. Omit the override to use the configured `timeout_seconds`
(default 3600). The effective limit is saved in the run's `task.json` and
`result.json`, and expiration uses the existing worker timeout failure
(status `blocked`, exit code 124, the stream captured so far kept as evidence).
A worker that times out, is cancelled, or is aborted is stopped with its whole
process tree: its process group and every descendant, including commands that
left the group for their own session, get SIGTERM, then SIGKILL after a
three-second grace. A worker that exits on its own is not cleaned up after;
anything it deliberately left running keeps running.

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

## Direct delegation and worker evidence

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
command returns nonzero, its receipt remains in `command_evidence` and the UI's
**Evidence → Command observations**. Searches with no matches, corrected file
lookups, and tests run before a fix do not become permanent blockers. Unresolved
handoff blockers, provider errors, required evidence, and configured acceptance
checks still determine whether a stage passes.

Caveats that do not block go under the handoff's `LIMITATIONS:` label, which the
result keeps as `limitations` and no gate reads. A `BLOCKERS:` value that opens by
declaring nothing blocks ("none blocking", "No blocker to this check.",
"Non-blocking: …", or a scoped "None for this node." / "none for the assigned
investigation." followed by a sentence) is treated as empty, and the rest of it
moves to `limitations`. It still gates when the field carries a contrast or
unresolved-work word (but, however, except, although, though, yet, still,
requires, needs, pending, awaiting, outstanding, must): "No blockers, but the
suite fails" and "None for this node. The deploy still requires approval." gate. A bare "none." followed by
a failure word ("none. The result is unverified") also still gates.

## Ultra without the token fire

Ultra is an explicit bounded pipeline modeled after the useful part of
[UltraCode](https://github.com/diepquynh/ultracode): explore, plan, implement,
review, and synthesize. It uses fresh stage contexts and JSON handoff files in
`.fusion/ultra/`, so later stages read evidence instead of inheriting every
earlier transcript. The stage count is capped, writes are serialized, and the
`orc-best` route caps its Claude call with `--max-budget-usd`. `orc-free` is
deliberately left uncapped: that flag prices tokens at Anthropic list rates,
which would only sabotage a free lane.

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
than a direct lead/sidekick run. Use `fusion delegate --agent claude --route orc-free "Review the diff"`
for one bounded cheap worker, or `fusion delegate --agent claude --route orc-best "Review the diff"` when the stage needs a
stronger model. `--harness codex` runs every configured stage through Codex;
`fusion lead --agent codex` makes Codex the interactive lead and exposes the
same MCP delegation tools for Claude workers.

## Persisted fan-out workflows

For real orchestration, use `fusion workflow` with a JSON graph. Unlike the
linear Ultra preset, a workflow can map one node over many items, fan in on
explicit dependencies, run independent read-only nodes concurrently, retry
invalid handoffs, send review blockers back to the writer for repair, pause on provider quota, and resume from the persisted
manifest without repeating accepted nodes.

Git snapshot failures are coordinator errors: Fusion stops without spending
more worker attempts or switching providers, and shows the underlying error
instead of missing-handoff warnings. After repairing it, resume the affected
stage. Snapshots preserve tracked files under ignored directories and leave the
real Git index untouched; ignored generated files stay excluded.

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
an explicitly reported zero. Review repair loops show the ordered attempts:
implement → review → implement (repair) → review. The manifest's `attempt_ledger`
and structured report retain the source review and repair provenance.
Reporting starts no agents and leaves receipts intact.

```sh
orc fusion workflow report WORKFLOW_ID                # final output
orc fusion workflow report WORKFLOW_ID --finding 3    # one recommendation + next command
orc fusion workflow report WORKFLOW_ID --node explore # supporting investigation
orc fusion workflow report WORKFLOW_ID --all          # every stage's full answer
orc fusion workflow report WORKFLOW_ID --brief        # compact status
orc fusion workflow report WORKFLOW_ID --output report.md
orc fusion --json workflow report WORKFLOW_ID         # structured outputs and provenance
orc fusion --progress build --from-workflow WORKFLOW_ID --finding 3 --plan-only
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

When a read-only review reports unresolved blockers in a valid handoff, its
normal `repair` action can reopen a successful direct write dependency. Fusion
chooses `independent_of` when it names a writer, otherwise the first writer in
`needs` order. Both nodes must have attempts remaining. The writer receives the
original review blockers appended to its prompt, keeps its node baselines and
route exclusions, and runs through the normal scheduler and budget checks;
the review waits for that repair to succeed before running again. Each review
and writer can participate in only one such repair loop, persisted across resume.
Quota, provider errors, missing handoff fields and other harness failures retain
their existing retry, switch or pause behavior without reopening the writer.

Before dispatching, the runner preflights the account-aware lanes used in the
graph. It checks resolved commands on PATH and, on a fresh `run`, reads the
latest 50 traces and cools a lane only when that lane's most recent trace reported
a quota/session limit within 15 minutes; `auto` nodes are not preflighted.
Route account identity determines which pending nodes share a cooldown;
the manifest's `lanes` field records the reason. `resume` skips historical
preflight cooldown, but still checks executables and applies in-run cooldowns
and routing constraints. A new quota failure cools the affected lane so the
remaining wave does not repeat that failure in parallel.

Every accepted node's receipt carries a content digest: a hash of the node's
own definition (task, role, agent, route, required files, acceptance) plus
its dependencies' digests, chained the way a build cache key would be. A
plain resume replays the persisted spec but reruns acceptance validation before
reuse. Missing or unchanged required artifacts and failing checks can invalidate
an otherwise matching receipt. `fusion workflow resume WORKFLOW_ID
--spec workflow.json` instead re-validates against a workflow JSON you may
have edited; only nodes whose own definition changed, or whose dependency
evidence changed, lose their cached digest match and rerun — everything else
is reused if acceptance still passes. Dynamic `orc-free`/`orc-best` model
resolution alone does not invalidate a receipt. Explicit Codex model/effort
pairs and native-delegation authorization do enter the definition digest when
effort is configured; changing those choices invalidates it. Resolved worker
metadata remains on the receipt for provenance.

For a first integration smoke, use the bundled read-only graph against a
disposable checkout:

```sh
fusion --workspace /path/to/checkout --json workflow run \
  /path/to/orc/examples/readonly.workflow.json
```

Start with this graph before adding implementation writers. It intentionally
uses three Claude inventory nodes, two Codex counterchecks, and one Claude
reviewer. If a provider reaches a session or usage limit, the command exits
with status 2 and leaves a resumable manifest under the workspace's
`.fusion/workflows/`.

## Development verification

```sh
make test dogfood
./test/run.sh
make test-ui
npm install --prefix /tmp/orc-ui-tools --no-audit --no-fund @playwright/test@1.63.0
/tmp/orc-ui-tools/node_modules/.bin/playwright install chromium
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/ui_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/garden_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/laya_tabs_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/training_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/council_approval_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/council_available_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/ui_connection_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/truffle_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/truffle_survey_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/publish_browser.cjs
```

`make test-ui` unit-tests the control room's pure decision logic
([`fusion_ui_assets/logic.js`](../fusion_ui_assets/logic.js)) with no browser and
no server — escaping, URL allowlisting, credential and token handling, label
approval gating, filters and elapsed-time formatting. It installs vitest outside
the repo on first run, so the shipped runtime stays npm-free.

Browser checks use disposable workspaces and fake coding workers. They exercise
rendering, launches, settings, cancellation, reviewed labels and mobile layouts
without paid agent calls. Browser tools are needed only to run these checks.
