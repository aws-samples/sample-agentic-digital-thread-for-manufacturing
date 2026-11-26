# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Credentials in an exception message never reach a log line's message head.

The log heads (``PUBLIC_ERROR``, the ``exception_fingerprint`` head behind
``QUERY_FAILURE``, and the digit-masked heads ``chat_client._redact_digits`` and
``runtime_app._redacted_error_head``) used to flatten whitespace and mask
digits only, so a connection-string password, a ``password=`` assignment, a
bearer token, a JWT or an access key id in an exception message was logged as
written. Every head now runs the app's existing redactors
(``connection_security._mask_url``, then
``response_integrity_security.redact_sensitive``) over the WHOLE message before
digit masking and truncation, on the default path with no call-site scrub.
The credential forms (:data:`CREDENTIAL_FORMS`: Authorization under any
scheme, prefixed secret keys of any value length, values folded onto
following lines or written as YAML blocks) are checked through every head too,
and prose that only names a credential stays as written. Log lines also mask a
URL conservatively, from '://' through the last '@' on its line, so a raw '/',
'?', '#' or whitespace in a password cannot leak (:data:`LOG_ONLY_FORMS`).
test_credential_compositions.py composes every separator with every placement.

The secret values have no digits where possible; where they do, the
digit-masked form is checked as well, since masking alone must not count as
redaction. Offline: exceptions are built locally.
"""
from __future__ import annotations

import base64
import logging
import re

import pytest

from agents import failure_fingerprint as fp
from agents import public_errors as pe


def _b64url(text: str) -> str:
    """Unpadded base64url of *text*: how each JWT segment is encoded."""
    return base64.urlsafe_b64encode(text.encode()).rstrip(b"=").decode()


#: The unsigned test JWT's three segments, built here rather than written out.
_JWT_HEADER = _b64url('{"alg":"HS256"}')
_JWT_PAYLOAD = _b64url('{"sub":"worker"}')
_JWT_SIGNATURE = _b64url("signature-worker")

#: name -> (exception message, secret values, non-secret text that must stay)
_CASES = {
    "postgres_url_password": (
        "connect postgresql://adt_app:dsn-pass-wxyz@db.example.com:5432/erp "
        "failed while loading orders",
        ("dsn-pass-wxyz",),
        ("db.example.com", "failed while loading orders"),
    ),
    "password_assignment": (
        "login rejected for orders-loader password=plain-pass-wxyz host=db.example.com",
        ("plain-pass-wxyz",),
        ("login rejected for orders-loader", "host=db.example.com"),
    ),
    "authorization_bearer": (
        "upstream refused Authorization: Bearer bearer-tok-abcdefghij from api.example.com",
        ("bearer-tok-abcdefghij",),
        ("upstream refused", "from api.example.com"),
    ),
    "jwt": (
        f"token rejected: {_JWT_HEADER}.{_JWT_PAYLOAD}.{_JWT_SIGNATURE} by auth.example.com",
        (_JWT_HEADER, _JWT_PAYLOAD, _JWT_SIGNATURE),
        ("token rejected", "by auth.example.com"),
    ),
    "aws_access_key_id": (
        "sts call failed for key AKIAIOSFODNN7EXAMPLE in the orders account",
        ("AKIAIOSFODNN7EXAMPLE",),
        ("sts call failed for key", "in the orders account"),
    ),
    "url_query_secret": (
        "fetch https://api.example.com/openapi.json?api_key=query-key-wxyz refused",
        ("query-key-wxyz",),
        ("fetch https://api.example.com/openapi.json",),
    ),
}

#: Credential forms the shared redactor (response_integrity_security) must
#: catch: an Authorization or Proxy-Authorization value under any scheme, and
#: secret keys with a snake, camel or kebab prefix, for values of any length,
#: '=' or ':', quoted or not. Same shape as _CASES; the response-integrity
#: tests (test_response_integrity_credential_forms.py) reuse this table.
CREDENTIAL_FORMS = {
    "auth_basic": (
        "upstream refused Authorization: Basic dXNlcjpwYXNzd29yZA== at api.example.com",
        ("dXNlcjpwYXNzd29yZA",),
        ("upstream refused Authorization: Basic ", " at api.example.com"),
    ),
    "auth_digest": (
        'retry Authorization: Digest username="svc-reader", realm="plant floor", '
        'nonce="noncevalue", response="digestresponse" failed',
        ("svc-reader", "plant floor", "noncevalue", "digestresponse"),
        ("retry Authorization: Digest ", " failed"),
    ),
    "auth_bare_token": (
        "header Authorization: baretokenvalue sent",
        ("baretokenvalue",),
        ("header Authorization: ",),
    ),
    "auth_bare_base64_token": (
        "header Authorization: tok+val/abc= rejected",
        ("tok+val/abc",),
        ("header Authorization: ", " rejected"),
    ),
    "proxy_authorization": (
        "proxy said Proxy-Authorization: Basic cHJveHk6c2VjcmV0 denied",
        ("cHJveHk6c2VjcmV0",),
        ("proxy said Proxy-Authorization: Basic ", " denied"),
    ),
    "auth_quoted_header": (
        "headers {'Authorization': 'Bearer shorttok', 'Accept': 'json'}",
        ("shorttok",),
        ("'Authorization': 'Bearer ", "'Accept': 'json'"),
    ),
    "client_secret_short": (
        "token exchange failed client_secret=shortsec grant=client_credentials",
        ("shortsec",),
        ("token exchange failed client_secret=", " grant=client_credentials"),
    ),
    "access_token_colon": (
        "refresh failed access_token: shortacc expires=soon",
        ("shortacc",),
        ("refresh failed access_token: ", " expires=soon"),
    ),
    "oauth_tokens": (
        "oauth reply refresh_token=rtokvalue id_token=itokvalue session_token=stokvalue end",
        ("rtokvalue", "itokvalue", "stokvalue"),
        ("oauth reply refresh_token=", " end"),
    ),
    "aws_secret_keys": (
        "env AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY "
        "AWS_SESSION_TOKEN=sesstokvalue loaded",
        ("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "sesstokvalue"),
        ("env AWS_SECRET_ACCESS_KEY=", " loaded"),
    ),
    "api_key_forms": (
        "client sent api_key=akone apikey=aktwo x-api-key: akthree clientApiKey=akfour",
        ("akone", "aktwo", "akthree", "akfour"),
        ("client sent api_key=", "x-api-key"),
    ),
    "private_key_forms": (
        "loaded private_key=pkvalue and privateKey: pkcamel",
        ("pkvalue", "pkcamel"),
        ("loaded private_key=", " and privateKey: "),
    ),
    "password_forms": (
        'db login failed dbPassword="pw with spaces" PGPASSWORD=pgpwvalue passwd=pwdvalue',
        ("pw with spaces", "pgpwvalue", "pwdvalue"),
        ("db login failed dbPassword=",),
    ),
    "kebab_and_json": (
        'reply {"client-secret": "kebabsec", "refresh_token":"jsonrt", "user": "svc"}',
        ("kebabsec", "jsonrt"),
        ('reply {"client-secret": ', '"user": "svc"'),
    ),
    "escaped_json": (
        r'body {\"Authorization\": \"Basic ZXNjOnBhc3M\", \"client_secret\": \"esc sec\"}',
        ("ZXNjOnBhc3M", "esc sec"),
        (r'body {\"Authorization\": \"Basic ', r'\"client_secret\": '),
    ),
    "auth_sigv4": (
        "signed Authorization: AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=sigvaluewxyz sent",
        ("AKIDEXAMPLE", "SignedHeaders", "sigvaluewxyz"),
        ("signed Authorization: AWS", "-HMAC-SHA", " sent"),
    ),
    # URL userinfo ends at the LAST '@' of the authority (what URL parsers
    # take), so a raw '@' or ':' in a password is masked too.
    "dsn_multi_at": (
        "connect postgresql://svc:alpha@bravo:charlie@db.example.com:5432/erp failed",
        ("alpha", "bravo", "charlie"),
        ("connect postgresql://", "@db.example.com:", "/erp failed"),
    ),
    "dsn_percent_encoded": (
        "connect postgresql://svc:al%40pha%3Abravo@db.example.com/erp failed",
        ("al%40pha", "pha%3Abravo", "svc:al"),
        ("connect postgresql://", "@db.example.com/erp failed"),
    ),
    "dsn_ipv6": (
        "connect postgresql://svc:ipv-pass@[::1]:5432/erp failed",
        ("ipv-pass", "svc:ipv"),
        ("connect postgresql://", "@[::", "/erp failed"),
    ),
    "url_mysql_mongodb": (
        "retry mysql://app:my-pass@db.example.com/a and "
        "mongodb+srv://app:mongo-pass@cluster.example.com/b",
        ("my-pass", "mongo-pass"),
        ("retry mysql://", "@db.example.com/a and mongodb+srv://", "@cluster.example.com/b"),
    ),
    "url_redis_amqp": (
        "retry redis://:redis-pass@cache.example.com:6379/0 and amqp://app:amqp-pass@mq.example.com/vh",
        ("redis-pass", "amqp-pass"),
        ("retry redis://", "@cache.example.com:", " and amqp://", "@mq.example.com/vh"),
    ),
    "url_https_userinfo": (
        "fetch https://app:web-pass@api.example.com/v1/spec refused",
        ("web-pass", "app:web"),
        ("fetch https://", "@api.example.com/v", "/spec refused"),
    ),
    # An '@' in the query is not userinfo: host and path stay (the address
    # itself is redacted as an email address by the shared redactor).
    # Answer path only (ANSWER_ONLY_FORMS): log lines mask a URL conservatively
    # through its last '@' (test_log_heads_mask_an_at_anywhere_in_a_url).
    # Authorization values inside JSON strings inside the message: the value
    # opens with an escaped quote and its parameters use a deeper escape.
    "digest_nested_json": (
        r'upstream said {"headers": "{\"Authorization\": \"Digest username=\\\"svcReader\\\", '
        r'nonce=\\\"nonceValue\\\", response=\\\"digestResponse\\\"\"}"}',
        ("svcReader", "nonceValue", "digestResponse"),
        ('upstream said {"headers": ', "Authorization", "Digest "),
    ),
    "sigv4_double_nested": (
        r'signed {"req": "{\"h\": \"{\\\"Authorization\\\": \\\"AWS4-HMAC-SHA256 '
        r'Credential=AKIDEXAMPLE/aws4_request, SignedHeaders=host;x-amz-date, '
        r'Signature=sigvaluewxyz\\\"}\"}"}',
        ("AKIDEXAMPLE", "SignedHeaders", "sigvaluewxyz"),
        ('signed {"req": ', "Authorization", "AWS", "-HMAC-SHA"),
    ),
    "auth_quoted_unknown_scheme": (
        'hdr {"Authorization": "Custom partone parttwo", "Accept": "json"}',
        ("partone", "parttwo"),
        ('hdr {"Authorization": ', '"Accept": "json"'),
    ),
    "auth_unclosed_escaped": (
        r'log {\"Authorization\": \"Digest username=\\\"svcReader\\\", nonce=\\\"abcnonce'
        "\nnext line kept",
        ("svcReader", "abcnonce"),
        ("log {", "Digest ", "next line kept"),
    ),
    "secret_double_nested": (
        r'body {"cfg": "{\"inner\": \"{\\\"client_secret\\\": \\\"dbl sec value\\\", '
        r'\\\"user\\\": \\\"svc\\\"}\"}"}',
        ("dbl sec value",),
        ('body {"cfg": ', "client_secret", "user", "svc"),
    ),
    # An Authorization value folded onto following lines (obsolete header
    # folding), on the next line, as a YAML block scalar, or with CRLF line
    # ends: every continuation line indented deeper than the key line is part
    # of the value. The next line at the key's own indentation stays.
    "auth_folded_digest": (
        'upstream refused Authorization: Digest username="svcFold",\n'
        ' nonce="foldedNonce",\n response="foldedResponse"\nAccept: json',
        ("svcFold", "foldedNonce", "foldedResponse"),
        ("upstream refused Authorization: Digest ", "Accept: json"),
    ),
    "auth_next_line_basic": (
        "upstream refused Authorization:\n  Basic foldedBasicSecret\nAccept: json",
        ("foldedBasicSecret",),
        ("upstream refused Authorization:", "Accept: json"),
    ),
    "auth_yaml_literal": (
        "headers:\n  Authorization: |\n    Basic yamlBlockSecret\n  Accept: json",
        ("yamlBlockSecret",),
        ("headers:", "Authorization:", "Accept: json"),
    ),
    "auth_yaml_folded_blank_line": (
        "headers:\n  Authorization: >-\n    Digest username=svcYaml,\n\n"
        "    response=yamlResponse\n  Accept: json",
        ("svcYaml", "yamlResponse"),
        ("headers:", "Authorization:", "Accept: json"),
    ),
    "auth_crlf_folded": (
        "reply\r\nProxy-Authorization: Basic\r\n  crlfBasicSecret\r\nAccept: json",
        ("crlfBasicSecret",),
        ("Proxy-Authorization:", "Accept: json"),
    ),
    "auth_sigv4_folded": (
        "signed Authorization: AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/aws4_request,\n"
        "\tSignedHeaders=host;x-amz-date,\n\tSignature=foldsigvalue\nnext: kept",
        ("AKIDEXAMPLE", "SignedHeaders", "foldsigvalue"),
        ("signed Authorization: AWS", "next: kept"),
    ),
    # A quoted value that closes on its line takes no continuation: the deeper
    # line after it is another field.
    "auth_quoted_closed_then_deeper_line": (
        'req {"Authorization": "Bearer closedTokenValue",\n      "X-Trace": "kept-trace"}',
        ("closedTokenValue",),
        ('req {"Authorization": "Bearer ', '"X-Trace": "kept-trace"'),
    ),
    # The same for a secret key with its value on the following lines.
    "secret_yaml_block": (
        "db:\n  password: |\n    yamlPasswordValue\n  host: db.example.com",
        ("yamlPasswordValue",),
        ("db:", "password", "host: db.example.com"),
    ),
    "secret_next_line": (
        "creds:\n  client_secret:\n    nextLineSecretValue\n  user: svc",
        ("nextLineSecretValue",),
        ("creds:", "client_secret:", "user: svc"),
    ),
}

#: Forms log lines must mask and answers deliberately do not (answers stay
#: aligned with a URL parser): a raw '/', '?' or '#' in a password ends the
#: parsed authority early, so only the log path's conservative rule, through
#: the last '@' of the URL, catches them. The remnant the at-rest stripper
#: kept for a raw '?' before registration refused it has no '@' left; it does
#: not parse, so its whole authority is masked.
LOG_ONLY_FORMS = {
    "dsn_raw_slash": (
        "connect postgresql://svcUser:alpha/bravo@db.example.com/erp failed",
        ("svcUser", "alpha", "bravo"),
        ("connect postgresql://", " failed"),
    ),
    "url_raw_question": (
        "fetch https://svcUser:qrst?uvwx@host.example.com/v failed",
        ("svcUser", "qrst", "uvwx"),
        ("fetch https://", " failed"),
    ),
    "url_raw_hash": (
        "fetch https://svcUser:qrst#uvwx@host.example.com/v failed",
        ("svcUser", "qrst", "uvwx"),
        ("fetch https://", " failed"),
    ),
    "stored_raw_question_remnant": (
        "persist https://svcUser:qrst?uvwx%40host.example.com%2Fx= failed",
        ("svcUser", "qrst", "uvwx"),
        ("persist https://", " failed"),
    ),
}

#: Answer-path forms only (test_response_integrity_credential_forms).
ANSWER_ONLY_FORMS = {
    "url_query_at": (
        "GET https://api.example.com/v1/users?email=ops@example.com returned not found",
        (),
        ("GET https://api.example.com/v", "/users?email=", " returned not found"),
    ),
}
_CASES.update(CREDENTIAL_FORMS)
_CASES.update(LOG_ONLY_FORMS)
_IDS = sorted(_CASES)

#: Prose and bare key names with no assigned value: left exactly as written.
PROSE_NEGATIVES = (
    "the access token expired before the client secret rotation finished",
    "the secret column holds a salted hash",
    "field=client_secret; source=erp; field=password",
    "password_hash: argon and isValidToken: true",
    "Authorization header missing; AuthorizationError: request denied",
    "connect https://[fe::ab]/health refused",
    "connect postgresql://db.example.com/erp refused",
    'headers {"Authorization": "", "Accept": "json"}',
)

#: URLs with an '@' outside the authority. Answers leave them exactly as
#: written and do not flag them; log lines mask them through the last '@'.
URL_AT_OUTSIDE_AUTHORITY = (
    "See https://h.example.com?owner=a@b for the owner.",
    "GET https://api.example.com/users/@ops-team returned not found",
    "open https://h.example.com/feed#@latest now",
)


def _forms(secret: str) -> set[str]:
    """The secret as written and as digit masking alone would leave it."""
    return {secret, re.sub(r"\d", "#", secret)}


def _assert_redacted(text: str, case: str) -> None:
    _message, secrets, keep = _CASES[case]
    for secret in secrets:
        for form in _forms(secret):
            assert form not in text, (case, form, text)
    for needle in keep:
        assert needle in text, (case, needle, text)


def _fail_with(message: str) -> None:
    try:
        raise ConnectionRefusedError("refused")
    except ConnectionRefusedError as inner:
        raise RuntimeError(message) from inner


def _raised(message: str) -> RuntimeError:
    with pytest.raises(RuntimeError) as info:
        _fail_with(message)
    return info.value


# -- PUBLIC_ERROR, default path (no call-site scrub) -----------------------------

@pytest.mark.parametrize("case", _IDS)
def test_public_error_line_redacts_credentials_on_the_default_path(case, caplog):
    caplog.set_level(logging.WARNING, logger="agents.public_errors")
    out = pe.public_error(_raised(_CASES[case][0]), "unit.redaction")
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith(f"PUBLIC_ERROR ref={out['error_ref']} ")]
    assert len(lines) == 1, lines
    line = lines[0]
    _assert_redacted(line, case)
    # Everything an operator searches by stays on the line.
    assert line.startswith(f"PUBLIC_ERROR ref={out['error_ref']} where=unit.redaction "
                           f"category={out['error_category']} "), line
    assert "exc_type=RuntimeError" in line
    assert "cause=ConnectionRefusedError" in line
    assert re.search(r" where=\S*test_log_head_redaction\.py:\d+:_fail_with", line), line
    # And the response itself never carried any of it.
    _message, secrets, _keep = _CASES[case]
    assert not any(form in out["error"] for s in secrets for form in _forms(s))


# -- exception_fingerprint head (QUERY_FAILURE and the other fingerprint lines) --

@pytest.mark.parametrize("case", _IDS)
def test_exception_fingerprint_head_redacts_credentials(case):
    out = fp.exception_fingerprint(_raised(_CASES[case][0]))
    _assert_redacted(out["head"], case)
    assert out["exc_type"] == "RuntimeError"
    assert out["cause"] == "ConnectionRefusedError"
    assert out["where"].endswith(":_fail_with")


class _Body:
    def __init__(self, exc):
        self._exc = exc

    def iter_lines(self, chunk_size=None):
        raise self._exc
        yield  # pragma: no cover - makes this a generator

    def close(self):
        pass


def _state():
    from agents.chat_client import ThinkingFilter, _CloudStreamState
    return _CloudStreamState(
        ThinkingFilter(), integrity_mode=False,
        log=logging.getLogger("agentcore_client"),
        owner_id="u", session_id="s", runtime_session_id="session-abc",
    )


@pytest.mark.parametrize("case", _IDS)
def test_query_failure_line_redacts_credentials(case, caplog):
    from agents.chat_client import _stream_cloud_response
    with caplog.at_level(logging.INFO, logger="agentcore_client"):
        list(_stream_cloud_response({"response": _Body(_raised(_CASES[case][0]))}, _state()))
    lines = [r.getMessage() for r in caplog.records if "QUERY_FAILURE" in r.getMessage()]
    assert len(lines) == 1, lines
    _assert_redacted(lines[0], case)
    assert "exc_type=RuntimeError" in lines[0] and ":_fail_with" in lines[0]


# -- the digit-masked heads beside the fingerprint -------------------------------

@pytest.mark.parametrize("case", _IDS)
def test_chat_client_digit_head_redacts_credentials(case):
    from agents.chat_client import _redact_digits
    _assert_redacted(_redact_digits(_CASES[case][0]), case)


@pytest.mark.parametrize("case", _IDS)
def test_runtime_error_head_redacts_credentials(case):
    from agents.runtime_app import _redacted_error_head
    _assert_redacted(_redacted_error_head(_CASES[case][0], limit=200), case)


# -- order: redact the whole message, then mask digits, then cut ----------------

@pytest.mark.parametrize("message", [
    # The value straddles the 160-char cut and its pattern needs the end:
    # the userinfo mask needs the '@', the bearer pattern twelve characters.
    "a" * 130 + " postgresql://adt_app:zqxjvkwpmbzqxjvk@db.example.com/erp failed",
    "a" * 144 + " Bearer zqxjvkwpmbzqxjvkwp from api.example.com",
    "a" * 144 + " password=zqxjvkwpmbzqxjvk trailing text",
], ids=["url_userinfo", "bearer", "assignment"])
def test_a_secret_cut_by_the_head_limit_is_still_redacted(message):
    """Redacting after the cut would leave the value's first characters."""
    start = message.index("zqx")
    assert start < 160 < start + 16              # the value straddles the cut
    head = fp.masked_head(message)               # default limit: 160
    assert len(head) == 160
    assert "zqx" not in head, head


