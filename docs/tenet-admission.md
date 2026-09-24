# Optional local TENET admission

This downstream integration uses the existing ORC room and workflow executor.
TENET owns admission, context snapshots and terminal observations. It never
spawns, retries or supervises workers. Standalone ORC remains the default.

The operator sets `FUSION_ADMISSION_PROVIDER` when starting the room:

```json
{
  "command": ["/absolute/node", "/absolute/tenet/dist/commands/admission-provider.js", "--policy", "/operator/policy.json"],
  "workspaces": ["/absolute/project"]
}
```

Only these workspace paths require the provider. Adding a workspace through the
UI does not grant it a binding. Malformed configuration and unavailable required
providers fail closed. The operator policy and admission state must be outside
every admitted worker workspace. Do not put this setting in `.fusion.json`.

The initial command class is a bounded explicit Codex workflow. New run supplies
a one-worker task, or an authored JSON graph. It requires restricted execution,
approval `never`, `git_write: false`, publishing off, one attempt per node, an
operator-selected executable/model and finite worker limits. Native provider
costs remain unknown: the reported-spend budget is not a billing cap. GitHub
inventory-only sync remains available. Other model/training/resume/publication
actions are refused until they have a supported admission contract.

An admitted run may explicitly select Laya `off` (the default) or `shadow`.
Shadow requires an operator-owned `observations` entry in that workspace's TENET
policy, for example `"observations": {"python": "/operator/laya-venv/bin/python"}`.
The launcher and its resolved target must be outside every admitted workspace;
the launcher path is preserved so a virtualenv retains its dependencies. This
records operator trust, not binary attestation or successful checkpoint loading.
Existing policies without observations continue to support off mode.

The provider derives frozen CPU/cached-model settings from that policy. Callers
may select only off/shadow; they cannot supply Python, model path, calibration,
automatic actions or `active` mode. The supervisor pins both observation mode
and runtime from verified admitted configuration, overriding ambient environment
and mutable workspace/job settings. Laya remains offline and does not set up or
download a checkpoint. An invalid or missing operator interpreter refuses before
admission/claim. Once observation begins, missing checkpoint/dependencies or an
inference error records unavailable advice; it does not veto otherwise valid
structural acceptance. Its bounded local inference wait is separate from the
coding worker timeout, not a total-run deadline.

Admission snapshots the task, source commit reference, selected local context,
effective workflow/configuration and policy. Context is appended as reference
data, not a permission grant. A stable `request_id` is required by `/api/launch`;
the browser retains it across a failed submission. Duplicate IDs cannot reserve
or claim a second execution.

Each frozen node also receives a provider-derived `decision_context` containing
its original authored task. Caller overrides are refused. This keeps full frozen
references in the worker prompt without automatically placing them in the small
classifier input. Long tasks or summaries can still be truncated; that fact is
recorded and prevents label eligibility rather than being hidden.

Shadow observations belong to the engineering workflow in the selected
workspace's `.fusion/decisions/events.jsonl`, not to its application/simulation
learning. Native attempt and workflow IDs link the observed model/schema identity
and prediction back to the admission's source/context hashes. An accepted
explicit discovery node normally records post-worker acceptance and recovery
advice. Explicit Codex routing stays fixed; shadow never gains rejection,
rerouting, retry or publishing authority. Decisions are not approved labels or
training examples until separately reviewed.

Before dispatch the supervisor claims once and reconstructs execution from the
provider's verified artifacts. It does not trust mutable project request argv.
`FUSION_CONFIG_SHA256` bypasses project/global configuration inheritance and
checks the consumed configuration bytes. `FUSION_SPEC_SHA256` checks the consumed
workflow bytes. Closing the room does not terminate its existing supervisors.

A crash after claim and before spawn leaves an unresolved claim. A missing
terminal observation means unknown liveness, not permission to restart. The
current provider does not qualify interrupted-worker takeover or resume. Its
terminal observation records executor exit separately from independent checks;
success does not establish artifact acceptance. Existing ORC cancellation remains
best effort and does not establish that all descendants have stopped.

`/api/admission`, job detail and workflow detail expose the same provider reader.
TENET CLI `admission status` and MCP `admission_status` use the operator policy
with the MCP workspace fixed by the server. This local authority is not a shared
team authorization system or a completed migration into TENET's new state layer.
The provider receipts remain authoritative until that cutover is qualified.

The operator-selected coding harness may have separately configured MCP/service
capabilities. This integration records its native sandbox settings; it does not
claim that all harness/service capabilities have been independently confined.
# Label drafts

Bound workspaces can now draft labels through one read-only admitted workflow,
with a frozen attempt-specific evidence packet and human approval required.
See [admitted label drafts](admitted-label-drafts.md) for the protocol and
completion recovery path.
