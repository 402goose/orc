import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_admission as admission
import fusion_core as core
from fusion_workflow import load_spec
from fusion_ui import ControlRoom


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.environment = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "FUSION_TELEMETRY": "0"})
        self.environment.start()
        for key in ("FUSION_CONFIG", "FUSION_CONFIG_SHA256", "FUSION_SPEC_SHA256", "FUSION_ADMISSION_PROVIDER"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def bind(self, command=None):
        os.environ["FUSION_ADMISSION_PROVIDER"] = json.dumps({"command": command or ["/missing/provider"], "workspaces": [str(self.workspace)]})

    def test_standalone_and_required_unavailable_are_distinct(self):
        self.assertEqual(admission.describe(self.workspace)["mode"], "standalone")
        self.bind()
        value = admission.describe(self.workspace)
        self.assertEqual(value["mode"], "tenet")
        self.assertFalse(value["ready"])
        self.assertEqual(admission.describe(self.root)["mode"], "standalone")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            admission.call(self.workspace, "admit", run_id="first")

    def test_malformed_operator_binding_cannot_fall_back(self):
        os.environ["FUSION_ADMISSION_PROVIDER"] = "{}"
        self.assertFalse(admission.describe(self.workspace)["ready"])
        with self.assertRaises(ValueError):
            ControlRoom(self.workspace).launch(self.workspace, {"action": "delegate", "text": "test"})

    def test_response_identity_is_checked_even_on_success(self):
        self.bind([sys.executable])
        value = {"schema": "tenet.admission-response.v1", "ok": True, "operation": "claim",
                 "workspace": str(self.root), "run_id": "first"}
        with patch("fusion_admission.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(value), "")):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                admission.call(self.workspace, "claim", run_id="first")

    def test_unsupported_model_routes_refuse_without_dispatch(self):
        self.bind()
        app = ControlRoom(self.workspace)
        with patch("fusion_ui.subprocess.Popen") as spawn:
            for action in ("build", "delegate", "resume", "train", "suggest-labels", "publish", "probe", "truffle-run"):
                with self.assertRaisesRegex(ValueError, "no admitted execution contract"):
                    app.launch(self.workspace, {"action": action, "text": "task", "allow_write": True})
            spawn.assert_not_called()
        self.assertFalse((self.workspace / ".fusion/ui/jobs").exists())

    def test_bound_launch_requires_stable_request_identity(self):
        self.bind()
        with self.assertRaisesRegex(ValueError, "stable request_id"):
            ControlRoom(self.workspace).launch(self.workspace, {"action": "workflow"})

    def test_admitted_config_ignores_mutable_project_and_global_policy(self):
        global_dir = self.root / "orc"
        global_dir.mkdir()
        (global_dir / "fusion.json").write_text(json.dumps({"execution_mode": "yolo", "codex": {"sandbox": "danger-full-access"}}))
        (self.workspace / ".fusion.json").write_text(json.dumps({"execution_mode": "yolo"}))
        frozen = self.root / "config.json"
        raw = json.dumps({"execution_mode": "restricted", "codex": {"sandbox": "read-only", "git_write": False}}).encode()
        frozen.write_bytes(raw)
        os.environ.update(FUSION_CONFIG=str(frozen), FUSION_CONFIG_SHA256=hashlib.sha256(raw).hexdigest())
        config, _ = core.load_config(self.workspace)
        self.assertEqual(config["execution_mode"], "restricted")
        self.assertEqual(config["codex"]["sandbox"], "read-only")
        frozen.write_text('{"execution_mode":"yolo"}')
        with self.assertRaisesRegex(SystemExit, "hash mismatch"):
            core.load_config(self.workspace)

    def test_workflow_validates_consumed_bytes_before_dispatch(self):
        path = self.root / "workflow.json"
        raw = json.dumps({"task": "review", "nodes": [{"id": "review", "agent": "codex", "task": "inspect"}]}).encode()
        path.write_bytes(raw)
        os.environ["FUSION_SPEC_SHA256"] = hashlib.sha256(raw).hexdigest()
        self.assertEqual(load_spec(path)["graph"]["nodes"][0]["task"], "inspect")
        path.write_text('{"nodes":[{"id":"review","task":"changed"}]}')
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_spec(path)


if __name__ == "__main__":
    unittest.main()
