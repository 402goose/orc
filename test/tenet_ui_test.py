"""Actual local receipt/HTTP attachment contract, independent of UI markup."""
import hashlib
import io
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion_tenet import artifact, read_file, workflow_evidence
from fusion_ui import ControlRoom, Handler, Server


class TenetEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'workspace'
        self.root.mkdir()
        self.base = self.root / '.tenet/recipe-runs/recipe-001'
        self.flow = self.root / '.fusion/workflows/flow-001'
        self.request = {'schema': 'tenet.recipe-run.v1', 'run_id': 'recipe-001',
                        'project_root': str(self.root), 'started_at': '2026-09-24T00:00:00Z',
                        'recipe': {'title': 'Real recipe'}, 'external_runs': [{'system': 'fusion', 'id': 'flow-001'}]}
        self.manifest = {'schema': 'fusion.workflow.v1', 'workflow_id': 'flow-001', 'nodes': {}}
        self.write(self.base / 'request.json', self.request)
        self.write(self.flow / 'manifest.json', self.manifest)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def view(self):
        return workflow_evidence(self.root, 'flow-001', self.manifest)

    def result(self, **changes):
        return {'schema': 'tenet.recipe-run-result.v1', 'run_id': 'recipe-001', 'status': 'failed',
                'finished_at': '2026-09-24T00:00:01Z',
                'result': {'recipe': 'Real recipe', 'steps': [], 'totalDuration': 0, 'success': False}, **changes}

    def observation(self, terminal=False):
        # Same shape as the actual Oasis002 and TENET refusal001 sync observations.
        return {'schema': 'tenet.recipe-state-observation.v1', 'observed_at': '2026-09-24T00:00:02Z',
                'projection': {'schema': 'tenet.recipe-state-projection-result.v1',
                    'run_id': 'recipe-001', 'workspace_id': 'saved-workspace-identity',
                    'authority': 'project-local-receipts', 'projection_status': 'verified',
                    'terminal_receipt_present': terminal, 'cloud': 'not_attempted',
                    'objects': [{'phase': phase, 'hash': 'a' * 64,
                                 'source': {'path': f'.tenet/recipe-runs/recipe-001/{phase}.json',
                                            'sha256': hashlib.sha256((self.base / f'{phase}.json').read_bytes()).hexdigest()},
                                 'primary': {'status': 'verified'}, 'shadow': {'status': 'verified'}}
                                for phase in (['request', 'result'] if terminal else ['request'])]}}

    def test_real_receipts_keep_executor_failure_separate_from_passed_checks(self):
        self.assertEqual(self.view()['runs'][0]['status'], 'running')
        self.write(self.base / 'result.json', self.result())
        check = self.flow / 'nodes/build/acceptance/attempt-1/check-1-real/receipt.json'
        self.write(check, {'status': 'passed', 'exit_code': 0})
        self.manifest['nodes']['build'] = {'result': {'acceptance_checks': [{'check_index': 0, 'status': 'passed', 'artifacts': {'receipt': str(check)}}]}}
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        view = self.view()
        self.assertEqual(view['runs'][0]['status'], 'failed')
        self.assertTrue(view['runs'][0]['terminal'])
        self.assertEqual(view['runs'][0]['liveness'], 'unknown')
        receipt = view['artifacts'][0]
        self.assertEqual(json.loads(artifact(self.root, 'flow-001', self.manifest, receipt['id'])['text'])['status'], 'passed')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_only_matching_identity_and_declared_artifact_paths(self):
        self.assertEqual(workflow_evidence(self.root, 'other', self.manifest)['runs'], [])
        self.write(self.base / 'result.json', self.result(run_id='wrong', status='succeeded'))
        self.assertEqual(self.view()['runs'][0]['status'], 'unreadable')
        self.manifest['nodes']['build'] = {'result': {'acceptance_checks': [{'artifacts': {'receipt': str(self.root / '.tenet/context-hub.token')}}]}}
        self.assertEqual(self.view()['artifacts'], [])
        with self.assertRaises(ValueError):
            artifact(self.root, 'flow-001', self.manifest, 'a' * 24)
        with self.assertRaises(ValueError):
            artifact(self.root, 'flow-001', self.manifest, '../request.json')

    def test_orc_node_names_keep_their_existing_contract(self):
        for node_id in ['build.api', '_review', '-check', 'x' * 80]:
            check = self.flow / 'nodes' / node_id / 'acceptance/attempt-1/check-1-real/receipt.json'
            self.write(check, {'status': 'passed'})
            self.manifest['nodes'][node_id] = {'result': {'acceptance_checks': [{'artifacts': {'receipt': str(check)}}]}}
        self.assertEqual(len(self.view()['artifacts']), 4)
        for item in self.view()['artifacts']:
            self.assertTrue(item['available'])

    def test_saved_projection_must_cover_current_receipt_bytes_and_phases(self):
        observation = self.observation()
        self.write(self.root / '.tenet/recipe-state/recipe-001/projection.json', observation)
        saved = self.view()['runs'][0]['state_projection']
        self.assertEqual(saved['status'], 'recorded')
        self.assertEqual(saved['workspace_id'], 'saved-workspace-identity')
        self.assertIn('current storage and cloud delivery are not checked', saved['note'])
        self.write(self.base / 'result.json', self.result())
        self.assertEqual(self.view()['runs'][0]['state_projection']['status'], 'stale')
        self.write(self.root / '.tenet/recipe-state/recipe-001/projection.json', self.observation(terminal=True))
        self.assertEqual(self.view()['runs'][0]['state_projection']['status'], 'recorded')
        self.request['recipe']['title'] = 'changed'
        self.write(self.base / 'request.json', self.request)
        self.assertEqual(self.view()['runs'][0]['state_projection']['status'], 'stale')

    def test_malformed_or_contradictory_projection_is_unreadable_not_execution_failure(self):
        mutations = [
            (('projection', 'schema'), 'unsupported'),
            (('projection', 'workspace_id'), ''),
            (('projection', 'terminal_receipt_present'), True),
            (('projection', 'terminal_receipt_present'), 0),
            (('projection', 'cloud'), 'replicated'),
            (('projection', 'objects', 0, 'hash'), 'not-a-hash'),
            (('projection', 'objects', 0, 'source', 'sha256'), 'not-a-hash'),
            (('projection', 'objects', 0, 'primary'), None),
            (('projection', 'objects', 0, 'primary', 'status'), 'unknown'),
            (('projection', 'objects', 0, 'primary', 'status'), 'missing'),
            (('projection', 'objects', 0, 'shadow', 'status'), 'error'),
            (('projection', 'objects', 0, 'write_error'), {}),
            (('projection', 'projection_status'), 'partial'),
        ]
        for path, value in mutations:
            with self.subTest(path=path, value=value):
                observation = self.observation()
                target = observation
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                self.write(self.root / '.tenet/recipe-state/recipe-001/projection.json', observation)
                run = self.view()['runs'][0]
                self.assertEqual(run['state_projection']['status'], 'unreadable')
                self.assertEqual(run['status'], 'running')
                self.assertFalse(run['terminal'])
        observation = self.observation()
        observation['projection']['objects'].append(deepcopy(observation['projection']['objects'][0]))
        self.write(self.root / '.tenet/recipe-state/recipe-001/projection.json', observation)
        self.assertEqual(self.view()['runs'][0]['state_projection']['status'], 'unreadable')

    def test_partial_failed_and_post_write_error_observations_remain_honest(self):
        for primary, shadow, expected in [('verified', 'missing', 'partial'), ('error', 'mismatch', 'failed'),
                                           ('verified', 'verified', 'verified')]:
            with self.subTest(primary=primary, shadow=shadow):
                observation = self.observation()
                projection = observation['projection']
                projection['projection_status'] = expected
                projection['objects'][0]['primary']['status'] = primary
                projection['objects'][0]['shadow']['status'] = shadow
                projection['objects'][0]['write_error'] = 'Write returned an error; destination read-back is separate.'
                projection['future_optional_field'] = {'preserved-on-disk': True}
                self.write(self.root / '.tenet/recipe-state/recipe-001/projection.json', observation)
                run = self.view()['runs'][0]
                self.assertEqual(run['state_projection']['status'], 'recorded')
                self.assertEqual(run['state_projection']['projection_status'], expected)
                self.assertEqual(run['status'], 'running')

    def test_malformed_result_does_not_hide_valid_request_or_look_terminal(self):
        for changes in [{'status': 'interrupted'}, {'status': []}, {'result': None},
                        {'result': {'steps': 'not a list'}}, {'result': {'steps': [None]}}]:
            with self.subTest(changes=changes):
                self.write(self.base / 'result.json', self.result(**changes))
                self.write(self.root / '.tenet/recipe-state/recipe-001/projection.json', self.observation())
                run = self.view()['runs'][0]
                self.assertEqual(run['status'], 'unreadable')
                self.assertFalse(run['terminal'])
                self.assertEqual(run['state_projection']['status'], 'stale')
                self.assertEqual(len(run['artifacts']), 3)

    def test_symlinks_special_files_and_oversized_artifacts_are_rejected(self):
        outside = Path(self.temp.name) / 'private'
        outside.write_text('never expose')
        link = self.base / 'result.json'
        link.symlink_to(outside)
        self.assertEqual(self.view()['runs'][0]['status'], 'unreadable')
        with self.assertRaises(OSError):
            read_file(self.root, link)
        link.unlink()
        os.mkfifo(link)
        with self.assertRaises(ValueError):
            read_file(self.root, link)
        link.unlink()
        with link.open('wb') as f:
            f.truncate(8 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            read_file(self.root, link)
        moved = self.root / 'linked-parent'
        moved.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(OSError):
            read_file(self.root, moved / 'request.json')

    def test_actual_http_endpoint_requires_capability_and_serves_only_declared_ids(self):
        with patch.dict(os.environ, {'ORC_HOME': str(Path(self.temp.name) / 'orc-home')}):
            app = ControlRoom(self.root, Path(self.temp.name) / 'registry.json')
            server = Server(0, app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                item = self.view()['runs'][0]['artifacts'][0]
                url = server.origin + '/api/run-artifact?id=flow-001&artifact=' + item['id']
                with self.assertRaises(HTTPError) as error:
                    urlopen(url)
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                with urlopen(Request(url, headers={'X-Fusion-Token': server.token})) as result:
                    self.assertEqual(result.status, 200)
                    self.assertEqual(result.headers['Cache-Control'], 'no-store')
                    self.assertEqual(json.loads(json.load(result)['text']), self.request)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url + 'x', headers={'X-Fusion-Token': server.token}))
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_json_response_preserves_malformed_unicode_evidence_without_dropping_connection(self):
        handler = object.__new__(Handler)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda code: None
        handler.send_header = lambda key, value: None
        handler.end_headers = lambda: None
        payload = {'title': 'Unicode text: café', 'error': '\ud800'}
        handler.send(200, payload)
        self.assertEqual(json.loads(handler.wfile.getvalue()), payload)


if __name__ == '__main__':
    unittest.main()
