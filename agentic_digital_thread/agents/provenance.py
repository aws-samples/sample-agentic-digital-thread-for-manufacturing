# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-claim provenance records for the additive ``metadata.provenance`` key.

Pure, stdlib-only (import purity like ``query_intelligence.py`` — no boto3,
no I/O, and NEVER an import from ``runtime_app``/``chat_client``: both
emitters import THIS module, keeping the unit lane offline-pure).

Single source of truth for (provenance plan, frozen contracts D1–D4/D6):

- the CLIENT citation predicate (``is_client_cited`` — the EXACT
  ``thread-store.ts`` test ``data.tool !== 'explore_graph'``; deliberately
  NOT ``query_intelligence.is_graph_query``, which also matches the
  gateway-prefixed spelling — a prefixed ``x___explore_graph`` IS numbered
  by the client today and must stay numbered here, D2);
- citation ordinals (``citation_ordinals`` — toolUseId → citation_num,
  reproducing the client counter exactly);
- the deterministic Tier-3 handle (``mint_query_ref`` —
  ``"q-" + sha256(session_id + "|" + tool_id)[:12]``, D3; Wave 3's sidecar
  keys by the same derivation, so no new field crosses the gateway);
- the D1 payload shape (``build_provenance_records`` — the ONE builder both
  emitters use; ≤``MAX_PROVENANCE_RECORDS`` records, labels ≤
  ``MAX_LABEL_LEN``, ≤``MAX_SCOPE_FILTERS`` filters ×
  ``MAX_FILTER_LEN`` chars, ≤``MAX_RULES_APPLIED`` rules, serialized
  payload ≤``MAX_PAYLOAD_BYTES`` with an explicit ``truncated`` flag).

