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


def checkpoint(name="english", online=False):
    from huggingface_hub import snapshot_download
    from laya.router import DEFAULT_MODELS
    repo, folder = DEFAULT_MODELS[name]
    patterns = [f"{folder}/*"] if folder else ["rl_agent_config.json", "model.safetensors", "encoder/*", "tokenizer/*"]
    root = Path(snapshot_download(repo, allow_patterns=patterns, local_files_only=not online))
    return root / folder if folder else root


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
            agent = laya.load(str(path), device=device)
            self.loaded[key] = (agent, identity(path), path)
        self.loaded.move_to_end(key)
        return self.loaded[key]

    def predict(self, request):
        from laya import Router
        if self.router is None:
            self.router = Router(auto_task_detection=False)
        state, questions = request["state"], request["questions"]
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


def evaluate(args):
    from fusion_decisions import distribution, digest
    from fusion_quality import dataset_quality, input_key
    rows = dataset_rows(args.dataset)
    backend, output = Backend(), Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Predict both partitions: temperature fitting uses train; qualification uses validation.
    correct = total = 0
    results = []
    by_kind = defaultdict(lambda: {"correct": 0, "questions": 0})
    train_labels = defaultdict(Counter)
    for row in rows:
        if row["split"] == "train":
            for key, label in row["labels"].items():
                train_labels[digest(row["questions"][key])][label] += 1
    majority_correct = majority_total = 0
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
                counts = train_labels[digest(row["questions"][key])]
                if counts:
                    majority_total += 1
                    majority_correct += sorted(counts, key=lambda v: (-counts[v], v))[0] == label
        results.append({**row, "prediction": prediction, "model_identity": result["model_identity"]})
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
            for key, label in row["labels"].items():
                control_total += 1
                control_correct += max(prediction[key], key=prediction[key].get) == label
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
    report = {"path": str(output), "validation_questions": total, "accuracy": correct / total if total else None,
              "correct": correct, "by_kind": {k: {**v, "accuracy": v['correct'] / v['questions']} for k, v in by_kind.items()},
              "majority_accuracy": majority_correct / majority_total if majority_total else None,
              "majority_questions": majority_total,
              "control_accuracy": control_correct / control_total if control_total else None,
              "control_questions": control_total,
              "validation_groups": len({row['group'] for row in validation}),
              "dataset_hash": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
              "benchmark_hash": digest(benchmark), "data_quality": quality, "holdout": holdout,
              "model_path": args.model_path, "model_identities": sorted({r['model_identity'] for r in results})}
    output.with_suffix('.report.json').write_text(json.dumps(report, indent=2))
    return report


def train(args):
    """Supervised decision-head adaptation; holdout groups never enter optimization."""
    import torch
    from laya.common import QTYPES, build_sequence, collate_items
    from safetensors.torch import save_file
    from fusion_decisions import labels_for, digest
    from fusion_quality import dataset_quality, input_key
    rows = dataset_rows(args.dataset)
    train_rows = [row for row in rows if row["split"] == "train"]
    if not train_rows or not any(row["split"] == "validation" for row in rows):
        raise ValueError("training requires both train and held-out validation groups")
    if not 1 <= args.epochs <= 20 or not 0 < args.learning_rate <= 0.01:
        raise ValueError("epochs must be 1..20 and learning rate in (0, .01]")
    output = Path(args.output)
    if output.exists():
        raise ValueError("candidate output already exists")
    torch.manual_seed(args.seed)
    agent, source_id, _ = Backend().load("english", args.device, args.model_path)
    for name, parameter in agent.model.named_parameters():
        parameter.requires_grad_(not name.startswith(("encoder.", "act_head.")))
    optimizer = torch.optim.AdamW([p for p in agent.model.parameters() if p.requires_grad], lr=args.learning_rate)
    steps, losses = 0, []
    for _ in range(args.epochs):
        for row in train_rows:
            questions = {key: row["questions"][key] for key in row["labels"]}
            if truncated(agent, row["state"], questions):
                raise ValueError("training example is truncated; shorten and re-review it")
            items, targets = [], []
            for key, q in questions.items():
                internal = agent._to_internal(q)
                ids, markers = build_sequence(agent.tok, row["state"], internal,
                                              agent.cfg.get("max_len", 512), agent.cfg.get("head_max_len", 192))
                items.append({"ids": ids, "markers": markers, "qtype": QTYPES[internal["t"]]})
                targets.append(labels_for(q).index(row["labels"][key]))
            collated = collate_items([items], agent.tok.pad_token_id)
            batch = {key: collated[key].to(agent.device) for key in ["input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype"]}
            agent.model.train()
            agent.model.encoder.eval()
            optimizer.zero_grad()
            logits, _ = agent.model(**batch, detach_encoder=True)
            loss = torch.nn.functional.cross_entropy(logits, torch.tensor(targets, device=agent.device))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(agent.model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
            steps += 1
    output.mkdir(parents=True)
    agent.model.encoder.config.save_pretrained(output / "encoder")
    agent.tok.save_pretrained(output / "tokenizer")
    cfg = {**agent.cfg, "temperature": [1, 1, 1], "temperature_by_options": {}}
    (output / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2))
    save_file({key: tensor.detach().cpu().contiguous() for key, tensor in agent.model.state_dict().items()}, str(output / "model.safetensors"))
    parent = {}
    if args.model_path and (Path(args.model_path) / 'training.json').is_file():
        parent = json.loads((Path(args.model_path) / 'training.json').read_text())
    report = {"method": "supervised decision-head fine-tuning", "source_identity": source_id,
              "model_identity": identity(output),
              "dataset_hash": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
              "seed": args.seed, "epochs": args.epochs, "steps": steps, "mean_loss": sum(losses) / steps,
              "data_quality": dataset_quality(train_rows),
              "seen_train_groups": sorted(set(parent.get('seen_train_groups', [])) | {digest(r['group']) for r in train_rows}),
              "seen_train_inputs": sorted(set(parent.get('seen_train_inputs', [])) | {input_key(r) for r in train_rows}),
              "lineage_complete": not args.model_path or parent.get('lineage_complete') is True,
              "train_groups": len({row["group"] for row in train_rows}), "promoted": False}
    (output / "training.json").write_text(json.dumps(report, indent=2))
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
