"""Multiline handoff regression from the first Oasis review, 2026-09-23.

The embedded answer is the actual saved public answer from run
20260923-194019-3bb1ba53. Tests require neither that workspace nor a model call.
"""
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core


class HandoffBlocksTest(unittest.TestCase):
    def test_actual_oasis_review_keeps_summary_and_verification_observations(self):
        self.assertEqual(hashlib.sha256(ACTUAL_REVIEW_ANSWER.encode()).hexdigest(), ACTUAL_REVIEW_SHA256)
        parsed = core.parse_handoff(ACTUAL_REVIEW_ANSWER)
        self.assertEqual(parsed["reported_status"], "success")
        self.assertIn("**Atomic writes:**", parsed["summary"])
        self.assertIn("Proposed next Oasis increment (002)", parsed["summary"])
        self.assertEqual(parsed["changed"], [])
        self.assertEqual(len(parsed["tests"]), 15)
        self.assertTrue(any("Ran a read-only probe" in entry for entry in parsed["tests"]))
        self.assertTrue(any("Inspecting an untouched player" in entry for entry in parsed["tests"]))
        self.assertTrue(any("denied" in entry for entry in parsed["blockers"]))

    def test_multiline_fields_and_standalone_none(self):
        parsed = core.parse_handoff("""STATUS:
success
SUMMARY:
The first paragraph.

A second paragraph with the actual finding.
CHANGED:
- src/a.py
- src/b.py
TESTS:
- pytest -q passed
- ruff check passed
BLOCKERS:
none
""")
        self.assertEqual(parsed["reported_status"], "success")
        self.assertEqual(parsed["summary"], "The first paragraph.\n\nA second paragraph with the actual finding.")
        self.assertEqual(parsed["changed"], ["src/a.py", "src/b.py"])
        self.assertEqual(parsed["tests"], ["pytest -q passed", "ruff check passed"])
        self.assertEqual(parsed["blockers"], [])

    def test_none_does_not_erase_continuations_or_ambiguous_explanations(self):
        for blocker in (
            "none\n- The migration remains unreviewed",
            "none.\nProduction verification has not happened",
            "none. The deployment requires external approval",
            "none. However the suite still fails",
            "none. The MCP request was denied by the harness",
        ):
            with self.subTest(blocker=blocker):
                self.assertTrue(core.parse_handoff("BLOCKERS: " + blocker)["blockers"])
        self.assertEqual(core.parse_handoff("BLOCKERS:\n- none\n- permission denied")["blockers"], ["permission denied"])

    def test_explained_recovery_is_not_inferred_from_blocker_prose(self):
        # Intentional fail-closed change: historical normalization discarded
        # everything after "None.". A parser cannot establish recovery or
        # decide whether a later clause changes the operational outcome.
        explanation = "None. The `ls` alias points to `eza`, which isn't installed, so I used `/bin/ls`."
        self.assertEqual(core.parse_handoff("BLOCKERS: " + explanation)["blockers"], [explanation])
        unresolved = explanation + " The release still requires an independent check."
        self.assertEqual(core.parse_handoff("BLOCKERS: " + unresolved)["blockers"], [unresolved])

    def test_fenced_handoff_and_separate_acceptance_contract(self):
        parsed = core.parse_handoff("""```text
STATUS: success
SUMMARY:
Planned the work.
TESTS:
- inspected source
BLOCKERS: none
```

```acceptance-contract
{"required_files": ["hello.py"], "verification": ["pytest -q"]}
```
""")
        self.assertEqual(parsed["reported_status"], "success")
        self.assertEqual(parsed["tests"], ["inspected source"])
        self.assertEqual(parsed["blockers"], [])
        plain = core.parse_handoff("STATUS: success\nSUMMARY: Planned\nBLOCKERS: none\n\n```acceptance-contract\n{}\n```")
        self.assertEqual(plain["blockers"], [])

    def test_fenced_examples_cannot_override_the_real_handoff(self):
        parsed = core.parse_handoff("""STATUS: success
SUMMARY: Checked this literal example:
```python
STATUS: error
BLOCKERS: fabricated example
```
TESTS: inspected source
BLOCKERS: none
""")
        self.assertEqual(parsed["reported_status"], "success")
        self.assertIn("BLOCKERS: fabricated example", parsed["summary"])
        self.assertEqual(parsed["blockers"], [])

    def test_parsing_a_real_review_does_not_clear_provider_permission_denials(self):
        envelope = json.dumps({"result": ACTUAL_REVIEW_ANSWER, "is_error": False,
                               "permission_denials": [{"tool_name": "mcp__tenet__recipe_status",
                                                       "tool_input": {"run_id": "oasis-district-001"}}]})
        _, answer, _, _, _, denials = core.parse_claude_output(envelope)
        self.assertTrue(core.parse_handoff(answer)["tests"])
        self.assertTrue(denials)
        self.assertEqual(core.failure_class({"status": "error", "blockers": denials}), "permission_denied")