Records carry ONLY already-bounded metadata surfaces
(``safe_tool_result_metadata`` shapes + bounded evidence dicts) — raw
payload content and executed-query text are structurally absent
(``tool_inputs`` is accepted for signature parity with the Wave-3 sidecar
and never read into a record, D6). Sparse records are the NORM in the
default report-only deployment: every field except ``citation_num``,
``tool_id``, ``source_system``, ``captured_at``, ``verification`` may be
empty — absence is rendered honestly, never fabricated (D4).
"""
from __future__ import annotations

import ast
import hashlib
import json
from typing import Any, Callable, Iterable

from agents.query_intelligence import MAX_LABEL_LEN, MAX_RULES_APPLIED

#: Hard cap on provenance records per turn (D1).
MAX_PROVENANCE_RECORDS = 32

#: Hard caps on plain-language scope filters (D1).
MAX_SCOPE_FILTERS = 8
MAX_FILTER_LEN = 120

#: Hard cap on entity column names (bounded like filters).
MAX_ENTITY_COLUMNS = 16

#: Hard cap on the serialized ``provenance`` payload (D1 — mirrors the
#: ≈4 KB ``intelligence_summary`` precedent).
MAX_PAYLOAD_BYTES = 4096

#: ``system_id`` / tool-name bounds — named HERE once (the pure D1 home);
#: ``provenance_store`` imports both so record and store bounds can never
#: drift. ``MAX_SYSTEM_ID_LEN`` mirrors ``SourceMetadata``'s ≤100.
MAX_SYSTEM_ID_LEN = 100
MAX_TOOL_NAME_LEN = 160

#: The ONE ``captured_at`` wire spelling (D1 example precision — whole
#: seconds, ``Z`` suffix). Every emitter formats through
#: ``format_captured_at`` so the same D1 field can never render
#: differently by path.
CAPTURED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The ONE bare graph-tool spelling the client does NOT number.
_GRAPH_TOOL_BARE = "explore_graph"


def _text(value: Any, limit: int) -> str:
    """Defensive string coercion: str passes, datetimes isoformat, None ⇒ '',
    and any other type is ``str()``-coerced (never dropped)."""
    if isinstance(value, str):
        return value[:limit]
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())[:limit]
    return str(value)[:limit]


def format_captured_at(now: Any) -> str:
    """The D1 ``captured_at`` wire spelling for an aware UTC datetime — the
    ONE format home for every emitter call site (runtime ×4, local ×2), so
    the field renders identically on all paths. The CALLER supplies the
    clock value: this module stays import-pure (no datetime import — pinned
    by the import test)."""
    return now.strftime(CAPTURED_AT_FORMAT)


def is_client_cited(name: str) -> bool:
    """The EXACT client citation predicate (D2): ``thread-store.ts`` numbers
    one citation per ``tool_start`` where ``data.tool !== 'explore_graph'``
    — an exact-string test, so a gateway-prefixed graph name
    (``x___explore_graph``) IS cited. Deliberately NOT
    ``query_intelligence.is_graph_query`` (which matches the prefix too)."""
    return name != _GRAPH_TOOL_BARE


def citation_ordinals(tool_calls: list[dict]) -> dict[str, int]:
    """toolUseId → citation_num, reproducing the client counter exactly.

    The counter increments for EVERY client-cited call (parity with the
    client, which numbers calls whether or not they carry a tool id); only
    entries with a non-empty string ``toolUseId`` land in the map (nothing
    to bind otherwise — ``citation_num`` fallback still aligns, D2).
    """
    ordinals: dict[str, int] = {}
    counter = 0
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        if not is_client_cited(_text(call.get("name"), MAX_TOOL_NAME_LEN)):
            continue
        counter += 1
        tool_id = call.get("toolUseId")
        if isinstance(tool_id, str) and tool_id:
            ordinals[tool_id] = counter
    return ordinals


def mint_query_ref(session_id: str, tool_id: str) -> str:
    """Deterministic Tier-3 handle (D3): ``q-`` + first 12 hex chars of
    ``sha256(session_id + "|" + tool_id)``. Stable for the same pair, so the
    Wave-3 sidecar needs no coordination with the record."""
    digest = hashlib.sha256(f"{session_id}|{tool_id}".encode("utf-8")).hexdigest()
    return f"q-{digest[:12]}"


#: Reserved tool-call argument carrying the forward-travelling provenance
#: context (Wave 5, frozen contract F1). The orchestrator merges it into
#: INTERACTIVE gateway tool calls; the system-agent runtime pops it
#: UNCONDITIONALLY at the ASGI seam (the exact ``_adt_identity``/ADR-0049
#: posture — it never reaches tool signatures or any LLM token path). ONE
#: definition home; every consumer imports it (pinned by the join-contract
#: test).
RESERVED_PROVENANCE_ARG = "_adt_provenance"

#: Structural mirrors of ``provenance_store``'s validation regexes — spelled
#: without ``re`` so this module stays import-pure (the import pin allows
#: stdlib ``ast``/``hashlib``/``json``/``typing`` only).
_SESSION_ID_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789._:@-")
MAX_SESSION_ID_LEN = 256


def _valid_session_id(value: Any) -> bool:
    """Structural mirror of ``_SESSION_ID_RE`` (``^[A-Za-z0-9._:@-]{1,256}$``)."""
    return (isinstance(value, str)
            and 1 <= len(value) <= MAX_SESSION_ID_LEN
            and all(c in _SESSION_ID_CHARS for c in value))


def _valid_query_ref(value: Any) -> bool:
    """Structural mirror of ``QUERY_REF_RE`` (``^q-[0-9a-f]{12}$``, D3)."""
    return (isinstance(value, str) and len(value) == 14
            and value.startswith("q-")
            and all(c in "0123456789abcdef" for c in value[2:]))


def provenance_context(session_id: str, query_ref: str,
                       write_token: "str | None" = None) -> dict:
    """The reserved-arg value: ``{"session_id", "query_ref"}`` plus an
    OPTIONAL per-call ``write_token`` (the Wave-7 write capability).

    Pure builder — the ONE wire spelling for ``RESERVED_PROVENANCE_ARG``'s
    value, so orchestrator (writer) and system-agent runtime (reader) can
    never drift. The ``query_ref`` is the existing D3 handle
    (``mint_query_ref(session_id, tool_use_id)``) minted at the
    gateway-client seam. ``write_token`` is a fresh high-entropy nonce the
    app mints per call; it names the opaque physical key the runtime writes
    under (``provenance_store.capability_suffix``) and is server-only, never
    emitted in SSE, provenance frames, logs, URLs, or the Quick bridge.
    Omitted (legacy/local) ⇒ today's value.
    """
    context = {"session_id": session_id, "query_ref": query_ref}
    if write_token:
        context["write_token"] = write_token
    return context


#: A reserved-arg write token: 24-64 lowercase hex chars (mirrors
#: ``provenance_store._WRITE_TOKEN_RE``; spelled without ``re`` to keep this
#: module import-pure).
_WRITE_TOKEN_CHARS = frozenset("0123456789abcdef")


def parse_write_token(value: Any) -> "str | None":
    """The validated per-call write token from a reserved-arg value, or
    ``None``. Accepts the reserved-arg dict (reads its ``write_token``) or a
    bare string; a malformed/absent token ⇒ ``None`` (legacy key, honest
    absence). Never raises, never logs the value."""
    token = value.get("write_token") if isinstance(value, dict) else value
    if (isinstance(token, str) and 24 <= len(token) <= 64
            and all(c in _WRITE_TOKEN_CHARS for c in token)):
        return token
    return None


def parse_provenance_context(value: Any) -> "tuple[str, str] | None":
    """Validated ``(session_id, query_ref)`` from a reserved-arg value.

    Malformed anything (non-dict, wrong ref shape, bad/oversized session id,
    missing keys) ⇒ ``None`` — never raises, never logs the value. The
    system-agent runtime treats ``None`` as a legacy caller: honest absence,
    no sidecar write, never a deny.
    """
    if not isinstance(value, dict):
        return None
    session_id = value.get("session_id")
    query_ref = value.get("query_ref")
    if _valid_session_id(session_id) and _valid_query_ref(query_ref):
        return (session_id, query_ref)
    return None


def _source_system(tool_name: str, evidence: dict) -> str:
    """Evidence ``system_id`` first (trusted capture); else the gateway
    prefix (``sys___tool`` → ``sys``); else honestly empty."""
    system_id = evidence.get("system_id")
    if isinstance(system_id, str) and system_id:
        return system_id[:MAX_SYSTEM_ID_LEN]
    if "___" in tool_name:
        return tool_name.split("___", 1)[0][:MAX_SYSTEM_ID_LEN]
    return ""


def _entity_from_evidence(evidence: dict) -> dict:
    """``{table, columns}`` from bounded evidence — empty when unbound (D4:
    never fabricated)."""
    columns: list[str] = []
    for item in evidence.get("columns") or []:
        if isinstance(item, str) and item:
            columns.append(item[:MAX_FILTER_LEN])
        if len(columns) >= MAX_ENTITY_COLUMNS:
            break
    return {"table": _text(evidence.get("table"), MAX_LABEL_LEN), "columns": columns}


def _scope_from_evidence(evidence: dict) -> dict:
    """``{filters, window}`` — plain-language strings only, capped per D1.

    ``window`` accepts an explicit ``{"start", "end"}`` dict or the
    ``source_observed_start``/``source_observed_end`` evidence keys;
    both-empty ⇒ ``None`` (honest absence, matching the D1 example).
    """
    filters: list[str] = []
    for item in evidence.get("filters") or []:
        if isinstance(item, str) and item:
            filters.append(item[:MAX_FILTER_LEN])
        if len(filters) >= MAX_SCOPE_FILTERS:
            break
    win = evidence.get("window")
    if isinstance(win, dict):
        start = _text(win.get("start"), MAX_FILTER_LEN)
        end = _text(win.get("end"), MAX_FILTER_LEN)
    else:
        start = _text(evidence.get("source_observed_start"), MAX_FILTER_LEN)
        end = _text(evidence.get("source_observed_end"), MAX_FILTER_LEN)
    window = {"start": start, "end": end} if (start or end) else None
    return {"filters": filters, "window": window}


def _result_shape(meta: Any) -> dict:
    """``{rows, truncated, partial_reason}`` from a
    ``safe_tool_result_metadata``-shaped dict; no capture ⇒ honest nulls."""
    if not isinstance(meta, dict):
        return {"rows": None, "truncated": False, "partial_reason": ""}
    rows = None
    for key in ("row_count", "item_count"):
        value = meta.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            rows = value
            break
    return {
        "rows": rows,
        "truncated": meta.get("truncated") is True,
        "partial_reason": _text(meta.get("partial_reason"), 40),
    }


def _is_rejected(verdict: str | None) -> bool:
    """Whether the integrity verdict rejected the turn (D4)."""
    return isinstance(verdict, str) and verdict.strip().lower() == "rejected"


def _verification(meta: Any, evidence: dict, verdict: str | None) -> str:
    """D4 mapping — honest absence, never blocks the answer.

    - ``verified``: enforce path only — verdict ``accepted`` AND bound typed
      evidence for this call.
    - ``partial``: bounded capture shows a successful/partial result shape
      but no typed-evidence binding (the report-only best case).
    - ``unverified``: verdict ``rejected``, or no captured result metadata,
      or an error/unknown capture. Always rendered, never hidden.
    """
    verdict_norm = verdict.strip().lower() if isinstance(verdict, str) else ""
    if _is_rejected(verdict):
        return "unverified"
    if verdict_norm == "accepted" and evidence:
        return "verified"
    if isinstance(meta, dict) and meta.get("app_status") in ("success", "partial"):
        return "partial"
    return "unverified"


def _kg_route(rules_applied: list[dict]) -> dict:
    """Turn-level KG route: ≤``MAX_RULES_APPLIED`` defensive ``{id, label}``
    rows (labels ≤``MAX_LABEL_LEN``, missing label falls back to the id);
    ``value_mappings`` carries the ``vm-``-prefixed ids (the pinned
    ``vm-learned-*`` artifact shape, ``usage_tracking.py``)."""
    rows: list[dict] = []
    value_mappings: list[str] = []
    for entry in (rules_applied or [])[:MAX_RULES_APPLIED]:
        if not isinstance(entry, dict):
            continue
        rule_id = _text(entry.get("id"), MAX_LABEL_LEN)
        label = _text(entry.get("label"), MAX_LABEL_LEN) or rule_id
        rows.append({"id": rule_id, "label": label})
        if rule_id.startswith("vm-"):
            value_mappings.append(rule_id)
    return {"rules_applied": rows, "value_mappings": value_mappings}


#: Hard cap on claim-check handles carried per record (Wave 4 — mirrors the
#: capture bound in ``tool_result_capture._MAX_DATASET_REFS``).
MAX_DATASET_REFS = 4


def _dataset_refs(meta: Any) -> list[str]:
    """Fullmatch-validated claim-check handles the bounded capture retained
    (``tool_result_capture.claim_check_dataset_refs`` is the ONE validating
    writer). Re-checked structurally here (no ``re`` — the import pin keeps
    this module minimal): ``tel-`` + 12 lowercase hex chars, ≤4 handles.
    Handles are opaque server-minted references — never payload content."""
    if not isinstance(meta, dict):
        return []
    raw = meta.get("dataset_refs")
    refs: list[str] = []
    for ref in raw if isinstance(raw, list) else []:
        if (isinstance(ref, str) and len(ref) == 16 and ref.startswith("tel-")
                and all(c in "0123456789abcdef" for c in ref[4:])):
            refs.append(ref)
        if len(refs) >= MAX_DATASET_REFS:
            break
    return refs


def _build_record(*, citation_num: int, tool_id: str, tool_name: str,
                  evidence: Any, meta: Any, kg_route: dict, session_id: str,
                  verdict: str | None, captured_at: Any) -> dict:
    """One D1 record — required keys always present; enrichment honest-empty.

    ``dataset_refs`` is ADDITIVE (Wave 4): present ONLY when the bounded
    capture retained validated claim-check handles for this call — absent
    key ⇒ exactly the pre-Wave-4 record (legacy clients and the D1 shape
    pin unaffected). Handles only; the withheld series itself is served
    exclusively by the admin-gated redemption route, never this payload.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    record = {
        "citation_num": citation_num,
        "tool_id": tool_id,
        "source_system": _source_system(tool_name, evidence),
        "tool_name": tool_name,
        "entity": _entity_from_evidence(evidence),
        "scope": _scope_from_evidence(evidence),
        "query_ref": mint_query_ref(session_id, tool_id),
        "result_shape": _result_shape(meta),
        "kg_route": dict(kg_route),
        "captured_at": _text(evidence.get("queried_at"), MAX_FILTER_LEN)
                       or _text(captured_at, MAX_FILTER_LEN),
        "verification": _verification(meta, evidence, verdict),
    }
    dataset_refs = _dataset_refs(meta)
    if dataset_refs:
        record["dataset_refs"] = dataset_refs
    return record


