# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Self-learning relationship discovery — DynamoDB storage + Neptune write-back.

Stores proposed cross-system mappings discovered by the orchestrator during
query execution. Mappings are reviewed by humans via the Review UI, then
accepted (written to Neptune) or rejected.

Implements ADR 0003's unified proposal lifecycle (S3.1):
  - lookup-before-write keyed on the type-aware canonical key (proposal_key.py);
  - source-aware collision/supersede (user wins over agent; agent cannot clobber
    an accepted user correction);
  - a ``superseded`` state + monotonic ``version``;
  - subject-key applied-node identity (one live Neptune node per subject);
  - fail-loud on the four critical-path functions (log_proposed_mapping,
    accept_mapping, record_user_correction, and their table/Neptune seams).

Bulk admin helpers (clear_*/reset_*/accept_all_pending) and read-only get_*
deliberately keep swallowing (bounded blast radius, ADR 0003 F).

Table: adt-proposed-mappings
  PK: mapping_id (UUID)
  SK: discovered_at (ISO 8601)
  GSI: status-index (status + discovered_at)
  GSI: subject-key-index (subject_key + discovered_at)
"""
import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3

from . import joinpath_rule_link as _jp_rule
from .proposal_key import canonical_keys, CanonicalKeys  # noqa: F401  (re-exported contract)
from .metric_turn_invalidation import invalidates_metric_turn_results
from .stored_value_mapping_memo import mutates_value_mappings
# The ADR-0050 vocabulary keys must share proposal_key's EXACT normalization
# and digest recipe. Aliased ONCE here under public names — this module's
# sanctioned seam over those primitives (proposal_key's dispatch is untouched).
from .proposal_key import _digest as key_digest, _norm as key_norm
# ADR-0051 P3/P9 seam: the exported unordered-pair canonicalization and the
# field-path normalizer (aliased like key_norm/key_digest — never copied).
from .proposal_key import unordered_value_pair
from .proposal_key import _norm_field_path as key_norm_field
# Single home for the LEARNED-note marker (the pure primitives module), so the
# write format here and every reader (guidance split, agent mirror, composed-
# context telemetry) cannot drift.
from .query_intelligence import LEARNED_NOTE_PREFIX as _qi_learned_prefix
from .public_errors import public_error

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("PROPOSED_MAPPINGS_TABLE", "adt-proposed-mappings")
_dynamodb = None

# --------------------------------------------------------------------------- #
# Subject-level churn controls (ADR-0042). Module constants, env-overridable
# per existing conventions (read once at import, like TABLE_NAME). All three
# branches are AGENT-ONLY — user-sourced proposals are untouched and still win
# unconditionally (ADR-0003 C).
# --------------------------------------------------------------------------- #
# D2 — flip-flop circuit breaker: once a subject's supersede chain reaches this
# many superseded rows, the incoming agent proposal is QUARANTINED (stored
# pending with quarantined="true" + a stated review_reason; nothing superseded)
# and the subject is frozen until a human accept/reject.
SUBJECT_FLIPFLOP_QUARANTINE_THRESHOLD = int(
    os.environ.get("SUBJECT_FLIPFLOP_QUARANTINE_THRESHOLD", "3"))
# D1 — supersede cooldown: an agent proposal that would supersede on a subject
# whose most recent supersede happened inside this window is a dup-style no-op.
SUBJECT_SUPERSEDE_COOLDOWN_HOURS = float(
    os.environ.get("SUBJECT_SUPERSEDE_COOLDOWN_HOURS", "24"))
# D3 — rejection damping: an exact re-proposal whose subject has at least
# REJECTION_DAMP_MIN_COUNT rejections with the most recent inside
# REJECTION_REPROPOSAL_COOLDOWN_DAYS is dropped (logged, no row).
REJECTION_REPROPOSAL_COOLDOWN_DAYS = float(
    os.environ.get("REJECTION_REPROPOSAL_COOLDOWN_DAYS", "7"))
REJECTION_DAMP_MIN_COUNT = int(
    os.environ.get("REJECTION_DAMP_MIN_COUNT", "2"))
# E6 — tombstone decay (ADR-0053): rejected rows ARE the tombstones (no new
# store). An exact agent re-proposal of a shape a steward rejected inside this
# window is FLAGGED, never dropped (flag-not-drop — the row inserts pending
# with the pinned review reason naming the rejection so nothing is silently
# lost); past the window the tombstone decays and the shape re-proposes clean.
# Repeat offenders still hard-drop via D3 above; user source and the /reopen
# route bypass.
TOMBSTONE_TTL_DAYS = float(os.environ.get("TOMBSTONE_TTL_DAYS", "90"))

# --------------------------------------------------------------------------- #
# Insight finding lifecycle (ADR-0051 a/b). Insights carry a PER-FINDING
# subject grain (proposal_key folds the narrative digest into the LHS), so
# distinct findings coexist and the ADR-0042 breaker never sees them churn.
# --------------------------------------------------------------------------- #
# (a) — pending insight rows are findings, not rules: stale ones decay.
# Expired LAZILY at read/propose time (status → "expired"), no scheduler.
INSIGHT_PENDING_TTL_DAYS = float(
    os.environ.get("INSIGHT_PENDING_TTL_DAYS", "30"))
# (b) — graduation ladder: this many DISTINCT corroborating narratives on one
# (from_system, to_system, concept) group emit ONE pending `correlation`
# proposal (count of independent confirmations, NOT confidence — ADR-0043 D2).
INSIGHT_GRADUATION_MIN_CORROBORATIONS = int(
    os.environ.get("INSIGHT_GRADUATION_MIN_CORROBORATIONS", "3"))

# --------------------------------------------------------------------------- #
# Propose-time co-reference staging (ADR-0051 d — P5) + cross-site guard
# (ADR-0051 e — P8). Module constants, env-overridable per convention.
# --------------------------------------------------------------------------- #
# (d) — an agent-sourced CROSS-SYSTEM value_mapping/join_path needs this many
# observed co-references before a reviewable pending row exists. The first
# observation writes a staging row (``staged#`` mapping_id, NO status attribute
# — invisible to every lifecycle read); the threshold-th DISTINCT observation
# within STAGING_TTL_DAYS promotes it to a real pending row whose
# query_context names all observations. 1 (or less) disables staging entirely
# (the offline-test escape hatch — first observation stores directly).
PROPOSAL_STAGING_MIN_COREFERENCES = int(
    os.environ.get("PROPOSAL_STAGING_MIN_COREFERENCES", "2"))
STAGING_TTL_DAYS = float(os.environ.get("STAGING_TTL_DAYS", "30"))
# Only the co-occurrence capture paths stage. Measured/promoted pipelines
# BYPASS by provenance: ``heuristic`` (join_discovery — value-overlap counts
# ARE the evidence), ``value_promotion`` (the ADR-0043 sweep — promoted
# artifacts already carry >= 3-value evidence), and
# ``deterministic-response-integrity`` (Mode-A response-integrity typed
# proposals — each is bound to VERIFIED current-turn evidence ids, so the
# ≥2-co-reference bar is met by construction; ADR-0051 (d)). User source and
# same-system proposals (single-system observations, e.g. code→label pairs
# read from one row — not co-references) bypass by construction in
# ``_staging_applies``.
_STAGING_TYPES = ("value_mapping", "join_path")
_STAGING_BYPASS_PROVENANCES = ("heuristic", "value_promotion",
                               "deterministic-response-integrity")
_STAGED_PREFIX = "staged#"
#: Non-lifecycle row families sharing this table — NEVER review items:
#: the artifact-usage ledger (ADR-0052 evidence rows, ``artifact_usage.py``)
#: and the measured cost record (ADR-0053, ``cost_ledger.py``). They carry
#: no ``status`` and leaked into the review list as "Unknown type — review
#: manually" noise (live-found 2026-08-06, 40 rows).
_NON_LIFECYCLE_PREFIXES = ("staged#", "usage#", "cost#", "failure_signal#")

# Accepted broad rules suppress finer-grained observations they already cover.
# The status-index snapshot is process-local and short-lived so a burst of value
# observations pays for at most one accepted-row query, not one query per write.
ACCEPTED_COVERAGE_CACHE_TTL_SECONDS = float(
    os.environ.get("ACCEPTED_COVERAGE_CACHE_TTL_SECONDS", "60"))
_accepted_coverage_cache: "tuple[float, list[dict]] | None" = None
_accepted_coverage_cache_generation = 0
_accepted_coverage_cache_lock = threading.Lock()


def _reset_accepted_coverage_cache() -> None:
    """Invalidate snapshots and advance the generation for in-flight readers."""
    global _accepted_coverage_cache, _accepted_coverage_cache_generation
    with _accepted_coverage_cache_lock:
        _accepted_coverage_cache_generation += 1
        _accepted_coverage_cache = None


def _publish_accepted_coverage_cache(
    generation: int, rows: list[dict], *, expires_at: float,
) -> bool:
    """Publish ``rows`` only if no mutation invalidated the reader's generation."""
    global _accepted_coverage_cache
    with _accepted_coverage_cache_lock:
        if generation != _accepted_coverage_cache_generation:
            return False
        _accepted_coverage_cache = (expires_at, rows)
        return True


def _get_table():
    """Get DynamoDB table resource (lazy init)."""
    global _dynamodb
    if _dynamodb is None:
        region = os.environ.get("AWS_REGION", "us-east-1")
        _dynamodb = boto3.resource("dynamodb", region_name=region)
    return _dynamodb.Table(TABLE_NAME)


# --------------------------------------------------------------------------- #
# Fail-loud table seam (ADR 0003 F). These RAISE — they do NOT swallow. The
# read-only get_* and the bulk clear_*/reset_*/accept_all_pending helpers keep
# their own try/except (unchanged). We do NOT rewrite storage.py.
# --------------------------------------------------------------------------- #
def _put_item(table, item: dict) -> None:
    """Insert an item. Raises on backend failure (fail-loud)."""
    table.put_item(Item=item)


def _query_subject(table, subject_key: str) -> list[dict]:
    """Query the subject-key-index GSI for all rows on a subject. Fail-loud."""
    response = table.query(
        IndexName="subject-key-index",
        KeyConditionExpression="subject_key = :sk",
        ExpressionAttributeValues={":sk": subject_key},
        ScanIndexForward=False,  # newest first
    )
    return response.get("Items", [])


