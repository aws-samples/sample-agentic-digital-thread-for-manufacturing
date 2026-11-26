# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Real-Postgres integration test for the sensor-heatmap resolver (S5.3 §6.3).

Closes the end-to-end gap left by ``tests/unit/test_heatmap_resolver.py``: that
suite verifies SQL *composition* with a **mocked** ``query_pg``, so the composed
``DISTINCT ON (d1, d2)`` LATEST grid and the threshold JOIN have NEVER executed
against a real database. Here ``query_pg`` is left **REAL** and the resolver's
composed SQL runs against the live local Postgres (schema ``iot_historian``,
table ``sensor_readings`` — real seeded sensor data), doubling ONLY the
graph-metadata seams (authority / measure mapping / schema / dimension mappings
/ order column / join path) that would otherwise need a live Neptune graph.

Gating: reuses the ``local_postgres`` fixture (``tests/integration/conftest.py``),
so the suite SKIPS cleanly when no local Postgres is reachable and RUNS when one
is — exactly like ``test_resolver_integration.py``.

Seams DOUBLED (graph metadata only):
  * ``mr._resolve_authority``        -> AuthoritySource(system_id='iot-historian')
  * ``mr._resolve_table_column``     -> MappingProvenance(sensor_readings.value, measure)
  * ``mr.get_schema``                -> 'iot_historian'
  * ``md._resolve_dimension_mappings`` -> per-concept MappingProvenance (co-located)
  * ``md._resolve_order_column``     -> 'timestamp'  (the event-time order column)
  * ``md._select_join_path``         -> JoinPathProvenance (threshold join, TEST 2)

Seams left REAL (the integration under test):
  * ``mr.query_pg`` (live local Postgres via ``agents.db.get_conn_params``),
    ``_compose_heatmap_sql``, ``_execute_heatmap``, ``_severity``.
