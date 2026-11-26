# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The writes the provision sends again after an ambiguous failure, run on a
real openCypher engine (Memgraph): a first send that took effect and then
answered a 500, followed by the resend, leaves the graph exactly as one send
does.

Each case seeds the same graph twice. Once the provision function runs with
every graph call answered normally; once the targeted statement's first send
runs, commits, and the client then raises ``InternalServerException`` (the
case the opt-in exists for: the service applied the query but the answer was
an error), so ``graph_resubmit`` sends it again. The two graphs must be equal,
node for node and edge for edge, and another system's records must be
untouched. Covered: the c3 ValueMapping write, the four system-owned cleanup
deletes, and the five schema-rewrite marks.

Skipped unless a THROWAWAY Memgraph is named (every node in it is deleted):
``ADT_TEST_MEMGRAPH_URI`` with the Bolt driver installed (``pip install
.[local]``), or ``ADT_TEST_MEMGRAPH_CONTAINER``, a running Memgraph container
whose ``mgconsole`` is used through ``docker exec`` (parameters are written
into the statement as literals). For example::

    docker run -d --rm --name adt-mg-replay memgraph/memgraph:3.12.0 --telemetry-enabled=false
    ADT_TEST_MEMGRAPH_CONTAINER=adt-mg-replay PYTHONPATH=. \\
        python -m pytest tests/integration/test_resubmit_replay_memgraph.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess  # nosec B404 - docker exec of a named local test container
from collections import Counter
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import agents.db as db
import agents.onboard as onboard

pytestmark = pytest.mark.integration

_URI = os.environ.get("ADT_TEST_MEMGRAPH_URI")
_CONTAINER = os.environ.get("ADT_TEST_MEMGRAPH_CONTAINER")
_PARAM_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|\$([A-Za-z_][A-Za-z0-9_]*)")


def _literal(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"`{k}`: {_literal(v)}" for k, v in value.items()) + "}"
    raise TypeError(type(value))


def _inline(cypher: str, params: dict) -> str:
    return _PARAM_RE.sub(lambda m: m.group(0) if m.group(1) is None
                         else _literal(params[m.group(1)]), cypher)


def _mgconsole_runner(container: str):
    def run(cypher, params=None):
        out = subprocess.run(  # nosec B603 B607 - fixed argv, a named local container
            ["docker", "exec", "-i", container, "mgconsole", "--output-format=csv"],
            input=_inline(cypher, params or {}) + ";\n", capture_output=True, text=True,
            timeout=60)
        if out.returncode or "exception" in out.stdout.lower() or "failed query" in out.stdout.lower():
            raise RuntimeError(f"mgconsole: {out.stdout.strip()} {out.stderr.strip()}")
        rows = list(csv.reader(io.StringIO(out.stdout)))
        return [dict(zip(rows[0], row)) for row in rows[1:]] if rows else []
    return run


def _bolt_runner(uri: str):
    neo4j = pytest.importorskip("neo4j")
    driver = neo4j.GraphDatabase.driver(uri, auth=("", ""))

    def run(cypher, params=None):
        with driver.session() as session:
            return [{k: (json.dumps(v, sort_keys=True, default=str)
                         if isinstance(v, (dict, list)) else v) for k, v in r.items()}
                    for r in session.run(cypher, **(params or {}))]
    return run


@pytest.fixture
def engine(monkeypatch):
    if _URI:
        run = _bolt_runner(_URI)
    elif _CONTAINER:
        run = _mgconsole_runner(_CONTAINER)
    else:
        pytest.skip("set ADT_TEST_MEMGRAPH_URI or ADT_TEST_MEMGRAPH_CONTAINER to a throwaway Memgraph")
    import agents.graph_resubmit as gr
    monkeypatch.setattr(gr, "_sleep", lambda _s: None)
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    monkeypatch.setattr(onboard.time, "time", lambda: 1_700_000_000.0)
    state = {"fault": None, "fired": 0, "sent": []}

    class Client:
        def execute_query(self, **kwargs):
            query, params = kwargs["queryString"], kwargs.get("parameters") or {}
            rows = run(query, params)
            state["sent"].append(query)
            if state["fault"] and state["fault"] in query and not state["fired"]:
                state["fired"] += 1   # it ran and committed; the answer is an error
                raise type("InternalServerException", (ClientError,), {})(
                    {"Error": {"Code": "InternalServerException", "Message": "boom"},
                     "ResponseMetadata": {"HTTPStatusCode": 500}}, "ExecuteQuery")
            return {"payload": io.BytesIO(json.dumps({"results": rows}).encode())}

    monkeypatch.setattr(db.boto3, "client", lambda *a, **k: Client())
    return SimpleNamespace(run=run, state=state)


def _dump(run) -> Counter:
    nodes = run("MATCH (n) RETURN labels(n) AS labels, properties(n) AS props")
    edges = run("MATCH (a)-[r]->(b) RETURN type(r) AS type, properties(r) AS props, "
                "properties(a) AS a, properties(b) AS b")
    return Counter([("node", r["labels"], r["props"]) for r in nodes]
                   + [("edge", r["type"], r["props"], r["a"], r["b"]) for r in edges])