def _query_by_status(status: str, *, newest_first: bool = False) -> list[dict]:
    """Paginated status-index GSI query for ALL rows with ``status``. Fail-loud.

    Shared by ``get_pending_mappings``, ``record_rule_usage``, and
    ``get_accepted_rules_by_node_ids`` — callers own their swallow contracts
    (this helper RAISES, per the seam convention above).

    PAGINATES on ``LastEvaluatedKey``: a single DynamoDB page is capped at
    1 MB, so rows past page 1 must still be read (snapshot completeness for
    the ADR-0043 promotion threshold; an accepted rule past page 1 would
    otherwise never get its usage bump).
    """
    table = _get_table()
    items: list[dict] = []
    query_kwargs: dict = {
        "IndexName": "status-index",
        "KeyConditionExpression": "#s = :status",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":status": status},
    }
    if newest_first:
        query_kwargs["ScanIndexForward"] = False  # newest first
    for _page in range(20):  # bounded — defensive cap, never spins
        response = table.query(**query_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    return items


def _accepted_coverage_rows() -> list[dict]:
    """Return a TTL-cached deterministic snapshot of accepted rows.

    Carries the broad rule types (coverage suppression) PLUS accepted
    ``value_mapping`` rows for the ADR-0051 P3 reverse-conflict probe —
    ``_accepted_rule_covering_observation`` explicitly skips the VM rows, so
    coverage semantics are byte-unchanged."""
    now = time.monotonic()
    with _accepted_coverage_cache_lock:
        cached = _accepted_coverage_cache
        generation = _accepted_coverage_cache_generation
        if cached is not None and now < cached[0]:
            return list(cached[1])
    try:
        rows = sorted(
            (r for r in _query_by_status("accepted")
             if r.get("proposed_type") in
             ("join_path", "identity_rule", "crosswalk", "value_mapping")),
            key=lambda r: (r.get("discovered_at", ""), r.get("mapping_id", "")),
        )
    except Exception as exc:  # best-effort suppression must not block ingestion
        logger.debug("accepted coverage lookup failed; storing observation: %s", exc)
        rows = []
    _publish_accepted_coverage_cache(
        generation,
        rows,
        expires_at=now + ACCEPTED_COVERAGE_CACHE_TTL_SECONDS,
    )
    return list(rows)


def _same_field_pair(rule: dict, *, from_system: str, from_field: str,
                     to_system: str, to_field: str) -> bool:
    """Case-insensitive exact directional field-pair comparison. Pure."""
    norm = lambda value: str(value or "").strip().casefold()
    return (
        norm(rule.get("from_system")) == norm(from_system)
        and norm(rule.get("from_field")) == norm(from_field)
        and norm(rule.get("to_system")) == norm(to_system)
        and norm(rule.get("to_field")) == norm(to_field)
    )


def _rule_transform_covers(rule: dict, *, from_value: str, to_value: str,
                           transform: str) -> bool:
    """Whether an accepted rule's transform explains this value observation."""
    rule_transform = str(rule.get("transform") or "identity").strip().casefold()
    incoming_transform = str(transform or "").strip().casefold()
    if incoming_transform:
        return incoming_transform == rule_transform

    left = str(from_value or "").strip()
    right = str(to_value or "").strip()
    if not left or not right:
        return False

    # A crosswalk without an explicit transform is an exemplar, not a blanket
    # identity rule: it covers only the accepted pair itself.
    if rule.get("proposed_type") == "crosswalk" and not rule.get("transform"):
        return (
            left.casefold() == str(rule.get("from_value") or "").strip().casefold()
            and right.casefold() == str(rule.get("to_value") or "").strip().casefold()
        )
    if rule_transform == "identity":
        return left == right
    if rule_transform == "casefold":
        return left.casefold() == right.casefold()
    if rule_transform.startswith("strip_prefix:"):
        affix = rule_transform.split(":", 1)[1]
        strip = lambda value: value[len(affix):] if value.casefold().startswith(affix) else value
        return strip(left).casefold() == strip(right).casefold()
    if rule_transform.startswith("strip_suffix:"):
        affix = rule_transform.split(":", 1)[1]
        strip = lambda value: value[:-len(affix)] if affix and value.casefold().endswith(affix) else value
        return strip(left).casefold() == strip(right).casefold()
    if rule_transform == "token_extract":
        try:
            from .resolve_evidence import extract_join_tokens
            return bool(set(extract_join_tokens(left)) & set(extract_join_tokens(right)))
        except Exception:
            return False
    return False


def _accepted_rule_covering_observation(
    proposed_type: str, from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str, transform: str,
) -> dict | None:
    """Find the first accepted broad rule covering a VM/crosswalk observation."""
    if proposed_type not in ("value_mapping", "crosswalk"):
        return None
    for rule in _accepted_coverage_rows():
        if rule.get("proposed_type") == "value_mapping":
            continue  # per-value rows ride the snapshot for P3 only — never "cover"
        if not _same_field_pair(
            rule, from_system=from_system, from_field=from_field,
            to_system=to_system, to_field=to_field,
        ):
            continue
        if _rule_transform_covers(
            rule, from_value=from_value, to_value=to_value, transform=transform,
        ):
            return rule
    return None


def _record_coverage_evidence(rule: dict) -> None:
    """Best-effort atomic evidence bump on a covering accepted rule."""
    try:
        _get_table().update_item(
            Key={"mapping_id": rule["mapping_id"],
                 "discovered_at": rule["discovered_at"]},
            UpdateExpression=(
                "ADD coverage_evidence_count :one "
                "SET last_coverage_evidence_at = :now"
            ),
            ExpressionAttributeValues={
                ":one": 1,
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:  # evidence is useful, suppression remains valid
        logger.debug("could not record accepted-rule coverage evidence: %s", exc)


def _update_status(table, mapping_id: str, discovered_at: str, status: str,
                   extra: dict | None = None, *,
                   expect_status: str | None = None) -> None:
    """Update a row's status (+ optional extra attrs). Fail-loud.

    ``expect_status`` (keyword-only) makes the write CONDITIONAL on the row's
    CURRENT status (``ConditionExpression: #s = :expected``) — used by the
    ADR-0043 subsume so an accept/reject landing between snapshot and subsume
    wins (DynamoDB raises ``ConditionalCheckFailedException``). The default
    ``None`` keeps the historical unconditional write byte-identical for
    existing callers."""
    extra = extra or {}
    expr = "SET #s = :status"
    values = {":status": status}
    for i, (k, v) in enumerate(extra.items()):
        expr += f", {k} = :e{i}"
        values[f":e{i}"] = v
    kwargs = {
        "Key": {"mapping_id": mapping_id, "discovered_at": discovered_at},
        "UpdateExpression": expr,
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": values,
    }
    if expect_status is not None:
        values[":expected"] = expect_status
        kwargs["ConditionExpression"] = "#s = :expected"
    table.update_item(**kwargs)


def _neptune_write(cypher: str, parameters: dict | None = None) -> bool:
    """Module-global Neptune write seam (monkeypatch target in tests).

    Delegates to onboard's re-export of db.neptune_write (FLAG d: returns bool).
    """
    from .onboard import neptune_write as _nw
    return _nw(cypher, parameters)


def _neptune_write_returning(cypher: str, parameters: dict | None = None) -> "list | None":
    """Module-global write seam that RETURNS rows (monkeypatch target in
    tests): the rows the statement returned, or ``None`` when the graph
    refused it. Delegates to onboard's ``neptune_write_returning``."""
    from .onboard import neptune_write_returning as _nwr
    return _nwr(cypher, parameters)


def _neptune_write_loud(cypher: str, parameters: dict | None = None):
    """Fail-loud Neptune write: raise on a False return OR a raised exception."""
    result = _neptune_write(cypher, parameters)
    if result is False:
        raise RuntimeError(f"Neptune write failed (returned False): {cypher[:80]}")
    return result


def _neptune_query(cypher: str, parameters: dict | None = None) -> list:
    """Module-global Neptune read seam (monkeypatch target in tests). Fail-loud.

    Mirrors ``_neptune_write``/``_neptune_write_loud``: delegates to onboard's
    re-export of db.neptune_query and lets backend errors raise (no swallowing).
    Used by the concept_mapping repoint to read the current max MAPS_TO version.
    """
    from .onboard import neptune_query as _nq
    return _nq(cypher, parameters)


def _graph_rows_matched(cypher: str, parameters: dict) -> "int | None":
    """Count-probe for the fail-loud apply path (live incident, 2026-07-14): a
    ``MATCH … SET/MERGE`` whose MATCH finds zero rows is a SILENT no-op — the
    ledger flips to accepted while the graph is unchanged. ``cypher`` must be a
    read that RETURNs a single aggregate aliased ``n``.

    Returns the matched count, or ``None`` when the read yielded no row — the
    read seam (db.neptune_query) swallows backend errors into ``[]``, which is
    indistinguishable from "backend unreachable". Callers PROCEED on ``None``
    (never block on uncertainty): a genuinely unreachable graph still fails
    loud at the follow-on ``_neptune_write_loud``, and the offline unit lane
    (where the seam yields ``[]``) keeps its historical behavior.
    """
    rows = _neptune_query(cypher, parameters)
    if not rows:
        return None
    try:
        return int(rows[0].get("n") or 0)
    except Exception:  # noqa: BLE001 - malformed row → indeterminate, proceed
        return None


#: The accept pre-check's read of one System node: whether it exists and
#: whether its removal has started (the marker the removal writes first).
_SYSTEM_PRESENCE_READ = (
    "MATCH (s:System {id: $sid}) RETURN count(s) AS n, "
    "count(CASE WHEN s.removal_incomplete = true THEN 1 END) AS removing")


def _presence_count(row: dict, key: str) -> "int | None":
    try:
        return int(row.get(key) or 0)
    except Exception:  # noqa: BLE001 - malformed row: indeterminate, proceed
        return None


def _require_graph_systems(*system_ids: str) -> None:
    """Fail-loud pre-check: every referenced System node must EXIST in the graph
    before a join-like apply MERGEs its rule node (live incident: a proposal
    whose from_system was a plant name matched zero ``(s:System {id: …})`` rows,
    so the FROM_SYSTEM/TO_SYSTEM edges silently never materialized), and must
    not be a system whose removal has started (its node stays while the
    removal runs and after a failed one; the removal would delete the rule,
    or the rule would point at a system that is never served). Raises an
    actionable ``ValueError`` (the accept route surfaces it as a 400 and the
    ledger stays pending); an indeterminate probe (no row: backend
    unreachable, or the offline lane) proceeds, per ``_graph_rows_matched``'s contract.
    """
    for sid in dict.fromkeys(s for s in system_ids if s):
        rows = _neptune_query(_SYSTEM_PRESENCE_READ, {"sid": sid})
        row = rows[0] if rows and isinstance(rows[0], dict) else None
        if row is None:
            continue
        if _presence_count(row, "n") == 0:
            raise ValueError(
                f"system '{sid}' does not exist in the knowledge graph — the "
                f"rule was NOT applied; edit the proposal to name an onboarded "
                f"system id before accepting"
            )
        if (_presence_count(row, "removing") or 0) > 0:
            raise ValueError(
                f"system '{sid}' is being removed (or its removal did not finish), so "
                f"the rule was NOT applied; accept it after that system is onboarded again"
            )


def _write_between_served_systems(label: str, alias: str, set_clause: str,
                                  params: dict) -> None:
    """Write one ``label`` node (id ``$sid``) and its FROM/TO_SYSTEM edges to
    ``$fsys`` / ``$tsys`` in ONE statement that matches both System nodes only
    when neither one's removal has started (``catalog.SERVED_ENDPOINTS_WHERE``,
    the same guard as the ValueMapping upsert). The marker check and the write
    are one transaction, so a removal that starts after the accept's
    pre-check writes nothing; the returned id is the readback. Raises
    ``ValueError`` when no row came back (an endpoint is missing or being
    removed: the ledger stays pending) and ``RuntimeError`` when the graph
    refused the write or the readback is unexpected."""
    from .catalog import SERVED_ENDPOINTS_WHERE
    rows = _neptune_write_returning(
        "MATCH (fs:System {id: $fsys}), (ts:System {id: $tsys}) " + SERVED_ENDPOINTS_WHERE
        + f"MERGE ({alias}:{label} {{id: $sid}}) SET {set_clause} "
        f"MERGE ({alias})-[:FROM_SYSTEM]->(fs) MERGE ({alias})-[:TO_SYSTEM]->(ts) "
        f"RETURN {alias}.id AS id", params)
    if rows == []:
        raise ValueError(
            f"system '{params.get('fsys')}' or '{params.get('tsys')}' does not exist in the "
            f"knowledge graph or is being removed, so the rule was NOT applied; accept it "
            f"once both systems are onboarded")
    if not (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
            and rows[0].get("id") == params.get("sid")):
        raise RuntimeError(f"Neptune write failed: {label} {params.get('sid')} was not written")


# --------------------------------------------------------------------------- #
# Known-systems source that is LIVE in ANY process.
#
# ``catalog.catalog`` is an in-memory singleton hydrated only in the ECS app
# process (rebuilt from Neptune at app startup) — in the AgentCore runtime
# process it is EMPTY, which made gate 3d below (and the runtime insight
# extractor's system detection) inert there: an empty catalog gives no basis
# to judge, so everything stored raw. ``known_systems()`` closes that gap:
# the hydrated catalog when non-empty (ECS path — zero graph reads), else a
# read-only Neptune ``System`` read through the ``_known_systems_graph_read``
# seam, TTL-cached in-process (positive AND negative — a failed/empty graph
# read caches ``[]`` briefly so the gates fall back to store-raw exactly as
# today, without hammering the graph). Best-effort by construction: every
# failure path yields ``[]`` (never raises), preserving the existing
# fail-safe gate semantics (empty → no drop).
# --------------------------------------------------------------------------- #
KNOWN_SYSTEMS_TTL_SECONDS = 300.0        # positive result cache
KNOWN_SYSTEMS_NEGATIVE_TTL_SECONDS = 60.0  # failure / empty read — retry sooner

# (expires_at_monotonic, entries) — module-global; a benign race just costs an
# extra read. Reset per-test by the unit conftest.
_known_systems_cache: "tuple[float, list[dict]] | None" = None


def _reset_known_systems_cache() -> None:
    """Drop the cached graph-read result. Test-only seam (reset by the unit
    conftest between tests); production code never calls it."""
    global _known_systems_cache
    _known_systems_cache = None


def _known_systems_graph_read() -> list[dict]:
    """Read System nodes (id/name/system_type/plant) from the graph. Fail-loud seam.

    Dedicated seam (rather than calling ``_neptune_query`` at the call site) so
    tests — and the offline unit conftest — can patch the known-systems graph
    path without disturbing the other ``_neptune_query`` consumers. Read-only,
    static query, no user input (nothing to parameterize).
    """
    rows = _neptune_query(
        "MATCH (s:System) RETURN s.id AS id, s.name AS name, "
        "s.system_type AS system_type, s.plant AS plant"
    )
    return [
        {"id": r.get("id"), "name": r.get("name"),
         "system_type": r.get("system_type"), "plant": r.get("plant")}
        for r in (rows or []) if r.get("id")
    ]


def known_systems() -> list[dict]:
    """Known catalog systems, live in ANY process. Never raises.

    Resolution: the in-memory ``catalog`` singleton when non-empty (the ECS app
    process — zero graph reads), ELSE a TTL-cached Neptune read of ``System``
    nodes (the AgentCore runtime process, where the singleton is never
    hydrated). A graph failure is swallowed and ``[]`` is negative-cached for
    ``KNOWN_SYSTEMS_NEGATIVE_TTL_SECONDS`` — callers (gate 3d, the runtime
    insight extractor) treat empty as "no basis to judge" and store raw, the
    exact pre-F6 fail-safe.
    """
    global _known_systems_cache
    try:
        from .catalog import catalog as _catalog
        entries = _catalog.get_all()
        if entries:
            return entries
    except Exception as e:  # noqa: BLE001 - fail-safe source, never raise
        logger.warning(f"catalog read failed in known_systems: {e}")

    now = time.monotonic()
    # Read-once snapshot: the module-global tuple is replaced atomically, so a
    # single local read gives a consistent (expires_at, entries) view even if a
    # concurrent call swaps the global mid-function.
    cached = _known_systems_cache
    if cached is not None and now < cached[0]:
        return list(cached[1])
    try:
        systems = _known_systems_graph_read()
    except Exception as e:  # noqa: BLE001 - fail-safe source, never raise
        logger.warning(f"known-systems graph read failed (store-raw fallback): {e}")
        systems = []
    ttl = (KNOWN_SYSTEMS_TTL_SECONDS if systems
           else KNOWN_SYSTEMS_NEGATIVE_TTL_SECONDS)
    _known_systems_cache = (now + ttl, systems)
    return list(systems)


def _subject_id(subject_key: str) -> str:
    """Stable applied-node identity for a subject (ADR 0003 D)."""
    return "subj-" + hashlib.sha256(subject_key.encode("utf-8")).hexdigest()[:16]


def _subject_key_from_item(item: dict) -> str:
    """Recompute a subject_key from a stored item's fields (legacy fallback)."""
    annotation = item.get("query_context", "").replace("Auto-analysis: ", "")
    keys = _canonical_keys_for(
        item.get("proposed_type", "value_mapping"),
        from_system=item.get("from_system", ""),
        from_field=item.get("from_field", ""),
        from_value=item.get("from_value", ""),
        to_system=item.get("to_system", ""),
        to_field=item.get("to_field", ""),
        to_value=item.get("to_value", ""),
        concept=item.get("concept", ""),
        annotation=annotation,
        description=item.get("to_description", ""),
    )
    return keys.subject_key


# --------------------------------------------------------------------------- #
# Vocabulary lifecycle types (ADR-0050/0065). ``concept_vocab``,
# ``guidance_note``, and ``metric_definition`` are NARRATIVE-ISH proposed_types
# (like ``insight``): every join-shape / degeneracy / schema ingestion gate in
# ``log_proposed_mapping`` allowlists the join-like types, so they pass through
# by construction, and
# ``schema_check.SCHEMA_CHECKED_TYPES`` (an allowlist) excludes them too
# (asserted in tests/unit/test_vocab_lifecycle.py — no edit needed there).
# Item-field contract (frozen in the Wave-2 shared interface contract):
#   concept_vocab  — ``concept`` = concept id, ``to_value`` = display name,
#                    ``to_description`` = description; the op (add|edit|retire)
#                    and optional synonyms ride in the ``query_context`` JSON.
#   guidance_note  — ``from_system`` = system id, ``to_value`` = note text.
#   metric_definition — ``to_value`` = metric id; ``query_context`` = the C1
#                       canonical JSON; all system/field/concept slots empty.
# Canonical keys are computed HERE (proposal_key.py's dispatch is untouched —
# targeted extension of the ONE ADR-0003 lifecycle, no parallel store):
#   concept_vocab  — subject = the concept id (one live proposal per concept,
#                    so a later edit supersedes a pending add); target adds a
#                    digest of (name | description | op-context) for idempotent
#                    re-assertion — a description-only edit is a real change.
#   guidance_note  — subject = (system id, note digest): each DISTINCT note is
#                    its own subject (notes ACCUMULATE per system — a new note
#                    must never supersede a different pending one), while an
#                    identical re-proposal dedupes as dup_pending/noop.
#   concept_overlay — subject = (pack id, concept id): ONE live overlay per
#                    (pack, concept) — a content edit supersedes the prior
#                    row; target digests source + the JSON text fields so an
#                    identical re-proposal dedupes while any content change is
#                    a real change (T1.3; pack id rides in ``from_value``).
#   metric_definition — subject = metric id (ONE live proposal per id); target
#                    digests the canonical document, so an edit supersedes a
#                    pending add while an exact re-proposal dedupes (ADR-0066 C2).
# --------------------------------------------------------------------------- #
VOCAB_PROPOSED_TYPES = (
    "concept_vocab", "guidance_note", "concept_overlay", "metric_definition",
)


def _vocab_canonical_keys(
    proposed_type: str, *, from_system: str = "", concept: str = "",
    to_value: str = "", annotation: str = "", description: str = "",
    from_value: str = "", guidance_scope: str = "",
) -> CanonicalKeys:
    """Two-grained canonical key for the vocabulary types (ADR-0050).

    Mirrors ``proposal_key.canonical_keys``'s construction exactly (same
    separators, normalization, and digest — via the ``key_norm``/``key_digest``
    seam, not copied). The ``concept_vocab`` target digests name AND
    description AND the op-context (which carries synonyms), so a
    description-only edit is a REAL change, never a dup_pending no-op."""
    if proposed_type == "concept_vocab":
        subject_key = "concept_vocab|" + key_norm(concept)
        rhs = key_digest(f"{key_norm(to_value)}|{key_norm(description)}|"
                         f"{key_norm(annotation)}")
    elif proposed_type == "metric_definition":
        subject_key = "metric_definition|" + key_norm(to_value)
        rhs = key_digest(annotation)
    elif proposed_type == "concept_overlay":
        # T1.3 — one live overlay per (pack, concept); annotation carries the
        # JSON text fields, to_value the source stamp, from_value the pack id.
        subject_key = ("concept_overlay|" + key_norm(from_value) + "|"
                       + key_norm(concept))
        rhs = key_digest(f"{key_norm(to_value)}|{key_norm(annotation)}")
    else:  # guidance_note
        from .guidance_scope import normalize_scope
        scope = normalize_scope(guidance_scope).value
        subject_key = ("guidance_note|" + key_norm(from_system) + "|"
                       + key_norm(scope) + "|" + key_digest(key_norm(to_value)))
        rhs = key_digest(f"{key_norm(scope)}|{key_norm(to_value)}")
    return CanonicalKeys(subject_key=subject_key,
                         target_key=subject_key + ">>" + rhs)


def _canonical_keys_for(
    proposed_type: str, *, from_system: str = "", from_field: str = "",
    from_value: str = "", to_system: str = "", to_field: str = "",
    to_value: str = "", concept: str = "", annotation: str = "",
    description: str = "", reason: str = "", guidance_scope: str = "",
) -> CanonicalKeys:
    """``proposal_key.canonical_keys`` extended with the vocabulary types."""
    if proposed_type in VOCAB_PROPOSED_TYPES:
        return _vocab_canonical_keys(
            proposed_type, from_system=from_system, concept=concept,
            to_value=to_value, annotation=annotation, description=description,
            from_value=from_value, guidance_scope=guidance_scope,
        )
    return canonical_keys(
        proposed_type,
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value,
        concept=concept, annotation=annotation,
        reason=reason, description=description,
    )


def _supersede(table, prior_rows: list[dict]) -> None:
    """Mark prior on-subject rows superseded + stamp superseded_at. Fail-loud."""
    superseded_at = datetime.now(timezone.utc).isoformat()
    for r in prior_rows:
        _update_status(table, r["mapping_id"], r["discovered_at"], "superseded",
                       {"superseded_at": superseded_at})


def _parse_iso(ts: str | None):
    """Parse a stored ISO-8601 timestamp. Returns an aware datetime or None.

    Timestamps are written by this module as ``datetime.now(timezone.utc)
    .isoformat()``; a naive value (legacy/hand-edited) is assumed UTC. Never
    raises — unparseable input yields ``None`` (callers treat that as "no
    usable timestamp", the conservative no-damp outcome).
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _within_window(ts: str | None, *, hours: float = 0.0, days: float = 0.0) -> bool:
    """True when ``ts`` falls inside the trailing window from now. Pure-ish."""
    dt = _parse_iso(ts)
    if dt is None:
        return False
    return (datetime.now(timezone.utc) - dt) < timedelta(hours=hours, days=days)


def _is_stale_insight_pending(row: dict) -> bool:
    """True for a PENDING insight row older than ``INSIGHT_PENDING_TTL_DAYS``.

    ADR-0051 (a): insight pendings are findings, not rules — stale ones decay.
    Key-agnostic on purpose: legacy rows keyed under the pre-ADR-0051 insight
    grain decay on the same clock (the build's backward-compat policy). A row
    without a parseable ``discovered_at`` never expires (conservative)."""
    if row.get("proposed_type") != "insight" or row.get("status") != "pending":
        return False
    discovered_at = row.get("discovered_at")
    return (_parse_iso(discovered_at) is not None
            and not _within_window(discovered_at, days=INSIGHT_PENDING_TTL_DAYS))


def _expire_stale_insight_pendings(rows: list[dict]) -> list[dict]:
    """Lazy TTL sweep over ``rows`` (ADR-0051 a); returns the still-live rows.

    Stale pending insight rows move to the terminal ``expired`` status (with
    an ``expired_at`` stamp) — removed from review pressure, kept queryable
    (no deletion, audit trail preserved). Runs at read time
    (``get_pending_mappings``) and propose time (``_lpm_classify``'s prior
    snapshot), never on a scheduler. Best-effort per row: a failed expiry
    write keeps the row visible and is retried on the next read."""
    live: list[dict] = []
    expired_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if not _is_stale_insight_pending(row):
            live.append(row)
            continue
        try:
            _update_status(_get_table(), row["mapping_id"],
                           row["discovered_at"], "expired",
                           {"expired_at": expired_at})
        except Exception as e:  # noqa: BLE001 - lazy expiry must never break reads
            logger.warning(f"insight TTL expiry failed (non-critical): {e}")
            live.append(row)
    return live


def _is_churn_supersede(row: dict) -> bool:
    """A supersede that counts as target churn for the D2 breaker. Offboarding
    retires a removed system's pending rows (``superseded_reason:
    system_offboarded``, agents/offboard_retirement.py), and onboarding retires
    a proposal it filed against a system whose removal started meanwhile
    (``system_being_removed``): lifecycle, not a flip-flop, so a
    remove/re-onboard cycle must not pre-load the breaker."""
    from .offboard_retirement import OFFBOARDED_REASON, SYSTEM_BEING_REMOVED_REASON
    return (row.get("status") == "superseded"
            and row.get("superseded_reason") not in (OFFBOARDED_REASON,
                                                     SYSTEM_BEING_REMOVED_REASON))


def _flipflop_review_reason(prior: list[dict]) -> str:
    """The D2 quarantine's stated review reason (ADR-0042): names the flip-flop
    count and the distinct targets the subject churned between, from the prior
    rows' ``to_system``/``to_field``."""
    superseded_count = sum(1 for r in prior if _is_churn_supersede(r))
    targets = sorted({
        f"{r.get('to_system', '')}.{r.get('to_field', '')}".strip(".")
        for r in prior
        if r.get("status") in ("pending", "accepted", "superseded")
        and (r.get("to_system") or r.get("to_field"))
    })
    return (f"flip-flopped {superseded_count} times between targets "
            f"{', '.join(targets)} — adjudicate")


def _supersedes_since_adjudication(prior: list[dict]) -> int:
    """The D2 circuit breaker's threshold input (ADR-0042 re-arm semantics).

    Counts superseded rows on the subject SINCE the most recent
    human-adjudicated row — a row with status ``accepted``/``rejected``, dated
    by its ``accepted_at``/``rejected_at`` stamp. The raw superseded count is
    monotonic, so without this cut-off a once-quarantined subject would re-trip
    the breaker forever on settled history; adjudication lifts the freeze AND
    re-arms the breaker. Rows superseded before the adjudication don't count;
    superseded rows without a usable timestamp are treated as pre-adjudication
    when an adjudication exists (legacy rows predate the stamps). With no
    adjudicated row on the subject every superseded row counts — the original
    trip condition. ISO-8601 UTC strings compare lexicographically.
    """
    adjudicated_at = max(
        (r.get("accepted_at") or r.get("rejected_at") or ""
         for r in prior if r.get("status") in ("accepted", "rejected")),
        default="",
    )
    count = 0
    for r in prior:
        if not _is_churn_supersede(r):
            continue
        if adjudicated_at:
            ts = r.get("superseded_at") or ""
            if not ts or ts <= adjudicated_at:
                continue
        count += 1
    return count


def _latest_rejected_at(rejected: list[dict]) -> str:
    """Most recent ``rejected_at`` across rejected rows ('' when none carry one).

    Only ``rejected_at`` (stamped by ``reject_mapping`` per ADR-0042 D3) is
    consulted — ``discovered_at`` is when a row was PROPOSED, not rejected, so
    legacy rows without the stamp simply don't establish recency (conservative:
    no damping without a real rejection date).
    """
    return max((r.get("rejected_at") or "" for r in rejected), default="")


def _rejection_review_reason(rejected: list[dict]) -> str:
    """The D3 re-proposal review reason (ADR-0042): names the last rejection date."""
    latest = _latest_rejected_at(rejected)
    date = latest[:10] if latest else "unknown date"
    return (f"re-proposed; last rejected on {date} — state what changed "
            f"before accepting")


def _tombstone_match(exact_rejected: list[dict], now=None) -> bool:
    """ADR-0053 E6 tombstone predicate — a steward rejection that still binds.

    True when the exact-target rejected rows carry a REAL rejection date
    (``rejected_at`` — stamped ONLY by ``reject_mapping``, i.e. a human
    steward claim; the machine never mints one) whose latest falls inside
    ``TOMBSTONE_TTL_DAYS``. Undated legacy rejections never tombstone
    (recency unknowable — the same conservative posture as D3 damping); past
    the TTL the rejection decays and the caller re-proposes clean. Pure given
    ``now`` (defaults to the wall clock)."""
    latest = _parse_iso(_latest_rejected_at(exact_rejected))
    if latest is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - latest) < timedelta(days=TOMBSTONE_TTL_DAYS)


def _tombstone_review_reason(rejected: list[dict]) -> str:
    """The pinned ADR-0053 E6 tombstone flag — names the steward's rejection.

    EXACT wording contract (audit probes and the SlCards conflict banner key
    on it): ``re-proposal of a shape the steward rejected on <YYYY-MM-DD>
    (mapping <mapping_id>)``. The date lets a human judge recency; the id
    links straight to the prior adjudication."""
    latest = max(rejected, key=lambda r: r.get("rejected_at") or "")
    date = str(latest.get("rejected_at") or "")[:10]
    return (f"re-proposal of a shape the steward rejected on {date} "
            f"(mapping {latest.get('mapping_id', '')})")


def _classify_rejected_reproposal(rejected: list[dict]) -> tuple:
    """Classify an exact agent re-proposal of a REJECTED shape (ADR-0042 D3 +
    ADR-0053 E6 — the rejected rows themselves are the tombstones).

    Pinned precedence: D3 damping first (>= ``REJECTION_DAMP_MIN_COUNT``
    rejections with the latest inside the 7d cooldown still DROPS — repeat
    offenders never reach review), then the E6 tombstone (a steward rejection
    inside ``TOMBSTONE_TTL_DAYS`` flags-not-drops: the row inserts PENDING
    with the pinned tombstone review reason), then decay (a dated rejection
    past the TTL re-proposes CLEAN — schemas evolve; last year's rejection
    must not suppress today's correct proposal). Undated legacy rejections
    keep the pre-E6 consumable-history insert (recency unknowable)."""
    if len(rejected) >= REJECTION_DAMP_MIN_COUNT and _within_window(
            _latest_rejected_at(rejected),
            days=REJECTION_REPROPOSAL_COOLDOWN_DAYS):
        return ("rejection_damped", None, rejected)
    if _tombstone_match(rejected):
        return ("tombstoned", None, rejected)
    if _latest_rejected_at(rejected):
        return ("insert", None, [])  # tombstone decayed — clean re-proposal
    return ("insert_with_rejection_history", None, rejected)


def _is_reverse_disagreement(
    acc: dict, proposed_type: str, from_system: str, from_field: str,
    from_value: str, to_system: str, to_field: str, to_value: str,
) -> bool:
    """True when accepted row ``acc`` states the OPPOSITE direction of the
    incoming proposal over the same endpoint anchor but lands a DIFFERENT
    counterpart (ADR-0051 P3). Pure.

    Direction reversed = the incoming FROM endpoint is ``acc``'s TO endpoint
    and the systems swap. ``join_path``: a different reverse field is the
    disagreement. ``value_mapping``: same reversed field pair + shared pivot
    value + a different counterpart value (a different pivot value is a
    different record entirely — not a conflict)."""
    if (key_norm(acc.get("to_system")) != key_norm(from_system)
            or key_norm_field(acc.get("to_field")) != key_norm_field(from_field)
            or key_norm(acc.get("from_system")) != key_norm(to_system)):
        return False
    if proposed_type == "join_path":
        return key_norm_field(acc.get("from_field")) != key_norm_field(to_field)
    if key_norm_field(acc.get("from_field")) != key_norm_field(to_field):
        return False
    if key_norm(acc.get("to_value")) != key_norm(from_value):
        return False
    return key_norm(acc.get("from_value")) != key_norm(to_value)


def _reverse_conflict_review_reason(
    proposed_type: str, from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str, source: str,
) -> str:
    """ADR-0051 P3: flag a pending ``B→A′`` contradicting an accepted ``A→B``.

    Agent-only (ADR-0003 C — user proposals untouched); ``value_mapping`` /
    ``join_path`` only. The lookup canonicalizes endpoints through the
    exported ``proposal_key.unordered_value_pair`` — an accepted row whose
    unordered pair EQUALS the incoming's is a consistent restatement and is
    never flagged; a reverse-direction disagreement returns the ADR-0042 D4
    review reason naming the conflicting accepted row. Best-effort: any
    lookup failure yields "" (the propose path is never blocked). Flagged,
    not blocked: the row still stores pending — never silently coexisting."""
    if source == "user" or proposed_type not in ("value_mapping", "join_path"):
        return ""
    incoming = unordered_value_pair(from_system, from_field, from_value,
                                    to_system, to_field, to_value)
    try:
        accepted = [r for r in _accepted_coverage_rows()
                    if r.get("proposed_type") == proposed_type]
    except Exception:  # noqa: BLE001 - probe only, never block the propose path
        return ""
    for r in accepted:
        pair = unordered_value_pair(
            r.get("from_system", ""), r.get("from_field", ""),
            r.get("from_value", ""), r.get("to_system", ""),
            r.get("to_field", ""), r.get("to_value", ""))
        if pair == incoming:
            continue  # consistent restatement of the same mapping — fine
        if _is_reverse_disagreement(r, proposed_type, from_system, from_field,
                                    from_value, to_system, to_field, to_value):
            return (f"conflicts-with-accepted-reverse-mapping "
                    f"{r.get('mapping_id', '')}")
    return ""


# --------------------------------------------------------------------------- #
# Collision actions — the _classify_collision contract. ``rows`` carries the
# rows to supersede or the prior rejections, depending on ``action``.
#   ("dup_pending", id, [])                  exact target, prior pending → no insert
#   ("noop", id, [])                         exact target, prior accepted → no insert
#   ("insert_with_rejection_history", None, rejected_rows)
#                                            undated LEGACY rejections only
#                                            (ADR-0053 E6 — no decay clock)
#   ("rejection_damped", None, rejected_rows) ADR-0042 D3: >= REJECTION_DAMP_MIN_COUNT
#                                            rejections, latest inside the cooldown
#                                            → caller drops (returns None)
#   ("tombstoned", None, rejected_rows)      ADR-0053 E6: a steward rejection
#                                            inside TOMBSTONE_TTL_DAYS → caller
#                                            inserts pending FLAGGED with the
#                                            pinned tombstone review reason
#   ("cooldown_noop", id, [])                ADR-0042 D1: would-be supersede inside
#                                            the 24h window → dup-style no-op
#   ("quarantine", None, active_diff)        ADR-0042 D2: flip-flop threshold reached
#                                            (or subject already frozen) → caller
#                                            inserts pending quarantined="true",
#                                            supersedes NOTHING
#   ("supersede", None, rows_to_supersede)   same subject diff target (or user wins)
#   ("record_disagreement", None, [])        agent over an accepted user correction
#   ("insert", None, [])                     no active prior on subject
# --------------------------------------------------------------------------- #
def _classify_active_diff(prior: list[dict], active_diff: list[dict]) -> tuple:
    """Classify an agent proposal whose subject carries ACTIVE rows with a
    DIFFERENT target (ADR 0003 D + ADR-0042 D1/D2).

    Precedence: an accepted USER correction is never clobbered — the
    disagreement is recorded pending with review_reason=
    'conflicts-with-user-assertion' (ADR-0050 pinning); then the D2 circuit
    breaker (supersede chain at the threshold → quarantine instead of
    superseding again; RE-ARM: the threshold input is the count of supersedes
    SINCE the most recent human adjudication on the subject, so an
    adjudicated subject resumes the normal supersede path and only re-trips
    on a FRESH flip-flop streak); then the D1 cooldown (two contradictory
    "discoveries" inside one day are rediscovery noise, not new evidence →
    dup-style no-op returning the current active mapping id); else
    supersede."""
    user_accepted = [
        r for r in active_diff
        if r.get("status") == "accepted" and r.get("source") == "user"
    ]
    if user_accepted:
        return ("record_disagreement", None, [])
    if (_supersedes_since_adjudication(prior)
            >= SUBJECT_FLIPFLOP_QUARANTINE_THRESHOLD):
        return ("quarantine", None, active_diff)
    last_supersede = max(
        (r.get("superseded_at") or r.get("discovered_at") or ""
         for r in prior if r.get("status") == "superseded"),
        default="",
    )
    if last_supersede and _within_window(
            last_supersede, hours=SUBJECT_SUPERSEDE_COOLDOWN_HOURS):
        return ("cooldown_noop", active_diff[0]["mapping_id"], [])
    return ("supersede", None, active_diff)


def _classify_collision(keys: CanonicalKeys, prior: list[dict], source: str):
    """Decide ``(action, existing_id, rows)`` for a proposal given prior rows
    on the same subject — ADR 0003 C + D, refined by ADR-0042 (subject-level
    churn controls — AGENT-ONLY branches; the user path is untouched and
    still wins) and ADR-0053 E6 (tombstones). The full action table lives in
    the comment block above; branch rationale lives in the helpers
    (``_classify_active_diff``, ``_classify_rejected_reproposal``)."""
    exact = [r for r in prior if r.get("target_key") == keys.target_key]
    active = [r for r in prior if r.get("status") in ("pending", "accepted")]

    # User wins (D): supersede everything active on the subject, then apply.
    # Bypasses quarantine/cooldown/damping/tombstones entirely (ADR-0042: a
    # user correction cuts through the freeze).
    if source == "user":
        return ("supersede", None, active)

    # --- incoming agent (C) --------------------------------------------------
    pend = [r for r in exact if r.get("status") == "pending"]
    if pend:
        return ("dup_pending", pend[0]["mapping_id"], [])
    acc = [r for r in exact if r.get("status") == "accepted"]
    if acc:
        return ("noop", acc[0]["mapping_id"], [])

    active_diff = [r for r in active if r.get("target_key") != keys.target_key]

    # ADR-0042 D2 freeze: a quarantined pending row on the subject freezes
    # every further agent proposal (any target) until a human accept/reject.
    # Deliberate precedence corner (no behavior change): the freeze beats the
    # accepted-USER-row guard in _classify_active_diff — the human
    # adjudication the freeze demands subsumes the disagreement record, and a
    # user-SOURCE proposal still bypasses both (user wins, handled above).
    if any(r.get("quarantined") == "true" and r.get("status") == "pending"
           for r in prior):
        return ("quarantine", None, active_diff)

    if active_diff:
        return _classify_active_diff(prior, active_diff)

    rejected = [r for r in exact if r.get("status") == "rejected"]
    if rejected:
        # D3 damping → E6 tombstone → decay: the pinned precedence and its
        # rationale live in _classify_rejected_reproposal (module helper).
        return _classify_rejected_reproposal(rejected)
    return ("insert", None, [])


def _next_version(prior: list[dict], newly_superseded: list[dict]) -> int:
    """version = 1 + (prior superseded on subject + those superseded now). ADR 0003 E."""
    already = sum(1 for r in prior if r.get("status") == "superseded")
    return 1 + already + len(newly_superseded)


def _analysis_join_like(*, proposed_type: str, from_system: str, from_col: str,
                        to_system: str, to_col: str, fv: str, tv: str,
                        concept: str, cross_system: bool,
                        tool_calls_count: int) -> str:
    """Rationale text for the join-like types (extracted from
    ``_proposal_analysis`` — same strings, decomposition only)."""
    if proposed_type == "join_path":
        scope = (
            f"a cross-system join from {from_system}.{from_col} to {to_system}.{to_col}"
            if cross_system else
            f"a join from {from_system}.{from_col} to {to_system}.{to_col}"
        )
        effort = (f" It took {tool_calls_count} tool call(s) to resolve this by hand."
                  if tool_calls_count else "")
        return (
            f"Saving {scope} lets the orchestrator translate this identifier "
            f"between the systems in one hop, instead of re-deriving it with "
            f"exploratory queries.{effort}"
        )
    if proposed_type == "value_mapping":
        pair = (
            f"the {from_system} value '{fv}' corresponds to {to_system} '{tv}'"
            if tv else
            f"the {from_system} value '{fv}' maps to concept '{concept}'"
        )
        return (
            f"Saving that {pair} lets future answers resolve this identifier "
            f"directly, instead of re-querying to look it up."
        )
    if proposed_type == "crosswalk":
        return (
            f"Candidate crosswalk: {from_system} '{fv}' may correspond to {to_system} "
            f"'{tv}' for concept '{concept}'. These entities share NO overlapping "
            f"values (disjoint vocabularies) but sit under an already-joined parent, "
            f"so this is proposed by name/token similarity for you to confirm. "
            f"Confirming persists a ValueMapping so future answers translate directly."
        )
    return (  # identity_rule
        f"Saving an identity rule linking {from_system} and {to_system} on "
        f"concept '{concept}' lets the orchestrator match records across these "
        f"systems automatically in future queries."
    )


def _proposal_analysis(*, proposed_type: str, from_system: str, from_field: str,
                       from_value: str, to_system: str, to_field: str,
                       to_value: str, concept: str, tool_calls_count: int = 0) -> str:
    """Human-facing rationale for a proposal — intuitive, accurate, and generic.

    Explains WHAT relationship is being saved and the concrete benefit. A join
    is only described as "cross-system" when it genuinely spans two DISTINCT
    data systems (neither being the knowledge graph itself). GENERIC — no
    dataset literals. ``tool_calls_count`` is honest PER-TURN context only
    ("it took N tool calls to resolve this by hand"); it never implies a
    measured before/after delta — the stored item carries no tool_calls_*
    fields (value evidence = the ADR-0050 D6 usage ledger). Per-type text
    lives in ``_analysis_join_like`` / ``_analysis_narrative``.
    """
    from .proposal_scoring import is_knowledge_graph_system
    if proposed_type in ("join_path", "value_mapping", "crosswalk", "identity_rule"):
        cross_system = (
            bool(from_system) and bool(to_system)
            and from_system != to_system
            and not is_knowledge_graph_system(from_system)
            and not is_knowledge_graph_system(to_system)
        )
        return _analysis_join_like(
            proposed_type=proposed_type, from_system=from_system,
            from_col=(from_field or "").split(".")[-1], to_system=to_system,
            to_col=(to_field or "").split(".")[-1], fv=(from_value or "")[:40],
            tv=(to_value or "")[:40], concept=concept,
            cross_system=cross_system, tool_calls_count=tool_calls_count)
    return _analysis_narrative(
        proposed_type=proposed_type, from_system=from_system,
        from_field=from_field, to_system=to_system, to_field=to_field,
        concept=concept)


def _analysis_narrative(*, proposed_type: str, from_system: str,
                        from_field: str, to_system: str, to_field: str,
                        concept: str) -> str:
    """Rationale text for the narrative/vocabulary types (extracted from
    ``_proposal_analysis`` — same strings, decomposition only)."""
    if proposed_type == "field_annotation":
        return (
            f"Annotating {from_system}.{from_field} records what this column means, "
            f"so future queries select the right column without re-inspecting the schema."
        )

    if proposed_type in ("field_concept_mint", "concept_mapping"):
        return (
            f"Recording that {to_system}.{to_field} represents concept '{concept}' "
            f"lets the orchestrator bind this column deterministically in future queries."
        )

    if proposed_type == CONCEPT_MAPPING_RETIRE:
        return (
            f"Retiring the link from concept '{concept}' to {to_system}.{to_field} stops "
            f"system agents and metrics from using that column for the concept; the "
            f"concept's other links stay, and re-onboarding does not bring this one back."
        )

    if proposed_type == "insight":
        subject = f" about {from_system}" if from_system else ""
        topic = f" (topic: '{concept}')" if concept else ""
        return (
            f"Recorded finding{subject}{topic}: a narrative observation saved as "
            f"reviewable context. Informational only — it never becomes a graph rule."
        )

    if proposed_type == "correlation":
        return (
            f"Graduated correlation between {from_system} and {to_system} "
            f"(topic: '{concept}'): multiple independent findings corroborate "
            f"this pattern (the evidence context names them). Accepting writes "
            f"a Correlation node the resolver surfaces alongside answers."
        )

    return (
        f"Saving this {proposed_type} for concept '{concept}' improves future answers."
    )


def _attach_optional_attrs(item: dict, *, to_description: str, transform: str,
                           method: str, reason: str, provenance: str,
                           review_reason: str, quarantined: str,
                           time_key: dict | None) -> None:
    """Attach the optional proposal attributes to ``item`` in place.

    Every attribute here is ADDITIVE and stored ONLY when non-empty (DynamoDB
    rejects empty-string attrs), so legacy items and callers that do not pass
    them are unaffected. ``method``/``reason`` are the discovery's OWN stated
    evidence; ``provenance`` names the producing pipeline; ``review_reason``/
    ``quarantined`` are the ADR-0042 D4 shared review-card contract."""
    if to_description:
        item["to_description"] = to_description
    if transform:
        item["transform"] = transform
    if method:
        item["method"] = method
    if reason:
        item["reason"] = reason
    if provenance:
        item["provenance"] = provenance
    if review_reason:
        item["review_reason"] = review_reason
    if quarantined:
        item["quarantined"] = quarantined
    if time_key:
        # Store only the well-known keys, all as strings (DynamoDB-safe, no empties).
        tk = {k: str(time_key[k]) for k in ("from_time_field", "to_time_field", "semantics")
              if time_key.get(k)}
        if tk:
            item["time_key"] = tk


def _build_item(*, from_system, from_field, from_value, to_system, to_field, to_value,
                concept, confidence, query_context, proposed_type, keys, source, status,
                version, tool_calls_count=0, to_description: str = "",
                transform: str = "", time_key: dict | None = None,
                review_reason: str = "", quarantined: str = "",
                method: str = "", reason: str = "", provenance: str = "",
                guidance_scope: str = ""):
    """Construct a proposal DynamoDB item. Returns (mapping_id, discovered_at, item).

    ``transform`` (a ``join_path``'s real value transform) and ``time_key``
    (optional time-window alignment) are ADDITIVE — attached with the other
    optional attributes by ``_attach_optional_attrs`` (non-empty-only).

    VALUE EVIDENCE = the usage ledger (``artifact_usage``, ADR-0050 D6). The
    item deliberately carries NO ``tool_calls_before``/``tool_calls_after``
    fields (ADR audit P10): ``tool_calls_after`` was never measured (empty
    since inception) and a stored "before" implied a before/after delta nobody
    computes. ``tool_calls_count`` remains ONLY as honest per-turn context for
    the ``analysis`` narrative ("it took N tool calls THIS turn"). Legacy rows
    still carrying the old fields are read-tolerated, never re-written."""
    mapping_id = str(uuid.uuid4())
    discovered_at = datetime.now(timezone.utc).isoformat()
    analysis = _proposal_analysis(
        proposed_type=proposed_type, from_system=from_system,
        from_field=from_field, from_value=from_value, to_system=to_system,
        to_field=to_field, to_value=to_value, concept=concept,
        tool_calls_count=tool_calls_count,
    )
    if source != "user":
        # Machine-generated narrative: keep it free of session-memory phrasing
        # (same normalizer as the ``reason`` field; steward text never rewritten).
        analysis = _normalize_provenance_phrasing(analysis)
    item = {
        "mapping_id": mapping_id,
        "discovered_at": discovered_at,
        "from_system": from_system, "from_field": from_field, "from_value": from_value,
        "to_system": to_system, "to_field": to_field, "to_value": to_value,
        "concept": concept, "confidence": str(confidence), "query_context": query_context,
        "status": status, "proposed_type": proposed_type,
        "subject_key": keys.subject_key, "target_key": keys.target_key,
        "source": source, "version": str(version),
        "analysis": analysis,
    }
    if guidance_scope:
        item["guidance_scope"] = guidance_scope
    _attach_optional_attrs(
        item, to_description=to_description, transform=transform,
        method=method, reason=reason, provenance=provenance,
        review_reason=review_reason, quarantined=quarantined, time_key=time_key)
    return mapping_id, discovered_at, item


# --------------------------------------------------------------------------- #
# Ingestion-time auto-accept (flag-gated, best-effort). Uses the SHARED scoring
# function (proposal_scoring.score_proposal) as the single source of truth for
# "high confidence" so the route, auto-accept, and the bulk endpoint agree.
# --------------------------------------------------------------------------- #
_AUTO_ACCEPT_TRUTHY = ("1", "true", "yes", "on")
_AUTO_ACCEPT_TYPES = ("value_mapping", "identity_rule")

# DENY-TUPLE (future-safety): types that must NEVER be auto-accepted, checked
# EXPLICITLY in both _maybe_auto_accept and sweep_auto_accept BEFORE the
# allowlist. The deny beats the allowlist by construction — a future edit adding
# one of these types to _AUTO_ACCEPT_TYPES cannot silently admit it. ``insight``
# is a narrative finding (never a graph rule); the others are human-judgment
# types (annotations, crosswalk guesses, concept minting/repointing).
_NEVER_AUTO_ACCEPT_TYPES = (
    "insight", "field_annotation", "crosswalk", "field_concept_mint", "concept_mapping",
    # A retirement removes a concept -> column link: a steward decision only.
    "concept_mapping_retire",
    # ADR-0050 vocabulary lifecycle — human-judgment writes, never auto-accepted.
    # concept_overlay (T1.3) rides the same lifecycle: overlay text composes
    # into EVERY map prompt, so it lands via steward accept or a pre-accepted
    # pack install — never an ingestion-time auto-accept.
    "concept_vocab", "guidance_note", "concept_overlay",
    # ADR-0066 D3 — a formula is the highest-authority artifact: it defines
    # what a number MEANS, so nothing machine-sourced may apply one.
    "metric_definition",
    # ADR-0051 P2 — a graduated correlation is reviewed evidence, never applied
    # automatically (ADR-0043 D5 posture).
    "correlation",
    # ADR-0053 E5 — a schema-drift finding is acknowledged by a human steward;
    # auto-accepting one would silently swallow the drift signal. ADR-0077:
    # accepting an ADDITIVE drift now writes Field/SchemaEntry nodes, which
    # makes the steward-only door even more important.
    "schema_drift",
    # A coverage-gap finding rides the same acknowledge-only lifecycle as
    # schema_drift (a NAME match is a diagnostic, never an applied rule);
    # auto-accepting one would silently mark it seen with no steward review.
    "coverage_gap",
    # ADR-0059 D3 — a steward question is a QUEUE ENTRY asking a human for
    # knowledge; auto-accepting one would silently mark it seen with no human
    # ever reading it, defeating the mechanism's entire purpose. Its accept is
    # acknowledge-only (_accept_steward_question — zero graph writes, ever);
    # answering happens through the guided-turn machinery, which supersedes
    # the row.
    "steward_question",
)

# SOURCE deny-tuple (defense-in-depth beside the type deny above): proposals
# from an UNTRUSTED-INPUT path must NEVER be auto-accepted regardless of
# proposed_type. ``user-feedback`` rows are filed by the feedback-exploration
# worker off free-text user feedback (fenced, zero evidence weight —
# ADR-0054); they reach the graph ONLY through an explicit steward accept.
# ``answer-audit`` rows come from the ONE door through which a findings
# ``suggested_mapping`` enters durable storage — a steward-triggered
# ``POST /findings/{id}/file-proposal`` calling log_proposed_mapping(
# source="answer-audit") (ADR-0062 D6). The mapping VALUES are LLM-authored
# (untrusted text, D8): filing one expresses "worth reviewing", never "apply
# this", so it must land pending and stay pending until a steward accepts it.
# ``conversation`` carries agent-mediated, user-supplied formula data; filing
# it means "worth steward review", never authority (ADR-0066 D5).
# ``user`` rows are pending only when a non-steward asserted them
# (``record_user_proposal``) or filed a formula; a steward's own assertion
# enters ``accepted`` and never waits here. Either way a person who is not a
# steward asked for it, so only a steward accept may apply it.
# Checked EXPLICITLY in both _maybe_auto_accept and sweep_auto_accept BEFORE
# the type allowlist, so a future type change cannot silently admit them.
_NEVER_AUTO_ACCEPT_SOURCES = ("user-feedback", "answer-audit", "conversation",
                              "user")


# ADR-0063 D1 — ONE bounded probe behind target verification, generalized from
# the ADR-0051 P4 UUID-ONLY check to ANY target shape. Measured cause: a steward
# pass over 101 pending cross-system proposals found the dominant false class was
# an UNVERIFIED TARGET (a value that does not exist in the target system at all)
# — and every one of those rows named a NAME target, so the UUID-only guard never
# fired. Matches by ``id`` (the historical UUID predicate, FIRST and unchanged),
# by ``source_id`` (the telemetry projection's merge key — ``Asset``/``Property``)
# or by exact case-insensitive ``name``, and RETURNS the node's own system
# provenance (``Asset``/``Property`` carry ``system``; ``SchemaEntry``/``Field``
# carry ``system_id`` — docs/graph-shape-contract.md §2) so a cross-system claim
# can be scoped to the TO system's projection. Bounded by LIMIT.
_TARGET_PROBE_CYPHER = (
    "MATCH (n) WHERE coalesce(n.system, n.system_id, '') = $sid AND "
    "(n.id = $id OR n.source_id = $id "
    "OR toLower(coalesce(n.name, '')) = toLower($id)) "
    "RETURN n.id AS id, n.name AS name, "
    "coalesce(n.system, n.system_id) AS system LIMIT 25"
)

# The ONE unverified-target block wording (shape-named: a UUID target keeps the
# ADR-0051 P4 text byte-for-byte).
_UNVERIFIED_TARGET_BLOCK = "{shape} target not verified against the graph — left pending"


def _name_consistent(row: dict, expected: str) -> bool:
    """The ADR-0051 P4 case-insensitive containment test, semantics unchanged:
    a matched node's ``name`` and the proposal's ``from_value`` must not
    contradict each other when BOTH are present. Either absent ⇒ nothing to
    contradict (not a failure). ``expected`` is pre-casefolded."""
    name = str((row or {}).get("name") or "").strip().casefold()
    if not name or not expected:
        return True
    return expected in name or name in expected


def _target_probe_rows(target: str, to_system: str) -> "list | None":
    """Run the one bounded probe inside the requested TO-system projection."""
    if not to_system:
        return None
    try:
        return list(_neptune_query(
            _TARGET_PROBE_CYPHER, {"id": target, "sid": to_system}) or [])
    except Exception as e:  # noqa: BLE001 - unknown state, never a false
        logger.warning("target probe failed (grounding unknown): %s",
                       type(e).__name__)
        return None


def _empty_probe_verdict(to_system: str) -> "bool | None":
    """Disambiguate an EMPTY target probe: ``False`` (the target genuinely is
    absent from the TO system's projection) ONLY when the graph is
    demonstrably reachable AND that system is onboarded; ``None`` (unknown)
    otherwise.

    Two distinct reasons empty is not evidence of absence: the read seam
    swallows backend errors into ``[]`` (see ``_graph_rows_matched``), and a
    name missing from a NEVER-ONBOARDED system means "nobody could look", not
    "the claim is false" (ADR-0063 D1 honest absence)."""
    if not to_system:
        return None
    try:
        n = _graph_rows_matched(
            "MATCH (s:System {id: $sid}) RETURN count(s) AS n", {"sid": to_system})
    except Exception as e:  # noqa: BLE001 - indeterminate ⇒ unknown
        logger.warning(f"to-system presence probe failed (grounding unknown): {e}")
        return None
    return False if n else None


def _target_verification(item: dict) -> dict:
    """Deterministic target grounding for ANY proposal target (ADR-0063 D1).

    Returns ``{"verified": True|False|None, "scope": "system"|"unscoped"|None}``:

    - ``True`` — a name-consistent node matching the target EXISTS in the
      explicit TO-system projection; ``scope="system"`` is mandatory.
    - ``False`` — the scoped check RAN and did not pass: nothing matched in a
      reachable graph whose TO system is onboarded, a name contradiction, or
      the value exists ONLY in another system's projection (the from-system's
      own naming, which is exactly the refuted false class).
    - ``None`` — the check could NOT run, the TO system is not onboarded, or
      a matching node lacks TO-system provenance. That last case returns
      ``scope="unscoped"`` but remains UNKNOWN and grants no authority.

    NEVER rejects and NEVER deletes — callers gate AUTO-ACCEPT and inform
    review. Read-only: the ``verified`` stamp belongs to ``_verify_target``."""
    target = str(item.get("to_value") or "").strip()
    if not target:
        return {"verified": False, "scope": None}
    to_system = str(item.get("to_system") or "").strip()
    if not to_system:
        return {"verified": None, "scope": "unscoped"}
    rows = _target_probe_rows(target, to_system)
    if rows is None:
        return {"verified": None, "scope": None}
    if not rows:
        return {"verified": _empty_probe_verdict(to_system), "scope": None}
    expected = str(item.get("from_value") or "").strip().casefold()
    matches = [r for r in rows if _name_consistent(r, expected)]
    if not matches:
        return {"verified": False, "scope": None}
    provenance = [str((r or {}).get("system") or "").strip() for r in matches]
    if any(p.casefold() == to_system.casefold() for p in provenance):
        return {"verified": True, "scope": "system"}
    if any(provenance):
        return {"verified": False, "scope": None}
    return {"verified": None, "scope": "unscoped"}


def _stamp_target_verified(item: dict, scope: str) -> None:
    """Best-effort ``verified``/``verified_at`` (+ honest ``verified_scope``)
    stamp for a PASSING check. The check already passed, so a stamp failure is
    non-critical and never changes the verdict."""
    try:
        expr = "SET verified = :v, verified_at = :at"
        values = {":v": "true", ":at": datetime.now(timezone.utc).isoformat()}
        if scope:
            expr += ", verified_scope = :sc"
            values[":sc"] = scope
        _get_table().update_item(
            Key={"mapping_id": item["mapping_id"],
                 "discovered_at": item["discovered_at"]},
            UpdateExpression=expr, ExpressionAttributeValues=values)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"verified stamp failed (non-critical): {e}")


def _verify_target(item: dict) -> bool:
    """Target-side existence + name-consistency check for an auto-accept target
    (ADR-0051 P4, generalized from UUID-shaped to ANY target by ADR-0063 D1).
    Returns True ONLY on a passing check.

    FAIL-CLOSED: every non-passing verdict — no match, a name contradiction, a
    match only in another system's projection, an indeterminate read, an
    un-onboarded TO system — returns False and the proposal simply STAYS
    PENDING. Accept-on-doubt is forbidden; verification never rejects or
    deletes (an unverified proposal is pending, not wrong). On a PASSING check
    the row is stamped ``verified="true"``/``verified_at`` (plus the honest
    ``verified_scope``) so review surfaces can distinguish a CHECKED target —
    no text may claim confirmation without the stamp."""
    state = _target_verification(item)
    if state != {"verified": True, "scope": "system"}:
        return False
    item["verified"] = "true"
    item["verified_scope"] = "system"
    item["target_verified"] = "true"
    _stamp_target_verified(item, "system")
    return True


# Legacy name (ADR-0051 P4 shipped this as a UUID-only check). Kept as a THIN
# alias so an external import of the old name keeps working; production code and
# tests use ``_verify_target``.
_verify_uuid_target = _verify_target


def _recorded_target_verified(item: dict) -> "bool | None":
    """Authoritative recorded target state, requiring explicit TO-system scope.

    Legacy ``verified=true`` / ``target_verified=true`` rows without
    ``verified_scope=system`` are non-authoritative: absence of scope is
    unknown, never silently upgraded. A recorded false remains useful even
    without scope because it grants no authority."""
    raw = item.get("target_verified")
    recorded = str(raw if raw is not None else "").strip().lower()
    if recorded == "false" or raw is False:
        return False
    positive = (str(item.get("verified") or "").strip().lower() == "true"
                or recorded == "true" or raw is True)
    scope = str(item.get("verified_scope") or "").strip().lower()
    return True if positive and scope == "system" else None


def _auto_accept_block_reason(item: dict, *, verify: bool = True) -> str:
    """ADR-0051 P4 hardening: why an otherwise-eligible proposal must NOT
    auto-accept ('' = eligible). Checked BEFORE the allowlist in BOTH
    ``_maybe_auto_accept`` and ``sweep_auto_accept`` (deny-tuple posture), and
    REUSED — never re-implemented — by the review surface, so ADR-0063 D2 is
    DISPLAY of this policy and not a second trust model.

    - ``method == "hierarchy_position"`` NEVER auto-accepts: it is a
      positional guess ("the Nth element matched"), whatever its confidence.
    - A target requires a PASSING existence/name-consistency check
      (``_verify_target`` — ADR-0063 D1 generalized it from UUID-shaped to ANY
      target); unverified ⇒ stays pending. A row with no target at all (an
      annotation-shaped proposal) has nothing to verify.

    ``verify=False`` is the READ-FREE display mode: no graph read and no stamp
    — it judges the target from the grounding already RECORDED on the row and
    stays SILENT when that state is absent (unknown ≠ blocked). Display
    callers must pass it; the accept gates never do."""
    if str(item.get("method") or "").strip().lower() == "hierarchy_position":
        return "method=hierarchy_position is a positional guess — human review required"
    if not str(item.get("to_value") or "").strip():
        return ""
    from .proposal_scoring import is_uuid_like
    shape = "UUID" if is_uuid_like(item.get("to_value")) else "name"
    if verify:
        return "" if _verify_target(item) else _UNVERIFIED_TARGET_BLOCK.format(shape=shape)
    if _recorded_target_verified(item) is False:
        return _UNVERIFIED_TARGET_BLOCK.format(shape=shape)
    return ""


def review_trust_signals(item: dict) -> dict:
    """ADR-0063 D2 — the trust signals the engine ALREADY computes, shaped for
    the review surface. DISPLAY ONLY: no accept semantics change, and no new
    policy (the block reason comes from ``_auto_accept_block_reason``, THE
    single source of truth, in its read-free mode).

    Honest absence by construction: ``target_verified`` appears ONLY when the
    target was actually checked (absent ≠ false), and ``auto_accept_block``
    only when a block applies. Pure, no I/O, never raises."""
    signals: dict = {}
    try:
        verified = _recorded_target_verified(item)
        if verified is not None:
            signals["target_verified"] = verified
        block = _auto_accept_block_reason(item, verify=False)
        if block:
            signals["auto_accept_block"] = block
    except Exception as e:  # noqa: BLE001 - display enrichment, never fatal
        logger.debug("review trust signals unavailable: %s", e)
    return signals


def _attach_review_trust_signals(rows: list[dict]) -> list[dict]:
    """Attach the ADR-0063 D2 display signals to review rows, in place.

    COMPUTED ON READ (never persisted from here, so it can never go stale) and
    ADDITIVE. ``target_verified`` is normalized to a bool on EVERY row so the
    stored string shape never leaks to a client; ``auto_accept_block`` is
    attached to PENDING rows only — auto-accept eligibility is meaningless once
    a steward has decided."""
    for row in rows:
        signals = review_trust_signals(row)
        if row.get("status") != "pending":
            signals.pop("auto_accept_block", None)
        row.update(signals)
    return rows


def _attach_target_grounding(item: dict) -> None:
    """ADR-0063 D1 — record a CROSS-SYSTEM proposal's target-grounding state ON
    the row at FILE time, so review never sees a row whose grounding is simply
    unknown-because-nobody-looked.

    Cross-system only (``from_system != to_system``, both non-empty): a
    same-system pairing has no target projection to check. The key is ABSENT
    when the probe could not run — never a fabricated ``false``. Bounded (one
    probe, plus one system-presence read only when it came back empty) and
    best-effort: this NEVER prevents filing and NEVER raises (ADR-0003 F)."""
    from_system = str(item.get("from_system") or "").strip()
    to_system = str(item.get("to_system") or "").strip()
    if not from_system or not to_system or from_system == to_system:
        return
    try:
        state = _target_verification(item)
    except Exception as e:  # noqa: BLE001 - the probe already swallows; belt+braces
        logger.warning(f"file-time target grounding unavailable (absent): {e}")
        return
    verified = state.get("verified")
    if verified is None:
        return  # UNKNOWN — the state stays ABSENT rather than fabricating a false
    item["target_verified"] = "true" if verified else "false"
    if verified and state.get("scope"):
        item["verified_scope"] = str(state["scope"])


def _auto_accept_enabled() -> bool:
    """SELF_LEARNING_AUTO_ACCEPT truthiness (default OFF)."""
    return os.environ.get("SELF_LEARNING_AUTO_ACCEPT", "").strip().lower() in _AUTO_ACCEPT_TRUTHY


def _maybe_auto_accept(mapping_id: str, item: dict) -> bool:
    """Best-effort auto-accept of a freshly-logged proposal (S: auto-accept).

    Accepts ONLY when ALL hold: the flag is on; ``proposed_type`` is
    ``value_mapping`` or ``identity_rule`` (NEVER ``field_annotation`` or any other
    type); the SHARED ``score_proposal`` yields ``ai_confidence == 'high'``; and
    the ADR-0051 P4 hardening finds no block (``_auto_accept_block_reason`` —
    ``hierarchy_position`` never auto-accepts, and any target (UUID- or
    name-shaped, ADR-0063 D1) needs a passing existence/name check).

    Best-effort by contract: an auto-accept failure must NEVER fail the propose
    path — it is logged loudly and the proposal is simply left pending. This does
    not weaken ``log_proposed_mapping``'s fail-loud storage contract (the put_item
    already succeeded before we get here).
    """
    try:
        if not _auto_accept_enabled():
            return False
        if item.get("quarantined") == "true":
            return False  # ADR-0042 D2: frozen until a HUMAN accept/reject — never auto
        if item.get("source", "") in _NEVER_AUTO_ACCEPT_SOURCES:
            return False  # untrusted-input source: steward accept ONLY
        ptype = item.get("proposed_type", "")
        if ptype in _NEVER_AUTO_ACCEPT_TYPES:
            return False  # deny-tuple beats the allowlist (future-safety)
        if ptype not in _AUTO_ACCEPT_TYPES:
            return False
        block = _auto_accept_block_reason(item)
        if block:
            logger.info(f"auto-accept blocked for {mapping_id}: {block}")
            return False
        from .proposal_scoring import score_proposal
        if score_proposal(item).get("ai_confidence") != "high":
            return False
        logger.info(
            f"SELF_LEARNING_AUTO_ACCEPT: auto-accepting high-confidence {ptype} "
            f"proposal {mapping_id}"
        )
        result = accept_mapping(mapping_id)
        return isinstance(result, dict) and result.get("status") == "accepted"
    except Exception as e:  # noqa: BLE001 - best-effort, must not fail propose path
        logger.warning(
            f"auto-accept failed for {mapping_id} (leaving pending): {e}",
            exc_info=True,
        )
        return False


def sweep_auto_accept() -> dict:
    """Accept every PENDING high-confidence value_mapping/identity_rule in one pass.

    THE single source of truth for high-confidence auto-accept, reused three ways:
    (a) the ``POST /api/proposed-mappings/accept-high-confidence`` route, (b) the
    periodic scheduler safety-net, and (c) the post-``/api/ask`` background hook.
    Uses the SHARED scorer (``proposal_scoring.score_proposal``) for the
    high-confidence gate and only ever accepts ``value_mapping``/``identity_rule``
    (NEVER ``field_annotation`` or any other type — same gate as
    ``_maybe_auto_accept``).

    Best-effort per proposal: a per-item failure increments ``failed`` and never
    raises, so one bad proposal cannot abort the batch. ``get_pending_mappings`` is
    itself read-only/swallowing. Returns ``{"accepted": int, "failed": int}``.
    """
    from .proposal_scoring import score_proposal
    accepted = 0
    failed = 0
    for m in get_pending_mappings():
        if m.get("quarantined") == "true":
            continue  # ADR-0042 D2: frozen until a HUMAN accept/reject — never auto
        if m.get("source", "") in _NEVER_AUTO_ACCEPT_SOURCES:
            continue  # untrusted-input source: steward accept ONLY
        ptype = m.get("proposed_type", "")
        if ptype in _NEVER_AUTO_ACCEPT_TYPES:
            continue  # deny-tuple beats the allowlist (future-safety)
        if ptype not in _AUTO_ACCEPT_TYPES:
            continue
        if _auto_accept_block_reason(m):
            continue  # target authority must be established before scoring
        if score_proposal(m).get("ai_confidence") != "high":
            continue
        try:
            result = accept_mapping(m["mapping_id"])
        except Exception:  # noqa: BLE001 - accept_mapping is fail-loud; isolate the batch
            failed += 1
            continue
        if isinstance(result, dict) and "error" in result:
            failed += 1
        else:
            accepted += 1
    return {"accepted": accepted, "failed": failed}


# --------------------------------------------------------------------------- #
# Insight graduation ladder (ADR-0051 b). Sweep-shaped like the ADR-0043
# promote-values sweep: operator-triggered / on-demand, never scheduled,
# NEVER accepts (``correlation`` sits in the deny-tuple by construction).
# --------------------------------------------------------------------------- #
def _correlation_group_key(row: dict) -> str | None:
    """Grouping key for corroborating findings: normalized (from_system,
    to_system, concept) — both systems non-empty and DISTINCT (a correlation
    is cross-system; single-system narratives never graduate). Pure."""
    from_sys = key_norm(row.get("from_system"))
    to_sys = key_norm(row.get("to_system"))
    if not from_sys or not to_sys or from_sys == to_sys:
        return None
    return f"{from_sys}|{to_sys}|{key_norm(row.get('concept'))}"


def _graduate_insight_group(members: list[dict], digests: set[str]) -> str | None:
    """Emit ONE pending ``correlation`` proposal for a corroborated group.

    Goes through ``log_proposed_mapping`` (full ADR-0003/0042 lifecycle):
    the evidence summary names the corroborating rows, so a sweep re-run
    dedupes (same target digest ⇒ dup_pending) and FRESH corroboration
    supersedes the prior pending graduation. Confidence is nominal — the
    threshold is the COUNT of distinct narratives (ADR-0043 D2), never
    confidence."""
    exemplar = members[0]
    corroborating = sorted(str(m.get("mapping_id", "")) for m in members)
    summary = (
        f"Graduated from {len(digests)} corroborating findings on "
        f"{exemplar.get('from_system', '')} ↔ {exemplar.get('to_system', '')} "
        f"(topic: {exemplar.get('concept', '')}). Corroborating rows: "
        f"{', '.join(corroborating)}")
    return log_proposed_mapping(
        exemplar.get("from_system", ""), "", "",
        exemplar.get("to_system", ""), "", "",
        exemplar.get("concept", ""), 0.5, summary,
        proposed_type="correlation", source="agent",
        provenance="insight-graduation")


def sweep_insight_graduation() -> dict:
    """Graduate corroborated insight groups into ``correlation`` proposals.

    ADR-0051 (b): groups pending+accepted ``insight`` rows by
    ``_correlation_group_key``; a group holding at least
    ``INSIGHT_GRADUATION_MIN_CORROBORATIONS`` DISTINCT narrative digests
    (count of independent corroborations, NOT confidence) emits ONE pending
    ``correlation`` proposal. Never accepts anything (deny-tuple). Best-effort
    per group — one bad group never aborts the sweep. Returns
    ``{"graduated", "skipped_below_threshold", "failed"}``."""
    try:
        rows = [r for r in (get_pending_mappings()
                            + _query_by_status("accepted"))
                if r.get("proposed_type") == "insight"]
    except Exception as e:  # noqa: BLE001 - sweep is read-heavy, degrade to no-op
        logger.warning(f"insight graduation sweep read failed: {e}")
        return {"graduated": 0, "skipped_below_threshold": 0, "failed": 0}
    groups: dict[str, list[dict]] = {}
    for r in rows:
        group_key = _correlation_group_key(r)
        if group_key:
            groups.setdefault(group_key, []).append(r)
    graduated = skipped = failed = 0
    for _key, members in sorted(groups.items()):
        digests = {key_digest(str(m.get("query_context", ""))
                              .replace("Auto-analysis: ", ""))
                   for m in members}
        if len(digests) < INSIGHT_GRADUATION_MIN_CORROBORATIONS:
            skipped += 1
            continue
        try:
            _graduate_insight_group(members, digests)
            graduated += 1
        except Exception as e:  # noqa: BLE001 - per-group isolation
            logger.warning(f"insight graduation failed for a group: {e}")
            failed += 1
    return {"graduated": graduated, "skipped_below_threshold": skipped,
            "failed": failed}


# --------------------------------------------------------------------------- #
# Ingestion gates for ``log_proposed_mapping`` — one single-responsibility
# helper per gate, applied in the pinned order 3d → P8 → 3a → 3e → 3b → 3g →
# 3f → 3c. Every
# gate is BEST-EFFORT by contract: a raising predicate/normalizer must never
# fail the fail-loud propose path (it falls through to normal storage).
# --------------------------------------------------------------------------- #


def _gate_normalize_systems(
    proposed_type: str, from_system: str, to_system: str, source: str,
) -> "tuple[bool, str, str, str]":
    """(3d) System-id normalization gate — runs FIRST so every downstream gate
    and the canonical key see CANONICAL catalog ids ("Milwaukee MES" → mes-mil,
    "ERP" → erp when unique). A reference that normalizes to the ``None``
    sentinel names the knowledge graph itself (self-referential) — for
    JOIN-LIKE types the proposal is dropped at the source (extends 3b below,
    which only token-matches the raw strings). The known-systems read is LAZY
    (inside the function — no import-time I/O) and PROCESS-AGNOSTIC:
    ``known_systems()`` serves the hydrated catalog in the ECS app process
    and a TTL-cached Neptune read in the AgentCore runtime process, so this
    gate is live in BOTH (F6 — it was inert in the runtime, whose singleton
    catalog is never hydrated). Best-effort: a raising normalizer/source must
    never fail the propose path (falls through with the raw ids).

    R1 (ADR-0042 D4): when a reference verdicts AMBIGUOUS ("MES" matching
    mes-cr/mes-det/mes-mil), the stored (non-dropped) proposal carries a
    ``review_reason`` naming the candidates
    (``system_normalize.ambiguous_candidates``) so the review card can state
    which systems to confirm. Computed inside the same best-effort boundary —
    a raising candidate lookup never fails the propose path.

    Returns ``(dropped, from_system, to_system, review_reason)`` —
    ``dropped=True`` means the caller must return ``None``; otherwise the
    (possibly normalized) system ids replace the caller's, and
    ``review_reason`` ('' unless ambiguous) threads through to ``_build_item``.
    """
    review_reason = ""
    try:
        from .system_normalize import (
            VERDICT_AMBIGUOUS, ambiguous_candidates, resolve_system_id,
        )
        _known = known_systems()
        _norm_from, _from_verdict = resolve_system_id(from_system, _known)
        _norm_to, _to_verdict = resolve_system_id(to_system, _known)
        if proposed_type in ("join_path", "value_mapping", "identity_rule") and (
            _norm_from is None or _norm_to is None
        ):
            logger.info(
                f"dropped {proposed_type} with self-referential (knowledge-graph) "
                f"system reference: {from_system!r} → {to_system!r}"
            )
            return True, from_system, to_system, ""
        if _gate_drop_unknown_agent_systems(
            proposed_type, from_system, to_system, source,
            _known, _from_verdict, _to_verdict,
        ):
            return True, from_system, to_system, ""
        # R1 — state WHY an ambiguous reference is held for review. Dedupe on
        # the raw value so a proposal with from==to ambiguous emits one clause.
        seen_ambiguous: list[str] = []
        for raw, verdict in ((from_system, _from_verdict), (to_system, _to_verdict)):
            if verdict == VERDICT_AMBIGUOUS and raw not in seen_ambiguous:
                seen_ambiguous.append(raw)
        clauses = []
        for raw in seen_ambiguous:
            cands = ambiguous_candidates(raw, _known)
            if cands:
                clauses.append(
                    f"System reference '{raw}' is ambiguous across "
                    f"{', '.join(cands)}"
                )
        if clauses:
            review_reason = ("; ".join(clauses)
                             + " — confirm which system before accepting")
        if _norm_from is not None:
            from_system = _norm_from
        if _norm_to is not None:
            to_system = _norm_to
    except Exception as e:  # noqa: BLE001 - best-effort gate, never fail propose
        logger.warning(f"system-id normalization failed, using raw ids: {e}")
    return False, from_system, to_system, review_reason


def _gate_drop_unknown_agent_systems(
    proposed_type: str, from_system: str, to_system: str, source: str,
    known: list, from_verdict: str, to_verdict: str,
) -> bool:
    """(part of 3d) UNKNOWN-SYSTEM drop (agent-sourced only): a reference that
    matched NO catalog id/name/type is junk when an LLM emitted it (live
    examples: model ids, the string 'None', raw UUIDs, table names, site
    codes). USER-sourced proposals are exempt (an operator naming a system they
    know is coming stays stored raw), an AMBIGUOUS passthrough ("MES" matching
    several MES systems) is legitimate and NOT dropped (the verdict
    distinguishes the two), and an EMPTY catalog gives no basis to judge (base
    mode pre-onboarding) so everything stores raw.

    Returns ``True`` to DROP. Called inside ``_gate_normalize_systems``'s
    try/except, so it shares the same best-effort boundary.
    """
    from .system_normalize import VERDICT_NO_MATCH
    if (
        source != "user"
        and known
        and proposed_type in (
            "join_path", "value_mapping", "identity_rule", "field_annotation",
            # ADR-0053 E5: a drift finding naming a system that matches no
            # catalog id is junk — drift only means anything for a system the
            # engine actually knows (defense-in-depth; run_drift_check already
            # iterates onboarded systems only).
            "schema_drift",
            # Same posture for coverage-gap findings: the analyzer only scans
            # onboarded systems, so an unknown system id here is junk.
            "coverage_gap",
        )
        and VERDICT_NO_MATCH in (from_verdict, to_verdict)
    ):
        logger.info(
            f"dropped agent-sourced {proposed_type} naming unknown "
            f"system(s): {from_system!r} → {to_system!r}"
        )
        return True
    return False


def _gate_drop_degenerate_field_annotation(
    proposed_type: str, from_system: str, from_field: str, to_field: str,
    from_value: str, to_value: str, query_context: str,
) -> bool:
    """(3a) Deterministic degeneracy gate — drop low-value field_annotation
    noise at the source before any backend round-trip. Best-effort: a raising
    predicate must not break the fail-loud propose path, so it falls through to
    storage.

    Returns ``True`` to DROP.
    """
    if proposed_type != "field_annotation":
        return False
    if not _qualified_physical_field(from_field):
        raise ValueError("field annotation requires a qualified physical table.column endpoint")
    try:
        from .proposal_scoring import is_degenerate_field_annotation
        _degenerate = is_degenerate_field_annotation(
            from_system, from_field, to_field, from_value, to_value,
            query_context.replace("Auto-analysis: ", ""),
        )
    except Exception as e:  # noqa: BLE001 - best-effort gate, never fail propose
        logger.warning(f"degeneracy check failed, not dropping: {e}")
        _degenerate = False
    if _degenerate:
        logger.info(
            f"dropped degenerate field_annotation: {from_system}.{from_field}"
        )
        return True
    return False


def _gate_shape_reroute_to_insight(
    proposed_type: str, from_system: str, to_system: str,
    from_field: str, to_field: str, query_context: str,
) -> "tuple[str, str, str, str]":
    """(3e) Shape-gate — an identity_rule/join_path endpoint must be exactly one
    ``table.column`` reference. A field that fails the shape predicate means the
    LLM emitted a NARRATIVE finding (a sentence / prose pattern), not a rule:
    RE-ROUTE the same content to proposed_type='insight' (informational; its
    accept path never writes a graph rule) instead of storing a malformed rule
    or silently losing the finding. The original narrative is preserved in
    query_context (kept when already set; else the malformed field text becomes
    the finding). Runs BEFORE the degenerate-join drop (3b) so a SAME-SYSTEM
    sentence-shaped rule becomes an insight rather than being silently dropped
    as a self-join, and BEFORE the schema gate (3c) so the narrative is
    re-routed rather than dropped as a missing column. value_mapping is NOT
    shape-gated (empty fields are legal there). Best-effort: a raising
    predicate must not break the fail-loud propose path.

    Returns ``(proposed_type, from_field, to_field, query_context)`` —
    unchanged on pass, re-routed to ``insight`` with empty fields on re-route.
    """
    if proposed_type not in ("identity_rule", "join_path"):
        return proposed_type, from_field, to_field, query_context
    try:
        from .proposal_scoring import is_table_column_shaped
        _malformed = [
            f for f in (from_field, to_field) if not is_table_column_shaped(f)
        ]
    except Exception as e:  # noqa: BLE001 - best-effort gate, never fail propose
        logger.warning(f"shape-gate check failed, not re-routing: {e}")
        _malformed = []
    if _malformed:
        logger.info(
            f"re-routed sentence-shaped {proposed_type} to insight "
            f"({from_system} → {to_system})"
        )
        if not (query_context or "").strip():
            # Preserve the narrative: with no context set, the malformed
            # field text IS the finding.
            query_context = " ".join(m for m in _malformed if m).strip()
        proposed_type = "insight"
        from_field = ""
        to_field = ""
    return proposed_type, from_field, to_field, query_context


def _gate_drop_degenerate_join(
    proposed_type: str, from_system: str, from_field: str,
    to_system: str, to_field: str, from_value: str, to_value: str,
) -> bool:
    """(3b) Deterministic degeneracy gate for JOIN-LIKE proposals — drop, at the
    source, a self-referential/same-system self-join and any proposal that names
    the knowledge-graph target itself (the graph is the rules engine, not a
    joinable data source). Keeps the analyzer from emitting a "cross-system
    join" between a field and itself, or between the graph and itself. Runs
    AFTER the shape-gate (3e): a sentence-shaped rule has already been re-routed
    to ``insight`` (not join-like → never flagged here), so only WELL-SHAPED
    join-like proposals reach this drop. Best-effort: a raising predicate must
    not break the fail-loud propose path.

    Returns ``True`` to DROP.
    """
    if proposed_type not in ("join_path", "value_mapping", "identity_rule"):
        return False
    try:
        from .proposal_scoring import is_degenerate_join_proposal
        _degen_join = is_degenerate_join_proposal(
            from_system, from_field, to_system, to_field,
            from_value, to_value, proposed_type,
        )
    except Exception as e:  # noqa: BLE001 - best-effort gate, never fail propose
        logger.warning(f"join degeneracy check failed, not dropping: {e}")
        _degen_join = False
    if _degen_join:
        logger.info(
            f"dropped degenerate {proposed_type} (self-join / knowledge-graph "
            f"endpoint): {from_system}.{from_field} → {to_system}.{to_field}"
        )
        return True
    return False


def _gate_drop_equal_literal_echo(
    proposed_type: str, from_system: str, from_field: str,
    to_system: str, to_field: str, from_value: str, to_value: str,
) -> bool:
    """(3g) Equal-literal cross-field echo gate for ``value_mapping`` — drop,
    at the source, a SAME-SYSTEM proposal mapping one literal to ITSELF across
    two DIFFERENT columns: it encodes no transform and resolves no lookup.
    Predicate: ``proposal_scoring.is_equal_literal_echo`` (``.strip()`` exact
    equality, NO casefold — a case-changing pair encodes a real transform).
    PROPHYLACTIC: the same-system shape this gate drops is not currently
    present in the live pilot-env sample — the live degenerates are CROSS-system.

    Scope (pinned): SAME normalized system ``value_mapping`` ONLY. The
    CROSS-system equal-literal shape (value_mapping and crosswalk alike) is
    deliberately NOT gated here: dropping those rows at ingestion would
    starve the 3rd-distinct-pending-value → rule promotion trigger and the
    ADR-0051 P7 corroboration path (``_lpm_self_map_collapse``). The
    generation-side answer for the cross-system shape is onboarding-time
    promotion to field-grain identity rules (pre-campaign plan item 3), not
    a drop; review-side it is surfaced by the advisory
    ``cross_system_equal_literal`` badge flag
    (``proposal_scoring.is_cross_system_equal_literal``). ALL sources (agent
    AND user), like siblings 3a/3b: a user recording "these two fields carry
    the same literal" is describing a join_path, not a value mapping.

    Accepted tradeoff: these rows do not feed
    ``observe_pending_value_mapping`` grouping; intra-system discovery
    comes from ``join_discovery`` (data-sampling, not queue based).
    Best-effort sibling contract: a raising predicate never fails the propose
    path (falls through to storage). Returns ``True`` to DROP (one
    ``logger.info`` suppression line per drop).
    """
    if proposed_type != "value_mapping":
        return False
    try:
        from .proposal_scoring import is_equal_literal_echo
        _echo = is_equal_literal_echo(
            from_system, from_field, to_system, to_field, from_value, to_value)
    except Exception as e:  # noqa: BLE001 - best-effort gate, never fail propose
        logger.warning(f"equal-literal echo check failed, not dropping: {e}")
        _echo = False
    if _echo:
        _v = (from_value or "").strip()  # gate fired ⇒ trimmed values EQUAL
        _v = _v if len(_v) <= 40 else _v[:40] + "…"
        logger.info(
            f"dropped equal-literal cross-field echo value_mapping: "
            f"{from_system}.{from_field} == {to_system}.{to_field} "
            f"(shared value {_v!r})")
        return True
    return False


# Failure sentinels a to-side value/field can carry when a lookup FAILED —
# normalized to upper-snake before comparison ("NOT_FOUND", "Not Found",
# "N/A", "n-a", "None", "null" all match; a punctuation-only value like "-"
# normalizes to "" and matches too). Matching is BOTH whole-form and
# TOKEN-LEVEL: any '_'-token of the normalized form that is a sentinel flags
# the whole value, so multi-word annotations like UNKNOWN_PENDING_RESOLUTION,
# NOT_FOUND_IN_SYSTEM, or PENDING_LOOKUP are caught (a real identifier merely
# CONTAINING a sentinel substring — e.g. ANYNAVCO, PENDINGTON — stays safe
# because only whole tokens fire). A raw value that is FULLY PARENTHESIZED
# ("(unknown — pending resolution)", "(see note)") is also a sentinel: a
# parenthesized "value" is an annotation, never an identifier. Live evidence
# (2026-07): a speculative orchestrator proposal with
# ``to_value='(unknown — pending resolution)'`` passed the whole-match-only
# gate and was ingested at confidence 0.7. An EMPTY INPUT is exempt (checked
# before normalization): the reason-code path legitimately proposes a pending
# value_mapping with an EMPTY label ("never fabricated") for human fill-in.
_FAILED_LOOKUP_SENTINELS = frozenset({
    "NOT_FOUND", "NOTFOUND", "NO_MATCH", "NOMATCH", "N_A", "NA",
    "NONE", "NULL", "NIL", "UNKNOWN", "MISSING", "ERROR", "",
    "PENDING", "TBD", "UNRESOLVED",
})


def _is_failed_lookup_sentinel(value: str, *, token_match: bool = True) -> bool:
    """True when a NON-EMPTY value normalizes to a failure sentinel, when ANY
    '_'-token of the normalized form is a sentinel (multi-word annotations;
    disable via ``token_match=False`` for table.column-shaped FIELD names,
    where legitimate columns like ``error_code``/``missing_field`` carry
    sentinel tokens), or when the raw stripped form is fully parenthesized
    (an annotation, never an identifier). Pure."""
    import re as _re_fs  # module keeps re imports local to their consumers
    v = (value or "").strip()
    if not v:
        return False  # empty is a separate, legitimate contract (see above)
    if v.startswith("(") and v.endswith(")"):
        return True  # fully parenthesized "value" is an annotation
    norm = _re_fs.sub(r"[^A-Z0-9]+", "_", v.upper()).strip("_")
    if norm in _FAILED_LOOKUP_SENTINELS:
        return True
    if not token_match:
        return False
    toks = norm.split("_")
    if any(tok in _FAILED_LOOKUP_SENTINELS for tok in toks):
        return True
    # Two-token sentinels (NOT_FOUND, NO_MATCH, N_A) inside longer annotations.
    return any(
        f"{a}_{b}" in _FAILED_LOOKUP_SENTINELS for a, b in zip(toks, toks[1:])
    )


def _gate_drop_failed_lookup_vm(
    proposed_type: str, to_field: str, to_value: str,
) -> bool:
    """(3f) Failed-lookup gate for ``value_mapping`` proposals — a to-side
    ``to_value``/``to_field`` that is a failure sentinel (NOT_FOUND / N/A /
    None / null / a parenthesized or multi-word annotation like
    "(unknown — pending resolution)" …) records a FAILED SEARCH, not a learned
    mapping. Live evidence: such a proposal reached the queue with
    ``ai_confidence=high`` because the scorer verified the from-side only —
    drop it at ingestion so no scorer ever sees it. The VALUE side gets full
    token-level matching; the FIELD side stays whole-match (legitimate
    table.column names like ``orders.missing_field`` carry sentinel tokens).
    Pure and deterministic; returns ``True`` to DROP.
    """
    if proposed_type != "value_mapping":
        return False
    return _is_failed_lookup_sentinel(to_value) or _is_failed_lookup_sentinel(
        to_field, token_match=False
    )


def _gate_schema_check(
    proposed_type: str, from_system: str, from_field: str,
    to_system: str, to_field: str,
) -> bool:
    """(3c) Schema-existence gate — reject proposals whose columns DEFINITIVELY
    do not exist in the onboarded schema. The verdict is trustworthy because the
    check is deterministic (no LLM; uses onboarded schema cache) and the False
    verdict is only emitted on a definitive "column does not exist" (never on
    uncertainty). Best-effort: never raise, always fall through to storage on
    exceptions.

    Returns ``True`` to DROP.
    """
    try:
        from .schema_check import proposal_missing_field
        missing = proposal_missing_field(
            proposed_type, from_system, from_field, to_system, to_field
        )
        if missing:
            logger.info(
                f"dropped {proposed_type} with missing column: {missing}"
            )
            return True
    except Exception as e:  # noqa: BLE001 - best-effort gate, never fail propose
        logger.warning(f"schema gate check failed, not dropping: {e}")
    return False


def _qualified_physical_field(field: str) -> bool:
    parts = str(field or "").split(".")
    return len(parts) >= 2 and all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$-]*", part or "") for part in parts
    )


