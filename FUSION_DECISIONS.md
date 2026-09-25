# Local decisions with Laya

Fusion integrates [Laya](https://github.com/NandhaKishorM/laya) at five decision
points and provides a sixth capability: learning from reviewed outcomes.
Laya classifies bounded inputs locally. Claude, Codex and agy still perform
the coding work through their configured accounts.

## Start with an idea or an issue

```sh
orc fusion decisions setup
orc fusion build "Add saved searches with names, deletion, and tests"
orc fusion build https://github.com/OWNER/REPO/issues/123
```

Setup uses `uv` to create a Python 3.12 environment under
`~/.local/share/orc/laya`, installs Laya 0.3.4 and Transformers 4.x, and
downloads the English checkpoint. Normal inference runs offline. To support
Laya's automatic language detection, also cache the multilingual checkpoint:

```sh
orc fusion decisions setup --checkpoint multilingual
```

`build` reads GitHub issues through authenticated `gh issue view`, saves the
complete request, brief and executable workflow under `.fusion/builds/`, and
opens the interactive lead. The lead handles investigation, specification,
implementation, verification, independent review and fixes.

```sh
# Create inspectable artifacts without starting a coding agent.
orc fusion build --plan-only "Add CSV export to the filtered dashboard"

# Execute the generated explore → plan → implement → review workflow.
orc fusion build --execute --budget-usd 5 --max-attempts 2 \
  "Add CSV export to the filtered dashboard and test filtering and escaping"

# An explicit investigation workflow has no implementation node.
orc fusion build --kind discovery "Investigate the checkout retry behavior"
```

Planning-only restrictions in the full request or issue body take priority
over classifier advice and `--kind build`. Discovery/review leads use the
CLI's read-only/plan mode; their Fusion MCP server rejects writer tasks.
These are agent tool policies, not an OS security boundary. Fusion itself
still saves its local artifacts. A request to `--plan-only` means prepare
the workflow without executing it; that artifact can contain a future writer.

The unattended workflow stops on unresolved blockers or review findings.
Use the interactive lead for an open-ended review/fix conversation. Saved
workflows can be edited and resumed using `fusion workflow resume ID --spec
PATH`; valid accepted nodes remain cached. Custom acceptance checks use argv
arrays and remain authoritative. Generated workflows require verification
handoffs and an independent review; the lead/reviewer determines the actual
project-specific checks. A successful model handoff alone does not prove
product correctness.

`--budget-usd` controls the persisted workflow, including all retry costs.
It stops new dispatches when **reported** spend reaches the limit and passes
remaining budget into routing. It is not a hard provider spending cap:
missing cost reports, in-flight parallel calls and the last call can exceed
it. Interactive lead sessions use their own provider controls.

## What changes at each decision point

1. **Intake:** recommends discovery, build, debug or review; records whether
   clarification appears necessary. Explicit scope is authoritative.
2. **Routing:** `--agent auto` and workflow nodes with `"agent": "auto"`
   choose among available native workers and configured ORC routes. Inputs
   include task, write permission, routing goal, reported costs/latency,
   reported success, and observed workflow acceptance. Missing metrics stay
   unknown. ORC candidates require passing tool-fit evidence; read-only
   routes cannot become writers. Recent quota failures exclude equivalent
   native lanes. Explicit agent/route selections stay pinned, and a lane
   with a single candidate records no routing decision: there is nothing
   to choose.

   Without a qualified classifier, automatic lanes follow the configured
   preference order. `"decisions": {"rank_by_outcomes": true}` orders them
   by verified outcomes instead: a lane with fewer than 3 checked runs (or
   the integer you set) is tried first so it earns evidence -- on ordinary
   read-only work only: a writer or a review goes to a lane with verified
   evidence once any lane has reached that minimum -- then lanes
   follow smoothed acceptance `(accepted + 1) / (checked + 2)`. Workflow
   gate outcomes and lead verdicts count, and so does an observed error
   (not quota or permission, which are lane health); a worker's
   `STATUS: success` is its own claim until a gate or lead checks it. A qualified classifier still
   overrides the order. An ORC route with `"arms": 3` offers orc's top three
   tool-fit models as separate candidates (`orc-free:<model id>`), each with
   its own history; the default of one arm keeps the route's key and stats.
   A task that names such a route without pinning a model gets its model
   chosen inside that route the same way (when decisions mode is not off);
   the named route itself is never swapped for another lane.

   Delegations have no acceptance gate. After inspecting one, the lead
   records its verdict so it counts:

   ```sh
   fusion outcome RUN_ID --accepted --reason "diff reviewed, tests pass"
   fusion outcome RUN_ID --rejected --reason "empty diff"
   ```

   MCP leads call `fusion_outcome` with `run_id`, `accepted` and `reason`.
   The latest verdict for a run wins. A verdict with a reason also becomes
   an acceptance label; see [Learn from verified runs](#learn-from-verified-runs).
3. **Recovery:** classifies actual acceptance results as continue, repair,
   switch, ask or stop. A classifier cannot accept a failed check, bypass a
   permission denial, increase attempts, or discard prior spend. A qualified
   switch applies only to an automatic lane and excludes the failed route.
   Repairs receive the previous result and log paths. Quota failures pause
   by default. An attempt that repeats the previous attempt's blockers
   exactly stops instead of retrying; that is a rule applied to the
   receipts, not a classifier question. User-requested resume is an
   explicit new attempt.
4. **Review:** adds general, security, payments or data-integrity checks to
   read-only review workers. Generated implementation workflows always
   retain their independent review. The classifier cannot remove it or
   authorize writes. A different installed worker is preferred; otherwise
   review uses a fresh context on an available worker.
5. **Acceptance:** a semantic Done-check on workflow nodes. It runs only
   after every structural check has already passed (required files exist and
   changed, handoff fields present, acceptance commands exited 0) and asks
   two `noul` questions: does the reported success plausibly satisfy the
   task, and did the worker fail to do what was asked? Only a qualified
   "not plausible" rejects; the second answer is recorded for calibration
   and training. On authored near-misses it detects "no work was done" but
   not "the wrong work was done", so it is evidence, not a gate. The
   structural checks own every fact; the classifier only judges them. A
   qualified rejection adds a blocker and hands the node to recovery
   like any other rejected result. It can never accept a node: a structural
   failure is decided before it is called, and there is no path back.
6. **Learning:** local decisions and acceptance outcomes are logged. Only
   verified labels with evidence enter training exports: human reviews,
   explicitly enabled council approvals, and lead verdicts on acceptance. Human review, candidate fine-tuning, held-out evaluation and
   calibration are separate steps; no model promotes itself.

## Prompt cache and sessions

Provider prompt caches dominate worker cost (one Claude run read 3.38M
input tokens from cache against 96 uncached), and they are per
model/provider and expire after minutes, with provider-specific TTLs.
Fusion records enough to see this and acts on it only where configured.

**Recorded on every span** in `.fusion/traces.jsonl` (and the run's
`result.json`):

- `session_key`: the key the run's native session is stored under
  (`agent:role`, extended for model/effort pins and automatic lanes).
- `resumed`: whether a stored native session id was passed to the worker.
- `session_idle_s`: seconds between the end of the previous worker run
  under the same `session_key` and this run's start; `null` for the first.
  Last-use times live in `.fusion/session_use.json`; `sessions.json` keeps
  its `key -> session id` shape, and a session without a use record is
  treated as unknown age.
- `cache_read_ratio`: cache-read tokens over all prompt tokens. For
  Anthropic-style usage that is `cache_read / (input + cache_read +
  cache_write)`; for OpenAI-style usage, where `cached_input_tokens` is part
  of `input_tokens`, it is `cached / input`. `null` when no cache counts
  were reported.
- `resume_skipped: "cold"` when the policy below started a fresh session.

**Configuration** (all optional):

```json
"cache": {"ttl_seconds": 300, "cold_resume": "resume", "warm_epsilon": 0.05}
```

`ttl_seconds` (default 300) is when Fusion considers a session or lane
cold; set it to your provider's cache lifetime. `cold_resume: "resume"`
(default) keeps resuming regardless of idle time. `"fresh"` starts a new
session instead of resuming one idle longer than `ttl_seconds`, so a long
history is not re-sent at cache-write price; the worker then sees only the
current brief, not its earlier conversation, so use it where briefs are
self-contained. A session of unknown age is still resumed.

**Routing.** Each automatic candidate carries `warm` (its lane's most
recent span, per model for split arms, ended less than `ttl_seconds` ago),
`session_idle_s` (seconds since that span ended) and
`mean_cost_usd_warm`/`mean_cost_usd_cold` (mean reported cost of runs
whose own `session_idle_s` was under/over the TTL; a first run counts as
cold, spans recorded before these fields existed count in neither).
Warmth is lane-level, not the exact session a task will resume: a warm
lane may still hold a cached system prompt and tool prefix.

With a `cache` block present and `rank_by_outcomes` enabled, warmth only
breaks ties: candidates in the same bucket (exploring, or ranked) whose
smoothed acceptance is within `warm_epsilon` of the best rate in their
tier are equal, and the warm one goes first. Warmth never moves a lane
past one whose smoothed acceptance is more than `warm_epsilon` better,
and reviews still prefer a different agent from the implementer. Without a
`cache` block the order is unchanged. Candidates passed to Laya's routing
decision include `warm` and `session_idle_s` as shadow state; they add no
authority.

## Shadow, off and active modes

The default is **shadow**: recommendations are recorded, but deterministic
policies select the action. `agent=auto` falls back to the configured
sidekick, then other available workers. Missing optional dependencies,
uncached models, timeouts and invalid predictions cause abstention.

```sh
orc fusion decisions status
orc fusion decisions probe "Implement CSV export with tests"
orc fusion decisions probe --kind acceptance \
  '{"task": "Add CSV export with tests", "summary": "did nothing", "changed": [], "tests": []}'
orc fusion decisions list --limit 10
orc fusion decisions show DECISION_ID
orc fusion delegate --agent auto --read-only "Map the checkout retry logic"
```

`orc fusion workflow report ID` lists each node's recorded recommendations with
their probabilities and whether they were applied or advisory, and a `Gate:`
line counting accepted vs rejected nodes. The gate count comes from a trace
span per acceptance decision (agent `gate`), so `fusion usage` and remote
telemetry can see rejections; the worker's own span is written before the
gate runs and only carries the worker's claim. A request the runtime rejects
(for example a malformed question) abstains for that decision only; the
runtime keeps serving later decisions in the same run.

Interactive terminals also show each recommendation as it arrives, its selected
label probability and inference time, followed by the action Fusion actually
takes. Cold starts and long-running workers emit a heartbeat every ten seconds.
Use `orc fusion --progress ...` or `FUSION_PROGRESS=1` for the same display when
stderr is redirected, and `--quiet` or `FUSION_PROGRESS=0` to suppress it. MCP
sessions never emit terminal progress. A heartbeat confirms the coordinator is
waiting in that phase; it is not a completion estimate.

`orc fusion workflow watch` attaches to the latest saved workflow; supply an ID
to select one, or `--once` for a snapshot. It starts no workers. With the global
`--json` flag it emits JSONL snapshots. New runs expose worker PID, elapsed time,
log sizes and log paths; older runs expose their saved stage state and blockers.

The worker runtime stays loaded and serializes classifier requests inside a
Fusion process. Separate CLI invocations have separate cold starts. The
English checkpoint is about 421M parameters; startup and inference time
depend on local hardware. No GPU or latency guarantee is assumed.

Configure the optional block in `.fusion.json` (merge it with existing
settings):

```json
{
  "decisions": {
    "mode": "shadow",
    "device": "cpu",
    "routing_goal": "quality",
    "timeout_seconds": 120,
    "max_state_chars": 2200,
    "auto_actions": [],
    "threshold": 0.9,
    "calibration_file": "",
    "model_path": "",
    "verdict_labels": true
  }
}
```

`python` or `FUSION_LAYA_PYTHON` can select a different runtime interpreter.
`FUSION_DECISIONS_MODE=off` disables classification and decision logging.
`mode: off` in configuration has the same effect. Existing orchestration
and deterministic automatic routing still work.

Active actions require **all** of:

- `mode: active` and the decision kind listed in `auto_actions`;
- a qualified calibration bucket for the exact question schema and model
  identity, including weights, tokenizer, configuration and SDK version;
- probability at least the configured and calibrated thresholds;
- complete state, instructions and options within the checkpoint's token
  limits; any detected truncation forces abstention;
- all deterministic permission, availability, attempt and acceptance gates.

The SDK's entropy-derived `confidence` is not treated as a probability.
Fusion uses the selected label's distribution and fitted temperature.
The upstream typed-decisions checkpoint is not an automatic default for
Fusion's schemas. `setup --checkpoint typed-decisions` makes it available
for an explicit `model_path` experiment, subject to the same qualification.

## Learn from verified runs

### Lead verdicts become acceptance labels

`fusion outcome RUN_ID --accepted|--rejected --reason "..."` (MCP
`fusion_outcome`) is a verified judgment about exactly what the acceptance
decision asks, so it is saved as an approved acceptance label without a
click. Only the questions a verdict determines are answered:

| Verdict | `plausible` | `failed_task` |
| --- | --- | --- |
| `--accepted` | `true` | `false` |
| `--rejected` | `false` | unlabeled |

A rejection says the reported success should not have been accepted. It
does not say the worker failed to do what was asked: the work may have been
done in an unacceptable way, or the brief may have been wrong. So
`failed_task` stays unlabeled.

No label is written when the verdict has no `--reason` (the outcome is still
recorded for route ranking), when the run did not report `success` (acceptance
is only asked of a reported success), when the run's `task.json` is missing,
or when the task cannot be shown whole within the token budget (below; the
record is kept, marked truncated). `mode: off` or `"verdict_labels": false` turn verdict labels off.

The label attaches to the run's acceptance decision. A workflow node that
already has a complete acceptance decision is labeled in place. Otherwise
Fusion records an `unscored` acceptance decision, with the same input
`accept_node` builds or the input an unavailable workflow gate recorded.
Laya does not run, so `fusion outcome` stays fast and works without a
checkpoint. An `unscored` decision has no prediction and can never drive an
automatic action. Calibration skips it and reports it under
`unscored_examples`; score it with `evaluate` first.

Each label is `verified` with `source: "lead_verdict"`, `reviewers:
[{"agent": "lead", "run_id": ..., "accepted": ...}]` and evidence holding the
reason and the run's `result.json` path. Its context is `task_id` = run id and
`group` = the run's workflow or trace, so repeats stay in one split. A later
verdict on the same run replaces the earlier verdict's answers; a later
verdict that cannot label retracts them. Labels from a human or council on
that decision are never overwritten.

Verdict labels count as approved in the Laya lab, the training loop's
new-answer count and readiness, and exports. Every exported answer carries
its `label_provenance` source. To audit or leave them out:

```sh
orc fusion decisions export .fusion/decisions/reviewed.jsonl
jq -c 'select(.label_provenance[]?.source == "lead_verdict")' .fusion/decisions/reviewed.jsonl
orc fusion decisions export .fusion/decisions/human.jsonl --exclude-source lead_verdict
```

Excluding a single example in the lab also removes it. The automatic training
loop exports every approved source; disable `verdict_labels` before labels
accumulate if you want them out of automatic rounds.

Verdicts never create routing labels. A worker succeeding does not prove it
was the best route. Outcomes rank routes (`rank_by_outcomes`) and nothing
else. Workflow gate outcomes are not labels either: the acceptance question
is asked only after structural checks pass, so a gate "accept" repeats the
policy's own choice.

#### What the classifier sees

The workflow gate and verdicts build the acceptance input in one place
(`fusion_decisions.acceptance_state`), so one run always yields the same
input. It is `{"task", "summary", "changed", "tests"}` as JSON, within two
bounds: at most `max_state_chars` characters, and at most the tokens Laya
reads beside the acceptance questions (the token budget, below):

- `task` is the job the report is judged against, not the worker's prompt.
  It is the run's `decision_context` when that is a string; Truffle sets one
  ("Truffle scout: shortlist at most N ... in OWNER/REPO ..." or "Truffle
  survey: grade each of N issues ..."). A stage of a `fusion build` workflow
  gets `{"request", "workflow_kind", "stage", "role"}`: the original request
  without the dependency receipts a run's context also carries. Otherwise it
  is the task text (a delegation brief, a hand-written node's task).
- `summary` is the handoff's `SUMMARY` field (the whole answer only when
  the worker gave no `SUMMARY`).
- `changed` keeps 12 entries and `tests` 8, each at most 200 characters;
  fewer when the token budget requires (below).

When the whole input does not fit, it is cut by one rule. The acceptance
question asks whether the reported summary plausibly satisfies the task, so
the task's criterion is never cut. The criterion is the whole task, except
for a `fusion build` request, whose first line is the ask; the lines after
it (for an issue Truffle selected, the scouting assessment and evidence)
are detail. Only these may be cut, each keeping its start:

- the request detail, which gives up room first;
- the summary, whose start is where a handoff states what was done: it keeps
  at least 3/5 of the room left after the criterion and lists, and never
  under 400 characters with its marker (all of it if shorter);
- list entries past the caps. When the criterion and minimum summary do
  not fit beside the lists, the lists shrink to 6 `changed` and 4 `tests`
  entries of at most 100 characters, then to 3 and 2 of at most 60.

Every cut is visible in the input, as `[…truncated N chars]` or `[…N more]`.
An input with visible markers is complete for labeling: it says exactly
what the classifier saw, and a label on it is a label on that input. When
the criterion plus the minimum summary cannot fit, nothing is excerpted: the
input is marked `source_truncated`, recorded truncated, and never labeled
or acted on. A delegation brief of more than roughly 1,000 characters of
prose (about 750 of code, paths or commands) is such a case; the token
budget, not `max_state_chars`, sets that limit, so raising the cap does not
help. A cut does not change the question schema, so calibration buckets
(`kind:schema_hash:question`) are unaffected; inputs recorded before this
rule remain as they were.

##### The token budget

Laya's encoder reads 512 tokens: the question head, then the state, which
gets whatever is left. The longest acceptance question's head takes 40, so
an acceptance input has 472 tokens (`fusion_decisions.state_tokens`). A
character cap cannot decide that: on the checkpoint's tokenizer, recorded
decision states run 2.4-4.8 characters per token, hashes, UUIDs and diffs
about 1.7, CJK about 1.2. A 2,200-character acceptance input was 550-750
tokens, so the model never saw its end.

The verdict path cannot run the tokenizer (`fusion outcome` never loads the
model), so the gate and verdicts both bound the input with
`fusion_decisions.estimated_tokens`, a pure-Python estimate that charges
more than the tokenizer spends. `acceptance_state` lowers its character cap
in proportion to the estimate until the input fits 472 estimated tokens.
Measured with the tokenizer on 1,596 texts (the repository's code, docs,
JSON and shell, recorded decision states, and synthetic hashes, digits,
unicode and emoji), the true count was at most 0.89 of the estimate on
JSON-encoded text and 0.81 on recorded acceptance states (0.72 on average),
so an input built this way uses about 340 of the 472 tokens and at most
about 420. The margin is the estimate's own; there is no separate factor.
The estimate is not a bound for text built to defeat BPE (random consonant
strings, base64, alternating case). Such an input can still be truncated;
the gate records the runtime's report, which disables automatic action, and
`train`/`evaluate` still refuse it.

`test/fixtures/laya_token_counts.json` holds the tokenizer's counts for the
synthetic texts and for six acceptance inputs built by `acceptance_state`;
the default suite checks that the estimate is never below them and that the
fixture inputs are rebuilt exactly and fit. `test/laya_token_budget.py`
re-measures them with the managed runtime (`--write` rewrites the fixture);
run it after changing the estimate, the acceptance questions or
`acceptance_state`. The budget applies to acceptance inputs only;
`max_state_chars` still bounds every other kind, whose truncation the runtime
reports at inference.

##### Labels on inputs recorded before the token budget

An `unscored` input was never checked by the model. One whose estimate
exceeds the budget -- an acceptance input of up to 2,200 characters recorded
by a verdict before this rule -- counts as truncated
(`fusion_decisions.exceeds_token_budget`): the lab shows it ineligible,
exports skip it and report `skipped_over_token_budget`, the training loop
does not count its answers, and it cannot be labeled again. Nothing on disk
is rewritten, and a scored decision keeps the runtime's own truncation
report. To repair a run, record its verdict again (`fusion outcome RUN_ID
--accepted|--rejected --reason ...`): the verdict labels on the old input are
retracted and the verdict labels a freshly built, bounded input. Labels a
human or council attached to an old input are left in place but, like the
input, are not exported.

#### Runs in workflow worktrees

A workflow that publishes runs its stages in
`.fusion/worktrees/<workflow_id>`, whose `.fusion` links back to the
workspace's. A worker that can write the worktree can also replace that
link. So workflow runs, traces, sessions, gate spans, and routing, review,
acceptance and recovery decisions are always written to the workspace that
started the workflow; the worktree is only the worker's working directory.
Route ranking in the workspace therefore counts implementer runs and their
outcomes.

`fusion outcome RUN_ID` looks for the run in `.fusion/runs`, then in
`.fusion/worktrees/*/.fusion/runs` (runs written before this change, or
while the link was missing). A path that resolves outside the workspace's
`.fusion` is not accepted. The outcome and label are recorded in the
workspace's decision store, with evidence pointing at the run's
`result.json` in the worktree. Traces written inside a worktree before this
change are not merged into ranking.

### Review decisions by hand

Read a decision and inspect the task's actual request, diff, checks and
acceptance receipt before labeling it. Merely echoing the model's choice
or a worker's reported success is not verification.

```sh
orc fusion decisions show DECISION_ID
orc fusion decisions label DECISION_ID workflow=build \
  --evidence "Reviewed the request and accepted implementation; tests passed in RUN_ID"
orc fusion decisions export .fusion/decisions/reviewed.jsonl

# Evaluate/calibrate the current checkpoint without training it.
orc fusion decisions calibrate .fusion/decisions/reviewed.jsonl \
  .fusion/decisions/baseline-calibration.json

# Train a separate candidate; the encoder stays frozen.
orc fusion decisions train .fusion/decisions/reviewed.jsonl \
  .fusion/decisions/candidate --epochs 1
orc fusion decisions evaluate .fusion/decisions/reviewed.jsonl \
  .fusion/decisions/candidate-predictions.jsonl \
  --model-path .fusion/decisions/candidate
orc fusion decisions calibrate .fusion/decisions/candidate-predictions.jsonl \
  .fusion/decisions/candidate-calibration.json
```

Exports group by workflow/task so related examples stay in the same split;
approximately 20% of groups are held out. Training uses only train groups
and supervised cross-entropy on the decision head. Evaluation reports
held-out accuracy; `evaluate --control` also scores each held-out example
against a different example's state, and a model that scores about the
same on that control is answering from the question, not the state,
whatever its accuracy says. Calibration fits temperature on train and
reports held-out accuracy, Brier score, expected calibration error with a
ten-bin reliability table, the count of confident wrong answers at 0.9,
0.95 and 0.99, coverage and selective accuracy. Duplicate examples and
train/validation group leakage are rejected.

A bucket qualifies only with at least 20 independent training groups,
20 confident validation groups, and at least 95% selective validation
accuracy. This is a rollout gate, not proof of generalization. Small
datasets remain unqualified. Calibration currently targets one model
identity per report; other language checkpoints abstain from active actions.

Temperature calibration sharpens a distribution whose ordering is already
right; it cannot make a wrong answer right. Run `test/laya_benchmark.py`
before choosing where to spend labeling effort: a kind that ranks the
clear-cut cases correctly at low confidence is the one calibration can carry
over the threshold, and a kind that returns the same answer for most states
will not qualify however many workflows are labeled.

Compare candidate and baseline reports before setting `model_path`,
`calibration_file`, `mode: active` and selected `auto_actions`. None of those
settings is changed by train, evaluate or calibrate. Output commands refuse
to overwrite existing datasets, reports or candidates.

The control room can run this sequence automatically: **Laya lab → Training →
Enable auto-training**. A round exports effective approvals, removes duplicate
inputs (preserving held-out copies), withholds conflicting inputs, evaluates the
configured source, trains a candidate, evaluates that candidate, and saves
calibration. Round receipts and settings are stored in `.fusion/decisions/training`.
Ten new or changed approved answers trigger the next round by default; the threshold
is configurable. Unchanged reapprovals do not trigger training. At least two train
and two held-out workflow groups must survive cleanup.

The UI shows live optimizer loss, paired held-out scores, controls, sample sizes,
and lineage checks. Comparisons require identical source/candidate benchmarks and
verified independent holdouts. Loss or knowledge XP is not evidence of improved
generalization. Small-sample and changing-benchmark limitations remain visible.
The loop advances while the control-room server is running, or whenever
`fusion learn tick` runs (see below); detached steps and saved rounds
survive restarts. Errors wait for an explicit retry. Automatic rounds do not change
the configured checkpoint, calibration file, decision mode, or permitted actions.

## Run the loop without the UI

Label drafting (the garden) and training rounds are advanced by a tick. The
control-room server ticks every three seconds. Without the server, run the
same tick yourself or on a schedule:

```sh
orc fusion learn tick                          # the current workspace
orc fusion learn tick --workspace ~/a --workspace ~/b
orc fusion learn tick --all                    # every workspace the control room knows
orc fusion learn status [--all]                # read-only: is it on, and is it improving?

orc fusion learn schedule install [--interval 300] [--all | --workspace W ...]
orc fusion learn schedule status
orc fusion learn schedule uninstall
```

**Nothing runs unless a workspace opts in.** A tick on a workspace whose
garden and training are both off reads its settings and exits. It creates no
files there, including lock files. Turn each part on per workspace:

- **Garden** (label drafting): **Laya lab → Garden → Enable auto-drafts**.
  The setting is saved in `.fusion/decisions/garden.json` (`enabled`, `agent`:
  `auto|codex|claude|agy|grok`, `labeling_mode`: `single|council`,
  `council_agents`, `council_rule`: `unanimous|available`, `approval_mode`:
  `human|council`). Use the UI rather than editing this file. The UI also
  records when drafting started (`since_ms`), so only new decisions are
  drafted unless you choose to include existing ones. It also records a
  `policy_id`, which is how pausing or changing the policy withdraws council
  approval from a draft that is still running. A hand-written
  `{"enabled": true}` queues every existing undrafted decision and has no
  `policy_id`.
- **Training** (automatic rounds): **Laya lab → Training → Enable
  auto-training**, or write `.fusion/decisions/training/settings.json` as
  `{"enabled": true, "min_new_answers": 10}` (`min_new_answers` is 1–10,000).

Each tick, for each selected workspace:

- **Garden:** if no label draft is running and a decision is waiting, it
  starts one `suggest-labels` job for the oldest waiting decision. Otherwise
  it does nothing. There is one draft at a time per workspace. Drafts call
  your configured worker CLI, so provider usage may be charged.
- **Training:** it takes one step of the round. That step is one of: start
  a round when there is enough new evidence and no manual learning job is
  running; record a finished step's result and launch the next of export,
  baseline, train, evaluate and calibrate; or mark the round as needing
  attention. It waits while a step is running or a round needs a retry.

Jobs run as the same detached supervisors the UI starts, under
`.fusion/ui/jobs`, and keep running after the tick exits. Short, repeated
ticks make the same progress a running server would. Ticks take the same
per-workspace file locks as the server (`.fusion/decisions/garden.lock`,
`label-jobs.lock`, `training-loop.lock`). A scheduled tick and an open
control room can run together without launching the same step twice.

`tick` prints one JSON line per workspace with the time (`at_ms`), garden
state, queue size and active job, training state and last round, the jobs
this tick `launched`, and any `error`. It exits 1 if any workspace reported
an error. `status` prints, per workspace:

- garden `enabled`, `approval_mode`, `queued` and `latest_job`
- training `enabled`, `min_new_answers`, `completed_rounds` and `last_round`
  (with `outcome` and held-out `delta` once a round completes)
- decision counts by state, `drafts` awaiting review, and approved decisions
  and answers
- Laya `mode`, `model_path`, `qualified_buckets` and prediction/label
  agreement

`--all` means every workspace in the control room's registry
(`$ORC_HOME/ui-workspaces.json`, default `~/.config/orc`), plus the current
workspace if it has `.fusion/decisions`. The server never writes its own
start-up workspace to that registry.

`schedule install` on macOS writes
`~/Library/LaunchAgents/ai.orc.fusion-learn.plist` and prints it, then loads
it with `launchctl bootstrap gui/$UID`, replacing any loaded copy. The agent
runs `learn tick` at load and then every `--interval` seconds (default 300,
minimum 60). It appends output to `~/.local/share/orc/learn.log`; the log is
rotated to `learn.log.1` (replacing any older copy) when it exceeds 5 MB. Details of the plist:

- It pins the Python interpreter and the `fusion` script that ran `install`.
  Run it from the installed `fusion`, and run it again after upgrading Python
  or moving the install.
- launchd's `PATH` is minimal, so the plist's `PATH` lists the directories
  where `claude`, `codex`, `agy`, `grok`, `orc` and `node` were found at
  install time. Install a worker later and you need to reinstall the schedule.
- `ORC_HOME` is copied into the plist if it is set.
- `--all` is resolved at every tick, so workspaces added later are included.
- `AbandonProcessGroup` keeps launchd from stopping the jobs a tick started.

`schedule uninstall` runs `launchctl bootout` and removes the plist. On
other systems, `install` prints the equivalent crontab line to add with
`crontab -e` and installs nothing.

Decision states and labels live locally in `.fusion/decisions/events.jsonl`
with private file permissions. They may contain project text; they are
excluded from remote Fusion telemetry and ignored by Git. Export is an
explicit local action. Model setup downloads public checkpoint files;
ordinary inference and training use cached/local files offline.

## Verification

```sh
make test dogfood
make dogfood-paired
~/.local/share/orc/laya/bin/python test/laya_smoke.py --train --acceptance
~/.local/share/orc/laya/bin/python test/laya_benchmark.py
~/.local/share/orc/laya/bin/python test/laya_token_budget.py
```

`dogfood-paired` runs the fixture fan-out workflow twice, with decisions off
and in shadow mode, and fails if shadow changed any node's status or attempt
count, recorded fewer than one successful recovery decision per node, or left
the verdicts and gate count out of `workflow report`. It needs the local Laya
runtime and pays one cold start.

`--acceptance` runs six authored acceptance states on the installed checkpoint
and asserts only the clear-cut ones: a clean success reads plausible, a
"did nothing" claim and a plausible-sounding off-task summary do not. The
near-misses print for comparison and are not asserted.

The benchmark runs authored cases, including deliberate near-misses, against
the installed checkpoint and prints per-kind accuracy on the clear-cut ones,
the probability range, and how many distinct answers the kind produced. It
answers, before anyone spends weeks labeling, whether a decision kind has any
signal on this checkpoint at all. Re-run it after a fine-tune or a checkpoint
change to see what moved. It uses synthetic inputs and contributes no labels.

The standard tests use isolated fixtures and require no Laya installation
or coding-agent calls. The optional smoke test exercises real cached-model
inference, a gradient update, candidate reload/evaluation and calibration
using clearly synthetic inputs. It removes its temporary candidate and
does not contribute production training labels or performance claims.
