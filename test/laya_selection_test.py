"""Dev-split epoch selection and the bounded proper-scoring term, without the Laya runtime."""
import argparse
import contextlib
import io
import math
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_laya
import fusion_training_loop as loop
from fusion_decisions import assign_splits, config_for, digest
from fusion_laya_objective import (MAX_EPOCHS, TRAINING_DEFAULTS, dev_guard, dev_score, fit_epochs, log_softmax, policy_gradient,
                                   policy_scale, proper_reward, selection_key, selection_record, sigma, training_options,
                                   training_plan)


def exported(groups=20, per_group=2, method="time"):
    first = {f"group-{n:02d}": 1000 * n for n in range(groups)}
    splits = assign_splits(first, method)
    return [{"id": f"{group}-{k}", "group": group, "group_first_ms": first[group], "split": splits[group], "kind": "acceptance",
             "labels": {"plausible": "true" if (n + k) % 2 else "false"}}
            for n, group in enumerate(first) for k in range(per_group)]


def ids(rows):
    return {row["id"] for row in rows}


def groups(rows):
    return {row["group"] for row in rows}


class DevSplitTest(unittest.TestCase):
    def test_held_out_rows_never_enter_fit_or_dev(self):
        for method in ("time", "group-hash"):
            with self.subTest(method):
                rows = exported(method=method)
                plan = training_plan(rows, method)
                held = [row for row in rows if row["split"] == "validation"]
                self.assertTrue(held and plan["fit"] and plan["dev"])
                self.assertEqual(ids(plan["held_out"]), ids(held))
                self.assertEqual(ids(held) & (ids(plan["fit"]) | ids(plan["dev"])), set())
                self.assertEqual(groups(held) & (groups(plan["fit"]) | groups(plan["dev"])), set())
                self.assertEqual(groups(plan["fit"]) & groups(plan["dev"]), set())
                self.assertEqual(ids(plan["fit"]) | ids(plan["dev"]), ids(r for r in rows if r["split"] == "train"))

    def test_time_dev_is_the_newest_training_groups_by_the_held_out_rule(self):
        rows = exported(20)
        plan = training_plan(rows, "time")
        train = sorted(groups(r for r in rows if r["split"] == "train"))
        self.assertEqual(len(train), 16)
        self.assertEqual(sorted(groups(plan["dev"])), train[-4:])
        self.assertLess(max(r["group_first_ms"] for r in plan["fit"]), min(r["group_first_ms"] for r in plan["dev"]))
        self.assertLess(max(r["group_first_ms"] for r in plan["dev"]), min(r["group_first_ms"] for r in plan["held_out"]))

    def test_group_hash_dev_takes_the_same_count_without_reusing_the_held_out_hash(self):
        rows = exported(20, method="group-hash")
        plan = training_plan(rows, "group-hash")
        count = len(groups(r for r in rows if r["split"] == "train"))
        self.assertEqual(len(groups(plan["dev"])), max(1, min(count - 2, max(2, math.ceil(count * 0.2)))))
        self.assertNotEqual(sorted(groups(plan["dev"])), sorted(groups(training_plan(rows, "time")["dev"])))
        self.assertEqual(training_plan(rows, "group-hash"), plan)

    def test_too_few_training_groups_leave_no_dev_split(self):
        rows = exported(2)
        plan = training_plan(rows, "time")
        self.assertEqual((len(groups(plan["fit"])), plan["dev"]), (1, []))
        with self.assertRaises(ValueError):
            training_plan(rows, "random")


