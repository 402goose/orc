import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core  # noqa: E402
from fusion_decisions import DecisionStore  # noqa: E402
from fusion_policy import rank_by_outcomes, route_candidates, route_task  # noqa: E402


class Clock:
    def __init__(self, value=1_700_000_000_000):
        self.value = value

    def __call__(self):
        return self.value


class CacheRoutingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0",
                                      "ORC_HOME": str(self.root / "orc-home")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_READ_ONLY", None)
        self.calls = self.root / "calls.jsonl"
        codex = self.root / "codex-fake"
        codex.write_text(f"""#!/usr/bin/env python3
import json, pathlib, sys, uuid
argv = sys.argv[1:]
pathlib.Path({str(self.calls)!r}).open('a').write(json.dumps(argv) + '\\n')
sys.stdin.read()
thread = argv[argv.index('resume') + 1] if 'resume' in argv else 'thread-' + uuid.uuid4().hex[:6]
print(json.dumps({{'type': 'thread.started', 'thread_id': thread}}))
print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': 'STATUS: success\\nSUMMARY: done'}}}}))
print(json.dumps({{'type': 'turn.completed', 'usage': {{'input_tokens': 1000, 'cached_input_tokens': 900, 'output_tokens': 5}}}}))
""", encoding="utf-8")
        codex.chmod(0o755)
        self.config = {"codex": {"command": str(codex)}, "claude": {"command": "missing-cache-test-agent"},
                       "agy": {"command": "missing-cache-test-agent"}, "grok": {"command": "missing-cache-test-agent"},
                       "timeout_seconds": 30}
        self.store = core.RunStore(self.workspace)
        self.clock = Clock()
        clock = patch.object(core, "now_ms", self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def dispatch(self, config=None):
        task = core.make_task(self.workspace, "codex", "do work", "implementation", [], [], None, True, False)
        result = core.dispatch(config or self.config, task, self.store)
        return result, self.store.traces(limit=1)[0], json.loads(self.calls.read_text().splitlines()[-1])

    def test_cache_read_ratio_handles_both_usage_conventions(self):
        self.assertEqual(core.cache_read_ratio({"input_tokens": 96, "cache_read_input_tokens": 3_380_000,
                                                "cache_creation_input_tokens": 79_000}), round(3_380_000 / 3_459_096, 4))
        self.assertEqual(core.cache_read_ratio({"input_tokens": 1000, "cached_input_tokens": 900}), 0.9)
        self.assertIsNone(core.cache_read_ratio({"input_tokens": 10, "output_tokens": 2}))
        self.assertIsNone(core.cache_read_ratio({"input_tokens": 0, "cache_read_input_tokens": 0}))
        self.assertIsNone(core.cache_read_ratio(None))

    def test_spans_record_session_resume_idle_and_cache_ratio(self):
        first, span, argv = self.dispatch()
        self.assertNotIn("resume", argv)
        self.assertEqual((span["session_key"], span["resumed"], span["session_idle_s"], span["cache_read_ratio"]),
                         ("codex:implementation", False, None, 0.9))
        self.assertEqual((first["resumed"], first["session_idle_s"]), (False, None))
        self.assertNotIn("resume_skipped", span)
        self.clock.value += 60_000
        second, span, argv = self.dispatch()
        self.assertIn("resume", argv)
        self.assertEqual((span["resumed"], span["session_idle_s"]), (True, 60.0))
        self.assertEqual(second["session_idle_s"], 60.0)
        self.assertEqual(self.store.session_last_used("codex:implementation"), self.clock.value)
        self.assertEqual(json.loads(self.store.sessions_path.read_text())["codex:implementation"], argv[argv.index("resume") + 1])

    def test_cold_resume_fresh_skips_a_stale_session_and_default_keeps_resuming(self):
        self.dispatch()
        thread = self.store.sessions()["codex:implementation"]
        self.clock.value += 400_000
        _, span, argv = self.dispatch()
        self.assertEqual((argv[argv.index("resume") + 1], span["resumed"], span["session_idle_s"]), (thread, True, 400.0))
        self.assertNotIn("resume_skipped", span)
        fresh = {**self.config, "cache": {"ttl_seconds": 300, "cold_resume": "fresh"}}
        self.clock.value += 100_000
        _, span, argv = self.dispatch(fresh)
        self.assertEqual((argv[argv.index("resume") + 1], span["resumed"]), (thread, True))
        self.clock.value += 301_000
        result, span, argv = self.dispatch(fresh)
        self.assertNotIn("resume", argv)
        self.assertEqual((span["resumed"], span["resume_skipped"], result["resume_skipped"]), (False, "cold", "cold"))
        self.assertNotEqual(self.store.sessions()["codex:implementation"], thread)
        with self.assertRaises(ValueError):
            core.cache_settings({"cache": {"cold_resume": "sometimes"}})

    def test_legacy_sessions_file_still_resumes_without_use_records(self):
        self.store.root.mkdir(parents=True)
        self.store.sessions_path.write_text(json.dumps({"codex:implementation": "thread-old"}))
        _, span, argv = self.dispatch({**self.config, "cache": {"cold_resume": "fresh"}})
        self.assertEqual((argv[argv.index("resume") + 1], span["resumed"], span["session_idle_s"]), ("thread-old", True, None))
        self.assertEqual(self.store.sessions(), {"codex:implementation": "thread-old"})
        self.assertIsNotNone(self.store.session_last_used("codex:implementation"))

    def test_warm_breaks_ties_only_within_epsilon(self):
        def lane(key, accepted, checked, warm):
            return {"key": key, "checked_runs": checked, "acceptance_rate": accepted / checked, "warm": warm}
        tied = [lane("cold", 8, 10, False), lane("warm", 8, 10, True)]
        self.assertEqual([c["key"] for c in rank_by_outcomes(tied, 3)], ["cold", "warm"])
        self.assertEqual([c["key"] for c in rank_by_outcomes(tied, 3, 0.05)], ["warm", "cold"])
        near = [lane("cold", 8, 10, False), lane("warm", 15, 20, True)]
        self.assertEqual([c["key"] for c in rank_by_outcomes(near, 3, 0.05)], ["warm", "cold"])
        better = [lane("verified", 9, 10, False), lane("warm", 6, 10, True)]
        self.assertEqual([c["key"] for c in rank_by_outcomes(better, 3, 0.05)], ["verified", "warm"])
        self.assertEqual([c["key"] for c in rank_by_outcomes(better, 3, 0.0)], ["verified", "warm"])
        explore = [lane("proven", 5, 5, True), {"key": "new", "checked_runs": 0, "acceptance_rate": None, "warm": False}]
        self.assertEqual([c["key"] for c in rank_by_outcomes(explore, 3, 0.05)], ["new", "proven"])

    def candidate_config(self):
        return {**self.config, "claude": {"command": sys.executable}, "codex": {"command": sys.executable},
                "decisions": {"mode": "shadow", "rank_by_outcomes": 3}, "routes": {}}

    def outcome_spans(self, now):
        spans = []
        for agent, end, idles in (("codex", now - 3_600_000, [None, 30.0, 900.0]), ("claude", now - 60_000, [10.0, 20.0, None])):
            for n, idle in enumerate(idles):
                spans.append({"agent": agent, "run_id": f"{agent}-{n}", "status": "success", "end_time_ms": end - n,
                              "session_idle_s": idle, "usage": {"cost_usd": 1.0 if idle is not None and idle < 300 else 3.0}})
                DecisionStore(self.workspace).append("outcome", task_id=f"{agent}-{n}", accepted=True)
        spans.append({"agent": "codex", "run_id": "legacy", "status": "success", "end_time_ms": 0, "usage": {"cost_usd": 50.0}})
        return spans

    def test_candidates_carry_warmth_and_split_costs(self):
        spans = self.outcome_spans(self.clock.value)
        task = core.make_task(self.workspace, "auto", "work", "implementation", [], [], None, False, False)
        with patch.object(self.store, "traces", return_value=spans):
            lanes = {c["key"]: c for c in route_candidates(self.candidate_config(), task, self.store)}
        self.assertEqual((lanes["claude"]["warm"], lanes["claude"]["session_idle_s"]), (True, 60.0))
        self.assertEqual((lanes["codex"]["warm"], lanes["codex"]["session_idle_s"]), (False, 3600.0))
        self.assertEqual((lanes["codex"]["mean_cost_usd_warm"], lanes["codex"]["mean_cost_usd_cold"]), (1.0, 3.0))
        self.assertEqual(lanes["codex"]["mean_cost_usd"], 57.0 / 4)
        self.assertEqual((lanes["claude"]["mean_cost_usd_warm"], lanes["claude"]["mean_cost_usd_cold"]), (1.0, 3.0))

    def test_route_task_uses_warmth_only_with_cache_config_and_shows_it_to_laya(self):
        spans = self.outcome_spans(self.clock.value)
        engine = MagicMock()
        engine.options = {"mode": "shadow"}
        engine.decide.return_value = {"id": "routing-1"}
        engine.allowed.return_value = False
        chosen = {}
        for label, extra in (("default", {}), ("cache", {"cache": {"ttl_seconds": 300}})):
            task = core.make_task(self.workspace, "auto", "work", "implementation", [], [], None, False, False)
            with patch("fusion_policy.DecisionEngine", return_value=engine), patch.object(self.store, "traces", return_value=spans):
                route_task({**self.candidate_config(), **extra}, task, self.store)
            chosen[label] = task["agent"]
        self.assertEqual(chosen, {"default": "codex", "cache": "claude"})
        state = engine.decide.call_args.args[1]
        self.assertTrue(all("warm" in c and "session_idle_s" in c for c in state["candidates"]))
        self.assertEqual([c["key"] for c in state["candidates"]][:2], ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
