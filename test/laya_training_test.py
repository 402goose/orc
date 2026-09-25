import argparse
import contextlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_laya
from fusion_decisions import (ACCEPTANCE_QUESTIONS, CHECKPOINTS, DecisionEngine, acceptance_state, checkpoint_limits,
                              config_for, estimated_tokens, exceeds_token_budget, state_tokens)
from fusion_laya_objective import (TRAINING_DEFAULTS, class_weights, item_weights, log_softmax, proper_reward, sigma,
                                   soft_cross_entropy, target_distribution, training_options)


def softmax(logits):
    return [math.exp(v) for v in log_softmax(logits)]


class ObjectiveMathTest(unittest.TestCase):
    def test_soft_cross_entropy_of_a_one_hot_target_is_hard_cross_entropy(self):
        logits = [2.0, -1.0, 0.5]
        self.assertAlmostEqual(soft_cross_entropy(logits, [0, 1, 0]), -math.log(softmax(logits)[1]))
        self.assertAlmostEqual(sum(softmax(logits)), 1.0)

    def test_soft_cross_entropy_is_smallest_when_the_prediction_matches_a_soft_target(self):
        target = [0.7, 0.3]
        matched = [math.log(0.7), math.log(0.3)]
        entropy = -sum(t * math.log(t) for t in target)
        self.assertAlmostEqual(soft_cross_entropy(matched, target), entropy)
        for logits in ([0, 0], [2, 0], [0, 2], [5, -5]):
            self.assertGreater(soft_cross_entropy(logits, target), entropy)

    def test_proper_reward_matches_hand_values(self):
        expected = math.log(0.5) + 0.75 * 0.5 / math.sqrt(0.5)
        self.assertAlmostEqual(proper_reward([0.5, 0.5], [1, 0], False), expected)
        # Ranked probability score on a three-level score question: cdfs (.2,.5,1) vs (0,1,1).
        plain = math.log(0.3) + 0.75 * 0.3 / math.sqrt(0.04 + 0.09 + 0.25)
        self.assertAlmostEqual(proper_reward([0.2, 0.3, 0.5], [0, 1, 0], True), plain - (0.04 + 0.25) / 2)
        self.assertAlmostEqual(proper_reward([1e-30, 1.0], [1, 0], False), -9.21, places=6)

    def test_expected_reward_peaks_at_the_target_distribution(self):
        target = [0.3, 0.7]
        best = max((i / 100 for i in range(1, 100)), key=lambda p: proper_reward([1 - p, p], target, False))
        self.assertAlmostEqual(best, 0.7, delta=0.02)
        levels = [0.1, 0.6, 0.3]
        grid = [[a / 20, b / 20, 1 - a / 20 - b / 20] for a in range(1, 19) for b in range(1, 19) if a + b < 20]
        best = max(grid, key=lambda q: proper_reward(q, levels, True))
        self.assertTrue(all(abs(x - y) <= 0.1 for x, y in zip(best, levels)), best)

    def test_noise_follows_upstreams_schedule(self):
        self.assertEqual(sigma(0, 1), 0.4)
        self.assertAlmostEqual(sigma(0, 4), 0.4)
        self.assertAlmostEqual(sigma(3, 4), 0.1)
        self.assertAlmostEqual(sigma(1, 3), 0.25)


class TargetTest(unittest.TestCase):
    labels = ["false", "true"]

    def test_a_hard_label_is_one_hot_unless_smoothed(self):
        row = {"labels": {"plausible": "true"}}
        self.assertEqual(target_distribution(row, "plausible", self.labels), [0, 1])
        smoothed = target_distribution(row, "plausible", self.labels, 0.1)
        self.assertAlmostEqual(smoothed[0], 0.05)
        self.assertAlmostEqual(smoothed[1], 0.95)

    def test_a_rows_own_target_distribution_wins_and_is_normalized(self):
        row = {"labels": {"plausible": "true"}, "targets": {"plausible": {"false": 0.33, "true": 0.66}}}
        target = target_distribution(row, "plausible", self.labels, 0.2)
        self.assertAlmostEqual(sum(target), 1.0)
        self.assertAlmostEqual(target[1], 0.66 / 0.99)

    def test_invalid_target_distributions_are_refused(self):
        for given in ({"true": 1.0}, {"false": -0.1, "true": 1.1}, {"false": 0.2, "true": 0.2},
                      {"false": float("nan"), "true": 1}, "true"):
            with self.subTest(given), self.assertRaises(ValueError):
                target_distribution({"labels": {"q": "true"}, "targets": {"q": given}}, "q", self.labels)


