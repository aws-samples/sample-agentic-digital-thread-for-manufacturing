# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The route seed never routes to, explains from or translates into a system
whose removal has started.

A removal writes ``removal_incomplete = true`` on the System node and then
deletes the system's runtime first; a removal that fails after that keeps the
node, its rules and its authority rule (for audit) with the marker still set.
The seed's three reads (inventory, rule traversal, authority) leave such a
system out in their Cypher, before any ordering or 50-item cap
(``tests/integration/test_route_seed_memgraph.py`` runs that Cypher on a real
engine). A removal can also start WHILE the seed is being built, so one
final marker read follows the three reads and every retained row is
filtered by it before the seed is composed.

The fake graph here answers the four reads. Once the system is marked, a
read "as the Cypher does" leaves it out (a row rooted at it, and every
nested shared concept or rule naming it); a read that ran before the
marker was written still names it. Each
scenario checks that nothing the seed carries (systems, shared concepts,
every rule kind, concept authority, identifier translations and owners)
names the marked system, while everything between the served systems is
unchanged. Fixture ids are generic.
"""
from __future__ import annotations

import copy
import json
import re

import pytest

import agents.identity_affinity as ia
import agents.orchestrator as orch
from agents.catalog import REMOVING_SYSTEMS_READ

SERVED = "sys-a"
PEER = "sys-b"
REMOVING = "sys-x"   # its removal starts at the point each scenario names

QUESTION = ("What is the state of X-2847 and Y-3301, and which lot did run "
            "RUN-20261006-90001 draw?")

_SEED_READS = (orch._ROUTE_SEED_INVENTORY_CYPHER, orch._ROUTE_SEED_CYPHER,
               orch._ROUTE_SEED_AUTHORITY_CYPHER)


def _inventory() -> list:
    return [{"system_id": sid, "system_name": sid.upper(), "plants": ["site-1"],
             "capabilities": f"{sid} records", "connection_type": "PostgreSQL",
             "query_guidance": "Ask bounded questions."} for sid in (SERVED, PEER, REMOVING)]


def _join(jid, frm, to):
    return {"id": jid, "from_system": frm, "from_field": "assets.asset_code", "to_system": to,
            "to_field": "equipment.code", "transform": "identity", "confidence": 0.9,
            "method": "user_confirmed"}


def _vm(frm, to):
    return {"from_system": frm, "from_field": "asset_code", "to_system": to, "to_field": "code",
            "concept": "equipment-id", "method": "user_confirmed", "confidence": 0.9}


def _pattern_rule(rid, src, tgt, src_pat, tgt_pat):
    return {"id": rid, "concept": "equipment-id", "source_system": src, "source_field": "asset_code",
            "target_system": tgt, "target_field": "code", "source_pattern": src_pat,
            "target_pattern": tgt_pat, "confidence": 0.9, "method": "user_confirmed"}


def _exact_rule(rid, src, tgt):
    return {"id": rid, "concept": "production-run", "source_system": src, "source_field": "run_number",
            "target_system": tgt, "target_field": "batch_id", "source_pattern": "",
            "target_pattern": "", "confidence": 0.95, "method": ia.EXACT_METHOD,
            "transform": ia.EXACT_TRANSFORM,
            "provenance": ("12 exact values of production_runs.run_number exist in sys "
                           "shipment_items.batch_id (membership-probed); both columns shape "
                           "AAA-99999999-99999; concept production-run; 40 and 40 distinct "
                           "values sampled")}


def _rule_rows() -> list:
    to_removing = [_pattern_rule("ir-a-x", SERVED, REMOVING, "X-{id}", "PLANT-0{id}"),
                   _exact_rule("ir-a-x-exact", SERVED, REMOVING)]
    to_peer = [_pattern_rule("ir-a-b", SERVED, PEER, "Y-{id}", "B-{id}")]
    return [
        {"system_id": SERVED, "mapped_concepts": ["equipment-id", "machine-state"],
         "shared_concepts": [{"concept": "equipment-id", "peer_system": REMOVING},
                             {"concept": "equipment-id", "peer_system": PEER}],
         "join_paths": [_join("jp-a-x", SERVED, REMOVING), _join("jp-a-b", SERVED, PEER)],
         "value_mappings": [_vm(SERVED, REMOVING), _vm(SERVED, PEER)],
         "identity_rules": to_removing + to_peer},
        {"system_id": PEER, "mapped_concepts": ["equipment-id"],
         "shared_concepts": [{"concept": "equipment-id", "peer_system": SERVED}],
         "join_paths": [_join("jp-a-b", SERVED, PEER)], "value_mappings": [_vm(SERVED, PEER)],
         "identity_rules": to_peer},
        {"system_id": REMOVING, "mapped_concepts": ["equipment-id", "machine-state"],
         "shared_concepts": [{"concept": "equipment-id", "peer_system": SERVED}],
         "join_paths": [_join("jp-a-x", SERVED, REMOVING)], "value_mappings": [_vm(SERVED, REMOVING)],
         "identity_rules": to_removing},
    ]


def _authority_rows() -> list:
    return [{"id": f"ar-{REMOVING}-machine-state", "concept": "machine-state", "system_id": REMOVING,
             "method": "user_confirmed", "confidence": 1.0},
            {"id": f"ar-{SERVED}-equipment-id", "concept": "equipment-id", "system_id": SERVED,
             "method": "user_confirmed", "confidence": 1.0}]


_KEYS = ("system_id", "peer_system", "from_system", "to_system", "source_system", "target_system")


def _names(item, sid) -> bool:
    return isinstance(item, dict) and any(item.get(k) == sid for k in _KEYS)


def _as_the_cypher_reads(rows: list, sid: str) -> list:
    """What a seed read answers once ``sid`` is marked: no row rooted at it,
    no nested item naming it."""
    return [{k: ([i for i in v if not _names(i, sid)] if isinstance(v, list) else v)
             for k, v in row.items()} for row in rows if not _names(row, sid)]


class _Graph:
    """The four reads. ``mark_after`` is the read after which REMOVING's
    removal starts (``None``: never; ``"start"``: before the first read)."""

    def __init__(self, mark_after=None, marker_read_fails=False):
        self.marked = mark_after == "start"
        self.mark_after = mark_after
        self.marker_read_fails = marker_read_fails
        self.calls: list = []

    def __call__(self, cypher):
        self.calls.append(cypher)
        if cypher == REMOVING_SYSTEMS_READ:
            if self.marker_read_fails:
                raise RuntimeError("graph unavailable")
            return [{"sid": REMOVING}] if self.marked else []
        rows = {orch._ROUTE_SEED_INVENTORY_CYPHER: _inventory,
                orch._ROUTE_SEED_AUTHORITY_CYPHER: _authority_rows}.get(cypher, _rule_rows)()
        if self.marked:
            rows = _as_the_cypher_reads(rows, REMOVING)
        if self.mark_after == cypher:
            self.marked = True
        return copy.deepcopy(rows)


def _split(seed: str) -> tuple:
    """``(payload, tail)``: the seed's JSON object and the text after it."""
    for header in (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER):
        if seed.startswith(header):
            body = seed[len(header):]
            payload, end = json.JSONDecoder().raw_decode(body)
            return payload, body[end:]
    raise AssertionError("route seed has no recognized header")


