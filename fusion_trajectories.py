"""Settled runs as a trajectory dataset: `fusion trajectories export` and `parquet`.

A run's harness stdout, task, diff and effective verdict are joined into one
run row and its normalised steps (fusion_core.harness_steps). Only runs a
verdict settled leave, only from repos on the requested side of the data wall,
and every exported string passes the redactor. See docs/trajectories.md.
"""
from collections import Counter
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sys

SCHEMA = "fusion.trajectories.v1"
SCHEMA_VERSION = 1
STEPS_PER_CHUNK = 10000
HELDOUT_FRACTION = 0.1
WALL_SIDES = ("personal", "employer")
WALL_KEYS = (*WALL_SIDES, "prerelease")
RUN_FIELDS = ("run_id", "issue", "role", "write", "agent", "lane_key", "model", "reasoning_effort", "repo", "host",
              "task", "outcome", "diff", "diff_truncated", "usage", "cost_usd", "cost_estimated", "split", "provenance",
              "steps")
STEP_FIELDS = ("run_id", "index", "kind", "role", "text", "tool", "arguments", "result", "is_error", "time_ms", "raw_type")
OUTCOME_FIELDS = ("accepted", "stage", "rejection_class", "reporter", "reason")
# Runs whose failure was the harness's, not the work's: no verdict on them is a label.
HARNESS_FAILURES = frozenset({"permission_denied", "auth", "missing_executable", "coordinator_error", "verdict_missing"})
REDACTED = "[REDACTED]"


def add_parser(sub):
    parser = sub.add_parser("trajectories", help="export settled runs as a trajectory dataset (runs, steps, verdicts)")
    commands = parser.add_subparsers(dest="trajectories_command", required=True)
    export = commands.add_parser(
        "export", help="write settled runs and their normalised steps as JSONL",
        description="Write meta/info.json, meta/runs.jsonl and steps/chunk-NNN.jsonl. Only runs with an effective "
                    "accepted or rejected verdict, from repos on the --for side of export.walls, are written; every "
                    "excluded run is counted by reason in info.json.")
    export.add_argument("--out", required=True, metavar="DIR", help="output directory; must not exist or be empty")
    export.add_argument("--for", dest="wall_side", required=True, choices=WALL_SIDES,
                        help="which side of export.walls to export; a repo on both sides or neither is excluded")
    export.add_argument("--since", type=float, metavar="DAYS", help="only runs started in the last DAYS days")
    export.add_argument("--role", help="only runs of this role")
    export.add_argument("--include-prerelease", action="store_true", help="also export repos listed in export.walls.prerelease")
    export.add_argument("--dry-run", action="store_true", help="print the counts per filter and exclusion reason; write nothing")
    parquet = commands.add_parser("parquet", help="convert an export to parquet; needs pyarrow")
    parquet.add_argument("directory", help="a directory written by trajectories export")


class Redactor:
    """Masks common key shapes, Authorization header values, secret-named fields, and the value
    of any environment variable whose name has KEY, TOKEN or SECRET in it, counting each mask."""
    PATTERNS = (
        re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S),
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    )
    HEADER = re.compile(r"""(\bauthorization["']?\s*[:=]\s*["']?)[^\r\n"']+""", re.I)
    ENV_NAME = re.compile(r"KEY|TOKEN|SECRET", re.I)
    MIN_ENV_VALUE = 8  # shorter values ("1", "true") are settings, not credentials

    def __init__(self, environ=None):
        environ = os.environ if environ is None else environ
        self.values = sorted({value for name, value in environ.items()
                              if self.ENV_NAME.search(name) and len(value or "") >= self.MIN_ENV_VALUE}, key=len, reverse=True)
        self.count = 0

    def text(self, value):
        for secret in self.values:
            if secret in value:
                self.count += value.count(secret)
                value = value.replace(secret, REDACTED)
        for pattern in self.PATTERNS:
            value, found = pattern.subn(REDACTED, value)
            self.count += found
        value, found = self.HEADER.subn(lambda match: match.group(1) + REDACTED, value)
        self.count += found
        return value

    def __call__(self, value):
        from fusion_core import SECRET
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, dict):
            masked = {}
            for key, item in value.items():
                if SECRET.search(str(key)) and isinstance(item, str) and item and item != REDACTED:
                    self.count += 1
                    masked[key] = REDACTED
                else:
                    masked[key] = self(item)
            return masked
        return value


def walls(config):
    """export.walls, validated: each of personal, employer and prerelease a list of repo patterns."""
    value = ((config or {}).get("export") or {}).get("walls") or {}
    if not isinstance(value, dict) or set(value) - set(WALL_KEYS) or not all(
            isinstance(patterns, list) and all(isinstance(item, str) and item for item in patterns) for patterns in value.values()):
        raise ValueError("export.walls must map personal, employer and prerelease to lists of owner/name patterns")
    return {key: [pattern.lower() for pattern in value.get(key, [])] for key in WALL_KEYS}