class ClassWeightTest(unittest.TestCase):
    def items(self, positives, negatives, schema="plausible"):
        return [(schema, [0, 1])] * positives + [(schema, [1, 0])] * negatives

    def test_an_all_positive_split_trains_unweighted(self):
        self.assertEqual(class_weights(self.items(22, 0), 4), {("plausible", 1): 1.0})
        weights, _ = item_weights(self.items(22, 0), [1.0] * 22, 4)
        self.assertEqual(weights, [1.0] * 22)

    def test_a_rare_class_is_upweighted_up_to_the_cap(self):
        self.assertEqual(class_weights(self.items(30, 10), 4), {("plausible", 1): 1.0, ("plausible", 0): 3.0})
        self.assertEqual(class_weights(self.items(30, 5), 4)[("plausible", 0)], 4.0)

    def test_each_question_is_balanced_on_its_own_classes(self):
        items = self.items(4, 4, "a") + self.items(8, 2, "b")
        weights = class_weights(items, 4)
        self.assertEqual((weights[("a", 0)], weights[("b", 0)], weights[("b", 1)]), (1.0, 4.0, 1.0))

    def test_item_weights_keep_mean_one_and_multiply_row_weights(self):
        items = self.items(6, 2)
        weights, per_class = item_weights(items, [1.0] * 7 + [2.0], 4)
        self.assertAlmostEqual(sum(weights) / len(weights), 1.0)
        self.assertAlmostEqual(weights[-1] / weights[-2], 2.0)
        self.assertAlmostEqual(weights[-2] / weights[0], 3.0)
        unbalanced, per_class = item_weights(items, [1.0] * 8, 4, balance=False)
        self.assertEqual((unbalanced, per_class), ([1.0] * 8, {}))

    def test_a_soft_targets_class_is_its_argmax(self):
        self.assertEqual(set(class_weights([("q", [0.4, 0.6]), ("q", [0.6, 0.4])], 4).values()), {1.0})


