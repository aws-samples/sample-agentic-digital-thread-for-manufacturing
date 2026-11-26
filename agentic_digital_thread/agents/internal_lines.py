# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Engine lines for the orchestrator only, kept off every surface a person reads.

A system agent's answer reaches the orchestrator with fixed engine lines
appended (``system_agent._append_engine_facts``). Some of them quote a value
the answer gave that no result of the call returned and tell the orchestrator
not to state it as data: the names line (``answer_names``) and the
identifiers line (``answer_ids``). The orchestrator has to read them. Nobody
else should: the same answer text is also the tool activity summary, the
stored investigation trace, and, when the orchestrator copies a line, part of
the answer a person gets, and on each of those it puts the unverified value
and an instruction meant for a model in front of the reader.

The unpaired-forms line (``unresolved_miss``) is the third: it tells the
orchestrator that figures resting on translating a refused value into another
form were not paired by any result of the call. It is followed by the shape
guard's own IDENTIFIER-MISS blocks, which are not engine lines: a block is the
protocol a system agent writes itself, it names only the value the request
sent and measured shapes, and the public answer and its telemetry already read
blocks (``runtime_app._record_metric_term_miss_signal``). Only the line is
removed; the blocks stay. Route facts alone read a result without its blocks
(``route_facts.without_miss_blocks``): a block names its value under the
protocol's own ``token`` label, not under the field the value came from.

The wrong-form line (``wrong_form_refused``) is the fourth: it names the
concepts of the values the shape guard refused as wrong-form lookups that the
call did not resolve, evidence the orchestrator's decline guard stores on a
decline's record. It names no value, but it is evidence for a guard, not a
fact for a reader. The result-identifiers line (``result_identifiers``) is
the fifth: the identifiers the system's own tool results returned that its
answer states, which the decline guard reads; the delegation wrapper removes
it from the result first, so it is listed here for any surface it reaches
without that wrapper.

:func:`public_text` removes those lines (:data:`MARKERS`) from one text: a
prose answer, or the JSON shapes ``system_agent._with_result_cut_fact``
writes, where the lines ride in the reserved note member. A line counts only
when it starts its own line (spaces or tabs before it allowed) with one of
the markers, so prose that mentions a marker mid-sentence is left alone. The
streamed answer uses the same markers line by line
(``public_answer.InternalTelemetryFilter``).

Other engine lines are not covered: they describe the call's own rows and
carry no value the answer made up, and several are followed by a
FINAL-ANSWER REQUIREMENTS block the orchestrator acts on, so removing the
line alone would leave the block behind.

Stdlib only.
"""
from __future__ import annotations

import json
import re

from .answer_ids import MARKER as _IDS_MARKER
from .answer_names import MARKER as _NAMES_MARKER
from .result_identifiers import MARKER as _RESULT_IDS_MARKER
from .unresolved_miss import MARKER as _UNPAIRED_MARKER
from .wrong_form_refused import MARKER as _REFUSED_MARKER

#: Opening words of every engine line meant for the orchestrator only.
MARKERS = (_IDS_MARKER, _NAMES_MARKER, _UNPAIRED_MARKER, _REFUSED_MARKER, _RESULT_IDS_MARKER)
_LINE = re.compile(
    r"(?:\A|\n)(?:[ \t]*\n)*[ \t]*(?:" + "|".join(re.escape(m) for m in MARKERS) + r")[^\n]*")


def is_marker_line(line: str) -> bool:
    """Whether ``line`` (one line, no newline) is an orchestrator-only engine line."""
    return isinstance(line, str) and line.lstrip(" \t").startswith(MARKERS)


def strip_lines(text: str) -> str:
    """``text`` without its orchestrator-only engine lines and the blank
    lines right before each; unchanged (the same object) when it has none."""
    if not isinstance(text, str) or not any(marker in text for marker in MARKERS):
        return text
    stripped = _LINE.sub("", text)
    if stripped == text:
        return text
    if not text.startswith("\n"):
        stripped = stripped.lstrip("\n")
    return stripped


def _json_note_text(text: str):
    """``(parsed, note key, items key)`` when ``text`` is one JSON object
    carrying the engine note member, else ``None``."""
    from .tool_result_capture import RESULT_CUT_ITEMS_KEY, RESULT_CUT_NOTE_KEY
    stripped = text.strip()
    if stripped[:1] != "{":
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get(RESULT_CUT_NOTE_KEY), str):
        return None
    return parsed, RESULT_CUT_NOTE_KEY, RESULT_CUT_ITEMS_KEY


def public_text(text: str) -> str:
    """``text`` as a person may read it: prose without its orchestrator-only
    engine lines (:func:`strip_lines`), or a JSON answer whose engine note no
    longer holds them (a note left empty is dropped, and an array the note
    wrapped is unwrapped). Unchanged (the same object) when it has none."""
    if not isinstance(text, str) or not any(marker in text for marker in MARKERS):
        return text
    found = _json_note_text(text)
    if found is None:
        return strip_lines(text)
    parsed, note_key, items_key = found
    note = strip_lines(parsed[note_key])
    if note == parsed[note_key]:
        return text
    rest = {key: value for key, value in parsed.items() if key != note_key}
    if note.strip():
        rest[note_key] = note
    elif set(rest) == {items_key} and isinstance(rest[items_key], list):
        return json.dumps(rest[items_key], ensure_ascii=False)
    return json.dumps(rest, ensure_ascii=False)
