# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import hashlib
import hmac
import time
from base64 import b64encode
from dataclasses import dataclass, field
from typing import Optional

import boto3
import httpx
import jwt
from jwt import ExpiredSignatureError, PyJWTError

from agents.config import get_config

# JWKS cache: {url: (keys, fetched_at)}
_jwks_cache: dict[str, tuple[list, float]] = {}
_JWKS_TTL = 3600


class AuthenticationError(Exception):
    """A failed Cognito call. ``code`` and ``request_id`` are the AWS error
    code and request id when the provider gave them (None otherwise): safe,
    structured fields for logs, unlike the free-form message text.
    ``throttled`` is True for Cognito's own rate-limit and lockout replies."""

    def __init__(self, message: str = "", *, code: Optional[str] = None,
                 request_id: Optional[str] = None, throttled: bool = False):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.throttled = throttled


# Cognito error codes that mean "slow down", and the lockout reply Cognito
# sends as a NotAuthorizedException with this message.
_THROTTLE_CODES = frozenset({
    "TooManyRequestsException", "TooManyFailedAttemptsException",
    "LimitExceededException",
})
_LOCKOUT_MESSAGE = "Password attempts exceeded"


def _auth_error(exc: Exception) -> AuthenticationError:
    """Wrap a boto3/botocore failure, keeping its AWS error code and request
    id (a botocore ClientError's ``response``) as structured fields, and
    whether it was a throttle or lockout.

    The message is fixed text: the provider's own message can carry the
    submitted address, and the original exception stays on ``__cause__``
    through the caller's ``raise ... from``."""
    response = getattr(exc, "response", None)
    response = response if isinstance(response, dict) else {}
    error = response.get("Error") or {}
    metadata = response.get("ResponseMetadata") or {}
    code = error.get("Code")
    throttled = code in _THROTTLE_CODES or _LOCKOUT_MESSAGE in str(error.get("Message") or "")
    return AuthenticationError(
        "Cognito authentication failed",
        code=code, request_id=metadata.get("RequestId"), throttled=throttled,
    )


def _signing_key(token: str, keys: list):
    """The public key from the JWKS whose ``kid`` matches the token header.

    A token without a ``kid``, or with one the pool does not publish, is
    rejected (``PyJWTError``) instead of being tried against every key.
    """
    kid = jwt.get_unverified_header(token).get("kid")
    for jwk in keys:
        if kid and jwk.get("kid") == kid:
            return jwt.PyJWK(jwk).key
    raise jwt.InvalidKeyError("no JWKS key matches the token kid")


