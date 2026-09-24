"""Bounded label evidence and completion. Never executes a worker or approves a label."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from fusion_decisions import DecisionStore, digest, read_jsonl

SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
FILE_LIMIT = 2_000_000
TOTAL_LIMIT = 4_000_000
SOURCE_LIMIT = 32


def _read(workspace, relative, limit=FILE_LIMIT):
    """No symlinks, nonregular files, traversal, or unbounded reads."""
    workspace = Path(workspace).resolve()
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {".", ".."} for p in parts):
        raise ValueError("Evidence paths must be plain workspace-relative paths")
    path = workspace
    for part in parts:
        path /= part
        if path.is_symlink():
            raise ValueError("Label evidence cannot follow symlinks")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Label evidence must be a regular file")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Label evidence exceeds byte limit")
    return raw


def decision_source(workspace, decision_id):
    if not isinstance(decision_id, str) or not SAFE_ID.fullmatch(decision_id):
        raise ValueError("Invalid decision ID")
    raw = _read(workspace, ".fusion/decisions/events.jsonl", 64_000_000)
    selected = None
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(record, dict) and record.get("event") == "decision" and record.get("id") == decision_id:
            selected = (record, hashlib.sha256(line).hexdigest())
    if selected is None:
        raise ValueError("Unknown decision")
    if selected[0].get("status") != "ok" or selected[0].get("truncated"):
        raise ValueError("Suggestions require a successful decision with complete input")
    return selected


def collect_sources(workspace, record):
    """Only this attempt's handoff and independently recorded coordinator checks.

    These remain local mutable artifacts. A source hash freezes what was read;
    it does not establish that a workspace cannot forge a receipt.
    """
    workspace = Path(workspace).resolve()
    context = record.get("context") or {}
    task_id, group = context.get("task_id"), context.get("group")
    if not isinstance(task_id, str) or not SAFE_ID.fullmatch(task_id):
        return []
    sources, total = [], 0

    def add(relative, kind, raw=None):
        nonlocal total
        raw = _read(workspace, relative) if raw is None else raw
        total += len(raw)
        if total > TOTAL_LIMIT or len(sources) >= SOURCE_LIMIT:
            raise ValueError("Label evidence packet exceeds its bounded source budget")
        sources.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "kind": kind, "raw": raw})

    for name in ("result.json", "answer.md"):
        path = f".fusion/runs/{task_id}/{name}"
        try:
            raw = _read(workspace, path)
        except FileNotFoundError:
            continue
        # Never convert malformed worker JSON into an apparently empty claim.
        if name == "result.json" and not isinstance(json.loads(raw), dict):
            raise ValueError("Worker result evidence is not an object")
        add(path, "worker_claim", raw)
    if not isinstance(group, str) or not SAFE_ID.fullmatch(group):
        return sources
    root = workspace / ".fusion/workflows" / group / "nodes"
    for path in sorted(root.glob("*/acceptance/attempt-*/check-*/receipt.json")):
        relative = path.relative_to(workspace).as_posix()
        raw = _read(workspace, relative)
        receipt = json.loads(raw)
        if not isinstance(receipt, dict) or receipt.get("schema") != "fusion.acceptance-check.v1":
            raise ValueError("Malformed coordinator acceptance receipt")
        if receipt.get("run_id") != task_id:
            continue
        parts = Path(relative).parts
        if (receipt.get("workflow_id") != group or receipt.get("node_id") != parts[4]
                or parts[6] != f"attempt-{receipt.get('attempt')}"):
            raise ValueError("Coordinator receipt does not match its attempt identity")
        add(relative, "coordinator_check", raw)
        for name in ("stdout", "stderr"):
            output = (receipt.get("outputs") or {}).get(name)
            if output is None:
                continue  # An interrupted check is still an observation, not a pass.
            output_path = path.with_name(name + ".log")
            if not isinstance(output, dict) or output.get("path") != str(output_path):
                raise ValueError("Coordinator output path does not match its receipt")
            output_relative = output_path.relative_to(workspace).as_posix()
            output_raw = _read(workspace, output_relative)
            if hashlib.sha256(output_raw).hexdigest() != output.get("sha256") or len(output_raw) != output.get("bytes"):
                raise ValueError("Coordinator output hash does not match its receipt")
            add(output_relative, "coordinator_output", output_raw)
    return sources


def evidence_sources(workspace, record):
    sources = [{"id": "E1", "title": "Original decision input", "kind": "original_input", "text": record["state"]}]
    remaining = 24000
    for source in collect_sources(workspace, record):
        text = source["raw"].decode("utf-8", "replace")
        if source["path"].endswith("/result.json"):
            result = json.loads(text)
            text = json.dumps({key: result.get(key) for key in ("status", "summary", "changed", "tests", "blockers", "exit_code")}, ensure_ascii=False)
        count = min(16000, remaining)
        preview = text[:count]
        remaining -= len(preview)
        sources.append({"id": f"E{len(sources) + 1}", "title": source["path"], "path": source["path"],
                        "sha256": source["sha256"], "kind": source["kind"], "text": preview, "truncated": len(text) > count,
                        "timing": "Supplemental outcome; may postdate the original decision"})
    return sources


def draft_request(workspace, decision_id, model=None, reasoning_effort=None):
    record, fingerprint = decision_source(workspace, decision_id)
    refs = [{key: source[key] for key in ("path", "sha256")} for source in collect_sources(workspace, record)]
    task = f"Draft labels for decision {decision_id} from frozen evidence."
    node = {"id": "label", "role": "labeling", "agent": "codex", "write": False, "task": task,
            "acceptance": {"required_handoff": ["summary"]}}
    if model is not None or reasoning_effort is not None:
        from fusion_reasoning import validate_pair
        node.update(validate_pair(model, reasoning_effort))
        if reasoning_effort == "ultra":
            raise ValueError("Label drafts require a non-ultra pair without native delegation")
    return {"run_id": "label-" + fingerprint[:56], "label_draft": {
        "schema": "fusion.label-draft-source.v1", "decision_id": decision_id,
        "decision_line_sha256": fingerprint, "sources": refs},
        "spec": {"schema": "fusion.workflow.v1", "task": task, "nodes": [node], "max_parallel": 1,
                 "max_parallel_writers": 0, "max_attempts": 1, "budget_usd": 1, "publish": {"mode": "off"}}}


def frozen_packet(workspace, response):
    artifact = response.get("artifacts", {}).get("label_draft")
    if not isinstance(artifact, dict):
        raise ValueError("Admission has no frozen label draft packet")
    path = Path(artifact["path"])
    if not path.is_absolute() or path.is_symlink() or path.resolve().is_relative_to(Path(workspace).resolve()):
        raise ValueError("Label draft packet must be outside the worker workspace")
    with path.open("rb") as handle:
        raw = handle.read(2_000_001)
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
        raise ValueError("Frozen label packet changed")
    packet = json.loads(raw)
    if packet.get("schema") != "fusion.label-draft-packet.v1":
        raise ValueError("Unsupported frozen label packet schema")
    return packet


def finalize(workspace, run_id):
    """Retry only this local completion step after a terminal provider receipt.

    A missing/claimed/failed receipt never causes execution or implicit recovery.
    The review lock prevents concurrent duplicate events; if the event was saved
    before a process interruption, the same suggestion is returned on retry.
    """
    import fusion_admission as admission
    from fusion_labeling import parse_suggestion
    if not isinstance(run_id, str) or not re.fullmatch(r"label-[a-f0-9]{56}", run_id):
        raise ValueError("Invalid admitted label run ID")
    response = admission.call(workspace, "status", run_id=run_id)
    packet = frozen_packet(workspace, response)
    if not response.get("terminal") or response.get("status") != "success":
        raise ValueError("Label worker has no successful terminal admission receipt")
    record = packet["decision"]
    current_record, fingerprint = decision_source(workspace, record["id"])
    if fingerprint != packet["decision_line_sha256"] or run_id != "label-" + fingerprint[:56]:
        raise ValueError("Original decision changed after admission")
    store = DecisionStore(workspace)
    with store.review_lock():
        existing = next((event for event in reversed(read_jsonl(store.path))
                         if event.get("event") == "label_suggestion" and event.get("admission_run_id") == run_id), None)
        if existing:
            if existing.get("admission_request_sha256") != response["request_sha256"]:
                raise ValueError("Existing draft has different admission provenance")
            return {"decision_id": record["id"], **existing, "approval": {"status": "needs_review"}, "recovered": True}
        manifest_raw = _read(workspace, f".fusion/workflows/{run_id}/manifest.json")
        manifest = json.loads(manifest_raw)
        if manifest.get("schema") != "fusion.workflow.v1" or manifest.get("workflow_id") != run_id:
            raise ValueError("Label workflow identity does not match admission")
        node = (manifest.get("nodes") or {}).get("label") or {}
        result = node.get("result") or {}
        worker_id = result.get("run_id")
        if (node.get("status") != "success" or result.get("status") != "success" or result.get("exit_code") != 0
                or not isinstance(worker_id, str) or not SAFE_ID.fullmatch(worker_id)):
            raise ValueError("Completed label workflow has no successful worker evidence")
        task_raw = _read(workspace, f".fusion/runs/{worker_id}/task.json")
        task = json.loads(task_raw)
        if task.get("parent_task_id") != run_id or task.get("agent") != "codex" or task.get("role") != "labeling" or task.get("write") is not False:
            raise ValueError("Label worker does not belong to this admitted read-only attempt")
        answer_raw = _read(workspace, f".fusion/runs/{worker_id}/answer.md")
        answer = answer_raw.decode("utf-8")
        parsed = parse_suggestion(answer, record, packet["sources"])
        teacher_answers = parsed["answers"]
        # Success of a chosen option is not a counterfactual comparison.
        if record.get("kind") == "routing" and packet.get("comparative_evidence") != "matched":
            parsed = {"answers": {}, "abstentions": {key: "No matched comparative evidence establishes the best route/model/effort pair."
                                                     for key in record["questions"]}}
        suggestion = {"suggestion_id": run_id, "decision_hash": digest({key: current_record.get(key) for key in ("state", "questions", "schema_hash")}),
                      **parsed, "agent": result.get("agent"), "model": result.get("model"), "run_id": worker_id,
                      "usage": result.get("usage"), "execution_choice": result.get("execution_choice"),
                      "sources": packet["sources"], "labeling_mode": "single", "approval_mode": "human", "verified": False,
                      "admission_run_id": run_id, "admission_request_sha256": response["request_sha256"],
                      "source_decision_line_sha256": fingerprint, "teacher_answers": teacher_answers}
        suggestion["teacher_artifacts"] = {
            "workflow_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "worker_task_sha256": hashlib.sha256(task_raw).hexdigest(),
            "answer_sha256": hashlib.sha256(answer_raw).hexdigest()}
        store.append("label_suggestion", id=record["id"], **suggestion)
        return {"decision_id": record["id"], **suggestion, "approval": {"status": "needs_review"}, "recovered": False}
