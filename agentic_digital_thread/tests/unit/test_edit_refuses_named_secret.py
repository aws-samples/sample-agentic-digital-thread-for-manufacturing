# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An admin Edit cannot switch a system's credential source.

A system takes its credentials either inline or from a Secrets Manager secret
it NAMED when it was connected (``secret_arn`` in the registration). An Edit
changes a system's settings or rotates its inline credentials; it never
switches the source. An Edit body that carries ``secret_arn`` is refused with
a 400 for every connection type, before anything is read, probed or written,
instead of being dropped while the rest of the Edit goes through. An Edit
without it behaves as before. All IO is mocked.
"""
import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
from agents.admin_routes import update_connection as update_connection_route  # noqa: E402
from agents.connection_types import (ATHENA, MCP_SERVER, POSTGRESQL, REST_API,  # noqa: E402
                                     SITEWISE_DIRECT, TULIP)

SID = "sys-x"
OWN_ARN = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{SID}-AbCdEf"
NAMED_ARN = ("arn:aws:secretsmanager:us-east-1:111122223333:secret:"
             "customer/tulip/other-connection-AbCdEf")

TULIP_STORED = {"system_id": SID, "connection_type": TULIP,
                "base_url": "https://acme.tulip.co", "workspace_id": "abc123",
                "secret_arn": OWN_ARN, "_secret_unresolved": False,
                "api_key": "KEY-OLD", "api_secret": "SECRET-OLD"}
TULIP_CATALOG = {"name": "Tulip", "connection_type": TULIP, "secret_arn": OWN_ARN,
                 "connection_config": {"base_url": "https://acme.tulip.co",
                                       "workspace_id": "abc123"}}
INLINE = {TULIP: (TULIP_STORED, TULIP_CATALOG, {"api_key": "KEY-NEW"})}


def _request():
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        is_admin=True, user=types.SimpleNamespace(email="admin@example.com"))
    return req


class _Catalog:
    def __init__(self, entry):
        self.entry = entry
        self.synced = []

    def get(self, system_id):
        return self.entry if system_id == SID else None

    def set_connection_config(self, system_id, cfg, secret_arn=None):
        self.synced.append(dict(cfg))


def _run(body, *, stored, catalog_entry):
    """Run one admin Edit; return (response, calls) where ``calls`` records
    every resolve, secret read, probe and write the Edit made."""
    calls = {"resolve": [], "read": [], "probe": [], "write": []}
    catalog = _Catalog(dict(catalog_entry))

    def _resolve(system_id):
        calls["resolve"].append(system_id)
        return dict(stored)

    def _read(arn):
        calls["read"].append(arn)
        return {k: v for k, v in stored.items()
                if k in ("client_id", "client_secret", "api_key", "api_secret")}

    def _probe(system_id, resolved=None):
        calls["probe"].append(dict(resolved or {}))
        return {"status": "pass", "latency_ms": 2, "reason": "ok", "detail": "ok",
                "connection_type": stored["connection_type"]}

    def _write(system_id, *, non_secret, secret=None, secret_arn=None, entry=None, **_kw):
        calls["write"].append({"non_secret": dict(non_secret), "secret": dict(secret or {})})
        return OWN_ARN

    with patch("agents.catalog.catalog", catalog), \
            patch.object(cr, "resolve_connection", _resolve), \
            patch.object(cr, "read_secret_uncached", _read), \
            patch.object(cr, "probe_connection", _probe), \
            patch.object(cr, "write_connection", _write), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
            patch.object(admin_routes, "_reconnect_system", lambda *a, **k: False):
        res = asyncio.run(update_connection_route(SID, _request(), body))
    calls["synced"] = catalog.synced
    return res, calls


def _refused(res) -> dict:
    assert getattr(res, "status_code", 200) == 400, res
    return json.loads(res.body.decode())


def _nothing_done(calls) -> bool:
    return all(calls[k] == [] for k in ("resolve", "read", "probe", "write", "synced"))


@pytest.mark.parametrize("ctype", [TULIP])
@pytest.mark.parametrize("key", ["secret_arn", "Secret_Arn"])
def test_an_inline_edit_naming_a_secret_is_refused_and_nothing_is_written(ctype, key):
    stored, catalog_entry, change = INLINE[ctype]
    res, calls = _run({"connection_type": ctype, **change, key: NAMED_ARN},
                      stored=stored, catalog_entry=catalog_entry)
    body = _refused(res)
    assert body["reason"] == "credential_source_not_editable", body
    assert "secret" in body["detail"].lower() and NAMED_ARN not in body["detail"], body
    assert _nothing_done(calls), calls


@pytest.mark.parametrize("ctype", [TULIP, POSTGRESQL, REST_API, MCP_SERVER,
                                   SITEWISE_DIRECT, ATHENA])
def test_every_connection_type_refuses_it(ctype):
    stored = {"system_id": SID, "connection_type": ctype, "secret_arn": OWN_ARN}
    res, calls = _run({"connection_type": ctype, "secret_arn": NAMED_ARN},
                      stored=stored, catalog_entry={"name": "X", "connection_type": ctype})
    assert _refused(res)["reason"] == "credential_source_not_editable"
    assert _nothing_done(calls), calls


def test_a_system_that_already_names_a_secret_gets_the_same_refusal():
    stored = {**TULIP_STORED, "credential_source": "secret_arn", "secret_arn": NAMED_ARN}
    catalog_entry = {**TULIP_CATALOG, "secret_arn": NAMED_ARN,
                     "connection_config": {**TULIP_CATALOG["connection_config"],
                                           "credential_source": "secret_arn"}}
    res, calls = _run({"connection_type": TULIP, "secret_arn": NAMED_ARN},
                      stored=stored, catalog_entry=catalog_entry)
    assert _refused(res)["reason"] == "credential_source_not_editable"
    assert _nothing_done(calls), calls


@pytest.mark.parametrize("ctype", [TULIP])
@pytest.mark.parametrize("extra", [{}, {"secret_arn": ""}, {"secret_arn": "  "}])
def test_an_edit_without_it_is_unchanged(ctype, extra):
    stored, catalog_entry, change = INLINE[ctype]
    res, calls = _run({"connection_type": ctype, **change, **extra},
                      stored=stored, catalog_entry=catalog_entry)
    assert isinstance(res, dict) and res["status"] == "ok", res
    assert len(calls["probe"]) == 1 and len(calls["write"]) == 1, calls
    written = {**calls["write"][0]["non_secret"], **calls["write"][0]["secret"]}
    for field, value in change.items():
        assert written[field] == value, written
    assert "secret_arn" not in calls["write"][0]["non_secret"], calls["write"][0]
