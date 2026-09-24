"""Portable Claude prompt-boundary regressions; no native runtime required."""
import argparse
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core


class ClaudeArgvTest(unittest.TestCase):
    def task(self, workspace, **settings):
        return core.make_task(
            Path(workspace), "claude", "Review fixture; preserve $literal and `text`.\nDo not edit.",
            "review", [], [], None, False, False, settings_overrides=settings,
        )

    def parse_fixture(self, argv):
        # Claude documents both options as variadic and its prompt as optional.
        # This parser consumes a missing-separator prompt as another tool name,
        # unlike fixtures that merely inspect argv[-1] or always return success.
        parser = argparse.ArgumentParser()
        parser.add_argument("-p", action="store_true")
        parser.add_argument("--allowedTools", nargs="+")
        parser.add_argument("--mcp-config", nargs="+")
        parser.add_argument("--strict-mcp-config", action="store_true")
        parser.add_argument("--dangerously-skip-permissions", action="store_true")
        for option in ("--output-format", "--permission-mode", "--permission-prompts", "--model", "--max-budget-usd", "--settings", "--resume"):
            parser.add_argument(option)
        parser.add_argument("prompt", nargs="?")
        return parser.parse_args(argv[1:])

    def test_fresh_and_resumed_launches_keep_prompt_separate_from_tools(self):
        tools = ["Read", "Glob", "Grep", "mcp__example__run_status"]
        task = self.task("/tmp/fixture", allowed_tools=tools, max_budget_usd=2,
                         launcher_args=["--mcp-config", "/tmp/fixture-mcp.json", "--strict-mcp-config"])
        for session in (None, "prior-session"):
            with self.subTest(session=session):
                argv, _, _ = core.agent_command(core.DEFAULTS, task, session)
                parsed = self.parse_fixture(argv)
                self.assertEqual(parsed.prompt, core.brief_for(task))
                self.assertEqual(parsed.allowedTools, tools)
                self.assertEqual(parsed.resume, session)
                self.assertEqual(parsed.permission_mode, "plan")
                self.assertEqual(parsed.mcp_config, ["/tmp/fixture-mcp.json"])
        # Demonstrate why the old fixture shape was wrong, with the same parser.
        argv, _, _ = core.agent_command(core.DEFAULTS, task, None)
        without_boundary = [arg for arg in argv if arg != "--"]
        broken = self.parse_fixture(without_boundary)
        self.assertIsNone(broken.prompt)
        self.assertEqual(broken.allowedTools, [*tools, core.brief_for(task)])

    def test_boundary_is_kept_without_allowlist_and_through_orc_passthrough(self):
        for mode in ("restricted", "yolo"):
            for command in ("claude", "orc"):
                for session in (None, "prior-session"):
                    with self.subTest(mode=mode, command=command, session=session):
                        task = self.task("/tmp/fixture", command=command, model="fixture-model", allowed_tools=[])
                        config = core.deep_merge(core.DEFAULTS, {"execution_mode": mode})
                        argv, _, _ = core.agent_command(config, task, session)
                        self.assertEqual(argv[-2:], ["--", core.brief_for(task)])
                        if command == "claude":
                            self.assertEqual(self.parse_fixture(argv).prompt, core.brief_for(task))

if __name__ == "__main__":
    unittest.main()