#: When the removal starts: before the seed's reads (they leave it out), right
#: after the inventory read (the reviewer's reproduction: only the final
#: marker read can drop the inventory row), and after every read (each read
#: still named it; the final marker read drops it from all three).
SCENARIOS = {
    "marked_before_the_reads": dict(mark_after="start"),
    "marked_after_the_inventory_read": dict(mark_after=orch._ROUTE_SEED_INVENTORY_CYPHER),
    "marked_after_every_read": dict(mark_after=orch._ROUTE_SEED_AUTHORITY_CYPHER),
}


@pytest.fixture(params=sorted(SCENARIOS))
def seed(request) -> tuple:
    built = orch.build_route_seed(query=_Graph(**SCENARIOS[request.param]), question=QUESTION)
    assert built is not None
    return _split(built)


def test_the_marker_is_read_once_more_after_the_seeds_own_reads():
    graph = _Graph(mark_after=orch._ROUTE_SEED_INVENTORY_CYPHER)
    orch.build_route_seed(query=graph, question=QUESTION)
    assert graph.calls == [*_SEED_READS, REMOVING_SYSTEMS_READ]


def test_the_marked_system_has_no_seed_entry(seed):
    payload, _tail = seed
    assert {s["id"] for s in payload["systems"]} == {SERVED, PEER}


def test_no_shared_concept_or_rule_under_a_surviving_peer_names_the_marked_system(seed):
    payload, _tail = seed
    for system in payload["systems"]:
        assert REMOVING not in {c["peer_system"] for c in system["shared_concepts"]}
        for kind, rules in system["rules"].items():
            named = {rule.get(key) for rule in rules
                     for key in ("from_system", "to_system", "source_system", "target_system")}
            assert REMOVING not in named, (system["id"], kind, rules)
    served = {s["id"]: s for s in payload["systems"]}[SERVED]
    # Rules between the served systems are untouched.
    assert PEER in {c["peer_system"] for c in served["shared_concepts"]}
    assert "jp-a-b" in {r["id"] for s in payload["systems"] for r in s["rules"].get("join_paths", [])}
    assert "ir-a-b" in {r["id"] for s in payload["systems"]
                        for r in s["rules"].get("identity_rules", [])}


