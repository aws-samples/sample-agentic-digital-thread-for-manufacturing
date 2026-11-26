# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engine crosses a stored pairing of two identifier forms itself.

A system can keep one concept under a different identifier in each of two
fields (two tables), while another system pairs the two forms per item. The
route seed says so (the IDENTIFIER FORMS block, ADR-0087 amendment
2026-10-15), the shape guard refuses a lookup of one form in the other's
field, and the orchestrator prompt asks the model to fetch the other form
from the pairing system and ask again. Measured on a workshop seat
(2026-11-06, seven wordings of one lot-to-customers question, 10 asks each),
the model did that about half the time: it ran a graph lookup that found no
stored value mapping and stopped, or it took the holder's ``not_connected``
decline as final, so the answer covered only the batch it was asked about.

This module does the crossing in the delegation tool, deterministically:

* :func:`crossings_for` reads the turn's IDENTIFIER FORMS lines (the route
  seed's own facts, ``identifier_provenance.turn_facts``) for the system a
  delegation went to (the holder), when another system pairs its forms
  through a stored JoinPath between two tables, the line's concept is on the
  catalog's crossing allowlist (``catalog.CROSSABLE_CONCEPTS``, never a
  ``catalog.PERSON_CONCEPTS`` one) and no listed field is named like a
  person-valued field (the pairing system checks every column's stored
  concepts and names again);
* :func:`need` reads the holder's answer for one of the two signals that it
  could not cross: a wrong-form IDENTIFIER-MISS block (a token of one form
  looked up in the field that holds another form) or a typed
  ``not_connected`` decline about the line's concept. With either, every
  token of the delegated question and of the answer whose literal shape is a
  listed form's measured shape is a value to pair;
* each pairing system is asked, concurrently and with no model, for the other
  form of those values (``form_pairing``): the request names only the stored
  JoinPath and the values, and the runtime picks the columns itself;
* a round is complete only when every pairing read succeeded whole, named the
  two expected fields, dropped nothing, and paired every selected value with
  exactly one counterpart that pairs with nothing else (:meth:`Pairs.add_round`).
  Then the holder is asked again once with the pairs added to its question.

Any round that starts and does not finish complete (a missing, ambiguous,
conflicting, cut or failed pairing; a re-ask that fails or runs out of time)
returns the delegation's own result unchanged: the same object, no pairs, no
note. Only a finished crossing carries one fixed note naming the pairing
system and the pairs. Rounds, pairing calls, forms per line and time are
bounded per delegation and per turn; a round that the turn's remaining time
cannot hold does not start.

Nothing is inferred from digits or from a value's text: a pair is only what
the pairing system's rows hold. Generic: the systems, fields and shapes come
from the graph's rules and join paths; no dataset literal.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from .form_pairing import ENGINE_TAG, PAIRING_QUESTION, RESERVED_ARG, request_arg
from .turn_limits import ASK_RUN_CEILING_SECONDS

logger = logging.getLogger(__name__)

#: Rounds (pairing reads plus one re-ask) for one delegation.
MAX_ROUNDS = 2
#: Rounds across one turn (one gateway client), so also the most re-asks it makes.
MAX_ROUNDS_PER_TURN = 3
#: Most forms one IDENTIFIER FORMS line may list and still be crossed.
MAX_FORMS = 3
#: Values of one form paired per round.
MAX_VALUES = 16
#: Pairing calls one round may make, and one turn.
MAX_PAIRING_CALLS_PER_ROUND = 4
MAX_PAIRING_CALLS_PER_TURN = 8
#: How long one round's pairing reads may take together (they run concurrently).
PAIRING_SECONDS = 30.0
#: The least time a round needs for its holder re-ask; a round with less left does not start.
REASK_MIN_SECONDS = 120.0
#: Time kept back from the turn's ceiling for the rest of the answer.
ANSWER_RESERVE_SECONDS = 300.0
#: The turn's ceiling (``turn_limits``): the crossing's one aggregate deadline is
#: the gateway client's build (the turn's start) plus this, less the reserve.
TURN_BUDGET_SECONDS = float(ASK_RUN_CEILING_SECONDS)
_SCAN_CHARS = 64_000
_SCAN_TOKENS = 4000
QUESTION_HEADER = (
    "[engine] The other identifier form of the items below, read just now from {peers}, "
    "which pairs them per item. Each line gives one item's value in each of two fields "
    "of this system:")
QUESTION_FOOTER = ("Look each item up in each field with the form that field holds. These "
                   "pairs cover only the items listed.")
