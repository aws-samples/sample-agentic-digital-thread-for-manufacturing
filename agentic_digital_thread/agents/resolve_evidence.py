# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evidence-based helpers for the cross-system resolve step.

Generalizes the resolve step from "Postgres-only, LLM-guessed" to
"any-source, measured-evidence". Nothing here is dataset-specific.

Three concerns, each independently testable:
  * :func:`sample_field_values` — fetch distinct sample values for a field on ANY
    system, dispatching on connection_type (PostgreSQL / Athena / SiteWise).
  * :func:`extract_join_tokens` + :func:`value_overlap` — normalize values
    (including scalars buried inside JSON/struct blobs) and MEASURE how many
    concrete values two fields share, so a proposed link carries evidence, not
    just an LLM hunch.
  * :func:`coverage_gap_recommendations` — when a concept is shared across
    systems but NO in-data value bridge is found, emit an OPTIONAL "for best
    results, expose a shared key in the source" recommendation (never required,
    never assumes a source change).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import blake2b
from secrets import token_bytes
from typing import NamedTuple

from . import stage_timing

logger = logging.getLogger(__name__)

#: True inside :func:`checked_source_reads` (the join sweep that is the only
#: one covering its pairs: the workshop seed's ``Phase2JoinSweep`` once). A
#: sample or containment probe whose source read FAILS then raises instead of
#: answering ``[]`` / ``None``, so a failed read is never measured as an empty
#: column. Every other caller (the default) keeps the best-effort answers.
_CHECKED_SOURCE_READS: ContextVar = ContextVar("checked_source_reads", default=False)


@contextmanager
def checked_source_reads():
    """Make every source sample and probe in this context a checked read."""
    token = _CHECKED_SOURCE_READS.set(True)
    try:
        yield
    finally:
        _CHECKED_SOURCE_READS.reset(token)


def _unresolved_if_checked(what: str) -> None:
    """Inside :func:`checked_source_reads`, raise for a source whose read or
    connection lookup failed (*what*); outside, the caller answers empty."""
    if _CHECKED_SOURCE_READS.get():
        raise RuntimeError(f"source read failed: {what}")

# Distinct sample values fetched per field for cross-system value-overlap
# detection. This is a JOIN-KEY detector, not a data preview: the shared keys are
# frequently a SMALL fraction of a field's distinct values (e.g. two site codes
# among a system's many asset names), so a tiny cap silently misses real overlaps
# — a 10-value sample almost never contains the handful of keys two systems share.
# A few-hundred cap makes overlap detection reliable while staying cheap: Athena
# cost is dominated by the (columnar, single-column) GROUP BY scan and is
# independent of this LIMIT, and the graph/Postgres sample queries are trivially
# bounded. Kept below sql_safety.MAX_ROWS (500) so the row cap never lowers it.
_SAMPLE_LIMIT = 200
#: The longest sampled value SHOWN in a prompt, on the checkpoint or in a
#: reason (characters). Evidence keeps every sampled value whatever its
#: length (the token extractor finds a short key inside a long JSON value);
#: this only limits what one value adds to a text a model or a person reads
#: (:func:`prompt_values`, :func:`shown_text`). A longer value is left out of
#: the shown values, never cut: a cut value would read as a different value.
MAX_PROMPT_VALUE_CHARS = 256
#: Unicode categories a value shown in a prompt, on the checkpoint or in a
#: reason may not hold: controls (line breaks and tabs included), format
#: characters (zero-width and bidirectional overrides) and line/paragraph
#: separators. Each can make a text read differently from what it shows.
_NOT_FOR_A_PROMPT_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
#: What a reason says in place of a sampled value the display policy leaves
#: out (:func:`shown_text`).
VALUE_NOT_SHOWN = "(not shown: over 256 characters, or holding a control or format character)"

# ---------------------------------------------------------------------------
# Process-wide sample memo
# ---------------------------------------------------------------------------
# A field's bounded sample is the same evidence whichever caller asks for it:
# every directed join-discovery pair, the resolve gates, the affinity rules
# and the post-provision value miner all sample the same identifier fields,
# and each used to keep only a per-call cache. Measured on a workshop account
# (2026-10-03): a resolve of one eight-table system against six
# peers sampled its own fields a dozen times and every peer's twice, then the
# provision step repeated the whole sweep, 8.5 minutes for the one system.
# One memo, a short TTL, successes only: a backend failure is retried on the
# next ask rather than hidden for the window.
SAMPLE_MEMO_TTL_SECONDS = 600
_SAMPLE_MEMO_MAX_ENTRIES = 5000
_sample_memo: dict[tuple, tuple[float, list[str]]] = {}
_sample_memo_lock = threading.Lock()
_sample_memo_counts = {"fetched": 0, "hits": 0}


def _memo_clock() -> float:
    """Monotonic seconds; a seam so tests can age the memo."""
    return time.monotonic()


def _reset_sample_memo() -> None:
    """Drop every memoized sample and zero the counters (tests, redeploys)."""
    with _sample_memo_lock:
        _sample_memo.clear()
        _sample_memo_counts.update(fetched=0, hits=0)


def sample_memo_stats() -> dict[str, int]:
    """``{"fetched": backend samples taken, "hits": served from the memo}``."""
    with _sample_memo_lock:
        return dict(_sample_memo_counts)


def _memo_get(key: tuple) -> list[str] | None:
    now = _memo_clock()
    with _sample_memo_lock:
        hit = _sample_memo.get(key)
        if hit is None or hit[0] <= now:
            return None
        _sample_memo_counts["hits"] += 1
        return list(hit[1])


def _memo_put(key: tuple, values: list[str]) -> None:
    now = _memo_clock()
    with _sample_memo_lock:
        _sample_memo_counts["fetched"] += 1
        if len(_sample_memo) >= _SAMPLE_MEMO_MAX_ENTRIES:
            for stale in [k for k, (exp, _v) in _sample_memo.items() if exp <= now]:
                del _sample_memo[stale]
            if len(_sample_memo) >= _SAMPLE_MEMO_MAX_ENTRIES:
                _sample_memo.clear()
        _sample_memo[key] = (now + SAMPLE_MEMO_TTL_SECONDS, list(values))


# ---------------------------------------------------------------------------
# Nested/JSON token extraction + measured overlap (pure — unit-testable)
# ---------------------------------------------------------------------------

def extract_join_tokens(value) -> list[str]:
    """Return candidate join tokens from a sample value.

    A join key is often a clean scalar (``"SITE-A-1003"``), but sometimes it is
    buried inside a JSON/struct blob a federated source serializes as text
    (e.g. ``[{"name":"SITE-B-CS6900Z-01","id":"SITE-C-21"}]``). This pulls the
    scalar leaf values out so overlap comparison can see the real identifiers.

    - JSON object/array text → every scalar leaf (str/number/bool), stringified.
    - Plain scalar/text → the value itself (stripped).
    Empty/None → no tokens. De-duplicated, order-preserving.
    """
    if value is None:
        return []
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    if not text:
        return []

    tokens: list[str] = []

    def _walk(node):
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        elif isinstance(node, bool):
            tokens.append(str(node).lower())
        elif isinstance(node, (str, int, float)):
            s = str(node).strip()
            if s:
                tokens.append(s)

    parsed = None
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
    if parsed is not None:
        _walk(parsed)
    else:
        tokens.append(text)

    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _token_set(values) -> set[str]:
    toks: set[str] = set()
    for v in values or []:
        toks.update(extract_join_tokens(v))
    return toks


def value_overlap(values_a, values_b) -> dict:
    """Measure how many concrete join tokens two sampled fields share.

    Returns ``{overlap_count, examples, a_tokens, b_tokens, ratio}`` where
    ``ratio`` is overlap / smaller-token-set (0..1) — a size-normalized strength
    signal. Both sides are tokenized via :func:`extract_join_tokens` so a value
    hidden in a JSON blob still matches a clean scalar on the other side.
    """
    a = _token_set(values_a)
    b = _token_set(values_b)
    shared = a & b
    smaller = min(len(a), len(b)) or 1
    return {
        "overlap_count": len(shared),
        "examples": sorted(shared)[:5],
        "a_tokens": len(a),
        "b_tokens": len(b),
        "ratio": round(len(shared) / smaller, 3),
    }


# ---------------------------------------------------------------------------
# Multi-source sampling (IO — dispatch on connection_type)
# ---------------------------------------------------------------------------

# Cross-account credential keys an Athena boto3 client needs to STS AssumeRole
# into a source account (see agents.aws_session.boto3_client_for). ``role_arn`` is
# a non-secret identifier persisted on the System node; ``external_id`` is a SECRET
# resolved from the referenced Secrets Manager secret; ``region`` pins the
# assume-role/query region. Generic — connector-agnostic, no dataset literals.
_CROSS_ACCOUNT_KEYS = ("role_arn", "external_id", "region")


