# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Where an identifier came from this turn, and the delegation that skips its join.

A system agent's result can hand the orchestrator an identifier of its own
(a part code in a run record). When the orchestrator then sends that value to
a system that keys the same records under another field, and the knowledge
graph records a join into that system from a field of a third system, the
target finds nothing and the agent guesses from a part of the value. The
recorded join says how to get there: read the joined field from the third
system for that record and pass that value.

Two halves, both deterministic, no value is ever looked up or substituted:

* system side. The capture hook keeps, per SQL result, the values of each
  result column whose source column the SELECT list proves
  (:func:`record_result`, ``select_lineage.output_sources``). When the call
  asked for it (the reserved :data:`RESERVED_ARG` the orchestrator stamps),
  :func:`with_line` appends ONE bounded line naming the cross-system key
  columns among them and WHERE in the answer their values occur, each
  position bound to its exact token by a short digest, or an empty line
  (:data:`ACK_LINE`) when there are none, so a caller can tell an answered
  request from an ignored one. The line never holds
  a value: the runtime cannot verify that the caller is the
  orchestrator (any caller of the tool can send the argument), so the line
  gives a caller nothing the answer has not already given it. Model text
  that reads as the line is made inert first, so the final line is always
  the engine's;
* orchestrator side. The delegation wrapper strips that line from the result
  before anything else sees it (the model, the activity trace, the stored
  conversation; :func:`strip_result`), reads each value back from the
  answer at its position and records the values, and the concepts their
  column names, per source column. :class:`TurnGuard` then refuses a
  delegation only when every condition in :meth:`TurnGuard.refusal` holds,
  through a recorded join about the value's own concept, and at most once
  per target and value in a turn: a repeat goes through unchanged.

