# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Delegated (on-behalf-of) user identity on the Quick path — app side (ADR-0079).

The query-bridge forwards the interceptor-minted ADR-0049 claim VERBATIM as
``x-adt-identity``. The app is the ONE verifier. Pins:

  * header absent ⇒ the anonymous ``quick-gateway`` identity (today) and NO
    signing-key read;
  * a valid claim ⇒ the delegated subject, a plain user
    (``is_admin``/``is_integrator`` False), ``request.state.delegated`` True;
  * a present-but-unverifiable header (malformed / bad signature / expired /
    signing key unreadable) ⇒ 401 with the DISTINCT detail — never the
    shared-identity fallback;
  * the header on a NON-quick path is ignored (normal auth applies);
  * the key is read BY NAME and cached; its read failure logs no value;
  * the key is read ONLY when the app task carries the grant flag
    (``IDENTITY_CLAIM_KEY_READABLE``, set by the compute stack exactly when it
    grants the read); without it no read is attempted and a present header
    is still refused.

Offline: Cognito and Secrets Manager are stubbed; no network.
"""

import logging
import types

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import agents.auth.middleware as middleware_mod
from agents.auth import delegated_identity as di
from agents.auth import identity_claim as ic
from agents.auth.cognito import TokenValidationError
from agents.auth.middleware import AuthMiddleware

_TOKEN = "unit-test-gateway-token"  # nosec B105 - test fixture
SECRET = "delegated-test-key"  # nosec B105 - test fixture
SUB = "delegated-user-42"


class _StubAuth:
    def __init__(self, *a, **k):
        pass

    def validate_token(self, token, id_token=None, **_kwargs):
        raise TokenValidationError("bad token")


@pytest.fixture(autouse=True)
def _fresh_cache():
    di.reset_identity_claim_secret_cache()
    yield
    di.reset_identity_claim_secret_cache()


@pytest.fixture()
def key_reads(monkeypatch):
    """Stub the by-NAME key fetch; records every read."""
    reads = []

    def _fetch():
        reads.append(1)
        return SECRET

    monkeypatch.setattr(di, "_fetch_identity_claim_secret", _fetch)
    return reads


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(middleware_mod, "CognitoAuth", _StubAuth)
    monkeypatch.setattr(middleware_mod, "get_config",
                        lambda: types.SimpleNamespace(has_cognito=True))
    monkeypatch.delenv("EDGE_AUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", _TOKEN)
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.post("/api/ask/async")
    async def ask_async(request: Request):
        return {"who": request.state.user.user_id,
                "username": request.state.user.username,
                "is_admin": request.state.is_admin,
                "is_integrator": request.state.is_integrator,
                "delegated": request.state.delegated}

    @app.get("/api/dashboard")
    async def dashboard():
        return {"cards": []}

    return TestClient(app)


def _headers(claim=None):
    headers = {"x-quick-auth": _TOKEN}
    if claim is not None:
        headers[di.DELEGATED_IDENTITY_HEADER] = claim
    return headers


def test_header_name_is_the_one_shared_constant():
    from agents import query_bridge_server as qbs
    import inspect
    assert di.DELEGATED_IDENTITY_HEADER == "x-adt-identity"
    # the bridge imports the constant — it never spells the header itself
    assert '"x-adt-identity"' not in inspect.getsource(qbs)


def test_absent_header_is_the_anonymous_quick_gateway_identity(client, key_reads):
    body = client.post("/api/ask/async", headers=_headers(), json={}).json()
    assert body == {"who": "quick-gateway", "username": "quick-gateway",
                    "is_admin": False, "is_integrator": False,
                    "delegated": False}
    assert key_reads == []  # no key read on the anonymous path


def test_valid_claim_runs_as_the_delegated_plain_user(client, key_reads):
    claim = ic.mint_claim(SUB, [], SECRET)
    resp = client.post("/api/ask/async", headers=_headers(claim), json={})
    assert resp.status_code == 200
    assert resp.json() == {"who": SUB, "username": SUB, "is_admin": False,
                           "is_integrator": False, "delegated": True}


@pytest.mark.parametrize("claim_factory", [
    lambda: "not-a-claim",
    lambda: ic.mint_claim(SUB, [], "a-different-key"),
    lambda: ic.mint_claim(SUB, [], SECRET, now=1_000_000),  # long expired
    lambda: "x.y.z",
], ids=["malformed", "bad-signature", "expired", "three-segments"])
def test_unverifiable_claim_is_401_never_the_shared_identity(
        client, key_reads, claim_factory):
    resp = client.post("/api/ask/async", headers=_headers(claim_factory()), json={})
    assert resp.status_code == 401
    assert resp.json() == {"detail": di.DELEGATED_IDENTITY_REJECTED_DETAIL}


def test_unreadable_signing_key_fails_closed_401(client, monkeypatch):
    monkeypatch.setattr(di, "_fetch_identity_claim_secret", lambda: None)
    claim = ic.mint_claim(SUB, [], SECRET)
    resp = client.post("/api/ask/async", headers=_headers(claim), json={})
    assert resp.status_code == 401
    assert resp.json()["detail"] == di.DELEGATED_IDENTITY_REJECTED_DETAIL


def test_header_without_the_shared_secret_is_still_401_unauthorized(client, key_reads):
    # The claim never ADMITS a request — only x-quick-auth does.
    claim = ic.mint_claim(SUB, [], SECRET)
    resp = client.post("/api/ask/async",
                       headers={di.DELEGATED_IDENTITY_HEADER: claim}, json={})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}
    assert key_reads == []


def test_header_on_a_non_quick_path_is_ignored(client, key_reads):
    claim = ic.mint_claim(SUB, [], SECRET)
    resp = client.get("/api/dashboard", headers=_headers(claim))
    assert resp.status_code == 401  # normal auth applies; no delegated admit
    assert key_reads == []


#: The flag the compute stack sets on the app task ONLY when its role may read
#: the signing key (spelled literally here so the behaviour tests below fail
#: on code that has no such gate; the constant is pinned to it separately).
_KEY_READABLE_ENV = "IDENTITY_CLAIM_KEY_READABLE"


def test_key_readable_flag_is_the_one_shared_constant():
    assert di.IDENTITY_CLAIM_KEY_READABLE_ENV == _KEY_READABLE_ENV


def _recording_secrets_manager(monkeypatch, *, answer):
    """Patch boto3.client; returns the list of SecretIds read."""
    calls = []

    class _Sm:
        def get_secret_value(self, SecretId):
            calls.append(SecretId)
            if isinstance(answer, Exception):
                raise answer
            return {"SecretString": answer}

    import boto3
    monkeypatch.setattr(boto3, "client", lambda service, **_k: _Sm())
    return calls


def test_no_key_read_when_this_app_is_not_granted_the_key(monkeypatch):
    """Workshop deploy 2026-10-20: the task role may read the key only in the
    delegated-token mode, so on every other deploy a read is AccessDenied in
    CloudTrail. Without the grant flag the key is never read, and a present
    claim is still refused (fail closed, never the shared identity)."""
    monkeypatch.delenv(_KEY_READABLE_ENV, raising=False)
    calls = _recording_secrets_manager(
        monkeypatch, answer=RuntimeError("AccessDeniedException"))
    claim = ic.mint_claim(SUB, [], SECRET)
    assert di._fetch_identity_claim_secret() is None
    with pytest.raises(di.DelegatedIdentityRejected):
        di.resolve_delegated_identity({di.DELEGATED_IDENTITY_HEADER: claim})
    assert calls == []


def test_not_granted_is_logged_once_without_values(monkeypatch, caplog):
    monkeypatch.delenv(_KEY_READABLE_ENV, raising=False)
    _recording_secrets_manager(monkeypatch, answer=SECRET)
    with caplog.at_level(logging.INFO, logger=di.logger.name):
        for _ in range(3):
            assert di._fetch_identity_claim_secret() is None
    notes = [r for r in caplog.records if _KEY_READABLE_ENV in r.getMessage()]
    assert len(notes) == 1 and notes[0].levelno == logging.INFO
    assert SECRET not in caplog.text


def test_resolver_reads_the_key_by_name_and_caches(monkeypatch):
    monkeypatch.setenv(_KEY_READABLE_ENV, "1")
    calls = _recording_secrets_manager(monkeypatch, answer=SECRET)
    claim = ic.mint_claim(SUB, [], SECRET)
    for _ in range(3):
        assert di.resolve_delegated_identity({di.DELEGATED_IDENTITY_HEADER: claim}) \
            == di.DelegatedIdentity(sub=SUB)
    assert calls == [ic.IDENTITY_CLAIM_SECRET_NAME]


def test_key_read_failure_logs_no_value_and_rejects(monkeypatch, caplog):
    monkeypatch.setenv(_KEY_READABLE_ENV, "1")
    _recording_secrets_manager(monkeypatch, answer=RuntimeError(f"denied for {SECRET}"))
    claim = ic.mint_claim(SUB, [], SECRET)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(di.DelegatedIdentityRejected):
            di.resolve_delegated_identity({di.DELEGATED_IDENTITY_HEADER: claim})
    assert SECRET not in caplog.text
    assert claim not in caplog.text
    assert SUB not in caplog.text