ACTUAL_REVIEW_ANSWER = 'STATUS: success\n\nSUMMARY: I reviewed Fusion workflow `oasis-district-001-review` (node `review`, attempt 2). This attempt fixes the earlier failure: run `20260923-193525-4d6c7b7a` stopped after 1.4s because the worker was started without a prompt (`Error: Input must be provided either through stdin or as a prompt argument when using --print`), so its summary and tests were empty.\n- **MCP result:** I called TENET `recipe_status` for `oasis-district-001`, and the harness denied it (plan mode, no way to approve). That means I have no MCP result of my own. I read the saved result file directly instead: `.tenet/recipe-runs/oasis-district-001/result.json` shows `status: "succeeded"`, `finished_at` 2026-09-24T01:32:24Z, and inner `success: true`. Only a finished run writes that file, so the run is terminal, but that comes from the file and not from the MCP tool.\n- **Acceptance receipts:** Two coordinator acceptance receipts (`check-1-da1a95773368`, `check-1-0eeb42f5149b`) both show `status: passed` and `exit_code: 0`, and their logs end with "Ran 15 tests … OK". A success status in these files doesn\'t prove the behavior on its own, so I read the source and probed it myself.\n- **Source reviewed against the frozen spec:** `specs/001-persistent-district.md`, `oasis_runtime/{runtime,engine,validation,__main__,errors}.py` and `runtime/README.md`.\n  - **Idempotency:** the receipt lookup and the canonical payload comparison both happen inside a `BEGIN IMMEDIATE` transaction.\n  - **Atomic writes:** each world update, event and receipt is written in one transaction, and any exception rolls the whole transaction back. That covers init, activate and replay.\n  - **Replay:** it re-runs the authoritative inputs through the same `transition` the live path uses. It saves each historical receipt as each event is replayed, checks sequence numbers, behavior versions, the state projection and the digest, and rejects a replay ID that collides with an imported ID before the world is created.\n  - **Activation:** the whole behavior is validated before the version-increase check.\n\nI found nothing that blocks release. Non-blocking observations:\n1. A snapshot with a second `init` event partway through is rejected with `world_exists` instead of `invalid_snapshot`. It is still rejected atomically.\n2. `worlds.state_json` is a cached projection that live commands trust. It is only checked against the events during replay, not when the database is opened.\n3. There is no upper limit on how many events a snapshot can hold, so a very large import can pin the writer lock.\n4. I did not exercise contention between processes myself. That relies on `BEGIN IMMEDIATE`, a 10s busy timeout and the `database_busy` error.\n\nProposed next Oasis increment (002): add a `verify` op or startup check that replays the stored events and compares the result with `worlds.state_json` and its digest. Also map any bad event in a snapshot to `invalid_snapshot`, set a documented event-count limit on snapshots, and add a black-box test with several processes sending the same `command_id` at once.\n\nCHANGED: none\n\nTESTS:\n- `mcp__tenet__recipe_status(run_id=oasis-district-001)`: denied by the harness (plan mode), not performed.\n- Read `.tenet/recipe-runs/oasis-district-001/result.json` (succeeded), `.tenet/recipe-runs/oasis-district-001-review/result.json` (failed, empty handoff) and the stderr of the earlier review attempt.\n- Read both acceptance `receipt.json` files and the tail of their `stderr.log`: both passed, 15/15 OK.\n- Did not run `python3 -m unittest`, because its `TemporaryDirectory` is created inside the workspace and this node must not write there.\n- Ran a read-only probe, `python3 -B` against `Runtime(":memory:")` with `content/harbor.json` and behavior v1/v2. All of these came out as expected:\n  - An identical retry returns the original result, including after a behavior change.\n  - Reusing a `command_id` with a different payload gives `command_conflict`.\n  - A behavior with a boolean parameter is rejected, and its `command_id` stays unused.\n  - Events are numbered 1..4 with no gaps.\n  - A replay ID that collides with an imported ID is rejected; so are a tampered state and a tampered amount (`invalid_snapshot`), and the world stays absent after each rejection.\n  - A clean replay gives the same digest and a byte-identical export.\n  - Retrying an old command on the replayed world returns its original response, and a different payload under an old ID conflicts.\n  - Retrying the replay command returns the same result.\n  - The same future command gives the same result on the original world and the replayed one.\n  - Inspecting an untouched player leaves the digest unchanged.\n\nBLOCKERS: none. The TENET MCP `recipe_status` call was denied by this session\'s permission mode, so the recipe status above comes from the saved `result.json` on disk, not from an MCP response.\n'
ACTUAL_REVIEW_SHA256 = '8f27bf72818b447fb939cf412283eec66ac7df97563dfc51758bda8c6b10356c'
