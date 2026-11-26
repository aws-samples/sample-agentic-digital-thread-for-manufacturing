# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A small graph for the measured different-identifier records (test helper).

It holds what the record touches: :System nodes' :SchemaEntry and :Field nodes
(``HAS_SCHEMA`` / ``HAS_FIELD``) and the ``DIFFERENT_IDENTIFIER_FROM`` edges
between two Field nodes. It answers:

* provision's own SchemaEntry, Field and ``HAS_FIELD`` writes (b0 / b2), so a
  full provision builds the Field nodes the record joins;
* every statement about the edge, by RUNNING it (below);
* the Field deletes a re-provision and a removal run
  (``onboard._SCHEMA_SUBGRAPH_DELETE_CYPHER`` and the ``fields`` step of
  ``onboard._RESET_GRAPH_DELETES``).

Every other write is accepted and ignored, and every other read returns no
rows.

A statement about the edge is evaluated as openCypher, not recognised by its
text: its ``MATCH`` patterns are matched against the nodes and relationships
held here, and only the constraints the statement itself states narrow the
match. So a read that leaves out the ``(:System {id: $sid})-[:HAS_SCHEMA]->
(:SchemaEntry)-[:HAS_FIELD]->`` path returns other systems' edges, and a
delete with no ``system_id`` on its Field deletes other systems' edges, as
they would on a real engine. The grammar is the one the record's statements
use: ``MATCH`` paths of ``(var:Label {key: $param})`` nodes joined by
``-[var:TYPE]->`` / ``<-[var:TYPE]-``, then ``CREATE`` with ``SET r.key =
$param``, ``DELETE r``, or ``RETURN var.key AS alias`` with ``ORDER BY``
aliases. A statement about the edge outside that grammar (``WHERE``,
``OPTIONAL MATCH``, ``MERGE``, an expression it cannot evaluate) raises, so
a test cannot pass on a statement this graph does not understand.

Node properties are the ones provision writes: a System's ``id``; a
SchemaEntry's ``id`` (``<system>.<table>``), ``name`` and ``system_id``; a
Field's ``id`` (``<system>.<table>.<column>``), ``name`` and ``system_id``. A
Field is under its SchemaEntry (``HAS_FIELD``) only when attached.

