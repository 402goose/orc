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
handoffs and an independent review. The plan's `verification` commands that
pass the safety rules run as the implementation's acceptance checks, before
and after the change (see
[Executed plan verification](#executed-plan-verification)); the reviewer still
reruns the project-specific checks. A successful model handoff alone does
not prove product correctness.

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
   routes cannot become writers. A lane whose latest run in the last 15
   minutes hit a quota limit cools down, and equivalent native lanes with
   it. A permission denial in the current execution mode does the same only
   when a denied tool is one every task needs: read, edit, write, list or
   search (`BASELINE_TOOLS` in `fusion_core.py`: `Read`, `Edit`, `Write`,
   `MultiEdit`, `Glob`, `Grep`, `LS`, agy's `ViewFile`/`ListDir`/...;
   matched case- and punctuation-insensitively). Denying anything else,
   such as `Bash` in restricted mode or an MCP tool, is specific to the
   task: the run still fails as `permission_denied`, but the lane stays a
   candidate. Each result and span records `denied_tools`, taken from
   Claude's `permission_denials` or agy's `denied_actions`, else from
   `permission denied: X` blocker lines. A denial that names no tool,
   including spans recorded before `denied_tools` existed, keeps the
   lane-wide cooldown. Explicit agent/route selections stay pinned, and a lane
   with a single candidate records no routing decision: there is nothing
   to choose. Every automatic or within-route choice, single-candidate ones
   included, is still written to the routing log with its propensities; see
   [Routing log and exploration](#routing-log-and-exploration).

   Without a qualified classifier, automatic lanes follow the configured
   preference order. `"decisions": {"rank_by_outcomes": true}` orders them
   by verified outcomes instead: a lane with fewer than 3 checked runs (or
   the integer you set) is tried first so it earns evidence -- on ordinary
   read-only work only: a writer or a review goes to a lane with verified
   evidence once any lane has reached that minimum -- then lanes
   follow smoothed acceptance `(accepted + 1) / (checked + 2)`. Workflow
   gate outcomes and lead verdicts count, and so does an observed error
   (not quota or permission, which are lane health); a worker's
   `STATUS: success` is its own claim until a gate or lead checks it. A
   reported success rejected only by Laya's own veto or by what was parsed
   from the worker's report (blockers, empty handoff fields) is recorded as
   `outcome_excluded` and does not count: otherwise an active acceptance
   head would rank lanes by its own answers. A qualified classifier still
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
   changed, handoff fields present, acceptance commands and executed plan
   verification exited 0, a write node changed the tree) and asks
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
   explicitly enabled council approvals, lead verdicts on acceptance,
   objective workflow-gate results on acceptance (`structural_gate`) and a
   user's explicit `--kind` on intake (`user_explicit`); see
   [Labels recorded automatically](#labels-recorded-automatically). Human review, candidate fine-tuning, held-out evaluation and
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

## Routing log and exploration

Only the chosen lane's outcome is ever observed, so comparing a different
router against past runs needs the probability with which each lane was
picked. Every automatic choice and every model choice inside a named route
with arms appends a `routing_log` event to `.fusion/decisions/events.jsonl`,
including single-candidate choices (those do not ask Laya). Explicit lanes
and pairs are not logged: nothing was chosen. With decisions `mode: off`
nothing is logged and nothing explores.

```json
{"event": "routing_log", "task_id": "RUN_ID", "group": "...", "decision_id": "... or null",
 "scope": "automatic | route_arms", "write": false, "role": "implementation",
 "policy": {"rank_by_outcomes": 3, "explore": true, "warm_epsilon": null,
            "epsilon": 0.1, "routing_epsilon": 0.1, "laya_applied": false},
 "candidates": [{"key": "codex", "checked_runs": 4, "acceptance_rate": 0.75, "...": "...", "propensity": 0.95},
                {"key": "claude", "checked_runs": 0, "...": "...", "propensity": 0.05}],
 "chosen": "codex", "explored": false}
```

`candidates` is the final ranked order with the same evidence fields Laya's
routing decision sees. `policy.rank_by_outcomes` is the minimum checked
runs (`null` when ranking is off), `explore` whether unproven lanes led the
ranking, `epsilon` the exploration rate actually applied to this choice and
`routing_epsilon` the configured one. `propensity` is the probability this
policy picks each candidate in this state: 1.0 for the chosen lane and 0 for
the others when the pick is deterministic, including when a qualified Laya
recommendation is applied.

**Exploration.** `"decisions": {"routing_epsilon": 0.1}` (default 0, the
deterministic behaviour) picks uniformly among the candidates with
probability epsilon, so with k candidates the ranked top has propensity
`(1 - epsilon) + epsilon / k` and each other `epsilon / k`. It applies to
ordinary read-only work only: never to a writer, a review, a task that must
use a different agent than the implementer, a pinned lane, or a choice where
a qualified Laya recommendation was applied. Candidates are the lanes that
already passed every safety filter, so exploration can never pick a lane
those filters dropped. The application record's reason says when
exploration picked the lane.

**Report.** `orc fusion decisions routing-report` (read-only) joins each run's
latest `routing_log` to its latest outcome by run id -- gate outcomes and
lead verdicts; outcomes marked `laya_veto` are skipped -- and reports per
lane, over the logged choices where it was a candidate: `available`,
`chosen`, `accepted`, `observed_acceptance` (on-policy, biased by the
ranking), `ips_acceptance` (sum of accepted/propensity over the lane's
picks, divided by `available`), `snips_acceptance` (the same, normalised by
the summed weights), and `ess`, the effective sample size
`(sum w)^2 / sum w^2`. A lane that had propensity 0 anywhere it was
available, or was never chosen, is marked `insufficient overlap` and gets
no estimate: deterministic logs cannot say how an unchosen lane would have
done. Runs logged before this existed have no `routing_log` and are not
counted.

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
    "checkpoints": {},
    "training": {
      "objective": "soft_ce+proper_scoring",
      "unfreeze_encoder": false,
      "encoder_learning_rate": 2.5e-5,
      "label_smoothing": 0.0,
      "class_balance": true,
      "max_class_weight": 4.0
    },
    "verdict_labels": true,
    "automatic_labels": true,
    "split": "time",
    "risk": {"alpha": 0.05, "delta": 0.1, "min_examples": 30, "min_groups": 20}
  },
  "verification": {
    "execute": true,
    "runners": ["python3", "python", "pytest", "npm", "pnpm", "yarn", "bun", "node", "deno",
                "go", "cargo", "make", "uv", "ruby", "rspec", "bundle", "mvn", "gradle"],
    "timeout_seconds": 900
  }
}
```

`automatic_labels: false` stops gate and `--kind` labels (verdict labels
have their own switch). `verification` governs plan commands only; authored
`acceptance.checks` are unaffected by it.

`python` or `FUSION_LAYA_PYTHON` can select a different runtime interpreter.
`FUSION_DECISIONS_MODE=off` disables classification and decision logging.
`mode: off` in configuration has the same effect. Existing orchestration
and deterministic automatic routing still work.

Active actions require **all** of:

- `mode: active` and the decision kind listed in `auto_actions`;
- a qualified calibration bucket for the exact question schema and model
  identity, including weights, tokenizer, configuration and SDK version;
- probability at least the bucket's certified Learn-then-Test threshold
  (see [Evaluation and gating](#evaluation-and-gating)); a calibration report
  from before certified thresholds uses the higher of `threshold` and the
  bucket's threshold instead;
- complete state, instructions and options within the checkpoint's token
  limits; any detected truncation forces abstention;
- all deterministic permission, availability, attempt and acceptance gates.

The SDK's entropy-derived `confidence` is not treated as a probability.
Fusion uses the selected label's distribution and fitted temperature.

### Checkpoint per decision kind

`checkpoints` maps a decision kind to a published checkpoint name
(`english`, `typed-decisions`, `multilingual`) or a checkpoint directory
(relative paths resolve against the workspace):

```json
{"decisions": {"checkpoints": {"acceptance": "typed-decisions"}}}
```

A kind's entry wins over `model_path`; a kind without one uses `model_path`,
else the runtime's default English/multilingual routing. The upstream
typed-decisions checkpoint (ModernBERT-large, 1,024-token context, 256-token
question head, tuned on typed workflow decisions) is not a default for
Fusion's schemas; it is subject to the same qualification as any other
model. Nothing is downloaded at inference or training: a checkpoint that is
not cached fails with the command that fetches it,
`orc fusion decisions setup --checkpoint typed-decisions`.

The state token budget (below) derives from the kind's checkpoint:
`max_len` minus the question head, read from `fusion_decisions.CHECKPOINTS`
for a name or from the directory's `rl_agent_config.json`. On
`typed-decisions` an acceptance input may use 984 estimated tokens instead
of 472. `max_state_chars` (default 2,200, at most 6,000) still caps the
characters, so raise it to use the larger budget. The multilingual
checkpoint's tokenizer was never measured against the estimate, so it keeps
the English budget. Each recorded decision stores the budget it was built
under (`state_tokens`), and exports judge unscored inputs by it.

`decisions train --kind acceptance` trains a candidate for one kind from
that kind's entry. Without `--kind` (as the automatic training loop runs it),
kinds configured for a different checkpoint are left out of training. The
loop's evaluation still scores every kind on one model, so an acceptance
input built for 1,024 tokens stops an automatic round at evaluation
("candidate truncates a reviewed example"); until evaluation is per kind,
train and evaluate such a kind by hand.

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
already has a complete acceptance decision is labeled in place: the input
the structural gate labeled when there is one (so a run stays one example),
else the latest complete one. Otherwise
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
that decision are never overwritten. A verdict supersedes a
`structural_gate` answer only for the questions it answers; the gate's
other answers stay, still tagged `structural_gate`.

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
else. A gate's accept/reject is not a label either; only the objective codes
behind it are (below).

### Labels recorded automatically

Three sources label without a click. Each is `verified`, carries evidence,
and every exported answer names its source in `label_provenance`:

| Source | Decision | Answers | Written when |
| --- | --- | --- | --- |
| `lead_verdict` | acceptance | `plausible`, `failed_task` (above) | `fusion outcome` with a reason |
| `structural_gate` | acceptance | `failed_task` only | a workflow node reported success and the gate's objective codes decide it |
| `user_explicit` | intake | `workflow` | a user typed `fusion build --kind discovery\|build\|debug\|review` |

`mode: off` turns all of them off; `"automatic_labels": false` turns off
`structural_gate` and `user_explicit`.

#### Structural gate labels

For every workflow node whose worker reported `success`, Fusion records an
`unscored` acceptance decision (`source: "structural_gate"`) **before** the
structural gate runs, with the same input `accept_node` builds. A failed
gate therefore leaves a labelable example too, not only the successes that
reach the classifier. After the gate, the input is labeled only from these
objective codes:

| Gate result | Label |
| --- | --- |
| the gate passed and a check failed before the change and passed after it (fail→pass) | `failed_task=false` |
| a check that passed before the change fails after it with a test failure (pass→fail: the change broke what the plan said must pass) | `failed_task=true` |
| a first attempt finished without changing the tree (`write_no_change`, attempt 1) | `failed_task=true` |
| a check that failed before and still fails (fail→fail), or whose baseline is unknown | unlabeled |
| a later attempt that changed nothing (its tree holds earlier attempts' work) | unlabeled |
| anything else | unlabeled |

This follows SWE-bench's FAIL_TO_PASS / PASS_TO_PASS reading. Fail→fail is
not evidence because a plan's command can cover a pre-existing failure that
has nothing to do with the task: on 2026-09-24 a plan checked a whole test
file with an environment-caused failure, and the earlier rule labeled a
correct implementation `failed_task=true` twice (those labels were
retracted).

- `plausible` is never labeled by the gate. It asks whether the *report*
  plausibly matches the task, which no exit code decides.
- Blockers and `required_handoff` fields never label. They are parsed from
  the worker's report and are Laya's own inputs; a gate failing on them is a
  parser or reporting fact, not evidence about the work. A missing or
  unchanged required file blocks a positive label but is not a negative
  one: the plan, a model, named the file.
- A check whose runner could not run it (`error`, `timed_out`, or pytest exit
  codes 2-5: interrupted, internal error, usage error, no tests collected) is
  not a test failure and does not label.
- Laya's veto never enters: the label is computed from the codes alone, so an
  active acceptance head can never label itself.
- An authored check has no pre-change run unless its node opts in, so its
  baseline is unknown and neither its failure nor its pass labels. A write
  node with `"acceptance": {"checks": [...], "before": true}` runs its
  authored checks once on the tree before the first attempt, exactly as plan
  checks run (receipts under `attempt-1/before-check-*`, summary in
  `acceptance/before-authored.json`, reused by retries and resumes), and
  then labels them by the table above. Unlike plan checks they run with the
  same environment and timeout before and after, and are not filtered by
  `fusion_verification`: they are authored, not model output. `before` on a
  read-only node, or a non-boolean, is a spec error.

The node result in `node.json` and the manifest keeps `gate_codes` (one
structured code beside each gate problem: `worker_status`, `worker_blockers`, `required_file_missing`,
`required_file_unchanged`, `required_handoff_empty`, `check_failed`,
`check_error`, `check_unpersisted`, `write_no_change`, `coordinator_error`,
`repeated_failure`) and `gate_label` (what was written, or why nothing was).

#### Precedence

A source never overwrites a label from a higher one; a higher source
supersedes a lower one only for the questions it answers:

`human` = `human_approved_suggestion` = `user_explicit` >
`council_approved_suggestion` > `lead_verdict` > `structural_gate`

A gate labels only the fresh input it recorded, so it never overwrites
anything. A lead verdict replaces its own earlier answers and the gate's
answers to the questions it answers, and keeps the rest. A council approval
is not blocked by gate labels (it is by any other source's). Every label
event stays in `events.jsonl`, so a disagreement between a gate and a later
verdict remains auditable.

```sh
orc fusion decisions export .fusion/decisions/no-gate.jsonl --exclude-source structural_gate
orc fusion decisions export .fusion/decisions/judged.jsonl --exclude-source structural_gate --exclude-source user_explicit
```

#### Outcomes are not labels, and Laya's veto is not an outcome

Workflow outcome events carry `laya_veto` and `gate_codes`. A reported
success rejected only by Laya's veto, or only by `worker_blockers`,
`required_handoff_empty` or `repeated_failure`, is written as
`outcome_excluded` with an `excluded_reason`, so route ranking never reads
it (`fusion_policy.outcome_counts`). No label is derived from outcomes.

#### Intake intent

`fusion build --kind K` records `explicit_kind` and `kind_source` in the
intake application event. When the user typed it (`kind_source: "user"`),
the intake decision is labeled `workflow=K` with `source: "user_explicit"`:
it is the user's own statement of which work they asked for. The fallback
regex, Laya, an MCP lead (`fusion_run_start`, `kind_source: "agent"`),
Truffle (`"truffle"`) and `--from-workflow`'s default (`"default"`) never
label. No label is written when the request's own scope overrides the kind
(a planning-only request becomes `discovery`), for `sweep` (not an intake
answer), or when the input is truncated. `needs_clarification` stays
unlabeled.

#### Executed plan verification

The plan node of a generated `build`/`debug` workflow (Truffle queues use
`debug`) declares `verification` commands in its acceptance contract. The
implementation node opts in with `acceptance.plan_verification`, and the
commands that pass the rules below run as its acceptance checks, with the
same receipts, timeouts and process-group cleanup as authored checks. They
run twice: once on the tree before the first attempt (receipts under
`nodes/implement/acceptance/attempt-1/before-check-*`, summary in
`acceptance/before.json`, reused by retries and resumes), and after every
attempt. A check that already passed before the change is `vacuous`: it
cannot tell whether the work was done, and it never produces a negative
label. The after-receipt records `origin: "plan"`, `vacuous` and `before`.

The commands are model output and run unsandboxed with your privileges, so
`fusion_verification` admits only:

1. **argv, no shell.** An argv array is used as is. A string is split with
   `shlex` only when it has no shell syntax: operators, pipes, redirection,
   `$`/backtick expansion, globs or brackets, braces, `~`, `!`, `#`,
   backslashes, newlines or a leading `VAR=` assignment (so a parametrized
   pytest id such as `test_x[a]` must be an argv array). Anything else is dropped, never run through a shell: a
   command a shell would have rewritten would fail without one for reasons
   unrelated to the work and become a false negative label.
2. **An allowlisted runner.** `argv[0]` must be a bare program name (no
   path: a path could be a script the plan or implementer wrote) listed in
   `verification.runners`. The default covers the test runners and the build
   tools that invoke them across common ecosystems: `python3`, `python`,
   `pytest`, `npm`, `pnpm`, `yarn`, `bun`, `node`, `deno`, `go`, `cargo`,
   `make`, `uv`, `ruby`, `rspec`, `bundle`, `mvn`, `gradle`. Wrappers such as
   `./gradlew` are paths and are refused; add a runner name to opt in.
3. **No installs, publishing or inline code.** Package managers are held to
   test/run subcommands (`npm test|run`, `go test|vet|build`, `cargo
   test|check|build|clippy|fmt`, `uv run`, `bundle exec`, ...); `install`,
   `add`, `ci`, `update`, `publish`, `deploy`, `pip`, `dlx`/`npx`, `uv run
   --with` and similar are refused. So are `python -c`, `python -m pip`,
   `node -e/-p`, `ruby -e` and `deno eval`.
4. **Offline by request.** Checks run with `PYTHONDONTWRITEBYTECODE=1`,
   `PIP_NO_INDEX=1`, `UV_OFFLINE=1`, `npm_config_offline=true`,
   `YARN_ENABLE_NETWORK=0`, `GOPROXY=off` and `CARGO_NET_OFFLINE=true`. That
   asks tools not to fetch; it is not a network sandbox.
5. **Bounded.** At most 8 commands, 64 arguments and 400 characters each,
   `verification.timeout_seconds` (default 900) per run.

Refused commands are listed in the node result's `verification_rejected`
with a reason and stay in the plan for the reviewer. This is not a sandbox:
an allowed runner executes repository code (tests, `conftest.py`,
`package.json` scripts, Makefiles) exactly as the worker was already told
to. The rules bound which tools a plan can invoke, not what repository code
does. `"verification": {"execute": false}` records the commands without
running them. Authored workflows run only their own `acceptance.checks`.

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

The English checkpoint's encoder reads 512 tokens: the question head, then
the state, which gets whatever is left. The longest acceptance question's
head takes 40, so an acceptance input has 472 tokens
(`fusion_decisions.state_tokens`; a 1,024-token checkpoint configured for
acceptance gives 984, see "Checkpoint per decision kind"). A
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

# Train a separate candidate (decisions.training sets the objective).
orc fusion decisions train .fusion/decisions/reviewed.jsonl \
  .fusion/decisions/candidate --epochs 1
# Or one kind, starting from that kind's decisions.checkpoints entry.
orc fusion decisions train .fusion/decisions/reviewed.jsonl \
  .fusion/decisions/acceptance-candidate --kind acceptance
orc fusion decisions evaluate .fusion/decisions/reviewed.jsonl \
  .fusion/decisions/candidate-predictions.jsonl \
  --model-path .fusion/decisions/candidate
orc fusion decisions calibrate .fusion/decisions/candidate-predictions.jsonl \
  .fusion/decisions/candidate-calibration.json
```

Training uses only train groups, with the objective described in
"Training objective" below. Duplicate examples and train/validation group
leakage are rejected.

### Evaluation and gating

**Time-split holdout.** Exports group examples by workflow (`context.group`,
else `task_id`) so a workflow is never split. `decisions.split: "time"` (the
default) dates each group by its first recorded decision, labeled or not, and
holds out the newest groups: about 20%, and at least two on each side once
there are four or more labeled groups. The model is trained on older work and
judged on newer work, as it will be deployed, so drift shows up as a worse
score instead of being averaged away. `"group-hash"` is the earlier
assignment by a hash of the group name; use it (or `export --split
group-hash`) to reproduce a round measured before this change. Each exported
row carries `group_first_ms`, and the export reports `split_method`.

A group's date never moves, so a newer group can only push an older one from
validation into training. The reverse can happen once: when an older group is
labeled late, the held-out count (20% of groups) can grow by one and take the
next-newest training group. A candidate trained in an earlier round that saw
that group is caught at evaluation, which compares the candidate's
`seen_train_groups` with the held-out groups and marks the holdout
`contaminated`.

**Baselines per question.** `evaluate` reports, for each held-out question
(`kind:question`), the number of answers and groups, the candidate's
accuracy, and each baseline that applies to it, each compared on only the
answers it covers:

| Baseline | Answer |
|---|---|
| `majority` | Most common training label for that exact question schema. |
| `heuristic` | What the deterministic policy answers without Laya, exported with each row as `heuristic`. |
| `control` | The candidate itself, reading another held-out example's state (`evaluate --control`). A model that scores the same here is answering from the question, not the state. |

The deterministic answers (`fusion_decisions.heuristic_answers`):

| Question | Policy answer | Source |
|---|---|---|
| `intake.workflow` | the fallback kind: planning-only scope → `discovery`, fix/bug words → `debug`, a leading "review" → `review`, else `build` | the `application` event's `actual`; skipped when the caller named the kind (intent, not a heuristic) |
| `intake.needs_clarification` | `false`; intake never stops to ask | constant |
| `review.specialty` | `general` | constant (`review_task`'s default focus) |
| `review.needs_review` | `true`; a requested review always runs | constant |
| `recovery.action` | continue / stop on quota, permission or attempt limit / switch / repair | the `application` event's `actual` |
| `acceptance.plausible`, `failed_task` | `true`, `false`: trust the worker's reported success | constant |

When Laya's answer was applied (`applied: true`), there is no policy answer
for that decision. The acceptance baseline is not the structural gate's
result, because a gate-sourced label is that result and would score 1.0 by
construction. Routing has no labeled questions: routes are bandit feedback
(see [Routing log and exploration](#routing-log-and-exploration)).

A round's outcome is **gain** only if the candidate beats the source
checkpoint *and* every applicable baseline on the held-out answers by more
than 0.02 accuracy (`IMPROVEMENT_MARGIN`). It is **regression** if it trails
the source by more than that, and otherwise **flat**; a note names each
baseline that was not beaten. Evaluations saved before per-question
baselines compare against their overall majority and control scores.

**Temperature.** Calibration fits one temperature per question bucket
(`kind:schema_hash:question`) on train groups, so the probabilities shown are
readable, and reports held-out accuracy, Brier score, expected calibration
error with a ten-bin reliability table, and the count of confident wrong
answers at 0.9, 0.95 and 0.99. Temperatures are not fit on held-out groups:
the acting threshold below is chosen on those, and would reuse them.

**When a head may act: Learn-then-Test.** For each bucket, calibration takes
the held-out (temperature-scaled top probability, correct) pairs and
certifies a threshold with Learn-then-Test (Angelopoulos et al., arXiv
2110.01052; `fusion_risk.py`):

1. Each grid threshold t from 1.00 down to 0.50 is a hypothesis "the error
   rate of the decisions acted on at t exceeds α".
2. Its p-value is the exact binomial tail P(Binomial(n_t, α) ≤ errors_t)
   over the n_t held-out answers at or above t. Rejecting at δ is the same as
   the one-sided Clopper–Pearson upper bound at 1−δ being at most α.
3. Thresholds are tested in a fixed sequence, most conservative first, each
   at level δ, stopping at the first that is not rejected. Fixed-sequence
   testing controls the family-wise error at δ without a multiplicity
   correction, so every rejected threshold is certified at once.
4. The sequence skips thresholds with fewer acted answers than zero errors
   could ever reject (45 at α = 0.05, δ = 0.1). That depends only on the
   confidences, never on correctness, so the sequence is still fixed before
   testing.
5. The certified coverage is the lowest rejected threshold's. The published
   threshold is the *highest* certified one that acts on the same held-out
   answers, so the head does not act on confidences it never showed.

The guarantee: with probability at least 1−δ over the held-out draw, the
error rate among acted decisions is at most α, assuming future decisions are
exchangeable with held-out ones. The time split is what tests that
assumption honestly. The loss is 0/1, so the binomial tail is exact.
Hoeffding and Hoeffding–Bentkus bounds hold for any bounded loss and are
looser for a binary one, which at 30–100 held-out answers decides whether a
bucket qualifies at all.

Each bucket stores `risk`: `threshold`, `alpha`, `delta`, `n`, `coverage`,
`risk` (empirical), `upper_bound`, `min_acted`, and the risk–coverage
`curve` (acted count, errors, coverage, empirical risk, upper bound and
p-value at every grid threshold). The bucket's `threshold` is the certified
one, or the `--threshold` fallback when nothing is certified, which is shown
for reporting only. `status` says why a bucket is not qualified.

A bucket **qualifies** only with all of:

- a certified threshold;
- at least `risk.min_examples` held-out answers (default 30); fewer is
  `not qualified (n<30)` whatever they show;
- at least `risk.min_groups` train groups and held-out groups acted on
  (default 20), because answers within one workflow are not independent.

`DecisionEngine.allowed` then acts only when the probability, rescaled under
the calibration read at that moment, reaches the certified threshold. The
fixed `decisions.threshold` no longer applies to such a bucket. Configure
the gate in `.fusion.json`:

```json
{"decisions": {"risk": {"alpha": 0.05, "delta": 0.1, "min_examples": 30, "min_groups": 20}}}
```

`orc fusion decisions calibrate` reads it from the workspace configuration.
What the defaults cost: with no errors, 45 acted held-out answers are
needed, and 77 with one error. At α = 0.05 and δ = 0.1, none of today's
buckets can qualify. That is the intended outcome.

Put reversible decisions in `auto_actions` first: review specialty and
intake, then recovery, and acceptance last. A wrong review focus costs one
review. A wrong acceptance ships the wrong work.
Calibration currently targets one model identity per report; other language
checkpoints abstain from active actions.

Temperature calibration sharpens a distribution whose ordering is already
right; it cannot make a wrong answer right. Run `test/laya_benchmark.py`
before choosing where to spend labeling effort: a kind that ranks the
clear-cut cases correctly at low confidence is the one calibration can carry
over the threshold, and a kind that returns the same answer for most states
will not qualify however many workflows are labeled.

#### Training objective

`train` ports upstream Laya's fine-tuning objective (the typed-decisions
notebook's training step, scored with `laya.common.proper_reward`):

- **Soft-target cross-entropy**, weight 1.0. A reviewed hard label is a
  one-hot target, mixed with the uniform distribution by `label_smoothing`
  (default 0). An exported row may instead carry
  `targets: {question: {label: probability}}`, for example the mean of
  several drafting samples, and `weights: {question: w}`, for example
  council agreement; both are used as given.
- **Proper-scoring policy term** (`objective: "soft_ce+proper_scoring"`, the
  default; `"soft_ce"` turns it off): four Gaussian-noised copies of the
  logits (noise projected to zero mean over the options; standard deviation
  0.4 in the first epoch falling linearly to 0.1 in the last) are scored
  against the target by log score plus 0.75 × spherical score, less the
  ranked probability score on `score` questions. Each sample's advantage over
  the group mean, normalized, weights its Gaussian log-likelihood.
- **Class balance** (`class_balance`, default on): per question schema, a
  class seen n times gets weight min(`max_class_weight`, n_majority / n),
  where an item's class is its target's argmax. The cap (default 4) follows
  upstream's advice to weight rare classes ×3–4, and bounds how far a handful
  of rare examples can pull the head. A class with no examples gets no
  weight, so an all-positive split (the first round's 22:0) trains
  unweighted; balancing cannot invent the missing negatives. Item weights
  (class × row weight) are rescaled to mean 1, so the loss keeps its scale.
- **Encoder** (`unfreeze_encoder`, default false): frozen, only the decision
  head trains at `--learning-rate` (default 1e-4). Unfrozen, the encoder
  trains too at `encoder_learning_rate` (default 2.5e-5, upstream's); this is
  slow on CPU and needs several GB of memory for ModernBERT-large.
- **Determinism**: `--seed` seeds Python, torch and the noise generator, and
  each epoch's example order. AdamW (weight decay 0.01), gradient clipping at
  1.0. A truncated training example still stops training before any update.

`training.json` records the objective and all of the above: settings,
upstream constants, per-class weights, the item weight range, learning
rates, noise by epoch, seed, starting checkpoint and its token limits, and
the soft cross-entropy by epoch. The loss curve is the soft cross-entropy;
the policy term is a zero-mean surrogate whose value is not a loss, and is
reported only as `mean_objective`. `test/laya_training_objective.py` checks
the objective against a pure-Python reference with the managed runtime and,
with `--train`, fine-tunes the cached English checkpoint on a small synthetic
set.

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

## ORC gym: replayed fixes as benchmark tasks

`fusion gym` turns ORC's own squash-merged fix PRs into benchmark tasks and
runs them on several lanes, so the structural gate produces honest labels,
negatives included, and the same task yields lane-vs-lane evidence. It
follows SWE-smith (arXiv 2504.21798: tasks from reverted fixes) and grades
like SWE-bench (FAIL_TO_PASS / PASS_TO_PASS).

```sh
# 1. Extract tasks (git + unittest only; gh reads PR/issue text).
orc fusion gym extract --prs 60 63 73 86 87 88 103 104 --ref origin/main \
  --github-repo hathbanger/orc --out ~/orc-gym/tasks
# 2. Run each task on each lane (sequential, resumable, budget-capped).
orc fusion gym run ~/orc-gym/tasks --workspace ~/orc-gym/ws \
  --lanes claude-sonnet-high claude-opus-high claude-fable-medium agy --budget-usd 10
# 3. Per-lane and per-task results.
orc fusion gym report ~/orc-gym/ws
```

**Extraction.** For PR N's squash commit C (found by `mergeCommit` from gh,
else by a `(#N)` subject on `--ref`) with parent B, files under `test/` or
`tests/` or named `test_*.py` / `*_test.py` are tests; everything else is
source. The task commit is B plus C's test-file changes, written as a commit
object (fixed author and date, message without the PR number) to
`refs/gym/tasks/pr-N` in the source repository. That ref is the only thing
extraction writes there; both trees are materialized with `git archive`.
Every test in the changed unittest files runs on the task tree and on C.
FAIL_TO_PASS are the changed or new test methods (a changed class fixture
or module-level code marks its tests changed) that fail or error on the task
tree and pass on C; PASS_TO_PASS are the other tests there that pass on both
(at most `--p2p-limit`, default 200). Each file's ids become one argv
command, `python3 -m unittest discover -s test -t test -p FILE -k
'*module.Class.method' ...`; those commands are run again on both trees and
a task is kept only if its F2P commands fail on the task tree and pass on C.
A P2P command that is not green on both is dropped. PRs with no test change,
only test changes, no Python unittest file or no F2P test are skipped with
a reason. Tasks are JSON (`id`, `base`, `fix`, `task_ref`, `task_sha`,
`prompt`, `fail_to_pass`, `pass_to_pass`, `checks`, `test_files`,
`source_files`, `pr_url`) plus `index.json`.

**Prompt.** The linked issue's title and body when the PR closes one
(sections headed Fix, Solution, Proposed, Plan, Changes are cut). Otherwise
the PR title without its `fix(scope):` prefix plus the body's paragraphs up
to the first that describes the change: a bullet list, or a paragraph that
starts with "This PR", "Now", "New", "Changes", "Validation", a change verb
("Adds", "Replaces", "Reworks", ...), or says "now" in its first sentence.
Lines inside kept paragraphs that start with "Fix:" or "Solution:" are
dropped, as are Markdown headings. Fenced diffs are removed; other code
blocks (usually the failing input) are kept. `--no-gh` uses the commit
subject.

**Runs.** The gym directory is its own Fusion workspace: `.fusion` (runs,
traces, decisions, labels) and a `.fusion.json` copied once from the source
repository. It must be outside the source repository, which a run only
reads (`git fetch` of the task ref). Each task gets a repository under
`tasks/<id>/repo` holding only the task commit and its history, never C,
and each lane a worktree of it, removed after the run unless
`--keep-worktrees`. The lane runs one authored write node (`max_attempts:
1`, `required_handoff: [summary]`) through `WorkflowRunner`, with the
worktree as the worker's directory and the gym as the control workspace;
its `acceptance.checks` are the F2P then P2P commands with `before: true`.
Lanes are `claude-sonnet-high`, `claude-opus-high`, `claude-fable-medium`,
`claude`, `codex`, `agy`, `grok`, any configured route name, or entries in
`gym.lanes` (`{"agent", "route", "model", "reasoning_effort"}`) in the gym's
`.fusion.json`. `--budget-usd` caps one invocation: no run starts once it is
spent, the workflow budget is what is left, and Claude lanes get it as
`max_budget_usd`. `--max-tasks N` processes at most N tasks with pending
lanes.

A task × lane pair is complete once it was dispatched and its checks ran.
A lane that is unavailable (not on PATH, cooling down), paused on quota or
budget, or interrupted is recorded but retried by the next `gym run`. Each
run appends to `results.jsonl` and saves the worktree's diff under
`results/<task>/<lane>/`.

**What it measures.** Per lane: tasks attempted, `solved` (every F2P
command passed, no P2P failed, no test file touched), F2P pass rate, P2P
regressions, test tampering, cost and mean wall time; per task, which lanes
solved it. `invalid_baseline` (an F2P check that did not fail, or a P2P
check that did not pass, before the change on this machine) is excluded
from the rates. This is per-lane evidence on the same tasks, not a
counterfactual: each lane is observed once per task, with no retries.

**Labels.** Every run is an ordinary workflow (id `gym-<task>-<lane>-…`),
so the structural gate labels it automatically, as `structural_gate`, in
the gym workspace's decision store: all F2P passed with the gate passing is
`failed_task=false`; a P2P test that passed before and fails after is
`failed_task=true`; no change on the first attempt is `failed_task=true`;
F2P still failing is unlabeled (fail→fail). The label's `group` is the gym
workflow id. Laya trains on them with `orc fusion learn tick --workspace
~/orc-gym/ws`, or export with `decisions export` there.

**Leakage and caveats (v1).**

- The regression tests are in the tree. A worker can read them, and a worker
  that edits a test file is reported `tampered`; the gate may still have
  labeled its run, so audit `tampered` rows before training on them.
- Workers are not told which checks will run; a restricted Claude writer
  cannot run tests at all. That is the lane as ORC runs it, not its ceiling.
- Prompts come from issues and PR descriptions written after the fact; the
  cut is heuristic and can leave a hint of the fix. The PR title often names
  the desired behavior.
- The task repository has no C, but the network does: a worker with `gh` or
  a browser can look up the issue and its linked PR.
- Earlier lanes' diffs stay under `results/` in the gym directory, readable
  by a later worker that leaves its worktree.
- Tests run on the host with no sandbox, as all acceptance checks do. A
  flaky test can make a pass look like a regression.

**Scheduling.** `gym run` holds a lock on the gym directory, skips
completed pairs and stops at its budget, so a periodic invocation advances
it:

```sh
# crontab: every night at 02:00, at most 3 tasks and $5 per night.
0 2 * * * cd ~/orc && ./orc fusion --quiet gym run ~/orc-gym/tasks --workspace ~/orc-gym/ws --lanes claude-sonnet-high agy --max-tasks 3 --budget-usd 5 >> ~/orc-gym/gym.log 2>&1
```

Re-extract after new fix PRs merge (existing task shas do not change);
`gym report` reads receipts only and can run at any time.

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
- `measured_round`: the newest completed round's `outcome`, `margin`, overall
  `baselines`, and per question (`kind:question`) the `holdout_n` and
  `holdout_groups`, the candidate's accuracy beside `source`, `majority`,
  `heuristic` and `control`, and the candidate calibration's `gate`: either
  "acts at p>=T, coverage C of N held-out; error <= α with probability 1−δ"
  or why it is not qualified, e.g. `not qualified (n<30)`
- decision counts by state, `drafts` awaiting review, and approved decisions
  and answers
- Laya `mode`, `model_path`, `qualified_buckets`, the configured calibration
  file's `gates` per question (the ones `allowed()` uses now), and
  prediction/label agreement

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
