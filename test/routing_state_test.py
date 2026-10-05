"""Laya's routing input fits its cap: every candidate's evidence, compact, and
the job itself, cut with a visible marker only when the candidates leave too
little room. Before, full candidate records filled the cap and the task, sorted
last, was the first thing cut."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fusion_decisions import DecisionEngine
from fusion_policy import routing_candidate, routing_state
from decisions_test import Backend


def verbose_candidate(key, model, effort):
    return {"key": key, "route": key, "agent": "claude", "model": model, "reasoning_effort": effort, "cost_tier": 3,
            "acceptance_rate": 0.8018, "acceptance_rate_local": 0.8076923076923077, "checked_runs": 55.5, "checked_runs_local": 52,
            "evidence_scope": "role", "local_class": "implementation", "mean_cost_usd": 2.416127105263158,
            "mean_cost_usd_cold": 2.48, "mean_cost_usd_warm": 1.84, "mean_ms": 613442.9578947368, "warm": False,
            "session_idle_s": 124678.344, "reported_success_rate": 0.2526, "runs": 95,
            "pooled": {"attempts": 98, "successes": 63, "lanes": [key, "api-" + key], "scope": "role", "cost_per_accepted": 2.79},
            "prior": {"class": "write", "generated_at": "2026-10-02T08:36:11Z", "gym_attempts": 7, "gym_successes": 5,
                      "key": f"claude:{model}:{effort}", "match": "lane", "source": "gym"},
            "quota": {"classification": "available", "lane_key": "claude", "observed_at": "2026-10-05T19:20:12.255000Z",
                      "reasons": ["within quota thresholds"], "source": "/Users/x/.fusion/runs/20261005-121900-8c4debc4/trace.json",
                      "status": "allowed", "thresholds": {"hard": 0.97, "pace_margin": 1.0, "soft": 0.97},
                      "windows": {"five_hour": {"active": True, "elapsed": 0.27, "resets_at": 1791241200.0, "used": 0.07},
                                  "seven_day": {"active": True, "elapsed": 0.55, "resets_at": 1791500400.0, "used": 0.55}}}}


CANDIDATES = [verbose_candidate(f"lane-{n}", f"model-{n}", "high") for n in range(8)]
QUESTIONS = {"route": {"type": "choice", "instructions": "Choose one.",
                       "criteria": {c["key"]: c["model"] for c in CANDIDATES}}}


class RoutingStateTest(unittest.TestCase):
    def test_a_candidate_keeps_only_rounded_evidence(self):
        self.assertEqual(routing_candidate(CANDIDATES[0]),
                         {"key": "lane-0", "model": "model-0", "reasoning_effort": "high", "cost_tier": 3, "warm": False,
                          "acceptance": 0.8, "checked": 55.5, "usd": 2.42, "minutes": 10.2, "idle_minutes": 2078.0,
                          "quota": "available"})

    def test_eight_full_candidates_and_a_long_task_fit_with_every_candidate_whole(self):
        task = {"task": "Read AGENT.md", "decision_context": "S-675: credentialed login. " + "Check the 401 path. " * 200,
                "write": True}
        state = routing_state(task, {}, CANDIDATES, 2200)
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True)
        self.assertLessEqual(len(encoded), 2200)
        self.assertEqual([c["key"] for c in state["candidates"]], [c["key"] for c in CANDIDATES])
        self.assertTrue(state["task"].startswith("S-675: credentialed login."))
        self.assertIn("[…truncated", state["task"])

    def test_a_short_task_is_kept_whole(self):
        state = routing_state({"task": "Fix the 401 path", "write": False, "budget_remaining_usd": 3.5}, {}, CANDIDATES[:2], 2200)
        self.assertEqual((state["task"], state["budget_remaining_usd"], state["write"]), ("Fix the 401 path", 3.5, False))

    def test_the_recorded_routing_decision_is_no_longer_truncated(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "shadow"}):
            engine = DecisionEngine(Path(directory), {"decisions": {"mode": "shadow"}}, backend=Backend({"route": "lane-3"}))
            task = {"task": "Read AGENT.md", "decision_context": "Check the 401 path. " * 200, "write": True}
            record = engine.decide("routing", routing_state(task, {}, CANDIDATES, 2200), QUESTIONS)
        self.assertEqual((record["status"], record["truncated"]), ("ok", False))
        self.assertEqual(record["recommendations"]["route"]["value"], "lane-3")


if __name__ == "__main__":
    unittest.main()
