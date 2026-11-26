# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared Cognito Authorization@Edge infrastructure for CloudFront frontends.

This module is deliberately a function-based facade over the caller's existing
scope.  It must never introduce a nested Construct: the fixed construct paths
below map to live Cognito clients, Lambda functions, providers, custom
resources, web ACLs, and distributions in multiple accounts.

The AWS-published SAR application runs in auth-only mode.  Its nested-stack
Lambda versions are attached through CloudFront's L2 ``EdgeLambda`` API; the
post-distribution callback updater breaks the otherwise circular client → SAR
→ distribution → client dependency.  Lambda@Edge replication makes teardown
ordering significant, so distributions retain an explicit SAR dependency.
CSP remains report-only pending validation against each live SPA's assets and,
where enabled, WebSocket traffic.  The Cognito domain and client must both be
live before the first redirect; CloudFront must be removed before replicated
Lambda@Edge versions can be fully deleted.

Optional external OIDC identity provider (ADR-0072): every user pool that
carries an edge client may delegate sign-in to ONE customer-supplied OpenID Connect
provider.  :func:`resolve_external_oidc` validates the context trio,
:func:`create_external_oidc_provider` builds the Cognito provider (the client
secret stays a Secrets Manager dynamic reference — the template never carries
the literal), and :func:`create_edge_oauth_client` lists it next to the local
Cognito directory so the hosted UI offers both sign-in choices.  Absent config
leaves every template byte-identical.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from aws_cdk import (
    CustomResource,
    Duration,
    SecretValue,
    aws_cloudfront as cloudfront,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_sam as sam,
    aws_wafv2 as wafv2,
    custom_resources as cr,
)
from constructs import Construct

from stacks._demo_credentials import get_demo_password
from stacks._edge_callback_updater import CALLBACK_UPDATER_CODE

#: Edge OAuth client token lifetimes (all three edge clients, unconditionally).
#:
#: ACCESS and ID validity MUST be EQUAL. The Lambda@Edge ``CheckAuth`` handler
#: (SAR cloudfront-authorization-at-edge) verifies ONLY the id token and sends
#: the browser to ``/refreshauth`` ONLY when the id token has expired — it
#: never inspects the access token. The origin, however, validates the ACCESS
#: token (``agents/auth/middleware.py`` -> ``cognito.py::validate_token``,
#: ``token_use=access``). If access < id, there is a window after the access
#: token expires in which the edge still admits the browser but the origin
#: rejects every ``/api/*`` call with 401, and the SPA falls back to
#: ``/login.html`` — a dead end for external-IdP-only users (``oidc_only``).
#: Keeping the two lifetimes equal makes CheckAuth's id-token-expiry refresh
#: renew the access token before the origin ever sees it expired. The refresh
#: token bounds one working day (one sign-in per day).
EDGE_REFRESH_TOKEN_VALIDITY = Duration.hours(10)
EDGE_ACCESS_TOKEN_VALIDITY = Duration.minutes(60)
EDGE_ID_TOKEN_VALIDITY = Duration.minutes(60)

#: Default hosted-UI button label / Cognito provider name for the external IdP.
DEFAULT_EXTERNAL_OIDC_PROVIDER_NAME = "CorporateIdP"

_OIDC_CONTEXT_HELP = (
    "oidc_issuer_url, oidc_client_id and oidc_client_secret_arn must be provided "
    "together (deploy.sh: --oidc-issuer <https-url> --oidc-client-id <id> "
    "--oidc-secret-arn <secretsmanager-arn> [--oidc-name <ProviderName>])"
)
# Cognito provider names: 3–32 chars, no whitespace, must not start with an
# underscore. We additionally forbid underscores anywhere because external-IdP
# usernames are ``<ProviderName>_<subject>`` and an embedded underscore would
# make that shape ambiguous. ``COGNITO`` is the reserved local-directory name.
_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{1,30}[A-Za-z0-9]$")
_SECRET_ARN_RE = re.compile(r"^arn:[^:]+:secretsmanager:[^:]+:\d{12}:secret:.+$")


