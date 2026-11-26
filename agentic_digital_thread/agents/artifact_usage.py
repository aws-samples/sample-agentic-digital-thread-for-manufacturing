# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Artifact-usage extraction + day-bucketed usage ledger (ADR-0050 usage loop).

Sibling of the item-C utility loop (``usage_tracking`` /
``self_learning.record_rule_usage``): after a turn, the runtime hook extracts
which deterministic artifacts (accepted rules, concepts) the orchestrator
actually USED and bumps a day-bucketed counter so the vocabulary admin surface
can show "used in K answers (30d)".

Extraction sources
------------------
1. **Rule node ids** — the caller's ``precomputed_ids`` when the turn already
   ran the extraction (the runtime-hook idiom — never re-scan), else the
   EXISTING pure extractor ``usage_tracking.extract_rule_node_ids`` over the
   turn's tool-result text (never re-implemented here). The ref *kind* is the
   accepted row's ``proposed_type`` when resolvable (``self_learning.
   get_accepted_rules_by_node_ids`` — itself best-effort), else ``"rule"``.
2. **Concept ids — two TYPED paths, NEVER regex over free text** (concept
   ids are plain words):

   a. **Accepted rows — the production-live path.** Every accepted proposal
      row carries a typed ``concept`` attribute (the DynamoDB item shape
      ``self_learning`` persists and ``get_accepted_rules_by_node_ids``
      returns). A rule being used means its concept was used: each row whose
      ``neptune_node_id`` fired this turn emits a ``("concept", <id>)`` ref
      alongside its rule ref — live on exactly the turns where deterministic
      artifacts fire, honest signal, no fabrication.
   b. **Typed evidence — the richer path when a caller has one.** The ONLY
      typed concept field in the capture contracts is
      ``LearningProposal.concept`` (a validated pydantic field) on
      ``FinalAnswerDraft`` / ``FinalAnswer`` ``proposals`` — read from a
      ``proposals`` attribute/key on the optional ``evidence`` object. The
      shadow ``IntegrityContext`` itself carries no typed concept refs, and
      the default report-only runtime hook has no capture object in scope —
      it passes ``evidence=None`` and relies entirely on path (a).

Ledger
------
Day-bucketed rows on the EXISTING proposals DynamoDB table (via
``self_learning._get_table()`` — no new infrastructure):

    mapping_id    = "usage#<kind>#<id>"     (partition key)
    discovered_at = "YYYY-MM-DD"            (sort key = the day bucket)
    count         — atomic ``ADD count :one`` (absent-safe)
    last_used_at  — ISO timestamp of the latest bump in the bucket
    evidence      — ADDITIVE evidence class (ADR-0052 decision (b)):
                    "relationship-proof" / "tool-text" / "route-intersection",
                    plus the ADR-0066 D6 metric-definition delivery classes
                    "metric-resolve" / "term-precheck-resolved" and the
                    ADR-0078 D4 class "metric-turn-composed" (a graph
                    definition's metric-layer result was composed into a
                    chat turn) — every class
                    names a DELIVERY/COMPOSITION event, never obedience;
                    absent on pre-B3 rows, folded as "tool-text" on read

Rows deliberately carry **NO ``status`` attribute**, so every status-GSI read
(``get_all_mappings`` and friends) never sees them. Reads use a bounded scan
(``begins_with(mapping_id, "usage#")``, page cap 20 — the
``record_rule_usage`` idiom) behind a 60 s in-process cache.

Failure contract: best-effort / SWALLOWS (ADR-0003 F bulk category) — every
backend failure degrades to ``0`` / ``{}`` / empty set, never raises. Behavior
is identical with ``RESPONSE_INTEGRITY_MODE`` unset (shadow mode) — this
module never consults that env var.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import usage_tracking

logger = logging.getLogger(__name__)

_LEDGER_PREFIX = "usage#"
_SCAN_PAGE_CAP = 20
_READ_CACHE_TTL_SECONDS = 60.0

# Pre-B3 ledger rows carry no evidence class attr; on read they fold as
# "tool-text" — the honest default, since the legacy tool-result extraction
# was the only writer of those rows (ADR-0052 decision (b)).
_LEGACY_EVIDENCE_CLASS = "tool-text"

