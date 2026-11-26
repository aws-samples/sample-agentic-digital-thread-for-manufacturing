# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Trusted, invocation-local connector capture for response integrity."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from typing import Any, Iterable

from .response_integrity_contracts import (
    DEFAULT_CURRENT_MAX_AGE_SECONDS, FUTURE_TIMESTAMP_SKEW_SECONDS,
    MAX_EVIDENCE_COUNT, MAX_LINEAGE_RECORDS, MAX_RESULT_BYTES, MAX_RESULT_DEPTH,
    MAX_RESULT_NODES, MAX_SCALAR_LENGTH, MAX_SYSTEMS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN, MAX_TOTAL_TOOL_PAYLOAD_BYTES, PRIVATE_SOURCE_STATE_KEY,
    SOURCE_METADATA_KEY, Completeness, FreshnessClass, ReasonCode, SourceEvidence,
    SourceMetadata, requires_current_state,
)
from .response_integrity_security import (
    classified_string_field, free_text_field, safe_renderable_scalar, sensitive_field, sensitive_text,
)
from .response_integrity_sql import PhysicalSchema, ProjectionProof, validate_direct_projections

_BLOCKED_SCALAR_FIELDS = {
    SOURCE_METADATA_KEY, "columns", "headers", "header", "row_count", "item_count",
    "count", "success_count", "error_count", "scanned_bytes", "budget_bytes",
    "status", "error", "error_type", "message", "guidance", "queried_at",
    "truncated", "continuation", "nexttoken", "next_token", "rejected", "over_budget",
    "expected", "returned", "expected_count", "returned_count", "capped_at_limit",
}
_GENERIC_TIMESTAMP_FIELDS = {
    "timestamp", "time", "time_utc", "observed_at", "source_timestamp",
    "event_time", "updated_at", "created_at", "date",
}
_STRUCTURAL_VALUE_TIME_FIELDS = {
    "timestamp", "time_utc", "observed_at", "source_timestamp",
}
_TIME_PREFIXES = {
    "__adt_source_time__": "end", "__adt_source_start__": "start",
    "__adt_source_end__": "end",
}
_ENTITY_SUFFIXES = ("_id", "_identifier", "_code", "_name", "_label", "_key")
_ENTITY_EXACT = {"id", "identifier", "code", "name", "label", "key"}
_FAILURE_STATUSES = {"error", "failed", "failure", "unavailable", "rejected", "cancelled", "canceled"}
_PARTIAL_STATUSES = {"partial", "incomplete", "over_budget", "truncated"}


class _BudgetExceeded(Exception):
    pass


class _PrivateObservation:
    __slots__ = ("public", "canonical_fingerprint", "canonical_value", "entity_label")

    def __init__(self, public: SourceEvidence, canonical_fingerprint: str,
                 canonical_value: Any, entity_label: str = "") -> None:
        self.public = public
        self.canonical_fingerprint = canonical_fingerprint
        self.canonical_value = canonical_value
        self.entity_label = entity_label


