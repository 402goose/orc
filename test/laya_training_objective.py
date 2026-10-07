"""Optional runtime check of the Laya training objective; never downloads a checkpoint.

Run with the managed Laya interpreter:

    ~/.local/share/orc/laya/bin/python test/laya_training_objective.py            # objective math
    ~/.local/share/orc/laya/bin/python test/laya_training_objective.py --train    # + a real CPU fine-tune
    ~/.local/share/orc/laya/bin/python test/laya_training_objective.py --overfit  # + the 32-row learnability check

Checks that fusion_laya.objective and laya.common.proper_reward agree with the
pure-Python reference in fusion_laya_objective, that the objective moves free
logits to a soft target, and that fusion_decisions.CHECKPOINTS matches every
checkpoint cached locally. --train fine-tunes the cached English checkpoint on
a small synthetic, balanced acceptance dataset (a few CPU minutes; add
--unfreeze-encoder to adapt the encoder too) and prints the loss curve and the
candidate's held-out accuracy. --overfit trains with the configured defaults
(decisions.training: max_epochs, patience, proper_scoring_weight) on 32
balanced synthetic training rows plus 8 held-out rows, seed 20261007, and
fails unless the fit rows reach 95% train accuracy; it prints the dev
balanced accuracy and the chosen epoch. Synthetic texts only; no production
decisions.
"""
import argparse
import json
import math
from pathlib import Path
import random
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import torch

from fusion_decisions import ACCEPTANCE_QUESTIONS, CHECKPOINTS, digest
import fusion_laya
from fusion_laya_objective import PROPER_SCORING, policy_gradient, proper_reward, soft_cross_entropy


def check_math(failures):
    from laya.common import QTYPES, proper_reward as upstream
    random.seed(7)
    generator = torch.Generator().manual_seed(7)
    sizes, qtypes = [2, 3, 4], [QTYPES["noul"], QTYPES["score"], QTYPES["choice"]]
    logits = torch.tensor([[random.gauss(0, 2) for _ in range(4)] for _ in sizes])
    mask = torch.tensor([[index < size for index in range(4)] for size in sizes])
    target = torch.zeros(3, 4)
    for row, size in enumerate(sizes):
        values = [random.random() for _ in range(size)]
        target[row, :size] = torch.tensor([v / sum(values) for v in values])
    qtype = torch.tensor(qtypes)
    weights = torch.tensor([0.5, 1.0, 1.5])
    loss, cross_entropy, _ = fusion_laya.objective(logits, mask, target, qtype, weights, 0.4, generator, proper=False)
    for row, size in enumerate(sizes):
        reference = soft_cross_entropy(logits[row, :size].tolist(), target[row, :size].tolist())
        if abs(float(cross_entropy[row]) - reference) > 1e-4:
            failures.append(f"soft cross-entropy row {row}: {float(cross_entropy[row])} != {reference}")
    expected = sum(float(w) * float(c) for w, c in zip(weights, cross_entropy)) / 3
    if abs(float(loss) - expected) > 1e-4:
        failures.append(f"weighted loss {float(loss)} != {expected}")
    q = torch.softmax(logits.masked_fill(~mask, -1e4), -1)
    rewards = upstream(q, target, qtype, mask, w_sph=PROPER_SCORING["w_sph"], w_rps=PROPER_SCORING["w_rps"])
    for row, size in enumerate(sizes):
        reference = proper_reward(q[row, :size].tolist(), target[row, :size].tolist(), qtypes[row] == QTYPES["score"])
        if abs(float(rewards[row]) - reference) > 1e-4:
            failures.append(f"proper reward row {row}: {float(rewards[row])} != {reference}")
    two = torch.tensor([[True, True, False, False]])
    gradients = []
    for proper in (True, False):
        values = torch.tensor([[0.3, -0.2, 0.0, 0.0]], requires_grad=True)
        loss, _, _ = fusion_laya.objective(values, two, torch.tensor([[0.0, 1.0, 0.0, 0.0]]), torch.tensor([QTYPES["noul"]]),
                                           torch.ones(1), 0.3, torch.Generator().manual_seed(11), proper, 0.1)
        loss.backward()
        gradients.append(values.grad[0, :2].tolist())
    noise = torch.randn((PROPER_SCORING["group_size"], 1, 4), generator=torch.Generator().manual_seed(11)) * 0.3 * two
    noise = (noise - noise.sum(-1, keepdim=True) / 2) * two
    reference = policy_gradient([0.3, -0.2], [0, 1], False, noise[:, 0, :2].tolist(), 0.3, 0.1)
    policy = [a - b for a, b in zip(*gradients)]
    if any(abs(a - b) > 1e-5 for a, b in zip(policy, reference)):
        failures.append(f"policy-term gradient {policy} != reference {reference}")
    free = torch.zeros(3, 4, requires_grad=True)
    optimizer = torch.optim.Adam([free], lr=0.05)
    for _ in range(400):
        optimizer.zero_grad()
        loss, _, _ = fusion_laya.objective(free, mask, target, qtype, torch.ones(3), 0.2, generator)
        loss.backward()
        optimizer.step()
    fitted = torch.softmax(free.detach().masked_fill(~mask, -1e4), -1)
    gap = float((fitted - target).abs().max())
    if gap > 0.05:
        failures.append(f"objective did not fit free logits to the soft target (max gap {gap:.3f})")
    return {"fit_gap": round(gap, 4), "policy_gradient": [round(v, 6) for v in policy]}


