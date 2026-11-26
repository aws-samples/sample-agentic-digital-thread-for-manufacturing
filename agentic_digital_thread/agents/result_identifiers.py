# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The identifiers a system's own tool results returned in a call, as one engine-only line.

Measured on a test event (2026-11-11): a holder declined ``not_connected``
about a concept after its shape guard refused a wrong-form lookup, the model
fetched the other identifier form from the system that pairs the two and
asked the holder again with it, and the decline guard refused the re-ask as a
repeat. The decline guard may reopen such a decline for a value a pairing
system returned (``delegation_decline.brings_paired_form``), but the pairing
system's answer text cannot show that a value was returned: the orchestrator
model can put an invented value of the right shape in a question to the
pairing system, and a truthful negative answer echoes it ("no record exists
for ..."). The system runtime knows which values its own tool results
returned in the call, and says so in one fixed line:

    [engine] Result identifiers v1: ["<value>", ...]

System side:

* :func:`record_result` keeps, per call in its invocation state
  (:data:`STATE_KEY`, fed by the capture hook), the identifier tokens of the
  row cells of a successful tabular SQL result (its ``columns``, its
  ``rows`` and the ``_adt_executed_query`` the SQL tool stamps), and only of
  the result columns whose source the executed SELECT list proves is a
  column of one of the system's tables (``identifier_provenance.
  _projected_sources``, ``select_lineage``): a literal, an expression, a
  derived table, a WITH or a set operation proves none. Nothing else is
  data here: a refusal the query tool returns instead of running a query
  (the shape guard's names the model's own literal in its check, note and
  miss block), a note, an error, the executed query's text and every other
  result shape add nothing, until a shape has its own data fields listed.
  It needs no schema example value, so a runtime rebuilt from the graph's
  field names and types records as one built at onboarding does. Bounded
  (:data:`MAX_RECORDED`); past it the call is cut and lists nothing.
* :func:`returned` reads that record: the case-folded tokens a result
  returned that hold a letter and a digit, 3 to 64 characters, and that the
  text the line follows states as a whole token, sorted. A missing record, a
  cut one or more than :data:`MAX_VALUES` such tokens give none. It is not
  the unreturned-identifier check's record (``answer_ids``), which reads any
  result's text to show what a result did NOT return, and so stays as it
  is. The runtime does not know the turn's identifier forms (they are
  the orchestrator's route-seed facts), so the orchestrator applies the form
  shapes; a form whose values hold no letter is never listed, so it never
  reopens a decline. Only values the answer already states are listed: the
  runtime cannot tell the orchestrator from any other caller, so the line
  gives a caller nothing the answer has not.
* :func:`with_line` makes every copy of the line's opening in the text inert
  (the model's answer, a data cell, another engine line quoting a literal, a
  relayed chart tag) and appends the line when it lists a value. The runtime
  calls it only when the caller asked for the identifier-sources line (the
  same reserved argument, ``runtime_asgi.identifier_sources_var``), never
  under enforce, right before that line, which stays last
  (``system_agent._with_identifier_sources``). :func:`neutralize` makes the
  copies in the model's answer inert with the other engine lines' copies.

Orchestrator side:

* :func:`split` reads the line only as the result's final line and removes
  it from every text block and string member of ``structuredContent``; a
  malformed final line is removed and reads as none. The delegation wrapper
  calls it right outside the identifier-provenance guard, which has already
  removed the identifier-sources line. Whenever the caller asked, the
  runtime made every other copy inert first, so a line read there is the
  engine's; :class:`TurnReturned` reads none from a turn that did not ask.
* :class:`TurnReturned` keeps, per delegation tool, the values of a
  successful result of a delegation that asked for the line, less every
  value the delegation's own arguments (question and context) hold: a tool
  result can echo the request back as data. The line is for the orchestrator
  only (``internal_lines.MARKERS``); the wrapper removes it before anything
  else reads the result.

Generic: tokens, their own characters and the SELECT list's proven columns
only; no dataset literal. Stdlib only apart from the lineage and the
argument readers.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field

