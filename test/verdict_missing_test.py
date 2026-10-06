"""A review delegation without a VERDICT line is verdict_missing, not a success (finding 367)."""
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
from fusion_policy import route_candidates

HANDOFF = "STATUS: success\nSUMMARY: read the diff\nCHANGED: none\nTESTS: none\nBLOCKERS: none"


class VerdictMissingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_CONTROL_WORKSPACE", None)
        self.answer = self.root / "answer.txt"
        self.brief = self.root / "brief.txt"
        worker = self.root / "claude-fixture"
        worker.write_text(f"#!{sys.executable}\nimport json, pathlib, sys\n"
                          f"pathlib.Path({str(self.brief)!r}).write_text(sys.argv[-1])\n"
                          f"answer = pathlib.Path({str(self.answer)!r}).read_text()\n"
                          "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                          "'result':answer}))\n")
        worker.chmod(0o755)
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off", "rank_by_outcomes": 1},
                                                      "claude": {"command": str(worker)}})

    def cli(self, *argv, config=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(core, "load_config", return_value=(config or self.config, None)):
            code = core.main(["--workspace", str(self.workspace), *argv])
        return code, out.getvalue()

    def delegate(self, answer, role="review", *extra):
        self.answer.write_text(answer)
        code, out = self.cli("--json", "delegate", "--agent", "claude", "--fresh", "--role", role, *extra, "review the diff")
        return code, json.loads(out)

    def checked_runs(self, role="review"):
        task = core.make_task(self.workspace, "auto", "x", role, [], [], None, False, False)
        return next(c for c in route_candidates(self.config, task, core.RunStore(self.workspace))
                    if c["key"] == "claude")["checked_runs_local"]

    def test_parse_verdict_reads_a_decorated_label_at_line_start_only(self):
        for text, verdict in (("VERDICT: approve\nreasons", "approve"), ("**VERDICT:** Changes, see a.py:3", "changes"),
                              ("## Verdict: approve", "approve"), ("  VERDICT: `changes`", "changes"),
                              ("VERDICT: maybe\nVERDICT: approve", "approve"), ("VERDICT: maybe", ""),
                              ("I would say VERDICT: approve", ""), (HANDOFF, "")):
            with self.subTest(text=text):
                self.assertEqual(core.parse_verdict(text), verdict)
                self.assertEqual(core.parse_handoff(text)["reported_verdict"], verdict)
        self.assertEqual(core.parse_handoff("VERDICT: changes\n" + HANDOFF)["blockers"], [])
        self.assertEqual(core.parse_handoff("VERDICT: changes\n" + HANDOFF)["summary"], "read the diff")

    def test_a_review_with_a_verdict_is_an_ordinary_success(self):
        code, result = self.delegate("VERDICT: approve\n" + HANDOFF)
        self.assertEqual((code, result["status"], result["verdict"]), (0, "success", "ok"))
        self.assertEqual(result["reported_verdict"], "approve")
        self.assertEqual(result["blockers"], [])
        self.assertIn("VERDICT: approve | changes\nSTATUS:", self.brief.read_text())
        self.assertIsNone(core.failure_class(result))

    def test_a_review_without_a_verdict_is_verdict_missing_and_exits_5(self):
        code, result = self.delegate(HANDOFF, "lead reviewer")
        self.assertEqual(code, 5)
        self.assertEqual((result["status"], result["verdict"], result["reported_verdict"]),
                         ("verdict_missing", "verdict_missing", None))
        self.assertEqual(core.failure_class(result), "verdict_missing")
        self.assertIsNone(result["provider_failure"])
        self.assertTrue(any(b.startswith("verdict missing:") for b in result["blockers"]))
        stored = json.loads((Path(result["artifacts"]["run_dir"]) / "result.json").read_text())
        self.assertEqual(stored["status"], "verdict_missing")
        [span] = [s for s in core.RunStore(self.workspace).traces() if s["run_id"] == result["run_id"]]
        self.assertEqual((span["status"], span["failure_class"]), ("verdict_missing", "verdict_missing"))

    def test_a_partial_review_without_a_verdict_is_verdict_missing_but_a_blocked_one_stays_blocked(self):
        self.assertEqual(self.delegate(HANDOFF.replace("success", "partial"), "reviewer")[1]["status"], "verdict_missing")
        self.assertEqual(self.delegate(HANDOFF.replace("success", "blocked"), "reviewer")[1]["status"], "blocked")

    def test_verdict_missing_counts_neither_way_in_routing_and_makes_no_label(self):
        _, missing = self.delegate(HANDOFF)
        self.assertEqual(self.checked_runs(), 0)
        shadow = core.deep_merge(self.config, {"decisions": {"mode": "shadow", "verdict_labels": True}})
        for flag in ("--accepted", "--rejected"):
            with patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "shadow"}):
                code, out = self.cli("outcome", missing["run_id"], flag, "--reason", "read it", config=shadow)
            self.assertEqual(code, 0)
            label = json.loads(out)["label"]
            self.assertEqual(label["status"], "skipped")
            self.assertIn("'verdict_missing'", label["reason"])
            self.assertEqual(self.checked_runs(), 0)
        _, approved = self.delegate("VERDICT: approve\n" + HANDOFF)
        self.assertEqual(self.cli("outcome", approved["run_id"], "--rejected", "--reason", "x")[0], 0)
        self.assertEqual(self.checked_runs(), 1)

    def test_the_mcp_delegate_reports_verdict_missing(self):
        self.answer.write_text(HANDOFF)
        request = {"id": 1, "method": "tools/call", "params": {"name": "fusion_delegate", "arguments": {
            "agent": "claude", "task": "review the diff", "role": "review", "write": False, "resume": False}}}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(request) + "\n")), contextlib.redirect_stdout(output):
            core.run_mcp(self.workspace, self.config)
        response = json.loads(output.getvalue())["result"]
        self.assertEqual(response["structuredContent"]["status"], "verdict_missing")

    def test_other_roles_and_internal_dispatches_need_no_verdict(self):
        code, result = self.delegate(HANDOFF, "implementation")
        self.assertEqual((code, result["status"], result["verdict"]), (0, "success", "ok"))
        self.assertNotIn("reported_verdict", result)
        self.assertNotIn("VERDICT", self.brief.read_text())
        self.answer.write_text(HANDOFF)
        task = core.make_task(self.workspace, "claude", "x", "review", [], [], None, False, False)
        result = core.dispatch(self.config, task, core.RunStore(self.workspace))
        self.assertEqual(result["status"], "success")
        self.assertNotIn("reported_verdict", result)


if __name__ == "__main__":
    unittest.main()
