# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolver integration suite — the concept->SQL->value loop (S0.2 Wave 3).

These tests exercise the **real** deterministic resolver
(``agents.resolve.resolve_entity``) against the **real** backends it depends on:
a *live* Neptune Analytics graph (read-only ``neptune_query`` traversals) and a
*local* disposable Postgres seeded with ``cmms_maintenance`` (see
``tests/integration/docker-compose.yml`` + ``seed_pg.py``).

Every external dependency is gated by a skip (never a hard failure) via the W2
fixtures, so the suite stays green in a credential-free / no-Postgres CI lane:

  * ``live_graph``     skips when no reachable graph has an onboarded system.
  * ``local_postgres`` skips when no local Postgres is reachable.

The suite is strictly READ-ONLY against the graph: it imports the audit
``CYPHER_FIELD_CONCEPT`` traversal verbatim (never re-declares it) and only ever
reads. No ``neptune_write`` is called anywhere in this module. The ``no_llm_guard``
fixture turns any Bedrock invocation during resolve into a hard error, enforcing
ADR-0001 resolve/render determinism — the ``neptune-graph`` boto3 reads are
legitimate and stay live.

Four tests:
  (a) ``test_brute_force_equipment_id_hits_cmms`` — the real brute-force path:
      ``resolve_entity('equipment-id', <seeded asset_tag>)`` exercises
      ``_try_brute_force``'s MAPS_TO traversal + ``information_schema`` lookup +
      ``SELECT`` against local Postgres; asserts a brute-force rule hit a
      ``cmms_maintenance.*`` table and produced a non-empty ``unified`` record.
  (b) ``test_inventory_level_concept_to_value`` — the GATE-G0 / S0.1 F9 tracer:
      reads the ``mro-inventory -> spare_parts.quantity_on_hand`` mapping from
      live Neptune, composes a read-only ``SELECT`` via ``agents.db.query_pg``
      against local Postgres, asserts a non-null NUMERIC value (value read is now
      local — closes F9). Post the ADR-0013-amendment split the CMMS spare-parts
      on-hand field is the ``mro-inventory`` measure concept.
  (c) ``test_no_llm_in_resolve_path`` — the ADR-0001 determinism guard: confirms
      ``no_llm_guard`` is active (``bedrock_call`` / ``bedrock_stream`` raise) and
      that a full resolve completes without tripping it.
  (d) ``test_resolve_metric_spare_parts_on_hand_end_to_end`` — the S1.2 / GATE-G1
      end-to-end resolver path (ADR 0015): ``metric_resolver.resolve_metric(
      'spare-parts-on-hand')`` builds a ``QueryPlan`` from the live graph
      (concept ``mro-inventory`` -> ``spare_parts.quantity_on_hand`` in CMMS),
      composes SQL, and reads a non-null NUMERIC value from local Postgres via
      the *single shared* ``resolve_metric`` function — under ``no_llm_guard``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Callable

import pytest

import agents.db as agents_db
from agents.catalog import catalog, get_schema
from agents.db import neptune_query, query_pg, validate_identifier
from tests.audit.audit_onboarding_graph import CYPHER_FIELD_CONCEPT, _row_get

pytestmark = pytest.mark.integration

# A real, stable asset_tag seeded by lambda-adt-seed/004_cmms_maintenance.sql
# (Milwaukee Line-1 rotary filler — 'critical', so it is a demo-narrative anchor).
SEEDED_EQUIPMENT_ID = "MKE-FILL-001"

# Concept ids (validated against catalog.CORE_CONCEPTS by onboarding).
EQUIPMENT_CONCEPT = "equipment-id"
# Post ADR-0013-amendment split: the CMMS spare-parts on-hand quantity is a
# *measure* concept (``mro-inventory``), distinct from ERP material/product stock
# (``inventory-level``) and the ``spare-part`` entity. The F9 tracer field below
# (``spare_parts.quantity_on_hand``) is a CMMS field and now maps to
# ``mro-inventory``; this does NOT collide with ERP ``inventory-level`` (which
# maps to ``inventory_balances``/``inventory_items``).
MRO_CONCEPT = "mro-inventory"

# The S0.1 F9 tracer field: cmms_maintenance.spare_parts.quantity_on_hand.
INVENTORY_TABLE = "spare_parts"
INVENTORY_COLUMN = "quantity_on_hand"

