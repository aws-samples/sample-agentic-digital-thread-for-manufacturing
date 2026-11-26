# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The shared redactor catches every Authorization scheme and prefixed secret keys.

``response_integrity_security.redact_sensitive`` (and ``markdown_literal``,
which chat answer rendering uses) redacted ``Authorization: Bearer <12+
chars>`` and ``password=``/``secret=``/``token=`` style assignments only where
the key started a word. So ``Authorization: Basic <credential>`` lost only the
word ``Basic``, and ``client_secret=<short>``, ``dbPassword="..."`` or
``AWS_SECRET_ACCESS_KEY=...`` were left as written. ``sensitive_text`` uses the
same patterns, so those values also passed the rendered-answer scan.

The table is the log-path table (``test_log_head_redaction.CREDENTIAL_FORMS``)
so both paths pin the same forms, including URL userinfo with a raw '@' or ':'
in the password, Authorization values inside escaped JSON strings, and values
folded onto following lines or written as YAML blocks. Answers stay aligned
with a URL parser: an '@' outside the authority is left as written. Each
redaction step must also stay linear: each is timed on 256,000-character
inputs built from key-like, separator, '@'/':', escaped-quote and folded-line
runs.
"""
from __future__ import annotations

import time

import pytest

from agents import response_integrity_security as ris
from tests.unit.test_log_head_redaction import (
    ANSWER_ONLY_FORMS, CREDENTIAL_FORMS, PROSE_NEGATIVES, URL_AT_OUTSIDE_AUTHORITY)

_FORMS = {**CREDENTIAL_FORMS, **ANSWER_ONLY_FORMS}
_IDS = sorted(_FORMS)


def _unescaped(text: str) -> str:
    """``markdown_literal`` output with its escaping backslashes removed; the
    needles are compared the same way."""
    return text.replace("\\", "")


def _check(out: str, case: str, *, unescape: bool = False) -> None:
    _message, secrets, keep = _FORMS[case]
    for secret in secrets:
        assert secret not in out, (case, secret, out)
    for needle in keep:
        needle = _unescaped(needle) if unescape else needle
        assert needle in out, (case, needle, out)


@pytest.mark.parametrize("case", _IDS)
def test_redact_sensitive_removes_the_credential(case):
    message, secrets, _keep = _FORMS[case]
    _check(ris.redact_sensitive(message), case)
    if secrets:
        assert ris.sensitive_text(message)


@pytest.mark.parametrize("case", _IDS)
def test_markdown_literal_removes_the_credential(case):
    message = _FORMS[case][0]
    _check(_unescaped(ris.markdown_literal(message)), case, unescape=True)


@pytest.mark.parametrize("text", PROSE_NEGATIVES)
def test_prose_naming_a_credential_is_left_alone(text):
    assert ris.redact_sensitive(text) == text
    assert _unescaped(ris.markdown_literal(text)) == text
    assert not ris.sensitive_text(text)


@pytest.mark.parametrize("text", URL_AT_OUTSIDE_AUTHORITY)
def test_an_answer_keeps_a_url_whose_at_is_outside_the_authority(text):
    """Answers stay aligned with a URL parser: an '@' in a path, query or
    fragment is not userinfo, so the URL comes back exactly as written and the
    answer is not flagged (an enforce-mode answer would otherwise fall back)."""
    assert ris.redact_sensitive(text) == text
    assert _unescaped(ris.markdown_literal(text)) == text
    assert not ris.sensitive_text(text)


def test_an_answer_masks_a_user_name_but_flags_only_a_password():
    """A user name alone may be an opaque token, so it is masked; only a
    password part (a ':' in the userinfo) makes the text sensitive, so an
    answer naming http://admin@host is not rejected."""
    user_only = "the health check calls http://admin@localhost:8080/health"
    assert ris.redact_sensitive(user_only) == (
        "the health check calls http://***@localhost:8080/health")
    assert not ris.sensitive_text(user_only)
    with_password = "the cache is redis://:cache-pass@cache.example.com:6379/0"  # nosec B105 - a redaction test input, not a credential
    assert "cache-pass" not in ris.redact_sensitive(with_password)
    assert ris.sensitive_text(with_password)


