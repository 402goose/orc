"""Retryable provider failures resume the same conversation and preserve partial writes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy as policy
import fusion_quota as quota
import fusion_trajectories as trajectories


NETWORK_ERROR = 'AGY_ERROR: ' + json.dumps({
    "short_error": 'agent executor error: generating and executing: request failed: Post '
                   '"https://example.googleapis.com/v1internal:streamGenerateContent?alt=sse": '
                   'read tcp 127.0.0.1: read: operation timed out',
    "status": "UNKNOWN", "error_code": 2, "code_kind": "grpc", "retryable": True, "error_id": "error-123",
})
BROKEN_PIPE_ERROR = 'AGY_ERROR ' + json.dumps({
    "short_error": 'pre-invocation hook: context summarization: request failed: Post '
                   '"https://example.googleapis.com/v1": write tcp 127.0.0.1: write: broken pipe',
    "retryable": False,
})
HANDOFF = "STATUS: success\nSUMMARY: done\nCHANGED: none\nTESTS: none\nBLOCKERS: none"


class AgyRetryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.calls = self.root / "calls.jsonl"
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0",
                                      "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off",
                                      "FUSION_TMP_SWEEP": "0"})
        env.start()
        self.addCleanup(env.stop)
        # Keep subprocess polling's clock independent of the patched retry clock.
        clock = patch.object(core, "time", SimpleNamespace(time=time.time, strftime=time.strftime,
                                                           monotonic=time.monotonic, sleep=time.sleep))
        clock.start()
        self.addCleanup(clock.stop)
        sleep = patch.object(core.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.store = core.RunStore(self.workspace)

    def write_agent(self, failures=1, conversation="conv-123", error=NETWORK_ERROR, stdout_error=False,
                    scratch=False, empty_final_id=False, handoff=HANDOFF):
        agent = self.root / "agy"
        agent.write_text(f"#!{sys.executable}\n" + f'''
import json, os, pathlib, sys
calls = pathlib.Path({str(self.calls)!r})
attempt = len(calls.read_text().splitlines()) if calls.exists() else 0
directory = os.environ.get('TMP') if {scratch!r} else None
if directory:
    marker = pathlib.Path(directory, 'partial')
    edited = pathlib.Path('edited.txt')
    if attempt:
        assert marker.read_text() == 'saved'
        assert edited.read_text() == 'saved'
    marker.write_text('saved')
    edited.write_text('saved')
with calls.open('a') as stream:
    stream.write(json.dumps({{'argv': sys.argv[1:], 'tmp': directory}}) + '\\n')
failed = attempt < {failures!r}
conversation = {conversation!r} if failed or not {empty_final_id!r} else ''
print(json.dumps({{'conversation_id': conversation, 'status': 'ERROR' if failed else 'SUCCESS',
                  'error': 'provider failed' if failed else '', 'response': '' if failed else {handoff!r}}}))
if failed:
    print({error!r}, file=sys.stdout if {stdout_error!r} else sys.stderr)
sys.exit(3 if failed else 0)
''')
        agent.chmod(0o755)
        return agent

    def dispatch(self, agent, timeout=3600, settings=None, write=False, resume=False):
        config = core.deep_merge(core.DEFAULTS, {"timeout_seconds": timeout, "decisions": {"mode": "off"},
                                                 "agy": {"command": str(agent), **(settings or {})}})
        task = core.make_task(self.workspace, "agy", "edit it" if write else "inspect it", "implement",
                              [], [], None, resume, write)
        self.task, self.config = task, config
        return core.dispatch(config, task, self.store)

    def recorded_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def assert_network_failure(self, result):
        self.assertEqual((result["status"], result["verdict"], result["verdict_reason"]),
                         ("error", "error", "provider_network"))
        self.assertEqual(core.failure_class(result), "provider_network")
        self.assertFalse(core.quota_failure(result))

    def test_retryable_then_success_resumes_and_appends_logs(self):
        result = self.dispatch(self.write_agent(empty_final_id=True))
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--conversation", calls[0]["argv"])
        second = calls[1]["argv"]
        self.assertEqual(second[second.index("--conversation") + 1], "conv-123")
        self.assertEqual(second[second.index("-p") + 1], core.AGY_NETWORK_CONTINUATION)
        self.assertEqual((result["status"], result["verdict"]), ("success", "ok"))
        self.assertEqual(len(result["network_retries"]), 1)
        retry = result["network_retries"][0]
        self.assertEqual((retry["attempt"], retry["delay_s"], retry["resumed"]), (1, 15, True))
        self.assertIn("AGY_ERROR:", retry["error_head"])
        self.assertEqual(result["session_id"], "conv-123")
        self.assertEqual(result["agy_conversation_id"], "conv-123")
        self.assertEqual(self.store.sessions()[self.task["session_key"]], "conv-123")
        self.sleep.assert_called_once_with(15)
        stdout = Path(result["artifacts"]["stdout"]).read_text()
        stderr = Path(result["artifacts"]["stderr"]).read_text()
        self.assertIn('"status": "ERROR"', stdout)
        self.assertIn('"status": "SUCCESS"', stdout)
        self.assertIn(NETWORK_ERROR, stderr)
        for text in (stdout, stderr):
            self.assertEqual(text.count("--- network retry 1 ---"), 1)

    def test_retryable_four_times_exhausts_default_retries(self):
        result = self.dispatch(self.write_agent(failures=4))
        self.assertEqual(len(self.recorded_calls()), 4)
        self.assert_network_failure(result)
        self.assertEqual([r["attempt"] for r in result["network_retries"]], [1, 2, 3])
        self.assertEqual([r["delay_s"] for r in result["network_retries"]], [15, 60, 180])
        self.assertTrue(all(r["resumed"] for r in result["network_retries"]))
        self.assertEqual(self.sleep.call_args_list, [call(15), call(60), call(180)])
        key = self.task["session_key"]
        self.assertNotIn(key, self.store.sessions())
        self.assertEqual(result["agy_conversation_id"], "conv-123")
        saved_result = json.loads(Path(result["artifacts"]["run_dir"], "result.json").read_text())
        self.assertEqual(saved_result["agy_conversation_id"], "conv-123")
        self.dispatch(self.root / "agy", resume=True, settings={"network_retries": 0})
        self.assertEqual(self.task["session_key"], key)
        self.assertNotIn("--conversation", self.recorded_calls()[-1]["argv"])

    def test_non_retryable_error_does_not_retry(self):
        self.dispatch(self.write_agent(failures=0))
        key = self.task["session_key"]
        result = self.dispatch(self.write_agent(failures=99, error='AGY_ERROR: '
                               '{"short_error":"invalid-argument 400","retryable":false}'), resume=True)
        self.assertEqual(len(self.recorded_calls()), 2)
        self.assertIn("--conversation", self.recorded_calls()[-1]["argv"])
        self.assertEqual(result["network_retries"], [])
        self.assertNotEqual(result["verdict_reason"], "provider_network")
        self.assertNotEqual(core.failure_class(result), "provider_network")
        self.assertNotIn(key, self.store.sessions())
        self.assertEqual(result["agy_conversation_id"], "conv-123")
        self.sleep.assert_not_called()

    def test_zero_disables_network_retries_and_clears_session(self):
        self.dispatch(self.write_agent(failures=0))
        key = self.task["session_key"]
        result = self.dispatch(self.write_agent(failures=99), resume=True, settings={"network_retries": 0})
        self.assertEqual(len(self.recorded_calls()), 2)
        self.assert_network_failure(result)
        self.assertEqual(result["network_retries"], [])
        self.assertNotIn(key, self.store.sessions())
        self.assertEqual(result["agy_conversation_id"], "conv-123")
        self.sleep.assert_not_called()

    def test_non_success_handoffs_clear_existing_session(self):
        for status in ("partial", "blocked", "error"):
            with self.subTest(status=status):
                self.dispatch(self.write_agent(failures=0))
                key = self.task["session_key"]
                result = self.dispatch(self.write_agent(failures=0, handoff=HANDOFF.replace(
                    "STATUS: success", "STATUS: " + status)), resume=True)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["agy_conversation_id"], "conv-123")
                self.assertNotIn(key, self.store.sessions())

    def test_timeout_clears_existing_session_and_records_conversation(self):
        self.dispatch(self.write_agent(failures=0))
        key = self.task["session_key"]
        with patch.object(core.progress, "run_logged", side_effect=core.subprocess.TimeoutExpired(
                "agy", 3600, output=json.dumps({"conversation_id": "conv-timeout"}))):
            result = self.dispatch(self.root / "agy", resume=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["agy_conversation_id"], "conv-timeout")
        self.assertNotIn(key, self.store.sessions())

    def test_missing_binary_clears_existing_session(self):
        self.dispatch(self.write_agent(failures=0))
        key = self.task["session_key"]
        result = self.dispatch(self.root / "missing-agy", resume=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["agy_conversation_id"], "conv-123")
        self.assertNotIn(key, self.store.sessions())

    def test_without_conversation_repeats_original_brief(self):
        result = self.dispatch(self.write_agent(conversation=""))
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["argv"], calls[1]["argv"])
        self.assertNotIn("--conversation", calls[1]["argv"])
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["network_retries"][0]["resumed"])
        self.sleep.assert_called_once_with(15)

    def test_timeout_budget_skips_backoff_and_retry(self):
        result = self.dispatch(self.write_agent(), timeout=130)
        self.assertEqual(len(self.recorded_calls()), 1)
        self.assertEqual(result["network_retries"], [])
        self.assert_network_failure(result)
        self.sleep.assert_not_called()

    def test_timeout_budget_is_checked_again_after_sleep(self):
        clock = [0]
        self.sleep.side_effect = lambda delay: clock.__setitem__(0, clock[0] + 25)
        with patch.object(core.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.dispatch(self.write_agent(), timeout=140)
        self.assertEqual(len(self.recorded_calls()), 1)
        self.assertEqual(result["network_retries"], [])
        self.assert_network_failure(result)
        self.sleep.assert_called_once_with(15)

    def test_attempt_timeout_shares_the_overall_budget(self):
        clock, timeouts = [0], []
        run_logged = core.progress.run_logged

        def run(argv, **kwargs):
            timeouts.append(kwargs["timeout"])
            completed = run_logged(argv, **kwargs)
            clock[0] += 100
            return completed

        self.sleep.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay)
        with patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(core.progress, "run_logged", side_effect=run):
            result = self.dispatch(self.write_agent(), timeout=400)
        self.assertEqual(timeouts, [400, 285])
        self.assertEqual(result["status"], "success")

    def test_retryable_error_on_stdout_keeps_conversation(self):
        result = self.dispatch(self.write_agent(stdout_error=True))
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["network_retries"][0]["resumed"])
        self.assertEqual(len(self.recorded_calls()), 2)

    def test_message_only_network_failure_retries(self):
        result = self.dispatch(self.write_agent(error="Eligibility check failed: loadCodeAssist: connection reset by peer"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.recorded_calls()), 2)
        self.sleep.assert_called_once_with(15)

    def test_success_that_quotes_network_errors_does_not_retry(self):
        for message in ("operation timed out", "Eligibility check failed", "network issue connecting to the server"):
            with self.subTest(message=message):
                stdout = json.dumps({"status": "SUCCESS", "conversation_id": "conv-123", "response": message})
                self.assertIsNone(core.parse_agy_output(stdout)[2])

    def test_network_retries_must_be_a_non_negative_integer(self):
        task = core.make_task(self.workspace, "agy", "inspect", "investigator", [], [], None, False, False)
        for invalid in (-1, 1.5, "3", True, None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "agy.network_retries"):
                core.agent_command({"agy": {"network_retries": invalid}}, task, None)
        for valid in (0, 1, 3):
            core.agent_command({"agy": {"network_retries": valid}}, task, None)

    def test_incremental_edits_instruction_is_only_for_agy_writers(self):
        for agent, write in (("agy", True), ("agy", False), ("claude", True), ("codex", True)):
            task = core.make_task(self.workspace, agent, "inspect", "implement", [], [], None, False, write)
            self.assertEqual(core.AGY_INCREMENTAL_EDITS in core.brief_for(task), agent == "agy" and write)

    def test_writer_reuses_sandbox_temp_and_partial_edits(self):
        wrapper = self.root / "sandbox-exec"
        wrapper.write_text(f"#!{sys.executable}\nimport os, sys\nos.execvp(sys.argv[3], sys.argv[3:])\n")
        wrapper.chmod(0o755)
        directory = str(self.root / "run-tmp")

        def run_tmpdir(env):
            env.update(TMPDIR=directory + "/", TMP=directory, TEMP=directory)
            return directory

        with patch.object(core, "sandbox_exec", return_value=str(wrapper)), \
                patch.object(core, "run_tmpdir", side_effect=run_tmpdir) as choose_tmp, \
                patch.object(core, "shared_write_roots", return_value=[str(self.root / "shared")]), \
                patch.object(core, "darwin_user_cache_dir", return_value=None):
            result = self.dispatch(self.write_agent(scratch=True), write=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual([c["tmp"] for c in self.recorded_calls()], [directory, directory])
        choose_tmp.assert_called_once()
        self.assertFalse(Path(directory).exists())
        self.assertEqual((self.workspace / "edited.txt").read_text(), "saved")
        second = self.recorded_calls()[1]["argv"]
        self.assertIn(core.AGY_INCREMENTAL_EDITS, second[second.index("-p") + 1])

    def test_real_error_is_transient_and_not_a_refusal(self):
        session, _, failure, *_ = core.parse_agy_output(json.dumps({"conversation_id": "conv-123"}), NETWORK_ERROR)
        self.assertEqual(session, "conv-123")
        result = {"status": "error", "exit_code": 3, "provider_failure": failure}
        self.assertEqual(core.classify_verdict(result), {"verdict": "error", "reason": "provider_network"})
        self.assertEqual(core.failure_class(result), "provider_network")
        self.assertEqual(core.classify_verdict({"status": "error", "exit_code": 3, "blockers": [NETWORK_ERROR]}),
                         {"verdict": "error", "reason": "provider_network"})

    def test_broken_pipe_overrides_false_retryable_flag(self):
        failure = core.parse_agy_output("", BROKEN_PIPE_ERROR)[2]
        result = {"status": "error", "exit_code": 3, "provider_failure": failure}
        self.assertEqual(core.classify_verdict(result), {"verdict": "error", "reason": "provider_network"})
        self.assertEqual(core.failure_class(result), "provider_network")
        result = self.dispatch(self.write_agent(error=BROKEN_PIPE_ERROR))
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.recorded_calls()), 2)
        self.assertIn("broken pipe", result["network_retries"][0]["error_head"])
        self.sleep.assert_called_once_with(15)

    def test_write_reset_overrides_false_retryable_flag(self):
        error = 'AGY_ERROR: ' + json.dumps({"short_error": "write: connection reset", "retryable": False})
        result = self.dispatch(self.write_agent(error=error), settings={"network_retries": 0})
        self.assert_network_failure(result)

    def test_retryable_json_flag_without_known_message(self):
        error = 'AGY_ERROR: ' + json.dumps({"short_error": "transport interrupted", "retryable": True})
        failure = core.parse_agy_output("", error)[2]
        self.assertEqual(core.failure_class({"status": "error", "provider_failure": failure}), "provider_network")
        self.assertEqual(core.classify_verdict({"status": "error", "exit_code": 3, "blockers": [error]}),
                         {"verdict": "error", "reason": "provider_network"})
        result = self.dispatch(self.write_agent(error=error))
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.recorded_calls()), 2)
        self.sleep.assert_called_once_with(15)

    def test_network_message_patterns(self):
        messages = ["read: connection reset by peer", "write: connection reset", "write: broken pipe",
                    "read: operation timed out",
                    "There was a network issue connecting to the server, please try again.",
                    "Eligibility check failed: loadCodeAssist", "read: i/o timeout",
                    'Post "https://example.googleapis.com/v1": EOF',
                    'EOF on https://example.googleapis.com/v1']
        for message in messages:
            with self.subTest(message=message):
                self.assertIsNotNone(core.parse_agy_output("", message)[2])
                self.assertEqual(core.failure_class({"status": "error", "provider_failure": message}), "provider_network")
        for message in ("EOF", 'Post "https://example.com": EOF',
                        'Post "https://example.com/googleapis.com": EOF', "invalid request"):
            self.assertIsNone(core.agy_network_error(message))
        self.assertIsNone(core.failure_class({"status": "success", "summary": "operation timed out"}))
        self.assertEqual(core.failure_class({"status": "error", "provider_failure": None,
                                             "summary": "operation timed out in application test"}), "worker_error")

    def test_network_failure_does_not_demote_or_cool_lane(self):
        result = self.dispatch(self.write_agent(), settings={"network_retries": 0, "dangerously_skip_permissions": True})
        self.assert_network_failure(result)
        config = core.deep_merge(self.config, {"decisions": {"auto_routes": ["agy"], "priors": False}})
        with patch.object(core, "executable", return_value=True):
            candidates = policy.route_candidates(config, {"workspace": str(self.workspace)}, self.store)
        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0]["key"], candidates[0]["checked_runs"], candidates[0]["acceptance_rate"]),
                         ("agy", 0, None))
        self.assertEqual(quota.cooling(config, self.store, core.now_ms()), [])
        self.assertFalse(core.quota_failure({"provider_failure": 'AGY_ERROR: {"short_error":"quota check interrupted",'
                                                               '"retryable":true}'}))
        audit = {"check_inputs_changed": set(), "withdrawn": set(), "unmeasured": set()}
        self.assertEqual(trajectories.settle_reason(result["run_id"], result, {"accepted": False}, audit), "harness_failure")


if __name__ == "__main__":
    unittest.main()
