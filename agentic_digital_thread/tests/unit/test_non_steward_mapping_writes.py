# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Only a steward can write an ACCEPTED graph mapping through the two
user-correction routes (ADR-0050 steward gate).

``POST /api/proposed-mappings/user-define`` and ``POST
/api/metrics/correct-mapping`` both reach ``self_learning.record_user_correction``,
which stores the row ``accepted`` and applies it to Neptune at once. A steward
(``request.state.is_admin`` True, the verdict ``AuthMiddleware`` stamps) keeps
that behavior exactly. Anyone else files the same assertion as a PENDING
proposal: it lands in the steward queue, nothing is superseded, nothing reaches
Neptune, and it never auto-accepts. An ABSENT verdict is treated like a
non-steward one (fail closed).

The non-steward path does not touch the graph at all, READS INCLUDED: no target
grounding probe, no schema read for the field check. Those tests record every
statement that reaches the graph wire (``agents.db``'s one Neptune entry point
and its one Memgraph entry point), so a read anywhere below the route counts.
The field check a steward's own correction runs before writing moves, for a
user proposal, to the steward's accept (``accept_mapping``), which refuses a
field the schema knows is absent and a system that is no longer onboarded.

The route tests mount the REAL route objects (offload and all) on a bare app
whose tiny middleware stamps ``is_admin`` the way ``AuthMiddleware`` does. The
proposal store is an in-memory table and every lifecycle function runs for
real. Offline: no AWS, no graph, no network.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import agents.metric_resolver as mr
import agents.schema_check as schema_check
import agents.self_learning as self_learning
import agents.server as server
from tests.unit.test_answer_audit_auto_accept import FakeTable, _install

_UD_URL = "/api/proposed-mappings/user-define"
_CM_URL = "/api/metrics/correct-mapping"

_UD_BODY = {
    "from_system": "erp",
    "from_field": "orders.customer_code",
    "from_value": "ANYCOMPANY-001",
    "to_system": "mes",
    "to_field": "runs.cust_id",
    "to_value": "C1001",
    "concept": "customer",
}

_CM_BODY = {
    "system_id": "cmms",
    "concept": "mro-inventory",
    "to_field": "parts.qty_on_hand",
    "from_field": "parts.qty",
}


def _real_route(app_or_router, path: str) -> APIRoute:
    for r in app_or_router.routes:
        if isinstance(r, APIRoute) and r.path == path and "POST" in r.methods:
            return r
    raise AssertionError(f"route not registered: POST {path}")


def _client(is_admin: "bool | None") -> TestClient:
    """Bare app carrying the REAL user-define and correct-mapping routes.
    ``is_admin=None`` mounts no middleware, so the verdict is ABSENT."""
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    app.router.routes.append(_real_route(server.app, _UD_URL))
    app.router.routes.append(_real_route(mr.router, _CM_URL))
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _systems_onboarded(monkeypatch):
    """Every system is onboarded. ``is_onboarded_system`` reads the in-memory
    catalog only, so stubbing it hides no graph read."""
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)


@pytest.fixture
def store(monkeypatch):
    """In-memory proposal store plus the list of every Neptune write, with the
    self_learning graph seams stubbed and every field present (the steward
    path's fixture)."""
    monkeypatch.setattr(schema_check, "column_exists", lambda _sid, _field: True)
    table = FakeTable()
    writes = _install(monkeypatch, table)
    return table, writes


@pytest.fixture
def wire(monkeypatch):
    """In-memory proposal store plus every statement that reaches the graph
    WIRE, reads and writes alike. Nothing above ``agents.db`` is stubbed: the
    self_learning seams, ``graph_tools.get_system_schema`` and the real
    ``schema_check.column_exists`` all run, so any graph access below the route
    lands in the list. Every statement answers no rows."""
    import agents.db as db
    calls: list[str] = []

    def _record(cypher, parameters=None):
        calls.append(cypher)
        return []

    monkeypatch.setattr(db, "_neptune_execute", _record)
    monkeypatch.setattr(db, "_memgraph_run", _record)
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    return table, calls


def _ud_keys():
    return self_learning._canonical_keys_for(
        "value_mapping", from_system="erp", from_field="orders.customer_code",
        from_value="ANYCOMPANY-001", to_system="mes", to_field="runs.cust_id",
        to_value="C1001", concept="customer", annotation="user_correction")


