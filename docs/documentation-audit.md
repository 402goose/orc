# Documentation migration audit

[Start here](../README.md)

This migration preserves the original README's operational material in topic
guides. The ranges below refer to the 1,496-line README before this restructure;
they are provenance for the move, not current line references. Commands and
schemas in the generated references come directly from the runtime definitions.
No runtime behavior was changed.

## Migration inventory

| README lines / section | Destination and facts to preserve |
|---|---|
| 1–33: Introduction/navigation | `README.md`: title, motto, launcher/harness distinction, local orchestration, optional publishing, capability navigation. Correct the blanket locality/dependency claims below. |
| 34–64: Requirements/install | `README.md`; detailed installation in `docs/configuration.md`: operating systems, Python, worker CLIs, Git/GitHub prerequisites, launcher dependencies, optional fzf, `DEST`, installation and first UI launch. |
| 65–87: Launcher setup | `docs/launcher.md`: key discovery, catalog picker, permission modes, saved choices, launch menu, invocation overrides versus saved defaults, profile snapshots. Keep a short README link. |
| 88–111: Guild/pipeline | `README.md`: compact guild glossary and pipeline; `docs/workflows.md`: full roles, dispatch, acceptance, review and opt-in publishing explanation. |
| 112–142: Acceptance | `docs/workflows.md`: self-report versus structural evidence, Git change checks, non-Git behavior, `allow_no_changes`, acceptance contracts, path validation, verification allowlist, before/after checks, vacuity and unsandboxed execution. |
| 143–154: Starting commands | `README.md`: quickstart and model/effort link; detailed examples in `docs/workflows.md` and `docs/truffle.md`. |
| 155–199: Control-room overview | `docs/workflows.md`, control-room subsection: branding/assets, launch/port examples, shared artifacts, workspace/history navigation, Markdown reports, finding actions, launch types, availability/settings, themes and activity behavior. Link learning/truffle details to their pages. |
| 200–210: UI lifecycle/reconnection | `docs/workflows.md`: write consent, preparation, permissions/locks/limits, detached jobs, observing external runs, reconnect credentials, retained edits and no replay of failed actions. |
| 211–242: Laya tabs/review | `docs/learning.md`: six tabs, URL/keyboard navigation, editing persistence, suggestion workers and evidence isolation, inherited settings, human/council approval, provenance, draft exclusion, candidate selection and evaluation distinctions. |
| 243–287: Quality/automatic training | `docs/learning.md`: duplicates/conflicts, source/candidate identity, leakage/ancestry, controls, trigger threshold, unchanged approvals, no daily cap, XP/round displays, cleanup, minimum groups, no promotion, pause/retry/restart behavior and storage. |
| 288–317: Garden/council | `docs/learning.md`: opt-in drafts, backlog selection, one assessment at a time, persistent usage/settings, pause semantics, two-to-four workers, independent sequential assessments, unanimous/available rules, cooldown and approval provenance. |
| 318–323: Snout recovery | `docs/truffle.md`: field guide, recovered hunt states, continue-queue behavior, viewing without launching and published PR links. |
| 324–346: Council view/queues | `docs/learning.md`: public updates/votes, review/evidence/failure queues, reassessment, exclusion/restoration, immutable existing exports/weights and CLI equivalents. |
| 347–399: Publishing | `docs/workflows.md`: Open PR, preview/editing, legacy confirmation, path isolation, publish configuration, off/manual/auto, fetched-base worktrees, dependencies, review snapshots, CLI overrides, retries, hooks, checks, target changes and failure exit status. Link configuration keys from `docs/configuration.md`. |
| 400–427: Runtime permissions | `docs/configuration.md`: restricted/YOLO, global/project precedence, worker-specific flags, AGY native settings, loss of runtime read-only guarantees, Codex Git profile and legacy-config interaction. |
| 428–447: Auto fallback/native workers | `docs/routing.md`: provider-quota fallback, pinned workers, attempt/budget limits, targeted resume, native lanes, Grok permissions and ORC fit requirements. Correct Grok claims below. |
| 448–500: Account lanes/quota | `docs/routing.md`: route `env` expansion/precedence, explicit/inferred account identity, shared cooldowns, ORC lanes, normalized quota, thresholds, pacing, strict comparisons, expiry, pinned selections, observation retention and routing audit. Put configuration examples in `docs/configuration.md` with links. |
| 501–510: UI connection/shortcuts | `docs/workflows.md`: loopback binding, capability URL/token persistence, `--no-open`, ephemeral port, bundled rendering assets and shortcuts. |
| 511–540: Development verification | `README.md` Development summary; full commands in `docs/workflows.md`: Python/dogfood/launcher checks, vitest, external Playwright installation, all listed browser scripts, disposable fixtures and no paid workers. |
| 541–621: Truffle pig | `docs/truffle.md`: entire survey/hunt/queue guide, patches/wilds, references, eight-issue batches, sync/resume rules, grade table, filters, defaults, citations/exclusions, worktrees, publishing, revalidation, recovery, storage and all examples. |
| 622–643: Fusion overview | `README.md` capability map/terminal mode; `docs/workflows.md`: lead/worker contract and artifacts; `docs/routing.md`: session/cache behavior; `docs/telemetry.md`: trace fields. |
| 644–670: MCP | `README.md` chat-client mode; `docs/workflows.md`: connection/usage explanation, durable handles, prompts/resources and read-only discovery. `docs/mcp.md`: generated complete tool reference. Remove unsupported ecosystem claims below. |
| 671–691: Builds | `docs/workflows.md`: interactive Codex default, Claude override, issue URLs, briefs/plans, independent review, planning-only behavior, `--plan-only`, `--execute` and inspection commands. |
| 692–707: Laya setup | `docs/learning.md`: covered decisions, shadow default, setup/probe/list, explicit activation/calibration, deterministic fallback and disable environment variable. Preserve links to `FUSION_DECISIONS.md`. |
| 708–729: Progress/watch | `docs/workflows.md`: stderr progress, JSON compatibility, global display flags, ten-second heartbeats, intake registration, latest-started attachment, viewer-only Ctrl-C and legacy limitations. |
| 730–740: Delegate/Ultra examples | `docs/workflows.md`; keep one bounded invocation in README quickstart. |
| 741–771: Control workspace/configuration | `docs/configuration.md`: storage selection, absolute worker identity, sessions/locks, disposable checkouts, config layering and six controller-overridable keys. `docs/telemetry.md`: reporting against the control store. |
| 772–786: Serialization/worker evidence | `docs/workflows.md`: single-writer locking, separate worktrees, adapter output, structural failures, command observations and unresolved acceptance conditions. |
| 787–837: AGY | `docs/routing.md`: worker selection/session behavior; `docs/configuration.md`: sandbox, permissions, native settings, explicit bypass, readiness, Nix allowance, timeout/budget and lead limitations. |
| 838–872: Ultra | `docs/workflows.md`: bounded stages, fresh contexts/handoffs, serialization, budgets, free/best selectors, fit evidence/override, runtime model resolution, examples and harness override. Link route details to `docs/routing.md`. |
| 873–966: Persisted workflows | `docs/workflows.md`: maps/dependencies/concurrency, retries/quota, snapshot failures, run/status/resume, reports/findings/export, artifacts, scheduling limits, lane preflight, digests/cache reuse and bundled smoke graph. |
| 967–989: Traces/dogfood | `docs/telemetry.md`: local trace schema/content and disabling; `README.md` Development: test/dogfood/provider-smoke commands and exit-code distinction. Preserve architecture link to `FUSION_RESEARCH.md`. |
| 990–999: Gym | `docs/learning.md`: extraction, FAIL_TO_PASS/PASS_TO_PASS, lane comparison, hidden tests, visible-test override and provider usage; link existing gym design notes. |
| 1000–1112: Usage/headroom | `docs/telemetry.md`: preserve all source paths, offline behavior, examples/defaults, timestamps/windows, coordinator thresholds, deduplication, token accounting, cost ambiguity, limits, complete JSON field tables, accounts/windows, freshness, reader functions and daily snapshot semantics. |
| 1113–1172: Remote telemetry | `docs/telemetry.md`: endpoint/default, notice, opt-out/config, exact outbound fields and omissions, cache-hit spans, best-effort send, reporting/authentication, collector and architecture links. README must prominently summarize default transmission and disabling. |
| 1173–1183: Launcher introduction | `README.md` launcher summary; `docs/launcher.md`: account isolation, gateway/model selection and harness routes. |
| 1184–1218: Launcher commands | `docs/launcher.md`: preserve every command, option and purpose; generated `docs/cli.md` covers Fusion, not this shell dispatcher. |
| 1219–1259: Models | `docs/launcher.md`: live prices/free/tools, ranking/ties, bundled quality data, quality table, fit probes/cache, filtering and doctor behavior. |
| 1260–1290: Profiles | `docs/launcher.md`: resolved snapshots, status/env, five-layer precedence, examples and environment overrides. |
| 1291–1309: Project configuration | `docs/launcher.md`: upward `.orc.json` discovery, profile/inline examples and common resolution consumers; link from `docs/configuration.md`. |
| 1310–1328: Stats | `docs/launcher.md`: isolated transcripts/subagents, cached pricing, groupings, JSON and unknown-price notation. Link from usage summary. |
| 1329–1348: Keys/probes | `docs/launcher.md`: environment/key-store precedence, service/path, permissions, missing-key behavior, resolved launch probe, endpoint, cost and skip/standalone controls. |
| 1349–1366: Claude environment | `docs/launcher.md`: every exported variable, context lookup, isolated state and forced CLI settings. |
| 1367–1409: HUD | `docs/launcher.md`: all display fields, context thresholds, per-response pricing, unknown/free values, resume/subagent costs, incremental cache, launch stamp, generation and controls. |
| 1410–1440: Launcher configuration | `docs/launcher.md`: complete JSON example, environment controls, cache behavior and `env`; cross-link `docs/configuration.md`. |
| 1441–1479: Development | `README.md` summary; detailed source/build/test mechanics in `docs/launcher.md`: shared pricing, template generation/hash, quality fetcher/map, fixtures, CI and Python skip policy. |
| 1480–1496: Caveats | `README.md` concise caveats; `docs/launcher.md` pricing/tool caveats; `docs/configuration.md` Grok output fallback. Qualify externally unsupported provider claims below. |

