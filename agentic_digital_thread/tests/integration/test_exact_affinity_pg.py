# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The exact-agreement pass on the REAL seeded data (ADR-0087, PG lane).

After db-init (site MES views + ``erp_data``) and the live-story history,
every text column of every site MES view schema and of the ERP view schema is
sampled by the engine's own sampler and probed by the engine's own membership
probe; the pass then yields EXACTLY two true joins, each in both resolve
directions: the run-number hop (the MES run -> the ERP shipment line it
shipped on) and the work-order lot hop (the MES work-order number -> the ERP
``lot_mes_tracking.batch_id`` that holds it). The frozen story (plant MES seed
+ PLM, no history) gains no exact rule, and the raw-sample
``discover_transform`` identity rung now clears too.
With the concept gate removed (every field one concept) exactly the same TWO
candidate pairs exist, so the concept gate now holds back nothing true.
The probe is the real seam, ``onboard._affinity_probe``.
The widened run view (D18: sku, planned quantity, status, times, line appended)
adds no qualifying pair, and replaces the original 2-column view in place.

Concepts are test-supplied and model the LIVE onboarding assignment for the
WIDENED run view (one-clock walk step 5, live graph 2026-09-30): the run
number MAPS_TO ``production-run`` ONLY (identifier, 0.95; the ``batch``
correlate the 2-column view drew is gone); the ERP ``batch_id``/``batch_number``
columns ``batch``; ``work-order`` on the work-order columns; the column name
elsewhere (so same-named UUID columns share one). No concept is shared, so
both hops are derived through catalog-related identifier pairs
(``catalog.RELATED_IDENTIFIER_CONCEPTS``: ``production-run`` ~ ``batch`` and
``work-order`` ~ ``batch``). The two ``production-order`` pairs (``~
production-run`` and ``~ batch``) are measured on the same data: a column of
run numbers mapped ``production-order`` joins the MES run number and the ERP
shipment line's ``batch_id``, the two hops keep the name ``batch``, and across
every seeded system the concept-neutral candidates pair no order column with a
run or batch column.

Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_exact_affinity_pg.py -q
"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents import identity_affinity as aff  # noqa: E402
from agents.demo import systems, workshop  # noqa: E402
from agents.join_discovery import JOIN_MIN_OVERLAP_COUNT, discover_transform  # noqa: E402
from tests.integration import story_pg_support as support  # noqa: E402
from tests.integration._pg_sql import stmt  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_CONCEPTS = {"run_number": "production-run", "batch_id": "batch", "batch_number": "batch",
             "work_order_number": "work-order", "work_order_id": "work-order",
             "work_order_ref": "work-order"}
#: Every concept a column MAPS_TO (with its confidence), when known: the live
#: widened-view assignment maps the run number to ``production-run`` alone.
_CONCEPT_SETS = {"run_number": (("production-run", 0.95),)}
_LIVE_MES = workshop.LIVE_MES_SYSTEM["id"]
_RUN = ("production_runs", "run_number")
_SHIP = ("shipment_items", "batch_id")
_WO = ("work_orders", "work_order_number")
_LOT_TRACK = ("lot_mes_tracking", "batch_id")


def _schema(entries, system_id):
    (entry,) = [e for e in entries if e["id"] == system_id]
    return entry["schema"]


def _live_systems() -> dict:
    """system id -> view schema: the ERP view schema and every site MES."""
    out = {"erp": _schema(systems.DEMO_SYSTEMS, "erp"),
           _LIVE_MES: workshop.LIVE_MES_SYSTEM["schema"]}
    out.update({s["id"]: s["schema"] for s in systems.DEMO_SYSTEMS if s.get("system_type") == "MES"})
    return out


def _frozen_systems() -> dict:
    return {"erp": _schema(systems.DEMO_SYSTEMS, "erp"),
            _LIVE_MES: _schema(workshop.WORKSHOP_SEEDED_SYSTEMS, _LIVE_MES),
            "plm": _schema(systems.DEMO_SYSTEMS, "plm")}


