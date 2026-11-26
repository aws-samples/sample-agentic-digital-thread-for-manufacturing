# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A record identifier the answer gives in the form of the schema's examples
that no result of the call returned.

A system agent's call returned rows with a record's row id and its name but
not its business key. Its answer stated a business key for the record anyway:
a code built in the form of the example values its baked schema shows for
that key column (same leading segment, same separators), which no tool
result held. The orchestrator relayed it as the record's identifier. The
prompt already says an identifier comes from the call's own results, never
from the schema's example values (RECORD IDENTIFIERS); this checks it the
same way :mod:`answer_names` checks names. The answer is never changed: the
identifiers no result returned reach the orchestrator as one fixed engine
line (:func:`engine_line`).

* :func:`schema_id_leads` reads the leading segments of the identifier-shaped
  example values of the baked schema's text columns. An identifier-shaped
  value is one token of letters and digits joined by ``-`` or ``_``, with a
  digit or at least three segments; its lead is its first segment when that
  is two or more letters (``ABC-2001-X`` gives ``abc``). Cut or redacted
  examples are skipped.
* :func:`record_result` reads each of THIS call's tool results as it arrives
  (the capture hook calls it) and keeps, in the call's own invocation state,
  the identifier tokens of its data strings that hold a lead as a segment.
  The data strings are the ones :mod:`answer_names` reads (rows of a tabular
  result, keys and values of other JSON except ``_adt`` keys, plain text;
  nothing from an error result). The record is bounded
  (:data:`MAX_RESULT_TOKENS`); past it, or after a result too large to read
  whole, the call is marked cut and no identifier is ever reported for it.
* :func:`unreturned_ids` lists the tokens of the answer that start with a
  lead and a separator, hold a digit, and that neither a recorded token nor
  the delegated question holds between separators or ends (case folded). A
  token cut short with an ellipsis is skipped.
* The system's identifier forms (``identifier_forms``: the active
  IdentityRules and trusted value mappings with this system at either end,
  read from the graph by the system runtime itself, kept in invocation state
  under :data:`FORMS_STATE_KEY`) account for a token too: an IdentityRule's
  output (``identifier_translation.apply`` read backwards), or the other end
  of a trusted value mapping, whose input this call's results returned as a
  whole token or the question holds. Only those graph facts count; nothing
  the caller sends, and no transform read from free text. With forms
  present, :func:`record_result` also keeps every whole token of the data
  strings, under the same bound.

The contract is conservative, as for names: every ambiguity resolves toward
no line. Accepted misses: an identifier whose leading segment no schema
example has; one a longer returned token contains between separators; any
call with a cut result.

Stdlib only, no dataset knowledge.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .answer_names import _FRAGMENT, _TEXT_TYPE, _result_strings

#: Attribute the system agent factory sets on the agent: the frozenset of
#: case-folded leads of its baked schema's identifier-shaped example values.
SCHEMA_LEADS_ATTR = "_adt_schema_id_leads"
#: Attribute the system agent factory sets on the agent: its own system id.
SYSTEM_ID_ATTR = "_adt_system_id"
#: Invocation-state key for the identifiers the answer gives that no result returned.
STATE_KEY = "_adt_unreturned_ids"
#: Invocation-state key of the identifier tokens this call's results returned.
RESULT_IDS_STATE_KEY = "_adt_answer_id_results"
#: Opening words of the engine line.
MARKER = "[engine] Identifiers in no result:"
#: Most identifiers one line quotes.
MAX_IDS = 5
#: Result tokens kept per call; past it the call counts as cut.
MAX_RESULT_TOKENS = 50_000
#: Invocation-state key of the system's identifier forms
#: (``identifier_forms.IdentifierForms``), set by the runtime before the agent runs.
FORMS_STATE_KEY = "_adt_identifier_forms"
#: Answer identifiers checked per call; the rest are not checked.
MAX_CANDIDATES = 200
_SEPARATORS = "-_"
_ID_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+")
_VALUE_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
_ANSWER_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+")
_SEGMENTS = re.compile(r"[-_]")
_ELLIPSIS = ("\u2026", "...")


def _lead(value: str) -> str:
    """The case-folded lead of an identifier-shaped value, ``""`` otherwise."""
    if not value or value.endswith(_ELLIPSIS) or "[REDACTED" in value.upper():
        return ""
    if not _ID_TOKEN.fullmatch(value):
        return ""
    segments = _SEGMENTS.split(value)
    if not (any(ch.isdigit() for ch in value) or len(segments) >= 3):
        return ""
    first = segments[0]
    return first.casefold() if len(first) >= 2 and first.isalpha() else ""


def schema_id_leads(entries: Iterable) -> frozenset:
    """Leads of the identifier-shaped example values of the text columns in
    ``entries`` (``(table, column_fragments, ...)`` as
    ``system_agent._desc_entries`` yields them)."""
    leads: set[str] = set()
    for entry in entries or ():
        try:
            fragments = entry[1]
        except (TypeError, IndexError):
            continue
        for fragment in fragments or ():
            match = _FRAGMENT.match(str(fragment))
            if not match or not _TEXT_TYPE.match(match.group(2).strip()):
                continue
            for value in match.group(3).split(", "):
                lead = _lead(value.strip())
                if lead:
                    leads.add(lead)
    return frozenset(leads)


def _record(invocation_state: dict) -> dict:
    record = invocation_state.get(RESULT_IDS_STATE_KEY)
    if not isinstance(record, dict):
        record = {"tokens": set(), "cut": False, "values": set(), "values_cut": False}
        invocation_state[RESULT_IDS_STATE_KEY] = record
    return record