Bounded, generic, no dataset knowledge. Pure except the per-turn context
variable the route seed sets (:func:`remember_turn_facts`).
"""
from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Reserved tool-call argument asking a system agent for the line. Popped at
#: the system runtime's ASGI seam (``runtime_asgi``) before any model sees it.
RESERVED_ARG = "_adt_identifier_sources"
REQUESTED = "requested"
#: The line's fixed opening; its body is compact JSON.
LINE_PREFIX = "[engine] identifier-sources v1: "
#: The line of a call that was asked and has no key column to name.
ACK_LINE = LINE_PREFIX + "[]"
#: What a copy of the opening inside model text is rewritten to.
_INERT_PREFIX = "[engine-quoted] identifier-sources v1: "
#: The invocation-state keys of the per-call record and its column types (system side).
STATE_KEY = "_adt_identifier_sources"
TYPES_KEY = "_adt_identifier_source_types"
MAX_FIELDS = 8
MAX_VALUES = 12
MAX_LINE_CHARS = 4000
#: Hex characters of a position's token digest.
DIGEST_CHARS = 12
#: Most concept ids one source column carries in the line.
MAX_CONCEPTS = 4
_CONCEPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
#: A value that can be recorded: one token of identifier characters, no
#: space, quote, ``@`` or markup, 3 to 64 characters.
_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:#-]{2,63}")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,127}")
_LINE_RE = re.compile(r"\n\n" + re.escape(LINE_PREFIX) + r"(?P<body>\[[^\n]*\])\s*\Z")
#: Words that make a value a prefix or family search when they come just
#: before it ("items beginning X"), so the value is not sent as an identifier.
_FAMILY_WORDS = re.compile(
    r"\b(?:prefix(?:ed)?|start(?:s|ing)?|begin(?:s|ning)?|like|family|series)\b[^\n]{0,24}\Z",
    re.IGNORECASE)
_FAMILY_SPAN = 40


# --------------------------------------------------------------------------- #
# System side: record the result columns, render the line
# --------------------------------------------------------------------------- #
def _value_text(value) -> str | None:
    """``value`` as a recordable key: one identifier token (:data:`_VALUE_RE`)
    with the shape an exact cross-system join may ride on
    (``identity_affinity.is_join_shape``: two or more segments, not all
    digits), that the value-level personal-data redaction leaves unchanged
    (``pii.redact_pii``). A bare number or a single word is no key worth
    tracing, and a value holding personal data is never recorded."""
    from .identity_affinity import is_join_shape, literal_shape
    from .pii import redact_pii
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not (_VALUE_RE.fullmatch(text) and is_join_shape(literal_shape(text))):
        return None
    return text if redact_pii(text) == text else None


def _projected_sources(sql: str, columns, physical_schema) -> list | None:
    """``[(result column index, (relation, column))]`` for the result columns
    whose source the SELECT list proves, or None when it cannot be read."""
    from .select_lineage import output_sources
    tables = {str(t).lower(): {str(c).lower() for c in (entry.get("columns") or {})}
              for t, entry in (physical_schema or {}).items() if isinstance(entry, dict)}
    names = [str(c).lower() for c in columns or ()]
    found = output_sources(sql, tables)
    if not found or len(found) != len(names):
        return None
    return [(i, source) for i, (name, (out, source)) in enumerate(zip(names, found))
            if source is not None and out == name and names.count(name) == 1]


def _column_type(physical_schema, relation: str, column: str) -> str:
    for table, entry in (physical_schema or {}).items():
        if str(table).lower() == relation and isinstance(entry, dict):
            for name, declared in (entry.get("columns") or {}).items():
                if str(name).lower() == column:
                    return str(declared or "")
    return ""


def record_result(invocation_state: dict, sql: str, payload, physical_schema) -> None:
    """Keep the bounded values of each proven source column of one SQL
    result (``payload`` with ``columns`` and ``rows``), with the column's
    declared type. PII-named columns are never kept
    (``pii.column_looks_pii``). Never raises."""
    try:
        if not (isinstance(payload, dict) and isinstance(payload.get("rows"), list)
                and isinstance(sql, str) and sql.strip()):
            return
        from .pii import column_looks_pii
        sources = _projected_sources(sql, payload.get("columns"), physical_schema) or []
        record = invocation_state.setdefault(STATE_KEY, {})
        types = invocation_state.setdefault(TYPES_KEY, {})
        for index, (relation, column) in sources:
            if column_looks_pii(column):
                continue
            key = f"{relation}.{column}"
            if key not in record and len(record) >= MAX_FIELDS:
                continue
            types.setdefault(key, _column_type(physical_schema, relation, column))
            kept = record.setdefault(key, [])
            for row in payload["rows"]:
                value = _value_text(row[index]) if isinstance(row, (list, tuple)) \
                    and len(row) > index else None
                if value and value not in kept and len(kept) < MAX_VALUES:
                    kept.append(value)
    except Exception as exc:  # noqa: BLE001 - the record is an addition, never required
        logger.debug("identifier sources record skipped (%s)", type(exc).__name__)


def recorded_types(invocation_state) -> dict:
    """``{"table.column": declared type}`` of the columns this call recorded."""
    types = invocation_state.get(TYPES_KEY) if isinstance(invocation_state, dict) else None
    return dict(types) if isinstance(types, dict) else {}


def _identifier_records(invocation_state, key_fields) -> list:
    """``[(table.column, values, concepts)]`` of the recorded key columns,
    sorted; ``key_fields`` maps each key column to the concepts it names (a
    plain set of key columns carries none)."""
    record = invocation_state.get(STATE_KEY) if isinstance(invocation_state, dict) else None
    if not isinstance(record, dict) or not key_fields:
        return []
    out = []
    for key in sorted(record):
        table, _dot, column = key.rpartition(".")
        field_key = (table.lower(), column.lower())
        if field_key in key_fields and record[key]:
            concepts = key_fields.get(field_key) if isinstance(key_fields, dict) else ()
            out.append((key, list(record[key]), tuple(concepts or ())))
    return out


def _clean_concepts(concepts) -> list:
    """The bounded, sorted concept ids of ``concepts`` that are valid ids."""
    return sorted({str(c) for c in concepts or () if _CONCEPT_RE.fullmatch(str(c))})[:MAX_CONCEPTS]


def token_digest(value: str) -> str:
    """A short digest of the exact token, binding a position to it. The token
    is in the answer at that position, so the digest tells a reader nothing
    the answer does not."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def _positions(body: str, values) -> list:
    """``[[start, end, digest]]`` of the first whole-token occurrence in
    ``body`` of each value that occurs there (:func:`token_digest`); a value
    the text does not state is left out."""
    out = []
    for value in values:
        match = _token_re(value).search(body)
        if match:
            out.append([match.start(), match.end(), token_digest(value)])
    return out


