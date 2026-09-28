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
from fusion_decisions import DecisionEngine, DecisionStore, digest, read_jsonl
from fusion_policy import propensities, rank_by_outcomes, route_candidates, route_task, routing_report


class Backend:
    def __init__(self, route=None):
        self.route = route

    def predict(self, state, questions):
        answers = {}
        for key, question in questions.items():
            labels = list(question["criteria"])
            selected = self.route if self.route in labels else labels[0]
            answers[key] = {"probabilities": {label: .99 if label == selected else .01 / (len(labels) - 1) for label in labels}}
        return {"answers": answers, "model_identity": "fixture-model"}


class Rng:
    def __init__(self, draw, pick):
        self.draw, self.pick, self.calls = draw, pick, 0

    def random(self):
        self.calls += 1
        return self.draw

    def randrange(self, n):
        self.calls += 1
        return self.pick


class Untouchable:
    def random(self):
        raise AssertionError("exploration must not draw for this task")

    randrange = random


class RoutingPropensityTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.workspace / "orc-home")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_DECISIONS_MODE", None)
        os.environ.pop("FUSION_READ_ONLY", None)
        self.config = {"decisions": {"mode": "shadow"}, "routes": {},
                       "codex": {"command": sys.executable}, "claude": {"command": sys.executable},
                       "agy": {"command": "missing-fusion-test-agent"}, "grok": {"command": "missing-fusion-test-agent"}}

    def task(self, agent="auto", write=False, role="implementation", **extra):
        return core.make_task(self.workspace, agent, "Map the retry logic", role, [], [], None, False, write, **extra)

    def route(self, task, rng=None, engine=None):
        engine = engine or DecisionEngine(self.workspace, self.config, Backend())
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            route_task(self.config, task, core.RunStore(self.workspace), rng)
        return task

    def logs(self):
        return [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "routing_log"]

    def evidence(self, key, accepted, checked):
        store = core.RunStore(self.workspace)
        store.root.mkdir(parents=True, exist_ok=True)
        for n in range(checked):
            run_id = f"{key}-{n}"
            with store.traces_path.open("a") as stream:
                stream.write(json.dumps({"run_id": run_id, "agent": "codex" if key in self.config["routes"] else key,
                                         "route": key if key in self.config["routes"] else None, "status": "success"}) + "\n")
            DecisionStore(self.workspace).append("outcome", task_id=run_id, accepted=n < accepted)

    def test_ninth_route_ranked_before_candidate_cap(self):
        self.config["decisions"]["rank_by_outcomes"] = 3
        self.config["routes"] = {f"lane-{n}": {"agent": "codex"} for n in range(1, 10)}
        self.evidence("lane-9", 3, 3)
        candidates = route_candidates(self.config, self.task(write=True), core.RunStore(self.workspace))
        self.assertEqual(len(candidates), 11)
        self.assertEqual(candidates[-1]["key"], "lane-9")
        backend = Backend()
        with patch.object(backend, "predict", wraps=backend.predict) as predict:
            task = self.route(self.task(write=True), engine=DecisionEngine(self.workspace, self.config, backend))
        self.assertEqual(task["route"], "lane-9")
        self.assertEqual(len(predict.call_args.args[1]["route"]["criteria"]), 8)
        [log] = self.logs()
        self.assertEqual(len(log["candidates"]), 8)
        self.assertEqual(log["chosen"], "lane-9")

    def test_cost_tie_breaker_and_route_override_are_logged_and_reported(self):
        self.config["decisions"]["rank_by_outcomes"] = 3
        self.config["codex"]["cost_tier"] = 5
        self.config["routes"] = {"cheap": {"agent": "codex", "cost_tier": 1},
                                 "inherited": {"agent": "codex"}}
        self.evidence("codex", 3, 3)
        self.evidence("cheap", 6, 7)
        task = self.route(self.task(write=True))
        self.assertEqual(task["route"], "cheap")
        [log] = self.logs()
        costs = {c["key"]: c["cost_tier"] for c in log["candidates"]}
        self.assertEqual(costs, {"codex": 5, "cheap": 1, "inherited": 5, "claude": None})
        self.assertEqual(log["policy"]["cost_epsilon"], .05)
        [reported] = routing_report(self.logs())["routing_policies"]
        self.assertEqual(reported["policy"], log["policy"])
        self.assertEqual(reported["cost_tiers"], costs)
        self.config["decisions"]["cost_epsilon"] = .01
        self.assertIsNone(self.route(self.task(write=True))["route"])
        self.config["cache"] = {"warm_epsilon": .05}
        self.assertEqual(self.route(self.task(write=True))["route"], "cheap")

    def test_cost_tier_requires_integer_and_route_can_clear_preference(self):
        self.config["codex"]["cost_tier"] = 2
        self.config["routes"] = {"unset": {"agent": "codex", "cost_tier": None}}
        self.assertIsNone(core.agent_settings(self.config, {"agent": "codex", "route": "unset"})["cost_tier"])
        for invalid in (True, 1.5, "1"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "cost_tier"):
                core.agent_settings({"codex": {"cost_tier": invalid}}, {"agent": "codex"})

    def test_cost_never_beats_clear_evidence_and_orders_exploration(self):
        expensive = {"key": "expensive", "checked_runs": 3, "acceptance_rate": 1, "cost_tier": 5}
        cheap = {"key": "cheap", "checked_runs": 7, "acceptance_rate": 6 / 7, "cost_tier": 1}
        keys = lambda candidates, **kw: [c["key"] for c in rank_by_outcomes(candidates, **kw)]
        self.assertEqual(keys([expensive, cheap]), ["cheap", "expensive"])
        self.assertEqual(keys([expensive, {**cheap, "acceptance_rate": 2 / 7}]), ["expensive", "cheap"])
        new = [{**expensive, "checked_runs": 0}, {**cheap, "checked_runs": 0}]
        self.assertEqual(keys(new), ["cheap", "expensive"])
        self.assertEqual(keys(new, warm_epsilon=.05), ["cheap", "expensive"])
        unset = [{"key": "a", "checked_runs": 0}, {"key": "b", "checked_runs": 0}]
        self.assertEqual(keys(unset), ["a", "b"])
        self.assertEqual(keys([expensive, {**cheap, "quota": {"classification": "tight"}}]), ["expensive", "cheap"])
        self.assertEqual(keys([{**expensive, "warm": True}, cheap]), ["cheap", "expensive"])

    def test_unset_costs_retain_existing_outcome_and_warmth_rankings(self):
        candidates = [{"key": "cold", "checked_runs": 3, "acceptance_rate": 1},
                      {"key": "warm", "checked_runs": 7, "acceptance_rate": 6 / 7, "warm": True},
                      {"key": "new", "checked_runs": 0}]
        for null_cost in (False, True):
            lanes = [{**c, **({"cost_tier": None} if null_cost else {})} for c in candidates]
            for epsilon, expected in ((None, ["new", "cold", "warm"]), (.05, ["new", "warm", "cold"])):
                self.assertEqual([c["key"] for c in rank_by_outcomes(lanes, warm_epsilon=epsilon)], expected)

    def test_propensities_sum_to_one(self):
        keys = ["a", "b", "c"]
        self.assertEqual(propensities(keys, "b", 0), {"a": 0.0, "b": 1.0, "c": 0.0})
        mixed = propensities(keys, "c", .3)
        self.assertAlmostEqual(sum(mixed.values()), 1.0)
        self.assertAlmostEqual(mixed["a"], .7 + .1)
        self.assertAlmostEqual(mixed["c"], .1)
        self.assertEqual(propensities(["only"], "only", .5), {"only": 1.0})

    def test_deterministic_choice_logs_policy_candidates_and_propensity(self):
        self.config["decisions"]["rank_by_outcomes"] = 2
        task = self.route(self.task())
        [log] = self.logs()
        self.assertEqual((log["task_id"], log["scope"], log["chosen"], log["explored"]), (task["run_id"], "automatic", "codex", False))
        self.assertEqual(log["policy"], {"rank_by_outcomes": 2, "explore": True, "warm_epsilon": None, "epsilon": 0.0,
                                         "routing_epsilon": 0.0, "laya_applied": False, "priors": None})
        self.assertEqual({c["key"]: c["propensity"] for c in log["candidates"]}, {"codex": 1.0, "claude": 0.0})
        self.assertIn("checked_runs", log["candidates"][0])
        self.assertEqual(log["decision_id"], task["decisions"]["routing"])

    def test_single_candidate_is_logged_without_asking_laya(self):
        self.config["claude"] = {"command": "missing-fusion-test-agent"}
        backend = Backend()
        with patch.object(backend, "predict", side_effect=AssertionError("Laya asked")):
            self.route(self.task(), engine=DecisionEngine(self.workspace, self.config, backend))
        [log] = self.logs()
        self.assertIsNone(log["decision_id"])
        self.assertEqual([(c["key"], c["propensity"]) for c in log["candidates"]], [("codex", 1.0)])
        self.assertFalse([e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "decision"])

    def test_epsilon_explores_read_only_work_with_mixed_propensities(self):
        self.config["decisions"]["routing_epsilon"] = .5
        rng = Rng(.1, 1)
        task = self.route(self.task(), rng)
        self.assertEqual(task["agent"], "claude")
        [log] = self.logs()
        self.assertTrue(log["explored"])
        chances = {c["key"]: c["propensity"] for c in log["candidates"]}
        self.assertEqual(chances, {"codex": .75, "claude": .25})
        self.assertAlmostEqual(sum(chances.values()), 1.0)
        application = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "application"][-1]
        self.assertEqual(application["actual"], "claude")
        self.assertIn("epsilon exploration", application["reason"])
        self.route(self.task(), Rng(.9, 1))
        log = self.logs()[-1]
        self.assertEqual((log["chosen"], log["explored"]), ("codex", False))
        self.assertEqual({c["key"]: c["propensity"] for c in log["candidates"]}, {"codex": .75, "claude": .25})

    def test_epsilon_never_applies_to_writers_reviews_pins_or_qualified_advice(self):
        self.config["decisions"]["routing_epsilon"] = 1
        for task in (self.task(write=True), self.task(role="review")):
            self.route(task, Untouchable())
            log = self.logs()[-1]
            self.assertEqual((log["policy"]["epsilon"], log["explored"]), (0.0, False))
            self.assertEqual(sorted(c["propensity"] for c in log["candidates"]), [0.0, 1.0])
        review = self.task()
        review["prefer_different_agent"] = "claude"
        self.route(review, Untouchable())
        self.assertEqual(self.logs()[-1]["policy"]["epsilon"], 0.0)
        before = len(self.logs())
        pinned = self.route(self.task("claude"), Untouchable())
        self.assertEqual(pinned["agent"], "claude")
        self.assertEqual(len(self.logs()), before)
        engine = DecisionEngine(self.workspace, {**self.config, "decisions": {
            "mode": "active", "auto_actions": ["routing"], "calibration_file": "calibration.json", "routing_epsilon": 1}},
            Backend("claude"))
        original = engine.decide

        def decide(kind, state, questions, context):
            (self.workspace / "calibration.json").write_text(json.dumps({
                "schema": "fusion.calibration.v1", "model_identity": "fixture-model", "buckets": {
                    f"{kind}:{digest(questions)}:{key}": {"qualified": True, "temperature": 1, "threshold": .9} for key in questions}}))
            return original(kind, state, questions, context)
        with patch.object(engine, "decide", side_effect=decide):
            advised = self.route(self.task(), Untouchable(), engine)
        self.assertEqual(advised["agent"], "claude")
        log = self.logs()[-1]
        self.assertTrue(log["policy"]["laya_applied"])
        self.assertEqual({c["key"]: c["propensity"] for c in log["candidates"]}, {"codex": 0.0, "claude": 1.0})

    def test_named_route_arms_log_and_explore_within_the_route(self):
        self.config["routes"] = {"orc-free": {"agent": "claude", "command": "orc", "model_selector": "free", "arms": 2}}
        self.config["decisions"]["routing_epsilon"] = .5
        ranked = {("--free", "--tools"): ["free/top", "free/next"], ("--fit",): ["free/top", "free/next"]}
        task = self.task("claude", route="orc-free")
        with patch.object(core, "executable", return_value="/fixture/agent"), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]):
            self.route(task, Rng(0, 1))
        self.assertEqual((task["route"], task["settings_overrides"]), ("orc-free", {"model": "free/next"}))
        log = self.logs()[-1]
        self.assertEqual((log["scope"], log["chosen"]), ("route_arms", "orc-free:free/next"))
        self.assertEqual({c["key"]: c["propensity"] for c in log["candidates"]}, {"orc-free:free/top": .75, "orc-free:free/next": .25})

    def test_invalid_epsilon_and_mode_off(self):
        self.config["decisions"]["routing_epsilon"] = 1.5
        with self.assertRaisesRegex(ValueError, "routing_epsilon"):
            self.route(self.task())
        self.config["decisions"] = {"mode": "off", "routing_epsilon": 1}
        self.route(self.task(), Untouchable(), DecisionEngine(self.workspace, self.config))
        self.assertEqual(self.logs(), [])

    @staticmethod
    def log(task_id, chosen, chances, explored=False):
        return {"event": "routing_log", "task_id": task_id, "chosen": chosen, "explored": explored,
                "candidates": [{"key": key, "propensity": p} for key, p in chances.items()]}

    def test_report_ips_snips_and_ess_on_a_known_log(self):
        mixed = {"a": .75, "b": .25}
        events = [self.log("t1", "a", mixed), self.log("t2", "a", mixed), self.log("t3", "b", mixed, True),
                  self.log("t4", "a", mixed), self.log("t5", "a", mixed), self.log("t6", "b", mixed, True),
                  {"event": "outcome", "task_id": "t1", "accepted": False, "source": "lead"},
                  {"event": "outcome", "task_id": "t1", "accepted": True, "source": "lead"},
                  {"event": "outcome", "task_id": "t2", "accepted": False},
                  {"event": "outcome", "task_id": "t3", "accepted": True},
                  {"event": "outcome", "task_id": "t4", "accepted": True, "source": "lead"},
                  {"event": "outcome", "task_id": "t6", "accepted": True, "laya_veto": True}]
        report = routing_report(events)
        self.assertEqual((report["logged_choices"], report["with_outcome"], report["vetoed_outcomes_skipped"], report["explored"]),
                         (6, 4, 1, 2))
        self.assertEqual(report["outcome_sources"], {"lead": 2, "gate": 2})
        lanes = {lane["key"]: lane for lane in report["lanes"]}
        self.assertEqual((lanes["a"]["available"], lanes["a"]["chosen"], lanes["a"]["accepted"]), (4, 3, 2))
        self.assertAlmostEqual(lanes["a"]["ips_acceptance"], 2 / 3)
        self.assertAlmostEqual(lanes["a"]["snips_acceptance"], 2 / 3)
        self.assertAlmostEqual(lanes["a"]["ess"], 3.0)
        self.assertAlmostEqual(lanes["b"]["ips_acceptance"], 1.0)
        self.assertAlmostEqual(lanes["b"]["ess"], 1.0)
        self.assertEqual(lanes["b"]["overlap"], "ok")
        self.assertEqual(report["warnings"], ["1 logged routing choices have no outcome yet"])

    def test_report_flags_no_overlap_for_deterministic_logs(self):
        fixed = {"a": 1.0, "b": 0.0}
        events = [self.log("t1", "a", fixed), self.log("t2", "a", fixed),
                  {"event": "outcome", "task_id": "t1", "accepted": True}, {"event": "outcome", "task_id": "t2", "accepted": False}]
        lanes = {lane["key"]: lane for lane in routing_report(events)["lanes"]}
        self.assertEqual((lanes["a"]["overlap"], lanes["a"]["ips_acceptance"], lanes["a"]["ess"]), ("ok", .5, 2.0))
        self.assertEqual((lanes["b"]["overlap"], lanes["b"]["ips_acceptance"], lanes["b"]["zero_propensity"]),
                         ("insufficient overlap", None, 2))
        self.assertIn("insufficient overlap", routing_report(events)["warnings"][0])

    def test_cli_routing_report_reads_the_decision_log(self):
        store = DecisionStore(self.workspace)
        store.append("routing_log", task_id="t1", chosen="a", candidates=[{"key": "a", "propensity": 1.0}])
        store.append("outcome", task_id="t1", accepted=True, source="lead")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(core.main(["--workspace", str(self.workspace), "decisions", "routing-report"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual((report["with_outcome"], report["lanes"][0]["ips_acceptance"]), (1, 1.0))


if __name__ == "__main__":
    unittest.main()