@dataclass(frozen=True)
class EdgeVersions:
    """Imported published versions exported by the SAR nested stack."""

    check_auth: lambda_.IVersion
    parse_auth: lambda_.IVersion
    refresh_auth: lambda_.IVersion
    sign_out: lambda_.IVersion
    http_headers: lambda_.IVersion


@dataclass(frozen=True)
class ExternalOidcConfig:
    """Resolved, validated external OIDC identity-provider settings.

    ``oidc_only`` makes the external provider the edge clients' SOLE identity
    provider (the local Cognito directory is removed from every edge client).
    Cognito's ``/oauth2/authorize`` then skips the hosted-UI chooser and sends
    an unauthenticated browser straight to the external provider, so the
    redirect chain ends at the corporate sign-in page rather than at a Cognito
    form that also offers a local password login.
    """

    issuer_url: str
    client_id: str
    client_secret_arn: str
    provider_name: str = DEFAULT_EXTERNAL_OIDC_PROVIDER_NAME
    oidc_only: bool = False


_TRUE_WORDS = frozenset({"true", "on", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "off", "no", "0", ""})


def parse_oidc_only(value: Optional[object]) -> bool:
    """Parse the ``oidc_only`` context/SSM value; unknown words fail loud."""
    if isinstance(value, bool):
        return value
    word = str(value or "").strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(
        f"oidc_only {value!r} is not a boolean (use true/false; "
        "deploy.sh: --oidc-only on|off)."
    )


def resolve_external_oidc(
    issuer_url: Optional[str],
    client_id: Optional[str],
    client_secret_arn: Optional[str],
    provider_name: Optional[str] = None,
    oidc_only: Optional[object] = None,
) -> Optional[ExternalOidcConfig]:
    """Validate the OIDC context trio into a config, or ``None`` when unset.

    Pure function (no CDK resources, no AWS calls) so it is offline-unit-
    testable. Partial configuration fails LOUD — a silently ignored half-set
    trio would strand a deployment without its corporate sign-in button.
    ``provider_name`` is optional and defaults to ``CorporateIdP``; supplying
    it WITHOUT the trio is also an error (there is nothing to name). The same
    holds for ``oidc_only=true``: an external-only client with no external
    provider would lock everyone out, so it is rejected rather than ignored.

    :raises ValueError: on partial configuration, a non-HTTPS issuer, a
        malformed Secrets Manager ARN, an invalid provider name, or a
        non-boolean / trio-less ``oidc_only``.
    """
    values = {
        "oidc_issuer_url": (issuer_url or "").strip(),
        "oidc_client_id": (client_id or "").strip(),
        "oidc_client_secret_arn": (client_secret_arn or "").strip(),
    }
    name = (provider_name or "").strip()
    only = parse_oidc_only(oidc_only)
    provided = {k: v for k, v in values.items() if v}
    if not provided:
        if name:
            raise ValueError(
                f"oidc_provider_name {name!r} was set without the provider trio. "
                + _OIDC_CONTEXT_HELP
            )
        if only:
            raise ValueError(
                "oidc_only=true was set without the provider trio; an edge client "
                "with no identity provider at all would lock every user out. "
                + _OIDC_CONTEXT_HELP
            )
        return None
    if len(provided) != len(values):
        missing = sorted(set(values) - set(provided))
        raise ValueError(
            f"Incomplete external-OIDC configuration: missing {', '.join(missing)}. "
            + _OIDC_CONTEXT_HELP
        )

    issuer = values["oidc_issuer_url"].rstrip("/")
    if not issuer.startswith("https://") or len(issuer) <= len("https://"):
        raise ValueError(
            f"oidc_issuer_url {values['oidc_issuer_url']!r} must be an https:// URL "
            "(the OpenID Connect issuer, e.g. https://login.example.com)."
        )
    if not _SECRET_ARN_RE.match(values["oidc_client_secret_arn"]):
        raise ValueError(
            f"oidc_client_secret_arn {values['oidc_client_secret_arn']!r} is not an "
            "AWS Secrets Manager secret ARN "
            "(arn:aws:secretsmanager:<region>:<account>:secret:<name>)."
        )
    name = name or DEFAULT_EXTERNAL_OIDC_PROVIDER_NAME
    if name.upper() == "COGNITO" or not _PROVIDER_NAME_RE.match(name):
        raise ValueError(
            f"oidc_provider_name {name!r} is not a valid Cognito provider name "
            "(3-32 letters, digits, '.' or '-'; no underscores or whitespace; "
            "not the reserved name COGNITO)."
        )
    return ExternalOidcConfig(
        issuer_url=issuer,
        client_id=values["oidc_client_id"],
        client_secret_arn=values["oidc_client_secret_arn"],
        provider_name=name,
        oidc_only=only,
    )


