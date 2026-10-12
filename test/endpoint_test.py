"""A route's `endpoint` block points a lane at a self-hosted or private model server."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy

KEY = "stub-secret-7f3a9c1e5b"
LOCAL = {"base_url": "http://127.0.0.1:8000/v1", "api_key_env": "ENDPOINT_TEST_KEY", "region": "host", "retention": "none"}


class StubServer:
    """An OpenAI-compatible chat server on a loopback port that records each request."""

    def __init__(self):
        requests = self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
                reply = json.dumps({"id": "c1", "object": "chat.completion", "model": body.get("model"),
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant", "content": "written by the stub"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


# Plays OpenCode: resolves the model's provider from OPENCODE_CONFIG_CONTENT,
# substitutes {env:NAME} as OpenCode does, asks the server and writes its answer.
OPENCODE_FIXTURE = """
import json, os, re, sys, urllib.request
args = sys.argv[1:]
model = args[args.index("-m") + 1]
workspace = args[args.index("--dir") + 1]
provider, model_id = model.split("/", 1)
config = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])["provider"][provider]
assert config["npm"] == "@ai-sdk/openai-compatible", config
assert model_id in config["models"], config
options = config["options"]
key = re.sub(r"\\{env:([A-Z0-9_]+)\\}", lambda m: os.environ[m.group(1)], options["apiKey"])
request = urllib.request.Request(options["baseURL"] + "/chat/completions", method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                                 data=json.dumps({"model": model_id, "messages": [{"role": "user", "content": args[-1]}]}).encode())
with urllib.request.urlopen(request, timeout=10) as response:
    answer = json.loads(response.read())["choices"][0]["message"]["content"]
with open(os.path.join(workspace, "hello.txt"), "w") as stream:
    stream.write(answer + "\\n")
