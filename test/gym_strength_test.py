"""Bradley-Terry lane strength in the gym report: pairs of lanes on the same
task, a recovered order on a synthetic fixture, ties and uninformative tasks
ignored, a finite undefeated lane, reproducible bootstrap intervals, train and
held-out fitted apart, and a single lane reported as "no pairs"."""
import io
import json
import math
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as core
import fusion_gym as gym
from fusion_gym_strength import strengths
from gym_split_test import TASKS, row
from gym_test import Isolated

# Log-odds skill per lane: on a task of difficulty d a lane succeeds with
# probability sigmoid(skill - d), so a decisive pair goes to lane i over j with
# probability sigmoid(skill_i - skill_j) whatever d is.
SKILL = {"high": 1.0, "mid": 0.0, "low": -1.0}


def sigmoid(value):
    return 1 / (1 + math.exp(-value))


def synthetic(count, seed=1):
    rng = random.Random(seed)
    tasks = {}
    for index in range(count):
        difficulty = rng.uniform(-2, 2)
        tasks[f"t{index}"] = {"hidden": {lane: rng.random() < sigmoid(skill - difficulty) for lane, skill in SKILL.items()}}
    return tasks


def width(cell):
    return cell["strength_ci95"][1] - cell["strength_ci95"][0]


class StrengthTest(unittest.TestCase):
    def test_a_synthetic_fixture_recovers_the_order_and_probabilities(self):
        section = strengths(synthetic(600))
        lanes = section["lanes"]
        self.assertEqual(sorted(lanes, key=lambda lane: -lanes[lane]["strength"]), ["high", "mid", "low"])
        self.assertAlmostEqual(sum(cell["strength"] for cell in lanes.values()), 0, places=2)
        for lane, skill in SKILL.items():
            self.assertAlmostEqual(lanes[lane]["strength"], skill, delta=.15)
            # The median lane is mid (skill 0).
            self.assertAlmostEqual(lanes[lane]["p_beats_median"], sigmoid(skill), delta=.05)
            low, high = lanes[lane]["strength_ci95"]
            self.assertTrue(low <= lanes[lane]["strength"] <= high)
        self.assertEqual(lanes["mid"]["p_beats_median"], .5)
        self.assertEqual((section["resamples"], section["seed"]), (500, 57))

    def test_ties_and_all_pass_or_all_fail_tasks_contribute_nothing(self):
        tasks = synthetic(60)
        base = strengths(tasks)
        padded = dict(tasks)
        for index in range(30):
            padded[f"pass{index}"] = {"hidden": {lane: True for lane in SKILL}}
            padded[f"fail{index}"] = {"hidden": {lane: False for lane in SKILL}}
            padded[f"one{index}"] = {"hidden": {"high": False}}
        value = strengths(padded)
        self.assertEqual(value["lanes"], base["lanes"])
        self.assertEqual((value["pairs"], value["tasks"]), (base["pairs"], base["tasks"]))
        self.assertEqual(value["skipped_tasks"], base["skipped_tasks"] + 90)
        # Two successes or two failures are a tie: only high beating low is a pair.
        tied = strengths({"t": {"hidden": {"high": True, "mid": True}, "visible": {"mid": False, "low": False}},
                          "u": {"hidden": {"high": True, "low": False}}})
        self.assertEqual((tied["pairs"], tied["tasks"], tied["skipped_tasks"]), (1, 1, 1))
        self.assertIsNone(tied["lanes"]["mid"]["strength"], "mid only tied: no pairs")

    def test_an_undefeated_lane_gets_a_finite_strength(self):
        tasks = {f"t{n}": {"hidden": {"high": True, "mid": n % 2 == 0, "low": False}} for n in range(20)}
        lanes = strengths(tasks)["lanes"]
        for cell in lanes.values():
            self.assertTrue(math.isfinite(cell["strength"]))
            self.assertTrue(all(math.isfinite(bound) for bound in cell["strength_ci95"]))
        self.assertGreater(lanes["high"]["strength"], lanes["mid"]["strength"])
        self.assertGreater(lanes["mid"]["strength"], lanes["low"]["strength"])
        self.assertLess(lanes["high"]["p_beats_median"], 1)

    def test_bootstrap_intervals_are_reproducible_and_widen_as_tasks_are_removed(self):
        tasks = synthetic(400)
        first, second = strengths(tasks), strengths(tasks)
        self.assertEqual(first, second)
        self.assertNotEqual(strengths(tasks, seed=58)["lanes"]["high"]["strength_ci95"],
                            first["lanes"]["high"]["strength_ci95"])
        fewer = strengths({task: tasks[task] for task in list(tasks)[:100]})
        fewest = strengths({task: tasks[task] for task in list(tasks)[:25]})
        for lane in SKILL:
            self.assertLess(width(first["lanes"][lane]), width(fewer["lanes"][lane]))
            self.assertLess(width(fewer["lanes"][lane]), width(fewest["lanes"][lane]))

    def test_a_single_lane_gets_no_pairs(self):
        value = strengths({"t": {"hidden": {"solo": True}}, "u": {"hidden": {"solo": False}}})
        self.assertEqual(value["lanes"], {"solo": {"strength": None, "strength_ci95": None, "p_beats_median": None,
                                                   "p_beats_median_ci95": None, "pairs": 0, "tasks": 0}})
        self.assertEqual((value["pairs"], value["tasks"], value["skipped_tasks"]), (0, 0, 2))


