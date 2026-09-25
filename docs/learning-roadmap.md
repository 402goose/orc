# Laya learning roadmap

Status as of 2026-09-24, and the plan to make Laya's decisions learn from honest,
automatic evidence. The rules in [FUSION_DECISIONS.md](../FUSION_DECISIONS.md) still
hold: no model promotes itself, a policy choice is never ground truth, and every
label carries its provenance.

## Where it stands

The loop runs end to end without the control room: verdicts become labels, and
`fusion learn tick` (launchd) exports, trains, evaluates and calibrates. The first
complete round (`round-47d56069e88b`) was **flat**: candidate and base checkpoint
both scored 0.929 on 14 held-out questions, the training-majority baseline scored
1.0, and nothing was promoted.

That result was fixed by how labels are gathered, not by the model:

- **No honest negatives exist.** All lead-verdict acceptance labels are
  `plausible=true, failed_task=false` (22:0). A label exists only after a worker
  reported success, the structural gate passed, and the lead accepted. That is
  verification bias: only positives are ever verified.
- **The objective verifier never runs.** Plans carry `verification` commands, but
  build workflows do not execute them. No acceptance-check receipt exists in the
  data.
- **Recorded gate failures are not honest labels.** The three claimed-success runs
  that failed the gate all failed on blocker parsing (since fixed in #60, #73) or
  on `required_handoff` fields that are part of Laya's own input.
- **Outcome events can contain Laya's own veto** once acceptance runs in active
  mode, which would make the model label itself.
- **Routing logs cannot be evaluated off-policy.** The chosen lane is always the
  top of a deterministic ranking (propensity 1 or 0), and the ranking settings are
  not logged.

## Principles (from the research)

- **Objective checks beat judgements, and failed runs are the negatives.** Coding
  verifiers are trained on test-labelled successes *and failures*
  (SWE-Gym, arXiv 2412.21139; R2E-Gym, 2504.07164).
- **Real outcomes beat benchmark proxies.** A critic trained on benchmarks scored
  near chance on production outcomes; "code survival" was a better label than
  "PR merged" (OpenHands, arXiv 2603.03800).
- **Routing is bandit feedback, not classification.** Only the chosen lane's
  outcome is observed. Log propensities now or the data can never evaluate a new
  router (Dudík et al., 1103.4601; BaRP, 2510.07429). Laya does not train routing
  labels.
- **Soft targets and proper scoring, as upstream trains.** Laya upstream trains on
  averaged teacher distributions with a proper-scoring objective; ORC trained hard
  cross-entropy on a frozen encoder until Phase 2.
- **No Jev-derived labels.** TypeSafe's terms (MCA §2.3(b)) prohibit using Jev
  output to train or distill a model.
- **Act only behind a risk bound.** Temperature scaling for readable
  probabilities; a Learn-then-Test / conformal threshold (arXiv 2110.01052) for
  when a head may act; reversible decisions first, acceptance last.

## Phase 1: honest data (in progress)

| # | Change | Label it produces |
|---|---|---|
| 1a | Build workflows execute the plan's `verification` commands as implement acceptance checks. For `debug`, require fail-before / pass-after. | Objective exit-code receipts |
| 1b | Record an unscored acceptance decision for every reported success **before** the gate; label from objective gate codes only (check exit, tree unchanged, required file missing). | `failed_task`, `source: structural_gate` |
| 1c | Outcome events carry `laya_veto` and structured `gate_codes`; ranking and labeling ignore vetoed and blocker-parse-only failures. | Removes self-labeling |
| 1d | Routing logs the ranking policy and per-candidate propensity; small epsilon exploration among qualified lanes on read-only work. | Enables off-policy evaluation |
| 1e | Intake records an explicit `--kind` as user intent. | `workflow`, `source: user_explicit` |

## Phase 2: learning quality

- [x] Train with soft cross-entropy plus upstream's proper-scoring objective,
  class-balanced weights and an optional unfrozen encoder
  (`decisions.training`; see FUSION_DECISIONS.md, "Training objective").
- [x] Choose the checkpoint per decision kind (`decisions.checkpoints`); the
  token budget derives from that checkpoint's limits, so
  `laya-typed-decisions` (1024 tokens) gives acceptance 984 state tokens.
- [ ] Evaluate `laya-typed-decisions` for acceptance on the held-out set. This
  needs per-kind evaluation: `evaluate` still scores every kind on one model.
- [x] Report per-head majority and heuristic baselines on a time-split holdout.
  `decisions.split: "time"` (default) holds out the newest workflow groups;
  `evaluate` reports majority, deterministic-policy and shuffled-state
  baselines per question, and a round is a gain only if it beats all of
  them by more than 0.02.
- [x] Gate any `auto_actions` on a Learn-then-Test threshold with a published
  risk/coverage curve. Exact binomial tests in fixed sequence
  (`decisions.risk`, default α 0.05, δ 0.1, at least 30 held-out answers);
  `allowed()` uses the certified threshold. Temperatures stay fit on train
  groups, because the threshold is chosen on held-out groups and fitting
  both there would reuse them. See
  [Evaluation and gating](../FUSION_DECISIONS.md#evaluation-and-gating).

## Phase 3: more honest labels

| # | Change | Label it produces |
|---|---|---|
| 3a | **ORC gym** (`fusion gym extract/run/report`): merged fix PRs become tasks (B + the PR's tests); each lane runs them with the F2P/P2P tests as `acceptance.before` checks, hidden by default (worker starts at B with the problem text; the tests are `acceptance.fixtures`). See [ORC gym](../FUSION_DECISIONS.md#orc-gym-replayed-fixes-as-benchmark-tasks). | `failed_task` from fail→pass / pass→fail / no change, `source: structural_gate`, on real tasks that can fail; per-lane solve rates on the same tasks |

- **Code survival:** `learn tick` mines git history for ORC-built commits that are
  reverted or re-fixed within a window (delayed negatives) or survive it (matured
  positives).
- **Council soft labels for uncertain items only:** drafting agents sample about
  three times; the mean is a soft target, disagreement a weight (active learning).
- **Recovery reframed** as a noul question, "will a same-lane retry pass?", labelled
  from the next attempt.
- **Review → implementer links** as a separate, excluded-by-default source.

## Phase 4: routing

- Per-arm noul question, "will this lane pass the objective gate?", trained only on
  lanes that ran, weighted by logged inverse propensity.
- Optional difficulty × lane-ability model (IRT) so routing pools evidence across
  lanes without counterfactuals.
