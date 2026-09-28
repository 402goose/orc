"""Acceptance evidence and isolated, fake-worker drafter evaluation."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_decision_cli import add_parser, run
from fusion_decisions import (ACCEPTANCE_QUESTIONS, DecisionEngine, DecisionStore, STATE_VERSION,
                              acceptance_state, estimated_tokens, state_tokens)
from fusion_drafter_eval import evaluate_drafter, format_report


class AcceptanceEvidenceTest(unittest.TestCase):
    def test_read_only_body_and_step_assignment_are_original_input(self):
        source = {"task": "Plan the corrections; do not edit.", "write": False,
                  "decision_context": {"request": "Fix the docs", "stage": "plan"}}
        answer = "STATUS: success\nSUMMARY: Three corrections are supported:\n\n## Findings\nThe docs contradict the code.\n- Fix A.\n- Fix B.\n- Fix C.\n\nFirst line of rationale.\ncontinued prose\nCHANGED: none"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "answer.md"
            path.write_text(answer)
            state = acceptance_state(source, {"summary": "Three corrections are supported:",
                                               "artifacts": {"answer": str(path)}}, 2200)
        self.assertEqual(state["node_task"], source["task"])
        self.assertEqual(state["task"]["request"], "Fix the docs")
        self.assertEqual(state["deliverable"], "## Findings\nThe docs contradict the code.\n- Fix A.\n- Fix B.\n- Fix C.\nFirst line of rationale.")

    def test_write_digest_contains_receipts_and_changed_files(self):
        state = acceptance_state({"task": "Fix A", "write": True},
                                 {"summary": "Fixed A", "changed": ["a.py", "test/a.py"], "tests": ["claimed"],
                                  "acceptance_checks": [{"argv": ["python3", "test/a.py"], "status": "passed", "exit_code": 0}]}, 2200)
        self.assertIn('"a.py", "test/a.py"', state["deliverable"])
        self.assertIn('"argv": ["python3", "test/a.py"]', state["deliverable"])
        self.assertIn('"status": "passed"', state["deliverable"])
        self.assertNotIn("claimed", state["deliverable"])

    def test_saved_workflow_task_matches_node_input_without_request_context(self):
        node = {"id": "plan", "task": "Plan the fix", "write": False}
        saved = {"run_id": "r", "task": "Worker wrapper", "node_task": node["task"], "write": False,
                 "decision_context": {"request": node["task"], "dependencies": []}}
        result = {"summary": "Three findings:"}
        self.assertEqual(acceptance_state(node, result, 2200, answer_text="- A"),
                         acceptance_state(saved, result, 2200, answer_text="- A"))

    def test_digest_uses_remaining_budget_and_marks_omissions(self):
        summary = "Found corrections." * 22
        state = acceptance_state({"task": "Review docs"}, {"summary": summary}, 2200,
                                 answer_text="\n".join(f"- Correction {i}: " + "detail " * 30 for i in range(40)))
        self.assertNotIn("source_truncated", state)
        self.assertEqual(state["summary"], summary)
        self.assertIn("[…truncated", state["deliverable"])
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False, sort_keys=True)), 2200)
        self.assertLessEqual(estimated_tokens(json.dumps(state, ensure_ascii=False, sort_keys=True)), state_tokens("acceptance"))
        tiny = acceptance_state({"task": "requirement " * 400}, {"summary": summary}, 2200)
        self.assertTrue(tiny["source_truncated"])

    def test_long_node_task_is_visibly_excerpted_and_recorded_within_both_budgets(self):
        source = {"task": "Review docs", "node_task": "Review the docs and cite the relevant code. " * 75}
        self.assertGreater(len(source["node_task"]), 3000)
        for cap in (2200, 6000):
            for summary in ("Three findings:", "Found corrections. " * 40):
                with self.subTest(cap=cap, summary_length=len(summary)), tempfile.TemporaryDirectory() as root:
                    state = acceptance_state(source, {"summary": summary}, cap, answer_text="- Fix A.\n- Fix B.\n- Fix C.")
                    self.assertFalse(state.get("source_truncated", False))
                    self.assertTrue(state["node_task"].startswith("Review the docs"))
                    self.assertIn("[…truncated", state["node_task"])
                    self.assertTrue(state["summary"].startswith(summary[:300]))
                    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True)
                    self.assertLessEqual(len(encoded), cap)
                    self.assertLessEqual(estimated_tokens(encoded), state_tokens("acceptance"))
                    engine = DecisionEngine(root, {"decisions": {"max_state_chars": cap}})
                    record = engine.record_unscored("acceptance", state, ACCEPTANCE_QUESTIONS)
                    self.assertFalse(record["truncated"])
                    self.assertEqual(json.loads(record["state"]), state)

    def test_request_detail_has_priority_over_the_digest(self):
        source = {"task": "Review docs", "decision_context": {"request": "Fix docs\n" + "Supporting detail. " * 300,
                                                               "stage": "plan"}}
        state = acceptance_state(source, {"summary": "Three findings:"}, 2200,
                                 answer_text="- Finding: " + "Evidence. " * 500)
        self.assertFalse(state.get("source_truncated", False))
        self.assertGreater(len(state["task"]["request"]), 300)
        self.assertIn("[…truncated", state["task"]["request"])
        self.assertIn("[…truncated", state["deliverable"])
        self.assertLessEqual(estimated_tokens(json.dumps(state, ensure_ascii=False, sort_keys=True)), state_tokens("acceptance"))

    def test_digest_keeps_findings_that_also_appear_in_the_summary(self):
        state = acceptance_state({"task": "Review docs"}, {"summary": "Found: - Fix A. and - Fix B."}, 2200,
                                 answer_text="SUMMARY: Found: - Fix A. and - Fix B.\n\n- Fix A.\n- Fix B.")
        self.assertEqual(state["deliverable"], "- Fix A.\n- Fix B.")

    def test_decisions_record_builder_version_and_source_truncation(self):
        with tempfile.TemporaryDirectory() as root:
            engine = DecisionEngine(root, {})
            for kind in ("acceptance", "review", "routing", "recovery", "intake"):
                record = engine.record_unscored(kind, {"source_truncated": True}, ACCEPTANCE_QUESTIONS)
                self.assertEqual(record["state_version"], STATE_VERSION)
                self.assertTrue(record["source_truncated"])


class DrafterEvalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.store = DecisionStore(self.workspace)
        self.engine = DecisionEngine(self.workspace, {})
        self.calls = []
        self.outputs = []

    def record(self, answers=None, excluded=False, source="human"):
        record = self.engine.record_unscored("acceptance", {"task": "Plan a fix", "summary": "Three findings:"},
                                              ACCEPTANCE_QUESTIONS, {"task_id": "run1"})
        if answers:
            self.store.append("label", id=record["id"], answers=answers, verified=True, source=source)
        if excluded:
            self.store.append("label_exclusion", id=record["id"], excluded=True, source=source)
        return record

    def dispatch(self, config, task, store):
        self.calls.append(task)
        self.assertNotEqual(store.workspace, self.workspace)
        self.assertEqual(Path(task["workspace"]).resolve(), store.workspace.resolve())
        self.assertTrue(DecisionStore(store.workspace).path.read_bytes().startswith(self.store.path.read_bytes()))
        prompt = task["task"]
        self.assertIn("truncation marker", prompt)
        self.assertIn("announces results but shows none of them", prompt)
        self.assertIn("a\nmarker alone is not a reason to abstain", prompt)
        output = self.outputs.pop(0)
        if output == "error":
            return {"status": "error", "summary": "fake unavailable"}
        directory = store.create(task)
        payload = {"answers": {k: {"value": v, "reason": "Supported by E1", "evidence": ["E1"]}
                               for k, v in output.items()},
                   "abstentions": {k: "Missing findings" for k in ACCEPTANCE_QUESTIONS if k not in output}}
        (directory / "answer.md").write_text('```label-suggestion\n' + json.dumps(payload) + '\n```')
        return {"status": "success", "exit_code": 0, "run_id": task["run_id"], "agent": "codex"}

    def evaluate(self, **kwargs):
        before = {str(p.relative_to(self.workspace)): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        with patch.object(core, "dispatch", side_effect=self.dispatch), \
                patch.dict(os.environ, {"FUSION_CONTROL_WORKSPACE": str(self.workspace)}):
            report = evaluate_drafter(self.workspace, core.DEFAULTS, agent="codex", **kwargs)
        after = {str(p.relative_to(self.workspace)): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(all(not Path(task["workspace"]).exists() for task in self.calls))
        return report

    def test_scores_effective_human_reviews_exclusions_and_errors_without_live_writes(self):
        self.record({"plausible": "true", "failed_task": "false"})
        self.record({"plausible": "true"}, source="human_approved_suggestion")
        self.record(excluded=True)
        self.record(excluded=True)
        self.record({"plausible": "true"})
        self.record({"plausible": "true"}, source="structural_gate")
        self.record(excluded=True, source="automatic")
        replaced = self.record({"plausible": "true"})
        self.store.append("label", id=replaced["id"], answers={}, verified=True, replace=True, source="human")
        self.outputs = [{"plausible": "true", "failed_task": "true"}, {}, {}, {"plausible": "false"}, "error"]
        report = self.evaluate()
        self.assertEqual(report["totals"], {"decisions": 5, "agree": 1, "disagree": 1, "abstain": 1,
                                           "errors": 1, "exclusions_abstained": 1, "exclusions_answered": 1})
        self.assertEqual(report["questions"]["plausible"], {"agree": 1, "disagree": 0, "abstain": 1})
        self.assertIn("TOTAL", format_report(report))

    def test_rebuild_uses_saved_artifacts_and_current_builder(self):
        original = self.record({"plausible": "true"})
        self.store.append("decision", **{**original, "state_version": 0})
        directory = self.workspace / ".fusion/runs/run1"
        directory.mkdir(parents=True)
        task = {"run_id": "run1", "task": "worker wrapper", "node_task": "Plan only", "write": False,
                "decision_context": {"request": "Fix the bug", "stage": "plan"}}
        result = {"summary": "Three findings:"}
        answer = "SUMMARY: Three findings:\n\n- A\n- B\n- C"
        for name, value in (("task.json", task), ("result.json", result)):
            (directory / name).write_text(json.dumps(value))
        (directory / "answer.md").write_text(answer)
        self.outputs = [{"plausible": "true"}]
        report = self.evaluate(rebuild_input=True)
        self.assertEqual(report["totals"]["agree"], 1)
        self.assertTrue(report["results"][0]["rebuilt"])
        sources = json.loads(self.calls[0]["task"].split("\nEvidence packet:\n")[1])
        self.assertEqual(json.loads(sources[0]["text"]), acceptance_state(task, result, 2200, answer_text=answer))
        self.assertEqual(report["results"][0]["state_version"], STATE_VERSION)
        task.pop("node_task")
        task["task"] = "You are node plan in a persisted Fusion workflow.\n\nTask: Plan only\n\nDependency artifacts:\n- none"
        (directory / "task.json").write_text(json.dumps(task))
        self.outputs = [{"plausible": "true"}]
        historical = self.evaluate(rebuild_input=True)
        self.assertEqual(historical["totals"]["errors"], 0)
        sources = json.loads(self.calls[-1]["task"].split("\nEvidence packet:\n")[1])
        self.assertEqual(json.loads(sources[0]["text"])["node_task"], "Plan only")

    def test_missing_rebuild_artifacts_are_errors_and_limit_is_respected(self):
        self.record({"plausible": "true"})
        self.record({"plausible": "true"})
        report = self.evaluate(rebuild_input=True, limit=1)
        self.assertEqual(report["totals"]["errors"], 1)
        self.assertEqual(report["totals"]["decisions"], 1)
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            self.evaluate(limit=0)

    def test_cli_json_and_table(self):
        parser = argparse.ArgumentParser()
        add_parser(parser.add_subparsers())
        for flags in ([], ["--json"]):
            args = parser.parse_args(["decisions", "eval-drafter", "--agent", "codex", "--limit", "2", *flags])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(run(args, self.workspace, core.DEFAULTS), 0)
            if flags:
                self.assertEqual(json.loads(out.getvalue())["totals"]["decisions"], 0)
            else:
                self.assertIn("Agree  Disagree  Abstain", out.getvalue())


if __name__ == "__main__":
    unittest.main()
