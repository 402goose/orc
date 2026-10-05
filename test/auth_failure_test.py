"""A rejected login is account health, not model quality: it cools the account and is not counted as a failed run."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy

REFRESH_REUSED = "workspace routing discovery unauthorized (401)"


class AuthFailureTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.store = core.RunStore(self.root)
        self.config = core.deep_merge(core.DEFAULTS, {
            "decisions": {"mode": "off", "priors": False, "auto_routes": ["A", "A2", "B"]},
            "routes": {"A": {"agent": "codex", "account": "A", "model": "model-a"},
                       "A2": {"agent": "codex", "account": "A", "model": "model-a", "reasoning_effort": "low"},
                       "B": {"agent": "claude", "account": "B", "model": "model-b"}}})

    def span(self, route, provider_failure, age_s=60):
        result = {"status": "error", "exit_code": 1, "blockers": [provider_failure], "provider_failure": provider_failure}
        span = {"run_id": f"r-{route}-{age_s}", "agent": self.config["routes"][route]["agent"], "route": route,
                "status": "error", "exit_code": 1, "failure_class": core.failure_class(result),
                "end_time_ms": (time.time() - age_s) * 1000}
        self.store.root.mkdir(parents=True, exist_ok=True)
        with self.store.traces_path.open("a") as stream:
            stream.write(json.dumps(span) + "\n")

    def candidates(self):
        with patch.object(core, "executable", return_value=True):
            return [c["key"] for c in fusion_policy.route_candidates(self.config, {"workspace": str(self.root)}, self.store)]

    def test_a_401_is_classified_as_auth(self):
        result = {"status": "error", "exit_code": 1, "blockers": [REFRESH_REUSED], "provider_failure": REFRESH_REUSED}
        self.assertEqual(core.failure_class(result), "auth")
        self.assertEqual(core.failure_class({"status": "error", "blockers": ["Not logged in. Please log in"]}), "auth")

    def test_an_auth_failure_cools_every_lane_on_the_account(self):
        self.span("A", REFRESH_REUSED)
        self.assertEqual(self.candidates(), ["B"])

    def test_the_cooldown_expires(self):
        self.span("A", REFRESH_REUSED, age_s=core.LANE_COOLDOWN_SECONDS + 60)
        self.assertIn("A", self.candidates())

    def test_an_auth_failure_is_not_quality_evidence(self):
        self.span("A", REFRESH_REUSED, age_s=core.LANE_COOLDOWN_SECONDS + 60)
        with patch.object(core, "executable", return_value=True):
            ranked = {c["key"]: c for c in fusion_policy.route_candidates(self.config, {"workspace": str(self.root)}, self.store)}
        self.assertEqual((ranked["A"]["checked_runs"], ranked["A"]["acceptance_rate"]), (0, None))


if __name__ == "__main__":
    unittest.main()
