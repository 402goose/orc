# 🧌 ORC — Orchestrate · Review · Commit

> Big tusks. Small diffs. Show your work.

**Orchestrate** a fleet of coding agents across a dependency graph. **Review**
every stage against structural evidence before it counts as done. **Commit** the
result as a pull request from an isolated worktree — when you ask for it;
publishing is off by default.

ORC is two tools that grew together:

- **The guild** — a local orchestration harness. Point it at a request or an
  open GitHub issue and it plans the work, fans it out across Claude Code,
  Codex, Antigravity and Grok, gates every stage on declared files and
  acceptance checks, has an independent worker review the result, and leaves a
  receipt for all of it. It comes with a browser control room, a persisted
  workflow graph that resumes on content digests, and an optional on-device
  classifier that learns your routing and acceptance calls.
- **The launcher** — the original `orc`: run Claude Code against any OpenRouter
  model with one command, with live pricing, a cost HUD, and Claude state kept
  fully isolated from your normal Anthropic login.

Everything runs locally against your own agent accounts. There is no hosted
service and no npm install — the runtime is Python's standard library.

| I want to… | Start here |
| --- | --- |
| Run agents against a task or a GitHub issue | [The guild](#the-guild) |
| Watch runs in a browser | [Control room](#control-room) |
| Turn open issues into reviewed PRs | [Truffle pig](#truffle-pig) |
| Run Claude Code on a non-Anthropic model | [The launcher](#the-launcher) |
| See what it all cost | [Stats](#stats) · [The HUD](#the-hud) |

## Requirements

Both halves need macOS or Linux and Python 3 (standard library only — nothing
to `pip install`).

[Claude Code](https://claude.com/claude-code) (`claude`) is required by
`install.sh` regardless of which workers you plan to use — it exits if `claude`
is not on PATH.

**For the guild**, add `git` and whichever workers you want to dispatch to:
Codex (`codex`), Antigravity (`agy`) or Grok Build (`grok`). Snout and PR
publishing also need an authenticated [GitHub CLI](https://cli.github.com)
(`gh`).

**For the launcher**, you need `jq`, `curl` and an
[OpenRouter](https://openrouter.ai) API key. `fzf` is optional but the
interactive model picker needs it (`brew install jq fzf`, or `sudo apt-get
install jq curl fzf`).

## Install

```bash
./install.sh
```

Copies `orc` to `~/.local/bin` (override with `DEST=/somewhere ./install.sh`)
and checks dependencies. To go straight to the guild, run `orc fusion ui` from
a repository — the control room needs no OpenRouter key unless you use the
`orc-free` / `orc-best` routes.

To set up the launcher:

```bash
orc
```

First run launches the setup wizard: it finds your API key (or helps you set
one up), fetches the live OpenRouter model catalog, and gives you an fzf
picker with per-model pricing and context sizes. It also offers a launch
permission mode (default / auto / acceptEdits / plan / dontAsk / yolo /
bypassPermissions).
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
its declared `required_files` exist, its acceptance commands exit zero, and — for
a node that may write — the repository actually changed. Structural signals
(`turn.failed`, `is_error`, a nonzero `command_execution` exit code) override a
"success" claim outright. The gap between what a worker says it did and what the
run can actually prove is the point of the whole thing.

The change check reads Git, not the worker's `CHANGED` line, because a worker
can misreport that. A writer with nothing to do can opt out with
`acceptance.allow_no_changes`; a workspace without Git abstains rather than
failing every write.

In a generated `build`, the planning node declares what the implementation must
produce, as one fenced `acceptance-contract` block:

```acceptance-contract
{"required_files": ["src/export.py", "tests/test_export.py"], "verification": ["pytest tests/test_export.py"]}
```

Those files become `required_files` on the implementing node, so it is gated on
producing them. Paths are workspace-relative and validated — absolute paths,
`~`, and `..` are refused — and a malformed contract is ignored rather than
widening what is enforced. `verification` is recorded for the reviewer to rerun
and is **not** executed by the orchestrator: acceptance `checks` run unsandboxed
with your privileges, so only an authored workflow may supply them.

Start here, from the repository you want worked on:

```sh
orc fusion ui                       # the control room, in your browser
orc fusion build "Add CSV export to the dashboard, with tests"
orc fusion --progress truffle hunt --count 5 --search 'label:bug'
```

### Control room

The control room wears the ORC guild identity: the supplied tusked logo, a
forgekeeper and truffle-scout illustration, and local vector sigils across
navigation, workflows and Laya. **Meet the guild** opens the characters' field
guide. All ten palettes tint the artwork and icons, including the favicon;
light/dark/system settings still persist locally. Decorative motion respects
reduced-motion preferences. Assets and the illustration prompt live in
[`fusion_ui_assets/brand`](fusion_ui_assets/brand/README.md).

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

Laya lab has six focused tabs: **Overview** (model and teaching progress),
**Review** (decisions and labels), **Garden** (automation and live council runs),
**Training** (automatic rounds, live training, and saved candidates), **Quality** (data health),
and **Results** (measured model comparisons). Tabs update the URL without reloading
and support browser Back/Forward. Reload or share a link to return to the same
registered workspace and tab; Review links also retain the decision and queue filter.
For example, `#decisions/garden?w=<workspace-id>` opens the garden directly.
Unsaved review edits survive tab changes within the open page; save them before
reloading. Use Left/Right or Home/End while a tab is focused to move between sections.

In **Laya lab → Review**, select a decision and click **Suggest labels**. Choose Auto or an
installed Codex, Claude, AGY, or Grok worker. A background job reads a snapshot of
the original input and that attempt's saved result/answer, then drafts labels with
reasons and evidence references. The teacher does not receive Laya's predictions,
policy choices, or previous labels. It can abstain when evidence is missing.
No repository changes or new verification runs are requested. This uses your
configured worker account and can incur provider usage.

Individual label editors inherit the garden’s drafting and approval settings unless
you choose different settings for that decision. Choose **Label approval → Council
approves unanimous answers** to switch an existing draft to council assessment,
then click **Run council & approve**. Already approved labels show their saved status;
the correction form does not require another approval.

By default, review the evidence, edit any answers or explanation, and click **Approve labels**.
Drafts never enter training exports. Approval records the worker/model/run, draft
identity, final labels and human evidence; regenerating does not replace existing
approved labels. In-progress edits survive polling and navigation within the tab.
Failed or cancelled jobs retain logs and can be retried with another worker.
The Laya tab tracks retained approved answers, coverage by decision type, label
balance, workflow-group splits, dataset exports, trained candidates and evaluations.
The configured checkpoint is shown separately from candidate models. Approval
feeds automatic training when enabled; manual export, training, and evaluation
remain available. Choose a checkpoint in project settings after evaluation. Candidate cards
show held-out accuracy and the shuffled-state control; improvement is reported
only when the source model was evaluated on the same held-out benchmark. Historical
prediction/label agreement is explicitly separate from held-out evaluation.

**Training data health** shows distinct inputs, workflow groups, recorded review
evidence, edits at approval, answer revisions, missing classes, and label sources.
Duplicate inputs and conflicting labels link back to the examples for correction
or exclusion. **Measured results** pairs exact source/candidate model identities
on identical held-out inputs and labels, with sample counts, results by decision
type, a training-majority baseline, and shuffled-state control. Export and training
jobs save data-quality snapshots. New candidates record their local training
ancestry; evaluation flags overlapping inputs or groups and withholds improvement
claims for known leakage. Older candidates without this metadata show an unknown
audit status. Repeated use of a small benchmark still does not prove generalization.

**Laya lab → Training → Enable auto-training** starts a persistent local learning
loop. Each round exports approved answers, cleans the dataset, measures the configured
source model, trains a separate candidate, evaluates it on the same held-out benchmark,
and saves calibration. The first eligible round starts immediately; subsequent rounds
need ten new or changed approved answers by default, configurable in **Training settings**.
Reapproving unchanged answers does not trigger another round. There is no daily cap.

The training grounds show knowledge XP (retained approved answers), levels, animated
round stages, live optimizer steps and loss, and paired source/candidate accuracy
charts. Loss measures training fit; held-out trials measure improvement. Regressions
and flat results stay visible. A changed benchmark gets its own comparison instead of
a misleading trend line. Receipts show sample counts, shuffled-state and majority
controls, exact model identities, dataset cleanup, and links to every job.

Automatic rounds keep one copy of identical inputs, preferring an existing held-out
copy, and withhold conflicting inputs. Original labels and exports stay intact.
At least two training and two held-out workflow groups must remain after cleanup.
Small samples are identified as early measurements; duplicate removal alone does
not establish generalization. Known leakage or unknown model lineage prevents an
improvement claim. Candidates are never automatically promoted.

The loop runs while the control-room server is running, including with the browser
closed. Pausing prevents subsequent steps; the active job may finish. A failed or
cancelled job waits for **Retry this step**. Saved rounds resume after a server restart,
and manual and automatic learning jobs share one workspace slot. Settings, curated
datasets, progress, and round receipts live under `.fusion/decisions/training`;
individual jobs remain under `.fusion/ui/jobs`. **Manual training tools & saved
candidates** keeps the original controls available below the training grounds.

**Laya lab → Garden → Enable auto-drafts** queues incoming complete decisions for a
labeling worker. There is **no daily cap**, including for gardens with an old saved
limit. Optionally include existing eligible decisions and drafts in the selected setup.
Garden runs one assessment at a time while the control-room server is running, even
with the browser closed. Settings, attempted decisions and daily usage survive
restart. Pausing stops new calls and withdraws automatic approval from the active
garden run; its assessment can finish as a draft or be cancelled from the live view.
Provider usage may be charged to your worker account.

Choose **Agent council** in Garden settings or an individual label editor to use
two to four distinct local workers. Every member receives the same evidence in a
fresh session, without prior votes, predictions, or labels. Members run sequentially;
each uses one worker call. Only unanimous, evidence-citing answers become suggested
labels. Disagreements, abstentions, and member failures remain visible with individual
reasons and run/model provenance. Agreement alone is not proof of correctness.

To run labeling end to end, choose **Label approval → Council approves unanimous
answers**. This is opt-in, in Garden settings or for an individual assessment.
The default agreement rule requires every selected member. Choose **Council agreement
→ Available members agree · minimum two** to keep going when a member hits a quota,
times out, lacks a runtime, or cannot get permission. At least two independent members
must supply matching, evidence-citing answers; every participating member must agree.
Disagreements, abstentions and invalid assessments remain pending. Recent quota failures
use the existing 15-minute cooldown. Failed/skipped members and the selected agreement
rule stay in the audit trail and training provenance. CLI equivalent:
`fusion decisions suggest --council codex claude agy grok --approval council --council-rule available -- DECISION_ID`.
Existing human approvals are preserved. Changing the council settings or
pausing the garden withdraws permission to approve an in-flight garden assessment.
Council approvals carry their own source, member/run identities and evidence into
training exports and data-quality reports, separately from human approvals.

Snout’s field guide lives beside Truffle pig hunts, with a theme-colored journey from
issue inspection to accepted fixes and PRs. Hunt status follows linked workflow recovery:
completed selected fixes clear a stale pause, a running recovery shows waiting, and a
recovered queue with unstarted issues offers **Continue queue**. Viewing a hunt never
starts additional work. Published fixes link directly to their PRs.

The live council view shows the active worker, public updates when available,
completed assessments, per-question votes and the approval result. Recent runs
stay inspectable after completion. New results animate gently; reduced-motion
preferences are respected. Updates continue while you edit review notes.

The **Ready to review** queue contains suggested answers awaiting approval;
all-abstention drafts appear under **Needs evidence**, and unsuccessful drafts
under **Failed drafts**. Garden attempts each decision once per council approval
setup; selecting an existing backlog can reassess prior drafts. **Exclude
example** removes a decision from automatic drafting and future training exports;
**Restore example** brings its existing reviewed answers back. Existing exported
datasets and model weights are unchanged. Automatic approval adds labels; enabled
automatic training can use those labels in its next candidate. Selecting an active
checkpoint remains a separate action.

CLI equivalents (draft-only unless `--approval council` is explicit):

```sh
orc fusion decisions suggest DECISION_ID --agent codex
orc fusion decisions suggest --council codex claude agy -- DECISION_ID
orc fusion decisions suggest --approval council --council codex claude -- DECISION_ID
```

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

Runtime access is configured with `execution_mode`: `restricted` (default) or
`yolo`. Settings load from `~/.config/orc/fusion.json` (or `$ORC_HOME/fusion.json`)
and then the project's `.fusion.json`. To use YOLO across workspaces, set the
machine configuration to:

```json
{"execution_mode": "yolo"}
```

YOLO applies to leads, new and resumed workers, all workflow roles, and named
routes. Codex gets `--dangerously-bypass-approvals-and-sandbox`; Claude, ORC
routes, and AGY get `--dangerously-skip-permissions`; Grok gets
`--permission-mode bypassPermissions --sandbox none --no-plan`. Claude's
sandbox is disabled through invocation settings. AGY additionally requires
`enableTerminalSandbox: false` in its native settings if it was enabled there.
In YOLO, review/discovery scopes are worker instructions, not runtime read-only
guarantees. Attempt limits, budgets, provider quotas, and acceptance checks
remain in effect. The UI shows the selected access mode and lets you override
it per workspace under Settings → Runtime access.

In restricted mode, Codex writer runs use the `fusion_git_write` permission profile: the workspace
sandbox plus writable Git metadata, including linked worktrees. Branch creation,
staging, and commits work; discovery and review remain read-only. This requires
a Codex CLI with named permission profiles and `--strict-config` support.
Set `codex.git_write` to `false` to retain Codex's standard protected `.git` behavior.
Explicit legacy `sandbox_mode` settings in Codex config take precedence over named
profiles; remove those settings to use this scoped profile. Already-running workers
keep the permissions they started with; new launches and resumed workers use the update.

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
headless plain output, a fresh session, and no subagents; its default `plan`
permission mode makes it an automatic read-only review/discovery option in restricted mode. Its
token usage and cost are unknown unless reported by a future adapter. Set
`grok.permission_mode` to `acceptEdits` for explicitly authorized writer work.
Named ORC routes can use other configured models; automatic ORC selection still
requires passing tool-fit evidence.

The server binds only to `127.0.0.1`. Use the complete URL printed in your terminal
to connect a browser; its fragment carries a per-machine access capability, stored at
`$ORC_HOME/ui-token` and reused across restarts.
`--no-open` prints that URL without opening a browser. `--port 0` chooses a free
port. The runtime needs only Python's standard library; Markdown rendering and
sanitization assets ship locally. There is no npm install, CDN, or hosted UI service.

Keyboard shortcuts: **N** new run, **1–5** sections, **/** workflow search,
**Esc** close dialog, **?** shortcut reference.

Development verification:

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
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/ui_connection_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/truffle_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/truffle_survey_browser.cjs
NODE_PATH=/tmp/orc-ui-tools/node_modules node test/publish_browser.cjs
```

`make test-ui` unit-tests the control room's pure decision logic
([`fusion_ui_assets/logic.js`](fusion_ui_assets/logic.js)) with no browser and
no server — escaping, URL allowlisting, credential and token handling, label
approval gating, filters and elapsed-time formatting. It installs vitest outside
the repo on first run, so the shipped runtime stays npm-free.

Browser checks use disposable workspaces and fake coding workers. They exercise
rendering, launches, settings, cancellation, reviewed labels and mobile layouts
without paid agent calls. Browser tools are needed only to run these checks.

### Truffle pig

Open **Truffle pig → Map every open issue** to give Snout the whole backlog.
The woodland follows every page of the repository’s open issues. Epics, trackers,
linked-issue checklists, and native GitHub parents become **patches**; their linked
open issues are the **truffles** inside. Standalone issues appear in **the wilds**.
Parent links and tracker references remain inspectable; closed parent issues can
still provide a patch for open children. Linking is inferred from explicit issue
references, so inspect tracker scope before choosing overlapping fixes.

Mapping alone makes no coding-worker calls. **Grade all issues** inspects source
in batches of eight using your configured worker account. Results appear live;
stopping or a worker failure preserves completed batches. **Resume grading** only
assesses remaining issues. A changed checkout or changed pending issue requires a
fresh sync; previous snapshots remain in **Field journals**.

| Grade | Meaning |
| --- | --- |
| A · Ripe | Small, low-risk fix with checked source quotes and a concrete regression plan |
| B · Promising | Bounded small/medium work with evidence and a verification plan |
| C · Needs digging | More evidence or clearer acceptance criteria needed |
| D · Leave for now | Too broad, blocked, assigned (unless included), or already covered |
| P · Patch | Epic/tracker container; assess and queue the child issues |
| U · Unassessed | Still waiting for source investigation |

Every issue stays visible, including C/D grades and policy exclusions. Grades are
suitability judgments, not success probabilities. Click a truffle for the evidence
and proposed implementation. Select A/B candidates into the basket and use **Queue
selected fixes**. Implementation still needs its own tests and independent review.
Woodland/All issues/Field journals, patch, grade and search filters use shallow URL
routing and survive reload. The map uses the active theme and honors reduced motion.

```sh
# Inventory only: no coding-worker calls.
orc fusion --progress truffle survey --sync-only
# Inventory and grade the whole backlog in saved batches.
orc fusion --progress truffle survey --agent codex
# Resume only unassessed issues in a saved survey.
orc fusion --progress truffle survey --resume truffle-0123456789ab --agent codex
```

For a smaller targeted expedition, use **Overview → Send in the Truffle pig**,
or **Truffle pig → Quick hunt**.
Choose a target count (default 5), pool size (default 40), worker, and optional
GitHub search filter such as `label:bug sort:updated-desc`. The repository comes
from the selected Git remote, using your authenticated `gh` CLI. Scouting uses a
coding worker for investigation; it does not edit code or post to GitHub.

The shortlist shows why each issue is tractable, checked source quotations,
effort/risk, reproduction evidence, implementation steps, test commands and
acceptance criteria. It can return fewer issues than requested, including zero.
Source citations are checked against the files; feasibility is a worker judgment,
not a calibrated success probability. Skip reasons remain visible. Assigned issues
(unless included), issues linked to open PRs, and issues already queued by another
hunt are excluded. The PR check covers GitHub's closing-issue links, not every
informal mention in a PR; the scout also checks related source and history.

**Queue selected fixes** runs one issue at a time in its own worktree from the
chosen target branch. Manual PR mode is the default; automatic mode commits,
pushes and opens a PR only after implementation and independent review pass.
Open/updated status and linked PRs are checked again before each launch. A changed
issue needs a fresh hunt. Failure or quota pauses the queue: open the linked workflow,
resume that workflow, then queue the remaining selection again. Successful workflows
are reused, not repeated. Jobs survive closing the browser, and records live under
`.fusion/truffle/` alongside the usual workflow artifacts.

```sh
orc fusion --progress truffle hunt --count 5 --scan-limit 40 --search 'label:bug'
orc fusion truffle show truffle-0123456789ab
# Use the saved hunt ID and only the issue numbers you chose from its shortlist:
orc fusion --progress truffle run truffle-0123456789ab --issues 123 456 --base staging --publish manual
```

Use `--publish auto` to publish accepted fixes automatically; PRs default to drafts.
Each stage has an attempt limit (`--max-attempts`, default 2). Queue execution pauses
at the first unresolved workflow instead of consuming more attempts across the pool.

## Fusion — the forge

Fusion is the harness the rest of the guild is built on. It keeps a lead agent
in charge of the conversation and the final review, then delegates bounded work
to Claude Code, Codex, Antigravity or Grok through a shared task contract. Each
run records its task, JSONL events, stdout, stderr, result, and reusable session
id under `.fusion/` in the workspace — so every claim in a report has a receipt
behind it.

The lead can call the other agent through an MCP server:

```sh
fusion lead                 # Claude leads by default
fusion lead --agent codex   # Codex leads and can call Claude
fusion doctor
fusion trace --limit 50
fusion usage --limit 1000
```

### Driving ORC from a chat client (MCP)

`fusion mcp-serve` exposes ORC over the Model Context Protocol, so an agent in a
chat client drives the same `.fusion/` artifacts the CLI and control room read.
Three primitives, mapped onto what ORC already is:

| Primitive | What it exposes |
| --- | --- |
| **Prompts** | the verbs — `where-am-i`, `ship-feature`, `review-changes`, `explain-run`, surfaced as slash commands |
| **Resources** | the evidence — `orc://here`, `orc://workflows`, `orc://workflow/{id}/manifest`, `.../report` |
| **Tools** | the operations — `fusion_here`, `fusion_run_start`, `fusion_run_status`, `fusion_run_cancel`, plus `fusion_delegate` |

Long-running work uses **durable handles rather than blocking calls**.
`fusion_run_start` returns a `workflow_id` immediately; poll `fusion_run_status`
with it. The handle is the workflow directory on disk, so it survives a restart
of both the server and the client, and a result hands back `resource_link`
evidence URIs instead of inlining artifacts.

`fusion_here` answers "where am I" for a workspace — active workflows, what
failed, what it cost, Laya's mode, and the next command — reading artifacts only.
It starts nothing and contacts no provider.

This deliberately does **not** use the MCP tasks extension. Tasks has the right
shape, but as of spec revision `2026-07-28` it sits outside core, is absent from
the official client matrix, and no mainstream client implements it — and tasks
cannot carry `notifications/progress`. Handles work in every client today, and
`tasks/get` / `tasks/update` / `tasks/cancel` adapt onto them when that changes.

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
command returns nonzero, its receipt remains in `command_evidence` and the UI's
**Evidence → Command observations**. Searches with no matches, corrected file
lookups, and tests run before a fix do not become permanent blockers. Unresolved
handoff blockers, provider errors, required evidence, and configured acceptance
checks still determine whether a stage passes.

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

In restricted mode, Fusion launches `agy` with `--sandbox` and uses `plan` for readers or
`accept-edits` for writers. Execution mode alone does not grant command
permissions. In `~/.gemini/antigravity-cli/settings.json`, merge these settings
with your existing configuration:

```json
{"enableTerminalSandbox": true, "toolPermission": "proceed-in-sandbox"}
```

This permits sandboxed commands while preserving explicit permission rules;
commands outside the sandbox can still require approval. See the
[Antigravity sandbox documentation](https://www.antigravity.google/docs/sandbox?tab=cli).
If your shell or tools come from Nix and fail with a sandbox-blocked library
under `/nix/store`, add `read_file(/nix/store)` to the existing
`permissions.allow` list. This mounts the runtime files read-only; it does not
authorize unsandboxed commands. Preserve other permission rules.

Fusion checks this local setup before selecting AGY automatically. This is a
configuration check, not a guarantee of authentication, quota, or every tool's
permission. Explicit AGY selections can still use a narrower custom allowlist.
`fusion doctor` and the UI show headless setup status. Permission denials stop
the attempt; resuming with Auto excludes that failed worker, while explicitly
selecting AGY lets you retry after correcting its configuration. Fusion never
adds a permission bypass in restricted mode. Explicit `execution_mode: "yolo"`
uses the bypass flags described above. `agy` has no per-call budget flag, so cap its
cost with `timeout_seconds` and the workflow's `budget_usd`. OpenRouter models
are not in the `agy` host list — route those through the `orc` path instead.
`agy` is not (yet) a lead candidate; `fusion lead` remains Claude or Codex.

### Ultra without the token fire

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
an explicitly reported zero. Reporting starts no agents and leaves receipts intact.

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

### Traces and dogfood

Every dispatched worker writes a metadata-only span to
`.fusion/traces.jsonl`. Spans include the trace and parent IDs, agent, route,
resolved model when the provider reports it, duration, status, token usage,
tests, blockers, and links to the raw run artifacts. Prompts and model output
are not copied into telemetry; inspect the run's `stdout.log` when you need
that detail. Set `telemetry.enabled` to `false` in `.fusion.json` to disable
the trace ledger.

```sh
make test                 # deterministic Python unit tests
./test/run.sh             # the launcher + HUD suite (separate; CI runs both)
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

To stop sending while keeping local traces:

```sh
fusion telemetry off          # persists in .fusion.json
export FUSION_TELEMETRY=0     # same, for one invocation
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
`timeout` / `missing_executable` / `worker_error` / `coordinator_error` —
never the raw blocker
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

`fusion telemetry report [--hours N]` (default 168, i.e. 7 days) reads your
own rows back — calls, cost, and average duration grouped by
agent/route/model/status/failure_class. It needs no token: this machine
already holds an unguessable install id and already sends it with every
span, so it can ask for its own rows back.

`--all` widens that to every install, and is the only thing that needs the
shared token in `.fusion.json` under `telemetry.remote.token` — that view
reveals how many people are running this and what they spend, so it stays
guarded. Add `--json` for the machine-readable form.

The collector itself (a small Fly + Postgres app) lives in
[`telemetry/`](telemetry/).

The architecture and source map are in [FUSION_RESEARCH.md](FUSION_RESEARCH.md).

## The launcher

The original `orc`: one command to run Claude Code against any OpenRouter model
— GPT, Gemini, Kimi, DeepSeek, free stealth previews like `stealth/ox-alpha`. It
handles env wiring, model selection and key management, and keeps its Claude
state fully isolated from your normal Anthropic login, so you never have to
`/logout` between Anthropic and OpenRouter sessions.

The guild uses it too: the `orc-free` and `orc-best` workflow routes resolve
through this catalog at run time.

### Command reference

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

### Choosing a model

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

### Profiles

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

### Per-project config: `.orc.json`

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

### Stats

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

### How the key is resolved

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

### What it sets for Claude Code

- `ANTHROPIC_BASE_URL` → OpenRouter's Anthropic-compatible endpoint
- `ANTHROPIC_AUTH_TOKEN` → your resolved key (`ANTHROPIC_API_KEY` is set to
  an empty string to avoid conflicts)
- `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` → your saved models
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` → the model's real context window from the
  OpenRouter catalog (Claude Code otherwise assumes 200k for unknown models)
- `ANTHROPIC_DEFAULT_HAIKU_MODEL` → your saved small model
- `CLAUDE_CONFIG_DIR` → `~/.config/orc/claude-state`, so orc never touches
  your real `~/.claude` login and you never have to `/logout` between
  Anthropic and OpenRouter sessions

The base URL and model are additionally forced via `claude --settings` (CLI
settings outrank directory settings), so a project-level
`.claude/settings.json` with its own `env.ANTHROPIC_BASE_URL` — a proxy, a
gateway — can't silently hijack an orc session.

### The HUD

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

### Config

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
- Extended thinking and prompt caching only work fully on Anthropic models;
  costs on other models may be higher than the same workflow on Anthropic
  first-party.
- Stealth/preview models (`stealth/*`) come from anonymous providers that
  retain prompts and completions — don't point them at anything sensitive.
- The HUD cost is an estimate: it trusts the catalog's per-token price fields,
  including OpenRouter's separate 5-minute/1-hour cache-write multipliers,
  which may not match what your route actually serves.

Grok workers default to `streaming-json` output. For an older CLI without that
format, set `grok.output_format` to `plain` in `.fusion.json`; public text still
appears live, but that format does not provide individual tool receipts or usage.
