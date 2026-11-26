# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Replica-coherent Tier-3 provenance-query store (provenance plan, Task 3.1).

Thin wrapper over :class:`agents.job_store.JobStore` with
``kind="provenance-query"`` — DynamoDB write-through on the existing
onboarding-sessions table, fail-soft to in-memory, TTL'd; NO new
infrastructure. The Wave-3 sidecar ingest (``chat_client``, Task 3.3) writes
turn entries here; the admin-gated redemption route (Task 3.4) reads them
back by ``(session_id, query_ref)``.

WHY a durable shared store (risk #5): SSE ingest and the Tier-2 card's
redemption ``GET`` can hit DIFFERENT ECS replicas — the exact failure class
the ``JobStore`` machinery already fixed for the onboarding job state. The
fail-soft in-memory path degrades to an honest "unknown or expired" miss
(``None`` here ⇒ structured 404 at the route), never a hang or fabricated
query text.

Contracts (frozen — D5/D6):

- Entry shape mirrors the D5 redemption body: ``{"query_ref", "session_id",
  "system_id", "tool_name", "executed_query", "kg_artifacts",
  "captured_at"}`` (sidecar spellings ``query_text``/``kg_artifact_ids``
  are normalized at write so the route returns the stored dict as-is).
  Trusted system-agent sidecars may add validated ``query_fingerprint`` and
  ``query_shape_version`` fields; legacy entries remain byte-identical.
- Bounds enforced AT WRITE: ≤``MAX_ENTRIES_PER_TURN`` entries per turn and
  ≤``MAX_QUERY_TEXT_BYTES`` query text per entry — an over-cap entry is
  stored HONESTLY with ``executed_query: ""`` and
  ``note: "query text exceeded bound"``, never truncated mid-SQL silently.
- An entry with no query text may carry the one note a writer can supply,
  ``API_READ_NOTE`` (a source read through its API has no query text); any
  other writer-supplied note is dropped at write.
- ``query_ref`` keys must match the D3 handle shape (``q-`` + 12 hex);
  anything else is dropped at write and misses at read.
- Logging NEVER carries query text: lengths and ``sha256[:12]`` digests
  only (the ``_redact_digits`` claim-check posture applied to this store —
  no fragment of the query, masked or not, reaches a log line).
"""
from __future__ import annotations

import hashlib
import logging
import re

from agents.job_store import JobStore, JobStoreUnavailableError
from agents.provenance import (MAX_SYSTEM_ID_LEN, MAX_TOOL_NAME_LEN,
                               ROW_EXCLUSIONS_OMITTED_KEY, entry_row_exclusions)
from agents.query_shape import QUERY_SHAPE_VERSION

logger = logging.getLogger(__name__)

#: JobStore namespace for this family (shared-table key prefix).
_KIND = "provenance-query"

#: DELIBERATE per-kind TTL for stored query entries (MED — not JobStore's
#: incidental 1 h async-job default): Tier-3 transcripts get reopened across
#: a workday — a steward returning to a morning conversation in the
#: afternoon must still be able to redeem its ``query_ref`` handles, so 24 h
#: matches the actual read pattern. The longer horizon is safe because the
#: rows are strictly bounded at write (entries ≤``MAX_QUERY_TEXT_BYTES`` =
#: 16 KB each, ≤``MAX_ENTRIES_PER_TURN`` = 32 per turn) and expiry degrades
#: to the route's honest structured 404 ("Unknown or expired query_ref").
PROVENANCE_QUERY_TTL_SECONDS = 24 * 3600

#: Hard cap on sidecar entries persisted per turn (D6).
MAX_ENTRIES_PER_TURN = 32

#: Hard cap on stored query text per entry (D6: ≤16 KB, UTF-8 bytes).
MAX_QUERY_TEXT_BYTES = 16 * 1024

#: Honest marker stored in place of over-cap query text (D6).
OVER_BOUND_NOTE = "query text exceeded bound"

#: The one note a writer may supply: a call that ran no query and read its
#: source through the source's API (``system_agent._write_provenance_sidecar``)
#: has no query text, and without this the card would read "executed query
#: not captured", which sounds like a capture failure. Stored only exactly,
#: and only on an entry with no query text (``_additive_fields``). The chat
#: carries the same text (``frontend/src/api/provenance.ts``, parity-pinned).
API_READ_NOTE = "read through the source's API; there is no query text"

#: D3 handle shape — the ONE home for this validation (the redemption route
#: imports it from here rather than re-growing a copy).
QUERY_REF_RE = re.compile(r"^q-[0-9a-f]{12}$")
_QUERY_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

#: Mirrors ``agents_trace_routes._SESSION_ID_RE`` (not imported — that
#: module carries route dependencies this store must stay free of).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,256}$")

#: Public alias (Wave 6, additive): the investigation-trace store and its
#: redemption route import THIS spelling — no second session-id regex home.
SESSION_ID_RE = _SESSION_ID_RE

#: Metadata field bounds (metadata only — the no-silent-truncation rule
#: applies to query text, which is never cut, per D6). The system-id and
#: tool-name bounds are IMPORTED from ``agents.provenance`` (the pure D1
#: home) so record and store can never drift.
_MAX_SYSTEM_ID_LEN = MAX_SYSTEM_ID_LEN
_MAX_TOOL_NAME_LEN = MAX_TOOL_NAME_LEN
_MAX_TIMESTAMP_LEN = 120
_MAX_KG_ARTIFACTS = 32
_MAX_KG_ARTIFACT_LEN = 200

# Lazily-constructed module singleton; tests inject a JobStore built on a
# fake table by assigning ``provenance_store._job_store`` directly.
_job_store: JobStore | None = None


def _jobs() -> JobStore:
    """The one ``JobStore(kind="provenance-query")`` for this process —
    with the DELIBERATE 24 h ``PROVENANCE_QUERY_TTL_SECONDS`` (see the
    constant's rationale), not the 1 h async-job default."""
    global _job_store
    if _job_store is None:
        _job_store = JobStore(kind=_KIND,
                              ttl_seconds=PROVENANCE_QUERY_TTL_SECONDS)
    return _job_store


def digest12(text: str) -> str:
    """``sha256[:12]`` — the ONLY form in which identifying values or query
    text may appear in a log line (lengths + hashes only). Exported: the
    redemption route imports this rather than re-growing a copy."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Per-call write capability (no new infrastructure; no caller identity).
# --------------------------------------------------------------------------- #
# The reserved ``_adt_provenance`` arg is a request any gateway caller can
# make, and the key it selects (``session_id``/``query_ref``) is public to the
# owner of a completed answer. Without a capability a caller could replay a
# completed answer's pair in a later direct tool call and OVERWRITE that
# answer's provenance/trace evidence (an unconditional ``PutItem``), so an
# admin opening technical detail later would see the replay filed as the
# original call's evidence.
#
# Fix: the app mints a fresh high-entropy write token per DELEGATED remote
# call, writes a DURABLE server-side capability record (shared table only,
# never process memory) mapping the public ``(session_id, query_ref)`` to an
# opaque physical key suffix ``sha256(token | session_id | query_ref)``, and
# forwards the token INSIDE the reserved arg ONLY when that write landed. The
# runtime, holding the token, writes the entry under that SAME opaque suffix.
#
# Readers resolve through the durable capability record:
#   * a record exists  -> read ONLY the capability key; a missing entry is a
#     MISS, never a legacy-key read (so a tokenless replay of the public pair,
#     which can only reach a DIFFERENT suffix or the legacy key, is never
#     returned, even when the real entry is absent/delayed/expired);
#   * no record exists (authoritative, on a store with a shared table, or a
#     memory-only store that holds no durable records) -> read the legacy
#     ``<sid>:<ref>`` key, for local mode and records written before this
#     change, which no external caller can write remotely;
#   * the capability store cannot be read (a transient table error on a
#     shared-table store) -> fail CLOSED to a miss, never the legacy key.
#
# The runtime writes NOTHING for a remote call that carries no valid token,
# because every remote issuer now forwards one. A capability write that does
# not land (no shared table, throttle, error) forwards no token and creates no
# record, so the runtime writes nothing and the answer is unaffected.
#
# The token is NEVER emitted in SSE, provenance frames, logs, URLs, the Quick
# bridge, or stored conversations. Only its one-way suffix is stored, and only
# ``sha256[:12]`` digests are logged.

#: JobStore namespace for capability records (shared table only).
_CAP_KIND = "provenance-cap"

#: The capability must outlive every entry it protects: it is written BEFORE
#: the delegated tool runs, the entry AFTER, so with equal horizons the
#: capability would expire first (by the call duration) and a reader would
#: fall back to legacy in that gap. Give the capability a longer horizon with
#: generous margin so it always outlives the entries it resolves.
CAPABILITY_TTL_SECONDS = PROVENANCE_QUERY_TTL_SECONDS + 3600

#: A valid write token: 24-64 lowercase hex chars (``secrets.token_hex(16..32)``).
_WRITE_TOKEN_RE = re.compile(r"^[0-9a-f]{24,64}$")

#: Lazily-constructed capability-store singleton (tests inject a fake table by
#: assigning ``provenance_store._cap_store`` directly).
_cap_store: JobStore | None = None


def _caps() -> JobStore:
    """The one ``JobStore(kind="provenance-cap")`` for this process, with the
    longer ``CAPABILITY_TTL_SECONDS`` so a capability never expires before the
    entry it resolves."""
    global _cap_store
    if _cap_store is None:
        _cap_store = JobStore(kind=_CAP_KIND,
                              ttl_seconds=CAPABILITY_TTL_SECONDS)
    return _cap_store


def valid_write_token(value: object) -> bool:
    """A reserved-arg write token is 24-64 lowercase hex chars, else invalid
    (``None``/malformed ⇒ no capability)."""
    return isinstance(value, str) and bool(_WRITE_TOKEN_RE.match(value))


def capability_suffix(write_token: str, session_id: str, query_ref: str) -> str:
    """The opaque physical key suffix for a per-call write token: a one-way
    ``sha256(token | session_id | query_ref)``. The runtime derives it from
    the token it holds; the app stores it in the capability record. The token
    is never recoverable from the suffix and never leaves the server."""
    return hashlib.sha256(
        f"{write_token}|{session_id}|{query_ref}".encode("utf-8")).hexdigest()


def put_capability(session_id: str, query_ref: str, write_token: str) -> bool:
    """APP-ONLY: durably record the capability for one delegated remote call
    (the orchestrator / onboarding verifier writes this before the tool call).
    Maps the public ``(session_id, query_ref)`` to the opaque suffix so readers
    can resolve the entry the runtime writes under the token.

    Returns ``True`` ONLY when the shared-table write landed (``put_durable``);
    a memory-only store, a table that cannot be opened, or a throttled/failed
    write returns ``False`` and keeps NOTHING in process memory. The caller
    forwards the token only on ``True``, so a capability a cross-process reader
    could not see is never created. The runtime never writes this record, so a
    forger cannot plant one."""
    if not (_valid_session_id(session_id)
            and isinstance(query_ref, str) and QUERY_REF_RE.match(query_ref)
            and valid_write_token(write_token)):
        return False
    suffix = capability_suffix(write_token, session_id, query_ref)
    return _caps().put_durable(f"{session_id}:{query_ref}", {"suffix": suffix})


def issue_provenance_arg(session_id: str, query_ref: str) -> dict:
    """Mint a fresh per-call write token, DURABLY record the capability, and
    return the reserved-arg value: the ONE issuing home every remote stamper
    (``orchestrator`` gateway wrapper, ``onboard_verify_agent``) calls.

    The token rides the reserved arg ONLY when the capability record landed
    (``put_capability`` returned ``True``); otherwise the value carries no
    token and the runtime, which writes only with a valid token, writes
    nothing for that call (its answer is unaffected). Never raises: any fault
    yields a tokenless value, i.e. no evidence recorded, never a legacy write.
    The token is server-only (stripped at the ASGI seam; never in
    SSE/frames/logs/URLs)."""
    import secrets

    from .provenance import provenance_context

    token = secrets.token_hex(16)
    try:
        recorded = put_capability(session_id, query_ref, token)
    except Exception:  # nosec B110 - capability is best-effort; never break the call
        recorded = False
    return provenance_context(session_id, query_ref, token if recorded else None)


#: ``_resolve_suffix`` outcomes: a suffix string, or one of these sentinels.
_CAP_ABSENT = "absent"      # authoritative: no capability record (legacy allowed)
_CAP_UNKNOWN = "unknown"    # cannot tell (store read failed): fail closed


def _resolve_suffix(session_id: str, query_ref: str) -> str:
    """The capability suffix for ``(session_id, query_ref)``, or a sentinel:

    * ``_CAP_ABSENT``: no durable capability record exists (a shared-table
      store that answered 'absent', or a memory-only store that holds none);
      the reader may use the legacy key.
    * ``_CAP_UNKNOWN``: a shared-table store could not be read (transient
      error); the reader must fail closed, never the legacy key.
    """
    caps = _caps()
    if not caps.has_shared_table():
        return _CAP_ABSENT  # memory-only: no durable records, legacy allowed
    record = caps.get_durable(f"{session_id}:{query_ref}")
    if isinstance(record, dict):
        suffix = record.get("suffix")
        if isinstance(suffix, str) and suffix:
            return suffix
        # A record EXISTS but is malformed (no valid suffix). It is still an
        # existing capability record, so fail CLOSED: never serve the legacy
        # key, which a tokenless replay could have written.
        return _CAP_UNKNOWN
    # get_durable returns None for both 'absent' and 'read error'; re-probe
    # strictly to tell them apart so a transient error never serves legacy.
    try:
        probe = caps.get_strict(f"{session_id}:{query_ref}")
    except JobStoreUnavailableError:
        return _CAP_UNKNOWN
    return _CAP_ABSENT if probe is None else _CAP_UNKNOWN


def read_entry(store: JobStore, session_id: str, query_ref: str, *,
               consistent: bool = False) -> "dict | None":
    """Resolve and read one entry for ``(session_id, query_ref)`` from
    ``store`` (a query or trace JobStore), enforcing the capability.

    A capability record binds the call to an opaque key; when one exists the
    entry is read ONLY from that key and a miss is a miss: the legacy key is
    NEVER consulted, so a tokenless replay of the public pair cannot be served
    even if the legitimate entry is absent, delayed or expired. The legacy key
    is read only when there is authoritatively no capability record (local
    mode / pre-change records). A capability store that cannot be read fails
    closed to a miss. ``None`` on miss. Shared by the query and trace stores.
    ``consistent`` reads a store with a shared table by ``get_durable``
    (``ConsistentRead``, never memory; a failed read is a miss)."""
    suffix = _resolve_suffix(session_id, query_ref)
    if suffix == _CAP_UNKNOWN:
        return None
    key = (f"{session_id}:{query_ref}" if suffix == _CAP_ABSENT else f"cap:{suffix}")
    entry = (store.get_durable(key) if consistent and store.has_shared_table()
             else store.get(key))
    return entry if isinstance(entry, dict) else None


class FrameReader:
    """The sidecar reader for ONE provenance frame: :func:`get_query`, which
    the frame assembly injects, plus a count of capability misses.

    A miss is a delegated call whose capability record exists and under whose
    capability the runtime wrote nothing: no query entry and no investigation
    trace. The runtime writes its trace for every answered call, and both
    before its result returns, so at frame assembly a miss is an anomaly. A
    call that ran no query has a trace and no query entry, and is not a miss.
    When a legacy-keyed entry exists for a missed pair, the runtime that
    answered predates the capability, which is a version mismatch between it
    and this orchestrator. The entry itself is read only through
    :func:`get_query`, unchanged; the miss check runs only when that read
    found nothing, and tests the other keys for existence alone
    (``JobStore.get_durable``, never cached, never returned), so the record
    stays as the fail-closed reader leaves it.
    """

    def __init__(self) -> None:
        self.misses = 0
        self.legacy = 0

    def __call__(self, session_id: str, query_ref: str) -> "dict | None":
        entry = get_query(session_id, query_ref)
        if entry is None:
            self._check_miss(session_id, query_ref)
        return entry

    def _check_miss(self, session_id: str, query_ref: str) -> None:
        try:
            suffix = _durable_suffix(session_id, query_ref)
            if suffix is None:
                return  # no capability (the legacy key was read), or none readable
            from . import investigation_trace_store as traces
            if traces.stored_durably(session_id, query_ref, suffix):
                return  # answered under the capability, with no query to record
            self.misses += 1
            if (_stored_durably(session_id, query_ref)
                    or traces.stored_durably(session_id, query_ref)):
                self.legacy += 1
        except Exception as exc:  # noqa: BLE001 - a count; never touches the frame
            logger.debug("provenance_store: miss check skipped (%s)", type(exc).__name__)

    def log_misses(self, session_id: str) -> None:
        """ONE warning for this frame's misses, digests and counts only."""
        if not self.misses:
            return
        if self.legacy:
            cause = (f"{self.legacy} have a legacy-keyed entry, so the system agent that "
                     "answered likely runs older code than this orchestrator "
                     "(version mismatch)")
        else:
            cause = "nothing was written under their capabilities"
        logger.warning(
            "PROVENANCE_CAPABILITY_MISS sid_sha=%s refs=%d legacy=%d: %s; "
            "no captured evidence is added to their citations",
            digest12(str(session_id)), self.misses, self.legacy, cause)


def _durable_suffix(session_id: str, query_ref: str) -> "str | None":
    """The pair's capability suffix from ONE consistent shared-table read, or
    ``None`` when there is no shared table, no record, a read error or a
    malformed record. For counting misses only: the reader's own
    absent/unknown distinction (:func:`_resolve_suffix`) is not needed here,
    because every ``None`` case is one that is not reported."""
    if not (_valid_session_id(session_id)
            and isinstance(query_ref, str) and QUERY_REF_RE.match(query_ref)):
        return None
    caps = _caps()
    record = caps.get_durable(f"{session_id}:{query_ref}") if caps.has_shared_table() else None
    suffix = record.get("suffix") if isinstance(record, dict) else None
    return suffix if isinstance(suffix, str) and suffix else None


def _stored_durably(session_id: str, query_ref: str) -> bool:
    """Whether the shared table holds a query entry under the pair's legacy
    key. Existence only: ``JobStore.get_durable`` never caches, and the entry
    is not returned."""
    return _jobs().get_durable(f"{session_id}:{query_ref}") is not None


def _valid_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and bool(_SESSION_ID_RE.match(session_id))


def _bounded_str(value: object, limit: int) -> str:
    """Metadata coercion: str passes (capped), anything else ⇒ ''."""
    return value[:limit] if isinstance(value, str) else ""


def _kg_artifacts(entry: dict) -> list[str]:
    """Bounded artifact-id list; accepts the sidecar ``kg_artifact_ids``
    spelling or the D5 ``kg_artifacts`` spelling (raw ``subj-*``/
    ``vm-learned-*`` id shapes — ids, never query text)."""
    raw = entry.get("kg_artifacts")
    if not isinstance(raw, list):
        raw = entry.get("kg_artifact_ids")
    artifacts: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item:
            artifacts.append(item[:_MAX_KG_ARTIFACT_LEN])
        if len(artifacts) >= _MAX_KG_ARTIFACTS:
            break
    return artifacts


def _query_text(entry: dict) -> str:
    """Executed-query text from either accepted spelling; non-str ⇒ ''."""
    for key in ("executed_query", "query_text"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return ""


#: Wave-5 additive-field bounds (F2). Entity mirrors the record-side caps
#: (``provenance.MAX_ENTITY_COLUMNS`` = 16 columns; column names bounded like
#: filters at 120 chars); ``statement_count`` can never exceed the per-turn
#: entry cap's order of magnitude.
_MAX_ENTITY_COLUMNS = 16
_MAX_ENTITY_COLUMN_LEN = 120
_MAX_STATEMENT_COUNT = 32
_MAX_ORIGIN_LEN = 40


def _entity_field(entry: dict) -> dict | None:
    """Bounded ``{"table", "columns"}`` from an F2 entry — ``None`` when the
    key is absent/malformed (legacy entries stay byte-identical)."""
    raw = entry.get("entity")
    if not isinstance(raw, dict):
        return None
    columns: list[str] = []
    raw_columns = raw.get("columns")
    for item in raw_columns if isinstance(raw_columns, list) else []:
        if isinstance(item, str) and item:
            columns.append(item[:_MAX_ENTITY_COLUMN_LEN])
        if len(columns) >= _MAX_ENTITY_COLUMNS:
            break
    return {"table": _bounded_str(raw.get("table"), _MAX_TOOL_NAME_LEN),
            "columns": columns}


def _query_shape_fields(entry: dict) -> dict:
    """Validated one-way shape metadata; both fields are present or absent."""
    fingerprint = entry.get("query_fingerprint")
    version = entry.get("query_shape_version")
    if (isinstance(fingerprint, str)
            and _QUERY_FINGERPRINT_RE.fullmatch(fingerprint)
            and version == QUERY_SHAPE_VERSION):
        return {
            "query_fingerprint": fingerprint,
            "query_shape_version": version,
        }
    return {}


def _additive_fields(entry: dict) -> dict:
    """Wave-5+ ADDITIVE keys — each present only when the sidecar carried a
    valid bounded value. Query-shape fields are passively validated here;
    this store never normalizes or fingerprints query text."""
    additive: dict = {}
    entity = _entity_field(entry)
    if entity is not None:
        additive["entity"] = entity
    row_count = entry.get("row_count")
    if row_count is None and "row_count" in entry:
        additive["row_count"] = None
    elif isinstance(row_count, int) and not isinstance(row_count, bool) and row_count >= 0:
        additive["row_count"] = row_count
    statement_count = entry.get("statement_count")
    if (isinstance(statement_count, int) and not isinstance(statement_count, bool)
            and statement_count >= 0):
        additive["statement_count"] = min(statement_count, _MAX_STATEMENT_COUNT)
    origin = entry.get("origin")
    if isinstance(origin, str) and origin:
        additive["origin"] = origin[:_MAX_ORIGIN_LEN]
    if entry.get("note") == API_READ_NOTE and not _query_text(entry):
        additive["note"] = API_READ_NOTE
    additive.update(_query_shape_fields(entry))
    rules, omitted = entry_row_exclusions(entry)
    if rules:
        additive["row_exclusions"] = rules
    if omitted:
        additive[ROW_EXCLUSIONS_OMITTED_KEY] = True
    return additive


def _normalize_entry(session_id: str, query_ref: object, entry: object) -> dict | None:
    """One D5-shaped entry, bounds enforced — ``None`` when the key or the
    entry is malformed (dropped at write; the route will honestly 404).

    Over-cap query text is NEVER silently truncated (D6): the entry is kept
    but stored with ``executed_query: ""`` and an explicit note.
    Wave-5 additive fields (F2) ride only when present — see
    ``_additive_fields``.
    """
    if not (isinstance(query_ref, str) and QUERY_REF_RE.match(query_ref)):
        return None
    if not isinstance(entry, dict):
        return None
    query_text = _query_text(entry)
    normalized = {
        "query_ref": query_ref,
        "session_id": session_id,
        "system_id": _bounded_str(entry.get("system_id"), _MAX_SYSTEM_ID_LEN),
        "tool_name": _bounded_str(entry.get("tool_name"), _MAX_TOOL_NAME_LEN),
        "executed_query": query_text,
        "kg_artifacts": _kg_artifacts(entry),
        "captured_at": _bounded_str(entry.get("captured_at"), _MAX_TIMESTAMP_LEN),
    }
    normalized.update(_additive_fields(entry))
    size = len(query_text.encode("utf-8"))
    if size > MAX_QUERY_TEXT_BYTES:
        normalized["executed_query"] = ""
        normalized["note"] = OVER_BOUND_NOTE
        normalized.pop("query_fingerprint", None)
        normalized.pop("query_shape_version", None)
        logger.info(
            "provenance_store: query text over bound ref=%s len_bytes=%d sha=%s",
            digest12(query_ref), size, digest12(query_text),
        )
    return normalized


def put_turn_queries(session_id: str, entries: dict,
                     write_token: "str | None" = None) -> int:
    """Persist one turn's sidecar entries. Returns the number stored.

    With a valid ``write_token`` (the per-call capability the orchestrator
    forwarded) each entry is stored under the opaque capability suffix
    ``cap:<sha256(token|sid|ref)>`` so a caller without the token cannot
    overwrite it; without a token the legacy ``<sid>:<ref>`` key is used
    (local mode, and callers predating the capability). Bounds enforced at
    write (≤``MAX_ENTRIES_PER_TURN`` stored; malformed keys/entries dropped).
    Fail-soft end to end: ``JobStore`` degrades to in-memory on any backend
    error, and this function never raises on malformed input: a bad sidecar
    frame must never break the answer stream (Task 3.3 swallow-guards the
    call too).
    """
    if not _valid_session_id(session_id) or not isinstance(entries, dict):
        logger.debug("provenance_store: put rejected sid_sha=%s",
                     digest12(str(session_id)))
        return 0
    token = write_token if valid_write_token(write_token) else None
    store = _jobs()
    stored = 0
    dropped = 0
    for query_ref, entry in entries.items():
        if stored >= MAX_ENTRIES_PER_TURN:
            dropped += 1
            continue
        normalized = _normalize_entry(session_id, query_ref, entry)
        if normalized is None:
            dropped += 1
            continue
        suffix = (capability_suffix(token, session_id, query_ref)
                  if token else None)
        job_id = f"cap:{suffix}" if suffix else f"{session_id}:{query_ref}"
        store[job_id] = normalized
        stored += 1
    logger.info("provenance_store: put sid_sha=%s stored=%d dropped=%d capability=%s",
                digest12(session_id), stored, dropped, bool(token))
    return stored


def get_query(session_id: str, query_ref: str) -> dict | None:
    """The stored D5-shaped entry, or ``None`` when unknown/expired/invalid.

    Resolves the entry through the per-call capability record when one exists,
    so a forged write under a replayed ``(session_id, query_ref)`` is never
    returned; falls back to the legacy key for pre-capability/local entries.
    ``None`` is the ONLY miss signal — the redemption route maps it to the
    structured 404 ("Unknown or expired query_ref"); this function never
    fabricates an entry and never raises on malformed ids.
    """
    if not _valid_session_id(session_id):
        return None
    if not (isinstance(query_ref, str) and QUERY_REF_RE.match(query_ref)):
        return None
    return read_entry(_jobs(), session_id, query_ref)