def _with_cross_account_credentials(system_id: str, connection_config: dict) -> dict:
    """Ensure a sampling ``connection_config`` carries the cross-account credentials.

    The onboarding resolve/map/provision sampling paths pass either the request
    body's ``connection_config`` or the NON-SECRET catalog/node snapshot — neither
    of which carries the STS ``external_id`` (a secret) and sometimes lacks
    ``role_arn``. A boto3 Athena client built from that partial config silently
    falls back to AMBIENT (deploy-account) credentials and is denied on the
    cross-account workgroup (``AccessDeniedException`` on ``athena:GetWorkGroup`` /
    ``StartQueryExecution``) — even though QUERY-TIME succeeds, because
    ``metric_resolver`` resolves the FULL config first.

    Close that asymmetry the SAME way, DRY: resolve the system's complete
    connection via the canonical node-based :func:`connection_resolver.resolve_connection`
    (node non-secret config + the merged Secrets Manager secret — the single source
    of a system's live connection per ADR-0031) and fill ONLY the cross-account
    keys the passed config is MISSING, so the boto3 client assumes the correct
    role. Non-credential fields the caller supplied (``workgroup``/``database``/…)
    are preserved untouched.

    Best-effort and generic: a blank ``system_id`` (pre-registration Mode-B
    discovery) or a resolve failure leaves the passed config unchanged; the caller
    still fails-soft to ``[]`` if the resulting config cannot query. Secret values
    are never logged.
    """
    cfg = dict(connection_config or {})
    if not system_id:
        return cfg
    if cfg.get("role_arn") and cfg.get("external_id"):
        return cfg  # already complete — no resolution needed
    # 1. Node-based resolver — the live source for PROVISIONED systems.
    try:
        from .connection_resolver import resolve_connection
        resolved = resolve_connection(system_id) or {}
    except Exception as e:  # noqa: BLE001 - best-effort; leave config as-is
        logger.info("%s: %s (cross-account credential resolve skipped)", system_id, e)
        resolved = {}
    for key in _CROSS_ACCOUNT_KEYS:
        if not cfg.get(key) and resolved.get(key):
            cfg[key] = resolved[key]
    # 2. Pre-provision fallbacks (the registration secrets).
    _fill_from_registration_secrets(system_id, cfg)
    return cfg


def _fill_from_registration_secrets(system_id: str, cfg: dict) -> None:
    """Step 2 of :func:`_with_cross_account_credentials`: fill the missing
    cross-account keys of ``cfg`` in place from the registration secrets.

    A freshly-REGISTERED system has NO graph node yet (``resolve_connection``
    is node-only per ADR-0031), but its cross-account STS credentials may live
    in (a) the registration-time connection secret ``adt/conn/<system_id>`` —
    minted by ``connect_new_system`` when the submitted config carried
    ``external_id``/``token``, and the SAME secret the provisioned node later
    points at — or (b) the deterministic agent-config secret
    (``adt/agent-config/<system_id>``). Merge them the SAME way
    ``metric_resolver._full_connection_config`` does (DRY) so DISCOVER-time and
    pre-provision resolve-evidence queries still assume the cross-account role
    instead of falling back to ambient (deploy-account) creds. Secret values
    are never logged.
    """
    for _secret_name in (f"adt/conn/{system_id}",
                         f"adt/agent-config/{system_id}"):
        if cfg.get("role_arn") and cfg.get("external_id"):
            break
        try:
            from .system_agent import _fetch_credentials
            secret = _fetch_credentials(_secret_name) or {}
        except Exception as e:  # noqa: BLE001 - best-effort; leave config as-is
            logger.info("%s: %s (cross-account credential secret fallback skipped)",
                        system_id, e)
            secret = {}
        if isinstance(secret, dict):
            for key in _CROSS_ACCOUNT_KEYS:
                if not cfg.get(key) and secret.get(key):
                    cfg[key] = secret[key]


def _fit_for_a_prompt(value) -> bool:
    """Whether a sampled value may be shown: at most
    :data:`MAX_PROMPT_VALUE_CHARS` characters, none of
    :data:`_NOT_FOR_A_PROMPT_CATEGORIES`. The one display policy for every
    text a model or a person reads: the resolve prompts, the checkpoint's
    examples and the rejection reasons."""
    text = value if isinstance(value, str) else str(value)
    return len(text) <= MAX_PROMPT_VALUE_CHARS and not any(
        unicodedata.category(ch) in _NOT_FOR_A_PROMPT_CATEGORIES for ch in text)


def prompt_values(values, n: int) -> list:
    """The first ``n`` of ``values`` fit to be shown (:func:`_fit_for_a_prompt`),
    in order: what a resolve prompt shows of a field's sample, and the
    ``shown_examples`` the checkpoint renders. Display only; evidence keeps
    every value."""
    return [v for v in values or () if v is not None and _fit_for_a_prompt(v)][:max(0, int(n))]


def shown_text(value, cut: "int | None" = None) -> str:
    """``value`` as a reason shows it: its text (its first ``cut``
    characters, when given), or :data:`VALUE_NOT_SHOWN` when that text is not
    fit to be shown (:func:`_fit_for_a_prompt`)."""
    text = "" if value is None else value if isinstance(value, str) else str(value)
    text = text[:cut] if cut else text
    return text if _fit_for_a_prompt(text) else VALUE_NOT_SHOWN


def quoted_text(value, cut: "int | None" = None) -> str:
    """:func:`shown_text` in single quotes; the marker is not quoted."""
    text = shown_text(value, cut)
    return text if text == VALUE_NOT_SHOWN else f"'{text}'"


def bounded_sample(values, limit: int) -> list[str]:
    """``values`` as a sample: the first ``limit`` distinct values that are
    not blank, as text, in order, kept as the source holds them (an
    exact-agreement rule is judged on raw values). Every source's sample goes
    through it, so a backend that returns more than it was asked for cannot
    widen the evidence. No value is left out for its length or its
    characters: the overlap's token extractor reads it (a short key inside a
    long JSON value), and only what a prompt shows is limited
    (:func:`prompt_values`)."""
    out: list[str] = []
    seen: set = set()
    for value in values or ():
        if len(out) >= limit:
            break
        text = None if value is None else value if isinstance(value, str) else str(value)
        if text is not None and text.strip() and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def sample_field_values(
    *,
    system_id: str,
    connection_type: str,
    schema: str = "",
    table: str = "",
    field: str = "",
    connection_config: dict | None = None,
    limit: int = _SAMPLE_LIMIT,
    statement_timeout_ms: int | None = None,
    use_memo: bool = True,
) -> list[str]:
    """Fetch up to ``limit`` distinct sample values for a field on ANY system
    (:func:`bounded_sample`: a backend that returns more cannot widen it).

    ``statement_timeout_ms`` bounds a PostgreSQL read (``db.pg`` clamps it to
    its default; None keeps the default); a memoized sample is served as is.
    ``use_memo=False`` reads the source now and leaves the memo as it was
    (neither read nor written): the provision step's re-measurement of a
    measured rule, whose evidence must be the columns as they are, uses it.

    Dispatches on ``connection_type`` (best-effort; returns ``[]`` on any error so
    the resolve step degrades gracefully rather than failing; not so in
    :func:`checked_source_reads`):
      * PostgreSQL      → ``SELECT DISTINCT CAST(field AS TEXT) ...`` via ``db.pg``.
      * Athena          → ``SELECT DISTINCT "field" FROM "table" LIMIT n`` (boto3);
                          cross-account ``role_arn``/``external_id`` are merged in
                          from the resolved node/secret config (see
                          :func:`_with_cross_account_credentials`) so sampling
                          assumes the SAME role query-time uses.
      * SiteWise (*)    → per the connector field contract from the graph
                          (_sample_sitewise_assets): id/name/path/level columns
                          map to Asset attributes; property columns return [].
    """
    ct = (connection_type or "").lower()
    key = (system_id, ct, schema, table, field, int(limit))
    memoized = _usable_memo(key, use_memo, checked := _CHECKED_SOURCE_READS.get())
    if memoized is not None:
        return memoized
    try:
        values = _bounded_backend_sample(ct, system_id, schema, table, field, connection_config,
                                         limit, connection_type, statement_timeout_ms)
    except Exception as e:  # noqa: BLE001 - sampling is best-effort
        logger.info("sample_field_values skipped (%s %s.%s): %s", system_id, table, field, e)
        if checked:
            raise
        return []
    if use_memo:
        _memo_put(key, values)
    return list(values)


def _usable_memo(key: tuple, use_memo: bool, checked: bool) -> "list[str] | None":
    """The memoized sample :func:`sample_field_values` may serve, else ``None``.
    A checked read (:func:`checked_source_reads`) reads an empty one again: a
    best-effort read memoizes the empty answer of a failed read."""
    memoized = _memo_get(key) if use_memo else None
    if memoized is None or (checked and not memoized):
        return None
    stage_timing.incr("io.source_sample.memo_hits")
    return memoized


