# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every route that can write what the startup restore writes carries the
startup-restore guard (``startup_restore``), or each writer it reaches is
listed here with why it does not race the restore.

The startup restore (``server._restore``) can still be running in the
background after the app starts serving. It writes the system registry (the
catalog entries and the orchestrator's agent list), starts or reuses each
system's runtime and gateway target (``AgentManager.start_agent``, which it
replaces for its whole pass) and writes the agent config, and it reads each
system's connection, schema and System node and writes Concept and schema
provenance graph records. A route that writes any of these while it runs
can be overwritten by the restore or leave a record the restore then
contradicts.

The scan is static: from each mounted route's handler it follows the names
the handler's code references (a call, or a function handed to ``offload``
or a thread), through any module under ``agents/``, and reports each
restore-competing writer it reaches (catalog and ``AgentManager`` writes,
AgentCore control-plane writes, :data:`SINK_FUNCTIONS`, and Cypher that
writes what the restore reads or writes, see :func:`_cypher_sinks`) with the
function it sits in. Reaching is an over-approximation: a reference on a
branch the route never takes counts.

Guards. A FastAPI dependency guard (``Depends(refuse_while_restoring)`` or
the admin-first variant) runs before the handler, so a route carrying one
is guarded whatever its handler does. An in-body guard counts only when the
function OPENS with it (:meth:`_Scan.opens_with_guard`): a recognized
top-level statement whose result is acted on, with every decorator, every
parameter default and every statement before it reaching no unexplained
writer. Then the walk does
not enter that function. A guard after a write, an ignored ``refusal()``
result or a guard under a branch does not count, and the walk goes on into
what the function reaches. A route whose refusal is in its body under a
condition (refused only when it would write) is listed in
:data:`IN_BODY_GUARDED` with the function that holds that conditional
refusal, the exact writer pairs it reaches after it, and the behavioral
tests that prove the refusal comes first. Nothing before the refusal is
excused: every decorator, every parameter default, every statement before
the refusal's ``if`` and the ``if`` test must reach no unexplained writer,
and neither may anything the handler reaches other than through the
refusing function. After the
refusal the listing covers only the pairs it names: any other writer fails
the route, and a named pair the route no longer reaches makes the entry
stale.

Limits. The scan follows names written in the code. It does not follow
calls it cannot resolve by name (``getattr`` with a computed name,
``globals()``, ``exec``, a callable fetched from a container at run time),
so it is a regression net for ordinary route code, not a proof against
code written to evade it.

So a route carries a dependency guard, or is listed, or every writer it
reaches outside a recognized guard is in :data:`NOT_RACING_WRITERS` with the
reason it does not race. Anything else fails :func:`test_every_route_that_
reaches_a_restore_competing_writer_is_guarded_or_explained`; the planted
routes in :func:`test_the_scan_rejects_a_guard_that_does_not_come_first` and
:func:`test_a_listed_route_fails_on_a_writer_its_entry_does_not_name` pin the
forms it must reject.
"""
from __future__ import annotations

import ast
import re
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import pytest

_APP = Path(__file__).resolve().parents[2]
_AGENTS = _APP / "agents"

#: The raising guards, callable as a dependency or in a body.
GUARD_NAMES = frozenset({"refuse_while_restoring", "admin_refuse_while_restoring"})

#: Catalog methods that write an entry the restore writes (``catalog.add``,
#: ``update_status``, ``set_system_type``, ``set_connection_origin``,
#: ``ensure_removal_failed``) or remove one it is rebuilding.
CATALOG_WRITES = frozenset({
    "add", "remove", "remove_entry", "update_status", "set_system_type",
    "set_connection_origin", "ensure_removal_failed", "mark_removing",
    "mark_removal_failed", "reset_all", "clear_all", "set_connection_config",
    "claim_schema", "reserve_schema", "commit_schema",
})
#: ``AgentManager`` methods that create, change or remove a system's runtime,
#: gateway target, agent config or in-memory registration.
MANAGER_WRITES = frozenset({
    "start_agent", "stop_agent", "stop_all", "delete_cloud_agent",
    "ensure_digital_thread_target", "ensure_query_bridge_target",
    "resync_cloud_connection", "refresh_business_context",
    "refresh_agent_model", "refresh_systems_batched",
})
#: AgentCore control-plane calls that change a runtime or gateway target.
CONTROL_WRITES = frozenset({
    "create_agent_runtime", "update_agent_runtime", "delete_agent_runtime",
    "create_gateway_target", "update_gateway_target", "delete_gateway_target",
    "synchronize_gateway_targets",
})
#: Functions that write what the restore writes, by ``module:qualname``.
SINK_FUNCTIONS = {
    "agents.orchestrator:add_system_agent": "registry",
    "agents.orchestrator:remove_system_agent": "registry",
    "agents.orchestrator:reset_agents": "registry",
    "agents.server:_restore_integrity": "restore",
    "agents.server:_restore_base": "restore",
    "agents.graph_snapshot:import_snapshot": "graph-wipe",
    "agents.graph_snapshot:repair_systems": "restore",
    "agents.onboard:_persist_schema_relation_kinds": "graph",
}
#: A Cypher literal that writes a node kind the restore reads or writes:
#: System (its connection and status), SchemaEntry and Field (the schema it
#: rebuilds an agent from), Concept (its seed); or a whole-graph delete.
_RESTORE_LABELS = ("System", "SchemaEntry", "Field", "Concept")
_NODE = re.compile(r"\(\s*(\w*)\s*:\s*(System|SchemaEntry|Field|Concept)\b")
_CREATE_NODE = re.compile(r"\b(MERGE|CREATE)\s*\(\s*\w*\s*:\s*(System|SchemaEntry|Field|Concept)\b",
                          re.I)
#: A node pattern whose label is an f-string field (``(n:{label} ...)``).
_DYNAMIC_NODE = re.compile(r"\(\s*(\w+)\s*:\s*\{\}")
_CREATE_DYNAMIC = re.compile(r"\b(MERGE|CREATE)\s*\(\s*\w*\s*:\s*\{\}", re.I)
_DELETE = re.compile(r"\b(?:DETACH\s+)?DELETE\s+(\w+(?:\s*,\s*\w+)*)", re.I)
_SET_CLAUSE = re.compile(r"\b(SET|REMOVE)\b(.*?)(?=\b(?:RETURN|WITH|MERGE|MATCH|CREATE|DELETE|"
                         r"DETACH|UNWIND|WHERE|ON|FOREACH|CALL|OPTIONAL)\b|$)", re.I | re.S)
_ASSIGNED = re.compile(r"\b(\w+)\s*(?:\.\s*(\w+)\s*=|\+=|=\s*[{$]|:\w+)")
_REMOVED = re.compile(r"\b(\w+)\s*\.\s*(\w+)")
#: The node properties the restore reads or writes, by label. A write of any
#: other property (a System's query guidance or business context, a Field's
#: annotation) is not read by the restore. ``*`` is a whole-map SET.
RESTORE_PROPERTIES = {
    "System": frozenset({"id", "name", "schema", "plant", "connection_type", "system_type",
                         "connection_origin", "removal_incomplete", "removal_full", "status",
                         "connection_config", "secret_arn", "*"}),
    "SchemaEntry": frozenset({"id", "name", "relation_kind", "*"}),
    "Field": frozenset({"id", "name", "data_type", "is_partition", "*"}),
    "Concept": frozenset(),
}


def _cypher_sinks(text: str) -> set:
    """``graph-write:<Label>:<what>`` for each write in ``text`` that the
    restore competes with: a create or delete of a System, SchemaEntry or
    Field node, a delete of a Concept node or of an unlabelled ``(n)`` match,
    and a SET/REMOVE of a property in :data:`RESTORE_PROPERTIES`. A Concept
    MERGE is idempotent with the restore's ON CREATE seed and is not one."""
    found = {f"graph-write:{label}:{clause.upper()}"
             for clause, label in _CREATE_NODE.findall(text) if label != "Concept"}
    bound = {var: label for var, label in _NODE.findall(text) if var}
    for var in _DYNAMIC_NODE.findall(text):  # an f-string label: unknown
        bound.setdefault(var, "?")
    found |= {f"graph-write:?:{clause.upper()}" for clause in _CREATE_DYNAMIC.findall(text)}
    for targets in _DELETE.findall(text):
        for var in (v.strip() for v in targets.split(",")):
            if var in bound:
                found.add(f"graph-write:{bound[var]}:DELETE")
            elif var and re.search(rf"MATCH\s*\(\s*{re.escape(var)}\s*\)", text):
                found.add("graph-write:*:DELETE")
    for clause, body in _SET_CLAUSE.findall(text):
        pattern = _REMOVED if clause.upper() == "REMOVE" else _ASSIGNED
        for var, prop in pattern.findall(body):
            label = bound.get(var)
            if label and label != "?" and (prop or "*") in RESTORE_PROPERTIES[label]:
                found.add(f"graph-write:{label}:{clause.upper()} {prop or '*'}")
    return found

