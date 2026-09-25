"""Objective acceptance evidence: executed plan verification, gate labels,
outcomes free of Laya's own veto, and explicit intake intent (#91-#93, #95)."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_decisions
from fusion_build import prepare
from fusion_decisions import ACCEPTANCE_QUESTIONS, DecisionStore, digest, label_provenance, read_jsonl, reviewed_labels
from fusion_labeling import gate_answers
from fusion_policy import outcome_counts, route_candidates
from fusion_verification import counts_as_failure, plan_checks, to_argv
from fusion_workflow import WorkflowRunner, parse_acceptance_contract
from decisions_test import Backend

WORKER = '''import json, pathlib, sys
prompt = sys.stdin.read()
spec = json.loads(pathlib.Path(SPEC).read_text())
if "node plan" in prompt:
    text = ("STATUS: success\\nSUMMARY: planned\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none\\n\\n"
            "```acceptance-contract\\n" + json.dumps(spec["contract"]) + "\\n```")
else:
    writes = spec.get("write", {})
    if "sequence" in spec:
        counter = pathlib.Path(SPEC + ".calls")
        calls = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(calls + 1))
        writes = spec["sequence"][min(calls, len(spec["sequence"]) - 1)]
    for name, body in writes.items():
        pathlib.Path(name).write_text(body)
    text = ("STATUS: success\\nSUMMARY: implemented the plan\\nCHANGED: hello.py\\nTESTS: ran the check\\nBLOCKERS: "
            + spec.get("blockers", "none"))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''
HELLO_EXISTS = "import pathlib, sys\nsys.exit(0 if pathlib.Path('hello.py').exists() else 1)\n"
HELLO_ABSENT = "import pathlib, sys\nsys.exit(1 if pathlib.Path('hello.py').exists() else 0)\n"


class ImplementsOnly(Backend):
    """Vetoes the implementation stage only, so the plan still runs."""
    def predict(self, state, questions):
        response = super().predict(state, questions)
        if "plausible" in questions:
            response["answers"]["plausible"] = {"noul": .01 if "Implement it" in state else .99}
        return response


class VerificationPolicyTest(unittest.TestCase):
    def test_only_shell_free_allowlisted_runners_become_argv_checks(self):
        checks, rejected = plan_checks([
            ["python3", "-m", "pytest", "tests/test_one.py"],
            "pytest -q tests/test_two.py -k 'csv and not slow'",
            "go test ./...",
            "pytest tests/ | tee out.log",
            "cd web && npm test",
            "FOO=1 pytest",
            "pytest tests/*.py",
            ["./scripts/pytest"],
            ["rm", "-rf", "build"],
            ["npm", "install"],
            "npm ci",
            ["python3", "-c", "print(1)"],
            ["python3", "-m", "pip", "install", "x"],
            ["node", "-e", "1"],
            ["uv", "run", "--with", "requests", "pytest"],
            ["uv", "pip", "install", "x"],
            ["make", "install"],
            ["cargo", "test"],
            ["npm", "test"],
            ["python3", "-m", "pytest", "tests/test_one.py"],
        ], {})
        self.assertEqual(checks, [["python3", "-m", "pytest", "tests/test_one.py"],
                                  ["pytest", "-q", "tests/test_two.py", "-k", "csv and not slow"],
                                  ["go", "test", "./..."], ["cargo", "test"], ["npm", "test"]])
        reasons = {json.dumps(item["command"]): item["reason"] for item in rejected}
        self.assertIn("shell syntax", reasons[json.dumps("pytest tests/ | tee out.log")])
        self.assertIn("not a path", reasons[json.dumps(["./scripts/pytest"])])
        self.assertIn("not an allowed test runner", reasons[json.dumps(["rm", "-rf", "build"])])
        self.assertIn("inline code", reasons[json.dumps(["python3", "-c", "print(1)"])])
        self.assertIn("not an allowed subcommand", reasons[json.dumps(["npm", "install"])])
        self.assertIn("installs", reasons[json.dumps(["make", "install"])])
        self.assertIn("environment assignments", reasons[json.dumps("FOO=1 pytest")])
        self.assertEqual(len(rejected), 14)

    def test_allowlist_and_execution_are_configurable(self):
        self.assertEqual(plan_checks([["tox", "-e", "py"]], {"verification": {"runners": ["tox"]}})[0], [["tox", "-e", "py"]])
        self.assertEqual(plan_checks([["pytest"]], {"verification": {"runners": ["tox"]}})[0], [])
        checks, rejected = plan_checks([["pytest"]], {"verification": {"execute": False}})
        self.assertEqual((checks, rejected[0]["reason"]), ([], "verification.execute is false"))
        with self.assertRaises(ValueError):
            plan_checks([["pytest"]], {"verification": {"runners": ["/usr/bin/pytest"]}})
        self.assertEqual(to_argv(["pytest", "bad\x00"])[0], None)

    def test_pytest_usage_errors_are_not_test_failures(self):
        self.assertTrue(counts_as_failure(["pytest", "x"], 1))
        for code in (2, 3, 4, 5):
            self.assertFalse(counts_as_failure(["python3", "-m", "pytest"], code))
        self.assertTrue(counts_as_failure(["cargo", "test"], 101))
        self.assertFalse(counts_as_failure(["cargo", "test"], 0))

    def test_contract_keeps_argv_arrays_and_strings(self):
        contract = parse_acceptance_contract('```acceptance-contract\n{"verification": [["pytest", "-q"], "make test", 3, []]}\n```')
        self.assertEqual(contract["verification"], [["pytest", "-q"], "make test"])


class GateAnswersTest(unittest.TestCase):
    def test_only_objective_codes_label(self):
        failed = {"code": "check_failed", "vacuous": False, "test_failure": True}
        self.assertEqual(gate_answers([failed], [])[0], {"failed_task": "true"})
        self.assertEqual(gate_answers([{**failed, "vacuous": None}], [])[0], {"failed_task": "true"})
        self.assertIsNone(gate_answers([{**failed, "vacuous": True}], [])[0], "a vacuous check never labels negative")
        self.assertIsNone(gate_answers([{**failed, "test_failure": False}], [])[0], "a runner error is not a test failure")
        self.assertEqual(gate_answers([{"code": "write_no_change"}], [])[0], {"failed_task": "true"})
        for code in ("worker_blockers", "required_handoff_empty", "check_error", "required_file_missing"):
            self.assertIsNone(gate_answers([{"code": code}], [{"status": "passed", "vacuous": False}])[0], code)
        self.assertEqual(gate_answers([], [{"status": "passed", "vacuous": False}])[0], {"failed_task": "false"})
        self.assertIsNone(gate_answers([], [{"status": "passed", "vacuous": True}])[0])
        self.assertIsNone(gate_answers([], [{"status": "passed", "vacuous": None}])[0])
        self.assertIsNone(gate_answers([], [])[0])

    def test_outcomes_resting_on_a_veto_or_the_report_are_not_evidence(self):
        self.assertTrue(outcome_counts(True, "success"))
        self.assertTrue(outcome_counts(False, "error", [{"code": "worker_status"}]))
        self.assertTrue(outcome_counts(False, "success", [{"code": "check_failed"}, {"code": "worker_blockers"}]))
        self.assertFalse(outcome_counts(False, "success", [], laya_veto=True))
        self.assertFalse(outcome_counts(False, "success", ["worker_blockers", "repeated_failure"]))
        self.assertFalse(outcome_counts(False, "success", [{"code": "required_handoff_empty"}]))


class HonestLabelsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("FUSION_DECISIONS_MODE", "FUSION_CONFIG", "FUSION_READ_ONLY"):
            os.environ.pop(name, None)
        self.backend = Backend()
        runtime = patch.object(fusion_decisions, "runtime_for", lambda options: self.backend)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.spec_path = self.root / "worker-spec.json"
        worker = self.root / "fake-codex"
        worker.write_text("#!/usr/bin/env python3\n" + f"SPEC = {str(self.spec_path)!r}\n" + WORKER)
        worker.chmod(0o755)
        self.config = {"codex": {"command": str(worker)}, "claude": {"command": "missing-claude"},
                       "agy": {"command": "missing-agy"}, "grok": {"command": "missing-grok"},
                       "timeout_seconds": 30, "decisions": {"mode": "shadow"}}
        (self.workspace / "hello_exists.py").write_text(HELLO_EXISTS)
        (self.workspace / "hello_absent.py").write_text(HELLO_ABSENT)
        (self.workspace / ".gitignore").write_text(".fusion/\n.fusion.json\n")
        self.write_config()
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"], cwd=self.workspace, check=True)
        self.store = DecisionStore(self.workspace)

    def write_config(self):
        (self.workspace / ".fusion.json").write_text(json.dumps(self.config))

    def run_build(self, contract, write=None, blockers="none", plan_verification=True, sequence=None, attempts=1):
        self.spec_path.write_text(json.dumps({"contract": contract, "write": write or {}, "blockers": blockers,
                                              **({"sequence": sequence} if sequence else {})}))
        acceptance = {"required_handoff": ["summary"], **({"plan_verification": True} if plan_verification else {})}
        spec = {"task": "build", "max_attempts": attempts, "nodes": [
            {"id": "plan", "role": "planning", "agent": "codex", "write": False, "task": "Plan it"},
            {"id": "implement", "role": "implementation", "agent": "codex", "write": True, "needs": ["plan"],
             "task": "Implement it", "acceptance": acceptance}]}
        outcome = WorkflowRunner(self.workspace, self.config, spec).run()
        return outcome, {node["id"]: node for node in outcome["nodes"]}

    def events(self, name, run_id=None):
        return [e for e in read_jsonl(self.store.path) if e.get("event") == name
                and (run_id is None or e.get("task_id", e.get("context", {}).get("task_id")) == run_id)]

    def gate_labels(self):
        return [e for e in self.events("label") if e.get("source") == "structural_gate"]

    def test_a_check_that_fails_before_and_passes_after_labels_the_task_done(self):
        outcome, nodes = self.run_build({"verification": [["python3", "hello_exists.py"], "npm install", "pytest | tee x"]},
                                        write={"hello.py": "print(1)\n"})
        self.assertEqual(outcome["status"], "success", outcome)
        result = nodes["implement"]["result"]
        [check] = result["acceptance_checks"]
        self.assertEqual((check["origin"], check["phase"] if "phase" in check else "after", check["status"], check["vacuous"]),
                         ("plan", "after", "passed", False))
        self.assertEqual(check["before"]["status"], "failed")
        self.assertIn("PYTHONDONTWRITEBYTECODE", check["env_overrides"])
        before = json.loads(Path(check["before"]["receipt"]).read_text())
        self.assertEqual((before["phase"], before["exit_code"]), ("before", 1))
        self.assertEqual([item["command"] for item in result["verification_rejected"]], ["npm install", "pytest | tee x"])
        self.assertEqual(result["gate_label"]["answers"], {"failed_task": "false"})
        [label] = self.gate_labels()
        decision = next(e for e in self.events("decision") if e["id"] == label["id"])
        self.assertEqual((decision["kind"], decision["status"], decision["source"], decision["context"]["task_id"]),
                         ("acceptance", "unscored", "structural_gate", result["run_id"]))
        self.assertIn("hello_exists.py", label["evidence"])
        scored = [e for e in self.events("decision", result["run_id"]) if e["status"] == "ok"]
        self.assertEqual(json.loads(scored[0]["state"]), json.loads(decision["state"]), "the gate and Laya see one input")
        [counted] = self.events("outcome", result["run_id"])
        self.assertEqual((counted["accepted"], counted["laya_veto"], counted["gate_codes"]), (True, False, []))
        # The plan node reported success too, but ran no check: recorded, unlabeled.
        plan_run = nodes["plan"]["result"]["run_id"]
        self.assertEqual(nodes["plan"]["result"]["gate_label"]["status"], "unlabeled")
        self.assertEqual([e["status"] for e in self.events("decision", plan_run) if e["kind"] == "acceptance"], ["unscored", "ok"])

    def test_a_check_still_failing_after_the_change_labels_the_task_failed(self):
        outcome, nodes = self.run_build({"verification": [["python3", "hello_exists.py"]]}, write={"other.py": "x = 1\n"})
        self.assertEqual(outcome["status"], "failed")
        result = nodes["implement"]["result"]
        self.assertIn("acceptance check failed: python3 hello_exists.py", result["blockers"])
        self.assertEqual([code["code"] for code in result["gate_codes"]], ["check_failed"])
        self.assertEqual(result["gate_label"]["answers"], {"failed_task": "true"})
        [outcome_event] = self.events("outcome", result["run_id"])
        self.assertEqual((outcome_event["accepted"], outcome_event["gate_codes"]), (False, ["check_failed"]))

    def test_a_check_that_already_passed_is_vacuous_and_never_labels(self):
        outcome, nodes = self.run_build({"verification": [["python3", "hello_absent.py"]]}, write={"other.py": "x = 1\n"})
        self.assertEqual(outcome["status"], "success", outcome)
        [check] = nodes["implement"]["result"]["acceptance_checks"]
        self.assertTrue(check["vacuous"])
        self.assertEqual(nodes["implement"]["result"]["gate_label"]["status"], "unlabeled")
        # Passed before, fails after: the gate rejects, but a vacuous check is no negative label.
        outcome, nodes = self.run_build({"verification": [["python3", "hello_absent.py"]]}, write={"hello.py": "print(1)\n"})
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(nodes["implement"]["result"]["gate_label"]["status"], "unlabeled")
        self.assertEqual(self.gate_labels(), [])

    def test_a_retry_reuses_the_pre_change_baseline(self):
        outcome, nodes = self.run_build({"verification": [["python3", "hello_exists.py"]]}, attempts=2,
                                        sequence=[{"other.py": "x = 1\n"}, {"hello.py": "print(1)\n"}])
        self.assertEqual(outcome["status"], "success", outcome)
        self.assertEqual(nodes["implement"]["attempts"], 2)
        root = Path(outcome["artifacts"]["root"]) / "nodes/implement/acceptance"
        self.assertEqual([path.name.split("-")[0] for path in sorted((root / "attempt-1").iterdir())], ["before", "check"])
        self.assertEqual([path.name.split("-")[0] for path in (root / "attempt-2").iterdir()], ["check"])
        answers = [label["answers"] for label in self.gate_labels()]
        self.assertEqual(answers, [{"failed_task": "true"}, {"failed_task": "false"}])

    def test_a_write_node_that_changed_nothing_labels_the_task_failed(self):
        outcome, nodes = self.run_build({"verification": []})
        self.assertEqual(outcome["status"], "failed")
        result = nodes["implement"]["result"]
        self.assertEqual([code["code"] for code in result["gate_codes"]], ["write_no_change"])
        self.assertEqual(result["gate_label"]["answers"], {"failed_task": "true"})

    def test_a_runner_that_cannot_start_is_dropped_not_blamed_on_the_implementation(self):
        self.config["verification"] = {"runners": ["python3", "fusion-missing-runner"]}
        outcome, nodes = self.run_build({"verification": [["fusion-missing-runner", "test"], ["python3", "hello_exists.py"]]},
                                        write={"hello.py": "print(1)\n"})
        self.assertEqual(outcome["status"], "success", outcome)
        result = nodes["implement"]["result"]
        self.assertEqual([check["argv"] for check in result["acceptance_checks"]], [["python3", "hello_exists.py"]])
        [dropped] = result["verification_rejected"]
        self.assertIn("could not start before the change", dropped["reason"])
        self.assertEqual(result["gate_label"]["answers"], {"failed_task": "false"})

    def test_authored_workflows_do_not_execute_plan_verification(self):
        outcome, nodes = self.run_build({"verification": [["python3", "hello_exists.py"]]}, write={"other.py": "x = 1\n"},
                                        plan_verification=False)
        self.assertEqual(outcome["status"], "success", outcome)
        self.assertEqual(nodes["implement"]["result"]["acceptance_checks"], [])
        self.assertFalse((Path(outcome["artifacts"]["root"]) / "nodes/implement/acceptance").exists())

    def test_blocker_only_rejection_is_neither_an_outcome_nor_a_label(self):
        outcome, nodes = self.run_build({"verification": [["python3", "hello_exists.py"]]}, write={"hello.py": "print(1)\n"},
                                        blockers="a note the parser read as a blocker")
        self.assertEqual(outcome["status"], "failed")
        result = nodes["implement"]["result"]
        self.assertEqual([code["code"] for code in result["gate_codes"]], ["worker_blockers"])
        self.assertEqual(result["gate_label"]["status"], "unlabeled")
        self.assertEqual(self.events("outcome", result["run_id"]), [])
        [excluded] = self.events("outcome_excluded", result["run_id"])
        self.assertEqual((excluded["accepted"], excluded["laya_veto"], excluded["gate_codes"]), (False, False, ["worker_blockers"]))

    def test_laya_veto_never_labels_or_ranks_and_gate_labels_ignore_it(self):
        self.backend = ImplementsOnly()
        self.config["decisions"] = {"mode": "active", "auto_actions": ["acceptance"],
                                    "calibration_file": str(self.root / "calibration.json")}
        (self.root / "calibration.json").write_text(json.dumps({
            "schema": "fusion.calibration.v1", "model_identity": "fixture-model", "buckets": {
                f"acceptance:{digest(ACCEPTANCE_QUESTIONS)}:{key}": {"qualified": True, "temperature": 1, "threshold": .9}
                for key in ACCEPTANCE_QUESTIONS}}))
        self.write_config()
        outcome, nodes = self.run_build({"verification": [["python3", "hello_exists.py"]]}, write={"hello.py": "print(1)\n"})
        result = nodes["implement"]["result"]
        self.assertIn("Laya acceptance check: reported success does not plausibly match the task", result["blockers"])
        self.assertEqual(result["gate_codes"], [])
        self.assertEqual(result["gate_label"]["answers"], {"failed_task": "false"}, "the objective evidence stands")
        self.assertEqual(self.events("outcome", result["run_id"]), [])
        [excluded] = self.events("outcome_excluded", result["run_id"])
        self.assertTrue(excluded["laya_veto"])
        store = core.RunStore(self.workspace)
        spans = [{"agent": "codex", "run_id": result["run_id"], "status": "success"}]
        with patch.object(store, "traces", return_value=spans):
            codex = next(c for c in route_candidates(self.config, core.make_task(
                self.workspace, "auto", "x", "implementation", [], [], None, False, True), store) if c["key"] == "codex")
        self.assertEqual(codex["checked_runs"], 0, "a vetoed run is not a rejection of the lane")

    def test_lead_verdicts_supersede_gate_answers_only_for_the_questions_they_answer(self):
        _, nodes = self.run_build({"verification": [["python3", "hello_exists.py"]]}, write={"hello.py": "print(1)\n"})
        run_id = nodes["implement"]["result"]["run_id"]
        gate_id = nodes["implement"]["result"]["gate_label"]["decision_id"]
        rejected = core.record_outcome(self.workspace, run_id, False, "The export ignores the delimiter option")
        self.assertEqual(rejected["label"]["decision_id"], gate_id, "the verdict labels the input the gate labeled")
        events = read_jsonl(self.store.path)
        self.assertEqual(reviewed_labels(events)[0][gate_id], {"plausible": "false", "failed_task": "false"})
        sources = {key: value["source"] for key, value in label_provenance(events)[gate_id].items()}
        self.assertEqual(sources, {"plausible": "lead_verdict", "failed_task": "structural_gate"})
        core.record_outcome(self.workspace, run_id, True, "Second look: the option is honored")
        events = read_jsonl(self.store.path)
        self.assertEqual({k: v["source"] for k, v in label_provenance(events)[gate_id].items()},
                         {"plausible": "lead_verdict", "failed_task": "lead_verdict"})
        retracted = core.record_outcome(self.workspace, run_id, True, "")
        self.assertEqual(retracted["label"]["status"], "retracted")
        events = read_jsonl(self.store.path)
        self.assertEqual(reviewed_labels(events)[0][gate_id], {"failed_task": "false"})
        self.assertEqual(label_provenance(events)[gate_id]["failed_task"]["source"], "structural_gate")
        # A human label is never overwritten by a gate, and export carries provenance.
        exported = self.store.export(self.root / "all.jsonl")
        rows = [json.loads(line) for line in (self.root / "all.jsonl").read_text().splitlines()]
        row = next(r for r in rows if r["id"] == gate_id)
        self.assertEqual(row["label_provenance"]["failed_task"]["source"], "structural_gate")
        self.assertGreaterEqual(exported["examples"], 1)
        self.store.export(self.root / "no-gate.jsonl", ["structural_gate"])
        kept = [json.loads(line)["id"] for line in (self.root / "no-gate.jsonl").read_text().splitlines()]
        self.assertNotIn(gate_id, kept)

    def test_explicit_kind_from_the_user_is_an_intake_label(self):
        prepared = prepare(self.workspace, self.config, "Fix the CSV export crash", kind="debug", kind_source="user")
        self.assertEqual(prepared["intake_label"]["answers"], {"workflow": "debug"})
        [application] = [e for e in self.events("application") if e["id"] == prepared["decision_id"]]
        self.assertEqual((application["explicit_kind"], application["kind_source"]), ("debug", "user"))
        [label] = [e for e in self.events("label") if e["id"] == prepared["decision_id"]]
        self.assertEqual((label["source"], label["answers"], label["reviewers"][0]["agent"]),
                         ("user_explicit", {"workflow": "debug"}, "user"))
        spec = json.loads(Path(prepared["workflow"]).read_text())
        implement = next(node for node in spec["nodes"] if node["id"] == "implement")
        self.assertTrue(implement["acceptance"]["plan_verification"])

    def test_fallback_agent_and_programmatic_kinds_are_not_labels(self):
        for kind, source in ((None, None), ("debug", "truffle"), ("build", "agent"), ("build", None)):
            with self.subTest(kind=kind, source=source):
                prepared = prepare(self.workspace, self.config, "Fix the CSV export crash", kind=kind, kind_source=source)
                self.assertNotIn("intake_label", prepared)
                [application] = [e for e in self.events("application") if e["id"] == prepared["decision_id"]]
                self.assertEqual(application["explicit_kind"], kind)
                self.assertEqual(application["kind_source"], (source or "caller") if kind else None)
        self.assertEqual(self.events("label"), [])
        overridden = prepare(self.workspace, self.config, "Investigate retries. Do not implement.", kind="build", kind_source="user")
        self.assertEqual((overridden["kind"], overridden["intake_label"]["status"]), ("discovery", "skipped"))
        self.assertEqual(self.events("label"), [])

    def test_cli_kind_is_user_intent_and_mcp_kind_is_not(self):
        for extra, labeled in (([], True), (["--kind-source", "agent"], False)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(core.main(["--workspace", str(self.workspace), "build", "--plan-only", "--kind", "build",
                                            *extra, "Add exports"]), 0)
            prepared = json.loads(output.getvalue())
            self.assertEqual("intake_label" in prepared, labeled)
        with contextlib.redirect_stdout(io.StringIO()):
            core.main(["--workspace", str(self.workspace), "build", "--plan-only", "Add exports"])
        self.assertEqual(len([e for e in self.events("label") if e.get("source") == "user_explicit"]), 1)

    def test_automatic_labels_can_be_turned_off(self):
        self.config["decisions"]["automatic_labels"] = False
        self.write_config()
        prepared = prepare(self.workspace, self.config, "Fix the CSV export crash", kind="debug", kind_source="user")
        self.assertEqual(prepared["intake_label"]["status"], "disabled")
        _, nodes = self.run_build({"verification": [["python3", "hello_exists.py"]]}, write={"hello.py": "print(1)\n"})
        self.assertNotIn("gate_label", nodes["implement"]["result"])
        self.assertEqual(self.events("label"), [])


if __name__ == "__main__":
    unittest.main()


class WriterMayRunVerificationTest(unittest.TestCase):
    """Live 2026-09-24: a restricted Claude writer fixed the bug, was denied Bash
    when it tried the plan's test, reported the work unverified, and the node
    failed although the coordinator's own check went from failing to passing."""
    def task(self, argv=None, write=True):
        task = core.make_task(Path(tempfile.gettempdir()), "claude", "Fix add()", "implementation", [], [], None, False, write)
        if argv is not None:
            task["verification_argv"] = argv
        return task

    def test_claude_writer_may_run_exactly_the_vetted_checks(self):
        config = core.deep_merge(core.DEFAULTS, {"claude": {"command": sys.executable}})
        argv, _, _ = core.agent_command(config, self.task([["python3", "-m", "unittest", "test_calc"]]), None)
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--")]
        self.assertEqual(allowed, ["Bash(python3 -m unittest test_calc:*)"])
        plain, _, _ = core.agent_command(config, self.task(), None)
        self.assertNotIn("--allowedTools", plain)
        yolo = core.deep_merge(config, {"execution_mode": "yolo"})
        full, _, _ = core.agent_command(yolo, self.task([["python3", "-m", "unittest"]]), None)
        self.assertNotIn("--allowedTools", full)
