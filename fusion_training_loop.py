"""Persistent local learning rounds: export, source trial, train, candidate trial, calibration.

No agent calls or model promotion. Every comparison keeps its exact benchmark and
model identities. New evidence, not a timer alone, triggers another round.
"""
from __future__ import annotations
from collections import defaultdict
import json
import os
from pathlib import Path
import stat
import uuid

import fusion_core as core
from fusion_decisions import (DEFAULTS as DECISION_DEFAULTS, CHECKPOINTS, LABELABLE_STATUSES, DecisionEngine, config_for, digest,
                              exceeds_token_budget, labeled_splits, model_dir)
from fusion_garden import locked
from fusion_learning import decision_rows, read_object
from fusion_publish import save
from fusion_quality import dataset_quality, input_key, matched_comparisons, unbeaten

DEFAULTS = {"enabled": False, "min_new_answers": 10}
PHASES = ("export", "baseline", "train", "evaluate", "calibrate")
ACTIONS = {"baseline": "evaluate", **{p:p for p in PHASES if p != "baseline"}}
ACTIVE = {"queued", "running", "stopping"}
DEFAULT_SPLIT = DECISION_DEFAULTS['split']
# A candidate improves only if it beats the source checkpoint and every
# applicable baseline on the held-out questions by more than this much accuracy,
# and the held-out majority and control by more than this much balanced accuracy.
IMPROVEMENT_MARGIN = 0.02
WEIGHT_SUFFIXES = {'.safetensors', '.bin', '.pt', '.pth', '.ckpt', '.gguf'}


def root(workspace):
    return Path(workspace) / '.fusion/decisions/training'


def settings(workspace):
    return {**DEFAULTS, **read_object(root(workspace) / 'settings.json')}


def rounds(workspace):
    return sorted((read_object(p) for p in (root(workspace)/'rounds').glob('*/round.json')), key=lambda r:r.get('started_at_ms',0), reverse=True)


def tokens(rows):
    """Effective evidence only: repeat approvals and predictions cannot trigger training."""
    result = {}
    for row in rows:
        if row.get('excluded') or row.get('truncated') or row.get('status', 'ok') not in LABELABLE_STATUSES or exceeds_token_budget(row):
            continue
        group = row.get('group') or row.get('context', {}).get('group') or row.get('context', {}).get('task_id') or digest(row['state'])
        for key, value in row.get('reviewed_answers', row.get('labels', {})).items():
            result[row['id'] + ':' + key] = digest([row['state'], row['questions'][key], value, group])
    return result


def changes(before, after):
    return sum(before.get(k) != after.get(k) for k in before.keys() | after.keys())


def readiness(rows, method='time'):
    splits = labeled_splits(rows, method)
    return {k:sum(v == k for v in splits.values()) for k in ('train','validation')}


def split_method(workspace):
    try:
        config,_ = core.load_config(workspace)
        return DecisionEngine(workspace,config).options['split']
    except (OSError, ValueError, KeyError, TypeError, SystemExit):
        return DEFAULT_SPLIT


def curate(source, destination):
    """Never move held-out examples into training. Keep one copy of an input,
    favoring its existing validation copy. Conflicting inputs are withheld whole.
    Original exports, labels, workflow groups and review provenance remain intact.
    A round trains on this host, so export.exclude_repos does not withhold rows.
    """
    from fusion_laya import dataset_rows
    rows = dataset_rows(source)
    rows_in = len(rows)
    buckets = defaultdict(list)
    for row in rows:
        buckets[input_key(row)].append(row)
    kept, conflicts, duplicates = [], [], []
    for bucket in buckets.values():
        labels = defaultdict(set)
        for row in bucket:
            for key,value in row['labels'].items(): labels[key].add(value)
        if any(len(values)>1 for values in labels.values()):
            conflicts.extend(r['id'] for r in bucket)
            continue
        ordered = sorted(bucket, key=lambda r:(r['split'] != 'validation', -len(r['labels']), r['id']))
        kept.append(ordered[0])
        duplicates.extend(r['id'] for r in ordered[1:])
    quality = dataset_quality(kept)
    if quality['group_overlap'] or quality['cross_split_duplicates']:
        raise ValueError('Dataset still contains training/validation overlap')
    if quality['groups']['train'] < 2 or quality['groups']['validation'] < 2:
        raise ValueError('Need at least two training and two held-out workflow groups after removing duplicate and conflicting inputs')
    destination = Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('x') as out:
        for row in sorted(kept,key=lambda r:r['id']): out.write(json.dumps(row,ensure_ascii=False)+'\n')
    destination.chmod(0o600)
    return {'original_examples':rows_in, 'retained_examples':len(kept), 'duplicate_ids':duplicates,
            'conflicting_ids':conflicts, 'data_quality':quality, 'path':str(destination)}


