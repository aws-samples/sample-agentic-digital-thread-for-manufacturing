# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Removing a system deletes the ValueMappings and Correlations that name it.

The single-system removal (``_reset_delete_graph_artifacts``) deleted
ValueMappings with ``MATCH (vm:ValueMapping {system_id: $sid})``, but no
ValueMapping writer sets ``system_id``: every one of them sets
``from_system`` / ``to_system`` (plus FROM_SYSTEM / TO_SYSTEM edges). The
delete matched nothing, so a removed system's mappings outlived it; on a
rehearsal account the corporate CMMS's equipment mappings to SiteWise were
still in the graph hours after the CMMS was removed. Learned Correlations
were never deleted at all.

These tests write the rows through the REAL writers (equipment, the resolve
step's cross-system rows, the provision replay of accepted items, the steward
accept, and both Correlation writers) and remove the system through the REAL
removal, against a small in-memory graph that evaluates the statements both
sides send. The graph understands only the statement shapes listed in
``_MiniGraph`` and refuses any other ValueMapping or Correlation statement, so
a delete rewritten into a shape it cannot judge fails here instead of passing
by matching nothing.

The same defect class is closed at the three places a mapping is (re)made:

* resolve c3 (``_dp_write_cross_vm``) copied the model's ``from_system`` /
  ``to_system`` unchecked, so a short name (``mes``) or an unknown system
  reached the graph. ``from_system`` is now the onboarding system and
  ``to_system`` must name exactly one existing System; the reset also deletes
  a legacy short-name row under the sibling-safe ``_short_name_for_delete``
  rule the IdentityRule/JoinPath deletes use.
* the provision replay of accepted items (``_dp_replay_learned_vm``)
  recreated a mapping when only its surviving system was re-onboarded. It now
  replays only after a checked read shows BOTH System nodes.
* the steward graph repair (``_repair_value_mapping_edges``) kept a mapping
  whose other system is gone and re-linked its live side. After a
  completeness-controlled System inventory it now retires such a mapping, and
  a delete the graph rejects is not reported.

Offline: the graph is the fake below; no AWS, no network.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import agents.onboard as onboard
import agents.resolve as resolve
import agents.self_learning as self_learning
from agents.db import GraphQueryError
from agents.models import LearnRequest

#: Labels whose statements the fake must understand (anything else naming
#: one of them raises).
_JUDGED = ("ValueMapping", "Correlation")
_MERGE_NODE = re.compile(r"^MERGE \((\w+):(\w+) \{id: \$(\w+)\}\) SET (.+)$", re.S)
_CREATE_NODE = re.compile(r"^CREATE \((\w+):(\w+) \{(.+)\}\)$", re.S)
_DELETE = re.compile(
    r"^MATCH \((\w+):(\w+)(?: \{([^}]*)\})?\)(?: WHERE (.+?))? DETACH DELETE (\w+)$", re.S)
_EDGE_DELETE = re.compile(
    r"^MATCH \((\w+):(\w+)\) WHERE \((\w+)\)-\[:([\w|]+)\]->\(:System \{id: \$(\w+)\}\) "
    r"DETACH DELETE (\w+)$")
_EDGE = re.compile(
    r"^MATCH \((\w+):(\w+) \{id: \$(\w+)\}\), \((\w+):System \{id: \$(\w+)\}\) "
    r"(?:MERGE|CREATE) \((\w+)\)-\[:(\w+)\]->\((\w+)\)$")
# A mapping's edges to its Concept / Asset: not what a removal keys on.
_OTHER_EDGE = re.compile(r"MERGE \(\w+\)-\[:(LINKS_CONCEPT|MAPS_TO_ASSET)\]->\(\w+\)$")
_PROJECTION = re.compile(r"^MATCH \((\w+):(\w+)\) RETURN (.+)$")
# The one-statement mapping write (onboard._upsert_value_mapping): both System
# nodes (and, for an anchored equipment mapping, the one Asset), then the
# node, both edges and either the steward accept's Concept link or the
# equipment anchor reconcile, returning the node id.
_ASSET_GUARD = re.compile(
    r"^MATCH \((\w+):Asset \{source_id: \$(\w+)\}\) WITH count\(\1\) AS (\w+) WHERE \3 = 1 "
    r"(MATCH .+)$", re.S)
_UPSERT = re.compile(
    r"^MATCH \((\w+):System \{id: \$(\w+)\}\), \((\w+):System \{id: \$(\w+)\}\)"
    r"(?:, \((\w+):Asset \{source_id: \$(\w+)\}\))? "
    r"(?:WHERE (.+?) )?"
    r"MERGE \((\w+):(\w+) \{id: \$(\w+)\}\) SET (.+?) "
    r"MERGE \((\w+)\)-\[:(\w+)\]->\((\w+)\) MERGE \((\w+)\)-\[:(\w+)\]->\((\w+)\) "
    r"(.*)RETURN (\w+)\.id AS (\w+)$", re.S)
# The endpoint guard of the mapping write: both System nodes must be ones
# whose removal has not started (no ``removal_incomplete = true``).
_SERVED_GUARD = re.compile(
    r"^coalesce\((\w+)\.removal_incomplete, false\) = false "
    r"AND coalesce\((\w+)\.removal_incomplete, false\) = false$")
_CONCEPT_TAIL = re.compile(
    r"^MERGE \((\w+):Concept \{id: \$(\w+)\}\) ON CREATE SET \1\.name = \$\2 "
    r"MERGE \((\w+)\)-\[:LINKS_CONCEPT\]->\(\1\) $")
_ANCHOR_TAIL = re.compile(
    r"^WITH (\w+), (\w+) OPTIONAL MATCH \(\1\)-\[(\w+):MAPS_TO_ASSET\]->\(\) DELETE \3 "
    r"WITH DISTINCT \1, \2 MERGE \(\1\)-\[:MAPS_TO_ASSET\]->\(\2\) $")
# The equipment anchor as an earlier version wrote it (a follow-up MERGE to EVERY Asset
# with the source id). Kept so a test run against that code fails on what the
# graph ends up holding, not on an unknown statement.
_ANCHOR = re.compile(
    r"^MATCH \((\w+):ValueMapping \{id: \$(\w+)\}\), \((\w+):Asset \{source_id: \$(\w+)\}\) "
    r"MERGE \((\w+)\)-\[:MAPS_TO_ASSET\]->\((\w+)\) RETURN count\((\w+)\) AS (\w+)$")
_COUNT = re.compile(r"^MATCH \((\w+):(\w+)\) RETURN count\((\w+)\) AS (\w+)$")
_COUNT_BY_PROP = re.compile(
    r"^MATCH \((\w+):(\w+) \{(\w+): \$(\w+)\}\) RETURN count\(\1\) AS (\w+)$")
_COUNT_WHERE = re.compile(r"^MATCH \((\w+):(\w+)\) WHERE (.+) RETURN count\(\1\) AS (\w+)$", re.S)
_ANCHOR_READ = re.compile(
    r"^MATCH \((\w+):ValueMapping \{id: \$(\w+)\}\)-\[:MAPS_TO_ASSET\]->\((\w+):Asset\) "
    r"RETURN count\(\3\) AS (\w+), "
    r"count\(CASE WHEN \3\.source_id = \$(\w+) THEN 1 END\) AS (\w+)$")


