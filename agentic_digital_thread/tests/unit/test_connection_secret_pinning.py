# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A credential read from a Secrets Manager secret is PINNED to the URL and
workspace the secret itself names.

A system may take its credentials from a Secrets Manager secret (the second
credential source, beside a pasted key). When that secret carries ``base_url``
or ``workspace_id`` they decide where the key is sent: a request-supplied URL or
workspace is accepted only when it matches the secret's, otherwise the request
is refused and nothing is probed or written. The URL is not itself secret, but
it decides where the key travels, so it has to travel with the key.

The first test reproduces the leak these rules close: before the pinning, the
admin connection-update route overlaid a submitted ``base_url`` on the resolved
config, kept the stored key, and probed the candidate — so a public host the
caller chose received ``Authorization`` carrying the stored key. All IO mocked.
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
from agents.admin_routes import update_connection as update_connection_route  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402

SID = "tulip-1"
OWN_ARN = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{SID}-AbCdEf"
STORED_URL = "https://acme.tulip.co"
STORED_WS = "abc123"
# The seat secret as the dispenser writes it: credentials PLUS the url/workspace
# the key is pinned to. ``resolve_connection`` merges these onto the resolved
# config and, once pinning is built, they win over any submitted value.
SECRET = {"api_key": "KEY", "api_secret": "SECRET", "base_url": STORED_URL,
          "workspace_id": STORED_WS, "key_scope": "workspace"}
# A credentials-ONLY secret (no url/workspace) models the pre-fix shape that
# makes the leak reproducible: without pinning the url lives only on the node,
# so a submitted base_url is not caught as "set by the secret".
SECRET_KEY_ONLY = {"api_key": "KEY", "api_secret": "SECRET"}
STORED = {"system_id": SID, "connection_type": TULIP, "base_url": STORED_URL,
          "workspace_id": STORED_WS, "key_scope": "workspace", "credential_source": "secret_arn",
          "secret_arn": OWN_ARN, "_secret_unresolved": False, **SECRET}
# Resolved shape for the key-only secret: url/workspace on the node, key merged.
STORED_KEY_ONLY = {"system_id": SID, "connection_type": TULIP, "base_url": STORED_URL,
                   "workspace_id": STORED_WS, "key_scope": "workspace",
                   "credential_source": "secret_arn",
                   "secret_arn": OWN_ARN, "_secret_unresolved": False, **SECRET_KEY_ONLY}
CATALOG = {"name": "Tulip", "connection_type": TULIP, "secret_arn": OWN_ARN,
           "connection_config": {"base_url": STORED_URL, "workspace_id": STORED_WS,
                                 "key_scope": "workspace"}}
PASS = {"status": "pass", "latency_ms": 2, "connection_type": TULIP,
        "reason": "ok", "detail": "ok"}


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


def _run(body, *, resolved=None, catalog_entry=None, probe=PASS, secret=None):
    """Run one admin Edit; return (response, writes, probe_candidates, catalog)."""
    resolved = dict(STORED if resolved is None else resolved)
    read_secret = dict(SECRET if secret is None else secret)
    writes, probes = [], []
    catalog = _Catalog(dict(CATALOG if catalog_entry is None else catalog_entry))

    def _write(system_id, *, non_secret, secret=None, secret_arn=None, entry=None,
               require_node=False, allow_schema_change=False):
        writes.append({"non_secret": dict(non_secret),
                       "secret": dict(secret or {}), "secret_arn": secret_arn})
        return OWN_ARN

    def _probe(system_id, resolved=None):
        probes.append(dict(resolved or {}))
        return probe

    with patch("agents.catalog.catalog", catalog), \
            patch.object(cr, "resolve_connection", lambda sid: dict(resolved)), \
            patch.object(cr, "probe_connection", _probe), \
            patch.object(cr, "write_connection", _write), \
            patch.object(cr, "read_secret_uncached", lambda arn: dict(read_secret)), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
            patch.object(admin_routes, "_reconnect_system", lambda *a, **k: True):
        res = asyncio.run(update_connection_route(SID, _request(), body))
    return res, writes, probes, catalog


def test_admin_edit_cannot_send_the_stored_key_to_a_caller_supplied_url():
    """The pinned URL wins: a different submitted base_url is refused, and the
    stored key is never probed against the caller's host (the closed leak).

    Modelled on the real seat secret, which carries ONLY the credentials — the
    url/workspace live on the node — so without pinning the submitted base_url
    is not caught as "set by the secret" and the probe sends the stored key to
    the caller's host."""
    evil = "https://attacker.example.com"
    res, writes, probes, catalog = _run(
        {"connection_type": TULIP, "base_url": evil},
        resolved=STORED_KEY_ONLY, secret=SECRET_KEY_ONLY)
    assert getattr(res, "status_code", None) == 400, res
    # Nothing was probed against, or written for, the caller's host.
    for probed in probes:
        assert probed.get("base_url") != evil, probed
    for written in writes:
        assert written["non_secret"].get("base_url") != evil, written
    assert writes == [], writes


