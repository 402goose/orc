"""Fusion's deterministic constraints around optional learned decisions."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import time

from fusion_decisions import DecisionEngine, DecisionStore, ACCEPTANCE_QUESTIONS, RECOVERY_QUESTIONS, REVIEW_QUESTIONS, read_jsonl
import fusion_progress as progress


def context(task):
    return {"task_id": task["run_id"], "group": task.get("parent_task_id") or task["run_id"]}


def route_candidates(config, task, store, rejected=None):
    """Pass `rejected` to collect why each lane was dropped. The reasons live
    beside the checks that produce them so an explanation can never drift from
    the filter it is explaining."""
    import fusion_core as core

    def drop(key, why):
        if rejected is not None:
            rejected.setdefault(key, why)
    yolo = core.execution_mode(config) == "yolo"
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
        if key not in seen and 0 <= time.time() * 1000 - span.get("end_time_ms", 0) < core.LANE_COOLDOWN_SECONDS * 1000:
            if span.get("failure_class") == "quota" or (span.get("failure_class") == "permission_denied" and span.get("execution_mode", "restricted") == core.execution_mode(config)):
                unhealthy.add(key)
                if family not in seen_commands and Path(str(family[1])).name != "orc":
                    unavailable_commands.add(family)
        seen.add(key)
        seen_commands.add(family)
    choices = []
    preferred = config.get("sidekick", "codex")
    candidates = [(name, name, None) for name in dict.fromkeys([preferred, "codex", "claude", "agy", "grok"])]
    candidates += [(name, settings.get("agent"), name) for name, settings in config.get("routes", {}).items()]
    if task.get("prefer_different_agent"):
        candidates.sort(key=lambda item: item[1] == task["prefer_different_agent"])
    for key, agent, route in candidates:
        if key in unhealthy:
            drop(key, "a recent run on this lane hit a quota or permission limit")
            continue
        if agent not in {"claude", "codex", "agy", "grok"}:
            continue
        settings = core.agent_settings(config, {"agent": agent, "route": route})
        if settings.get("reasoning_effort") is not None:
            from fusion_reasoning import native_capability, validate_pair
            try:
                if agent != "codex":
                    raise ValueError("reasoning effort requires native Codex")
                if settings["reasoning_effort"] == "ultra" and (task.get("write") or settings.get("allow_native_delegation") is not True):
                    raise ValueError("ultra requires read-only scope and explicit native delegation")
                native_capability(validate_pair(settings.get("model"), settings["reasoning_effort"]))
            except ValueError as exc:
                drop(key, str(exc))
                continue
        if (agent, settings.get("command", agent)) in unavailable_commands:
            drop(key, f"{settings.get('command', agent)} is already known to be unavailable this run")
            continue
        if not core.executable(settings.get("command", agent)):
            drop(key, f"{settings.get('command', agent)} is not on PATH")
            continue
        if not yolo and agent == "agy" and not core.agy_headless_status(settings)["automatic_ready"]:
            drop(key, core.agy_headless_status(settings).get("reason", "agy is not ready for headless use"))
            continue
        # Named read-only routes cannot be repurposed into writer routes.
        if not yolo and task.get("write") and (settings.get("sandbox") == "read-only" or settings.get("permission_mode") == "plan" or settings.get("mode") == "plan"):
            drop(key, "read-only or plan-mode lane cannot take a task that writes")
            continue
        if not yolo and not task.get("write") and settings.get("mode") in {"yolo", "auto", "accept-edits"}:
            drop(key, f"{settings.get('mode')} mode is too permissive for a read-only task")
            continue
        command = str(settings.get("command", agent))
        if Path(command).name == "orc":
            # Automatic ORC routes require passing tool-fit evidence even for explicit models.
            model = settings.get("model")
            if model:
                if model not in core._orc_model_ids(command, ["--fit"]):
                    drop(key, f"{model} has no passing `orc probe --fit` evidence")
                    continue
            else:
                try:
                    model = core.select_orc_model(command, str(settings.get("model_selector", "best")), False)
                except (ValueError, RuntimeError, OSError) as exc:
                    drop(key, f"no ORC model passed tool-fit for this route ({exc})")
                    continue
            if not model:
                drop(key, "no ORC model passed tool-fit for this route")
                continue
        else:
            model = settings.get("model", "")
        spans = history[key]
        verified = [outcomes[span["run_id"]] for span in spans if span.get("run_id") in outcomes]
        costs = [core.number(span["usage"].get("cost_usd", span["usage"].get("cost", 0))) for span in spans
                 if "cost_usd" in span.get("usage", {}) or "cost" in span.get("usage", {})]
        mean_cost = sum(costs) / len(costs) if costs else None
        if task.get("budget_remaining_usd") is not None and mean_cost is not None and mean_cost > task["budget_remaining_usd"]:
            drop(key, f"its average reported cost ${mean_cost:.4f} exceeds the ${task['budget_remaining_usd']:.4f} left in the budget")
            continue
        choices.append({"key": key, "agent": agent, "route": route, "model": model,
                        **({"reasoning_effort": settings["reasoning_effort"]} if settings.get("reasoning_effort") is not None else {}),
                        "runs": len(spans), "reported_success_rate": sum(s.get("status") == "success" for s in spans) / len(spans) if spans else None,
                        "checked_runs": len(verified), "acceptance_rate": sum(verified) / len(verified) if verified else None,
                        "mean_cost_usd": mean_cost,
                        "mean_ms": sum(s.get("duration_ms", 0) for s in spans) / len(spans) if spans else None})
        if len(choices) == 8:
            break
    return choices


def no_route_reason(config, task, store):
    """Say which lane was dropped and why, instead of only that none survived.

    Without this the first real failure on an installed machine is an
    implementation node reporting that no route is available, with nothing
    naming the binary that is missing or the read-only lane that cannot write.
    """
    rejected = {}
    route_candidates(config, task, store, rejected)
    detail = "; ".join(f"{key}: {why}" for key, why in sorted(rejected.items())) or "no lane is configured"
    scope = "that can write" if task.get("write") else "for a read-only task"
    return (f"no permitted worker is available {scope} -- {detail}. "
            "Run `fusion doctor` to see every lane and its command.")


def route_task(config, task, store):
    """Record advice for explicit routing, apply only to a genuinely automatic lane."""
    engine = DecisionEngine(task["workspace"], config)
    automatic = task["agent"] == "auto" and not task.get("route")
    if task["agent"] == "auto" and task.get("route"):
        task["agent"] = config.get("routes", {}).get(task["route"], {}).get("agent")
        if task["agent"] not in {"codex", "claude", "agy", "grok"}:
            raise ValueError("automatic task specifies an unknown route")
    if not automatic and engine.options["mode"] == "off":
        return
    if automatic and task.get("settings_overrides"):
        raise ValueError("agent=auto cannot use per-agent settings overrides; configure named routes")
    # Explicit lanes are never silently substituted, even if unhealthy.
    with progress.activity(task.get("progress_label", task["role"]), "checking available workers and model fit" if automatic else "checking selected worker"):
        if automatic:
            candidates = route_candidates(config, task, store)
        else:
            import fusion_core as core
            from fusion_reasoning import pair_candidates, pair_key
            settings = core.agent_settings(config, task)
            pairs = pair_candidates(config, settings, task.get("write", False)) if task["agent"] == "codex" else []
            candidates = [{"key": pair_key(pair), "agent": "codex", "route": task.get("route"), **pair} for pair in pairs] or [{
                "key": task.get("route") or task["agent"], "agent": task["agent"], "route": task.get("route"),
                "model": settings.get("model", ""),
                **({"reasoning_effort": settings["reasoning_effort"]} if settings.get("reasoning_effort") is not None else {}),
            }]
    if not candidates:
        raise ValueError(no_route_reason(config, task, store))
    selected, applied, record = candidates[0], False, None
    # A single candidate is not a choice: nothing to record, and Laya rejects one-option choices.
    if len(candidates) > 1:
        questions = {"route": {"type": "choice", "instructions": "Choose a capable permitted worker for the goal and observed evidence; unknown metrics are unknown.",
                               "criteria": {c["key"]: f"{c['agent']} {c.get('model') or 'configured default'}" +
                                            (f" / {c['reasoning_effort']} effort" if c.get("reasoning_effort") else "") for c in candidates}}}
        if any(c.get("reasoning_effort") for c in candidates):
            questions["route"]["instructions"] = "Choose one model and effort pair for the task. Prioritize correctness; effort names are not comparable across models. Unknown outcomes, latency and cost are unknown."
        record = engine.decide("routing", {"task": task.get("decision_context", task["task"]), "write": task["write"],
                                          "goal": config.get("decisions", {}).get("routing_goal", "quality"),
                                          "budget_remaining_usd": task.get("budget_remaining_usd"), "candidates": candidates}, questions, context(task))
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
            if selected.get("reasoning_effort"):
                task["settings_overrides"]["reasoning_effort"] = selected["reasoning_effort"]
    if record:
        task.setdefault("decisions", {})["routing"] = record["id"]
        engine.applied(record, selected["key"], applied, "qualified automatic route" if applied else "explicit route or pair retained; advice does not change dispatch")


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
        import fusion_core as core
        delegation = core.agent_settings(config, task).get("allow_native_delegation") is True
        task["task"] += "\nReview focus: " + instructions[selected] + "\nReport unresolved findings as STATUS: blocked." + ("" if delegation else " Do not delegate further.")
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
    if engine.allowed(record, "plausible") and record["recommendations"]["plausible"]["value"] == "false":
        plausible, applied = False, True
    engine.applied(record, "accept" if plausible else "reject", applied,
                   "qualified active classification" if applied else "shadow mode or unqualified recommendation; structural acceptance stands")
    return plausible, record["id"]


def recovery(config, workspace, workflow_id, node, result, accepted, max_attempts):
    import fusion_core as core
    failure = core.failure_class(result)
    if failure == "coordinator_error":
        progress.emit(node.get("id", "workflow"), "Fusion snapshot failed; repair the coordinator error, then resume this stage. Accepted stages remain saved.")
        return "stop", None
    engine = DecisionEngine(workspace, config)
    repeated = bool(node.get("repeated_failure"))
    can_retry = node["attempts"] < max_attempts and not repeated
    actual = "continue" if accepted else "stop" if failure in {"quota", "permission_denied"} or not can_retry else "repair"
    automatic = node["agent"] == "auto" and not node.get("route")
    if not accepted and failure == "quota" and automatic:
        failed_route = result.get("route") or result.get("agent")
        excluded = node.setdefault("excluded_routes", [])
        if failed_route and failed_route not in excluded:
            excluded.append(failed_route)
        candidate_task = {"workspace": workspace, "write": node.get("write", False), "excluded_routes": excluded}
        if can_retry and route_candidates(config, candidate_task, core.RunStore(workspace)):
            actual = "switch"
    record = engine.decide("recovery", {"status": result.get("status"), "accepted": accepted,
                                       "failure": failure, "blockers": result.get("blockers", []),
                                       "attempt": node["attempts"], "max_attempts": max_attempts,
                                       "repeated_failure": repeated,
                                       "automatic_lane": node["agent"] == "auto"}, RECOVERY_QUESTIONS,
                           {"task_id": result.get("run_id"), "group": workflow_id})
    applied = False
    if not accepted and failure != "permission_denied" and engine.allowed(record, "action"):
        suggested = record["recommendations"]["action"]["value"]
        if suggested in {"ask", "stop"} or (can_retry and suggested == "repair" and failure != "quota"):
            actual, applied = suggested, True
        elif can_retry and suggested == "switch" and node["agent"] == "auto" and not node.get("route"):
            actual, applied = "switch", True
            excluded = node.setdefault("excluded_routes", [])
            lane = result.get("route") or result.get("agent")
            if lane not in excluded:
                excluded.append(lane)
    engine.applied(record, actual, applied, "acceptance, permissions and attempt limits enforced")
    if engine.options["mode"] != "off":
        engine.store.append("outcome", task_id=result.get("run_id"), group=workflow_id, accepted=accepted,
                            status=result.get("status"), evidence=str(workspace / ".fusion" / "workflows" / workflow_id / "nodes" / node.get("id", "unknown") / "node.json"))
    return actual, record["id"]
