# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""URL userinfo runs through the LAST '@' of the authority, everywhere it is masked.

URL parsers (``urllib.parse.urlsplit`` included) end the userinfo at the last
'@' before the host, so a password with a raw '@' or ':' in it
(``postgresql://svc:alpha@bravo:charlie@db.example.com/erp``) is
``svc:alpha@bravo:charlie``. The display masker ``connection_security._mask_url``
ended it at the FIRST '@' and showed ``bravo:charlie``; so did the served
catalog entry, the onboarding agent's system lookup, the at-rest stripper and
the probe's Host header. An '@' in the path or query is still not userinfo.

``_mask_url`` serves display and audit masking, so it may mask a little more
than before, never less: every span the previous rule hid stays hidden.
Offline: no sockets, no AWS.
"""
from __future__ import annotations

import re

import pytest

from agents import connection_security as cs

_MULTI_AT = "postgresql://svc:alpha@bravo:charlie@db.example.com:5432/erp"
_SECRET_PARTS = ("alpha", "bravo", "charlie")

_MASKED = [
    (_MULTI_AT, "postgresql://***@db.example.com:5432/erp"),
    ("postgresql://svc:al%40pha%3Abravo@db.example.com/erp",
     "postgresql://***@db.example.com/erp"),
    ("postgresql://svc:pw@[::1]:5432/erp", "postgresql://***@[::1]:5432/erp"),
    ("mysql://app:my-pass@db.example.com:3306/a", "mysql://***@db.example.com:3306/a"),
    ("mongodb+srv://app:mongo@pass@cluster.example.com/b?retryWrites=true",
     "mongodb+srv://***@cluster.example.com/b?retryWrites=true"),
    ("redis://:redis-pass@cache.example.com:6379/0", "redis://***@cache.example.com:6379/0"),
    ("amqp://app:amqp:pass@mq.example.com/vh", "amqp://***@mq.example.com/vh"),
    ("https://app:web@pass@api.example.com/v1/spec", "https://***@api.example.com/v1/spec"),
    ("postgresql://svc:pw@db.example.com/erp?application_name=a@b",
     "postgresql://***@db.example.com/erp?application_name=a@b"),
    # Malformed, but the previous rule masked these, so they stay masked.
    ("postgresql://svc:pa ss@db.example.com/erp", "postgresql://***@db.example.com/erp"),
    ("postgresql://svc:pa?ss@db.example.com/erp", "postgresql://***@db.example.com/erp"),
    ("postgresql://svc:pa#ss@db.example.com/erp", "postgresql://***@db.example.com/erp"),
]


@pytest.mark.parametrize("raw, expected", _MASKED)
def test_mask_url_masks_through_the_last_at_of_the_authority(raw, expected):
    assert cs._mask_url(raw) == expected


_NOT_USERINFO = [
    "https://api.example.com/v1/users/@ops-team",
    "https://api.example.com/v1/users?email=ops@example.com",
    "https://api.example.com/v1/feed#@latest",
    "postgresql://[::1]:5432/erp",
    "https://[2001:db8::1]:8443/health",
    "postgresql://db.example.com/erp",
]


@pytest.mark.parametrize("raw", _NOT_USERINFO)
def test_an_at_outside_the_authority_is_not_userinfo(raw):
    assert cs._mask_url(raw) == raw


def _old_rule_hidden(raw: str) -> list[str]:
    return [m.group(0)[3:-1] for m in re.finditer(r"://[^@/]+@", raw)]


@pytest.mark.parametrize("raw", [raw for raw, _ in _MASKED] + _NOT_USERINFO + [
    "GET https://api.example.com failed; ask the owner at ops@example.com",
    "https://h.example.com?next=a@b then postgresql://u:p@db.example.com/x",
    "line one https://x.example.com\nline two user@host",
])
def test_nothing_the_previous_rule_hid_is_shown(raw):
    masked = cs._mask_url(raw)
    for span in _old_rule_hidden(raw):
        assert span not in masked, (span, masked)


def test_mask_connection_masks_a_multi_at_connection_string():
    out = cs.mask_connection({"connection_string": _MULTI_AT, "url": _MULTI_AT})
    for value in out.values():
        assert value == "postgresql://***@db.example.com:5432/erp"


def test_safe_detail_masks_a_multi_at_dsn():
    out = cs._safe_detail(f"could not connect to {_MULTI_AT}: timeout", {})
    assert not any(part in out for part in _SECRET_PARTS), out
    assert "@db.example.com:5432/erp: timeout" in out


def test_strip_url_credentials_drops_userinfo_through_the_last_at():
    assert cs._strip_url_credentials(
        "https://svc:alpha@bravo:charlie@api.example.com/x?page=2") == "https://api.example.com/x?page=2"
    assert cs._strip_url_credentials(_MULTI_AT) == "postgresql://db.example.com:5432/erp"
    assert cs._strip_url_credentials("https://api.example.com/v1/users?email=ops@example.com") == (
        "https://api.example.com/v1/users?email=ops%40example.com")


def test_served_catalog_entry_masks_a_multi_at_connection_string():
    from agents import onboard
    served = onboard._served_catalog_entry({
        "id": "erp-a", "connection_type": "PostgreSQL", "connection_string": _MULTI_AT,
        "connection_config": {"schema": "erp"}})
    assert served["connection_string"] == "postgresql://***@db.example.com:5432/erp"


def test_served_catalog_entry_keeps_the_previous_masking_of_a_raw_slash():
    """The served masker always hid through the first '@', even past a '/'."""
    from agents import onboard
    served = onboard._served_catalog_entry({
        "id": "erp-a", "connection_type": "PostgreSQL",
        "connection_string": "postgresql://svc:pa/ss@db.example.com/erp",
        "connection_config": {"schema": "erp"}})
    assert served["connection_string"] == "postgresql://***@db.example.com/erp"


def test_onboarding_system_lookup_masks_a_multi_at_connection_string(monkeypatch):
    from agents import onboard_agent as oa
    monkeypatch.setattr(oa.catalog, "lookup", lambda _sid: {
        "id": "erp-a", "connection_string": _MULTI_AT})
    out = oa.get_system_info("erp-a")
    assert out["connection_string"] == "postgresql://***@db.example.com:5432/erp"


def test_probe_host_header_drops_userinfo_through_the_last_at():
    from agents.connection_probes import _pin_host_to_ip
    pinned, host_header, sni = _pin_host_to_ip(
        "https://svc:alpha@bravo:charlie@api.example.com:8443/x", "192.0.2.10")
    assert host_header == "api.example.com:8443"
    assert sni == "api.example.com"
    assert pinned == "https://192.0.2.10:8443/x"


# -- a raw '/', '?' or '#' in a password ------------------------------------------
#
# These end the parsed authority early: urlsplit reads "svcUser:qrst" as a host
# and port, so the port is not a number. Registration now refuses them; one
# stored before that does not parse, so serving and logging mask its whole
# authority (and everything after it, since where the password ends is unknown).

_SECRET_BITS = ("svcUser", "qrst", "uvwx")
_RAW_DELIMITER_URLS = [
    "postgresql://svcUser:qrst/uvwx@db.example.com/erp",
    "https://svcUser:qrst?uvwx@host.example.com/x",
    "https://svcUser:qrst#uvwx@host.example.com/x",
]
# What the at-rest stripper kept for those before this change.
_LEGACY_STORED = [
    "postgresql://svcUser:qrst/uvwx@db.example.com/erp",
    "https://svcUser:qrst?uvwx%40host.example.com%2Fx=",
    "https://svcUser:qrst#uvwx@host.example.com/x",
]


def _no_secret_bits(text: str) -> None:
    for bit in _SECRET_BITS:
        assert bit not in text, (bit, text)


@pytest.mark.parametrize("raw", _RAW_DELIMITER_URLS)
def test_strip_url_credentials_masks_a_url_that_does_not_parse(raw):
    out = cs._strip_url_credentials(raw)
    _no_secret_bits(out)
    assert out == raw.split("://", 1)[0] + "://***@"


@pytest.mark.parametrize("stored", _LEGACY_STORED)
def test_a_stored_url_that_does_not_parse_is_masked_for_display_and_logs(stored):
    _no_secret_bits(cs._mask_url(stored))
    _no_secret_bits(cs.mask_stored_url(stored))
    masked = cs.mask_connection({"connection_string": stored, "url": stored,
                                 "base_url": stored})
    for value in masked.values():
        _no_secret_bits(value)
        assert "://***@" in value
    _no_secret_bits(cs._safe_detail(f"could not reach {stored}: refused", {}))


@pytest.mark.parametrize("stored", _LEGACY_STORED)
def test_served_catalog_entry_masks_a_stored_url_that_does_not_parse(stored):
    from agents import onboard
    served = onboard._served_catalog_entry({
        "id": "mcp-a", "connection_type": "MCP Server", "connection_string": stored,
        "connection_config": {"url": stored, "base_url": stored}})
    _no_secret_bits(served["connection_string"])
    assert "://***@" in served["connection_string"]   # the wizard's masked marker
    for key in ("url", "base_url"):
        _no_secret_bits(str(served.get("connection_config", {}).get(key, "")))


@pytest.mark.parametrize("stored", _LEGACY_STORED)
def test_onboarding_system_lookup_masks_a_stored_url_that_does_not_parse(stored, monkeypatch):
    from agents import onboard_agent as oa
    monkeypatch.setattr(oa.catalog, "lookup", lambda _sid: {
        "id": "mcp-a", "connection_string": stored,
        "connection_config": {"url": cs._strip_url_credentials(stored)}})
    out = oa.get_system_info("mcp-a")
    _no_secret_bits(out["connection_string"])
    _no_secret_bits(str(out.get("connection_config")))


@pytest.mark.parametrize("raw", _RAW_DELIMITER_URLS + _LEGACY_STORED)
def test_the_node_write_failure_log_masks_a_url_that_does_not_parse(raw, monkeypatch, caplog):
    import logging

    from agents import connection_resolver as cr

    def _fail(_cypher, _params=None):
        raise RuntimeError(f"graph write failed while saving {raw}")
    monkeypatch.setattr(cr, "_neptune_write", _fail)
    with caplog.at_level(logging.WARNING, logger=cr.logger.name):
        cr._persist_node_connection_config(
            "mcp-a", {"url": raw, "connection_string": raw}, "MCP Server", "")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "node write failed" in text
    _no_secret_bits(text)


# -- registration refuses a credential URL that needs percent-encoding ------------

_REFUSED = _RAW_DELIMITER_URLS + [
    "postgresql://svcUser:qrst@uvwx@db.example.com/erp",     # raw '@'
    "postgresql://svcUser:qrst uvwx@db.example.com/erp",     # raw space
    "postgresql://svcUser:qrst%zzuvwx@db.example.com/erp",   # broken escape
    "https://mcp.example.com:notaport/mcp",                  # invalid port
    "https://mcp.example.com:70000/mcp",                     # port out of range
]


@pytest.mark.parametrize("raw", _REFUSED)
def test_credential_url_error_explains_percent_encoding(raw):
    message = cs.credential_url_error(raw)
    assert "percent-encode" in message
    _no_secret_bits(message)


def _userinfo_url(scheme: str, userinfo: str, rest: str) -> str:
    """``scheme://userinfo@rest``, assembled at run time."""
    return f"{scheme}://{userinfo}@{rest}"


@pytest.mark.parametrize("ok", [
    _userinfo_url("postgresql", "svcUser:qrst%2Fuvwx%40%3F%23",
                  "db.example.com/erp?options=-csearch_path%3Derp"),
    "postgresql://svc:p!$&'()*+,;=:x@db.example.com/erp",
    "https://h.example.com?owner=a@b",
    "https://api.example.com/v1/users/@ops-team",
    "https://[2001:db8::1]:8443/health",
    "postgresql://db.example.com/erp",
    "not a url at all",
])
def test_credential_url_error_accepts_a_well_formed_url(ok):
    assert cs.credential_url_error(ok) == ""


@pytest.mark.parametrize("raw", _REFUSED)
def test_a_user_registration_is_refused_with_the_plain_message(raw):
    from agents import onboard_agent as oa
    pg = raw.startswith("postgresql://")
    if pg:
        raw += "?options=-csearch_path%3Derp"
    out = oa._prepare_user_registration(
        "PostgreSQL" if pg else "MCP Server", raw, "", None)
    assert "percent-encode" in out.get("error", ""), out
    _no_secret_bits(out["error"])


@pytest.mark.parametrize("field", ["url", "base_url", "connection_string"])
def test_a_connection_edit_is_refused_with_the_plain_message(field):
    from agents import admin_routes
    raw = "https://svcUser:qrst?uvwx@mcp.example.com/mcp"
    _ns, _secret, err = admin_routes._parse_connection_body({field: raw}, "MCP Server")
    assert err and "percent-encode" in err, err
    _no_secret_bits(err)


def test_the_target_guard_refuses_an_invalid_port_before_any_lookup(monkeypatch):
    import socket

    def _no_lookup(*_a, **_k):
        raise AssertionError("resolved a target with an invalid port")
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    for raw in ("https://svcUser:qrst?uvwx@host.example.com/x",
                "https://mcp.example.com:notaport/mcp"):
        with pytest.raises(cs.ConnectionTargetError, match="percent-encode") as info:
            cs.validate_connection_target(raw, connection_type="MCP Server")
        _no_secret_bits(str(info.value))
