# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Learning-health stat builders (ADR-0052 B1/B5 — the SHARED module).

Computes the pinned ``GET /api/admin/learning/health`` shape (and, in a later
wave, the ``/digest`` shape) from the proposals table, the artifact-usage
ledger, and the response-integrity config. Both the B1 audit script
(``scripts/audit_learning.py``, via the HTTP endpoint) and the B5 steward
digest consume the SAME functions here — never duplicated queries.

Design rules (mirrors ``artifact_usage.py``):

* **Best-effort / SWALLOWS** (ADR-0003 F bulk category): every backend
  failure degrades to zeros / empty collections, never raises — a health
  read must never take the app down.
* **Import purity** (ADR 0020/0021): no boto3/pg8000/graph imports at
  module import time — ``self_learning``, ``artifact_usage``, and ``config``
  are imported lazily inside the functions.
* **Staging rows are NOT pending** (ADR-0051 P5): ``staged#``-prefixed rows
  carry no ``status`` attribute and are structurally invisible to every
  status-GSI read; the census here scans by mapping_id PREFIX (the
  ``artifact_usage._scan_ledger_rows`` bounded-scan idiom) and its counts
  are reported SEPARATELY — never folded into any pending number.
* **Pure where possible**: ``pending_age_days`` / ``quarantine_stats`` take
  already-fetched proposal rows (``self_learning.get_all_mappings()``
  output) so they unit-test fully offline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import concept_authority

logger = logging.getLogger(__name__)

# Bounded staged-row scan: begins_with prefix, page cap (the
# artifact_usage._SCAN_PAGE_CAP idiom — a scan must never spin unbounded).
_STAGING_SCAN_PAGE_CAP = 20

# Pre-B3 ledger rows carry no evidence class; "tool-text" is the honest
# default — the legacy extraction path was the only writer of those rows.
_LEGACY_EVIDENCE_CLASS = "tool-text"


def integrity_mode() -> str:
    """Echo the Mode-A response-integrity mode.

    Delegates to ``agents.config.response_integrity_mode()`` — the single
    source of truth per the project's Mode-A invariant. Lets the health
    consumer report a 0-row usage ledger as honest-expected (report-only)
    versus a regression (enforce).
    """
    try:
        from .config import response_integrity_mode
        return response_integrity_mode()
    except Exception as e:  # noqa: BLE001 - health reads never raise
        logger.warning(f"integrity_mode read failed (non-critical): {e}")
        return ""


def _scan_staged_rows(table, prefix: str) -> list[dict]:
    """Bounded scan of staged rows: ``begins_with(mapping_id, prefix)``,
    page cap ``_STAGING_SCAN_PAGE_CAP`` (never spins). Fail-loud — the
    caller (``staging_census``) owns the swallow contract."""
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": "begins_with(mapping_id, :prefix)",
        "ExpressionAttributeValues": {":prefix": prefix},
    }
    for _ in range(_STAGING_SCAN_PAGE_CAP):
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


def staging_census() -> dict[str, int]:
    """Count the ADR-0051 P5 staging-buffer rows + their distinct subjects.

    Staged rows carry NO ``status`` attribute (structurally invisible to the
    status GSI), so the census scans by the ``staged#`` mapping_id prefix.
    Reported SEPARATELY from pending — never counted as review pressure.
    Best-effort: zeros on any backend failure.
    """
    try:
        from . import self_learning
        table = self_learning._get_table()
        rows = _scan_staged_rows(table, self_learning._STAGED_PREFIX)
        subjects = {str(r.get("subject_key") or "") for r in rows}
        subjects.discard("")
        return {"rows": len(rows), "distinct_subjects": len(subjects)}
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.warning(f"staging_census failed (non-critical): {e}")
        return {"rows": 0, "distinct_subjects": 0}