# --------------------------------------------------------------------------- #
# ADR-0051 P5 — durable co-reference staging buffer. Staged rows ride the SAME
# table under a ``staged#``-prefixed mapping_id AND subject_key with NO
# ``status`` attribute (the ``artifact_usage`` ledger idiom): the status-index
# never sees them, plain subject-key lookups never see them, and
# ``get_all_mappings`` filters the prefix — invisible to every lifecycle read.
# --------------------------------------------------------------------------- #
def _staging_applies(proposed_type: str, source: str, provenance: str,
                     from_system: str, to_system: str) -> bool:
    """Whether the ADR-0051 P5 staging buffer holds this proposal. Pure.

    Agent-sourced CROSS-SYSTEM ``value_mapping``/``join_path`` only. User
    AND steward sources bypass (ADR-0003 C — a direct human assertion is
    already confirmed; live-found 2026-08-07: a steward's interview answer
    was staged awaiting a "second observation" that can never come);
    measured/promoted pipelines bypass by provenance
    (``_STAGING_BYPASS_PROVENANCES``); same-system proposals are
    single-system observations (e.g. reason-code → label pairs read from one
    row), not co-references — they store directly, as before."""
    if proposed_type not in _STAGING_TYPES or source in ("user", "steward"):
        return False
    if PROPOSAL_STAGING_MIN_COREFERENCES <= 1:
        return False
    if (provenance or "").strip().lower() in _STAGING_BYPASS_PROVENANCES:
        return False
    return key_norm(from_system) != key_norm(to_system)


def _staged_observation_digest(target_key: str, query_context: str) -> str:
    """Distinctness discriminator for one staged observation. Pure.

    Two observations are DISTINCT when they differ in target (a new value
    pair / endpoint) or in the observing context — the same query replayed is
    still ONE co-reference."""
    return key_digest(f"{target_key}||{key_norm(query_context)}")


def _read_staged_row(table, staged_key: str) -> dict | None:
    """The live staged row for a staged subject key, honoring the lazy TTL.

    A staged row older than ``STAGING_TTL_DAYS`` is expired: best-effort
    deleted and reported absent, so a fresh observation restarts the buffer
    (findings-grade evidence decays — same posture as the insight TTL)."""
    rows = [r for r in _query_subject(table, staged_key)
            if str(r.get("mapping_id") or "").startswith(_STAGED_PREFIX)]
    if not rows:
        return None
    row = rows[0]  # newest first (query is ScanIndexForward=False)
    staged_at = _parse_iso(row.get("discovered_at", ""))
    now = datetime.now(timezone.utc)
    if staged_at is not None and (now - staged_at) > timedelta(days=STAGING_TTL_DAYS):
        for stale in rows:
            _delete_staged_row(table, stale)
        return None
    return row


def _delete_staged_row(table, row: dict) -> None:
    """Best-effort staged-row delete (the buffer is evidence, never critical)."""
    try:
        table.delete_item(Key={"mapping_id": row["mapping_id"],
                               "discovered_at": row["discovered_at"]})
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.debug("staged-row delete failed (ignored): %s", e)


def _write_staged_row(table, staged_key: str, digests: list[str],
                      contexts: list[str]) -> None:
    """Persist the staging buffer state for a subject. Fail-loud (put seam)."""
    _put_item(table, {
        "mapping_id": _STAGED_PREFIX + str(uuid.uuid4()),
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "subject_key": staged_key,
        "staged_digests": digests,
        "staged_contexts": contexts,
        # deliberately NO "status" attribute — invisible to lifecycle reads
    })


# Per-thread outcome marker so synchronous callers (the
# save_value_mapping tool path) can tell a STAGED observation (evidence
# recorded, no review row yet) apart from a gate DROP — reporting "dropped"
# for staged invited LLM reword-retries that defeat the ≥2-independent-
# observations bar. Set by ``log_proposed_mapping``; read via
# ``last_propose_outcome()``.
_last_propose_outcome = threading.local()


def last_propose_outcome() -> str:
    """``"staged"`` when the most recent ``log_proposed_mapping`` call on
    THIS thread buffered its observation (ADR-0051 P5); ``"already_stored"``
    when it restated a ValueMapping already in the graph
    (``_lpm_restates_stored_mapping``); ``""`` otherwise."""
    return getattr(_last_propose_outcome, "value", "")


def reset_propose_outcome() -> None:
    """Clear the per-thread outcome BEFORE calling ``log_proposed_mapping``.

    ``log_proposed_mapping`` resets on entry too; callers reset explicitly so
    the read stays correct even when the propose seam is replaced (mocks)."""
    _note_propose_outcome("")


def _note_propose_outcome(value: str) -> None:
    _last_propose_outcome.value = value


def _lpm_staging(table, keys: CanonicalKeys, action: str, proposed_type: str,
                 source: str, provenance: str, from_system: str,
                 to_system: str, query_context: str) -> tuple[bool, str, dict | None]:
    """ADR-0051 P5: hold a first-of-its-kind agent observation in the buffer.

    Runs ONLY for the plain ``insert`` action (a subject with lifecycle
    history — supersede chains, rejections, quarantine — is already past the
    evidence bar). Returns ``(buffered, query_context, stale_row)``:
    ``buffered`` True means no pending row is written (caller returns
    ``None``); on release the returned ``query_context`` names every staged
    observation and ``stale_row`` is the spent staged row the caller deletes
    ONLY AFTER the pending put succeeds (deleting first loses
    the evidence when the put raises)."""
    if action != "insert" or not _staging_applies(
            proposed_type, source, provenance, from_system, to_system):
        return False, query_context, None
    staged_key = _STAGED_PREFIX + keys.subject_key
    digest = _staged_observation_digest(keys.target_key, query_context)
    row = _read_staged_row(table, staged_key)
    digests = list((row or {}).get("staged_digests") or [])
    contexts = list((row or {}).get("staged_contexts") or [])
    if digest in digests:
        _note_propose_outcome("staged")
        return True, query_context, None  # identical re-observation — one co-reference
    digests.append(digest)
    contexts.append((query_context or "").strip()[:300] or "(unnamed observation)")
    if len(digests) < PROPOSAL_STAGING_MIN_COREFERENCES:
        # Ordering: the fail-loud put lands FIRST; the superseded row is
        # only deleted once the merged buffer is durable.
        _write_staged_row(table, staged_key, digests, contexts)
        if row is not None:
            _delete_staged_row(table, row)
        logger.info(
            "staged %s observation %d/%d on %s (ADR-0051 P5 — no review row yet)",
            proposed_type, len(digests), PROPOSAL_STAGING_MIN_COREFERENCES,
            keys.subject_key)
        _note_propose_outcome("staged")
        return True, query_context, None
    merged = "; ".join(contexts)
    return False, (f"{len(digests)} observed co-references (ADR-0051 P5): "
                   f"{merged}")[:500], row


# --------------------------------------------------------------------------- #
# ADR-0051 P7 — self-map suppression: an identity value pair already explained
# by a DIRECT-transform JoinPath is field-level corroboration, not a new row.
# --------------------------------------------------------------------------- #
# Graph JoinPath transforms that pass a value through unchanged (a direct
# translation edge); an absent transform reads as direct (legacy writers).
_DIRECT_JOINPATH_TRANSFORMS = ("", "identity", "direct")


def _direct_joinpath_rows(from_system: str, to_system: str) -> list[dict]:
    """Graph ``JoinPath`` rows between two systems (either direction), minus
    the switched-off ones (their own status, or their IdentityRule's).

    Best-effort seam → ``[]`` (a graph miss means "no covering rule", so the
    observation stores normally — fail-open, never fail-closed on storage)."""
    try:
        return _neptune_query(
            "MATCH (jp:JoinPath) WHERE ((jp.from_system = $a AND jp.to_system = $b) "
            "OR (jp.from_system = $b AND jp.to_system = $a)) "
            "AND " + _jp_rule.rule_active("jp") + " "
            + _jp_rule.drop_if_rule_inactive() +
            "RETURN jp.from_system AS from_system, jp.from_field AS from_field, "
            "jp.to_system AS to_system, jp.to_field AS to_field, "
            "jp.transform AS transform",
            {"a": from_system, "b": to_system}) or []
    except Exception as e:  # noqa: BLE001 - best-effort read
        logger.debug("direct-joinpath read failed (storing observation): %s", e)
        return []


def _field_refs_match(a: str, b: str) -> bool:
    """Case-insensitive field-reference comparison tolerating a bare column.

    Full normalized-path equality first (``key_norm_field`` — the ADR-0051 P9
    normalization); when either side is unqualified (dot-less), fall back to
    column-tail equality. Pure."""
    na, nb = key_norm_field(a), key_norm_field(b)
    if na == nb:
        return True
    if not na or not nb:
        return False
    if "." not in na or "." not in nb:
        return na.rsplit(".", 1)[-1] == nb.rsplit(".", 1)[-1]
    return False