def line_for(body: str, fields) -> str:
    """The line for ``fields`` (``[(table.column, values[, concepts])]``) as
    positions in ``body``: each column with the concepts it names and where
    its values occur in that text, never a value itself. Trimmed to
    :data:`MAX_LINE_CHARS` by dropping trailing positions; ``""`` when no
    value occurs in ``body``."""
    kept = []
    for key, values, *rest in fields:
        at = _positions(body, values)
        concepts = _clean_concepts(rest[0] if rest else ())
        if at:
            kept.append({"f": key, **({"c": concepts} if concepts else {}), "at": at})
    while kept:
        line = LINE_PREFIX + json.dumps(kept, separators=(",", ":"), ensure_ascii=True)
        if len(line) <= MAX_LINE_CHARS:
            return line
        kept[-1]["at"].pop()
        if not kept[-1]["at"]:
            kept.pop()
    return ""


def with_line(answer: str, invocation_state: dict, key_fields) -> str:
    """``answer`` with any copy of the line's opening made inert and the line
    appended last: the cross-system key columns (``key_fields``:
    ``{(table, column)}``) whose values this call returned and the answer
    states, or :data:`ACK_LINE` when there are none, so a caller that asked
    can tell a system that answered the request from one that ignored it.
    The line carries no value: no caller identity is verifiable here, so it
    holds only what the answer itself already gives the caller."""
    if not isinstance(answer, str):
        return answer
    body = answer.replace(LINE_PREFIX, _INERT_PREFIX)
    line = line_for(body, _identifier_records(invocation_state, key_fields))
    return f"{body}\n\n{line or ACK_LINE}"


# --------------------------------------------------------------------------- #
# Orchestrator side: the request, the strip
# --------------------------------------------------------------------------- #
def with_request(arguments) -> dict:
    """A copy of ``arguments`` asking the system agent for the line."""
    merged = dict(arguments) if isinstance(arguments, dict) else {}
    merged[RESERVED_ARG] = REQUESTED
    return merged


def _read_back(text: str, span) -> str | None:
    """The key ``text`` holds at ``span`` (``[start, end, digest]``): a whole
    token with a key's shape (:func:`_value_text`) whose digest is the one
    written for it (:func:`token_digest`), else None. A position that moved
    with the text, or now lands on another token, reads back nothing."""
    if not (isinstance(span, list) and len(span) == 3 and isinstance(span[2], str)
            and all(isinstance(i, int) and not isinstance(i, bool) for i in span[:2])):
        return None
    start, end, digest = span
    value = text[start:end] if 0 <= start < end <= len(text) else ""
    if _value_text(value) != value or token_digest(value) != digest:
        return None
    match = _token_re(value).search(text, start)
    return value if match and match.start() == start else None


