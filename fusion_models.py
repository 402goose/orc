"""Provider catalog metadata is separate from benchmark and harness evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.request

import fusion_core as core

SOURCE = "https://openrouter.ai/api/v1/models"
MAX_BYTES = 8_000_000


def home():
    return Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc")


def parsed_models(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("Model catalog exceeds 8 MB")
    value = json.loads(raw)
    rows = value.get("data") if isinstance(value, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("Provider catalog must contain a nonempty data array")
    result = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or item["id"] in seen:
            raise ValueError("Provider catalog contains an invalid or duplicate model identity")
        seen.add(item["id"])
        result.append({"id": item["id"], "name": str(item.get("name") or item["id"]),
                       "created": item.get("created"),
                       "context_length": item.get("context_length"),
                       "tools": "tools" in (item.get("supported_parameters") or []),
                       "pricing": item.get("pricing") if isinstance(item.get("pricing"), dict) else {}})
    return result


def atomic_bytes(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".models-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def catalog(workspace):
    cache = home() / "models.json"
    result = {"source": SOURCE, "status": "missing", "cached_at_ms": None, "count": 0, "models": [],
              "availability": "provider_catalog_only", "account_access": "unchecked"}
    if cache.exists():
        try:
            rows = parsed_models(cache.read_bytes())
            stamp = int(cache.stat().st_mtime * 1000)
            result.update(status="cached" if time.time() * 1000 - stamp < 86400000 else "stale",
                          cached_at_ms=stamp, count=len(rows), models=rows)
        except (OSError, ValueError, TypeError) as exc:
            result.update(status="unreadable", error=str(exc))
    quality = home() / "quality.json"
    if not quality.exists():
        quality = Path(__file__).with_name("data") / "quality.json"
    benchmark = {"source": None, "fetched_at": None, "status": "missing", "record_count": 0}
    try:
        saved = json.loads(quality.read_text())
        rows = saved.get("records")
        if not isinstance(rows, list):
            raise ValueError("Benchmark records are unreadable")
        benchmark.update(source=saved.get("source"), fetched_at=saved.get("fetchedAt"),
                         status="snapshot", record_count=len(rows))
    except (OSError, ValueError, TypeError, AttributeError):
        benchmark["status"] = "unreadable" if quality.exists() else "missing"
    try:
        config, _ = core.load_config(Path(workspace))
        workers = [{**core.worker_availability(config, agent), "model": config.get(agent, {}).get("model") or None}
                   for agent in ("codex", "claude", "agy", "grok")]
    except (SystemExit, OSError, ValueError):
        workers = []
    return {"schema": "fusion.model-catalog.v1", "catalog": result, "benchmark": benchmark,
            "workers": workers, "note": "Catalog listing, benchmark scores, account access and verified harness execution are separate evidence."}


def refresh(workspace):
    """Explicit public metadata refresh; no credentials, prompts or model calls."""
    error = None
    try:
        request = urllib.request.Request(SOURCE, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
        parsed_models(raw)  # Keep the previous catalog if the response is invalid.
        atomic_bytes(home() / "models.json", raw)
    except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
        error = str(exc)[:500]
    value = catalog(workspace)
    value["refresh"] = {"ok": error is None, "error": error, "at_ms": core.now_ms()}
    return value
