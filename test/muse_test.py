"""Muse Code (`muse exec --json`) as a Fusion worker (issue #70)."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_mcp
import fusion_policy

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "muse_exec_echo.jsonl"
HANDOFF = "STATUS: success\nSUMMARY: reviewed\nCHANGED: none\nTESTS: none\nBLOCKERS: none"
WRAPPER = "/usr/bin/sandbox-exec"


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


def event(payload_type, payload, session="11111111-2222-4333-8444-555555555555"):
    return {"schema_version": 1, "stream": {"kind": "session", "id": session},
            "payload_type": payload_type, "payload": payload}


def delta(text):
    return event("run.output.delta", {"kind": "run_output_delta", "text": text})


def terminal(kind="completed", text=HANDOFF, reason=None):
    return event(f"run.terminal.{kind}", {"kind": "run_terminal", "terminal": kind, "text": text, "reason": reason})


class MuseParseTest(unittest.TestCase):
    def test_real_echo_fixture_parses_terminal_text_and_session(self):
        session, answer, failure, usage, model, notes = core.parse_muse_output(FIXTURE.read_text())
        self.assertTrue(session)
        self.assertTrue(answer.startswith("echo: Return a compact handoff"))
        # Reminder child tasks fail in the fixture; only the run terminal decides.
        self.assertIsNone(failure)
        self.assertEqual(notes, [])

    def test_usage_model_and_command_evidence_and_unknown_events(self):
        output = stream(
            event("run.lifecycle.started", {"kind": "run_started", "model": "muse-spark-1.3"}),
            {"totally": "unknown"},
            event("tool.result", {"kind": "tool_result", "command": "make test", "exit_code": 2}),
            event("model.usage", {"kind": "model_usage", "usage": {
                "inputTokens": 100, "outputTokens": 40, "reasoningTokens": 10,
                "cacheReadTokens": 30, "cacheWriteTokens": 5, "cost": {"usd": 0.25, "partial": True}}}),
            delta(HANDOFF),
            terminal(),
        )
        session, answer, failure, usage, model, notes = core.parse_muse_output(output)
        self.assertEqual((session, answer, failure, model),
                         ("11111111-2222-4333-8444-555555555555", HANDOFF, None, "muse-spark-1.3"))
        self.assertEqual(usage, {"input_tokens": 100, "output_tokens": 40, "reasoning_output_tokens": 10,
                                 "cache_read_input_tokens": 30, "cache_creation_input_tokens": 5})
        self.assertEqual(notes, ["command exited 2: make test"])

    def test_failed_terminal_and_missing_terminal_are_failures(self):
        _, _, failure, _, _, _ = core.parse_muse_output(stream(terminal("failed", "", "provider exploded")))
        self.assertEqual(failure, "provider exploded")
        _, _, failure, _, _, _ = core.parse_muse_output(stream(delta("half an answer")))
        self.assertIn("without a terminal event", failure)

    def test_non_json_output_is_plain_text(self):
        self.assertEqual(core.parse_muse_output("plain answer\n")[1], "plain answer")


class MuseCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.shared = [str(self.root / "shared-tmp"), str(self.root / "shared-folders")]
        for mock in (patch.dict(os.environ, {"HOME": str(self.home)}),
                     patch.object(core, "darwin_user_cache_dir", return_value=None),
                     patch.object(core, "shared_write_roots", return_value=self.shared)):
            mock.start()
            self.addCleanup(mock.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, write=True, mode="restricted", route=None, session=None, wrapper=WRAPPER, **settings):
        config = core.deep_merge(core.DEFAULTS, {"execution_mode": mode, "muse": settings})
        task = core.make_task(self.root, "muse", "Review", "reviewer", [], [], None, False, write, route=route)
        with patch.object(core, "sandbox_exec", return_value=wrapper):
            result = core.agent_command(config, task, session)
        self.addCleanup(Path(result[2]["prompt_file"]).unlink, missing_ok=True)
        return result

    def test_writer_argv_matches_spec_and_brief_is_a_file(self):
        argv, _, metadata = self.command(model="muse-wonder-full-1.0", reasoning_effort="high")
        self.assertEqual(argv[:2], [WRAPPER, "-p"])
        self.assertEqual(argv[3:6], ["muse", "exec", "--json"])
        for flag, value in (("--model", "muse-wonder-full-1.0"), ("--reasoning-effort", "high"),
                            ("--approval-mode", "never"), ("--contributor-mode", "off"),
                            ("--sandbox-network", "proxy-only"), ("--workspace", str(self.root))):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        # The outer profile replaces Muse's own sandbox, which cannot nest inside it.
        for flag in ("--no-foreign-personal-context", "--no-session-log", "--disable-sandbox"):
            self.assertIn(flag, argv)
        for flag in ("--yolo", "--disable-write", "--disable-shell"):
            self.assertNotIn(flag, argv)
        brief = Path(argv[argv.index("--prompt-file") + 1])
        self.assertIn("Fusion coding harness", brief.read_text())
        self.assertEqual(metadata["model"], "muse-wonder-full-1.0")

    def test_reader_cannot_write_or_shell_out_even_in_yolo_workspace(self):
        for mode in ("restricted", "yolo"):
            for wrapper in (WRAPPER, None):
                for profile in ("", "reviewer"):
                    with self.subTest(mode=mode, wrapper=wrapper, profile=profile):
                        argv, _, _ = self.command(write=False, mode=mode, wrapper=wrapper,
                                                  permission_profile=profile, inner_sandbox=False)
                        expected = ["muse", "exec", "--json", "--approval-mode", "never",
                                    "--sandbox-network", "proxy-only", "--contributor-mode", "off",
                                    "--no-foreign-personal-context", "--no-session-log", "--disable-write"]
                        expected += ["--permission-profile", profile] if profile else ["--disable-shell"]
                        expected += ["--workspace", str(self.root), "--prompt-file", argv[-1]]
                        self.assertEqual(argv, expected)

    def test_yolo_writer_is_the_only_yolo(self):
        for wrapper in (WRAPPER, None):
            with self.subTest(wrapper=wrapper):
                argv, _, _ = self.command(mode="yolo", wrapper=wrapper, inner_sandbox=False,
                                          model="muse-wonder-1.0", writer_allow="ignored")
                self.assertEqual(argv[:3], ["muse", "exec", "--json"])
                self.assertIn("--yolo", argv)
                self.assertNotIn("--approval-mode", argv)
                self.assertNotIn("--disable-sandbox", argv)
                self.assertIn("--contributor-mode", argv)

    def test_inner_sandbox_can_only_be_disabled_under_the_wrapper(self):
        self.assertIs(core.DEFAULTS["muse"]["inner_sandbox"], False)
        for inner in (True, False):
            with self.subTest(inner=inner):
                argv, _, _ = self.command(inner_sandbox=inner)
                self.assertEqual(argv[:2], [WRAPPER, "-p"])
                self.assertEqual("--disable-sandbox" in argv[3:], not inner)
                self.assertIn("--sandbox-network", argv)
        with self.assertRaisesRegex(ValueError, "muse writers run only inside ORC's macOS sandbox-exec profile"):
            self.command(wrapper=None, inner_sandbox=False)

    def test_no_sandbox_exec_refuses_writers_before_creating_a_prompt(self):
        with patch.object(core.tempfile, "NamedTemporaryFile", side_effect=AssertionError("must refuse first")):
            with self.assertRaisesRegex(ValueError, "muse writers run only inside ORC's macOS sandbox-exec profile"):
                self.command(wrapper=None)

    def test_profile_has_exact_writable_set_and_no_shared_roots(self):
        cache = str(self.root / "shared-folders" / "C")
        git = str(self.root.parent / (self.root.name + "-external-git"))
        with patch.object(core, "darwin_user_cache_dir", return_value=cache), \
                patch.object(core, "git_metadata_dirs", return_value={git, str(self.root / ".git")}):
            argv, env, metadata = self.command()
        directory = metadata["run_tmpdir"]
        writable = [str(self.root), git, directory, str(Path(directory) / "xdg"),
                    str(self.home / ".local/share/muse"), cache]
        expected = ["(version 1)", "(allow default)", "(deny file-write*)", "(allow file-write*",
                    *(f'    (subpath "{path}")' for path in writable), '    (literal "/dev/null")',
                    '    (literal "/dev/ptmx")', '    (literal "/dev/dtracehelper")',
                    '    (regex #"^/dev/tty[^/]*$"))']
        self.assertEqual(argv[2].splitlines(), expected)
        for shared in ("/tmp", "/private/tmp", "/var/folders", os.path.realpath(tempfile.gettempdir()),
                       str(self.home), str(self.home / ".config"), *self.shared):
            self.assertNotIn(f'(subpath "{shared}")', argv[2])
            self.assertNotIn(f'(subpath "{shared}/")', argv[2])
        self.assertEqual((env["TMPDIR"], env["TMP"], env["TEMP"]), (directory + "/", directory, directory))
        for name, leaf in core.READER_CACHE_DIRS.items():
            self.assertEqual(env[name], f"{directory}/{leaf}")
        self.assertEqual(env["XDG_CONFIG_HOME"], str(Path(directory) / "xdg"))
        self.assertFalse(Path(directory).exists())

    def test_worktree_git_metadata_is_writable_outside_the_workspace(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        git = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
        subprocess.run([*git, "commit", "--allow-empty", "-qm", "fixture"], check=True)
        worktree = self.root / "worktree"
        subprocess.run([*git, "worktree", "add", "-qb", "fixture-worktree", str(worktree)], check=True)
        task = core.make_task(worktree, "muse", "edit", "implement", [], [], None, False, True)
        with patch.object(core, "sandbox_exec", return_value=WRAPPER):
            argv, _, metadata = core.agent_command(core.DEFAULTS, task, None)
        self.addCleanup(Path(metadata["prompt_file"]).unlink, missing_ok=True)
        for path in (worktree, repo / ".git", repo / ".git/worktrees/worktree"):
            self.assertIn(f'(subpath "{path}")', argv[2])
        self.assertNotIn(f'(subpath "{repo}")', argv[2])

    def test_writer_allow_uses_the_agy_validation_rules(self):
        extra = self.root / 'state "quoted"'
        link = self.root / "state-link"
        link.symlink_to(extra)
        profile = self.command(writer_allow=[str(link), "~/.config/muse/auth.json"])[0][2]
        self.assertIn(f'(subpath {core.sbpl_string(str(extra))})', profile)
        self.assertIn(f'(subpath "{self.home / ".config/muse/auth.json"}")', profile)
        self.assertIn(f'(subpath "{self.home / ".local/share/muse"}")', profile)
        self.assertNotIn(str(link), profile)
        (self.root / "link-to-tmp").symlink_to(self.shared[0])
        for bad in ("relative/dir", "", 7, "/", "~", "~/", str(self.home.parent),
                    *self.shared, self.shared[0] + "/other-run", self.shared[1] + "/T/other-run",
                    str(self.root / "link-to-tmp")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "muse.writer_allow"):
                self.command(writer_allow=[bad])
        for bad in ("~/.config/muse", None, {}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "muse.writer_allow"):
                self.command(writer_allow=bad)

    def test_worker_mcp_never_appears_in_argv_or_metadata(self):
        muse_home = self.root / "xdg" / "muse"
        muse_home.mkdir(parents=True)
        (muse_home / "settings.json").write_text(json.dumps(
            {"schema_version": 1, "mcpServers": {"alpha": {}, "beta": {}}}))
        server = {"transport": "stdio", "command": "fixture", "env": {"TOKEN": "worker-secret"}}
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}):
            for write in (False, True):
                for mode in ("restricted", "yolo"):
                    for servers in ({}, {"worker": server}):
                        with self.subTest(write=write, mode=mode, servers=bool(servers)):
                            argv, env, metadata = self.command(write=write, mode=mode,
                                                              model="muse-spark-1.3", mcp_servers=servers)
                            self.assertFalse(any(item.startswith("mcpServers") for item in argv))
                            self.assertNotIn("-c", argv)
                            self.assertNotIn("worker-secret", json.dumps(argv) + json.dumps(metadata))
                            self.assertEqual(env["XDG_CONFIG_HOME"], str(Path(metadata["run_tmpdir"]) / "xdg"))
                            self.assertFalse(Path(metadata["run_tmpdir"]).exists())

    def test_worker_mcp_validation_is_preserved(self):
        for servers in ("invalid", {"bad name": {}}, {"worker": "invalid"}):
            with self.subTest(servers=servers), self.assertRaisesRegex(ValueError, "muse.mcp_servers"):
                self.command(mcp_servers=servers)

    def test_api_key_comes_from_stdin_never_argv(self):
        with patch.dict(os.environ, {"MUSE_TEST_KEY": "sk-do-not-print"}):
            argv, _, metadata = self.command(model="muse-spark-1.3", api_key_env="MUSE_TEST_KEY")
        self.assertIn("--api-key-stdin", argv)
        self.assertNotIn("sk-do-not-print", json.dumps(argv) + json.dumps(metadata))
        self.assertEqual(metadata["api_key_env"], "MUSE_TEST_KEY")
        with self.assertRaisesRegex(ValueError, "api_key_env"):
            self.command(model="muse-spark-1.3", api_key_env="MUSE_MISSING_KEY")

    def test_session_resumes_by_id_and_logging_is_opt_in(self):
        session = "11111111-2222-4333-8444-555555555555"
        argv, _, _ = self.command(model="muse-spark-1.3", session=session, session_log=True)
        self.assertEqual(argv[argv.index("--session-id") + 1], session)
        self.assertNotIn("--no-session-log", argv)

    def test_default_routes_are_the_catalog_lanes(self):
        routes = core.DEFAULTS["routes"]
        expected = {"muse-wonder-full-high": "muse-wonder-full-1.0",
                    "muse-wonder-high": "muse-wonder-1.0", "muse-spark-high": "muse-spark-1.3"}
        for name, model in expected.items():
            route = routes[name]
            self.assertEqual((route["agent"], route["model"], route["reasoning_effort"]), ("muse", model, "high"))
            self.assertEqual((route["upstream"], route["creator"], route["billing"]), ("meta", "meta", "subscription"))
            self.assertNotIn("cost_tier", route)
        task = core.make_task(self.root, "muse", "Review", "reviewer", [], [], None, False, False,
                              route="muse-spark-high")
        argv, _, _ = core.agent_command(core.DEFAULTS, task, None)
        self.assertEqual(argv[argv.index("--model") + 1], "muse-spark-1.3")


class MuseConfigTest(unittest.TestCase):
    def test_config_mirrors_other_tools_and_shares_login_with_only_worker_servers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            for explicit_xdg in (False, True):
                config = root / "config" if explicit_xdg else home / ".config"
                muse = config / "muse"
                muse.mkdir(parents=True)
                (config / "git").mkdir()
                (config / "gh").mkdir()
                (config / "tool.conf").write_text("tool settings")
                (config / "linked").symlink_to(config / "git")
                original = {"schema_version": 1, "permissionProfiles": {"reader": {}},
                            "mcpServers": {"operator": {"env": {"TOKEN": "operator-secret"}}}}
                (muse / "settings.json").write_text(json.dumps(original))
                (muse / "auth.json").write_text("login-secret")
                (muse / "trust.json").write_text("trust settings")
                env = {"HOME": str(home)}
                if explicit_xdg:
                    env["XDG_CONFIG_HOME"] = str(config)
                for servers in ({}, {"worker": {"command": "fixture", "env": {"TOKEN": "worker-secret"}}}):
                    with self.subTest(explicit_xdg=explicit_xdg, servers=bool(servers)):
                        run = Path(tempfile.mkdtemp(dir=root))
                        core.muse_worker_config(str(run), servers, env)
                        xdg = run / "xdg"
                        self.assertEqual({p.name for p in xdg.iterdir()}, {p.name for p in config.iterdir()})
                        for name in ("git", "gh", "tool.conf", "linked"):
                            self.assertTrue((xdg / name).is_symlink())
                            self.assertEqual((xdg / name).readlink(), config / name)
                        worker = xdg / "muse"
                        self.assertFalse(worker.is_symlink())
                        self.assertEqual(worker.stat().st_mode & 0o777, 0o700)
                        for name in ("auth.json", "trust.json"):
                            self.assertTrue((worker / name).is_symlink())
                            self.assertEqual((worker / name).readlink(), muse / name)
                        (worker / "auth.json").write_text("refreshed-login")
                        self.assertEqual((muse / "auth.json").read_text(), "refreshed-login")
                        self.assertFalse((worker / "settings.json").is_symlink())
                        self.assertEqual(json.loads((worker / "settings.json").read_text()),
                                         {**original, "mcpServers": servers})
                        self.assertEqual(json.loads((muse / "settings.json").read_text()), original)
                        self.assertNotIn("operator-secret", (worker / "settings.json").read_text())

    def test_config_without_existing_settings_or_login_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = root / "run"
            run.mkdir()
            core.muse_worker_config(str(run), {}, {"HOME": str(root / "home")})
            worker = run / "xdg" / "muse"
            self.assertEqual({p.name for p in worker.iterdir()}, {"settings.json"})
            self.assertEqual(json.loads((worker / "settings.json").read_text()), {"mcpServers": {}})


class MuseLaneOfferTest(unittest.TestCase):
    def test_muse_lanes_offer_meta_upstream_and_subscription_billing(self):
        import fusion_lanes as lanes
        offers = {item["lane"]: item for item in lanes.lane_offers(core.DEFAULTS)}
        for lane, model in (("muse-wonder-full-high", "muse-wonder-full-1.0"),
                            ("muse-wonder-high", "muse-wonder-1.0"), ("muse-spark-high", "muse-spark-1.3")):
            offer = offers[lane]
            self.assertEqual((offer["schema"], offer["agent"], offer["model"]), ("fusion.lane_offer.v1", "muse", model))
            self.assertEqual((offer["creator"], offer["upstream"], offer["billing"]), ("meta", "meta", "subscription"))
            self.assertEqual(offer["reasoning_effort"], "high")
            self.assertIsNone(offer["cost_tier"])
            self.assertIn("tools", offer["features"])
        self.assertEqual(offers["muse"]["agent"], "muse")


class MuseRoutingTest(unittest.TestCase):
    def test_not_auto_routed_until_the_operator_opts_in(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = core.make_task(root, "auto", "fixture", "review", [], [], None, False, False)
            env = {"ORC_HOME": str(root / "orc"), "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"}
            # Minimal config (no orc selector routes to probe), as in the OpenCode routing test.
            config = {"sidekick": "codex", "decisions": {"mode": "off", "priors": False},
                      "routes": {name: dict(core.DEFAULTS["routes"][name])
                                 for name in ("muse-wonder-full-high", "muse-spark-high")}}
            rejected = {}
            with patch.dict(os.environ, env), patch.object(core, "executable", return_value=True):
                keys = {c["key"] for c in fusion_policy.route_candidates(config, task, core.RunStore(root), rejected=rejected)}
            self.assertNotIn("muse-wonder-full-high", keys)
            self.assertIn("not auto-routed", rejected["muse-wonder-full-high"])
            config["decisions"]["auto_routes"] = ["muse-wonder-full-high"]
            with patch.dict(os.environ, env), patch.object(core, "executable", return_value=True):
                keys = {c["key"] for c in fusion_policy.route_candidates(config, task, core.RunStore(root), rejected={})}
            self.assertIn("muse-wonder-full-high", keys)

    def test_lanes_show_upstream_meta_and_no_automatic_routing(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"ORC_HOME": str(Path(d) / "orc")}):
            lanes = {lane["route"] or lane["agent"]: lane for lane in fusion_mcp.orientation(Path(d))["lanes"]}
        lane = lanes["muse-spark-high"]
        self.assertEqual((lane["agent"], lane["model"], lane["upstream"], lane["creator"], lane["billing"]),
                         ("muse", "muse-spark-1.3", "meta", "meta", "subscription"))
        self.assertFalse(lane["automatic"])
        self.assertIn("muse", lanes)


class MuseDispatchTest(unittest.TestCase):
    def test_cli_delegate_accepts_a_muse_route_and_the_muse_agent(self):
        parser = core.build_parser()
        self.assertEqual(parser.parse_args(["delegate", "--agent", "muse", "task"]).agent, "muse")
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}), \
                patch.object(core, "dispatch", return_value={"status": "success", "summary": "ok", "artifacts": {"run_dir": None}}) as dispatch, \
                contextlib.redirect_stdout(io.StringIO()):
            core.main(["--workspace", d, "delegate", "--route", "muse-wonder-high", "--fresh", "task"])
        self.assertEqual(dispatch.call_args.args[1]["agent"], "muse")

    def test_dispatch_records_session_usage_and_public_handoff(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            root = Path(d)
            config_root = root / "config"
            muse = config_root / "muse"
            muse.mkdir(parents=True)
            (muse / "settings.json").write_text(json.dumps({"mcpServers": {
                "operator": {"env": {"TOKEN": "operator-secret"}}}}))
            (muse / "auth.json").write_text("login-secret")
            servers = {"worker": {"command": "fixture", "env": {"TOKEN": "worker-secret"}}}
            worker = root / "muse-fixture"
            wrapper = root / "sandbox-exec-fixture"
            wrapper.write_text(f"#!{sys.executable}\nimport os,sys\n"
                               "assert sys.argv[1] == '-p' and '(deny file-write*)' in sys.argv[2]\n"
                               "os.execvp(sys.argv[3], sys.argv[3:])\n")
            wrapper.chmod(0o755)
            events = [event("run.lifecycle.started", {"kind": "run_started"}),
                      event("model.usage", {"kind": "model_usage", "model": "muse-spark-1.3",
                                            "usage": {"inputTokens": 9, "outputTokens": 4}}),
                      delta(HANDOFF), terminal()]
            worker.write_text(f"#!{sys.executable}\n" + f"""import json,os,sys
