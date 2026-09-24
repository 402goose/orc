import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fusion_workflow import WorkflowRunner, resume_workflow  # noqa: E402


class AcceptanceReceiptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir()
        self.calls = Path(self.temp.name) / "calls.txt"
        self.worker = Path(self.temp.name) / "fake-codex"
        self.worker.write_text(
            "#!/usr/bin/env python3\nimport json, pathlib, sys\n"
            "sys.stdin.read()\n"
            f"pathlib.Path({str(self.calls)!r}).open('a').write('call\\n')\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message',"
            "'text':'STATUS: success\\nSUMMARY: fixture complete\\nTESTS: fixture worker\\nBLOCKERS: none'}}))\n"
            "print(json.dumps({'type':'turn.completed'}))\n",
            encoding="utf-8",
        )
        self.worker.chmod(0o755)
        environment = patch.dict(os.environ, {
            "FUSION_DECISIONS_MODE": "off",
            "FUSION_TELEMETRY": "0",
            "ORC_HOME": str(Path(self.temp.name) / "orc-home"),
            "ACCEPTANCE_TEST_SECRET": "must-not-be-snapshotted",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.config = {"codex": {"command": str(self.worker)}, "timeout_seconds": 5}

    def runner(self, checks):
        return WorkflowRunner(self.workspace, self.config, {
            "task": "Exercise coordinator acceptance evidence",
            "max_attempts": 1,
            "nodes": [{"id": "verify", "agent": "codex", "task": "Return a fixture handoff",
                       "acceptance": {"required_handoff": ["summary"], "checks": checks}}],
        })

    def assert_receipt(self, receipt, expected_status):
        self.assertEqual(receipt["status"], expected_status)
        self.assertEqual(receipt["cwd"], str(self.workspace.resolve()))
        self.assertGreaterEqual(receipt["finished_at_ms"], receipt["started_at_ms"])
        self.assertGreaterEqual(receipt["duration_ms"], 0)
        self.assertEqual(receipt["attempt"], 1)
        path = Path(receipt["artifacts"]["receipt"])
        self.assertEqual(json.loads(path.read_text()), receipt)
        for name in ("stdout", "stderr"):
            output = receipt["outputs"][name]
            content = Path(output["path"]).read_bytes()
            self.assertEqual(output["path"], receipt["artifacts"][name])
            self.assertEqual(output["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(output["bytes"], len(content))
        self.assertNotIn("must-not-be-snapshotted", path.read_text())

    def test_success_persists_command_output_in_node_and_manifest(self):
        command = [sys.executable, "-c", "import sys; print('verified'); print('diagnostic', file=sys.stderr)"]
        runner = self.runner([command])
        outcome = runner.run()
        self.assertEqual(outcome["status"], "success")
        receipt = outcome["nodes"][0]["result"]["acceptance_checks"][0]
        self.assert_receipt(receipt, "passed")
        self.assertEqual(receipt["argv"], command)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertFalse(receipt["timed_out"])
        self.assertEqual(Path(receipt["artifacts"]["stdout"]).read_text(), "verified\n")
        self.assertEqual(Path(receipt["artifacts"]["stderr"]).read_text(), "diagnostic\n")
        node = json.loads(Path(outcome["nodes"][0]["artifact"]).read_text())
        manifest = json.loads(runner.manifest_path.read_text())
        self.assertEqual(node["acceptance"]["checks"], [receipt])
        self.assertEqual(node["result"]["acceptance_checks"], [receipt])
        self.assertEqual(manifest["nodes"]["verify"]["result"]["acceptance_checks"], [receipt])

    def test_nonzero_and_launch_error_keep_evidence_and_fail_closed(self):
        for command, status in (
            ([sys.executable, "-c", "import sys; print('assertion context'); print('failure details', file=sys.stderr); sys.exit(7)"], "failed"),
            ([str(self.workspace / "missing-check")], "error"),
            ("not-an-argv-array", "error"),
            ([sys.executable, "bad\x00argument"], "error"),
        ):
            with self.subTest(status=status, command=command):
                outcome = self.runner([command]).run()
                self.assertEqual(outcome["status"], "failed")
                receipt = outcome["nodes"][0]["result"]["acceptance_checks"][0]
                self.assert_receipt(receipt, status)
                if status == "failed":
                    self.assertEqual(receipt["exit_code"], 7)
                    self.assertIn("failure details", Path(receipt["artifacts"]["stderr"]).read_text())
                else:
                    self.assertIsNone(receipt["exit_code"])
                    self.assertTrue(receipt["error"])
                saved = json.loads(Path(outcome["nodes"][0]["artifact"]).read_text())
                self.assertFalse(saved["acceptance"]["ok"])
                self.assertEqual(saved["acceptance"]["checks"], [receipt])

    def test_timeout_retains_partial_output_and_final_receipt(self):
        self.config["timeout_seconds"] = 1
        command = [sys.executable, "-c", "import sys,time; print('before timeout', flush=True); print('stderr before timeout', file=sys.stderr, flush=True); time.sleep(10)"]
        runner = self.runner([command])
        outcome = runner.run()
        self.assertEqual(outcome["status"], "failed")
        receipt = outcome["nodes"][0]["result"]["acceptance_checks"][0]
        self.assert_receipt(receipt, "timed_out")
        self.assertTrue(receipt["timed_out"])
        self.assertNotEqual(receipt["exit_code"], 0)
        self.assertIn("before timeout", Path(receipt["artifacts"]["stdout"]).read_text())
        self.assertIn("stderr before timeout", Path(receipt["artifacts"]["stderr"]).read_text())
        events = [json.loads(line) for line in runner.events_path.read_text().splitlines()]
        evidence_events = [event for event in events if event["type"].startswith("acceptance.check.")]
        self.assertEqual([event["type"] for event in evidence_events], ["acceptance.check.started", "acceptance.check.finished"])
        self.assertEqual(evidence_events[0]["receipt"], receipt["artifacts"]["receipt"])

    def test_resume_rechecks_preserve_original_receipts_without_worker_redispatch(self):
        runner = self.runner([[sys.executable, "-c", "print('restart evidence')"]])
        first = runner.run()
        original = first["nodes"][0]["result"]["acceptance_checks"][0]
        original_path = Path(original["artifacts"]["receipt"])
        original_bytes = original_path.read_bytes()
        resumed = resume_workflow(self.workspace, self.config, first["workflow_id"])
        self.assertEqual(resumed["status"], "success")
        latest = resumed["nodes"][0]["result"]["acceptance_checks"][0]
        self.assert_receipt(latest, "passed")
        self.assertNotEqual(latest["artifacts"]["receipt"], str(original_path))
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(self.calls.read_text().splitlines(), ["call"])
        saved = json.loads(Path(resumed["nodes"][0]["artifact"]).read_text())
        self.assertEqual(saved["acceptance"]["checks"], [latest])

    def test_output_is_file_backed_and_hashes_large_binary_output(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff' * (2 * 1024 * 1024)); sys.stderr.buffer.write(b'\\x00detail')"]
        runner = self.runner([command])
        node = runner.nodes["verify"]
        result = {"status": "success", "summary": "fake worker", "attempt": 1,
                  "acceptance_checks": [{"status": "passed", "argv": ["forged-worker-evidence"]}]}
        observed = []
        real_popen = subprocess.Popen

        def observe(*args, **kwargs):
            observed.append(kwargs)
            return real_popen(*args, **kwargs)

        with patch("fusion_workflow.subprocess.Popen", side_effect=observe):
            accepted, problems = runner._accept_node(node, result)
        self.assertTrue(accepted, problems)
        self.assertEqual(len(result["acceptance_checks"]), 1)
        receipt = result["acceptance_checks"][0]
        self.assert_receipt(receipt, "passed")
        self.assertEqual(receipt["outputs"]["stdout"]["bytes"], 2 * 1024 * 1024)
        self.assertNotEqual(observed[0]["stdout"], subprocess.PIPE)
        self.assertNotEqual(observed[0]["stderr"], subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