def test_the_marked_systems_authority_rule_is_not_routing_truth(seed):
    payload, _tail = seed
    authority = [r["id"] for s in payload["systems"] for r in s["rules"].get("authority_rules", [])]
    assert authority == [f"ar-{SERVED}-equipment-id"]
    assert REMOVING not in json.dumps(payload.get("concept_authority") or {})


def test_no_identifier_translation_or_owner_points_at_the_marked_system(seed):
    _payload, tail = seed
    assert "B-3301" in tail            # the translation between served systems still renders
    assert REMOVING not in tail
    assert "PLANT-02847" not in tail


def test_nothing_in_the_seed_names_the_marked_system(seed):
    payload, tail = seed
    assert REMOVING not in json.dumps(payload) + tail


def test_with_nothing_marked_every_row_is_kept_as_read():
    rows = _rule_rows()
    assert orch._without_marked_systems(rows, frozenset()) is rows
    graph = _Graph()
    reads = orch._route_seed_reads(graph)
    assert reads == (_inventory(), _rule_rows(), _authority_rows())
    full = orch.build_route_seed(query=_Graph(), question=QUESTION)
    assert REMOVING in full and "PLANT-02847" in full


def test_a_failed_final_marker_read_leaves_the_seed_as_read():
    assert (orch.build_route_seed(query=_Graph(marker_read_fails=True), question=QUESTION)
            == orch.build_route_seed(query=_Graph(), question=QUESTION))


# ---------------------------------------------------------------- the Cypher itself

def _order_by_after(cypher: str, alias: str) -> int:
    # Value mappings are ordered by value first, then grouped per field pair
    # and only then ranked and capped: the filter must precede the first.
    pattern = {"peer": r"ORDER BY coalesce\(c\.id",
               "vm": r"ORDER BY coalesce\(toString\(vm\.from_value\)"}.get(
        alias, rf"ORDER BY CASE WHEN {alias}\.method")
    match = re.search(pattern, cypher)
    assert match, alias
    return match.start()


@pytest.mark.parametrize("alias, props", [
    ("peer", ("peer.id",)),
    ("jp", ("jp.from_system", "jp.to_system")),
    ("vm", ("vm.from_system", "vm.to_system")),
    ("ir", ("ir.source_system", "ir.target_system")),
])
def test_each_rule_collection_drops_a_marked_endpoint_before_it_is_ordered_and_capped(alias, props):
    """Offline check of the shape the Memgraph test runs: each collection's
    endpoint filter sits between its OPTIONAL MATCH and its ORDER BY (and so
    before its slice)."""
    cypher = orch._ROUTE_SEED_CYPHER
    order_by = _order_by_after(cypher, alias)
    opened = cypher.rfind("OPTIONAL MATCH", 0, cypher.rfind(f"{alias}:", 0, order_by) + 1)
    for prop in props:
        at = cypher.find(f"NOT (coalesce({prop}, '') IN removing)", opened)
        assert opened <= at < order_by, (alias, prop)


def test_every_seed_read_leaves_out_a_marked_root_system():
    assert orch._ROUTE_SEED_CYPHER.startswith(orch._ROUTE_SEED_REMOVING_IDS)
    assert "MATCH (s:System)\nWHERE NOT (s.id IN removing)" in orch._ROUTE_SEED_CYPHER
    assert ("MATCH (s:System)\nWHERE coalesce(s.removal_incomplete, false) = false"
            in orch._ROUTE_SEED_INVENTORY_CYPHER)
    assert orch._ROUTE_SEED_AUTHORITY_CYPHER.startswith(orch._ROUTE_SEED_REMOVING_IDS)
    assert "NOT (coalesce(ar.system_id, '') IN removing)" in orch._ROUTE_SEED_AUTHORITY_CYPHER


def test_the_models_fallback_traversal_is_the_filtered_cypher():
    """When no seed was built, the prompt tells the model to run this exact
    traversal itself; it carries the same filter."""
    assert orch._ROUTE_SEED_CYPHER in orch.ORCHESTRATOR_PROMPT
    assert "IN removing" in orch._ROUTE_SEED_CYPHER