def proof(round):
    results = round['results']
    candidate = {'id':round['jobs']['train'],'training':results['train']}
    evaluations = [{'id':round['jobs'][p], 'result':results[p]} for p in ('baseline','evaluate')]
    comparison = matched_comparisons([candidate],evaluations)[0] if results['evaluate'].get('model_identities') == [results['train'].get('model_identity')] else {}
    notes = list(comparison.get('notes',[]))
    if any(results[p].get('holdout',{}).get('status') != 'checked' for p in ('baseline','evaluate')):
        notes.append('Held-out independence could not be established for both models.')
    delta = comparison.get('delta') if not notes else None
    n, groups = comparison.get('validation_questions',0), comparison.get('validation_groups',0)
    if n < 30 or groups < 10: notes.append('Small held-out sample; this is an early measurement, not established generalization.')
    baselines = comparison.get('baselines')
    if baselines is None:
        # An evaluation from before per-question baselines: compare on its whole-benchmark numbers.
        accuracy = comparison.get('accuracy')
        baselines = {name: {'n': n, 'accuracy': comparison[key], 'candidate_accuracy': accuracy, 'margin': accuracy - comparison[key]}
                     for name, key in (('train_majority','majority_accuracy'),('control','control_accuracy'))
                     if accuracy is not None and comparison.get(key) is not None}
    missed = unbeaten(baselines, IMPROVEMENT_MARGIN)
    labels = {'majority': 'held-out majority baseline', 'train_majority': 'training-majority baseline',
              'heuristic': 'deterministic-policy baseline', 'control': 'shuffled-state control'}
    for name in missed:
        notes.append(f"The candidate did not beat the {labels.get(name, name)} by more than {IMPROVEMENT_MARGIN:g}.")
    headline = comparison.get('headline') or {}
    balanced, degenerate = headline.get('balanced_accuracy'), headline.get('degenerate') is True
    if degenerate:
        notes.append(f"The candidate is a constant predictor: {headline.get('degenerate_reason')}.")
    if not headline:
        notes.append('This evaluation predates balanced accuracy; re-evaluate the candidate to measure it.')
    elif balanced is None:
        notes.append('No held-out question has more than one label value; balanced accuracy is unmeasured.')
    honest = balanced is not None and not degenerate
    for name in ('majority', 'control'):
        floor = (baselines.get(name) or {}).get('balanced_accuracy')
        if balanced is not None and (floor is None or not balanced > floor + IMPROVEMENT_MARGIN):
            honest = False
            notes.append(f"Balanced accuracy {balanced:.3f} on the non-constant questions does not beat the {labels[name]} "
                         f"({'unmeasured' if floor is None else format(floor, '.3f')}) by more than {IMPROVEMENT_MARGIN:g}.")
    if delta is None: outcome = 'unmeasured'
    elif degenerate: outcome = 'degenerate'
    elif delta < -IMPROVEMENT_MARGIN: outcome = 'regression'
    elif delta > IMPROVEMENT_MARGIN and not missed and honest: outcome = 'gain'
    else: outcome = 'flat'
    notes.append('Benchmarks can change between rounds. Compare source and candidate within each round; repeated trials are not independent evidence.')
    calibration = results.get('calibrate',{})
    return {**comparison,'baselines':baselines,'delta':delta,'outcome':outcome,'margin':IMPROVEMENT_MARGIN,'notes':notes,
            'balanced_accuracy':balanced,'auc':headline.get('auc'),'degenerate':degenerate,
            'degenerate_reason':headline.get('degenerate_reason'),
            'questions':question_table(comparison, calibration),
            'qualified_buckets':sum(b.get('qualified') is True for b in calibration.get('buckets',{}).values()),
            'training':training_summary(results['train']),'promoted':False}