def create_external_oidc_provider(
    scope: Construct,
    *,
    construct_id: str,
    user_pool: cognito.IUserPool,
    config: ExternalOidcConfig,
) -> cognito.UserPoolIdentityProviderOidc:
    """Attach the customer's OIDC provider to ``user_pool`` (ONE per pool).

    The client secret is read from Secrets Manager through a CloudFormation
    dynamic reference (``{{resolve:secretsmanager:<arn>:SecretString:::}}``),
    so the synthesized template never carries the literal and rotating the
    secret needs no code change. ``unsafe_unwrap`` only renders that
    placeholder string — CDK's warning is about accidental plaintext, which a
    dynamic reference is not. Scopes are ``openid email`` only: the app needs
    an address to greet the user, nothing from ``profile``.
    """
    return cognito.UserPoolIdentityProviderOidc(
        scope,
        construct_id,
        user_pool=user_pool,
        name=config.provider_name,
        client_id=config.client_id,
        client_secret=SecretValue.secrets_manager(
            config.client_secret_arn
        ).unsafe_unwrap(),
        issuer_url=config.issuer_url,
        scopes=["openid", "email"],
        attribute_request_method=cognito.OidcAttributeRequestMethod.GET,
        attribute_mapping=cognito.AttributeMapping(
            email=cognito.ProviderAttribute.other("email"),
            given_name=cognito.ProviderAttribute.other("given_name"),
            family_name=cognito.ProviderAttribute.other("family_name"),
        ),
    )


def create_edge_oauth_client(
    scope: Construct,
    *,
    construct_id: str,
    user_pool: cognito.IUserPool,
    external_idp: Optional[cognito.UserPoolIdentityProviderOidc] = None,
    oidc_only: bool = False,
) -> cognito.UserPoolClient:
    """Create the code-grant client at the caller-supplied historical scope.

    Placeholder URLs avoid a client → SAR → distribution → client cycle; the
    callback-updater custom resource replaces them after distribution creation.
    When ``external_idp`` is given the client lists it alongside the local
    Cognito directory (the hosted UI then shows both sign-in choices) and
    depends on it, because Cognito rejects a client that names a provider
    that does not exist yet. With ``oidc_only`` the external provider is the
    client's ONLY provider: Cognito then skips the hosted-UI chooser and
    ``/oauth2/authorize`` redirects straight to it, so no local-password form
    is reachable on this surface. ``oidc_only`` without a provider is a
    programming error (it would produce a client nobody can sign in to).
    """
    if oidc_only and external_idp is None:
        raise ValueError(
            f"{construct_id}: oidc_only requires an external identity provider."
        )
    providers: list[cognito.UserPoolClientIdentityProvider] = []
    if not oidc_only:
        providers.append(cognito.UserPoolClientIdentityProvider.COGNITO)
    if external_idp is not None:
        providers.append(
            cognito.UserPoolClientIdentityProvider.custom(external_idp.provider_name)
        )
    client = cognito.UserPoolClient(
        scope,
        construct_id,
        user_pool=user_pool,
        generate_secret=True,
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(authorization_code_grant=True),
            scopes=[
                cognito.OAuthScope.OPENID,
                cognito.OAuthScope.EMAIL,
                cognito.OAuthScope.PROFILE,
            ],
            callback_urls=["https://example.com/parseauth"],
            logout_urls=["https://example.com/signout"],
        ),
        supported_identity_providers=providers,
        auth_flows=cognito.AuthFlow(user_srp=True),
        refresh_token_validity=EDGE_REFRESH_TOKEN_VALIDITY,
        access_token_validity=EDGE_ACCESS_TOKEN_VALIDITY,
        id_token_validity=EDGE_ID_TOKEN_VALIDITY,
    )
    if external_idp is not None:
        client.node.add_dependency(external_idp)
    return client