class _CaptureState:
    __slots__ = (
        "observations", "completeness", "reason_codes", "current_intent", "nonce",
        "tool_calls", "payload_bytes", "systems",
    )

    def __init__(self) -> None:
        self.observations: list[_PrivateObservation] = []
        self.completeness: list[Completeness] = []
        self.reason_codes: list[ReasonCode] = []
        self.current_intent = False
        self.nonce = secrets.token_hex(16)
        self.tool_calls = 0
        self.payload_bytes = 0
        self.systems: set[str] = set()

    def budget_exceeded(self) -> None:
        if ReasonCode.TRAVERSAL_BUDGET_EXCEEDED not in self.reason_codes:
            self.reason_codes.append(ReasonCode.TRAVERSAL_BUDGET_EXCEEDED)
        self.completeness.append(Completeness.PARTIAL)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.replace(".", "", 1).isdigit() and len(text.split(".", 1)[0]) == 10:
            try:
                return datetime.fromtimestamp(float(text), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _canonical_decimal(value: Any) -> str | None:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or abs(number) > Decimal("1e30"):
        return None
    if len(number.as_tuple().digits) > 18:
        return None
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical(value: Any) -> tuple[str, str] | None:
    if isinstance(value, bool):
        return "boolean", "true" if value else "false"
    if isinstance(value, int):
        if abs(value) > 10**30:
            return None
        return "integer", str(value)
    if isinstance(value, (float, Decimal)):
        text = _canonical_decimal(value)
        return ("number", text) if text is not None else None
    if isinstance(value, str):
        if len(value) > MAX_SCALAR_LENGTH or sensitive_text(value):
            return None
        return "string", value
    return None


def _fingerprint(value: Any) -> str:
    canonical = _canonical(value)
    if canonical is None:
        return ""
    return hashlib.sha256(f"{canonical[0]}:{canonical[1]}".encode()).hexdigest()


def _freshness(observed: datetime | None, queried: datetime, max_age: int) -> FreshnessClass:
    if observed is None:
        return FreshnessClass.UNKNOWN
    return FreshnessClass.CURRENT if (queried - observed).total_seconds() <= max_age else FreshnessClass.HISTORICAL


def _sql_typed_value(value: Any, data_type: str) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    low_type = data_type.lower()
    try:
        if any(token in low_type for token in ("tinyint", "smallint", "bigint", "integer", "int")):
            return int(text) if re.fullmatch(r"[+-]?\d+", text) else value
        if any(token in low_type for token in ("decimal", "numeric", "double", "float", "real")):
            parsed = Decimal(text)
            return parsed if _canonical_decimal(parsed) is not None else value
        if "bool" in low_type and text.lower() in {"true", "false"}:
            return text.lower() == "true"
    except (ValueError, OverflowError):
        return value
    return value


def _is_time_field(key: Any) -> bool:
    low = str(key).lower()
    return low in _GENERIC_TIMESTAMP_FIELDS or any(low.startswith(prefix) for prefix in _TIME_PREFIXES)


def _budget_check(value: Any) -> int:
    nodes = size = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal nodes, size
        nodes += 1
        if nodes > MAX_RESULT_NODES or depth > MAX_RESULT_DEPTH:
            raise _BudgetExceeded
        if isinstance(item, dict):
            for key, child in item.items():
                size += len(str(key).encode("utf-8", "replace"))
                if size > MAX_RESULT_BYTES:
                    raise _BudgetExceeded
                walk(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child, depth + 1)
        elif item is not None:
            text = str(item)
            if len(text) > MAX_SCALAR_LENGTH:
                raise _BudgetExceeded
            size += len(text.encode("utf-8", "replace"))
            if size > MAX_RESULT_BYTES:
                raise _BudgetExceeded

    walk(value, 0)
    return size


def _parse_json_text(text: Any) -> Any | None:
    if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > MAX_RESULT_BYTES:
        return None
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped, parse_float=Decimal)
    except (ValueError, TypeError, RecursionError):
        return None


def bounded_payloads(result: Any, max_chars: int = MAX_RESULT_BYTES) -> list[Any]:
    del max_chars
    candidates: list[Any] = []
    content = result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
    if content is None and isinstance(result, (dict, list)):
        candidates.append(result)
    elif isinstance(content, (dict, list)):
        candidates.extend(content[:32] if isinstance(content, list) else [content])
    for item in list(candidates):
        if isinstance(item, dict) and isinstance(item.get("json"), (dict, list)):
            candidates.append(item["json"])
        parsed = _parse_json_text(item.get("text") if isinstance(item, dict) else None)
        if parsed is not None:
            candidates.append(parsed)
    return [
        item for item in candidates
        if isinstance(item, (dict, list))
        and not (isinstance(item, dict) and ("json" in item or "text" in item) and len(item) <= 2)
    ][:32]