def training_summary(train):
    """How the candidate was fit: epochs, the dev split that chose the epoch, and the policy-term weight.

    Dev groups come from the training split only, so they say nothing about the
    held-out benchmark; a candidate trained before dev selection has None here.
    """
    selection = train.get('selection') or {}
    chosen = next((e for e in selection.get('dev_by_epoch', []) if e.get('epoch') == selection.get('chosen_epoch')), {})
    return {'max_epochs':selection.get('max_epochs'),'epochs_run':selection.get('epochs_run'),
            'chosen_epoch':selection.get('chosen_epoch'),'patience':selection.get('patience'),
            'selected_on':selection.get('selected_on'),'selection_mode':selection.get('mode'),
            'selection_reason':selection.get('reason'),'dev_rows':selection.get('dev_rows'),
            'dev_per_class':selection.get('dev_per_class'),'dev_groups':selection.get('dev_groups'),
            'dev_group_count':selection.get('dev_group_count'),'dev_balanced_accuracy':chosen.get('balanced_accuracy'),
            'dev_accuracy':chosen.get('accuracy'),'train_accuracy':train.get('train_accuracy'),
            'proper_scoring_weight':selection.get('proper_scoring_weight'),
            'objective':(train.get('objective') or {}).get('objective') if isinstance(train.get('objective'), dict) else None}


def question_table(comparison, calibration):
    """Per held-out question: n, candidate vs source and each baseline, and whether it may act.

    Calibration buckets are keyed kind:schema_hash:question; a question asked
    under more than one schema lists each bucket's gate.
    """
    table = {}
    for name, value in (comparison.get('by_question') or {}).items():
        before = (comparison.get('baseline_by_question') or {}).get(name, {})
        table[name] = {'n': value.get('n'), 'groups': value.get('groups'), 'candidate': value.get('accuracy'),
                       'source': before.get('accuracy'), 'balanced': value.get('balanced_accuracy'), 'auc': value.get('auc'),
                       'positive_rate': value.get('positive_rate'), 'constant': value.get('constant'),
                       'degenerate': value.get('degenerate'),
                       **{b: v.get('accuracy') for b, v in (value.get('baselines') or {}).items()}, 'gates': []}
    for key, bucket in (calibration.get('buckets') or {}).items():
        kind, question = key.split(':', 1)[0], key.rsplit(':', 1)[-1]
        risk = bucket.get('risk') or {}
        gate = {'bucket': key, 'n': bucket.get('validation'), 'qualified': bucket.get('qualified') is True,
                'threshold': risk.get('threshold'), 'coverage': risk.get('coverage') if risk.get('threshold') is not None else None,
                'upper_bound': risk.get('upper_bound'), 'alpha': risk.get('alpha'), 'delta': risk.get('delta'),
                'status': bucket.get('status') or ('qualified' if bucket.get('qualified') else 'not qualified')}
        table.setdefault(f"{kind}:{question}", {'gates': []})['gates'].append(gate)
    return table


def gate_line(gate):
    if gate.get('qualified') and gate.get('threshold') is not None:
        return (f"acts at p>={gate['threshold']:.2f}, coverage {gate['coverage']:.0%} of {gate['n']} held-out; "
                f"error <= {gate['alpha']:g} with probability {1 - gate['delta']:g} (bound {gate['upper_bound']:.3f})")
    return gate.get('status') or 'not qualified'


def question_lines(table):
    """The per-question table as short, readable fields for `fusion learn status`."""
    def score(value):
        return None if value is None else round(value, 3)
    lines = {}
    for name, row in sorted(table.items()):
        line = {'holdout_n': row.get('n'), 'holdout_groups': row.get('groups')}
        line.update({key: score(row.get(key)) for key in ('candidate', 'source', 'majority', 'train_majority', 'heuristic', 'control',
                                                          'balanced', 'auc', 'positive_rate') if key in row})
        line.update({key: row[key] for key in ('constant', 'degenerate') if row.get(key) is not None})
        line['gate'] = [gate_line(g) for g in row.get('gates', [])] or ['not calibrated']
        lines[name] = line
    return lines


