"""Cross-workspace observation is read-only and does not infer worker liveness."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion_ui import ControlRoom, Server


class ActivityProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.empty = self.root / 'idle-project'
        self.busy = self.root / 'working-project'
        self.empty.mkdir(); self.busy.mkdir()
        self.app = ControlRoom(self.empty, self.root / 'registry.json')
        self.busy_id = self.app.add_workspace(str(self.busy), save=False)['id']

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def seed(self):
        self.write(self.busy / '.fusion/workflows/current/manifest.json', {
            'task': 'Read the shared contract', 'status': 'running', 'coordinator_pid': -9999,
            'started_at_ms': 100, 'nodes': {'read': {'agent': 'codex', 'status': 'running'}}})
        self.write(self.busy / '.fusion/workflows/current/nodes/read/active.json', {'run_id': 'worker-1'})
        self.write(self.busy / '.fusion/runs/worker-1/activity.json', {'updated_at_ms': 101})
        self.write(self.busy / '.fusion/runs/worker-1/stdout.log', {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'I found the boundary between runtime and client.'}})
        self.write(self.busy / '.fusion/workflows/failed/manifest.json', {
            'task': 'Historical failed run', 'status': 'failed', 'started_at_ms': 200, 'nodes': {}})

    def test_idle_workspace_still_observes_other_workspace_and_preserves_status(self):
        self.seed()
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('fusion_ui.alive', side_effect=AssertionError('No PID inference')), patch('fusion_ui.subprocess.Popen', side_effect=AssertionError('No worker')):
            data = self.app.activity()
        self.assertEqual(data['runs'][0]['id'], 'current')
        self.assertEqual(data['runs'][0]['workspace_id'], self.busy_id)
        self.assertEqual(data['runs'][0]['latest_update'], 'I found the boundary between runtime and client.')
        self.assertEqual(data['runs'][0]['status'], 'running')
        self.assertEqual(data['runs'][0]['liveness'], 'unknown')
        self.assertFalse(data['runs'][0]['terminal'])
        self.assertTrue(data['runs'][1]['terminal'])
        self.assertEqual(data['workspaces'][0]['run_count'], 0)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_damaged_or_escaping_evidence_is_visible_without_substitution(self):
        self.seed()
        self.write(self.busy / '.fusion/workflows/bad/manifest.json', {'status': 'running', 'started_at_ms': 'not-a-date'})
        self.write(self.root / 'outside/manifest.json', {'task': 'must not expose', 'status': 'running'})
        (self.busy / '.fusion/workflows/escape').symlink_to(self.root / 'outside', target_is_directory=True)
        data = self.app.activity()
        self.assertEqual(len(data['errors']), 2)
        self.assertNotIn('must not expose', json.dumps(data))
        self.assertEqual({r['id'] for r in data['runs']}, {'current', 'failed'})

    def test_authenticated_http_is_registered_workspace_scoped(self):
        self.seed()
        with patch.dict(os.environ, {'ORC_HOME': str(self.root / 'home')}):
            server = Server(0, self.app)
        server.service_actions = lambda: None
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with self.assertRaises(HTTPError) as exc:
                urlopen(server.origin + '/api/activity', timeout=2)
            self.assertEqual(exc.exception.code, 401)
            with urlopen(Request(server.origin + '/api/activity', headers={'X-Fusion-Token': server.token}), timeout=2) as response:
                data = json.load(response)
            self.assertEqual(len(data['workspaces']), 2)
            self.assertEqual(data['runs'][0]['workspace_name'], 'working-project')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