def check_checkpoints(failures):
    from huggingface_hub import snapshot_download
    from laya.router import DEFAULT_MODELS
    seen = {}
    for name, (repo, folder) in DEFAULT_MODELS.items():
        patterns = [f"{folder}/rl_agent_config.json"] if folder else ["rl_agent_config.json"]
        try:
            root = Path(snapshot_download(repo, allow_patterns=patterns, local_files_only=True))
            config = json.loads(((root / folder) if folder else root).joinpath("rl_agent_config.json").read_text())
        except Exception:
            seen[name] = "not cached"
            continue
        actual = {"encoder": config.get("encoder"), "max_len": config.get("max_len"), "head_max_len": config.get("head_max_len")}
        seen[name] = actual
        if actual != CHECKPOINTS.get(name):
            failures.append(f"CHECKPOINTS[{name!r}] is {CHECKPOINTS.get(name)}, the cached checkpoint has {actual}")
    return seen


POSITIVE = ["Added the CSV export endpoint, wired the download button, and the new export tests pass.",
            "Implemented pagination on the orders list; the list and API tests pass and the diff touches only orders.",
            "Fixed the null check in the invoice parser and added a regression test that failed before and passes now.",
            "Renamed the config flag as asked, updated every reader, and the full suite passes.",
            "Added rate limiting to the login route with a test for the 429 response; all tests pass.",
            "Migrated the settings table to the new column and verified the migration test passes.",
            "Wrote the requested README section on setup; the docs build passes.",
            "Implemented the retry with backoff in the webhook sender and covered it with unit tests that pass."]
NEGATIVE = ["Could not start: the repository failed to install, so no files were changed.",
            "Ran out of time before making any change; nothing was edited.",
            "Reformatted unrelated files instead; the requested endpoint was not added.",
            "The task was unclear, so I only read the code and made no changes.",
            "Tests fail to import after my edit and I reverted everything; no change remains.",
            "Started on a different feature; the requested fix is not implemented.",
            "Blocked on missing credentials; no work was done.",
            "Deleted the failing test rather than fixing the bug the task described."]
TASKS = ["Add CSV export to the reports page.", "Paginate the orders list.", "Fix the invoice parser crash on empty totals.",
         "Rename the legacy config flag.", "Rate-limit the login route.", "Add the settings column migration.",
         "Document local setup in the README.", "Retry failed webhook deliveries with backoff."]


def dataset(path):
    rows = []
    for index, task in enumerate(TASKS):
        for accepted, summaries in ((True, POSITIVE), (False, NEGATIVE)):
            state = json.dumps({"task": task, "summary": summaries[index], "changed": [], "tests": []}, sort_keys=True)
            group = f"synthetic-{index}-{accepted}"
            rows.append({"schema": "fusion.training.v1", "id": digest(group), "group": group,
                         "split": "validation" if index >= 6 else "train", "kind": "acceptance", "state": state,
                         "questions": ACCEPTANCE_QUESTIONS, "prediction": {}, "model_identity": None,
                         "labels": {"plausible": str(accepted).lower(), "failed_task": str(not accepted).lower()},
                         "schema_hash": digest(ACCEPTANCE_QUESTIONS)})
    Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows


PREFIXES = ["", "Worker report: ", "Handoff: "]


