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

Admission snapshots the task, source commit reference, selected local context,
effective workflow/configuration and policy. Context is appended as reference
data, not a permission grant. A stable `request_id` is required by `/api/launch`;
the browser retains it across a failed submission. Duplicate IDs cannot reserve
or claim a second execution.

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
