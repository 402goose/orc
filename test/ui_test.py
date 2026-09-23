"""HTTP boundary, artifact recovery, settings preservation and real job lifecycle."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion_ui import ControlRoom, Server, atomic_json, read_json, MASK


def seed_workspace(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    worker = workspace / "codex-fixture"
    worker.write_text(f"#!{sys.executable}\n" + '''import json, sys, time
prompt = sys.stdin.read()
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Inspecting fixture source files'}}),flush=True)
if 'wait fixture' in prompt:
    time.sleep(30)
if 'LABEL_SUGGESTION_V1' in prompt:
    suggestion = {'answers': {'plausible': {'value': 'false', 'reason': 'The input reports no work on the requested fix.', 'evidence': ['E1']}}, 'abstentions': {}}
    message = 'STATUS: success\\nSUMMARY: Drafted labels from the supplied input\\n```label-suggestion\\n' + json.dumps(suggestion) + '\\n```\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':message}}),flush=True)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':80}}),flush=True)
    sys.exit(0)
message = "STATUS: success\\nSUMMARY: Fixture investigation complete\\n\\n1. **Read payment input once**\\n\\nThe CLI consumes stdin twice. Read once and reuse the parsed value.\\n\\n| Impact | Effort | Risk |\\n| --- | --- | --- |\\n| High | Small | Low |\\n\\n```bash\\npython -m unittest\\n```\\n\\n2. **Preserve typed API errors**\\n\\nKeep retryable transport failures distinct from authentication errors.\\n\\nCHANGED: none\\nTESTS: fixture checks passed\\nBLOCKERS: none"
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':message}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':80}}),flush=True)
''')
    worker.chmod(0o755)
    config = {"sidekick": "codex", "decisions": {"mode": "off"}, "telemetry": {"remote": {"enabled": False}},
              "codex": {"command": str(worker)}, "claude": {"command": "missing-ui-test-worker"}, "agy": {"command": "missing-ui-test-worker"}, "grok": {"command": "missing-ui-test-worker"},
              "routes": {"orc-free": {"command": "missing-ui-test-route"}, "orc-best": {"command": "missing-ui-test-route"}}}
    atomic_json(workspace / ".fusion.json", config)
    return config


class ControlRoomTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "repo"
        self.config = seed_workspace(self.workspace)
        self.env = patch.dict(os.environ, {"FUSION_TELEMETRY": "0", "ORC_HOME": str(self.root / "orc-home")})
        self.env.start()
        self.app = ControlRoom(self.workspace, self.root / "registry.json")
        self.server = Server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        for job in self.app.jobs(self.workspace):
            if job["status"] in {"queued", "running", "stopping"}:
                (self.workspace / ".fusion/ui/jobs" / job["id"] / "cancel").touch()
        for proc in self.app.children:
            proc.wait(timeout=10)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        request = Request(self.server.origin + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"X-Fusion-Token": self.server.token, "Content-Type": "application/json", **(headers or {})})
        try:
            response = urlopen(request, timeout=8)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), dict(response.headers)

    def wait_job(self, job_id, predicate=lambda j: j["status"] not in {"queued", "running", "stopping"}):
        until = time.monotonic() + 12
        while time.monotonic() < until:
            job = self.app.job(self.workspace, job_id)
            if predicate(job):
                return job
            time.sleep(.05)
        self.fail(f"job did not reach expected state: {job}")

    def test_capability_host_origin_and_mutation_boundary(self):
        status, _, _ = self.request("/api/overview", headers={"X-Fusion-Token": ""})
        self.assertEqual(status, 401)
        self.assertEqual(self.request("/api/overview", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/overview", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/launch", {"action": "setup"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/overview")[0], 200)
        self.assertEqual(self.app.jobs(self.workspace), [])

    def test_ui_ships_offline_assets_and_does_not_serve_arbitrary_files(self):
        status, page, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"ORC", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/vendor/marked.js")[0], 200)
        self.assertEqual(self.request("/vendor/purify.js")[0], 200)
        self.assertEqual(self.request("/../fusion_core.py")[0], 404)
        self.assertEqual(self.request("/api/file?path=../registry.json")[0], 400)
        self.assertEqual(self.request("/api/file?path=.fusion.json")[0], 400)
        (self.root / "outside.txt").write_text("private")
        (self.workspace / "link.txt").symlink_to(self.root / "outside.txt")
        self.assertEqual(self.request("/api/file?path=link.txt")[0], 400)

    def test_settings_preserve_secrets_and_reject_stale_saves(self):
        self.config["telemetry"]["remote"]["token"] = "secret-never-returned"
        self.config["custom"] = {"keep": True}
        atomic_json(self.workspace / ".fusion.json", self.config)
        original = self.app.config(self.workspace)
        self.assertNotIn("secret-never-returned", json.dumps(original))
        self.assertEqual(original["local"]["telemetry"]["remote"]["token"], MASK)
        edited = original["local"]
        edited["decisions"]["mode"] = "shadow"
        saved = self.app.save_config(self.workspace, {"value": edited, "revision": original["revision"]})
        on_disk = read_json(self.workspace / ".fusion.json")
        self.assertEqual(on_disk["telemetry"]["remote"]["token"], "secret-never-returned")
        self.assertTrue(on_disk["custom"]["keep"])
        self.assertNotEqual(saved["revision"], original["revision"])
        with self.assertRaisesRegex(ValueError, "changed on disk"):
            self.app.save_config(self.workspace, {"value": {}, "revision": original["revision"]})

    def test_launch_validates_scope_and_arguments_before_starting_workers(self):
        for body in [{"action":"build","kind":"build","text":"Implement CSV export"},
                     {"action":"build","kind":"debug","text":"Fix defect","budget":float('nan')},
                     {"action":"shell","text":"touch bad"},
                     {"action":"build","text":"Inspect","attempts":9},
                     {"action":"delegate","agent":"codex","text":"Inspect","route":"invented"},
                     {"action":"resume","run_id":"../outside"}]:
            with self.subTest(body=body), self.assertRaises((ValueError, TypeError)):
                self.app.launch(self.workspace, body)
        self.assertEqual(self.app.jobs(self.workspace), [])

    def test_preparation_launch_is_literal_and_creates_no_worker_runs(self):
        text = 'Inspect $(touch INJECTED) `touch ALSO_INJECTED` and file names'
        job = self.app.launch(self.workspace, {"action":"build","kind":"discovery","prepare":True,"text":text,"mode":"off"})
        result = self.wait_job(job["id"])
        self.assertEqual(result["status"], "success", result)
        self.assertFalse((self.workspace / "INJECTED").exists())
        self.assertFalse((self.workspace / "ALSO_INJECTED").exists())
        self.assertFalse((self.workspace / ".fusion/runs").exists())
        self.assertTrue(Path(result["result"]["workflow"]).is_file())
        saved = read_json(Path(result["result"]["request"]))
        self.assertEqual(saved["text"], text)

    def test_live_workflow_recovers_deliverables_and_can_resume_without_reexecution(self):
        job = self.app.launch(self.workspace, {"action":"build","kind":"discovery","text":"Inspect fixture","mode":"off","attempts":1})
        finished = self.wait_job(job["id"])
        self.assertEqual(finished["status"], "success", finished)
        report = self.app.workflow(self.workspace, finished["workflow_id"])
        self.assertEqual(report["status"], "success")
        self.assertIn("| Impact | Effort | Risk |", report["outputs"][-1]["text"])
        self.assertEqual(report["outputs"][-1]["findings"][0]["number"], 1)
        self.assertIn("Inspecting fixture source files", report["live_nodes"][0]["messages"])
        self.assertEqual(len(report["live_nodes"]), 2)
        before = list((self.workspace / ".fusion/runs").iterdir())
        resumed = self.app.launch(self.workspace, {"action":"resume","run_id":finished["workflow_id"],"mode":"off"})
        self.assertEqual(self.wait_job(resumed["id"])["status"], "success")
        self.assertEqual(len(list((self.workspace / ".fusion/runs").iterdir())), len(before))

    def test_cancellation_survives_server_reconstruction_and_cleans_worker(self):
        job = self.app.launch(self.workspace, {"action":"delegate","agent":"codex","text":"wait fixture","mode":"off"})
        directory = self.workspace / ".fusion/ui/jobs" / job["id"]
        self.wait_job(job["id"], lambda j: "worker started" in j.get("console", ""))
        with self.assertRaisesRegex(ValueError, "already active"):
            self.app.launch(self.workspace, {"action":"delegate","agent":"codex","text":"duplicate","mode":"off"})
        fresh_app = ControlRoom(self.workspace, self.root / "registry.json")
        self.assertEqual(fresh_app.jobs(self.workspace)[0]["id"], job["id"])
        status, _, _ = self.request("/api/cancel", {"id": job["id"]})
        self.assertEqual(status, 200)
        result = self.wait_job(job["id"])
        self.assertEqual(result["status"], "cancelled")
        activity = read_json(next((self.workspace / ".fusion/runs").glob("*/activity.json")))
        self.assertEqual(activity["status"], "interrupted")
        with self.assertRaises(ProcessLookupError):
            os.kill(activity["pid"], 0)

    def test_workspace_registration_does_not_scan_or_launch_other_repositories(self):
        second = self.root / "another-repo"
        second.mkdir()
        added = self.app.add_workspace(str(second))
        self.assertEqual(self.app.workspace(added["id"]), second.resolve())
        self.assertEqual(self.app.overview(second)["workflows"], [])
        self.assertFalse((second / ".fusion").exists())
        restored = ControlRoom(self.workspace, self.root / "registry.json")
        self.assertEqual(restored.workspace(added["id"]), second.resolve())

    def seed_decision(self):
        from fusion_decisions import DecisionStore
        store = DecisionStore(self.workspace)
        store.append("decision", id="label-test", kind="acceptance", mode="shadow", status="ok",
                     state='{"task":"Fix retries","summary":"Did nothing"}', truncated=False,
                     questions={"plausible": {"type": "noul", "instructions": "Does the evidence satisfy the task?"}},
                     prediction={"plausible": {"false": .7, "true": .3}}, schema_hash="fixture", context={})
        return store

    def test_label_suggestion_job_requires_approval_and_records_edits(self):
        from fusion_decisions import read_jsonl
        store = self.seed_decision()
        status, response, _ = self.request("/api/launch", {"action": "suggest-labels", "decision_id": "label-test", "agent": "codex"})
        self.assertEqual(status, 200, response)
        job = self.wait_job(json.loads(response)["id"])
        self.assertEqual(job["status"], "success", job)
        self.assertEqual(job["decision_id"], "label-test")
        self.assertEqual(store.export(self.root / "before.jsonl")["examples"], 0)
        events = read_jsonl(store.path)
        draft = next(e for e in events if e["event"] == "label_suggestion")
        self.assertFalse(draft["verified"])
        self.assertEqual(len(store.records()), 1)  # no recursive Laya decisions
        _, response, _ = self.request("/api/decisions")
        record = json.loads(response)["records"][0]
        self.assertEqual(record["suggestion_job"]["id"], job["id"])
        self.assertEqual(record["suggestions"][0]["suggestion_id"], draft["suggestion_id"])
        body = {"id": "label-test", "answers": {"plausible": "true"}, "evidence": "Human corrected the answer after checking the fixture.", "suggestion_id": draft["suggestion_id"]}
        self.assertEqual(self.request("/api/label", body)[0], 400)
        self.assertEqual(self.request("/api/label", {**body, "approved": True})[0], 200)
        self.assertEqual(store.export(self.root / "after.jsonl")["examples"], 1)
        label = read_jsonl(store.path)[-1]
        self.assertTrue(label["answers_edited"])
        self.assertEqual(label["suggested_by"]["run_id"], draft["run_id"])
        self.assertEqual(label["source"], "human_approved_suggestion")

    def test_duplicate_suggestion_jobs_are_rejected_and_cancellable(self):
        store = self.seed_decision()
        record = store.get("label-test")
        record.pop("schema", None)
        record.pop("event", None)
        record["state"] = "wait fixture"
        store.append("decision", **record)
        job = self.app.launch(self.workspace, {"action": "suggest-labels", "decision_id": "label-test", "agent": "codex"})
        self.wait_job(job["id"], lambda j: "worker started" in j.get("console", ""))
        with self.assertRaisesRegex(ValueError, "already being drafted"):
            self.app.launch(self.workspace, {"action": "suggest-labels", "decision_id": "label-test"})
        self.assertEqual(self.request("/api/cancel", {"id": job["id"]})[0], 200)
        self.assertEqual(self.wait_job(job["id"])["status"], "cancelled")
        self.assertEqual(store.export(self.root / "cancelled.jsonl")["examples"], 0)


if __name__ == "__main__":
    unittest.main()
