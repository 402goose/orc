"""Control-store regressions use only a local fake worker and isolated config."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_decisions import DecisionStore
from fusion_policy import route_candidates, rank_by_outcomes


class ControlWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worker = self.root / "worker"
        self.control = self.root / "control"
        self.other = self.root / "other"
        for path in (self.worker, self.control, self.other):
            path.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("FUSION_")}
        env.update(ORC_HOME=str(self.root / "home"), FUSION_TELEMETRY="0", FUSION_DECISIONS_MODE="off")
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.fake = self.root / "fake-worker"
        self.fake.write_text("#!/usr/bin/env python3\n" + '''import json, os, sys
sys.stdin.read()
print(json.dumps({'type': 'thread.started', 'thread_id': 'fake-session'}))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
 'text': 'STATUS: success\\nSUMMARY: cwd=' + os.getcwd() + '; resumed=' + str('resume' in sys.argv)}}))
''')
        self.fake.chmod(0o755)
        self.settings = {"codex": {"command": str(self.fake)}, "timeout_seconds": 10}
        for path in (self.worker, self.other):
            (path / ".fusion.json").write_text(json.dumps(self.settings))

    def cli(self, *args, workspace=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = core.main(["--workspace", str(workspace or self.worker), "--json", "--quiet", *map(str, args)])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def delegate(self, *options, workspace=None, route=None):
        return self.cli(*options, "delegate", "--agent", "codex", *(["--route", route] if route else []),
                        "do work", workspace=workspace)

    def test_flag_centralizes_writable_delegate_and_readers(self):
        result = self.delegate("--control-workspace", self.control)
        run_id = result["run_id"]
        directory = self.control / ".fusion" / "runs" / run_id
        self.assertTrue((directory / "result.json").is_file())
        self.assertTrue((directory / "trace.json").is_file())
        self.assertFalse((self.worker / ".fusion").exists())
        self.assertEqual(result["workspace"], str(self.worker))
        self.assertIn("cwd=" + str(self.worker), result["summary"])
        for command in ("status", "runs", "trace"):
            records = self.cli("--control-workspace", self.control, command)
            self.assertEqual((records[0] if command == "trace" else records[0]["result"])["run_id"], run_id)
        shutil.rmtree(self.worker)
        with patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "shadow"}):
            verdict = self.cli("outcome", run_id, "--accepted", "--reason", "Verified the result", workspace=self.control)
        self.assertEqual(verdict["label"]["status"], "labeled")
        self.assertTrue(verdict["accepted"])
        self.assertTrue(DecisionStore(self.control).path.exists())
        self.cli("decisions", "routing-report", workspace=self.control)

    def test_env_and_flag_precedence_and_no_selection_leak(self):
        with patch.dict(os.environ, {"FUSION_CONTROL_WORKSPACE": str(self.control)}):
            result = self.delegate()
            self.assertTrue(Path(result["artifacts"]["run_dir"]).is_relative_to(self.control))
            result = self.delegate("--control-workspace", self.other)
            self.assertTrue(Path(result["artifacts"]["run_dir"]).is_relative_to(self.other))
        result = self.delegate()
        self.assertTrue(Path(result["artifacts"]["run_dir"]).is_relative_to(self.worker))
        self.assertEqual(result["workspace"], str(self.worker))
        self.assertIn("codex:implementation", json.loads((self.worker / ".fusion/sessions.json").read_text()))

    def test_sessions_are_scoped_to_worker(self):
        first = self.delegate("--control-workspace", self.control)
        second = self.delegate("--control-workspace", self.control, workspace=self.other)
        third = self.delegate("--control-workspace", self.control)
        self.assertFalse(first["resumed"])
        self.assertFalse(second["resumed"])
        self.assertTrue(third["resumed"])

    def test_missing_binary_receipt_has_workspace(self):
        (self.worker / ".fusion.json").write_text(json.dumps({"codex": {"command": str(self.root / "missing")}}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = core.main(["--workspace", str(self.worker), "--control-workspace", str(self.control),
                              "--json", "--quiet", "delegate", "--agent", "codex", "work"])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["workspace"], str(self.worker))
        self.assertEqual(json.loads(Path(result["artifacts"]["run_dir"], "result.json").read_text()), result)
        self.assertFalse((self.worker / ".fusion").exists())

    def test_worker_config_and_control_routing_overlay(self):
        child = self.worker / "child"
        child.mkdir()
        (self.control / ".fusion.json").write_text(json.dumps({
            "codex": {"command": "do-not-use"}, "execution_mode": "yolo", "timeout_seconds": 1,
            "routes": {"shared": {"agent": "codex"}}, "cache": {"ttl_seconds": 123},
            "decisions": {"rank_by_outcomes": 1},
        }))
        with patch.dict(os.environ, {"FUSION_CONTROL_WORKSPACE": str(self.control)}):
            config, source = core.load_config(child)
            self.assertEqual(source, self.worker / ".fusion.json")
            self.assertEqual(config["codex"]["command"], str(self.fake))
            self.assertEqual(config["timeout_seconds"], 10)
            self.assertEqual(config["execution_mode"], "restricted")
            self.assertEqual(config["cache"]["ttl_seconds"], 123)
            self.assertEqual(config["routes"]["shared"]["agent"], "codex")
            result = self.delegate(workspace=child, route="shared")
        self.assertEqual(result["workspace"], str(child))
        self.assertFalse((child / ".fusion").exists())

    def test_shared_outcomes_rank_routes_across_checkouts(self):
        routes = {"good": {"agent": "codex"}, "bad": {"agent": "codex"}}
        (self.control / ".fusion.json").write_text(json.dumps({**self.settings, "routes": routes}))
        for worker, route, accepted in ((self.worker, "good", True), (self.other, "bad", False)):
            result = self.delegate("--control-workspace", self.control, workspace=worker, route=route)
            self.cli("outcome", result["run_id"], "--accepted" if accepted else "--rejected", workspace=self.control)
        config, _ = core.load_config(self.control)
        task = core.make_task(self.control, "auto", "work", "implementation", [], [], None, False, True)
        choices = route_candidates(config, task, core.RunStore(self.control))
        ranked = rank_by_outcomes([c for c in choices if c["route"] in routes], minimum=1, explore=False)
        self.assertEqual(ranked[0]["route"], "good")
        self.assertEqual({c["route"]: c["acceptance_rate"] for c in ranked}, {"good": 1.0, "bad": 0.0})
        self.assertTrue(all(c["checked_runs"] == 1 for c in ranked))

    def test_workflow_run_resume_and_report_use_control(self):
        spec = self.root / "workflow.json"
        spec.write_text(json.dumps({"task": "work", "nodes": [{"id": "one", "agent": "codex", "task": "work", "write": True}]}))
        options = ("--control-workspace", self.control, "workflow")
        result = self.cli(*options, "run", spec)
        workflow_id = result["workflow_id"]
        self.assertTrue(Path(result["artifacts"]["manifest"]).is_relative_to(self.control))
        receipt = result["nodes"][0]["result"]
        self.assertEqual(receipt["workspace"], str(self.worker))
        self.assertFalse((self.worker / ".fusion").exists())
        self.cli(*options, "resume", workflow_id)
        # Resuming from the controller with an edited spec still runs in W.
        spec.write_text(json.dumps({"task": "work", "nodes": [{"id": "one", "agent": "codex", "task": "different work", "write": True}]}))
        resumed = self.cli("workflow", "resume", workflow_id, "--spec", spec, workspace=self.control)
        self.assertIn("cwd=" + str(self.worker), resumed["nodes"][0]["result"]["summary"])
        self.assertFalse((self.worker / ".fusion").exists())
        self.cli(*options, "status", workflow_id)
        self.cli(*options, "report", workflow_id)

    def test_mcp_delegate_keeps_target_cwd_and_control_store(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "fusion_delegate", "arguments": {"agent": "codex", "workspace": str(self.other), "task": "work"}}}]
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("\n".join(map(json.dumps, requests)))), contextlib.redirect_stdout(output):
            code = core.main(["--workspace", str(self.worker), "--control-workspace", str(self.control), "mcp-serve"])
        self.assertEqual(code, 0)
        response = json.loads(output.getvalue())["result"]
        self.assertFalse(response.get("isError"), response)
        runs = core.RunStore(self.control).recent()
        self.assertEqual(runs[0]["result"]["workspace"], str(self.other))
        self.assertFalse((self.other / ".fusion").exists())

    def test_leads_propagate_flag_to_mcp_and_child_environment(self):
        (self.worker / ".fusion.json").write_text(json.dumps({**self.settings, "claude": {"command": str(self.fake)}}))
        for agent in ("codex", "claude"):
            captured = {}
            def launch(argv, **kwargs):
                captured.update(argv=argv, **kwargs)
                if "--mcp-config" in argv:
                    captured["mcp"] = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
                return Mock(returncode=0, stdout="")
            with patch.dict(os.environ, {"FUSION_CONTROL_WORKSPACE": str(self.other)}), \
                    patch.object(core.subprocess, "run", side_effect=launch):
                code = core.main(["--workspace", str(self.worker), "--control-workspace", str(self.control),
                                  "--quiet", "lead", "--agent", agent, "work"])
            self.assertEqual(code, 0)
            self.assertEqual(captured["cwd"], self.worker)
            self.assertEqual(captured["env"]["FUSION_CONTROL_WORKSPACE"], str(self.control))
            if agent == "claude":
                self.assertEqual(captured["mcp"]["mcpServers"]["fusion"]["env"]["FUSION_CONTROL_WORKSPACE"], str(self.control))
            else:
                self.assertIn('mcp_servers.fusion.env.FUSION_CONTROL_WORKSPACE=' + json.dumps(str(self.control)), captured["argv"])

    def test_async_mcp_launch_centralizes_logs_and_passes_control(self):
        import fusion_mcp
        captured = {}
        def spawn(argv, **kwargs):
            captured.update(argv=argv, **kwargs)
            (self.control / ".fusion/workflows/test-workflow").mkdir(parents=True)
            return Mock(pid=123, poll=Mock(return_value=None))
        with patch.dict(os.environ, {"FUSION_CONTROL_WORKSPACE": str(self.control)}), patch.object(fusion_mcp, "_LAUNCHED", []):
            result = fusion_mcp.start_run(self.worker, "work", workflow_id="test-workflow", spawn=spawn)
        self.assertEqual(result["status"], "running")
        self.assertEqual(captured["cwd"], str(self.worker))
        self.assertEqual(captured["env"]["FUSION_CONTROL_WORKSPACE"], str(self.control))
        self.assertTrue((self.control / ".fusion/mcp-launch.log").exists())
        self.assertFalse((self.worker / ".fusion").exists())

    def test_build_preparation_and_worktree_artifacts_use_control(self):
        from fusion_publish import setup_worktree
        prepared = self.cli("--control-workspace", self.control, "build", "--kind", "review", "--plan-only", "Review code")
        self.assertTrue(Path(prepared["brief"]).is_relative_to(self.control))
        calls = []
        def git(workspace, *args):
            calls.append((workspace, args))
            Path(args[-2]).mkdir()
        with patch.dict(os.environ, {"FUSION_CONTROL_WORKSPACE": str(self.control)}), \
                patch("fusion_publish.fetch_base", return_value=("owner/repo", "url", "abc")), \
                patch("fusion_publish.git", side_effect=git):
            result = setup_worktree(self.worker, "test-workflow", "work", {"mode": "manual"})
        self.assertEqual(calls[0][0], self.worker)
        self.assertTrue(Path(result["workspace"]).is_relative_to(self.control))
        self.assertEqual((Path(result["workspace"]) / ".fusion").resolve(), self.control / ".fusion")
        self.assertTrue((self.control / ".fusion/workflows/test-workflow/git.json").exists())
        self.assertFalse((self.worker / ".fusion").exists())

    def test_ultra_uses_control_for_receipts_and_stage_artifacts(self):
        (self.worker / ".fusion.json").write_text(json.dumps({**self.settings, "ultra": {
            "stages": {"implement": {"agent": "codex", "write": True}}}}))
        self.cli("--control-workspace", self.control, "ultra", "work")
        self.assertTrue(list((self.control / ".fusion/ultra").glob("*/manifest.json")))
        self.assertEqual(core.RunStore(self.control).recent()[0]["result"]["workspace"], str(self.worker))
        self.assertFalse((self.worker / ".fusion").exists())


if __name__ == "__main__":
    unittest.main()