def wall_reason(config, slug, side, include_prerelease=False):
    """Why a known repo may not be exported to `side`, or None. A repo on both sides, or neither, is excluded."""
    table = walls(config)
    on = {key: any(fnmatch.fnmatchcase(slug.lower(), pattern) for pattern in table[key]) for key in WALL_KEYS}
    if on["personal"] and on["employer"]:
        return "wall_both"
    if not on["personal"] and not on["employer"]:
        return "wall_neither"
    if not on[side]:
        return "wall_other_side"
    if on["prerelease"] and not include_prerelease:
        return "prerelease"
    return None


def split_for(issue, fallback):
    """train or heldout by a sha256 of the issue, so every run of one issue lands on one side.
    A run without an issue is split by its workflow group."""
    key = str(issue).lower() if issue else str(fallback)
    score = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) / 0x100000000
    return "heldout" if score < HELDOUT_FRACTION else "train"


def settle_reason(run_id, result, outcome, audit):
    """Why a run is not settled by its verdict, or None when it is."""
    import fusion_core as core
    from fusion_policy import HARNESS_REJECTION_CLASSES
    if outcome is None:
        for reason in ("check_inputs_changed", "withdrawn", "unmeasured"):
            if run_id in audit[reason]:
                return reason
        return "no_outcome"
    if outcome.get("rejection_class") == "eval_unmeasured":
        return "eval_unmeasured"
    verdict = result.get("verdict") or core.classify_verdict(result)["verdict"]
    if verdict == "quota":
        return "quota"
    if verdict == "refused":
        return "refusal"
    if core.failure_class(result) in HARNESS_FAILURES or outcome.get("rejection_class") in HARNESS_REJECTION_CLASSES:
        return "harness_failure"
    return None


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _lane_key(value):
    """A lane key with any account path cut to its last component, so no home path leaves."""
    if not value:
        return None
    return "@".join(Path(part).name if part.startswith(("/", "~")) else part for part in str(value).split("@"))


def run_ids(workspace):
    """Every completed run of the workspace, including runs a workflow worktree recorded, by run id."""
    import fusion_core as core
    store = core.RunStore(workspace)
    names = {path.name for path in store.runs.glob("*")}
    names |= {path.name for path in (store.workspace / ".fusion" / "worktrees").glob("*/.fusion/runs/*")}
    return {name: directory for name in sorted(names) if (directory := core.run_directory(store.workspace, name))}


