"""Optional real checkpoint smoke test; uses synthetic fixtures, never production labels.

Run after `fusion decisions setup` with the managed Laya interpreter.
No coding-agent calls, no promotion, no downloads. Candidate files are temporary.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion_decisions import ACCEPTANCE_QUESTIONS, DecisionEngine, INTAKE_QUESTIONS, fit_calibration
from fusion_laya import train, evaluate


ACCEPTANCE_TASK = "Add CSV export with tests to this project: a csv_export module plus unit tests covering escaping and filtering."
# Authored states with deliberate near-misses. `expect` is asserted for the
# clear-cut cases only; the near-misses print so a checkpoint can be compared.
ACCEPTANCE_CASES = [
    ("clean success", {"summary": "Added csv_export.py with escaping and filtering; 6 unit tests pass", "changed": ["csv_export.py", "test_csv_export.py"], "tests": ["python -m unittest: 6 passed"]}, "true"),
    ("did nothing", {"summary": "did nothing", "changed": [], "tests": []}, "false"),
    ("plausible but off-task", {"summary": "Refactored the dashboard filter for readability; all tests pass", "changed": ["dashboard.py"], "tests": ["python -m unittest: 12 passed"]}, "false"),
    ("files, no tests (near-miss; structural gate owns this)", {"summary": "Added csv_export.py with escaping and filtering", "changed": ["csv_export.py"], "tests": []}, None),
    ("tests listed, not run (near-miss; structural gate owns this)", {"summary": "Added csv_export.py and test_csv_export.py", "changed": ["csv_export.py", "test_csv_export.py"], "tests": ["not run"]}, None),
    ("honest partial (near-miss)", {"summary": "Added csv_export.py; tests not yet written", "changed": ["csv_export.py"], "tests": []}, None),
]


def acceptance(engine):
    misses = []
    for name, state, expect in ACCEPTANCE_CASES:
        record = engine.decide("acceptance", {"task": ACCEPTANCE_TASK, **state}, ACCEPTANCE_QUESTIONS, {"group": name})
        assert record["status"] == "ok", record
        got = record["recommendations"]["plausible"]["value"]
        probs = {key: round(record["prediction"][key]["true"], 2) for key in ACCEPTANCE_QUESTIONS}
        print(json.dumps({"case": name, "plausible_true": probs["plausible"], "failed_task_true": probs["failed_task"],
                          "expect_plausible": expect, "duration_ms": record["duration_ms"]}), flush=True)
        if expect is not None and got != expect:
            misses.append(name)
    assert not misses, f"acceptance question missed clear-cut cases: {misses}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", action="store_true", help="also exercise one real gradient update and candidate evaluation")
    parser.add_argument("--acceptance", action="store_true", help="also run the authored acceptance near-miss set and assert the clear-cut cases")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fusion-laya-smoke-") as directory:
        workspace = Path(directory)
        engine = DecisionEngine(workspace, {"decisions": {"python": sys.executable}})
        if args.acceptance:
            acceptance(engine)
        rows = []
        for index, state in enumerate(["Implement a CSV export button.", "Investigate the build failure. Planning only."]):
            record = engine.decide("intake", state, INTAKE_QUESTIONS, {"group": f"synthetic-{index}"})
            assert record["status"] == "ok", record
            assert not record["truncated"], record
            assert not engine.allowed(record, "workflow")
            rows.append({"schema": "fusion.training.v1", "id": record["id"], "group": f"synthetic-{index}",
                         "split": "train" if index == 0 else "validation", "kind": "intake", "state": record["state"],
                         "questions": record["questions"], "schema_hash": record["schema_hash"],
                         "labels": {"workflow": "build" if index == 0 else "discovery"},
                         "prediction": record["prediction"], "model_identity": record["model_identity"]})
            print(json.dumps({"status": record["status"], "duration_ms": record["duration_ms"], "model_identity": record["model_identity"]}), flush=True)
        assert rows[0]["model_identity"] == rows[1]["model_identity"]
        dataset = workspace / "synthetic.jsonl"
        dataset.write_text("".join(json.dumps(row) + "\n" for row in rows))
        baseline = fit_calibration(dataset, workspace / "baseline.json")
        assert not any(bucket["qualified"] for bucket in baseline["buckets"].values())
        if args.train:
            started = time.monotonic()
            candidate = workspace / "candidate"
            report = train(argparse.Namespace(dataset=str(dataset), output=str(candidate), model_path="", device="cpu", seed=42, epochs=1, learning_rate=.0001))
            assert report["steps"] == 1 and not report["promoted"], report
            output = workspace / "candidate-predictions.jsonl"
            evaluation = evaluate(argparse.Namespace(dataset=str(dataset), output=str(output), model_path=str(candidate), device="cpu"))
            assert evaluation["validation_questions"] == 1
            assert evaluation["holdout"]["status"] == "checked", evaluation
            assert report["data_quality"]["answers"] == 1
            assert len(report["seen_train_inputs"]) == 1 and report["lineage_complete"]
            assert evaluation["by_kind"]["intake"]["questions"] == 1
            calibration = fit_calibration(output, workspace / "candidate-calibration.json")
            assert calibration["model_identity"] != baseline["model_identity"]
            assert not any(bucket["qualified"] for bucket in calibration["buckets"].values())
            print(json.dumps({"training_steps": report["steps"], "candidate_evaluated": True, "qualified": False,
                              "duration_seconds": round(time.monotonic() - started, 2)}), flush=True)
    print("Laya smoke passed; synthetic fixtures and candidate removed.")


if __name__ == "__main__":
    main()
