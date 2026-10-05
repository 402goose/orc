"""Rejected issue rework avoids the lanes that failed for the same role (#9)."""
import contextlib
import io
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy as policy
import fusion_progress
from fusion_decisions import DecisionStore, read_jsonl


class ReworkLaneTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off",
                                      "FUSION_CONTROL_WORKSPACE": ""})
        env.start()
        self.addCleanup(env.stop)
        executable = patch.object(core, "executable", return_value=True)
        executable.start()
        self.addCleanup(executable.stop)
        self.store = core.RunStore(self.root)
        self.decisions = DecisionStore(self.root)
        self.config = core.deep_merge(core.DEFAULTS, {
            "decisions": {"mode": "off", "priors": False, "rank_by_outcomes": 3, "auto_routes": ["A", "B", "C"]},
            "routes": {lane: {"agent": "claude", "account": lane, "model": f"model-{lane.lower()}"}
                       for lane in ("A", "B", "C")}})

    def task(self, issue="o/r#1", role="implementation", agent="auto", route=None, model=None):
        task = core.make_task(self.root, agent, "Rework the issue", role, [], [], None, False, True,
                              route=route, settings_overrides={"model": model} if model else None)
        if issue:
            task["issue"] = issue
        return task

    def outcome(self, lane, run=None, issue="o/r#1", role="implementation", accepted=False, **extra):
        run = run or f"rejected-{lane}"
        self.decisions.append("outcome", task_id=run, route=lane, issue=issue, accepted=accepted,
                              **({"role": role} if role is not None else {}), **extra)
        return run

    def route(self, task=None, seed=7):
        task = task or self.task()
        policy.route_task(self.config, task, self.store, random.Random(seed))
        return task

    def explain(self, task=None, seed=7):
        return policy.explain_route(self.config, task or self.task(), self.store, seed)

    def last_log(self):
        return [event for event in read_jsonl(self.decisions.path) if event.get("event") == "routing_log"][-1]

    def test_rejected_lane_is_never_selected_and_has_zero_thompson_propensity(self):
        self.outcome("A")
        reason = "rejected on o/r#1 (implementation)"
        # Rework exclusions belong to ranking, not quota/cooldown candidacy.
        self.assertIn("A", [c["key"] for c in policy.route_candidates(self.config, self.task(), self.store)])
        self.assertNotEqual(self.route()["route"], "A")
        self.config["decisions"]["gating_policy"] = "thompson"
        for seed in range(5):
            with self.subTest(seed=seed):
                self.assertIn(self.route(seed=seed)["route"], {"B", "C"})
                log = self.last_log()
                self.assertEqual(next(c["propensity"] for c in log["candidates"] if c["key"] == "A"), 0.0)
                value = self.explain(seed=seed)
                self.assertIn(value["chosen"], {"B", "C"})
                self.assertEqual(value["excluded_lanes"], ["A"])
                self.assertEqual(value["excluded_reason"], reason)
                self.assertEqual(value["rejected"]["A"], reason)
                self.assertEqual(next(c["propensity"] for c in value["candidates"] if c["key"] == "A"), 0.0)
                self.assertAlmostEqual(sum(c["propensity"] for c in value["candidates"]), 1.0)
                self.assertEqual({f["lead"] for f in value["sampled"]["families"]}, {"B", "C"})

    def test_another_issue_or_role_is_unaffected(self):
        self.outcome("A")
        for issue, role in (("o/r#2", "implementation"), ("o/r#1", "review"),
                            (None, "implementation"), ("o/r#1", None), ("o/r#1", "unknown")):
            with self.subTest(issue=issue, role=role):
                value = self.explain(self.task(issue=issue, role=role))
                self.assertEqual(value["chosen"], "A")
                self.assertEqual(value.get("excluded_lanes", []), [])
                self.assertNotIn("A", value["rejected"])
                self.assertEqual(self.route(self.task(issue=issue, role=role))["route"], "A")

    def test_an_acceptance_overrides_a_rejection_of_the_same_run(self):
        run = self.outcome("A")
        self.outcome("A", run=run, accepted=True)
        self.assertEqual(policy.rework_exclusions(self.task(), self.store), [])
        self.assertEqual(self.explain()["chosen"], "A")
        self.assertEqual(self.route()["route"], "A")

    def test_every_rejected_lane_keeps_the_full_pool_and_is_recorded(self):
        for lane in ("A", "B", "C"):
            self.outcome(lane)
        self.assertEqual(policy.rework_exclusions(self.task(), self.store), ["A", "B", "C"])
        self.assertIn(self.route()["route"], {"A", "B", "C"})
        for value in (self.last_log(), self.explain()):
            self.assertTrue(value["all_lanes_rejected"])
            self.assertEqual(value["excluded_lanes"], [])
            self.assertEqual(value["excluded_reason"], "rejected on o/r#1 (implementation)")
            self.assertEqual({c["key"] for c in value["candidates"]}, {"A", "B", "C"})
            self.assertTrue({"A", "B", "C"}.isdisjoint(value["rejected"]))

    def test_a_pinned_route_is_never_changed(self):
        self.outcome("A")
        for agent in ("auto", "claude"):
            with self.subTest(agent=agent):
                task = self.task(agent=agent, route="A")
                self.assertEqual(policy.rework_exclusions(task, self.store), [])
                self.assertEqual(self.route(task)["route"], "A")

    def test_explicit_agent_and_model_pins_are_retained(self):
        self.outcome("A")
        for model in (None, "model-a"):
            with self.subTest(model=model):
                task = self.task(agent="claude", model=model)
                self.assertEqual(policy.rework_exclusions(task, self.store), [])
                self.route(task)
                self.assertEqual(task["agent"], "claude")
                self.assertIsNone(task["route"])
                self.assertEqual(task["settings_overrides"], {"model": model} if model else {})
        self.assertEqual(policy.rework_exclusions(self.task(model="model-a"), self.store), [])

    def test_routing_log_records_every_exclusion_with_laya_off_and_one_candidate(self):
        self.outcome("B")
        self.outcome("A")
        task = self.route()
        self.assertEqual(task["route"], "C")
        log = self.last_log()
        self.assertEqual(log["task_id"], task["run_id"])
        self.assertEqual(log["scope"], "automatic")
        self.assertEqual(log["excluded_lanes"], ["A", "B"])
        self.assertEqual(log["excluded_reason"], "rejected on o/r#1 (implementation)")
        self.assertEqual(log["rejected"], self.explain()["rejected"])
        self.assertEqual({c["key"]: c["propensity"] for c in log["candidates"]}, {"A": 0, "B": 0, "C": 1})
        self.assertEqual(self.decisions.records(), [])

    def test_legacy_outcome_role_comes_from_the_trace_and_is_normalized(self):
        run = self.outcome("A", role=None)
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.store.traces_path.write_text(json.dumps({"run_id": run, "route": "A", "agent": "claude",
                                                     "role": " Implementation ", "end_time_ms": 0}) + "\n")
        value = self.explain(self.task(role=" IMPLEMENTATION "))
        self.assertEqual(value["excluded_lanes"], ["A"])
        self.assertEqual(value["rejected"]["A"], "rejected on o/r#1 (implementation)")

    def test_a_withdrawn_lead_rejection_does_not_exclude(self):
        run = self.outcome("A", source="lead")
        self.decisions.append("outcome_withdraw", task_id=run, source="lead")
        self.assertEqual(policy.rework_exclusions(self.task(), self.store), [])
        self.assertEqual(self.route()["route"], "A")

    def test_changed_check_inputs_invalidate_a_rejection(self):
        run = self.outcome("A")
        self.decisions.append("outcome_excluded", task_id=run, check_inputs_changed=["test/oracle.py"])
        self.assertEqual(policy.rework_exclusions(self.task(), self.store), [])
        self.assertEqual(self.route()["route"], "A")

    def test_cli_explains_issue_exclusions_without_logging_or_dispatching(self):
        self.outcome("A")
        before = self.decisions.path.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(core, "load_config", return_value=(self.config, None)), \
                patch.object(fusion_progress, "run_logged", side_effect=AssertionError("a worker was started")):
            code = core.main(["--workspace", str(self.root), "--json", "route", "--explain", "--write",
                              "--issue", "o/r#1", "--role", "implementation", "--seed", "7"])
        value = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(value["task"]["issue"], "o/r#1")
        self.assertNotEqual(value["chosen"], "A")
        self.assertEqual(value["excluded_lanes"], ["A"])
        self.assertEqual(value["rejected"]["A"], "rejected on o/r#1 (implementation)")
        self.assertEqual(self.decisions.path.read_bytes(), before)
        self.assertEqual(self.store.traces(10), [])


if __name__ == "__main__":
    unittest.main()