def usage_ledger_stats(days: int = 30) -> dict[str, Any]:
    """Usage-ledger rollup over the trailing ``days`` window.

    Wraps ``artifact_usage.read_artifact_usage`` (its 60 s cache included).
    ``rows_30d`` — the SINGLE total of recorded uses across artifacts;
    ``artifacts`` — distinct artifact ids; ``evidence_classes`` — the sorted
    union of classes seen, reported as FLAGS, never per-class counts:
    the ledger's day buckets are last-write-wins on the
    ``evidence`` attr and the per-artifact read model unions classes across
    buckets, so an honest per-class partition of ``rows_30d`` is not
    recoverable — the old ``by_evidence`` rollup double-counted multi-class
    artifacts. Entries without ``evidence_classes`` (every pre-B3 row) fold
    under ``"tool-text"``, the honest default since the legacy extraction
    was the only writer. Best-effort: zeros on any backend failure.
    """
    try:
        from . import artifact_usage
        usage = artifact_usage.read_artifact_usage(days) or {}
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.warning(f"usage_ledger_stats read failed (non-critical): {e}")
        usage = {}
    rows_total = 0
    classes: set[str] = set()
    for entry in usage.values():
        try:
            count = int(entry.get("usage_30d", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        rows_total += count
        classes.update(
            str(cls) for cls in
            (entry.get("evidence_classes") or [_LEGACY_EVIDENCE_CLASS]))
    return {"rows_30d": rows_total, "artifacts": len(usage),
            "evidence_classes": sorted(classes)}


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO timestamp; naive values are treated as UTC. Pure."""
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def pending_age_days(rows: Iterable[dict],
                     now: datetime | None = None) -> list[float]:
    """Ages (in days) of the PENDING proposal rows, from ``discovered_at``.

    PURE over already-fetched rows (``self_learning.get_all_mappings()``
    output). Non-pending rows, ``staged#``-prefixed rows (defense in depth —
    ``get_all_mappings`` already filters them), and rows with unparseable
    timestamps are skipped. Returns ascending ages rounded to 2 decimals.
    """
    now = now or datetime.now(timezone.utc)
    ages: list[float] = []
    for row in rows or []:
        if row.get("status") != "pending":
            continue
        if str(row.get("mapping_id") or "").startswith("staged#"):
            continue
        discovered = _parse_iso(row.get("discovered_at"))
        if discovered is None:
            continue
        age = (now - discovered).total_seconds() / 86400.0
        ages.append(round(max(age, 0.0), 2))
    return sorted(ages)


def quarantine_stats(rows: Iterable[dict]) -> dict[str, int]:
    """Count PENDING rows carrying the ``quarantined == "true"`` string attr
    (the ADR-0042 D2 stamp). PURE over already-fetched rows."""
    count = sum(
        1 for row in rows or []
        if row.get("status") == "pending"
        and str(row.get("quarantined") or "").strip().lower() == "true"
    )
    return {"pending_quarantined": count}


def guidance_budget_stats(entries: Iterable[dict], *,
                          available: bool = True) -> dict[str, Any]:
    """Aggregate payload-safe, report-only guidance budget annotations."""
    rows = [row for row in (entries or []) if isinstance(row, dict)]
    overages: list[dict[str, Any]] = []
    for row in rows:
        try:
            over_by = max(0, int(row.get("over_by", 0) or 0))
        except (TypeError, ValueError):
            over_by = 0
        if bool(row.get("over_budget")) or over_by > 0:
            overages.append({"system_id": str(row.get("system_id") or ""),
                             "over_by": over_by})
    return {
        "available": available,
        "systems": len(rows) if available else None,
        "over_budget": bool(overages),
        "over_by": max((row["over_by"] for row in overages), default=0),
        "over_budget_systems": overages,
    }


def read_guidance_budget_stats() -> dict[str, Any]:
    """Read guidance budget facts without degrading unrelated health facts."""
    rows = _read_guidance_rows()
    return guidance_budget_stats(rows or [], available=rows is not None)


def _read_guidance_rows() -> "list[dict] | None":
    """ONE durable-guidance read shared by both annotations; ``None`` = unavailable."""
    try:
        from . import self_learning
        return self_learning.get_all_query_guidance(strict=True)
    except Exception as exc:  # noqa: BLE001 - annotation read never raises
        logger.warning("guidance budget read failed (non-critical): %s",
                       type(exc).__name__)
        return None


#: Hard cap on the per-system delivery-truncation list (payload-bounded).
_DELIVERY_SYSTEM_LIST_CAP = 20


def _delivery_document(row: dict, names=None) -> dict:
    """Rebuild, from one ``get_all_query_guidance`` row, the v1 guidance
    document the route seed shows the orchestrator (only the query hints that
    name none of ``names`` stay: the system agent gets every hint with every
    question)."""
    from .guidance_scope import empty_document, orchestrator_document
    document = empty_document(str(row.get("base") or ""))
    for name in ("query_hints", "answer_requirements", "legacy_unscoped"):
        values = row.get(name)
        document[name] = ([value for value in values if isinstance(value, str)]
                          if isinstance(values, list) else [])
    return orchestrator_document(document, names)


def _read_hint_schema_names(rows) -> dict:
    """The table and column names the route seed decides hint delivery with
    (``guidance_scope.read_schema_names``, the same bounded read of the
    hinted systems), so this projection fits the document the seed shows.
    Read only when a row carries query hints; a failed read leaves every hint
    reference-only, as in the seed."""
    hinted = {str(row.get("system_id")) for row in rows or []
              if isinstance(row, dict) and row.get("system_id") and row.get("query_hints")}
    if not hinted:
        return {}
    try:
        from .db import neptune_query
        from .guidance_scope import read_schema_names
        return read_schema_names(neptune_query, hinted)
    except Exception as exc:  # noqa: BLE001 - annotation read never raises
        logger.warning("hint schema names read failed (non-critical): %s", type(exc).__name__)
        return {}


def guidance_delivery_stats(entries: Iterable[dict], *,
                            available: bool = True,
                            schema_names: "dict | None" = None) -> dict[str, Any]:
    """Project the PROMPT-DELIVERY cap over durable guidance — annotate-only.

    The store retains an over-budget document VERBATIM (report-only, ADR-0063
    D7 — nothing is evicted), so the loss a steward actually feels happens later,
    when each rendered section is capped on the way into the prompt. This runs
    the SAME pure fit the orchestrator's delivery path runs over the SAME rows
    ``guidance_budget_stats`` reads, so the fact is VISIBLE next to the budget
    overage instead of being buried in a runtime log. Deterministic, read-only,
    never gates, and writes NOTHING to the guidance store. ``schema_names``
    (:func:`_read_hint_schema_names`) are the names the seed shows hints by.
    """
    from .guidance_scope import DELIVERY_FIELD_CAP, fit_delivery_sections
    rows = [row for row in (entries or []) if isinstance(row, dict)]
    truncated: list[dict[str, Any]] = []
    notes_dropped = 0
    chars_dropped = 0
    for row in rows:
        names = (schema_names or {}).get(str(row.get("system_id") or ""))
        _sections, report = fit_delivery_sections(_delivery_document(row, names))
        if not report["truncated"]:
            continue
        notes_dropped += report["notes_dropped"]
        chars_dropped += report["chars_dropped"]
        if len(truncated) < _DELIVERY_SYSTEM_LIST_CAP:
            truncated.append({
                "system_id": str(row.get("system_id") or ""),
                "fields": list(report["fields"]),
                "notes_dropped": report["notes_dropped"],
                "notes_truncated": report["notes_truncated"],
                "chars_dropped": report["chars_dropped"],
            })
    return {
        "available": available,
        "systems": len(rows) if available else None,
        "field_cap": DELIVERY_FIELD_CAP,
        "truncated": bool(truncated),
        "notes_dropped": notes_dropped,
        "chars_dropped": chars_dropped,
        "truncated_systems": truncated,
    }


_AUTHORITY_RULE_EVIDENCE_CYPHER = (
    "MATCH (ar:AuthorityRule) "
    "WHERE coalesce(ar.status, '') <> 'deactivated' "
    "OPTIONAL MATCH (mapped:System)"
    f"{concept_authority.FIELD_EVIDENCE_TRAVERSAL}(c:Concept) "
    "WHERE c.id = ar.concept "
    "RETURN ar.id AS rule_id, ar.system_id AS system_id, "
    "ar.concept AS concept, mapped.id AS mapped_system_id, "
    "mapped.name AS mapped_system_name"
)


def authority_rule_evidence_stats(rows: Iterable[dict], *,
                                  available: bool = True) -> dict[str, Any]:
    """Compute active AuthorityRule evidence orphans from graph-read rows.

    PURE and deterministic.  A rule is evidenced only when its claiming
    ``system_id`` has a FIELD-level ``MAPS_TO`` edge to its concept.  Entity
    mappings do not count.  Other mapped systems are named for steward repair;
    an empty list honestly means no connected system maps the concept.
    """
    if not available:
        return {"available": False, "active_rules": None,
                "orphan_count": None, "evidence_orphans": []}
    rules: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        rule_id = str(row.get("rule_id") or "").strip()
        if not rule_id:
            continue
        rule = rules.setdefault(rule_id, {
            "rule_id": rule_id,
            "system_id": str(row.get("system_id") or "").strip(),
            "concept": str(row.get("concept") or "").strip(),
            "mapped_systems": {},
        })
        mapped_id = str(row.get("mapped_system_id") or "").strip()
        if mapped_id:
            mapped_name = str(row.get("mapped_system_name") or "").strip()
            rule["mapped_systems"][mapped_id] = mapped_name or mapped_id
    orphans = []
    for rule_id in sorted(rules):
        rule = rules[rule_id]
        mapped = rule.pop("mapped_systems")
        if rule["system_id"] in mapped:
            continue
        rule["mapped_systems"] = [
            {"system_id": sid, "system_name": mapped[sid]}
            for sid in sorted(mapped)]
        orphans.append(rule)
    return {"available": True, "active_rules": len(rules),
            "orphan_count": len(orphans), "evidence_orphans": orphans}


def read_graph_integrity() -> dict[str, Any]:
    """Read-only graph integrity section; backend failure is unavailable."""
    try:
        from .db import neptune_query
        rows = neptune_query(_AUTHORITY_RULE_EVIDENCE_CYPHER, {})
        stats = authority_rule_evidence_stats(rows)
    except Exception as exc:  # noqa: BLE001 - health reads never raise
        logger.warning("authority evidence read failed (non-critical): %s",
                       type(exc).__name__)
        stats = authority_rule_evidence_stats([], available=False)
    return {"authority_rule_evidence": stats}


def zero_health() -> dict[str, Any]:
    """The degraded (all-zeros) health shape — same pinned keys, honest
    ``integrity_mode`` echo (an env read, effectively infallible)."""
    return {
        "integrity_mode": integrity_mode(),
        "usage_ledger": {"rows_30d": 0, "artifacts": 0,
                         "evidence_classes": []},
        "staging": {"rows": 0, "distinct_subjects": 0},
        "pending_ages_days": [],
        "quarantine": {"pending_quarantined": 0},
        "guidance_budget": guidance_budget_stats([], available=False),
        "guidance_delivery": guidance_delivery_stats([], available=False),
        "graph_integrity": {
            "authority_rule_evidence": authority_rule_evidence_stats(
                [], available=False),
        },
    }


def build_health() -> dict[str, Any]:
    """Assemble the pinned ``GET /api/admin/learning/health`` response.

    BLOCKING (DynamoDB reads) — the route calls it off-loop via
    ``asyncio.to_thread``. Each stat builder swallows its own failures; the
    outer try degrades the whole shape to ``zero_health()`` so a health read
    can never 500.
    """
    try:
        from . import self_learning
        mappings = self_learning.get_all_mappings()
        guidance_rows = _read_guidance_rows()
        return {
            "integrity_mode": integrity_mode(),
            "usage_ledger": usage_ledger_stats(30),
            "staging": staging_census(),
            "pending_ages_days": pending_age_days(mappings),
            "quarantine": quarantine_stats(mappings),
            "guidance_budget": guidance_budget_stats(
                guidance_rows or [], available=guidance_rows is not None),
            # The DELIVERY half of the same fact: the store keeps an
            # over-budget document verbatim, the prompt cap does not.
            "guidance_delivery": guidance_delivery_stats(
                guidance_rows or [], available=guidance_rows is not None,
                schema_names=_read_hint_schema_names(guidance_rows)),
            "graph_integrity": read_graph_integrity(),
        }
    except Exception as e:  # noqa: BLE001 - a health read never 500s
        logger.warning(f"build_health failed (non-critical): {e}")
        return zero_health()


# Distinct systems sharing each concept — BOTH mapping layers in ONE read:
# field-level (Field-[:MAPS_TO]->Concept) UNION entity-level
# (SchemaEntry-[:MAPS_TO]->Concept). The blast-radius input for the
# consequence score. SHARED home: the review route
# (``server._concept_system_counts``) and the digest (``build_digest``) rank
# with the SAME inputs — never two divergent fan-out reads.
_CONCEPT_SYSTEM_COUNT_CYPHER = (
    "MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(:Field)"
    "-[:MAPS_TO]->(c:Concept) WHERE c.id IN $ids "
    "RETURN c.id AS id, collect(DISTINCT s.id) AS systems "
    "UNION "
    "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry)-[:MAPS_TO]->(c:Concept) "
    "WHERE c.id IN $ids "
    "RETURN c.id AS id, collect(DISTINCT s.id) AS systems"
)


def concept_system_counts(concept_ids, query=None) -> dict[str, int]:
    """Count catalog systems sharing each concept — ONE graph read for ALL ids.

    Batched (``c.id IN $ids``, no N+1). ``query`` is the injectable graph
    read (the server route passes its module-level ``neptune_query`` so its
    test seam holds); default is the lazy ``db.neptune_query`` (module
    import-purity rule). Best-effort: ``neptune_query`` never raises ([] on
    failure) and an empty result folds to ``{}`` — every consequence score
    then degrades to endpoint-systems-only blast radius, never a 500."""
    ids = sorted(c for c in (concept_ids or ()) if c)
    if not ids:
        return {}
    try:
        if query is None:
            from .db import neptune_query as query
        folded: dict[str, set] = {}
        for row in (query(_CONCEPT_SYSTEM_COUNT_CYPHER, {"ids": ids}) or []):
            cid = row.get("id")
            if cid:
                folded.setdefault(cid, set()).update(
                    s for s in (row.get("systems") or []) if s)
        return {cid: len(sids) for cid, sids in folded.items()}
    except Exception as e:  # noqa: BLE001 - a fan-out read never raises
        logger.warning(f"concept_system_counts failed (non-critical): {e}")
        return {}


# --------------------------------------------------------------------------- #
# B5 steward digest (ADR-0052) — the pinned
# ``GET /api/admin/learning/digest`` shape. Same design rules as the health
# builders above: best-effort/swallow, lazy impure imports, pure where
# possible. The consequence ranking DELEGATES to the shared Task-2.2 scorer
# in ``agents.proposal_scoring`` — never a duplicated formula here.
# --------------------------------------------------------------------------- #

_DIGEST_WORTH_REVIEW_LIMIT = 3
_DIGEST_TOP_ARTIFACTS_LIMIT = 5


def _scorer_fns():
    """Resolve the SHARED consequence scorer (Task 2.2, ``proposal_scoring``).

    Returns ``(consequence, provenance_tier)`` — the pinned B2 helpers
    (``consequence(mapping, concept_system_count)`` /
    ``provenance_tier(mapping)``). Raises when the scorer has not landed;
    the caller (``_worth_review``) owns the degrade-to-empty contract.
    """
    from . import proposal_scoring
    return proposal_scoring.consequence, proposal_scoring.provenance_tier


def _claim_text(row: dict) -> str:
    """One human-readable claim line for a proposal row. PURE, generic —
    built only from the row's own type/endpoint fields, no dataset literals
    of ours."""
    ptype = str(row.get("proposed_type") or "proposal")
    left = ".".join(p for p in (str(row.get("from_system") or ""),
                                str(row.get("from_field") or "")) if p)
    right = ".".join(p for p in (str(row.get("to_system") or ""),
                                 str(row.get("to_field") or "")) if p)
    if left and right:
        return f"{ptype}: {left} ↔ {right}"
    concept = str(row.get("concept") or "")
    subject = left or right or concept
    return f"{ptype}: {subject}" if subject else ptype


def _is_pending_row(row: dict) -> bool:
    """PENDING lifecycle row check with the staged#-prefix defense in depth
    (``get_all_mappings`` already filters staged rows — ADR-0051 P5)."""
    return (row.get("status") == "pending"
            and not str(row.get("mapping_id") or "").startswith("staged#"))


def _split_stale_system_rows(
    pending: list[dict], onboarded_ids: "set[str] | None",
) -> tuple[list[dict], int]:
    """Partition pending rows into (actionable, stale_count) by system liveness.

    A proposal whose ``from_system``/``to_system`` is no longer onboarded is
    UNREVIEWABLE — the steward cannot accept a mapping into a system that does
    not exist — so surfacing it as "worth review" is dishonest (observed live:
    demo-era rows from a prior data mode outliving the systems they named, and
    one rendering above real candidates). They are NOT deleted or hidden: the
    machine never writes terminal statuses (ADR-0053), so they are counted
    separately for the operator. ``onboarded_ids=None`` = liveness unknown ⇒ no
    filtering (degrade honestly rather than guess); empty-string system fields
    never count as stale.
    """
    if onboarded_ids is None:
        return pending, 0
    actionable: list[dict] = []
    stale = 0
    for row in pending:
        refs = {str(row.get(k) or "").strip()
                for k in ("from_system", "to_system")} - {""}
        if refs - onboarded_ids:
            stale += 1
        else:
            actionable.append(row)
    return actionable, stale


def _worth_review(pending_rows: Iterable[dict],
                  concept_counts: dict[str, int]) -> list[dict]:
    """Top-N pending rows by CONSEQUENCE (the shared 2.2 scorer). PURE over
    already-fetched rows. Degrades to ``[]`` (with a warning) when the
    shared scorer is unavailable — never a duplicated local formula."""
    try:
        consequence_fn, tier_fn = _scorer_fns()
    except Exception as e:  # noqa: BLE001 - scorer lands in task 2.2
        logger.warning(f"consequence scorer unavailable (non-critical): {e}")
        return []
    scored: list[dict] = []
    for row in pending_rows or []:
        concept = str(row.get("concept") or "")
        try:
            score = float(consequence_fn(row, int(concept_counts.get(concept, 0))))
            tier = int(tier_fn(row))
        except Exception as e:  # noqa: BLE001 - one bad row never sinks the digest
            logger.warning(f"consequence scoring failed for a row (non-critical): {e}")
            continue
        scored.append({
            "mapping_id": str(row.get("mapping_id") or ""),
            "claim": _claim_text(row),
            "consequence": round(score, 2),
            "provenance_tier": tier,
        })
    scored.sort(key=lambda item: (-item["consequence"], item["mapping_id"]))
    return scored[:_DIGEST_WORTH_REVIEW_LIMIT]


def _expired_unreviewed(rows: Iterable[dict]) -> int:
    """Insight findings that hit the 30d pending TTL without steward review.

    Counts rows already lazily swept to the terminal ``expired`` status PLUS
    pending insight rows currently past the TTL but not yet swept — the SAME
    semantics as ``self_learning._is_stale_insight_pending`` (reused, never
    re-implemented). Best-effort: falls back to the swept count alone."""
    rows = list(rows or [])
    swept = sum(1 for r in rows if r.get("status") == "expired")
    try:
        from .self_learning import _is_stale_insight_pending
        return swept + sum(1 for r in rows if _is_stale_insight_pending(r))
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.warning(f"stale-insight check unavailable (non-critical): {e}")
        return swept


def _top_artifacts(usage: dict[str, dict],
                   limit: int = _DIGEST_TOP_ARTIFACTS_LIMIT) -> list[dict]:
    """Top-``limit`` ledger artifacts by ``usage_30d``. PURE over the
    ``artifact_usage.read_artifact_usage`` shape; entries without
    ``evidence_classes`` (every pre-B3 row) fold under ``"tool-text"`` —
    the same honest default as ``usage_ledger_stats``."""
    entries: list[dict] = []
    for artifact_id, entry in (usage or {}).items():
        try:
            count = int(entry.get("usage_30d", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        entries.append({
            "artifact_id": str(entry.get("artifact_id") or artifact_id),
            "kind": str(entry.get("kind") or "rule"),
            "usage_30d": count,
            "evidence_classes": [str(c) for c in
                                 (entry.get("evidence_classes")
                                  or [_LEGACY_EVIDENCE_CLASS])],
        })
    entries.sort(key=lambda e: (-e["usage_30d"], e["artifact_id"]))
    return entries[:limit]


def _pending_drift_rows(rows: Iterable[dict]) -> list[dict]:
    """PENDING ``proposed_type=="schema_drift"`` lifecycle rows (staged#
    defense in depth via ``_is_pending_row``). PURE."""
    return [r for r in rows or []
            if _is_pending_row(r)
            and str(r.get("proposed_type") or "") == "schema_drift"]


def drift_stats(rows: Iterable[dict],
                now: datetime | None = None) -> dict[str, Any]:
    """The pinned digest ``drift`` section — unacknowledged schema-drift
    pressure (ADR-0053 E5: THE read path for the drift writer).

    PURE over already-fetched rows: ``pending`` counts pending schema_drift
    proposals, ``oldest_age_days`` from ``discovered_at`` (``None`` when no
    pending row carries a parseable timestamp — never a fabricated zero),
    ``by_kind`` buckets on the pinned ``reason`` attr
    (column_added/column_removed/…; ``"unknown"`` for rows without one).
    """
    now = now or datetime.now(timezone.utc)
    pending = _pending_drift_rows(rows)
    ages: list[float] = []
    by_kind: dict[str, int] = {}
    for row in pending:
        kind = str(row.get("reason") or "").strip() or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        discovered = _parse_iso(row.get("discovered_at"))
        if discovered is not None:
            age = (now - discovered).total_seconds() / 86400.0
            ages.append(round(max(age, 0.0), 2))
    return {"pending": len(pending),
            "oldest_age_days": max(ages) if ages else None,
            "by_kind": by_kind}


def cost_trend_section(trend_rows: "list[dict] | None") -> "dict[str, Any] | None":
    """The pinned digest ``cost_trend`` section from
    ``cost_ledger.read_cost_trend`` rows (ADR-0053 E7 read path).

    PURE. Returns ``None`` when there is nothing measured (absence = absent
    — the reader must never see fabricated zeros; ``read_cost_trend``
    already folds backend failure to ``[]``). Days with no turns are
    skipped (a bump implies a turn — a 0-turn day is not a measurement).
    ``avg_total_tokens`` = (input+output)/turns, rounded to an int.
    """
    days: list[dict[str, Any]] = []
    for row in trend_rows or []:
        try:
            turns = int(row.get("turns", 0))
            total = (int(row.get("input_tokens", 0))
                     + int(row.get("output_tokens", 0)))
        except (TypeError, ValueError):
            continue
        if turns <= 0:
            continue
        days.append({"date": str(row.get("date") or ""), "turns": turns,
                     "avg_total_tokens": round(total / turns)})
    if not days:
        return None
    return {"days": days, "scope": "orchestrator-model-only"}


# --------------------------------------------------------------------------- #
# D3 miner precision (ADR-0063) — the steward's quality signal ABOUT the miner.
#
# COMPUTED from persisted steward decisions only (the accepted/rejected
# lifecycle stamps): no LLM, no inference, decisions or nothing. REPORT-ONLY —
# nothing in the filing, accept or auto-accept path reads it, and it never
# gates. Measured precedent (2026-08-18): a steward pass adjudicated 101
# pending cross-system proposals, 7 accepted / 48 rejected — a ~13% accept rate
# that NO surface reported, so a miner could degrade indefinitely unnoticed.
# --------------------------------------------------------------------------- #

#: Bounded recent window for the precision block, in days.
_PRECISION_WINDOW_DAYS = 30

#: Below this many decisions the RATE key is ABSENT while the count still
#: shows — a small sample states its own smallness (ADR-0053 honesty), rather
#: than reporting a 1-of-2 pass as "50% precision".
_PRECISION_MIN_DECISIONS = 10

#: The two terminal steward verdicts and the stamp each one writes.
_PRECISION_STAMPS = {"accepted": "accepted_at", "rejected": "rejected_at"}

def _decision(row: dict, cutoff: datetime) -> "tuple[str, str] | None":
    """Exact recent steward verdict on one ADR-0058 machine artifact."""
    from .miner_contract import MINER_METHOD, is_miner_artifact

    if not is_miner_artifact(row) or str(row.get("requeued_at") or "").strip():
        return None
    outcome = str(row.get("status") or "")
    stamp = _PRECISION_STAMPS.get(outcome)
    if stamp is None:
        return None
    actor_key = "accepted_by" if outcome == "accepted" else "rejected_by"
    if not str(row.get(actor_key) or "").strip():
        return None
    decided = _parse_iso(row.get(stamp))
    if decided is None or decided < cutoff:
        return None
    return outcome, MINER_METHOD


def _precision_entry(accepted: int, rejected: int,
                     min_decisions: int) -> dict[str, Any]:
    """One precision entry: ALWAYS the decision count, the ``rate`` key only
    above the minimum-sample bar (key-absent = too few decisions to judge,
    never a zero and never a guess). PURE."""
    decisions = accepted + rejected
    entry: dict[str, Any] = {"decisions": decisions, "accepted": accepted}
    if decisions >= min_decisions:
        entry["rate"] = round(accepted / decisions, 3)
    return entry


def decision_precision(rows: Iterable[dict], now: datetime | None = None,
                       window_days: int = _PRECISION_WINDOW_DAYS,
                       min_decisions: int = _PRECISION_MIN_DECISIONS,
                       ) -> dict[str, Any]:
    """The pinned digest ``precision`` section (ADR-0063 D3) — PURE.

    Over a BOUNDED recent window, per ``method`` AND overall:
    ``accepted / (accepted + rejected)`` plus the decision count. Report-only
    (see the section header); the rate is ABSENT below ``min_decisions``.
    An empty window reports a MEASURED ``decisions: 0`` with no rate — the
    honest "nothing decided lately", distinct from ``zero_digest``'s absent
    key (unavailable).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(int(window_days), 0))
    totals = {"accepted": 0, "rejected": 0}
    by_method: dict[str, dict[str, int]] = {}
    for row in rows or []:
        decided = _decision(row, cutoff)
        if decided is None:
            continue
        outcome, method = decided
        totals[outcome] += 1
        by_method.setdefault(method, {"accepted": 0, "rejected": 0})
        by_method[method][outcome] += 1
    out = _precision_entry(totals["accepted"], totals["rejected"],
                           min_decisions)
    out["window_days"] = int(window_days)
    out["min_decisions"] = int(min_decisions)
    out["by_method"] = {
        method: _precision_entry(tally["accepted"], tally["rejected"],
                                 min_decisions)
        for method, tally in sorted(by_method.items())}
    return out