Generic: no dataset, system, table, column or concept literals.
"""
from __future__ import annotations

import re

EDGE = "DIFFERENT_IDENTIFIER_FROM"
_SET_RE = re.compile(r"\br\.(\w+) = (\$\w+|'[^']*')")
_CLAUSE_RE = re.compile(r"\b(OPTIONAL MATCH|DETACH DELETE|ORDER BY|MATCH|CREATE|SET|RETURN|"
                        r"DELETE|WHERE|MERGE|WITH|UNWIND|REMOVE)\b")
_NODE_RE = re.compile(r"\((\w*)(?::(\w+))?(?:\s*\{([^}]*)\})?\)")
_REL_RE = re.compile(r"(<)?-\[(\w*)(?::(\w+))?\]-(>)?")
_PROP_RE = re.compile(r"(\w+):\s*(\$\w+|'[^']*')")
_RETURN_RE = re.compile(r"(\w+)\.(\w+) AS (\w+)")
_MODELLED = ("MATCH", "CREATE", "SET", "RETURN", "ORDER BY", "DELETE")


class FieldGraph:
    """``write`` / ``query`` stand in for ``onboard.neptune_write`` /
    ``onboard.neptune_query_checked``. ``auto_fields`` makes every Field a
    record write names exist under its system (``<system>.<table>.<column>``),
    as b2 writes one Field per mapped column; leave it off to build the
    Fields by hand (``add_field``) or from provision's own writes."""

    def __init__(self, auto_fields: bool = False):
        self.auto_fields = auto_fields
        self.entries: set = set()    # (system_id, table)
        self.fields: dict = {}       # field id -> {system_id, table, name, attached}
        self.edges: list = []        # {from, to, props}
        self.statements: list = []   # every statement about the edge, in order

    # ------------------------------------------------------------------ setup
    def add_field(self, system_id: str, table: str, column: str, attached: bool = True) -> str:
        fid = f"{system_id}.{table}.{column}"
        self.entries.add((system_id, table))
        self.fields[fid] = {"system_id": system_id, "table": table, "name": column,
                            "attached": attached}
        return fid

    def add_edge(self, field_a: str, field_b: str, **props) -> None:
        self.edges.append({"from": field_a, "to": field_b, "props": dict(props)})

    # ------------------------------------------------------------- statements
    def write(self, cypher: str, params=None) -> bool:
        import agents.onboard as onboard
        params = dict(params or {})
        if cypher in (onboard._SCHEMA_SUBGRAPH_DELETE_CYPHER,
                      dict(onboard._RESET_GRAPH_DELETES).get("fields")):
            self._delete_fields(params["sid"])
            return True
        if cypher.startswith("MERGE (se:SchemaEntry {id: $seid})") and "HAS_SCHEMA" in cypher:
            self.entries.add((params["sid"], params["name"]))
            return True
        if cypher.startswith("MERGE (f:Field {id: $fid})"):
            table = str(params["schema_id"])[len(params["sid"]) + 1:]
            self.fields.setdefault(params["fid"], {
                "system_id": params["sid"], "table": table, "name": params["name"],
                "attached": False})
            return True
        if "MERGE (se)-[:HAS_FIELD]->(f)" in cypher:
            field = self.fields.get(params["fid"])
            if field and (field["system_id"], field["table"]) in self.entries \
                    and params["seid"] == f"{field['system_id']}.{field['table']}":
                field["attached"] = True
            return True
        if EDGE not in cypher:
            return True                            # a statement this graph does not hold
        self.statements.append(("write", cypher, params))
        clauses = _clauses(cypher)
        kinds = [kind for kind, _body in clauses]
        if "RETURN" in kinds:
            raise AssertionError(f"a write that returns rows is not modelled: {cypher}")
        if self.auto_fields:
            self._auto_fields(params)
        bindings = self._bindings(clauses, params, cypher)
        if "CREATE" in kinds:
            self._create(clauses, bindings, params, cypher)
        elif "DELETE" in kinds:
            self._delete(clauses, bindings, cypher)
        else:
            raise AssertionError(f"unmodelled statement about the edge: {cypher}")
        return True

    def query(self, cypher: str, params=None) -> list:
        params = dict(params or {})
        if EDGE not in cypher:
            return []
        self.statements.append(("read", cypher, params))
        return self._run_read(cypher, params)

    # ------------------------------------------------------------ evaluation
    def _run_read(self, cypher: str, params: dict) -> list:
        clauses = _clauses(cypher)
        kinds = [kind for kind, _body in clauses]
        if "RETURN" not in kinds or {"CREATE", "DELETE", "SET"} & set(kinds):
            raise AssertionError(f"unmodelled read of the edge: {cypher}")
        bindings = self._bindings(clauses, params, cypher)
        (items,) = [body for kind, body in clauses if kind == "RETURN"]
        columns = []
        for item in _split_top(items):
            found = _RETURN_RE.fullmatch(item.strip())
            if not found:
                raise AssertionError(f"unmodelled RETURN item {item!r}: {cypher}")
            columns.append(found.groups())
        nodes, _rels = self._model()
        rows = [{alias: self._value(nodes, binding, var, prop, cypher)
                 for var, prop, alias in columns} for binding in bindings]
        for kind, body in clauses:
            if kind == "ORDER BY":
                keys = [key.strip() for key in body.split(",")]
                rows.sort(key=lambda row: tuple(str(row[k]) for k in keys))
        return rows

    def _bindings(self, clauses, params, cypher) -> list:
        for kind, _body in clauses:
            if kind not in _MODELLED:
                raise AssertionError(f"unmodelled clause {kind} in: {cypher}")
        nodes, rels = self._model()
        bindings: list = [{}]
        for kind, body in clauses:
            if kind != "MATCH":
                continue
            for path in _split_top(body):
                steps = _parse_path(path.strip(), cypher)
                bindings = [b for binding in bindings
                            for b in _match(steps, binding, nodes, rels, params)]
        return bindings

    def _model(self):
        """The graph as node and relationship tables."""
        nodes: dict = {}
        for sid, table in self.entries:
            nodes[("System", sid)] = {"label": "System", "props": {"id": sid}}
            nodes[("SchemaEntry", sid, table)] = {
                "label": "SchemaEntry",
                "props": {"id": f"{sid}.{table}", "name": table, "system_id": sid}}
        for fid, field in self.fields.items():
            nodes.setdefault(("System", field["system_id"]),
                             {"label": "System", "props": {"id": field["system_id"]}})
            nodes[("Field", fid)] = {
                "label": "Field",
                "props": {"id": fid, "name": field["name"], "system_id": field["system_id"]}}
        rels: list = []
        for sid, table in self.entries:
            rels.append(("HAS_SCHEMA", ("System", sid), ("SchemaEntry", sid, table), None))
        for fid, field in self.fields.items():
            entry = ("SchemaEntry", field["system_id"], field["table"])
            if field["attached"] and entry in nodes:
                rels.append(("HAS_FIELD", entry, ("Field", fid), None))
        for index, edge in enumerate(self.edges):
            ends = (("Field", edge["from"]), ("Field", edge["to"]))
            if all(end in nodes for end in ends):
                rels.append((EDGE, ends[0], ends[1], index))
        return nodes, rels

    def _value(self, nodes, binding, var, prop, cypher):
        bound = binding.get(var)
        if bound is None or bound[0] == "structural":
            raise AssertionError(f"RETURN names an unbound variable {var}: {cypher}")
        if bound[0] == "rel":
            return self.edges[bound[1]]["props"].get(prop)
        return nodes[bound[1]]["props"].get(prop)

    def _create(self, clauses, bindings, params, cypher) -> None:
        (body,) = [b for kind, b in clauses if kind == "CREATE"]
        found = re.fullmatch(rf"\((\w+)\)-\[r:{EDGE}\]->\((\w+)\)", body.strip())
        if not found:
            raise AssertionError(f"unmodelled CREATE: {cypher}")
        props = {}
        for prop, token in _SET_RE.findall(" ".join(b for k, b in clauses if k == "SET")):
            props[prop] = params[token[1:]] if token.startswith("$") else token[1:-1]
        var_a, var_b = found.groups()
        for binding in bindings:
            end_a, end_b = binding.get(var_a), binding.get(var_b)
            if not (end_a and end_b and end_a[1][0] == "Field" and end_b[1][0] == "Field"):
                raise AssertionError(f"CREATE between unbound or non-Field ends: {cypher}")
            self.add_edge(end_a[1][1], end_b[1][1], **props)

    def _delete(self, clauses, bindings, cypher) -> None:
        (body,) = [b for kind, b in clauses if kind == "DELETE"]
        var = body.strip()
        gone = set()
        for binding in bindings:
            bound = binding.get(var)
            if bound is None or bound[0] != "rel":
                raise AssertionError(f"DELETE of a node without DETACH: {cypher}")
            gone.add(bound[1])
        self.edges = [e for i, e in enumerate(self.edges) if i not in gone]

    def _auto_fields(self, params: dict) -> None:
        sid = params.get("sid")
        for value in params.values():
            if isinstance(value, str) and sid and value.startswith(f"{sid}.") \
                    and value not in self.fields:
                table, _dot, column = value[len(sid) + 1:].rpartition(".")
                if table and column:
                    self.add_field(sid, table, column)

    def _delete_fields(self, sid: str) -> None:
        gone = {fid for fid, f in self.fields.items()
                if f["system_id"] == sid and f["attached"]}
        self.fields = {fid: f for fid, f in self.fields.items() if fid not in gone}
        self.edges = [e for e in self.edges if e["from"] not in gone and e["to"] not in gone]

    # --------------------------------------------------------------- helpers
    def records(self, sid: str) -> list:
        """The rows of the production read (``onboard.DISTINCT_IDENTIFIER_RECORDS_READ``)
        for ``sid``, evaluated like any other read but not recorded."""
        import agents.onboard as onboard
        return self._run_read(onboard.DISTINCT_IDENTIFIER_RECORDS_READ, {"sid": sid})

    def edges_of(self, sid: str) -> list:
        """Every edge whose FROM Field carries ``sid``, attached or not."""
        return [e for e in self.edges
                if self.fields.get(e["from"], {}).get("system_id") == sid]

    def install(self, monkeypatch) -> "FieldGraph":
        """Route onboarding's graph writes and checked reads to this graph."""
        import agents.onboard as onboard
        monkeypatch.setattr(onboard, "neptune_write", self.write)
        monkeypatch.setattr(onboard, "neptune_query_checked", self.query)
        return self


