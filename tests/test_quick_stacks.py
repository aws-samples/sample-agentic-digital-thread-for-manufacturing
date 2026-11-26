# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Quick stacks are opt-in, IAM-only, and never internet-open.

Pins the quick_api_enabled / quick_connector_enabled contract:
  (a) with no context, NEITHER Quick stack is in the assembly (base deploys
      are byte-unaffected);
  (b) quick_api_enabled=true adds ONLY QuickApiGatewayStack, and that API has
      ZERO API keys / usage plans and AWS_IAM on EVERY method (the branch's
      /key/* fallback was deliberately removed at port time);
  (c) no Quick-only stack ever gets the retired kiosk stack;
  (d) the API stack's security groups have NO 0.0.0.0/0 (or ::/0) ingress;
  (e) quick_api_enabled=true adds the bridge-only `adt-quick-gw` gateway
      (CUSTOM_JWT, same authorizer as the shared gateway) and the
      QUICK_GATEWAY_ID/URL task env — base synth has exactly ONE gateway and
      none of that env (ADR-0071);
  (f) quick_connector_enabled=true (the connector-only path, ADR-0093) adds
      the same gateway and env with NO Quick stack, a per-account generated
      x-quick-auth token, the connector outputs, the adt-quick-client-secret
      secret and the bridge runtime's ingress to the internal ALB;
  (g) on the connector and --quick-api paths the app task carries
      QUICK_GATEWAY_TOKEN_REF (the ARN of the generated secret holding the
      x-quick-auth token, passed to the bridge runtime in place of the token)
      and the AgentCore runtime role may read exactly that ARN; a base deploy
      has neither, and no template holds the token value.

Synths are credential-blind (the workshop-packaging environment): account
tokens must stay tokens.
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

_API_STACK = "QuickApiGatewayStack"
_KIOSK_STACK = "QuickKioskStack"


def _synth_assembly(tmp_path_factory, *, context: dict[str, str],
                    output_slug: str) -> Path:
    """Credential-blind `cdk synth --all`; returns the cloud-assembly dir.

    The environment is scrubbed of AWS credentials and config: with
    no resolvable account, VPC lookups use dummy AZs and account references
    stay ${AWS::AccountId} tokens. cdk-nag stays ON: the quick stacks must
    synth clean under the documented suppressions.
    """
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp(output_slug)
    env = dict(os.environ)
    for key in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN", "CDK_DEFAULT_ACCOUNT", "CDK_DEFAULT_REGION"):
        env.pop(key, None)
    env.update({
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
    })
    args = [str(CDK_BIN), "synth", "--all", "--no-lookups",
            "-c", "region=us-east-1"]
    for key, value in context.items():
        args.extend(["-c", f"{key}={value}"])
    args.extend(["--output", str(out_dir), "--quiet"])
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        args,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        # A Python traceback from the CDK app is a bug in OUR code, not an
        # environment gap: fail it. Skipping it hid an app.py AttributeError
        # that broke every synth with the Quick OIDC option set (2026-10-02).
        if "Traceback (most recent call last)" in result.stderr:
            pytest.fail(f"cdk synth raised in the app:\n{result.stderr[-2000:]}")
        pytest.skip(f"cdk synth failed in this environment:\n{result.stderr[-2000:]}")
    return out_dir


def _stack_names(assembly: Path) -> set[str]:
    manifest = json.loads((assembly / "manifest.json").read_text())
    return {
        name
        for name, artifact in manifest.get("artifacts", {}).items()
        if artifact.get("type") == "aws:cloudformation:stack"
    }


def _template(assembly: Path, stack: str) -> dict:
    return json.loads((assembly / f"{stack}.template.json").read_text())


def _resources_of_type(template: dict, type_name: str) -> dict:
    return {
        logical_id: resource
        for logical_id, resource in template.get("Resources", {}).items()
        if resource.get("Type") == type_name
    }


@pytest.fixture(scope="session")
def base_assembly(tmp_path_factory) -> Path:
    return _synth_assembly(tmp_path_factory, context={},
                           output_slug="cdk_synth_quick_base")


@pytest.fixture(scope="session")
def api_assembly(tmp_path_factory) -> Path:
    return _synth_assembly(tmp_path_factory,
                           context={"quick_api_enabled": "true"},
                           output_slug="cdk_synth_quick_api")


def test_base_assembly_contains_no_quick_stacks(base_assembly):
    names = _stack_names(base_assembly)
    assert _API_STACK not in names
    assert _KIOSK_STACK not in names


def test_api_flag_adds_only_the_api_stack(api_assembly):
    names = _stack_names(api_assembly)
    assert _API_STACK in names
    assert _KIOSK_STACK not in names


def test_api_has_zero_api_keys_or_usage_plans(api_assembly):
    template = _template(api_assembly, _API_STACK)
    for type_name in ("AWS::ApiGateway::ApiKey", "AWS::ApiGateway::UsagePlan",
                      "AWS::ApiGateway::UsagePlanKey",
                      "AWS::SecretsManager::Secret"):
        assert _resources_of_type(template, type_name) == {}, (
            f"the removed API-key auth path leaked back in ({type_name})"
        )


def test_the_quick_api_stays_regional(api_assembly):
    # An EDGE endpoint would put CloudFront in front of the HTTP_PROXY
    # integration, so the injected x-quick-auth would arrive with X-Amz-Cf-Id
    # and the middleware (CLOUDFRONT_REQUEST_ID_HEADER) would refuse it.
    template = _template(api_assembly, _API_STACK)
    (api,) = _resources_of_type(template, "AWS::ApiGateway::RestApi").values()
    assert api["Properties"]["EndpointConfiguration"]["Types"] == ["REGIONAL"]


def test_the_api_resource_policy_admits_only_this_accounts_principals(api_assembly):
    template = _template(api_assembly, _API_STACK)
    (api,) = _resources_of_type(template, "AWS::ApiGateway::RestApi").values()
    (statement,) = api["Properties"]["Policy"]["Statement"]
    assert statement["Effect"] == "Allow" and statement["Principal"] == "*"
    assert statement["Condition"] == {
        "StringEquals": {"aws:PrincipalAccount": {"Ref": "AWS::AccountId"}}}


def test_every_api_method_requires_aws_iam(api_assembly):
    template = _template(api_assembly, _API_STACK)
    methods = _resources_of_type(template, "AWS::ApiGateway::Method")
    assert methods, "expected the /iam/* route methods"
    for logical_id, method in methods.items():
        props = method["Properties"]
        assert props.get("AuthorizationType") == "AWS_IAM", (
            f"{logical_id} is not AWS_IAM-authorized"
        )
        assert props.get("ApiKeyRequired") in (None, False)


def _open_world_ingress(template: dict) -> list[str]:
    findings = []
    for logical_id, sg in _resources_of_type(
            template, "AWS::EC2::SecurityGroup").items():
        for rule in sg["Properties"].get("SecurityGroupIngress", []) or []:
            if rule.get("CidrIp") == "0.0.0.0/0" or rule.get("CidrIpv6") == "::/0":
                findings.append(f"{logical_id}: {rule}")
    for logical_id, rule_res in _resources_of_type(
            template, "AWS::EC2::SecurityGroupIngress").items():
        props = rule_res["Properties"]
        if props.get("CidrIp") == "0.0.0.0/0" or props.get("CidrIpv6") == "::/0":
            findings.append(f"{logical_id}: {props}")
    return findings


def test_api_stack_has_no_internet_open_ingress(api_assembly):
    template = _template(api_assembly, _API_STACK)
    assert _open_world_ingress(template) == []


# --- Dedicated Quick gateway (ADR-0071) -------------------------------------
_AGENTCORE_STACK = "ADTAgentCoreStack"
_COMPUTE_STACK = "ADTComputeStack"
_GATEWAY_TYPE = "AWS::BedrockAgentCore::Gateway"


def _gateways_by_name(template: dict) -> dict[str, dict]:
    return {
        res["Properties"]["Name"]: res["Properties"]
        for res in _resources_of_type(template, _GATEWAY_TYPE).values()
    }


def _app_task_env(template: dict) -> dict[str, object]:
    """Env of the app container on the ONE ECS task definition."""
    task_defs = _resources_of_type(template, "AWS::ECS::TaskDefinition")
    assert len(task_defs) == 1, f"expected one task definition, got {list(task_defs)}"
    (task_def,) = task_defs.values()
    (container,) = task_def["Properties"]["ContainerDefinitions"]
    return {e["Name"]: e.get("Value") for e in container.get("Environment", [])}


def test_base_synth_has_exactly_one_gateway(base_assembly):
    gateways = _gateways_by_name(_template(base_assembly, _AGENTCORE_STACK))
    assert set(gateways) == {"adt-query-gw"}, (
        f"base deploy must carry ONLY the shared gateway, got {sorted(gateways)}"
    )


def test_quick_synth_adds_the_bridge_only_gateway(api_assembly):
    gateways = _gateways_by_name(_template(api_assembly, _AGENTCORE_STACK))
    assert set(gateways) == {"adt-query-gw", "adt-quick-gw"}, sorted(gateways)
    quick = gateways["adt-quick-gw"]
    shared = gateways["adt-query-gw"]
    assert quick["AuthorizerType"] == "CUSTOM_JWT"
    # Same issuer as the shared gateway and the same adt-app / adt-app-service
    # clients, plus ONE client only this gateway lists: Quick's own
    # adt-quick-service (the tests below pin which). The shared gateway
    # ADDITIONALLY allows the edge-auth client (edge browsers forward their
    # end-user bearer, ADR-0049/0070); Quick never holds an edge token, so its
    # gateway deliberately does NOT allow it (least privilege).
    q_jwt = quick["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    s_jwt = shared["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert q_jwt["DiscoveryUrl"] == s_jwt["DiscoveryUrl"]
    q_clients, s_clients = q_jwt["AllowedClients"], s_jwt["AllowedClients"]
    quick_only = [c for c in q_clients if c not in s_clients]
    assert len(quick_only) == 1, (q_clients, s_clients)
    assert len(s_clients) - (len(q_clients) - 1) in (0, 1), (q_clients, s_clients)
    assert quick["ProtocolType"] == "MCP"
    # No semantic search: it would list x_amz_bedrock_agentcore_search as a
    # fourth action next to the three bridge tools (ADR-0071 amendment).
    assert "SearchType" not in quick["ProtocolConfiguration"]["Mcp"]
    assert shared["ProtocolConfiguration"]["Mcp"]["SearchType"] == "SEMANTIC"


def test_quick_synth_exposes_quick_gateway_outputs(api_assembly, base_assembly):
    quick_outputs = _template(api_assembly, _AGENTCORE_STACK).get("Outputs", {})
    base_outputs = _template(base_assembly, _AGENTCORE_STACK).get("Outputs", {})
    assert {"QuickGatewayId", "QuickGatewayUrl"} <= set(quick_outputs)
    assert not {"QuickGatewayId", "QuickGatewayUrl"} & set(base_outputs)


def test_compute_task_env_carries_quick_gateway_only_when_enabled(
        api_assembly, base_assembly):
    quick_env = _app_task_env(_template(api_assembly, _COMPUTE_STACK))
    base_env = _app_task_env(_template(base_assembly, _COMPUTE_STACK))
    assert {"QUICK_API_ENABLED", "QUICK_GATEWAY_ID", "QUICK_GATEWAY_URL"} <= set(quick_env)
    # Cross-stack reference to the agentcore stack's gateway, not a literal.
    assert quick_env["QUICK_GATEWAY_ID"] != quick_env["AGENTCORE_GATEWAY_ID"]
    assert isinstance(quick_env["QUICK_GATEWAY_ID"], dict)  # Fn::ImportValue
    for key in ("QUICK_API_ENABLED", "QUICK_GATEWAY_ID", "QUICK_GATEWAY_URL"):
        assert key not in base_env, f"{key} leaked into the base task env"


# --- Connector-only Quick path (quick_connector_enabled, ADR-0093) ----------
@pytest.fixture(scope="session")
def connector_assembly(tmp_path_factory) -> Path:
    return _synth_assembly(tmp_path_factory,
                           context={"quick_connector_enabled": "true"},
                           output_slug="cdk_synth_quick_connector")


def _app_task_secrets(template: dict) -> dict[str, object]:
    (task_def,) = _resources_of_type(template, "AWS::ECS::TaskDefinition").values()
    (container,) = task_def["Properties"]["ContainerDefinitions"]
    return {s["Name"]: s["ValueFrom"] for s in container.get("Secrets", [])}


def _alb_ingress_from(template: dict, source_prefix: str) -> list[dict]:
    rules = [r["Properties"] for r in _resources_of_type(
        template, "AWS::EC2::SecurityGroupIngress").values()]
    return [r for r in rules
            if r.get("GroupId", {}).get("Fn::GetAtt", [""])[0].startswith("AdtAlbSg")
            and str(r.get("SourceSecurityGroupId", {}).get("Fn::GetAtt", [""])[0]).startswith(source_prefix)]


def test_connector_flag_adds_no_quick_stack(connector_assembly):
    names = _stack_names(connector_assembly)
    assert _API_STACK not in names and _KIOSK_STACK not in names


def test_connector_flag_adds_the_bridge_only_gateway(connector_assembly):
    gateways = _gateways_by_name(_template(connector_assembly, _AGENTCORE_STACK))
    assert set(gateways) == {"adt-query-gw", "adt-quick-gw"}, sorted(gateways)
    assert gateways["adt-quick-gw"]["AuthorizerType"] == "CUSTOM_JWT"


def test_connector_values_are_outputs_and_one_named_secret(connector_assembly, api_assembly,
                                                           base_assembly):
    template = _template(connector_assembly, _AGENTCORE_STACK)
    outputs = template["Outputs"]
    assert {"QuickGatewayId", "QuickGatewayUrl", "QuickTokenUrl", "QuickClientSecretName",
            "AppServiceClientId"} <= set(outputs)
    token_url = json.dumps(outputs["QuickTokenUrl"]["Value"])
    assert "AdtAppDomain" in token_url and "/oauth2/token" in token_url
    secrets = [r["Properties"] for r in _resources_of_type(
        template, "AWS::SecretsManager::Secret").values()]
    (secret,) = [s for s in secrets if s.get("Name") == "adt-quick-client-secret"]
    assert "Fn::GetAtt" in secret["SecretString"]  # the client's secret, never a literal
    for other in (api_assembly, base_assembly):
        assert "QuickTokenUrl" not in _template(other, _AGENTCORE_STACK).get("Outputs", {})


# --- Quick holds its OWN Cognito client, accepted only by adt-quick-gw ------
# Whoever holds the Quick connector secret (every workshop attendee, any Quick
# admin) must reach the three bridge tools and nothing else. So the secret is
# the dedicated adt-quick-service client's, and only adt-quick-gw lists that
# client: adt-query-gw (every system target) and adt-onboard-gw refuse it.
_QUICK_CLIENT_NAME = "adt-quick-service"
#: The app's service client (the retired adt-app-service is pinned in
#: tests/test_app_service_client_v2.py).
_SERVICE_CLIENT_NAME = "adt-app-service-v2"
_ONBOARDING_STACK = "ADTOnboardingStack"


def _client_logical_id(template: dict, name: str) -> str:
    (logical_id,) = [lid for lid, res in _resources_of_type(
        template, "AWS::Cognito::UserPoolClient").items()
        if res["Properties"].get("ClientName") == name]
    return logical_id


def _allowed_clients(template: dict, gateway_name: str) -> list:
    gateway = _gateways_by_name(template)[gateway_name]
    return gateway["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedClients"]


def _connector_secret(template: dict) -> dict:
    secrets = [r["Properties"] for r in _resources_of_type(
        template, "AWS::SecretsManager::Secret").values()]
    (secret,) = [s for s in secrets if s.get("Name") == "adt-quick-client-secret"]
    return secret


def _connector_secret_client(template: dict) -> str:
    """Logical id of the user-pool client whose secret adt-quick-client-secret
    copies. CDK reads a client secret through its DescribeCognitoUserPoolClient
    custom resource, whose call names the client by Ref."""
    reader, attribute = _connector_secret(template)["SecretString"]["Fn::GetAtt"]
    assert attribute == "UserPoolClient.ClientSecret"
    call = template["Resources"][reader]["Properties"]["Create"]["Fn::Join"][1]
    (client,) = [call[i + 1]["Ref"] for i, part in enumerate(call[:-1])
                 if isinstance(part, str) and part.endswith('"ClientId":"')]
    return client


@pytest.mark.parametrize("assembly_name", ["api_assembly", "connector_assembly"])
def test_quick_client_is_accepted_only_by_the_quick_gateway(assembly_name, request):
    assembly = request.getfixturevalue(assembly_name)
    template = _template(assembly, _AGENTCORE_STACK)
    quick = _client_logical_id(template, _QUICK_CLIENT_NAME)
    service = _client_logical_id(template, _SERVICE_CLIENT_NAME)
    assert {"Ref": quick} in _allowed_clients(template, "adt-quick-gw")
    assert {"Ref": quick} not in _allowed_clients(template, "adt-query-gw")
    # The app's own service client keeps its gateways (background turns).
    assert {"Ref": service} in _allowed_clients(template, "adt-query-gw")
    # The onboarding gateway imports its clients from this stack by export
    # name, which carries the client's logical id.
    onboarding = _gateways_by_name(_template(assembly, _ONBOARDING_STACK))["adt-onboard-gw"]
    onboarding_clients = json.dumps(
        onboarding["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedClients"])
    assert quick not in onboarding_clients
    assert service in onboarding_clients


def test_the_client_behind_the_connector_secret_cannot_reach_system_targets(
        connector_assembly):
    """The exposure itself, independent of client names: the client whose
    secret attendees read from adt-quick-client-secret works on adt-quick-gw
    and nowhere else."""
    template = _template(connector_assembly, _AGENTCORE_STACK)
    holder = _connector_secret_client(template)
    assert {"Ref": holder} in _allowed_clients(template, "adt-quick-gw")
    assert {"Ref": holder} not in _allowed_clients(template, "adt-query-gw"), (
        "the Quick connector secret's client is accepted by the shared system-target gateway")
    onboarding = _gateways_by_name(
        _template(connector_assembly, _ONBOARDING_STACK))["adt-onboard-gw"]
    assert holder not in json.dumps(
        onboarding["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedClients"])


def test_quick_client_is_client_credentials_only_with_its_own_scope(api_assembly):
    template = _template(api_assembly, _AGENTCORE_STACK)
    clients = _resources_of_type(template, "AWS::Cognito::UserPoolClient")
    props = clients[_client_logical_id(template, _QUICK_CLIENT_NAME)]["Properties"]
    assert props["GenerateSecret"] is True
    assert props["AllowedOAuthFlows"] == ["client_credentials"]
    assert props["AllowedOAuthFlowsUserPoolClient"] is True
    # No user sign-in through this client: only refresh is left, which a
    # client_credentials client never uses.
    assert props["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]
    assert "CallbackURLs" not in props
    # One scope, <its own resource server>/bridge; never the app's adt/* scopes.
    (scope,) = props["AllowedOAuthScopes"]
    server_ref, suffix = scope["Fn::Join"][1]
    assert suffix == "/bridge"
    servers = _resources_of_type(template, "AWS::Cognito::UserPoolResourceServer")
    server = servers[server_ref["Ref"]]["Properties"]
    assert server["Identifier"] == "adt-quick"
    assert [s["ScopeName"] for s in server["Scopes"]] == ["bridge"]


def test_quick_connector_secret_and_client_id_are_the_dedicated_client(connector_assembly):
    template = _template(connector_assembly, _AGENTCORE_STACK)
    quick = _client_logical_id(template, _QUICK_CLIENT_NAME)
    assert _connector_secret_client(template) == quick
    assert _SERVICE_CLIENT_NAME not in _connector_secret(template)["Description"]
    assert template["Outputs"]["QuickClientId"]["Value"] == {"Ref": quick}


def test_base_synth_has_no_quick_client(base_assembly):
    template = _template(base_assembly, _AGENTCORE_STACK)
    names = {r["Properties"].get("ClientName") for r in _resources_of_type(
        template, "AWS::Cognito::UserPoolClient").values()}
    assert _QUICK_CLIENT_NAME not in names and _SERVICE_CLIENT_NAME in names
    assert "QuickClientId" not in template.get("Outputs", {})
    identifiers = {r["Properties"]["Identifier"] for r in _resources_of_type(
        template, "AWS::Cognito::UserPoolResourceServer").values()}
    assert "adt-quick" not in identifiers


def test_connector_task_env_and_per_account_token(connector_assembly):
    template = _template(connector_assembly, _COMPUTE_STACK)
    env = _app_task_env(template)
    assert env["QUICK_API_ENABLED"] == "true"
    assert isinstance(env["QUICK_GATEWAY_ID"], dict)  # Fn::ImportValue
    secrets = _app_task_secrets(template)
    # Generated per account in Secrets Manager; never the kiosk's SSM pair.
    token = secrets["QUICK_GATEWAY_TOKEN"]
    assert "Ref" in token and "Ssm" not in json.dumps(token)
    assert "QUICK_DESKTOP_URL" not in secrets
    (generated,) = [r["Properties"] for lid, r in _resources_of_type(
        template, "AWS::SecretsManager::Secret").items() if lid.startswith("QuickGatewayToken")]
    assert "GenerateSecretString" in generated and "Name" not in generated


def test_bridge_runtime_may_reach_the_internal_alb_only_under_the_connector(
        connector_assembly, api_assembly, base_assembly):
    assert len(_alb_ingress_from(_template(connector_assembly, _COMPUTE_STACK),
                                 "AgentCoreRuntimeSg")) == 1
    for other in (api_assembly, base_assembly):
        assert _alb_ingress_from(_template(other, _COMPUTE_STACK), "AgentCoreRuntimeSg") == []


def test_no_identity_key_read_flag_or_grant_without_delegated_tokens(
        connector_assembly, api_assembly, base_assembly):
    """The workshop packages the connector path (quick_connector_enabled) and
    never the delegated-token mode, so its app task has no read on the
    identity-claim key and no IDENTITY_CLAIM_KEY_READABLE flag: the app makes
    no key read there (each one was AccessDenied on the 2026-10-20 walk)."""
    for assembly in (connector_assembly, api_assembly, base_assembly):
        template = _template(assembly, _COMPUTE_STACK)
        assert "IDENTITY_CLAIM_KEY_READABLE" not in _app_task_env(template)
        assert "identity-claim-key" not in json.dumps(template)


# --- The bridge reads the x-quick-auth token BY REFERENCE (S1b) ------------
# The app task carries QUICK_GATEWAY_TOKEN_REF (an ARN) and passes it to the
# query-bridge runtime instead of the token; the AgentCore runtime role may
# read exactly that ARN. The connector and --quick-api paths; never a base
# deploy.
_TOKEN_REF = "QUICK_GATEWAY_TOKEN_REF"  # nosec B105 - an env var name, not a value
_TOKEN_READ_ACTIONS = ("secretsmanager:GetSecretValue", "ssm:GetParameter")


def _token_read_policies(template: dict) -> list[dict]:
    """Standalone IAM policies on the AgentCore runtime role (an import from
    ADTAgentCoreStack) whose statements carry a token-read action. The task
    execution role's own secret/parameter reads are not in scope."""
    found = []
    for res in _resources_of_type(template, "AWS::IAM::Policy").values():
        if "RuntimeRole" not in json.dumps(res["Properties"].get("Roles", [])):
            continue
        statements = res["Properties"]["PolicyDocument"]["Statement"]
        actions = {a for s in statements
                   for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
        if actions & set(_TOKEN_READ_ACTIONS):
            found.append(res["Properties"])
    return found


def _assert_one_exact_grant(template: dict, action: str, resource) -> None:
    (policy,) = _token_read_policies(template)
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == action
    assert statement["Resource"] == resource  # exactly the referenced ARN
    assert "*" not in json.dumps(statement["Resource"])
    (role,) = policy["Roles"]
    assert "RuntimeRole" in json.dumps(role)  # the AgentCore runtime role, imported
    assert "Fn::ImportValue" in role


def test_connector_bridge_gets_the_secret_arn_and_a_grant_on_it_only(connector_assembly):
    template = _template(connector_assembly, _COMPUTE_STACK)
    env = _app_task_env(template)
    secret_ref = _app_task_secrets(template)["QUICK_GATEWAY_TOKEN"]
    # The plain env value IS the generated secret's ARN (Ref), not a value.
    assert env[_TOKEN_REF] == secret_ref
    assert list(env[_TOKEN_REF]) == ["Ref"]
    assert env[_TOKEN_REF]["Ref"].startswith("QuickGatewayTokenSecret")
    _assert_one_exact_grant(template, "secretsmanager:GetSecretValue", secret_ref)


def test_quick_api_bridge_gets_the_secret_arn_and_a_grant_on_it_only(api_assembly):
    # On --quick-api the token is the same per-account generated secret as on
    # the connector path, so the bridge reads it by reference there too.
    template = _template(api_assembly, _COMPUTE_STACK)
    env = _app_task_env(template)
    secret_ref = _app_task_secrets(template)["QUICK_GATEWAY_TOKEN"]
    assert env[_TOKEN_REF] == secret_ref
    assert env[_TOKEN_REF]["Ref"].startswith("QuickGatewayTokenSecret")
    _assert_one_exact_grant(template, "secretsmanager:GetSecretValue", secret_ref)


def test_the_stage_serves_a_deployment_made_after_the_token_moved(api_assembly):
    """The V6 deployment snapshot holds the old literal token header; an
    existing stage keeps serving it unless a new deployment replaces it."""
    api = _template(api_assembly, _API_STACK)
    (deployment_id,) = _resources_of_type(api, "AWS::ApiGateway::Deployment")
    assert deployment_id.startswith("QuickDeploymentV8")
    (stage,) = _resources_of_type(api, "AWS::ApiGateway::Stage").values()
    assert stage["Properties"]["DeploymentId"] == {"Ref": deployment_id}


def test_quick_api_token_is_a_generated_secret_never_a_template_literal(api_assembly):
    """The x-quick-auth token is generated by Secrets Manager in the compute
    stack. The API stack injects it through a secretsmanager dynamic
    reference, so neither template holds the value, and there is no SSM
    parameter copy of it."""
    compute = _template(api_assembly, _COMPUTE_STACK)
    api = _template(api_assembly, _API_STACK)
    (generated,) = [r["Properties"] for lid, r in _resources_of_type(
        compute, "AWS::SecretsManager::Secret").items() if lid.startswith("QuickGatewayToken")]
    assert "GenerateSecretString" in generated and "SecretString" not in generated
    assert "Ssm" not in json.dumps(_app_task_secrets(compute)["QUICK_GATEWAY_TOKEN"])
    for template in (compute, api):
        assert not [r for r in _resources_of_type(template, "AWS::SSM::Parameter").values()
                    if "quick-gateway-token" in json.dumps(r)]
    headers = [m["Properties"]["Integration"]["RequestParameters"]["integration.request.header.x-quick-auth"]
               for m in _resources_of_type(api, "AWS::ApiGateway::Method").values()
               if "x-quick-auth" in json.dumps(m)]
    assert headers
    for header in headers:
        joined = json.dumps(header)
        assert "Fn::Join" in joined and "{{resolve:secretsmanager:" in joined
        assert "Fn::ImportValue" in joined  # the compute stack's secret ARN
        # Exactly a quoted static header value around the dynamic reference:
        # the only literal text is the reference syntax, never a token.
        sep, parts = header["Fn::Join"]
        assert sep == ""
        literals = [p for p in parts if isinstance(p, str)]
        assert literals == ["'{{resolve:secretsmanager:", ":SecretString:::}}'"], literals
        (arn,) = [p for p in parts if not isinstance(p, str)]
        assert list(arn) == ["Fn::ImportValue"]


def test_base_deploy_has_no_token_reference_and_no_token_grant(base_assembly):
    template = _template(base_assembly, _COMPUTE_STACK)
    assert _TOKEN_REF not in _app_task_env(template)
    assert _token_read_policies(template) == []


def test_the_vpc_link_may_reach_the_internal_alb_on_port_80_only(api_assembly):
    """API Gateway reaches the app through the VPC link, so the internal ALB's
    security group admits the VPC link's group, on the listener port only.
    Without the rule every Quick API call ended in a 500."""
    template = _template(api_assembly, _API_STACK)
    rules = [r["Properties"] for r in _resources_of_type(
        template, "AWS::EC2::SecurityGroupIngress").values()]
    (rule,) = rules
    assert rule["SourceSecurityGroupId"]["Fn::GetAtt"][0].startswith("QuickVpcLinkSg")
    assert (rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) == ("tcp", 80, 80)
    assert "AdtAlbSg" in json.dumps(rule["GroupId"])


def _quick_gate() -> dict[str, object]:
    """The app gate's x-quick-auth allowlists, read from the source (no import)."""
    import ast

    source = (Path(__file__).resolve().parents[1] / "agentic_digital_thread"
              / "agents" / "auth" / "middleware.py").read_text(encoding="utf-8")
    (cls,) = [n for n in ast.parse(source).body
              if isinstance(n, ast.ClassDef) and n.name == "AuthMiddleware"]
    return {t.id: ast.literal_eval(n.value) for n in cls.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name) and t.id.startswith("QUICK_GATEWAY_")}


def _gate_admits(gate: dict, method: str, path: str) -> bool:
    """Mirror of AuthMiddleware._quick_gateway_request's path check."""
    return (path in gate["QUICK_GATEWAY_ROUTES"]
            or any(path.startswith(p) for p in gate["QUICK_GATEWAY_PREFIXES"])
            or (method == "GET" and path in gate["QUICK_GATEWAY_GET_ROUTES"]))


def test_every_quick_api_route_is_one_the_app_admits(api_assembly):
    """API Gateway authenticates with x-quick-auth, which the app accepts only
    on its allowlisted paths. A route to any other path answers 401 to every
    signed call, so the API serves exactly the kiosk's submit and result pair."""
    gate = _quick_gate()
    template = _template(api_assembly, _API_STACK)
    routes = set()
    for method in _resources_of_type(template, "AWS::ApiGateway::Method").values():
        props = method["Properties"]
        uri = json.dumps(props["Integration"]["Uri"])
        path = "/api/" + uri.split("/api/", 1)[1].split('"', 1)[0]
        routes.add((props["HttpMethod"], path))
    assert routes == {("POST", "/api/ask/async"), ("GET", "/api/ask/result/{job_id}")}, routes
    for http_method, path in routes:
        assert _gate_admits(gate, http_method, path), (http_method, path)