def _joinpath_directly_covers(rows: list[dict], from_system: str,
                              from_field: str, to_system: str,
                              to_field: str) -> bool:
    """Whether any row is a DIRECT-transform JoinPath on this field pair. Pure."""
    for r in rows or []:
        transform = str(r.get("transform") or "").strip().lower()
        if transform not in _DIRECT_JOINPATH_TRANSFORMS:
            continue
        fs, ts = key_norm(r.get("from_system")), key_norm(r.get("to_system"))
        if (fs == key_norm(from_system) and ts == key_norm(to_system)
                and _field_refs_match(r.get("from_field"), from_field)
                and _field_refs_match(r.get("to_field"), to_field)):
            return True
        if (fs == key_norm(to_system) and ts == key_norm(from_system)
                and _field_refs_match(r.get("from_field"), to_field)
                and _field_refs_match(r.get("to_field"), from_field)):
            return True
    return False


def _record_staged_corroboration(from_system: str, from_field: str,
                                 to_system: str, to_field: str,
                                 concept: str, note: str) -> None:
    """Bump the P5 field-level (join_path-grain) corroboration count.

    A collapsed self-map is evidence that the covering direct rule WORKS: it
    lands as a staged observation on the field pair's ``join_path`` subject
    (never a new pending row — the pair already has a live graph rule).
    Best-effort/swallows: corroboration must never fail the propose path."""
    try:
        table = _get_table()
        keys = _canonical_keys_for(
            "join_path", from_system=from_system, from_field=from_field,
            to_system=to_system, to_field=to_field, concept=concept)
        staged_key = _STAGED_PREFIX + keys.subject_key
        digest = _staged_observation_digest(keys.target_key, note)
        row = _read_staged_row(table, staged_key)
        digests = list((row or {}).get("staged_digests") or [])
        contexts = list((row or {}).get("staged_contexts") or [])
        if digest in digests:
            return
        digests.append(digest)
        contexts.append((note or "").strip()[:300] or "(unnamed corroboration)")
        # Ordering: put the merged row first; delete the superseded row
        # only once the new evidence is durable.
        _write_staged_row(table, staged_key, digests, contexts)
        if row is not None:
            _delete_staged_row(table, row)
    except Exception as e:  # noqa: BLE001 - best-effort evidence
        logger.debug("staged corroboration failed (ignored): %s", e)


def _lpm_self_map_collapse(proposed_type: str, from_system: str,
                           from_field: str, from_value: str, to_system: str,
                           to_field: str, to_value: str, concept: str,
                           source: str) -> bool:
    """ADR-0051 P7: collapse a cross-system identity value pair to
    field-level corroboration when a DIRECT-transform JoinPath already covers
    the field pair. Returns True when collapsed (nothing stored).

    Covers UUID and name variants alike (normalized value equality). Without
    a covering rule the identity pair IS information and stores normally;
    same-system pairs are the degenerate-join gate's turf; user source
    bypasses (ADR-0003 C). Accepted-ROW coverage is handled upstream by
    ``_lpm_covered_by_rule`` — this is the graph-JoinPath analogue."""
    if proposed_type != "value_mapping" or source == "user":
        return False
    lhs = str(from_value or "").strip().casefold()
    if not lhs or lhs != str(to_value or "").strip().casefold():
        return False
    if key_norm(from_system) == key_norm(to_system):
        return False
    rows = _direct_joinpath_rows(from_system, to_system)
    if not _joinpath_directly_covers(rows, from_system, from_field,
                                     to_system, to_field):
        return False
    _record_staged_corroboration(
        from_system, from_field, to_system, to_field, concept,
        f"self-map corroboration: {from_value} observed identical in both systems")
    logger.info(
        "collapsed self-map value_mapping %s.%s=%s → %s.%s to field-level "
        "corroboration (direct JoinPath covers the pair — ADR-0051 P7)",
        from_system, from_field, from_value, to_system, to_field)
    return True


# Proposal types whose accepted form is a graph ValueMapping
# (``_apply_value_mapping``): a ValueMapping already in the graph for the same
# pair answers them, whichever writer stored it (onboarding's equipment and
# Resolve passes write theirs with no ledger row behind them).
_STORED_VALUE_MAPPING_TYPES = ("value_mapping", "crosswalk")

_STORED_VALUE_MAPPING_CYPHER = (
    "MATCH (vm:ValueMapping) WHERE (vm.from_system = $a AND vm.to_system = $b) "
    "OR (vm.from_system = $b AND vm.to_system = $a) "
    "RETURN vm.id AS id, vm.from_system AS from_system, "
    "vm.from_field AS from_field, vm.from_value AS from_value, "
    "vm.to_system AS to_system, vm.to_field AS to_field, "
    "vm.to_value AS to_value LIMIT $lim")
#: Most stored ValueMappings one system pair is judged against. A read that
#: answers more is not judged: the observation files.
STORED_VALUE_MAPPING_ROW_CAP = 500
#: Server limit of the optional reads below (``db.graph_statement_limit``;
#: the client read timeout is this plus ``db.GRAPH_READ_MARGIN_S``). They only
#: decide whether a proposal is a restatement, so a slow graph files it.
STORED_VALUE_MAPPING_READ_TIMEOUT_MS = 3_000
#: Client connect timeout of the same reads (the shared default is 10 s).
STORED_VALUE_MAPPING_CONNECT_TIMEOUT_S = 2.0


def _optional_graph_read(cypher: str, parameters: dict) -> "list | None":
    """One read attempt under the short limits
    (``db.single_attempt_graph_reads``: never resubmitted), or ``None`` when
    it failed. A failed read the graph seam degrades to no rows answers
    ``[]``; both mean the proposal files. Best-effort: never raises."""
    from .db import single_attempt_graph_reads
    try:
        with single_attempt_graph_reads(
                timeout_ms=STORED_VALUE_MAPPING_READ_TIMEOUT_MS,
                connect_timeout_s=STORED_VALUE_MAPPING_CONNECT_TIMEOUT_S):
            return list(_neptune_query(cypher, parameters) or [])
    except Exception as e:  # noqa: BLE001 - best-effort read
        logger.debug("optional graph read failed (proposal files): %s",
                     type(e).__name__)
        return None


def _read_stored_value_mappings(system_a: str, system_b: str) -> "list | None":
    """The ValueMappings between two systems, or ``None`` when the read failed
    or answered more than :data:`STORED_VALUE_MAPPING_ROW_CAP` rows."""
    rows = _optional_graph_read(_STORED_VALUE_MAPPING_CYPHER, {
        "a": system_a, "b": system_b, "lim": STORED_VALUE_MAPPING_ROW_CAP + 1})
    if rows is not None and len(rows) > STORED_VALUE_MAPPING_ROW_CAP:
        logger.info("stored value mappings %s/%s: more than %d; restatements "
                    "between them are not judged", system_a, system_b,
                    STORED_VALUE_MAPPING_ROW_CAP)
        return None
    return rows


def _stored_value_mapping_rows(system_a: str, system_b: str) -> "list | None":
    """Graph ``ValueMapping`` rows between two systems, either direction, or
    ``None`` when unknown (the observation then files). Read once per system
    pair per operation (``stored_value_mapping_memo.operation_scope``)."""
    from . import stored_value_mapping_memo
    pair = tuple(sorted((key_norm(system_a), key_norm(system_b))))
    return stored_value_mapping_memo.memoized(
        ("value_mappings", *pair),
        lambda: _read_stored_value_mappings(system_a, system_b))


#: One column of one system: the schema fields ending in it (two are enough
#: to know the column is not unique).
_FIELD_BY_COLUMN_CYPHER = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field) "
    "WHERE toLower(f.name) = $col "
    "RETURN DISTINCT se.name AS table_name, f.name AS field_name LIMIT 2")


def _unique_field_path(system: str, column: str) -> str:
    """The one ``table.column`` of ``system`` whose column is ``column``,
    normalized (``key_norm_field``), or ``''`` when the schema holds none or
    several, or the read failed. Read once per operation, like the pair."""
    from . import stored_value_mapping_memo
    rows = stored_value_mapping_memo.memoized(
        ("field", key_norm(system), column),
        lambda: _optional_graph_read(_FIELD_BY_COLUMN_CYPHER,
                                     {"sid": system, "col": column}))
    if not rows or len(rows) != 1:
        return ""
    return key_norm_field(f"{rows[0].get('table_name') or ''}."
                          f"{rows[0].get('field_name') or ''}")


def _stored_field_matches(system: str, stored_field: str, field: str) -> bool:
    """Whether a stored mapping's field on ``system`` is this proposal's
    field: the same normalized path (``key_norm_field``), or a bare column on
    one side that the system's schema resolves to exactly one ``table.column``,
    equal to the other side. Anything it cannot prove is no match, so the
    proposal files. Unlike ``_field_refs_match``, a shared column name alone
    is never enough."""
    a, b = key_norm_field(stored_field), key_norm_field(field)
    if not a or not b:
        return False
    if a == b:
        return True
    if ("." in a) == ("." in b):
        return False
    bare, qualified = (a, b) if "." not in a else (b, a)
    if qualified.rsplit(".", 1)[-1] != bare:
        return False
    return _unique_field_path(system, bare) == qualified


def _same_value_endpoint(row: dict, side: str, system: str, field: str,
                         value: str) -> bool:
    """Whether ``row``'s ``side`` endpoint is this (system, field, value):
    systems and values compared normalized (``key_norm``), fields through
    ``_stored_field_matches`` (last, as it may read the schema)."""
    return (key_norm(row.get(f"{side}_system")) == key_norm(system)
            and key_norm(row.get(f"{side}_value")) == key_norm(value)
            and _stored_field_matches(system, row.get(f"{side}_field"), field))


def _stored_value_mapping_match(
    rows: "list[dict] | None", from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str,
) -> dict | None:
    """The first stored row with the same systems, fields and values as the
    observation, in either direction, or ``None`` (also for unknown rows,
    ``None``). Reads the schema only for a bare field against a qualified one
    (``_stored_field_matches``)."""
    for row in rows or []:
        if ((_same_value_endpoint(row, "from", from_system, from_field, from_value)
             and _same_value_endpoint(row, "to", to_system, to_field, to_value))
                or (_same_value_endpoint(row, "from", to_system, to_field, to_value)
                    and _same_value_endpoint(row, "to", from_system, from_field,
                                             from_value))):
            return row
    return None


def _record_stored_mapping_confirmation(node_id: str) -> bool:
    """Count a restatement on the accepted ledger row whose applied node is
    ``node_id`` (``_record_coverage_evidence``, the counter an accepted rule
    gets for an observation it covers). Returns whether such a row exists; a
    node written by onboarding has none, and nothing is recorded for it."""
    if not node_id:
        return False
    for row in _accepted_coverage_rows():
        if row.get("neptune_node_id") == node_id:
            _record_coverage_evidence(row)
            return True
    return False


def _lpm_restates_stored_mapping(
    proposed_type: str, from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str, source: str,
) -> bool:
    """Suppress an observation that restates a ValueMapping already in the
    graph (same systems, fields and values, either direction): it is not a
    new mapping, so nothing is staged or queued, and the propose outcome
    reads ``already_stored``. A person's own proposal (``source="user"``)
    always files (ADR-0003 C). Returns ``True`` when suppressed."""
    if proposed_type not in _STORED_VALUE_MAPPING_TYPES or source == "user":
        return False
    if not str(from_value or "").strip() or not str(to_value or "").strip():
        return False
    stored = _stored_value_mapping_match(
        _stored_value_mapping_rows(from_system, to_system),
        from_system, from_field, from_value, to_system, to_field, to_value)
    if stored is None:
        return False
    counted = _record_stored_mapping_confirmation(str(stored.get("id") or ""))
    _note_propose_outcome("already_stored")
    logger.info(
        "%s %s.%s=%s -> %s.%s=%s restates stored ValueMapping %s; not queued "
        "(evidence %s)", proposed_type, from_system, from_field, from_value,
        to_system, to_field, to_value, stored.get("id"),
        "counted on its accepted row" if counted else "has no ledger row to count on")
    return True


def _lpm_observation_suppressed(
    proposed_type: str, from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str, transform: str,
    concept: str, source: str,
) -> bool:
    """Pre-classification suppression: covered-by-accepted-rule (evidence
    bump, ADR-0043), a restatement of a ValueMapping already in the graph
    (``_lpm_restates_stored_mapping``) or ADR-0051 P7 self-map collapse
    (corroboration bump).
    True ⇒ the observation was absorbed as evidence; no row is written."""
    if _lpm_covered_by_rule(
            proposed_type, from_system, from_field, from_value,
            to_system, to_field, to_value, transform):
        return True
    if _lpm_restates_stored_mapping(
            proposed_type, from_system, from_field, from_value,
            to_system, to_field, to_value, source):
        return True
    return _lpm_self_map_collapse(
        proposed_type, from_system, from_field, from_value,
        to_system, to_field, to_value, concept, source)


# --------------------------------------------------------------------------- #
# ADR-0051 P8 — cross-site guard: a system that serves one plant writes that
# plant's local names, so two systems at DIFFERENT plants (of any class: two
# MES, or a plant MES and another plant's historian or maintenance system)
# share identifier formats, not identities, unless an All-Sites authority
# owns the concept. Same rule as the onboarding Resolve step's
# ``resolve_site_gate`` (ADR-0051 (e), amended 2026-10-16 and 2026-10-19).
# --------------------------------------------------------------------------- #
_CROSS_SITE_GUARDED_TYPES = ("value_mapping", "join_path", "identity_rule",
                             "crosswalk")


def _active_authority_rows(concept: str) -> list:
    """The active ``AuthorityRule`` rows for ``concept`` (matched by the
    concept PROPERTY, casefolded and key-normalized in Python: an exact-match
    Cypher lookup fails closed on spelling drift), each with a non-empty
    ``sid``; soft-deactivated rules and those of a system being removed
    (``catalog.removed_ids``) are left out. A read failure raises."""
    from .catalog import removed_ids
    want = key_norm(str(concept or "").casefold())
    rows = _neptune_query(
        "MATCH (ar:AuthorityRule) "
        "WHERE coalesce(ar.status, '') <> 'deactivated' "
        "OPTIONAL MATCH (s:System) WHERE s.id = ar.system_id "
        "RETURN ar.id AS id, ar.concept AS concept, ar.system_id AS sid, "
        "ar.method AS method, ar.approved_by AS approved_by, "
        "ar.evidence AS evidence, coalesce(s.removal_incomplete, false) AS removal_incomplete",
        {}) or []
    removing = removed_ids(rows, "sid")
    return [r for r in rows
            if key_norm(str(r.get("concept") or "").casefold()) == want
            and str(r.get("sid") or "").strip() and str(r.get("sid")).strip() not in removing]


def _authority_vouch_verdict(concept: str, entries_by_id: dict, *,
                             known_owner_only: bool = False) -> tuple[str, str]:
    """Classify the All-Sites authority vouching for ``concept`` (ADR-0052 B4).

    Reads the active authority rules for ``concept``
    (:func:`_active_authority_rows`: a deactivated authority never vouches,
    nor does a system being removed). An authority whose catalog entry is
    UNKNOWN or unsited (``site_of`` → ``""`` — the wildcard/All-Sites
    spelling) is an All-Sites candidate; a read failure raises to the
    caller's fail-open wrapper. With ``known_owner_only`` (onboarding's site
    gate, which withholds rather than proposes) only an owner in
    ``entries_by_id`` known to be unsited is a candidate.

    Returns ``("steward", ar_id)`` when a matching All-Sites authority is
    steward-confirmed (user-tier ``method`` stamp or non-empty
    ``approved_by``) or evidence-bearing (non-empty ``evidence`` — the seam
    for a future value-overlap minting path; the overlap computation itself
    is deferred, ADR-0052); ``("unverified", ar_id)`` when All-Sites
    authorities exist but NONE is verified (machine claims never vouch for
    machine claims); ``("none", "")`` when no All-Sites authority owns
    the concept."""
    from .metric_multisite import site_of
    unverified_id = ""
    for r in _active_authority_rows(concept):
        sid = str(r.get("sid") or "").strip()
        entry = entries_by_id.get(sid.casefold())
        if entry is None and known_owner_only:
            continue  # owner not known: it can't be shown to be All Sites
        if entry is not None and site_of(entry) != "":
            continue  # sited authority — never satisfies P8
        ar_id = str(r.get("id") or "").strip() or f"ar-{sid}"
        if (str(r.get("method") or "").strip()
                in ("user_confirmed", "crosswalk_confirmed")
                or str(r.get("approved_by") or "").strip()
                or str(r.get("evidence") or "").strip()):
            return ("steward", ar_id)
        unverified_id = unverified_id or ar_id
    return ("unverified", unverified_id) if unverified_id else ("none", "")


def _gate_cross_site_namespace(proposed_type: str, from_system: str,
                               to_system: str, concept: str,
                               source: str) -> tuple[str, str]:
    """ADR-0051 P8 gate. Returns a verdict pair: ``("", "")`` pass (steward-
    confirmed or evidence-bearing All-Sites authority — ADR-0052 B4);
    ``("drop", reason)`` when NO All-Sites authority exists at all;
    ``("flag", reason)`` when one exists but is unverified — flag-not-drop:
    the proposal proceeds to pending carrying the stated ``review_reason``.

    Fires ONLY when every piece of metadata is present and adverse: both
    systems known, at DIFFERENT non-empty ``site_of`` sites (compared without
    case), whatever their ``system_type``. A same-class pair (two MES) and a
    cross-class pair (a plant MES and another plant's historian) are the same
    collision: each plant's systems write that plant's local names. Missing
    metadata or any backend failure ⇒ fail-open (this is a proposal-quality
    gate, not a security gate). User source bypasses (ADR-0003 C)."""
    if source == "user" or proposed_type not in _CROSS_SITE_GUARDED_TYPES:
        return ("", "")
    try:
        from .metric_multisite import site_of
        entries = {str(s.get("id") or "").strip().casefold(): s
                   for s in known_systems() if s.get("id")}
        a = entries.get(str(from_system or "").strip().casefold())
        b = entries.get(str(to_system or "").strip().casefold())
        if not a or not b:
            return ("", "")  # fail-open: unknown systems
        site_a, site_b = site_of(a), site_of(b)
        if (not site_a or not site_b
                or site_a.strip().casefold() == site_b.strip().casefold()):
            return ("", "")  # fail-open on missing/wildcard site metadata
        concept_norm = str(concept or "").strip().casefold()
        if not concept_norm or concept_norm == "unknown":
            # MISSING METADATA, not adverse evidence: the R2
            # ingest default stamps 'unknown' — fail-open per ADR-0051 (e).
            return ("", "")
        verdict, ar_id = _authority_vouch_verdict(concept, entries)
        if verdict == "steward":
            return ("", "")
        if verdict == "unverified":
            return ("flag", "cross-site link vouched only by unverified "
                            f"authority {ar_id}")
        return ("drop",
                f"site-local identifier namespaces collide on format: "
                f"'{concept or 'unspecified'}' between "
                f"{_cross_site_systems(a, b)} at {site_a} and {site_b} "
                f"needs an All-Sites authority or a steward assertion")
    except Exception as e:  # noqa: BLE001 - fail-open quality gate
        logger.debug("cross-site guard skipped (fail-open): %s", e)
        return ("", "")


def _cross_site_systems(a: dict, b: dict) -> str:
    """How the P8 drop reason names the two systems: ``two MES systems`` for
    one class, ``one MES system and one Historian system`` for two, ``two
    systems`` when either class is unknown."""
    type_a = str(a.get("system_type") or "").strip()
    type_b = str(b.get("system_type") or "").strip()
    if type_a and type_a.casefold() == type_b.casefold():
        return f"two {type_a} systems"
    if type_a and type_b:
        return f"one {type_a} system and one {type_b} system"
    return "two systems"


def _gate_cross_site_apply(proposed_type: str, from_system: str,
                           to_system: str, concept: str, source: str,
                           ambiguity_reason: str) -> tuple[bool, str]:
    """Apply the P8 verdict pair inside the gate pipeline.

    Returns ``(dropped, review_reason)``. A drop verdict drops the proposal
    (logged, unchanged behavior). A flag verdict (ADR-0052 B4 flag-not-drop)
    carries its stated reason forward on the shared ``review_reason`` slot;
    the vouching reason takes precedence over the 3d ambiguity default
    because B1 probe 5 and B2's conflict display key on its exact wording
    (in practice the two cannot co-occur: an ambiguous system passthrough
    never resolves to a known catalog entry, so P8 fails open)."""
    verdict, reason = _gate_cross_site_namespace(
        proposed_type, from_system, to_system, concept, source)
    if verdict == "drop":
        logger.info(f"dropped cross-site proposal (ADR-0051 P8): "
                    f"{reason}")
        return True, ambiguity_reason
    if verdict == "flag":
        logger.info(f"flagged cross-site proposal (ADR-0052 B4): {reason}")
        return False, reason
    return False, ambiguity_reason


# --------------------------------------------------------------------------- #
# log_proposed_mapping — decomposed pipeline. Each phase is a module-level
# ``_lpm_*`` helper with explicit inputs/outputs (no shared mutable state).
#
# proposed_type: join_path | value_mapping | identity_rule | field_annotation |
# crosswalk | concept_vocab | guidance_note. ``concept_vocab``/``guidance_note``
# (ADR-0050, vocabulary lifecycle) are narrative-ish types like ``insight``:
# the join-shape/degeneracy/schema gates allowlist the join-like types, so
# these two are excluded from every drop/re-route by construction (see
# ``VOCAB_PROPOSED_TYPES``). ``description`` is the minted concept's cosmetic
# definition (ADR 0021), stored only when non-empty.
#
# Source-level noise prevention (best-effort — a failing predicate falls
# through to normal storage rather than raising; callers must tolerate a
# ``None`` return):
#   * A degenerate ``field_annotation`` (self-referential / describe-output
#     artifact — ``proposal_scoring.is_degenerate_field_annotation``) is
#     DROPPED at ingestion (not stored).
#   * A degenerate JOIN-LIKE proposal (``join_path``/``value_mapping``/
#     ``identity_rule``) — a same-system/same-field self-join, a value mapped
#     to itself, or one that names the knowledge-graph target itself (the
#     graph is the rules engine, not a joinable data source; see
#     ``proposal_scoring.is_degenerate_join_proposal``) — is DROPPED.
#   * EQUAL-LITERAL ECHO (3g) — a SAME-SYSTEM ``value_mapping`` carrying the
#     SAME trimmed literal across two DIFFERENT columns
#     (``proposal_scoring.is_equal_literal_echo``) is DROPPED — it encodes no
#     transform. PROPHYLACTIC — a shape not currently present live. CROSS-system
#     equal-literal pairs (value_mapping and crosswalk) are NOT dropped —
#     ingestion drops would starve the value-promotion trigger and ADR-0051 P7;
#     they are surfaced review-side by the advisory
#     ``cross_system_equal_literal`` badge flag instead.
#   * SYSTEM-ID NORMALIZATION (3d) — ``from_system``/``to_system`` are
#     normalized against the live catalog
#     (``system_normalize.resolve_system_id``; the catalog is read LAZILY). A
#     join-like proposal whose system reference normalizes to the ``None``
#     sentinel (names the knowledge graph itself) is DROPPED. An AGENT-sourced
#     (``source != 'user'``) join-like or ``field_annotation`` proposal whose
#     system reference matches NO catalog id/name/type (the ``no_match``
#     verdict — junk like model ids, 'None', raw UUIDs, table names) is also
#     DROPPED; user-sourced proposals, ambiguous passthroughs ("MES" matching
#     several systems), and empty-catalog reads are exempt (stored raw).
#   * SHAPE-GATE (3e) — an ``identity_rule``/``join_path`` whose ``from_field``
#     or ``to_field`` is not ``table.column``-shaped
#     (``proposal_scoring.is_table_column_shaped``) is RE-ROUTED to
#     ``proposed_type='insight'`` (a narrative FINDING — reviewable context
#     that never becomes a graph rule), with the original narrative preserved
#     in ``query_context``.
# --------------------------------------------------------------------------- #


def _lpm_ingestion_gates(
    proposed_type: str, from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str, query_context: str,
    source: str, concept: str = "",
) -> tuple | None:
    """Run the ingestion gates in the pinned order 3d → P8 → 3a → 3e → 3b → 3g → 3f → 3c.

    Each ``_gate_*`` helper carries its gate's rationale and best-effort
    contract; ordering is behavioral (3d canonicalizes ids first; P8 needs
    them; 3e re-routes BEFORE 3b/3c can drop the narrative; 3g runs AFTER 3b
    so the same-field self-map stays 3b's drop). Returns ``None`` on DROP,
    else the (possibly rewritten) 7-tuple below; a P8 flag reason
    (ADR-0052 B4) rides the ``ambiguity_reason`` slot."""
    dropped, from_system, to_system, ambiguity_reason = _gate_normalize_systems(
        proposed_type, from_system, to_system, source)
    if dropped:
        return None
    dropped, ambiguity_reason = _gate_cross_site_apply(
        proposed_type, from_system, to_system, concept, source,
        ambiguity_reason)
    if dropped:
        return None
    if _gate_drop_degenerate_field_annotation(
            proposed_type, from_system, from_field, to_field,
            from_value, to_value, query_context):
        return None
    proposed_type, from_field, to_field, query_context = (
        _gate_shape_reroute_to_insight(
            proposed_type, from_system, to_system,
            from_field, to_field, query_context))
    if _gate_drop_degenerate_join(
            proposed_type, from_system, from_field, to_system, to_field,
            from_value, to_value):
        return None
    if _gate_drop_equal_literal_echo(
            proposed_type, from_system, from_field, to_system, to_field,
            from_value, to_value):
        return None
    if _gate_drop_failed_lookup_vm(proposed_type, to_field, to_value):
        # (3f) A sentinel to-side records a FAILED SEARCH, not a mapping.
        logger.info(
            f"dropped failed-lookup value_mapping (sentinel to-side): "
            f"{from_system}.{from_field}={from_value} → "
            f"{to_system}.{to_field}={to_value}")
        return None
    if _gate_schema_check(
            proposed_type, from_system, from_field, to_system, to_field):
        return None
    return (proposed_type, from_system, to_system, from_field, to_field,
            query_context, ambiguity_reason)


def _lpm_covered_by_rule(
    proposed_type: str, from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str, transform: str,
) -> bool:
    """Suppress a fresh per-value observation covered by a broad accepted rule.

    Broad accepted rules already encode the field pair + transform. A fresh
    per-value observation covered by one is evidence for that rule, not a new
    review row. This runs before canonical-key lookup by design. Returns
    ``True`` when the observation was suppressed."""
    covering_rule = _accepted_rule_covering_observation(
        proposed_type, from_system, from_field, from_value,
        to_system, to_field, to_value, transform,
    )
    if covering_rule is None:
        return False
    _record_coverage_evidence(covering_rule)
    logger.debug(
        "suppressed covered %s observation for %s.%s -> %s.%s; rule=%s",
        proposed_type, from_system, from_field, to_system, to_field,
        covering_rule.get("mapping_id"),
    )
    return True


def _lpm_classify(
    proposed_type: str, from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str, concept: str,
    query_context: str, source: str, description: str = "", reason: str = "",
    guidance_scope: str = "",
) -> tuple:
    """Canonical key → subject-key GSI query → collision classification
    (ADR 0003 C/D). Returns ``(table, keys, prior, action, existing_id,
    rows)``; RAISES on query failure (fail-loud)."""
    table = _get_table()
    keys = _canonical_keys_for(
        proposed_type,
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value,
        concept=concept, annotation=query_context.replace("Auto-analysis: ", ""),
        description=description, reason=reason, guidance_scope=guidance_scope,
    )
    prior = _expire_stale_insight_pendings(_query_subject(table, keys.subject_key))
    if proposed_type == "metric_definition":
        # Unlike record_user_correction, a formula filed with source=user is
        # STILL pending (ADR-0066 D3). Preserve C2 idempotency before applying
        # the generic user-wins branch to a genuine content edit.
        exact = [row for row in prior if row.get("target_key") == keys.target_key]
        pending = [row for row in exact if row.get("status") == "pending"]
        accepted = [row for row in exact if row.get("status") == "accepted"]
        if pending:
            action, existing_id, rows = "dup_pending", pending[0]["mapping_id"], []
        elif accepted:
            action, existing_id, rows = "noop", accepted[0]["mapping_id"], []
        else:
            action, existing_id, rows = _classify_collision(keys, prior, source)
    else:
        action, existing_id, rows = _classify_collision(keys, prior, source)
    return table, keys, prior, action, existing_id, rows


def _lpm_early_action(
    action: str, keys: CanonicalKeys, existing_id: str | None,
    rows: list[dict],
) -> tuple[bool, str | None]:
    """Handle the no-write collision actions. Returns ``(handled, result)`` —
    when ``handled`` is True the caller returns ``result`` without writing."""
    if action in ("dup_pending", "noop"):
        logger.info(f"Proposal {action} on {keys.target_key} → {existing_id}")
        return True, existing_id
    if action == "cooldown_noop":
        # ADR-0042 D1: a supersede inside the cooldown window is rediscovery
        # noise — dup-style no-op (no row, no supersede, existing id returned).
        logger.info(
            f"Proposal cooldown_noop on {keys.subject_key} → {existing_id} "
            f"(subject superseded within {SUBJECT_SUPERSEDE_COOLDOWN_HOURS}h)"
        )
        return True, existing_id
    if action == "rejection_damped":
        # ADR-0042 D3: repeatedly-rejected re-proposal inside the cooldown —
        # dropped (no row). Callers already tolerate a None return.
        logger.info(
            f"Proposal rejection_damped on {keys.target_key} "
            f"({len(rows)} prior rejection(s), latest within "
            f"{REJECTION_REPROPOSAL_COOLDOWN_DAYS}d) — dropped"
        )
        return True, None
    return False, None


def _lpm_apply_action(
    table, action: str, rows: list[dict], prior: list[dict],
    ambiguity_reason: str, endpoints: tuple = (),
) -> tuple[str, str, list[dict]]:
    """Apply the write-path collision action; returns ``(review_reason,
    quarantined, newly)`` for the item build.

    ADR-0042 D4: one shared review_reason attribute. The ambiguity reason
    (R1, from gate 3d) is the default; the quarantine (R2) and rejection
    history (R3, applied later in ``_lpm_store``) branches override it with
    their more specific reason. ``endpoints`` (the proposal's
    ``(proposed_type, from_*, to_*, source)`` tuple) feeds the ADR-0051 P3
    reverse-direction contradiction probe when no stronger reason applies."""
    review_reason = ambiguity_reason
    quarantined = ""
    newly: list[dict] = []
    if action == "supersede":
        _supersede(table, rows)
        newly = rows
    elif action == "quarantine":
        # ADR-0042 D2: store the incoming proposal pending + quarantined with
        # the stated flip-flop reason; prior active rows are NOT superseded.
        quarantined = "true"
        review_reason = _flipflop_review_reason(prior)
    elif action == "record_disagreement":
        # ADR-0050 PINNING: an accepted USER assertion on the subject is never
        # silently superseded by later agent learning — the colliding agent
        # proposal lands pending with the steward conflict-surface reason
        # (nothing is superseded; _classify_collision already guarantees that).
        review_reason = "conflicts-with-user-assertion"
    if not review_reason and not quarantined and endpoints:
        review_reason = _reverse_conflict_review_reason(*endpoints)
    return review_reason, quarantined, newly


def _lpm_rejection_flags(item: dict, action: str, rows: list[dict]) -> None:
    """Attach consumable rejection history + the review reason for the two
    rejected-shape re-proposal actions (ADR-0042 D3 R3 / ADR-0053 E6).

    ``prior_rejections`` is dict-shaped ({"mapping_id", "rejected_at"} per
    prior rejected row — additive; readers handle both shapes). The reason
    OVERRIDES any weaker flag already on the item: the pinned E6 tombstone
    wording (naming date + mapping id) for a live tombstone, R3's
    "state what changed" text for undated legacy history."""
    if action not in ("insert_with_rejection_history", "tombstoned"):
        return
    item["prior_rejections"] = [
        {"mapping_id": r["mapping_id"],
         "rejected_at": r.get("rejected_at", "")}
        for r in rows
    ]
    item["review_reason"] = (
        _tombstone_review_reason(rows) if action == "tombstoned"
        else _rejection_review_reason(rows))


def _lpm_store(
    *, table, keys: CanonicalKeys, action: str, rows: list[dict],
    prior: list[dict], newly: list[dict], review_reason: str,
    quarantined: str, from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str, concept: str,
    confidence: float, query_context: str, proposed_type: str, source: str,
    tool_calls_count: int, description: str, transform: str,
    time_key: dict | None, method: str, reason: str, provenance: str,
    guidance_scope: str = "",
) -> str:
    """Build the pending item, attach rejection history, put, log, post-put.

    ``transform`` and ``time_key`` are ADDITIVE, threaded through to the
    stored item (and, on accept, to the Neptune ``JoinPath`` node):
    ``transform`` is a ``join_path``'s real value transform (identity |
    casefold | strip_prefix:<affix> | strip_suffix:<affix> | token_extract,
    default empty → identity); ``time_key`` is an optional
    ``{from_time_field, to_time_field, semantics}`` for time-window alignment.
    Both default off, so every other type and every legacy caller is
    unaffected. RAISES on put failure (fail-loud)."""
    mapping_id, _discovered_at, item = _build_item(
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value, concept=concept,
        confidence=confidence, query_context=query_context, proposed_type=proposed_type,
        keys=keys, source=source, status="pending",
        version=_next_version(prior, newly), tool_calls_count=tool_calls_count,
        to_description=description, transform=transform, time_key=time_key,
        review_reason=review_reason, quarantined=quarantined,
        method=method, reason=reason, provenance=provenance,
        guidance_scope=guidance_scope,
    )
    # Rejected-shape re-proposals (ADR-0042 D3 R3 / ADR-0053 E6 tombstone):
    # consumable history + the action's review reason — _lpm_rejection_flags
    # is a no-op for every other action.
    _lpm_rejection_flags(item, action, rows)
    # ADR-0063 D1: ground a CROSS-SYSTEM target BEFORE the put, so review never
    # sees a row whose grounding is unknown-because-nobody-looked. Best-effort
    # and ABSENT-on-doubt — it never blocks or delays the filing.
    _attach_target_grounding(item)
    _put_item(table, item)
    logger.info(
        f"Proposed mapping logged: {mapping_id} ({action}, v{item['version']}, "
        f"{from_system}.{from_field} → {to_system}.{to_field})"
    )
    _lpm_post_put(mapping_id, item, proposed_type, source)
    return mapping_id


def _lpm_post_put(
    mapping_id: str, item: dict, proposed_type: str, source: str,
) -> None:
    """Post-put hooks: auto-accept, then the value-promotion observer."""
    auto_accepted = _maybe_auto_accept(mapping_id, item)
    if (not auto_accepted and proposed_type == "value_mapping"
            and source == "agent" and item.get("quarantined") != "true"):
        # Lazy import avoids a module cycle and keeps proposal ingestion's
        # import path unchanged. The observer performs one cached pending-row
        # snapshot and triggers at most once per field pair per process.
        from .value_promotion import observe_pending_value_mapping
        observe_pending_value_mapping(item)


def _lpm_write_pending(
    *, table, keys: CanonicalKeys, action: str, rows: list[dict],
    prior: list[dict], ambiguity_reason: str, endpoints: tuple,
    staged_stale: dict | None, **store_kwargs,
) -> str:
    """Apply the collision action, store the pending row, then retire any
    spent staging-buffer row. The staged delete runs ONLY AFTER ``_lpm_store``
    succeeded: a raise on the pending put keeps the staged
    evidence intact for the next observation."""
    review_reason, quarantined, newly = _lpm_apply_action(
        table, action, rows, prior, ambiguity_reason, endpoints)
    mapping_id = _lpm_store(
        table=table, keys=keys, action=action, rows=rows, prior=prior,
        newly=newly, review_reason=review_reason, quarantined=quarantined,
        **store_kwargs)
    if staged_stale is not None:
        _delete_staged_row(table, staged_stale)
    return mapping_id


# Session-flavored provenance phrasing in MACHINE-authored text (word-boundary,
# case-insensitive; the optional trailing "s" is captured to preserve plurality).
# Matches "prior session(s)", "previous session(s)", "earlier session(s)",
# "past session(s)" — never bare-substring tokens like "sessional" (the \b after
# the captured "s?" stops at word boundaries only) or hyphenated compounds like
# "session-store" (the ``(?!-)`` lookahead refuses a trailing hyphen, which \b
# alone would accept).
_SESSION_PHRASE_RE = re.compile(
    r"\b(?:prior|previous|earlier|past)\s+session(s?)\b(?!-)", re.IGNORECASE
)


