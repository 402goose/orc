import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core


class CodexRolloutQuotaTest(unittest.TestCase):
    def rollout(self, home, session_id, events):
        day = Path(home) / "sessions" / "2026" / "09" / "28"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-2026-09-28T00-00-00-{session_id}.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return path

    def test_latest_rate_limits_from_the_session_rollout(self):
        sid = "01a0e6ce-8466-7cf1-a0ab-95818e5b0143"
        with tempfile.TemporaryDirectory() as home:
            limits = lambda used: {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 1791142300}}}}
            self.rollout(home, sid, [{"type": "session_meta", "payload": {"id": sid}}, limits(30.0), limits(37.0)])
            quota = core.codex_rollout_quota(sid, {"CODEX_HOME": home})
        self.assertEqual(quota["windows"]["primary"]["used"], 0.37)
        self.assertEqual(quota["windows"]["primary"]["window_minutes"], 10080)

    def test_account_home_unknown_session_and_unsafe_ids(self):
        sid = "01a0e6ce-0000-7cf1-a0ab-95818e5b0143"
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as other:
            self.rollout(home, sid, [{"payload": {"rate_limits": {"primary": {"used_percent": 5.0, "resets_at": 1791142300}}}}])
            self.assertIsNone(core.codex_rollout_quota(sid, {"CODEX_HOME": other}))
            self.assertIsNone(core.codex_rollout_quota("missing-session", {"CODEX_HOME": home}))
            self.assertIsNone(core.codex_rollout_quota("../etc", {"CODEX_HOME": home}))
            self.assertIsNone(core.codex_rollout_quota(None, {"CODEX_HOME": home}))
            self.assertIsNotNone(core.codex_rollout_quota(sid, {"CODEX_HOME": home}))


if __name__ == "__main__":
    unittest.main()