#: ``(writer, site) -> why it does not race``: writers left unguarded. A
#: route that reaches only these (or reaches others only behind a guard)
#: needs no guard. Each is checked to still be at its site.
_SCHEMA_MATERIALISE = (
    "additive schema materialisation (schema_fields, ADR-0077: a column or table the "
    "source has, MERGEd under an onboarded system after an accepted drift row). The "
    "restore never deletes or rewrites it: its provenance SET matches only the fields it "
    "read, and both write a field's data type from the same physical source. An agent "
    "the restore built before the MERGE lacks the new column exactly as one built at a "
    "start before the accept does; the SCHEMA_ENTRY_MERGE System MERGE only matches, "
    "because materialise_drift refuses a table whose system is not onboarded")
NOT_RACING_WRITERS: dict = {
    ("catalog.set_connection_config", "agents.onboard:_persist_catalog_config_from_body"): (
        "sets a sanitized, non-secret connection config on a catalog entry that has none; "
        "the restore never writes an entry's connection_config"),
    ("catalog.set_connection_config", "agents.onboard:_lazy_backfill_connection_config"): (
        "GET /catalog copies the node's sanitized config onto an entry that has none, once "
        "per process; the restore never writes an entry's connection_config"),
    ("catalog.set_connection_origin", "agents.onboard:_pg_entry_with_origin"): (
        "marks an unmarked entry 'user' when its node points at its own bound secret: the "
        "same marker from the same node and secret the restore writes "
        "(_restore_mark_user_origin), so either order leaves the same entry"),
    ("catalog.ensure_removal_failed", "agents.onboard_agent:_removal_failed_refusal"): (
        "rebuilds a 'removal_failed' entry from the node's removal_incomplete marker, the "
        "call the restore makes for that node (idempotent: _restore_removal_failed)"),
    ("catalog.ensure_removal_failed", "agents.onboard_agent:_unfinished_removal_handoff"): (
        "same rebuild of a 'removal_failed' entry from the node's marker as the restore's"),
    ("graph-write:?:DELETE", "agents.self_learning:_undo_graph_artifact"): (
        "the f-string label comes from _REQUEUE_DELETE_LABELS (ValueMapping, JoinPath, "
        "Correlation): learning records the restore neither reads nor writes"),
    ("graph-write:?:DELETE", "agents.self_learning:clear_all_learnings"): (
        "the f-string label is one of JoinPath, ValueMapping, IdentityRule: learning "
        "records the restore neither reads nor writes"),
    ("graph-write:Concept:DELETE", "agents.admin_routes:uninstall_concept_pack"): (
        "deletes only Concept nodes whose source is 'pack:<id>' and that nothing maps to; "
        "the restore's seed writes only source 'core' nodes and reads no Concept"),
    **{(sink, "agents.schema_fields:FIELD_MERGE"): _SCHEMA_MATERIALISE for sink in (
        "graph-write:Field:MERGE", "graph-write:Field:SET data_type",
        "graph-write:Field:SET is_partition", "graph-write:Field:SET name")},
    **{(sink, "agents.schema_fields:SCHEMA_ENTRY_MERGE"): _SCHEMA_MATERIALISE for sink in (
        "graph-write:SchemaEntry:MERGE", "graph-write:SchemaEntry:SET name",
        "graph-write:System:MERGE", "graph-write:System:SET name",
        "graph-write:System:SET status")},
}


# ── The module index ─────────────────────────────────────────────────────────