def _cm_keys(to_field: str = "parts.qty_on_hand"):
    return self_learning._canonical_keys_for(
        "concept_mapping", from_system="", from_field="parts.qty",
        from_value="", to_system="cmms", to_field=to_field, to_value="",
        concept="mro-inventory", annotation="user_correction")


def _accepted_prior(subject_key: str, target_key: str, **extra) -> dict:
    """An ACCEPTED row on the same subject with a DIFFERENT target."""
    row = {
        "mapping_id": "prior-accepted",
        "discovered_at": "2026-08-01T10:00:00+00:00",
        "status": "accepted",
        "source": "agent",
        "subject_key": subject_key,
        "target_key": target_key,
        "version": "1",
        "confidence": "0.9",
        "neptune_node_id": "existing-node",
    }
    row.update(extra)
    return row


def _rows(table: FakeTable) -> list[dict]:
    return table.scan()["Items"]


# --------------------------------------------------------------------------- #
# user-define
# --------------------------------------------------------------------------- #
_NON_STEWARD = pytest.mark.parametrize(
    "is_admin", [False, None], ids=["non-steward", "absent-verdict"])


def _same_target_row(keys, status: str, **extra) -> dict:
    """A row with EXACTLY the submission's canonical keys (same subject and
    target), filed earlier by someone else."""
    row = {
        "mapping_id": f"earlier-{status}",
        "discovered_at": "2026-08-02T10:00:00+00:00",
        "status": status,
        "source": "agent",
        "subject_key": keys.subject_key,
        "target_key": keys.target_key,
        "version": "1",
        "confidence": "0.9",
    }
    row.update(extra)
    return row


@_NON_STEWARD
def test_user_define_from_non_steward_files_pending_and_touches_nothing(wire, is_admin):
    table, calls = wire
    table.put_item(_accepted_prior(
        _ud_keys().subject_key, "vm-target-other",
        proposed_type="value_mapping", from_system="erp", to_system="mes",
        from_field="orders.customer_code", from_value="ANYCOMPANY-001",
        to_field="runs.cust_id", to_value="C9999", concept="customer"))

    resp = _client(is_admin).post(_UD_URL, json=_UD_BODY)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "pending"
    assert "neptune_node_id" not in payload
    filed = table.by_id(payload["mapping_id"])
    assert filed["status"] == "pending"
    assert filed["source"] == "user"
    assert filed["method"] == "user_proposed"
    assert filed["to_value"] == "C1001"
    assert "accepted_at" not in filed and "neptune_node_id" not in filed
    # No grounding was attached: the review surface shows the target unchecked.
    assert "target_verified" not in filed and "verified_scope" not in filed
    assert "target_verified" not in self_learning.review_trust_signals(filed)
    # The accepted row on the same subject is untouched.
    assert table.by_id("prior-accepted")["status"] == "accepted"
    assert [r for r in _rows(table) if r["status"] == "accepted"] == [
        table.by_id("prior-accepted")]
    # Nothing reached the graph: no write, and no read either.
    assert calls == []


@_NON_STEWARD
def test_user_define_retry_returns_the_same_row_and_touches_nothing(wire, is_admin):
    table, calls = wire
    client = _client(is_admin)
    first = client.post(_UD_URL, json=_UD_BODY).json()
    second = client.post(_UD_URL, json=_UD_BODY).json()
    assert first["status"] == second["status"] == "pending"
    assert second["mapping_id"] == first["mapping_id"]
    assert len(_rows(table)) == 1
    assert calls == []


