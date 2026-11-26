# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Put the engine's provenance sentence into the orchestrator's answer stream.

``runtime_app._chart_injecting_build`` wraps every orchestrator Agent's
``stream_async`` with :func:`wrap_stream` (outside the gate-frozen legacy
loop, beside the deterministic chart injection). :class:`ProvenanceStream`
watches one turn's events:

* tool results: the facts their text carries (``answer_provenance``), each
  bound to that result's citation ordinal (its trailing stamp, else the
  client's own count of cited tool calls, kept here across an output-cap
  continuation) and its system; population facts (``[engine] Filtered
  rows:`` lines) are capped apart from the other kinds, so they never
  crowd one out;
* the answer: text after the last data-reading tool call (a write-back call
  such as a proposal does not end the answer, as in ``answer_runs``);
* the end of the answer (the model's reply with no tool call, or a
  write-back call): when the answer states a tagged figure where it
  attributes it to that fact's source (``answer_provenance.attributed_figures``),
  the provenance sentence is emitted as a plain ``{"data": ...}`` event.

The answer's control blocks (a ``---FOLLOWUPS---`` block, a proposal block)
come last and the chat, the async ask and the eval lanes all cut or strip
them, so the sentence goes BEFORE the first one. Only while a turn has a fact
does the wrapper hold text back: whole lines are released as they complete;
from a control-block line on, the text waits for the end of the answer. A
turn without a fact passes every event through unchanged.

Scope: the report-only and annotate modes only. Under
``RESPONSE_INTEGRITY_MODE=enforce`` the stream is returned unwrapped, because
the answer there is the server-built typed envelope, so no provenance
sentence is added.
"""
from __future__ import annotations

import logging
import re

from . import answer_provenance as provenance

logger = logging.getLogger(__name__)

#: A line that is only a rule or blank: it may sit above a FOLLOWUPS label.
_RULE_OR_BLANK = re.compile(r"[ \t]*(?:(?:-{3,}|\*{3,}|_{3,})[ \t]*)?\r?")


def _control_block_line(line: str) -> bool:
    """True for a line that starts a FOLLOWUPS block or a proposal block
    (the same tolerant matchers the finalizers use)."""
    from .eval_core import _FOLLOWUPS_RE
    from .public_answer import _PROPOSAL_OPENED
    return bool(_FOLLOWUPS_RE.search(line) or _PROPOSAL_OPENED.search(line))


def split_for_release(pending: str, *, final: bool = False) -> tuple[str, str, bool]:
    """``(text safe to release, text kept, a control block starts in kept)``.

    Complete lines are released up to a control-block line; rule and blank
    lines directly above one stay with it (a ``---`` rule above a FOLLOWUPS
    label is part of the marker). ``final`` treats the unterminated last line
    as complete."""
    lines = pending.split("\n")
    complete, rest = (lines, "") if final else (lines[:-1], lines[-1])
    released, tentative = 0, None
    for index, line in enumerate(complete):
        if _control_block_line(line):
            start = index if tentative is None else tentative
            return "".join(f"{x}\n" for x in complete[:start]), _join_from(lines, start), True
        if _RULE_OR_BLANK.fullmatch(line):
            tentative = index if tentative is None else tentative
            continue
        tentative, released = None, index + 1
    if final and tentative is None:
        return pending, "", False
    return "".join(f"{x}\n" for x in complete[:released]), _join_from(lines, released), False


def _join_from(lines: list[str], start: int) -> str:
    return "\n".join(lines[start:])


class ProvenanceStream:
    """One turn's provenance state (see the module docstring)."""

    def __init__(self) -> None:
        self.facts: list[dict] = []
        self.tool_names: dict[str, str] = {}
        self.systems: set[str] = set()       # systems this turn's tools belong to
        self.ordinals: dict[str, int] = {}   # tool use id -> the client's count
        self.ordinal_systems: dict[int, str] = {}  # cited ordinal -> its system
        self.counter = 0
        self.body = ""          # this answer's released text
        self.pending = ""       # text not yet released
        self.held = False       # ``pending`` starts at a control block
        self.tail = ""          # the last text yielded in this answer
        self.done = False       # the sentence was emitted this turn
        self.ended = False      # the terminal result event went by

    # -- events ------------------------------------------------------------ #
    def feed(self, event) -> list:
        if not isinstance(event, dict):
            return [event]
        self._learn(event)
        text = event.get("data")
        if isinstance(text, str) and "current_tool_use" not in event:
            if (not self.facts or self.done) and not self.pending:
                self._note_text(text)
                return [event]
            self.pending += text
            return self._release()
        current = event.get("current_tool_use")
        if isinstance(current, dict) and current.get("name"):
            return self._tool_boundary(str(current["name"])) + [event]
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            uses = _blocks(message, "toolUse")
            if not uses:
                return self.finish(final=True) + [event]
            return self._tool_boundary(str(uses[-1].get("name") or "")) + [event]
        if "result" in event:
            self.ended = True
            return self.finish(final=True) + [event]
        return [event]

    def finish(self, *, final: bool) -> list:
        """Release everything held; at the end of the answer, with the
        provenance sentence before the control blocks."""
        if not self.pending and not (final and self.facts and not self.done):
            return []
        release, kept, _marker = (("", self.pending, True) if self.held
                                  else split_for_release(self.pending, final=True))
        self.body += release
        sentence = self._sentence() if final else ""
        out = release
        if sentence:
            out += _joiner(self.tail + release) + sentence
            if kept:
                out += "\n" if kept.startswith("\n") else "\n\n"
        out += kept
        self.pending, self.held = "", False
        return [self._emit(out)] if out else []

    # -- internals --------------------------------------------------------- #
    def _sentence(self) -> str:
        if self.done or not self.facts:
            return ""
        lines = provenance.final_lines(self.facts, self.body, self.systems,
                                       self.ordinal_systems)
        if not lines:
            return ""
        self.done = True
        logger.info("ANSWER_PROVENANCE: carried %d provenance sentence(s)", len(lines))
        return "\n".join(lines)

    def _release(self) -> list:
        if self.held:
            return []
        release, self.pending, self.held = split_for_release(self.pending)
        if not release:
            return []
        self.body += release
        return [self._emit(release)]

    def _tool_boundary(self, name: str) -> list:
        from .answer_runs import is_write_back_tool
        if is_write_back_tool(name):
            return self.finish(final=True)
        out = self.finish(final=False)
        self.body, self.tail = "", ""
        return out

    def _emit(self, text: str) -> dict:
        self.tail = (self.tail + text)[-2:]
        return {"data": text}

    def _note_text(self, text: str) -> None:
        self.body += text
        self.tail = (self.tail + text)[-2:]

    def _learn(self, event: dict) -> None:
        current = event.get("current_tool_use")
        if isinstance(current, dict) and current.get("toolUseId") and current.get("name"):
            self.tool_names[str(current["toolUseId"])] = str(current["name"])
        message = event.get("message")
        if not isinstance(message, dict):
            return
        uses = _blocks(message, "toolUse")
        for use in uses:
            if use.get("toolUseId") and use.get("name"):
                self.tool_names[str(use["toolUseId"])] = str(use["name"])
                name = str(use["name"])
                if "___" in name:
                    self.systems.add(name.split("___", 1)[0])
        if message.get("role") == "assistant" and uses:
            # The client's own count, used when a result carries no stamp.
            from .citation_ordinals import assign_ordinals
            assigned, self.counter = assign_ordinals(uses, self.counter)
            self.ordinals.update(assigned)
        if message.get("role") != "user":
            return
        for result in _blocks(message, "toolResult"):
            tool_id = str(result.get("toolUseId") or "")
            name = self.tool_names.get(tool_id, "")
            system = name.split("___", 1)[0] if "___" in name else ""
            text = _result_text(result)
            ordinal = provenance.trailing_ordinal(text) or self.ordinals.get(tool_id)
            if ordinal is not None:
                # Every cited result, so a citation in the answer resolves to its
                # system (an empty system marks a non-system tool: unknown).
                self.ordinal_systems[ordinal] = system
            for fact in provenance.facts_in_tool_text(text, system, ordinal):
                self._keep(fact)

    def _keep(self, fact: dict) -> None:
        """Keep ``fact`` for this turn, at most ``MAX_FACTS`` population
        facts and as many of every other kind, so a call carrying many
        filtered-rows lines never crowds out a split, storage or approximate
        fact."""
        if fact in self.facts:
            return
        population = fact.get("kind") == "population"
        same = sum(1 for kept in self.facts
                   if (kept.get("kind") == "population") == population)
        if same < provenance.MAX_FACTS:
            self.facts.append(fact)


