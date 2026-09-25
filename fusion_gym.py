"""ORC gym: replay merged fix PRs as benchmark tasks, per lane.

`extract` turns a squash-merged PR (commit C, parent B) into a task in the
SWE-smith / SWE-bench shape: the task tree is B plus C's test-file changes,
FAIL_TO_PASS are the changed or new unittest tests that fail on the task tree
and pass on C, PASS_TO_PASS the other tests in those files that pass on both.
The task commit is written to `refs/gym/tasks/pr-N` in the source repository;
nothing else there changes (trees are materialized with `git archive`).

`run` gives each task to each lane in its own git worktree of a per-task
repository under the gym directory. That repository holds only the task
commit and its history, never C, so a worker cannot read the fix from git.
The worker runs as an authored single-node workflow whose acceptance checks
are the F2P and P2P commands with `acceptance.before: true`, so the gate
labels each run from the checks' before/after exit codes.

`report` reads the gym's results: per lane, per task, from receipts only.
"""
from __future__ import annotations

import ast
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from typing import Any

TASK_SCHEMA = "fusion.gym.task.v1"
RESULT_SCHEMA = "fusion.gym.result.v1"
REF_PREFIX = "refs/gym/tasks/"
TASK_REF = "refs/gym/task"
DEFAULT_TIMEOUT = 900
DEFAULT_P2P_LIMIT = 200
TEST_DIRS = {"test", "tests"}
# The interpreter the checks name. It is looked up on PATH, as the gate's
# acceptance checks are, so extraction and the gym run the same Python.
PYTHON = "python3"
# Lanes named on the command line. `gym.lanes` in the gym's .fusion.json adds
# or replaces entries; a configured route name is also a lane.
DEFAULT_LANES = {
    "claude-sonnet-high": {"agent": "claude", "model": "claude-sonnet-5", "reasoning_effort": "high"},
    "claude-opus-high": {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"},
    "claude-fable-medium": {"agent": "claude", "model": "claude-fable-5-1", "reasoning_effort": "medium"},
    "claude": {"agent": "claude"},
    "codex": {"agent": "codex"},
    "agy": {"agent": "agy"},
    "grok": {"agent": "grok"},
}
LANE_KEYS = {"agent", "route", "model", "reasoning_effort"}
# Run in each tree by `extract`: per-test outcomes for the given files, written
# as JSON to argv[1] so a test's own stdout cannot corrupt it.
DRIVER = r'''
import json, sys, unittest
from pathlib import Path
out = {}
class Result(unittest.TestResult):
    def addSuccess(self, test): out.setdefault(test.id(), "passed")
    def addFailure(self, test, err): out[test.id()] = "failed"
    def addError(self, test, err): out[test.id()] = "error"
    def addSkip(self, test, reason): out[test.id()] = "skipped"
    def addExpectedFailure(self, test, err): out[test.id()] = "skipped"
    def addUnexpectedSuccess(self, test): out[test.id()] = "failed"
    def addSubTest(self, test, subtest, err):
        if err is not None:
            out[test.id()] = "failed"
for relative in sys.argv[2:]:
    path = Path.cwd() / relative
    try:
        suite = unittest.TestLoader().discover(str(path.parent), pattern=path.name, top_level_dir=str(path.parent))
    except Exception:
        continue
    suite.run(Result())
Path(sys.argv[1]).write_text(json.dumps(out))
'''
_PR_SUBJECT = r"\(#{}\)\s*$"
# A PR body paragraph that starts describing the change. A bullet list is
# taken as a change list too: PR bodies list what they did, not the bug.
_FIX_CUE = re.compile(
    r"^\s*(?:[-*+]\s|\d+[.)]\s|(?:\*\*|__)?(?:this (?:pr|change|patch|commit)\b|the fix\b|fix(?:es|ed)?:|now\b|new\b|changes?\b|"
    r"validation\b|tests?\b|made with\b|\U0001F916|docs?\b|implementation\b|solution\b|approach\b|with this\b|"
    r"(?:adds?|added|introduces?|replaces?|removes?|moves?|renames?|refactors?|switch(?:es)?|makes?|uses?|"
    r"updates?|extends?|drops?|splits?|keeps?|stops?|reworks?)\b))",
    re.I)
# A line inside a kept paragraph that states the fix ("- Fix: ...").
_FIX_LINE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?(?:fix(?:es|ed)?|solution|resolution|the fix|now)\b\W", re.I)
_FIX_HEADING = re.compile(r"^#+\s*(?:fix|solution|proposal|proposed|implementation|plan|changes?|approach|validation|test)",
                          re.I | re.M)


# ---------------------------------------------------------------- git helpers

def git(repo, *args, env=None, binary=False, check=True):
    proc = subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=repo, capture_output=True,
                          env={**os.environ, **env} if env else None, stdin=subprocess.DEVNULL)
    if check and proc.returncode:
        raise ValueError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace').strip()[:600]}")
    return proc.stdout if binary else proc.stdout.decode(errors="replace")


