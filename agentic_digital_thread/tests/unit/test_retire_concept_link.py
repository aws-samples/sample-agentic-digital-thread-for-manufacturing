# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A steward retires one concept -> column link, and it stays retired.

Measured on an analytics deployment: two wrong links captured at onboarding
kept steering a system agent's SQL, and no route removed a single
concept -> column link. The correction route REPOINTS a concept (every other
link of that concept is deleted), which is wrong when the concept also has
right links that must stay.

``POST /api/metrics/retire-mapping`` removes exactly one link:

* a steward's call is recorded ACCEPTED in the proposal store (source user,
  the same audit row a correction gets) and deletes that one edge at once;
  anyone else's call files it pending for a steward;
* re-provisioning does not bring it back: the onboarding snapshot leaves it
  out, and a sweep after the provision writes deletes it again and reads the
  edge back. An unreadable retirement ledger, a failed delete or an edge still
  there fails the provision; it is never reported provisioned;
* a later steward correction to the same column voids the retirement.

Neutral fixtures (system-a, table_x). Fully offline.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agents.metric_resolver as mr
import agents.onboard as onboard
import agents.self_learning as sl
from tests.unit.test_self_learning_lifecycle import FakeTable

_STEWARD = SimpleNamespace(state=SimpleNamespace(is_admin=True))
_RETIRE = "concept_mapping_retire"


def _retire_request(**overrides):
    body = {"system_id": "system-a", "concept": "production-line",
            "field": "table_x.location"}
    body.update(overrides)
    return mr.RetireMappingRequest(**body)


# --------------------------------------------------------------------------- #
# 1. The route
# --------------------------------------------------------------------------- #
def test_a_steward_retirement_is_recorded_accepted_through_the_correction_door(monkeypatch):
    record = Mock(return_value={"status": "accepted", "neptune_node_id": "n-1",
                                "proposed_type": _RETIRE, "version": 1})
    monkeypatch.setattr(mr, "record_user_correction", record)
    result = asyncio.run(mr.retire_mapping_route(_retire_request(), _STEWARD))
    assert result["status"] == "accepted"
    assert result["concept"] == "production-line"
    assert result["field"] == "table_x.location"
    kwargs = record.call_args.kwargs
    assert kwargs["proposed_type"] == _RETIRE
    assert kwargs["to_system"] == "system-a"
    assert kwargs["to_field"] == "table_x.location"
    assert kwargs["concept"] == "production-line"


def test_a_nested_column_is_accepted_whole(monkeypatch):
    record = Mock(return_value={"status": "accepted", "neptune_node_id": "n", "version": 1})
    monkeypatch.setattr(mr, "record_user_correction", record)
    asyncio.run(mr.retire_mapping_route(
        _retire_request(field="table_x.details.product.ink"), _STEWARD))
    assert record.call_args.kwargs["to_field"] == "table_x.details.product.ink"


def test_anyone_else_files_it_pending(monkeypatch):
    record = Mock()
    propose = Mock(return_value={"status": "pending", "mapping_id": "m-1", "version": 1})
    monkeypatch.setattr(mr, "record_user_correction", record)
    monkeypatch.setattr(mr, "record_user_proposal", propose)
    monkeypatch.setattr("agents.schema_check.is_onboarded_system", lambda _s: True)
    non_steward = SimpleNamespace(state=SimpleNamespace(is_admin=False))
    result = asyncio.run(mr.retire_mapping_route(_retire_request(), non_steward))
    assert result["status"] == "pending" and result["mapping_id"] == "m-1"
    assert record.call_count == 0
    assert propose.call_args.kwargs["proposed_type"] == _RETIRE


@pytest.mark.parametrize("overrides", [
    {"field": "no_dot"}, {"field": ".col"}, {"field": "tbl."},
    {"concept": " "}, {"system_id": ""},
])
def test_a_malformed_body_is_refused_before_any_write(monkeypatch, overrides):
    record = Mock()
    monkeypatch.setattr(mr, "record_user_correction", record)
    result = asyncio.run(mr.retire_mapping_route(_retire_request(**overrides), _STEWARD))
    assert "error" in result
    assert record.call_count == 0


def test_the_route_runs_on_the_resolver_lane():
    import inspect
    source = inspect.getsource(mr.retire_mapping_route)
    assert "offload_resolver(_retire_mapping_payload" in source