def _joiner(before: str) -> str:
    if not before or before.endswith("\n\n"):
        return ""
    return "\n" if before.endswith("\n") else "\n\n"


def _blocks(message: dict, key: str) -> list[dict]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b[key] for b in content if isinstance(b, dict) and isinstance(b.get(key), dict)]


def _result_text(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(c["text"] for c in content
                   if isinstance(c, dict) and isinstance(c.get("text"), str))


def wrap_stream(stream):
    """``stream`` (an Agent's ``stream_async``) carrying the provenance
    sentence; ``stream`` itself under ``RESPONSE_INTEGRITY_MODE=enforce``.
    One state per wrapped Agent, so an output-cap continuation (a second
    ``stream_async`` call in the same turn) keeps the turn's facts."""
    from .config import response_integrity_enforced
    if response_integrity_enforced():
        return stream
    state = ProvenanceStream()

    async def wrapped(prompt, **kwargs):
        try:
            async for event in stream(prompt, **kwargs):
                for out in _fed(state, event):
                    yield out
        except Exception:
            for out in state.finish(final=False):
                yield out
            raise
        # Nothing is ever yielded past the terminal result event (the
        # synchronous Agent path reads the LAST event's result key).
        if not state.ended:
            for out in state.finish(final=True):
                yield out
    return wrapped


def _fed(state: ProvenanceStream, event) -> list:
    """``state.feed(event)``, fail-soft: a bookkeeping fault passes the event
    through and stops holding text (whatever was held is released first)."""
    try:
        return state.feed(event)
    except Exception as exc:  # noqa: BLE001 - provenance must never break the turn
        logger.warning("ANSWER_PROVENANCE failed (%s); event passed through",
                       type(exc).__name__)
        held, state.pending, state.held, state.facts = state.pending, "", False, []
        return ([{"data": held}] if held else []) + [event]
