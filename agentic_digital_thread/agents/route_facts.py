# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The turn's route-seed facts, written into each system delegation's ROUTE context.

The Mode A route seed gives the orchestrator model the connected systems, the
concepts each one shares with another, the IdentityRules and the confirmed
joins between them. The delegate step asks the model to carry those into a
fenced ROUTE block in each delegation's optional ``kg_context``. When it
leaves the block out, the system agent has no canonical concept or join rule
to read. The gateway client merges the facts of THIS delegation in at the
call boundary (:func:`merge`, :func:`call_facts`):

* ``target_system``: the seed system the delegation tool queries;
* ``canonical_concept``: the concept of each identity rule the delegation's
  question carried values along (``carried_values.carried``: values an
  earlier successful call to the rule's other system returned, under that
  rule's field), then the concepts the seed's matcher finds in the delegated
  question itself that the target shares with another system;
* ``join_rule``: the confirmed joins (steward-confirmed, or measured equal
  values) over the fields of those carried rules, only when the carried
  rules are specific (``Carried.specific``).

A label the model filled with anything but ``none`` keeps the model's text.
``translated_identifiers`` and ``scope`` are only ever the model's (``not
given`` when it gave none). With no concept and no join for the call, the
arguments go out as the model wrote them, and so does a block the merge would
add nothing to. The merged block has the shape the system agent's strict
ROUTE parser reads (``system_agent._route_blocks``): the fence, each label
once, a further join on its own indented line.

Each merged context of a call that went out (``call_delivery``) is kept by
tool use id (:attr:`TurnRouteFacts.received`) so the chat trace can show what
the system was sent (:func:`with_received_contexts`). Pure apart from the
per-turn context variable.
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
from dataclasses import dataclass, field

from . import carried_values, internal_lines
from .call_delivery import CallDelivery
from .concept_authority import STEWARD_CONFIRMED_METHODS
from .identity_affinity import EXACT_METHOD

#: The labels of the delegate step's ROUTE block, in its order.
LABELS = ("target_system", "translated_identifiers", "canonical_concept", "scope", "join_rule")
#: The join methods a ROUTE ``join_rule`` may cite (the delegate step's set).
CONFIRMED_JOIN_METHODS = frozenset({*STEWARD_CONFIRMED_METHODS, EXACT_METHOD})
#: The key a forwarded tool result carries its call's merged route context under.
RECEIVED_KEY = "adtRouteContext"
#: What a model-only label says when the model gave nothing for it.
NOT_GIVEN = "not given"
MAX_CONCEPTS = 4
MAX_JOINS = 4
#: Earlier results of this turn kept for :func:`carried_values.carried`.
MAX_RESULTS = 32
#: The merged block's bound: a join line that would pass it is left out.
BLOCK_CHAR_CAP = 1200
_VALUE_CAP = 256