def _cap_payload_bytes(payload: dict) -> dict:
    """Enforce the D1 total-size cap: drop trailing records until the
    serialized payload fits ``MAX_PAYLOAD_BYTES``, flagging ``truncated``."""
    while payload["records"]:
        size = len(json.dumps(payload, separators=(",", ":"), default=str)
                   .encode("utf-8"))
        if size <= MAX_PAYLOAD_BYTES:
            break
        payload["records"].pop()
        payload["truncated"] = True
    return payload


def build_provenance_records(*, session_id: str, tool_calls: list[dict],
                             tool_inputs: dict, tool_results_metadata: dict,
                             evidence_by_tool: dict, rules_applied: list[dict],
                             verdict: str | None, captured_at: Any,
                             graph_query_refs: list[dict] | None = None) -> dict:
    """Build the D1 ``{"records": [...], "truncated": bool}`` payload — the
    ONE builder both emitters (runtime + local) call.

    One record per client-cited tool call that carries a toolUseId (the
    bare ``explore_graph`` is never cited, D2). ``tool_inputs`` is accepted
    for emitter signature parity and NEVER read into a record — query text
    is Wave-3 sidecar-only (D6). ``tool_results_metadata`` /
    ``evidence_by_tool`` map toolUseId → ``safe_tool_result_metadata``-shaped
    dict / bounded evidence dict. All caps per D1; verification per D4.
    ``graph_query_refs`` (ADDITIVE, live cloud-path fix 2026-08-04): rows
    from ``uncited_query_refs`` — when non-empty the payload carries a
    ``graph_queries`` key (refs + tool names ONLY, never query text) so the
    turn's uncited-but-redeemable Tier-3 handles are surfaced; absent
    otherwise (legacy payload byte-identical).
    """
    del tool_inputs  # D6: query/input text structurally never enters records.
    metadata_map = tool_results_metadata if isinstance(tool_results_metadata, dict) else {}
    evidence_map = evidence_by_tool if isinstance(evidence_by_tool, dict) else {}
    ordinals = citation_ordinals(tool_calls)
    kg_route = _kg_route(rules_applied)
    records: list[dict] = []
    seen: set[str] = set()
    truncated = False
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        tool_id = call.get("toolUseId")
        if not isinstance(tool_id, str) or tool_id not in ordinals or tool_id in seen:
            continue
        if len(records) >= MAX_PROVENANCE_RECORDS:
            truncated = True
            break
        seen.add(tool_id)
        records.append(_build_record(
            citation_num=ordinals[tool_id], tool_id=tool_id,
            tool_name=_text(call.get("name"), MAX_TOOL_NAME_LEN),
            evidence=evidence_map.get(tool_id), meta=metadata_map.get(tool_id),
            kg_route=kg_route, session_id=session_id, verdict=verdict,
            captured_at=captured_at))
    payload = {"records": records, "truncated": truncated}
    if graph_query_refs:
        payload["graph_queries"] = list(graph_query_refs[:MAX_GRAPH_QUERY_REFS])
    return _cap_payload_bytes(payload)


