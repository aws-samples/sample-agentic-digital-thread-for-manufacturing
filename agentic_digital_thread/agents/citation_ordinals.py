# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deterministic citation numbers for the orchestrator's tool results.

The chat client numbers the Sources chips by COUNTING tool calls in stream
order: one number per ``tool_start`` whose tool is not the bare
``explore_graph`` (provenance contract D2, mirrored by
``agents.provenance.citation_ordinals``). The prompt, on the other hand,
asked the model to number "each system agent you call" itself, so the two
counters drifted whenever a turn re-queried a system, called a non-system
tool (a metric-definition proposal, a graph-target tool), or ran tool calls
in parallel: the answer's ``[3]`` then pointed at a different chip than the
Sources row labelled ``[3]``.

``CitationOrdinalHook`` closes that gap without touching the client
contract: it runs the SAME counting predicate over the toolUse blocks the
model emits, in message order, and stamps every toolResult the model reads
with the number its Sources chip will carry. The model no longer has to
count; it copies the number printed on the evidence it cites. Pure
bookkeeping: no system, question, or dataset content is read or written.

Earlier questions (test event 32, 2026-10-15): a follow-up answered from a
result an EARLIER turn returned had no chip, and the answer copied that
turn's citation numbers ("[2][5]"), which stayed literal text. Chat memory
stores a tool result before this hook stamps it, so the rehydrated copy has
no number at all, and the numbers restart every turn anyway. At the start of
each invocation the hook therefore labels the most recent earlier cited
results ``[E1]``..``[E<k>]`` (``restamp_earlier_results``), replacing any
stamp an in-memory copy still carries; the runtime sends the same labels to
the client (``earlier_sources``) so they show as Sources chips. Labels are
written into the live conversation only; stored memory events are never
rewritten.

Earlier answers (live walk, 2026-10-28): a correlation follow-up made no
system call and answered from the two results the questions before it had
returned, labelled ``[E1]`` and ``[E2]``. Two of three runs cited them as
``[E1] [E2]``; the third wrote ``[1][2]``, which no Sources chip carries,
so the answer had no Sources row. The earlier answers the model reads beside
those results each cited their own result as ``[1]``: a number that meant
one thing in its own turn and nothing (or this turn's first chip) now.

Rewriting those markers to the current labels needs the binding each answer
was written under, and it is not stored (reviews 2026-10-29): an ``[E<j>]``
an earlier answer wrote named the labels of ITS turn, which moved since
whenever the 8-result cap, a window cut or an "Ask again" read's dropped
turns shifted them, so a rewritten or kept label can name a different result
now. ``strip_earlier_answers`` therefore REMOVES the citation markers from
the earlier answers of the live conversation, and the model cites only from
the labels the results carry now. A citation marker is a group the client's
grammar finds (``citation_markers``, pinned to the client by a shared case
list, which never finds an index such as ``sensor[1]`` or a footnote line
``[1]: ...``): any group holding an ``[E<j>]`` member (that form is only ever
a citation), and a group of numbers when every number is one its turn gave
out, rebuilt by counting that turn's stored calls the way this hook numbered
them. Every other bracket is left as written: ``[12]`` when the turn gave
out no 12, an interval ``[4, 7]`` beyond it, code, chart tags, links. The
text before the window's first question is a turn the window cut mid-way,
whose first calls (and so its count) are gone: there a number counts only
when it is at most the calls still visible, so a year ``[2024]`` or an
interval past them stays. Like the result labels, this is the live
conversation only: stored events, the answers the reader saw and this
turn's answer are never touched.

Retrieved long-term memory (review 2026-10-29): ``_inject_ltm_into_user_turn``
in the orchestrator moves the ``<user_context>`` block into this turn's
user message, where no earlier answer is read, and a summary there can carry
an old ``[1]`` or ``[E1]``. ``without_memory_citations`` gives the model's
copy no linkable marker: earlier labels go, and a numeric group up to
``MAX_TURN_CITATIONS`` keeps its numbers in fullwidth brackets, because with
no turn to bind it to it may be a value; stored records are not changed.

The response-integrity gate's structured answer (``FinalAnswerDraft``, the
name ``memory_turns`` reloads turns by) is never announced to the client, so
it gets no number and no earlier label.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from strands.hooks import BeforeInvocationEvent, HookProvider, HookRegistry, MessageAddedEvent