class SelectionTest(unittest.TestCase):
    def run_scripted(self, dev, held_out, patience=3, max_epochs=10, plan=None, guard=None):
        plan = plan or training_plan(exported(), "time")
        held_ids, seen_fit, seen_dev, weights = ids(plan["held_out"]), [], [], {"epoch": 0}
        restored = []

        def run_epoch(epoch, fit):
            seen_fit.append(ids(fit))
            weights["epoch"] = epoch + 1

        def score(selected):
            seen_dev.append(ids(selected))
            return {"balanced_accuracy": dev[weights["epoch"] - 1], "accuracy": dev[weights["epoch"] - 1],
                    "held_out_would_be": held_out[weights["epoch"] - 1]}

        result = fit_epochs(plan, max_epochs, patience, run_epoch, score, lambda: dict(weights), restored.append, guard)
        self.assertTrue(all(not (s & held_ids) for s in seen_fit + seen_dev))
        self.assertTrue(all(s == ids(plan["fit"]) for s in seen_fit))
        self.assertTrue(all(s == ids(plan["dev"]) for s in seen_dev))
        return result, restored

    def test_the_best_dev_epoch_is_kept_even_when_held_out_would_pick_another(self):
        dev = [0.5, 0.6, 0.7, 0.9, 0.8, 0.8, 0.85, 0.95, 0.95, 0.95]
        held_out = [0.5, 0.95, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6]
        result, restored = self.run_scripted(dev, held_out)
        self.assertEqual((result["chosen_epoch"], result["epochs_run"], result["stopped_early"]), (4, 7, True))
        self.assertEqual(restored, [{"epoch": 4}])
        self.assertEqual(result["selected_on"], "dev")
        self.assertEqual([e["epoch"] for e in result["dev_by_epoch"]], list(range(1, 8)))

    def test_the_last_epoch_needs_no_restore(self):
        result, restored = self.run_scripted([0.5 + 0.05 * n for n in range(10)], [0.5] * 10)
        self.assertEqual((result["chosen_epoch"], result["epochs_run"], result["stopped_early"], restored), (10, 10, False, []))

    def test_without_dev_rows_every_epoch_runs(self):
        plan = training_plan(exported(2), "time")
        calls = []
        result = fit_epochs(plan, 4, 1, lambda epoch, fit: calls.append(epoch), lambda rows: self.fail("scored"),
                            lambda: self.fail("snapshot"), lambda saved: self.fail("restore"))
        self.assertEqual((calls, result["chosen_epoch"], result["selected_on"]), ([0, 1, 2, 3], 4, None))

    def test_dev_score_is_balanced_on_the_questions_that_vary(self):
        answers = [("a", 1, 1)] * 9 + [("a", 0, 1), ("b", 0, 0), ("b", 0, 0)]
        score = dev_score(answers)
        self.assertAlmostEqual(score["accuracy"], 11 / 12)
        self.assertEqual(score["balanced_accuracy"], 0.5)
        self.assertIsNone(dev_score([("b", 0, 0)])["balanced_accuracy"])
        self.assertGreater(selection_key({"balanced_accuracy": 0.9, "cross_entropy": 0.4}),
                           selection_key({"balanced_accuracy": 0.9, "cross_entropy": 0.5}))
        self.assertGreater(selection_key({"balanced_accuracy": None, "accuracy": 0.6}), selection_key({}))


def dev_plan(true, false, question="plausible", kind="acceptance"):
    rows = [{"id": f"dev-{n}", "group": f"dev-{n:03d}", "split": "train", "kind": kind,
             "labels": {question: "true" if n < true else "false"}} for n in range(true + false)]
    fit = [{"id": f"fit-{n}", "group": f"fit-{n:03d}", "split": "train", "kind": kind, "labels": {question: "true"}}
           for n in range(40)]
    return {"fit": fit, "dev": rows, "held_out": []}


def answers(rows):
    return [(f"{row['kind']}:{key}", label) for row in rows for key, label in row["labels"].items()]


def guard_for(plan, options=None):
    options = training_options(options or {})
    return dev_guard(plan["dev"], answers(plan["dev"]), options["min_dev_rows"], options["min_dev_per_class"])


