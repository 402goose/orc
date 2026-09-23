"""Scouting contracts and resumable queues: real files/Git, fake workers/GitHub."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_truffle as truffle
import fusion_publish as pub
from publish_test import seed_git
from ui_test import seed_workspace
from fusion_ui import ControlRoom


def issue(number):
    return dict(number=number, title=f"Fix issue {number}", body="A reproducible bug", url=f"https://github.com/fixture/project/issues/{number}",
                state="OPEN", updatedAt="2026-09-23T01:00:00Z", labels=[], assignees=[])


def candidate(number):
    return dict(number=number, reason="A bounded fix with a local regression test", effort="small", risk="low",
                reproduction="Proposed test, not run: assert the corrected output", evidence=[dict(path="app.txt", line=1, quote="before")],
                plan=["Correct the output"], verification=["python3 -m unittest"], acceptance=["Output matches the regression test"])


class TruffleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace, self.remote = seed_git(self.root)
        self.config = core.deep_merge(core.DEFAULTS, seed_workspace(self.workspace))
        self.opts = dict(mode="manual", base="staging", remote="origin", draft=True)
        self.env = patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.issues = [issue(n) for n in range(1, 5)]
        self.prs = []
        self.calls = []
        self.gh = patch.object(truffle, "gh", side_effect=self.github)
        self.gh.start()
        self.addCleanup(self.gh.stop)

    def github(self, workspace, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "list"):
            return self.prs
        if args[:2] == ("issue", "list"):
            return self.issues
        if args[:2] == ("issue", "view"):
            return next(row for row in self.issues if row["number"] == int(args[2]))
        raise AssertionError(args)

    def draft_worker(self, value):
        def worker(config, task, store):
            self.assertFalse(task["write"])
            self.assertFalse(task["resume"])
            answer = self.workspace / ".fusion/runs" / task["run_id"] / "answer.md"
            answer.parent.mkdir(parents=True, exist_ok=True)
            answer.write_text("```truffle\n" + json.dumps(value) + "\n```")
            return dict(status="success", exit_code=0, run_id=task["run_id"], agent="codex")
        return worker

    def saved_hunt(self, count=2):
        scout_id = "truffle-123456789abc"
        rows, _ = truffle.parse_assessment("```truffle\n" + json.dumps({"candidates": [candidate(n) for n in range(1, count+1)], "skipped": []}) + "\n```", self.issues[:count], count, self.workspace)
        record = dict(id=scout_id, status="ready", repo="fixture/project", head=pub.text(self.workspace, "rev-parse", "HEAD"),
                      candidates=rows, skipped=[], target=count, scanned=count, started_at_ms=core.now_ms())
        pub.save(truffle.root_for(self.workspace, scout_id) / "hunt.json", record)
        return record

    def test_hunt_filters_inflight_and_assigned_and_accepts_shortfall(self):
        self.issues[1]["assignees"] = [{"login": "someone"}]
        self.prs = [dict(url="https://github.com/fixture/project/pull/80", closingIssuesReferences=[self.issues[2]])]
        value = dict(candidates=[candidate(1)], skipped=[dict(number=4, reason="Acceptance criteria are ambiguous")])
        with patch.object(core, "dispatch", side_effect=self.draft_worker(value)):
            result = truffle.hunt(self.workspace, self.config, count=3)
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["scanned"], 4)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(len(result["skipped"]), 3)
        self.assertIn("Linked open PR", result["skipped"][1]["reason"])
        self.assertFalse(any(c[:2] in [("issue", "comment"), ("pr", "create")] for c in self.calls))

    def test_zero_candidates_and_github_failures_are_explicit(self):
        self.issues = []
        with patch.object(core, "dispatch", side_effect=AssertionError("No worker needed")):
            result = truffle.hunt(self.workspace, self.config)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["candidates"], [])
        with patch.object(truffle, "gh", side_effect=ValueError("gh auth failed")):
            failed = truffle.hunt(self.workspace, self.config)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("gh auth failed", failed["message"])

    def test_fabricated_quotes_unknown_issues_and_scope_are_rejected(self):
        base = dict(candidates=[candidate(1)], skipped=[])
        mutations = [lambda v: v["candidates"][0].update(number=999),
                     lambda v: v["candidates"][0]["evidence"][0].update(quote="made up"),
                     lambda v: v["candidates"][0]["evidence"][0].update(path="../outside"),
                     lambda v: v["candidates"][0]["evidence"][0].update(line=99),
                     lambda v: v["candidates"][0].update(verification=[]),
                     lambda v: v["candidates"].append(candidate(1)),
                     lambda v: v["candidates"][0].update(risk="high")]
        for mutate in mutations:
            value = copy.deepcopy(base)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                truffle.parse_assessment("```truffle\n" + json.dumps(value) + "\n```", self.issues[:1], 2, self.workspace)
        with self.assertRaisesRegex(ValueError, "every supplied"):
            truffle.parse_assessment("```truffle\n" + json.dumps(base) + "\n```", self.issues, 2, self.workspace)

    def test_queue_uses_isolated_worktrees_and_does_not_repeat_accepted_work(self):
        record = self.saved_hunt()
        (self.workspace / "app.txt").write_text("user's unfinished edit\n")
        dispatched = []
        def worker(config, task, store):
            dispatched.append(task)
            if task["write"]:
                (Path(task["workspace"]) / "app.txt").write_text("fixed\n")
            return dict(status="success", run_id=task["run_id"], agent="codex", exit_code=0,
                        summary="Verified", tests=["fixture regression passed"], changed=["app.txt"] if task["write"] else [], blockers=[])
        with patch("fusion_build.source_for", side_effect=lambda idea, w: dict(request=idea, text=idea, issue=None)), patch.object(core, "dispatch", side_effect=worker):
            result = truffle.run_queue(self.workspace, self.config, record["id"], [1, 2], self.opts, 1)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(dispatched), 8)
        self.assertEqual(len({t["workspace"] for t in dispatched}), 2)
        self.assertNotIn(str(self.workspace), {t["workspace"] for t in dispatched})
        self.assertEqual((self.workspace / "app.txt").read_text(), "user's unfinished edit\n")
        with patch.object(core, "dispatch", side_effect=AssertionError("Duplicate worker")):
            again = truffle.run_queue(self.workspace, self.config, record["id"], [1, 2], self.opts)
        self.assertEqual(again["status"], "complete")
        self.assertEqual(len(list((self.workspace / ".fusion/workflows").glob("*/manifest.json"))), 2)

    def test_queue_pauses_on_failure_then_reuses_resumed_workflow(self):
        record = self.saved_hunt()
        def worker(config, task, store):
            return dict(status="error", run_id=task["run_id"], agent="codex", exit_code=1,
                        summary="quota", tests=[], changed=[], blockers=["quota exhausted"])
        with patch("fusion_build.source_for", side_effect=lambda idea, w: dict(request=idea, text=idea, issue=None)), patch.object(core, "dispatch", side_effect=worker):
            result = truffle.run_queue(self.workspace, self.config, record["id"], [1, 2], self.opts, 1)
        self.assertEqual(result["status"], "paused", result)
        self.assertNotIn("workflow_id", result["candidates"][1])
        with patch.object(core, "dispatch", side_effect=AssertionError("Do not duplicate unfinished work")):
            retried = truffle.run_queue(self.workspace, self.config, record["id"], [1, 2], self.opts)
        self.assertEqual(retried["status"], "paused")
        self.assertIn("Resume it", retried["message"])

    def test_stale_closed_linked_and_duplicate_issues_do_not_dispatch(self):
        record = self.saved_hunt()
        self.issues[0]["updatedAt"] = "2026-09-24T00:00:00Z"
        with patch.object(core, "dispatch", side_effect=AssertionError("Stale issue dispatched")):
            result = truffle.run_queue(self.workspace, self.config, record["id"], [1], self.opts)
        self.assertEqual(result["status"], "paused")
        self.assertIn("changed since scouting", result["message"])
        self.issues[0]["state"] = "CLOSED"
        self.prs = [dict(url="https://github.com/fixture/project/pull/1", closingIssuesReferences=[self.issues[1]])]
        with patch.object(core, "dispatch", side_effect=AssertionError("Closed issue dispatched")):
            result = truffle.run_queue(self.workspace, self.config, record["id"], [1, 2], self.opts)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(all(r["status"] == "skipped" for r in result["candidates"]))
        other = self.saved_hunt(1)
        other["id"] = "truffle-aaaaaaaaaaaa"
        other["candidates"][0]["workflow_id"] = "existing-run"
        pub.save(truffle.root_for(self.workspace, other["id"]) / "hunt.json", other)
        self.assertEqual(truffle.reserved_issues(self.workspace, "fixture/project", record["id"]), {1:"existing-run"})

    def test_receipt_reads_liveness_without_probing_a_process_group(self):
        record = self.saved_hunt(1)
        root = truffle.root_for(self.workspace, record["id"])

        # A record with no pid is not evidence of death: os.kill(0, 0) signals
        # the caller's own group and always succeeds, so probing it would mark
        # every such hunt permanently alive.
        pub.save(root / "hunt.json", {**record, "status": "scouting"})
        self.assertEqual(truffle.receipt(self.workspace, record["id"])["status"], "scouting")

        pub.save(root / "hunt.json", {**record, "status": "scouting", "pid": os.getpid()})
        self.assertEqual(truffle.receipt(self.workspace, record["id"])["status"], "scouting")

        pub.save(root / "hunt.json", {**record, "status": "scouting", "pid": 2 ** 22 - 1})
        self.assertEqual(truffle.receipt(self.workspace, record["id"])["status"], "interrupted")

        # A pid owned by another user raises PermissionError; it exists, and it
        # must not propagate out of receipt() and break the whole hunt list.
        pub.save(root / "hunt.json", {**record, "status": "running", "pid": 1})
        self.assertEqual(truffle.receipt(self.workspace, record["id"])["status"], "running")
        self.assertTrue(truffle.history(self.workspace))

    def test_receipt_reports_a_dead_workflow_coordinator(self):
        record = self.saved_hunt(1)
        root = truffle.root_for(self.workspace, record["id"])
        run_id = "20260923-000000-wf-deadbeef"
        manifest = self.workspace / ".fusion/workflows" / run_id / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"status": "running", "coordinator_pid": 2 ** 22 - 1}))
        record["candidates"][0]["workflow_id"] = run_id
        pub.save(root / "hunt.json", record)
        row = truffle.receipt(self.workspace, record["id"])["candidates"][0]
        self.assertEqual(row["workflow_status"], "interrupted", "a killed coordinator must not read as running")

    def test_paused_queue_tracks_manually_recovered_workflow_and_publication(self):
        record = self.saved_hunt()
        record.update(status='paused', selected=[1], publish={'mode':'auto'}, message='Old failure')
        record['candidates'][0].update(workflow_id='recovered', status='failed')
        root = truffle.root_for(self.workspace, record['id'])
        pub.save(root/'hunt.json', record)
        workflow = self.workspace/'.fusion/workflows/recovered'
        pub.save(workflow/'manifest.json', {'status':'failed'})
        self.assertEqual(truffle.receipt(self.workspace, record['id'])['status'], 'paused')
        pub.save(workflow/'manifest.json', {'status':'running', 'coordinator_pid':os.getpid()})
        self.assertEqual(truffle.receipt(self.workspace, record['id'])['status'], 'waiting')
        pub.save(workflow/'manifest.json', {'status':'success'})
        self.assertEqual(truffle.receipt(self.workspace, record['id'])['status'], 'paused', 'Auto publication still pending')
        pub.save(workflow/'publish.json', {'status':'published', 'url':'https://github.com/fixture/project/pull/9'})
        self.assertEqual(truffle.receipt(self.workspace, record['id'])['status'], 'complete')
        self.assertEqual(truffle.history(self.workspace)[0]['status'], 'complete')
        self.assertEqual(pub.read(root/'hunt.json')['status'], 'paused', 'Read must preserve the original coordinator receipt')
        record['selected'] = [1,2]
        pub.save(root/'hunt.json', record)
        restored = truffle.receipt(self.workspace, record['id'])
        self.assertEqual(restored['status'], 'ready')
        self.assertIn('Continue queue', restored['message'])
        self.assertFalse(restored['candidates'][1].get('workflow_id'), 'Read must not dispatch another issue')
        record['publish']['mode'] = 'manual'
        record['selected'] = [1]
        pub.save(root/'hunt.json', record)
        (workflow/'publish.json').unlink()
        self.assertEqual(truffle.receipt(self.workspace, record['id'])['status'], 'complete')

    def test_lock_validation_and_write_boundary(self):
        record = self.saved_hunt()
        with truffle.locked(self.workspace), self.assertRaisesRegex(ValueError, "already running"):
            with truffle.locked(self.workspace):
                self.fail("second coordinator acquired the lock")
        with self.assertRaises(ValueError):
            truffle.hunt_options(5, 2)
        with self.assertRaises(ValueError):
            truffle.selection(self.workspace, record["id"], [999])
        app = ControlRoom(self.workspace, self.root / "registry.json")
        with self.assertRaisesRegex(ValueError, "Enable workspace edits"):
            app.launch(self.workspace, dict(action="truffle-run", scout_id=record["id"], issues=[1], publish=self.opts))
        self.assertEqual(app.jobs(self.workspace), [])
        with self.assertRaisesRegex(ValueError, "own worktree"):
            truffle.run_queue(self.workspace, self.config, record["id"], [1], {**self.opts, "mode":"off"})

    def test_unfinished_publication_and_setup_failures_are_retryable_without_extra_work(self):
        record = self.saved_hunt(1)
        row = record["candidates"][0]
        row["workflow_id"] = "wf-publish"
        pub.save(truffle.root_for(self.workspace, record["id"]) / "hunt.json", record)
        run_root = self.workspace / ".fusion/workflows/wf-publish"
        pub.save(run_root / "manifest.json", {"status": "success"})
        pub.save(run_root / "git.json", {"mode": "auto"})
        for publication in ({"status": "failed"}, {"status": "pushing"}, {}):
            pub.save(run_root / "publish.json", publication)
            with patch.object(core, "dispatch", side_effect=AssertionError("Do not repeat the fix")):
                result = truffle.run_queue(self.workspace, self.config, record["id"], [1], self.opts)
            self.assertEqual(result["status"], "paused")
            self.assertIn("PR publication", result["message"])
        pub.save(run_root / "publish.json", {"status": "published", "url": "https://github.com/fixture/project/pull/1"})
        self.assertEqual(truffle.run_queue(self.workspace, self.config, record["id"], [1], self.opts)["status"], "complete")
        record = self.saved_hunt(1)
        with patch("fusion_build.source_for", side_effect=lambda idea, w: dict(request=idea, text=idea, issue=None)), patch("fusion_workflow.WorkflowRunner", side_effect=ValueError("target branch not found")):
            result = truffle.run_queue(self.workspace, self.config, record["id"], [1], self.opts)
        self.assertEqual(result["status"], "paused")
        self.assertNotIn("workflow_id", result["candidates"][0])
        self.assertIn("target branch", result["message"])


if __name__ == "__main__":
    unittest.main()
