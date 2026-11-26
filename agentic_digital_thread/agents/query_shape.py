# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One-way, bounded query-shape fingerprints for observability only.

The canonical token stream is intentionally private and ephemeral. Public
callers receive only a SHA-256 digest and its algorithm version; normalized
query text is never returned, logged, persisted, or suitable for execution.
This is a conservative lexical normalizer, not a parser or safety validator.
"""
from __future__ import annotations

import hashlib
import re
from typing import Final

QUERY_SHAPE_VERSION: Final = "v1"
MAX_QUERY_SHAPE_BYTES: Final = 16 * 1024

_NUMBER_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)"
)
_DOLLAR_QUOTE_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_LITERAL_WORDS = frozenset({"false", "null", "true", "unknown"})
_MULTI_CHAR_TOKENS = (
    "#>>", "->>", "<=>", "::", "<=", ">=", "<>", "!=", "==", "=~",
    "||", "&&", ":=", "..", "->", "<-", "=>", "!~",
)


def _consume_block_comment(query: str, start: int) -> int:
    """Return the first index after a nested block comment."""
    index = start + 2
    depth = 1
    while index < len(query):
        if query.startswith("/*", index):
            depth += 1
            index += 2
        elif query.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    raise ValueError("unterminated block comment")


def _consume_quoted(query: str, start: int, closing: str) -> int:
    """Return the first index after a quoted token, honoring doubled quotes."""
    index = start + 1
    while index < len(query):
        char = query[index]
        if char == "\\" and index + 1 < len(query):
            index += 2
            continue
        if char == closing:
            if index + 1 < len(query) and query[index + 1] == closing:
                index += 2
                continue
            return index + 1
        index += 1
    raise ValueError("unterminated quoted token")


def _consume_bracket_identifier(query: str, start: int) -> int:
    """Return the first index after a SQL Server-style bracket identifier."""
    index = start + 1
    while index < len(query):
        if query[index] == "]":
            if index + 1 < len(query) and query[index + 1] == "]":
                index += 2
                continue
            return index + 1
        index += 1
    raise ValueError("unterminated bracket identifier")


def _consume_identifier(query: str, start: int) -> int:
    index = start + 1
    while index < len(query):
        char = query[index]
        if not (char.isalnum() or char in "_$"):
            break
        index += 1
    return index


def _shape_tokens(query: str) -> list[str]:
    """Build an internal lexical token stream; it must never escape this module."""
    tokens: list[str] = []
    index = 0
    while index < len(query):
        char = query[index]
        if char.isspace():
            index += 1
        elif query.startswith("--", index) or query.startswith("//", index):
            newline = query.find("\n", index + 2)
            index = len(query) if newline < 0 else newline + 1
        elif query.startswith("/*", index):
            index = _consume_block_comment(query, index)
        elif char == "'":
            index = _consume_quoted(query, index, "'")
            tokens.append("?")
        elif char == "$":
            index = _consume_dollar_token(query, index, tokens)
        elif char in ('"', "`"):
            end = _consume_quoted(query, index, char)
            tokens.append("i:" + query[index:end])
            index = end
        elif char == "[":
            end = _consume_bracket_identifier(query, index)
            tokens.append("i:" + query[index:end])
            index = end
        elif char.isdigit() or (char == "." and index + 1 < len(query)
                                and query[index + 1].isdigit()):
            match = _NUMBER_RE.match(query, index)
            if match is None:
                raise ValueError("malformed numeric literal")
            tokens.append("?")
            index = match.end()
        elif char.isalpha() or char == "_":
            end = _consume_identifier(query, index)
            word = query[index:end].casefold()
            tokens.append("?" if word in _LITERAL_WORDS else word)
            index = end
        elif ord(char) < 32:
            raise ValueError("unsupported control character")
        else:
            token = next((item for item in _MULTI_CHAR_TOKENS
                          if query.startswith(item, index)), char)
            tokens.append("?" if token == "?" else token.casefold())  # nosec B105 - a SQL placeholder token, not a credential
            index += len(token)
    return tokens


def _consume_dollar_token(query: str, start: int, tokens: list[str]) -> int:
    """Mask PostgreSQL dollar strings and positional parameters."""
    quote = _DOLLAR_QUOTE_RE.match(query, start)
    if quote is not None:
        marker = quote.group(0)
        end = query.find(marker, quote.end())
        if end < 0:
            raise ValueError("unterminated dollar quote")
        tokens.append("?")
        return end + len(marker)
    index = start + 1
    while index < len(query) and query[index].isdigit():
        index += 1
    if index > start + 1:
        tokens.append("?")
        return index
    tokens.append("$")
    return start + 1


def fingerprint_query_shape(query: object) -> dict[str, str]:
    """Return additive ``query_fingerprint``/``query_shape_version`` fields.

    Empty, non-string, lexically malformed, comment-only, and over-cap input
    returns ``{}``: honest absence is safer than hashing a partial shape.
    The private canonical stream is discarded immediately after hashing.
    """
    if not isinstance(query, str) or not query.strip():
        return {}
    try:
        if len(query.encode("utf-8")) > MAX_QUERY_SHAPE_BYTES:
            return {}
        tokens = _shape_tokens(query)
    except (UnicodeError, ValueError):
        return {}
    if not tokens:
        return {}
    digest = hashlib.sha256(
        (QUERY_SHAPE_VERSION + "\0" + "\x1f".join(tokens)).encode("utf-8")
    ).hexdigest()
    return {
        "query_fingerprint": digest,
        "query_shape_version": QUERY_SHAPE_VERSION,
    }
