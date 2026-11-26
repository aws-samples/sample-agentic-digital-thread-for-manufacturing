# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Register a system whose credentials come from a NAMED Secrets Manager secret
(the second credential source, beside a pasted key). Offline — no AWS, no
Neptune, no network.

The rules these pin, for an OPTED-IN connection type only (Tulip today,
``connection_types.SECRET_ARN_CREDENTIAL_TYPES``):

* a request may name a ``secret_arn`` in ``connection_config`` ONLY when the
  type opted in AND the ARN clears ``connection_pinning.check_arn_allowed``
  (allowlisted prefix, this Region, not an ADT-owned secret). The existing
  client-supplied-ARN refusal stays for every other type;
* an ARN-sourced registration stores the NAMED ARN as the entry's pointer and
  marks the node ``credential_source="secret_arn"`` — it never mints an
  ``adt/conn/<id>`` secret, and the key/secret from the external secret never
  reach the catalog entry;
* URL and workspace fields are optional for an ARN system; the pin rule
  (``test_connection_secret_pinning.py``) refuses a filled value that differs
  at use time, because the secret's url/workspace win there.

These tests FAIL on 9219dda8 (registration refuses or ignores the ARN) and pass
after item 1 is built.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.catalog as catalog_mod  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402
from agents.models import ConnectRequest  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    cat, no_dns, secrets)

ALLOWLIST = "customer/tulip/"
SEAT_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf"
TULIP_URL = "https://acme.tulip.co"


