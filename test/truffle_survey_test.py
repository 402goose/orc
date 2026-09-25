"""Complete inventories and resumable source-backed grading, without model calls."""
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_truffle as truffle
import fusion_truffle_survey as survey
import fusion_publish as pub
from publish_test import seed_git
from truffle_test import issue, candidate
from ui_test import seed_workspace
from fusion_ui import ControlRoom


def page(rows, more=False, cursor=None, total=None):
    return {'data': {'repository': {'issues': {'totalCount': total or len(rows),
        'nodes': [{**r, 'labels': {'nodes': r['labels']}, 'assignees': {'nodes': r['assignees']}} for r in rows],
        'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}


class WoodlandTest(unittest.TestCase):
    def test_pagination_maps_every_open_issue_and_rejects_incomplete_inventory(self):
        rows = [issue(n) for n in range(1, 238)]
        received = []
        with patch.object(truffle, 'gh', side_effect=[page(rows[:100], True, 'a', 237), page(rows[100:200], True, 'b', 237), page(rows[200:], total=237)]) as gh:
            result = survey.open_issues(Path('.'), 'fixture/project', lambda rows, total: received.append((len(rows), total)))
        self.assertEqual(len(result), 237)
        self.assertEqual(received, [(100,237),(200,237),(237,237)])
        self.assertIn('cursor=b', gh.call_args.args)
        self.assertIn('states:OPEN', gh.call_args.args[4])
        with patch.object(truffle, 'gh', side_effect=[page(rows[:100], True, 'a'), page(rows[:100], True, 'a')]), self.assertRaisesRegex(ValueError, 'did not advance'):
            survey.open_issues(Path('.'), 'fixture/project')
        with patch.object(truffle, 'gh', return_value={'errors':[{'message':'permission denied'}]}), self.assertRaisesRegex(ValueError, 'permission denied'):
            survey.open_issues(Path('.'), 'fixture/project')

    def test_patches_native_markdown_closed_parent_and_cycles(self):
        rows = [issue(n) for n in range(1, 9)]
        rows[0].update(title='[Epic] A leafy canopy', body='- [ ] #2\n- [ ] https://github.com/fixture/project/issues/3\nRelated foreign/project#4, https://github.com/foreign/project/issues/5\n#1 #999')
        rows[1].update(title='Tracking the child patch', body='- [ ] #3\n- [ ] #1')
        rows[2]['parent'] = {k:rows[0][k] for k in ('number','title','url','state')}
        rows[3]['body'] = 'Part of #1\nAn unrelated mention #2'
        rows[4].update(title='Fix telemetry tracking bug')
        rows[5]['parent'] = {'number':100,'title':'Old epic','state':'CLOSED','url':'https://github.com/fixture/project/issues/100'}
        rows[6].update(title='Removal program', body='#4 and #5')
        rows[7].update(title='General work', body='- [ ] https://github.com/fixture/project/issues/4\n- [ ] https://github.com/fixture/project/issues/5')
        issues, patches = survey.woodland(rows, 'fixture/project')
        by = {p['number']:p for p in patches}
        self.assertEqual(set(by), {1,2,7,8,100})
        self.assertEqual(set(by[1]['children']), {2,3,4})
        self.assertEqual(by[1]['outside_inventory'], [999])
        self.assertEqual(by[100]['children'], [6])
        self.assertEqual(issues[4]['grade'], 'U')
        self.assertEqual(issues[0]['grade'], 'P')
        self.assertEqual(len(issues[2]['patches']), 2)
        self.assertEqual(survey.references('`#1`\n```\n#2\n```\n#3 foreign/project#4 fixture/project#5', 'fixture/project'), {3,5})


class SurveyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace, _ = seed_git(self.root)
        self.config = core.deep_merge(core.DEFAULTS, seed_workspace(self.workspace))
        self.issues = [issue(n) for n in range(1, 11)]
        self.prs = []
        self.calls = []
        def gh(workspace, *args):
            self.calls.append(args)
            if args[:2] == ('api','graphql'):
                return page(copy.deepcopy(self.issues))
            if args[:2] == ('pr','list'):
                return self.prs
            if args[:2] == ('issue','view'):
                return copy.deepcopy(next(r for r in self.issues if r['number'] == int(args[2])))
            raise AssertionError(args)
        self.mock = patch.object(truffle, 'gh', side_effect=gh)
        self.mock.start(); self.addCleanup(self.mock.stop)

    def worker(self, config, task, store):
        record = survey.latest(self.workspace)
        self.assertEqual(record['active_run_id'], task['run_id'])
        batch = record['batches'][-1]
        self.assertEqual(batch['status'], 'running')
        self.assertFalse(task['write'])
        self.assertIn('Do not edit', task['prompt'] if 'prompt' in task else str(task))
        grades = [{'number':n, 'grade':'A' if n == 1 else 'C', 'reason':'Checked source; scoped fix' if n == 1 else 'Need reproduction'} for n in batch['numbers']]
        if grades[0]['number'] == 1:
            grades[0]['candidate'] = candidate(1)
        answer = self.workspace / '.fusion/runs' / task['run_id'] / 'answer.md'
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text('```truffle-grades\n' + json.dumps({'grades':grades}) + '\n```')
        return dict(status='success',exit_code=0,run_id=task['run_id'],agent='codex',model='fixture')

    def test_sync_only_retains_patches_and_policy_exclusions_without_workers(self):
        self.issues[0].update(title='Epic: Root system',body='#2 #3')
        self.issues[3]['assignees'] = [{'login':'owner'}]
        self.prs = [dict(url='https://github.com/fixture/project/pull/77', closingIssuesReferences=[self.issues[4]])]
        with patch.object(core, 'dispatch', side_effect=AssertionError('No worker for sync')):
            r = survey.survey(self.workspace, self.config, sync_only=True)
        self.assertEqual(r['status'], 'ready', r)
        self.assertEqual(len(r['issues']), 10)
        self.assertEqual(r['issues'][0]['grade'], 'P')
        self.assertEqual(r['issues'][3]['grade'], 'D')
        self.assertIn('Linked open PR', r['issues'][4]['reason'])
        self.assertEqual(survey.latest(self.workspace)['grade_counts'], dict(A=0,B=0,C=0,D=2,P=1,U=7))

    def test_failure_then_resume_only_grades_pending_and_saves_earlier_evidence(self):
        def fail_second(config, task, store):
            if len(survey.latest(self.workspace)['batches']) == 2:
                return dict(status='error',exit_code=1,agent='codex',summary='quota exhausted')
            return self.worker(config,task,store)
        with patch.object(core, 'dispatch', side_effect=fail_second) as dispatch:
            result = survey.survey(self.workspace, self.config)
        self.assertEqual(result['status'], 'paused', result)
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(survey.latest(self.workspace)['grade_counts']['U'], 2)
        self.assertEqual(len(result['candidates']), 1)
        with patch.object(core, 'dispatch', side_effect=self.worker) as dispatch:
            resumed = survey.survey(self.workspace, self.config, resume=result['id'])
        self.assertEqual(resumed['status'], 'ready', resumed)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(resumed['batches'][-1]['numbers'], [9,10])
        self.assertEqual(len(resumed['candidates']), 1)
        self.assertEqual(survey.latest(self.workspace)['grade_counts']['U'], 0)
        with patch.object(core, 'dispatch', side_effect=AssertionError('Duplicate call')):
            again = survey.survey(self.workspace, self.config, resume=result['id'])
        self.assertEqual(again['status'], 'ready')
        self.assertEqual(truffle.selection(self.workspace, result['id'], [1])['candidates'][0]['number'],1)
        with self.assertRaisesRegex(ValueError, 'Only shortlisted'):
            truffle.selection(self.workspace, result['id'], [9])

    def test_inventory_sync_can_coexist_with_worker_and_cancel_retains_grades(self):
        with truffle.locked(self.workspace), patch.object(core, 'dispatch', side_effect=AssertionError('No worker')):
            r = survey.survey(self.workspace, self.config, sync_only=True)
        self.assertEqual(r['status'], 'ready')
        def cancelled(config, task, store):
            if len(survey.latest(self.workspace)['batches']) > 1:
                raise survey.progress.WorkerCancelled('Stopped by user')
            return self.worker(config, task, store)
        with patch.object(core, 'dispatch', side_effect=cancelled):
            r=survey.survey(self.workspace, self.config, resume=r['id'])
        self.assertEqual(r['status'], 'interrupted')
        self.assertEqual(len(r['candidates']), 1)
        self.assertNotIn('active_run_id', r)
        self.assertEqual(survey.latest(self.workspace)['grade_counts']['U'], 2)

    def stale_survey(self, pid):
        """A survey left mid-flight, holding a pid it no longer owns."""
        with patch.object(core, 'dispatch', side_effect=self.worker):
            survey.survey(self.workspace, self.config, sync_only=True)
        record = survey.latest(self.workspace)
        path = truffle.root_for(self.workspace, record['id']) / 'hunt.json'
        saved = json.loads(path.read_text())
        saved.update(status='scouting', pid=pid)
        path.write_text(json.dumps(saved))
        return record['id']

    def test_a_recycled_pid_does_not_strand_the_woodland(self):
        """process_alive answers "is something alive", not "is it ours".

        After a crash or reboot the recorded pid gets recycled onto an
        unrelated program. Gating on liveness alone locked every saved grade
        behind a stranger that would never exit, with no override.
        """
        import os

        survey_id = self.stale_survey(os.getpid())   # alive, but not our coordinator
        with patch.object(core, 'process_matches', return_value=False):
            with patch.object(core, 'dispatch', side_effect=self.worker):
                survey.survey(self.workspace, self.config, resume=survey_id)  # must not raise

    def test_a_genuine_live_coordinator_still_blocks(self):
        import os

        survey_id = self.stale_survey(os.getpid())
        with patch.object(core, 'process_matches', return_value=True):
            with self.assertRaisesRegex(ValueError, "already has an active coordinator"):
                survey.survey(self.workspace, self.config, resume=survey_id)

    def test_takeover_overrides_a_coordinator_the_user_says_is_gone(self):
        import os

        survey_id = self.stale_survey(os.getpid())
        with patch.object(core, 'process_matches', return_value=True):
            with patch.object(core, 'dispatch', side_effect=self.worker):
                survey.survey(self.workspace, self.config, resume=survey_id, takeover=True)

    def test_stale_checkout_and_issue_pause_before_dispatch(self):
        r = survey.survey(self.workspace, self.config, sync_only=True)
        self.issues[1]['updatedAt'] = 'new date'
        with patch.object(core, 'dispatch', side_effect=AssertionError('Stale issue dispatched')):
            resumed = survey.survey(self.workspace, self.config, resume=r['id'])
        self.assertEqual(resumed['status'], 'paused')
        self.assertIn('Issue #2 changed', resumed['message'])
        (self.workspace/'app.txt').write_text('edited by user\n')
        with self.assertRaisesRegex(ValueError, 'Checkout changed'):
            survey.survey(self.workspace, self.config, resume=r['id'])

    def test_invalid_grades_and_unchecked_evidence_cannot_become_candidates(self):
        valid = {'grades':[{'number':1,'grade':'A','reason':'Bounded and verified','candidate':candidate(1)}]}
        def parse(value, rows=None):
            return survey.parse_grades('```truffle-grades\n'+json.dumps(value)+'\n```', rows or self.issues[:1], self.workspace)
        grades, candidates = parse(valid)
        self.assertEqual(candidates[0]['grade'], 'A')
        for mutate in [lambda v:v['grades'][0]['candidate']['evidence'][0].update(quote='fiction'),
                       lambda v:v['grades'][0]['candidate'].update(effort='medium'),
                       lambda v:v['grades'][0].update(grade='C'),
                       lambda v:v['grades'].append(v['grades'][0]),
                       lambda v:v['grades'][0].update(number=300),
                       lambda v:v['grades'][0].pop('candidate')]:
            value=copy.deepcopy(valid); mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError): parse(value)
        with self.assertRaisesRegex(ValueError, 'omitted'): parse(valid, self.issues[:2])

    def test_source_change_during_batch_rejects_grade(self):
        def mutate(config,task,store):
            r = self.worker(config,task,store)
            (self.workspace/'other.txt').write_text('concurrent change\n')
            return r
        with patch.object(core,'dispatch',side_effect=mutate):
            result=survey.survey(self.workspace,self.config)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(result['candidates'],[])
        self.assertIn('Checkout changed during grading',result['message'])

    def test_api_launch_passes_survey_flags_and_prevents_overlapping_jobs(self):
        app=ControlRoom(self.workspace,self.root/'registry.json')
        with patch('fusion_ui.subprocess.Popen'):
            job=app.launch(self.workspace,dict(action='truffle-survey',agent='codex',sync_only=True,include_assigned=True))
        args=str(pub.read(self.workspace / '.fusion/ui/jobs' / job['id'] / 'request.json')['argv'])
        self.assertIn('--sync-only',args)
        self.assertIn('--include-assigned',args)
        with patch.object(app,'jobs',return_value=[dict(status='running',action='truffle-survey')]), self.assertRaises(ValueError):
            app.launch(self.workspace,dict(action='truffle-hunt'))

    def test_api_launch_mints_a_survey_id_the_caller_can_navigate_to(self):
        """launch() must not make the frontend guess the new survey's id from the
        filesystem: between this call returning and the detached CLI writing its
        first hunt.json, the previous survey is still "latest" on disk."""
        app=ControlRoom(self.workspace,self.root/'registry.json')
        with patch('fusion_ui.subprocess.Popen'):
            first=app.launch(self.workspace,dict(action='truffle-survey',agent='codex',sync_only=True))
            second=app.launch(self.workspace,dict(action='truffle-survey',agent='codex',sync_only=True))
        for job in (first, second):
            self.assertRegex(job['survey_id'], r'\Atruffle-[a-f0-9]{12}\Z')
            request = pub.read(self.workspace / '.fusion/ui/jobs' / job['id'] / 'request.json')
            self.assertEqual(request['survey_id'], job['survey_id'])
        self.assertNotEqual(first['survey_id'], second['survey_id'])

    def test_survey_adopts_the_id_minted_by_its_caller(self):
        """fusion_ui.launch's minted id must round-trip through the environment
        into survey()'s own record id, the way FUSION_WORKFLOW_ID already does
        for workflow ids (fusion_workflow._run_id)."""
        chosen = 'truffle-' + 'ab' * 6
        with patch.dict(os.environ, {survey.SURVEY_ID_ENV: chosen}), patch.object(core, 'dispatch', side_effect=AssertionError('No worker for sync')):
            result = survey.survey(self.workspace, self.config, sync_only=True)
        self.assertEqual(result['id'], chosen)
        self.assertNotIn(survey.SURVEY_ID_ENV, os.environ)
        with patch.dict(os.environ, {survey.SURVEY_ID_ENV: 'not-a-valid-id'}), patch.object(core, 'dispatch', side_effect=AssertionError('No worker for sync')):
            fallback = survey.survey(self.workspace, self.config, sync_only=True)
        self.assertNotEqual(fallback['id'], 'not-a-valid-id')
        self.assertRegex(fallback['id'], r'\Atruffle-[a-f0-9]{12}\Z')


if __name__ == '__main__': unittest.main()
