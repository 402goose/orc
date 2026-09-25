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
from fusion_workflow import WorkflowRunner, resume_workflow


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

    def ignored_corpus(self):
        corpus = self.workspace / 'fuzz/corpus'
        corpus.mkdir(parents=True)
        (self.workspace / '.gitignore').write_text('.fusion/\nfuzz/corpus/\n')
        for name in ['seed', 'delete-me']:
            (corpus / name).write_text('original\n')
        pub.git(self.workspace, 'add', '.gitignore')
        pub.git(self.workspace, 'add', '-f', 'fuzz/corpus/seed', 'fuzz/corpus/delete-me')
        pub.git(self.workspace, 'commit', '-m', 'tracked fixtures in ignored directory')
        return corpus

    def test_snapshot_updates_tracked_ignored_files_without_adding_ignored_output(self):
        corpus = self.ignored_corpus()
        (corpus / 'seed').write_text('updated\n')
        (corpus / 'delete-me').unlink()
        (corpus / 'generated').write_text('must stay ignored\n')
        index = (self.workspace / '.git/index').read_bytes()
        tree = pub.snapshot(self.workspace)
        self.assertEqual(pub.git(self.workspace, 'show', tree + ':fuzz/corpus/seed'), b'updated\n')
        files = pub.text(self.workspace, 'ls-tree', '-r', '--name-only', tree).splitlines()
        self.assertNotIn('fuzz/corpus/generated', files)
        self.assertNotIn('fuzz/corpus/delete-me', files)
        self.assertEqual((self.workspace / '.git/index').read_bytes(), index)

    def test_snapshot_includes_explicitly_staged_ignored_addition_only(self):
        corpus = self.ignored_corpus()
        (corpus / 'intentional').write_text('explicit user intent\n')
        (corpus / 'generated').write_text('not staged\n')
        pub.git(self.workspace, 'add', '-f', 'fuzz/corpus/intentional')
        index = (self.workspace / '.git/index').read_bytes()
        tree = pub.snapshot(self.workspace)
        files = pub.text(self.workspace, 'ls-tree', '-r', '--name-only', tree).splitlines()
        self.assertIn('fuzz/corpus/intentional', files)
        self.assertNotIn('fuzz/corpus/generated', files)
        self.assertEqual((self.workspace / '.git/index').read_bytes(), index)

    def test_scoped_snapshot_expands_directories_and_preserves_literal_names(self):
        corpus = self.ignored_corpus()
        (corpus / 'seed').write_text('scoped edit\n')
        (self.workspace / 'other.txt').write_text('unrelated\n')
        source = self.workspace / 'src'
        source.mkdir()
        (source / ':(magic)[1].txt').write_text('literal\n')
        (source / '.fusion').mkdir()
        (source / '.fusion/private.txt').write_text('not publishable\n')
        tree = pub.snapshot(self.workspace, ['fuzz/corpus', 'src'])
        self.assertEqual(pub.git(self.workspace, 'show', tree + ':other.txt'), b'unrelated baseline\n')
        self.assertEqual(pub.git(self.workspace, 'show', tree + ':src/:(magic)[1].txt'), b'literal\n')
        self.assertNotIn('private.txt', pub.text(self.workspace, 'ls-tree', '-r', '--name-only', tree))

    def test_snapshot_failure_stops_once_and_resume_reuses_accepted_implementation(self):
        config = core.deep_merge(self.config, {'codex': {'command': sys.executable}})
        spec = {'task': 'Fix routing', 'publish': self.config['publish'], 'max_attempts': 3, 'nodes': [
            {'id': 'implement', 'agent': 'codex', 'role': 'implementation', 'write': True, 'task': 'Implement'},
            {'id': 'review', 'agent': 'codex', 'role': 'review', 'needs': ['implement'], 'task': 'Review',
             'acceptance': {'required_handoff': ['tests']}}]}
        called = []
        def worker(config, task, store):
            called.append(task['role'])
            if task['write']:
                (Path(task['workspace']) / 'app.txt').write_text('fixed\n')
            return {'run_id': task['run_id'], 'status': 'success', 'agent': 'codex', 'exit_code': 0,
                    'summary': 'Verified', 'tests': ['fixture check'], 'changed': ['app.txt'] if task['write'] else [], 'blockers': []}
        with patch.object(core, 'dispatch', side_effect=worker):
            runner = WorkflowRunner(self.workspace, config, spec)
            with patch.object(pub, 'snapshot', side_effect=ValueError('snapshot fixture failure')):
                failed = runner.run()
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(called, ['implementation'])
            self.assertEqual(runner.nodes['review']['attempts'], 1)
            result = runner.nodes['review']['result']
            self.assertEqual(result['blockers'], ['snapshot fixture failure'])
            self.assertEqual(result['failure_phase'], 'snapshot_before_review')
            resumed = resume_workflow(self.workspace, config, runner.run_id, node_id='review', max_attempts=3)
        self.assertEqual(resumed['status'], 'success', resumed)
        self.assertEqual(called, ['implementation', 'review'])

    def test_post_review_snapshot_failure_preserves_worker_evidence(self):
        config = core.deep_merge(self.config, {'codex': {'command': sys.executable}})
        spec = {'task': 'Review snapshot', 'publish': self.config['publish'], 'nodes': [
            {'id': 'implement', 'agent': 'codex', 'role': 'implementation', 'write': True, 'task': 'Implement'},
            {'id': 'review', 'agent': 'codex', 'role': 'review', 'task': 'Review', 'needs': ['implement']}]}
        runner = WorkflowRunner(self.workspace, config, spec)
        result = {'status': 'success', 'agent': 'codex', 'tests': ['verified fixture'], 'blockers': [],
                  'artifacts': {'answer': 'saved answer.md'}, 'usage': {'output_tokens': 42}}
        with patch.object(pub, 'snapshot', side_effect=['tree', ValueError('after-review snapshot failed')]), patch.object(core, 'dispatch', return_value=result):
            payload = runner._run_node('review', 1)
        self.assertEqual(payload['result']['failure_phase'], 'snapshot_after_review')
        self.assertEqual(payload['result']['tests'], ['verified fixture'])
        self.assertEqual(payload['result']['artifacts'], {'answer': 'saved answer.md'})
        self.assertEqual(payload['result']['usage']['output_tokens'], 42)

    def test_snapshot_failure_never_spends_a_classifier_call_or_switches_workers(self):
        from fusion_policy import recovery
        node = {'id': 'review', 'agent': 'auto', 'attempts': 1}
        result = {'status': 'error', 'failure_phase': 'snapshot_before_review', 'blockers': ['git failed']}
        with patch('fusion_policy.DecisionEngine', side_effect=AssertionError('Classifier cannot repair a coordinator failure')):
            self.assertEqual(recovery(self.config, self.workspace, 'wf', node, result, False, 5), ('stop', None))

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

    def test_worktree_runs_land_in_the_parent_even_when_a_writer_replaces_the_fusion_link(self):
        from fusion_policy import route_candidates
        config = core.deep_merge(self.config, {"codex": {"command": sys.executable}})
        spec = {"task": "Fix routing", "publish": self.config["publish"], "nodes": [
            {"id": "implement", "agent": "codex", "role": "implementation", "write": True, "task": "Implement"},
            {"id": "review", "agent": "codex", "role": "review", "needs": ["implement"], "task": "Review"}]}
        runs = {}
        def worker(config, task, store):
            worktree = Path(task["workspace"])
            if task["write"]:
                (worktree / ".fusion").unlink()
                (worktree / ".fusion").mkdir()
                (worktree / "app.txt").write_text("fixed\n")
            self.assertEqual(store.workspace, self.workspace)
            run_dir = store.create(task)
            result = {"run_id": task["run_id"], "status": "success", "agent": "codex", "exit_code": 0, "summary": "Verified",
                      "changed": ["app.txt"] if task["write"] else [], "tests": ["fixture check"], "blockers": []}
            store.write_json(run_dir / "result.json", result)
            store.trace_span(config, task, result, core.now_ms(), core.now_ms(), {})
            runs[task["role"]] = task["run_id"]
            return result
        with patch.object(core, "dispatch", side_effect=worker):
            runner = WorkflowRunner(self.workspace, config, spec)
            self.assertEqual(runner.run()["status"], "success")
        self.assertNotEqual(runner.workspace, self.workspace)
        self.assertEqual(list((runner.workspace / ".fusion").iterdir()), [])
        implement = runs["implementation"]
        spans = [s for s in core.RunStore(self.workspace).traces(limit=50) if s.get("run_id") == implement]
        self.assertEqual({s["agent"] for s in spans}, {"codex", "gate"})
        task = {"workspace": str(self.workspace), "write": True}
        def codex():
            return next(c for c in route_candidates(config, task, core.RunStore(self.workspace)) if c["key"] == "codex")
        self.assertEqual((codex()["runs"], codex()["checked_runs"]), (2, 0))
        self.assertTrue(core.record_outcome(self.workspace, implement, True, "Read the diff")["recorded"])
        self.assertEqual((codex()["runs"], codex()["checked_runs"], codex()["acceptance_rate"]), (2, 1, 1.0))

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