def _pagination_is_partial(key: str, value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
    token_keys = {
        "continuation", "continuationtoken", "nexttoken", "nextpagetoken",
        "nextpage", "cursor", "odatanextlink", "lastevaluatedkey",
        "nextcontinuationtoken",
    }
    if normalized in token_keys:
        return value not in (None, "", False, {}, [])
    if normalized in {"hasmore", "istruncated"}:
        return value is True
    if normalized in {"links", "pageinfo", "pagination"}:
        return not isinstance(value, dict) or bool(value)
    return False


def _state(invocation_state: dict) -> _CaptureState:
    current = invocation_state.get(PRIVATE_SOURCE_STATE_KEY)
    if isinstance(current, _CaptureState):
        return current
    current = _CaptureState()
    invocation_state[PRIVATE_SOURCE_STATE_KEY] = current
    return current


def initialize_integrity_state(invocation_state: dict, prompt: str = "") -> None:
    invocation_state[PRIVATE_SOURCE_STATE_KEY] = _CaptureState()
    _state(invocation_state).current_intent = requires_current_state(prompt)


def _payload_completeness(payload: Any) -> Completeness:
    unavailable = partial = False
    nodes = 0

    def walk(value: Any, depth: int) -> None:
        nonlocal unavailable, partial, nodes
        nodes += 1
        if nodes > MAX_RESULT_NODES or depth > MAX_RESULT_DEPTH:
            partial = True
            return
        if isinstance(value, dict):
            for key, child in value.items():
                low = str(key).lower()
                if low == SOURCE_METADATA_KEY:
                    continue
                if low == "status" and isinstance(child, str):
                    status = child.strip().lower()
                    unavailable = unavailable or status in _FAILURE_STATUSES
                    partial = partial or status in _PARTIAL_STATUSES
                elif low in {"error", "errors"} and child not in (None, "", [], {}):
                    unavailable = True
                elif low in {"rejected", "over_budget"} and child is True:
                    unavailable = True
                elif low in {"truncated", "capped_at_limit"} and child is True:
                    partial = True
                elif _pagination_is_partial(low, child):
                    partial = True
                walk(child, depth + 1)
            expected = value.get("expected_count", value.get("expected"))
            returned = value.get("returned_count", value.get("returned"))
            if isinstance(expected, int) and isinstance(returned, int) and returned < expected:
                partial = True
        elif isinstance(value, list):
            inspected = value[:256]
            if len(value) > len(inspected):
                partial = True
            shapes = {"dict" if isinstance(item, dict) else "list" if isinstance(item, list) else "scalar" for item in inspected}
            if len(shapes) > 1:
                partial = True
            for item in inspected:
                walk(item, depth + 1)

    walk(payload, 0)
    if unavailable:
        return Completeness.UNAVAILABLE
    return Completeness.PARTIAL if partial else Completeness.COMPLETE


def raw_envelope_completeness(result: Any) -> tuple[list[Any], bool, Completeness]:
    """Inspect raw cardinality/bytes/completeness before bounded payload slicing."""
    content = result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
    overflow = isinstance(content, list) and len(content) > 32
    try:
        _budget_check(result)
    except (RecursionError, _BudgetExceeded):
        overflow = True
    payloads = bounded_payloads(result)
    completeness = _payload_completeness(result)
    return payloads, overflow, completeness


def _entity_label(columns: list[Any], row: list[Any]) -> str:
    candidates: list[str] = []
    for index, column in enumerate(columns):
        if index >= len(row) or row[index] is None or sensitive_field(str(column)):
            continue
        low = str(column).lower()
        if low in _ENTITY_EXACT or low.endswith(_ENTITY_SUFFIXES):
            canonical = _canonical(row[index])
            if canonical and safe_renderable_scalar(row[index]):
                candidates.append(str(row[index])[:160])
    return candidates[0] if len(candidates) == 1 else ""


def _time_alias(column: Any) -> tuple[str, str] | None:
    low = str(column).lower()
    for prefix, role in _TIME_PREFIXES.items():
        if low.startswith(prefix) and len(low) > len(prefix):
            return low[len(prefix):], role
    return None


def _row_time_bindings(columns: list[Any], row: list[Any]) -> dict[str, tuple[datetime | None, datetime | None]]:
    bindings: dict[str, list[datetime | None]] = {}
    for index, column in enumerate(columns):
        if index >= len(row):
            continue
        alias = _time_alias(column)
        if alias is None:
            continue
        target, role = alias
        dt = _as_utc(row[index])
        if dt is None:
            continue
        pair = bindings.setdefault(target, [None, None])
        pair[0 if role == "start" else 1] = dt
    return {field: (pair[0] or pair[1], pair[1]) for field, pair in bindings.items()}


def _record_observation(
    state: _CaptureState, *, value: Any, field: str, physical_field: str,
    locator: str, source_start: datetime | None, source_end: datetime | None,
    entity_label: str, system_id: str, boundary: str, tool_name: str,
    queried_at: datetime, completeness: Completeness,
    current_max_age_seconds: int, trusted_schema_field: bool = False,
) -> None:
    canonical = _canonical(value)
    # String values: a generic (structural) capture only admits token-classified
    # field names; a SCHEMA-PROVEN capture (the value came out of a validated
    # direct projection of a physical base-relation column) admits any
    # non-free-text column — the PII screen (safe_renderable_scalar) still
    # applies to the VALUE either way.
    if isinstance(value, str):
        blocked = (free_text_field(field) if trusted_schema_field
                   else not classified_string_field(field))
    else:
        blocked = False
    if sensitive_field(field) or canonical is None or not safe_renderable_scalar(value) or blocked:
        return
    if len(state.observations) >= MAX_EVIDENCE_COUNT:
        state.budget_exceeded()
        return
    future = queried_at + timedelta(seconds=FUTURE_TIMESTAMP_SKEW_SECONDS)
    if (source_start and source_start > future) or (source_end and source_end > future):
        if ReasonCode.FUTURE_SOURCE_TIMESTAMP not in state.reason_codes:
            state.reason_codes.append(ReasonCode.FUTURE_SOURCE_TIMESTAMP)
        return
    field = str(field)[:256] or "value"
    locator = str(locator)[:256] or "item:0"
    digest = "|".join((
        state.nonce, system_id, boundary, tool_name, field, locator,
        canonical[0], canonical[1], _iso(source_start) or "", _iso(source_end) or "",
    ))
    evidence_id = "ev-" + hashlib.sha256(digest.encode()).hexdigest()[:20]
    if any(item.public.evidence_id == evidence_id for item in state.observations):
        return
    public = SourceEvidence(
        evidence_id=evidence_id, system_id=system_id, boundary=boundary,
        tool_name=tool_name[:120], queried_at=queried_at,
        source_observed_start=source_start, source_observed_end=source_end,
        freshness=_freshness(source_end, queried_at, current_max_age_seconds),
        completeness=completeness, semantic_field=field,
        physical_field=str(physical_field or "")[:256], locator=locator,
        canonical_type=canonical[0],
    )
    state.observations.append(_PrivateObservation(
        public, _fingerprint(value), value, entity_label=entity_label,
    ))


def _sql_time_bindings(
    columns: list[Any], row: list[Any], proof: ProjectionProof,
) -> dict[str, tuple[datetime | None, datetime | None]]:
    bindings: dict[str, list[datetime | None]] = {}
    proven = proof.by_output()
    for index, raw_column in enumerate(columns):
        column = str(raw_column).lower()
        projection = proven.get(column)
        alias = _time_alias(column)
        if index >= len(row) or projection is None or alias is None or not projection.is_reserved_time:  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
            continue
        parsed = _as_utc(row[index])
        if parsed is None:
            continue
        target, role = alias
        pair = bindings.setdefault(target, [None, None])
        pair[0 if role == "start" else 1] = parsed
    return {field: (pair[0] or pair[1], pair[1]) for field, pair in bindings.items()}


def _extract_sql_observations(payload: dict, **kwargs: Any) -> None:
    columns, rows, proof = payload.get("columns"), payload.get("rows"), kwargs.pop("projection_proof", None)
    if not isinstance(columns, list) or not isinstance(rows, list) or not isinstance(proof, ProjectionProof):
        return
    expected = [item.output_name.lower() for item in proof.projections]
    if [str(item).lower() for item in columns] != expected:
        kwargs["state"].budget_exceeded()
        return
    state: _CaptureState = kwargs["state"]
    for row_index, raw_row in enumerate(rows):
        if not isinstance(raw_row, (list, tuple)) or len(raw_row) != len(columns):
            state.budget_exceeded()
            continue
        row = list(raw_row)
        bindings = _sql_time_bindings(columns, row, proof)
        entity = _entity_label(columns, row)
        for column_index, projection in enumerate(proof.projections):
            if projection.is_reserved_time or row[column_index] is None:  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
                continue
            field = projection.output_name
            start, end = bindings.get(field.lower(), (None, None))
            _record_observation(
                value=_sql_typed_value(row[column_index], projection.data_type),
                field=field, physical_field=f"{projection.table}.{projection.column}",
                locator=f"row:{row_index}",
                source_start=start, source_end=end, entity_label=entity,
                trusted_schema_field=True, **kwargs,
            )
            if len(state.observations) >= MAX_EVIDENCE_COUNT:
                return


def _local_structural_time(value: dict, field: str) -> tuple[datetime | None, datetime | None]:
    explicit: list[tuple[str, datetime]] = []
    for key, child in value.items():
        alias = _time_alias(key)
        if alias and alias[0] == field.lower():
            dt = _as_utc(child)
            if dt:
                explicit.append((alias[1], dt))
    if explicit:
        starts = [dt for role, dt in explicit if role == "start"]
        ends = [dt for role, dt in explicit if role == "end"]
        end = ends[0] if len(ends) == 1 else None
        start = starts[0] if len(starts) == 1 else end
        return start, end
    if field != "value":
        return None, None
    plausible = [_as_utc(value[key]) for key in _STRUCTURAL_VALUE_TIME_FIELDS if key in value]
    times = [item for item in plausible if item is not None]
    return (times[0], times[0]) if len(times) == 1 else (None, None)


def _dict_entity_label(value: dict) -> str:
    candidates = [
        str(child)[:160] for key, child in value.items()
        if (str(key).lower() in _ENTITY_EXACT or str(key).lower().endswith(_ENTITY_SUFFIXES))
        and _canonical(child) is not None and not sensitive_field(str(key))
    ]
    return candidates[0] if len(candidates) == 1 else ""


def _extract_structured_observations(payload: Any, **kwargs: Any) -> None:
    state: _CaptureState = kwargs["state"]

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if len(state.observations) >= MAX_EVIDENCE_COUNT:
            state.budget_exceeded()
            return
        if isinstance(value, dict):
            entity = _dict_entity_label(value)
            for key, child in value.items():
                field = str(key)
                low = field.lower()
                if key == SOURCE_METADATA_KEY or _is_time_field(key) or sensitive_field(field):
                    continue  # sensitive parents taint and stop their entire subtree
                if isinstance(child, (dict, list)):
                    walk(child, path + (field,))
                elif low not in _BLOCKED_SCALAR_FIELDS and child is not None:
                    start, end = _local_structural_time(value, low)
                    _record_observation(
                        value=child, field=field, physical_field="",
                        locator=":".join(path) or "item:0",
                        source_start=start, source_end=end, entity_label=entity, **kwargs,
                    )
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + (str(index),))

    walk(payload, ())