def _forms(invocation_state: Any):
    """The system's identifier forms (:data:`FORMS_STATE_KEY`, an
    ``identifier_forms.IdentifierForms``), or ``None``."""
    forms = invocation_state.get(FORMS_STATE_KEY) if isinstance(invocation_state, dict) else None
    ok = getattr(forms, "rules", None) is not None and getattr(forms, "pairs", None) is not None
    return forms if ok else None


def _record_values(record: dict, strings: list, whole: bool) -> None:
    """Keep every whole token of ``strings`` (case folded): what a rule's or
    a mapping's input is checked against. Past :data:`MAX_RESULT_TOKENS`, or
    after a result not read whole, the values are marked cut."""
    if record["values_cut"]:
        return
    values = record["values"]
    for text in strings:
        values.update(match.group(0).casefold() for match in _VALUE_TOKEN.finditer(text))
        if len(values) > MAX_RESULT_TOKENS:
            break
    if not whole or len(values) > MAX_RESULT_TOKENS:
        record["values_cut"] = True
        record["values"] = set()


def record_result(invocation_state: Any, result: Any, leads: Iterable[str] | None) -> None:
    """Keep the identifier tokens with a lead segment that one of this call's
    tool results returns as data, and, when the call has identifier rules
    (:func:`_forms`), every token of its data strings."""
    if not isinstance(invocation_state, dict) or not isinstance(result, dict) or not leads:
        return
    if str(result.get("status") or "").lower() == "error":
        return
    record = _record(invocation_state)
    if record["cut"]:
        return
    lead_set = set(leads)
    forms = _forms(invocation_state)
    with_rules = forms is not None and bool(forms.rules or forms.pairs)
    strings, _raw, whole = _result_strings(result.get("content"), numbers=with_rules)
    if with_rules:
        _record_values(record, strings, whole)
    tokens = record["tokens"]
    for text in strings:
        for match in _ID_TOKEN.finditer(text):
            token = match.group(0).casefold()
            if lead_set.intersection(_SEGMENTS.split(token)):
                tokens.add(token)
        if len(tokens) > MAX_RESULT_TOKENS:
            break
    if not whole or len(tokens) > MAX_RESULT_TOKENS:
        record["cut"] = True
        record["tokens"] = set()


def _held(token: str, text: str) -> bool:
    """Whether folded ``text`` holds folded ``token`` between separators or ends."""
    return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", text) is not None


def _input_held(given: str, record: dict, asked: str) -> bool:
    """Whether ``given`` (folded) is a value this call's results returned as
    a whole token, or the question holds. With the values cut it counts as
    held: that cannot be checked."""
    return bool(record.get("values_cut") or given in (record.get("values") or set())
                or _held(given, asked))


def _rule_input_held(token: str, rules: tuple, record: dict, asked: str) -> bool:
    """Whether a rule direction produces ``token`` from a held value
    (:func:`_input_held`; ``identifier_translation.apply`` read backwards:
    the token is the rule's output)."""
    from .identifier_translation import apply
    for source, target in rules:
        given = apply(target, source, token)
        if given and _input_held(given.casefold(), record, asked):
            return True
    return False


def _permitted(token: str, forms, record: dict, asked: str) -> bool:
    """Whether the system's identifier forms account for ``token``: the other
    end of a trusted value mapping whose end is held, or a rule's output from
    a held value (:func:`_input_held`)."""
    if forms is None:
        return False
    folded = token.casefold()
    if any(folded == end and _input_held(start, record, asked) for start, end in forms.pairs):
        return True
    return bool(forms.rules) and _rule_input_held(token, forms.rules, record, asked)


def _candidates(answer: str, leads: frozenset) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for match in _ANSWER_TOKEN.finditer(answer):
        token = match.group(0)
        if answer.startswith(_ELLIPSIS, match.end()):
            continue
        if not any(ch.isdigit() for ch in token):
            continue
        if _SEGMENTS.split(token)[0].casefold() in leads:
            found.append((match.start(), token))
    return found


def unreturned_ids(answer: str, leads: Iterable[str], invocation_state: Any,
                   question: str = "") -> list[str]:
    """The identifiers ``answer`` writes in a lead's form that no result of
    this call returned (:func:`record_result`) and ``question`` does not
    hold, in the answer's spelling and order, at most :data:`MAX_IDS`."""
    lead_set = frozenset(leads or ())
    if not isinstance(answer, str) or not answer or not lead_set:
        return []
    record = invocation_state.get(RESULT_IDS_STATE_KEY) if isinstance(
        invocation_state, dict) else None
    record = record if isinstance(record, dict) else {"tokens": set(), "cut": False}
    if record.get("cut"):
        return []
    returned = "\x00".join(sorted(record.get("tokens") or ()))
    asked = (question or "").casefold()
    forms = _forms(invocation_state)
    hits: dict[str, str] = {}
    for _pos, token in _candidates(answer, lead_set)[:MAX_CANDIDATES]:
        folded = token.casefold()
        if folded in hits or _held(folded, returned) or _held(folded, asked) \
                or _permitted(token, forms, record, asked):
            continue
        hits[folded] = token
    return list(hits.values())[:MAX_IDS]


def _quoted(ids: list[str]) -> str:
    return ", ".join(f'"{value}"' for value in ids)


def engine_line(ids: list[str]) -> str:
    """The fixed line for the orchestrator about ``ids``."""
    one = len(ids) == 1
    return (
        f"{MARKER} the answer gives {_quoted(ids)}, which no result in this call "
        f"returned ({'it has' if one else 'they have'} the form of the system's schema "
        "example values, not data). Do not state "
        f"{'it' if one else 'them'} as data: give the record's id or key a result "
        "returned instead, or say the identifier was not returned."
    )
