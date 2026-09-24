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
import fusion_decisions
from fusion_decisions import (ACCEPTANCE_QUESTIONS, DecisionEngine, DecisionStore, fit_calibration, read_jsonl,
                              reviewed_labels)
from fusion_learning import decision_rows, learning_summary
from fusion_training_loop import readiness, tokens
from decisions_test import Backend


def no_runtime(options):
    raise AssertionError("a lead verdict must never start the Laya runtime")


class VerdictLabelsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ["ORC_HOME"] = str(self.workspace / "orc-home")
        os.environ.pop("FUSION_DECISIONS_MODE", None)
        self.write_config({"decisions": {"mode": "shadow"}})
        runtime = patch.object(fusion_decisions, "runtime_for", no_runtime)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.store = DecisionStore(self.workspace)

    def write_config(self, value):
        path = self.workspace / "fusion-config.json"
        path.write_text(json.dumps(value))
        os.environ["FUSION_CONFIG"] = str(path)

    def run_dir(self, run_id="20260924-000000-abcdef12", status="success", task="Add CSV export with tests", **task_fields):
        run = core.RunStore(self.workspace).runs / run_id
        run.mkdir(parents=True)
        (run / "task.json").write_text(json.dumps({"run_id": run_id, "task": task, "trace_id": run_id, **task_fields}))
        (run / "result.json").write_text(json.dumps({"status": status, "summary": "Added CSV export", "changed": ["export.py"],
                                                     "tests": ["pytest -q: 3 passed"], "route": "orc-free", "agent": "claude"}))
        return run_id

    def events(self, name):
        return [e for e in read_jsonl(self.store.path) if e.get("event") == name]

    def verdict(self, run_id, accepted, reason="Read the diff; CSV rows match and the tests cover empty input"):
        return core.record_outcome(self.workspace, run_id, accepted, reason)

    def test_accepted_verdict_labels_both_questions_on_an_unscored_acceptance_decision(self):
        run_id = self.run_dir(parent_task_id="workflow-1")
        payload = self.verdict(run_id, True)
        self.assertEqual(payload["label"]["status"], "labeled")
        self.assertEqual(payload["label"]["answers"], {"plausible": "true", "failed_task": "false"})
        [decision] = self.events("decision")
        self.assertEqual((decision["kind"], decision["status"], decision["prediction"], decision["recommendations"]),
                         ("acceptance", "unscored", {}, {}))
        self.assertEqual(decision["questions"], ACCEPTANCE_QUESTIONS)
        self.assertEqual(decision["context"], {"task_id": run_id, "group": "workflow-1"})
        self.assertEqual(json.loads(decision["state"]), {"task": "Add CSV export with tests", "summary": "Added CSV export",
                                                         "changed": ["export.py"], "tests": ["pytest -q: 3 passed"]})
        [label] = self.events("label")
        self.assertEqual((label["source"], label["verified"], label["reviewers"][0]["agent"]), ("lead_verdict", True, "lead"))
        self.assertIn("CSV rows match", label["evidence"])
        self.assertIn(str(core.RunStore(self.workspace).runs / run_id / "result.json"), label["evidence"])
        self.assertFalse(DecisionEngine(self.workspace, {"decisions": {"mode": "active", "auto_actions": ["acceptance"]}}).allowed(decision, "plausible"))

    def test_rejection_answers_only_plausible_and_a_later_verdict_supersedes_it(self):
        run_id = self.run_dir()
        rejected = self.verdict(run_id, False, "Tests pass but the export drops quoted commas")
        self.assertEqual((rejected["label"]["answers"], rejected["label"]["unlabeled"]), ({"plausible": "false"}, ["failed_task"]))
        accepted = self.verdict(run_id, True)
        self.assertEqual(accepted["label"]["decision_id"], rejected["label"]["decision_id"])
        self.assertEqual(len(self.events("decision")), 1)
        answers, _ = reviewed_labels(read_jsonl(self.store.path))
        self.assertEqual(answers[accepted["label"]["decision_id"]], {"plausible": "true", "failed_task": "false"})
        self.verdict(run_id, False, "Second look: the header row is missing")
        answers, _ = reviewed_labels(read_jsonl(self.store.path))
        self.assertEqual(answers[accepted["label"]["decision_id"]], {"plausible": "false"})

    def test_verdict_without_reason_records_the_outcome_and_retracts_an_earlier_verdict_label(self):
        run_id = self.run_dir()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(core.main(["--workspace", str(self.workspace), "outcome", run_id, "--accepted"]), 0)
        self.assertEqual(json.loads(output.getvalue())["label"]["status"], "skipped")
        self.assertEqual((len(self.events("outcome")), self.events("decision"), self.events("label")), (1, [], []))
        labeled = self.verdict(run_id, True)
        retracted = self.verdict(run_id, False, "")
        self.assertEqual((retracted["label"]["status"], retracted["label"]["decision_id"]), ("retracted", labeled["label"]["decision_id"]))
        answers, _ = reviewed_labels(read_jsonl(self.store.path))
        self.assertFalse(answers.get(labeled["label"]["decision_id"]))
        [row] = decision_rows(self.workspace)
        self.assertNotEqual(row["garden_state"], "approved")

    def test_only_reported_successes_with_a_known_task_are_labeled(self):
        failed = self.run_dir("run-failed", status="error")
        self.assertEqual(self.verdict(failed, False)["label"]["status"], "skipped")
        orphan = core.RunStore(self.workspace).runs / "run-orphan"
        orphan.mkdir(parents=True)
        (orphan / "result.json").write_text(json.dumps({"status": "success", "summary": "done"}))
        self.assertIn("task.json", self.verdict("run-orphan", True)["label"]["reason"])
        self.assertEqual(self.events("decision"), [])

    def test_long_briefs_are_recorded_truncated_and_left_unlabeled(self):
        run_id = self.run_dir(task="x" * 5000)
        label = self.verdict(run_id, True)["label"]
        self.assertEqual(label["status"], "skipped")
        self.assertTrue(self.events("decision")[0]["truncated"])
        self.assertEqual(self.events("label"), [])

    def test_a_workflow_gate_decision_is_labeled_in_place(self):
        run_id = self.run_dir(parent_task_id="workflow-2")
        engine = DecisionEngine(self.workspace, {"decisions": {"mode": "shadow"}}, Backend({"plausible": "true"}))
        gate = engine.decide("acceptance", {"task": "stage task", "summary": "s", "changed": [], "tests": []},
                             ACCEPTANCE_QUESTIONS, {"task_id": run_id, "group": "workflow-2"})
        label = self.verdict(run_id, False, "The stage edited the wrong module")["label"]
        self.assertEqual(label["decision_id"], gate["id"])
        self.assertEqual(len(self.events("decision")), 1)

    def test_an_unavailable_gate_input_is_reused_without_inference(self):
        run_id = self.run_dir(parent_task_id="workflow-3")
        engine = DecisionEngine(self.workspace, {"decisions": {"mode": "shadow"}}, Backend(malformed=True))
        gate = engine.decide("acceptance", {"task": "stage task", "summary": "s", "changed": [], "tests": []},
                             ACCEPTANCE_QUESTIONS, {"task_id": run_id, "group": "workflow-3"})
        self.assertEqual(gate["status"], "unavailable")
        label = self.verdict(run_id, True)["label"]
        unscored = next(e for e in self.events("decision") if e["id"] == label["decision_id"])
        self.assertEqual((unscored["status"], unscored["state"], unscored["context"]), ("unscored", gate["state"], gate["context"]))

    def test_other_reviewers_labels_are_preserved(self):
        run_id = self.run_dir()
        first = self.verdict(run_id, True)["label"]
        self.store.label(first["decision_id"], {"failed_task": "true"}, "Human check: the export button is missing")
        second = self.verdict(run_id, False, "Rejected after review")["label"]
        self.assertEqual(second["status"], "preserved")
        answers, _ = reviewed_labels(read_jsonl(self.store.path))
        self.assertEqual(answers[first["decision_id"]], {"plausible": "true", "failed_task": "true"})

    def test_verdicts_never_create_routing_labels(self):
        run_id = self.run_dir()
        engine = DecisionEngine(self.workspace, {"decisions": {"mode": "shadow"}}, Backend())
        routing = engine.decide("routing", {"task": "t"}, {"lane": {"type": "choice", "instructions": "Which lane?",
                                                                     "criteria": {"a": "one", "b": "two"}}},
                                {"task_id": run_id, "group": run_id})
        self.verdict(run_id, True)
        self.assertTrue(all(label["id"] != routing["id"] for label in self.events("label")))
        kinds = {e["id"]: e["kind"] for e in self.events("decision")}
        self.assertEqual({kinds[label["id"]] for label in self.events("label")}, {"acceptance"})

    def test_switch_disables_verdict_labels_and_is_validated(self):
        run_id = self.run_dir()
        self.write_config({"decisions": {"verdict_labels": False}})
        payload = self.verdict(run_id, True)
        self.assertEqual((payload["recorded"], payload["label"]["status"]), (True, "disabled"))
        self.assertEqual(self.events("label"), [])
        os.environ["FUSION_DECISIONS_MODE"] = "off"
        self.write_config({"decisions": {"verdict_labels": True}})
        self.assertEqual(self.verdict(run_id, True)["label"]["reason"], "decisions.mode is off")
        self.assertEqual((self.events("label"), self.events("decision")), ([], []))
        del os.environ["FUSION_DECISIONS_MODE"]
        self.write_config({"decisions": {"verdict_labels": "yes"}})
        with self.assertRaisesRegex(ValueError, "verdict_labels"):
            self.verdict(run_id, True)

    def test_learning_views_and_export_show_verdict_provenance(self):
        for index in range(6):
            self.verdict(self.run_dir(f"run-{index}"), index % 2 == 0)
        rows = decision_rows(self.workspace)
        self.assertEqual({row["garden_state"] for row in rows}, {"approved"})
        self.assertEqual({p["source"] for row in rows for p in row["label_provenance"].values()}, {"lead_verdict"})
        summary = learning_summary(self.workspace, {"decisions": {"mode": "shadow"}}, rows)
        self.assertEqual((summary["reviewed_decisions"], summary["labeled_questions"]), (6, 9))
        self.assertEqual(len(tokens(rows)), 9)
        self.assertEqual(sum(readiness(rows).values()), 6)
        exported = self.store.export(self.workspace / "all.jsonl")
        self.assertEqual((exported["examples"], exported["data_quality"]["approval_sources"]), (6, {"lead_verdict": 9}))
        row = json.loads((self.workspace / "all.jsonl").read_text().splitlines()[0])
        self.assertEqual((row["prediction"], row["model_identity"]), ({}, None))
        self.assertEqual({p["source"] for p in row["label_provenance"].values()}, {"lead_verdict"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            core.main(["--workspace", str(self.workspace), "decisions", "export", str(self.workspace / "human.jsonl"),
                       "--exclude-source", "lead_verdict"])
        self.assertEqual(json.loads(output.getvalue())["examples"], 0)
        with self.assertRaisesRegex(ValueError, "no example in this dataset has stored predictions"):
            fit_calibration(self.workspace / "all.jsonl", self.workspace / "calibration.json")

    def test_calibration_skips_unscored_rows_beside_scored_ones(self):
        engine = DecisionEngine(self.workspace, {"decisions": {"mode": "shadow"}}, Backend({"plausible": "true"}))
        scored = engine.decide("acceptance", {"task": "t", "summary": "s", "changed": [], "tests": []},
                               ACCEPTANCE_QUESTIONS, {"task_id": "w", "group": "w"})
        self.store.label(scored["id"], {"plausible": "true"}, "Human verified the diff")
        self.verdict(self.run_dir(), True)
        self.store.export(self.workspace / "mixed.jsonl")
        report = fit_calibration(self.workspace / "mixed.jsonl", self.workspace / "calibration.json")
        self.assertEqual((report["model_identity"], report["unscored_examples"]), ("fixture-model", 1))


if __name__ == "__main__":
    unittest.main()
