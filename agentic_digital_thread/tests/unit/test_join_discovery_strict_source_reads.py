# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A strict join sweep tells a failed source read from an empty column.

The workshop seed's ``Phase2JoinSweep`` once gives each pair of seeded
systems to ONE of the two re-resolves, which sweeps it strictly
(``discover_joins_for_system(strict=True)``): a direction that raises is
tried once more, then the peer is reported ``unswept`` and the seed retries
the resolve, then fails. Source sampling and containment probes answered a
failed read with ``[]`` / ``None``, so a transient read failure looked like
an empty column: zero proposals, ``incomplete`` False, no retry, and the seed
stayed green without the ``mes-ind.production_runs.run_number`` ->
``erp.shipment_items.batch_id`` join. Strict sweeps now raise on a failed
read (``resolve_evidence.checked_source_reads``); every other caller (the
wizard, chat, re-onboard, phase 1, the per-system lap: no ``strict``, no skip
list) keeps the best-effort answers.
"""
import pytest

from agents import db
from agents import join_discovery as jd
from agents import resolve_evidence as rev

RUNS = [f"RUN-{i:05d}" for i in range(60)]
FIELDS = {
    "mes-ind": [{"table": "production_runs", "field": "run_number", "concept": "",
                 "role": "identifier", "data_type": "text"}],
    "erp": [{"table": "shipment_items", "field": "batch_id", "concept": "",
             "role": "identifier", "data_type": "text"}],
}


@pytest.fixture
def sweep(monkeypatch):
    """Two PostgreSQL systems whose key columns hold the same values, read
    through the real sampler (``resolve_evidence.sample_field_values``) with
    ``db.pg`` faked. ``state["fail"]``: how many sample reads fail next
    (``"raise"`` or ``"unavailable"``, as ``state["how"]`` says)."""
    state = {"fail": 0, "how": "raise", "reads": 0}
    emitted = []

    def pg(schema, sql, params=None, **_kw):
        state["reads"] += 1
        if state["fail"]:
            state["fail"] -= 1
            if state["how"] == "raise":
                raise ConnectionError("source reset")
            return {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
                    "error_category": "OperationalError"}
        return {"rows": [[v] for v in RUNS]}
    rev._reset_sample_memo()
    monkeypatch.setattr(db, "pg", pg)
    monkeypatch.setattr(jd, "_onboarded_systems", lambda: ["erp", "mes-ind"])
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: FIELDS[sid])
    monkeypatch.setattr(jd, "_timestamp_fields", lambda sid: [])
    monkeypatch.setattr(jd, "_system_sample_meta",
                        lambda sid: ("PostgreSQL", sid.replace("-", "_"), {}))
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: False)
    monkeypatch.setattr(jd, "_neptune_query", lambda cypher, params=None: [])
    monkeypatch.setattr(jd, "_log_proposed_mapping",
                        lambda **kw: emitted.append(kw) or f"id-{len(emitted)}")
    yield state, emitted
    rev._reset_sample_memo()


def _pairs(emitted):
    return {(e["from_system"], e["to_system"]) for e in emitted}


def test_a_healthy_strict_sweep_proposes_the_join_both_ways(sweep):
    _state, emitted = sweep
    out = jd.discover_joins_for_system("mes-ind", strict=True)
    assert len(out["joins"]) == 2 and out["incomplete"] is False
    assert _pairs(emitted) == {("mes-ind", "erp"), ("erp", "mes-ind")}


@pytest.mark.parametrize("how", ["raise", "unavailable"])
def test_a_transient_sample_failure_is_retried_not_measured_as_an_empty_column(sweep, how):
    """REPRODUCTION: one failed sample read lost the forward join with
    ``incomplete`` False, so the seed never retried. Now the direction
    raises, its retry reads the column, and the join is proposed."""
    state, emitted = sweep
    state.update(fail=1, how=how)
    out = jd.discover_joins_for_system("mes-ind", strict=True)
    assert _pairs(emitted) == {("mes-ind", "erp"), ("erp", "mes-ind")}
    assert out["incomplete"] is False and out["unswept"] == []


@pytest.mark.parametrize("how", ["raise", "unavailable"])
def test_a_source_that_stays_unreadable_reports_the_peer_unswept(sweep, how):
    state, emitted = sweep
    state.update(fail=10_000, how=how)
    out = jd.discover_joins_for_system("mes-ind", strict=True)
    assert out["incomplete"] is True and out["unswept"] == ["erp"]
    assert emitted == []


@pytest.mark.parametrize("how", ["raise", "unavailable"])
def test_a_non_strict_sweep_still_reads_a_failed_sample_as_empty(sweep, how):
    """Every attendee path (no ``strict``, no skip list): no raise, no
    ``incomplete`` key, the failed read measured as an empty column, as before."""
    state, emitted = sweep
    state.update(fail=10_000, how=how)
    out = jd.discover_joins_for_system("mes-ind")
    assert out == {"joins": [], "crosswalks": []} and emitted == []


def test_outside_a_strict_sweep_the_sampler_and_prober_answer_as_before(sweep):
    state, _emitted = sweep
    state.update(fail=2, how="raise")
    assert rev.sample_field_values(system_id="erp", connection_type="PostgreSQL",
                                   schema="erp", table="shipment_items",
                                   field="batch_id") == []
    assert rev.probe_field_values("erp", "PostgreSQL", "erp", "shipment_items", "batch_id",
                                  RUNS[:3]) is None


def test_inside_checked_reads_the_sampler_and_prober_raise_on_a_failed_read(sweep):
    state, _emitted = sweep
    with rev.checked_source_reads():
        state.update(fail=1, how="unavailable")
        with pytest.raises(RuntimeError, match="sample read failed"):
            rev.sample_field_values(system_id="erp", connection_type="PostgreSQL",
                                    schema="erp", table="shipment_items", field="batch_id")
        state.update(fail=1, how="unavailable")
        with pytest.raises(RuntimeError, match="probe read failed"):
            rev.probe_field_values("erp", "PostgreSQL", "erp", "shipment_items", "batch_id",
                                   RUNS[:3])
        state.update(fail=1, how="raise")
        with pytest.raises(ConnectionError):
            rev.probe_field_values("erp", "PostgreSQL", "erp", "shipment_items", "batch_id",
                                   RUNS[:3])
    assert rev._CHECKED_SOURCE_READS.get() is False


def test_a_checked_read_rereads_an_empty_sample_a_best_effort_read_memoized(sweep):
    """A best-effort read memoizes the empty answer of a failed read; a
    strict sweep in the same memo window reads the column again."""
    state, emitted = sweep
    state.update(fail=1, how="unavailable")
    assert rev.sample_field_values(system_id="erp", connection_type="PostgreSQL",
                                   schema="erp", table="shipment_items",
                                   field="batch_id") == []
    out = jd.discover_joins_for_system("mes-ind", strict=True)
    assert out["incomplete"] is False and len(emitted) == 2


def test_a_name_that_cannot_be_read_is_not_a_failed_read(sweep):
    """An invalid identifier means the column cannot be sampled or probed:
    an empty answer even in a checked read, as before."""
    state, _emitted = sweep
    with rev.checked_source_reads():
        assert rev.sample_field_values(system_id="erp", connection_type="PostgreSQL",
                                       schema="erp", table="bad table",
                                       field="batch_id") == []
        assert rev.probe_field_values("erp", "PostgreSQL", "erp", "bad table", "batch_id",
                                      RUNS[:3]) is None
    assert state["reads"] == 0


def test_a_strict_sweep_reports_a_failed_containment_probe(monkeypatch, sweep):
    """The ladder finds nothing on disjoint samples of a same-named key, so
    the containment probe decides; its failed read fails the direction."""
    state, _emitted = sweep
    fields = {sid: [{"table": "t", "field": "work_order_id", "concept": "",
                     "role": "identifier", "data_type": "text"}] for sid in ("erp", "mes-ind")}
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: fields[sid])
    monkeypatch.setattr(jd, "_sample", lambda sid, t, f: [f"{sid}-{i}" for i in range(60)])
    state.update(fail=10_000, how="unavailable")
    out = jd.discover_joins_for_system("mes-ind", strict=True)
    assert out["incomplete"] is True and out["unswept"] == ["erp"]
    assert jd.discover_joins_for_system("mes-ind") == {"joins": [], "crosswalks": []}


# ---------------------------------------------------------------------------
# Every other reader the strict sweep reaches: SiteWise, Athena, Tulip
# ---------------------------------------------------------------------------
SW_ROWS = [{"name": v} for v in RUNS]


def _graph_down(*_a, **_kw):
    raise db.GraphQueryError("graph down")


@pytest.fixture
def sitewise_sweep(monkeypatch, sweep):
    """``erp`` is a SiteWise (Direct) system: its key is the hierarchy's
    ``asset_id``, sampled from the graph. ``graph["up"]`` False: the fail-soft
    read answers ``[]`` and the checked one raises."""
    state, emitted = sweep
    graph = {"up": True}
    fields = {"mes-ind": FIELDS["mes-ind"],
              "erp": [{"table": "asset_hierarchy", "field": "asset_id", "concept": "",
                       "role": "identifier", "data_type": "text"}]}
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: fields[sid])
    monkeypatch.setattr(jd, "_system_sample_meta", lambda sid: (
        ("SiteWise (Direct)", "", {}) if sid == "erp" else ("PostgreSQL", "mes_ind", {})))
    monkeypatch.setattr(db, "neptune_query",
                        lambda cypher, params=None: SW_ROWS if graph["up"] else [])
    monkeypatch.setattr(db, "neptune_query_checked",
                        lambda cypher, params=None: SW_ROWS if graph["up"] else _graph_down())
    return graph, emitted


def test_a_healthy_sitewise_peer_is_swept_both_ways(sitewise_sweep):
    _graph, emitted = sitewise_sweep
    out = jd.discover_joins_for_system("mes-ind", strict=True)
    assert out["incomplete"] is False and _pairs(emitted) == {("mes-ind", "erp"),
                                                              ("erp", "mes-ind")}


def test_a_failed_sitewise_graph_read_reports_the_peer_unswept(sitewise_sweep):
    """REPRODUCTION: the SiteWise sampler read the graph fail-soft, so a
    failed read was an empty projection and the strict sweep said complete."""
    graph, emitted = sitewise_sweep
    graph["up"] = False
    out = jd.discover_joins_for_system("mes-ind", strict=True)
    assert out["incomplete"] is True and out["unswept"] == ["erp"] and emitted == []


def test_a_non_strict_sweep_reads_a_failed_sitewise_graph_read_as_empty(sitewise_sweep):
    graph, emitted = sitewise_sweep
    graph["up"] = False
    assert jd.discover_joins_for_system("mes-ind") == {"joins": [], "crosswalks": []}
    assert emitted == []


def _both_ways(read, empty):
    """*read* answers *empty* outside a checked read and raises inside one."""
    assert read() == empty
    with rev.checked_source_reads(), pytest.raises(Exception):
        read()


def test_an_athena_system_whose_workgroup_cannot_be_resolved(monkeypatch):
    monkeypatch.setattr(rev, "_with_cross_account_credentials", lambda sid, cfg: dict(cfg))
    _both_ways(lambda: rev._sample_athena_source("hist", {}, "t", "f", 10), [])


def test_an_athena_error_answer(monkeypatch):
    from agents import athena_tools
    monkeypatch.setattr(athena_tools, "_resolve_config", lambda cfg: cfg)
    monkeypatch.setattr(athena_tools, "run_athena_query", lambda sql, cfg, max_rows=0: {
        "error": "query_unavailable", "error_type": "tool_error"})
    _both_ways(lambda: rev._sample_athena({"workgroup": "wg"}, "t", "f", 10), [])


def test_a_tulip_connection_that_cannot_be_resolved(monkeypatch):
    monkeypatch.setattr(rev, "_tulip_connection_config", lambda sid, cfg: {})
    _both_ways(lambda: rev._sample_tulip_records("tulip", {}, 10, field="c", table="T"), [])
    _both_ways(lambda: rev._probe_tulip_records("tulip", "T", "c", ["a"]), None)


def test_a_tulip_probe_whose_read_fails(monkeypatch):
    monkeypatch.setattr(rev, "_tulip_connection_config",
                        lambda sid, cfg: {"base_url": "https://tulip.example"})
    monkeypatch.setattr(rev, "_tulip_table_id_for", lambda sid, table, cfg=None: "tid")

    def down(*_a, **_kw):
        raise ConnectionError("reset")
    monkeypatch.setattr(rev, "_tulip_probe_reads", down)
    _both_ways(lambda: rev._probe_tulip_records("tulip", "T", "c", ["a"]), None)


@pytest.mark.parametrize("kind, listed", [(rev.SEAT_UNAVAILABLE, {"T": "tid"}),
                                          (rev.INLINE, None)])
def test_a_tulip_table_list_or_seat_that_cannot_be_read(monkeypatch, kind, listed):
    monkeypatch.setattr(rev, "tulip_seat_state",
                        lambda sid, cfg: rev.TulipSeatState(kind))
    monkeypatch.setattr(rev, "_tulip_list_identity", lambda sid, cfg, state: ("k", kind))
    monkeypatch.setattr(rev, "tulip_name_id_map", lambda sid, cfg, seat_request=None: listed)
    _both_ways(lambda: rev._tulip_name_id_map_get("tulip", {"base_url": "https://t"}), {})
