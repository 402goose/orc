# Admitted label drafts

In a workspace bound to TENET, `suggest-labels` uses the existing admitted
workflow executor. It admits one read-only Codex node, one attempt, Laya off,
no native delegation, and an operator-allowed non-ultra model/effort pair.
Labeling mode is single and approval mode is human. Standalone ORC retains its
existing labeling path.

The existing launch endpoint accepts:

```json
{
  "action": "suggest-labels",
  "decision_id": "record-id",
  "agent": "codex",
  "labeling_mode": "single",
  "approval_mode": "human",
  "model": "gpt-6-sol",
  "reasoning_effort": "xhigh"
}
```

Omit both model/effort fields to use the operator default, which must be a
non-ultra pair for label work. Project config is checked against that operator
default; choosing a node override does not silently alter project config.
The provider remains optional for standalone ORC, and a required unavailable
provider never falls back to standalone execution.

## Frozen evidence and authority

The admission ID is `label-` plus the first56 hex characters of the SHA256 of
the exact original decision journal line. Changing the proposed teacher model
or clicking again cannot mint another model run for that source occurrence.
The provider reserves this identity before execution and derives the teacher
packet and prompt itself from verified source references. Original predictions,
applications and prior labels are withheld. Project reference context is empty
for this job; the teacher receives the cited packet only.

Sources distinguish:

- Original decision input.
- Worker result/answer claims, including self-reported tests and process status.
- Coordinator check receipts matching the original workflow, node, worker run
  and attempt, with actual output hashes and bounded previews.

Source files cannot traverse outside their allowed attempt or follow symlinks.
The collector limits files to2,000,000 bytes, combined raw sources to4,000,000
bytes and source count to32. The provider independently validates the packet.
Malformed evidence fails explicitly. Output truncation is marked. Missing
independent checks do not become passing evidence.

A coordinator receipt is an observation of its command, not an external
correctness oracle. These workspace files are mutable; hashing freezes the
observed bytes and does not prove a malicious workspace cannot forge them.

## Completion and recovery

The supervisor first records the terminal executor result with the provider.
For a successful label worker it then parses one strict `label-suggestion`
block against the frozen question schema and source IDs. Draft completion
requires the worker task to belong to this admitted read-only labeling attempt.
The stored suggestion retains the admission request hash, original decision
fingerprint, answer/task/manifest hashes and worker execution evidence.

Under the existing review lock, completion checks for an already persisted
suggestion and returns it. A repeated launch of the same local job retries
completion or returns current status; it does not run another worker. An
explicit authenticated `POST /api/label-draft/finalize` with
`{"run_id":"label-..."}` recovers completion from a successful terminal receipt,
including when the local UI job file was lost. This operation starts no worker.
Missing, claimed, failed or changed-provenance receipts cannot be finalized.
A crash after claim but before terminal persistence remains unknown; there is
no automatic redispatch or generic exactly-once/power-loss guarantee.

The result is always `verified: false` and `approval_mode: human`. Training
exports remain empty until a human approves supported answers. Routing/model/
effort questions without matched alternatives are mechanically converted to
abstentions even if the teacher proposes an answer; that proposal is retained
separately for inspection. A successful chosen pair is not an optimal-pair label.

Automatic scheduling uses the existing garden controls, including the bounded
TENET daily allowance described in [automatic draft limits](automatic-draft-limits.md).
This feature neither approves labels nor trains or promotes a model.

## Checks

Local tests use a benign fixture executable, not native model inference:

```sh
TENET_LABEL_TEST_PROVIDER=/absolute/tenet/dist/commands/admission-provider.js \
  python3 -m unittest discover -s test -p label_drafts_test.py -v
```

The optional integration test launches the real compiled provider and detached
ORC supervisor, verifies one worker invocation across service reconstruction,
and checks that drafts remain excluded from training. It does not measure label
quality or verify that a real model obeys every instruction.