def _bounded_backend_sample(ct: str, system_id: str, schema: str, table: str, field: str,
                            connection_config, limit: int, connection_type: str,
                            statement_timeout_ms: "int | None") -> list[str]:
    """One backend sample (:func:`_sample_backend`), bounded
    (:func:`bounded_sample`); raises on failure."""
    values = _sample_backend(ct, system_id, schema, table, field, connection_config or {}, limit,
                             declared_type=connection_type or "",
                             **({"statement_timeout_ms": statement_timeout_ms}
                                if statement_timeout_ms is not None else {}))
    return bounded_sample(values, int(limit))


def _sample_backend(ct: str, system_id: str, schema: str, table: str, field: str,
                    connection_config: dict, limit: int,
                    declared_type: str = "", statement_timeout_ms: int | None = None) -> list[str]:
    """One backend sample, dispatched on the connection type; raises on failure."""
    if "postgres" in ct:
        if statement_timeout_ms is not None:
            return _sample_postgres(schema, table, field, limit,
                                    statement_timeout_ms=statement_timeout_ms)
        return _sample_postgres(schema, table, field, limit)
    if "athena" in ct:
        return _sample_athena_source(system_id, connection_config, table, field, limit)
    if "sitewise" in ct:
        return _sample_sitewise_assets(system_id, limit, table=table, field=field)
    # A REST-family vendor whose join keys live behind its API (not as graph
    # Asset nodes) supplies a ``sample_values`` hook; vendors without one fall
    # through to []. Keyed off the registry, not a per-vendor substring. ``table``
    # is passed through for a vendor (Tulip) that needs it to resolve a table id.
    from .rest_vendor_registry import resolve_vendor
    adapter = resolve_vendor(declared_type)
    if adapter is not None and adapter.sample_values is not None:
        return adapter.sample_values(system_id, connection_config, field, limit, table=table)
    return []


#: Max values one containment probe sends (bounds the IN-list / ANY array).
PROBE_MAX_VALUES = 200


def probe_field_values(
    system_id: str,
    connection_type: str,
    schema: str,
    table: str,
    field: str,
    values,
    statement_timeout_ms: int | None = None,
) -> "list[str] | None":
    """Which of ``values`` EXIST in ``table.field`` of a system (containment).

    Independent ``DISTINCT … LIMIT n`` samples cannot see a SPARSE foreign key:
    a child table referencing a few parent rows shares almost nothing with an
    arbitrary sample of a large parent key space, so measured overlap reads ~0
    for a real join. A targeted membership probe asks the other side directly.

    PostgreSQL (parameterized ``= ANY(%s)`` — values never reach the SQL
    text) and REST-family vendors whose registry adapter supplies a
    ``membership_probe`` hook (Tulip: equal filters joined by
    ``filterAggregator=any``); every other connection type returns ``None``
    (= "cannot probe", distinct from ``[]`` = "probed, nothing found").
    Bounded to :data:`PROBE_MAX_VALUES` values. Best-effort: a failure
    returns ``None``, except in :func:`checked_source_reads`, where a probe
    whose read fails raises. ``statement_timeout_ms`` bounds the PostgreSQL read
    (``db.pg`` clamps it; None keeps the default).
    """
    vals = [str(v) for v in (values or []) if v is not None and str(v).strip()]
    vals = list(dict.fromkeys(vals))[:PROBE_MAX_VALUES]
    if not vals:
        return None
    if (connection_type or "PostgreSQL") != "PostgreSQL":
        return _probe_rest_vendor(system_id, connection_type, table, field, vals)
    try:
        return _probe_postgres(schema, table, field, vals, statement_timeout_ms)
    except Exception as e:  # noqa: BLE001 - probing is best-effort evidence
        logger.info("probe_field_values skipped (%s %s.%s): %s", system_id, table, field, e)
        if _CHECKED_SOURCE_READS.get():
            raise
        return None


def _probe_postgres(schema: str, table: str, field: str, vals: list,
                    statement_timeout_ms: "int | None") -> "list[str] | None":
    """:func:`probe_field_values`' PostgreSQL probe: ``None`` when the column
    cannot be probed or (outside a checked read) the read failed; a failed
    read raises in :func:`checked_source_reads`."""
    from .db import validate_identifier
    fld = (field or "").split(".")[-1]
    if not (schema and table and fld) or schema == "mcp":
        return None
    try:
        validate_identifier(fld)
        validate_identifier(table)
    except ValueError:              # a name that cannot be probed, not a failed read
        return None
    r = (_probe_query(schema, table, fld, vals) if statement_timeout_ms is None
         else _probe_query(schema, table, fld, vals, statement_timeout_ms=statement_timeout_ms))
    if r.get("unavailable"):
        if _CHECKED_SOURCE_READS.get():
            raise RuntimeError(f"probe read failed: {r.get('error_category') or 'unavailable'}")
        return None
    return [row[0] for row in r.get("rows", []) if row and row[0] is not None]


def _probe_rest_vendor(system_id: str, connection_type: str, table: str, field: str,
                       vals: list) -> "list[str] | None":
    """Dispatch a membership probe to the REST vendor adapter's
    ``membership_probe`` hook (keyed off the registry, not a per-vendor
    substring). ``None`` when no adapter owns the type, it has no hook, or the
    hook fails: the exact pass then fails the pair closed, as before."""
    try:
        from .rest_vendor_registry import resolve_vendor
        adapter = resolve_vendor(connection_type or "")
        if adapter is None or adapter.membership_probe is None:
            return None
        found = adapter.membership_probe(system_id, table, field, vals)
    except Exception as e:  # noqa: BLE001 - probing is best-effort evidence
        logger.info("probe_field_values skipped (%s %s.%s): %s", system_id, table, field, e)
        if _CHECKED_SOURCE_READS.get():
            raise
        return None
    if found is None:
        return None
    asked = set(vals)
    return [str(v) for v in found if str(v) in asked]


#: Values one Tulip probe request asks about (equal filters, ``any``-joined).
TULIP_PROBE_CHUNK = 20
#: Record pages read for one chunk before it stops (a value with many rows).
TULIP_PROBE_MAX_PAGES = 3
#: Hard ceiling on Tulip requests one probe makes, across every chunk.
TULIP_PROBE_MAX_REQUESTS = 30
#: Tulip's page size for record reads (the API maximum).
_TULIP_PAGE = 100


def _probe_tulip_records(system_id: str, table: str, field: str, values) -> "list[str] | None":
    """Which of ``values`` exist in ONE Tulip Table column: the ``tulip``
    adapter's ``membership_probe`` delegate.

    Bounded, read-only ``GET {prefix}/tables/{id}/records`` reads through the
    shared ``_TulipClient`` (auth, byte cap, workspace scope): each request asks
    about at most :data:`TULIP_PROBE_CHUNK` values as ``equal`` filters joined by
    ``filterAggregator=any``, reads at most :data:`TULIP_PROBE_MAX_PAGES` pages of
    a chunk (stopping as soon as every value in it is found), and the whole probe
    makes at most :data:`TULIP_PROBE_MAX_REQUESTS` requests. Only values that
    were ASKED and that a returned row actually carries count as found, so a
    server ignoring a filter cannot make a value look present. A value the page
    or request caps never reach is reported as not found: that can only lower
    the measured overlap (the exact pass fails closed), never raise it.

    ``None`` (cannot probe) when the column, base URL or table id cannot be
    resolved, or when any request fails; ``[]`` when probed and none found.
    Connection and table-id resolution are the sampler's
    (:func:`_tulip_connection_config`, :func:`_tulip_table_id_for`)."""
    column = (field or "").strip()
    if table and column.startswith(f"{table}."):
        column = column[len(table) + 1:]
    column = column.split(".")[-1].strip()
    asked = [str(v) for v in (values or []) if v is not None and str(v).strip()]
    asked = list(dict.fromkeys(asked))[:PROBE_MAX_VALUES]
    if not column or not asked:
        return None
    cfg = _tulip_connection_config(system_id, {})
    base_url = (cfg.get("base_url") or "").strip()
    if not base_url:
        _unresolved_if_checked("Tulip connection unresolved")
        return None
    table_id = _tulip_table_id_for(system_id, table, cfg)
    if not table_id:
        return None
    try:
        return _tulip_probe_reads(cfg, base_url, table_id, column, asked)
    except Exception as e:  # noqa: BLE001 - probing is best-effort evidence
        logger.info("tulip probe skipped (%s %s.%s): %s", system_id, table, field, e)
        _unresolved_if_checked("Tulip probe read failed")
        return None