def edge_http_headers(*, allow_websockets: bool) -> str:
    """Return the historical report-only SPA security-header JSON."""
    websocket = "wss: " if allow_websockets else ""
    return json.dumps({
        "Content-Security-Policy-Report-Only": (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' {websocket}https://*.amazonaws.com https://*.amazoncognito.com"
        ),
        "Strict-Transport-Security": "max-age=31536000; includeSubdomains; preload",
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
    })


def _edge_version(
    scope: Construct, edge_auth: sam.CfnApplication, handler_name: str
) -> lambda_.IVersion:
    """Wrap ``Outputs.<Name>Handler`` without adding a construct path segment."""
    version_arn = edge_auth.get_att(f"Outputs.{handler_name}Handler").to_string()
    return lambda_.Version.from_version_arn(
        scope, f"EdgeFn{handler_name}", version_arn
    )


def deploy_edge_auth(
    scope: Construct,
    *,
    user_pool: cognito.IUserPool,
    client: cognito.IUserPoolClient,
    http_headers: str,
) -> tuple[sam.CfnApplication, EdgeVersions]:
    """Deploy SAR v2.3.2 in auth-only mode and import its five versions."""
    edge_auth = sam.CfnApplication(
        scope,
        "LambdaEdgeAuth",
        location=sam.CfnApplication.ApplicationLocationProperty(
            # AWS-published SAR application publisher account; required ARN.
            application_id=(
                "arn:aws:serverlessrepo:us-east-1:520945424137:"
                "applications/cloudfront-authorization-at-edge"
            ),
            semantic_version="2.3.2",
        ),
        parameters={
            "CreateCloudFrontDistribution": "false",
            "EnableSPAMode": "false",
            "UserPoolArn": user_pool.user_pool_arn,
            "UserPoolClientId": client.user_pool_client_id,
            "OAuthScopes": "openid,email,profile",
            "HttpHeaders": http_headers,
        },
    )
    versions = EdgeVersions(**{
        attr: _edge_version(scope, edge_auth, handler)
        for attr, handler in (
            ("check_auth", "CheckAuth"),
            ("parse_auth", "ParseAuth"),
            ("refresh_auth", "RefreshAuth"),
            ("sign_out", "SignOut"),
            ("http_headers", "HttpHeaders"),
        )
    })
    return edge_auth, versions


def edge_behavior_parts(
    *, origin, versions: EdgeVersions, origin_request_policy
) -> tuple[dict, cloudfront.BehaviorOptions, dict]:
    """Build common options, guarded default, and the three auth endpoints."""
    common = _common_options(origin, origin_request_policy)
    vreq = cloudfront.LambdaEdgeEventType.VIEWER_REQUEST
    default = guarded_behavior(
        origin=origin, versions=versions, origin_request_policy=origin_request_policy
    )
    auth_paths = {
        "/parseauth": _auth_behavior(common, vreq, versions.parse_auth),
        "/refreshauth": _auth_behavior(common, vreq, versions.refresh_auth),
        "/signout": _auth_behavior(common, vreq, versions.sign_out),
    }
    return common, default, auth_paths


#: The SAR's three auth endpoints (its RedirectPathSignIn / RedirectPathAuthRefresh
#: / SignOutUrl defaults; deploy_edge_auth does not override them).
EDGE_AUTH_PATHS = ("/parseauth", "/refreshauth", "/signout")

#: Sign-out URLs registered on each edge client, per hostname: ``/signout``
#: (historical) and ``/``, the SAR's RedirectPathSignOut default, which its
#: SignOut handler sends to Cognito ``/logout`` as ``logout_uri``.
EDGE_LOGOUT_PATHS = ("/signout", "/")

#: Web ACL rule (and metric) name for the bare-callback redirect.
BARE_AUTH_REDIRECT_RULE_NAME = "EdgeAuthBareCallbackRedirect"

_W = wafv2.CfnWebACL
_NO_TRANSFORM = [_W.TextTransformationProperty(priority=0, type="NONE")]


