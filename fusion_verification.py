"""Which plan-written verification commands the coordinator may execute.

A plan node is model output, so its `verification` commands are untrusted.
They run as implement-node acceptance checks only after this policy turns
them into argv arrays:

- argv only. A list of strings is taken as is; a string is split with shlex
  only when it contains no shell syntax (operators, redirection, expansion,
  globs, env assignments, newlines). Anything else is dropped with a reason,
  never run through a shell. A command a shell would have rewritten would run
  differently without one and fail for reasons unrelated to the work, which
  would become a false negative label.
- argv[0] is a bare program name (no path) whose name is on the allowlist
  (`verification.runners`, default DEFAULT_RUNNERS): test runners and the
  build tools that invoke them. A path could name a script the plan or
  implementer wrote.
- no installs, publishing or inline code: package-manager subcommands that
  install, add, update, publish or fetch are refused, as are interpreter
  flags that execute a string (`python -c`, `node -e`, `ruby -e`, ...).
- the check environment asks tools to stay offline (OFFLINE_ENV) and not to
  write bytecode into the tree. That is a request to the tools, not a network
  sandbox.

This is not a sandbox. An allowed runner executes repository code (tests,
conftest.py, package.json scripts, Makefiles) with the user's privileges,
exactly as the worker was already told to. The policy bounds which tools a
plan can invoke, not what repository code does once invoked.
"""
from __future__ import annotations

import re
import shlex
from typing import Any

DEFAULT_RUNNERS = (
    "python3", "python", "pytest", "npm", "pnpm", "yarn", "bun", "node", "deno", "go", "cargo",
    "make", "uv", "ruby", "rspec", "bundle", "mvn", "gradle",
)
DEFAULT_TIMEOUT_SECONDS = 900
MAX_CHECKS = 8
MAX_ARGV = 64
MAX_CHARS = 400
OFFLINE_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_NO_INDEX": "1",
    "UV_OFFLINE": "1",
    "npm_config_offline": "true",
    "YARN_ENABLE_NETWORK": "0",
    "GOPROXY": "off",
    "CARGO_NET_OFFLINE": "true",
}
_SHELL = re.compile(r"[|&;<>()$`\\*?\[\]{}~!#\n\r]")
_ASSIGNMENT = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*=")
# First positional argument a package manager may be given. Everything else
# (install, add, ci, update, publish, dlx, exec, x, ...) is refused.
_SUBCOMMANDS = {
    "npm": {"test", "t", "run", "run-script"},
    "pnpm": {"test", "t", "run"},
    "yarn": {"test", "run"},
    "bun": {"test", "run"},
    "go": {"test", "vet", "build"},
    "cargo": {"test", "check", "build", "clippy", "fmt"},
    "uv": {"run"},
    "deno": {"test", "check", "lint", "fmt"},
    "bundle": {"exec"},
}
_INLINE = {
    "python": {"-c"}, "python3": {"-c"},
    "node": {"-e", "--eval", "-p", "--print"},
    "bun": {"-e", "--eval", "-p", "--print"},
    "ruby": {"-e"},
    "deno": {"eval"},
}
_DENIED_WORDS = {"install", "uninstall", "add", "remove", "update", "upgrade", "publish", "deploy", "release",
                 "upload", "push", "login", "logout", "adduser", "link", "dlx", "npx", "pip", "ensurepip",
                 "sync", "lock", "tool", "self", "get", "mod"}


def settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = (config or {}).get("verification") or {}
    if not isinstance(raw, dict):
        raise ValueError("verification must be an object")
    runners = raw.get("runners", list(DEFAULT_RUNNERS))
    if not isinstance(runners, list) or not all(isinstance(name, str) and name and "/" not in name for name in runners):
        raise ValueError("verification.runners must be a list of bare program names")
    execute = raw.get("execute", True)
    if not isinstance(execute, bool):
        raise ValueError("verification.execute must be true or false")
    timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("verification.timeout_seconds must be a positive integer")
    return {"execute": execute, "runners": sorted(set(runners)), "timeout_seconds": timeout}


def to_argv(item: Any) -> tuple[list[str] | None, str | None]:
    """(argv, None) for a command that needs no shell, else (None, reason)."""
    if isinstance(item, list):
        if not item or not all(isinstance(part, str) and part and "\x00" not in part for part in item):
            return None, "argv must be non-empty strings"
        argv = list(item)
    elif isinstance(item, str):
        text = item.strip()
        if not text:
            return None, "empty command"
        if _SHELL.search(text):
            return None, "shell syntax (operators, redirection, expansion or globs) is not executed"
        try:
            argv = shlex.split(text)
        except ValueError as exc:
            return None, f"cannot be split into argv: {exc}"
    else:
        return None, "a command must be an argv array or a plain string"
    if len(argv) > MAX_ARGV or sum(len(part) for part in argv) > MAX_CHARS:
        return None, "command is too long"
    if _ASSIGNMENT.match(argv[0]):
        return None, "environment assignments need a shell"
    return argv, None


def refusal(argv: list[str], runners: list[str]) -> str | None:
    program = argv[0]
    if "/" in program or "\\" in program:
        return "argv[0] must be a bare program name on PATH, not a path"
    if program not in runners:
        return f"{program} is not an allowed test runner"
    rest = argv[1:]
    if set(rest) & _INLINE.get(program, set()):
        return f"{program} would execute inline code"
    if program in {"python", "python3"} and "-m" in rest:
        module = rest[rest.index("-m") + 1] if rest.index("-m") + 1 < len(rest) else ""
        if module in {"pip", "ensurepip", "venv", "http.server"}:
            return f"python -m {module} is not a verification command"
    positional = [part for part in rest if not part.startswith("-")]
    allowed = _SUBCOMMANDS.get(program)
    if allowed is not None and (not positional or positional[0] not in allowed):
        return f"{program} {positional[0] if positional else '(no subcommand)'} is not an allowed subcommand"
    words = {part.lower() for part in positional}
    if program in {"make", "mvn", "gradle", "npm", "pnpm", "yarn", "bun", "uv", "cargo", "go", "bundle"} and words & _DENIED_WORDS:
        return "installs, publishing and dependency changes are not verification"
    if program == "uv" and ("--with" in rest or any(part.startswith("--with=") for part in rest)):
        return "uv run --with installs packages"
    return None


def plan_checks(items: list[Any], config: dict[str, Any]) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """(checks to execute, rejected [{command, reason}]) for a plan's verification list."""
    options = settings(config)
    checks: list[list[str]] = []
    rejected: list[dict[str, Any]] = []
    if not options["execute"]:
        return [], [{"command": item, "reason": "verification.execute is false"} for item in items or []]
    for item in items or []:
        argv, reason = to_argv(item)
        reason = reason or refusal(argv, options["runners"])
        if reason:
            rejected.append({"command": item, "reason": reason})
        elif argv in checks:
            continue
        elif len(checks) >= MAX_CHECKS:
            rejected.append({"command": item, "reason": f"at most {MAX_CHECKS} verification commands run"})
        else:
            checks.append(argv)
    return checks, rejected


def counts_as_failure(argv: list[str], exit_code: Any) -> bool:
    """Whether a nonzero exit means tests ran and failed, rather than that the
    runner could not run them. pytest documents its exit codes: 1 is failed
    tests; 2-5 are interruption, internal error, usage error and no tests
    collected, which a broken environment or command produces as well."""
    if not isinstance(exit_code, int) or exit_code == 0:
        return False
    if argv and (argv[0] == "pytest" or argv[:3] in (["python3", "-m", "pytest"], ["python", "-m", "pytest"])):
        return exit_code == 1
    return True