class TrainingConfigTest(unittest.TestCase):
    def test_defaults_follow_upstream(self):
        options = config_for({})["training"]
        self.assertEqual(options, TRAINING_DEFAULTS)
        self.assertEqual((options["objective"], options["unfreeze_encoder"], options["encoder_learning_rate"]),
                         ("soft_ce+proper_scoring", False, 2.5e-5))

    def test_invalid_training_settings_are_refused(self):
        for value in ({"objective": "hinge"}, {"unfreeze_encoder": "yes"}, {"encoder_learning_rate": 0},
                      {"encoder_learning_rate": 0.1}, {"label_smoothing": 1}, {"max_class_weight": 0.5},
                      {"class_balance": 1}, {"epochs": 3}, []):
            with self.subTest(value), self.assertRaises(ValueError):
                training_options(value)
        with self.assertRaises(ValueError):
            config_for({"decisions": {"training": {"objective": "hinge"}}})

    def test_checkpoints_map_decision_kinds_to_names_or_directories(self):
        options = config_for({"decisions": {"checkpoints": {"acceptance": "typed-decisions", "intake": " ./ckpt "}}})
        self.assertEqual(options["checkpoints"], {"acceptance": "typed-decisions", "intake": "./ckpt"})
        for value in ({"planning": "english"}, {"acceptance": ""}, {"acceptance": 3}, ["english"]):
            with self.subTest(value), self.assertRaises(ValueError):
                config_for({"decisions": {"checkpoints": value}})


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def checkpoint_dir(self, **config):
        path = self.workspace / "candidate"
        path.mkdir(exist_ok=True)
        (path / "rl_agent_config.json").write_text(json.dumps(config))
        return path

    def test_the_budget_derives_from_the_checkpoints_limits(self):
        self.assertEqual(state_tokens("acceptance"), 512 - 40)
        self.assertEqual(state_tokens("acceptance", "english"), 472)
        self.assertEqual(state_tokens("acceptance", "typed-decisions"), 1024 - 40)
        self.assertEqual(state_tokens("intake", "typed-decisions"), 1024 - 256 - 3)
        self.assertEqual(state_tokens("intake"), 512 - 195)

    def test_an_unmeasured_tokenizer_never_gets_more_than_the_english_budget(self):
        self.assertEqual(CHECKPOINTS["multilingual"]["max_len"], 1024)
        self.assertEqual(state_tokens("acceptance", "multilingual"), 472)

    def test_a_checkpoint_directory_is_read_from_its_config(self):
        path = self.checkpoint_dir(encoder="answerdotai/ModernBERT-large", max_len=1024, head_max_len=256)
        self.assertEqual(checkpoint_limits(str(path))["max_len"], 1024)
        self.assertEqual(state_tokens("acceptance", str(path)), 984)
        self.assertEqual(state_tokens("acceptance", str(self.workspace / "missing")), 472)
        broken = self.checkpoint_dir(encoder="answerdotai/ModernBERT-large", max_len=10, head_max_len=256)
        self.assertEqual(state_tokens("acceptance", str(broken)), 472)

    def test_the_engine_budgets_each_kind_on_its_own_checkpoint(self):
        engine = DecisionEngine(self.workspace, {"decisions": {"checkpoints": {"acceptance": "typed-decisions"}}})
        self.assertEqual((engine.state_tokens("acceptance"), engine.state_tokens("intake")), (984, 317))
        path = self.checkpoint_dir(encoder="answerdotai/ModernBERT-large", max_len=1024, head_max_len=256)
        engine = DecisionEngine(self.workspace, {"decisions": {"model_path": "candidate"}})
        self.assertEqual(engine.checkpoint("acceptance"), str(path.resolve()))
        self.assertEqual(engine.state_tokens("acceptance"), 984)
        engine = DecisionEngine(self.workspace, {"decisions": {"model_path": "candidate", "checkpoints": {"acceptance": "english"}}})
        self.assertEqual(engine.state_tokens("acceptance"), 472)

    def test_a_1024_token_checkpoint_doubles_the_acceptance_input(self):
        engine = DecisionEngine(self.workspace, {"decisions": {"checkpoints": {"acceptance": "typed-decisions"}}})
        node = {"task": "Add CSV export to the reports page."}
        result = {"summary": "Added CSV export with escaping, a download button and tests. " * 60}
        short = acceptance_state(node, result, 6000)
        long = acceptance_state(node, result, 6000, engine.state_tokens("acceptance"))
        self.assertLessEqual(estimated_tokens(json.dumps(short, ensure_ascii=False, sort_keys=True)), 472)
        self.assertLessEqual(estimated_tokens(json.dumps(long, ensure_ascii=False, sort_keys=True)), 984)
        self.assertGreater(len(long["summary"]), 1.8 * len(short["summary"]))

    def test_an_unscored_input_is_judged_by_the_budget_recorded_with_it(self):
        engine = DecisionEngine(self.workspace, {"decisions": {"checkpoints": {"acceptance": "typed-decisions"},
                                                               "max_state_chars": 6000}})
        state = {"task": "Add CSV export.", "summary": "Added CSV export with escaping and tests. " * 70}
        record = engine.record_unscored("acceptance", state, ACCEPTANCE_QUESTIONS)
        self.assertEqual(record["state_tokens"], 984)
        self.assertGreater(estimated_tokens(record["state"]), 472)
        self.assertFalse(record["truncated"])
        self.assertFalse(exceeds_token_budget(record))
        self.assertTrue(exceeds_token_budget({**record, "state_tokens": None}))

    def test_a_configured_checkpoint_is_sent_with_its_kinds_requests_only(self):
        seen = []

        class Backend:
            def predict(self, state, questions, checkpoint=None):
                seen.append(checkpoint)
                return {"answers": {key: {"noul": .9} for key in questions}, "model_identity": "fixture"}
        engine = DecisionEngine(self.workspace, {"decisions": {"checkpoints": {"acceptance": "typed-decisions"}}}, Backend())
        engine.decide("acceptance", {"task": "t", "summary": "s"}, ACCEPTANCE_QUESTIONS)
        engine.decide("intake", "fix the bug", {"needs_clarification": {"type": "noul", "instructions": "?"}})
        self.assertEqual(seen, ["typed-decisions", None])


class CheckpointTest(unittest.TestCase):
    def modules(self, error):
        def snapshot_download(repo, allow_patterns, local_files_only):
            raise error
        hub = types.SimpleNamespace(snapshot_download=snapshot_download)
        router = types.SimpleNamespace(DEFAULT_MODELS={"english": ("convaiinnovations/laya", None),
                                                       "typed-decisions": ("convaiinnovations/laya", "typed-decisions")})
        return {"huggingface_hub": hub, "laya": types.SimpleNamespace(router=router), "laya.router": router}

    def test_a_missing_checkpoint_names_the_setup_command(self):
        with patch.dict(sys.modules, self.modules(FileNotFoundError("not in cache"))):
            with self.assertRaisesRegex(ValueError, r"orc fusion decisions setup --checkpoint typed-decisions"):
                fusion_laya.checkpoint("typed-decisions")
            with self.assertRaisesRegex(ValueError, "unknown Laya checkpoint"):
                fusion_laya.checkpoint("french")

    def test_online_errors_are_not_disguised(self):
        with patch.dict(sys.modules, self.modules(ConnectionError("offline"))):
            with self.assertRaises(ConnectionError):
                fusion_laya.checkpoint("english", online=True)


class TrainCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def argv(self, config, **overrides):
        from fusion_decision_cli import run
        values = dict(decision_command="train", dataset="data.jsonl", output="candidate", model_path="", device="cpu",
                      epochs=1, learning_rate=0.0001, seed=7, kind=None, checkpoint=None)
        values.update(overrides)
        with patch("fusion_decision_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as execute:
            self.assertEqual(run(argparse.Namespace(**values), self.workspace, config), 0)
        return execute.call_args.args[0]

    def value(self, argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    def test_training_settings_become_explicit_runtime_arguments(self):
        argv = self.argv({"decisions": {"training": {"unfreeze_encoder": True, "class_balance": False, "label_smoothing": 0.05}}})
        self.assertEqual(self.value(argv, "--objective"), "soft_ce+proper_scoring")
        self.assertEqual(self.value(argv, "--encoder-learning-rate"), "2.5e-05")
        self.assertEqual(self.value(argv, "--label-smoothing"), "0.05")
        self.assertIn("--unfreeze-encoder", argv)
        self.assertIn("--no-class-balance", argv)
        self.assertEqual((self.value(argv, "--model-path"), self.value(argv, "--seed")), ("", "7"))
        self.assertNotIn("--checkpoint", argv)

    def test_a_kind_trains_from_its_configured_checkpoint(self):
        config = {"decisions": {"model_path": "base", "checkpoints": {"acceptance": "typed-decisions", "intake": "ckpt"}}}
        argv = self.argv(config, kind="acceptance")
        self.assertEqual((self.value(argv, "--checkpoint"), self.value(argv, "--model-path"), self.value(argv, "--kinds")),
                         ("typed-decisions", "", "acceptance"))
        argv = self.argv(config, kind="intake")
        self.assertEqual(self.value(argv, "--model-path"), str((self.workspace / "ckpt").resolve()))
        argv = self.argv(config, kind="review")
        self.assertEqual(self.value(argv, "--model-path"), str((self.workspace / "base").resolve()))
        argv = self.argv(config, kind="acceptance", model_path=str(self.workspace / "other"))
        self.assertEqual((self.value(argv, "--model-path"), self.value(argv, "--checkpoint")), (str((self.workspace / "other").resolve()), None))

    def test_without_a_kind_kinds_on_another_checkpoint_are_left_out(self):
        self.assertNotIn("--kinds", self.argv({}))
        argv = self.argv({"decisions": {"checkpoints": {"acceptance": "typed-decisions", "intake": "english"}}})
        self.assertEqual(self.value(argv, "--kinds"), "intake,recovery,review,routing")
        argv = self.argv({"decisions": {"checkpoints": {"acceptance": "typed-decisions", "intake": "english"}}},
                         checkpoint="typed-decisions")
        self.assertEqual(self.value(argv, "--kinds"), "acceptance,recovery,review,routing")
        with self.assertRaisesRegex(ValueError, "--kind"):
            self.argv({"decisions": {"checkpoints": {kind: "typed-decisions" for kind in
                                                     ("acceptance", "intake", "recovery", "review", "routing")}}})

    def test_the_runtime_parser_accepts_every_argument_the_cli_sends(self):
        argv = self.argv({"decisions": {"training": {"unfreeze_encoder": True, "class_balance": False},
                                        "checkpoints": {"acceptance": "typed-decisions"}}}, kind="acceptance")
        captured = {}
        with patch.dict("os.environ"), patch.object(sys, "argv", ["fusion_laya.py", *argv[2:]]), \
                patch.object(fusion_laya, "train", side_effect=lambda args: captured.update(vars(args)) or {}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fusion_laya.main(), 0)
        self.assertEqual((captured["checkpoint"], captured["kinds"], captured["unfreeze_encoder"], captured["class_balance"],
                          captured["objective"], captured["encoder_learning_rate"]),
                         ("typed-decisions", "acceptance", True, False, "soft_ce+proper_scoring", 2.5e-5))


if __name__ == "__main__":
    unittest.main()
