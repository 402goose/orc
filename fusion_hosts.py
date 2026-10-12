"""Which machine a record came from, this machine's facts, and the registered machines side by side.

Every run's task.json, result.json and trace, and every decision event, carries `host`:
`self` from the optional registry `$ORC_HOME/hosts.json` (normally
`~/.config/orc/hosts.json`), else the short hostname. Records written before this
field have none, and readers take that as `host: null`.

    {"self": "laptop", "hosts": {"laptop": {"ssh": null},
                                 "runner": {"ssh": "user@runner-host", "fusion": "~/orc/fusion"}}}

`fusion host-info` reports this machine's facts without network calls or secrets;
`fusion hosts` runs it here and over ssh on each registered host with `ssh` and
prints one table. Nothing here changes dispatch or routing.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
from typing import Any

HARNESSES = ("claude", "codex", "agy", "opencode")
VERSION_TIMEOUT_SECONDS = 5
SSH_TIMEOUT_SECONDS = 90
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")


class HostsError(ValueError):
    """hosts.json is unreadable or not the registry shape."""


def registry_path() -> Path:
    return Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc").expanduser() / "hosts.json"


def load_registry(path: Path | None = None) -> dict[str, Any]:
    """The validated registry, or {"self": None, "hosts": {}} when the file is absent.

    Only `self` and `hosts` are allowed; each host has `ssh` (a string or null) and
    an optional `fusion` path. Anything else raises HostsError naming the file."""
    path = path or registry_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"self": None, "hosts": {}}
    except OSError as exc:
        raise HostsError(f"{path}: cannot be read: {exc.strerror or exc}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HostsError(f"{path}: not valid JSON: {exc}") from exc

    def refuse(message: str) -> HostsError:
        return HostsError(f"{path}: {message}")

    if not isinstance(value, dict):
        raise refuse("must be a JSON object with `self` and `hosts`")
    if unknown := sorted(set(value) - {"self", "hosts"}):
        raise refuse(f"unknown key {', '.join(map(repr, unknown))}; only `self` and `hosts` are allowed")
    self_name = value.get("self")
    if self_name is not None and (not isinstance(self_name, str) or not self_name.strip()):
        raise refuse("`self` must be a non-empty string or null")
    hosts = value.get("hosts", {})
    if not isinstance(hosts, dict):
        raise refuse("`hosts` must be an object of host name to {\"ssh\": ..., \"fusion\": ...}")
    for name, entry in hosts.items():
        if not name.strip():
            raise refuse("a host name in `hosts` is empty")
        if not isinstance(entry, dict):
            raise refuse(f"host {name!r} must be an object with `ssh`")
        if unknown := sorted(set(entry) - {"ssh", "fusion"}):
            raise refuse(f"host {name!r} has unknown key {', '.join(map(repr, unknown))}; only `ssh` and `fusion` are allowed")
        if "ssh" not in entry:
            raise refuse(f"host {name!r} needs `ssh` (a destination such as user@host, or null)")
        ssh = entry["ssh"]
        if ssh is not None and (not isinstance(ssh, str) or not ssh.strip() or ssh.startswith("-")):
            raise refuse(f"host {name!r}: `ssh` must be a destination string not starting with '-', or null")
        if "fusion" in entry and (not isinstance(entry["fusion"], str) or not entry["fusion"].strip()):
            raise refuse(f"host {name!r}: `fusion` must be a non-empty path")
    return {"self": self_name, "hosts": hosts}


def short_hostname() -> str:
    return socket.gethostname().split(".")[0] or socket.gethostname()


def current_host() -> str:
    """The name records carry: the registry's `self`, else the short hostname.

    Tagging a record never fails on a malformed registry; `host-info` and `hosts`
    refuse it instead."""
    try:
        name = load_registry()["self"]
    except HostsError:
        name = None
    return name or short_hostname()


def harness_version(command: str, timeout: float | None = None) -> str | None:
    """The first line `command --version` prints, or None when it is absent, fails or is slow."""
    try:
        completed = subprocess.run([command, "--version"], capture_output=True, text=True, errors="replace",
                                   stdin=subprocess.DEVNULL, timeout=VERSION_TIMEOUT_SECONDS if timeout is None else timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    line = next((line.strip() for line in (completed.stdout or completed.stderr).splitlines() if line.strip()), "")
    return line[:200] or None


def host_info(workspace: Path, config: dict[str, Any]) -> dict[str, Any]:
    """This machine's facts. Reads local files and runs `--version` only: no network, no secrets."""
    import fusion_core as core
    import fusion_quota
    import fusion_runs
    registry = load_registry()
    store = core.RunStore(workspace)
    commands = {agent: str((config.get(agent) or {}).get("command") or agent) for agent in HARNESSES}
    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        versions = dict(zip(commands, pool.map(harness_version, commands.values())))
    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        load = None
    try:
        disk_free = shutil.disk_usage(store.workspace if store.workspace.exists() else workspace).free
    except OSError:
        disk_free = None
    try:
        quota = {account["lane_key"]: account["classification"]
                 for account in fusion_quota.status(config, store)["accounts"]}
    except (OSError, ValueError):
        quota = None
    return {"schema": "fusion.host_info.v1", "host": registry["self"] or short_hostname(), "orc_sha": core.orc_sha(),
            "python": platform.python_version(), "harnesses": versions, "load": load, "disk_free_bytes": disk_free,
            "runs_in_flight": sum(1 for _, _, state in fusion_runs.run_states(store.workspace) if state["state"] == "running"),
            "quota": quota}