class _MiniGraph:
    """Nodes keyed by (label, id); evaluates exactly these shapes:

    * ``MERGE (x:L {id: $p}) SET x.a = $q, x.b = '', ...`` — upsert a node.
    * ``CREATE (x:L {id: $p, a: $q, n: 1, ...})`` — create a node.
    * ``MATCH (x:L[ {a: $p, ...}])[ WHERE x.a = $p OR x.b = $q ...] DETACH DELETE x``
      — inline map is AND, the WHERE a disjunction of equalities; a missing
      property never equals (openCypher null semantics).
    * ``MATCH (x:L) WHERE (x)-[:E1|E2]->(:System {id: $p}) DETACH DELETE x``
      — delete the L nodes with one of those edges to that System.
    * ``MATCH (x:L {id: $p}), (s:System {id: $q}) MERGE|CREATE (x)-[:E]->(s)``
      — an edge, kept only when both ends exist (a MATCH that finds nothing
      writes nothing). A mapping's LINKS_CONCEPT / MAPS_TO_ASSET edges are
      accepted and not tracked.
    * ``MATCH (x:L) RETURN x.a AS b, ...`` — a read projection, and
      ``MATCH (x:L) RETURN count(x) AS n``.
    * through ``mutate`` (the write seam that returns rows; the ValueMapping
      upsert and the JoinPath / Correlation accepts, each SET value a
      parameter or ``''``):
      ``MATCH (a:System {id: $p}), (b:System {id: $q}) MERGE (x:L {id: $r})
      SET ... MERGE (x)-[:E1]->(a) MERGE (x)-[:E2]->(b)
      [MERGE (c:Concept {id: $s}) ON CREATE SET c.name = $s MERGE (x)-[:LINKS_CONCEPT]->(c)]
      RETURN x.id AS id`` — ONE statement: when either System is missing it
      writes nothing and returns no row, else it writes the node and both
      edges (the Concept link is accepted and not tracked) and returns the id.
      With the endpoint guard ``WHERE coalesce(a.removal_incomplete, false) =
      false AND coalesce(b.removal_incomplete, false) = false`` after the
      MATCH, a System node carrying ``removal_incomplete = True`` also writes
      nothing and returns no row.
      With the equipment guard ``MATCH (n:Asset {source_id: $p}) WITH
      count(n) AS k WHERE k = 1`` in front, the Asset in the MATCH and the
      reconcile tail ``WITH x, a OPTIONAL MATCH (x)-[o:MAPS_TO_ASSET]->()
      DELETE o WITH DISTINCT x, a MERGE (x)-[:MAPS_TO_ASSET]->(a)``, it writes
      nothing unless exactly one Asset carries that source id, and then leaves
      the mapping anchored to that Asset only. The earlier version's follow-up anchor
      ``MATCH (x:ValueMapping {id: $p}), (a:Asset {source_id: $q}) MERGE
      (x)-[:MAPS_TO_ASSET]->(a) RETURN count(a) AS n`` is evaluated as the
      engine would: an edge to EVERY matching Asset.
    * Asset nodes are real (``add_asset``): ``MATCH (a:Asset {source_id: $p})
      RETURN count(a) AS n`` counts them, and ``MATCH (x:ValueMapping {id:
      $p})-[:MAPS_TO_ASSET]->(a:Asset) RETURN count(a) AS k, count(CASE WHEN
      a.source_id = $q THEN 1 END) AS c`` reads a mapping's anchors back.
    * ``MATCH (x:L) WHERE <term> AND <term> ... [OR ...] DETACH DELETE x`` with
      terms ``x.a = $p``, ``x.a <> $p`` and ``x.a STARTS WITH $p`` (through
      either seam), and the same WHERE with ``RETURN count(x) AS n``.

    A statement containing any token in ``reject`` is refused as the engine
    would refuse it: nothing it would write is written, ``write`` answers
    False and ``mutate`` None. Any other statement naming a ValueMapping or
    Correlation raises; other statements (the removal's schema, asset and rule
    deletes) are recorded and succeed."""

    def __init__(self, systems=()):
        self.nodes: dict = {}
        self.edges: set = set()
        self.writes: list = []
        self.reject: set = set()
        self.anchors: set = set()       # (ValueMapping id, Asset node key)
        self.ignore_deletes = False     # a DETACH DELETE that runs but removes nothing
        for sid in systems:
            self.nodes[("System", sid)] = {"id": sid}

    def add_asset(self, source_id, key=None):
        key = key or source_id
        self.nodes[("Asset", key)] = {"id": key, "source_id": source_id}
        return key

    def assets_with(self, source_id) -> list:
        return [k for (lbl, k), p in self.nodes.items()
                if lbl == "Asset" and p.get("source_id") == source_id]

    def anchors_of(self, vm_id) -> set:
        """The source ids a mapping's MAPS_TO_ASSET edges point at."""
        return {self.nodes[("Asset", k)]["source_id"] for v, k in self.anchors if v == vm_id}

    # -- views -----------------------------------------------------------------
    def of(self, label) -> dict:
        return {nid: props for (lbl, nid), props in self.nodes.items() if lbl == label}

    def value_mappings(self) -> dict:
        return self.of("ValueMapping")

    def correlations(self) -> dict:
        return self.of("Correlation")

    def snapshot(self):
        return {k: dict(v) for k, v in self.nodes.items()}, set(self.edges)

    # -- seams -----------------------------------------------------------------
    def _refused(self, cypher) -> bool:
        return any(token in cypher for token in self.reject)

    def mutate(self, cypher, params=None):
        params = dict(params or {})
        self.writes.append((cypher, params))
        if self._refused(cypher):
            return None
        m = _ANCHOR.match(cypher)
        if m:
            alias, id_param, _, aid_param, src, _, counted, name = m.groups()
            assert src == alias, cypher
            if ("ValueMapping", params[id_param]) not in self.nodes:
                return [{name: 0}]
            targets = self.assets_with(params[aid_param])
            self.anchors |= {(params[id_param], k) for k in targets}
            return [{name: len(targets)}]
        m = _DELETE.match(cypher)
        if m and m.group(2) in _JUDGED:
            if not self.ignore_deletes:
                self._delete(m, params)
            return []
        guard_param = None
        g = _ASSET_GUARD.match(cypher)
        if g:
            guard_param, cypher = g.group(2), g.group(4)
        m = _UPSERT.match(cypher)
        if not m:
            if any(label in cypher for label in _JUDGED):
                raise AssertionError(f"the fake graph cannot judge this statement: {cypher}")
            return []
        (a, a_param, b, b_param, asset_alias, asset_param, guard, alias, label, id_param, sets,
         e1_src, e1, e1_dst, e2_src, e2, e2_dst, tail, ret_alias, ret_name) = m.groups()
        assert alias == e1_src == e2_src == ret_alias, cypher
        ends = {a: params[a_param], b: params[b_param]}
        assert {e1_dst, e2_dst} == set(ends), cypher
        served = _SERVED_GUARD.match(guard) if guard else None
        assert guard is None or (served and set(served.groups()) == set(ends)), guard
        concept = _CONCEPT_TAIL.match(tail)
        anchor = _ANCHOR_TAIL.match(tail)
        assert tail == "" or (concept and concept.group(3) == alias) or anchor, tail
        # The Asset, its guard and the reconcile tail come together or not at all.
        assert bool(asset_alias) == bool(guard_param) == bool(anchor), cypher
        if anchor:
            assert anchor.group(1) == alias and anchor.group(2) == asset_alias, cypher
            assert asset_param == guard_param, cypher
        if any(("System", sid) not in self.nodes for sid in ends.values()):
            return []
        if served and any(self.nodes[("System", sid)].get("removal_incomplete") is True
                          for sid in ends.values()):
            return []           # the guard filtered the row: an endpoint is being removed
        if guard_param and len(self.assets_with(params[guard_param])) != 1:
            return []           # the WITH count(...) WHERE = 1 guard filtered the row
        node = self.nodes.setdefault((label, params[id_param]), {"id": params[id_param]})
        for assignment in sets.split(", "):
            prop, value = re.fullmatch(rf"{alias}\.(\w+) = (\$\w+|'')", assignment.strip()).groups()
            node[prop] = params[value[1:]] if value.startswith("$") else ""
        self.edges.add((params[id_param], e1, ends[e1_dst]))
        self.edges.add((params[id_param], e2, ends[e2_dst]))
        if anchor:
            (target,) = self.assets_with(params[guard_param])
            self.anchors = {x for x in self.anchors if x[0] != params[id_param]}
            self.anchors.add((params[id_param], target))
        return [{ret_name: params[id_param]}]

    def write(self, cypher, params=None):
        params = dict(params or {})
        self.writes.append((cypher, params))
        if self._refused(cypher):
            return False
        m = _MERGE_NODE.match(cypher)
        if m:
            alias, label, id_param, sets = m.groups()
            node = self.nodes.setdefault((label, params[id_param]), {"id": params[id_param]})
            for assignment in sets.split(", "):
                prop, value = re.fullmatch(rf"{alias}\.(\w+) = (\$\w+|'')", assignment.strip()).groups()
                node[prop] = params[value[1:]] if value.startswith("$") else ""
            return True
        m = _CREATE_NODE.match(cypher)
        if m:
            _, label, body = m.groups()
            props = {}
            for pair in body.split(", "):
                prop, value = re.fullmatch(r"(\w+): (\S+)", pair.strip()).groups()
                props[prop] = params[value[1:]] if value.startswith("$") else value
            self.nodes[(label, props["id"])] = props
            return True
        m = _EDGE_DELETE.match(cypher)
        if m:
            alias, label, pattern_alias, types, sid_param, deleted = m.groups()
            assert deleted == alias == pattern_alias, cypher
            types = set(types.split("|"))
            doomed = {nid for (nid, edge, sid) in self.edges
                      if edge in types and sid == params[sid_param] and (label, nid) in self.nodes}
            for nid in doomed:
                del self.nodes[(label, nid)]
            self.edges = {e for e in self.edges if e[0] not in doomed}
            return True
        m = _DELETE.match(cypher)
        if m and m.group(2) in _JUDGED:
            if not self.ignore_deletes:
                self._delete(m, params)
            return True
        m = _EDGE.match(cypher)
        if m:
            _, label, id_param, _, sid_param, _, edge, _ = m.groups()
            if (label, params[id_param]) in self.nodes and ("System", params[sid_param]) in self.nodes:
                self.edges.add((params[id_param], edge, params[sid_param]))
            return True
        if _OTHER_EDGE.search(cypher):
            return True
        if any(label in cypher for label in _JUDGED):
            raise AssertionError(f"the fake graph cannot judge this statement: {cypher}")
        if "(s:System {id: $sid}) DETACH DELETE s" in cypher:
            self.nodes.pop(("System", params["sid"]), None)
            self.edges = {e for e in self.edges if e[2] != params["sid"]}
        return True

    def query(self, cypher, params=None):
        params = dict(params or {})
        m = _COUNT.match(cypher)
        if m:
            alias, label, counted, name = m.groups()
            assert counted == alias, cypher
            return [{name: len(self.of(label))}]
        m = _COUNT_BY_PROP.match(cypher)
        if m:
            _, label, prop, param, name = m.groups()
            return [{name: sum(1 for p in self.of(label).values() if p.get(prop) == params[param])}]
        m = _COUNT_WHERE.match(cypher)
        if m:
            alias, label, where, name = m.groups()
            return [{name: len(self._matching_keys(alias, label, None, where, params))}]
        m = _ANCHOR_READ.match(cypher)
        if m:
            _, id_param, _, total, aid_param, correct = m.groups()
            targets = [self.nodes[("Asset", k)]["source_id"]
                       for v, k in self.anchors if v == params[id_param]]
            return [{total: len(targets),
                     correct: sum(1 for t in targets if t == params[aid_param])}]
        m = _PROJECTION.match(cypher)
        if not m:
            return []
        alias, label, returns = m.groups()
        cols = [re.fullmatch(rf"{alias}\.(\w+) AS (\w+)", c.strip()).groups()
                for c in returns.split(",")]
        return [{name: props.get(prop) for prop, name in cols}
                for (lbl, _), props in self.nodes.items() if lbl == label]

    def _delete(self, m, params):
        alias, label, inline, where, deleted = m.groups()
        assert deleted == alias, m.group(0)
        for key in self._matching_keys(alias, label, inline, where, params):
            del self.nodes[key]
            self.edges = {e for e in self.edges if e[0] != key[1]}
            self.anchors = {a for a in self.anchors if a[0] != key[1]}

    def _matching_keys(self, alias, label, inline, where, params) -> list:
        conjuncts = []
        for pair in (inline or "").split(","):
            if pair.strip():
                prop, value = re.fullmatch(r"(\w+): \$(\w+)", pair.strip()).groups()
                conjuncts.append((prop, params[value]))
        disjuncts = []      # each a list of AND-ed (prop, op, value) terms
        for clause in (where.split(" OR ") if where else []):
            terms = []
            for term in clause.split(" AND "):
                hit = re.fullmatch(rf"{alias}\.(\w+) (=|<>|STARTS WITH) \$(\w+)", term.strip())
                if not hit:
                    raise AssertionError(f"the fake graph cannot judge this WHERE term: {term}")
                terms.append((hit.group(1), hit.group(2), params[hit.group(3)]))
            disjuncts.append(terms)

        def holds(props, prop, op, value):
            have = props.get(prop)
            if have is None:
                return False            # openCypher: a null comparison is not true
            if op == "=":
                return have == value
            if op == "<>":
                return have != value
            return str(have).startswith(value)

        def matches(props):
            if any(props.get(p) is None or props.get(p) != v for p, v in conjuncts):
                return False
            return not disjuncts or any(all(holds(props, *t) for t in terms)
                                        for terms in disjuncts)
        return [k for k, props in self.nodes.items() if k[0] == label and matches(props)]