## Added coverage

- README capability map and three execution modes, including direct stdio MCP setup.
- Complete generated CLI reference, including nested commands, `runs` and `mcp-serve`.
- All eight MCP tools and their complete input schemas, including `fusion_decisions`
  and `fusion_status` previously omitted from the overview.
- Outcome lifecycle stages, withdrawal and unmeasured results; headless learning
  ticks/status/scheduling; gym lane priors and links to model/effort evidence.
- Generator and drift tests, with fixed help width independent of terminal size.

## Corrections applied

1. **Requirements/install:** `install.sh` requires `jq` and `curl` even for harness-only installation, as well as `claude`; it copies Fusion modules, UI assets and quality data, not only `orc`. Its fzf requirement is only a warning.
2. **“Standard library only/nothing to pip install”:** limit this to the base harness. `fusion_decision_cli.py:108–113` requires `uv`, creates a Python 3.12 environment, and installs optional Laya/Transformers; `fusion_laya.py` imports additional dependencies.
3. **“Everything runs locally/no hosted service”:** distinguish local orchestration/UI from remote worker providers and default remote telemetry.
4. **Nonzero command exits override success:** false at README 118–120. `fusion_core.py:1189–1220,1879–1896` retains Codex command exits as observations; they do not independently force failure. Preserve the later, more accurate explanation.
5. **Required files merely need to exist:** incomplete. `fusion_workflow.py:1170–1180` also rejects unchanged required files when an artifact baseline exists.
6. **Grok plain output/unknown usage:** stale at README 441–445. Defaults are `streaming-json`; `parse_grok_output()` reads usage, model and nonpartial cost. Plain fallback lacks those structured receipts.
7. **AGY automatic readiness:** sandbox/native command settings alone do not qualify it. `fusion_policy.py:270–273` excludes restricted-mode automatic AGY unless `dangerously_skip_permissions` is explicitly true; `worker_availability()` agrees.
8. **“Never adds a permission bypass in restricted mode”:** too absolute. The explicit AGY setting does exactly that (`fusion_core.py:1562–1567`). Say it never adds the bypass *implicitly*.
9. **Route-only delegate examples:** `fusion delegate --route orc-free …` fails because `--agent` is required. Use `fusion delegate --agent claude --route orc-free …` and likewise for `orc-best`.
10. **Telemetry JSON placement:** use `fusion --json telemetry report`; `fusion telemetry report --json` fails parsing. Usage specifically supports either placement.
11. **Telemetry environment duration:** `export FUSION_TELEMETRY=0` persists for the shell and its children. For one invocation, use `FUSION_TELEMETRY=0 fusion …`.
12. **Telemetry “never blocks”:** sending is synchronous with a three-second network timeout (`send_remote_telemetry()`); failures are swallowed, but dispatch completion can be delayed.
13. **Telemetry status is not effective-send status:** it reports configured remote enablement without applying the environment opt-out; actual sending also requires local telemetry enabled. Remote reports refuse when configured remote enablement is false.
14. **Telemetry payload inventory:** include schema/install ID and trace/span/parent identifiers. Dedicated prompt/output/path fields are omitted, but role/route/model strings are copied directly; avoid claiming arbitrary configured identifiers are scrubbed.
15. **Resume always reuses unchanged specs:** too strong. `_invalidate_stale_receipts()` reruns acceptance validation before reuse. Missing/unchanged artifacts or failing checks can invalidate receipts.
16. **Resolved models never affect digests:** qualify. `_definition_digest()` includes explicit Codex model/effort execution pairs and native-delegation setting when effort is configured.
17. **Cooldown described per agent:** update to account-aware lanes; `_preflight_lanes()` resolves route lane keys and inspects the latest 50 traces. Resume skips historical preflight cooldown, not all later routing constraints.
18. **`orc refresh` refetches quality leaderboard:** false. `fetch_quality()` seeds a missing cache and otherwise warns; it does not fetch a leaderboard. Updating bundled data/reinstalling also does not overwrite an existing user quality cache.
19. **FIT models sort first:** only within earlier quality-score and prompt-price ordering (`orc:556–563`).
20. **Key file permission guarantee:** `umask 077` protects newly created Linux files; overwriting an existing file does not repair its mode. The warning checks world readability, not every group/world permission.
21. **Doctor uses saved model:** say resolved model, including project/profile/override resolution. `ORC_NO_PROBE=1` also bypasses the nested fit-probe branch.
22. **HUD source of truth:** generated `hud.sh` is not the editable source. Preserve `hud.sh.in` plus `pricing.jq`, as confirmed by `build.sh`.

