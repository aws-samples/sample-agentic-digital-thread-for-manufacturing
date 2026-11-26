# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from agents.auth import edge_session
from agents.auth.cognito import (
    CognitoAuth, TokenExpiredError, TokenValidationError, UserInfo,
)
from agents.config import dev_no_auth_allowed, get_config
from agents.turn_limits import BEARER_REFRESH_LEEWAY_SECONDS, refresh_window

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "adt_session"
_REFRESH_COOKIE_NAME = "adt_session_refresh"

#: Env override for the proactive refresh leeway (seconds).
REFRESH_LEEWAY_ENV = "AUTH_REFRESH_LEEWAY_SECONDS"


def _refresh_leeway(environ) -> int:
    """The proactive refresh leeway: ``AUTH_REFRESH_LEEWAY_SECONDS`` when set,
    else ``turn_limits.BEARER_REFRESH_LEEWAY_SECONDS`` (the turn ceiling plus
    a margin)."""
    raw = environ.get(REFRESH_LEEWAY_ENV)
    return int(raw) if raw else BEARER_REFRESH_LEEWAY_SECONDS


# Proactive refresh leeway (seconds): a validated access token with less than
# this left is refreshed BEFORE the request proceeds, because the token is
# forwarded as the AgentCore gateway bearer (ADR-0049) and is used for EVERY
# gateway call of the turn, so it must outlive the whole turn, not just its
# start. Derived from the app's turn ceiling plus a margin
# (agents/turn_limits.py), capped at half the token's own lifetime so a
# just-issued token is never refreshed again (refresh_window). Observed
# 2026-09-24: a ~30 s "Query failed." window every 60 min at the token
# boundary; 2026-10-14: a turn that started 2.5 min before expiry (outside the
# old 120 s leeway) lost its gateway session mid-turn. A near-expiry refresh
# failure falls through with the still-valid token; it never fails the
# request.
REFRESH_LEEWAY = _refresh_leeway(os.environ)

# ONE home for the session-cookie encoding + attributes. Relocated FROM
# agents/auth/routes.py (which re-imports them) so the middleware can write
# the refreshed session back to the response cookie without a circular
# import — routes.py already imports from this module. Never duplicate.
_COOKIE_OPTS = {"httponly": True, "samesite": "lax", "secure": True}