def _sampled(sid: str, schema: str, table: str, column: str):
    field = support.sampled(sid, schema, table, column, _CONCEPTS.get(column, column))
    scored = _CONCEPT_SETS.get(column, ())
    return dataclasses.replace(field, concepts=frozenset(c for c, _ in scored),
                               concept_confidence=scored)


def _fields(pg, schema_of: dict) -> dict:
    return {sid: [_sampled(sid, schema, t, c) for t, c in support.text_columns(pg, schema)]
            for sid, schema in schema_of.items()}


def _rules(fields: dict, probe) -> list[dict]:
    """Every rule each system's resolve would find against all the others."""
    out = []
    for sid, own in fields.items():
        others = [f for other, fs in fields.items() if other != sid for f in fs]
        out += [{**r, "source_system": sid} for r in aff.find_affinity_rules(own, others, probe=probe)]
    return out


def _exact(rules: list[dict]) -> set:
    return {(r["source_system"], r["source_field"], r["target_system"], r["target_field"])
            for r in rules if r["method"] == aff.EXACT_METHOD}


@pytest.fixture(scope="module")
def live_db():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "exl") as (conns, plan):
        yield conns, plan


@pytest.fixture(scope="module")
def frozen_db():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")
    with support.story_databases(_DSN, "exf", with_history=False) as (conns, _plan):
        yield conns


def _routed(monkeypatch, conns):
    import agents.db as db
    pg = support.routed_pg(conns)
    monkeypatch.setattr(db, "pg", pg)
    return pg


def test_the_live_story_derives_exactly_the_two_true_hops(live_db, monkeypatch, capsys):
    """The run-number hop (MES run -> ERP shipment line) and the work-order
    lot hop (MES work order -> ERP lot-tracking row, whose ``batch_id`` holds
    the work-order number), each seen from both resolves, and nothing else."""
    conns, plan = live_db
    pg = _routed(monkeypatch, conns)
    schema_of = _live_systems()
    fields = _fields(pg, schema_of)
    rules = _rules(fields, support.engine_probe(monkeypatch, schema_of))
    found = _exact(rules)
    with capsys.disabled():
        print("\nqualifying exact-agreement pairs:", sorted(found))
    hop = (_LIVE_MES, ".".join(_RUN), "erp", ".".join(_SHIP))
    reverse = ("erp", ".".join(_SHIP), _LIVE_MES, ".".join(_RUN))
    wo_hop = (_LIVE_MES, ".".join(_WO), "erp", ".".join(_LOT_TRACK))
    wo_reverse = ("erp", ".".join(_LOT_TRACK), _LIVE_MES, ".".join(_WO))
    assert found == {hop, reverse, wo_hop, wo_reverse}
    (wo_rule,) = [r for r in rules if (r["source_system"], r["source_field"]) == wo_hop[:2]
                  and r["method"] == aff.EXACT_METHOD]
    assert wo_rule["concept"] == "batch"   # declaring concept of the work-order ~ batch pair
    assert "catalog-related work-order" in wo_rule["reasoning"]
    assert wo_rule["shared_count"] >= JOIN_MIN_OVERLAP_COUNT
    # A true join: every shared value is a work order the MES holds.
    tracked = {row[0] for row in pg(schema_of["erp"], "SELECT DISTINCT batch_id FROM lot_mes_tracking")["rows"]}
    numbers = {row[0] for row in pg(schema_of[_LIVE_MES], "SELECT work_order_number FROM work_orders")["rows"]}
    assert tracked and tracked <= numbers
    (rule,) = [r for r in rules if (r["source_system"], r["source_field"]) == hop[:2]
               and r["method"] == aff.EXACT_METHOD]
    shipped = len(plan.runs) + 1
    assert rule["shared_count"] == shipped == 19
    assert (rule["source_pattern"], rule["target_pattern"], rule["transform"]) == ("", "", "identity")
    assert f"{shipped} exact values" in rule["reasoning"] and "concept batch" in rule["reasoning"]
    assert rule["concept"] == "batch"      # the catalog's DECLARING concept, not production-run
    assert "catalog-related production-run" in rule["reasoning"]
    # A true join: every shared value is a run the history made AND shipped.
    runs = {r.run_number for r in (*plan.runs, plan.failed_run)}
    batch_ids = {row[0] for row in pg(schema_of["erp"], "SELECT DISTINCT batch_id FROM shipment_items")["rows"]}
    assert runs == batch_ids