SYSTEMS = ("cmms", "sitewise-direct", "erp", "mes-mil", "mes-det")

#: The graph of the running test, so the writer helpers below can add the
#: Asset an equipment mapping anchors to.
_CURRENT: dict = {}


@pytest.fixture
def graph(monkeypatch):
    g = _MiniGraph(systems=SYSTEMS)
    _CURRENT["graph"] = g
    monkeypatch.setattr(onboard, "neptune_write", g.write)
    # The write seam that returns rows (the one-statement mapping write).
    monkeypatch.setattr(onboard, "_db_neptune_write_returning", g.mutate, raising=False)
    monkeypatch.setattr(onboard, "neptune_query", g.query)
    # The checked read: the removal's diagnostic (counts only, never blocks
    # the delete) and the System inventory c3 and the replay check against.
    monkeypatch.setattr(onboard, "neptune_query_checked", g.query)
    monkeypatch.setattr(resolve, "neptune_write", g.write)
    monkeypatch.setattr(resolve, "neptune_query", g.query)
    # The steward accept's pre-check reads the graph through its own seam;
    # both ends exist here.
    monkeypatch.setattr(self_learning, "_require_graph_systems", lambda *ids: None)
    return g


def _drop_system(graph, sid):
    """A system removed earlier (its :System node and edges are gone)."""
    del graph.nodes[("System", sid)]
    graph.edges = {e for e in graph.edges if e[2] != sid}


def _unreadable(*_a, **_k):
    raise GraphQueryError("TimeoutError", "the graph did not answer")


# ── the four ValueMapping writers ────────────────────────────────────────────

def _equipment(src, dst, tag):
    _CURRENT["graph"].add_asset(f"uuid-{tag}")
    vm = onboard._equipment_vm(src, "assets.asset_tag", tag,
                               {"system": dst, "name": f"Asset {tag}", "source_id": f"uuid-{tag}",
                                "path": f"/Plant/Asset {tag}"}, 0.95, "value_format_match")
    vm_id = onboard._equipment_vm_id(vm)
    assert onboard._write_equipment_vm(vm_id, vm)
    return vm_id


def _cross_vm(src, dst, tag, **over):
    vm = {"from_system": src, "from_field": "orders.line", "from_value": tag,
          "to_system": dst, "to_field": "lines.code", "to_value": f"L-{tag}",
          "concept": "production-line", "confidence": 0.9, "method": "semantic_similarity"}
    vm.update(over)
    return vm


def _cross_system(src, dst, tag):
    ctx = SimpleNamespace(system_id=src, value_mappings_written=0, nodes=0)
    assert onboard._dp_write_cross_vm(ctx, _cross_vm(src, dst, tag), 0)
    return f"vm-xsys-{src}-{dst}-0"


def _learned_item(src, dst, tag, kind="value_mapping"):
    return {"neptune_node_id": f"subj-replay{tag}", "mapping_id": f"m-{tag}",
            "proposed_type": kind,
            "from_system": src, "from_field": "wo.machine", "from_value": tag,
            "to_system": dst, "to_field": "assets.asset_tag", "to_value": f"T-{tag}",
            "concept": "equipment-id", "confidence": 0.9}


def _replayed(src, dst, tag):
    item = _learned_item(src, dst, tag)
    onboard._dp_replay_learned_vm(item)
    return item["neptune_node_id"]


def _steward_accepted(src, dst, tag):
    vm_id = f"subj-accept{tag}"
    self_learning._apply_value_mapping(
        {"from_system": src, "from_field": "parts.part_no", "from_value": tag,
         "to_system": dst, "to_field": "items.item", "to_value": f"I-{tag}", "confidence": 0.9},
        vm_id, 1, "part-number", "crosswalk")
    return vm_id


WRITERS = {"equipment": _equipment, "cross_system": _cross_system,
           "replayed_learned": _replayed, "steward_accepted": _steward_accepted}


@pytest.mark.parametrize("side", ["from", "to"])
@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_removal_deletes_a_mapping_naming_the_system_on_either_side(graph, writer, side):
    write = WRITERS[writer]
    gone = write("cmms", "sitewise-direct", "A1") if side == "from" else \
        write("erp", "cmms", "A1")
    kept = write("erp", "mes-mil", "B2")
    assert {gone, kept} <= set(graph.value_mappings()), graph.value_mappings()

    onboard._reset_delete_graph_artifacts("cmms")

    left = graph.value_mappings()
    assert gone not in left, f"{writer} mapping naming the removed system on its {side} side survived"
    assert kept in left, "a mapping between two other systems was deleted"


def test_removal_keeps_every_other_systems_mappings(graph):
    """One graph, every writer, both directions: only the removed system's rows go."""
    removed = [w("cmms", "sitewise-direct", f"F{i}") for i, w in enumerate(WRITERS.values())]
    removed += [w("erp", "cmms", f"T{i}") for i, w in enumerate(WRITERS.values())]
    others = [w("erp", "sitewise-direct", f"O{i}") for i, w in enumerate(WRITERS.values())]
    others += [w("mes-det", "erp", f"P{i}") for i, w in enumerate(WRITERS.values())]

    onboard._reset_delete_graph_artifacts("cmms")

    left = set(graph.value_mappings())
    assert left.isdisjoint(removed), sorted(left & set(removed))
    assert left == set(others)
    assert all("cmms" not in edge for edge in graph.edges), graph.edges