def _normalize_provenance_phrasing(text: str) -> str:
    """Deterministically rewrite session-memory phrasing in MACHINE-authored
    provenance text to graph-accurate phrasing.

    The machine has NO session memory — it learns through graph rows. A reason
    like "known at <site> from prior sessions" is factually wrong, gets
    parroted verbatim into answers quoting the row, and trips the smoke
    memory-reference gate (which correctly flags fresh-session memory claims;
    2026-08-04 live diagnosis: 3 pilot-env rows carried it). Rewrites
    'prior|previous|earlier|past session(s)' → 'earlier learned mapping(s)',
    preserving plurality and all surrounding text byte-for-byte. Pure and
    deterministic (no LLM). Callers apply it ONLY to machine-sourced
    (``source != 'user'``) text — steward-typed text is UNTRUSTED but never
    silently rewritten (ADR-0054)."""
    if not text:
        return text
    return _SESSION_PHRASE_RE.sub(
        lambda m: "earlier learned mapping" + ("s" if m.group(1) else ""), text
    )




def _normalized_guidance_scope(proposed_type: str, value: str = "") -> str:
    if proposed_type != "guidance_note":
        return ""
    from .guidance_scope import normalize_scope
    return normalize_scope(value).value


def _metric_definition_proposal_fields(
    proposed_type: str, to_value: str, query_context: str,
) -> tuple[str, str]:
    """Enforce ADR-0066 C2's id + canonical-document item contract."""
    if proposed_type != "metric_definition":
        return to_value, query_context
    from .metric_definition import (
        MetricDefinitionError, canonical_json, parse_definition,
    )
    try:
        doc = parse_definition(query_context)
    except MetricDefinitionError as exc:
        raise ValueError(str(exc)) from exc
    metric_id = str(doc["id"])
    if to_value and to_value != metric_id:
        raise ValueError(
            "metric_definition to_value must equal the document id "
            f"({to_value!r} != {metric_id!r})")
    return metric_id, canonical_json(doc)


def log_proposed_mapping(
    from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str,
    concept: str, confidence: float, query_context: str,
    tool_calls_count: int = 0, proposed_type: str = "value_mapping",
    source: str = "agent", description: str = "", transform: str = "",
    time_key: dict | None = None, method: str = "", reason: str = "",
    provenance: str = "", guidance_scope: str = "") -> str | None:
    """Write one proposal; return its id or ``None`` on a documented gate.
    Pipeline: gates → suppression → classify → staging → pending; store failures raise."""
    _note_propose_outcome("")
    to_value, query_context = _metric_definition_proposal_fields(proposed_type, to_value, query_context)
    guidance_scope = _normalized_guidance_scope(proposed_type, guidance_scope)
    reason = reason if source == "user" else _normalize_provenance_phrasing(reason)
    gated = _lpm_ingestion_gates(
        proposed_type, from_system, from_field, from_value,
        to_system, to_field, to_value, query_context, source, concept)
    if gated is None:
        return None
    (proposed_type, from_system, to_system, from_field, to_field,
     query_context, ambiguity_reason) = gated
    if _lpm_observation_suppressed(
            proposed_type, from_system, from_field, from_value,
            to_system, to_field, to_value, transform, concept, source):
        return None
    table, keys, prior, action, existing_id, rows = _lpm_classify(
        proposed_type, from_system, from_field, from_value, to_system,
        to_field, to_value, concept, query_context, source, description, reason,
        guidance_scope)
    handled, early = _lpm_early_action(action, keys, existing_id, rows)
    if handled:
        return early
    buffered, query_context, staged_stale = _lpm_staging(
        table, keys, action, proposed_type, source, provenance,
        from_system, to_system, query_context)
    if buffered:
        return None
    return _lpm_write_pending(
        table=table, keys=keys, action=action, rows=rows, prior=prior,
        ambiguity_reason=ambiguity_reason, staged_stale=staged_stale,
        endpoints=(proposed_type, from_system, from_field, from_value,
                   to_system, to_field, to_value, source),
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value,
        concept=concept, confidence=confidence, query_context=query_context,
        proposed_type=proposed_type, source=source, method=method,
        tool_calls_count=tool_calls_count, description=description,
        transform=transform, time_key=time_key, reason=reason,
        provenance=provenance, guidance_scope=guidance_scope)