def _module_name(path: Path) -> str:
    parts = list(path.relative_to(_APP).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package(module: str, is_init: bool) -> str:
    return module if is_init else module.rsplit(".", 1)[0]


#: The module-level assignments the scan follows: an alias (``_restore =
#: _restore_integrity``), a Cypher string, and a dispatch table or lambda. A
#: constructed object (``app = FastAPI(..., lifespan=lifespan)``, a router, a
#: lock) is not followed: through it every route would reach every startup step.
_FOLLOWED_VALUES = (ast.Name, ast.Attribute, ast.Constant, ast.JoinedStr, ast.BinOp, ast.Dict,
                    ast.List, ast.Tuple, ast.Set, ast.Lambda)


class _Module:
    def __init__(self, name: str, source: str, is_package: bool = False):
        self.name = name
        self.package = _package(self.name, is_package)
        self.tree = ast.parse(source)
        self.defs: dict = {}      # name -> function node
        self.values: dict = {}    # name -> module-level assigned expressions
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs[node.name] = node
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.defs[f"{node.name}.{item.name}"] = item
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
                    node.value, _FOLLOWED_VALUES):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.values.setdefault(target.id, []).append(node.value)
        self.imports = _imports(self.tree.body, self)
        # A function stored as a keyword of a constructed record in a
        # module-level value (``RestVendorAdapter(provision_agent=_x)``) is
        # reached where the code reads that field (``adapter.provision_agent``),
        # not by every reader of the value (see :func:`_fields`).
        self.fields: dict = {}
        self.field_names: set = set()
        for value in (v for vs in self.values.values() for v in vs):
            for sub in ast.walk(value):
                if isinstance(sub, ast.keyword) and sub.arg and isinstance(sub.value, ast.Name) \
                        and self.knows(sub.value.id):
                    self.fields.setdefault(sub.arg, set()).add(f"{self.name}:{sub.value.id}")
                    self.field_names.add(id(sub.value))

    def knows(self, name: str) -> bool:
        return name in self.defs or name in self.values


def _resolve(module: "_Module", level: int, name: str | None) -> str:
    if level == 0:
        return name or ""
    base = module.package.split(".")
    base = base[:len(base) - (level - 1)] if level > 1 else base
    return ".".join(base + ([name] if name else []))


def _imports(body, module: "_Module") -> dict:
    """``alias -> ("module", name)`` or ``("object", module, attr)``."""
    found: dict = {}
    for node in body if isinstance(body, list) else [body]:
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom):
                source = _resolve(module, sub.level, sub.module)
                for alias in sub.names:
                    found[alias.asname or alias.name] = ("object", source, alias.name)
            elif isinstance(sub, ast.Import):
                for alias in sub.names:
                    found[alias.asname or alias.name.split(".")[0]] = (
                        "module", alias.name if alias.asname else alias.name.split(".")[0])
    return found


@lru_cache(maxsize=1)
def _index() -> dict:
    modules = {}
    for path in sorted(_AGENTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _Module(_module_name(path), path.read_text(encoding="utf-8"),
                         path.name == "__init__.py")
        modules[module.name] = module
    return modules


def _record_fields(modules: dict) -> dict:
    """``field name -> {module:name}``: every function stored under that
    keyword in a module-level record (see ``_Module.fields``)."""
    out: dict = {}
    for module in modules.values():
        for name, keys in module.fields.items():
            out.setdefault(name, set()).update(keys)
    return out


@lru_cache(maxsize=1)
def _fields() -> dict:
    return _record_fields(_index())


def _target(modules: dict, imported) -> "str | None":
    """The ``module:name`` node an imported object names, or ``None``."""
    if imported[0] == "object":
        _kind, source, attr = imported
        if source in modules and modules[source].knows(attr):
            return f"{source}:{attr}"
    return None


def _imported_module(modules: dict, imported) -> "str | None":
    if imported[0] == "module":
        return imported[1] if imported[1] in modules else None
    _kind, source, attr = imported
    full = f"{source}.{attr}"
    return full if full in modules else None


def _receiver_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _receiver_name(node.func)
    return ""


def _attribute_sink(module: "_Module", node: ast.Attribute) -> "str | None":
    receiver = _receiver_name(node.value).lower()
    if node.attr in CATALOG_WRITES and receiver.endswith("catalog"):
        return f"catalog.{node.attr}"
    if node.attr in MANAGER_WRITES and module.name != "agents.agent_manager" \
            and receiver != "self":
        return f"AgentManager.{node.attr}"
    if node.attr in CONTROL_WRITES:
        return f"control.{node.attr}"
    return None


def _edges(modules: dict, module: "_Module", qualname: str, nodes: list, fields=None):
    """``(callees, sinks)`` of one function or module-level value (or some of
    its statements): the ``module:name`` nodes the code references and the
    writers it holds."""
    names = dict(module.imports)
    for node in nodes:
        names.update(_imports(node, module))
    callees, sinks = set(), set()
    owner = qualname.split(".")[0] if "." in qualname else None
    fields = _fields() if fields is None else fields
    for sub in (sub for node in nodes for sub in ast.walk(node)):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            if id(sub) in module.field_names:
                continue  # reached through the field, not through the record
            if sub.id in names:
                target = _target(modules, names[sub.id])
                if target:
                    callees.add(target)
            elif module.knows(sub.id):
                callees.add(f"{module.name}:{sub.id}")
        elif isinstance(sub, ast.Attribute):
            callees |= fields.get(sub.attr, set())
            sink = _attribute_sink(module, sub)
            if sink:
                sinks.add(sink)
            value = sub.value
            if isinstance(value, ast.Name) and value.id in names:
                imported = _imported_module(modules, names[value.id])
                if imported and modules[imported].knows(sub.attr):
                    callees.add(f"{imported}:{sub.attr}")
            elif isinstance(value, ast.Name) and value.id == "self" and owner:
                if f"{owner}.{sub.attr}" in module.defs:
                    callees.add(f"{module.name}:{owner}.{sub.attr}")
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            sinks |= _cypher_sinks(sub.value)
        if isinstance(sub, ast.Call) and _receiver_name(sub.func) == "getattr" \
                and len(sub.args) >= 2 and isinstance(sub.args[1], ast.Constant) \
                and sub.args[1].value in MANAGER_WRITES:
            sinks.add(f"AgentManager.{sub.args[1].value}")
        elif isinstance(sub, ast.JoinedStr):
            sinks |= _cypher_sinks("".join(
                part.value if isinstance(part, ast.Constant) else "{}" for part in sub.values))
    return callees, sinks


def _node_edges(modules: dict, key: str, fields=None):
    module_name, qualname = key.split(":", 1)
    module = modules[module_name]
    nodes = ([module.defs[qualname]] if qualname in module.defs else []) \
        + module.values.get(qualname, [])
    callees, sinks = _edges(modules, module, qualname, nodes, fields)
    if key in SINK_FUNCTIONS:
        sinks.add(f"{SINK_FUNCTIONS[key]}:{qualname}")
    return callees, sinks


# ── In-body guards ───────────────────────────────────────────────────────────
# A refusal in a function body counts only in a form that refuses before the
# function does anything else: a top-level statement of the body (not under
# an ``if``, ``try`` or loop) whose result is acted on, with every decorator
# and every statement before it reaching no writer the restore competes with
# (an explained, non-racing writer is allowed). Anything else (a guard after
# a write, an ignored ``refusal()``, a guard under a branch) does not count,
# and the walk goes on into what the function reaches.

def _call_name(node) -> str:
    if isinstance(node, ast.Call):
        func = node.func
        return func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else "")
    return ""


