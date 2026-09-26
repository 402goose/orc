"""Measured dataset health and evaluation evidence; no model calls or inferred grades."""
from collections import Counter, defaultdict

from fusion_decisions import digest, labelable_record, labeled_splits, labels_for, record_group


def input_key(row):
    return digest({"state": row["state"], "questions": row["questions"]})


def dataset_quality(rows):
    inputs, distribution, contracts = defaultdict(list), defaultdict(Counter), {}
    approval_sources = Counter()
    groups = defaultdict(set)
    for row in rows:
        inputs[input_key(row)].append(row)
        groups[row["split"]].add(row["group"])
        for question, value in row["labels"].items():
            approval_sources[row.get("label_provenance", {}).get(question, {}).get("source", "unrecorded")] += 1
            key = f"{row['kind']} · {question} · {digest(row['questions'][question])[:8]}"
            distribution[key][value] += 1
            contracts[key] = labels_for(row["questions"][question])
    duplicates, conflicts, leakage = [], [], []
    for bucket in inputs.values():
        if len(bucket) < 2:
            continue
        ids = [row["id"] for row in bucket]
        duplicates.append(ids)
        values = defaultdict(set)
        for row in bucket:
            for key, value in row["labels"].items():
                values[key].add(value)
        if any(len(v) > 1 for v in values.values()):
            conflicts.append({"ids": ids, "questions": [k for k, v in values.items() if len(v) > 1]})
        if len({r["split"] for r in bucket}) > 1:
            leakage.append(ids)
    balance = [{"question": key, "counts": {label: counts[label] for label in contracts[key]},
                "answers": sum(counts.values()), "dominant_share": max(counts.values()) / sum(counts.values()),
                "missing_labels": [label for label in contracts[key] if not counts[label]]}
               for key, counts in sorted(distribution.items())]
    return {"examples": len(rows), "answers": sum(len(r["labels"]) for r in rows), "approval_sources": dict(approval_sources),
            "unique_inputs": len(inputs), "duplicate_examples": sum(len(ids) - 1 for ids in duplicates),
            "duplicate_sets": duplicates, "conflicts": conflicts, "cross_split_duplicates": leakage,
            "groups": {split: len(groups[split]) for split in ("train", "validation")},
            "group_overlap": sorted(groups["train"] & groups["validation"]), "balance": balance}


def review_quality(rows, split="time"):
    """Inspect current retained labels, with one effective review event per answer."""
    splits = labeled_splits(rows, split)
    examples, sources, evidence, edits, revisions = [], Counter(), 0, Counter(), 0
    council = Counter()
    for row in rows:
        if row.get("excluded") or not labelable_record(row):
            continue
        suggestion = (row.get("suggestions") or [None])[-1]
        if suggestion and suggestion.get("council"):
            council["drafts"] += 1
            for q in suggestion["council"].get("questions", {}).values():
                council[q["state"]] += 1
            council["failed_members"] += sum(m.get("status") not in {"success", "unavailable"} for m in suggestion["council"].get("members", []))
            council["unavailable_members"] += sum(m.get("status") == "unavailable" for m in suggestion["council"].get("members", []))
        effective, previous = {}, {}
        for event in row.get("labels", []):
            if not event.get("verified"):
                continue
            revisions += sum(k in previous and previous[k] != value for k, value in event["answers"].items())
            if event.get("replace"):
                effective, previous = {}, {}
            for key, value in event["answers"].items():
                effective[key], previous[key] = event, value
        answers = row.get("reviewed_answers", {})
        if not answers:
            continue
        group = record_group(row)
        examples.append({**row, "labels": answers, "group": group, "split": splits.get(group, "train")})
        for key in answers:
            event = effective.get(key, {})
            evidence += bool(str(event.get("evidence", "")).strip())
            source = ("council_auto" if event.get("source") == "council_approved_suggestion" else
                      event["source"] if event.get("source") in {"structural_gate", "gym_grade", "user_explicit"} else
                      event.get("suggested_by", {}).get("agent") or "human")
            sources[source] += 1
        # Count the latest retained review of each suggestion once, not per answer.
        seen = set()
        for key in answers:
            event = effective.get(key, {})
            sid = event.get("suggestion_id")
            if sid and sid not in seen and type(event.get("answers_edited")) is bool:
                seen.add(sid)
                edits["edited" if event["answers_edited"] else "unchanged"] += 1
    quality = dataset_quality(examples)
    return {**quality, "evidence_recorded": evidence, "sources": dict(sources),
            "draft_reviews": dict(edits), "revised_answers": revisions, "council": dict(council)}


BASELINES = ("majority", "heuristic", "control")


def _selected(probs):
    return max(probs, key=probs.get)


