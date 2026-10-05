"""Outcome predictor: predict real lifecycle outcomes of dispatched attempts.

`build-dataset` writes one row per attempt (a build round). Generic ORC
records supply the lane and the reviewed stages: `runs/<id>/task.json` and
`result.json`, `traces.jsonl`, and the `outcome`, `outcome_withdraw` and
`routing_log` events of `decisions/events.jsonl`. Lifecycle sources are
optional adapters; the TENET adapter (`--tenet-repo PATH`) reads a repo's
`.tenet/build-journal.jsonl` (rounds and their outcomes, joined to ORC runs
by the executor's run id), `.tenet/verify/<issue>/*.json` and, through `gh`,
the repo's `issue/<N>-` pull requests.

Labels are ground truth, not a lead's verdict: `verify` (the run's verify
stage, else the first verify record of the issue that measured this round as
the latest kept round), `kept` (the gate kept the round) and `landed` (the issue's PR merged
and was not reverted; unknown while the issue is open or recent). Two
feature sets: `routing` holds only what is known before a lane is picked;
`verify` adds what the finished worker reported, which is known before
verify runs. Lane is agent + model + effort, so a new model is a new lane.

`evaluate` fits a logistic regression (lane interactions, strong L2) and a
gradient-boosting baseline on stdlib Python and reports held-out metrics
against majority and a per-lane Beta(1,1) posterior mean, which is what
Thompson routing knows. Splits keep every attempt of an issue on the side
of the issue's first attempt. Nothing here changes routing.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess

SCHEMA = "fusion.outcome-row.v1"
LABELS = ("verify", "kept", "landed")
ROUTING_FEATURES = ("agent", "model", "effort", "lane", "repo", "kind", "round", "prior_rounds", "prior_failed",
                    "prior_kept", "prior_rejections", "prior_best_score", "task_chars", "hour", "issue_age_h")
WORKER_FEATURES = ("score", "delta", "missing_required", "files", "frozen_unverified", "worker_status",
                   "executor_status", "cost_usd", "duration_s", "blockers", "denied")
# Known only after the lane was picked: never a routing feature.
POST_DISPATCH = frozenset(WORKER_FEATURES) | {"kept", "reviewer", "rejection_class"}
FEATURE_SETS = {"routing": ROUTING_FEATURES, "verify": ROUTING_FEATURES + WORKER_FEATURES}
CATEGORICAL = frozenset({"agent", "model", "effort", "lane", "repo", "kind", "worker_status", "executor_status"})
INTERACTIONS = {"routing": ("kind", "prior_failed"), "verify": ("kind", "prior_failed", "score")}
TIME_CUTOFF = "2026-10-03T00:00:00Z"
CENSOR_HOURS = 48
FEW = 30


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def ms(value):
    """Epoch milliseconds from an ISO time or a number; None when unreadable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def num(value):
    if isinstance(value, bool):
        return float(value)
    return float(value) if isinstance(value, (int, float)) else None


def agent_of(model):
    model = (model or "").lower()
    return "claude" if "claude" in model else "codex" if model.startswith(("gpt", "o3", "o4", "codex")) else model.split("-")[0] or None


# ---- generic ORC records -------------------------------------------------

def orc_records(fusion_dir):
    """{runs, outcomes, routing} from an ORC control store's `.fusion` directory."""
    fusion_dir = Path(fusion_dir)
    runs = {}
    for trace in read_jsonl(fusion_dir / "traces.jsonl"):
        if trace.get("run_id") and not trace.get("parent_span_id"):
            runs[trace["run_id"]] = {"agent": trace.get("agent"), "model": trace.get("model"), "role": trace.get("role"),
                                     "effort": trace.get("reasoning_effort"), "route": trace.get("route"),
                                     "repo": trace.get("repo"), "write": trace.get("write")}
    root = fusion_dir / "runs"
    for directory in sorted(root.iterdir()) if root.is_dir() else []:
        task, result = _json(directory / "task.json"), _json(directory / "result.json") or {}
        if not task:
            continue
        overrides = task.get("settings_overrides") or {}
        run = runs.setdefault(directory.name, {})
        for key, value in {"agent": result.get("agent") or task.get("agent"), "role": task.get("role"),
                           "model": result.get("model") or overrides.get("model"),
                           "effort": result.get("reasoning_effort") or overrides.get("reasoning_effort"),
                           "route": task.get("route"), "repo": task.get("repo"), "write": task.get("write")}.items():
            if value is not None:
                run[key] = value
        run["task_chars"] = len(task.get("task") or "")
        run["created_ms"] = num(task.get("created_at"))
        run["workspace"] = task.get("workspace")
    outcomes, routing = defaultdict(dict), {}
    for event in read_jsonl(fusion_dir / "decisions" / "events.jsonl"):
        kind, task_id = event.get("event"), event.get("task_id")
        if not task_id:
            continue
        if kind == "outcome" and event.get("stage"):
            outcomes[task_id][event["stage"]] = {"accepted": bool(event.get("accepted")), "time_ms": event.get("time_ms"),
                                                 "rejection_class": event.get("rejection_class")}
        elif kind == "outcome_withdraw":
            outcomes[task_id] = {stage: value for stage, value in outcomes[task_id].items()
                                 if (value.get("time_ms") or 0) > (event.get("time_ms") or 0)}
        elif kind == "routing_log" and event.get("candidates"):
            candidates = [{"key": c.get("key"), "agent": c.get("agent"), "model": c.get("model"),
                           "effort": c.get("reasoning_effort"), "propensity": num(c.get("propensity")),
                           "cost_tier": num(c.get("cost_tier")), "mean_cost_usd": num(c.get("mean_cost_usd"))}
                          for c in event["candidates"] if isinstance(c, dict)]
            chosen = next((c for c in candidates if c["key"] == event.get("chosen")), None)
            routing[task_id] = {"chosen": event.get("chosen"), "propensity": chosen and chosen["propensity"],
                                "explored": event.get("explored"), "candidates": candidates}
    return {"runs": runs, "outcomes": dict(outcomes), "routing": routing}


