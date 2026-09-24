"""Resumable Git publication. No model runs, implicit staging, or force pushes."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

import fusion_progress as progress

DEFAULTS = {"mode": "off", "base": "staging", "remote": "origin", "draft": True}
EXCLUDED = {".fusion", ".git", ".fusion.json", ".orc.json"}


def read(path):
    return json.loads(Path(path).read_text()) if Path(path).is_file() else {}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + uuid.uuid4().hex)
    with open(temporary, "x", opener=lambda p, f: os.open(p, f, 0o600)) as out:
        json.dump(value, out, indent=2, ensure_ascii=False)
    temporary.replace(path)


def options(config, overrides=None):
    value = {**DEFAULTS, **(config.get("publish") or {}), **(overrides or {})}
    if value["mode"] not in {"off", "manual", "auto"} or not isinstance(value["draft"], bool):
        raise ValueError("publish.mode must be off, manual or auto; publish.draft must be boolean")
    for key in ("base", "remote"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value[key]) or ".." in value[key]:
            raise ValueError(f"Invalid publish.{key}")
    return {key: value[key] for key in DEFAULTS}


def run(argv, cwd, *, data=None, env=None, timeout=120):
    completed = subprocess.run(argv, cwd=cwd, input=data, capture_output=True,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}, timeout=timeout)
    if completed.returncode:
        message = completed.stderr.decode(errors="replace").strip() or completed.stdout.decode(errors="replace").strip()
        raise ValueError(f"{argv[0]} {argv[1]} failed: {message[:2000]}")
    return completed.stdout


def git(workspace, *args, **kwargs):
    return run(["git", "-c", "core.fsmonitor=false", *args], workspace, **kwargs)


def text(workspace, *args):
    return git(workspace, *args).decode().strip()


def root_for(workspace, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid workflow identifier")
    root = (Path(workspace) / ".fusion/workflows" / run_id).resolve()
    if not root.is_relative_to((Path(workspace) / ".fusion").resolve()):
        raise ValueError("Workflow path escapes workspace")
    return root


def allowed_path(path):
    p = Path(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and not (set(p.parts) & EXCLUDED)


def snapshot(workspace, paths=None):
    """Create a tree from disk using a private index, preserving the user's index."""
    with tempfile.TemporaryDirectory(prefix="fusion-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index"), "GIT_LITERAL_PATHSPECS": "1"}
        git(workspace, "read-tree", "HEAD", env=env)
        head = set(git(workspace, "ls-tree", "-r", "--name-only", "-z", "HEAD").decode().split("\0"))
        indexed = set(git(workspace, "ls-files", "--cached", "-z").decode().split("\0"))
        untracked = set(git(workspace, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0"))
        selected = {p for p in head | indexed | untracked if allowed_path(p)}
        if paths is not None:
            scopes = {p.rstrip('/') for p in paths if allowed_path(p)}
            selected = {p for p in selected if any(p == s or p.startswith(s + '/') for s in scopes)}
        # Update HEAD-tracked files without re-adding their ignored parent directories.
        # Newly indexed files express explicit user intent, even if now ignored;
        # only those exact paths may be forced. New untracked files honor ignore rules.
        indexed_new = {p for p in selected & (indexed - head) if os.path.lexists(Path(workspace) / p)}
        for args, group in ((["add", "-u"], selected & head),
                            (["add", "-f"], indexed_new),
                            (["add"], selected & (untracked - head - indexed))):
            files = sorted(group)
            for offset in range(0, len(files), 200):
                git(workspace, *args, "--", *files[offset:offset + 200], env=env)
        return git(workspace, "write-tree", env=env).decode().strip()


def repo_for(workspace, remote):
    configured = subprocess.run(["git", "config", "--get", f"remote.{remote}.url"],
                                cwd=workspace, capture_output=True, text=True, timeout=5)
    if configured.returncode == 1:
        raise ValueError(f"No Git remote named '{remote}' in this workspace. Choose an existing GitHub remote or configure one first.")
    if configured.returncode:
        raise ValueError("Git could not read this workspace's remote configuration")
    raw = configured.stdout.strip()
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", raw)
    if not match:
        raise ValueError("Publishing requires a GitHub remote (SSH or HTTPS)")
    push = subprocess.run(["git", "config", "--get-all", f"remote.{remote}.pushurl"], cwd=workspace, capture_output=True, text=True)
    if push.stdout.strip() and push.stdout.strip() != raw:
        raise ValueError("Remote has a different push URL; use a remote with matching fetch and push destinations")
    return match[1], raw


def fetch_base(workspace, opts):
    git(workspace, "check-ref-format", "refs/heads/" + opts["base"])
    repo, url = repo_for(workspace, opts["remote"])
    git(workspace, "fetch", "--no-tags", opts["remote"],
        f"+refs/heads/{opts['base']}:refs/remotes/{opts['remote']}/{opts['base']}")
    return repo, url, text(workspace, "rev-parse", f"refs/remotes/{opts['remote']}/{opts['base']}")


def branch_name(task, run_id):
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40].rstrip("-") or "task"
    return f"fusion/{slug}-{run_id}"


def setup_worktree(workspace, run_id, task, opts):
    root = root_for(workspace, run_id)
    repo, url, base_sha = fetch_base(workspace, opts)
    branch = branch_name(task, run_id)
    directory = Path(workspace).resolve() / ".fusion/worktrees" / run_id
    directory.parent.mkdir(parents=True, exist_ok=True)
    git(workspace, "worktree", "add", "-b", branch, str(directory), base_sha)
    if (directory / ".fusion").exists():
        raise ValueError("Repository tracks .fusion; cannot share workflow artifacts")
    (directory / ".fusion").symlink_to((Path(workspace) / ".fusion").resolve(), target_is_directory=True)
    context = {**opts, "repo": repo, "remote_url": url, "base_sha": base_sha,
               "branch": branch, "workspace": str(directory), "isolated": True}
    save(root / "git.json", context)
    return context


def eligible(manifest):
    nodes = manifest.get("nodes", {})
    if manifest.get("status") != "success" or not nodes or any(n.get("status") != "success" for n in nodes.values()):
        raise ValueError("Publishing requires a successfully completed workflow")
    writers = {key for key, n in nodes.items() if n.get("write")}
    if not writers:
        raise ValueError("This workflow contains no implementation")
    def ancestors(key):
        seen, pending = set(), list(nodes[key].get("needs", []))
        while pending:
            dep = pending.pop()
            if dep in seen:
                continue
            seen.add(dep)
            pending.extend(nodes.get(dep, {}).get("needs", []))
        return seen
    reviews = [n for key, n in nodes.items() if not n.get("write") and "review" in n.get("role", key).lower()
               and writers <= ancestors(key)]
    if not reviews:
        raise ValueError("An accepted review downstream of every implementation stage is required")
    return reviews


def description(manifest, files, legacy):
    lines = ["## Summary", manifest.get("task", "Completed Fusion workflow").splitlines()[0], "", "## Changes"]
    lines.extend(f"- `{p}`" for p in files)
    lines += ["", "## Validation", "Recorded workflow evidence:"]
    for key, node in manifest["nodes"].items():
        result = node.get("result") or {}
        if node.get("write") or "review" in node.get("role", key):
            lines.append(f"- **{key} ({result.get('agent', node.get('agent'))})**: {result.get('summary') or 'Accepted'}")
            lines.extend(f"  - {check}" for check in result.get("tests", []))
    if legacy:
        lines += ["", "The workflow predates Git snapshots. The publisher reviewed the final diff before publication."]
    lines += ["", "## CI", "GitHub checks have not been observed yet. See the PR checks for current status.", "", f"Fusion workflow: `{manifest['workflow_id']}`"]
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "publish.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Publication is already running for this workflow")
        yield


