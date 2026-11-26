# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a person is shown of a recorded investigation-trace step's payloads.

A step records its tool input and output as the system agent's model saw
them (``investigation_trace.record_step``). A tool result there carries the
engine's own members beside the source's data: ``_adt_source`` (the
response-integrity capture) and ``_adt_executed_query`` (the SQL system
agents), both appended after the source's own members. The write side drops
``_adt_*`` keys only at the top of a dict payload
(``investigation_trace.serialize_tool_payload``); a strands result is a list
of content blocks whose text holds the JSON, so the members are recorded.

:func:`public_entry` is the ONE helper that takes them out for display. It
returns a copy and never touches the stored entry, which every other reader
(the trace summary's telemetry lines, the reload, the capability miss check)
keeps reading as written. Recorded text comes in several forms, handled as
follows (:func:`public_payload`):

* text that parses as JSON (a content-block list, an object, an array): the
  members whose key starts with ``_adt_`` (as written or spelled with
  ``\\u`` escapes) are removed, at any depth, and every other character is
  kept as recorded, formatting included. A string value holding such a
  member (the JSON text of a content block) is cleaned the same way and
  written back as a JSON string; when that inner text would have to be cut,
  the whole recorded text is cut instead (next point), so a cut field is
  always a prefix of the record;
* text that does not parse (cut at the size limit, a Python repr, prose):
  nothing can be removed with certainty, so the text is cut before the first
  internal member, or before a key cut short at the end of the text that
  could be one, and the caller marks the field truncated. What is shown is
  then always a prefix of what was recorded;
* text with no internal member is returned as recorded.

Stdlib only; never raises (any fault returns the text cut, or unchanged when
it holds no member).
"""
from __future__ import annotations

import json
import re
from json.decoder import scanstring
from json.scanner import NUMBER_RE

#: Key prefix of the engine's own members (ADR-0049 spelling).
INTERNAL_PREFIX = "_adt_"


def _literal_or_escape(char: str) -> str:
    """``char`` as written, or as a ``\\uXXXX`` escape (behind one or more
    backslashes when the text sits inside a JSON string), any hex case."""
    code = "".join(f"[{h}{h.upper()}]" if h.isalpha() else h for h in format(ord(char), "04x"))
    return "(?:" + re.escape(char) + r"|\\+u" + code + ")"


#: A member key in text that does not parse: a quote (escaped once or more
#: when the JSON sits inside a JSON string, single in a Python repr) right
#: before the prefix, each of its characters literal or escaped.
_MEMBER_KEY = re.compile(r"""\\*["']""" + "".join(map(_literal_or_escape, INTERNAL_PREFIX)))
#: A key cut short at the end of the text by the size limit (``"_ad``). Only
#: the literal spelling, the one the engine writes.
_CUT_KEY_TAIL = re.compile(r"""\\*["']_(?:a(?:d(?:t)?)?)?\Z""")
_WHITESPACE = re.compile(r"[ \t\n\r]*")
_LITERALS = ("true", "false", "null", "NaN", "Infinity", "-Infinity")
#: The step fields shown as payloads and the flag that marks each cut.
_PAYLOAD_FIELDS = (("tool_input", "input_truncated"), ("tool_output", "output_truncated"))


def public_entry(entry: dict) -> dict:
    """``entry`` as a person is shown it: a copy whose steps' ``tool_input``
    and ``tool_output`` hold no internal member (:func:`public_payload`), a
    field cut for that marked ``*_truncated`` (its ``*_bytes`` stay the
    writer's full size). The entry itself is never changed; with no member
    anywhere the same object is returned."""
    steps = entry.get("steps") if isinstance(entry, dict) else None
    if not isinstance(steps, list):
        return entry
    shown = [_public_step(step) for step in steps]
    if all(new is old for new, old in zip(shown, steps)):
        return entry
    return {**entry, "steps": shown}


def _public_step(step):
    if not isinstance(step, dict):
        return step
    changes: dict = {}
    for field, flag in _PAYLOAD_FIELDS:
        text, cut = public_payload(step.get(field))
        if text != step.get(field):
            changes[field] = text
        if cut:
            changes[flag] = True
    return {**step, **changes} if changes else step


def public_payload(text):
    """``(text without internal members, cut)``; see the module docstring.
    A non-string, or text with no internal member, is returned as is."""
    if not isinstance(text, str):
        return text, False
    if not _may_hold_member(text) and not _CUT_KEY_TAIL.search(text):
        return text, False
    if _may_hold_member(text):
        try:
            return _Members(text).removed()
        except Exception:  # noqa: BLE001 - text that does not walk is cut instead
            pass
    return _cut_before_member(text)


def _may_hold_member(text: str) -> bool:
    """Whether ``text`` can hold an internal key: the prefix as written, or
    any ``\\u`` escape, which a JSON key can spell it with."""
    return INTERNAL_PREFIX in text or "\\u" in text


def _cut_before_member(text: str):
    found = _MEMBER_KEY.search(text) or _CUT_KEY_TAIL.search(text)
    if found is None:
        return text, False
    return text[:found.start()].rstrip(", \t\r\n"), True


def _skip(text: str, i: int) -> int:
    return _WHITESPACE.match(text, i).end()


class _Members:
    """One JSON text walked once: the character ranges to replace so that no
    ``_adt_`` member is left, every other character kept. Raises
    ``ValueError`` when the text is not one JSON value."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list = []  # (start, end, replacement, cut)

    def removed(self):
        """``(text, cut)`` with the edits applied."""
        if _skip(self.text, self.value(0)) != len(self.text):
            raise ValueError("trailing text")
        out, at, cut = [], 0, False
        for start, end, replacement, edit_cut in sorted(self.edits):
            if start < at:
                raise ValueError("overlapping edits")
            out += [self.text[at:start], replacement]
            at, cut = end, cut or edit_cut
        return "".join(out) + self.text[at:], cut

    def value(self, i: int) -> int:
        i = _skip(self.text, i)
        head = self.text[i:i + 1]
        if head == "{":
            return self.members(i)
        if head == "[":
            return self.items(i)
        if head == '"':
            return self.string(i)
        number = NUMBER_RE.match(self.text, i)
        if number:
            return number.end()
        for literal in _LITERALS:
            if self.text.startswith(literal, i):
                return i + len(literal)
        raise ValueError("not a JSON value")

    def items(self, i: int) -> int:
        i = _skip(self.text, i + 1)
        if self.text[i:i + 1] == "]":
            return i + 1
        while True:
            i = _skip(self.text, self.value(i))
            head = self.text[i:i + 1]
            if head == "]":
                return i + 1
            if head != ",":
                raise ValueError("bad array")
            i += 1

    def members(self, i: int) -> int:
        i = _skip(self.text, i + 1)
        if self.text[i:i + 1] == "}":
            return i + 1
        spans = []  # (start, end, internal)
        while True:
            if self.text[i:i + 1] != '"':
                raise ValueError("bad key")
            key, after = scanstring(self.text, i + 1)
            colon = _skip(self.text, after)
            if self.text[colon:colon + 1] != ":":
                raise ValueError("bad member")
            nested = len(self.edits)
            end = self.value(colon + 1)
            internal = key.startswith(INTERNAL_PREFIX)
            if internal:
                del self.edits[nested:]  # the member goes whole
            spans.append((i, end, internal))
            i = _skip(self.text, end)
            head = self.text[i:i + 1]
            if head == "}":
                self.drop(spans)
                return i + 1
            if head != ",":
                raise ValueError("bad object")
            i = _skip(self.text, i + 1)

    def drop(self, spans: list) -> None:
        """Remove the internal members with the separator that joined each:
        a leading run up to the first kept member, any later one from the end
        of the member before it."""
        kept = [k for k, span in enumerate(spans) if not span[2]]
        if len(kept) == len(spans):
            return
        if not kept:
            self.edits.append((spans[0][0], spans[-1][1], "", False))
            return
        if kept[0]:
            self.edits.append((spans[0][0], spans[kept[0]][0], "", False))
        for k in range(kept[0] + 1, len(spans)):
            if spans[k][2]:
                self.edits.append((spans[k - 1][1], spans[k][1], "", False))

    def string(self, i: int) -> int:
        value, end = scanstring(self.text, i + 1)
        if _may_hold_member(value):
            shown, cut = public_payload(value)
            if cut:
                # A string holding text that had to be cut: the whole text is
                # cut instead, so what is shown stays a prefix of the record.
                raise ValueError("cut inside a string")
            if shown != value:
                token = self.text[i:end]
                self.edits.append(
                    (i, end, json.dumps(shown, ensure_ascii=token.isascii()), False))
        return end