def find_existing_equivalent_rule(
    proposed_type: str, *, from_system: str, from_field: str,
    to_system: str, to_field: str, from_value: str = "", to_value: str = "",
    concept: str = "", transform: str = "",
) -> str | None:
    """Read-only dedup probe: the ``mapping_id`` of an EXISTING proposal that
    makes a fresh emit of these args redundant, or ``None``.

    Two lookup shapes — the SAME grains ``log_proposed_mapping`` stamps and
    dedups on (ADR-0003 / ADR-0043), ADDITIVE (no existing caller's behavior
    changes):

      * an ACTIVE (pending|accepted) row at the exact canonical target grain
        (``_canonical_keys_for`` — the lookup-before-write key), and
      * an ACCEPTED broad rule covering the observation
        (``_accepted_rule_covering_observation`` — the rule-absorption
        suppression whose ``None`` return from ``log_proposed_mapping`` is
        otherwise indistinguishable from a gate drop).

    Lets a caller whose emit returned ``None`` (the ADR-0043 promotion sweep)
    tell "an equivalent rule already exists" (already satisfied) apart from a
    genuine gate drop/hold (rows must stay visibly pending). A REJECTED row is
    deliberately NOT equivalent — a steward rejection is a human decision, not
    satisfaction (ADR-0053). NEVER writes; best-effort — any backend failure
    yields ``None`` (never claim satisfaction on uncertainty)."""
    try:
        keys = _canonical_keys_for(
            proposed_type, from_system=from_system, from_field=from_field,
            from_value=from_value, to_system=to_system, to_field=to_field,
            to_value=to_value, concept=concept)
        for r in _query_subject(_get_table(), keys.subject_key):
            if (r.get("target_key") == keys.target_key
                    and r.get("status") in ("pending", "accepted")):
                return r.get("mapping_id")
        rule = _accepted_rule_covering_observation(
            proposed_type, from_system, from_field, from_value,
            to_system, to_field, to_value, transform)
        return rule.get("mapping_id") if rule else None
    except Exception as exc:  # noqa: BLE001 - advisory probe, never raises
        logger.debug("equivalent-rule lookup failed (treating as none): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Reason-code → failure-mode auto-learning (generic; no dataset literals).
#
# OT/MES stop records (e.g. SiteWise State_Event) carry opaque numeric fault/reason
# codes with no human-readable meaning. When a query result exposes such a code
# column, propose a value_mapping (raw code -> human label) through the SAME
# proposal pipeline (``log_proposed_mapping``), so it inherits lookup-before-write
# dedup, the accept/auto-accept path, and the shared scorer's noise prevention, and
# future answers can render the label.
#
# GENERIC by contract: column classification uses generic OT/MES vocabulary tokens
# (never dataset column names); the label is DERIVED from a co-located descriptive
# column in the SAME row (never a hardcoded lookup). With no usable description, the
# code is proposed LOW-CONFIDENCE / pending with an EMPTY label (never fabricated).
# --------------------------------------------------------------------------- #
import re as _re_rc  # noqa: E402 - module-local alias kept next to its consumers

# Generic tokens marking a column as a machine-stop reason/fault CODE column: its
# normalized name contains "code" AND one of these stop concepts. Not dataset
# literals — OT/MES concept vocabulary.
_REASON_CODE_STOP_TOKENS = ("reason", "fault", "stop", "alarm", "trip", "downtime")

# Generic tokens marking a column as a clear human-readable DESCRIPTION/label.
_STRONG_DESCRIPTION_TOKENS = (
    "description", "desc", "message", "msg", "meaning", "detail", "label",
)

# The canonical concept a reason/fault code resolves to (generic concept name, not
# a dataset label): the raw code translates to a FAILURE MODE.
_FAILURE_MODE_CONCEPT = "failure-mode"

_REASON_CODE_LABELLED_CONFIDENCE = 0.9   # code + co-located label present
_REASON_CODE_PENDING_CONFIDENCE = 0.3    # opaque code, no label → pending review

_CODE_LIKE_RE = _re_rc.compile(r"^[A-Za-z0-9._\-]+$")


def _normalize_column(name: str) -> str:
    """Lowercase a column name and strip separators for token matching. Generic."""
    return _re_rc.sub(r"[^a-z0-9]", "", (name or "").lower())


def _is_reason_code_column(name: str) -> bool:
    """True when a column name looks like a machine-stop reason/fault CODE column.

    Generic: the normalized name contains ``code`` AND a stop concept token
    (reason/fault/stop/alarm/trip/downtime) — e.g. ``reason_code``, ``faultCode``,
    ``stop_code``. Carries no dataset literals.
    """
    norm = _normalize_column(name)
    return "code" in norm and any(tok in norm for tok in _REASON_CODE_STOP_TOKENS)


def _is_label_column(name: str, stop_tokens: tuple[str, ...]) -> bool:
    """True when a column can supply a human-readable label for a code column.

    Accepts a column that either carries a strong description token
    (description/message/…) OR shares the code column's stop concept (e.g.
    ``reason_code`` ↔ a ``reason`` text column). Never a code or identifier column
    (those hold no prose). Generic — avoids grabbing unrelated names (e.g. an asset
    name) as a fabricated label.
    """
    norm = _normalize_column(name)
    if not norm or "code" in norm or norm.endswith("id"):
        return False
    if any(tok in norm for tok in _STRONG_DESCRIPTION_TOKENS):
        return True
    return any(tok in norm for tok in stop_tokens)


def _is_opaque_code(value) -> bool:
    """True when a value is an opaque code lacking human-readable meaning.

    Opaque = no whitespace AND (all digits, e.g. ``131607000``, OR an alphanumeric
    code carrying at least one digit, e.g. ``E4012``/``FLT-22``). A phrase (has a
    space) or a pure-letter word (e.g. ``Overheat``) is treated as already readable.
    Pure/deterministic, generic.
    """
    s = str(value).strip()
    if not s or any(c.isspace() for c in s):
        return False
    if s.isdigit():
        return True
    return bool(_CODE_LIKE_RE.match(s)) and any(c.isdigit() for c in s)


def _is_human_label(value) -> bool:
    """True when a value reads as a human label (has letters, not an opaque code)."""
    s = str(value).strip()
    return bool(s) and any(c.isalpha() for c in s) and not _is_opaque_code(s)


def propose_reason_code_mappings(rows, system_id: str) -> list[str]:
    """Propose reason-code → failure-mode value_mappings from a query result.

    GENERIC self-learning (no dataset literals): scans ``rows`` (a list of
    column→value dicts) for a machine-stop reason/fault CODE column whose values are
    opaque codes lacking a human-readable meaning. For each DISTINCT opaque code it
    proposes a ``value_mapping`` (raw code -> label) via ``log_proposed_mapping`` —
    the SAME pipeline as every other discovery, so it inherits lookup-before-write
    dedup, the accept/auto-accept path, and the shared scorer's noise prevention.

    The label is DERIVED from a co-located descriptive column in the SAME row
    (description/message/… or a column sharing the code's stop concept), never a
    hardcoded lookup. When a row carries no usable description, the code is proposed
    LOW-CONFIDENCE / pending with an EMPTY label (never fabricated) so a human can
    supply the failure-mode meaning.

    Returns the list of proposal mapping_ids created (or reused). Errors from
    ``log_proposed_mapping`` propagate (fail-loud storage contract).
    """
    if not system_id or not isinstance(rows, list):
        return []
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not dict_rows:
        return []

    columns = list(dict_rows[0].keys())
    code_cols = [c for c in columns if _is_reason_code_column(c)]
    if not code_cols:
        return []

    proposed: list[str] = []
    seen: set[tuple[str, str]] = set()  # (code_col, raw_code) — dedup within call
    for code_col in code_cols:
        stop_tokens = tuple(
            t for t in _REASON_CODE_STOP_TOKENS if t in _normalize_column(code_col)
        )
        label_cols = [
            c for c in columns
            if c != code_col and _is_label_column(c, stop_tokens)
        ]
        for row in dict_rows:
            raw = row.get(code_col)
            if raw is None:
                continue
            code = str(raw).strip()
            if not _is_opaque_code(code):
                continue  # already readable / not a code — nothing to learn
            key = (code_col, code)
            if key in seen:
                continue
            seen.add(key)

            # Derive the label from a co-located description column (same row).
            label = ""
            label_field = code_col
            for lc in label_cols:
                cand = row.get(lc)
                if cand is not None and _is_human_label(cand):
                    label = str(cand).strip()
                    label_field = lc
                    break

            confidence = (
                _REASON_CODE_LABELLED_CONFIDENCE if label
                else _REASON_CODE_PENDING_CONFIDENCE  # pending — never fabricate
            )
            mid = log_proposed_mapping(
                from_system=system_id, from_field=code_col, from_value=code,
                to_system=system_id, to_field=label_field, to_value=label,
                concept=_FAILURE_MODE_CONCEPT, confidence=confidence,
                query_context=f"Auto-analysis: reason-code → failure-mode ({code_col})",
                proposed_type="value_mapping",
            )
            if mid:
                proposed.append(mid)
    return proposed


def get_mapping(mapping_id: str) -> "dict | None":
    """Read one current proposal row by explicit id; no scan or predicate."""
    if not str(mapping_id or "").strip():
        return None
    response = _get_table().query(
        KeyConditionExpression="mapping_id = :mid",
        ExpressionAttributeValues={":mid": str(mapping_id).strip()},
    )
    items = response.get("Items", [])
    return dict(items[0]) if items else None


def get_pending_mappings() -> list[dict]:
    """Query DynamoDB GSI for all pending proposals. (Read-only — swallows.)

    PAGINATES on ``LastEvaluatedKey`` (via ``_query_by_status`` — defensive
    cap of 20 pages, never spins): a single DynamoDB page is capped at 1 MB,
    and the ADR-0043 promotion threshold depends on snapshot completeness —
    rows past page 1 must still be counted.

    Applies the lazy ADR-0051 insight TTL (``_expire_stale_insight_pendings``)
    so stale findings drop out of review pressure at read time, then attaches
    the ADR-0063 D2 trust signals (computed on read, additive)."""
    try:
        return _attach_review_trust_signals(_expire_stale_insight_pendings(
            _query_by_status("pending", newest_first=True)))
    except Exception as e:
        logger.error(f"Failed to query pending mappings: {e}")
        return []


def get_all_mappings() -> list[dict]:
    """Scan DynamoDB for all mappings (pending + accepted + rejected). (Swallows.)

    ADR-0051 P5: staging-buffer rows (``staged#`` mapping_id, no status) are
    evidence accumulation, not lifecycle rows — filtered from the scan.

    The scan follows ``LastEvaluatedKey`` to the end: a single scan call
    returns at most one 1 MB page, which silently undercounted a month's-worth
    of rows (bell said 486, the table held 850+ — live-found 2026-08-06).
    Bounded by page count, fail-loud past the cap. Rows carry the ADR-0063 D2
    trust signals (computed on read, additive).

    ``ConsistentRead=True`` (ADR-0065, 2026-09-18): the review list and the
    header badge re-read this scan IMMEDIATELY after an accept/reject resolves;
    the default eventually-consistent scan could return the pre-write row, and
    the client then cached that snapshot (30 s staleTime, no polling), so an
    accepted/rejected proposal kept showing as pending. Base-table scans
    support strong consistency; the queue is small, so the 2× RCU is cheap."""
    try:
        table = _get_table()
        items: list[dict] = []
        kwargs: dict = {"ConsistentRead": True}
        for _ in range(50):  # 50 pages ≈ 50 MB — far past any sane queue
            response = table.scan(**kwargs)
            items.extend(
                i for i in response.get("Items", [])
                if not str(i.get("mapping_id") or "").startswith(
                    _NON_LIFECYCLE_PREFIXES))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        else:
            logger.error("get_all_mappings: scan exceeded 50 pages — truncated")
        # Sort by discovered_at descending
        items.sort(key=lambda x: x.get("discovered_at", ""), reverse=True)
        return _attach_review_trust_signals(items)
    except Exception as e:
        logger.error(f"Failed to scan all mappings: {e}")
        return []


#: The fields an edit may change on a pending proposal: the relationship it
#: names (``models.EditMappingRequest``'s fields, kept equal by a test). Never
#: ``source``, ``method`` or ``confidence`` (where the row came from, which
#: decides whether its accept caps the number: :func:`_is_steward_correction`),
#: its status, ids or timestamps.
EDITABLE_MAPPING_FIELDS = frozenset({
    "from_system", "from_field", "from_value", "to_system", "to_field", "to_value",
    "concept", "description",
})


def _refuse_uneditable_fields(edit):
    """Guard ``edit`` (:func:`edit_mapping`) so an edit with any key outside
    :data:`EDITABLE_MAPPING_FIELDS` is refused whole, before anything is read
    or written, for every caller (the route's request model refuses the same
    keys first)."""
    @functools.wraps(edit)
    def guarded(mapping_id: str, updates: dict) -> dict:
        refused = sorted(set(updates or {}) - EDITABLE_MAPPING_FIELDS)
        if refused:
            return {"error": f"fields cannot be edited: {', '.join(refused)}"}
        return edit(mapping_id, updates)
    return guarded


@_refuse_uneditable_fields
def edit_mapping(mapping_id: str, updates: dict) -> dict:
    """Edit an existing PENDING mapping with new field values.
    
    Validates the provided fields against the onboarded schema before updating.
    Only PENDING mappings can be edited (already accepted/rejected are immutable).
    
    Returns the updated mapping dict on success, or {"error": str} on failure.
    """
    from .schema_check import column_exists, is_onboarded_system
    
    table = _get_table()
    
    # Find the mapping by ID
    try:
        response = table.query(
            KeyConditionExpression="mapping_id = :mid",
            ExpressionAttributeValues={":mid": mapping_id},
        )
        items = response.get("Items", [])
        if not items:
            return {"error": f"mapping {mapping_id} not found"}
        item = items[0]
    except Exception as e:
        logger.error(f"Failed to query mapping {mapping_id}: {e}")
        return public_error(e, "learning.edit_mapping")
    
    # Only pending mappings can be edited
    status = item.get("status", "")
    if status != "pending":
        return {"error": f"only pending mappings can be edited (status: {status})"}
    
    # Validate when we have both system and field info - skip if insufficient info
    errors: list[str] = []
    
    # from_field validation: only validate if we have BOTH system and field info
    from_fld = updates.get("from_field")
    from_sys = updates.get("from_system")
    existing_from_sys = item.get("from_system", "")
    
    if from_fld:
        sys_to_check = from_sys if from_sys else existing_from_sys
        if sys_to_check:
            if is_onboarded_system(sys_to_check):
                if column_exists(sys_to_check, from_fld) is False:
                    errors.append(f"from_field '{from_fld}' does not exist in {sys_to_check} schema")
            else:
                errors.append(f"from_system '{sys_to_check}' is not onboarded")
    elif from_sys:
        if not is_onboarded_system(from_sys):
            errors.append(f"from_system '{from_sys}' is not onboarded")
    
    # to_field validation: similar logic
    to_fld = updates.get("to_field")
    to_sys = updates.get("to_system")
    existing_to_sys = item.get("to_system", "")
    
    if to_fld:
        sys_to_check = to_sys if to_sys else existing_to_sys
        if sys_to_check:
            if is_onboarded_system(sys_to_check):
                if column_exists(sys_to_check, to_fld) is False:
                    errors.append(f"to_field '{to_fld}' does not exist in {sys_to_check} schema")
            else:
                errors.append(f"to_system '{sys_to_check}' is not onboarded")
    elif to_sys:
        if not is_onboarded_system(to_sys):
            errors.append(f"to_system '{to_sys}' is not onboarded")
    
    if errors:
        return {"error": "; ".join(errors)}
    
    # Apply the updates
    try:
        discovered_at = item.get("discovered_at", "")
        # Build update expression
        expr_parts = []
        values = {}
        names = {}
        for i, (key, value) in enumerate(updates.items()):
            if value is None:
                continue
            expr_parts.append(f"#k{i} = :v{i}")
            values[f":v{i}"] = value
            names[f"#k{i}"] = key
        
        if expr_parts:
            table.update_item(
                Key={"mapping_id": mapping_id, "discovered_at": discovered_at},
                UpdateExpression="SET " + ", ".join(expr_parts),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        
        # Re-fetch the updated item
        response = table.query(
            KeyConditionExpression="mapping_id = :mid",
            ExpressionAttributeValues={":mid": mapping_id},
        )
        return response["Items"][0] if response.get("Items") else item
        
    except Exception as e:
        logger.error(f"Failed to update mapping {mapping_id}: {e}")
        return public_error(e, "learning.edit_mapping")


def _repoint_require_field(fid: str, to_system: str, table: str, column: str) -> None:
    """FAIL-LOUD PRE-CHECK (live incident, 2026-07-14): the corrected Field must
    EXIST before any delete, or the concept silently loses ALL its mappings.
    An unreachable graph (System read empty too) raises RuntimeError, not the
    misleading schema ValueError — the read seam swallows backend errors."""
    exists_rows = _neptune_query(
        "MATCH (f:Field {id: $fid}) RETURN count(f) AS n", {"fid": fid},
    )
    _n = (exists_rows[0].get("n") if exists_rows else 0) or 0
    if int(_n) >= 1:
        return
    sys_rows = _neptune_query(
        "MATCH (s:System {id: $sys}) RETURN count(s) AS n", {"sys": to_system},
    )
    _sn = (sys_rows[0].get("n") if sys_rows else 0) or 0
    if int(_sn) < 1:
        raise RuntimeError("graph unavailable — correction not applied")
    raise ValueError(
        f"corrected column '{table}.{column}' is not in the discovered schema "
        f"for system '{to_system}' — correction not applied"
    )


def _repoint_next_version(to_system: str, concept: str) -> int:
    """Monotonic MAPS_TO version bump for ``(system, concept)``."""
    rows = _neptune_query(
        "MATCH (s:System {id: $sys})-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->"
        "(:Field)-[r:MAPS_TO]->(c:Concept {id: $concept}) RETURN max(r.version) AS v",
        {"sys": to_system, "concept": concept},
    )
    v = rows[0].get("v") if rows else None
    return int(v or 1) + 1


def _approved_by(item: dict) -> str:
    """Who approved an applied mapping (the accepting steward, else the
    proposal's source), stored on the edge as ``r.approved_by``."""
    return str(item.get("accepted_by") or item.get("source") or "user")[:128]


def _bump_mapping_generation(system_id: str) -> None:
    """Move ``system_id``'s mapping generation, so its system agent re-reads
    its concept mappings on the next question (``live_concept_mappings``).
    Fail-loud like the edge writes it follows."""
    from .live_concept_mappings import BUMP_GENERATION_CYPHER
    _neptune_write_loud(BUMP_GENERATION_CYPHER, {"sid": system_id})


def _repoint_replace_edges(fid: str, to_system: str, concept: str,
                           conf: float, new_ver: int, approved_by: str = "user") -> None:
    """DELETE every prior MAPS_TO for the (system, concept) AND every edge
    leaving THIS column (R5 win-rule, S5.2 W2: a later user correction must
    beat a prior mint pointing the same column elsewhere), then MERGE the
    corrected edge, marked approved (``r.confirmed``, ``r.approved_by``), and
    move the system's mapping generation. Fail-loud writes. The two MERGEs
    are safe to run twice with the same parameters (``new_ver`` is read before
    the first send), so they may be sent again after an ambiguous failure
    (``db.resubmit_safe_write``; texts pinned in a test); the DELETEs and the
    generation counter are not."""
    from .db import resubmit_safe_write
    _neptune_write_loud(
        "MATCH (s:System {id: $sys})-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->"
        "(f:Field)-[r:MAPS_TO]->(c:Concept {id: $concept}) DELETE r",
        {"sys": to_system, "concept": concept},
    )
    _neptune_write_loud(
        "MATCH (f:Field {id: $fid})-[r:MAPS_TO]->() DELETE r", {"fid": fid},
    )
    with resubmit_safe_write():
        _neptune_write_loud(
            "MERGE (c:Concept {id: $concept}) ON CREATE SET c.name = $concept",
            {"concept": concept},
        )
    with resubmit_safe_write():
        _neptune_write_loud(
            "MATCH (f:Field {id: $fid}), (c:Concept {id: $concept}) "
            "MERGE (f)-[r:MAPS_TO]->(c) SET r.confidence = $conf, r.role = 'identifier', "
            "r.method = 'user_corrected', r.version = $ver, r.confirmed = true, "
            "r.approved_by = $approved_by",
            {"fid": fid, "concept": concept, "conf": conf, "ver": new_ver,
             "approved_by": approved_by},
        )
    _bump_mapping_generation(to_system)


@invalidates_metric_turn_results
def _repoint_maps_to(item: dict) -> str:
    """Repoint the authoritative ``MAPS_TO`` edge for ``(system, concept)`` to the
    user-corrected Field (S3.2, FLAG a). Does NOT write a VM/JP/IR node.

    The resolver reads the system-scoped MAPS_TO ``LIMIT 1`` with no role
    filter, so the chosen column must become the SOLE authoritative edge:
    verify the Field exists (before any delete), read the current max version,
    delete prior edges, MERGE the corrected edge with a bumped version.
    Coordinator only — the three steps are the helpers above."""
    to_system = item["to_system"]
    concept = item["concept"]
    table, column = item["to_field"].rsplit(".", 1)  # "table.column"
    fid = f"{to_system}.{table}.{column}"  # Field id (onboard.py:1573)
    conf = float(item.get("confidence", 1.0))
    _repoint_require_field(fid, to_system, table, column)
    new_ver = _repoint_next_version(to_system, concept)
    _repoint_replace_edges(fid, to_system, concept, conf, new_ver, _approved_by(item))
    return f"mapsto-{to_system}-{concept}"


def accepted_user_concept_mappings(system_id: str) -> dict[str, str]:
    """ACCEPTED, USER-sourced ``concept_mapping`` corrections for a system.

    Returns ``{concept: "table.column"}`` — the columns the USER pointed each
    concept at. Consumed by the provision-time MAPS_TO writer (onboard.py) so a
    reset + re-onboard does NOT let the re-run LLM mapper silently clobber an
    accepted user correction (live incident, 2026-07-14: an accepted
    machine-speed correction was reverted by a re-onboard).

    DRY: built ON the existing table-access seam :func:`get_accepted_for_system`
    (no duplicated DynamoDB query logic), filtered to ``proposed_type ==
    'concept_mapping' AND source == 'user' AND to_system == system_id``. Rows are
    considered newest-first (``discovered_at`` desc), so the MOST RECENT accepted
    correction per concept wins. Read-only — swallows (returns ``{}`` on any
    backend failure), matching the get_* convention: a proposal-store outage must
    never block provisioning.
    """
    if not system_id:
        return {}
    try:
        items = get_accepted_for_system(system_id)
        items.sort(key=lambda x: x.get("discovered_at", ""), reverse=True)
        out: dict[str, str] = {}
        for item in items:
            if (item.get("proposed_type") != "concept_mapping"
                    or item.get("source") != "user"
                    or item.get("to_system") != system_id):
                continue
            concept = str(item.get("concept") or "").strip()
            to_field = str(item.get("to_field") or "").strip()
            # A repointable correction always carries "table.column" (the
            # _repoint_maps_to contract); skip malformed rows.
            if concept and "." in to_field and concept not in out:
                out[concept] = to_field
        return out
    except Exception as e:  # noqa: BLE001 - read-only; never block provisioning
        logger.error(f"Failed to load accepted user concept mappings for {system_id}: {e}")
        return {}


#: The proposal type of a steward's retirement of ONE concept -> column link
#: (``POST /api/metrics/retire-mapping``).
CONCEPT_MAPPING_RETIRE = "concept_mapping_retire"

_RETIRE_EDGE_COUNT_CYPHER = (
    "MATCH (f:Field {id: $fid})-[r:MAPS_TO]->(c:Concept {id: $concept}) RETURN count(r) AS n")
_RETIRE_EDGE_DELETE_CYPHER = (
    "MATCH (f:Field {id: $fid})-[r:MAPS_TO]->(c:Concept {id: $concept}) DELETE r")


def _retire_edge_count(fid: str, concept: str) -> int:
    rows = _neptune_query(_RETIRE_EDGE_COUNT_CYPHER, {"fid": fid, "concept": concept})
    return int((rows[0].get("n") if rows else 0) or 0)


def _neptune_query_checked(cypher: str, parameters: dict | None = None) -> list:
    """Checked graph read seam: RAISES on a backend failure (``_neptune_query``
    goes through a read that answers ``[]`` on one, which reads as "nothing
    there")."""
    from .db import neptune_query_checked
    return neptune_query_checked(cypher, parameters)


def _retire_edge_gone(fid: str, concept: str) -> bool:
    """Exact absence readback of one retired edge through the checked read:
    ``True`` only when the read answered and counted zero."""
    rows = _neptune_query_checked(_RETIRE_EDGE_COUNT_CYPHER, {"fid": fid, "concept": concept})
    row = (rows or [None])[0]
    return isinstance(row, dict) and int(row.get("n") or 0) == 0 and "n" in row


@invalidates_metric_turn_results
def _retire_maps_to(item: dict) -> str:
    """Delete the ONE ``Field-[:MAPS_TO]->Concept`` edge an accepted
    retirement names; every other link of the concept and of the column stays.

    The column must exist in the system's discovered schema (the repoint's
    fail-loud pre-check, before any delete): a typo is refused, never
    recorded as removing nothing. An edge already gone is not an error (the
    retirement still stands and is replayed after every provision). The
    delete is fail-loud and read back. The Field id is the system id plus the
    whole ``table.column`` (a flattened nested column keeps its dots)."""
    to_system = item["to_system"]
    concept = item["concept"]
    to_field = item["to_field"]
    table, column = to_field.rsplit(".", 1)
    fid = f"{to_system}.{to_field}"
    _repoint_require_field(fid, to_system, table, column)
    before = _retire_edge_count(fid, concept)
    _neptune_write_loud(_RETIRE_EDGE_DELETE_CYPHER, {"fid": fid, "concept": concept})
    try:
        gone = _retire_edge_gone(fid, concept)
    except Exception as exc:
        raise RuntimeError(f"retired link {concept} -> {to_system}.{to_field} could not be "
                           f"read back ({type(exc).__name__})") from exc
    if not gone:
        raise RuntimeError(
            f"retired link {concept} -> {to_system}.{to_field} is still in the graph")
    _bump_mapping_generation(to_system)
    logger.info("Concept link retired: %s -> %s.%s (%d edge(s) removed)",
                concept, to_system, to_field, before)
    return f"retired-{to_system}-{concept}"


class RetirementLedgerUnavailable(RuntimeError):
    """The proposal store could not say which concept links were retired."""


def _retirement_rows() -> list[dict]:
    """Every accepted proposal row (the paginated status-index read); the
    retirement lookup's one store seam. RAISES on a store failure."""
    return _query_by_status("accepted")


def accepted_retired_concept_links(system_id: str) -> list[dict]:
    """The concept -> column links a steward retired for ``system_id`` and
    that still stand: ``[{"concept", "field"}]``.

    A retirement is void once a LATER accepted user correction points the
    same concept at the same column (the steward put the link back). Reads
    every accepted row (the paginated status-index read). A store failure
    RAISES :class:`RetirementLedgerUnavailable`: "unreadable" must never read
    as "none retired", or a re-provision writes a retired link back."""
    if not system_id:
        return []
    try:
        items = [item for item in _retirement_rows()
                 if item.get("to_system") == system_id]
    except Exception as e:
        logger.error("Failed to load retired concept links for %s (%s)", system_id,
                     type(e).__name__)
        raise RetirementLedgerUnavailable(
            f"retired concept links for {system_id} could not be read") from e
    restored: dict = {}
    for item in items:
        if item.get("proposed_type") == "concept_mapping" and item.get("source") == "user":
            key = (str(item.get("concept") or "").strip(), str(item.get("to_field") or "").strip())
            restored[key] = max(restored.get(key, ""), str(item.get("discovered_at") or ""))
    out: list[dict] = []
    seen: set = set()
    for item in sorted(items, key=lambda x: str(x.get("discovered_at") or "")):
        if item.get("proposed_type") != CONCEPT_MAPPING_RETIRE:
            continue
        key = (str(item.get("concept") or "").strip(), str(item.get("to_field") or "").strip())
        if not key[0] or "." not in key[1] or key in seen:
            continue
        if restored.get(key, "") > str(item.get("discovered_at") or ""):
            continue
        seen.add(key)
        out.append({"concept": key[0], "field": key[1]})
    return out



@invalidates_metric_turn_results
def _apply_field_concept_mint(item: dict, version: int) -> str:
    """S5.2 W2: mint a field concept and repoint the column's authoritative
    MAPS_TO edge. Column-keyed (R5): a column has exactly one authoritative
    field-concept meaning. The minted Concept MUST carry non-null
    source='minted' so it survives onboard.py's per-system orphan cleanup."""
    cid = item.get("concept", "")
    # Human-readable name rides in to_value (not part of the mint key).
    name = item.get("to_value") or cid
    to_system = item["to_system"]
    table, column = item["to_field"].rsplit(".", 1)  # "table.column"
    fid = f"{to_system}.{table}.{column}"  # Field id (onboard.py:1640)
    _neptune_write_loud(
        "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $name, "
        "c.source = 'minted', c.version = $ver",
        {"cid": cid, "name": name, "ver": version},
    )
    # Cosmetic description (ADR 0021): persist ONLY when authored, as a
    # follow-on SET so a re-mint refreshes it. GUARD: a mint colliding with a
    # CURATED CORE concept id must NEVER overwrite the curated definition.
    from .catalog import CORE_CONCEPT_DEFINITIONS
    desc = item.get("to_description", "")
    if desc and cid not in CORE_CONCEPT_DEFINITIONS:
        _neptune_write_loud(
            "MERGE (c:Concept {id: $cid}) SET c.description = $desc",
            {"cid": cid, "desc": desc},
        )
    # COLUMN-KEYED EDGE CLEANUP (R5): drop EVERY prior MAPS_TO edge leaving
    # this column before MERGEing the authoritative mint edge, so a stale mint
    # edge can never coexist with a later correction.
    _neptune_write_loud(
        "MATCH (f:Field {id: $fid})-[r:MAPS_TO]->() DELETE r",
        {"fid": fid},
    )
    _neptune_write_loud(
        "MATCH (f:Field {id: $fid}), (c:Concept {id: $cid}) "
        "MERGE (f)-[r:MAPS_TO]->(c) SET r.role = 'field_concept', "
        "r.confidence = $conf, r.method = 'minted', r.version = $ver, "
        "r.confirmed = true, r.approved_by = $approved_by",
        {"fid": fid, "cid": cid,
         "conf": float(item.get("confidence", 0.8)), "ver": version,
         "approved_by": _approved_by(item)},
    )
    # Applied only through a steward's accept (field_concept_mint is never
    # auto-accepted) or a user's own mint, so the edge is approved.
    _bump_mapping_generation(to_system)
    return f"mint-{to_system}-{table}-{column}"


def _apply_correlation(item: dict, subject_id: str, version: int) -> str:
    """MERGE the Correlation node for an accepted graduated finding (ADR-0051 b).

    Reuses the EXISTING Correlation metamodel shape (the ``resolve.py`` /
    ``graph_analytics.py`` reader contract: ``pattern`` / ``confidence`` /
    ``concept_a`` / ``concept_b``) plus the established FROM_SYSTEM/TO_SYSTEM
    edge types — no new edge taxonomy (ADR-0050 D1). ``concept_a`` carries the
    finding topic; ``concept_b`` stays empty (a graduated narrative names one
    topic). Subject-keyed MERGE: a re-accept upserts the same live node.
    Fail-loud and atomic with the removal marker
    (:func:`_write_between_served_systems`)."""
    _require_graph_systems(item["from_system"], item["to_system"])
    _write_between_served_systems(
        "Correlation", "c",
        "c.pattern = $pattern, "
        "c.confidence = $conf, c.concept_a = $concept, c.concept_b = '', "
        "c.from_system = $fsys, c.to_system = $tsys, c.version = $ver, "
        "c.discovered = $disc, c.provenance = $prov, c.proposal_id = $pid",
        {
            "sid": subject_id, "pattern": str(item.get("query_context", ""))[:300],
            "conf": float(item.get("confidence", 0.5)),
            "concept": item.get("concept", ""), "fsys": item["from_system"],
            "tsys": item["to_system"], "ver": version,
            "disc": datetime.now(timezone.utc).isoformat(),
            "prov": item.get("provenance", "") or item.get("source", ""),
            "pid": item.get("mapping_id", ""),
        },
    )
    return subject_id


def _apply_join_path(item: dict, subject_id: str, version: int, concept: str) -> str:
    """MERGE the JoinPath node + FROM/TO_SYSTEM edges (real transform,
    optional time_key). Fail-loud: both endpoint System nodes must exist and
    must not be being removed, checked first (``_require_graph_systems``) and
    again inside the one write statement (:func:`_write_between_served_systems`),
    so a removal that starts after the pre-check writes nothing."""
    _require_graph_systems(item["from_system"], item["to_system"])
    from_parts = item.get("from_field", "").rsplit(".", 1)
    to_parts = item.get("to_field", "").rsplit(".", 1)
    params = {
        "sid": subject_id, "fsys": item["from_system"],
        "ftbl": from_parts[0] if len(from_parts) == 2 else "unknown",
        "ffld": from_parts[-1], "tsys": item["to_system"],
        "ttbl": to_parts[0] if len(to_parts) == 2 else "unknown",
        "tfld": to_parts[-1], "concept": concept, "ver": version,
        # Real, discovered transform (default identity for legacy items —
        # both identity and the historical 'direct' are pass-throughs).
        "xform": item.get("transform") or "identity",
    }
    set_clause = (
        "jp.from_system = $fsys, jp.from_table = $ftbl, jp.from_field = $ffld, "
        "jp.to_system = $tsys, jp.to_table = $ttbl, jp.to_field = $tfld, "
        "jp.transform = $xform, jp.method = 'user_confirmed', jp.concept = $concept, "
        "jp.version = $ver"
    )
    # Optional time_key for time-window alignment (additive — only when present).
    time_key = item.get("time_key") or {}
    if isinstance(time_key, dict) and time_key.get("from_time_field") and time_key.get("to_time_field"):
        set_clause += (", jp.time_from_field = $tfrom, jp.time_to_field = $tto, "
                       "jp.time_semantics = $tsem")
        params["tfrom"] = time_key.get("from_time_field")
        params["tto"] = time_key.get("to_time_field")
        params["tsem"] = time_key.get("semantics") or "window"
    _write_between_served_systems("JoinPath", "jp", set_clause, params)
    return subject_id


#: The pattern an accepted identity-rule proposal is stored with, both sides:
#: the bare direct-match convention (the values join as they are).
_ACCEPTED_RULE_PATTERN = "*"


def _is_steward_correction(item: dict) -> bool:
    """Whether ``item`` is a row :func:`record_user_correction` wrote: a
    steward's own assertion, whose confidence is the steward's number. Both
    marks are needed: a proposal's filer chooses its row's ``method``, but only
    the user doors write ``source='user'``, and only the correction door
    writes ``method='user_corrected'``. Requeue keeps both."""
    return item.get("source") == "user" and item.get("method") == "user_corrected"


def _accepted_rule_confidence(item: dict, proposal: bool) -> float:
    """The working ``confidence`` an accepted rule's MERGE writes. Accepting
    a filed proposal (``proposal``) confirms the relationship, not the
    number: the proposal's confidence is its proposer's (a model's, which
    does not discriminate: ADR-0043), so the rule is written at min(it,
    ``identifier_translation.MODEL_CONFIDENCE_CEILING``). The proposal's own
    number stays on its ledger row (the rule's ``proposal_id`` points there)
    and is never copied onto the graph node, where the chat's free graph
    queries could read it as the rule's confidence. A steward's own
    correction (``record_user_correction``) keeps its number, also when it is
    requeued and accepted again (:func:`_is_steward_correction`)."""
    from .identifier_translation import MODEL_CONFIDENCE_CEILING
    stated = float(item.get("confidence", 0.8))
    if proposal and not _is_steward_correction(item):
        return min(stated, MODEL_CONFIDENCE_CEILING)
    return stated


def _apply_identity_rule(item: dict, subject_id: str, version: int, concept: str,
                         *, proposal: bool = False) -> str:
    """MERGE the IdentityRule node (+ LINKS_CONCEPT edge) with full provenance
    (maintain-side audit: pipeline, query context, who accepted it and when,
    and the proposal back-pointer; confidence from
    :func:`_accepted_rule_confidence`). Orchestrator/resolve projections are
    explicit field lists, so these never reach model prompts. The pattern pair
    is held to ``resolve.identity_pattern_problem``."""
    from .identifier_translation import PATTERN_GRAMMAR_LITERAL
    from .resolve import identity_pattern_problem
    problem = identity_pattern_problem(_ACCEPTED_RULE_PATTERN, _ACCEPTED_RULE_PATTERN)
    if problem:  # pragma: no cover - a constant pair; the check keeps this writer on it
        raise ValueError(f"accepted identity rule pattern is not valid: {problem}")
    _neptune_write_loud(
        "MERGE (ir:IdentityRule {id: $sid}) SET ir.concept = $concept, "
        "ir.source_system = $fsys, ir.source_field = $ffield, ir.source_pattern = $pat, "
        "ir.target_system = $tsys, ir.target_field = $tfield, ir.target_pattern = $pat, "
        # status='active' restores routing when a requeued (soft-deactivated,
        # see _undo_graph_artifact) rule is re-accepted onto the SAME
        # subject-keyed node — matches _set_rule_lifecycle's reactivate value.
        "ir.status = 'active', ir.pattern_grammar = $grammar, "
        "ir.confidence = $conf, ir.method = 'user_confirmed', ir.version = $ver, "
        "ir.provenance = $prov, ir.origin_context = $octx, "
        "ir.approved_by = $actor, ir.created_at = $created_at, ir.proposal_id = $pid",
        {
            "sid": subject_id, "concept": concept, "fsys": item["from_system"],
            "pat": _ACCEPTED_RULE_PATTERN, "grammar": PATTERN_GRAMMAR_LITERAL,
            "ffield": item.get("from_field", "").rsplit(".", 1)[-1], "tsys": item["to_system"],
            "tfield": item.get("to_field", "").rsplit(".", 1)[-1],
            "conf": _accepted_rule_confidence(item, proposal), "ver": version,
            "prov": item.get("provenance", "") or item.get("source", ""),
            "octx": str(item.get("query_context", ""))[:300],
            "actor": item.get("accepted_by", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pid": item.get("mapping_id", ""),
        },
    )
    if concept:
        _neptune_write_loud(
            "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid "
            "WITH c MATCH (ir:IdentityRule {id: $sid}) MERGE (ir)-[:LINKS_CONCEPT]->(c)",
            {"cid": concept, "sid": subject_id},
        )
    # A re-accept after a requeue restores the JoinPaths the requeue switched
    # off with this rule (joinpath_rule_link); none carry the mark otherwise.
    _jp_rule.cascade_rule_lifecycle(
        _neptune_write_loud, subject_id, item.get("accepted_by", ""),
        datetime.now(timezone.utc).isoformat(), deactivate=False)
    return subject_id


def _apply_field_annotation(item: dict, subject_id: str, version: int) -> str:
    """SET the Field's disambiguation annotation, with the fail-loud pre-check
    (live incident, 2026-07-14): when from_system is not a graph System id (a
    plant name) or the table/field was hallucinated, the SET matches ZERO rows
    — a silent no-op that flipped the ledger to accepted while no rule was
    learned. Probe the exact MATCH first and raise an actionable error
    (indeterminate probe → proceed; a real outage fails loud at the write).

    The Field is matched by its id ``<system>.<from_field>``: a flattened
    nested column keeps its dots (``table.struct.leaf``), and the id does not
    depend on where the table/column split falls."""
    system_id = item["from_system"]
    from_field = item.get("from_field", "")
    table_name, _sep, field_name = from_field.partition(".")
    annotation_text = item.get("query_context", "").replace("Auto-analysis: ", "")
    if not (system_id and table_name and field_name and annotation_text):
        # Can't identify the field — mark applied against the subject id.
        return f"annotation-{subject_id}"
    fid = f"{system_id}.{from_field}"
    n = _graph_rows_matched(
        "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(f:Field {id: $fid}) "
        "RETURN count(f) AS n",
        {"sid": system_id, "fid": fid},
    )
    if n == 0:
        raise ValueError(
            f"field annotation target {fid} "
            f"does not exist in the knowledge graph — the rule was NOT "
            f"applied; edit the proposal to an onboarded system and a "
            f"real table.column before accepting"
        )
    _neptune_write_loud(
        "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(f:Field {id: $fid}) "
        "SET f.disambiguation = $annotation, f.disambiguation_version = $ver",
        {"sid": system_id, "fid": fid,
         "annotation": annotation_text, "ver": version},
    )
    logger.info(f"Field annotation applied: {fid} (v{version})")
    return f"field-{system_id}-{table_name}-{field_name}"


def _apply_value_mapping(item: dict, subject_id: str, version: int, concept: str,
                         proposed_type: str) -> str:
    """Write the ValueMapping node + edges — row-level translation. A CONFIRMED
    crosswalk persists here too (a human-confirmed value↔value bridge for
    disjoint-vocabulary entities IS a ValueMapping), tagged with its crosswalk
    provenance so its origin stays auditable. Fail-loud: both endpoint System
    nodes are checked first (``_require_graph_systems``), then the node, both
    edges and the Concept link are written in ONE statement that also matches
    both System nodes (``onboard._upsert_value_mapping``): it lands whole or
    not at all, and the caller marks the ledger accepted only after it
    returned this id. A statement that matched no System pair (the pre-check
    was indeterminate, or a System went away) raises the same actionable
    ``ValueError``; a refused one raises ``RuntimeError``. The node carries
    the one-row accept's stamp (``accepted_by`` and ``confirmation_mode``)
    only when the item does (:func:`accept_mapping` and
    :func:`record_user_correction` with ``confirmation_mode``); otherwise
    both are written empty."""
    from .identifier_translation import SINGLE_ROW_CONFIRMATION
    from .onboard import _upsert_value_mapping
    _require_graph_systems(item["from_system"], item["to_system"])
    vm_method = "crosswalk_confirmed" if proposed_type == "crosswalk" else "user_confirmed"
    # The one-row accept's stamp (``identifier_translation.SINGLE_ROW_CONFIRMATION``
    # plus the accepting actor), written on EVERY accept so a later bulk,
    # batch or automatic re-accept of the same subject node clears it.
    single = item.get("confirmation_mode") == SINGLE_ROW_CONFIRMATION
    stamp = (("accepted_by", "acc_by", str(item.get("accepted_by") or "") if single else ""),
             ("confirmation_mode", "conf_mode", SINGLE_ROW_CONFIRMATION if single else ""))
    status = _upsert_value_mapping(
        subject_id, {**item, "concept": concept, "method": vm_method,
                     "confidence": float(item.get("confidence", 0.8))},
        extra=(("version", "ver", version), *stamp), concept_link=bool(concept),
        write=_neptune_write_returning)
    if status == "no_system":
        raise ValueError(
            f"system '{item['from_system']}' or '{item['to_system']}' does not exist in the "
            f"knowledge graph — the rule was NOT applied; edit the proposal to name an "
            f"onboarded system id before accepting")
    if status != "written":
        raise RuntimeError(f"Neptune write failed: ValueMapping {subject_id} was not written")
    return subject_id


def _vocab_op_and_synonyms(item: dict) -> "tuple[str, list[str]]":
    """Parse the op (add|edit|retire) + optional synonyms a ``concept_vocab``
    item carries in its ``query_context`` JSON (the frozen Wave-2 contract).
    Tolerant by construction: a non-JSON/legacy context yields ('add', [])."""
    try:
        ctx = json.loads(str(item.get("query_context", "") or ""))
    except (ValueError, TypeError):
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}
    op = (str(ctx.get("op", "") or "").strip().lower()) or "add"
    raw_syn = ctx.get("synonyms")
    synonyms = ([str(s).strip() for s in raw_syn if str(s).strip()]
                if isinstance(raw_syn, list) else [])
    return op, synonyms


def _retire_concept(cid: str, version: int, actor: str) -> str:
    """SOFT-retire a concept, guarded by the pack-uninstall mapped-concept
    rule: a concept still carrying inbound ``MAPS_TO`` edges raises an
    actionable ``ValueError`` (the accept route surfaces it as a 400 and the
    ledger stays pending); an indeterminate probe proceeds per
    ``_graph_rows_matched``'s contract. Fail-loud write."""
    n = _graph_rows_matched(
        "MATCH (c:Concept {id: $cid})<-[m:MAPS_TO]-() RETURN count(m) AS n",
        {"cid": cid},
    )
    if n is not None and n > 0:
        raise ValueError(
            f"concept '{cid}' is still mapped to {n} field(s) — repoint or "
            f"remove those field mappings before retiring it"
        )
    _neptune_write_loud(
        "MATCH (c:Concept {id: $cid}) SET c.retired = true, "
        "c.updated_at = $ts, c.approved_by = $actor, c.version = $ver",
        {"cid": cid, "ts": datetime.now(timezone.utc).isoformat(),
         "actor": actor, "ver": version},
    )
    return f"concept-{cid}"


def _apply_concept_vocab(item: dict, subject_id: str, version: int) -> str:
    """Apply an accepted ``concept_vocab`` proposal (ADR-0050 vocabulary write).

    op add|edit → MERGE the Concept node and SET only the AUTHORED fields
    (name/description/synonyms — an op with no name preserves the existing
    ``c.name``), ``source = coalesce(c.source, 'custom')`` (an existing
    core/pack/minted provenance is never clobbered), ``updated_at``, and the
    stamped ``approved_by`` provenance (same accepted_by threading as the other
    appliers). op retire → :func:`_retire_concept` (mapped-concept guard).
    Fail-loud writes via ``_neptune_write_loud``."""
    cid = str(item.get("concept", "") or "").strip()
    if not cid:
        raise ValueError(
            "concept_vocab proposal has no concept id — edit the proposal to "
            "name the concept before accepting"
        )
    op, synonyms = _vocab_op_and_synonyms(item)
    actor = item.get("accepted_by", "")
    if op == "retire":
        return _retire_concept(cid, version, actor)
    # op add | edit — one MERGE upsert; optional attrs SET only when authored
    # (a name-only edit must not blank an existing description/synonyms, and a
    # name-less edit must not clobber an existing name — coalesce keeps it,
    # while a fresh name-less add still gets the concept id as its name).
    name = str(item.get("to_value", "") or "").strip()
    desc = str(item.get("to_description", "") or item.get("description", "") or "")
    set_clause = (
        "c.source = coalesce(c.source, 'custom'), "
        "c.updated_at = $ts, c.approved_by = $actor, c.version = $ver"
    )
    params = {"cid": cid,
              "ts": datetime.now(timezone.utc).isoformat(), "actor": actor,
              "ver": version}
    if name:
        set_clause = "c.name = $name, " + set_clause
        params["name"] = name
    else:
        set_clause = "c.name = coalesce(c.name, $cid), " + set_clause
    if desc:
        set_clause += ", c.description = $desc"
        params["desc"] = desc
    if synonyms:
        # One JSON string: Neptune openCypher takes no list property values.
        from .vocab_import_core import encode_synonyms
        set_clause += ", c.synonyms = $syn"
        params["syn"] = encode_synonyms(synonyms)
    _neptune_write_loud(f"MERGE (c:Concept {{id: $cid}}) SET {set_clause}", params)
    return f"concept-{cid}"


def _read_query_guidance_strict(system_id: str) -> str | None:
    """Current graph text, or None when existence/readback cannot be proven."""
    rows = _neptune_query(
        "MATCH (s:System {id: $sid}) RETURN s.query_guidance AS qg",
        {"sid": system_id},
    )
    if not rows:
        return None
    return str(rows[0].get("qg") or "")


def _guidance_graph_compare_and_set(system_id: str, expected: str,
                                    desired: str) -> bool:
    """Race-safe expected-current graph write; exact readback is separate."""
    result = _neptune_write_loud(
        "MATCH (s:System {id: $sid}) "
        "WHERE coalesce(s.query_guidance, '') = $expected "
        "SET s.query_guidance = $desired",
        {"sid": system_id, "expected": expected, "desired": desired},
    )
    return result is not False


def _guidance_proposal_status(table, item: dict) -> str:
    """The row's status, read strongly consistent: the saga reads it right
    after writing it, and an eventually consistent query could still return
    the row from before that write."""
    response = table.query(
        KeyConditionExpression="mapping_id = :mid",
        ExpressionAttributeValues={":mid": item["mapping_id"]},
        ConsistentRead=True)
    for row in response.get("Items", []):
        if row.get("discovered_at") == item["discovered_at"]:
            return str(row.get("status") or "")
    return ""


def _guidance_terminal_accept(table, item: dict, actor: str,
                              graph_text: str, delivered) -> bool:
    from .guidance_acceptance import opaque_digest
    extra = {
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "neptune_node_id": f"guidance-{_subject_id(item['subject_key'])}",
        "guidance_graph_digest": opaque_digest(graph_text),
        "guidance_delivery_digest": delivered.digest,
        "guidance_delivery_version": str(delivered.version),
        **({"accepted_by": actor} if actor else {}),
    }
    _update_status(table, item["mapping_id"], item["discovered_at"],
                   "accepted", extra, expect_status="pending")
    return True


def _guidance_terminal_restore(table, item: dict) -> bool:
    """Undo this accept's status write: a conditional ``accepted`` to
    ``pending`` update sent with no read first, so a failed read cannot skip
    the undo. A conditional miss means the row was not ``accepted`` and there
    is nothing to undo; any other error reaches the saga. Either way the saga
    then proves the result with a strongly consistent read
    (``_guidance_proposal_status``) and reports the proposal status as
    drifted unless that read says ``pending``."""
    try:
        _update_status(table, item["mapping_id"], item["discovered_at"],
                       "pending", expect_status="accepted")
    except Exception as exc:
        if not _is_conditional_miss(exc):
            raise
    return True


def _is_conditional_miss(exc: Exception) -> bool:
    """True for DynamoDB's ``ConditionalCheckFailedException``: boto3 raises
    a ``ClientError`` subclass of that name carrying the code; offline fakes
    raise a plain class of the same name."""
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    return (code == "ConditionalCheckFailedException"
            or type(exc).__name__ == "ConditionalCheckFailedException")


def _execute_guidance_item(table, item: dict, actor: str,
                           confirmation: dict | None = None) -> dict:
    """Strict parity → graph → delivery → terminal proposal transaction."""
    from .guidance_acceptance import execute_guidance_acceptance
    from .guidance_saga import note_stopped
    from .system_guidance import restore, snapshot
    system_id = str(item.get("from_system") or "")
    error = None
    try:
        current = _read_query_guidance_strict(system_id)
        delivery_before = snapshot(system_id)
    except Exception as exc:
        current, error = None, exc
    if current is None:
        from .guidance_saga import pending_result
        note_stopped("reading the current guidance", error,
                     reason="the graph returned no system row")
        return {**pending_result(), "outcome": "unavailable",
                "http_status": 503}
    return execute_guidance_acceptance(
        system_id=system_id, current=current,
        note=str(item.get("to_value") or ""), confirmation=confirmation,
        graph_compare_and_set=lambda expected, desired: (
            _guidance_graph_compare_and_set(system_id, expected, desired)),
        graph_read=lambda: _read_query_guidance_strict(system_id),
        delivery_before=delivery_before,
        delivery_compare_and_set=lambda expected, target: (
            restore(system_id, expected, target)),
        delivery_read=lambda: snapshot(system_id),
        terminal_compare_and_set=lambda graph_text, delivered: (
            _guidance_terminal_accept(
                table, item, actor, graph_text, delivered)),
        terminal_read=lambda: _guidance_proposal_status(table, item),
        terminal_restore=lambda: _guidance_terminal_restore(table, item),
        max_len=_MAX_GUIDANCE_LEN,
        scope=str(item.get("guidance_scope") or "legacy_unscoped"))


def _accept_guidance_pending(table, item: dict, actor: str,
                             confirmation: dict | None) -> dict:
    """Return accepted only after every saga stage and readback succeeds."""
    result = _execute_guidance_item(table, item, actor, confirmation)
    if result.get("status") != "delivered":
        return result
    # The saga's success shape carries ``"error": None``; the route layer's
    # codebase-wide ``"error" in result`` idiom reads key PRESENCE as failure
    # (measured live: every successful accept answered 500 after the writes).
    # An accepted result carries no error key at all.
    accepted = {k: v for k, v in result.items() if k != "error"}
    return {**accepted, "status": "accepted", "proposed_type": "guidance_note",
            "neptune_node_id": f"guidance-{_subject_id(item['subject_key'])}",
            "version": int(item.get("version", "1"))}


def preview_guidance_mapping(mapping_id: str) -> dict:
    """Read-only opaque preview for one pending guidance proposal."""
    from .guidance_acceptance import preview_guidance
    try:
        response = _get_table().query(
            KeyConditionExpression="mapping_id = :mid",
            ExpressionAttributeValues={":mid": mapping_id},
        )
    except Exception:
        preview = preview_guidance(None, None, max_len=_MAX_GUIDANCE_LEN)
        return {"status": "preview", "preview": preview, "http_status": 503}
    items = response.get("Items", [])
    if not items:
        return {"error": "mapping_not_found", "http_status": 404}
    item = items[0]
    if item.get("proposed_type") != "guidance_note" or item.get("status") != "pending":
        return {"error": "guidance_preview_unavailable", "http_status": 409}
    try:
        current = _read_query_guidance_strict(str(item.get("from_system") or ""))
    except Exception:
        current = None
    preview = preview_guidance(
        current, str(item.get("to_value") or ""), max_len=_MAX_GUIDANCE_LEN,
        scope=str(item.get("guidance_scope") or "legacy_unscoped"))
    code = 503 if preview["outcome"] == "unavailable" else 200
    return {"status": "preview", "mapping_id": mapping_id, "preview": preview,
            **({"http_status": code} if code != 200 else {})}


def _apply_guidance_note(item: dict, subject_id: str, version: int) -> str:
    """Guidance acceptance has a dedicated graph+delivery transaction."""
    raise RuntimeError(
        "guidance_note must use the strict acceptance path before terminal status"
    )


# T1.3 concept_overlay item contract: concept = concept id, from_value = pack
# id, to_value = source stamp ("pack:<id>@<ver>" | "interview:<session>"),
# query_context = JSON {examples, synonyms, value_patterns, note}.
_CONCEPT_OVERLAY_TEXT_FIELDS = ("examples", "synonyms", "value_patterns", "note")
_CONCEPT_OVERLAY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _concept_overlay_fields(item: dict) -> dict:
    """Parse + SANITIZE the overlay text fields from the item's query_context.

    Every overlay text field passes ``untrusted_text.sanitize_untrusted`` +
    ``validate_untrusted`` at WRITE time (T1.7 — overlays compose into every
    map prompt; composition-time caps alone are not an injection defense).
    Fail-loud ``ValueError`` with the pinned reason on rejection — a hostile
    or malformed overlay never reaches the graph. Returns the non-empty
    sanitized fields; raises when none remain."""
    from .untrusted_text import sanitize_untrusted, validate_untrusted
    try:
        raw = json.loads(item.get("query_context") or "{}")
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"concept_overlay query_context must be a JSON object of overlay "
            f"text fields {_CONCEPT_OVERLAY_TEXT_FIELDS}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(
            "concept_overlay query_context must be a JSON object of overlay "
            f"text fields {_CONCEPT_OVERLAY_TEXT_FIELDS}")
    fields: dict[str, str] = {}
    for name in _CONCEPT_OVERLAY_TEXT_FIELDS:
        value = raw.get(name)
        if value in (None, ""):
            continue
        sanitized = sanitize_untrusted(value)
        problem = validate_untrusted(sanitized, field=f"'{name}'",
                                     label=f"concept overlay {name}")
        if problem:
            reason, detail = problem
            raise ValueError(
                f"concept_overlay rejected ({reason}): {detail}")
        if sanitized:
            fields[name] = sanitized
    if not fields:
        raise ValueError(
            "concept_overlay proposal carries no overlay text — supply at "
            f"least one of {_CONCEPT_OVERLAY_TEXT_FIELDS} in query_context")
    return fields


def _apply_concept_overlay(item: dict, subject_id: str, version: int) -> str:
    """Apply an accepted ``concept_overlay`` proposal (T1.3, ADR-0050 D1).

    MERGEs the STANDALONE ``(:ConceptOverlay)`` node keyed on
    ``ovl:<pack_id>:<concept_id>`` — NO new edge type, and the target Concept
    node is NEVER touched (its ``source`` stays ``core``; overlays never fork
    a concept, ADR-0020). Text fields are sanitized + validated at this write
    (:func:`_concept_overlay_fields`); a field absent from the proposal is
    cleared on the node so a superseding edit fully replaces the prior
    content. Uninstall = delete by ``pack_id`` (T2.2). Fail-loud via
    ``_neptune_write_loud``."""
    cid = str(item.get("concept", "") or "").strip()
    pack_id = str(item.get("from_value", "") or "").strip()
    if not cid or not pack_id:
        raise ValueError(
            "concept_overlay proposal needs concept (the concept id) and "
            "from_value (the pack id) — edit the proposal before accepting")
    if not _CONCEPT_OVERLAY_ID_RE.match(pack_id):
        raise ValueError(
            f"concept_overlay pack id {pack_id!r} is not a valid pack id "
            "(alphanumeric plus ._- only)")
    fields = _concept_overlay_fields(item)
    source = str(item.get("to_value", "") or "").strip() or f"pack:{pack_id}"
    overlay_id = f"ovl:{pack_id}:{cid}"
    set_clause = (
        "o.concept_id = $cid, o.pack_id = $pack, o.source = $src, "
        "o.created_at = coalesce(o.created_at, $ts), o.created_by = $actor, "
        "o.updated_at = $ts, o.version = $ver"
    )
    params = {"oid": overlay_id, "cid": cid, "pack": pack_id, "src": source,
              "ts": datetime.now(timezone.utc).isoformat(),
              "actor": item.get("accepted_by", ""), "ver": version}
    for name in _CONCEPT_OVERLAY_TEXT_FIELDS:
        set_clause += f", o.{name} = ${name}"
        params[name] = fields.get(name, "")
    _neptune_write_loud(
        f"MERGE (o:ConceptOverlay {{id: $oid}}) SET {set_clause}", params)
    return overlay_id


_METRIC_CONCEPT_IDS_CYPHER = (
    "MATCH (c:Concept) WHERE coalesce(c.retired, false) = false "
    "RETURN c.id AS id ORDER BY id LIMIT 500"
)


def _metric_definition_context() -> tuple[frozenset[str], frozenset[str],
                                          frozenset[str], dict[str, dict]]:
    """Current concept/metric universe for accept and read-only preview."""
    from .catalog import CORE_CONCEPTS
    from .metric_definition_store import load_definitions
    from .metric_layer import CodeMetricLayer, get_metric_layer
    from .vocabulary import neptune_query

    rows = neptune_query(_METRIC_CONCEPT_IDS_CYPHER, {}) or []
    concepts = set(CORE_CONCEPTS)
    concepts.update(str(row["id"]) for row in rows if row.get("id"))
    definitions = load_definitions()
    code_ids = frozenset(m.id for m in CodeMetricLayer().list_metrics())
    metric_ids = {m.id for m in get_metric_layer().list_metrics()}
    metric_ids.update(definitions)
    return frozenset(concepts), frozenset(metric_ids), code_ids, definitions


def _validate_metric_definition(doc: dict, context: tuple) -> None:
    from .metric_definition import MetricDefinitionError, validate_definition
    from .metric_dimension import WINDOW_VOCAB

    concepts, metric_ids, code_ids, definitions = context
    try:
        validate_definition(
            doc, known_concepts=concepts, known_metric_ids=metric_ids,
            code_metric_ids=code_ids, window_vocab=WINDOW_VOCAB,
            definitions=definitions,
        )
    except MetricDefinitionError as exc:
        raise ValueError(str(exc)) from exc


def _apply_metric_definition(item: dict, subject_id: str, version: int) -> str:
    """Validate, write with exact readback, then invalidate the composed layer."""
    del subject_id  # the graph store is keyed by the C1 metric id (ADR-0066 C3)
    from . import metric_definition_store, metric_layer
    from .metric_definition import MetricDefinitionError, parse_definition

    try:
        doc = parse_definition(item.get("query_context") or "")
    except MetricDefinitionError as exc:
        raise ValueError(str(exc)) from exc
    _validate_metric_definition(doc, _metric_definition_context())
    node_id = metric_definition_store.write_definition(
        doc, actor=str(item.get("accepted_by") or ""), version=version)
    metric_layer.invalidate_metric_layer()
    return node_id


def _formula_term_document(term) -> dict:
    from .metric_layer import ConceptRef, Formula, MetricRef
    if isinstance(term, Formula):
        return {"op": term.op.value,
                "inputs": [_formula_term_document(child) for child in term.inputs]}
    if isinstance(term, MetricRef):
        return {"metric": term.metric}
    if isinstance(term, ConceptRef):
        return {"concept": term.concept}
    return {"value": float(term.value)}


def _code_metric_document(metric) -> dict:
    """Synthesize the C1 view needed for a code-collision preview."""
    from .metric_definition import filter_to_document
    from .metric_layer import DirectMetric
    doc = {"id": metric.id, "kind": metric.kind.value, "label": metric.label,
           "synonyms": [], "unit": metric.unit,
           "max_exclusive": metric.max_exclusive}
    if isinstance(metric, DirectMetric):
        doc.update(concept=metric.concept, aggregation=metric.aggregation.value)
        if metric.filter:
            doc["filter"] = filter_to_document(metric.filter)
    else:
        doc["formula"] = _formula_term_document(metric.formula)
        if metric.prefer_stored:
            doc["prefer_stored"] = metric.prefer_stored
        if metric.denominator_provenance:
            doc["denominator_provenance"] = metric.denominator_provenance
    if metric.valid_range is not None:
        doc["valid_range"] = list(metric.valid_range)
    if metric.window:
        doc["window"] = metric.window
    return doc


def _pending_metric_definition_item(mapping_id: str) -> dict:
    """The PENDING ``metric_definition`` proposal row, or an error dict
    carrying ``http_status`` (404 unknown id, 409 not a pending definition)."""
    response = _get_table().query(
        KeyConditionExpression="mapping_id = :mid",
        ExpressionAttributeValues={":mid": mapping_id},
    )
    items = response.get("Items", [])
    if not items:
        return {"error": "mapping_not_found", "http_status": 404}
    item = items[0]
    if (item.get("proposed_type") != "metric_definition"
            or item.get("status") != "pending"):
        return {"error": "metric_definition_preview_unavailable", "http_status": 409}
    return item


def _structural_metric_preview(item: dict, context: tuple) -> dict:
    """The ADR-0066 structural diff + current-universe validation report."""
    from .metric_definition import (
        MetricDefinitionError, diff_definitions, parse_definition,
    )
    from .metric_layer import CodeMetricLayer
    definitions = context[3]
    try:
        proposed = parse_definition(item.get("query_context") or "")
    except MetricDefinitionError as exc:
        return {"current": None, "proposed": item.get("query_context") or "",
                "diff": None, "validation": {"valid": False, "error": str(exc)},
                "collision": False}

    code_by_id = {m.id: m for m in CodeMetricLayer().list_metrics()}
    collision = proposed["id"] in code_by_id
    current = (_code_metric_document(code_by_id[proposed["id"]]) if collision
               else definitions.get(proposed["id"]))
    try:
        _validate_metric_definition(proposed, context)
        validation = {"valid": True, "error": ""}
    except ValueError as exc:
        validation = {"valid": False, "error": str(exc)}
    return {"current": current, "proposed": proposed,
            "diff": diff_definitions(current, proposed),
            "validation": validation, "collision": collision}


def _companion_metric_documents(with_ids) -> dict:
    """``with=`` ids → ``{mapping_id: query_context}`` — each must be a PENDING
    metric definition; the first that is not returns its error dict."""
    docs: dict = {}
    for companion_id in with_ids:
        companion = _pending_metric_definition_item(companion_id)
        if "http_status" in companion:
            return {**companion, "error": f"with_{companion['error']}",
                    "mapping_id": companion_id}
        docs[companion_id] = companion.get("query_context") or ""
    return docs


def preview_metric_definition_mapping(mapping_id: str, *, resolve: bool = False,
                                      with_ids=()) -> dict:
    """Read-only structural diff and current-universe validation report.

    ``resolve=True`` (ADR-0080) adds the ``dry_run`` block: the pending
    definition (plus pending ``with_ids`` companions) resolved read-only by the
    live card's resolver — see ``metric_preview``. ZERO writes either way; with
    ``resolve`` False the response is byte-identical to the structural preview.
    """
    item = _pending_metric_definition_item(mapping_id)
    if "http_status" in item:
        return item
    context = _metric_definition_context()
    preview = _structural_metric_preview(item, context)
    if not resolve:
        return preview
    companions = _companion_metric_documents(with_ids)
    if "http_status" in companions:
        return companions
    from .metric_preview import dry_run
    return {**preview, "dry_run": dry_run(
        item.get("query_context") or "", companions, context)}


def _apply_to_neptune(item: dict, subject_id: str, version: int, *, proposal: bool = False) -> str:
    """Shared subject-keyed applied-node writer (ADR 0003 D). Returns neptune_node_id.

    MERGE-upserts the SAME node in place keyed on ``subject_id`` and stamps
    ``version`` (so a supersede chain re-keys one live node, not a new one per
    proposal). Dispatches to one per-type applier; each reuses the per-type
    Cypher body from the legacy accept path. Fail-loud via
    ``_neptune_write_loud``. Used by BOTH accept_mapping (``proposal``: the
    accept of a filed proposal) and record_user_correction (no duplication).
    """
    proposed_type = item.get("proposed_type", "value_mapping")
    concept = item.get("concept", "")
    if proposed_type == "insight":
        # An insight is a FINDING — informational context only. It NEVER
        # becomes a graph rule: accepting one merely marks it reviewed and
        # writes NOTHING to Neptune. EXPLICIT no-op so an accepted insight can
        # never fall through to the ValueMapping default ("insights are
        # findings, never graph rules" invariant).
        return f"insight-{subject_id}"
    if proposed_type == "concept_vocab":
        # ADR-0050 vocabulary write — Concept node properties only.
        return _apply_concept_vocab(item, subject_id, version)
    if proposed_type == "guidance_note":
        # ADR-0050 — delegates to the existing query_guidance accumulation.
        return _apply_guidance_note(item, subject_id, version)
    if proposed_type == "concept_overlay":
        # T1.3 — standalone ConceptOverlay node (no new edge type, ADR-0050
        # D1); the Concept node itself is never touched (source stays 'core').
        return _apply_concept_overlay(item, subject_id, version)
    if proposed_type == "metric_definition":
        return _apply_metric_definition(item, subject_id, version)
    if proposed_type == "concept_mapping":
        # S3.2: repoint the authoritative MAPS_TO edge (no VM/JP/IR node). FLAG a.
        return _repoint_maps_to(item)
    if proposed_type == CONCEPT_MAPPING_RETIRE:
        # Remove ONE concept -> column edge; the concept's other links stay.
        return _retire_maps_to(item)
    if proposed_type == "field_concept_mint":
        return _apply_field_concept_mint(item, version)
    if proposed_type == "correlation":
        # ADR-0051 b: a human-accepted graduated finding becomes a Correlation
        # node (the metamodel type finally gets a producer — via review only).
        return _apply_correlation(item, subject_id, version)
    if proposed_type == "join_path":
        return _apply_join_path(item, subject_id, version, concept)
    if proposed_type == "identity_rule":
        return _apply_identity_rule(item, subject_id, version, concept, proposal=proposal)
    if proposed_type == "field_annotation":
        return _apply_field_annotation(item, subject_id, version)
    return _apply_value_mapping(item, subject_id, version, concept, proposed_type)



def _non_executable_mapping_item(item: dict) -> bool:
    proposed_type = item.get("proposed_type", "value_mapping")
    if proposed_type == "field_annotation":
        return not _qualified_physical_field(str(item.get("from_field") or ""))
    if proposed_type != "value_mapping":
        return False
    endpoints = (str(item.get("from_value") or ""), str(item.get("to_value") or ""))
    return any(not value or value.startswith("sha256:") or len(value) > 512 for value in endpoints)
def _schema_drift_materialisable(item: dict) -> bool:
    """ADR-0077 D1: ``column_added``/``table_added`` MATERIALISE on accept."""
    from .schema_drift import MATERIALISABLE_KINDS
    return item.get("reason") in MATERIALISABLE_KINDS


def _accept_schema_drift_materialise(table, item: dict, actor: str = "") -> dict:
    """Materialising accept for ADDITIVE schema drift (ADR-0077 D1).

    Validate → re-read the source → MERGE the Field(s)/SchemaEntry through the
    ONE writer home ``schema_fields`` → exact readback, all inside
    ``schema_repair.materialise_drift`` over this module's loud seams. A
    ``ValueError`` (bad identifier / not in source / table not onboarded) is a
    422 with ZERO writes; a ``RuntimeError`` (graph unreachable, readback
    miss) propagates. Either way the row stays pending. Never MAPS_TO,
    authority, identity/join rules or runtime state; no LLM."""
    from .schema_repair import materialise_drift
    if item.get("status") != "pending":
        # A graph-mutating accept is a steward decision on a row UNDER review:
        # rejected / superseded / already-accepted rows are never re-applied by id.
        return {"error": f"schema drift proposal is {item.get('status') or 'unknown'}, "
                         "not pending — nothing materialised", "http_status": 409,
                "materialised": False, "proposed_type": "schema_drift"}
    try:
        written = materialise_drift(item, write=_neptune_write_loud, query=_neptune_query)
    except ValueError as exc:
        return {"error": f"schema drift not materialised: {exc}", "http_status": 422,
                "materialised": False, "proposed_type": "schema_drift"}
    # Terminal transition is CONDITIONAL on the row still being pending (the
    # bulk repair route and a concurrent steward click cannot both win).
    _update_status(table, item["mapping_id"], item["discovered_at"], "accepted", {
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "neptune_node_id": written["node_id"],
        **({"accepted_by": actor} if actor else {}),
    }, expect_status="pending")
    logger.info("Schema drift materialised: %s (%s → %d field(s) on %s)",
                item["mapping_id"], item.get("reason", ""), written["field_count"],
                written["schema_entry"])
    return {"status": "accepted", "acknowledged": True, "materialised": True,
            "proposed_type": "schema_drift", "version": int(item.get("version", "1")),
            "neptune_node_id": written["node_id"], "written": written}


def _accept_schema_drift(table, item: dict, actor: str = "") -> dict:
    """Accept for a ``schema_drift`` proposal (ADR-0053 E5, amended by ADR-0077).

    ``column_added``/``table_added`` MATERIALISE the column/table
    (``_accept_schema_drift_materialise``). ``column_removed``/
    ``column_retyped``/``table_removed`` stay ACKNOWLEDGE-ONLY: the steward
    has SEEN it — ZERO graph writes (destructive/semantic drift; re-onboarding
    is the steward's explicit fix). The row flips to ``accepted`` with the
    usual provenance stamps but NO ``neptune_node_id`` (honest absence — there
    is no applied artifact). For removal drifts the existing
    ``reconcile_blocked_proposals`` cleanup runs best-effort, purging pending
    proposals that reference the now-gone column/table.
    """
    if _schema_drift_materialisable(item):
        return _accept_schema_drift_materialise(table, item, actor)
    _update_status(table, item["mapping_id"], item["discovered_at"], "accepted", {
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        **({"accepted_by": actor} if actor else {}),
    })
    logger.info(
        f"Schema drift acknowledged: {item['mapping_id']} "
        f"({item.get('reason', '')} on {item.get('from_system', '')}."
        f"{item.get('from_field', '')})"
    )
    if item.get("reason") in ("column_removed", "table_removed"):
        try:  # best-effort cleanup — the acknowledgment itself already landed
            reconcile_blocked_proposals()
        except Exception as e:  # noqa: BLE001 - cleanup must not fail the ack
            logger.warning(f"post-acknowledge reconcile failed (non-fatal): {e}")
    return {"status": "accepted", "acknowledged": True, "materialised": False,
            "proposed_type": "schema_drift",
            "version": int(item.get("version", "1"))}


def _accept_coverage_gap(table, item: dict, actor: str = "") -> dict:
    """Acknowledge-only accept for a ``coverage_gap`` proposal (the
    ``_accept_schema_drift`` pattern, ADR-0053 E5 posture).

    Accepting a coverage-gap finding means the steward has SEEN the candidate
    — it performs ZERO graph writes, ever. In particular it never runs
    ``_repoint_maps_to``: that path's column-keyed delete (the R5 win-rule)
    would strip a NEIGHBORING concept's edge off the candidate column, and a
    coverage-gap candidate is by design allowed to already map a different
    concept (the many-to-one field rule). Applying the mapping stays the
    steward's explicit remap action (``POST /api/onboard/remap`` — MERGE-only,
    never deletes). The row flips to ``accepted`` with the usual provenance
    stamps but NO ``neptune_node_id`` (honest absence — there is no applied
    artifact).
    """
    _update_status(table, item["mapping_id"], item["discovered_at"], "accepted", {
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        **({"accepted_by": actor} if actor else {}),
    })
    logger.info(
        f"Coverage-gap finding acknowledged: {item['mapping_id']} "
        f"({item.get('to_system', '')}.{item.get('to_field', '')} → "
        f"{item.get('concept', '')})"
    )
    return {"status": "accepted", "acknowledged": True,
            "proposed_type": "coverage_gap",
            "version": int(item.get("version", "1"))}


def _accept_steward_question(table, item: dict, actor: str = "") -> dict:
    """Acknowledge-only accept for a ``steward_question`` proposal (ADR-0059
    D3; the ``_accept_coverage_gap`` pattern, ADR-0053 E5 posture).

    Accepting a steward question means the steward has SEEN it — it performs
    ZERO graph writes, ever, and it is NOT the answer action. Answering
    happens through the guided-turn machinery (``context_interview.landing.
    land_answer``), which lands the knowledge as its own pending proposals and
    supersedes this row with ``superseded_reason="answered"``. The row flips
    to ``accepted`` with the usual provenance stamps but NO ``neptune_node_id``
    (honest absence — there is no applied artifact).
    """
    _update_status(table, item["mapping_id"], item["discovered_at"], "accepted", {
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        **({"accepted_by": actor} if actor else {}),
    })
    logger.info(
        f"Steward question acknowledged: {item['mapping_id']} "
        f"({item.get('reason', '')} on {item.get('from_system', '')} / "
        f"{item.get('concept', '')})"
    )
    return {"status": "accepted", "acknowledged": True,
            "proposed_type": "steward_question",
            "version": int(item.get("version", "1"))}


# Acknowledge-only accept dispatch (ADR-0053 E5 posture): these types flip to
# ``accepted`` with ZERO graph writes — the diagnostic classes. schema_drift is
# acknowledge-only EXCEPT the materialisable additive kinds (ADR-0077 D1).
_ACKNOWLEDGE_ONLY_ACCEPTS = {
    "schema_drift": _accept_schema_drift,
    "coverage_gap": _accept_coverage_gap,
    "steward_question": _accept_steward_question,
}


def _single_row_mode(confirmation_mode: str, actor: str) -> str:
    """The ``confirmation_mode`` an accept stamps: the one-row mode
    (``identifier_translation.SINGLE_ROW_CONFIRMATION``) when the caller
    passed it AND named the accepting person, else ``""``."""
    from .identifier_translation import SINGLE_ROW_CONFIRMATION
    return (SINGLE_ROW_CONFIRMATION
            if confirmation_mode == SINGLE_ROW_CONFIRMATION and str(actor or "").strip() else "")


def accept_mapping(mapping_id: str, actor: str = "",
                   guidance_confirmation: dict | None = None,
                   confirmation_mode: str = "") -> dict:
    """Accept through the unchanged type path; guidance proves delivery first.
    ``confirmation_mode``: see :func:`_accept_applied`."""
    table = _get_table()
    response = table.query(
        KeyConditionExpression="mapping_id = :mid",
        ExpressionAttributeValues={":mid": mapping_id},
    )
    items = response.get("Items", [])
    if not items:
        return {"error": f"Mapping {mapping_id} not found"}

    item = items[0]
    if item.get("proposed_type") == "guidance_note":
        if item.get("status") != "pending":
            return {"status": item.get("status", ""),
                    "error": "guidance_proposal_not_pending",
                    "http_status": 409}
        return _accept_guidance_pending(
            table, item, actor, guidance_confirmation)
    ack_only = _ACKNOWLEDGE_ONLY_ACCEPTS.get(item.get("proposed_type", ""))
    if ack_only is not None:
        return ack_only(table, item, actor)
    if _non_executable_mapping_item(item):
        return {"error": "Mapping is non-actionable and cannot be accepted"}
    refusal = _user_proposal_refusal(item)
    if refusal:
        return {"error": refusal, "http_status": 400}
    blocked = _identity_rule_category_refusal(item)
    if blocked:
        return {"error": f"{blocked}; the proposal stays pending", "blocked_reason": blocked,
                "status": "pending", "http_status": 400}
    return _accept_applied(table, item, mapping_id, actor, confirmation_mode)


def _accept_applied(table, item: dict, mapping_id: str, actor: str,
                    confirmation_mode: str) -> dict:
    """The graph-applying tail of :func:`accept_mapping`: apply the item to
    its subject node, then mark the ledger row accepted.

    ``confirmation_mode`` is ``identifier_translation.SINGLE_ROW_CONFIRMATION``
    only from the route a person uses to accept ONE proposal
    (``server._accept_proposed_mapping_sync``); with a non-empty ``actor`` it
    stamps the ValueMapping node (``_apply_value_mapping``) and the ledger row
    (so a replay keeps it). Every other caller (accept-all, an explicit batch,
    the high-confidence sweep, the fresh-log auto-accept) leaves it empty,
    and the ledger row's mode is then written empty too."""
    discovered_at = item["discovered_at"]
    proposed_type = item.get("proposed_type", "value_mapping")
    subject_key = item.get("subject_key")
    if not subject_key:
        raise RuntimeError(
            f"Mapping {mapping_id} has no subject_key (legacy item). Run "
            f"clear_all_learnings() to reset the proposal store, then re-discover."
        )
    subject_id = _subject_id(subject_key)
    version = int(item.get("version", "1"))

    if actor:
        item["accepted_by"] = actor  # visible to _apply_to_neptune's provenance stamp
    item["confirmation_mode"] = _single_row_mode(confirmation_mode, actor)
    neptune_node_id = _apply_to_neptune(item, subject_id, version, proposal=True)

    _update_status(table, mapping_id, discovered_at, "accepted", {
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "neptune_node_id": neptune_node_id,
        **({"accepted_by": actor} if actor else {}),
        "confirmation_mode": item["confirmation_mode"],
    })
    _reset_accepted_coverage_cache()
    logger.info(f"Mapping accepted: {mapping_id} → {neptune_node_id} (type={proposed_type}, v{version})")
    return {"status": "accepted", "neptune_node_id": neptune_node_id,
            "proposed_type": proposed_type, "version": version}


def _refuse_metric_definition_correction(proposed_type: str) -> None:
    """Close the one-door invariant AT the ``record_user_correction`` door.

    That entry point is accepted-on-entry and dispatches generically through
    ``_apply_to_neptune``, which HAS a ``metric_definition`` arm — so without this
    guard a caller passing ``proposed_type='metric_definition'`` would MERGE a
    formula immediately and the steward would never see the computed diff
    (ADR-0066 D3). Today no caller does; the invariant must hold at the door
    rather than by caller discipline, so this refuses fail-loud BEFORE any read or
    write. A formula routes through ``log_proposed_mapping`` →
    ``preview_metric_definition_mapping`` → ``accept_mapping``.
    """
    if str(proposed_type or "").strip().lower() == "metric_definition":
        raise ValueError(
            "record_user_correction refuses proposed_type='metric_definition' "
            "(ADR-0066 D3): a metric formula defines what a number MEANS and "
            "must go through propose → preview → accept so the steward sees the "
            "computed diff. File it with log_proposed_mapping instead."
        )


def _refuse_category_identity_correction(proposed_type: str, concept: str) -> None:
    """A steward's ``identity_rule`` correction on a category or status
    concept is refused, with the reason the resolve step, the provision
    writer and the proposal accept give (``resolve_gates.category_rejection``,
    the graph's entity-layer concepts exempt). It raises ``ValueError`` BEFORE
    any write, so the user-define route answers 400 with the message and
    nothing is stored or applied. Only the entity-layer read runs first, and
    only for a non-core concept whose name the check would withhold."""
    if str(proposed_type or "").strip().lower() != "identity_rule":
        return
    from .resolve_gates import category_rejection, entity_layer_concepts
    reason = category_rejection({"concept": concept},
                                entity_layer_concepts([concept], _neptune_query))
    if reason:
        raise ValueError(f"{reason}; an identity rule needs a concept that names one "
                         "record, so the correction is not applied")


def record_user_correction(
    from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str,
    concept: str, *, proposed_type: str = "value_mapping",
    confidence: float = 1.0, query_context: str = "user_correction",
    description: str = "", actor: str = "", confirmation_mode: str = "",
) -> dict:
    """Enter the lifecycle already ``accepted`` with source='user'; run the SAME
    collision/supersede logic (user wins over anything on the subject — including
    a prior accepted agent proposal); apply to Neptune immediately via the
    subject-key applied node. Fail-loud. The two HTTP routes reach it only for a
    steward; anyone else's assertion goes through :func:`record_user_proposal`.
    ``description`` is the minted concept's cosmetic definition (ADR 0021), stored
    only when non-empty. ``actor`` and ``confirmation_mode``: see
    :func:`_store_user_correction`. REFUSES ``metric_definition`` at the door
    (see the guard), and an ``identity_rule`` on a category or status concept
    (:func:`_refuse_category_identity_correction`).
    """
    _refuse_metric_definition_correction(proposed_type)
    _refuse_category_identity_correction(proposed_type, concept)
    table = _get_table()
    keys = _canonical_keys_for(
        proposed_type,
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value,
        concept=concept, annotation=query_context.replace("Auto-analysis: ", ""),
        description=description,
    )
    prior = _query_subject(table, keys.subject_key)
    _action, _id, rows = _classify_collision(keys, prior, source="user")  # user → supersede all active
    if rows:
        _supersede(table, rows)
    version = _next_version(prior, rows)

    mapping_id, discovered_at, item = _build_item(
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value, concept=concept,
        confidence=confidence, query_context=query_context, proposed_type=proposed_type,
        keys=keys, source="user", status="accepted", version=version,
        to_description=description,
    )
    item["method"] = "user_corrected"
    item["accepted_at"] = discovered_at
    return _store_user_correction(table, item, (mapping_id, discovered_at),
                                  _subject_id(keys.subject_key), version,
                                  actor, confirmation_mode)


def _store_user_correction(table, item: dict, key: tuple, subject_id: str, version: int,
                           actor: str, confirmation_mode: str) -> dict:
    """Store the accepted correction (``key`` is its ``(mapping_id,
    discovered_at)``), apply it to its subject node, and record the node id
    (:func:`record_user_correction`). The user-define route passes the
    steward as ``actor`` and the one-row ``confirmation_mode``: a correction
    is one row a person wrote, so it is stamped the way a one-row accept is
    (:func:`_accept_applied`); any other caller stores it unstamped."""
    mapping_id, discovered_at = key
    mode = _single_row_mode(confirmation_mode, actor)
    if mode:
        item["accepted_by"], item["confirmation_mode"] = actor, mode
    _put_item(table, item)
    _reset_accepted_coverage_cache()

    neptune_node_id = _apply_to_neptune(item, subject_id, version)
    _update_status(table, mapping_id, discovered_at, "accepted",
                   {"neptune_node_id": neptune_node_id})

    proposed_type = item.get("proposed_type", "value_mapping")
    logger.info(f"User correction recorded: {mapping_id} → {neptune_node_id} "
                f"(type={proposed_type}, v{version})")
    return {"status": "accepted", "neptune_node_id": neptune_node_id,
            "proposed_type": proposed_type, "version": version}


#: The ``method`` stamped on a non-steward's pending assertion
#: (:func:`record_user_proposal`). It marks the rows whose field check runs at
#: the steward's accept (:func:`_user_proposal_refusal`).
USER_PROPOSED_METHOD = "user_proposed"


def _identity_rule_category_refusal(item: dict) -> str:
    """Why an ``identity_rule`` proposal must not be applied, or ``""``: its
    concept names a category or status (``resolve_gates.category_rejection``,
    the decision the resolve step and the provision writer apply; the graph's
    entity-layer concepts are exempt). Every accept goes through
    :func:`accept_mapping` (one row, accept-all, the sweeps), so the rule
    never reaches ``_apply_identity_rule`` and the proposal stays pending."""
    if item.get("proposed_type") != "identity_rule":
        return ""
    from .resolve_gates import category_rejection, entity_layer_concepts
    concept = str(item.get("concept") or "")
    return category_rejection({"concept": concept},
                              entity_layer_concepts([concept], _neptune_query))


def _user_proposal_refusal(item: dict) -> str:
    """Why a steward's accept must refuse a non-steward's proposal, or ``""``.

    :func:`record_user_proposal` reads nothing from the graph, so the checks
    the steward's own correction runs before writing (the user-define route's
    semantic validation) run here instead, at the accept that would apply the
    row: each named system must still be onboarded, and a field the schema
    KNOWS is absent refuses (an unavailable schema does not, as on the route).
    Only rows stamped :data:`USER_PROPOSED_METHOD` are checked; every other
    row accepts exactly as before."""
    if str(item.get("method") or "") != USER_PROPOSED_METHOD:
        return ""
    from . import schema_check
    for side in ("from", "to"):
        system = str(item.get(f"{side}_system") or "").strip()
        if system and not schema_check.is_onboarded_system(system):
            return (f"{side}_system '{system}' is not an onboarded system; "
                    "the proposal stays pending")
    for side in ("from", "to"):
        system = str(item.get(f"{side}_system") or "").strip()
        field = str(item.get(f"{side}_field") or "").strip()
        if system and field and schema_check.column_exists(system, field) is False:
            return (f"{side}_field '{field}' does not exist in {system} schema; "
                    "the proposal stays pending")
    return ""


def record_user_proposal(
    from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str,
    concept: str, *, proposed_type: str = "value_mapping",
    confidence: float = 1.0, query_context: str = "user_correction",
    description: str = "",
) -> dict:
    """The non-steward twin of :func:`record_user_correction` (ADR-0050).

    Files the same assertion, with the same canonical keys, as a ``pending``
    proposal for steward review. Nothing on the subject is superseded, and the
    graph is not touched at all, reads included: an open route reaches this
    door, so a non-steward must not be able to drive graph queries through it.
    That is why no target grounding is attached (``_attach_target_grounding``
    probes the graph); absent grounding is honest "not checked", and the
    review surface shows it that way. The field check a steward's own
    correction runs before writing happens at the steward's accept instead
    (``_user_proposal_refusal``). Only a steward's ``accept_mapping`` applies
    the row. This is the rule ``assert_vocab`` already follows for a non-admin
    assertion. ``source='user'`` rows never auto-accept
    (``_NEVER_AUTO_ACCEPT_SOURCES``). An identical active row on the same
    target is returned as it stands instead of filing a duplicate. REFUSES
    ``metric_definition`` exactly like the accepted door. Fail-loud on a
    store error. Returns ``{"status", "mapping_id", "proposed_type",
    "version"}``.
    """
    _refuse_metric_definition_correction(proposed_type)
    table = _get_table()
    keys = _canonical_keys_for(
        proposed_type,
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value,
        concept=concept, annotation=query_context.replace("Auto-analysis: ", ""),
        description=description,
    )
    prior = _query_subject(table, keys.subject_key)
    for row in prior:
        if (row.get("target_key") == keys.target_key
                and row.get("status") in ("pending", "accepted")):
            return {"status": row["status"], "mapping_id": row["mapping_id"],
                    "proposed_type": proposed_type,
                    "version": int(row.get("version", "1") or 1)}
    version = _next_version(prior, [])
    mapping_id, _discovered_at, item = _build_item(
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value, concept=concept,
        confidence=confidence, query_context=query_context, proposed_type=proposed_type,
        keys=keys, source="user", status="pending", version=version,
        to_description=description, method=USER_PROPOSED_METHOD,
    )
    _put_item(table, item)
    logger.info(f"User proposal filed for steward review: {mapping_id} "
                f"(type={proposed_type}, v{version})")
    return {"status": "pending", "mapping_id": mapping_id,
            "proposed_type": proposed_type, "version": version}


def mint_field_concept(
    system: str, table: str, column: str, concept_id: str, name: str,
    confidence: float, source: str = "agent", description: str = "",
) -> str | dict:
    """Mint a field concept for a column (S5.2 W2, ADR 0021). Thin entry over the
    proposal lifecycle reusing the source-aware supersede policy.

    proposed_type='field_concept_mint' is COLUMN-keyed (subject=(to_system,
    to_field)), so a re-mint on the same column supersedes the prior proposal and
    the apply path repoints the column's single authoritative MAPS_TO edge.

    - source='user' → enters the lifecycle ``accepted`` and applies to Neptune
      immediately (and supersedes anything active on the column);
    - source='agent' → logged ``pending`` for review; an agent mint CANNOT clobber
      an accepted user mapping already on the column (record_disagreement).

    The display ``name`` rides in ``to_value`` (not part of the mint key). The
    concept node is created with ``source='minted'`` so it survives the per-system
    onboard orphan cleanup. Returns the user-correction result dict (user) or the
    proposal mapping_id (agent).
    """
    to_field = f"{table}.{column}"
    if source == "user":
        return record_user_correction(
            from_system="", from_field="", from_value="",
            to_system=system, to_field=to_field, to_value=name,
            concept=concept_id, proposed_type="field_concept_mint",
            confidence=confidence, query_context="field_concept_mint",
            description=description,
        )
    return log_proposed_mapping(
        "", "", "", system, to_field, name,
        concept_id, confidence, "field_concept_mint",
        proposed_type="field_concept_mint", source="agent",
        description=description,
    )


def score_assertion_evidence(concept: str, system: str, field: str) -> "float | None":
    """Evidence-at-authoring score for a human concept↔field assertion (ADR-0050).

    REUSES the existing evidence machinery verbatim (imported, never copied):
    sample the named field's values (``resolve_evidence.sample_field_values``),
    gather peer value populations for the concept
    (``concept_suggest._peer_concept_populations``), measure
    ``resolve_evidence.value_overlap``, and map the overlap through
    ``concept_suggest.evidence_confidence`` (capped band, never 1.0).

    Returns the score, or ``None`` when no evidence can be measured — missing
    inputs, no sampled values, no peer population for the concept, or ANY
    backend failure. ALL failures swallow to ``None`` by contract: authoring a
    vocabulary assertion must never break on the evidence check.
    """
    try:
        if not (concept and system and field):
            return None
        from .catalog import catalog
        from .concept_suggest import _peer_concept_populations, evidence_confidence
        from .resolve_evidence import sample_field_values, value_overlap
        entry = catalog.get(system) or {}
        table_name, _sep, column = str(field).rpartition(".")
        vals = sample_field_values(
            system_id=system,
            connection_type=entry.get("connection_type", "PostgreSQL"),
            schema=entry.get("schema", "") or "",
            table=table_name or str(field),
            field=column or str(field),
            connection_config=entry.get("connection_config", {}) or {},
        )
        if not vals:
            return None
        pop = (_peer_concept_populations(system) or {}).get(concept) or {}
        if not pop.get("values"):
            return None
        return evidence_confidence(value_overlap(vals, pop["values"]))
    except Exception as e:  # noqa: BLE001 - evidence is advisory, never blocks authoring
        logger.info("score_assertion_evidence skipped (%s %s %s): %s",
                    system, concept, field, e)
        return None


def _vocab_assertion_fields(
    proposed_type: str, *, concept: str, name: str, description: str,
    synonyms: "list[str] | None", op: str, system_id: str, note: str,
) -> "tuple[str, str, str, str, str]":
    """Validate + normalize a vocabulary assertion's inputs (ADR-0050).

    Returns ``(from_system, to_value, concept, description, query_context)``
    per the frozen Wave-2 item-field contract (the op + optional synonyms ride
    in the ``query_context`` JSON). Raises ``ValueError`` on bad input."""
    if proposed_type not in ("concept_vocab", "guidance_note"):
        # concept_overlay is IN VOCAB_PROPOSED_TYPES (canonical-key treatment)
        # but has no human-assertion route — overlays land as proposals via
        # log_proposed_mapping and apply through _apply_concept_overlay (T1.3).
        raise ValueError(
            f"assert_vocab does not handle proposed_type={proposed_type!r}")
    if proposed_type == "concept_vocab":
        if not (concept or "").strip():
            raise ValueError("concept_vocab assertion requires a concept id")
        if op not in ("add", "edit", "retire"):
            raise ValueError(
                f"concept_vocab op must be add|edit|retire, got {op!r}")
        ctx: dict = {"op": op}
        clean_synonyms = [str(s).strip() for s in (synonyms or [])
                          if str(s).strip()]
        if clean_synonyms:
            ctx["synonyms"] = clean_synonyms
        return ("", (name or "").strip(), concept.strip(), description,
                json.dumps(ctx, sort_keys=True))
    # guidance_note
    if not ((system_id or "").strip() and (note or "").strip()):
        raise ValueError("guidance_note assertion requires system_id and note")
    return (system_id.strip(), note.strip(), "", "",
            json.dumps({"op": "add"}, sort_keys=True))


def _stamp_assertion_evidence(item: dict, score: "float | None",
                              override_evidence: bool) -> None:
    """Record the authoring-time evidence verbatim on the item (non-empty-only,
    the DynamoDB empty-attr convention shared with _build_item)."""
    if score is not None:
        item["evidence_score"] = str(score)
    if override_evidence:
        item["override_evidence"] = "true"


def _insert_pending_assertion(table, prior: list[dict], build: dict,
                              score: "float | None",
                              override_evidence: bool) -> dict:
    """Non-admin assertion path: the row stays ``pending`` for steward review —
    nothing is superseded and nothing is applied until a steward accepts."""
    version = _next_version(prior, [])
    mapping_id, _discovered_at, item = _build_item(
        status="pending", version=version, **build)
    _stamp_assertion_evidence(item, score, override_evidence)
    _put_item(table, item)
    logger.info(f"Vocabulary assertion pending review: {mapping_id} "
                f"(type={item['proposed_type']}, v{version})")
    return {"status": "pending", "mapping_id": mapping_id,
            "evidence_score": score}



def _accept_guidance_assertion(table, prior: list[dict], build: dict,
                               score: "float | None",
                               override_evidence: bool, actor: str) -> dict:
    """Create pending first; only strict graph+delivery proof may accept it."""
    active = [row for row in prior
              if row.get("status") in ("pending", "accepted")]
    version = _next_version(prior, active)
    mapping_id, _discovered_at, item = _build_item(
        status="pending", version=version, **build)
    item["method"] = "user_asserted"
    _stamp_assertion_evidence(item, score, override_evidence)
    _put_item(table, item)
    result = _accept_guidance_pending(table, item, actor, None)
    if result.get("status") != "accepted":
        return {**result, "mapping_id": mapping_id,
                "evidence_score": score}
    if active:
        _supersede(table, active)
    _reset_accepted_coverage_cache()
    return {**result, "mapping_id": mapping_id, "evidence_score": score}

def _accept_assertion(table, prior: list[dict], keys: CanonicalKeys,
                      build: dict, score: "float | None",
                      override_evidence: bool, actor: str) -> dict:
    """Admin/steward assertion path: user wins. The apply runs FIRST, so a
    refused apply (e.g. the mapped-retire guard's ValueError) raises before
    any row is written or superseded; then active prior rows on the subject
    are superseded and the accepted row lands with its applied-node id.

    Guidance is the exception: it is inserted pending first and becomes
    accepted only after graph CAS plus exact delivery readback.
    """
    if build.get("proposed_type") == "guidance_note":
        return _accept_guidance_assertion(
            table, prior, build, score, override_evidence, actor)
    active = [r for r in prior if r.get("status") in ("pending", "accepted")]
    version = _next_version(prior, active)
    mapping_id, discovered_at, item = _build_item(
        status="accepted", version=version, **build)
    item["method"] = "user_asserted"
    item["accepted_at"] = discovered_at
    if actor:
        item["accepted_by"] = actor
    _stamp_assertion_evidence(item, score, override_evidence)

    subject_id = _subject_id(keys.subject_key)
    neptune_node_id = _apply_to_neptune(item, subject_id, version)

    if active:
        _supersede(table, active)
    item["neptune_node_id"] = neptune_node_id
    _put_item(table, item)
    _reset_accepted_coverage_cache()
    logger.info(f"Vocabulary assertion accepted: {mapping_id} → "
                f"{neptune_node_id} (type={item['proposed_type']}, v{version})")
    return {"status": "accepted", "mapping_id": mapping_id,
            "evidence_score": score, "neptune_node_id": neptune_node_id}


def assert_vocab(
    *, proposed_type: str = "concept_vocab", concept: str = "",
    name: str = "", description: str = "",
    synonyms: "list[str] | None" = None, op: str = "add",
    system_id: str = "", note: str = "", evidence_system: str = "",
    evidence_field: str = "", override_evidence: bool = False,
    actor: str = "", is_admin: bool = False, guidance_scope: str = "") -> dict:
    """Human-assertion entry point for the vocabulary types (ADR-0050) — the
    :func:`record_user_correction` mirror for ``concept_vocab`` /
    ``guidance_note``: enters the ONE lifecycle with ``source='user'``,
    ``provenance='human-asserted'``. Admin/steward (``is_admin=True``) → lands
    ``accepted`` and applies immediately (:func:`_accept_assertion` — user
    wins); otherwise stays ``pending`` (:func:`_insert_pending_assertion`).
    An identical ACTIVE assertion dedupes to the existing row. Evidence at
    authoring is stored in ``confidence`` + ``evidence_score`` (``None`` →
    1.0) with ``override_evidence`` recorded verbatim. Returns ``{"status",
    "mapping_id", "evidence_score"}`` (+ ``neptune_node_id`` when applied)."""
    (from_system, to_value, concept, description,
     query_context) = _vocab_assertion_fields(
        proposed_type, concept=concept, name=name, description=description,
        synonyms=synonyms, op=op, system_id=system_id, note=note)
    guidance_scope = _normalized_guidance_scope(proposed_type, guidance_scope)
    score = None
    if proposed_type == "concept_vocab" and evidence_system and evidence_field:
        score = score_assertion_evidence(concept, evidence_system, evidence_field)
    table = _get_table()
    keys = _canonical_keys_for(
        proposed_type, from_system=from_system, to_value=to_value,
        concept=concept, annotation=query_context, description=description,
        guidance_scope=guidance_scope)
    prior = _query_subject(table, keys.subject_key)
    exact_active = [r for r in prior
                    if r.get("target_key") == keys.target_key
                    and r.get("status") in ("pending", "accepted")]
    if exact_active:
        return {"status": exact_active[0]["status"],
                "mapping_id": exact_active[0]["mapping_id"],
                "evidence_score": score}
    build = dict(
        from_system=from_system, from_field="", from_value="",
        to_system="", to_field="", to_value=to_value, concept=concept,
        confidence=score if score is not None else 1.0,
        query_context=query_context, proposed_type=proposed_type, keys=keys,
        to_description=description, source="user", provenance="human-asserted",
        guidance_scope=guidance_scope)
    if not is_admin:
        return _insert_pending_assertion(table, prior, build, score,
                                         override_evidence)
    return _accept_assertion(table, prior, keys, build, score,
                             override_evidence, actor)


def reject_mapping(mapping_id: str, actor: str = "") -> dict:
    """Reject a proposed mapping: update DynamoDB status to rejected. (Swallows.)

    Also stamps ``rejected_at`` (ADR-0042 D3) and, when supplied by a
    steward-gated route, ``rejected_by`` for attributed precision evidence.
    """
    try:
        table = _get_table()
        response = table.query(
            KeyConditionExpression="mapping_id = :mid",
            ExpressionAttributeValues={":mid": mapping_id},
        )
        items = response.get("Items", [])
        if not items:
            return {"error": f"Mapping {mapping_id} not found"}

        item = items[0]
        discovered_at = item["discovered_at"]

        _update_status(table, mapping_id, discovered_at, "rejected", {
            "rejected_at": datetime.now(timezone.utc).isoformat(),
            **({"rejected_by": actor} if actor else {}),
        })

        logger.info(f"Mapping rejected: {mapping_id}")
        return {"status": "rejected"}

    except Exception as e:
        logger.error(f"Failed to reject mapping {mapping_id}: {e}")
        return public_error(e, "learning.reject_mapping")


_REQUEUE_REVIEW_REASON = ("requeued for steward review: accepted without "
                          "target verification (pre-P4)")

# Requeue undo: proposal types whose accept wrote a dedicated graph node that
# no reader soft-filters — the honest undo is deleting the applied node.
_REQUEUE_DELETE_LABELS = {
    "value_mapping": "ValueMapping",
    "crosswalk": "ValueMapping",
    "join_path": "JoinPath",
    "correlation": "Correlation",
}


def _deactivate_requeued_identity_rule(rule_id: str, actor: str) -> None:
    """Soft-deactivate a requeued IdentityRule (the same semantics as the
    /api/thread/rules/{id}/deactivate route) and switch off the JoinPaths made
    from it (joinpath_rule_link); a re-accept restores both. Fail-loud."""
    now = datetime.now(timezone.utc).isoformat()
    _neptune_write_loud(
        "MATCH (ir:IdentityRule {id: $rid}) SET ir.status = 'deactivated', "
        "ir.deactivated_by = $actor, ir.deactivated_at = $now",
        {"rid": rule_id, "actor": actor, "now": now})
    _jp_rule.cascade_rule_lifecycle(_neptune_write_loud, rule_id, actor, now,
                                    deactivate=True)


@mutates_value_mappings
def _undo_graph_artifact(item: dict) -> str:
    """Honestly UNDO the routing effect ``accept_mapping`` created for a row.

    Dispatch mirrors ``_apply_to_neptune``; returns what happened to the
    artifact ('none' | 'deactivated' | 'removed'). Fail-loud via
    ``_neptune_write_loud``.

    - insight → accept wrote nothing → nothing to undo;
    - identity_rule → the EXISTING soft-deactivation machinery (every rule
      reader filters status='deactivated' — same semantics as the
      /api/thread/rules/{id}/deactivate route); re-accept restores 'active';
    - value_mapping/crosswalk/join_path/correlation → DETACH DELETE the
      applied node (no reader-side deactivation filter exists for these
      labels, so a soft mark would NOT stop routing);
    - field_annotation → REMOVE the Field's disambiguation properties;
    - vocab/guidance/concept_mapping/mint types mutated shared graph state
      non-invertibly → ValueError (client-actionable 400 at the route).
    """
    ptype = item.get("proposed_type", "value_mapping")
    node_id = str(item.get("neptune_node_id") or "")
    if ptype == "insight" or not node_id or node_id.startswith(("insight-", "annotation-")):
        return "none"
    if ptype == "identity_rule":
        _deactivate_requeued_identity_rule(node_id, item.get("requeued_by", ""))
        return "deactivated"
    if ptype == "field_annotation":
        from_parts = str(item.get("from_field") or "").rsplit(".", 1)
        if len(from_parts) != 2 or not item.get("from_system"):
            return "none"  # accept could not identify the field either
        _neptune_write_loud(
            "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {name: $tbl})"
            "-[:HAS_FIELD]->(f:Field {name: $fld}) "
            "REMOVE f.disambiguation, f.disambiguation_version",
            {"sid": item["from_system"], "tbl": from_parts[0], "fld": from_parts[1]})
        return "removed"
    # schema_drift is deliberately absent: a materialised Field is schema
    # subgraph, corrected by re-onboard, never DETACH DELETEd (ADR-0077 D1).
    label = _REQUEUE_DELETE_LABELS.get(ptype)
    if label is None:
        raise ValueError(
            f"requeue is not supported for proposed_type={ptype} — accepting it "
            f"mutated shared graph state that cannot be automatically undone")
    _neptune_write_loud(f"MATCH (n:{label} {{id: $nid}}) DETACH DELETE n",
                        {"nid": node_id})
    return "removed"


def requeue_mapping(mapping_id: str, actor: str = "") -> dict:
    """Move an ACCEPTED proposal back to pending for steward re-review,
    honestly UNDOING the routing effect its accept created (the graph artifact
    is removed or deactivated — ``_undo_graph_artifact``).

    The row keeps its history (method/discovered_at/accepted_by untouched);
    ``accepted_at``/``neptune_node_id``/``verified`` are cleared and
    ``review_reason`` states WHY the row is back in the queue. Fail-loud like
    ``accept_mapping``: raises on Neptune/DynamoDB failure (ValueError for the
    unsupported-type case); error dicts only for not-found / not-accepted.
    """
    table = _get_table()
    response = table.query(
        KeyConditionExpression="mapping_id = :mid",
        ExpressionAttributeValues={":mid": mapping_id},
    )
    items = response.get("Items", [])
    if not items:
        return {"error": f"Mapping {mapping_id} not found"}
    item = items[0]
    status = item.get("status", "")
    if status != "accepted":
        return {"error": f"Mapping {mapping_id} is not accepted "
                         f"(status={status or 'unknown'}) — only accepted "
                         f"proposals can be requeued"}
    if actor:
        item["requeued_by"] = actor  # visible to _undo_graph_artifact's stamp
    graph_artifact = _undo_graph_artifact(item)

    expr = ("SET #s = :status, review_reason = :rr, requeued_at = :at"
            + (", requeued_by = :by" if actor else "")
            + " REMOVE accepted_at, neptune_node_id, verified, verified_at,"
              " verified_scope")
    table.update_item(
        Key={"mapping_id": mapping_id, "discovered_at": item["discovered_at"]},
        UpdateExpression=expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "pending", ":rr": _REQUEUE_REVIEW_REASON,
            ":at": datetime.now(timezone.utc).isoformat(),
            **({":by": actor} if actor else {}),
        },
    )
    _reset_accepted_coverage_cache()
    logger.info(f"Mapping requeued: {mapping_id} (graph artifact: {graph_artifact})")
    return {"status": "requeued", "mapping_id": mapping_id,
            "graph_artifact": graph_artifact,
            "review_reason": _REQUEUE_REVIEW_REASON}