# ---- TENET adapter -------------------------------------------------------

ISSUE_AGENT = re.compile(r"^build-(?:s-)?(\d+)-")
ISSUE_BRANCH = re.compile(r"^issue/(\d+)-")


def tenet_attempts(repo):
    """Generic attempts from a TENET build journal: each build:round with its build:outcome."""
    repo = Path(repo)
    name, pending = repo.name, {}
    for row in read_jsonl(repo / ".tenet" / "build-journal.jsonl"):
        key = (row.get("agent"), row.get("round"))
        if row.get("type") == "build:outcome":
            pending[key] = row
        if row.get("type") != "build:round":
            continue
        outcome = pending.pop(key, None) or {}
        if outcome and abs((ms(outcome.get("ts")) or 0) - (ms(row.get("ts")) or 0)) > 3_600_000:
            outcome = {}
        executor = outcome.get("executor") or {}
        spawn = executor.get("spawn") or {}
        blockers = [str(b) for b in executor.get("blockers") or []]
        text = " ".join([*map(str, outcome.get("reasons") or []), *blockers])
        match = ISSUE_AGENT.match(row.get("agent") or "")
        yield {"source": "tenet", "repo": name, "issue": int(match.group(1)) if match else None, "unit": row.get("agent"),
               "round": row.get("round"), "ts": ms(row.get("ts")), "kept": bool(row.get("kept")),
               "score": num(row.get("score")), "delta": num(row.get("delta")), "run_id": executor.get("runId"),
               "model": executor.get("model") or spawn.get("model") or None, "effort": spawn.get("effort"),
               "cost_usd": num(executor.get("costUsd")), "duration_ms": num(executor.get("durationMs")),
               "files": len(outcome["files"]) if isinstance(outcome.get("files"), list) else None,
               "missing_required": len(outcome["missingRequired"]) if isinstance(outcome.get("missingRequired"), list) else None,
               "worker_status": outcome.get("status"), "executor_status": executor.get("status"),
               "blockers": len(blockers) if executor else None,
               "denied": sum("permission denied" in b.lower() for b in blockers) if executor else None,
               "frozen_unverified": ("proof_keeps_frozen_suites" in text) if outcome else None}


def tenet_verifies(repo):
    """[{issue, ts, clean}] from tenet.verify.v1 records; records keyed only by sha cannot join an issue."""
    root = Path(repo) / ".tenet" / "verify"
    records = []
    for directory in sorted(root.iterdir()) if root.is_dir() else []:
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        for path in sorted(directory.glob("*.json")):
            record = _json(path)
            if record and record.get("schema") == "tenet.verify.v1" and isinstance(record.get("clean"), bool):
                records.append({"issue": int(record.get("issue") or directory.name), "ts": ms(record.get("ts")),
                                "clean": record["clean"]})
    return records


def gh_pulls(repo):
    out = subprocess.run(["gh", "pr", "list", "--state", "all", "--limit", "3000", "--json",
                          "number,headRefName,title,state,mergedAt,closedAt"],
                         cwd=repo, capture_output=True, text=True, timeout=120, check=True).stdout
    return json.loads(out or "[]")


def tenet_landings(pulls):
    """{issue: {landed, open, reverted}} from `issue/<N>-` pull requests; a merged revert un-lands its PR."""
    reverted = {m.group(1) for p in pulls if p.get("state") == "MERGED"
                for m in [re.match(r'^Revert "(.*)"$', p.get("title") or "")] if m}
    issues = {}
    for pull in pulls:
        match = ISSUE_BRANCH.match(pull.get("headRefName") or "")
        if not match:
            continue
        entry = issues.setdefault(int(match.group(1)), {"landed": False, "open": False, "reverted": False, "merged_ms": None})
        if pull.get("state") == "OPEN":
            entry["open"] = True
        if pull.get("state") == "MERGED":
            if pull.get("title") in reverted:
                entry["reverted"] = True
            else:
                entry["landed"], entry["merged_ms"] = True, ms(pull.get("mergedAt"))
    return issues


# ---- dataset -------------------------------------------------------------

def lane_of(agent, model, effort):
    return f"{agent or '?'}/{model or '?'}/{effort or 'default'}"


