# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which identity rule a delegation's identifiers came along.

A delegation often asks one system about values another system returned
earlier in the same turn: the run numbers a shipment list gave as its batch
ids, say. The route seed lists the IdentityRules between the systems (a
concept, and the field on each side that holds its identifier). This module
reads which of those rules a delegation's question carried values along
(:func:`carried`), so the ROUTE block can name that rule's concept and join:

* a carried value is an identifier token of the question (at most
  :data:`MAX_CARRIED`) that a successful call to another system returned
  earlier this turn (:class:`PeerResult`, bounded at record time);
* provenance first: where that result labels the value (a table column, a
  ``label: value`` line, a JSON key, or, in a validated ``SystemAnswer``,
  the evidence entry's physical field, its semantic field only when no
  physical field was captured), the label must name the rule's field on that
  system's side;
* only a value the result does not label is matched by the rule's pattern on
  that side (``identifier_translation.pattern_regex``), and a rule with no
  pattern matches no unlabelled value;
* when several rules fit the same value, the ones whose field on the
  target's side the question names are kept; when the question names none of
  them and they bind different field pairs, the match is not specific: their
  shared concept still counts, no join does (:attr:`Carried.specific`).

A question that carries no identifier (a pronoun-only follow-up) carries no
rule. Pure; bounded by the caps below.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: Identifier tokens read from one delegated question.
MAX_CARRIED = 16
#: What one recorded result is read for, and how much of it.
RESULT_CHAR_CAP = 32_000
MAX_CELLS = 2000
MAX_TOKENS_PER_RESULT = 4000
_CELL_CAP = 512
_QUESTION_CAP = 4000
_JSON_DEPTH = 6
#: The prefix of the members the engine adds to a JSON answer (a result-cut
#: note, say); they are not part of a ``SystemAnswer``.
_ENGINE_MEMBER_PREFIX = "_adt_"

#: An identifier-shaped token: letters, digits and inner separators, holding
#: at least one digit (checked by :func:`_identifiers`).
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z0-9](?:[A-Za-z0-9_./:-]*[A-Za-z0-9])?(?![A-Za-z0-9_])")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_KV_RE = re.compile(
    r"^\s*(?:[-+]\s+|\d+[.)]\s+)?([A-Za-z][A-Za-z0-9 _./#()-]{0,60}?)\s*:\s+(\S.*)$")
