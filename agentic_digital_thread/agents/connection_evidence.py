# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What proved each connection onboarding wrote, and one system's connections.

Provision writes a JoinPath for every join it finds. ``jp.method`` names how
the join is used and readers key on it (the route seed, the orchestrator,
resolve, self-learning), so recording evidence left it as it was, with one
correction: a cross-system join from a direct identity rule reads
:data:`IDENTITY_RULE_JOIN_METHOD`, not the ``foreign_key_match`` it was
written with until 2026-10-19 (no foreign key exists; the readers that key on
``foreign_key_match`` read only joins inside one system). Beside it each
provision writer records WHY the join exists:

* ``jp.basis``: one of :data:`JOIN_BASES`;
* the numbers behind it (:data:`JOIN_PATH_EVIDENCE_FIELDS`): the declared
  constraint's name, the shared-value count and ratio of a value overlap, the
  value-pair count of a same-row pair (or that both columns are UNIQUE), the
  identity rule a cross-system join came from and that rule's method, the
  counts a measured reference was proven on, or why a naming guess stayed one.

A JoinPath written before these properties existed has no ``basis``; the UI
labels it from ``method`` alone and never calls it declared.

:func:`system_connections` is the bounded read behind the system detail
page's Connections section: the JoinPaths, ValueMappings and IdentityRules
that touch one system (either direction), each list cut at its limit and
reported with the total it was cut from.
"""
from __future__ import annotations

from . import joinpath_rule_link as _jp_rule
from .rule_status import rule_active as _rule_active
from .db import neptune_query_checked
from .public_errors import public_error

#: A FOREIGN KEY constraint the database declares (on the table, or on the
#: table behind a one-table view).
BASIS_DECLARED_FOREIGN_KEY = "declared_foreign_key"
#: The ``*_id`` naming guess: a column named after a table that has the same
#: column. Nothing about the data proved it: either the data was not read
#: (``reference_check`` says why) or the read did not bear it out.
BASIS_COLUMN_NAME_PATTERN = "column_name_pattern"
#: A ``*_id`` naming guess provision measured against the data: every non-null
#: value of the referencing column is in the target column, the target column
#: holds one distinct, non-null value per row (``reference_count``,
#: ``key_count``), and no other table the name could mean holds them too, at
#: ``measured_at`` (UTC). Evidence for ONE reader only: the identifier-forms
#: pairing (``identifier_translation``), whose per-item conversion still runs
#: as a live query against the source. Everywhere else it counts exactly as
#: the naming guess does; it is never a declared key.
BASIS_MEASURED_REFERENCE = "measured_reference"
#: Sampled values two tables of the system share (the transform ladder).
BASIS_VALUE_OVERLAP = "value_overlap"
#: Two identifier columns of the same row: both UNIQUE, or measured 1:1.
BASIS_SAME_ROW_IDENTIFIERS = "same_row_identifiers"
#: A cross-system identity rule (accepted at the Resolve checkpoint).
BASIS_IDENTITY_RULE = "identity_rule"

#: ``jp.method`` of the join a direct identity rule (no format change, not
#: an exact-agreement rule) writes: the values are joined as they are, and the
#: link came from the identity rule (``rule_id``), not from a foreign key.
IDENTITY_RULE_JOIN_METHOD = "identity_rule_match"

JOIN_BASES = frozenset({
    BASIS_DECLARED_FOREIGN_KEY, BASIS_COLUMN_NAME_PATTERN, BASIS_MEASURED_REFERENCE,
    BASIS_VALUE_OVERLAP, BASIS_SAME_ROW_IDENTIFIERS, BASIS_IDENTITY_RULE,
})

#: The evidence properties a JoinPath may carry beside ``basis``.
#: ``reference_count`` / ``key_count``: a measured reference's distinct
#: referencing values (all found) and the target column's rows (all distinct),
#: read at ``measured_at`` (UTC, ISO 8601);
#: ``reference_check``: why a naming guess was left a guess.
JOIN_PATH_EVIDENCE_FIELDS = (
    "basis", "constraint_name", "overlap_count", "overlap_ratio", "pair_count",
    "unique_constraints", "rule_id", "rule_method", "transform",
    "reference_count", "key_count", "measured_at", "reference_check",
)

#: Most rows each list of :func:`system_connections` returns.
JOIN_PATH_LIMIT = 200
VALUE_MAPPING_LIMIT = 50
IDENTITY_RULE_LIMIT = 100

#: Longest system id the read accepts (catalog ids are far shorter).
_MAX_SYSTEM_ID_CHARS = 128

_JOIN_PATH_FIELDS = (
    "id", "from_system", "from_table", "from_field", "to_system", "to_table",
    "to_field", "method", "description", "confidence", "origin_context",
) + JOIN_PATH_EVIDENCE_FIELDS
_VALUE_MAPPING_FIELDS = (
    "id", "from_system", "from_field", "from_value", "to_system", "to_field",
    "to_value", "concept", "confidence", "method",
)
_IDENTITY_RULE_FIELDS = (
    "id", "concept", "source_system", "source_field", "source_pattern",
    "target_system", "target_field", "target_pattern", "method", "status",
    "confidence", "origin_context", "provenance", "approved_by",
)

#: (label, variable, WHERE, fields, ORDER BY keys (at most 5), limit, result key)
#: A switched-off JoinPath (its own status, or the IdentityRule it came from:
#: :mod:`joinpath_rule_link`) is not a connection, like a deactivated rule.
_READS = (
    ("JoinPath", "jp", "(jp.from_system = $sid OR jp.to_system = $sid) "
     "AND " + _jp_rule.rule_active("jp"), _JOIN_PATH_FIELDS,
     ("from_system", "from_table", "from_field", "to_system", "to_table"),
     JOIN_PATH_LIMIT, "join_paths"),
    ("ValueMapping", "vm", "vm.from_system = $sid OR vm.to_system = $sid", _VALUE_MAPPING_FIELDS,
     ("concept", "from_system", "from_value", "to_system", "to_value"),
     VALUE_MAPPING_LIMIT, "value_mappings"),
    ("IdentityRule", "ir",
     "(ir.source_system = $sid OR ir.target_system = $sid) "
     f"AND {_rule_active('ir')}",
     _IDENTITY_RULE_FIELDS, ("concept", "source_field", "target_system", "target_field"),
     IDENTITY_RULE_LIMIT, "identity_rules"),
)


#: Clauses after a kind's MATCH ... WHERE, by label.
_AFTER_MATCH = {"JoinPath": _jp_rule.drop_if_rule_inactive("jp", "jp_rule")}


def _aliased(var: str, fields) -> str:
    return ", ".join(f"{var}.{f} AS {f}" for f in fields)


def _read_kind(label, var, where, fields, order, limit, sid) -> tuple[list, int]:
    """``(rows, total)`` of one kind: a count, then at most ``limit`` rows in
    a stable order. Both reads are checked: a failure raises."""
    match = f"MATCH ({var}:{label}) WHERE {where} " + _AFTER_MATCH.get(label, "")
    counted = neptune_query_checked(match + f"RETURN count({var}) AS total", {"sid": sid})
    total = int((counted or [{}])[0].get("total") or 0)
    rows = neptune_query_checked(
        match + "RETURN " + _aliased(var, fields)
        + " ORDER BY " + ", ".join(f"{var}.{k}" for k in order)
        + f" LIMIT {int(limit)}",
        {"sid": sid})
    rows = [dict(r) for r in (rows or []) if isinstance(r, dict)][:limit]
    return rows, max(total, len(rows))


def system_connections(system_id: str) -> dict:
    """The connections that touch ``system_id``: JoinPaths and ValueMappings
    in either direction and the IdentityRules it is the source or target of,
    each list bounded and reported with its total. A failed graph read answers
    the fixed public error (an empty list would read as "no connections")."""
    sid = str(system_id or "").strip()
    if not sid or len(sid) > _MAX_SYSTEM_ID_CHARS:
        return {"system_id": sid[:_MAX_SYSTEM_ID_CHARS], "error": "Unknown system."}
    out: dict = {"system_id": sid}
    try:
        for label, var, where, fields, order, limit, key in _READS:
            rows, total = _read_kind(label, var, where, fields, order, limit, sid)
            out[key] = rows
            out[f"{key}_total"] = total
    except Exception as exc:  # noqa: BLE001 - answered as the fixed public error
        return {"system_id": sid,
                **public_error(exc, "onboard.connections", category="graph_query_failed")}
    out["limits"] = {key: limit for *_rest, limit, key in _READS}
    return out