NOTE_PREFIX = "[engine] Identifier forms crossed:"
_CROSSED = ("{holder} keeps {concept} under a different identifier in each field below, and "
            "{peers} pairs them per item. The engine read the other form of the items "
            "{holder} named from {peers} and asked {holder} again {times} with both forms of "
            "each item; the answer above is its answer to that.")


def _monotonic() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class Form:
    """One listed form: the holder's field, the pairing system's table and
    column holding the same values, and the measured shape."""
    holder_field: str
    table: str
    column: str
    shape: str

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}".lower()


@dataclass(frozen=True)
class Crossing:
    """One IDENTIFIER FORMS line whose forms another system pairs."""
    holder: str
    concept: str
    peers: tuple
    forms: tuple


def _form(entry) -> Form | None:
    holder_field, peer_field, shape = entry
    table, _dot, column = str(peer_field or "").rpartition(".")
    if not (table and column and shape):
        return None
    return Form(str(holder_field), table.split(".")[-1], column, str(shape))


def _crossable(line: dict, holder: str) -> tuple | None:
    """The forms of a line this module may cross: the holder's, about a
    concept on the catalog's crossing allowlist (``catalog.crossable_concept``),
    paired by the source through a stored join (not one row), at most
    :data:`MAX_FORMS`, each with its own table and its own measured shape,
    and no field named like a person-valued field (``catalog.person_valued_field``)."""
    from .catalog import crossable_concept, person_valued_field
    if (line.get("system") != holder or line.get("pairer") != "source"
            or not line.get("pairing") or not crossable_concept(line.get("concept"))):
        return None
    forms = tuple(_form(entry) for entry in line.get("fields") or ())
    if not 2 <= len(forms) <= MAX_FORMS or None in forms:
        return None
    if any(person_valued_field(f.holder_field.rpartition(".")[2]) or person_valued_field(f.column)
           for f in forms):
        return None
    if len({f.shape for f in forms}) != len(forms) or len({f.table.lower() for f in forms}) != len(forms):
        return None
    return forms


def crossings_for(facts, holder: str) -> list:
    """The turn's IDENTIFIER FORMS lines of ``holder`` that a source system
    pairs through a stored join (:func:`_crossable`)."""
    from .identifier_translation import find_field_forms
    if facts is None or not holder:
        return []
    out = []
    for line in find_field_forms(list(facts.identity_rules), list(facts.join_paths),
                                 facts.question):
        forms = _crossable(line, holder)
        if forms:
            out.append(Crossing(holder, str(line.get("concept") or ""),
                                tuple(line.get("peers") or ()), forms))
    return out


# --------------------------------------------------------------------------- #
# Did the holder fail to cross?
# --------------------------------------------------------------------------- #
_MISS_FIELDS = ("token", "looked_in", "my_format", "context")


def miss_blocks(text: str) -> list:
    """``(token, looked_in)`` of each IDENTIFIER-MISS block in ``text``."""
    import re
    marker = re.compile(r"^[ \t]*IDENTIFIER-MISS[ \t]*:[ \t]*$", re.IGNORECASE)
    labelled = re.compile(r"^[ \t]*(" + "|".join(_MISS_FIELDS) + r")[ \t]*:[ \t]*(.*?)[ \t]*$",
                          re.IGNORECASE)
    lines, out = str(text or "").splitlines(), []
    for i, line in enumerate(lines):
        if not marker.match(line):
            continue
        fields: dict = {}
        for nxt in lines[i + 1:i + 1 + len(_MISS_FIELDS)]:
            match = labelled.match(nxt)
            if not match:
                break
            fields.setdefault(match.group(1).lower(), match.group(2).strip("`'\" "))
        if fields.get("token") and fields.get("looked_in"):
            out.append((fields["token"], fields["looked_in"]))
    return out


def _names_field(looked_in: str, holder_field: str) -> bool:
    from .identifier_translation import _table_column
    want = _table_column(holder_field).lower()
    got = str(looked_in or "").strip("`'\" ").lower()
    return bool(want) and (got == want or got.endswith("." + want))


def _refused(text: str, crossing: Crossing) -> bool:
    """A miss block looked a token of one listed form up in another form's field."""
    from .identity_affinity import literal_shape
    for token, looked_in in miss_blocks(text):
        shape = literal_shape(token)
        for k, form in enumerate(crossing.forms):
            if _names_field(looked_in, form.holder_field) and any(
                    other.shape == shape for j, other in enumerate(crossing.forms) if j != k):
                return True
    return False


