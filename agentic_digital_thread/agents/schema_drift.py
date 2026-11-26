# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E5 schema drift detection (ADR-0053) — drift is a PROPOSAL class, never a
graph mutation.

Scheduled (daily) and on-demand re-discovery per onboarded system, diffed
against the graph's onboarded schema (``schema_check.load_system_schema`` —
the single source, which carries ``data_type`` per Field). Each drift finding
(column added/removed/retyped, table added/removed) files ONE pending
``schema_drift`` proposal through ``self_learning.log_proposed_mapping`` — the
ingestion gates and canonical-key dedup apply (the ``proposal_key.py``
``schema_drift`` grain: the SAME unresolved drift dedupes as ``dup_pending``;
a type-flap files a distinct row). The steward resolves drift in the same
consequence-ordered queue. Accepting a removal/retype row is acknowledge-
only (``self_learning._accept_schema_drift`` — zero graph writes; re-
onboarding stays the steward's explicit fix for destructive drift);
accepting ``column_added``/``table_added`` MATERIALISES the column/table
through ``schema_fields`` (ADR-0077 D1). Detection itself never writes the
graph. Additive drift whose subject's latest accept was the SAME addition
is re-filed with the generational ``REDETECTED_DESCRIPTION`` marker
(ADR-0077 D2) so no accepted row — pre-ADR acknowledge-only or a later
marked accept — can ever blind the queue.

Hard rules (plan D-E5):

* NEVER calls map/resolve/provision; NEVER deletes or writes graph artifacts.
  ``onboard.onboard_discover`` is read-only w.r.t. the graph (verified: graph
  writes live in provision only).
* Skips are HONEST, per system: no baseline in the graph ⇒
  ``skipped:no-baseline``; a discover failure ⇒ ``skipped:<error class>`` —
  never a fabricated empty diff.
* The diff is PURE and deterministic (separated from the impure fetch — the
  audit-gate house pattern); an ``unknown`` type NEVER emits retyped (honest
  indeterminate), and dotted/struct columns compare with the same
  prefix-tolerant matching ``schema_check.column_in_schema`` uses (the
  lake-athena struct-path lesson) — reused, not re-implemented.

Scheduler: ``register_drift_check_job()`` hangs a daily job off the shared
``prompts.add_interval_job`` seam — ``DRIFT_CHECK_INTERVAL_SECONDS`` (default
86400), ``0`` disables. The scheduled run iterates onboarded systems SERIALLY.
The on-demand path is the ``POST /api/admin/learning/drift-check`` async job
in ``agents.learning_routes`` (the CloudFront 120 s cap forbids a blocking POST).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DRIFT_JOB_ID = "schema-drift-check"
_DEFAULT_INTERVAL_SECONDS = 86400  # daily

# The five pinned drift kinds (the proposal row's ``reason`` attribute).
DRIFT_KINDS = ("column_added", "column_removed", "column_retyped",
               "table_added", "table_removed")

# ADR-0077 D1 — the ADDITIVE kinds a steward accept materialises in the graph.
MATERIALISABLE_KINDS = ("column_added", "table_added")

# ADR-0077 D2 — the pinned description of an additive drift re-filed after a
# steward accepted the SAME addition: a DIFFERENT target key on the SAME
# subject. Generation 1 is the bare constant; each later accepted generation
# advances the suffix so an accepted marked row can never blind the next
# re-detection (its exact target would classify ``noop``).
REDETECTED_DESCRIPTION = ("re-detected after a prior accept of this addition; "
                          "accept again to materialise")
_GENERATION_RE = re.compile(re.escape(REDETECTED_DESCRIPTION) + r" \(generation (\d{1,6})\)")


# ── pure diff ────────────────────────────────────────────────────────────────

def _norm_type(value) -> str:
    """Case-folded, whitespace-collapsed type token; empty ⇒ ``unknown``."""
    normalized = " ".join(str(value or "").split()).casefold()
    return normalized or "unknown"


def _fresh_table_map(fresh_tables: list) -> dict:
    """``{name: {"columns": [...], "types": {col: raw_type}}}`` from an
    ``onboard_discover`` result's ``tables`` list. Pure; tolerates both
    dict-shaped (``{"name", "data_type"}``) and bare-string column entries."""
    out: dict[str, dict] = {}
    for t in fresh_tables or []:
        name = str((t or {}).get("name") or "").strip()
        if not name:
            continue
        columns: list[str] = []
        types: dict[str, str] = {}
        for c in (t or {}).get("columns") or []:
            if isinstance(c, dict):
                cname = str(c.get("name") or "").strip()
                if cname:
                    columns.append(cname)
                    if c.get("data_type"):
                        types[cname] = str(c["data_type"])
            elif isinstance(c, str) and c.strip():
                columns.append(c.strip())
        out[name] = {"columns": columns, "types": types}
    return out


def _match_fresh_table(fresh_map: dict, table: str) -> dict | None:
    """Case-insensitive fresh-table lookup (mirror of
    ``schema_check._resolve_table`` for the fresh side)."""
    meta = fresh_map.get(table)
    if meta is not None:
        return meta
    tl = table.lower()
    for name, m in fresh_map.items():
        if name.lower() == tl:
            return m
    return None


def _type_of(types: dict, column: str) -> str:
    """Case-insensitive normalized type lookup; absent ⇒ ``unknown``."""
    cl = column.lower()
    for key, value in (types or {}).items():
        if str(key).lower() == cl:
            return _norm_type(value)
    return "unknown"


def _drift(reason: str, table: str, column: str, summary: str) -> dict:
    """One drift descriptor. ``from_field`` is ``table.column``, or
    ``table.`` for table-level events (the pinned subject grain)."""
    return {"reason": reason, "table": table, "column": column,
            "from_field": f"{table}.{column}" if column else f"{table}.",
            "description": "", "summary": summary}


def _diff_table(table: str, baseline_meta: dict, fresh_meta: dict) -> list:
    """PURE per-table diff. Column presence uses ``schema_check``'s
    prefix-tolerant ``_column_present`` in BOTH directions, so flattened
    struct/dotted property paths never emit false added/removed drift.
    Retype compares only when both sides carry a KNOWN type for the exact
    column — ``unknown`` never retypes (honest indeterminate)."""
    from .schema_check import _column_present
    drifts: list[dict] = []
    baseline_cols = {"columns": baseline_meta.get("columns") or []}
    fresh_cols = {"columns": fresh_meta.get("columns") or []}
    for col in fresh_cols["columns"]:
        if not _column_present(baseline_cols, col):
            drifts.append(_drift("column_added", table, col,
                                 f"column '{col}' appeared in table '{table}'"))
    for col in baseline_cols["columns"]:
        col = str(col)
        if not _column_present(fresh_cols, col):
            drifts.append(_drift(
                "column_removed", table, col,
                f"column '{col}' is no longer visible in table '{table}'"))
            continue
        old = _type_of(baseline_meta.get("column_types") or {}, col)
        new = _type_of(fresh_meta.get("types") or {}, col)
        if "unknown" in (old, new) or old == new:
            continue
        retyped = _drift("column_retyped", table, col,
                         f"column '{col}' in table '{table}' changed type")
        retyped["description"] = f"was {old}, now {new}"
        drifts.append(retyped)
    return drifts


def diff_schemas(baseline: dict, fresh_tables: list) -> list:
    """PURE drift diff: graph baseline (``load_system_schema`` shape) vs the
    fresh connector tables (``onboard_discover`` result shape). Emits the
    pinned drift descriptors sorted deterministically. Table matching is
    case-insensitive on both sides (``schema_check._resolve_table`` for the
    baseline — reused, not re-implemented)."""
    from .schema_check import _resolve_table
    fresh_map = _fresh_table_map(fresh_tables)
    drifts: list[dict] = []
    for name in fresh_map:
        if _resolve_table(baseline, name) is None:
            drifts.append(_drift("table_added", name, "",
                                 f"table '{name}' appeared in the source"))
    for table, meta in baseline.items():
        if not isinstance(meta, dict):
            continue
        fresh_meta = _match_fresh_table(fresh_map, str(table))
        if fresh_meta is None:
            drifts.append(_drift(
                "table_removed", str(table), "",
                f"table '{table}' is no longer visible in the source"))
            continue
        drifts.extend(_diff_table(str(table), meta, fresh_meta))
    return sorted(drifts, key=lambda d: (d["from_field"], d["reason"]))


# ── impure fetch + proposal filing ───────────────────────────────────────────

def _discover_fresh(system_id: str) -> dict:
    """One READ-ONLY schema re-read via the existing discover machinery
    (module attr — tests stub it entirely). The catalog entry's own
    ``connection_type``/``schema`` are passed EXPLICITLY: ``DiscoverRequest``
    defaults ``connection_type`` to PostgreSQL and the route prefers the body,
    so omitting them would mis-route a non-Postgres system's discovery."""
    from .catalog import catalog
    from .models import DiscoverRequest
    from .onboard import onboard_discover
    entry = catalog.get(system_id) or {}
    request = DiscoverRequest(
        system_id=system_id,
        connection_type=str(entry.get("connection_type") or "PostgreSQL"),
        schema=str(entry.get("schema") or ""))
    return asyncio.run(onboard_discover(request))


def _drift_target_key(system_id: str, drift: dict) -> str:
    """One drift descriptor's canonical ``target_key`` (the ``proposal_key``
    ``schema_drift`` grain). Pure."""
    from .proposal_key import canonical_keys
    return canonical_keys(
        "schema_drift", from_system=system_id,
        from_field=str(drift.get("from_field") or ""),
        reason=str(drift.get("reason") or ""),
        description=str(drift.get("description") or ""),
    ).target_key


def redetected_description(generation: int) -> str:
    """The marker for re-file ``generation`` (1 = the bare constant). Pure."""
    if generation <= 1:
        return REDETECTED_DESCRIPTION
    return f"{REDETECTED_DESCRIPTION} (generation {int(generation)})"


def redetected_generation(description) -> int:
    """The re-file generation a stored description carries (0 = unmarked)."""
    text = str(description or "")
    if text == REDETECTED_DESCRIPTION:
        return 1
    match = _GENERATION_RE.fullmatch(text)
    return int(match.group(1)) if match else 0


def _subject_drift_rows(system_id: str, from_field: str) -> list:
    """Every stored ``schema_drift`` row on the subject (any status). Seam."""
    from . import self_learning
    from .proposal_key import canonical_keys
    subject_key = canonical_keys("schema_drift", from_system=system_id,
                                 from_field=from_field).subject_key
    rows = self_learning._query_subject(self_learning._get_table(), subject_key)
    return [r for r in rows if r.get("proposed_type") == "schema_drift"]


def _accept_generation(system_id: str, from_field: str, reason: str) -> int:
    """The marker generation for a re-detected ``reason`` drift; 0 = file it
    UNMARKED (a fresh steward review).

    Eligible ONLY when the subject's most recent steward accept
    (``accepted_at`` — it survives the later ``superseded`` flip) was the
    SAME additive reason: an accepted removal/retype is not acceptance of a
    later addition, so it never authorises repair. The generation is one past
    the highest among the accepted same-reason rows, so the new target key
    differs from every accepted one (no exact-accepted ``noop``)."""
    accepted = [r for r in _subject_drift_rows(system_id, from_field)
                if r.get("accepted_at")]
    if not accepted:
        return 0
    latest = max(accepted, key=lambda r: str(r.get("accepted_at")))
    if latest.get("reason") != reason:
        return 0
    return 1 + max(redetected_generation(r.get("to_description"))
                   for r in accepted if r.get("reason") == reason)


def _pending_description(system_id: str, drift: dict, pending_rows: list):
    """The description of an already-pending row for this exact drift
    (stability: a re-run reuses it — no store lookup, no churn), else None."""
    for row in pending_rows or []:
        if (row.get("proposed_type") == "schema_drift"
                and str(row.get("from_system") or "") == system_id
                and row.get("from_field") == drift.get("from_field")
                and row.get("reason") == drift.get("reason")):
            return str(row.get("to_description") or "")
    return None


def _mark_unmaterialised(system_id: str, drifts: list, pending_rows: list) -> list:
    """ADR-0077 D2: a NEW list of NEW descriptors where every materialisable
    drift whose subject's latest accept was the SAME addition carries the
    next-generation ``REDETECTED_DESCRIPTION`` marker.

    Why: an exact-target ACCEPTED row classifies ``noop`` in
    ``_classify_collision``, so an accept that did not (or no longer) hold
    would blind re-detection forever. The marked descriptor has a DIFFERENT
    target on the SAME subject, so the unchanged lifecycle supersedes the old
    accepted row (stamps survive — ADR-0003 D) and files a fresh pending one.
    Stability: an already-pending row for the drift keeps its description."""
    out: list[dict] = []
    for drift in drifts:
        if drift.get("reason") not in MATERIALISABLE_KINDS:
            out.append(dict(drift))
            continue
        pending_desc = _pending_description(system_id, drift, pending_rows)
        if pending_desc is not None:
            out.append({**drift, "description": pending_desc})
            continue
        generation = _accept_generation(system_id, str(drift.get("from_field") or ""),
                                        str(drift.get("reason") or ""))
        out.append({**drift, "description": redetected_description(generation)}
                   if generation else dict(drift))
    return out


def _pending_drift_ids(system_id: str, rows: list) -> set:
    """The pending ``schema_drift`` mapping ids for ``system_id`` in a
    ``get_pending_mappings`` snapshot."""
    return {str(r.get("mapping_id")) for r in rows
            if r.get("proposed_type") == "schema_drift"
            and str(r.get("from_system") or "") == system_id}


def _file_drift_proposals(system_id: str, drifts: list,
                          pending_rows: list | None = None) -> tuple:
    """File one pending ``schema_drift`` proposal per drift through
    ``log_proposed_mapping`` (gates + dedup apply), SERIALLY. Returns
    ``(filed, noops)``: ``filed`` is the count of rows ACTUALLY WRITTEN (the
    pending snapshot after minus the one before; the write returns
    the existing id on ``dup_pending`` / ``noop`` / ``cooldown_noop``, and an
    ACCEPTED row's id is not in the pending snapshot, so it used to count as
    filed); ``noops`` is the count of drifts whose write returned an id
    without writing. A gate drop (``None``) counts as found, not filed, not
    noop. ``pending_rows`` is the caller's prefetched pending snapshot
    (fetched here when ``None``); a re-filed (marked) drift rides the same
    path."""
    if not drifts:
        return 0, 0
    from . import self_learning
    if pending_rows is None:
        pending_rows = self_learning.get_pending_mappings()
    before = _pending_drift_ids(system_id, pending_rows)
    detected = datetime.now(timezone.utc).date().isoformat()
    returned: list = []
    for drift in drifts:
        returned.append(self_learning.log_proposed_mapping(
            system_id, drift["from_field"], "",
            system_id, "", "",
            "", 1.0,
            f"schema drift detected {detected}: {drift['reason']} — "
            f"{drift['summary']}",
            proposed_type="schema_drift", source="agent",
            method="schema-drift", reason=drift["reason"],
            description=drift.get("description", ""),
        ))
    written = _pending_drift_ids(system_id, self_learning.get_pending_mappings()) - before
    noops = sum(1 for mid in returned if mid and str(mid) not in written)
    return len(written), noops


def _current_drift_target_keys(system_id: str, current_drifts: list) -> set:
    """Recompute each current drift's canonical ``target_key`` (the
    ``proposal_key.py`` ``schema_drift`` grain: subject ``(from_system,
    from_field)`` + RHS digest of ``(reason, description)``). Stored rows
    CARRY this key — ``self_learning._build_item`` writes ``target_key`` on
    every proposal item — so the sweep compares recomputed vs stored. Pure."""
    return {_drift_target_key(system_id, d) for d in current_drifts or []}


def sweep_resolved_drift(system_id: str, current_drifts: list) -> int:
    """Machine-legal supersede of RESOLVED drift rows (ADR-0052 replace-not-
    accrete; ADR-0053: tombstones are steward claims — the machine's ONLY
    legal terminal here is ``superseded``, never a steward-only status).

    Flips every pending, agent-sourced, non-quarantined ``schema_drift`` row
    for ``system_id`` whose stored ``target_key`` is NOT in the current drift
    set to ``superseded`` (+ ``superseded_at``,
    ``superseded_reason="drift-resolved"``) via
    ``self_learning._update_status``. DynamoDB-only — never touches the graph.
    TARGET-grain (not subject-grain) matching is deliberate: an UNRESOLVED
    type-flap row (same ``table.column`` subject, descriptor still in the
    current diff) stays pending; subject-grain would sweep it as a sibling
    casualty. A row with no stored ``target_key`` is never swept on a guess.
    ``current_drifts=[]`` means "no drift outstanding" (the re-onboard case —
    the baseline was just rewritten from the live source). Returns the swept
    count (rides as ``resolved_swept``). Each flip is CONDITIONAL on a
    still-pending row (the ADR-0043 subsume idiom) — a mid-sweep steward
    decision WINS (skipped, uncounted). Fail-loud otherwise; the hook wraps it."""
    from . import self_learning
    from .value_promotion import _is_conditional_check_failed
    current_keys = _current_drift_target_keys(system_id, current_drifts)
    superseded_at = datetime.now(timezone.utc).isoformat()
    table = self_learning._get_table()
    swept = 0
    for row in self_learning.get_pending_mappings():
        if (row.get("proposed_type") != "schema_drift"
                or str(row.get("from_system") or "") != system_id
                or str(row.get("source") or "") != "agent"
                or str(row.get("quarantined") or "") == "true"
                or str(row.get("status") or "") != "pending"):
            continue
        stored_key = str(row.get("target_key") or "")
        if not stored_key or stored_key in current_keys:
            continue
        try:
            self_learning._update_status(
                table, row["mapping_id"], row["discovered_at"], "superseded",
                {"superseded_at": superseded_at,
                 "superseded_reason": "drift-resolved"},
                expect_status="pending")
        except Exception as exc:
            if not _is_conditional_check_failed(exc):
                raise  # fail-loud on genuine write failures
            continue  # a steward decision landed mid-sweep — it wins
        swept += 1
    if swept:
        logger.info("drift sweep: superseded %d resolved drift row(s) for "
                    "system %s", swept, system_id)
    return swept


def _skip(system_id: str, status: str) -> dict:
    """The pinned per-system skip entry — honest, never a fabricated diff."""
    logger.warning("schema drift check %s for system %s", status, system_id)
    return {"system_id": system_id, "status": status,
            "drifts_found": 0, "proposals_filed": 0, "proposal_noops": 0, "kinds": {},
            "resolved_swept": 0, "refiled_after_accept": 0}


def _kind_counts(drifts: list) -> dict:
    """``{reason: count}`` over the drift descriptors."""
    kinds: dict[str, int] = {}
    for drift in drifts:
        kinds[drift["reason"]] = kinds.get(drift["reason"], 0) + 1
    return kinds


def run_drift_check(system_id: str) -> dict:
    """One system's drift check → the pinned per-system result entry.

    Baseline unavailable in the graph ⇒ ``skipped:no-baseline``; a discover
    failure ⇒ ``skipped:<error class>`` (exception CLASS only — messages can
    carry hosts/values). Otherwise diff + mark + file (gates and dedup
    apply): additive drift whose subject's latest accept was the same
    addition is re-filed with the ``REDETECTED_DESCRIPTION`` marker
    (ADR-0077 D2; counted as
    ``refiled_after_accept``) and the sweep uses the SAME marked list.
    RAISES only on a proposal-write failure (fail-loud — the callers' honest
    error paths surface it)."""
    from . import self_learning
    from .schema_check import load_system_schema
    baseline = load_system_schema(system_id)
    if not baseline:
        return _skip(system_id, "skipped:no-baseline")
    try:
        fresh = _discover_fresh(system_id)
    except Exception as exc:  # noqa: BLE001 - honest skip, coarse class only
        return _skip(system_id, f"skipped:{type(exc).__name__}")
    if not isinstance(fresh, dict) or fresh.get("error"):
        return _skip(system_id, "skipped:DiscoveryError")
    drifts = diff_schemas(baseline, fresh.get("tables") or [])
    if drifts:
        _log_diff_counts(system_id, baseline, fresh.get("tables") or [], drifts)
    pending = self_learning.get_pending_mappings() if drifts else []
    drifts = _mark_unmaterialised(system_id, drifts, pending)
    filed, noops = _file_drift_proposals(system_id, drifts, pending)
    # ADR-0052 replace-not-accrete: re-detection replaces stale generated
    # rows — drift rows no longer in the current diff are resolved and get
    # machine-legally superseded (never a steward-only status).
    swept = sweep_resolved_drift(system_id, drifts)
    refiled = sum(1 for d in drifts if redetected_generation(d.get("description")))
    return {"system_id": system_id, "status": "ok",
            "drifts_found": len(drifts), "proposals_filed": filed,
            "proposal_noops": noops,
            "kinds": _kind_counts(drifts), "resolved_swept": swept,
            "refiled_after_accept": refiled}


def _log_diff_counts(system_id: str, baseline: dict, fresh_tables: list,
                     drifts: list) -> None:
    """One INFO line per drifted table with the baseline / fresh / added /
    removed column COUNTS (table names only, never column names or values),
    so a live over-count (93 reported against 25 real) can be read
    straight off the log instead of reproduced."""
    fresh_map = _fresh_table_map(fresh_tables)
    for table in sorted({d["table"] for d in drifts}):
        meta = baseline.get(table) if isinstance(baseline.get(table), dict) else None
        fresh_meta = _match_fresh_table(fresh_map, table) or {}
        mine = [d for d in drifts if d["table"] == table]
        logger.info(
            "drift diff system=%s table=%s baseline_cols=%d fresh_cols=%d "
            "added=%d removed=%d retyped=%d", system_id, table,
            len((meta or {}).get("columns") or []), len(fresh_meta.get("columns") or []),
            sum(1 for d in mine if d["reason"] == "column_added"),
            sum(1 for d in mine if d["reason"] == "column_removed"),
            sum(1 for d in mine if d["reason"] == "column_retyped"))


# ── scheduler (the register_auto_accept_job idiom) ───────────────────────────

def onboarded_system_ids() -> list:
    """Sorted onboarded-system ids (the in-memory catalog source)."""
    from .catalog import catalog
    return sorted(catalog.get_onboarded_ids())


def run_scheduled_drift_check() -> list:
    """The scheduled sweep: every onboarded system, SERIALLY. Never raises
    (a bad system is an honest per-system skip; the scheduler must survive
    every tick).

    The ADR-0059 failure-signal question sweep rides this SAME cadence tick
    (co-located per the plan — one shared ``DRIFT_CHECK_INTERVAL_SECONDS``
    job, no second scheduler registration): after the drift systems are
    checked, over-threshold failure signals file their steward questions.
    Best-effort — a broken accumulator never kills the tick, and the drift
    results are returned unchanged."""
    results: list[dict] = []
    for system_id in onboarded_system_ids():
        try:
            results.append(run_drift_check(system_id))
        except Exception as exc:  # noqa: BLE001 - a bad system never kills the sweep
            results.append(_skip(system_id, f"skipped:{type(exc).__name__}"))
    found = sum(r.get("drifts_found", 0) for r in results)
    logger.info("scheduled schema drift check: %d system(s), %d drift(s)",
                len(results), found)
    try:
        from .failure_signals import sweep_and_file_questions
        sweep = sweep_and_file_questions()
        if sweep.get("filed"):
            logger.info("failure-signal sweep filed %d steward question(s)",
                        len(sweep["filed"]))
    except Exception:  # noqa: BLE001 - best-effort: never kills the drift tick
        logger.debug("failure-signal question sweep failed (non-fatal)",
                     exc_info=True)
    return results


def register_drift_check_job() -> None:
    """Register the daily drift check on the SHARED scheduler
    (``prompts.add_interval_job`` — no second scheduler). Env-tunable:
    ``DRIFT_CHECK_INTERVAL_SECONDS`` (default 86400 = daily); ``0`` (or any
    non-positive value) disables — no job registered."""
    raw = os.environ.get("DRIFT_CHECK_INTERVAL_SECONDS",
                         str(_DEFAULT_INTERVAL_SECONDS))
    try:
        seconds = int(raw)
    except ValueError:
        logger.warning("invalid DRIFT_CHECK_INTERVAL_SECONDS=%r — using default",
                       raw)
        seconds = _DEFAULT_INTERVAL_SECONDS
    if seconds <= 0:
        logger.info("schema drift check disabled (DRIFT_CHECK_INTERVAL_SECONDS=%s)",
                    raw)
        return
    from .prompts import add_interval_job
    add_interval_job(_DRIFT_JOB_ID, run_scheduled_drift_check, seconds)
    logger.info("schema drift check job registered (every %ss)", seconds)
