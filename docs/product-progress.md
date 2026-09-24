# Product checkpoint in the control room

The optional `ORC_HOME/product-progress.json` supplies a coordinator-authored
project checkpoint above the cross-workspace run history. It is a read-only
presentation input, not a scheduler, training corpus, state replication protocol
or inferred acceptance result. The page labels its source and timestamp. Saved
run outcomes and checks remain separate and authoritative for their own scope.

Use schema `fusion.product-progress.v1`, timezone-bearing `updated_at`, and
`projects` (1–12 entries). Each project has text fields `id`, `name`, `phase`,
`working`, `current`, `next`, and `limits`. Optional `evidence` entries contain
`workspace_id`, `run_id` and `label`; they open the existing workspace-scoped
run view. Referenced workspaces must be registered in this control room.

The operator may produce the snapshot from their project/state service or write
it explicitly. Updating a snapshot does not change workflow receipts, approve a
label, dispatch work or prove the claimed milestone. Keep checkpoint descriptions
supported by their linked evidence and update them when project status changes.
Missing input hides the optional section; damaged or unknown-source input is
shown as unavailable. The reader does not execute any supplied content or fetch
external URLs. No TENET-specific plan or workspace paths are embedded in ORC.

An optional `milestones` array (up to eight entries) gives each milestone a `name`,
`exit` condition and `status`: `planned`, `in_progress`, or `qualified`. The room
shows these in a collapsed roadmap. These are explicit coordinator checkpoints,
not statuses inferred from successful execution or model predictions.