def _encode_cookie(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


class AuthMiddleware(BaseHTTPMiddleware):
    PUBLIC_ROUTES = {"/health", "/api/auth/login", "/api/auth/logout", "/api/branding"}
    PUBLIC_PREFIXES = ["/assets/", "/css/", "/js/", "/fonts/", "/login.html"]
    ADMIN_PREFIXES = ["/api/admin"]
    # Routes reachable with the Quick gateway shared secret (the API Gateway
    # kiosk path and the query-bridge Gateway target). Both callers are
    # themselves IAM/SigV4-authenticated and inject/send x-quick-auth; these
    # routes bypass the cookie/JWT check ONLY when that header matches
    # QUICK_GATEWAY_TOKEN (read at task start from the generated Secrets
    # Manager secret, ADTComputeStack.quick_gateway_token_secret).
    # Trust model: both legitimate senders reach the internal ALB DIRECTLY
    # (the API Gateway REST API over its VPC link, the bridge runtime at
    # ADT_APP_INTERNAL_URL); neither passes through the public CloudFront
    # distribution. That distribution forwards viewer headers to the ALB, so
    # it COULD carry the secret; two independent layers stop it: a CloudFront
    # Function deletes x-quick-auth from viewer requests
    # (stacks/_quick_header_strip.py), and this gate refuses the secret on any
    # request carrying X-Amz-Cf-Id (see CLOUDFRONT_REQUEST_ID_HEADER). Such a
    # request falls through to normal cookie/JWT auth, so a signed-in browser
    # is unaffected. The token grants ONLY these paths: /api/admin/* and every
    # other route keep normal auth. See docs/quick-integration.md.
    QUICK_GATEWAY_HEADER = "x-quick-auth"
    QUICK_GATEWAY_ROUTES = {"/api/ask/async"}         # any method (POST)
    QUICK_GATEWAY_PREFIXES = ["/api/ask/result/"]     # GET /api/ask/result/{job_id}
    # Read-only catalog for the query-bridge's get_prompts greeting — GET and
    # the EXACT path only (POST /api/prompts and /api/prompts/{id}/... keep
    # normal session auth).
    QUICK_GATEWAY_GET_ROUTES = {"/api/prompts"}
    # CloudFront adds this header to every request it forwards to an origin:
    # "CloudFront adds the header to the viewer request before forwarding the
    # request to your origin" (Amazon CloudFront Developer Guide, "HTTP
    # request headers and CloudFront behavior (custom and Amazon S3 origins)",
    # X-Amz-Cf-Id row:
    # https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/RequestAndResponseBehaviorCustomOrigin.html).
    # A request that came through CloudFront therefore always carries it,
    # whatever the viewer sent; the direct ALB callers above never do.
    CLOUDFRONT_REQUEST_ID_HEADER = "x-amz-cf-id"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        edge_redirect = self._edge_login_redirect(request, path)
        if edge_redirect is not None:
            return edge_redirect
        if path in self.PUBLIC_ROUTES or any(path.startswith(p) for p in self.PUBLIC_PREFIXES):
            return await call_next(request)
        if self._quick_gateway_request(request, path):
            rejected = self._admit_quick_gateway(request)
            if rejected is not None:
                return rejected
            return await call_next(request)
        cfg = get_config()
        if not cfg.has_cognito:
            return await self._development_request(request, call_next, cfg)
        auth, validated, session_refreshed = self._resolve_cookie_session(request)
        if validated is None:
            # No (valid) adt_session — accept the Authorization@Edge cookies
            # when the deploy runs the edge gate, so hosted-UI-authenticated
            # humans are not asked to log in a SECOND time.
            edge = self._validate_edge_session(request)
            if edge is not None:
                auth, validated = edge
        if validated is None:
            return self._unauthorized(request)
        user, session = validated
        user, is_admin, is_integrator = self._identity_roles(auth, user, session)
        if any(path.startswith(p) for p in self.ADMIN_PREFIXES) and not is_admin:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        request.state.user = user
        request.state.session = session
        request.state.is_admin = is_admin  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        request.state.is_integrator = is_integrator  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        response = await call_next(request)
        if session_refreshed:
            response.set_cookie(
                SESSION_COOKIE_NAME, _encode_cookie(session), **_COOKIE_OPTS)
        return response

    def _resolve_cookie_session(self, request: Request):
        """Validate the adt_session cookie (password path).

        Returns ``(auth, validated, refreshed)``: ``validated`` is
        ``(user, session)`` or None; ``refreshed`` is True when the session
        used for this request carries a DIFFERENT access token than the cookie
        (an expired OR near-expiry refresh happened), so dispatch writes it
        back and later requests stop re-calling Cognito.
        """
        cookie_session = self._decode_session(request.cookies.get(SESSION_COOKIE_NAME))
        if cookie_session is None:
            return None, None, False
        auth = CognitoAuth()
        validated = self._validate_session(
            auth, cookie_session, request.cookies.get(_REFRESH_COOKIE_NAME))
        if validated is None:
            return auth, None, False
        refreshed = validated[1].get("access_token") != cookie_session.get("access_token")
        return auth, validated, refreshed

    @staticmethod
    def _quick_gateway_request(request: Request, path: str) -> bool:
        """True when the Quick gateway shared secret admits this request.

        Constant-time comparison (hmac.compare_digest); the expected value
        comes from the QUICK_GATEWAY_TOKEN env (task-start SSM resolution,
        written by QuickApiGatewayStack) — never a literal. The deploy.sh
        "placeholder" seed sentinel is rejected: an environment whose token
        was never written must fail CLOSED, not accept a guessable string.
        Only the allowlisted async-query paths (any method) and the exact
        GET /api/prompts catalog read are eligible; everything else falls
        through to normal auth regardless of the header. A request that came
        through CloudFront (X-Amz-Cf-Id present) is never eligible either:
        no legitimate sender uses that path, so it falls through to normal
        cookie/JWT auth even when it carries the correct secret.
        """
        allowed = (
            path in AuthMiddleware.QUICK_GATEWAY_ROUTES
            or any(path.startswith(p) for p in AuthMiddleware.QUICK_GATEWAY_PREFIXES)
            or (request.method == "GET"
                and path in AuthMiddleware.QUICK_GATEWAY_GET_ROUTES)
        )
        if not allowed:
            return False
        provided = request.headers.get(AuthMiddleware.QUICK_GATEWAY_HEADER, "")
        if AuthMiddleware.CLOUDFRONT_REQUEST_ID_HEADER in request.headers:
            if provided:
                # Values are never logged; the event alone is the signal.
                logger.warning("quick_gateway outcome=refused reason=via_cloudfront")
            return False
        expected = (os.environ.get("QUICK_GATEWAY_TOKEN") or "").strip()
        if not expected or expected.lower() == "placeholder":  # nosec B105 - deploy.sh SSM seed sentinel, not a credential
            return False
        return bool(provided) and hmac.compare_digest(expected, provided)

    @staticmethod
    def _admit_quick_gateway(request: Request):
        """Set the identity of a request the Quick shared secret admitted.

        ADR-0079: the shared secret still ADMITS the request; the optional
        delegated-identity header only decides WHO it runs as. Absent ⇒ the
        anonymous ``quick-gateway`` identity (unchanged). Valid ⇒ the external
        identity provider's subject, a plain user (no admin/integrator role).
        Present but unverifiable ⇒ 401 with a distinct detail — NEVER the
        shared-identity fallback (fail closed). Returns the rejection
        response, or None when the request may proceed. Values never logged.
        """
        from agents.auth import delegated_identity as _delegated

        try:
            delegated = _delegated.resolve_delegated_identity(request.headers)
        except _delegated.DelegatedIdentityRejected:
            logger.warning("quick_delegated_identity outcome=rejected")
            return JSONResponse(
                {"detail": _delegated.DELEGATED_IDENTITY_REJECTED_DETAIL},
                status_code=401)
        if delegated is None:
            request.state.user = UserInfo(
                user_id="quick-gateway", email="quick-gateway@localhost",
                username="quick-gateway")
        else:
            request.state.user = UserInfo(
                user_id=delegated.sub, email="", username=delegated.sub)
        request.state.delegated = delegated is not None
        request.state.session = {}
        request.state.is_admin = False  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        request.state.is_integrator = False  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        return None

    @staticmethod
    def _validate_edge_session(request: Request):
        """Validate the SAR's Authorization@Edge cookies (edge auth deploys only).

        Inert unless EDGE_AUTH_CLIENT_ID is set (agents/auth/edge_session.py).
        The access token is validated with the EXISTING CognitoAuth.validate_token,
        pinned to the EDGE client id (validate_token rejects any other client_id
        claim — strictness is preserved, the edge client is simply the pin).
        Returns (auth, (user, session)) shaped exactly like the adt_session path,
        or None.
        """
        client_id = edge_session.edge_client_id()
        if not client_id:
            return None
        tokens = edge_session.parse_edge_cookies(request.cookies, client_id)
        if tokens is None:
            return None
        auth = CognitoAuth(client_id=client_id)
        try:
            user = auth.validate_token(tokens.access_token, id_token=tokens.id_token)
        except (TokenExpiredError, TokenValidationError, KeyError):
            # Expired edge tokens are refreshed at the edge (/refreshauth), not
            # here — an invalid/expired cookie simply falls through to 401/302.
            return None
        session = {
            "access_token": tokens.access_token,
            "id_token": tokens.id_token or "",
            "username": tokens.username,
        }
        return auth, (user, session)

    @staticmethod
    def _edge_login_redirect(request: Request, path: str):
        """GET /login.html with a valid edge session -> 302 to the app root.

        Edge-authenticated humans must never see the username/password form —
        the hosted UI already signed them in. Inert when edge auth is off or
        the cookies are absent/invalid (the form renders as before).
        """
        if request.method != "GET" or not path.startswith("/login.html"):
            return None
        if AuthMiddleware._validate_edge_session(request) is None:
            return None
        return RedirectResponse("/", status_code=302)

    async def _development_request(self, request, call_next, cfg):
        if not dev_no_auth_allowed():
            if os.getenv("DEV_NO_AUTH"):
                logger.error(
                    "DEV_NO_AUTH requested but refused: the complete local-only "
                    "authentication contract is not satisfied."
                )
            return self._unauthorized(request)
        logger.warning(
            "Local authentication bypass enabled. Do not use in a deployed environment."
        )
        request.state.user = UserInfo(
            user_id=cfg.dev_user_id, email="dev@localhost", username="dev-user"
        )
        request.state.session = {}
        request.state.is_admin = True  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        request.state.is_integrator = True  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        return await call_next(request)

    @staticmethod
    def _decode_session(raw):
        if not raw:
            return None
        try:
            return json.loads(base64.urlsafe_b64decode(raw))
        except Exception:
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None

    @staticmethod
    def _refresh_session(auth, session, refresh_raw):
        """Exchange the refresh cookie for fresh tokens; the ONE refresh path.

        Shared by the expired path and the proactive near-expiry path so the
        two can never drift. Raises on any failure — each caller applies its
        own policy (expired → 401; near-expiry → fall through with the
        still-valid token).
        """
        refresh = AuthMiddleware._decode_session(refresh_raw)
        tokens = auth.refresh_tokens(
            refresh["refresh_token"], session.get("username", "")
        )
        user = auth.validate_token(tokens.access_token, id_token=tokens.id_token)
        return user, {
            "access_token": tokens.access_token, "id_token": tokens.id_token,
            "username": session.get("username", ""),
        }

    @staticmethod
    def _validate_session(auth, session, refresh_raw):
        try:
            user = auth.validate_token(
                session["access_token"], id_token=session.get("id_token")
            )
        except TokenExpiredError:
            if not refresh_raw:
                return None
            try:
                return AuthMiddleware._refresh_session(auth, session, refresh_raw)
            except Exception:
                return None
        except (TokenValidationError, KeyError):
            return None
        # Proactive near-expiry refresh (ADR-0049 consequence): this access
        # token is forwarded as the AgentCore gateway bearer for the WHOLE
        # turn, so refresh while less than a turn is left (REFRESH_LEEWAY,
        # see refresh_window) and a refresh cookie is present; any failure
        # falls through with the still-valid token: the near-expiry path
        # must never fail the request.
        if refresh_raw:
            # exp/iat of the token validate_token just verified.
            exp, iat = user.expires_at, user.issued_at
            lifetime = exp - iat if exp is not None and iat is not None else None
            if exp is not None and exp - time.time() < refresh_window(REFRESH_LEEWAY, lifetime):
                try:
                    return AuthMiddleware._refresh_session(auth, session, refresh_raw)
                except Exception:
                    logger.warning(
                        "Proactive near-expiry token refresh failed; "
                        "proceeding with the still-valid token"
                    )
        return user, session

    @staticmethod
    def _identity_roles(auth, user, session):
        username = session.get("username", "")
        if not user.email and username and "@" in username:
            user = UserInfo(user_id=user.user_id, email=username, username=user.username)
        try:
            lookup = username or user.username or user.email or user.user_id
            groups = auth.get_user_groups(lookup)
            return user, auth.is_admin(groups), auth.is_integrator(groups)
        except Exception:
            return user, False, False

    @staticmethod
    def _unauthorized(request: Request):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return RedirectResponse("/login.html", status_code=302)


def get_current_user(request: Request) -> UserInfo:
    return request.state.user


def get_session_data(request: Request) -> dict:
    return request.state.session