_REOPEN_REVIEW_REASON = "re-opened by steward"


def reopen_mapping(mapping_id: str, actor: str = "") -> dict:
    """Move a REJECTED proposal back to pending — the steward's tombstone
    escape hatch (ADR-0053 E6).

    A pending row no longer matches the tombstone predicate (rejected-status
    rows only), so re-opening clears the tombstone: the row itself becomes
    reviewable again and an exact re-proposal classifies ``dup_pending``.
    History stays honest — ``rejected_at`` is kept (a later re-reject simply
    re-stamps it). Mirrors ``requeue_mapping``'s posture: fail-loud on the
    table seam; error dicts only for not-found / not-rejected. Returns the
    updated row."""
    table = _get_table()
    response = table.query(
        KeyConditionExpression="mapping_id = :mid",
        ExpressionAttributeValues={":mid": mapping_id},
    )
    items = response.get("Items", [])
    if not items:
        return {"error": f"Mapping {mapping_id} not found"}
    item = items[0]
    status = item.get("status", "")
    if status != "rejected":
        return {"error": f"Mapping {mapping_id} is not rejected "
                         f"(status={status or 'unknown'}) — only rejected "
                         f"proposals can be re-opened"}
    reopened_at = datetime.now(timezone.utc).isoformat()
    table.update_item(
        Key={"mapping_id": mapping_id, "discovered_at": item["discovered_at"]},
        UpdateExpression=("SET #s = :status, review_reason = :rr, "
                          "reopened_at = :at"
                          + (", reopened_by = :by" if actor else "")),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "pending", ":rr": _REOPEN_REVIEW_REASON,
            ":at": reopened_at,
            **({":by": actor} if actor else {}),
        },
    )
    logger.info(f"Mapping re-opened: {mapping_id} (tombstone cleared)")
    item.update({"status": "pending", "review_reason": _REOPEN_REVIEW_REASON,
                 "reopened_at": reopened_at,
                 **({"reopened_by": actor} if actor else {})})
    return item


