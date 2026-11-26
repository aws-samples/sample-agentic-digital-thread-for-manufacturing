# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""i3X registered-secret credential fallback (UI-parity fix).

A UI-registered SiteWise (i3X) system stores its bearer token in Secrets
Manager (``adt/conn/<id>``) at registration — the catalog entry is
allowlist-sanitized and NEVER carries the token, and the graph node does not
exist until provision. These tests pin the fix that lets the discover and
provision paths (and the hierarchy walker) authenticate anyway:

* ``connection_resolver.resolve_registered_token`` — precedence: inline token,
  then the node-based ``resolve_connection``, then the REGISTRATION-time
  ``adt/conn/<id>`` secret reached ONLY through the catalog entry's own pointer
  with a payload bound to the system (never by NAME — a reused id must not
  inherit a removed system's token); fail-soft ``("", "bearer")``.
* ``onboard._provision_i3x_post_pass`` feeds the resolved token to
  ``provision_i3x_hierarchy`` (previously: the always-empty catalog token —
  the walker 401'd and wrote ZERO assets for UI-registered systems).
* ``onboard._dp_agent_i3x`` persists the node pointing at the EXISTING
  registered secret (``secret_arn`` pointer) instead of dropping credentials.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agents import connection_resolver as cr


@pytest.fixture(autouse=True)
def _clear_secret_cache(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")  # the Region the own-secret ARN names
    cr._secret_cache.clear()
    yield
    cr._secret_cache.clear()


_OWN_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/sys-a-AbCdEf"
_I3X_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/sys-i3x-AbCdEf"


def _bound(system_id, **payload):
    """A registration payload as connect_new_system writes it (bound)."""
    return {**payload, "connection_origin": "user", "system_id": system_id}


def _entry(system_id, arn):
    return {"id": system_id, "secret_arn": arn, "connection_origin": "user"}


# ── resolve_registered_token ────────────────────────────────────────────────

def test_inline_token_wins():
    with patch.object(cr, "resolve_connection", side_effect=AssertionError("must not resolve")), \
         patch.object(cr, "_fetch_secret", side_effect=AssertionError("must not fetch")):
        assert cr.resolve_registered_token("sys-a", "tok-inline", "bearer_literal") == (
            "tok-inline", "bearer_literal")


def test_node_resolved_token_second():
    with patch.object(cr, "resolve_connection",
                      return_value={"token": "tok-node", "auth_scheme": "bearer"}), \
         patch.object(cr, "_fetch_secret", side_effect=AssertionError("must not fetch")):
        assert cr.resolve_registered_token("sys-a") == ("tok-node", "bearer")


def test_registered_secret_through_the_entry_pointer():
    """Pre-node (freshly registered): the registration's own secret, reached
    through the catalog entry's pointer, with a bound payload."""
    seen = {}

    def fake_fetch(secret_id):
        seen["id"] = secret_id
        return _bound("sys-a", token="tok-registered")  # nosec B106 - a fake test token

    with patch.object(cr, "resolve_connection", return_value={"_unconfigured": True}), \
         patch.object(cr, "_fetch_secret", side_effect=fake_fetch):
        token, scheme = cr.resolve_registered_token("sys-a", entry=_entry("sys-a", _OWN_ARN))
    assert (token, scheme) == ("tok-registered", "bearer")
    assert seen["id"] == _OWN_ARN


def test_registered_secret_is_never_read_by_name():
    """No entry pointer ⇒ no token, and adt/conn/<id> is never fetched by NAME."""
    with patch.object(cr, "resolve_connection", return_value={"_unconfigured": True}), \
         patch.object(cr, "_fetch_secret", side_effect=AssertionError("must not fetch")):
        assert cr.resolve_registered_token("sys-a") == ("", "bearer")
        assert cr.resolve_registered_token("sys-a", entry={"id": "sys-a"}) == ("", "bearer")


def test_failsoft_empty_when_nothing_configured():
    with patch.object(cr, "resolve_connection", side_effect=RuntimeError("no graph")), \
         patch.object(cr, "_fetch_secret", side_effect=RuntimeError("no secret")):
        assert cr.resolve_registered_token("sys-a") == ("", "bearer")


def test_node_resolve_error_still_reaches_secret_fallback():
    with patch.object(cr, "resolve_connection", side_effect=RuntimeError("boom")), \
         patch.object(cr, "_fetch_secret",
                      return_value=_bound("sys-a", token="tok-r", auth_scheme="bearer_literal")):  # nosec B106 - a fake test token
        assert cr.resolve_registered_token(
            "sys-a", entry=_entry("sys-a", _OWN_ARN)) == ("tok-r", "bearer_literal")


# ── provision post-pass wires the resolved token to the walker ──────────────

def test_provision_post_pass_feeds_registered_token_to_walker():
    from agents import onboard
    from agents.models import ProvisionRequest

    calls = {}

    def fake_walker(system_id, system_name, base_url, token="", auth_scheme="bearer"):  # nosec B107 - empty default sentinel; real value supplied by caller
        calls.update(system_id=system_id, base_url=base_url,
                     token=token, auth_scheme=auth_scheme)
        return {"roots": 1, "assets": 5, "properties": 9, "warnings": []}

    entry = {
        "id": "sys-i3x", "connection_string": "https://i3x.example.com",
        # Sanitized catalog config — deliberately NO token (the real shape); the
        # registration's pointer to its own bound secret is the token source.
        "connection_config": {"base_url": "https://i3x.example.com",
                              "auth_scheme": "bearer"},
        "secret_arn": _I3X_ARN, "connection_origin": "user",
    }
    body = ProvisionRequest(system_id="sys-i3x", system_name="I3X")

    with (patch.object(onboard.catalog, "get", return_value=entry),
          patch.object(cr, "resolve_connection", return_value={"_unconfigured": True}),
          patch.object(cr, "_fetch_secret", return_value=_bound("sys-i3x", token="tok-registered")),  # nosec B106 - a fake test token
          patch("agents.onboard_i3x.provision_i3x_hierarchy", side_effect=fake_walker),
          patch("agents.onboard_i3x.bridge_i3x_schema", return_value=None)):
        onboard._provision_i3x_post_pass(body)

    assert calls["token"] == "tok-registered"
    assert calls["auth_scheme"] == "bearer"
    assert calls["base_url"] == "https://i3x.example.com"
    # The walk counters surface on the entry — unchanged behavior.
    assert entry["i3x_walk"]["assets"] == 5


# ── provision agent branch persists the secret POINTER on the node ──────────

def test_dp_agent_i3x_persists_registered_secret_pointer():
    from agents import onboard

    entry = {
        "id": "sys-i3x", "connection_string": "https://i3x.example.com",
        "secret_arn": _I3X_ARN, "connection_origin": "user",
        "connection_config": {"base_url": "https://i3x.example.com",
                              "auth_scheme": "bearer"},
    }
    persisted = {}

    class _Ctx:
        system_id, system_name = "sys-i3x", "I3X"
        connection_type, schema = "SiteWise (i3X)", ""
        cm_items = []
        field_descriptions = []

        class body:
            connection_string = ""

    def fake_persist(system_id, *, non_secret, secret=None, secret_arn=None):
        persisted.update(system_id=system_id, non_secret=non_secret,
                         secret=secret, secret_arn=secret_arn)

    with (patch.object(onboard.catalog, "get", return_value=entry),
          patch.object(cr, "resolve_connection", return_value={"_unconfigured": True}),
          patch.object(cr, "_fetch_secret", return_value=_bound("sys-i3x", token="tok-registered")),  # nosec B106 - a fake test token
          patch.object(onboard, "_persist_node_connection", side_effect=fake_persist),
          patch.object(onboard, "_agent_manager") as mgr,
          patch.object(onboard, "add_system_agent", return_value=None)):
        mgr.start_agent.return_value = "mcp://local"
        assert onboard._dp_agent_rest_family(_Ctx()) is None

    # Points the node at the EXISTING registered secret — no secret rewrite
    # (which could drop sibling keys), no dropped credentials.
    assert persisted["secret_arn"] == entry["secret_arn"]
    assert persisted["secret"] is None
    assert persisted["non_secret"]["base_url"] == "https://i3x.example.com"
    # The agent config got the resolved token (start_agent call).
    cfg = mgr.start_agent.call_args.kwargs["connection_config"]
    assert cfg["token"] == "tok-registered"


# ── /v1 base-URL normalization — both transports, one rule ──────────────────

def test_i3x_api_base_appends_v1_once():
    from agents.i3x_envelope import i3x_api_base
    assert i3x_api_base("https://x.example.com") == "https://x.example.com/v1"
    assert i3x_api_base("https://x.example.com/") == "https://x.example.com/v1"
    assert i3x_api_base("https://x.example.com/v1") == "https://x.example.com/v1"
    assert i3x_api_base("https://x.example.com/v1/") == "https://x.example.com/v1"


def test_i3x_client_normalizes_like_walker_transport():
    """The live regression: _I3xClient used base_url VERBATIM while the walker
    appended /v1 — every runtime tool call 404'd against a record that the
    walker succeeded with. Pin that both clients resolve the same API base."""
    from agents.i3x_envelope import i3x_api_base
    from agents.system_agent import _I3xClient
    for stored in ("https://i3x.example.com", "https://i3x.example.com/v1"):
        client = _I3xClient(stored, "", "tok", "bearer")
        assert client.base_url == i3x_api_base(stored) == "https://i3x.example.com/v1"