#: Tool-input fields that ARE executed-query text (D6, Wave-3 sidecar):
#: Mode-B SQL rides ``sql``/``query`` and the graph tool's openCypher rides
#: ``cypher``. Mode-A NL sub-questions ride ``question`` — NOT query text,
#: never a sidecar entry. ONE home for both emitters (runtime + local).
QUERY_INPUT_FIELDS = ("sql", "query", "cypher")


def executed_query_from_input(raw: Any, *, literal_eval_fallback: bool = False) -> str:
    """Executed-query text from a captured tool input, or ``""`` (D6).

    Only inputs that parse AND carry a query-bearing field qualify — a
    2 KB-truncated capture that no longer parses is honestly OMITTED
    (``""`` ⇒ no sidecar entry; Tier 3 then renders "executed query not
    captured for this call"), never guessed at or reconstructed.

    ``literal_eval_fallback`` is the tolerance the local emitter needs:
    ``chat_client`` retains inputs as ``str()`` of the input dict (python
    literal, sometimes JSON), so it also tries ``ast.literal_eval``; the
    runtime captures JSON and keeps the strict parse.
    """
    data = raw
    if isinstance(raw, str):
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if data is None and literal_eval_fallback:
            try:
                data = ast.literal_eval(raw)
            except Exception:  # nosec B110 - best-effort parse; '' ⇒ honest omission
                data = None
    if not isinstance(data, dict):
        return ""
    for field in QUERY_INPUT_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


#: Hard cap on additive ``graph_queries`` rows per payload (refs only).
MAX_GRAPH_QUERY_REFS = 8


