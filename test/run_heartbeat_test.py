"""A run's heartbeat lock tells a live run from one that died, and a dead run can be tombstoned."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fusion_core as core
import fusion_runs
import fusion_trajectories
from fusion_decisions import DecisionStore, read_jsonl

ANSWER = [{"type": "text", "sessionID": "s", "part": {"type": "text", "text": "STATUS: success\nSUMMARY: ok"}},
          {"type": "step_finish", "sessionID": "s", "part": {"reason": "stop", "tokens": {"input": 10, "output": 5}}}]

# Dispatches one read-only task to a fixture worker, with a short heartbeat interval.
DISPATCHER = """
import sys
sys.path.insert(0, {root!r})
from pathlib import Path
import fusion_core as core, fusion_runs
fusion_runs.HEARTBEAT_SECONDS = {interval!r}
config = core.deep_merge(core.DEFAULTS, {{"decisions": {{"mode": "off"}}, "opencode": {{"command": {worker!r}}},
                                         "routes": {{"oc": {{"agent": "opencode", "model": "x/y"}}}}}})
task = core.make_task(Path({workspace!r}), "opencode", "Review", "reviewer", [], [], None, False, False, route="oc")
core.dispatch(config, task, core.RunStore(Path({workspace!r})))
"""


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def iso(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")


class RunHeartbeatTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.env = {"ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0",
                    "FUSION_DECISIONS_MODE": "off"}
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.runs = core.RunStore(self.workspace).runs

    def worker(self, name, body):
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(0o755)
        return path

    def answering_worker(self):
        return self.worker("answer", f"import json\nfor e in {ANSWER!r}:\n    print(json.dumps(e))\n")

    def run_dir(self, run_id, lock=None, activity=None, result=None, task=True):
        directory = self.runs / run_id
        directory.mkdir(parents=True)
        if task:
            (directory / "task.json").write_text(json.dumps({"run_id": run_id, "role": "implementation",
                                                             "agent": "claude", "created_at": core.now_ms()}))
        for name, value in ((fusion_runs.LOCK_NAME, lock), ("activity.json", activity), ("result.json", result)):
            if value is not None:
                (directory / name).write_text(json.dumps(value))
        return directory

    def lock(self, pid=None, heartbeat_age=0.0, started="2026-01-01T00:00:00+00:00", host=None):
        return {"pid": os.getpid() if pid is None else pid, "pgid": os.getpgid(0), "host": host or socket.gethostname(),
                "started_at": iso(time.time() - 60), "process_started_at": started, "fusion_sha": None,
                "heartbeat_at": iso(time.time() - heartbeat_age)}

    def dead_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(core, "load_config", return_value=(core.deep_merge(core.DEFAULTS, {}), None)):
            try:
                code = core.main(["--workspace", str(self.workspace), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def outcome_events(self, run_id):
        path = DecisionStore(self.workspace).path
        return [e for e in (read_jsonl(path) if path.exists() else [])
                if str(e.get("event", "")).startswith("outcome") and e.get("task_id") == run_id]

    def test_a_live_run_is_running_and_after_sigkill_it_is_abandoned(self):
        worker = self.worker("sleeper", "import time\ntime.sleep(60)\n")
        script = DISPATCHER.format(root=str(ROOT), interval=0.2, worker=str(worker), workspace=str(self.workspace))
        dispatcher = subprocess.Popen([sys.executable, "-c", script], env={**os.environ, **self.env},
                                      start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: dispatcher.poll() is None and os.killpg(dispatcher.pid, signal.SIGKILL))
        lock_path = wait_for(lambda: next(self.runs.glob(f"*/{fusion_runs.LOCK_NAME}"), None))
        run_dir = lock_path.parent
        # The worker runs in its own session; it is not in the killed group.
        activity = wait_for(lambda: fusion_runs._read(run_dir / "activity.json"))
        self.addCleanup(lambda: fusion_runs.pid_alive(activity["pid"]) and os.kill(activity["pid"], signal.SIGKILL))
        lock = json.loads(lock_path.read_text())
        self.assertEqual((lock["pid"], lock["pgid"], lock["host"]), (dispatcher.pid, dispatcher.pid, socket.gethostname()))
        self.assertTrue(set(lock) >= {"started_at", "process_started_at", "fusion_sha", "heartbeat_at"})
        wait_for(lambda: json.loads(lock_path.read_text())["heartbeat_at"] != lock["heartbeat_at"])
        state = fusion_runs.run_state(run_dir)
        self.assertEqual(state["state"], "running", state)
        self.assertIn(state["evidence"]["start_time_check"], ("match", "unavailable"))
        os.killpg(dispatcher.pid, signal.SIGKILL)
        dispatcher.wait()
        last = json.loads(lock_path.read_text())["heartbeat_at"]
        with patch.object(fusion_runs, "STALE_SECONDS", 0.5):
            time.sleep(0.7)
            state = fusion_runs.run_state(run_dir)
        self.assertEqual(state["state"], "abandoned")
        self.assertEqual(state["last_heartbeat"], last)
        self.assertEqual(state["evidence"]["pid_alive"], False)
        self.assertTrue(any("heartbeat stale" in reason for reason in state["reasons"]), state["reasons"])
        self.assertTrue(any(f"pid {dispatcher.pid} is not running" in reason for reason in state["reasons"]))
        self.assertFalse((run_dir / "result.json").exists())

    def test_a_reused_pid_with_another_start_time_is_abandoned(self):
        with patch.object(fusion_runs, "process_started_at", return_value="2026-03-01T09:00:00+00:00"):
            reused = fusion_runs.run_state(self.run_dir("reused", lock=self.lock(started="2026-02-01T09:00:00+00:00")))
            same = fusion_runs.run_state(self.run_dir("same", lock=self.lock(started="2026-03-01T09:00:00+00:00")))
        self.assertEqual(reused["state"], "abandoned")
        self.assertEqual(reused["evidence"]["start_time_check"], "mismatch")
        self.assertTrue(reused["evidence"]["pid_alive"])
        self.assertEqual(same["state"], "running")
        self.assertEqual(same["evidence"]["start_time_check"], "match")

    def test_a_reused_pid_is_caught_with_the_real_start_time(self):
        now = fusion_runs.process_started_at(os.getpid())
        if now is None:
            self.skipTest("this host does not expose process start times")
        self.assertEqual(fusion_runs.run_state(self.run_dir("mine", lock=self.lock(started=now)))["state"], "running")
        self.assertEqual(fusion_runs.run_state(self.run_dir("other", lock=self.lock(started="2001-01-01T00:00:00+00:00")))["state"],
                         "abandoned")

    def test_a_stale_heartbeat_or_dead_pid_is_abandoned_and_another_host_is_judged_by_heartbeat(self):
        with patch.object(fusion_runs, "process_started_at", return_value=None):
            stale = fusion_runs.run_state(self.run_dir("stale", lock=self.lock(heartbeat_age=600)))
            dead = fusion_runs.run_state(self.run_dir("dead", lock=self.lock(pid=self.dead_pid())))
            remote = fusion_runs.run_state(self.run_dir("remote", lock=self.lock(pid=1, host="elsewhere.invalid")))
        self.assertEqual((stale["state"], dead["state"], remote["state"]), ("abandoned", "abandoned", "running"))
        self.assertEqual(remote["evidence"]["start_time_check"], "other_host")

    def test_a_run_from_before_locks_is_unknown_never_running(self):
        directory = self.run_dir("legacy", activity={"pid": os.getpid(), "status": "running", "updated_at_ms": core.now_ms()})
        state = fusion_runs.run_state(directory)
        self.assertEqual(state["state"], "unknown")
        self.assertEqual((state["evidence"]["pid"], state["evidence"]["pid_alive"]), (os.getpid(), True))
        self.assertEqual(fusion_runs.run_state(self.run_dir("bare"))["state"], "unknown")
        self.assertEqual(fusion_runs.run_state(self.run_dir("done", result={"status": "success"}))["state"], "finished")
        code, out, _ = self.cli("--json", "runs")
        self.assertEqual(code, 0)
        self.assertEqual({row["task"]["run_id"]: row["state"] for row in json.loads(out)},
                         {"legacy": "unknown", "bare": "unknown", "done": "finished"})

    def test_tombstone_is_a_dry_run_refuses_live_and_finished_runs_and_writes_only_with_apply(self):
        live = self.run_dir("live", lock=self.lock())
        done = self.run_dir("done", lock=self.lock(), result={"status": "success"})
        dead = self.run_dir("dead", lock=self.lock(pid=self.dead_pid()), activity={"pid": 1, "status": "running"})
        legacy = self.run_dir("legacy", activity={"pid": self.dead_pid(), "status": "running"})
        before = {path: path.read_bytes() for path in self.runs.rglob("*") if path.is_file()}
        with patch.object(fusion_runs, "process_started_at", return_value=None):
            self.assertEqual(self.cli("runs", "tombstone", "live", "--apply")[0], 1)
        self.assertEqual(self.cli("runs", "tombstone", "done", "--apply")[0], 1)
        code, _, err = self.cli("runs", "tombstone", "legacy", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("--reason", err)
        self.assertEqual(self.cli("runs", "tombstone", "missing")[0], 4)
        code, out, _ = self.cli("runs", "tombstone", "dead")
        self.assertEqual(code, 0)
        self.assertIn("run dead: abandoned", out)
        self.assertIn("dry run", out)
        code, out, _ = self.cli("--json", "runs", "tombstone", "legacy")
        self.assertEqual(json.loads(out)["evidence"]["source"], "activity.json")
        self.assertTrue(json.loads(out)["needs_reason"])
        # Nothing was written or removed by refusals and dry runs.
        self.assertEqual({path: path.read_bytes() for path in self.runs.rglob("*") if path.is_file()}, before)
        self.assertEqual(self.outcome_events("dead"), [])
        self.assertFalse((live / "result.json").exists())

        code, out, _ = self.cli("--json", "runs", "tombstone", "dead", "--apply")
        self.assertEqual(code, 0, out)
        result = json.loads((dead / "result.json").read_text())
        self.assertEqual(result["status"], "abandoned")
        self.assertIn("tombstoned_at", result)
        self.assertEqual(result["evidence"]["start_time_check"], "pid_dead")
        self.assertEqual(result["last_heartbeat"], json.loads((dead / fusion_runs.LOCK_NAME).read_text())["heartbeat_at"])
        [event] = self.outcome_events("dead")
        self.assertEqual((event["event"], event["abandoned"], event["status"]), ("outcome_unmeasured", True, "abandoned"))
        code, _, _ = self.cli("runs", "tombstone", "legacy", "--apply", "--reason", "killed in the host restart")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((legacy / "result.json").read_text())["reason"], "killed in the host restart")
        # Every earlier file is still there and unchanged; only the two results are new.
        after = {path: path.read_bytes() for path in self.runs.rglob("*") if path.is_file()}
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(set(after) - set(before), {dead / "result.json", legacy / "result.json"})
        self.assertEqual(self.cli("runs", "tombstone", "dead", "--apply")[0], 1)
        self.assertEqual(json.loads((done / "result.json").read_text()), {"status": "success"})

    def test_a_tombstoned_run_takes_an_unmeasured_outcome_and_is_no_longer_pending(self):
        self.run_dir("dead", lock=self.lock(pid=self.dead_pid()))
        with patch.object(fusion_runs, "process_started_at", return_value=None):
            self.run_dir("live", lock=self.lock())
            self.assertEqual(self.cli("outcome", "dead", "--unmeasured", "--reason", "gone")[0], 4)
            payload = core.pending_outcomes(self.workspace)
        self.assertEqual([row["run_id"] for row in payload["abandoned"]], ["dead"])
        self.assertEqual(payload["unreported"] + payload["unmeasured"], [])
        self.assertEqual(self.cli("runs", "tombstone", "dead", "--apply")[0], 0)
        code, out, _ = self.cli("--json", "outcome", "dead", "--unmeasured", "--reason", "host incident")
        self.assertEqual(code, 0, out)
        with patch.object(fusion_runs, "process_started_at", return_value=None):
            code, out, _ = self.cli("--json", "outcome", "--pending")
        payload = json.loads(out)
        self.assertEqual((payload["unreported"], payload["unmeasured"], payload["abandoned"]), ([], [], []))
        # The fixture's live lock carries a made-up start time; with `ps` readable it would be a reused pid.
        with patch.object(fusion_runs, "process_started_at", return_value=None):
            code, out, _ = self.cli("outcome", "--pending")
        self.assertTrue(out.startswith("unreported 0, unmeasured 0, abandoned 0"), out)

    def test_routing_and_training_readers_skip_runs_without_a_result_by_state(self):
        self.run_dir("dead", lock=self.lock(pid=self.dead_pid()))
        self.run_dir("legacy", activity={"pid": self.dead_pid(), "status": "running"})
        self.run_dir("done", result={"status": "success", "role": "implementation"})
        self.run_dir("tombstoned", result={"status": "abandoned", "role": "implementation"})
        self.assertEqual([run_id for run_id, *_ in core.completed_runs(self.workspace)], ["done"])
        runs, _, excluded, _ = fusion_trajectories.collect(self.workspace, {}, "personal")
        self.assertEqual(runs, [])
        self.assertEqual((excluded["abandoned"], excluded["unknown"]), (2, 1))
        with patch.object(fusion_runs, "run_state", side_effect=AssertionError("asked")):
            with self.assertRaises(AssertionError):
                core.completed_runs(self.workspace)

    def test_the_heartbeat_stops_when_the_run_ends(self):
        worker = self.answering_worker()
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "opencode": {"command": str(worker)},
                                                 "routes": {"oc": {"agent": "opencode", "model": "x/y"}}})
        task = core.make_task(self.workspace, "opencode", "Review", "reviewer", [], [], None, False, False, route="oc")
        with patch.object(fusion_runs, "HEARTBEAT_SECONDS", 0.05):
            result = core.dispatch(config, task, core.RunStore(self.workspace))
            self.assertEqual(result["status"], "success", result)
            self.assertFalse([t for t in threading.enumerate() if t.name == "run-heartbeat"])
            run_dir = Path(result["artifacts"]["run_dir"])
            beat = (run_dir / fusion_runs.LOCK_NAME).read_text()
            time.sleep(0.2)
            self.assertEqual((run_dir / fusion_runs.LOCK_NAME).read_text(), beat)
        self.assertEqual(json.loads((run_dir / "result.json").read_text()), result)
        self.assertEqual(fusion_runs.run_state(run_dir), {"state": "finished"})
        # A run that fails after its run dir exists stops its heartbeat too.
        task = core.make_task(self.workspace, "opencode", "Review", "reviewer", [], [], None, False, False, route="oc")
        with patch.object(core, "agent_command", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            core.dispatch(config, task, core.RunStore(self.workspace))
        self.assertFalse([t for t in threading.enumerate() if t.name == "run-heartbeat"])
        self.assertTrue((self.runs / task["run_id"] / fusion_runs.LOCK_NAME).exists())

    def test_the_heartbeat_thread_does_not_keep_a_finished_process_alive(self):
        heartbeat = fusion_runs.Heartbeat(self.run_dir("beat"))
        self.addCleanup(heartbeat.stop)
        self.assertTrue(heartbeat.thread.daemon)
        worker = self.answering_worker()
        # The default 30 s interval: the process must exit as soon as dispatch returns.
        script = DISPATCHER.format(root=str(ROOT), interval=fusion_runs.HEARTBEAT_SECONDS, worker=str(worker),
                                   workspace=str(self.workspace))
        started = time.monotonic()
        completed = subprocess.run([sys.executable, "-c", script], env={**os.environ, **self.env},
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertLess(time.monotonic() - started, fusion_runs.HEARTBEAT_SECONDS)


if __name__ == "__main__":
    unittest.main()
