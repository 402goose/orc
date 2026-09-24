"""Read-only TENET receipt attachments for the canonical ORC control room.

Project-local request/result receipts remain authoritative. Saved projection
observations describe an earlier read-back, never current worker liveness.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from fusion_workflow import NODE_ID_RE

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_BYTES = 8 * 1024 * 1024


def read_file(root, path):
    root, path = Path(root).resolve(), Path(path)
    parts = path.relative_to(root).parts
    if not parts or any(p in {'..', '.'} for p in parts):
        raise ValueError('Artifact must be inside this workspace')
    # Open each component relative to its directory descriptor. Never follow a
    # symlink, including a parent swapped while another process is writing.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Artifact must be a regular file')
        if info.st_size > MAX_BYTES:
            raise ValueError('Artifact exceeds the 8 MiB display limit')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('Artifact exceeds the 8 MiB display limit')
        return data
    finally:
        os.close(fd)


def read_json(root, path):
    try:
        value = json.loads(read_file(root, path))
    except RecursionError as error:
        raise ValueError('Artifact JSON is nested too deeply') from error
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def saved_projection(observation, run_id):
    """Validate a saved v1 observation, without querying either destination."""
    projection = observation.get('projection')
    if (observation.get('schema') != 'tenet.recipe-state-observation.v1'
            or not isinstance(observation.get('observed_at'), str) or not observation['observed_at'].strip()
            or not isinstance(projection, dict)
            or projection.get('schema') != 'tenet.recipe-state-projection-result.v1'
            or projection.get('run_id') != run_id
            or projection.get('authority') != 'project-local-receipts'
            or not isinstance(projection.get('workspace_id'), str) or not projection['workspace_id'].strip()
            or type(projection.get('terminal_receipt_present')) is not bool
            or projection.get('cloud') != 'not_attempted'):
        raise ValueError('Invalid state observation identity or schema')
    objects = projection.get('objects')
    if not isinstance(objects, list) or not 1 <= len(objects) <= 2:
        raise ValueError('State observation must cover request and optional result')
    phases, statuses = [], []
    for obj in objects:
        if not isinstance(obj, dict) or obj.get('phase') not in ('request', 'result'):
            raise ValueError('Invalid projection phase')
        phases.append(obj['phase'])
        source = obj.get('source')
        if (not isinstance(obj.get('hash'), str) or not SHA256.fullmatch(obj['hash'])
                or not isinstance(source, dict) or not isinstance(source.get('path'), str)
                or not isinstance(source.get('sha256'), str) or not SHA256.fullmatch(source['sha256'])
                or ('write_error' in obj and not isinstance(obj['write_error'], str))):
            raise ValueError('Invalid projection object hash or source')
        for name in ('primary', 'shadow'):
            destination = obj.get(name)
            if (not isinstance(destination, dict)
                    or destination.get('status') not in ('verified', 'missing', 'mismatch', 'error')
                    or ('error' in destination and not isinstance(destination['error'], str))):
                raise ValueError('Invalid saved projection destination status')
            statuses.append(destination['status'])
    expected = ['request', 'result'] if projection['terminal_receipt_present'] else ['request']
    if sorted(phases) != expected:
        raise ValueError('Projection phases disagree with its terminal receipt flag')
    verified = statuses.count('verified')
    aggregate = 'verified' if verified == len(statuses) else 'partial' if verified else 'failed'
    if projection.get('projection_status') != aggregate:
        raise ValueError('Projection aggregate disagrees with saved destination statuses')
    return projection


def workflow_evidence(workspace, workflow_id, manifest):
    root = Path(workspace).resolve()
    if not SAFE_ID.fullmatch(workflow_id or ''):
        raise ValueError('Invalid workflow ID')
    response = {'runs': [], 'artifacts': [], 'errors': []}

    def add(rows, label, path):
        path = Path(path)
        rel = str(path.relative_to(root))
        item = {'id': hashlib.sha256(rel.encode()).hexdigest()[:24], 'label': label, 'path': str(path)}
        try:
            item.update(available=True, bytes=len(read_file(root, path)))
        except (ValueError, OSError) as error:
            item.update(available=False, error=str(error), bytes=None)
        if not any(row['id'] == item['id'] for row in rows):
            rows.append(item)

    directory = root / '.tenet/recipe-runs'
    try:
        # Do not enumerate an externally linked receipt directory.
        for parent in [root / '.tenet', directory]:
            if parent.is_symlink():
                raise ValueError('Linked recipe directories are not accessible')
        candidates = sorted(directory.iterdir()) if directory.exists() else []
        if len(candidates) > 1000:
            raise ValueError('Too many local recipe receipt directories')
    except (ValueError, OSError) as error:
        response['errors'].append(str(error))
        candidates = []
    for base in candidates:
        run_id = base.name
        if not SAFE_ID.fullmatch(run_id):
            continue
        try:
            request = read_json(root, base / 'request.json')
            if request.get('schema') != 'tenet.recipe-run.v1' or request.get('run_id') != run_id:
                raise ValueError('Invalid recipe request identity')
            refs = request.get('external_runs')
            if not isinstance(refs, list):
                raise ValueError('Invalid external run references')
            if not any(isinstance(ref, dict) and ref.get('system') == 'fusion' and ref.get('id') == workflow_id for ref in refs):
                continue
            project_root = request.get('project_root')
            if (not isinstance(project_root, str) or not Path(project_root).is_absolute()
                    or Path(project_root).resolve() != root):
                raise ValueError('Recipe receipt belongs to another workspace')
            run = {'run_id': run_id, 'title': request.get('recipe', {}).get('title'),
                   'status': 'running', 'terminal': False, 'liveness': 'unknown',
                   'started_at': request.get('started_at'), 'artifacts': [],
                   'state_projection': {'status': 'not-recorded'}}
            add(run['artifacts'], 'TENET request', base / 'request.json')
            add(run['artifacts'], 'TENET result', base / 'result.json')
            try:
                result = read_json(root, base / 'result.json')
                body = result.get('result')
                if (result.get('schema') != 'tenet.recipe-run-result.v1' or result.get('run_id') != run_id
                        or result.get('status') not in ('succeeded', 'failed')
                        or not isinstance(body, dict) or not isinstance(body.get('steps'), list)
                        or any(not isinstance(step, dict) for step in body['steps'])):
                    raise ValueError('Invalid recipe result identity or status')
                run.update(status=result['status'], terminal=True)
            except FileNotFoundError:
                pass
            except (ValueError, OSError) as error:
                run.update(status='unreadable', error=str(error))
            projection_path = root / '.tenet/recipe-state' / run_id / 'projection.json'
            try:
                observation = read_json(root, projection_path)
                projection = saved_projection(observation, run_id)
                objects = projection['objects']
                phases = [obj.get('phase') for obj in objects]
                expected = ['request', 'result'] if run['terminal'] else ['request']
                fresh = run['status'] != 'unreadable' and sorted(phases) == sorted(expected)
                for obj in objects:
                    phase = obj.get('phase')
                    source = obj['source']
                    source_path = base / (phase + '.json')
                    fresh = fresh and source.get('path') == str(source_path.relative_to(root)) and source.get('sha256') == hashlib.sha256(read_file(root, source_path)).hexdigest()
                run['state_projection'] = {'status': 'recorded' if fresh else 'stale', 'observed_at': observation.get('observed_at'),
                    'workspace_id': projection['workspace_id'],
                    'projection_status': projection['projection_status'], 'objects': objects,
                    'note': 'Saved local read-back observation; current storage and cloud delivery are not checked here.'}
                add(run['artifacts'], 'TENET state observation', projection_path)
            except FileNotFoundError:
                pass
            except (ValueError, OSError, TypeError, AttributeError) as error:
                run['state_projection'] = {'status': 'unreadable', 'note': str(error)}
            response['runs'].append(run)
        except (ValueError, OSError, TypeError, AttributeError) as error:
            response['errors'].append(f'{run_id}: {error}')
    # Attach only known log/receipt names linked by this workflow, never a
    # caller-supplied path or an arbitrary path declared by a worker.
    flow = root / '.fusion/workflows' / workflow_id
    for node_id, node in (manifest.get('nodes') or {}).items():
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id) or node_id in {'.', '..'} or not isinstance(node, dict):
            continue
        result = node.get('result') or {}
        for check in result.get('acceptance_checks') or []:
            if not isinstance(check, dict):
                continue
            for key, filename in [('receipt', 'receipt.json'), ('stdout', 'stdout.log'), ('stderr', 'stderr.log')]:
                value = (check.get('artifacts') or {}).get(key)
                if not isinstance(value, str):
                    continue
                path = Path(value)
                try:
                    rel = path.relative_to(flow / 'nodes' / node_id / 'acceptance')
                except ValueError:
                    continue
                if re.fullmatch(r'attempt-\d+/check-[A-Za-z0-9_-]+/' + re.escape(filename), str(rel)):
                    index = check.get('check_index')
                    number = index + 1 if type(index) is int and index >= 0 else '?'
                    add(response['artifacts'], f'{node_id} acceptance {number} {key}', path)
        worker_id = result.get('run_id')
        try:
            active = read_json(root, flow / 'nodes' / node_id / 'active.json')
            worker_id = active.get('run_id') or worker_id
        except (ValueError, OSError):
            pass
        if isinstance(worker_id, str) and SAFE_ID.fullmatch(worker_id):
            for filename in ['answer.md', 'stdout.log', 'stderr.log', 'activity.json']:
                add(response['artifacts'], f'{node_id} {filename}', root / '.fusion/runs' / worker_id / filename)
    return response


def artifact(workspace, workflow_id, manifest, artifact_id):
    if not re.fullmatch(r'[a-f0-9]{24}', artifact_id or ''):
        raise ValueError('Invalid artifact ID')
    evidence = workflow_evidence(workspace, workflow_id, manifest)
    rows = evidence['artifacts'] + [item for run in evidence['runs'] for item in run['artifacts']]
    item = next((row for row in rows if row['id'] == artifact_id), None)
    if item is None:
        raise ValueError('Artifact is not declared for this workflow')
    return {'path': item['path'], 'text': read_file(workspace, item['path']).decode('utf-8', errors='replace')}
