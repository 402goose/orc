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
from fusion_decisions import DecisionEngine, INTAKE_QUESTIONS, fit_calibration
from fusion_laya import train, evaluate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", action="store_true", help="also exercise one real gradient update and candidate evaluation")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fusion-laya-smoke-") as directory:
        workspace = Path(directory)
        engine = DecisionEngine(workspace, {"decisions": {"python": sys.executable}})
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
            calibration = fit_calibration(output, workspace / "candidate-calibration.json")
            assert calibration["model_identity"] != baseline["model_identity"]
            assert not any(bucket["qualified"] for bucket in calibration["buckets"].values())
            print(json.dumps({"training_steps": report["steps"], "candidate_evaluated": True, "qualified": False,
                              "duration_seconds": round(time.monotonic() - started, 2)}), flush=True)
    print("Laya smoke passed; synthetic fixtures and candidate removed.")


if __name__ == "__main__":
    main()
