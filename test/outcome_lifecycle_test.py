"""External grader lifecycle, using isolated stores and no worker processes."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_decisions import DecisionStore, read_jsonl, reviewed_labels, label_provenance
from fusion_policy import route_candidates, rank_by_outcomes, routing_report


class OutcomeLifecycleTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith('FUSION_')}
        env.update(ORC_HOME=str(self.workspace / 'home'), FUSION_TELEMETRY='0')
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        (self.workspace / '.fusion.json').write_text(json.dumps({
            'decisions': {'mode': 'shadow'},
            'routes': {'a': {'agent': 'codex'}, 'b': {'agent': 'codex'}}}))
        self.store = DecisionStore(self.workspace)
        self.runs = core.RunStore(self.workspace)
        for run_id, route in [('run-a', 'a'), ('run-b', 'b')]:
            directory = self.runs.runs / run_id
            directory.mkdir(parents=True)
            (directory / 'task.json').write_text(json.dumps({'run_id': run_id, 'task': 'Fix export'}))
            (directory / 'result.json').write_text(json.dumps({
                'status': 'success', 'summary': 'Fixed export', 'changed': ['export.py'],
                'tests': ['export check passed'], 'agent': 'codex', 'route': route}))
            with self.runs.traces_path.open('a') as stream:
                stream.write(json.dumps({'run_id': run_id, 'agent': 'codex', 'route': route,
                                         'status': 'success', 'end_time_ms': 1}) + '\n')
            self.store.append('routing_log', task_id=run_id, chosen=route,
                              candidates=[{'key': route, 'propensity': 1.0}])
        self.store.append('outcome', task_id='run-b', accepted=False, source='gate')

    def outcome(self, accepted=None, **kwargs):
        return core.record_outcome(self.workspace, 'run-a', accepted, **kwargs)

    def ranking(self):
        config, _ = core.load_config(self.workspace)
        task = core.make_task(self.workspace, 'auto', 'Fix export', 'implementation', [], [], None, False, True)
        with patch.object(core, 'executable', return_value=True), patch('fusion_usage.headroom', return_value=[]):
            candidates = route_candidates(config, task, core.RunStore(self.workspace))
        candidates = rank_by_outcomes([c for c in candidates if c['route'] in {'a', 'b'}], minimum=1, explore=False)
        return [(c['route'], c['checked_runs'], c['acceptance_rate']) for c in candidates]

    def report(self):
        return routing_report(read_jsonl(self.store.path))

    def test_stages_latest_measured_verdict_wins_and_withdraw_restores_ranking(self):
        before = self.ranking()
        self.outcome(True, stage='gate', reason='Frozen suite passed; reviewed output')
        self.assertEqual(self.ranking()[0], ('a', 1, 1.0))
        rejected = self.outcome(False, stage='land', reason='Merge validation failed: a free-text reason')
        self.assertEqual(rejected['stage'], 'land')
        self.assertIn(('a', 1, 0.0), self.ranking())
        self.assertEqual(self.report()['lanes'][0]['accepted'], 0)
        label_id = rejected['label']['decision_id']
        withdrawn = self.outcome(withdraw=True, reason='Grader dispute upheld')
        self.assertTrue(withdrawn['withdraw'])
        self.assertEqual(self.ranking(), before)
        self.assertEqual(self.report()['with_outcome'], 1)
        self.assertFalse(reviewed_labels(read_jsonl(self.store.path))[0].get(label_id))
        self.outcome(withdraw=True, reason='Repeated request')
        self.assertEqual(self.ranking(), before)
        self.outcome(True, stage='verify', reason='Regraded successfully')
        self.assertEqual(self.ranking()[0], ('a', 1, 1.0))
        self.outcome(False, stage='gate', reason='Latest event wins even for an earlier stage')
        self.assertIn(('a', 1, 0.0), self.ranking())
        audit = [e for e in read_jsonl(self.store.path) if e['event'] == 'outcome_withdraw']
        self.assertEqual(audit[0]['reason'], 'Grader dispute upheld')
        self.assertNotIn('accepted', audit[0])

    def test_review_is_measured_and_latest_append_wins(self):
        before = self.ranking()
        self.outcome(True, stage='review', reason='Lead checked the triage claim')
        self.assertEqual(self.ranking()[0], ('a', 1, 1.0))
        self.outcome(False, stage='review', reason='Claim points at the wrong file')
        self.assertIn(('a', 1, 0.0), self.ranking())
        self.outcome(unmeasured=True, stage='review')
        self.assertIn(('a', 1, 0.0), self.ranking())
        self.outcome(True, stage='gate')
        self.assertEqual(self.ranking()[0], ('a', 1, 1.0))
        self.outcome(False, stage='review')
        self.assertIn(('a', 1, 0.0), self.ranking())
        self.outcome(withdraw=True, stage='review', reason='Invalid spot check')
        self.assertEqual(self.ranking(), before)

    def test_unmeasured_preserves_ranking_and_labels_with_or_without_verdict(self):
        for accepted in (None, True, False):
            if accepted is not None:
                self.outcome(accepted, stage='gate', reason='Measured result')
            before = self.ranking(), self.report(), reviewed_labels(read_jsonl(self.store.path))
            payload = self.outcome(unmeasured=True, stage='verify', reason='Infrastructure unavailable')
            self.assertTrue(payload['unmeasured'])
            self.assertNotIn('accepted', payload)
            self.assertEqual((self.ranking(), self.report(), reviewed_labels(read_jsonl(self.store.path))), before)
        self.assertEqual(len([e for e in read_jsonl(self.store.path) if e['event'] == 'outcome_unmeasured']), 3)

    def test_withdraw_restores_independent_outcome_and_gate_labels(self):
        from fusion_decisions import DecisionEngine, ACCEPTANCE_QUESTIONS
        record = DecisionEngine(self.workspace, {'decisions': {'mode': 'shadow'}}).record_unscored(
            'acceptance', {'task': 'Fix export'}, ACCEPTANCE_QUESTIONS, {'task_id': 'run-a'})
        self.store.append('label', id=record['id'], answers={'failed_task': 'true'}, verified=True,
                          source='structural_gate', evidence='Original gate failed')
        self.store.append('outcome', task_id='run-a', accepted=False, source='gate')
        before = self.ranking()
        self.outcome(True, stage='gate', reason='External suite passed')
        self.outcome(withdraw=True, reason='External suite was invalid')
        self.assertEqual(self.ranking(), before)
        events = read_jsonl(self.store.path)
        self.assertEqual(reviewed_labels(events)[0][record['id']], {'failed_task': 'true'})
        self.assertEqual(label_provenance(events)[record['id']]['failed_task']['source'], 'structural_gate')
        self.assertEqual(self.report()['outcome_sources'], {'gate': 2})

    def test_withdraw_preserves_human_labels_even_when_automatic_labeling_disabled(self):
        label = self.outcome(True, reason='Verified export')['label']
        self.store.label(label['decision_id'], {'failed_task': 'true'}, 'Human checked missing export')
        with patch.dict(os.environ, {'FUSION_DECISIONS_MODE': 'off'}):
            self.outcome(withdraw=True, reason='External grader withdrawn')
        events = read_jsonl(self.store.path)
        self.assertEqual(reviewed_labels(events)[0][label['decision_id']], {'failed_task': 'true'})
        self.assertEqual(label_provenance(events)[label['decision_id']]['failed_task']['source'], 'human')

    def test_withdraw_does_not_restore_a_gate_label_retracted_by_gym(self):
        from fusion_gym import withdraw_untrusted_label
        label = self.outcome(True, reason='Verified export')['label']
        self.store.append('label', id=label['decision_id'], answers={'failed_task': 'true'}, verified=True,
                          source='structural_gate', evidence='Gate failed', replace=True)
        self.outcome(True, reason='External grader passed')
        withdraw_untrusted_label(self.workspace, {'verdict': 'tampered', 'key': 'task:lane', 'gate_label': label})
        events = read_jsonl(self.store.path)
        self.assertEqual(reviewed_labels(events)[0][label['decision_id']],
                         {'plausible': 'true', 'failed_task': 'false'})
        self.outcome(withdraw=True, reason='External grader also invalid')
        self.assertFalse(reviewed_labels(read_jsonl(self.store.path))[0].get(label['decision_id']))

    def test_unmeasured_and_withdraw_preserve_worker_error_fallback(self):
        spans = [json.loads(line) for line in self.runs.traces_path.read_text().splitlines()]
        spans[0].update(status='error', failure_class='worker_error')
        self.runs.traces_path.write_text(''.join(json.dumps(span) + '\n' for span in spans))
        before = self.ranking()
        self.assertIn(('a', 1, 0.0), before)
        self.outcome(unmeasured=True, reason='Infrastructure unavailable')
        self.assertEqual(self.ranking(), before)
        self.outcome(True, stage='gate', reason='External check passed')
        self.assertEqual(self.ranking()[0], ('a', 1, 1.0))
        self.outcome(withdraw=True, reason='Grader invalid')
        self.assertEqual(self.ranking(), before)

    def test_invalid_options_do_not_write(self):
        for args in ({}, {'accepted': 'false'}, {'accepted': True, 'withdraw': True},
                     {'withdraw': True, 'unmeasured': True}, {'unmeasured': 'yes'},
                     {'accepted': False, 'stage': 'ship'}, {'withdraw': True, 'reason': ' '}):
            with self.subTest(args=args):
                before = self.store.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.outcome(**args)
                self.assertEqual(self.store.path.read_bytes(), before)

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = core.main(['--workspace', str(self.workspace), 'outcome', 'run-a', *args])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_cli_and_legacy_behavior(self):
        legacy = self.cli('--accepted')
        self.assertTrue(legacy['accepted'])
        self.assertNotIn('stage', legacy)
        self.assertEqual(legacy['label']['status'], 'skipped')
        self.assertFalse(self.cli('--rejected', '--stage', 'land', '--reason', 'Land failed')['accepted'])
        self.assertTrue(self.cli('--accepted', '--stage', 'review')['accepted'])
        self.assertFalse(self.cli('--rejected', '--stage', 'review')['accepted'])
        self.assertTrue(self.cli('--unmeasured', '--reason', 'Infra failed')['unmeasured'])
        self.assertTrue(self.cli('--withdraw', '--reason', 'Disputed')['withdraw'])
        for flags in (['--accepted', '--withdraw'], ['--accepted', '--stage', 'ship']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.cli(*flags)

    def mcp(self, args):
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'fusion_outcome', 'arguments': {'run_id': 'run-a', **args}}}
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(request) + '\n')), contextlib.redirect_stdout(output):
            self.assertEqual(core.main(['--workspace', str(self.workspace), 'mcp-serve']), 0)
        return json.loads(output.getvalue())['result']

    def test_mcp_schema_and_dispatch(self):
        schema = next(t['inputSchema'] for t in core.tool_definitions() if t['name'] == 'fusion_outcome')
        self.assertEqual(schema['properties']['stage']['enum'], ['gate', 'verify', 'land', 'review'])
        self.assertTrue({'accepted', 'withdraw', 'unmeasured'} <= schema['properties'].keys())
        self.assertNotIn('accepted', schema['required'])
        for args in ({'accepted': True}, {'accepted': False, 'stage': 'land'}, {'accepted': True, 'stage': 'review'},
                     {'accepted': False, 'stage': 'review'},
                     {'unmeasured': True}, {'withdraw': True, 'reason': 'Disputed'}):
            self.assertFalse(self.mcp(args).get('isError'))
        for args in ({}, {'accepted': 'false'}, {'accepted': True, 'withdraw': True},
                     {'unmeasured': True, 'stage': 'ship'}):
            before = self.store.path.read_bytes()
            self.assertTrue(self.mcp(args).get('isError'))
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_control_workspace_stores_and_reads_lifecycle(self):
        worker = self.workspace / 'worker'
        worker.mkdir()
        before = self.ranking()
        with patch.dict(os.environ, {'FUSION_CONTROL_WORKSPACE': str(self.workspace)}):
            original = self.workspace
            self.workspace = worker
            try:
                self.cli('--accepted', '--stage', 'gate', '--reason', 'Passed')
                self.assertEqual(self.ranking()[0], ('a', 1, 1.0))
                self.mcp({'accepted': False, 'stage': 'land', 'reason': 'Failed'})
                self.assertIn(('a', 1, 0.0), self.ranking())
                self.cli('--unmeasured', '--reason', 'Unavailable')
                self.assertIn(('a', 1, 0.0), self.ranking())
                self.cli('--withdraw', '--reason', 'Disputed')
                self.assertEqual(self.ranking(), before)
            finally:
                self.workspace = original
        self.assertFalse((worker / '.fusion').exists())
        self.assertEqual(self.report()['with_outcome'], 1)
        self.assertFalse(any(reviewed_labels(read_jsonl(self.store.path))[0].values()))


if __name__ == '__main__':
    unittest.main()
