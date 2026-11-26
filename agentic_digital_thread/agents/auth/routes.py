# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from agents.auth import edge_session
from agents.auth.cognito import CognitoAuth, AuthenticationError, UserInfo
from agents.auth.middleware import (
    SESSION_COOKIE_NAME, _COOKIE_OPTS, _encode_cookie, get_current_user,
)
from agents.config import get_config

router = APIRouter(prefix="/api/auth")

logger = logging.getLogger(__name__)

_REFRESH_COOKIE = "adt_session_refresh"
# _COOKIE_OPTS and _encode_cookie live in agents/auth/middleware.py (the ONE
# home) so the middleware's refreshed-session write-back shares the exact
# encoding + attributes — routes.py imports them above; never re-define here.

# --- Login failure throttle (best-effort, in-memory) -------------------------
# After _THROTTLE_MAX_FAILURES consecutive failures for the same
# (client-ip, email) pair, login is rejected with 429 + Retry-After until
# _THROTTLE_WINDOW_S seconds have passed since the last failure. A successful
# login resets the counter.
#
# LIMITATION: this is a module-level dict, so the state is per-process. The
# demo deployment runs a single ECS task, where this is sufficient; a
# multi-task deployment would need shared state (e.g. ElastiCache/DynamoDB)
# for the throttle to be effective across tasks. The built-in Cognito lockout
# ("Password attempts exceeded") remains the authoritative backstop.
_THROTTLE_MAX_FAILURES = 5
_THROTTLE_WINDOW_S = 60.0
# Size cap: entries expire naturally after the window, but a spray of unique
# (ip, email) pairs would otherwise grow the dict unboundedly. At the cap,
# expired entries are swept first; if the dict is still full, the oldest
# entry is evicted (best-effort — Cognito lockout remains the backstop).
_THROTTLE_MAX_ENTRIES = 10_000
# {(client_ip, email_lower): (consecutive_failures, last_failure_monotonic)}
_login_failures: dict[tuple[str, str], tuple[int, float]] = {}


def _sweep_throttle(now: float) -> None:
    """Drop expired entries; if still at cap, evict the oldest."""
    if len(_login_failures) < _THROTTLE_MAX_ENTRIES:
        return
    expired = [k for k, (_, ts) in _login_failures.items() if now - ts >= _THROTTLE_WINDOW_S]
    for k in expired:
        _login_failures.pop(k, None)
    while len(_login_failures) >= _THROTTLE_MAX_ENTRIES:
        oldest = min(_login_failures, key=lambda k: _login_failures[k][1])
        _login_failures.pop(oldest, None)

# Generic client-facing messages (the frontend renders `detail` verbatim —
# keep them human-readable). Raw boto3/Cognito text is logged server-side only.
_GENERIC_AUTH_FAILURE = "Incorrect username or password."
_GENERIC_THROTTLED = "Too many sign-in attempts. Please wait a minute and try again."


def _client_ip(request: Request | None) -> str:
    if request is not None and request.client:
        return request.client.host or "unknown"
    return "unknown"


# Key for log fingerprints, random per process and never written anywhere.
_FINGERPRINT_KEY = secrets.token_bytes(32)


def _email_fingerprint(email: str) -> str:
    """Short stand-in for an email address in logs: an HMAC-SHA-256 of the
    normalised address under a process-random key, truncated to 12 hex chars.

    Repeated failures for one address match within this process, which is all
    the in-memory throttle needs (its state is per-process too). Because the
    key is secret and random, hashing a list of likely addresses does not
    recover one from the log, as it would with a plain SHA-256. Fingerprints
    do not line up across processes or restarts; that would need a
    secret-managed key."""
    normalized = (email or "").strip().lower()
    digest = hmac.new(_FINGERPRINT_KEY, normalized.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:12]


def _log_login_failure(email: str, client_ip: str, exc: AuthenticationError) -> None:
    """Log a failed sign-in with safe structured fields only: the address
    fingerprint, client IP, exception class, AWS error code and request id.
    The provider's message text is never logged: it is free-form and could
    carry the address."""
    logger.warning(
        "Login failed for user %s from %s: %s code=%s request_id=%s",
        _email_fingerprint(email), client_ip, type(exc.__cause__ or exc).__name__,
        getattr(exc, "code", None), getattr(exc, "request_id", None),
    )


def _throttle_key(request: Request | None, email: str) -> tuple[str, str]:
    return (_client_ip(request), (email or "").strip().lower())