def digest(mappings: Iterable[dict], usage: dict[str, dict], staging: dict[str, int],
           concept_counts: dict[str, int] | None = None,
           cost_trend: "list[dict] | None" = None,
           onboarded_ids: "set[str] | None" = None,
           guidance_budget: "dict[str, Any] | None" = None) -> dict[str, Any]:
    """PURE assembly of the pinned ``/api/admin/learning/digest`` shape.

    ``mappings`` = ``self_learning.get_all_mappings()`` output, ``usage`` =
    ``artifact_usage.read_artifact_usage`` output, ``staging`` = the
    ``staging_census()`` shape (reported SEPARATELY — never pending).
    ``concept_counts`` is the concept→system-fan-out input to the shared
    consequence scorer — ``build_digest`` fills it from the SAME batched
    ``concept_system_counts`` read the B2 review endpoint uses (one
    ranking input set, never two); an empty/absent map degrades ranking to
    the scorer's mapping-local blast-radius component alone. ``cost_trend``
    = already-fetched ``cost_ledger.read_cost_trend`` rows; the section is
    ABSENT when nothing was measured (ADR-0053 honesty — as is the
    ``drift`` key from ``zero_digest``, distinguishing measured
    ``{"pending": 0}`` from key-absent unavailable). ``precision`` (ADR-0063
    D3) is computed from the SAME rows' terminal decision stamps and is
    REPORT-ONLY — no other digest field, and no accept/filing gate, reads it.
    """
    rows = list(mappings or [])
    pending = [r for r in rows if _is_pending_row(r)]
    pending, stale_refs = _split_stale_system_rows(pending, onboarded_ids)
    out = {
        "worth_review": _worth_review(pending, concept_counts or {}),
        "pending_total": len(pending),
        "expired_unreviewed": _expired_unreviewed(rows),
        "top_artifacts": _top_artifacts(usage or {}),
        "quarantine": quarantine_stats(rows),
        "staging": {
            "rows": int((staging or {}).get("rows", 0) or 0),
            "distinct_subjects": int(
                (staging or {}).get("distinct_subjects", 0) or 0),
        },
        "drift": drift_stats(rows),
        "precision": decision_precision(rows),
        "guidance_budget": (guidance_budget
                            if isinstance(guidance_budget, dict)
                            else guidance_budget_stats([], available=False)),
    }
    if onboarded_ids is not None:
        # Measured (possibly 0) only when liveness was checkable; key-absent =
        # unknown (same honesty convention as drift/cost_trend).
        out["stale_system_refs"] = stale_refs
    trend = cost_trend_section(cost_trend)
    if trend is not None:
        out["cost_trend"] = trend
    return out


