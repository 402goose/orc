"""Optional tokenizer check for the decision-input token budget; loads the checkpoint, never runs inference.

Run with the managed Laya interpreter after `fusion decisions setup`:

    ~/.local/share/orc/laya/bin/python test/laya_token_budget.py          # verify
    ~/.local/share/orc/laya/bin/python test/laya_token_budget.py --write  # re-measure the fixture

Verifies that STATE_HEAD_TOKENS matches the question heads the tokenizer
builds, that test/fixtures/laya_token_counts.json still holds the
tokenizer's counts, and that every acceptance input in it is complete for
the model (fusion_laya.truncated is False) unless it is marked
`source_truncated`. Synthetic texts only; never
production decisions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "test/fixtures/laya_token_counts.json"
sys.path.insert(0, str(ROOT))
from fusion_decisions import ACCEPTANCE_QUESTIONS, STATE_HEAD_TOKENS, acceptance_state, estimated_tokens, state_tokens


def texts():
    random.seed(20260924)
    words = ("acceptance summary verdict worker route stage review label export dataset tokenizer budget "
             "workflow criterion excerpt marker calibration threshold decision runtime checkpoint").split()
    prose = ("The worker added a bounded acceptance input, kept the criterion whole, and excerpted the summary "
             "from its start with a visible marker. Tests cover the gate, the verdict, and the export. ")
    code = ("def acceptance_state(source, result, cap, tokens=None):\n    fields, criterion, detail = acceptance_task(source)\n"
            "    lists = {key: _listed(result.get(key), limit) for key, limit in ACCEPTANCE_LIST_LIMITS.items()}\n"
            "    if _encoded(whole) <= cap:\n        return whole  # fits\n")
    yield "prose", prose * 8
    yield "code", code * 5
    yield "shell", "PYTHONDONTWRITEBYTECODE=1 python3 test/run_python.py && git -C .fusion/worktrees/wf-9 log --oneline -3 | head\n" * 12
    yield "pytest", "\n".join(f"test/verdict_labels_test.py::VerdictLabelsTest::test_{random.choice(words)}_{i} PASSED [{i}%]" for i in range(20))
    yield "paths", " ".join(f"src/{random.choice(words)}/{random.choice(words)}_{random.randint(1, 99)}.py:{random.randint(1, 999)}" for _ in range(40))
    yield "shas", " ".join(hashlib.sha1(str(i).encode()).hexdigest() for i in range(30))
    yield "uuids", " ".join(uuid.UUID(int=random.getrandbits(128)).hex for _ in range(30))
    yield "digits", " ".join(str(random.randint(0, 10 ** 12)) for _ in range(100))
    yield "diff", "\n".join(f"@@ -{i},7 +{i},9 @@\n-    x = foo(bar[{i}])\n+    x = foo(bar[{i}], baz={{'k': {i}}})" for i in range(20))
    yield "json", json.dumps({"evidence": [{"quote": prose[:80], "line": i, "path": f"fusion_{random.choice(words)}.py"} for i in range(12)]})
    yield "json-escaped", json.dumps(json.dumps({"request": prose + "\n" + code}))
    yield "whitespace", "a\n\t  b\n\n  c   " * 60
    yield "accented", "Résumé naïve café — “quoted” ✓ → ← " * 25
    yield "cjk", "日本語のテキストは長いです。" * 40
    yield "emoji", "😀🚀✅❌🔥 done " * 40


def acceptance_cases():
    random.seed(46)
    headline = "Fixes https://github.com/hathbanger/orc/issues/46 — Woodland can show the previous survey"
    detail = "\nSelected from Truffle pig hunt.\n" + json.dumps({"evidence": ["quote " * 40] * 30})
    node = {"role": "implementation", "decision_context": {"request": headline + detail, "workflow_kind": "debug", "stage": "implement"}}
    prose = "Minted the survey id before launch and navigated to it. "
    yield "workflow-stage", node, {"summary": prose * 120, "changed": [f"src/module_{i}.py" for i in range(30)],
                                   "tests": ["python3 test/run_python.py: 409 passed"]}
    yield "hash-heavy", node, {"summary": " ".join(hashlib.sha1(str(i).encode()).hexdigest() for i in range(60)),
                               "changed": [f"{hashlib.sha1(str(i).encode()).hexdigest()}.json" for i in range(12)],
                               "tests": ["pytest -q " + " ".join(f"t{i}::case_{i}" for i in range(30))]}
    yield "long-tests", node, {"summary": prose * 20, "changed": ["fusion_decisions.py"],
                               "tests": ["PYTHONDONTWRITEBYTECODE=1 python3 test/run_python.py " + "-k test_verdict " * 40] * 8}
    yield "brief", {"task": "Add CSV export with tests: a csv_export module plus unit tests covering escaping and filtering."}, \
        {"summary": "Added csv_export.py; 6 unit tests pass.\n" * 60, "changed": ["csv_export.py"], "tests": ["python -m unittest: 6 passed"]}
    yield "json-summary", {"decision_context": "Truffle scout: shortlist at most 3 tractable open issues in hathbanger/orc."}, \
        {"summary": json.dumps([{"issue": i, "why": "needs a product decision", "quote": "if (!id) return;"} for i in range(80)])}
    yield "unicode-summary", {"task": "Translate the onboarding copy."}, {"summary": "日本語のテキストは長いです。— “quoted” ✓ " * 80}


def measure():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import fusion_laya
    import laya
    from laya.common import build_sequence
    agent = laya.load(str(fusion_laya.checkpoint("english")), device="cpu")
    heads = {}
    for kind, questions in {"acceptance": ACCEPTANCE_QUESTIONS}.items():
        heads[kind] = max(len(build_sequence(agent.tok, "", agent._to_internal(q), agent.cfg.get("max_len", 512),
                                             agent.cfg.get("head_max_len", 192))[0]) for q in questions.values())
    count = lambda text: len(agent.tok.encode(text, add_special_tokens=False))
    samples = [{"name": name, "text": text, "tokens": count(text)} for name, text in texts()]
    states = []
    for name, source, result in acceptance_cases():
        built = acceptance_state(source, result, 2200)
        state = json.dumps(built, ensure_ascii=False, sort_keys=True)
        states.append({"name": name, "source": source, "result": result, "state": state, "tokens": count(state),
                       "source_truncated": bool(built.get("source_truncated")),
                       "model_truncated": fusion_laya.truncated(agent, state, ACCEPTANCE_QUESTIONS)})
    return {"schema": "fusion.laya_token_counts.v1", "model": "english", "max_len": agent.cfg.get("max_len", 512),
            "head_tokens": heads, "samples": samples, "acceptance_states": states}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="re-measure and overwrite the fixture")
    args = parser.parse_args()
    measured = measure()
    for entry in measured["samples"] + measured["acceptance_states"]:
        text = entry.get("text", entry.get("state"))
        print(f"{entry['name']:16s} chars={len(text):5d} tokens={entry['tokens']:4d} estimate={estimated_tokens(text):4d} "
              f"chars/token={len(text) / entry['tokens']:.2f} tokens/estimate={entry['tokens'] / estimated_tokens(text):.3f}")
    if args.write:
        FIXTURE.write_text(json.dumps(measured, ensure_ascii=False, indent=1) + "\n")
        print(f"wrote {FIXTURE}")
        return 0
    failures = []
    if measured["head_tokens"] != {kind: STATE_HEAD_TOKENS[kind] for kind in measured["head_tokens"]}:
        failures.append(f"question heads {measured['head_tokens']} differ from STATE_HEAD_TOKENS {STATE_HEAD_TOKENS}")
    stored = json.loads(FIXTURE.read_text())
    if stored != measured:
        failures.append("fixture differs from the tokenizer or the current acceptance_state; rerun with --write and review the diff")
    for entry in measured["acceptance_states"]:
        if not entry["source_truncated"] and (entry["model_truncated"] or entry["tokens"] > state_tokens("acceptance")):
            failures.append(f"labelable acceptance input {entry['name']} is truncated by the model ({entry['tokens']} tokens)")
    for failure in failures:
        print("FAIL:", failure)
    print("ok" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
