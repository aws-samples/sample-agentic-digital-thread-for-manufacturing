# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A provision survives one dropped graph connection on the writes whose call
site states that a copy still running alongside the resend leaves the graph
as one run (``db.resubmit_safe_write(after_drop=True)``).

A workshop deploy on 2026-10-29: seven systems were
re-provisioning at once when the connection of the ERP schema rewrite's Field
MERGE closed before Neptune answered (``ConnectionClosedError``). The write
was not declared, so it was not sent again; the strict provision write raised
and the job ended ``provision_failed``, which failed the workshop seed and the
whole stack. The same drop fails an attendee's own onboarding.

The Field MERGE SETs only properties that are part of the Field's own id, so
a copy that lands late writes the values the resend wrote. It is now
declared, with its HAS_FIELD edge, the table's SchemaEntry MERGE and entity
edge, and the ON CREATE Concept MERGEs (each read in
``test_neptune_write_resubmit_opt_in``). A
timeout is still never sent again, and a write that is not declared (the
MAPS_TO edge) still fails the provision on a drop.

Labels and properties are invented except in the pinned provision statements.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError

import agents.db as db
import agents.onboard as onboard

URL = "https://g-abcdefghij.example/queries"

FIELD_MERGE = ("MERGE (f:Field {id: $fid}) SET f.name = $name, f.schema_id = $schema_id, "
               "f.system_id = $sid")
HAS_FIELD = "MATCH (se:SchemaEntry {id: $seid}), (f:Field {id: $fid}) MERGE (se)-[:HAS_FIELD]->(f)"
CONCEPT_MERGE = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $cid, "
    "c.created_by_system = $created_by_system, c.created_at = $created_at, "
    "c.created_by_run = $created_by_run")
FIELD_MAPS_TO = (
    "MATCH (f:Field {id: $fid}), (c:Concept {id: $cid}) MERGE (f)-[r:MAPS_TO]->(c) "
    "SET r.confidence = $conf, r.role = $role, r.version = coalesce(r.version, 1)")
SCHEMA_ENTRY_MERGE = (
    "MERGE (se:SchemaEntry {id: $seid}) SET se.name = $name, se.system_id = $sid "
    "WITH se MERGE (s:System {id: $sid}) ON CREATE SET s.name = $sid, s.status = 'onboarded' "
    "MERGE (s)-[:HAS_SCHEMA]->(se)")
ENTITY_CONCEPT_MERGE = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $name, c.source = 'minted', "
    "c.layer = 'entity', c.description = $desc, c.created_by_system = $created_by_system, "
    "c.created_at = $created_at, c.created_by_run = $created_by_run")
ENTITY_MAPS_TO = ("MATCH (se:SchemaEntry {id: $seid}), (c:Concept {id: $entity}) "
                  "MERGE (se)-[:MAPS_TO]->(c)")


def _bad_gateway() -> ClientError:
    response = {"Error": {"Code": "BadGateway", "Message": "boom"},
                "ResponseMetadata": {"HTTPStatusCode": 502}}
    return type("BadGateway", (ClientError,), {})(response, "ExecuteQuery")


#: The two ends a dropped connection reaches the app as (``graph_resubmit.DROPPED``).
DROPS = [ConnectionClosedError(endpoint_url=URL), _bad_gateway()]
DROP_IDS = ["connection-closed", "502"]


class _Client:
    """A neptune-graph client: raises the queued errors, in order, on the
    statements equal to ``only``, then answers no rows."""

    def __init__(self, errors, only):
        self.errors, self.only = list(errors), only
        self.calls: list[str] = []

    def execute_query(self, **kwargs):
        self.calls.append(kwargs["queryString"])
        if self.errors and kwargs["queryString"] == self.only:
            raise self.errors.pop(0)
        return {"payload": io.BytesIO(json.dumps({"results": []}).encode())}


@pytest.fixture
def graph(monkeypatch):
    import agents.graph_resubmit as gr
    monkeypatch.setattr(gr, "_sleep", lambda _s: None)
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")

    def use(errors, only):
        client = _Client(errors, only)
        monkeypatch.setattr(db.boto3, "client", lambda *a, **k: client)
        return client
    return use


@pytest.fixture
def strict_provision():
    """The provision transaction's contract: a write that is not confirmed raises."""
    token = onboard._STRICT_PROVISION_WRITES.set(True)
    yield
    onboard._STRICT_PROVISION_WRITES.reset(token)


def _b2_state():
    """The fields of the provision state the b2 step reads, for one ERP table."""
    return SimpleNamespace(
        system_id="erp", default_table="", athena_physical_names={},
        committable={"bin-code"}, user_corrections={}, deferred_llm_edges={},
        unmapped_columns=[], nodes=0)


ROWS = [{"table": "bins", "field": f"bins.{col}", "concept": "bin-code",
         "mappings": [{"concept": "bin-code", "role": "identifier", "confidence": 0.9}]}
        for col in ("code", "alt_code")]


def _b2(monkeypatch):
    """The provision's b2 step (Field nodes with their concept edges) over two columns."""
    monkeypatch.setattr(onboard.self_learning, "accepted_user_concept_mappings", lambda _s: {})
    ctx = _b2_state()
    ctx.mappings = ROWS
    onboard._dp_field_mappings(ctx)
    return ctx


# ── The failing statement of 2026-10-29 ──────────────────────────────────────