def test_max_length_still_cuts_before_redacting():
    assert ris.redact_sensitive("a" * 600) == "a" * 512
    early = "client_secret=early " + "a" * 600
    assert ris.redact_sensitive(early) == "client_secret=[REDACTED_SECRET] " + "a" * 492
    late = "a " * 300 + "client_secret=late"
    assert ris.redact_sensitive(late) == ("a " * 300)[:512]
    short = ris.markdown_literal("Authorization: Basic dXNlcjpwYXNz", max_length=100)
    assert "dXNlcjpwYXNz" not in _unescaped(short)


# -- linear time on adversarial input ---------------------------------------------

_N = 256_000


def _run(unit: str) -> str:
    return (unit * (_N // len(unit) + 1))[:_N]


_ADVERSARIAL = {
    "key_chars": "a" * _N,
    "snake_run": _run("a_"),
    "kebab_run": _run("a-"),
    "camel_run": _run("aB"),
    "screaming_run": "A" * _N,
    "key_names": _run("passwordclient_secretaccess_tokenx-api-key"),
    "partial_aws_key": _run("aws_secret_access_"),
    "key_equals": _run("password="),
    "key_colon_quote": _run('password:"'),
    "unclosed_quote": 'password="' + "a" * _N,
    "key_then_spaces": "password" + " " * _N,
    "separator_then_spaces": "password=" + " " * _N,
    "separators": _run(':= "\'\\'),
    "auth_words": _run("authorization"),
    "auth_colons": _run("Authorization:"),
    "auth_scheme_runs": _run("Authorization: Basic "),
    "auth_scheme_then_spaces": "Authorization: Basic" + " " * _N,
    "digest_params": "Authorization: Digest " + _run("a=b, "),
    "digest_unclosed": "Authorization: Digest " + _run('a="'),
    "proxy_runs": _run("Proxy-Authorization:"),
    "auth_then_key_chars": "Authorization: " + "a" * _N,
    "auth_then_param_name": "Authorization: Digest " + _run("a.") + "=",
    "camel_then_core_fragments": _run("aBpasswor"),
    "screaming_then_core_fragments": _run("APASSWOR"),
    "quoted_value_runs": _run("password='a"),
    # URL userinfo
    "at_runs": "@" * _N,
    "colon_runs": ":" * _N,
    "at_colon_runs": _run("@:"),
    "scheme_then_at_colon": "://" + _run("@:"),
    "scheme_then_key_chars": "://" + "a" * _N,
    "scheme_runs": _run("://"),
    "scheme_space_at_runs": _run(":// a@"),
    "userinfo_without_at": "postgresql://" + _run("a:b?c#d "),
    # escaped quotes
    "backslash_runs": "\\" * _N,
    "auth_then_backslashes": "Authorization" + "\\" * _N,
    "auth_backslash_quote_runs": "Authorization: " + _run('\\"'),
    "auth_unclosed_escaped": 'Authorization: \\"Digest ' + "a" * _N,
    "auth_inner_quotes_unclosed": 'Authorization: \\\\\\"Digest ' + _run('x=\\"y\\", '),
    "auth_quoted_repeats": _run('"Authorization": "'),
    "auth_escaped_repeats": _run('\\"Authorization\\": \\"'),
    "auth_rising_escapes": "".join(
        'Authorization: ' + "\\" * i + '"Digest a ' for i in range(1, 720))[:_N],
    "secret_unclosed_escaped": 'password=\\\\\\"' + _run('a\\"'),
    "secret_escaped_repeats": _run('\\"password\\": \\"'),
    "secret_rising_escapes": "".join(
        "password=" + "\\" * i + '"a ' for i in range(1, 720))[:_N],
    # values folded onto following lines
    "auth_folded_lines": "Authorization: Basic a\n" + _run(" x\n"),
    "auth_next_line_runs": _run("Authorization:\n"),
    "auth_yaml_block_blank_lines": "  Authorization: |\n" + "\n" * _N,
    "auth_yaml_markers": _run("Authorization: |\n"),
    "auth_keys_then_deep_line": _run("Authorization: a ") + "\n" + " " * 1_000 + "x",
    "auth_crlf_runs": _run("Authorization: Basic\r\n x\r\n"),
    "auth_deep_indent_lines": "Authorization: Basic a\n" + _run(" " * 999 + "x\n"),
    "secret_next_line_runs": _run("password:\n"),
    "secret_yaml_block_blank_lines": "  password: |\n" + "\n" * _N,
    "secret_keys_one_line": _run("password: "),
    # log masking: '@' and ':' runs after one scheme, many schemes in a token
    "token_scheme_runs_no_at": _run("a://"),
    "token_scheme_then_at_runs": "a://" + "@" * _N,
    "token_unparseable_runs": _run("h://u:p?"),
    # header and YAML lines (the value is the rest of the line), quotes that
    # close on a later line, ',' tails, and URLs whose authority a raw space,
    # line break or '/' in the password ends early
    "auth_line_start_runs": _run("Authorization: a b\n"),
    "secret_line_start_runs": _run("password: a b\n"),
    "auth_quote_next_line_runs": _run('Authorization: "a\n'),
    "secret_quote_next_line_runs": _run('password: "a\n'),
    "secret_quote_rising_depth_lines": "".join(
        "password: " + "\\" * i + '"a\n' for i in range(1, 720))[:_N],
    "auth_quote_then_escaped_quotes": 'Authorization: "a\n' + _run('\\"\n'),
    "secret_comma_tail": "password=" + _run("a,"),
    "secret_comma_key_runs": "password=" + _run("a,b="),
    "auth_comma_tail": "upstream Authorization: Basic " + _run("a,"),
    "auth_comma_lines": _run("Authorization: a,\n"),
    "quote_openers_one_line": "password: " + _run("= '"),
    "url_space_hop_runs": _run("x://a:b c@"),
    "url_line_cut_runs": _run("x://a:b\nc@"),
    "url_slash_cut_runs": _run("x://a:b/c@"),
    "url_slash_cut_no_at": "x://a:b/" + "c" * _N,
    "url_space_no_at_runs": _run("x://a:b c "),
    # a password with a nested '://': credential-shaped authorities cut by one
    # '://' after another in a single token, with and without a later '@'
    "url_nested_scheme_runs_no_at": _run("x://a:b"),
    "url_nested_scheme_then_at": _run("x://a:b") + "@h",
    "url_space_nested_scheme_runs": _run("x://a:b ://c"),
    "url_slash_nested_scheme_runs": _run("x://a:b/://"),
    # a quote that closes on the next line, key after key down a staircase
    "secret_staircase_unclosed": "".join(" " * i + 'password: "a\n' for i in range(720))[:_N],
    "auth_staircase_unclosed": "".join(
        " " * i + 'Authorization: "a\n' for i in range(720))[:_N],
    "midline_rising_depth": "".join(
        "x password=" + "\\" * i + '"b\n' for i in range(1, 720))[:_N],
}

#: The redaction steps this module owns, timed one by one (redact_sensitive
#: also runs the older email pattern, which is bounded by max_length).
_TIMED = [
    ("agents.response_integrity_security", "mask_url_userinfo"),
    ("agents.response_integrity_security", "url_has_password"),
    ("agents.response_integrity_security", "_redact_auth_headers"),
    ("agents.response_integrity_security", "_redact_secret_keys"),
    ("agents.connection_security", "_mask_url"),
    ("agents.connection_security", "mask_url_credentials_for_log"),
    ("agents.connection_security", "mask_stored_url"),
    ("agents.connection_security", "credential_url_error"),
    ("agents.response_integrity_security", "redact_credential_values"),
]


@pytest.mark.parametrize("module, name", _TIMED, ids=[name for _m, name in _TIMED])
def test_each_redaction_step_is_linear_on_adversarial_input(module, name):
    import importlib
    step = getattr(importlib.import_module(module), name)
    slowest = ("", 0.0)
    for case, text in _ADVERSARIAL.items():
        assert len(text) >= _N - 1_000, case
        started = time.perf_counter()
        step(text)
        elapsed = time.perf_counter() - started
        slowest = max(slowest, (case, elapsed), key=lambda pair: pair[1])
    assert slowest[1] < 1.0, slowest
