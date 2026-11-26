# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which names a SQL statement writes, and whether a name the engine could not
resolve is an alias the failing reference's own SELECT defines.

Read from the shared tokenizer (``athena_scope.tokenize``), so comments are
skipped and string literals are single tokens: neither can define or
reference a name. A statement the tokenizer reads with any uncertainty
gives no answer.

A SELECT scope is one query block: the SELECT keyword's own parenthesis
level (a CTE body or a subquery is its own block) and, at that level, the
SELECT up to the next one (``UNION`` and the like start another block).
Parentheses that hold no SELECT (function calls, ``OVER (...)``) belong to
the block around them. An alias is defined by ``AS <name>`` directly in a
block's SELECT list, between its SELECT and its FROM (or the clause that
ends the list), not inside parentheses.

Pure and offline.
"""
from __future__ import annotations

from .athena_scope import tokenize

_NAME_KINDS = ("word", "identifier")
_LIST_END = frozenset({"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION",
                       "INTERSECT", "EXCEPT", "WINDOW", "OFFSET", "FETCH"})


def _tokens(sql):
    if not isinstance(sql, str):
        return None
    tokens, uncertain = tokenize(sql)
    return None if uncertain else tokens


def _is_name(token) -> bool:
    return token.kind in _NAME_KINDS


def _chains(tokens) -> list[tuple[int, int, list[str]]]:
    """Every dotted name ``a.b.c`` (words or quoted identifiers joined by
    ``.``): ``(first token, last token, parts)``."""
    out, i = [], 0
    while i < len(tokens):
        if not _is_name(tokens[i]):
            i += 1
            continue
        start, parts = i, [tokens[i].value]
        while (i + 2 < len(tokens) and tokens[i + 1].text == "."
               and _is_name(tokens[i + 2])):
            parts.append(tokens[i + 2].value)
            i += 2
        out.append((start, i, parts))
        i += 1
    return out


def written_names(sql) -> list[tuple[str, int]] | None:
    """``(dotted name, number of parts)`` for every name ``sql`` writes,
    outside comments and literals; ``None`` when it cannot be read."""
    tokens = _tokens(sql)
    if tokens is None:
        return None
    return [(".".join(parts), len(parts)) for _s, _e, parts in _chains(tokens)]


def _blocks(tokens) -> list:
    """The SELECT block each token belongs to, ``(paren index, block number)``,
    or ``None`` before any SELECT."""
    selects = set()
    frame_of, stack = [], [-1]
    for i, token in enumerate(tokens):
        if token.text == ")" and len(stack) > 1:
            stack.pop()
        frame_of.append(stack[-1])
        if token.text == "(":
            stack.append(i)
        if token.kind == "word" and token.value.upper() == "SELECT":
            selects.add(frame_of[i])
    parent = {i: frame_of[i] for i, t in enumerate(tokens) if t.text == "("}
    count: dict = {}
    blocks = []
    for i, token in enumerate(tokens):
        frame = frame_of[i]
        if token.kind == "word" and token.value.upper() == "SELECT":
            count[frame] = count.get(frame, 0) + 1
        while frame not in selects and frame != -1:
            frame = parent[frame]
        blocks.append((frame, count[frame]) if count.get(frame) else None)
    return blocks


def _definitions(tokens, blocks) -> dict:
    """``{block: alias names}`` from ``AS <name>`` in each block's SELECT list."""
    defs: dict = {}
    in_list: dict = {}
    depths: dict = {}
    for i, token in enumerate(tokens):
        block = blocks[i]
        if block is None:
            continue
        word = token.value.upper() if token.kind == "word" else ""
        own_level = token.depth == _block_depth(tokens, block, depths)
        if word == "SELECT" and own_level:
            in_list[block] = True
        elif word in _LIST_END and own_level:
            in_list[block] = False
        elif (word == "AS" and own_level and in_list.get(block)
              and i + 1 < len(tokens) and _is_name(tokens[i + 1])):
            defs.setdefault(block, set()).add(tokens[i + 1].value.lower())
    return defs


def _block_depth(tokens, block, cache: dict) -> int:
    """The parenthesis depth of the tokens directly in ``block``."""
    frame = block[0]
    if frame not in cache:
        cache[frame] = 0 if frame == -1 else tokens[frame].depth + 1
    return cache[frame]


def _references(tokens, blocks, name: str) -> list[tuple[int, object]]:
    """``(token, block)`` for every reference whose last part is ``name``:
    not an ``AS`` target, not a function name, not a qualifier."""
    out = []
    for start, end, parts in _chains(tokens):
        if parts[-1].lower() != name:
            continue
        before = tokens[start - 1] if start else None
        after = tokens[end + 1] if end + 1 < len(tokens) else None
        if before is not None and before.kind == "word" and before.value.upper() == "AS":
            continue
        if after is not None and after.text == "(":
            continue
        out.append((start, blocks[start]))
    return out


def _offset(sql: str, line: int, column: int) -> int | None:
    """The 0-based offset of the engine's 1-based ``line:column``."""
    lines = sql.split("\n")
    if not (1 <= line <= len(lines)) or not (1 <= column <= len(lines[line - 1])):
        return None
    return sum(len(text) + 1 for text in lines[:line - 1]) + column - 1


def same_scope_alias(sql, name: str, position: tuple[int, int] | None = None) -> bool:
    """True when ``name`` is an alias defined in the SELECT list of the block
    the failing reference sits in.

    ``position`` is the engine's ``(line, column)`` of the failing reference
    in the statement it ran (``sql`` is read as the row cap sends it:
    stripped). When it falls on a reference of ``name``, that reference's
    block decides. Otherwise every reference of ``name`` must sit in a block
    that defines it, so whichever failed, the alias is in its own scope."""
    text = sql.strip() if isinstance(sql, str) else sql
    tokens = _tokens(text)
    if tokens is None or not name:
        return False
    name = name.lower()
    blocks = _blocks(tokens)
    defs = _definitions(tokens, blocks)
    refs = _references(tokens, blocks, name)
    at = _offset(text, *position) if position else None
    hit = [block for index, block in refs if tokens[index].start == at]
    if hit:
        return hit[0] is not None and name in defs.get(hit[0], ())
    return bool(refs) and all(b is not None and name in defs.get(b, ()) for _i, b in refs)
