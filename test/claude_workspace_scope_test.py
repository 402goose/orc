"""A writer whose workspace is under $HOME can write it even when a home-path deny covers $HOME,
and nothing beside the workspace becomes writable."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core

# How Claude Code 2.1.291's matcher treated deny rules when probed with real Write calls: `*` and `?`
# also cross `/`, classes are literal sets, matching ignores case, and a rule matching a directory
# covers everything under it.
MATCHER = r'''
import os, re
def _regex(pattern):
    out, i = "", 0
    while i < len(pattern):
        char = pattern[i]
        if char == "[":
            end = pattern.index("]", i + 2 if pattern[i + 1] == "]" else i + 1)
            out += "[" + pattern[i + 1:end].replace("\\", "\\\\") + "]"
            i = end + 1
            continue
        out += ".*" if char == "*" else "." if char == "?" else re.escape(char)
        i += 1
    return re.compile(out, re.IGNORECASE | re.DOTALL)
def denied(rules, path, home):
    path = os.path.normpath(path)
    candidates = [path] + [path[:i] for i in range(len(path)) if path[i] == "/" and i]
    for rule in rules:
        if not rule.startswith(("Edit(", "Write(")):
            continue
        body = rule[rule.index("(") + 1:-1]
        if body.startswith("//"):
            pattern = body[1:]
        elif body.startswith("~/"):
            pattern = home.rstrip("/") + "/" + body[2:]
        else:
            continue
        if pattern.endswith("/**"):
            pattern = pattern[:-3]
        regex = _regex(pattern)
        if any(regex.fullmatch(candidate) for candidate in candidates):
            return True
    return False
'''
namespace = {}
exec(MATCHER, namespace)
denied = namespace["denied"]

FAKE_CLAUDE = r'''#!{python}
import json, os, sys
from pathlib import Path
''' + MATCHER + r'''
argv = sys.argv
rules = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())["permissions"]["deny"]
target = {target!r}
use = {{"type": "assistant", "message": {{"role": "assistant", "content": [
    {{"type": "tool_use", "id": "toolu_w", "name": "Write", "input": {{"file_path": target, "content": "# report"}}}}]}}}}
events = [{{"type": "system", "subtype": "init", "session_id": "s", "permissionMode": "bypassPermissions"}}, use]
if denied(rules, target, os.environ.get("HOME", "/nonexistent")):
    events.append({{"type": "user", "message": {{"role": "user", "content": [{{"type": "tool_result", "tool_use_id": "toolu_w",
        "is_error": True, "content": "File is in a directory that is denied by your permission settings."}}]}}}})
    denials = [{{"tool_name": "Write", "tool_use_id": "toolu_w", "tool_input": {{"file_path": target}}}}]
    text = "STATUS: blocked\nSUMMARY: the report could not be written\nCHANGED: none\nTESTS: none\nBLOCKERS: write denied"
else:
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text("# report\n")
    events.append({{"type": "user", "message": {{"role": "user", "content": [{{"type": "tool_result", "tool_use_id": "toolu_w",
        "content": "File created"}}]}}}})
    denials, text = [], "STATUS: success\nSUMMARY: wrote the report\nCHANGED: out/report.md\nTESTS: none\nBLOCKERS: none"
events.append({{"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "result": text,
                "permission_denials": denials}})
print("\n".join(json.dumps(e) for e in events))
'''


class ClaudeWorkspaceScopeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.workspace = self.home / "Projects" / "tenet-work"
        for path in (self.workspace / "drafts", self.home / "Projects" / "sibling", self.home / ".ssh"):
            path.mkdir(parents=True)
        (self.home / ".profile").write_text("")
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.settings = self.config_dir / "worker-settings.json"
        self.home_rule = f"Edit(/{self.home}/**)"
        self.write_settings([self.home_rule])
        env = patch.dict(os.environ, {"HOME": str(self.home), "ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0",
                                      "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        version = patch.object(core, "claude_version", lambda command: "2.1.291")
        version.start()
        self.addCleanup(version.stop)

    def write_settings(self, extra):
        self.settings.write_text(json.dumps({"sandbox": {"enabled": True}, "permissions": {
            "additionalDirectories": ["/tmp/scratch"],
            "deny": ["Read(~/.ssh/**)", "Edit(~/.ssh/**)", "Edit(//tmp/*.sh)", *extra]}}))

    def scoped(self, workspace, write=True, narrow=True):
        args = ["--settings", str(self.settings), "--append-system-prompt", "sandboxed"]
        task = core.make_task(Path(workspace), "claude", "Write the report.", "implementation", [], [], None, False, write,
                              settings_overrides={"launcher_args": args, "narrow_home_deny": narrow, "user_hooks": True})
        argv, _, _ = core.agent_command(core.DEFAULTS, task, None)
        return argv[argv.index("--settings") + 1], argv

    def deny(self, workspace=None):
        path, _ = self.scoped(workspace or self.workspace)
        return json.loads(Path(path).read_text())["permissions"]["deny"]

    def test_the_workspace_subtree_stays_writable(self):
        rules = self.deny()
        for inside in ("drafts/RESEARCH.md", "new.md", "deep/new/dir/x.md", ".github/workflows/ci.yml"):
            self.assertFalse(denied(rules, str(self.workspace / inside), str(self.home)), inside)

    def test_everything_beside_the_workspace_stays_denied_including_new_names(self):
        rules = self.deny()
        beside = [
            "../sibling/x.md", "../newproj/x.md", "../tenet-workx/x.md", "../tenet-work-old/x.md", "../tenet/x.md",
            "../t", "../.hidden", "../!x", "../../.profile", "../../.zshrc", "../../.bash_profile", "../../newfile",
            "../../newdir/x.md", "../../Projectsx/a.md", "../../Project", "../../.ssh/config", "../../!bang",
            "../../été.md", "../../Pro_jects/x",
        ]
        for name in beside:
            self.assertTrue(denied(rules, os.path.normpath(self.workspace / name), str(self.home)), name)

    def test_the_workspace_claude_directory_stays_denied(self):
        rules = self.deny()
        for name in (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/pre.sh"):
            self.assertTrue(denied(rules, str(self.workspace / name), str(self.home)), name)

    def test_the_narrowed_copy_keeps_everything_else(self):
        path, argv = self.scoped(self.workspace)
        self.assertNotEqual(path, str(self.settings))
        self.assertEqual(Path(path).parent, self.config_dir)
        self.assertIn("sandboxed", argv)
        value = json.loads(Path(path).read_text())
        self.assertEqual(value["permissions"]["deny"][:3], ["Read(~/.ssh/**)", "Edit(~/.ssh/**)", "Edit(//tmp/*.sh)"])
        self.assertNotIn(self.home_rule, value["permissions"]["deny"])
        self.assertEqual(value["sandbox"], {"enabled": True})
        self.assertEqual(value["permissions"]["additionalDirectories"], ["/tmp/scratch"])
        self.assertEqual(self.scoped(self.workspace)[0], path)

    def test_readers_and_workspaces_outside_the_deny_keep_the_settings_file(self):
        self.assertEqual(self.scoped(self.workspace, write=False)[0], str(self.settings))
        elsewhere = self.root / "worktree"
        elsewhere.mkdir()
        self.assertEqual(self.scoped(elsewhere)[0], str(self.settings))

    def test_it_fails_closed_when_the_workspace_is_the_rule_base(self):
        self.assertEqual(self.scoped(self.home)[0], str(self.settings))
        self.write_settings(["Edit(~/**)"])
        self.assertEqual(self.scoped(self.home)[0], str(self.settings))
        rules = json.loads(self.settings.read_text())["permissions"]["deny"]
        self.assertTrue(denied(rules, str(self.home / ".ssh" / "config"), str(self.home)))

    def test_it_fails_closed_on_a_glob_space_or_non_ascii_character_in_the_path(self):
        for name in ("Pro*jects", "My Projects", "\u0130stanbul"):
            with self.subTest(name=name):
                odd = self.home / name / "ws"
                odd.mkdir(parents=True)
                self.assertEqual(self.scoped(odd)[0], str(self.settings))

    def test_it_fails_closed_past_the_rule_cap(self):
        self.write_settings(["Edit(//**)"])
        with patch.object(core, "CLAUDE_MAX_SCOPED_RULES", 10):
            self.assertEqual(self.scoped(self.workspace)[0], str(self.settings))
        with patch.object(core, "CLAUDE_MAX_SCOPED_RULES", 10_000):
            rules = self.deny()
        self.assertFalse(denied(rules, str(self.workspace / "new.md"), str(self.home)))
        self.assertTrue(denied(rules, "/etc/hosts", str(self.home)))
        self.assertTrue(denied(rules, str(self.root / "elsewhere.md"), str(self.home)))

    def test_narrowing_is_off_by_default(self):
        self.assertFalse(core.DEFAULTS["claude"]["narrow_home_deny"])
        self.assertEqual(self.scoped(self.workspace, narrow=False)[0], str(self.settings))

    def test_an_untested_claude_version_does_not_narrow(self):
        with patch.object(core, "claude_version", lambda command: "2.1.282"):
            self.assertEqual(self.scoped(self.workspace)[0], str(self.settings))
        with patch.object(core, "claude_version", lambda command: None):
            self.assertEqual(self.scoped(self.workspace)[0], str(self.settings))

    def dispatch(self, **claude):
        target = self.workspace / "out" / "report.md"
        started = self.root / "worker-started"
        worker = self.root / "claude-fixture"
        worker.write_text(FAKE_CLAUDE.format(python=sys.executable, target=str(target)).replace(
            "argv = sys.argv\n", f"argv = sys.argv\nPath({str(started)!r}).write_text('started')\n", 1))
        worker.chmod(0o755)
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {
            "command": str(worker), "permission_mode": "bypassPermissions",
            "launcher_args": ["--settings", str(self.settings)], **claude}})
        task = core.make_task(self.workspace, "claude", "Write out/report.md.", "implementation", [], [], None, False, True)
        return core.dispatch(config, task, core.RunStore(self.workspace)), target, started

    def test_a_writer_under_a_home_deny_is_refused_before_it_starts_by_default(self):
        value, target, started = self.dispatch()
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["artifacts"], {"run_dir": None})
        self.assertEqual(value["blockers"], [f"workspace deny: workspace {self.workspace} is under deny rule {self.home_rule}; "
                                             "run the writer in a worktree outside $HOME or set claude.narrow_home_deny"])
        self.assertFalse(started.exists())
        self.assertFalse(target.exists())

    def test_an_untested_version_is_refused_before_it_starts(self):
        with patch.object(core, "claude_version", lambda command: "2.1.282"):
            value, _, started = self.dispatch(narrow_home_deny=True)
        self.assertEqual(value["status"], "error")
        self.assertIn("Claude Code 2.1.282 is not in claude.narrow_home_deny_versions ['2.1.291']", value["blockers"][0])
        self.assertFalse(started.exists())

    def test_a_refused_narrowing_is_refused_before_it_starts(self):
        self.write_settings(["Edit(~/**)"])
        (self.home / "Pro*jects" / "ws").mkdir(parents=True)
        self.workspace = self.home / "Pro*jects" / "ws"
        value, _, started = self.dispatch(narrow_home_deny=True)
        self.assertIn("could not narrow it safely", value["blockers"][0])
        self.assertFalse(started.exists())

    def test_a_writer_outside_the_deny_is_not_refused(self):
        self.workspace = self.root / "worktree"
        self.workspace.mkdir()
        value, target, started = self.dispatch()
        self.assertEqual(value["status"], "success")
        self.assertTrue(started.exists())

    def test_a_writer_under_home_writes_its_required_file_when_narrowing_is_on(self):
        value, target, started = self.dispatch(narrow_home_deny=True)
        self.assertEqual((value["status"], value["denied"]), ("success", []))
        self.assertEqual(core.classify_verdict(value)["verdict"], "ok")
        self.assertEqual(target.read_text(), "# report\n")


if __name__ == "__main__":
    unittest.main()
