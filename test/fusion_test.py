import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fusion_core  # noqa: E402
from fusion_workflow import WorkflowRunner, run_workflow, resume_workflow, workflow_report  # noqa: E402


class FusionHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir()
        self.bin_dir = Path(self.temp.name) / "bin"
        self.bin_dir.mkdir()
        self.calls = Path(self.temp.name) / "calls.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def write_agent(self, name: str, body: str) -> Path:
        path = self.bin_dir / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def config(self, codex: Path | None = None, claude: Path | None = None, agy: Path | None = None) -> None:
        value = {
            "codex": {"command": str(codex or "missing-codex")},
            "claude": {"command": str(claude or "missing-claude")},
            "agy": {"command": str(agy or "missing-agy")},
            "timeout_seconds": 30,
        }
        (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")

    def test_codex_result_is_structured_and_session_is_resumed(self):
        calls = str(self.calls)
        codex = self.write_agent(
            "codex-fake",
            f"""
import json, pathlib, sys
pathlib.Path({calls!r}).open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')
prompt = sys.stdin.read()
print(json.dumps({{'type':'thread.started','thread_id':'thread-123'}}))
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'STATUS: success\\nSUMMARY: worker completed'}}}}))
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':12,'output_tokens':4}}}}))
""",
        )
        self.config(codex=codex)

        first = io.StringIO()
        with contextlib.redirect_stdout(first):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "codex", "--role", "implementation", "do work"]
                ),
                0,
            )
        first_result = json.loads(first.getvalue())
        self.assertEqual(first_result["status"], "success")
        self.assertEqual(json.loads((self.workspace / ".fusion" / "sessions.json").read_text())["codex:implementation"], "thread-123")

        second = io.StringIO()
        with contextlib.redirect_stdout(second):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "codex", "--role", "implementation", "follow up"]
                ),
                0,
            )
        self.assertEqual(json.loads(second.getvalue())["status"], "success")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn("resume", calls[1])
        self.assertIn("thread-123", calls[1])

    def test_claude_json_result_is_structured(self):
        claude = self.write_agent(
            "claude-fake",
            """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'claude-123','result':'STATUS: success\\nSUMMARY: reviewed\\nCHANGED: src/app.py, README.md\\nTESTS: python -m unittest\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "review it"]
                ),
                0,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["summary"], "reviewed")
        self.assertEqual(result["changed"], ["src/app.py", "README.md"])
        self.assertEqual(result["tests"], ["python -m unittest"])
        self.assertEqual(result["blockers"], [])

    def test_agy_result_is_structured_and_session_is_resumed(self):
        calls = str(self.calls)
        agy = self.write_agent(
            "agy-fake",
            f"""
import json, pathlib, sys
pathlib.Path({calls!r}).open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')
print(json.dumps({{'conversation_id':'conv-123','status':'SUCCESS','response':'STATUS: success\\nSUMMARY: agy completed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none','num_turns':1,'usage':{{'input_tokens':20,'output_tokens':5,'thinking_tokens':3,'cache_read_tokens':10}}}}))
""",
        )
        self.config(agy=agy)

        first = io.StringIO()
        with contextlib.redirect_stdout(first):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "agy", "--read-only", "--fresh", "--role", "investigator", "inspect"]
                ),
                0,
            )
        first_result = json.loads(first.getvalue())
        self.assertEqual(first_result["status"], "success")
        self.assertEqual(first_result["summary"], "agy completed")
        self.assertEqual(first_result["usage"]["cache_read_input_tokens"], 10)
        self.assertEqual(first_result["usage"]["reasoning_output_tokens"], 3)
        self.assertEqual(json.loads((self.workspace / ".fusion" / "sessions.json").read_text())["agy:investigator"], "conv-123")

        argv = json.loads(self.calls.read_text().splitlines()[0])
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--mode", argv)
        self.assertIn("plan", argv)

        second = io.StringIO()
        with contextlib.redirect_stdout(second):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "agy", "--read-only", "--role", "investigator", "follow up"]
                ),
                0,
            )
        self.assertEqual(json.loads(second.getvalue())["status"], "success")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn("--conversation", calls[1])
        self.assertIn("conv-123", calls[1])

    def test_agy_denied_tools_and_failure_are_reported(self):
        agy = self.write_agent(
            "agy-denied",
            """
import json
print(json.dumps({'conversation_id':'conv-denied','status':'SUCCESS','response':'','denied_actions':[{'action':'command','display_name':'RunCommand'}],'usage':{'input_tokens':9,'output_tokens':1}}))
""",
        )
        self.config(agy=agy)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "agy", "--fresh", "run something"]
                ),
                1,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertIn("auto-denied", " ".join(result["blockers"]))
        self.assertIn("RunCommand", " ".join(result["blockers"]))

    def test_orc_free_routes_skip_max_budget_but_paid_routes_keep_it(self):
        free_task = fusion_core.make_task(
            self.workspace, "claude", "t", "r", [], [], None, False, False,
            settings_overrides={"command": "orc", "model": "cohere/north-mini-code:free", "max_budget_usd": 0.1},
        )
        argv, _, _ = fusion_core.agent_command({}, free_task, None)
        self.assertNotIn("--max-budget-usd", argv)
        paid_task = fusion_core.make_task(
            self.workspace, "claude", "t", "r", [], [], None, False, False,
            settings_overrides={"command": "orc", "model": "anthropic/claude-opus-5", "max_budget_usd": 0.4},
        )
        argv, _, _ = fusion_core.agent_command({}, paid_task, None)
        self.assertIn("--max-budget-usd", argv)

    def test_mcp_lists_tools(self):
        self.config()
        requests = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            ]
        ) + "\n"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "mcp-serve"],
            input=requests,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "fusion")
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"fusion_delegate", "fusion_status"})

    def test_mcp_delegates_with_the_task_contract(self):
        codex = self.write_agent(
            "codex-fake",
            """
import json
print(json.dumps({'type':'thread.started','thread_id':'mcp-thread'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'STATUS: success\\nSUMMARY: delegated\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}}))
print(json.dumps({'type':'turn.completed','usage':{}}))
""",
        )
        self.config(codex=codex)
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "fusion_delegate",
                    "arguments": {
                        "agent": "codex",
                        "task": "review the change",
                        "role": "reviewer",
                        "success_criteria": ["return a handoff"],
                        "constraints": ["do not edit files"],
                        "write": False,
                        "resume": False,
                    },
                },
            }
        ) + "\n"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "mcp-serve"],
            input=request,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        response = json.loads(proc.stdout)
        result = response["result"]["structuredContent"]
        self.assertEqual(result["status"], "success", response)
        self.assertEqual(result["summary"], "delegated")
        task_files = list((self.workspace / ".fusion" / "runs").glob("*/task.json"))
        self.assertEqual(len(task_files), 1)
        task = json.loads(task_files[0].read_text())
        self.assertEqual(task["task"], "review the change")
        self.assertEqual(task["role"], "reviewer")
        self.assertEqual(task["constraints"], ["do not edit files"])

    def test_ultra_pipeline_keeps_stage_artifacts_and_stops_at_limit(self):
        claude = self.write_agent(
            "claude-fake",
            """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'ultra-claude','result':'STATUS: success\\nSUMMARY: stage complete\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        codex = self.write_agent(
            "codex-fake",
            """
import json
print(json.dumps({'type':'thread.started','thread_id':'ultra-codex'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'STATUS: success\\nSUMMARY: implementation complete\\nCHANGED: src/app.py\\nTESTS: python -m unittest\\nBLOCKERS: none'}}))
print(json.dumps({'type':'turn.completed','usage':{}}))
""",
        )
        value = {
            "claude": {"command": str(claude)},
            "codex": {"command": str(codex)},
            "routes": {"cheap": {"agent": "claude", "command": str(claude)}},
            "ultra": {
                "max_stages": 5,
                "stages": {
                    "explore": {"agent": "claude", "route": "cheap", "write": False},
                    "implement": {"agent": "codex", "write": True},
                    "review": {"agent": "claude", "route": "cheap", "write": False},
                    "synthesize": {"agent": "claude", "route": "cheap", "write": False},
                },
            },
        }
        (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            actual = fusion_core.main(
                ["--workspace", str(self.workspace), "--json", "ultra", "--stages", "3", "ship the bounded feature"]
            )
        self.assertEqual(actual, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "success")
        self.assertEqual([item["stage"] for item in result["stages"]], ["explore", "implement", "review"])
        self.assertEqual(len(list(Path(result["artifacts"]["root"]).glob("*.json"))), 4)
        self.assertEqual(result["stages"][1]["result"]["changed"], ["src/app.py"])

        codex_output = io.StringIO()
        with contextlib.redirect_stdout(codex_output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "ultra", "--harness", "codex", "--stages", "1", "codex-only"]
                ),
                0,
            )
        codex_result = json.loads(codex_output.getvalue())
        self.assertEqual(codex_result["stages"][0]["task"]["agent"], "codex")
        self.assertEqual(codex_result["stages"][0]["task"]["route"], "codex-read")

    def test_doctor_checks_named_route_commands(self):
        claude = self.write_agent("claude-fake", "print('unused')\n")
        codex = self.write_agent("codex-fake", "print('unused')\n")
        self.config(codex=codex, claude=claude)
        value = json.loads((self.workspace / ".fusion.json").read_text())
        value["routes"] = {
            "good": {"agent": "claude", "command": str(claude)},
            "missing": {"agent": "claude", "command": "missing-orc"},
        }
        (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(fusion_core.main(["--workspace", str(self.workspace), "doctor"]), 1)
        result = json.loads(output.getvalue())
        route_checks = {item["route"]: item for item in result["route_checks"]}
        self.assertTrue(route_checks["good"]["ok"])
        self.assertFalse(route_checks["missing"]["ok"])

    def test_workflow_fanout_fanin_and_artifact_gate(self):
        claude = self.write_agent(
            "workflow-claude",
            """
import json, pathlib, sys
prompt = sys.argv[-1]
if 'final.md' in prompt:
    pathlib.Path('final.md').write_text('synthesized workflow output\\n', encoding='utf-8')
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'workflow-claude','result':'STATUS: success\\nSUMMARY: node completed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "evaluate the repository",
            "max_parallel": 2,
            "max_attempts": 1,
            "nodes": [
                {
                    "id": "research",
                    "items": ["alpha", "beta", "gamma"],
                    "task_template": "Research {item}",
                    "role": "researcher",
                    "agent": "claude",
                },
                {
                    "id": "synthesize",
                    "needs": ["research"],
                    "task": "Synthesize the research into final.md",
                    "role": "synthesizer",
                    "agent": "claude",
                    "write": True,
                    "required_files": ["final.md"],
                    "acceptance": {"required_handoff": ["summary"]},
                },
            ],
            "acceptance": {"required_files": ["final.md"], "required_nodes": ["synthesize"]},
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), spec_path)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["nodes"]), 4)
        self.assertTrue(all(node["status"] == "success" for node in result["nodes"]))
        self.assertTrue((self.workspace / "final.md").is_file())
        events = (Path(result["artifacts"]["events"])).read_text(encoding="utf-8")
        self.assertIn('"type": "node.succeeded"', events)

    def test_workflow_pauses_on_quota_and_resumes_with_persisted_state(self):
        claude = self.write_agent(
            "quota-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'error','is_error':True,'session_id':'quota-claude','result':\"You've hit your session limit; resets at 5:10am\"}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "quota test",
            "nodes": [{"id": "probe", "task": "probe", "agent": "claude"}],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        first = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(first["status"], "paused_quota")
        run_id = first["workflow_id"]

        claude.write_text(
            "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'quota-claude','result':'STATUS: success\\nSUMMARY: resumed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n",
            encoding="utf-8",
        )
        claude.chmod(0o755)
        resumed = resume_workflow(self.workspace, config, run_id)
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(resumed["nodes"][0]["attempts"], 2)

    def test_workflow_rejects_stale_required_artifact(self):
        claude = self.write_agent(
            "stale-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'stale-claude','result':'STATUS: success\\nSUMMARY: reported success\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        (self.workspace / "FINAL.md").write_text("old output\\n", encoding="utf-8")
        spec = {
            "task": "stale artifact test",
            "nodes": [{"id": "writer", "task": "write the final artifact", "agent": "claude", "required_files": ["FINAL.md"]}],
            "acceptance": {"required_files": ["FINAL.md"]},
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), spec_path)
        self.assertEqual(result["status"], "failed")
        self.assertIn("did not change", " ".join(result["nodes"][0]["result"]["blockers"]))

    def test_workflow_report_groups_waves_and_usage(self):
        claude = self.write_agent(
            "report-claude",
            """
import json, pathlib, sys
prompt = sys.argv[-1]
if 'final.md' in prompt:
    pathlib.Path('final.md').write_text('synthesized workflow output\\n', encoding='utf-8')
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'report-claude','result':'STATUS: success\\nSUMMARY: node completed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "evaluate the repository",
            "max_parallel": 2,
            "nodes": [
                {"id": "research", "items": ["alpha", "beta", "gamma"], "task_template": "Research {item}", "agent": "claude"},
                {
                    "id": "synthesize",
                    "needs": ["research"],
                    "task": "Synthesize the research into final.md",
                    "agent": "claude",
                    "write": True,
                    "required_files": ["final.md"],
                },
            ],
            "acceptance": {"required_files": ["final.md"]},
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        result = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(result["status"], "success")

        report = workflow_report(self.workspace, result["workflow_id"])
        self.assertEqual(report["status"], "success")
        waves = {item["wave"]: {node["id"] for node in item["nodes"]} for item in report["waves"]}
        self.assertEqual(waves[0], {"research-01", "research-02", "research-03"})
        self.assertEqual(waves[1], {"synthesize"})
        self.assertEqual(report["usage"]["spans"], 4)
        self.assertEqual(report["blockers"], [])
        self.assertIsNone(report["resume_command"])

    def test_workflow_report_surfaces_blockers_and_resume_command(self):
        claude = self.write_agent(
            "report-quota-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'error','is_error':True,'session_id':'s','result':\"You've hit your session limit; resets at 5:10am\"}))
""",
        )
        self.config(claude=claude)
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps({"task": "report quota test", "nodes": [{"id": "probe", "task": "probe", "agent": "claude"}]}), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        result = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(result["status"], "paused_quota")

        report = workflow_report(self.workspace, result["workflow_id"])
        self.assertEqual(report["status"], "paused_quota")
        self.assertTrue(report["blockers"])
        self.assertTrue(all(item["node_id"] == "probe" for item in report["blockers"]))
        self.assertTrue(any("session limit" in item["blocker"] for item in report["blockers"]))
        self.assertIn("workflow resume", report["resume_command"])
        self.assertIn(result["workflow_id"], report["resume_command"])

    def test_preflight_blocks_missing_executable_before_any_dispatch(self):
        self.config(claude=Path("missing-claude-binary"))
        spec = {
            "task": "preflight test",
            "max_parallel": 3,
            "nodes": [
                {"id": "a", "task": "a", "agent": "claude"},
                {"id": "b", "task": "b", "agent": "claude"},
                {"id": "c", "task": "c", "agent": "claude"},
            ],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), spec_path)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(node["status"] == "blocked" for node in result["nodes"]))
        self.assertTrue(all("not available on PATH" in node["result"]["blockers"][0] for node in result["nodes"]))
        # No node was actually dispatched: the preflight caught the missing
        # binary once instead of three separate worker attempts discovering it.
        self.assertFalse((self.workspace / ".fusion" / "runs").exists())

    def test_lane_cooldown_prevents_further_dispatch_in_same_run(self):
        calls = self.bin_dir / "calls.txt"
        claude = self.write_agent(
            "quota-lane-claude",
            f"""
import json, pathlib
pathlib.Path({str(calls)!r}).open('a', encoding='utf-8').write('call\\n')
print(json.dumps({{'type':'result','subtype':'error','is_error':True,'session_id':'s','result':\"You've hit your session limit; resets at 5:10am\"}}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "lane cooldown test",
            "max_parallel": 1,
            "nodes": [
                {"id": "a", "task": "a", "agent": "claude"},
                {"id": "b", "task": "b", "agent": "claude"},
            ],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), spec_path)
        self.assertEqual(result["status"], "paused_quota")
        statuses = {node["id"]: node["status"] for node in result["nodes"]}
        self.assertEqual(statuses["a"], "paused_quota")
        self.assertEqual(statuses["b"], "paused_quota")
        node_b = next(node for node in result["nodes"] if node["id"] == "b")
        self.assertIn("lane", node_b["result"]["summary"])
        # Node b never actually ran a worker; only node a's failure did.
        self.assertEqual(calls.read_text().count("call"), 1)

    def test_fresh_run_seeds_lane_cooldown_from_recent_trace_but_resume_does_not(self):
        claude = self.write_agent(
            "history-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'error','is_error':True,'session_id':'s','result':\"You've hit your usage limit; resets in 10 minutes\"}))
""",
        )
        self.config(claude=claude)
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps({"task": "seed", "nodes": [{"id": "a", "task": "a", "agent": "claude"}]}), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        first = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(first["status"], "paused_quota")

        # A brand new workflow in the same workspace should see the still-fresh
        # quota trace and refuse to dispatch into the same dead lane.
        spec_path_2 = self.workspace / "workflow2.json"
        spec_path_2.write_text(json.dumps({"task": "seed2", "nodes": [{"id": "b", "task": "b", "agent": "claude"}]}), encoding="utf-8")
        second = run_workflow(self.workspace, config, spec_path_2)
        self.assertEqual(second["status"], "paused_quota")
        self.assertEqual(second["nodes"][0]["status"], "paused_quota")
        self.assertIn("quota", second["nodes"][0]["result"]["blockers"][0])

        # But an explicit resume of the first run is a "try again now" signal
        # and must not be blocked by that same trace history.
        claude.write_text(
            "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: resumed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n",
            encoding="utf-8",
        )
        claude.chmod(0o755)
        resumed = resume_workflow(self.workspace, config, first["workflow_id"])
        self.assertEqual(resumed["status"], "success")

    def test_select_orc_model_requires_fit_unless_allow_untested(self):
        orc = self.write_agent(
            "orc",
            """
import sys
argv = sys.argv[1:]
if argv[:1] == ['models']:
    if '--fit' in argv:
        print('providerA/good-model\\tFIT stuff')
    else:
        # Untested candidate ranks ahead of the fit one.
        print('providerB/untested-model\\tstuff')
        print('providerA/good-model\\tstuff')
    sys.exit(0)
sys.exit(1)
""",
        )
        self.assertEqual(fusion_core.select_orc_model(str(orc), "free"), "providerA/good-model")
        self.assertEqual(
            fusion_core.select_orc_model(str(orc), "free", allow_untested=True),
            "providerB/untested-model",
        )

    def test_select_orc_model_returns_none_when_nothing_is_fit(self):
        orc = self.write_agent(
            "orc",
            """
import sys
argv = sys.argv[1:]
if argv[:1] == ['models']:
    if '--fit' not in argv:
        print('providerB/untested-model\\tstuff')
    sys.exit(0)
sys.exit(1)
""",
        )
        self.assertIsNone(fusion_core.select_orc_model(str(orc), "free"))

    def test_workflow_waits_for_all_dependencies_before_blocking_fanin(self):
        spec = {
            "nodes": [
                {"id": "quota", "task": "quota", "agent": "claude"},
                {"id": "sibling", "task": "sibling", "agent": "claude"},
                {"id": "fanin", "needs": ["quota", "sibling"], "task": "fanin", "agent": "claude"},
            ]
        }
        # Pin the claude lane to a resolvable command: WorkflowRunner's
        # preflight blocks lanes whose executable is missing, which would
        # otherwise depend on whether claude happens to be on PATH.
        runner = WorkflowRunner(self.workspace, {"claude": {"command": "true"}}, spec)
        runner.nodes["quota"]["status"] = "paused_quota"
        runner.nodes["sibling"]["status"] = "running"
        runner._block_unrunnable()
        self.assertEqual(runner.nodes["fanin"]["status"], "pending")

        runner.nodes["sibling"]["status"] = "failed"
        runner._block_unrunnable()
        self.assertEqual(runner.nodes["fanin"]["status"], "blocked")
        self.assertEqual(
            runner.nodes["fanin"]["result"]["blockers"],
            ["quota: paused_quota", "sibling: failed"],
        )

    def test_failure_class_categorizes_common_failure_reasons(self):
        self.assertIsNone(fusion_core.failure_class({"status": "success", "blockers": []}))
        self.assertEqual(
            fusion_core.failure_class({"status": "error", "blockers": ["You've hit your session limit; resets at 5pm"]}),
            "quota",
        )
        self.assertEqual(
            fusion_core.failure_class({"status": "error", "blockers": ["permission denied: Bash"]}),
            "permission_denied",
        )
        self.assertEqual(
            fusion_core.failure_class({"status": "blocked", "exit_code": 124, "blockers": ["timeout after 60 seconds"]}),
            "timeout",
        )
        self.assertEqual(
            fusion_core.failure_class({"status": "error", "blockers": ["orc is not available on PATH"]}),
            "missing_executable",
        )
        self.assertEqual(
            fusion_core.failure_class({"status": "error", "blockers": ["something else broke"]}),
            "worker_error",
        )

    def test_telemetry_install_id_is_stable_and_not_per_workspace(self):
        with tempfile.TemporaryDirectory() as home:
            old_home = os.environ.get("ORC_HOME")
            os.environ["ORC_HOME"] = home
            try:
                first = fusion_core.telemetry_install_id()
                second = fusion_core.telemetry_install_id()
                self.assertEqual(first, second)
                self.assertTrue((Path(home) / "telemetry_id").is_file())
            finally:
                if old_home is None:
                    os.environ.pop("ORC_HOME", None)
                else:
                    os.environ["ORC_HOME"] = old_home

    def test_remote_telemetry_disabled_by_default_sends_nothing(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(True)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            claude = self.write_agent(
                "claude-default-telemetry",
                """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
            )
            value = {
                "claude": {"command": str(claude)},
                # remote.enabled deliberately omitted -- must default to off.
                "telemetry": {"remote": {"endpoint": f"http://127.0.0.1:{port}/v1/ingest"}},
            }
            (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "do it"]
                )
            self.assertEqual(received, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_remote_telemetry_sends_reduced_payload_when_enabled(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append({
                    "body": json.loads(self.rfile.read(length)),
                    "auth": self.headers.get("Authorization"),
                })
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            claude = self.write_agent(
                "claude-remote-telemetry",
                """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: done\\nCHANGED: secret/path.py\\nTESTS: pytest\\nBLOCKERS: none'}))
""",
            )
            value = {
                "claude": {"command": str(claude)},
                "telemetry": {"remote": {"enabled": True, "endpoint": f"http://127.0.0.1:{port}/v1/ingest", "token": "sekret"}},
            }
            (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    fusion_core.main(
                        ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "do secret/path.py work"]
                    ),
                    0,
                )
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["auth"], "Bearer sekret")
            body = received[0]["body"]
            self.assertEqual(body["schema"], "fusion.telemetry.v1")
            span = body["spans"][0]
            self.assertEqual(span["status"], "success")
            self.assertIsNone(span["failure_class"])
            # The risky, potentially project-identifying fields never leave
            # the local trace even when remote telemetry is enabled.
            self.assertNotIn("changed", span)
            self.assertNotIn("tests", span)
            self.assertNotIn("blockers", span)
            self.assertNotIn("artifacts", span)
        finally:
            server.shutdown()
            server.server_close()

    def test_telemetry_status_reports_configuration(self):
        self.config()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(fusion_core.main(["--workspace", str(self.workspace), "telemetry", "status"]), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["remote_enabled"])
        self.assertIsNone(result["install_id"])
        self.assertEqual(result["fields_sent"], [])


if __name__ == "__main__":
    unittest.main()