def _replay(engine, seed, action, fault):
    """``(graph after one clean run, graph after a run whose ``fault``
    statement committed then answered a 500, resend count)``."""
    strict = onboard._STRICT_PROVISION_WRITES.set(True)
    try:
        graphs = []
        for inject in (None, fault):
            engine.run("MATCH (n) DETACH DELETE n")
            for statement in seed:
                engine.run(statement)
            engine.state.update(fault=inject, fired=0, sent=[])
            action()
            graphs.append(_dump(engine.run))
        sent = [q for q in engine.state["sent"] if fault in q]
        return graphs[0], graphs[1], engine.state["fired"], len(sent)
    finally:
        onboard._STRICT_PROVISION_WRITES.reset(strict)


def _assert_replay_is_one_run(engine, seed, action, fault):
    clean, replayed, fired, sends = _replay(engine, seed, action, fault)
    assert fired == 1 and sends == 2, (fired, sends)
    assert replayed == clean
    return clean


SYSTEMS = ["CREATE (:System {id: 'erp', name: 'erp'})",
           "CREATE (:System {id: 'cmms', name: 'cmms'})"]


def test_a_value_mapping_sent_twice_is_one_mapping(engine):
    vm = {"from_system": "erp", "from_field": "orders.line", "from_value": "L7",
          "to_system": "cmms", "to_field": "lines.code", "to_value": "C-L7",
          "concept": "production-line", "confidence": 0.9, "method": "semantic_similarity"}
    ctx = SimpleNamespace(system_id="erp", value_mappings_written=0, nodes=0)
    graph = _assert_replay_is_one_run(
        engine, SYSTEMS,
        lambda: onboard._dp_write_cross_vm(ctx, dict(vm), 0, {"erp", "cmms"}),
        "MERGE (vm:ValueMapping")
    assert sum(n for k, n in graph.items() if k[0] == "node" and "ValueMapping" in k[1]) == 1
    assert sum(n for k, n in graph.items() if k[0] == "edge") == 2


OWN_AND_OTHER_SCHEMA = [
    "CREATE (s:System {id: 'sys-a'}), (o:System {id: 'sys-b'}), (c:Concept {id: 'c1'})",
    "MATCH (s:System {id: 'sys-a'}), (c:Concept {id: 'c1'}) "
    "CREATE (s)-[:HAS_SCHEMA]->(se:SchemaEntry {id: 'sys-a.t1'})-[:HAS_FIELD]->"
    "(f:Field {id: 'sys-a.t1.k', system_id: 'sys-a'})-[:MAPS_TO]->(c), "
    "(se)-[:HAS_FIELD]->(:Field {id: 'sys-a.t1.v', system_id: 'sys-a'})",
    "MATCH (o:System {id: 'sys-b'}), (c:Concept {id: 'c1'}) "
    "CREATE (o)-[:HAS_SCHEMA]->(:SchemaEntry {id: 'sys-b.t1'})-[:HAS_FIELD]->"
    "(:Field {id: 'sys-b.t1.k', system_id: 'sys-b'})-[:MAPS_TO]->(c)",
]


def test_the_schema_subgraph_delete_sent_twice_is_one_delete(engine):
    graph = _assert_replay_is_one_run(
        engine, OWN_AND_OTHER_SCHEMA,
        lambda: onboard._delete_stale_schema_subgraph("sys-a"), "DETACH DELETE se, f")
    text = repr(graph)
    assert "sys-a.t1" not in text and "sys-b.t1.k" in text


def test_the_auto_internal_joinpath_delete_sent_twice_is_one_delete(engine):
    seed = [f"CREATE (:JoinPath {{id: '{jid}', from_system: '{f}', to_system: '{t}', method: '{m}'}})"
            for jid, f, t, m in [
                ("jp-internal-sys-a-x", "sys-a", "sys-a", "foreign_key_match"),
                ("jp-vo-sys-a-y", "sys-a", "sys-a", "value_overlap"),
                ("jp-samerow-sys-a-z", "sys-a", "sys-a", "same_row_identifier_set"),
                ("jp-internal-sys-a-kept", "sys-a", "sys-a", "user_confirmed"),
                ("jp-vo-sys-a-cross", "sys-a", "sys-b", "value_overlap"),
                ("jp-internal-sys-b-x", "sys-b", "sys-b", "foreign_key_match")]]
    graph = _assert_replay_is_one_run(
        engine, seed, lambda: onboard._delete_stale_auto_internal_joinpaths("sys-a"),
        "DETACH DELETE jp")
    text = repr(graph)
    assert all(k in text for k in ("jp-internal-sys-a-kept", "jp-vo-sys-a-cross",
                                   "jp-internal-sys-b-x"))
    assert not any(k in text for k in ("sys-a-x'", "jp-vo-sys-a-y", "jp-samerow-sys-a-z"))