def test_a_sibling_sites_mappings_survive_by_full_id(graph):
    """``mes-mil`` and ``mes-det`` share the short name ``mes``; removing one
    site deletes its full-id rows and leaves the sibling's untouched."""
    mine = [_equipment("mes-mil", "sitewise-direct", "M1"), _replayed("erp", "mes-mil", "M2")]
    sibling = [_equipment("mes-det", "sitewise-direct", "D1"), _replayed("erp", "mes-det", "D2")]

    onboard._reset_delete_graph_artifacts("mes-mil")

    left = set(graph.value_mappings())
    assert left.isdisjoint(mine) and left == set(sibling)


def test_steward_confirmed_mappings_go_with_the_system(graph):
    """A ValueMapping whose source or target system no longer exists cannot be
    valid; steward-confirmed rows are deleted like the JoinPath and
    IdentityRule rows the same removal deletes. The accepted item stays in the
    self-learning table and replays once both systems are onboarded."""
    accepted = _steward_accepted("cmms", "erp", "S1")
    assert graph.value_mappings()[accepted]["method"] == "crosswalk_confirmed"

    onboard._reset_delete_graph_artifacts("cmms")

    assert accepted not in graph.value_mappings()


def test_a_failed_value_mapping_delete_keeps_the_system_node(graph, monkeypatch):
    """The ValueMapping delete is checked like every other removal delete."""
    _equipment("cmms", "sitewise-direct", "X1")

    def failing(cypher, params=None):
        return False if ("ValueMapping" in cypher and "DETACH DELETE" in cypher) else graph.write(cypher, params)
    monkeypatch.setattr(onboard, "neptune_write", failing)
    with pytest.raises(onboard.ResetIncompleteError, match="ValueMapping"):
        onboard._reset_delete_graph_artifacts("cmms")
    assert ("System", "cmms") in graph.nodes


# ── resolve c3: the endpoints are exact System ids ──────────────────────────

def _c3(graph, ctx_sid, vm, idx=0):
    ctx = SimpleNamespace(system_id=ctx_sid, value_mappings_written=0, nodes=0)
    return onboard._dp_write_cross_vm(ctx, vm, idx), ctx


def test_c3_canonicalizes_a_short_target_that_names_one_system(graph):
    """``mes`` with only ``mes-mil`` onboarded IS ``mes-mil``: stored by full id,
    so the removal of ``mes-mil`` finds it."""
    _drop_system(graph, "mes-det")
    written, ctx = _c3(graph, "erp", _cross_vm("erp", "mes", "C1"))

    assert written and ctx.value_mappings_written == 1
    rows = graph.value_mappings()
    assert set(rows) == {"vm-xsys-erp-mes-mil-0"}, rows
    assert rows["vm-xsys-erp-mes-mil-0"]["to_system"] == "mes-mil"
    assert ("vm-xsys-erp-mes-mil-0", "TO_SYSTEM", "mes-mil") in graph.edges


@pytest.mark.parametrize("to_system, why", [
    ("mes", "two sites share the short name"),
    ("plant-7", "no such system"),
    ("", "no target"),
])
def test_c3_rejects_a_target_that_names_no_single_system(graph, to_system, why):
    written, ctx = _c3(graph, "erp", _cross_vm("erp", to_system, "C2"))

    assert not written, why
    assert ctx.value_mappings_written == 0
    assert graph.value_mappings() == {}, why


@pytest.mark.parametrize("from_system, expected", [
    ("mes-mil", "mes-mil"),     # the onboarding system's own id
    ("mes", "mes-mil"),         # its short name, unique once mes-det is gone
    (None, "mes-mil"),          # omitted by the model
])
def test_c3_stores_the_onboarding_system_as_the_source(graph, from_system, expected):
    _drop_system(graph, "mes-det")
    vm = _cross_vm(from_system, "erp", "C3")
    if from_system is None:
        del vm["from_system"]
    written, _ = _c3(graph, "mes-mil", vm)

    assert written
    rows = graph.value_mappings()
    assert [r["from_system"] for r in rows.values()] == [expected], rows


def test_c3_rejects_a_mapping_between_two_other_systems(graph):
    """A c3 row must have the onboarding system on one side; one between two
    other systems is not the onboarding system's to write."""
    written, _ = _c3(graph, "erp", _cross_vm("cmms", "sitewise-direct", "C4"))
    assert not written
    assert graph.value_mappings() == {}


@pytest.mark.parametrize("to_name, drop", [
    ("erp", None),              # exact ids, the onboarding system on the to side
    ("mes", "mes-det"),         # its unique short name on the to side
], ids=["exact", "unique-short-name"])
def test_c3_stores_reversed_endpoints_with_the_onboarding_system_as_source(graph, to_name, drop):
    """The resolve prompt never says which side is the onboarding system. A
    row the model writes the other way round is the same mapping: it is
    stored with the system/field/value triples swapped, so the onboarding
    system is always the source."""
    onboarding = "erp" if to_name == "erp" else "mes-mil"
    if drop:
        _drop_system(graph, drop)
    vm = {"from_system": "cmms", "from_field": "wo.machine", "from_value": "PUMP-7",
          "to_system": to_name, "to_field": "orders.line", "to_value": "L7",
          "concept": "equipment-id", "confidence": 0.9, "method": "semantic_similarity"}
    written, ctx = _c3(graph, onboarding, vm)

    assert written and ctx.value_mappings_written == 1
    vm_id = f"vm-xsys-{onboarding}-cmms-0"
    rows = graph.value_mappings()
    assert set(rows) == {vm_id}, rows
    row = rows[vm_id]
    assert (row["from_system"], row["from_field"], row["from_value"],
            row["to_system"], row["to_field"], row["to_value"]) == (
        onboarding, "orders.line", "L7", "cmms", "wo.machine", "PUMP-7")
    assert {(vm_id, "FROM_SYSTEM", onboarding), (vm_id, "TO_SYSTEM", "cmms")} <= graph.edges


def test_c3_rejects_an_ambiguous_source_short_name(graph):
    """``mes`` names both ``mes-mil`` and ``mes-det``: it is not silently read
    as the onboarding site."""
    written, ctx = _c3(graph, "mes-mil", _cross_vm("mes", "erp", "C7"))
    assert not written and ctx.value_mappings_written == 0
    assert graph.value_mappings() == {}


def test_c3_rejects_a_target_that_resolves_to_the_onboarding_system(graph):
    _drop_system(graph, "mes-det")
    written, _ = _c3(graph, "mes-mil", _cross_vm("mes-mil", "mes", "C5"))
    assert not written
    assert graph.value_mappings() == {}


def test_c3_writes_nothing_when_the_system_inventory_is_unreadable(graph, monkeypatch):
    monkeypatch.setattr(onboard, "neptune_query_checked", _unreadable)
    written, _ = _c3(graph, "erp", _cross_vm("erp", "cmms", "C6"))
    assert not written
    assert graph.value_mappings() == {}


def test_c3_pass_reads_the_inventory_once(graph, monkeypatch):
    reads = []

    def counting(cypher, params=None):
        reads.append(cypher)
        return graph.query(cypher, params)
    monkeypatch.setattr(onboard, "neptune_query_checked", counting)
    ctx = SimpleNamespace(system_id="erp", value_mappings_written=0, nodes=0,
                          body=SimpleNamespace(value_mappings=[
                              _cross_vm("erp", "cmms", f"P{i}") for i in range(4)]))
    onboard._dp_cross_system_vms(ctx)

    assert ctx.value_mappings_written == 4
    assert len(reads) == 1, reads


def test_reset_deletes_a_legacy_short_name_mapping_with_the_last_site(graph):
    """A legacy row stored the short name (``mes``). While a sibling site is
    onboarded it is the sibling's too and survives; the removal of the LAST
    ``mes-*`` site deletes it, as the IdentityRule/JoinPath deletes do."""
    graph.nodes[("ValueMapping", "vm-legacy-mes")] = {
        "id": "vm-legacy-mes", "from_system": "mes", "to_system": "sitewise-direct",
        "from_value": "L1", "to_value": "Line 1", "concept": "production-line"}
    mil = _equipment("mes-mil", "sitewise-direct", "M1")
    det = _equipment("mes-det", "sitewise-direct", "D1")

    onboard._reset_delete_graph_artifacts("mes-mil")
    left = set(graph.value_mappings())
    assert mil not in left
    assert {"vm-legacy-mes", det} <= left, "a sibling site's rows were deleted"

    onboard._reset_delete_graph_artifacts("mes-det")
    assert graph.value_mappings() == {}, graph.value_mappings()


# ── the provision replay of accepted items ──────────────────────────────────

def _accepted(monkeypatch, *items):
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda _sid: list(items))


def test_replay_writes_nothing_when_only_the_surviving_system_is_onboarded(graph, monkeypatch):
    """``cmms`` was removed; its accepted mapping to ``erp`` stays in the
    self-learning table. Re-onboarding ``erp`` alone must not bring it back."""
    _drop_system(graph, "cmms")
    _accepted(monkeypatch, _learned_item("cmms", "erp", "R1"),
              _learned_item("erp", "cmms", "R2", kind="crosswalk"))

    assert onboard._dp_reapply_self_learned("erp") == 0
    assert graph.value_mappings() == {}
    assert not any("ValueMapping" in c for c, _ in graph.writes), graph.writes