# In-process read cache: {days: (expires_at_monotonic, result)}.
_read_cache: dict[int, tuple[float, dict[str, dict[str, Any]]]] = {}
_read_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Extraction (pure aside from the best-effort accepted-row lookup)
# --------------------------------------------------------------------------- #
def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from an attribute OR a dict key (duck-typed evidence)."""
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return value


def _concept_ids_from_evidence(evidence: Any) -> set[str]:
    """Concept ids from TYPED fields only — never regex over free text.

    Reads ``LearningProposal``-shaped items on a ``proposals`` attribute/key
    (the ``FinalAnswerDraft``/``FinalAnswer`` shape;
    ``LearningProposal.concept`` is the one typed concept field in the
    capture contracts). ``None`` evidence yields nothing.
    """
    ids: set[str] = set()
    if evidence is None:
        return ids
    proposals = _field(evidence, "proposals")
    if isinstance(proposals, (list, tuple)):
        for proposal in proposals:
            concept = _field(proposal, "concept")
            if isinstance(concept, str) and concept.strip():
                ids.add(concept.strip())
    return ids


def _kinds_and_concepts_from_rows(
    node_ids: set[str], accepted_rows: list[dict] | None,
) -> tuple[dict[str, str], set[str]]:
    """Per-node kinds + fired-rule concept ids from the accepted rows.

    Rows come from the caller (the ``record_rule_usage(prefetched_rows=...)``
    idiom) or ONE best-effort ``self_learning.get_accepted_rules_by_node_ids``
    lookup. Only rows whose ``neptune_node_id`` is in ``node_ids`` (the rules
    that actually fired this turn) contribute — a used rule's typed
    ``concept`` attribute becomes a concept id. Best-effort: any failure
    yields whatever was collected so far.
    """
    kind_by_node: dict[str, str] = {}
    concepts: set[str] = set()
    try:
        if accepted_rows is None:
            from . import self_learning
            accepted_rows = self_learning.get_accepted_rules_by_node_ids(node_ids)
        for row in accepted_rows or []:
            node_id = row.get("neptune_node_id")
            if node_id not in node_ids:
                continue
            proposed_type = row.get("proposed_type")
            if isinstance(proposed_type, str) and proposed_type:
                kind_by_node[node_id] = proposed_type
            concept = row.get("concept")
            if isinstance(concept, str) and concept.strip():
                concepts.add(concept.strip())
    except Exception as e:  # row decoration is best-effort, never fatal
        logger.warning(f"artifact kind/concept lookup failed (non-critical): {e}")
    return kind_by_node, concepts


def extract_artifact_refs(
    tool_results: dict, evidence: object | None = None, *,
    accepted_rows: list[dict] | None = None,
    precomputed_ids: set[str] | None = None,
) -> set[tuple[str, str]]:
    """Return ``{(kind, id), ...}`` for every artifact used in this turn.

    - Rule node ids: ``precomputed_ids`` when the caller already ran the
      extraction this turn (the runtime-hook idiom — an EMPTY set is
      honored, no silent rescan), else the EXISTING
      ``usage_tracking.extract_rule_node_ids`` over the tool-result text.
    - Ref kinds + concept refs derive from the ACCEPTED ROWS whose rule
      fired (``_kinds_and_concepts_from_rows``): a used rule's typed
      ``concept`` attribute emits ``("concept", <id>)`` alongside the rule
      ref — the production-live concept path.
    - ``evidence`` is the richer typed source when a caller has one
      (``_concept_ids_from_evidence``); never regex over free text.

    Best-effort: any failure swallows to the refs collected so far (or an
    empty set) — never raises.
    """
    refs: set[tuple[str, str]] = set()
    try:
        if precomputed_ids is not None:
            node_ids = set(precomputed_ids)
        else:
            texts = [v for v in (tool_results or {}).values()]
            node_ids = usage_tracking.extract_rule_node_ids(texts)
        if node_ids:
            kind_by_node, concepts = _kinds_and_concepts_from_rows(
                node_ids, accepted_rows)
            for node_id in node_ids:
                refs.add((kind_by_node.get(node_id, "rule"), node_id))
            for concept_id in concepts:
                refs.add(("concept", concept_id))
        for concept_id in _concept_ids_from_evidence(evidence):
            refs.add(("concept", concept_id))
        return refs
    except Exception as e:
        logger.warning(f"extract_artifact_refs failed (non-critical): {e}")
        return refs


# --------------------------------------------------------------------------- #
# Ledger — day-bucketed counters on the proposals table
# --------------------------------------------------------------------------- #
def bump_artifact_usage(refs: Iterable[tuple[str, str]], evidence: str = "") -> int:
    """Atomically bump today's day bucket for each ``(kind, id)`` ref.

    One ``update_item`` per ref: ``ADD count :one`` (absent-safe) + ``SET
    last_used_at``. A non-empty ``evidence`` class (ADR-0052 decision (b):
    ``relationship-proof`` / ``tool-text`` / ``route-intersection``; ADR-0066
    D6 adds the metric-definition delivery classes ``metric-resolve`` /
    ``term-precheck-resolved``; ADR-0078 D4 adds ``metric-turn-composed``) is stored
    as an ADDITIVE attribute — never written when empty, so pre-B3 callers
    stay byte-identical. The row carries NO ``status`` attribute, so
    status-GSI reads never see it. Best-effort / SWALLOWS: returns the number
    of refs bumped, ``0`` on any backend failure — a metrics bump must never
    fail a live query.
    """
    refs = set(refs or ())
    if not refs:
        return 0
    try:
        from . import self_learning
        table = self_learning._get_table()
        now = datetime.now(timezone.utc)
        bucket = now.strftime("%Y-%m-%d")
        # "count" is a DynamoDB reserved word — aliased, but the stored
        # attribute IS literally named "count".
        update = "ADD #c :one SET last_used_at = :now"
        values: dict[str, Any] = {":one": 1, ":now": now.isoformat()}
        if evidence:
            update += ", evidence = :evidence"
            values[":evidence"] = str(evidence)
        bumped = 0
        for kind, artifact_id in sorted(refs):
            if not kind or not artifact_id:
                continue
            table.update_item(
                Key={
                    "mapping_id": f"{_LEDGER_PREFIX}{kind}#{artifact_id}",
                    "discovered_at": bucket,
                },
                UpdateExpression=update,
                ExpressionAttributeNames={"#c": "count"},
                ExpressionAttributeValues=values,
            )
            bumped += 1
        if bumped:
            logger.info(f"Artifact usage recorded for {bumped} artifact(s)")
        return bumped
    except Exception as e:
        logger.warning(f"bump_artifact_usage failed (non-critical): {e}")
        return 0


def _scan_ledger_rows(table) -> list[dict]:
    """Bounded scan of the usage rows: begins_with prefix, page cap 20."""
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": "begins_with(mapping_id, :prefix)",
        "ExpressionAttributeValues": {":prefix": _LEDGER_PREFIX},
    }
    for _ in range(_SCAN_PAGE_CAP):
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


def _window_cutoff_bucket(days: int) -> str:
    """First ``YYYY-MM-DD`` day bucket inside the trailing ``days`` window."""
    return (
        datetime.now(timezone.utc) - timedelta(days=max(int(days), 1) - 1)
    ).strftime("%Y-%m-%d")


def _fold_ledger_item(
    out: dict[str, dict[str, Any]], item: dict, cutoff: str
) -> None:
    """Fold one ledger row into the per-artifact aggregate (in place).

    Skips rows whose ``mapping_id`` is not ``usage#<kind>#<id>`` or whose
    day bucket predates ``cutoff``. Same-id-two-kinds merges: sum counts,
    latest ``last_used_at``, first-seen kind kept. ``evidence_classes`` is
    the sorted union of the folded buckets' ``evidence`` attrs; a bucket
    without one folds as ``"tool-text"`` — the honest legacy default, since
    that extraction was the only pre-B3 writer (ADR-0052 decision (b)).
    """
    mapping_id = item.get("mapping_id", "")
    bucket = str(item.get("discovered_at", ""))
    parts = str(mapping_id).split("#", 2)
    if len(parts) != 3 or parts[0] + "#" != _LEDGER_PREFIX:
        return
    _, kind, artifact_id = parts
    if not kind or not artifact_id or bucket < cutoff:
        return
    try:
        count = int(item.get("count", 0))
    except (TypeError, ValueError):
        count = 0
    last_used = item.get("last_used_at") or None
    entry = out.setdefault(artifact_id, {
        "artifact_id": artifact_id, "kind": kind,
        "usage_30d": 0, "last_used_at": None, "evidence_classes": [],
    })
    entry["usage_30d"] += count
    evidence_class = str(item.get("evidence") or "") or _LEGACY_EVIDENCE_CLASS
    if evidence_class not in entry["evidence_classes"]:
        entry["evidence_classes"].append(evidence_class)
        entry["evidence_classes"].sort()
    if last_used and (entry["last_used_at"] is None
                      or str(last_used) > str(entry["last_used_at"])):
        entry["last_used_at"] = last_used


def read_artifact_usage(days: int = 30) -> dict[str, dict[str, Any]]:
    """Aggregate the ledger over the trailing ``days`` window (inclusive).

    Returns ``{artifact_id: {"artifact_id", "kind", "usage_30d",
    "last_used_at", "evidence_classes"}}`` summing day buckets whose date
    falls within the last ``days`` days (today and the ``days - 1`` preceding
    days); older buckets are skipped. ``evidence_classes`` is the sorted
    union over the folded buckets (buckets without the attr fold as
    ``"tool-text"`` — ADR-0052 decision (b)). Results are cached in-process
    for 60 s (per ``days`` value).
    In the degenerate case of the same id under two kinds, counts merge
    (sum; latest ``last_used_at``; first-seen kind kept). Best-effort /
    SWALLOWS: ``{}`` on any backend failure.
    """
    now_monotonic = time.monotonic()
    with _read_cache_lock:
        cached = _read_cache.get(days)
        if cached and cached[0] > now_monotonic:
            return cached[1]
    try:
        from . import self_learning
        table = self_learning._get_table()
        items = _scan_ledger_rows(table)
        cutoff = _window_cutoff_bucket(days)
        out: dict[str, dict[str, Any]] = {}
        for item in items:
            _fold_ledger_item(out, item, cutoff)
        with _read_cache_lock:
            _read_cache[days] = (now_monotonic + _READ_CACHE_TTL_SECONDS, out)
        return out
    except Exception as e:
        logger.warning(f"read_artifact_usage failed (non-critical): {e}")
        return {}


def _reset_read_cache() -> None:
    """Test seam: clear the in-process read cache."""
    with _read_cache_lock:
        _read_cache.clear()
