"""Claude readers are read-only by construction: their settings deny file edits, keep Bash in the
sandbox with nothing writable but the run's own temp directory, and never grant the shared
/tmp/claude-<uid>."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core

UID = os.getuid()
SHARED = re.compile(r"(?:/private)?/tmp/claude-\d+")
PROMPT = f"Write only inside the working directory and $TMPDIR (/tmp/claude-{UID}; /tmp itself is not writable)."


def settings_sources(argv):
    sources = [argv[i + 1] for i, item in enumerate(argv) if item == "--settings"]
    return [json.loads(s) if s.lstrip().startswith("{") else json.loads(Path(s).read_text()) for s in sources]


class ClaudeReaderScopeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        self.settings_file = self.root / "worker-settings.json"
        self.settings_file.write_text(json.dumps({
            "permissions": {"deny": ["Bash(rm:*)"], "additionalDirectories": [
                f"/tmp/claude-{UID}", f"/private/tmp/claude-{UID}/", str(self.root / "evidence")]},
            "sandbox": {"enabled": True, "filesystem": {"allowWrite": [str(self.root / "cache")]}}}))
        self.launcher = ["--settings", str(self.settings_file), "--append-system-prompt", PROMPT]

    def command(self, write=False, launcher=None, **settings):
        task = core.make_task(self.workspace, "claude", "look at it", "triage-locate", [], [], None, False, write,
                              settings_overrides={"launcher_args": self.launcher if launcher is None else launcher, **settings})
        return core.agent_command(core.DEFAULTS, task, None)

    def test_a_reader_gets_one_writable_place_and_no_file_edit_tools(self):
        argv, env, metadata = self.command()
        sources = settings_sources(argv)
        self.assertEqual(len(sources), 1)
        settings = sources[0]
        self.assertTrue(set(core.CLAUDE_READER_DENIED_TOOLS) <= set(settings["permissions"]["deny"]))
        self.assertIn("Bash(rm:*)", settings["permissions"]["deny"])
        self.assertEqual(settings["permissions"]["additionalDirectories"], [str(self.root / "evidence")])
        self.assertEqual(settings["sandbox"]["filesystem"]["allowWrite"], [])
        self.assertIn(os.path.abspath(self.workspace), settings["sandbox"]["filesystem"]["denyWrite"])
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        self.assertTrue(settings["disableAllHooks"])
        directory = metadata["reader_tmpdir"]
        self.assertTrue(directory.startswith(str(Path("/tmp").resolve() / "orc-")))
        self.assertFalse(Path(directory).exists())
        self.assertEqual(env["CLAUDE_CODE_TMPDIR"], directory)
        self.assertEqual((env["TMPDIR"].rstrip("/"), env["TMP"], env["TEMP"]), (directory,) * 3)
        prompt = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn(f"{directory}/claude-{UID}", prompt)
        self.assertIn("read-only", prompt)
        self.assertIsNone(SHARED.search(prompt))
        self.assertIsNone(SHARED.search(json.dumps(settings)))
        self.assertEqual(json.loads(self.settings_file.read_text())["sandbox"]["filesystem"]["allowWrite"], [str(self.root / "cache")])

    def test_a_reader_without_settings_or_with_inline_settings_is_scoped_too(self):
        for launcher in ([], ["--settings", '{"sandbox":{"enabled":true}}']):
            with self.subTest(launcher=launcher):
                argv, _, metadata = self.command(launcher=launcher)
                settings = settings_sources(argv)[0]
                self.assertTrue(set(core.CLAUDE_READER_DENIED_TOOLS) <= set(settings["permissions"]["deny"]))
                self.assertEqual(settings["sandbox"]["filesystem"]["allowWrite"], [])
                self.assertTrue(settings["disableAllHooks"])
                self.assertIn(metadata["reader_tmpdir"], argv[argv.index("--append-system-prompt") + 1])

    def test_a_reader_route_that_keeps_user_hooks_is_still_scoped(self):
        argv, _, _ = self.command(user_hooks=True)
        settings = settings_sources(argv)[0]
        self.assertNotIn("disableAllHooks", settings)
        self.assertTrue(set(core.CLAUDE_READER_DENIED_TOOLS) <= set(settings["permissions"]["deny"]))
        self.assertEqual(settings["sandbox"]["filesystem"]["allowWrite"], [])

    def test_a_writer_is_not_scoped(self):
        argv, env, metadata = self.command(write=True)
        settings = settings_sources(argv)[0]
        self.assertEqual(settings["permissions"], json.loads(self.settings_file.read_text())["permissions"])
        self.assertEqual(settings["sandbox"], json.loads(self.settings_file.read_text())["sandbox"])
        self.assertNotIn("reader_tmpdir", metadata)
        self.assertEqual(env.get("CLAUDE_CODE_TMPDIR"), os.environ.get("CLAUDE_CODE_TMPDIR"))
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], PROMPT)

    def test_a_readers_caches_live_in_its_own_sandbox_temp_directory(self):
        with patch.dict(os.environ, {"PYTEST_ADDOPTS": "-q"}):
            _, env, metadata = self.command()
        own = f"{metadata['reader_tmpdir']}/claude-{UID}"
        for name in core.READER_CACHE_DIRS:
            self.assertTrue(env[name].startswith(own + "/"), (name, env[name]))
        self.assertEqual(env["PYTEST_ADDOPTS"], "-q -p no:cacheprovider")
        self.assertFalse(any(SHARED.fullmatch(env[name].rsplit("/", 1)[0]) for name in core.READER_CACHE_DIRS))

    def test_a_writers_caches_are_left_alone(self):
        with patch.dict(os.environ, {"PYTEST_ADDOPTS": "-q"}):
            _, env, _ = self.command(write=True)
        for name in core.READER_CACHE_DIRS:
            self.assertEqual(env.get(name), os.environ.get(name), name)
        self.assertEqual(env["PYTEST_ADDOPTS"], "-q")

    def test_unreadable_settings_raise_rather_than_launch_unscoped(self):
        broken = self.root / "broken.json"
        broken.write_text("{not json")
        for launcher in (["--settings", str(broken)], ["--settings", str(self.root / "absent.json")], ["--settings", "[1]"]):
            with self.subTest(launcher=launcher), self.assertRaises(ValueError):
                core.claude_reader_settings(launcher, str(self.workspace))

    def test_dispatch_creates_the_reader_directory_private_and_removes_it(self):
        seen = self.root / "seen.json"
        fake = self.root / "claude"
        fake.write_text(f"#!{sys.executable}\nimport json, os, stat\nd = os.environ['CLAUDE_CODE_TMPDIR']\n"
                        f"open({str(seen)!r}, 'w').write(json.dumps({{'dir': d, 'isdir': os.path.isdir(d), "
                        "'mode': stat.S_IMODE(os.stat(d).st_mode)}))\n"
                        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                        "'result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n")
        fake.chmod(0o755)
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {
            "command": str(fake), "launcher_args": self.launcher}})
        env = {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"), "FUSION_PROGRESS": "0",
               "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"}
        out = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(core, "load_config", return_value=(config, None)):
            code = core.main(["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude",
                              "--role", "triage-locate", "--read-only", "look at it"])
        self.assertEqual(code, 0, out.getvalue())
        record = json.loads(seen.read_text())
        self.assertTrue(record["isdir"])
        self.assertEqual(record["mode"], stat.S_IRWXU)
        self.assertFalse(Path(record["dir"]).exists())


if __name__ == "__main__":
    unittest.main()
