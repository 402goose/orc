"""Real Git objects/worktrees/remotes, fake GitHub API, no external publication."""
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
import fusion_publish as pub
from fusion_workflow import WorkflowRunner


def seed_git(root):
    workspace, remote = root / "repo", root / "remote.git"
    workspace.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "staging", str(workspace)], check=True, capture_output=True)
    for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid"), ("commit.gpgsign", "false")):
        pub.git(workspace, "config", key, value)
    (workspace / "app.txt").write_text("before\n")
    (workspace / "other.txt").write_text("unrelated baseline\n")
    (workspace / ".gitignore").write_text(".fusion/\n")
    pub.git(workspace, "add", ".")
    pub.git(workspace, "commit", "-m", "initial")
    pub.git(workspace, "remote", "add", "origin", "git@github.com:fixture/project.git")
    pub.git(workspace, "config", f"url.{remote}.insteadOf", "git@github.com:fixture/project.git")
    pub.git(workspace, "push", "origin", "staging")
    pub.git(workspace, "fetch", "origin")
    return workspace, remote


def manifest(workspace, run_id="wf-test", review_tree=None):
    data = {"workflow_id": run_id, "task": "Fix payment routing", "status": "success", "nodes": {
        "implement": {"id": "implement", "write": True, "role": "implementation", "status": "success", "needs": [],
                      "result": {"agent": "codex", "changed": ["app.txt"], "tests": ["fixture checks passed"], "summary": "Fixed routing"}},
        "review": {"id": "review", "write": False, "role": "review", "status": "success", "needs": ["implement"],
                   "result": {"agent": "agy", "tests": ["independent check passed"], **({"reviewed_tree": review_tree} if review_tree else {})}}}}
    pub.save(pub.root_for(workspace, run_id) / "manifest.json", data)
    return data