def _check_throttle(key: tuple[str, str]) -> None:
    """Raise 429 (+ Retry-After) if the key is locked out; expire stale entries."""
    entry = _login_failures.get(key)
    if not entry:
        return
    failures, last_ts = entry
    elapsed = time.monotonic() - last_ts
    if elapsed >= _THROTTLE_WINDOW_S:
        # Window expired — forget the streak entirely.
        _login_failures.pop(key, None)
        return
    if failures >= _THROTTLE_MAX_FAILURES:
        retry_after = max(1, math.ceil(_THROTTLE_WINDOW_S - elapsed))
        raise HTTPException(
            status_code=429,
            detail=_GENERIC_THROTTLED,
            headers={"Retry-After": str(retry_after)},
        )


def _record_failure(key: tuple[str, str]) -> None:
    failures, last_ts = _login_failures.get(key, (0, 0.0))
    now = time.monotonic()
    _sweep_throttle(now)
    if now - last_ts >= _THROTTLE_WINDOW_S:
        failures = 0  # stale streak — start over
    _login_failures[key] = (failures + 1, now)


def _record_success(key: tuple[str, str]) -> None:
    _login_failures.pop(key, None)


def _is_cognito_throttle(exc: Exception) -> bool:
    """True for rate-limit/lockout responses from Cognito itself (kept distinct from
    a plain bad-credentials failure, but never echoed to the client raw). Read
    from the structured flag cognito._auth_error sets, since the message text
    is fixed."""
    return bool(getattr(exc, "throttled", False))


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    cfg = get_config()

    if not cfg.has_cognito:
        response.set_cookie(SESSION_COOKIE_NAME, _encode_cookie({"username": "dev-user"}), **_COOKIE_OPTS)
        return {"status": "ok", "email": "dev@localhost", "is_admin": True}

    throttle_key = _throttle_key(request, body.email)
    _check_throttle(throttle_key)

    auth = CognitoAuth()
    try:
        tokens = auth.authenticate(body.email, body.password)
    except AuthenticationError as e:
        # The client gets a GENERIC message so authentication errors do not
        # leak Cognito/boto3 internals (user enumeration, pool config, ids).
        _log_login_failure(body.email, throttle_key[0], e)
        _record_failure(throttle_key)
        if _is_cognito_throttle(e):
            raise HTTPException(
                status_code=429,
                detail=_GENERIC_THROTTLED,
                headers={"Retry-After": str(int(_THROTTLE_WINDOW_S))},
            )
        raise HTTPException(status_code=401, detail=_GENERIC_AUTH_FAILURE)
    _record_success(throttle_key)

    user = auth.validate_token(tokens.access_token, id_token=tokens.id_token)
    try:
        groups = auth.get_user_groups(user.username or user.email or user.user_id)
    except Exception:
        groups = []
    is_admin = auth.is_admin(groups)

    session = {
        "access_token": tokens.access_token,
        "id_token": tokens.id_token,
        "username": body.email,
    }
    response.set_cookie(SESSION_COOKIE_NAME, _encode_cookie(session), **_COOKIE_OPTS)
    if tokens.refresh_token:
        response.set_cookie(
            _REFRESH_COOKIE,
            _encode_cookie({"refresh_token": tokens.refresh_token}),
            **_COOKIE_OPTS,
        )

    return {"status": "ok", "email": user.email or body.email, "is_admin": is_admin}


@router.post("/logout")
async def logout(request: Request, response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(_REFRESH_COOKIE)
    if not edge_session.edge_auth_enabled():
        return {"status": "ok"}
    # Authorization@Edge deploys: send the frontend to the /signout edge
    # behavior, which performs the authoritative Cognito hosted-UI sign-out.
    # The SAR edge cookies are deliberately LEFT in place: the SAR SignOut
    # handler needs the id token to call Cognito /logout and then expires
    # every edge cookie at its own path (the refresh token is scoped to
    # /refreshauth and never reaches this route). A /signout with no cookies
    # is redirected into sign-in at the edge (ADR-0070), not signed out.
    return {"status": "ok", "redirect": "/signout"}


@router.get("/me")
async def me(request: Request):
    user: UserInfo = get_current_user(request)
    # GENERIC UI capability: the connection types this deployment lets take a
    # named Secrets Manager secret as their credential source (empty when the
    # allowlist is), and whether there is any. The Connect New System form
    # shows the "Secrets Manager secret" credential choice for a listed type
    # only. Deploy-config-driven, no workshop/seat wording; the server still
    # enforces the allowlist and the type list on write.
    from ..connection_pinning import source_types
    enabled_types = list(source_types())
    return {
        "user_id": user.user_id,
        "email": user.email,
        "username": user.username,
        "is_admin": getattr(request.state, "is_admin", False),
        "secret_arn_source_enabled": bool(enabled_types),
        "secret_arn_source_types": enabled_types,
    }
