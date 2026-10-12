"""`fusion trajectories export` writes only settled runs from repos on the requested side of
the data wall, normalises each harness's steps, redacts secrets and records provenance."""
import builtins
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_trajectories as trajectories
from fusion_decisions import DecisionStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "trajectories"
# Assembled at runtime so no key-shaped literal is committed.
FAKE_KEY = "sk-" + "planted" + "0123456789abcdefFAKE"
FAKE_BEARER = "bearer" + "-planted-" + "zz99"
FAKE_ENV = "env-planted-" + "value-4242"
CONFIG = {"export": {"exclude_repos": ["me/excluded"],
                     "walls": {"personal": ["me/*"], "employer": ["corp/*", "me/both"], "prerelease": ["me/launch"]}}}


def fixture(name):
    return ((FIXTURES / name).read_text(encoding="utf-8").replace("__PLANTED_KEY__", FAKE_KEY)
            .replace("__PLANTED_BEARER__", FAKE_BEARER).replace("__PLANTED_ENV__", FAKE_ENV))


def git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    for argv in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True)
    (path / "kept.txt").write_text("kept\n")
    subprocess.run(["git", "-C", str(path), "add", "kept.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "start"], check=True)
    return path


class Isolated(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0",
                                      "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_CONTROL_WORKSPACE", None)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()

    def run_dir(self, run_id, repo, agent="claude", stdout="", issue=None, role="implementation", provenance=None,
                diff=None, created_at=None, **result):
        directory = self.workspace / ".fusion" / "runs" / run_id
        directory.mkdir(parents=True)
        task = {"schema": core.SCHEMA, "run_id": run_id, "trace_id": run_id, "agent": agent, "role": role,
                "workspace": str(self.root / "gone"), "write": True, "task": f"Fix {run_id}",
                "success_criteria": ["tests pass"], "constraints": ["stay in scope"],
                "created_at": created_at or core.now_ms(), "resolved": {"lane_key": f"{agent}@/Users/someone/.{agent}-alt"},
                **({"repo": repo} if repo else {}), **({"issue": issue} if issue else {})}
        (directory / "task.json").write_text(json.dumps(task))
        (directory / "result.json").write_text(json.dumps({
            "schema": core.SCHEMA, "run_id": run_id, "status": "success", "verdict": "ok", "agent": agent, "role": role,
            "model": f"{agent}-model", "reasoning_effort": None, "usage": {"cost_usd": 0.5, "input_tokens": 10},
            "blockers": [], **({"issue": issue} if issue else {}), **({"provenance": provenance} if provenance else {}),
            **result}))
        (directory / "stdout.log").write_text(stdout)
        if diff is not None:
            (directory / "diff.patch").write_text(diff)
        return directory

    def verdict(self, run_id, accepted=True, **options):
        core.record_outcome(self.workspace, run_id, accepted, "checked the diff and tests", **options)

    def export(self, side="personal", **options):
        out = self.root / "out"
        payload = trajectories.export(self.workspace, CONFIG, out, side, **options)
        return out, payload

    def rows(self, path):
        return [json.loads(line) for line in Path(path).read_text().splitlines()]


class ExportTest(Isolated):
    def setUp(self):
        super().setUp()
        self.run_dir("r-claude", "me/app", "claude", fixture("claude_stream.jsonl"), issue="me/app#1", diff="diff --git a/x b/x\n")
        self.verdict("r-claude")
        self.run_dir("r-codex", "me/app", "codex", fixture("codex_exec.jsonl"), issue="me/app#2",
                     provenance={"schema_version": 1, "orc_sha": None, "repo_head": "a" * 40, "prompt_sha256": "b" * 64,
                                 "lane_config_sha256": "c" * 64})
        self.verdict("r-codex", False, stage="review", rejection_class="review_changes")

    def test_round_trip_writes_runs_and_normalised_steps_and_keeps_unknown_events(self):
        with patch.dict(os.environ, {"SERVICE_TOKEN": FAKE_ENV}):
            out, payload = self.export()
        info = json.loads((out / "meta" / "info.json").read_text())
        self.assertEqual(info["schema"], "fusion.trajectories.v1")
        self.assertEqual(info["fields"]["steps"][:10], ["run_id", "index", "kind", "role", "text", "tool", "arguments",
                                                         "result", "is_error", "time_ms"])
        runs = self.rows(out / "meta" / "runs.jsonl")
        self.assertEqual([run["run_id"] for run in runs], ["r-claude", "r-codex"])
        steps = self.rows(out / "steps" / "chunk-000.jsonl")
        self.assertEqual([(step["run_id"], step["index"]) for step in steps],
                         sorted((step["run_id"], step["index"]) for step in steps))
        self.assertEqual(info["totals"]["steps"], len(steps))
        self.assertEqual({step["run_id"]: 0 for step in steps}.keys(), {"r-claude", "r-codex"})
        claude = [step for step in steps if step["run_id"] == "r-claude"]
        self.assertEqual([(step["kind"], step["role"]) for step in claude],
                         [("message", "reasoning"), ("message", "assistant"), ("tool_call", "assistant"),
                          ("tool_result", "tool"), ("other", None), ("message", "assistant")])
        self.assertEqual(claude[2]["tool"], "Bash")
        self.assertEqual((claude[3]["tool"], claude[3]["result"], claude[3]["is_error"]), ("Bash", "1 failed", True))
        self.assertEqual(claude[4]["raw_type"], "rate_limit_event")
        codex = [step for step in steps if step["run_id"] == "r-codex"]
        self.assertEqual([step["kind"] for step in codex],
                         ["message", "tool_call", "tool_result", "tool_call", "tool_result", "other", "message"])
        self.assertEqual(codex[1]["arguments"], {"command": "pytest -q"})
        self.assertTrue(codex[2]["is_error"])
        self.assertEqual(codex[5]["raw_type"], "item.future_widget")
        self.assertEqual(runs[0]["steps"] + runs[1]["steps"], len(steps))
        claude_run = runs[0]
        self.assertEqual(claude_run["task"], {"text": "Fix r-claude", "success_criteria": ["tests pass"],
                                              "constraints": ["stay in scope"]})
        self.assertEqual((claude_run["diff"], claude_run["diff_truncated"]), ("diff --git a/x b/x\n", False))
        self.assertEqual((claude_run["cost_usd"], claude_run["cost_estimated"], claude_run["repo"]), (0.5, False, "me/app"))
        self.assertEqual(claude_run["lane_key"], "claude@.claude-alt")
        self.assertTrue(claude_run["outcome"]["accepted"])
        self.assertEqual(payload["totals"]["runs"], 2)
        for path in out.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o077, 0, path)

    def test_a_review_stage_rejection_carries_its_stage_and_class(self):
        out, _ = self.export()
        codex = next(run for run in self.rows(out / "meta" / "runs.jsonl") if run["run_id"] == "r-codex")
        self.assertEqual(codex["outcome"], {"accepted": False, "stage": "review", "rejection_class": "review_changes",
                                            "reporter": None, "reason": "checked the diff and tests"})

    def test_planted_keys_are_redacted_and_counted(self):
        with patch.dict(os.environ, {"SERVICE_TOKEN": FAKE_ENV, "SHORT_KEY": "1"}):
            out, payload = self.export()
        written = "".join(path.read_text() for path in out.rglob("*") if path.is_file())
        for secret in (FAKE_KEY, FAKE_BEARER, FAKE_ENV):
            self.assertNotIn(secret, written)
        self.assertIn("[REDACTED]", written)
        info = json.loads((out / "meta" / "info.json").read_text())
        self.assertEqual(info["redactions"], 3)
        self.assertEqual(payload["redactions"], 3)
        command = next(step for step in self.rows(out / "steps" / "chunk-000.jsonl") if step["kind"] == "tool_call")
        self.assertIn("Authorization: [REDACTED]'", command["arguments"]["command"])

    def test_redactor_masks_each_key_shape(self):
        redact = trajectories.Redactor({"API_SECRET": FAKE_ENV, "PATH": "/usr/bin:/bin"})
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----"
        samples = [FAKE_KEY, "ghp_" + "a" * 36, "AKIA" + "ABCDEFGHIJKLMNOP", pem, "authorization: token xyz", FAKE_ENV]
        masked = redact({"text": "\n".join(samples), "headers": {"Authorization": "Basic abc"}, "api_key": "short", "path": "/usr/bin"})
        self.assertEqual(masked["text"].count("[REDACTED]"), len(samples))
        self.assertEqual((masked["headers"]["Authorization"], masked["api_key"], masked["path"]),
                         ("[REDACTED]", "[REDACTED]", "/usr/bin"))
        self.assertEqual(redact.count, len(samples) + 2)

    def test_old_runs_export_with_derived_provenance(self):
        out, _ = self.export()
        runs = {run["run_id"]: run for run in self.rows(out / "meta" / "runs.jsonl")}
        derived = runs["r-claude"]["provenance"]
        self.assertTrue(derived["derived"])
        self.assertEqual(derived["schema_version"], core.PROVENANCE_SCHEMA_VERSION)
        task = json.loads((self.workspace / ".fusion/runs/r-claude/task.json").read_text())
        self.assertEqual(derived["prompt_sha256"], hashlib.sha256(core.brief_for(task).encode()).hexdigest())
        self.assertNotIn("derived", runs["r-codex"]["provenance"])
        self.assertEqual(runs["r-codex"]["provenance"]["repo_head"], "a" * 40)

    def test_dry_run_writes_nothing_and_reports_counts(self):
        self.run_dir("r-open", "me/app")
        out = self.root / "out"
        printed = io.StringIO()
        (self.workspace / ".fusion.json").write_text(json.dumps(CONFIG))
        with contextlib.redirect_stdout(printed):
            code = core.main(["--workspace", str(self.workspace), "trajectories", "export", "--out", str(out),
                              "--for", "personal", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(out.exists())
        payload = json.loads(printed.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["totals"]["runs"], 2)
        self.assertEqual(payload["excluded"], {"no_outcome": 1})

    def test_cli_export_writes_the_layout_and_refuses_a_used_directory(self):
        (self.workspace / ".fusion.json").write_text(json.dumps(CONFIG))
        out = self.root / "cli-out"
        argv = ["--workspace", str(self.workspace), "trajectories", "export", "--out", str(out), "--for", "personal"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(core.main(argv), 0)
        self.assertEqual(sorted(str(path.relative_to(out)) for path in out.rglob("*.json*")),
                         ["meta/info.json", "meta/runs.jsonl", "steps/chunk-000.jsonl"])
        with contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
            core.main(argv)
        self.assertIn("not an empty directory", error.getvalue())


class ExclusionTest(Isolated):
    def test_each_exclusion_is_absent_and_counted(self):
        for run_id, repo in (("r-kept", "me/app"), ("r-excluded", "me/excluded"), ("r-unknown", None),
                             ("r-employer", "corp/tool"), ("r-both", "me/both"), ("r-neither", "other/thing"),
                             ("r-launch", "me/launch"), ("r-unsettled", "me/app"), ("r-unmeasured-eval", "me/app"),
                             ("r-withdrawn", "me/app"), ("r-unmeasured", "me/app"), ("r-quota", "me/app"),
                             ("r-old", "me/app"), ("r-reviewer", "me/app")):
            self.run_dir(run_id, repo, created_at=1 if run_id == "r-old" else None,
                         role="reviewer" if run_id == "r-reviewer" else "implementation",
                         **({"verdict": "quota", "status": "error"} if run_id == "r-quota" else {}))
            if run_id == "r-unmeasured-eval":
                self.verdict(run_id, False, rejection_class="eval_unmeasured")
            elif run_id == "r-withdrawn":
                self.verdict(run_id)
                core.record_outcome(self.workspace, run_id, withdraw=True, reason="graded the wrong run")
            elif run_id == "r-unmeasured":
                core.record_outcome(self.workspace, run_id, unmeasured=True, reason="no checks ran")
            elif run_id != "r-unsettled":
                self.verdict(run_id)
        out, payload = self.export(since_days=30, role="implementation")
        self.assertEqual([run["run_id"] for run in self.rows(out / "meta" / "runs.jsonl")], ["r-kept"])
        expected = {"excluded_repo": 1, "unknown_repo": 1, "wall_other_side": 1, "wall_both": 1, "wall_neither": 1,
                    "prerelease": 1, "no_outcome": 1, "eval_unmeasured": 1, "withdrawn": 1, "unmeasured": 1, "quota": 1,
                    "filter_since": 1, "filter_role": 1}
        self.assertEqual(payload["excluded"], expected)
        self.assertEqual(json.loads((out / "meta" / "info.json").read_text())["excluded"], expected)
        written = "".join(path.read_text() for path in (out / "steps").glob("*.jsonl")) + (out / "meta" / "runs.jsonl").read_text()
        for run_id in ("r-excluded", "r-unknown", "r-employer", "r-both", "r-neither", "r-launch",
                                    "r-unsettled", "r-unmeasured-eval", "r-withdrawn"):
            self.assertNotIn(run_id, written)

    def test_prerelease_needs_the_flag_and_the_other_side_exports_its_own_repos(self):
        for run_id, repo in (("r-launch", "me/launch"), ("r-employer", "corp/tool")):
            self.run_dir(run_id, repo)
            self.verdict(run_id)
        _, payload = self.export(include_prerelease=True, dry_run=True)
        self.assertEqual((payload["totals"]["runs"], payload["excluded"]), (1, {"wall_other_side": 1}))
        _, payload = self.export("employer", dry_run=True)
        self.assertEqual((payload["totals"]["runs"], payload["excluded"]), (1, {"wall_other_side": 1}))

    def test_without_walls_nothing_leaves_and_malformed_walls_are_refused(self):
        self.run_dir("r-kept", "me/app")
        self.verdict("r-kept")
        payload = trajectories.export(self.workspace, {}, self.root / "out", "personal", dry_run=True)
        self.assertEqual((payload["totals"]["runs"], payload["excluded"]), (0, {"wall_neither": 1}))
        for walls in ({"personal": "me/*"}, {"personal": [""]}, {"interns": ["me/*"]}, ["me/*"]):
            with self.subTest(walls=walls), self.assertRaisesRegex(ValueError, "export.walls"):
                trajectories.export(self.workspace, {"export": {"walls": walls}}, self.root / "out", "personal", dry_run=True)


class SplitTest(unittest.TestCase):
    def test_the_split_is_deterministic_by_issue(self):
        issues = [f"me/app#{number}" for number in range(400)]
        first = [trajectories.split_for(issue, "run-a") for issue in issues]
        self.assertEqual(first, [trajectories.split_for(issue, "run-b") for issue in issues])
        self.assertEqual(first, [trajectories.split_for(issue.upper(), "run-c") for issue in issues])
        self.assertEqual(set(first), {"train", "heldout"})
        self.assertLess(first.count("heldout"), first.count("train"))
        self.assertEqual(trajectories.split_for(None, "group-1"), trajectories.split_for(None, "group-1"))


class ProvenanceTest(Isolated):
    def dispatch(self, workspace, secret="one"):
        events = [{"type": "text", "sessionID": "s", "part": {"type": "text", "text": "STATUS: success\nSUMMARY: ok"}},
                  {"type": "step_finish", "sessionID": "s", "part": {"reason": "stop", "tokens": {"input": 10, "output": 5},
                                                                     "cost": 0.01}}]
        worker = self.root / "opencode-fixture"
        worker.write_text(f"#!{sys.executable}\nimport json, pathlib\npathlib.Path('new_file.py').write_text('print(1)\\n')\n"
                          f"for e in {events!r}:\n    print(json.dumps(e))\n")
        worker.chmod(0o755)
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "opencode": {"command": str(worker)},
                                                 "routes": {"oc": {"agent": "opencode", "model": "x/y", "api_key": secret,
                                                                   "env": {"X_TOKEN": secret}}}})
        task = core.make_task(workspace, "opencode", "Add a file", "implementation", [], [], None, False, True, route="oc")
        return task, core.dispatch(config, task, core.RunStore(workspace))

    def test_new_runs_record_provenance_and_their_diff(self):
        workspace = git_repo(self.root / "repo")
        head = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        task, result = self.dispatch(workspace)
        self.assertEqual(result["status"], "success", result)
        run_dir = Path(result["artifacts"]["run_dir"])
        recorded = json.loads((run_dir / "result.json").read_text())["provenance"]
        self.assertEqual(set(recorded), {"schema_version", "orc_sha", "repo_head", "prompt_sha256", "lane_config_sha256"})
        self.assertEqual(recorded["schema_version"], core.PROVENANCE_SCHEMA_VERSION)
        self.assertEqual(recorded["repo_head"], head)
        self.assertEqual(recorded["prompt_sha256"], hashlib.sha256(core.brief_for(task).encode()).hexdigest())
        self.assertRegex(recorded["lane_config_sha256"], "^[0-9a-f]{64}$")
        diff = (run_dir / "diff.patch").read_text()
        self.assertIn("new_file.py", diff)
        self.assertNotIn(".fusion", diff)
        # A secret or an environment value is stripped before the lane settings are hashed.
        _, again = self.dispatch(git_repo(self.root / "repo2"), secret="two")
        self.assertEqual(again["provenance"]["lane_config_sha256"], recorded["lane_config_sha256"])

    def test_provenance_git_calls_spawn_no_background_maintenance(self):
        workspace = git_repo(self.root / "repo")
        base = core.git_head(workspace)
        (workspace / "kept.txt").write_text("changed\n")
        (workspace / "new_file.py").write_text("print(1)\n")
        trace = self.root / "trace.jsonl"
        with patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}):
            self.assertEqual(core.git_head(workspace), base)
            self.assertIn("new_file.py", core.capture_diff(workspace, base))
            self.assertIsNone(core.repo_slug(workspace))
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        started = [event["argv"] for event in events if event.get("event") == "start"]
        self.assertTrue(any("diff" in argv for argv in started))
        for argv in started:
            self.assertTrue({"maintenance.auto=false", "gc.auto=0", "core.fsmonitor=false"} <= set(argv), argv)
        self.assertFalse([argv for argv in (event.get("argv", []) for event in events)
                          if "maintenance" in argv or "gc" in argv])

    def test_suite_digest_names_pinned_checks(self):
        task = {"check_input_pins": {"test/check.py": "d" * 64}, "verification_argv": [["python3", "test/check.py"]]}
        self.assertRegex(core.suite_digest(task), "^[0-9a-f]{64}$")
        self.assertNotEqual(core.suite_digest(task), core.suite_digest({**task, "check_input_pins": {"test/check.py": "e" * 64}}))
        self.assertIsNone(core.suite_digest({}))


