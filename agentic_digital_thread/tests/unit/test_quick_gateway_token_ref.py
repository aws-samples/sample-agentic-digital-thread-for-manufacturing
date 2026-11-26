# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The query-bridge reads the x-quick-auth token BY REFERENCE (S1b).

Runtime env is plaintext to any caller of GetAgentRuntime, so the bridge
runtime gets ``QUICK_GATEWAY_TOKEN_REF`` (the ARN of the secret or SSM
parameter holding the token) instead of the token. Pinned here:

* provisioning (``runtime_provision_helpers._query_bridge_env``): a valid
  reference replaces the plaintext key; no reference keeps today's env
  byte-identical, placeholder normalization included;
* the ONE format rule (``runtime_env_contract.parse_quick_gateway_token_ref``)
  and the contract's key registry;
* the bridge resolver (``agents/quick_gateway_token.py``): secret and SSM
  references, plaintext fallback, caching, failure as a safe error, and the
  value never logged;
* the bridge tools: same headers for the same token, and a resolution failure
  is the tool's structured error, never a raise.

Offline: AWS clients and httpx are stubbed.
"""
from __future__ import annotations

import json
import logging

import pytest
from botocore.exceptions import ClientError

from agents import query_bridge_server as qbs
from agents import quick_gateway_token as qgt
from agents.connection_types import QUERY_BRIDGE_CONNECTION_TYPE
from agents.runtime_env_contract import (
    COMPOSED_SYSTEM_KEYS,
    QUICK_GATEWAY_TOKEN_REF,
    known_keys,
    parse_quick_gateway_token_ref,
)
from agents.runtime_provision_helpers import _query_bridge_env, runtime_connection_env

SECRET_ARN = ("arn:aws:secretsmanager:us-east-1:111122223333:secret:"  # nosec B105 - a secret's ARN with a placeholder account, not a value
              "QuickGatewayTokenSecret-AbCdEf")
SSM_ARN = "arn:aws:ssm:us-west-2:111122223333:parameter/adt/quick-gateway-token"
TOKEN = "tok-SECRET-value-0123456789"  # nosec B105 - offline test fixture
APP_URL = "http://internal-alb.example.com"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in ("ADT_APP_URL", "ADT_APP_INTERNAL_URL", "QUICK_GATEWAY_TOKEN",
                QUICK_GATEWAY_TOKEN_REF):
        monkeypatch.delenv(key, raising=False)
    qgt.forget_cached_token()
    yield
    qgt.forget_cached_token()


# --------------------------------------------------------------------------- #
# Provisioning: _query_bridge_env
# --------------------------------------------------------------------------- #
def _legacy_query_bridge_env(environ: dict) -> dict:
    """The function as it was before the token reference, kept verbatim
    so 'no reference' is compared byte for byte."""
    app_url = (environ.get("ADT_APP_URL")
               or environ.get("ADT_APP_INTERNAL_URL", "")).strip()
    token = environ.get("QUICK_GATEWAY_TOKEN", "").strip()
    if token.lower() == "placeholder":
        token = ""  # nosec B105 - a cleared token, not a credential
    env = {}
    if app_url:
        env["ADT_APP_URL"] = app_url
    if token:
        env["QUICK_GATEWAY_TOKEN"] = token
    return env


@pytest.mark.parametrize("environ", [
    {},
    {"ADT_APP_URL": APP_URL, "QUICK_GATEWAY_TOKEN": TOKEN},
    {"ADT_APP_INTERNAL_URL": APP_URL, "QUICK_GATEWAY_TOKEN": f"  {TOKEN}\n"},
    {"ADT_APP_URL": APP_URL, "QUICK_GATEWAY_TOKEN": "placeholder"},
    {"ADT_APP_URL": APP_URL, "QUICK_GATEWAY_TOKEN": "PlaceHolder"},
    {"QUICK_GATEWAY_TOKEN": TOKEN},
    {"ADT_APP_URL": APP_URL, "QUICK_GATEWAY_TOKEN": TOKEN,
     QUICK_GATEWAY_TOKEN_REF: "   "},
], ids=["empty", "url+token", "internal-url+padded-token", "placeholder",
        "placeholder-mixed-case", "token-only", "blank-ref"])
def test_no_reference_keeps_todays_env_byte_identical(monkeypatch, environ):
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    env = _query_bridge_env()
    assert list(env.items()) == list(_legacy_query_bridge_env(environ).items())


@pytest.mark.parametrize("ref", [SECRET_ARN, SSM_ARN])
@pytest.mark.parametrize("token", [TOKEN, "placeholder", ""])
def test_a_reference_replaces_the_plaintext_token(monkeypatch, ref, token):
    monkeypatch.setenv("ADT_APP_URL", APP_URL)
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", token)
    monkeypatch.setenv(QUICK_GATEWAY_TOKEN_REF, f" {ref} ")
    env = _query_bridge_env()
    assert list(env.items()) == [("ADT_APP_URL", APP_URL),
                                 (QUICK_GATEWAY_TOKEN_REF, ref)]
    assert "QUICK_GATEWAY_TOKEN" not in env
    assert TOKEN not in json.dumps(env)


def test_the_connection_env_router_carries_the_reference(monkeypatch):
    monkeypatch.setenv("ADT_APP_URL", APP_URL)
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setenv(QUICK_GATEWAY_TOKEN_REF, SECRET_ARN)
    env = runtime_connection_env(QUERY_BRIDGE_CONNECTION_TYPE, "", {})
    assert env == {"ADT_APP_URL": APP_URL, QUICK_GATEWAY_TOKEN_REF: SECRET_ARN}


def test_a_malformed_reference_keeps_todays_env_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("ADT_APP_URL", APP_URL)
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setenv(QUICK_GATEWAY_TOKEN_REF, "ssm:/adt/quick-gateway-token")
    with caplog.at_level(logging.WARNING):
        env = _query_bridge_env()
    assert env == {"ADT_APP_URL": APP_URL, "QUICK_GATEWAY_TOKEN": TOKEN}
    assert QUICK_GATEWAY_TOKEN_REF in caplog.text
    assert TOKEN not in caplog.text


# --------------------------------------------------------------------------- #
# The format rule and the contract registry
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value, service, region", [
    (SECRET_ARN, "secretsmanager", "us-east-1"),
    (SSM_ARN, "ssm", "us-west-2"),
    ("arn:aws:ssm:eu-west-1:111122223333:parameter/flat-name", "ssm", "eu-west-1"),
    ("arn:aws-cn:secretsmanager:cn-north-1:111122223333:secret:x-1", "secretsmanager",
     "cn-north-1"),
    (f"  {SECRET_ARN}\n", "secretsmanager", "us-east-1"),
])
def test_parse_accepts_secret_and_parameter_arns(value, service, region):
    ref = parse_quick_gateway_token_ref(value)
    assert ref is not None
    assert (ref.service, ref.arn, ref.region) == (service, value.strip(), region)


@pytest.mark.parametrize("value", [
    None, "", "   ", 42,
    "ssm:/adt/quick-gateway-token",
    "/adt/quick-gateway-token",
    "QuickGatewayTokenSecret-AbCdEf",
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:",
    "arn:aws:ssm:us-east-1:111122223333:parameter/",
    "arn:aws:ssm:us-east-1:111122223333:document/adt",
    "arn:aws:secretsmanager:us-east-1:111122223333:parameter/adt",
    "arn:aws:kms:us-east-1:111122223333:key/abc",
    "arn:aws:secretsmanager::111122223333:secret:x",
    "arn:aws:secretsmanager:us-east-1:12345:secret:x",
    "arn:aws:secretsmanager:us-east-1:abcdabcdabcd:secret:x",
    "arn:other:secretsmanager:us-east-1:111122223333:secret:x",
    "urn:aws:secretsmanager:us-east-1:111122223333:secret:x",
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:x y",
])
def test_parse_rejects_everything_else(value):
    assert parse_quick_gateway_token_ref(value) is None


def test_the_contract_knows_the_reference_key():
    assert QUICK_GATEWAY_TOKEN_REF == "QUICK_GATEWAY_TOKEN_REF"  # nosec B105 - an env var name, not a value
    # Both keys stay known: an older bridge still carries the plaintext one,
    # and the drift lane must not call either of them foreign.
    assert {"QUICK_GATEWAY_TOKEN", QUICK_GATEWAY_TOKEN_REF} <= COMPOSED_SYSTEM_KEYS
    assert {"QUICK_GATEWAY_TOKEN", QUICK_GATEWAY_TOKEN_REF} <= known_keys("system")
    assert QUICK_GATEWAY_TOKEN_REF not in known_keys("orchestrator")


def test_the_resolver_uses_the_contract_rule():
    assert qgt.parse_ref is parse_quick_gateway_token_ref
    assert qgt.TOKEN_REF_ENV == QUICK_GATEWAY_TOKEN_REF


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #
class _Store:
    """Stub factory + client for both stores; records every call."""

    def __init__(self, secret=TOKEN, parameter=TOKEN, error=None):
        self.secret, self.parameter, self.error = secret, parameter, error
        self.clients: list = []
        self.calls: list = []

    def __call__(self, service, region):
        self.clients.append((service, region))
        return self

    def get_secret_value(self, **kwargs):
        self.calls.append(("get_secret_value", kwargs))
        if self.error:
            raise self.error
        return {"SecretString": self.secret} if self.secret is not None else {
            "SecretBinary": b"\x00"}

    def get_parameter(self, **kwargs):
        self.calls.append(("get_parameter", kwargs))
        if self.error:
            raise self.error
        return {"Parameter": {"Name": "/adt/quick-gateway-token", "Value": self.parameter}}


def test_no_reference_returns_the_plaintext_env_unchanged():
    store = _Store()
    assert qgt.resolve_gateway_token({}, client_factory=store) == ""
    assert qgt.resolve_gateway_token({"QUICK_GATEWAY_TOKEN": TOKEN},
                                     client_factory=store) == TOKEN
    # Exactly what the bridge sent before: no strip, no normalization.
    assert qgt.resolve_gateway_token({"QUICK_GATEWAY_TOKEN": " x "},
                                     client_factory=store) == " x "
    assert store.clients == [] and store.calls == []


def test_a_secret_reference_reads_secrets_manager_in_the_arn_region():
    store = _Store(secret=f"{TOKEN}\n")
    env = {QUICK_GATEWAY_TOKEN_REF: SECRET_ARN, "QUICK_GATEWAY_TOKEN": "stale"}
    assert qgt.resolve_gateway_token(env, client_factory=store) == TOKEN
    assert store.clients == [("secretsmanager", "us-east-1")]
    assert store.calls == [("get_secret_value", {"SecretId": SECRET_ARN})]


def test_an_ssm_reference_reads_the_parameter_with_decryption():
    store = _Store(parameter=TOKEN)
    assert qgt.resolve_gateway_token({QUICK_GATEWAY_TOKEN_REF: SSM_ARN},
                                     client_factory=store) == TOKEN
    assert store.clients == [("ssm", "us-west-2")]
    assert store.calls == [("get_parameter", {"Name": SSM_ARN, "WithDecryption": True})]


def test_the_value_is_read_once_and_cached_per_reference():
    store = _Store()
    env = {QUICK_GATEWAY_TOKEN_REF: SECRET_ARN}
    for _ in range(3):
        assert qgt.resolve_gateway_token(env, client_factory=store) == TOKEN
    assert len(store.calls) == 1
    other = _Store(parameter="other-token")
    assert qgt.resolve_gateway_token({QUICK_GATEWAY_TOKEN_REF: SSM_ARN},
                                     client_factory=other) == "other-token"
    qgt.forget_cached_token()
    qgt.resolve_gateway_token(env, client_factory=store)
    assert len(store.calls) == 2


@pytest.mark.parametrize("stored", ["placeholder", " PLACEHOLDER ", "", "   "])
def test_an_unwritten_token_is_no_token_and_is_not_cached(stored):
    store = _Store(parameter=stored)
    env = {QUICK_GATEWAY_TOKEN_REF: SSM_ARN}
    assert qgt.resolve_gateway_token(env, client_factory=store) == ""
    store.parameter = TOKEN  # the real token is written later
    assert qgt.resolve_gateway_token(env, client_factory=store) == TOKEN
    assert len(store.calls) == 2


def test_a_failed_read_is_a_safe_error_and_is_retried(caplog):
    leaky = ClientError({"Error": {"Code": "AccessDeniedException",
                                   "Message": f"detail {TOKEN} {SECRET_ARN}"}},
                        "GetSecretValue")
    store = _Store(error=leaky)
    env = {QUICK_GATEWAY_TOKEN_REF: SECRET_ARN}
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(qgt.TokenResolutionError) as info:
            qgt.resolve_gateway_token(env, client_factory=store)
    message = str(info.value)
    assert "AccessDeniedException" in message and "Secrets Manager" in message
    assert TOKEN not in message and SECRET_ARN not in message
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert TOKEN not in caplog.text and SECRET_ARN not in caplog.text
    assert "AccessDeniedException" in caplog.text
    store.error = None
    assert qgt.resolve_gateway_token(env, client_factory=store) == TOKEN


def test_a_non_aws_failure_names_the_exception_class_only():
    store = _Store(error=TimeoutError(f"connect timeout {TOKEN}"))
    with pytest.raises(qgt.TokenResolutionError, match=r"\(TimeoutError\)") as info:
        qgt.resolve_gateway_token({QUICK_GATEWAY_TOKEN_REF: SSM_ARN}, client_factory=store)
    assert TOKEN not in str(info.value)


def test_a_binary_secret_is_an_error_not_a_token():
    store = _Store(secret=None)
    with pytest.raises(qgt.TokenResolutionError, match="not a string"):
        qgt.resolve_gateway_token({QUICK_GATEWAY_TOKEN_REF: SECRET_ARN}, client_factory=store)


def test_a_malformed_reference_on_the_runtime_is_an_error():
    store = _Store()
    with pytest.raises(qgt.TokenResolutionError, match=QUICK_GATEWAY_TOKEN_REF):
        qgt.resolve_gateway_token({QUICK_GATEWAY_TOKEN_REF: "ssm:/adt/x",
                                   "QUICK_GATEWAY_TOKEN": TOKEN}, client_factory=store)
    assert store.calls == []


def test_a_successful_read_logs_nothing_about_the_value(caplog):
    store = _Store()
    with caplog.at_level(logging.DEBUG):
        qgt.resolve_gateway_token({QUICK_GATEWAY_TOKEN_REF: SECRET_ARN}, client_factory=store)
    assert TOKEN not in caplog.text


# --------------------------------------------------------------------------- #
# The bridge tools
# --------------------------------------------------------------------------- #
class _Client:
    calls: list = []
    status = 200

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _respond(self, method, url, headers, payload):
        import httpx
        type(self).calls.append({"method": method, "url": url, "headers": headers})
        request = httpx.Request(method, url)
        return httpx.Response(type(self).status, request=request, json=payload)

    def post(self, url, json=None, headers=None):
        return self._respond("POST", url, headers, {"job_id": "j1", "status": "running"})

    def get(self, url, headers=None, timeout=None, params=None):
        return self._respond("GET", url, headers, {"status": "done", "answer": "42"})


@pytest.fixture()
def http(monkeypatch):
    import httpx
    _Client.calls, _Client.status = [], 200
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setenv("ADT_APP_URL", APP_URL)
    return _Client


@pytest.fixture()
def store(monkeypatch):
    stub = _Store()
    monkeypatch.setattr(qgt, "_default_client", stub)
    return stub


def test_headers_are_the_same_for_the_same_token_either_way(monkeypatch, store):
    from agents.runtime_asgi import identity_claim_var

    reset = identity_claim_var.set(None)
    try:
        monkeypatch.setenv("QUICK_GATEWAY_TOKEN", TOKEN)
        plaintext = qbs._gateway_headers()
        monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "stale-plaintext")
        monkeypatch.setenv(QUICK_GATEWAY_TOKEN_REF, SECRET_ARN)
        by_reference = qbs._gateway_headers()
    finally:
        identity_claim_var.reset(reset)
    assert by_reference == plaintext == {"Content-Type": "application/json",
                                         qbs.QUICK_GATEWAY_HEADER: TOKEN}
    assert list(by_reference) == list(plaintext)


@pytest.mark.parametrize("tool", ["submit_query", "get_query_result", "get_prompts"])
def test_every_tool_sends_the_referenced_token(monkeypatch, http, store, tool):
    monkeypatch.setenv(QUICK_GATEWAY_TOKEN_REF, SSM_ARN)
    call = {"submit_query": lambda: qbs.submit_query("q"),
            "get_query_result": lambda: qbs.get_query_result("j1"),
            "get_prompts": qbs.get_prompts}[tool]
    call()
    (sent,) = http.calls
    assert sent["headers"][qbs.QUICK_GATEWAY_HEADER] == TOKEN


@pytest.mark.parametrize("tool, field", [
    ("submit_query", "error"), ("get_query_result", "error"), ("get_prompts", "message")])
def test_a_resolution_failure_is_the_tools_structured_error(monkeypatch, http, store,
                                                            tool, field):
    store.error = ClientError({"Error": {"Code": "AccessDeniedException",
                                         "Message": TOKEN}}, "GetParameter")
    monkeypatch.setenv(QUICK_GATEWAY_TOKEN_REF, SSM_ARN)
    call = {"submit_query": lambda: qbs.submit_query("q"),
            "get_query_result": lambda: qbs.get_query_result("j1"),
            "get_prompts": qbs.get_prompts}[tool]
    reply = call()
    payload = json.loads(reply)
    assert http.calls == []  # nothing reached the app without a token
    assert "TokenResolutionError" in payload[field]  # a typed, fixed-text error
    if tool == "get_prompts":
        assert payload["prompts"] == []
    else:
        assert payload["status"] == "error"
        assert "AccessDeniedException" in payload["error"]
    assert TOKEN not in reply


@pytest.mark.parametrize("tool", ["submit_query", "get_query_result", "get_prompts"])
def test_a_401_drops_the_cached_token(monkeypatch, http, store, tool):
    monkeypatch.setenv(QUICK_GATEWAY_TOKEN_REF, SECRET_ARN)
    call = {"submit_query": lambda: qbs.submit_query("q"),
            "get_query_result": lambda: qbs.get_query_result("j1"),
            "get_prompts": qbs.get_prompts}[tool]
    http.status = 401
    reply = json.loads(call())
    if tool == "get_prompts":
        assert reply["prompts"] == [] and "HTTPStatusError" in reply["message"]
    else:
        assert reply["status"] == "error" and "HTTP 401" in reply["error"]
    store.secret = "rotated-token"  # nosec B105 - a fake test token
    http.status = 200
    call()
    assert http.calls[-1]["headers"][qbs.QUICK_GATEWAY_HEADER] == "rotated-token"
    assert len(store.calls) == 2


def test_other_http_errors_keep_the_cached_token(monkeypatch, http, store):
    monkeypatch.setenv(QUICK_GATEWAY_TOKEN_REF, SECRET_ARN)
    http.status = 500
    assert json.loads(qbs.get_query_result("j1"))["status"] == "error"
    http.status = 200
    qbs.get_query_result("j1")
    assert len(store.calls) == 1
