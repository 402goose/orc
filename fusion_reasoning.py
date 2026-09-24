"""Explicit model/effort pairs for native Codex; no runtime or routing authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


def validate_pair(model, effort):
    if not isinstance(model, str) or not model.strip():
        raise ValueError("reasoning_effort requires an explicit model")
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise ValueError("unsupported reasoning_effort")
    return {"model": model, "reasoning_effort": effort}


def native_capability(pair):
    """Read local Codex metadata only. A cache is evidence, not live attestation.

    Missing metadata is explicitly unchecked: Codex remains responsible for
    validating its live capabilities. A known unsupported pair is refused.
    No network, auth reads, subprocess, or fabricated model capability table.
    """
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("native catalog exceeds limit")
        value = json.loads(raw)
        models = value.get("models")
        if not isinstance(models, list):
            raise ValueError("native catalog has no model list")
        model = next((item for item in models if isinstance(item, dict) and item.get("slug") == pair["model"]), None)
        evidence = {"source": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                    "fetched_at": value.get("fetched_at"), "client_version": value.get("client_version")}
        if model is None:
            return {**evidence, "status": "unchecked", "reason": "model absent from cached native catalog"}
        levels = model.get("supported_reasoning_levels")
        if not isinstance(levels, list) or not levels or any(not isinstance(item, dict) or not isinstance(item.get("effort"), str) for item in levels):
            raise ValueError("native catalog has no usable reasoning capability metadata")
        supported = [item["effort"] for item in levels]
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {"status": "unchecked", "source": str(path), "reason": str(exc)}
    if pair["reasoning_effort"] not in supported:
        raise ValueError(f"{pair['model']} does not support reasoning_effort={pair['reasoning_effort']} in the cached native catalog")
    return {**evidence, "status": "supported_in_cache", "supported_efforts": supported}


def execution_choice(settings):
    model, effort = settings.get("model") or None, settings.get("reasoning_effort")
    delegation = settings.get("allow_native_delegation", False)
    if not isinstance(delegation, bool):
        raise ValueError("allow_native_delegation must be a boolean")
    if effort == "ultra" and not delegation:
        raise ValueError("ultra requires explicit allow_native_delegation")
    capability = native_capability(validate_pair(model, effort)) if effort is not None else {
        "status": "unchecked", "reason": "effort inherited from native defaults; not pinned"}
    return {"schema": "fusion.execution-choice.v1", "requested": {"model": model, "reasoning_effort": effort},
            "dispatch": {"status": "prepared"}, "catalog": capability,
            "observed": {"model": None, "reasoning_effort": None, "status": "unobserved"},
            "native_delegation": {"allowed": delegation, "child_trace_visibility": "unobserved"},
            "granularity": "worker_invocation"}


def pair_candidates(config, settings, write=False):
    """Explicit alternatives for the existing router's advisory question."""
    values = (config.get("decisions") or {}).get("model_effort_pairs") or []
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("decisions.model_effort_pairs must be a list of at most eight pairs")
    if not values:
        return []
    selected = validate_pair(settings.get("model"), settings.get("reasoning_effort"))
    pairs = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"model", "reasoning_effort"}:
            raise ValueError("model_effort_pairs entries require exactly model and reasoning_effort")
        pair = validate_pair(value["model"], value["reasoning_effort"])
        native_capability(pair)
        if pair["reasoning_effort"] == "ultra" and (write or settings.get("allow_native_delegation") is not True):
            continue
        if pair not in pairs:
            pairs.append(pair)
    if selected not in pairs:
        raise ValueError("explicit model/effort pair is not in decisions.model_effort_pairs")
    pairs.remove(selected)
    return [selected, *pairs]


def pair_key(pair):
    return "pair-" + hashlib.sha256(json.dumps(pair, sort_keys=True).encode()).hexdigest()[:16]