def uncited_query_refs(*, session_id: str, tool_calls: list[dict],
                       tool_inputs: dict,
                       literal_eval_fallback: bool = False) -> list[dict]:
    """``{"query_ref", "tool_name"}`` rows for UNCITED query-bearing calls.

    Live cloud-path gap (2026-08-04): in the default Mode A the ONLY call
    whose executed query the orchestrator actually sees is the bare
    ``explore_graph`` (openCypher input) — which D2 deliberately never
    cites, so its stored sidecar entry had NO client-visible handle while
    every cited record's ref was honestly absent (D6). These rows ride the
    ADDITIVE ``graph_queries`` payload key: opaque D3 refs + tool names
    ONLY — query text stays sidecar-only (D6 broadcast safety unchanged).
    Bounded to ``MAX_GRAPH_QUERY_REFS``; [] when nothing qualifies (key
    absent — legacy payload byte-identical).
    """
    refs: list[dict] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        tool_id = call.get("toolUseId")
        if not isinstance(tool_id, str) or not tool_id:
            continue
        name = _text(call.get("name"), MAX_TOOL_NAME_LEN)
        if is_client_cited(name):
            continue
        if not executed_query_from_input(
                (tool_inputs or {}).get(tool_id),
                literal_eval_fallback=literal_eval_fallback):
            continue
        refs.append({"query_ref": mint_query_ref(session_id, tool_id),
                     "tool_name": name})
        if len(refs) >= MAX_GRAPH_QUERY_REFS:
            break
    return refs


def build_sidecar_entries(*, session_id: str, tool_calls: list[dict],
                          tool_inputs: dict, rule_ids: Iterable[str] | None,
                          captured_at: str,
                          literal_eval_fallback: bool = False) -> dict:
    """``{query_ref: entry}`` for the query-bearing calls of one turn — the
    ONE sidecar entry builder both emitters (runtime + local) call (Wave 3).

    Keys are the SAME D3 derivation the provenance record carries
    (``mint_query_ref`` — no coordination needed); ``system_id`` reuses the
    ``_source_system`` gateway-prefix derivation (``sys___tool`` → ``sys``,
    else honestly empty); ``kg_artifact_ids`` is the turn's raw rule-id set
    (``subj-*``/``vm-learned-*`` shapes — ids only, never query text).
    Query text is stored VERBATIM: the frame is server-only (chat_client
    strips it before broadcast) and the store bounds it at write.
    """
    kg_artifact_ids = sorted(rule_ids or [])
    entries: dict[str, dict] = {}
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        tool_id = call.get("toolUseId")
        if not isinstance(tool_id, str) or not tool_id:
            continue
        query_text = executed_query_from_input(
            (tool_inputs or {}).get(tool_id),
            literal_eval_fallback=literal_eval_fallback)
        if not query_text:
            continue
        name = _text(call.get("name"), MAX_TOOL_NAME_LEN)
        entries[mint_query_ref(session_id, tool_id)] = {
            "system_id": _source_system(name, {}),
            "tool_name": name,
            "query_text": query_text,
            "kg_artifact_ids": kg_artifact_ids,
            "captured_at": captured_at,
        }
    return entries


#: Row exclusions shown on one record: the source owner's rules a cited
#: call's queries ran with (``row_exclusion.card_record``), one per table.
MAX_ROW_EXCLUSIONS = 4
#: Compact JSON bytes of ONE shown rule; a larger rule is not shown at all.
MAX_ROW_EXCLUSION_BYTES = 1024
#: A shown rule's own bounds, the ones ``row_exclusion`` validates at write.
MAX_ROW_EXCLUSION_CONDITIONS = 8
MAX_ROW_EXCLUSION_VALUES = 32
MAX_ROW_EXCLUSION_VALUE_LEN = 128
_ROW_EXCLUSION_OPS = ("empty", "not_empty", "in", "not_in")
#: Candidate rules read from one entry (its statements' rules, before dedupe).
_MAX_ROW_EXCLUSION_CANDIDATES = 32


#: A rule the card cannot show whole within :data:`MAX_ROW_EXCLUSION_BYTES`
#: (well formed, just too large): left out, and its record marked.
_TOO_LARGE = "too_large"
#: The record key that says a rule the call ran with was left off its card
#: for size (the rule, or the frame, had no room for its details).
ROW_EXCLUSIONS_OMITTED_KEY = "row_exclusions_omitted"


def _row_exclusion_condition(raw: Any) -> "dict | None":
    """One shown condition, ``{column, op[, values]}``, or ``None`` when it
    is not exactly one of the shapes ``row_exclusion`` stores: a column, one
    of its ops, and values only (and always) for ``in`` / ``not_in``, none
    holding a double quote (the store refuses quotes; the card quotes each
    value)."""
    if not isinstance(raw, dict) or set(raw) - {"column", "op", "values"}:
        return None
    column, op, values = raw.get("column"), raw.get("op"), raw.get("values", [])
    if not (isinstance(column, str) and 0 < len(column) <= MAX_FILTER_LEN
            and op in _ROW_EXCLUSION_OPS and isinstance(values, list)):
        return None
    if (op in ("in", "not_in")) != bool(values) or len(values) > MAX_ROW_EXCLUSION_VALUES:
        return None
    if not all(isinstance(v, str) and 0 < len(v) <= MAX_ROW_EXCLUSION_VALUE_LEN
               and '"' not in v for v in values):
        return None
    return {"column": column, "op": op, **({"values": list(values)} if values else {})}


def _rule_core(raw: Any) -> "dict | None":
    """``{table, label, conditions, at}`` of one rule, never shortened, or
    ``None`` when any part is out of shape (a condition, a label over
    ``MAX_LABEL_LEN``). Who set the rule is never carried."""
    if not isinstance(raw, dict):
        return None
    label, conditions = raw.get("label"), raw.get("conditions")
    table, at = raw.get("table", ""), raw.get("at", "")
    if not (isinstance(label, str) and label.strip() and len(label) <= MAX_LABEL_LEN
            and isinstance(conditions, list) and isinstance(table, str)
            and 0 < len(conditions) <= MAX_ROW_EXCLUSION_CONDITIONS):
        return None
    shown = [_row_exclusion_condition(c) for c in conditions]
    if any(c is None for c in shown):
        return None
    return {"table": table, "label": label, "conditions": shown,
            "at": _text(at if isinstance(at, str) else "", MAX_LABEL_LEN)}


