# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reads, from the ``agents/`` source, which graph writes are opted in to being
sent again after an ambiguous failure, and every statement text each one can
send. Used by ``test_neptune_write_resubmit_opt_in``, which compares the result
with the statements pinned there.

A write opts in through one of three names (``OPT_IN_NAMES``), however it is
spelled: the bare name, an import alias (``from .onboard import
neptune_write_resubmit_safe as w``) or a module-qualified reference
(``onboard.neptune_write_resubmit_safe``, ``db.resubmit_safe_write``):

* a call of ``onboard.neptune_write_resubmit_safe`` or
  ``onboard.neptune_write_returning_resubmit_safe``, whose first argument is
  the statement;
* one of those helpers passed to a function as a writer, by keyword or
  position (``write_schema_entry(..., write=neptune_write_resubmit_safe)``):
  the statements are the ones that function sends through that parameter;
* ``with resubmit_safe_write():`` around exactly one statement that calls a
  graph-write seam. ``with resubmit_safe_write(after_drop=True):`` is the same
  and is also listed by :func:`opted_in_after_drop`; ``after_drop`` must be a
  literal keyword, and the onboard helpers may not carry it.

Every other reference to one of the names (an assignment, a decorator, a
second-hand pass-through, a module-level use) is reported as a problem, so a
new way of opting in cannot go unseen.

A statement text is evaluated, not matched: string literals, ``+``, a
conditional expression, a module-level string constant, a local name assigned
from those (``+=`` included, or unpacked from a tuple, a conditional of tuples
or a same-module function's returned tuples), and a parameter of a
same-module function every caller of which passes a pinnable text. Anything
else (an f-string, a call that builds the text) cannot be pinned, and is a
problem.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
from collections import defaultdict
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2] / "agents"
#: The names that opt a write in, and the module that defines each.
OPT_IN_NAMES = {"neptune_write_resubmit_safe": "onboard",
                "neptune_write_returning_resubmit_safe": "onboard",
                "resubmit_safe_write": "db"}
WRITE_HELPERS = {"neptune_write_resubmit_safe", "neptune_write_returning_resubmit_safe"}
#: The write seams a ``with resubmit_safe_write():`` block may wrap.
#: ``neptune_write_outcome`` is ``neptune_write`` answering how a write that
#: was not confirmed ended (the core-concept seed, ``server._send_core_concepts``).
WITH_SEAMS = {"neptune_write", "neptune_write_returning", "_neptune_write",
              "_neptune_write_loud", "_neptune_write_returning", "neptune_write_outcome"}
#: The two helpers' own bodies: one write inside ``resubmit_safe_write()``.
HELPER_BODIES = {
    ("onboard.py", "neptune_write_resubmit_safe"): "neptune_write(cypher, parameters)",
    ("onboard.py", "neptune_write_returning_resubmit_safe"):
        "neptune_write_returning(cypher, parameters)",
}


class Unpinnable(Exception):
    """A statement text that is not built only from pinnable parts."""


