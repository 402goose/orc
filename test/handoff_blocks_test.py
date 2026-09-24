"""Synthetic multiline handoff regressions; no private run data or model calls."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core


class HandoffBlocksTest(unittest.TestCase):
    def test_multiline_review_keeps_summary_and_verification_observations(self):
        parsed = core.parse_handoff(REVIEW_ANSWER)
        self.assertEqual(parsed["reported_status"], "success")
        self.assertIn("**Atomic writes:**", parsed["summary"])
        self.assertIn("Proposed next increment", parsed["summary"])
        self.assertEqual(parsed["changed"], [])
        self.assertEqual(len(parsed["tests"]), 15)
        self.assertTrue(any("Ran a read-only probe" in entry for entry in parsed["tests"]))
        self.assertTrue(any("Inspecting an untouched record" in entry for entry in parsed["tests"]))
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

    def test_none_explanation_preserves_failure_signals_not_workaround_prose(self):
        explanation = "None. The `ls` alias points to `eza`, which isn't installed, so I used `/bin/ls`."
        self.assertEqual(core.parse_handoff("BLOCKERS: " + explanation)["blockers"], [])
        unresolved = explanation + " The release still requires an independent check."
        self.assertEqual(core.parse_handoff("BLOCKERS: " + unresolved)["blockers"], [unresolved])

    def test_signoff_after_blank_line_is_not_a_blocker(self):
        for gap in ("\n\n", "\n\n\n", "\n  \n\n"):
            parsed = core.parse_handoff("BLOCKERS: none" + gap + "Let me know if you want anything else!")
            self.assertEqual(parsed["blockers"], [])
        for continuation in ("- permission denied", "  permission denied", "1. permission denied"):
            with self.subTest(continuation=continuation):
                parsed = core.parse_handoff("BLOCKERS: none\n\n" + continuation)
                self.assertEqual(parsed["blockers"], ["permission denied"])

    def test_arbitrary_paragraphs_after_blank_lines_remain_blockers(self):
        for continuation in ("Production verification was denied.",
                             "The database migration remains unreviewed.",
                             "Approval pending.",
                             "Let me know when the release is approved."):
            with self.subTest(continuation=continuation):
                parsed = core.parse_handoff("BLOCKERS: none\n\n" + continuation)
                self.assertEqual(parsed["blockers"], [continuation])
                # A benign paragraph cannot hide a later unresolved statement.
                parsed = core.parse_handoff("BLOCKERS: none\n\nThanks.\n\n" + continuation)
                self.assertEqual(parsed["blockers"], [continuation])

    def test_status_survives_trailing_signoff(self):
        parsed = core.parse_handoff("BLOCKERS: none\nSTATUS: success\n\nDone.")
        self.assertEqual(parsed["reported_status"], "success")
        self.assertEqual(parsed["blockers"], [])

    def test_none_explanation_failure_vocabulary(self):
        for explanation in ("Access denied", "Tests failed", "The task is blocked",
                            "Unable to check", "I can't run it", "I cannot run it",
                            "I could not verify", "An error occurred", "The request timed out",
                            "Approval is still pending", "It requires approval",
                            "Verification has not happened", "It was not yet reviewed",
                            "The test was not run", "It was not verified",
                            "Approval pending", "The migration is incomplete",
                            "The migration is unreviewed", "The result is unverified",
                            "The issue is unresolved", "The service is unavailable",
                            "The build is broken", "Awaiting approval", "Review is outstanding"):
            with self.subTest(explanation=explanation):
                self.assertTrue(core.parse_handoff("BLOCKERS: None. " + explanation)["blockers"])

    def test_markdown_emphasized_labels(self):
        # Actual Haiku 4.5 answer from a pinned-model smoke run, 2026-09-24.
        parsed = core.parse_handoff(
            "---\n\n**STATUS:** success\n\n**SUMMARY:** Read calc.py; the add() function is incorrect"
            "—it returns `a - b` instead of `a + b`.\n\n**CHANGED:** none\n\n**TESTS:** none\n\n"
            "**BLOCKERS:** none\n")
        self.assertEqual(parsed["reported_status"], "success")
        self.assertTrue(parsed["summary"].startswith("Read calc.py"))
        self.assertEqual(parsed["blockers"], [])
        failing = core.parse_handoff("**STATUS:** error\n**SUMMARY:** broke\n**BLOCKERS:** tests fail")
        self.assertEqual(failing["reported_status"], "error")
        self.assertEqual(failing["blockers"], ["tests fail"])
        for text in ("**STATUS**: partial", "__STATUS:__ partial", "## STATUS: partial", "STATUS: **partial**"):
            with self.subTest(text=text):
                self.assertEqual(core.parse_handoff(text)["reported_status"], "partial")
        inline = core.parse_handoff("STATUS: success\nSUMMARY: Mentions **STATUS:** error inline\nBLOCKERS: none")
        self.assertEqual(inline["reported_status"], "success")

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

    def test_parsing_a_review_does_not_clear_provider_permission_denials(self):
        envelope = json.dumps({"result": REVIEW_ANSWER, "is_error": False,
                               "permission_denials": [{"tool_name": "mcp__example__run_status",
                                                       "tool_input": {"run_id": "example-run"}}]})
        _, answer, _, _, _, denials = core.parse_claude_output(envelope)
        self.assertTrue(core.parse_handoff(answer)["tests"])
        self.assertTrue(denials)
        self.assertEqual(core.failure_class({"status": "error", "blockers": denials}), "permission_denied")

REVIEW_ANSWER = 'STATUS: success\n\nSUMMARY: Reviewed a synthetic workflow after an earlier missing-prompt failure.\n- **Source review:** inspected a small local event store.\n  - **Atomic writes:** updates and receipts share a transaction.\n  - **Replay:** recorded inputs can reconstruct the projection.\n\nNon-blocking observations:\n1. Large imports may hold a writer lock.\n2. Process contention needs a separate check.\n\nProposed next increment: add an independent verification command.\n\nCHANGED: none\n\nTESTS:\n- The status MCP request was denied by the harness, not performed.\n- Read the saved outcome and its source identity.\n- Read two coordinator acceptance receipts and retained output.\n- Did not run a workspace-writing command in this read-only node.\n- Ran a read-only probe against a disposable in-memory fixture:\n  - An identical retry preserves the original result.\n  - Reusing an ID with different input is rejected.\n  - Invalid input does not consume the request ID.\n  - Event sequence numbers remain contiguous.\n  - Conflicting replay IDs are rejected.\n  - Tampered projections are rejected.\n  - Exporting the reconstructed state preserves the digest.\n  - Historical receipt reads do not mutate state.\n  - The same next command produces the same result.\n  - Inspecting an untouched record leaves the digest unchanged.\n\nBLOCKERS: none. The status MCP request was denied, so the observation came from a saved fixture receipt instead.\n'