_NEUTRAL = "any-concept"
_WORK_ORDER_HOP = frozenset({("erp", "lot_mes_tracking.batch_id"),
                             (_LIVE_MES, "work_orders.work_order_number")})


def test_concept_neutral_candidates_are_exactly_two(live_db, monkeypatch, capsys):
    """Every gate EXCEPT the concept gate (all fields share one concept): the
    demo data holds exactly two exact-agreement candidates. The run-number hop,
    and ``lot_mes_tracking.batch_id`` <-> the MES work-order number — the same
    two true joins the concept gate now passes through its related pairs
    (``batch`` ~ ``production-run`` and ``batch`` ~ ``work-order``)."""
    conns, _plan = live_db
    pg = _routed(monkeypatch, conns)
    schema_of = _live_systems()
    fields = [dataclasses.replace(f, concept=_NEUTRAL, concepts=frozenset(), concept_confidence=())
              for fs in _fields(pg, schema_of).values() for f in fs]
    probe = support.engine_probe(monkeypatch, schema_of)
    found = {frozenset({(s.system_id, s.qualified), (t.system_id, t.qualified)})
             for s in fields for t in fields if aff.exact_rule(s, t, probe)}
    with capsys.disabled():
        print("\nconcept-neutral exact-agreement candidates:", sorted(map(sorted, found)))
    run_hop = frozenset({(_LIVE_MES, ".".join(_RUN)), ("erp", ".".join(_SHIP))})
    assert found == {run_hop, _WORK_ORDER_HOP}


def _seeded_pg_systems(pg) -> dict:
    """system id -> schema for every PostgreSQL system the live story seeds or
    registers whose schema this harness builds: the ERP, the four plant MES,
    PLM and the plant maintenance system (the corporate CMMS schema is not
    built here)."""
    entries = [*systems.DEMO_SYSTEMS, workshop.LIVE_MES_SYSTEM, *workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS]
    out = {e["id"]: e["schema"] for e in entries
           if e.get("connection_type") == "PostgreSQL" and e.get("schema")}
    built = {row[0] for row in pg("erp_data", "SELECT DISTINCT table_schema FROM information_schema.columns")["rows"]}
    built |= {row[0] for row in pg("plm_engineering",
                                   "SELECT DISTINCT table_schema FROM information_schema.columns")["rows"]}
    return {sid: schema for sid, schema in out.items() if schema in built}


def test_concept_neutral_candidates_across_every_seeded_system(live_db, monkeypatch):
    """The bound on what ANY concept assignment or catalog relation can add:
    with every field one concept, the pairs that pass every measured gate
    across all seeded PostgreSQL systems are the two batch hops and the ERP
    part number in PLM's item columns. None pairs a production order column
    with a run or batch column, so the two ``production-order`` pairs
    (ADR-0087 amendment 2026-10-21) add no exact rule on this data unless the
    mapper tags one of the batch-hop columns ``production-order``."""
    conns, _plan = live_db
    pg = _routed(monkeypatch, conns)
    schema_of = _seeded_pg_systems(pg)
    assert {"erp", _LIVE_MES, "mes-mil", "mes-det", "mes-cr", "plm", "cmms-ind"} <= set(schema_of)
    fields = [dataclasses.replace(f, concept=_NEUTRAL, concepts=frozenset(), concept_confidence=())
              for fs in _fields(pg, schema_of).values() for f in fs]
    probe = support.engine_probe(monkeypatch, schema_of)
    found = {frozenset({(s.system_id, s.qualified), (t.system_id, t.qualified)})
             for s in fields for t in fields if s.system_id < t.system_id and aff.exact_rule(s, t, probe)}
    part = ("erp", "products.engineering_part_number")
    plm_items = {frozenset({part, ("plm", col)})
                 for col in ("products.item_id", "bom_items.parent_item", "specifications.item_id")}
    run_hop = frozenset({(_LIVE_MES, ".".join(_RUN)), ("erp", ".".join(_SHIP))})
    assert found == {run_hop, _WORK_ORDER_HOP} | plm_items


