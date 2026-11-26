# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Inject one predicate into a simple SELECT's WHERE clause.

Shared by the Athena scope guard (``system_agent``) and the Athena exact-total
split (``athena_exact_split``): both add ONE conjunct to a model-written query
and leave the rest of its text as written. Moved here unchanged from
``system_agent`` so the connector can use it without importing the agent
module. Pure and offline.
"""
from __future__ import annotations

import re as _re

# Structural rewrite gate: only a SINGLE-statement, single top-level SELECT over
# one table is eligible for predicate injection. Complex shapes are analyzed by
# athena_scope and execute unchanged only when every partitioned physical scan is
# proven constrained; otherwise the guarded tool returns scope_required.
_SQL_TAIL_CLAUSE_RE = _re.compile(
    r"\b(group\s+by|having|order\s+by|limit|offset)\b", _re.IGNORECASE
)
_SQL_WHERE_RE = _re.compile(r"\bwhere\b", _re.IGNORECASE)


def _sql_top_level_mask(s: str) -> list:
    """Per-character mask: True outside comments, quotes, and parentheses."""
    structural = _blank_sql_noncode(s, blank_double_quotes=True)
    mask = [False] * len(s)
    depth = 0
    i = 0
    while i < len(structural):
        ch = structural[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and not (ch.isspace() and s[i] != ch):
            mask[i] = True
        i += 1
    return mask


def _find_top_level(s: str, pattern, mask, start: int = 0):
    """First regex match at a fully top-level (unquoted, unparenthesized) position."""
    for m in pattern.finditer(s):
        if m.start() < start:
            continue
        if all(mask[k] for k in range(m.start(), min(m.end(), len(mask)))):
            return m
    return None


def _inject_where_predicate(sql: str, predicate: str) -> str | None:
    """Rewrite a simple SELECT so ``predicate`` constrains it, or None (bail out).

    - existing WHERE → ``WHERE <predicate> AND (<original condition>)`` (the
      original condition is parenthesized so OR precedence is preserved);
    - no WHERE → ``WHERE <predicate>`` inserted before the first top-level
      GROUP BY / HAVING / ORDER BY / LIMIT / OFFSET, or appended at the end.
    """
    s = sql.strip().rstrip(";").strip()
    trailing_start = _trailing_comment_start(s)
    if trailing_start < len(s):
        core = s[:trailing_start].rstrip()
        trailing = s[trailing_start:].lstrip()
        rewritten = _inject_where_predicate(core, predicate)
        return f"{rewritten} {trailing}" if rewritten else None
    mask = _sql_top_level_mask(s)
    where_m = _find_top_level(s, _SQL_WHERE_RE, mask)
    if where_m:
        tail_m = _find_top_level(s, _SQL_TAIL_CLAUSE_RE, mask, start=where_m.end())
        cond_end = tail_m.start() if tail_m else len(s)
        cond = s[where_m.end():cond_end].strip()
        if not cond:
            return None
        tail = s[cond_end:].strip()
        rewritten = f"{s[:where_m.start()].rstrip()} WHERE {predicate} AND ({cond})"
        return f"{rewritten} {tail}".strip()
    tail_m = _find_top_level(s, _SQL_TAIL_CLAUSE_RE, mask)
    if tail_m:
        return f"{s[:tail_m.start()].rstrip()} WHERE {predicate} {s[tail_m.start():]}"
    return f"{s} WHERE {predicate}"


def _blank_sql_noncode(sql: str, *, blank_double_quotes: bool) -> str:
    """Blank comments/string literals while preserving positions and parentheses."""
    chars = list(sql)
    i = 0
    quote = None
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if quote:
            chars[i] = " " if ch != "\n" else "\n"
            if ch == quote:
                if i + 1 < len(chars) and chars[i + 1] == quote:
                    chars[i + 1] = " "
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and nxt == "-":
            chars[i] = chars[i + 1] = " "
            i += 2
            while i < len(chars) and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        if ch == "/" and nxt == "*":
            chars[i] = chars[i + 1] = " "
            i += 2
            while i < len(chars):
                if chars[i] == "*" and i + 1 < len(chars) and chars[i + 1] == "/":
                    chars[i] = chars[i + 1] = " "
                    i += 2
                    break
                chars[i] = " " if chars[i] != "\n" else "\n"
                i += 1
            continue
        if ch == "'" or (blank_double_quotes and ch == '"'):
            quote = ch
            chars[i] = " "
        i += 1
    return "".join(chars)


def _sql_comment_spans(sql: str) -> list[tuple[int, int]]:
    """Return SQL line/block comment spans, excluding comment markers in quotes."""
    spans: list[tuple[int, int]] = []
    quote = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote:
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "-" and nxt == "-":
            start = i
            newline = sql.find("\n", i + 2)
            end = len(sql) if newline < 0 else newline
            spans.append((start, end))
            i = end
            continue
        if ch == "/" and nxt == "*":
            start = i
            close = sql.find("*/", i + 2)
            end = len(sql) if close < 0 else close + 2
            spans.append((start, end))
            i = end
            continue
        i += 1
    return spans


def _trailing_comment_start(sql: str) -> int:
    """Start of a trailing comment suffix, or ``len(sql)`` when absent."""
    spans = _sql_comment_spans(sql)
    boundary = len(sql.rstrip())
    changed = True
    while changed:
        changed = False
        for start, end in reversed(spans):
            if end <= boundary and not sql[end:boundary].strip():
                boundary = len(sql[:start].rstrip())
                changed = True
                break
    return boundary if boundary < len(sql.rstrip()) else len(sql)