def build_rows(attempts, orc=None, verifies=None, landings=None, now_ms=None):
    """Dataset rows from generic attempts. verifies and landings are keyed by (repo, issue)."""
    orc = orc or {"runs": {}, "outcomes": {}, "routing": {}}
    verifies, landings = verifies or {}, landings or {}
    groups = defaultdict(list)
    for attempt in attempts:
        if attempt.get("ts"):
            groups[(attempt["repo"], attempt["issue"] if attempt.get("issue") is not None else attempt["unit"])].append(attempt)
    rows = []
    for (repo, group), items in groups.items():
        items.sort(key=lambda a: a["ts"])
        first, last = items[0]["ts"], items[-1]["ts"]
        issue = items[0].get("issue")
        landing = landings.get((repo, issue)) if issue is not None else None
        if issue is None or landing and landing["open"] and not landing["landed"]:
            landed = None
        elif landing and landing["landed"]:
            landed = True
        elif landing or now_ms is None or now_ms - last >= CENSOR_HOURS * 3_600_000:
            landed = False
        else:
            landed = None
        # A verify record measured the branch head: the latest kept round at or before it.
        measured = {}
        for record in sorted((v for v in verifies.get((repo, issue), []) if v["ts"]), key=lambda v: v["ts"]):
            head = max((i for i, a in enumerate(items) if a["kept"] and a["ts"] <= record["ts"]), default=None)
            if head is not None:
                measured.setdefault(head, record["clean"])
        history = []
        for index, attempt in enumerate(items):
            run = orc["runs"].get(attempt.get("run_id")) or {}
            stages = orc["outcomes"].get(attempt.get("run_id")) or {}
            model = run.get("model") or attempt.get("model")
            agent = run.get("agent") or agent_of(model)
            effort = run.get("effort") or attempt.get("effort")
            dispatched = attempt["ts"] - int(attempt.get("duration_ms") or 0)
            prior_rejections = sum(1 for earlier in history for stage in (orc["outcomes"].get(earlier.get("run_id")) or {}).values()
                                   if not stage["accepted"] and (stage.get("time_ms") or 0) < dispatched)
            scores = [h["score"] for h in history if h.get("score") is not None]
            features = {"agent": agent, "model": model, "effort": effort or "default", "lane": lane_of(agent, model, effort),
                        "repo": repo, "kind": run.get("role") or "unknown", "round": attempt.get("round"),
                        "prior_rounds": len(history), "prior_failed": sum(not h["kept"] for h in history),
                        "prior_kept": sum(h["kept"] for h in history), "prior_rejections": prior_rejections,
                        "prior_best_score": max(scores) if scores else None, "task_chars": run.get("task_chars"),
                        "hour": datetime.fromtimestamp(dispatched / 1000, timezone.utc).hour,
                        "issue_age_h": round((dispatched - first) / 3_600_000, 3),
                        "duration_s": attempt["duration_ms"] / 1000 if attempt.get("duration_ms") is not None else None,
                        **{key: attempt.get(key) for key in WORKER_FEATURES if key != "duration_s"}}
            verify, verify_source = None, None
            if "verify" in stages:
                verify, verify_source = stages["verify"]["accepted"], "orc"
            elif index in measured:
                verify, verify_source = measured[index], "tenet"
            rows.append({"schema": SCHEMA, "id": f"{repo}:{attempt['unit']}:{attempt.get('round')}:{attempt['ts']}",
                         "source": attempt.get("source"), "repo": repo, "group": f"{repo}:{group}", "issue": issue,
                         "ts": attempt["ts"], "time": iso(attempt["ts"]), "group_first_ts": first,
                         "run_id": attempt.get("run_id"), "joined_run": bool(run),
                         "features": features,
                         "labels": {"verify": verify, "kept": attempt["kept"], "landed": landed},
                         "label_sources": {"verify": verify_source, "landed": "gh" if landing else None},
                         "stages": {stage: value["accepted"] for stage, value in stages.items()},
                         "routing": orc["routing"].get(attempt.get("run_id"))})
            history.append(attempt)
    rows.sort(key=lambda r: (r["ts"], r["id"]))
    return rows


def build_dataset(control_workspace, tenet_repos=(), use_gh=True, now_ms=None, pulls=None):
    """Rows plus a coverage summary. pulls: optional {repo path: gh pull list} instead of calling gh."""
    orc = orc_records(Path(control_workspace) / ".fusion")
    attempts, verifies, landings, sources = [], {}, {}, {}
    for repo in tenet_repos:
        repo = Path(repo).expanduser()
        found = list(tenet_attempts(repo))
        attempts += found
        for record in tenet_verifies(repo):
            verifies.setdefault((repo.name, record["issue"]), []).append(record)
        listing = (pulls or {}).get(str(repo))
        if listing is None and use_gh:
            try:
                listing = gh_pulls(repo)
            except (OSError, ValueError, subprocess.SubprocessError):
                listing = None
        for issue, entry in tenet_landings(listing or []).items():
            landings[(repo.name, issue)] = entry
        sources[repo.name] = {"attempts": len(found), "pulls": None if listing is None else len(listing)}
    rows = build_rows(attempts, orc, verifies, landings,
                      now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000))
    return rows, coverage(rows, sources)


def coverage(rows, sources=None):
    by_repo = defaultdict(Counter)
    for row in rows:
        counts = by_repo[row.get("repo")]
        counts["rows"] += 1
        counts["run_id"] += bool(row.get("run_id"))
        counts["joined_run"] += bool(row.get("joined_run"))
        counts["propensity"] += bool(row.get("routing") and row["routing"].get("propensity") is not None)
        counts["issue"] += row.get("issue") is not None
        for label in LABELS:
            value = row["labels"][label]
            counts[f"{label}_known"] += value is not None
            counts[f"{label}_pos"] += value is True
        for label, source in (row.get("label_sources") or {}).items():
            if source:
                counts[f"{label}_from_{source}"] += 1
    return {"sources": sources or {}, "by_repo": {str(repo): dict(counts) for repo, counts in sorted(by_repo.items(), key=lambda item: str(item[0]))},
            "groups": len({row["group"] for row in rows}), "rows": len(rows)}


# ---- features and models -------------------------------------------------

