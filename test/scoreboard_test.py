"""`fusion scoreboard` counts every settled run, pinned or routed, per role and lane family."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_scoreboard as board
from fusion_decisions import DecisionStore


class ScoreboardTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0",
                                      "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_CONTROL_WORKSPACE", None)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()

    def run_dir(self, run_id, agent="codex", model="gpt-6.1-sol", effort="high", role="implementation", cost=0.4,
                minutes=10, **task):
        directory = self.workspace / ".fusion" / "runs" / run_id
        directory.mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps({
            "schema": core.SCHEMA, "run_id": run_id, "trace_id": run_id, "agent": agent, "role": role, "write": True,
            "task": f"Fix {run_id}", "created_at": core.now_ms(), "resolved": {"model": model, "reasoning_effort": effort},
            **task}))
        (directory / "result.json").write_text(json.dumps({
            "schema": core.SCHEMA, "run_id": run_id, "status": "success", "verdict": "ok", "agent": agent, "role": role,
            "model": model, "reasoning_effort": effort, "usage": {"cost_usd": cost}, "blockers": []}))
        (directory / "trace.json").write_text(json.dumps({"duration_ms": minutes * 60000}))

    def verdict(self, run_id, accepted=True, **options):
        core.record_outcome(self.workspace, run_id, accepted, "checked the diff and tests", **options)

    def test_pinned_runs_count_and_each_row_has_an_interval(self):
        for n in range(4):
            self.run_dir(f"sol-{n}")
            self.verdict(f"sol-{n}", n != 0)
        self.run_dir("opus-0", agent="claude", model="claude-opus-5-5", cost=1.2, minutes=30)
        self.verdict("opus-0")
        rows = {row["lane"]: row for row in board.scoreboard(self.workspace)["rows"]}
        sol = rows["codex:gpt-6.1-sol:high"]
        self.assertEqual((sol["accepted"], sol["settled"], sol["chosen"]), (3, 4, {"pinned": 4}))
        self.assertEqual(sol["acceptance_ci95"], [0.301, 0.954])
        self.assertEqual((sol["median_cost_usd"], sol["median_minutes"]), (0.4, 10.0))
        self.assertEqual(rows["claude:claude-opus-5-5:high"]["settled"], 1)

    def test_effort_falls_back_to_the_requested_choice(self):
        directory = self.workspace / ".fusion" / "runs" / "old-1"
        directory.mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps({
            "schema": core.SCHEMA, "run_id": "old-1", "agent": "claude", "role": "implementation", "write": True,
            "task": "Fix old-1", "created_at": core.now_ms(),
            "resolved": {"model": "claude-opus-5-5", "execution_choice": {"requested": {"model": "claude-opus-5-5",
                                                                                      "reasoning_effort": "medium"}}}}))
        (directory / "result.json").write_text(json.dumps({
            "schema": core.SCHEMA, "run_id": "old-1", "status": "success", "verdict": "ok", "agent": "claude",
            "role": "implementation", "model": "claude-opus-5-5", "blockers": []}))
        self.verdict("old-1")
        self.assertEqual([row["lane"] for row in board.scoreboard(self.workspace)["rows"]], ["claude:claude-opus-5-5:medium"])

    def test_routed_and_fallback_runs_are_told_apart(self):
        self.run_dir("auto-1")
        self.verdict("auto-1")
        DecisionStore(self.workspace).append("routing_log", task_id="auto-1", scope="auto", chosen="codex-sol61-high")
        self.run_dir("moved-1", agent="claude", model="claude-opus-5-5",
                     pin_fallback={"from": "codex-sol61-high", "to": "claude-opus-high", "reason": "quota"})
        self.verdict("moved-1", False, stage="review", rejection_class="review_changes")
        rows = {row["lane"]: row for row in board.scoreboard(self.workspace)["rows"]}
        self.assertEqual(rows["codex:gpt-6.1-sol:high"]["chosen"], {"routed": 1})
        self.assertEqual(rows["claude:claude-opus-5-5:high"]["chosen"], {"fallback": 1})

    def test_unsettled_runs_are_left_out_and_counted(self):
        self.run_dir("settled")
        self.verdict("settled")
        self.run_dir("no-verdict")
        self.run_dir("eval-gone")
        self.verdict("eval-gone", False, stage="verify", rejection_class="eval_unmeasured")
        value = board.scoreboard(self.workspace)
        self.assertEqual([row["settled"] for row in value["rows"]], [1])
        self.assertEqual(value["excluded"], {"eval_unmeasured": 1, "no_outcome": 1})

    def test_role_filter_and_text_view(self):
        self.run_dir("impl")
        self.verdict("impl")
        self.run_dir("rev", role="review", agent="claude", model="claude-opus-5-5")
        self.verdict("rev")
        value = board.scoreboard(self.workspace, role="review")
        self.assertEqual([row["role"] for row in value["rows"]], ["review"])
        text = board.render(board.scoreboard(self.workspace))
        self.assertIn("codex:gpt-6.1-sol:high", text)
        self.assertIn("1/1", text)
        self.assertIn("pinned=1", text)

    def test_cli_json(self):
        self.run_dir("one")
        self.verdict("one")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = core.main(["--workspace", str(self.workspace), "--json", "scoreboard"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["schema"], "fusion.scoreboard.v1")


if __name__ == "__main__":
    unittest.main()