def _retagged(fields: dict, system_id: str, qualified: str, concept: str):
    (field,) = [f for f in fields[system_id] if f.qualified == qualified]
    return dataclasses.replace(field, concept=concept, concepts=frozenset({concept}),
                               concept_confidence=((concept, 0.95),))


def test_a_production_order_column_joins_the_run_numbers_it_holds(live_db, monkeypatch):
    """A column of the ERP's run numbers tagged ``production-order`` (as the
    Tulip "ERP Order ID" was) agrees with the MES run number
    (``production-run``) through the catalog relation, on the real data and
    the engine's own probe, and is named the declaring concept. Without the
    relation the same pair is refused on concept."""
    conns, plan = live_db
    pg = _routed(monkeypatch, conns)
    schema_of = _live_systems()
    fields = _fields(pg, schema_of)
    order = _retagged(fields, "erp", ".".join(_SHIP), "production-order")
    run = _retagged(fields, _LIVE_MES, ".".join(_RUN), "production-run")
    probe = support.engine_probe(monkeypatch, schema_of)
    for source, target in ((run, order), (order, run)):
        rule, outcome = aff.exact_verdict(source, target, probe)
        assert outcome == aff.EXACT_EMITTED
        assert rule["concept"] == "production-order"
        assert rule["shared_count"] == len(plan.runs) + 1 == 19
        assert "catalog-related production-run" in rule["reasoning"]
    monkeypatch.setattr(aff, "RELATED_IDENTIFIER_CONCEPTS",
                        frozenset({("production-run", "batch"), ("work-order", "batch")}))
    assert aff.exact_verdict(run, order, probe)[1] == aff.EXACT_REFUSED_CONCEPT


def test_a_production_order_column_joins_the_erp_batch_column_holding_its_values(live_db, monkeypatch):
    """The Tulip shape on the real data: run numbers in a column mapped
    ``production-order`` (as the Tulip "ERP Order ID" was) against the ERP
    shipment line's ``batch_id`` (``batch``, the only ERP column holding run
    numbers on every recorded graph) agree through the ``production-order`` ~
    ``batch`` pair, from either side, and are named ``batch``. Without that
    pair the same columns are refused on concept."""
    conns, plan = live_db
    pg = _routed(monkeypatch, conns)
    schema_of = _live_systems()
    fields = _fields(pg, schema_of)
    order = _retagged(fields, _LIVE_MES, ".".join(_RUN), "production-order")
    ship = _retagged(fields, "erp", ".".join(_SHIP), "batch")
    probe = support.engine_probe(monkeypatch, schema_of)
    for source, target in ((order, ship), (ship, order)):
        rule, outcome = aff.exact_verdict(source, target, probe)
        assert outcome == aff.EXACT_EMITTED
        assert rule["concept"] == "batch"
        assert rule["shared_count"] == len(plan.runs) + 1 == 19
        assert "concept batch (catalog-related production-order)" in rule["reasoning"]
    monkeypatch.setattr(aff, "RELATED_IDENTIFIER_CONCEPTS",
                        aff.RELATED_IDENTIFIER_CONCEPTS - {("production-order", "batch")})
    assert aff.exact_verdict(order, ship, probe)[1] == aff.EXACT_REFUSED_CONCEPT