## Unsupported claims removed or qualified

- MCP specification/client-adoption assertions at README 665–670. Source proves durable handles are implemented; it does not prove universal client support or current ecosystem adoption.
- Universal provider claims about extended thinking, caching, stealth-model retention, and current leaderboard/model availability. Preserve verified launcher behavior and identify catalog output as illustrative; code cannot establish those external policies.

Additional qualifications made during implementation:

- Model IDs, quality tables and sample prices are illustrative snapshots. The
  repository cannot attest current provider availability or price ceilings for probes.
- The Linux key file is plaintext; the key is omitted from JSON configuration.
- Launcher exports use resolved model choices, including project/profile/overrides.
- Context length is exported when found in the catalog; the old claim about an
  external CLI's universal unknown-model default was removed as unsupported.
- HUD costs are computed from catalog data; the repository cannot attest an
  external CLI's current billing calculation or a provider's actual bill.

## Source checkpoints

- `install.sh`: dependency checks, destinations and copied assets.
- `fusion_core.py`: `DEFAULTS`, `build_parser`, `tool_definitions`, adapters,
  `record_outcome`, control-workspace configuration and telemetry serialization.
- `fusion_workflow.py`: `_gate`, `_preflight_lanes`, `_definition_digest`,
  `_invalidate_stale_receipts` and acceptance contracts.
