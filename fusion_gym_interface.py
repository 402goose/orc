"""Interface hints for ORC gym tasks: names and signatures, never bodies.

A hidden-test task is underspecified when its tests call a function, method,
parameter or CLI option the fix introduced and the problem text never names.
`interface_hints` reads the fix's test files (the hidden fixtures) with `ast`,
collects the source-module symbols they reference, resolves each at the base
B and the fix C, and returns the ones that are new at C or whose signature
changed: module-qualified name plus the signature at C (arguments with
defaults, keyword-only markers, annotations), the class header for a new
class, and the name alone for a new module-level constant. CLI options and
subcommands are included when the fix's source adds an `add_argument` or
`add_parser` literal that a test file also contains as a string.

Nothing else from either tree is emitted: no function body, no docstring, no
default computed elsewhere, no test name, path or code. Test-helper modules
(paths under test/ or tests/, or named test_*.py / *_test.py) are never
resolved as sources.

Stdlib only; `read(rev, path)` returns a file's text at a revision or None.
"""
from __future__ import annotations

import ast
from pathlib import PurePosixPath
import re

KIND_ORDER = {"function": 0, "class": 1, "method": 2, "name": 3, "cli_option": 4, "cli_command": 5}
SOURCE_ROOTS = ("", "src/")
MAX_IMPORT_HOPS = 3


def _parse(text):
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _decorators(fn):
    names = set()
    for deco in fn.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names & {"staticmethod", "classmethod", "property", "setter"}


def signature(fn):
    """`(args) -> returns`, from the def line only."""
    text = f"({ast.unparse(fn.args)})"
    if fn.returns is not None:
        text += f" -> {ast.unparse(fn.returns)}"
    prefix = "async " if isinstance(fn, ast.AsyncFunctionDef) else ""
    marks = sorted(_decorators(fn))
    return (f"@{' @'.join(marks)} " if marks else "") + prefix + text


def _top_level(tree):
    """Statements at module level, descending into if/try/with blocks."""
    stack = list(tree.body)
    while stack:
        node = stack.pop(0)
        if isinstance(node, (ast.If, ast.With, ast.AsyncWith)):
            stack[:0] = node.body + getattr(node, "orelse", [])
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            stack[:0] = node.body + node.orelse + node.finalbody + [s for h in node.handlers for s in h.body]
        else:
            yield node


def _class_entry(node):
    methods, fields = {}, []
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "setter" not in _decorators(item):
                methods[item.name] = signature(item)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            fields.append(item.target.id)
    bases = [ast.unparse(base) for base in node.bases] + [ast.unparse(k) for k in node.keywords]
    return {"kind": "class", "signature": f"({', '.join(bases)})" if bases else "", "methods": methods,
            "fields": fields}