@stage_timing.timed("io.source_probe")
def _tulip_probe_reads(cfg: dict, base_url: str, table_id: str, column: str,
                       asked: list) -> list[str]:
    """The bounded Tulip reads :func:`_probe_tulip_records` runs once its guards
    pass; its own seam so only a probe that reaches the source counts as
    ``io.source_probe``. Raises on a failed request (the caller answers None)."""
    from .system_agent import _TulipClient
    client = _TulipClient(
        base_url=base_url, secret_arn=cfg.get("secret_arn", ""),
        token=cfg.get("token", ""), auth_scheme=cfg.get("auth_scheme", "") or "basic",
        workspace_id=cfg.get("workspace_id", ""), key_scope=cfg.get("key_scope", ""),
        system_id=cfg.get("system_id", ""),
        credential_source=cfg.get("credential_source", ""),
        table_label_prefix=cfg.get("table_label_prefix") or "")
    found: set[str] = set()
    requests = 0
    for start in range(0, len(asked), TULIP_PROBE_CHUNK):
        chunk = asked[start:start + TULIP_PROBE_CHUNK]
        filters = json.dumps([{"field": column, "functionType": "equal", "arg": v}
                              for v in chunk])
        want = set(chunk)
        for page in range(TULIP_PROBE_MAX_PAGES):
            if requests >= TULIP_PROBE_MAX_REQUESTS:
                return sorted(found)
            requests += 1
            params = {"limit": _TULIP_PAGE, "filters": filters, "filterAggregator": "any"}
            if page:
                params["offset"] = page * _TULIP_PAGE
            data = client.get(f"/api/v3/tables/{table_id}/records", params)
            rows = _tulip_probe_rows(data)
            for row in rows:
                v = row.get(column)
                # Exact membership: the cell's own text, never trimmed, so a
                # padded " A " is not reported as the asked "A".
                s = "" if v is None else str(v)
                if s in want:
                    found.add(s)
            if want <= found or len(rows) < _TULIP_PAGE:
                break
    return sorted(found)


def _tulip_probe_rows(data) -> list:
    """The record rows of one Tulip records response: a JSON list of objects,
    or an object whose ``records`` member is one. Any other shape raises, so the
    probe answers ``None`` (cannot tell) instead of ``[]`` (probed, absent)."""
    rows = data.get("records") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("unexpected Tulip records response shape")
    return rows


@stage_timing.timed("io.source_probe")
def _probe_query(schema: str, table: str, fld: str, vals: list,
                 statement_timeout_ms: int | None = None) -> dict:
    """The containment query :func:`probe_field_values` runs; its own seam so
    only a probe that reaches the source counts as ``io.source_probe``.
    Validates its identifiers itself (raises on a bad one)."""
    from .db import pg, validate_identifier
    validate_identifier(fld)
    validate_identifier(table)
    deadline = {} if statement_timeout_ms is None else {"statement_timeout_ms": statement_timeout_ms}
    return pg(schema,
              f"SELECT DISTINCT CAST({fld} AS TEXT) FROM {table} "  # nosec B608 - identifiers validated
              f"WHERE CAST({fld} AS TEXT) = ANY(%s)", (vals,), **deadline)


def _sample_postgres(schema: str, table: str, field: str, limit: int,
                     statement_timeout_ms: int | None = None) -> list[str]:
    from .db import validate_identifier
    fld = (field or "").split(".")[-1]
    if not (schema and table and fld) or schema == "mcp":
        return []
    try:
        validate_identifier(fld)
        validate_identifier(table)
    except ValueError:              # a name that cannot be sampled, not a failed read
        return []
    if statement_timeout_ms is not None:
        return _postgres_distinct(schema, table, fld, limit, statement_timeout_ms=statement_timeout_ms)
    return _postgres_distinct(schema, table, fld, limit)


@stage_timing.timed("io.source_sample")
def _postgres_distinct(schema: str, table: str, fld: str, limit: int,
                       statement_timeout_ms: int | None = None) -> list[str]:
    """The sample query :func:`_sample_postgres` runs once its guards pass; its
    own seam so a guarded no-op never counts as ``io.source_sample``.
    Validates its identifiers itself (raises on a bad one). A read that fails
    raises (``db.pg`` reports it as ``unavailable``), so a failure is never
    memoized as an empty sample."""
    from .db import pg, validate_identifier
    validate_identifier(fld)
    validate_identifier(table)
    deadline = {} if statement_timeout_ms is None else {"statement_timeout_ms": statement_timeout_ms}
    r = pg(schema, f"SELECT DISTINCT CAST({fld} AS TEXT) FROM {table} WHERE {fld} IS NOT NULL LIMIT {int(limit)}", **deadline)  # nosec B608 - identifiers validated
    if r.get("unavailable") and (statement_timeout_ms is not None
                                 or _CHECKED_SOURCE_READS.get()):
        raise RuntimeError(f"sample read failed: {r.get('error_category') or 'unavailable'}")
    return [row[0] for row in r.get("rows", []) if row and row[0] is not None]


@stage_timing.timed("io.source_sample")
def _sample_athena_source(system_id: str, connection_config: dict, table: str, field: str,
                          limit: int) -> list[str]:
    """Athena sampling INCLUDING the cross-account credential merge (graph and
    Secrets Manager reads), so that lookup is source-sampling time too."""
    cfg = _with_cross_account_credentials(system_id, connection_config)
    return _sample_athena(cfg, table, field, limit)


def _athena_answer(res: dict) -> dict:
    """An Athena sample's result; an error answer raises in
    :func:`checked_source_reads` (outside, it reads as no rows)."""
    if res.get("error"):
        _unresolved_if_checked(f"Athena {res.get('error_type') or 'error'}")
    return res


def _sample_athena(connection_config: dict, table: str, field: str, limit: int) -> list[str]:
    from .athena_tools import run_athena_query, _resolve_config, _safe_sql_literal
    if not (table and field and connection_config.get("workgroup")):
        if table and field:
            _unresolved_if_checked("Athena workgroup unresolved")
        return []
    cfg = _resolve_config(connection_config)
    tbl = _safe_sql_literal(table)
    # Mappings carry a table-qualified ``field`` (``"{table}.{column}"`` — the
    # onboarding map job, and the >100-col virtual-table remap, both prefix the
    # real table onto the field). Athena's column is the UNQUALIFIED remainder,
    # so strip exactly the leading ``{table}.`` while PRESERVING any further dotted
    # path (flattened nested-struct columns like ``a.b.c`` stay intact). Without
    # this, ``SELECT "{table}.{column}"`` raises COLUMN_NOT_FOUND and sampling
    # returns [] — silently starving the evidence/coverage-gap resolve paths.
    column = field
    if table and column.startswith(f"{table}."):
        column = column[len(table) + 1:]
    col = _safe_sql_literal(column)
    # ``field`` is quoted as a single identifier. This is correct for federated
    # sources that FLATTEN nested structs into dotted COLUMN NAMES (e.g. the column
    # literally named "a.b.c", whose VALUE may itself be a JSON blob that
    # extract_join_tokens then mines). Sources exposing TRUE nested struct columns
    # (requiring "a"."b"."c" navigation) aren't handled here — that query errors and
    # is skipped (best-effort []), never crashing resolve.
    #
    # NO DISTINCT-via-plain-LIMIT: a bare ``LIMIT n`` reads only the first
    # split(s), so on a large clustered/partitioned column it returns a SINGLE
    # cluster's value (observed live: one facility code repeated) — a
    # non-representative sample that silently starves value-overlap even when the
    # two columns genuinely share values elsewhere in the table. Instead sample
    # DISTINCT join-key values via ``GROUP BY``: this spreads the sample across the
    # distinct value space so a real overlap is actually observed. On columnar
    # storage the scan touches only the single grouped column's bytes, so it stays
    # cheap; ``LIMIT n`` still bounds the number of distinct keys returned. (A
    # workgroup-level BytesScannedCutoffPerQuery is the recommended extra guard for
    # very large non-columnar sources.)
    fetch = int(limit)
    sql = f'SELECT "{col}" FROM "{tbl}" WHERE "{col}" IS NOT NULL GROUP BY "{col}" LIMIT {fetch}'  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
    res = _athena_answer(run_athena_query(sql, cfg, max_rows=fetch))
    seen: set = set()
    out: list[str] = []
    for row in res.get("rows", []):
        v = row[0] if row else None
        if v is not None and v not in seen:
            seen.add(v)
            out.append(v)
        if len(out) >= limit:
            break
    return out


#: SiteWise field -> graph-projection contract. The sampler dispatches on this
#: instead of name-vs-path guesswork. A spec is ``(projection, level)``:
#:   projection: which value to read -- "name", "path", "source_id",
#:               "asset_type" (Asset node attributes), or "parent" (the
#:               HAS_PARENT parent's source_id);
#:   level:      an exact ``a.level`` to scope to (Direct asset_hierarchy only),
#:               else None (model tables scope by the exact asset model).
#: ``None`` as the whole spec means a contract column with NO graph source yet
#: (sample []). Columns NOT in a contract (model properties) also sample [].

#: Asset-node attribute for each non-parent projection key.
_SW_ATTR_PROJECTIONS = {"name": "a.name", "path": "a.path",
                        "source_id": "a.source_id", "asset_type": "a.asset_type"}

