# Fusion research and build notes

Updated 2026-09-18. This is a curated working set of high-signal sources for multi-model coding agents, model routing, and Claude/Codex delegation. “Fusion” here means a harness that combines agents at run time; it does not mean merging model weights.

## What the linked Artificial Analysis post says

The linked post is the Artificial Analysis thread on Cognition’s Devin Fusion:

- [Artificial Analysis thread](https://x.com/artificialanlys/status/2098504936984293447?s=46&t=6-cVizCt6wEDkRXUxmV5dA)
- [Artificial Analysis mirror with the thread text](https://es.twstalker.com/ArtificialAnlys/status/2098504936984293447)
- [Cognition: Fusion in Devin Desktop and CLI](https://cognition.com/blog/local-fusion)
- [Cognition: Devin Fusion technical breakdown](https://cognition.com/blog/devin-fusion)

The useful architectural claims are:

1. A frontier lead owns planning, ambiguity, delegation, and final review.
2. A cheaper sidekick runs bounded implementation, exploration, tests, or other mechanical work.
3. The two agents keep separate persistent contexts and exchange briefs, results, and feedback instead of copying entire transcripts.
4. Routing can change during a session when the current work becomes harder or easier. Cognition describes doing this at compaction boundaries to avoid throwing away a useful prompt cache.
5. The lead can take control back when the sidekick is out of depth.

The benchmark results are vendor and benchmark claims. Artificial Analysis reports a Claude Fable 5.1 plus SWE-2 sidekick configuration around 62 on its Coding Agent Index, and an Astra plus SWE-2 configuration that is cheaper and faster while scoring lower. Cognition’s own updated table reports task-level savings across several coding benchmarks. These numbers motivate the architecture; they are not a guarantee for a local workflow.

The most important failure case in Cognition’s examples is a task where the judgment itself is the deliverable. Delegating implementation on a mechanically specified task worked well; delegating a subtle product decision caused a large quality drop. Our harness therefore makes the lead explicit and returns structured evidence for review.

## Existing tools and protocols

| Source | Pattern | Use in this build |
| --- | --- | --- |
| [hathbanger/orc](https://github.com/hathbanger/orc) | Runs Claude Code against OpenRouter models, tracks model fit, quality, context, and spend | Keep as the provider and model launcher. Fusion is a separate orchestration layer beside it. |
| [dwgx/claude-codex-subagent](https://github.com/dwgx/claude-codex-subagent) | Claude is the orchestrator; `codex exec` is a fresh-context worker; uses personas, JSON output, resume, and bounded dispatch | The closest existing Claude-to-Codex implementation. Fusion generalizes the adapter in both directions and adds a run ledger plus workspace lock. |
| [OpenAI Codex CLI](https://github.com/openai/codex) | `codex exec --json` emits `thread.started`, `item.completed`, `turn.completed`, `turn.failed`, and `error` events | Fusion parses those events and stores the thread id for a later sidekick turn. |
| [Anthropic Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage) | Print mode, JSON/stream-JSON output, session resume, MCP configuration | Fusion uses print mode for bounded Claude workers and MCP for the lead surface. |
| [Model Context Protocol](https://github.com/modelcontextprotocol/servers) | Standard way to expose tools and data to different agent clients | Fusion exposes `fusion_delegate` and `fusion_status` over stdio MCP, so Claude or Codex can be the lead. |
| [Agent Client Protocol](https://github.com/agentclientprotocol/rust-sdk) | Standardizes communication between editors and coding agents; supports client, agent, proxy, and conductor roles | A later UI/editor integration path. MCP is enough for the first local harness. |
| [OpenAI Agents SDK patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns) | Handoffs transfer ownership; agents-as-tools keep a manager in control; parallelization and judge loops are explicit patterns | Fusion follows the manager-as-lead pattern for coding work. |
| [UltraCode](https://github.com/diepquynh/ultracode) | Staged explore/spec/plan/implement/review workflow with cross-harness artifacts and model routing | Fusion adopts the bounded stage pipeline and artifact handoffs, with ORC model selection and per-stage budget caps. |

## What the Saloon run taught us about fan-out

The Claude run in `~/saloon` was a dynamic workflow rather than a simple
three-stage pipeline. It produced 148 recorded agent files for the strategy
workflow and 127 for the pro-se workflow. The useful mechanism is described in
Anthropic's [dynamic workflow cookbook](https://platform.claude.com/cookbook/claude-agent-sdk-08-dynamic-workflows): Claude writes an orchestration script, the runtime executes independent tasks concurrently, intermediate results live in script state, and a run can be resumed. The documented runtime allows up to 16 concurrent agents and up to 1,000 agents per run.

The failure was equally instructive. Both workflows exhausted the shared Claude
session budget after research and verification. Their result objects still named
the promised output files, but no strategy paths, designs, synthesis, or files
were actually produced. A Fusion workflow must therefore treat a provider
limit as a terminal stage failure, persist the failed task graph, and verify
required artifacts before returning `success`.

The official [Claude Agent SDK workshop](https://github.com/anthropics/agent-sdk-workshop/blob/main/01-guided-demo/subagents.py)
shows the lower-level pattern: each subagent has a focused prompt, tool
allowlist, and model, while only its distilled result returns to the coordinator.
The SDK also exposes hooks for deterministic checks and session forking for
branching a conversation. Fusion should use those primitives where available,
while keeping its provider-neutral receipt and worktree boundary.

## Open-source orchestration worth borrowing from

| Project | Good idea to borrow | Fit for Fusion |
| --- | --- | --- |
| [carloluisito/orchestra](https://github.com/carloluisito/orchestra) | DAG dependencies, minimal per-task context, parallelism limits, retries, evidence verification, and `.orchestra` run state | Highest-value design reference for the next `fusion workflow` command. |
| [Kilbex/Vigla](https://github.com/Kilbex/Vigla) | Cross-vendor workers in isolated worktrees, typed event streams, supervisor verdicts, and whole-mission revert | Borrow the receipt/event schema and acceptance gate. Keep Fusion's CLI-first shape. |
| [wshobson/agents](https://github.com/wshobson/agents) | One source of role prompts translated to Claude Code, Codex, Cursor, OpenCode, and other harnesses | Borrow the role-pack format and progressive disclosure. Do not copy its large catalog into the core. |
| [mco-org/squad](https://github.com/mco-org/squad) | SQLite coordination with explicit worker roles and durable messages | Useful for a local coordinator queue when a run outlives one process. |
| [ruvnet/ruflo](https://github.com/ruvnet/claude-flow) | Large Claude/Codex meta-harness with memory, plugins, routing, and swarm concepts | Study its boundaries, but avoid importing the full surface area or background autonomy. |
| [General Action Emdash](https://emdash.com/docs) | Provider-neutral parallel worktrees, task history, automations, and diff review | Product/UI reference if Fusion later gets a local operations console. |

The goal is not to clone a swarm framework. The useful common pattern is a
durable task graph with small context packets, explicit concurrency, typed
events, retry and quota states, isolated writers, and a final acceptance gate.

## Research that informs the design

| Source | Finding | What it means for coding agents |
| --- | --- | --- |
| [Mixture-of-Agents](https://arxiv.org/abs/2406.04692) and [reference implementation](https://github.com/togethercomputer/MoA) | Layered agents can improve answer quality by giving later agents earlier outputs | Useful for proposal and review workflows. It is expensive and transcript-heavy for code editing, so Fusion uses narrow briefs and a single writer. |
| [RouteLLM](https://github.com/lm-sys/RouteLLM) | A learned router can trade quality for cost between strong and weak models | A future policy layer can choose the lead/sidekick pair from receipts; an initial prompt-only router is too brittle for changing task difficulty. |
| [RouterBench](https://github.com/withmartian/routerbench) | Routing needs a common evaluation set with quality and cost data | Record task outcome, latency, tokens, and spend in `.fusion` so the routing policy can be evaluated on real work. |
| [LLMRouterBench](https://github.com/ynulihao/LLMRouterBench) | Unified evaluation finds model complementarity, recall failures, diminishing returns, and weak gains from careless ensemble growth | Curate a small roster of models and measure task-level outcomes instead of adding every available model. |
| [RouteMoA](https://github.com/Jize-W/RouteMoA) | Dynamic routing can reduce the dense cost and latency of a full mixture-of-agents graph | Supports using routing at task boundaries and compaction boundaries rather than calling every model on every turn. |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) | Open coding-agent infrastructure and evaluation make agent/tool behavior observable | Use its benchmarks and task traces as a future external eval target. |
| [agent-watch](https://github.com/soul-sol/agent-watch) | Process exit, terminal JSONL events, and stall detection should be treated separately | Fusion stores worker stdout/stderr and classifies timeout, worker error, and successful completion independently. |

## High-signal posts and discussions

- [Artificial Analysis: Devin Fusion benchmark thread](https://x.com/artificialanlys/status/2098504936984293447?s=46&t=6-cVizCt6wEDkRXUxmV5dA)
- [Cognition: Fusion announcement and benchmark results](https://cognition.com/blog/local-fusion)
- [Lawrence W. Zen: Claude as lead, Codex as subagent](https://x.com/LawrenceW_Zen/status/2035949835124351009)
- [A practical Claude-to-Codex subagent series](https://github.com/dwgx/claude-codex-subagent)
- [Codex multi-agents versus Claude Code agent teams](https://x.com/akihiro_genai/status/2026137417179365828)
- [Git worktrees for multiple Claude/Codex sessions](https://x.com/chenchengpro/status/2032411474703053012)
- [ACP session binding across Claude Code, Codex, and OpenCode](https://x.com/ichiaimarketer/status/2038146648627716195)
- [Claude Code Codex plugin patterns: review, adversarial review, rescue](https://x.com/reach_vb/status/2039251986357338257)
- [Codex discussion: queue/worker orchestration and isolated worktrees](https://github.com/openai/codex/discussions/3898)

## Decisions in this prototype

Fusion starts with Claude as the lead because it is already the user-facing orchestrator in the local workflow. Codex is the default sidekick because `codex exec --json` gives us a stable machine-readable worker boundary and a resumable thread id. Both directions are supported: `fusion lead --agent codex` wires the same MCP server into Codex, allowing Codex to delegate to Claude.

The Ultra pipeline can force every stage through Claude or Codex with
`--harness claude|codex`. Codex stages use separate read-only and
workspace-write routes, while Claude stages can use ORC's live free/best model
selectors. This keeps the harness boundary symmetric even though ORC is a
Claude-compatible OpenRouter launcher.

The handoff contract is deliberately small: task, role, workspace, success criteria, constraints, status, summary, changed paths, tests, blockers, and artifacts. The lead does not receive the sidekick’s full transcript by default. Raw stdout and stderr stay on disk for inspection and cost accounting.

Writes are serialized per workspace with `.fusion/workspace-writer.lock`. Parallel read-only work is safe; parallel writes require separate Git worktrees and a later merge or patch application step. This follows the same constraint surfaced in the Codex orchestration discussion and the worktree research above.

The persisted `fusion workflow` engine is now the orchestration layer for that
graph: it validates a declarative DAG, expands mapped fan-out nodes, bounds
parallel workers, persists node receipts, pauses on quota, resumes accepted
work, and refuses success when required artifacts are absent. The next useful
increment is an outcome-based router trained from this ledger: classify a task
after initial exploration, choose the sidekick model from observed cost and
repair rate, and promote work back to the lead when tests fail, the diff grows
beyond scope, or the lead’s acceptance checks disagree with the worker’s
report.

The repository now also has a bounded UltraCode-style pipeline. It keeps the
useful stage boundaries from [UltraCode](https://github.com/diepquynh/ultracode)
while making the expensive fan-out explicit: stage count is capped, each
stage gets a fresh context, handoffs live in `.fusion/ultra/`, and ORC routes
can select the current free or strongest tool-capable model with a per-call
budget. The pipeline is opt-in because a multi-stage workflow can spend more
tokens than a direct lead/sidekick run.

Each worker call also emits a metadata-only `fusion.trace.v1` span. The ledger
is intentionally local and provider-neutral: it can aggregate token usage and
latency now, and later attach ORC price receipts or provider billing data
without changing the orchestration contract. This is the data needed to tune
stage count, route choice, retry policy, and repair rate from real repository
work instead of benchmark guesses.

## Lateral findings from the Saloon assessment

The first real persisted workflow tested the control plane, not model quality.
Fusion expanded the graph correctly, launched three Claude inventory nodes,
then two Codex counterchecks, persisted five quota pauses, and refused to run
the synthesis node. The trace ledger recorded five failed spans, zero tokens,
and zero spend because both providers rejected the turns before generation.
That is a useful production result: the scheduler preserved state and avoided
claiming a deliverable that did not exist. It also exposed the next boundary:
provider admission and outcome evaluation need to be as deliberate as graph
execution.

The mechanism skeleton is:

```text
goal -> graph -> input snapshot -> routed worker -> evidence/artifact
     -> adjudication -> durable receipt -> resume/replay
```

The current implementation is strongest from `graph` through `durable
receipt`. The next work should make the other nodes explicit.

### Ranked adjacent hypotheses

1. **Make an agent node behave like a hermetic build action.** Bazel's
   [hermeticity model](https://docs.bazel.build/versions/main/hermeticity.html)
   and [remote cache](https://bazel.build/versions/7.1.0/remote/caching?hl=en)
   connect naturally to Fusion: hash the workflow spec, repository/input
   snapshot, prompt packet, route, model, and tool policy, then reuse only a
   successful receipt with the same key. The established part is content
   addressed build reuse; the inferred part is applying it to agent evidence.
   Test this with a no-op Saloon rerun and prove that unchanged accepted nodes
   are not dispatched while changed evidence invalidates only downstream work.

   **Open in #9.** Implemented as `WorkflowRunner._invalidate_stale_receipts()`:
   each accepted node's digest chains its own definition (task, role, agent,
   route, required files, acceptance) with its dependencies' digests, so
   invalidation cascades downstream automatically. One deliberate deviation
   from the hypothesis above: the resolved **model** is recorded on the
   receipt for provenance but excluded from the digest itself.
   `orc-free`/`orc-best` are designed to re-resolve to a different model as
   the catalog shifts; hashing that in would invalidate a cached node on
   every catalog reshuffle even though nothing about the task changed,
   making resume useless for exactly the routes most likely to use it.
   Verified end to end through the real `fusion` CLI (not just unit tests):
   a no-op resume redispatched nothing, and resuming with one edited node's
   task text redispatched only that node and its downstream dependent,
   confirmed against the real subprocess call log and the `node.stale`
   events in `.fusion/workflows/<id>/events.jsonl`.

2. **Use MapReduce-style straggler handling for expensive workers.** The
   [MapReduce paper](https://research.google.com/archive/mapreduce-osdi04.pdf)
   describes backup tasks for slow workers. Fusion can launch one hedge after
   a latency threshold, using a different provider or model, rather than
   blindly retrying every failed node. Accept the first result that passes the
   evidence gate and record the losing result for comparison. Test with a fake
   delayed worker, a strict hedge budget, and cancellation or late-result
   handling.

3. **Turn ORC routing into admission control and a circuit breaker.** The
   current free/best selector can take the first tool-capable model even when
   ORC reports it as `UNTESTED`. The Saloon run also showed that known Claude
   session limits and Codex usage limits should stop a wave before dispatch,
   rather than produce one failure per fan-out item. A preflight should check
   executable/auth/provider health, model fit, and quota state; the scheduler
   should mark a lane `healthy`, `degraded`, `cooldown`, or `blocked`.
   [Temporal's durable execution model](https://docs.temporal.io/) and
   [retry policies](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx)
   are the closest adjacent design. Test by injecting a provider limit and
   verifying zero worker dispatches, one admission event, and a resumable
   manifest.

   **Shipped in #6.** `select_orc_model` now requires a model that passed
   `orc probe --fit` unless a route/task/node explicitly sets
   `allow_untested: true`. `WorkflowRunner` preflights each agent lane before
   the first dispatch (executable on PATH, and — on a fresh run, not a
   resume — the most recent trace for that agent within 15 minutes) and
   reactively cools a lane down the moment any node in the run hits a quota
   failure, so the rest of that wave stops dispatching into a lane already
   known dead. Landed with three lane states (`healthy`, `blocked`,
   `cooldown`) rather than the four proposed above — `degraded` had no
   concrete trigger condition once the other three covered the observed
   failure modes, so it was left out rather than added speculatively.
   Verified live: a real fan-out that hit a quota failure mid-wave correctly
   cooled that lane, and a brand-new second workflow in the same workspace
   made zero dispatch attempts because preflight saw the still-fresh trace.

4. **Make synthesis a provenance query, not a prose merge.** Saloon's output
   is evidence-backed, so every accepted claim should point to its source
   artifact, worker activity, model/route, timestamp, and verification result.
   The [W3C PROV primer](https://www.w3.org/TR/prov-primer/) provides the
   useful cross-domain vocabulary: entities, activities, agents, and
   derivations. Test by requiring claim coverage and contradiction resolution
   before synthesis can pass acceptance.

5. **Choose the worker set by information gain.** Three identical inventory
   prompts spend more than two role-diverse prompts when they discover the
   same facts. Borrow ensemble diversity and active-learning stop rules:
   dispatch an initial small sample, measure unique findings and disagreement,
   then buy another worker only when expected evidence gain exceeds its cost.
   Test on a fixed Saloon question set and compare unique accepted facts per
   dollar, disagreement detection, and wall time.

6. **Use mutation tests for reviewer quality.** A reviewer prompt saying
   “check this” is weaker than a verifier that must catch a seeded false claim,
   removed citation, or stale artifact. Generate controlled mutations of a
   worker receipt, run the countercheck, and score detection. This transfers
   mutation-testing and N-version validation ideas into agent evaluation.

### Build order

Status as of this writing — 1, 2, and 4 merged; 3 open for review; 5 partially
done:

1. ✅ **Merged in #6.** Route admission/preflight. Refuse `UNTESTED` ORC
   models unless a workflow explicitly opts in, and collapse a known
   provider outage into one blocked lane before fan-out.
2. ✅ **Merged in #7.** `fusion workflow report RUN_ID`, combining node
   waves, blockers, latency, token/cost totals, artifact acceptance, and the
   next resume command. The Saloon assessment is now one command instead of
   manually joining `status`, `trace`, `usage`, and events.
3. 🔶 **Open in #9.** Workflow/spec/input digests and route/model versions
   on manifests and node receipts. Resume reuses accepted work only when
   those digests still match; provider session resume is unaffected. See
   the resolution note on hypothesis 1 above for what shipped and the one
   deliberate deviation (model excluded from the digest itself).
4. ✅ **Merged in #4.** The stale fan-in receipt fix. A fan-in node now
   waits until all dependencies reach terminal states before writing its
   blocker receipt.
5. 🔶 **Partially done.** `FUSION_REAL=1 make dogfood-real` ran against live
   Claude and Codex: Claude succeeded end-to-end with real usage recorded;
   Codex hit its actual OpenAI usage limit (external quota, not a harness
   bug — reset time was in the error). That single-worker-per-agent smoke
   is not the same thing as the two-worker real ORC smoke or the six-node
   Saloon replay described below, which are still open, and no deterministic
   evidence/provenance fixture has been built yet. The live pass did already
   pay for itself once: it surfaced a real `model`/cost parsing bug, fixed
   in #10 — `parse_claude_output` assumed a top-level `model`/`model_id`
   field and `usage.cost_usd` that don't exist in real `claude -p
   --output-format json` output (the model is a key under `modelUsage`; cost
   is the top-level `total_cost_usd`), so every real dispatch was recording
   `model: ""` and `$0` spent regardless of actual cost — meaning
   `budget_usd`/`max_budget_usd` were not being enforced for any real
   dispatch. A fixture-only test suite would never have caught that, since
   the fixtures encode the same assumption the parser did; `parse_codex_events`
   makes the identical top-level `model`/`model_id` assumption and hasn't
   been checked against a real successful Codex response yet, since Codex
   has been quota-gated this whole session — flagged rather than patched
   blind.

The test ladder should be: deterministic fake workers; a two-node real ORC
smoke; the six-node Saloon read-only graph; and finally one writer in a
disposable Saloon worktree. Compare task success, artifact validity, evidence
coverage, disagreements caught, wall time, tokens, cost, and repair rate.
Until the real ORC route is exercised successfully, adding more frameworks or
more parallel agents would measure availability failures rather than Fusion
quality.

## Real provider verification: build order items 1-4 closed, one trace bug found

The build order above (route admission, `workflow report`, digests, the
fan-in fix) landed as #4/#6/#7/#9. Before calling it done, each feature was
also dogfooded through the real `fusion` CLI with real subprocesses -- not
just unit tests -- and the resulting `.fusion/traces.jsonl` and
`.fusion/workflows/<id>/events.jsonl` were read back to confirm the claims:

- Fail-safe model selection: a live run against a fake `orc-free` catalog
  with one FIT model ranked below an UNTESTED one picked the FIT model, not
  the higher-ranked untested one.
- Lane admission: a fan-out that hit a real quota failure mid-wave recorded
  `lane.status: cooldown`; a brand-new second workflow in the same workspace
  made zero dispatch attempts because preflight saw the still-fresh trace; an
  explicit resume correctly bypassed that same trace-history block.
- Digests: a no-op resume redispatched nothing; resuming with one edited
  node's task text redispatched only that node and its downstream dependent,
  leaving an unrelated sibling node cached at its original digest -- all
  confirmed against the real subprocess call log, not just the reported
  status.

Then `FUSION_REAL=1 make dogfood-real` was run against the live `claude` and
`codex` CLIs (real quota, real cost, run with explicit authorization). Claude
succeeded end-to-end with real usage recorded; Codex hit its actual OpenAI
usage limit (reset time included in the error), which the harness correctly
classified as a provider gate rather than a broken worker.

That live Claude trace is what surfaced a real bug, found by diffing what
`parse_claude_output` assumes against the actual JSON `claude -p
--output-format json` returns:

- **`model` was always blank.** The parser reads top-level `model`/
  `model_id`; real output has neither. The model name is a key in a
  `modelUsage` dict instead (`{"claude-sonnet-5": {...}}`).
- **Cost was silently dropped.** Real cost is `total_cost_usd` at the *top
  level* of the response, not inside `usage`. `_result_cost()` and every
  `budget_usd`/`max_budget_usd` check only ever look inside `usage`, so every
  real Claude Code dispatch was reporting `$0` spent regardless of actual
  cost -- meaning budget caps were not being enforced at all for real
  dispatches, only for test fixtures that happened to set `usage.cost_usd`
  directly. Fixed in the same pass the bug was found, with a regression test
  built from the actual captured JSON shape (see `fusion_core.py:
  parse_claude_output`).

This is a useful general lesson for this harness: a fixture that encodes an
*assumed* schema will happily stay green forever even if the real CLI's
schema drifts or was never quite what the parser assumed, since nothing ever
diffs the fixture against a live response.

**Update, Codex quota reset:** checked the same `model`/`model_id`
assumption in `parse_codex_events` against real `codex exec --json` output
(inspected the full event vocabulary across a multi-step run --
`thread.started`, `turn.started`, `item.completed` for both
`agent_message` and `command_execution` items, `turn.completed`). Unlike
Claude, **no event carries a model field at all** in this Codex CLI
version -- there's no salvageable key to fall back to the way `modelUsage`
saved the Claude case. Concluded this needs no code change:
`dispatch()` already falls back to the *configured* model
(`metadata["model"]`) when parsing reports none, which is the most honest
answer available when the provider genuinely doesn't report one. A real
gap the same live check *did* find: real `turn.completed` usage reports
`cache_write_input_tokens`, which didn't match any field
`normalized_usage()` recognized (silently dropped, no crash, but codex
cache-write spend was invisible in aggregation). Fixed by mapping it to
`cache_creation_input_tokens`, the same normalized name Claude's
`cache_creation` sub-object maps to. Verified against a real `fusion
delegate --agent codex` dispatch end to end, including through `fusion
usage`'s aggregation. Real Codex/ChatGPT-plan usage also never reports
`cost_usd`/`cost` at all -- confirmed this is a genuine provider-side gap,
not a parsing miss, so codex spend stays at `$0` in aggregation by design,
not by bug.

Also visible in the real Claude response: `permission_denials` (an array,
empty in every run so far) and `subagent_stats` (not surfaced anywhere;
left for later). A tool denial that isn't verbally mentioned in the model's
own summary text would otherwise be invisible to the harness --
`permission_denials` catches that structurally instead of relying on the
model to self-report it.

Seven separate live attempts to trigger a populated `permission_denials`
entry all came back with an empty array:

1. Plan mode asked to delete a file -- declined verbally, never attempted
   the tool call.
2. `--allowedTools Read` asked to run Bash -- ran anyway (allowlist did not
   restrict it in this scenario).
3. `--disallowedTools Bash` asked to run Bash -- and 4. the same with
   `--permission-prompts none` added -- the model reported "I don't have a
   Bash tool," meaning the tool was filtered out of its visible list before
   it could attempt (and be denied) the call.
5. `--permission-mode manual --permission-prompts none` (prompts
   auto-deny) asked to run a benign `echo` -- ran anyway; "manual" mode did
   not require approval for this command.
6. The same mode asked to run `rm -rf` on a nonexistent path -- the model
   refused on its own judgment before attempting any tool call, so nothing
   reached the permission layer to be denied.
7. `--permission-mode plan --permission-prompts none`, explicitly instructed
   to call the Write tool immediately with no explanation -- found an
   allowed side-channel instead (wrote a plan file to `~/.claude/plans/`,
   outside the working directory, and asked for confirmation before writing
   the actual requested file).

Across all seven, `permission_denials` never populated: the model either
self-censors before attempting a disallowed action (a judgment-level
refusal, not a permission-system-level one), finds an allowed alternative
path, or the action turns out to be permitted after all. None of the
headless, flag-only scenarios this harness can construct reach the
"model attempts a visible tool, permission layer rejects it" path this field
is presumably for -- a real populated example likely needs genuine
interactive rejection, or a fine-grained pattern rule (e.g. `Bash(rm *)`
denied while `Bash(*)` is otherwise allowed) that a headless smoke script
can't trivially construct. (Note: `--permission-prompt-tool`, floated above
as a possible headless path, turned out not to be an actual CLI flag on
this Claude Code version when checked against `claude --help` -- it's only
referenced as an SDK-level concept inside `--permission-prompts`' help text.)

Not knowing the populated shape doesn't mean the surfacing has to wait,
though -- `parse_claude_output` extracts it defensively instead: tries a
handful of plausible field names per entry (`tool_name`/`name`/`tool`/
`display_name`/`action`, plus `reason`/`message`), and falls back to
dumping the raw entry as JSON when none match, so an unrecognized shape
still surfaces instead of silently vanishing or crashing. That's a
different risk profile than the `total_cost_usd` bug: that bug produced a
plausible-looking but *wrong* value ($0) with nothing to indicate anything
was off; this either extracts correctly or visibly shows raw data, never a
confident wrong answer. The same pass also fixed `parse_agy_output`, which
already has a *verified* `denied_actions` shape (PR #8) but was only
surfacing it when a denial was the entire outcome -- a denial alongside an
otherwise-successful turn was silently dropped, which is now fixed too.

## How to try it

See the README's [Fusion](README.md#fusion-claude-lead--codex-sidekick)
section for the current command set (`lead`/`delegate`/`ultra`/`workflow`,
plus `agy` as a third worker) — kept in one place instead of duplicated here
to avoid the two drifting apart. Minimal bootstrap:

```sh
./install.sh
fusion doctor
```

Use `.fusion.json` in a repository to pin commands, models, permission modes, timeouts, and the default lead. Do not put API keys in that file.
