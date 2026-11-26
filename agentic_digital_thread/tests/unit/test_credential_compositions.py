# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every separator inside a secret value, in every placement, through every
path that logs it, answers with it or registers it.

The same leak kept turning up in one more spelling: a raw space, tab, '/',
'?', '#' or line break inside a password, or a value folded onto the next
line. Like the SQL guard's compositions, this test builds them all. The
secret value is ``alpha<separator>bravo-s3cr3t`` for each of the
:data:`SEPARATORS`, written into each placement in :func:`compositions`:

- URL userinfo password (a DSN, an https URL to localhost, and a DSN to an IP
  address);
- an Authorization or Proxy-Authorization value: on the key line, mid-line,
  quoted (JSON and Python repr), on the next line, folded across lines (after
  a scheme alone, and after a trailing comma), as a YAML block and as a YAML
  sequence item;
- a password, client_secret, access_token or aws_secret_access_key value, the
  same ways.

Each composition runs through:

- the log paths: the PUBLIC_ERROR line, the QUERY_FAILURE line,
  ``_safe_detail``, ``failure_fingerprint.redact_secrets``, ``masked_head``
  and the two digit-masked heads beside it;
- the answer paths: ``redact_sensitive``, ``sensitive_text`` and
  ``markdown_literal``;
- for the URL placements, registration (``credential_url_error``) and the
  stored and display maskers.

No piece of the value (``alpha``, ``bravo``, ``s3cr3t``, or ``s#cr#t`` after
digit masking) may survive, and the line or field after the value must.

A placement writes the value the way its syntax can carry it. The quoted forms
carry every separator. An unquoted value carries a separator only where its
grammar keeps the value going, so these are not composed:

- a raw line break at the key's own indent in an unquoted value: the next line
  is the next key (YAML and HTTP header folding agree);
- a fold in an unquoted Authorization value on its key line: a complete value
  stops at its line end (a deeper line continues only after an empty value, a
  YAML block marker, a scheme alone, an unclosed quote, or a trailing comma or
  backslash);
- whitespace in an unquoted value whose key sits mid-line (prose, or a
  ``key=value`` list), and a quote or backslash in a mid-line unquoted
  Authorization value: there the value ends.

