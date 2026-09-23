import contextlib
import io
import json
import os
from pathlib import Path
import signal
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
import fusion_progress as progress
import fusion_build as build
from fusion_decisions import DecisionEngine, INTAKE_QUESTIONS


class ProgressTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.env = {**os.environ, "FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}
        self.env.pop("FUSION_PROGRESS", None)

    def fake_worker(self, body):
        worker = self.workspace / "codex-fixture"
        worker.write_text(f"#!{sys.executable}\n" + body)
        worker.chmod(0o755)
        (self.workspace / ".fusion.json").write_text(json.dumps({"codex": {"command": str(worker)}, "decisions": {"mode": "off"}}))
        return worker

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.02)
        self.fail("expected condition did not become true before deadline")

    def test_heartbeat_is_immediate_and_stops_with_the_phase(self):
        stream = io.StringIO()
        with progress.session(True, stream, interval=.03):
            with progress.activity("laya", "loading checkpoint"):
                self.assertIn("loading checkpoint", stream.getvalue())
                self.wait_for(lambda: "still active" in stream.getvalue())
            count = len(stream.getvalue())
            time.sleep(.08)
            self.assertEqual(len(stream.getvalue()), count)

    def test_public_worker_events_exclude_reasoning_commands_and_terminal_escapes(self):
        self.assertIsNone(progress.worker_message(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "private reasoning"}})))
        text = progress.worker_message(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "curl -H SECRET"}}))
        self.assertEqual(text, "running a command")
        text = progress.worker_message(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "\x1b[2JInspecting\nfiles"}}))
        self.assertEqual(text, "Inspecting files")
        self.assertIsNone(progress.worker_message("[]"))
        self.assertIsNone(progress.worker_message("not JSON"))

    def test_cli_streams_progress_and_logs_before_worker_exit_and_keeps_json_clean(self):
        release = self.workspace / "release"
        self.fake_worker(f"""import json, pathlib, sys, time
sys.stdin.read()
print(json.dumps({{'type':'thread.started','thread_id':'fixture'}}), flush=True)
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'Inspecting source files'}}}}), flush=True)
print('fixture diagnostic', file=sys.stderr, flush=True)
while not pathlib.Path({str(release)!r}).exists():
    time.sleep(.02)
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'STATUS: success\\nSUMMARY: inspected\\nBLOCKERS: none'}}}}), flush=True)
""")
        output, errors = self.workspace / "result.json", self.workspace / "progress.log"
        with output.open("w") as out, errors.open("w") as err:
            proc = subprocess.Popen([sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "--progress", "--json", "delegate", "--agent", "codex", "--read-only", "Inspect"], stdout=out, stderr=err, env=self.env)
            try:
                self.wait_for(lambda: "Inspecting source files" in errors.read_text())
                self.assertIsNone(proc.poll())
                self.assertEqual(output.read_text(), "")
                logs = list((self.workspace / ".fusion/runs").glob("*/stdout.log"))
                self.assertEqual(len(logs), 1)
                self.assertIn("thread.started", logs[0].read_text())
                self.assertIn("fixture diagnostic", logs[0].with_name("stderr.log").read_text())
                release.touch()
                self.assertEqual(proc.wait(timeout=5), 0)
            finally:
                release.touch()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        self.assertEqual(json.loads(output.read_text())["status"], "success")
        answer = Path(json.loads(output.read_text())["artifacts"]["answer"])
        self.assertIn("SUMMARY: inspected", answer.read_text())
        self.assertNotIn("Inspecting source files", answer.read_text())
        self.assertEqual(answer.stat().st_mode & 0o777, 0o600)
        self.assertIn("selected codex", errors.read_text())
        self.assertIn("worker success", errors.read_text())
        self.assertEqual(json.loads(logs[0].with_name("activity.json").read_text())["status"], "finished")

    def test_large_worker_input_and_both_outputs_do_not_deadlock_or_truncate_logs(self):
        script = "import sys; data=sys.stdin.read(); print(len(data)); print('x'*300000); print('z'*300000,file=sys.stderr)"
        result = progress.run_logged([sys.executable, "-c", script], cwd=self.workspace, env=self.env, input="p" * 200000,
                                     timeout=5, stdout_path=self.workspace / "stdout.log", stderr_path=self.workspace / "stderr.log", label="fixture")
        self.assertTrue(result.stdout.startswith("200000\n"))
        self.assertEqual(result.stdout.count("x"), 300000)
        self.assertEqual(result.stderr.count("z"), 300000)

    def test_timeout_keeps_partial_logs_and_stops_descendants(self):
        sentinel = self.workspace / "orphan-wrote"
        child = f"import pathlib,time; time.sleep(.6); pathlib.Path({str(sentinel)!r}).touch()"
        script = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); print('partial',flush=True); print('diagnostic',file=sys.stderr,flush=True); time.sleep(30)"
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            progress.run_logged([sys.executable, "-c", script], cwd=self.workspace, env=self.env, input=None, timeout=.2,
                                stdout_path=self.workspace / "stdout.log", stderr_path=self.workspace / "stderr.log", label="fixture")
        self.assertIn("partial", raised.exception.output)
        self.assertIn("diagnostic", raised.exception.stderr)
        time.sleep(.7)
        self.assertFalse(sentinel.exists())
        self.assertEqual(json.loads((self.workspace / "activity.json").read_text())["status"], "timed_out")

    def test_interrupt_cleans_up_workflow_worker_and_saves_interrupted_status(self):
        self.fake_worker("import sys,time; sys.stdin.read(); print('started',flush=True); time.sleep(30)\n")
        spec = self.workspace / "workflow.json"
        spec.write_text(json.dumps({"nodes": [{"id": "scan", "agent": "codex", "task": "Inspect", "write": False}]}))
        proc = subprocess.Popen([sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "--progress", "workflow", "run", str(spec)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True)
        try:
            self.wait_for(lambda: bool(list((self.workspace / ".fusion/runs").glob("*/activity.json"))))
            activity_file = next((self.workspace / ".fusion/runs").glob("*/activity.json"))
            pid = json.loads(activity_file.read_text())["pid"]
            proc.send_signal(signal.SIGINT)
            _, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 130, stderr)
            manifest = json.loads(next((self.workspace / ".fusion/workflows").glob("*/manifest.json")).read_text())
            self.assertEqual(manifest["status"], "interrupted")
            self.assertEqual(json.loads(activity_file.read_text())["status"], "interrupted")
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_laya_recommendation_and_actual_shadow_action_are_visible(self):
        class Backend:
            def predict(self, state, questions):
                return {"model_identity": "fixture", "answers": {"workflow": {"probabilities": {"discovery": .1, "build": .7, "debug": .1, "review": .1}}, "needs_clarification": {"noul": .1}}}
        output = io.StringIO()
        with patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "shadow"}), progress.session(True, output):
            engine = DecisionEngine(self.workspace, {}, Backend())
            record = engine.decide("intake", "Build exports", INTAKE_QUESTIONS)
            engine.applied(record, "review", False, "explicit choice")
        text = output.getvalue()
        self.assertIn("workflow=build (p=0.70)", text)
        self.assertIn("action=review; advisory only (explicit choice)", text)

    def test_watch_old_run_displays_blockers_without_dispatching(self):
        root = self.workspace / ".fusion/workflows/saved"
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({"status": "failed", "nodes": {"scan": {"status": "invalid", "agent": "codex", "attempts": 1,
                                                                                                 "result": {"summary": "scan completed", "blockers": ["verification missing"]}}}}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(core, "dispatch") as dispatch:
            self.assertEqual(progress.watch_workflow(self.workspace, "saved", once=True), 0)
        dispatch.assert_not_called()
        self.assertIn("scan [codex]: invalid", output.getvalue())
        self.assertIn("blocker: verification missing", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            progress.watch_workflow(self.workspace, as_json=True, once=True)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")

    def test_watch_attaches_before_intake_and_follows_build_into_workflow(self):
        old = self.workspace / ".fusion/workflows/old/manifest.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({"status": "failed", "started_at_ms": 1, "nodes": {}}))
        self.fake_worker("""import json, sys
sys.stdin.read()
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'STATUS: success\\nSUMMARY: inspected\\nTESTS: inspected fixture\\nBLOCKERS: none'}}), flush=True)
""")
        source_release, intake_release = self.workspace / "source-release", self.workspace / "intake-release"
        script = f"""import sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, {str(ROOT)!r})
import fusion_build as build
import fusion_core as core
from fusion_decisions import DecisionEngine
source, decide = build.source_for, DecisionEngine.decide
def gated_source(*args):
    while not Path({str(source_release)!r}).exists(): time.sleep(.02)
    return source(*args)
def gated_decide(self, kind, *args, **kwargs):
    if kind == 'intake':
        while not Path({str(intake_release)!r}).exists(): time.sleep(.02)
    return decide(self, kind, *args, **kwargs)
with patch.object(build, 'source_for', gated_source), patch.object(DecisionEngine, 'decide', gated_decide):
    raise SystemExit(core.main(['--workspace', {str(self.workspace)!r}, '--json', 'build', '--kind', 'discovery', '--execute', 'Inspect fixture']))
"""
        producer = subprocess.Popen([sys.executable, "-c", script], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        watcher = None
        watched = self.workspace / "watch.jsonl"
        try:
            self.wait_for(lambda: bool(list((self.workspace / ".fusion/builds").glob("*/status.json"))))
            state_path = next((self.workspace / ".fusion/builds").glob("*/status.json"))
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            os.utime(old, None)  # An older workflow writing later must not steal attachment.
            with watched.open("w") as output:
                watcher = subprocess.Popen([sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "--json", "workflow", "watch"], env=self.env, stdout=output, stderr=subprocess.PIPE, text=True)
                self.wait_for(lambda: '"phase": "intake"' in watched.read_text())
                pinned = io.StringIO()
                with contextlib.redirect_stdout(pinned):
                    progress.watch_workflow(self.workspace, "old", once=True, as_json=True)
                self.assertEqual(json.loads(pinned.getvalue())["workflow_id"], "old")
                source_release.touch()
                self.wait_for(lambda: '"phase": "classification"' in watched.read_text())
                intake_release.touch()
                result, errors = producer.communicate(timeout=10)
                self.assertEqual(producer.returncode, 0, errors)
                _, errors = watcher.communicate(timeout=10)
                self.assertEqual(watcher.returncode, 0, errors)
            frames = [json.loads(line) for line in watched.read_text().splitlines()]
            self.assertEqual(frames[-1]["status"], "success")
            self.assertEqual(frames[-1]["workflow_id"], json.loads(result)["workflow_id"])
            self.assertTrue(all(frame["workflow_id"] != "old" for frame in frames))
            self.assertEqual(json.loads(state_path.read_text())["workflow_id"], frames[-1]["workflow_id"])
        finally:
            for process in (producer, watcher):
                if process and process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_intake_errors_and_interruptions_remain_visible_to_watch(self):
        for error, expected in ((ValueError("issue unavailable"), "failed"), (KeyboardInterrupt(), "interrupted")):
            with self.subTest(expected=expected), patch.object(build, "source_for", side_effect=error):
                with self.assertRaises(type(error)):
                    build.prepare(self.workspace, {}, "Inspect modules", execute=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                progress.watch_workflow(self.workspace, once=True, as_json=True)
            snapshot = json.loads(output.getvalue())
            self.assertEqual(snapshot["status"], expected)
            self.assertIsNone(snapshot["workflow_id"])

    def test_dead_intake_coordinator_is_not_reported_as_running(self):
        state = {"build_id": "build-fixture", "status": "running", "phase": "classification",
                 "message": "waiting", "started_at_ms": 1, "coordinator_pid": 123}
        with patch.object(progress.os, "kill", side_effect=ProcessLookupError):
            snapshot = progress._build_snapshot(state)
        self.assertEqual(snapshot["status"], "interrupted")
        self.assertIn("no longer running", snapshot["message"])

    def test_workflow_startup_failure_does_not_leave_build_waiting(self):
        with patch.dict(os.environ, self.env):
            prepared = build.prepare(self.workspace, {}, "Inspect modules", execute=True)
        with patch("fusion_workflow.WorkflowRunner", side_effect=ValueError("invalid workflow")):
            with self.assertRaisesRegex(ValueError, "invalid workflow"):
                build.run_prepared(self.workspace, {}, prepared)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            progress.watch_workflow(self.workspace, prepared["build_id"], once=True, as_json=True)
        snapshot = json.loads(output.getvalue())
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["message"], "invalid workflow")

    def test_plan_only_build_does_not_replace_workflow_watch_target(self):
        old = self.workspace / ".fusion/workflows/saved/manifest.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({"status": "success", "nodes": {}}))
        with patch.dict(os.environ, self.env):
            prepared = build.prepare(self.workspace, {}, "Inspect modules")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            progress.watch_workflow(self.workspace, once=True, as_json=True)
        self.assertEqual(json.loads(output.getvalue())["workflow_id"], "saved")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            progress.watch_workflow(self.workspace, prepared["build_id"], once=True, as_json=True)
        self.assertEqual(json.loads(output.getvalue())["status"], "prepared")

    def test_quiet_overrides_environment_and_mcp_never_emits_progress(self):
        env = {**self.env, "FUSION_PROGRESS": "1"}
        args = [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace)]
        result = subprocess.run([*args, "--quiet", "build", "--plan-only", "Inspect modules"], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        json.loads(result.stdout)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        result = subprocess.run([*args, "--progress", "mcp-serve"], input=json.dumps(request) + "\n", env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["id"], 1)


if __name__ == "__main__":
    unittest.main()