def test_the_live_hops_keep_their_names_under_every_related_pair(live_db, monkeypatch):
    """The modeled live assignment derives the same four directional batch-hop
    rules, every one named ``batch``, under the relation as it stood with two
    pairs, with three, and as it is on disk."""
    conns, _plan = live_db
    pg = _routed(monkeypatch, conns)
    schema_of = _live_systems()
    fields = _fields(pg, schema_of)
    probe = support.engine_probe(monkeypatch, schema_of)
    on_disk = aff.RELATED_IDENTIFIER_CONCEPTS
    two = frozenset({("production-run", "batch"), ("work-order", "batch")})
    seen = []
    for relation in (two, two | {("production-run", "production-order")}, on_disk):
        monkeypatch.setattr(aff, "RELATED_IDENTIFIER_CONCEPTS", relation)
        seen.append({(r["source_system"], r["source_field"], r["target_system"], r["target_field"],
                      r["concept"]) for r in _rules(fields, probe) if r["method"] == aff.EXACT_METHOD})
    assert seen[0] == seen[1] == seen[2]
    assert len(seen[2]) == 4 and {r[-1] for r in seen[2]} == {"batch"}


def test_the_work_order_batch_column_never_qualifies(live_db, monkeypatch):
    """``lot_mes_tracking.batch_id`` holds MES work-order numbers: same
    concept, different shape and no shared value — never a run-number join."""
    conns, _plan = live_db
    pg = _routed(monkeypatch, conns)
    schema_of = _live_systems()
    fields = _fields(pg, schema_of)
    rules = _rules(fields, support.engine_probe(monkeypatch, schema_of))
    assert not [r for r in rules if r["target_field"] == "lot_mes_tracking.batch_id"
                and r["source_field"] == ".".join(_RUN)]


def test_raw_sample_discover_transform_now_clears(live_db, monkeypatch):
    conns, _plan = live_db
    _routed(monkeypatch, conns)
    schema_of = _live_systems()
    run = support.sampled(_LIVE_MES, schema_of[_LIVE_MES], *_RUN, "batch")
    ship = support.sampled("erp", schema_of["erp"], *_SHIP, "batch")
    found = discover_transform(run.values, ship.values)
    assert found["transform"] == "identity"
    assert found["overlap_count"] >= JOIN_MIN_OVERLAP_COUNT and found["ratio"] == 1.0


def test_the_frozen_story_gains_no_exact_rule(frozen_db, monkeypatch):
    """B-2847-era data (plant MES seed, PLM, no history): no exact rule, and
    the probe changes nothing (every rule byte-identical to the probe-less pass)."""
    pg = _routed(monkeypatch, frozen_db)
    schema_of = _frozen_systems()
    fields = _fields(pg, schema_of)
    probed = _rules(fields, support.engine_probe(monkeypatch, schema_of))
    assert _exact(probed) == set()
    unprobed = []
    for sid, own in fields.items():
        others = [f for other, fs in fields.items() if other != sid for f in fs]
        unprobed += [{**r, "source_system": sid} for r in aff.find_affinity_rules(own, others)]
    assert probed == unprobed


#: The columns D18 appends to every site MES ``production_runs`` view.
_WIDENED_TEXT = ("sku", "status", "line")


def test_the_exact_pass_counters_on_the_widened_view(live_db, monkeypatch, capsys):
    """The run-number hop and the work-order lot hop (each in both resolve
    directions) are the ONLY qualifying pairs and the refusal counters are
    recorded (counts, never values)."""
    conns, _plan = live_db
    pg = _routed(monkeypatch, conns)
    schema_of = _live_systems()
    fields = _fields(pg, schema_of)
    probe = support.engine_probe(monkeypatch, schema_of)
    stats = aff.ExactPassStats()
    for sid, own in fields.items():
        others = [f for other, fs in fields.items() if other != sid for f in fs]
        aff.find_affinity_rules(own, others, probe=probe, stats=stats)
    with capsys.disabled():
        print("\n" + stats.summary())
    assert stats.qualified == stats.emitted == 4
    assert stats.considered == (stats.refused_concept + stats.refused_shape
                                + stats.refused_probe + stats.qualified)
    assert min(stats.refused_concept, stats.refused_shape, stats.refused_probe) > 0