def _is_refusal(node) -> bool:
    """``startup_restore.refusal(...)``, alone or as the FIRST operand of an
    ``or`` (nothing after it runs while it answers)."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        node = node.values[0]
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "refusal" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "startup_restore")


def _returns_name(stmt, name: str) -> bool:
    """``if <name>: return <name>``, with no ``else``."""
    return (isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Name)
            and stmt.test.id == name and not stmt.orelse and len(stmt.body) == 1
            and isinstance(stmt.body[0], ast.Return)
            and isinstance(stmt.body[0].value, ast.Name) and stmt.body[0].value.id == name)


def _guard_index(body: list) -> "int | None":
    """The index of the first top-level statement of ``body`` that refuses
    while the restore runs, in a form whose result is acted on, else None:
    ``refuse_while_restoring()`` / ``admin_refuse_while_restoring(...)`` as a
    statement (it raises); ``return startup_restore.refusal(...) [or ...]``;
    or ``x = startup_restore.refusal(...) [or ...]`` followed at once by
    ``if x: return x``."""
    for index, stmt in enumerate(body):
        if isinstance(stmt, ast.Expr) and _call_name(stmt.value) in GUARD_NAMES:
            return index
        if isinstance(stmt, ast.Return) and stmt.value is not None \
                and _is_refusal(stmt.value):
            return index
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and _is_refusal(stmt.value)
                and index + 1 < len(body)
                and _returns_name(body[index + 1], stmt.targets[0].id)):
            return index
    return None


def _before_the_body(node) -> list:
    """What a function reaches before its first statement runs: its
    decorators and its parameter defaults (positional and keyword-only).
    A default that names a writer counts as reached, as if called, since the
    body may call it through the parameter before any refusal."""
    args = node.args
    defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
    return list(node.decorator_list) + defaults


def _conditional_refusal_index(body: list) -> "int | None":
    """The index of the first top-level ``if`` of ``body`` that has no
    ``else`` and whose branch opens with a recognized refusal
    (:func:`_guard_index` 0), else None: the refusal of an
    :data:`IN_BODY_GUARDED` route."""
    for index, stmt in enumerate(body):
        if isinstance(stmt, ast.If) and not stmt.orelse and _guard_index(stmt.body) == 0:
            return index
    return None


class _Scan:
    """The writer scan over one module index: the real one (:func:`_scan`),
    or one with a planted module in a test."""

    def __init__(self, modules: dict):
        self.modules = modules
        self.fields = _record_fields(modules)
        self._reach: dict = {}
        self._opens: dict = {}
        self._edges: dict = {}

    def edges(self, key: str):
        """``(callees, sinks)`` of ``key``, computed once per scan: every walk
        (one per route and guard) asks again for the functions it shares."""
        if key not in self._edges:
            callees, sinks = _node_edges(self.modules, key, self.fields)
            self._edges[key] = (frozenset(callees), frozenset(sinks))
        return self._edges[key]

    def opens_with_guard(self, key: str) -> bool:
        """Whether the function ``key`` refuses before anything it does can
        reach an unexplained writer (see the section comment above)."""
        if key not in self._opens:
            self._opens[key] = self._compute_opens(key)
        return self._opens[key]

    def _compute_opens(self, key: str) -> bool:
        module_name, qualname = key.split(":", 1)
        module = self.modules.get(module_name)
        node = module.defs.get(qualname) if module else None
        index = _guard_index(node.body) if node is not None else None
        if index is None:
            return False
        before = _before_the_body(node) + node.body[:index]
        callees, sinks = _edges(self.modules, module, qualname, before, self.fields)
        pairs = {(sink, key) for sink in sinks}
        for callee in callees:
            pairs |= {pair for pair, _chain in self.reach(callee)}
        return not pairs - set(NOT_RACING_WRITERS)

    def reach(self, key: str, through_guards: bool = True) -> tuple:
        """``(((sink, site), chain), ...)``: every writer reachable from
        ``key``, each with the ``module:name`` it occurs in (its site) and the
        shortest chain of names from ``key`` to that site. With
        ``through_guards`` false the walk does not enter a function that opens
        with a guard (:meth:`opens_with_guard`), ``key`` itself included: what
        it reaches is behind the refusal."""
        if (key, through_guards) not in self._reach:
            self._reach[(key, through_guards)] = self._walk(key, through_guards)
        return self._reach[(key, through_guards)]

    def _walk(self, key: str, through_guards: bool, stop: str = "") -> tuple:
        seen = {key: (key,)}
        queue = deque([key])
        found: dict = {}
        while queue:
            current = queue.popleft()
            if current.split(":", 1)[0] not in self.modules or current == stop:
                continue
            if not through_guards and self.opens_with_guard(current):
                continue
            callees, sinks = self.edges(current)
            for sink in sinks:
                found.setdefault((sink, current), seen[current])
            for callee in sorted(callees):
                if callee not in seen:
                    seen[callee] = seen[current] + (callee,)
                    queue.append(callee)
        return tuple(sorted(found.items()))

    def open_pairs(self, key: str) -> frozenset:
        """The ``(sink, site)`` pairs ``key`` reaches outside a guard."""
        return frozenset(pair for pair, _chain in self.reach(key, False))

    def before_conditional_refusal(self, handler: str, refuses_in: str) -> frozenset:
        """The unexplained ``(sink, site)`` pairs a listed route can reach
        before its conditional refusal (:func:`_conditional_refusal_index`) in
        ``refuses_in``: from that function's decorators and parameter
        defaults, the statements before
        the refusal's ``if`` and the ``if`` test, and from whatever ``handler``
        reaches other than through ``refuses_in``. A ``refuses_in`` with no
        conditional refusal answers ``("conditional-refusal-missing",
        refuses_in)``."""
        module_name, qualname = refuses_in.split(":", 1)
        module = self.modules.get(module_name)
        node = module.defs.get(qualname) if module else None
        index = _conditional_refusal_index(node.body) if node is not None else None
        if index is None:
            return frozenset({("conditional-refusal-missing", refuses_in)})
        before = _before_the_body(node) + node.body[:index] + [node.body[index].test]
        callees, sinks = _edges(self.modules, module, qualname, before, self.fields)
        pairs = {(sink, refuses_in) for sink in sinks}
        for callee in callees:
            pairs |= {pair for pair, _chain in self.reach(callee)}
        if handler != refuses_in:
            pairs |= {pair for pair, _chain in self._walk(handler, False, stop=refuses_in)}
        return frozenset(pairs - set(NOT_RACING_WRITERS))


@lru_cache(maxsize=1)
def _scan() -> _Scan:
    return _Scan(_index())


def reached_writers(key: str, through_guards: bool = True) -> tuple:
    return _scan().reach(key, through_guards)


# ── The mounted routes ───────────────────────────────────────────────────────

def _api_routes(router, inherited=(), prefix=""):
    """``(route, path, dependencies)`` for every APIRoute under ``router``,
    descending through included routers (FastAPI at the deployment lock keeps
    ``include_router`` lazy: the app's own list holds one wrapper per include,
    whose context carries the include's prefix and dependencies)."""
    from fastapi.routing import APIRoute
    for route in getattr(router, "routes", []):
        ctx = getattr(route, "include_context", None)
        if ctx is not None:
            deps = tuple(inherited) + tuple(getattr(ctx, "dependencies", None) or ())
            yield from _api_routes(getattr(ctx, "included_router", None), deps,
                                   prefix + (getattr(ctx, "prefix", "") or ""))
        elif isinstance(route, APIRoute):
            yield route, prefix + route.path, tuple(inherited) + tuple(route.dependencies)


class Listed(NamedTuple):
    """One :data:`IN_BODY_GUARDED` entry: the behavioral tests that prove the
    route refuses before it writes, the exact ``(sink, site)`` pairs it
    reaches after its conditional refusal that those tests cover, and the
    ``module:name`` of the function holding that refusal (empty: the route's
    handler). Nothing the route can reach before the refusal is excused
    (:meth:`_Scan.before_conditional_refusal`)."""
    tests: tuple
    pairs: frozenset
    refuses_in: str = ""


#: ``(method, path) -> Listed``: routes whose refusal is in the body in a form
#: the scan does not read as coming before every write (a refusal under a
#: condition the write shares). Each names the function holding the
#: conditional refusal, the behavioral tests that prove the refusal comes
#: before the write, and the writer pairs reached after it. The listing
#: excuses only those pairs and only after the refusal: a writer reachable
#: before it, or a writer not named here, fails the route
#: (:func:`route_problems`), and a named pair the route no longer reaches
#: makes the entry stale.
IN_BODY_GUARDED: dict = {
    ("POST", "/api/admin/graph/snapshot/reconcile"): Listed(
        # refuses only when the body asks for the repair; the check stays
        tests=(
            "tests/unit/test_startup_restore_routes.py::"
            "test_a_repair_reconcile_claims_nothing_during_a_restore",
            "tests/unit/test_startup_restore_routes.py::"
            "test_a_check_only_reconcile_still_answers_during_a_restore",
        ),
        # the repair job runs a second restore pass (server._restore)
        pairs=frozenset({
            ("AgentManager.start_agent", "agents.server:_restore_integrity"),
            ("AgentManager.start_agent", "agents.server:_restore_start"),
            ("catalog.add", "agents.server:_restore_direct_catalog_entry"),
            ("catalog.add", "agents.server:_restore_sql_catalog_entry"),
            ("catalog.ensure_removal_failed", "agents.server:_restore_removal_failed"),
            ("catalog.set_connection_origin", "agents.server:_restore_mark_user_origin"),
            ("catalog.set_system_type", "agents.server:_restore_direct_catalog_entry"),
            ("catalog.set_system_type", "agents.server:_restore_sql_catalog_entry"),
            ("catalog.update_status", "agents.server:_restore_direct_source_system"),
            ("catalog.update_status", "agents.server:_restore_sql_catalog_entry"),
            ("graph-write:Field:SET data_type", "agents.onboard:_persist_schema_relation_kinds"),
            ("graph-write:SchemaEntry:SET relation_kind",
             "agents.onboard:_persist_schema_relation_kinds"),
            ("graph:_persist_schema_relation_kinds",
             "agents.onboard:_persist_schema_relation_kinds"),
            ("registry:add_system_agent", "agents.orchestrator:add_system_agent"),
            ("restore:_restore_base", "agents.server:_restore_base"),
            ("restore:_restore_integrity", "agents.server:_restore_integrity"),
            ("restore:repair_systems", "agents.graph_snapshot:repair_systems"),
        }),
        refuses_in="agents.graph_snapshot_routes:reconcile",
    ),
    ("POST", "/api/neptune/graph/repair"): Listed(
        # refuses unless the body asks for a dry run, which writes nothing
        tests=(
            "tests/unit/test_startup_restore_routes.py::"
            "test_a_writing_graph_repair_is_refused_during_a_restore",
            "tests/unit/test_startup_restore_routes.py::"
            "test_a_dry_run_graph_repair_runs_during_a_restore",
        ),
        pairs=frozenset({
            ("graph-write:?:DELETE", "agents.server:_delete_orphan_nodes"),
            ("graph-write:System:MERGE", "agents.server:_repair_system_edges"),
            ("graph-write:System:SET name", "agents.server:_repair_system_edges"),
            ("graph-write:System:SET status", "agents.server:_repair_system_edges"),
        }),
    ),
}


def _dependency_guarded(deps) -> bool:
    """A FastAPI dependency guard: it runs before the handler, so it is safe
    whatever the handler does."""
    return any(getattr(dep.dependency, "__name__", "") in GUARD_NAMES for dep in deps)


def _unexplained_open_pairs(scan: _Scan, key: str) -> frozenset:
    return frozenset(scan.open_pairs(key) - set(NOT_RACING_WRITERS))


def route_problems(scan: _Scan, key: str, deps, route_id, listed: dict | None = None) -> list:
    """The ``(sink, site)`` pairs that fail a route: none when a dependency
    guards it; else every pair its handler reaches outside a recognized guard
    that NOT_RACING_WRITERS does not explain. For a route in ``listed``
    (default :data:`IN_BODY_GUARDED`) the pairs its entry names are excused,
    but only after its conditional refusal: whatever it can reach before the
    refusal fails it, named or not."""
    listed = IN_BODY_GUARDED if listed is None else listed
    if _dependency_guarded(deps):
        return []
    if route_id not in listed:
        return sorted(_unexplained_open_pairs(scan, key))
    entry = listed[route_id]
    after = _unexplained_open_pairs(scan, key) - entry.pairs
    return sorted(after | scan.before_conditional_refusal(key, entry.refuses_in or key))


def stale_listed_pairs(scan: _Scan, key: str, entry: Listed) -> list:
    """The pairs ``entry`` names that the route ``key`` no longer reaches
    outside a recognized guard (each must leave the entry)."""
    return sorted(entry.pairs - _unexplained_open_pairs(scan, key))


@lru_cache(maxsize=1)
def route_inventory() -> tuple:
    """``((method, path, handler, guard, writers, open_pairs, problems), ...)``
    for every mounted route whose handler reaches a restore-competing writer:
    ``guard`` is ``"dependency"``, ``"listed"`` (:data:`IN_BODY_GUARDED`) or
    ``""``; ``writers`` as :meth:`_Scan.reach` gives them; ``open_pairs`` the
    pairs reached outside a guard; ``problems`` as :func:`route_problems`."""
    from agents import server
    scan = _scan()
    rows = []
    for route, path, deps in _api_routes(server.app):
        key = f"{route.endpoint.__module__}:{route.endpoint.__qualname__}"
        writers = scan.reach(key)
        if not writers:
            continue
        for method in sorted(route.methods):
            guard = ("dependency" if _dependency_guarded(deps)
                     else "listed" if (method, path) in IN_BODY_GUARDED else "")
            rows.append((method, path, key, guard, writers, scan.open_pairs(key),
                         route_problems(scan, key, deps, (method, path))))
    return tuple(sorted(rows))


def _direct_sinks(site: str) -> set:
    return _scan().edges(site)[1] if site.split(":", 1)[0] in _index() else set()


# ── Tests ────────────────────────────────────────────────────────────────────

def _kinds(writers) -> set:
    return {sink.split(":", 1)[0] for (sink, _site), _chain in writers}


def test_the_scan_finds_the_restore_competing_writers_it_is_built_for():
    """Positive controls: the scan reaches each kind of writer from a route
    known to write it."""
    by_route = {(m, p): _kinds(w) for m, p, _h, _g, w, _o, _x in route_inventory()}
    assert "catalog.add" in by_route[("POST", "/api/onboard/connect")]
    assert "AgentManager.start_agent" in by_route[("POST", "/api/onboard/provision/confirm")]
    assert "graph-wipe" in by_route[("POST", "/api/admin/graph/snapshot/import")]
    assert "restore" in by_route[("POST", "/api/admin/graph/snapshot/reconcile")]
    assert "registry" in by_route[("POST", "/api/onboard/reset/{system_id}")]
    assert "control.update_agent_runtime" in by_route[("POST", "/api/admin/restore-runtime-auth")]
    assert "graph-write" in by_route[("PUT", "/api/admin/scan-budget/{system_id}")]
    assert "graph-write" in by_route[("POST", "/api/neptune/graph/repair")]


def test_the_cypher_scan_reads_the_writes_the_restore_competes_with():
    assert _cypher_sinks("MATCH (s:System {id: $sid}) SET s.status = 'x', s.name = $n") == {
        "graph-write:System:SET status", "graph-write:System:SET name"}
    assert _cypher_sinks("MATCH (s:System {id: $sid}) SET s.query_guidance = $g") == set()
    assert _cypher_sinks("MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $n") == set()
    assert _cypher_sinks("MATCH (f:Field {id: $fid}) DETACH DELETE f") == {
        "graph-write:Field:DELETE"}
    assert _cypher_sinks("MATCH (n) WITH n LIMIT 5 DETACH DELETE n") == {"graph-write:*:DELETE"}
    assert _cypher_sinks("OPTIONAL MATCH (ir)-[old:LINKS_CONCEPT]->(:Concept) DELETE old "
                         "WITH ir MATCH (ir)") == set()


def test_every_route_that_reaches_a_restore_competing_writer_is_guarded_or_explained():
    unexplained = {(m, p): problems for m, p, _h, _g, _w, _o, problems in route_inventory()
                   if problems}
    assert unexplained == {}, (
        "these routes can write what the startup restore writes before any refusal: "
        "add Depends(refuse_while_restoring) (or the admin-first variant), open the "
        "handler with a recognized refusal, list the route in IN_BODY_GUARDED with the "
        "behavioral tests that prove its refusal comes first, or add the (writer, site) "
        "pair to NOT_RACING_WRITERS with why it does not race")


def test_every_explained_writer_is_still_there():
    stale = {(sink, site) for sink, site in NOT_RACING_WRITERS if sink not in _direct_sinks(site)}
    assert stale == set(), "drop these NOT_RACING_WRITERS entries: the writer is gone"


def stale_listed_entries(scan: _Scan, rows: dict, listed: dict) -> dict:
    """``route -> why`` for each entry in ``listed`` that must go or change:
    the route is not a listed route in ``rows`` (``(method, path) ->
    (handler, guard)``), the entry names no writer pairs, or the route no
    longer reaches a pair it names (those pairs)."""
    stale = {}
    for route, entry in listed.items():
        if route not in rows or rows[route][1] != "listed":
            stale[route] = "not a listed route in the inventory"
        elif not entry.pairs:
            stale[route] = "names no writer pairs"
        elif gone := stale_listed_pairs(scan, rows[route][0], entry):
            stale[route] = gone
    return stale


def test_every_listed_in_body_route_still_needs_its_entry():
    """An IN_BODY_GUARDED route exists, carries no dependency guard, and still
    reaches every writer pair its entry names outside a recognized guard (a
    pair it no longer reaches, or an entry that excuses nothing, must go)."""
    rows = {(m, p): (handler, guard) for m, p, handler, guard, _w, _o, _x in route_inventory()}
    assert stale_listed_entries(_scan(), rows, IN_BODY_GUARDED) == {}


def test_every_listed_in_body_route_names_behavioral_tests_that_exist():
    missing = []
    for route, entry in IN_BODY_GUARDED.items():
        assert entry.tests, route
        for node_id in entry.tests:
            path, name = node_id.split("::")
            tree = ast.parse((_APP / path).read_text(encoding="utf-8"))
            if name not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}:
                missing.append(node_id)
    assert missing == []


