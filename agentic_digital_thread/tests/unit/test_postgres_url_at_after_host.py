# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A PostgreSQL URL with a raw '@' after its host is refused, and masked in answers.

A password whose part before a raw '/', '?' or '#' is all digits reads, to a URL
parser, as a host and a numeric port: ``postgresql://svcUser:4821/7093@db.example.com/erp``
is host ``svcUser``, port 4821 and a path. Registration accepted it and the answer
masker kept it whole, because the same shape is a valid HTTPS base URL with an '@'
in its path (``https://svc.example.com:8443/users/@me``). For the postgres and
postgresql schemes an '@' after the host is never a database name in practice, and
libpq decodes a percent-encoded one, so:
- registration refuses any raw '@' after a postgres host with the percent-encode
  message (a fix the user can apply);
- the answer masker hides a postgres authority cut by a raw '/', '?' or '#' through
  the '@' later in the same token, even when it looks like ``host:port``;
- the log and stored-URL maskers already hid it, and still do.
Other schemes keep the parser's reading. Offline: no sockets, no AWS.
"""
from __future__ import annotations

import logging

import pytest

from agents import connection_security as cs
from agents import response_integrity_security as ris
from tests.unit.test_credential_compositions import _LOG_PATH_NAMES, _log_paths, _unescaped

_DIGIT_PASSWORDS = [
    ("postgresql://svcUser:4821/7093@db.example.com/erp", "postgresql://***@db.example.com/erp"),
    ("postgresql://svcUser:4821?7093@localhost/erp", "postgresql://***@localhost/erp"),
    ("postgres://svcUser:4821#7093@localhost/erp", "postgres://***@localhost/erp"),
    ("POSTGRESQL://svcUser:4821/7093@localhost/erp", "POSTGRESQL://***@localhost/erp"),
    ("postgresql://svcUser:4821/7093@10.0.0.12:5432/erp", "postgresql://***@10.0.0.12:5432/erp"),
]
_URLS = [url for url, _masked in _DIGIT_PASSWORDS]
_PIECES = ("svcUser", "4821", "7093")


def _pieces_in(text: str) -> list[str]:
    return [piece for piece in _PIECES if piece in text]


# -- registration -----------------------------------------------------------------

@pytest.mark.parametrize("url", _URLS)
def test_registration_refuses_a_digit_password_that_reads_as_a_port(url):
    assert cs.credential_url_error(url) == cs.MALFORMED_URL_MESSAGE


@pytest.mark.parametrize("url", [
    "postgresql://svc:pw@db.example.com/erp?application_name=ops@plant",
    "postgresql://db.example.com/erp@archive",
    "postgres://db.example.com:5432/erp#ops@plant",
])
def test_registration_refuses_any_raw_at_after_a_postgres_host(url):
    assert cs.credential_url_error(url) == cs.MALFORMED_URL_MESSAGE


@pytest.mark.parametrize("url", [
    "postgresql://svc:pw@db.example.com/erp?application_name=ops%40plant",
    "postgresql://svcUser:4821%2F7093@db.example.com/erp",
    "postgresql://svc:p%40ss@db.example.com:5432/erp",
    "https://svc.example.com:8443/users/@me",
    "https://api.example.com/v1/users/@ops-team",
])
def test_registration_still_accepts_an_encoded_at_and_other_schemes(url):
    assert cs.credential_url_error(url) == ""


# -- answers ----------------------------------------------------------------------

@pytest.mark.parametrize("url,masked", _DIGIT_PASSWORDS)
def test_answers_hide_a_digit_password_cut_by_a_raw_delimiter(url, masked):
    text = f"Connect with {url} and retry.\nThe next line stays."
    assert ris.mask_url_userinfo(text) == (
        f"Connect with {masked} and retry.\nThe next line stays.")
    assert ris.url_has_password(text)
    assert ris.sensitive_text(text)
    for out in (ris.redact_sensitive(text), _unescaped(ris.markdown_literal(text))):
        assert not _pieces_in(out), out
        assert "and retry." in out and "The next line stays." in out


@pytest.mark.parametrize("text,expected", [
    # Another scheme keeps the parser's reading: a host, a port and a path.
    ("See https://svc.example.com:8443/users/@me for the owner.",
     "See https://svc.example.com:8443/users/@me for the owner."),
    # A postgres URL whose authority holds the '@' still ends its userinfo there.
    ("Use postgresql://svc:pw@db.example.com/erp?application_name=ops@plant today.",
     "Use postgresql://***@db.example.com/erp?application_name=ops@plant today."),
    # No '@' later in the token: nothing to hide.
    ("Open postgresql://db.example.com:5432/erp then ops@plant.example.com.",
     "Open postgresql://db.example.com:5432/erp then ops@plant.example.com."),
])
def test_answers_keep_the_parser_reading_everywhere_else(text, expected):
    assert ris.mask_url_userinfo(text) == expected


def test_a_longer_scheme_ending_in_postgres_is_not_postgres():
    text = "See notpostgres://svc.example.com:8443/users/@me now."
    assert ris.mask_url_userinfo(text) == text


# -- logs and the stored masker (already hid it; pinned) ---------------------------

@pytest.mark.parametrize("path", _LOG_PATH_NAMES)
@pytest.mark.parametrize("url", _URLS)
def test_logs_hide_it(path, url, caplog):
    caplog.set_level(logging.INFO, logger="agents.public_errors")
    caplog.set_level(logging.INFO, logger="agentcore_client")
    out = _log_paths(caplog)[path](f"connect failed for {url}: timeout")
    assert not _pieces_in(out), out


@pytest.mark.parametrize("url", _URLS)
def test_the_stored_url_masker_hides_it(url):
    assert not _pieces_in(cs.mask_stored_url(url))
