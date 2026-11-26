# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression tests for the optional external OIDC identity
provider on the Authorization@Edge user pools (ADR-0072), and for the
gateway CUSTOM_JWT allowlist carrying the edge client (ADR-0070 amendment
2026-09-23).

Credential-blind synths of the stacks that carry an edge OAuth client (ADT
app — owned by ADTAgentCoreStack since the 2026-09-23 relocation —, ERP
dashboard, HighByte UI) plus ADTComputeStack (the ADT app's SAR/callback-
updater home) and ADTOnboardingStack (the second gateway), synthesized
``--exclusively`` so the Docker-bundled sibling stacks are not built:

* **on-path** — the ``oidc_*`` context trio is set: every edge client lists the
  provider next to the local Cognito directory and depends on it, the provider
  is type OIDC with the fixed scopes/mapping, and the client secret is a
  Secrets Manager DYNAMIC REFERENCE (the template never carries a literal).
* **off-path** — no ``oidc_*`` context: no ``UserPoolIdentityProvider``
  resource exists anywhere and each edge client supports exactly ``COGNITO``.
* **only-path** — the trio plus ``oidc_only=true``: every edge client lists
  the provider as its SOLE identity provider (``COGNITO`` absent), so Cognito
  skips the hosted-UI chooser and ``/oauth2/authorize`` redirects straight to
  the provider; everything else (provider resource, secret reference, token
  lifetimes) is identical to the on-path.
* **edge-off** — ``edge_auth=false``: both gateways keep exactly the two
  original allowed clients (the template is byte-identical to the pre-edge
  baseline).

Both paths also pin the tightened edge-client token lifetimes (refresh 10 h,
access 60 min, id 60 min — access MUST equal id because ``CheckAuth``
refreshes on id-token expiry only while the origin validates the access
token; see stacks/_edge_auth.py).

