# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The route seed built from a real graph export, on a real openCypher engine.

Loads a graph snapshot file (``agents.graph_snapshot`` format, the file the
steward export writes) into a THROWAWAY local Memgraph and builds the route
seed with the production Cypher. The assertions are derived from the export
itself, so the test names no system, field or value:

* every JoinPath whose IdentityRule is deactivated or retired, or whose own
  status is deactivated, is absent from the seed;
* every ValueMapping entry the seed serves carries value pairs, each pair is
  one of the stored ValueMapping records of that field pair, and no entry is
  ever served without them (no field pair with only a count), for each of
  three ordinary questions;
* for those questions the seed serves every authority rule it serves when
  the graph holds no ValueMapping at all (mappings never cost one);
* the export's records are left as they were (the seed only reads).

Skipped unless BOTH ``ADT_TEST_MEMGRAPH_URI`` names a throwaway Memgraph
(every node in it is deleted) and ``ADT_TEST_GRAPH_EXPORT`` names a snapshot
file. Run (from ``agentic_digital_thread/``)::

    ADT_TEST_MEMGRAPH_URI=bolt://127.0.0.1:57687 ADT_TEST_GRAPH_EXPORT=/path/graph.json \\
      PYTHONPATH=. python -m pytest tests/integration/test_route_seed_live_export_memgraph.py -q -s
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_URI = os.environ.get("ADT_TEST_MEMGRAPH_URI")
_EXPORT = os.environ.get("ADT_TEST_GRAPH_EXPORT")


@pytest.fixture(scope="module")
def loaded():
    if not _URI or not _EXPORT:
        pytest.skip("set ADT_TEST_MEMGRAPH_URI and ADT_TEST_GRAPH_EXPORT to run these")
    neo4j = pytest.importorskip("neo4j")
    from agents import graph_snapshot
    from agents.db import _to_plain
    payload = json.loads(Path(_EXPORT).read_text(encoding="utf-8"))
    driver = neo4j.GraphDatabase.driver(_URI, auth=("", ""))

    def run(cypher, params=None):
        with driver.session() as session:
            return [{k: _to_plain(v) for k, v in record.items()}
                    for record in session.run(cypher, **(params or {}))]

    def write(cypher, params=None):
        run(cypher, params)
        return True

    graph_snapshot.import_snapshot(payload, query=run, write=write)
    yield run, payload
    run("MATCH (n) DETACH DELETE n")
    driver.close()


def _seed(run, question):
    import agents.orchestrator as orch
    seed = orch.build_route_seed(query=run, question=question)
    assert seed
    body = seed.split("ROUTE SEED RESULT:\n", 1)[1]
    payload, _end = json.JSONDecoder().raw_decode(body)
    return seed, payload


def _props(payload, label):
    return [n["props"] for n in payload["nodes"] if label in n["labels"]]


def _switched_off_joinpaths(payload) -> set:
    from agents import joinpath_rule_link as link
    rules = {r.get("id"): r for r in _props(payload, "IdentityRule")}
    off = set()
    for jp in _props(payload, "JoinPath"):
        rule = rules.get(link.linked_rule_id(jp.get("id"), jp.get("rule_id")))
        if (jp.get("status") == "deactivated"
                or (rule and link.is_inactive_status(rule.get("status")))):
            off.add(jp["id"])
    return off


def test_no_switched_off_joinpath_is_served(loaded):
    run, payload = loaded
    off = _switched_off_joinpaths(payload)
    seed, _body = _seed(run, "What happened yesterday?")
    print(f"\nswitched-off JoinPaths in the export: {sorted(off)}")
    for jid in off:
        assert jid not in seed


def _stored_pairs(payload) -> dict:
    stored = defaultdict(set)
    for vm in _props(payload, "ValueMapping"):
        if vm.get("from_value") is not None and vm.get("to_value") is not None:
            key = (vm.get("from_system"), vm.get("from_field"), vm.get("to_system"), vm.get("to_field"))
            stored[key].add((str(vm["from_value"]), str(vm["to_value"])))
    return stored


def _served_value_mappings(body) -> list:
    return [entry for system in body["systems"]
            for entry in system.get("rules", {}).get("value_mappings", [])]


