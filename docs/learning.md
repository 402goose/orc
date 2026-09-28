# Evidence and learning

[Start here](../README.md) · [CLI reference](cli.md) · [MCP reference](mcp.md)

- [Outcomes and labels](#outcomes-and-labels)
- [Laya setup and shadow mode](#laya-setup-and-shadow-mode)
- [Lab navigation and review](#lab-navigation-and-review)
- [Quality and automatic training](#quality-and-automatic-training)
- [Garden and council](#garden-and-council)
- [Learning without the UI](#learning-without-the-ui)
- [Gym](#gym)

## Outcomes and labels

Record an outcome after checking a delegated run's evidence. Direct delegations
have no workflow coordinator gate; the worker's status alone is not a verified
outcome. The latest measured external verdict for a run feeds outcome ranking.
A verdict with a reason on a reported success can also produce an acceptance
training label. Acceptance does not prove that the selected lane was optimal.

Write workflows fingerprint existing acceptance inputs before the first attempt.
If a worker changes or deletes one, `check_inputs_changed` marks the receipts and
gate span as untrusted (`tampered`). The node can still succeed, but its structural
gate produces no training label and its outcome is excluded from routing counts
and ranking. New tests are allowed. Resume retains the original pins and retracts
earlier structural labels and gate outcomes if a recheck detects changed inputs;
independent human reviewers' labels are preserved. Gym results with this flag are
tampered evidence, including hidden modes, and cannot establish task solvability
or contribute lane priors.

```sh
fusion outcome RUN_ID --accepted --stage gate --reason "The regression check passed"
fusion outcome RUN_ID --rejected --stage verify --reason "Independent review reproduced the failure"
fusion outcome RUN_ID --accepted --stage land --reason "Verified the landed change"
fusion outcome RUN_ID --withdraw --reason "The grader used the wrong fixture"
fusion outcome RUN_ID --unmeasured --reason "The grader could not run"
```

`--stage gate|verify|land` is metadata: append order, not stage ordering, determines
the latest measured verdict. `--withdraw` requires a reason and removes prior
external verdicts and their labels, preserving independent gate evidence.
`--unmeasured` records an unscored attempt without changing ranking or labels.
Choose exactly one of accepted, rejected, withdraw, or unmeasured.

The [decision and evidence design](../FUSION_DECISIONS.md) explains label provenance,
qualification and executed verification. The [learning roadmap](learning-roadmap.md)
distinguishes implemented evidence collection from proposed future work.

## Laya setup and shadow mode

Optional local [Laya decisions](../FUSION_DECISIONS.md) cover intake, automatic
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
classifier; see the [setup and learning guide](../FUSION_DECISIONS.md).

## Lab navigation and review

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

Acceptance inputs include the node's own assignment, separately from the overall
request, and a bounded deliverable digest. Read-only digests include answer-body
headings, list items and opening lines beyond SUMMARY. Write digests include changed
files and coordinator acceptance-check receipts (argv, status and exit code),
separately from claimed tests. Summary text keeps priority, followed by excerpts of
the node assignment and supporting request detail; the digest fills the remaining
character and token budget. Cuts, including cuts to long node assignments, carry
visible `[…truncated N chars]` markers. Inputs whose request criterion and minimum
summary cannot fit beside the excerpt markers are marked
`source_truncated`. Every new decision records input-builder `state_version: 2`.
Council automatic approval skips source-truncated inputs and older builder versions,
independently of whether the structural gate passed.

The drafter abstains per question when the input announces findings without including
them, or when relevant evidence was truncated. Supplemental artifacts cannot fill
gaps in the original input.

Compare drafts against effective human labels and human exclusions:

```sh
fusion decisions eval-drafter --agent codex
fusion decisions eval-drafter --agent codex --rebuild-input --limit 20 --json
```

Evaluation copies the decision store into a temporary workspace and dispatches all
drafter runs there; it never writes to the live store or run artifacts. It includes
human labels and human-approved suggestions, excluding labels approved solely by
automatic sources. The table reports agree/disagree/abstain per reviewed question,
totals, and whether each human exclusion received an all-abstention draft. JSON also
includes individual decisions, answers and abstention reasons. Worker failures and
missing rebuild artifacts count as errors, not abstentions, and produce a nonzero
exit status. `--limit` selects the first N eligible decisions in store order.

`--rebuild-input` rebuilds acceptance inputs with the current builder from saved
`task.json`, `result.json` and `answer.md` before drafting; other decision kinds keep
their original input. This measures input-builder changes against the same human
reference labels. Older workflow wrappers are used to recover the node assignment
when the dedicated field is absent. Historical runs without saved coordinator
receipts cannot reconstruct those receipts. Runs and the temporary copy are removed
after evaluation. Evaluation uses the configured worker account and incurs normal
provider usage; it does not run acceptance checks again.

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
The configured checkpoint is shown separately from candidate models.

## Quality and automatic training

Approval feeds automatic training when enabled; manual export, training, and evaluation
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
closed, or without the server via `fusion learn tick` (see [decision design](../FUSION_DECISIONS.md)). Pausing prevents subsequent steps; the active job may finish. A failed or
cancelled job waits for **Retry this step**. Saved rounds resume after a server restart,
and manual and automatic learning jobs share one workspace slot. Settings, curated
datasets, progress, and round receipts live under `.fusion/decisions/training`;
individual jobs remain under `.fusion/ui/jobs`. **Manual training tools & saved
candidates** keeps the original controls available below the training grounds.

## Garden and council

**Laya lab → Garden → Enable auto-drafts** queues incoming complete decisions for a
labeling worker. There is **no daily cap**, including for gardens with an old saved
limit. Optionally include existing eligible decisions and drafts in the selected setup.
Garden runs one assessment at a time while the control-room server is running, even
with the browser closed, or on each `fusion learn tick` without it. Settings, attempted decisions and daily usage survive
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

## Learning without the UI

```sh
fusion learn status
fusion learn tick
fusion learn tick --workspace /path/to/project
fusion learn schedule install --interval 300
fusion learn schedule status
fusion learn schedule uninstall
```

`status` reads loop state; `tick` advances the same garden/training step as the
control room once and exits. It respects the existing enabled/paused settings;
it does not enable training itself. Detached jobs continue between ticks.
Per-workspace locks prevent a scheduled tick and live UI from launching the same
step twice. Repeat `--workspace` for multiple targets, or use `--all` for known
workspaces (including the current one when it has Laya decisions).

Scheduling installs a launchd job on macOS; elsewhere it prints a crontab line
for you to install. The interval defaults to 300 seconds and must be at least 60.
See [the complete CLI reference](cli.md#fusion-learn).

## Gym

`fusion gym` replays merged fix PRs as benchmark tasks: `gym extract` builds
tasks from a PR's squash commit (its parent plus the PR's tests, with
FAIL_TO_PASS and PASS_TO_PASS test ids), `gym run` gives each task to each
lane in its own worktree and lets the gate label the result, and `gym report`
compares lanes on the same tasks. By default the tests are hidden: the worker
starts from the parent commit with only the problem text, and the PR's tests
are written in only while the gate runs them (`--visible-tests` for the old
mode). `gym run` calls paid models. See
[ORC gym](../FUSION_DECISIONS.md#orc-gym-replayed-fixes-as-benchmark-tasks).


Export gym evidence with `fusion gym priors GYM_DIR`; see
[routing priors](routing.md#gym-lane-priors) for defaults and disabling.