def test_admin_edit_cannot_send_the_stored_key_to_a_caller_supplied_workspace():
    """Same rule for the workspace the key is scoped to."""
    res, writes, probes, _ = _run(
        {"connection_type": TULIP, "workspace_id": "zzz999"},
        resolved=STORED_KEY_ONLY, secret=SECRET_KEY_ONLY)
    assert getattr(res, "status_code", None) == 400, res
    for probed in probes:
        assert probed.get("workspace_id") != "zzz999", probed
    assert writes == [], writes


def test_admin_edit_matching_the_secret_url_is_a_noop_resave():
    """A re-save that submits the SAME base_url the secret names is accepted as
    a no-op (the matching value is dropped), and nothing is probed against a
    different destination than the secret's own."""
    res, writes, probes, _ = _run({"connection_type": TULIP, "base_url": STORED_URL})
    assert isinstance(res, dict) and res["status"] == "ok", res
    for probed in probes:
        assert probed.get("base_url") == STORED_URL, probed


def test_admin_edit_matching_all_pinned_fields_is_a_noop_resave():
    """The full form re-save (matching base_url + workspace + scope) succeeds."""
    res, writes, probes, _ = _run({"connection_type": TULIP, "base_url": STORED_URL,
                                   "workspace_id": STORED_WS, "key_scope": "workspace"})
    assert isinstance(res, dict) and res["status"] == "ok", res


def test_admin_edit_mismatched_url_is_still_refused():
    """A base_url that DIFFERS from the secret's is still refused (set_by_secret),
    with no probe or write against the caller's host."""
    res, writes, probes, _ = _run({"connection_type": TULIP,
                                   "base_url": "https://evil.example.com"})
    assert getattr(res, "status_code", None) == 400, res
    assert res.body and b"set_by_secret" in res.body
    for probed in probes:
        assert probed.get("base_url") != "https://evil.example.com", probed
    assert writes == []


def test_admin_edit_without_a_target_field_keeps_the_secret_url():
    """A metadata-only edit (no routing field) is fine and stays on the secret's
    url/workspace."""
    res, writes, probes, _ = _run({"connection_type": TULIP, "name": "Renamed"})
    assert isinstance(res, dict) and res["status"] == "ok", res
    assert probes and probes[0].get("base_url") == STORED_URL, probes


# ── item 4b / LOW 2: the registered type is fixed and the fields are allowlisted ──

def test_admin_edit_cannot_switch_an_arn_sourced_system_to_another_type():
    """A body naming a different connection_type (MCP Server) with its own url
    would send the secret-derived token to that url. The registered type is
    held fixed: refuse, probe nothing, write nothing."""
    res, writes, probes, _ = _run(
        {"connection_type": "MCP Server", "url": "https://attacker.example.com/mcp"})
    assert getattr(res, "status_code", None) == 400, res
    assert res.body and b"type_fixed_by_secret" in res.body
    assert writes == [] and probes == [], (writes, probes)


def test_admin_edit_allowlists_fields_and_refuses_health_path_and_targets():
    """An ARN-sourced edit accepts only metadata; every non-pinned routing/auth
    field AND the REST probe's health_path (which chooses the credentialed
    request path on the secret's host) is refused by the ALLOWLIST."""
    for field in ("url", "connection_string", "host", "endpoint", "health_path",
                  "auth_scheme"):
        res, writes, probes, _ = _run({"connection_type": TULIP, field: "x-value"})
        assert getattr(res, "status_code", None) == 400, (field, res)
        assert res.body and b"field_fixed_by_secret" in res.body, field
        assert writes == [] and probes == [], (field, writes, probes)


def test_admin_edit_mismatched_pinned_field_is_refused_as_set_by_secret():
    """A pinned field (base_url/workspace_id/key_scope) whose submitted value
    does NOT match the secret's is refused as set_by_secret, not accepted."""
    for field, bad in (("base_url", "https://evil.example.com"),
                       ("workspace_id", "zzz999"), ("key_scope", "instance")):
        res, writes, probes, _ = _run({"connection_type": TULIP, field: bad})
        assert getattr(res, "status_code", None) == 400, (field, res)
        assert res.body and b"set_by_secret" in res.body, field
        assert writes == [] and probes == [], (field, writes, probes)


def test_real_frontend_resave_body_posts_to_the_backend_and_succeeds():
    """End to end: the body the REAL frontend builders produce for an
    ARN-sourced Tulip re-save (an empty body, routing/auth fields dropped) is
    posted to the REAL backend route and succeeds, with the probe going to the
    secret's own URL and no write of a new destination.

    The frontend side (obEditConnFields -> buildEditConnectionBody emits {})
    is pinned in the frontend suite (onboard-payloads.test.ts); here we post
    that exact empty body."""
    res, writes, probes, _ = _run({})   # the real builder's ARN re-save body
    assert isinstance(res, dict) and res["status"] == "ok", res
    for probed in probes:
        assert probed.get("base_url") == STORED_URL, probed   # the secret's URL, not a new one


def test_admin_edit_keeping_the_type_with_only_metadata_still_works():
    """The allowlist does not block a metadata-only re-save (name/description)."""
    res, writes, probes, _ = _run({"connection_type": TULIP, "description": "note"})
    assert isinstance(res, dict) and res["status"] == "ok", res
