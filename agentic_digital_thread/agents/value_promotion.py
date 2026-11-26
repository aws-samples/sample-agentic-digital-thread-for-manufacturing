# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-value → rule promotion sweep (ADR-0043, T1).

The agent's finest-grained discovery — the ``value_mapping`` proposal
(``LINE1 → DET-L1``, one value pair per row) — accumulates in the queue
without ever generalizing (live evidence: 3 per-plant ``line_id`` rows and
40 serial↔tag rows, each an independent per-value rediscovery of ONE rule).
This module collapses them:

  * **Grain** (ADR-0043 D1) — PENDING ``value_mapping`` rows are grouped by
    the FIELD PAIR ``(from_system, from_field, to_system, to_field)``. Only
    groups whose fields are ``table.column``-shaped and whose systems OR
    tables differ are eligible (same-system same-table pairs are ADR-0041's
    same-row territory, captured at provision — never promoted from the queue).
  * **Threshold** (ADR-0043 D2) — count of INDEPENDENT CONFIRMATIONS: distinct
    normalized ``from_value``s (default ≥ 3). Explicitly NOT LLM confidence
    (audit evidence: everything scores 0.7–0.9 — it does not discriminate).
  * **Promotion** (ADR-0043 D3) — ``join_discovery.discover_transform`` runs
    over the accumulated value pairs: transform found → ONE ``join_path``
    proposal (transform + measured overlap carried); no transform but aligned
    pairs → ONE ``crosswalk`` proposal (first aligned pair as the exemplar).
    Both are emitted through ``self_learning.log_proposed_mapping`` with
    ``source="agent"`` at the EXISTING ``proposal_key`` grains — no
    ``_DISPATCH`` change, so the promoted proposal inherits the full ADR-0003
    collision policy and the ADR-0042 churn controls unchanged.
  * **Subsumption** (ADR-0043 D4) — the group's per-value rows move to
    ``superseded`` with ``subsumed_by=<new mapping_id>`` (additive attribute:
    "generalized into a rule", distinct from a target-collision supersede).
  * **Auto-accept stays OFF** (ADR-0043 D5) — the sweep NEVER calls accept.
    The emitted proposal enters ``pending`` by construction: ``join_path`` is
    outside ``self_learning._AUTO_ACCEPT_TYPES`` and ``crosswalk`` is in the
    ``_NEVER_AUTO_ACCEPT_TYPES`` deny-tuple. Operator-triggered admin route
    (``POST /api/proposed-mappings/promote-values``) — no scheduler hook.

Mirrors the ``self_learning.sweep_auto_accept`` sweep shape: per-group
best-effort (one bad group never aborts the sweep), quarantined rows skipped
(ADR-0042 D2 — frozen until a HUMAN accept/reject), counts returned.

Import purity (ADR-0020/0021): module level is stdlib-only; the
self_learning / join_discovery / proposal_scoring imports are LAZY (inside
functions), so importing this module performs zero AWS/DB/graph I/O.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ADR-0043 D2: default promotion threshold — distinct confirming from_values.
DEFAULT_MIN_CONFIRMATIONS = 3

# Process-local observation cache. It is primed from one pending-row snapshot,
# then updated from successful writes. A field pair triggers at most once for
# the lifetime of the process; the sweep itself remains the source of truth and
# re-checks persisted pending rows before promoting anything.
_observed_pending_values: dict[tuple, set[str]] | None = None
_triggered_field_pairs: set[tuple] = set()


def _reset_auto_promotion_state() -> None:
    """Clear process-local trigger state (offline-test seam)."""
    global _observed_pending_values
    _observed_pending_values = None
    _triggered_field_pairs.clear()


# --------------------------------------------------------------------------- #
# PURE helpers (stdlib-only — offline-testable, no I/O)
# --------------------------------------------------------------------------- #
# A bare identifier token (no dots): the field shape non-SQL connectors
# register (historian/asset properties). Same charset as the dotted form's
# segments; length-bounded like _FIELD_MAX_LEN's intent.
_BARE_TOKEN_RE = re.compile(r"[A-Za-z0-9_$-]{1,128}")


def _field_table(field: str) -> str:
    """Table part of a dotted ``table.column`` field reference. Pure."""
    return (field or "").split(".", 1)[0]


def _norm_value(value) -> str:
    """Normalize a value for distinctness counting (strip + casefold). Pure."""
    return str(value if value is not None else "").strip().casefold()


