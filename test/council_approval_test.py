"""Explicit automated approvals, audit provenance, revocation and live receipts."""
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
import fusion_garden as garden
from fusion_decisions import DecisionStore, read_jsonl
from fusion_labeling import suggest, approve_council
from fusion_learning import decision_rows, learning_summary
from fusion_ui import ControlRoom, atomic_json
from ui_test import seed_workspace


class CouncilApprovalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / 'repo'
        self.config = seed_workspace(self.workspace)
        self.store = DecisionStore(self.workspace)
        self.record = dict(id='decision', kind='review', status='ok', truncated=False, state='Review payment rounding.',
                           questions={'specialty': {'type':'choice','criteria':{'general':'general','payments':'payments'}},
                                      'needs_review':{'type':'noul'}}, schema_hash='fixture', prediction={}, context={})
        self.store.append('decision', **self.record)
        self.app = ControlRoom(self.workspace, Path(self.temp.name) / 'registry.json')

    def member(self, agent, specialty='payments', review='true'):
        values = {'specialty':specialty, 'needs_review':review}
        return dict(status='success', requested_agent=agent, agent=agent, model='fixture', run_id='run-'+agent,
                    answers={k:{'value':v,'reason':'Supported by original request','evidence':['E1']} for k,v in values.items() if v},
                    abstentions={k:'Missing supporting evidence' for k,v in values.items() if not v})

    def run_council(self, members=None, approval='council', policy=None):
        with patch('fusion_labeling.assessment', side_effect=members or [self.member('codex'),self.member('claude')]):
            return suggest(self.workspace, self.config, 'decision', labeling_mode='council',
                           council_agents=['codex','claude'], approval_mode=approval, garden_policy=policy)

    def labels(self):
        return [e for e in read_jsonl(self.store.path) if e['event']=='label']

    def test_opt_in_unanimous_approval_is_exported_with_council_provenance(self):
        draft = self.run_council(approval='human')
        self.assertEqual(self.labels(), [])
        result = self.run_council()
        self.assertEqual(result['approval']['status'], 'approved')
        event = self.labels()[0]
        self.assertEqual(event['source'], 'council_approved_suggestion')
        self.assertEqual(event['approval_rule'], 'unanimous')
        self.assertEqual([m['agent'] for m in event['reviewers']], ['codex','claude'])
        path = self.workspace / 'training.jsonl'
        exported = self.store.export(path)
        row = json.loads(path.read_text())
        self.assertEqual(row['labels'], {'specialty':'payments','needs_review':'true'})
        self.assertEqual(row['label_provenance']['specialty']['source'], 'council_approved_suggestion')
        self.assertEqual(exported['data_quality']['approval_sources'], {'council_approved_suggestion':2})
        self.assertEqual(learning_summary(self.workspace, self.config)['quality']['sources'], {'council_auto':2})
        self.assertEqual(self.app.label_runs(self.workspace)[0]['phase'], 'approved')
        approve_council(self.workspace, 'decision', result['suggestion_id'])
        self.assertEqual(len(self.labels()), 1)
        with self.assertRaisesRegex(ValueError, 'newer draft'):
            approve_council(self.workspace, 'decision', draft['suggestion_id'])

    def test_partial_approvals_leave_missing_answers_for_review_and_human_can_correct(self):
        result = self.run_council([self.member('codex',review=None),self.member('claude',review=None)])
        self.assertEqual(result['approval']['status'], 'partial')
        self.assertEqual(result['approval']['pending'], ['needs_review'])
        row = decision_rows(self.workspace)[0]
        self.assertEqual(row['garden_state'], 'needs_review')
        self.assertEqual(row['reviewed_answers'], {'specialty':'payments'})
        self.store.label('decision', {'specialty':'general'}, 'Human correction after reading the diff', result['suggestion_id'], replace=True)
        self.run_council()
        self.assertEqual(decision_rows(self.workspace)[0]['reviewed_answers'], {'specialty':'general'})
        self.assertEqual(len(self.labels()), 2)
        self.assertIn('Human-reviewed', self.app.label_runs(self.workspace)[0]['approval']['reason'])

    def test_disagreement_abstention_or_failed_member_does_not_approve(self):
        for members in ([self.member('codex',review=None),self.member('claude',specialty='general',review=None)],
                        [self.member('codex'), {'status':'error','requested_agent':'claude','error':'Quota exhausted'}]):
            result = self.run_council(members)
            self.assertEqual(result['approval']['status'], 'needs_review')
            self.assertEqual(self.labels(), [])
        with patch.object(core, 'dispatch') as dispatch, self.assertRaisesRegex(ValueError,'requires an agent council'):
            suggest(self.workspace, self.config, 'decision', approval_mode='council')
        dispatch.assert_not_called()

    def test_stale_excluded_and_human_reviewed_inputs_are_preserved(self):
        for mutation in ('stale', 'excluded', 'human'):
            with self.subTest(mutation=mutation):
                self.store.path.unlink(missing_ok=True)
                self.store.append('decision', **self.record)
                def assess(workspace, config, record, sources, agent, on_started=None):
                    if agent=='claude':
                        if mutation=='stale': self.store.append('decision', **{**self.record,'state':'Changed task'})
                        if mutation=='excluded': self.store.append('label_exclusion',id='decision',excluded=True)
                        if mutation=='human': self.store.label('decision',{'specialty':'general'},'Human evidence')
                    return self.member(agent)
                result = self.run_council(assess)
                self.assertEqual(result['approval']['status'], 'needs_review')
                self.assertFalse(any(e['source']=='council_approved_suggestion' for e in self.labels()))

    def test_garden_pause_and_changed_policy_revoke_inflight_approval(self):
        for mutation in ('pause', 'members'):
            configured = garden.save(self.app,self.workspace,dict(enabled=True,include_existing=True,labeling_mode='council',
                                      council_agents=['codex','claude'],approval_mode='council'))
            def assess(workspace, config, record, sources, agent, on_started=None):
                if agent=='claude':
                    garden.save(self.app,self.workspace,{'enabled':False} if mutation=='pause' else {'enabled':True,'council_agents':['codex','grok']})
                return self.member(agent)
            result = self.run_council(assess,policy=configured['policy_id'])
            self.assertEqual(result['approval']['status'], 'needs_review')
            self.assertIn('paused or its settings changed', result['approval']['reason'])
            self.assertEqual(self.labels(), [])

    def test_progress_records_votes_before_all_members_finish_and_cancellation_never_approves(self):
        seen = []
        def assess(workspace, config, record, sources, agent, on_started=None):
            on_started('run-'+agent)
            live = self.app.label_runs(workspace)[0]
            seen.append(copy.deepcopy(live))
            if agent=='claude': raise KeyboardInterrupt()
            return self.member(agent)
        with self.assertRaises(KeyboardInterrupt):
            self.run_council(assess)
        self.assertEqual(seen[0]['members'][0]['status'], 'running')
        self.assertEqual(seen[1]['members'][0]['answers']['specialty']['value'], 'payments')
        self.assertEqual(seen[1]['members'][1]['status'], 'running')
        self.assertEqual(self.app.label_runs(self.workspace)[0]['status'], 'interrupted')
        self.assertEqual(self.labels(), [])

    def test_garden_backlog_opt_in_and_one_attempt_per_policy(self):
        self.store.append('decision', **{**self.record,'time_ms':1})
        self.run_council(approval='human')
        settings = dict(enabled=True,labeling_mode='council',council_agents=['codex','claude'],approval_mode='council')
        garden.save(self.app,self.workspace,settings)
        self.assertEqual(garden.status(self.app,self.workspace)['queued'],0)
        garden.save(self.app,self.workspace,{**settings,'include_existing':True})
        self.assertEqual(garden.status(self.app,self.workspace)['queued'],1)
        launches=[]
        def launch(workspace,body,*,garden=False):
            launches.append(body)
            atomic_json(workspace/'.fusion/ui/jobs/fixture/job.json',dict(id='fixture',action='suggest-labels',status='failed',
                         decision_id=body['decision_id'],started_at_ms=1,garden=True,garden_policy=body['garden_policy']))
        with patch.object(self.app,'launch',side_effect=launch):
            garden.tick(self.app,self.workspace)
            garden.tick(self.app,self.workspace)
        self.assertEqual(len(launches),1)
        self.assertEqual(launches[0]['approval_mode'],'council')
        garden.save(self.app,self.workspace,{'enabled':False})
        garden.save(self.app,self.workspace,{'enabled':True})
        self.assertEqual(garden.status(self.app,self.workspace)['queued'],0)


if __name__ == '__main__': unittest.main()
