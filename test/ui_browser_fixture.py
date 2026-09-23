"""Disposable browser fixture: real HTTP/jobs, fake coding workers, no paid calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from ui_test import seed_workspace
from fusion_decisions import DecisionStore, digest
from fusion_ui import ControlRoom, Server, atomic_json

with tempfile.TemporaryDirectory(prefix="orc-ui-e2e-") as directory:
    root = Path(directory)
    workspace = root / "visa-demo"
    config = seed_workspace(workspace)
    if os.environ.get('FUSION_FIXTURE_COUNCIL') == '1':
        worker = workspace / 'claude-fixture'
        worker.write_text(f'#!{sys.executable}\n' + '''import json
suggestion = {'answers': {'plausible': {'value': 'false', 'reason': 'The original input says no work was done.', 'evidence': ['E1']}}, 'abstentions': {}}
message = 'STATUS: success\\nSUMMARY: Assessed evidence\\n```label-suggestion\\n' + json.dumps(suggestion) + '\\n```\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'
print(json.dumps({'type': 'result', 'result': message, 'is_error': False}))
''')
        worker.chmod(0o755)
        config['claude']['command'] = str(worker)
        atomic_json(workspace / '.fusion.json', config)
    os.environ["ORC_HOME"] = str(root / "orc-home")
    os.environ["FUSION_TELEMETRY"] = "0"
    app = ControlRoom(workspace, root / "registry.json")
    job = app.launch(workspace, {"action":"build", "kind":"discovery", "text":"Find the highest-impact improvements in CLI reliability and payment routing", "mode":"off", "attempts":1})
    for _ in range(200):
        result = app.job(workspace, job["id"])
        if result["status"] not in {"queued", "running"}:
            break
        time.sleep(.05)
    assert result["status"] == "success", result
    for path in (workspace / ".fusion/runs").glob("*/answer.md"):
        with path.open("a") as out:
            out.write('\n<script>window.PWNED=true</script><img src="https://evil.invalid/pixel" onerror="window.PWNED=true"><button data-action="setup-start">INJECTED ACTION</button>\n')
    store = DecisionStore(workspace)
    review = dict(id="abstained-review", kind="review", mode="shadow", status="ok", duration_ms=58,
                  state='{"request":"Obtain an independent review."}', truncated=False,
                  questions={"specialty": {"type": "choice", "criteria": {"general": "correctness", "payments": "money movement"}},
                             "needs_review": {"type": "noul", "instructions": "Does this handoff need independent review?"}},
                  recommendations={}, prediction={}, schema_hash="review-fixture", context={})
    store.append("decision", **review)
    store.append("label_suggestion", id=review["id"], suggestion_id="abstained-draft", verified=False,
                 decision_hash=digest({k: review.get(k) for k in ("state", "questions", "schema_hash")}),
                 answers={}, abstentions={"specialty": "Missing implementation details.", "needs_review": "The old prompt omitted allowed labels."},
                 agent="codex", run_id="fixture-teacher", sources=[{"id": "E1", "title": "Original decision input", "text": review["state"]}])
    store.append("decision", id="fixture-decision", kind="acceptance", mode="shadow", status="ok", duration_ms=58,
                 state='{"task":"Fix payment retries","summary":"Did nothing"}', truncated=False,
                 questions={"plausible":{"type":"noul","instructions":"Does the evidence plausibly satisfy the task?"}},
                 recommendations={"plausible":{"value":"false","probability":.88}},
                 prediction={"plausible":{"false":.88,"true":.12}}, model_identity="fixture-model", schema_hash="fixture", context={})
    store.append("application", id="fixture-decision", actual="accept", applied=False)
    if os.environ.get('FUSION_FIXTURE_LIVE_COUNCIL') == '1':
        store.append('label_exclusion', id='abstained-review', excluded=True)
        codex = workspace / 'codex-fixture'
        codex.write_text(codex.read_text().replace("if 'LABEL_SUGGESTION_V1' in prompt:", "if 'LABEL_SUGGESTION_V1' in prompt:\n    from pathlib import Path\n    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Checking the original evidence independently.'}}),flush=True)\n    while not Path(" + repr(str(root / 'release-codex')) + ").exists(): time.sleep(.05)"))
        claude = workspace / 'claude-fixture'
        claude.write_text(claude.read_text().replace('import json', 'import json, sys, time\nfrom pathlib import Path\nprompt=" ".join(sys.argv)')
                         .replace("message = 'STATUS:", "while not Path(" + repr(str(root / 'release-claude')) + ").exists(): time.sleep(.05)\nif 'fixture disputed' in prompt: suggestion['answers']['plausible']['value']='true'\nmessage = 'STATUS:"))
    server = Server(0, app)
    print(json.dumps({"url":server.url,"workspace":str(workspace),"workflow_id":result["workflow_id"],"root":str(root)}),flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for child in app.children:
            child.wait(timeout=10)