class _Source:
    def __init__(self, root: Path, path: Path):
        self.root, self.path = root, path
        self.tree = ast.parse(path.read_text(), filename=str(path))
        self.top = {n.name: n for n in self.tree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.parent = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
        self.names, self.modules = _aliases(self.tree, path.stem)
        self._module = None

    @property
    def module(self):
        if self._module is None:
            if (AGENTS / self.path.name).exists():
                self._module = importlib.import_module(f"agents.{self.path.stem}")
            else:
                spec = importlib.util.spec_from_file_location(
                    f"_pinned_{abs(hash(self.path))}", self.path)
                self._module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(self._module)
        return self._module

    def enclosing(self, node):
        while node in self.parent:
            node = self.parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None


def _aliases(tree, stem: str):
    """``(names, modules)``: local names bound to an opt-in name (by an
    import, or the definition in its own module), and local names bound to an
    ``agents`` module."""
    names = {n: n for n, home in OPT_IN_NAMES.items() if home == stem}
    modules = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            package = node.level == 1 or (node.level == 0 and (node.module or "").split(".")[0]
                                          == "agents")
            if not package:
                continue
            target = (node.module or "").split(".")[-1] if node.module not in (None, "agents") \
                else None
            for alias in node.names:
                local = alias.asname or alias.name
                if target and alias.name in OPT_IN_NAMES and OPT_IN_NAMES[alias.name] == target:
                    names[local] = alias.name
                elif target is None:
                    modules[local] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("agents.") and alias.asname:
                    modules[alias.asname] = alias.name.split(".")[-1]
    return names, modules


def _sources(root: Path) -> dict:
    return {p.stem: _Source(root, p) for p in sorted(root.glob("*.py"))}


def _referenced_name(src: _Source, node) -> "str | None":
    """The opt-in name ``node`` refers to in ``src``, or ``None``."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return src.names.get(node.id)
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.attr in OPT_IN_NAMES and src.modules.get(node.value.id)
            == OPT_IN_NAMES[node.attr]):
        return node.attr
    return None


def _stray_name(node) -> "str | None":
    """An opt-in name ``node`` spells that :func:`_referenced_name` did not
    resolve: a name rebound, a dotted module path, or a string (as passed to
    ``getattr``). Each is reported, so no spelling can opt in unseen."""
    if isinstance(node, ast.Name) and node.id in OPT_IN_NAMES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in OPT_IN_NAMES:
        return node.attr
    if isinstance(node, ast.Constant) and node.value in OPT_IN_NAMES:
        return node.value
    return None


def _function(sources, src: _Source, func_node):
    """``(source, FunctionDef)`` a call target refers to: a same-module
    function, one imported ``from .mod import fn``, or ``mod.fn``."""
    if isinstance(func_node, ast.Name):
        if func_node.id in src.top:
            return src, src.top[func_node.id]
        for node in ast.walk(src.tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if (alias.asname or alias.name) == func_node.id:
                        other = sources.get(node.module.split(".")[-1])
                        if other and alias.name in other.top:
                            return other, other.top[alias.name]
    if isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Name):
        other = sources.get(src.modules.get(func_node.value.id, ""))
        if other and func_node.attr in other.top:
            return other, other.top[func_node.attr]
    raise Unpinnable(f"{src.path.name}: cannot resolve {ast.unparse(func_node)}")


def _params(fn) -> list:
    return [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]


class _Scan:
    def __init__(self, root: Path):
        self.sources = _sources(root)
        #: The sites inside ``resubmit_safe_write(after_drop=True)``.
        self.after_drop: dict = defaultdict(frozenset)

    # ── statement texts ─────────────────────────────────────────────────────

    def texts(self, src, fn, node, depth=0) -> frozenset:
        if depth > 12:
            raise Unpinnable(f"{src.path.name}: too deep at {ast.unparse(node)}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return frozenset({node.value})
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return frozenset(a + b for a in self.texts(src, fn, node.left, depth + 1)
                             for b in self.texts(src, fn, node.right, depth + 1))
        if isinstance(node, ast.IfExp):
            return (self.texts(src, fn, node.body, depth + 1)
                    | self.texts(src, fn, node.orelse, depth + 1))
        if isinstance(node, ast.Name):
            local = self._local_texts(src, fn, node.id, depth)
            is_param = fn is not None and node.id in (
                _params(fn) + [a.arg for a in fn.args.kwonlyargs])
            if local is not None or is_param:
                return ((local or frozenset())
                        | (self._param_texts(src, fn, node.id, depth) if is_param else frozenset()))
            return self._module_texts(src, node, depth)
        raise Unpinnable(f"{src.path.name}:{getattr(node, 'lineno', '?')} {ast.unparse(node)}")

    def _module_texts(self, src, node, depth) -> frozenset:
        """A module-level string constant: read from its assignment when that
        is pinnable on its own, else (a constant a function builds at import,
        ``_VALUE_MAPPING_UPSERT``) the imported module's value."""
        assigned = [n.value for n in src.tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == node.id for t in n.targets)]
        if len(assigned) == 1:
            try:
                return self.texts(src, None, assigned[0], depth + 1)
            except Unpinnable:
                pass
        try:
            value = getattr(src.module, node.id, None)
        except Exception as exc:  # noqa: BLE001 - a planted module that cannot import
            raise Unpinnable(f"{src.path.name}: cannot import for {node.id}: {exc}") from exc
        if isinstance(value, str):
            return frozenset({value})
        raise Unpinnable(f"{src.path.name}:{node.lineno} {node.id} is not a string constant")

    def _local_texts(self, src, fn, name, depth):
        """Every text local ``name`` of ``fn`` can hold: the union over all
        its assignments (any of them may be the live one), or ``None`` when
        ``fn`` never assigns it."""
        if fn is None:
            return None
        stmts = sorted((n for n in ast.walk(fn)
                        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))),
                       key=lambda n: (n.lineno, n.col_offset))
        values = None
        for stmt in stmts:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(stmt, ast.AugAssign):
                        if values is None or not isinstance(stmt.op, ast.Add):
                            raise Unpinnable(f"{src.path.name}:{stmt.lineno} {ast.unparse(stmt)}")
                        values = values | frozenset(
                            v + t for v in values for t in self.texts(src, fn, stmt.value, depth + 1))
                    else:
                        values = (values or frozenset()) | self.texts(src, fn, stmt.value, depth + 1)
                elif isinstance(target, ast.Tuple) and any(
                        isinstance(e, ast.Name) and e.id == name for e in target.elts):
                    index = [getattr(e, "id", None) for e in target.elts].index(name)
                    values = (values or frozenset()) | self._element_texts(
                        src, fn, stmt.value, index, depth + 1)
        return values

    def _element_texts(self, src, fn, value, index, depth) -> frozenset:
        """Element ``index`` of every tuple ``value`` can be."""
        if isinstance(value, ast.Tuple):
            return self.texts(src, fn, value.elts[index], depth)
        if isinstance(value, ast.IfExp):
            return (self._element_texts(src, fn, value.body, index, depth)
                    | self._element_texts(src, fn, value.orelse, index, depth))
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id in src.top:
            callee = src.top[value.func.id]
            out = frozenset()
            for ret in (n for n in ast.walk(callee) if isinstance(n, ast.Return)):
                if ret.value is None:
                    raise Unpinnable(f"{src.path.name}:{ret.lineno} bare return")
                out |= self._element_texts(src, callee, ret.value, index, depth)
            return out
        raise Unpinnable(f"{src.path.name}:{value.lineno} {ast.unparse(value)}")

    def _param_texts(self, src, fn, name, depth) -> frozenset:
        """The texts every caller passes as parameter ``name`` of ``fn``: all
        callers must be same-module calls of ``fn`` (an import of it from
        another module, or a reference other than a call, is a problem)."""
        for other in self.sources.values():
            for node in ast.walk(other.tree):
                if (isinstance(node, ast.ImportFrom) and other is not src
                        and any(a.name == fn.name for a in node.names)
                        and (node.module or "").split(".")[-1] == src.path.stem):
                    raise Unpinnable(f"{fn.name} is imported by {other.path.name}")
        positional = _params(fn)
        out, calls = frozenset(), 0
        for node in ast.walk(src.tree):
            if not (isinstance(node, ast.Name) and node.id == fn.name):
                continue
            call = src.parent.get(node)
            if not (isinstance(call, ast.Call) and call.func is node):
                raise Unpinnable(f"{src.path.name}:{node.lineno} {fn.name} used other than as a call")
            arg = next((k.value for k in call.keywords if k.arg == name), None)
            if arg is None and name in positional and positional.index(name) < len(call.args):
                arg = call.args[positional.index(name)]
            if arg is None:
                raise Unpinnable(f"{src.path.name}:{call.lineno} {fn.name} called without {name}")
            out |= self.texts(src, src.enclosing(call), arg, depth + 1)
            calls += 1
        if not calls:
            raise Unpinnable(f"{fn.name} has no caller to read {name} from")
        return out

    # ── opt-in sites ────────────────────────────────────────────────────────

    def _writer_param_texts(self, callee_src, callee, param) -> frozenset:
        """The statements ``callee`` sends through its writer parameter."""
        out = frozenset()
        for node in ast.walk(callee):
            if isinstance(node, ast.Name) and node.id == param:
                call = callee_src.parent.get(node)
                if not (isinstance(call, ast.Call) and call.func is node and call.args):
                    raise Unpinnable(f"{callee_src.path.name}:{node.lineno} {callee.name} "
                                     f"passes its writer {param} on")
                out |= self.texts(callee_src, callee, call.args[0])
        if not out:
            raise Unpinnable(f"{callee.name} never calls {param}")
        return out

    def _injected(self, src, call, ref) -> frozenset:
        callee_src, callee = _function(self.sources, src, call.func)
        kw = next((k for k in call.keywords if k.value is ref), None)
        if kw is not None:
            param = kw.arg
        else:
            index = call.args.index(ref)
            positional = _params(callee)
            if index >= len(positional):
                raise Unpinnable(f"{src.path.name}:{call.lineno} writer past the parameters")
            param = positional[index]
        if param is None:
            raise Unpinnable(f"{src.path.name}:{call.lineno} writer in **kwargs")
        return self._writer_param_texts(callee_src, callee, param)

    def _after_drop(self, src, call, problems) -> "bool | None":
        """The literal ``after_drop`` of a ``resubmit_safe_write(...)`` call
        (``False`` when absent), or ``None`` (a problem) for any argument the
        scanner cannot read as one literal boolean keyword."""
        flag = False
        for kw in call.keywords:
            if (kw.arg == "after_drop" and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, bool)):
                flag = kw.value.value
            else:
                problems.append(f"{src.path.name}:{call.lineno} resubmit_safe_write() takes "
                                "only a literal after_drop=True or False")
                return None
        if call.args:
            problems.append(f"{src.path.name}:{call.lineno} resubmit_safe_write() takes "
                            "after_drop by keyword only")
            return None
        return flag

    def _with_block(self, src, fn, call, problems) -> "frozenset | None":
        item = src.parent.get(call)
        block = src.parent.get(item)
        if not (isinstance(item, ast.withitem) and isinstance(block, (ast.With, ast.AsyncWith))
                and item.context_expr is call):
            problems.append(f"{src.path.name}:{call.lineno} resubmit_safe_write() "
                            "used other than as a with-block")
            return None
        after_drop = self._after_drop(src, call, problems)
        if after_drop is None:
            return None
        body = block.body[0] if len(block.body) == 1 else None
        value = getattr(body, "value", None) if isinstance(
            body, (ast.Expr, ast.Assign, ast.Return)) else None
        seam = (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id in WITH_SEAMS)
        key = (src.path.name, getattr(fn, "name", None))
        if key in HELPER_BODIES:
            if not (seam and isinstance(body, ast.Return) and not after_drop
                    and ast.unparse(value) == HELPER_BODIES[key]):
                problems.append(f"{key[1]}: the helper body changed")
            return None
        if not seam:
            problems.append(f"{src.path.name}:{block.lineno} an opt-in block must wrap "
                            "exactly one write-seam call")
            return None
        texts = self.texts(src, fn, value.args[0])
        if after_drop:
            self.after_drop[key] |= texts
        return texts

    def run(self):
        found: dict = defaultdict(frozenset)
        problems: list = []
        for src in self.sources.values():
            for node in ast.walk(src.tree):
                name = _referenced_name(src, node)
                if name is None:
                    stray = _stray_name(node)
                    if stray:
                        problems.append(f"{src.path.name}:{getattr(node, 'lineno', '?')}: "
                                        f"cannot resolve this use of {stray}")
                    continue
                fn = src.enclosing(node)
                where = f"{src.path.name}:{node.lineno}"
                if fn is None:
                    problems.append(f"{where}: {name} referenced outside a function")
                    continue
                parent = src.parent.get(node)
                try:
                    if isinstance(parent, ast.Call) and parent.func is node:
                        if name == "resubmit_safe_write":
                            texts = self._with_block(src, fn, parent, problems)
                        else:
                            texts = self.texts(src, fn, parent.args[0])
                    elif isinstance(parent, ast.keyword) and name in WRITE_HELPERS:
                        texts = self._injected(src, src.parent[parent], node)
                    elif (isinstance(parent, ast.Call) and node in parent.args
                          and name in WRITE_HELPERS):
                        texts = self._injected(src, parent, node)
                    else:
                        problems.append(f"{where} {fn.name}: {name} used other than as a "
                                        "call, an injected writer or a with-block")
                        continue
                except Unpinnable as exc:
                    problems.append(f"{where} {fn.name}: unpinnable: {exc}")
                    continue
                if texts is not None:
                    found[(src.path.name, fn.name)] |= texts
        return dict(found), problems


def opted_in_statements(root: Path = AGENTS):
    """``({(file, function): frozenset of statements}, problems)`` for every
    opted-in write under ``root``; each problem is a reference to an opt-in
    name this reader cannot tie to one pinned write (each fails the pin test)."""
    return _Scan(root).run()


def opted_in_after_drop(root: Path = AGENTS):
    """``({(file, function): frozenset of statements}, problems)`` for the
    opted-in writes that are also sent again after a dropped connection
    (``resubmit_safe_write(after_drop=True)``): a subset of
    :func:`opted_in_statements`."""
    scan = _Scan(root)
    _found, problems = scan.run()
    return dict(scan.after_drop), problems