def overfit_dataset(path, seed=20261007):
    order = random.Random(seed)
    cases = [(index, accepted, variant) for index in range(len(TASKS)) for accepted in (True, False) for variant in range(2)]
    order.shuffle(cases)
    held = [(index, accepted, 2) for index in range(4) for accepted in (True, False)]
    rows = []
    for number, (index, accepted, variant) in enumerate(cases + held):
        summary = PREFIXES[variant] + (POSITIVE if accepted else NEGATIVE)[index]
        state = json.dumps({"task": TASKS[index], "summary": summary, "changed": [], "tests": []}, sort_keys=True)
        group = f"overfit-{number:02d}"
        rows.append({"schema": "fusion.training.v1", "id": digest(group), "group": group, "group_first_ms": number,
                     "split": "validation" if number >= len(cases) else "train", "kind": "acceptance", "state": state,
                     "questions": ACCEPTANCE_QUESTIONS, "prediction": {}, "model_identity": None,
                     "labels": {"plausible": str(accepted).lower(), "failed_task": str(not accepted).lower()},
                     "schema_hash": digest(ACCEPTANCE_QUESTIONS)})
    Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows


def overfit(failures):
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / "data.jsonl"
        rows = overfit_dataset(data)
        started = time.monotonic()
        report = fusion_laya.train(argparse.Namespace(
            dataset=str(data), output=str(Path(directory) / "candidate"), model_path="", checkpoint="english", device="cpu",
            seed=20261007, epochs=None, learning_rate=1e-4, kinds="", split="time"))
        selection = report["selection"]
        chosen = selection["dev_by_epoch"][selection["chosen_epoch"] - 1] if selection["dev_by_epoch"] else {}
        result = {"train_rows": sum(r["split"] == "train" for r in rows), "fit_examples": selection["fit_examples"],
                  "dev_examples": selection["dev_examples"], "dev_groups": selection["dev_group_count"],
                  "max_epochs": selection["max_epochs"], "epochs_run": selection["epochs_run"],
                  "chosen_epoch": selection["chosen_epoch"], "proper_scoring_weight": selection["proper_scoring_weight"],
                  "train_accuracy": report["train_accuracy"], "train_balanced_accuracy": report["train_balanced_accuracy"],
                  "dev_balanced_accuracy": chosen.get("balanced_accuracy"), "dev_accuracy": chosen.get("accuracy"),
                  "dev_by_epoch": [{k: round(v, 3) if isinstance(v, float) else v for k, v in e.items()}
                                   for e in selection["dev_by_epoch"]],
                  "cross_entropy_by_epoch": [round(v, 3) for v in report["cross_entropy_by_epoch"]],
                  "train_seconds": round(time.monotonic() - started)}
        if report["train_accuracy"] < 0.95:
            failures.append(f"train accuracy {report['train_accuracy']:.3f} on the 32-row overfit set is below 0.95")
        return result


def train(args):
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / "data.jsonl"
        rows = dataset(data)
        started = time.monotonic()
        report = fusion_laya.train(argparse.Namespace(
            dataset=str(data), output=str(Path(directory) / "candidate"), model_path="", checkpoint="english", device="cpu",
            seed=42, epochs=args.epochs, learning_rate=1e-4, kinds="", unfreeze_encoder=args.unfreeze_encoder or None))
        result = {"steps": report["steps"], "method": report["method"], "cross_entropy_by_epoch": report["cross_entropy_by_epoch"],
                  "mean_reward": report["mean_reward"], "loss_curve": [round(p["loss"], 3) for p in report["loss_curve"]],
                  "train_rows": sum(r["split"] == "train" for r in rows), "train_seconds": round(time.monotonic() - started)}
        if args.evaluate:
            evaluated = fusion_laya.evaluate(argparse.Namespace(dataset=str(data), output=str(Path(directory) / "eval.jsonl"),
                                                                model_path=report["path"], device="cpu", control=False))
            base = fusion_laya.evaluate(argparse.Namespace(dataset=str(data), output=str(Path(directory) / "base.jsonl"),
                                                           model_path="", device="cpu", control=False))
            result.update(candidate_accuracy=evaluated["accuracy"], base_accuracy=base["accuracy"],
                          validation_questions=evaluated["validation_questions"])
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--evaluate", action="store_true", help="with --train: score candidate and base on the held-out rows")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--unfreeze-encoder", action="store_true")
    parser.add_argument("--overfit", action="store_true", help="train the 32-row overfit set with the default settings")
    args = parser.parse_args()
    failures = []
    result = {"math": check_math(failures), "checkpoints": check_checkpoints(failures)}
    if args.train:
        result["train"] = train(args)
    if args.overfit:
        result["overfit"] = overfit(failures)
    print(json.dumps(result, indent=2))
    for failure in failures:
        print("FAIL:", failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