def parse_records(line_body: str, text: str) -> list | None:
    """The records a line body holds, each value read back from ``text`` at
    its position (a position that does not read back to a key is dropped),
    or None when the line is not one the engine writes."""
    try:
        data = json.loads(line_body)
    except ValueError:
        return None
    if not isinstance(data, list) or len(data) > MAX_FIELDS:
        return None
    out = []
    for item in data:
        if not (isinstance(item, dict) and isinstance(item.get("f"), str)
                and isinstance(item.get("at"), list) and len(item["at"]) <= MAX_VALUES):
            return None
        table, _dot, column = item["f"].rpartition(".")
        concepts = item.get("c", [])
        if not (_NAME_RE.fullmatch(table or "") and _NAME_RE.fullmatch(column or "")
                and isinstance(concepts, list) and _clean_concepts(concepts) == concepts):
            return None
        values = list(dict.fromkeys(v for v in (_read_back(text, s) for s in item["at"]) if v))
        if values:
            out.append({"table": table.lower(), "column": column.lower(), "values": values,
                        "concepts": concepts})
    return out


def split_line(text) -> tuple:
    """``(text without the line, records or None)``. The line is read only
    as the final line; a malformed one is still removed."""
    if not isinstance(text, str) or LINE_PREFIX not in text:
        return text, None
    match = _LINE_RE.search(text)
    if not match:
        return text, None
    body = text[:match.start()]
    return body, parse_records(match.group("body"), body)


def strip_result(result) -> tuple:
    """``(result without the line, records or None, remaining text)``. The
    line is removed from every text block and from a string member of
    ``structuredContent``. The caller's result is never mutated, and is
    returned itself when it carries no line."""
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return result, None, ""
    records, texts, blocks, changed = None, [], [], False
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text, found = split_line(block["text"])
            records = found if found is not None else records
            if text is not block["text"]:
                block, changed = {**block, "text": text}, True
            texts.append(block["text"])
        blocks.append(block)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        cleaned = {k: split_line(v)[0] if isinstance(v, str) else v for k, v in structured.items()}
        changed = changed or any(cleaned[k] is not structured[k] for k in structured)
    if not changed:
        return result, records, "\n".join(texts)
    out = copy.copy(result)
    out["content"] = blocks
    if isinstance(structured, dict):
        out["structuredContent"] = cleaned
    return out, records, "\n".join(texts)


# --------------------------------------------------------------------------- #
# The turn's graph facts (set once by the route seed)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TurnFacts:
    """The user's question and the graph rules the turn's route seed read.
    ``capped``: systems whose rule lists hit the read cap (facts incomplete)."""
    question: str
    systems: frozenset
    join_paths: tuple
    identity_rules: tuple
    value_mappings: tuple
    capped: frozenset = field(default_factory=frozenset)


_TURN_FACTS: "contextvars.ContextVar[TurnFacts | None]" = contextvars.ContextVar(
    "adt_identifier_provenance_facts", default=None)


def _unique(rule_rows, kind: str) -> tuple:
    seen, out = set(), []
    for row in rule_rows or ():
        for rule in (row.get(kind) or []) if isinstance(row, dict) else ():
            key = json.dumps(rule, sort_keys=True, default=str) if isinstance(rule, dict) else None
            if key and key not in seen:
                seen.add(key)
                out.append(rule)
    return tuple(out)


def facts_from(question, rule_rows, cap: int) -> TurnFacts | None:
    """The turn facts of the route seed's rule rows, or None without a question."""
    if not isinstance(question, str) or not question.strip():
        return None
    rows = [r for r in rule_rows or () if isinstance(r, dict) and r.get("system_id")]
    capped = {r["system_id"] for r in rows
              for kind in ("join_paths", "identity_rules", "value_mappings")
              if len(r.get(kind) or []) >= cap}
    return TurnFacts(question=question, systems=frozenset(r["system_id"] for r in rows),
                     join_paths=_unique(rows, "join_paths"),
                     identity_rules=_unique(rows, "identity_rules"),
                     value_mappings=_unique(rows, "value_mappings"),
                     capped=frozenset(capped))


def remember_turn_facts(question, rule_rows, cap: int) -> None:
    """Record this turn's facts (None when they cannot be built). Never raises."""
    try:
        _TURN_FACTS.set(facts_from(question, rule_rows, cap))
    except Exception as exc:  # noqa: BLE001 - no facts means every delegation passes
        logger.debug("identifier provenance facts skipped (%s)", type(exc).__name__)
        _TURN_FACTS.set(None)


