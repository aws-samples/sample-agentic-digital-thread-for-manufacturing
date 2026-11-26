# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How the query-bridge runtime gets the x-quick-auth token.

The token admits the app's async ask endpoints without a browser session. In
the bridge runtime's environment it is plaintext to any principal allowed
``bedrock-agentcore:GetAgentRuntime``, so the stacks give the app task a
REFERENCE instead (``QUICK_GATEWAY_TOKEN_REF``). The app passes that
reference to the bridge runtime (``runtime_provision_helpers._query_bridge_env``)
and the bridge reads the value with its own IAM role on first use.

Reference format: one full ARN, and nothing else (the ONE rule is
``runtime_env_contract.parse_quick_gateway_token_ref``).

* A Secrets Manager secret ARN: the Quick connector path's generated secret
  (``stacks/_quick_connector.py``), read with GetSecretValue. This is the
  only reference the stacks wire.
* An SSM parameter ARN, read with GetParameter and decryption. Accepted by
  the format rule; the ``--quick-api`` path does not wire it, because its
  token is already readable in that stack's template.

The client Region is the ARN's Region. With no reference set, the bridge
uses the plaintext ``QUICK_GATEWAY_TOKEN`` env exactly as before, so a bridge
runtime provisioned by an older app keeps working until it is reprovisioned.

Import-light: stdlib and the stdlib-only contract at module level; boto3 is
imported only when a reference is read. Neither the token nor any part of it
is ever logged or put in an error.
"""
from __future__ import annotations

import logging
import os
import threading

from .runtime_env_contract import QUICK_GATEWAY_TOKEN_REF as TOKEN_REF_ENV
from .runtime_env_contract import QuickGatewayTokenRef as TokenRef
from .runtime_env_contract import parse_quick_gateway_token_ref as parse_ref

logger = logging.getLogger(__name__)

__all__ = ["TOKEN_ENV", "TOKEN_REF_ENV", "PLACEHOLDER", "TokenRef",
           "TokenResolutionError", "parse_ref", "normalize_token",
           "resolve_gateway_token", "forget_cached_token"]

#: Plaintext token env (the app task always has it; old bridge runtimes too).
TOKEN_ENV = "QUICK_GATEWAY_TOKEN"  # nosec B105 - env var NAME, not a credential
#: deploy.sh seeds the SSM parameter with this before the real token exists.
PLACEHOLDER = "placeholder"  # nosec B105 - seed sentinel, not a credential

#: Reference service -> the store name used in log lines and errors.
_STORE_NAME = {"secretsmanager": "Secrets Manager", "ssm": "SSM Parameter Store"}


class TokenResolutionError(RuntimeError):
    """The reference could not be read. The message names the store and the
    AWS error code only; it never carries the token."""


def normalize_token(value) -> str:
    """Strip the value; the deploy.sh seed sentinel counts as no token."""
    token = value.strip() if isinstance(value, str) else ""
    return "" if token.lower() == PLACEHOLDER else token


_CACHE: dict[str, str] = {}
_LOCK = threading.Lock()


def resolve_gateway_token(environ=None, *, client_factory=None) -> str:
    """The token the bridge sends as x-quick-auth ("" means send none).

    * No reference: the plaintext ``QUICK_GATEWAY_TOKEN`` env, returned
      unchanged (the bridge's behavior before references existed).
    * A reference: the stored value, stripped, read once and cached for the
      process. An empty value or the seed sentinel is returned as "" and NOT
      cached, so the real token is picked up once it is written.
    * A reference that does not parse, or a failed read: raises
      :class:`TokenResolutionError`. The bridge tools turn that into their
      structured error reply.

    ``client_factory(service, region)`` builds the AWS client (tests inject a
    stub); the default is a boto3 client with short timeouts."""
    env = os.environ if environ is None else environ
    raw = env.get(TOKEN_REF_ENV) or ""
    if not raw.strip():
        return env.get(TOKEN_ENV, "")
    ref = parse_ref(raw)
    if ref is None:
        raise TokenResolutionError(
            f"{TOKEN_REF_ENV} is not a Secrets Manager secret ARN or an SSM "
            "parameter ARN")
    cached = _CACHE.get(ref.arn)
    if cached:
        return cached
    with _LOCK:
        cached = _CACHE.get(ref.arn)
        if cached:
            return cached
        token = normalize_token(_read(ref, client_factory or _default_client))
        if token:
            _CACHE[ref.arn] = token
        return token


def forget_cached_token() -> None:
    """Drop the cached token so the next call reads the store again (the app
    answered 401, so the stored token may have been replaced)."""
    with _LOCK:
        _CACHE.clear()


def _default_client(service: str, region: str):
    import boto3
    from botocore.config import Config

    return boto3.client(service, region_name=region, config=Config(
        connect_timeout=5, read_timeout=10,
        retries={"max_attempts": 3, "mode": "standard"}))


def _read(ref: TokenRef, client_factory) -> str:
    """Read the stored value. Any failure becomes a TokenResolutionError that
    names the store and the error code; the original exception is not
    chained."""
    store = _STORE_NAME[ref.service]
    try:
        client = client_factory(ref.service, ref.region)
        if ref.service == "secretsmanager":
            value = client.get_secret_value(SecretId=ref.arn).get("SecretString")
        else:
            value = client.get_parameter(
                Name=ref.arn, WithDecryption=True)["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 - every failure maps to one safe error
        code = _error_code(exc)
        logger.warning("query-bridge: could not read %s (%s) for the x-quick-auth "
                       "token", store, code)
        raise TokenResolutionError(
            f"could not read the x-quick-auth token from {store} ({code})") from None
    if not isinstance(value, str):
        logger.warning("query-bridge: %s does not hold a string x-quick-auth token",
                       store)
        raise TokenResolutionError(
            f"the x-quick-auth token in {store} is not a string value")
    return value


def _error_code(exc: Exception) -> str:
    """The AWS error code when there is one, else the exception class name.
    Never the exception message: it can carry ARNs and request detail."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__