def _with_source(core: dict, source: str, shortened: bool) -> "dict | str":
    """``core`` with its ``source``: whole when the rule fits
    :data:`MAX_ROW_EXCLUSION_BYTES` with it, else the longest prefix that
    fits with ``source_shortened: true`` (the card then says so). ``shortened``
    keeps the flag of a source shortened earlier. :data:`_TOO_LARGE` when
    the rule does not fit even with no source."""
    whole = {**core, "source": source, **({"source_shortened": True} if shortened else {})}
    if _frame_bytes(whole) <= MAX_ROW_EXCLUSION_BYTES:
        return whole
    lo, hi = 0, len(source)          # the longest prefix that fits, by bisection
    while lo < hi:
        mid = (lo + hi + 1) // 2
        cut = {**core, "source": source[:mid], "source_shortened": True}
        lo, hi = (mid, hi) if _frame_bytes(cut) <= MAX_ROW_EXCLUSION_BYTES else (lo, mid - 1)
    rule = {**core, "source": source[:lo], "source_shortened": True}
    return rule if _frame_bytes(rule) <= MAX_ROW_EXCLUSION_BYTES else _TOO_LARGE


def _row_exclusion(raw: Any) -> "dict | str | None":
    """One shown rule, ``{table, label, conditions, at, source[,
    source_shortened]}``: its label and conditions WHOLE (part of a rule would
    misstate which rows were left out) and its source shortened only to fit
    :data:`MAX_ROW_EXCLUSION_BYTES` (:func:`_with_source`). ``None`` for a
    rule out of shape, :data:`_TOO_LARGE` for one that cannot fit."""
    core = _rule_core(raw)
    if core is None:
        return None
    source = raw.get("source")
    return _with_source(core, source if isinstance(source, str) else "",
                        raw.get("source_shortened") is True)


def normalize_row_exclusions(raw: Any) -> "tuple[list[dict], bool]":
    """``(rules, omitted)`` for one entry: each valid rule
    (:func:`_row_exclusion`) once, in order, at most
    :data:`MAX_ROW_EXCLUSIONS`, and whether a well-formed rule was left out
    for size or count (``omitted``; a malformed one is not counted). The ONE
    validator of the store's write and the frame's merge."""
    rules: list[dict] = []
    omitted = False
    for item in (raw if isinstance(raw, list) else [])[:_MAX_ROW_EXCLUSION_CANDIDATES]:
        rule = _row_exclusion(item)
        if rule == _TOO_LARGE:
            omitted = True
        elif rule is not None and rule not in rules:
            if len(rules) >= MAX_ROW_EXCLUSIONS:
                omitted = True
            else:
                rules.append(rule)
    return rules, omitted


def _frame_bytes(value: Any) -> int:
    """Compact JSON bytes of ``value``, as the frame cap measures them."""
    return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))


def entry_row_exclusions(entry: dict) -> "tuple[list[dict], bool]":
    """``(rules, omitted)`` of one sidecar entry: its shown rules
    (:func:`normalize_row_exclusions`) and whether one was left out for size
    there or already at the store's write (:data:`ROW_EXCLUSIONS_OMITTED_KEY`)."""
    rules, omitted = normalize_row_exclusions(entry.get("row_exclusions"))
    return rules, omitted or entry.get(ROW_EXCLUSIONS_OMITTED_KEY) is True


def _fits(payload: dict, record: dict, key: str, value: Any) -> bool:
    """Put ``record[key] = value`` and keep it only while the whole frame
    stays within :data:`MAX_PAYLOAD_BYTES`; whether it was kept."""
    record[key] = value
    if _frame_bytes(payload) <= MAX_PAYLOAD_BYTES:
        return True
    del record[key]
    return False


def _add_row_exclusions(payload: dict, pending: list) -> dict:
    """``payload`` with each record's shown rules on it (``row_exclusions``),
    in record order, only while the whole frame stays within
    :data:`MAX_PAYLOAD_BYTES`: a rule never pushes a citation record off the
    frame. A record whose rules do not fit shows none of them, and gets the
    bounded omission marker (:data:`ROW_EXCLUSIONS_OMITTED_KEY`) instead when
    that fits, as does a record whose entry had a rule left out for size;
    otherwise it says nothing. A record the cap dropped gets neither."""
    records = payload.get("records") or []
    for record, rules, omitted in pending:
        if not any(r is record for r in records):
            continue
        shown = bool(rules) and _fits(payload, record, "row_exclusions", rules)
        if omitted or (rules and not shown):
            _fits(payload, record, ROW_EXCLUSIONS_OMITTED_KEY, True)
    return payload


#: Hard cap on sidecar reads per frame enrichment (assembly-time merge —
#: the designed Wave-5 follow-up). Records are already bounded at
#: ``MAX_PROVENANCE_RECORDS``; this bounds the store round-trips
#: independently, so a hostile payload can never widen the read fan-out.
MAX_SIDECAR_READS = 32