def archive(repo, rev, destination):
    """Materialize a commit's tree without touching the repository's worktrees."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "--format=tar", rev, binary=True))) as tar:
        tar.extractall(destination, filter="data")
    return destination


def is_test_path(path):
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return bool(set(parts[:-1]) & TEST_DIRS) or bool(re.fullmatch(r"test_.*\.py|.*_test\.py", name))


def is_unittest_file(path):
    return bool(re.fullmatch(r"test_.*\.py|.*_test\.py", PurePosixPath(path).name))


# ---------------------------------------------------------------- PR text

def gh_json(repo, *args):
    proc = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    if proc.returncode:
        raise ValueError("GitHub read failed: " + (proc.stderr.strip() or "check gh auth status")[:600])
    return json.loads(proc.stdout)


def pr_info(repo, pr, gh_repo=None, use_gh=True):
    """Title, body, linked issues and merge commit of a PR, or {} without gh."""
    if not use_gh:
        return {}
    scope = ["-R", gh_repo] if gh_repo else []
    info = gh_json(repo, "pr", "view", str(pr), *scope, "--json", "title,body,mergeCommit,closingIssuesReferences,url")
    issues = []
    for reference in info.get("closingIssuesReferences") or []:
        owner = ((reference.get("repository") or {}).get("owner") or {}).get("login")
        name = (reference.get("repository") or {}).get("name")
        issue_scope = ["-R", f"{owner}/{name}"] if owner and name else scope
        issue = gh_json(repo, "issue", "view", str(reference["number"]), *issue_scope, "--json", "title,body,url")
        issues.append({"number": reference["number"], **issue})
    return {"title": info.get("title", ""), "body": info.get("body", ""), "url": info.get("url"),
            "merge_oid": (info.get("mergeCommit") or {}).get("oid"), "issues": issues}


def _unescape(text):
    return (text or "").replace("\\`", "`").replace("\r\n", "\n")


def _drop_fix_code(text):
    """Remove fenced code blocks that show a change (diffs, +/- lines)."""
    def keep(match):
        block = match.group(0)
        language = match.group(1).strip().lower()
        lines = block.splitlines()[1:-1]
        diff = language in {"diff", "patch"} or any(line.startswith(("diff --git", "@@", "+++", "---")) for line in lines)
        return "" if diff else block
    return re.sub(r"```([^\n]*)\n.*?\n```", keep, text, flags=re.S)


def strip_title(title):
    """`fix(scope): read labels` -> `read labels`."""
    return re.sub(r"^\s*[a-z]+(?:\([^)]*\))?!?:\s*", "", title or "").strip()


def problem_text(body):
    """The paragraphs of a PR body before it starts describing the fix."""
    body = _drop_fix_code(_unescape(body))
    heading = _FIX_HEADING.search(body)
    if heading:
        body = body[:heading.start()]
    body = re.sub(r"^\s*(?:fixes|closes|resolves)\s+#\d+\.?\s*", "", body.strip(), flags=re.I)
    kept = []
    for paragraph in re.split(r"\n\s*\n", body):
        text = paragraph.strip()
        if not text.startswith("```"):
            text = "\n".join(line for line in text.splitlines()
                             if not re.match(r"\s*#+\s", line) and not _FIX_LINE.match(line)).strip()
        if not text:
            continue
        first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        if not text.startswith("```") and (_FIX_CUE.match(text) or re.search(r"\bnow\b", first, re.I)):
            break
        kept.append(text)
    return "\n\n".join(kept).strip()


def task_prompt(info, fallback_subject):
    """(prompt, source). A linked issue states the problem without the fix;
    otherwise the PR title and the body's problem paragraphs."""
    if info.get("issues"):
        parts = []
        for issue in info["issues"]:
            body = _drop_fix_code(_unescape(issue.get("body")))
            heading = _FIX_HEADING.search(body)
            parts.append(f"{issue.get('title', '').strip()}\n\n{(body[:heading.start()] if heading else body).strip()}".strip())
        return "\n\n---\n\n".join(parts), "issue"
    if info.get("title"):
        problem = problem_text(info.get("body"))
        return (strip_title(info["title"]) + ("\n\n" + problem if problem else "")).strip(), "pr"
    return strip_title(re.sub(r"\s*\(#\d+\)(?:\s*\(#\d+\))*\s*$", "", fallback_subject)), "commit"