Requires the vendored CDK CLI + Node under node_modules — skips if unavailable
(mirrors tests/test_cognito_domain_pinning.py).
"""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"

#: Stacks that OWN an edge OAuth client. The ADT app's client lives in
#: ADTAgentCoreStack (relocated 2026-09-23): the query/onboarding gateway
#: CUSTOM_JWT authorizers must list it in AllowedClients, and ADTComputeStack
#: is downstream of both, so owning it there was a dependency cycle.
EDGE_STACKS = ("ADTAgentCoreStack", "ADTERPDashboardStack", "ADTHighByteOpcuaStack")
#: Logical-id fragment of each stack's edge OAuth client (construct ids are
#: migration contracts — see stacks/_edge_auth.py).
EDGE_CLIENT_IDS = {
    "ADTAgentCoreStack": "AdtEdgeAuthClient",
    "ADTERPDashboardStack": "DashboardAuthClient",
    "ADTHighByteOpcuaStack": "HighByteUiAuthClient",
}
#: Stack holding each edge client's CallbackUpdater custom resource. The ADT
#: app's updater stays DOWNSTREAM in ADTComputeStack (next to the CloudFront
#: distribution) and consumes the client id via a CFN export.
UPDATER_STACKS = {
    "ADTAgentCoreStack": "ADTComputeStack",
    "ADTERPDashboardStack": "ADTERPDashboardStack",
    "ADTHighByteOpcuaStack": "ADTHighByteOpcuaStack",
}
#: Both adt-app-issuer gateways and the stacks that own them.
GATEWAYS = {
    "ADTAgentCoreStack": "adt-query-gw",
    "ADTOnboardingStack": "adt-onboard-gw",
}
#: Everything the edge-on fixtures synthesize.
SYNTH_STACKS = EDGE_STACKS + ("ADTOnboardingStack", "ADTComputeStack")

ISSUER = "https://login.example.com"
CLIENT_ID = "example-client-id"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/oidc-client-secret-AbCdEf"  # nosec B105 - a secret's ARN with a placeholder account, not a value
PROVIDER_NAME = "CorporateIdP"

_BASE_CONTEXT = {
    "region": "us-east-1",
    "with_simulator": "true",      # ERP dashboard stack exists
    "highbyte_enabled": "true",    # HighByte stack exists
    "highbyte_accept_eula": "true",  # the hub runs only with its EULA accepted
    "edge_auth": "true",           # ADT app edge client exists
    "cdk_nag_disabled": "true",
}
_OIDC_CONTEXT = {
    "oidc_issuer_url": ISSUER,
    "oidc_client_id": CLIENT_ID,
    "oidc_client_secret_arn": SECRET_ARN,
}


def _synth(
    tmp_path_factory,
    *,
    context: dict[str, str],
    slug: str,
    stacks: tuple[str, ...] = SYNTH_STACKS,
) -> dict[str, dict]:
    """Credential-blind synth of the given stacks; returns templates by name."""
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp(slug)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    env.pop("CDK_DEFAULT_ACCOUNT", None)
    args = [str(CDK_BIN), "synth", *stacks, "--exclusively", "--no-lookups"]
    for key, value in context.items():
        args.extend(["-c", f"{key}={value}"])
    args.extend(["--output", str(out_dir), "--quiet"])
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        args, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=900,
    )
    templates = {}
    for stack in stacks:
        path = out_dir / f"{stack}.template.json"
        if not path.exists():
            pytest.skip(f"synth did not produce {stack}:\n{result.stderr[-2000:]}")
        templates[stack] = json.loads(path.read_text())
    return templates


@pytest.fixture(scope="session")
def oidc_on(tmp_path_factory) -> dict[str, dict]:
    return _synth(tmp_path_factory, context={**_BASE_CONTEXT, **_OIDC_CONTEXT}, slug="cdk_synth_oidc_on")


@pytest.fixture(scope="session")
def oidc_off(tmp_path_factory) -> dict[str, dict]:
    return _synth(tmp_path_factory, context=_BASE_CONTEXT, slug="cdk_synth_oidc_off")


@pytest.fixture(scope="session")
def oidc_only(tmp_path_factory) -> dict[str, dict]:
    return _synth(
        tmp_path_factory,
        context={**_BASE_CONTEXT, **_OIDC_CONTEXT, "oidc_only": "true"},
        slug="cdk_synth_oidc_only",
    )


@pytest.fixture(scope="session")
def edge_off(tmp_path_factory) -> dict[str, dict]:
    """Edge auth explicitly OFF — only the two gateway-owning stacks."""
    return _synth(
        tmp_path_factory,
        context={
            "region": "us-east-1",
            "edge_auth": "false",
            "cdk_nag_disabled": "true",
        },
        slug="cdk_synth_edge_off",
        stacks=("ADTAgentCoreStack", "ADTOnboardingStack"),
    )


def _resources(template: dict, cfn_type: str) -> dict:
    return {
        lid: r for lid, r in template.get("Resources", {}).items()
        if r.get("Type") == cfn_type
    }


def _edge_client(template: dict, stack: str) -> tuple[str, dict]:
    clients = _resources(template, "AWS::Cognito::UserPoolClient")
    matches = {lid: r for lid, r in clients.items() if EDGE_CLIENT_IDS[stack] in lid}
    assert len(matches) == 1, (stack, sorted(clients))
    return next(iter(matches.items()))


def _import_target(producer_template: dict, entry) -> object:
    """Resolve an ``Fn::ImportValue`` entry to the producer's exported value."""
    assert isinstance(entry, dict) and "Fn::ImportValue" in entry, entry
    name = entry["Fn::ImportValue"]
    for output in producer_template.get("Outputs", {}).values():
        if output.get("Export", {}).get("Name") == name:
            return output["Value"]
    raise AssertionError(f"no export named {name!r} in the producer template")


def _gateway(template: dict, name: str) -> dict:
    gws = [
        r for r in _resources(template, "AWS::BedrockAgentCore::Gateway").values()
        if r["Properties"].get("Name") == name
    ]
    assert len(gws) == 1, (name, len(gws))
    return gws[0]