# --------------------------------------------------------------------------- #
# Parsing and matching
# --------------------------------------------------------------------------- #

def _clauses(cypher: str) -> list:
    """``[(keyword, body), ...]`` of a statement, in order."""
    parts = _CLAUSE_RE.split(cypher)
    if parts[0].strip():
        raise AssertionError(f"a statement must start with a clause: {cypher}")
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def _split_top(text: str) -> list:
    """``text`` split at commas outside parentheses, brackets and braces."""
    out, depth, current = [], 0, ""
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += char
    out.append(current)
    return [part for part in out if part.strip()]


def _props(text) -> dict:
    return dict(_PROP_RE.findall(text or ""))


def _parse_path(path: str, cypher: str) -> list:
    """``[node, rel, node, ...]``: a node is ``("node", var, label, props)``,
    a relationship ``("rel", var, type, direction)`` with direction ``out``,
    ``in`` or ``any``."""
    steps, pos = [], 0
    while True:
        node = _NODE_RE.match(path, pos)
        if not node:
            raise AssertionError(f"unmodelled pattern {path!r} in: {cypher}")
        steps.append(("node", node.group(1), node.group(2), _props(node.group(3))))
        pos = node.end()
        if pos == len(path):
            return steps
        rel = _REL_RE.match(path, pos)
        if not rel or (rel.group(1) and rel.group(4)):
            raise AssertionError(f"unmodelled pattern {path!r} in: {cypher}")
        direction = "in" if rel.group(1) else ("out" if rel.group(4) else "any")
        steps.append(("rel", rel.group(2), rel.group(3), direction))
        pos = rel.end()


