"""Explicit optional probe: python3 -B test/claude_native_probe.py.

Not part of *_test.py discovery. Requires installed Claude plus the macOS
network sandbox; tests local argument validation only, never model inference.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core


class ClaudeNativeProbe(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("claude") and shutil.which("sandbox-exec"),
                         "requires native Claude and macOS network sandbox")
    def test_native_claude_recognizes_prompt_before_local_validation_gate(self):
        # No credentials are inherited, --bare disables hooks/keychain/discovery,
        # config is isolated, and the OS denies networking as an extra backstop.
        # Invalid stream-json verbosity stops after prompt parsing, before model
        # initialization. Never turn this into a live inference check.
        with tempfile.TemporaryDirectory(prefix="orc-claude-argv-") as directory:
            task = core.make_task(
                Path(directory), "claude", "Review fixture; preserve $literal and `text`.\nDo not edit.",
                "review", [], [], None, False, False,
                settings_overrides={"command": shutil.which("claude"), "allowed_tools": ["Read", "Glob"],
                    "launcher_args": ["--bare", "--strict-mcp-config", "--setting-sources", ""]},
            )
            argv, _, _ = core.agent_command(core.DEFAULTS, task, None)
            argv[argv.index("--output-format") + 1] = "stream-json"
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CLAUDE_CONFIG_DIR": directory,
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"}
            prefix = [shutil.which("sandbox-exec"), "-p", "(version 1) (allow default) (deny network*)"]
            for label, command, expected in (
                ("regression", [arg for arg in argv if arg != "--"], "Input must be provided"),
                ("fixed", argv, "--output-format=stream-json requires --verbose"),
            ):
                with self.subTest(label=label):
                    result = subprocess.run([*prefix, *command], cwd=directory, env=env, input="",
                                            capture_output=True, text=True, timeout=15)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