from .citation_markers import MarkerGroup, marker_groups
from .history_turns import starts_turn
from .memory_turns import STRUCTURED_ANSWER_TOOL
from .provenance import MAX_PROVENANCE_RECORDS, MAX_TOOL_NAME_LEN, is_client_cited

logger = logging.getLogger(__name__)

#: Printed at the end of every cited tool result. The word "engine" marks
#: the line as deterministic bookkeeping, like the result-cut notice.
CITATION_STAMP_PREFIX = "[engine] Cite this result as "

#: Printed at the end of a cited tool result an EARLIER question in this chat
#: returned (see the module docstring).
EARLIER_STAMP_PREFIX = "[engine] Returned for an earlier question in this chat. Cite it as "

#: How many earlier cited results get a label: the most recent ones. Older
#: ones lose any stale stamp and get none.
MAX_EARLIER_RESULTS = 8

#: The highest number read as a citation in text whose turn is not known
#: (retrieved memory): the per-turn cap on citation records
#: (``provenance.MAX_PROVENANCE_RECORDS``). A larger number, a year such as
#: ``[2024]`` for one, is left as written.
MAX_TURN_CITATIONS = MAX_PROVENANCE_RECORDS

#: Either stamp, exactly as written above (code-authored text only).
_STAMP_RE = re.compile(
    r"\[engine\] (?:Cite this result as \[\d{1,4}\]"
    r"|Returned for an earlier question in this chat\. Cite it as \[E\d{1,2}\])\.")


def citation_stamp(ordinal: int) -> str:
    """The exact line stamped onto a cited tool result."""
    return f"{CITATION_STAMP_PREFIX}[{ordinal}]."


def earlier_stamp(index: int) -> str:
    """The exact line stamped onto the ``index``-th earlier cited result."""
    return f"{EARLIER_STAMP_PREFIX}[E{index}]."


def _is_cited(name: str) -> bool:
    """A call the client numbers a Sources chip for: client-cited
    (``provenance.is_client_cited``) and not the gate's structured answer,
    which the stream never announces."""
    return is_client_cited(name) and name != STRUCTURED_ANSWER_TOOL


def assign_ordinals(tool_uses: list[dict], counter: int) -> tuple[dict[str, int], int]:
    """Number the client-cited toolUse blocks of ONE assistant message.

    Returns ``(toolUseId -> ordinal, next counter)``. Blocks whose tool is
    not client-cited (``_is_cited``) keep no number and do not advance the
    counter, which is exactly what the client's chip counter does.
    """
    ordinals: dict[str, int] = {}
    for use in tool_uses:
        name = str(use.get("name") or "")[:MAX_TOOL_NAME_LEN]
        if not _is_cited(name):
            continue
        counter += 1
        tool_id = use.get("toolUseId")
        if isinstance(tool_id, str) and tool_id:
            ordinals[tool_id] = counter
    return ordinals, counter


def stamp_tool_result(tool_result: dict, ordinal: int) -> bool:
    """Append the citation stamp to one toolResult's model-visible content.

    String content (gateway MCP results) gets the line appended; list
    content gets one more ``{"text": ...}`` part. Any other shape is left
    alone. Returns True when a stamp was written.
    """
    return _append_line(tool_result, citation_stamp(ordinal))


def _append_line(tool_result: dict, line: str) -> bool:
    """Append ``line`` to a toolResult's string or list content (see
    ``stamp_tool_result``); any other shape is left alone."""
    content = tool_result.get("content")
    if isinstance(content, str):
        tool_result["content"] = f"{content.rstrip()}\n\n{line}" if content.strip() else line
        return True
    if isinstance(content, list):
        content.append({"text": line})
        return True
    return False


