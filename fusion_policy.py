"""Fusion's deterministic constraints around optional learned decisions."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import time

from fusion_decisions import DecisionEngine, DecisionStore, ACCEPTANCE_QUESTIONS, RECOVERY_QUESTIONS, REVIEW_QUESTIONS, read_jsonl
import fusion_progress as progress


def context(task):
    return {"task_id": task["run_id"], "group": task.get("parent_task_id") or task["run_id"]}


def route_candidates(config, task, store):
    import fusion_core as core
    history = defaultdict(list)
    outcomes = {event["task_id"]: event["accepted"] for event in read_jsonl(DecisionStore(task["workspace"]).path)
                if event.get("event") == "outcome"}
    unhealthy = set(task.get("excluded_routes", []))
    unavailable_commands = set()
    for excluded in unhealthy:
        lane = config.get("routes", {}).get(excluded, {})
        agent = lane.get("agent", excluded)
        settings = {**config.get(agent, {}), **lane}
        if Path(str(settings.get("command", agent))).name != "orc":
            unavailable_commands.add((agent, settings.get("command", agent)))
    seen = set()
    seen_commands = set()
    for span in store.traces(limit=200):
        key = span.get("route") or span.get("agent")
        if not key:
            continue
        history[key].append(span)
        agent = span.get("agent")
        lane = config.get("routes", {}).get(span.get("route"), {})
        settings = {**config.get(agent, {}), **lane}
        family = (agent, settings.get("command", agent))
        if key not in seen and 0 <= time.time() * 1000 - span.get("end_time_ms", 0) < 900_000:
            if span.get("failure_class") in {"quota", "permission_denied"}:
                unhealthy.add(key)
                if family not in seen_commands and Path(str(family[1])).name != "orc":
                    unavailable_commands.add(family)
        seen.add(key)
        seen_commands.add(family)
    choices = []
    preferred = config.get("sidekick", "codex")
    candidates = [(name, name, None) for name in dict.fromkeys([preferred, "codex", "claude", "agy"])]
    candidates += [(name, settings.get("agent"), name) for name, settings in config.get("routes", {}).items()]
    if task.get("prefer_different_agent"):
        candidates.sort(key=lambda item: item[1] == task["prefer_different_agent"])
    for key, agent, route in candidates:
        if key in unhealthy or agent not in {"claude", "codex", "agy"}:
            continue
        settings = core.agent_settings(config, {"agent": agent, "route": route})
        if (agent, settings.get("command", agent)) in unavailable_commands:
            continue
        if not core.executable(settings.get("command", agent)):
            continue
        # Named read-only routes cannot be repurposed into writer routes.
        if task.get("write") and (settings.get("sandbox") == "read-only" or settings.get("permission_mode") == "plan" or settings.get("mode") == "plan"):
            continue
        if not task.get("write") and settings.get("mode") in {"yolo", "auto", "accept-edits"}:
            continue
        command = str(settings.get("command", agent))
        if Path(command).name == "orc":
            # Automatic ORC routes require passing tool-fit evidence even for explicit models.
            model = settings.get("model")
            if model:
                if model not in core._orc_model_ids(command, ["--fit"]):
                    continue
            else:
                try:
                    model = core.select_orc_model(command, str(settings.get("model_selector", "best")), False)
                except (ValueError, RuntimeError, OSError):
                    continue
            if not model:
                continue
        else:
            model = settings.get("model", "")
        spans = history[key]
        verified = [outcomes[span["run_id"]] for span in spans if span.get("run_id") in outcomes]
        costs = [core.number(span["usage"].get("cost_usd", span["usage"].get("cost", 0))) for span in spans
                 if "cost_usd" in span.get("usage", {}) or "cost" in span.get("usage", {})]
        mean_cost = sum(costs) / len(costs) if costs else None
        if task.get("budget_remaining_usd") is not None and mean_cost is not None and mean_cost > task["budget_remaining_usd"]:
            continue
        choices.append({"key": key, "agent": agent, "route": route, "model": model,
                        "runs": len(spans), "reported_success_rate": sum(s.get("status") == "success" for s in spans) / len(spans) if spans else None,
                        "checked_runs": len(verified), "acceptance_rate": sum(verified) / len(verified) if verified else None,
                        "mean_cost_usd": mean_cost,
                        "mean_ms": sum(s.get("duration_ms", 0) for s in spans) / len(spans) if spans else None})
        if len(choices) == 8:
            break
    return choices


def route_task(config, task, store):
    """Record advice for explicit routing, apply only to a genuinely automatic lane."""
    engine = DecisionEngine(task["workspace"], config)
    automatic = task["agent"] == "auto" and not task.get("route")
    if task["agent"] == "auto" and task.get("route"):
        task["agent"] = config.get("routes", {}).get(task["route"], {}).get("agent")
        if task["agent"] not in {"codex", "claude", "agy"}:
            raise ValueError("automatic task specifies an unknown route")
    if not automatic and engine.options["mode"] == "off":
        return
    if automatic and task.get("settings_overrides"):
        raise ValueError("agent=auto cannot use per-agent settings overrides; configure named routes")
    # Explicit lanes are never silently substituted, even if unhealthy.
    with progress.activity(task.get("progress_label", task["role"]), "checking available workers and model fit" if automatic else "checking selected worker"):
        candidates = route_candidates(config, task, store) if automatic else [{
            "key": task.get("route") or task["agent"], "agent": task["agent"], "route": task.get("route"),
            "model": task.get("settings_overrides", {}).get("model", ""),
        }]
    if not candidates:
        raise ValueError("no healthy, permitted agent route is available")
    questions = {"route": {"type": "choice", "instructions": "Choose a capable permitted worker for the goal and observed evidence; unknown metrics are unknown.",
                           "criteria": {c["key"]: f"{c['agent']} {c.get('model') or 'configured default'}" for c in candidates}}}
    record = engine.decide("routing", {"task": task.get("decision_context", task["task"]), "write": task["write"],
                                      "goal": config.get("decisions", {}).get("routing_goal", "quality"),
                                      "budget_remaining_usd": task.get("budget_remaining_usd"), "candidates": candidates}, questions, context(task))
    selected, applied = candidates[0], False
    if automatic and engine.allowed(record, "route"):
        value = record["recommendations"]["route"]["value"]
        selected = next(c for c in candidates if c["key"] == value)
        applied = True
    if automatic:
        task["requested_agent"] = "auto"
        task["agent"], task["route"] = selected["agent"], selected["route"]
        # Sessions belong to a worker/model. A switched lane starts a separate session.
        task["session_key"] += ":" + selected["key"] + ":" + str(selected.get("model", ""))
        if selected.get("model"):
            task["settings_overrides"] = {"model": selected["model"]}
    task.setdefault("decisions", {})["routing"] = record["id"]
    engine.applied(record, selected["key"], applied, "qualified automatic route" if applied else "explicit route or deterministic fallback")


def review_task(config, task):
    engine = DecisionEngine(task["workspace"], config)
    record = engine.decide("review", {"task": task.get("decision_context", task["task"]), "role": task["role"], "write": task["write"]}, REVIEW_QUESTIONS, context(task))
    selected = "general"
    applied = engine.allowed(record, "specialty")
    if applied:
        selected = record["recommendations"]["specialty"]["value"]
    instructions = {
        "general": "Inspect correctness, scope, regressions and test evidence.",
        "security": "Also inspect authentication, authorization, secret handling, injection and trust boundaries.",
        "payments": "Also inspect amounts, currencies, rounding, idempotency, retries, reconciliation and failure atomicity.",
        "data": "Also inspect schema compatibility, migrations, rollback, concurrency and data integrity.",
    }
    # Classification only adds scrutiny; it never removes the requested review or grants writes.
    if applied:
        task["task"] += "\nReview focus: " + instructions[selected] + "\nReport unresolved findings as STATUS: blocked. Do not delegate further."
    task.setdefault("decisions", {})["review"] = record["id"]
    engine.applied(record, selected, applied)


def accept_node(config, workspace, workflow_id, node, result):
    """A semantic Done-check, run only after every structural acceptance check
    already passed. A classifier verdict can add scrutiny -- reject a node
    whose artifact is syntactically valid but substantively off-task -- but
    it can never accept on its own; a structural failure is decided before
    this is ever called, and this function has no path back to True from one."""
    engine = DecisionEngine(workspace, config)
    record = engine.decide("acceptance", {"task": node.get("decision_context", node["task"]),
                                         "summary": result.get("summary"), "changed": result.get("changed", []),
                                         "tests": result.get("tests", [])},
                           ACCEPTANCE_QUESTIONS, {"task_id": result.get("run_id"), "group": workflow_id})
    plausible, applied = True, False
    answers = record["recommendations"]
    if (engine.allowed(record, "plausible") and engine.allowed(record, "off_task")
            and answers["plausible"]["value"] == "false" and answers["off_task"]["value"] == "true"):
        plausible, applied = False, True
    engine.applied(record, "accept" if plausible else "reject", applied,
                   "qualified active classification" if applied else "shadow mode or unqualified recommendation; structural acceptance stands")
    return plausible, record["id"]


def recovery(config, workspace, workflow_id, node, result, accepted, max_attempts):
    import fusion_core as core
    engine = DecisionEngine(workspace, config)
    failure = core.failure_class(result)
    can_retry = node["attempts"] < max_attempts
    actual = "continue" if accepted else "stop" if failure in {"quota", "permission_denied"} or not can_retry else "repair"
    record = engine.decide("recovery", {"status": result.get("status"), "accepted": accepted,
                                       "failure": failure, "blockers": result.get("blockers", []),
                                       "attempt": node["attempts"], "max_attempts": max_attempts,
                                       "automatic_lane": node["agent"] == "auto"}, RECOVERY_QUESTIONS,
                           {"task_id": result.get("run_id"), "group": workflow_id})
    applied = False
    if not accepted and failure != "permission_denied" and engine.allowed(record, "action"):
        suggested = record["recommendations"]["action"]["value"]
        if suggested in {"ask", "stop"} or (can_retry and suggested == "repair" and failure != "quota"):
            actual, applied = suggested, True
        elif can_retry and suggested == "switch" and node["agent"] == "auto" and not node.get("route"):
            actual, applied = "switch", True
            node.setdefault("excluded_routes", []).append(result.get("route") or result.get("agent"))
    engine.applied(record, actual, applied, "acceptance, permissions and attempt limits enforced")
    if engine.options["mode"] != "off":
        engine.store.append("outcome", task_id=result.get("run_id"), group=workflow_id, accepted=accepted,
                            status=result.get("status"), evidence=str(workspace / ".fusion" / "workflows" / workflow_id / "nodes" / node.get("id", "unknown") / "node.json"))
    return actual, record["id"]