def _run_column_verdicts(pg, monkeypatch, schema_of, column):
    """The live MES run-view *column*, the membership probe, and each ERP
    text field -> (field, the engine's own exact-pass outcome)."""
    fields = _fields(pg, {_LIVE_MES: schema_of[_LIVE_MES], "erp": schema_of["erp"]})
    (source,) = [f for f in fields[_LIVE_MES] if f.qualified == f"production_runs.{column}"]
    probe = support.engine_probe(monkeypatch, schema_of)
    verdicts = {t.qualified: (t, aff.exact_verdict(source, t, probe)[1]) for t in fields["erp"]}
    return source, probe, verdicts


@pytest.mark.parametrize("column", _WIDENED_TEXT)
def test_no_widened_run_column_qualifies(live_db, monkeypatch, column):
    """No appended column qualifies against any ERP field (D18). Every
    eligible concept-sharing pair is refused on shape or by the probe: status
    and line hold single-segment values; the part catalog's dominant sku shape
    is not the machined one, and even past shape the parts the runs build
    share fewer than JOIN_MIN_OVERLAP_COUNT values with it."""
    conns, _plan = live_db
    pg = _routed(monkeypatch, conns)
    source, probe, verdicts = _run_column_verdicts(pg, monkeypatch, _live_systems(), column)
    outcomes = {t: v for t, (_f, v) in verdicts.items()
                if v not in (aff.EXACT_REFUSED_CONCEPT, "ineligible")}
    assert source.values and outcomes
    assert set(outcomes.values()) <= {aff.EXACT_REFUSED_SHAPE, aff.EXACT_REFUSED_PROBE}
    if column == "sku":
        assert len(set(source.values)) < JOIN_MIN_OVERLAP_COUNT
        part_catalog = verdicts["products.sku"][0]
        assert len(aff.exact_shared(source, part_catalog, probe)) < JOIN_MIN_OVERLAP_COUNT
    else:
        assert set(outcomes.values()) == {aff.EXACT_REFUSED_SHAPE}


def _view_columns(cur, schema):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name = 'production_runs' ORDER BY ordinal_position", (schema,))
    return [r[0] for r in cur.fetchall()]


def _old_runs_view_sql(schema, site_code):
    """The pre-D18 2-column view, verbatim (what a running account holds)."""
    from agents.pgdriver import sql

    return stmt("CREATE OR REPLACE VIEW {schema}.production_runs AS SELECT "
                "pr.run_id AS production_run_id, pr.run_number FROM public.production_runs pr "
                "WHERE pr.run_id IN (SELECT w.production_run_id FROM public.mes_work_orders w "
                "WHERE w.site = {site_code});\n", schema=schema, site_code=sql.Literal(site_code))


def test_the_widened_view_replaces_the_old_two_column_view(live_db):
    """A running account holds the 2-column view: the admin migration's batch
    (``CREATE OR REPLACE``) replaces it in place, appending the new columns."""
    from agents.demo import site_views
    conns, _plan = live_db
    conn = conns["erp"]
    try:
        with conn.cursor() as cur:
            for schema, site_code, _prefix in site_views.SITE_MES_VIEWS:
                cur.execute(stmt("DROP VIEW {schema}.production_runs", schema=schema))
                cur.execute(_old_runs_view_sql(schema, site_code))
                assert _view_columns(cur, schema) == ["production_run_id", "run_number"]
            cur.execute(site_views.erp_mes_views_sql())
            for schema, _site_code, _prefix in site_views.SITE_MES_VIEWS:
                assert _view_columns(cur, schema) == [
                    "production_run_id", "run_number", "sku", "planned_quantity", "status",
                    "actual_start_time", "actual_end_time", "line"]
    finally:
        conn.rollback()
