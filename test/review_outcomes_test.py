"""Reviews carry what they concluded, a land grades the reviews it settles, and runs owed a verdict are listed."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_decisions import DecisionStore, read_jsonl

MINUTE_MS = 60 * 1000


class ReviewOutcomesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.workers = {name: self.worker(name, blockers) for name, blockers in
                        (("approve", "none"), ("block", "the output file loses its annotation"))}
        self.use("approve")

    def worker(self, name, blockers):
        path = self.root / f"claude-{name}"
        path.write_text(f"#!{sys.executable}\nimport json\n"
                        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                        f"'result':'STATUS: success\\nSUMMARY: reviewed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: {blockers}'}}))\n")
        path.chmod(0o755)
        return path

    def use(self, name):
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"},
                                                      "claude": {"command": str(self.workers[name])}})

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(core, "load_config", return_value=(self.config, None)):
            try:
                code = core.main(["--workspace", str(self.workspace), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def delegate(self, role, *extra, worker="approve", at_minute=None):
        self.use(worker)
        code, out, _ = self.cli("--json", "delegate", "--agent", "claude", "--role", role, *extra, "look at it")
        self.assertEqual(code, 0, out)
        result = json.loads(out)
        if at_minute is not None:
            task_path = Path(result["artifacts"]["run_dir"]) / "task.json"
            task = json.loads(task_path.read_text())
            task["created_at"] = 1_700_000_000_000 + at_minute * MINUTE_MS
            task_path.write_text(json.dumps(task))
        return result

    def review(self, worker, at_minute, issue="o/r#7"):
        return self.delegate("review", "--read-only", "--issue", issue, worker=worker, at_minute=at_minute)["run_id"]

    def saved(self, run_id):
        return json.loads((core.RunStore(self.workspace).runs / run_id / "result.json").read_text())

    def outcomes(self, run_id):
        return [e for e in read_jsonl(DecisionStore(self.workspace).path)
                if e.get("event", "").startswith("outcome") and e.get("task_id") == run_id]

    def test_a_review_records_approve_or_changes_requested_and_a_writer_records_neither(self):
        approve = self.review("approve", 0)
        block = self.review("block", 0)
        writer = self.delegate("implementation")["run_id"]
        self.assertEqual(self.saved(approve)["review_verdict"], "approve")
        self.assertEqual(self.saved(block)["review_verdict"], "changes_requested")
        self.assertNotIn("review_verdict", self.saved(writer))

    def test_a_review_that_did_not_complete_has_no_verdict(self):
        self.assertIsNone(core.review_verdict({"status": "error", "exit_code": 1, "blockers": ["timeout after 1800 seconds"]}))
        self.assertEqual(core.review_verdict({"status": "partial", "blockers": ["x"]}), "changes_requested")

    def test_a_land_grades_only_the_reviews_it_settles(self):
        first_block = self.review("block", 0)
        first_approve = self.review("approve", 1)
        final_a = self.review("approve", 40)
        final_b = self.review("approve", 41)
        other_issue = self.review("approve", 41, issue="o/r#8")
        writer = self.delegate("implementation", "--issue", "o/r#7", at_minute=50)["run_id"]
        code, out, _ = self.cli("outcome", writer, "--accepted", "--stage", "land", "--reason", "landed")
        self.assertEqual(code, 0)
        derived = {row["run_id"] for row in json.loads(out)["derived_reviews"]}
        self.assertEqual(derived, {first_block, final_a, final_b})
        [event] = self.outcomes(first_block)
        self.assertEqual((event["accepted"], event["stage"], event["reporter"], event["source"], event["issue"]),
                         (True, "review", "orc-land", "lead", "o/r#7"))
        self.assertEqual(self.outcomes(first_approve), [])
        self.assertEqual(self.outcomes(other_issue), [])

    def test_a_final_round_blocker_leaves_the_whole_round_to_the_lead(self):
        approve = self.review("approve", 0)
        block = self.review("block", 1)
        writer = self.delegate("implementation", "--issue", "o/r#7", at_minute=20)["run_id"]
        payload = core.record_outcome(self.workspace, writer, True, "landed", stage="land")
        self.assertEqual(payload["derived_reviews"], [])
        self.assertEqual(self.outcomes(approve) + self.outcomes(block), [])

    def test_a_derived_verdict_never_overrides_the_lead(self):
        review = self.review("approve", 0)
        core.record_outcome(self.workspace, review, False, "missed the leaked temp tree", stage="review")
        writer = self.delegate("implementation", "--issue", "o/r#7", at_minute=20)["run_id"]
        payload = core.record_outcome(self.workspace, writer, True, "landed", stage="land")
        self.assertEqual(payload["derived_reviews"], [])
        self.assertEqual([e["accepted"] for e in self.outcomes(review)], [False])

    def test_a_rejected_land_or_one_without_an_issue_derives_nothing(self):
        review = self.review("approve", 0)
        writer = self.delegate("implementation", "--issue", "o/r#7", at_minute=20)["run_id"]
        self.assertNotIn("derived_reviews", core.record_outcome(self.workspace, writer, False, "red", stage="land"))
        bare = self.delegate("implementation", at_minute=21)["run_id"]
        self.assertNotIn("derived_reviews", core.record_outcome(self.workspace, bare, True, "landed", stage="land"))
        self.assertEqual(self.outcomes(review), [])

    def test_pending_splits_unreported_from_unmeasured_and_skips_measured_runs(self):
        measured = self.delegate("implementation")["run_id"]
        unmeasured = self.delegate("suite-author")["run_id"]
        unreported = self.review("approve", None)
        core.record_outcome(self.workspace, measured, True, "kept", stage="gate")
        core.record_outcome(self.workspace, unmeasured, None, "gate: eval did not measure: timed out after 3600s",
                            stage="gate", unmeasured=True)
        code, out, _ = self.cli("--json", "outcome", "--pending")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([row["run_id"] for row in payload["unreported"]], [unreported])
        [row] = payload["unmeasured"]
        self.assertEqual((row["run_id"], row["role"]), (unmeasured, "suite-author"))
        self.assertIn("timed out after 3600s", row["reason"])
        self.assertEqual(payload["counts"], {"review": {"unreported": 1, "unmeasured": 0},
                                             "suite-author": {"unreported": 0, "unmeasured": 1}})
        code, out, _ = self.cli("--json", "outcome", "--pending", "--role", "review")
        self.assertEqual(json.loads(out)["unmeasured"], [])
        code, out, _ = self.cli("outcome", "--pending")
        self.assertTrue(out.startswith("unreported 1, unmeasured 1"))

    def test_pending_hours_drops_older_runs(self):
        self.review("approve", 0)
        payload = core.pending_outcomes(self.workspace, hours=1)
        self.assertEqual(payload["unreported"], [])

    def test_outcome_still_needs_a_run_and_a_verdict_and_pending_takes_neither(self):
        run = self.delegate("implementation")["run_id"]
        self.assertEqual(self.cli("outcome", run)[0], 2)
        self.assertEqual(self.cli("outcome", "--accepted")[0], 2)
        self.assertEqual(self.cli("outcome", run, "--pending")[0], 2)
        self.assertEqual(self.cli("outcome", "--pending", "--accepted")[0], 2)
        self.assertEqual(self.outcomes(run), [])

    def test_an_unknown_run_exits_4_and_names_the_workspace_searched(self):
        code, _, err = self.cli("outcome", "20990101-000000-deadbeef", "--accepted")
        self.assertEqual(code, 4)
        self.assertIn(str(self.workspace.resolve()), err)
        self.assertIn("(from --workspace)", err)
        control = self.root / "control"
        control.mkdir()
        code, _, err = self.cli("--control-workspace", str(control), "outcome", "20990101-000000-deadbeef", "--accepted")
        self.assertEqual(code, 4)
        self.assertIn(f"{control.resolve()} (from --control-workspace)", err)
        self.assertEqual(self.cli("outcome", "not a run id!", "--accepted")[0], 2)
        with self.assertRaises(ValueError):
            core.record_outcome(self.workspace, "20990101-000000-deadbeef", True)


if __name__ == "__main__":
    unittest.main()
