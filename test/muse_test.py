"""Muse Code (`muse exec --json`) as a Fusion worker (issue #70)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_mcp
import fusion_policy

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "muse_exec_echo.jsonl"
HANDOFF = "STATUS: success\nSUMMARY: reviewed\nCHANGED: none\nTESTS: none\nBLOCKERS: none"


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


def event(payload_type, payload, session="11111111-2222-4333-8444-555555555555"):
    return {"schema_version": 1, "stream": {"kind": "session", "id": session},
            "payload_type": payload_type, "payload": payload}


def delta(text):
    return event("run.output.delta", {"kind": "run_output_delta", "text": text})


def terminal(kind="completed", text=HANDOFF, reason=None):
    return event(f"run.terminal.{kind}", {"kind": "run_terminal", "terminal": kind, "text": text, "reason": reason})


class MuseParseTest(unittest.TestCase):
    def test_real_echo_fixture_parses_terminal_text_and_session(self):
        session, answer, failure, usage, model, notes = core.parse_muse_output(FIXTURE.read_text())
        self.assertTrue(session)
        self.assertTrue(answer.startswith("echo: Return a compact handoff"))
        # Reminder child tasks fail in the fixture; only the run terminal decides.
        self.assertIsNone(failure)
        self.assertEqual(notes, [])

    def test_usage_model_and_command_evidence_and_unknown_events(self):
        output = stream(
            event("run.lifecycle.started", {"kind": "run_started", "model": "muse-spark-1.3"}),
            {"totally": "unknown"},
            event("tool.result", {"kind": "tool_result", "command": "make test", "exit_code": 2}),
            event("model.usage", {"kind": "model_usage", "usage": {
                "inputTokens": 100, "outputTokens": 40, "reasoningTokens": 10,
                "cacheReadTokens": 30, "cacheWriteTokens": 5, "cost": {"usd": 0.25, "partial": True}}}),
            delta(HANDOFF),
            terminal(),
        )
        session, answer, failure, usage, model, notes = core.parse_muse_output(output)
        self.assertEqual((session, answer, failure, model),
                         ("11111111-2222-4333-8444-555555555555", HANDOFF, None, "muse-spark-1.3"))
        self.assertEqual(usage, {"input_tokens": 100, "output_tokens": 40, "reasoning_output_tokens": 10,
                                 "cache_read_input_tokens": 30, "cache_creation_input_tokens": 5})
        self.assertEqual(notes, ["command exited 2: make test"])

    def test_failed_terminal_and_missing_terminal_are_failures(self):
        _, _, failure, _, _, _ = core.parse_muse_output(stream(terminal("failed", "", "provider exploded")))
        self.assertEqual(failure, "provider exploded")
        _, _, failure, _, _, _ = core.parse_muse_output(stream(delta("half an answer")))
        self.assertIn("without a terminal event", failure)

    def test_non_json_output_is_plain_text(self):
        self.assertEqual(core.parse_muse_output("plain answer\n")[1], "plain answer")


class MuseCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, write=True, mode="restricted", route=None, session=None, **settings):
        config = core.deep_merge(core.DEFAULTS, {"execution_mode": mode, "muse": settings})
        task = core.make_task(self.root, "muse", "Review", "reviewer", [], [], None, False, write, route=route)
        return core.agent_command(config, task, session)

    def test_writer_argv_matches_spec_and_brief_is_a_file(self):
        argv, _, metadata = self.command(model="muse-wonder-full-1.0", reasoning_effort="high")
        self.assertEqual(argv[:3], ["muse", "exec", "--json"])
        for flag, value in (("--model", "muse-wonder-full-1.0"), ("--reasoning-effort", "high"),
                            ("--approval-mode", "never"), ("--contributor-mode", "off"),
                            ("--sandbox-network", "proxy-only"), ("--workspace", str(self.root))):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        for flag in ("--no-foreign-personal-context", "--no-session-log"):
            self.assertIn(flag, argv)
        for flag in ("--yolo", "--disable-sandbox", "--disable-write", "--disable-shell"):
            self.assertNotIn(flag, argv)
        brief = Path(argv[argv.index("--prompt-file") + 1])
        self.assertIn("Fusion coding harness", brief.read_text())
        self.assertEqual(metadata["model"], "muse-wonder-full-1.0")

    def test_reader_cannot_write_or_shell_out_even_in_yolo_workspace(self):
        for mode in ("restricted", "yolo"):
            argv, _, _ = self.command(write=False, mode=mode, model="muse-spark-1.3")
            self.assertIn("--disable-write", argv)
            self.assertIn("--disable-shell", argv)
            self.assertNotIn("--yolo", argv)

    def test_yolo_writer_is_the_only_yolo(self):
        argv, _, _ = self.command(mode="yolo", model="muse-wonder-1.0")
        self.assertIn("--yolo", argv)
        self.assertNotIn("--approval-mode", argv)
        self.assertIn("--contributor-mode", argv)

    def test_worker_mcp_is_none_by_default_and_configurable(self):
        muse_home = self.root / "xdg" / "muse"
        muse_home.mkdir(parents=True)
        (muse_home / "settings.json").write_text(json.dumps(
            {"schema_version": 1, "mcpServers": {"alpha": {}, "beta": {}}}))
        playwright = {"transport": "stdio", "command": "playwright-mcp", "mode": "optional"}
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}):
            argv, _, _ = self.command(model="muse-spark-1.3", mcp_servers={"playwright": playwright})
        overrides = [argv[i + 1] for i, item in enumerate(argv) if item == "-c"]
        self.assertIn("mcpServers.alpha.enabled=false", overrides)
        self.assertIn("mcpServers.beta.enabled=false", overrides)
        added = next(item for item in overrides if item.startswith("mcpServers.playwright="))
        self.assertEqual(json.loads(added.split("=", 1)[1]), playwright)

    def test_api_key_comes_from_stdin_never_argv(self):
        with patch.dict(os.environ, {"MUSE_TEST_KEY": "sk-do-not-print"}):
            argv, _, metadata = self.command(model="muse-spark-1.3", api_key_env="MUSE_TEST_KEY")
        self.assertIn("--api-key-stdin", argv)
        self.assertNotIn("sk-do-not-print", json.dumps(argv) + json.dumps(metadata))
        self.assertEqual(metadata["api_key_env"], "MUSE_TEST_KEY")
        with self.assertRaisesRegex(ValueError, "api_key_env"):
            self.command(model="muse-spark-1.3", api_key_env="MUSE_MISSING_KEY")

    def test_session_resumes_by_id_and_logging_is_opt_in(self):
        session = "11111111-2222-4333-8444-555555555555"
        argv, _, _ = self.command(model="muse-spark-1.3", session=session, session_log=True)
        self.assertEqual(argv[argv.index("--session-id") + 1], session)
        self.assertNotIn("--no-session-log", argv)

    def test_default_routes_are_the_catalog_lanes(self):
        routes = core.DEFAULTS["routes"]
        expected = {"muse-wonder-full-high": "muse-wonder-full-1.0",
                    "muse-wonder-high": "muse-wonder-1.0", "muse-spark-high": "muse-spark-1.3"}
        for name, model in expected.items():
            route = routes[name]
            self.assertEqual((route["agent"], route["model"], route["reasoning_effort"]), ("muse", model, "high"))
            self.assertEqual((route["upstream"], route["creator"], route["billing"]), ("meta", "meta", "subscription"))
            self.assertNotIn("cost_tier", route)
        task = core.make_task(self.root, "muse", "Review", "reviewer", [], [], None, False, False,
                              route="muse-spark-high")
        argv, _, _ = core.agent_command(core.DEFAULTS, task, None)
        self.assertEqual(argv[argv.index("--model") + 1], "muse-spark-1.3")


class MuseLaneOfferTest(unittest.TestCase):
    def test_muse_lanes_offer_meta_upstream_and_subscription_billing(self):
        import fusion_lanes as lanes
        offers = {item["lane"]: item for item in lanes.lane_offers(core.DEFAULTS)}
        for lane, model in (("muse-wonder-full-high", "muse-wonder-full-1.0"),
                            ("muse-wonder-high", "muse-wonder-1.0"), ("muse-spark-high", "muse-spark-1.3")):
            offer = offers[lane]
            self.assertEqual((offer["schema"], offer["agent"], offer["model"]), ("fusion.lane_offer.v1", "muse", model))
            self.assertEqual((offer["creator"], offer["upstream"], offer["billing"]), ("meta", "meta", "subscription"))
            self.assertEqual(offer["reasoning_effort"], "high")
            self.assertIsNone(offer["cost_tier"])
            self.assertIn("tools", offer["features"])
        self.assertEqual(offers["muse"]["agent"], "muse")


class MuseRoutingTest(unittest.TestCase):
    def test_not_auto_routed_until_the_operator_opts_in(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = core.make_task(root, "auto", "fixture", "review", [], [], None, False, False)
            env = {"ORC_HOME": str(root / "orc"), "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"}
            # Minimal config (no orc selector routes to probe), as in the OpenCode routing test.
            config = {"sidekick": "codex", "decisions": {"mode": "off", "priors": False},
                      "routes": {name: dict(core.DEFAULTS["routes"][name])
                                 for name in ("muse-wonder-full-high", "muse-spark-high")}}
            rejected = {}
            with patch.dict(os.environ, env), patch.object(core, "executable", return_value=True):
                keys = {c["key"] for c in fusion_policy.route_candidates(config, task, core.RunStore(root), rejected=rejected)}
            self.assertNotIn("muse-wonder-full-high", keys)
            self.assertIn("not auto-routed", rejected["muse-wonder-full-high"])
            config["decisions"]["auto_routes"] = ["muse-wonder-full-high"]
            with patch.dict(os.environ, env), patch.object(core, "executable", return_value=True):
                keys = {c["key"] for c in fusion_policy.route_candidates(config, task, core.RunStore(root), rejected={})}
            self.assertIn("muse-wonder-full-high", keys)

    def test_lanes_show_upstream_meta_and_no_automatic_routing(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"ORC_HOME": str(Path(d) / "orc")}):
            lanes = {lane["route"] or lane["agent"]: lane for lane in fusion_mcp.orientation(Path(d))["lanes"]}
        lane = lanes["muse-spark-high"]
        self.assertEqual((lane["agent"], lane["model"], lane["upstream"], lane["creator"], lane["billing"]),
                         ("muse", "muse-spark-1.3", "meta", "meta", "subscription"))
        self.assertFalse(lane["automatic"])
        self.assertIn("muse", lanes)


class MuseDispatchTest(unittest.TestCase):
    def test_dispatch_records_session_usage_and_public_handoff(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            root = Path(d)
            worker = root / "muse-fixture"
            events = [event("run.lifecycle.started", {"kind": "run_started"}),
                      event("model.usage", {"kind": "model_usage", "model": "muse-spark-1.3",
                                            "usage": {"inputTokens": 9, "outputTokens": 4}}),
                      delta(HANDOFF), terminal()]
            worker.write_text(f"#!{sys.executable}\n" + f"""import json,sys
assert sys.argv[1:3]==['exec','--json'], sys.argv
assert '--contributor-mode' in sys.argv and sys.argv[sys.argv.index('--contributor-mode')+1]=='off'
assert '--disable-write' in sys.argv and '--disable-shell' in sys.argv
brief = open(sys.argv[sys.argv.index('--prompt-file')+1]).read()
assert 'Fusion coding harness' in brief
for event in {events!r}:
    print(json.dumps(event))
""")
            worker.chmod(0o755)
            config = core.deep_merge(core.DEFAULTS, {"muse": {"command": str(worker), "model": "muse-spark-1.3"},
                                                     "decisions": {"mode": "off"}})
            task = core.make_task(root, "muse", "Review fixture", "review", [], [], None, False, False)
            store = core.RunStore(root)
            result = core.dispatch(config, task, store)
            self.assertEqual(result["status"], "success", result)
            self.assertEqual((result["summary"], result["model"]), ("reviewed", "muse-spark-1.3"))
            self.assertEqual(result["usage"]["input_tokens"], 9)
            self.assertIn("11111111-2222-4333-8444-555555555555", store.sessions().values())


if __name__ == "__main__":
    unittest.main()
