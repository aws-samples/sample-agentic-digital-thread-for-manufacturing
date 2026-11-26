# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Plain-language trace text for the Mode-A knowledge-graph routing step.

Every Mode-A turn starts with a deterministic knowledge-graph lookup
(``orchestrator.build_route_seed``) whose result rides in the user turn. The UI
shows that lookup as one "Knowledge Graph" trace step. Before this module the
step carried one fixed sentence ("Checked the knowledge graph ..."), so a user
could see THAT the graph was read but not WHAT it contributed, and could not
tell why a given system was asked or how an identifier was matched.

:func:`seed_step_text` renders what the seed actually carried for THIS
question: the connected systems it listed, the shared concepts that link them,
the assets each system was counted to hold under each site when it was
onboarded, every identifier translation the rules engine applied, and every
stored value mapping the seed carried for an identifier the question names. It
reads only the already-built seed string (no graph I/O), uses display names
rather than rule ids, and never adds anything the seed does not contain. This text goes to the
trace stream only; it never enters the model's token path.

Pure and fail-soft: an unparseable seed yields :data:`FALLBACK_TEXT`. A seed
that lists no system but counts the connected systems it left out (the bounded
emergency seed) says so instead of the generic sentence.

The step is streamed only; it is not one of the agent's messages, so the stored
turn has no call for it. :func:`stored_step` rebuilds it for a reopened chat
from the seed the stored user turn starts with, with the same tool id, label
and text the live step had.
"""
from __future__ import annotations

import functools
import json
import re

from .identifier_translation import (MATCH_HOP, candidate_phrase, parse_rendered,
                                     parse_value_mappings)

#: The pre-findings sentence, kept for Mode B, empty seeds and parse failures.
FALLBACK_TEXT = ("Checked the knowledge graph for the systems, shared "
                 "concepts, and translation rules relevant to this question.")

#: Display bounds. The seed is already budget-bounded; these keep the trace
#: step to a few readable lines.
MAX_SYSTEMS = 8
MAX_CONCEPTS = 8

#: The payload marker shared by the full and PARTIAL route-seed headers.
_RESULT_MARKER = "ROUTE SEED RESULT:\n"

#: How a rule came to exist, in words a non-engineer can read. Unknown methods
#: fall back to a neutral statement rather than a guess.
_METHOD_WORDS = {
    "user_confirmed": "confirmed by a reviewer",
    "crosswalk_confirmed": "confirmed by a reviewer",
    "value_core_match": "learned at onboarding by matching identifier values",
    "onboarding-llm": "proposed at onboarding and approved at the Identity Rules step",
    "provision-request": "sent in a provision request, not reviewed at the Identity Rules step",
    "user_unverified": ("taught by a reviewer without the identifier it was learned from, "
                        "so not checked on real values"),
}
_METHOD_DEFAULT = "recorded in the knowledge graph"


def _payload(seed: str) -> "dict | None":
    if not isinstance(seed, str) or _RESULT_MARKER not in seed:
        return None
    fragment = seed.split(_RESULT_MARKER, 1)[1].lstrip()
    payload, _end = json.JSONDecoder().raw_decode(fragment)
    return payload if isinstance(payload, dict) else None


def _system_names(payload: dict) -> "dict[str, str]":
    names: dict[str, str] = {}
    for system in payload.get("systems") or []:
        if isinstance(system, dict) and system.get("id"):
            names[str(system["id"])] = str(system.get("name") or system["id"])
    return names


def _concepts(payload: dict) -> "list[str]":
    seen: list[str] = []
    for system in payload.get("systems") or []:
        if not isinstance(system, dict):
            continue
        for item in system.get("shared_concepts") or []:
            name = item.get("concept") if isinstance(item, dict) else item
            if isinstance(name, str) and name:
                label = name.replace("_", " ").replace("-", " ")
                if label not in seen:
                    seen.append(label)
    return seen


def _coverage_lines(payload: dict, names: "dict[str, str]") -> "list[str]":
    """One line per system whose seed entry carries ``asset_coverage`` (the
    counts onboarding took from its asset hierarchy), by display name."""
    lines = []
    for system in payload.get("systems") or []:
        if not isinstance(system, dict):
            continue
        coverage = system.get("asset_coverage")
        if isinstance(coverage, str) and coverage and system.get("id"):
            label = names.get(str(system["id"]), str(system["id"]))
            lines.append(f"- Assets in {label}: {coverage}")
    if len(lines) > MAX_SYSTEMS:
        more = len(lines) - MAX_SYSTEMS
        lines = [*lines[:MAX_SYSTEMS], f"- (+{more} more systems with asset coverage)"]
    return lines


def _bounded(items: "list[str]", cap: int, extra: int = 0) -> str:
    more = max(0, len(items) - cap) + extra
    text = ", ".join(items[:cap])
    return f"{text} (+{more} more)" if more else text


def _translation_line(item: dict, names: "dict[str, str]") -> str:
    def where(system: str, field: str) -> str:
        label = names.get(system, system)
        return f"{label} ({field})" if field else label

    how = _METHOD_WORDS.get(item["method"].lower(), _METHOD_DEFAULT)
    if item.get("candidate_set"):  # ADR-0086: a set of records, never one value
        target, tail = candidate_phrase(item["translated"]), (
            "; several records share it, so the answer must choose one.")
    else:
        target, tail = item["translated"], "."
    return (f"- Identifier match: {item['token']} in "
            f"{where(item['from_system'], item['from_field'])} matches "
            f"{target} in {where(item['to_system'], item['to_field'])}{tail} "
            f"Rule {how}.")


_LOOKED_UP = "Looked up this question in the knowledge graph before asking any system."


def _none_listed_text(payload: dict) -> str:
    """The step for a seed that lists no system but counts the connected
    systems it left out (the bounded emergency seed), or :data:`FALLBACK_TEXT`
    when it counts none. It reports only what the seed shows: how many systems
    were left out, not why."""
    omitted = payload.get("systems_omitted")
    if isinstance(omitted, bool) or not isinstance(omitted, int) or omitted <= 0:
        return FALLBACK_TEXT
    noun = "system" if omitted == 1 else "systems"
    pronoun = "it" if omitted == 1 else "them"
    return (f"{_LOOKED_UP}\n- Connected systems: none listed. The lookup result left out "
            f"all {omitted} connected {noun}, so the answer has to find {pronoun} itself.")


def _render(payload: dict, seed: str) -> str:
    names = _system_names(payload)
    if not names:
        return _none_listed_text(payload)
    lines = [_LOOKED_UP]
    omitted = payload.get("systems_omitted") or 0
    extra = omitted if isinstance(omitted, int) and omitted > 0 else 0
    lines.append(f"- Connected systems: {_bounded(list(names.values()), MAX_SYSTEMS, extra)}")
    concepts = _concepts(payload)
    if concepts:
        lines.append(f"- Shared concepts linking them: {_bounded(concepts, MAX_CONCEPTS)}")
    lines.extend(_coverage_lines(payload, names))
    translations = parse_rendered(seed, systems=names)
    if translations:
        lines.extend(_translation_line(t, names) for t in translations)
    mappings = parse_value_mappings(seed)
    lines.extend(_value_mapping_line(m, names) for m in mappings)
    if not translations and not mappings:
        lines.append("- No identifier in this question needed translating between systems.")
    return "\n".join(lines)


#: How a stored ValueMapping the block may carry came to exist, read before
#: :data:`_METHOD_WORDS`: the block carries a steward-confirmed row only when
#: a person accepted that one row (``identifier_translation.value_mapping_trusted``),
#: so its words say so, and the deterministic onboarding matches
#: (``identifier_translation.DETERMINISTIC_VALUE_MAPPING_METHODS``).
_VALUE_MAPPING_METHOD_WORDS = {
    "user_confirmed": "accepted by a steward one row at a time",
    "crosswalk_confirmed": "accepted by a steward one row at a time",
    "value_format_match": "matched at onboarding by the form of the two values",
    "equipment_name_match": "matched at onboarding by the same machine name in both systems",
}


def _value_mapping_line(item: dict, names: "dict[str, str]") -> str:
    """One stored value mapping the seed handed over, in plain words."""
    def where(system: str, field: str) -> str:
        label = names.get(system, system)
        return f"{label} ({field})" if field else label

    method = item["method"].lower()
    how = _VALUE_MAPPING_METHOD_WORDS.get(method, _METHOD_DEFAULT)
    via = (f" Found one link further, through {item['via']}."
           if item.get("match") == MATCH_HOP and item.get("via") else "")
    return (f"- Stored value mapping: {item['from_value']} in "
            f"{where(item['from_system'], item['from_field'])} is {item['to_value']} in "
            f"{where(item['to_system'], item['to_field'])}.{via} Mapping {how}.")


def seed_step_text(seed: "str | None") -> str:
    """The Knowledge Graph trace step for a Mode-A route seed.

    Returns :data:`FALLBACK_TEXT` for anything that is not a parseable Mode-A
    route seed (Mode B graph-first seeds, malformed payloads) and for a seed
    that lists no system and counts none left out.
    """
    try:
        payload = _payload(seed or "")
        return _render(payload, seed or "") if payload else FALLBACK_TEXT
    except Exception:  # noqa: BLE001 - trace text never breaks a turn
        return FALLBACK_TEXT


#: Characters of the user's question the step label keeps.
QUESTION_LABEL_CHARS = 140
_WORD = re.compile(r"\S+")


def _collapsed_head(text: str, limit: int) -> str:
    """``" ".join(text.split())``, cut at ``limit`` characters with a trailing
    ellipsis when longer. Reads ``text`` only as far as the cut needs and
    copies at most ``limit + 1`` characters of any one word."""
    pieces: list[str] = []
    size = -1
    for match in _WORD.finditer(text):
        start, end = match.span()
        pieces.append(text[start:min(end, start + limit + 1)])
        size += 1 + end - start
        if size > limit:
            return " ".join(pieces)[:limit].rstrip() + "\u2026"
    return " ".join(pieces)


def seed_question(mode: str, prompt: str) -> str:
    """The step's ``input.question``: the user's question (whitespace
    collapsed, cut at :data:`QUESTION_LABEL_CHARS`) inside a routing sentence,
    so the trace reads as the routing decision for this question. The chat's
    KG labels read it first (``kg-label.ts`` ``kgQuestionText``); without it
    the step is labelled with the bare word "query". ``mode`` is
    ``SYSTEM_QUERY_MODE``; ``orchestrator`` is Mode B."""
    question = _collapsed_head(prompt or "", QUESTION_LABEL_CHARS)
    if mode == "orchestrator":
        return (f"Which systems (and their exact schemas) are needed to answer: "
                f"\u201c{question}\u201d")
    return (f"Which systems hold this data, and how do their identifiers relate, "
            f"to answer: \u201c{question}\u201d")


#: What separates the composed preamble (the seed, then the blocks composed
#: after it) from the question in a stored user turn
#: (``runtime_app._compose_seeded_turn``).
_TURN_SEPARATOR = "\n\n---\n\n"


@functools.lru_cache(maxsize=1)
def _seed_headers() -> tuple:
    """``(header, mode)`` for every header the server opens a seed with: the
    Mode-A route seed's full and PARTIAL headers and their value-mapping
    variants (``orchestrator.build_route_seed``), and the Mode-B graph-first
    header (``orchestrator.build_graph_first_seed``)."""
    from .orchestrator import (_GRAPH_FIRST_SEED_HEADER, _ROUTE_SEED_HEADER,
                               _ROUTE_SEED_PARTIAL_HEADER, _ROUTE_SEED_VALUE_MAPPING_HEADERS)
    mode_a = (_ROUTE_SEED_HEADER, _ROUTE_SEED_PARTIAL_HEADER,
              *_ROUTE_SEED_VALUE_MAPPING_HEADERS.values())
    return (*((header, "agent") for header in mode_a), (_GRAPH_FIRST_SEED_HEADER, "orchestrator"))


@functools.lru_cache(maxsize=1)
def stored_turn_read_chars() -> int:
    """The most characters of a stored turn the reload reads for a Mode-A
    seed before its separator, and for either mode after the separator, where
    the label needs only the bracketed preambles and
    :data:`QUESTION_LABEL_CHARS` of the question.

    Sized from what the server composes before the separator in Mode A. The
    route seed is at most its budget (``orchestrator._ROUTE_SEED_SERIALIZED_BUDGET``)
    plus the asset-coverage and concept-notes allowances put on top of it,
    each held to its own cap (``_ROUTE_SEED_ASSET_COVERAGE_TOTAL_CAP``,
    ``concept_notes.TOTAL_CHAR_CAP`` and the unrecorded-terms line); a second
    budget holds those with their JSON keys and the metric-term gap notice.
    Then come the resolved time windows (``time_windows.MAX_BLOCK_CHARS``)
    and the metric-layer notice (``metric_turn_render.MAX_NOTICE_CHARS``). A
    Mode-A turn whose separator lies further in is not one the server
    composed. A Mode-B seed has no size cap (the server caps the number of
    routed systems, not their schemas), so it is never decoded
    (:func:`_mode_b_separator`)."""
    from .metric_turn_render import MAX_NOTICE_CHARS
    from .orchestrator import _ROUTE_SEED_SERIALIZED_BUDGET
    from .time_windows import MAX_BLOCK_CHARS
    return 2 * _ROUTE_SEED_SERIALIZED_BUDGET + MAX_BLOCK_CHARS + MAX_NOTICE_CHARS


def _mode_a_seed(turn: str, body: int) -> "tuple | None":
    """``(payload, separator index)`` for a Mode-A seed whose payload starts
    at ``body``, else ``None``: the separator within
    :func:`stored_turn_read_chars`, and a payload object listing ``systems``,
    as every route seed the server writes does (an empty list in the bounded
    emergency seed). Decodes only the text before the separator. Raises on a
    payload that does not parse."""
    separator = turn.find(_TURN_SEPARATOR, body, stored_turn_read_chars())
    if separator < 0:
        return None
    payload, _end = json.JSONDecoder().raw_decode(turn[body:separator])
    if isinstance(payload, dict) and isinstance(payload.get("systems"), list):
        return payload, separator
    return None


#: The last two members of every Mode-B payload, in the order the server
#: writes them (``orchestrator._explore_graph_with_schema``, ``json.dumps``
#: with its default separators): the routed schemas, opening on a first
#: system id, then their note, a string that closes the object.
_ROUTED_SCHEMAS_OPENING = '"routed_system_schemas": {"'
_ROUTED_SCHEMAS_NOTE = ', "routed_system_schemas_note": "'
_PAYLOAD_CLOSE = '"}'


def _mode_b_separator(turn: str, body: int) -> "int | None":
    """The separator index for a Mode-B seed whose payload starts at
    ``body``, else ``None``.

    The payload is one line of JSON (``json.dumps`` writes no raw newline),
    so it ends at the first newline. It must open an object at ``body``, close
    with the note string, and hold the note member after a non-empty routed
    schemas object, which is how ``build_graph_first_seed`` writes every seed
    it returns. These are positional checks with ``str.startswith`` and
    ``str.find`` / ``rfind`` over the turn: nothing is decoded or copied, so a
    seed of any size is read in place. A payload cut short does not close
    with the note string."""
    if not turn.startswith("{", body):
        return None
    line_end = turn.find("\n", body)
    if line_end < body + len(_PAYLOAD_CLOSE) or not turn.startswith(
            _PAYLOAD_CLOSE, line_end - len(_PAYLOAD_CLOSE)):
        return None
    note = turn.rfind(_ROUTED_SCHEMAS_NOTE, body, line_end)
    if note < 0 or turn.rfind(_ROUTED_SCHEMAS_OPENING, body, note) < 0:
        return None
    separator = turn.find(_TURN_SEPARATOR, line_end)
    return separator if separator >= 0 else None


def _stored_seed(turn: object) -> "tuple | None":
    """``(mode, payload, separator index)`` for a turn that opens with a
    seed in the place and shape the server writes it, else ``None``: one of
    the server's own headers (:func:`_seed_headers`) at the very start and
    its payload right after it, read by :func:`_mode_a_seed` or
    :func:`_mode_b_separator`. ``payload`` is ``None`` for Mode B, whose step
    text does not depend on it. Raises on a Mode-A payload that does not
    parse."""
    if not isinstance(turn, str):
        return None
    form = next((form for form in _seed_headers() if turn.startswith(form[0])), None)
    if form is None:
        return None
    header, mode = form
    if mode == "orchestrator":
        separator = _mode_b_separator(turn, len(header))
        return None if separator is None else (mode, None, separator)
    found = _mode_a_seed(turn, len(header))
    return None if found is None else (mode, *found)


def _rendered(payload: dict, seed: str) -> str:
    """:func:`seed_step_text` for a payload already decoded from ``seed``."""
    try:
        return _render(payload, seed)
    except Exception:  # noqa: BLE001 - trace text never breaks a reload
        return FALLBACK_TEXT


def stored_step(turn: "str | None") -> "dict | None":
    """The Knowledge Graph step the live trace showed for a stored user turn,
    as ``runtime_app._seed_trace_events`` built it: ``{"tool_id", "input",
    "text"}``. ``None`` when the turn does not open with a seed in the place
    and shape the server writes (:func:`_stored_seed`), when a Mode-A seed is
    longer before its separator than the server composes, and when a
    payload does not parse or is cut short: that turn shows no step. Never
    raises. Copies at most :func:`stored_turn_read_chars` characters of the
    turn, whatever its size.

    The Mode-A step text is read from the part before the separator (the
    payload parse stops where the seed's JSON ends; the identifier blocks are
    found by their own headers). Mode B shows its fixed sentence, as live.

    The label is rebuilt from the question after the separator with the
    server's bracketed preambles peeled: on the browser path, the question
    the runtime labelled live (the payload's ``rawPrompt``). A direct runtime
    caller that sends no ``rawPrompt`` had its label built from the whole
    prompt; the stored text is the same either way, so its reload shows the
    peeled question.

    A seed is told from user text by its place and shape alone. On the
    browser path the server's temporal context comes before anything typed
    (``server._prepare_question``), so typed text never opens a stored turn.
    A direct caller that sends no temporal context could store user text that
    is byte for byte a seed and separator; nothing stored (the event is the
    message alone) tells the two apart, gated or not. Such a turn reads as
    its seed (gated: tool name, id and metadata only), in that actor's own
    reload only (events are read per actor); the admin view never shows it."""
    try:
        found = _stored_seed(turn)
        if found is None:
            return None
        mode, payload, separator = found
        from .memory_labels import strip_turn_preambles
        from .orchestrator import GRAPH_FIRST_SEED_TOOL_ID, ROUTE_SEED_TOOL_ID
        start = separator + len(_TURN_SEPARATOR)
        asked = strip_turn_preambles(turn[start:start + stored_turn_read_chars()])
        mode_b = mode == "orchestrator"
        return {
            "tool_id": GRAPH_FIRST_SEED_TOOL_ID if mode_b else ROUTE_SEED_TOOL_ID,
            "input": {"question": seed_question(mode, asked)},
            "text": FALLBACK_TEXT if mode_b else _rendered(payload, turn[:separator]),
        }
    except Exception:  # noqa: BLE001 - a reload without the step is the fallback
        return None
