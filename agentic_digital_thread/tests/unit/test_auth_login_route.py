# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Login route hardening.

* **Generic 401 detail** — an authentication failure returns the fixed,
  human-readable ``'Incorrect username or password.'`` (the frontend renders
  ``detail`` verbatim). The raw boto3/Cognito exception text (which can leak
  user-enumeration hints, pool config, request ids) never reaches the client;
  the server log keeps only the exception class, AWS error code and request id.
* **Throttle/lockout responses from Cognito itself** stay distinct (429), but the raw
  boto3 text is likewise never echoed to the client.
* **In-memory failure throttle** — after 5 consecutive failures for the same
  (client-ip, email) pair the route rejects with 429 + ``Retry-After`` for 60s;
  a successful login resets the streak; the streak expires after the window.
  (Best-effort/per-process by design — see the comment in agents/auth/routes.py.)
"""
import hashlib
import logging
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.auth.routes as routes  # noqa: E402
from agents.auth.cognito import AuthenticationError  # noqa: E402

_RAW_BOTO_TEXT = (
    "An error occurred (NotAuthorizedException) when calling the InitiateAuth "
    "operation: Incorrect username or password. RequestId: abc-123-secret-internals"
)
_GENERIC = "Incorrect username or password."


class _StubAuth:
    """CognitoAuth stand-in; behavior driven by the module-level _STATE dict."""

    def __init__(self):
        pass

    def authenticate(self, email, password):
        exc = _STATE.get("exc")
        if exc is not None:
            raise exc
        return types.SimpleNamespace(access_token="at", id_token="it", refresh_token=None)  # nosec B106 - offline test fixture/placeholder credential, not a live secret

    def validate_token(self, token, id_token=None):
        return types.SimpleNamespace(user_id="u1", email="user@example.com", username="user")

    def get_user_groups(self, username):
        return []

    @staticmethod
    def is_admin(groups):
        return False


_STATE: dict = {"exc": None}


def _auth_error(message, code=None, request_id=None):
    """An AuthenticationError carrying the structured fields cognito.py keeps
    from the AWS error (set as attributes, the shape the route reads)."""
    exc = AuthenticationError(message)
    exc.code, exc.request_id = code, request_id
    return exc


@pytest.fixture()
def client(monkeypatch):
    _STATE["exc"] = _auth_error(_RAW_BOTO_TEXT, code="NotAuthorizedException",
                                request_id="abc-123-secret-internals")
    routes._login_failures.clear()
    monkeypatch.setattr(routes, "CognitoAuth", _StubAuth)
    monkeypatch.setattr(routes, "get_config",
                        lambda: types.SimpleNamespace(has_cognito=True))
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _post(client, email="user@example.com", password="wrong"):  # nosec B107 - empty default sentinel; real value supplied by caller
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_failed_login_log_has_no_raw_email(client, caplog):
    with caplog.at_level(logging.WARNING, logger="agents.auth.routes"):
        _post(client, email="Person.Name@example.com")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "person.name" not in text.lower()
    assert "example.com" not in text
    assert "Login failed" in text


# --------------------------------------------------------------------------- #
# Generic 401 detail
# --------------------------------------------------------------------------- #
def test_auth_failure_returns_generic_detail_and_logs_raw(client, caplog):
    with caplog.at_level(logging.WARNING, logger="agents.auth.routes"):
        r = _post(client)
    assert r.status_code == 401
    assert r.json()["detail"] == _GENERIC
    # Raw boto3 text never reaches the client...
    assert "RequestId" not in r.text and "InitiateAuth" not in r.text
    # ...but its AWS request id IS logged server-side for diagnosis.
    assert "abc-123-secret-internals" in caplog.text


def test_auth_failure_log_does_not_carry_the_email(client, caplog):
    # A failed sign-in logs a stable fingerprint of the address, never the
    # address itself (the log is not a place to keep personal data).
    with caplog.at_level(logging.WARNING, logger="agents.auth.routes"):
        _post(client, email="Person.Name@Example.com")
        _post(client, email="person.name@example.com ")
    assert "person.name" not in caplog.text.lower()
    assert "example.com" not in caplog.text.lower()
    fingerprints = [r.args[0] for r in caplog.records if r.msg.startswith("Login failed")]
    assert len(fingerprints) == 2 and fingerprints[0] == fingerprints[1]
    assert fingerprints[0] == routes._email_fingerprint("person.name@example.com")


def test_email_fingerprint_is_keyed_not_plain_sha256(monkeypatch):
    # Stable within the process (so throttle-window failures line up) but
    # keyed, so hashing a list of likely addresses does not recover it.
    fingerprint = routes._email_fingerprint("Person.Name@Example.com")
    assert fingerprint == routes._email_fingerprint(" person.name@example.com ")
    plain = hashlib.sha256(b"person.name@example.com").hexdigest()
    assert not plain.startswith(fingerprint)
    monkeypatch.setattr(routes, "_FINGERPRINT_KEY", b"\x00" * 32)
    assert routes._email_fingerprint("person.name@example.com") != fingerprint


def test_auth_failure_log_omits_exception_message(client, caplog):
    # The provider's message text is free-form and could carry the address;
    # only the exception class, AWS error code and request id are logged.
    _STATE["exc"] = _auth_error("Cognito rejected person.name@example.com",
                                code="NotAuthorizedException", request_id="req-42")
    with caplog.at_level(logging.WARNING, logger="agents.auth.routes"):
        assert _post(client, email="person.name@example.com").status_code == 401
    text = caplog.text.lower()
    assert "person.name" not in text and "example.com" not in text
    assert "cognito rejected" not in text
    assert "AuthenticationError" in caplog.text
    assert "NotAuthorizedException" in caplog.text and "req-42" in caplog.text


def test_cognito_authenticate_keeps_aws_error_code_and_request_id():
    from botocore.exceptions import ClientError

    from agents.auth.cognito import CognitoAuth

    error = ClientError(
        {"Error": {"Code": "NotAuthorizedException",
                   "Message": "Incorrect username or password."},
         "ResponseMetadata": {"RequestId": "req-77"}},
        "InitiateAuth",
    )

    def _raise(**_kwargs):
        raise error

    auth = CognitoAuth.__new__(CognitoAuth)
    auth.client_id, auth.client_secret = "client", None
    auth._client = types.SimpleNamespace(
        initiate_auth=_raise,
        exceptions=types.SimpleNamespace(NotAuthorizedException=type("Never", (Exception,), {})),
    )
    with pytest.raises(AuthenticationError) as caught:
        auth.authenticate("person.name@example.com", "pw")  # nosec B106 - offline test fixture
    assert caught.value.code == "NotAuthorizedException"
    assert caught.value.request_id == "req-77"
    assert caught.value.__cause__ is error


def test_cognito_lockout_maps_to_429_without_raw_text(client):
    _STATE["exc"] = AuthenticationError(
        "Cognito authentication failed", throttled=True,
    )
    r = _post(client)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    detail = r.json()["detail"]
    assert "Password attempts exceeded" not in detail
    assert detail == routes._GENERIC_THROTTLED  # human-readable, generic


def _client_error(code, message):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message},
                        "ResponseMetadata": {"RequestId": "req-9"}}, "InitiateAuth")


@pytest.mark.parametrize("code,message", [
    ("NotAuthorizedException", "Password attempts exceeded"),
    ("TooManyRequestsException", "Rate exceeded"),
    ("TooManyFailedAttemptsException", "Too many failed attempts"),
    ("LimitExceededException", "Attempt limit exceeded"),
])
def test_real_cognito_throttle_errors_still_map_to_429(client, code, message):
    # Built through cognito._auth_error, the path production takes, which
    # replaces the provider text with a fixed message.
    from agents.auth.cognito import _auth_error as wrap

    _STATE["exc"] = wrap(_client_error(code, message))
    assert _post(client).status_code == 429


def test_plain_bad_password_from_cognito_is_401(client):
    from agents.auth.cognito import _auth_error as wrap

    _STATE["exc"] = wrap(_client_error("NotAuthorizedException",
                                       "Incorrect username or password."))
    assert _post(client).status_code == 401


# --------------------------------------------------------------------------- #
# In-memory throttle
# --------------------------------------------------------------------------- #
def test_lockout_after_five_consecutive_failures(client):
    for _ in range(5):
        assert _post(client).status_code == 401
    r = _post(client)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    detail = r.json()["detail"]
    assert detail == routes._GENERIC_THROTTLED
    # Readable prose, not an exception dump.
    assert "Exception" not in detail and "boto" not in detail.lower()


def test_lockout_is_per_email(client):
    for _ in range(5):
        _post(client, email="a@example.com")
    assert _post(client, email="a@example.com").status_code == 429
    # A different email from the same IP is not locked out.
    assert _post(client, email="b@example.com").status_code == 401


def test_success_resets_failure_streak(client):
    for _ in range(4):
        assert _post(client).status_code == 401
    _STATE["exc"] = None  # correct password now
    assert _post(client, password="right").status_code == 200  # nosec B106 - offline test fixture/placeholder credential, not a live secret
    # Streak was reset: four more failures do NOT lock out; the fifth+1 does.
    _STATE["exc"] = AuthenticationError(_RAW_BOTO_TEXT)
    for _ in range(4):
        assert _post(client).status_code == 401
    assert _post(client).status_code == 401  # 5th failure of the NEW streak
    assert _post(client).status_code == 429


def test_lockout_expires_after_window(client, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(routes, "time",
                        types.SimpleNamespace(monotonic=lambda: fake_now[0]))
    for _ in range(5):
        assert _post(client).status_code == 401
    assert _post(client).status_code == 429
    # Advance past the window — attempts are allowed again (401, not 429).
    fake_now[0] += routes._THROTTLE_WINDOW_S + 1
    assert _post(client).status_code == 401


def test_dev_mode_without_cognito_bypasses_throttle(client, monkeypatch):
    monkeypatch.setattr(routes, "get_config",
                        lambda: types.SimpleNamespace(has_cognito=False))
    r = _post(client)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
