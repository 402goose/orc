"""ORC gym: authored checks with a pre-change run, acceptance fixtures, task
extraction from a squash-merged fix, and lane runs (hidden and visible tests)
that label fail->pass and no-change honestly."""
import contextlib
import hashlib
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
prompt = sys.stdin.read()
if SEE:
    seen = {name: pathlib.Path(name).read_text() if pathlib.Path(name).is_file() else None for name in SEE}
    pathlib.Path("seen.json").write_text(json.dumps({"prompt": prompt, "files": seen}))
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
FORGED = TEST_BASE + '''
    def test_add_negative(self):
        pass
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

    def worker(self, name, writes, see=()):
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + f"WRITES = {writes!r}\nSEE = {list(see)!r}\n" + WORKER)
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

    def run_node(self, checks, writes, before=True, fixtures=None, see=()):
        config = {"codex": {"command": self.worker("fake-codex", writes, see)}, "claude": {"command": "missing-claude"},
                  "agy": {"command": "missing-agy"}, "grok": {"command": "missing-grok"},
                  "timeout_seconds": 30, "decisions": {"mode": "shadow"}}
        acceptance = {"checks": checks, "required_handoff": ["summary"], **({"before": True} if before else {}),
                      **({"fixtures": fixtures} if fixtures is not None else {})}
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


class FixtureTest(AuthoredBeforeTest):
    """acceptance.fixtures: written for every check run, set aside again after."""

    def test_fixtures_run_before_and_after_and_the_worker_never_sees_them(self):
        (self.root / "hidden-check.py").write_text(EXISTS)
        fixtures = [{"path": "gate.py", "from_file": str(self.root / "hidden-check.py")},
                    {"path": "deep/er/note.txt", "content": "fixture\n"}]
        # The worker writes its own gate.py that always passes: the fixture wins.
        outcome, result = self.run_node([["python3", "gate.py"]], {"done.txt": "x", "gate.py": "raise SystemExit(0)\n"},
                                        fixtures=fixtures, see=["gate.py", "deep/er/note.txt"])
        self.assertEqual(outcome["status"], "success", outcome)
        seen = json.loads((self.workspace / "seen.json").read_text())
        self.assertEqual(seen["files"], {"gate.py": None, "deep/er/note.txt": None})
        self.assertNotIn("gate.py", seen["prompt"])
        [check] = result["acceptance_checks"]
        self.assertEqual((check["status"], check["before"]["status"]), ("passed", "failed"))
        before = json.loads(Path(check["before"]["receipt"]).read_text())
        digest = hashlib.sha256(EXISTS.encode()).hexdigest()
        self.assertEqual([(f["path"], f["sha256"], f["found"]["type"]) for f in before["fixtures"]],
                         [("gate.py", digest, "absent"), ("deep/er/note.txt", hashlib.sha256(b"fixture\n").hexdigest(), "absent")])
        worker_gate = hashlib.sha256(b"raise SystemExit(0)\n").hexdigest()
        self.assertEqual(check["fixtures"][0], {"path": "gate.py", "sha256": digest,
                                                "found": {"type": "file", "sha256": worker_gate}})
        # Afterwards the worker's own file is back and the fixture directory is gone.
        self.assertEqual((self.workspace / "gate.py").read_text(), "raise SystemExit(0)\n")
        self.assertFalse((self.workspace / "deep").exists())
        self.assertEqual(result["gate_label"]["answers"], {"failed_task": "false"})
        saved = json.loads((Path(outcome["artifacts"]["root"]) / "nodes/implement/acceptance/before-authored.json").read_text())
        self.assertEqual([f["path"] for f in saved["fixtures"]], ["gate.py", "deep/er/note.txt"])

    def test_a_worker_edit_at_a_fixture_path_cannot_pass_the_check(self):
        fixtures = [{"path": "gate.py", "content": EXISTS}]
        _, result = self.run_node([["python3", "gate.py"]], {"gate.py": "raise SystemExit(0)\n", "other.txt": "x"},
                                  fixtures=fixtures)
        [check] = result["acceptance_checks"]
        self.assertEqual(check["status"], "failed")
        self.assertEqual(result["gate_label"]["status"], "unlabeled")

    def test_bytecode_of_a_fixture_is_removed(self):
        (self.workspace / "pkg").mkdir()
        (self.workspace / "pkg/keep.txt").write_text("k\n")
        fixtures = [{"path": "pkg/hidden_mod.py", "content": "VALUE = 1\n"}]
        check = ["python3", "-c", "import sys; sys.path.insert(0, 'pkg'); import hidden_mod"]
        with patch.dict(os.environ):
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            _, result = self.run_node([check], {"done.txt": "x"}, fixtures=fixtures)
        self.assertEqual(result["acceptance_checks"][0]["status"], "passed")
        self.assertFalse((self.workspace / "pkg/hidden_mod.py").exists())
        self.assertEqual(list((self.workspace / "pkg/__pycache__").glob("hidden_mod.*")) if
                         (self.workspace / "pkg/__pycache__").exists() else [], [])

    def test_fixtures_are_validated(self):
        for fixtures in ("x", [{"path": "a"}], [{"path": "a", "content": "x", "from_file": "y"}],
                         [{"path": "../a", "content": "x"}], [{"path": ".git/x", "content": "x"}],
                         [{"path": "a", "content": "x"}, {"path": "a/b", "content": "y"}],
                         [{"path": "a", "content": "x"}, {"path": "a", "content": "y"}],
                         [{"path": "a", "content": 3}], [{"path": "a", "content": "x", "mode": "644"}]):
            with self.subTest(fixtures=fixtures), self.assertRaises(ValueError):
                validate_spec({"nodes": [{"id": "n", "agent": "codex", "task": "t", "write": True,
                                          "acceptance": {"checks": [["true"]], "fixtures": fixtures}}]})

    def test_a_fixture_path_through_a_symlink_out_of_the_tree_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "link").symlink_to(outside, target_is_directory=True)
        _, result = self.run_node([["python3", "exists.py"]], {"done.txt": "x"},
                                  fixtures=[{"path": "link/x.py", "content": "x"}])
        [check] = result["acceptance_checks"]
        self.assertEqual(check["status"], "error")
        self.assertIn("outside the workspace", check["error"])
        self.assertEqual(list(outside.iterdir()), [])


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
                           "forger": {"agent": "codex", "command": self.worker("forger", {"test/calc_test.py": FORGED})},
                           "observer": {"agent": "codex", "command": self.worker(
                               "observer", {"calc.py": FIXED}, see=["test/calc_test.py"])},
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
        # The hidden form: the worker starts at B; C's test file is a fixture.
        hidden = task["hidden"]
        self.assertEqual((hidden["base_ref"], hidden["base_sha"]), ("refs/gym/bases/pr-7", self.base))
        self.assertEqual(run_git(self.repo, "rev-parse", "refs/gym/bases/pr-7"), self.base)
        [fixture] = hidden["fixtures"]
        self.assertEqual(fixture["path"], "test/calc_test.py")
        self.assertEqual(gym.base64.b64decode(fixture["content_base64"]).decode(), TEST_FIX)
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
        result = gym.run(self.tasks, ["fixer", "idler", "cheater"], self.gym_dir, mode="visible")
        self.assertEqual(result["status"], "complete")
        runs = {row["lane"]: row for row in result["runs"]}
        self.assertEqual({lane: row["verdict"] for lane, row in runs.items()},
                         {"fixer": "solved", "idler": "unsolved", "cheater": "tampered"})
        self.assertEqual(runs["fixer"]["gate_label"]["answers"], {"failed_task": "false"})
        self.assertEqual(runs["fixer"]["changed"], ["calc.py"])
        self.assertIn("return a + b", Path(runs["fixer"]["patch"]).read_text())
        # Fail before, fail after on a declared FAIL_TO_PASS check: the task was not done.
        self.assertEqual((runs["idler"]["status"], runs["idler"]["gate_label"]["answers"]), ("failed", {"failed_task": "true"}))
        self.assertTrue(runs["idler"]["baseline_ok"])
        self.assertEqual(runs["cheater"]["tampered"], ["test/calc_test.py"])
        # Labels, runs and workflows accumulate in the gym workspace only.
        labels = self.gate_labels(self.gym_dir)
        # Fixer done, idler failed; the cheater's label is appended then retracted ({}).
        self.assertEqual([label["answers"] for label in labels],
                         [{"failed_task": "false"}, {"failed_task": "true"}, {"failed_task": "true"}, {}])
        self.assertFalse((self.repo / ".fusion").exists())
        self.assertTrue(all(row["workflow_id"].startswith("gym-pr-7-") for row in runs.values()))
        # The per-task repository never holds the fix; lane worktrees are removed.
        task_repo = self.gym_dir / "tasks/pr-7/repo"
        self.assertFalse(gym.succeeds(task_repo, "cat-file", "-e", self.fix + "^{commit}"))
        self.assertEqual(list((self.gym_dir / "tasks/pr-7/lanes").iterdir()), [])
        # Resumable: completed task x lane pairs are skipped.
        again = gym.run(self.tasks, ["fixer", "idler", "cheater"], self.gym_dir, mode="visible")
        self.assertEqual(again["runs"], [])
        value = gym.report(self.gym_dir)["modes"]["visible"]
        self.assertEqual(value["tasks"]["pr-7"]["solved_by"], ["fixer"])
        self.assertEqual((value["lanes"]["fixer"]["f2p_pass_rate"], value["lanes"]["idler"]["f2p_pass_rate"]), (1.0, 0.0))
        self.assertEqual(value["lanes"]["cheater"]["tampered"], 1)
        self.assertIn("fixer", gym.table(gym.report(self.gym_dir)))

    def test_hidden_tests_grade_a_worker_that_only_sees_the_problem(self):
        self.extract()
        result = gym.run(self.tasks, ["fixer", "idler", "cheater", "forger", "observer"], self.gym_dir)
        self.assertEqual((result["status"], result["mode"]), ("complete", "hidden"))
        runs = {row["lane"]: row for row in result["runs"]}
        self.assertEqual({lane: row["verdict"] for lane, row in runs.items()},
                         {"fixer": "solved", "idler": "unsolved", "cheater": "unsolved", "forger": "unsolved",
                          "observer": "solved"})
        self.assertTrue(all(row["mode"] == "hidden" and row["key"].endswith(":hidden") for row in runs.values()))
        # fail->pass labels the fixer done; fail->fail on its FAIL_TO_PASS check labels the idler failed.
        self.assertEqual(runs["fixer"]["gate_label"]["answers"], {"failed_task": "false"})
        self.assertTrue(runs["fixer"]["baseline_ok"])
        self.assertEqual(runs["fixer"]["changed"], ["calc.py"])
        self.assertEqual((runs["idler"]["status"], runs["idler"]["gate_label"]["answers"]), ("failed", {"failed_task": "true"}))
        # A worker that rewrites the hidden test file (the forger makes the F2P
        # test pass trivially) is graded by the fixture anyway: recorded, not
        # rewarded, and not "tampered". The cheater's write equals B: no change.
        self.assertEqual((runs["forger"]["touched_fixtures"], runs["forger"]["tampered"]), (["test/calc_test.py"], []))
        self.assertEqual((runs["cheater"]["touched_fixtures"], runs["cheater"]["changed"]), ([], []))
        self.assertFalse(runs["forger"]["f2p_passed"] or runs["cheater"]["f2p_passed"])
        # The fixture graded the forger's untouched code: the task was not done.
        self.assertEqual(runs["forger"]["gate_label"]["answers"], {"failed_task": "true"})
        # The worker saw the base test file and a prompt that names no test.
        patch_text = Path(runs["observer"]["patch"]).read_text()
        self.assertIn("seen.json", patch_text)
        self.assertNotIn("test_add_negative", patch_text)
        self.assertIn("test_add_positive", patch_text)
        self.assertIn("tests that are not in this repository will grade the change", patch_text)
        self.assertNotIn("calc_test", patch_text.split("prompt")[1].split("files")[0])
        # The hidden task repository holds neither the fix nor the task commit.
        hidden_repo = self.gym_dir / "tasks/pr-7/hidden/repo"
        task = json.loads((self.tasks / "pr-7.json").read_text())
        for sha in (self.fix, task["task_sha"]):
            self.assertFalse(gym.succeeds(hidden_repo, "cat-file", "-e", sha + "^{commit}"))
        self.assertEqual(list((self.gym_dir / "tasks/pr-7/hidden/lanes").iterdir()), [])
        value = gym.report(self.gym_dir)
        self.assertEqual(list(value["modes"]), ["hidden"])
        self.assertEqual(value["modes"]["hidden"]["tasks"]["pr-7"]["solved_by"], ["fixer", "observer"])
        self.assertEqual(value["modes"]["hidden"]["lanes"]["forger"]["touched_fixtures"], 1)
        self.assertIn("hidden tests", gym.table(value))

    def test_resume_keys_keep_hidden_and_visible_apart(self):
        self.extract()
        [hidden] = gym.run(self.tasks, ["fixer"], self.gym_dir)["runs"]
        self.assertEqual(hidden["key"], "pr-7:fixer:hidden")
        [visible] = gym.run(self.tasks, ["fixer"], self.gym_dir, mode="visible")["runs"]
        self.assertEqual(visible["key"], "pr-7:fixer:visible")
        self.assertEqual(gym.run(self.tasks, ["fixer"], self.gym_dir)["runs"], [])
        self.assertEqual(gym.run(self.tasks, ["fixer"], self.gym_dir, mode="visible")["runs"], [])
        # A row written before modes existed was a visible run.
        legacy = {"schema": gym.RESULT_SCHEMA, "event": "finished", "key": "pr-7:idler", "task": "pr-7", "lane": "idler",
                  "completed": True, "verdict": "unsolved", "f2p_passed": False, "p2p_regressed": False, "tampered": []}
        with (self.gym_dir / "results.jsonl").open("a") as stream:
            stream.write(json.dumps(legacy) + "\n")
        self.assertEqual(gym.run(self.tasks, ["idler"], self.gym_dir, mode="visible")["runs"], [])
        [row] = gym.run(self.tasks, ["idler"], self.gym_dir)["runs"]
        self.assertEqual(row["key"], "pr-7:idler:hidden")
        modes = gym.report(self.gym_dir)["modes"]
        self.assertEqual({mode: sorted(section["lanes"]) for mode, section in modes.items()},
                         {"hidden": ["fixer", "idler"], "visible": ["fixer", "idler"]})

    def test_a_task_extracted_before_the_hidden_form_upgrades_in_place(self):
        self.extract()
        path = self.tasks / "pr-7.json"
        task = json.loads(path.read_text())
        task.pop("hidden")
        path.write_text(json.dumps(task))
        run_git(self.repo, "update-ref", "-d", "refs/gym/bases/pr-7")
        [row] = gym.run(self.tasks, ["fixer"], self.gym_dir)["runs"]
        self.assertEqual((row["mode"], row["verdict"]), ("hidden", "solved"))
        self.assertNotIn("hidden", json.loads(path.read_text()))

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
        self.assertEqual(gym.report(self.gym_dir)["incomplete"], ["pr-7:agy:hidden"])
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
        self.assertNotIn("fixtures", node["acceptance"])
        task = json.loads((self.tasks / "pr-7.json").read_text())
        fixtures = [{"path": "test/calc_test.py", "from_file": "/nowhere"}]
        node = gym.build_spec(task, {"agent": "codex"}, mode="hidden", fixtures=fixtures)["nodes"][0]
        self.assertEqual(node["acceptance"]["fixtures"], fixtures)
        for leak in ("calc_test", "test_add_negative", "test/"):
            self.assertNotIn(leak, node["task"])
        with self.assertRaises(ValueError):
            gym.run(self.tasks, ["fixer"], self.gym_dir, mode="secret")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = core.main(["--quiet", "gym", "report", str(self.gym_dir)])
        self.assertEqual(code, 0)
        self.assertIn("no completed gym runs", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class WithdrawUntrustedLabelTest(unittest.TestCase):
    def test_tampered_and_invalid_baseline_labels_are_retracted(self):
        from fusion_decisions import DecisionStore, read_jsonl
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labeled = {"status": "labeled", "decision_id": "d1", "answers": {"failed_task": "false"}}
            for verdict in ("tampered", "invalid_baseline"):
                with self.subTest(verdict=verdict):
                    out = gym.withdraw_untrusted_label(root, {"verdict": verdict, "key": "t:l", "gate_label": labeled})
                    self.assertEqual(out["status"], "retracted")
            kept = gym.withdraw_untrusted_label(root, {"verdict": "solved", "key": "t:l", "gate_label": labeled})
            self.assertEqual(kept, labeled)
            retractions = [e for e in read_jsonl(DecisionStore(root).path) if e.get("event") == "label"]
            self.assertEqual([(e["answers"], e["replace"], e["source"]) for e in retractions], [({}, True, "structural_gate")] * 2)


class SolvabilityAuditTest(unittest.TestCase):
    def test_negatives_count_only_for_tasks_some_lane_solved(self):
        from fusion_decisions import DecisionStore, read_jsonl, reviewed_labels
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = DecisionStore(root)
            def row(task, lane, verdict, decision):
                return {"event": "finished", "mode": "hidden", "completed": True, "key": f"{task}:{lane}:hidden",
                        "task": task, "lane": lane, "verdict": verdict,
                        "gate_label": {"status": "labeled", "decision_id": decision}}
            rows = [row("pr-1", "a", "solved", "d1"), row("pr-1", "b", "unsolved", "d2"),
                    row("pr-2", "a", "unsolved", "d3"), row("pr-2", "b", "unsolved", "d4")]
            (root / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            store.append("label", id="d1", answers={"failed_task": "false"}, verified=True, replace=False, source="structural_gate")
            for d in ("d2", "d3", "d4"):
                store.append("label", id=d, answers={"failed_task": "true"}, verified=True, replace=False, source="structural_gate")
            store.append("label", id="d4", answers={"failed_task": "true"}, verified=True, replace=True, source="lead_verdict")
            summary = gym.audit(root)
            self.assertEqual((summary["solved_tasks"], summary["unsolved_tasks"]), (["pr-1"], ["pr-2"]))
            answers, _ = reviewed_labels(read_jsonl(store.path))
            # Solvable task keeps its negative; an unsolved task's gate negative is withdrawn;
            # a lead's own verdict is never touched.
            self.assertEqual(answers["d2"], {"failed_task": "true"})
            self.assertFalse(answers.get("d3"))
            self.assertEqual(answers["d4"], {"failed_task": "true"})
            self.assertEqual(summary["retracted"], ["pr-2:a:hidden"])
            self.assertEqual(gym.audit(root)["retracted"], [], "idempotent")
            # Once a lane solves pr-2, the audited negative comes back.
            rows[2:] = [row("pr-2", "a", "unsolved", "d3"), row("pr-2", "c", "solved", "d5")]
            (root / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            self.assertEqual(gym.audit(root)["restored"], ["pr-2:a:hidden"])
            answers, _ = reviewed_labels(read_jsonl(store.path))
            self.assertEqual(answers["d3"], {"failed_task": "true"})
