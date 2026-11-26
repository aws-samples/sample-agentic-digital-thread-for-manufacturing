# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A lost lease stops a reset and a provision at every step boundary, the
graph and this process's own state refuse writes after it, and nothing
absorbs the loss.

A reset whose lease was lost part-way still retired proposals, scheduled
secrets, wrote the catalog, deleted onboarding sessions and reset the agent
registry; the concept reseed absorbed the loss as an ordinary seed failure,
so the cascade went on and answered ``reset``. A provision's best-effort
handlers absorbed the loss too, and the compare-and-set graph writer the row
exclusion uses was not guarded.

Now no step of either reset cascade, and no step after a provision, starts
once the lease is lost; every graph write primitive, catalog change and
agent-registry change checks it; and ``reset_lease.LeaseLostError`` is a
``BaseException``, so an ``except Exception`` boundary cannot absorb it. The
AWS writes inside a step are stopped one by one by the botocore hook
(test_lease_stops_every_aws_write.py).

Each step is proven with a test that loses the lease just before it: the
step does not run, nothing after it runs, and the job says why it stopped.

Offline: the shared table, the clock and every step are fakes or recorders.
"""
from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

import agents.db as db
import agents.onboard as onboard
import agents.orchestrator as orchestrator
from agents import reset_lease
from agents.catalog import SystemCatalog
from tests.unit.test_one_reset_lease import (  # noqa: F401 - fixtures
    SID, SID2, Replica, _one_process, clock, table)

LOSE = onboard._RESET_LEASE_SECONDS


@pytest.fixture
def sent(monkeypatch):
    """The graph statements that reached the backend."""
    out: list = []
    monkeypatch.setattr(db, "_graph_backend", lambda: "local")
    monkeypatch.setattr(db, "_memgraph_run",
                        lambda cypher, parameters=None: out.append(cypher) or [])
    return out


@pytest.fixture
def lost(table, clock):
    """A write guard whose lease is lost: ``with lost(): ...``."""
    from contextlib import contextmanager

    @contextmanager
    def _lost():
        store = onboard._RESET_LEASES
        lease = reset_lease.new(SID, LOSE)
        assert store.lease_acquire(reset_lease.provision_key(SID), lease,
                                   now=clock()) is None
        renewal = reset_lease.Renewal(store, reset_lease.provision_key(SID), lease, LOSE)
        clock.advance(LOSE)
        with db.write_guard(renewal.check):
            yield
    return _lost


# ── nothing absorbs a lost lease ───────────────────────────────────────────

def test_a_lost_lease_is_not_an_exception_a_best_effort_handler_can_absorb():
    assert issubclass(reset_lease.LeaseLostError, BaseException)
    assert not issubclass(reset_lease.LeaseLostError, Exception)


def test_the_concept_reseed_does_not_absorb_a_lost_lease(clock, sent, monkeypatch):
    """Lost after the last label delete, during the concept reseed."""
    real = db._memgraph_run
    deletes = len(onboard._RESET_NODE_LABELS)

    def _run(cypher, parameters=None):
        out = real(cypher, parameters)
        if len(sent) == deletes:
            clock.advance(LOSE)
        return out
    monkeypatch.setattr(db, "_memgraph_run", _run)
    assert onboard._RESET_DOMAIN.claim_all() is None
    try:
        with onboard._reset_lease_guard(reset_lease.ALL):
            with pytest.raises(reset_lease.LeaseLostError):
                onboard._global_reset_wipe_graph()
    finally:
        onboard._RESET_DOMAIN.release_all()
    assert len(sent) == deletes, "a concept was seeded after the lease was lost"


def test_a_provision_best_effort_post_pass_does_not_absorb_a_lost_lease(lost, monkeypatch):
    import agents.metamodel as metamodel
    ran: list = []
    monkeypatch.setattr(metamodel, "write_metamodel", lambda: db.check_write_guard())
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings", lambda: ran.append("vm"))
    with lost(), pytest.raises(reset_lease.LeaseLostError):
        onboard._provision_knowledge_post_pass(MagicMock(system_id=SID), {})
    assert ran == []


# ── the reset cascades stop before every step ──────────────────────────────

class Steps:
    """Every step of a cascade replaced by a recorder (in order); the step
    named ``lose_after`` loses the lease when it runs."""

    def __init__(self, monkeypatch, clock, names: list, results: dict):
        self.ran: list = []
        self.lose_after = None
        self.clock = clock
        for name in names:
            owner, attr = self._owner(name)
            monkeypatch.setattr(owner, attr, self._fake(name, results.get(name)))

    @staticmethod
    def _owner(name: str):
        if name.startswith("catalog."):
            return onboard.catalog, name.split(".", 1)[1]
        if name.startswith("_agent_manager."):
            return onboard._agent_manager, name.split(".", 1)[1]
        return onboard, name

    def _fake(self, name, result):
        def _step(*_a, **_kw):
            self.ran.append(name)
            if name == self.lose_after:
                self.clock.advance(LOSE)
            return result() if callable(result) else result
        return _step


def _clean():
    return {"runtimes_awaited": [], "targets_awaited": [], "wait_timed_out": False}


#: Every step of a full single-system removal that changes something, in order.
SINGLE = ["_mark_removal_incomplete", "catalog.mark_removing", "_reset_cleanup_agentcore_sync",
          "remove_system_agent", "_retire_or_incomplete", "_delete_own_secret_or_incomplete",
          "_keep_registration_connections", "_reset_delete_graph_artifacts_invalidating",
          "_delete_onboarding_session", "_full_removal_catalog"]
SINGLE_RESULTS = {"_reset_cleanup_agentcore_sync": _clean, "_retire_or_incomplete": 0,
                  "_delete_own_secret_or_incomplete": "", "_keep_registration_connections": 0,
                  "_delete_onboarding_session": False, "_full_removal_catalog": (True, False)}

#: Every step of a full Reset All that changes something, in order.
ALL = ["neptune_write", "_global_reset_cleanup_agentcore_sync", "_retire_many_or_incomplete",
       "_delete_user_registration_secrets", "_keep_registration_connections",
       "_global_reset_wipe_graph", "_delete_onboarding_sessions", "_agent_manager.stop_all",
       "reset_agents", "catalog.reset_all", "catalog.clear_all"]
ALL_RESULTS = {"neptune_write": True, "_global_reset_cleanup_agentcore_sync": _clean,
               "_retire_many_or_incomplete": 0, "_delete_user_registration_secrets": 0,
               "_keep_registration_connections": 0, "_global_reset_wipe_graph": 0,
               "_delete_onboarding_sessions": 0, "catalog.clear_all": 0}


@pytest.fixture
def cascade_world(monkeypatch):
    monkeypatch.setattr(onboard, "_agent_manager", MagicMock())
    monkeypatch.setattr(onboard, "catalog", SystemCatalog(
        [{"id": SID, "name": "Line MES", "status": "onboarded",
          "connection_type": "PostgreSQL", "schema": "mes"},
         {"id": SID2, "name": "Plant CMMS", "status": "onboarded",
          "connection_type": "PostgreSQL", "schema": "cmms"}]))


@pytest.mark.parametrize("index", range(len(SINGLE)), ids=SINGLE)
def test_a_removal_whose_lease_is_lost_just_before_a_step_does_not_run_it(
        clock, cascade_world, monkeypatch, index):
    steps = Steps(monkeypatch, clock, SINGLE, SINGLE_RESULTS)
    onboard._reset_publish_claim(SID, True)
    if index == 0:
        clock.advance(LOSE)
    else:
        steps.lose_after = SINGLE[index - 1]
    asyncio.run(onboard._run_reset_job(SID, full=True))
    record = onboard._RESET_JOBS.get(SID)
    assert steps.ran == SINGLE[:index], f"ran after the lease was lost: {steps.ran}"
    assert record["state"] == "error" and record["reason"] == "reset_lease_lost", record
    status = onboard.catalog.get(SID)["status"]
    assert status == ("onboarded" if index == 0 else "removal_failed"), status


@pytest.mark.parametrize("index", range(len(ALL)), ids=ALL)
def test_a_reset_all_whose_lease_is_lost_just_before_a_step_does_not_run_it(
        clock, cascade_world, monkeypatch, index):
    steps = Steps(monkeypatch, clock, ALL, ALL_RESULTS)
    assert onboard._RESET_DOMAIN.claim_all(True) is None
    if index == 0:
        clock.advance(LOSE)
    else:
        steps.lose_after = ALL[index - 1]
    asyncio.run(onboard._run_reset_all_job("job-lost", True))
    record = onboard._RESET_ALL_JOBS.get("job-lost")
    assert steps.ran == ALL[:index], f"ran after the lease was lost: {steps.ran}"
    assert record["state"] == "error" and record["reason"] == "reset_lease_lost", record
    assert not onboard._RESET_DOMAIN.reset_all_running()
    # After the marker, this process's catalog records that the reset did not
    # finish (each card offers the retry); before it nothing was touched.
    statuses = {e["id"]: e["status"] for e in onboard.catalog.get_all()}
    want = "onboarded" if index == 0 else "removal_failed"
    assert statuses == {SID: want, SID2: want}, statuses


# ── every graph write primitive ────────────────────────────────────────────

def _graph_writers() -> list:
    return sorted(name for name, fn in vars(db).items()
                  if name.startswith("neptune_write") and inspect.isfunction(fn))


def test_the_graph_writers_are_the_ones_this_test_knows():
    """A new graph writer must be added here, and to the guard."""
    assert _graph_writers() == ["neptune_write", "neptune_write_outcome",
                                "neptune_write_returning", "neptune_write_returning_cas"]


@pytest.mark.parametrize("name", _graph_writers())
def test_every_graph_writer_sends_nothing_once_the_lease_is_lost(lost, sent, name):
    with lost(), pytest.raises(reset_lease.LeaseLostError):
        getattr(db, name)("MERGE (n:X {id: 1})")
    assert sent == []


# ── this process's own state ───────────────────────────────────────────────

@pytest.mark.parametrize("change", ["add", "remove", "reset"])
def test_the_agent_registry_is_not_changed_once_the_lease_is_lost(lost, monkeypatch, change):
    monkeypatch.setattr(orchestrator, "_mcp_clients", {SID: "client"})
    with lost(), pytest.raises(reset_lease.LeaseLostError):
        {"add": lambda: orchestrator.add_system_agent(SID2, "https://x"),
         "remove": lambda: orchestrator.remove_system_agent(SID),
         "reset": orchestrator.reset_agents}[change]()
    assert orchestrator._mcp_clients == {SID: "client"}


@pytest.mark.parametrize("change", [
    ("update_status", (SID, "onboarded")), ("mark_removing", (SID,)), ("reset_all", ()),
    ("clear_all", ()), ("remove", (SID,)), ("set_connection_config", (SID, {"host": "h"})),
    ("set_connection_origin", (SID, "user")), ("set_system_type", (SID, "MES")),
    ("add", ({"id": "new-one", "name": "New", "status": "registered"},)),
    ("keep_node_connection", ({"id": SID, "connection_config": "{}"},)),
    ("claim_schema", (SID, "other")), ("reserve_schema", (SID, "other")),
    ("commit_schema", (SID, "other")), ("release_schema_reservation", (SID, "other")),
    ("remove_entry", ({"id": SID},))], ids=lambda c: c[0])
def test_the_catalog_is_not_changed_once_the_lease_is_lost(lost, change):
    cat = SystemCatalog([{"id": SID, "name": "Line MES", "status": "registered",
                          "connection_type": "PostgreSQL", "schema": "mes"}])
    before = [dict(e) for e in cat.get_all()]
    name, args = change
    with lost(), pytest.raises(reset_lease.LeaseLostError):
        getattr(cat, name)(*args)
    assert [dict(e) for e in cat.get_all()] == before


def test_a_failed_removal_is_still_recorded_in_the_catalog_once_the_lease_is_lost(lost):
    """The record that a removal did not finish is not progress: it stays."""
    cat = SystemCatalog([{"id": SID, "name": "Line MES", "status": "removing"}])
    with lost():
        cat.mark_removal_failed(SID, True)
    assert cat.get(SID)["status"] == "removal_failed"


# ── a provision job stops and says so ──────────────────────────────────────

POST_PASSES = ["drift", "coverage", "mining", "shape"]


@pytest.mark.parametrize("index", range(len(POST_PASSES) + 1),
                         ids=["before-relation-kinds"] + POST_PASSES)
def test_a_provision_whose_lease_is_lost_runs_no_later_pass_and_says_so(
        table, clock, monkeypatch, index):
    import agents.coverage_gap_analyzer as cga
    import agents.schema_drift as schema_drift
    import agents.schema_shape as schema_shape
    ran: list = []
    order = ["kinds"] + POST_PASSES
    lose_after = order[index - 1] if index else None

    def _rec(name, result=None):
        def _fn(*_a, **_kw):
            ran.append(name)
            if lose_after == name:
                clock.advance(LOSE)
            return result
        return _fn

    def _provision(_body):
        if index == 0:
            clock.advance(LOSE)
        return {"status": "provisioned"}
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", _provision)
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds", _rec("kinds"))
    monkeypatch.setattr(schema_drift, "sweep_resolved_drift", _rec("drift", 0))
    monkeypatch.setattr(cga, "run_coverage_gap_check", _rec("coverage"))
    monkeypatch.setattr(onboard, "_post_provision_mining", _rec("mining", {}))
    monkeypatch.setattr(schema_shape, "run_schema_shape_capture", _rec("shape"))
    with Replica(table):
        claim = onboard._claim_provision(SID)
        onboard._provision_jobs["job-p"] = {"status": "running"}
        onboard._run_provision_job("job-p", MagicMock(system_id=SID, mappings=[]), claim=claim)
        record = onboard._provision_jobs["job-p"]
    assert ran == order[:index], f"ran after the lease was lost: {ran}"
    assert record.get("reason") == "provision_lease_lost", record
    assert record.get("retryable") is True


def _post_pass_recorders(monkeypatch, clock, lose_in: str = "") -> list:
    """Every step after the provision itself, as recorders (in order); the
    provision (``_do_provision``) loses the lease when *lose_in* is
    ``"provision"``."""
    import agents.coverage_gap_analyzer as cga
    import agents.schema_drift as schema_drift
    import agents.schema_shape as schema_shape
    ran: list = []

    def _rec(name, result=None):
        return lambda *_a, **_kw: ran.append(name) or result

    def _provision(_body):
        if lose_in == "provision":
            clock.advance(LOSE)
        return {"status": "provisioned"}
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", _provision)
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds", _rec("kinds"))
    monkeypatch.setattr(schema_drift, "sweep_resolved_drift", _rec("drift", 0))
    monkeypatch.setattr(cga, "run_coverage_gap_check", _rec("coverage"))
    monkeypatch.setattr(onboard, "_post_provision_mining", _rec("mining", {}))
    monkeypatch.setattr(schema_shape, "run_schema_shape_capture", _rec("shape"))
    return ran


def test_a_chat_provision_whose_lease_is_lost_runs_no_later_pass_and_says_so(
        table, clock, monkeypatch):
    """The chat's provision job releases its claim itself, so it binds the
    claim's lease check to its steps (``claim.guard()``)."""
    import agents.onboard_chat_lifecycle as lifecycle
    ran = _post_pass_recorders(monkeypatch, clock, lose_in="provision")
    finalized: list = []
    monkeypatch.setattr(lifecycle, "_finalize", lambda sid, job, rec: finalized.append(rec))
    with Replica(table):
        claim = onboard._claim_provision(SID)
        lifecycle._run_job("job-c", MagicMock(system_id=SID, mappings=[]), claim)
        record = onboard._provision_jobs["job-c"]
        assert not onboard._provision_running(SID), "the claim was not released"
    assert ran == [], f"ran after the lease was lost: {ran}"
    assert record.get("reason") == "provision_lease_lost", record
    assert finalized == [record], "the session was not told how the provision ended"