# ---------------------------------------------------------------- extraction

def succeeds(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, stdin=subprocess.DEVNULL).returncode == 0


def find_commit(repo, pr, ref, merge_oid=None):
    if merge_oid and succeeds(repo, "cat-file", "-e", merge_oid + "^{commit}") and \
            succeeds(repo, "merge-base", "--is-ancestor", merge_oid, ref):
        return merge_oid
    pattern = re.compile(_PR_SUBJECT.format(int(pr)))
    for line in git(repo, "log", "--format=%H%x00%s", ref).splitlines():
        sha, _, subject = line.partition("\0")
        if pattern.search(subject):
            return sha
    raise ValueError(f"no commit for PR #{pr} on {ref}")


def test_methods(source):
    """{Class.method: source} for unittest-style tests, plus per-class and
    module-level fixture code, so a changed helper marks its tests changed."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    tests, fixtures = {}, {"": []}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fixtures[node.name] = []
            for item in node.body:
                text = ast.get_source_segment(source, item) or ""
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    tests[f"{node.name}.{item.name}"] = text
                else:
                    fixtures[node.name].append(text)
        else:
            fixtures[""].append(ast.get_source_segment(source, node) or "")
    return tests, fixtures


def changed_tests(before, after, module):
    """Test ids in `after` that are new, changed, or depend on changed fixtures."""
    new = test_methods(after)
    if new is None:
        return set()
    tests, fixtures = new
    old = test_methods(before) if before is not None else None
    if old is None or old[1].get("") != fixtures.get(""):
        return {f"{module}.{name}" for name in tests}
    old_tests, old_fixtures = old
    return {f"{module}.{name}" for name, text in tests.items()
            if old_tests.get(name) != text or old_fixtures.get(name.split(".")[0]) != fixtures.get(name.split(".")[0])}


def run_tests(tree, files, timeout=DEFAULT_TIMEOUT):
    from fusion_verification import OFFLINE_ENV
    with tempfile.TemporaryDirectory(prefix="fusion-gym-") as temp:
        out = Path(temp) / "outcomes.json"
        try:
            subprocess.run([PYTHON, "-c", DRIVER, str(out), *files], cwd=tree, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=timeout, env={**os.environ, **OFFLINE_ENV})
        except subprocess.TimeoutExpired:
            return {}
        try:
            return json.loads(out.read_text())
        except (OSError, ValueError):
            return {}


def check_commands(ids, files):
    """[(argv, ids)]: one `python3 -m unittest discover` per test file.

    Test files import from the repository root via their own sys.path setup,
    so discovery starts in the file's directory; `-k '*module.Class.method'`
    is an fnmatch pattern anchored at the end of the full test name, so it
    selects exactly that test."""
    commands = []
    for path in files:
        module = PurePosixPath(path).stem
        selected = [test_id for test_id in ids if test_id.split(".")[0] == module]
        if not selected:
            continue
        directory = str(PurePosixPath(path).parent)
        argv = [PYTHON, "-m", "unittest", "discover", "-s", directory, "-t", directory, "-p", PurePosixPath(path).name]
        for test_id in selected:
            argv += ["-k", "*" + test_id]
        commands.append((argv, selected))
    return commands


def exit_code(tree, argv, timeout):
    from fusion_verification import OFFLINE_ENV
    try:
        return subprocess.run(argv, cwd=tree, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                              env={**os.environ, **OFFLINE_ENV}).returncode
    except subprocess.TimeoutExpired:
        return None


def make_task_commit(repo, base, fix, changes):
    """B plus C's test-file changes, as a commit object (no worktree touched).
    Author, committer and dates are fixed, so re-extraction gives the same sha.
    The message names no PR, so the worker cannot look the fix up from it."""
    with tempfile.TemporaryDirectory(prefix="fusion-gym-index-") as temp:
        env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
        git(repo, "read-tree", base, env=env)
        for status, path in changes:
            if status.startswith("D"):
                git(repo, "update-index", "--force-remove", "--", path, env=env)
            else:
                mode, _, rest = git(repo, "ls-tree", fix, "--", path).strip().partition(" ")
                sha = rest.split()[1]
                git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}", env=env)
        tree = git(repo, "write-tree", env=env).strip()
    date = git(repo, "show", "-s", "--format=%cI", base).strip()
    identity = {"GIT_AUTHOR_NAME": "orc gym", "GIT_AUTHOR_EMAIL": "gym@orc.invalid", "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_NAME": "orc gym", "GIT_COMMITTER_EMAIL": "gym@orc.invalid", "GIT_COMMITTER_DATE": date}
    return git(repo, "commit-tree", tree, "-p", base, "-m", "gym task: regression tests added", env=identity).strip()


def extract_one(repo, pr, ref="HEAD", gh_repo=None, use_gh=True, timeout=DEFAULT_TIMEOUT, p2p_limit=DEFAULT_P2P_LIMIT):
    """One task dict, or {"pr": N, "skipped": reason}."""
    repo = Path(repo).resolve()
    try:
        info = pr_info(repo, pr, gh_repo, use_gh)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        info = {"gh_error": str(exc)}
    fix = find_commit(repo, pr, ref, info.get("merge_oid"))
    parents = git(repo, "rev-list", "--parents", "-n", "1", fix).split()[1:]
    if len(parents) != 1:
        return {"pr": pr, "fix": fix, "skipped": "not a squash commit (needs exactly one parent)"}
    base = parents[0]
    listing = git(repo, "diff", "--no-renames", "--name-status", "-z", base, fix).split("\0")
    changes = [(listing[i], listing[i + 1]) for i in range(0, len(listing) - 1, 2)]
    tests = [(status, path) for status, path in changes if is_test_path(path)]
    sources = sorted(path for _, path in changes if not is_test_path(path))
    units = sorted(path for status, path in tests if not status.startswith("D") and is_unittest_file(path))
    if not tests:
        return {"pr": pr, "fix": fix, "skipped": "no test files changed"}
    if not sources:
        return {"pr": pr, "fix": fix, "skipped": "only test files changed"}
    if not units:
        return {"pr": pr, "fix": fix, "skipped": "no Python unittest file changed"}
    task_sha = make_task_commit(repo, base, fix, tests)
    candidates = set()
    for path in units:
        before = git(repo, "show", f"{base}:{path}", check=False) or None
        candidates |= changed_tests(before, git(repo, "show", f"{fix}:{path}"), PurePosixPath(path).stem)
    with tempfile.TemporaryDirectory(prefix="fusion-gym-trees-") as temp:
        task_tree, fix_tree = archive(repo, task_sha, Path(temp) / "task"), archive(repo, fix, Path(temp) / "fix")
        at_task, at_fix = run_tests(task_tree, units, timeout), run_tests(fix_tree, units, timeout)
        f2p = sorted(test for test in candidates if at_task.get(test, "missing") in {"failed", "error", "missing"}
                     and at_fix.get(test) == "passed")
        p2p = sorted(test for test, outcome in at_fix.items() if outcome == "passed" and at_task.get(test) == "passed"
                     and test not in f2p)[:p2p_limit]
        if not f2p:
            return {"pr": pr, "fix": fix, "skipped": "no changed test fails on the task tree and passes on the fix",
                    "candidates": len(candidates)}
        # The commands themselves are the proof: F2P fails on the task tree and
        # passes on the fix; P2P passes on both, or it is dropped.
        f2p_checks, p2p_checks, dropped = check_commands(f2p, units), [], []
        for argv, _ in f2p_checks:
            if exit_code(task_tree, argv, timeout) in (0, None) or exit_code(fix_tree, argv, timeout) != 0:
                return {"pr": pr, "fix": fix, "skipped": f"F2P command did not fail->pass: {' '.join(argv[:10])}"}
        for argv, ids in check_commands(p2p, units):
            if exit_code(task_tree, argv, timeout) == 0 and exit_code(fix_tree, argv, timeout) == 0:
                p2p_checks.append((argv, ids))
            else:
                dropped.append(argv)
    prompt, source = task_prompt(info, git(repo, "log", "-1", "--format=%s", fix).strip())
    task_id = f"pr-{int(pr)}"
    git(repo, "update-ref", REF_PREFIX + task_id, task_sha)
    return {
        "schema": TASK_SCHEMA, "id": task_id, "pr": int(pr), "pr_url": info.get("url"),
        "issues": [issue.get("url") for issue in info.get("issues") or []],
        "repo_path": str(repo), "base": base, "fix": fix, "task_ref": REF_PREFIX + task_id, "task_sha": task_sha,
        "prompt": prompt, "prompt_source": source, **({"gh_error": info["gh_error"]} if info.get("gh_error") else {}),
        "fail_to_pass": f2p, "pass_to_pass": sorted(test for _, ids in p2p_checks for test in ids),
        "checks": {"fail_to_pass": [argv for argv, _ in f2p_checks], "pass_to_pass": [argv for argv, _ in p2p_checks]},
        **({"pass_to_pass_dropped": dropped} if dropped else {}),
        "test_files": sorted(path for _, path in tests), "source_files": sources,
        "extracted_at_ms": int(time.time() * 1000),
    }


def extract(repo, prs, out=None, ref="HEAD", gh_repo=None, use_gh=True, timeout=DEFAULT_TIMEOUT,
            p2p_limit=DEFAULT_P2P_LIMIT):
    rows = []
    for pr in prs:
        try:
            task = extract_one(repo, pr, ref, gh_repo, use_gh, timeout, p2p_limit)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            task = {"pr": pr, "skipped": str(exc)}
        if out and "schema" in task:
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / f"{task['id']}.json").write_text(json.dumps(task, indent=2, ensure_ascii=False) + "\n")
        rows.append(task)
    summary = {"repo": str(Path(repo).resolve()), "ref": ref, "tasks": [
        {"id": t["id"], "pr": t["pr"], "fail_to_pass": len(t["fail_to_pass"]), "pass_to_pass": len(t["pass_to_pass"]),
         "prompt_source": t["prompt_source"]} for t in rows if "schema" in t],
        "skipped": [{"pr": t["pr"], "reason": t["skipped"]} for t in rows if "skipped" in t]}
    if out:
        (Path(out) / "index.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary, rows


# ---------------------------------------------------------------- running

def load_tasks(path):
    path = Path(path)
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    tasks = []
    for file in files:
        try:
            value = json.loads(file.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read gym task {file}: {exc}") from exc
        if isinstance(value, dict) and value.get("schema") == TASK_SCHEMA:
            tasks.append(value)
    if not tasks:
        raise ValueError(f"no gym tasks in {path}")
    return sorted(tasks, key=lambda t: (t.get("pr") or 0, t["id"]))


def resolve_lane(config, name):
    lanes = {**DEFAULT_LANES, **((config.get("gym") or {}).get("lanes") or {})}
    if name in lanes:
        lane = dict(lanes[name])
    elif name in (config.get("routes") or {}):
        lane = {"agent": (config["routes"][name] or {}).get("agent", "claude"), "route": name}
    else:
        raise ValueError(f"unknown gym lane {name}: use {', '.join(sorted(lanes))}, a route, or gym.lanes")
    if not isinstance(lane, dict) or set(lane) - LANE_KEYS or lane.get("agent") not in {"claude", "codex", "agy", "grok"}:
        raise ValueError(f"gym lane {name} must be an object with agent and optional route, model, reasoning_effort")
    return lane


def _slug(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:24] or "lane"


def build_spec(task, lane, budget_remaining=None):
    checks = task["checks"]["fail_to_pass"] + task["checks"]["pass_to_pass"]
    node = {"id": "implement", "role": "implementation", "agent": lane["agent"], "write": True,
            "task": task["prompt"] + "\n\nFix this in the repository. Keep the change scoped to the problem above.",
            "decision_context": task["prompt"],
            "acceptance": {"checks": checks, "before": True, "required_handoff": ["summary"]}}
    for key in ("route", "model", "reasoning_effort"):
        if lane.get(key):
            node[key] = lane[key]
    if budget_remaining is not None and lane["agent"] == "claude":
        node["max_budget_usd"] = round(max(budget_remaining, 0.01), 2)
    return {"task": f"gym {task['id']}: {task['prompt'].splitlines()[0][:200]}", "max_attempts": 1,
            "budget_usd": max(budget_remaining, 0.01) if budget_remaining is not None else 0, "nodes": [node]}


def read_results(gym):
    path = Path(gym) / "results.jsonl"
    rows = []
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _append(gym, row):
    with (Path(gym) / "results.jsonl").open("a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


@contextlib.contextmanager
def _locked(gym):
    with (Path(gym) / ".gym.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"another gym run holds {gym}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def prepare_gym(gym, source_repo=None):
    """The gym directory is its own Fusion workspace (.fusion, .fusion.json).
    Its config is copied once from the source repository when it has one."""
    gym = Path(gym).resolve()
    gym.mkdir(parents=True, exist_ok=True)
    (gym / ".fusion").mkdir(exist_ok=True)
    config = gym / ".fusion.json"
    if not config.exists():
        source = Path(source_repo) / ".fusion.json" if source_repo else None
        config.write_text(source.read_text() if source and source.is_file() else "{}\n")
    return gym


def task_repo(gym, task):
    """A repository holding only the task commit and its history (never the fix)."""
    root = gym / "tasks" / task["id"]
    repo = root / "repo"
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q")
    if git(repo, "rev-parse", "-q", "--verify", TASK_REF, check=False).strip() != task["task_sha"]:
        git(repo, "fetch", "-q", "--no-tags", "--update-shallow", task["repo_path"], f"+{task['task_ref']}:{TASK_REF}")
        if git(repo, "rev-parse", "-q", "--verify", TASK_REF, check=False).strip() != task["task_sha"]:
            raise ValueError(f"{task['task_ref']} in {task['repo_path']} is not {task['task_sha']}; re-extract the task")
    return repo


def _remove_worktree(repo, path):
    if path.exists() or path.is_symlink():
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


def summarize(task, lane_name, lane, outcome, diff_files, wall_ms):
    node = (outcome.get("nodes") or [{}])[0]
    result = node.get("result") or {}
    receipts = result.get("acceptance_checks") or []
    by_argv = {json.dumps(r.get("argv")): r for r in receipts}

    def statuses(kind):
        return [by_argv.get(json.dumps(argv), {}) for argv in task["checks"][kind]]

    f2p, p2p = statuses("fail_to_pass"), statuses("pass_to_pass")
    f2p_passed = bool(f2p) and all(r.get("status") == "passed" for r in f2p)
    p2p_regressed = any(r.get("status") == "failed" for r in p2p)
    baseline_ok = (all((r.get("before") or {}).get("status") == "failed" for r in f2p)
                   and all((r.get("before") or {}).get("status") == "passed" for r in p2p))
    tampered = sorted(path for path in diff_files if is_test_path(path))
    dispatched = bool(result.get("run_id"))
    status = outcome.get("status")
    if not dispatched or status in {"paused_quota", "paused_budget", "interrupted"}:
        verdict, completed = ("unavailable" if outcome.get("lanes") else status or "not_run"), False
    elif not receipts:
        verdict, completed = "no_checks", False
    elif not baseline_ok:
        verdict, completed = "invalid_baseline", True
    elif tampered:
        verdict, completed = "tampered", True
    elif f2p_passed and not p2p_regressed:
        verdict, completed = "solved", True
    elif f2p_passed:
        verdict, completed = "regressed", True
    else:
        verdict, completed = "unsolved", True
    return {"schema": RESULT_SCHEMA, "event": "finished", "key": f"{task['id']}:{lane_name}", "task": task["id"],
            "lane": lane_name, "lane_spec": lane, "workflow_id": outcome.get("workflow_id"), "status": status,
            "verdict": verdict, "completed": completed, "f2p_passed": f2p_passed, "p2p_regressed": p2p_regressed,
            "baseline_ok": baseline_ok, "tampered": tampered, "changed": diff_files,
            "worker": {"status": result.get("status"), "agent": result.get("agent"), "route": result.get("route"),
                       "model": result.get("model"), "run_id": result.get("run_id")},
            "gate_label": result.get("gate_label"), "cost_usd": float(outcome.get("spent_usd") or 0),
            "duration_ms": wall_ms, "finished_at_ms": int(time.time() * 1000)}


def run(tasks_path, lanes, gym, max_tasks=None, budget_usd=None, keep_worktrees=False, runner=None):
    """Sequential task x lane runs; resumable (completed pairs are skipped)."""
    import fusion_core as core
    from fusion_publish import snapshot
    from fusion_workflow import WorkflowRunner
    runner = runner or WorkflowRunner
    tasks = load_tasks(tasks_path)
    gym = Path(gym).resolve()
    for task in tasks:
        source = Path(task["repo_path"]).resolve()
        if gym == source or gym.is_relative_to(source):
            raise ValueError(f"the gym directory must be outside the source repository {source}")
    gym = prepare_gym(gym, tasks[0]["repo_path"])
    config, _ = core.load_config(gym)
    resolved = {name: resolve_lane(config, name) for name in lanes}
    with _locked(gym):
        done = {row["key"] for row in read_results(gym) if row.get("event") == "finished" and row.get("completed")}
        spent, processed, finished = 0.0, 0, []
        for task in tasks:
            pending = [name for name in lanes if f"{task['id']}:{name}" not in done]
            if not pending:
                continue
            if max_tasks is not None and processed >= max_tasks:
                break
            processed += 1
            repo = task_repo(gym, task)
            for name in pending:
                if budget_usd is not None and spent >= budget_usd:
                    return {"status": "budget_reached", "spent_usd": spent, "runs": finished}
                lane = resolved[name]
                workflow_id = f"gym-{task['id']}-{_slug(name)}-{uuid.uuid4().hex[:8]}"
                worktree = gym / "tasks" / task["id"] / "lanes" / _slug(name)
                _remove_worktree(repo, worktree)
                worktree.parent.mkdir(parents=True, exist_ok=True)
                git(repo, "worktree", "add", "-q", "--detach", str(worktree), TASK_REF)
                (worktree / ".fusion").symlink_to(gym / ".fusion", target_is_directory=True)
                _append(gym, {"schema": RESULT_SCHEMA, "event": "started", "key": f"{task['id']}:{name}",
                              "workflow_id": workflow_id, "started_at_ms": int(time.time() * 1000)})
                spec = build_spec(task, lane, None if budget_usd is None else budget_usd - spent)
                started = time.monotonic()
                try:
                    outcome = runner(gym, config, spec, run_id=workflow_id,
                                     worktree={"workspace": str(worktree), "base_sha": task["task_sha"]}).run()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    outcome = {"workflow_id": workflow_id, "status": "error", "error": str(exc), "nodes": []}
                wall_ms = round((time.monotonic() - started) * 1000)
                diff_files, patch = [], b""
                try:
                    tree = snapshot(worktree)
                    diff_files = [p for p in git(repo, "diff", "--name-only", "-z", task["task_sha"], tree).split("\0") if p]
                    patch = git(repo, "diff", "--binary", task["task_sha"], tree, binary=True)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                row = summarize(task, name, lane, outcome, diff_files, wall_ms)
                if outcome.get("error"):
                    row["error"] = outcome["error"]
                evidence = gym / "results" / task["id"] / _slug(name)
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / f"{workflow_id}.patch").write_bytes(patch)
                row["patch"] = str(evidence / f"{workflow_id}.patch")
                _append(gym, row)
                finished.append(row)
                spent += row["cost_usd"]
                if not keep_worktrees:
                    _remove_worktree(repo, worktree)
        return {"status": "complete", "spent_usd": spent, "runs": finished}


# ---------------------------------------------------------------- report

def report(gym):
    latest = {}
    for row in read_results(gym):
        if row.get("event") == "finished":
            latest[row["key"]] = row
    lanes, tasks = {}, {}
    for row in latest.values():
        if not row.get("completed"):
            continue
        lane = lanes.setdefault(row["lane"], {"attempted": 0, "solved": 0, "f2p_passed": 0, "p2p_regressions": 0,
                                              "tampered": 0, "invalid_baseline": 0, "cost_usd": 0.0, "duration_ms": 0,
                                              "gate_labels": {}})
        valid = row["verdict"] != "invalid_baseline"
        lane["attempted"] += valid
        lane["invalid_baseline"] += not valid
        lane["solved"] += row["verdict"] == "solved"
        lane["f2p_passed"] += bool(valid and row["f2p_passed"] and not row["tampered"])
        lane["p2p_regressions"] += bool(valid and row["p2p_regressed"])
        lane["tampered"] += bool(row["tampered"])
        lane["cost_usd"] += row.get("cost_usd") or 0
        lane["duration_ms"] += row.get("duration_ms") or 0
        answer = json.dumps(((row.get("gate_label") or {}).get("answers")) or None)
        lane["gate_labels"][answer] = lane["gate_labels"].get(answer, 0) + 1
        task = tasks.setdefault(row["task"], {"solved_by": [], "attempted_by": []})
        task["attempted_by"].append(row["lane"])
        if row["verdict"] == "solved":
            task["solved_by"].append(row["lane"])
    for lane in lanes.values():
        n = lane["attempted"]
        lane["f2p_pass_rate"] = round(lane["f2p_passed"] / n, 3) if n else None
        lane["mean_duration_s"] = round(lane["duration_ms"] / n / 1000, 1) if n else None
        lane["cost_usd"] = round(lane["cost_usd"], 4)
    for task in tasks.values():
        task["solved_by"].sort()
        task["attempted_by"].sort()
    pending = sorted(key for key, row in latest.items() if not row.get("completed"))
    return {"gym": str(Path(gym).resolve()), "lanes": dict(sorted(lanes.items())), "tasks": dict(sorted(tasks.items())),
            "incomplete": pending}


def table(value):
    lines = [f"{'lane':<22} {'tasks':>5} {'solved':>6} {'f2p%':>6} {'p2p-reg':>7} {'tamper':>6} {'cost$':>8} {'mean s':>7}"]
    for name, lane in value["lanes"].items():
        rate = "-" if lane["f2p_pass_rate"] is None else f"{lane['f2p_pass_rate'] * 100:.0f}"
        lines.append(f"{name:<22} {lane['attempted']:>5} {lane['solved']:>6} {rate:>6} {lane['p2p_regressions']:>7} "
                     f"{lane['tampered']:>6} {lane['cost_usd']:>8.2f} {lane['mean_duration_s'] or 0:>7.1f}")
    lines.append("")
    for task_id, task in value["tasks"].items():
        lines.append(f"{task_id:<12} solved by: {', '.join(task['solved_by']) or 'none'}"
                     f"  (of {', '.join(task['attempted_by'])})")
    if value["incomplete"]:
        lines.append("incomplete (rerun `gym run` to retry): " + ", ".join(value["incomplete"]))
    return "\n".join(lines)


def extract_table(summary):
    lines = [f"{'task':<10} {'F2P':>4} {'P2P':>5} prompt"]
    lines += [f"{t['id']:<10} {t['fail_to_pass']:>4} {t['pass_to_pass']:>5} {t['prompt_source']}" for t in summary["tasks"]]
    lines += [f"PR #{s['pr']:<6} skipped: {s['reason']}" for s in summary["skipped"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def add_parser(sub):
    parser = sub.add_parser("gym", help="replay merged fix PRs as benchmark tasks across lanes")
    commands = parser.add_subparsers(dest="gym_command", required=True)
    extract_cmd = commands.add_parser("extract", help="turn squash-merged fix PRs into tasks with FAIL_TO_PASS tests")
    extract_cmd.add_argument("--repo-path", default=".", help="source repository (default: current directory)")
    extract_cmd.add_argument("--prs", type=int, nargs="+", required=True)
    extract_cmd.add_argument("--out", help="directory for task JSON files and index.json")
    extract_cmd.add_argument("--ref", default="HEAD", help="history to search for the PR commits (default HEAD)")
    extract_cmd.add_argument("--github-repo", help="OWNER/REPO for gh (default: inferred from the repository)")
    extract_cmd.add_argument("--no-gh", action="store_true", help="prompt from the commit subject; no GitHub reads")
    extract_cmd.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per test run")
    extract_cmd.add_argument("--p2p-limit", type=int, default=DEFAULT_P2P_LIMIT)
    run_cmd = commands.add_parser("run", help="run each task on each lane in its own worktree (resumable)")
    run_cmd.add_argument("tasks", help="task JSON file or directory from `gym extract`")
    run_cmd.add_argument("--lanes", nargs="+", required=True)
    run_cmd.add_argument("--workspace", dest="gym_workspace", required=True, metavar="GYMDIR",
                         help="gym directory: its own Fusion workspace, outside the source repository")
    run_cmd.add_argument("--max-tasks", type=int, help="at most N tasks with pending lanes in this invocation")
    run_cmd.add_argument("--budget-usd", type=float, help="stop starting runs once this invocation spent this much")
    run_cmd.add_argument("--keep-worktrees", action="store_true", help="keep each lane's worktree after its run")
    report_cmd = commands.add_parser("report", help="per-lane and per-task results of a gym directory")
    report_cmd.add_argument("gym_dir")


def command(args, workspace, as_json=False, out=None):
    out = out or sys.stdout
    if args.gym_command == "extract":
        repo = Path(args.repo_path if Path(args.repo_path).is_absolute() else Path(workspace) / args.repo_path)
        summary, _ = extract(repo, args.prs, args.out, args.ref, args.github_repo, not args.no_gh, args.timeout, args.p2p_limit)
        print(json.dumps(summary, indent=2) if as_json else extract_table(summary), file=out)
        return 0 if summary["tasks"] else 1
    if args.gym_command == "run":
        if args.max_tasks is not None and args.max_tasks < 1:
            raise ValueError("--max-tasks must be at least 1")
        result = run(args.tasks, args.lanes, args.gym_workspace, args.max_tasks, args.budget_usd, args.keep_worktrees)
        print(json.dumps(result, indent=2) if as_json else
              f"{result['status']}: {len(result['runs'])} runs, ${result['spent_usd']:.2f}\n" + table(report(args.gym_workspace)),
              file=out)
        return 0
    value = report(args.gym_dir)
    print(json.dumps(value, indent=2) if as_json else table(value), file=out)
    return 0