@pytest.fixture
def arn_enabled(monkeypatch):
    """The deploy opts the ARN source in by setting the allowlist prefix."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", ALLOWLIST)


def _register(body: dict) -> dict:
    return onboard.onboard_connect(ConnectRequest(**body))


def _register_tulip_arn(secret_arn=SEAT_ARN, **cfg_extra):
    cfg = {"secret_arn": secret_arn, **cfg_extra}
    return _register({"name": "Tulip MES", "system_type": "MES", "plant": "Plant 1",
                      "connection_type": TULIP, "connection_config": cfg})


SID = "tulip-mes"  # sanitized from "Tulip MES"


# ── the ARN is accepted for an opted-in type and stored as the pointer ──

def test_tulip_registration_with_an_allowlisted_arn_is_accepted(cat, secrets, arn_enabled):
    res = _register_tulip_arn()
    assert res.get("status") == "created", res
    entry = cat.get(SID)
    assert entry is not None, res
    # The NAMED ARN is the entry's pointer — no adt/conn/<id> secret was minted.
    assert entry["secret_arn"] == SEAT_ARN, entry
    assert secrets == {}, "an ARN-sourced registration mints no secret"
    # The node is marked ARN-sourced so the resolver pins url/workspace later.
    assert entry.get("connection_config", {}).get("credential_source") == "secret_arn", entry


def test_an_arn_sourced_registration_never_stores_the_secret_contents(cat, secrets, arn_enabled):
    _register_tulip_arn()
    blob = json.dumps(cat.get(SID))
    for leak in ("api_key", "api_secret", "KEY", "SECRET"):
        assert leak not in blob or leak == "api_key", blob  # no credential material
    assert "api_key" not in blob and "api_secret" not in blob, blob


def test_an_optional_url_is_kept_on_the_node_for_the_pin_check(cat, secrets, arn_enabled):
    """A filled base_url is allowed through registration (the use-time pin rule
    compares it to the secret's value); it rides on the node config."""
    res = _register_tulip_arn(base_url=TULIP_URL)
    assert res.get("status") == "created", res
    assert cat.get(SID)["connection_config"].get("base_url") == TULIP_URL


def test_registration_refuses_a_filled_url_that_conflicts_with_a_readable_secret(
        cat, secrets, arn_enabled):
    """When the named secret is readable at registration and carries a different
    base_url, a filled conflicting url is refused (the secret wins)."""
    secrets[SEAT_ARN] = {"api_key": "K", "api_secret": "S", "base_url": TULIP_URL,
                         "workspace_id": "ws-7"}
    res = _register_tulip_arn(base_url="https://evil.example.com")
    assert "error" in res and "base_url" in res["error"], res
    assert cat.get(SID) is None


def test_registration_allows_a_filled_url_that_matches_a_readable_secret(
        cat, secrets, arn_enabled):
    secrets[SEAT_ARN] = {"api_key": "K", "api_secret": "S", "base_url": TULIP_URL}
    res = _register_tulip_arn(base_url=TULIP_URL)
    assert res.get("status") == "created", res


# ── the ARN source is gated: refused when off, non-allowlisted, or non-opted-in ──

def test_the_arn_option_is_refused_when_the_allowlist_is_empty(cat, secrets, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("ADT_SECRET_ARN_ALLOWLIST", raising=False)
    res = _register_tulip_arn()
    assert "error" in res, res
    assert cat.get(SID) is None and secrets == {}


def test_a_non_allowlisted_arn_is_refused(cat, secrets, arn_enabled):
    other = "arn:aws:secretsmanager:us-east-1:111122223333:secret:team/other/thing-AbCdEf"
    res = _register_tulip_arn(secret_arn=other)
    assert "error" in res, res
    assert cat.get(SID) is None and secrets == {}


def test_an_adt_owned_secret_is_refused_even_inside_the_allowlist(cat, secrets, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", "adt/")  # would otherwise cover adt/conn/
    owned = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/evil-AbCdEf"
    res = _register_tulip_arn(secret_arn=owned)
    assert "error" in res, res
    assert cat.get(SID) is None and secrets == {}


def test_a_client_supplied_arn_is_refused_for_a_non_opted_in_type(cat, secrets, arn_enabled):
    """MCP has not opted in: naming a secret ARN stays refused even with the
    allowlist set (the historical client-supplied-ARN refusal)."""
    res = _register({"name": "Quality Lab", "connection_type": "MCP Server",
                     "connection_string": "https://mcp.example.com/mcp",
                     "connection_config": {"secret_arn": SEAT_ARN}})
    assert "error" in res, res
    assert cat.get("quality-lab") is None


# ── the chat tool shares the same registration logic ────────────────────

def test_the_chat_tool_registers_an_arn_sourced_tulip(cat, secrets, arn_enabled):
    res = onboard_agent.connect_new_system(
        name="Tulip MES", system_type="MES", plant="Plant 1",
        connection_type=TULIP, connection_config={"secret_arn": SEAT_ARN})
    assert res.get("status") == "created", res
    assert cat.get(SID)["secret_arn"] == SEAT_ARN
    assert secrets == {}


# ── use-time resolution: the secret's url/workspace/key WIN ──────────────

SECRET = {"schema": 1, "mode": "live", "api_key": "KEY", "api_secret": "SECRET",
          "base_url": TULIP_URL, "workspace_id": "ws-7", "key_scope": "workspace",
          "token": "YmFzaWM=", "auth_scheme": "basic"}  # nosec B106


def _arn_entry():
    return {"id": SID, "name": "Tulip MES", "connection_type": TULIP,
            "schema": "", "status": "registered", "connection_origin": "user",
            "secret_arn": SEAT_ARN,
            "connection_config": {"credential_source": "secret_arn"}}


def test_registration_connection_resolves_url_and_workspace_from_the_named_secret(
        cat, secrets, arn_enabled, monkeypatch):
    """Before the first provision (no node yet) an ARN-sourced system resolves
    its url/workspace/key from the NAMED secret, not an adt/conn/<id> secret."""
    secrets[SEAT_ARN] = dict(SECRET)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)  # unprovisioned
    resolved = cr.registration_connection(SID, _arn_entry())
    assert resolved.get("base_url") == TULIP_URL, resolved
    assert resolved.get("workspace_id") == "ws-7", resolved
    assert resolved.get("key_scope") == "workspace", resolved
    assert resolved.get("token") == "YmFzaWM=", resolved
    assert resolved.get("_secret_unresolved") is False, resolved


def test_a_filled_url_that_matches_the_secret_is_fine_but_a_conflict_is_refused(
        cat, secrets, arn_enabled, monkeypatch):
    secrets[SEAT_ARN] = dict(SECRET)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    entry = _arn_entry()
    entry["connection_config"]["base_url"] = "https://evil.example.com"  # conflicts
    resolved = cr.registration_connection(SID, entry)
    # The secret's url wins; the conflicting node url never survives.
    assert resolved.get("base_url") == TULIP_URL, resolved


def test_an_unreadable_named_secret_is_a_retryable_miss(cat, secrets, arn_enabled, monkeypatch):
    # SEAT_ARN not in the store ⇒ _fetch raises ResourceNotFound.
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    resolved = cr.registration_connection(SID, _arn_entry())
    assert resolved.get("_secret_unresolved") is True, resolved