def _st(**kwargs) -> _W.StatementProperty:
    return _W.StatementProperty(**kwargs)


#: The SAR auth paths, including the equivalent spellings that were seen
#: reaching the SAR behaviors on a live distribution (2026-11-16): repeated
#: leading slashes, ``%2F`` and ``/./`` segments (``//signout``,
#: ``/%2Fparseauth``, ``//./signout``, ``/%2e/signout``). An exact match on
#: the raw path missed those, and each served the SAR's 200 page anonymously.
#: Matched after URL_DECODE: one leading slash, then any mix of extra slashes
#: and ``./`` segments, then the endpoint name, nothing after.
#: Not covered here: parent-segment forms such as ``/x/../signout``. On that
#: distribution they got 403 from the managed rule groups, which run first
#: (priorities 1-3); this rule relies on them for those forms.
AUTH_PATH_REGEX = (
    r"^/(/|\./)*("
    + "|".join(p.lstrip("/") for p in EDGE_AUTH_PATHS)
    + r")$"
)


def _auth_path() -> _W.StatementProperty:
    return _st(regex_match_statement=_W.RegexMatchStatementProperty(
        field_to_match=_W.FieldToMatchProperty(uri_path={}),
        regex_string=AUTH_PATH_REGEX,
        text_transformations=[_W.TextTransformationProperty(priority=0, type="URL_DECODE")],
    ))


def _not_empty(field: _W.FieldToMatchProperty) -> _W.StatementProperty:
    """Matches when the component is present and longer than zero bytes."""
    return _st(size_constraint_statement=_W.SizeConstraintStatementProperty(
        field_to_match=field, comparison_operator="GT", size=0,
        text_transformations=_NO_TRANSFORM,
    ))


def _bare_redirect_headers(http_headers: str) -> list:
    """The SAR's security headers (same JSON the HttpHeaders function gets),
    with ``X-Frame-Options: DENY`` since a redirect is never framed."""
    headers = {**json.loads(http_headers), "X-Frame-Options": "DENY", "Location": "/"}
    return [_W.CustomHTTPHeaderProperty(name=k, value=v) for k, v in headers.items()]


def bare_auth_callback_rule(http_headers: str, priority: int = 4) -> _W.RuleProperty:
    """302 to ``/`` for a BARE request to one of the SAR auth endpoints.

    The SAR handlers answer requests that are not part of a real flow with an
    HTTP 200 HTML error page, which a security scanner reads as an
    unauthenticated page (found 2026-11-05). ``/`` runs CheckAuth, which
    starts the Cognito -> IdP sign-in chain.

    Bare means no query string AND no Cookie header (or an empty one). Every
    real flow has one or the other: the Cognito callback carries
    ``code``/``state`` or ``error``, CheckAuth's refresh redirect carries
    ``requestedUri`` and ``nonce``, and sign-out carries the session cookies.
    The rule does not try to judge the SAR's checks; anything that is not bare
    reaches the unchanged SAR handler. The Cookie header is inspected as a
    single header, which AWS WAF reads in full (no 8 KB window), so a large
    Cookie header is never taken for an absent one. ``priority`` 4 puts it
    after the three managed rule groups.
    """
    return _W.RuleProperty(
        name=BARE_AUTH_REDIRECT_RULE_NAME,
        priority=priority,
        action=_W.RuleActionProperty(block=_W.BlockActionProperty(
            custom_response=_W.CustomResponseProperty(
                response_code=302,
                response_headers=_bare_redirect_headers(http_headers),
            ),
        )),
        statement=_st(and_statement=_W.AndStatementProperty(statements=[
            _auth_path(),
            _st(not_statement=_W.NotStatementProperty(statement=_not_empty(
                _W.FieldToMatchProperty(query_string={})))),
            _st(not_statement=_W.NotStatementProperty(statement=_not_empty(
                _W.FieldToMatchProperty(single_header={"Name": "cookie"})))),
        ])),
        visibility_config=_W.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=BARE_AUTH_REDIRECT_RULE_NAME,
            sampled_requests_enabled=True,
        ),
    )


