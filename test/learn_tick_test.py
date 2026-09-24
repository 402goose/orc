"""`fusion learn` advances the loop without a server, and does nothing unless asked."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_garden as garden
import fusion_learn_cli as learn
from fusion_decisions import DecisionStore
from fusion_publish import save
from fusion_ui import ControlRoom
from ui_test import seed_workspace


def snapshot(root):
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in Path(root).rglob('*')}


class LearnTickTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.w = self.root / 'repo'
        seed_workspace(self.w)
        self.orc_home = self.root / 'orc'
        env = patch.dict(os.environ, {'ORC_HOME': str(self.orc_home), 'FUSION_DECISIONS_MODE': 'off'})
        env.start()
        self.addCleanup(env.stop)
        DecisionStore(self.w).append('decision', id='d1', kind='acceptance', mode='shadow', status='ok', truncated=False,
                                     state='{"task":"Fix retries"}', schema_hash='fixture', context={},
                                     questions={'plausible': {'type': 'noul', 'instructions': 'Does it satisfy the task?'}},
                                     prediction={'plausible': {'false': .7, 'true': .3}})
        self.launched = []
        launch = patch.object(ControlRoom, 'launch', autospec=True, side_effect=self.fake_launch)
        launch.start()
        self.addCleanup(launch.stop)

    def fake_launch(self, app, workspace, body, *, garden=False):
        # A live supervisor (this process) keeps the job active, as a real one would.
        self.launched.append(body)
        job = {'id': 'job-%d' % len(self.launched), 'action': body['action'], 'status': 'running',
               'supervisor_pid': os.getpid(), 'started_at_ms': core.now_ms(), 'garden': garden,
               'decision_id': body.get('decision_id'), 'garden_policy': body.get('garden_policy')}
        save(Path(workspace) / '.fusion/ui/jobs' / job['id'] / 'job.json', job)
        return job

    def cli(self, *argv, workspace=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = core.main(['--workspace', str(workspace or self.w), *argv])
        return code, out.getvalue()

    def test_tick_on_a_disabled_workspace_creates_nothing(self):
        before = snapshot(self.root)
        code, output = self.cli('learn', 'tick')
        self.assertEqual(code, 0, output)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.launched, [])
        value = json.loads(output)
        self.assertEqual((value['garden']['state'], value['training']['state']), ('paused', 'paused'))
        self.assertEqual(value['launched'], [])

    def test_enabled_garden_launches_one_draft_and_waits_while_it_is_active(self):
        garden.save(ControlRoom(self.w), self.w, {'enabled': True, 'agent': 'codex', 'include_existing': True})
        code, output = self.cli('learn', 'tick')
        self.assertEqual(code, 0, output)
        self.assertEqual([(b['action'], b['decision_id']) for b in self.launched], [('suggest-labels', 'd1')])
        first = json.loads(output)
        self.assertEqual(first['garden']['state'], 'drafting')
        self.assertEqual([j['id'] for j in first['launched']], ['job-1'])
        code, output = self.cli('learn', 'tick')
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(json.loads(output)['launched'], [])
        # Training stayed off: its settings, rounds and lock were never created.
        self.assertFalse((self.w / '.fusion/decisions/training').exists())

    def test_all_reads_the_registry_and_reports_each_workspace(self):
        other = self.root / 'other'
        other.mkdir()
        save(self.orc_home / 'ui-workspaces.json', {'paths': [str(other)]})
        code, output = self.cli('learn', 'tick', '--all')
        self.assertEqual(code, 0, output)
        self.assertEqual({json.loads(line)['workspace'] for line in output.splitlines()}, {str(other), str(self.w)})
        # Launchd's working directory is not a workspace just because it is the cwd.
        code, output = self.cli('learn', 'tick', '--all', workspace=self.root)
        self.assertEqual(code, 0, output)
        self.assertEqual([json.loads(line)['workspace'] for line in output.splitlines()], [str(other)])

    def test_status_summarizes_the_loop_read_only(self):
        before = snapshot(self.root)
        code, output = self.cli('learn', 'status')
        self.assertEqual(code, 0, output)
        self.assertEqual(snapshot(self.root), before)
        [value] = json.loads(output)
        self.assertEqual(set(value), {'workspace', 'garden', 'training', 'decisions', 'laya'})
        self.assertEqual(value['decisions']['states'], {'needs_draft': 1})
        self.assertEqual((value['decisions']['drafts'], value['decisions']['approved_answers']), (0, 0))
        self.assertEqual(set(value['garden']) >= {'enabled', 'approval_mode', 'queued', 'state'}, True)
        self.assertEqual(set(value['training']) >= {'enabled', 'min_new_answers', 'last_round', 'state'}, True)
        self.assertEqual(set(value['laya']) >= {'mode', 'model_path', 'qualified_buckets'}, True)

    def test_unknown_workspace_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli('learn', 'tick', '--workspace', str(self.root / 'missing'))


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.calls = []
        self.tools = {'claude': '/opt/claude/bin/claude', 'codex': '/opt/codex/bin/codex', 'node': '/opt/node/bin/node'}

    def launchctl(self, *argv):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '', '')

    def schedule(self, action, *extra, platform='darwin'):
        args = core.build_parser().parse_args(['learn', 'schedule', action, *extra])
        out = io.StringIO()
        code = learn.schedule(args, self.home / 'repo', run=self.launchctl, platform=platform,
                              which=self.tools.get, home=self.home, out=out)
        return code, out.getvalue()

    def test_install_writes_a_loadable_plist_and_uninstall_removes_it(self):
        code, output = self.schedule('install', '--interval', '600', '--workspace', str(self.home / 'a'))
        self.assertEqual(code, 0, output)
        target = self.home / 'Library/LaunchAgents/ai.orc.fusion-learn.plist'
        value = plistlib.loads(target.read_bytes())
        self.assertEqual(value['ProgramArguments'][-4:], ['learn', 'tick', '--workspace', str(self.home / 'a')])
        self.assertEqual(value['ProgramArguments'][:2], [sys.executable, str(Path(learn.__file__).resolve().with_name('fusion'))])
        self.assertEqual(value['StartInterval'], 600)
        path = value['EnvironmentVariables']['PATH'].split(':')
        self.assertTrue({'/opt/claude/bin', '/opt/codex/bin', '/opt/node/bin', '/usr/bin'} <= set(path))
        self.assertEqual(value['StandardOutPath'], str(self.home / '.local/share/orc/learn.log'))
        self.assertEqual(value['StandardErrorPath'], value['StandardOutPath'])
        self.assertTrue(value['AbandonProcessGroup'])
        uid = f'gui/{os.getuid()}'
        self.assertEqual(self.calls, [('bootout', uid + '/ai.orc.fusion-learn'), ('bootstrap', uid, str(target))])
        self.assertIn(target.read_text(), output)
        self.calls.clear()
        code, output = self.schedule('uninstall')
        self.assertEqual(code, 0)
        self.assertFalse(target.exists())
        self.assertEqual(self.calls, [('bootout', uid + '/ai.orc.fusion-learn')])

    def test_all_keeps_the_install_workspace_and_status_reads_the_plist(self):
        self.schedule('install', '--all')
        code, output = self.schedule('status')
        value = json.loads(output)
        self.assertTrue(value['installed'] and value['loaded'])
        self.assertEqual(value['program'][2:], ['--workspace', str(self.home / 'repo'), 'learn', 'tick', '--all'])
        self.assertEqual(value['interval'], 300)

    def test_other_platforms_get_a_crontab_line_and_nothing_is_installed(self):
        code, output = self.schedule('install', platform='linux')
        self.assertEqual(code, 0)
        self.assertIn('*/5 * * * *', output)
        self.assertIn('learn tick --workspace', output)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / 'Library').exists())

    def test_interval_below_a_minute_is_refused(self):
        with self.assertRaises(ValueError):
            self.schedule('install', '--interval', '5')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
