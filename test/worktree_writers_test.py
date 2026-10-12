"""Workflows whose writers each run in their own Git worktree and branch."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_publish as pub  # noqa: E402
from fusion_workflow import WorkflowRunner, resume_workflow, validate_spec  # noqa: E402


# A fake Codex worker. The plan file says, per node, what to write, whether to
# commit it, whether to fail, and whether to wait at a barrier until every
# barrier node has started. Each call records where it ran and what it saw.
WORKER = r'''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time
prompt = sys.stdin.read()
node = re.search(r"You are node (\S+) in", prompt)[1]
plan = json.loads(pathlib.Path(os.environ["WT_PLAN"]).read_text()).get(node, {})
calls = pathlib.Path(os.environ["WT_CALLS"])
record = {"node": node, "cwd": os.getcwd(),
          "seen": sorted(p.name for p in pathlib.Path(".").iterdir() if p.name not in {".git", ".fusion"})}
if plan.get("barrier"):
    barrier = pathlib.Path(os.environ["WT_BARRIER"])
    (barrier / node).touch()
    deadline = time.time() + 30
    while len(list(barrier.iterdir())) < int(os.environ["WT_BARRIER_N"]) and time.time() < deadline:
        time.sleep(0.05)
    record["met_at_barrier"] = len(list(barrier.iterdir()))
for name, text in (plan.get("write") or {}).items():
    pathlib.Path(name).write_text(text)
if plan.get("commit"):
    subprocess.run(["git", "add", "-A", "--", "."], check=True)
    subprocess.run(["git", "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-qm", "worker: " + node], check=True)
(calls / f"{node}-{time.time_ns()}.json").write_text(json.dumps(record))
if plan.get("fail"):
    text = "STATUS: error\nSUMMARY: could not finish\nCHANGED: none\nTESTS: none\nBLOCKERS: it broke"
else:
    text = "STATUS: success\nSUMMARY: done\nCHANGED: some\nTESTS: none\nBLOCKERS: none"
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''


class WorktreeWritersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.calls = self.root / "calls"
        self.calls.mkdir()
        self.barrier = self.root / "barrier"
        self.barrier.mkdir()
        self.plan = self.root / "plan.json"
        worker = self.root / "codex-worker"
        worker.write_text(WORKER)
        worker.chmod(0o755)
        self.config = {"codex": {"command": str(worker)}, "claude": {"command": "missing-claude"},
                       "timeout_seconds": 60}
        environment = patch.dict(os.environ, {
            "FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0", "ORC_HOME": str(self.root / "orc-home"),
            "WT_PLAN": str(self.plan), "WT_CALLS": str(self.calls), "WT_BARRIER": str(self.barrier),
            "WT_BARRIER_N": "4"})
        environment.start()
        self.addCleanup(environment.stop)
        for argv in (["init", "-q", "-b", "main"], ["config", "user.name", "Fixture"],
                     ["config", "user.email", "fixture@example.invalid"], ["config", "commit.gpgsign", "false"]):
            pub.git(self.workspace, *argv)
        (self.workspace / "app.txt").write_text("base\n")
        (self.workspace / ".gitignore").write_text(".fusion/\n")
        pub.git(self.workspace, "add", ".")
        pub.git(self.workspace, "commit", "-qm", "base")
        self.base = pub.text(self.workspace, "rev-parse", "HEAD")

    def run_spec(self, plan, nodes, **spec):
        self.plan.write_text(json.dumps(plan))
        runner = WorkflowRunner(self.workspace, self.config, {
            "task": "parallel writers", "writer_isolation": "worktree", "nodes": nodes, **spec})
        return runner, runner.run()

    def recorded(self):
        calls = {}
        for path in sorted(self.calls.iterdir(), key=lambda p: int(p.stem.rsplit("-", 1)[1])):
            call = json.loads(path.read_text())
            calls.setdefault(call["node"], []).append(call)
        return calls

    def branch_files(self, start, branch):
        return pub.text(self.workspace, "diff", "--name-only", start, branch).splitlines()

    def assert_base_untouched(self):
        self.assertEqual(pub.text(self.workspace, "rev-parse", "HEAD"), self.base)
        self.assertEqual(pub.text(self.workspace, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), [".fusion", ".git", ".gitignore", "app.txt"])

    def test_four_writers_run_concurrently_each_in_its_own_worktree_and_branch(self):
        ids = ["w1", "w2", "w3", "w4"]
        plan = {node: {"write": {f"{node}.txt": node}, "barrier": True} for node in ids}
        runner, result = self.run_spec(plan, [{"id": node, "agent": "codex", "write": True, "task": f"write {node}.txt"}
                                              for node in ids], max_parallel=4, max_parallel_writers=4)
        self.assertEqual(result["status"], "success", [node["result"].get("blockers") for node in result["nodes"]])
        calls = self.recorded()
        worktrees = {node: (runner.root / "worktrees" / node).resolve() for node in ids}
        for node in ids:
            [call] = calls[node]
            # All four were inside their worker at the same time.
            self.assertEqual(call["met_at_barrier"], 4)
            self.assertEqual(Path(call["cwd"]).resolve(), worktrees[node])
            self.assertEqual(call["seen"], [".gitignore", "app.txt"])
            saved = result["worktrees"]["nodes"][node]
            branch = f"fusion/{runner.run_id}/{node}"
            self.assertEqual((saved["branch"], saved["start_sha"]), (branch, self.base))
            self.assertEqual(saved["head_sha"], pub.text(self.workspace, "rev-parse", branch))
            self.assertIn(f"{node}.txt", saved["diff_stat"])
            self.assertEqual(self.branch_files(self.base, branch), [f"{node}.txt"])
            # No writer saw or changed another writer's files.
            self.assertEqual(sorted(p.name for p in worktrees[node].iterdir() if p.name not in {".git", ".fusion"}),
                             [".gitignore", "app.txt", f"{node}.txt"])
            self.assertEqual(pub.text(worktrees[node], "status", "--porcelain"), "")
            node_result = runner.nodes[node]["result"]
            self.assertEqual((node_result["branch"], node_result["head_sha"]), (branch, saved["head_sha"]))
            self.assertEqual(Path(node_result["workspace"]), worktrees[node])
            self.assertNotIn("worktree_left", runner.nodes[node])
        self.assert_base_untouched()
        saved = json.loads((runner.root / "worktrees.json").read_text())
        self.assertEqual(saved, result["worktrees"])
        self.assertEqual(json.loads(runner.manifest_path.read_text())["worktrees"], saved)

    def test_a_writer_that_commits_its_own_output_is_accepted(self):
        plan = {"w": {"write": {"out.txt": "made"}, "commit": True}}
        runner, result = self.run_spec(plan, [{"id": "w", "agent": "codex", "write": True, "task": "make out.txt",
                                               "required_files": ["out.txt"]}])
        self.assertEqual(result["status"], "success", result["nodes"][0]["result"].get("blockers"))
        saved = result["worktrees"]["nodes"]["w"]
        # Nothing was left to commit: the branch tip is the worker's own commit.
        self.assertEqual(pub.text(self.workspace, "log", "-1", "--format=%s", saved["branch"]), "worker: w")
        self.assertEqual(self.branch_files(self.base, saved["branch"]), ["out.txt"])
        self.assert_base_untouched()

    def test_workflow_required_files_with_one_writer_are_checked_in_its_tree(self):
        nodes = [{"id": "w", "agent": "codex", "write": True, "task": "make out.txt"}]
        runner, result = self.run_spec({"w": {"write": {"out.txt": "made"}}}, nodes,
                                       acceptance={"required_files": ["out.txt"]})
        self.assertEqual(result["status"], "success", result.get("acceptance"))
        self.assert_base_untouched()
        runner, result = self.run_spec({"w": {"write": {"other.txt": "made"}}}, nodes,
                                       acceptance={"required_files": ["out.txt"]})
        self.assertNotEqual(result["status"], "success")
        self.assertIn("required workflow artifact is missing: out.txt", json.dumps(result))

    def test_a_reader_sees_its_writer_and_a_chained_writer_starts_from_its_tip(self):
        plan = {"w1": {"write": {"a.txt": "a"}}, "w2": {"write": {"b.txt": "b"}}}
        nodes = [
            {"id": "survey", "agent": "codex", "task": "look at the base"},
            {"id": "w1", "agent": "codex", "write": True, "task": "write a.txt"},
            {"id": "read", "agent": "codex", "needs": ["w1"], "task": "read a.txt"},
            {"id": "w2", "agent": "codex", "write": True, "needs": ["read"], "task": "write b.txt"},
            {"id": "review", "agent": "codex", "needs": ["w2", "w1"], "task": "review both"},
        ]
        runner, result = self.run_spec(plan, nodes)
        self.assertEqual(result["status"], "success", [node["result"].get("blockers") for node in result["nodes"]])
        calls = {node: items[0] for node, items in self.recorded().items()}
        tree = {node: (runner.root / "worktrees" / node).resolve() for node in ("w1", "w2")}
        self.assertEqual(Path(calls["survey"]["cwd"]).resolve(), self.workspace)
        self.assertEqual(Path(calls["read"]["cwd"]).resolve(), tree["w1"])
        self.assertIn("a.txt", calls["read"]["seen"])
        self.assertEqual(Path(calls["w2"]["cwd"]).resolve(), tree["w2"])
        self.assertIn("a.txt", calls["w2"]["seen"])
        self.assertEqual(Path(calls["review"]["cwd"]).resolve(), tree["w2"])
        self.assertEqual(calls["review"]["seen"], [".gitignore", "a.txt", "app.txt", "b.txt"])
        w1, w2 = result["worktrees"]["nodes"]["w1"], result["worktrees"]["nodes"]["w2"]
        self.assertEqual(w2["start_sha"], w1["head_sha"])
        self.assertEqual(self.branch_files(w2["start_sha"], w2["branch"]), ["b.txt"])
        self.assertEqual(self.branch_files(self.base, w2["branch"]), ["a.txt", "b.txt"])
        self.assertEqual(self.branch_files(self.base, w1["branch"]), ["a.txt"])
        self.assert_base_untouched()

    def test_a_writer_that_fails_its_last_attempt_leaves_its_worktree_and_resume_reuses_it(self):
        plan = {"w": {"write": {"half.txt": "partial"}, "fail": True}}
        runner, result = self.run_spec(plan, [{"id": "w", "agent": "codex", "write": True, "task": "write half.txt"}])
        self.assertEqual(result["status"], "failed")
        path = runner.root / "worktrees" / "w"
        self.assertEqual(runner.nodes["w"]["worktree_left"], str(path))
        self.assertEqual(result["worktrees"]["nodes"]["w"]["worktree_left"], str(path))
        self.assertEqual((path / "half.txt").read_text(), "partial")
        self.assertNotIn("head_sha", result["worktrees"]["nodes"]["w"])
        manifest = json.loads(runner.manifest_path.read_text())
        self.assertEqual(manifest["nodes"]["w"]["worktree_left"], str(path))
        self.assert_base_untouched()

        self.plan.write_text(json.dumps({"w": {"write": {"half.txt": "whole"}}}))
        resumed = resume_workflow(self.workspace, self.config, runner.run_id, max_attempts=2)
        self.assertEqual(resumed["status"], "success", resumed["nodes"][0]["result"].get("blockers"))
        [first, second] = self.recorded()["w"]
        self.assertEqual(first["cwd"], second["cwd"])
        self.assertIn("half.txt", second["seen"])
        saved = resumed["worktrees"]["nodes"]["w"]
        self.assertEqual((saved["path"], saved["branch"]), (str(path), f"fusion/{runner.run_id}/w"))
        self.assertNotIn("worktree_left", saved)
        self.assertNotIn("worktree_left", resumed["nodes"][0])
        self.assertEqual(self.branch_files(self.base, saved["branch"]), ["half.txt"])
        self.assert_base_untouched()

    def test_a_writer_whose_thread_crashes_leaves_its_worktree(self):
        self.plan.write_text("{}")
        runner = WorkflowRunner(self.workspace, self.config, {
            "writer_isolation": "worktree", "max_parallel_writers": 2,
            "nodes": [{"id": "w", "agent": "codex", "write": True, "task": "write"}]})
        with patch.object(WorkflowRunner, "_run_node", side_effect=RuntimeError("thread died")):
            result = runner.run()
        self.assertEqual(result["status"], "failed")
        path = runner.root / "worktrees" / "w"
        self.assertTrue(path.is_dir())
        self.assertEqual(runner.nodes["w"]["worktree_left"], str(path))
        self.assertEqual(json.loads((runner.root / "worktrees.json").read_text())["nodes"]["w"]["worktree_left"], str(path))
        self.assert_base_untouched()

    def test_a_node_commit_spawns_no_background_maintenance(self):
        trace = self.root / "trace.jsonl"
        with patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}):
            runner, result = self.run_spec({"w": {"write": {"out.txt": "x"}}},
                                           [{"id": "w", "agent": "codex", "write": True, "task": "write"}])
        self.assertEqual(result["status"], "success", result["nodes"][0]["result"].get("blockers"))
        argvs = [json.loads(line).get("argv", []) for line in trace.read_text().splitlines()]
        self.assertTrue(any("commit-tree" in argv for argv in argvs))
        self.assertFalse([argv for argv in argvs if "maintenance" in argv or "gc" in argv])
        self.assertEqual(pub.text(self.workspace, "log", "-1", "--format=%s", result["worktrees"]["nodes"]["w"]["branch"]),
                         "w: write")

    def test_a_base_with_uncommitted_tracked_changes_is_refused(self):
        (self.workspace / "app.txt").write_text("edited\n")
        with self.assertRaisesRegex(ValueError, "commit or stash"):
            WorkflowRunner(self.workspace, self.config, {
                "writer_isolation": "worktree", "nodes": [{"id": "w", "agent": "codex", "write": True, "task": "x"}]})
        self.assertFalse((self.workspace / ".fusion" / "workflows").exists())

    def test_the_caller_base_sha_is_the_writers_start(self):
        (self.workspace / "app.txt").write_text("later\n")
        pub.git(self.workspace, "commit", "-qam", "later")
        self.plan.write_text(json.dumps({"w": {"write": {"out.txt": "x"}}}))
        runner = WorkflowRunner(self.workspace, self.config, {
            "writer_isolation": "worktree", "nodes": [{"id": "w", "agent": "codex", "write": True, "task": "x"}]},
            worktree={"workspace": str(self.workspace), "base_sha": self.base})
        result = runner.run()
        self.assertEqual(result["status"], "success", result["nodes"][0]["result"].get("blockers"))
        saved = result["worktrees"]["nodes"]["w"]
        self.assertEqual((result["worktrees"]["base_sha"], saved["start_sha"]), (self.base, self.base))
        self.assertEqual(self.branch_files(self.base, saved["branch"]), ["out.txt"])


class WorktreeCommitNoteTest(unittest.TestCase):
    def test_a_claude_writer_is_told_fusion_commits_and_others_may_commit(self):
        from fusion_workflow import WORKTREE_COMMIT_NOTE
        self.assertIn("Do not commit or push", WORKTREE_COMMIT_NOTE["claude"])
        self.assertIn("Fusion commits", WORKTREE_COMMIT_NOTE["claude"])
        self.assertEqual(WORKTREE_COMMIT_NOTE.get("codex", WORKTREE_COMMIT_NOTE[None]),
                         "You may commit to this branch. Do not push.")


class WriterIsolationValidationTest(unittest.TestCase):
    def writer(self, node_id, *needs):
        return {"id": node_id, "agent": "codex", "write": True, "task": "write", "needs": list(needs)}

    def reader(self, node_id, *needs):
        return {"id": node_id, "agent": "codex", "task": "read", "needs": list(needs)}

    def refuses(self, pattern, **spec):
        with self.assertRaisesRegex(ValueError, pattern):
            validate_spec(spec)

    def test_parallel_writers_need_worktree_isolation(self):
        nodes = [self.writer("a"), self.writer("b")]
        self.refuses('writer_isolation: "worktree"', max_parallel_writers=2, nodes=nodes)
        self.refuses('writer_isolation: "worktree"', max_parallel_writers=2, writer_isolation="shared", nodes=nodes)
        self.refuses('writer_isolation must be "shared" or "worktree"', writer_isolation="branches", nodes=nodes)
        self.assertEqual(validate_spec({"max_parallel_writers": 2, "writer_isolation": "worktree",
                                        "nodes": nodes})["max_parallel_writers"], 2)

    def test_publishing_is_not_combined_with_worktree_isolation(self):
        self.refuses("Publishing cannot be combined", writer_isolation="worktree", publish={"mode": "manual"},
                     nodes=[self.writer("a"), self.reader("review", "a")])
        validate_spec({"writer_isolation": "worktree", "publish": {"mode": "off"}, "nodes": [self.writer("a")]})

    def test_workflow_required_files_are_not_combined_with_several_writers(self):
        self.refuses("acceptance.required_files cannot be combined", writer_isolation="worktree",
                     acceptance={"required_files": ["out.txt"]}, nodes=[self.writer("a"), self.writer("b")])
        validate_spec({"writer_isolation": "worktree", "acceptance": {"required_files": ["out.txt"]},
                       "nodes": [self.writer("a")]})
        validate_spec({"acceptance": {"required_files": ["out.txt"]}, "nodes": [self.writer("a"), self.writer("b")]})

    def test_a_writer_after_unrelated_writers_is_refused(self):
        self.refuses("merging writers is the caller's job", writer_isolation="worktree",
                     nodes=[self.writer("a"), self.writer("b"), self.reader("look", "a"), self.writer("c", "look", "b")])
        # Without isolation the shared tree needs no merge.
        validate_spec({"nodes": [self.writer("a"), self.writer("b"), self.writer("c", "a", "b")]})

    def test_a_reader_after_unrelated_writers_is_refused(self):
        self.refuses("no single tree to run in", writer_isolation="worktree",
                     nodes=[self.writer("a"), self.writer("b"), self.reader("review", "a", "b")])

    def test_writers_on_one_chain_are_accepted(self):
        validate_spec({"writer_isolation": "worktree", "nodes": [
            self.writer("a"), self.reader("look", "a"), self.writer("b", "look"), self.reader("review", "a", "b"),
            self.writer("c", "b", "a")]})

    def test_a_writer_id_must_name_a_branch(self):
        self.refuses("names a Git branch", writer_isolation="worktree", nodes=[self.writer("a..b")])
        self.refuses("names a Git branch", writer_isolation="worktree", nodes=[self.writer("a.lock")])


if __name__ == "__main__":
    unittest.main()
