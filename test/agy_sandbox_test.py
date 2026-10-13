"""agy writers run inside ORC's own sandbox-exec profile (issue #80): writes reach the workspace,
its Git metadata, a per-run temp directory and `agy.writer_allow`, nothing shared. Readers keep
`--mode plan --sandbox`; YOLO writers run unwrapped; a host without sandbox-exec refuses writers."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core

WRAPPER = "/usr/bin/sandbox-exec"
HANDOFF = "STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none"


def nested_sandbox_exec():
    """Whether sandbox-exec can apply a profile here (it cannot inside another sandbox)."""
    if core.sandbox_exec() is None:
        return False
    try:
        return subprocess.run([WRAPPER, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class AgySandboxTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        home = patch.dict(os.environ, {"HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)
        # The test's own temp directory sits in a real shared root, so stand-ins take their place.
        self.shared = [str(self.root / "tmp"), str(self.root / "folders")]
        self.real_shared_write_roots = core.shared_write_roots
        cache = patch.object(core, "darwin_user_cache_dir", return_value=None)
        cache.start()
        self.addCleanup(cache.stop)
        roots = patch.object(core, "shared_write_roots", return_value=self.shared)
        roots.start()
        self.addCleanup(roots.stop)

    def command(self, write=True, config=None, wrapper=WRAPPER, workspace=None, **settings):
        task = core.make_task(workspace or self.workspace, "agy", "edit it", "implement", [], [], None, False, write,
                              settings_overrides=settings)
        with patch.object(core, "sandbox_exec", return_value=wrapper):
            # Other suites mutate DEFAULTS' shared agy table, so the command is pinned here.
            return core.agent_command(core.deep_merge(core.deep_merge(core.DEFAULTS, {"agy": {"command": "agy"}}),
                                                      config or {}), task, None)

    def test_a_reader_keeps_plan_and_agys_own_sandbox_exactly(self):
        for wrapper in (WRAPPER, None):
            with self.subTest(wrapper=wrapper):
                argv, env, metadata = self.command(write=False, wrapper=wrapper)
                self.assertEqual(argv, ["agy", "-p", argv[2], "--output-format", "json", "--mode", "plan", "--sandbox"])
                self.assertNotIn("run_tmpdir", metadata)
                self.assertEqual(env.get("TMPDIR"), os.environ.get("TMPDIR"))

    def test_a_writer_is_wrapped_and_the_outer_profile_replaces_agys_own_sandbox(self):
        argv, env, metadata = self.command()
        self.assertEqual(argv[:2], [WRAPPER, "-p"])
        self.assertEqual(argv[3:], ["agy", "-p", argv[5], "--output-format", "json", "--mode", "accept-edits",
                                    "--dangerously-skip-permissions"])
        directory = metadata["run_tmpdir"]
        self.assertTrue(directory.startswith(str(Path("/tmp").resolve() / "orc-run-")))
        self.assertFalse(Path(directory).exists())
        self.assertEqual((env["TMPDIR"], env["TMP"], env["TEMP"]), (directory + "/", directory, directory))
        for name in core.READER_CACHE_DIRS:
            self.assertTrue(env[name].startswith(directory + "/"), name)

    def test_the_profile_denies_every_write_but_the_workspace_run_temp_and_agy_state(self):
        argv, _, metadata = self.command()
        profile = argv[2]
        lines = profile.splitlines()
        self.assertEqual(lines[:4], ["(version 1)", "(allow default)", "(deny file-write*)", "(allow file-write*"])
        state = os.path.realpath(self.home / ".gemini/antigravity-cli")
        allowed = [line.strip() for line in lines[4:] if line.strip()]
        self.assertEqual(allowed, [f'(subpath "{self.workspace}")', f'(subpath "{metadata["run_tmpdir"]}")',
                                   f'(subpath "{state}")', '(literal "/dev/null")', '(literal "/dev/ptmx")',
                                   '(literal "/dev/dtracehelper")', '(regex #"^/dev/tty[^/]*$"))'])
        for shared in ("/tmp", "/private/tmp", "/var/folders", os.path.realpath(tempfile.gettempdir()), str(self.home)):
            self.assertNotIn(f'(subpath "{shared}")', profile)
            self.assertNotIn(f'(subpath "{shared}/")', profile)

    def test_the_per_user_cache_dir_is_writable_so_agy_can_load_its_login(self):
        cache = str(self.root / "folders" / "C")
        with patch.object(core, "darwin_user_cache_dir", return_value=cache):
            argv, _, _ = self.command()
        self.assertIn(f'(subpath "{cache}")', argv[2])
        self.assertNotIn(f'(subpath "{self.root / "folders"}")', argv[2])

    def test_the_shared_roots_are_tmp_and_the_per_user_temp_directories(self):
        roots = self.real_shared_write_roots()
        for root in ("/tmp", "/var/folders", tempfile.gettempdir()):
            self.assertIn(os.path.realpath(root), roots)

    def test_a_worktree_writer_may_write_the_shared_git_dir_outside_it(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        git = ["git", "-C", str(repo), "-c", "user.name=ORC test", "-c", "user.email=test@example.invalid"]
        subprocess.run([*git, "commit", "--allow-empty", "-qm", "fixture"], check=True)
        worktree = self.root / "worktree"
        subprocess.run([*git, "worktree", "add", "-qb", "fixture-worktree", str(worktree)], check=True)
        profile = self.command(workspace=worktree)[0][2]
        self.assertIn(f'(subpath "{worktree}")', profile)
        self.assertIn(f'(subpath "{repo / ".git"}")', profile)
        self.assertIn(f'(subpath "{repo / ".git/worktrees/worktree"}")', profile)
        # A repository's own .git is inside the workspace subpath already.
        self.assertNotIn(".git", self.command(workspace=repo)[0][2])

    def test_paths_are_resolved_and_escaped_for_the_profile_language(self):
        odd = self.root / 'say "hi" \\ there'
        odd.mkdir()
        link = self.root / "link"
        link.symlink_to(odd)
        profile = self.command(workspace=link)[0][2]
        self.assertIn('(subpath "' + str(odd).replace("\\", "\\\\").replace('"', '\\"') + '")', profile)
        self.assertNotIn(str(link) + '"', profile)
        self.assertEqual(core.sbpl_string('a"b\\c'), '"a\\"b\\\\c"')
        with self.assertRaisesRegex(ValueError, "control characters"):
            core.sbpl_string("/a\nb")

    def test_writer_allow_is_validated_and_a_route_may_replace_it(self):
        extra = self.root / "agy-state"
        (self.root / "link-to-tmp").symlink_to(self.shared[0])
        profile = self.command(writer_allow=[str(extra), "~/.config/agy-lsp"])[0][2]
        self.assertIn(f'(subpath "{extra}")', profile)
        self.assertIn(f'(subpath "{os.path.realpath(self.home / ".config/agy-lsp")}")', profile)
        self.assertNotIn("antigravity-cli", profile)
        tmp, folders = self.shared
        for bad in ("relative/dir", "", 7, "/", tmp, tmp + "/other-run", "~", "~/", str(self.home.parent),
                    folders, folders + "/T/grading-tree", str(self.root / "link-to-tmp")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "agy.writer_allow"):
                self.command(writer_allow=[bad])
        with self.assertRaisesRegex(ValueError, "agy.writer_allow"):
            self.command(writer_allow="~/.gemini/antigravity-cli")

    def test_a_host_without_sandbox_exec_refuses_writers_but_not_readers(self):
        with self.assertRaisesRegex(ValueError, "agy writers run only inside ORC's macOS sandbox-exec profile"):
            self.command(wrapper=None)
        self.assertEqual(self.command(write=False, wrapper=None)[0][-3:], ["--mode", "plan", "--sandbox"])

    def test_yolo_writers_are_not_wrapped(self):
        for wrapper in (WRAPPER, None):
            with self.subTest(wrapper=wrapper):
                argv, env, metadata = self.command(config={"execution_mode": "yolo"}, wrapper=wrapper)
                self.assertEqual(argv[0], "agy")
                self.assertIn("--dangerously-skip-permissions", argv)
                self.assertNotIn("run_tmpdir", metadata)

    def test_the_explicit_skip_permissions_lane_is_still_wrapped(self):
        argv, _, _ = self.command(dangerously_skip_permissions=True)
        self.assertEqual(argv[0], WRAPPER)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_sandbox_exec_is_only_found_on_macos(self):
        with patch.object(core.sys, "platform", "linux"):
            self.assertIsNone(core.sandbox_exec())

    def test_dispatch_creates_the_run_temp_dir_private_marked_and_removes_it(self):
        seen = self.root / "seen.json"
        profile_copy = self.root / "profile.sb"
        wrapper = self.root / "sandbox-exec"
        wrapper.write_text(f"#!{sys.executable}\nimport os, sys\nassert sys.argv[1] == '-p'\n"
                           f"open({str(profile_copy)!r}, 'w').write(sys.argv[2])\nos.execvp(sys.argv[3], sys.argv[3:])\n")
        wrapper.chmod(0o755)
        fake = self.root / "agy"
        fake.write_text(f"#!{sys.executable}\nimport json, os, stat\nd = os.environ['TMP']\n"
                        f"open({str(seen)!r}, 'w').write(json.dumps({{'dir': d, 'tmpdir': os.environ['TMPDIR'], "
                        "'isdir': os.path.isdir(d), 'mode': stat.S_IMODE(os.stat(d).st_mode), "
                        "'marked': os.path.exists(os.path.join(d, '.orc-owner'))}))\n"
                        f"print(json.dumps({{'status': 'SUCCESS', 'response': '{HANDOFF}'}}))\n")
        fake.chmod(0o755)
        runs = self.root / "runs"
        runs.mkdir()
        original = core.run_tmpdir

        def run_tmpdir(env):
            directory = original(env)
            moved = str(runs / Path(directory).name)
            env.update(TMPDIR=moved + "/", TMP=moved, TEMP=moved)
            return moved
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "agy": {"command": str(fake)}})
        env = {"ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0",
               "FUSION_DECISIONS_MODE": "off", "FUSION_TMP_SWEEP": "0"}
        out = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(core, "load_config", return_value=(config, None)), \
                patch.object(core, "sandbox_exec", return_value=str(wrapper)), \
                patch.object(core, "run_tmpdir", run_tmpdir):
            code = core.main(["--workspace", str(self.workspace), "--json", "delegate", "--agent", "agy",
                              "--role", "implement", "edit it"])
        self.assertEqual(code, 0, out.getvalue())
        record = json.loads(seen.read_text())
        self.assertEqual(record["tmpdir"], record["dir"] + "/")
        self.assertTrue(record["isdir"] and record["marked"])
        self.assertEqual(record["mode"], stat.S_IRWXU)
        self.assertFalse(Path(record["dir"]).exists())
        self.assertIn(f'(subpath "{record["dir"]}")', profile_copy.read_text())

    def test_dispatch_reports_a_missing_agy_behind_the_wrapper(self):
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"},
                                                 "agy": {"command": str(self.root / "no-such-agy")}})
        env = {"ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0",
               "FUSION_DECISIONS_MODE": "off"}
        task = core.make_task(self.workspace, "agy", "edit it", "implement", [], [], None, False, True)
        with patch.dict(os.environ, env), patch.object(core, "sandbox_exec", return_value=shutil.which("true")):
            result = core.dispatch(config, task, core.RunStore(self.workspace))
        self.assertEqual(result["verdict_reason"], "missing_binary")
        self.assertIn("no-such-agy", result["summary"])

    @unittest.skipUnless(nested_sandbox_exec(), "sandbox-exec cannot apply a profile here")
    def test_real_profile_writes_workspace_and_run_temp_only(self):
        """The generated profile under the real sandbox-exec, with Python standing in for agy."""
        script = '''
import pathlib, sys
for target in sys.argv[1:]:
    try:
        pathlib.Path(target, "written-by-agy-writer").write_text("x")
        print("WRITTEN", target)
    except PermissionError:
        print("DENIED", target)
'''
        slash_tmp = Path("/tmp").resolve()
        shared = Path(tempfile.gettempdir()).resolve()
        with contextlib.ExitStack() as stack:
            run_tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="orc-run-", dir=slash_tmp))
            other_run = stack.enter_context(tempfile.TemporaryDirectory(prefix="orc-agy-other-run-", dir=slash_tmp))
            grading = stack.enter_context(tempfile.TemporaryDirectory(prefix="grading-tree-probe-", dir=shared))
            home = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".orc-agy-test-", dir=Path.home())))
            workspace, sibling = home / "ws", home / "sibling"
            workspace.mkdir()
            sibling.mkdir()
            profile = core.agy_writer_profile([str(workspace), run_tmp])
            denied = [str(sibling), str(slash_tmp), other_run, grading, str(home)]
            result = subprocess.run([WRAPPER, "-p", profile, sys.executable, "-c", script, str(workspace), run_tmp,
                                     *denied], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(),
                             ["WRITTEN " + str(workspace), "WRITTEN " + run_tmp, *("DENIED " + path for path in denied)])
            for path in denied:
                self.assertFalse(Path(path, "written-by-agy-writer").exists(), path)


if __name__ == "__main__":
    unittest.main()