def remote_info(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """`host-info` from a registered host over ssh; an unreachable host is a row, never an exception."""
    argv = ["ssh", *SSH_OPTIONS, entry["ssh"], entry.get("fusion") or "fusion", "--json", "host-info"]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                                   timeout=SSH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"host": name, "reachable": False, "error": f"no answer in {SSH_TIMEOUT_SECONDS}s"}
    except OSError as exc:
        return {"host": name, "reachable": False, "error": f"cannot run ssh: {exc.strerror or exc}"}
    if completed.returncode != 0:
        detail = next((line.strip() for line in reversed(completed.stderr.splitlines()) if line.strip()), "")
        return {"host": name, "reachable": False, "error": f"exit {completed.returncode}" + (f": {detail[:300]}" if detail else "")}
    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError:
        info = None
    if not isinstance(info, dict):
        return {"host": name, "reachable": False, "error": "host-info did not print a JSON object"}
    reported = info.get("host")
    return {**info, "host": name, "reachable": True, **({"reported_host": reported} if reported != name else {})}


def hosts(workspace: Path, config: dict[str, Any]) -> dict[str, Any]:
    """One row per host: this one, then each registered host with `ssh`, with `skew` where orc_sha differs."""
    registry = load_registry()
    local = host_info(workspace, config)
    remote = [(name, entry) for name, entry in registry["hosts"].items() if entry.get("ssh") and name != local["host"]]
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(remote)))) as pool:
        rows = [{**local, "reachable": True}, *pool.map(lambda item: remote_info(*item), remote)]
    for row in rows:
        row["skew"] = row["orc_sha"] != local["orc_sha"] if row["reachable"] else None
    return {"orc_sha": local["orc_sha"], "hosts": rows}


def _version(value: Any) -> str:
    match = re.search(r"\d+(\.\d+)+\S*", str(value or ""))
    return match.group(0)[:16] if match else (str(value)[:16] if value else "-")


def _gigabytes(value: Any) -> str:
    return f"{value / 1e9:.1f}G" if isinstance(value, (int, float)) else "-"


def render_hosts(view: dict[str, Any]) -> str:
    header = ("host", "reachable", "orc_sha", "skew", "load1", "disk_free", "in_flight", *HARNESSES)
    table = [header]
    errors = []
    for row in view["hosts"]:
        if not row["reachable"]:
            table.append((row["host"], "no", *("-",) * (len(header) - 2)))
            errors.append(f"{row['host']}: {row['error']}")
            continue
        harnesses = row.get("harnesses") or {}
        load = row.get("load") or [None]
        table.append((row["host"], "yes", (row.get("orc_sha") or "-")[:12], "skew" if row["skew"] else "-",
                      "-" if load[0] is None else f"{load[0]:.2f}", _gigabytes(row.get("disk_free_bytes")),
                      str(row.get("runs_in_flight", "-")),
                      *(_version(harnesses.get(agent)) for agent in HARNESSES)))
    widths = [max(len(str(line[index])) for line in table) for index in range(len(header))]
    lines = ["  ".join(str(cell).ljust(width) for cell, width in zip(line, widths)).rstrip() for line in table]
    return "\n".join(lines + [f"unreachable {line}" for line in errors])


def render_info(info: dict[str, Any]) -> str:
    load = info.get("load")
    lines = [f"host: {info['host']}", f"orc_sha: {info['orc_sha'] or 'unknown'}", f"python: {info['python']}",
             "load: " + (" ".join(f"{value:.2f}" for value in load) if load else "unknown"),
             f"disk free: {_gigabytes(info['disk_free_bytes'])}", f"runs in flight: {info['runs_in_flight']}"]
    lines += [f"{agent}: {version or 'absent'}" for agent, version in info["harnesses"].items()]
    lines += [f"quota {account}: {classification}" for account, classification in (info.get("quota") or {}).items()]
    return "\n".join(lines)


def add_parser(sub) -> None:
    import argparse
    info = sub.add_parser("host-info", help="this host's ORC commit, harness versions, load, disk, runs in flight and quota",
                          description="Print this host's facts: host, orc_sha, python, the claude, codex, agy and "
                                      "opencode versions (null when absent), the 1/5/15-minute load, free disk under the "
                                      "control workspace, runs in flight and the quota classification per account. Makes "
                                      "no network calls and prints no secrets. Exit codes: 0 printed; 2 hosts.json refused.")
    info.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    table = sub.add_parser("hosts", help="host-info here and over ssh on each registered host, as one table",
                           description="Run host-info here and on each host in hosts.json with `ssh`, through "
                                       "`ssh -o BatchMode=yes -o ConnectTimeout=5 SSH FUSION --json host-info`. An "
                                       "unreachable host is a row with reachable false and its error; a host whose "
                                       "orc_sha differs from this one is marked skew. Exit codes: 0 printed; 2 hosts.json refused.")
    table.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def run(args, workspace: Path, config: dict[str, Any]) -> int:
    try:
        view = host_info(workspace, config) if args.command == "host-info" else hosts(workspace, config)
    except HostsError as exc:
        print(f"fusion: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
    else:
        print(render_info(view) if args.command == "host-info" else render_hosts(view))
    return 0
