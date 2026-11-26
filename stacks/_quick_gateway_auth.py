# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Quick-gateway inbound auth — the ONE home (ADR-0079).

The dedicated Quick gateway (``adt-quick-gw``, ``AgentCoreStack.
_build_quick_gateway``) authenticates inbound calls in exactly ONE of two
modes per deployment:

* **Cognito service auth** (default): ``CUSTOM_JWT`` against the deployment's
  ``adt-app`` pool, ``allowed_clients`` = the login + service clients plus
  the dedicated Quick connector client ``adt-quick-service``
  (:func:`create_quick_service_client`), which no other gateway lists.
* **Delegated user tokens** from an external OIDC identity provider
  (``QuickOidcConfig`` set): ``CUSTOM_JWT`` against the provider's discovery
  URL with ``allowed_audience`` = the gateway URL (incl. ``/mcp``) and
  ``allowed_clients`` OMITTED (delegated tokens may carry the client id only
  under an actor claim). ONE REQUEST interceptor Lambda mints the ADR-0049
  ``_adt_identity`` claim for the token's subject.

Every piece of that plumbing — the context resolver, the authorizer property,
the interceptor Lambda + its IAM, and the interceptor configuration — lives
HERE. ``allowed_audience=`` and ``interceptor_configurations=`` appear in no
other stack file (tripwire: ``tests/test_quick_gateway_delegated_oidc.py``).
The authorizer TYPE stays ``CUSTOM_JWT`` in both modes, so switching modes is
an in-place gateway update (the service rejects only type changes); the MCP
protocol pin is untouched.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from aws_cdk import (
    Duration,
    aws_bedrockagentcore as agentcore,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

from stacks._staging import stage_tree

_DISCOVERY_SUFFIX = "/.well-known/openid-configuration"

_QUICK_OIDC_CONTEXT_HELP = (
    "Pass BOTH deploy.sh --quick-oidc-discovery "
    "https://<issuer>/.well-known/openid-configuration and "
    "--quick-oidc-audience <QuickGatewayUrl> (or -c quick_oidc_discovery_url=... "
    "-c quick_oidc_audience=...), or neither. To switch delegated tokens off, "
    "delete the SSM parameters /adt/quick-oidc-discovery-url and "
    "/adt/quick-oidc-audience and redeploy (see ADR-0079)."
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INTERCEPTOR_SRC = os.path.join(_REPO_ROOT, "lambda-quick-interceptor")
_IDENTITY_CLAIM_SRC = os.path.join(
    _REPO_ROOT, "agentic_digital_thread", "agents", "auth", "identity_claim.py")
#: MUST equal agents/auth/identity_claim.py::IDENTITY_CLAIM_SECRET_NAME (pinned
#: by test — the stacks package cannot import the app package).
IDENTITY_CLAIM_SECRET_NAME = "adt/identity-claim-key"  # nosec B105 - secret NAME, not a credential
#: App-task env flag set to "1" exactly when the task role is granted read on
#: that key. MUST equal agents/auth/delegated_identity.py::
#: IDENTITY_CLAIM_KEY_READABLE_ENV (pinned by test); the app reads the key
#: only when it is set.
IDENTITY_CLAIM_KEY_READABLE_ENV = "IDENTITY_CLAIM_KEY_READABLE"
#: The Cognito app client AWS Quick's MCP connector signs in with (Service
#: authentication). It is Quick's own client, so the secret Quick holds works
#: on adt-quick-gw only; adt-query-gw and adt-onboard-gw never list it.
QUICK_CLIENT_NAME = "adt-quick-service"
#: Its own resource server and scope (``adt-quick/bridge``), apart from the
#: app's ``adt/*`` scopes.
QUICK_RESOURCE_SERVER = "adt-quick"
QUICK_SCOPE_NAME = "bridge"


@dataclass(frozen=True)
class QuickOidcConfig:
    """Validated external-OIDC settings for the Quick gateway authorizer."""

    discovery_url: str
    audience: str


def parse_audience(raw: Optional[str]) -> str:
    """ONE audience: the Quick gateway URL. A list (comma or whitespace
    separated) is REJECTED — every extra audience is another resource whose
    tokens this gateway would then accept."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "," in value or any(ch.isspace() for ch in value):
        raise ValueError(
            "quick_oidc_audience must be exactly ONE value, the Quick gateway URL "
            "including /mcp; lists are not accepted (each extra audience widens "
            "the set of tokens this gateway trusts).")
    return value


def _validate_discovery_url(url: str) -> None:
    if not url.startswith("https://") or not url.endswith(_DISCOVERY_SUFFIX) \
            or len(url) <= len("https://") + len(_DISCOVERY_SUFFIX):
        raise ValueError(
            f"quick_oidc_discovery_url {url!r} must be an https:// OpenID Connect "
            f"discovery URL ending in {_DISCOVERY_SUFFIX} "
            "(e.g. https://idp.example.com/.well-known/openid-configuration).")


def _validate_audience(audience: str) -> None:
    if not audience.startswith("https://") or not audience.endswith("/mcp"):
        raise ValueError(
            f"quick_oidc_audience {audience!r} must be the Quick gateway URL "
            "INCLUDING /mcp (the QuickGatewayUrl output, e.g. "
            "https://<quick-gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp).")


def resolve_quick_oidc(
    discovery_url: Optional[str],
    audience: Optional[str],
    *,
    quick_api_enabled: bool,
) -> Optional[QuickOidcConfig]:
    """Validate the context pair into a config, or ``None`` when unset.

    Pure (no CDK resources, no AWS calls). Fails LOUD on a partial pair, a
    malformed discovery URL or audience, and on a configured pair while
    ``quick_api_enabled`` is off — ``--quick-api`` is not sticky, and silently
    dropping the gateway an enterprise assistant depends on is the failure
    this prevents.

    :raises ValueError: with a message naming the deploy.sh flags / the fix.
    """
    url = (discovery_url or "").strip()
    aud = parse_audience(audience)
    if not url and not aud:
        return None
    if not url or not aud:
        missing = "quick_oidc_discovery_url" if not url else "quick_oidc_audience"
        raise ValueError(
            f"Incomplete Quick-gateway OIDC configuration: missing {missing}. "
            + _QUICK_OIDC_CONTEXT_HELP)
    if not quick_api_enabled:
        raise ValueError(
            "Quick-gateway OIDC settings are configured but the Quick integration "
            "is off: pass --quick-api (quick_api_enabled=true) or --quick-connector "
            "(quick_connector_enabled=true) on this deploy, or "
            "delete the SSM parameters /adt/quick-oidc-discovery-url and "
            "/adt/quick-oidc-audience to switch delegated user tokens off.")
    _validate_discovery_url(url)
    _validate_audience(aud)
    return QuickOidcConfig(discovery_url=url, audience=aud)


def create_quick_service_client(stack) -> cognito.UserPoolClient:
    """The confidential client for AWS Quick's connector, on the adt-app pool.

    client_credentials only, with its own ``adt-quick/bridge`` scope and no
    user sign-in flow (only refresh is left, which a client_credentials
    client never uses). Only the Quick gateway's Cognito-mode authorizer
    lists it, so whoever holds its secret (every workshop attendee, any Quick
    admin) reaches the three bridge tools and nothing else. The app's own
    ``adt-app-service-v2`` client stays the background-turn credential on
    adt-query-gw and adt-onboard-gw. Built whenever the Quick gateway is, in
    both modes: under delegated user tokens no gateway lists it, so its tokens
    are refused everywhere.
    """
    scope = cognito.ResourceServerScope(
        scope_name=QUICK_SCOPE_NAME,
        scope_description="Call the AWS Quick query bridge on adt-quick-gw")
    server = stack.adt_user_pool.add_resource_server(
        "QuickResourceServer", identifier=QUICK_RESOURCE_SERVER, scopes=[scope])
    return stack.adt_user_pool.add_client(
        "AdtQuickServiceClient",
        user_pool_client_name=QUICK_CLIENT_NAME,
        generate_secret=True,
        # Any explicit flow set leaves ALLOW_REFRESH_TOKEN_AUTH alone; without
        # one Cognito would also allow SRP and custom user sign-in.
        auth_flows=cognito.AuthFlow(user_srp=False),
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(client_credentials=True),
            scopes=[cognito.OAuthScope.resource_server(server, scope)],
        ),
    )


def quick_gateway_authorizer(stack, cfg: Optional[QuickOidcConfig]):
    """The Quick gateway's ``CustomJWTAuthorizerConfigurationProperty``.

    ``cfg`` None ⇒ the Cognito shape (discovery + both adt-app clients + the
    dedicated Quick client, no audience). ``cfg`` set ⇒ provider discovery +
    ``allowed_audience`` and NO ``allowed_clients``.
    """
    if cfg is None:
        return agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
            discovery_url=stack.adt_app_discovery_url,
            allowed_clients=[
                stack.adt_app_client.user_pool_client_id,
                stack.adt_service_client.user_pool_client_id,
                stack.quick_service_client.user_pool_client_id,
            ],
        )
    return agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
        discovery_url=cfg.discovery_url,
        allowed_audience=[cfg.audience],
    )


def create_quick_request_interceptor(scope: Construct, gateway_role: iam.Role) -> lambda_.Function:
    """The REQUEST interceptor Lambda (+ its IAM) for the delegated-token mode.

    The asset is STAGED under ``cdk.out`` (``cdk synth`` never writes into the
    checkout): the handler plus a COPY of the ONE claim implementation. The
    function reads ONLY the identity-claim signing key (by name); the gateway
    role may invoke ONLY this function.
    """
    asset_dir = stage_tree(scope, "quick-interceptor", _INTERCEPTOR_SRC,
                           extra_files={"identity_claim.py": _IDENTITY_CLAIM_SRC})
    fn = lambda_.Function(
        scope, "QuickGatewayRequestInterceptor",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(asset_dir),
        timeout=Duration.seconds(10),
        memory_size=256,
        description="ADR-0079: mints the ADR-0049 identity claim for delegated Quick-gateway calls",
    )
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["secretsmanager:GetSecretValue"],
        resources=[identity_claim_key_arn_pattern(scope)],
    ))
    gateway_role.add_to_policy(iam.PolicyStatement(
        actions=["lambda:InvokeFunction"],
        resources=[fn.function_arn],
    ))
    return fn


def identity_claim_key_arn_pattern(scope: Construct) -> str:
    """ARN pattern of the by-NAME signing key (Secrets Manager adds a suffix)."""
    from aws_cdk import Stack
    stack = Stack.of(scope)
    return (f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:"
            f"{IDENTITY_CLAIM_SECRET_NAME}-*")


def quick_gateway_auth_props(stack, cfg: Optional[QuickOidcConfig], gateway_role: iam.Role) -> dict:
    """The Quick ``CfnGateway`` auth keyword arguments for this deploy.

    ALWAYS ``authorizer_configuration``; ``interceptor_configurations`` (and
    the interceptor Lambda) ONLY in delegated-token mode, so the Cognito-mode
    template carries no new key at all (byte-identical).
    """
    props = {"authorizer_configuration": agentcore.CfnGateway.AuthorizerConfigurationProperty(
        custom_jwt_authorizer=quick_gateway_authorizer(stack, cfg))}
    if cfg is not None:
        fn = create_quick_request_interceptor(stack, gateway_role)
        props["interceptor_configurations"] = quick_gateway_interceptors(fn)
    return props


def quick_gateway_interceptors(fn: Optional[lambda_.IFunction]):
    """``interceptor_configurations`` for the Quick gateway (None ⇒ none)."""
    if fn is None:
        return None
    return [agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
        interception_points=["REQUEST"],
        interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
            lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                arn=fn.function_arn),
        ),
        input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
            pass_request_headers=True),
    )]
