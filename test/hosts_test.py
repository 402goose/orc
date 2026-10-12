"""A host on every run and decision event, `fusion host-info`, and `fusion hosts` over a stubbed ssh."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fusion_core as core
import fusion_hosts
import fusion_runs
from fusion_decisions import DecisionStore, read_jsonl

LOCAL_SHA = "a" * 40
ANSWER = [{"type": "text", "sessionID": "s", "part": {"type": "text", "text": "STATUS: success\nSUMMARY: ok"}},
          {"type": "step_finish", "sessionID": "s", "part": {"reason": "stop", "tokens": {"input": 10, "output": 5}}}]

# Stands in for ssh: logs its argv, answers host-info for `same@*` and `skewed@*`, and
# fails like an unreachable host for anything else.
FAKE_SSH = """
import json, os, sys
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
destination = sys.argv[5]
if destination.startswith(("same@", "skewed@")):
    sha = {local!r} if destination.startswith("same@") else "b" * 40
    print(json.dumps({{"schema": "fusion.host_info.v1", "host": destination.split("@")[1], "orc_sha": sha,
                      "python": "3.12.0", "harnesses": {{"claude": "2.0.1 (Claude Code)", "codex": None, "agy": None,
                      "opencode": None}}, "load": [0.5, 0.4, 0.3], "disk_free_bytes": 10 ** 10, "runs_in_flight": 1,
                      "quota": {{}}}}))
    sys.exit(0)
