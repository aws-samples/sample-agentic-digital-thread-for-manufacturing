# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression tests for the onboarding control-plane stack (Wave 2).

Guards the offline-synthesizable parts of onboarding Wave 2 (refresh) after the
IN-PLACE (P1b) route: the onboarding agent runtime + its gateway target are
created **imperatively by deploy.sh** (mirroring the orchestrator, ADR-0010), NOT
declaratively, so the branch deploys in-place with AZ-strip on the existing VPC.
This suite therefore asserts the declarative runtime/endpoint/runtime-target are
ABSENT while the rest of the control plane remains declarative:

* **2.1R** — NO ``BedrockAgentCore::Runtime`` and NO ``RuntimeEndpoint`` in the
  template (created imperatively by deploy.sh §2e). A least-privileged onboarding
  **runtime role** IS present (its ARN is a stack output consumed as
  ``--role-arn``): DDB-CRUD on the session table, ``conn/*`` + ``systems/*``
  ``GetSecretValue``, the graph read tier and the SiteWise reads its exported
  tools make, and **NO** secret write, graph write, ``states:StartExecution``,
  ``bedrock-agentcore:Create*`` / ``iam:PassRole`` or ``agent-config/*`` access.
* **2.2R** — the onboarding **gateway** is ``CUSTOM_JWT`` (not ``AWS_IAM``, not
  semantic) with the ``adt-app`` discovery URL + **both** allowed clients, and
  **NO** Cedar ``PolicyEngine`` or ``Policy`` attached (deferred until a target exists).
  There is **NO** declarative ``GatewayTarget`` — the single onboarding target is
  created imperatively by deploy.sh (needs the runtime MCP URL, unknown at synth),
  exactly like the query gateway's agent_manager-created targets.
* The Step Functions provisioner (state machine, its Lambda, image and roles) is
  ABSENT: it had no caller and was removed on 2026-10-29. Provisioning runs in
  the app.

Synthesized once offline (no AWS calls); requires the vendored CDK CLI + Node
under node_modules — skips if unavailable (mirrors tests/test_highbyte_stack.py /
tests/test_agentcore_app_cognito.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
STACK = "ADTOnboardingStack"


def _resolve_cdk() -> str | None:
    """Prefer the vendored CDK CLI; fall back to a `cdk` on PATH."""
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


@pytest.fixture(scope="session")
def onboarding_template(onboarding_synth) -> dict:
    return onboarding_synth[0]


@pytest.fixture(scope="session")
def onboarding_assets(onboarding_synth) -> dict:
    """The synthesized ``<Stack>.assets.json``: what ``cdk deploy`` builds."""
    return onboarding_synth[1]


@pytest.fixture(scope="session")
def onboarding_synth(tmp_path_factory) -> tuple[dict, dict]:
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")

    out_dir = tmp_path_factory.mktemp("cdk_synth_onboarding")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [
            cdk_bin, "synth", STACK,
            "-c", "region=us-east-1",
            # Pin the OFF path: this suite is the edge-auth-off baseline for
            # the onboarding gateway (exactly TWO AllowedClients). Without the
            # pin, default resolution (ON except --workshop, plus ambient
            # sticky SSM) would add the edge client (ADR-0070 amendment
            # 2026-09-23); the edge-on 3-client shape is pinned in
            # tests/test_edge_external_oidc.py.
            "-c", "edge_auth=false",
            "--output", str(out_dir),
            "--quiet",
        ],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=420,
    )
    template_path = out_dir / f"{STACK}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    assets_path = out_dir / f"{STACK}.assets.json"
    return json.loads(template_path.read_text()), json.loads(assets_path.read_text())


def _resources(template: dict, cfn_type: str) -> dict:
    return {
        lid: r for lid, r in template.get("Resources", {}).items()
        if r.get("Type") == cfn_type
    }


def _policy_statements(template: dict, role_lid: str) -> list:
    """All policy statements attached to a role (inline policies + AWS::IAM::Policy)."""
    statements = []
    role = template["Resources"][role_lid]
    for pol in role["Properties"].get("Policies", []):
        statements += pol["PolicyDocument"]["Statement"]
    # Standalone AWS::IAM::Policy resources referencing this role.
    for r in _resources(template, "AWS::IAM::Policy").values():
        roles_json = json.dumps(r["Properties"].get("Roles", []))
        if role_lid in roles_json:
            statements += r["Properties"]["PolicyDocument"]["Statement"]
    return statements


def _all_actions(statements: list) -> set:
    actions: set = set()
    for s in statements:
        a = s.get("Action", [])
        actions |= set(a if isinstance(a, list) else [a])
    return actions


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# --------------------------------------------------------------------------- #
# 2.1R — runtime is IMPERATIVE (deploy.sh), NOT declarative
# --------------------------------------------------------------------------- #
def test_no_declarative_runtime(onboarding_template):
    # P1b (in-place route): the onboarding runtime is created imperatively by
    # deploy.sh §2e (AZ-strip), NOT as a declarative CfnRuntime pinned to all
    # private subnets (which CREATE_FAILEDs in unsupported AZs). The template must
    # contain NO Runtime.
    runtimes = _resources(onboarding_template, "AWS::BedrockAgentCore::Runtime")
    assert not runtimes, f"expected NO declarative Runtime (created imperatively by deploy.sh), found {list(runtimes)}"


def test_no_declarative_runtime_endpoint(onboarding_template):
    endpoints = _resources(onboarding_template, "AWS::BedrockAgentCore::RuntimeEndpoint")
    assert not endpoints, f"expected NO declarative RuntimeEndpoint (imperative), found {list(endpoints)}"


# --------------------------------------------------------------------------- #
# 2.1R — least-privileged runtime role
# --------------------------------------------------------------------------- #
def _runtime_role_lid(template: dict) -> str:
    roles = _resources(template, "AWS::IAM::Role")
    for lid, r in roles.items():
        if "OnboardingRuntimeRole" in lid:
            return lid
    raise AssertionError(f"OnboardingRuntimeRole not found among {list(roles)}")


def test_runtime_role_has_no_create_or_passrole(onboarding_template):
    lid = _runtime_role_lid(onboarding_template)
    actions = _all_actions(_policy_statements(onboarding_template, lid))
    create_like = {a for a in actions if a.startswith("bedrock-agentcore:Create")}
    assert not create_like, f"runtime role must NOT carry bedrock-agentcore:Create*: {create_like}"
    assert "iam:PassRole" not in actions, "runtime role must NOT carry iam:PassRole"


# The runtime role's grants, derived from what its eight exported tools call.
#
# A static call inventory of the onboarding runtime's exported tools (the
# onboarding gateway's whole surface; test_onboarding_runtime_tools_list.py
# pins the set) over agents/, following imports, re-exports, module dispatch
# tables and self/instance method calls:
#
#   get_system_info       catalog read (graph read on a miss)
#   test_connection       connection_probes.probe_connection: secret read of the
#                         node's pointer; SiteWise ListAssetModels; Athena
#                         GetWorkGroup; sts:AssumeRole for a cross-account role
#   discover              secret reads; SiteWise ListAssetModels,
#                         DescribeAssetModel, ListAssetModelProperties,
#                         ListAssets, DescribeAsset, GetAssetPropertyValue;
#                         Athena queries; session table GetItem/PutItem/UpdateItem
#   assess_vocabulary_fit graph read, session read
#   propose_concepts      graph read
#   map_fields            graph read, Bedrock (Converse, or Mantle for a GPT-5.x
#                         model), session read/update, the saved system-agent
#                         model (SSM /adt/llm-agent-model)
#   resolve_identities    graph read, Bedrock (Converse, or Mantle for a GPT-5.x
#                         model), session read/update, the saved system-agent
#                         model (SSM /adt/llm-agent-model)
#   verify_system         graph read, source probe, session read
#
# None of them reaches a graph write (no neptune_write / write_connection; every
# Cypher it runs is a MATCH ... RETURN), a Secrets Manager create, put,
# describe, restore or delete, or states:StartExecution (provisioning runs in
# the app, and the stack has no state machine).
EXPORTED_TOOLS = {"get_system_info", "test_connection", "discover", "assess_vocabulary_fit",
                  "propose_concepts", "map_fields", "resolve_identities", "verify_system"}

#: The same-account SiteWise reads test_connection and discover make
#: (connection_probes._probe_sitewise, sitewise_read via
#: onboard_agent._discover_sitewise_direct_model and _sitewise_model_samples).
RUNTIME_SITEWISE_READS = {
    "iotsitewise:ListAssetModels", "iotsitewise:DescribeAssetModel",
    "iotsitewise:ListAssetModelProperties", "iotsitewise:ListAssets",
    "iotsitewise:DescribeAsset", "iotsitewise:GetAssetPropertyValue",
}


def test_runtime_role_grants_the_sitewise_reads_its_exported_tools_make(onboarding_template):
    """A same-account SiteWise (Direct) system is read with the runtime's own
    credentials: without these, test_connection is AccessDenied and discover
    falls back to a static schema instead of the real models."""
    lid = _runtime_role_lid(onboarding_template)
    actions = _all_actions(_policy_statements(onboarding_template, lid))
    assert RUNTIME_SITEWISE_READS <= actions, RUNTIME_SITEWISE_READS - actions
    sitewise = {a for a in actions if a.startswith("iotsitewise:")}
    assert sitewise == RUNTIME_SITEWISE_READS, sitewise - RUNTIME_SITEWISE_READS


def test_runtime_role_reads_the_connection_secrets_its_tools_resolve(onboarding_template):
    """discover and test_connection read the secret a System node points at:
    a registration's adt/conn/<id>, a system's adt/systems/<id>, or a demo
    database's owner secret."""
    lid = _runtime_role_lid(onboarding_template)
    reads = [r for s in _policy_statements(onboarding_template, lid)
             if "secretsmanager:GetSecretValue" in _as_list(s.get("Action"))
             for r in _as_list(s.get("Resource"))]
    blob = json.dumps(reads)
    for prefix in ("adt/conn/*", "adt/systems/*", "AdtDbSecret*", "DatabaseSecret*"):
        assert f":secret:{prefix}" in blob, (prefix, blob)
    assert "adt/agent-config/" not in blob, (
        "runtime role must NOT read agent-config/* (provisioning runs in the app)")


def test_runtime_role_carries_no_write_its_exported_tools_do_not_make(onboarding_template):
    """No exported tool writes or deletes a secret, writes or deletes in the
    graph, or starts a state machine, so the role can do none of them: an
    explore target any app token can still call on the gateway."""
    lid = _runtime_role_lid(onboarding_template)
    statements = _policy_statements(onboarding_template, lid)
    actions = _all_actions(statements)
    secret_actions = {a for a in actions if a.startswith("secretsmanager:")}
    assert secret_actions == {"secretsmanager:GetSecretValue"}, secret_actions
    assert not ({"neptune-graph:WriteDataViaQuery", "neptune-graph:DeleteDataViaQuery"}
                & actions), actions
    assert not any(a.startswith("states:") for a in actions), actions


def test_runtime_role_has_the_graph_read_tier_only(onboarding_template):
    """ADR-0069: the exported tools read the graph (catalog, vocabulary,
    identity evidence), through the ONE stacks/_graph_access.py helper's read
    tier, scoped to account/Region-bounded graph/* (no stack edge to
    ADTDatabaseStack)."""
    lid = _runtime_role_lid(onboarding_template)
    statements = _policy_statements(onboarding_template, lid)
    graph = {a for a in _all_actions(statements) if a.startswith("neptune-graph:")}
    assert graph == {"neptune-graph:ExecuteQuery", "neptune-graph:ReadDataViaQuery",
                     "neptune-graph:GetGraph", "neptune-graph:ListGraphs"}, graph
    assert ":graph/*" in json.dumps(statements), "graph statement must scope to graph/*"


def test_runtime_role_has_session_ddb(onboarding_template):
    lid = _runtime_role_lid(onboarding_template)
    actions = _all_actions(_policy_statements(onboarding_template, lid))
    # discover creates the session; map_fields and resolve_identities update it.
    assert {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"} <= actions, actions


def test_runtime_role_reads_only_the_saved_agent_model_parameter(onboarding_template):
    """map_fields and resolve_identities try the configured system-agent model
    first (db.bedrock_call, model_config.configured_agent_model), which reads
    the Admin > Settings save from SSM; the role may read exactly that one
    parameter and write none."""
    lid = _runtime_role_lid(onboarding_template)
    statements = _policy_statements(onboarding_template, lid)
    ssm = [s for s in statements
           if any(str(a).startswith("ssm:") for a in _as_list(s.get("Action")))]
    assert len(ssm) == 1, ssm
    assert _as_list(ssm[0]["Action"]) == ["ssm:GetParameter"], ssm[0]
    blob = json.dumps(ssm[0]["Resource"])
    assert "parameter/adt/llm-agent-model" in blob and "*" not in blob.replace(
        "${AWS::", ""), blob


def test_runtime_role_may_run_a_mantle_model(onboarding_template):
    """map_fields and resolve_identities reach db.bedrock_call, which runs a
    GPT-5.x system-agent or orchestrator model on Bedrock Mantle (ADR-0055).
    The role carries the Mantle grant the app task role and the query runtime
    role carry (stacks/_bedrock_access.mantle_inference_statements):
    CreateInference on this account's Mantle projects, and the bearer-token
    call, which has no resource type, on '*'."""
    lid = _runtime_role_lid(onboarding_template)
    by_action = {a: s["Resource"] for s in _policy_statements(onboarding_template, lid)
                 for a in _as_list(s.get("Action")) if str(a).startswith("bedrock-mantle:")}
    assert set(by_action) == {"bedrock-mantle:CreateInference",
                              "bedrock-mantle:CallWithBearerToken"}, by_action
    assert by_action["bedrock-mantle:CallWithBearerToken"] == "*"
    scoped = json.dumps(by_action["bedrock-mantle:CreateInference"])
    assert ":bedrock-mantle:*:" in scoped and ":project/*" in scoped, scoped
    assert "AWS::AccountId" in scoped, scoped


# --------------------------------------------------------------------------- #
# 2.2R — gateway CUSTOM_JWT, no Cedar policy engine attached, single target
# --------------------------------------------------------------------------- #
def _gateway(onboarding_template) -> dict:
    gws = _resources(onboarding_template, "AWS::BedrockAgentCore::Gateway")
    assert len(gws) == 1, f"expected exactly one onboarding Gateway, found {len(gws)}"
    return next(iter(gws.values()))


def test_gateway_is_custom_jwt_not_iam(onboarding_template):
    props = _gateway(onboarding_template)["Properties"]
    assert props.get("AuthorizerType") == "CUSTOM_JWT", props.get("AuthorizerType")


def test_gateway_jwt_discovery_and_both_clients(onboarding_template):
    props = _gateway(onboarding_template)["Properties"]
    jwt = props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    # DiscoveryUrl is a cross-stack ref to the adt-app pool's discovery URL.
    blob = json.dumps(jwt.get("DiscoveryUrl"))
    assert ".well-known/openid-configuration" in blob, jwt.get("DiscoveryUrl")
    allowed = jwt.get("AllowedClients", [])
    # Both the login client and the service client must be present (two refs).
    assert len(allowed) == 2, f"expected both adt-app client ids in AllowedClients: {allowed}"


def test_gateway_search_type_not_semantic(onboarding_template):
    props = _gateway(onboarding_template)["Properties"]
    mcp = props.get("ProtocolConfiguration", {}).get("Mcp", {})
    assert mcp.get("SearchType") != "SEMANTIC", (
        "onboarding gateway must NOT use SEMANTIC search (small fixed toolset)"
    )


def test_cedar_is_deliberately_not_attached_in_stack(onboarding_template):
    """Cedar is DEFERRED, not forgotten — pin the deferral so it cannot drift.

    This test previously asserted the OPPOSITE (one PolicyEngine + one Policy
    attached in LOG_ONLY) and had been failing on ``main``. It was not a
    regression: the stack now documents, at length, why a declarative in-stack
    Cedar layer is impossible here. AgentCore rejects a permit-all baseline as
    "Overly Permissive" and rejects a specific forbid as an "unrecognized
    action", because the gateway's tool action ids only exist AFTER its target
    is registered — and the target is registered IMPERATIVELY by deploy.sh
    (§2e), which runs AFTER ``cdk deploy``.

    So the contract this pins is the real one: the synthesized template carries
    NO policy engine and NO policy, while the Cedar sources remain in the repo
    UNATTACHED, ready to be created and flipped to ENFORCE against the real
    gateway ARN and action ids after target sync. If someone later attaches a
    declarative engine, this fails and sends them to the stack's rationale
    instead of letting a known-broken deployment path look tested.
    """
    engines = _resources(onboarding_template, "AWS::BedrockAgentCore::PolicyEngine")
    policies = _resources(onboarding_template, "AWS::BedrockAgentCore::Policy")
    assert not engines, (
        "onboarding stack must carry NO declarative Cedar PolicyEngine "
        f"(pre-target-registration attachment fails); found {list(engines)}"
    )
    assert not policies, (
        "onboarding stack must carry NO declarative Cedar Policy; "
        f"found {list(policies)}"
    )
    gprops = _gateway(onboarding_template)["Properties"]
    assert "PolicyEngineConfiguration" not in gprops, (
        "onboarding gateway must not reference a policy engine while Cedar is "
        f"deferred; found {gprops.get('PolicyEngineConfiguration')!r}"
    )
    # The deferral is only honest if the policy SOURCES still exist to attach.
    repo_root = Path(__file__).resolve().parents[1]
    for name in ("onboarding_gateway.cedar", "onboarding_gateway_baseline.cedar"):
        assert (repo_root / "policies" / name).is_file(), (
            f"policies/{name} must remain in the repo: the deferral promises "
            "these sources are attached after target sync"
        )


def test_no_declarative_gateway_target(onboarding_template):
    # P1b (in-place route): the single onboarding gateway target is created
    # imperatively by deploy.sh §2e (it needs the runtime MCP URL, unknown at
    # synth — exactly like the query gateway's agent_manager-created targets). The
    # template must contain NO declarative GatewayTarget.
    targets = _resources(onboarding_template, "AWS::BedrockAgentCore::GatewayTarget")
    assert not targets, (
        f"expected NO declarative GatewayTarget (created imperatively by deploy.sh "
        f"after the runtime is READY), found {list(targets)}"
    )


# --------------------------------------------------------------------------- #
# The Step Functions provisioner is gone (removed 2026-10-29: it had no caller)
# --------------------------------------------------------------------------- #
def test_no_state_machine_and_no_provisioner_lambda(onboarding_template):
    """Provisioning runs in the app, so the stack synthesizes no state
    machine, no provisioner Lambda and no role either of them assumed."""
    assert _resources(onboarding_template, "AWS::StepFunctions::StateMachine") == {}
    names = [f["Properties"].get("FunctionName")
             for f in _resources(onboarding_template, "AWS::Lambda::Function").values()]
    assert "adt-onboarding-provisioner" not in names, names
    roles = _resources(onboarding_template, "AWS::IAM::Role")
    for lid in roles:
        assert not lid.startswith(("ProvisionerExecutionRole", "ProvisionerStateMachineRole")), lid
    assert "OnboardingProvisionerSfnArn" not in onboarding_template.get("Outputs", {})
    blob = json.dumps(onboarding_template)
    assert "adt-onboarding-provisioner" not in blob


def test_no_provisioner_image_asset(onboarding_assets):
    """No container image asset is built from Dockerfile.provisioner."""
    files = json.dumps(onboarding_assets.get("dockerImages", {}))
    assert "Dockerfile.provisioner" not in files, files[:400]


# --------------------------------------------------------------------------- #
# P1b — imperative-route stack outputs consumed by deploy.sh §2e
# --------------------------------------------------------------------------- #
def test_imperative_runtime_outputs_present(onboarding_template):
    outputs = onboarding_template.get("Outputs", {})
    keys = set(outputs)
    # deploy.sh §2e reads these to create the adt_onboarding runtime imperatively
    # (VPC mode, AZ-strip) and to create/sync the onboarding gateway target.
    for required in (
        "OnboardingRuntimeRoleArn",
        "OnboardingRuntimeSgId",
        "OnboardingRuntimeSubnetIds",
        "OnboardingSessionsTableName",
        "OnboardingGatewayId",
    ):
        assert required in keys, f"missing stack output {required!r} for deploy.sh §2e; have {sorted(keys)}"


def test_onboarding_runtime_role_still_present(onboarding_template):
    # The least-privileged runtime role is KEPT (its ARN is passed to the
    # imperative create as --role-arn) even though the CfnRuntime is gone.
    lid = _runtime_role_lid(onboarding_template)
    assert lid, "OnboardingRuntimeRole must remain (imperative create consumes its ARN)"
