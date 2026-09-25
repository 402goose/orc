"""Read-only learning evidence: reviewed coverage, candidate lineage, evaluations."""
from collections import Counter, defaultdict
import json
from pathlib import Path

from fusion_decisions import (DecisionEngine, DecisionStore, labelable_record, labeled_splits, read_jsonl, record_group,
                              reviewed_labels, label_provenance)


def read_object(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def decision_rows(workspace):
    events = read_jsonl(DecisionStore(workspace).path)
    answers, excluded = reviewed_labels(events)
    provenance = label_provenance(events)
    rows = {e['id']: {**e, 'applications': [], 'labels': [], 'suggestions': []}
            for e in events if e.get('event') == 'decision'}
    fields = {'application': 'applications', 'label': 'labels', 'label_suggestion': 'suggestions'}
    for event in events:
        row = rows.get(event.get('id'))
        if row is not None and event.get('event') in fields:
            row[fields[event['event']]].append(event)
    for key, row in rows.items():
        row['reviewed_answers'] = answers.get(key, {})
        row['label_provenance'] = provenance.get(key, {})
        row['excluded'] = excluded.get(key, False)
        suggestion = row['suggestions'][-1] if row['suggestions'] else None
        approved_at = max((e.get('time_ms', 0) for e in row['labels'] if e.get('verified')), default=-1)
        if row['excluded']:
            state = 'excluded'
        elif not labelable_record(row):
            state = 'ineligible'
        elif suggestion and (suggestion.get('time_ms', 0) > approved_at or
                             any(e.get('source') == 'council_approved_suggestion' and e.get('suggestion_id') == suggestion.get('suggestion_id') for e in row['labels'])
                             and set(row['questions']) - set(row['reviewed_answers'])):
            disputed = any(q.get('state') == 'disputed' for q in suggestion.get('council', {}).get('questions', {}).values())
            state = 'needs_review' if suggestion.get('answers') or disputed else 'needs_evidence'
        elif row['reviewed_answers']:
            state = 'approved'
        else:
            state = 'needs_draft'
        row['garden_state'] = state
    return sorted(reversed(list(rows.values())), key=lambda r: r.get('time_ms', 0), reverse=True)


def learning_artifacts(workspace):
    """Index successful UI learning jobs without loading model weights."""
    root = Path(workspace) / '.fusion'
    artifacts = []
    for path in (root / 'ui/jobs').glob('*/job.json'):
        job = read_object(path)
        if job.get('action') not in {'export', 'train', 'evaluate', 'calibrate'}:
            continue
        request = read_object(path.with_name('request.json'))
        result = job.get('result') if isinstance(job.get('result'), dict) else {}
        metadata = request.get('learning') or {}
        argv = request.get('argv', [])
        model_path = metadata.get('model_path', '')
        if not model_path and '--model-path' in argv:
            model_path = argv[argv.index('--model-path') + 1]
        if model_path:
            path_value = Path(model_path).expanduser()
            model_path = str((path_value if path_value.is_absolute() else root / path_value).resolve())
        entry = {k: job.get(k) for k in ('id', 'action', 'status', 'started_at_ms')}
        entry.update(result=result, model_path=model_path, dataset=metadata.get('dataset', ''))
        if job.get('action') == 'train' and job.get('status') == 'success':
            candidate = path.parent / 'candidate'
            entry['training'] = read_object(candidate / 'training.json')
            entry['path'] = str(candidate)
        elif job.get('status') == 'success':
            entry['path'] = result.get('path') or str(path.parent / ('calibration.json' if job['action'] == 'calibrate' else 'dataset.jsonl'))
        artifacts.append(entry)
    return sorted(artifacts, key=lambda a: a.get('started_at_ms') or 0, reverse=True)


def learning_summary(workspace, config, rows=None):
    rows = decision_rows(workspace) if rows is None else rows
    engine = DecisionEngine(workspace, config)
    splits = labeled_splits(rows, engine.options['split'])
    counts = Counter(row['garden_state'] for row in rows)
    kinds, distribution, growth = {}, defaultdict(Counter), Counter()
    groups = {'train': set(), 'validation': set()}
    labeled, questions, agreed, compared = 0, 0, 0, 0
    for row in rows:
        kind = kinds.setdefault(row['kind'], {'eligible': 0, 'reviewed': 0, 'questions': 0, 'labeled': 0})
        if not labelable_record(row) or row['excluded']:
            continue
        kind['eligible'] += 1
        kind['questions'] += len(row['questions'])
        questions += len(row['questions'])
        answers = row['reviewed_answers']
        if not answers:
            continue
        kind['reviewed'] += 1
        kind['labeled'] += len(answers)
        labeled += len(answers)
        group = record_group(row)
        groups[splits.get(group, 'train')].add(group)
        for key, value in answers.items():
            distribution[f"{row['kind']} · {key}"][value] += 1
            prediction = row.get('prediction', {}).get(key, {})
            if prediction:
                compared += 1
                agreed += max(prediction, key=prediction.get) == value
        # First approval date, current retained question count (not duplicate approvals).
        first_review = next((e for e in row['labels'] if e.get('verified')), {})
        growth[first_review.get('time_ms', row.get('time_ms', 0)) // 86400000] += len(answers)
    artifacts = learning_artifacts(workspace)
    candidates = [a for a in artifacts if a['action'] == 'train' and a['status'] == 'success']
    evaluations = [a for a in artifacts if a['action'] == 'evaluate' and a['status'] == 'success']
    exports = [a for a in artifacts if a['action'] == 'export' and a['status'] == 'success']
    options = engine.options
    calibration = engine.calibration()
    configured = options.get('model_path', '')
    observed = next((r for r in rows if r.get('status') == 'ok' and r.get('model_identity')), {})
    training = read_object(Path(configured) / 'training.json') if configured else {}
    from fusion_quality import review_quality, matched_comparisons
    return {'counts': dict(counts), 'total': len(rows), 'labeled_questions': labeled, 'eligible_questions': questions,
            'reviewed_decisions': sum(k['reviewed'] for k in kinds.values()), 'kinds': kinds,
            'groups': {k: len(v) for k, v in groups.items()}, 'can_train': all(groups.values()),
            'growth': [{'day_ms': day * 86400000, 'labels': n} for day, n in sorted(growth.items())],
            'distribution': {k: dict(v) for k, v in distribution.items()},
            'agreement': {'matched': agreed, 'compared': compared, 'rate': agreed / compared if compared else None},
            'model': {'path': configured, 'mode': options['mode'], 'training': training,
                      'last_observed_identity': observed.get('model_identity'),
                      'last_observed_at_ms': observed.get('time_ms'),
                      'qualified_buckets': sum(b.get('qualified') is True for b in calibration.get('buckets', {}).values()),
                      'calibration_identity': calibration.get('model_identity'),
                      'calibration_buckets': {key: {**{k: v for k, v in bucket.items() if k not in {'reliability', 'risk'}},
                                                    'risk': {k: v for k, v in (bucket.get('risk') or {}).items() if k != 'curve'}}
                                              for key, bucket in calibration.get('buckets', {}).items()}},
            'quality': review_quality(rows, options['split']), 'comparisons': matched_comparisons(candidates, evaluations),
            'candidates': candidates, 'evaluations': evaluations, 'exports': exports,
            'jobs': artifacts[:10]}
