"""Opt-in drafting and unanimous council approval, with persisted policy and progress."""
import contextlib
import fcntl
from pathlib import Path
import time

from fusion_learning import decision_rows, read_object
from fusion_decisions import digest

DEFAULTS = {'enabled': False, 'agent': 'auto', 'since_ms': 0,
            'labeling_mode': 'single', 'council_agents': [], 'council_rule': 'unanimous', 'approval_mode': 'human', 'approval_since_ms': 0}


@contextlib.contextmanager
def locked(workspace, name='garden'):
    path = Path(workspace) / '.fusion/decisions' / (name + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def settings(workspace):
    value = {**DEFAULTS, **read_object(Path(workspace) / '.fusion/decisions/garden.json')}
    # Legacy limits no longer stop an enabled garden, including after a restart.
    value.pop('daily_limit', None)
    from fusion_admission import binding
    if binding(workspace) and value.get('max_drafts_per_day') is None:
        value['max_drafts_per_day'] = 10
    return value


def save(app, workspace, body):
    from fusion_ui import atomic_json
    if type(body.get('enabled')) is not bool:
        raise ValueError('Choose whether automatic drafting is enabled')
    from fusion_labeling import labeling_options, approval_options, council_rule
    agent = body.get('agent', 'auto')
    if agent not in {'auto', 'codex', 'claude', 'agy', 'grok'}:
        raise ValueError('Choose an installed labeling worker')
    with app.lock, locked(workspace):
        old = settings(workspace)
        options = labeling_options(body.get('labeling_mode', old['labeling_mode']),
                                   body.get('council_agents', old['council_agents']))
        approval = approval_options(body.get('approval_mode', old['approval_mode']), options['labeling_mode'])
        rule = council_rule(body.get('council_rule', old['council_rule']))
        limit = body.get('max_drafts_per_day', old.get('max_drafts_per_day'))
        from fusion_admission import binding
        admitted = bool(binding(workspace))
        if admitted and limit is None:
            limit = 10
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError('Automatic drafts per UTC day must be an integer from 1 to 100')
        if admitted and body['enabled']:
            if options['labeling_mode'] != 'single' or approval != 'human' or agent not in {'auto', 'codex'}:
                raise ValueError('TENET automatic drafts require one Codex worker and human approval')
            agent = 'codex'
        pair = {key: body.get(key, old.get(key)) for key in ('model', 'reasoning_effort')}
        if admitted and body['enabled']:
            from fusion_admission import describe
            provider = describe(workspace)
            if not provider.get('ready'):
                raise ValueError('TENET provider is unavailable; automatic drafts cannot be enabled')
            executor = provider.get('executor', {})
            if all(v is None for v in pair.values()):
                pair = {key: executor.get(key) for key in pair}
            allowed = executor.get('allowed_pairs') or [{key: executor.get(key) for key in pair}]
            if pair not in allowed or pair.get('reasoning_effort') in {None, 'ultra'}:
                raise ValueError('Choose an allowed explicit non-ultra pair for automatic drafts')
        elif not admitted and any(v is not None for v in pair.values()):
            raise ValueError('Garden model pairs currently require TENET admission')
        since = old['since_ms'] if old.get('configured') else int(time.time() * 1000)
        approval_since = old['approval_since_ms'] if old['approval_mode'] == approval and old['council_rule'] == rule else int(time.time() * 1000)
        if body.get('include_existing') is True:
            since = 0
            approval_since = 0
        value = {'enabled': body['enabled'], 'agent': agent, **options, 'council_rule': rule,
                 'since_ms': since, 'configured': True, 'approval_mode': approval, 'approval_since_ms': approval_since}
        if limit is not None:
            value['max_drafts_per_day'] = limit
        if admitted and all(isinstance(v, str) and v for v in pair.values()):
            value.update(pair)
        value['policy_id'] = digest({key: value[key] for key in ('approval_mode', 'approval_since_ms', 'labeling_mode', 'council_agents', 'council_rule')})
        atomic_json(Path(workspace) / '.fusion/decisions/garden.json', value)
    return status(app, workspace)


def status(app, workspace, rows=None):
    value = settings(workspace)
    jobs = app.jobs(workspace, limit=None)
    now = int(time.time() * 1000)
    garden_jobs = [j for j in jobs if j.get('garden')]
    attempted = {j.get('decision_id') for j in jobs if j.get('action') == 'suggest-labels'}
    automatic = value['approval_mode'] == 'council'
    attempted_policy = {j.get('decision_id') for j in jobs if j.get('action') == 'suggest-labels' and j.get('garden_policy') == value.get('policy_id')}
    queue = []
    for row in (decision_rows(workspace) if rows is None else rows):
        if row.get('time_ms', 0) < max(value['since_ms'], value['approval_since_ms'] if automatic else 0):
            continue
        if automatic:
            human_reviewed = any(e.get('verified') and e.get('source') != 'council_approved_suggestion' for e in row.get('labels', []))
            eligible = row['garden_state'] in {'needs_draft', 'needs_review', 'needs_evidence', 'needs_attention'} and not human_reviewed
            if eligible and row['id'] not in attempted_policy:
                queue.append(row)
        elif row['garden_state'] == 'needs_draft' and row['id'] not in attempted:
            queue.append(row)
    used = sum(j.get('started_at_ms', 0) // 86400000 == now // 86400000 for j in garden_jobs)
    active = next((j for j in jobs if j.get('action') == 'suggest-labels' and j.get('status') in {'queued', 'running', 'stopping'}), None)
    latest = garden_jobs[0] if garden_jobs else None
    limit = value.get('max_drafts_per_day')
    invalid_limit = limit is not None and (type(limit) is not int or not 1 <= limit <= 100)
    state = ('paused' if not value['enabled'] else 'drafting' if active else
             'invalid_limit' if invalid_limit else 'daily_limit' if limit is not None and used >= limit else
             'waiting' if not queue else 'queued')
    return {**value, 'state': state, 'queued': len(queue), 'used_today': used,
            'active_job': active, 'latest_job': latest, 'next_id': queue[-1]['id'] if queue else None}


def tick(app, workspace):
    # Disabled workspaces stay read-only and do not acquire/create any artifacts.
    if not settings(workspace)['enabled']:
        return
    with app.lock, locked(workspace):
        current = status(app, workspace)
        if current['state'] != 'queued':
            return
        app.launch(workspace, {'action': 'suggest-labels', 'decision_id': current['next_id'],
                               'agent': current['agent'], 'labeling_mode': current['labeling_mode'],
                               'council_agents': current['council_agents'], 'approval_mode': current['approval_mode'],
                               'council_rule': current['council_rule'],
                               'garden_policy': current.get('policy_id'),
                               **{key: current[key] for key in ('model', 'reasoning_effort') if current.get(key)}}, garden=True)