def _without_trailing_stamps(text: str) -> str:
    """``text`` without the stamps at its end (linear: one ``rfind`` per stamp)."""
    body = text.rstrip()
    while True:
        start = body.rfind("[engine] ")
        if start < 0 or not _STAMP_RE.fullmatch(body[start:]):
            return body
        body = body[:start].rstrip()


def _strip_stamps(tool_result: dict) -> None:
    """Remove the citation or earlier stamps a toolResult already carries."""
    content = tool_result.get("content")
    if isinstance(content, str):
        tool_result["content"] = _without_trailing_stamps(content)
    elif isinstance(content, list):
        content[:] = [part for part in content
                      if not (isinstance(part, dict) and isinstance(part.get("text"), str)
                              and _STAMP_RE.fullmatch(part["text"].strip()))]


def _blocks(message: Any, key: str) -> list[dict]:
    """The ``key``-bearing content blocks of a Strands message, unwrapped."""
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b[key] for b in content
            if isinstance(b, dict) and isinstance(b.get(key), dict)]


def _cited_results(messages: Any) -> list[tuple[str, dict]]:
    """``(tool name, toolResult)`` for every client-cited call in ``messages``
    that has a result with stampable (string or list) content, in call order.
    A result whose call is outside the window is skipped (no name to cite)."""
    names: dict[str, str] = {}
    results: dict[str, dict] = {}
    for message in messages if isinstance(messages, list) else ():
        for use in _blocks(message, "toolUse"):
            name = str(use.get("name") or "")[:MAX_TOOL_NAME_LEN]
            tool_id = use.get("toolUseId")
            if _is_cited(name) and isinstance(tool_id, str) and tool_id:
                names.setdefault(tool_id, name)
        for result in _blocks(message, "toolResult"):
            tool_id = result.get("toolUseId")
            if (isinstance(tool_id, str) and tool_id in names
                    and isinstance(result.get("content"), (str, list))):
                results.setdefault(tool_id, result)
    return [(name, results[tool_id]) for tool_id, name in names.items() if tool_id in results]


def earlier_sources(messages: Any) -> list[dict]:
    """``[{"label": "E<k>", "tool": name}]`` for the earlier cited results
    ``restamp_earlier_results`` labels in the same ``messages`` (names only,
    at most ``MAX_EARLIER_RESULTS``). ``[]`` on any fault."""
    try:
        recent = _cited_results(messages)[-MAX_EARLIER_RESULTS:]
        return [{"label": f"E{k}", "tool": name} for k, (name, _) in enumerate(recent, start=1)]
    except Exception:  # nosec B110 - bookkeeping only; no label beats a broken turn
        return []


def restamp_earlier_results(messages: Any) -> int:
    """Label the earlier cited results in ``messages`` (in place).

    Every earlier cited result loses the stamps it carries; the most recent
    ``MAX_EARLIER_RESULTS`` then get ``earlier_stamp(k)``, numbered in call
    order. Returns how many were labelled.
    """
    return len(_restamp(messages))


def _restamp(messages: Any) -> dict[str, str]:
    """``restamp_earlier_results``; returns ``toolUseId -> "E<k>"`` for the
    results it labelled."""
    cited = _cited_results(messages)
    for _name, result in cited:
        _strip_stamps(result)
    labels: dict[str, str] = {}
    for k, (_name, result) in enumerate(cited[-MAX_EARLIER_RESULTS:], start=1):
        _append_line(result, earlier_stamp(k))
        labels[result["toolUseId"]] = f"E{k}"
    return labels


def _turns(messages: Any) -> list[list[dict]]:
    """``messages`` split into turns at each question (``starts_turn``, the
    rule the window and the history read use). The first entry holds the
    messages before the first question: a turn the window cut, whose first
    calls are gone, so only its visible calls can be counted."""
    turns: list[list[dict]] = [[]]
    for message in messages if isinstance(messages, list) else ():
        if starts_turn(message):
            turns.append([])
        elif isinstance(message, dict):
            turns[-1].append(message)
    return turns