@_NON_STEWARD
@pytest.mark.parametrize("status", ["pending", "accepted"])
def test_user_define_identical_row_is_returned_and_nothing_is_touched(wire, is_admin, status):
    table, calls = wire
    earlier = _same_target_row(_ud_keys(), status, proposed_type="value_mapping")
    table.put_item(earlier)

    resp = _client(is_admin).post(_UD_URL, json=_UD_BODY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == status
    assert resp.json()["mapping_id"] == earlier["mapping_id"]
    assert _rows(table) == [earlier]
    assert calls == []


def test_the_wire_recorder_sees_the_steward_path_reads(wire):
    """Positive control for the zero-call assertions above: the steward's field
    check reads the schema from the graph, and the recorder sees it."""
    _table, calls = wire
    _client(True).post(_UD_URL, json=_UD_BODY)
    assert any("HAS_SCHEMA" in cypher for cypher in calls), calls


def test_user_define_from_steward_is_unchanged(store):
    table, writes = store
    table.put_item(_accepted_prior(
        _ud_keys().subject_key, "vm-target-other",
        proposed_type="value_mapping", from_system="erp", to_system="mes",
        from_field="orders.customer_code", from_value="ANYCOMPANY-001",
        to_field="runs.cust_id", to_value="C9999", concept="customer"))

    resp = _client(True).post(_UD_URL, json=_UD_BODY)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "accepted"
    assert payload["neptune_node_id"]
    assert payload["version"] == 2
    accepted = [r for r in _rows(table) if r["status"] == "accepted"]
    assert len(accepted) == 1 and accepted[0]["to_value"] == "C1001"
    assert accepted[0]["method"] == "user_corrected"
    assert table.by_id("prior-accepted")["status"] == "superseded"
    assert writes, "a steward correction applies to the graph at once"


def test_user_define_steward_calls_the_correction_door_with_todays_arguments(monkeypatch):
    record = Mock(return_value={"status": "accepted", "neptune_node_id": "n",
                                "proposed_type": "value_mapping", "version": 1})
    propose = Mock(side_effect=AssertionError("a steward never files a proposal here"))
    monkeypatch.setattr(self_learning, "record_user_correction", record)
    monkeypatch.setattr(self_learning, "record_user_proposal", propose, raising=False)

    resp = _client(True).post(_UD_URL, json={**_UD_BODY, "description": "d"})

    assert resp.status_code == 200
    assert record.call_count == 1
    assert record.call_args.kwargs == {
        "from_system": "erp", "from_field": "orders.customer_code",
        "from_value": "ANYCOMPANY-001", "to_system": "mes", "to_field": "runs.cust_id",
        "to_value": "C1001", "concept": "customer", "proposed_type": "value_mapping",
        "description": "d",
        # The one-row stamp; this bare app has no signed-in user, so no actor.
        "actor": "", "confirmation_mode": "single_row",
    }


@_NON_STEWARD
def test_user_define_non_steward_never_reaches_the_correction_door(monkeypatch, wire, is_admin):
    monkeypatch.setattr(self_learning, "record_user_correction",
                        Mock(side_effect=AssertionError("accepted-on-entry door")))
    resp = _client(is_admin).post(_UD_URL, json=_UD_BODY)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"


def test_user_define_validation_still_runs_before_either_door(monkeypatch, wire):
    table, calls = wire
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda sid: sid != "mes")
    resp = _client(False).post(_UD_URL, json=_UD_BODY)
    assert resp.status_code == 400
    assert "not an onboarded system" in resp.json()["error"]
    assert _rows(table) == [] and calls == []


