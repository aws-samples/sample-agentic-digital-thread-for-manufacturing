# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Defense-in-depth sensitivity controls for integrity data paths."""
from __future__ import annotations

import hashlib
import re
from typing import Any

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
# A phone number: an optional '+', then digit groups separated by spaces,
# dots, parentheses or hyphens. The left guard rejects a run joined on its left
# to a letter, digit, underscore, hyphen or dot, so the numeric tail of a
# letter-prefixed or hyphen-joined record identifier (RUN-..., WO-...) and the
# fractional part of a decimal are not matched. The leading negative lookahead
# rejects an ISO-8601 date or the date head of a date-time (YYYY-MM-DD...). The
# right guard rejects a run continued by a word character or another hyphen (a
# longer identifier), while a trailing sentence '.' is allowed.
_PHONE_RE = re.compile(
    r"(?<![\w.\-])(?!\d{4}-\d{2}-\d{2})(?:\+?\d[\d .()\-]{7,}\d)(?![\w\-])"
)
# A card number: 13 to 19 digits with an optional single space or hyphen
# between them. The guards reject a run joined to a preceding or following
# digit, hyphen or dot, so the numeric part of a hyphen- or dot-joined record
# identifier is not matched, and the leading lookahead rejects an ISO-8601 date
# or date-time. A letter glued directly to the digits (a labelled PAN such as a
# card number written after a word) is still matched.
_CARD_RE = re.compile(
    r"(?<![\d.\-])(?!\d{4}-\d{2}-\d{2})(?:\d[ -]?){13,19}(?![\d.\-])"
)
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b")
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I)
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----", re.I)
_GITHUB_TOKEN_RE = re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b")
_STRIPE_TOKEN_RE = re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox(?:a|b|p|r|s)-[A-Za-z0-9-]{10,}\b")
_GENERIC_TOKEN_RE = re.compile(
    r"\b(?:api[_-]?(?:key|token)|access[_-]?token|client[_-]?secret)[_-]?[A-Za-z0-9+/=.-]{16,}\b",
    re.I,
)
_SECRET_ASSIGN_RE = re.compile(
    r"\b(password|passwd|secret|token|api[_ -]?key|credential)\s*[:=]\s*[^\s,;]+",
    re.I,
)

# URL userinfo in an answer, aligned with a URL parser: from '://' through the
# LAST '@' of the authority, which ends at the first '/', '?', '#' or
# whitespace. An '@' in a path, query or fragment is not userinfo and stays,
# so https://h.example.com?owner=a@b comes back as written. Two shapes a parser
# misreads, because the password itself ends the authority early, are masked
# too (_userinfo_spans). (Log lines mask more:
# connection_security.mask_url_credentials_for_log.)
_AUTHORITY_RUN_RE = re.compile(r"[^/?#\s]*")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_WHITESPACE_RE = re.compile(r"\s")
# A host with an optional numeric port: what an authority with no '@' is when
# it is not the start of a password.
_HOST_PORT_RE = re.compile(r"(?:\[[^\]]*\]|[^:\[\]]*)(?::\d+)?")


def _postgres_scheme(text: str, sep: int) -> bool:
    """True when the scheme before the '://' at *sep* is postgres or postgresql
    (any case). There an '@' after the host is never a database name in
    practice, so a ``host:port``-shaped authority before a raw '/', '?' or '#'
    may be ``user:<digits>`` (registration refuses it)."""
    for name in ("postgresql", "postgres"):
        start = sep - len(name)
        if start >= 0 and text[start:sep].lower() == name:
            return start == 0 or not (text[start - 1].isalnum() or text[start - 1] in "+.-")
    return False


def _userinfo_shaped(text: str, sep: int, authority: str) -> bool:
    """Whether an authority with no '@' that a raw character cut short may be
    the start of userinfo: it holds a ':' not followed by a port, or any ':'
    under a postgres scheme, where ``user:<digits>`` reads as a host and a port
    (registration refuses that shape). A host or ``host:port`` never is."""
    if not authority:
        return False
    if not _HOST_PORT_RE.fullmatch(authority):
        return True
    return ":" in authority and _postgres_scheme(text, sep)


