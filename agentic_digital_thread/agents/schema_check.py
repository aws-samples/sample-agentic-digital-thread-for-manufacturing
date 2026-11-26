# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Live schema-existence checks for the self-learning enrichment / validation layer.

``proposal_scoring.score_proposal`` is a PURE, offline, string-only scorer (reused
by ingestion auto-accept and the bulk endpoints) — it must never touch the graph.
This module is the ENRICHMENT-layer counterpart: it consults the real onboarded
schema (the knowledge graph, via ``graph_tools.get_system_schema``) so the API can
tell an operator whether a ``table.column`` a proposal references actually exists,
and so the user-define route can reject a relationship on a column that is not
there.

Design contract (callers depend on it):

* ``column_exists`` returns ``True``/``False``, or ``None`` when the schema is
  UNAVAILABLE (system not onboarded, empty schema, or a lookup error). It NEVER
  raises — an unavailable schema is an expected fallback, not an error, so callers
  degrade gracefully (keep the static recommendation / skip that specific check).
* A field is a qualified ``table.column`` reference. Matching is case-insensitive
  and tolerant of flattened nested-struct / SiteWise dotted property paths. A
  *single-dot* reference resolves definitively (``True``/``False``). A *multi-dot*
  reference (``table.struct.leaf`` vs a catalog-qualified ``db.table.column``) is
  ambiguous, so it is only affirmed (``True``) on a match and otherwise left
  ``None`` (indeterminate) rather than a misleading ``False``. An unqualified field
  (no dot) is ``None``.
* When the schema IS available but a single-dot reference names a table/column that
  is absent, the column definitively does not exist ⇒ ``False``.
