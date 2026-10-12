"""`fusion lanes`: one fusion.lane_offer.v1 record per lane, derived from existing settings; nothing reads it yet."""
import contextlib
import io
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
import fusion_lanes as lanes
import fusion_policy
import fusion_progress
from fusion_decisions import DecisionStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lane_offers"
WEEK = 7 * 24 * 3600


class LaneOffersTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off",
                                      "FUSION_CONFIG": str(FIXTURES / "config.json")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_CONTROL_WORKSPACE", None)
        self.store = core.RunStore(self.root)

    def config(self, agents=None, **routes):
        return core.deep_merge(core.DEFAULTS, {"routes": routes, **(agents or {})})

    def offer(self, route=None, agents=None):
        """The offer of one route named `lane`, or of a bare agent when `route` is a string."""
        if isinstance(route, str):
            config = self.config(agents)
            return next(item for item in lanes.lane_offers(config) if item["lane"] == route)
        return next(item for item in lanes.lane_offers(self.config(agents, lane=route)) if item["lane"] == "lane")

    def cli(self, *argv, config=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                patch.object(fusion_progress, "run_logged", side_effect=AssertionError("a worker was started")), \
                patch.object(core, "load_config", return_value=(config, None)) if config else contextlib.nullcontext():
            code = core.main(["--workspace", str(self.root), *argv])
        return code, out.getvalue()

    def test_the_fixture_config_matches_the_golden_offers(self):
        config = core.load_config(self.root)[0]
        golden = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))
        self.assertEqual(lanes.lane_offers(config), golden)
        self.assertEqual([item["lane"] for item in golden][-5:], ["claude", "codex", "opencode", "agy", "grok"])
        self.assertEqual(set(golden[0]), {"schema", "lane", "agent", "model", "reasoning_effort", "creator", "upstream", "billing",
                                          "endpoint", "features", "rates_usd_per_mtok", "cost_tier"})

    def test_upstream_is_the_endpoint_or_the_harness_provider(self):
        local = {"base_url": "http://127.0.0.1:8000/v1"}
        remote = {"base_url": "https://llm.example/v1", "region": "us"}
        cases = {
            "local endpoint": ({"agent": "opencode", "model": "local/m", "endpoint": local}, "self"),
            "host region": ({"agent": "codex", "model": "m", "endpoint": {**remote, "region": "host"}}, "self"),
            "remote endpoint": ({"agent": "opencode", "model": "private/m", "endpoint": remote}, "llm.example"),
            "orc": ({"agent": "claude", "command": "orc", "model_selector": "best"}, "openrouter"),
            "orc path": ({"agent": "claude", "command": "/opt/bin/orc"}, "openrouter"),
            "claude": ({"agent": "claude"}, "anthropic"),
            "codex": ({"agent": "codex"}, "openai"),
            "agy": ({"agent": "agy"}, "google"),
            "grok": ({"agent": "grok"}, "xai"),
            "opencode": ({"agent": "opencode", "model": "zai-coding/glm-5"}, "zai-coding"),
            "opencode without a provider": ({"agent": "opencode"}, None),
        }
        for name, (route, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.offer(route)["upstream"], expected)

    def test_endpoint_reports_host_and_data_class_only(self):
        offer = self.offer({"agent": "opencode", "model": "private/m",
                            "endpoint": {"base_url": "https://llm.example/v1", "api_key_env": "SOME_KEY", "retention": "none"}})
        self.assertEqual(offer["endpoint"], {"host": "llm.example", "data_class": "remote"})
        self.assertIsNone(self.offer({"agent": "codex"})["endpoint"])

    def test_creator_is_inferred_from_an_obvious_model_id(self):
        for model, expected in (("claude-sonnet-5", "anthropic"), ("anthropic/claude-sonnet-5", "anthropic"),
                                ("gpt-6.1-sol", "openai"), ("openrouter/openai/gpt-6", "openai"), ("gemini-3-pro", "google"),
                                ("gateway/kimi-k2.6", None), ("", None)):
            with self.subTest(model):
                self.assertEqual(self.offer({"agent": "opencode", "model": model})["creator"], expected)

    def test_billing(self):
        cases = {
            "api": ({"agent": "claude", "billing": "api"}, "api"),
            "subscription": ({"agent": "claude", "billing": "subscription"}, "subscription"),
            "default": ({"agent": "codex"}, "subscription"),
            "free model": ({"agent": "claude", "command": "orc", "model": "qwen/qwen3-coder:free"}, "free"),
            "free selector": ({"agent": "claude", "command": "orc", "model_selector": "free"}, "free"),
            "paid selector": ({"agent": "claude", "command": "orc", "model_selector": "best"}, "subscription"),
            "local endpoint": ({"agent": "opencode", "model": "local/m", "endpoint": {"base_url": "http://localhost:11434/v1"}}, "local"),
            "remote endpoint": ({"agent": "opencode", "model": "p/m", "endpoint": {"base_url": "https://llm.example/v1"}}, "subscription"),
            "metered local": ({"agent": "codex", "billing": "api", "endpoint": {"base_url": "http://127.0.0.1:1/v1"}}, "api"),
        }
        for name, (route, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.offer(route)["billing"], expected)

    def test_features(self):
        cases = {
            "writer": ({"agent": "codex", "sandbox": "workspace-write"}, ["write", "tools"]),
            "read-only sandbox": ({"agent": "codex", "sandbox": "read-only"}, ["tools"]),
            "plan permission": ({"agent": "claude", "permission_mode": "plan"}, ["tools"]),
            "plan mode": ({"agent": "agy", "mode": "plan"}, ["tools"]),
            "lacks write": ({"agent": "opencode", "model": "p/m", "lacks": ["write"]}, ["tools"]),
            "codex browser": ({"agent": "codex", "browser": True}, ["write", "browser", "tools"]),
            "codex browser table": ({"agent": "codex", "browser": {"chromium_args": []}}, ["write", "browser", "tools"]),
            "browser off": ({"agent": "codex", "browser": False}, ["write", "tools"]),
            "claude browser": ({"agent": "claude", "browser": True}, ["write", "browser", "tools"]),
            "opencode has no browser setting": ({"agent": "opencode", "model": "p/m", "browser": True}, ["write", "tools"]),
        }
        for name, (route, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.offer(route)["features"], expected)
        self.assertEqual(self.offer("codex", {"codex": {"browser": True}})["features"], ["write", "browser", "tools"])

    def test_rates_resolve_for_the_lane_model(self):
        by_model = {"gpt-6.1-sol": {"input": 2.0, "output": 10.0, "cache_read": 0.1},
                    "gpt-6-astra": {"input": 10.0, "output": 50.0}}
        agents = {"codex": {"price_per_mtok": by_model}}
        config = self.config(agents, sol={"agent": "codex", "model": "gpt-6.1-sol"}, astra={"agent": "codex", "model": "gpt-6-astra"},
                             other={"agent": "codex", "model": "gpt-7"},
                             flat={"agent": "claude", "price_per_mtok": {"input": 3, "output": 15}})
        offers = {item["lane"]: item for item in lanes.lane_offers(config)}
        self.assertEqual(offers["sol"]["rates_usd_per_mtok"], by_model["gpt-6.1-sol"])
        self.assertEqual(offers["astra"]["rates_usd_per_mtok"], by_model["gpt-6-astra"])
        self.assertIsNone(offers["other"]["rates_usd_per_mtok"])
        self.assertIsNone(offers["codex"]["rates_usd_per_mtok"])
        self.assertEqual(offers["flat"]["rates_usd_per_mtok"], {"input": 3.0, "output": 15.0})
        self.assertIsNone(offers["claude"]["rates_usd_per_mtok"])
        star = self.config({"codex": {"price_per_mtok": {**by_model, "*": {"input": 1.0}}}}, other={"agent": "codex", "model": "gpt-7"})
        self.assertEqual({item["lane"]: item for item in lanes.lane_offers(star)}["other"]["rates_usd_per_mtok"], {"input": 1.0})

    def test_model_effort_and_cost_tier_come_from_the_resolved_settings(self):
        offer = self.offer({"agent": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "high", "cost_tier": 1})
        self.assertEqual((offer["agent"], offer["model"], offer["reasoning_effort"], offer["cost_tier"]), ("codex", "gpt-6.1-sol", "high", 1))
        # An orc route does not inherit the native claude.model.
        self.assertIsNone(self.offer({"agent": "claude", "command": "orc"}, {"claude": {"model": "claude-sonnet-5"}})["model"])

    def test_an_offer_block_overrides_the_derived_values(self):
        offer = self.offer({"agent": "opencode", "model": "gateway/kimi-k2.6",
                            "offer": {"creator": "kimi", "upstream": "gateway-eu", "billing": "api", "features": ["tools"]}})
        self.assertEqual((offer["creator"], offer["upstream"], offer["billing"], offer["features"]),
                         ("kimi", "gateway-eu", "api", ["tools"]))
        self.assertIsNone(self.offer({"agent": "claude", "model": "claude-sonnet-5", "offer": {"creator": None}})["creator"])

    def test_an_unknown_offer_key_fails_config_load_and_names_the_route(self):
        cases = {"unknown": ({"creator": "openai", "price": 1}, "offer has unknown keys: price"),
                 "not an object": (["tools"], "offer must be an object"),
                 "billing": ({"billing": "free-ish"}, "offer.billing"),
                 "features": ({"features": "write"}, "offer.features"),
                 "creator": ({"creator": 7}, "offer.creator")}
        path = self.root / ".fusion.json"
        os.environ["FUSION_CONFIG"] = str(path)
        for name, (block, message) in cases.items():
            with self.subTest(name):
                path.write_text(json.dumps({"routes": {"my-lane": {"agent": "codex", "offer": block}}}))
                with self.assertRaisesRegex(ValueError, "route my-lane: " + message):
                    core.load_config(self.root)
        path.write_text(json.dumps({"routes": {"my-lane": {"agent": "codex", "offer": {"upstream": "azure"}}}}))
        self.assertEqual(core.load_config(self.root)[0]["routes"]["my-lane"]["offer"], {"upstream": "azure"})

    def test_lanes_json_round_trips(self):
        config = core.load_config(self.root)[0]
        code, out = self.cli("lanes", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), lanes.lane_offers(config))
        self.assertEqual(json.loads(json.dumps(json.loads(out))), json.loads(out))
        self.assertTrue(all("live" not in item for item in json.loads(out)))
        _, global_flag = self.cli("--json", "lanes")
        self.assertEqual(json.loads(global_flag), json.loads(out))

    def test_lanes_text_prints_one_line_per_lane(self):
        code, out = self.cli("lanes")
        self.assertEqual(code, 0)
        rows = {line.split()[0]: line for line in out.splitlines()}
        self.assertEqual(len(rows), len(out.splitlines()))
        self.assertEqual(list(rows), [item["lane"] for item in json.loads((FIXTURES / "golden.json").read_text())])
        self.assertIn("upstream=self  billing=local", rows["oc-local"])
        self.assertIn("data=local", rows["oc-local"])
        self.assertIn("gpt-6.1-sol/high", rows["codex-sol61-high"])
        self.assertIn("features=write,browser,tools", rows["codex-sol61-high"])
        self.assertNotIn("quota=", out)

    def write_quota(self, lane, used, reset):
        self.store.root.mkdir(parents=True, exist_ok=True)
        span = {"agent": "claude", "lane_key": lane, "end_time_ms": (time.time() - 60) * 1000,
                "quota": {"status": "allowed", "windows": {"seven_day": {"used": used, "resets_at": reset}}}}
        with self.store.traces_path.open("a") as stream:
            stream.write(json.dumps(span) + "\n")

    def test_live_adds_quota_and_availability_without_dispatching(self):
        self.write_quota("claude@api", 1.0, time.time() + WEEK / 2)
        self.write_quota("claude", 0.1, time.time() + WEEK / 2)
        traces = self.store.traces_path.read_bytes()
        decisions = DecisionStore(self.root).path
        before = decisions.read_bytes() if decisions.exists() else None
        on_path = {"claude", "codex", "orc"}
        with patch.object(core, "executable", side_effect=lambda command: command if command in on_path else None):
            code, out = self.cli("lanes", "--json", "--live")
            _, text = self.cli("lanes", "--live")
        self.assertEqual(code, 0)
        live = {item["lane"]: item["live"] for item in json.loads(out)}
        self.assertEqual(live["claude-api"]["quota"], "exhausted")
        self.assertFalse(live["claude-api"]["available"])
        self.assertIn("seven_day: used 1.000 exceeds hard 0.970", live["claude-api"]["reason"])
        self.assertEqual(live["claude-sub"], {"quota": "available", "available": True, "reason": None})
        self.assertEqual(live["codex-sol61-high"], {"quota": "unknown", "available": True, "reason": None})
        self.assertEqual(live["grok"], {"quota": "unknown", "available": False, "reason": "grok is not on PATH"})
        # The rest of each offer is the offline one.
        offline = {item["lane"]: item for item in lanes.lane_offers(core.load_config(self.root)[0])}
        self.assertEqual({item["lane"]: {k: v for k, v in item.items() if k != "live"} for item in json.loads(out)}, offline)
        self.assertIn("quota=exhausted  available=no (seven_day", {line.split()[0]: line for line in text.splitlines()}["claude-api"])
        self.assertEqual(self.store.traces_path.read_bytes(), traces)
        self.assertEqual(decisions.read_bytes() if decisions.exists() else None, before)

    def test_offer_blocks_change_no_routing_choice(self):
        base = core.deep_merge(core.DEFAULTS, {
            "decisions": {"mode": "off", "priors": False, "auto_routes": ["A", "B"]},
            "routes": {"A": {"agent": "claude", "account": "A", "model": "model-a", "cost_tier": 2},
                       "B": {"agent": "codex", "model": "model-b", "cost_tier": 1}}})
        offered = core.deep_merge(base, {"routes": {"A": {"offer": {"billing": "free", "features": ["write", "tools"]}},
                                                    "B": {"offer": {"creator": "x", "upstream": "y", "billing": "api"}}}})
        task = core.make_task(self.root, "auto", "routing preview", "implementation", [], [], None, False, True)
        with patch.object(core, "executable", return_value=True):
            plain = fusion_policy.explain_route(base, task, self.store, seed=3)
            with_offers = fusion_policy.explain_route(offered, task, self.store, seed=3)
        self.assertIsNotNone(plain["chosen"])
        self.assertEqual(with_offers, plain)


if __name__ == "__main__":
    unittest.main()