def _declined(text: str, crossing: Crossing) -> bool:
    """The answer's typed decline is ``not_connected`` about the line's concept."""
    from .delegation_decline import fold, parse_engine_line
    decline = parse_engine_line(text)
    return bool(decline) and decline.get("reason_kind") == "not_connected" \
        and fold(decline.get("concept")) == fold(crossing.concept)


def _values(text: str, question: str, crossing: Crossing, asked: set) -> dict:
    """``{form index: [values]}``: the tokens of the question and the answer
    (orchestrator-only lines and miss-block protocol lines left out, the
    blocks' tokens kept) of a listed form's shape, not asked before."""
    from .carried_values import _identifiers
    from .identity_affinity import literal_shape
    from .internal_lines import public_text
    from .route_facts import without_miss_blocks
    tokens = "\n".join(token for token, _ in miss_blocks(text))
    scan = f"{question}\n{without_miss_blocks(public_text(text))}\n{tokens}"[:_SCAN_CHARS]
    found: dict = {}
    for token in _identifiers(scan, _SCAN_TOKENS):
        shape = literal_shape(token)
        for i, form in enumerate(crossing.forms):
            values = found.setdefault(i, []) if form.shape == shape else None
            if (values is not None and token.casefold() not in asked
                    and token.casefold() not in {v.casefold() for v in values}):
                values.append(token)
    return {i: values for i, values in found.items() if values}


def need(text: str, question: str, crossing: Crossing, asked: set = frozenset()) -> dict:
    """The values to pair (:func:`_values`) when the answer shows the holder
    could not cross (:func:`_refused` or :func:`_declined`), else ``{}``. A
    form with more than :data:`MAX_VALUES` values makes it ``{}``: a partial
    pairing is never sent."""
    if not (_refused(text, crossing) or _declined(text, crossing)):
        return {}
    found = _values(text, question or "", crossing, set(asked))
    return {} if any(len(v) > MAX_VALUES for v in found.values()) else found


# --------------------------------------------------------------------------- #
# Pairing requests and their answers
# --------------------------------------------------------------------------- #
def requests(crossing: Crossing, found: dict, join_paths) -> list | None:
    """``[(peer, (i, j), reserved argument)]``: for each two forms with a value
    to pair and each pairing system with a stored direct pairing JoinPath
    between their tables, one request with both forms' values. None when such
    two forms have no pairing system with that join (the round cannot be
    complete)."""
    from .identifier_translation import pairing_edge
    out = []
    count = len(crossing.forms)
    for i in range(count):
        for j in range(i + 1, count):
            values = list(found.get(i, [])) + list(found.get(j, []))
            if not values:
                continue
            edges = [(peer, pairing_edge(peer, crossing.forms[i].table, crossing.forms[j].table,
                                         join_paths)) for peer in crossing.peers]
            edges = [(peer, edge[1]) for peer, edge in edges if edge and edge[1]]
            if not edges:
                return None
            out.extend((peer, (i, j), request_arg(join_id, values)) for peer, join_id in edges)
    return out


def result_text(result) -> str:
    blocks = result.get("content") if isinstance(result, dict) else None
    return "\n".join(b["text"] for b in blocks or ()
                     if isinstance(b, dict) and isinstance(b.get("text"), str))


def read_reply(result, forms: tuple) -> list | None:
    """``[(value of forms[0], value of forms[1])]`` from one pairing answer,
    or None unless it is the engine's whole successful read of exactly those
    two fields with nothing dropped."""
    try:
        data = json.loads(result_text(result))
    except ValueError:
        return None
    if (not isinstance(data, dict) or data.get("engine") != ENGINE_TAG
            or data.get("status") != "ok" or data.get("cut") is not False
            or data.get("dropped") != 0 or not isinstance(data.get("pairs"), list)):
        return None
    names = [str(n).lower() for n in data.get("fields") or ()]
    want = [forms[0].name, forms[1].name]
    if sorted(names) != sorted(want) or len(names) != 2:
        return None
    flip = names != want
    pairs = []
    for pair in data["pairs"]:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            return None
        pairs.append((pair[1], pair[0]) if flip else (pair[0], pair[1]))
    return pairs


def one_to_one(pairs: list, values_i: list, values_j: list) -> bool:
    """Every value on either side of ``pairs`` pairs with exactly one value,
    and every selected value of either form is in a pair."""
    left = Counter(a.casefold() for a, _b in set(pairs))
    right = Counter(b.casefold() for _a, b in set(pairs))
    if any(n != 1 for n in (*left.values(), *right.values())):
        return False
    return all(v.casefold() in left for v in values_i) and \
        all(v.casefold() in right for v in values_j)