def _common_options(origin, origin_request_policy) -> dict:
    return dict(
        origin=origin,
        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
        origin_request_policy=origin_request_policy,
        allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
    )


def guarded_behavior(
    *, origin, versions: EdgeVersions, origin_request_policy
) -> cloudfront.BehaviorOptions:
    """A behavior behind CheckAuth (viewer request) + HttpHeaders (viewer
    response): the default behavior's shape, for any origin."""
    return cloudfront.BehaviorOptions(
        **_common_options(origin, origin_request_policy),
        edge_lambdas=[
            cloudfront.EdgeLambda(
                event_type=cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
                function_version=versions.check_auth,
            ),
            cloudfront.EdgeLambda(
                event_type=cloudfront.LambdaEdgeEventType.VIEWER_RESPONSE,
                function_version=versions.http_headers,
            ),
        ],
    )


#: Path of the Streamlit frontend bundle (the static files shipped inside the
#: streamlit wheel: JS chunks, CSS, fonts). Content-hashed, public, no app data.
STREAMLIT_STATIC_PATH = "/static/*"


def public_bundle_behavior(*, origin) -> cloudfront.BehaviorOptions:
    """Ungated, edge-cached behavior for a framework's public static bundle.

    Streamlit's index.html preloads about 110 hashed files at once. Behind the
    guarded default every one of them runs CheckAuth on viewer request and
    HttpHeaders on viewer response, so the first page load after sign-in fires
    over 200 cold Lambda@Edge invocations in about a second. On a fresh account
    (low Lambda concurrency, every replica cold) some of those can fail, a chunk
    never loads, and the page stays blank with the index title "Streamlit".
    A reload works only because the chunks that did load are browser-cached
    ("public, immutable"), so the second burst is small. Seen once on a fresh
    Workshop Studio account (2026-10-03); the failure mode is inferred from the
    config, not observed in edge metrics.

    The bundle carries no application data: data reaches the browser over the
    gated ``/_stcore/stream`` websocket and the gated document. So this path
    skips edge auth, is cached at CloudFront (GET/HEAD only, no cookies or
    query string in the key, nothing forwarded to the origin), and keeps the
    baseline security headers through the managed response-headers policy
    instead of the HttpHeaders function.
    """
    return cloudfront.BehaviorOptions(
        origin=origin,
        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
        allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
    )


def _auth_behavior(common: dict, event_type, version) -> cloudfront.BehaviorOptions:
    return cloudfront.BehaviorOptions(
        **common,
        edge_lambdas=[cloudfront.EdgeLambda(
            event_type=event_type, function_version=version
        )],
    )


def managed_rule(
    name: str, priority: int, metric: str, *, rule_action_overrides=None
) -> wafv2.CfnWebACL.RuleProperty:
    """One AWS managed rule group entry with the historical visibility config."""
    return wafv2.CfnWebACL.RuleProperty(
        name=name,
        priority=priority,
        override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
        statement=wafv2.CfnWebACL.StatementProperty(
            managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                vendor_name="AWS",
                name=name,
                rule_action_overrides=rule_action_overrides,
            ),
        ),
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=metric,
            sampled_requests_enabled=True,
        ),
    )


def create_standard_web_acl(
    scope: Construct,
    *,
    construct_id: str,
    metric_name: str,
    body_inspection_limit: str | None = None,
    count_common_rules: Iterable[str] = (),
    bare_callback_redirect_headers: str | None = None,
) -> wafv2.CfnWebACL:
    """Create the shared three-rule CLOUDFRONT web ACL with optional ADT tuning.

    ADT raises body inspection to 64 KB for onboarding mapping payloads and
    counts ``SizeRestrictions_BODY`` because that managed rule retains a fixed
    8-KB limit; all XSS/LFI/RFI and other common rules remain active.

    ``bare_callback_redirect_headers`` (distributions with Authorization@Edge:
    the same HttpHeaders JSON given to :func:`deploy_edge_auth`) adds
    :func:`bare_auth_callback_rule` after the managed groups, at priority 4.
    Without it the ACL is the three managed rules, byte-identical to before.
    """
    association_config = _association_config(body_inspection_limit)
    common_overrides = [
        wafv2.CfnWebACL.RuleActionOverrideProperty(
            name=name,
            action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={}),
        )
        for name in count_common_rules
    ] or None
    return wafv2.CfnWebACL(
        scope,
        construct_id,
        scope="CLOUDFRONT",
        default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
        association_config=association_config,
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=metric_name,
            sampled_requests_enabled=True,
        ),
        rules=[
            managed_rule("AWSManagedRulesCommonRuleSet", 1, "CommonRuleSet",
                         rule_action_overrides=common_overrides),
            managed_rule("AWSManagedRulesKnownBadInputsRuleSet", 2, "KnownBadInputs"),
            managed_rule("AWSManagedRulesAmazonIpReputationList", 3, "IpReputation"),
            *([bare_auth_callback_rule(bare_callback_redirect_headers)]
              if bare_callback_redirect_headers else []),
        ],
    )