LIVE_JOB_FIELDS = ('id', 'status', 'started_at_ms', 'progress')


def live_job(job):
    """The fields the round display actually uses, and nothing else.

    ControlRoom.job attaches a 60 KB stderr tail as `console` and an 80 KB
    stdout tail as `output`. This rides on /api/decisions, which the control
    room polls every two seconds while a round runs, and `learning_progress`
    writes a line per optimizer step, so those tails are reliably near their
    caps rather than occasionally large. The live view reads four fields; the
    logs are a click away under the job itself.
    """
    return {key: job.get(key) for key in LIVE_JOB_FIELDS} if isinstance(job, dict) else job


def status(app, workspace, rows=None):
    value = settings(workspace)
    history = rounds(workspace)
    rows = decision_rows(workspace) if rows is None else rows
    current = tokens(rows)
    previous = next((r for r in history if r.get('status')=='complete'), {})
    groups = readiness(rows, split_method(workspace))
    running = next((r for r in history if r.get('status') in {'running','needs_attention'}), None)
    pending = changes((running or previous).get('tokens',{}),current)
    active_job = None
    if running and running.get('active_job'):
        try:
            active_job = live_job(app.job(workspace,running['active_job']))
        except (ValueError, OSError):
            # The job directory can be pruned, or simply absent in a copied or
            # restored workspace. A vanished job must not take out the lab:
            # /api/decisions serves decisions, review, garden AND training from
            # this one call, and configure() returns it too - so raising here
            # would 500 everything and leave no way to even turn the loop off.
            active_job = None
    lost_job = bool(running and running.get('active_job') and active_job is None)
    ready = groups['train'] >= 2 and groups['validation'] >= 2
    if not value['enabled']: state,reason = 'paused','Automatic training is off. Saved rounds and active jobs are preserved.'
    elif running and running['status']=='needs_attention': state,reason='needs_attention',running.get('error','This round needs attention.')
    elif lost_job: state,reason='needs_attention','The saved job for this step is no longer on disk. Retry this step.'
    elif running: state,reason='running',f"Round {running['number']} · {running['phase']}"
    elif not ready: state,reason='gathering','Gather approved examples from at least two training and two held-out workflow groups.'
    elif previous and pending < value['min_new_answers']: state,reason='gathering',f"{pending} / {value['min_new_answers']} new or changed approved answers for the next round."
    else: state,reason='ready','Enough new evidence. The next round starts automatically.'
    safe = [{k:v for k,v in r.items() if k != 'tokens'} for r in history[:30]]
    return {**value,'state':state,'reason':reason,'new_answers':pending,'approved_answers':len(current),'groups':groups,
            'active_job':active_job,'rounds':safe,'completed_rounds':sum(r.get('status')=='complete' for r in history)}


def configure(app, workspace, body):
    with app.lock, locked(workspace,'training-loop'):
        old=settings(workspace)
        enabled=body.get('enabled',old['enabled']); minimum=body.get('min_new_answers',old['min_new_answers'])
        if type(enabled) is not bool or type(minimum) is not int or not 1 <= minimum <= 10000:
            raise ValueError('Choose automatic training and 1–10,000 new answers per round')
        if body.get('retry') is True:
            round=next((r for r in rounds(workspace) if r.get('status')=='needs_attention'),None)
            if round:
                round.update(status='running',active_job=None,error=None)
                round.get('jobs',{}).pop(round['phase'],None)
                round.pop('dispatch_key',None)
                save(root(workspace)/'rounds'/round['id']/'round.json',round)
        save(root(workspace)/'settings.json',{'enabled':enabled,'min_new_answers':minimum})
    return status(app,workspace)


