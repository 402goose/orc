"""Optional operator-authored product checkpoint, separate from run outcomes."""
import json
import os
from pathlib import Path
import re
from datetime import datetime


def product_progress(workspaces):
    path = Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc") / "product-progress.json"
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 100_000:
            raise ValueError("Product checkpoint exceeds 100 KB")
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("schema") != "fusion.product-progress.v1":
            raise ValueError("Unsupported product checkpoint schema")
        stamp = datetime.fromisoformat(value.get("updated_at", "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Product checkpoint requires a timezone")
        projects = value.get("projects")
        if not isinstance(projects, list) or not 1 <= len(projects) <= 12:
            raise ValueError("Product checkpoint requires 1–12 projects")
        seen = set()
        for project in projects:
            if not isinstance(project, dict):
                raise ValueError("Invalid project checkpoint")
            for field in ("id", "name", "phase", "working", "current", "next", "limits"):
                if not isinstance(project.get(field), str) or not 1 <= len(project[field]) <= 2000:
                    raise ValueError("Invalid project field: " + field)
            if project["id"] in seen:
                raise ValueError("Duplicate project checkpoint")
            seen.add(project["id"])
            refs = project.get("evidence", [])
            if not isinstance(refs, list) or len(refs) > 12:
                raise ValueError("Invalid project evidence references")
            for ref in refs:
                if (not isinstance(ref, dict) or ref.get("workspace_id") not in workspaces
                        or not isinstance(ref.get("run_id"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", ref["run_id"])
                        or not isinstance(ref.get("label"), str) or not 1 <= len(ref["label"]) <= 200):
                    raise ValueError("Project evidence must reference a registered workspace and run")
        milestones = value.get("milestones", [])
        if not isinstance(milestones, list) or len(milestones) > 8:
            raise ValueError("Invalid product milestones")
        for milestone in milestones:
            if (not isinstance(milestone, dict) or milestone.get("status") not in {"planned", "in_progress", "qualified"}
                    or any(not isinstance(milestone.get(field), str) or not 1 <= len(milestone[field]) <= 1000
                           for field in ("name", "exit"))):
                raise ValueError("Invalid milestone checkpoint")
        return {"status": "ready", "source": "operator_checkpoint", "updated_at": value["updated_at"],
                "projects": projects, "milestones": milestones,
                "note": "Coordinator checkpoint; live run evidence is shown separately."}
    except (ValueError, TypeError, OSError, AttributeError) as exc:
        return {"status": "unreadable", "source": "operator_checkpoint", "error": str(exc)}