@_NON_STEWARD
def test_user_define_non_steward_does_not_read_the_schema_for_the_field_check(
        monkeypatch, wire, is_admin):
    """A non-steward's field check is the steward's accept, not a graph read
    here: even a field the schema would call absent is filed pending."""
    table, calls = wire
    monkeypatch.setattr(schema_check, "column_exists",
                        Mock(side_effect=AssertionError("no schema read for a non-steward")))
    resp = _client(is_admin).post(_UD_URL, json={**_UD_BODY, "to_field": "runs.nope"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert calls == []


# --------------------------------------------------------------------------- #
# correct-mapping
# --------------------------------------------------------------------------- #
@_NON_STEWARD
def test_correct_mapping_from_non_steward_files_pending_and_touches_nothing(
        monkeypatch, wire, is_admin):
    table, calls = wire
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    enqueue = Mock(name="rederive.enqueue")
    monkeypatch.setattr(mr.rederive, "enqueue", enqueue)
    import agents.dashboard as dashboard
    bust = Mock(name="invalidate_dashboard_caches")
    monkeypatch.setattr(dashboard, "invalidate_dashboard_caches", bust)
    table.put_item(_accepted_prior(
        _cm_keys().subject_key, _cm_keys("parts.qty").target_key,
        proposed_type="concept_mapping", to_system="cmms", to_field="parts.qty",
        concept="mro-inventory", source="user"))

    resp = _client(is_admin).post(_CM_URL, json=_CM_BODY)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "pending"
    assert payload["concept"] == "mro-inventory"
    assert payload["affected_cards"] == []
    filed = table.by_id(payload["mapping_id"])
    assert filed["status"] == "pending"
    assert filed["proposed_type"] == "concept_mapping"
    assert filed["to_field"] == "parts.qty_on_hand"
    assert filed["source"] == "user"
    assert "target_verified" not in filed
    assert table.by_id("prior-accepted")["status"] == "accepted"
    assert calls == []
    enqueue.assert_not_called()
    bust.assert_not_called()


@_NON_STEWARD
def test_correct_mapping_retry_returns_the_same_row_and_touches_nothing(wire, is_admin):
    table, calls = wire
    client = _client(is_admin)
    first = client.post(_CM_URL, json=_CM_BODY).json()
    second = client.post(_CM_URL, json=_CM_BODY).json()
    assert first["status"] == second["status"] == "pending"
    assert second["mapping_id"] == first["mapping_id"]
    assert len(_rows(table)) == 1
    assert calls == []


@_NON_STEWARD
@pytest.mark.parametrize("status", ["pending", "accepted"])
def test_correct_mapping_identical_row_is_returned_and_nothing_is_touched(
        wire, is_admin, status):
    table, calls = wire
    earlier = _same_target_row(_cm_keys(), status, proposed_type="concept_mapping")
    table.put_item(earlier)

    resp = _client(is_admin).post(_CM_URL, json=_CM_BODY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == status
    assert resp.json()["mapping_id"] == earlier["mapping_id"]
    assert resp.json()["affected_cards"] == []
    assert _rows(table) == [earlier]
    assert calls == []


def test_correct_mapping_from_steward_is_unchanged(monkeypatch):
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    record = Mock(return_value={"status": "accepted", "neptune_node_id": "mapsto-cmms-x",
                                "proposed_type": "concept_mapping", "version": 2})
    monkeypatch.setattr(mr, "record_user_correction", record)
    monkeypatch.setattr(mr, "record_user_proposal",
                        Mock(side_effect=AssertionError("steward never files pending")),
                        raising=False)
    monkeypatch.setattr(mr.rederive, "affected_cards", lambda concept: ["oee"])

    resp = _client(True).post(_CM_URL, json=_CM_BODY)

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "accepted", "neptune_node_id": "mapsto-cmms-x", "version": 2,
        "concept": "mro-inventory", "affected_cards": ["oee"],
    }
    assert record.call_args.kwargs == {
        "proposed_type": "concept_mapping", "to_system": "cmms",
        "to_field": "parts.qty_on_hand", "concept": "mro-inventory",
        "from_system": "", "from_field": "parts.qty", "from_value": "",
        "to_value": "", "confidence": 1.0, "query_context": "user_correction",
    }


@_NON_STEWARD
def test_correct_mapping_non_steward_never_reaches_the_correction_door(
        monkeypatch, wire, is_admin):
    monkeypatch.setattr(mr, "record_user_correction",
                        Mock(side_effect=AssertionError("accepted-on-entry door")))
    resp = _client(is_admin).post(_CM_URL, json=_CM_BODY)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


def test_correct_mapping_non_steward_unknown_system_is_refused(monkeypatch, wire):
    table, calls = wire
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: False)
    resp = _client(False).post(_CM_URL, json=_CM_BODY)
    assert resp.status_code == 200
    assert "not an onboarded system" in resp.json()["error"]
    assert _rows(table) == [] and calls == []


@_NON_STEWARD
def test_correct_mapping_non_steward_does_not_read_the_schema(monkeypatch, wire, is_admin):
    """The column check moves to the steward's accept: no schema read here."""
    table, calls = wire
    monkeypatch.setattr(schema_check, "column_exists",
                        Mock(side_effect=AssertionError("no schema read for a non-steward")))
    resp = _client(is_admin).post(_CM_URL, json={**_CM_BODY, "to_field": "parts.nope"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert calls == []


# --------------------------------------------------------------------------- #
# The pending door itself
# --------------------------------------------------------------------------- #
def test_record_user_proposal_dedupes_an_identical_active_row(store):
    table, _writes = store
    first = self_learning.record_user_proposal(**_UD_BODY)
    second = self_learning.record_user_proposal(**_UD_BODY)
    assert first["status"] == "pending"
    assert second == {**first}
    assert len(_rows(table)) == 1


def test_record_user_proposal_refuses_metric_definition(store):
    table, _writes = store
    with pytest.raises(ValueError, match="metric_definition"):
        self_learning.record_user_proposal(**_UD_BODY, proposed_type="metric_definition")
    assert _rows(table) == []


def test_steward_accepts_a_user_proposal_through_the_normal_accept(store):
    table, writes = store
    filed = self_learning.record_user_proposal(**_UD_BODY)
    assert writes == []
    result = self_learning.accept_mapping(filed["mapping_id"], actor="steward@example.com")
    assert result["status"] == "accepted"
    row = table.by_id(filed["mapping_id"])
    assert row["status"] == "accepted" and row["accepted_by"] == "steward@example.com"
    assert writes


def test_a_pending_user_proposal_never_auto_accepts(monkeypatch, store):
    """Both auto-accept doors leave a non-steward's proposal pending, while a
    same-shape agent row in the same store does accept (positive control)."""
    from tests.unit.test_answer_audit_auto_accept import (
        _UUID_AGENT, _UUID_AUDIT, _pending_row)
    table, _writes = store
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    user_row = _pending_row("m-user", "user", _UUID_AUDIT, "2026-08-18T10:00:00+00:00")
    control = _pending_row("m-agent", "agent", _UUID_AGENT, "2026-08-18T10:00:01+00:00")
    table.put_item(user_row)
    table.put_item(control)

    assert self_learning._maybe_auto_accept("m-user", user_row) is False
    assert server._run_auto_accept_sweep("post-query") == {"accepted": 1, "failed": 0}
    assert table.by_id("m-agent")["status"] == "accepted"
    assert table.by_id("m-user")["status"] == "pending"


# --------------------------------------------------------------------------- #
# The steward's accept runs the field check a user proposal skipped
# --------------------------------------------------------------------------- #
def _accept_client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):  # noqa: ANN001
        request.state.is_admin = True
        return await call_next(request)

    for r in server.app.routes:
        if isinstance(r, APIRoute) and r.path == "/api/proposed-mappings/{mapping_id}/accept":
            app.router.routes.append(r)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("missing", ["orders.customer_code", "runs.cust_id"])
def test_steward_accept_refuses_a_user_proposal_whose_field_is_absent(
        monkeypatch, store, missing):
    table, writes = store
    filed = self_learning.record_user_proposal(**_UD_BODY)
    monkeypatch.setattr(schema_check, "column_exists",
                        lambda _sid, field: field != missing)

    resp = _accept_client().post(f"/api/proposed-mappings/{filed['mapping_id']}/accept")

    assert resp.status_code == 400, resp.text
    assert missing in resp.json()["error"]
    assert "stays pending" in resp.json()["error"]
    assert table.by_id(filed["mapping_id"])["status"] == "pending"
    assert writes == []


def test_steward_accept_refuses_a_user_proposal_for_a_system_no_longer_onboarded(
        monkeypatch, store):
    table, writes = store
    filed = self_learning.record_user_proposal(**_UD_BODY)
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda sid: sid != "mes")

    result = self_learning.accept_mapping(filed["mapping_id"], actor="steward@example.com")

    assert result["http_status"] == 400
    assert "'mes' is not an onboarded system" in result["error"]
    assert table.by_id(filed["mapping_id"])["status"] == "pending"
    assert writes == []


def test_steward_accept_refuses_a_user_repoint_to_an_absent_column(monkeypatch, store):
    table, writes = store
    filed = self_learning.record_user_proposal(
        proposed_type="concept_mapping", to_system="cmms", to_field="parts.nope",
        concept="mro-inventory", from_system="", from_field="parts.qty",
        from_value="", to_value="", query_context="user_correction")
    monkeypatch.setattr(schema_check, "column_exists", lambda _sid, _field: False)

    result = self_learning.accept_mapping(filed["mapping_id"])

    assert result["http_status"] == 400
    assert "parts.nope" in result["error"]
    assert table.by_id(filed["mapping_id"])["status"] == "pending"
    assert writes == []


def test_the_accept_check_leaves_every_other_row_alone(monkeypatch, store):
    """Only ``method=user_proposed`` rows get the accept-time check; an agent
    row accepts exactly as before, without a schema read."""
    from tests.unit.test_answer_audit_auto_accept import _UUID_AGENT, _pending_row
    table, writes = store
    row = _pending_row("m-agent", "agent", _UUID_AGENT, "2026-08-18T10:00:01+00:00")
    table.put_item(row)
    monkeypatch.setattr(schema_check, "column_exists",
                        Mock(side_effect=AssertionError("no extra check for agent rows")))
    monkeypatch.setattr(schema_check, "is_onboarded_system",
                        Mock(side_effect=AssertionError("no extra check for agent rows")))

    assert self_learning.accept_mapping("m-agent")["status"] == "accepted"
    assert writes