def test_replay_writes_once_both_systems_are_onboarded(graph, monkeypatch):
    _drop_system(graph, "cmms")
    _accepted(monkeypatch, _learned_item("cmms", "erp", "R1"),
              _learned_item("sitewise-direct", "erp", "R3"))

    assert onboard._dp_reapply_self_learned("erp") == 1
    assert set(graph.value_mappings()) == {"subj-replayR3"}

    graph.nodes[("System", "cmms")] = {"id": "cmms"}     # cmms onboarded again
    assert onboard._dp_reapply_self_learned("cmms") == 2
    assert set(graph.value_mappings()) == {"subj-replayR1", "subj-replayR3"}
    assert ("subj-replayR1", "FROM_SYSTEM", "cmms") in graph.edges
    assert ("subj-replayR1", "TO_SYSTEM", "erp") in graph.edges


def test_replay_writes_nothing_when_the_system_check_is_unreadable(graph, monkeypatch):
    monkeypatch.setattr(onboard, "neptune_query_checked", _unreadable)
    _accepted(monkeypatch, _learned_item("cmms", "erp", "R4"))

    assert onboard._dp_reapply_self_learned("erp") == 0
    assert graph.value_mappings() == {}


def test_replay_counts_only_mappings_the_graph_accepted(graph, monkeypatch):
    graph.reject = {"MERGE (vm:ValueMapping"}     # the graph refuses the mapping write
    _accepted(monkeypatch, _learned_item("cmms", "erp", "R5"))

    assert onboard._dp_reapply_self_learned("erp") == 0


# ── c3 validation does not depend on orientation ────────────────────────────

#: A sentence-like value (> 50 characters with spaces): a definition, not an
#: identifier, on either side of a mapping.
_DEFINITION = "A pump that circulates coolant through the cooling loop of line one"


def _oriented(onboarding_value, peer_value, orientation):
    """The same erp<->cmms pair, written by the model either way round."""
    onb = ("erp", "orders.line", onboarding_value)
    peer = ("cmms", "wo.machine", peer_value)
    first, second = (onb, peer) if orientation == "as-written" else (peer, onb)
    return {"from_system": first[0], "from_field": first[1], "from_value": first[2],
            "to_system": second[0], "to_field": second[1], "to_value": second[2],
            "concept": "equipment-id", "confidence": 0.9, "method": "semantic_similarity"}


@pytest.mark.parametrize("orientation", ["as-written", "reversed"])
@pytest.mark.parametrize("long_side", ["onboarding", "peer"])
def test_c3_rejects_a_definitional_value_on_either_side_in_either_orientation(
        graph, long_side, orientation):
    """A definition is rejected whichever system's value it is and whichever
    way round the model wrote the row: the check runs on the stored
    (canonical) row and reads both values."""
    pair = (_DEFINITION, "PUMP-7") if long_side == "onboarding" else ("L7", _DEFINITION)
    written, ctx = _c3(graph, "erp", _oriented(*pair, orientation))

    assert not written and ctx.value_mappings_written == 0
    assert graph.value_mappings() == {}


@pytest.mark.parametrize("pair", [("L7", "PUMP-7"), (_DEFINITION, "PUMP-7"), ("L7", _DEFINITION)],
                         ids=["identifiers", "definition-on-onboarding-side",
                              "definition-on-peer-side"])
def test_c3_gives_the_same_verdict_and_row_in_both_orientations(graph, pair):
    outcomes = []
    for orientation in ("as-written", "reversed"):
        written, _ = _c3(graph, "erp", _oriented(*pair, orientation))
        outcomes.append((written, {k: dict(v) for k, v in graph.value_mappings().items()},
                         {e for e in graph.edges if e[0].startswith("vm-xsys-")}))
        for vm_id in list(graph.value_mappings()):
            del graph.nodes[("ValueMapping", vm_id)]
        graph.edges = {e for e in graph.edges if not e[0].startswith("vm-xsys-")}

    assert outcomes[0] == outcomes[1], outcomes


# ── one statement per mapping (c3 and replay) ───────────────────────────────

_PARTS = ["MERGE (vm:ValueMapping", "FROM_SYSTEM]", "TO_SYSTEM]"]


def _mapping_edges(graph, vm_id):
    return {e for e in graph.edges if e[0] == vm_id}


@pytest.mark.parametrize("rejected", _PARTS, ids=["node", "from-edge", "to-edge"])
def test_a_rejected_c3_write_leaves_no_mapping_and_counts_nothing(graph, rejected):
    """The graph refuses the write of the node or of either edge: no half
    mapping is left (one edge would still reach that system's route seed) and
    nothing is counted."""
    graph.reject = {rejected}
    written, ctx = _c3(graph, "erp", _cross_vm("erp", "cmms", "W1"))

    assert not written and ctx.value_mappings_written == 0
    assert graph.value_mappings() == {}
    assert not _mapping_edges(graph, "vm-xsys-erp-cmms-0")


@pytest.mark.parametrize("rejected", _PARTS, ids=["node", "from-edge", "to-edge"])
def test_a_rejected_replay_write_leaves_no_mapping_and_counts_nothing(graph, monkeypatch, rejected):
    graph.reject = {rejected}
    _accepted(monkeypatch, _learned_item("cmms", "erp", "W2"))

    assert onboard._dp_reapply_self_learned("erp") == 0
    assert graph.value_mappings() == {}
    assert not _mapping_edges(graph, "subj-replayW2")


def test_c3_writes_nothing_when_an_endpoint_system_is_gone_at_write_time(graph):
    """The inventory was read before ``cmms`` was removed: the write itself
    matches both System nodes, so it writes nothing rather than a node with
    one edge."""
    inventory = set(SYSTEMS)
    _drop_system(graph, "cmms")
    ctx = SimpleNamespace(system_id="erp", value_mappings_written=0, nodes=0)

    assert not onboard._dp_write_cross_vm(ctx, _cross_vm("erp", "cmms", "G1"), 0, inventory)
    assert ctx.value_mappings_written == 0
    assert graph.value_mappings() == {}
    assert not _mapping_edges(graph, "vm-xsys-erp-cmms-0")


def test_replay_writes_nothing_when_an_endpoint_system_is_gone_at_write_time(graph):
    inventory = set(SYSTEMS)
    _drop_system(graph, "cmms")

    assert onboard._dp_replay_learned_vm(_learned_item("cmms", "erp", "G2"), inventory) is False
    assert graph.value_mappings() == {}
    assert not _mapping_edges(graph, "subj-replayG2")


def test_a_mapping_is_one_graph_statement_counted_once(graph, monkeypatch):
    before = len(graph.writes)
    written, ctx = _c3(graph, "erp", _cross_vm("erp", "cmms", "S1"))
    c3_statements = [c for c, _ in graph.writes[before:] if "ValueMapping" in c]

    assert written and ctx.value_mappings_written == 1
    assert len(c3_statements) == 1, c3_statements
    assert _mapping_edges(graph, "vm-xsys-erp-cmms-0") == {
        ("vm-xsys-erp-cmms-0", "FROM_SYSTEM", "erp"), ("vm-xsys-erp-cmms-0", "TO_SYSTEM", "cmms")}

    _accepted(monkeypatch, _learned_item("cmms", "erp", "S2"))
    before = len(graph.writes)
    assert onboard._dp_reapply_self_learned("erp") == 1
    replay_statements = [c for c, _ in graph.writes[before:] if "ValueMapping" in c]
    assert len(replay_statements) == 1, replay_statements
    assert _mapping_edges(graph, "subj-replayS2") == {
        ("subj-replayS2", "FROM_SYSTEM", "cmms"), ("subj-replayS2", "TO_SYSTEM", "erp")}


def test_c3_and_replay_write_through_the_one_helper(graph, monkeypatch):
    calls = []
    real = getattr(onboard, "_write_value_mapping", None)

    def spy(vm_id, *args, **kwargs):
        calls.append(vm_id)
        return real(vm_id, *args, **kwargs) if real else False
    monkeypatch.setattr(onboard, "_write_value_mapping", spy, raising=False)
    _c3(graph, "erp", _cross_vm("erp", "cmms", "H1"))
    _accepted(monkeypatch, _learned_item("cmms", "erp", "H2"))
    onboard._dp_reapply_self_learned("erp")

    assert calls == ["vm-xsys-erp-cmms-0", "subj-replayH2"], calls


