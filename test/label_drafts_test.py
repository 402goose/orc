"""Attempt evidence, frozen completion and the admitted local fixture worker path."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_admission as admission
import fusion_core as core
from fusion_decisions import DecisionStore, read_jsonl
from fusion_label_drafts import collect_sources, completion_observation, decision_source, draft_request, evidence_sources, finalize
from fusion_ui import ControlRoom


class LabelDraftEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        self.store = DecisionStore(self.workspace)
        self.record = {"id": "decision", "kind": "review", "status": "ok", "truncated": False,
                       "state": "An independent review is required.", "questions": {
                           "needs_review": {"type": "noul", "instructions": "Is independent review required?"}},
                       "schema_hash": "fixture", "context": {"task_id": "attempt-one", "group": "workflow-one"},
                       "prediction": {"secret_teacher_leak": True}}
        self.store.append("decision", **self.record)
        self.run = self.workspace / ".fusion/runs/attempt-one"; self.run.mkdir(parents=True)
        (self.run / "result.json").write_text(json.dumps({"status": "success", "tests": ["worker claims pass"], "decisions": {"hidden": "never teach"}}))
        (self.run / "answer.md").write_text("Worker claim, not coordinator proof.")

    def receipt(self, worker="attempt-one", directory="check-1-0123456789ab", status="failed"):
        root = self.workspace / ".fusion/workflows/workflow-one/nodes/work/acceptance/attempt-1" / directory
        root.mkdir(parents=True)
        outputs = {}
        for name in ("stdout", "stderr"):
            data = b"independent failure evidence\n" if name == "stdout" else b""
            path = root / (name + ".log"); path.write_bytes(data)
            outputs[name] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        receipt = {"schema": "fusion.acceptance-check.v1", "workflow_id": "workflow-one", "node_id": "work", "run_id": worker,
                   "attempt": 1, "status": status, "exit_code": 1, "timed_out": False, "finished_at_ms": 100, "outputs": outputs,
                   "artifacts": {"receipt": str(root / "receipt.json"), "stdout": str(root / "stdout.log"), "stderr": str(root / "stderr.log")}}
        path = root / "receipt.json"; path.write_text(json.dumps(receipt))
        return path

    def test_exact_attempt_checks_are_distinct_from_worker_claims_and_other_attempts(self):
        self.receipt()
        other = self.receipt(worker="another-attempt", directory="check-2-fedcba987654", status="passed")
        sources = evidence_sources(self.workspace, self.record)
        checks = [source for source in sources if source["kind"] == "coordinator_check"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(json.loads(checks[0]["text"])["status"], "failed")
        self.assertFalse(any(source.get("path") == other.relative_to(self.workspace).as_posix() for source in sources))
        self.assertTrue(any(source["kind"] == "worker_claim" and "worker claims pass" in source["text"] for source in sources))
        self.assertNotIn("secret_teacher_leak", json.dumps(sources))
        self.assertNotIn("never teach", json.dumps(sources))

    def test_mismatched_output_hash_or_symlink_or_receipt_identity_is_refused(self):
        path = self.receipt()
        path.with_name("stdout.log").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "hash"):
            collect_sources(self.workspace, self.record)
        path.with_name("stdout.log").unlink()
        path.with_name("stdout.log").symlink_to(self.run / "answer.md")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            collect_sources(self.workspace, self.record)
        path.with_name("stdout.log").unlink()
        path.with_name("stdout.log").write_text("independent failure evidence\n")
        receipt = json.loads(path.read_text()); receipt["attempt"] = 2; path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "attempt identity"):
            collect_sources(self.workspace, self.record)

    def test_deterministic_identity_does_not_change_with_model_or_later_events(self):
        one = draft_request(self.workspace, "decision", "gpt-6-astra", "high")
        self.store.append("application", id="decision", actual="not ground truth")
        two = draft_request(self.workspace, "decision", "gpt-6-sol", "xhigh")
        self.assertEqual(one["run_id"], two["run_id"])
        self.assertEqual(one["label_draft"], two["label_draft"])
        with self.assertRaisesRegex(ValueError, "non-ultra"):
            draft_request(self.workspace, "decision", "gpt-6-astra", "ultra")

    def test_label_payload_cannot_become_a_handoff_blocker_or_override_status(self):
        text = 'STATUS: success\nSUMMARY: labels drafted\nBLOCKERS: none\n```label-suggestion\n{"abstentions":{"q":"STATUS: blocked"},"answers":{}}\n```'
        handoff = core.parse_handoff(text)
        self.assertEqual(handoff["reported_status"], "success")
        self.assertEqual(handoff["blockers"], [])
        self.assertEqual(core.parse_handoff(text + '\nBLOCKERS: missing review')["blockers"], ["missing review"])
        self.assertEqual(core.parse_handoff(text.replace('BLOCKERS: none', 'BLOCKERS: genuine failure'))["blockers"], ["genuine failure"])
        self.assertTrue(core.parse_handoff(text.rsplit('\n```', 1)[0])["blockers"])

    def prepare_completed(self, routing=False):
        if routing:
            self.record.update(kind="routing", questions={"route": {"type": "choice", "criteria": {"a": "Astra high", "s": "Sol xhigh"}}})
            self.store.append("decision", **self.record)
        request = draft_request(self.workspace, "decision")
        self.identifier = request["run_id"]
        self.packet = {"schema": "fusion.label-draft-packet.v1", "decision": {key: self.record[key] for key in ("id", "kind", "state", "questions", "schema_hash", "context")},
                       "sources": evidence_sources(self.workspace, self.record), "comparative_evidence": "absent",
                       "decision_line_sha256": request["label_draft"]["decision_line_sha256"]}
        frozen = self.root / "label_draft.json"; frozen.write_text(json.dumps(self.packet))
        self.response = {"terminal": True, "status": "success", "request_sha256": "frozen-request", "artifacts": {"label_draft": {
            "path": str(frozen), "sha256": hashlib.sha256(frozen.read_bytes()).hexdigest()}}}
        directory = self.workspace / ".fusion/workflows" / self.identifier; directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps({"schema": "fusion.workflow.v1", "workflow_id": self.identifier, "nodes": {"label": {"status": "success", "result": {
            "run_id": "teacher-attempt", "agent": "codex", "status": "success", "exit_code": 0, "model": "fixture-model"}}}}))
        teacher = self.workspace / ".fusion/runs/teacher-attempt"; teacher.mkdir(parents=True)
        (teacher / "task.json").write_text(json.dumps({"parent_task_id": self.identifier, "agent": "codex", "role": "labeling", "write": False}))
        (teacher / "result.json").write_text(json.dumps({"run_id": "teacher-attempt", "agent": "codex", "status": "success", "exit_code": 0, "model": "fixture-model"}))
        answers = {"route" if routing else "needs_review": {"value": "a" if routing else "true", "reason": "Fixture evidence.", "evidence": ["E1"]}}
        (teacher / "answer.md").write_text('```label-suggestion\n' + json.dumps({"answers": answers, "abstentions": {}}) + '\n```')
        output = {"schema": "fusion.label-draft-output.v1", "run_id": self.identifier,
                  "request_sha256": self.response["request_sha256"], "worker_id": "teacher-attempt",
                  "manifest": json.loads((directory / "manifest.json").read_text()),
                  "task": json.loads((teacher / "task.json").read_text()),
                  "result": json.loads((teacher / "result.json").read_text()),
                  "answer_text": (teacher / "answer.md").read_text(),
                  "hashes": completion_observation(self.workspace, self.identifier)}
        frozen_output = self.root / "label_output.json"; frozen_output.write_text(json.dumps(output))
        self.response["artifacts"]["label_output"] = {"path": str(frozen_output), "sha256": hashlib.sha256(frozen_output.read_bytes()).hexdigest()}

    def test_concurrent_completion_and_restart_recovery_append_once_and_never_approve(self):
        self.prepare_completed()
        with patch.object(admission, "call", return_value=self.response), patch.object(core, "dispatch", side_effect=AssertionError("No worker in completion")):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: finalize(self.workspace, self.identifier), range(4)))
            again = finalize(self.workspace, self.identifier)
        self.assertEqual(len([event for event in read_jsonl(self.store.path) if event["event"] == "label_suggestion"]), 1)
        self.assertTrue(again["recovered"])
        self.assertTrue(all(not result["verified"] for result in results))
        self.assertEqual(self.store.export(self.root / "dataset.jsonl")["examples"], 0)

    def test_comparative_answer_is_mechanically_abstained_and_raw_proposal_retained(self):
        self.prepare_completed(routing=True)
        with patch.object(admission, "call", return_value=self.response):
            result = finalize(self.workspace, self.identifier)
        self.assertEqual(result["answers"], {})
        self.assertIn("No matched comparative", result["abstentions"]["route"])
        self.assertIn("route", result["teacher_answers"])

    def test_incomplete_or_changed_provenance_cannot_complete(self):
        self.prepare_completed()
        with patch.object(admission, "call", return_value={**self.response, "terminal": False, "status": "claimed"}):
            with self.assertRaisesRegex(ValueError, "terminal"):
                finalize(self.workspace, self.identifier)
        self.store.append("decision", **{**self.record, "state": "changed input"})
        with patch.object(admission, "call", return_value=self.response):
            with self.assertRaisesRegex(ValueError, "changed"):
                finalize(self.workspace, self.identifier)
        self.assertFalse(any(event["event"] == "label_suggestion" for event in read_jsonl(self.store.path)))

    def test_completion_uses_frozen_output_after_mutable_teacher_files_change_or_disappear(self):
        self.prepare_completed()
        teacher = self.workspace / ".fusion/runs/teacher-attempt"
        (teacher / "answer.md").write_text('```label-suggestion\n' + json.dumps({"answers": {"needs_review": {
            "value": "false", "reason": "Substituted after terminal success", "evidence": ["E1"]}}, "abstentions": {}}) + '\n```')
        shutil.rmtree(teacher)
        shutil.rmtree(self.workspace / ".fusion/workflows" / self.identifier)
        with patch.object(admission, "call", return_value=self.response):
            result = finalize(self.workspace, self.identifier)
        self.assertEqual(result["answers"]["needs_review"]["value"], "true")
        self.assertEqual(result["teacher_artifacts"]["frozen_output"], self.response["artifacts"]["label_output"])

    def test_missing_or_changed_frozen_output_refuses_mutable_fallback(self):
        self.prepare_completed()
        missing = copy.deepcopy(self.response); del missing["artifacts"]["label_output"]
        with patch.object(admission, "call", return_value=missing), self.assertRaisesRegex(ValueError, "label_output"):
            finalize(self.workspace, self.identifier)
        Path(self.response["artifacts"]["label_output"]["path"]).write_text('{}')
        with patch.object(admission, "call", return_value=self.response), self.assertRaisesRegex(ValueError, "changed"):
            finalize(self.workspace, self.identifier)
        self.assertFalse(any(event["event"] == "label_suggestion" for event in read_jsonl(self.store.path)))

    def test_completion_observation_refuses_wrong_worker_identity_or_missing_output(self):
        self.prepare_completed()
        teacher = self.workspace / ".fusion/runs/teacher-attempt"
        (teacher / "result.json").write_text(json.dumps({"run_id": "substituted", "status": "success", "exit_code": 0}))
        with self.assertRaisesRegex(ValueError, "completed attempt"):
            completion_observation(self.workspace, self.identifier)
        (teacher / "result.json").unlink()
        with self.assertRaises(FileNotFoundError):
            completion_observation(self.workspace, self.identifier)


@unittest.skipUnless(os.environ.get("TENET_LABEL_TEST_PROVIDER"), "set compiled optional TENET label provider")
class LabelProviderTest(unittest.TestCase):
    setUp = LabelDraftEvidenceTest.setUp
    receipt = LabelDraftEvidenceTest.receipt
    def test_real_provider_supervisor_drafts_once_and_recovers_without_worker_repeat(self):
        provider = os.environ["TENET_LABEL_TEST_PROVIDER"]
        self.receipt()
        counter = self.root / "dispatches"
        worker = self.root / "fixture-codex"
        worker.write_text(f'#!{sys.executable}\n' + f'''import json,sys,pathlib
prompt=sys.stdin.read()
assert 'LABEL_SUGGESTION_V1' in prompt
assert sys.argv[sys.argv.index('-s')+1]=='read-only'
assert 'forbidden project reference' not in prompt
with pathlib.Path({str(counter)!r}).open('a') as out: out.write('dispatch\\n')
answer='STATUS: success\\nSUMMARY: Drafted evidence-grounded label\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none\\n```label-suggestion\\n'+json.dumps({{"answers":{{"needs_review":{{"value":"true","reason":"The original input explicitly requires independent review.","evidence":["E1"]}}}},"abstentions":{{}}}})+'\\n```'
print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":answer}}}}))
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":10,"output_tokens":5}}}}))
'''); worker.chmod(0o700)
        (self.workspace / "README.md").write_text("forbidden project reference")
        policy = self.root / "operator.json"
        policy.write_text(json.dumps({"schema": "tenet.admission-policy.v1", "state_root": str(self.root / "external-state"), "workspaces": [{
            "id": "fixture", "path": str(self.workspace), "context_paths": ["README.md"], "executor": {
                "command": str(worker), "model": "gpt-6-astra", "reasoning_effort": "high", "allow_native_delegation": True}}]}))
        (self.workspace / ".fusion.json").write_text(json.dumps({"execution_mode": "restricted", "codex": {
            "command": str(worker), "model": "gpt-6-astra", "reasoning_effort": "high", "sandbox": "read-only", "approval": "never", "git_write": False},
            "timeout_seconds": 10, "publish": {"mode": "off"}, "telemetry": {"remote": {"enabled": False}}}))
        env = {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0", "TENET_TELEMETRY": "0", "FUSION_ADMISSION_PROVIDER": json.dumps({
            "command": [shutil.which("node"), provider, "--policy", str(policy)], "workspaces": [str(self.workspace)]})}
        with patch.dict(os.environ, env):
            for key in ("FUSION_CONFIG", "FUSION_CONFIG_SHA256", "FUSION_SPEC_SHA256", "FUSION_JOB_ADMISSION", "FUSION_DECISIONS_MODE"):
                os.environ.pop(key, None)
            app = ControlRoom(self.workspace)
            body = {"action": "suggest-labels", "decision_id": "decision", "agent": "codex", "approval_mode": "human"}
            job = app.launch(self.workspace, body)
            deadline = time.time() + 20
            while time.time() < deadline:
                job = app.job(self.workspace, job["id"])
                if job["status"] not in {"queued", "running", "stopping"}:
                    break
                time.sleep(.05)
            for child in app.children:
                child.wait(timeout=5)
            self.assertEqual(job["status"], "success", {"error": job.get("error"), "stderr": job.get("stderr", "")[-3000:],
                "nodes": [{k: node.get("result", {}).get(k) for k in ("status", "summary", "blockers")} for node in (job.get("result") or {}).get("nodes", [])]})
            self.assertEqual(job["phase"], "needs_review")
            receipt = admission.call(self.workspace, "status", run_id=job["id"])
            frozen_output = json.loads(Path(receipt["artifacts"]["label_output"]["path"]).read_text())
            # Reconstruct the interruption window after terminal persistence but
            # before the local suggestion append. This edits fixture data only.
            self.store.path.write_text(''.join(json.dumps(event) + '\n' for event in read_jsonl(self.store.path)
                                               if event["event"] != "label_suggestion"))
            teacher = self.workspace / ".fusion/runs" / frozen_output["worker_id"]
            (teacher / "answer.md").write_text("post-terminal replacement must never be parsed")
            shutil.rmtree(teacher)
            shutil.rmtree(self.workspace / ".fusion/workflows" / job["id"])
            recovered = ControlRoom(self.workspace).launch(self.workspace, body)
            self.assertEqual(recovered["result"]["answers"]["needs_review"]["value"], "true")
            again = ControlRoom(self.workspace).launch(self.workspace, body)
            self.assertTrue(again["result"]["recovered"])
            self.assertEqual(counter.read_text().splitlines(), ["dispatch"])
            self.assertEqual(len([event for event in read_jsonl(self.store.path) if event["event"] == "label_suggestion"]), 1)
            self.assertEqual(self.store.export(self.root / "export.jsonl")["examples"], 0)
            receipt = admission.call(self.workspace, "status", run_id=job["id"])
            self.assertEqual(receipt["context"]["refs"], [])
            self.assertFalse(receipt["request"]["intent"]["spec"]["nodes"][0]["allow_native_delegation"])
            self.assertTrue(any(source["kind"] == "coordinator_check" and '"failed"' in source["text"]
                                for source in job["result"]["sources"]))


if __name__ == "__main__":
    unittest.main()