class DevTooSmallTest(unittest.TestCase):
    FLAT = [0.6] * 10
    run_scripted = SelectionTest.run_scripted

    def test_a_seven_row_dev_trains_every_epoch_and_keeps_the_last(self):
        plan = dev_plan(5, 2)
        guard = guard_for(plan)
        self.assertEqual(guard, {"mode": "fixed", "reason": "dev too small", "dev_rows": 7,
                                 "dev_per_class": {"acceptance:plausible": {"false": 2, "true": 5}},
                                 "min_dev_rows": 30, "min_dev_per_class": 10})
        early, restored = self.run_scripted(self.FLAT, self.FLAT, plan=plan)
        self.assertEqual((early["epochs_run"], early["chosen_epoch"], restored), (4, 1, [{"epoch": 1}]))
        result, restored = self.run_scripted(self.FLAT, self.FLAT, plan=plan, guard=guard)
        self.assertEqual((result["epochs_run"], result["chosen_epoch"], result["stopped_early"], restored), (10, 10, False, []))
        self.assertEqual((result["mode"], result["reason"], result["dev_rows"], result["selected_on"]),
                         ("fixed", "dev too small", 7, None))
        self.assertEqual([e["epoch"] for e in result["dev_by_epoch"]], list(range(1, 11)))

    def test_a_large_balanced_dev_keeps_early_stopping(self):
        plan = dev_plan(20, 10)
        guard = guard_for(plan)
        self.assertEqual((guard["mode"], guard["reason"], guard["dev_rows"]), ("early_stopping", None, 30))
        dev = [0.5, 0.6, 0.7, 0.9, 0.8, 0.8, 0.85, 0.95, 0.95, 0.95]
        result, restored = self.run_scripted(dev, dev, plan=plan, guard=guard)
        self.assertEqual((result["chosen_epoch"], result["epochs_run"], result["stopped_early"], restored),
                         (4, 7, True, [{"epoch": 4}]))
        self.assertEqual((result["mode"], result["selected_on"]), ("early_stopping", "dev"))

    def test_one_class_under_the_minimum_trains_fixed(self):
        self.assertEqual(guard_for(dev_plan(81, 9))["mode"], "fixed")
        self.assertEqual(guard_for(dev_plan(80, 10))["mode"], "early_stopping")
        result, restored = self.run_scripted(self.FLAT, self.FLAT, plan=dev_plan(81, 9), guard=guard_for(dev_plan(81, 9)))
        self.assertEqual((result["epochs_run"], result["chosen_epoch"], restored), (10, 10, []))

    def test_per_class_counts_follow_the_questions_selection_uses(self):
        plan = dev_plan(20, 20)
        for n, row in enumerate(plan["dev"]):
            row["labels"]["blocking"] = "false"
            if n < 3:
                row["labels"]["scope"] = "narrow" if n else "wide"
        guard = guard_for(plan)
        self.assertEqual(guard["dev_per_class"], {"acceptance:plausible": {"false": 20, "true": 20},
                                                  "acceptance:scope": {"narrow": 2, "wide": 1}})
        self.assertEqual(guard["mode"], "fixed")
        for row in plan["dev"]:
            row["labels"].pop("scope", None)
        guard = guard_for(plan)
        self.assertEqual((guard["mode"], list(guard["dev_per_class"])), ("early_stopping", ["acceptance:plausible"]))
        constant = dev_plan(35, 0)
        self.assertEqual(guard_for(constant)["dev_per_class"], {"acceptance:plausible": {"true": 35}})
        self.assertEqual(guard_for(constant)["mode"], "early_stopping")

    def test_the_thresholds_come_from_config(self):
        plan = dev_plan(5, 2)
        self.assertEqual(guard_for(plan, {"min_dev_rows": 7, "min_dev_per_class": 2})["mode"], "early_stopping")
        self.assertEqual(guard_for(plan, {"min_dev_rows": 8, "min_dev_per_class": 2})["mode"], "fixed")
        self.assertEqual(guard_for(plan, {"min_dev_rows": 7, "min_dev_per_class": 3})["mode"], "fixed")
        self.assertEqual(guard_for(dev_plan(81, 9), {"min_dev_per_class": 9})["mode"], "early_stopping")
        options = config_for({"decisions": {"training": {"min_dev_rows": 12, "min_dev_per_class": 4}}})["training"]
        self.assertEqual((options["min_dev_rows"], options["min_dev_per_class"]), (12, 4))
        self.assertEqual((TRAINING_DEFAULTS["min_dev_rows"], TRAINING_DEFAULTS["min_dev_per_class"]), (30, 10))
        for value in ({"min_dev_rows": -1}, {"min_dev_rows": 2.0}, {"min_dev_per_class": True}, {"min_dev_per_class": "10"}):
            with self.subTest(value), self.assertRaises(ValueError):
                training_options(value)