_HEADER_RE = re.compile(r"^\s*(?:`{3})?\s*ROUTE\s*:?\s*$", re.IGNORECASE)
_LABEL_RE = re.compile(r"^\s*(" + "|".join(LABELS) + r")\s*:[ \t]*(.*)$", re.IGNORECASE)
_CONTINUED_RE = re.compile(r"^[ \t]+\S")
_FENCE_LINE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*$")
_FENCE_RE = re.compile(r"`{3,}|~{3,}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def _clean(value: object, cap: int = _VALUE_CAP) -> str:
    """One line of graph text: no control characters or fence runs, capped."""
    text = _FENCE_RE.sub("", _CONTROL_RE.sub(" ", str(value or "")))
    return text.strip()[:cap].strip()


@dataclass(frozen=True)
class Join:
    """One confirmed cross-system join from the seed."""

    id: str
    from_system: str
    from_field: str
    to_system: str
    to_field: str

    def peer_of(self, system_id: str) -> "str | None":
        """The other end's system when ``system_id`` is one end, else ``None``."""
        if self.from_system == system_id:
            return self.to_system
        return self.from_system if self.to_system == system_id else None

    def line(self) -> str:
        path = f"{self.from_system}.{self.from_field} -> {self.to_system}.{self.to_field}"
        return f"{self.id} {path}" if self.id else path


@dataclass
class TurnRouteFacts:
    """This turn's seed facts, what its delegations were sent, and what the
    successful ones returned."""

    systems: tuple = ()
    #: system id -> the concepts it shares with another system.
    shared: dict = field(default_factory=dict)
    #: The concept vocabulary the seed matched the user's question from.
    vocabulary: tuple = ()
    joins: tuple = ()
    rules: tuple = ()
    #: tool use id -> the merged ``kg_context`` that call carried.
    received: dict = field(default_factory=dict)
    #: ``carried_values.PeerResult`` of each successful delegation, oldest first.
    results: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


def _confirmed_join(rule) -> "Join | None":
    """``rule`` (a seed join_paths entry) as a :class:`Join` when it is
    confirmed and joins two different systems, else ``None``."""
    if not isinstance(rule, dict) or rule.get("method") not in CONFIRMED_JOIN_METHODS:
        return None
    parts = [_clean(rule.get(key)) for key in
             ("from_system", "from_field", "to_system", "to_field")]
    if not all(parts) or parts[0] == parts[2]:
        return None
    return Join(_clean(rule.get("id")), *parts)


def _shared(system: dict) -> set:
    """The concepts ``system``'s seed entry shares with another system."""
    own = system.get("id")
    return {str(item.get("concept")) for item in system.get("shared_concepts") or ()
            if isinstance(item, dict) and item.get("concept")
            and item.get("peer_system") and item.get("peer_system") != own}


def _rules(system: dict, kind: str) -> list:
    return list((system.get("rules") or {}).get(kind) or ())


def facts_from_seed(payload, vocabulary=()) -> "TurnRouteFacts | None":
    """The route facts of a composed seed's ``payload``, with the concept
    ``vocabulary`` delegated questions are matched with; ``None`` when the
    payload names no system."""
    listed = payload.get("systems") if isinstance(payload, dict) else None
    systems = [s for s in listed or () if isinstance(s, dict)
               and isinstance(s.get("id"), str) and s["id"]]
    if not systems:
        return None
    joins = [j for s in systems for j in map(_confirmed_join, _rules(s, "join_paths")) if j]
    rules = [r for s in systems
             for r in (carried_values.identity_rule(i, _clean) for i in _rules(s, "identity_rules"))
             if r]
    return TurnRouteFacts(systems=tuple(s["id"] for s in systems),
                          shared={s["id"]: frozenset(_shared(s)) for s in systems},
                          vocabulary=tuple(vocabulary or ()),
                          joins=tuple(dict.fromkeys(joins)), rules=tuple(dict.fromkeys(rules)))


_TURN: "contextvars.ContextVar" = contextvars.ContextVar("adt_turn_route_facts", default=None)


def remember_turn(payload, vocabulary=()) -> None:
    """Record this turn's route facts (:func:`facts_from_seed`)."""
    _TURN.set(facts_from_seed(payload, vocabulary))


def reset_turn() -> None:
    """Clear the current turn's route facts (start of every invocation)."""
    _TURN.set(None)


def turn_facts() -> "TurnRouteFacts | None":
    return _TURN.get()


def target_of(tool_name, systems) -> "str | None":
    """The seed system a gateway ``<target>___query_<id>`` tool queries."""
    if not isinstance(tool_name, str) or "___" not in tool_name:
        return None
    target, tool = tool_name.split("___", 1)
    if not tool.startswith("query_"):
        return None
    return next((sid for sid in systems if sid.replace("_", "-")[:100] == target), None)


def _parse(text: str) -> tuple:
    """``({label: value}, the text around the block)`` for the first ROUTE
    block in ``text``: a ``ROUTE:`` header (fenced or not), else the first
    label line, then label lines and their indented continuations up to a
    closing fence or any other line."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if _HEADER_RE.match(line)), None)
    first = header + 1 if header is not None else next(
        (i for i, line in enumerate(lines) if _LABEL_RE.match(line)), len(lines))
    values: dict = {}
    label, end = None, first
    while end < len(lines):
        line, end = lines[end], end + 1
        labelled = _LABEL_RE.match(line)
        if _FENCE_LINE_RE.match(line):
            break
        if labelled and labelled.group(1).lower() not in values:
            label = labelled.group(1).lower()
            values[label] = _FENCE_RE.sub("", labelled.group(2)).strip()
        elif label and _CONTINUED_RE.match(line):
            values[label] += "\n  " + _FENCE_RE.sub("", line).strip()
        else:
            end -= 1
            break
    start = header if header is not None else first
    return values, "\n".join(lines[:start] + lines[end:]).strip()


def _real(value: str) -> bool:
    return bool(value and value.strip()) and value.strip().lower() != "none"


def _filled(values: dict, target: str, concepts: tuple, joins: list) -> "dict | None":
    """The block's label values: the model's where real, the seed's facts
    where the model gave none; ``None`` when the seed adds nothing."""
    out = {label: values.get(label, "") for label in LABELS}
    added = False
    for label, fact in (("target_system", target), ("canonical_concept", ", ".join(concepts)),
                        ("join_rule", joins)):
        if fact and not _real(out[label]):
            out[label], added = fact, True
    if not added:
        return None
    for label, empty in (("translated_identifiers", NOT_GIVEN), ("scope", NOT_GIVEN),
                         ("canonical_concept", "none"), ("join_rule", "none")):
        out[label] = out[label] or empty
    return out


def _render(values: dict) -> str:
    """The fenced ROUTE block. Seed joins (a list) go one per line, the
    first on the label line, while the block stays within the bound."""
    head = [f"{label}: {values[label]}" for label in LABELS[:-1]]
    joins = values["join_rule"]
    if isinstance(joins, list):
        room = BLOCK_CHAR_CAP - len("```ROUTE:\n" + "\n".join(head) + "\njoin_rule: \n```")
        kept: list = []
        for join in joins:
            if len("\n  ".join([*kept, join.line()])) > room:
                break
            kept.append(join.line())
        joins = "\n  ".join(kept) or "none"
    return "```ROUTE:\n" + "\n".join([*head, f"join_rule: {joins}"]) + "\n```"


def _field_parts(path: str) -> tuple:
    return tuple(p.strip('"').lower() for p in str(path or "").split(".") if p.strip('"'))


def _ends_with(path: str, field_name: str) -> bool:
    whole, tail = _field_parts(path), _field_parts(field_name)
    return bool(tail) and whole[-len(tail):] == tail


def _join_follows(join: Join, rule, target: str) -> bool:
    """Is ``join`` between ``target`` and the rule's other system, over the
    rule's field on each side?"""
    peer = rule.systems[1 - rule.side(target)]
    if join.peer_of(target) != peer:
        return False
    own, other = ((join.from_field, join.to_field) if join.from_system == target
                  else (join.to_field, join.from_field))
    return _ends_with(own, rule.field_of(target)) and _ends_with(other, rule.field_of(peer))


def call_facts(facts: TurnRouteFacts, target: str, question) -> tuple:
    """``(concepts, joins)`` for one delegation to ``target`` asking
    ``question``: the carried rules' concepts, then the question's own shared
    concepts; the confirmed joins over specific carried rules."""
    from .concept_notes import matched_ids
    with facts.lock:
        results = tuple(facts.results)
    carried = carried_values.carried(facts.rules, results, target, question)
    shared = facts.shared.get(target, frozenset())
    asked = [c for c in matched_ids(question, facts.vocabulary) if c in shared]
    concepts = tuple(dict.fromkeys([*carried.concepts(), *asked]))[:MAX_CONCEPTS]
    joins = [j for j in facts.joins
             if carried.specific and any(_join_follows(j, r, target) for r in carried.rules)]
    return concepts, joins[:MAX_JOINS]


def merge(facts: "TurnRouteFacts | None", tool_name, arguments):
    """``arguments`` with this turn's route facts merged into the delegation's
    ``kg_context`` (a copy), or ``arguments`` itself when there is no seed,
    the tool is not a seed system's delegation, the seed has no concept and
    no join for this call (:func:`call_facts`), or the model's block already
    holds every fact."""
    if facts is None or not isinstance(arguments, dict):
        return arguments
    target = target_of(tool_name, facts.systems)
    existing = arguments.get("kg_context")
    if target is None or not (existing is None or isinstance(existing, str)):
        return arguments
    concepts, joins = call_facts(facts, target, arguments.get("question"))
    if not (concepts or joins):
        return arguments
    values, rest = _parse(existing or "")
    filled = _filled(values, target, concepts, joins)
    if filled is None:
        return arguments
    merged = dict(arguments)
    merged["kg_context"] = _render(filled) + (f"\n\n{rest}" if rest else "")
    return merged


def _result_text(result) -> str:
    blocks = result.get("content") if isinstance(result, dict) else None
    return "\n".join(b["text"] for b in blocks or ()
                     if isinstance(b, dict) and isinstance(b.get("text"), str))


#: A miss block's marker line, the opening the IDENTIFIER-MISS protocol writes.
_MISS_MARKER_RE = re.compile(r"^[ \t]*IDENTIFIER-MISS[ \t]*:[ \t]*$", re.IGNORECASE)
#: The fields a miss block holds, in the shape guard's blocks and in the
#: system agent's own protocol (``system_agent._MISS_PROTOCOL_LABELS``).
MISS_FIELD_LABELS = ("token", "looked_in", "my_format", "context")
_MISS_FIELD_RE = re.compile(
    r"^[ \t]*(?:" + "|".join(MISS_FIELD_LABELS) + r")[ \t]*:", re.IGNORECASE)
#: A fence line: its run of backticks or tildes, then the rest of the line.
_FENCE_LINE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


def _fence_step(line: str, fence: "str | None") -> "tuple[bool, str | None]":
    """``(is a fence line, the open fence after it)``. A fence opens with any
    run of three or more backticks or tildes; it closes on a line holding
    only a run of the same character at least as long."""
    match = _FENCE_LINE_RE.match(line)
    if not match:
        return False, fence
    run, rest = match.groups()
    if fence is None:
        return True, run
    if run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
        return True, None
    return False, fence


def _strip_miss_lines(text: str) -> str:
    """``text`` without the protocol lines of its IDENTIFIER-MISS blocks: each
    marker line and the field lines right after it (:data:`MISS_FIELD_LABELS`).
    The first line after them that is not a field is kept, with everything
    after it. When the marker is the first line inside a fence and the fields
    are followed at once by that fence's closing line, the two fence lines go
    too. Nothing else is removed: real data inside a block, before its fence
    closes, or after a block that never closes, is read as before."""
    lines, out, fence, opened_at, i = text.split("\n"), [], None, None, 0
    while i < len(lines):
        line = lines[i]
        if _MISS_MARKER_RE.match(line):
            j = i + 1
            while j < len(lines) and _MISS_FIELD_RE.match(lines[j]):
                j += 1
            own_fence = fence is not None and opened_at == len(out) - 1
            if own_fence and j < len(lines) and _fence_step(lines[j], fence) == (True, None):
                out.pop()
                fence, opened_at, j = None, None, j + 1
            i = j
            continue
        is_fence, after = _fence_step(line, fence)
        if is_fence and after is not None:
            opened_at = len(out)
        fence = after
        out.append(line)
        i += 1
    return "\n".join(out)


def without_miss_blocks(text: str) -> str:
    """``text`` as route facts read it: the protocol lines of every
    IDENTIFIER-MISS block removed (:func:`_strip_miss_lines`), whether the
    system agent wrote the block or the engine carried the shape guard's
    (``unresolved_miss``), in prose and in a JSON answer's engine note. A
    block's ``token:`` names a value the call could not use there, and read
    as a labelled cell it would take that value off its identity rule. The
    result the orchestrator reads keeps every block."""
    if not isinstance(text, str) or "IDENTIFIER-MISS" not in text.upper():
        return text
    from .tool_result_capture import RESULT_CUT_NOTE_KEY
    stripped = text.strip()
    if stripped[:1] == "{":
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get(RESULT_CUT_NOTE_KEY), str):
            parsed[RESULT_CUT_NOTE_KEY] = _strip_miss_lines(parsed[RESULT_CUT_NOTE_KEY])
            return json.dumps(parsed, ensure_ascii=False)
    return _strip_miss_lines(text)