def baseline_comparison(rows, predictions, controls=None):
    """Held-out accuracy per question beside each baseline that applies to it.

    `predictions` maps a row id to its {question: probabilities}; `controls`
    maps a validation row id to the same questions answered against another
    example's state. Baselines, each compared on only the answers it covers:
    majority -- the most common training label for that exact question;
    heuristic -- the deterministic policy's answer exported with the row
      (fusion_decisions.heuristic_answers);
    control -- the candidate itself, reading a shuffled state.
    """
    train_labels = defaultdict(Counter)
    for row in rows:
        if row["split"] == "train":
            for key, label in row["labels"].items():
                train_labels[digest(row["questions"][key])][label] += 1
    empty = lambda: {"n": 0, "correct": 0, "baseline_correct": 0}
    questions = defaultdict(lambda: {"n": 0, "correct": 0, "groups": set(), "baselines": defaultdict(empty)})
    overall = defaultdict(empty)
    for row in rows:
        if row["split"] != "validation":
            continue
        for key, label in row["labels"].items():
            hit = _selected(predictions[row["id"]][key]) == label
            entry = questions[f"{row['kind']}:{key}"]
            entry["n"] += 1
            entry["correct"] += hit
            entry["groups"].add(row["group"])
            counts = train_labels[digest(row["questions"][key])]
            answers = {"majority": sorted(counts, key=lambda v: (-counts[v], v))[0] if counts else None,
                       "heuristic": (row.get("heuristic") or {}).get(key),
                       "control": _selected(controls[row["id"]][key]) if controls and row["id"] in controls else None}
            for name, answer in answers.items():
                if answer is None:
                    continue
                for target in (entry["baselines"][name], overall[name]):
                    target["n"] += 1
                    target["correct"] += hit
                    target["baseline_correct"] += answer == label

    def summary(value):
        return {"n": value["n"], "accuracy": value["baseline_correct"] / value["n"],
                "candidate_accuracy": value["correct"] / value["n"],
                "margin": (value["correct"] - value["baseline_correct"]) / value["n"]}
    return {"baselines": {name: summary(overall[name]) for name in BASELINES if overall[name]["n"]},
            "by_question": {name: {"n": value["n"], "groups": len(value["groups"]), "accuracy": value["correct"] / value["n"],
                                   "baselines": {b: summary(value["baselines"][b]) for b in BASELINES if value["baselines"][b]["n"]}}
                            for name, value in sorted(questions.items())}}


def unbeaten(baselines, margin):
    """Baselines the candidate does not beat by more than `margin` on the answers each covers."""
    return [name for name, value in (baselines or {}).items() if not value["margin"] > margin]


def matched_comparisons(candidates, evaluations):
    """Pair exact model identities on the same held-out benchmark; keep gaps visible."""
    comparisons = []
    for candidate in candidates:
        training = candidate.get("training", {})
        identity, source = training.get("model_identity"), training.get("source_identity")
        for evaluation in evaluations:
            result = evaluation["result"]
            if not identity or result.get("model_identities") != [identity]:
                continue
            baseline = next((e for e in evaluations if source and e["result"].get("model_identities") == [source]
                             and ((result.get("benchmark_hash") and e["result"].get("benchmark_hash") == result["benchmark_hash"])
                                  or (result.get("dataset_hash") and e["result"].get("dataset_hash") == result["dataset_hash"]))), None)
            before = baseline["result"] if baseline else {}
            reasons = []
            if not baseline:
                reasons.append("Evaluate the source model on the same held-out benchmark.")
            if any(r.get("holdout", {}).get("status") == "contaminated" for r in (result, before)):
                reasons.append("Training and validation overlap; this comparison cannot establish generalization.")
            measured = (isinstance(result.get("accuracy"), (int, float)) and isinstance(before.get("accuracy"), (int, float)))
            comparisons.append({"candidate_id": candidate["id"], "evaluation_id": evaluation["id"],
                                "time_ms": evaluation.get("started_at_ms"), "baseline_id": baseline["id"] if baseline else None,
                                "accuracy": result.get("accuracy"), "baseline_accuracy": before.get("accuracy"),
                                "delta": result["accuracy"] - before["accuracy"] if measured and not reasons else None,
                                "validation_questions": result.get("validation_questions"),
                                "validation_groups": result.get("validation_groups"),
                                "benchmark": result.get("benchmark_hash") or result.get("dataset_hash"),
                                "train_answers": training.get("data_quality", {}).get("answers"),
                                "by_kind": result.get("by_kind", {}), "baseline_by_kind": before.get("by_kind", {}),
                                "control_accuracy": result.get("control_accuracy"),
                                "majority_accuracy": result.get("majority_accuracy"),
                                "heuristic_accuracy": result.get("heuristic_accuracy"),
                                "baselines": result.get("baselines"), "by_question": result.get("by_question", {}),
                                "baseline_by_question": before.get("by_question", {}),
                                "holdout_status": result.get("holdout", {}).get("status", "unknown"), "notes": reasons})
    return sorted(comparisons, key=lambda r: r.get("time_ms") or 0, reverse=True)
