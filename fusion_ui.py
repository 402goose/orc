"""Local control room. Existing Fusion artifacts are the source of truth.

No web framework or frontend build is required. API access requires a per-server
capability and a matching loopback Host/Origin. Jobs run in separate supervisors
so closing the browser or restarting the server does not stop a workflow.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

import fusion_core as core
import fusion_publish as publishing
from fusion_decisions import DecisionEngine, DecisionStore, config_for, read_jsonl
from fusion_report import finding_request, format_report, reported_cost, select_report, terminal_text
from fusion_workflow import validate_spec, workflow_report, workflow_status

ASSETS = Path(__file__).with_name("fusion_ui_assets")
MASK = "••••••••"
SECRET = re.compile(r"token|secret|password|api.?key|authorization", re.I)
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")
ACTIVE = {"queued", "running", "stopping"}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with open(temporary, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError("Invalid run identifier")
    return value


def inside(root, path):
    root = Path(root).resolve()
    path = Path(path).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path must stay inside this workspace")
    return path


def redact(value):
    if isinstance(value, dict):
        return {key: MASK if SECRET.search(key) and item else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def restore_secrets(value, previous):
    if isinstance(value, dict):
        previous = previous if isinstance(previous, dict) else {}
        return {key: (previous.get(key, "") if item == MASK and SECRET.search(key)
                      else restore_secrets(item, previous.get(key, {}))) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_secrets(item, previous[index] if isinstance(previous, list) and index < len(previous) else {})
                for index, item in enumerate(value)]
    return value


def revision(path):
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def tail(path, limit=100_000):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            return terminal_text(stream.read().decode("utf-8", "replace"))
    except OSError:
        return ""


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def run_job(directory):
    """Detached supervisor owns its child; cancellation never targets a saved PID."""
    directory = Path(directory)
    request = read_json(directory / "request.json")
    state = {"id": directory.name, "action": request["action"], "title": request["title"],
             "decision_id": request.get("decision_id"),
             "started_at_ms": core.now_ms(), "status": "running", "supervisor_pid": os.getpid()}
    atomic_json(directory / "job.json", state)
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "FUSION_PROGRESS": "1"}
    if request.get("mode"):
        environment["FUSION_DECISIONS_MODE"] = request["mode"]
    try:
        with (directory / "stdout.log").open("wb") as out, (directory / "stderr.log").open("wb") as err:
            proc = subprocess.Popen(request["argv"], cwd=request["workspace"], env=environment,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            cancelled = False
            while proc.poll() is None:
                if (directory / "cancel").exists() and not cancelled:
                    cancelled = True
                    state["status"] = "stopping"
                    try:
                        proc.send_signal(signal.SIGINT)
                    except ProcessLookupError:
                        pass
                state["updated_at_ms"] = core.now_ms()
                if not state.get("workflow_id"):
                    match = re.search(r"(\d{8}-\d{6}-wf-[a-f0-9]+)", tail(directory / "stderr.log", 12000))
                    if match:
                        state["workflow_id"] = match[1]
                atomic_json(directory / "job.json", state)
                time.sleep(.25)
            state.update(exit_code=proc.returncode, status="cancelled" if cancelled else "success" if proc.returncode == 0 else "failed")
        result = read_json(directory / "stdout.log")
        state["result"] = result
        if isinstance(result, dict):
            state["workflow_id"] = result.get("workflow_id")
        if not state.get("workflow_id"):
            match = re.search(r"(\d{8}-\d{6}-wf-[a-f0-9]+)", tail(directory / "stderr.log"))
            if match:
                state["workflow_id"] = match[1]
    except Exception as exc:
        state.update(status="failed", error=str(exc))
    state["finished_at_ms"] = core.now_ms()
    atomic_json(directory / "job.json", state)
    return 0


class ControlRoom:
    def __init__(self, workspace, registry=None):
        home = Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc")
        self.registry = Path(registry) if registry else home / "ui-workspaces.json"
        self.lock = threading.RLock()
        self.workspaces = {}
        for path in read_json(self.registry, {}).get("paths", []):
            if Path(path).is_dir():
                self.add_workspace(path, save=False)
        self.default = self.add_workspace(str(workspace), save=False)["id"]
        self.children = []

    def add_workspace(self, path, save=True):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Enter a workspace directory")
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Workspace directory does not exist")
        key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        with self.lock:
            self.workspaces[key] = root
            if save:
                atomic_json(self.registry, {"paths": [str(p) for p in self.workspaces.values()]})
        return {"id": key, "name": root.name, "path": str(root)}

    def workspace(self, key=None):
        try:
            return self.workspaces[key or self.default]
        except KeyError:
            raise ValueError("Unknown workspace; add it using the workspace switcher")

    def config(self, workspace):
        try:
            value, source = core.load_config(workspace)
            options = config_for(value)
        except SystemExit as exc:
            raise ValueError(str(exc))
        calibration = DecisionEngine(workspace, value).calibration()
        local = workspace / ".fusion.json"
        orc = workspace / ".orc.json"
        return {"local": redact(read_json(local)), "effective": redact(value), "source": str(source) if source else "Built-in defaults",
                "revision": revision(local), "orc": redact(read_json(orc)), "orc_revision": revision(orc),
                "mode": options["mode"], "execution_mode": core.execution_mode(value),
                "publish": publishing.git_options(workspace, value),
                "qualified_buckets": sum(bool(b.get("qualified")) for b in calibration.get("buckets", {}).values()),
                "environment": {key: os.environ[key] for key in ("FUSION_DECISIONS_MODE", "FUSION_TELEMETRY", "FUSION_CONFIG") if key in os.environ},
                "workers": [core.worker_availability(value, name)
                            for name in ("codex", "claude", "agy", "grok")]}

    def save_config(self, workspace, body):
        filename = ".orc.json" if body.get("target") == "orc" else ".fusion.json"
        path = inside(workspace, filename)
        value = body.get("value")
        if not isinstance(value, dict):
            raise ValueError("Settings must be a JSON object")
        with self.lock:
            if body.get("revision") != revision(path):
                raise ValueError("Settings changed on disk. Reload before saving to preserve those edits.")
            old = read_json(path)
            value = restore_secrets(value, old)
            if filename == ".fusion.json":
                merged = core.deep_merge(core.DEFAULTS, value)
                core.execution_mode(merged)
                publishing.options(merged)
                config_for(merged)
                if not isinstance(merged.get("routes"), dict):
                    raise ValueError("routes must be a JSON object")
                for name in ("codex", "claude", "agy", "grok", "telemetry", "decisions"):
                    if not isinstance(merged.get(name), dict):
                        raise ValueError(f"{name} must be a JSON object")
            atomic_json(path, value)
        return self.config(workspace)

    def jobs(self, workspace):
        results = []
        for path in (workspace / ".fusion/ui/jobs").glob("*/job.json"):
            job = read_json(path)
            if not job:
                continue
            if job.get("status") in ACTIVE and job.get("supervisor_pid") and not alive(job["supervisor_pid"]):
                job["status"] = "interrupted"
            result = job.pop("result", None)
            if isinstance(result, dict):
                job["workflow_id"] = job.get("workflow_id") or result.get("workflow_id")
            results.append(job)
        return sorted(results, key=lambda row: row.get("started_at_ms", 0), reverse=True)[:100]

    def overview(self, workspace):
        workflows = []
        for path in (workspace / ".fusion/workflows").glob("*/manifest.json"):
            manifest = read_json(path)
            if not manifest:
                continue
            nodes = list((manifest.get("nodes") or {}).values())
            status = manifest.get("status")
            if status == "running" and manifest.get("coordinator_pid") and not alive(manifest["coordinator_pid"]):
                status = "interrupted"
            workflows.append({"id": path.parent.name, "task": manifest.get("task", "Untitled workflow"), "status": status,
                              "started_at_ms": manifest.get("started_at_ms", path.stat().st_mtime * 1000),
                              "completed": sum(n.get("status") == "success" for n in nodes), "total": len(nodes),
                              "read_only": all(not n.get("write") for n in nodes), "spent_usd": manifest.get("spent_usd"),
                              "agents": sorted({(n.get("result") or {}).get("agent", n.get("agent", "auto")) for n in nodes})})
        workflows.sort(key=lambda row: row["started_at_ms"], reverse=True)
        builds = []
        for path in (workspace / ".fusion/builds").glob("*/status.json"):
            state = read_json(path)
            if state and not state.get("workflow_id"):
                if state.get("status") == "running" and state.get("coordinator_pid") and not alive(state["coordinator_pid"]):
                    state["status"] = "interrupted"
                builds.append(state)
        spans = core.RunStore(workspace).traces(10000)
        worker_spans = [span for span in spans if span.get("agent") != "gate" and span.get("status") != "cache_hit"]
        return {"workflows": workflows[:200], "builds": sorted(builds, key=lambda b: b.get("started_at_ms", 0), reverse=True)[:30],
                "jobs": self.jobs(workspace), "usage": core.usage_summary(spans),
                "cost": {"calls": len(worker_spans), "reported_calls": sum(reported_cost(span.get("usage")) is not None for span in worker_spans)},
                "now_ms": core.now_ms()}

    def workflow(self, workspace, run_id):
        run_id = identifier(run_id)
        manifest_path = inside(workspace / ".fusion", f"workflows/{run_id}/manifest.json")
        if not manifest_path.is_file():
            raise ValueError("Workflow does not exist")
        manifest = workflow_status(workspace, run_id)
        report = workflow_report(workspace, run_id)
        nodes = []
        saved_nodes = manifest.get("nodes", {})
        order = [n["id"] for n in manifest.get("spec", {}).get("graph", {}).get("nodes", [])]
        for key in dict.fromkeys([*order, *saved_nodes]):
            node = saved_nodes[key]
            key = identifier(key)
            current = dict(node, id=key)
            if node.get("status") == "paused_quota":
                result = node.get("result") or {}
                current["quota"] = {"agent": result.get("agent", node.get("agent")),
                                    "route": result.get("route") or "native", "message": result.get("summary", "Provider quota exhausted")}
            result = node.get("result") or {}
            if core.failure_class(result) == "permission_denied":
                current["permission_failure"] = {
                    "agent": result.get("agent", node.get("agent")),
                    "message": next((b for b in result.get("blockers", []) if "denied" in str(b).lower()), "Worker permission denied"),
                }
            active = read_json(inside(workspace / ".fusion", f"workflows/{run_id}/nodes/{key}/active.json"))
            worker_id = active.get("run_id") or (node.get("result") or {}).get("run_id")
            current["messages"] = []
            if worker_id:
                directory = inside(workspace / ".fusion", "runs/" + identifier(worker_id))
                current["activity"] = read_json(directory / "activity.json")
                task = read_json(directory / "task.json")
                current["agent"] = task.get("agent", current.get("agent"))
                for line in tail(directory / "stdout.log").splitlines():
                    message = core.progress.worker_message(line)
                    if message:
                        current["messages"].append(message)
                current["messages"] = current["messages"][-60:]
                current["stderr"] = tail(directory / "stderr.log", 12000)
            nodes.append(current)
        if report["status"] == "running" and manifest.get("coordinator_pid") and not alive(manifest["coordinator_pid"]):
            report["status"] = "interrupted"
        report["live_nodes"] = nodes
        report["started_at_ms"] = manifest.get("started_at_ms")
        report["spec"] = manifest.get("spec")
        report["events"] = read_jsonl(manifest_path.with_name("events.jsonl"))[-150:]
        report["markdown"] = format_report(select_report(report, all_nodes=True))
        return report

    def job(self, workspace, job_id):
        directory = inside(workspace / ".fusion", "ui/jobs/" + identifier(job_id))
        if not (directory / "job.json").exists():
            raise ValueError("Job does not exist")
        job = read_json(directory / "job.json")
        job["console"] = tail(directory / "stderr.log", 60000)
        job["output"] = tail(directory / "stdout.log", 80000)
        if job.get("status") in ACTIVE and job.get("supervisor_pid") and not alive(job["supervisor_pid"]):
            job["status"] = "interrupted"
        return job

    def launch(self, workspace, body):
        action = body.get("action", "build")
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        directory = inside(workspace / ".fusion", "ui/jobs/" + job_id)
        argv = [sys.executable, str(Path(__file__).with_name("fusion")), "--workspace", str(workspace), "--json", "--progress"]
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > 50000:
            raise ValueError("Request must be text, at most 50,000 characters")
        mode = body.get("mode") or None
        if mode not in {None, "off", "shadow", "active"}:
            raise ValueError("Invalid Laya mode")
        writes = False
        spec = None
        if action == "build":
            kind = body.get("kind", "discovery")
            if kind not in {"discovery", "review", "build", "debug"}:
                raise ValueError("Choose discovery, review, build or debug")
            budget, attempts = float(body.get("budget", 0)), int(body.get("attempts", 2))
            if not math.isfinite(budget) or budget < 0 or not 1 <= attempts <= 5:
                raise ValueError("Budget must be nonnegative; attempts must be between 1 and 5")
            prepare = body.get("prepare") is True
            argv += ["build", "--kind", kind, "--plan-only" if prepare else "--execute", "--budget-usd", str(budget), "--max-attempts", str(attempts)]
            if kind in {"build", "debug"} and body.get("publish") is not None:
                config, _ = core.load_config(workspace)
                publish_options = publishing.options(config, body["publish"])
                argv += ["--publish", publish_options["mode"], "--base", publish_options["base"], "--remote", publish_options["remote"], "--draft" if publish_options["draft"] else "--ready"]
            if body.get("from_workflow"):
                run_id = identifier(body["from_workflow"])
                finding = int(body.get("finding", 0))
                node = body.get("from_node") or None
                finding_request(workspace, run_id, finding, node)
                argv += ["--from-workflow", run_id, "--finding", str(finding)]
                if node:
                    argv += ["--from-node", identifier(node)]
            elif not text.strip():
                raise ValueError("Describe the task or paste a GitHub issue URL")
            argv += ["--", text]
            writes = kind in {"build", "debug"} and not prepare
        elif action == "publish":
            run_id = identifier(body.get("run_id"))
            publishing.eligible(workflow_status(workspace, run_id))
            if not isinstance(body.get("snapshot_id"), str):
                raise ValueError("Preview the PR before publishing")
            argv += ["workflow", "publish", run_id, "--request", str(directory / "publication-request.json")]
        elif action == "delegate":
            agent = body.get("agent", "auto")
            if agent not in {"auto", "codex", "claude", "agy", "grok"} or not text.strip():
                raise ValueError("Choose a worker and describe its task")
            role = body.get("role", "review")
            if role not in {"review", "discovery", "planning", "implementation"}:
                raise ValueError("Invalid worker role")
            argv += ["delegate", "--agent", agent, "--role", role, "--fresh"]
            writes = body.get("allow_write") is True
            if not writes:
                argv += ["--read-only"]
            if body.get("route"):
                config, _ = core.load_config(workspace)
                if body["route"] not in config.get("routes", {}):
                    raise ValueError("Unknown route")
                argv += ["--route", body["route"]]
            argv += ["--", text]
        elif action == "resume":
            run_id = identifier(body.get("run_id"))
            manifest = workflow_status(workspace, run_id)
            if manifest.get("status") == "running" and alive(manifest.get("coordinator_pid")):
                raise ValueError("This workflow is already running")
            writes = any(n.get("write") for n in manifest.get("nodes", {}).values())
            argv += ["workflow", "resume", run_id]
            if any(body.get(key) is not None for key in ("node", "agent", "route", "max_attempts")):
                from fusion_workflow import reroute_resume_spec
                config, _ = core.load_config(workspace)
                limit = int(body["max_attempts"]) if body.get("max_attempts") is not None else None
                reroute_resume_spec(manifest, config, body.get("node"), body.get("agent"), body.get("route"), limit)
                for key in ("node", "agent", "route"):
                    if body.get(key):
                        argv += ["--" + key, body[key]]
                if limit is not None:
                    argv += ["--max-attempts", str(limit)]
        elif action == "workflow":
            spec = validate_spec(body.get("spec"))
            writes = any(n.get("write") for n in spec["graph"]["nodes"])
            argv += ["workflow", "run", str(directory / "workflow.json")]
        elif action == "probe":
            kind = body.get("kind", "intake")
            if kind not in {"intake", "review", "acceptance", "recovery"} or not text.strip():
                raise ValueError("Choose a classifier and provide input")
            argv += ["decisions", "probe", "--kind", kind, "--", text]
        elif action == "suggest-labels":
            from fusion_labeling import labelable
            labelable(DecisionStore(workspace), body.get("decision_id"))
            agent = body.get("agent", "auto")
            if agent not in {"auto", "codex", "claude", "agy", "grok"}:
                raise ValueError("Choose a labeling worker")
            argv += ["decisions", "suggest", "--agent", agent, "--", body["decision_id"]]
            mode = "off"
        elif action == "setup":
            checkpoint = body.get("checkpoint", "english")
            if checkpoint not in {"english", "multilingual", "typed-decisions"}:
                raise ValueError("Invalid checkpoint")
            argv += ["decisions", "setup", "--checkpoint", checkpoint]
        elif action in {"export", "calibrate", "train", "evaluate"}:
            argv += ["decisions", action]
            if action != "export":
                dataset = inside(workspace / ".fusion", body.get("dataset", ""))
                if not dataset.is_file():
                    raise ValueError("Choose an existing dataset inside this workspace's .fusion directory")
                argv += [str(dataset)]
            destination = directory / ("candidate" if action == "train" else "dataset.jsonl" if action in {"export", "evaluate"} else "calibration.json")
            argv += [str(destination)]
            if action == "evaluate":
                argv += ["--control"]
            if action in {"train", "evaluate"} and body.get("model_path"):
                argv += ["--model-path", str(inside(workspace / ".fusion", body["model_path"]))]
        else:
            raise ValueError("Unsupported action")
        if writes and body.get("allow_write") is not True:
            raise ValueError("This workflow includes implementation. Enable workspace edits before launching.")
        with self.lock:
            if action == "publish" and any(j["status"] in ACTIVE and j["action"] == "publish" for j in self.jobs(workspace)):
                raise ValueError("A publication job is already active in this workspace")
            if action == "suggest-labels" and any(j["status"] in ACTIVE and j.get("decision_id") == body["decision_id"] for j in self.jobs(workspace)):
                raise ValueError("Labels are already being drafted for this decision")
            if action in {"build", "delegate", "resume", "workflow"} and any(j["status"] in ACTIVE and j["action"] in {"build", "delegate", "resume", "workflow"} for j in self.jobs(workspace)):
                raise ValueError("A UI workflow is already active in this workspace. Follow or stop it before launching another.")
            directory.mkdir(parents=True, mode=0o700)
            if spec:
                atomic_json(directory / "workflow.json", spec)
            if action == "publish":
                atomic_json(directory / "publication-request.json", {k: body[k] for k in ("snapshot_id", "title", "body", "draft", "accept_legacy_diff") if k in body})
            title = text[:200] or (f"Resume {body.get('run_id')}" if action == "resume" else f"{action.title()} · {body.get('kind', 'Laya')}")
            decision_id = body.get("decision_id") if action == "suggest-labels" else None
            atomic_json(directory / "request.json", {"action": action, "title": title, "argv": argv, "workspace": str(workspace), "mode": mode, "decision_id": decision_id})
            atomic_json(directory / "job.json", {"id": job_id, "action": action, "title": title, "status": "queued", "started_at_ms": core.now_ms(), "decision_id": decision_id})
            with (directory / "supervisor.log").open("wb") as log:
                proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--job", str(directory)],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            self.children = [child for child in self.children if child.poll() is None] + [proc]
        return self.job(workspace, job_id)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, app):
        self.app = app
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.url = self.origin + "/#token=" + self.token


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Task text and capability credentials do not belong in access logs.

    def send(self, status, value, content_type="application/json"):
        raw = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected or self.headers.get("Origin", self.server.origin) != self.server.origin:
            self.send(403, {"error": "Use the local URL printed by orc fusion ui"})
            return False
        if not secrets.compare_digest(self.headers.get("X-Fusion-Token", "").encode(), self.server.token.encode()):
            self.send(401, {"error": "Open the full local URL from your terminal to connect this browser"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            try:
                file = inside(ASSETS, "index.html" if path == "/" else path.lstrip("/"))
                if not file.is_file():
                    raise ValueError("Not found")
                self.send(200, file.read_bytes(), mimetypes.guess_type(str(file))[0] or "application/octet-stream")
            except (ValueError, OSError):
                self.send(404, {"error": "Not found"})
            return
        if not self.authorized():
            return
        query = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}
        app = self.server.app
        try:
            workspace = app.workspace(query.get("w"))
            if path == "/api/bootstrap":
                result = {"default": app.default, "workspaces": [{"id": key, "name": p.name, "path": str(p)} for key, p in app.workspaces.items()]}
            elif path == "/api/overview":
                result = app.overview(workspace)
            elif path == "/api/workflow":
                result = app.workflow(workspace, query.get("id"))
            elif path == "/api/job":
                result = app.job(workspace, query.get("id"))
            elif path == "/api/config":
                result = app.config(workspace)
            elif path == "/api/decisions":
                store = DecisionStore(workspace)
                events = read_jsonl(store.path)
                records = store.records()[-200:][::-1]
                for record in records:
                    record["applications"] = [e for e in events if e.get("id") == record["id"] and e.get("event") == "application"]
                    record["labels"] = [e for e in events if e.get("id") == record["id"] and e.get("event") == "label"]
                    record["suggestions"] = [e for e in events if e.get("id") == record["id"] and e.get("event") == "label_suggestion"][-5:]
                jobs = app.jobs(workspace)
                for record in records:
                    record["suggestion_job"] = next((j for j in jobs if j.get("decision_id") == record["id"]), None)
                result = {"records": records}
            elif path == "/api/file":
                file = inside(workspace, query.get("path", ""))
                relative = file.relative_to(workspace)
                if any(part.startswith(".") and part != ".fusion" for part in relative.parts) or file.suffix not in {".md", ".txt", ".json", ".jsonl", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".sh", ".yaml", ".yml", ".css", ".html", ".toml", ".sql"}:
                    raise ValueError("This file is not a supported source or report artifact")
                if not file.is_file() or file.stat().st_size > 2_000_000:
                    raise ValueError("File is missing or larger than 2 MB")
                result = {"path": str(file), "text": file.read_text(errors="replace")}
            elif path == "/api/orc":
                command = query.get("command", "status")
                arguments = {"status": ["status", "--json"], "models": ["models", "--tools"], "free": ["models", "--free", "--tools"], "profiles": ["profiles"], "quality": ["quality"]}.get(command)
                if arguments is None:
                    raise ValueError("Unsupported ORC view")
                orc = str(Path(__file__).with_name("orc"))
                proc = subprocess.run([orc, *arguments], cwd=workspace, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                      env={**os.environ, "NO_COLOR": "1"})
                result = {"text": terminal_text(proc.stdout), "error": terminal_text(proc.stderr), "exit_code": proc.returncode}
            else:
                self.send(404, {"error": "Unknown API route"})
                return
            self.send(200, result)
        except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            self.send(400, {"error": str(exc)})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_000_000 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Send a JSON request smaller than 1 MB")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            app = self.server.app
            workspace = app.workspace(body.get("workspace"))
            path = urlsplit(self.path).path
            if path == "/api/workspaces":
                result = app.add_workspace(body.get("path", ""))
            elif path == "/api/config":
                result = app.save_config(workspace, body)
            elif path == "/api/publish-preview":
                config, _ = core.load_config(workspace)
                result = publishing.preview(workspace, identifier(body.get("run_id")), config, body.get("publish"))
            elif path == "/api/pr-refresh":
                result = publishing.refresh_pr(workspace, identifier(body.get("run_id")))
            elif path == "/api/launch":
                result = app.launch(workspace, body)
            elif path == "/api/cancel":
                job = app.job(workspace, body.get("id"))
                if job["status"] not in ACTIVE:
                    raise ValueError("This job is no longer running")
                path = inside(workspace / ".fusion", "ui/jobs/" + identifier(body["id"]) + "/cancel")
                path.touch(mode=0o600)
                result = {"status": "stopping"}
            elif path == "/api/label":
                if body.get("suggestion_id") and body.get("approved") is not True:
                    raise ValueError("Suggested labels require explicit human approval")
                DecisionStore(workspace).label(body.get("id"), body.get("answers", {}), body.get("evidence", ""), body.get("suggestion_id"), replace=True)
                result = {"saved": True}
            else:
                self.send(404, {"error": "Unknown API route"})
                return
            self.send(200, result)
        except (ValueError, OSError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
            self.send(400, {"error": str(exc)})


def serve(workspace, port=8765, open_browser=True):
    if not 0 <= port <= 65535:
        print("fusion ui: port must be between 0 and 65535", file=sys.stderr)
        return 1
    app = ControlRoom(workspace)
    try:
        server = Server(port, app)
    except OSError as exc:
        print(f"fusion ui: {exc}. Choose another port with ui --port 8766.", file=sys.stderr)
        return 1
    print(f"\nORC control room\n{server.url}\nWorkspace: {workspace}\nCtrl-C closes the server; launched workflows continue.\n", flush=True)
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--job":
        raise SystemExit(run_job(sys.argv[2]))