class ProperScoringTermTest(unittest.TestCase):
    def test_the_advantage_is_not_normalized_by_its_own_spread(self):
        logits, target, noises = [0.3, -0.2], [0, 1], [[0.1, -0.1], [-0.2, 0.2], [0.05, -0.05], [0.0, 0.0]]
        rewards = [proper_reward([math.exp(v) for v in log_softmax([l + n for l, n in zip(logits, noise)])], target, False)
                   for noise in noises]
        mean = sum(rewards) / 4
        expected = [-0.1 * sum((r - mean) * noise[i] for r, noise in zip(rewards, noises)) / (0.2 ** 2 * 4) for i in range(2)]
        self.assertEqual(policy_gradient(logits, target, False, noises, 0.2, 0.1), expected)
        tiny = [[v / 100 for v in noise] for noise in noises]
        self.assertLess(max(map(abs, policy_gradient(logits, target, False, tiny, 0.2, 1.0))), 1e-3)

    def test_the_log_likelihood_scale_is_bounded_by_the_noise_floor(self):
        self.assertEqual(policy_scale(0.4), 0.4)
        self.assertEqual(policy_scale(0.01), 0.1)
        logits, target, noises = [0.0, 0.0], [1, 0], [[0.01, -0.01], [-0.01, 0.01]]
        self.assertEqual(policy_gradient(logits, target, False, noises, 0.01, 1.0),
                         policy_gradient(logits, target, False, noises, 0.1, 1.0))

    def test_the_gradient_pushes_toward_the_target(self):
        r = random.Random(3)
        noises = []
        for _ in range(4000):
            a, b = r.gauss(0, 0.4), r.gauss(0, 0.4)
            noises.append([(a - b) / 2, (b - a) / 2])
        gradient = policy_gradient([0.0, 0.0], [0, 1], False, noises, 0.4, 1.0)
        self.assertGreater(gradient[0], 0)
        self.assertLess(gradient[1], 0)


DIMENSIONS = 8


def separable(seed=20261007, train=32, held_out=8):
    r = random.Random(seed)
    direction = [r.gauss(0, 1) for _ in range(DIMENSIONS)]
    rows, counts = [], {"train": [0, 0], "validation": [0, 0]}
    while len(rows) < train + held_out:
        x = [r.gauss(0, 1) for _ in range(DIMENSIONS)]
        margin = sum(a * b for a, b in zip(direction, x))
        label, split = int(margin > 0), "train" if len(rows) < train else "validation"
        size = train if split == "train" else held_out
        if abs(margin) < 0.3 or counts[split][label] >= size // 2:
            continue
        counts[split][label] += 1
        n = len(rows)
        rows.append({"id": f"row-{n}", "group": f"g-{n:02d}", "group_first_ms": n, "split": split, "kind": "acceptance",
                     "labels": {"plausible": "true" if label else "false"}, "x": x + [1.0], "y": label})
    return rows


