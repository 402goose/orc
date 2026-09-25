"""Training targets, example weights and a pure-Python reference of the Laya objective.

Stdlib only, so the default suite checks the math without the runtime.
fusion_laya.train computes the same quantities with torch.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

OBJECTIVES = ("soft_ce", "soft_ce+proper_scoring")
TRAINING_DEFAULTS = {
    "objective": "soft_ce+proper_scoring",
    "unfreeze_encoder": False,
    "encoder_learning_rate": 2.5e-5,
    "label_smoothing": 0.0,
    "class_balance": True,
    "max_class_weight": 4.0,
}
# Upstream's RLCD settings (NandhaKishorM/laya, typed-decisions fine-tuning
# notebook, training cell): 4 noisy samples per item, noise 0.4 -> 0.1 across
# epochs, spherical weight 0.75, ranked-probability weight 1.0, soft CE weight 1.0.
PROPER_SCORING = {"group_size": 4, "sigma_start": 0.4, "sigma_end": 0.1, "w_sph": 0.75, "w_rps": 1.0,
                  "log_floor": -9.21, "ce_weight": 1.0}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def training_options(value):
    """`decisions.training`, validated and completed with defaults."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("decisions.training must be an object")
    unknown = set(value) - set(TRAINING_DEFAULTS)
    if unknown:
        raise ValueError(f"decisions.training has unknown settings: {', '.join(sorted(unknown))}")
    options = {**TRAINING_DEFAULTS, **value}
    if options["objective"] not in OBJECTIVES:
        raise ValueError(f"decisions.training.objective must be one of {', '.join(OBJECTIVES)}")
    for key in ("unfreeze_encoder", "class_balance"):
        if not isinstance(options[key], bool):
            raise ValueError(f"decisions.training.{key} must be true or false")
    if not _number(options["encoder_learning_rate"]) or not 0 < options["encoder_learning_rate"] <= 1e-3:
        raise ValueError("decisions.training.encoder_learning_rate must be in (0, 0.001]")
    if not _number(options["label_smoothing"]) or not 0 <= options["label_smoothing"] < 1:
        raise ValueError("decisions.training.label_smoothing must be in [0, 1)")
    if not _number(options["max_class_weight"]) or not 1 <= options["max_class_weight"] <= 10:
        raise ValueError("decisions.training.max_class_weight must be in [1, 10]")
    return options


def target_distribution(row, key, labels, smoothing=0.0):
    """The training target for question `key`, in label order.

    A row may carry `targets: {key: {label: probability}}` -- for example the
    mean of several drafting samples -- which is used as given (normalized).
    Otherwise the reviewed hard label is one-hot, mixed with the uniform
    distribution by `smoothing`.
    """
    given = (row.get("targets") or {}).get(key)
    if given is not None:
        if not isinstance(given, dict) or set(given) != set(labels):
            raise ValueError(f"target distribution for {key} must give every label of the question")
        if any(not _number(v) or v < 0 for v in given.values()):
            raise ValueError(f"target distribution for {key} has an invalid probability")
        total = sum(given.values())
        if not 0.98 <= total <= 1.02:
            raise ValueError(f"target distribution for {key} does not sum to 1")
        return [given[label] / total for label in labels]
    hard = row["labels"][key]
    size = len(labels)
    return [(1 - smoothing) * (label == hard) + smoothing / size for label in labels]


def example_weight(row, key):
    """A row's own weight for question `key` (for example council agreement); 1 when absent."""
    value = (row.get("weights") or {}).get(key, 1.0)
    if not _number(value) or not 0 < value <= 100:
        raise ValueError(f"example weight for {key} must be in (0, 100]")
    return float(value)


def argmax(values):
    return max(range(len(values)), key=lambda index: (values[index], -index))


def class_weights(items, cap):
    """Per (question schema, class) weight from the training items' class counts.

    `items` are (schema, target) pairs; an item's class is its target's argmax.
    A class seen n times gets min(cap, n_majority / n): the majority class 1,
    a class four times rarer 4 (upstream reweights rare classes x3-4). A class
    never seen gets no weight -- there is nothing to reweight -- so an
    all-positive split trains unweighted.
    """
    counts = defaultdict(Counter)
    for schema, target in items:
        counts[schema][argmax(target)] += 1
    return {(schema, cls): min(float(cap), max(counter.values()) / n)
            for schema, counter in counts.items() for cls, n in counter.items()}


def item_weights(items, row_weights, cap, balance=True):
    """Final per-item weights: class weight x row weight, rescaled to mean 1, so
    the weighted loss keeps the unweighted loss's scale and learning rate."""
    per_class = class_weights(items, cap) if balance else {}
    raw = [per_class.get((schema, argmax(target)), 1.0) * weight
           for (schema, target), weight in zip(items, row_weights)]
    mean = sum(raw) / len(raw) if raw else 1.0
    return [value / mean for value in raw], per_class


def sigma(epoch, epochs, start=PROPER_SCORING["sigma_start"], end=PROPER_SCORING["sigma_end"]):
    """Exploration noise for `epoch` (0-based), linear from start to end as upstream."""
    return start + (end - start) * (epoch / max(1, epochs - 1))


def log_softmax(logits):
    top = max(logits)
    total = math.log(sum(math.exp(v - top) for v in logits))
    return [v - top - total for v in logits]


def soft_cross_entropy(logits, target):
    """-sum(target * log_softmax(logits)); equals hard cross-entropy for a one-hot target."""
    return -sum(t * lp for t, lp in zip(target, log_softmax(logits)))


def proper_reward(q, target, is_score, w_sph=PROPER_SCORING["w_sph"], w_rps=PROPER_SCORING["w_rps"],
                  log_floor=PROPER_SCORING["log_floor"]):
    """Reference of laya.common.proper_reward for one item: log score plus
    spherical score, minus the ranked probability score for `score` questions."""
    log_score = sum(t * max(math.log(max(p, 1e-12)), log_floor) for p, t in zip(q, target))
    norm = max(math.sqrt(sum(p * p for p in q)), 1e-9)
    reward = log_score + w_sph * sum(t * p for p, t in zip(q, target)) / norm
    if is_score:
        k = max(2, len(q))
        cq = ct = rps = 0.0
        for p, t in zip(q, target):
            cq, ct = cq + p, ct + t
            rps += (cq - ct) ** 2
        reward -= w_rps * rps / (k - 1)
    return reward
