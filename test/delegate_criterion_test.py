"""A delegation can name what acceptance is judged against (--criterion): the
run records it as decision_context, so a generic dispatch prompt never stands
in for the task in the acceptance input."""
import contextlib
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
import fusion_decisions
from fusion_decisions import DecisionStore, read_jsonl

TEMPLATE = "Read AGENT.md and EXPERIMENTS.md. Then create or modify the files listed under \"Files in scope\"."
SPEC = "## Task\nA credentialed login rejects a wrong password with 401 and never creates a session.\n"


def no_runtime(options):
    raise RuntimeError("no Laya runtime in this test")


class DelegateCriterionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_DECISIONS_MODE", None)
        runtime = patch.object(fusion_decisions, "runtime_for", no_runtime)
        runtime.start()
        self.addCleanup(runtime.stop)
        worker = self.root / "claude-fixture"
        worker.write_text(f"#!{sys.executable}\nimport json\n"
                          "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                          "'result':'STATUS: success\\nSUMMARY: Added the 401 path\\nCHANGED: login.py\\nTESTS: none\\nBLOCKERS: none'}))\n")
        worker.chmod(0o755)
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "shadow"}, "claude": {"command": str(worker)}})

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(core, "load_config", return_value=(self.config, None)):
            try:
                code = core.main(["--workspace", str(self.workspace), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def runs(self):
        return sorted((self.workspace / ".fusion" / "runs").glob("*/task.json")) if (self.workspace / ".fusion" / "runs").exists() else []

    def test_a_criterion_file_replaces_the_dispatch_prompt_in_the_acceptance_input(self):
        spec = self.workspace / "AGENT.md"
        spec.write_text(SPEC)
        code, out, err = self.cli("--json", "delegate", "--agent", "claude", "--criterion-file", str(spec), TEMPLATE)
        self.assertEqual(code, 0, out + err)
        run_id = json.loads(out)["run_id"]
        [task_path] = self.runs()
        task = json.loads(task_path.read_text())
        self.assertEqual((task["task"], task["decision_context"]), (TEMPLATE, SPEC.strip()))
        with patch.object(core, "load_config", return_value=(self.config, None)):
            payload = core.record_outcome(self.workspace, run_id, False, "No test covers the session table")
        self.assertEqual(payload["label"]["status"], "labeled")
        [decision] = [e for e in read_jsonl(DecisionStore(self.workspace).path)
                      if e.get("event") == "decision" and e.get("kind") == "acceptance"]
        state = json.loads(decision["state"])
        self.assertEqual(state["task"], SPEC.strip())
        self.assertNotIn("AGENT.md and EXPERIMENTS.md", decision["state"])

    def test_without_a_criterion_the_task_text_is_judged_as_before(self):
        code, out, err = self.cli("--json", "delegate", "--agent", "claude", "Add the 401 path")
        self.assertEqual(code, 0, out + err)
        [task_path] = self.runs()
        self.assertNotIn("decision_context", json.loads(task_path.read_text()))

    def test_criterion_flags_are_exclusive_bounded_and_record_nothing_when_refused(self):
        spec = self.workspace / "AGENT.md"
        spec.write_text(SPEC)
        for extra in (("--criterion", "x", "--criterion-file", str(spec)),
                      ("--criterion", "y" * (core.MAX_CRITERION_CHARS + 1)),
                      ("--criterion-file", str(self.workspace / "missing.md"))):
            with self.subTest(extra=extra[0:3:2]):
                code, _, _ = self.cli("delegate", "--agent", "claude", *extra, TEMPLATE)
                self.assertEqual(code, 2)
        self.assertEqual(self.runs(), [])

    def test_mcp_delegate_records_the_criterion(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "fusion_delegate", "arguments": {"agent": "claude", "task": TEMPLATE, "criterion": SPEC}}}]
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("\n".join(map(json.dumps, requests)))), contextlib.redirect_stdout(output), \
                patch.object(core, "load_config", return_value=(self.config, None)):
            code = core.main(["--workspace", str(self.workspace), "mcp-serve"])
        self.assertEqual(code, 0)
        response = json.loads(output.getvalue())["result"]
        self.assertFalse(response.get("isError"), response)
        [task_path] = self.runs()
        self.assertEqual(json.loads(task_path.read_text())["decision_context"], SPEC.strip())


if __name__ == "__main__":
    unittest.main()