def preview(workspace, run_id, config, overrides=None):
    root = root_for(workspace, run_id)
    with lock(root):
        manifest = read(root / "manifest.json")
        reviews = eligible(manifest)
        prior = read(root / "publish.json")
        if prior.get("url"):
            return prior
        context = read(root / "git.json")
        opts = options(config, {**{k: prior[k] for k in DEFAULTS if k in prior},
                                **{k: context[k] for k in DEFAULTS if k in context}, **(overrides or {})})
        if prior.get("commit") and any(opts[k] != prior[k] for k in ("remote", "base")):
            raise ValueError("Publication already committed against a different target; keep its saved base and remote")
        if context and any(opts[k] != context[k] for k in ("remote", "base")):
            raise ValueError("This run was reviewed against a different target. Keep its saved base and remote.")
        repo, url, remote_sha = fetch_base(workspace, opts)
        source = Path(context.get("workspace", workspace))
        base_sha = context.get("base_sha") or text(source, "rev-parse", "HEAD")
        git(workspace, "merge-base", "--is-ancestor", base_sha, remote_sha)
        paths = None if context else sorted({p for n in manifest["nodes"].values() if n.get("write")
                                            for p in (n.get("result") or {}).get("changed", []) if allowed_path(p)})
        if paths == []:
            raise ValueError("No implementation paths recorded; cannot identify this older run's changes")
        tree = snapshot(source, paths)
        reviewed = {n.get("result", {}).get("reviewed_tree") for n in reviews} - {None}
        legacy = not reviewed
        if context and (legacy or tree not in reviewed):
            raise ValueError("The current files differ from the accepted review. Run a new review before publishing.")
        if prior.get("commit") and (tree != prior.get("tree") or base_sha != prior.get("base_sha")):
            raise ValueError("Files changed after publication began; finish or inspect the existing publication first")
        diff = git(workspace, "diff", "--binary", "--full-index", base_sha, tree)
        files = git(workspace, "diff", "--name-only", "-z", base_sha, tree).decode().strip("\0").split("\0")
        if not diff or any(not allowed_path(p) for p in files):
            raise ValueError("No publishable changes, or the diff includes Fusion state")
        if len(diff) > 8_000_000:
            raise ValueError("Diff exceeds the 8 MB publication preview limit")
        state = {**prior, **opts, "workflow_id": run_id, "repo": repo, "remote_url": url,
                 "base_sha": base_sha, "target_sha": remote_sha, "tree": tree,
                 "source": str(source), "paths": paths, "legacy": legacy, "files": files,
                 "branch": context.get("branch") or branch_name(manifest.get("task", ""), run_id),
                 "worktree": context.get("workspace") or str(Path(workspace).resolve() / ".fusion/publish-worktrees" / run_id),
                 "title": prior.get("title") or manifest.get("task", "Fusion implementation").splitlines()[0][:180],
                 "body": prior.get("body") or description(manifest, files, legacy),
                 "diff": diff.decode(errors="replace"), "status": "preview", "error": None,
                 "updated_at_ms": int(time.time() * 1000)}
        state["snapshot_id"] = hashlib.sha256(json.dumps({k: state[k] for k in ("repo", "remote_url", "base", "base_sha", "tree", "branch", "files")}, sort_keys=True).encode()).hexdigest()
        (root / "publish.patch").write_bytes(diff)
        save(root / "publish.json", state)
        return state