"""

from __future__ import annotations

import pytest

from agents import pgdriver

import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.heatmap_resolver import _severity, resolve_heatmap
from agents.metric_dimension import JoinPathProvenance
from agents.metric_resolver import (
    AuthorityResolver,
    AuthoritySource,
    MappingProvenance,
)

pytestmark = pytest.mark.integration

# --- The real local PG target (matches the seeded iot_historian sensor data) --
SYSTEM_ID = "iot-historian"
SCHEMA = "iot_historian"
MEASURE_TABLE = "sensor_readings"
MEASURE_COLUMN = "value"
ORDER_COLUMN = "timestamp"
EQUIP_COLUMN = "equipment_id"
TYPE_COLUMN = "measurement_type"
THRESHOLD_TABLE = "sensor_thresholds_itest"  # temp table created/dropped by fixture
THRESHOLD_COLUMN = "limit_value"

# Metric + dimension/threshold concepts (the metric is real; the concept->field
# mappings are the doubled graph seams).
METRIC_ID = "sensor-reading-latest"          # DirectMetric, Aggregation.LATEST
DIM_X = "equipment-id"
DIM_Y = "sensor-type"
THRESHOLD_CONCEPT = "sensor-threshold"

# Seeded threshold limits chosen against the real latest-value spread so the
# server-side severity classifier produces a MIX of ok/warning/critical:
#   temperature latest values span ~43.9..71.1  -> limit 60  (ok/warning/critical)
#   pressure    latest values span ~4.08..5.54  -> limit 5.0 (ok + critical)
SEEDED_LIMITS = {"temperature": 60.0, "pressure": 5.0}

# Co-located dimension mappings (both dims live on the measure table).
_DIMS_X = [MappingProvenance(table=MEASURE_TABLE, column=EQUIP_COLUMN,
                             role="dimension", confidence=1.0, source="itest")]
_DIMS_Y = [MappingProvenance(table=MEASURE_TABLE, column=TYPE_COLUMN,
                             role="dimension", confidence=1.0, source="itest")]


class _FakeAuthority(AuthorityResolver):
    """Resolves every concept to the iot-historian system (no live graph)."""

    def authoritative_system(self, concept):
        return SYSTEM_ID


def _patch_common_seams(monkeypatch, *, dim_maps):
    """Double ONLY the graph-metadata seams; leave query_pg (real PG) untouched."""
    monkeypatch.setattr(
        mr, "_resolve_authority",
        lambda concept, authority=None: AuthoritySource(
            system_id=SYSTEM_ID, source="authority_rule"),
    )
    monkeypatch.setattr(
        mr, "_resolve_table_column",
        lambda system_id, concept: MappingProvenance(
            table=MEASURE_TABLE, column=MEASURE_COLUMN, role="measure",
            confidence=1.0, source="itest"),
    )
    monkeypatch.setattr(mr, "get_schema", lambda system_id: SCHEMA)
    monkeypatch.setattr(
        md, "_resolve_order_column",
        lambda system_id, measure_table, *, query=None: ORDER_COLUMN,
    )
    monkeypatch.setattr(
        md, "_resolve_dimension_mappings",
        lambda system_id, concept, *, query=None: list(dim_maps.get(concept, [])),
    )


def _read(params, sql, args=None):
    """Run an independent READ-ONLY control query directly on the same local PG."""
    conn = pgdriver.connect(**params)
    conn.set_session(readonly=True, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture
def seeded_thresholds(local_postgres):
    """Create a small REAL threshold table for the JOIN, drop it in teardown.

    Leaves the database exactly as found (idempotent DROP both before and after).
    """
    params = local_postgres
    create = (  # nosec B608 - all identifiers are module constants, no user input
        f'CREATE TABLE {SCHEMA}.{THRESHOLD_TABLE} '
        f'({TYPE_COLUMN} varchar PRIMARY KEY, {THRESHOLD_COLUMN} numeric)'
    )
    insert = (  # nosec B608 - identifiers are module constants; values bound
        f'INSERT INTO {SCHEMA}.{THRESHOLD_TABLE} '  # nosec B608 - test fixture composing expected SQL from module constants
        f'({TYPE_COLUMN}, {THRESHOLD_COLUMN}) VALUES (%s, %s)'
    )
    drop = f"DROP TABLE IF EXISTS {SCHEMA}.{THRESHOLD_TABLE}"  # nosec B608

    conn = pgdriver.connect(**params)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(drop)
            cur.execute(create)
            for mtype, limit in SEEDED_LIMITS.items():
                cur.execute(insert, (mtype, limit))
    finally:
        conn.close()

    yield SEEDED_LIMITS

    conn = pgdriver.connect(**params)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(drop)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TEST 1 — LATEST 2-dim, uncovered (no threshold), REAL execution + cross-check
# ---------------------------------------------------------------------------

def test_heatmap_latest_uncovered_real_pg(local_postgres, monkeypatch):
    """The DISTINCT ON (d1, d2) LATEST grid executes on real PG and is CORRECT.

    Doubles only the graph seams; ``query_pg`` runs the composed SQL against the
    live ``iot_historian.sensor_readings``. Asserts the uncovered envelope shape,
    then CROSS-CHECKS each sampled cell's value against an independent control
    query (the true latest reading at that cell) — proving ``DISTINCT ON`` picks
    the actual most-recent row per (equipment_id, measurement_type), not an
    arbitrary one.
    """
    _patch_common_seams(monkeypatch, dim_maps={DIM_X: _DIMS_X, DIM_Y: _DIMS_Y})

    env = resolve_heatmap(METRIC_ID, DIM_X, DIM_Y, threshold_concept=None,
                          authority=_FakeAuthority())

    assert env["status"] == "ok"
    assert env["kind"] == "heatmap"
    assert env["threshold_status"] == "uncovered"
    assert env["dims"] == [DIM_X, DIM_Y]

    rows = env["rows"]
    assert rows, "resolver returned NO cells from real PG (expected ~34)"

    # Every cell is uncovered (no threshold requested) — never a fabricated limit.
    assert all(cell[3] is None for cell in rows), "expected null threshold per cell"
    assert all(cell[4] == "uncovered" for cell in rows), "expected uncovered severity"

    # Capture the SQL that actually executed (recorded verbatim in per-cell lineage).
    sql = env["lineage"][0]["sql"]
    print("\n[TEST 1] executed SQL:\n" + sql)
    assert "DISTINCT ON" in sql
    assert "JOIN" not in sql                  # uncovered → bare measure table
    assert ORDER_COLUMN in sql                # ordered by the real time column

    # CROSS-CHECK: resolver value == true latest value at that cell (independent).
    params = local_postgres
    control_sql = (  # nosec B608 - identifiers are module constants; values bound
        f"SELECT {MEASURE_COLUMN} FROM {SCHEMA}.{MEASURE_TABLE} "  # nosec B608 - test fixture composing expected SQL from module constants
        f"WHERE {EQUIP_COLUMN} = %s AND {TYPE_COLUMN} = %s AND {ORDER_COLUMN} = "
        f"(SELECT max({ORDER_COLUMN}) FROM {SCHEMA}.{MEASURE_TABLE} "
        f" WHERE {EQUIP_COLUMN} = %s AND {TYPE_COLUMN} = %s)"
    )
    checked = 0
    for equip, mtype, value, _thr, _sev in rows[:12]:
        latest_vals = {float(r[0]) for r in
                       _read(params, control_sql, (equip, mtype, equip, mtype))}
        assert latest_vals, f"control query found no rows for ({equip}, {mtype})"
        assert float(value) in latest_vals, (
            f"resolver value {value!r} for cell ({equip}, {mtype}) is not the "
            f"true latest {latest_vals!r} — DISTINCT ON picked the wrong row"
        )
        checked += 1
    assert checked >= 1, "no cells cross-checked"
    print(f"[TEST 1] cells returned={len(rows)} cross-checked={checked} (all latest-correct)")


# ---------------------------------------------------------------------------
# TEST 2 — threshold JOIN, covered severity, REAL execution + recompute
# ---------------------------------------------------------------------------

def test_heatmap_threshold_join_covered_real_pg(local_postgres, seeded_thresholds,
                                                monkeypatch):
    """The threshold-JOIN grid executes on real PG and severity is CORRECT.

    Seeds a real ``sensor_thresholds_itest`` table, doubles the join-path seam so
    the threshold concept resolves to ``sensor_thresholds_itest.limit_value``
    joined on ``measurement_type``, and leaves ``query_pg`` REAL. Asserts the
    covered envelope, that the JOIN restricts cells to the seeded measurement
    types, and that EVERY cell's threshold + severity match an independent
    recompute of ``_severity(value, seeded_limit)``.
    """
    limits = seeded_thresholds
    dim_maps = {
        DIM_X: _DIMS_X,
        DIM_Y: _DIMS_Y,
        THRESHOLD_CONCEPT: [MappingProvenance(
            table=THRESHOLD_TABLE, column=THRESHOLD_COLUMN, role="threshold",
            confidence=1.0, source="itest")],
    }
    _patch_common_seams(monkeypatch, dim_maps=dim_maps)
    monkeypatch.setattr(
        md, "_select_join_path",
        lambda system_id, measure_table, to_table, *, query=None: JoinPathProvenance(
            join_id="jp-itest", from_table=MEASURE_TABLE, from_field=TYPE_COLUMN,
            to_table=THRESHOLD_TABLE, to_field=TYPE_COLUMN,
            transform="direct", method="itest"),
    )

    env = resolve_heatmap(METRIC_ID, DIM_X, DIM_Y, threshold_concept=THRESHOLD_CONCEPT,
                          authority=_FakeAuthority())

    assert env["status"] == "ok"
    assert env["threshold_status"] == "covered"

    rows = env["rows"]
    assert rows, "threshold-JOIN resolver returned NO cells from real PG"

    sql = env["lineage"][0]["sql"]
    print("\n[TEST 2] executed SQL:\n" + sql)
    assert "DISTINCT ON" in sql
    assert "JOIN" in sql                       # covered → measure JOIN threshold
    assert THRESHOLD_TABLE in sql

    # The JOIN restricts cells to the seeded measurement types only.
    seen_types = {mtype for _eq, mtype, *_rest in rows}
    assert seen_types == set(limits), (
        f"JOIN cells {seen_types!r} != seeded types {set(limits)!r}"
    )

    # Every cell: threshold == seeded limit; severity == independent recompute.
    severities = set()
    for equip, mtype, value, thr, sev in rows:
        seeded = limits[mtype]
        assert float(thr) == seeded, (
            f"cell ({equip}, {mtype}) threshold {thr!r} != seeded {seeded!r}"
        )
        expected = _severity(value, seeded)
        assert sev == expected, (
            f"cell ({equip}, {mtype}) value={value!r} limit={seeded!r}: resolver "
            f"severity {sev!r} != recomputed {expected!r}"
        )
        severities.add(sev)

    # The real data + chosen limits must exercise more than one severity band,
    # proving the server-side classifier actually discriminated on real values.
    assert len(severities) >= 2, (
        f"expected a mix of severities over real data, saw only {severities!r}"
    )
    print(f"[TEST 2] covered cells={len(rows)} types={sorted(seen_types)} "
          f"severities={sorted(severities)} (all match recompute)")