def _allowed_clients(template: dict, gateway_name: str) -> list:
    props = _gateway(template, gateway_name)["Properties"]
    return props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedClients"]


# ---------------------------------------------------------------------------
# On-path: trio configured
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stack", EDGE_STACKS)
def test_each_edge_pool_gets_exactly_one_oidc_provider(oidc_on, stack):
    providers = _resources(oidc_on[stack], "AWS::Cognito::UserPoolIdentityProvider")
    assert len(providers) == 1, (stack, sorted(providers))
    props = next(iter(providers.values()))["Properties"]
    assert props["ProviderName"] == PROVIDER_NAME
    assert props["ProviderType"] == "OIDC"
    details = props["ProviderDetails"]
    assert details["oidc_issuer"] == ISSUER
    assert details["client_id"] == CLIENT_ID
    assert details["authorize_scopes"] == "openid email"
    assert details["attributes_request_method"] == "GET"
    assert props["AttributeMapping"] == {
        "email": "email",
        "given_name": "given_name",
        "family_name": "family_name",
    }


@pytest.mark.parametrize("stack", EDGE_STACKS)
def test_client_secret_is_a_dynamic_reference_never_a_literal(oidc_on, stack):
    providers = _resources(oidc_on[stack], "AWS::Cognito::UserPoolIdentityProvider")
    secret = next(iter(providers.values()))["Properties"]["ProviderDetails"]["client_secret"]
    assert secret.startswith("{{resolve:secretsmanager:"), secret
    assert SECRET_ARN in secret
    assert secret.endswith(":SecretString:::}}"), secret


@pytest.mark.parametrize("stack", EDGE_STACKS)
def test_edge_client_lists_cognito_and_the_provider_and_depends_on_it(oidc_on, stack):
    template = oidc_on[stack]
    provider_lid = next(iter(_resources(template, "AWS::Cognito::UserPoolIdentityProvider")))
    _lid, client = _edge_client(template, stack)
    assert client["Properties"]["SupportedIdentityProviders"] == [
        "COGNITO", {"Ref": provider_lid},
    ]
    assert provider_lid in client.get("DependsOn", []), client.get("DependsOn")


# ---------------------------------------------------------------------------
# Only-path: trio + oidc_only — the external provider is the SOLE sign-in
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stack", EDGE_STACKS)
def test_oidc_only_client_lists_the_provider_alone_and_depends_on_it(oidc_only, stack):
    """No ``COGNITO`` on the client: Cognito skips the hosted-UI chooser and
    ``/oauth2/authorize`` redirects straight to the external provider, so an
    unauthenticated browser never reaches a local-password form."""
    template = oidc_only[stack]
    provider_lid = next(iter(_resources(template, "AWS::Cognito::UserPoolIdentityProvider")))
    _lid, client = _edge_client(template, stack)
    assert client["Properties"]["SupportedIdentityProviders"] == [{"Ref": provider_lid}]
    assert provider_lid in client.get("DependsOn", []), client.get("DependsOn")


@pytest.mark.parametrize("stack", EDGE_STACKS)
def test_oidc_only_changes_nothing_but_the_provider_list(oidc_on, oidc_only, stack):
    """The provider resource and every other client property are byte-identical
    to the on-path; ``oidc_only`` is purely a provider-list switch."""
    on_providers = _resources(oidc_on[stack], "AWS::Cognito::UserPoolIdentityProvider")
    only_providers = _resources(oidc_only[stack], "AWS::Cognito::UserPoolIdentityProvider")
    assert on_providers == only_providers
    _l1, on_client = _edge_client(oidc_on[stack], stack)
    _l2, only_client = _edge_client(oidc_only[stack], stack)
    on_props = {k: v for k, v in on_client["Properties"].items() if k != "SupportedIdentityProviders"}
    only_props = {k: v for k, v in only_client["Properties"].items() if k != "SupportedIdentityProviders"}
    assert on_props == only_props
    assert on_client.get("DependsOn") == only_client.get("DependsOn")