class TinyHead:
    """A linear two-option head trained like fusion_laya.train: per-row steps, soft
    cross-entropy plus the policy term on four projected noise samples, gradient
    clipping at 1.0, and a start that leans toward `false` like an untrained head."""

    def __init__(self, normalized=False, weight=TRAINING_DEFAULTS["proper_scoring_weight"], learning_rate=0.1, seed=7):
        self.w = [[0.0] * (DIMENSIONS + 1) for _ in range(2)]
        self.w[0][DIMENSIONS] = 0.5
        self.normalized, self.weight, self.learning_rate = normalized, weight, learning_rate
        self.noise, self.seed = random.Random(seed), seed

    def logits(self, x):
        return [sum(a * b for a, b in zip(row, x)) for row in self.w]

    def predict(self, x):
        values = self.logits(x)
        return int(values[1] > values[0])

    def policy(self, logits, target, noise):
        samples = []
        for _ in range(4):
            values = [self.noise.gauss(0, 1) * noise for _ in range(2)]
            samples.append([v - sum(values) / 2 for v in values])
        if not self.normalized:
            return policy_gradient(logits, target, False, samples, noise, self.weight)
        rewards = [proper_reward([math.exp(v) for v in log_softmax([l + n for l, n in zip(logits, sample)])], target, False)
                   for sample in samples]
        advantage = [r - sum(rewards) / 4 for r in rewards]
        spread = math.sqrt(sum(a * a for a in advantage) / 3) + 1e-6
        return [-self.weight * sum(a / spread * s[i] for a, s in zip(advantage, samples)) / (noise ** 2 * 4) for i in range(2)]

    def epoch(self, epoch, epochs, rows):
        noise = sigma(epoch, epochs)
        order = list(range(len(rows)))
        random.Random(self.seed + epoch).shuffle(order)
        for index in order:
            x, target = rows[index]["x"], [1 - rows[index]["y"], rows[index]["y"]]
            logits = self.logits(x)
            probabilities = [math.exp(v) for v in log_softmax(logits)]
            policy = self.policy(logits, target, noise)
            delta = [p - t + g for p, t, g in zip(probabilities, target, policy)]
            gradient = [[d * v for v in x] for d in delta]
            clip = min(1.0, 1.0 / (math.sqrt(sum(v * v for row in gradient for v in row)) + 1e-6))
            for k in range(2):
                for j in range(DIMENSIONS + 1):
                    self.w[k][j] -= self.learning_rate * clip * gradient[k][j]

    def score(self, rows):
        return {**dev_score([("acceptance:plausible", row["y"], self.predict(row["x"])) for row in rows]), "cross_entropy":
                sum(-log_softmax(self.logits(row["x"]))[row["y"]] for row in rows) / len(rows)}

    def snapshot(self):
        return [list(row) for row in self.w]

    def restore(self, saved):
        self.w = [list(row) for row in saved]


class LearnabilityTest(unittest.TestCase):
    def fit(self, head, epochs, patience):
        plan = training_plan(separable(), "time")
        selection = fit_epochs(plan, epochs, patience, lambda epoch, fit: head.epoch(epoch, epochs, fit),
                               head.score, head.snapshot, head.restore)
        return plan, selection, head.score(plan["fit"])

    def test_the_new_defaults_fit_a_tiny_separable_set(self):
        options = training_options({})
        plan, selection, fit = self.fit(TinyHead(), options["max_epochs"], options["patience"])
        self.assertEqual((len(plan["fit"]), len(plan["dev"]), len(plan["held_out"])), (25, 7, 8))
        self.assertGreaterEqual(fit["accuracy"], 0.95, selection)
        self.assertEqual(selection["selected_on"], "dev")
        self.assertGreaterEqual(selection["dev_by_epoch"][selection["chosen_epoch"] - 1]["balanced_accuracy"], 0.85)

    def test_the_tiny_set_trains_every_epoch_when_dev_is_too_small(self):
        options = training_options({})
        plan, head = training_plan(separable(), "time"), TinyHead()
        guard = guard_for(plan)
        selection = fit_epochs(plan, options["max_epochs"], options["patience"],
                               lambda epoch, fit: head.epoch(epoch, options["max_epochs"], fit),
                               head.score, head.snapshot, lambda saved: self.fail("restored"), guard)
        self.assertEqual((guard["mode"], guard["dev_rows"]), ("fixed", 7))
        self.assertEqual((selection["epochs_run"], selection["chosen_epoch"], selection["selected_on"]), (15, 15, None))
        self.assertGreaterEqual(head.score(plan["fit"])["accuracy"], 0.95)

    def test_the_old_configuration_stalls_on_the_same_set(self):
        _, _, one_epoch = self.fit(TinyHead(normalized=True, weight=1.0), 1, 1)
        self.assertLess(one_epoch["accuracy"], 0.95)
        plan = training_plan(separable(), "time")
        head = TinyHead(normalized=True, weight=1.0)
        for epoch in range(15):
            head.epoch(epoch, 15, plan["fit"])
        self.assertLess(head.score(plan["fit"])["accuracy"], 0.95)