from pathlib import Path
assert sys.argv[1:3]==['exec','--json'], sys.argv
assert '--contributor-mode' in sys.argv and sys.argv[sys.argv.index('--contributor-mode')+1]=='off'
if os.environ['FIXTURE_WRITE'] == '0':
    assert '--disable-write' in sys.argv and '--disable-shell' in sys.argv and '--yolo' not in sys.argv
else:
    assert '--disable-write' not in sys.argv
assert ('--yolo' in sys.argv) == (os.environ['FIXTURE_WRITE'] == '1' and os.environ['FIXTURE_MODE'] == 'yolo')
assert '-c' not in sys.argv and not any(arg.startswith('mcpServers') for arg in sys.argv)
xdg = Path(os.environ['XDG_CONFIG_HOME'])
assert xdg.is_dir() and xdg == Path(os.environ['TMPDIR']) / 'xdg'
assert (xdg.parent / '.orc-owner').is_file()
assert xdg.parent.stat().st_mode & 0o777 == {stat.S_IRWXU}
if os.environ['FIXTURE_WRITE'] == '1' and os.environ['FIXTURE_MODE'] == 'restricted':
    assert os.environ['TMP'] == os.environ['TEMP'] == str(xdg.parent)
    for name, leaf in {core.READER_CACHE_DIRS!r}.items():
        assert os.environ[name] == str(xdg.parent / leaf)
