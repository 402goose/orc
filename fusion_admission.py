"""Optional local admission provider; no scheduler or automatic redispatch.

The operator supplies the provider command and workspace bindings at server
startup, outside editable project configuration. An unavailable required
provider is an error, never permission to launch standalone.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def binding(workspace):
    raw = os.environ.get("FUSION_ADMISSION_PROVIDER")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        command, paths = value["command"], value["workspaces"]
        if not isinstance(command, list) or not command or not all(isinstance(p, str) and p for p in command):
            raise ValueError("command must be an argv array")
        if not Path(command[0]).is_absolute():
            raise ValueError("provider executable must be absolute")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and Path(p).is_absolute() for p in paths):
            raise ValueError("workspaces must be absolute paths")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Invalid required admission provider configuration: {exc}") from exc
    return {"command": command} if str(Path(workspace).resolve()) in {str(Path(p).resolve()) for p in paths} else None


def call(workspace, operation, *, run_id=None, **fields):
    provider = binding(workspace)
    if provider is None:
        raise ValueError("No admission provider is bound to this workspace")
    request = {"schema": "tenet.admission-command.v1", "operation": operation,
               "workspace": str(Path(workspace).resolve()), **fields}
    if run_id is not None:
        request["run_id"] = run_id
    try:
        result = subprocess.run(provider["command"], input=json.dumps(request, allow_nan=False),
                                text=True, capture_output=True, timeout=20, cwd=workspace)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Required TENET admission provider unavailable: {type(exc).__name__}") from exc
    if len(result.stdout) > 2_000_000:
        raise ValueError("Admission provider response exceeds 2 MB")
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("schema") != "tenet.admission-response.v1":
            raise ValueError("wrong response schema")
        if response.get("operation") != operation or response.get("workspace") != request["workspace"] or response.get("run_id") != run_id:
            raise ValueError("response identity mismatch")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid admission provider response: {exc}") from exc
    if result.returncode or response.get("ok") is not True:
        raise ValueError("TENET admission refused: " + str(response.get("error") or "provider failed")[:1000])
    return response


def describe(workspace):
    try:
        if binding(workspace) is None:
            return {"mode": "standalone", "ready": True, "provider": None}
        return {**call(workspace, "describe"), "mode": "tenet", "ready": True, "provider": "tenet"}
    except ValueError as exc:
        return {"mode": "tenet", "ready": False, "provider": "tenet", "error": str(exc)}


def admit(workspace, run_id, spec, config, mode="off"):
    if mode not in {"off", "shadow"}:
        raise ValueError("Admitted observations must be off or shadow")
    codex = config.get("codex") or {}
    if (config.get("execution_mode") != "restricted" or codex.get("sandbox") not in {"read-only", "workspace-write"}
            or codex.get("approval") != "never" or codex.get("git_write") is not False
            or config.get("publish", {}).get("mode") != "off"):
        raise ValueError("TENET admission requires restricted Codex, approval never, git_write false and publishing off")
    if set(codex) - {"command", "model", "sandbox", "approval", "git_write"}:
        raise ValueError("Unsupported Codex settings must be removed before TENET admission")
    command = shutil.which(codex.get("command", "codex"))
    if not command:
        raise ValueError("Configured Codex executable is unavailable")
    writes = any(n.get("write") for n in spec["nodes"])
    profile = {"execution_mode": "restricted", "timeout_seconds": min(config.get("timeout_seconds", 600), 600),
               "codex": {**codex, "command": str(Path(command).resolve()),
                         "sandbox": codex["sandbox"] if writes else "read-only"},
               "publish": {"mode": "off"}, "decisions": {"mode": mode},
               "telemetry": {"remote": {"enabled": False}}}
    try:
        revision = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=5)
        source_revision = revision.stdout.strip() if revision.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        source_revision = None
    return call(workspace, "admit", run_id=run_id, intent={
        "action": "workflow", "task": spec.get("task", ""), "spec": spec, "config": profile,
        "executor": {"system": "fusion", "workflow_id": run_id},
        "source_revision": source_revision,
        "permissions": {"execution_mode": "restricted", "sandbox": profile["codex"]["sandbox"],
                        "approval": "never", "git_write": False, "publish": "off", "allow_write": writes},
    })


def execution(workspace, admission):
    """Claim once before spawn. A crash after this point remains unknown."""
    run_id = admission["run_id"]
    claimed = call(workspace, "claim", run_id=run_id,
                   expected_request_sha256=admission["request_sha256"])
    paths = {}
    frozen_config = None
    for name in ("spec", "config"):
        artifact = claimed["artifacts"][name]
        path = Path(artifact["path"])
        if not path.is_absolute() or path.resolve().is_relative_to(Path(workspace).resolve()):
            raise ValueError("Admitted execution inputs must be outside the worker workspace")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise ValueError(f"Admitted {name} changed before dispatch")
        if name == "config":
            frozen_config = json.loads(raw)
        paths[name] = str(path)
    decisions = frozen_config.get("decisions") or {}
    mode = decisions.get("mode")
    if mode not in {"off", "shadow"}:
        raise ValueError("Admitted observations must be off or shadow")
    python = decisions.get("python", "") if mode == "shadow" else ""
    if mode == "shadow" and (not isinstance(python, str) or not Path(python).is_absolute()
                             or Path(python).resolve().is_relative_to(Path(workspace).resolve())):
        raise ValueError("Shadow observations require operator-pinned Python outside the workspace")
    argv = [sys.executable, str(Path(__file__).with_name("fusion")), "--workspace", str(workspace),
            "--json", "--progress", "workflow", "run", paths["spec"]]
    environment = {"FUSION_CONFIG": paths["config"],
                   "FUSION_CONFIG_SHA256": claimed["artifacts"]["config"]["sha256"],
                   "FUSION_SPEC_SHA256": claimed["artifacts"]["spec"]["sha256"],
                   "FUSION_WORKFLOW_ID": run_id, "FUSION_DECISIONS_MODE": mode,
                   "FUSION_LAYA_PYTHON": python, "FUSION_TELEMETRY": "0"}
    return claimed, argv, environment


def observation(workspace, admission):
    try:
        return call(workspace, "status", run_id=admission["run_id"])
    except ValueError as exc:
        return {"status": "unknown", "terminal": False, "liveness": "unknown", "error": str(exc)}