def public_status(workspace, run_id):
    value = read(root_for(workspace, run_id) / "publish.json")
    return {k: v for k, v in value.items() if k not in {"diff", "body", "paths"}}


def gh(workspace, *args):
    return run(["gh", *args], workspace).decode().strip()


def refresh_pr(workspace, run_id):
    root = root_for(workspace, run_id)
    with lock(root):
        state = read(root / "publish.json")
        if not state.get("url"):
            raise ValueError("No pull request has been published")
        info = json.loads(gh(workspace, "pr", "view", str(state["number"]), "--repo", state["repo"],
                             "--json", "url,state,isDraft,headRefOid,baseRefName,statusCheckRollup"))
        state.update(pr=info, checked_at_ms=int(time.time() * 1000))
        save(root / "publish.json", state)
        return state


def publish(workspace, run_id, config, request):
    root = root_for(workspace, run_id)
    if not request.get("snapshot_id"):
        prepared = preview(workspace, run_id, config, {k: request[k] for k in DEFAULTS if k in request})
        request = {**request, "snapshot_id": prepared.get("snapshot_id")}
    with lock(root):
        state = read(root / "publish.json")
        if state.get("url"):
            return state
        try:
            manifest = read(root / "manifest.json")
            eligible(manifest)
            if state.get("snapshot_id") != request["snapshot_id"]:
                raise ValueError("Preview changed. Review a fresh publication preview.")
            if state["legacy"] and request.get("accept_legacy_diff") is not True:
                raise ValueError("This older run has no Git review snapshot. Inspect the diff and confirm it before publishing.")
            repo, url = repo_for(workspace, state["remote"])
            if (repo, url) != (state["repo"], state["remote_url"]):
                raise ValueError("Git remote changed since preview")
            if snapshot(Path(state["source"]), state["paths"]) != state["tree"]:
                raise ValueError("Files changed since preview. Review a fresh diff before publishing.")
            state["title"] = request.get("title", state["title"])
            state["body"] = request.get("body", state["body"])
            state["draft"] = request.get("draft", state["draft"])
            if not isinstance(state["title"], str) or not state["title"].strip() or len(state["title"]) > 256:
                raise ValueError("PR title must be between 1 and 256 characters")
            if not isinstance(state["body"], str) or not state["body"].strip() or len(state["body"]) > 60000 or not isinstance(state["draft"], bool):
                raise ValueError("PR body must be nonempty and at most 60,000 characters; draft must be boolean")
            state.update(status="publishing", error=None)
            save(root / "publish.json", state)
            tree_dir = Path(state["worktree"])
            if not tree_dir.exists():
                progress.emit("publish", "creating feature branch and publication worktree")
                tree_dir.parent.mkdir(parents=True, exist_ok=True)
                # Save the owned branch before any mutation; retry never replaces an unrelated branch.
                git(workspace, "worktree", "add", "-b", state["branch"], str(tree_dir), state["base_sha"])
            if text(tree_dir, "branch", "--show-current") != state["branch"]:
                raise ValueError("Publication worktree is on a different branch")
            head = text(tree_dir, "rev-parse", "HEAD")
            if head == state["base_sha"]:
                if state["source"] != state["worktree"] and snapshot(tree_dir) == text(tree_dir, "rev-parse", "HEAD^{tree}"):
                    patch = git(workspace, "diff", "--binary", "--full-index", state["base_sha"], state["tree"])
                    git(tree_dir, "apply", "--binary", "-", data=patch)
                if snapshot(tree_dir) != state["tree"]:
                    raise ValueError("Publication worktree differs from the reviewed diff")
                progress.emit("publish", "committing the reviewed changes")
                git(tree_dir, "add", "-A", "--", *state["files"], env={"GIT_LITERAL_PATHSPECS": "1"})
                git(tree_dir, "commit", "-m", state["title"], timeout=600)
                head = text(tree_dir, "rev-parse", "HEAD")
            git(tree_dir, "merge-base", "--is-ancestor", state["base_sha"], head)
            if text(tree_dir, "rev-parse", "HEAD^{tree}") != state["tree"] or snapshot(tree_dir) != state["tree"]:
                raise ValueError("Commit or commit hooks changed the reviewed tree; publication stopped")
            state.update(commit=head, phase="committed")
            save(root / "publish.json", state)
            # Exact SHA refspec and normal fast-forward checks: no implicit ref or force push.
            progress.emit("publish", f"pushing {state['branch']}")
            git(tree_dir, "push", state["remote"], f"{head}:refs/heads/{state['branch']}")
            state["phase"] = "pushed"
            save(root / "publish.json", state)
            prs = json.loads(gh(workspace, "pr", "list", "--repo", repo, "--head", state["branch"], "--state", "all",
                                "--json", "number,url,baseRefName,headRefName,state,isCrossRepository,headRefOid"))
            prs = [pr for pr in prs if not pr.get("isCrossRepository")]
            matches = [pr for pr in prs if pr["headRefName"] == state["branch"] and pr["baseRefName"] == state["base"]]
            if len(matches) > 1 or (prs and not matches):
                raise ValueError("This branch already has conflicting PR history; inspect it before retrying")
            if matches:
                pr = matches[0]
                if pr.get("headRefOid") and pr["headRefOid"] != head:
                    raise ValueError("GitHub reports a different PR commit; refresh and retry publication")
            else:
                body_path = root / "pr-body.md"
                body_path.write_text(state["body"])
                progress.emit("publish", f"opening {'draft ' if state['draft'] else ''}PR against {state['base']}")
                gh(workspace, "pr", "create", "--repo", repo, "--base", state["base"], "--head", state["branch"],
                   "--title", state["title"], "--body-file", str(body_path), *(["--draft"] if state["draft"] else []))
                pr = json.loads(gh(workspace, "pr", "view", state["branch"], "--repo", repo, "--json", "number,url,state"))
            state.update(status="published", phase="published", url=pr["url"], number=pr["number"], error=None,
                         updated_at_ms=int(time.time() * 1000))
            save(root / "publish.json", state)
            progress.emit("publish", state["url"])
            return state
        except BaseException as exc:
            state.update(status="failed", error=str(exc) or type(exc).__name__, updated_at_ms=int(time.time() * 1000))
            save(root / "publish.json", state)
            raise


