"""Opt-in automatic label drafts. Human approval remains a separate operation."""
import contextlib
import fcntl
from pathlib import Path
import time

from fusion_learning import decision_rows, read_object

DEFAULTS = {'enabled': False, 'agent': 'auto', 'since_ms': 0,
            'labeling_mode': 'single', 'council_agents': []}


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
    return value


def save(app, workspace, body):
    from fusion_ui import atomic_json
    if type(body.get('enabled')) is not bool:
        raise ValueError('Choose whether automatic drafting is enabled')
    from fusion_labeling import labeling_options
    agent = body.get('agent', 'auto')
    if agent not in {'auto', 'codex', 'claude', 'agy', 'grok'}:
        raise ValueError('Choose an installed labeling worker')
    with app.lock, locked(workspace):
        old = settings(workspace)
        options = labeling_options(body.get('labeling_mode', old['labeling_mode']),
                                   body.get('council_agents', old['council_agents']))
        since = old['since_ms'] if old.get('configured') else int(time.time() * 1000)
        if body.get('include_existing') is True:
            since = 0
        value = {'enabled': body['enabled'], 'agent': agent, **options,
                 'since_ms': since, 'configured': True}
        atomic_json(Path(workspace) / '.fusion/decisions/garden.json', value)
    return status(app, workspace)


def status(app, workspace, rows=None):
    value = settings(workspace)
    jobs = app.jobs(workspace, limit=None)
    now = int(time.time() * 1000)
    garden_jobs = [j for j in jobs if j.get('garden')]
    attempted = {j.get('decision_id') for j in jobs if j.get('action') == 'suggest-labels'}
    queue = [r for r in (decision_rows(workspace) if rows is None else rows)
             if r['garden_state'] == 'needs_draft' and r['id'] not in attempted
             and r.get('time_ms', 0) >= value['since_ms']]
    used = sum(j.get('started_at_ms', 0) // 86400000 == now // 86400000 for j in garden_jobs)
    active = next((j for j in jobs if j.get('action') == 'suggest-labels' and j.get('status') in {'queued', 'running', 'stopping'}), None)
    latest = garden_jobs[0] if garden_jobs else None
    state = 'paused' if not value['enabled'] else 'drafting' if active else 'waiting' if not queue else 'queued'
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
                               'council_agents': current['council_agents']}, garden=True)
