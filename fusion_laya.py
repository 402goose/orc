"""Optional Laya worker. Heavy dependencies stay out of Fusion's interpreter."""
from __future__ import annotations

import argparse
from collections import OrderedDict, Counter, defaultdict
import contextlib
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time


def missing_checkpoint(name):
    return (f"the {name!r} Laya checkpoint is not cached locally; fetch it once with "
            f"`orc fusion decisions setup --checkpoint {name}` (downloads public files; inference and training stay offline)")


def checkpoint(name="english", online=False):
    from huggingface_hub import snapshot_download
    from laya.router import DEFAULT_MODELS
    if name not in DEFAULT_MODELS:
        raise ValueError(f"unknown Laya checkpoint {name!r}; choose one of {', '.join(DEFAULT_MODELS)} or a checkpoint directory")
    repo, folder = DEFAULT_MODELS[name]
    patterns = [f"{folder}/*"] if folder else ["rl_agent_config.json", "model.safetensors", "encoder/*", "tokenizer/*"]
    try:
        root = Path(snapshot_download(repo, allow_patterns=patterns, local_files_only=not online))
    except Exception as error:
        if online:
            raise
        raise ValueError(missing_checkpoint(name)) from error
    path = root / folder if folder else root
    if not online and not (path / "rl_agent_config.json").is_file():
        raise ValueError(missing_checkpoint(name))
    return path


def identity(path):
    result = hashlib.sha256()
    result.update(importlib.metadata.version("laya").encode())
    path = Path(path)
    files = [path / "rl_agent_config.json", path / "model.safetensors"]
    files += list((path / "encoder").rglob("*")) + list((path / "tokenizer").rglob("*"))
    for file in sorted(files):
        if file.is_file() and (file.suffix in {".json", ".safetensors", ".model", ".txt"}):
            result.update(str(file.relative_to(path)).encode())
            with file.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    result.update(chunk)
    return "laya-sha256:" + result.hexdigest()


def truncated(agent, state, questions):
    from laya.common import build_sequence, render_options
    tokens = len(agent.tok.encode(state.replace(agent.tok.mask_token, " "), add_special_tokens=False))
    max_len, head_len = agent.cfg.get("max_len", 512), agent.cfg.get("head_max_len", 192)
    for question in questions.values():
        q = agent._to_internal(question)
        empty, _ = build_sequence(agent.tok, "", q, max_len, head_len)
        options = [len(agent.tok.encode(" " + option.replace(agent.tok.mask_token, " "), add_special_tokens=False)) for option in render_options(q)]
        instruction = len(agent.tok.encode(f"{q['t']} question: {q['ins'].replace(agent.tok.mask_token, ' ')}", add_special_tokens=False))
        if tokens > max_len - len(empty) or any(size > 48 for size in options):
            return True
        option_budget = sum(size + 1 for size in options)
        if option_budget > head_len - 16 or instruction > max(8, head_len - option_budget):
            return True
    return False


class Backend:
    def __init__(self, online=False):
        import torch
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        self.online = online
        self.loaded = OrderedDict()
        self.router = None

    def load(self, name, device, model_path=""):
        import laya
        key = (model_path or name, device)
        if key not in self.loaded:
            # Evict before allocating the next encoder.
            if len(self.loaded) >= 2:
                self.loaded.popitem(last=False)
            path = Path(model_path).expanduser().resolve() if model_path else checkpoint(name, self.online)
            if model_path and not (path / "rl_agent_config.json").is_file():
                raise ValueError(f"{path} is not a Laya checkpoint directory (no rl_agent_config.json)")
            agent = laya.load(str(path), device=device)
            self.loaded[key] = (agent, identity(path), path)
        self.loaded.move_to_end(key)
        return self.loaded[key]

    def predict(self, request):
        from laya import Router
        if self.router is None:
            self.router = Router(auto_task_detection=False)
        state, questions = request["state"], request["questions"]
        if request.get("checkpoint") and not request.get("model_path"):
            route = {"model": request["checkpoint"], "reason": "configured for this decision kind"}
        else:
            route = dict(self.router.route(state, questions))
        agent, model_id, _ = self.load(route["model"], request.get("device", "cpu"), request.get("model_path", ""))
        result = agent.predict(state, questions)
        return {"answers": result["answers"], "model_identity": model_id,
                "routing": {**route, "custom": bool(request.get("model_path")), "device": str(agent.device)},
                "truncated": truncated(agent, state, questions)}


