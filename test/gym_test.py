"""ORC gym: authored checks with a pre-change run, task extraction from a
squash-merged fix, and lane runs that label fail->pass and no-change honestly."""
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
import fusion_decisions
import fusion_gym as gym
from fusion_decisions import DecisionStore, read_jsonl
from fusion_workflow import WorkflowRunner, validate_spec
from decisions_test import Backend

WORKER = '''import json, pathlib, sys
sys.stdin.read()
for name, body in WRITES.items():
    pathlib.Path(name).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(name).write_text(body)
text = "STATUS: success\\nSUMMARY: fixed the reported problem\\nCHANGED: calc.py\\nTESTS: none\\nBLOCKERS: none"
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''
BUGGY = "def add(a, b):\n    return abs(a) + b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST_BASE = '''import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calc import add


class CalcTest(unittest.TestCase):
    def test_add_positive(self):
        self.assertEqual(add(2, 3), 5)
'''
TEST_FIX = TEST_BASE + '''
    def test_add_negative(self):
        self.assertEqual(add(-2, 3), 1)
'''
EXISTS = "import pathlib, sys\nsys.exit(0 if pathlib.Path('done.txt').exists() else 1)\n"
ABSENT = "import pathlib, sys\nsys.exit(1 if pathlib.Path('done.txt').exists() else 0)\n"


def run_git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class Isolated(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("FUSION_DECISIONS_MODE", "FUSION_CONFIG", "FUSION_READ_ONLY"):
            os.environ.pop(name, None)
        runtime = patch.object(fusion_decisions, "runtime_for", lambda options: Backend())
        runtime.start()
        self.addCleanup(runtime.stop)

    def worker(self, name, writes):
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + f"WRITES = {writes!r}\n" + WORKER)
        path.chmod(0o755)
        return str(path)

    def gate_labels(self, workspace):
        return [e for e in read_jsonl(DecisionStore(workspace).path)
                if e.get("event") == "label" and e.get("source") == "structural_gate"]


class AuthoredBeforeTest(Isolated):
    def setUp(self):
        super().setUp()
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        (self.workspace / "exists.py").write_text(EXISTS)
        (self.workspace / "absent.py").write_text(ABSENT)
        (self.workspace / ".gitignore").write_text(".fusion/\n")
        run_git(self.workspace, "init", "-q")
        run_git(self.workspace, "add", ".")
        run_git(self.workspace, "commit", "-qm", "seed")

    def run_node(self, checks, writes, before=True):
        config = {"codex": {"command": self.worker("fake-codex", writes)}, "claude": {"command": "missing-claude"},
                  "agy": {"command": "missing-agy"}, "grok": {"command": "missing-grok"},
                  "timeout_seconds": 30, "decisions": {"mode": "shadow"}}
        acceptance = {"checks": checks, "required_handoff": ["summary"], **({"before": True} if before else {})}
        spec = {"task": "t", "nodes": [{"id": "implement", "role": "implementation", "agent": "codex", "write": True,
                                        "task": "Make it done", "acceptance": acceptance}]}
        outcome = WorkflowRunner(self.workspace, config, spec).run()
        return outcome, outcome["nodes"][0]["result"]

    def test_fail_before_pass_after_labels_done(self):
        outcome, result = self.run_node([["python3", "exists.py"]], {"done.txt": "x"})
        self.assertEqual(outcome["status"], "success", outcome)
        [check] = result["acceptance_checks"]
        self.assertEqual((check["origin"], check["vacuous"], check["before"]["status"]), ("authored", False, "failed"))
        self.assertNotIn("env_overrides", check, "authored checks run in the same environment before and after")
        saved = json.loads((Path(outcome["artifacts"]["root"]) / "nodes/implement/acceptance/before-authored.json").read_text())
        self.assertEqual(saved["checks"], [["python3", "exists.py"]])
        self.assertEqual(result["gate_label"]["answers"], {"failed_task": "false"})

    def test_pass_before_fail_after_labels_failed_and_fail_fail_is_no_evidence(self):
        _, result = self.run_node([["python3", "absent.py"]], {"done.txt": "x"})
        self.assertEqual(result["gate_label"]["answers"], {"failed_task": "true"})
        _, result = self.run_node([["python3", "exists.py"]], {"other.txt": "x"})
        self.assertEqual(result["gate_label"]["status"], "unlabeled")

    def test_without_before_authored_checks_stay_unlabeled(self):
        _, result = self.run_node([["python3", "exists.py"]], {"done.txt": "x"}, before=False)
        [check] = result["acceptance_checks"]
        self.assertNotIn("vacuous", check)
        self.assertEqual(result["gate_label"]["status"], "unlabeled")

    def test_before_must_be_a_boolean_on_a_write_node(self):
        for node in ({"write": False, "acceptance": {"before": True}}, {"write": True, "acceptance": {"before": "yes"}}):
            with self.assertRaises(ValueError):
                validate_spec({"nodes": [{"id": "n", "agent": "codex", "task": "t", **node}]})


class GymTest(Isolated):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "source"
        (self.repo / "test").mkdir(parents=True)
        (self.repo / "calc.py").write_text(BUGGY)
        (self.repo / "test/calc_test.py").write_text(TEST_BASE)
        (self.repo / ".gitignore").write_text(".fusion/\n.fusion.json\n__pycache__/\n")
        run_git(self.repo, "init", "-q")
        run_git(self.repo, "add", ".gitignore")
        run_git(self.repo, "commit", "-qm", "init")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "seed")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        (self.repo / "calc.py").write_text(FIXED)
        (self.repo / "test/calc_test.py").write_text(TEST_FIX)
        run_git(self.repo, "commit", "-qam", "fix(calc): add keeps the sign of a negative first operand (#7)")
        self.fix = run_git(self.repo, "rev-parse", "HEAD")
        (self.repo / "README").write_text("later\n")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "docs: unrelated (#8)")
        self.config = {"claude": {"command": "missing-claude"}, "agy": {"command": "missing-agy"},
                       "grok": {"command": "missing-grok"}, "timeout_seconds": 60, "decisions": {"mode": "shadow"},
                       "routes": {
                           "fixer": {"agent": "codex", "command": self.worker("fixer", {"calc.py": FIXED})},
                           "idler": {"agent": "codex", "command": self.worker("idler", {"notes.txt": "looked\n"})},
                           "cheater": {"agent": "codex", "command": self.worker("cheater", {"test/calc_test.py": TEST_BASE})},
                       }}
        (self.repo / ".fusion.json").write_text(json.dumps(self.config))
        self.tasks = self.root / "tasks"
        self.gym_dir = self.root / "gym"

    def extract(self):
        summary, rows = gym.extract(self.repo, [7, 8, 99], self.tasks, use_gh=False)
        return summary, {row["pr"]: row for row in rows}

    def test_extraction_finds_fail_to_pass_without_touching_the_worktree(self):
        status = run_git(self.repo, "status", "--porcelain")
        summary, rows = self.extract()
        task = rows[7]
        self.assertEqual(task["fail_to_pass"], ["calc_test.CalcTest.test_add_negative"])
        self.assertEqual(task["pass_to_pass"], ["calc_test.CalcTest.test_add_positive"])
        self.assertEqual((task["base"], task["fix"], task["source_files"], task["test_files"]),
                         (self.base, self.fix, ["calc.py"], ["test/calc_test.py"]))
        self.assertEqual(task["prompt"], "add keeps the sign of a negative first operand")
        self.assertEqual(task["prompt_source"], "commit")
        self.assertEqual(task["checks"]["fail_to_pass"], [["python3", "-m", "unittest", "discover", "-s", "test", "-t", "test",
                                                          "-p", "calc_test.py", "-k", "*calc_test.CalcTest.test_add_negative"]])
        # The task commit is the base plus the new test only; the fix is absent.
        self.assertEqual(run_git(self.repo, "rev-parse", "refs/gym/tasks/pr-7"), task["task_sha"])
        self.assertEqual(run_git(self.repo, "show", f"{task['task_sha']}:calc.py") + "\n", BUGGY)
        self.assertEqual(run_git(self.repo, "show", f"{task['task_sha']}:test/calc_test.py") + "\n", TEST_FIX)
        self.assertNotIn("7", run_git(self.repo, "log", "-1", "--format=%B", task["task_sha"]))
        self.assertEqual(run_git(self.repo, "status", "--porcelain"), status)
        self.assertEqual(rows[8]["skipped"], "no test files changed")
        self.assertIn("no commit for PR #99", rows[99]["skipped"])
        self.assertEqual([t["id"] for t in summary["tasks"]], ["pr-7"])
        self.assertTrue((self.tasks / "pr-7.json").is_file() and (self.tasks / "index.json").is_file())
        # Re-extraction is deterministic.
        self.assertEqual(gym.extract_one(self.repo, 7, use_gh=False)["task_sha"], task["task_sha"])

    def test_prompts_come_from_the_issue_or_the_problem_paragraphs(self):
        prompt, source = gym.task_prompt({"issues": [{"title": "Bug", "body": "It breaks.\n\n## Proposed fix\nDo X"}]}, "")
        self.assertEqual((prompt, source), ("Bug\n\nIt breaks.", "issue"))
        body = ("Fixes #3. Workers wrap labels in \\`**\\`.\n\n```\n**STATUS:** ok\n```\n\n"
                "Labels now accept emphasis.\n\n```diff\n+x\n```\n\nValidation: tests pass.")
        prompt, source = gym.task_prompt({"title": "fix(core): read bold labels", "body": body}, "")
        self.assertEqual(source, "pr")
        self.assertEqual(prompt, "read bold labels\n\nWorkers wrap labels in `**`.\n\n```\n**STATUS:** ok\n```")
        body = "## Problem\nInstall misses a module.\n\n- Adds it to the loop.\n- A test checks every module."
        self.assertEqual(gym.task_prompt({"title": "fix(install): copy modules", "body": body}, "")[0],
                         "copy modules\n\nInstall misses a module.")
        self.assertEqual(gym.task_prompt({"title": "fix: x", "body": "Adds `y` to the loop."}, "")[0], "x")
        body = "**1. Routes inherit a model.**\n- The merge copies it.\n- Fix: routes keep their own."
        self.assertEqual(gym.task_prompt({"title": "fix: x", "body": body}, "")[0],
                         "x\n\n**1. Routes inherit a model.**\n- The merge copies it.")

    def test_lanes_label_fail_to_pass_and_no_change_and_resume(self):
        self.extract()
        result = gym.run(self.tasks, ["fixer", "idler", "cheater"], self.gym_dir)
        self.assertEqual(result["status"], "complete")
        runs = {row["lane"]: row for row in result["runs"]}
        self.assertEqual({lane: row["verdict"] for lane, row in runs.items()},
                         {"fixer": "solved", "idler": "unsolved", "cheater": "tampered"})
        self.assertEqual(runs["fixer"]["gate_label"]["answers"], {"failed_task": "false"})
        self.assertEqual(runs["fixer"]["changed"], ["calc.py"])
        self.assertIn("return a + b", Path(runs["fixer"]["patch"]).read_text())
        # Fail before, fail after: the gate rejects, and labels nothing.
        self.assertEqual((runs["idler"]["status"], runs["idler"]["gate_label"]["status"]), ("failed", "unlabeled"))
        self.assertTrue(runs["idler"]["baseline_ok"])
        self.assertEqual(runs["cheater"]["tampered"], ["test/calc_test.py"])
        # Labels, runs and workflows accumulate in the gym workspace only.
        labels = self.gate_labels(self.gym_dir)
        self.assertEqual([label["answers"] for label in labels], [{"failed_task": "false"}])
        self.assertFalse((self.repo / ".fusion").exists())
        self.assertTrue(all(row["workflow_id"].startswith("gym-pr-7-") for row in runs.values()))
        # The per-task repository never holds the fix; lane worktrees are removed.
        task_repo = self.gym_dir / "tasks/pr-7/repo"
        self.assertFalse(gym.succeeds(task_repo, "cat-file", "-e", self.fix + "^{commit}"))
        self.assertEqual(list((self.gym_dir / "tasks/pr-7/lanes").iterdir()), [])
        # Resumable: completed task x lane pairs are skipped.
        again = gym.run(self.tasks, ["fixer", "idler", "cheater"], self.gym_dir)
        self.assertEqual(again["runs"], [])
        value = gym.report(self.gym_dir)
        self.assertEqual(value["tasks"]["pr-7"]["solved_by"], ["fixer"])
        self.assertEqual((value["lanes"]["fixer"]["f2p_pass_rate"], value["lanes"]["idler"]["f2p_pass_rate"]), (1.0, 0.0))
        self.assertEqual(value["lanes"]["cheater"]["tampered"], 1)
        self.assertIn("fixer", gym.table(value))

    def test_a_shallow_source_repository_works(self):
        shallow = self.root / "shallow"
        run_git(self.root, "clone", "-q", "--depth", "3", self.repo.as_uri(), str(shallow))
        self.assertEqual(run_git(shallow, "rev-parse", "--is-shallow-repository"), "true")
        (shallow / ".fusion.json").write_text(json.dumps(self.config))
        _, [task] = gym.extract(shallow, [7], self.tasks, use_gh=False)
        self.assertEqual(task["fail_to_pass"], ["calc_test.CalcTest.test_add_negative"])
        [row] = gym.run(self.tasks, ["fixer"], self.gym_dir)["runs"]
        self.assertEqual(row["verdict"], "solved")

    def test_an_unavailable_lane_and_budget_are_not_completed_runs(self):
        self.extract()
        result = gym.run(self.tasks, ["agy"], self.gym_dir)
        [row] = result["runs"]
        self.assertEqual((row["verdict"], row["completed"]), ("unavailable", False))
        self.assertEqual(gym.report(self.gym_dir)["incomplete"], ["pr-7:agy"])
        self.assertEqual(gym.run(self.tasks, ["fixer"], self.gym_dir, budget_usd=0)["status"], "budget_reached")

    def test_cli_and_guards(self):
        self.extract()
        with self.assertRaises(ValueError):
            gym.run(self.tasks, ["fixer"], self.repo / "inside")
        with self.assertRaises(ValueError):
            gym.resolve_lane({}, "nope")
        self.assertEqual(gym.resolve_lane({}, "claude-opus-high"),
                         {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"})
        spec = validate_spec(gym.build_spec(json.loads((self.tasks / "pr-7.json").read_text()),
                                            gym.resolve_lane({}, "claude-sonnet-high"), 2.0))
        node = spec["graph"]["nodes"][0]
        self.assertEqual((node["acceptance"]["before"], node["max_budget_usd"], node["reasoning_effort"]), (True, 2.0, "high"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = core.main(["--quiet", "gym", "report", str(self.gym_dir)])
        self.assertEqual(code, 0)
        self.assertIn("lane", out.getvalue())


if __name__ == "__main__":
    unittest.main()