def turn_facts() -> TurnFacts | None:
    return _TURN_FACTS.get()


def reset_turn_facts() -> None:
    _TURN_FACTS.set(None)


# --------------------------------------------------------------------------- #
# Requests a system did not answer (logged, never acted on)
# --------------------------------------------------------------------------- #
#: Most characters, and candidate tokens, read to tell whether an answer
#: states a value the line could name (:func:`states_a_value`).
_SCAN_CHARS = 64_000
_SCAN_TOKENS = 2_000
_TOKEN_SCAN_RE = re.compile(r"(?<![A-Za-z0-9_./:#-])[A-Za-z0-9][A-Za-z0-9_./:#-]{2,63}"
                            r"(?![A-Za-z0-9_./:#-])")
#: Most systems one log line names.
MAX_LOGGED_SYSTEMS = 8
_SYSTEM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def states_a_value(text) -> bool:
    """Whether ``text`` states a value the line could name (a token
    :func:`_value_text` accepts). Bounded by :data:`_SCAN_CHARS` and
    :data:`_SCAN_TOKENS`."""
    if not isinstance(text, str):
        return False
    for count, match in enumerate(_TOKEN_SCAN_RE.finditer(text[:_SCAN_CHARS])):
        if count >= _SCAN_TOKENS:
            return False
        if _value_text(match.group(0)):
            return True
    return False


def _unanswered(result, records, text) -> bool:
    """A successful result that states a value and carries no line at all."""
    if records is not None or not isinstance(result, dict) or result.get("status") != "success":
        return False
    blocks = result.get("content") if isinstance(result.get("content"), list) else ()
    if any(isinstance(b, dict) and LINE_PREFIX in str(b.get("text", "")) for b in blocks):
        return False
    return states_a_value(text)


@dataclass
class Unanswered:
    """Delegations that asked for the line and came back successful and
    stating values without it, counted per system. ``turn``: a chat turn
    (the cloud runtime entrypoint or the local chat turn) owns this record
    and logs it once at its end (:meth:`log`); a guard built with no turn
    record logs each system's first one as it happens. Logged only: the
    guard still lets every such call through."""
    turn: bool = True
    counts: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def note(self, system: str) -> None:
        system = system if _SYSTEM_ID_RE.fullmatch(str(system)) else "unknown"
        with self.lock:
            first = system not in self.counts
            self.counts[system] = self.counts.get(system, 0) + 1
        if first and not self.turn:
            _log_unanswered({system: 1})

    def log(self) -> None:
        """ONE line for this record's counts, then forget them. Never raises."""
        try:
            with self.lock:
                counts, self.counts = self.counts, {}
            _log_unanswered(counts)
        except Exception as exc:  # noqa: BLE001 - a log line is never required
            logger.debug("identifier sources summary skipped (%s)", type(exc).__name__)


def _log_unanswered(counts: dict) -> None:
    """System ids and counts only: never a value, question or session."""
    if not counts:
        return
    named = sorted(counts.items())
    listed = ",".join(f"{system}:{n}" for system, n in named[:MAX_LOGGED_SYSTEMS])
    more = len(named) - MAX_LOGGED_SYSTEMS
    logger.info("IDENTIFIER_SOURCES_MISSING calls=%d systems=%s%s: asked for identifier "
                "sources, answered with values but without them, so the identifier guard "
                "had nothing to check for those answers",
                sum(counts.values()), listed, f",+{more} more" if more > 0 else "")


_TURN_UNANSWERED: "contextvars.ContextVar[Unanswered | None]" = contextvars.ContextVar(
    "adt_identifier_sources_unanswered", default=None)