# ---------------------------------------------------------------------------
# Off-path: no trio — nothing added
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stack", EDGE_STACKS)
def test_without_config_no_provider_and_cognito_only_clients(oidc_off, stack):
    template = oidc_off[stack]
    assert _resources(template, "AWS::Cognito::UserPoolIdentityProvider") == {}
    _lid, client = _edge_client(template, stack)
    assert client["Properties"]["SupportedIdentityProviders"] == ["COGNITO"]
    assert "DependsOn" not in client or not any(
        "ExternalIdp" in dep for dep in client["DependsOn"]
    )


# ---------------------------------------------------------------------------
# Token lifetimes — unconditional on every edge client
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stack", EDGE_STACKS)
@pytest.mark.parametrize("fixture_name", ["oidc_on", "oidc_off", "oidc_only"])
def test_edge_client_token_lifetimes(request, fixture_name, stack):
    template = request.getfixturevalue(fixture_name)[stack]
    _lid, client = _edge_client(template, stack)
    props = client["Properties"]
    assert props["RefreshTokenValidity"] == 600     # 10 h
    assert props["AccessTokenValidity"] == 60       # 60 min
    assert props["IdTokenValidity"] == 60           # 60 min
    # CheckAuth refreshes on ID-token expiry only; the origin validates the
    # ACCESS token — unequal lifetimes open an edge-admits/origin-401 window.
    assert props["AccessTokenValidity"] == props["IdTokenValidity"]
    assert props["TokenValidityUnits"] == {
        "AccessToken": "minutes", "IdToken": "minutes", "RefreshToken": "minutes",
    }


@pytest.mark.parametrize("stack", EDGE_STACKS)
@pytest.mark.parametrize("fixture_name", ["oidc_on", "oidc_off", "oidc_only"])
def test_callback_updater_reruns_on_client_change(request, fixture_name, stack):
    """The CallbackUpdater custom resource carries the client's lifetimes and
    provider list so a client-only change (which makes CFN re-apply the
    placeholder callback URLs) also re-runs the updater (2026-09-23 incident:
    lifetime change -> `redirect_mismatch` on every host).

    The ADT app's client lives UPSTREAM in ADTAgentCoreStack while its updater
    stays in ADTComputeStack next to the distribution, so that pair's
    resource-valued entries render as ``Fn::ImportValue`` in the updater and
    must be resolved through the producer's export back to the client-side
    ``Ref``. Same-stack pairs (ERP, HighByte) stay exact-equal."""
    templates = request.getfixturevalue(fixture_name)
    client_template = templates[stack]
    updater_template = templates[UPDATER_STACKS[stack]]
    _lid, client = _edge_client(client_template, stack)
    cprops = client["Properties"]
    updaters = [
        r for r in updater_template["Resources"].values()
        if r["Type"] == "AWS::CloudFormation::CustomResource"
        and "ClientId" in r["Properties"] and "CloudFrontDomain" in r["Properties"]
    ]
    assert len(updaters) == 1, "exactly one CallbackUpdater per edge surface"
    uprops = updaters[0]["Properties"]
    assert uprops["ClientAccessTokenValidity"] == cprops["AccessTokenValidity"]
    assert uprops["ClientIdTokenValidity"] == cprops["IdTokenValidity"]
    assert uprops["ClientRefreshTokenValidity"] == cprops["RefreshTokenValidity"]
    cprov = cprops["SupportedIdentityProviders"]
    uprov = uprops["ClientIdentityProviders"]
    assert len(uprov) == len(cprov), (uprov, cprov)
    for client_entry, updater_entry in zip(cprov, uprov):
        if isinstance(client_entry, str) or UPDATER_STACKS[stack] == stack:
            assert updater_entry == client_entry
        else:
            # Cross-stack: the updater carries the imported provider name and
            # the producer's export must point at the SAME provider Ref.
            assert _import_target(client_template, updater_entry) == client_entry