class StepParserTest(unittest.TestCase):
    def test_other_harnesses_normalise_and_keep_unknown_events(self):
        opencode = "\n".join(json.dumps(event) for event in (
            {"type": "step_start", "part": {}},
            {"type": "tool_use", "timestamp": 5, "part": {"tool": "bash", "state": {"status": "error", "input": {"command": "ls"},
                                                                                     "error": "denied"}}},
            {"type": "brand_new", "part": {}},
            {"type": "text", "part": {"text": "done"}}))
        self.assertEqual([(step["kind"], step["is_error"], step["raw_type"]) for step in core.opencode_steps(opencode)],
                         [("tool_call", None, None), ("tool_result", True, None), ("other", None, "brand_new"),
                          ("message", None, None)])
        self.assertEqual(core.opencode_steps(opencode)[0]["time_ms"], 5)
        grok = "\n".join(json.dumps(event) for event in (
            {"type": "text", "data": "Checking "}, {"type": "text", "data": "tests."},
            {"type": "tool_call", "toolCallId": "c1", "toolName": "run"},
            {"type": "tool_call_update", "toolCallId": "c1", "rawInput": {"command": "pytest"}},
            {"type": "tool_call_update", "toolCallId": "c1", "status": "failed", "rawOutput": "1 failed"},
            {"type": "surprise"}, {"type": "end", "stopReason": "end_turn"}))
        steps = core.grok_steps(grok)
        self.assertEqual([step["kind"] for step in steps], ["message", "tool_call", "tool_result", "other"])
        self.assertEqual((steps[0]["text"], steps[1]["arguments"], steps[2]["is_error"]),
                         ("Checking tests.", {"command": "pytest"}, True))
        agy = core.agy_steps(json.dumps({"response": "ok", "denied_actions": [{"action": "run"}], "status": "SUCCESS"}))
        self.assertEqual([(step["kind"], step["tool"]) for step in agy], [("message", None), ("tool_result", "run")])
        self.assertEqual(core.claude_steps("not json")[0]["raw_type"], "non_json")
        single = core.claude_steps(json.dumps({"type": "result", "result": "STATUS: success", "is_error": False}))
        self.assertEqual([(step["kind"], step["text"]) for step in single], [("message", "STATUS: success")])


class ParquetTest(Isolated):
    def test_the_converter_refuses_cleanly_without_pyarrow(self):
        directory = self.root / "export"
        trajectories.export(self.workspace, CONFIG, directory, "personal")
        before = sorted(directory.rglob("*"))
        real_import = builtins.__import__

        def no_pyarrow(name, *args, **kwargs):
            if name == "pyarrow" or name.startswith("pyarrow."):
                raise ImportError("No module named 'pyarrow'")
            return real_import(name, *args, **kwargs)
        error = io.StringIO()
        with patch("builtins.__import__", no_pyarrow), contextlib.redirect_stderr(error), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = core.main(["--workspace", str(self.workspace), "trajectories", "parquet", str(directory)])
        self.assertEqual(code, 1)
        self.assertIn("needs pyarrow", error.getvalue())
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(sorted(directory.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