def _capture_payload(
    payload: Any, state: _CaptureState, projection_proof: ProjectionProof,
    field_allowlist: set[str], kwargs: dict[str, Any],
) -> bool:
    completeness = _payload_completeness(payload)
    state.completeness.append(completeness)
    try:
        state.payload_bytes += _budget_check(payload)
    except (RecursionError, _BudgetExceeded):
        state.budget_exceeded()
        return True
    if state.payload_bytes > MAX_TOTAL_TOOL_PAYLOAD_BYTES:
        state.budget_exceeded()
        return False
    capture_args = {**kwargs, "completeness": completeness}
    before = len(state.observations)
    is_sql = (
        isinstance(payload, dict) and isinstance(payload.get("columns"), list)
        and isinstance(payload.get("rows"), list)
    )
    if is_sql and projection_proof.valid:
        _extract_sql_observations(payload, projection_proof=projection_proof, **capture_args)
    elif is_sql:
        state.completeness.append(Completeness.PARTIAL)
        for reason in (ReasonCode.STRUCTURAL_LINEAGE_MISMATCH, ReasonCode.SOURCE_PARTIAL):
            if reason not in state.reason_codes:
                state.reason_codes.append(reason)
    else:
        _extract_structured_observations(payload, **capture_args)
    if field_allowlist:
        state.observations[before:] = [
            item for item in state.observations[before:]
            if item.public.semantic_field.lower() in field_allowlist
        ]
    return True