@dataclass
class Pairs:
    """The pairs read for one crossing, per two forms, and the peers that held them."""
    peers: list = field(default_factory=list)
    items: dict = field(default_factory=dict)

    def add_round(self, crossing: Crossing, found: dict, sent: list, replies: list) -> bool:
        """Add one round's pairs when the round is complete (see the module
        docstring); nothing is added otherwise."""
        by_key: dict = {}
        for (peer, key, _raw), reply in zip(sent, replies):
            pairs = read_reply(reply, (crossing.forms[key[0]], crossing.forms[key[1]]))
            if pairs is None:
                return False
            by_key.setdefault(key, []).extend(pairs)
            if pairs and peer not in self.peers:
                self.peers.append(peer)
        for (i, j), pairs in by_key.items():
            merged = self.items.get((i, j), []) + pairs
            if not one_to_one(merged, found.get(i, []), found.get(j, [])):
                return False
        for key, pairs in by_key.items():
            bucket = self.items.setdefault(key, [])
            bucket.extend(p for p in pairs if p not in bucket)
        return True


def pair_lines(crossing: Crossing, pairs: Pairs) -> list:
    """One header line per two fields, then one line per item."""
    from .identifier_translation import _table_column
    lines = []
    for (i, j), kept in sorted(pairs.items.items()):
        lines.append(f"{_table_column(crossing.forms[i].holder_field)} = "
                     f"{_table_column(crossing.forms[j].holder_field)}:")
        lines.extend(f"- {a} = {b}" for a, b in kept)
    return lines


def question_with_pairs(question: str, crossing: Crossing, pairs: Pairs) -> str:
    """The delegated question with the pairs block after it."""
    block = [QUESTION_HEADER.format(peers=", ".join(pairs.peers)),
             *pair_lines(crossing, pairs), QUESTION_FOOTER]
    return f"{str(question or '').rstrip()}\n\n" + "\n".join(block)


def note(crossing: Crossing, pairs: Pairs, rounds: int) -> str:
    """The fixed engine note a crossed delegation's result carries."""
    words = {"holder": crossing.holder, "concept": crossing.concept,
             "peers": ", ".join(pairs.peers), "times": "once" if rounds == 1 else f"{rounds} times"}
    return "\n".join([f"{NOTE_PREFIX} {_CROSSED.format(**words)}", *pair_lines(crossing, pairs)])


def is_error(result) -> bool:
    return not isinstance(result, dict) or result.get("status") == "error"


def with_note(result, text: str) -> dict:
    """A copy of ``result`` with ``text`` after its last text block and after
    each string member of ``structuredContent``. Never mutates ``result``."""
    content = list(result.get("content") or [])
    last = max((i for i, b in enumerate(content)
                if isinstance(b, dict) and isinstance(b.get("text"), str)), default=None)
    if last is None:
        content.append({"text": text})
    else:
        content[last] = {**content[last], "text": f"{content[last]['text'].rstrip()}\n\n{text}"}
    out = {**result, "content": content}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        out["structuredContent"] = {k: f"{v.rstrip()}\n\n{text}" if isinstance(v, str) else v
                                    for k, v in structured.items()}
    return out


# --------------------------------------------------------------------------- #
# The turn's budget, one delegation's calls, the crossing
# --------------------------------------------------------------------------- #
@dataclass
class TurnCrossing:
    """One turn's facts, its time and call budgets, and the lines read per holder."""
    facts: object
    started: float = field(default_factory=lambda: _monotonic())
    rounds: int = 0
    calls: int = 0
    lines: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def crossings(self, holder: str) -> list:
        with self.lock:
            if holder not in self.lines:
                self.lines[holder] = crossings_for(self.facts, holder)
            return self.lines[holder]

    def left(self) -> float:
        """Seconds before the turn's crossing deadline."""
        return self.started + TURN_BUDGET_SECONDS - ANSWER_RESERVE_SECONDS - _monotonic()

    def start_round(self, calls: int) -> bool:
        """Spend one round and ``calls`` pairing calls when the turn's caps
        and remaining time allow; False (nothing spent) otherwise."""
        with self.lock:
            if (self.rounds >= MAX_ROUNDS_PER_TURN or calls > MAX_PAIRING_CALLS_PER_ROUND
                    or self.calls + calls > MAX_PAIRING_CALLS_PER_TURN
                    or self.left() < PAIRING_SECONDS + REASK_MIN_SECONDS):
                return False
            self.rounds += 1
            self.calls += calls
            return True