#: Model-table columns (i3X per-type tables and Direct per-model tables). i3X
#: discovery's identifier columns are elementId / displayName / typeElementId /
#: namespaceUri / parentElementId / path (onboard_i3x._IDENTIFIER_COLUMN_NAMES);
#: Direct per-model tables add _hierarchyName / _path / name / asset_name.
_SW_MODEL_FIELD_PROJECTIONS = {
    # i3X identifier columns
    "elementid": ("source_id", None),
    "displayname": ("name", None),
    "parentelementid": ("parent", None),
    "path": ("path", None),
    # typeElementId / namespaceUri are not persisted on the Asset node, so there
    # is no graph value to sample. Explicit "no graph source" entries, not
    # silent omissions, so the contract-completeness test can see them.
    "typeelementid": None,
    "namespaceuri": None,
    # Direct per-model identifier columns
    "_hierarchyname": ("name", None),
    "name": ("name", None),
    "asset_name": ("name", None),
    "_path": ("path", None),
}

#: Direct ``asset_hierarchy`` pseudo-table columns (NOT a model; it enumerates
#: assets by hierarchy level). Imported column set:
#: onboard_agent.ASSET_HIERARCHY_COLUMNS. Level strings are what the Direct and
#: i3X walkers write via onboard_i3x._level_for: enterprise/site/area/line, then
#: equipment (depth<=3) / component. ``process_name`` is the on-line process
#: step, i.e. the equipment level.
_SW_HIERARCHY_FIELD_PROJECTIONS = {
    "asset_id": ("source_id", None),
    "asset_model": ("asset_type", None),
    "site_name": ("name", "site"),
    "area_name": ("name", "area"),
    "line_name": ("name", "line"),
    "process_name": ("name", "equipment"),
}

#: The Direct asset_hierarchy pseudo-table name.
_SW_HIERARCHY_TABLE = "asset_hierarchy"


@stage_timing.timed("io.source_sample")
def _sample_sitewise_assets(system_id: str, limit: int, *,
                            table: str = "", field: str = "") -> list[str]:
    """Distinct sample values for a SiteWise field, by the connector contract.

    Dispatches each ``<Table>.<column>`` through the per-connector field
    contracts (:data:`_SW_MODEL_FIELD_PROJECTIONS`,
    :data:`_SW_HIERARCHY_FIELD_PROJECTIONS`) instead of name-vs-path guesswork.
    The Direct ``asset_hierarchy`` table is not a model: its name columns scope
    to their exact ``a.level``. Any other table is a model table, scoped to
    ``a.asset_type = table``. A property column, or a contract column with no
    graph source (typeElementId / namespaceUri), returns []. No ``table`` keeps
    the historic all-names contract. An unknown model returns [] (see the
    contract constants for the type-id-fallback rationale).

    Only fixed fragments reach the query text; system/model/level/limit bind.
    """
    from .db import neptune_query, neptune_query_checked
    neptune_query = neptune_query_checked if _CHECKED_SOURCE_READS.get() else neptune_query
    column = (field or "").rsplit(".", 1)[-1].strip().lower()
    tbl = (table or "").strip()
    if not tbl:
        rows = neptune_query(
            "MATCH (a:Asset {system: $sid}) WHERE a.name IS NOT NULL "
            "RETURN DISTINCT a.name AS name LIMIT $lim",
            {"sid": system_id, "lim": int(limit)})
        return [r.get("name") for r in (rows or []) if r.get("name")]
    is_hierarchy = tbl.lower() == _SW_HIERARCHY_TABLE
    contract = (_SW_HIERARCHY_FIELD_PROJECTIONS if is_hierarchy
                else _SW_MODEL_FIELD_PROJECTIONS)
    spec = contract.get(column)
    if spec is None:  # property column, or a contract column with no graph source
        return []
    proj, level = spec
    params = {"sid": system_id, "lim": int(limit)}
    match = "MATCH (a:Asset {system: $sid})"
    if level is not None:
        match = "MATCH (a:Asset {system: $sid, level: $level})"
        params["level"] = level
    elif not is_hierarchy:
        match = "MATCH (a:Asset {system: $sid, asset_type: $model})"
        params["model"] = tbl
    if proj == "parent":  # parentElementId = the HAS_PARENT parent's source_id
        match += "-[:HAS_PARENT]->(p:Asset)"
        projection = "p.source_id"
    else:
        projection = _SW_ATTR_PROJECTIONS[proj]
    guard = f"{projection} IS NOT NULL"
    cypher = f"{match} WHERE {guard} RETURN DISTINCT {projection} AS name LIMIT $lim"
    rows = neptune_query(cypher, params)
    return [r.get("name") for r in (rows or []) if r.get("name")]


_budget_clock = time.monotonic   # module seam so tests can age a budget



# ---------------------------------------------------------------------------
# Tulip value sampling (IO behind the Tulip Tables REST API)
# ---------------------------------------------------------------------------

#: Fence tokens and markup that must not ride a vendor string into a prompt.
_TULIP_PROMPT_BREAKERS = re.compile(r"<<<|>>>|```|BUSINESS_CONTEXT")
#: Control characters, line breaks included: a vendor string stays one line.
_TULIP_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")
#: Longest sampled Tulip value kept as identity evidence (identifiers are short).
TULIP_SAMPLE_VALUE_MAX = 128


def valid_tulip_table_id(table_id) -> bool:
    """True when ``table_id`` is a plain Tulip id token safe to put in a URL
    (the connector's one rule, :func:`onboard_agent._valid_tulip_table_id`)."""
    from .onboard_agent import _valid_tulip_table_id
    return _valid_tulip_table_id(table_id)


def tulip_prompt_token(raw, max_len: int) -> str:
    """``raw`` (a Tulip-sourced label, column name or value) when it is safe to
    show a model verbatim: non-empty, at most ``max_len`` characters, one line,
    no control characters and no fence or markup tokens. ``""`` otherwise: the
    caller OMITS the item rather than mangling it into something misleading."""
    s = "" if raw is None else str(raw)
    if not s.strip() or len(s) > max_len:
        return ""
    if _TULIP_CONTROL.search(s) or _TULIP_PROMPT_BREAKERS.search(s):
        return ""
    return s


def _sample_tulip_records(system_id: str, connection_config: dict, limit: int,
                          field: str = "", table: str = "") -> list[str]:
    """Distinct non-empty values of ONE Tulip Table column from a bounded LIVE
    records read — the ``tulip`` registry adapter's ``sample_values`` delegate.

    A Tulip system persists NO Asset nodes to the graph (unlike SiteWise), so its
    cross-system join keys (run numbers, order ids, equipment tags) live only in
    Tulip Table columns. This makes them reachable by the identity-rules step:
    one bounded, read-only
    ``GET {prefix}/tables/{id}/records`` via the shared ``_TulipClient`` (owns the
    HTTP Basic auth, the response byte cap, and the workspace scope), returning up
    to ``limit`` distinct non-empty values of ``field`` (the column name;
    table-qualified ``table.column`` is accepted and the leading ``table.`` is
    stripped). The Tulip ``table_id`` is resolved from the saved discovery by the
    table NAME; without a resolvable id or a column, returns ``[]`` (no fabricated
    overlap). De-duplicated, order-preserving, capped at ``limit``; errors propagate.

    Connection precedence mirrors the Athena branch (node is the live
    source, ADR-0031): :func:`_tulip_connection_config` fills base_url /
    workspace_id / secret_arn a provisioned system's passed snapshot may lack;
    ``_TulipClient`` resolves the credentials from ``secret_arn`` — never here."""
    column = (field or "").strip()
    if table and column.startswith(f"{table}."):
        column = column[len(table) + 1:]
    column = column.split(".")[-1].strip()
    if not column:
        return []
    cfg = _tulip_connection_config(system_id, connection_config or {})
    base_url = (cfg.get("base_url") or "").strip()
    if not base_url:
        _unresolved_if_checked("Tulip connection unresolved")
        return []
    table_id = _tulip_table_id_for(system_id, table, cfg)
    if not table_id:
        return []
    rows = _tulip_sample_rows(cfg, base_url, table_id, int(limit))
    seen: set = set()
    out: list[str] = []
    for row in rows:
        v = row.get(column) if isinstance(row, dict) else None
        # Exact value (no trimming, so the membership probe agrees with it);
        # vendor text is untrusted, so only identifier-sized, single-line,
        # fence-free values become evidence a model reads.
        s = tulip_prompt_token(v, TULIP_SAMPLE_VALUE_MAX)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _tulip_sample_rows(cfg: dict, base_url: str, table_id: str, limit: int) -> list:
    """The bounded Tulip record rows for ``table_id`` via the shared client,
    scoped to the resolved workspace. Returns ``[]`` for an empty/non-list
    payload."""
    from .system_agent import _TulipClient
    client = _TulipClient(
        base_url=base_url, secret_arn=cfg.get("secret_arn", ""),
        token=cfg.get("token", ""), auth_scheme=cfg.get("auth_scheme", "") or "basic",
        workspace_id=cfg.get("workspace_id", ""), key_scope=cfg.get("key_scope", ""),
        system_id=cfg.get("system_id", ""),
        credential_source=cfg.get("credential_source", ""),
        table_label_prefix=cfg.get("table_label_prefix") or "")
    data = client.get(f"/api/v3/tables/{table_id}/records",
                      {"limit": max(1, min(int(limit), 100))})
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        recs = data.get("records")
        return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []
    return []