def _merge_sidecar_entry(record: dict, entry: dict) -> None:
    """Merge ONE sidecar entry into ONE record in place — SQL-free by
    construction (D6): ONLY ``entity.table``/``entity.columns`` and
    ``result_shape.rows`` cross over, plus the D4 ``unverified`` →
    ``partial`` upgrade. ``executed_query`` (and every other entry key) is
    structurally never copied. Empty record fields fill from the entry;
    non-empty fields (typed evidence) always win — and ``verified`` stays
    reserved for enforce-mode typed evidence (never minted here, never
    downgraded). A record of a turn the integrity gate rejected never comes
    here (:func:`enrich_records_from_sidecar`)."""
    entity = entry.get("entity")
    rec_entity = record.get("entity")
    if isinstance(entity, dict) and isinstance(rec_entity, dict):
        if not rec_entity.get("table"):
            table = _text(entity.get("table"), MAX_LABEL_LEN)
            if table:
                rec_entity["table"] = table
        if not rec_entity.get("columns"):
            columns = [c[:MAX_FILTER_LEN]
                       for c in (entity.get("columns") or [])
                       if isinstance(c, str) and c][:MAX_ENTITY_COLUMNS]
            if columns:
                rec_entity["columns"] = columns
    shape = record.get("result_shape")
    row_count = entry.get("row_count")
    if (isinstance(shape, dict) and shape.get("rows") is None
            and isinstance(row_count, int) and not isinstance(row_count, bool)
            and row_count >= 0):
        shape["rows"] = row_count
    if record.get("verification") == "unverified":
        record["verification"] = "partial"


def enrich_records_from_sidecar(payload: dict, *, session_id: str,
                                get_entry: "Callable[[str, str], Any]",
                                verdict: "str | None" = None) -> dict:
    """Assembly-time sidecar merge — the ONE merge home for ALL THREE
    emitters (runtime legacy, runtime enforce, local), the designed Wave-5
    follow-up to redemption-time resolution (F3).

    For each record with a valid ``query_ref``, reads the sidecar entry via
    the injected ``get_entry(session_id, query_ref)`` reader (injection
    keeps this module import-pure — emitters pass
    ``provenance_store.get_query``). FAIL-SOFT per record: a store miss,
    a non-dict entry, or a reader error leaves the record EXACTLY as built —
    refs without entries render exactly as today. Total reads are bounded
    at ``MAX_SIDECAR_READS``. A record corroborated by a successful sidecar
    capture moves ``unverified`` → ``partial`` (the FROZEN D4 mapping:
    bounded capture success WITHOUT typed binding — ``verified`` stays
    reserved for enforce-mode typed evidence). ``verdict`` is the turn's
    integrity verdict, which the emitter that built the frame passes in: D4
    makes a rejected verdict unverified whatever was captured, so each entry
    is still read (capability misses are still counted) but none is merged,
    and the frame stays exactly as built. The rejection rides nowhere in the
    frame, so it costs no frame capacity. ``executed_query``/SQL never
    enters the frame (D6 — the no-SQL-on-broadcast pin extends through this
    path). Re-enforces the D1 payload cap after the merge (merged columns
    may grow records; trailing records drop with an honest ``truncated``
    flag). Then each record gets the row exclusions its entry kept, only
    where they fit the cap (:func:`_add_row_exclusions`)."""
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not records or not session_id:
        return payload
    reads = 0
    merged = False
    pending: list = []
    for record in records:
        if reads >= MAX_SIDECAR_READS:
            break
        if not isinstance(record, dict):
            continue
        if not _valid_query_ref(record.get("query_ref")):
            continue
        reads += 1
        try:
            entry = get_entry(session_id, record["query_ref"])
        except Exception:  # nosec B110 - fail-soft: record renders as built
            continue
        if isinstance(entry, dict) and not _is_rejected(verdict):
            _merge_sidecar_entry(record, entry)
            merged = True
            pending.append((record, *entry_row_exclusions(entry)))
    return _add_row_exclusions(_cap_payload_bytes(payload) if merged else payload, pending)


# --------------------------------------------------------------------------- #
# Wave-5 writer-seam entity extraction (sidecar statement parse)
# --------------------------------------------------------------------------- #

#: Fail-soft input bound for the tokenizer below — over-cap SQL text returns
#: today's honest empty rather than scanning attacker-sized input.
MAX_ENTITY_SQL_CHARS = 20_000

#: Catalog schemas whose statements are discovery probes, never the
#: data-bearing statement — the Wave-5 writer skips them when choosing the
#: entity-bearing statement. Spelled without ``re`` (this module's import
#: pin is an exact stdlib set that excludes it).
_DISCOVERY_SCHEMAS = frozenset({"information_schema", "pg_catalog"})


def _mask_sql_top_level(sql: str) -> str:
    """Space-mask comments, string literals, and everything inside
    parentheses so only depth-0 tokens survive (positions preserved; the
    outermost paren pair itself survives as ``(``/``)`` tokens). Depth-0
    double-quoted identifiers pass through verbatim. Single bounded pass;
    unterminated constructs mask to end-of-text (fail-soft)."""
    out: list[str] = []
    depth, i, n = 0, 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif ch == "'":
            j = i + 1
            while j < n:
                if sql.startswith("''", j):
                    j += 2
                elif sql[j] == "'":
                    j += 1
                    break
                else:
                    j += 1
        elif ch == '"':
            j = sql.find('"', i + 1)
            j = n if j < 0 else j + 1
            out.append(sql[i:j] if depth == 0 else " " * (j - i))
            i = j
            continue
        elif ch == "(":
            depth += 1
            out.append("(" if depth == 1 else " ")
            i += 1
            continue
        elif ch == ")":
            depth = max(0, depth - 1)
            out.append(")" if depth == 0 else " ")
            i += 1
            continue
        else:
            out.append(ch if depth == 0 else " ")
            i += 1
            continue
        out.append(" " * (j - i))
        i = j
    return "".join(out)