@pytest.mark.parametrize("error", DROPS, ids=DROP_IDS)
def test_b2_survives_a_dropped_connection_on_its_field_merge(
        graph, strict_provision, monkeypatch, error):
    """REPRODUCTION: on 285e994d the first Field MERGE's dropped connection
    raised "provision graph write failed" and the job ended provision_failed.
    Now the MERGE is sent once more and the step writes every column."""
    client = graph([error], only=FIELD_MERGE)
    ctx = _b2(monkeypatch)
    assert client.calls[:3] == [FIELD_MERGE, FIELD_MERGE, HAS_FIELD]
    assert client.calls.count(FIELD_MERGE) == 3          # two columns, one resend
    assert ctx.nodes == 2 and ctx.unmapped_columns == []


@pytest.mark.parametrize("error", DROPS, ids=DROP_IDS)
def test_b2_survives_a_dropped_connection_on_its_has_field_edge(
        graph, strict_provision, monkeypatch, error):
    client = graph([error], only=HAS_FIELD)
    ctx = _b2(monkeypatch)
    assert client.calls[:3] == [FIELD_MERGE, HAS_FIELD, HAS_FIELD]
    assert ctx.nodes == 2


@pytest.mark.parametrize("error", DROPS, ids=DROP_IDS)
def test_b2_survives_a_dropped_connection_on_its_concept_merge(
        graph, strict_provision, monkeypatch, error):
    client = graph([error], only=CONCEPT_MERGE)
    ctx = _b2(monkeypatch)
    assert client.calls[2:5] == [CONCEPT_MERGE, CONCEPT_MERGE, FIELD_MAPS_TO]
    assert ctx.nodes == 2


def test_the_field_merge_survives_one_drop_per_attempt_within_the_bound(
        graph, strict_provision, monkeypatch):
    import agents.graph_resubmit as gr
    client = graph([ConnectionClosedError(endpoint_url=URL)] * (gr.MAX_ATTEMPTS - 1),
                   only=FIELD_MERGE)
    _b2(monkeypatch)
    assert client.calls[:gr.MAX_ATTEMPTS] == [FIELD_MERGE] * gr.MAX_ATTEMPTS


def test_drops_past_the_bound_still_fail_the_provision(graph, strict_provision, monkeypatch):
    import agents.graph_resubmit as gr
    client = graph([ConnectionClosedError(endpoint_url=URL)] * gr.MAX_ATTEMPTS, only=FIELD_MERGE)
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        _b2(monkeypatch)
    assert client.calls == [FIELD_MERGE] * gr.MAX_ATTEMPTS


def test_a_timed_out_field_merge_is_still_not_sent_again(graph, strict_provision, monkeypatch):
    """A timeout leaves the first copy running: never sent again, declared or not."""
    client = graph([ReadTimeoutError(endpoint_url=URL)], only=FIELD_MERGE)
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        _b2(monkeypatch)
    assert client.calls == [FIELD_MERGE]


# ── An undeclared write is still not sent again after a drop ────────────────

@pytest.mark.parametrize("error", DROPS, ids=DROP_IDS)
def test_an_undeclared_maps_to_edge_still_fails_the_provision_on_a_drop(
        graph, strict_provision, monkeypatch, error):
    """The MAPS_TO edge SETs a confidence and a role that a later write of
    the same run can change (the user-correction repoint), and the
    retired-link sweep deletes such edges, so a late copy could undo either:
    it stays out of ``after_drop`` and a drop on it still fails at once."""
    client = graph([error], only=FIELD_MAPS_TO)
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        _b2(monkeypatch)
    assert client.calls.count(FIELD_MAPS_TO) == 1


@pytest.mark.parametrize("error", DROPS, ids=DROP_IDS)
def test_a_plain_write_outside_a_declared_site_is_not_sent_again_after_a_drop(graph, error):
    client = graph([error], only=FIELD_MERGE)
    assert onboard.neptune_write(FIELD_MERGE, {"fid": "erp.bins.code"}) is False
    assert client.calls == [FIELD_MERGE]


@pytest.mark.parametrize("error", DROPS, ids=DROP_IDS)
def test_the_resubmit_safe_helper_alone_still_does_not_cover_a_drop(graph, error):
    client = graph([error], only=FIELD_MERGE)
    assert onboard.neptune_write_resubmit_safe(FIELD_MERGE, {"fid": "erp.bins.code"}) is False
    assert client.calls == [FIELD_MERGE]


# ── The table writes of b0 + b ──────────────────────────────────────────────

def _schema_entries(monkeypatch):
    """The b step's per-table writes (b0's mark and deletes stubbed out),
    for one table whose entity Concept is minted."""
    monkeypatch.setattr(onboard, "_begin_schema_rewrite", lambda _ctx: None)
    monkeypatch.setattr(onboard, "_delete_stale_onboarding_rule_artifacts", lambda _s: None)
    monkeypatch.setattr(onboard, "_classify_table_entity", lambda *_a, **_k: {
        "entity": "bin", "mint": {"name": "Bin", "description": "A bin."}})
    ctx = SimpleNamespace(system_id="erp", schema="erp", connection_type="REST API",
                          tables_seen=["bins"], table_columns={}, table_mappings={},
                          entity_overrides={}, resolved_entities={}, nodes=0)
    onboard._dp_schema_entries(ctx)
    return ctx


@pytest.mark.parametrize("statement", [SCHEMA_ENTRY_MERGE, ENTITY_CONCEPT_MERGE, ENTITY_MAPS_TO],
                         ids=["schema-entry", "entity-concept", "entity-edge"])
@pytest.mark.parametrize("error", DROPS, ids=DROP_IDS)
def test_the_table_writes_survive_a_dropped_connection(
        graph, strict_provision, monkeypatch, error, statement):
    client = graph([error], only=statement)
    ctx = _schema_entries(monkeypatch)
    assert client.calls.count(statement) == 2
    assert client.calls[-1] == ENTITY_MAPS_TO and ctx.nodes == 3