def begin_unanswered_record() -> Unanswered:
    """Start this turn's record of unanswered requests and return it. Call
    it before the turn's orchestrator is built (each guard takes the record
    current at its build) and log it when the turn ends: the cloud runtime
    entrypoint and the local chat turn both do."""
    record = Unanswered()
    _TURN_UNANSWERED.set(record)
    return record


def _current_unanswered() -> Unanswered:
    return _TURN_UNANSWERED.get() or Unanswered(turn=False)


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #
def _field_parts(text) -> tuple:
    """``(table leaf or "", column)`` of a stored field (``schema.table.col``,
    ``table.col`` or ``col``), lowercased."""
    parts = str(text or "").lower().split(".")
    return (parts[-2] if len(parts) >= 2 else "", parts[-1])


def _same_field(stored, table: str, column: str) -> bool:
    stored_table, stored_column = _field_parts(stored)
    return stored_column == column and (not stored_table or stored_table == table)


def _token_re(value: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(value) + r"(?![A-Za-z0-9_*%-])",
                      re.IGNORECASE)


def mentions(text: str, value: str) -> bool:
    """``value`` occurs in ``text`` as a whole token."""
    return bool(text) and _token_re(value).search(text) is not None


def carried(text: str, value: str) -> bool:
    """``value`` is sent in ``text`` as an identifier: a whole token, and not
    as a prefix or family search (a following wildcard or separator, or a
    prefix word just before it)."""
    for match in _token_re(value).finditer(text or ""):
        if not _FAMILY_WORDS.search(text[max(0, match.start() - _FAMILY_SPAN):match.start()]):
            return True
    return False


def call_text(arguments) -> str:
    """Every string argument of a delegation, joined."""
    if not isinstance(arguments, dict):
        return ""
    return "\n".join(v for k, v in arguments.items() if isinstance(v, str) and k != RESERVED_ARG)


def _target_of(tool_name, systems) -> str | None:
    from .concept_notes import _names_system
    found = [s for s in systems if _names_system(tool_name, s)]
    return found[0] if len(found) == 1 else None


def _systems_joined(facts: TurnFacts, a: str, b: str) -> bool:
    return any({jp.get("from_system"), jp.get("to_system")} == {a, b}
               for jp in facts.join_paths if isinstance(jp, dict))


def _field_linked(facts: TurnFacts, system: str, table: str, column: str, target: str) -> bool:
    """An IdentityRule or stored value mapping links this column with the target."""
    pairs = [(r.get("source_system"), r.get("source_field"), r.get("target_system"),
              r.get("target_field")) for r in facts.identity_rules if isinstance(r, dict)]
    pairs += [(m.get("from_system"), m.get("from_field"), m.get("to_system"), m.get("to_field"))
              for m in facts.value_mappings if isinstance(m, dict)]
    for a_sys, a_field, b_sys, b_field in pairs:
        if (a_sys == system and b_sys == target and _same_field(a_field, table, column)) or \
                (b_sys == system and a_sys == target and _same_field(b_field, table, column)):
            return True
    return False


@dataclass(frozen=True)
class JoinEnd:
    """One recorded join at a target: the other system's field, the
    target's field, and the concepts the graph says the join is about."""
    system: str
    field: str
    target_field: str
    concepts: frozenset


def _same_value(jp: dict) -> bool:
    """A join that holds the same values on both sides (an exact-value join
    or an identity transform): it links them whichever way it was recorded."""
    from .identity_affinity import EXACT_METHOD
    from .join_discovery import TRANSFORM_IDENTITY
    return jp.get("method") == EXACT_METHOD or jp.get("transform") == TRANSFORM_IDENTITY


def _join_ends(jp: dict) -> tuple:
    return ((jp.get("from_system"), jp.get("from_field")),
            (jp.get("to_system"), jp.get("to_field")))


def _ends_match(jp_ends, rule_ends) -> bool:
    return all(j_sys == r_sys and _same_field(r_field, *_field_parts(j_field))
               for (j_sys, j_field), (r_sys, r_field) in zip(jp_ends, rule_ends))