def tick(app, workspace):
    if not settings(workspace)['enabled']:
        return
    with app.lock, locked(workspace,'training-loop'):
        history=rounds(workspace)
        round=next((r for r in history if r.get('status') in {'running','needs_attention'}),None)
        if round and round['status']=='needs_attention': return
        config,_=core.load_config(workspace)
        source=DecisionEngine(workspace,config).options['model_path']
        if not round:
            current=status(app,workspace)
            if current['state']!='ready': return
            # Wait for any manual local learning job before taking the CPU slot.
            if any(j['action'] in {'train','evaluate','calibrate','export'} and j['status'] in ACTIVE for j in app.jobs(workspace,limit=None)): return
            round={'id':'round-'+uuid.uuid4().hex[:12],'number':len(history)+1,'started_at_ms':core.now_ms(),
                   'status':'running','phase':'export','jobs':{},'results':{},'attempts':[], 'source_path':source,
                   'tokens':tokens(decision_rows(workspace)), 'active_job':None}
        path=root(workspace)/'rounds'/round['id']/'round.json'
        try:
            if source != round['source_path']:
                raise ValueError('The configured model changed during this round. Restore that model before retrying this step.')
            phase=round['phase']
            if round.get('active_job'):
                job=app.job(workspace,round['active_job'])
                if job['status'] in ACTIVE: return
                if job['status']!='success':
                    raise ValueError(f"{phase} {job['status']}: " + str(job.get('error') or job.get('console') or 'Open the saved job for details')[-1600:])
                result=job.get('result')
                if not isinstance(result,dict) or not result:
                    raise ValueError(f'{phase} finished without a saved result')
                round['results'][phase]=result
                if phase=='export':
                    raw=Path(result['path'])
                    raw_rows=[json.loads(line) for line in raw.read_text().splitlines() if line.strip()]
                    round['tokens']=tokens(raw_rows)
                    destination=path.parent/'dataset.jsonl'
                    # A prior failed curation never leaves a partially accepted dataset.
                    if destination.exists(): destination.unlink()
                    round['curation']=curate(raw,destination)
                    round['dataset']=str(destination)
                if phase=='train':
                    if result.get('source_identity') not in round['results']['baseline'].get('model_identities',[]):
                        raise ValueError('Training source differs from the measured baseline. No improvement claim can be made.')
                round['active_job']=None
                index=PHASES.index(phase)+1
                if index==len(PHASES):
                    round.update(status='complete',phase='complete',finished_at_ms=core.now_ms(),proof=proof(round))
                    save(path,round)
                    try:
                        retained=prune(workspace)
                    except Exception as exc:
                        retained={'status':'error','reason':str(exc)}
                    round['retention']={k:retained[k] for k in ('status','reason','protected','removed_bytes','retained_bytes') if k in retained}
                    save(path,round); return
                round['phase']=phase=PHASES[index]
                round.pop('dispatch_key',None)
                save(path,round)
            jobs = app.jobs(workspace,limit=None)
            job = next((j for j in jobs if round.get('dispatch_key') and j.get('learning_dispatch')==round['dispatch_key']),None)
            if not job and any(j['action'] in {'train','evaluate','calibrate','export'} and j['status'] in ACTIVE for j in jobs): return
            round.setdefault('dispatch_key',uuid.uuid4().hex)
            save(path,round)  # Persist launch intent before creating a detached job.
            request={'action':ACTIONS[phase], 'text':f"Laya round {round['number']} · {phase}",
                     'learning_round':round['id'],'learning_dispatch':round['dispatch_key']}
            if phase!='export': request['dataset']=round['results']['evaluate']['path'] if phase=='calibrate' else round['dataset']
            if phase in {'baseline','train'} and source: request['model_path']=source
            if phase=='evaluate': request['model_path']=round['results']['train']['path']
            if not job: job=app.launch(workspace,request)
            round['jobs'][phase]=job['id'];round['active_job']=job['id']
            round['attempts'].append({'phase':phase,'job_id':job['id'],'started_at_ms':core.now_ms()})
        except Exception as exc:
            round.update(status='needs_attention',error=str(exc))
        save(path,round)


def weight_files(candidate):
    """(path, bytes) of each regular weight file under a candidate, outside tokenizer/.

    Symlinks are never followed or listed, and a candidate that is itself a
    symlink has none.
    """
    candidate = Path(candidate)
    if candidate.is_symlink() or not candidate.is_dir():
        return []
    found = []
    for directory, folders, names in os.walk(candidate):
        if Path(directory) == candidate:
            folders[:] = [name for name in folders if name != 'tokenizer']
        for name in sorted(names):
            path = Path(directory) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and path.suffix in WEIGHT_SUFFIXES:
                found.append((path, info.st_size))
    return found


