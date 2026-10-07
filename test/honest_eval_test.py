"""Round evaluation that a constant predictor cannot pass: held-out majority, balanced accuracy, constant questions."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_learn_cli as learn_cli
import fusion_quality as quality
import fusion_training_loop as loop
from fusion_quality import baseline_comparison

QUESTIONS = {"plausible": {"type": "noul", "instructions": "Does the work satisfy the task?"},
             "failed_task": {"type": "noul", "instructions": "Did the worker fail the task?"}}


def noul(p):
    return {"true": p, "false": 1 - p}


class HonestEvaluationTest(unittest.TestCase):
    def rows(self):
        rows = []
        for index in range(11):
            rows.append({"id": f"t{index}", "group": f"t{index}", "split": "train", "kind": "acceptance", "questions": QUESTIONS,
                         "labels": {"plausible": "true" if index < 6 else "false", "failed_task": "true" if index == 10 else "false"}})
        for index in range(20):
            rows.append({"id": f"v{index}", "group": f"v{index}", "split": "validation", "kind": "acceptance", "questions": QUESTIONS,
                         "labels": {"plausible": "true" if index < 7 else "false", "failed_task": "false"}})
        return rows

    def constant_false(self, rows):
        return {row["id"]: {"plausible": noul(0.3 + 0.01 * (index % 20)), "failed_task": noul(0.1)} for index, row in enumerate(rows)}

    def discriminating(self, rows):
        def p(row):
            index, truth = int(row["id"][1:]), row["labels"]["plausible"] == "true"
            if row["split"] == "validation" and index in (0, 19):
                return 0.3 if truth else 0.6
            return 0.8 if truth else 0.2
        return {row["id"]: {"plausible": noul(p(row)), "failed_task": noul(0.1)} for row in rows}

    def controls(self, rows):
        return {row["id"]: {"plausible": noul(0.4), "failed_task": noul(0.1)} for row in rows if row["split"] == "validation"}

    def round(self, report):
        source = {"model_identities": ["source"], "accuracy": 0.6, "benchmark_hash": "b", "holdout": {"status": "checked"},
                  "validation_questions": 40, "validation_groups": 20}
        candidate = {**source, "model_identities": ["candidate"], "accuracy": report["pooled"]["accuracy"],
                     **{k: report[k] for k in ("baselines", "by_question", "headline", "pooled", "constant_questions")}}
        return {"jobs": {"train": "t", "baseline": "b", "evaluate": "e"},
                "results": {"train": {"model_identity": "candidate", "source_identity": "source"},
                            "baseline": source, "evaluate": candidate, "calibrate": {"buckets": {}}}}

    def test_constant_false_predictor_is_degenerate_and_not_improved(self):
        rows = self.rows()
        report = baseline_comparison(rows, self.constant_false(rows), self.controls(rows))
        headline = report["headline"]
        self.assertTrue(headline["degenerate"])
        self.assertIn("acceptance:plausible answers false on 20 of 20", headline["degenerate_reason"])
        self.assertEqual(headline["balanced_accuracy"], 0.5)
        self.assertAlmostEqual(headline["accuracy"], 0.65)
        self.assertEqual(headline["positive_rate"], 0)
        self.assertLess(headline["auc"], 1)
        self.assertAlmostEqual(report["pooled"]["accuracy"], 33 / 40)
        self.assertEqual(report["baselines"]["majority"]["balanced_accuracy"], 0.5)
        self.assertEqual(report["baselines"]["majority"]["balanced_margin"], 0)
        self.assertTrue(quality.degenerate([noul(0.4)] * 3).startswith("identical probabilities"))
        proof = loop.proof(self.round(report))
        self.assertGreater(proof["delta"], loop.IMPROVEMENT_MARGIN)
        self.assertEqual(proof["outcome"], "degenerate")
        self.assertTrue(proof["degenerate"])
        self.assertEqual(proof["balanced_accuracy"], 0.5)
        self.assertTrue(any("constant predictor" in note for note in proof["notes"]))
        status = learn_cli.measured_round({"id": "r33", "proof": proof})
        self.assertEqual((status["outcome"], status["balanced_accuracy"], status["degenerate"]), ("degenerate", 0.5, True))
        self.assertIn("auc", status)
        self.assertEqual(status["baselines"]["majority"]["balanced_accuracy"], 0.5)
        self.assertTrue(status["questions"]["acceptance:plausible"]["degenerate"])

    def test_majority_baseline_is_the_held_out_majority_not_the_training_one(self):
        rows = self.rows()
        report = baseline_comparison(rows, self.constant_false(rows))
        question = report["by_question"]["acceptance:plausible"]
        self.assertAlmostEqual(question["baselines"]["majority"]["accuracy"], 13 / 20)
        self.assertEqual(question["baselines"]["majority"]["balanced_accuracy"], 0.5)
        self.assertAlmostEqual(question["baselines"]["train_majority"]["accuracy"], 7 / 20)
        self.assertEqual(question["baselines"]["train_majority"]["balanced_accuracy"], 0.5)
        self.assertAlmostEqual(report["baselines"]["majority"]["accuracy"], 13 / 20)
        self.assertAlmostEqual(report["baselines"]["train_majority"]["accuracy"], 7 / 20)
        self.assertEqual(question["label_rates"], {"false": 0.65, "true": 0.35})

    def test_constant_question_is_reported_but_left_out_of_the_headline(self):
        rows = self.rows()
        report = baseline_comparison(rows, self.discriminating(rows), self.controls(rows))
        self.assertEqual(report["constant_questions"], {"acceptance:failed_task": {"label": "false", "n": 20}})
        self.assertEqual(report["headline"]["questions"], ["acceptance:plausible"])
        self.assertEqual(report["headline"]["n"], 20)
        self.assertEqual(report["pooled"]["n"], 40)
        self.assertTrue(report["by_question"]["acceptance:failed_task"]["constant"])
        self.assertIsNone(report["by_question"]["acceptance:failed_task"]["auc"])
        self.assertEqual({b: v["n"] for b, v in report["baselines"].items()}, {"majority": 20, "train_majority": 20, "control": 20})

    def test_discriminating_predictor_beats_the_baselines_and_can_be_a_gain(self):
        rows = self.rows()
        report = baseline_comparison(rows, self.discriminating(rows), self.controls(rows))
        headline = report["headline"]
        self.assertFalse(headline["degenerate"])
        self.assertIsNone(headline["degenerate_reason"])
        self.assertAlmostEqual(headline["balanced_accuracy"], (6 / 7 + 12 / 13) / 2)
        self.assertAlmostEqual(headline["auc"], 90 / 91)
        self.assertAlmostEqual(headline["positive_rate"], 7 / 20)
        self.assertEqual(report["baselines"]["control"]["balanced_accuracy"], 0.5)
        proof = loop.proof(self.round(report))
        self.assertEqual(proof["outcome"], "gain", proof["notes"])
        self.assertFalse(proof["degenerate"])
        worse = self.round(report)
        worse["results"]["evaluate"]["baselines"]["control"].update(balanced_accuracy=0.9)
        self.assertEqual(loop.proof(worse)["outcome"], "flat")


if __name__ == "__main__":
    unittest.main()
