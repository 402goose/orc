"""Per-run temp directories for sandboxed readers, and the sweep that removes stale ones.

A reader's `/tmp/orc-<name>` directory is removed when its run ends. A run that is
SIGKILLed never gets there, so each directory carries an owner marker
(`.orc-owner`: the creating process's pid and start time) and every new directory
first sweeps old ones. A directory is removed only when it is a plain directory (not
a symlink) owned by this user, its name matches the pattern ORC creates (`orc-run-`
plus 8 mkdtemp characters or 12 hex digits; the older `orc-` form is still recognised), it is
older than the age limit, it carries ORC's owner marker, and that owner is gone: the
marker's pid is dead or belongs to a different process now (another start time). A
directory without a readable marker is never removed, so a same-named directory that
ORC did not create (or one from before markers existed) is left alone.

For a Claude reader the directory root is outside what the worker may write (it gets
`<root>/claude-<uid>`), so its marker cannot be forged. A Codex browser reader may
write its whole directory, marker included; a forged marker can only keep that
directory from being swept early, never cause another directory to be removed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any

ROOT = Path("/tmp").resolve()
NAME = re.compile(r"orc-run-(?:[a-z0-9_]{8}|[0-9a-f]{12})|orc-(?:[a-z0-9_]{8}|[0-9a-f]{12})")
MARKER = ".orc-owner"
MAX_AGE_SECONDS = 24 * 3600
LIMIT = 500
_warned = False


def process_start(pid: int) -> str | None:
    """A process's start time as `ps` reports it, or None when it cannot be read."""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(out.stdout.split())
    return text or None


def mark(directory: str | Path) -> bool:
    """Record this process as the owner of a per-run directory. A marker that cannot be
    written does not stop the run: runs end long before the age limit, so the sweep still
    leaves a live directory alone."""
    pid = os.getpid()
    try:
        fd = os.open(Path(directory) / MARKER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": pid, "start": process_start(pid)}, handle)
    except OSError:
        return False
    return True


def _owner(directory: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(directory / MARKER, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, encoding="utf-8") as handle:
            owner = json.loads(handle.read(4096))
    except (OSError, ValueError):
        return None
    return owner if isinstance(owner, dict) else None


def _alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_running(owner: dict[str, Any] | None) -> bool:
    if owner is None or not _alive(owner.get("pid")):
        return False
    recorded = owner.get("start")
    if not recorded:
        return True
    current = process_start(int(owner["pid"]))
    return current is None or current == recorded


def sweep(root: str | Path | None = None, max_age_seconds: float = MAX_AGE_SECONDS, dry_run: bool = False,
          now: float | None = None, limit: int = LIMIT) -> dict[str, Any]:
    """Remove stale per-run directories under `root` (default /tmp). Returns what was
    removed (or, with dry_run, would be), what matched but was kept and why, and errors."""
    root = Path(root) if root is not None else ROOT
    now = time.time() if now is None else now
    uid = os.getuid()
    removed: list[str] = []
    kept: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        return {"root": str(root), "dry_run": dry_run, "removed": removed, "kept": kept,
                "errors": [{"path": str(root), "error": str(exc)}]}
    for name in [name for name in names if NAME.fullmatch(name)][:limit]:
        path = root / name
        try:
            info = os.lstat(path)
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        if not stat.S_ISDIR(info.st_mode):
            kept.append({"path": str(path), "reason": "not a directory"})
            continue
        if info.st_uid != uid:
            kept.append({"path": str(path), "reason": "owned by another user"})
            continue
        if now - info.st_mtime < max_age_seconds:
            kept.append({"path": str(path), "reason": "younger than the age limit"})
            continue
        owner = _owner(path)
        if owner is None or not isinstance(owner.get("pid"), int):
            kept.append({"path": str(path), "reason": "no ORC owner marker"})
            continue
        if _owner_running(owner):
            kept.append({"path": str(path), "reason": "owner still running"})
            continue
        if not dry_run:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue
        removed.append(str(path))
    return {"root": str(root), "dry_run": dry_run, "removed": removed, "kept": kept, "errors": errors}


def sweep_quietly(root: str | Path | None = None) -> None:
    """The sweep a new per-run directory triggers: never raises, warns once per process."""
    global _warned
    if os.environ.get("FUSION_TMP_SWEEP") == "0":
        return
    try:
        result = sweep(root)
        failure = result["errors"][0]["error"] if result["errors"] else None
    except Exception as exc:
        failure = str(exc)
    if failure and not _warned:
        _warned = True
        print(f"fusion: temp sweep skipped some directories: {failure}", file=sys.stderr)