def note_call(facts: "TurnRouteFacts | None", tool_name, tool_use_id, sent, result,
              delivery: CallDelivery = CallDelivery.SENT) -> None:
    """Record a delegation by how it went (``delivery``, ``call_delivery``,
    never read from the result's words): nothing of a call that was not sent;
    the merged context it was sent (``sent``, when the merge changed it) for
    every other call; and, for a sent call whose result is not an error, what
    its system returned (``carried_values.peer_result``, the latest
    :data:`MAX_RESULTS` kept). A call that may have acted got no answer, so
    nothing it returned is read. The orchestrator-only engine lines
    (``internal_lines``) and the IDENTIFIER-MISS blocks
    (:func:`without_miss_blocks`) are left out of what is read: the lines
    quote values no result of the call returned, and a block's token is a
    value the call could not use where it looked, so none of those is
    carried or labelled from them. The result the orchestrator reads is not
    changed."""
    if facts is None or delivery is CallDelivery.NOT_SENT:
        return
    text = without_miss_blocks(internal_lines.public_text(_result_text(result)))
    target = target_of(tool_name, facts.systems)
    ok = (delivery is CallDelivery.SENT and target is not None and isinstance(result, dict)
          and result.get("status") != "error")
    read = carried_values.peer_result(target, text) if ok and text else None
    with facts.lock:
        if isinstance(sent, str) and isinstance(tool_use_id, str) and tool_use_id:
            facts.received[tool_use_id] = sent
        if read is not None:
            facts.results.append(read)
            del facts.results[:-MAX_RESULTS]


