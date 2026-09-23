"""Quality and measured change must reflect retained labels and comparable evidence."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fusion_decisions import digest
from fusion_quality import dataset_quality, input_key, matched_comparisons, review_quality
from fusion_laya import evaluate


class QualityTest(unittest.TestCase):
    def row(self, key, state, split='train', value='false'):
        questions = {'ok': {'type': 'noul', 'instructions': 'Completed?'}}
        return {'schema': 'fusion.training.v1', 'id': key, 'group': key, 'kind': 'acceptance',
                'state': state, 'questions': questions, 'schema_hash': digest(questions),
                'split': split, 'labels': {'ok': value}}

    def test_duplicates_conflicts_and_cross_split_leakage_are_distinct(self):
        rows = [self.row('one', 'same'), self.row('two', 'same'),
                self.row('three', 'same', 'validation', 'true'), self.row('four', 'different')]
        quality = dataset_quality(rows)
        self.assertEqual((quality['unique_inputs'], quality['duplicate_examples']), (2, 2))
        self.assertEqual(quality['conflicts'][0]['questions'], ['ok'])
        self.assertEqual(quality['cross_split_duplicates'], [['one', 'two', 'three']])
        self.assertEqual(quality['balance'][0]['counts'], {'false': 3, 'true': 1})
        self.assertEqual(dataset_quality(rows[:2])['balance'][0]['missing_labels'], ['true'])
        # Identical text with a different question contract is a different input.
        rows[1]['questions'] = {'ok': {'type': 'noul', 'instructions': 'Needs repair?'}}
        self.assertEqual(dataset_quality(rows)['unique_inputs'], 3)

    def test_effective_review_evidence_sources_and_corrections(self):
        row = self.row('a', 'same')
        row.update(status='ok', reviewed_answers={'ok': 'true'}, labels=[
            {'verified': True, 'answers': {'ok': 'false'}, 'evidence': 'E1', 'suggestion_id': 'draft',
             'answers_edited': False, 'suggested_by': {'agent': 'codex'}},
            {'verified': True, 'answers': {'ok': 'true'}, 'evidence': 'corrected', 'replace': True,
             'suggestion_id': 'draft', 'answers_edited': True, 'suggested_by': {'agent': 'council'}}])
        quality = review_quality([row])
        self.assertEqual(quality['sources'], {'council': 1})
        self.assertEqual(quality['draft_reviews'], {'edited': 1})
        self.assertEqual(quality['revised_answers'], 1)
        self.assertEqual(quality['evidence_recorded'], 1)
        row['excluded'] = True
        self.assertEqual(review_quality([row])['answers'], 0)

    def test_comparisons_require_source_identity_matching_benchmark_and_no_known_leak(self):
        candidates = [{'id': 'candidate-job', 'training': {'source_identity': 'base', 'model_identity': 'new'}}]
        candidate = {'id': 'after', 'result': {'model_identities': ['new'], 'dataset_hash': 'same', 'accuracy': .8}}
        baseline = {'id': 'before', 'result': {'model_identities': ['base'], 'dataset_hash': 'same', 'accuracy': .6}}
        comparisons = matched_comparisons(candidates, [candidate, baseline])
        self.assertAlmostEqual(comparisons[0]['delta'], .2)
        baseline['result']['dataset_hash'] = 'different'
        self.assertIsNone(matched_comparisons(candidates, [candidate, baseline])[0]['delta'])
        # More training rows are fine if the exact held-out benchmark stays fixed.
        for e in (candidate, baseline):
            e['result']['benchmark_hash'] = 'fixed-benchmark'
        self.assertAlmostEqual(matched_comparisons(candidates, [candidate, baseline])[0]['delta'], .2)
        candidate['result']['holdout'] = {'status': 'contaminated'}
        self.assertIsNone(matched_comparisons(candidates, [candidate, baseline])[0]['delta'])
        baseline['result']['model_identities'] = ['unrelated']
        self.assertIsNone(matched_comparisons(candidates, [candidate, baseline])[0]['baseline_id'])

    def test_evaluations_snapshot_health_baselines_and_ancestral_overlap(self):
        class FakeBackend:
            def predict(self, request):
                return {'answers': {'ok': {'noul': .2}},
                        'model_identity': 'candidate', 'truncated': False}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [self.row('train', 'train'), self.row('heldout', 'unseen', 'validation')]
            dataset = root / 'data.jsonl'
            dataset.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            model = root / 'candidate'
            model.mkdir()
            (model / 'training.json').write_text(json.dumps({'lineage_complete': True,
                'seen_train_groups': [digest('older-training-group')], 'seen_train_inputs': [input_key(rows[1])]}))
            args = argparse.Namespace(dataset=str(dataset), output=str(root / 'eval.jsonl'), model_path=str(model), device='cpu', control=True)
            with patch('fusion_laya.Backend', return_value=FakeBackend()):
                report = evaluate(args)
            self.assertEqual(report['accuracy'], 1)
            self.assertEqual(report['majority_accuracy'], 1)
            self.assertEqual(report['by_kind']['acceptance']['questions'], 1)
            self.assertEqual(report['holdout']['status'], 'contaminated')
            self.assertEqual(report['holdout']['overlap_inputs'], 1)
            self.assertIsNone(report['control_accuracy'])  # Only one held-out state.
            self.assertEqual(report['data_quality']['unique_inputs'], 2)
            # Label corrections produce a new benchmark, preventing false improvement claims.
            before = report['benchmark_hash']
            rows[1]['labels']['ok'] = 'true'
            dataset.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            args.output = str(root / 'next.jsonl')
            with patch('fusion_laya.Backend', return_value=FakeBackend()):
                self.assertNotEqual(evaluate(args)['benchmark_hash'], before)


if __name__ == '__main__':
    unittest.main()
