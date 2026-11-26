# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Mixed versions on a real openCypher engine (Memgraph): a schema rewrite
written by this version is seen by the previous version's reader statement,
and one written with the previous version's statements is seen by this
version's reader (``tests/unit/test_schema_rewrite_mixed_versions.py`` has
the reasons and the same directions on a fake).

The previous version's statements are b4182b45's, copied verbatim into that
unit file. Skipped unless a THROWAWAY Memgraph is named, as in
``test_resubmit_replay_memgraph.py`` (whose ``engine`` fixture this uses;
every node in it is deleted). The engine fixture fixes the clock at
1_700_000_000 s, so every mark is fresh.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.onboard as onboard
from agents.job_store import JobStore
from tests.integration.test_resubmit_replay_memgraph import engine  # noqa: F401 - fixture
from tests.unit.test_schema_rewrite_mixed_versions import (
    BASE_FAILED, BASE_MARK, BASE_SETTLED, BASE_SNAPSHOT)

pytestmark = pytest.mark.integration

SEED = ["MATCH (n) DETACH DELETE n",
        "CREATE (s:System {id: 'hub-1'})-[:HAS_SCHEMA]->(:SchemaEntry {id: 'hub-1.t', name: 't'})"
        "-[:HAS_FIELD]->(:Field {id: 'hub-1.t.k', name: 'k'})-[:MAPS_TO]->(:Concept {id: 'c1'})"]
PARAMS = {"sid": "hub-1", "tbl": "t", "fld": "k"}


def _absent(value) -> bool:  # null as each runner answers it (Bolt: None; mgconsole: "Null")
    return str(value).lower() in ("", "none", "null")


@pytest.fixture
def seeded(engine, monkeypatch):  # noqa: F811 - the imported fixture
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", JobStore("schema-rewrite", table=None))
    monkeypatch.setattr(onboard, "_schema_rewrite_sleep", lambda _s: None)
    for statement in SEED:
        engine.run(statement)
    return engine


def test_a_new_writers_rewrite_is_seen_by_the_base_reader_statement(seeded):
    token = onboard._mark_schema_rewrite("hub-1")
    (row,) = seeded.run(BASE_SNAPSHOT, PARAMS)
    assert str(row["started"]) == str(token)
    onboard._finish_schema_rewrite("hub-1", token, settled=False)
    (row,) = seeded.run(BASE_SNAPSHOT, PARAMS)
    assert _absent(row["started"]) and not _absent(row["failed"])
    token = onboard._mark_schema_rewrite("hub-1")
    onboard._finish_schema_rewrite("hub-1", token, settled=True)
    (row,) = seeded.run(BASE_SNAPSHOT, PARAMS)
    assert _absent(row["started"]) and _absent(row["failed"])


def test_a_base_writers_rewrite_is_seen_by_the_new_reader(seeded):
    started = 1_700_000_000_000
    seeded.run(BASE_MARK, {"sid": "hub-1", "started": started})
    ctx = SimpleNamespace(schema_wait_budget_s=0.0)
    concepts, why = onboard._settled_target_concepts(ctx, "hub-1", "t", "k")
    assert concepts is None and "being rewritten" in why
    seeded.run(BASE_FAILED, {"sid": "hub-1", "token": started, "failed": started})
    concepts, why = onboard._settled_target_concepts(ctx, "hub-1", "t", "k")
    assert concepts is None and "left unsettled" in why
    seeded.run(BASE_SETTLED, {"sid": "hub-1", "token": started})
    concepts, why = onboard._settled_target_concepts(ctx, "hub-1", "t", "k")
    assert why is None