def test_digit_masking_does_not_defeat_a_pattern():
    """Masking first would turn the key into AKIAIOSFODNN#EXAMPLE, which no
    access-key pattern matches, so the rest of the key would be logged."""
    head = fp.masked_head("key AKIAIOSFODNN7EXAMPLE rejected")
    assert "IOSFODNN" not in head and "EXAMPLE" not in head, head
    assert head.startswith("key ") and head.endswith(" rejected"), head


def test_a_failing_redactor_logs_no_message_text(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("redactor down")
    monkeypatch.setattr(fp, "redact_sensitive", _boom)
    head = fp.masked_head("connect postgresql://u:dsn-pass-wxyz@db.example.com/erp")
    assert "dsn-pass-wxyz" not in head and "db.example.com" not in head, head
    assert head  # still a value, never an exception


def test_plain_text_is_unchanged_apart_from_digits():
    assert fp.masked_head("stream torn down at row 42 on host db.example.com") == (
        "stream torn down at row ## on host db.example.com")


# -- URLs inside a sentence -------------------------------------------------------

def test_a_query_secret_after_an_earlier_question_mark_is_redacted():
    """The URL masker parses one URL; handed the whole sentence it read the
    first '?' as the query and missed the real one."""
    head = fp.masked_head("is the API up? GET https://api.example.com/v1/spec?"
                          "access_token=short-tok-wxyz returned an error page", 300)
    assert "short-tok-wxyz" not in head, head
    assert head.startswith("is the API up? GET https://api.example.com/v#/spec?"), head
    assert head.endswith(" returned an error page"), head


def test_each_url_on_a_log_line_is_masked_through_its_last_at():
    """A log line masks from '://' through the last '@' before the next '://'
    on the line, so the prose between a URL and a later address is masked
    (a password can hold raw whitespace), while a second URL starts its own
    mask and keeps its host."""
    head = fp.masked_head("GET https://api.example.com failed; ask the owner at "
                          "ops@example.com then retry postgresql://u:pw-wxyz@db.example.com/erp",
                          300)
    assert "pw-wxyz" not in head, head
    assert head == "GET https://***@example.com then retry postgresql://***@db.example.com/erp"


# -- the redaction window ---------------------------------------------------------

def test_a_long_message_is_redacted_in_bounded_time():
    """One shared pattern is quadratic on long dotted runs (about four minutes
    for 256,000 characters), so only a window past the head is redacted."""
    import time
    message = "token=early-secret-wxyz " + "a." * 128_000
    started = time.perf_counter()
    head = fp.masked_head(message, 300)
    assert time.perf_counter() - started < 5.0
    assert "early-secret-wxyz" not in head and head.startswith("token="), head


def test_a_token_the_window_splits_is_dropped_not_half_redacted():
    """Redaction shortens the five assignments enough to pull a JWT that the
    window cut through into the head; a partial JWT matches no pattern."""
    jwt = "eyJ" + "h" * 140 + f".{_JWT_PAYLOAD}.{_JWT_SIGNATURE}"
    message = ("password=" + "v" * 100 + " ") * 5 + jwt + " tail"
    assert message.index(jwt) < 160 + 512 < message.index(jwt) + len(jwt)
    head = fp.masked_head(message)
    assert "vvvv" not in head and "eyJ" not in head, head
    assert head.startswith("password=[REDACTED_SECRET] "), head


def test_one_unbroken_token_longer_than_the_window_is_withheld():
    head = fp.masked_head("x" * 5_000)
    assert head == fp.LONG_TOKEN_WITHHELD


# -- prose that only names a credential -------------------------------------------

@pytest.mark.parametrize("text", PROSE_NEGATIVES)
def test_prose_naming_a_credential_is_logged_as_written(text):
    from agents.chat_client import _redact_digits
    from agents.runtime_app import _redacted_error_head
    assert fp.masked_head(text, 300) == text
    assert _redact_digits(text, limit=300) == text
    assert _redacted_error_head(text, limit=300) == text


# -- log lines mask a URL through its last '@' ------------------------------------

@pytest.mark.parametrize("text", URL_AT_OUTSIDE_AUTHORITY + (
    ANSWER_ONLY_FORMS["url_query_at"][0],))
def test_log_heads_mask_an_at_anywhere_in_a_url(text):
    """A log line cannot tell a raw '/', '?' or '#' in a password from an '@'
    in a path or query, so it masks from '://' through the last '@' of the
    URL. Answers keep these URLs (test_response_integrity_credential_forms)."""
    from agents.chat_client import _redact_digits
    from agents.runtime_app import _redacted_error_head
    first, last = text.split(" ", 1)[0], text.rsplit(" ", 1)[1]
    for head in (fp.masked_head(text, 300), _redact_digits(text, limit=300),
                 _redacted_error_head(text, limit=300)):
        assert "://***@" in head, head
        assert head.startswith(first + " ") and head.endswith(" " + last), head
        assert "example.com/users" not in head and "?owner=" not in head, head