def capture_trusted_tool_result(
    result: Any, invocation_state: dict, *, system_id: str, boundary: str,
    tool_name: str, current_intent: bool = False, queried_at: datetime | None = None,
    current_max_age_seconds: int = DEFAULT_CURRENT_MAX_AGE_SECONDS,
    eligible_fields: Iterable[str] | None = None, executed_query: str = "",
    physical_schema: PhysicalSchema | None = None,
) -> None:
    """Mint evidence only from a server-owned connector boundary."""
    state = _state(invocation_state)
    state.current_intent = state.current_intent or current_intent
    state.tool_calls += 1
    state.systems.add(system_id)
    if state.tool_calls > MAX_TOOL_CALLS_PER_TURN or len(state.systems) > MAX_SYSTEMS_PER_TURN:
        state.budget_exceeded(); return
    payloads, raw_overflow, raw_completeness = raw_envelope_completeness(result)
    if raw_overflow:
        state.budget_exceeded()
    if raw_completeness != Completeness.COMPLETE:
        state.completeness.append(raw_completeness)
    if not payloads:
        state.completeness.append(Completeness.UNAVAILABLE); return
    projection = validate_direct_projections(executed_query, physical_schema or {})
    kwargs = {
        "state": state, "system_id": system_id[:100], "boundary": boundary,
        "tool_name": tool_name[:120],
        "queried_at": (queried_at or utc_now()).astimezone(timezone.utc),
        "current_max_age_seconds": current_max_age_seconds,
    }
    allowlist = {str(item).lower() for item in eligible_fields or ()}
    for payload in payloads:
        if not _capture_payload(payload, state, projection, allowlist, kwargs):
            return
    if len(state.observations) > MAX_LINEAGE_RECORDS:
        state.observations[:] = state.observations[:MAX_LINEAGE_RECORDS]
        state.budget_exceeded()


