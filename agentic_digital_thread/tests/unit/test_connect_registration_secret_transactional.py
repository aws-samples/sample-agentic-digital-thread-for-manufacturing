# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A user registration whose credentials cannot be stored is not registered.

A registration that carries credentials (an MCP token, an i3X token, Tulip
keys, OAuth credentials, an Athena external id) wrote them to its own
``adt/conn/<id>`` secret AFTER claiming the id, and a failed write only left
a warning ``note``: the catalog kept the claim with no credentials, the form
(which reads only ``error``) closed and started onboarding, the token was
gone, and the claimed id blocked a clean retry. Now a user registration is
transactional, like PostgreSQL's: on a failed write exactly the entry just
claimed is removed and the registration answers an ``error``, so the form
stays open with what was typed. Demo registrations keep their fail-soft
note. Offline.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    SID, _register, cat, no_dns, secrets)

TOKEN = "form-token-SECRET-77"  # nosec B105 - fixture
URL = "https://mcp.example.com/mcp"


@pytest.fixture
def write_fails(monkeypatch):
    def _boom(system_id, secret):
        raise RuntimeError(f"ThrottlingException {secret}")   # would leak if echoed
    monkeypatch.setattr(cr, "_store_connection_secret", _boom)


@pytest.mark.parametrize("body", [
    {"connection_type": "MCP Server", "connection_string": URL,
     "connection_config": {"token": TOKEN}},
    {"connection_type": "SiteWise (i3X)", "connection_string": "https://i3x.example.com",
     "connection_config": {"base_url": "https://i3x.example.com", "token": TOKEN}},
], ids=["mcp", "i3x"])
def test_a_failed_credential_write_registers_nothing(cat, secrets, write_fails, body):
    res = _register({"name": "Quality Lab", **body})
    assert "error" in res and "status" not in res, res
    assert "Secrets Manager" in res["error"] and "Try again" in res["error"], res
    assert TOKEN not in json.dumps(res)
    assert cat.get(SID) is None, "the claimed entry was kept"
    # A retry with the same id is not blocked by a half-made registration.
    cr_store = {}

    def _ok(system_id, secret):
        cr_store[system_id] = dict(secret)
        return f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{system_id}-AbCdEf"
    import agents.connection_resolver as _cr
    _cr._store_connection_secret = _ok          # restored by the fixture's monkeypatch
    again = _register({"name": "Quality Lab", **body})
    assert again.get("status") == "created", again
    assert cr_store[SID]["token"] == TOKEN


def test_only_the_entry_just_claimed_is_removed(cat, secrets, write_fails):
    cat.add({"id": "other", "name": "Other", "connection_type": "MCP Server",
             "status": "registered", "connection_origin": "user"})
    _register({"name": "Quality Lab", "connection_type": "MCP Server",
               "connection_string": URL, "connection_config": {"token": TOKEN}})
    assert cat.get("other") is not None


def test_guard_a_registration_without_credentials_writes_nothing_and_registers(
        cat, secrets, write_fails):
    res = _register({"name": "Quality Lab", "connection_type": "MCP Server",
                     "connection_string": URL})
    assert res.get("status") == "created", res


def test_guard_a_demo_registration_keeps_its_fail_soft_note(cat, write_fails):
    import agents.onboard_agent as oa
    res = oa.register_demo_system(name="HighByte", system_type="UNS", plant="",
                                  connection_type="MCP Server",
                                  connection_string=URL, schema="", system_id="highbyte",
                                  connection_config={"token": TOKEN})
    assert res.get("status") == "created", res
    assert "could NOT be stored" in res.get("note", ""), res