def _written_by_exact_pass(jp: dict) -> bool:
    """The join's method and transform are the ones the exact-agreement pass
    writes with its reasoning: the exact method, and the identity transform
    or none recorded. Any other pair means the reasoning text, whatever it
    reads like, is not that pass's evidence for this join."""
    from .identity_affinity import EXACT_METHOD, EXACT_TRANSFORM
    return jp.get("method") == EXACT_METHOD and jp.get("transform") in (None, "", EXACT_TRANSFORM)


def join_concepts(facts: TurnFacts, jp: dict) -> frozenset:
    """The concepts a recorded join is about: the concept of each
    IdentityRule with the same two fields (either direction) and, on a join
    the exact-agreement pass wrote (:func:`_written_by_exact_pass`), the
    concept its reasoning records
    (``identity_affinity.exact_concept_from_reasoning``). Empty when the
    graph gives none."""
    from .identity_affinity import exact_concept_from_reasoning
    ends = _join_ends(jp)
    out = {exact_concept_from_reasoning(jp.get("provenance")) if _written_by_exact_pass(jp)
           else None}
    for rule in facts.identity_rules:
        if not isinstance(rule, dict) or not rule.get("concept"):
            continue
        rule_ends = ((rule.get("source_system"), rule.get("source_field")),
                     (rule.get("target_system"), rule.get("target_field")))
        if _ends_match(ends, rule_ends) or _ends_match(ends, rule_ends[::-1]):
            out.add(str(rule["concept"]))
    return frozenset(c for c in out if c)


def joins_at(facts: TurnFacts, target: str) -> list:
    """Every recorded join between a field of ``target`` and a field of
    another system: those recorded into the target, and same-value joins
    recorded out of it (:func:`_same_value`), in the order the facts list
    them. A link recorded more than once (once from each side, say) is one
    join, about the concepts of all its records."""
    found: dict = {}
    for jp in facts.join_paths:
        if not isinstance(jp, dict):
            continue
        ends = _join_ends(jp)
        if ends[0][0] == target and _same_value(jp):
            ends = ends[::-1]
        (other, other_field), (end, end_field) = ends
        if end == target and other not in (None, "", target):
            key = (str(other), str(other_field or ""), str(end_field or ""))
            found[key] = found.get(key, frozenset()) | join_concepts(facts, jp)
    return [JoinEnd(*key, concepts) for key, concepts in found.items()]


def _join_line(join: JoinEnd, target: str) -> str:
    return f"{join.system} {join.field} -> {target} {join.target_field}"