def group_value_mappings(pending: list[dict]) -> dict[tuple, list[dict]]:
    """Group pending ``value_mapping`` rows by the field-pair grain (ADR-0043 D1).

    Key = ``(from_system, from_field, to_system, to_field)``. A row enters a
    group only when ALL hold:

      * ``proposed_type == "value_mapping"`` (other types are never promoted);
      * BOTH fields are promotable references: ``table.column``-shaped
        (``proposal_scoring.is_table_column_shaped``) OR a single bare
        identifier token. The bare form is REQUIRED coverage, not a
        relaxation: non-SQL sources register bare property names (observed
        live: a historian ``asset_name`` → MES ``machine_identifier``, 19
        pending per-value rows invisible to the sweep because neither side
        carries a dot). A narrative sentence still never qualifies;
      * the systems differ OR the tables differ — a same-system same-table
        pair is ADR-0041 same-row territory (captured at provision as a
        ``transform='same_row'`` internal JoinPath), NOT promoted from the queue.

    Pure — no I/O (``is_table_column_shaped`` is a pure predicate, imported
    lazily to keep this module import-pure)."""
    from .proposal_scoring import is_table_column_shaped

    def _promotable_field(field: str) -> bool:
        return is_table_column_shaped(field) or bool(_BARE_TOKEN_RE.fullmatch(field))

    groups: dict[tuple, list[dict]] = {}
    for row in pending or []:
        if (row.get("proposed_type") or "") != "value_mapping":
            continue
        from_system = (row.get("from_system") or "").strip()
        from_field = (row.get("from_field") or "").strip()
        to_system = (row.get("to_system") or "").strip()
        to_field = (row.get("to_field") or "").strip()
        if not (from_system and to_system):
            continue
        if not (_promotable_field(from_field) and _promotable_field(to_field)):
            continue
        if (from_system == to_system
                and _field_table(from_field) == _field_table(to_field)):
            continue  # ADR-0041 same-row turf — provision-time, not queue-time
        key = (from_system, from_field, to_system, to_field)
        groups.setdefault(key, []).append(row)
    return groups


def independent_confirmations(rows: list[dict]) -> int:
    """Count of DISTINCT normalized ``from_value``s — the threshold input
    (ADR-0043 D2). N copies of the same pair are ONE observation; LLM
    confidence plays no role. Pure."""
    return len({
        _norm_value(r.get("from_value"))
        for r in rows or []
        if _norm_value(r.get("from_value"))
    })


def observe_pending_value_mapping(item: dict) -> bool:
    """Observe a newly stored pending VM and auto-run the sweep at threshold.

    The first call primes all field-pair counts with ONE cached pending snapshot;
    subsequent writes update that in-memory view. Returns ``True`` only when this
    call claimed the pair's one process-lifetime trigger. The sweep is
    best-effort and keeps generated rules pending by its existing contract.
    """
    global _observed_pending_values
    if (item.get("proposed_type") != "value_mapping"
            or item.get("status") != "pending"
            or item.get("quarantined") == "true"):
        return False

    item_groups = group_value_mappings([item])
    if not item_groups:
        return False
    key = next(iter(item_groups))

    if _observed_pending_values is None:
        from .self_learning import get_pending_mappings
        snapshot = group_value_mappings(get_pending_mappings())
        _observed_pending_values = {
            pair: {_norm_value(r.get("from_value")) for r in rows
                   if _norm_value(r.get("from_value"))}
            for pair, rows in snapshot.items()
        }

    values = _observed_pending_values.setdefault(key, set())
    normalized = _norm_value(item.get("from_value"))
    if normalized:
        values.add(normalized)
    if len(values) < DEFAULT_MIN_CONFIRMATIONS or key in _triggered_field_pairs:
        return False

    _triggered_field_pairs.add(key)
    logger.debug(
        "value promotion: auto-triggering sweep for field pair %s at %d confirmations",
        key, len(values),
    )
    try:
        sweep_value_promotion()
    except Exception:  # defensive: proposal logging must remain available
        logger.warning(
            "value promotion: automatic sweep trigger failed for %s", key,
            exc_info=True,
        )
    return True