def test_a_refused_mapping_write_still_fails_the_provision_transaction(graph):
    """Inside a provision a refused graph write raises, as ``neptune_write``
    does; a write that matched no System pair is not a failure."""
    token = onboard._STRICT_PROVISION_WRITES.set(True)
    try:
        _drop_system(graph, "sitewise-direct")
        ctx = SimpleNamespace(system_id="erp", value_mappings_written=0, nodes=0)
        assert not onboard._dp_write_cross_vm(
            ctx, _cross_vm("erp", "sitewise-direct", "T1"), 0, set(SYSTEMS))
        assert ctx.value_mappings_written == 0
        graph.reject = {"MERGE (vm:ValueMapping"}
        with pytest.raises(RuntimeError, match="provision graph write failed"):
            _c3(graph, "erp", _cross_vm("erp", "cmms", "T2"))
    finally:
        onboard._STRICT_PROVISION_WRITES.reset(token)


def test_the_returning_write_seam_answers_rows_or_none(monkeypatch):
    import agents.db as db
    monkeypatch.setattr(db, "_graph_backend", lambda: "local")
    monkeypatch.setattr(db, "_memgraph_run", lambda cypher, parameters=None: [{"id": "vm-1"}])
    assert db.neptune_write_returning("MERGE (n:X {id: $i}) RETURN n.id AS id", {"i": "vm-1"}) \
        == [{"id": "vm-1"}]

    def refused(cypher, parameters=None):
        raise RuntimeError("engine refused the statement")
    monkeypatch.setattr(db, "_memgraph_run", refused)
    assert db.neptune_write_returning("MERGE (n:X {id: $i}) RETURN n.id AS id", {"i": "vm-1"}) is None


# ── every other ValueMapping writer: one statement too ───────────────────────

#: A token of each piece of a mapping write; refusing one refuses the
#: statement that contains it (the one-statement write contains them all).
_EQUIPMENT_PARTS = [*_PARTS, "MAPS_TO_ASSET"]


def _mapping_writes(graph):
    """The statements that write a ValueMapping node (not a retirement)."""
    return [c for c, _ in graph.writes if "MERGE (vm:ValueMapping" in c]


def _equipment_rows(*tags, assets=True):
    """Equipment mappings for ``tags``; with ``assets`` each one's Asset is
    added to the graph (``uuid-<tag>``), as the walk that generated it did."""
    for tag in (tags if assets else ()):
        _CURRENT["graph"].add_asset(f"uuid-{tag}")
    return [onboard._equipment_vm("cmms", "assets.asset_tag", tag,
                                  {"system": "sitewise-direct", "name": f"Asset {tag}",
                                   "source_id": f"uuid-{tag}", "path": f"/Plant/Asset {tag}"},
                                  0.95, "value_format_match") for tag in tags]


def _seed_complete_duplicate(graph, vm):
    """A prior generated row (legacy id) for the same equipment identity,
    complete the way a walk wrote it: the mapping properties, both System
    edges, its element id and path, its Asset and exactly one MAPS_TO_ASSET
    anchor. This is the row a retirement would remove.

    Its Asset is a different one (``<to_element_id>-prior``, as when an asset
    was re-created between walks), so it never counts toward the new mapping's
    exactly-one-Asset check. The duplicate identity does not include the
    element id, so it is still the same identity."""
    old_id = "vm-equip-legacy-0"
    prior_element = f"{vm['to_element_id']}-prior"
    graph.nodes[("ValueMapping", old_id)] = {
        "id": old_id,
        **{k: vm[k] for k in ("from_system", "from_field", "from_value", "to_system",
                              "to_field", "to_value", "concept", "confidence", "method")},
        "to_element_id": prior_element, "to_path": vm["to_path"]}
    graph.edges |= {(old_id, "FROM_SYSTEM", vm["from_system"]), (old_id, "TO_SYSTEM", vm["to_system"])}
    graph.anchors.add((old_id, graph.add_asset(prior_element)))
    return old_id


def _assert_prior_copy_intact(graph, old_id, vm):
    """The complete prior copy survived untouched: node, element id, both
    System edges and its one anchor to its own Asset."""
    assert old_id in graph.value_mappings(), "the complete duplicate was retired"
    assert graph.value_mappings()[old_id]["to_element_id"] == f"{vm['to_element_id']}-prior"
    assert _mapping_edges(graph, old_id) == {(old_id, "FROM_SYSTEM", vm["from_system"]),
                                             (old_id, "TO_SYSTEM", vm["to_system"])}
    assert graph.anchors_of(old_id) == {f"{vm['to_element_id']}-prior"}
    assert sum(1 for v, _ in graph.anchors if v == old_id) == 1


def test_the_equipment_pass_writes_one_statement_and_counts_once(graph, monkeypatch):
    (vm,) = _equipment_rows("E1")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    old_id = _seed_complete_duplicate(graph, vm)

    assert onboard._persist_equipment_value_mappings() == 1

    vm_id = onboard._equipment_vm_id(vm)
    statements = _mapping_writes(graph)
    assert len(statements) == 1, statements
    assert _mapping_edges(graph, vm_id) == {(vm_id, "FROM_SYSTEM", "cmms"),
                                            (vm_id, "TO_SYSTEM", "sitewise-direct")}
    assert graph.value_mappings()[vm_id]["to_element_id"] == "uuid-E1"
    assert (vm_id, "uuid-E1") in graph.anchors
    assert set(graph.value_mappings()) == {vm_id}, "the complete duplicate is retired after the write"
    assert old_id not in graph.value_mappings()


@pytest.mark.parametrize("rejected", _PARTS, ids=["node", "from-edge", "to-edge"])
def test_a_refused_equipment_write_leaves_no_mapping_and_counts_nothing(graph, monkeypatch, rejected):
    (vm,) = _equipment_rows("E2")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    graph.reject = {rejected}

    assert onboard._persist_equipment_value_mappings() == 0
    assert graph.value_mappings() == {}
    assert not _mapping_edges(graph, onboard._equipment_vm_id(vm))


@pytest.mark.parametrize("rejected", _EQUIPMENT_PARTS,
                         ids=["node", "from-edge", "to-edge", "asset-anchor"])
def test_the_equipment_pass_never_retires_a_complete_duplicate_when_its_replacement_failed(
        graph, monkeypatch, rejected):
    """Replace-not-accrete retires the prior generated row only after the new
    row's statement (mapping, edges and asset anchor) returned its id. A failure
    anywhere keeps the old, complete row (it may be the only one left for that
    identity) and counts nothing."""
    (vm,) = _equipment_rows("E3")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    old_id = _seed_complete_duplicate(graph, vm)
    graph.reject = {rejected}

    assert onboard._persist_equipment_value_mappings() == 0
    _assert_prior_copy_intact(graph, old_id, vm)
    new_id = onboard._equipment_vm_id(vm)
    # The anchor is part of the one mapping statement: refusing any piece
    # refuses all of it.
    assert new_id not in graph.value_mappings() and not _mapping_edges(graph, new_id)
    assert not graph.anchors_of(new_id)


def test_the_equipment_pass_writes_nothing_when_an_endpoint_system_is_gone(graph, monkeypatch):
    (vm,) = _equipment_rows("E4")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    _drop_system(graph, "sitewise-direct")

    assert onboard._persist_equipment_value_mappings() == 0
    assert graph.value_mappings() == {}


def _legacy_c3b(monkeypatch, rows):
    monkeypatch.setattr(onboard, "catalog",
                        SimpleNamespace(get=lambda sid: {"schema": "cmms_db", "status": "onboarded"}))
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda *_a: list(rows))
    ctx = SimpleNamespace(system_id="cmms", value_mappings_written=0, nodes=0)
    onboard._dp_equipment_vms_legacy(ctx)
    return ctx


def test_the_legacy_c3b_block_writes_one_statement_and_counts_once(graph, monkeypatch):
    """The block is inert today (its generator call returns nothing), but it
    still writes through the same one-statement write if it ever runs."""
    (vm,) = _equipment_rows("L1")
    ctx = _legacy_c3b(monkeypatch, [vm])

    assert ctx.value_mappings_written == 1
    statements = _mapping_writes(graph)
    assert len(statements) == 1, statements
    vm_id = "vm-equip-L1-0"
    assert _mapping_edges(graph, vm_id) == {(vm_id, "FROM_SYSTEM", "cmms"),
                                            (vm_id, "TO_SYSTEM", "sitewise-direct")}


@pytest.mark.parametrize("rejected", _PARTS, ids=["node", "from-edge", "to-edge"])
def test_a_refused_legacy_c3b_write_leaves_no_mapping_and_counts_nothing(graph, monkeypatch, rejected):
    graph.reject = {rejected}
    ctx = _legacy_c3b(monkeypatch, _equipment_rows("L2"))

    assert ctx.value_mappings_written == 0
    assert graph.value_mappings() == {}
    assert not _mapping_edges(graph, "vm-equip-L2-0")


def _steward_item(src="cmms", dst="erp", tag="S9"):
    return {"from_system": src, "from_field": "parts.part_no", "from_value": tag,
            "to_system": dst, "to_field": "items.item", "to_value": f"I-{tag}", "confidence": 0.9}


