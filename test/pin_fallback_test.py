"""decisions.pin_fallback_routes: a pinned worker whose account cannot serve it
(quota or a rejected login) runs on the first eligible fallback route, a
different model by the owner's choice; without the setting a pin never moves
model."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_decisions import DecisionStore, read_jsonl
from fusion_policy import route_task


class PinFallbackTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.config = core.deep_merge(core.DEFAULTS, {"execution_mode": "yolo", "decisions": {
            "mode": "shadow", "auto_routes": ["sol", "opus"], "pin_fallback_routes": ["opus"]}, "routes": {
            "sol": {"agent": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "high"},
            "opus": {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"}}})
        self.config["claude"]["command"] = sys.executable
        self.config["codex"]["command"] = sys.executable
        self.store = core.RunStore(self.workspace)

    def pinned(self, spans=(), **task):
        task = core.make_task(self.workspace, task.pop("agent", "codex"), "Fix it", "implementation", [], [], None, False, True,
                              **{"settings_overrides": {"model": "gpt-6.1-sol", "reasoning_effort": "high"}, **task})
        with patch.object(self.store, "traces", return_value=list(spans)), patch.object(core, "executable", return_value=True), \
                patch("fusion_policy.DecisionEngine.decide", return_value={"id": "d", "status": "off", "recommendations": {}}):
            route_task(self.config, task, self.store)
        return task

    def failure(self, failure_class):
        return {"agent": "codex", "model": "gpt-6.1-sol", "failure_class": failure_class, "end_time_ms": core.now_ms()}

    def test_a_pinned_worker_out_of_quota_runs_on_the_fallback_route(self):
        task = self.pinned([self.failure("quota")])
        self.assertEqual((task["agent"], task["route"], task["settings_overrides"]), ("claude", "opus", {}))
        self.assertEqual((task["pin_fallback"]["from_model"], task["pin_fallback"]["to_model"]), ("gpt-6.1-sol", "claude-opus-5-5"))
        self.assertTrue(task["session_key"].endswith(":opus"))
        [log] = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("scope") == "pin_fallback"]
        self.assertEqual(log["chosen"], "opus")

    def test_a_rejected_login_moves_the_pin_too(self):
        self.assertEqual(self.pinned([self.failure("auth")])["route"], "opus")

    def test_a_pin_its_account_can_serve_stays_put(self):
        task = self.pinned([self.failure("worker_error")])
        self.assertEqual((task["agent"], task.get("route")), ("codex", None))
        self.assertNotIn("pin_fallback", task)

    def test_without_the_setting_a_pin_never_changes_model(self):
        self.config["decisions"]["pin_fallback_routes"] = []
        task = self.pinned([self.failure("quota")])
        self.assertEqual((task["agent"], task["settings_overrides"]["model"]), ("codex", "gpt-6.1-sol"))

    def test_a_quota_probe_and_a_fallback_on_the_same_account_never_move(self):
        probe = core.make_task(self.workspace, "codex", "probe", "quota-probe", [], [], None, False, False,
                               settings_overrides={"model": "gpt-6.1-sol", "reasoning_effort": "high"})
        probe["quota_probe"] = True
        with patch.object(self.store, "traces", return_value=[self.failure("quota")]), patch.object(core, "executable", return_value=True), \
                patch("fusion_policy.DecisionEngine.decide", return_value={"id": "d", "status": "off", "recommendations": {}}):
            route_task(self.config, probe, self.store)
        self.assertNotIn("pin_fallback", probe)
        self.config["decisions"]["pin_fallback_routes"] = ["sol"]
        self.assertNotIn("pin_fallback", self.pinned([self.failure("quota")]))


if __name__ == "__main__":
    unittest.main()