def _is_conditional_check_failed(exc: Exception) -> bool:
    """True for the DynamoDB ``ConditionalCheckFailedException``.

    boto3 raises a ``ClientError`` subclass whose CLASS NAME is
    ``ConditionalCheckFailedException`` and whose ``response`` carries the
    error code; offline fakes raise a plain class of the same name. Pure."""
    if exc.__class__.__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return (response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException")
    return False


# --------------------------------------------------------------------------- #
# The sweep (impure — DynamoDB via self_learning's seams; lazy imports)
# --------------------------------------------------------------------------- #
def _group_confidence(rows: list[dict]) -> float:
    """Max parseable per-row confidence (informational only — NOT the
    threshold), defaulting to 0.7 when none parses. Pure."""
    best = None
    for r in rows:
        try:
            c = float(r.get("confidence"))
        except (TypeError, ValueError):
            continue
        best = c if best is None else max(best, c)
    return best if best is not None else 0.7


def _log_promoted(common: dict, base_context: str, result: dict | None) -> str | None:
    """One promoted emit: ``join_path`` when a transform cleared the ladder,
    else a ``crosswalk`` candidate (exemplar pair carried). Lazy import keeps
    module import purity."""
    from .self_learning import log_proposed_mapping

    if result:
        return log_proposed_mapping(
            query_context=(f"{base_context}; transform={result['transform']}, "
                           f"overlap_count={result['overlap_count']}, "
                           f"ratio={result['ratio']}"),
            proposed_type="join_path", transform=result["transform"], **common)
    return log_proposed_mapping(
        query_context=(f"{base_context}; no transform cleared the ladder "
                       f"\u2014 crosswalk candidate (exemplar pair carried)"),
        proposed_type="crosswalk", **common)


def _promotion_emission(key: tuple, rows: list[dict],
                        confirmations: int) -> tuple[dict, str, dict | None] | None:
    """Build the ONE promoted emission for a threshold-clearing group
    (ADR-0043 D3): ``(common kwargs, base_context, transform-result)`` —
    a non-``None`` result means a transform cleared the ladder (``join_path``),
    else ``crosswalk``. Returns ``None`` when the group holds no aligned
    ``(from_value, to_value)`` pairs (nothing emittable). Emissions carry
    ``provenance="value_promotion"`` — the promoted artifact already holds the
    group's ≥ N-value evidence, so it BYPASSES the ADR-0051 P5 staging
    buffer."""
    from .join_discovery import discover_transform

    from_system, from_field, to_system, to_field = key
    aligned = [r for r in rows
               if str(r.get("from_value") or "").strip()
               and str(r.get("to_value") or "").strip()]
    if not aligned:
        return None
    exemplar = aligned[0]
    from_values = sorted({str(r.get("from_value")).strip() for r in aligned})
    to_values = sorted({str(r.get("to_value")).strip() for r in aligned})
    result = discover_transform(from_values, to_values)
    concept = next((str(r.get("concept")).strip() for r in rows
                    if str(r.get("concept") or "").strip()), "")
    base_context = (
        f"Promoted from {len(rows)} pending per-value mapping(s) with "
        f"{confirmations} distinct confirming from_values "
        f"(ADR-0043 per-value \u2192 rule promotion)")
    common = dict(
        from_system=from_system, from_field=from_field,
        from_value=str(exemplar.get("from_value")).strip(),
        to_system=to_system, to_field=to_field,
        to_value=str(exemplar.get("to_value")).strip(),
        concept=concept, confidence=_group_confidence(rows),
        source="agent", provenance="value_promotion",
    )
    return common, base_context, result


def _existing_rule_id(common: dict, result: dict | None) -> str | None:
    """Post-``None``-emit dedup probe: the id of the EXISTING equivalent rule
    this emission deduped against (the store's own canonical grains —
    ``self_learning.find_existing_equivalent_rule``), or ``None`` when the
    drop was a genuine gate/hold. Read-only and best-effort — never claims
    satisfaction on uncertainty (a ``None`` here leaves the rows pending)."""
    from .self_learning import find_existing_equivalent_rule

    return find_existing_equivalent_rule(
        "join_path" if result else "crosswalk",
        from_system=common["from_system"], from_field=common["from_field"],
        from_value=common["from_value"], to_system=common["to_system"],
        to_field=common["to_field"], to_value=common["to_value"],
        concept=common["concept"],
        transform=(result or {}).get("transform", ""))


def _promote_group(key: tuple, rows: list[dict],
                   confirmations: int) -> tuple[str, str | None]:
    """Promote ONE threshold-clearing group. Returns ``(outcome, rule_id)``:

      * ``("promoted", new_id)`` — a fresh rule proposal was emitted (pending);
      * ``("already_promoted", existing_id)`` — the emit deduped against an
        EXISTING equivalent rule (same canonical target grain, or a covering
        accepted rule): the promotion is ALREADY SATISFIED — the caller
        subsumes the per-value rows against ``existing_id``;
      * ``("gated", None)`` — a genuine gate drop/hold (or nothing alignable):
        rows stay pending, but the group is COUNTED so the operator sees it.

    A steward rejection (damping/tombstone) is NOT an equivalent rule — those
    groups report ``gated`` and never subsume (ADR-0053: the machine never
    writes ``status=rejected``, and a human rejection is not satisfaction)."""
    emission = _promotion_emission(key, rows, confirmations)
    if emission is None:
        logger.info(
            "value promotion: group %s met the threshold but has no "
            "aligned (from_value, to_value) pairs — leaving pending", key)
        return ("gated", None)
    common, base_context, result = emission
    new_id = _log_promoted(common, base_context, result)
    if new_id:
        return ("promoted", new_id)
    existing_id = _existing_rule_id(common, result)
    if existing_id:
        logger.info(
            "value promotion: emit for group %s deduped against existing "
            "rule %s — subsuming as already promoted", key, existing_id)
        return ("already_promoted", existing_id)
    logger.info(
        "value promotion: emit for group %s was dropped/held (gated) — "
        "leaving the per-value rows pending", key)
    return ("gated", None)


def _subsume_group(rows: list[dict], new_id: str) -> int:
    """Move a promoted group's per-value rows to ``superseded`` (ADR-0043 D4).

    One timestamp per group (the ``_supersede`` idiom); each flip is
    CONDITIONAL on the row STILL being pending (S-B): an accept/reject landing
    between snapshot and subsume WINS — flipping an accepted row would orphan
    its live Neptune node from ``reset_all_accepted`` bookkeeping (ADR-0003),
    and flipping a rejected row would pull it out of ADR-0042 damping.
    Best-effort per row; returns the number of rows subsumed."""
    from .self_learning import _get_table, _update_status

    table = _get_table()
    subsumed_at = datetime.now(timezone.utc).isoformat()
    subsumed = 0
    for r in rows:
        try:
            _update_status(
                table, r["mapping_id"], r["discovered_at"], "superseded",
                extra={"subsumed_by": new_id, "superseded_at": subsumed_at},
                expect_status="pending",
            )
            subsumed += 1
        except Exception as exc:  # noqa: BLE001 - best-effort per row
            if _is_conditional_check_failed(exc):
                logger.info(
                    "value promotion: row %s changed status between snapshot "
                    "and subsume — skipping (the accept/reject decision wins; "
                    "not counted as subsumed)", r.get("mapping_id"),
                )
                continue
            logger.warning(
                "value promotion: failed to subsume row %s into %s",
                r.get("mapping_id"), new_id, exc_info=True,
            )
    return subsumed


def sweep_value_promotion(min_confirmations: int = DEFAULT_MIN_CONFIRMATIONS) -> dict:
    """Promote accumulated per-value observations into single rule proposals.

    The ``sweep_auto_accept`` shape: read ``get_pending_mappings()``, group by
    the field-pair grain, and for each group holding at least
    ``min_confirmations`` distinct ``from_value``s promote it
    (``_promote_group``) and subsume the group (``_subsume_group`` —
    conditional flips, decisions win). An emit that deduped against an
    EXISTING equivalent rule subsumes against that rule's id and counts as
    ``already_promoted``; a genuine gate drop/hold leaves the rows pending and
    counts as ``gated`` (visible, never silent). NEVER accepts anything
    (ADR-0043 D5 — no accept call by construction). Per-group best-effort —
    one bad group never aborts the sweep. Returns ``{"promoted", "subsumed",
    "skipped_below_threshold", "already_promoted", "gated"}`` (the last two
    ADDITIVE keys).
    """
    from .self_learning import get_pending_mappings

    counts = {"promoted": 0, "subsumed": 0, "skipped_below_threshold": 0,
              "already_promoted": 0, "gated": 0}
    pending = [
        m for m in get_pending_mappings()
        if m.get("quarantined") != "true"  # ADR-0042 D2: frozen until HUMAN review
    ]
    groups = group_value_mappings(pending)
    for key in sorted(groups):
        try:
            # Deterministic order: oldest observation first (exemplar stability).
            rows = sorted(
                groups[key],
                key=lambda r: (r.get("discovered_at", ""), r.get("mapping_id", "")),
            )
            confirmations = independent_confirmations(rows)
            if confirmations < min_confirmations:
                counts["skipped_below_threshold"] += 1
                continue
            outcome, rule_id = _promote_group(key, rows, confirmations)
            counts[outcome] += 1
            if rule_id:
                counts["subsumed"] += _subsume_group(rows, rule_id)
        except Exception:  # noqa: BLE001 - best-effort per group (sweep shape)
            logger.warning(
                "value promotion failed for group %s — continuing sweep",
                key, exc_info=True,
            )
    return counts