#: A token that starts a URL of its own (a scheme word, then '://').
_URL_START_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}://")


class _Scan:
    """Searches over one text that remember what they found, so a long token
    holding many '://' costs O(n) in total: the end of the token a position is
    in, and a stretch a search for '@' already found empty."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._token = (0, -1)
        self._empty = (0, -1)

    def token_end(self, pos: int) -> int:
        start, end = self._token
        if not start <= pos <= end:
            space = _WHITESPACE_RE.search(self.text, pos)
            start, end = pos, space.start() if space else len(self.text)
            self._token = (start, end)
        return end

    def find_at(self, pos: int, stop: int) -> int:
        start, end = self._empty
        inside = start <= pos < end
        frm = end if inside else pos
        found = self.text.find("@", frm, stop) if frm < stop else -1
        if found < 0:
            self._empty = (start, max(end, stop)) if inside else (pos, stop)
        return found


def _cut_userinfo_end(scan: _Scan, end: int) -> int:
    """Where userinfo cut short at *end* ends (its '@'), or -1. After a '/',
    '?', '#' or a nested '://', the first '@' later in the same token; after
    whitespace with at most one line break, the first '@' in the next token,
    unless that token starts a URL of its own. Then, through the host's run
    after it, its last '@'."""
    text = scan.text
    if text[end].isspace():
        gap = _WHITESPACE_RUN_RE.match(text, end).end()
        if gap >= len(text) or text.count("\n", end, gap) > 1 or _URL_START_RE.match(text, gap):
            return -1
        end = gap
    stop = scan.token_end(end)
    first = scan.find_at(end, stop)
    if first < 0:
        return -1
    run_end = _AUTHORITY_RUN_RE.match(text, first + 1, stop).end()
    return text.rfind("@", first, run_end)


def _userinfo_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start of '://', end after the '@', userinfo)`` for each URL in *text*.

    The userinfo runs through the last '@' of the authority. When the
    authority holds no '@' but is userinfo-shaped (:func:`_userinfo_shaped`),
    a raw character in the password still cut it short:
    - a '/', '?', '#' or a nested '://': the '@' comes later in the same token;
    - whitespace, with at most one line break: the next token holds the '@'.
    The URL is then masked through that '@' (:func:`_cut_userinfo_end`). A
    host or ``host:port`` is never joined to what follows it. Searches are
    remembered (:class:`_Scan`), so the scan stays linear.
    """
    spans: list[tuple[int, int, str]] = []
    scan, pos, size = _Scan(text), 0, len(text)
    while True:
        sep = text.find("://", pos)
        if sep < 0:
            return spans
        begin = sep + 3
        limit = text.find("://", begin)
        if limit < 0:
            limit = size
        end = _AUTHORITY_RUN_RE.match(text, begin, limit).end()
        authority = text[begin:end]
        last = authority.rfind("@")
        if last >= 0:
            spans.append((sep, begin + last + 1, authority[:last]))
            pos = begin + last + 1
            continue
        found = _cut_userinfo_end(scan, end) if end < size and _userinfo_shaped(text, sep, authority) else -1
        if found >= 0:
            spans.append((sep, found + 1, text[begin:found]))
            pos = found + 1
        else:
            pos = begin


def mask_url_userinfo(text: str) -> str:
    """*text* with every URL's userinfo replaced by ``***`` (a user name alone
    too: it may be an opaque token)."""
    out: list[str] = []
    pos = 0
    for start, end, _userinfo in _userinfo_spans(text):
        out.append(text[pos:start] + "://***@")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def url_has_password(text: str) -> bool:
    """True when a URL in *text* carries userinfo with a password part (a
    ``:``). A user name alone does not make the text sensitive."""
    return any(":" in userinfo for _start, _end, userinfo in _userinfo_spans(text))