def test_the_steward_accept_writes_one_statement(graph):
    vm_id = self_learning._apply_value_mapping(_steward_item(), "subj-acceptS9", 3,
                                               "part-number", "crosswalk")

    assert vm_id == "subj-acceptS9"
    statements = [c for c, _ in graph.writes if "ValueMapping" in c]
    assert len(statements) == 1, statements
    assert "LINKS_CONCEPT" in statements[0]
    row = graph.value_mappings()[vm_id]
    assert (row["method"], row["version"], row["concept"]) == ("crosswalk_confirmed", 3, "part-number")
    assert _mapping_edges(graph, vm_id) == {(vm_id, "FROM_SYSTEM", "cmms"), (vm_id, "TO_SYSTEM", "erp")}


@pytest.mark.parametrize("rejected", _PARTS, ids=["node", "from-edge", "to-edge"])
def test_a_refused_steward_accept_raises_and_leaves_no_mapping(graph, rejected):
    graph.reject = {rejected}
    with pytest.raises(RuntimeError, match="ValueMapping|Neptune write failed"):
        self_learning._apply_value_mapping(_steward_item(tag="S8"), "subj-acceptS8", 1,
                                           "part-number", "value_mapping")

    assert graph.value_mappings() == {}
    assert not _mapping_edges(graph, "subj-acceptS8")


def test_a_steward_accept_whose_system_is_gone_raises_and_writes_nothing(graph):
    """The System pre-check proceeds on an indeterminate probe (here: patched
    to pass); the statement itself matches both System nodes, so a missing
    one writes nothing and the accept fails with the same actionable error."""
    _drop_system(graph, "erp")
    with pytest.raises(ValueError, match="does not exist in the knowledge graph"):
        self_learning._apply_value_mapping(_steward_item(tag="S7"), "subj-acceptS7", 1,
                                           "part-number", "value_mapping")

    assert graph.value_mappings() == {}
    assert not _mapping_edges(graph, "subj-acceptS7")


def test_every_value_mapping_writer_goes_through_the_one_statement(graph, monkeypatch):
    calls = []
    real = getattr(onboard, "_upsert_value_mapping", None)

    def spy(vm_id, *args, **kwargs):
        calls.append(vm_id)
        return real(vm_id, *args, **kwargs) if real else "failed"
    monkeypatch.setattr(onboard, "_upsert_value_mapping", spy, raising=False)
    (equip,) = _equipment_rows("A1")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [equip])
    onboard._persist_equipment_value_mappings()
    _legacy_c3b(monkeypatch, _equipment_rows("A2"))
    _c3(graph, "erp", _cross_vm("erp", "cmms", "A3"))
    _accepted(monkeypatch, _learned_item("cmms", "erp", "A4"))
    onboard._dp_reapply_self_learned("erp")
    try:
        self_learning._apply_value_mapping(_steward_item(tag="A5"), "subj-acceptA5", 1,
                                           "part-number", "value_mapping")
    except (RuntimeError, ValueError):
        pass

    assert calls == [onboard._equipment_vm_id(equip), "vm-equip-A2-0", "vm-xsys-erp-cmms-0",
                     "subj-replayA4", "subj-acceptA5"], calls


# ── the equipment anchor: exactly one Asset, reconciled, read back ───────────

def _audit_rows(graph):
    """``CYPHER_EQUIPMENT_VM_ASSET_EDGES`` evaluated on the fake graph: one row
    per equipment mapping with a non-empty ``to_element_id``."""
    return [{"vm_id": vm_id, "to_element_id": props["to_element_id"],
             "edge_count": sum(1 for v, _ in graph.anchors if v == vm_id),
             "asset_source_ids": [graph.nodes[("Asset", k)]["source_id"]
                                  for v, k in graph.anchors if v == vm_id]}
            for vm_id, props in graph.value_mappings().items()
            if props.get("concept") == "equipment" and props.get("to_element_id")]


def _audit_passes(graph):
    """Both family-(e) facts of the onboarding audit hold on the fake graph."""
    from tests.audit import audit_onboarding_graph as audit
    links = audit.assert_equipment_vm_asset_links(_audit_rows(graph))
    bare = [v for v, _ in graph.anchors
            if not (graph.value_mappings().get(v) or {}).get("to_element_id")]
    return links["pass"] and not bare, (links, bare)


@pytest.mark.parametrize("assets", [0, 2], ids=["no-asset", "two-assets"])
def test_an_equipment_mapping_needs_exactly_one_asset(graph, monkeypatch, assets):
    """With no Asset, or two Assets sharing the source id, the mapping is not
    written, not counted, and the prior complete duplicate is kept: an anchor
    to the wrong Asset (or to two) sends equipment questions to the wrong
    telemetry."""
    (vm,) = _equipment_rows("N1", assets=False)
    for i in range(assets):
        graph.add_asset("uuid-N1", key=f"uuid-N1#{i}")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    old_id = _seed_complete_duplicate(graph, vm)

    assert onboard._persist_equipment_value_mappings() == 0

    new_id = onboard._equipment_vm_id(vm)
    assert new_id not in graph.value_mappings()
    assert not graph.anchors_of(new_id)
    _assert_prior_copy_intact(graph, old_id, vm)
    assert _audit_passes(graph)[0], _audit_passes(graph)[1]


def test_one_asset_anchors_the_mapping_to_that_asset_only(graph, monkeypatch):
    (vm,) = _equipment_rows("N2")
    graph.add_asset("uuid-other")                    # another Asset, never touched
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])

    assert onboard._persist_equipment_value_mappings() == 1

    vm_id = onboard._equipment_vm_id(vm)
    assert graph.anchors == {(vm_id, "uuid-N2")}
    assert _audit_passes(graph)[0], _audit_passes(graph)[1]


def test_an_element_id_change_moves_the_anchor_instead_of_adding_one(graph, monkeypatch):
    """The mapping id does not include the element id, so a later walk can
    keep the id and move the asset. The anchor is reconciled: the old edge
    goes, the new one is the only one."""
    (first,) = _equipment_rows("N3")
    moved = {**first, "to_element_id": "uuid-N3-moved"}
    graph.add_asset("uuid-N3-moved")
    vm_id = onboard._equipment_vm_id(first)
    assert onboard._equipment_vm_id(moved) == vm_id

    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [first])
    assert onboard._persist_equipment_value_mappings() == 1
    assert graph.anchors_of(vm_id) == {"uuid-N3"}
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [moved])
    assert onboard._persist_equipment_value_mappings() == 1

    assert graph.anchors_of(vm_id) == {"uuid-N3-moved"}
    assert graph.value_mappings()[vm_id]["to_element_id"] == "uuid-N3-moved"
    assert _audit_passes(graph)[0], _audit_passes(graph)[1]


def test_an_unreadable_asset_count_writes_nothing(graph, monkeypatch):
    (vm,) = _equipment_rows("N4")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    old_id = _seed_complete_duplicate(graph, vm)

    def unreadable_assets(cypher, params=None):
        if ":Asset {source_id" in cypher:
            raise GraphQueryError("TimeoutError", "the graph did not answer")
        return graph.query(cypher, params)
    monkeypatch.setattr(onboard, "neptune_query_checked", unreadable_assets)

    assert onboard._persist_equipment_value_mappings() == 0
    assert onboard._equipment_vm_id(vm) not in graph.value_mappings()
    _assert_prior_copy_intact(graph, old_id, vm)


def test_an_anchor_that_does_not_read_back_is_not_counted(graph, monkeypatch):
    """The write returned its id but the anchor readback does not show exactly
    the one expected Asset (here: the graph answers with no anchor): the
    mapping is not counted and the prior duplicate is kept."""
    (vm,) = _equipment_rows("N5")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    old_id = _seed_complete_duplicate(graph, vm)

    def no_anchor(cypher, params=None):
        if "-[:MAPS_TO_ASSET]->(" in cypher and cypher.startswith("MATCH (vm:ValueMapping {id:"):
            return [{"anchors": 0, "correct": 0}]
        return graph.query(cypher, params)
    monkeypatch.setattr(onboard, "neptune_query_checked", no_anchor)

    assert onboard._persist_equipment_value_mappings() == 0
    _assert_prior_copy_intact(graph, old_id, vm)


# ── the equipment duplicate retirement is checked ───────────────────────────

def test_a_refused_retirement_is_not_reported_complete(graph, monkeypatch, caplog):
    """The new mapping landed whole, but the graph refused to retire the prior
    generated duplicate: both rows are live, so the pass does not count the
    identity as done, says so, and the next pass retires it."""
    (vm,) = _equipment_rows("D1")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    old_id = _seed_complete_duplicate(graph, vm)
    graph.reject = {"DETACH DELETE"}

    with caplog.at_level("WARNING", logger="agents.onboard"):
        assert onboard._persist_equipment_value_mappings() == 0
    new_id = onboard._equipment_vm_id(vm)
    assert {new_id, old_id} <= set(graph.value_mappings())
    assert any(new_id in r.getMessage() and "duplicate" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]

    graph.reject = set()
    assert onboard._persist_equipment_value_mappings() == 1
    assert set(graph.value_mappings()) == {new_id}


