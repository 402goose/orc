"""Wilson 95% intervals on lane comparisons (routing-report, route --explain,
gym priors and report) and the frozen held-out gym split: deterministic
assignment, never overwritten, extended without moving a task, excluded from
priors and gym_grade labels, measured on its own, edits flagged."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as core
import fusion_gym as gym
import fusion_policy as policy
from fusion_policy import acceptance_ci95, routing_report, wilson_interval
from gym_test import Isolated

AGY = {"agent": "agy"}
TASKS = [f"pr-{n}" for n in range(1, 21)]


def row(task, lane, verdict, kind="fix", spec=AGY):
    suffix = "" if kind == "fix" else f":{kind}"
    return {"event": "finished", "key": f"{task}:{lane}:hidden{suffix}", "task": task, "lane": lane, "lane_spec": spec,
            "kind": kind, "mode": "hidden", "verdict": verdict, "completed": True, "cost_usd": 0.0, "duration_ms": 1000,
            "f2p_passed": verdict == "solved", "p2p_regressed": False, "tampered": False}


def rule(salt, task, fraction):
    return int(hashlib.sha256((salt + task).encode()).hexdigest()[:16], 16) / 2 ** 64 < fraction


class WilsonTest(unittest.TestCase):
    def test_reference_values_to_three_places(self):
        for (successes, n), expected in (((0, 10), (0, .278)), ((5, 10), (.237, .763)), ((10, 10), (.722, 1))):
            low, high = wilson_interval(successes, n)
            self.assertEqual((round(low, 3), round(high, 3)), expected)
            self.assertEqual(acceptance_ci95(successes, n), list(expected))
        self.assertIsNone(wilson_interval(0, 0))
        self.assertIsNone(acceptance_ci95(0, 0))
        self.assertEqual(policy.format_rate(5, 10), "0.50 [0.24, 0.76]")


class RoutingIntervalTest(unittest.TestCase):
    def test_routing_report_carries_the_interval_of_accepted_over_chosen(self):
        events = []
        for index, accepted in enumerate((True, True, False)):
            events += [{"event": "routing_log", "task_id": f"t{index}", "chosen": "a",
                        "candidates": [{"key": "a", "propensity": .5}, {"key": "b", "propensity": .5}]},
                       {"event": "outcome", "task_id": f"t{index}", "accepted": accepted}]
        lanes = {lane["key"]: lane for lane in routing_report(events)["lanes"]}
        self.assertEqual((lanes["a"]["accepted"], lanes["a"]["chosen"]), (2, 3))
        self.assertEqual(lanes["a"]["acceptance_ci95"], acceptance_ci95(2, 3))
        self.assertIsNone(lanes["b"]["acceptance_ci95"], "never chosen: no interval")

    def test_route_explain_reads_local_checked_runs_before_prior_pooling(self):
        temp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off", "priors": False},
                                                 "routes": {"A": {"agent": "claude", "model": "model-a"}}})
        # Pooled with a prior, acceptance_rate is 0.75 over 8; the interval reads only the 5 of 10 local runs.
        candidate = {"key": "A", "agent": "claude", "route": "A", "model": "model-a", "checked_runs": 8,
                     "acceptance_rate": .75, "checked_runs_local": 10, "acceptance_rate_local": .5}
        ranked = {"candidates": [candidate], "minimum": None, "explore": None, "trial": None, "sampled": None,
                  "needs_unmet": False, "sampling": False, "rework": {}, "excluded_candidates": []}
        outputs = []
        for flags in (["--json"], []):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.dict(os.environ, {"FUSION_TELEMETRY": "0", "FUSION_PROGRESS": "0"}), \
                    patch.object(core, "load_config", return_value=(config, None)), \
                    patch.object(policy, "rank_automatic", return_value=ranked):
                self.assertEqual(core.main(["--workspace", str(root), *flags, "route", "--explain"]), 0)
            outputs.append(out.getvalue())
        [explained] = json.loads(outputs[0])["candidates"]
        self.assertEqual(explained["acceptance_ci95"], [.237, .763])
        self.assertIn("accepted=0.50 [0.24, 0.76]", outputs[1])


class SplitTest(Isolated):
    def setUp(self):
        super().setUp()
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        for task in TASKS:
            self.write_task(task)
        self.gym_dir = self.root / "gym"

    def write_task(self, task, problem="a problem"):
        (self.tasks / f"{task}.json").write_text(json.dumps({"schema": gym.TASK_SCHEMA, "id": task,
                                                             "pr": int(task[3:]), "problem": problem}))

    def freeze(self, gym_dir=None, salt="fixed", fraction=0.25):
        return gym.freeze_split(gym_dir or self.gym_dir, self.tasks, fraction, salt)

    def test_freeze_is_deterministic_records_its_rule_and_refuses_to_overwrite(self):
        first, second = self.freeze(), self.freeze(self.root / "other")
        self.assertEqual((first["heldout"], first["task_sha256"]), (second["heldout"], second["task_sha256"]))
        self.assertEqual(first["heldout"], sorted(task for task in TASKS if rule("fixed", task, .25)))
        self.assertTrue(first["heldout"] and len(first["heldout"]) < len(TASKS))
        self.assertEqual(sorted(first["task_sha256"]), sorted(TASKS))
        stored = json.loads((self.gym_dir / "split.json").read_text())
        self.assertEqual({k: stored[k] for k in ("schema", "fraction", "salt", "rule")},
                         {"schema": "fusion.gym_split.v1", "fraction": .25, "salt": "fixed",
                          "rule": "sha256(salt + task_id) < fraction"})
        before = (self.gym_dir / "split.json").read_text()
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            self.freeze(salt="another")
        self.assertEqual((self.gym_dir / "split.json").read_text(), before)
        self.assertNotEqual(self.freeze(self.root / "salted", salt="another")["heldout"], first["heldout"])

    def test_extend_records_new_tasks_by_the_same_rule_without_moving_any(self):
        frozen = self.freeze()
        added = [f"pr-{n}" for n in range(21, 61)]
        for task in added:
            self.write_task(task)
        # Before --extend records it, a new task is already assigned by the rule.
        split = gym.read_split(self.gym_dir)
        self.assertEqual([t for t in added if gym.is_heldout(split, t)], [t for t in added if rule("fixed", t, .25)])
        self.write_task(frozen["heldout"][0], "an edited problem")
        result = gym.extend_split(self.gym_dir, self.tasks)
        self.assertEqual(result["added"], added)
        self.assertEqual(result["added_heldout"], [t for t in added if rule("fixed", t, .25)])
        self.assertEqual(result["heldout_task_changed"], [frozen["heldout"][0]])
        split = gym.read_split(self.gym_dir)
        self.assertEqual([t for t in split["heldout"] if t in TASKS], frozen["heldout"])
        self.assertEqual({t: split["task_sha256"][t] for t in TASKS}, frozen["task_sha256"], "recorded digests never change")
        self.assertEqual(sorted(split["task_sha256"]), sorted(TASKS + added))
        self.assertEqual(gym.extend_split(self.gym_dir, self.tasks)["added"], [])

    def test_cli_freezes_extends_and_rejects_a_new_salt_on_extend(self):
        parser = core.build_parser()
        out = io.StringIO()
        args = parser.parse_args(["gym", "split", str(self.gym_dir), "--tasks", str(self.tasks), "--freeze", "--salt", "fixed"])
        self.assertEqual(gym.command(args, self.root, out=out), 0)
        self.assertIn("froze 20 tasks", out.getvalue())
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            gym.command(args, self.root, out=io.StringIO())
        args = parser.parse_args(["gym", "split", str(self.gym_dir), "--tasks", str(self.tasks), "--extend", "--salt", "x"])
        with self.assertRaisesRegex(ValueError, "keeps the frozen salt"):
            gym.command(args, self.root, out=io.StringIO())

    def rows(self):
        heldout = self.freeze()["heldout"][0]
        train = next(task for task in TASKS if not rule("fixed", task, .25))
        rows = [row(heldout, "agy", "solved"), row(heldout, "codex", "unsolved", spec={"agent": "codex"}),
                row(train, "agy", "solved"), row(train, "codex", "solved", spec={"agent": "codex"})]
        (self.gym_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        return heldout, train

    def test_priors_exclude_held_out_rows_with_the_reason_counted(self):
        self.rows()
        value = gym.lane_priors(self.gym_dir, include_unknown=True)
        self.assertEqual(value["excluded"], {"heldout": 2})
        self.assertEqual({key: (entry["write"]["successes"], entry["write"]["attempts"]) for key, entry in value["priors"].items()},
                         {"agy": (1, 1), "codex": (1, 1)})
        text = gym.priors_table(value)
        self.assertIn("1/1    1.00 [0.21, 1.00]", text)
        self.assertIn("excluded: heldout 2", text)
        os.remove(self.gym_dir / "split.json")
        self.assertEqual(gym.lane_priors(self.gym_dir, include_unknown=True)["excluded"], {}, "no split: nothing held out")

    def test_report_measures_each_split_and_flags_an_edited_held_out_task(self):
        heldout, train = self.rows()
        held = gym.report(self.gym_dir, "heldout")
        self.assertEqual(list(held["modes"]["hidden"]["tasks"]), [heldout])
        self.assertEqual(list(held["acceptance"]), ["heldout"])
        self.assertEqual(held["acceptance"]["heldout"]["codex"]["write"],
                         {"attempts": 1, "successes": 0, "rate": 0.0, "ci95": acceptance_ci95(0, 1)})
        self.assertEqual(list(gym.report(self.gym_dir, "train")["modes"]["hidden"]["tasks"]), [train])
        value = gym.report(self.gym_dir)
        self.assertEqual(sorted(value["modes"]["hidden"]["tasks"]), sorted([heldout, train]))
        self.assertEqual(sorted(value["acceptance"]), ["heldout", "train"])
        self.assertEqual(value["acceptance"]["train"]["agy"]["write"]["ci95"], acceptance_ci95(1, 1))
        self.assertEqual(value["heldout_task_changed"], [])
        text = gym.table(value)
        self.assertIn("== acceptance per lane and work class", text)
        self.assertIn("heldout  agy                    write           1/1    1.00 [0.21, 1.00]", text)
        self.write_task(heldout, "an edited problem")
        changed = gym.report(self.gym_dir, tasks=self.tasks)
        self.assertEqual(changed["heldout_task_changed"], [heldout])
        self.assertEqual(changed["acceptance"]["heldout"], {}, "an edited held-out task is flagged, not re-scored")
        self.assertIn(f"heldout_task_changed (edited since the split recorded them; not scored): {heldout}", gym.table(changed))
        out = io.StringIO()
        args = core.build_parser().parse_args(["gym", "report", str(self.gym_dir), "--split", "heldout", "--tasks", str(self.tasks)])
        self.assertEqual(gym.command(args, self.root, as_json=True, out=out), 0)
        self.assertEqual(json.loads(out.getvalue())["heldout_task_changed"], [heldout])

    def test_without_a_split_the_report_has_no_held_out_side(self):
        self.gym_dir.mkdir()
        (self.gym_dir / "results.jsonl").write_text(json.dumps(row("pr-1", "agy", "solved")) + "\n")
        value = gym.report(self.gym_dir)
        self.assertNotIn("heldout_task_changed", value)
        self.assertEqual(value["acceptance"]["heldout"], {})
        self.assertEqual(value["acceptance"]["train"]["agy"]["write"]["successes"], 1)


if __name__ == "__main__":
    unittest.main()