# OEE input concepts (ADR 0016 / S2.1): the derived `oee` metric's three
# MetricRef inputs (availability/performance/quality-rate) map to these concepts.
# No MES maps them today, so the OEE e2e gates on their presence in the graph.
OEE_INPUT_CONCEPTS = ("availability", "performance", "quality-metric")


# ---------------------------------------------------------------------------
# Helpers (small, read-only — keep tests flat: functions <50 lines, nesting <4)
# ---------------------------------------------------------------------------

def _brute_force_cmms_hits(resolution: list[dict]) -> list[dict]:
    """Return resolution entries produced by the brute-force concept search that
    hit a ``cmms_maintenance.*`` table.

    ``_try_brute_force`` tags its rule string ``"... (no IdentityRule —
    brute-force)"`` and sets ``check`` to ``"<schema>.<table>.<field> ✓ (fuzzy
    match)"``. Matching on both proves the brute-force MAPS_TO traversal +
    ``information_schema`` lookup + ``SELECT`` actually ran against the cmms schema.
    """
    return [
        entry
        for entry in resolution
        if "brute-force" in entry.get("rule", "")
        and "cmms_maintenance" in entry.get("check", "")
    ]


def _inventory_mapping(system_id: str) -> tuple[str, str] | None:
    """Read the ``mro-inventory`` field->concept mapping from live Neptune.

    Reuses the audit ``CYPHER_FIELD_CONCEPT`` traversal verbatim, filters to the
    ``mro-inventory`` concept (the post ADR-0013-split measure concept the CMMS
    spare-parts on-hand field maps to), and returns the ``(table, column)`` for
    the ``quantity_on_hand`` tracer (preferred) or the first ``mro-inventory``
    row. Returns ``None`` when the system maps no ``mro-inventory`` field.
    """
    rows = neptune_query(CYPHER_FIELD_CONCEPT, {"sid": system_id}) or []
    inventory = [
        (_row_get(r, "table", "se.name"), _row_get(r, "column", "f.name"))
        for r in rows
        if _row_get(r, "concept", "c.id") == MRO_CONCEPT
    ]
    inventory = [(t, c) for t, c in inventory if t and c]
    if not inventory:
        return None
    for table, column in inventory:
        if column == INVENTORY_COLUMN:
            return table, column
    return inventory[0]


def _maps_concept(system_id: str, concept: str) -> bool:
    """True when the live graph has a Field-[:MAPS_TO]->Concept for ``concept``.

    Reuses the audit ``CYPHER_FIELD_CONCEPT`` traversal verbatim (read-only).
    Used to gate the OEE e2e on the presence of the availability / performance /
    quality-metric input mappings (no MES maps them today).
    """
    rows = neptune_query(CYPHER_FIELD_CONCEPT, {"sid": system_id}) or []
    return any(_row_get(r, "concept", "c.id") == concept for r in rows)


def _read_scalar(schema: str, table: str, column: str):
    """Compose + run a read-only ``SELECT <column> FROM <table> LIMIT 1``.

    Identifiers are regex-validated before composition (``query_pg`` opens a
    read-only connection). Returns the single scalar value (or ``None``).
    """
    safe_table = validate_identifier(table)
    safe_column = validate_identifier(column)
    sql = f"SELECT {safe_column} FROM {safe_table} LIMIT 1"  # nosec B608 - identifiers regex-validated; read-only connection
    parsed = json.loads(query_pg(schema, sql))
    data_rows = parsed.get("rows", [])
    if data_rows and data_rows[0]:
        return data_rows[0][0]
    return None