def _tulip_table_id_for(system_id: str, table: str, cfg: dict | None = None) -> str:
    """The Tulip ``table_id`` whose discovered NAME matches ``table``.

    The chat onboarding path saves each table's id + name in the session's
    ``discovered_tables`` (``_discover_tulip``); that session is read FIRST. The
    WIZARD (REST) path writes no session, so when no saved discovery has the
    table the id is resolved from the Tulip API itself — one bounded, read-only
    ``GET {prefix}/tables`` through the shared ``_TulipClient`` (same auth, byte
    cap, workspace scope and request-path allowlist), matching the discovered
    NAME (label) exactly and validated with :func:`valid_tulip_table_id` before
    any URL use. The name->id map is cached for the resolve job's lifetime
    (:func:`tulip_name_id_map`), so a resolve lists tables once per system, not
    once per field.

    ``""`` when there is no session AND the live list cannot resolve it (no
    connection, a malformed/absent id, or a failed request): the caller then
    returns ``[]`` / ``None`` (never a guessed id). ``cfg`` is the already
    resolved connection (:func:`_tulip_connection_config`) the live list needs;
    omitted ⇒ the session path only.

    Under a table scope (:func:`tulip_table_scope`) the session is not read:
    the id comes from the fresh scoped list alone. A seat-backed system whose
    seat is unavailable resolves to ``""`` (neither the session nor a list).

    ``table`` is matched EXACTLY as discovery saved it (no strip): Tulip labels
    ``"Orders"`` and ``"Orders "`` are two tables, and normalizing the name here
    would read one table's data for the other. A blank name resolves to ``""``."""
    name = table or ""
    if not name.strip():
        return ""
    state = tulip_seat_state(system_id, cfg) if cfg else None
    if state is not None:
        if state.kind == SEAT_UNAVAILABLE:
            _unresolved_if_checked("Tulip seat unavailable")
            return ""
        if _scope_of(state, cfg).prefix:
            # Under a table scope the fresh scoped list is the only source: a
            # session saved before the scope, or under another seat, may name a
            # table outside it or an id that is no longer this table's.
            return _tulip_name_id_map_get(system_id, cfg, state=state).get(name, "")
    tid = _tulip_table_id_from_session(system_id, name, state)
    if tid is not None:
        # The session answered (a match or an explicit absence): trust it, make
        # no live call. ``""`` here means the session exists and lacks the table.
        return tid
    # No usable session (the wizard path): resolve the id from the live list.
    return _tulip_name_id_map_get(system_id, cfg or {}, state=state).get(name, "")


def _tulip_table_id_from_session(system_id: str, name: str,
                                 state: "TulipSeatState | None" = None) -> "str | None":
    """The ``table_id`` for ``name`` from the saved discovery session, or ``""``
    when the session exists but has no such table, or names it for more than one
    table (ambiguous: fail closed, as the live list does). ``None`` (not ``""``)
    when there is NO session or it cannot be read, so the caller falls through
    to the live list rather than treating a missing session as a confirmed
    absence; also when a seat-backed system's session was produced by another
    seat (:func:`tulip_session_from_seat`, given the current ``state``). Saved
    names are compared exactly, as discovery saved them."""
    try:
        from .onboard_agent import _store
        from .onboarding_session import SessionNotFoundError
        try:
            session = _store().get_session(system_id)
        except SessionNotFoundError:
            return None
    except Exception as e:  # noqa: BLE001 - best-effort; fall through to the live list
        logger.info("tulip sample: session table-id lookup skipped for %s.%s: %s",
                    system_id, name, e)
        return None
    if state is not None and not tulip_session_from_seat(session, state):
        return None                                # another seat's ids: list afresh
    tables = session.get("discovered_tables") or []
    ids = {str(t.get("table_id")) for t in tables
           if isinstance(t, dict) and str(t.get("name") or t.get("label") or "") == name
           and t.get("table_id")}
    if len(ids) != 1:
        return ""
    tid = ids.pop()
    # Never put a vendor-supplied id that is not a plain token in a URL.
    return tid if valid_tulip_table_id(tid) else ""


# ---------------------------------------------------------------------------
# Tulip name->id map (the wizard-path fallback, listed once per resolve job)
# ---------------------------------------------------------------------------
#: Tables one live list reads (the connector's own listing bound; enforced
#: CLIENT-SIDE below — iteration stops at the cap whatever the server returns —
#: so a server ignoring ``limit`` cannot fan the list out).
_TULIP_LIST_MAX_TABLES = 100
#: Sentinel kept in the name->id map for a name that more than one table carries:
#: the lookup returns "" for it (fail closed), never an arbitrary first id.
_TULIP_AMBIGUOUS = ""
_tulip_table_id_memo: dict[tuple, tuple[float, dict]] = {}
_tulip_table_id_lock = threading.Lock()
# A process-random salt so the credential fingerprint in the cache key is a
# one-way, per-process value — the key never carries a token or secret, and the
# fingerprints are not comparable across processes (no cross-process leak).
_TULIP_CRED_SALT = token_bytes(16)


def _reset_tulip_table_id_memo() -> None:
    """Drop the cached Tulip name->id maps and seat misses (tests, redeploys)."""
    with _tulip_table_id_lock:
        _tulip_table_id_memo.clear()
        _tulip_seat_miss.clear()


def _tulip_resolved_token(cfg: dict) -> tuple[str, str]:
    """``(token, auth_scheme)`` the Tulip list will actually send, resolved the
    SAME way ``_TulipClient`` does: an inline ``token`` wins, else it is read
    from the ``secret_arn`` Secrets Manager secret. So a same-pointer secret
    ROTATION changes the token here (and thus the cache fingerprint), not just a
    pointer change. Fail-soft: a read error yields ``("", scheme)`` and the list
    then runs tokenless (and resolves nothing), as before."""
    token = cfg.get("token", "") or ""
    scheme = cfg.get("auth_scheme", "") or "basic"
    if not token and cfg.get("secret_arn"):
        try:
            from .system_agent import _fetch_credentials
            secret = _fetch_credentials(cfg["secret_arn"]) or {}
            token = secret.get("token", "") or ""
            scheme = secret.get("auth_scheme", scheme) or scheme
        except Exception as e:  # noqa: BLE001 - fail-soft; tokenless list resolves nothing
            logger.info("tulip table list: %s (credential read skipped)", type(e).__name__)
    return token, scheme


def _tulip_cred_fingerprint(token: str) -> str:
    """A short, non-reversible fingerprint of the resolved ``token``, salted per
    process. Put in the cache key so a credential change (new token, or a
    same-pointer rotation) misses the old tenant's map; the key itself NEVER
    holds the token or the secret. ``""`` (its own bucket) when there is no
    token."""
    if not token:
        return ""
    return blake2b(token.encode("utf-8"), salt=_TULIP_CRED_SALT, digest_size=12).hexdigest()


#: What a Tulip connection's seat is now (:func:`tulip_seat_state`).
INLINE, SEAT, SEAT_UNAVAILABLE = "inline", "seat", "seat_unavailable"
#: A full Secrets Manager secret ARN (a seat pointer is always one).
_SM_SECRET_ARN = re.compile(r"arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:.+")
#: Seat secret ARN -> monotonic time until which that seat is taken as
#: unavailable without another read (the seat secret cache's TTL). Holds no
#: secret value and no table data.
_tulip_seat_miss: dict[str, float] = {}
_TULIP_SEAT_MISS_MAX = 1000


class TulipSeatState(NamedTuple):
    """``kind`` is :data:`INLINE` (no seat: an inline or own-secret system),
    :data:`SEAT` (``request`` is the seat's validated ``TulipRequest``) or
    :data:`SEAT_UNAVAILABLE` (the seat cannot be read, or is not a complete
    contract): nothing may be read or trusted on that seat's behalf."""
    kind: str
    request: object = None


class TulipTableScope(NamedTuple):
    """The table scope a Tulip connection reads under: ``prefix`` (``""`` for
    every table), or ``seat_unavailable`` when its seat cannot say."""
    prefix: str = ""
    seat_unavailable: bool = False


def _tulip_seat_backed(system_id: str, cfg: dict) -> bool:
    """Whether ``cfg`` takes its routing, scope and key from a seat secret:
    its explicit ``credential_source`` marker when it has one, else a full
    Secrets Manager ARN that is not this system's own ``adt/conn/<id>``
    (``cfg``'s system id, else ``system_id``). Pure; reads nothing."""
    arn = str(cfg.get("secret_arn") or "")
    if not arn:
        return False
    source = str(cfg.get("credential_source") or "").strip()
    if source:
        return source == "secret_arn"
    if not _SM_SECRET_ARN.fullmatch(arn):
        return False
    from .connection_resolver import is_own_connection_secret
    return not is_own_connection_secret(arn, str(cfg.get("system_id") or system_id or ""))