def test_a_retirement_that_leaves_a_duplicate_is_not_reported_complete(graph, monkeypatch):
    """The delete ran but the readback still finds a generated duplicate."""
    (vm,) = _equipment_rows("D2")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    old_id = _seed_complete_duplicate(graph, vm)
    graph.ignore_deletes = True

    assert onboard._persist_equipment_value_mappings() == 0
    assert old_id in graph.value_mappings()


def test_an_unreadable_retirement_readback_is_not_reported_complete(graph, monkeypatch):
    (vm,) = _equipment_rows("D3")
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    _seed_complete_duplicate(graph, vm)

    def unreadable_duplicates(cypher, params=None):
        if "STARTS WITH $prefix" in cypher:
            raise GraphQueryError("TimeoutError", "the graph did not answer")
        return graph.query(cypher, params)
    monkeypatch.setattr(onboard, "neptune_query_checked", unreadable_duplicates)

    assert onboard._persist_equipment_value_mappings() == 0


# ── Correlations naming the removed system ──────────────────────────────────

def _graduated(src, dst, tag):
    """The accepted graduated finding (self_learning._apply_correlation):
    from_system / to_system properties + FROM_SYSTEM / TO_SYSTEM edges."""
    cid = f"subj-corr{tag}"
    self_learning._apply_correlation(
        {"from_system": src, "to_system": dst, "query_context": f"finding {tag}",
         "confidence": 0.8, "concept": "downtime", "mapping_id": f"m-corr-{tag}"}, cid, 1)
    return cid


def _taught(src, dst, tag):
    """A correlation taught through /learn (resolve.thread_learn): no system
    properties, SOURCE_SYSTEM / TARGET_SYSTEM edges only."""
    out = resolve.thread_learn(LearnRequest(
        type="correlation", concept_a=f"temp{tag}", concept_b="scrap", pattern=f"p {tag}",
        confidence=0.7, system_a=src, system_b=dst, plant_id="plant-a"))
    return out["id"]


CORRELATION_WRITERS = {"graduated": _graduated, "taught": _taught}


@pytest.mark.parametrize("side", ["from", "to"])
@pytest.mark.parametrize("writer", sorted(CORRELATION_WRITERS))
def test_removal_deletes_a_correlation_naming_the_system(graph, writer, side):
    write = CORRELATION_WRITERS[writer]
    gone = write("cmms", "erp", "A") if side == "from" else write("erp", "cmms", "A")
    kept = write("erp", "sitewise-direct", "B")
    assert {gone, kept} <= set(graph.correlations()), graph.correlations()

    onboard._reset_delete_graph_artifacts("cmms")

    left = graph.correlations()
    assert gone not in left, f"{writer} correlation naming the removed system ({side}) survived"
    assert kept in left


def test_removal_keeps_a_correlation_with_no_system(graph):
    """A /learn correlation taught without systems is keyed by concepts and
    plant only; no removal can claim it."""
    free = _taught("", "", "C")
    assert graph.correlations()[free].get("plant_id") == "plant-a"
    assert not any(e[0] == free for e in graph.edges)

    onboard._reset_delete_graph_artifacts("cmms")

    assert free in graph.correlations()


def test_removal_deletes_a_legacy_correlation_by_its_properties_alone(graph):
    """A graduated correlation whose edge to the removed system is already
    gone (or never materialised) still names it by property."""
    cid = _graduated("cmms", "erp", "D")
    graph.edges = {e for e in graph.edges if e[0] != cid}

    onboard._reset_delete_graph_artifacts("cmms")

    assert cid not in graph.correlations()


@pytest.mark.parametrize("fails", ["c.from_system = $sid", "SOURCE_SYSTEM|TARGET_SYSTEM"])
def test_a_failed_correlation_delete_keeps_the_system_node(graph, monkeypatch, fails):
    _graduated("cmms", "erp", "E")

    def failing(cypher, params=None):
        return False if fails in cypher else graph.write(cypher, params)
    monkeypatch.setattr(onboard, "neptune_write", failing)
    with pytest.raises(onboard.ResetIncompleteError, match="Correlation"):
        onboard._reset_delete_graph_artifacts("cmms")
    assert ("System", "cmms") in graph.nodes


# ── the removal is idempotent ───────────────────────────────────────────────

@pytest.mark.parametrize("sid", ["cmms", "mes-mil"])
def test_a_second_removal_deletes_nothing_new_and_raises_nothing(graph, sid):
    for w in WRITERS.values():
        w(sid, "sitewise-direct", "I1")
        w("erp", "mes-det", "I2")
    _graduated(sid, "erp", "I3")
    _taught("erp", sid, "I4")
    _graduated("erp", "sitewise-direct", "I5")

    onboard._reset_delete_graph_artifacts(sid)
    after_first = graph.snapshot()
    onboard._reset_delete_graph_artifacts(sid)

    assert graph.snapshot() == after_first
    assert graph.value_mappings() and graph.correlations()


# ── the steward graph repair ────────────────────────────────────────────────

def test_repair_restores_mapping_edges_from_the_properties_the_writers_set(graph):
    import agents.server as server
    vm_id = _equipment("cmms", "sitewise-direct", "R1")
    graph.edges.clear()                     # e.g. a replay that ran before its peer existed

    repairs = server._repair_system_edges(graph.query, graph.write)

    assert (vm_id, "FROM_SYSTEM", "cmms") in graph.edges
    assert (vm_id, "TO_SYSTEM", "sitewise-direct") in graph.edges
    assert f"ValueMapping({vm_id}) --FROM_SYSTEM--> System(cmms)" in repairs
    assert f"ValueMapping({vm_id}) --TO_SYSTEM--> System(sitewise-direct)" in repairs


def test_repair_retires_a_mapping_whose_other_system_is_gone(graph):
    """A mapping left behind by a removal made before this fix names a system
    with no :System node. Repair retires it (both sides), never links its live
    side, and creates no System node from a mapping."""
    import agents.server as server
    stale = _equipment("cmms", "sitewise-direct", "R2")
    stale_to = _replayed("erp", "cmms", "R3")
    healthy = _equipment("erp", "sitewise-direct", "R4")
    _drop_system(graph, "cmms")             # removed under the old, no-op delete
    graph.edges.clear()

    repairs = server._repair_system_edges(graph.query, graph.write)

    assert set(graph.value_mappings()) == {healthy}
    assert ("System", "cmms") not in graph.nodes
    assert not any("MERGE (s:System" in cypher and params.get("sid") == "cmms"
                   for cypher, params in graph.writes), graph.writes
    assert not any(e[0] in (stale, stale_to) for e in graph.edges), graph.edges
    assert any(f"ValueMapping({stale})" in r and "retired" in r for r in repairs), repairs
    assert any(f"ValueMapping({stale_to})" in r and "retired" in r for r in repairs), repairs
    assert not any("System(cmms)" in r for r in repairs), repairs
    assert (healthy, "TO_SYSTEM", "sitewise-direct") in graph.edges
    # Repair has no Correlation step: it never re-links one either.
    assert not any("Correlation" in c for c, _ in graph.writes)


def test_repair_does_not_report_a_retirement_the_graph_rejected(graph, monkeypatch):
    import agents.server as server
    stale = _equipment("cmms", "sitewise-direct", "R5")
    _drop_system(graph, "cmms")
    graph.edges.clear()

    def rejecting(cypher, params=None):
        return False if "DETACH DELETE" in cypher else graph.write(cypher, params)

    repairs = server._repair_system_edges(graph.query, rejecting)

    assert stale in graph.value_mappings()
    assert not any(stale in r for r in repairs), repairs
    assert not any(e[0] == stale for e in graph.edges), "a stale mapping was half-repaired"


@pytest.mark.parametrize("broken", ["count", "scan"])
def test_repair_deletes_nothing_when_the_system_inventory_is_unreadable(graph, broken):
    """``neptune_query`` degrades a failed read to ``[]``: a missing count, or
    a scan shorter than its count, is an unverified inventory, and an
    unverified inventory retires nothing (it would read every system as
    gone)."""
    import agents.server as server
    vm_ids = [_equipment("cmms", "sitewise-direct", "U1"), _replayed("erp", "mes-mil", "U2")]
    graph.edges.clear()

    def degraded(cypher, params=None):
        if "(s:System)" in cypher:
            if broken == "count":
                return [] if "count(" in cypher else graph.query(cypher, params)
            return [] if "count(" not in cypher else graph.query(cypher, params)
        return graph.query(cypher, params)

    repairs = server._repair_system_edges(degraded, graph.write)

    assert set(vm_ids) <= set(graph.value_mappings())
    assert not any("ValueMapping" in c and "DETACH DELETE" in c for c, _ in graph.writes)
    assert not any("ValueMapping(" in r for r in repairs), repairs