def collect(workspace, config, side, since_days=None, role=None, include_prerelease=False, environ=None):
    """(run rows, {run_id: step rows}, excluded counts, redactor) for an export."""
    import fusion_core as core
    from fusion_decisions import DecisionStore, normalize_role, read_jsonl
    from fusion_policy import effective_outcomes
    walls(config)
    events = read_jsonl(DecisionStore(workspace).path)
    outcomes = effective_outcomes(events)
    audit = {"withdrawn": {e.get("task_id") for e in events if e.get("event") == "outcome_withdraw"},
             "unmeasured": {e.get("task_id") for e in events if e.get("event") == "outcome_unmeasured"},
             "check_inputs_changed": {e.get("task_id") for e in events
                                      if e.get("event") in {"outcome", "outcome_excluded"} and e.get("check_inputs_changed")}}
    cutoff = core.now_ms() - since_days * 86400000 if since_days is not None else None
    wanted_role = normalize_role(role) if role else None
    redact = Redactor(environ)
    runs, steps, excluded = [], {}, Counter()
    for run_id, run_dir in run_ids(workspace).items():
        result, task = _read_json(run_dir / "result.json"), _read_json(run_dir / "task.json")
        run_role = result.get("role") or task.get("role")
        if cutoff is not None and core.run_created_ms(run_dir) < cutoff:
            excluded["filter_since"] += 1
            continue
        if wanted_role and normalize_role(run_role) != wanted_role:
            excluded["filter_role"] += 1
            continue
        outcome = outcomes.get(run_id)
        if reason := settle_reason(run_id, result, outcome, audit):
            excluded[reason] += 1
            continue
        slug = core.run_repo(workspace, run_id)
        if reason := core.excluded_repo(config, slug):
            excluded["unknown_repo" if reason == "unknown repo" else "excluded_repo"] += 1
            continue
        if reason := wall_reason(config, slug, side, include_prerelease):
            excluded[reason] += 1
            continue
        try:
            stdout = (run_dir / "stdout.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            stdout = ""
        resolved = task.get("resolved") if isinstance(task.get("resolved"), dict) else {}
        agent = result.get("agent") or task.get("agent")
        steps[run_id] = [{"run_id": run_id, "index": index, **{key: redact(value) if key in {"text", "arguments", "result"} else value
                                                                for key, value in step.items()}}
                         for index, step in enumerate(core.harness_steps(agent, stdout, resolved.get("output_format")))]
        try:
            diff = (run_dir / "diff.patch").read_text(encoding="utf-8", errors="replace")
        except OSError:
            diff = None
        trace = _read_json(run_dir / "trace.json")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        issue = outcome.get("issue") or result.get("issue") or task.get("issue")
        runs.append({
            "run_id": run_id, "issue": issue, "role": run_role, "write": bool(task.get("write", result.get("write"))),
            "agent": agent, "lane_key": _lane_key(resolved.get("lane_key") or trace.get("lane_key")),
            "model": result.get("model"), "reasoning_effort": result.get("reasoning_effort"), "repo": slug,
            "host": (result.get("endpoint") or {}).get("host"),
            "task": redact({"text": task.get("task"), "success_criteria": task.get("success_criteria") or [],
                            "constraints": task.get("constraints") or []}),
            "outcome": {key: redact(outcome.get(key)) if key == "reason" else outcome.get(key) for key in OUTCOME_FIELDS},
            "diff": redact(diff[:core.DIFF_LIMIT]) if diff is not None else None,
            "diff_truncated": diff is not None and len(diff) > core.DIFF_LIMIT,
            "usage": usage, "cost_usd": usage.get("cost_usd"), "cost_estimated": bool(usage.get("cost_estimated")),
            "split": split_for(issue, result.get("trace_id") or task.get("trace_id") or run_id),
            "provenance": result.get("provenance") if isinstance(result.get("provenance"), dict) else core.derived_provenance(task),
            "steps": len(steps[run_id]),
        })
    return runs, steps, dict(sorted(excluded.items())), redact


def _write_jsonl(path, rows):
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def export(workspace, config, out, side, since_days=None, role=None, include_prerelease=False, dry_run=False, environ=None):
    """Export settled runs to `out` (or, with dry_run, only count them). Returns the summary."""
    import fusion_core as core
    if side not in WALL_SIDES:
        raise ValueError("--for must be personal or employer")
    if since_days is not None and since_days <= 0:
        raise ValueError("--since must be a positive number of days")
    out = Path(out).expanduser().resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} already exists and is not an empty directory")
    runs, steps, excluded, redact = collect(workspace, config, side, since_days, role, include_prerelease, environ)
    chunks, current = [], []
    for run in runs:
        if len(current) >= STEPS_PER_CHUNK:
            chunks.append(current)
            current = []
        current.extend(steps[run["run_id"]])
    if current or not chunks:
        chunks.append(current)
    info = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "orc_sha": core.orc_sha(),
            "totals": {"runs": len(runs), "steps": sum(len(chunk) for chunk in chunks), "chunks": len(chunks),
                       "splits": dict(sorted(Counter(run["split"] for run in runs).items())),
                       "accepted": sum(run["outcome"]["accepted"] is True for run in runs),
                       "rejected": sum(run["outcome"]["accepted"] is False for run in runs)},
            "fields": {"runs": list(RUN_FIELDS), "steps": list(STEP_FIELDS)},
            "filters": {"for": side, "since_days": since_days, "role": role, "include_prerelease": include_prerelease},
            "excluded": excluded, "redactions": redact.count}
    if dry_run:
        return {"dry_run": True, "out": str(out), **{key: info[key] for key in ("totals", "filters", "excluded", "redactions")}}
    (out / "meta").mkdir(parents=True, mode=0o700)
    (out / "steps").mkdir(mode=0o700)
    for number, chunk in enumerate(chunks):
        _write_jsonl(out / "steps" / f"chunk-{number:03d}.jsonl", chunk)
    _write_jsonl(out / "meta" / "runs.jsonl", runs)
    with open(out / "meta" / "info.json", "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
        handle.write(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"path": str(out), **{key: info[key] for key in ("totals", "filters", "excluded", "redactions")}}


PARQUET_JSON_FIELDS = {"runs": ("task", "outcome", "usage", "provenance"), "steps": ("arguments",)}


def to_parquet(directory):
    """Write a .parquet beside each JSONL file of an export. Structured fields whose shape varies
    by harness are stored as JSON text. Raises ValueError without pyarrow."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        raise ValueError("trajectories parquet needs pyarrow, which is not installed here; the JSONL export is "
                         "complete without it (install pyarrow, for example with pip install pyarrow, to convert)") from None
    directory = Path(directory).expanduser().resolve()
    if _read_json(directory / "meta" / "info.json").get("schema") != SCHEMA:
        raise ValueError(f"{directory} is not a trajectories export (no meta/info.json with schema {SCHEMA})")
    written = []
    for kind, paths in (("runs", [directory / "meta" / "runs.jsonl"]), ("steps", sorted((directory / "steps").glob("chunk-*.jsonl")))):
        for path in paths:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            fields = RUN_FIELDS if kind == "runs" else STEP_FIELDS
            rows = [{key: json.dumps(row.get(key), ensure_ascii=False, sort_keys=True)
                     if key in PARQUET_JSON_FIELDS[kind] or isinstance(row.get(key), (dict, list)) else row.get(key)
                     for key in fields} for row in rows]
            target = path.with_suffix(".parquet")
            pq.write_table(pa.Table.from_pylist(rows), target)
            written.append(str(target))
    return {"path": str(directory), "written": written}


def run(args, workspace, config):
    if args.trajectories_command == "parquet":
        try:
            payload = to_parquet(args.directory)
        except ValueError as exc:
            print(f"fusion trajectories parquet: {exc}", file=sys.stderr)
            return 1
    else:
        payload = export(workspace, config, args.out, args.wall_side, args.since, args.role, args.include_prerelease, args.dry_run)
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0