def tulip_seat_state(system_id: str, cfg: dict | None) -> TulipSeatState:
    """The :class:`TulipSeatState` of a Tulip ``cfg``, from ONE validated read
    of its seat: the same TTL-cached read the ``_TulipClient`` sends with
    (``system_agent._tulip_secret``). A seat that cannot be read or is not a
    complete contract is remembered as unavailable for the seat cache's TTL
    (:data:`_tulip_seat_miss`), so it is not read again on every lookup."""
    cfg = cfg or {}
    if not _tulip_seat_backed(system_id, cfg):
        return TulipSeatState(INLINE)
    from . import connection_resolver as _cr
    from . import tulip_seat_contract as _tsc
    from .system_agent import _tulip_secret
    arn = str(cfg["secret_arn"])
    now = _cr._monotonic()
    if _tulip_seat_miss.get(arn, 0.0) > now:
        return TulipSeatState(SEAT_UNAVAILABLE)
    try:
        return TulipSeatState(SEAT, _tsc.request_tuple(_tulip_secret(arn)))
    except Exception as exc:  # noqa: BLE001 - unreadable/incomplete seat: fail closed
        logger.warning("tulip seat for %s is unavailable (%s): nothing is read or "
                       "trusted through it", system_id, type(exc).__name__)
        with _tulip_table_id_lock:
            if len(_tulip_seat_miss) >= _TULIP_SEAT_MISS_MAX:
                _tulip_seat_miss.clear()
            _tulip_seat_miss[arn] = now + float(_cr._TTL)
        return TulipSeatState(SEAT_UNAVAILABLE)


def _scope_of(state: TulipSeatState, cfg: dict | None) -> TulipTableScope:
    if state.kind == SEAT_UNAVAILABLE:
        return TulipTableScope(seat_unavailable=True)
    if state.kind == SEAT:
        return TulipTableScope(prefix=state.request.table_label_prefix)
    return TulipTableScope(prefix=(cfg or {}).get("table_label_prefix") or "")


def tulip_session_from_seat(session: dict | None, state: TulipSeatState) -> bool:
    """Whether a saved onboarding session's discovery may be used for the seat
    a Tulip system reads now (``state``, :func:`tulip_seat_state`): always for
    an inline system; for a seat, only when the session recorded that same
    seat (its ``discovery_seat`` equals the current tuple's
    ``tulip_seat_contract.request_identity``), scoped or not. A session saved
    before seats were recorded has none and is not trusted; neither is any
    session of an unavailable seat. Pure: the caller already read both."""
    if state.kind == INLINE:
        return True
    if state.kind != SEAT:
        return False
    from .tulip_seat_contract import request_identity
    saved = (session or {}).get("discovery_seat")
    return isinstance(saved, dict) and saved == request_identity(state.request)


def tulip_table_scope(system_id: str, cfg: dict | None) -> TulipTableScope:
    """The table scope a Tulip ``cfg`` reads under now: the seat's (read
    fresh) for a seat-backed system, else the cfg's own ``table_label_prefix``;
    ``seat_unavailable`` when the seat cannot be read, never an unscoped
    answer in its place."""
    if not cfg:
        return TulipTableScope()
    return _scope_of(tulip_seat_state(system_id, cfg), cfg)


def _tulip_list_identity(system_id: str, cfg: dict, state: TulipSeatState) -> tuple:
    """The cache key of a Tulip table list of ``cfg``. A seat's comes from its
    validated tuple (``state.request``): URL, workspace, key scope,
    ``table_label_prefix`` and a salted fingerprint of the key it sends (the
    Basic token derived from the seat's raw ``api_key``/``api_secret``, or its
    explicit token), so a seat switch, a scope change or a key rotation misses
    a map an earlier seat answered. An inline cfg keeps its own URL, workspace,
    token fingerprint and ``table_label_prefix``. Never a credential value."""
    if state.kind == SEAT:
        req = state.request
        return ("seat", system_id, req.base_url, req.workspace_id, req.key_scope,
                req.table_label_prefix, _tulip_cred_fingerprint(req.token))
    token, _scheme = _tulip_resolved_token(cfg)
    return (system_id, (cfg.get("base_url") or "").strip(),
            (cfg.get("workspace_id") or "").strip(),
            _tulip_cred_fingerprint(token), cfg.get("table_label_prefix") or "")


def tulip_name_id_map(system_id: str, cfg: dict, *, seat_request=None) -> "dict | None":
    """``{table_name: table_id}`` from ONE bounded, read-only live
    ``GET {prefix}/tables`` through the shared ``_TulipClient`` (workspace-scoped,
    byte-capped, request-path allowlisted).

    Only the first :data:`_TULIP_LIST_MAX_TABLES` tables are read (CLIENT-SIDE
    bound), and only tables whose id is a plain token (:func:`valid_tulip_table_id`)
    are kept. The key is the table's RAW ``label``/``name`` — the exact string
    ``onboard_agent._tulip_tables_from_list`` saves as the table ``name`` (no
    strip on either side), so the sampler/probe/seed match by the same string.
    A name more than one kept table carries is AMBIGUOUS and resolves to ``""``
    (fail closed), never an arbitrary first id.

    ``None`` (not ``{}``) when the connection has no base URL or the list
    request FAILS, so the caller does not cache a failure as an empty listing;
    ``{}`` only when the list answered but nothing was URL-safe. This is the
    uncached worker; callers use :func:`_tulip_name_id_map_get` for the
    credential-scoped cache. ``seat_request`` is a seat's validated tuple the
    caller already read: the list is sent with it, not a second read."""
    base_url = (cfg.get("base_url") or "").strip()
    if not base_url:
        return None
    try:
        return _tulip_list_name_id(cfg, base_url, seat_request)
    except Exception as e:  # noqa: BLE001 - best-effort id resolution (fail closed)
        logger.info("tulip table list skipped for %s: %s", system_id, e)
        return None


def _tulip_list_client(cfg: dict, base_url: str, seat_request=None):
    """The ``_TulipClient`` a table list is sent with. Given a seat's validated
    tuple, the client sends with that tuple (no further read); else it uses the
    resolved token, so the request and the cache fingerprint agree."""
    from .system_agent import _TulipClient
    if seat_request is not None:
        return _TulipClient(base_url=base_url, secret_arn=cfg.get("secret_arn", ""),
                            system_id=cfg.get("system_id", ""),
                            credential_source="secret_arn", seat_request=seat_request)
    token, scheme = _tulip_resolved_token(cfg)
    return _TulipClient(
        base_url=base_url, secret_arn=cfg.get("secret_arn", ""),
        token=token, auth_scheme=scheme,
        workspace_id=cfg.get("workspace_id", ""), key_scope=cfg.get("key_scope", ""),
        system_id=cfg.get("system_id", ""),
        credential_source=cfg.get("credential_source", ""),
        table_label_prefix=cfg.get("table_label_prefix") or "")


@stage_timing.timed("io.source_sample")
def _tulip_list_name_id(cfg: dict, base_url: str, seat_request=None) -> dict:
    """The bounded ``GET {prefix}/tables`` the map is built from; its own seam so
    the one list read counts as source sampling. Raises on a failed request (the
    caller answers ``None``). The client is :func:`_tulip_list_client`'s."""
    client = _tulip_list_client(cfg, base_url, seat_request)
    data = client.get("/api/v3/tables", {"limit": _TULIP_LIST_MAX_TABLES})
    rows = data if isinstance(data, list) else (
        data.get("tables") if isinstance(data, dict) else None)
    out: dict = {}
    seen = 0
    for t in (rows or []):
        if seen >= _TULIP_LIST_MAX_TABLES:
            break                                  # client-side bound, server ignored
        seen += 1
        if not isinstance(t, dict) or not valid_tulip_table_id(t.get("id")):
            continue
        # The RAW name exactly as onboard_agent._tulip_tables_from_list saves it
        # (str(label or name or tid), no strip), so matching agrees with the
        # session path and _discover_tulip.
        name = str(t.get("label") or t.get("name") or "")
        tid = t.get("id")
        if not name:
            continue
        if name in out and out[name] != tid:
            out[name] = _TULIP_AMBIGUOUS           # two tables, same name -> fail closed
        elif name not in out:
            out[name] = tid
    return out


