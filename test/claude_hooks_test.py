"""Claude workers run hermetic: user and project hooks are off unless a route opts in, and ORC's own
permission rules still reach the worker."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core

# How Claude Code 2.1.292 treated hooks when probed: a `disableAllHooks: true` in any `--settings`
# source (file or inline JSON) suppressed both user-level and project-level hooks.
FAKE_CLAUDE = r'''#!{python}
import json, os, subprocess, sys
from pathlib import Path
argv = sys.argv
sources = [argv[i + 1] for i, item in enumerate(argv) if item == "--settings" and i + 1 < len(argv)]
loaded = [json.loads(s) if s.lstrip().startswith("{") else json.loads(Path(s).read_text()) for s in sources]
Path(os.environ["FAKE_SEEN"]).write_text(json.dumps(loaded))
if not any(item.get("disableAllHooks") is True for item in loaded):
    user = json.loads((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json").read_text())
    for group in user.get("hooks", {}).get("Stop", []):
        for hook in group["hooks"]:
            subprocess.run(hook["command"], shell=True, check=False)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "s",
                  "result": "STATUS: success\nSUMMARY: done\nCHANGED: none\nTESTS: none\nBLOCKERS: none"}))
'''


def settings_sources(argv):
    sources = [argv[i + 1] for i, item in enumerate(argv) if item == "--settings"]
    return [json.loads(s) if s.lstrip().startswith("{") else json.loads(Path(s).read_text()) for s in sources]


class ClaudeHooksTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        self.settings_file = self.root / "worker-settings.json"
        self.settings_file.write_text(json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}, "sandbox": {"enabled": True}}))

    def task(self, write=False, **settings):
        return core.make_task(self.workspace, "claude", "look at it", "triage-locate", [], [], None, False, write,
                              settings_overrides=settings)

    def test_hooks_are_off_by_default_and_deny_rules_still_reach_the_worker(self):
        for launcher in ([], ["--settings", str(self.settings_file)], ["--settings", '{"sandbox":{"enabled":true}}']):
            with self.subTest(launcher=launcher):
                argv, _, _ = core.agent_command(core.DEFAULTS, self.task(launcher_args=launcher), None)
                sources = settings_sources(argv)
                self.assertTrue(sources and all(item.get("disableAllHooks") is True for item in sources))
                if launcher[1:] == [str(self.settings_file)]:
                    self.assertEqual(sources[0]["permissions"]["deny"], ["Bash(rm:*)"])
                    self.assertNotIn("disableAllHooks", json.loads(self.settings_file.read_text()))
                if launcher[1:2] and launcher[1].startswith("{"):
                    self.assertTrue(sources[0]["sandbox"]["enabled"])

    def test_a_route_that_opts_in_keeps_user_hooks(self):
        argv, _, _ = core.agent_command(core.DEFAULTS, self.task(user_hooks=True, launcher_args=["--settings", str(self.settings_file)]), None)
        self.assertEqual(settings_sources(argv), [json.loads(self.settings_file.read_text())])

    def test_yolo_settings_carry_both_sandbox_off_and_hooks_off(self):
        config = core.deep_merge(core.DEFAULTS, {"execution_mode": "yolo", "routes": {}, "decisions": {"mode": "off"}})
        for opt_in, expected in ((False, {"sandbox": {"enabled": False}, "disableAllHooks": True}),
                                 (True, {"sandbox": {"enabled": False}})):
            with self.subTest(user_hooks=opt_in):
                task = self.task(write=True, user_hooks=opt_in)
                argv, _, _ = core.agent_command(config, task, None)
                self.assertEqual(settings_sources(argv), [expected])

    def dispatch_with(self, launcher, **claude):
        started = self.root / "worker-started"
        started.unlink(missing_ok=True)
        fake = self.root / "claude-start-only"
        fake.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport json\nPath({str(started)!r}).write_text('started')\n"
                        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                        "'result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n")
        fake.chmod(0o755)
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {
            "command": str(fake), "launcher_args": launcher, **claude}})
        task = core.make_task(self.workspace, "claude", "look at it", "triage-locate", [], [], None, False, False)
        env = {"ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"}
        with patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()):
            value = core.dispatch(config, task, core.RunStore(self.workspace))
        return value, started.exists()

    def test_settings_that_cannot_be_read_refuse_the_run_before_a_worker_starts(self):
        missing = self.root / "absent-settings.json"
        broken = self.root / "broken-settings.json"
        broken.write_text("{not json")
        for launcher, named in ((["--settings", '{"sandbox":'], "inline --settings JSON does not parse"),
                                (["--settings", "[1, 2]"], None),
                                (["--settings", str(missing)], str(missing)),
                                (["--settings", str(broken)], str(broken)),
                                (["--settings"], "--settings has no value")):
            with self.subTest(launcher=launcher):
                value, ran = self.dispatch_with(launcher)
                self.assertFalse(ran)
                self.assertEqual(value["status"], "error")
                self.assertEqual(value["provider_failure"], "settings_unreadable")
                self.assertTrue(value["blockers"][0].startswith("settings unreadable: "))
                if named:
                    self.assertIn(named, value["blockers"][0])
                with self.assertRaises(ValueError):
                    core.claude_hooks_off(launcher)

    def test_a_route_that_keeps_user_hooks_passes_its_settings_through_unread(self):
        value, ran = self.dispatch_with(["--settings", str(self.root / "absent-settings.json")], user_hooks=True)
        self.assertTrue(ran)
        self.assertNotEqual(value.get("provider_failure"), "settings_unreadable")

    def run_worker(self, user_hooks):
        config_dir = self.root / f"claude-config-{user_hooks}"
        config_dir.mkdir()
        marker = self.root / f"stop-hook-ran-{user_hooks}"
        (config_dir / "settings.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"touch {marker}"}]}]}}))
        fake = self.root / "claude"
        fake.write_text(FAKE_CLAUDE.replace("{python}", sys.executable))
        fake.chmod(0o755)
        seen = self.root / f"seen-{user_hooks}.json"
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {
            "command": str(fake), "user_hooks": user_hooks, "launcher_args": ["--settings", str(self.settings_file)]}})
        env = {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"), "FUSION_PROGRESS": "0",
               "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off", "CLAUDE_CONFIG_DIR": str(config_dir),
               "FAKE_SEEN": str(seen)}
        out = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(core, "load_config", return_value=(config, None)):
            code = core.main(["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude",
                              "--role", "triage-locate", "--read-only", "look at it"])
        self.assertEqual(code, 0, out.getvalue())
        return marker.exists(), json.loads(seen.read_text())

    def test_a_user_stop_hook_does_not_run_inside_a_worker(self):
        ran, seen = self.run_worker(False)
        self.assertFalse(ran)
        self.assertEqual(seen[0]["permissions"]["deny"], ["Bash(rm:*)"])
        ran, _ = self.run_worker(True)
        self.assertTrue(ran)


if __name__ == "__main__":
    unittest.main()