_MARKUP_RE = re.compile(r"[*`]+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def _fold(text: object) -> str:
    """Lowercase words of ``text``: markup gone, any other separator a space."""
    return _NON_WORD_RE.sub(" ", _MARKUP_RE.sub("", str(text or "")).lower()).strip()


def _identifiers(text: str, cap: int) -> list:
    """The distinct identifier tokens of ``text`` (with a digit, at least three
    characters), in order, at most ``cap``."""
    out: dict = {}
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if len(token) >= 3 and any(ch.isdigit() for ch in token):
            out.setdefault(token, None)
            if len(out) >= cap:
                break
    return list(out)


def question_identifiers(question) -> list:
    """The identifier tokens :func:`carried` reads from a delegated
    ``question``, in order, bounded the same way."""
    return _identifiers(str(question or "")[:_QUESTION_CAP], MAX_CARRIED)


def question_scan_overflowed(question) -> bool:
    """Whether ``question`` holds more identifiers than
    :func:`question_identifiers` reads, or more text than it scans: then the
    tokens it returns are not every identifier the question carried."""
    text = str(question or "")
    return len(text) > _QUESTION_CAP or len(_identifiers(text, MAX_CARRIED + 1)) > MAX_CARRIED


@dataclass(frozen=True)
class IdentityRule:
    """One seed IdentityRule: a concept and the field on each side."""

    id: str
    concept: str
    systems: tuple
    fields: tuple
    patterns: tuple

    def links(self, target: str, peer: str) -> bool:
        return target != peer and set(self.systems) == {target, peer}

    def side(self, system_id: str) -> int:
        return self.systems.index(system_id)

    def field_of(self, system_id: str) -> str:
        return self.fields[self.side(system_id)]

    def pattern_of(self, system_id: str) -> str:
        return self.patterns[self.side(system_id)]


def identity_rule(item, clean) -> "IdentityRule | None":
    """A seed ``identity_rules`` entry as an :class:`IdentityRule`, or ``None``
    when it lacks a concept, either system or either field. ``clean`` bounds
    each value."""
    if not isinstance(item, dict):
        return None
    values = [clean(item.get(key)) for key in
              ("concept", "source_system", "source_field", "target_system", "target_field")]
    if not all(values) or values[1] == values[3]:
        return None
    concept, s_sys, s_field, t_sys, t_field = values
    patterns = (str(item.get("source_pattern") or ""), str(item.get("target_pattern") or ""))
    return IdentityRule(clean(item.get("id")), concept, (s_sys, t_sys), (s_field, t_field),
                        patterns)


@dataclass(frozen=True)
class PeerResult:
    """What one successful delegation returned, as this module reads it: the
    identifier tokens of its text, and for each the labels it appeared under."""

    system: str
    tokens: frozenset
    labels: dict


def _row(line: str) -> list:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_cells(lines: list) -> list:
    """``(label, cell text)`` for each data cell of the Markdown tables in ``lines``."""
    out, header, skip = [], None, -1
    for i, line in enumerate(lines):
        if i == skip:
            continue
        piped = line.lstrip().startswith("|")
        if header is not None and piped:
            out.extend(zip(header, _row(line)))
            continue
        header = None
        if piped and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            header, skip = [_fold(cell) for cell in _row(line)], i + 1
    return out


def _line_cells(lines: list) -> list:
    """``(label, value text)`` for each ``label: value`` line (list markers
    and bold markup allowed)."""
    out = []
    for line in lines:
        if line.lstrip().startswith("|"):
            continue
        match = _KV_RE.match(_MARKUP_RE.sub("", line))
        if match:
            out.append((_fold(match.group(1)), match.group(2)))
    return out


def _json_cells(value, key: str = "", depth: int = 0, out=None) -> list:
    """``(key, scalar text)`` for each scalar a JSON value holds under a key."""
    out = [] if out is None else out
    if depth > _JSON_DEPTH or len(out) >= MAX_CELLS:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            _json_cells(v, _fold(k), depth + 1, out)
    elif isinstance(value, list):
        for v in value:
            _json_cells(v, key, depth + 1, out)
    elif key and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        out.append((key, str(value)))
    return out


def _input_field_names(item, fields: dict) -> tuple:
    """The field names a claim input's value is labelled by: its evidence
    entry's ``physical_field`` when the server captured one (the source
    column), else that entry's and the input's ``semantic_field`` (an output
    alias the model may have chosen, so never used beside a physical field)."""
    semantic, physical = fields.get(item.evidence_id, ("", ""))
    if physical:
        return (physical,)
    return tuple(name for name in (semantic, item.semantic_field) if name)


def _envelope_cells(value) -> "list | None":
    """``(label, value text)`` for each claim input of a validated
    ``SystemAnswer`` envelope (the answer of a system agent under the
    response-integrity gate), labelled by :func:`_input_field_names` in the
    forms a label names a field by. The engine's reserved members
    (``_adt_*``) are set aside first. ``None`` when ``value`` does not
    validate as one, so the generic key reading applies."""
    if not isinstance(value, dict) or "claims" not in value or "evidence" not in value:
        return None
    from .response_integrity_contracts import SystemAnswer
    try:
        answer = SystemAnswer.model_validate(
            {k: v for k, v in value.items() if not str(k).startswith(_ENGINE_MEMBER_PREFIX)})
    except ValueError:
        return None
    fields = {e.evidence_id: (e.semantic_field, e.physical_field) for e in answer.evidence}
    cells: list = []
    for item in (i for claim in answer.claims for i in claim.inputs):
        labels = set().union(*(_field_forms(name) for name in _input_field_names(item, fields)))
        cells.extend((label, str(item.value)) for label in sorted(labels))
    return cells[:MAX_CELLS]


def _leading_json(text: str) -> tuple:
    """``(the JSON value text starts with, the text after it)``, or
    ``(None, text)`` when it starts with none."""
    stripped = text.lstrip()
    if stripped[:1] not in ("{", "["):
        return None, text
    try:
        value, end = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return None, text
    return value, stripped[end:]


def _text_cells(text: str) -> list:
    lines = text.splitlines()
    return _table_cells(lines) + _line_cells(lines)


def _cells(text: str) -> list:
    """The labelled cells of a result. A validated ``SystemAnswer`` is read by
    its contract alone (what follows it, such as a relayed chart tag, is not
    validated data). Any other leading JSON is read by its keys, and the text
    after it as tables and ``label: value`` lines."""
    parsed, rest = _leading_json(text)
    if parsed is None:
        return _text_cells(text)
    envelope = _envelope_cells(parsed)
    if envelope is not None:
        return envelope
    return _json_cells(parsed) + _text_cells(rest)


def peer_result(system: str, text: str) -> PeerResult:
    """``text`` (a delegation's result) read for :func:`carried`, bounded."""
    text = text[:RESULT_CHAR_CAP] if isinstance(text, str) else ""
    labels: dict = {}
    for label, cell in _cells(text)[:MAX_CELLS]:
        if not label:
            continue
        for token in _identifiers(cell[:_CELL_CAP], MAX_CARRIED):
            labels.setdefault(token, set()).add(label)
    tokens = frozenset(_identifiers(text, MAX_TOKENS_PER_RESULT))
    return PeerResult(system, tokens, {t: frozenset(v) for t, v in labels.items()})


def _field_forms(field: str) -> set:
    """The folded forms a label names ``field`` by: its column, its
    ``table.column``, the whole path."""
    parts = [p for p in str(field or "").replace('"', "").split(".") if p]
    return {_fold(".".join(parts[i:])) for i in range(max(0, len(parts) - 2), len(parts))} \
        | {_fold(field)} if parts else set()


def _pattern_fits(pattern: str, token: str) -> bool:
    from .identifier_translation import pattern_regex
    regex = pattern_regex(pattern) if pattern else None
    match = regex.search(token) if regex is not None else None
    return match is not None and match.group(0) == token


def _rules_for(token: str, result: PeerResult, linking: list) -> list:
    """The rules among ``linking`` that ``token`` in ``result`` came along:
    by the label it appeared under, or, unlabelled, by the rule's pattern."""
    if token not in result.tokens:
        return []
    labels = result.labels.get(token)
    if labels:
        return [r for r in linking if labels & _field_forms(r.field_of(result.system))]
    return [r for r in linking if _pattern_fits(r.pattern_of(result.system), token)]


def names_field(question: str, field: str) -> bool:
    """Does ``question`` name ``field`` (its column, words in any of the
    ``_``/``-``/space spellings, plural allowed)?"""
    column = str(field or "").replace('"', "").split(".")[-1]
    words = [re.escape(w) for w in _fold(column).split()]
    if not words:
        return False
    pattern = r"(?<![a-z0-9])" + r"[\s_-]+".join(words) + r"(?:s|es)?(?![a-z0-9])"
    return re.search(pattern, str(question or "").lower()) is not None


@dataclass(frozen=True)
class Carried:
    """The rules a delegation's values came along, and whether they are
    specific enough to name a join."""

    rules: tuple = ()
    specific: bool = False

    def concepts(self) -> tuple:
        """The rules' concepts when specific, else the concepts every rule has."""
        ordered = tuple(dict.fromkeys(r.concept for r in self.rules))
        if self.specific:
            return ordered
        return tuple(c for c in ordered if all(r.concept == c for r in self.rules))


def carried(rules, results, target: str, question) -> Carried:
    """The identity rules between ``target`` and another system that
    ``question`` carried values along, from that system's ``results``
    (earlier successful calls of this turn, oldest first)."""
    tokens = question_identifiers(question)
    found: list = []
    for result in results if tokens else ():
        linking = [r for r in rules if r.links(target, result.system)]
        for token in tokens if linking else ():
            found.extend(r for r in _rules_for(token, result, linking) if r not in found)
    if not found:
        return Carried()
    named = [r for r in found if names_field(question, r.field_of(target))]
    chosen = named or found
    pairs = {(r.field_of(target), r.fields[1 - r.side(target)]) for r in chosen}
    return Carried(tuple(chosen), bool(named) or len(pairs) == 1)