def job_dirs(workspace):
    jobs = Path(workspace) / '.fusion/ui/jobs'
    if not jobs.is_dir():
        return []
    return sorted(d for d in jobs.iterdir() if d.is_dir() and not d.is_symlink())


def candidate_bytes(workspace):
    """Bytes of candidate weights still on disk across this workspace's training jobs."""
    try:
        return sum(size for d in job_dirs(workspace) for _, size in weight_files(d / 'candidate'))
    except OSError:
        return None


def strict_object(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f'{path} is not a JSON object')
    return value


def job_model(workspace, request):
    """The resolved --model-path a train or evaluate job ran on, or None."""
    value = (request.get('learning') or {}).get('model_path') or ''
    argv = request.get('argv') or []
    if not value and '--model-path' in argv[:-1]:
        value = argv[argv.index('--model-path') + 1]
    if not value:
        return None
    return model_dir(workspace, value, '.fusion')


def prune(workspace, dry_run=False):
    """Remove candidate weights that no round, job or configuration still needs.

    A candidate's weight files are removed only when an evaluation of it
    succeeded, its round (if any) is complete, it is not among the newest
    decisions.training.keep_candidates successful training jobs, it is not
    promoted, and no configured checkpoint, unfinished round or active job
    refers to it. A relative configured path is protected both as the decision
    engine loads it (from the workspace) and as a learning job requests one
    (from .fusion), never from the current directory. training.json, configs,
    tokenizer/ and logs stay. When the configuration, a round receipt or a job
    record cannot be read, nothing is removed.
    """
    workspace = Path(workspace)
    try:
        config, _ = core.load_config(workspace)
        options = config_for(config)
        keep = options['keep_candidates']
        configured = [v for v in [options['model_path'], *options['checkpoints'].values()] if v and v not in CHECKPOINTS]
        protected = {model_dir(workspace, v, base) for v in configured for base in ('', '.fusion')}
        history = [strict_object(p) for p in (root(workspace) / 'rounds').glob('*/round.json')]
        jobs = [(d, strict_object(d / 'job.json')) for d in job_dirs(workspace)]
        held, evaluated = set(), set()
        for r in history:
            train = (r.get('jobs') or {}).get('train')
            if r.get('status') != 'complete' or (r.get('proof') or {}).get('promoted') is True:
                held.add(train)
            if r.get('status') != 'complete' and r.get('source_path'):
                protected.add(model_dir(workspace, r['source_path']))
        for directory, job in jobs:
            if job.get('status') in ACTIVE:
                held.add(directory.name)
                model = job_model(workspace, strict_object(directory / 'request.json'))
                if model:
                    protected.add(model)
            elif job.get('action') == 'evaluate' and job.get('status') == 'success':
                model = job_model(workspace, read_object(directory / 'request.json'))
                if model:
                    evaluated.add(model)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SystemExit) as exc:
        return {'status': 'kept', 'reason': f'Promotion state is unreadable, so every candidate is kept: {exc}',
                'removed': [], 'removed_bytes': 0, 'retained_bytes': candidate_bytes(workspace)}
    trained = [d for d, job in sorted(jobs, key=lambda j: j[1].get('started_at_ms') or 0, reverse=True)
               if job.get('action') == 'train' and job.get('status') == 'success']
    held.update(d.name for d in trained[:keep])
    removed, errors = [], []
    for directory in trained:
        candidate = directory / 'candidate'
        if directory.name in held or candidate.is_symlink() or not candidate.is_dir():
            continue
        path = candidate.resolve()
        if path not in evaluated or any(path == p or path.is_relative_to(p) or p.is_relative_to(path) for p in protected):
            continue
        try:
            if strict_object(candidate / 'training.json').get('promoted') is not False:
                continue
        except (OSError, ValueError):
            continue
        for file, size in weight_files(candidate):
            try:
                if not dry_run:
                    file.unlink()
                removed.append({'path': str(file), 'bytes': size})
            except OSError as exc:
                errors.append(f'{file}: {exc}')
    return {'status': 'dry_run' if dry_run else 'pruned', 'keep_candidates': keep, 'protected': sorted(map(str, protected)), 'removed': removed,
            'removed_bytes': sum(r['bytes'] for r in removed), 'retained_bytes': candidate_bytes(workspace), 'errors': errors}