def zero_digest() -> dict[str, Any]:
    """The degraded (all-zeros) digest shape — same pinned keys. The
    ``drift``, ``precision`` and ``cost_trend`` sections are deliberately
    ABSENT here: key-absent = unavailable, distinct from a measured
    ``{"pending": 0}`` / ``{"decisions": 0}`` (ADR-0053 honesty — never
    fabricated zeros)."""
    return {
        "worth_review": [],
        "pending_total": 0,
        "expired_unreviewed": 0,
        "top_artifacts": [],
        "quarantine": {"pending_quarantined": 0},
        "staging": {"rows": 0, "distinct_subjects": 0},
        "guidance_budget": guidance_budget_stats([], available=False),
    }


def build_digest() -> dict[str, Any]:
    """Assemble the pinned ``GET /api/admin/learning/digest`` response.

    BLOCKING (DynamoDB + one batched graph read) — the route calls it
    off-loop via ``asyncio.to_thread`` behind its 60 s cache. Consequence
    ranking uses the SAME concept fan-out inputs as the review list (one
    batched read per cache window, no N+1; degrades to ``{}`` so ranking falls back
    to endpoint-systems-only blast radius). The cost trend rides
    ``cost_ledger.read_cost_trend(7)`` (its own 60 s cache; ``[]`` on
    failure ⇒ the ``cost_trend`` key is honestly absent). Outer swallow
    degrades the whole shape to ``zero_digest()`` — a digest read can
    never 500.
    """
    try:
        from . import artifact_usage, cost_ledger, self_learning
        mappings = self_learning.get_all_mappings()
        usage = artifact_usage.read_artifact_usage(30) or {}
        counts = concept_system_counts(
            {str(r.get("concept") or "") for r in mappings
             if _is_pending_row(r)})
        return digest(mappings, usage, staging_census(),
                      concept_counts=counts,
                      cost_trend=cost_ledger.read_cost_trend(7),
                      onboarded_ids=_onboarded_system_ids(),
                      guidance_budget=read_guidance_budget_stats())
    except Exception as e:  # noqa: BLE001 - a digest read never 500s
        logger.warning(f"build_digest failed (non-critical): {e}")
        return zero_digest()


def _onboarded_system_ids() -> "set[str] | None":
    """Best-effort onboarded system ids for the stale-reference partition.

    ``None`` (never an empty set) on ANY failure: an unreadable catalog must
    degrade to "liveness unknown ⇒ no filtering", not to "every system is
    stale" — which an empty set would silently mean.
    """
    try:
        from .catalog import catalog
        ids = {str(s.get("id") or "").strip() for s in catalog.get_all()} - {""}
        return ids or None
    except Exception as e:  # noqa: BLE001 - filter is optional, digest is not
        logger.warning(f"catalog read for digest liveness failed (non-critical): {e}")
        return None
