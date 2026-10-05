"""Outcome predictor: dataset joins, leakage guard, issue-grouped splits and metrics on synthetic records."""
import contextlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_outcome as outcome  # noqa: E402

DAY = 86_400_000


def iso(value):
    return outcome.iso(value).replace("Z", ".000Z")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class Fixture:
    """A control store and one TENET repo with two issues: 7 (two rounds, landed) and 8 (one round, recent)."""

    def __init__(self, root):
        self.control, self.repo = root / "control", root / "widget"
        base = outcome.ms("2026-10-01T10:00:00Z")
        self.now = base + 10 * DAY
        fusion = self.control / ".fusion"
        for run_id, model, effort, role in [("r1", "claude-opus-x", None, "implementation"),
                                            ("r2", "gpt-y", "high", "implementation")]:
            (fusion / "runs" / run_id).mkdir(parents=True)
            (fusion / "runs" / run_id / "task.json").write_text(json.dumps(
                {"agent": "claude" if "claude" in model else "codex", "role": role, "task": "x" * 40,
                 "settings_overrides": {"model": model, "reasoning_effort": effort}}))
            (fusion / "runs" / run_id / "result.json").write_text(json.dumps({"model": model, "reasoning_effort": effort}))
        write_jsonl(fusion / "traces.jsonl", [])
        write_jsonl(fusion / "decisions" / "events.jsonl", [
            {"event": "outcome", "task_id": "r1", "stage": "gate", "accepted": False, "time_ms": base + 1000},
            {"event": "outcome", "task_id": "r2", "stage": "verify", "accepted": True, "time_ms": base + DAY},
            {"event": "outcome", "task_id": "r2", "stage": "land", "accepted": False, "time_ms": base + DAY},
            {"event": "outcome_withdraw", "task_id": "r2", "time_ms": base + DAY + 1},
            {"event": "outcome", "task_id": "r2", "stage": "verify", "accepted": True, "time_ms": base + DAY + 2},
            {"event": "routing_log", "task_id": "r2", "chosen": "codex-y-high", "candidates": [
                {"key": "codex-y-high", "agent": "codex", "model": "gpt-y", "reasoning_effort": "high", "propensity": 0.6},
                {"key": "claude-x", "agent": "claude", "model": "claude-opus-x", "propensity": 0.4}]}])

        def outcome_row(agent, rnd, ts, run_id, model, files):
            return {"type": "build:outcome", "agent": agent, "round": rnd, "ts": iso(ts), "status": "completed",
                    "files": files, "missingRequired": [], "reasons": ["gate check proof_keeps_frozen_suites"],
                    "executor": {"runId": run_id, "model": model, "status": "partial", "costUsd": 1.5,
                                 "durationMs": 60_000, "blockers": ["permission denied: Bash"]}}

        write_jsonl(self.repo / ".tenet" / "build-journal.jsonl", [
            outcome_row("build-s-7-thing", 1, base, "r1", "claude-opus-x", ["a.py"]),
            {"type": "build:round", "agent": "build-s-7-thing", "round": 1, "score": 0.2, "delta": 0.2, "kept": False, "ts": iso(base)},
            outcome_row("build-s-7-thing", 2, base + 3_600_000, "r2", "gpt-y", ["a.py", "b.py"]),
            {"type": "build:round", "agent": "build-s-7-thing", "round": 2, "score": 1, "delta": 0.8, "kept": True,
             "ts": iso(base + 3_600_000)},
            {"type": "build:round", "agent": "build-s-8-other", "round": 1, "score": 1, "delta": 1, "kept": True,
             "ts": iso(self.now - 3_600_000)},
            {"type": "build:round", "agent": "build-legacy", "round": 1, "score": 0, "delta": 0, "kept": False, "ts": iso(base)}])
        record = self.repo / ".tenet" / "verify" / "8" / "abc.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"schema": "tenet.verify.v1", "issue": 8, "clean": False, "ts": iso(self.now - 1000)}))
        self.pulls = {str(self.repo): [
            {"number": 1, "headRefName": "issue/7-thing", "title": "agent(build-s-7-thing)", "state": "MERGED"},
            {"number": 2, "headRefName": "issue/9-gone", "title": "agent(build-s-9-gone)", "state": "MERGED"},
            {"number": 3, "headRefName": "revert-2", "title": 'Revert "agent(build-s-9-gone)"', "state": "MERGED"}]}

    def build(self):
        return outcome.build_dataset(self.control, [self.repo], use_gh=False, now_ms=self.now, pulls=self.pulls)


class DatasetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self.tmp.name))
        rows, self.summary = self.fixture.build()
        self.rows = {(r["features"]["round"], r["issue"]): r for r in rows}

    def tearDown(self):
        self.tmp.cleanup()

    def test_rounds_join_runs_lanes_and_reviewed_stages(self):
        first, second = self.rows[(1, 7)], self.rows[(2, 7)]
        self.assertEqual(first["features"]["lane"], "claude/claude-opus-x/default")
        self.assertEqual(second["features"]["lane"], "codex/gpt-y/high")
        self.assertEqual(second["features"]["kind"], "implementation")
        self.assertEqual(second["features"]["task_chars"], 40)
        self.assertEqual((second["features"]["prior_failed"], second["features"]["prior_rejections"]), (1, 1))
        self.assertEqual(second["features"]["prior_best_score"], 0.2)
        self.assertEqual(second["labels"]["verify"], True)
        self.assertEqual(second["label_sources"]["verify"], "orc")
        self.assertEqual(second["stages"], {"verify": True})
        self.assertEqual(second["routing"]["propensity"], 0.6)
        self.assertIsNone(first["labels"]["verify"])
        self.assertTrue(first["features"]["frozen_unverified"])
        self.assertEqual((first["features"]["denied"], first["features"]["files"]), (1, 1))

    def test_landing_censoring_and_verify_records(self):
        self.assertEqual({self.rows[(1, 7)]["labels"]["landed"], self.rows[(2, 7)]["labels"]["landed"]}, {True})
        recent = self.rows[(1, 8)]
        self.assertIsNone(recent["labels"]["landed"])
        self.assertEqual((recent["labels"]["verify"], recent["label_sources"]["verify"]), (False, "tenet"))
        legacy = self.rows[(1, None)]
        self.assertIsNone(legacy["labels"]["landed"])
        self.assertEqual(legacy["features"]["lane"], "?/?/default")
        self.assertEqual(outcome.tenet_landings(self.fixture.pulls[str(self.fixture.repo)])[9],
                         {"landed": False, "open": False, "reverted": True, "merged_ms": None})
        self.assertEqual(self.summary["by_repo"]["widget"]["rows"], 4)
        self.assertEqual(self.summary["by_repo"]["widget"]["propensity"], 1)

    def test_verify_record_labels_the_latest_kept_round_it_measured(self):
        attempts = [{"repo": "r", "issue": 5, "unit": "u", "round": i, "ts": 1000 * i, "kept": kept}
                    for i, kept in [(1, True), (2, True), (3, False), (4, True)]]
        records = {("r", 5): [{"issue": 5, "ts": 3500, "clean": False}, {"issue": 5, "ts": 3600, "clean": True},
                              {"issue": 5, "ts": 9000, "clean": True}]}
        rows = outcome.build_rows(attempts, verifies=records, now_ms=10 ** 9)
        self.assertEqual([r["labels"]["verify"] for r in rows], [None, False, None, True])

    def test_routing_features_exclude_post_dispatch_information(self):
        for label in outcome.LABELS:
            names = set(outcome.feature_names("routing", label))
            self.assertFalse(names & outcome.POST_DISPATCH, label)
            self.assertNotIn("kept", outcome.feature_names("verify", "kept"))
        row = self.rows[(2, 7)]
        encoder = outcome.fit_encoder(list(self.rows.values()), outcome.feature_names("routing", "verify"),
                                      outcome.INTERACTIONS["routing"], min_count=1, min_lane=1)
        self.assertFalse({c.split("=")[0].split("?")[0] for c in encoder["columns"]} & outcome.POST_DISPATCH)
        changed = {**row, "features": {**row["features"], "score": 0.0, "delta": -1.0, "duration_s": 9e9, "files": 99}}
        self.assertEqual(outcome.encode(row, encoder), outcome.encode(changed, encoder))


