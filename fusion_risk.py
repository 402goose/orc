"""Learn-then-Test thresholds for when a decision head may act. Stdlib only.

Given held-out (confidence, correct) pairs for one question bucket, find the
widest coverage at which the error rate among the decisions the head would
act on is at most `alpha`, with probability at least 1 - `delta` over the
draw of the held-out set (Angelopoulos, Bates, Candes, Jordan, Lei, "Learn
then Test", arXiv 2110.01052). The published threshold is the highest
certified one that still acts on those held-out answers.

Each candidate threshold t is a null hypothesis H_t: "the error rate of acted
decisions at t exceeds alpha". Its p-value is the exact binomial tail
P(Binomial(n_t, alpha) <= errors_t) over the n_t held-out answers at or above
t. The loss is 0/1, so this tail is exact; Hoeffding or Hoeffding-Bentkus
bounds are built for any bounded loss and are strictly looser for a binary
one, which at 30-100 held-out answers is the difference between qualifying
and not. Rejecting H_t at level delta is the same as the one-sided
Clopper-Pearson upper bound on the error rate at 1 - delta being at most
alpha; that bound is what the risk-coverage curve publishes.

Thresholds are tested in a fixed sequence from the most to the least
conservative, each at level delta, stopping at the first that is not
rejected (fixed-sequence testing controls the family-wise error at delta
without a multiplicity correction). The sequence is fixed before any
correctness is looked at: it keeps the grid thresholds whose acted count is
large enough that zero errors could reject at all, which depends only on the
held-out confidences. Validity assumes held-out and future decisions are
exchangeable; the time-split holdout is what tests that honestly.
"""
from __future__ import annotations

import math

GRID = tuple(round(1 - step / 100, 2) for step in range(51))


def binomial_cdf(k, n, p):
    """P(Binomial(n, p) <= k), summed in log space so large n stays finite."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    term = n * log_q
    terms = [term]
    for i in range(k):
        term += math.log((n - i) / (i + 1)) + log_p - log_q
        terms.append(term)
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(value - top) for value in terms))


def p_value(errors, n, alpha):
    """Evidence against "error rate > alpha" after `errors` in `n`; 1.0 when nothing was acted on."""
    return binomial_cdf(errors, n, alpha) if n else 1.0


def upper_bound(errors, n, delta):
    """One-sided Clopper-Pearson upper confidence bound on the error rate at level 1 - delta."""
    if not n:
        return 1.0
    if errors >= n:
        return 1.0
    low, high = errors / n, 1.0
    for _ in range(60):
        middle = (low + high) / 2
        if binomial_cdf(errors, n, middle) > delta:
            low = middle
        else:
            high = middle
    return high


def minimum_acted(alpha, delta):
    """The fewest acted answers with which zero errors can reject: (1 - alpha)^n <= delta."""
    return math.ceil(math.log(delta) / math.log1p(-alpha))


def curve(pairs, alpha, delta, grid=GRID):
    """Risk-coverage points for each grid threshold, most conservative first."""
    total = len(pairs)
    points = []
    for threshold in grid:
        acted = [correct for confidence, correct in pairs if confidence >= threshold]
        errors = len(acted) - sum(bool(c) for c in acted)
        n = len(acted)
        points.append({"threshold": threshold, "acted": n, "errors": errors,
                       "coverage": n / total if total else 0.0,
                       "risk": errors / n if n else None,
                       "upper_bound": upper_bound(errors, n, delta),
                       "p_value": p_value(errors, n, alpha)})
    return points


def learn_then_test(pairs, alpha=0.05, delta=0.1, min_examples=30, grid=GRID):
    """The lowest threshold whose acted error rate is certified at most alpha with probability 1 - delta.

    `pairs` is a sequence of (confidence, correct). Returns the threshold (None
    when none is certified or when there are fewer than `min_examples`
    held-out answers), the reason, and the full risk-coverage curve.
    """
    if not 0 < alpha < 1 or not 0 < delta < 1:
        raise ValueError("alpha and delta must be in (0, 1)")
    pairs = [(float(confidence), bool(correct)) for confidence, correct in pairs]
    points = curve(pairs, alpha, delta, grid)
    report = {"alpha": alpha, "delta": delta, "n": len(pairs), "min_examples": min_examples,
              "min_acted": minimum_acted(alpha, delta), "method": "learn-then-test/fixed-sequence/binomial",
              "threshold": None, "coverage": 0.0, "risk": None, "upper_bound": None, "curve": points}
    if len(pairs) < min_examples:
        return {**report, "reason": f"not qualified (n<{min_examples})"}
    sequence = [point for point in points if point["acted"] >= report["min_acted"]]
    if not sequence:
        return {**report, "reason": f"not qualified (fewer than {report['min_acted']} held-out answers at any threshold)"}
    rejected = []
    for point in sequence:
        if point["p_value"] > delta:
            break
        rejected.append(point)
    # Fixed-sequence testing certifies every rejected threshold at once. Of
    # those acting on the same held-out answers as the lowest, publish the
    # highest: same coverage, without acting on confidences never observed.
    chosen = next((point for point in rejected if point["acted"] == rejected[-1]["acted"]), None)
    if chosen is None:
        return {**report, "reason": f"not qualified (no threshold certifies error <= {alpha} at 1-delta={1 - delta:g})"}
    return {**report, "threshold": chosen["threshold"], "coverage": chosen["coverage"], "risk": chosen["risk"],
            "upper_bound": chosen["upper_bound"], "reason": "qualified"}
