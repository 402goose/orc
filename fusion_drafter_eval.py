"""Evaluate label drafts against human reviews without modifying live evidence."""
import json
from pathlib import Path
import re
import shutil
import tempfile

from fusion_decisions import (DecisionEngine, DecisionStore, STATE_VERSION, acceptance_state,
                              label_provenance, read_jsonl, reviewed_labels, state_cap)
from fusion_labeling import assessment, evidence_bundle


HUMAN_SOURCES = {"human", "human_approved_suggestion"}


def rebuild_acceptance(workspace, record, engine):
    import fusion_core as core
    run_id = record.get("context", {}).get("task_id")
    directory = core.run_directory(workspace, run_id)
    if directory is None:
        raise ValueError(f"Saved run not found: {run_id}")
    task = json.loads((directory / "task.json").read_text())
    result = json.loads((directory / "result.json").read_text())
    if not isinstance(task, dict) or not isinstance(result, dict):
        raise ValueError("Saved task and result must be JSON objects")
    answer = (directory / "answer.md").read_text()
    # Older workflow tasks saved the wrapper, before node_task was persisted.
    if not task.get("node_task") and str(task.get("task", "")).startswith("You are node "):
        match = re.search(r"\nTask: (.*?)\n\nDependency artifacts:", task["task"], re.S)
        if not match:
            raise ValueError("Cannot recover the saved workflow node task")
        task["node_task"] = match[1].strip()
    state = acceptance_state(task, result, state_cap(engine.options), engine.state_tokens("acceptance"),
                             answer_text=answer)
    rebuilt = {**record, "state_version": STATE_VERSION, "state_tokens": engine.state_tokens("acceptance")}
    engine.encode(rebuilt, state)
    return rebuilt


def evaluate_drafter(workspace, config, agent="auto", rebuild_input=False, limit=None):
    import fusion_core as core
    if limit is not None and limit < 1:
        raise ValueError("--limit must be positive")
    live = DecisionStore(workspace)
    rows = []
    totals = {"decisions": 0, "agree": 0, "disagree": 0, "abstain": 0, "errors": 0,
              "exclusions_abstained": 0, "exclusions_answered": 0}
    questions = {}
    with tempfile.TemporaryDirectory(prefix="fusion-eval-drafter-") as temporary:
        scratch = Path(temporary)
        store = DecisionStore(scratch)
        if live.root.exists():
            shutil.copytree(live.root, store.root)
        events = read_jsonl(store.path)
        labels, exclusions = reviewed_labels(events)
        provenance = label_provenance(events)
        exclusion_sources = {e["id"]: e.get("source", "human") for e in events
                             if e.get("event") == "label_exclusion"}
        # Repeated decision events represent revisions of the same input.
        records = {e["id"]: e for e in events if e.get("event") == "decision"}
        engine = DecisionEngine(scratch, config)
        # Relative checkpoint directories belong to the original workspace;
        # retain their limits while all store writes target the temporary copy.
        engine.options = DecisionEngine(workspace, config).options
        for identifier, record in records.items():
            expected = {key: value for key, value in labels.get(identifier, {}).items()
                        if provenance.get(identifier, {}).get(key, {}).get("source") in HUMAN_SOURCES}
            excluded = exclusions.get(identifier, False)
            if excluded:
                if exclusion_sources.get(identifier) not in HUMAN_SOURCES:
                    continue
            elif not expected:
                continue
            if limit is not None and len(rows) >= limit:
                break
            row = {"id": identifier, "kind": record["kind"], "excluded": excluded,
                   "rebuilt": False, "questions": {}}
            totals["decisions"] += 1
            try:
                if rebuild_input and record["kind"] == "acceptance":
                    record = rebuild_acceptance(workspace, record, engine)
                    row["rebuilt"] = True
                    store.append("decision", **{k: v for k, v in record.items() if k != "event"})
                sources = evidence_bundle(workspace, record)
                # RunStore honors a control-workspace context or environment override.
                # Override it explicitly so even dispatch artifacts stay in the copy.
                token = core._CONTROL_WORKSPACE.set(scratch)
                try:
                    draft = assessment(scratch, config, record, sources, agent)
                finally:
                    core._CONTROL_WORKSPACE.reset(token)
                if draft["status"] != "success":
                    raise ValueError(draft.get("error", "Drafter failed"))
                row.update(status="success", answers=draft["answers"], abstentions=draft["abstentions"],
                           state_version=record.get("state_version", 0))
                if excluded:
                    row["abstained"] = not draft["answers"]
                    totals["exclusions_abstained" if row["abstained"] else "exclusions_answered"] += 1
                else:
                    for key, value in expected.items():
                        actual = draft["answers"].get(key, {}).get("value")
                        score = "abstain" if actual is None else "agree" if actual == value else "disagree"
                        row["questions"][key] = {"expected": value, "actual": actual, "score": score}
                        questions.setdefault(key, {"agree": 0, "disagree": 0, "abstain": 0})[score] += 1
                        totals[score] += 1
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row.update(status="error", error=str(exc))
                totals["errors"] += 1
            rows.append(row)
    return {"schema": "fusion.drafter-eval.v1", "agent": agent, "rebuild_input": rebuild_input,
            "state_version": STATE_VERSION, "results": rows, "questions": questions, "totals": totals}


def format_report(report):
    lines = ["Question                         Agree  Disagree  Abstain"]
    for key, counts in sorted(report["questions"].items()):
        lines.append(f"{key:32} {counts['agree']:5} {counts['disagree']:9} {counts['abstain']:8}")
    totals = report["totals"]
    lines.append(f"{'TOTAL':32} {totals['agree']:5} {totals['disagree']:9} {totals['abstain']:8}")
    lines.append(f"Human exclusions: {totals['exclusions_abstained']} abstained; {totals['exclusions_answered']} answered")
    lines.append(f"Decisions: {totals['decisions']}; errors: {totals['errors']}")
    lines.extend(f"{row['id']}: {row['error']}" for row in report["results"] if row["status"] == "error")
    return "\n".join(lines)