def test_a_provision_confirm_whose_lease_is_lost_says_so(table, clock, monkeypatch):
    ran = _post_pass_recorders(monkeypatch, clock, lose_in="provision")
    monkeypatch.setattr("agents.models.ProvisionRequest", lambda **kw: MagicMock(**kw))
    with Replica(table):
        out = onboard.onboard_provision_confirm({"system_id": SID, "mappings": []})
        assert not onboard._provision_running(SID)
    assert ran == [], f"ran after the lease was lost: {ran}"
    assert out.get("reason") == "provision_lease_lost" and out.get("retryable") is True, out


def test_the_removal_head_start_deletes_under_the_removal_lease_check(
        clock, cascade_world, monkeypatch):
    """The head start runs in an executor thread; it runs in a copy of the
    cascade's context, so AgentManager's deletes there check the lease too."""
    import threading
    Steps(monkeypatch, clock, SINGLE, SINGLE_RESULTS)
    seen: dict = {}
    done = threading.Event()

    def _head_start(_sid):
        seen["guard"] = db._WRITE_GUARD.get()
        done.set()
    onboard._agent_manager.delete_cloud_agent = _head_start
    onboard._reset_publish_claim(SID, True)
    asyncio.run(onboard._run_reset_job(SID, full=True))
    assert done.wait(10), "the head start never ran"
    assert seen["guard"] is not None, "the head start ran without the lease check"
    assert onboard._RESET_JOBS.get(SID)["state"] == "done"