# ── Planted routes: what an in-body guard must look like ─────────────────────

_PLANTED = '''
from .catalog import catalog
from . import startup_restore
from .startup_restore import refuse_while_restoring


def _write():
    catalog.add({"id": "planted"})


def _writes_before_the_call(fn):
    def _wrapped(*args, **kwargs):
        catalog.add({"id": "planted"})
        return fn(*args, **kwargs)
    return _wrapped


def write_then_guard(request):
    catalog.add({"id": "planted"})
    refuse_while_restoring()


def helper_write_then_guard(request):
    _write()
    refuse_while_restoring()


@_writes_before_the_call
def decorator_writes_before_the_guard(request):
    refuse_while_restoring()


def ignored_refusal(system_id):
    startup_restore.refusal(system_id)
    catalog.add({"id": system_id})


def refusal_not_acted_on(system_id):
    restoring = startup_restore.refusal(system_id)
    catalog.add({"id": system_id})


def refusal_returned_after_the_write(system_id):
    restoring = startup_restore.refusal(system_id)
    catalog.add({"id": system_id})
    if restoring:
        return restoring


def write_or_refusal(system_id):
    return _write() or startup_restore.refusal(system_id)


def unreachable_guard(request):
    if False:
        refuse_while_restoring()
    catalog.add({"id": "planted"})


def guard_in_a_try(request):
    try:
        refuse_while_restoring()
    except Exception:
        pass
    catalog.add({"id": "planted"})


def guard_then_write(request):
    refuse_while_restoring()
    catalog.add({"id": "planted"})


def refusal_then_write(system_id):
    restoring = startup_restore.refusal(system_id)
    if restoring:
        return restoring
    catalog.add({"id": system_id})


def refusal_or_write(system_id):
    return startup_restore.refusal(system_id) or _write()


def calls_a_guarded_helper(request):
    return guard_then_write(request)


def listed_conditional_refusal(body):
    if not body.get("dry_run"):
        refuse_while_restoring()
    _write()


def listed_with_an_extra_writer(body):
    catalog.add({"id": "extra"})
    if not body.get("dry_run"):
        refuse_while_restoring()
    _write()


def listed_moves_an_approved_writer_first(body):
    _write()
    if not body.get("dry_run"):
        refuse_while_restoring()
    _write()


def listed_writes_in_its_condition(body):
    if not body.get("dry_run") and _write() is None:
        refuse_while_restoring()
    _write()


def listed_without_a_conditional_refusal(body):
    _write()


def listed_writes_in_its_refusing_branch(body):
    if not body.get("dry_run"):
        _write()
        refuse_while_restoring()
    _write()


def _refuses_when_writing(body):
    if not body.get("dry_run"):
        refuse_while_restoring()
    _write()


def listed_calls_its_refusing_helper(body):
    return _refuses_when_writing(body)


def listed_calls_a_default_writer_first(body, run=_write):
    run()
    if not body.get("dry_run"):
        refuse_while_restoring()
    _write()


def default_writer_then_guard(request, *, run=_write):
    run()
    refuse_while_restoring()


def listed_writes_before_its_refusing_helper(body):
    _write()
    return _refuses_when_writing(body)
'''
_PLANTED_MODULE = "agents.planted_routes"
#: The planted handlers the scan must REJECT: each writes, or can write,
#: before its refusal answers.
_PLANTED_REJECTED = (
    "write_then_guard", "helper_write_then_guard", "decorator_writes_before_the_guard",
    "ignored_refusal", "refusal_not_acted_on", "refusal_returned_after_the_write",
    "write_or_refusal", "unreachable_guard", "guard_in_a_try", "default_writer_then_guard",
)
#: The planted handlers the scan must ACCEPT: each refuses first.
_PLANTED_ACCEPTED = ("guard_then_write", "refusal_then_write", "refusal_or_write",
                     "calls_a_guarded_helper")