def _key(entry) -> tuple:
    return (entry.get("from_system"), entry.get("from_field"),
            entry.get("to_system"), entry.get("to_field"))


def _questions(payload) -> list:
    """Three ordinary questions: one naming nothing, and two naming a value
    a stored mapping holds (the values of the mapping with the most pairs)."""
    stored = _stored_pairs(payload)
    questions = ["What happened yesterday?"]
    if stored:
        _key, pairs = max(stored.items(), key=lambda item: (len(item[1]), str(item[0])))
        values = sorted({pair[0] for pair in pairs})
        questions.append(f"How many segments did {values[0]} complete yesterday?")
        questions.append(f"What was {values[-1]} bind throughput yesterday?")
    return questions


def test_value_mappings_are_served_value_level(loaded):
    """Every served entry lists stored value pairs, whatever the budget
    pressure and the question: never a bare field-to-field relationship,
    never a field pair with only a count."""
    run, payload = loaded
    stored = _stored_pairs(payload)
    for question in _questions(payload):
        _seed_text, body = _seed(run, question)
        served = _served_value_mappings(body)
        print(f"\n{question!r}: {len(served)} value mapping entries")
        for entry in served:
            pairs = {tuple(p) for p in entry.get("value_pairs") or []}
            print(f"  served {_key(entry)}: {sorted(pairs)} count={entry.get('value_pair_count')}")
            assert pairs, f"served without its value pairs: {_key(entry)}"
            assert pairs <= stored[_key(entry)], _key(entry)


def _authority_ids(body) -> dict:
    return {system["id"]: sorted(rule["id"] for rule in system.get("rules", {}).get(
        "authority_rules", [])) for system in body["systems"]}


def test_value_mappings_never_cost_an_authority_rule(loaded):
    """Authority rules decide routing. For each question the seed serves
    every authority rule it serves when the graph holds no ValueMapping at
    all: under budget pressure the mappings the question names no value of
    go first."""
    import agents.orchestrator as orch
    run, payload = loaded

    def without_value_mappings(cypher, params=None):
        rows = run(cypher, params)
        if cypher == orch._ROUTE_SEED_CYPHER:
            rows = [{**row, "value_mappings": []} for row in rows]
        return rows

    for question in _questions(payload):
        _seed_text, body = _seed(run, question)
        seed = orch.build_route_seed(query=without_value_mappings, question=question)
        reference = json.JSONDecoder().raw_decode(seed.split("ROUTE SEED RESULT:\n", 1)[1])[0]
        served, expected = _authority_ids(body), _authority_ids(reference)
        print(f"\n{question!r}: authority rules "
              f"{sum(map(len, served.values()))} served, {sum(map(len, expected.values()))} "
              f"without value mappings: {served}")
        assert served == expected, question


def test_a_named_value_keeps_its_mapping_pairs(loaded):
    """Ask about a value one stored mapping holds: that mapping is served with
    its pairs, whatever the budget pressure."""
    run, payload = loaded
    stored = _stored_pairs(payload)
    if not stored:
        pytest.skip("the export holds no ValueMapping with values")
    key, pairs = max(stored.items(), key=lambda item: len(item[1]))
    value = sorted(pairs)[0][0]
    _seed_text, body = _seed(run, f"How many records did {value} produce yesterday?")
    served = {_key(e): e for e in _served_value_mappings(body)}
    print(f"question names {value!r}: {key} -> {served.get(key)}")
    assert key in served
    assert served[key].get("value_pairs"), served[key]
    assert {tuple(p) for p in served[key]["value_pairs"]} <= pairs


def test_the_seed_does_not_change_the_records(loaded):
    run, payload = loaded
    before = run("MATCH (n) WHERE n:JoinPath OR n:IdentityRule OR n:ValueMapping "
                 "RETURN n.id AS id, n.status AS status ORDER BY id")
    _seed(run, "What happened yesterday?")
    after = run("MATCH (n) WHERE n:JoinPath OR n:IdentityRule OR n:ValueMapping "
                "RETURN n.id AS id, n.status AS status ORDER BY id")
    assert before == after
