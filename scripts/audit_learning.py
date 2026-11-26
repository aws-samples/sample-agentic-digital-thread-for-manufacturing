#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
audit_learning.py — the post-deploy learning-health gate (8 probes).

Sibling of smoke_prompts.py — COMPLEMENTS it, never replaces it. Where the
smoke gate proves the deployed env answers grounded questions, this gate
audits the LEARNING artifacts the env accumulated: graph rules, join paths,
proposals, and the usage ledger. The Neptune graph is VPC-private (ADR-0024),
so like smoke_prompts.py this is a pure HTTP client against the deployed app:

  GET /api/thread/graph-summary        rules / join_paths_detail / authority_rules_detail
  GET /api/proposed-mappings?include_expired=true
                                       proposal rows, every lifecycle status
                                       (staged# rows are filtered defensively)
  GET /api/admin/learning/health       integrity mode, usage ledger, staging census,
                                       AuthorityRule field-evidence integrity
                                       (contract pinned by the steward-transparency
                                       plan / ADR-0052; optional — {} when absent)

The FETCH layer is strictly separated from the PROBES, which are PURE
functions over the parsed JSON — that is what makes them offline
unit-testable (tests/unit/test_audit_learning_probes.py).

Probes and exit-code contract (exit 0 = pass):
  1 duplicate-content census   GATES (default): >0 duplicate groups fails.
       Join paths are keyed (from_system, from_field, to_system, to_field)
       with the TABLE-QUALIFIED from_field/to_field the API returns — NEVER
       concept-coarse/leaf keys (a coarse probe once reported 17 FAKE dup
       groups that were distinct table-qualified joins; regression-pinned).
  2 id-shape audit             GATES (default): live ids must be digest-shaped
       ^(ir|jp)-.+-[0-9a-f]{16}$ or steward ^subj- ; legacy index-keyed ids
       (e.g. ir-<sys>-14) fail.
  3 forbidden-pairing scan     GATES (default): identifier→measure endpoints,
       stream-suffix value names (_DeviceState/_Alarm/_oee...), and
       site⊃equipment grain pairings over LIVE rules. Shape checks are
       re-implemented here purely over the JSON; semantics follow
       agents/join_discovery.is_identifier_like + the W3 onboarding gate.
  4 auto-accept-method audit   GATES (default): accepted rows with
       method=="hierarchy_position" must be 0; accepted UUID-shaped-target
       rows must carry verified=="true".
  5 cross-site probe           GATES only with --strict: pending same-class
       cross-system proposals must state WHY they passed — a review_reason
       naming the vouching authority id (post-B4 contract). Rows lacking the
       reason but matching a live All-Sites authority by concept are noted as
       LEGACY (pre-B4) and do not gate.
  6 quarantine + pending-age   NEVER gates (trend data, report-only).
  7 usage-ledger liveness      GATES only with --strict: 0 rows in the window
       fails UNLESS the env reports integrity_mode=="report-only", where 0
       rows is the HONEST expected-gap (report-only turns retain no verified
       relationship proofs), reported as such — not a failure.
  8 unresolved-drift age       GATES only with --strict: pending
       proposed_type=="schema_drift" rows older than --drift-max-age-days
       (default 7) fail. An env with ZERO schema_drift rows (any status)
       reports "unavailable" and NEVER gates (the probe-7 idiom — a
       pre-drift-feature env is indistinguishable from a zero-drift env,
       and neither is a failure). Rows are keyed by mapping_id ONLY — no
       shape/site inference (the 3 pinned false-alarm lessons above all
       came from inferring content the API does not carry).

Usage:
  python3 scripts/audit_learning.py --base-url https://<dist>.cloudfront.net \
      --password "$ADT_PASSWORD" [--strict] [--report /tmp/audit.json] \
      [--drift-max-age-days 7]

Env fallbacks: ADT_BASE_URL, ADT_USERNAME (default ADT), ADT_PASSWORD,
AUDIT_REPORT, AUDIT_STRICT.
Exit 0 = gate passed; 1 = gate failed; 2 = usage/login/fetch error.
"""
import argparse
import http.client
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# One transient-network retry per GET (same NARROW posture as smoke_prompts:
# HTTP error statuses are the backend answering and are NEVER retried).
TRANSIENT_NETWORK_ERRORS = (
    ssl.SSLError,
    http.client.HTTPException,
    ConnectionError,
    TimeoutError,
)
FETCH_RETRY_BACKOFF_SECONDS = 3.0

# Probe 2 — live artifact id shapes: content digests or steward subjects.
DIGEST_ID_RE = re.compile(r"^(ir|jp)-.+-[0-9a-f]{16}$")
STEWARD_ID_RE = re.compile(r"^subj-")
# Sanctioned STRUCTURAL id families: intra-system JoinPaths minted by the
# b3/b4/b5 writers carry deterministic table-path ids by design (replace-
# not-accrete scoped by _AUTO_INTERNAL_JOINPATH_DELETE_CYPHER) — they are
# NOT the W1 digest scope and must not be flagged as legacy (live false
# alarm 2026-07-25: 176 flags, all structural).
STRUCTURAL_ID_RE = re.compile(r"^jp-(internal|vo|samerow)-")
# Probe 4 — canonical UUID (the row-level target shape auto-accept verifies).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Probe 5 — an AuthorityRule id cited inside a review_reason.
_AUTHORITY_ID_RE = re.compile(r"\bar-[a-z0-9][a-z0-9_-]*", re.IGNORECASE)

# Probe 3 vocabulary — GENERIC shape tokens (never dataset column names),
# ported from the W3 onboarding writer gate (onboard._is_valid_identity_rule)
# and join_discovery's shared token lists. Re-implemented here so the probe
# stays a pure stdlib function over JSON; semantics source:
PROBE3_SEMANTICS = ("agents/join_discovery.is_identifier_like + "
                    "agents/onboard._is_valid_identity_rule (W3)")
_MEASURE_NAME_TOKENS = ("count", "quantity", "qty", "rate", "size", "total",
                        "sum", "avg", "amount", "pct", "percent")
_MEASURE_UNIT_TOKENS = ("duration", "elapsed", "minutes", "seconds", "hours",
                        "days", "ms")
_IDENTIFIER_NAME_TOKENS = ("id", "code", "key", "name", "no", "num", "ref",
                           "sku", "serial", "tag", "cell", "site", "line",
                           "station")
_STREAM_VALUE_SUFFIX_TOKENS = ("state", "alarm", "status", "fault", "oee",
                               "metric", "setpoint")
_SITE_GRAIN_TOKENS = ("site", "plant", "facility")
_EQUIPMENT_GRAIN_TOKENS = ("equipment", "machine", "asset", "station")

# Probe 5 — the P8-guarded proposal types (self_learning._CROSS_SITE_GUARDED_TYPES).
_CROSS_SITE_GUARDED_TYPES = ("value_mapping", "join_path", "identity_rule",
                             "crosswalk")
# Staging rows are evidence accumulation, not lifecycle rows (ADR-0051 P5) —
# the API already filters them, but the probes exclude them DEFENSIVELY too.
_STAGED_PREFIX = "staged#"


# ── Pure text helpers ────────────────────────────────────────────────────────

def _camel_split(name: str) -> str:
    """Insert ``_`` at lower→Upper camelCase boundaries (``DeviceState`` →
    ``Device_State``) — same posture as join_discovery.camel_split."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name or "")


def _norm(value) -> str:
    """Lowercase + strip; None → '' (proposal_key._norm posture)."""
    return ("" if value is None else str(value)).strip().lower()


def _leaf(field) -> str:
    """Last dotted segment of a field path (the bare column name)."""
    return str(field or "").split(".")[-1]


def _leaf_tokens(field) -> set:
    """Lowercase word tokens of a field's leaf; camelCase split BEFORE lowering."""
    leaf = _camel_split(_leaf(field))
    return {t for t in re.split(r"[^a-z0-9]+", leaf.lower()) if t}


# ── Pure input normalizers ───────────────────────────────────────────────────

def live_rules(summary: dict) -> list:
    """Identity rules still in routing — graph-summary keeps deactivated rules
    for audit (pinned behavior); every probe over rules skips them."""
    return [r for r in (summary or {}).get("rules", []) or []
            if _norm(r.get("status")) != "deactivated"]


def live_join_paths(summary: dict) -> list:
    """Join paths from the summary (no lifecycle today; filter defensively)."""
    return [j for j in (summary or {}).get("join_paths_detail", []) or []
            if _norm(j.get("status")) != "deactivated"]


def live_mappings(proposals: dict) -> list:
    """Proposal rows minus staging-buffer rows (staged# mapping_id prefix —
    status-free evidence rows that must NEVER count anywhere here)."""
    return [m for m in (proposals or {}).get("mappings", []) or []
            if not str(m.get("mapping_id") or "").startswith(_STAGED_PREFIX)]


def _pending(mappings: list) -> list:
    return [m for m in mappings if _norm(m.get("status")) == "pending"]


# ── Probe 1 — duplicate-content census ───────────────────────────────────────

def _dup_groups(items: list, key_fn) -> list:
    """Groups (>1 member) of items sharing key_fn(item). Pure."""
    groups: dict = {}
    for item in items:
        groups.setdefault(key_fn(item), []).append(str(item.get("id") or ""))
    return [{"key": list(key), "ids": ids}
            for key, ids in sorted(groups.items())
            if len(ids) > 1]


def _joinpath_dup_key(j: dict) -> tuple:
    """Content key for the join-path duplicate census.

    Structural intra-system rows (jp-internal/vo/samerow) carry their TABLE
    path only in the id — the summary payload exposes leaf-level fields, so
    a field-tuple key collapses distinct table joins into fake duplicates
    (live false alarm 2026-07-25: 17 groups, all structural, e.g. cmms
    work_orders_raw.asset_id vs maintenance_schedules_raw.asset_id). They
    are MERGE-keyed by id, so the id IS their content key. Digest/steward
    rows keep the table-qualified field tuple — that is where the real
    duplicate-content defect class lived (24 groups pre-e6cd22c)."""
    jid = str(j.get("id") or "")
    if STRUCTURAL_ID_RE.match(jid):
        return ("structural", jid)
    return (_norm(j.get("from_system")), _qualified(j, "from"),
            _norm(j.get("to_system")), _qualified(j, "to"))


def _qualified(j: dict, side: str) -> str:
    """``table.field`` when the summary carries the table (steward joins store
    it separately — live false alarm 2026-09-27: mes-mil work_orders vs
    quality_checks ``work_order_id`` -> erp flagged as one group), else the
    field as-is (older summaries without ``*_table`` keep today's key)."""
    field = _norm(j.get(f"{side}_field"))
    table = _norm(j.get(f"{side}_table"))
    return f"{table}.{field}" if table and "." not in field else field


def _all_distinct_digests(ids: list) -> bool:
    """True when every member is digest-shaped with a distinct id. W1 digest
    ids ARE content hashes (MERGE-keyed): distinct digests = distinct content
    by construction, even when the summary's leaf-collapsed fields tie (live
    false alarm 2026-07-25 on the pilot env: two lake-athena joins, different qualified
    from-paths, same 'cell' leaf). Mixed-scheme groups (digest + legacy) are
    the REAL accretion defect and still flag."""
    return (len(set(ids)) == len(ids)
            and all(DIGEST_ID_RE.match(i) for i in ids))


def probe_duplicates(summary: dict) -> dict:
    """Duplicate census over LIVE rules and join paths.

    Join paths are keyed by _joinpath_dup_key (structural rows by id; the
    rest on the table-qualified field tuple) — never on concept-level/
    leaf-collapsed fields. A concept-coarse key once produced 17 FAKE
    duplicate groups out of distinct table-qualified joins (the
    2026-07-24 false alarm); the regression fixture pins this.
    """
    identity_groups = _dup_groups(live_rules(summary), lambda r: (
        _norm(r.get("source_system")), _norm(r.get("source_field")),
        _norm(r.get("target_system")), _norm(r.get("target_field")),
        _norm(r.get("concept"))))
    joinpath_groups = [
        g for g in _dup_groups(live_join_paths(summary), _joinpath_dup_key)
        if not _all_distinct_digests(g["ids"])]
    return {
        "identity_dup_groups": identity_groups,
        "joinpath_dup_groups": joinpath_groups,
        "count": len(identity_groups) + len(joinpath_groups),
    }


# ── Probe 2 — id-shape audit ─────────────────────────────────────────────────

def probe_id_shapes(summary: dict) -> dict:
    """LIVE rule/join-path ids must be content digests (ir|jp-…-16hex),
    steward subjects (subj-…), or sanctioned structural intra-system
    families (jp-internal-/jp-vo-/jp-samerow-); anything else (legacy
    index-keyed like ir-<sys>-14) is flagged."""
    flagged = []
    for item in live_rules(summary) + live_join_paths(summary):
        item_id = str(item.get("id") or "")
        if not (DIGEST_ID_RE.match(item_id) or STEWARD_ID_RE.match(item_id)
                or STRUCTURAL_ID_RE.match(item_id)):
            flagged.append(item_id)
    return {"legacy_ids": flagged, "count": len(flagged)}


# ── Probe 3 — forbidden-pairing scan ─────────────────────────────────────────

def _measure_like_name(field) -> bool:
    tokens = _leaf_tokens(field)
    return bool(tokens & set(_MEASURE_NAME_TOKENS)
                or tokens & set(_MEASURE_UNIT_TOKENS))


def _identifier_like_name(field) -> bool:
    """Name-shape identifier check (is_identifier_like semantics, minus graph
    roles, which the graph-summary JSON does not carry)."""
    if _measure_like_name(field):
        return False
    return bool(_leaf_tokens(field) & set(_IDENTIFIER_NAME_TOKENS))


def _measure_endpoint_reason(rule: dict) -> str:
    """An identity endpoint can never be a MEASURE (count/qty/rate/duration)."""
    src, tgt = rule.get("source_field"), rule.get("target_field")
    for field, other in ((src, tgt), (tgt, src)):
        if _leaf(field) and _measure_like_name(field):
            pairing = ("pairs an identifier with a measure"
                       if _identifier_like_name(other)
                       else "has a measure endpoint")
            return (f"identity rule {pairing}: '{_leaf(field)}' is "
                    "measure-like, never an identity endpoint")
    return ""


def _stream_suffix_reason(rule: dict) -> str:
    """A field named like a telemetry stream (_DeviceState/_Alarm/_oee...)
    names a stream, not an entity identity."""
    for slot in ("source_field", "target_field"):
        leaf = _camel_split(_leaf(rule.get(slot))).lower()
        for suffix in _STREAM_VALUE_SUFFIX_TOKENS:
            if leaf == suffix or leaf.endswith("_" + suffix):
                return (f"'{leaf}' is a stream/state value name "
                        f"(suffix '{suffix}'), not an identity endpoint")
    return ""


def _grain_mismatch_reason(rule: dict) -> str:
    """Site⊃equipment containment is not identity."""
    src = _leaf_tokens(rule.get("source_field"))
    tgt = _leaf_tokens(rule.get("target_field"))
    site, equip = set(_SITE_GRAIN_TOKENS), set(_EQUIPMENT_GRAIN_TOKENS)
    src_site, src_equip = bool(src & site), bool(src & equip)
    tgt_site, tgt_equip = bool(tgt & site), bool(tgt & equip)
    if ((src_site and not src_equip and tgt_equip and not tgt_site)
            or (tgt_site and not tgt_equip and src_equip and not src_site)):
        return "containment (site⊃equipment) is not identity"
    return ""


def probe_forbidden_pairings(summary: dict) -> dict:
    """Forbidden semantic pairings over LIVE identity rules (pure shape
    re-implementation of the W3 writer gate; see PROBE3_SEMANTICS)."""
    violations = []
    for rule in live_rules(summary):
        for check in (_measure_endpoint_reason, _stream_suffix_reason,
                      _grain_mismatch_reason):
            reason = check(rule)
            if reason:
                violations.append({"id": str(rule.get("id") or ""),
                                   "reason": reason})
                break
    return {"violations": violations, "count": len(violations),
            "semantics": PROBE3_SEMANTICS}


# ── Probe 4 — auto-accept-method audit ───────────────────────────────────────

def probe_auto_accept(mappings: list) -> dict:
    """Accepted method=='hierarchy_position' rows must be 0 (P4 deny); an
    accepted UUID-shaped-target row must carry the verified=='true' stamp
    (the attr proposal_scoring reads — no acceptance without the check)."""
    hierarchy, unverified_uuid = [], []
    for m in mappings:
        if _norm(m.get("status")) != "accepted":
            continue
        mid = str(m.get("mapping_id") or "")
        if _norm(m.get("method")) == "hierarchy_position":
            hierarchy.append(mid)
        target = str(m.get("to_value") or "").strip()
        if _UUID_RE.match(target) and str(m.get("verified") or "") != "true":
            unverified_uuid.append(mid)
    return {"hierarchy_position_accepted": hierarchy,
            "unverified_uuid_targets": unverified_uuid,
            "count": len(hierarchy) + len(unverified_uuid)}


# ── Probe 5 — cross-site vouching ────────────────────────────────────────────

def _system_class(system_id) -> str:
    """Leading alphanumeric token of a system id ('mes-poc' → 'mes') — a pure
    shape proxy for the catalog's system_type, which the three audited
    endpoints do not expose."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", _norm(system_id)) if t]
    return tokens[0] if tokens else ""


def _vouching_authority_id(concept, summary: dict) -> str:
    """First AuthorityRule id whose concept matches (casefolded) — how a
    LEGACY (pre-B4) cross-site pending is explained when its review_reason
    is absent."""
    want = _norm(concept)
    if not want:
        return ""
    for ar in (summary or {}).get("authority_rules_detail", []) or []:
        if _norm(ar.get("concept")) == want:
            return str(ar.get("id") or "")
    return ""


def _cross_site_row(mapping: dict, summary: dict) -> dict:
    reason = str(mapping.get("review_reason") or "")
    match = _AUTHORITY_ID_RE.search(reason)
    authority_id = match.group(0) if match else ""
    legacy = False
    if not reason:
        authority_id = _vouching_authority_id(mapping.get("concept"), summary)
        legacy = bool(authority_id)
    return {"mapping_id": str(mapping.get("mapping_id") or ""),
            "from_system": str(mapping.get("from_system") or ""),
            "to_system": str(mapping.get("to_system") or ""),
            "concept": str(mapping.get("concept") or ""),
            "review_reason_present": bool(reason),
            "authority_id": authority_id, "legacy": legacy}


def probe_cross_site(mappings: list, summary: dict, catalog: dict | None = None) -> dict:
    """Pending same-class cross-system proposals and WHY each passed P8.

    Same-class = matching leading-token proxy AND, when the optional catalog
    (/api/systems) is available, matching connection_type — the type VETOES
    the proxy (live false alarm 2026-07-25: the pilot env's two system ids
    shared their leading name token but are Athena vs SiteWise systems, not
    cross-site siblings).
    Post-B4 rows carry a review_reason naming the vouching authority id;
    rows lacking it but matching a live authority by concept are LEGACY
    (pre-B4, reported but never gated); rows with NEITHER are unexplained —
    those gate under --strict."""
    sys_list = (catalog if isinstance(catalog, list)
                else (catalog or {}).get("systems", []) or [])
    ctypes = {_norm(s.get("id")): _norm(s.get("connection_type"))
              for s in sys_list}
    rows = []
    for m in _pending(mappings):
        if _norm(m.get("proposed_type")) not in _CROSS_SITE_GUARDED_TYPES:
            continue
        from_sys, to_sys = _norm(m.get("from_system")), _norm(m.get("to_system"))
        if not from_sys or not to_sys or from_sys == to_sys:
            continue
        cls = _system_class(from_sys)
        if not cls or cls != _system_class(to_sys):
            continue
        ft, tt = ctypes.get(from_sys, ""), ctypes.get(to_sys, "")
        if ft and tt and ft != tt:
            continue  # different system kinds — not cross-site siblings
        rows.append(_cross_site_row(m, summary))
    unexplained = [r["mapping_id"] for r in rows
                   if not r["review_reason_present"] and not r["authority_id"]]
    return {"pending": rows, "unexplained": unexplained, "count": len(rows)}


# ── Probe 6 — quarantine growth + pending-age distribution (never gates) ────

def _age_days(discovered_at, now: datetime):
    try:
        then = datetime.fromisoformat(str(discovered_at or ""))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return round((now - then).total_seconds() / 86400.0, 2)
    except ValueError:
        return None


def probe_trends(mappings: list, health: dict, now: datetime | None = None) -> dict:
    """Trend data only — this probe NEVER gates."""
    now = now or datetime.now(timezone.utc)
    pend = _pending(mappings)
    ages = sorted(a for a in (_age_days(m.get("discovered_at"), now)
                              for m in pend) if a is not None)
    distribution = {"count": len(ages),
                    "min_days": ages[0] if ages else None,
                    "max_days": ages[-1] if ages else None,
                    "avg_days": round(sum(ages) / len(ages), 2) if ages else None}
    quarantined = sum(1 for m in pend if str(m.get("quarantined") or "") == "true")
    return {"pending_total": len(pend), "pending_ages_days": ages[:100],
            "age_distribution": distribution,
            "pending_quarantined": quarantined,
            "health_quarantine": (health or {}).get("quarantine", {}),
            "health_staging": (health or {}).get("staging", {}),
            "gates": False}


# ── Probe 7 — usage-ledger liveness ──────────────────────────────────────────

def probe_usage_ledger(health: dict) -> dict:
    """Ledger liveness with the HONEST framing: on a report-only env, 0 rows
    is the expected-gap (report-only turns retain no verified relationship
    proofs to extract artifact ids from), reported as such — never a failure.
    'starved' (0 rows on an enforcing env) gates only under --strict;
    'unavailable' (health endpoint absent/empty) is reported, never gated."""
    if not health:
        return {"verdict": "unavailable", "rows_30d": None, "artifacts": None,
                "evidence_classes": [], "integrity_mode": ""}
    ledger = health.get("usage_ledger") or {}
    rows = int(ledger.get("rows_30d") or 0)
    mode = str(health.get("integrity_mode") or "")
    if rows > 0:
        verdict = "ok"
    elif mode == "report-only":
        verdict = "expected-gap"
    else:
        verdict = "starved"
    return {"verdict": verdict, "rows_30d": rows,
            "artifacts": ledger.get("artifacts"),
            "evidence_classes": list(ledger.get("evidence_classes") or []),
            "integrity_mode": mode}


# ── Probe 8 — unresolved-drift age ───────────────────────────────────────────

# Default steward SLA for acknowledging a schema_drift proposal (days).
DRIFT_MAX_AGE_DAYS_DEFAULT = 7.0


def probe_unresolved_drift(mappings: list, now: datetime | None = None,
                           max_age_days: float = DRIFT_MAX_AGE_DAYS_DEFAULT) -> dict:
    """Ages of PENDING ``proposed_type=="schema_drift"`` rows (ADR-0053 E5).

    PURE over already-fetched proposal rows. FALSE-ALARM POSTURE (the 3
    pinned lessons): rows are keyed by mapping_id ONLY — no shape/site/
    content inference. An env with ZERO schema_drift rows of ANY status
    reports ``"unavailable"`` and never gates (probe-7 idiom): a
    pre-drift-feature env and a genuinely drift-free env are
    indistinguishable here, and neither is a failure. ``over_age`` rows
    gate only under --strict (report-only by default)."""
    drift_rows = [m for m in mappings
                  if _norm(m.get("proposed_type")) == "schema_drift"]
    if not drift_rows:
        return {"verdict": "unavailable", "pending": 0, "over_age": [],
                "oldest_age_days": None, "max_age_days": max_age_days}
    now = now or datetime.now(timezone.utc)
    aged = []
    for m in _pending(drift_rows):
        age = _age_days(m.get("discovered_at"), now)
        if age is not None:
            aged.append((str(m.get("mapping_id") or ""), age))
    over_age = [mid for mid, age in aged if age > max_age_days]
    oldest = max((age for _, age in aged), default=None)
    return {"verdict": "over-age" if over_age else "ok",
            "pending": len(_pending(drift_rows)), "over_age": over_age,
            "oldest_age_days": oldest, "max_age_days": max_age_days}


# ── Probe runner + gate + report (pure) ──────────────────────────────────────

def run_probes(summary: dict, proposals: dict, health: dict,
               now: datetime | None = None, catalog: dict | None = None,
               drift_max_age_days: float = DRIFT_MAX_AGE_DAYS_DEFAULT) -> dict:
    """All 8 probes over already-fetched JSON. Pure — offline-testable."""
    mappings = live_mappings(proposals)
    return {
        "duplicates": probe_duplicates(summary),
        "id_shapes": probe_id_shapes(summary),
        "forbidden_pairings": probe_forbidden_pairings(summary),
        "auto_accept": probe_auto_accept(mappings),
        "cross_site": probe_cross_site(mappings, summary, catalog),
        "trends": probe_trends(mappings, health, now),
        "usage_ledger": probe_usage_ledger(health),
        "unresolved_drift": probe_unresolved_drift(mappings, now,
                                                   drift_max_age_days),
    }


def gate_failures(probes: dict, strict: bool) -> list:
    """The exit-code contract. Default gates on STRUCTURAL regressions only
    (probes 1–4); --strict adds unexplained cross-site pendings (probe 5),
    ledger starvation (probe 7, never the expected-gap), and over-age
    unresolved drift (probe 8, never the unavailable posture). Probe 6
    never gates."""
    failures = []
    if probes["duplicates"]["count"]:
        failures.append(
            f"probe 1: {probes['duplicates']['count']} duplicate group(s)")
    if probes["id_shapes"]["count"]:
        failures.append(
            f"probe 2: {probes['id_shapes']['count']} legacy-shaped id(s): "
            f"{probes['id_shapes']['legacy_ids'][:5]}")
    if probes["forbidden_pairings"]["count"]:
        failures.append(
            f"probe 3: {probes['forbidden_pairings']['count']} forbidden pairing(s)")
    if probes["auto_accept"]["count"]:
        failures.append(
            f"probe 4: {probes['auto_accept']['count']} auto-accept violation(s)")
    if strict:
        unexplained = probes["cross_site"]["unexplained"]
        if unexplained:
            failures.append(
                f"probe 5: {len(unexplained)} cross-site pending(s) without a "
                f"stated vouching authority (strict): {unexplained[:5]}")
        if probes["usage_ledger"]["verdict"] == "starved":
            failures.append(
                "probe 7: usage ledger starved — 0 rows in window on an "
                "enforcing env (strict)")
        drift = probes["unresolved_drift"]
        if drift["over_age"]:
            failures.append(
                f"probe 8: {len(drift['over_age'])} unresolved schema_drift "
                f"proposal(s) older than {drift['max_age_days']:g} days "
                f"(strict): {drift['over_age'][:5]}")
    return failures


_GATED_DEFAULT = ("duplicates", "id_shapes", "forbidden_pairings", "auto_accept")


def _probe_verdict(name: str, probe: dict, strict: bool) -> str:
    """Per-probe verdict for the report."""
    if name in _GATED_DEFAULT:
        return "fail" if probe["count"] else "pass"
    if name == "cross_site":
        if probe["unexplained"]:
            return "fail" if strict else "warn"
        return "pass"
    if name == "trends":
        return "report-only"
    verdict = probe.get("verdict", "")
    if verdict in ("starved", "over-age"):
        return "fail" if strict else "warn"
    return verdict  # ok / expected-gap / unavailable


def _guidance_budget_annotation(health: dict | None) -> dict:
    """Normalize the optional health fact without turning it into a gate."""
    raw = (health or {}).get("guidance_budget")
    if not isinstance(raw, dict):
        return {"available": False, "over_budget": False, "over_by": 0,
                "systems": None, "over_budget_systems": []}
    try:
        over_by = max(0, int(raw.get("over_by", 0) or 0))
    except (TypeError, ValueError):
        over_by = 0
    return {
        "available": bool(raw.get("available", True)),
        "over_budget": bool(raw.get("over_budget")) or over_by > 0,
        "over_by": over_by,
        "systems": raw.get("systems"),
        "over_budget_systems": raw.get("over_budget_systems") or [],
    }


def _authority_rule_evidence_annotation(health: dict | None) -> dict:
    """Normalize the computed graph fact; annotation-only in every mode.

    An UNAVAILABLE read carries NO orphan list. The counts already null out on
    unavailability, so shipping the raw list alongside them would let a stale or
    partially-built payload state WHICH rules are orphans while the read that
    would have confirmed it is known to have failed — a positive claim resting on
    an admitted unknown. Unavailable means empty.
    """
    graph = (health or {}).get("graph_integrity")
    raw = (graph or {}).get("authority_rule_evidence") \
        if isinstance(graph, dict) else None
    if not isinstance(raw, dict):
        return {"available": False, "active_rules": None,
                "orphan_count": None, "evidence_orphans": []}
    available = bool(raw.get("available", True))
    return {
        "available": available,
        "active_rules": raw.get("active_rules") if available else None,
        "orphan_count": raw.get("orphan_count") if available else None,
        "evidence_orphans": (list(raw.get("evidence_orphans") or [])
                             if available else []),
    }


def build_report(*, base_url: str, probes: dict, failures: list,
                 strict: bool, health: dict | None = None) -> dict:
    """Credential-free machine report: every probe's verdict + counts +
    offending ids, plus the suite gate result and annotation-only health facts."""
    return {
        "base_url": base_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strict": strict,
        "passed": not failures,
        "failures": failures,
        "probes": {name: {**probe,
                          "verdict": _probe_verdict(name, probe, strict)}
                   for name, probe in probes.items()},
        "annotations": {
            "guidance_budget": _guidance_budget_annotation(health),
            "authority_rule_evidence": _authority_rule_evidence_annotation(
                health),
        },
    }


# ── Fetch layer (HTTP — the only impure part) ────────────────────────────────

class App:
    """Cookie-login stdlib HTTP client (smoke_prompts.App posture)."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

    def call(self, method: str, path: str, body=None, timeout: int = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=timeout) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def _is_transient_network_error(exc: Exception) -> bool:
    """NARROW: dropped-socket/TLS/timeout only; HTTP statuses never retry."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, TRANSIENT_NETWORK_ERRORS):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, TRANSIENT_NETWORK_ERRORS)
    return False


def _fetch(app: App, path: str, timeout: int, optional: bool = False):
    """One GET with a single transient retry. ``optional`` paths degrade to
    {} on an HTTP error status (e.g. the /api/admin/learning/health endpoint
    on an env that predates it) — reported honestly by probe 7."""
    for attempt in (1, 2):
        try:
            return app.call("GET", path, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if optional:
                print(f"  note: GET {path} → HTTP {exc.code} (optional; "
                      "continuing with empty payload)", flush=True)
                return {}
            raise
        except (urllib.error.URLError, *TRANSIENT_NETWORK_ERRORS) as exc:
            if attempt == 2 or not _is_transient_network_error(exc):
                raise
            print(f"  transient network error on GET {path} "
                  f"({type(exc).__name__}) — retrying once", flush=True)
            time.sleep(FETCH_RETRY_BACKOFF_SECONDS)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
    return {}


def fetch_audit_inputs(app: App, timeout: int) -> tuple:
    """The pinned GETs; health and catalog are optional (older envs)."""
    summary = _fetch(app, "/api/thread/graph-summary", timeout)
    # include_expired: the census/probes audit the WHOLE lifecycle; the route
    # excludes expired rows by default since 2026-09-28.
    proposals = _fetch(app, "/api/proposed-mappings?include_expired=true", timeout)
    health = _fetch(app, "/api/admin/learning/health", timeout, optional=True)
    catalog = _fetch(app, "/api/systems", timeout, optional=True)
    return summary, proposals, health, catalog


# ── CLI ──────────────────────────────────────────────────────────────────────

def _env_flag(name: str, environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def build_parser(environ=None) -> argparse.ArgumentParser:
    """CLI parser; accepts an environment mapping for pure tests."""
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(
        description="Post-deploy learning-health gate (8 probes over the "
                    "deployed app's HTTP API)")
    ap.add_argument("--base-url", default=env.get("ADT_BASE_URL", ""))
    ap.add_argument("--username", default=env.get("ADT_USERNAME", "ADT"))
    ap.add_argument("--password", default=env.get("ADT_PASSWORD", ""))
    ap.add_argument("--timeout", type=int, default=60,
                    help="per-request timeout (s)")
    ap.add_argument("--drift-max-age-days", type=float,
                    default=DRIFT_MAX_AGE_DAYS_DEFAULT,
                    help="probe 8: max age (days) a pending schema_drift "
                         "proposal may reach before gating under --strict")
    ap.add_argument("--report", default=env.get("AUDIT_REPORT", ""),
                    help="path for the machine-readable JSON report "
                         "(default /tmp/audit_learning_report.json)")
    ap.add_argument("--strict", action="store_true",
                    default=_env_flag("AUDIT_STRICT", env),
                    help="also gate on unexplained cross-site pendings "
                         "(probe 5), ledger starvation (probe 7), and "
                         "over-age unresolved drift (probe 8)")
    return ap


def _print_probe_summary(report: dict) -> None:
    order = ("duplicates", "id_shapes", "forbidden_pairings", "auto_accept",
             "cross_site", "trends", "usage_ledger", "unresolved_drift")
    for index, name in enumerate(order, 1):
        probe = report["probes"][name]
        verdict = probe["verdict"]
        detail = ""
        if name == "cross_site":
            detail = (f" pending={probe['count']} "
                      f"unexplained={len(probe['unexplained'])}")
        elif name == "trends":
            detail = (f" pending={probe['pending_total']} "
                      f"quarantined={probe['pending_quarantined']}")
        elif name == "usage_ledger":
            detail = (f" rows_30d={probe['rows_30d']} "
                      f"mode={probe['integrity_mode'] or '?'}")
        elif name == "unresolved_drift":
            detail = (f" pending={probe['pending']} "
                      f"over_age={len(probe['over_age'])}")
        elif "count" in probe:
            detail = f" count={probe['count']}"
        print(f"  [{index}] {name}: {verdict.upper()}{detail}", flush=True)
    guidance = report["annotations"]["guidance_budget"]
    availability = "measured" if guidance["available"] else "unavailable"
    print("  [annotation] guidance_budget: "
          f"{availability} over_budget={guidance['over_budget']} "
          f"over_by={guidance['over_by']}", flush=True)
    authority = report["annotations"]["authority_rule_evidence"]
    availability = "measured" if authority["available"] else "unavailable"
    print("  [annotation] authority_rule_evidence: "
          f"{availability} active={authority['active_rules']} "
          f"orphans={authority['orphan_count']}", flush=True)


def _write_report(args, report: dict) -> None:
    path = args.report or os.path.join("/tmp", "audit_learning_report.json")  # nosec B108 - fixed report path documented in --help; report contains no secrets
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"\n== report written: {path} ==", flush=True)
    except OSError as exc:
        print(f"\n== WARN: report write failed ({type(exc).__name__}) ==",
              flush=True)


def main() -> int:
    args = build_parser().parse_args()
    if not args.base_url or not args.password:
        print("ERROR: --base-url and --password (or ADT_BASE_URL/ADT_PASSWORD) required")
        return 2
    app = App(args.base_url)
    try:
        login = app.call("POST", "/api/auth/login",
                         {"email": args.username, "password": args.password})
    except (urllib.error.URLError, *TRANSIENT_NETWORK_ERRORS) as exc:
        print(f"ERROR: login request failed ({type(exc).__name__})")
        return 2
    if login.get("status") != "ok":
        print("ERROR: login failed")
        return 2
    try:
        summary, proposals, health, catalog = fetch_audit_inputs(app, args.timeout)
    except (urllib.error.URLError, *TRANSIENT_NETWORK_ERRORS) as exc:
        print(f"ERROR: fetch failed ({type(exc).__name__}: {exc})")
        return 2
    probes = run_probes(summary, proposals, health, catalog=catalog,
                        drift_max_age_days=args.drift_max_age_days)
    failures = gate_failures(probes, args.strict)
    report = build_report(base_url=args.base_url, probes=probes,
                          failures=failures, strict=args.strict, health=health)
    print(f"== audit_learning: strict={args.strict} ==", flush=True)
    _print_probe_summary(report)
    _write_report(args, report)
    if failures:
        print(f"\n== RESULT: FAIL ({len(failures)}) ==", flush=True)
        for failure in failures:
            print(f"  - {failure}", flush=True)
        return 1
    print("\n== RESULT: PASS — learning-health gate clean ==", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
