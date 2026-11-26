# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Statements whose size grows with the graph run under a longer server limit,
but only where no HTTP caller waits on them.

Every ExecuteQuery now carries ``queryTimeoutMilliseconds`` (60 s by default)
with a client read timeout just above it. A few statements touch a whole
system's subgraph or a whole graph restore, and nothing in the code bounds
how long they take: a system's removal deletes, the provision's
replace-not-accrete deletes, and the graph snapshot import. They get a
longer server limit and a read timeout matched to it, but only in a
background job: the provision job, a removal run as a background job
(``wait=false``, and the chat's removal) and the snapshot import job. A
synchronous request (Reset All, a ``wait=true`` removal, a provision
confirm) keeps the default: its caller's window is the 120 s web ceiling, so
a longer limit would only let the work run on after the caller saw a 504.

Graph labels in the generic tests are invented; the onboarding tests drive
the app's own reset and provision functions with an invented system id.
"""
from __future__ import annotations

import io
import json

import pytest

import agents.db as db
from agents.job_store import JobStore

READ = "MATCH (w:Widget {code: $code}) RETURN w.code AS code"


class _Client:
    """Records each call's server limit and the read timeout of the client
    that sent it."""

    def __init__(self, sent, read_timeout):
        self.sent = sent
        self.read_timeout = read_timeout

    def execute_query(self, **kwargs):
        self.sent.append({"cypher": kwargs["queryString"],
                          "limit_ms": kwargs.get("queryTimeoutMilliseconds"),
                          "read_timeout": self.read_timeout})
        return {"payload": io.BytesIO(json.dumps({"results": []}).encode())}


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    log: list[dict] = []

    def factory(*_args, **kwargs):
        config = kwargs.get("config")
        return _Client(log, getattr(config, "read_timeout", None))
    monkeypatch.setattr(db.boto3, "client", factory)
    return log


def _long():
    return {"limit_ms": db.GRAPH_LONG_QUERY_TIMEOUT_MS,
            "read_timeout": db.GRAPH_LONG_QUERY_TIMEOUT_MS / 1000 + db.GRAPH_READ_MARGIN_S}


def _default():
    return {"limit_ms": db.GRAPH_QUERY_TIMEOUT_MS, "read_timeout": db.GRAPH_READ_TIMEOUT_S}


def _tiers(log, text):
    return [{"limit_ms": s["limit_ms"], "read_timeout": s["read_timeout"]}
            for s in log if text in s["cypher"]]


# ── The scope itself ─────────────────────────────────────────────────────────

def test_the_default_read_timeout_is_the_server_limit_plus_the_margin():
    assert db.GRAPH_READ_TIMEOUT_S == db.GRAPH_QUERY_TIMEOUT_MS / 1000 + db.GRAPH_READ_MARGIN_S


def test_a_statement_inside_the_scope_gets_the_long_limit_and_a_matching_read_timeout(sent):
    db.neptune_query(READ, {"code": "W-1"})
    with db.graph_statement_limit():
        db.neptune_query(READ, {"code": "W-2"})
        db.neptune_write("MATCH (b:Bin {id: $id}) DETACH DELETE b", {"id": "b1"})
    db.neptune_query(READ, {"code": "W-3"})
    assert [{k: s[k] for k in ("limit_ms", "read_timeout")} for s in sent] == [
        _default(), _long(), _long(), _default()]


def test_the_scope_takes_an_explicit_limit_and_restores_the_outer_one(sent):
    with db.graph_statement_limit(120_000):
        with db.graph_statement_limit():
            db.neptune_query(READ, {"code": "W-1"})
        db.neptune_query(READ, {"code": "W-2"})
    assert [s["limit_ms"] for s in sent] == [db.GRAPH_LONG_QUERY_TIMEOUT_MS, 120_000]
    assert sent[1]["read_timeout"] == 120 + db.GRAPH_READ_MARGIN_S


def test_the_long_limit_is_below_the_graph_maximum_and_above_the_default():
    """Neptune Analytics caps a query at 60 minutes whatever the request asks."""
    assert db.GRAPH_QUERY_TIMEOUT_MS < db.GRAPH_LONG_QUERY_TIMEOUT_MS <= 60 * 60 * 1000


# ── Background only: what a caller waits on keeps the default ───────────────

def test_long_statements_outside_a_background_job_keep_the_default_limit(sent):
    with db.long_graph_statements():
        db.neptune_query(READ, {"code": "W-1"})
    with db.graph_background_job():
        db.neptune_query(READ, {"code": "W-2"})
        with db.long_graph_statements():
            db.neptune_query(READ, {"code": "W-3"})
        db.neptune_query(READ, {"code": "W-4"})
    assert [{k: s[k] for k in ("limit_ms", "read_timeout")} for s in sent] == [
        _default(), _default(), _long(), _default()]


def test_reset_all_wipes_each_label_under_the_default_limit(sent):
    """Reset All is a synchronous request: its caller's window is the web
    ceiling, so a longer limit would only let work run on after a 504."""
    import agents.onboard as onboard
    onboard._global_reset_wipe_graph()
    wipes = _tiers(sent, "DETACH DELETE")
    assert len(wipes) == len(onboard._RESET_NODE_LABELS)
    assert wipes == [_default()] * len(wipes)


def test_a_removal_a_caller_waits_on_deletes_under_the_default_limit(sent):
    import agents.onboard as onboard
    onboard._reset_delete_graph_artifacts("gizmos")
    deletes = _tiers(sent, "DELETE")
    assert len(deletes) == len(onboard._RESET_GRAPH_DELETES) + 1
    assert deletes == [_default()] * len(deletes)


def test_a_removal_in_a_background_job_deletes_under_the_long_limit(sent):
    import agents.onboard as onboard
    with db.graph_background_job():
        onboard._reset_delete_graph_artifacts("gizmos")
    deletes = _tiers(sent, "DELETE")
    assert len(deletes) == len(onboard._RESET_GRAPH_DELETES) + 1
    assert deletes == [_long()] * len(deletes)


@pytest.fixture
def removal(monkeypatch, sent):
    """The removal route with the cascade cut down to its graph deletes (run
    in a worker thread, as the cascade runs them) and no shared state."""
    import asyncio
    import agents.onboard as onboard

    async def steps(system_id, full=False, **_kwargs):
        await asyncio.to_thread(onboard._reset_delete_graph_artifacts, system_id)
        return {"status": "reset", "system_id": system_id}

    async def no_claim(*_args, **_kwargs):
        return None
    monkeypatch.setattr(onboard, "_run_reset_steps", steps)
    monkeypatch.setattr(onboard, "_claim_reset", no_claim)
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard._RESET_DOMAIN, "release_single", lambda _sid: None)
    return onboard


def _run_route(coro):
    """Run a route coroutine, then every task it started (the background job)."""
    import asyncio

    async def main():
        result = await coro
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*others)
        return result
    return asyncio.run(main())


def test_a_wait_true_removal_runs_its_deletes_under_the_default_limit(removal, sent):
    _run_route(removal.onboard_reset_system("gizmos", wait=True))
    deletes = _tiers(sent, "DELETE")
    assert deletes and deletes == [_default()] * len(deletes)


def test_a_wait_false_removal_runs_its_deletes_under_the_long_limit(removal, sent):
    assert _run_route(removal.onboard_reset_system("gizmos", wait=False))["status"] == "started"
    deletes = _tiers(sent, "DELETE")
    assert deletes and deletes == [_long()] * len(deletes)
    assert db.graph_query_timeout_ms() == db.GRAPH_QUERY_TIMEOUT_MS


_PROVISION_DELETES = ("_delete_stale_schema_subgraph",
                      "_delete_stale_onboarding_rule_artifacts",
                      "_delete_stale_pg_derived_records")


def _run_provision_deletes(onboard, system_id):
    for name in _PROVISION_DELETES:
        getattr(onboard, name)(system_id)


@pytest.mark.parametrize("name", _PROVISION_DELETES)
def test_a_provision_replace_delete_a_caller_waits_on_keeps_the_default_limit(sent, name):
    import agents.onboard as onboard
    token = onboard._STRICT_PROVISION_WRITES.set(True)
    try:
        getattr(onboard, name)("gizmos")
    finally:
        onboard._STRICT_PROVISION_WRITES.reset(token)
    deletes = _tiers(sent, "DELETE")
    assert deletes and deletes == [_default()] * len(deletes)


def test_the_provision_job_runs_its_replace_deletes_under_the_long_limit(sent, monkeypatch):
    import contextlib
    import types
    import agents.onboard as onboard

    def steps(_job_id, body):
        _run_provision_deletes(onboard, body.system_id)
        return {"status": "done"}
    monkeypatch.setattr(onboard, "_run_provision_job_steps", steps)
    onboard._run_provision_job("job-1", types.SimpleNamespace(system_id="gizmos"),
                               claim=contextlib.nullcontext())
    deletes = _tiers(sent, "DELETE")
    assert deletes and deletes == [_long()] * len(deletes)
    assert db.graph_query_timeout_ms() == db.GRAPH_QUERY_TIMEOUT_MS


def test_a_provision_confirm_runs_its_replace_deletes_under_the_default_limit(sent, monkeypatch):
    """``/provision/confirm`` runs the whole provision inside the request."""
    import contextlib
    import types
    import agents.models as models
    import agents.onboard as onboard

    def provision(body):
        _run_provision_deletes(onboard, body.system_id)
        return {"tables_mapped": 0}
    monkeypatch.setattr(models, "ProvisionRequest",
                        lambda **kw: types.SimpleNamespace(mappings=[], **kw))
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_claim_provision", lambda _sid: contextlib.nullcontext())
    monkeypatch.setattr(onboard, "_do_provision", provision)
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds", lambda *_a: None)
    assert onboard.onboard_provision_confirm({"system_id": "gizmos"})["status"] == "done"
    deletes = _tiers(sent, "DELETE")
    assert deletes and deletes == [_default()] * len(deletes)


def test_a_graph_snapshot_import_runs_under_the_long_limit():
    """The import runs only as the admin route's background job."""
    import agents.graph_snapshot as gs
    seen: list[int] = []

    def query(_cypher, _params=None):
        seen.append(db.graph_query_timeout_ms())
        return [{"n": 0}]

    def write(_cypher, _params=None):
        seen.append(db.graph_query_timeout_ms())
        return True
    gs.import_snapshot({"format": gs.FORMAT_VERSION, "nodes": [], "edges": []},
                       query=query, write=write)
    assert seen and set(seen) == {db.GRAPH_LONG_QUERY_TIMEOUT_MS}
    assert db.graph_query_timeout_ms() == db.GRAPH_QUERY_TIMEOUT_MS


def test_an_ordinary_provision_write_keeps_the_default_limit(sent):
    import agents.onboard as onboard
    with db.graph_background_job():
        onboard.neptune_write("MERGE (s:SchemaEntry {id: $seid}) SET s.name = $n",
                              {"seid": "gizmos.t", "n": "t"})
    assert [{k: s[k] for k in ("limit_ms", "read_timeout")} for s in sent] == [_default()]