print("ssh: connect to host " + destination.split("@")[-1] + " port 22: Connection refused", file=sys.stderr)
sys.exit(255)
"""


class HostsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.home = self.root / "orc"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.ssh_log = self.root / "ssh.log"
        self.env = {"ORC_HOME": str(self.home), "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0",
                    "FUSION_DECISIONS_MODE": "off", "FAKE_SSH_LOG": str(self.ssh_log),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "HOSTS_TEST_SECRET": "sentinel-secret-value"}
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_CONTROL_WORKSPACE", None)
        sha = patch.object(core, "orc_sha", return_value=LOCAL_SHA)
        sha.start()
        self.addCleanup(sha.stop)
        # No real harness runs: every command is a path that does not exist.
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, **{
            agent: {"command": str(self.root / f"missing-{agent}")} for agent in fusion_hosts.HARNESSES}})
        self.script("ssh", FAKE_SSH.format(local=LOCAL_SHA))

    def script(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(0o755)
        return path

    def registry(self, value):
        (self.home / "hosts.json").write_text(value if isinstance(value, str) else json.dumps(value))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(core, "load_config", return_value=(self.config, None)):
            try:
                code = core.main(["--workspace", str(self.workspace), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    # Records.

    def dispatch(self):
        worker = self.script("answer", f"import json\nfor e in {ANSWER!r}:\n    print(json.dumps(e))\n")
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "opencode": {"command": str(worker)},
                                                 "routes": {"oc": {"agent": "opencode", "model": "x/y"}}})
        task = core.make_task(self.workspace, "opencode", "Review", "reviewer", [], [], None, False, False, route="oc")
        return core.dispatch(config, task, core.RunStore(self.workspace))

    def test_a_new_run_and_its_decision_events_carry_the_registry_self(self):
        self.registry({"self": "laptop", "hosts": {"laptop": {"ssh": None}}})
        result = self.dispatch()
        run_dir = Path(result["artifacts"]["run_dir"])
        self.assertEqual(json.loads((run_dir / "task.json").read_text())["host"], "laptop")
        self.assertEqual(json.loads((run_dir / "result.json").read_text())["host"], "laptop")
        self.assertEqual(json.loads((run_dir / "trace.json").read_text())["host"], "laptop")
        self.assertEqual(result["host"], "laptop")
        core.record_outcome(self.workspace, result["run_id"], accepted=True, reason="checked the diff")
        events = read_jsonl(DecisionStore(self.workspace).path)
        self.assertTrue(events)
        self.assertEqual({event["host"] for event in events}, {"laptop"})
        self.assertEqual(core.RunStore(self.workspace).recent()[0]["host"], "laptop")

    def test_without_a_registry_records_carry_the_short_hostname(self):
        with patch("socket.gethostname", return_value="box.example.internal"):
            self.assertEqual(fusion_hosts.current_host(), "box")
            DecisionStore(self.workspace).append("routing_log", task_id="t")
        self.assertEqual(read_jsonl(DecisionStore(self.workspace).path)[0]["host"], "box")

    def test_a_malformed_registry_never_stops_a_record_being_tagged(self):
        self.registry("{not json")
        with patch("socket.gethostname", return_value="box.local"):
            self.assertEqual(fusion_hosts.current_host(), "box")

    def test_old_records_without_host_still_load_as_null(self):
        run_dir = core.RunStore(self.workspace).runs / "20260101-000000-old"
        run_dir.mkdir(parents=True)
        (run_dir / "task.json").write_text(json.dumps({"run_id": run_dir.name, "agent": "claude", "role": "implementation",
                                                       "created_at": core.now_ms()}))
        (run_dir / "result.json").write_text(json.dumps({"run_id": run_dir.name, "status": "success"}))
        row = core.RunStore(self.workspace).recent()[0]
        self.assertIsNone(row["host"])
        self.assertEqual(row["state"], "finished")
        (run_dir / "result.json").unlink()
        payload = fusion_runs.tombstone(self.workspace, run_dir.name)
        self.assertIsNone(payload["writes"]["result_json"]["host"])

    # host-info.

    def test_host_info_returns_every_field_and_null_for_a_missing_harness(self):
        self.registry({"self": "laptop", "hosts": {}})
        self.config["claude"]["command"] = str(self.script("fake-claude", "print('2.0.1 (Claude Code)')\n"))
        code, out, err = self.cli("--json", "host-info")
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertEqual(set(info), {"schema", "host", "orc_sha", "python", "harnesses", "load", "disk_free_bytes",
                                     "runs_in_flight", "quota"})
        self.assertEqual((info["host"], info["orc_sha"]), ("laptop", LOCAL_SHA))
        self.assertEqual(info["harnesses"], {"claude": "2.0.1 (Claude Code)", "codex": None, "agy": None, "opencode": None})
        self.assertEqual(len(info["load"]), 3)
        self.assertGreater(info["disk_free_bytes"], 0)
        self.assertEqual(info["runs_in_flight"], 0)
        self.assertEqual(info["quota"], {})
        self.assertNotIn("sentinel-secret-value", out)
        self.assertNotIn(str(self.root), out)
        # --json after the subcommand works too, and the text view prints.
        self.assertEqual(json.loads(self.cli("host-info", "--json")[1])["host"], "laptop")
        self.assertIn("claude: 2.0.1 (Claude Code)", self.cli("host-info")[1])

    def test_host_info_reports_quota_classifications_per_account(self):
        store = core.RunStore(self.workspace)
        store.root.mkdir(parents=True)
        span = {"agent": "claude", "lane_key": "claude", "end_time_ms": int(time.time() * 1000),
                "quota": {"status": "rejected", "windows": {"seven_day": {"used": 1.0, "resets_at": time.time() + 3600}}}}
        store.traces_path.write_text(json.dumps(span) + "\n")
        info = fusion_hosts.host_info(self.workspace, self.config)
        self.assertEqual(info["quota"], {"claude": "exhausted"})

    def test_a_slow_harness_times_out_quickly_and_reads_null(self):
        slow = self.script("slow", "import time\ntime.sleep(30)\n")
        started = time.monotonic()
        with patch.object(fusion_hosts, "VERSION_TIMEOUT_SECONDS", 0.5):
            self.assertIsNone(fusion_hosts.harness_version(str(slow)))
        self.assertLess(time.monotonic() - started, 5)
        self.assertLessEqual(fusion_hosts.VERSION_TIMEOUT_SECONDS, 5)
        self.assertIsNone(fusion_hosts.harness_version(str(self.root / "absent")))
        failing = self.script("failing", "import sys\nsys.exit(1)\n")
        self.assertIsNone(fusion_hosts.harness_version(str(failing)))

    # hosts.

    def hosts(self):
        code, out, err = self.cli("--json", "hosts")
        self.assertEqual(code, 0, err)
        return {row["host"]: row for row in json.loads(out)["hosts"]}

    def ssh_calls(self):
        return [json.loads(line) for line in self.ssh_log.read_text().splitlines()]

    def test_one_local_and_one_ssh_host_give_two_rows_and_skew_is_flagged(self):
        self.registry({"self": "laptop", "hosts": {"laptop": {"ssh": None},
                                                    "runner": {"ssh": "skewed@runner-host", "fusion": "~/orc/fusion"}}})
        rows = self.hosts()
        self.assertEqual(list(rows), ["laptop", "runner"])
        self.assertEqual((rows["laptop"]["reachable"], rows["laptop"]["skew"]), (True, False))
        self.assertEqual((rows["runner"]["reachable"], rows["runner"]["skew"]), (True, True))
        self.assertEqual(rows["runner"]["reported_host"], "runner-host")
        self.assertEqual(self.ssh_calls(), [["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "skewed@runner-host",
                                             "~/orc/fusion", "--json", "host-info"]])
        code, out, _ = self.cli("hosts")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"runner\s+yes\s+bbbbbbbbbbbb\s+skew")
        self.assertRegex(out, r"laptop\s+yes\s+aaaaaaaaaaaa\s+-")

    def test_a_host_on_the_same_commit_is_not_skewed(self):
        self.registry({"self": "laptop", "hosts": {"runner": {"ssh": "same@runner-host"}}})
        rows = self.hosts()
        self.assertFalse(rows["runner"]["skew"])
        self.assertEqual(self.ssh_calls()[0][5], "fusion")

    def test_an_offline_host_is_a_row_not_a_failure(self):
        self.registry({"self": "laptop", "hosts": {"runner": {"ssh": "skewed@runner-host"},
                                                    "offline": {"ssh": "down@offline-host"},
                                                    "unlisted": {"ssh": None}}})
        rows = self.hosts()
        self.assertEqual(list(rows), ["laptop", "runner", "offline"])
        self.assertFalse(rows["offline"]["reachable"])
        self.assertIsNone(rows["offline"]["skew"])
        self.assertIn("exit 255", rows["offline"]["error"])
        self.assertIn("Connection refused", rows["offline"]["error"])
        code, out, _ = self.cli("hosts")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"offline\s+no")
        self.assertIn("unreachable offline: exit 255", out)

    def test_an_ssh_that_cannot_run_is_unreachable(self):
        self.registry({"hosts": {"runner": {"ssh": "skewed@runner-host"}}})
        with patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            row = fusion_hosts.remote_info("runner", {"ssh": "skewed@runner-host"})
        self.assertFalse(row["reachable"])
        self.assertIn("cannot run ssh", row["error"])

    # The registry.

    def test_a_malformed_registry_is_refused_with_a_clear_message(self):
        cases = {
            "{not json": "not valid JSON",
            "[]": "must be a JSON object",
            json.dumps({"self": "a", "extra": 1}): "unknown key 'extra'",
            json.dumps({"self": 3}): "`self` must be a non-empty string",
            json.dumps({"hosts": []}): "`hosts` must be an object",
            json.dumps({"hosts": {"r": "user@host"}}): "host 'r' must be an object",
            json.dumps({"hosts": {"r": {"fusion": "x"}}}): "host 'r' needs `ssh`",
            json.dumps({"hosts": {"r": {"ssh": 5}}}): "host 'r': `ssh` must be a destination string",
            json.dumps({"hosts": {"r": {"ssh": "-oProxyCommand=x"}}}): "not starting with '-'",
            json.dumps({"hosts": {"r": {"ssh": "u@h", "fusion": ""}}}): "`fusion` must be a non-empty path",
            json.dumps({"hosts": {"r": {"ssh": "u@h", "port": 22}}}): "unknown key 'port'",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                self.registry(text)
                with self.assertRaises(fusion_hosts.HostsError) as caught:
                    fusion_hosts.load_registry()
                self.assertIn(str(self.home / "hosts.json"), str(caught.exception))
                self.assertIn(message, str(caught.exception))
                for command in ("hosts", "host-info"):
                    code, out, err = self.cli("--json", command)
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn(message, err)
        self.assertFalse(self.ssh_log.exists())

    def test_a_valid_or_absent_registry_loads(self):
        self.assertEqual(fusion_hosts.load_registry(), {"self": None, "hosts": {}})
        value = {"self": "laptop", "hosts": {"laptop": {"ssh": None},
                                             "runner": {"ssh": "user@runner-host", "fusion": "~/orc/fusion"}}}
        self.registry(value)
        self.assertEqual(fusion_hosts.load_registry(), value)


if __name__ == "__main__":
    unittest.main()