def auto_publish(workspace, run_id, config):
    try:
        publish(workspace, run_id, config, {})
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        root = root_for(workspace, run_id)
        state = read(root / "publish.json")
        state.update(status="failed", workflow_id=run_id, error=str(exc))
        save(root / "publish.json", state)
        progress.emit("publish", f"publication needs attention: {exc}; accepted stages remain saved")


def git_options(workspace, config):
    opts = options(config)
    try:
        remotes = text(workspace, "remote").splitlines()
        refs = text(workspace, "for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/{opts['remote']}").splitlines()
        return {**opts, "remotes": remotes, "branches": [r for r in refs if r != "HEAD"]}
    except ValueError:
        return {**opts, "remotes": [], "branches": []}


def add_parser(sub):
    parser = sub.add_parser("publish", help="preview or publish a completed workflow as a GitHub PR")
    parser.add_argument("run_id")
    parser.add_argument("--base")
    parser.add_argument("--remote")
    parser.add_argument("--title")
    parser.add_argument("--body-file")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--accept-legacy-diff", action="store_true")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--request", help="publication request JSON written by the control room")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--draft", dest="draft", action="store_true", default=None)
    kind.add_argument("--ready", dest="draft", action="store_false")


def command(workspace, config, args):
    request = read(args.request) if args.request else {k: getattr(args, k) for k in ("base", "remote", "title", "draft", "snapshot_id") if getattr(args, k) is not None}
    if args.accept_legacy_diff:
        request["accept_legacy_diff"] = True
    if args.body_file:
        request["body"] = Path(args.body_file).read_text()
    return preview(workspace, args.run_id, config, {k: request[k] for k in DEFAULTS if k in request}) if args.preview else publish(workspace, args.run_id, config, request)
