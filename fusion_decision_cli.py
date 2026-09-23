"""User-facing setup, inspection, and reviewed learning lifecycle."""
import json
from pathlib import Path
import shutil
import subprocess

from fusion_decisions import (DecisionEngine, DecisionStore, ACCEPTANCE_QUESTIONS, INTAKE_QUESTIONS, RECOVERY_QUESTIONS,
                              REVIEW_QUESTIONS, fit_calibration, runtime_python)


def add_parser(sub):
    parser = sub.add_parser("decisions", help="local Laya setup, decisions and reviewed learning")
    commands = parser.add_subparsers(dest="decision_command", required=True)
    setup = commands.add_parser("setup", help="install optional runtime and download a checkpoint")
    setup.add_argument("--checkpoint", choices=["english", "multilingual", "typed-decisions"], default="english")
    setup.add_argument("--device", default="cpu")
    commands.add_parser("status", help="show mode, runtime and calibration status")
    listing = commands.add_parser("list", help="show recent decisions")
    listing.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show")
    show.add_argument("id")
    probe = commands.add_parser("probe", help="run a local classifier without starting coding agents")
    probe.add_argument("state")
    probe.add_argument("--kind", choices=["intake", "review", "recovery", "acceptance"], default="intake")
    label = commands.add_parser("label", help="record human-reviewed answers with verification evidence")
    label.add_argument("id")
    label.add_argument("answers", nargs="+", help="question=value pairs")
    label.add_argument("--evidence", required=True)
    suggest = commands.add_parser("suggest", help="draft evidence-backed labels using a worker; requires human approval")
    suggest.add_argument("id")
    suggest.add_argument("--agent", choices=["auto", "codex", "claude", "agy", "grok"], default="auto")
    export = commands.add_parser("export", help="export reviewed labels, split by workflow group")
    export.add_argument("output")
    calibrate = commands.add_parser("calibrate", help="fit temperature on train and assess held-out groups")
    calibrate.add_argument("dataset")
    calibrate.add_argument("output")
    calibrate.add_argument("--threshold", type=float, default=0.9)
    for command in ["train", "evaluate"]:
        action = commands.add_parser(command, help=f"{command} a local candidate without promoting it")
        action.add_argument("dataset")
        action.add_argument("output")
        action.add_argument("--model-path", default="")
        action.add_argument("--device", default="cpu")
        if command == "train":
            action.add_argument("--epochs", type=int, default=1)
            action.add_argument("--learning-rate", type=float, default=0.0001)
            action.add_argument("--seed", type=int, default=42)
        else:
            action.add_argument("--control", action="store_true", help="also score held-out examples against a different example's state")


def run(args, workspace, config):
    command = args.decision_command
    options = DecisionEngine(workspace, config).options
    store = DecisionStore(workspace)
    script = str(Path(__file__).with_name("fusion_laya.py"))
    if command == "setup":
        uv = shutil.which("uv")
        if not uv:
            raise ValueError("install uv, then rerun fusion decisions setup")
        root = Path.home() / ".local/share/orc/laya"
        python = str(root / "bin/python")
        if not Path(python).is_file():
            subprocess.run([uv, "venv", "--python", "3.12", str(root)], check=True)
        subprocess.run([uv, "pip", "install", "--python", python, "laya==0.3.4", "transformers>=4.45,<5"], check=True)
        return subprocess.run([python, script, "warmup", "--checkpoint", args.checkpoint, "--device", args.device], check=False).returncode
    if command in {"train", "evaluate"}:
        argv = [runtime_python(options), script, command, "--dataset", str(Path(args.dataset).resolve()),
                "--output", str(Path(args.output).resolve()), "--device", args.device,
                "--model-path", str(Path(args.model_path).expanduser().resolve()) if args.model_path else options["model_path"]]
        if command == "train":
            argv += ["--epochs", str(args.epochs), "--learning-rate", str(args.learning_rate), "--seed", str(args.seed)]
        elif getattr(args, "control", False):
            argv.append("--control")
        return subprocess.run(argv, check=False).returncode
    if command == "status":
        report = DecisionEngine(workspace, config).calibration()
        payload = {"mode": options["mode"], "auto_actions": options["auto_actions"], "python": runtime_python(options),
                   "device": options["device"], "model_path": options["model_path"], "log": str(store.path),
                   "decisions": len(store.records()), "calibration_identity": report.get("model_identity"),
                   "qualified_buckets": sum(bool(b.get("qualified")) for b in report.get("buckets", {}).values())}
    elif command == "list":
        payload = [{key: row.get(key) for key in ["id", "kind", "mode", "status", "recommendations", "duration_ms", "truncated"]}
                   for row in store.records()[-max(1, min(args.limit, 1000)):][::-1]]
    elif command == "show":
        payload = store.get(args.id)
    elif command == "probe":
        questions = {"intake": INTAKE_QUESTIONS, "review": REVIEW_QUESTIONS, "recovery": RECOVERY_QUESTIONS,
                     "acceptance": ACCEPTANCE_QUESTIONS}[args.kind]
        state = args.state
        # A JSON object probes with the same state shape a workflow sends; a plain string stays a string.
        if state.lstrip().startswith("{"):
            try:
                state = json.loads(state)
            except ValueError:
                pass
        payload = DecisionEngine(workspace, config).decide(args.kind, state, questions)
    elif command == "suggest":
        from fusion_labeling import suggest
        payload = suggest(workspace, config, args.id, args.agent)
    elif command == "label":
        if any("=" not in answer for answer in args.answers):
            raise ValueError("answers must be question=value pairs")
        store.label(args.id, dict(answer.split("=", 1) for answer in args.answers), args.evidence)
        payload = {"id": args.id, "labeled": True}
    elif command == "export":
        payload = store.export(args.output)
    else:
        payload = fit_calibration(args.dataset, args.output, args.threshold)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if isinstance(payload, dict) and payload.get("status") == "unavailable" else 0