def _turn_ordinals(turn: list[dict]) -> tuple[dict[str, int], int]:
    """``(toolUseId -> ordinal, how many numbers the turn gave out)`` for one
    earlier turn, numbered as ``CitationOrdinalHook`` numbered it live (the
    numbers its answer cites and the client linked). The window only ever
    cuts from the front, so every turn holds all its calls except the one it
    cut into (``_turns``), whose count is then its visible calls: a lower
    bound, past which no number is read as a citation."""
    ordinals: dict[str, int] = {}
    counter = 0
    for message in turn:
        if message.get("role") == "assistant":
            assigned, counter = assign_ordinals(_blocks(message, "toolUse"), counter)
            ordinals.update(assigned)
    return ordinals, counter


def _is_citation(group: MarkerGroup, given_out: int) -> bool:
    """True when ``group`` is a citation marker of a turn that gave out
    ``given_out`` numbers: it holds an earlier label, or every member is a
    number from 1 to ``given_out``."""
    if any(member.earlier for member in group.members):
        return True
    return all(member.canonical and 1 <= member.number <= given_out
               for member in group.members)


def _removal_spans(text: str, given_out: int) -> list[tuple[int, int]]:
    """``(start, end)`` of each run of adjacent citation markers in ``text``
    (``_is_citation``), widened by one adjoining space so the words around it
    stay one space apart: the space before it when punctuation, a space or
    the end follows, else the space after it at the start of a line."""
    runs: list[list[int]] = []
    for group in marker_groups(text):
        if not _is_citation(group, given_out):
            continue
        if runs and runs[-1][1] == group.start:
            runs[-1][1] = group.end
        else:
            runs.append([group.start, group.end])
    spans = []
    for start, end in runs:
        after = text[end:end + 1]
        if start and text[start - 1] == " " and not (after.isalnum() or after == "["):
            start -= 1
        elif (not start or text[start - 1] == "\n") and after == " ":
            end += 1
        spans.append((start, end))
    return spans


def without_citations(text: str, given_out: int) -> str:
    """An earlier answer's ``text`` with every citation marker removed
    (``_is_citation``; ``given_out`` is how many numbers its turn gave out).
    Every other bracket is left as written."""
    out, last = [], 0
    for start, end in _removal_spans(text, given_out):
        out.append(text[last:start])
        last = end
    out.append(text[last:])
    return "".join(out)


def strip_earlier_answers(messages: Any) -> list[tuple[dict, str, str]]:
    """Remove the citation markers of the earlier answers in ``messages`` (in
    place; see the module docstring). Returns ``(text block, original,
    written)`` for every block it changed, so a caller can put the original
    back."""
    changed: list[tuple[dict, str, str]] = []
    for turn in _turns(messages):
        # In turn 0, the turn the window cut into, this counts the calls
        # still visible (``_turn_ordinals``).
        given_out = _turn_ordinals(turn)[1]
        for message in turn:
            if message.get("role") != "assistant":
                continue
            for block in message.get("content") or ():
                text = block.get("text") if isinstance(block, dict) else None
                if not isinstance(text, str) or "[" not in text:
                    continue
                written = without_citations(text, given_out)
                if written != text:
                    block["text"] = written
                    changed.append((block, text, written))
    return changed


def without_memory_citations(text: Any) -> Any:
    """Retrieved memory ``text`` as the model reads it. Recalled memory has no
    binding to any turn's citations, so a numeric group in it may be a value
    (a tolerance ``[3, 5]``, a step ``[1]``). Every earlier label goes, alone
    or in a group, as in an earlier answer; every other free-standing number
    or group up to ``MAX_TURN_CITATIONS`` keeps its numbers, spelled in
    fullwidth brackets (``\uff3b3, 5\uff3d``). The client's grammar reads only
    ASCII brackets, so it never links that spelling, and its closing bracket is
    no attached character, so a real marker written right after it still
    links (review 2026-10-29: a spaced ``[ 3, 5 ]`` kept an ASCII ``]`` that
    refused it). The client's refusals apply (an index, a footnote line, code
    and chart tags stay as written). Anything but a string is returned as it
    is."""
    if not isinstance(text, str) or "[" not in text:
        return text
    text = without_citations(text, 0)
    out, last = [], 0
    for group in marker_groups(text):
        if _is_citation(group, MAX_TURN_CITATIONS):
            out.append(text[last:group.start])
            out.append(_RECALLED_OPEN + text[group.start + 1:group.end - 1] + _RECALLED_CLOSE)
            last = group.end
    out.append(text[last:])
    return "".join(out)