def _association_config(limit: str | None):
    if limit is None:
        return None
    return wafv2.CfnWebACL.AssociationConfigProperty(
        request_body={
            "CLOUDFRONT": wafv2.CfnWebACL.RequestBodyAssociatedResourceTypeConfigProperty(
                default_size_inspection_limit=limit
            )
        }
    )


def _client_change_triggers(client: cognito.IUserPoolClient) -> dict:
    """Client properties whose change must re-run the callback updater.

    Any change to the client's template properties makes CloudFormation call
    ``UpdateUserPoolClient`` with the PLACEHOLDER callback URLs, silently
    reverting the real ones; the custom resource only re-runs when ITS
    properties change. Carrying the lifetimes and provider list (tokens
    included — CloudFormation resolves them) keeps the two in lock-step. The
    handler ignores these keys. Imported clients (no L1 child) contribute
    nothing — their template never updates the client anyway.
    """
    cfn = client.node.default_child
    if not isinstance(cfn, cognito.CfnUserPoolClient):
        return {}
    return {
        "ClientAccessTokenValidity": cfn.access_token_validity,
        "ClientIdTokenValidity": cfn.id_token_validity,
        "ClientRefreshTokenValidity": cfn.refresh_token_validity,
        "ClientIdentityProviders": cfn.supported_identity_providers or [],
    }


def create_callback_updater(
    scope: Construct,
    *,
    function_name: str,
    user_pool: cognito.IUserPool,
    client: cognito.IUserPoolClient,
    distribution: cloudfront.IDistribution,
    custom_domain: str | None,
) -> CustomResource:
    """Patch callback/logout URLs after the distribution token resolves."""
    fn = lambda_.Function(
        scope,
        "CallbackUpdaterLambda",
        function_name=function_name,
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        timeout=Duration.minutes(2),
        memory_size=256,
        code=lambda_.Code.from_inline(CALLBACK_UPDATER_CODE),
    )
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "cognito-idp:DescribeUserPoolClient",
            "cognito-idp:UpdateUserPoolClient",
        ],
        resources=[user_pool.user_pool_arn],
    ))
    provider = cr.Provider(scope, "CallbackUpdaterProvider", on_event_handler=fn)
    resource = CustomResource(
        scope,
        "CallbackUpdater",
        service_token=provider.service_token,
        properties={
            "UserPoolId": user_pool.user_pool_id,
            "ClientId": client.user_pool_client_id,
            "CloudFrontDomain": distribution.distribution_domain_name,
            "CustomDomain": custom_domain or "",
            # Added 2026-11-05: a new property re-runs the updater, so live
            # clients pick up the "/" sign-out URL on their next deploy.
            "LogoutPaths": list(EDGE_LOGOUT_PATHS),
            # Observed 2026-09-23: a token-lifetime change reverted the real
            # callbacks to the placeholders -> redirect_mismatch on every host.
            **_client_change_triggers(client),
        },
    )
    resource.node.add_dependency(distribution)
    return resource