#: Opening words of the line.
MARKER = "[engine] Result identifiers v1:"
#: What a copy of the opening is rewritten to.
_INERT = "Result identifiers v1:"
#: Most values one line lists; a call with more lists none.
MAX_VALUES = 32
#: Most values one turn keeps; past it, later lines add nothing.
MAX_TURN_VALUES = 256
#: Invocation-state key of the identifiers this call's tabular results returned.
STATE_KEY = "_adt_result_identifiers"
#: Tokens one call's record keeps; past it the call is cut and lists nothing.
MAX_RECORDED = 50_000
#: Characters of one result text read whole; a longer text adds nothing.
MAX_RESULT_TEXT = 2_000_000
#: Characters of the text the line follows that are read for stated tokens.
_SCAN_CHARS = 64_000
_VALUE_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_STATED_RES = (re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+"),
               re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*"))
_LINE_RE = re.compile(r"\n\n" + re.escape(MARKER) + r" (?P<body>[^\n]*)\s*\Z")


def _identifier(value: str) -> bool:
    """A folded token the line may list: :data:`_VALUE_RE`, 3 to 64
    characters, with a letter and a digit."""
    return (isinstance(value, str) and 3 <= len(value) <= 64 and bool(_VALUE_RE.fullmatch(value))
            and any(c.isdigit() for c in value) and any(c.isalpha() for c in value))


def _stated(text: str) -> set:
    """The case-folded whole tokens of ``text`` (its first :data:`_SCAN_CHARS`
    characters), read the two ways a result's text is read for identifiers
    (``answer_ids``)."""
    scan = text[:_SCAN_CHARS]
    return {match.group(0).casefold() for regex in _STATED_RES for match in regex.finditer(scan)}


def _payloads(content) -> list:
    """The JSON objects of a result's content items (a text item that parses
    whole, or a ``json`` item); a text longer than :data:`MAX_RESULT_TEXT` is
    not read."""
    out = []
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, dict):
            continue
        data = item.get("json")
        text = item.get("text")
        if data is None and isinstance(text, str) and len(text) <= MAX_RESULT_TEXT:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        if isinstance(data, dict):
            out.append(data)
    return out


def _row_cells(payload: dict, physical_schema) -> list:
    """The string cells of a tabular SQL result's proven columns (see the
    module docstring), ``[]`` for any other shape or a row that does not
    line up with the columns."""
    from .identifier_provenance import _projected_sources
    names, rows = payload.get("columns"), payload.get("rows")
    sql = payload.get("_adt_executed_query")
    if not (isinstance(names, list) and names and all(isinstance(n, str) for n in names)
            and isinstance(rows, list) and isinstance(sql, str) and sql.strip()):
        return []
    proven = [index for index, _source in _projected_sources(sql, names, physical_schema) or ()]
    if not proven or not all(isinstance(r, (list, tuple)) and len(r) == len(names) for r in rows):
        return []
    # A cell longer than the scan is skipped: a token cut at the scan's end is not a value.
    return [row[i] for row in rows for i in proven
            if isinstance(row[i], str) and len(row[i]) <= _SCAN_CHARS]


def record_result(invocation_state, result, physical_schema) -> None:
    """Keep the identifier tokens of one tool result's proven row cells
    (:func:`_row_cells`) in the call's record (:data:`STATE_KEY`); an error
    result, and every result of another shape, adds nothing."""
    if not isinstance(invocation_state, dict) or not isinstance(result, dict):
        return
    if str(result.get("status") or "").lower() == "error":
        return
    record = invocation_state.setdefault(STATE_KEY, {"values": set(), "cut": False})
    if record["cut"]:
        return
    values = record["values"]
    for payload in _payloads(result.get("content")):
        for cell in _row_cells(payload, physical_schema):
            values.update(token for token in _stated(cell) if _identifier(token))
            if len(values) > MAX_RECORDED:
                record["cut"], record["values"] = True, set()
                return


def returned(invocation_state, text: str) -> list:
    """The identifiers this call's tabular results returned (:func:`record_result`)
    that ``text`` states, sorted; ``[]`` for no record, a cut one, or more
    than :data:`MAX_VALUES`."""
    record = invocation_state.get(STATE_KEY) if isinstance(invocation_state, dict) else None
    if not isinstance(record, dict) or record.get("cut") or not isinstance(text, str):
        return []
    found = set(record.get("values") or ()) & _stated(text)
    kept = sorted(value for value in found if _identifier(value))
    return kept if len(kept) <= MAX_VALUES else []