@pytest.fixture(scope="module")
def planted_scan() -> _Scan:
    return _Scan({**_index(), _PLANTED_MODULE: _Module(_PLANTED_MODULE, _PLANTED)})


def _planted_problems(scan, name, deps=()):
    return route_problems(scan, f"{_PLANTED_MODULE}:{name}", deps, ("POST", f"/planted/{name}"))


@pytest.mark.parametrize("name", _PLANTED_REJECTED)
def test_the_scan_rejects_a_guard_that_does_not_come_first(planted_scan, name):
    assert ("catalog.add" in {sink for sink, _site in _planted_problems(planted_scan, name)}), (
        f"{name}: the scan accepted a route that can write before it refuses")


@pytest.mark.parametrize("name", _PLANTED_ACCEPTED)
def test_the_scan_accepts_a_guard_that_comes_first(planted_scan, name):
    assert _planted_problems(planted_scan, name) == []


def test_a_dependency_guard_covers_whatever_the_handler_does(planted_scan):
    from types import SimpleNamespace
    from agents.startup_restore import refuse_while_restoring
    deps = (SimpleNamespace(dependency=refuse_while_restoring),)
    assert _planted_problems(planted_scan, "write_then_guard", deps) == []


#: The writer the two planted conditional-refusal routes reach after their
#: refusal, which a planted listing names.
_PLANTED_APPROVED = frozenset({("catalog.add", f"{_PLANTED_MODULE}:_write")})