@mutates_value_mappings
def reset_all_accepted() -> dict:
    """Remove all user_confirmed nodes from Neptune, reset DynamoDB status to pending.

    This allows the user to "undo" all accepted self-learned mappings and
    return to the onboarded-only graph state. (Bulk admin — swallows.)
    """
    from .onboard import neptune_write as _neptune_write

    try:
        # Find all accepted mappings in DynamoDB
        table = _get_table()
        response = table.query(
            IndexName="status-index",
            KeyConditionExpression="#s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "accepted"},
        )
        accepted_items = response.get("Items", [])

        # Delete Neptune nodes
        for item in accepted_items:
            neptune_node_id = item.get("neptune_node_id")
            if neptune_node_id:
                _neptune_write(
                    "MATCH (vm:ValueMapping {id: $vmid}) DETACH DELETE vm",
                    {"vmid": neptune_node_id}
                )

        # Reset DynamoDB status to pending. Invalidate first so even a partial
        # bulk failure cannot leave the process suppressing from a stale snapshot.
        _reset_accepted_coverage_cache()
        for item in accepted_items:
            table.update_item(
                Key={"mapping_id": item["mapping_id"], "discovered_at": item["discovered_at"]},
                UpdateExpression="SET #s = :status REMOVE accepted_at, neptune_node_id",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":status": "pending"},
            )

        count = len(accepted_items)
        logger.info(f"Reset {count} accepted mappings to pending")
        return {"status": "reset", "count": count}

    except Exception as e:
        logger.error(f"Failed to reset accepted mappings: {e}")
        return public_error(e, "learning.reset_all_accepted")
    finally:
        # Reject any snapshot fetched concurrently before or during mutation.
        _reset_accepted_coverage_cache()


def accept_all_pending() -> dict:
    """Accept all pending mappings at once. (Bulk admin — swallows.)

    ``insight`` items are SKIPPED: an insight is a narrative FINDING, never a
    graph rule — bulk-accepting it would mislabel it as reviewed. (Individual
    ``accept_mapping`` on an insight stays allowed — a harmless no-op node id.)
    """
    try:
        pending = get_pending_mappings()
        accepted_count = 0
        skipped_insights = 0
        errors = []
        for item in pending:
            if item.get("proposed_type") == "insight":
                skipped_insights += 1
                continue  # findings are never bulk-accepted — leave pending
            try:
                result = accept_mapping(item["mapping_id"])
            except Exception as e:  # accept_mapping is fail-loud; isolate bulk runs
                errors.append(f"{item['mapping_id']}: "
                              f"{public_error(e, 'learning.accept_all_pending.item')['error']}")
                continue
            if "error" in result:
                errors.append(f"{item['mapping_id']}: {result['error']}")
            else:
                accepted_count += 1
        logger.info(f"Accept all: {accepted_count} accepted, {len(errors)} errors, "
                    f"{skipped_insights} insight(s) skipped")
        # skipped_insights is ADDITIVE (F8) — the UI surfaces it so a bulk
        # accept that silently left findings pending is visible; the rest of
        # the return shape is unchanged.
        return {"status": "accepted_all", "count": accepted_count,
                "errors": errors[:5], "skipped_insights": skipped_insights}
    except Exception as e:
        logger.error(f"Failed to accept all pending: {e}")
        return public_error(e, "learning.accept_all_pending")


# Accepted proposal types whose acceptance applier IS a ValueMapping node
# (``_apply_to_neptune`` → ``_apply_value_mapping``). The ONE positive
# allowlist for the provision §i replay (``onboard._dp_reapply_self_learned``),
# which re-MERGEs a ValueMapping per item: every other type (guidance notes —
# ADR-0063 prompt text, never graph rules — metric definitions, insights,
# vocab, overlays, correlations, identity/join rules, …) has its own applier
# and must never be force-fit into a ValueMapping. A row with no
# ``proposed_type`` is a legacy value_mapping (the ``_apply_to_neptune``
# default).
VALUE_MAPPING_REPLAY_TYPES = frozenset({"value_mapping", "crosswalk"})


def get_accepted_for_system(system_id: str) -> list[dict]:
    """Get accepted items involving a specific system (read-only — swallows).

    This read is a BROAD candidate list, NOT the replay contract: the
    provision §i replay loop (``onboard._dp_reapply_self_learned``) filters
    every returned item through ``VALUE_MAPPING_REPLAY_TYPES`` — the ONE
    positive allowlist of types re-MERGEd as a ValueMapping node. Consult
    that constant, not this filter, for what replays. ``field_annotation``
    items are additionally EXCLUDED here: force-fitting them into a
    ValueMapping node turned accepted schema annotations into garbage
    mappings and lost the annotation itself on a fresh graph.
    Annotations replay separately through the SAME write acceptance performs
    (``_apply_field_annotation``) — see ``get_accepted_field_annotations`` and
    the onboard provision post-pass.
    """
    try:
        table = _get_table()
        response = table.query(
            IndexName="status-index",
            KeyConditionExpression="#s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "accepted"},
        )
        items = response.get("Items", [])
        # Filter for items involving this system
        return [
            item for item in items
            if (item.get("from_system") == system_id or item.get("to_system") == system_id)
            and item.get("proposed_type", "value_mapping") != "field_annotation"
        ]
    except Exception as e:
        logger.error(f"Failed to get accepted mappings for {system_id}: {e}")
        return []


def get_accepted_field_annotations(system_id: str) -> list[dict]:
    """Accepted ``field_annotation`` items whose annotated field belongs to
    ``system_id``. (Read-only — swallows, per the get_* convention.)

    The provision post-pass replays these through
    ``_apply_field_annotation`` (``SET f.disambiguation`` with the fail-loud
    zero-match probe) — the acceptance write path — never as ValueMapping
    nodes (see ``get_accepted_for_system``).
    """
    try:
        return [
            item for item in _query_by_status("accepted")
            if item.get("proposed_type") == "field_annotation"
            and item.get("from_system") == system_id
        ]
    except Exception as e:
        logger.error(f"Failed to get accepted field annotations for {system_id}: {e}")
        return []


def _curated_annotation_item(sys_id: str, ffield: str, note: str, now: str) -> dict:
    """Build one pre-accepted curated field_annotation record. Deterministic
    ``fa-<digest12>`` id and a FIXED ``discovered_at``, so re-seeding
    overwrites the same (mapping_id, discovered_at) item with identical
    content instead of duplicating. Pure (no I/O)."""
    digest = hashlib.md5(  # nosec B324 - deterministic record id, not a security context
        f"{sys_id}.{ffield}".encode(), usedforsecurity=False,
    ).hexdigest()[:12]
    keys = canonical_keys(
        "field_annotation",
        from_system=sys_id, from_field=ffield, from_value="",
        to_system=sys_id, to_field=ffield, to_value="",
        concept="schema-annotation", annotation=note,
    )
    return {
        "mapping_id": f"fa-{digest}",
        "discovered_at": "2026-07-20T00:00:00+00:00",
        "status": "accepted",
        "proposed_type": "field_annotation",
        "from_system": sys_id, "from_field": ffield,
        "to_system": sys_id, "to_field": ffield,
        "from_value": "", "to_value": "",
        "concept": "schema-annotation",
        "confidence": "0.95",
        "query_context": note,
        "subject_key": keys.subject_key,
        "target_key": keys.target_key,
        "source": "curated-seed",
        "version": "1",
        "neptune_node_id": f"field-{sys_id}-{ffield.replace('.', '-')}",
        "accepted_at": now,
    }


def seed_preaccepted_annotations(annotations: dict) -> int:
    """Idempotently write ``annotations`` into the store as pre-accepted
    ``field_annotation`` records. MECHANISM ONLY — the content arrives as a
    parameter (``{system_id: {"table.field": annotation_text}}``); this module
    carries ZERO annotation text (project constraint: demo content never lives
    in engine files — the demo dict is in ``agents.demo.curated_annotations``).

    Records carry the standard lifecycle keys (subject/target key via
    ``canonical_keys``) plus ``source='curated-seed'`` and
    ``concept='schema-annotation'``; the standard replay path applies them to
    Neptune. Fail-loud (the ``_put_item`` seam raises) — the provision call
    site owns the non-fatal swallow. Returns the number of records written.
    """
    table = _get_table()
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for sys_id, fields in (annotations or {}).items():
        for ffield, note in (fields or {}).items():
            _put_item(table, _curated_annotation_item(sys_id, ffield, note, now))
            count += 1
    if count:
        logger.info("Seeded %d curated field annotation(s) into %s", count, TABLE_NAME)
    return count


def get_accepted_rules_by_node_ids(node_ids: set[str]) -> list[dict]:
    """Return accepted rows whose ``neptune_node_id`` is in ``node_ids``.

    ONE paginated status-index GSI query (via ``_query_by_status`` — never
    per-id), filtered in-process. Feeds BOTH the human rule labels in the
    intelligence summary AND — threaded back through
    ``record_rule_usage(..., prefetched_rows=...)`` — the usage bump, so the
    runtime pays for the GSI query once per turn.

    Best-effort / SWALLOWS (read-only bulk category, ADR-0003 F): ``[]`` on
    empty input or any backend failure.
    """
    if not node_ids:
        return []
    try:
        return [
            item for item in _query_by_status("accepted")
            if item.get("neptune_node_id") in node_ids
        ]
    except Exception as e:
        logger.warning(f"get_accepted_rules_by_node_ids failed (non-critical): {e}")
        return []


def record_rule_usage(node_ids: set[str], *,
                      prefetched_rows: list[dict] | None = None) -> int:
    """Bump the usage counter on accepted rules whose applied node was used.

    Item C (utility loop): the runtime post-run hook extracts applied-node ids
    (``usage_tracking.extract_rule_node_ids``) from a turn's tool results and
    calls this to record that the orchestrator actually USED those saved
    rules. Per matched accepted row:

        ``ADD usage_count :one SET last_used_at = :now``

    — an atomic counter (``ADD`` is absent-safe: first use creates the
    attribute at 1). Surfaced read-only in the review UI so unused rules can
    be retired.

    ``prefetched_rows``: when the caller already fetched the accepted rows
    for this turn (they MUST come from ``get_accepted_rules_by_node_ids`` on
    the same ids), pass them here to SKIP the GSI query — only those rows are
    bumped (still filtered by ``neptune_node_id in node_ids`` defensively).
    ``None`` → existing behavior: this function runs the query itself.

    Best-effort / SWALLOWS (read-only-adjacent bulk category, ADR-0003 F):
    a metrics bump must never fail a live query. Bounded: the status-index
    GSI query PAGINATES on ``LastEvaluatedKey`` (via ``_query_by_status`` —
    defensive cap of 20 pages, never spins; a single DynamoDB page is capped
    at 1 MB, so an accepted rule past page 1 would otherwise never get its
    bump) + one update_item per matched id.
    Returns the number of rows bumped (0 on any backend failure).
    """
    if not node_ids:
        return 0
    try:
        items = (prefetched_rows if prefetched_rows is not None
                 else _query_by_status("accepted"))
        table = _get_table()
        now = datetime.now(timezone.utc).isoformat()
        bumped = 0
        for item in items:
            if item.get("neptune_node_id") not in node_ids:
                continue
            table.update_item(
                Key={"mapping_id": item["mapping_id"],
                     "discovered_at": item["discovered_at"]},
                UpdateExpression="ADD usage_count :one SET last_used_at = :now",
                ExpressionAttributeValues={":one": 1, ":now": now},
            )
            bumped += 1
        if bumped:
            logger.info(f"Rule usage recorded on {bumped} accepted rule(s)")
        return bumped
    except Exception as e:
        logger.warning(f"record_rule_usage failed (non-critical): {e}")
        return 0


# --------------------------------------------------------------------------- #
# LEARN loop: accumulate recurring high-confidence discoveries into a system's
# PERSISTED query_guidance (Neptune System node). The orchestrator reads
# query_guidance at query time, so promoted learnings become durable, system-level
# guidance that grows over time. Generic — no dataset knowledge.
# --------------------------------------------------------------------------- #
_MAX_GUIDANCE_LEN = 4000
_LEARNED_PREFIX = _qi_learned_prefix


def guidance_budget_status(guidance: str, *,
                           max_len: int = _MAX_GUIDANCE_LEN) -> dict:
    """Return honest, read-only budget facts; never truncate or drop notes.

    ``length`` is what the cap counts: the notes, not the generic base
    (``guidance_scope.notes_length``). ``stored_length`` is the whole
    stored document."""
    from .guidance_scope import notes_length
    text = str(guidance or "")
    length = notes_length(text)
    over_by = max(0, length - max_len)
    return {
        "length": length,
        "stored_length": len(text),
        "max_len": max_len,
        "over_budget": over_by > 0,
        "over_by": over_by,
    }

# The compact suffix a consolidated negative-column note carries — generic OT/MES
# guidance phrasing (not a dataset literal). Kept short so a consolidated note stays
# far smaller than the many single "no column '<X>'" notes it replaces.
_CONCEPT_MAPPED_SUFFIX = "use concept-mapped columns"

# Generic patterns identifying a NEGATIVE-COLUMN learning (no dataset literals — the
# English phrasing is the only literal). Two accepted forms are parsed so
# consolidation is idempotent + accumulative:
#   * the single-column form written at discovery time:
#       "<table>: no column '<col>' — use the exact concept-mapped columns ..."
#   * the consolidated form this module emits:
#       "<table>: avoid columns [X, Y, Z]; use concept-mapped columns"
_NEG_COLUMN_RE = _re_rc.compile(
    r"^(?P<key>.+?):\s*no column\s+['\"]?(?P<col>[^'\"\s]+)['\"]?", _re_rc.IGNORECASE
)
_AVOID_COLUMNS_RE = _re_rc.compile(
    r"^(?P<key>.+?):\s*avoid columns\s*\[(?P<cols>[^\]]*)\]", _re_rc.IGNORECASE
)


def _split_guidance(guidance: str) -> tuple[str, list[str]]:
    """Compatibility view of structured or legacy guidance as base + notes."""
    from .guidance_scope import parse_document, scoped_entries
    document = parse_document(guidance)
    return document["base"], [text for _scope, text in scoped_entries(document)]


def _assemble_guidance(base: str, notes: list[str]) -> str:
    """Rebuild a ``query_guidance`` string from ``base`` + learned ``notes``. Pure.

    Matches the exact on-write format of :func:`append_query_guidance_note`
    (``"<base> LEARNED: n1 LEARNED: n2"``), so a round-trip through
    :func:`_split_guidance`/:func:`_assemble_guidance` is stable.
    """
    additions = " ".join(f"{_LEARNED_PREFIX}{n}" for n in notes)
    if base and additions:
        return f"{base} {additions}".strip()
    return (base or additions).strip()


def _parse_negative_columns(note: str) -> tuple[str, list[str]] | None:
    """If ``note`` is a negative-column learning, return ``(key, [columns])``; else None.

    Recognizes BOTH the single "no column '<col>'" discovery form and the already
    consolidated "avoid columns [..]" form, so re-consolidation is idempotent and
    later negatives on the same key accumulate. Pure; generic (no dataset literals).
    """
    m = _AVOID_COLUMNS_RE.match(note)
    if m:
        cols = [c.strip() for c in m.group("cols").split(",") if c.strip()]
        return m.group("key").strip(), cols
    m = _NEG_COLUMN_RE.match(note)
    if m:
        return m.group("key").strip(), [m.group("col").strip()]
    return None


def _format_avoid_note(key: str, cols: list[str]) -> str:
    """Render the compact consolidated negative-column note. Pure, generic."""
    return f"{key}: avoid columns [{', '.join(cols)}]; {_CONCEPT_MAPPED_SUFFIX}"


def consolidate_guidance_notes(notes: list[str]) -> list[str]:
    """Consolidate a list of learned notes into a compact, deterministic list. Pure.

    Repetitive negative-column notes for the SAME key (``<table>: no column 'X'``,
    ``<table>: no column 'Y'`` …) collapse into ONE
    ``<table>: avoid columns [X, Y]; use concept-mapped columns`` note, positioned
    at that key's first appearance; every other note passes through in order with
    exact duplicates dropped. Idempotent: consolidating an already-consolidated list
    yields the same list (the emitted "avoid columns [..]" form re-parses to the same
    key/columns). Generic — carries no dataset knowledge.
    """
    avoid: dict[str, dict[str, None]] = {}   # key -> ordered, deduped columns
    slots: list[tuple[str, str]] = []        # ("avoid", key) | ("note", text)
    seen_keys: set[str] = set()
    for note in notes:
        clean = (note or "").strip()
        if not clean:
            continue
        parsed = _parse_negative_columns(clean)
        if parsed:
            key, cols = parsed
            bucket = avoid.setdefault(key, {})
            for c in cols:
                bucket[c] = None
            if key not in seen_keys:
                seen_keys.add(key)
                slots.append(("avoid", key))
        else:
            slots.append(("note", clean))
    out: list[str] = []
    seen_out: set[str] = set()
    for kind, val in slots:
        text = _format_avoid_note(val, list(avoid[val].keys())) if kind == "avoid" else val
        if text not in seen_out:
            seen_out.add(text)
            out.append(text)
    return out


def evolve_query_guidance(current: str, new_note: str, *,
                          max_len: int = _MAX_GUIDANCE_LEN) -> str | None:
    """Fit ``current`` guidance + ``new_note`` within ``max_len`` by EVOLVING it. Pure.

    Instead of silently dropping ``new_note`` when the cap is hit, this (a) appends
    the note, (b) consolidates repetitive negative-column notes into compact form,
    and (c) prunes the OLDEST notes (front of the list) until the result fits — the
    freshly-added note is last, so it survives pruning. Deterministic and bounded.

    Returns the evolved guidance string, or ``None`` when even the base guidance
    with no learned notes still exceeds ``max_len`` (nothing can be accumulated).
    """
    base, notes = _split_guidance(current or "")
    clean = (new_note or "").strip()
    combined = notes + ([clean] if clean else [])
    consolidated = consolidate_guidance_notes(combined)
    candidate = _assemble_guidance(base, consolidated)
    while len(candidate) > max_len and consolidated:
        consolidated = consolidated[1:]   # drop the oldest note (front)
        candidate = _assemble_guidance(base, consolidated)
    if len(candidate) > max_len:
        return None   # base alone exceeds the cap — cannot fit any learning
    return candidate


def guidance_notes_evicted_by(current: str, new_note: str, *,
                             max_len: int = _MAX_GUIDANCE_LEN) -> list[str]:
    """Which EXISTING learned notes would be pruned to fit ``new_note``. Pure.

    :func:`evolve_query_guidance` prunes oldest-first to stay under the cap.
    That is deliberate and bounded, but it must not be SILENT: without this
    preview a steward accepts a new answer requirement, gets
    ``{"status": "accepted"}`` back, and an earlier requirement stops reaching
    the agent with no signal anywhere (measured: a 657-char requirement
    evicting an earlier accepted one regressed the golden it protected from
    3/3 passing to 3/3 failing in the same run; re-filed at 233 chars, both
    fit — and the terser note scored BETTER).

    Returns the evicted notes in eviction order (oldest first); ``[]`` when
    the new note fits with no loss. Compares the note LISTS rather than
    string lengths so consolidation is not mistaken for eviction.
    """
    if not (new_note or "").strip():
        return []
    _, before = _split_guidance(current or "")
    evolved = evolve_query_guidance(current, new_note, max_len=max_len)
    if evolved is None:
        return list(before)
    _, after = _split_guidance(evolved)
    kept = set(after)
    return [n for n in before if n not in kept]


def _file_guidance_proposal(system_id: str, note: str,
                            guidance_scope: str) -> str:
    """File one machine-authored pending candidate; never deliver it."""
    try:
        mapping_id = log_proposed_mapping(
            from_system=system_id, from_field="", from_value="",
            to_system=system_id, to_field="", to_value=note,
            concept="", confidence=0.5,
            query_context="Automated guidance candidate.",
            proposed_type="guidance_note", source="agent",
            provenance="automated-guidance-writer",
            guidance_scope=guidance_scope)
    except Exception as exc:  # noqa: BLE001 - caller remains best-effort
        logger.warning("automated guidance proposal filing failed for %s: %s",
                       system_id, type(exc).__name__)
        return "not_filed"
    if not mapping_id:
        return "not_filed"
    try:
        row = get_mapping(mapping_id)
    except Exception as exc:  # noqa: BLE001 - never claim filing on doubt
        logger.warning("automated guidance proposal readback failed for %s: %s",
                       system_id, type(exc).__name__)
        return "not_filed"
    return ("filed" if row and row.get("status") == "pending"
            and row.get("proposed_type") == "guidance_note" else "not_filed")


def append_query_guidance_note(
    system_id: str, note: str, *, max_len: int = _MAX_GUIDANCE_LEN,
    guidance_scope: str = "query_hint",
) -> str:
    """File an automated guidance candidate through the proposal lifecycle."""
    clean = str(note or "").strip()
    if not str(system_id or "").strip() or not clean or len(clean) > max_len:
        return "not_filed"
    return _file_guidance_proposal(
        str(system_id).strip(), clean, guidance_scope)


def _backfill_entries(entry: dict) -> list[tuple[str, str]]:
    groups = (
        ("query_hint", entry.get("query_hints")),
        ("answer_requirement", entry.get("answer_requirements")),
        ("legacy_unscoped", entry.get("legacy_unscoped")),
    )
    return [
        (scope, note.strip())
        for scope, values in groups if isinstance(values, list)
        for note in values if isinstance(note, str) and note.strip()
    ]


def backfill_agent_guidance_mirror() -> dict:
    """File existing graph-only guidance as pending scoped proposals."""
    result = {"filed": 0, "not_filed": 0}
    for entry in get_all_query_guidance():
        if not isinstance(entry, dict):
            continue
        system_id = str(entry.get("system_id") or "").strip()
        if not system_id:
            continue
        for scope, note in _backfill_entries(entry):
            outcome = append_query_guidance_note(
                system_id, note, guidance_scope=scope)
            result[outcome if outcome == "filed" else "not_filed"] += 1
    logger.info("guidance proposal backfill filed=%d not_filed=%d",
                result["filed"], result["not_filed"])
    return result


def _persist_query_guidance(system_id: str, guidance: str) -> None:
    """Write a system's accumulated ``query_guidance`` to its System node."""
    _neptune_write(
        "MERGE (s:System {id: $sid}) SET s.query_guidance = $qg",
        {"sid": system_id, "qg": guidance},
    )


def mirror_learned_notes_to_agent(system_id: str, guidance: str) -> bool:
    """Mirror a system's LEARNED notes into the per-system AGENT guidance store.

    Closes a consumption gap, not a cosmetic one. ``query_guidance`` lives on the
    Neptune System node and is read by the ORCHESTRATOR (its route seed); the
    per-system agent reads a DIFFERENT store (``system_guidance``, ADR-0035
    mechanism i′). That store shipped with a reader and no writer, so an accepted
    ``guidance_note`` never reached the agent that actually queries the system —
    the agent holding the coded values was structurally unable to learn what they
    mean. Observed live 2026-08-04: a defect-code→equipment note was present on
    the orchestrator surface while the system agent's answer carried raw
    identifiers only.

    Mirrors the LEARNED notes ONLY — the base guidance already reaches the agent
    through its connection-type prompt, so re-sending it would duplicate tokens.
    Uses the SAME accumulated text just written to the graph (no second read, no
    divergent accumulation logic), so the two surfaces cannot drift.

    Best-effort by category (like the append itself): a mirror failure is logged
    and swallowed so a learning write never breaks the caller's query. Writing
    bumps the store's persisted version, so agents pick the note up on their next
    read — no runtime roll, no re-onboard.
    """
    if not system_id:
        return False
    from .guidance_scope import delivery_document, scoped_entries
    delivery = delivery_document(guidance or "")
    notes = scoped_entries(delivery, plain_is_base=False)
    if not notes:
        return False
    try:
        from .system_guidance import write as _write_agent_guidance
        _write_agent_guidance(system_id, delivery)
    except Exception as e:  # noqa: BLE001 - best-effort mirror, never breaks the append
        logger.warning(f"agent guidance mirror failed for {system_id}: {e}")
        return False
    logger.info(f"Mirrored {len(notes)} learned note(s) to the {system_id} agent")
    return True


# --------------------------------------------------------------------------- #
# Guidance transparency / management (mirrors the proposed-mapping admin surface).
# The GET list is read-only (swallows, like get_all_mappings); the prune/clear
# mutations are scoped, single-node writes and are FAIL-LOUD (errors propagate),
# returning ``{"error": ...}`` only for the genuine not-found cases.
# --------------------------------------------------------------------------- #
def get_all_query_guidance(*, strict: bool = False) -> list[dict]:
    """List every System node's ``query_guidance``, parsed into base + LEARNED notes.

    Read-only (swallows backend errors → ``[]`` by default, like
    ``get_all_mappings``; ``strict=True`` lets health distinguish unavailable
    from a measured empty set). Each entry: ``{system_id, base, notes,
    learned_count, length, max_len, over_budget, over_by}``. Generic — no
    dataset knowledge; the notes are whatever accumulated on the graph. Overage
    is annotation-only and never truncates or drops steward-owned notes.
    """
    try:
        rows = _neptune_query(
            "MATCH (s:System) WHERE s.query_guidance IS NOT NULL "
            "RETURN s.id AS id, s.query_guidance AS qg ORDER BY s.id",
            {},
        )
    except Exception as e:  # noqa: BLE001 - read-only, bounded blast radius
        logger.error(f"Failed to read query_guidance: {e}")
        if strict:
            raise
        return []
    out: list[dict] = []
    for r in rows or []:
        sid = r.get("id") or ""
        qg = r.get("qg") or ""
        if not sid:
            continue
        from .guidance_scope import composition_metadata, parse_document
        document = parse_document(qg)
        notes = [*document["query_hints"], *document["answer_requirements"],
                 *document["legacy_unscoped"]]
        budget = guidance_budget_status(qg)
        out.append({
            "system_id": sid, "base": document["base"], "notes": notes,
            "query_hints": document["query_hints"],
            "answer_requirements": document["answer_requirements"],
            "legacy_unscoped": document["legacy_unscoped"],
            "scope_counts": composition_metadata(qg)["scope_counts"],
            "learned_count": len(notes), **budget,
        })
    return out


def _replace_query_guidance(system_id: str, current: str,
                            updated: str) -> dict:
    from .guidance_saga import replace_exact
    from .system_guidance import restore, snapshot
    try:
        before = snapshot(system_id)
    except Exception:
        from .guidance_saga import pending_result
        return pending_result(delivery_drift=True)
    return replace_exact(
        graph_before=current, graph_after=updated, delivery_before=before,
        graph_cas=lambda expected, desired: _guidance_graph_compare_and_set(
            system_id, expected, desired),
        graph_read=lambda: _read_query_guidance_strict(system_id),
        delivery_cas=lambda expected, target: restore(
            system_id, expected, target),
        delivery_read=lambda: snapshot(system_id))


def prune_query_guidance_note(system_id: str, note: str) -> dict:
    """Remove one exact note only when graph and delivery replace together."""
    clean = str(note or "").strip()
    if not system_id or not clean:
        return {"error": "system_id and note are required"}
    current = _read_query_guidance_strict(system_id)
    if current is None:
        return {"error": f"System {system_id} not found"}
    from .guidance_scope import scoped_entries, without_note
    updated, removed = without_note(current, clean)
    if not removed:
        return {"error": "note not found", "removed": 0}
    result = _replace_query_guidance(system_id, current, updated)
    if result.get("status") != "synchronized":
        return result
    remaining = len(scoped_entries(updated))
    logger.info("Pruned %d guidance note(s) from %s", removed, system_id)
    return {"status": "pruned", "removed": removed, "remaining": remaining}


def clear_query_guidance(system_id: str) -> dict:
    """Clear learned notes only after exact graph and delivery readbacks."""
    if not system_id:
        return {"error": "system_id is required"}
    current = _read_query_guidance_strict(system_id)
    if current is None:
        return {"error": f"System {system_id} not found"}
    from .guidance_scope import (
        empty_document, parse_document, scoped_entries, serialize_document)
    document = parse_document(current)
    removed = len(scoped_entries(document))
    updated = serialize_document(empty_document(document["base"]))
    result = _replace_query_guidance(system_id, current, updated)
    if result.get("status") != "synchronized":
        return result
    logger.info("Cleared %d guidance note(s) from %s", removed, system_id)
    return {"status": "cleared", "removed": removed}


def clear_rejected() -> dict:
    """Delete all rejected mappings from DynamoDB. (Bulk admin — swallows.)"""
    try:
        table = _get_table()
        response = table.query(
            IndexName="status-index",
            KeyConditionExpression="#s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "rejected"},
        )
        items = response.get("Items", [])
        for item in items:
            table.delete_item(Key={"mapping_id": item["mapping_id"], "discovered_at": item["discovered_at"]})
        count = len(items)
        logger.info(f"Cleared {count} rejected mappings")
        return {"status": "cleared", "count": count}
    except Exception as e:
        logger.error(f"Failed to clear rejected mappings: {e}")
        return public_error(e, "learning.clear_rejected")


def reconcile_blocked_proposals() -> dict:
    """Idempotently purge pending proposals whose schema_verdict is definitively not_found.

    This is the cleanup counterpart to the upstream ingestion gate: it deletes only
    ``pending`` (and ``superseded``) items whose referenced ``table.column``
    DEFINITIVELY does not exist in the onboarded schema. It NEVER touches
    ``accepted`` items (live graph rules).

    The check mirrors ``proposal_missing_field`` from schema_check.py: it returns
    the first definitive missing reference (False verdict) for a proposal, or
    None if all checkable endpoints exist or are indeterminate. Only proposals
    with a definitive not_found verdict are deleted.

    Returns ``{"deleted": int, "checked": int}`` on success, or ``{"error": str}``
    on failure. Safe to re-run (idempotent).
    """
    try:
        from .schema_check import (
            proposal_missing_field,
            SCHEMA_CHECKED_TYPES,
            load_system_schema,
        )

        table = _get_table()
        checked = 0
        deleted = 0

        # Gather all reviewable items FIRST (paginated), so we can preload each
        # DISTINCT system's schema exactly once and verify every item against that
        # cache. Calling proposal_missing_field per item WITHOUT a cache re-loads the
        # graph schema on every field — an N+1 over the whole backlog that times the
        # request out once the queue grows to hundreds of items (the reconcile-blocked
        # 500/timeout bug).
        all_items: list[dict] = []
        for status in ("pending", "superseded"):
            last_key = None
            while True:
                kwargs = {
                    "IndexName": "status-index",
                    "KeyConditionExpression": "#s = :status",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":status": status},
                    "ScanIndexForward": False,
                }
                if last_key:
                    kwargs["ExclusiveStartKey"] = last_key
                response = table.query(**kwargs)
                all_items.extend(response.get("Items", []))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break

        # Preload the schema of every distinct system referenced by a checkable
        # proposal, ONCE (bounded by the number of onboarded systems, not the queue
        # size); all field checks then resolve purely against this cache.
        sys_ids: set[str] = set()
        for item in all_items:
            if item.get("proposed_type", "") in SCHEMA_CHECKED_TYPES:
                for key in ("from_system", "to_system"):
                    sid = item.get(key, "")
                    if sid:
                        sys_ids.add(sid)
        schemas = {sid: load_system_schema(sid) for sid in sys_ids}

        for item in all_items:
            checked += 1
            proposed_type = item.get("proposed_type", "")

            # Only check SCHEMA_CHECKED_TYPES - other types don't have
            # column references we can verify
            if proposed_type not in SCHEMA_CHECKED_TYPES:
                continue

            # Check both from and to endpoints against the preloaded schema cache
            missing = proposal_missing_field(
                proposed_type,
                item.get("from_system", ""),
                item.get("from_field", ""),
                item.get("to_system", ""),
                item.get("to_field", ""),
                schemas=schemas,
            )

            # Only delete if there's a DEFINITIVE miss (not None/indeterminate)
            if missing:
                try:
                    table.delete_item(
                        Key={
                            "mapping_id": item["mapping_id"],
                            "discovered_at": item["discovered_at"],
                        }
                    )
                    deleted += 1
                    logger.info(
                        f"Deleted {proposed_type} proposal {item['mapping_id']} "
                        f"with missing reference: {missing}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to delete proposal {item['mapping_id']}: {e}"
                    )

        logger.info(f"Reconcile blocked: checked {checked}, deleted {deleted}")
        return {"deleted": deleted, "checked": checked}

    except Exception as e:
        logger.error(f"Failed to reconcile blocked proposals: {e}")
        return public_error(e, "learning.reconcile_blocked")


@mutates_value_mappings
def clear_all_learnings() -> dict:
    """Delete ALL mappings from DynamoDB and remove all user_confirmed nodes from Neptune.

    (Bulk admin — swallows. Also the documented one-time reset path for legacy
    items that predate subject_key.)
    """
    from .onboard import neptune_write as _neptune_write
    try:
        table = _get_table()
        # First remove Neptune nodes for accepted mappings
        response = table.query(
            IndexName="status-index",
            KeyConditionExpression="#s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "accepted"},
        )
        accepted_items = response.get("Items", [])
        for item in accepted_items:
            neptune_node_id = item.get("neptune_node_id")
            if neptune_node_id:
                # Try all node types
                for label in ["JoinPath", "ValueMapping", "IdentityRule"]:
                    _neptune_write(f"MATCH (n:{label} {{id: $nid}}) DETACH DELETE n", {"nid": neptune_node_id})

        # Now delete ALL items from DynamoDB (scan + batch delete)
        scan_response = table.scan()
        all_items = scan_response.get("Items", [])
        # Invalidate before mutation so a partial clear failure still forces the
        # next ingestion to read the accepted set that actually remains.
        _reset_accepted_coverage_cache()
        for item in all_items:
            table.delete_item(Key={"mapping_id": item["mapping_id"], "discovered_at": item["discovered_at"]})

        count = len(all_items)
        logger.info(f"Cleared all {count} learnings (DynamoDB + Neptune)")
        return {"status": "cleared_all", "count": count}
    except Exception as e:
        logger.error(f"Failed to clear all learnings: {e}")
        return public_error(e, "learning.clear_all")
    finally:
        # Reject any snapshot fetched concurrently before or during mutation.
        _reset_accepted_coverage_cache()