assert json.loads((xdg / 'muse' / 'settings.json').read_text()) == {{'mcpServers': {servers!r}}}
assert (xdg / 'muse' / 'auth.json').is_symlink()
brief = open(sys.argv[sys.argv.index('--prompt-file')+1]).read()
assert 'Fusion coding harness' in brief
for event in {events!r}:
    print(json.dumps(event))
""")
            worker.chmod(0o755)
            for write in (False, True):
                for mode in ("restricted", "yolo"):
                    with self.subTest(write=write, mode=mode), patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_root)}), \
                            patch.object(core, "sandbox_exec", return_value=str(wrapper)), \
                            patch.object(core, "darwin_user_cache_dir", return_value=None):
                        config = core.deep_merge(core.DEFAULTS, {"execution_mode": mode,
                                                                "muse": {"command": str(worker), "model": "muse-spark-1.3",
                                                                         "mcp_servers": servers,
                                                                         "env": {"FIXTURE_WRITE": str(int(write)), "FIXTURE_MODE": mode}},
                                                                "decisions": {"mode": "off"}})
                        task = core.make_task(root, "muse", "Review fixture", "review", [], [], None, False, write)
                        store = core.RunStore(root)
                        result = core.dispatch(config, task, store)
                        self.assertEqual(result["status"], "success", result)
                        self.assertEqual((result["summary"], result["model"]), ("reviewed", "muse-spark-1.3"))
                        self.assertEqual(result["usage"]["input_tokens"], 9)
                        self.assertIn("11111111-2222-4333-8444-555555555555", store.sessions().values())
                        self.assertFalse(Path(task["resolved"]["prompt_file"]).exists())
                        self.assertFalse(Path(task["resolved"]["run_tmpdir"]).exists())
                        for artifact in store.root.rglob("*"):
                            if artifact.is_file():
                                for secret in ("operator-secret", "login-secret", "worker-secret"):
                                    self.assertNotIn(secret, artifact.read_text(), str(artifact))

    def test_dispatch_cleans_config_when_worker_cannot_start(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HOME": d, "XDG_CONFIG_HOME": str(Path(d) / "config"),
                                                                      "FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            root = Path(d)
            config = core.deep_merge(core.DEFAULTS, {"muse": {"command": sys.executable}, "decisions": {"mode": "off"}})
            def fail(argv, **kwargs):
                xdg = Path(kwargs["env"]["XDG_CONFIG_HOME"])
                self.assertEqual(json.loads((xdg / "muse" / "settings.json").read_text()), {"mcpServers": {}})
                self.assertTrue((xdg.parent / ".orc-owner").is_file())
                self.assertEqual(xdg.parent.stat().st_mode & 0o777, stat.S_IRWXU)
                raise OSError("fixture could not start")

            for write in (False, True):
                task = core.make_task(root, "muse", "fixture", "review", [], [], None, False, write)
                with self.subTest(write=write), patch("fusion_progress.run_logged", side_effect=fail), \
                        patch.object(core, "sandbox_exec", return_value=sys.executable), \
                        patch.object(core, "darwin_user_cache_dir", return_value=None):
                    result = core.dispatch(config, task, core.RunStore(root))
                self.assertEqual((result["status"], result["exit_code"]), ("error", 126))
                self.assertFalse(Path(task["resolved"]["run_tmpdir"]).exists())
                self.assertFalse(Path(task["resolved"]["prompt_file"]).exists())


if __name__ == "__main__":
    unittest.main()