@dataclass
class TurnGuard:
    """One turn's provenance record and refusal decisions (one per gateway client)."""
    facts: TurnFacts | None
    sources: dict = field(default_factory=dict)
    concepts: dict = field(default_factory=dict)
    returned: dict = field(default_factory=dict)
    refused: set = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    unanswered: Unanswered = field(default_factory=_current_unanswered)

    def observe(self, tool_name, result, asked: bool = False):
        """The result without the line; its records and text are kept.
        ``asked``: the call carried the request, so a successful answer from
        a system of the turn's facts that states values without the line is
        counted (:class:`Unanswered`)."""
        stripped, records, text = strip_result(result)
        target = _target_of(tool_name, self.facts.systems) if self.facts else None
        if target is None:
            return stripped
        if asked and _unanswered(result, records, text):
            self.unanswered.note(target)
        with self.lock:
            self.returned.setdefault(target, []).append(text)
            for rec in records or ():
                source = (target, rec["table"], rec["column"])
                self.concepts[source] = self.concepts.get(source, frozenset()) | frozenset(
                    rec.get("concepts") or ())
                for value in rec["values"]:
                    self.sources.setdefault(value.casefold(), {}).setdefault(source, value)
        return stripped

    def _returned_by(self, target: str, value: str) -> bool:
        key = value.casefold()
        if any(src[0] == target for src in self.sources.get(key, {})):
            return True
        return any(mentions(text, value) for text in self.returned.get(target, ()))

    def _joined_value_sent(self, joins, text: str) -> bool:
        for join in joins:
            table, column = _field_parts(join.field)
            for srcs in self.sources.values():
                for (system, src_table, src_column), value in srcs.items():
                    if (system == join.system and src_column == column
                            and (not table or table == src_table) and carried(text, value)):
                        return True
        return False

    def _candidate(self, facts, target, key, srcs, text):
        """``(value, source)`` when this recorded value meets every value
        condition of :meth:`refusal`, else None."""
        if len(srcs) != 1:
            return None
        (system, table, column), value = next(iter(srcs.items()))
        if (system == target or (target, key) in self.refused or system in facts.capped
                or not carried(text, value) or mentions(facts.question, value)
                or self._returned_by(target, value) or _systems_joined(facts, system, target)
                or _field_linked(facts, system, table, column, target)):
            return None
        return value, (system, table, column)

    def _found(self, facts, target, joins, text) -> list:
        """``[(candidate, joins of its concept)]`` for every candidate value
        whose source column names a concept a join at the target is about."""
        found = []
        for key, srcs in sorted(self.sources.items()):
            candidate = self._candidate(facts, target, key, srcs, text)
            mine = self.concepts.get(candidate[1], frozenset()) if candidate else frozenset()
            matching = [j for j in joins if j.concepts & mine]
            if matching:
                found.append((candidate, matching))
        return found

    def refusal(self, tool_name, arguments) -> str | None:
        """The refusal text for this delegation, or None to send it.

        Refused only when ALL hold: the turn's graph facts were read and are
        not capped for the target; the call carries no value recorded this
        turn from the other system's field of any recorded join at the
        target (:func:`joins_at`); and a value V the call carries is not in
        the user's question, was recorded this turn from exactly ONE source
        column C of a system the graph joins to the target by no JoinPath,
        has no IdentityRule or stored value mapping from C to the target, the
        target has not returned V this turn, and a join at the target is
        about a concept C names (:func:`join_concepts`; no concept evidence on
        either side passes). At most once per (target, V); the refusal names
        only the joins of V's concept."""
        facts = self.facts
        target = _target_of(tool_name, facts.systems) if facts else None
        if target is None or target in facts.capped:
            return None
        joins = joins_at(facts, target)
        text = call_text(arguments)
        with self.lock:
            if not joins or self._joined_value_sent(joins, text):
                return None
            found = self._found(facts, target, joins, text)
            for (value, _source), _joins in found:
                self.refused.add((target, value.casefold()))
        return refusal_text(found[0][0], target, found[0][1]) if found else None


#: Most joins one refusal lists.
MAX_LISTED_JOINS = 3


def refusal_text(candidate, target: str, joins) -> str:
    """The refusal: where the value came from, the recorded joins of its
    concept at the target (``joins``, one per link, :func:`joins_at`), and
    what to read instead. Never a value it was not sent."""
    value, (system, table, column) = candidate
    shown = joins[:MAX_LISTED_JOINS]
    listed = "; ".join(_join_line(join, target) for join in shown)
    if len(joins) == 1:
        recorded = f"The recorded join for that kind of record is {listed}."
        read = (f"Read {joins[0].field} from {joins[0].system} for that record and pass "
                f"{target} the value that join gives.")
    else:
        verb = "are" if len(shown) == len(joins) else "include"
        recorded = f"The recorded joins for that kind of record {verb} {listed}."
        read = ("Use one of those joins: read its field from its system for that record and "
                f"pass {target} the value that join gives.")
    return (f"[engine] Not sent to {target}: {value} was returned this turn by {system} "
            f"{table}.{column}, and the knowledge graph records no join between {system} and "
            f"{target}, so {target} is not known to hold that value. {recorded} {read} "
            "Sending this same request again goes through unchanged.")