def test_the_distinct_identifier_record_delete_sent_twice_is_one_delete(engine):
    seed = ["CREATE (a:Field {id: 'sys-a.t1.k', system_id: 'sys-a'})"
            "-[:DIFFERENT_IDENTIFIER_FROM {concept: 'c1'}]->"
            "(b:Field {id: 'sys-a.t2.k', system_id: 'sys-a'}), "
            "(x:Field {id: 'sys-b.t1.k', system_id: 'sys-b'})"
            "-[:DIFFERENT_IDENTIFIER_FROM {concept: 'c1'}]->"
            "(y:Field {id: 'sys-b.t2.k', system_id: 'sys-b'})"]
    graph = _assert_replay_is_one_run(
        engine, seed, lambda: onboard._delete_stale_distinct_identifier_records("sys-a"),
        "DIFFERENT_IDENTIFIER_FROM]->() DELETE r")
    edges = [k for k in graph if k[0] == "edge"]
    assert len(edges) == 1 and "sys-b.t1.k" in repr(edges[0])


def test_the_plant_link_delete_sent_twice_is_one_delete(engine):
    seed = ["CREATE (s:System {id: 'sys-a'}), (o:System {id: 'sys-b'}), "
            "(p1:Plant {id: 'p1'}), (p2:Plant {id: 'p2'}), "
            "(s)-[:LOCATED_AT]->(p1), (s)-[:LOCATED_AT]->(p2), (o)-[:LOCATED_AT]->(p2)"]
    graph = _assert_replay_is_one_run(
        engine, seed, lambda: onboard._dp_link_plant("sys-a", "P1"), "DELETE r")
    edges = sorted(repr(k) for k in graph if k[0] == "edge")
    assert len(edges) == 2 and any("sys-b" in e and "p2" in e for e in edges)
    assert any("sys-a" in e and "p1" in e for e in edges)


REWRITING = ["CREATE (:System {id: 'sys-a', schema_rewrite_failed: 1000})",
             "CREATE (:System {id: 'sys-b', schema_rewrite_started: 1000})"]


def test_the_start_mark_sent_twice_is_one_mark(engine):
    tokens = []
    _assert_replay_is_one_run(
        engine, REWRITING, lambda: tokens.append(onboard._mark_schema_rewrite("sys-a")),
        "SET s.schema_rewrite_started = $started")
    assert tokens == [1_700_000_000_000] * 2


@pytest.mark.parametrize("settled", [True, False], ids=["settled", "failed"])
def test_the_end_of_the_rewrite_sent_twice_is_one_end(engine, settled):
    seed = ["CREATE (:System {id: 'sys-a', schema_rewrite_started: 1700000000000, "
            "schema_rewrite_failed: 1000})"] + REWRITING[1:]
    _assert_replay_is_one_run(
        engine, seed,
        lambda: onboard._finish_schema_rewrite("sys-a", 1_700_000_000_000, settled=settled),
        "s.schema_rewrite_started = $token")
    row, = engine.run("MATCH (s:System {id: 'sys-a'}) RETURN s.schema_rewrite_started AS started, "
                      "s.schema_rewrite_failed AS failed")
    def absent(value):  # null as each runner answers it (Bolt: None; mgconsole: "Null")
        return str(value).lower() in ("", "none", "null")
    assert absent(row["started"])
    assert absent(row["failed"]) if settled else str(row["failed"]) == "1700000000000"


@pytest.mark.parametrize("which", ["mark", "failed marker"])
def test_a_stale_marker_clear_sent_twice_is_one_clear(engine, which):
    seed = ["CREATE (:System {id: 'sys-a', schema_rewrite_started: 1000, "
            "schema_rewrite_failed: 1000})"]
    if which == "mark":
        action, fault = (lambda: onboard._fresh_rewrite_mark("sys-a", 1000),
                         "WHERE s.schema_rewrite_started = $started")
    else:
        action, fault = (lambda: onboard._fresh_marker(
            "sys-a", 1000, onboard._SCHEMA_REWRITE_CLEAR_STALE_FAILED_CYPHER, "failed",
            "failed schema-rewrite marker"), "WHERE s.schema_rewrite_failed = $failed")
    _assert_replay_is_one_run(engine, seed, action, fault)


def test_the_comparison_sees_a_create_sent_twice(engine):
    """The check can fail: the OnboardingLog CREATE (never opted in), forced
    through the resend here, leaves a second node where one run leaves one."""
    params = {"oid": "ol-sys-a-t", "sid": "sys-a", "ts": "t", "rc": 0, "tm": 1}
    clean, replayed, fired, sends = _replay(
        engine, [],
        lambda: onboard.neptune_write_resubmit_safe(
            "CREATE (ol:OnboardingLog {id: $oid, system_id: $sid, timestamp: $ts, "
            "rules_created: $rc, tables_mapped: $tm})", params),
        "CREATE (ol:OnboardingLog")
    assert fired == 1 and sends == 2
    assert replayed != clean
    assert sum(replayed.values()) == 2 * sum(clean.values()) == 2
