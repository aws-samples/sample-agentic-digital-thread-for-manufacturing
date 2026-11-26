# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Quick-gateway REQUEST interceptor (lambda-quick-interceptor/index.py, ADR-0079).

Pins:
  * non-``tools/call`` messages pass through unchanged;
  * ``tools/call`` with a subject ⇒ ``_adt_identity`` minted and
    ``verify_claim`` round-trips ``{sub, groups: []}``;
  * a caller-supplied (forged) ``_adt_identity`` is OVERWRITTEN; with no
    subject it is STRIPPED — the interceptor is the SOLE writer;
  * an unreadable signing key ⇒ stripped (anonymous), never a raise;
  * the output envelope carries ONLY the rewritten body (headers — incl. the
    inbound bearer — are never echoed);
  * logs never contain the bearer, the claim, or the subject.

The handler imports ``identity_claim`` as a sibling module (it is staged next
to a COPY of agents/auth/identity_claim.py); here the real module is aliased.
Offline: Secrets Manager is stubbed.
"""
from __future__ import annotations

import base64
import copy
import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

from agents.auth import identity_claim as ic

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HANDLER_PATH = _REPO_ROOT / "lambda-quick-interceptor" / "index.py"
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "quick_interceptor_event.json"
SECRET = "interceptor-test-key"  # nosec B105 - test fixture
SUB = "delegated-user-42"


def _segment(obj: dict) -> str:
    """One unpadded base64url JWT segment of compact JSON."""
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


#: The fixture's bearer token: an UNSIGNED test JWT, built here and put in
#: place of the fixture's ``<delegated_jwt>`` placeholder by ``_event()``.
_FIXTURE_JWT = ".".join((
    _segment({"alg": "RS256"}),
    _segment({"sub": SUB, "iss": "https://idp.example.com", "aud": "https://gateway.example.com/mcp",
              "act": {"client_id": "example-client"}}),
    "test-signature",
))


def _load_handler():
    sys.modules.setdefault("identity_claim", ic)
    spec = importlib.util.spec_from_file_location("quick_interceptor_index", _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


qi = _load_handler()


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(qi, "_secret_cache", None)
    monkeypatch.setattr(qi, "_fetch_secret", lambda: SECRET)


def _event():
    event = json.loads(_FIXTURE.read_text())
    headers = event["mcp"]["gatewayRequest"]["headers"]
    headers["Authorization"] = headers["Authorization"].replace("<delegated_jwt>", _FIXTURE_JWT)
    return event


def _bearer(claims: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"Bearer eyJhbGciOiJSUzI1NiJ9.{seg}.sig"


def _args(output):
    return output["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]


def test_the_handler_uses_the_one_claim_implementation():
    assert qi.identity_claim is ic
    src = _HANDLER_PATH.read_text()
    assert "hmac" not in src and "hashlib" not in src  # never re-implemented


def test_tools_call_with_a_subject_mints_a_verifiable_claim():
    out = qi.handler(_event(), None)
    args = _args(out)
    assert ic.verify_claim(args[ic.RESERVED_CLAIM_ARG], SECRET)["sub"] == SUB
    assert ic.verify_claim(args[ic.RESERVED_CLAIM_ARG], SECRET)["groups"] == []
    assert args["question"] == "what changed?"


def test_output_envelope_carries_only_the_body():
    out = qi.handler(_event(), None)
    assert set(out) == {"interceptorOutputVersion", "mcp"}
    assert out["interceptorOutputVersion"] == "1.0"
    assert set(out["mcp"]) == {"transformedGatewayRequest"}
    assert set(out["mcp"]["transformedGatewayRequest"]) == {"body"}


def test_a_forged_caller_claim_is_overwritten():
    event = _event()
    forged = ic.mint_claim("attacker", ["admins"], "attacker-key")
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"][ic.RESERVED_CLAIM_ARG] = forged
    claim = _args(qi.handler(event, None))[ic.RESERVED_CLAIM_ARG]
    assert claim != forged
    assert ic.verify_claim(claim, SECRET)["sub"] == SUB


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer not-a-jwt"},
    {"Authorization": _bearer({"iss": "https://idp.example.com"})},  # no sub
    {"Authorization": _bearer({"sub": ""})},
], ids=["no-header", "not-a-jwt", "no-sub", "empty-sub"])
def test_no_subject_strips_the_argument_and_keeps_the_rest(headers):
    event = _event()
    event["mcp"]["gatewayRequest"]["headers"] = headers
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"][ic.RESERVED_CLAIM_ARG] = "forged"
    assert _args(qi.handler(event, None)) == {"question": "what changed?"}


def test_unreadable_key_strips_and_never_raises(monkeypatch):
    monkeypatch.setattr(qi, "_fetch_secret", lambda: None)
    event = _event()
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"][ic.RESERVED_CLAIM_ARG] = "forged"
    assert _args(qi.handler(event, None)) == {"question": "what changed?"}


@pytest.mark.parametrize("method", ["initialize", "tools/list", "notifications/initialized", "ping"])
def test_non_tools_call_passes_through_unchanged(method):
    event = _event()
    event["mcp"]["gatewayRequest"]["body"] = {"jsonrpc": "2.0", "id": 3, "method": method}
    original = copy.deepcopy(event["mcp"]["gatewayRequest"]["body"])
    out = qi.handler(event, None)
    assert out["mcp"]["transformedGatewayRequest"]["body"] == original


def test_batch_bodies_rewrite_every_tools_call():
    event = _event()
    call = event["mcp"]["gatewayRequest"]["body"]
    event["mcp"]["gatewayRequest"]["body"] = [
        copy.deepcopy(call), {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}]
    body = qi.handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]
    assert ic.verify_claim(body[0]["params"]["arguments"][ic.RESERVED_CLAIM_ARG], SECRET)
    assert body[1] == {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}


def test_header_lookup_is_case_insensitive_and_bearer_prefix_optional():
    event = _event()
    token = event["mcp"]["gatewayRequest"]["headers"].pop("Authorization")
    event["mcp"]["gatewayRequest"]["headers"]["authorization"] = token[len("Bearer "):]
    assert qi.extract_delegated_subject(event) == qi.DelegatedSubject(
        sub=SUB, actor_client_present=True)


def test_a_validated_claims_block_takes_precedence():
    event = _event()
    event["mcp"]["gatewayRequest"]["validatedClaims"] = {"sub": "from-claims-block"}
    assert qi.extract_delegated_subject(event).sub == "from-claims-block"


def test_the_key_is_not_read_for_non_tools_call_messages(monkeypatch):
    reads = []
    monkeypatch.setattr(qi, "_fetch_secret", lambda: reads.append(1) or SECRET)
    event = _event()
    event["mcp"]["gatewayRequest"]["body"] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    qi.handler(event, None)
    assert reads == []


def test_logs_are_value_free(caplog):
    event = _event()
    bearer = event["mcp"]["gatewayRequest"]["headers"]["Authorization"]
    with caplog.at_level(logging.DEBUG):
        out = qi.handler(event, None)
    claim = _args(out)[ic.RESERVED_CLAIM_ARG]
    text = caplog.text
    for value in (bearer, bearer.split(".")[1], claim, SUB, SECRET):
        assert value not in text
    assert '"identity": "minted"' in text
    assert '"actor_client_present": true' in text


def test_key_read_by_name_and_failure_logged_without_values(monkeypatch, caplog):
    monkeypatch.undo()  # use the real _fetch_secret
    monkeypatch.setattr(qi, "_secret_cache", None)
    names = []

    class _Sm:
        def get_secret_value(self, SecretId):
            names.append(SecretId)
            raise RuntimeError(f"denied {SECRET}")

    import boto3
    monkeypatch.setattr(boto3, "client", lambda service, **_k: _Sm())
    with caplog.at_level(logging.DEBUG):
        assert qi._fetch_secret() is None
    assert names == [ic.IDENTITY_CLAIM_SECRET_NAME]
    assert SECRET not in caplog.text