def _tulip_name_id_map_get(system_id: str, cfg: dict, *,
                           state: "TulipSeatState | None" = None) -> dict:
    """:func:`tulip_name_id_map`, cached for the resolve job's lifetime per
    connection identity (:func:`_tulip_list_identity`: for a seat-backed
    system the seat's URL, workspace, key scope, table scope and key, from the
    one read that also sends the list). An unavailable seat answers ``{}``
    with no cached map served or stored and no list sent. ``state`` is the
    caller's :func:`tulip_seat_state`, when it has one. Cached with the
    sample memo's TTL, so a resolve lists a Tulip system's tables ONCE across its
    fields, a CREDENTIAL change (new token or same-pointer rotation) or a seat
    or scope switch misses the old map, and the key holds no secret. Only a
    SUCCESSFUL listing is cached
    (``None`` from a failed request is NOT, so a transient failure is retried on
    the next call); a successful empty map (the list answered, nothing URL-safe)
    IS cached. Returns ``{}`` for a failure (the caller reads ids with
    ``.get(name, "")``)."""
    base_url = (cfg.get("base_url") or "").strip()
    if not base_url:
        return {}
    state = state if state is not None else tulip_seat_state(system_id, cfg)
    if state.kind == SEAT_UNAVAILABLE:
        _unresolved_if_checked("Tulip seat unavailable")
        return {}                                  # fail closed: no map, cached or new
    key = _tulip_list_identity(system_id, cfg, state)
    now = _memo_clock()
    with _tulip_table_id_lock:
        hit = _tulip_table_id_memo.get(key)
        if hit is not None and hit[0] > now:
            return dict(hit[1])
    result = tulip_name_id_map(system_id, cfg, seat_request=state.request)
    if result is None:
        _unresolved_if_checked("Tulip table list failed")
        return {}                                  # failed list -> not cached, retried
    with _tulip_table_id_lock:
        if len(_tulip_table_id_memo) >= _SAMPLE_MEMO_MAX_ENTRIES:
            for stale in [k for k, (exp, _m) in _tulip_table_id_memo.items() if exp <= now]:
                del _tulip_table_id_memo[stale]
            if len(_tulip_table_id_memo) >= _SAMPLE_MEMO_MAX_ENTRIES:
                _tulip_table_id_memo.clear()
        _tulip_table_id_memo[key] = (now + SAMPLE_MEMO_TTL_SECONDS, dict(result))
    return result


#: What a resolved Tulip connection can carry (all NON-secret except secret_arn,
#: which is a POINTER, not a credential value). ``credential_source`` is the
#: non-secret marker that tells an ARN-sourced seat system from an inline one;
#: ``table_label_prefix`` the optional table scope (a seat-backed client reads
#: the seat's own at use time).
_TULIP_CONN_KEYS = ("base_url", "workspace_id", "key_scope", "secret_arn",
                    "auth_scheme", "token", "credential_source", "table_label_prefix")


def _tulip_connection_config(system_id: str, connection_config: dict) -> dict:
    """Fill base_url / workspace_id / secret_arn / auth_scheme for a Tulip value
    sample. The credentials are never inline — ``_TulipClient`` resolves them from
    ``secret_arn`` (adt/conn/<id>). Sources, per-key, passed config wins then the
    node (``resolve_connection`` — the live source, ADR-0031) then the catalog
    entry (whose top-level ``secret_arn`` is written at registration). A USER
    registration is restricted to its OWN credential pointer. Best-effort — any
    lookup failure leaves the passed config as-is."""
    cfg = dict(connection_config or {})
    if system_id and not (cfg.get("base_url") and cfg.get("secret_arn")):
        try:
            from .connection_resolver import resolve_connection
            node = resolve_connection(system_id) or {}
        except Exception:  # noqa: BLE001 - best-effort; keep the passed config
            node = {}
        for key in _TULIP_CONN_KEYS:
            if not cfg.get(key) and node.get(key):
                cfg[key] = node[key]
        if not (cfg.get("base_url") and cfg.get("secret_arn")):
            try:
                from .catalog import catalog
                entry = catalog.get(system_id) or {}
            except Exception:  # noqa: BLE001 - best-effort
                entry = {}
            entry_cfg = entry.get("connection_config") or {}
            for key in ("base_url", "workspace_id", "auth_scheme", "table_label_prefix"):
                if not cfg.get(key) and entry_cfg.get(key):
                    cfg[key] = entry_cfg[key]
            if not cfg.get("secret_arn") and entry.get("secret_arn"):
                cfg["secret_arn"] = entry["secret_arn"]
        # Pre-provision fallback (bug H2): a USER registration that gave only
        # the ARN has NO base_url on its raw entry (the secret supplies it) and
        # no node yet, so the node + entry fills above leave base_url empty and
        # the sampler/probe cannot reach the source. registration_connection is
        # ARN-aware: it resolves the whole routing tuple (base_url, workspace_id,
        # key_scope, credential_source) from the named seat secret. Fill only
        # the non-secret routing/marker fields a miss would otherwise leave
        # blank; the credential itself is still resolved by _TulipClient.
        if system_id and not cfg.get("base_url"):
            cfg = _tulip_fill_from_registration(system_id, cfg)
    cfg = _tulip_own_pointer_only(system_id, cfg)
    if system_id and not cfg.get("system_id"):
        # Stamp the system id so the _TulipClient the sampler/probe/table-list
        # build can tell an ARN-sourced seat secret from an own adt/conn/<id>.
        cfg["system_id"] = system_id
    return cfg


def _tulip_fill_from_registration(system_id: str, cfg: dict) -> dict:
    """Fill a Tulip cfg's missing NON-secret routing + ``credential_source`` from
    ``connection_resolver.registration_connection`` (ARN-aware) for a USER
    registration whose pre-provision base_url lives in its named seat secret.
    Best-effort: a non-user entry, an unresolved secret, or any failure leaves
    ``cfg`` unchanged (never fabricates a routing value)."""
    try:
        from .catalog import catalog
        from .connection_resolver import is_user_registration, registration_connection
        entry = catalog.get(system_id) or {}
        if not is_user_registration(entry):
            return cfg
        registered = registration_connection(system_id, entry) or {}
    except Exception:  # noqa: BLE001 - best-effort, as the other fallbacks
        return cfg
    if registered.get("_secret_unresolved"):
        return cfg
    for key in ("base_url", "workspace_id", "key_scope", "auth_scheme",
                "secret_arn", "credential_source", "table_label_prefix"):
        if not cfg.get(key) and registered.get(key):
            cfg[key] = registered[key]
    return cfg


def _tulip_own_pointer_only(system_id: str, cfg: dict) -> dict:
    """A USER Tulip registration samples only through its OWN credential pointer
    (``connection_resolver.own_registration_pointer``), never a secret by name
    or another system's. The ONE exception is an ARN-SOURCED system
    (``credential_source == "secret_arn"``): its credentials live in the
    request-NAMED seat secret, not an ``adt/conn/<id>``, so its pointer (the
    node's, else the entry's) is kept. Demo / legacy / unmarked systems and a
    catalog read failure leave ``cfg`` unchanged."""
    if not system_id:
        return cfg
    try:
        from .catalog import catalog
        entry = catalog.get(system_id) or {}
    except Exception:  # noqa: BLE001 - best-effort, as the fallbacks above
        return cfg
    from .connection_resolver import (
        _is_arn_sourced, _node_connection, is_user_registration, own_registration_pointer,
    )
    if not is_user_registration(entry):
        return cfg
    try:
        node = _node_connection(system_id)
    except Exception:  # noqa: BLE001 - best-effort
        node = {}
    if _is_arn_sourced(entry, node):
        # The named seat secret (the node's pointer when provisioned, else the
        # entry's); the resolver re-checks the allowlist on every read.
        arn = str((node or {}).get("secret_arn") or entry.get("secret_arn") or "")
        return {**cfg, "secret_arn": arn} if arn else cfg
    return {**cfg, "secret_arn": own_registration_pointer(system_id, entry)}


# ---------------------------------------------------------------------------
# Coverage-gap recommendations (optional, never required)
# ---------------------------------------------------------------------------

def coverage_gap_recommendations(overlap_by_concept: dict) -> list[dict]:
    """Emit OPTIONAL source-metadata recommendations for un-bridgeable concepts.

    ``overlap_by_concept`` maps ``concept -> best measured overlap_count`` across
    all candidate field pairs for that shared concept. When a concept is shared
    across systems but the best measured overlap is 0 (no in-data value bridge),
    a direct value join isn't evidenced — so we surface a soft, optional
    recommendation. This NEVER assumes a source change is made; it hands the
    user an informed option (confirm a ValueMapping manually, or ask the source
    team to expose a shared key "for best results").
    """
    recs = []
    for concept, best_overlap in sorted(overlap_by_concept.items()):
        if best_overlap == 0:
            recs.append({
                "concept": concept,
                "severity": "info",
                "message": (
                    f"Systems share the '{concept}' concept but no overlapping values were found "
                    f"in the sampled data, so a direct value join isn't evidenced. If these should "
                    f"link, confirm a ValueMapping manually. For best results (optional), the team "
                    f"maintaining the source could expose a shared '{concept}' key/identifier — "
                    f"ideally a low-effort metadata addition."
                ),
            })
    return recs
