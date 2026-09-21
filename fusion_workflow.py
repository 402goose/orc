#!/usr/bin/env python3
"""Persisted, bounded DAG execution for Fusion workflows.

The workflow engine deliberately keeps planning declarative. A model may
propose a graph, but Fusion validates and persists that graph before any
worker is dispatched. This makes fan-out auditable and resumable.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import copy
import hashlib
import json
import re
from pathlib import Path
import subprocess
import time
import uuid
from typing import Any

import fusion_core as core


WORKFLOW_SCHEMA = "fusion.workflow.v1"
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TERMINAL_SUCCESS = {"success"}
TERMINAL_FAILURE = {"failed", "blocked", "invalid"}
TERMINAL_PAUSED = {"paused_quota", "paused_budget"}
# How long a recent quota/session-limit trace keeps an agent lane in cooldown
# before a fresh run is willing to try it again.
LANE_COOLDOWN_SECONDS = 900


def _run_id(prefix: str = "wf") -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{prefix}-{uuid.uuid4().hex[:8]}"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _safe_relative(path: Any, field: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise ValueError(f"{field} cannot be empty")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"{field} must be a workspace-relative path: {value}")
    return value


def _fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _format_task(template: str, item: Any) -> str:
    values: dict[str, Any] = {"item": item if isinstance(item, str) else core.json_text(item)}
    if isinstance(item, dict):
        values.update(item)
    try:
        return template.format_map({key: str(value) for key, value in values.items()})
    except (KeyError, ValueError):
        # A task may contain braces as prose. Preserve it rather than making
        # graph expansion itself a failure.
        return template


def expand_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Expand mapped nodes and resolve dependencies to concrete node IDs."""
    raw_nodes = spec.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("workflow.nodes must be a non-empty array")

    expanded: list[dict[str, Any]] = []
    base_to_ids: dict[str, list[str]] = {}
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("every workflow node must be an object")
        base_id = str(raw.get("id") or "").strip()
        if not base_id or not NODE_ID_RE.fullmatch(base_id):
            raise ValueError(f"invalid workflow node id: {base_id!r}")
        if base_id in base_to_ids:
            raise ValueError(f"duplicate workflow node id: {base_id}")
        items = raw.get("items", raw.get("map"))
        if items is None:
            items_list = [None]
        elif isinstance(items, list) and items:
            items_list = items
        else:
            raise ValueError(f"workflow node {base_id} items must be a non-empty array")

        ids: list[str] = []
        for index, item in enumerate(items_list, start=1):
            node = copy.deepcopy(raw)
            node.pop("items", None)
            node.pop("map", None)
            node["base_id"] = base_id
            node["id"] = base_id if len(items_list) == 1 else f"{base_id}-{index:02d}"
            if not NODE_ID_RE.fullmatch(node["id"]):
                raise ValueError(f"invalid expanded workflow node id: {node['id']}")
            node["item"] = item
            template = str(node.get("task_template", node.get("task", "")))
            if not template:
                raise ValueError(f"workflow node {base_id} needs task or task_template")
            node["task"] = _format_task(template, item) if item is not None else template
            ids.append(node["id"])
            expanded.append(node)
        base_to_ids[base_id] = ids

    concrete_ids = {node["id"] for node in expanded}
    for node in expanded:
        needs: list[str] = []
        for dependency in _as_list(node.get("needs")):
            dependency_id = str(dependency)
            if dependency_id in base_to_ids:
                needs.extend(base_to_ids[dependency_id])
            elif dependency_id in concrete_ids:
                needs.append(dependency_id)
            else:
                raise ValueError(f"workflow node {node['id']} depends on unknown node {dependency_id}")
        node["needs"] = list(dict.fromkeys(needs))
        node["agent"] = str(node.get("agent", "claude"))
        if node["agent"] not in {"claude", "codex", "agy"}:
            raise ValueError(f"workflow node {node['id']} has unsupported agent {node['agent']}")
        node["role"] = str(node.get("role", node["id"]))
        node["write"] = bool(node.get("write", False))
        node["required_files"] = [
            _safe_relative(path, f"workflow node {node['id']} required_files")
            for path in _as_list(node.get("required_files", node.get("required_outputs")))
        ]

    return {"nodes": expanded}, base_to_ids


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("workflow spec must be an object")
    max_parallel = int(spec.get("max_parallel", 4))
    max_attempts = int(spec.get("max_attempts", 1))
    max_writers = int(spec.get("max_parallel_writers", 1))
    if not 1 <= max_parallel <= 64:
        raise ValueError("workflow.max_parallel must be between 1 and 64")
    if not 1 <= max_attempts <= 5:
        raise ValueError("workflow.max_attempts must be between 1 and 5")
    if not 0 <= max_writers <= 1:
        raise ValueError("workflow.max_parallel_writers must be 0 or 1 until worktree workers are enabled")
    budget = float(spec.get("budget_usd", 0) or 0)
    if budget < 0:
        raise ValueError("workflow.budget_usd cannot be negative")
    graph, _ = expand_spec(spec)
    nodes = graph["nodes"]
    if max_writers == 0 and any(node["write"] for node in nodes):
        raise ValueError("workflow.max_parallel_writers must be at least 1 when a node writes")
    ids = {node["id"] for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError(f"workflow contains a dependency cycle at {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        node = next(item for item in nodes if item["id"] == node_id)
        for dependency in node["needs"]:
            if dependency not in ids:
                raise ValueError(f"workflow node {node_id} depends on unknown node {dependency}")
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        visit(node["id"])

    acceptance = spec.get("acceptance") or {}
    if not isinstance(acceptance, dict):
        raise ValueError("workflow.acceptance must be an object")
    acceptance = copy.deepcopy(acceptance)
    acceptance["required_files"] = [
        _safe_relative(path, "workflow acceptance required_files")
        for path in _as_list(acceptance.get("required_files", acceptance.get("required_outputs")))
    ]
    normalized = copy.deepcopy(spec)
    normalized["schema"] = WORKFLOW_SCHEMA
    normalized["max_parallel"] = max_parallel
    normalized["max_attempts"] = max_attempts
    normalized["max_parallel_writers"] = max_writers
    normalized["budget_usd"] = budget
    normalized["acceptance"] = acceptance
    normalized["graph"] = graph
    return normalized


def load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read workflow spec {path}: {exc}") from exc
    return validate_spec(value)


def _result_cost(result: dict[str, Any]) -> float:
    usage = result.get("usage") or {}
    return core.number(usage.get("cost_usd", usage.get("cost", 0)))


def _quota_failure(result: dict[str, Any]) -> bool:
    text = " ".join(str(item) for item in result.get("blockers", []))
    text = f"{text} {result.get('summary', '')}".lower()
    markers = (
        "usage limit",
        "session limit",
        "rate limit",
        "quota",
        "credits",
        "resets at",
        "resets ",
        "too many requests",
    )
    return any(marker in text for marker in markers)


class WorkflowRunner:
    def __init__(
        self,
        workspace: Path,
        config: dict[str, Any],
        spec: dict[str, Any],
        run_id: str | None = None,
        resume: bool = False,
    ):
        self.workspace = workspace
        self.config = config
        self.spec = validate_spec(spec)
        self.run_id = run_id or _run_id()
        self.resume = resume
        self.root = workspace / ".fusion" / "workflows" / self.run_id
        self.nodes_root = self.root / "nodes"
        self.manifest_path = self.root / "manifest.json"
        self.events_path = self.root / "events.jsonl"
        self.workflow_baseline = {
            relative: _fingerprint(workspace / relative)
            for relative in self.spec.get("acceptance", {}).get("required_files", [])
        }
        self.nodes: dict[str, dict[str, Any]] = {}
        for node in self.spec["graph"]["nodes"]:
            self.nodes[node["id"]] = {
                **node,
                "status": "pending",
                "attempts": 0,
                "result": None,
                "artifact": str(self.nodes_root / node["id"] / "node.json"),
            }
        self.lane_health: dict[str, dict[str, Any]] = {}
        if resume:
            self._load_existing()
        else:
            self.root.mkdir(parents=True, exist_ok=False)
            self.nodes_root.mkdir(parents=True, exist_ok=True)
            self._write_manifest("running")
            self._event("workflow.created", {"task": self.spec.get("task", "")})
        self._preflight_lanes()

    def _load_existing(self) -> None:
        if not self.manifest_path.exists():
            raise ValueError(f"workflow run does not exist: {self.run_id}")
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read workflow manifest {self.manifest_path}: {exc}") from exc
        if manifest.get("schema") != WORKFLOW_SCHEMA:
            raise ValueError(f"unsupported workflow manifest schema in {self.manifest_id}")
        if isinstance(manifest.get("workflow_baseline"), dict):
            self.workflow_baseline = manifest["workflow_baseline"]
        for node_id, old in (manifest.get("nodes") or {}).items():
            if node_id not in self.nodes:
                continue
            self.nodes[node_id]["attempts"] = int(old.get("attempts", 0))
            result = old.get("result")
            self.nodes[node_id]["result"] = result
            status = str(old.get("status", "pending"))
            self.nodes[node_id]["status"] = "success" if status == "success" else "pending"
        self._invalidate_stale_receipts()
        self._write_manifest("running")
        self._event("workflow.resumed", {})

    def _definition_digest(self, node: dict[str, Any]) -> str:
        """Hash of everything that defines what a node does, independent of
        who ran it or what it depends on. Deliberately excludes the resolved
        command/model: orc-free/orc-best are meant to re-resolve to a
        different model over time, and invalidating a cached receipt every
        time that catalog reshuffles would make resume useless for them."""
        payload = {
            "task": node["task"],
            "role": node["role"],
            "agent": node["agent"],
            "route": node.get("route"),
            "write": node["write"],
            "required_files": node["required_files"],
            "acceptance": node.get("acceptance") or {},
        }
        return hashlib.sha256(core.json_text(payload).encode("utf-8")).hexdigest()

    def _input_digest(self, definition_digest: str, dependency_digests: dict[str, str]) -> str:
        payload = {"definition": definition_digest, "dependencies": dependency_digests}
        return hashlib.sha256(core.json_text(payload).encode("utf-8")).hexdigest()

    def _topological_order(self) -> list[str]:
        order: list[str] = []
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            visited.add(node_id)
            for dependency in self.nodes[node_id]["needs"]:
                if dependency in self.nodes:
                    visit(dependency)
            order.append(node_id)

        for node_id in self.nodes:
            visit(node_id)
        return order

    def _invalidate_stale_receipts(self) -> None:
        """Content-address every "success" node against its current
        definition and its dependencies' current digests, processed in
        dependency order. A node whose own definition changed, or whose
        digest no longer matches what was recorded when it last succeeded,
        goes back to pending; that invalidation cascades to dependents in
        the same pass since a dependent's expected digest embeds its
        dependencies' digests. This makes a no-op resume dispatch nothing,
        and an edited node (plus everything downstream of it) rerun."""
        current_digest: dict[str, str] = {}
        for node_id in self._topological_order():
            node = self.nodes[node_id]
            if node["status"] != "success":
                continue
            result = node.get("result") or {}
            if not self._accept_node(node, result)[0]:
                node["status"] = "pending"
                continue
            dependency_digests = {dep: current_digest.get(dep) for dep in node["needs"]}
            if any(value is None for value in dependency_digests.values()):
                node["status"] = "pending"
                self._event("node.stale", {"node_id": node_id, "reason": "a dependency was invalidated"})
                continue
            expected = self._input_digest(self._definition_digest(node), dependency_digests)
            if result.get("digest") != expected:
                node["status"] = "pending"
                self._event("node.stale", {"node_id": node_id, "reason": "definition or dependency evidence changed"})
                continue
            current_digest[node_id] = expected
            self._event("node.reused", {"node_id": node_id})
            self._emit_cache_hit_telemetry(node_id, node)

    def _emit_cache_hit_telemetry(self, node_id: str, node: dict[str, Any]) -> None:
        """A digest-matched node skips dispatch entirely, so it would
        otherwise never produce a trace span -- locally or remotely. Without
        this, "how much is caching actually saving the group" is invisible
        in the exact data source built to answer questions like that."""
        result = node.get("result") or {}
        resolved = result.get("resolved") or {}
        now = core.now_ms()
        task = {
            "agent": node["agent"],
            "role": node["role"],
            "route": node.get("route"),
            "run_id": f"{self.run_id}:{node_id}:cached",
            "trace_id": self.run_id,
            "parent_task_id": self.run_id,
            "write": node["write"],
        }
        cache_result = {"status": "cache_hit", "usage": {}, "changed": [], "tests": [], "blockers": [], "artifacts": {}}
        metadata = {"model": resolved.get("model")}
        try:
            core.RunStore(self.workspace).trace_span(self.config, task, cache_result, now, now, metadata)
        except OSError:
            pass  # telemetry is best-effort; never let it break a resume.

    @property
    def manifest_id(self) -> str:
        return str(self.manifest_path)

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        event = {"ts": core.now_ms(), "type": event_type, "workflow_id": self.run_id, **payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _write_manifest(self, status: str, error: str | None = None) -> None:
        manifest = {
            "schema": WORKFLOW_SCHEMA,
            "workflow_id": self.run_id,
            "status": status,
            "task": self.spec.get("task", ""),
            "spec": self.spec,
            "workflow_baseline": self.workflow_baseline,
            "nodes": self.nodes,
            "lanes": self.lane_health,
            "error": error,
            "artifacts": {
                "root": str(self.root),
                "manifest": str(self.manifest_path),
                "events": str(self.events_path),
            },
            "updated_at": core.now_ms(),
        }
        temp = self.manifest_path.with_suffix(".tmp")
        temp.write_text(core.json_text(manifest) + "\n", encoding="utf-8")
        temp.replace(self.manifest_path)

    def _node_dir(self, node_id: str) -> Path:
        path = self.nodes_root / node_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _dependency_context(self, node: dict[str, Any]) -> str:
        if not node["needs"]:
            return "- none; begin with repository inspection"
        lines = []
        for dependency in node["needs"]:
            item = self.nodes[dependency]
            lines.append(
                f"- {dependency}: status={item['status']}, artifact={item['artifact']}, "
                f"summary={str((item.get('result') or {}).get('summary', ''))[:500]}"
            )
        return "\n".join(lines)

    def _agent_command_for(self, agent: str) -> str:
        settings = core.agent_settings(self.config, {"agent": agent, "route": None, "settings_overrides": {}})
        return str(settings.get("command", agent))

    def _set_lane(self, agent: str, status: str, reason: str) -> None:
        if self.lane_health.get(agent, {}).get("status") == status:
            return
        self.lane_health[agent] = {"status": status, "reason": reason}
        self._event("lane.status", {"agent": agent, "status": status, "reason": reason})

    def _preflight_lanes(self) -> None:
        """Check agent lanes once before dispatch instead of discovering a dead
        lane N times in parallel. Executable checks are free; the quota/session
        cooldown reuses the most recent trace per agent rather than spending a
        real call to find out a lane is already blocked. A resume is an
        explicit "try again now", so it skips the trace-history cooldown but
        still gets the executable check and its own in-run cooldown."""
        for agent in {node["agent"] for node in self.nodes.values()}:
            command = self._agent_command_for(agent)
            if core.executable(command) is None:
                self._set_lane(agent, "blocked", f"{command} is not available on PATH")
        if self.resume:
            return
        store = core.RunStore(self.workspace)
        now = core.now_ms()
        seen: set[str] = set()
        for span in store.traces(limit=50):
            agent = span.get("agent")
            if not agent or agent in seen:
                continue
            seen.add(agent)
            if agent in self.lane_health:
                continue
            end_time = span.get("end_time_ms")
            if not isinstance(end_time, (int, float)):
                continue
            age_ms = now - end_time
            if 0 <= age_ms <= LANE_COOLDOWN_SECONDS * 1000 and _quota_failure(span):
                self._set_lane(agent, "cooldown", f"a {agent} run reported a quota/session limit {int(age_ms / 1000)}s ago")

    def _prompt(self, node: dict[str, Any]) -> str:
        required = ", ".join(node.get("required_files", [])) or "none"
        return f"""You are node {node['id']} in a persisted Fusion workflow.

Workflow: {self.run_id}
Role: {node['role']}
Task: {node['task']}

Dependency artifacts:
{self._dependency_context(node)}

Required workspace artifacts: {required}
Read dependency artifacts before acting. Keep the scope limited to this node.
If this node writes code, run the narrowest meaningful verification. The
orchestrator will validate required artifacts after your turn.

Return the exact labels:
STATUS: success | partial | blocked | error
SUMMARY: what you did and the current result
CHANGED: comma-separated paths, or none
TESTS: commands run and their outcome, or none
BLOCKERS: unresolved issues, or none
"""

    def _accept_node(self, node: dict[str, Any], result: dict[str, Any]) -> tuple[bool, list[str]]:
        problems: list[str] = []
        if result.get("status") not in TERMINAL_SUCCESS:
            problems.append(f"worker status is {result.get('status', 'unknown')}")
        for relative in node.get("required_files", []):
            path = self.workspace / relative
            if not path.is_file():
                problems.append(f"required artifact is missing: {relative}")
            else:
                baseline = (node.get("_artifact_baseline") or {}).get(relative)
                if baseline and _fingerprint(path) == baseline:
                    problems.append(f"required artifact did not change during node: {relative}")
        acceptance = node.get("acceptance") or {}
        if isinstance(acceptance, dict):
            for field in _as_list(acceptance.get("required_handoff")):
                if not result.get(str(field)):
                    problems.append(f"required handoff field is empty: {field}")
            for command in _as_list(acceptance.get("checks")):
                if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
                    problems.append("acceptance checks must be argv arrays")
                    continue
                try:
                    completed = subprocess.run(
                        command,
                        cwd=self.workspace,
                        capture_output=True,
                        text=True,
                        timeout=int(self.config.get("timeout_seconds", 3600)),
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    problems.append(f"acceptance check could not run: {' '.join(command)} ({exc})")
                    continue
                if completed.returncode != 0:
                    problems.append(f"acceptance check failed: {' '.join(command)}")
        return not problems, problems

    def _run_node(self, node_id: str, attempt: int) -> dict[str, Any]:
        node = self.nodes[node_id]
        route = node.get("route")
        agent = node["agent"]
        write = bool(node.get("write", False))
        settings = {key: node[key] for key in (
            "command", "model", "model_selector", "profile", "launcher_args",
            "max_budget_usd", "permission_mode", "permission_prompts", "allowed_tools",
            "allow_untested",
        ) if key in node}
        task = core.make_task(
            self.workspace,
            agent,
            self._prompt(node),
            node["role"],
            ["complete the assigned node", "return evidence in the required handoff format"],
            ["do not broaden the workflow task", "do not run parallel writers in this workspace"],
            f"workflow:{self.run_id}:{node_id}",
            bool(node.get("resume", False)) or attempt > 1,
            write,
            parent_task_id=self.run_id,
            route=route,
            settings_overrides=settings,
        )
        store = core.RunStore(self.workspace)
        try:
            result = core.dispatch(self.config, task, store)
        except (OSError, ValueError, RuntimeError) as exc:
            result = {
                "schema": core.SCHEMA,
                "run_id": task["run_id"],
                "status": "error",
                "agent": agent,
                "role": node["role"],
                "route": route,
                "summary": "workflow node could not be dispatched",
                "changed": [],
                "tests": [],
                "blockers": [str(exc)],
                "exit_code": 126,
                "duration_ms": 0,
                "usage": {},
                "artifacts": {},
            }
        result["workflow_id"] = self.run_id
        result["node_id"] = node_id
        result["attempt"] = attempt
        return {"task": task, "result": result}

    def _save_node(self, node_id: str, payload: dict[str, Any]) -> None:
        path = self._node_dir(node_id) / "node.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(core.json_text(payload) + "\n", encoding="utf-8")
        temp.replace(path)

    def _spent(self) -> float:
        return sum(_result_cost(node.get("result") or {}) for node in self.nodes.values())

    def _ready(self, node: dict[str, Any]) -> bool:
        return node["status"] == "pending" and all(self.nodes[dependency]["status"] == "success" for dependency in node["needs"])

    def _block_unrunnable(self) -> None:
        changed = True
        while changed:
            changed = False
            for node in self.nodes.values():
                if node["status"] != "pending":
                    continue
                lane = self.lane_health.get(node["agent"])
                if lane:
                    status = "paused_quota" if lane["status"] == "cooldown" else "blocked"
                    node["status"] = status
                    node["result"] = {
                        "status": status,
                        "summary": f"agent lane {node['agent']} is {lane['status']}",
                        "blockers": [lane["reason"]],
                    }
                    self._save_node(node["id"], {
                        "task": {},
                        "result": node["result"],
                        "acceptance": {"ok": False, "problems": node["result"]["blockers"]},
                    })
                    self._event(f"node.{status}", {"node_id": node["id"], "result": node["result"]})
                    changed = True
                    continue
                dependency_statuses = [self.nodes[dependency]["status"] for dependency in node["needs"]]
                if any(status in TERMINAL_FAILURE or status in TERMINAL_PAUSED for status in dependency_statuses):
                    # Wait until every dependency is terminal before taking
                    # the blocker snapshot. This keeps a downstream receipt
                    # from saying one sibling is still running when another
                    # sibling already made the fan-in impossible.
                    if any(status in {"pending", "running"} for status in dependency_statuses):
                        continue
                    node["status"] = "blocked"
                    node["result"] = {
                        "status": "blocked",
                        "summary": "dependency did not reach an accepted success state",
                        "blockers": [
                            f"{dependency}: {self.nodes[dependency]['status']}"
                            for dependency in node["needs"]
                            if self.nodes[dependency]["status"] != "success"
                        ],
                    }
                    self._save_node(node["id"], {
                        "task": {},
                        "result": node["result"],
                        "acceptance": {"ok": False, "problems": node["result"]["blockers"]},
                    })
                    self._event("node.blocked", {"node_id": node["id"], "result": node["result"]})
                    changed = True

    def _final_status(self) -> str:
        statuses = {node["status"] for node in self.nodes.values()}
        if "paused_quota" in statuses:
            return "paused_quota"
        if "paused_budget" in statuses:
            return "paused_budget"
        if statuses & TERMINAL_FAILURE:
            return "failed"
        if statuses == {"success"}:
            return "success" if self._accept_workflow()[0] else "failed"
        return "running"

    def _accept_workflow(self) -> tuple[bool, list[str]]:
        problems: list[str] = []
        acceptance = self.spec.get("acceptance") or {}
        for relative in acceptance.get("required_files", []):
            path = self.workspace / relative
            if not path.is_file():
                problems.append(f"required workflow artifact is missing: {relative}")
            elif relative in self.workflow_baseline and _fingerprint(path) == self.workflow_baseline[relative]:
                problems.append(f"required workflow artifact did not change during run: {relative}")
        for node_id in _as_list(acceptance.get("required_nodes")):
            if node_id not in self.nodes:
                problems.append(f"required workflow node is unknown: {node_id}")
            elif self.nodes[node_id]["status"] != "success":
                problems.append(f"required workflow node is not successful: {node_id}")
        return not problems, problems

    def run(self) -> dict[str, Any]:
        self._event("workflow.started", {"max_parallel": self.spec["max_parallel"]})
        max_parallel = self.spec["max_parallel"]
        max_writers = self.spec["max_parallel_writers"]
        active: dict[Future[dict[str, Any]], tuple[str, bool]] = {}
        active_writers = 0
        with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="fusion") as executor:
            while True:
                self._block_unrunnable()
                if not active:
                    ready = [node for node in self.nodes.values() if self._ready(node)]
                    if not ready:
                        break
                while len(active) < max_parallel:
                    ready = [node for node in self.nodes.values() if self._ready(node)]
                    selected = None
                    for node in ready:
                        if node["write"] and active_writers >= max_writers:
                            continue
                        selected = node
                        break
                    if selected is None:
                        break
                    if self.spec["budget_usd"] and self._spent() >= self.spec["budget_usd"]:
                        for node in ready:
                            node["status"] = "paused_budget"
                            node["result"] = {
                                "status": "paused_budget",
                                "summary": "workflow budget reached before dispatch",
                                "blockers": [f"budget_usd={self.spec['budget_usd']}"]
                            }
                            self._event("node.paused_budget", {"node_id": node["id"]})
                        break
                    selected["status"] = "running"
                    selected["attempts"] += 1
                    selected["_artifact_baseline"] = {
                        relative: _fingerprint(self.workspace / relative)
                        for relative in selected.get("required_files", [])
                    }
                    attempt = selected["attempts"]
                    writer = bool(selected["write"])
                    if writer:
                        active_writers += 1
                    self._event("node.started", {"node_id": selected["id"], "attempt": attempt})
                    future = executor.submit(self._run_node, selected["id"], attempt)
                    active[future] = (selected["id"], writer)
                    self._write_manifest("running")
                if not active:
                    break
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    node_id, writer = active.pop(future)
                    if writer:
                        active_writers -= 1
                    try:
                        payload = future.result()
                    except Exception as exc:  # pragma: no cover - defensive worker boundary
                        payload = {"task": {}, "result": {"status": "error", "summary": "worker thread failed", "blockers": [str(exc)]}}
                    node = self.nodes[node_id]
                    result = payload.get("result") or {}
                    accepted, problems = self._accept_node(node, result)
                    if problems:
                        result.setdefault("blockers", []).extend(problems)
                    if accepted:
                        dependency_digests = {dep: (self.nodes[dep].get("result") or {}).get("digest") for dep in node["needs"]}
                        result["digest"] = self._input_digest(self._definition_digest(node), dependency_digests)
                        result["resolved"] = payload.get("task", {}).get("resolved") or {}
                    node["result"] = result
                    node_dir = self._node_dir(node_id)
                    payload["acceptance"] = {"ok": accepted, "problems": problems}
                    self._save_node(node_id, payload)
                    if accepted:
                        node["status"] = "success"
                        self._event("node.succeeded", {"node_id": node_id, "attempt": node["attempts"]})
                    elif _quota_failure(result):
                        node["status"] = "paused_quota"
                        self._event("node.paused_quota", {"node_id": node_id, "attempt": node["attempts"]})
                        self._set_lane(node["agent"], "cooldown", f"node {node_id} reported a quota/session limit")
                    elif node["attempts"] < self.spec["max_attempts"]:
                        node["status"] = "pending"
                        self._event("node.retrying", {"node_id": node_id, "attempt": node["attempts"], "problems": problems})
                    else:
                        node["status"] = "invalid" if problems and result.get("status") == "success" else "failed"
                        self._event("node.failed", {"node_id": node_id, "attempt": node["attempts"], "problems": problems})
                    self._write_manifest("running")

        self._block_unrunnable()
        status = self._final_status()
        acceptance_ok, acceptance_problems = self._accept_workflow()
        if status == "success" and not acceptance_ok:
            status = "failed"
        if acceptance_problems:
            self._event("workflow.acceptance_failed", {"problems": acceptance_problems})
        self._write_manifest(status, "; ".join(acceptance_problems) if acceptance_problems else None)
        self._event("workflow.finished", {"status": status, "spent_usd": self._spent()})
        return self.result(status, acceptance_problems)

    def result(self, status: str | None = None, acceptance_problems: list[str] | None = None) -> dict[str, Any]:
        return {
            "schema": WORKFLOW_SCHEMA,
            "workflow_id": self.run_id,
            "status": status or self._final_status(),
            "task": self.spec.get("task", ""),
            "spent_usd": self._spent(),
            "nodes": [self.nodes[node_id] for node_id in self.nodes],
            "lanes": self.lane_health,
            "acceptance": {
                "ok": status == "success" and not acceptance_problems,
                "problems": acceptance_problems or [],
            },
            "artifacts": {
                "root": str(self.root),
                "manifest": str(self.manifest_path),
                "events": str(self.events_path),
            },
        }


def run_workflow(workspace: Path, config: dict[str, Any], spec_path: Path, task: str | None = None) -> dict[str, Any]:
    spec = load_spec(spec_path)
    if task:
        spec["task"] = task
    return WorkflowRunner(workspace, config, spec).run()


def resume_workflow(workspace: Path, config: dict[str, Any], run_id: str, spec_path: Path | None = None) -> dict[str, Any]:
    manifest_path = workspace / ".fusion" / "workflows" / run_id / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read workflow run {run_id}: {exc}") from exc
    # A resume normally replays the persisted spec unchanged. Passing spec_path
    # lets a caller resume with an edited workflow.json; the digest check in
    # _invalidate_stale_receipts() then reruns only the nodes whose definition
    # or dependency evidence actually changed, not the whole graph.
    spec = load_spec(spec_path) if spec_path else (manifest.get("spec") or {})
    return WorkflowRunner(workspace, config, spec, run_id=run_id, resume=True).run()


def workflow_status(workspace: Path, run_id: str) -> dict[str, Any]:
    path = workspace / ".fusion" / "workflows" / run_id / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read workflow run {run_id}: {exc}") from exc


def workflow_report(workspace: Path, run_id: str) -> dict[str, Any]:
    """One combined view of a workflow run: waves, lanes, usage, and blockers.

    Assessing the Saloon run required manually joining `workflow status`,
    `trace`, `usage`, and events by hand. This reconstructs that same picture
    from the persisted manifest and the trace ledger in one read-only call.
    """
    manifest = workflow_status(workspace, run_id)
    nodes = manifest.get("nodes") or {}
    spec_nodes = {node["id"]: node for node in (manifest.get("spec", {}).get("graph", {}).get("nodes") or [])}

    wave_cache: dict[str, int] = {}

    def wave_of(node_id: str) -> int:
        if node_id in wave_cache:
            return wave_cache[node_id]
        wave_cache[node_id] = 0  # guard against a spec that slipped a cycle past validation
        needs = spec_nodes.get(node_id, {}).get("needs") or []
        depth = 0 if not needs else 1 + max((wave_of(dep) for dep in needs), default=-1)
        wave_cache[node_id] = depth
        return depth

    waves: dict[int, list[dict[str, Any]]] = {}
    blockers: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        result = node.get("result") or {}
        digest = result.get("digest")
        waves.setdefault(wave_of(node_id), []).append({
            "id": node_id,
            "agent": node.get("agent"),
            "status": node.get("status"),
            "attempts": node.get("attempts"),
            "summary": result.get("summary"),
            "digest": digest[:12] if digest else None,
        })
        if node.get("status") not in {"success", "pending", "running"}:
            for blocker in result.get("blockers") or []:
                blockers.append({"node_id": node_id, "status": node.get("status"), "blocker": blocker})

    spans = [span for span in core.RunStore(workspace).traces(limit=10000) if span.get("trace_id") == run_id]
    status = manifest.get("status")
    error = manifest.get("error")
    return {
        "schema": "fusion.workflow.report.v1",
        "workflow_id": run_id,
        "status": status,
        "task": manifest.get("task"),
        "spent_usd": sum(_result_cost(node.get("result") or {}) for node in nodes.values()),
        "budget_usd": (manifest.get("spec") or {}).get("budget_usd") or 0,
        "waves": [{"wave": wave, "nodes": waves[wave]} for wave in sorted(waves)],
        "lanes": manifest.get("lanes") or {},
        "usage": core.usage_summary(spans),
        "blockers": blockers,
        "acceptance_problems": [item for item in (error or "").split("; ") if item],
        "artifacts": manifest.get("artifacts") or {},
        "resume_command": (
            f"fusion --workspace {workspace} --json workflow resume {run_id}"
            if status in {"paused_quota", "paused_budget", "running", "failed"}
            else None
        ),
    }