def _node_ok(step, key, nodes, params, binding) -> bool:
    _kind, var, label, props = step
    if var and var in binding and binding[var] != ("node", key):
        return False
    node = nodes[key]
    if label and node["label"] != label:
        return False
    for prop, token in props.items():
        want = params.get(token[1:]) if token.startswith("$") else token[1:-1]
        if node["props"].get(prop) != want:
            return False
    return True


def _bind(binding, var, value) -> dict:
    return {**binding, var: value} if var else binding


def _match(steps, binding, nodes, rels, params):
    """Every extension of ``binding`` that matches the path ``steps``."""
    first = steps[0]
    for key in nodes:
        if _node_ok(first, key, nodes, params, binding):
            yield from _walk(steps, 1, key, _bind(binding, first[1], ("node", key)),
                             nodes, rels, params)


def _walk(steps, i, at, binding, nodes, rels, params):
    if i >= len(steps):
        yield binding
        return
    (_r, rvar, rtype, direction), nxt = steps[i], steps[i + 1]
    for index, (kind, src, dst, ref) in enumerate(rels):
        if rtype and kind != rtype:
            continue
        ends = []
        if direction in ("out", "any") and src == at:
            ends.append(dst)
        if direction in ("in", "any") and dst == at:
            ends.append(src)
        rel_value = ("rel", ref) if ref is not None else ("structural", index)
        if rvar and rvar in binding and binding[rvar] != rel_value:
            continue
        for end in ends:
            if _node_ok(nxt, end, nodes, params, binding):
                bound = _bind(_bind(binding, rvar, rel_value), nxt[1], ("node", end))
                yield from _walk(steps, i + 2, end, bound, nodes, rels, params)