def _planted_listing(name, pairs=_PLANTED_APPROVED, refuses_in="") -> dict:
    return {("POST", f"/planted/{name}"): Listed(tests=(), pairs=frozenset(pairs),
                                                 refuses_in=refuses_in)}


def _planted_listed_problems(scan, name, listing):
    return route_problems(scan, f"{_PLANTED_MODULE}:{name}", (), ("POST", f"/planted/{name}"),
                          listing)


def test_a_conditional_refusal_needs_a_listing(planted_scan):
    """Unlisted, a refusal under a condition does not count as a guard."""
    assert _planted_problems(planted_scan, "listed_conditional_refusal") == sorted(
        _PLANTED_APPROVED)


def test_a_listing_excuses_the_writer_pairs_it_names(planted_scan):
    name = "listed_conditional_refusal"
    assert _planted_listed_problems(planted_scan, name, _planted_listing(name)) == []


def test_a_listed_route_fails_on_a_writer_its_entry_does_not_name(planted_scan):
    """A writer added before the conditional refusal of a listed route fails
    the route, though the entry and its tests are unchanged."""
    name = "listed_with_an_extra_writer"
    assert _planted_listed_problems(planted_scan, name, _planted_listing(name)) == [
        ("catalog.add", f"{_PLANTED_MODULE}:{name}")]