def dataset_rows(path):
    from fusion_decisions import digest, labels_for
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    groups, ids = {}, set()
    for row in rows:
        if row.get("schema") != "fusion.training.v1" or row.get("split") not in {"train", "validation"}:
            raise ValueError("expected an exported fusion.training.v1 dataset")
        group, split = row["group"], row["split"]
        if row["id"] in ids or group in groups and groups[group] != split:
            raise ValueError("duplicate example or train/validation group leakage")
        if row["schema_hash"] != digest(row["questions"]):
            raise ValueError("question schema hash mismatch")
        if not row.get("labels"):
            raise ValueError("dataset example has no reviewed labels")
        for key, label in row["labels"].items():
            if label not in labels_for(row["questions"][key]):
                raise ValueError("invalid reviewed label")
        groups[group], ids = split, ids | {row["id"]}
    if not rows:
        raise ValueError("dataset has no reviewed examples")
    return rows


def learning_progress(args, phase, **value):
    """A small atomic sidecar for UI polling, plus human-readable job logs."""
    destination = Path(str(args.output) + '.progress.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps({'phase':phase, 'time_ms':int(time.time()*1000), **value}))
    temporary.chmod(0o600)
    temporary.replace(destination)
    message = value.get('message') or f"{value.get('done', 0)} / {value.get('total', 0)}"
    print(f"laya {phase}: {message}", file=sys.stderr, flush=True)


CURVE_POINTS = 200


def emits_progress(step, total):
    """Should this step write a progress update?

    loss_curve downsamples to CURVE_POINTS, so a new point only appears every
    `stride` steps. Writing every step rebuilt the whole curve and rewrote the
    progress file each time for a display that could not show the difference.
    Short runs are unaffected: stride is 1 until there are more steps than
    points. The final step always emits so the curve ends where the run did.
    """
    if total <= 0:
        return True
    stride = max(1, (total + CURVE_POINTS - 1) // CURVE_POINTS)
    return step % stride == 0 or step >= total


def loss_curve(losses):
    stride = max(1, (len(losses) + 199) // 200)
    indices = sorted(set(range(0,len(losses),stride)) | ({len(losses)-1} if losses else set()))
    return [{'step':i+1, 'loss':losses[i]} for i in indices]


def evaluate(args):
    from fusion_decisions import distribution, digest
    from fusion_quality import baseline_comparison, dataset_quality, input_key
    rows = dataset_rows(args.dataset)
    validation = [row for row in rows if row["split"] == "validation"]
    total_work = len(rows) + (len(validation) if getattr(args, "control", False) and len(validation)>1 else 0)
    learning_progress(args, 'loading', done=0, total=total_work, message='Loading local checkpoint for evaluation')
    backend, output = Backend(), Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Predict both partitions: temperature fitting uses train; qualification uses validation.
    correct = total = 0
    results = []
    by_kind = defaultdict(lambda: {"correct": 0, "questions": 0})
    predictions, controls = {}, {}
    for row in rows:
        result = backend.predict({"state": row["state"], "questions": row["questions"],
                                  "model_path": args.model_path, "device": args.device})
        if result["truncated"]:
            raise ValueError("candidate truncates a reviewed example; shorten and re-review the input")
        prediction = {key: distribution(result["answers"][key], q) for key, q in row["questions"].items()}
        if row["split"] == "validation":
            for key, label in row["labels"].items():
                total += 1
                correct += max(prediction[key], key=prediction[key].get) == label
                metric = by_kind[row["kind"]]
                metric["questions"] += 1
                metric["correct"] += max(prediction[key], key=prediction[key].get) == label
        predictions[row["id"]] = prediction
        results.append({**row, "prediction": prediction, "model_identity": result["model_identity"]})
        learning_progress(args, "evaluation", done=len(results), total=total_work)
    # Control: score each held-out example against another example's state. A model
    # that scores the same here is answering from the question, not the state.
    control_correct = control_total = 0
    validation = [row for row in rows if row["split"] == "validation"]
    if getattr(args, "control", False) and len(validation) > 1:
        for index, row in enumerate(validation):
            foreign = validation[(index + 1) % len(validation)]["state"]
            result = backend.predict({"state": foreign, "questions": row["questions"],
                                      "model_path": args.model_path, "device": args.device})
            if result["truncated"]:
                raise ValueError("shuffled-state control truncates a reviewed example")
            prediction = {key: distribution(result["answers"][key], q) for key, q in row["questions"].items()}
            controls[row["id"]] = prediction
            for key, label in row["labels"].items():
                control_total += 1
                control_correct += max(prediction[key], key=prediction[key].get) == label
            learning_progress(args, "control", done=len(rows)+index+1, total=total_work)
    with output.open("x") as handle:
        for row in results:
            handle.write(json.dumps(row) + "\n")
    output.chmod(0o600)
    quality = dataset_quality(rows)
    ancestry = {}
    if args.model_path:
        metadata = Path(args.model_path) / 'training.json'
        if metadata.is_file():
            ancestry = json.loads(metadata.read_text())
    overlap_groups = set(ancestry.get('seen_train_groups', [])) & {digest(r['group']) for r in validation}
    overlap_inputs = set(ancestry.get('seen_train_inputs', [])) & {input_key(r) for r in validation}
    contaminated = bool(overlap_groups or overlap_inputs or quality['cross_split_duplicates'] or quality['group_overlap'])
    lineage_known = not args.model_path or ancestry.get('lineage_complete') is True
    holdout = {"status": "contaminated" if contaminated else "checked" if lineage_known else "unknown",
               "overlap_groups": len(overlap_groups), "overlap_inputs": len(overlap_inputs),
               "cross_split_duplicates": len(quality['cross_split_duplicates'])}
    benchmark = [{k: r[k] for k in ('id', 'group', 'state', 'questions', 'labels')} for r in sorted(validation, key=lambda r: r['id'])]
    comparison = baseline_comparison(rows, predictions, controls)
    baselines = comparison["baselines"]
    report = {"path": str(output), "validation_questions": total, "accuracy": correct / total if total else None,
              "correct": correct, "by_kind": {k: {**v, "accuracy": v['correct'] / v['questions']} for k, v in by_kind.items()},
              "majority_accuracy": baselines.get("majority", {}).get("accuracy"),
              "majority_questions": baselines.get("majority", {}).get("n", 0),
              "heuristic_accuracy": baselines.get("heuristic", {}).get("accuracy"),
              "heuristic_questions": baselines.get("heuristic", {}).get("n", 0),
              "baselines": baselines, "by_question": comparison["by_question"],
              "control_accuracy": control_correct / control_total if control_total else None,
              "control_questions": control_total,
              "validation_groups": len({row['group'] for row in validation}),
              "dataset_hash": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
              "benchmark_hash": digest(benchmark), "data_quality": quality, "holdout": holdout,
              "model_path": args.model_path, "model_identities": sorted({r['model_identity'] for r in results})}
    output.with_suffix('.report.json').write_text(json.dumps(report, indent=2))
    learning_progress(args, 'complete', done=total_work, total=total_work)
    return report


def objective(logits, mask, target, qtype, weights, sigma, generator, proper=True):
    """Soft-target cross-entropy plus upstream's proper-scoring policy term.

    A port of the RLCD step in NandhaKishorM/laya's typed-decisions
    fine-tuning notebook (training cell), scoring with laya.common.proper_reward:
    sample Gaussian-noised logits (projected to zero mean over the options),
    reward each sample by log + spherical score (minus the ranked probability
    score on `score` questions) against the target distribution, subtract the
    group mean, and push the logits toward better samples; plus soft
    cross-entropy at weight 1.0. `weights` (mean 1 over the training set)
    scale each item's loss. Returns (loss, per-item cross-entropy, mean reward).
    """
    import torch
    from laya.common import proper_reward
    from fusion_laya_objective import PROPER_SCORING
    weights = weights.to(logits.dtype)
    masked = logits.masked_fill(~mask, -1e4)
    cross_entropy = -(target * torch.log_softmax(masked, -1)).sum(-1)
    loss = PROPER_SCORING["ce_weight"] * (weights * cross_entropy).mean()
    reward = torch.zeros((), device=logits.device)
    if proper:
        k = mask.sum(-1, keepdim=True).float()
        noise = torch.randn((PROPER_SCORING["group_size"],) + tuple(logits.shape), generator=generator,
                            device=generator.device).to(logits.device) * sigma * mask
        noise = (noise - noise.sum(-1, keepdim=True) / k) * mask
        z = logits.detach().unsqueeze(0) + noise
        q = torch.softmax(z.masked_fill(~mask, -1e4), -1)
        with torch.no_grad():
            reward = proper_reward(q, target.unsqueeze(0), qtype, mask, w_sph=PROPER_SCORING["w_sph"],
                                   w_rps=PROPER_SCORING["w_rps"], log_floor=PROPER_SCORING["log_floor"])
            advantage = reward - reward.mean(0, keepdim=True)
            advantage = advantage / (advantage.std() + 1e-6)
        log_prob = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (2 * sigma ** 2)
        loss = loss + (weights * -(advantage * log_prob)).mean()
        reward = reward.mean()
    return loss, cross_entropy.detach(), reward.detach()


def train(args):
    """Supervised decision-head adaptation; holdout groups never enter optimization."""
    learning_progress(args, 'loading', message='Loading local checkpoint for training')
    import random
    import torch
    from laya.common import QTYPES, build_sequence, collate_items
    from safetensors.torch import save_file
    from fusion_decisions import labels_for, digest
    from fusion_laya_objective import (PROPER_SCORING, example_weight, item_weights, sigma as noise_at,
                                       target_distribution, training_options)
    from fusion_quality import dataset_quality, input_key
    options = training_options({key: getattr(args, key) for key in ("objective", "unfreeze_encoder", "encoder_learning_rate",
                                                                      "label_smoothing", "class_balance", "max_class_weight")
                                if getattr(args, key, None) is not None})
    rows = dataset_rows(args.dataset)
    kinds = set(filter(None, (getattr(args, "kinds", "") or "").split(",")))
    if kinds:
        rows = [row for row in rows if row["kind"] in kinds]
    train_rows = [row for row in rows if row["split"] == "train"]
    if not train_rows or not any(row["split"] == "validation" for row in rows):
        raise ValueError("training requires both train and held-out validation groups"
                         + (f" of kind {', '.join(sorted(kinds))}" if kinds else ""))
    if not 1 <= args.epochs <= 20 or not 0 < args.learning_rate <= 0.01:
        raise ValueError("epochs must be 1..20 and learning rate in (0, .01]")
    output = Path(args.output)
    if output.exists():
        raise ValueError("candidate output already exists")
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    generator = torch.Generator().manual_seed(args.seed)
    source = getattr(args, "checkpoint", None) or "english"
    agent, source_id, source_path = Backend().load(source, args.device, args.model_path)
    max_len, head_len = agent.cfg.get("max_len", 512), agent.cfg.get("head_max_len", 192)
    # Build every example first: a truncated one stops training before any
    # update, and class weights need the whole training split.
    examples, flat, row_weights = [], [], []
    for row in train_rows:
        questions = {key: row["questions"][key] for key in row["labels"]}
        if truncated(agent, row["state"], questions):
            raise ValueError("training example is truncated; shorten and re-review it")
        items = []
        for key, q in questions.items():
            internal = agent._to_internal(q)
            ids, markers = build_sequence(agent.tok, row["state"], internal, max_len, head_len)
            target = target_distribution(row, key, labels_for(q), options["label_smoothing"])
            items.append({"ids": ids, "markers": markers, "qtype": QTYPES[internal["t"]], "target": target})
            flat.append((digest(q), target))
            row_weights.append(example_weight(row, key))
        examples.append(items)
    weights, per_class = item_weights(flat, row_weights, options["max_class_weight"], options["class_balance"])
    position = 0
    for items in examples:
        for item in items:
            item["weight"], position = weights[position], position + 1
    unfreeze = options["unfreeze_encoder"]
    head, encoder = [], []
    for name, parameter in agent.model.named_parameters():
        trainable = not name.startswith("act_head.") and (unfreeze or not name.startswith("encoder."))
        parameter.requires_grad_(trainable)
        if trainable:
            (encoder if name.startswith("encoder.") else head).append(parameter)
    groups = [{"params": head, "lr": args.learning_rate}]
    if encoder:
        groups.append({"params": encoder, "lr": options["encoder_learning_rate"]})
    optimizer = torch.optim.AdamW(groups, weight_decay=0.01)
    proper = options["objective"] == "soft_ce+proper_scoring"
    # `losses` is the soft cross-entropy, the curve the UI shows. The policy
    # term is a zero-mean surrogate, so the full objective can go negative
    # and is recorded only as `mean_objective`.
    steps, losses, objectives, rewards = 0, [], [], []
    total_steps = args.epochs * len(examples)
    for epoch in range(args.epochs):
        noise = noise_at(epoch, args.epochs)
        order = list(range(len(examples)))
        random.Random(args.seed + epoch).shuffle(order)
        for index in order:
            items = examples[index]
            collated = collate_items([items], agent.tok.pad_token_id)
            batch = {key: collated[key].to(agent.device) for key in ["input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype"]}
            agent.model.train()
            if not unfreeze:
                agent.model.encoder.eval()
            optimizer.zero_grad()
            logits, _ = agent.model(**batch, detach_encoder=not unfreeze)
            item_weight = torch.tensor([item["weight"] for item in items], device=agent.device)
            loss, cross_entropy, reward = objective(logits, batch["marker_mask"], collated["target"].to(agent.device),
                                                    batch["qtype"], item_weight, noise, generator, proper)
            loss.backward()
            torch.nn.utils.clip_grad_norm_([p for group in groups for p in group["params"]], 1.0)
            optimizer.step()
            objectives.append(float(loss.detach().cpu()))
            losses.append(float(cross_entropy.mean().cpu()))
            rewards.append(float(reward.cpu()))
            steps += 1
            # The curve is downsampled to 200 points, so a new point only
            # appears every `stride` steps. Emitting every step rewrote the
            # progress file and rebuilt the whole curve each time -- O(steps^2)
            # work, and a write+rename per step, for a display that could not
            # show the difference.
            if emits_progress(steps, total_steps):
                learning_progress(args, 'training', done=steps, total=total_steps,
                                  loss=losses[-1], loss_curve=loss_curve(losses))
    output.mkdir(parents=True)
    agent.model.encoder.config.save_pretrained(output / "encoder")
    agent.tok.save_pretrained(output / "tokenizer")
    cfg = {**agent.cfg, "temperature": [1, 1, 1], "temperature_by_options": {}}
    (output / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2))
    save_file({key: tensor.detach().cpu().contiguous() for key, tensor in agent.model.state_dict().items()}, str(output / "model.safetensors"))
    parent = {}
    if args.model_path and (Path(args.model_path) / 'training.json').is_file():
        parent = json.loads((Path(args.model_path) / 'training.json').read_text())
    per_epoch = len(examples)
    report = {"method": f"supervised {'decision head and encoder' if encoder else 'decision head'} fine-tuning ({options['objective']})",
              "source_identity": source_id, "model_identity": identity(output),
              "checkpoint": str(source_path) if args.model_path else source,
              "limits": {"max_len": max_len, "head_max_len": head_len}, "kinds": sorted(kinds) or None,
              "objective": {**options, "proper_scoring": PROPER_SCORING if proper else None,
                            "sigma_by_epoch": [noise_at(epoch, args.epochs) for epoch in range(args.epochs)] if proper else None,
                            "learning_rates": {"head": args.learning_rate, "encoder": options["encoder_learning_rate"] if encoder else None},
                            "weight_decay": 0.01, "gradient_clip": 1.0,
                            "class_weights": [{"question": schema, "class": cls, "weight": weight}
                                              for (schema, cls), weight in sorted(per_class.items())],
                            "item_weight_range": [min(weights), max(weights)]},
              "dataset_hash": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
              "seed": args.seed, "epochs": args.epochs, "steps": steps, "mean_loss": sum(losses) / steps, "loss_curve": loss_curve(losses),
              "loss": "soft cross-entropy", "mean_objective": sum(objectives) / steps,
              "cross_entropy_by_epoch": [sum(losses[e * per_epoch:(e + 1) * per_epoch]) / per_epoch for e in range(args.epochs)],
              "mean_reward": sum(rewards) / steps if proper else None,
              "data_quality": dataset_quality(train_rows),
              "seen_train_groups": sorted(set(parent.get('seen_train_groups', [])) | {digest(r['group']) for r in train_rows}),
              "seen_train_inputs": sorted(set(parent.get('seen_train_inputs', [])) | {input_key(r) for r in train_rows}),
              "lineage_complete": not args.model_path or parent.get('lineage_complete') is True,
              "train_groups": len({row["group"] for row in train_rows}), "promoted": False}
    (output / "training.json").write_text(json.dumps(report, indent=2))
    learning_progress(args, "complete", done=steps, total=steps, loss_curve=loss_curve(losses))
    return {**report, "path": str(output)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "warmup", "train", "evaluate"])
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--checkpoint", choices=["english", "multilingual", "typed-decisions"], default="english")
    parser.add_argument("--model-path", default="")
    parser.add_argument("--dataset")
    parser.add_argument("--output")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--learning-rate", type=float, default=0.0001)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--kinds", default="", help="train: only these decision kinds (comma-separated)")
    parser.add_argument("--objective", choices=["soft_ce", "soft_ce+proper_scoring"])
    parser.add_argument("--unfreeze-encoder", dest="unfreeze_encoder", action="store_true", default=None)
    parser.add_argument("--encoder-learning-rate", dest="encoder_learning_rate", type=float)
    parser.add_argument("--label-smoothing", dest="label_smoothing", type=float)
    parser.add_argument("--no-class-balance", dest="class_balance", action="store_false", default=None)
    parser.add_argument("--max-class-weight", dest="max_class_weight", type=float)
    parser.add_argument("--control", action="store_true", help="evaluate: also score held-out examples against a different example's state")
    args = parser.parse_args()
    if args.command != "warmup":
        os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if args.command == "serve":
        backend = Backend()
        for line in sys.stdin:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    result = backend.predict(json.loads(line))
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(result), flush=True)
        return 0
    with contextlib.redirect_stdout(sys.stderr):
        if args.command == "warmup":
            agent, model_id, path = Backend(online=True).load(args.checkpoint, args.device, args.model_path)
            result = {"model_identity": model_id, "path": str(path), "device": str(agent.device)}
        else:
            if not args.dataset or not args.output:
                parser.error("--dataset and --output are required")
            result = train(args) if args.command == "train" else evaluate(args)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        print(f"fusion laya: {error}", file=sys.stderr)
        raise SystemExit(1)