class FakeGithub:
    def __init__(self):
        self.pr = None
        self.calls = []
        self.fail_after_create = False

    def __call__(self, workspace, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "list"):
            return json.dumps([self.pr] if self.pr else [])
        if args[:2] == ("pr", "create"):
            self.pr = {"number": 17, "url": "https://github.com/fixture/project/pull/17", "state": "OPEN",
                       "headRefName": args[args.index("--head") + 1], "baseRefName": args[args.index("--base") + 1]}
            if self.fail_after_create:
                raise ValueError("connection lost after PR creation")
            return self.pr["url"]
        if args[:2] == ("pr", "view"):
            return json.dumps({**self.pr, "statusCheckRollup": [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]})
        raise AssertionError(args)


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace, self.remote = seed_git(self.root)
        self.github = FakeGithub()
        self.mock = patch.object(pub, "gh", self.github)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.env = patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {**core.DEFAULTS, "publish": {"mode": "manual", "base": "staging", "remote": "origin", "draft": True}}

    def legacy(self):
        manifest(self.workspace)
        (self.workspace / "app.txt").write_text("fixed\n")
        return pub.preview(self.workspace, "wf-test", self.config)

    def test_legacy_preview_preserves_index_and_excludes_unrelated_edits(self):
        (self.workspace / "other.txt").write_text("private unrelated edit\n")
        pub.git(self.workspace, "add", "other.txt")
        before = pub.git(self.workspace, "diff", "--cached")
        draft = self.legacy()
        self.assertEqual(pub.git(self.workspace, "diff", "--cached"), before)
        self.assertEqual(draft["files"], ["app.txt"])
        with self.assertRaisesRegex(ValueError, "older run"):
            pub.publish(self.workspace, "wf-test", self.config, {"snapshot_id": draft["snapshot_id"]})
        result = pub.publish(self.workspace, "wf-test", self.config, {"snapshot_id": draft["snapshot_id"], "accept_legacy_diff": True})
        self.assertEqual(result["status"], "published")
        self.assertEqual(pub.text(self.workspace, "branch", "--show-current"), "staging")
        self.assertEqual(pub.git(self.workspace, "diff", "--cached"), before)
        tree_dir = Path(result["worktree"])
        self.assertEqual((tree_dir / "other.txt").read_text(), "unrelated baseline\n")
        self.assertEqual(pub.text(tree_dir, "rev-parse", "HEAD^{tree}"), draft["tree"])
        create = next(c for c in self.github.calls if c[:2] == ("pr", "create"))
        self.assertIn("--draft", create)
        self.assertEqual(create[create.index("--base") + 1], "staging")
        self.assertTrue(Path(create[create.index("--body-file") + 1]).is_file())

    def test_rejects_stale_preview_before_commit_or_push(self):
        draft = self.legacy()
        (self.workspace / "app.txt").write_text("new unreviewed edit\n")
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            pub.publish(self.workspace, "wf-test", self.config, {"snapshot_id": draft["snapshot_id"], "accept_legacy_diff": True})
        self.assertFalse(self.github.calls)
        self.assertFalse(Path(draft["worktree"]).exists())

    def test_retry_recovers_pr_created_before_network_failure_without_duplicates(self):
        draft = self.legacy()
        self.github.fail_after_create = True
        request = {"snapshot_id": draft["snapshot_id"], "accept_legacy_diff": True}
        with self.assertRaisesRegex(ValueError, "connection lost"):
            pub.publish(self.workspace, "wf-test", self.config, request)
        result = pub.publish(self.workspace, "wf-test", self.config, request)
        self.assertEqual(result["status"], "published")
        self.assertEqual(len([c for c in self.github.calls if c[:2] == ("pr", "create")]), 1)
        self.assertEqual(pub.publish(self.workspace, "wf-test", self.config, request)["url"], result["url"])
        self.assertEqual(pub.refresh_pr(self.workspace, "wf-test")["pr"]["statusCheckRollup"][0]["conclusion"], "SUCCESS")

    def test_commit_hook_failure_is_retryable(self):
        draft = self.legacy()
        hook = self.workspace / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        request = {"snapshot_id": draft["snapshot_id"], "accept_legacy_diff": True}
        with self.assertRaises(ValueError):
            pub.publish(self.workspace, "wf-test", self.config, request)
        self.assertFalse(self.github.calls)
        hook.unlink()
        self.assertEqual(pub.publish(self.workspace, "wf-test", self.config, request)["status"], "published")

    def test_snapshot_captures_staged_deletions_and_binary_untracked_files(self):
        pub.git(self.workspace, "rm", "app.txt")
        (self.workspace / "binary.bin").write_bytes(b"\x00\xff\x01")
        tree = pub.snapshot(self.workspace)
        files = pub.text(self.workspace, "ls-tree", "-r", "--name-only", tree)
        self.assertNotIn("app.txt", files)
        self.assertIn("binary.bin", files)

    def test_failed_unreviewed_and_discovery_workflows_cannot_publish(self):
        for alteration in ("failed", "no-review", "review-before-write", "discovery"):
            data = manifest(self.workspace)
            if alteration == "failed": data["status"] = "failed"
            if alteration == "no-review": del data["nodes"]["review"]
            if alteration == "review-before-write": data["nodes"]["review"]["needs"] = []
            if alteration == "discovery": data["nodes"]["implement"]["write"] = False
            pub.save(pub.root_for(self.workspace, "wf-test") / "manifest.json", data)
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                pub.preview(self.workspace, "wf-test", self.config)

    def test_new_isolated_run_auto_publishes_exact_reviewed_tree(self):
        (self.workspace / "app.txt").write_text("existing user edit\n")
        config = core.deep_merge(self.config, {"codex": {"command": sys.executable}})
        spec = {"task": "Fix routing", "publish": {**self.config["publish"], "mode": "auto"}, "nodes": [
            {"id": "implement", "agent": "codex", "role": "implementation", "write": True, "task": "Implement"},
            {"id": "review", "agent": "codex", "role": "review", "needs": ["implement"], "task": "Review"}]}
        dispatched = []
        def worker(config, task, store):
            dispatched.append(task)
            if task["write"]:
                (Path(task["workspace"]) / "app.txt").write_text("reviewed fix\n")
            return {"run_id": task["run_id"], "status": "success", "agent": "codex", "exit_code": 0,
                    "summary": "Verified", "changed": ["app.txt"] if task["write"] else [], "tests": ["fixture check"], "blockers": []}
        with patch.object(core, "dispatch", side_effect=worker):
            runner = WorkflowRunner(self.workspace, config, spec)
            result = runner.run()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual((self.workspace / "app.txt").read_text(), "existing user edit\n")
        self.assertNotEqual(runner.workspace, self.workspace)
        state = pub.read(runner.root / "publish.json")
        self.assertEqual(state["status"], "published", state)
        self.assertEqual(state["tree"], runner.nodes["review"]["result"]["reviewed_tree"])
        self.assertEqual(len(dispatched), 2)
        with patch.object(core, "dispatch", side_effect=AssertionError("Do not rerun workers")):
            self.assertEqual(pub.publish(self.workspace, runner.run_id, config, {})["url"], state["url"])

    def test_reviewed_worktree_cannot_be_published_after_later_edits(self):
        context = pub.setup_worktree(self.workspace, "wf-isolated", "Fix routing", self.config["publish"])
        worktree = Path(context["workspace"])
        (worktree / "app.txt").write_text("reviewed\n")
        manifest(self.workspace, "wf-isolated", pub.snapshot(worktree))
        (worktree / "app.txt").write_text("unreviewed\n")
        with self.assertRaisesRegex(ValueError, "differ from the accepted review"):
            pub.preview(self.workspace, "wf-isolated", self.config)
        with self.assertRaisesRegex(ValueError, "different target"):
            pub.preview(self.workspace, "wf-isolated", self.config, {"base": "main"})

    def test_changes_during_review_block_automatic_publication(self):
        config = core.deep_merge(self.config, {"codex": {"command": sys.executable}})
        spec = {"task": "Fix routing", "publish": {**self.config["publish"], "mode": "auto"}, "nodes": [
            {"id": "implement", "agent": "codex", "write": True, "task": "Implement"},
            {"id": "review", "agent": "codex", "role": "review", "needs": ["implement"], "task": "Review"}]}
        def worker(config, task, store):
            (Path(task["workspace"]) / "app.txt").write_text(task["role"])
            return {"run_id": task["run_id"], "status": "success", "agent": "codex", "exit_code": 0,
                    "summary": "Done", "changed": ["app.txt"], "tests": ["fixture check"], "blockers": []}
        with patch.object(core, "dispatch", side_effect=worker):
            result = WorkflowRunner(self.workspace, config, spec).run()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.github.calls)

    def test_invalid_branch_remote_and_hook_tree_mutations_are_rejected(self):
        for value in ("--upload-pack=bad", "../main", "branch;cmd"):
            with self.assertRaises(ValueError): pub.options({}, {"base": value})
        draft = self.legacy()
        hook = self.workspace / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook change\\n' > other.txt\ngit add other.txt\n")
        hook.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "changed the reviewed tree"):
            pub.publish(self.workspace, "wf-test", self.config, {"snapshot_id": draft["snapshot_id"], "accept_legacy_diff": True})
        self.assertFalse(self.github.calls)


if __name__ == "__main__": unittest.main()