def module_table(tree):
    """name -> entry for a module's top-level bindings."""
    table = {}
    for node in _top_level(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            table[node.name] = {"kind": "function", "signature": signature(node)}
        elif isinstance(node, ast.ClassDef):
            table[node.name] = _class_entry(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        table.setdefault(name.id, {"kind": "name"})
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name != "*":
                    table[alias.asname or alias.name] = {"kind": "import", "module": node.module, "name": alias.name}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                table.setdefault(alias.asname or alias.name.split(".")[0], {"kind": "name"})
    return table


class Tree:
    """Source modules of one revision, parsed on demand."""

    def __init__(self, read, rev):
        self.read, self.rev, self.cache = read, rev, {}

    def module_path(self, module):
        stem = module.replace(".", "/")
        for root in SOURCE_ROOTS:
            for candidate in (f"{root}{stem}.py", f"{root}{stem}/__init__.py"):
                if self.text(candidate) is not None:
                    return candidate
        return None

    def text(self, path):
        if path not in self.cache:
            self.cache[path] = self.read(self.rev, path)
        return self.cache[path]

    def table(self, module):
        path = self.module_path(module)
        tree = _parse(self.text(path)) if path else None
        return (module_table(tree), path) if tree else ({}, path)

    def resolve(self, module, name, hops=MAX_IMPORT_HOPS):
        table, path = self.table(module)
        entry = table.get(name)
        if entry and entry["kind"] == "import" and hops:
            target = self.resolve(entry["module"], entry["name"], hops - 1)
            if target[0]:
                return target
            return {"kind": "name"}, path
        return entry, path


def is_test_path(path):
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return bool({"test", "tests"} & set(parts[:-1])) or bool(re.fullmatch(r"test_.*\.py|.*_test\.py", name))


def _chain(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts[::-1]
    return None


def references(test_source, is_module):
    """(module, path) pairs a test file references in modules for which
    `is_module(name)` is true, plus every attribute name and string it uses."""
    tree = _parse(test_source)
    if tree is None:
        return set(), set(), set()
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = ("module", alias.name, ())
                else:
                    head = alias.name.split(".")[0]
                    aliases[head] = ("module", head, ())
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                full = f"{node.module}.{alias.name}"
                aliases[alias.asname or alias.name] = (
                    ("module", full, ()) if is_module(full) else ("symbol", node.module, (alias.name,)))
    refs, attrs, strings = set(), set(), set()

    def add(local, rest):
        kind, module, path = aliases[local]
        rest = list(path) + list(rest)
        while rest and is_module(f"{module}.{rest[0]}"):
            module = f"{module}.{rest.pop(0)}"
        if rest and is_module(module):
            refs.add((module, tuple(rest[:2])))

    for local, (kind, _, _) in aliases.items():
        if kind == "symbol":
            add(local, ())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            attrs.add(node.attr)
            chain = _chain(node)
            if chain and chain[0] in aliases:
                add(chain[0], chain[1:])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
        elif isinstance(node, ast.Call) and len(node.args) >= 2:
            first, second = node.args[0], node.args[1]
            if (isinstance(first, ast.Name) and first.id in aliases and isinstance(second, ast.Constant)
                    and isinstance(second.value, str) and second.value.isidentifier()):
                attrs.add(second.value)
                add(first.id, (second.value,))
    for value in strings:
        parts = value.split(".")
        if len(parts) >= 2 and all(part.isidentifier() for part in parts):
            for cut in range(len(parts) - 1, 0, -1):
                module = ".".join(parts[:cut])
                if is_module(module):
                    refs.add((module, tuple(parts[cut:cut + 2])))
                    break
    return refs, attrs, strings


def _cli_literals(tree):
    options, commands = set(), set()
    if tree is None:
        return options, commands
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        literals = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if node.func.attr == "add_argument":
            options |= {value for value in literals if value.startswith("-")}
        elif node.func.attr == "add_parser" and literals:
            commands.add(literals[0])
    return options, commands


def _item(kind, status, symbol, sig, path):
    return {"kind": kind, "status": status, "symbol": symbol, "signature": sig, "file": path}


def interface_hints(read, base, fix, test_paths, source_paths=()):
    """Sorted hint dicts {kind, status: new|changed, symbol, signature, file}."""
    at_b, at_c = Tree(read, base), Tree(read, fix)

    def is_module(name):
        path = at_c.module_path(name)
        return bool(path) and not is_test_path(path)

    items, all_attrs, all_strings = {}, set(), set()
    for test_path in sorted(test_paths):
        refs, attrs, strings = references(read(fix, test_path), is_module)
        all_attrs |= attrs
        all_strings |= strings
        for module, path in sorted(refs):
            _hint_symbol(at_b, at_c, module, path, attrs, items)
    for path in sorted(source_paths):
        if not path.endswith(".py") or is_test_path(path):
            continue
        new_options, new_commands = _cli_literals(_parse(read(fix, path)))
        old_options, old_commands = _cli_literals(_parse(read(base, path)))
        for option in sorted(new_options - old_options):
            if any(value == option or value.startswith(option + "=") for value in all_strings):
                items[("cli_option", option)] = _item("cli_option", "new", option, "", path)
        for name in sorted(new_commands - old_commands):
            if name in all_strings:
                items[("cli_command", name)] = _item("cli_command", "new", name, "", path)
    return sorted(items.values(), key=lambda i: (KIND_ORDER[i["kind"]], i["symbol"]))


def _hint_symbol(at_b, at_c, module, path, attrs, items):
    name = path[0]
    new, file = at_c.resolve(module, name)
    old, _ = at_b.resolve(module, name)
    if not new or new["kind"] == "import":
        return
    symbol = f"{module}.{name}"
    if new["kind"] == "function":
        if not old or old.get("signature") != new["signature"]:
            items[("function", symbol)] = _item("function", "changed" if old else "new", symbol, new["signature"], file)
    elif new["kind"] == "name":
        if not old:
            items[("name", symbol)] = _item("name", "new", symbol, "", file)
    elif new["kind"] == "class":
        old_methods = (old or {}).get("methods") or {}
        if not old or old["kind"] != "class":
            items[("class", symbol)] = _item("class", "new", symbol, new["signature"], file)
            if new["fields"]:
                items[("class", symbol)]["fields"] = list(new["fields"])
        wanted = ({path[1]} if len(path) > 1 else set()) | (attrs & set(new["methods"])) | {"__init__"}
        for method in sorted(wanted & set(new["methods"])):
            sig = new["methods"][method]
            if old_methods.get(method) != sig:
                status = "changed" if method in old_methods else "new"
                items[("method", f"{symbol}.{method}")] = _item("method", status, f"{symbol}.{method}", sig, file)


def render(items):
    """The prompt section, or "" when there is nothing to hint."""
    if not items:
        return ""
    lines = []
    for item in items:
        status = "new" if item["status"] == "new" else "changed signature"
        if item["kind"] in {"function", "method"}:
            lines.append(f"- {status}: {item['symbol']}{item['signature']}")
        elif item["kind"] == "class":
            fields = f" with fields {', '.join(item['fields'])}" if item.get("fields") else ""
            lines.append(f"- new class: {item['symbol']}{item['signature']}{fields}")
        elif item["kind"] == "name":
            lines.append(f"- new module-level name: {item['symbol']}")
        elif item["kind"] == "cli_option":
            lines.append(f"- new command-line option: {item['symbol']} ({item['file']})")
        else:
            lines.append(f"- new subcommand: {item['symbol']} ({item['file']})")
    return ("\n\nInterface the change must provide (the grading tests use these names; "
            "signatures as they must read after the change):\n" + "\n".join(lines))