def _is_numeric(value) -> bool:
    """True when ``value`` is a real (non-null) numeric scalar.

    ``query_pg`` serializes via ``json.dumps(default=str)``: native ints/floats
    stay numeric, ``Decimal`` may arrive as a numeric string — both are accepted
    as long as they parse as a number (and are not boolean).
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# (a) Real brute-force path
# ---------------------------------------------------------------------------

def test_brute_force_equipment_id_hits_cmms(
    live_graph: str,
    local_postgres: dict,
    resolve_fn: Callable[[str, str], tuple],
    no_llm_guard: None,
) -> None:
    """resolve_entity('equipment-id', <seeded asset_tag>) hits cmms via brute-force.

    With no RESOLVES-edge IdentityRules for ``equipment-id`` (onboarding writes
    LINKS_CONCEPT, not RESOLVES — ADR-0011 F1), the direct-match and
    pattern-transform sections are no-ops, so ``_try_brute_force`` is the real
    path under test: it traverses ``Field-[:MAPS_TO]->Concept`` for the seeded
    cmms field mapped to ``equipment-id``, resolves the table via
    ``information_schema``, and ``SELECT``s the seeded row from local Postgres.
    """
    raw, resolution, unified, _correlations, _boundaries = resolve_fn(
        EQUIPMENT_CONCEPT, SEEDED_EQUIPMENT_ID
    )

    hits = _brute_force_cmms_hits(resolution)
    assert hits, (
        f"brute-force did not hit a cmms_maintenance.* table resolving "
        f"'{EQUIPMENT_CONCEPT}' = '{SEEDED_EQUIPMENT_ID}'; resolution={resolution}"
    )
    assert unified, (
        f"resolve produced an empty 'unified' record for "
        f"'{SEEDED_EQUIPMENT_ID}'; raw={raw}"
    )
    assert any(item.get("fields") for item in unified), (
        f"'unified' record has no populated fields: {unified}"
    )


# ---------------------------------------------------------------------------
# (b) Concept -> SQL -> value tracer (GATE-G0 / S0.1 F9)
# ---------------------------------------------------------------------------

def test_inventory_level_concept_to_value(
    live_graph: str,
    local_postgres: dict,
    no_llm_guard: None,
) -> None:
    """Trace mro-inventory -> spare_parts.quantity_on_hand -> a real value.

    The *mapping* (concept -> table.column) is read from the live graph; the
    *value* is read from the local Postgres value-read lane via the read-only
    ``agents.db.query_pg`` — closing S0.1 F9 without cloud Postgres. Post the
    ADR-0013-amendment split, the CMMS spare-parts on-hand field is the
    ``mro-inventory`` measure concept (not ERP ``inventory-level``).
    """
    mapping = _inventory_mapping(live_graph)
    if mapping is None:
        pytest.skip(
            f"system '{live_graph}' maps no '{MRO_CONCEPT}' field on the "
            f"live graph — re-onboard so {INVENTORY_TABLE}.{INVENTORY_COLUMN} is "
            f"mapped (see the mapping-quality eval task)"
        )

    table, column = mapping
    schema = get_schema(live_graph)
    assert schema, f"no Postgres schema in catalog for system '{live_graph}'"

    value = _read_scalar(schema, table, column)
    assert _is_numeric(value), (
        f"concept '{MRO_CONCEPT}' traced to {schema}.{table}.{column} but "
        f"the value read was not a non-null numeric: {value!r}"
    )


# ---------------------------------------------------------------------------
# (c) Determinism guard (ADR-0001)
# ---------------------------------------------------------------------------

def test_no_llm_in_resolve_path(
    live_graph: str,
    local_postgres: dict,
    resolve_fn: Callable[[str, str], tuple],
    no_llm_guard: None,
) -> None:
    """No LLM fires in any resolve path (ADR-0001 determinism).

    Confirms ``no_llm_guard`` is actually wired (both Bedrock entry points raise)
    and that a full ``resolve_entity`` over the seeded equipment id completes
    without tripping the guard. boto3 ``neptune-graph`` reads are legitimate and
    are NOT guarded.
    """
    with pytest.raises(AssertionError, match="bedrock_call"):
        agents_db.bedrock_call("guard wiring probe — must not reach Bedrock")
    with pytest.raises(AssertionError, match="bedrock_stream"):
        agents_db.bedrock_stream("guard wiring probe — must not reach Bedrock")

    # A full resolve must not invoke either guarded LLM entry point; if it did,
    # the monkeypatched stubs would raise AssertionError and fail this test.
    resolve_fn(EQUIPMENT_CONCEPT, SEEDED_EQUIPMENT_ID)


# ---------------------------------------------------------------------------
# (d) End-to-end metric resolver (S1.2 / GATE-G1, ADR 0015)
# ---------------------------------------------------------------------------

def test_resolve_metric_spare_parts_on_hand_end_to_end(
    live_graph: str,
    local_postgres: dict,
    no_llm_guard: None,
) -> None:
    """resolve_metric('spare-parts-on-hand') -> QueryPlan -> SQL -> a real value.

    The S1.2 end-to-end path (ADR 0015): the single shared
    ``agents.metric_resolver.resolve_metric`` reads the metric layer
    (``spare-parts-on-hand`` -> concept ``mro-inventory``), resolves authority +
    the ``MAPS_TO`` field from the *live* graph into an explicit ``QueryPlan``
    (``spare_parts.quantity_on_hand`` in the CMMS system), composes a read-only
    aggregate ``SELECT``, and reads a non-null NUMERIC value from *local*
    Postgres — all under ``no_llm_guard`` (ADR-0001: no LLM in the resolve path).

    Skips cleanly (mirroring the F9 tracer) when the live graph has no
    ``mro-inventory`` mapping — e.g. a graph onboarded before the
    ADR-0013-amendment split, where ``spare_parts.quantity_on_hand`` still maps
    to ``inventory-level``. Re-onboarding to surface the mapping is the gated
    cloud lane, out of scope here.
    """
    # Import inside the test so collection never fails in lanes lacking the
    # resolver's optional deps (the gate fixtures skip before we reach here).
    import agents.metric_resolver as metric_resolver

    if _inventory_mapping(live_graph) is None:
        pytest.skip(
            f"system '{live_graph}' maps no '{MRO_CONCEPT}' field on the live "
            f"graph — re-onboard so {INVENTORY_TABLE}.{INVENTORY_COLUMN} maps to "
            f"'{MRO_CONCEPT}' (post ADR-0013-amendment split)"
        )

    resolved = metric_resolver.resolve_metric("spare-parts-on-hand")

    assert _is_numeric(resolved.value), (
        f"resolve_metric('spare-parts-on-hand') returned a non-numeric/null "
        f"value: {resolved.value!r} (query_plan={resolved.query_plan})"
    )
    assert resolved.query_plan.column == INVENTORY_COLUMN, (
        f"expected query_plan.column == {INVENTORY_COLUMN!r}, got "
        f"{resolved.query_plan.column!r}"
    )
    assert resolved.query_plan.table == INVENTORY_TABLE, (
        f"expected query_plan.table == {INVENTORY_TABLE!r}, got "
        f"{resolved.query_plan.table!r}"
    )

    # The resolved system must be the CMMS system (asserting on system_type keeps
    # this robust to the system id; table == 'spare_parts' pins it to cmms vs the
    # cmms2 synonym variant, which uses a 'stockroom' table).
    entry = catalog.get(resolved.system_id)
    assert entry and entry.get("system_type") == "CMMS", (
        f"resolved system_id '{resolved.system_id}' is not the CMMS system "
        f"(catalog entry={entry})"
    )


# ---------------------------------------------------------------------------
# (e) End-to-end derived metric resolver — OEE (S2.1 / GATE-G2, ADR 0016)
# ---------------------------------------------------------------------------

def test_resolve_metric_oee_end_to_end(
    live_graph: str,
    local_postgres: dict,
    no_llm_guard: None,
) -> None:
    """resolve_metric('oee') -> derive availability·performance·quality-rate.

    The S2.1 end-to-end path (ADR 0016): the single shared
    ``agents.metric_resolver.resolve_metric`` resolves the derived ``oee`` metric
    by evaluating its op-tree — each of the three ``MetricRef`` inputs
    (availability / performance / quality-rate) is resolved through the *same*
    graph→QueryPlan→SQL→value path against the *live* graph + *local* Postgres,
    under ``no_llm_guard`` (ADR-0001: no LLM in the resolve path).

    Gated mirror of the F9 spare-parts e2e: no MES maps
    availability/performance/quality-metric today, so this skips cleanly until an
    MES is onboarded with those mappings (the gated cloud lane). The unit lane
    exercises the evaluator offline with mocked sub-resolves.
    """
    # Import inside the test so collection never fails in lanes lacking the
    # resolver's optional deps (the gate fixtures skip before we reach here).
    import agents.metric_resolver as metric_resolver

    missing = [c for c in OEE_INPUT_CONCEPTS if not _maps_concept(live_graph, c)]
    if missing:
        pytest.skip(
            "OEE e2e requires an onboarded MES mapping "
            "availability/performance/quality-metric (+ optionally oee-score); "
            "re-onboard an MES to enable"
        )

    resolved = metric_resolver.resolve_metric("oee")

    assert _is_numeric(resolved.value), (
        f"resolve_metric('oee') returned a non-numeric/null value: "
        f"{resolved.value!r}"
    )
    assert resolved.metric_id == "oee"
    assert resolved.unit == "ratio"