"""
from __future__ import annotations

import json


def load_system_schema(system_id: str) -> dict | None:
    """Return the parsed graph schema ``{table: {"columns": [...], ...}}`` for an
    onboarded system, or ``None`` when it is unavailable.

    Reads ``graph_tools.get_system_schema`` (the single source of truth — the exact
    columns persisted during onboarding). ``get_system_schema`` returns a JSON
    string that is either the table map or ``{"error": ...}`` when nothing is
    onboarded for the id. Any parse/lookup failure, an ``error`` payload, or an
    empty map all collapse to ``None`` (schema unavailable). Never raises.

    Import is lazy so this helper stays importable without app-runtime deps and so
    tests can monkeypatch ``graph_tools.get_system_schema`` (the lazy import binds
    the current attribute at call time).
    """
    if not system_id:
        return None
    try:
        from .graph_tools import get_system_schema

        parsed = json.loads(get_system_schema(system_id))
    except Exception:  # nosec B110 - unavailable schema is an expected None fallback
        return None
    if not isinstance(parsed, dict) or "error" in parsed or not parsed:
        return None
    return parsed


def _resolve_table(schema: dict, table: str) -> dict | None:
    """Case-insensitive table lookup → its metadata dict, or ``None`` when absent.

    Onboarded identifiers vary in case across connectors (SiteWise asset models,
    Athena/Glue tables), so an exact-case miss falls back to a case-insensitive
    scan before concluding the table is absent.
    """
    meta = schema.get(table)
    if isinstance(meta, dict):
        return meta
    tl = table.lower()
    for name, m in schema.items():
        if isinstance(m, dict) and str(name).lower() == tl:
            return m
    return None


def _column_present(meta: dict, column: str) -> bool:
    """Whether ``column`` matches a real column in ``meta`` — case-insensitively and
    tolerant of flattened nested-struct / SiteWise-property dotted paths.

    A source that flattens nested structs (or SiteWise properties) stores columns as
    dotted leaf paths (e.g. ``oee.availability``). A proposal may reference either the
    struct PARENT (``oee``) or a specific leaf, so a match is affirmed when the
    reference and a stored column are equal OR one is a dotted-prefix of the other.
    """
    columns = meta.get("columns", []) or []
    cl = column.lower()
    for c in columns:
        s = str(c).lower()
        if s == cl or s.startswith(cl + ".") or cl.startswith(s + "."):
            return True
    return False


def column_in_schema(schema: dict | None, field: str) -> bool | None:
    """Whether a qualified ``table.column`` ``field`` exists in an already-loaded
    ``schema`` (as returned by :func:`load_system_schema`).

    ``None`` when the schema is unavailable (``schema is None``) or ``field`` is not
    a qualified (dotted) reference (indeterminate). ``False`` when a *single-dot*
    ``table.column`` reference names a table/column the known schema does not contain.
    Pure — no I/O; lets a caller batch one schema load and check many fields.

    Matching is case-insensitive and tolerant of flattened nested-struct / SiteWise
    dotted property paths (a struct parent matches when its dotted leaves are stored,
    and vice-versa) — this is what keeps a legitimate nested field from producing a
    misleading "column not found" verdict.

    Multi-dot fields (``table.struct.leaf`` vs a catalog-qualified
    ``db.table.column``) are genuinely ambiguous, so they are only ever affirmed
    (``True``) on a match and otherwise left INDETERMINATE (``None``) rather than a
    misleading ``False``.
    """
    if schema is None:
        return None
    if not field or "." not in field:
        return None  # unqualified reference — cannot resolve a table.column

    # Primary interpretation: split on the FIRST dot so the column keeps any further
    # dotted path (flattened nested-struct / SiteWise leaves stay intact).
    table, column = field.split(".", 1)
    meta = _resolve_table(schema, table)
    if meta is not None and _column_present(meta, column):
        return True

    if field.count(".") == 1:
        # Single-dot, definitive: table absent OR column absent ⇒ missing.
        return False

    # Multi-dot: ambiguous. Try the catalog-qualified reading (last two segments as
    # table.column), then a flattened-column reading (whole path is a column), and
    # only affirm on a hit — never return a misleading False.
    parts = field.split(".")
    alt = _resolve_table(schema, parts[-2])
    if alt is not None and _column_present(alt, parts[-1]):
        return True
    for m in schema.values():
        if isinstance(m, dict) and _column_present(m, field):
            return True
    return None


def column_exists(system_id: str, field: str) -> bool | None:
    """Whether ``table.column`` ``field`` exists for onboarded ``system_id``.

    ``True``/``False``, or ``None`` when the schema is unavailable (system not
    onboarded / lookup error) or ``field`` is not a single-dot reference. Never
    raises — callers fall back on ``None``. Composes :func:`load_system_schema`
    with :func:`column_in_schema`.
    """
    return column_in_schema(load_system_schema(system_id), field)


def table_exists(system_id: str, table: str) -> bool | None:
    """Whether ``table`` exists for onboarded ``system_id``.

    ``True``/``False``, or ``None`` when the schema is unavailable (system not
    onboarded / lookup error). Never raises — callers fall back on ``None``.
    Table matching is case-insensitive, matching the behavior of
    :func:`_resolve_table`.
    """
    schema = load_system_schema(system_id)
    if schema is None:
        return None
    return _resolve_table(schema, table) is not None


# Proposal types whose ``from_field`` / ``to_field`` are real ``table.column``
# references we can verify against the onboarded schema. SINGLE SOURCE OF TRUTH,
# shared by the ingestion gate (``self_learning.log_proposed_mapping``) and the
# display-time enrichment (``server.get_proposed_mappings``) so the "reject at
# creation" and the "block at display" verdicts can never drift. Types outside this
# set (``identity_rule`` / ``crosswalk`` / ``concept_mapping`` / correlations) carry
# a natural-language pattern in their field slots, not a column, so they are NEVER
# schema-gated.
SCHEMA_CHECKED_TYPES = frozenset({"field_annotation", "value_mapping", "join_path"})


def proposal_missing_field(
    proposed_type: str,
    from_system: str,
    from_field: str,
    to_system: str = "",
    to_field: str = "",
    schemas: "dict[str, dict | None] | None" = None,
) -> str | None:
    """Return the FIRST ``system.field`` a proposal references that DEFINITIVELY does
    not exist in the onboarded schema (``column_exists`` verdict ``False``), or
    ``None`` when every checkable endpoint exists (``True``) or is INDETERMINATE
    (``None``: schema unavailable / ambiguous multi-dot / unqualified field).

    This is the UPSTREAM ingestion gate's decision function — the mirror image of the
    display-time enrichment in ``server.get_proposed_mappings``. Contract:

    * Only ``SCHEMA_CHECKED_TYPES`` proposals are gated; any other type returns
      ``None`` (never gated — its field slot is a pattern, not a column).
    * A missing endpoint is only reported on a DEFINITIVE ``False``. An unknown
      verdict (``None`` — schema not loaded, or an ambiguous reference) is treated as
      "cannot determine" and does NOT report a miss, so ingestion degrades gracefully
      and never rejects on uncertainty (builds on the trustworthy verdict of
      2026-07-08 — no false negatives).
    * Both the ``from`` and the ``to`` endpoint are checked (a mapping to a
      nonexistent target column is equally unusable); the first definitive miss is
      returned. Never raises — ``column_exists`` swallows and falls back to ``None``.
    * Additionally checks table existence when column_exists returns None — if the
      table definitely does not exist, that is a definitive miss even if the schema
      was unavailable (column_exists returned None due to missing schema).
    """
    if proposed_type not in SCHEMA_CHECKED_TYPES:
        return None

    def _col(sid: str, field: str) -> bool | None:
        # With a preloaded ``schemas`` cache, resolve PURELY against it (no I/O) so a
        # bulk caller (reconcile) can verify a large backlog without an N+1 of graph
        # reads; otherwise consult the live schema.
        if schemas is not None:
            return column_in_schema(schemas.get(sid), field)
        return column_exists(sid, field)

    def _table_absent(sid: str, table: str) -> bool:
        # Definitive "table does not exist" — the fallback when the column verdict is
        # indeterminate. Mirrors ``table_exists(...) is False`` against the cache when
        # one is supplied.
        if schemas is not None:
            schema = schemas.get(sid)
            return schema is not None and _resolve_table(schema, table) is None
        return table_exists(sid, table) is False

    def _check_field(sid: str, field: str) -> str | None:
        """Check a single field and return the missing reference or None."""
        if not sid or not field:
            return None
        # First check if the column exists (definitive True/False)
        if _col(sid, field) is False:
            return f"{sid}.{field}"
        # If the column verdict is None (schema unavailable), check table existence
        # as a fallback - if the table definitively does not exist, that's a miss
        if "." in field:
            table = field.split(".", 1)[0]
            if _table_absent(sid, table):
                return f"{sid}.{field}"
        return None

    # Check both from and to fields
    missing = _check_field(from_system, from_field)
    if missing:
        return missing
    return _check_field(to_system, to_field)


def system_in_catalog(system_id: str, known: "list[dict] | None") -> bool | None:
    """Whether ``system_id`` is a canonical catalog system id, judged against a
    caller-supplied ``known`` list (catalog entries with an ``id`` key).

    Three-valued, mirroring ``column_in_schema``'s semantics:

    * ``True`` — the id matches a catalog entry (exact ``id`` match; the
      ingestion normalizer has already canonicalized resolvable references, so
      display-time checking is id-exact by design).
    * ``False`` — DEFINITIVE: the catalog is non-empty and the id matches no
      entry. A proposal naming such a system can never apply — the graph-side
      ``MATCH (s:System {id: ...})`` would match zero rows (a silent no-op) —
      so callers treat this as a blocking verdict.
    * ``None`` — no basis to judge: an empty/unavailable ``known`` list (base
      mode pre-onboarding, or a failed catalog read). Never block on
      uncertainty.

    Pure — no I/O; the caller supplies the catalog read (e.g.
    ``self_learning.known_systems()``).
    """
    if not system_id or not known:
        return None
    return any(entry.get("id") == system_id for entry in known)


def is_onboarded_system(system_id: str) -> bool:
    """Whether ``system_id`` is a currently-onboarded system in the catalog.

    The catalog is the in-memory source of truth for onboarded-system ids
    (``catalog.get_onboarded_ids``). Used to HARD-BLOCK a user-defined relationship
    that names a system that does not exist (distinct from the soft, ``None``-fallback
    column check).
    """
    if not system_id:
        return False
    from .catalog import catalog

    return system_id in catalog.get_onboarded_ids()
