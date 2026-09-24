"""Local capability observations: source scope, uncertainty and read-only HTTP.

Uses temporary repositories and synthetic records, never model workers or GitHub.
These tests qualify observation semantics, not learning or autonomous execution.
"""
import builtins
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_capabilities
import fusion_core
from fusion_decisions import DecisionStore, digest
from fusion_ui import ControlRoom, Server


class CapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='orc-capabilities-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.git = shutil.which('git')
        self.assertIsNotNone(self.git, 'Local Git is required for real repository fixtures')
        self.environment = patch.dict(os.environ, {
            'PATH': os.environ.get('PATH', ''), 'HOME': str(self.root),
            'ORC_HOME': str(self.root / 'orc-home'), 'FUSION_TELEMETRY': '0',
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = {'decisions': {'mode': 'shadow', 'python': sys.executable},
                       'telemetry': {'remote': {'enabled': False}},
                       **{name: {'command': 'absent-capability-test-worker'}
                          for name in ('codex', 'claude', 'agy', 'grok')}}
        self.save_config()
        self.app = ControlRoom(self.workspace, self.root / 'registry.json')

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def save_config(self):
        self.write(self.workspace / '.fusion.json', self.config)

    def git_command(self, *args, workspace=None):
        return subprocess.run([self.git, *args], cwd=workspace or self.workspace,
                              text=True, capture_output=True, check=True).stdout.strip()

    def repo(self, remote=None, name='origin', workspace=None):
        workspace = workspace or self.workspace
        self.git_command('init', '-q', workspace=workspace)
        if remote:
            self.git_command('config', f'remote.{name}.url', remote, workspace=workspace)

    def observe(self, workspace=None):
        return fusion_capabilities.capabilities(self.app, workspace or self.workspace)

    def decision(self, identifier, status='ok', when=100, workspace=None, **extra):
        questions = {'plausible': {'type': 'noul', 'instructions': 'Does this satisfy the task?'}}
        DecisionStore(workspace or self.workspace).append('decision', id=identifier, kind='acceptance',
            mode='shadow', status=status, state=identifier, questions=questions,
            schema_hash=digest(questions), truncated=False, context={'group': identifier},
            prediction={}, time_ms=when, **extra)

    def survey(self, repo, complete=True, when=100, workspace=None):
        workspace = workspace or self.workspace
        identifier = 'truffle-000000000001'
        self.write(workspace / '.fusion/truffle' / identifier / 'hunt.json', {
            'id': identifier, 'kind': 'survey', 'repo': repo, 'status': 'ready',
            'started_at_ms': when - 1, 'synced_at_ms': when,
            'inventory_complete': complete, 'issues': [{'number': 1, 'grade': 'U'}],
            'candidates': [], 'selected': [], 'patches': [],
        })
        return identifier

    def test_nonrepository_and_repository_without_origin_have_distinct_reasons(self):
        for initialized in (False, True):
            with self.subTest(initialized=initialized):
                if initialized:
                    self.repo()
                result = self.observe()
                self.assertEqual(result['schema'], 'fusion.capabilities.v1')
                self.assertIsInstance(result['observed_at_ms'], int)
                self.assertEqual(result['workspace']['path'], str(self.workspace))
                self.assertEqual(result['github']['status'], 'missing_remote' if initialized else 'unavailable')
                self.assertEqual(result['github']['auth'], 'unchecked')
                self.assertIsNone(result['github']['snapshot'])

    def test_selected_custom_remote_is_used_instead_of_origin(self):
        self.repo('git@github.com:wrong/origin.git')
        self.git_command('config', 'remote.product.url', 'https://github.com/owned/product.git')
        self.config['publish'] = {'remote': 'product'}
        self.save_config()
        result = self.observe()['github']
        self.assertEqual(result['remote'], 'product')
        self.assertEqual(result['repo'], 'owned/product')
        self.assertEqual(result['auth'], 'unchecked')

    def test_unsupported_remote_urls_do_not_leak_credentials(self):
        self.repo()
        for remote in ('https://gitlab.example/owned/product.git',
                       'https://secret-user:private-token@github.com/owned/product.git',
                       'https://github.com/owned/product.git?token=private-query-token'):
            with self.subTest(remote_kind=remote.split(':')[0]):
                self.git_command('config', 'remote.origin.url', remote)
                result = self.observe()
                self.assertEqual(result['github']['status'], 'unsupported_remote')
                for secret in ('secret-user', 'private-token', 'private-query-token'):
                    self.assertNotIn(secret, json.dumps(result))

    def test_snapshot_preserves_source_time_and_does_not_follow_changed_remote(self):
        self.repo('https://github.com/owned/current.git')
        identifier = self.survey('owned/previous', when=123)
        snapshot = self.observe()['github']['snapshot']
        self.assertEqual(snapshot['id'], identifier)
        self.assertEqual(snapshot['repo'], 'owned/previous')
        self.assertEqual(snapshot['synced_at_ms'], 123)
        self.assertEqual(snapshot['issue_count'], 1)
        self.assertTrue(snapshot['complete'])
        self.assertFalse(snapshot['matches_remote'])
        self.survey('owned/current', complete=False, when=124)
        snapshot = self.observe()['github']['snapshot']
        self.assertTrue(snapshot['matches_remote'])
        self.assertFalse(snapshot['complete'])

    def test_registered_workspace_history_is_not_pooled_into_selected_workspace(self):
        other = self.root / 'other'
        other.mkdir()
        self.repo('https://github.com/owned/other.git', workspace=other)
        self.survey('owned/other', workspace=other)
        self.decision('other-reviewed', workspace=other, model_identity='other-model')
        DecisionStore(other).label('other-reviewed', {'plausible': 'true'}, 'Synthetic fixture approval')
        self.app.add_workspace(str(other), save=False)
        result = self.observe()
        self.assertEqual(result['learning']['scope'], 'workspace')
        self.assertEqual(result['learning']['labels']['approved_answers'], 0)
        self.assertIsNone(result['github']['snapshot'])
        sources = {r['path']: r for r in result['workspaces']}
        self.assertEqual(sources[str(other)]['github_repo'], 'owned/other')
        self.assertIsNone(sources[str(self.workspace)]['github_repo'])

    def test_empty_labels_and_disabled_training_are_separate_from_runtime_observation(self):
        self.decision('saved-success', model_identity='fixture-model', when=123)
        result = self.observe()['learning']
        self.assertEqual(result['runtime']['status'], 'observed')
        self.assertEqual(result['runtime']['last_observed_identity'], 'fixture-model')
        self.assertEqual(result['runtime']['last_observed_at_ms'], 123)
        self.assertEqual(result['labels'], {'approved_answers': 0, 'train_groups': 0,
            'validation_groups': 0, 'min_train_groups': 2, 'min_validation_groups': 2})
        self.assertFalse(result['training_enabled'])
        self.assertEqual(result['training_state'], 'paused')

    def test_latest_unavailable_runtime_record_is_not_hidden_by_older_success(self):
        self.decision('old-success', model_identity='fixture-model', when=123)
        self.decision('new-failure', status='unavailable', error='fixture checkpoint missing', when=456)
        runtime = self.observe()['learning']['runtime']
        self.assertEqual(runtime['status'], 'observed', 'This describes a historical success, not current health')
        self.assertEqual(runtime['latest_status'], 'unavailable')
        self.assertEqual(runtime['latest_at_ms'], 456)
        self.assertEqual(runtime['last_observed_at_ms'], 123)
        self.assertIn('did not succeed', runtime['reason'])

    def test_missing_python_is_unavailable_without_overwriting_historical_identity(self):
        self.decision('saved-success', model_identity='old-model')
        self.config['decisions'].update(python=str(self.root / 'missing-python'), model_path='new-candidate')
        self.save_config()
        runtime = self.observe()['learning']['runtime']
        self.assertEqual(runtime['status'], 'unavailable')
        self.assertEqual(runtime['last_observed_identity'], 'old-model')
        self.assertEqual(runtime['configured_model_path'], 'new-candidate')

    def test_cli_absence_does_not_erase_source_or_invent_authentication(self):
        self.repo('https://github.com/owned/project.git')
        original = shutil.which
        with patch('fusion_capabilities.shutil.which', side_effect=lambda command: None if command == 'gh' else original(command)):
            github = self.observe()['github']
        self.assertEqual(github['status'], 'configured')
        self.assertEqual(github['repo'], 'owned/project')
        self.assertFalse(github['cli_available'])
        self.assertEqual(github['auth'], 'unchecked')

    def test_group_threshold_is_prerequisite_and_does_not_claim_model_qualification(self):
        self.write(self.workspace / '.fusion/decisions/training/settings.json', {'enabled': True})
        ids = {'train': [], 'validation': []}
        for number in range(100):
            identifier = 'group-' + str(number)
            split = 'validation' if int(digest(identifier)[:8], 16) % 5 == 0 else 'train'
            if len(ids[split]) < 2:
                ids[split].append(identifier)
            if all(len(values) == 2 for values in ids.values()):
                break
        for offset in range(2):
            for split in ids:
                identifier = ids[split][offset]
                self.decision(identifier)
                DecisionStore(self.workspace).label(identifier, {'plausible': 'true'}, 'Synthetic fixture approval')
            learning = self.observe()['learning']
            self.assertEqual(learning['labels']['train_groups'], offset + 1)
            self.assertEqual(learning['labels']['validation_groups'], offset + 1)
            self.assertEqual(learning['training_state'], 'gathering' if offset == 0 else 'ready')
            self.assertEqual(learning['runtime']['status'], 'unchecked')
            self.assertIn('prerequisites', learning['qualification_note'])
            self.assertIn('promotion', learning['qualification_note'])
        self.assertEqual(self.app.jobs(self.workspace), [], 'Observation never starts the ready round')

    def test_paused_training_does_not_hide_an_existing_active_job(self):
        self.write(self.workspace / '.fusion/decisions/training/rounds/round-1/round.json', {
            'id': 'round-1', 'number': 1, 'status': 'running', 'phase': 'train',
            'active_job': 'job-1', 'started_at_ms': 123,
        })
        self.write(self.workspace / '.fusion/ui/jobs/job-1/job.json', {
            'id': 'job-1', 'action': 'train', 'status': 'running',
            'supervisor_pid': os.getpid(), 'started_at_ms': 123,
        })
        learning = self.observe()['learning']
        self.assertFalse(learning['training_enabled'])
        self.assertEqual(learning['training_state'], 'paused')
        self.assertEqual(learning['active_job']['id'], 'job-1')
        self.assertEqual(learning['active_job']['status'], 'running')

    def test_malformed_learning_evidence_is_unknown_not_empty_or_ready(self):
        journal = self.workspace / '.fusion/decisions/events.jsonl'
        for content in ('[]\n', '{"event":"decision","status":"ok"}\n', '{"event":"decision"'):
            with self.subTest(content=content):
                journal.parent.mkdir(parents=True, exist_ok=True)
                journal.write_text(content)
                learning = self.observe()['learning']
                self.assertEqual(learning['status'], 'unreadable')
                self.assertIsNone(learning['labels'])
                self.assertIsNone(learning['training_enabled'])
                self.assertEqual(learning['training_state'], 'unknown')
                self.assertEqual(learning['runtime']['status'], 'unchecked')
                self.assertEqual(journal.read_text(), content)

    def test_malformed_survey_does_not_hide_other_capability_observations(self):
        path = self.workspace / '.fusion/truffle/truffle-000000000001/hunt.json'
        for value in ([], {'kind': 'survey', 'id': 'truffle-000000000001'}):
            with self.subTest(value=value):
                self.write(path, value)
                result = self.observe()
                self.assertIsNone(result['github']['snapshot'])
                self.assertTrue(result['github']['snapshot_error'])
                self.assertEqual(result['learning']['labels']['approved_answers'], 0)

    def test_unreadable_training_files_do_not_become_disabled_defaults_or_empty_history(self):
        cases = [
            ('settings.json', '[]'),
            ('settings.json', '{"enabled":'),
            ('settings.json', '{"enabled":"false"}'),
            ('settings.json', '{"enabled":true,"min_new_answers":false}'),
            ('rounds/round-1/round.json', '[]'),
            ('rounds/round-1/round.json', '{"status":"running"'),
        ]
        for relative, content in cases:
            with self.subTest(file=relative, content=content):
                path = self.workspace / '.fusion/decisions/training' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                try:
                    learning = self.observe()['learning']
                    self.assertEqual(learning['status'], 'unreadable')
                    self.assertIsNone(learning['labels'])
                    self.assertIsNone(learning['training_enabled'])
                    self.assertEqual(learning['training_state'], 'unknown')
                    self.assertEqual(path.read_text(), content)
                finally:
                    path.unlink()

    def test_malformed_config_error_does_not_return_its_content(self):
        (self.workspace / '.fusion.json').write_text('{"secret": "private-config-marker"')
        with self.assertRaises(ValueError) as caught:
            self.observe()
        self.assertNotIn('private-config-marker', str(caught.exception))

    def test_read_is_local_and_leaves_workspace_and_home_unchanged(self):
        self.repo('https://github.com/owned/project.git')
        self.decision('saved-success', model_identity='fixture-model')
        def tree():
            return {str(p.relative_to(self.root)): (p.stat().st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in self.root.rglob('*') if p.is_file()}
        before = tree()
        original_popen, original_import = subprocess.Popen, builtins.__import__
        commands, forbidden_imports = [], []
        def local_process(argv, *args, **kwargs):
            self.assertIsInstance(argv, (list, tuple))
            self.assertEqual(Path(argv[0]).name, 'git', f'Unexpected executable: {argv[0]}')
            self.assertFalse(kwargs.get('shell'))
            self.assertFalse(any(a in {'fetch', 'push', 'clone', 'pull', 'ls-remote'} for a in argv[1:]))
            commands.append(list(argv))
            return original_popen(argv, *args, **kwargs)
        def import_guard(name, *args, **kwargs):
            if name.split('.')[0] in {'fusion_laya', 'laya', 'torch', 'huggingface_hub'}:
                forbidden_imports.append(name)
                raise AssertionError('Capability read imported inference dependencies')
            return original_import(name, *args, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch('subprocess.Popen', side_effect=local_process))
            stack.enter_context(patch('builtins.__import__', side_effect=import_guard))
            network = stack.enter_context(patch('socket.socket', side_effect=AssertionError('Network is forbidden')))
            dispatch = stack.enter_context(patch('fusion_core.dispatch', side_effect=AssertionError('Dispatch is forbidden')))
            launch = stack.enter_context(patch.object(self.app, 'launch', side_effect=AssertionError('Launch is forbidden')))
            for _ in range(2):
                self.assertEqual(self.observe()['github']['repo'], 'owned/project')
            network.assert_not_called()
            dispatch.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(forbidden_imports, [])
        self.assertTrue(commands, 'Exercise the real local Git configuration path')
        self.assertEqual(tree(), before)

    def test_capability_http_route_requires_token_host_origin_and_known_workspace(self):
        with patch.object(self.app, 'garden_tick', return_value=None):
            server = Server(0, self.app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(query='', **headers):
                    req = Request(server.origin + '/api/capabilities' + query,
                        headers={'X-Fusion-Token': server.token, **headers})
                    try:
                        response = urlopen(req, timeout=4)
                    except HTTPError as error:
                        response = error
                    with response:
                        return response.status, json.loads(response.read())
                self.assertEqual(request(**{'X-Fusion-Token': ''})[0], 401)
                self.assertEqual(request(Host='evil.example')[0], 403)
                self.assertEqual(request(Origin='https://evil.example')[0], 403)
                self.assertEqual(request('?w=missing-workspace')[0], 400)
                status, body = request()
                self.assertEqual(status, 200)
                self.assertEqual(body['schema'], 'fusion.capabilities.v1')
                self.assertEqual(body['workspace']['id'], self.app.default)
                self.assertEqual(self.app.jobs(self.workspace), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