@dataclass
class Call:
    """One delegation: what was sent, the client call that sends it again
    (``send``) and the one below the guards and the turn's context that asks
    a pairing system (``read``)."""
    tool_use_id: object
    name: str
    arguments: dict
    read_timeout_seconds: object
    meta: object
    send: object
    read: object

    def question(self) -> str:
        value = self.arguments.get("question") if isinstance(self.arguments, dict) else None
        return value if isinstance(value, str) else ""

    async def _tracked(self, call, tool_use_id, name, arguments, read_timeout, seconds: float):
        from .call_delivery import CallDelivery, outcome, tracking
        with tracking() as delivery:
            result = await asyncio.wait_for(
                call(tool_use_id, name, arguments, read_timeout, self.meta), timeout=seconds)
        return None if outcome(delivery) is not CallDelivery.SENT or is_error(result) else result

    async def ask_again(self, question: str, seconds: float):
        """The holder's answer to the re-ask (the delegation's own tool use
        id, so its trace is the one the delegation shows), or None."""
        try:
            return await self._tracked(self.send, self.tool_use_id, self.name,
                                       {**self.arguments, "question": question},
                                       self.read_timeout_seconds, seconds)
        except asyncio.TimeoutError:
            return None

    async def ask_pairs(self, sent: list, seconds: float) -> list | None:
        """Every request of ``sent`` at once (each under its own tool use id),
        each answer or None; None when the reads together did not finish in
        ``seconds``."""
        calls = [self._tracked(self.read, f"{self.tool_use_id or 'call'}-pair-{n}",
                               tool_name(peer), {"question": PAIRING_QUESTION, RESERVED_ARG: raw},
                               timedelta(seconds=seconds), seconds)
                 for n, (peer, _key, raw) in enumerate(sent, start=1)]
        try:
            return list(await asyncio.wait_for(asyncio.gather(*calls), timeout=seconds))
        except asyncio.TimeoutError:
            return None


def tool_name(system_id: str) -> str:
    """The gateway delegation tool of ``system_id`` (the target name the
    gateway target is created under, ``___query_`` and the runtime's id)."""
    return f"{system_id.replace('_', '-')[:100]}___query_{system_id.replace('-', '_')}"


def _next(turn: TurnCrossing, holder: str, text: str, question: str, chosen, asked: set):
    """``(crossing, values)`` of the first line the answer needs crossed (the
    chosen line only, after the first round), else None."""
    for crossing in ([chosen] if chosen else turn.crossings(holder)):
        found = need(text, question, crossing, asked)
        if found:
            return crossing, found
    return None


async def _round(turn: TurnCrossing, call: Call, crossing: Crossing, found: dict,
                 pairs: Pairs, asked: set):
    """One round: ``"skip"`` when it cannot start, ``"void"`` when it started
    and did not finish complete, else the holder's answer to the re-ask."""
    sent = requests(crossing, found, turn.facts.join_paths)
    if not sent or not turn.start_round(len(sent)):
        return "skip"
    for values in found.values():
        asked.update(v.casefold() for v in values)
    replies = await call.ask_pairs(sent, min(PAIRING_SECONDS, turn.left()))
    if replies is None or not pairs.add_round(crossing, found, sent, replies):
        return "void"
    answer = await call.ask_again(question_with_pairs(call.question(), crossing, pairs),
                                  max(turn.left(), 0.0))
    return "void" if answer is None else answer


async def cross(turn: TurnCrossing, call: Call, result):
    """``result`` itself, or the holder's answer to the last re-ask with the
    engine note (see the module docstring). Never raises a guard fault."""
    from .route_facts import target_of
    holder = target_of(call.name, getattr(turn.facts, "systems", ()))
    if holder is None or is_error(result) or not turn.crossings(holder):
        return result
    current, chosen, pairs, asked, rounds = result, None, Pairs(), set(), 0
    for _round_no in range(MAX_ROUNDS):
        step = _next(turn, holder, result_text(current), call.question(), chosen, asked)
        if step is None:
            break
        chosen, found = step
        outcome = await _round(turn, call, chosen, found, pairs, asked)
        logger.info("FORM_CROSSING holder=%s round=%d values=%d pairs=%d outcome=%s", holder,
                    rounds + 1, sum(map(len, found.values())),
                    sum(map(len, pairs.items.values())),
                    outcome if isinstance(outcome, str) else "answered")
        if outcome == "skip":
            break
        if outcome == "void":
            return result
        current, rounds = outcome, rounds + 1
    return result if rounds == 0 else with_note(current, note(chosen, pairs, rounds))
