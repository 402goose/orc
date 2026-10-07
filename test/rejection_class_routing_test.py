"""A rejection classed as a harness problem never moves a lane's score (grader integrity G4)."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy as policy
from fusion_decisions import DecisionStore
from fusion_policy import route_candidates, routing_report

QUALITY = ("suite_red", "no_diff", "out_of_scope", "review_changes", "missed_gap", "false_blocker")
HARNESS = ("land_conflict", "eval_unmeasured", "other")


class RejectionClassRoutingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "FUSION_TELEMETRY": "0",
                                      "FUSION_PROGRESS": "0", "FUSION_DECISIONS_MODE": "off", "FUSION_CONTROL_WORKSPACE": ""})
        env.start()
        self.addCleanup(env.stop)
        executable = patch.object(core, "executable", return_value=True)
        executable.start()
        self.addCleanup(executable.stop)
        policy._PRIORS_CACHE.clear()
        self.config = core.deep_merge(core.DEFAULTS, {
            "decisions": {"mode": "off", "priors": False, "rank_by_outcomes": 1, "auto_routes": ["A", "B"]},
            "routes": {lane: {"agent": "claude", "account": lane, "model": f"model-{lane.lower()}"} for lane in ("A", "B")}})
        self.store = core.RunStore(self.root)
        self.decisions = DecisionStore(self.root)
        self.spans = []

    def task(self, issue="o/r#1"):
        task = core.make_task(self.root, "auto", "Fix the retry logic", "implementation", [], [], None, False, True)
        task["issue"] = issue
        return task

    def record(self, lane, accepted, rejection_class=None, status="success", issue="o/r#1"):
        run_id = f"{lane}-{len(self.spans)}"
        self.spans.append({"agent": "claude", "route": lane, "run_id": run_id, "status": status, "write": True,
                           "role": "implementation"})
        self.decisions.append("outcome", task_id=run_id, route=lane, issue=issue, role="implementation", accepted=accepted,
                              **({"rejection_class": rejection_class} if rejection_class else {}))
        return run_id

    def candidates(self, **extra):
        with patch.object(self.store, "traces", return_value=list(self.spans)):
            return {c["key"]: c for c in route_candidates(self.config, self.task(), self.store, **extra)}

    def test_the_classes_split_every_rejection_class(self):
        self.assertEqual(set(QUALITY) | set(HARNESS), set(core.REJECTION_CLASSES))
        self.assertEqual(policy.HARNESS_REJECTION_CLASSES, frozenset(HARNESS))
        self.assertEqual(policy.QUALITY_REJECTION_CLASSES, frozenset(QUALITY))

    def test_harness_class_rejections_leave_a_lane_as_if_it_had_none(self):
        before = self.candidates()["A"]
        self.record("A", False, "other")
        self.record("A", False, "land_conflict")
        self.record("A", False, "eval_unmeasured")
        after = self.candidates()["A"]
        self.assertEqual((after["checked_runs_local"], after["acceptance_rate_local"]),
                         (before["checked_runs_local"], before["acceptance_rate_local"]))
        self.assertEqual(after["checked_runs_local"], 0)

    def test_an_excluded_rejection_leaves_the_runs_own_status_to_count(self):
        for name in ("eval_unmeasured", "other"):
            with self.subTest(rejection_class=name):
                self.spans.clear()
                self.decisions.path.unlink(missing_ok=True)
                self.record("A", False, name, status="error")
                errored = self.candidates()["A"]
                self.assertEqual((errored["checked_runs_local"], errored["acceptance_rate_local"]), (1, 0.0))
                self.spans.clear()
                self.decisions.path.unlink(missing_ok=True)
                self.record("A", False, name, status="success")
                self.assertEqual(self.candidates()["A"]["checked_runs_local"], 0)

    def test_each_quality_class_counts_against_the_lane(self):
        for name in QUALITY:
            with self.subTest(rejection_class=name):
                self.spans.clear()
                self.decisions.path.unlink(missing_ok=True)
                self.record("A", False, name)
                lane = self.candidates()["A"]
                self.assertEqual((lane["checked_runs_local"], lane["acceptance_rate_local"]), (1, 0.0))

    def test_a_rejection_without_a_class_still_counts(self):
        self.record("A", True)
        self.record("A", False)
        lane = self.candidates()["A"]
        self.assertEqual((lane["checked_runs_local"], lane["acceptance_rate_local"]), (2, 0.5))

    def test_the_thompson_posterior_ignores_excluded_classes(self):
        self.record("A", True)
        self.record("A", False, "land_conflict")
        self.record("A", False, "other")
        self.record("A", False, "suite_red")
        pooled = self.candidates(pool_models=True)["A"]["pooled"]
        self.assertEqual((pooled["successes"], pooled["attempts"]), (1, 2))

    def test_rework_avoids_a_lane_only_for_quality_or_unclassed_rejections(self):
        self.record("A", False, "land_conflict")
        self.record("B", False, "eval_unmeasured")
        self.assertEqual(policy.rework_exclusions(self.task(), self.store), [])
        self.record("B", False, "no_diff")
        self.assertEqual(policy.rework_exclusions(self.task(), self.store), ["B"])

    def test_the_routing_report_skips_excluded_outcomes_but_still_lists_them(self):
        log = lambda task_id: {"event": "routing_log", "task_id": task_id, "chosen": "A",
                               "candidates": [{"key": "A", "propensity": 1.0}]}
        events = [log("t1"), log("t2"), log("t3"),
                  {"event": "outcome", "task_id": "t1", "accepted": False, "issue": "o/r#1", "rejection_class": "land_conflict"},
                  {"event": "outcome", "task_id": "t2", "accepted": False, "issue": "o/r#1", "rejection_class": "suite_red"},
                  {"event": "outcome", "task_id": "t3", "accepted": True}]
        report = routing_report(events)
        self.assertEqual((report["with_outcome"], report["excluded_by_class"]), (2, 1))
        lane = report["lanes"][0]
        self.assertEqual((lane["chosen"], lane["accepted"]), (2, 1))
        self.assertEqual(report["rejections"], [
            {"issue": "o/r#1", "rejection_class": "land_conflict", "count": 1, "counts_against_lane": False},
            {"issue": "o/r#1", "rejection_class": "suite_red", "count": 1, "counts_against_lane": True}])
        self.assertFalse(any("no outcome yet" in warning for warning in report["warnings"]))

    def test_only_a_harness_classed_rejection_is_excluded(self):
        self.assertTrue(policy.counts_against_lane({"accepted": True, "rejection_class": "other"}))
        self.assertTrue(policy.counts_against_lane({"accepted": False}))
        self.assertFalse(policy.counts_against_lane({"accepted": False, "rejection_class": "other"}))


if __name__ == "__main__":
    unittest.main()
