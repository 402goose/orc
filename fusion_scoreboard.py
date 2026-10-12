"""`fusion scoreboard`: acceptance per role and lane over every settled run, pinned or routed.

`routing-report` reads only automatically routed choices. Most work is pinned to
a lane, so its evidence never reached a lane comparison. This reads every
completed run with a settled verdict (the trajectory export's rules: no
unmeasured, withdrawn, quota, refusal or harness-failure runs), groups it by role
and lane family (agent, model, effort), and reports accepted out of settled with
a Wilson 95% interval, median cost and minutes, and how the runs got that lane:
routed automatically, pinned, or pinned but moved by a quota fallback. Reporting
only: no routing choice reads it.
"""
import argparse
from collections import defaultdict
import json
import statistics

import fusion_core as core
from fusion_policy import acceptance_ci95, format_rate


def family(result, task):
    """agent:model:effort, as routing-report's `--by family` names a lane.

    Runs from before effort was recorded on the result carry it only in the
    execution choice's request, which is what the harness was given."""
    agent = result.get("agent") or task.get("agent") or "-"
    model = result.get("model") or (task.get("resolved") or {}).get("model") or "default"
    resolved = task.get("resolved") or {}
    requested = (resolved.get("execution_choice") or {}).get("requested") or {}
    effort = result.get("reasoning_effort") or resolved.get("reasoning_effort") or requested.get("reasoning_effort")
    return f"{agent}:{model}" + (f":{effort}" if effort else "")


def how_chosen(task, routed):
    if task.get("pin_fallback") or task.get("quota_twin"):
        return "fallback"
    return "routed" if task.get("run_id") in routed else "pinned"


def scoreboard(workspace, since_days=None, role=None):
    """{"rows": [...], "excluded": {...}}: one row per (role, lane family) with settled runs."""
    from fusion_decisions import DecisionStore, normalize_role, read_jsonl
    from fusion_policy import effective_outcomes
    from fusion_trajectories import _read_json, run_ids, settle_reason
    events = read_jsonl(DecisionStore(workspace).path)
    outcomes = effective_outcomes(events)
    audit = {"withdrawn": {e.get("task_id") for e in events if e.get("event") == "outcome_withdraw"},
             "unmeasured": {e.get("task_id") for e in events if e.get("event") == "outcome_unmeasured"},
             "check_inputs_changed": {e.get("task_id") for e in events
                                      if e.get("event") in {"outcome", "outcome_excluded"} and e.get("check_inputs_changed")}}
    routed = {e.get("task_id") for e in events if e.get("event") == "routing_log" and e.get("scope") not in {"pin_fallback", "quota_twin"}}
    cutoff = core.now_ms() - since_days * 86400000 if since_days is not None else None
    wanted = normalize_role(role) if role else None
    groups, excluded = defaultdict(lambda: {"accepted": 0, "settled": 0, "costs": [], "minutes": [], "chosen": defaultdict(int)}), defaultdict(int)
    for run_id, run_dir in run_ids(workspace).items():
        result, task = _read_json(run_dir / "result.json"), _read_json(run_dir / "task.json")
        if result.get("status") == "abandoned":
            excluded["abandoned"] += 1
            continue
        if cutoff is not None and core.run_created_ms(run_dir) < cutoff:
            continue
        run_role = normalize_role(result.get("role") or task.get("role") or "unknown")
        if wanted and run_role != wanted:
            continue
        outcome = outcomes.get(run_id)
        if reason := settle_reason(run_id, result, outcome, audit):
            excluded[reason] += 1
            continue
        group = groups[(run_role, family(result, task))]
        group["settled"] += 1
        group["accepted"] += bool(outcome.get("accepted"))
        group["chosen"][how_chosen({**task, "run_id": run_id}, routed)] += 1
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        if isinstance(usage.get("cost_usd"), (int, float)):
            group["costs"].append(usage["cost_usd"])
        duration = _read_json(run_dir / "trace.json").get("duration_ms")
        if isinstance(duration, (int, float)) and duration > 0:
            group["minutes"].append(duration / 60000)
    rows = []
    for (run_role, lane), group in groups.items():
        rows.append({"role": run_role, "lane": lane, "settled": group["settled"], "accepted": group["accepted"],
                     "acceptance": round(group["accepted"] / group["settled"], 3),
                     "acceptance_ci95": acceptance_ci95(group["accepted"], group["settled"]),
                     "median_cost_usd": round(statistics.median(group["costs"]), 4) if group["costs"] else None,
                     "median_minutes": round(statistics.median(group["minutes"]), 1) if group["minutes"] else None,
                     "chosen": dict(sorted(group["chosen"].items()))})
    rows.sort(key=lambda row: (row["role"], -row["acceptance"], -row["settled"], row["lane"]))
    return {"schema": "fusion.scoreboard.v1", "since_days": since_days, "role": wanted, "rows": rows,
            "excluded": dict(sorted(excluded.items()))}


def render(value, min_settled=1):
    lines = ["role / lane: accepted/settled, rate [Wilson 95%], median cost and minutes, how chosen "
             "(routed automatically, pinned, or moved by a quota fallback). Lanes whose intervals overlap "
             "are not shown to differ."]
    role = None
    for row in value["rows"]:
        if row["settled"] < min_settled:
            continue
        if row["role"] != role:
            role = row["role"]
            lines.append(f"\n{role}")
        cost = f"${row['median_cost_usd']:.2f}" if row["median_cost_usd"] is not None else "-"
        minutes = f"{row['median_minutes']:.0f}m" if row["median_minutes"] is not None else "-"
        chosen = " ".join(f"{name}={count}" for name, count in row["chosen"].items())
        lines.append(f"  {row['lane']:<36} {row['accepted']:>4}/{row['settled']:<4} "
                     f"{format_rate(row['accepted'], row['settled']):<20} {cost:>7} {minutes:>5}  {chosen}")
    if value["excluded"]:
        lines.append("\nnot settled, left out: " + ", ".join(f"{name}={count}" for name, count in value["excluded"].items()))
    return "\n".join(lines)


def add_parser(sub):
    parser = sub.add_parser("scoreboard", help="acceptance per role and lane over every settled run, pinned or routed, "
                                               "with Wilson 95%% intervals; reporting only")
    parser.add_argument("--since", type=float, metavar="DAYS", help="only runs started in the last DAYS days")
    parser.add_argument("--role", help="only this role")
    parser.add_argument("--min-settled", type=int, default=1, help="hide lanes with fewer settled runs in the text view")


def run(args, workspace):
    value = scoreboard(workspace, args.since, args.role)
    print(json.dumps(value, indent=2) if args.json else render(value, args.min_settled))
    return 0