def _top_level_tokens(masked: str) -> list[str]:
    """Tokens of ``_mask_sql_top_level`` output: quoted identifiers (quotes
    kept), bare words (identifiers/numbers), and single-char punctuation."""
    tokens: list[str] = []
    i, n = 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch == '"':
            j = masked.find('"', i + 1)
            j = n - 1 if j < 0 else j
            tokens.append(masked[i:j + 1])
            i = j + 1
        elif ch.isalnum() or ch in "_$":
            j = i
            while j < n and (masked[j].isalnum() or masked[j] in "_$"):
                j += 1
            tokens.append(masked[i:j])
            i = j
        elif ch.isspace():
            i += 1
        else:
            tokens.append(ch)
            i += 1
    return tokens


def _ident(token: str) -> "str | None":
    """Bare column/table name from one identifier token — ``None`` when the
    token is not an identifier (numbers, operators, ``*``)."""
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1] or None
    if token and (token[0].isalpha() or token[0] == "_"):
        return token
    return None


def _column_name(item: list[str]) -> "str | None":
    """Result-column name of ONE SELECT-list item (token slice): the ``AS``
    alias, or the last segment of a plain dotted identifier. ``None`` ⇒ not
    enumerable (``*``, bare expressions, literals)."""
    if len(item) >= 2 and item[-2].upper() == "AS":
        return _ident(item[-1])
    dotted_ident = bool(item) and len(item) % 2 == 1 and all(
        (tok == "." if k % 2 else _ident(tok) is not None)
        for k, tok in enumerate(item))
    return _ident(item[-1]) if dotted_ident else None


def _select_columns(select_tokens: list[str]) -> list[str]:
    """Column names when EVERY comma-separated item is enumerable — one
    opaque item (``*``, an unaliased expression) makes the whole list ``[]``
    honestly (a partial list would misstate the projection)."""
    columns: list[str] = []
    item: list[str] = []
    for tok in select_tokens + [","]:
        if tok != ",":
            item.append(tok)
            continue
        name = _column_name(item)
        if name is None:
            return []
        if name not in columns:
            columns.append(name)
        item = []
    return columns[:MAX_ENTITY_COLUMNS]


def _final_select_parse(sql: Any) -> "tuple[list[str], list[str]] | None":
    """``(relation_parts, select_list_tokens)`` of the FINAL top-level
    SELECT — ``None`` when the text is not a bounded SELECT/WITH statement
    with a parseable depth-0 ``FROM`` relation."""
    if (not isinstance(sql, str) or not sql.strip()
            or len(sql) > MAX_ENTITY_SQL_CHARS):
        return None
    tokens = _top_level_tokens(_mask_sql_top_level(sql))
    if not tokens or tokens[0].upper() not in ("SELECT", "WITH"):
        return None
    uppers = [token.upper() for token in tokens]
    if "SELECT" not in uppers:
        return None
    sel = len(uppers) - 1 - uppers[::-1].index("SELECT")
    try:
        frm = uppers.index("FROM", sel + 1)
    except ValueError:
        return None
    parts: list[str] = []
    i = frm + 1
    while i < len(tokens) and _ident(tokens[i]) is not None:
        parts.append(tokens[i])
        if i + 1 < len(tokens) and tokens[i + 1] == ".":
            i += 2
        else:
            break
    if not parts:
        return None
    select_tokens = tokens[sel + 1:frm]
    if select_tokens and select_tokens[0].upper() in ("DISTINCT", "ALL"):
        select_tokens = select_tokens[1:]
    return parts, select_tokens


def _is_discovery_relation(parts: list[str]) -> bool:
    head = parts[0].strip('"').lower()
    return ((len(parts) > 1 and head in _DISCOVERY_SCHEMAS)
            or (len(parts) == 1 and head.startswith("pg_")))


def is_discovery_statement(sql: Any) -> bool:
    """True when the statement reads catalog metadata (``information_schema``
    / ``pg_catalog`` qualified, or an unqualified ``pg_*`` relation) — the
    Wave-5 writer predicate for skipping discovery statements when choosing
    the LAST data-bearing statement. Fail-soft: unparseable ⇒ False."""
    parsed = _final_select_parse(sql)
    return parsed is not None and _is_discovery_relation(parsed[0])


def sql_statement_entity(sql: Any) -> "tuple[str, list[str]]":
    """``(table, columns)`` parsed from ONE executed SQL statement — the
    Wave-5 WRITER fallback when the projection proof yields nothing.

    Deliberately small (no SQL-dialect ambitions): ``table`` is the primary
    FROM relation of the final top-level SELECT (schema qualifier and quotes
    stripped; JOINs ⇒ the primary relation only); ``columns`` are the
    SELECT-list names when every item is enumerable (plain identifiers or
    ``AS`` aliases) — ``*`` or any unaliased expression ⇒ ``[]`` honestly.
    Discovery statements, non-SELECT text, over-cap input, and anything
    unparseable ⇒ today's honest ``("", [])``. Never estimates or guesses."""
    parsed = _final_select_parse(sql)
    if parsed is None or _is_discovery_relation(parsed[0]):
        return "", []
    parts, select_tokens = parsed
    return parts[-1].strip('"'), _select_columns(select_tokens)
