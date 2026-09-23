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
   explicitly reviewed labels with verification evidence enter training
   exports. Human review, candidate fine-tuning, held-out evaluation and
   calibration are separate steps; no model promotes itself.

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
    "model_path": ""
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
The server must remain running to advance the loop; detached steps and saved rounds
survive restarts. Errors wait for an explicit retry. Automatic rounds do not change
the configured checkpoint, calibration file, decision mode, or permitted actions.

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