# --------------------------------------------------------------------------- #
# 2. The apply: one edge, fail-loud, read back
# --------------------------------------------------------------------------- #
class _EdgeGraph:
    """MAPS_TO edges as (field id, concept) pairs, for the retire apply."""

    def __init__(self, edges, fields=None, stuck=False, readback_fails=False):
        self.edges = set(edges)
        self.fields = set(fields if fields is not None else {f for f, _ in edges})
        self.stuck = stuck
        self.readback_fails = readback_fails
        self.writes: list = []

    def checked(self, cypher, params=None):
        if self.readback_fails:
            raise RuntimeError("graph down")
        return self.query(cypher, params)

    def query(self, cypher, params=None):
        params = params or {}
        if "count(f)" in cypher:
            return [{"n": int(params.get("fid") in self.fields)}]
        if "count(s)" in cypher:
            return [{"n": 1}]
        if "count(r)" in cypher:
            n = sum(1 for f, c in self.edges if f == params["fid"] and c == params["concept"])
            return [{"n": n}]
        raise AssertionError(cypher)

    def write(self, cypher, params=None):
        self.writes.append((cypher, params))
        if "DELETE r" in cypher and not self.stuck:
            self.edges.discard((params["fid"], params["concept"]))
        return True


def _install(monkeypatch, graph, table=None):
    monkeypatch.setattr(sl, "_neptune_query", graph.query)
    monkeypatch.setattr(sl, "_neptune_query_checked", graph.checked, raising=False)
    monkeypatch.setattr(sl, "_neptune_write", graph.write)
    if table is not None:
        monkeypatch.setattr(sl, "_get_table", lambda: table)


def _item(field="table_x.location", concept="production-line"):
    return {"proposed_type": _RETIRE, "to_system": "system-a", "to_field": field,
            "concept": concept}


def test_retire_deletes_only_that_one_link(monkeypatch):
    graph = _EdgeGraph({
        ("system-a.table_x.location", "production-line"),
        ("system-a.table_x.cell", "production-line"),      # the right link stays
        ("system-a.table_x.location", "site"),             # another concept stays
    })
    _install(monkeypatch, graph)
    sl._retire_maps_to(_item())
    assert graph.edges == {("system-a.table_x.cell", "production-line"),
                           ("system-a.table_x.location", "site")}


def test_retire_of_a_nested_column_uses_the_whole_field_id(monkeypatch):
    graph = _EdgeGraph({("system-a.table_x.details.product.ink", "media")})
    _install(monkeypatch, graph)
    sl._retire_maps_to(_item("table_x.details.product.ink", "media"))
    assert graph.edges == set()


def test_retire_of_a_column_not_in_the_schema_refuses_and_deletes_nothing(monkeypatch):
    graph = _EdgeGraph(set(), fields=set())
    _install(monkeypatch, graph)
    with pytest.raises(ValueError):
        sl._retire_maps_to(_item())
    assert not any("DELETE" in c for c, _ in graph.writes)


def test_a_link_still_there_after_the_delete_fails_loud(monkeypatch):
    graph = _EdgeGraph({("system-a.table_x.location", "production-line")}, stuck=True)
    _install(monkeypatch, graph)
    with pytest.raises(RuntimeError):
        sl._retire_maps_to(_item())


def test_an_unreadable_readback_after_the_delete_fails_loud(monkeypatch):
    # The swallowing read seam answers [] on a backend error, which read as
    # "count 0, link gone"; the readback must be a checked read.
    graph = _EdgeGraph({("system-a.table_x.location", "production-line")},
                       stuck=True, readback_fails=True)
    _install(monkeypatch, graph)
    monkeypatch.setattr(sl, "_neptune_query", lambda *_a, **_k: [])
    with pytest.raises(RuntimeError):
        sl._retire_maps_to(_item())


def test_retire_drops_the_metric_turn_cache():
    import agents.metric_turn_invalidation as mti
    assert getattr(sl._retire_maps_to, mti.MARKER_ATTR, False) is True
    assert getattr(onboard._sweep_retired_concept_links, mti.MARKER_ATTR, False) is True


def test_a_retirement_is_never_auto_accepted():
    assert _RETIRE in sl._NEVER_AUTO_ACCEPT_TYPES


# --------------------------------------------------------------------------- #
# 3. Audit: the same accepted row a correction gets
# --------------------------------------------------------------------------- #
def test_a_steward_retirement_writes_an_accepted_user_row_and_removes_the_edge(monkeypatch):
    table = FakeTable()
    graph = _EdgeGraph({("system-a.table_x.location", "production-line"),
                        ("system-a.table_x.stale_at", "production-line")})
    _install(monkeypatch, graph, table)
    for field in ("table_x.location", "table_x.stale_at"):
        result = sl.record_user_correction(
            from_system="", from_field="", from_value="", to_system="system-a",
            to_field=field, to_value="", concept="production-line",
            proposed_type=_RETIRE)
        assert result["status"] == "accepted"
    rows = sorted(table.all_items(), key=lambda r: r["to_field"])
    assert [r["to_field"] for r in rows] == ["table_x.location", "table_x.stale_at"]
    for row in rows:   # two retirements on one concept coexist
        assert row["status"] == "accepted"
        assert row["proposed_type"] == _RETIRE
        assert row["source"] == "user" and row["method"] == "user_corrected"
    assert graph.edges == set()