_PLANTED_WRITE = ("catalog.add", f"{_PLANTED_MODULE}:_write")


@pytest.mark.parametrize("name", ["listed_moves_an_approved_writer_first",
                                  "listed_writes_in_its_condition",
                                  "listed_calls_a_default_writer_first"])
def test_a_listed_route_fails_when_an_approved_writer_runs_before_its_refusal(planted_scan,
                                                                             name):
    """The listing names the writer, but the route can now reach it before
    its conditional refusal (a statement before the ``if``, or its test), so
    the route fails though the entry and its pairs are unchanged."""
    assert _planted_listed_problems(planted_scan, name, _planted_listing(name)) == [
        _PLANTED_WRITE]


@pytest.mark.parametrize("name", ["listed_without_a_conditional_refusal",
                                  "listed_writes_in_its_refusing_branch"])
def test_a_listed_route_without_a_conditional_refusal_fails(planted_scan, name):
    """No top-level ``if`` that opens with the refusal: none at all, or a
    branch that writes before it refuses."""
    assert _planted_listed_problems(planted_scan, name, _planted_listing(name)) == [
        ("conditional-refusal-missing", f"{_PLANTED_MODULE}:{name}")]


def test_a_listing_can_name_the_helper_that_refuses(planted_scan):
    """A handler that hands the request to a helper holding the conditional
    refusal is judged by that helper, and by what the handler reaches other
    than through it."""
    helper = f"{_PLANTED_MODULE}:_refuses_when_writing"
    good, bad = "listed_calls_its_refusing_helper", "listed_writes_before_its_refusing_helper"
    assert _planted_listed_problems(
        planted_scan, good, _planted_listing(good, refuses_in=helper)) == []
    assert _planted_listed_problems(
        planted_scan, bad, _planted_listing(bad, refuses_in=helper)) == [_PLANTED_WRITE]
    # Without the helper named, the handler itself has no conditional refusal.
    assert _planted_listed_problems(planted_scan, good, _planted_listing(good)) == [
        ("conditional-refusal-missing", f"{_PLANTED_MODULE}:{good}")]


def test_a_listed_pair_the_route_no_longer_reaches_is_stale(planted_scan):
    name = "listed_conditional_refusal"
    gone = ("catalog.add", f"{_PLANTED_MODULE}:a_writer_that_was_removed")
    entry = _planted_listing(name, _PLANTED_APPROVED | {gone})[("POST", f"/planted/{name}")]
    assert stale_listed_pairs(planted_scan, f"{_PLANTED_MODULE}:{name}", entry) == [gone]
    assert stale_listed_pairs(planted_scan, f"{_PLANTED_MODULE}:{name}",
                              _planted_listing(name)[("POST", f"/planted/{name}")]) == []


def test_a_stale_listing_is_reported_for_each_reason(planted_scan):
    """An entry for a route no longer listed, one naming no pairs, and one
    naming a pair the route no longer reaches are each reported; a current
    one is not."""
    key = f"{_PLANTED_MODULE}:listed_conditional_refusal"
    gone = ("catalog.add", f"{_PLANTED_MODULE}:a_writer_that_was_removed")
    listing = {
        ("POST", "/planted/current"): Listed((), _PLANTED_APPROVED),
        ("POST", "/planted/no_pairs"): Listed((), frozenset()),
        ("POST", "/planted/pair_gone"): Listed((), _PLANTED_APPROVED | {gone}),
        ("POST", "/planted/not_listed"): Listed((), _PLANTED_APPROVED),
        ("POST", "/planted/missing"): Listed((), _PLANTED_APPROVED),
    }
    rows = {route: (key, "listed") for route in listing if route[1] != "/planted/missing"}
    rows[("POST", "/planted/not_listed")] = (key, "dependency")
    assert stale_listed_entries(planted_scan, rows, listing) == {
        ("POST", "/planted/no_pairs"): "names no writer pairs",
        ("POST", "/planted/pair_gone"): [gone],
        ("POST", "/planted/not_listed"): "not a listed route in the inventory",
        ("POST", "/planted/missing"): "not a listed route in the inventory",
    }


#: Role checks that answer 403 and must run before the restore refusal.
_ROLE_CHECKS = frozenset({"require_onboard_access", "_check_steward", "check_steward",
                          "_check_admin"})


def test_a_role_check_runs_before_the_restore_refusal():
    """A caller without the role gets its 403 during a restore too: on each
    route carrying the refusal dependency, every role-check dependency comes
    first (admin routes use the admin-first ``admin_refuse_while_restoring``)."""
    from agents import server
    late = []
    for route, path, deps in _api_routes(server.app):
        names = [getattr(dep.dependency, "__name__", "") for dep in deps]
        guards = [i for i, name in enumerate(names) if name in GUARD_NAMES]
        roles = [i for i, name in enumerate(names) if name in _ROLE_CHECKS]
        if guards and roles and max(roles) > min(guards):
            late.append(path)
    assert late == []