def feature_names(feature_set, label):
    names = FEATURE_SETS[feature_set]
    # The gate keeps a round before verify runs, so verify-time predictions of later stages may use it.
    return names + ("kept",) if feature_set == "verify" and label != "kept" else names


def row_features(row):
    return {**row["features"], "kept": row["labels"]["kept"]}


def fit_encoder(rows, names, interactions=(), min_count=3, min_lane=5):
    spec = []
    for name in names:
        values = [row_features(row).get(name) for row in rows]
        if name in CATEGORICAL:
            counts = Counter(str(v) for v in values if v is not None)
            spec.append({"name": name, "type": "cat", "levels": sorted(k for k, c in counts.items() if c >= min_count)})
        else:
            present = [num(v) for v in values if num(v) is not None]
            mean = sum(present) / len(present) if present else 0.0
            std = math.sqrt(sum((v - mean) ** 2 for v in present) / len(present)) if present else 1.0
            spec.append({"name": name, "type": "num", "mean": mean, "std": std if std > 1e-9 else 1.0,
                         "missing": len(present) < len(values)})
    columns = [column for entry in spec for column in _columns(entry)]
    lanes = Counter(str(row_features(row).get("lane")) for row in rows)
    pairs = [(f"lane={lane}", column) for lane in sorted(lanes) if lanes[lane] >= min_lane and f"lane={lane}" in columns
             for column in columns if column.split("=")[0].split("?")[0] in interactions and column.split("=")[0].split("?")[0] in names]
    return {"spec": spec, "columns": columns, "interactions": pairs}


def _columns(entry):
    if entry["type"] == "cat":
        return [f"{entry['name']}={level}" for level in entry["levels"]]
    return [entry["name"]] + ([f"{entry['name']}?missing"] if entry["missing"] else [])


def encode(row, encoder, standardize=True, interactions=True):
    """Sparse {column index: value} for one row."""
    features, values = row_features(row), {}
    for entry in encoder["spec"]:
        value = features.get(entry["name"])
        if entry["type"] == "cat":
            if value is not None and str(value) in entry["levels"]:
                values[f"{entry['name']}={value}"] = 1.0
        else:
            number = num(value)
            if number is None:
                number = entry["mean"]
                if entry["missing"]:
                    values[f"{entry['name']}?missing"] = 1.0
            values[entry["name"]] = (number - entry["mean"]) / entry["std"] if standardize else number
    index = {column: i for i, column in enumerate(encoder["columns"])}
    sparse = {index[k]: v for k, v in values.items() if k in index and v}
    if interactions:
        base = len(encoder["columns"])
        for offset, (lane, column) in enumerate(encoder["interactions"]):
            product = values.get(lane, 0.0) * values.get(column, 0.0)
            if product:
                sparse[base + offset] = product
    return sparse


def sigmoid(z):
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def _solve(matrix, vector):
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        if abs(a[col][col]) < 1e-12:
            continue
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            if factor:
                row, top = a[r], a[col]
                for c in range(col, n + 1):
                    row[c] -= factor * top[c]
    out = [0.0] * n
    for r in range(n - 1, -1, -1):
        if abs(a[r][r]) >= 1e-12:
            out[r] = (a[r][n] - sum(a[r][c] * out[c] for c in range(r + 1, n))) / a[r][r]
    return out


def fit_logistic(xs, ys, dim, l2=5.0, iterations=25):
    """L2 logistic regression by Newton steps on sparse rows; weight 0 is the unpenalized intercept."""
    rows = [[(0, 1.0)] + [(i + 1, v) for i, v in x.items()] for x in xs]
    d = dim + 1
    w = [0.0] * d
    rate = min(max(sum(ys) / len(ys), 1e-3), 1 - 1e-3)
    w[0] = math.log(rate / (1 - rate))
    for _ in range(iterations):
        grad = [l2 * w[i] if i else 0.0 for i in range(d)]
        hess = [[0.0] * d for _ in range(d)]
        for i in range(1, d):
            hess[i][i] = l2
        hess[0][0] = 1e-6
        for row, y in zip(rows, ys):
            p = sigmoid(sum(w[i] * v for i, v in row))
            g, h = p - y, max(p * (1 - p), 1e-6)
            for i, v in row:
                grad[i] += g * v
                hr = hess[i]
                for j, u in row:
                    hr[j] += h * v * u
        step = _solve(hess, grad)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def predict_logistic(w, x):
    return sigmoid(w[0] + sum(w[i + 1] * v for i, v in x.items()))