# --------------------------------------------------------------------------- #
# 4. Which retirements stand
# --------------------------------------------------------------------------- #
def _row(ptype, concept, field, at, system="system-a", source="user"):
    return {"proposed_type": ptype, "concept": concept, "to_field": field,
            "to_system": system, "source": source, "discovered_at": at,
            "status": "accepted"}


def _accepted(rows):
    return lambda: [r for r in rows if r["status"] == "accepted"]


def test_retired_links_are_listed_and_a_later_correction_to_that_column_voids_one(monkeypatch):
    rows = [
        _row(_RETIRE, "production-line", "table_x.location", "2026-01-02T00:00:00"),
        _row(_RETIRE, "dwell_unit", "table_x.stale_at", "2026-01-02T00:00:00"),
        # the steward later pointed dwell_unit back at the same column: void
        _row("concept_mapping", "dwell_unit", "table_x.stale_at", "2026-01-03T00:00:00"),
        # a correction BEFORE the retirement does not void it
        _row("concept_mapping", "production-line", "table_x.location", "2026-01-01T00:00:00"),
        # another system's retirement is not this system's
        _row(_RETIRE, "site", "table_x.cell", "2026-01-02T00:00:00", system="system-b"),
    ]
    monkeypatch.setattr(sl, "_retirement_rows", _accepted(rows))
    assert sl.accepted_retired_concept_links("system-a") == [
        {"concept": "production-line", "field": "table_x.location"}]


def test_a_store_failure_is_not_read_as_no_retirements(monkeypatch):
    def _boom(_status, **_k):
        raise RuntimeError("store down")
    monkeypatch.setattr(sl, "_query_by_status", _boom)
    monkeypatch.setattr(sl, "_retirement_rows", lambda: sl._query_by_status("accepted"))
    # Before: [] (no retirements), so a re-provision could write the link back.
    with pytest.raises(sl.RetirementLedgerUnavailable):
        sl.accepted_retired_concept_links("system-a")


# --------------------------------------------------------------------------- #
# 5. Re-provision does not bring it back
# --------------------------------------------------------------------------- #
_MAPPINGS = [
    {"table": "table_x", "field": "location",
     "mappings": [{"concept": "production-line", "role": "correlate"}]},
    {"table": "table_x", "field": "cell",
     "mappings": [{"concept": "production-line", "role": "correlate"}]},
]


def test_the_onboarding_snapshot_leaves_a_retired_link_out(monkeypatch):
    from agents.models import ProvisionRequest
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: ["production-line"])
    monkeypatch.setattr(sl, "accepted_retired_concept_links", lambda _sid: [
        {"concept": "production-line", "field": "table_x.location"}], raising=False)
    ctx = onboard._make_provision_state(ProvisionRequest(
        system_id="system-a", schema="db_a", mappings=_MAPPINGS, connection_type="Athena"))
    assert ctx.cm_items == [
        {"concept": "production-line", "field": "table_x.cell", "role": "correlate"}]


_TWO = [{"concept": "production-line", "field": "table_x.location"},
        {"concept": "media", "field": "table_x.details.product.ink"}]


def _sweep_graph(monkeypatch, *, write_ok=True, left=(), read_fails=False):
    """The sweep's write and its checked readback, over (fid, concept) edges."""
    writes, edges = [], set(left)

    def _write(cypher, params=None):
        writes.append((cypher, params))
        return write_ok

    def _read(cypher, params=None):
        if read_fails:
            raise RuntimeError("graph down")
        assert "count(r)" in cypher
        return [{"n": int((params["fid"], params["concept"]) in edges)}]

    monkeypatch.setattr(onboard, "neptune_write", _write)
    monkeypatch.setattr(onboard, "neptune_query_checked", _read)
    return writes


def test_the_sweep_deletes_each_retired_link_and_reads_it_back(monkeypatch):
    monkeypatch.setattr(sl, "accepted_retired_concept_links", lambda _sid: list(_TWO))
    writes = _sweep_graph(monkeypatch)
    assert onboard._sweep_retired_concept_links("system-a") is True
    params = [p for c, p in writes if "DELETE r" in c]
    assert params == [
        {"fid": "system-a.table_x.location", "concept": "production-line"},
        {"fid": "system-a.table_x.details.product.ink", "concept": "media"}]