def with_received_contexts(event, facts: "TurnRouteFacts | None"):
    """``event`` with each tool result whose call carried a merged route
    context holding it under :data:`RECEIVED_KEY`, as a copy; ``event`` itself
    when none applies. The message the agent keeps is never changed."""
    message = event.get("message") if isinstance(event, dict) else None
    if facts is None or not isinstance(message, dict) or message.get("role") != "user" \
            or not isinstance(message.get("content"), list):
        return event
    with facts.lock:
        received = dict(facts.received)
    blocks, changed = [], False
    for block in message["content"]:
        result = block.get("toolResult") if isinstance(block, dict) else None
        sent = received.get(result.get("toolUseId")) if isinstance(result, dict) else None
        if isinstance(sent, str):
            block, changed = {**block, "toolResult": {**result, RECEIVED_KEY: sent}}, True
        blocks.append(block)
    return {**event, "message": {**message, "content": blocks}} if changed else event


def wrap_stream(stream):
    """``stream`` (an Agent's ``stream_async``) forwarding each merged route
    context with its tool result (:func:`with_received_contexts`); ``stream``
    itself under ``RESPONSE_INTEGRITY_MODE=enforce``, which shows no call
    arguments."""
    from .config import response_integrity_enforced
    if response_integrity_enforced():
        return stream

    async def wrapped(prompt, **kwargs):
        facts = turn_facts()
        async for event in stream(prompt, **kwargs):
            yield with_received_contexts(event, facts)
    return wrapped