handoff = "STATUS: success\\nSUMMARY: wrote hello.txt\\nCHANGED: hello.txt\\nTESTS: none\\nBLOCKERS: none"
for event in ({"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": handoff}},
              {"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "stop", "tokens": {"input": 10, "output": 5}, "cost": 0}}):
    print(json.dumps(event))
"""


class EndpointTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environment = patch.dict(os.environ, {
            "ORC_HOME": str(self.root / "orc-home"), "CODEX_HOME": str(self.root / "codex"),
            "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off",
            "ENDPOINT_TEST_KEY": KEY, "ANTHROPIC_API_KEY": "inherited-hosted-key"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("FUSION_CONFIG", None)

    def config(self, agents=None, **routes):
        # Agent settings go through the overlay: deep_merge shares the sub-dicts it leaves alone with DEFAULTS.
        return core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off", "priors": False}, "routes": routes, **(agents or {})})

    def task(self, agent, route, write=False):
        return core.make_task(self.root, agent, "Write hello.txt", "implementer", [], [], None, False, write, route=route)

    def test_opencode_write_task_reaches_a_stub_server_end_to_end(self):
        stub = StubServer()
        self.addCleanup(stub.stop)
        worker = self.root / "opencode-fixture"
        worker.write_text(f"#!{sys.executable}\n" + OPENCODE_FIXTURE)
        worker.chmod(0o755)
        workspace = self.root / "ws"
        workspace.mkdir()
        config = self.config({"opencode": {"command": str(worker)}}, **{"local-coder": {
            "agent": "opencode", "model": "local/coder-30b",
            "endpoint": {**LOCAL, "base_url": stub.base_url, "api": "openai"},
            "price_per_mtok": {"input": 0, "output": 0}}})
        task = core.make_task(workspace, "opencode", "Write hello.txt", "implementer", [], [], None, False, True, route="local-coder")
        store = core.RunStore(workspace)
        result = core.dispatch(config, task, store)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual((workspace / "hello.txt").read_text(), "written by the stub\n")
        self.assertEqual(len(stub.requests), 1)
        self.assertEqual(stub.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(stub.requests[0]["body"]["model"], "coder-30b")
        self.assertEqual(stub.requests[0]["authorization"], "Bearer " + KEY)
        expected = {"host": "127.0.0.1", "api": "openai", "region": "host", "retention": "none", "data_class": "local"}
        run_dir = Path(result["artifacts"]["run_dir"])
        self.assertEqual(json.loads((run_dir / "result.json").read_text())["endpoint"], expected)
        self.assertEqual(json.loads((run_dir / "task.json").read_text())["resolved"]["endpoint"], expected)
        self.assertEqual(json.loads((run_dir / "trace.json").read_text())["endpoint"], expected)
        # The key appears nowhere Fusion writes: not in the run, its trace or the decision log.
        written = [path for path in store.root.rglob("*") if path.is_file()]
        self.assertIn(run_dir / "stdout.log", written)
        for path in written:
            with self.subTest(path=path.relative_to(store.root)):
                self.assertNotIn(KEY.encode(), path.read_bytes())

    def test_opencode_provider_is_merged_under_the_model_prefix(self):
        config = self.config({"opencode": {"config": {"provider": {"other": {"npm": "x"}}}}},
                             local={"agent": "opencode", "model": "local/coder-30b", "endpoint": LOCAL})
        argv, env, _ = core.agent_command(config, self.task("opencode", "local"), None)
        content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(content["provider"]["local"], {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://127.0.0.1:8000/v1", "apiKey": "{env:ENDPOINT_TEST_KEY}"},
            "models": {"coder-30b": {}}})
        self.assertEqual(content["provider"]["other"], {"npm": "x"})
        self.assertEqual(argv[argv.index("-m") + 1], "local/coder-30b")
        self.assertNotIn(KEY, " ".join(argv) + env["OPENCODE_CONFIG_CONTENT"])

    def test_claude_sets_the_base_url_and_token_from_the_named_variable(self):
        block = {"base_url": "https://llm.internal.example/anthropic", "api": "anthropic", "api_key_env": "ENDPOINT_TEST_KEY"}
        config = self.config(private={"agent": "claude", "model": "private-model", "endpoint": block})
        argv, env, _ = core.agent_command(config, self.task("claude", "private"), None)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://llm.internal.example/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], KEY)
        # A hosted key in the parent shell never reaches the private server.
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn(KEY, " ".join(argv))
        block.pop("api_key_env")
        _, env, _ = core.agent_command(config, self.task("claude", "private"), None)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://llm.internal.example/anthropic")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_codex_adds_a_chat_provider_with_the_key_variable(self):
        config = self.config(local={"agent": "codex", "model": "coder-30b", "endpoint": LOCAL})
        argv, env, _ = core.agent_command(config, self.task("codex", "local"), None)
        provider = ('model_providers.fusion_endpoint={"name"="local","base_url"="http://127.0.0.1:8000/v1",'
                    '"env_key"="ENDPOINT_TEST_KEY","wire_api"="chat"}')
        self.assertEqual(argv[argv.index(provider) - 1], "-c")
        self.assertEqual(argv[argv.index('model_provider="fusion_endpoint"') - 1], "-c")
        self.assertLess(argv.index(provider), argv.index("exec"))
        self.assertEqual(argv[argv.index("-m") + 1], "coder-30b")
        self.assertNotIn(KEY, " ".join(argv))
        self.assertEqual(env["ENDPOINT_TEST_KEY"], KEY)

    def test_mismatches_are_refused_before_the_worker_starts(self):
        marker = self.root / "started"
        worker = self.root / "worker-fixture"
        worker.write_text(f"#!{sys.executable}\nopen({str(marker)!r}, 'w').close()\n")
        worker.chmod(0o755)
        anthropic = {**LOCAL, "api": "anthropic"}
        cases = {
            "claude": ({"agent": "claude", "model": "m", "endpoint": LOCAL}, 'api "anthropic"'),
            "codex": ({"agent": "codex", "model": "m", "endpoint": anthropic}, 'api "openai"'),
            "opencode": ({"agent": "opencode", "model": "local/m", "endpoint": anthropic}, 'api "openai"'),
            "grok": ({"agent": "grok", "endpoint": LOCAL}, "only on claude, codex and opencode"),
            "agy": ({"agent": "agy", "endpoint": LOCAL}, "only on claude, codex and opencode"),
            "orc": ({"agent": "claude", "command": "orc", "model": "m", "endpoint": anthropic}, "orc lane"),
            "unset-key": ({"agent": "opencode", "model": "local/m", "endpoint": {**LOCAL, "api_key_env": "ENDPOINT_TEST_UNSET"}},
                          "ENDPOINT_TEST_UNSET, which is not set"),
            "no-provider": ({"agent": "opencode", "endpoint": LOCAL}, "provider/model"),
        }
        for name, (route, message) in cases.items():
            with self.subTest(name=name):
                route["command"] = str(worker) if name != "orc" else "orc"
                config = self.config({agent: {"command": str(worker)} for agent in ("claude", "codex", "opencode", "grok", "agy")},
                                     lane=route)
                store = core.RunStore(self.root)
                with self.assertRaisesRegex(ValueError, message):
                    core.dispatch(config, self.task(route["agent"], "lane"), store)
                self.assertFalse(marker.exists())
                self.assertFalse(store.runs.exists() and any(store.runs.iterdir()))

    def test_malformed_blocks_are_refused_at_config_load(self):
        cases = {
            "unknown key": ({**LOCAL, "api_key": KEY}, "unknown keys: api_key"),
            "not an object": ("http://127.0.0.1:8000/v1", "endpoint must be an object"),
            "no url": ({"api": "openai"}, "base_url"),
            "ftp url": ({"base_url": "ftp://host/v1"}, "base_url"),
            "no host": ({"base_url": "http:///v1"}, "base_url"),
            "bad port": ({"base_url": "http://127.0.0.1:port/v1"}, "base_url"),
            "space": ({"base_url": "http://127.0.0.1:8000/ v1"}, "base_url"),
            "query key": ({"base_url": "http://127.0.0.1:8000/v1?key=" + KEY}, "base_url"),
            "credentials": ({"base_url": f"https://user:{KEY}@llm.example/v1"}, "credentials"),
            "api": ({**LOCAL, "api": "gemini"}, "endpoint.api"),
            "literal key": ({**LOCAL, "api_key_env": "sk-" + KEY}, "api_key_env"),
            "retention": ({**LOCAL, "retention": "forever"}, "retention"),
            "region": ({**LOCAL, "region": ""}, "region"),
        }
        for name, (block, message) in cases.items():
            with self.subTest(name=name):
                path = self.root / ".fusion.json"
                path.write_text(json.dumps({"routes": {"local": {"agent": "opencode", "model": "local/m", "endpoint": block}}}))
                with self.assertRaisesRegex(ValueError, "route local: .*" + message):
                    core.load_config(self.root)
        (self.root / ".fusion.json").write_text(json.dumps({"routes": {"local": {"agent": "opencode", "model": "local/m",
                                                                                 "endpoint": LOCAL}}}))
        self.assertEqual(core.load_config(self.root)[0]["routes"]["local"]["endpoint"], LOCAL)

    def test_data_class_is_local_for_loopback_or_host_region(self):
        for url, region, data_class in (("http://127.0.0.1:8000/v1", None, "local"), ("http://[::1]:8000/v1", None, "local"),
                                        ("http://localhost:11434/v1", "us", "local"), ("https://gpu-box.lan/v1", "host", "local"),
                                        ("https://gpu-box.lan/v1", "gpu-box", "remote"), ("https://api.example.com/v1", None, "remote")):
            with self.subTest(url=url, region=region):
                record = core.endpoint_record({"endpoint": {"base_url": url, **({"region": region} if region else {})}})
                self.assertEqual(record["data_class"], data_class)
                self.assertEqual(record["api"], "openai")
        self.assertIsNone(core.endpoint_record({}))

    def test_route_explain_and_routing_report_show_the_data_class(self):
        config = self.config({"decisions": {"mode": "off", "priors": False, "auto_routes": ["local-coder", "hosted", "plain"]}}, **{
            "local-coder": {"agent": "opencode", "model": "local/coder-30b", "endpoint": LOCAL},
            "hosted": {"agent": "opencode", "model": "private/m", "endpoint": {"base_url": "https://llm.example/v1"}},
            "plain": {"agent": "opencode", "model": "openai/gpt-5.5"}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(core, "load_config", return_value=(config, None)), \
                patch.object(core, "executable", return_value=True):
            core.main(["--workspace", str(self.root), "--json", "route", "--explain"])
        lanes = {c["key"]: c for c in json.loads(out.getvalue())["candidates"]}
        self.assertEqual(lanes["local-coder"]["data_class"], "local")
        self.assertEqual(lanes["hosted"]["data_class"], "remote")
        self.assertNotIn("data_class", lanes["plain"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(core, "load_config", return_value=(config, None)), \
                patch.object(core, "executable", return_value=True):
            core.main(["--workspace", str(self.root), "route", "--explain"])
        text = {line.split()[0]: line for line in out.getvalue().splitlines()[1:]}
        self.assertIn("data=local", text["local-coder"])
        self.assertNotIn("data=", text["plain"])
        # The routing log keeps each candidate as routing ranked it, so the report shows data_class per lane.
        task = core.make_task(self.root, "auto", "Review", "reviewer", [], [], None, False, False)
        with patch.object(core, "executable", return_value=True):
            candidates = fusion_policy.route_candidates(config, task, core.RunStore(self.root))
        events = [{"event": "routing_log", "task_id": "t", "chosen": "local-coder",
                   "candidates": [{**c, "propensity": 1.0 if c["key"] == "local-coder" else 0.0} for c in candidates]},
                  {"event": "outcome", "task_id": "t", "accepted": True, "source": "lead"}]
        rows = {row["key"]: row for row in fusion_policy.routing_report(events)["lanes"]}
        self.assertEqual(rows["local-coder"]["data_class"], "local")
        self.assertEqual(rows["hosted"]["data_class"], "remote")
        self.assertNotIn("data_class", rows["plain"])
        events = [{"event": "routing_log", "task_id": "t", "chosen": "a", "candidates": [
                      {"key": "a", "agent": "opencode", "model": "m", "data_class": "local", "propensity": 1.0},
                      {"key": "b", "agent": "opencode", "model": "m", "data_class": "remote", "propensity": 0.0}]},
                  {"event": "outcome", "task_id": "t", "accepted": True, "source": "lead"}]
        self.assertEqual(fusion_policy.routing_report(events, by="family")["lanes"][0]["data_class"], "mixed")

    def test_routes_without_endpoint_record_nothing_new(self):
        config = self.config({"claude": {"command": "missing-endpoint-test-worker"}}, plain={"agent": "claude", "model": "m"})
        _, env, metadata = core.agent_command(config, self.task("claude", "plain"), None)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("endpoint", metadata)
        argv, _, _ = core.agent_command(self.config(plain={"agent": "codex"}), self.task("codex", "plain"), None)
        self.assertFalse(any("model_provider" in item for item in argv))
        result = core.dispatch(config, self.task("claude", "plain"), core.RunStore(self.root))
        self.assertNotIn("endpoint", result)
        self.assertNotIn("endpoint", json.loads(Path(result["artifacts"]["run_dir"], "task.json").read_text())["resolved"])


if __name__ == "__main__":
    unittest.main()