def fit_boosting(xs, ys, dim, rounds=100, rate=0.1, depth=3, min_leaf=10, l2=1.0, bins=16):
    """Gradient-boosted trees on log loss (Newton leaves, quantile thresholds)."""
    dense = [[x.get(j, 0.0) for j in range(dim)] for x in xs]
    cuts = []
    for j in range(dim):
        values = sorted({row[j] for row in dense})
        step = max(1, len(values) // bins)
        cuts.append(values[step - 1:-1:step] if len(values) > 1 else [])
    binned = [[sum(v > c for c in cuts[j]) for j, v in enumerate(row)] for row in dense]
    base = min(max(sum(ys) / len(ys), 1e-3), 1 - 1e-3)
    trees, score = [], [math.log(base / (1 - base))] * len(ys)
    gains = defaultdict(float)

    def grow(index, level, g, h):
        total_g, total_h = sum(g[i] for i in index), sum(h[i] for i in index)
        leaf = {"v": -total_g / (total_h + l2) * rate}
        if level >= depth or len(index) < 2 * min_leaf:
            return leaf
        parent, best = total_g ** 2 / (total_h + l2), None
        for j in range(dim):
            if not cuts[j]:
                continue
            sums = [[0.0, 0.0, 0] for _ in range(len(cuts[j]) + 1)]
            for i in index:
                cell = sums[binned[i][j]]
                cell[0] += g[i]
                cell[1] += h[i]
                cell[2] += 1
            lg = lh = 0.0
            ln = 0
            for b, cut in enumerate(cuts[j]):
                lg, lh, ln = lg + sums[b][0], lh + sums[b][1], ln + sums[b][2]
                if ln < min_leaf or len(index) - ln < min_leaf:
                    continue
                gain = lg ** 2 / (lh + l2) + (total_g - lg) ** 2 / (total_h - lh + l2) - parent
                if best is None or gain > best[0]:
                    best = (gain, j, b, cut)
        if not best or best[0] <= 1e-9:
            return leaf
        gain, j, b, cut = best
        gains[j] += gain
        left = [i for i in index if binned[i][j] <= b]
        right = [i for i in index if binned[i][j] > b]
        return {"f": j, "t": cut, "l": grow(left, level + 1, g, h), "r": grow(right, level + 1, g, h)}

    for _ in range(rounds):
        p = [sigmoid(s) for s in score]
        g = [pi - y for pi, y in zip(p, ys)]
        h = [max(pi * (1 - pi), 1e-6) for pi in p]
        tree = grow(list(range(len(ys))), 0, g, h)
        trees.append(tree)
        score = [s + _tree_value(tree, row) for s, row in zip(score, dense)]
    return {"base": math.log(base / (1 - base)), "trees": trees, "gains": dict(gains)}


def _tree_value(tree, row):
    while "v" not in tree:
        tree = tree["l"] if row[tree["f"]] <= tree["t"] else tree["r"]
    return tree["v"]


def predict_boosting(model, x, dim):
    row = [x.get(j, 0.0) for j in range(dim)]
    return sigmoid(model["base"] + sum(_tree_value(tree, row) for tree in model["trees"]))


def lane_posterior(rows, label):
    counts = defaultdict(lambda: [0, 0])
    for row in rows:
        counts[row["features"]["lane"]][0] += row["labels"][label] is True
        counts[row["features"]["lane"]][1] += 1
    return {lane: (pos + 1) / (n + 2) for lane, (pos, n) in counts.items()}


# ---- metrics -------------------------------------------------------------

def auc(ys, ps):
    pairs = sorted(zip(ps, ys))
    positives = sum(ys)
    negatives = len(ys) - positives
    if not positives or not negatives:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        rank = (i + j + 1) / 2
        rank_sum += rank * sum(y for _, y in pairs[i:j])
        i = j
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def metrics(ys, ps, threshold=0.5):
    n = len(ys)
    if not n:
        return {"n": 0}
    predicted = [p >= threshold for p in ps]
    positives = sum(ys)
    tpr = sum(1 for y, q in zip(ys, predicted) if y and q) / positives if positives else None
    tnr = sum(1 for y, q in zip(ys, predicted) if not y and not q) / (n - positives) if n - positives else None
    rates = [r for r in (tpr, tnr) if r is not None]
    clipped = [min(max(p, 1e-6), 1 - 1e-6) for p in ps]
    reliability = []
    for b in range(5):
        members = [(y, p) for y, p in zip(ys, ps) if b / 5 <= p < (b + 1) / 5 or b == 4 and p == 1.0]
        if members:
            reliability.append({"bin": f"{b / 5:.1f}-{(b + 1) / 5:.1f}", "n": len(members),
                                "mean_p": round(sum(p for _, p in members) / len(members), 4),
                                "rate": round(sum(y for y, _ in members) / len(members), 4)})
    return {"n": n, "positive_rate": round(positives / n, 4),
            "accuracy": round(sum(q == bool(y) for y, q in zip(ys, predicted)) / n, 4),
            "balanced_accuracy": round(sum(rates) / len(rates), 4) if rates else None,
            "auc": None if auc(ys, ps) is None else round(auc(ys, ps), 4),
            "log_loss": round(-sum(y * math.log(p) + (1 - y) * math.log(1 - p) for y, p in zip(ys, clipped)) / n, 4),
            "brier": round(sum((p - y) ** 2 for y, p in zip(ys, ps)) / n, 4), "reliability": reliability}


# ---- splits and evaluation -----------------------------------------------

def split(rows, how, cutoff=TIME_CUTOFF, tail=0.2):
    """(train, test): every row of a group follows its group's first attempt."""
    if how == "time":
        edge = ms(cutoff)
        return [r for r in rows if r["group_first_ts"] < edge], [r for r in rows if r["group_first_ts"] >= edge]
    firsts = sorted({(r["group_first_ts"], r["group"]) for r in rows})
    held = {group for _, group in firsts[len(firsts) - max(1, round(len(firsts) * tail)):]} if firsts else set()
    return [r for r in rows if r["group"] not in held], [r for r in rows if r["group"] in held]


def fit_models(train, label, feature_set, l2=5.0, boosting=True):
    names = feature_names(feature_set, label)
    encoder = fit_encoder(train, names, INTERACTIONS[feature_set])
    ys = [int(r["labels"][label]) for r in train]
    dim = len(encoder["columns"]) + len(encoder["interactions"])
    weights = fit_logistic([encode(r, encoder) for r in train], ys, dim, l2)
    width = len(encoder["columns"])
    gbm = fit_boosting([encode(r, encoder, False, False) for r in train], ys, width) if boosting else None
    return {"label": label, "feature_set": feature_set, "features": list(names), "encoder": encoder, "l2": l2,
            "weights": weights, "boosting": gbm, "positive_rate": sum(ys) / len(ys),
            "lanes": lane_posterior(train, label), "n_train": len(train)}


def predict(model, row, which="logistic"):
    if which == "logistic":
        return predict_logistic(model["weights"], encode(row, model["encoder"]))
    if which == "boosting":
        return predict_boosting(model["boosting"], encode(row, model["encoder"], False, False), len(model["encoder"]["columns"]))
    if which == "lane":
        return model["lanes"].get(row["features"]["lane"], 0.5)
    return model["positive_rate"]


def top_features(model, k=10):
    columns = model["encoder"]["columns"] + [f"{a}*{b}" for a, b in model["encoder"]["interactions"]]
    weights = sorted(((columns[i - 1], w) for i, w in enumerate(model["weights"]) if i), key=lambda c: -abs(c[1]))
    gains = sorted(((model["encoder"]["columns"][j], g) for j, g in (model["boosting"] or {"gains": {}})["gains"].items()),
                   key=lambda c: -c[1])
    return {"logistic": [[c, round(w, 4)] for c, w in weights[:k]], "boosting": [[c, round(g, 4)] for c, g in gains[:k]]}


def counterfactual_picks(model, test, costs):
    """(logged rewards, matched 1/propensity weights, matched rewards) for 'pick the highest P/cost candidate'."""
    logged, weights, rewards = [], [], []
    for row in test:
        routing = row.get("routing") or {}
        p = routing.get("propensity")
        if not p or not routing.get("candidates"):
            continue
        y = int(row["labels"][model["label"]])
        logged.append(y)
        best = None
        for candidate in routing["candidates"]:
            lane = lane_of(candidate["agent"], candidate["model"], candidate["effort"])
            probe = {**row, "features": {**row["features"], "agent": candidate["agent"], "model": candidate["model"],
                                         "effort": candidate["effort"] or "default", "lane": lane}}
            cost = costs.get(lane) or candidate.get("mean_cost_usd") or (candidate.get("cost_tier") or 0) + 1
            value = predict(model, probe) / max(cost, 1e-3)
            if best is None or value > best[0]:
                best = (value, candidate["key"])
        if best[1] == routing["chosen"]:
            weights.append(1 / p)
            rewards.append(y)
    return logged, weights, rewards


def snips(logged, weights, rewards):
    """Self-normalized IPS on rows with logged propensities; identifiable only with enough matched weight."""
    out = {"rows_with_propensity": len(logged), "matches": len(weights),
           "logged_rate": round(sum(logged) / len(logged), 4) if logged else None}
    if weights:
        out["snips_rate"] = round(sum(w * r for w, r in zip(weights, rewards)) / sum(weights), 4)
        out["ess"] = round(sum(weights) ** 2 / sum(w * w for w in weights), 2)
    out["identifiable"] = len(weights) >= 10 and out.get("ess", 0) >= 5
    return out


def lane_costs(rows):
    costs = defaultdict(list)
    for row in rows:
        if row["features"].get("cost_usd") is not None:
            costs[row["features"]["lane"]].append(row["features"]["cost_usd"])
    return {lane: sum(v) / len(v) for lane, v in costs.items()}


def folds(rows, how, cutoff=TIME_CUTOFF, k=5):
    """[(train, test)]. rolling: issues ordered by first attempt in k+1 blocks; block i+1 tests on blocks 0..i."""
    if how != "rolling":
        return [split(rows, how, cutoff)]
    firsts = [group for _, group in sorted({(r["group_first_ts"], r["group"]) for r in rows})]
    block = {group: i * (k + 1) // max(len(firsts), 1) for i, group in enumerate(firsts)}
    return [([r for r in rows if block[r["group"]] < i], [r for r in rows if block[r["group"]] == i]) for i in range(1, k + 1)]


def evaluate(rows, splits=("time", "tail", "rolling"), cutoff=TIME_CUTOFF, l2=5.0, boosting=True):
    """Held-out metrics per label, split and feature set; rolling pools the predictions of every fold."""
    results, kinds = [], ("majority", "lane", "logistic", *(("boosting",) if boosting else ()))
    for label in LABELS:
        known = [r for r in rows if r["labels"][label] is not None]
        for how in splits:
            for feature_set in FEATURE_SETS:
                ys, scores, picks, model, n_train = [], {which: [] for which in kinds}, ([], [], []), None, 0
                for train, test in folds(known, how, cutoff):
                    if len(train) < 10 or len({bool(r["labels"][label]) for r in train}) < 2 or not test:
                        continue
                    model = fit_models(train, label, feature_set, l2, boosting)
                    n_train = max(n_train, len(train))
                    ys += [int(r["labels"][label]) for r in test]
                    for which in kinds:
                        scores[which] += [predict(model, r, which) for r in test]
                    if feature_set == "routing":
                        for pooled, part in zip(picks, counterfactual_picks(model, test, lane_costs(train))):
                            pooled += part
                entry = {"label": label, "feature_set": feature_set, "split": how, "n_train": n_train, "n_test": len(ys)}
                if not model:
                    results.append({**entry, "status": "insufficient"})
                    continue
                scored = {which: metrics(ys, scores[which]) for which in kinds}
                positives = sum(ys)
                entry.update({"status": "few" if len(ys) < FEW or min(positives, len(ys) - positives) < 5 else "ok",
                              "models": scored, "top_features": top_features(model)})
                if boosting:
                    lr, gb = scored["logistic"], scored["boosting"]
                    entry["boosting_beats_logistic"] = gb["log_loss"] < lr["log_loss"] and (gb["auc"] or 0) > (lr["auc"] or 0)
                if feature_set == "routing":
                    entry["routing_counterfactual"] = snips(*picks)
                results.append(entry)
    return results


STOP_THRESHOLDS = (0.10, 0.15, 0.20, 0.30)


def stop_rule(rows, splits=("rolling", "tail"), thresholds=STOP_THRESHOLDS, cutoff=TIME_CUTOFF, l2=5.0, boosting=True):
    """Offline round-stop rule: before round r of an issue, stop the issue when the kept·routing P(kept) < t.

    Held-out issues only. A skipped round's cost is its recorded cost, else its lane's mean training
    cost, else the mean training cost. A land is lost when a landed issue stops at or before its
    last kept round (its last round when none was kept); `at_risk_unknown` counts the same for
    issues whose landed label is unknown. The model is the logistic regression unless boosting
    beats it on held-out log loss and AUC.
    """
    report = []
    for how in splits:
        scored, ys = [], {"logistic": [], "boosting": []}
        for train, test in folds(rows, how, cutoff):
            if len(train) < 10 or len({r["labels"]["kept"] for r in train}) < 2 or not test:
                continue
            model = fit_models(train, "kept", "routing", l2, boosting)
            costs = lane_costs(train)
            spent = [r["features"]["cost_usd"] for r in train if r["features"].get("cost_usd") is not None]
            fallback = sum(spent) / len(spent) if spent else 0.0
            for row in test:
                cost, source = row["features"].get("cost_usd"), "actual"
                if cost is None:
                    cost, source = (costs[row["features"]["lane"]], "lane") if row["features"]["lane"] in costs else (fallback, "global")
                p = {which: predict(model, row, which) for which in ("logistic", *(("boosting",) if boosting else ()))}
                scored.append({"row": row, "p": p, "cost": cost, "cost_source": source})
                for which, value in p.items():
                    ys[which].append(value)
        if not scored:
            report.append({"split": how, "status": "insufficient"})
            continue
        truth = [int(s["row"]["labels"]["kept"]) for s in scored]
        quality = {which: metrics(truth, values) for which, values in ys.items() if values}
        which = "logistic"
        if "boosting" in quality and quality["boosting"]["log_loss"] < quality["logistic"]["log_loss"] \
                and (quality["boosting"]["auc"] or 0) > (quality["logistic"]["auc"] or 0):
            which = "boosting"
        groups = defaultdict(list)
        for s in scored:
            groups[s["row"]["group"]].append(s)
        kept_total = sum(truth)
        entry = {"split": how, "model": which, "rounds": len(scored), "issues": len(groups), "kept": kept_total,
                 "landed_issues": sum(g[0]["row"]["labels"]["landed"] is True for g in groups.values()),
                 "kept_auc": quality[which]["auc"]}
        for s in scored:
            s["p_stop"] = s["p"][which]
        entry["thresholds"] = [stop_outcome(groups, t, kept_total) for t in thresholds]
        report.append(entry)
    return report


def stop_outcome(groups, t, kept_total):
    """Replay the stop rule at threshold t over {group: [{row, p_stop, cost, cost_source}]}."""
    out, saved = Counter(), 0.0
    for items in groups.values():
        items = sorted(items, key=lambda s: (s["row"]["ts"], s["row"]["id"]))
        stop = next((i for i, s in enumerate(items) if s["p_stop"] < t), None)
        if stop is None:
            continue
        skipped, landed = items[stop:], items[0]["row"]["labels"]["landed"]
        kept = [i for i, s in enumerate(items) if s["row"]["labels"]["kept"]]
        out["issues_stopped"] += 1
        out["rounds_skipped"] += len(skipped)
        out["fallback_cost"] += sum(s["cost_source"] != "actual" for s in skipped)
        out["kept_lost"] += sum(bool(s["row"]["labels"]["kept"]) for s in skipped)
        saved += sum(s["cost"] for s in skipped)
        out["stopped_landed"] += landed is True
        out["stopped_unknown"] += landed is None
        if stop <= (kept[-1] if kept else len(items) - 1):
            out["lands_lost"] += landed is True
            out["at_risk_unknown"] += landed is None
    return {"t": t, **{k: out[k] for k in ("rounds_skipped", "fallback_cost", "kept_lost", "issues_stopped", "stopped_landed",
                                         "stopped_unknown", "lands_lost", "at_risk_unknown")},
            "usd_saved": round(saved, 2), "kept_lost_share": round(out["kept_lost"] / kept_total, 4) if kept_total else None}


def format_stop_rule(report):
    lines = [f"{'split':<8} {'model':<9} {'t':>5} {'skip':>5} {'$saved':>8} {'fallbk':>6} {'keptLost':>8} {'share':>6} "
             f"{'stopped':>7} {'stopLand':>8} {'landLost':>8} {'riskUnk':>7}"]
    for entry in report:
        if entry.get("status") == "insufficient":
            lines.append(f"{entry['split']:<8} insufficient")
            continue
        lines.append(f"{entry['split']:<8} {entry['rounds']} rounds, {entry['issues']} issues, {entry['kept']} kept, "
                     f"{entry['landed_issues']} landed; kept AUC {entry['kept_auc']}")
        for row in entry["thresholds"]:
            lines.append(f"{entry['split']:<8} {entry['model']:<9} {row['t']:>5.2f} {row['rounds_skipped']:>5} "
                         f"{row['usd_saved']:>8.2f} {row['fallback_cost']:>6} {row['kept_lost']:>8} "
                         f"{row['kept_lost_share'] if row['kept_lost_share'] is not None else '-':>6} {row['issues_stopped']:>7} "
                         f"{row['stopped_landed']:>8} {row['lands_lost']:>8} {row['at_risk_unknown']:>7}")
    return "\n".join(lines)


def format_results(results):
    lines = [f"{'label':<7} {'split':<5} {'features':<8} {'model':<9} {'n':>4} {'pos':>5} {'acc':>6} {'bacc':>6} "
             f"{'auc':>6} {'logloss':>7} {'brier':>6}"]
    for entry in results:
        head = f"{entry['label']:<7} {entry['split']:<5} {entry['feature_set']:<8}"
        if entry.get("status") == "insufficient":
            lines.append(f"{head} insufficient (train {entry['n_train']}, test {entry['n_test']})")
            continue
        for which, m in entry["models"].items():
            fmt = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
            lines.append(f"{head} {which:<9} {m['n']:>4} {fmt(m['positive_rate']):>5} {fmt(m['accuracy']):>6} "
                         f"{fmt(m['balanced_accuracy']):>6} {fmt(m['auc']):>6} {fmt(m['log_loss']):>7} {fmt(m['brier']):>6}"
                         + ("  (few)" if entry["status"] == "few" and which == "majority" else ""))
    return "\n".join(lines)


# ---- CLI -----------------------------------------------------------------

def default_dir(control_workspace):
    return Path(control_workspace) / ".fusion" / "outcome-model"


def add_parser(sub):
    parser = sub.add_parser("outcome-model", help="predict verify, kept and landed outcomes from lifecycle history; read-only")
    commands = parser.add_subparsers(dest="outcome_command", required=True)
    build = commands.add_parser("build-dataset", help="one row per dispatched attempt, with routing and verify-time features")
    build.add_argument("--tenet-repo", action="append", default=[], metavar="PATH",
                       help="a TENET repo whose build journal, verify records and issue PRs to read; repeatable")
    build.add_argument("--no-gh", action="store_true", help="do not list pull requests; landed stays unknown")
    build.add_argument("--out", help="dataset JSONL (default: .fusion/outcome-model/dataset.jsonl)")
    for name in ("evaluate", "train"):
        command = commands.add_parser(name, help={"evaluate": "held-out metrics against majority and per-lane posteriors",
                                                  "train": "fit one label and feature set on every row and save it"}[name])
        command.add_argument("--dataset", help="dataset JSONL (default: .fusion/outcome-model/dataset.jsonl)")
        command.add_argument("--l2", type=float, default=5.0, help="logistic regression L2 strength (default 5)")
        command.add_argument("--out", help="output JSON (default: .fusion/outcome-model/report.json or model.json)")
    evaluation = commands.choices["evaluate"]
    evaluation.add_argument("--split", choices=("time", "tail", "rolling", "all"), default="all",
                            help="time: issues first attempted before --cutoff train; tail: newest 20%% of issues test; "
                                 "rolling: five expanding-window folds over issues, pooled")
    evaluation.add_argument("--cutoff", default=TIME_CUTOFF)
    evaluation.add_argument("--no-boosting", action="store_true")
    evaluation.add_argument("--json", action="store_true", help="print the full report")
    evaluation.add_argument("--stop-rule", action="store_true",
                            help="instead: replay 'stop an issue before round r when P(kept) < t' on held-out issues "
                                 "(rolling and tail unless --split names one)")
    evaluation.add_argument("--thresholds", type=float, nargs="+", default=list(STOP_THRESHOLDS), metavar="T",
                            help="stop-rule thresholds (default 0.10 0.15 0.20 0.30)")
    training = commands.choices["train"]
    training.add_argument("--label", choices=LABELS, default="verify")
    training.add_argument("--features", choices=tuple(FEATURE_SETS), default="verify")


def command(args, control_workspace):
    directory = default_dir(control_workspace)
    if args.outcome_command == "build-dataset":
        rows, summary = build_dataset(control_workspace, args.tenet_repo, not args.no_gh)
        out = Path(args.out or directory / "dataset.jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        print(json.dumps({"dataset": str(out), **summary}, indent=2))
        return 0
    rows = read_jsonl(args.dataset or directory / "dataset.jsonl")
    if not rows:
        raise ValueError("empty dataset; run fusion outcome-model build-dataset first")
    if args.outcome_command == "train":
        known = [r for r in rows if r["labels"][args.label] is not None]
        model = fit_models(known, args.label, args.features, args.l2)
        out = Path(args.out or directory / "model.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(model) + "\n", encoding="utf-8")
        print(json.dumps({"model": str(out), "n_train": model["n_train"], "top_features": top_features(model)}, indent=2))
        return 0
    splits = ("time", "tail", "rolling") if args.split == "all" else (args.split,)
    if getattr(args, "stop_rule", False):
        report = stop_rule(rows, ("rolling", "tail") if args.split == "all" else splits, tuple(args.thresholds),
                           args.cutoff, args.l2, not args.no_boosting)
        out = Path(args.out or directory / "stop-rule.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2) if args.json else format_stop_rule(report) + f"\nreport: {out}")
        return 0
    results = evaluate(rows, splits, args.cutoff, args.l2, not args.no_boosting)
    out = Path(args.out or directory / "report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"coverage": coverage(rows), "results": results}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2) if args.json else format_results(results) + f"\nreport: {out}")
    return 0
