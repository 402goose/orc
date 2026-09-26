"""Localization tasks for the ORC gym: ground truth, answer parsing, grading.

A localization task is read-only. The worker gets the problem text and the
pre-fix tree B and names where the bug is; the gym grades that against the
reference fix C, as SWE-bench / Agentless localization is graded.

Ground truth (`localization`) comes from the diff B..C, never from the
worker: `files` are the changed source files (not tests, not docs) and
`symbols` the enclosing functions, classes and methods of every changed
line, as `module.qualname`. Added and modified lines are resolved in C,
removed lines in B. Files and symbols that do not exist in B are listed in
`new_files` / `new_symbols`: a worker who only sees B cannot name them, so
grading leaves them out. Doc files are listed in `docs` and not graded.

Stdlib only; `read(rev, path)` returns a file's text at a revision or None,
`diff(path)` the `git diff -U0 B C -- path` text.
"""
from __future__ import annotations

import ast
import json
from pathlib import PurePosixPath
import re

MAX_ITEMS = 10
TOP_K = 3
DOC_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc"}
DOC_DIRS = {"doc", "docs"}
BLOCK = re.compile(r"^[ \t]*```[ \t]*localization[ \t]*\n(.*?)\n[ \t]*```[ \t]*$", re.S | re.M)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)

BRIEF = """

Do not change any file: this is a read-only task. Find where in this repository the problem above has to be fixed: the source files (not tests) and the functions, classes or methods the fix must change. Rank each list most likely first, at most 10 entries each.

End your answer with exactly one fenced block tagged `localization` holding a JSON object, followed by the handoff:

```localization
{"files": ["path/to/module.py"], "symbols": ["path.to.module.function", "path.to.module.Class.method"]}
```

Files are paths relative to the repository root. A symbol is the file's dotted module path followed by the qualified name inside it (module.function, module.Class or module.Class.method)."""


def is_doc_path(path):
    parts = PurePosixPath(path).parts
    return PurePosixPath(path).suffix.lower() in DOC_SUFFIXES or bool(parts and parts[0] in DOC_DIRS)