def _epoch(value) -> Optional[float]:
    """A numeric claim as epoch seconds; None when it is missing or not a number."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class TokenExpiredError(AuthenticationError):
    pass


class TokenValidationError(AuthenticationError):
    pass


@dataclass
class TokenResponse:
    access_token: str
    id_token: str
    expires_in: int
    token_type: str
    refresh_token: Optional[str] = None


@dataclass
class UserInfo:
    user_id: str
    email: Optional[str] = None
    username: Optional[str] = None
    # Cognito group memberships from the access token's `cognito:groups` claim
    # (Task 1.3 / ADR-0033). Surfaced for app-layer defense-in-depth and the
    # downstream gateway/Cedar role checks. Empty when the claim is absent.
    groups: list[str] = field(default_factory=list)
    # The verified access token's ``exp`` and ``iat`` (epoch seconds), for the
    # middleware's near-expiry refresh. None when the claim is absent: callers
    # read None as "cannot tell", never as "expired".
    expires_at: Optional[float] = None
    issued_at: Optional[float] = None


class CognitoAuth:
    def __init__(
        self,
        user_pool_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        region: Optional[str] = None,
    ):
        cfg = get_config()
        self.user_pool_id = user_pool_id or cfg.cognito_user_pool_id
        self.client_id = client_id or cfg.cognito_client_id
        self.client_secret = client_secret or cfg.cognito_client_secret
        self.region = region or cfg.aws_region
        self._client = boto3.client("cognito-idp", region_name=self.region)

    def _get_secret_hash(self, username: str) -> str:
        msg = username + self.client_id
        sig = hmac.new(
            self.client_secret.encode(), msg.encode(), hashlib.sha256
        ).digest()
        return b64encode(sig).decode()

    def _auth_params(self, username: str, extra: dict) -> dict:
        params = {"USERNAME": username, **extra}
        if self.client_secret:
            params["SECRET_HASH"] = self._get_secret_hash(username)
        return params

    def authenticate(self, email: str, password: str) -> TokenResponse:
        try:
            resp = self._client.initiate_auth(
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters=self._auth_params(email, {"PASSWORD": password}),
                ClientId=self.client_id,
            )
        except self._client.exceptions.NotAuthorizedException as e:
            raise _auth_error(e) from e
        except Exception as e:
            raise _auth_error(e) from e
        r = resp["AuthenticationResult"]
        return TokenResponse(
            access_token=r["AccessToken"],
            id_token=r["IdToken"],
            refresh_token=r.get("RefreshToken"),
            expires_in=r["ExpiresIn"],
            token_type=r["TokenType"],
        )

    def refresh_tokens(self, refresh_token: str, username: str) -> TokenResponse:
        try:
            resp = self._client.initiate_auth(
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters=self._auth_params(username, {"REFRESH_TOKEN": refresh_token}),
                ClientId=self.client_id,
            )
        except Exception as e:
            raise _auth_error(e) from e
        r = resp["AuthenticationResult"]
        return TokenResponse(
            access_token=r["AccessToken"],
            id_token=r["IdToken"],
            expires_in=r["ExpiresIn"],
            token_type=r["TokenType"],
        )

    def _get_jwks(self) -> list:
        url = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}/.well-known/jwks.json"
        cached = _jwks_cache.get(url)
        if cached and (time.time() - cached[1]) < _JWKS_TTL:
            return cached[0]
        keys = httpx.get(url, timeout=5).json()["keys"]
        _jwks_cache[url] = (keys, time.time())
        return keys

    def _verified_access_claims(self, token: str, verify_exp: bool) -> tuple[list, dict]:
        """The JWKS and the claims of a verified Cognito access token."""
        try:
            keys = self._get_jwks()
            issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"
            claims = jwt.decode(
                token,
                _signing_key(token, keys),
                algorithms=["RS256"],
                issuer=issuer,
                # iat is not a validity check: Cognito and this host can
                # disagree by seconds (python-jose never checked it).
                options={
                    "verify_exp": verify_exp,
                    "verify_aud": False,
                    "verify_iat": False,
                },
            )
            # Cognito access tokens carry client_id (not aud) and token_use="access".
            if claims.get("token_use") != "access":
                raise TokenValidationError("Not an access token")
            if claims.get("client_id") != self.client_id:
                raise TokenValidationError("Token client_id mismatch")
        except ExpiredSignatureError as e:
            raise TokenExpiredError("Token expired") from e
        except PyJWTError as e:
            raise TokenValidationError(str(e)) from e
        return keys, claims

    def validate_token(
        self, token: str, verify_exp: bool = True, id_token: Optional[str] = None
    ) -> UserInfo:
        keys, claims = self._verified_access_claims(token, verify_exp)

        user_id = claims["sub"]
        email = claims.get("email")

        if id_token and not email:
            try:
                id_claims = jwt.decode(
                    id_token,
                    _signing_key(id_token, keys),
                    algorithms=["RS256"],
                    options={"verify_exp": False},
                )
                email = id_claims.get("email")
            except PyJWTError:
                pass

        username = claims.get("username") or claims.get("cognito:username")

        # Cognito groups from the access token (Task 1.3). The `cognito:groups`
        # claim is a list of group names; absent → empty list. Additive: callers
        # that ignore `groups` are unaffected, and is_admin semantics are
        # unchanged (the middleware still derives admin via get_user_groups).
        groups = claims.get("cognito:groups") or []

        # Last resort: look up email from Cognito user pool
        if not email and (username or user_id):
            try:
                resp = self._client.admin_get_user(
                    UserPoolId=self.user_pool_id,
                    Username=username or user_id,
                )
                for attr in resp.get("UserAttributes", []):
                    if attr["Name"] == "email":
                        email = attr["Value"]
                        break
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass

        return UserInfo(user_id=user_id, email=email, username=username, groups=groups,
                        expires_at=_epoch(claims.get("exp")),
                        issued_at=_epoch(claims.get("iat")))

    def get_user_groups(self, username: str) -> list[str]:
        resp = self._client.admin_list_groups_for_user(
            Username=username, UserPoolId=self.user_pool_id
        )
        return [g["GroupName"] for g in resp.get("Groups", [])]

    @staticmethod
    def is_admin(groups: list[str]) -> bool:
        return "Admin" in groups

    @staticmethod
    def is_integrator(groups: list[str]) -> bool:
        """True if the group list grants the onboarding commit tier.

        Onboarding (the commit-tier capability) is allowed for the ``integrator``
        role or ``Admin`` (ADR-0033 decision 2/5). Mirrors :meth:`is_admin`; used
        by the AuthMiddleware to set ``request.state.is_integrator`` for the
        app-layer onboarding gate (the active control while no gateway Cedar
        policy is attached).
        """
        return "integrator" in groups or "Admin" in groups
