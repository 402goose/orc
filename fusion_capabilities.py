"""Local configuration and saved evidence, without probing accounts or running models."""
from pathlib import Path
import json
import shutil
import subprocess

import fusion_core as core
import fusion_publish as publishing
import fusion_training_loop as training
from fusion_decisions import DecisionStore, config_for, runtime_python
from fusion_learning import decision_rows


def github_source(workspace, config):
    remote = publishing.options(config)["remote"]
    result = {"status": "unavailable", "remote": remote, "repo": None,
              "cli_available": bool(shutil.which("gh")), "auth": "unchecked",
              "reason": "GitHub access is not checked by this local observation."}
    try:
        # Distinguish an absent source without returning Git output or remote URLs
        # (URLs can contain credentials). This never contacts the remote.
        probe = subprocess.run(["git", "-c", "core.fsmonitor=false", "rev-parse", "--git-dir"],
                               cwd=workspace, capture_output=True, timeout=5)
        if probe.returncode:
            result["reason"] = "This workspace is not a Git repository."
            return result
        probe = subprocess.run(["git", "config", "--get", f"remote.{remote}.url"],
                               cwd=workspace, capture_output=True, timeout=5)
        if probe.returncode == 1:
            result.update(status="missing_remote", reason=f"No {remote} GitHub remote is configured for this workspace.")
            return result
        if probe.returncode:
            result["reason"] = "Git could not read this workspace's remote configuration."
            return result
        try:
            repo, _ = publishing.repo_for(workspace, remote)
        except ValueError:
            result.update(status="unsupported_remote", reason="Choose a GitHub SSH or HTTPS remote with matching fetch and push destinations.")
            return result
        result.update(status="configured", repo=repo,
                      reason="Repository configured locally; sync open issues to check GitHub access."
                      if result["cli_available"] else "Repository configured, but the gh executable is unavailable to this server.")
    except (OSError, subprocess.SubprocessError):
        result["reason"] = "Git configuration could not be inspected."
    return result


def learning_evidence(app, workspace, options):
    source = DecisionStore(workspace).path
    if source.exists():
        for line in source.read_text().splitlines():
            if line.strip() and not isinstance(json.loads(line), dict):
                raise ValueError("Learning event must be an object")
    settings = training.root(workspace) / 'settings.json'
    if settings.exists():
        value = json.loads(settings.read_text())
        if (not isinstance(value, dict) or type(value.get('enabled', False)) is not bool
                or type(value.get('min_new_answers', 10)) is not int
                or not 1 <= value.get('min_new_answers', 10) <= 10000):
            raise ValueError("Invalid saved training settings")
    for path in (training.root(workspace) / 'rounds').glob('*/round.json'):
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get('status') not in {'running', 'needs_attention', 'complete'}:
            raise ValueError("Invalid saved training round")
    rows = decision_rows(workspace)
    loop = training.status(app, workspace, rows)
    observed = next((r for r in rows if r.get("status") == "ok" and r.get("model_identity")), {})
    latest = rows[0] if rows else {}
    python = runtime_python(options)
    available = bool(shutil.which(python))
    runtime = {"status": "observed" if observed else "unchecked", "python": python,
               "last_observed_at_ms": observed.get("time_ms"),
               "last_observed_identity": observed.get("model_identity"),
               "configured_model_path": options.get("model_path") or None,
               "latest_status": latest.get("status"), "latest_at_ms": latest.get("time_ms"),
               "reason": "A saved inference succeeded; current model loading and health are not checked."
               if observed else "No successful inference is recorded in this workspace. Model loading is not checked."}
    if not available:
        runtime.update(status="unavailable", reason="The configured Python executable is unavailable to this server.")
    elif latest and latest.get("status") != "ok":
        runtime["reason"] = "The latest saved inference did not succeed. Previous success does not establish current runtime health."

    return {"status": "observed", "scope": "workspace", "source_path": str(DecisionStore(workspace).path),
                         "mode": options["mode"], "runtime": runtime,
                         "labels": {"approved_answers": loop["approved_answers"],
                                    "train_groups": loop["groups"]["train"],
                                    "validation_groups": loop["groups"]["validation"],
                                    "min_train_groups": 2, "min_validation_groups": 2},
                         "training_enabled": loop["enabled"], "training_state": loop["state"],
                         "active_job": loop["active_job"], "reason": loop["reason"],
                         "qualification_note": "Group counts are prerequisites. Dataset curation, held-out evaluation and promotion remain separate gates."}


def capabilities(app, workspace):
    workspace = Path(workspace).resolve()
    try:
        config, _ = core.load_config(workspace)
    except SystemExit as exc:
        raise ValueError("Workspace configuration could not be read. Open Settings to inspect the configuration source.") from exc
    options = config_for(config)
    github = github_source(workspace, config)
    github["snapshot"] = None
    try:
        from fusion_truffle_survey import latest
        saved = latest(workspace)
        if saved:
            github["snapshot"] = {"id": saved.get("id"), "repo": saved.get("repo"),
                                  "complete": saved.get("inventory_complete") is True,
                                  "issue_count": len(saved.get("issues") or []),
                                  "synced_at_ms": saved.get("synced_at_ms"),
                                  "matches_remote": bool(github["repo"] and saved.get("repo") == github["repo"])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        github["snapshot_error"] = "The saved survey could not be read; GitHub was not contacted."

    try:
        learning = learning_evidence(app, workspace, options)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        reason = "Saved learning evidence could not be read. Counts and training readiness are unknown."
        learning = {"status": "unreadable", "scope": "workspace",
                    "source_path": str(DecisionStore(workspace).path), "mode": options["mode"],
                    "labels": None, "training_enabled": None, "training_state": "unknown",
                    "active_job": None, "runtime": {"status": "unchecked", "reason": reason}, "reason": reason}

    with app.lock:
        registered = list(app.workspaces.items())
    workspaces = []
    for key, path in registered:
        try:
            other_config, _ = core.load_config(path)
            source = github if path == workspace else github_source(path, other_config)
        except (SystemExit, ValueError, TypeError, OSError):
            source = {"repo": None, "status": "unavailable"}
        workspaces.append({"id": key, "name": path.name, "path": str(path),
                           "github_repo": source["repo"], "github_status": source["status"]})
    current = next(w for w in workspaces if w["path"] == str(workspace))
    return {"schema": "fusion.capabilities.v1", "observed_at_ms": core.now_ms(),
            "workspace": {k: current[k] for k in ("id", "name", "path")},
            "github": github, "workspaces": workspaces,
            "workers": [core.worker_availability(config, name) for name in ("codex", "claude", "agy", "grok")],
            "worker_note": "Executable and permission configuration only; account access and live harness health are not checked.",
            "learning": learning}