def module_name(path):
    """`pkg/mod.py` -> `pkg.mod`; `pkg/__init__.py` -> `pkg`; `src/` is dropped."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__" and len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts)


def intervals(source):
    """[(start, end, qualname)] for every function, class and method; nested
    functions are part of the function that holds them. None if unparsable."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    from fusion_gym_interface import _top_level
    spans = []

    def visit(node, prefix):
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        name = f"{prefix}{node.name}"
        spans.append((start, node.end_lineno, name))
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(item, name + ".")

    for node in _top_level(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            visit(node, "")
    return spans


def enclosing(spans, line):
    """The innermost qualname holding `line`, or None at module level."""
    best = None
    for start, end, name in spans:
        if start <= line <= end and (best is None or start >= best[0]):
            best = (start, name)
    return best[1] if best else None


def hunks(diff_text):
    """[(old_lines, new_lines)] of a zero-context diff: the line numbers each
    hunk removes from B and adds in C."""
    out = []
    for match in HUNK.finditer(diff_text or ""):
        old_start, old_count, new_start, new_count = match.groups()
        old_count = 1 if old_count is None else int(old_count)
        new_count = 1 if new_count is None else int(new_count)
        out.append((range(int(old_start), int(old_start) + old_count),
                    range(int(new_start), int(new_start) + new_count)))
    return out


def all_symbols(spans):
    return {name for _, _, name in spans or []}


def ground_truth(read, base, fix, changes, diff):
    """{files, symbols, new_files, new_symbols, docs} for the source changes
    [(status, path)] of the fix; `changes` must not include test files."""
    files, docs, new_files, symbols, new_symbols = [], [], [], set(), set()
    for status, path in sorted(changes, key=lambda item: item[1]):
        if is_doc_path(path):
            docs.append(path)
            continue
        files.append(path)
        at_b = read(base, path)
        if at_b is None:
            new_files.append(path)
        if not path.endswith(".py"):
            continue
        at_c = read(fix, path)
        spans_b = intervals(at_b) if at_b is not None else []
        spans_c = intervals(at_c) if at_c is not None else []
        if spans_b is None or spans_c is None:
            continue
        module = module_name(path)
        found = set()
        for old_lines, new_lines in hunks(diff(path)):
            found |= {name for name in (enclosing(spans_b, line) for line in old_lines) if name}
            found |= {name for name in (enclosing(spans_c, line) for line in new_lines) if name}
        existing = all_symbols(spans_b)
        symbols |= {f"{module}.{name}" for name in found}
        new_symbols |= {f"{module}.{name}" for name in found if name not in existing}
    return {"files": files, "symbols": sorted(symbols), "new_files": new_files,
            "new_symbols": sorted(new_symbols), "docs": docs}


def gradeable(truth):
    """(files, symbols) a worker who sees only B can name."""
    files = [path for path in truth.get("files") or [] if path not in set(truth.get("new_files") or [])]
    symbols = [name for name in truth.get("symbols") or [] if name not in set(truth.get("new_symbols") or [])]
    return files, symbols


def _dedupe(values):
    return list(dict.fromkeys(values))


def normalize_file(value):
    value = value.strip().strip("`").strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def normalize_symbol(value):
    """`path/mod.py::Class.method`, `path/mod.py:func` or `mod.func()` ->
    `mod.Class.method` / `path.mod.func` / `mod.func`."""
    value = value.strip().strip("`").strip()
    value = re.sub(r"\(.*\)$", "", value)
    path, sep, qual = value.partition("::") if "::" in value else value.partition(":")
    if sep and path.endswith(".py"):
        return f"{module_name(normalize_file(path))}.{qual.strip()}"
    return value


def parse_answer(text):
    """(answer, error): answer is {files, symbols} (deduplicated, at most
    MAX_ITEMS each); error says why the answer is invalid."""
    blocks = BLOCK.findall(text or "")
    if not blocks:
        return None, "no ```localization block"
    if len(blocks) > 1:
        return None, f"{len(blocks)} ```localization blocks; exactly one is required"
    try:
        value = json.loads(blocks[0])
    except ValueError as exc:
        return None, f"the localization block is not JSON ({exc})"
    if not isinstance(value, dict):
        return None, "the localization block is not a JSON object"
    files, symbols = value.get("files"), value.get("symbols", [])
    if not isinstance(files, list) or not files or not all(isinstance(item, str) and item.strip() for item in files):
        return None, "files must be a non-empty list of paths"
    if not isinstance(symbols, list) or not all(isinstance(item, str) and item.strip() for item in symbols):
        return None, "symbols must be a list of strings"
    return {"files": _dedupe(normalize_file(item) for item in files)[:MAX_ITEMS],
            "symbols": _dedupe(normalize_symbol(item) for item in symbols)[:MAX_ITEMS]}, None


def grade(answer, truth):
    """Scores of a parsed answer against the ground truth.

    Only files and symbols that exist in B are graded (gradeable). file
    recall@k is the share of them in the answer's top k files; precision is
    the share of the answer's files that are among them; acc@1 is whether the
    top file is one of them; symbol recall is the share of changed symbols
    named anywhere in the answer's symbols (None when the fix changed no
    gradeable symbol). The verdict is `localized` when the top max(3, n) files
    hold all n changed files, `missed` when none of the top 3 is one of them,
    otherwise `partial`."""
    files, symbols = gradeable(truth)
    truth_files, truth_symbols = set(files), set(symbols)
    predicted = answer["files"]

    def recall(k):
        return round(len(set(predicted[:k]) & truth_files) / len(truth_files), 3)

    top = max(TOP_K, len(truth_files))
    hit_top3 = bool(set(predicted[:TOP_K]) & truth_files)
    verdict = "localized" if truth_files <= set(predicted[:top]) else "partial" if hit_top3 else "missed"
    return {"verdict": verdict,
            "file_acc_at_1": bool(predicted and predicted[0] in truth_files),
            "file_recall_at_1": recall(1), "file_recall_at_3": recall(3), "file_recall_at_5": recall(5),
            "file_precision": round(len(set(predicted) & truth_files) / len(predicted), 3),
            "symbol_recall": (round(len(set(answer["symbols"]) & truth_symbols) / len(truth_symbols), 3)
                              if truth_symbols else None),
            "graded_files": len(truth_files), "graded_symbols": len(truth_symbols)}


ZERO = {"file_acc_at_1": False, "file_recall_at_1": 0.0, "file_recall_at_3": 0.0, "file_recall_at_5": 0.0,
        "file_precision": 0.0}
