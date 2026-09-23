"""Automatic rounds use effective labels, independent trials, and durable jobs."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fusion_training_loop as loop
from fusion_decisions import DecisionStore, digest
from fusion_laya import dataset_rows
from fusion_publish import save
from fusion_ui import ControlRoom
from ui_test import seed_workspace


class TrainingLoopTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.w=Path(self.temp.name)/'repo';self.config=seed_workspace(self.w)
        self.env=patch.dict(os.environ,{'ORC_HOME':str(Path(self.temp.name)/'orc'),'FUSION_DECISIONS_MODE':'off'})
        self.env.start();self.addCleanup(self.env.stop)
        self.app=ControlRoom(self.w);self.store=DecisionStore(self.w);self.calls=[]
        self.seed(40)
        self.launch=patch.object(self.app,'launch',side_effect=self.worker)
        self.launch.start();self.addCleanup(self.launch.stop)

    def seed(self,count,start=0):
        q={'plausible':{'type':'noul','instructions':'Does the work satisfy the task?'}}
        for n in range(start,start+count):
            key='sample-'+str(n)
            self.store.append('decision',id=key,kind='acceptance',mode='shadow',status='ok',truncated=False,
                              state='Outcome '+str(n),questions=q,schema_hash=digest(q),prediction={},context={'group':key})
            self.store.label(key,{'plausible':'true' if n%2 else 'false'},'Verified outcome',replace=True)

    def worker(self,workspace,body,**kwargs):
        self.calls.append(copy.deepcopy(body))
        jid='job-'+str(len(self.calls));root=self.w/'.fusion/ui/jobs'/jid
        action=body['action'];result={}
        if action=='export': result=self.store.export(root/'dataset.jsonl')
        elif action=='train':
            result=dict(path=str(root/'candidate'),source_identity='source',model_identity='candidate',steps=12,
                        method='fixture decision head',train_groups=10,loss_curve=[dict(step=1,loss=.8),dict(step=12,loss=.3)])
            save(root/'candidate/training.json',result)
        elif action=='evaluate':
            rows=dataset_rows(body['dataset']);validation=[r for r in rows if r['split']=='validation']
            candidate=bool(body.get('model_path'))
            result=dict(path=str(root/'dataset.jsonl'),model_identities=['candidate' if candidate else 'source'],
                        accuracy=.8 if candidate else .6,correct=8 if candidate else 6,control_accuracy=.5,majority_accuracy=.5,
                        validation_questions=len(validation),validation_groups=len({r['group'] for r in validation}),
                        benchmark_hash=digest(validation),holdout={'status':'checked'},by_kind={})
            root.mkdir(parents=True,exist_ok=True)
            (root/'dataset.jsonl').write_text(Path(body['dataset']).read_text())
        elif action=='calibrate': result={'buckets':{'a':{'qualified':False}}}
        else: raise AssertionError(action)
        job=dict(id=jid,action=action,status='success',started_at_ms=len(self.calls),result=result,
                 learning_round=body.get('learning_round'),learning_dispatch=body.get('learning_dispatch'))
        save(root/'job.json',job);save(root/'request.json',{'learning':{'dataset':body.get('dataset',''),'model_path':body.get('model_path','')}})
        return job

    def enable(self): loop.configure(self.app,self.w,{'enabled':True})
    def finish(self):
        for _ in range(12): loop.tick(self.app,self.w)
        return loop.rounds(self.w)[0]

    def test_round_runs_all_steps_once_without_promoting_or_retraining_same_evidence(self):
        loop.tick(self.app,self.w);self.assertEqual(self.calls,[])
        before=(self.w/'.fusion.json').read_bytes()
        self.enable();r=self.finish()
        self.assertEqual(r['status'],'complete',r)
        self.assertEqual([b['action'] for b in self.calls],['export','evaluate','train','evaluate','calibrate'])
        self.assertEqual(r['proof']['outcome'],'gain')
        self.assertAlmostEqual(r['proof']['delta'],.2)
        self.assertEqual(r['results']['baseline']['benchmark_hash'],r['results']['evaluate']['benchmark_hash'])
        self.assertEqual((self.w/'.fusion.json').read_bytes(),before)
        self.assertEqual(loop.status(self.app,self.w)['state'],'gathering')
        self.store.label('sample-1',{'plausible':'true'},'Reapproved exact same answer',replace=True)
        self.finish();self.assertEqual(len(self.calls),5)
        self.seed(10,40);self.finish();self.assertEqual(len(self.calls),10)
        self.assertEqual(loop.status(self.app,self.w)['completed_rounds'],2)

    def test_pause_and_restart_preserve_next_step_and_failed_job_requires_retry(self):
        self.enable();loop.tick(self.app,self.w)
        loop.configure(self.app,self.w,{'enabled':False})
        loop.tick(self.app,self.w);self.assertEqual(len(self.calls),1)
        loop.configure(self.app,self.w,{'enabled':True});loop.tick(self.app,self.w)
        self.assertEqual(self.calls[-1]['action'],'evaluate')
        r=loop.rounds(self.w)[0];jid=r['active_job']
        job=self.app.job(self.w,jid);job.update(status='failed',error='fixture runtime stopped')
        save(self.w/'.fusion/ui/jobs'/jid/'job.json',job)
        loop.tick(self.app,self.w);loop.tick(self.app,self.w)
        self.assertEqual(loop.status(self.app,self.w)['state'],'needs_attention')
        self.assertEqual(len(self.calls),2)
        loop.configure(self.app,self.w,{'enabled':True,'retry':True})
        r=self.finish();self.assertEqual(r['status'],'complete',r)
        self.assertEqual(len(self.calls),6)
        self.assertEqual(len(r['attempts']),6)

    def test_corrected_answers_count_as_fresh_evidence_and_unknown_metrics_stay_unknown(self):
        self.enable();r=self.finish()
        self.store.label('sample-1',{'plausible':'false'},'Corrected after reproduction',replace=True)
        self.assertEqual(loop.status(self.app,self.w)['new_answers'],1)
        bad=copy.deepcopy(r);bad['results']['evaluate']['holdout']['status']='contaminated'
        self.assertEqual(loop.proof(bad)['outcome'],'unmeasured')
        bad=copy.deepcopy(r);bad['results']['baseline']['model_identities']=['wrong-source']
        self.assertIsNone(loop.proof(bad)['delta'])
        bad=copy.deepcopy(r);bad['results']['evaluate']['accuracy']=.4
        self.assertEqual(loop.proof(bad)['outcome'],'regression')
        self.assertTrue(any('control' in n for n in loop.proof(bad)['notes']))
        bad=copy.deepcopy(r);bad['results']['evaluate']['benchmark_hash']='another-benchmark'
        self.assertIsNone(loop.proof(bad)['delta'])

    def test_deduplication_keeps_validation_copy_and_withholds_conflicting_inputs(self):
        raw=self.w/'raw.jsonl';self.store.export(raw);rows=dataset_rows(raw)
        train=next(r for r in rows if r['split']=='train');validation=next(r for r in rows if r['split']=='validation')
        duplicate={**copy.deepcopy(train),'id':'duplicate-validation','group':validation['group'],'split':'validation'}
        conflict=copy.deepcopy(next(r for r in rows if r['id'] not in {train['id'],validation['id']}))
        conflict['id']='conflicting-copy';conflict['labels']['plausible']='false' if conflict['labels']['plausible']=='true' else 'true'
        rows.extend([duplicate,conflict]);raw.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        result=loop.curate(raw,self.w/'curated.jsonl');kept=dataset_rows(self.w/'curated.jsonl')
        self.assertNotIn(train['id'],[r['id'] for r in kept])
        self.assertIn('duplicate-validation',[r['id'] for r in kept])
        self.assertEqual(len(result['conflicting_ids']),2)
        self.assertFalse(result['data_quality']['cross_split_duplicates'])
        self.assertFalse(result['data_quality']['group_overlap'])
        self.assertEqual(len(dataset_rows(raw)),42,'Original export preserved')

    def test_saved_launch_intent_recovers_job_instead_of_dispatching_twice(self):
        self.enable();loop.tick(self.app,self.w)
        job_path=self.w/'.fusion/ui/jobs/job-1/job.json'
        job=json.loads(job_path.read_text());job.update(status='running',supervisor_pid=os.getpid())
        save(job_path,job)
        r=loop.rounds(self.w)[0];r['active_job']=None;r['jobs']={}
        save(loop.root(self.w)/'rounds'/r['id']/'round.json',r)
        loop.tick(self.app,self.w);self.assertEqual(len(self.calls),1)
        self.assertEqual(loop.status(self.app,self.w)['active_job']['id'],'job-1')
        self.assertEqual(loop.status(self.app,self.w)['new_answers'],0)
        job['status']='success';save(job_path,job)
        self.assertEqual(self.finish()['status'],'complete')
        self.assertEqual(len(self.calls),5)

    def test_invalid_settings_do_not_enable_and_manual_learning_slot_is_respected(self):
        for body in ({'enabled':'yes'},{'enabled':True,'min_new_answers':0},{'enabled':True,'min_new_answers':True}):
            with self.assertRaises(ValueError):loop.configure(self.app,self.w,body)
        self.assertFalse(loop.settings(self.w)['enabled'])
        self.enable()
        with patch.object(self.app,'jobs',return_value=[dict(action='train',status='running')]): loop.tick(self.app,self.w)
        self.assertEqual(self.calls,[])
        with patch.object(self.app,'jobs',return_value=[dict(action='train',status='running')]):
            with self.assertRaisesRegex(ValueError,'local learning job is already active'):
                ControlRoom.launch(self.app,self.w,{'action':'export'})

    def test_a_vanished_job_directory_does_not_take_down_the_whole_lab(self):
        """/api/decisions serves decisions, review, garden and training from one
        call, and configure() returns status too. A job directory that is gone -
        pruned, or absent in a copied or restored workspace - used to raise
        "Job does not exist" out of status(), 500ing all four and leaving no way
        to even turn the loop off.
        """
        self.enable()
        round = {'id': 'round-lost', 'number': 1, 'status': 'running', 'phase': 'baseline',
                 'active_job': '20260101-000000-deadbeef', 'tokens': {}, 'started_at_ms': 1}
        loop.save(loop.root(self.w) / 'rounds' / 'round-lost' / 'round.json', round)

        with patch.object(self.app, 'job', side_effect=ValueError('Job does not exist')):
            state = loop.status(self.app, self.w)          # must not raise
            self.assertIsNone(state['active_job'])
            self.assertEqual(state['state'], 'needs_attention')
            self.assertIn('no longer on disk', state['reason'])
            # and the user can still turn the loop off to recover
            self.assertFalse(loop.configure(self.app, self.w, {'enabled': False})['enabled'])

    def test_a_paused_loop_reports_paused_even_with_a_lost_job(self):
        round = {'id': 'round-lost', 'number': 1, 'status': 'running', 'phase': 'baseline',
                 'active_job': 'gone', 'tokens': {}, 'started_at_ms': 1}
        loop.save(loop.root(self.w) / 'rounds' / 'round-lost' / 'round.json', round)
        with patch.object(self.app, 'job', side_effect=ValueError('Job does not exist')):
            self.assertEqual(loop.status(self.app, self.w)['state'], 'paused')


if __name__=='__main__':unittest.main()
