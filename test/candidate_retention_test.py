"""Evaluated candidates lose their weights; promoted, configured, recent and active ones keep them."""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as core
import fusion_training_loop as loop
from fusion_decisions import config_for
from fusion_publish import save
from ui_test import seed_workspace

WEIGHTS = 1000


class CandidateRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.w = self.root / 'repo'
        self.config = seed_workspace(self.w)
        env = patch.dict(os.environ, {'ORC_HOME': str(self.root / 'orc'), 'FUSION_DECISIONS_MODE': 'off'})
        env.start()
        self.addCleanup(env.stop)
        self.jobs = self.w / '.fusion/ui/jobs'

    def configure(self, **decisions):
        self.config['decisions'] = {**self.config['decisions'], **decisions}
        save(self.w / '.fusion.json', self.config)

    def job(self, name, action, at, status='success', model_path=''):
        save(self.jobs / name / 'job.json', {'id': name, 'action': action, 'status': status, 'started_at_ms': at})
        save(self.jobs / name / 'request.json', {'action': action, 'learning': {'dataset': '', 'model_path': model_path}})

    def candidate(self, name, at, promoted=False, evaluated=True, status='success'):
        self.job(name, 'train', at, status)
        path = self.jobs / name / 'candidate'
        save(path / 'training.json', {'model_identity': name, 'promoted': promoted})
        save(path / 'rl_agent_config.json', {'max_len': 512})
        save(path / 'encoder/config.json', {'hidden_size': 8})
        save(path / 'tokenizer/tokenizer.json', {'model': 'fixture'})
        (path / 'model.safetensors').write_bytes(b'w' * WEIGHTS)
        if evaluated:
            self.job(name + '-eval', 'evaluate', at + 1, model_path=str(path))
        return path

    def weights(self, path):
        return (path / 'model.safetensors').is_file()

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = core.main(['--workspace', str(self.w), *argv])
        return code, out.getvalue()

    def test_an_evaluated_old_candidate_loses_its_weights_but_keeps_its_small_files(self):
        old = self.candidate('train-1', 100)
        middle, newest = self.candidate('train-2', 200), self.candidate('train-3', 300)
        result = loop.prune(self.w)
        self.assertEqual(result['status'], 'pruned', result)
        self.assertFalse(self.weights(old))
        self.assertEqual(result['removed'], [{'path': str(old / 'model.safetensors'), 'bytes': WEIGHTS}])
        for small in ('training.json', 'rl_agent_config.json', 'encoder/config.json', 'tokenizer/tokenizer.json'):
            self.assertTrue((old / small).is_file(), small)
        self.assertEqual(json.loads((old / 'training.json').read_text())['model_identity'], 'train-1')
        self.assertTrue((old.parent / 'job.json').is_file())
        self.assertTrue(self.weights(middle) and self.weights(newest))
        self.assertEqual((result['removed_bytes'], result['retained_bytes']), (WEIGHTS, 2 * WEIGHTS))

    def test_the_newest_keep_candidates_keep_their_weights(self):
        self.configure(training={'keep_candidates': 3})
        paths = [self.candidate(f'train-{n}', n * 100) for n in range(1, 5)]
        loop.prune(self.w)
        self.assertEqual([self.weights(p) for p in paths], [False, True, True, True])
        self.configure(training={'keep_candidates': 1})
        loop.prune(self.w)
        self.assertEqual([self.weights(p) for p in paths], [False, False, False, True])

    def test_other_large_weight_files_go_but_tokenizer_files_stay(self):
        old = self.candidate('train-1', 100)
        self.candidate('train-2', 200)
        self.candidate('train-3', 300)
        (old / 'encoder/model.safetensors').write_bytes(b'e' * 10)
        (old / 'pytorch_model.bin').write_bytes(b'b' * 10)
        (old / 'tokenizer/vocab.bin').write_bytes(b't' * 10)
        loop.prune(self.w)
        self.assertFalse((old / 'encoder/model.safetensors').exists() or (old / 'pytorch_model.bin').exists())
        self.assertTrue((old / 'tokenizer/vocab.bin').is_file())

    def test_a_promoted_or_configured_candidate_keeps_its_weights(self):
        promoted = self.candidate('train-1', 100, promoted=True)
        configured = self.candidate('train-2', 200)
        per_kind = self.candidate('train-3', 300)
        unknown = self.candidate('train-4', 400)
        save(unknown / 'training.json', {'model_identity': 'train-4'})
        self.candidate('train-5', 500)
        self.candidate('train-6', 600)
        self.configure(model_path=str(configured), checkpoints={'review': str(per_kind)})
        result = loop.prune(self.w)
        self.assertEqual(result['removed'], [])
        self.assertTrue(all(self.weights(p) for p in (promoted, configured, per_kind, unknown)))

    def test_a_round_marked_promoted_keeps_its_candidate(self):
        old = self.candidate('train-1', 100)
        self.candidate('train-2', 200)
        self.candidate('train-3', 300)
        save(loop.root(self.w) / 'rounds/round-a/round.json',
             {'id': 'round-a', 'status': 'complete', 'jobs': {'train': 'train-1'}, 'proof': {'promoted': True}})
        loop.prune(self.w)
        self.assertTrue(self.weights(old))

    def test_active_jobs_and_unfinished_rounds_are_untouched(self):
        running = self.candidate('train-1', 100, status='running', evaluated=False)
        evaluating = self.candidate('train-2', 200)
        self.job('eval-again', 'evaluate', 250, status='running', model_path=str(evaluating))
        in_round = self.candidate('train-3', 300)
        save(loop.root(self.w) / 'rounds/round-a/round.json',
             {'id': 'round-a', 'status': 'needs_attention', 'phase': 'calibrate', 'jobs': {'train': 'train-3'}})
        source = self.candidate('train-4', 400)
        save(loop.root(self.w) / 'rounds/round-b/round.json',
             {'id': 'round-b', 'status': 'running', 'phase': 'train', 'jobs': {}, 'source_path': str(source)})
        unevaluated = self.candidate('train-5', 500, evaluated=False)
        self.candidate('train-6', 600)
        self.candidate('train-7', 700)
        result = loop.prune(self.w)
        self.assertEqual(result['removed'], [])
        self.assertTrue(all(self.weights(p) for p in (running, evaluating, in_round, source, unevaluated)))

    def test_symlinked_weights_and_candidates_are_never_followed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'model.safetensors').write_bytes(b'o' * WEIGHTS)
        linked = self.candidate('train-1', 100)
        (linked / 'model.safetensors').unlink()
        (linked / 'model.safetensors').symlink_to(outside / 'model.safetensors')
        (linked / 'encoder/weights').symlink_to(outside, target_is_directory=True)
        whole = self.candidate('train-2', 200)
        for item in list(whole.iterdir()):
            item.rename(outside / ('whole-' + item.name))
        whole.rmdir()
        whole.symlink_to(outside, target_is_directory=True)
        self.candidate('train-3', 300)
        self.candidate('train-4', 400)
        result = loop.prune(self.w)
        self.assertEqual(result['removed'], [])
        self.assertTrue((linked / 'model.safetensors').is_symlink())
        self.assertEqual((outside / 'model.safetensors').read_bytes(), b'o' * WEIGHTS)
        self.assertTrue((outside / 'whole-model.safetensors').is_file())
        self.assertEqual(result['retained_bytes'], 2 * WEIGHTS)

    def kept_after(self, breakage):
        paths = [self.candidate(f'train-{n}', n * 100) for n in range(1, 5)]
        breakage()
        result = loop.prune(self.w)
        self.assertEqual(result['status'], 'kept', result)
        self.assertEqual(result['removed'], [])
        self.assertTrue(all(self.weights(p) for p in paths))

    def test_an_unreadable_config_keeps_everything(self):
        self.kept_after(lambda: (self.w / '.fusion.json').write_text('{not json'))

    def test_an_invalid_config_keeps_everything(self):
        self.kept_after(lambda: self.configure(training={'keep_candidates': -1}))

    def test_an_unreadable_round_keeps_everything(self):
        def breakage():
            path = loop.root(self.w) / 'rounds/round-a/round.json'
            path.parent.mkdir(parents=True)
            path.write_text('{')
        self.kept_after(breakage)

    def test_an_unreadable_job_record_keeps_everything(self):
        self.kept_after(lambda: (self.jobs / 'train-1-eval/job.json').write_text('['))

    def test_a_job_without_a_record_keeps_everything(self):
        self.kept_after(lambda: (self.jobs / 'half-made').mkdir())

    def test_keep_candidates_is_validated_and_not_a_training_objective_setting(self):
        options = config_for({'decisions': {'training': {'keep_candidates': 5, 'label_smoothing': 0.1}}})
        self.assertEqual(options['keep_candidates'], 5)
        self.assertNotIn('keep_candidates', options['training'])
        self.assertEqual(config_for({})['keep_candidates'], 2)
        for bad in (-1, '2', True, 1.5):
            with self.assertRaises(ValueError):
                config_for({'decisions': {'training': {'keep_candidates': bad}}})

    def test_status_reports_retained_bytes_and_prune_dry_run_removes_nothing(self):
        old = self.candidate('train-1', 100)
        self.candidate('train-2', 200)
        self.candidate('train-3', 300)
        code, output = self.cli('learn', 'status')
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)[0]['training']['candidate_bytes'], 3 * WEIGHTS)
        code, output = self.cli('learn', 'prune', '--dry-run', '--json')
        self.assertEqual(code, 0, output)
        [value] = json.loads(output)
        self.assertEqual((value['status'], value['removed_bytes']), ('dry_run', WEIGHTS))
        self.assertTrue(self.weights(old))
        code, output = self.cli('learn', 'prune')
        self.assertEqual(code, 0, output)
        self.assertIn(f'removed {WEIGHTS} bytes; {2 * WEIGHTS} bytes', output)
        self.assertFalse(self.weights(old))
        code, output = self.cli('learn', 'status')
        self.assertEqual(json.loads(output)[0]['training']['candidate_bytes'], 2 * WEIGHTS)


if __name__ == '__main__':
    unittest.main()
