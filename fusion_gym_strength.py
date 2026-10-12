"""Bradley-Terry lane strength from pairwise gym outcomes on the same task.

On each task (and mode), every pair of lanes with a graded result is one
comparison: a lane that succeeded beats a lane that failed; two successes or
two failures are a tie and are skipped. So a task every lane passes, or every
lane fails, says nothing, and a hard task costs every lane on it alike.

The fit is the MM iteration (Hunter 2004) with a small symmetric prior: each
lane plays one virtual win and one virtual loss against a reference lane of
fixed strength 1, so a lane that never lost stays finite. Strengths are
reported as log-scores normalised to mean 0, with the probability of beating
the median lane and percentile 95% intervals from a bootstrap over tasks
(tasks are resampled, not pairs; fixed seed). Reporting only: no routing
choice, prior or label reads it.
"""
from __future__ import annotations

import math
import random

RESAMPLES = 500
SEED = 57
# Virtual wins and losses per lane against the reference lane.
PRIOR = 1.0


def decisive_pairs(groups):
    """(winner, loser) for each pair of lanes graded in one group
    ({lane: succeeded}) whose results differ; ties are skipped."""
    passed = sorted(lane for lane, ok in groups.items() if ok)
    failed = sorted(lane for lane, ok in groups.items() if not ok)
    return [(winner, loser) for winner in passed for loser in failed]


def fit(pairs, lanes, iterations=10000, tolerance=1e-10):
    """{lane: log strength} relative to the reference lane (log 1 = 0).

    The reference is one more player: each lane beat it PRIOR times and lost
    to it PRIOR times. MM update: p_i <- W_i / sum_j n_ij / (p_i + p_j), with
    W_i the wins of lane i and n_ij the games between i and j, then every
    strength is divided by the reference's so it stays 1."""
    reference = object()
    wins = {lane: PRIOR for lane in lanes}
    wins[reference] = PRIOR * len(lanes)
    games = {lane: {reference: 2 * PRIOR} for lane in lanes}
    games[reference] = {lane: 2 * PRIOR for lane in lanes}
    for winner, loser in pairs:
        wins[winner] += 1
        games[winner][loser] = games[winner].get(loser, 0) + 1
        games[loser][winner] = games[loser].get(winner, 0) + 1
    strength = {player: 1.0 for player in games}
    for _ in range(iterations):
        updated = {player: wins[player] / sum(n / (p + strength[other]) for other, n in games[player].items())
                   for player, p in strength.items()}
        scale = updated[reference]
        updated = {player: p / scale for player, p in updated.items()}
        change = max(abs(math.log(updated[player] / strength[player])) for player in strength)
        strength = updated
        if change < tolerance:
            break
    return {lane: math.log(strength[lane]) for lane in lanes}


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def scores(pairs, lanes):
    """{lane: (log-score normalised to mean 0, probability of beating the median lane)}."""
    raw = fit(pairs, lanes)
    mean = sum(raw.values()) / len(raw)
    normalised = {lane: value - mean for lane, value in raw.items()}
    median = _median(normalised.values())
    return {lane: (value, 1 / (1 + math.exp(median - value))) for lane, value in normalised.items()}


def _quantile(ordered, q):
    """Linear interpolation between the closest ranks of a sorted list."""
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def strengths(tasks, resamples=RESAMPLES, seed=SEED):
    """Per lane, from {task: {group: {lane: succeeded}}} (a group is a mode
    on that task): strength (log-score, mean 0), p_beats_median, both with a
    bootstrap 95% interval over tasks, and the decisive pairs and tasks the
    lane was in. A lane with no decisive pair is listed with strength None
    ("no pairs"). `tasks` counts tasks with a decisive pair; `skipped_tasks`
    the rest (ties only, all pass, all fail, or a single lane)."""
    by_task, graded = {}, set()
    for task, groups in tasks.items():
        for results in groups.values():
            graded.update(results)
            found = decisive_pairs(results)
            if found:
                by_task.setdefault(task, []).extend(found)
    counts = {lane: {"pairs": 0, "tasks": set()} for lane in graded}
    for task, pairs in by_task.items():
        for pair in pairs:
            for lane in pair:
                counts[lane]["pairs"] += 1
                counts[lane]["tasks"].add(task)
    fitted = sorted(lane for lane, count in counts.items() if count["pairs"])
    lanes = {lane: {"strength": None, "strength_ci95": None, "p_beats_median": None, "p_beats_median_ci95": None,
                    "pairs": counts[lane]["pairs"], "tasks": len(counts[lane]["tasks"])} for lane in sorted(graded)}
    section = {"lanes": lanes, "pairs": sum(len(pairs) for pairs in by_task.values()), "tasks": len(by_task),
               "skipped_tasks": len(tasks) - len(by_task), "resamples": resamples, "seed": seed}
    if not fitted:
        return section
    order = sorted(by_task)
    point = scores([pair for task in order for pair in by_task[task]], fitted)
    rng = random.Random(seed)
    samples = {lane: ([], []) for lane in fitted}
    for _ in range(resamples):
        drawn = [rng.choice(order) for _ in order]
        for lane, (value, p) in scores([pair for task in drawn for pair in by_task[task]], fitted).items():
            samples[lane][0].append(value)
            samples[lane][1].append(p)
    for lane in fitted:
        values, probabilities = (sorted(sample) for sample in samples[lane])
        lanes[lane].update(strength=round(point[lane][0], 3), p_beats_median=round(point[lane][1], 3),
                           strength_ci95=[round(_quantile(values, q), 3) for q in (.025, .975)],
                           p_beats_median_ci95=[round(_quantile(probabilities, q), 3) for q in (.025, .975)])
    return section