# ---------------------------------------------------------------------------
# Gateway CUSTOM_JWT allowlist — the edge client MUST be allowed on BOTH
# adt-app-issuer gateways (ADR-0070 amendment 2026-09-23): with edge auth on,
# the browser's bearer is minted by the EDGE client and the app forwards it as
# the gateway bearer (ADR-0049) — a missing entry 403s every interactive query
# ("Query failed"). The edge client lives in ADTAgentCoreStack for exactly
# this reason (ADTComputeStack is downstream of both gateways — a cycle).
# ---------------------------------------------------------------------------

def test_query_gateway_allows_the_edge_client_when_edge_auth_is_on(oidc_on):
    agentcore = oidc_on["ADTAgentCoreStack"]
    edge_lid, _client = _edge_client(agentcore, "ADTAgentCoreStack")
    allowed = _allowed_clients(agentcore, GATEWAYS["ADTAgentCoreStack"])
    assert len(allowed) == 3, allowed
    assert {"Ref": edge_lid} in allowed, allowed


def test_onboarding_gateway_allows_the_edge_client_when_edge_auth_is_on(oidc_on):
    agentcore = oidc_on["ADTAgentCoreStack"]
    edge_lid, _client = _edge_client(agentcore, "ADTAgentCoreStack")
    allowed = _allowed_clients(
        oidc_on["ADTOnboardingStack"], GATEWAYS["ADTOnboardingStack"]
    )
    assert len(allowed) == 3, allowed
    # Cross-stack: every entry is an Fn::ImportValue; resolve each through the
    # agentcore exports and require the edge client's Ref among them.
    resolved = [_import_target(agentcore, entry) for entry in allowed]
    assert {"Ref": edge_lid} in resolved, resolved


@pytest.mark.parametrize("stack", sorted(GATEWAYS))
def test_gateways_keep_exactly_two_clients_when_edge_auth_is_off(edge_off, stack):
    """Off path stays byte-identical to the pre-edge baseline: exactly the
    interactive login client and the service client."""
    allowed = _allowed_clients(edge_off[stack], GATEWAYS[stack])
    assert len(allowed) == 2, allowed
    blob = json.dumps(allowed)
    assert "EdgeAuthClient" not in blob, allowed


def test_compute_stack_owns_no_cognito_client_or_provider(oidc_on):
    """Relocation tripwire: the ADT app's edge client + external IdP moved to
    ADTAgentCoreStack (2026-09-23); ADTComputeStack must not re-grow either —
    it reads both handles from its agentcore handle."""
    template = oidc_on["ADTComputeStack"]
    assert _resources(template, "AWS::Cognito::UserPoolClient") == {}
    assert _resources(template, "AWS::Cognito::UserPoolIdentityProvider") == {}


def _app_distribution_behaviors(template: dict) -> dict:
    """PathPattern -> behavior for the app distribution in ADTComputeStack."""
    (dist,) = [r for r in _resources(template, "AWS::CloudFront::Distribution").values()
               if any(b.get("PathPattern") == "/api/*"
                      for b in r["Properties"]["DistributionConfig"].get("CacheBehaviors", []))]
    return {b["PathPattern"]: b for b in dist["Properties"]["DistributionConfig"]["CacheBehaviors"]}


def test_only_api_health_and_vendor_js_bypass_checkauth(oidc_on):
    """The unauthenticated set is EXACTLY /api/* (APIs keep their own 401),
    /health, and /js/vendor/* — public open-source library code the sandboxed
    chart iframe (opaque origin ⇒ no edge cookies) must be able to load.
    Anything else here would put app content in front of the edge gate."""
    behaviors = _app_distribution_behaviors(oidc_on["ADTComputeStack"])
    no_auth = {p for p, b in behaviors.items() if not b.get("LambdaFunctionAssociations")}
    assert no_auth == {"/api/*", "/health", "/js/vendor/*"}, sorted(no_auth)