# One handler source with a marker for the historical ERP trailing whitespace.
# The two call sites still share one handler source; the __BLANK__ markers keep
# the ERP and AgentCore renders apart. The log line prints only the request type
# and username, never the full event (its ResourceProperties name the demo
# secret), so the deployed code hash changes once from the old full-event dump.
_DEMO_USER_HANDLER = '''
import json
import boto3
import os

def handler(event, context):
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    print(f"RequestType: {request_type} Username: {props.get('Username', 'ADT')}")
__BLANK__
    user_pool_id = props.get("UserPoolId") or os.environ.get("USER_POOL_ID")
    username = props.get("Username", "ADT")
__BLANK__
    cognito = boto3.client("cognito-idp")
__BLANK__
    if request_type == "Delete":
        try:
            cognito.admin_delete_user(
                UserPoolId=user_pool_id,
                Username=username
            )
            print(f"Deleted user: {username}")
        except cognito.exceptions.UserNotFoundException:
            print(f"User {username} not found, nothing to delete")
        except Exception as e:
            print(f"Error deleting user: {e}")
        return {"PhysicalResourceId": f"user-{username}"}

    # Read the password from Secrets Manager (the single source of truth that
    # operators retrieve post-deploy). Fail fast if missing rather than falling
    # back to a default — a wrong password would lock the demo user.
    secret_name = props.get("SecretName") or os.environ.get("DEMO_PASSWORD_SECRET_NAME")
    if not secret_name:
        raise RuntimeError("DEMO_PASSWORD_SECRET_NAME / SecretName not configured")
    password = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name).get("SecretString")
    if not password:
        raise RuntimeError(f"Secret {secret_name} has no SecretString value")
__BLANK__
    try:
        cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            TemporaryPassword=password,
            MessageAction="SUPPRESS",
        )
        print(f"Created user: {username}")
__BLANK8__
        cognito.admin_set_user_password(
            UserPoolId=user_pool_id,
            Username=username,
            Password=password,
            Permanent=True
        )
        print(f"Set permanent password for user: {username}")
__BLANK8__
    except cognito.exceptions.UsernameExistsException:
        print(f"User {username} already exists, updating password")
        cognito.admin_set_user_password(
            UserPoolId=user_pool_id,
            Username=username,
            Password=password,
            Permanent=True
        )
    except Exception as e:
        print(f"Error creating user: {e}")
        raise
__BLANK__
    return {
        "PhysicalResourceId": f"user-{username}",
        "Data": {
            "Username": username
        }
    }
'''


def create_demo_user_provider(
    scope: Construct,
    *,
    function_name: str,
    user_pool: cognito.IUserPool,
    secret_name: str,
    preserve_indented_blank_lines: bool = False,
) -> cr.Provider:
    """Create the shared demo-user handler and its least-privilege provider."""
    blank = "    " if preserve_indented_blank_lines else ""
    code = _DEMO_USER_HANDLER.replace(
        "__BLANK8__", "        " if preserve_indented_blank_lines else ""
    ).replace("__BLANK__", blank)
    fn = lambda_.Function(
        scope,
        "CreateUserLambda",
        function_name=function_name,
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        timeout=Duration.minutes(2),
        memory_size=256,
        code=lambda_.Code.from_inline(code),
        environment={
            "USER_POOL_ID": user_pool.user_pool_id,
            "DEMO_PASSWORD_SECRET_NAME": secret_name,
        },
    )
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=[
            "cognito-idp:AdminCreateUser",
            "cognito-idp:AdminSetUserPassword",
            "cognito-idp:AdminDeleteUser",
        ],
        resources=[user_pool.user_pool_arn],
    ))
    stack = scope if hasattr(scope, "region") else scope.node.scopes[-1]
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["secretsmanager:GetSecretValue"],
        resources=[
            f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:{secret_name}-*"
        ],
    ))
    return cr.Provider(scope, "CreateUserProvider", on_event_handler=fn)


def add_demo_user(
    scope: Construct,
    *,
    construct_id: str,
    provider: cr.Provider,
    user_pool: cognito.IUserPool,
    username: str,
    secret_name: str,
) -> CustomResource:
    """Add one exact historical demo-user custom resource."""
    return CustomResource(
        scope,
        construct_id,
        service_token=provider.service_token,
        properties={
            "Username": username,
            "UserPoolId": user_pool.user_pool_id,
            "SecretName": secret_name,
            "PasswordTrigger": get_demo_password(scope),
        },
    )