The negatives bound over-redaction on the answer paths: prose that says
"authorization" with a complete value then a deeper unrelated line, answers
with indented bullets, a quoted value that closes on its own line before a
deeper field, and URLs whose '@' is outside the authority. Offline.
"""
from __future__ import annotations

import logging
import re

import pytest

from agents import connection_security as cs
from agents import failure_fingerprint as fp
from agents import public_errors as pe
from agents import response_integrity_security as ris
from tests.unit.test_log_head_redaction import URL_AT_OUTSIDE_AUTHORITY, _Body, _state

SEPARATORS = {
    "space": " ", "tab": "\t", "cr": "\r", "lf": "\n", "crlf": "\r\n",
    "fold": "\n" + " " * 8, "crlf_fold": "\r\n" + " " * 8, "comma_fold": ",\n" + " " * 8,
    "comma": ",", "squote": "'", "dquote": '"', "backslash": "\\",
    "at": "@", "slash": "/", "question": "?", "hash": "#", "colon": ":",
}
#: Delimiter sequences a password can hold, composed into the URL placements
#: only: a nested scheme starts what looks like a second URL inside the first
#: one's userinfo, which a single separator never does.
URL_SEQUENCES = {
    "scheme": "://", "query_scheme": "?://", "slash_scheme": "/://",
    "space_scheme": " ://", "two_schemes": "://x://",
}
_ALL_SEPARATORS = {**SEPARATORS, **URL_SEQUENCES}
#: The secret's pieces, one per side of the separator, and the right-hand
#: piece as digit masking alone would leave it.
PIECES = ("alpha", "bravo", "s3cr3t", "s#cr#t")

_ALL = frozenset(SEPARATORS)
_LINE_BREAKS = frozenset({"lf", "crlf"})
#: Separators an unquoted credential token or bare value carries mid-line.
_IN_TOKEN = frozenset({"at", "slash", "question", "hash", "colon", "comma"})
_QUOTE_CHARS = frozenset({"squote", "dquote", "backslash"})
#: A line break followed by a deeper indent: a folded continuation line.
_FOLDS = frozenset({"fold", "crlf_fold", "comma_fold"})

_AUTH_KEYS = ("Authorization", "Proxy-Authorization")
_SECRET_KEYS = ("password", "client_secret", "access_token", "aws_secret_access_key")
_NEXT = "next: keep"


def _value(sep: str) -> str:
    return f"alpha{_ALL_SEPARATORS[sep]}bravo-s3cr3t"


def _dq(text: str) -> str:
    return '"' + text.replace('"', '\\"') + '"'


def _sq(text: str) -> str:
    return "'" + text.replace("'", "\\'") + "'"


def _auth_placements(key: str):
    """(name, separators, build(value) -> (text, keep)) for one Authorization key."""
    return [
        ("line_start", _ALL - _LINE_BREAKS - {"fold", "crlf_fold"},
         lambda v: (f"{key}: Basic {v}\n{_NEXT}", (f"{key}:", _NEXT))),
        ("line_start_dq", _ALL,
         lambda v: (f"{key}: {_dq('Basic ' + v)}\n{_NEXT}", (f"{key}:", _NEXT))),
        ("line_start_sq", _ALL,
         lambda v: (f"{key}: {_sq('Basic ' + v)}\n{_NEXT}", (f"{key}:", _NEXT))),
        ("mid_line", _IN_TOKEN,
         lambda v: (f"upstream refused {key}: Basic {v} at api.example.com\n{_NEXT}",
                    ("upstream refused", " at api.example.com", _NEXT))),
        ("json", _ALL,
         lambda v: (f'req {{"{key}": {_dq("Basic " + v)}, "Accept": "json"}}\n{_NEXT}',
                    ('"Accept": "json"', _NEXT))),
        ("repr", _ALL,
         lambda v: (f"req {{'{key}': {_sq('Basic ' + v)}, 'Accept': 'json'}}\n{_NEXT}",
                    ("'Accept': 'json'", _NEXT))),
        ("next_line", _ALL - _LINE_BREAKS,
         lambda v: (f"{key}:\n    Basic {v}\n{_NEXT}", (f"{key}:", _NEXT))),
        ("next_line_dq", _ALL,
         lambda v: (f"{key}:\n    {_dq('Basic ' + v)}\n{_NEXT}", (f"{key}:", _NEXT))),
        ("folded_after_scheme", _ALL - _LINE_BREAKS,
         lambda v: (f"{key}: Basic\n    {v}\n{_NEXT}", (f"{key}:", _NEXT))),
        ("folded_after_comma", _ALL - _LINE_BREAKS,
         lambda v: (f'{key}: Digest username="svc",\n    response={v}\n{_NEXT}',
                    (f"{key}:", _NEXT))),
        ("folded_after_comma_dq", _ALL,
         lambda v: (f'{key}: Digest username="svc",\n    response={_dq(v)}\n{_NEXT}',
                    (f"{key}:", _NEXT))),
        ("yaml_block", _ALL - _LINE_BREAKS,
         lambda v: (f"headers:\n  {key}: |\n    Basic {v}\n  Accept: json",
                    ("headers:", "Accept: json"))),
        ("yaml_item", _ALL - _LINE_BREAKS - {"fold", "crlf_fold"},
         lambda v: (f"- {key}: Basic {v}\n- Accept: json", ("- Accept: json",))),
    ]


def _secret_placements(key: str):
    """(name, separators, build(value) -> (text, keep)) for one secret key."""
    return [
        ("line_start", _ALL - _LINE_BREAKS,
         lambda v: (f"{key}: {v}\n{_NEXT}", (_NEXT,))),
        ("line_start_dq", _ALL,
         lambda v: (f"{key}: {_dq(v)}\n{_NEXT}", (_NEXT,))),
        ("line_start_sq", _ALL,
         lambda v: (f"{key}: {_sq(v)}\n{_NEXT}", (_NEXT,))),
        ("mid_line_equals", _IN_TOKEN | _QUOTE_CHARS | _FOLDS,
         lambda v: (f"login failed {key}={v}\n{_NEXT}", ("login failed", _NEXT))),
        ("mid_line_colon", _IN_TOKEN | _QUOTE_CHARS | _FOLDS,
         lambda v: (f"refresh failed {key}: {v}\n{_NEXT}", ("refresh failed", _NEXT))),
        ("json", _ALL,
         lambda v: (f'cfg {{"{key}": {_dq(v)}, "user": "svc"}}\n{_NEXT}',
                    ('"user": "svc"', _NEXT))),
        ("repr", _ALL,
         lambda v: (f"cfg {{'{key}': {_sq(v)}, 'user': 'svc'}}\n{_NEXT}",
                    ("'user': 'svc'", _NEXT))),
        ("next_line", _ALL - _LINE_BREAKS,
         lambda v: (f"{key}:\n    {v}\n{_NEXT}", (_NEXT,))),
        ("next_line_dq", _ALL,
         lambda v: (f"{key}:\n    {_dq(v)}\n{_NEXT}", (_NEXT,))),
        ("folded", _ALL - _LINE_BREAKS,
         lambda v: (f"{key}: lead\n    {v}\n{_NEXT}", (_NEXT,))),
        ("yaml_block", _ALL - _LINE_BREAKS,
         lambda v: (f"db:\n  {key}: |\n    {v}\n  host: db.example.com",
                    ("db:", "host: db.example.com"))),
        ("yaml_item", _ALL - _LINE_BREAKS,
         lambda v: (f"- {key}: {v}\n- user: svc", ("- user: svc",))),
    ]


def _url_placements():
    seps = _ALL | frozenset(URL_SEQUENCES)
    return [
        ("dsn", seps,
         lambda v: (f"connect postgresql://svc:{v}@db.example.com:5432/erp failed",
                    ("connect postgresql://", "@db.example.com", "/erp failed"))),
        ("https", seps,
         lambda v: (f"connect https://svc:{v}@localhost/x failed",
                    ("connect https://", "@localhost/x failed"))),
        ("ip", seps,
         lambda v: (f"connect postgresql://svc:{v}@10.0.0.12:5432/erp failed",
                    ("connect postgresql://", "/erp failed"))),
    ]


def _placements():
    for key in _AUTH_KEYS:
        for name, seps, build in _auth_placements(key):
            yield f"{key}.{name}", seps, build
    for key in _SECRET_KEYS:
        for name, seps, build in _secret_placements(key):
            yield f"{key}.{name}", seps, build
    for name, seps, build in _url_placements():
        yield f"url.{name}", seps, build


def compositions(only_urls: bool = False):
    """``(id, separator name, text, keep)`` for every composed message."""
    out = []
    for placement, seps, build in _placements():
        if only_urls and not placement.startswith("url."):
            continue
        for sep in _ALL_SEPARATORS:
            if sep in seps:
                text, keep = build(_value(sep))
                out.append((f"{placement}/{sep}", sep, text, keep))
    return out


_COMPOSITIONS = compositions()
_URL_COMPOSITIONS = compositions(only_urls=True)


def test_the_composition_count_is_what_the_report_states():
    assert len(SEPARATORS) == 17
    assert len(URL_SEQUENCES) == 5
    assert len(_COMPOSITIONS) == 1190
    assert len(_URL_COMPOSITIONS) == 66
    # Short enough for every head's cut (QUERY_FAILURE keeps 160 characters),
    # so a cut can never be what hides a piece.
    assert max(len(text) for _id, _sep, text, _keep in _COMPOSITIONS) < 160


def _unescaped(text: str) -> str:
    return text.replace("\\", "")


def _misses(path: str, fn, items, *, unescape: bool = False, keep_check: bool = True):
    misses = []
    for case_id, _sep, text, keep in items:
        out = fn(text)
        left = [piece for piece in PIECES if piece in out]
        lost = []
        if keep_check:
            lost = [k for k in keep
                    if (_unescaped(k) if unescape else k) not in re.sub(r"\s+", " ", out)
                    and (_unescaped(k) if unescape else k) not in out]
        if left or lost:
            misses.append(f"{path} {case_id}: left={left} lost={lost} out={out!r}")
    return misses


def _report(misses):
    assert not misses, f"{len(misses)} misses:\n" + "\n".join(misses[:40])


# -- log paths --------------------------------------------------------------------

def _public_error_line(caplog, text: str) -> str:
    caplog.clear()
    out = pe.public_error(RuntimeError(text), "unit.compose")
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith(f"PUBLIC_ERROR ref={out['error_ref']} ")]
    assert len(lines) == 1, lines
    return lines[0]


def _query_failure_line(caplog, text: str) -> str:
    from agents.chat_client import _stream_cloud_response
    caplog.clear()
    list(_stream_cloud_response({"response": _Body(RuntimeError(text))}, _state()))
    lines = [r.getMessage() for r in caplog.records if "QUERY_FAILURE" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


def _log_paths(caplog):
    from agents.chat_client import _redact_digits
    from agents.runtime_app import _redacted_error_head
    return {
        "public_error": lambda text: _public_error_line(caplog, text),
        "query_failure": lambda text: _query_failure_line(caplog, text),
        "safe_detail": lambda text: cs._safe_detail(text, {}),
        "redact_secrets": lambda text: fp.redact_secrets(text, len(text)),
        "masked_head": lambda text: fp.masked_head(text, 300),
        "chat_client_head": lambda text: _redact_digits(text, limit=300),
        "runtime_head": lambda text: _redacted_error_head(text, limit=300),
    }


_LOG_PATH_NAMES = ("public_error", "query_failure", "safe_detail", "redact_secrets",
                   "masked_head", "chat_client_head", "runtime_head")


def _log_compositions():
    """The compositions as a log line must keep them. A URL whose password holds
    a nested '://' does not parse, and a log masks such a URL whole from its
    authority on (where its password ends is unknown), so for the sequences a
    log keeps the text around the URL, not its host."""
    return [(case_id, sep, text, ("connect ", " failed") if sep in URL_SEQUENCES else keep)
            for case_id, sep, text, keep in _COMPOSITIONS]


@pytest.mark.parametrize("path", _LOG_PATH_NAMES)
def test_no_piece_of_a_composed_secret_reaches_a_log_line(path, caplog):
    caplog.set_level(logging.INFO, logger="agents.public_errors")
    caplog.set_level(logging.INFO, logger="agentcore_client")
    fn = _log_paths(caplog)[path]
    _report(_misses(path, fn, _log_compositions()))


# -- answer paths -----------------------------------------------------------------

def test_no_piece_of_a_composed_secret_reaches_an_answer():
    _report(_misses("redact_sensitive", ris.redact_sensitive, _COMPOSITIONS))


def test_no_piece_of_a_composed_secret_reaches_rendered_markdown():
    _report(_misses("markdown_literal", lambda t: _unescaped(ris.markdown_literal(t)),
                    _COMPOSITIONS, unescape=True))


def test_every_composed_secret_is_flagged_sensitive():
    unflagged = [case_id for case_id, _sep, text, _keep in _COMPOSITIONS
                 if not ris.sensitive_text(text)]
    assert not unflagged, f"{len(unflagged)} unflagged: {unflagged[:40]}"


# -- registration -----------------------------------------------------------------

#: Separators RFC 3986 allows in userinfo as written: a URL holding one is
#: well formed, and registration accepts it.
_USERINFO_CHARACTERS = frozenset({"comma", "squote", "colon"})


def test_registration_refuses_a_composed_url_unless_its_userinfo_is_well_formed():
    wrong = []
    for case_id, sep, text, _keep in _URL_COMPOSITIONS:
        url = text.split(" ", 1)[1].rsplit(" ", 1)[0]
        refused = bool(cs.credential_url_error(url))
        if refused == (sep in _USERINFO_CHARACTERS):
            wrong.append(f"{case_id}: refused={refused} {url!r}")
    assert not wrong, "\n".join(wrong)


def test_no_piece_of_a_composed_url_is_stored_or_shown():
    paths = {
        "strip_url_credentials": cs._strip_url_credentials,
        "mask_stored_url": cs.mask_stored_url,
        "mask_connection": lambda url: str(cs.mask_connection({"connection_string": url})),
    }
    items = [(case_id, sep, text.split(" ", 1)[1].rsplit(" ", 1)[0], keep)
             for case_id, sep, text, keep in _URL_COMPOSITIONS]
    misses = []
    for name, fn in paths.items():
        misses += _misses(name, fn, items, keep_check=False)
    _report(misses)


# -- negatives: what an answer keeps -----------------------------------------------

#: name -> (answer text, needles that must stay). Each holds no credential the
#: answer must keep, or one whose value ends before the needles.
NEGATIVES = {
    "prose_authorization_then_deeper_line": (
        "Work authorization: WA-1234 approved\n  owner: maintenance\nnext: keep",
        ("Work authorization:", "owner: maintenance", "next: keep")),
    "header_authorization_then_deeper_field": (
        "Authorization: Bearer completeTok\n    X-Trace: kept-trace\nnext: keep",
        ("Authorization:", "X-Trace: kept-trace", "next: keep")),
    "line_start_authorization_then_deeper_line": (
        "Authorization: approved by the plant manager\n  owner: maintenance\nnext: keep",
        ("Authorization:", "owner: maintenance", "next: keep")),
    "apostrophes_after_an_authorization": (
        "Authorization: don't share it\nThe shift lead's badge is 'B-12'\nnext: keep",
        ("The shift lead's badge is 'B-12'", "next: keep")),
    "bullets_after_an_authorization": (
        "Next steps:\n- Confirm the work authorization: WA-1234\n  - owner: maintenance\n"
        "  - due: Friday\n- Restart line 2 after the check",
        ("Next steps:", "  - owner: maintenance", "  - due: Friday",
         "- Restart line 2 after the check")),
    "quoted_authorization_closed_then_deeper_field": (
        'req {"Authorization": "Bearer closedTok",\n      "X-Trace": "kept-trace"}',
        ('"X-Trace": "kept-trace"',)),
    "quoted_secret_closed_then_deeper_field": (
        'cfg {"client_secret": "closedSec",\n      "user": "svc"}',
        ('"user": "svc"',)),
    "yaml_quoted_secret_closed_then_deeper_field": (
        'db:\n  password: "closedSec"\n    note: kept\n  host: db.example.com',
        ("note: kept", "host: db.example.com")),
}

#: Answers that hold no credential at all: they come back exactly as written.
EXACT_NEGATIVES = URL_AT_OUTSIDE_AUTHORITY + (
    "Line 2 summary:\n- OEE is 82 percent\n  - availability 90 percent\n"
    "  - performance 95 percent\n- Next shift: B",
    "GET https://localhost:8080/users/@ops-team returned not found",
    "Open https://api.example.com/v1/spec then ask the owner",
    # A host, or host and port, before whitespace is never joined to the next
    # token: these name a URL and then a local user, not a credential URL.
    "Open https://example.com ops@localhost for status.",
    "Open https://example.com ops@10.0.0.5 for status.",
    "Open https://example.com:8443\nops@localhost owns it.",
    "Open postgresql://db.example.com ops@localhost for status.",
)


@pytest.mark.parametrize("name", sorted(NEGATIVES))
def test_an_answer_keeps_what_follows_a_credential(name):
    text, keep = NEGATIVES[name]
    for out in (ris.redact_sensitive(text), _unescaped(ris.markdown_literal(text))):
        for needle in keep:
            assert _unescaped(needle) in _unescaped(out), (name, needle, out)


@pytest.mark.parametrize("text", EXACT_NEGATIVES)
def test_an_answer_without_a_credential_is_unchanged(text):
    assert ris.redact_sensitive(text) == text
    assert _unescaped(ris.markdown_literal(text)) == text
    assert not ris.sensitive_text(text)