class ReportTest(Isolated):
    def setUp(self):
        super().setUp()
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        for task in TASKS:
            (self.tasks / f"{task}.json").write_text(json.dumps({"schema": gym.TASK_SCHEMA, "id": task, "pr": int(task[3:])}))
        self.gym_dir = self.root / "gym"

    def test_train_and_held_out_are_fitted_and_reported_apart(self):
        heldout = gym.freeze_split(self.gym_dir, self.tasks, .25, "fixed")["heldout"]
        train = [task for task in TASKS if task not in heldout]
        # agy wins every train task and loses every held-out one; pooled, they would cancel.
        rows = [row(task, "agy", "solved") for task in train] + [row(task, "codex", "unsolved") for task in train]
        rows += [row(task, "agy", "unsolved") for task in heldout] + [row(task, "codex", "solved") for task in heldout]
        rows += [row(task, "agy", "missed", kind="localize") for task in train]
        (self.gym_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        value = gym.report(self.gym_dir, bradley_terry_fit=True)["bradley_terry"]
        self.assertEqual(sorted(value), ["heldout", "train"])
        self.assertEqual(list(value["train"]), ["fix", "localize"])
        self.assertEqual((value["train"]["fix"]["tasks"], value["heldout"]["fix"]["tasks"]), (len(train), len(heldout)))
        self.assertGreater(value["train"]["fix"]["lanes"]["agy"]["strength"], 0)
        self.assertLess(value["heldout"]["fix"]["lanes"]["agy"]["strength"], 0)
        self.assertIsNone(value["train"]["localize"]["lanes"]["agy"]["strength"])
        self.assertEqual(list(gym.report(self.gym_dir, "heldout", bradley_terry_fit=True)["bradley_terry"]), ["heldout"])
        self.assertNotIn("bradley_terry", gym.report(self.gym_dir), "only on request")
        text = gym.table(gym.report(self.gym_dir, bradley_terry_fit=True))
        self.assertIn("== Bradley-Terry strength per kind", text)
        self.assertRegex(text, r"\ntrain    fix        agy +\+\d\.\d\d \[")
        self.assertRegex(text, r"\nheldout  fix        agy +-\d\.\d\d \[")
        self.assertIn("train    localize   agy                    no pairs", text)
        out = io.StringIO()
        args = core.build_parser().parse_args(["gym", "report", str(self.gym_dir), "--bradley-terry", "--split", "train"])
        self.assertEqual(gym.command(args, self.root, as_json=True, out=out), 0)
        self.assertEqual(json.loads(out.getvalue())["bradley_terry"]["train"]["fix"], value["train"]["fix"])


class NoSplitTest(Isolated):
    def test_without_a_split_and_with_a_single_lane_the_lane_has_no_pairs(self):
        gym_dir = self.root / "gym"
        gym_dir.mkdir()
        rows = [row("pr-1", "agy", "solved"), row("pr-2", "agy", "unsolved"), row("pr-3", "agy", "tampered")]
        (gym_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        value = gym.report(gym_dir, bradley_terry_fit=True)
        self.assertEqual(value["bradley_terry"]["heldout"], {})
        section = value["bradley_terry"]["train"]["fix"]
        self.assertIsNone(section["lanes"]["agy"]["strength"])
        self.assertEqual((section["tasks"], section["skipped_tasks"]), (0, 2))
        self.assertIn("train    fix        agy                    no pairs", gym.table(value))
        out = io.StringIO()
        args = core.build_parser().parse_args(["gym", "report", str(gym_dir), "--bradley-terry"])
        self.assertEqual(gym.command(args, self.root, out=out), 0)
        self.assertIn("no pairs", out.getvalue())

    def test_an_empty_gym_reports_no_graded_runs(self):
        value = gym.report(self.root, bradley_terry_fit=True)
        self.assertEqual(value["bradley_terry"], {"train": {}, "heldout": {}})
        self.assertIn("no graded runs", gym.table(value))


if __name__ == "__main__":
    unittest.main()
