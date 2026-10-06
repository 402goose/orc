"""A writer whose workspace is under $HOME can write it even when a home-path deny covers $HOME."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core

FAKE_CLAUDE = r'''#!{python}
import json, os, sys
from pathlib import Path
argv = sys.argv
settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
target = Path({target!r})

def covers(rule):
    body = rule[rule.index("(") + 1:-1]
    path = Path(body[1:]) if body.startswith("//") else Path(os.environ.get("HOME", "/nonexistent")) / body[2:] if body.startswith("~/") else None
    if path is None:
        return False
    if body.endswith("/**"):
        return target.is_relative_to(Path(str(path)[:-3]))
    return target == path

use = {{"type": "assistant", "message": {{"role": "assistant", "content": [
    {{"type": "tool_use", "id": "toolu_w", "name": "Write", "input": {{"file_path": str(target), "content": "# report"}}}}]}}}}
events = [{{"type": "system", "subtype": "init", "session_id": "s", "permissionMode": "bypassPermissions"}}, use]
if any(covers(rule) for rule in settings["permissions"]["deny"] if rule.startswith("Edit(")):
    events.append({{"type": "user", "message": {{"role": "user", "content": [{{"type": "tool_result", "tool_use_id": "toolu_w", "is_error": True,
        "content": "File is in a directory that is denied by your permission settings."}}]}}}})
    denials, text = [{{"tool_name": "Write", "tool_use_id": "toolu_w", "tool_input": {{"file_path": str(target)}}}}], \
        "STATUS: blocked\nSUMMARY: the report could not be written\nCHANGED: none\nTESTS: none\nBLOCKERS: write denied"
else:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# report\n")
    events.append({{"type": "user", "message": {{"role": "user", "content": [{{"type": "tool_result", "tool_use_id": "toolu_w",
        "content": "File created"}}]}}}})
    denials, text = [], "STATUS: success\nSUMMARY: wrote the report\nCHANGED: out/report.md\nTESTS: none\nBLOCKERS: none"
events.append({{"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "result": text,
                "permission_denials": denials}})
print("\n".join(json.dumps(e) for e in events))
'''


def deny_covers(rules, path):
    for rule in rules:
        body = rule[rule.index("(") + 1:-1]
        if not body.startswith("//"):
            continue
        if body.endswith("/**") and path.is_relative_to(Path(body[1:-3])):
            return True
        if Path(body[1:]) == path:
            return True
    return False


class ClaudeWorkspaceScopeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.workspace = self.home / "projects" / "ws"
        for path in (self.workspace, self.home / "projects" / "sibling", self.home / ".ssh", self.home / "notes"):
            path.mkdir(parents=True)
        (self.home / ".profile").write_text("")
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.settings = self.config_dir / "worker-settings.json"
        self.settings.write_text(json.dumps({"sandbox": {"enabled": True}, "permissions": {
            "additionalDirectories": ["/tmp/scratch"],
            "deny": ["Read(~/.ssh/**)", "Edit(~/.ssh/**)", "Edit(//tmp/*.sh)", f"Edit(/{self.home}/**)"]}}))
        env = patch.dict(os.environ, {"HOME": str(self.home), "ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0",
                                      "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)

    def scoped(self, workspace, write=True):
        args = ["--settings", str(self.settings), "--append-system-prompt", "sandboxed"]
        task = core.make_task(workspace, "claude", "Write the report.", "implementation", [], [], None, False, write,
                              settings_overrides={"launcher_args": args})
        argv, _, _ = core.agent_command(core.DEFAULTS, task, None)
        return argv[argv.index("--settings") + 1], argv

    def test_a_home_deny_is_narrowed_to_everything_beside_the_workspace(self):
        path, argv = self.scoped(self.workspace)
        self.assertNotEqual(path, str(self.settings))
        self.assertEqual(Path(path).parent, self.config_dir)
        self.assertIn("sandboxed", argv)
        value = json.loads(Path(path).read_text())
        deny = value["permissions"]["deny"]
        self.assertEqual(deny[:3], ["Read(~/.ssh/**)", "Edit(~/.ssh/**)", "Edit(//tmp/*.sh)"])
        self.assertNotIn(f"Edit(/{self.home}/**)", deny)
        self.assertFalse(deny_covers(deny, self.workspace / "out" / "report.md"))
        for outside in (self.home / ".profile", self.home / "notes" / "x.md", self.home / ".ssh" / "config",
                        self.home / "projects" / "sibling" / "x.md"):
            self.assertTrue(deny_covers(deny, outside), outside)
        self.assertEqual({k: v for k, v in value.items() if k != "permissions"}, {"sandbox": {"enabled": True}})
        self.assertEqual(value["permissions"]["additionalDirectories"], ["/tmp/scratch"])
        self.assertEqual(self.scoped(self.workspace)[0], path)

    def test_readers_and_workspaces_outside_the_deny_keep_the_settings_file(self):
        self.assertEqual(self.scoped(self.workspace, write=False)[0], str(self.settings))
        elsewhere = self.root / "worktree"
        elsewhere.mkdir()
        self.assertEqual(self.scoped(elsewhere)[0], str(self.settings))

    def test_a_writer_under_home_writes_its_required_file(self):
        target = self.workspace / "out" / "report.md"
        worker = self.root / "claude-fixture"
        worker.write_text(FAKE_CLAUDE.format(python=sys.executable, target=str(target)))
        worker.chmod(0o755)
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {
            "command": str(worker), "permission_mode": "bypassPermissions",
            "launcher_args": ["--settings", str(self.settings)]}})
        task = core.make_task(self.workspace, "claude", "Write out/report.md.", "implementation", [], [], None, False, True)
        value = core.dispatch(config, task, core.RunStore(self.workspace))
        self.assertEqual((value["status"], value["denied"]), ("success", []))
        self.assertEqual(core.classify_verdict(value)["verdict"], "ok")
        self.assertEqual(target.read_text(), "# report\n")


if __name__ == "__main__":
    unittest.main()