def synthetic_rows(n=240, seed=3):
    rng = random.Random(seed)
    start = outcome.ms("2026-09-25T00:00:00Z")
    rows = []
    for i in range(n):
        lane = rng.choice(["a/m1/default", "b/m2/high"])
        score = rng.random()
        verify = rng.random() < (0.2 + 0.6 * score) * (1.0 if lane.startswith("a") else 0.7)
        group_ts = start + (i // 2) * 3_600_000
        features = {"agent": lane.split("/")[0], "model": lane.split("/")[1], "effort": lane.split("/")[2], "lane": lane,
                    "repo": "r", "kind": "implementation", "round": i % 2 + 1, "prior_rounds": i % 2,
                    "prior_failed": 0, "prior_kept": 0, "prior_rejections": 0, "prior_best_score": None,
                    "task_chars": None, "hour": 1, "issue_age_h": 0.0, "score": score, "delta": score,
                    "missing_required": 0, "files": 2, "frozen_unverified": False, "worker_status": "completed",
                    "executor_status": "success", "cost_usd": 1.0 if lane.startswith("a") else 0.5,
                    "duration_s": 60, "blockers": 0, "denied": 0}
        rows.append({"group": f"g{i // 2}", "group_first_ts": group_ts, "ts": group_ts + (i % 2), "features": features,
                     "labels": {"verify": verify, "kept": score > 0.5, "landed": verify},
                     "routing": {"chosen": lane, "propensity": 0.5, "candidates": [
                         {"key": key, "agent": key.split("/")[0], "model": key.split("/")[1], "effort": key.split("/")[2],
                          "propensity": 0.5} for key in ("a/m1/default", "b/m2/high")]}})
    return rows


class SplitAndModelTest(unittest.TestCase):
    def test_issue_split_keeps_groups_together_on_first_attempt_side(self):
        rows = synthetic_rows(40)
        cutoff = outcome.iso(rows[21]["group_first_ts"])
        straddle = {**rows[21], "ts": rows[21]["ts"] + 30 * DAY}
        for how in ("time", "tail"):
            train, test = outcome.split(rows[:21] + [straddle] + rows[22:], how, cutoff)
            self.assertFalse({r["group"] for r in train} & {r["group"] for r in test}, how)
            self.assertEqual(len(train) + len(test), 40)
        pairs = outcome.folds(rows, "rolling", k=4)
        self.assertEqual(len(pairs), 4)
        self.assertEqual(sum(len(test) for _, test in pairs), 40 - len(pairs[0][0]))
        for train, test in pairs:
            self.assertLess(max(r["group_first_ts"] for r in train), min(r["group_first_ts"] for r in test))
            self.assertFalse({r["group"] for r in train} & {r["group"] for r in test})
        train, test = outcome.split(rows, "time", cutoff)
        self.assertTrue(all(r["group_first_ts"] < outcome.ms(cutoff) for r in train))
        self.assertIn(rows[21]["group"], {r["group"] for r in test})

    def test_metrics_reference_values(self):
        m = outcome.metrics([1, 0, 1, 0], [0.9, 0.1, 0.4, 0.6])
        self.assertEqual((m["accuracy"], m["balanced_accuracy"], m["auc"]), (0.5, 0.5, 0.75))
        self.assertAlmostEqual(m["brier"], (0.01 + 0.01 + 0.36 + 0.36) / 4, places=4)
        self.assertEqual(outcome.auc([1, 0], [0.5, 0.5]), 0.5)
        self.assertIsNone(outcome.metrics([1, 1], [0.7, 0.8])["auc"])
        self.assertEqual([b["n"] for b in outcome.metrics([1, 0, 1], [0.05, 0.5, 1.0])["reliability"]], [1, 1, 1])

    def test_models_learn_signal_and_lane_baseline_is_beta_mean(self):
        rows = synthetic_rows()
        train, test = outcome.split(rows, "tail")
        model = outcome.fit_models(train, "verify", "verify", l2=1.0)
        lane = "a/m1/default"
        members = [r for r in train if r["features"]["lane"] == lane]
        self.assertAlmostEqual(model["lanes"][lane], (sum(r["labels"]["verify"] for r in members) + 1) / (len(members) + 2))
        ys = [int(r["labels"]["verify"]) for r in test]
        lr = outcome.metrics(ys, [outcome.predict(model, r) for r in test])
        gb = outcome.metrics(ys, [outcome.predict(model, r, "boosting") for r in test])
        self.assertGreater(lr["auc"], 0.65)
        self.assertGreater(gb["auc"], 0.55)
        self.assertEqual(outcome.metrics(ys, [outcome.predict(model, r, "majority") for r in test])["auc"], 0.5)

    def test_evaluate_reports_baselines_counterfactual_and_insufficient_labels(self):
        rows = synthetic_rows(120)
        for row in rows:
            row["labels"]["landed"] = None
        results = outcome.evaluate(rows, ("tail", "rolling"), boosting=False)
        verify = next(r for r in results if r["label"] == "verify" and r["feature_set"] == "routing" and r["split"] == "tail")
        rolling = next(r for r in results if r["label"] == "verify" and r["feature_set"] == "routing" and r["split"] == "rolling")
        self.assertGreater(rolling["n_test"], verify["n_test"])
        self.assertEqual(set(verify["models"]), {"majority", "lane", "logistic"})
        self.assertEqual(verify["routing_counterfactual"]["rows_with_propensity"], verify["n_test"])
        self.assertTrue(all(r["status"] == "insufficient" for r in results if r["label"] == "landed"))
        self.assertIn("insufficient", outcome.format_results(results))

    def test_cli_builds_and_evaluates_under_the_control_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            rows, _ = fixture.build()
            out = outcome.default_dir(fixture.control) / "dataset.jsonl"
            out.parent.mkdir(parents=True)
            write_jsonl(out, rows + synthetic_rows(60))

            class Args:
                outcome_command, dataset, out, l2, split, cutoff, no_boosting, json = (
                    "evaluate", None, None, 5.0, "tail", outcome.TIME_CUTOFF, True, False)
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(outcome.command(Args, fixture.control), 0)
            self.assertIn("logistic", printed.getvalue())
            report = json.loads((outcome.default_dir(fixture.control) / "report.json").read_text())
            self.assertEqual(report["coverage"]["rows"], 64)


class StopRuleTest(unittest.TestCase):
    @staticmethod
    def scored(group, rounds, landed):
        return [{"row": {"group": group, "id": f"{group}{i}", "ts": i, "labels": {"kept": kept, "landed": landed}},
                 "p_stop": p, "cost": 2.0, "cost_source": "actual" if i else "lane"} for i, (p, kept) in enumerate(rounds)]

    def test_stop_accounting_counts_skips_cost_and_lost_lands(self):
        groups = {"a": self.scored("a", [(0.9, False), (0.05, True), (0.5, False)], True),
                  "b": self.scored("b", [(0.9, True), (0.05, False)], True),
                  "c": self.scored("c", [(0.05, True)], None),
                  "d": self.scored("d", [(0.9, True)], True)}
        out = outcome.stop_outcome(groups, 0.1, kept_total=4)
        self.assertEqual({k: out[k] for k in ("rounds_skipped", "kept_lost", "issues_stopped", "stopped_landed",
                                              "stopped_unknown", "lands_lost", "at_risk_unknown", "fallback_cost")},
                         {"rounds_skipped": 4, "kept_lost": 2, "issues_stopped": 3, "stopped_landed": 2,
                          "stopped_unknown": 1, "lands_lost": 1, "at_risk_unknown": 1, "fallback_cost": 1})
        self.assertEqual((out["usd_saved"], out["kept_lost_share"]), (8.0, 0.5))
        self.assertEqual(outcome.stop_outcome(groups, 0.01, 4)["rounds_skipped"], 0)

    def test_stop_rule_replays_held_out_issues_per_threshold(self):
        rows = synthetic_rows(160)
        for row in rows:
            row["id"] = f"{row['group']}:{row['ts']}"
        report = outcome.stop_rule(rows, ("rolling", "tail"), (0.1, 0.5), boosting=False)
        self.assertEqual([e["split"] for e in report], ["rolling", "tail"])
        for entry in report:
            self.assertEqual(entry["model"], "logistic")
            self.assertEqual([t["t"] for t in entry["thresholds"]], [0.1, 0.5])
            low, high = entry["thresholds"]
            self.assertLessEqual(low["rounds_skipped"], high["rounds_skipped"])
            self.assertLessEqual(high["rounds_skipped"], entry["rounds"])
        self.assertIn("landLost", outcome.format_stop_rule(report))


if __name__ == "__main__":
    unittest.main()