class TrainingConfigRecordTest(unittest.TestCase):
    def test_new_defaults_and_their_bounds(self):
        options = config_for({})["training"]
        self.assertEqual((options["max_epochs"], options["patience"], options["proper_scoring_weight"]), (15, 3, 0.1))
        self.assertLessEqual(options["max_epochs"], MAX_EPOCHS)
        for value in ({"max_epochs": 0}, {"max_epochs": MAX_EPOCHS + 1}, {"max_epochs": 2.0}, {"patience": 0},
                      {"proper_scoring_weight": 0}, {"proper_scoring_weight": 1.5}, {"proper_scoring_weight": True}):
            with self.subTest(value), self.assertRaises(ValueError):
                training_options(value)
        self.assertEqual(training_options({"proper_scoring_weight": 0.25, "max_epochs": 20})["proper_scoring_weight"], 0.25)

    def argv(self, config, epochs=None):
        from fusion_decision_cli import run
        values = dict(decision_command="train", dataset="data.jsonl", output="candidate", model_path="", device="cpu",
                      epochs=epochs, learning_rate=0.0001, seed=7, kind=None, checkpoint=None)
        with tempfile.TemporaryDirectory() as directory, \
                patch("fusion_decision_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as execute:
            self.assertEqual(run(argparse.Namespace(**values), Path(directory), config), 0)
        argv = execute.call_args.args[0]
        return {flag: argv[argv.index(flag) + 1] for flag in ("--epochs", "--patience", "--proper-scoring-weight", "--split")}

    def test_the_cli_and_runtime_carry_the_dev_size_thresholds(self):
        from fusion_decision_cli import run
        sent = []
        for config in ({}, {"decisions": {"training": {"min_dev_rows": 12, "min_dev_per_class": 4}}}):
            values = dict(decision_command="train", dataset="data.jsonl", output="candidate", model_path="", device="cpu",
                          epochs=None, learning_rate=0.0001, seed=7, kind=None, checkpoint=None)
            with tempfile.TemporaryDirectory() as directory, \
                    patch("fusion_decision_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as execute:
                self.assertEqual(run(argparse.Namespace(**values), Path(directory), config), 0)
            argv = execute.call_args.args[0]
            sent.append((argv[argv.index("--min-dev-rows") + 1], argv[argv.index("--min-dev-per-class") + 1]))
        self.assertEqual(sent, [("30", "10"), ("12", "4")])
        captured = {}
        argv = ["fusion_laya.py", "train", "--dataset", "d", "--output", "o", "--min-dev-rows", "12", "--min-dev-per-class", "4"]
        with patch.dict("os.environ"), patch.object(sys, "argv", argv), \
                patch.object(fusion_laya, "train", side_effect=lambda args: captured.update(vars(args)) or {}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fusion_laya.main(), 0)
        self.assertEqual((captured["min_dev_rows"], captured["min_dev_per_class"]), (12, 4))

    def test_the_cli_sends_max_epochs_patience_weight_and_split(self):
        self.assertEqual(self.argv({}), {"--epochs": "15", "--patience": "3", "--proper-scoring-weight": "0.1", "--split": "time"})
        configured = {"decisions": {"split": "group-hash", "training": {"max_epochs": 8, "proper_scoring_weight": 0.2}}}
        self.assertEqual(self.argv(configured, epochs=4),
                         {"--epochs": "4", "--patience": "3", "--proper-scoring-weight": "0.2", "--split": "group-hash"})

    def test_the_runtime_parser_accepts_them(self):
        captured = {}
        argv = ["fusion_laya.py", "train", "--dataset", "d", "--output", "o", "--patience", "2",
                "--proper-scoring-weight", "0.2", "--split", "group-hash"]
        with patch.dict("os.environ"), patch.object(sys, "argv", argv), \
                patch.object(fusion_laya, "train", side_effect=lambda args: captured.update(vars(args)) or {}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fusion_laya.main(), 0)
        self.assertEqual((captured["epochs"], captured["patience"], captured["proper_scoring_weight"], captured["split"]),
                         (None, 2, 0.2, "group-hash"))

    def test_training_json_and_the_round_record_the_weight_epoch_and_dev_groups(self):
        rows = exported()
        plan = training_plan(rows, "time")
        selection = {"chosen_epoch": 4, "epochs_run": 7, "stopped_early": True, "selected_on": "dev",
                     "dev_by_epoch": [{"epoch": n, "balanced_accuracy": 0.5 + n / 20, "accuracy": 0.6} for n in range(1, 8)]}
        record = selection_record(plan, selection, training_options({}), 15)
        self.assertEqual((record["chosen_epoch"], record["max_epochs"], record["patience"], record["proper_scoring_weight"]),
                         (4, 15, 3, 0.1))
        self.assertEqual(record["dev_groups"], sorted(digest(g) for g in groups(plan["dev"])))
        self.assertEqual((record["dev_group_count"], record["dev_examples"], record["fit_groups"]), (4, 8, 12))
        self.assertEqual(set(record["dev_groups"]) & {digest(g) for g in groups(plan["held_out"])}, set())
        self.assertIsNone(selection_record(plan, selection, training_options({"objective": "soft_ce"}), 15)["proper_scoring_weight"])
        train = {"selection": record, "train_accuracy": 0.97, "objective": training_options({})}
        summary = loop.training_summary(train)
        self.assertEqual((summary["chosen_epoch"], summary["proper_scoring_weight"], summary["dev_group_count"],
                          summary["selected_on"], summary["train_accuracy"], summary["objective"]),
                         (4, 0.1, 4, "dev", 0.97, "soft_ce+proper_scoring"))
        self.assertAlmostEqual(summary["dev_balanced_accuracy"], 0.7)
        self.assertEqual(loop.training_summary({})["chosen_epoch"], None)

    def test_training_json_and_the_round_record_a_fixed_selection(self):
        plan = dev_plan(5, 2)
        guard = guard_for(plan)
        selection = fit_epochs(plan, 15, 3, lambda epoch, fit: None, lambda rows: {"balanced_accuracy": 0.6, "accuracy": 0.7},
                               lambda: None, lambda saved: self.fail("restored"), guard)
        record = selection_record(plan, selection, training_options({}), 15)
        self.assertEqual((record["mode"], record["reason"], record["dev_rows"], record["dev_per_class"], record["chosen_epoch"]),
                         ("fixed", "dev too small", 7, {"acceptance:plausible": {"false": 2, "true": 5}}, 15))
        summary = loop.training_summary({"selection": record})
        self.assertEqual((summary["selection_mode"], summary["selection_reason"], summary["dev_rows"], summary["dev_per_class"],
                          summary["selected_on"], summary["chosen_epoch"], summary["dev_balanced_accuracy"]),
                         ("fixed", "dev too small", 7, {"acceptance:plausible": {"false": 2, "true": 5}}, None, 15, 0.6))
        import fusion_learn_cli
        shown = fusion_learn_cli.measured_round({"id": "r1", "proof": {"training": summary}})["training"]
        self.assertEqual((shown["selection_mode"], shown["selection_reason"], shown["dev_rows"]), ("fixed", "dev too small", 7))


if __name__ == "__main__":
    unittest.main()