def test_a_failed_delete_fails_the_sweep(monkeypatch):
    monkeypatch.setattr(sl, "accepted_retired_concept_links", lambda _sid: list(_TWO))
    _sweep_graph(monkeypatch, write_ok=False)
    assert onboard._sweep_retired_concept_links("system-a") is False


def test_an_edge_still_there_after_the_delete_fails_the_sweep(monkeypatch):
    monkeypatch.setattr(sl, "accepted_retired_concept_links", lambda _sid: list(_TWO))
    _sweep_graph(monkeypatch, left={("system-a.table_x.location", "production-line")})
    # Before: no readback, so this reported success.
    assert onboard._sweep_retired_concept_links("system-a") is False


def test_an_unreadable_readback_fails_the_sweep(monkeypatch):
    monkeypatch.setattr(sl, "accepted_retired_concept_links", lambda _sid: list(_TWO))
    _sweep_graph(monkeypatch, read_fails=True)
    assert onboard._sweep_retired_concept_links("system-a") is False


def test_an_unreadable_ledger_fails_the_sweep(monkeypatch):
    def _boom(_sid):
        raise sl.RetirementLedgerUnavailable("store down")
    monkeypatch.setattr(sl, "accepted_retired_concept_links", _boom)
    _sweep_graph(monkeypatch)
    assert onboard._sweep_retired_concept_links("system-a") is False


def test_an_unreadable_ledger_stops_the_provision_before_any_write(monkeypatch):
    from agents.models import ProvisionRequest

    def _boom(_sid):
        raise sl.RetirementLedgerUnavailable("store down")
    monkeypatch.setattr(sl, "accepted_retired_concept_links", _boom)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: ["production-line"])
    started = []
    monkeypatch.setattr(onboard, "_dp_connection_phase", lambda ctx: started.append(1))
    result = onboard._do_provision_base(ProvisionRequest(
        system_id="system-a", schema="db_a", mappings=_MAPPINGS, connection_type="Athena"))
    assert started == []
    assert result["reason"] == "retirements_unreadable"
    record = onboard._provision_error_record(result)
    assert record["retryable"] is True and record["status"] == "error"


def _steps(monkeypatch, sweep_ok=True, dry_run=False):
    from agents.models import ProvisionRequest
    calls = []
    monkeypatch.setattr(onboard, "_do_provision_base",
                        lambda body, dry_run=False: {"status": "provisioned"})
    monkeypatch.setattr(onboard, "_provision_knowledge_post_pass", lambda *_a, **_k: None)
    monkeypatch.setattr("agents.asset_role_pass.run_after_provision", lambda: None)
    monkeypatch.setattr(onboard, "_sweep_retired_concept_links",
                        lambda sid: calls.append(sid) or sweep_ok, raising=False)
    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: True)
    body = ProvisionRequest(system_id="system-a", connection_type="Athena")
    return onboard._do_provision_steps(body, dry_run=dry_run), calls


def test_a_provision_runs_the_sweep(monkeypatch):
    result, calls = _steps(monkeypatch)
    assert calls == ["system-a"]
    assert result["status"] == "provisioned"
    assert result["provision_run_id"].startswith("run-")


def test_a_provision_whose_sweep_failed_carries_no_run_id(monkeypatch):
    """The run id names the concepts a SUCCESSFUL run created; a failed
    sweep fails the provision, so it carries none."""
    result, _calls = _steps(monkeypatch, sweep_ok=False)
    assert "provision_run_id" not in result


def test_a_provision_whose_sweep_failed_is_not_reported_provisioned(monkeypatch):
    result, _calls = _steps(monkeypatch, sweep_ok=False)
    # Before: {"status": "provisioned", "retired_link_sweep_failed": True}.
    assert result.get("status") != "provisioned"
    assert result["reason"] == "retired_link_sweep_failed"
    record = onboard._provision_error_record(result)
    assert record["retryable"] is True and record["status"] == "error"


def test_a_dry_run_does_not_sweep(monkeypatch):
    _result, calls = _steps(monkeypatch, dry_run=True)
    assert calls == []


# --------------------------------------------------------------------------- #
# 6. A steward remap is a confirmed link
# --------------------------------------------------------------------------- #
def test_a_steward_remap_marks_the_edge_user_confirmed(monkeypatch):
    from agents.models import RemapField, RemapRequest
    writes = []
    monkeypatch.setattr(onboard, "neptune_write_returning",
                        lambda cypher, params=None: writes.append(cypher) or [{"n": 1}])
    onboard.onboard_remap(RemapRequest(remaps=[RemapField(
        field_id="system-a.table_x.cell", concept_id="site", role="identifier")]))
    merge = next(c for c in writes if "MAPS_TO" in c)
    assert "rel.method = 'user_confirmed'" in merge
    assert "rel.confirmed = true" in merge
