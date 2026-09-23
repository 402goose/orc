import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fusion_mcp  # noqa: E402


def write_manifest(workspace: Path, workflow_id: str, **fields) -> Path:
    directory = workspace / ".fusion" / "workflows" / workflow_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"workflow_id": workflow_id, "status": "success", "nodes": {}, **fields}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


class OrientationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_empty_workspace_says_so_instead_of_failing(self):
        state = fusion_mcp.orientation(self.workspace)
        self.assertEqual(state["recent_workflows"], [])
        self.assertIn("No workflows recorded", state["next"])

    def test_counts_nodes_by_status_and_sums_spend(self):
        write_manifest(
            self.workspace, "wf-1", status="success", spent_usd=1.5,
            nodes={"a": {"status": "success"}, "b": {"status": "success"}, "c": {"status": "invalid"}},
        )
        write_manifest(self.workspace, "wf-2", status="success", spent_usd=0.25)
        state = fusion_mcp.orientation(self.workspace)
        self.assertEqual(state["workflow_spend_usd"], 1.75)
        first = next(w for w in state["recent_workflows"] if w["workflow_id"] == "wf-1")
        self.assertEqual(first["nodes"], {"success": 2, "invalid": 1})

    def test_points_at_the_failed_run_rather_than_the_newest(self):
        write_manifest(self.workspace, "wf-1-old", status="failed")
        write_manifest(self.workspace, "wf-2-new", status="success")
        self.assertIn("wf-1-old", fusion_mcp.orientation(self.workspace)["next"])

    def test_a_running_workflow_takes_priority(self):
        write_manifest(self.workspace, "wf-1", status="failed")
        write_manifest(self.workspace, "wf-2", status="running", coordinator_pid=None)
        state = fusion_mcp.orientation(self.workspace)
        self.assertIn("running", state["next"])

    def test_unreadable_manifest_is_skipped_not_fatal(self):
        write_manifest(self.workspace, "wf-good")
        broken = self.workspace / ".fusion" / "workflows" / "wf-broken"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("{ not json")
        state = fusion_mcp.orientation(self.workspace)
        self.assertEqual([w["workflow_id"] for w in state["recent_workflows"]], ["wf-good"])


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_every_advertised_prompt_expands(self):
        required = {"ship-feature": {"request": "x"}, "explain-run": {"workflow_id": "wf-1"}}
        for prompt in fusion_mcp.PROMPTS:
            with self.subTest(prompt=prompt["name"]):
                result = fusion_mcp.prompt_messages(
                    prompt["name"], required.get(prompt["name"], {}), self.workspace
                )
                self.assertTrue(result["messages"][0]["content"]["text"].strip())

    def test_declared_required_arguments_are_actually_enforced(self):
        for prompt in fusion_mcp.PROMPTS:
            required = [a["name"] for a in prompt["arguments"] if a.get("required")]
            if not required:
                continue
            with self.subTest(prompt=prompt["name"]):
                with self.assertRaises(ValueError):
                    fusion_mcp.prompt_messages(prompt["name"], {}, self.workspace)

    def test_where_am_i_embeds_live_state(self):
        write_manifest(self.workspace, "wf-1", status="failed")
        text = fusion_mcp.prompt_messages("where-am-i", {}, self.workspace)["messages"][0]["content"]["text"]
        self.assertIn("wf-1", text)

    def test_unknown_prompt_is_rejected(self):
        with self.assertRaises(ValueError):
            fusion_mcp.prompt_messages("nope", {}, self.workspace)


class ResourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_reads_here_and_workflows(self):
        write_manifest(self.workspace, "wf-1")
        for uri in ("orc://here", "orc://workflows"):
            with self.subTest(uri=uri):
                body = fusion_mcp.read_resource(uri, self.workspace)["contents"][0]
                self.assertEqual(body["uri"], uri)
                json.loads(body["text"])

    def test_reads_a_manifest_by_id(self):
        write_manifest(self.workspace, "wf-1", task="inspect")
        body = fusion_mcp.read_resource("orc://workflow/wf-1/manifest", self.workspace)
        self.assertEqual(json.loads(body["contents"][0]["text"])["task"], "inspect")

    def test_rejects_unknown_scheme_workflow_and_leaf(self):
        for uri in ("https://example.test/x", "orc://workflow/missing/manifest", "orc://workflow/wf-1/nope", "orc://nope"):
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError):
                    fusion_mcp.read_resource(uri, self.workspace)

    def test_listed_resources_are_all_readable(self):
        write_manifest(self.workspace, "wf-1")
        for entry in fusion_mcp.list_resources(self.workspace):
            if entry["uri"].endswith("/report"):
                continue  # needs a full run directory
            with self.subTest(uri=entry["uri"]):
                fusion_mcp.read_resource(entry["uri"], self.workspace)

    def test_every_template_matches_a_real_resolver(self):
        write_manifest(self.workspace, "wf-1")
        for template in fusion_mcp.RESOURCE_TEMPLATES:
            uri = template["uriTemplate"].replace("{workflow_id}", "wf-1")
            with self.subTest(uri=uri):
                try:
                    fusion_mcp.read_resource(uri, self.workspace)
                except ValueError as exc:
                    self.fail(f"template {template['uriTemplate']} does not resolve: {exc}")
                except Exception:
                    pass  # a report needs run artifacts; the route exists, which is the point


class RunHandleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def fake_spawn(self, register: bool):
        """A launcher that registers the id start_run chose, via the env."""
        outer = self

        class Proc:
            pid = 4242
            def poll(self):
                return None

        def spawn(argv, **kwargs):
            outer.argv = argv
            outer.env = kwargs.get("env") or {}
            if register:
                from fusion_workflow import WORKFLOW_ID_ENV
                write_manifest(outer.workspace, outer.env[WORKFLOW_ID_ENV], status="running")
            return Proc()

        return spawn

    def test_names_the_run_up_front_and_passes_it_down(self):
        from fusion_workflow import WORKFLOW_ID_ENV

        result = fusion_mcp.start_run(
            self.workspace, "add tests", "build",
            spawn=self.fake_spawn(True), sleep=lambda _s: None,
        )
        self.assertEqual(result["status"], "running")
        # The handle is the id we chose, not whatever appeared on disk.
        self.assertEqual(result["workflow_id"], self.env[WORKFLOW_ID_ENV])
        self.assertIn(f"orc://workflow/{result['workflow_id']}/report", result["evidence"])

    def test_a_concurrent_run_cannot_steal_the_handle(self):
        # The old implementation returned "newest directory that appeared",
        # so a run started by someone else in the same moment won the handle.
        def spawn_with_interloper(argv, **kwargs):
            from fusion_workflow import WORKFLOW_ID_ENV
            self.env = kwargs.get("env") or {}
            write_manifest(self.workspace, "zzz-someone-elses-run", status="running")
            write_manifest(self.workspace, self.env[WORKFLOW_ID_ENV], status="running")

            class Proc:
                pid = 1
                def poll(self):
                    return None

            return Proc()

        result = fusion_mcp.start_run(
            self.workspace, "add tests", "build",
            spawn=spawn_with_interloper, sleep=lambda _s: None,
        )
        self.assertNotEqual(result["workflow_id"], "zzz-someone-elses-run")

    def test_still_returns_a_usable_handle_when_registration_is_slow(self):
        ticks = iter([0.0, 1.0, 99.0])
        result = fusion_mcp.start_run(
            self.workspace, "add tests", "build",
            spawn=self.fake_spawn(False), now=lambda: next(ticks), sleep=lambda _s: None,
        )
        self.assertEqual(result["status"], "starting")
        self.assertTrue(result["workflow_id"])  # chosen, not guessed — always usable

    def test_reports_failed_when_the_launch_dies_before_registering(self):
        class DeadProc:
            pid = 1
            def poll(self):
                return 1

        result = fusion_mcp.start_run(
            self.workspace, "add tests", "build",
            spawn=lambda argv, **kw: DeadProc(), sleep=lambda _s: None,
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("error", result)

    def test_rejects_an_empty_request_and_an_unknown_kind(self):
        with self.assertRaises(ValueError):
            fusion_mcp.start_run(self.workspace, "   ", "build", spawn=self.fake_spawn(False))
        with self.assertRaises(ValueError):
            fusion_mcp.start_run(self.workspace, "x", "destroy", spawn=self.fake_spawn(False))

    def test_status_marks_terminal_runs_done(self):
        write_manifest(self.workspace, "wf-1", status="success", spent_usd=2.0,
                       nodes={"a": {"status": "success", "attempts": 1}})
        status = fusion_mcp.run_status(self.workspace, "wf-1")
        self.assertTrue(status["done"])
        self.assertEqual(status["spent_usd"], 2.0)
        self.assertEqual(status["nodes"]["a"]["status"], "success")

    def test_status_does_not_call_a_live_run_done(self):
        # `done` is what a polling client loops on; a false positive here makes
        # it read a report that does not exist yet.
        import os

        write_manifest(self.workspace, "wf-live", status="running", coordinator_pid=os.getpid())
        status = fusion_mcp.run_status(self.workspace, "wf-live")
        self.assertEqual(status["status"], "running")
        self.assertFalse(status["done"])

    def test_status_on_a_missing_handle_is_an_error(self):
        with self.assertRaises(ValueError):
            fusion_mcp.run_status(self.workspace, "nope")

    def test_cancel_refuses_a_pid_that_is_no_longer_the_coordinator(self):
        # A recorded pid can be recycled onto an unrelated program; signalling
        # it would interrupt something that has nothing to do with ORC.
        import os

        write_manifest(self.workspace, "wf-1", status="running", coordinator_pid=os.getpid())
        sent = []
        result = fusion_mcp.cancel_run(
            self.workspace, "wf-1",
            kill=lambda pid, sig: sent.append(pid),
            matches=lambda pid, wf: False,
        )
        self.assertFalse(result["cancelled"])
        self.assertIn("no longer", result["reason"])
        self.assertEqual(sent, [])

    def test_process_matches_rejects_a_stranger_and_a_dead_pid(self):
        self.assertFalse(fusion_mcp.process_matches(1, "wf-1"))        # launchd/init
        self.assertFalse(fusion_mcp.process_matches(999999, "wf-1"))   # not a process

    def test_cancel_reports_honestly_when_the_coordinator_is_gone(self):
        write_manifest(self.workspace, "wf-1", status="running", coordinator_pid=None)
        result = fusion_mcp.cancel_run(self.workspace, "wf-1")
        self.assertFalse(result["cancelled"])

    def test_cancel_sends_sigint_so_workers_are_cleaned_up(self):
        # Found by having ORC review this file: the coordinator installs a
        # handler for SIGINT only, and workers run in their own session. A
        # SIGTERM kills the coordinator outright and orphans its workers, which
        # keep running and keep billing. The signal is the whole fix.
        import os
        import signal

        write_manifest(self.workspace, "wf-1", status="running", coordinator_pid=os.getpid())
        sent = []
        result = fusion_mcp.cancel_run(
            self.workspace, "wf-1",
            kill=lambda pid, sig: sent.append((pid, sig)),
            matches=lambda pid, wf: True,
        )
        self.assertTrue(result["cancelled"])
        self.assertEqual(sent, [(os.getpid(), signal.SIGINT)])


class ActiveWorkflowVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_running_workflow_older_than_the_display_window_is_still_active(self):
        # Orientation shows ten runs but must never hide a live one behind them.
        import os

        for index in range(14):
            write_manifest(self.workspace, f"wf-{index:03d}", status="success")
        write_manifest(
            self.workspace, "wf-000-long", status="running", coordinator_pid=os.getpid()
        )
        state = fusion_mcp.orientation(self.workspace)
        self.assertEqual(len(state["recent_workflows"]), 10)
        self.assertIn("wf-000-long", [w["workflow_id"] for w in state["active_workflows"]])
        self.assertIn("running", state["next"])


class ToolContractTest(unittest.TestCase):
    def test_every_async_tool_is_dispatchable_and_declares_schemas(self):
        for tool in fusion_mcp.ASYNC_TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertIn("inputSchema", tool)
                self.assertIn("outputSchema", tool)
                self.assertTrue(tool["description"].strip())
                # Dispatch must recognize it; bad args may still raise ValueError.
                try:
                    fusion_mcp.dispatch_async_tool(tool["name"], {}, Path(tempfile.gettempdir()))
                except ValueError as exc:
                    self.assertNotIn("unknown tool", str(exc))
                except Exception:
                    pass

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ValueError) as raised:
            fusion_mcp.dispatch_async_tool("nope", {}, Path(tempfile.gettempdir()))
        self.assertIn("unknown tool", str(raised.exception))

    def test_capabilities_match_the_methods_the_server_answers(self):
        self.assertEqual(
            set(fusion_mcp.SERVER_CAPABILITIES), {"tools", "prompts", "resources"}
        )


if __name__ == "__main__":
    unittest.main()