# A quote that opens a value: plain, or escaped any number of times (a JSON
# string inside a JSON string). It closes at the next copy of itself with no
# further backslash in front, that is with the same escape depth.
_QUOTE_RE = re.compile(r"\\*[\"']")


class _Lines:
    """Line bounds for positions scanned left to right: each line is measured
    once, so a scan with many keys on one line stays linear. A line's content
    ends before its ``\\r\\n`` or ``\\n``."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.start = 0
        self.newline = -1        # index of the current line's '\n' (or len)
        self.content_end = 0
        self.next_start = -1     # start of the next line, -1 at the end
        self.indent = 0
        self.continuation: dict = {}

    def at(self, pos: int) -> "_Lines":
        if pos > self.newline:
            text = self.text
            start = text.rfind("\n", 0, pos) + 1
            newline = text.find("\n", pos)
            if newline < 0:
                newline, self.next_start = len(text), -1
            else:
                self.next_start = newline + 1
            self.start = start
            self.newline = newline
            self.content_end = newline - 1 if (newline > start and newline < len(text)
                                               and text[newline - 1] == "\r") else newline
            self.indent = _indent_width(text, start, self.content_end)
        return self

    def starts_at(self, pos: int) -> bool:
        """True when only blanks, and at most a YAML sequence marker ('- '),
        come before *pos* on its line: a header or YAML mapping line, not a
        key inside prose."""
        first = self.start + self.indent
        if pos == first:
            return True
        text = self.text
        if pos > first + 1 and text.startswith("-", first) and text[first + 1] in " \t":
            return _indent_width(text, first + 1, pos) == pos - first - 1
        return False

    def continuation_end(self, *, block: bool) -> int:
        """End of the following lines indented deeper than this one (header
        folding, or a YAML block when *block*: there a blank line belongs to
        the block when a deeper line follows it); -1 when none follows."""
        key = (self.next_start, block)
        if key not in self.continuation:
            self.continuation[key] = _continuation_end(
                self.text, self.next_start, self.indent, block=block)
        return self.continuation[key]


def _indent_width(text: str, start: int, end: int) -> int:
    pos = start
    while pos < end and text[pos] in " \t":
        pos += 1
    return pos - start


def _continuation_end(text: str, line_start: int, base_indent: int, *, block: bool) -> int:
    end = -1
    while line_start >= 0:
        newline = text.find("\n", line_start)
        stop = len(text) if newline < 0 else newline
        content_end = stop - 1 if stop > line_start and text[stop - 1] == "\r" else stop
        width = _indent_width(text, line_start, content_end)
        if line_start + width == content_end:      # a blank line
            if not block:
                break
        elif width > base_indent:
            end = content_end
        else:
            break
        line_start = -1 if newline < 0 else newline + 1
    return end


# A YAML block scalar indicator (| or >, with optional chomping/indent) as the
# whole rest of a line; or nothing but blanks after the separator.
_BLOCK_MARKER_RE = re.compile(r"[ \t]*[|>][-+]?[0-9]?[-+]?[ \t]*$")
_BLANK_RE = re.compile(r"[ \t]*$")


def _closing_quote(text: str, pos: int, end: int, quote: str) -> int:
    """Index of the next *quote* in ``text[pos:end]`` at the same escape depth, or -1."""
    while True:
        found = text.find(quote, pos, end)
        if found <= 0 or text[found - 1] != "\\":
            return found
        pos = found + 1


# What may come right before a quote that opens a value or a parameter value;
# a quote after a letter or digit is part of a word (an apostrophe, or a quote
# inside an unquoted value) and opens nothing.
_OPENER_BEFORE = frozenset(" \t\r\n=:,([{")


def _line_content_end(text: str, line_start: int) -> tuple[int, int]:
    """``(content end, index of the '\\n' or -1)`` for the line at *line_start*."""
    newline = text.find("\n", line_start)
    stop = len(text) if newline < 0 else newline
    if stop > line_start and text[stop - 1] == "\r":
        stop -= 1
    return stop, newline


def _close_after(text: str, pos: int, quote: str, base_indent: int) -> int:
    """Index of the *quote* that closes a value still open at *pos*, or -1.

    Searched on the rest of *pos*'s line, then on the following lines indented
    deeper than *base_indent* (the key's line) and on the first line after them
    that is not, never past a blank line. Each line is read once.
    """
    content_end, newline = _line_content_end(text, pos)
    close = _closing_quote(text, pos, content_end, quote)
    if close >= 0:
        return close
    while newline >= 0:
        line_start = newline + 1
        content_end, newline = _line_content_end(text, line_start)
        width = _indent_width(text, line_start, content_end)
        if line_start + width == content_end:      # a blank line
            return -1
        close = _closing_quote(text, line_start, content_end, quote)
        if close >= 0:
            return close
        if width <= base_indent:
            return -1
    return -1


def _open_quote_end(text: str, start: int, stop: int, base_indent: int) -> tuple[int, bool]:
    """``(end, unclosed)`` for the value ``text[start:stop]``.

    A quote that opens in it (at its start, or after a blank or one of
    ``= : , ( [ {``) and does not close inside it carries the value on to the
    quote that closes it later (:func:`_close_after`). *unclosed* is True when
    no quote does (the caller then falls back to the deeper continuation
    lines).
    """
    pos = start
    while True:
        match = _QUOTE_RE.search(text, pos, stop)
        if match is None:
            return stop, False
        if match.start() > start and text[match.start() - 1] not in _OPENER_BEFORE:
            pos = match.end()
            continue
        quote = match.group()
        close = _closing_quote(text, match.end(), stop, quote)
        if close >= 0:
            pos = close + len(quote)
            continue
        close = _close_after(text, stop, quote, base_indent)
        if close < 0:
            return stop, True
        return close + len(quote), False


def _rstrip_end(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1] in " \t":
        end -= 1
    return end


# An Authorization or Proxy-Authorization value, under any scheme. The key's
# closing quote and the value's opening quote, plain or escaped, are part of
# the separator. A quoted value runs to its closing quote, on a later line
# when it does not close on its own; a known scheme name at its start stays
# visible and the rest is the credential. An unquoted value on a header or
# YAML line (the key first on its line, then ':') is the rest of the line;
# elsewhere (prose, a key=value list) it is a scheme and a token68 or
# auth-param list (Digest, AWS SigV4; a parameter value may itself be quoted at
# any escape depth), or a bare token. An unknown scheme-shaped word followed by
# another token is redacted with it, since it may be a bare token. A value
# continues on the following deeper lines only when it is not finished on its
# line: empty, a YAML block marker, a scheme alone, an unclosed quote, or a
# trailing ',' or '\'.
_AUTH_SCHEMES = frozenset({
    "aws4-hmac-sha256", "basic", "bearer", "concealed", "digest", "dpop", "gnap",
    "hoba", "mutual", "negotiate", "ntlm", "oauth", "privatetoken", "scram-sha-1",
    "scram-sha-256", "token", "vapid",
})
_AUTH_KEY_RE = re.compile(
    r"\b(?:proxy-)?authorization"
    r"\\*[\"']?[ \t]*(?P<sep>[:=])[ \t]*"
    r"(?P<open>\\*[\"'])?",
    re.I,
)
_SCHEME_WORD_RE = re.compile(r"[ \t]*([A-Za-z][A-Za-z0-9-]{0,39})[ \t]+")
_PARAM_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+[ \t]*=[ \t]*")
_BARE_PARAM_VALUE_RE = re.compile(r"[^\s,\"]*")
_PARAM_SEP_RE = re.compile(r"[ \t]*,[ \t]*")
# A ',' joins two runs of one value unless what follows it is the next key.
_NOT_A_KEY = r"(?![A-Za-z_][A-Za-z0-9_.-]*[ \t]*[:=])"
_TOKEN_RE = re.compile(r"[^\s,;'\"\\]+(?:," + _NOT_A_KEY + r"[^\s,;'\"\\]+)*")


def _quoted_end(text: str, pos: int, end: int) -> int | None:
    """End of a quoted value opening at *pos*, or None if none opens there.
    An unclosed value runs to *end*."""
    quote = _QUOTE_RE.match(text, pos, end)
    if quote is None:
        return None
    close = _closing_quote(text, quote.end(), end, quote.group())
    return end if close < 0 else close + len(quote.group())


def _param_list_end(text: str, pos: int, end: int) -> int:
    """End of the auth-param list starting at *pos* (*pos* if there is none)."""
    last = pos
    while True:
        name = _PARAM_NAME_RE.match(text, pos, end)
        if name is None:
            return last
        pos = _quoted_end(text, name.end(), end)
        if pos is None:
            pos = _BARE_PARAM_VALUE_RE.match(text, name.end(), end).end()
        last = pos
        sep = _PARAM_SEP_RE.match(text, pos, end)
        if sep is None:
            return last
        pos = sep.end()


def _credential_end(text: str, pos: int, end: int) -> int:
    """End of the credential after a scheme: a quoted value, an auth-param
    list or a token68 (*pos* if there is none)."""
    quoted = _quoted_end(text, pos, end)
    if quoted is not None:
        return quoted
    params = _param_list_end(text, pos, end)
    if params > pos:
        return params
    token = _TOKEN_RE.match(text, pos, end)
    return token.end() if token else pos


def _kept_scheme(value: str) -> str:
    """The known scheme name (with its spaces) a credential *value* starts with."""
    word = _SCHEME_WORD_RE.match(value)
    if word and word.group(1).lower() in _AUTH_SCHEMES and value[word.end():].strip():
        return word.group(0)
    return ""


def _auth_unfinished(text: str, start: int, stop: int, line_end: int) -> bool:
    """True when an unquoted value ``text[start:stop]`` is not finished on its
    line: a scheme alone, or a line that ends in ',' or '\\' right after it."""
    rest = text[stop:line_end].strip(" \t")
    if rest not in ("", ",", "\\"):
        return False
    value = text[start:stop].strip(" \t")
    if (rest or value[-1:]) in (",", "\\"):
        return True
    return value.lower() in _AUTH_SCHEMES


def _auth_unquoted_end(text: str, key, start: int, line) -> tuple[int, str | None]:
    """``(end, kept scheme)`` of an unquoted Authorization value on its key line."""
    line_end = line.content_end
    word = _SCHEME_WORD_RE.match(text, start, line_end)
    if key.group("sep") == ":" and line.starts_at(key.start()):
        # A header or YAML line: the value is the rest of the line.
        stop = _rstrip_end(text, start, line_end)
        known = word and word.group(1).lower() in _AUTH_SCHEMES
        return stop, (word.group(0) if known and stop > word.end() else "")
    stop = _credential_end(text, word.end(), line_end) if word else start
    if word and stop > word.end():
        return stop, (word.group(0) if word.group(1).lower() in _AUTH_SCHEMES else "")
    token = _TOKEN_RE.match(text, start, line_end)
    return (token.end(), "") if token else (start, None)


def _redact_auth_headers(text: str) -> str:
    out: list[str] = []
    pos = 0
    lines = _Lines(text)
    while True:
        key = _AUTH_KEY_RE.search(text, pos)
        if key is None:
            break
        start = key.end()
        line = lines.at(key.start())
        line_end = line.content_end
        quote = key.group("open")
        more = block = False
        if quote:
            close = _closing_quote(text, start, line_end, quote)
            if close < 0:
                close = _close_after(text, line_end, quote, line.indent)
                more = close < 0     # never closes: the deeper lines, if any
            stop = line_end if close < 0 else close
            value = text[start:stop]
            kept = _kept_scheme(value) if value.strip() else None
        elif _BLOCK_MARKER_RE.match(text, start, line_end):
            stop, kept, more, block = line_end, None, True, True
        elif _BLANK_RE.match(text, start, line_end):
            stop, kept, more = start, None, True
        else:
            stop, kept = _auth_unquoted_end(text, key, start, line)
            stop, unclosed = _open_quote_end(text, start, stop, line.indent)
            more = stop <= line_end and (unclosed or _auth_unfinished(text, start, stop, line_end))
        if more:
            # Folded continuation lines, a value on the next lines, or a YAML
            # block: every following line indented deeper than the key's.
            further = line.continuation_end(block=block)
            if further >= 0:
                stop = further if block else _open_quote_end(text, start, further, line.indent)[0]
                if kept is None:
                    kept = "" if text[start - 1:start] in (" ", "\t") else " "
        if kept is None:  # nothing after the separator
            out.append(text[pos:start])
        else:
            out.append(f"{text[pos:start]}{kept}[REDACTED_CREDENTIAL]")
        pos = max(stop, start)
    out.append(text[pos:])
    return "".join(out)


# A secret key, possibly behind a snake, kebab, camel or SCREAMING prefix
# (db_password, x-api-key, dbPassword, PGPASSWORD), then '=' or ':' and a value
# of any length: quoted at any escape depth (to its closing quote, on a later
# line when it does not close on its own), the rest of the line on a YAML line
# (the key first on its line, then ':'), or else a run up to whitespace, ',' or
# ';' (a ',' that does not start the next key joins two runs). An unquoted
# value continues through the following deeper lines. The key must end at the
# separator, so password_hash or a bare key name in prose never matches.
_SECRET_KEY_NAMES = (
    "aws_secret_access_key", "aws_session_token", "client_secret", "access_token",
    "refresh_token", "session_token", "id_token", "private_key", "api_key",
    "password", "passwd",
)
_SECRET_KEY_CORE = "|".join(name.replace("_", "[_-]?") for name in _SECRET_KEY_NAMES)
_SECRET_KEY_SCREAMING = "|".join(
    name.upper().replace("_", "[_-]?") for name in _SECRET_KEY_NAMES)
_SECRET_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    # empty prefix, or one ending at '_'/'-' or at a camel/Pascal case boundary
    r"(?:[A-Za-z0-9_-]*(?:(?<=[_-])|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z0-9])(?=[A-Z][a-z])))?"
    r"(?i:" + _SECRET_KEY_CORE + r")"
    # SCREAMING prefix with no separator (PGPASSWORD)
    r"|[A-Za-z0-9_-]*[A-Z0-9](?:" + _SECRET_KEY_SCREAMING + r"))"
    r"\\*[\"']?[ \t]*(?P<sep>[:=])[ \t]*"
    r"(?P<open>\\*[\"'])?"
)
_SECRET_BARE_VALUE_RE = re.compile(r"[^\s,;]+(?:," + _NOT_A_KEY + r"[^\s,;]+)*")


def _secret_value_end(text: str, key, start: int, line) -> tuple[int, bool, bool]:
    """``(end, extend, block)`` of a secret value read from its key line;
    *extend* when the following deeper lines may continue it."""
    line_end = line.content_end
    quote = key.group("open")
    if quote:
        close = _closing_quote(text, start, line_end, quote)
        if close < 0:
            close = _close_after(text, line_end, quote, line.indent)
        # A value that never closes takes the deeper lines, if any.
        return (line_end, True, False) if close < 0 else (close, False, False)
    if _BLOCK_MARKER_RE.match(text, start, line_end):
        return start, True, True
    if _BLANK_RE.match(text, start, line_end):
        return start, True, False
    if key.group("sep") == ":" and line.starts_at(key.start()):
        stop = _rstrip_end(text, start, line_end)     # a YAML line
    else:
        bare = _SECRET_BARE_VALUE_RE.match(text, start, line_end)
        stop = bare.end() if bare else start
    stop, _unclosed = _open_quote_end(text, start, stop, line.indent)
    # An unquoted value reads on through deeper continuation lines, unless a
    # quote in it closed on a later line.
    return stop, stop <= line_end, False


def _redact_secret_keys(text: str) -> str:
    out: list[str] = []
    pos = 0
    lines = _Lines(text)
    while True:
        key = _SECRET_KEY_RE.search(text, pos)
        if key is None:
            break
        start = key.end()
        line = lines.at(key.start())
        stop, extend, block = _secret_value_end(text, key, start, line)
        lead = ""
        if extend:
            # A value folded onto, or written on, the next lines, or a YAML
            # block: the following lines indented deeper than the key's.
            more = line.continuation_end(block=block)
            if more >= 0:
                if stop == start and text[start - 1:start] not in (" ", "\t"):
                    lead = " "
                stop = more if block else _open_quote_end(text, start, more, line.indent)[0]
        if stop > start:
            out.append(f"{text[pos:start]}{lead}[REDACTED_SECRET]")
        else:
            out.append(text[pos:start])
        pos = stop
    out.append(text[pos:])
    return "".join(out)


def redact_credential_values(text: str) -> str:
    """*text* with Authorization values and secret-key values redacted, and
    nothing else changed (no length cut, no value patterns): for an error
    detail that must stay readable."""
    return _redact_secret_keys(_redact_auth_headers(str(text)))


_MARKDOWN_META_RE = re.compile(r"([\\`*_{}\[\]<>#+.!|()~-])")
_SENSITIVE_TOKENS = {
    "password", "passwd", "pass", "passphrase", "secret", "token", "authorization",
    "credential", "credentials", "email", "phone", "ssn", "social", "credit",
    "card", "apikey", "api", "private", "access", "session", "bearer", "oauth",
}


# A "...key" field is a secret by default; only a narrow, generic set of
# record-identifier key forms is exempt. Keeping unknown "...key" names
# sensitive fails closed: a credential name nobody listed (kms_key, data_key,
# a newly coined one) stays screened. The exempt forms are generic database and
# record grain keys, not credential names: sort, partition, row, primary,
# foreign, hash, range, shard and surrogate keys are standard storage keys, and
# a lot key and a batch key are record-grain identifiers (a lot number, a batch
# number), never a credential. "secret" and "token" suffixes are always
# sensitive.
_RECORD_KEY_FIELDS = {
    "sort_key", "partition_key", "row_key", "primary_key", "foreign_key",
    "hash_key", "range_key", "shard_key", "surrogate_key", "lot_key",
    "batch_key",
}


def _field_tokens(field: str) -> tuple[str, set[str]]:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", field or "")
    expanded = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", expanded)
    normalized = re.sub(r"[^a-z0-9]+", "_", expanded.lower()).strip("_")
    return normalized, set(normalized.split("_")) if normalized else set()


def _secret_key_suffix(normalized: str, token_list: list[str]) -> bool:
    """True when the field name ends in a credential suffix: ``_secret`` or
    ``_token`` on their own, or any ``...key`` name that is not one of the
    generic record-identifier key forms in ``_RECORD_KEY_FIELDS`` (so an
    unknown ``...key`` field stays sensitive, failing closed)."""
    if normalized.endswith(("_secret", "_token")):
        return True
    return (len(token_list) >= 2 and token_list[-1] == "key"
            and normalized not in _RECORD_KEY_FIELDS)


def sensitive_field(field: str) -> bool:
    normalized, tokens = _field_tokens(field)
    compounds = {"apikey", "clientsecret", "accesstoken", "sessionkey", "privatekey"}
    compact = normalized.replace("_", "")
    return (bool(tokens & _SENSITIVE_TOKENS) or compact in compounds
            or _secret_key_suffix(normalized, normalized.split("_") if normalized else []))


_SAFE_STRING_FIELD_TOKENS = {
    "id", "identifier", "code", "name", "label", "key", "status", "state",
    "type", "category", "enum", "class", "kind", "role", "date", "time",
    "timestamp", "unit", "site", "plant", "asset", "equipment", "order",
}
_FREE_TEXT_FIELD_TOKENS = {
    "note", "notes", "description", "message", "comment", "comments", "text",
    "value", "payload", "content", "body", "detail", "details",
}


def classified_string_field(field: str) -> bool:
    """Allow only schema-semantic bounded string categories, never generic prose."""
    normalized = re.sub(r"[^a-z0-9]+", "_", (field or "").lower()).strip("_")
    tokens = set(normalized.split("_"))
    if not normalized or tokens & _FREE_TEXT_FIELD_TOKENS:
        return False
    return bool(tokens & _SAFE_STRING_FIELD_TOKENS)


def free_text_field(field: str) -> bool:
    """True when the column name signals free-form prose (never evidence-safe)."""
    normalized = re.sub(r"[^a-z0-9]+", "_", (field or "").lower()).strip("_")
    return not normalized or bool(set(normalized.split("_")) & _FREE_TEXT_FIELD_TOKENS)


def sensitive_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in (
        _EMAIL_RE, _PHONE_RE, _CARD_RE, _SSN_RE, _JWT_RE, _AWS_KEY_RE,
        _BEARER_RE, _PEM_RE, _GITHUB_TOKEN_RE, _STRIPE_TOKEN_RE,
        _SLACK_TOKEN_RE, _GENERIC_TOKEN_RE, _SECRET_ASSIGN_RE,
    )) or url_has_password(text) or (
        _redact_auth_headers(text) != text or _redact_secret_keys(text) != text)


def redact_sensitive(value: Any, *, max_length: int = 512) -> str:
    text = str(value).replace("\x00", " ")[:max_length]
    # URL userinfo and credential assignments first, so their labels win over
    # a value pattern (an email address in a URL or an Authorization value).
    text = mask_url_userinfo(text)
    text = _redact_auth_headers(text)
    text = _redact_secret_keys(text)
    substitutions = (
        (_EMAIL_RE, "[REDACTED_EMAIL]"), (_PHONE_RE, "[REDACTED_PHONE]"),
        (_CARD_RE, "[REDACTED_NUMBER]"), (_SSN_RE, "[REDACTED_SSN]"),
        (_JWT_RE, "[REDACTED_TOKEN]"), (_AWS_KEY_RE, "[REDACTED_ACCESS_KEY]"),
        (_BEARER_RE, "[REDACTED_BEARER]"), (_PEM_RE, "[REDACTED_KEY_MATERIAL]"),
        (_GITHUB_TOKEN_RE, "[REDACTED_TOKEN]"), (_STRIPE_TOKEN_RE, "[REDACTED_TOKEN]"),
        (_SLACK_TOKEN_RE, "[REDACTED_TOKEN]"), (_GENERIC_TOKEN_RE, "[REDACTED_TOKEN]"),
    )
    for pattern, replacement in substitutions:
        text = pattern.sub(replacement, text)
    return _SECRET_ASSIGN_RE.sub(lambda match: f"{match.group(1)}=[REDACTED_SECRET]", text)


def markdown_literal(value: Any, *, max_length: int = 512) -> str:
    """Render untrusted dynamic text as literal Markdown, never syntax."""
    return _MARKDOWN_META_RE.sub(r"\\\1", redact_sensitive(value, max_length=max_length))


def safe_renderable_scalar(value: Any) -> bool:
    return not (isinstance(value, str) and sensitive_text(value))


def proposal_value_digest(value: str) -> str:
    """Persist review-safe equality metadata instead of raw typed values."""
    if not value:
        return ""
    return "sha256:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:24]
