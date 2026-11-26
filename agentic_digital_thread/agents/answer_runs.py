# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Rebuild a delivered answer from the orchestrator's text runs.

The orchestrator streams its text in RUNS separated by tool calls. A run
before a later tool call is usually the model narrating its next step
("Now I need to find all other batches ..."), not the answer. The web chat
rebuilds the answer from the runs (``frontend/src/chat/answer-segments.ts``
over ``selectFinalAnswer`` in ``frontend/src/lib/analysis-util.js``): the
LONGEST run plus every run after it, then any chart tag streamed in a
discarded run put back above the answer, then the last ``---FOLLOWUPS---``
block put back when the kept text has none.

Every server-side fold (the async ask the Quick query bridge polls,
``/api/ask/sync`` for the Quick Lambda proxy, scheduled prompts) joined
every text token instead, so Quick showed the narration glued onto the
answer (2026-10-14 rehearsal: "... trace which customers received those
batches.## Recall Picture"). This module is the server copy of the chat's
rule so both surfaces deliver the same text. :func:`select_final_answer`
is pinned to the JavaScript helper through node in
``tests/unit/test_answer_runs.py``.

The answer is chosen from the runs after the last tool call that READ data
(every tool but a write-back, :func:`is_write_back_tool`): text written
before such a call was the model planning it, however long, so a short
answer after a long planning run is delivered on its own. A write-back call
(a correlation, a value mapping or a metric definition proposal) comes after
the answer, so it does not end it and the short epilogue after it is kept.
Within those runs the longest-run rule applies, as in the chat, and when no
text follows the last read, every run is a candidate. Both rules are pinned
to the chat's ``analysis-util.js`` through node.

Runs are split only at tool calls: no text is matched or scrubbed, so an
answer with no tool call inside its text is returned unchanged apart from
the outer trim the chat also applies. Stdlib only.
"""
from __future__ import annotations

import re
from typing import Iterable

#: Same pattern as ``CHART_TAG_RE`` in ``frontend/src/chat/answer-segments.ts``.
_CHART_TAG_RE = re.compile(r"<chart-block\b[^>]*/>")


#: Same list as ``WRITE_BACK_TOOLS`` in ``frontend/src/lib/analysis-util.js``.
WRITE_BACK_TOOLS = frozenset({"save_correlation", "save_value_mapping",
                              "propose_metric_definition"})


def is_write_back_tool(name) -> bool:
    """True for a write-back tool, bare or gateway-prefixed (``target___name``),
    mirror of ``isWriteBackTool``."""
    tool = "" if name is None else str(name)
    cut = tool.rfind("___")
    if cut != -1:
        tool = tool[cut + 3:]
    return tool in WRITE_BACK_TOOLS


def select_final_answer(runs: Iterable[str], start: int = 0) -> str:
    """The longest non-blank run plus every run after it, joined by a blank
    line and trimmed; the first of equally long runs wins (mirror of
    ``selectFinalAnswer``). Only runs from index ``start`` on are candidates;
    when none of them has text, every run is."""
    raw = ["" if run is None else str(run) for run in runs or []]
    first = (int(start) if isinstance(start, (int, float))
             and not isinstance(start, bool) and start > 0 else 0)
    kept = [run for run in raw[first:] if run.strip()]
    if not kept:
        kept = [run for run in raw if run.strip()]
    if not kept:
        return ""
    longest = 0
    for index, run in enumerate(kept):
        if len(run) > len(kept[longest]):
            longest = index
    return "\n\n".join(kept[longest:]).strip()


def _followups_start(text: str) -> "int | None":
    """Where the first FOLLOWUPS marker region starts, the leading newline
    excluded (``findFollowupsMarker``); None when there is no marker."""
    from .eval_core import _FOLLOWUPS_RE
    match = _FOLLOWUPS_RE.search(text or "")
    if not match:
        return None
    return match.start() + (1 if match.group(0).startswith("\n") else 0)


class AnswerRuns:
    """Accumulate one turn's text runs (mirror of ``AnswerSegments``)."""

    def __init__(self) -> None:
        self._runs: list[str] = []
        self._current = ""
        self._charts: list[str] = []
        self._answer_start = 0

    def push_text(self, text: str) -> None:
        """Extend the current run; harvest complete chart tags as they land
        (matched over the whole run, so a tag split across chunks counts)."""
        self._current += text or ""
        if "<chart-block" in self._current:
            for tag in _CHART_TAG_RE.findall(self._current):
                if tag not in self._charts:
                    self._charts.append(tag)

    def tool_boundary(self, tool_name: "str | None" = None) -> None:
        """A tool call ends the current run (kept as a candidate). A call
        that reads data (any named tool but a write-back) moves the answer
        start past every run so far; a boundary without a tool name keeps
        the longest-run rule over all runs."""
        if self._current.strip():
            self._runs.append(self._current)
        self._current = ""
        if tool_name and not is_write_back_tool(tool_name):
            self._answer_start = len(self._runs)

    def finalize(self) -> str:
        """The delivered answer: selection, then FOLLOWUPS, then charts."""
        self.tool_boundary()
        return self._with_charts(self._with_followups(
            select_final_answer(self._runs, self._answer_start)))

    def _with_followups(self, answer: str) -> str:
        if _followups_start(answer) is not None:
            return answer
        for run in reversed(self._runs):
            start = _followups_start(run)
            if start is not None:
                return f"{answer}\n\n{run[start:]}"
        return answer

    def _with_charts(self, answer: str) -> str:
        """Prepend harvested tags the answer lacks, in stream order. A tag
        is the same chart only when byte-identical (the chat's title plus
        content-hash fingerprint), so same-title charts over different
        windows both stay."""
        present = set(_CHART_TAG_RE.findall(answer))
        missing = [tag for tag in self._charts if tag not in present]
        if not missing:
            return answer
        return "\n\n".join(missing) + "\n\n" + answer


def answer_text(events: Iterable) -> str:
    """Fold a chat-client event stream into the delivered answer: text from
    ``MessageEvent``, a run boundary at every ``ToolUseEvent``."""
    from .events import MessageEvent, ToolUseEvent
    runs = AnswerRuns()
    for event in events:
        if isinstance(event, MessageEvent):
            runs.push_text(event.content)
        elif isinstance(event, ToolUseEvent):
            runs.tool_boundary(event.tool_name)
    return runs.finalize()