def capture_private_source_evidence(result: Any, invocation_state: dict, **trusted: Any) -> None:
    required = {"system_id", "boundary", "tool_name"}
    if required.issubset(trusted):
        capture_trusted_tool_result(result, invocation_state, **trusted)


def _metadata_observed_times(payload: Any) -> list[datetime]:
    observed: list[datetime] = []
    if isinstance(payload, dict) and isinstance(payload.get("columns"), list) and isinstance(payload.get("rows"), list):
        columns = payload["columns"]
        for raw_row in payload["rows"][:10_000]:
            if not isinstance(raw_row, (list, tuple)):
                continue
            for start, end in _row_time_bindings(columns, list(raw_row)).values():
                observed.extend(item for item in (start, end) if item)
        return observed

    def walk(value: Any, depth: int = 0) -> None:
        if depth > MAX_RESULT_DEPTH:
            return
        if isinstance(value, dict):
            if "value" in value:
                observed.extend(item for item in _local_structural_time(value, "value") if item)
            for key, child in value.items():
                if key != SOURCE_METADATA_KEY and not sensitive_field(str(key)) and isinstance(child, (dict, list)):
                    walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:10_000]:
                walk(child, depth + 1)
    walk(payload)
    return observed


def attach_source_metadata(
    payload: Any, *, boundary: str, system_id: str = "", queried_at: datetime | None = None,
    current_max_age_seconds: int = DEFAULT_CURRENT_MAX_AGE_SECONDS,
) -> Any:
    """Attach informational metadata using only exact value-time bindings."""
    if not isinstance(payload, dict):
        return payload
    queried = (queried_at or utc_now()).astimezone(timezone.utc)
    observed = _metadata_observed_times(payload)
    start, end = (min(observed), max(observed)) if observed else (None, None)
    row_count = payload.get("row_count")
    metadata = SourceMetadata(
        evidence_id="info-" + secrets.token_hex(8), system_id=system_id,
        boundary=boundary, queried_at=queried, source_observed_start=start,
        source_observed_end=end, freshness=_freshness(end, queried, current_max_age_seconds),
        completeness=_payload_completeness(payload),
        row_count=row_count if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count >= 0 else None,
        item_count=(len([key for key in payload if key != SOURCE_METADATA_KEY]) if boundary == "sitewise" else None),
    )
    out = dict(payload)
    out[SOURCE_METADATA_KEY] = metadata.model_dump(mode="json")
    return out
