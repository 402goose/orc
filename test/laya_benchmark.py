"""Measure how well the installed checkpoint discriminates on Fusion's own schemas.

Authored cases with deliberate near-misses, no coding-agent calls and no
production labels. Answers the question a calibration report cannot answer
until a hundred workflows have been labeled: is there any signal in this
decision kind on this checkpoint, and is labeling it worth anyone's time?

    ~/.local/share/orc/laya/bin/python test/laya_benchmark.py
    ~/.local/share/orc/laya/bin/python test/laya_benchmark.py --kind recovery
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_decisions
from fusion_decisions import DecisionEngine, INTAKE_QUESTIONS, RECOVERY_QUESTIONS, REVIEW_QUESTIONS


SPEC = ("Add a CSV export button to the reports page. Export the currently filtered rows only, "
        "comma-delimited, RFC4180 quoting, UTF-8. Add unit tests for quote escaping and for the filter.")


def intake(request, planning_only=False):
    return {"request": request, "planning_only": planning_only}


def recovery_state(blockers, failure="worker_error", attempt=1, max_attempts=3,
                   status="error", accepted=False, repeated=False, automatic=True):
    return {"status": status, "accepted": accepted, "failure": failure, "blockers": list(blockers),
            "attempt": attempt, "max_attempts": max_attempts, "repeated_failure": repeated,
            "automatic_lane": automatic}


# (name, state, expected answer for the gating question or None for a near-miss)
CASES = {
    "intake": (INTAKE_QUESTIONS, "workflow", [
        ("build", intake(SPEC), "build"),
        ("debug", intake("Fix the crash when a user logs in with an expired session token."), "debug"),
        ("review", intake("Review the open pull request for the payments module and report findings."), "review"),
        ("discovery: map, no code", intake("Map how the checkout retry logic works and document the call flow. Do not change any code.", True), "discovery"),
        ("discovery: research", intake("Research which queue library would fit our ingestion path and write up the tradeoffs. Planning only.", True), "discovery"),
        ("discovery: audit only", intake("Audit the session handling for security issues and report what you find. Do not implement fixes.", True), "discovery"),
        ("nm: investigate and fix", intake("Investigate why checkout retries loop, and fix the cause."), None),
        ("nm: phrased as a question", intake("Could we add saved searches to the dashboard?"), None),
    ]),
    # Recovery scores 2/5 here with two distinct answers across eight states. Three
    # interventions were measured against these same states and none moved it, so
    # they are recorded rather than left for the next person to retry:
    #   state as a dict / as mechanical prose / as the blocker text alone -> 2/5 each
    #     (prose only changed which label it collapsed to: dict "stop", prose "switch")
    #   5 options / 3 non-overlapping / 2 -> +0.1 confidence at best; at 2 options the
    #     same label at ~0.60 for every state, including the quota one
    #   the 5-way action choice replaced by binary recognition nouls -> "does this need
    #     a person's decision" 7/8, "could a retry fix this" 4/7, "was the worker
    #     unavailable" 3/8 (0.89 on a plain test failure)
    # The pattern across all three: whether another attempt fixes "test failed: 3
    # assertions" depends on why it failed, and the blocker text does not say. The
    # kinds that score well ask questions their input fully determines.
    "recovery": (RECOVERY_QUESTIONS, "action", [
        ("continue", recovery_state([], failure=None, status="success", accepted=True), "continue"),
        ("repair", recovery_state(["test failed: 3 assertions in test_export.py"]), "repair"),
        ("switch", recovery_state(["codex is not available on PATH"], failure="missing_executable"), "switch"),
        ("stop: quota", recovery_state(["You've hit your usage limit; resets at 5:10am"], failure="quota"), "stop"),
        ("ask: needs a decision", recovery_state(["blocked: should archived records be included? needs a product decision"]), "ask"),
        ("nm: last attempt", recovery_state(["test failed: 3 assertions"], attempt=3, max_attempts=3), None),
        ("nm: repeated failure", recovery_state(["ModuleNotFoundError: laya"], attempt=2, repeated=True), None),
        ("nm: permission denied", recovery_state(["permission denied: Write(/etc/hosts)"], failure="permission_denied"), None),
    ]),
    "review": (REVIEW_QUESTIONS, "specialty", [
        ("payments", {"task": "Review the diff that changes refund amount rounding and retry behavior in the payments service.", "role": "review", "write": False}, "payments"),
        ("security", {"task": "Review the diff that changes session token storage and the login redirect.", "role": "review", "write": False}, "security"),
        ("data", {"task": "Review the migration that drops a column and backfills a new one on a 50M-row table.", "role": "review", "write": False}, "data"),
        ("general", {"task": "Review a one-line diff fixing a typo in a log message.", "role": "review", "write": False}, "general"),
    ]),
}

ACCEPTANCE_TASK = "Add CSV export with tests: a csv_export module plus unit tests covering escaping and filtering."
if hasattr(fusion_decisions, "ACCEPTANCE_QUESTIONS"):
    CASES["acceptance"] = (fusion_decisions.ACCEPTANCE_QUESTIONS, "plausible", [
        ("clean success", {"task": ACCEPTANCE_TASK, "summary": "Added csv_export.py with escaping and filtering; 6 unit tests pass", "changed": ["csv_export.py", "test_csv_export.py"], "tests": ["python -m unittest: 6 passed"]}, "true"),
        ("did nothing", {"task": ACCEPTANCE_TASK, "summary": "did nothing", "changed": [], "tests": []}, "false"),
        ("plausible but off-task", {"task": ACCEPTANCE_TASK, "summary": "Refactored the dashboard filter for readability; all tests pass", "changed": ["dashboard.py"], "tests": ["python -m unittest: 12 passed"]}, "false"),
        ("nm: files, no tests", {"task": ACCEPTANCE_TASK, "summary": "Added csv_export.py with escaping and filtering", "changed": ["csv_export.py"], "tests": []}, None),
        ("nm: tests listed, not run", {"task": ACCEPTANCE_TASK, "summary": "Added csv_export.py and test_csv_export.py", "changed": ["csv_export.py", "test_csv_export.py"], "tests": ["not run"]}, None),
    ])


def measure(engine, kind, questions, gate, cases):
    rows = []
    for name, state, expected in cases:
        record = engine.decide(kind, state, questions, {"group": f"{kind}:{name}"})
        if record["status"] != "ok":
            raise SystemExit(f"{kind}/{name}: classifier unavailable: {record.get('error')}")
        answer = record["recommendations"][gate]
        rows.append({"case": name, "answer": answer["value"], "probability": round(answer["probability"], 3),
                     "expected": expected, "correct": None if expected is None else answer["value"] == expected,
                     "duration_ms": record["duration_ms"]})
    return rows


def report(kind, gate, rows):
    graded = [row for row in rows if row["correct"] is not None]
    hits = sum(row["correct"] for row in graded)
    spread = statistics.pstdev([row["probability"] for row in rows]) if len(rows) > 1 else 0
    answers = {row["answer"] for row in rows}
    print(f"\n{kind} ({gate})  {hits}/{len(graded)} clear-cut correct   "
          f"p={min(r['probability'] for r in rows):.2f}-{max(r['probability'] for r in rows):.2f}  "
          f"spread={spread:.3f}  distinct answers={len(answers)}/{len(rows)}")
    for row in rows:
        mark = "  " if row["correct"] is None else ("ok" if row["correct"] else "XX")
        expected = f"  expected {row['expected']}" if row["correct"] is False else ""
        print(f"  {mark} {row['case']:28s} {row['answer']:<10s} p={row['probability']:.2f}{expected}")
    # A kind that answers the same label everywhere, or never concentrates, cannot
    # qualify however many workflows are labeled -- say so rather than implying patience.
    if len(answers) == 1:
        verdict = "degenerate: one answer for every state; labeling this kind cannot help until the state or checkpoint changes"
    elif graded and hits / len(graded) < 0.5:
        verdict = "weak: majority of clear-cut cases wrong"
    elif max(row["probability"] for row in rows) < 0.75:
        verdict = "low confidence: no case approaches a usable threshold"
    else:
        verdict = "usable signal on clear-cut cases; calibration decides the rest"
    print(f"  -> {verdict}")
    return {"kind": kind, "clear_cut": len(graded), "correct": hits, "distinct_answers": len(answers), "verdict": verdict}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=sorted(CASES), action="append", help="limit to one kind; repeatable")
    parser.add_argument("--json", action="store_true", help="emit raw rows instead of the table")
    args = parser.parse_args()
    kinds = args.kind or sorted(CASES)
    with tempfile.TemporaryDirectory(prefix="fusion-laya-benchmark-") as directory:
        engine = DecisionEngine(Path(directory), {"decisions": {"python": sys.executable}})
        output = {}
        for kind in kinds:
            questions, gate, cases = CASES[kind]
            output[kind] = measure(engine, kind, questions, gate, cases)
        if args.json:
            print(json.dumps(output, indent=2))
            return
        for kind in kinds:
            report(kind, CASES[kind][1], output[kind])
    print("\nNear-misses print without a verdict on purpose: they record how the checkpoint\n"
          "behaves on cases whose right answer is a judgment call, for comparison after a\n"
          "fine-tune or a checkpoint change.")


if __name__ == "__main__":
    main()
