import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fusion_core as core
from fusion_report import findings, format_report, read_answer, select_report
from fusion_workflow import workflow_report


ANSWER = """STATUS: success
SUMMARY: Completed the read-only audit. Recommendations only.

| Rank | Improvement | Risk |
| 1 | Lifecycle | High |
| 3 | API parity | Medium |

1. **Unify lifecycle.** Preserve uncertainty.

3. **Prevent API drift.** Six paths are absent from OpenAPI.
   **Acceptance:** Regeneration is deterministic; denied scopes stay denied.
   **Verification:** Add parity coverage, then run:
   ```sh
   pnpm api:spec
1. **This is a code example, not a finding.**
   ```

5. **Bound history.** Measure queries first.

Caveats: Runtime performance was not measured.
CHANGED: none
TESTS: static checks passed; runtime suites were not run
BLOCKERS: none
"""


class ReportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name) / "repo with spaces $(echo nope)"
        self.workspace.mkdir()
        self.run_id = "workflow-fixture"
        self.root = self.workspace / ".fusion/workflows" / self.run_id
        self.root.mkdir(parents=True)
        self.manifest = {"status": "success", "task": "Find useful improvements", "spent_usd": 0,
                         "nodes": {}, "spec": {"graph": {"nodes": []}}}
        self.add_node("explore", "Exploration evidence")
        self.add_node("plan", ANSWER, needs=["explore"])
        self.save()
        environment = patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off"})
        environment.start()
        self.addCleanup(environment.stop)

    def add_node(self, name, answer, needs=None, agent="codex"):
        root = self.workspace / ".fusion/runs" / name
        root.mkdir(parents=True, exist_ok=True)
        if agent == "codex":
            events = [{"type": "item.completed", "item": {"type": "reasoning", "text": "HIDDEN REASONING"}},
                      {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "HIDDEN COMMAND"}},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": "Still working"}},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": answer}}]
            raw = "\n".join(json.dumps(item) for item in events)
        else:
            raw = json.dumps({"result" if agent == "claude" else "response": answer})
        (root / "stdout.log").write_text(raw)
        result = {"agent": agent, "run_id": name, "status": "success", "summary": "Opening summary only",
                  "changed": [], "tests": ["static checks passed"], "blockers": [], "duration_ms": 123000,
                  "usage": {"input_tokens": 1000}, "artifacts": {"stdout": str(root / "stdout.log")}}
        self.manifest["nodes"][name] = {"agent": "auto", "write": False, "status": "success", "attempts": 1, "result": result}
        self.manifest["spec"]["graph"]["nodes"].append({"id": name, "needs": needs or []})

    def save(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))

    def report(self, **options):
        return select_report(workflow_report(self.workspace, self.run_id), **options)

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = core.main(["--workspace", str(self.workspace), *args])
        return status, output.getvalue(), errors.getvalue()

    def test_default_recovers_actual_deliverable_without_altering_receipts_or_dispatching(self):
        before = {p: p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        with patch.object(core, "dispatch") as dispatch:
            report = self.report()
            text = format_report(report)
        dispatch.assert_not_called()
        self.assertEqual(report["primary_nodes"], ["plan"])
        self.assertEqual(report["selected_outputs"][0]["text"], ANSWER.strip())
        self.assertIn("Six paths are absent", text)
        self.assertIn("Runtime performance was not measured", text)
        self.assertIn("runtime suites were not run", text)
        self.assertNotIn("Exploration evidence", text)
        self.assertNotIn("HIDDEN", text)
        self.assertNotIn("Still working", text)
        self.assertIn("plan [codex]", text)
        self.assertIn("model not reported", text)
        self.assertNotIn("codex/None/", text)
        self.assertIn("Cost: not reported", text)
        self.assertNotIn("$0.0000", text)
        self.assertEqual(before, {p: p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()})

    def test_focused_finding_generates_working_scoped_preparation_command(self):
        report = self.report(finding=3)
        text = format_report(report)
        self.assertIn("Six paths are absent", text)
        self.assertNotIn("Measure queries first", text)
        argv = shlex.split(report["selected_finding"]["prepare_command"])
        self.assertEqual(argv[:4], ["orc", "fusion", "--workspace", str(self.workspace)])
        with patch.object(core, "dispatch") as dispatch, contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(core.main(argv[2:]), 0)
        dispatch.assert_not_called()
        prepared = json.loads(output.getvalue())
        self.assertEqual(prepared["kind"], "build")
        spec = json.loads(Path(prepared["workflow"]).read_text())
        self.assertEqual(sum(bool(node["write"]) for node in spec["nodes"]), 1)
        request = json.loads(Path(prepared["request"]).read_text())["text"]
        self.assertIn("Implement only recommendation 3", request)
        self.assertIn("--finding 3", request)
        self.assertIn("worktree rules", request)

    def test_json_output_node_selection_and_markdown_export(self):
        target = self.workspace / "report.md"
        code, out, err = self.cli("--json", "workflow", "report", self.run_id, "--node", "explore", "--output", str(target))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["selected_outputs"][0]["node_id"], "explore")
        self.assertIn("Exploration evidence", target.read_text())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["report_artifact"], str(target.resolve()))
        previous = target.read_bytes()
        code, _, err = self.cli("workflow", "report", self.run_id, "--output", str(target))
        self.assertEqual(code, 2)
        self.assertIn("File exists", err)
        self.assertEqual(target.read_bytes(), previous)

    def test_missing_logs_are_explicit_and_do_not_hide_receipt_blockers(self):
        node = self.manifest["nodes"]["plan"]
        node["status"] = "invalid"
        node["result"]["blockers"] = ["verification missing"]
        self.manifest["status"] = "failed"
        self.manifest["error"] = "acceptance check failed"
        self.save()
        Path(node["result"]["artifacts"]["stdout"]).unlink()
        text = format_report(self.report())
        self.assertIn("Full worker answer unavailable", text)
        self.assertIn("verification missing", text)
        self.assertIn("acceptance check failed", text)
        self.assertIn("workflow resume", text)
        self.assertNotIn("Prepare this implementation", text)

    def test_saved_answer_survives_missing_logs_and_keeps_long_deliverables(self):
        result = self.manifest["nodes"]["plan"]["result"]
        answer = Path(result["artifacts"]["stdout"]).with_name("answer.md")
        content = ANSWER + "additional evidence\n" * 1000 + "LAST ACCEPTANCE CRITERION"
        answer.write_text(content)
        result["artifacts"]["answer"] = str(answer)
        Path(result["artifacts"]["stdout"]).unlink()
        self.save()
        recovered = self.report()["selected_outputs"][0]
        self.assertEqual(recovered["source"], "answer")
        self.assertEqual(recovered["text"], content)

    def test_legacy_provider_envelopes_and_controls(self):
        for agent in ("claude", "agy"):
            with self.subTest(agent=agent):
                self.add_node(agent, "\x1b]0;malicious-title\x07\x1b[2J" + ANSWER, agent=agent)
                self.save()
                text = format_report(self.report(node=agent))
                self.assertIn("Six paths are absent", text)
                self.assertNotIn("\x1b", text)
                self.assertNotIn("malicious-title", text)

    def test_malformed_events_do_not_become_a_deliverable(self):
        result = self.manifest["nodes"]["plan"]["result"]
        Path(result["artifacts"]["stdout"]).write_text('[]\nnull\n{"type":"item.completed","item":"invalid"}\n{"truncated":')
        recovered = read_answer(self.workspace, result)
        self.assertEqual(recovered["source"], "summary")
        self.assertEqual(recovered["text"], "Opening summary only")

    def test_running_report_offers_watch_and_failed_findings_cannot_launch_implementation(self):
        self.manifest["status"] = "running"
        self.save()
        text = format_report(self.report(finding=3))
        self.assertIn("workflow watch", text)
        self.assertNotIn("workflow resume", text)
        self.assertNotIn("Prepare this implementation", text)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.cli("build", "--from-workflow", self.run_id, "--finding", "3", "--plan-only")
        self.assertFalse((self.workspace / ".fusion/builds").exists())

    def test_unknown_selection_is_actionable_and_does_not_export(self):
        target = self.workspace / "report.md"
        code, _, err = self.cli("workflow", "report", self.run_id, "--finding", "99", "--output", str(target))
        self.assertEqual(code, 2)
        self.assertIn("numbered findings: 1, 3, 5", err)
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError, "unknown node"):
            self.report(node="missing")

    def test_independent_leaf_outputs_are_all_visible(self):
        self.add_node("independent", "Independent findings")
        self.save()
        text = format_report(self.report())
        self.assertIn("Independent findings", text)
        self.assertIn("Six paths are absent", text)
        with self.assertRaisesRegex(ValueError, "--node"):
            self.report(finding=3)

    def test_real_zero_cost_is_distinct_from_unreported_cost(self):
        self.manifest["nodes"]["plan"]["result"]["usage"]["cost_usd"] = 0
        self.save()
        text = format_report(self.report(node="plan"))
        self.assertIn("Reported cost: $0.0000 (1/2 calls", text)
        self.assertNotIn("Cost: not reported", text)

    def test_ambiguous_numbering_and_fenced_examples_do_not_generate_wrong_actions(self):
        self.assertEqual([item["number"] for item in findings(ANSWER)], [1, 3, 5])
        self.assertEqual(findings("1. **One**\n1. **Different one**"), [])


if __name__ == "__main__":
    unittest.main()
