"""A worker that times out keeps the evidence its stream already carried (#210)."""
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
import fusion_progress

STREAM = [
    {"type": "system", "subtype": "init", "cwd": "/workspace", "session_id": "s", "model": "m",
     "permissionMode": "bypassPermissions", "apiKeySource": "none", "claude_code_version": "9.9.9"},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "cat secret.txt"}}]}},
    {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "tool_use_id": "toolu_1",
     "decision_reason_type": "asyncAgent", "message": "Permission for this tool use was denied."},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": "Permission for this tool use was denied."}]}},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
                                                     "unifiedWindows": {"five_hour": {"utilization": 0.42, "resetsAt": 4102444800}}}},
]


class TimeoutEvidenceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0",
                                      "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_timed_out_run_keeps_version_denials_and_quota(self):
        def timed_out(argv, **kwargs):
            exc = subprocess.TimeoutExpired(argv, 1)
            exc.output = "\n".join(json.dumps(e) for e in STREAM) + "\n"
            exc.stderr = ""
            raise exc
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {"command": sys.executable}})
        task = core.make_task(self.root, "claude", "x", "implementation", [], [], None, False, True)
        with patch.object(fusion_progress, "run_logged", side_effect=timed_out):
            value = core.dispatch(config, task, core.RunStore(self.root))
        self.assertEqual((value["exit_code"], value["status"]), (124, "blocked"))
        self.assertEqual(core.failure_class(value), "timeout")
        self.assertEqual((value["provider_version"], value["provider_permission_mode"]), ("9.9.9", "bypassPermissions"))
        self.assertEqual(value["denied"][0]["reason_type"], "asyncAgent")
        self.assertEqual(value["quota"]["windows"]["five_hour"]["used"], 0.42)
        [span] = core.RunStore(self.root).traces(5)
        self.assertEqual(span["quota"]["windows"]["five_hour"]["used"], 0.42)


if __name__ == "__main__":
    unittest.main()