- `fusion_policy.py`: automatic eligibility, route/account quota and gym priors.
- `fusion_learn_cli.py`, `fusion_gym.py`, `fusion_decision_cli.py`: learning,
  benchmark and optional classifier setup entrypoints.
- `orc`, `pricing.jq`, `hud.sh.in`, `build.sh`: launcher resolution, key storage,
  catalog ranking/cache behavior, cost estimates and generated HUD sources.
- `Makefile`, `test/run_python.py`, `.github/workflows/ci.yml`: verification commands.

## Validation

- `python3 scripts/gen_docs.py` and `python3 scripts/gen_docs.py --check`: passed.
- Documentation unit tests: 3 passed; missing/stale checks return nonzero without
  changing files. A `COLUMNS=40` check also passed.
- `make test dogfood`: passed.
- `./test/run.sh`: all 70 launcher/HUD tests passed.
- `make test-ui`: all 36 unit tests passed.
- All ten `test/*_browser.cjs` scripts passed with the external Playwright tools.
- `go test ./... -v` in `telemetry/`: passed, including both integration tests
  against a disposable local Postgres database.
- Local link/anchor and fence checks passed; all 112 Fusion shell examples parsed
  through `fusion_core.build_parser()` without launching workers.
- `git diff --check`: passed. Runtime sources and existing tests are unchanged.