#: Fullwidth square brackets, the spelling of a recalled numeric group.
_RECALLED_OPEN, _RECALLED_CLOSE = "\uff3b", "\uff3d"


def _is_output_cap_continuation(messages: Any) -> bool:
    """True when an invocation's new input is the output-cap recovery notice
    (``output_cap_recovery``): the same turn continues, so the results
    already in the conversation include this turn's own."""
    from .output_cap_recovery import RECOVERY_NOTICE_PREFIX
    for message in messages if isinstance(messages, list) else ():
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and text.lstrip().startswith(RECOVERY_NOTICE_PREFIX):
                return True
    return False


class CitationOrdinalHook(HookProvider):
    """Stamp each cited toolResult with the number its Sources chip carries.

    The counter restarts on every invocation that starts a new question (the
    client counts per turn) and carries over into the one output-cap
    continuation of the same turn. The whole handler is fail-soft: a
    bookkeeping bug may never break the agent loop, the worst case is an
    unstamped result. At the start of a new question the conversation's
    earlier cited results are labelled ``[E<k>]`` and the earlier answers'
    citation markers are removed (the history is already loaded: the session
    manager restores it when the Agent is built, before any invocation). An
    agent kept across questions (the local singleton) gets its earlier
    answers' own text back before each removal, so each starts from what the
    answer wrote.
    """

    def __init__(self) -> None:
        self._counter = 0
        self._ordinals: dict[str, int] = {}
        self._stripped: list[tuple[dict, str, str]] = []

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_invocation_start)
        registry.add_callback(MessageAddedEvent, self._on_message_added)

    def _on_invocation_start(self, event) -> None:
        try:
            continuation = _is_output_cap_continuation(getattr(event, "messages", None))
        except Exception:  # nosec B110 - treat an unreadable input as a new question
            continuation = False
        if continuation:
            # The same turn goes on (output_cap_recovery): the client keeps
            # one chip counter per turn, so the count and the ordinals this
            # turn already assigned carry over, and nothing is relabelled
            # or removed.
            return
        self._counter = 0
        self._ordinals = {}
        try:
            messages = getattr(getattr(event, "agent", None), "messages", None)
            self._restore_answers()
            labels = _restamp(messages)
            self._stripped = strip_earlier_answers(messages)
            if labels or self._stripped:
                logger.debug("CITATION_ORDINALS: labelled %d earlier result(s), "
                             "%d earlier answer block(s) without markers",
                             len(labels), len(self._stripped))
        except Exception:  # nosec B110 - earlier labels are best-effort
            logger.exception("CITATION_ORDINALS earlier labels failed; history left intact")

    def _restore_answers(self) -> None:
        """Put back the text of every earlier-answer block the last removal
        changed and nothing has changed since."""
        for block, original, written in self._stripped:
            if block.get("text") == written:
                block["text"] = original
        self._stripped = []

    def _on_message_added(self, event: MessageAddedEvent) -> None:
        try:
            uses = _blocks(event.message, "toolUse")
            if uses:
                assigned, self._counter = assign_ordinals(uses, self._counter)
                self._ordinals.update(assigned)
                return
            stamped = 0
            for result in _blocks(event.message, "toolResult"):
                ordinal = self._ordinals.get(result.get("toolUseId"))
                if ordinal is not None and stamp_tool_result(result, ordinal):
                    stamped += 1
            if stamped:
                logger.debug("CITATION_ORDINALS: stamped %d tool result(s)", stamped)
        except Exception:  # nosec B110 - citation stamping is best-effort
            logger.exception("CITATION_ORDINALS failed; message left intact")
