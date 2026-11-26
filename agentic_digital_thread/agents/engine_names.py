# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Physical names in engine-authored text.

A table or column name reaches an engine line from a connector's result
record, a baked schema or a model's SQL: it is data, not engine authorship.
Measured offline, a quoted column named like an instruction was copied word
for word into an ``[engine]`` line and a FINAL-ANSWER REQUIREMENTS block, and
so was a quoted table name.

:func:`printable` is the one test an engine renderer applies before it prints
a physical name: a plain SQL identifier (dotted parts of
``[A-Za-z_][A-Za-z0-9_$]*``, no spaces, quotes or other characters, at most
:data:`MAX_CHARS` characters) that also passes the canonical untrusted-text
check (``untrusted_text.validate_untrusted``). A name that fails is never
printed: :class:`Withheld` puts a placeholder naming only its kind in its
place and counts it, so the text can say how many names it withheld.

Detection is not changed by this: the rules that match a query against a
table's columns keep the names as the schema and the SQL give them. Only what
an engine text prints goes through here. Pure.
"""
from __future__ import annotations

import re

from .untrusted_text import sanitize_untrusted, validate_untrusted

MAX_CHARS = 128
_PART = r"[A-Za-z_][A-Za-z0-9_$]*"
_NAME_RE = re.compile(rf"{_PART}(?:\.{_PART})*")
_KINDS = {"table": "a table whose name is withheld", "column": "a column whose name is withheld",
          "state": "a state value that is withheld"}
_PLACEHOLDERS = frozenset({*_KINDS.values(), "a name that is withheld"})


def printable(name) -> bool:
    """True when ``name`` may be printed in engine text (see the module
    docstring)."""
    if not isinstance(name, str) or len(name) > MAX_CHARS or not _NAME_RE.fullmatch(name):
        return False
    return (sanitize_untrusted(name) == name
            and validate_untrusted(name, max_len=MAX_CHARS, field="name") is None)


#: An opaque identifier of a record or series (a UUID, an ``<asset>:<property>``
#: element id): letters and digits, then also ``_ . : / # -``. No spaces.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/#-]*")


def printable_id(value) -> bool:
    """True when an identifier may be printed in engine text: one token of
    :data:`_ID_RE` (no spaces, quotes or other characters, at most
    :data:`MAX_CHARS` characters) that also passes the canonical
    untrusted-text check. The test :func:`printable` applies, widened to the
    hyphens and colons ids carry and kept free of anything that reads as a
    sentence."""
    if not isinstance(value, str) or len(value) > MAX_CHARS or not _ID_RE.fullmatch(value):
        return False
    return (sanitize_untrusted(value) == value
            and validate_untrusted(value, max_len=MAX_CHARS, field="name") is None)


def printable_sql(text) -> bool:
    """True when every name in a SQL fragment (each word and quoted identifier
    the shared tokenizer reads) is :func:`printable`. Literals are values, not
    names, and are not tested here."""
    from .athena_scope import tokenize
    if not isinstance(text, str):
        return False
    tokens, uncertain = tokenize(text)
    return not uncertain and all(printable(str(t.value)) for t in tokens
                                 if t.kind in {"word", "identifier"})


def count_sentence(count: int) -> str:
    """`` N name(s) ... withheld ...`` for ``count`` withheld names, ``""`` for none."""
    if not isinstance(count, int) or count <= 0:
        return ""
    if count == 1:
        return " One name here is withheld because it is not a plain identifier."
    return f" {count} names here are withheld because they are not plain identifiers."


class Withheld:
    """The names one engine text could not print. :meth:`show` gives the name
    or a placeholder, :attr:`count` the distinct names withheld and
    :meth:`sentence` the sentence that reports them."""

    def __init__(self):
        self._names: set = set()

    def show(self, name, kind: str = "column") -> str:
        """``name``, or the placeholder for its kind (counted). A placeholder
        an earlier renderer already put in is passed on and not counted again
        (:meth:`add` carries that renderer's count)."""
        if printable(name):
            return name
        if name in _PLACEHOLDERS:
            return name
        self._names.add((kind, str(name).lower()))
        return _KINDS.get(kind, "a name that is withheld")

    def keep(self, names, kind: str = "column") -> list:
        """The printable ``names`` in order; the rest are counted as withheld."""
        kept = []
        for name in names or ():
            if printable(name):
                kept.append(name)
            else:
                self._names.add((kind, str(name).lower()))
        return kept

    def add(self, count: int) -> None:
        """Count ``count`` names another renderer already withheld."""
        for index in range(count if isinstance(count, int) and count > 0 else 0):
            self._names.add(("carried", str(index)))

    def merge(self, other: "Withheld") -> None:
        """Count every name ``other`` withheld; a name both withheld, once."""
        self._names |= other._names

    @property
    def count(self) -> int:
        return len(self._names)

    def sentence(self) -> str:
        return count_sentence(self.count)
