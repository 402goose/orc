import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
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

    def runner(self, checks, *, write=False):
        return WorkflowRunner(self.workspace, self.config, {
            "task": "Exercise coordinator acceptance evidence",
            "max_attempts": 1,
            "nodes": [{"id": "verify", "agent": "codex", "write": write, "task": "Return a fixture handoff",
                       "acceptance": {"required_handoff": ["summary"], "checks": checks}}],
        })

    def test_writer_cannot_redirect_declared_hash_to_planted_argv_files(self):
        original = "raise SystemExit(1)\n"
        weakened = "from pathlib import Path\nPath('evaluator-ran').touch()\n"
        (self.workspace / "grade.py").write_text(original)
        edits = (f"pathlib.Path('python3').write_text({original!r})\n"
                 f"pathlib.Path('-B').write_text({original!r})\n"
                 f"pathlib.Path('grade.py').write_text({weakened!r})\n")
        self.worker.write_text(self.worker.read_text().replace("sys.stdin.read()\n", "sys.stdin.read()\n" + edits))
        check = {"argv": ["python3", "-B", "grade.py"], "sha256": hashlib.sha256(original.encode()).hexdigest()}
        runner = self.runner([check], write=True)
        outcome = runner.run()
        result = outcome["nodes"][0]["result"]
        self.assertEqual(outcome["status"], "failed", outcome)
        self.assertEqual(result["gate_codes"][0]["code"], "check_contract_changed")
        self.assertFalse((self.workspace / "evaluator-ran").exists())
        receipt = result["acceptance_checks"][0]
        self.assertEqual(Path(receipt["contract"]["path"]), self.workspace / "grade.py")
        self.assertIsNone(receipt["process"]["pid"])
        self.assertEqual((self.workspace / "python3").read_text(), original)

    def test_declared_path_survives_resume_with_deleted_evaluator_and_decoy(self):
        original = "# original evaluator passes\n"
        script = self.workspace / "grade.py"
        script.write_text(original)
        check = {"argv": ["python3", "grade.py"], "sha256": hashlib.sha256(original.encode()).hexdigest()}
        runner = self.runner([check], write=True)
        first = runner.run()
        self.assertEqual(first["status"], "success", first)
        paths = json.loads(runner.manifest_path.read_text())["nodes"]["verify"]["_declared_check_paths"]
        script.unlink()
        (self.workspace / "python3").write_text(original)
        resumed = resume_workflow(self.workspace, self.config, first["workflow_id"])
        self.assertEqual(resumed["status"], "failed", resumed)
        result = resumed["nodes"][0]["result"]
        self.assertEqual(result["gate_codes"][0]["code"], "check_contract_changed")
        self.assertEqual(result["acceptance_checks"][0]["contract"]["path"], str(script))
        self.assertEqual(json.loads(runner.manifest_path.read_text())["nodes"]["verify"]["_declared_check_paths"], paths)

    def test_bare_program_is_not_an_inferred_evaluator_even_when_file_exists(self):
        (self.workspace / "python3").write_text("# unrelated workspace file\n")
        script = self.workspace / "grade.py"
        script.write_text("# actual evaluator\n")
        check = {"argv": ["python3", "grade.py"], "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
        self.assertEqual(self.runner([check]).run()["status"], "success")
        script.unlink()
        with self.assertRaisesRegex(ValueError, 'explicit "path"'):
            self.runner([check])
        self.assertEqual(self.calls.read_text().splitlines(), ["call"])

    def test_explicit_path_binds_an_evaluator_created_by_a_fixture(self):
        body = "# trusted fixture evaluator\n"
        check = {"argv": ["python3", "grade.py"], "path": "grade.py",
                 "sha256": hashlib.sha256(body.encode()).hexdigest()}
        runner = self.runner([check])
        runner.nodes["verify"]["acceptance"]["fixtures"] = [{"path": "grade.py", "content": body}]
        self.assertEqual(runner.run()["status"], "success")
        self.assertFalse((self.workspace / "grade.py").exists())

    def test_resume_without_a_saved_binding_requires_an_explicit_path(self):
        script = self.workspace / "grade.py"
        script.write_text("# evaluator\n")
        check = {"argv": ["python3", "grade.py"], "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
        runner = self.runner([check])
        first = runner.run()
        manifest = json.loads(runner.manifest_path.read_text())
        del manifest["nodes"]["verify"]["_declared_check_paths"]
        runner.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'explicit "path"'):
            resume_workflow(self.workspace, self.config, first["workflow_id"])

    def test_declared_hash_is_checked_before_launch(self):
        script = self.workspace / "grade.py"
        script.write_text("raise AssertionError('must not execute')\n")
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                check = {"argv": ["python3", "grade.py"], "sha256": "0" * 64}
                if explicit:
                    check["path"] = "grade.py"
                outcome = self.runner([check]).run()
                result = outcome["nodes"][0]["result"]
                self.assertEqual(outcome["status"], "failed")
                self.assertEqual(result["gate_codes"][0]["code"], "check_contract_changed")
                receipt = result["acceptance_checks"][0]
                self.assertIsNone(receipt["process"]["pid"])
                self.assertIn("contract changed", receipt["error"])
                self.assert_receipt(receipt, "error")

    def test_pinned_fixture_digest_describes_the_executed_content(self):
        body = "print('Ran 2 tests in 0.001s')\n"
        (self.workspace / "grade.py").write_text("raise AssertionError('worker version')\n")
        check = {"argv": ["python3", "grade.py"], "path": "grade.py",
                 "sha256": hashlib.sha256(body.encode()).hexdigest(), "min_tests": 2}
        runner = self.runner([check])
        runner.nodes["verify"]["acceptance"]["fixtures"] = [{"path": "grade.py", "content": body}]
        outcome = runner.run()
        self.assertEqual(outcome["status"], "success", outcome)
        self.assertEqual(outcome["nodes"][0]["result"]["acceptance_checks"][0]["tests_run"], 2)
        self.assertIn("worker version", (self.workspace / "grade.py").read_text())

    def test_minimum_test_count_requires_observed_tests(self):
        for output, minimum, passed, count in (("Ran 1 test in 0.01s", 2, False, 1),
                                               ("=== 2 passed, 1 skipped in 0.02s ===", 3, False, 2),
                                               ("=== 3 passed in 0.02s ===", 3, True, 3),
                                               ("no tests ran in 0.01s", 1, False, None)):
            with self.subTest(output=output):
                body = f"print({output!r})\n"
                (self.workspace / "grade.py").write_text(body)
                check = {"argv": ["python3", "grade.py"], "sha256": hashlib.sha256(body.encode()).hexdigest(),
                         "min_tests": minimum}
                outcome = self.runner([check]).run()
                self.assertEqual(outcome["status"], "success" if passed else "failed")
                result = outcome["nodes"][0]["result"]
                receipt = result["acceptance_checks"][0]
                self.assertEqual(receipt["tests_run"], count)
                self.assert_receipt(receipt, "passed" if passed else "failed")
                if not passed:
                    self.assertEqual(result["gate_codes"][0]["code"], "check_tests_missing")

    def test_check_objects_are_validated(self):
        for check in ({}, {"argv": "python3"}, {"argv": []}, {"argv": ["python3"], "sha256": "bad"},
                      {"argv": ["python3"], "path": "grade.py"}, {"argv": ["python3"], "min_tests": True},
                      {"argv": ["python3"], "min_tests": -1}, {"argv": ["python3"], "min_tests": 1.5},
                      {"argv": ["python3"], "unexpected": 1}):
            with self.subTest(check=check), self.assertRaises(ValueError):
                self.runner([check])

    def test_automatic_pins_keep_symlink_paths_and_default_discovery_pattern(self):
        (self.workspace / "suite").mkdir()
        (self.workspace / "suite/test_one.py").write_text("# original\n")
        (self.workspace / "suite/helper.py").write_text("# not selected\n")
        link = self.workspace / "linked"
        link.symlink_to("suite", target_is_directory=True)
        runner = self.runner([["python3", "linked/test_one.py"],
                              ["python3", "-m", "unittest", "discover", "-s", "suite"]])
        node = runner.nodes["verify"]
        node["attempts"] = 1
        runner._pin_check_inputs(node)
        self.assertEqual(set(node["_check_input_pins"]), {"linked/test_one.py", "suite/test_one.py"})
        (self.workspace / "other").mkdir()
        (self.workspace / "other/test_one.py").write_text("# replacement\n")
        link.unlink()
        link.symlink_to("other", target_is_directory=True)
        result = {"status": "success", "summary": "done", "check_inputs_changed": ["worker-forged"]}
        runner._accept_node(node, result)
        self.assertEqual(result["check_inputs_changed"], ["linked/test_one.py"])

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
        run_dir = Path(node["result"]["artifacts"]["run_dir"])
        self.assertEqual(json.loads((run_dir / "result.json").read_text())["acceptance_checks"], [receipt])
        self.assertEqual(json.loads((run_dir / "task.json").read_text())["node_task"], "Return a fixture handoff")

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

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_successful_parent_with_live_group_is_rejected_and_group_terminated(self):
        started, release, completed = [self.workspace / name for name in ("started", "release", "completed")]
        child = ("import pathlib,time;"
                 f"pathlib.Path({str(started)!r}).write_text('ready');"
                 f"release=pathlib.Path({str(release)!r}); deadline=time.monotonic()+5;"
                 "\nwhile not release.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                 "print('late child output',flush=True);"
                 f"pathlib.Path({str(completed)!r}).write_text('escaped cleanup')")
        parent = ("import pathlib,subprocess,sys,time;"
                  f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
                  f"ready=pathlib.Path({str(started)!r});"
                  "\nwhile not ready.exists(): time.sleep(0.01)\n"
                  "print('parent output',flush=True)")
        runner = self.runner([[sys.executable, "-c", parent]])
        result = {"status": "success", "summary": "fixture", "attempt": 1}
        try:
            accepted, problems = runner._accept_node(runner.nodes["verify"], result)
        finally:
            release.touch()
        self.assertFalse(accepted)
        self.assertTrue(any("processes remaining" in problem for problem in problems))
        receipt = result["acceptance_checks"][0]
        self.assert_receipt(receipt, "error")
        self.assertEqual(receipt["exit_code"], 0)  # Direct exit remains truthful.
        self.assertTrue(receipt["process"]["exit_observed"])
        self.assertTrue(receipt["process"]["group_survivors_after_exit"])
        self.assertTrue(receipt["process"]["group_termination_requested"])
        time.sleep(0.85)
        self.assertFalse(completed.exists())
        self.assert_receipt(receipt, "error")
        self.assertEqual(Path(receipt["outputs"]["stdout"]["path"]).read_text(), "parent output\n")

    @unittest.skipUnless(os.name == "posix", "requires POSIX session escape")
    def test_escaped_inherited_descriptors_cannot_mutate_final_snapshots(self):
        started, release, completed = [self.workspace / name for name in ("started", "release", "completed")]
        # The escaped child stays idle until after the receipt is finalized, then
        # demonstrably writes through both inherited FDs before marking done.
        child = ("import os,pathlib,sys,time;"
                 f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()));"
                 f"release=pathlib.Path({str(release)!r}); deadline=time.monotonic()+5;"
                 "\nwhile not release.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                 "print('late escaped stdout',flush=True);print('late escaped stderr',file=sys.stderr,flush=True);"
                 f"pathlib.Path({str(completed)!r}).write_text('done')")
        parent = ("import pathlib,subprocess,sys,time;"
                  f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True);"
                  f"ready=pathlib.Path({str(started)!r});"
                  "\nwhile not ready.exists(): time.sleep(0.01)\n"
                  "print('parent stdout',flush=True);print('parent stderr',file=sys.stderr,flush=True)")
        runner = self.runner([[sys.executable, "-c", parent]])
        result = {"status": "success", "summary": "fixture", "attempt": 1}
        try:
            accepted, problems = runner._accept_node(runner.nodes["verify"], result)
            self.assertTrue(accepted, problems)
            receipt = result["acceptance_checks"][0]
            self.assert_receipt(receipt, "passed")
            self.assertEqual(receipt["output_scope"], "detached_observed_prefix_snapshot")
            self.assertFalse(receipt["process"]["group_survivors_after_exit"])
            self.assertFalse(receipt["process"]["group_termination_requested"])
            self.assertEqual(receipt["process"]["descendant_scope"], "same_posix_process_group_only")
            self.assertEqual(receipt["process"]["escaped_sessions"], "unobserved")
            before = {name: Path(receipt["outputs"][name]["path"]).read_bytes() for name in ("stdout", "stderr")}
            self.assertEqual(before, {"stdout": b"parent stdout\n", "stderr": b"parent stderr\n"})
            release.write_text("go")
            deadline = time.monotonic() + 3
            while not completed.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(completed.exists(), "escaped child did not exercise inherited output descriptors")
            self.assert_receipt(receipt, "passed")
            self.assertEqual(before, {name: Path(receipt["outputs"][name]["path"]).read_bytes() for name in before})
        finally:
            release.touch()
            if started.exists():
                try:
                    os.kill(int(started.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()


class NodeTaskContextTest(unittest.TestCase):
    def test_string_decision_context_keeps_the_worker_task_out(self):
        import fusion_decisions as decisions
        source = {"id": "work", "task": "Worker keeps complete frozen context. " * 50,
                  "decision_context": "Inspect a bounded contract", "write": False}
        state = decisions.acceptance_state(source, {"summary": "Observed the boundary", "changed": [], "tests": []}, 6000,
                                           answer_text="")
        self.assertNotIn("frozen context", json.dumps(state))
        self.assertNotIn("node_task", state)

    def test_structured_build_context_adds_the_stage_task(self):
        import fusion_decisions as decisions
        source = {"id": "plan", "task": "Produce an implementation brief.", "role": "planning", "write": False,
                  "decision_context": {"request": "Report what ORC is", "workflow_kind": "discovery", "stage": "plan"}}
        state = decisions.acceptance_state(source, {"summary": "Brief ready", "changed": [], "tests": []}, 6000,
                                           answer_text="")
        self.assertIn("Produce an implementation brief.", json.dumps(state))
