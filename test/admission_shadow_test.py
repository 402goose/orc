"""Shadow authority controls. No model or coding worker is executed.

Set TENET_ADMISSION_TEST_PROVIDER to a compiled admission-provider.js for the
optional real provider/supervisor checks; standalone ORC has no TENET dependency.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_admission as admission
import fusion_core as core
import fusion_decisions as decisions
import fusion_policy as policy
import fusion_ui as ui
from fusion_workflow import WorkflowRunner, load_spec


class Prediction:
    def __init__(self, plausible=False, unavailable=False):
        self.plausible, self.unavailable, self.calls = plausible, unavailable, 0

    def predict(self, state, questions):
        self.calls += 1
        if self.unavailable:
            raise RuntimeError("Controlled unavailable fixture; no model loaded")
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = {"noul": .999 if key != "plausible" or self.plausible else .001}
            else:
                labels = decisions.labels_for(question)
                selected = "switch" if "switch" in labels else labels[-1]
                answers[key] = {"probabilities": {label: .999 if label == selected else .001 / (len(labels) - 1) for label in labels}}
        return {"answers": answers, "model_identity": "controlled-predictor-not-a-model", "truncated": False}


class ShadowPolicyTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve()
        environment = patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "shadow", "FUSION_TELEMETRY": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("FUSION_WORKFLOW_ID", None)
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "shadow", "auto_actions": ["acceptance", "recovery"]}})

    def qualify(self, *args):
        return {"model_identity": "controlled-predictor-not-a-model", "buckets": {
            f"{kind}:{decisions.digest(questions)}:{key}": {"qualified": True, "temperature": 1, "threshold": .9}
            for kind, questions in (("acceptance", decisions.ACCEPTANCE_QUESTIONS), ("recovery", decisions.RECOVERY_QUESTIONS))
            for key in questions}}

    def run_workflow(self, backend, failed=False):
        spec = {"schema": "fusion.workflow.v1", "task": "Inspect a bounded contract", "max_attempts": 1,
                "publish": {"mode": "off"}, "nodes": [{"id": "work", "agent": "codex", "write": False,
                    "task": "Worker keeps complete frozen context. " * 200,
                    "decision_context": "Inspect a bounded contract"}]}
        worker = {"run_id": "native-attempt-fixture", "agent": "codex", "status": "success", "exit_code": 0,
                  "summary": "Observed the requested boundary", "tests": [], "changed": [], "usage": {},
                  "blockers": ["required evidence missing"] if failed else []}
        with patch.object(core, "dispatch", return_value=worker) as dispatch, \
                patch.object(decisions, "runtime_for", return_value=backend), \
                patch.object(decisions.DecisionEngine, "calibration", self.qualify):
            result = WorkflowRunner(self.workspace, self.config, spec, run_id="admitted-fixture").run()
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.args[1]["agent"], "codex")
        self.assertFalse(dispatch.call_args.args[1]["write"])
        return result, decisions.read_jsonl(decisions.DecisionStore(self.workspace).path)

    def test_shadow_cannot_veto_or_switch_and_records_linked_advice(self):
        result, events = self.run_workflow(Prediction(plausible=False))
        self.assertEqual(result["status"], "success")
        records = [e for e in events if e["event"] == "decision" and e.get("status") != "unscored"]
        self.assertEqual([r["kind"] for r in records], ["acceptance", "recovery"])
        for record in records:
            self.assertEqual(record["context"], {"task_id": "native-attempt-fixture", "group": "admitted-fixture"})
            self.assertEqual(record["model_identity"], "controlled-predictor-not-a-model")
            self.assertFalse(record["truncated"])
        self.assertNotIn("complete frozen context", records[0]["state"])
        self.assertTrue(all(not e["applied"] for e in events if e["event"] == "application"))
        self.assertTrue(next(e for e in events if e["event"] == "outcome")["accepted"])

    def test_positive_shadow_prediction_cannot_rescue_structural_failure_or_retry(self):
        backend = Prediction(plausible=True)
        result, events = self.run_workflow(backend, failed=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(backend.calls, 1)  # Recovery only; structural failure skips semantic acceptance.
        self.assertFalse(any(e.get("kind") == "acceptance" and e.get("status") != "unscored" for e in events if e["event"] == "decision"))
        # A non-objective structural failure is kept out of route ranking (#93).
        self.assertFalse(next(e for e in events if e["event"] in {"outcome", "outcome_excluded"})["accepted"])
        application = next(e for e in events if e["event"] == "application")
        self.assertEqual(application["actual"], "stop")
        self.assertFalse(application["applied"])

    def test_unavailable_shadow_preserves_structural_acceptance(self):
        result, events = self.run_workflow(Prediction(unavailable=True))
        self.assertEqual(result["status"], "success")
        self.assertTrue(all(e["status"] == "unavailable" for e in events if e["event"] == "decision" and e.get("status") != "unscored"))
        self.assertTrue(all(not e["applied"] for e in events if e["event"] == "application"))

    def test_active_countercontrol_really_can_veto(self):
        with patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "active"}):
            result, _ = self.run_workflow(Prediction(plausible=False))
        self.assertEqual(result["status"], "failed")
        self.assertIn("Laya acceptance check", " ".join(result["nodes"][0]["result"]["blockers"]))

    def test_explicit_codex_does_not_enumerate_routes_or_infer_a_choice(self):
        task = core.make_task(self.workspace, "codex", "Inspect", "discovery", [], [], None, False, False)
        before = copy.deepcopy(task)
        with patch.object(policy, "route_candidates", side_effect=AssertionError("No alternate routing")), \
                patch.object(decisions, "runtime_for", side_effect=AssertionError("One candidate is not a classifier choice")):
            policy.route_task(self.config, task, core.RunStore(self.workspace))
        self.assertEqual(task, before)


@unittest.skipUnless(os.environ.get("TENET_ADMISSION_TEST_PROVIDER") and shutil.which("node"), "set compiled optional TENET provider for integration controls")
class ShadowProviderSupervisorTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.worker = self.root / "never-run-codex"
        self.worker.write_text("#!/bin/sh\nexit 97\n")
        self.worker.chmod(0o700)
        (self.workspace / "README.md").write_text("Full frozen workspace reference. " * 200)
        self.python = self.root / "operator-python"
        self.python.symlink_to(sys.executable)
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"schema": "tenet.admission-policy.v1", "state_root": str(self.root / "state"),
            "workspaces": [{"id": "fixture", "path": str(self.workspace), "context_paths": ["README.md"],
                "executor": {"command": str(self.worker), "model": "fixture-only"},
                "observations": {"python": str(self.python)}, "allowed_checks": []}]}))
        provider = Path(os.environ["TENET_ADMISSION_TEST_PROVIDER"]).resolve()
        self.assertTrue(provider.is_file())
        environment = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "FUSION_TELEMETRY": "0",
            "FUSION_ADMISSION_PROVIDER": json.dumps({"command": [shutil.which("node"), str(provider), "--policy", str(self.policy)], "workspaces": [str(self.workspace)]}),
            "FUSION_DECISIONS_MODE": "active", "FUSION_LAYA_PYTHON": str(self.workspace / "untrusted-python")})
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("FUSION_CONFIG", "FUSION_CONFIG_SHA256", "FUSION_SPEC_SHA256", "FUSION_JOB_ADMISSION", "FUSION_WORKFLOW_ID"):
            os.environ.pop(key, None)
        self.config = {"execution_mode": "restricted", "timeout_seconds": 30, "publish": {"mode": "off"},
            "codex": {"command": str(self.worker), "model": "fixture-only", "sandbox": "read-only", "approval": "never", "git_write": False},
            "decisions": {"mode": "active", "python": str(self.workspace / "untrusted-python"), "model_path": "untrusted"}}
        (self.workspace / ".fusion.json").write_text(json.dumps(self.config))
        self.spec = {"schema": "fusion.workflow.v1", "task": "Inspect the bounded task", "max_parallel": 1,
            "max_parallel_writers": 0, "max_attempts": 1, "budget_usd": 1, "publish": {"mode": "off"},
            "nodes": [{"id": "work", "agent": "codex", "write": False, "task": "Inspect the bounded task"}]}

    def test_real_provider_and_supervisor_pin_shadow_runtime_and_context(self):
        self.supervisor_mode("shadow")

    def test_real_provider_and_supervisor_preserve_explicit_off(self):
        self.supervisor_mode("off")

    def test_configured_timeout_reaches_frozen_provider_contract_without_clamping(self):
        self.config["timeout_seconds"] = 1800
        admitted = admission.admit(self.workspace, "longer-build", self.spec, self.config)
        frozen = json.loads(Path(admitted["artifacts"]["config"]["path"]).read_text())
        self.assertEqual(frozen["timeout_seconds"], 1800)
        self.config["timeout_seconds"] = 3601
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            admission.admit(self.workspace, "over-policy-bound", self.spec, self.config)
        self.assertFalse((self.root / "state" / "fixture" / "over-policy-bound").exists())

    def test_teacher_timeout_remains_separately_bounded(self):
        self.config["timeout_seconds"] = 1800
        with patch.object(admission, "call", return_value={}) as provider:
            admission.admit(self.workspace, "teacher", self.spec, self.config, label_draft={})
        self.assertEqual(provider.call_args.kwargs["intent"]["config"]["timeout_seconds"], 600)

    def supervisor_mode(self, mode):
        app = ui.ControlRoom(self.workspace)
        original = subprocess.Popen
        supervisor = {}

        class Child:
            pid = 0
            returncode = 17
            def poll(self): return self.returncode

        def intercept(argv, *args, **kwargs):
            if "--job" in argv:
                supervisor.update(argv=argv, env=kwargs["env"])
                return Child()
            return original(argv, *args, **kwargs)

        with patch.object(subprocess, "Popen", side_effect=intercept):
            app.launch(self.workspace, {"action": "workflow", "request_id": "shadow-task", "spec": self.spec, "mode": mode})
        directory = self.workspace / ".fusion/ui/jobs/shadow-task"
        request = json.loads((directory / "request.json").read_text())
        request.update(mode="active", admission=None, argv=[str(self.worker)], workspace=str(self.root))
        (directory / "request.json").write_text(json.dumps(request))
        (self.workspace / ".fusion.json").write_text('{"execution_mode":"yolo","decisions":{"mode":"active"}}')
        (self.workspace / "README.md").write_text("Mutable source changed after admission")
        captured = []

        def executor_boundary(argv, *args, **kwargs):
            if "workflow" in argv and "run" in argv:
                # Exercise run_job through claim and config/spec loading, then
                # replace ONLY its executor process. Never run a coding worker.
                env = kwargs["env"]
                self.assertEqual(env["FUSION_DECISIONS_MODE"], mode)
                self.assertEqual(env["FUSION_LAYA_PYTHON"], str(self.python) if mode == "shadow" else "")
                with patch.dict(os.environ, env, clear=True):
                    config, _ = core.load_config(self.workspace)
                    options = decisions.config_for(config)
                    spec = load_spec(Path(argv[-1]))
                self.assertEqual(options["mode"], mode)
                self.assertEqual(options["python"], str(self.python) if mode == "shadow" else "")
                self.assertEqual(options["auto_actions"], [])
                self.assertEqual(config["publish"]["mode"], "off")
                self.assertEqual(spec["max_attempts"], 1)
                self.assertEqual(spec["nodes"][0]["agent"], "codex")
                self.assertEqual(spec["nodes"][0]["decision_context"], "Inspect the bounded task")
                self.assertIn("Full frozen workspace reference.", spec["nodes"][0]["task"])
                self.assertNotIn("Mutable source", spec["nodes"][0]["task"])
                captured.append(argv)
                kwargs["stdout"].write(b'{"status":"failed","workflow_id":"shadow-task"}')
                return Child()
            return original(argv, *args, **kwargs)

        with patch.dict(os.environ, supervisor["env"], clear=True), patch.object(subprocess, "Popen", side_effect=executor_boundary):
            ui.run_job(directory)
        self.assertEqual(len(captured), 1)
        status = admission.call(self.workspace, "status", run_id="shadow-task")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["liveness"], "unknown")
        self.assertFalse((self.workspace / ".fusion/decisions/events.jsonl").exists())

    def test_active_and_caller_context_refuse_before_supervisor(self):
        app = ui.ControlRoom(self.workspace)
        original = subprocess.Popen
        def refuse_supervisor(argv, *args, **kwargs):
            if "--job" in argv: raise AssertionError("Refusal must precede supervisor")
            return original(argv, *args, **kwargs)
        with patch.object(subprocess, "Popen", side_effect=refuse_supervisor):
            with self.assertRaisesRegex(ValueError, "off or shadow"):
                app.launch(self.workspace, {"action": "workflow", "request_id": "active", "spec": self.spec, "mode": "active"})
            self.spec["nodes"][0]["decision_context"] = "Caller substitute"
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                app.launch(self.workspace, {"action": "workflow", "request_id": "context", "spec": self.spec, "mode": "shadow"})
        self.assertFalse((self.root / "state").exists())


if __name__ == "__main__":
    unittest.main()
