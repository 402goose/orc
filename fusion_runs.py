"""Run liveness: a heartbeat lock beside each run, the state it implies, and tombstones for runs that died.

Dispatch writes `run.lock.json` in the run dir when the run starts and refreshes its
`heartbeat_at` from a daemon thread while the run is alive; the file stays when the run
ends. A run dir is then `finished` (result.json), `running` (fresh heartbeat from a live
process that is still the one that wrote the lock), `abandoned` (the lock says otherwise)
or `unknown` (no lock: a run from before locks were written, never taken for running).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCK_NAME = "run.lock.json"
HEARTBEAT_SECONDS = 30
STALE_SECONDS = 120
# ps reports start times to the second, and Linux boot time can drift by one.
START_TOLERANCE_SECONDS = 1


class TombstoneRefused(ValueError):
    """The run dir is live, already finished, or needs a reason it was not given."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def process_started_at(pid: int) -> str | None:
    """When process `pid` started, as UTC ISO time, or None when it is gone or cannot be read:
    from /proc on Linux, `ps -o lstart=` elsewhere. A reused pid has a different start time."""
    if sys.platform.startswith("linux"):
        try:
            # comm may contain spaces and parentheses; starttime is field 22, the 20th after it.
            ticks = int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19])
            boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                        if line.startswith("btime "))
        except (OSError, ValueError, IndexError, StopIteration):
            return None
        seconds = boot + ticks / os.sysconf("SC_CLK_TCK")
    else:
        try:
            text = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                                  timeout=10, env={**os.environ, "LC_ALL": "C"}).stdout.strip()
            seconds = time.mktime(time.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y"))
        except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
            return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _write(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class Heartbeat:
    """Writes a run's lock and refreshes its heartbeat_at every HEARTBEAT_SECONDS until stopped.

    The thread is a daemon and stop() joins it, so it neither outlives the run nor keeps a
    finished process alive."""

    def __init__(self, run_dir: Path):
        from fusion_core import orc_sha
        self.path = Path(run_dir) / LOCK_NAME
        now, pid = iso_now(), os.getpid()
        self.lock = {"pid": pid, "pgid": os.getpgid(0), "host": socket.gethostname(), "started_at": now,
                     "process_started_at": process_started_at(pid), "fusion_sha": orc_sha(), "heartbeat_at": now}
        _write(self.path, self.lock)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._beat, name="run-heartbeat", daemon=True)
        self.thread.start()

    def _beat(self) -> None:
        while not self.stopped.wait(HEARTBEAT_SECONDS):
            self.lock["heartbeat_at"] = iso_now()
            try:
                _write(self.path, self.lock)
            except OSError:
                pass

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join()

    def __enter__(self) -> "Heartbeat":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def run_state(run_dir: Path, now: float | None = None) -> dict[str, Any]:
    """{"state": finished | running | abandoned | unknown, ...} for a run dir.

    `running` needs all of: a heartbeat younger than STALE_SECONDS, the lock's pid alive,
    and that pid's start time equal to the one recorded. On another host the pid cannot be
    checked and the heartbeat alone decides; a start time that cannot be read now is
    reported as unavailable rather than taken for a mismatch. Abandoned and unknown
    states carry their evidence and reasons."""
    run_dir = Path(run_dir)
    if (run_dir / "result.json").is_file():
        return {"state": "finished"}
    lock = _read(run_dir / LOCK_NAME)
    if lock is None:
        activity = _read(run_dir / "activity.json") or {}
        pid = activity.get("pid")
        return {"state": "unknown", "evidence": {
            "source": "activity.json", "pid": pid, "pid_alive": pid_alive(pid) if pid is not None else None,
            "activity_status": activity.get("status"), "activity_updated_at_ms": activity.get("updated_at_ms")},
            "reasons": ["no run.lock.json: the run started before heartbeats were written"]}
    now = time.time() if now is None else now
    heartbeat, pid, reasons = lock.get("heartbeat_at"), lock.get("pid"), []
    beat = _parse_iso(heartbeat)
    age = round(now - beat.timestamp(), 1) if beat else None
    if age is None or age > STALE_SECONDS:
        reasons.append(f"heartbeat stale: last {heartbeat or 'never'}" + (f", {age:.0f}s ago" if age is not None else ""))
    local = lock.get("host") == socket.gethostname()
    alive = pid_alive(pid) if local else None
    recorded, current = lock.get("process_started_at"), None
    if not local:
        check = "other_host"
    elif not alive:
        check = "pid_dead"
        reasons.append(f"pid {pid} is not running")
    else:
        current = process_started_at(pid)
        then, started = _parse_iso(recorded) if recorded else None, _parse_iso(current) if current else None
        if then is None or started is None:
            check = "unavailable"
        elif abs((then - started).total_seconds()) <= START_TOLERANCE_SECONDS:
            check = "match"
        else:
            check = "mismatch"
            reasons.append(f"pid {pid} was reused: it started at {current}, the run's process at {recorded}")
    evidence = {"source": LOCK_NAME, "pid": pid, "pgid": lock.get("pgid"), "host": lock.get("host"), "pid_alive": alive,
                "last_heartbeat": heartbeat, "heartbeat_age_s": age, "process_started_at": recorded,
                "pid_started_at_now": current, "start_time_check": check, "fusion_sha": lock.get("fusion_sha")}
    return {"state": "abandoned" if reasons else "running", "last_heartbeat": heartbeat, "evidence": evidence,
            "reasons": reasons}


def run_states(workspace: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    """(run_id, run_dir, state) for every run dir of the workspace, by run id."""
    from fusion_core import RunStore
    root = RunStore(workspace).runs
    if not root.is_dir():
        return []
    return [(path.name, path, run_state(path)) for path in sorted(root.iterdir()) if path.is_dir()]


def tombstone(workspace: Path, run_id: str, apply: bool = False, reason: str | None = None) -> dict[str, Any]:
    """Settle a dead run as abandoned: a dry run unless `apply`.

    Refuses a running or finished run dir, and applying to an unknown one (no lock) without
    a reason; its dry run still shows the evidence. With `apply`, writes result.json (status abandoned, the evidence, tombstoned_at) and
    appends an unmeasured outcome event marked abandoned. Nothing is deleted or moved."""
    from fusion_core import SCHEMA, RunNotFound, RunStore
    from fusion_decisions import DecisionStore
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
        raise ValueError("run_id must be a Fusion run id")
    store = RunStore(workspace)
    run_dir = store.runs / run_id
    if not run_dir.is_dir():
        raise RunNotFound(f"no Fusion run {run_id} in this workspace")
    state = run_state(run_dir)
    reason = str(reason or "").strip()
    if state["state"] in ("running", "finished"):
        raise TombstoneRefused(f"run {run_id} is {state['state']}; only an abandoned or unknown run is tombstoned")
    needs_reason = state["state"] == "unknown" and not reason
    if needs_reason and apply:
        raise TombstoneRefused(f"run {run_id} has no run.lock.json (state unknown); pass --reason saying why it is dead")
    task = _read(run_dir / "task.json") or {}
    result_path = run_dir / "result.json"
    tombstoned_at = iso_now()
    why = "; ".join(state["reasons"] + ([reason] if reason else []))
    result = {"schema": SCHEMA, "run_id": run_id, "workspace": task.get("workspace"),
              **({"repo": task["repo"]} if task.get("repo") else {}), "status": "abandoned",
              "agent": task.get("agent"), "role": task.get("role"), **({"issue": task["issue"]} if task.get("issue") else {}),
              "route": task.get("route"), "model": (task.get("resolved") or {}).get("model"),
              "summary": f"run abandoned without a result: {why}", "changed": [], "tests": [],
              "blockers": [f"abandoned: {why}"], "previous_state": state["state"],
              "last_heartbeat": state.get("last_heartbeat"), "evidence": state["evidence"],
              **({"reason": reason[:2000]} if reason else {}), "tombstoned_at": tombstoned_at,
              "artifacts": {"run_dir": str(run_dir)}}
    event = {"task_id": run_id, "group": task.get("trace_id") or run_id, "status": "abandoned", "source": "lead",
             "reason": f"tombstoned: {why}"[:2000], "role": task.get("role"), "route": task.get("route"),
             "agent": task.get("agent"), "model": result["model"], "evidence": str(result_path), "unmeasured": True,
             "abandoned": True, "reporter": "tombstone", **({"issue": task["issue"]} if task.get("issue") else {})}
    payload = {"run_id": run_id, "run_dir": str(run_dir), "state": state["state"],
               "last_heartbeat": state.get("last_heartbeat"), "evidence": state["evidence"], "reasons": state["reasons"],
               "applied": False, **({"needs_reason": True} if needs_reason else {}), "writes": {"result": str(result_path), "result_json": result,
                                            "outcome_event": {"event": "outcome_unmeasured", **event}}}
    if not apply:
        return payload
    # Exclusive create: a run that finished meanwhile keeps its own result.
    try:
        with open(result_path, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError as exc:
        raise TombstoneRefused(f"run {run_id} finished while it was being tombstoned") from exc
    DecisionStore(store.workspace).append("outcome_unmeasured", **event)
    return {**payload, "applied": True}


def print_tombstone(payload: dict[str, Any]) -> None:
    evidence = payload["evidence"]
    print(f"run {payload['run_id']}: {payload['state']}")
    if evidence.get("source") == LOCK_NAME:
        print(f"  pid {evidence['pid']} on {evidence['host']}: alive {evidence['pid_alive']}, "
              f"start time {evidence['start_time_check']}; last heartbeat {evidence['last_heartbeat']}")
    else:
        print(f"  no {LOCK_NAME}; activity.json pid {evidence['pid']}: alive {evidence['pid_alive']}, "
              f"status {evidence['activity_status']}")
    for reason in payload["reasons"]:
        print(f"  - {reason}")
    verb = "wrote" if payload["applied"] else "would write"
    print(f"{verb} {payload['writes']['result']} (status abandoned) and an outcome_unmeasured event marked abandoned")
    if not payload["applied"]:
        print("dry run: nothing written; pass --apply" + (" and --reason" if payload.get("needs_reason") else "")
              + " to tombstone")