def line(values) -> str:
    """The line for ``values`` (folded tokens)."""
    return f"{MARKER} {json.dumps(list(values), separators=(',', ':'), ensure_ascii=True)}"


def neutralize(text: str) -> str:
    """``text`` with every copy of the line's opening made inert."""
    if not isinstance(text, str) or MARKER not in text:
        return text
    return text.replace(MARKER, _INERT)


def with_line(text: str, invocation_state) -> str:
    """``text`` with every copy of the opening made inert, then the line for
    the identifiers this call's results returned that ``text`` states, when
    there is one (:func:`returned`)."""
    if not isinstance(text, str):
        return text
    body = neutralize(text)
    values = returned(invocation_state, body)
    return f"{body}\n\n{line(values)}" if values else body


def _parse(body: str):
    """The values of a line body, or None unless it is one the engine writes."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if (not isinstance(data, list) or not data or len(data) > MAX_VALUES
            or not all(_identifier(value) for value in data)):
        return None
    return frozenset(data)


def split_text(text):
    """``(text without its final line, values or None)``; the line is read
    only as the final line, and a malformed one is still removed."""
    if not isinstance(text, str) or MARKER not in text:
        return text, None
    match = _LINE_RE.search(text)
    if not match:
        return text, None
    return text[:match.start()], _parse(match.group("body").strip())


def split(result) -> tuple:
    """``(result without the line, values or None)``. The line is removed
    from every text block and string member of ``structuredContent``; the
    values are the last text block's. The caller's result is never mutated,
    and is returned itself when it carries no line."""
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return result, None
    values, blocks, changed = None, [], False
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text, found = split_text(block["text"])
            values = found if text is not block["text"] else values
            if text is not block["text"]:
                block, changed = {**block, "text": text}, True
        blocks.append(block)
    structured = result.get("structuredContent")
    cleaned = structured
    if isinstance(structured, dict):
        cleaned = {k: split_text(v)[0] if isinstance(v, str) else v for k, v in structured.items()}
        changed = changed or any(cleaned[k] is not structured[k] for k in structured)
    if not changed:
        return result, values
    out = {**result, "content": blocks}
    if isinstance(structured, dict):
        out["structuredContent"] = cleaned
    return out, values


def _carried(value: str, sent: str) -> bool:
    """Whether the folded delegation text ``sent`` holds ``value`` between
    non-alphanumeric characters or ends."""
    return re.search(r"(?<![a-z0-9])" + re.escape(value) + r"(?![a-z0-9])", sent) is not None


@dataclass
class TurnReturned:
    """One turn's record (one per gateway client): per delegation tool, the
    identifiers its own tool results returned that the delegation did not
    carry. ``asked``: the turn's delegations ask for the line
    (:func:`for_turn`)."""
    asked: bool = False
    by_tool: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, tool_name, arguments, result):
        """``result`` without the line; its values kept when the turn asks for
        the line and the result is not an error, less every value the
        delegation's arguments hold (``identifier_provenance.call_text``)."""
        from .identifier_provenance import call_text
        stripped, values = split(result)
        if not (values and self.asked and isinstance(result, dict)
                and result.get("status") != "error"):
            return stripped
        sent = call_text(arguments).casefold()
        kept = {value for value in values if not _carried(value, sent)}
        with self.lock:
            room = MAX_TURN_VALUES - sum(len(v) for v in self.by_tool.values())
            if kept and room >= len(kept):
                self.by_tool.setdefault(str(tool_name), set()).update(kept)
        return stripped

    def values_of(self, system: str) -> frozenset:
        """The values kept for the delegation tools of ``system``
        (``route_facts.target_of``)."""
        from .route_facts import target_of
        with self.lock:
            items = [(tool, frozenset(values)) for tool, values in self.by_tool.items()]
        return frozenset(value for tool, values in items
                         if target_of(tool, (system,)) == system for value in values)


def for_turn(forms_facts) -> TurnReturned:
    """The turn's record. Its delegations ask for the line exactly when the
    identifier-provenance guard asks for its own (the turn has route-seed
    facts, ``identifier_provenance.turn_facts``), and never under enforce,
    where no system writes either line."""
    from .config import response_integrity_enforced
    return TurnReturned(asked=forms_facts is not None and not response_integrity_enforced())
