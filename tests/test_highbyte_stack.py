# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression tests for the HighByte OT/UNS stack.

These guard the architecture decisions that broke (and were fixed) during the
internal-ALB + CloudFront VPC-origin migration:

* The HighByte ALB must be **internal** (no public exposure).
* CloudFront must reach it via **VPC origins**, not public custom origins.
* The CloudFront-ingress security groups must be attached to the **load
  balancer** — and NOT passed to the Fargate service (doing both makes CDK
  generate a security-group rule cross-product that exceeds the per-SG quota).
* The setup Lambda must be able to reach the ALB (it sets the admin password
  and imports the project), so an ALB ingress from the Lambda SG must exist.

The stack is synthesized once (offline; no AWS calls) and the resulting
CloudFormation template is asserted against. Requires the CDK CLI + Node, which
the project already vendors under ``node_modules`` — tests skip if unavailable.
"""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
STACK = "ADTHighByteOpcuaStack"


#: HighByte on, with the deployer's explicit EULA acceptance (the hub runs
#: only with ACCEPT_EULA=Y; stacks/_highbyte_options.py accept_eula_env).
HIGHBYTE_CONTEXT = ("highbyte_enabled=true", "highbyte_accept_eula=true")


@pytest.fixture(scope="session")
def highbyte_template(tmp_path_factory) -> dict:
    """Synthesize ADTHighByteOpcuaStack offline and return its CFN template dict."""
    return _synth_template(tmp_path_factory, *HIGHBYTE_CONTEXT)


@pytest.fixture(scope="session")
def opcua_only_template(tmp_path_factory) -> dict:
    """The same stack with ``opcua_enabled`` alone: OPC UA servers, no HighByte."""
    return _synth_template(tmp_path_factory, "opcua_enabled=true")


#: HighByte with stop-first sizing for a low vCPU quota and the SiteWise
#: pipeline off (the sizing a small or event account deploys with).
WORKSHOP_HIGHBYTE_CONTEXT = (
    *HIGHBYTE_CONTEXT, "highbyte_cpu=2048", "highbyte_memory=8192",
    "highbyte_stop_first=true", "opcua_cpu=256", "opcua_memory=1024",
    "opcua_health_check_grace_seconds=300", "opcua_stop_first=true",
    "highbyte_sitewise_pipeline=disabled",
)


@pytest.fixture(scope="session")
def workshop_sized_template(tmp_path_factory) -> dict:
    """HighByte with the workshop's Option A sizing and the SiteWise pipeline off."""
    return _synth_template(tmp_path_factory, *WORKSHOP_HIGHBYTE_CONTEXT)


def _synth_template(tmp_path_factory, *context_flags: str) -> dict:
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")

    out_dir = tmp_path_factory.mktemp("cdk_synth")
    # cdk.json runs `python3 app.py`; ensure that resolves to this interpreter
    # (the venv) so app.py's deps (cdk_nag, etc.) are importable in the subprocess.
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    context_args = [arg for flag in context_flags for arg in ("-c", flag)]
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [
            str(CDK_BIN), "synth", STACK,
            "-c", "region=us-east-1",
            *context_args,
            "--output", str(out_dir),
            "--quiet",
        ],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=300,
    )
    template_path = out_dir / f"{STACK}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    return json.loads(template_path.read_text())


def _resources(template: dict, cfn_type: str) -> dict:
    return {
        lid: r for lid, r in template.get("Resources", {}).items()
        if r.get("Type") == cfn_type
    }


def _highbyte_alb(template: dict) -> dict:
    """The HighByte service ALB (excludes the OPC UA NLBs / other LBs)."""
    albs = {
        lid: r for lid, r in _resources(template, "AWS::ElasticLoadBalancingV2::LoadBalancer").items()
        if "HighByteServiceLB" in lid
    }
    assert albs, "HighByte service load balancer not found in template"
    return next(iter(albs.values()))


def test_highbyte_alb_is_internal(highbyte_template):
    alb = _highbyte_alb(highbyte_template)
    assert alb["Properties"].get("Scheme") == "internal", (
        "HighByte ALB must be internal (CloudFront reaches it via a VPC origin); "
        "an internet-facing ALB is publicly exposed and the in-VPC setup Lambda "
        "cannot reach it."
    )


def test_highbyte_alb_drops_invalid_header_fields(highbyte_template):
    """Outside the workshop (stacks/_sample_hardening.py, checkov CKV_AWS_131)."""
    attrs = {a["Key"]: a["Value"] for a in
             _highbyte_alb(highbyte_template)["Properties"].get("LoadBalancerAttributes", [])}
    assert attrs.get("routing.http.drop_invalid_header_fields.enabled") == "true"


def test_highbyte_ui_pool_offers_totp_mfa(highbyte_template):
    """Outside the workshop the HighByte UI pool is MFA OPTIONAL with TOTP."""
    (pool,) = _resources(highbyte_template, "AWS::Cognito::UserPool").values()
    assert pool["Properties"]["MfaConfiguration"] == "OPTIONAL"
    assert pool["Properties"]["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]


def test_cloudfront_uses_vpc_origins(highbyte_template):
    vpc_origins = _resources(highbyte_template, "AWS::CloudFront::VpcOrigin")
    assert len(vpc_origins) == 2, (
        f"expected 2 CloudFront VPC origins (UI + MCP ports), found {len(vpc_origins)}"
    )


def test_distribution_has_no_public_custom_origin(highbyte_template):
    dists = _resources(highbyte_template, "AWS::CloudFront::Distribution")
    assert dists, "HighByte CloudFront distribution not found"
    for dist in dists.values():
        origins = dist["Properties"]["DistributionConfig"].get("Origins", [])
        for origin in origins:
            assert "VpcOriginConfig" in origin, (
                "HighByte distribution origins must use VpcOriginConfig (private), "
                "not a public CustomOriginConfig pointing at a public ALB DNS"
            )
            assert "CustomOriginConfig" not in origin


def test_alb_security_groups_attached_to_load_balancer(highbyte_template):
    """The two CloudFront-ingress SGs must be attached to the ALB itself."""
    alb = _highbyte_alb(highbyte_template)
    sg_refs = json.dumps(alb["Properties"].get("SecurityGroups", []))
    assert "HighByteALBSG" in sg_refs, "UI ALB SG not attached to the load balancer"
    assert "HighByteALBMcpSG" in sg_refs, "MCP ALB SG not attached to the load balancer"


def test_no_security_group_exceeds_rule_budget(highbyte_template):
    """Guard against the SG-rule cross-product that broke the in-place deploy.

    Counts standalone ingress rules per target SG plus inline ingress; no single
    SG should approach the 60-rule AWS quota. The cross-product bug produced far
    more than this.
    """
    counts: dict[str, int] = {}

    def _ref(group) -> str:
        if isinstance(group, dict):
            if "Ref" in group:
                return group["Ref"]
            if "Fn::GetAtt" in group:
                return group["Fn::GetAtt"][0]
        return str(group)

    for r in _resources(highbyte_template, "AWS::EC2::SecurityGroupIngress").values():
        target = _ref(r["Properties"].get("GroupId"))
        counts[target] = counts.get(target, 0) + 1
    for lid, r in _resources(highbyte_template, "AWS::EC2::SecurityGroup").items():
        inline = len(r["Properties"].get("SecurityGroupIngress", []))
        if inline:
            counts[lid] = counts.get(lid, 0) + inline

    worst = max(counts.values(), default=0)
    assert worst < 50, f"a security group has {worst} ingress rules (approaching the 60 quota): {counts}"


def test_setup_lambda_can_reach_alb(highbyte_template):
    """An ALB-SG ingress sourced from the setup Lambda's SG must exist."""
    template_json = json.dumps(highbyte_template)
    assert "SetupLambdaSecurityGroup" in template_json, (
        "setup Lambda security group not found"
    )
    ingress = _resources(highbyte_template, "AWS::EC2::SecurityGroupIngress")
    from_lambda = [
        r for r in ingress.values()
        if "SetupLambdaSecurityGroup" in json.dumps(r["Properties"].get("SourceSecurityGroupId", ""))
    ]
    assert from_lambda, "no ALB ingress rule sourced from the setup Lambda SG"


def test_setup_lambda_can_store_mcp_token(highbyte_template):
    """The setup Lambda is wired to RECONCILE + store the MCP bearer token on
    every run: it has the secret id, a verify endpoint pointing at the ALB MCP
    port, and an ALB MCP-port ingress rule from its own SG."""
    fns = {
        lid: r for lid, r in _resources(highbyte_template, "AWS::Lambda::Function").items()
        if r["Properties"].get("FunctionName") == "highbyte-setup"
    }
    assert fns, "highbyte-setup Lambda not found"
    env = next(iter(fns.values()))["Properties"]["Environment"]["Variables"]
    assert env.get("MCP_TOKEN_SECRET") == "adt/conn/highbyte"
    # The reconciler verifies the key against the ALB MCP port (8885), in-VPC.
    verify = env.get("MCP_VERIFY_ENDPOINT")
    assert verify, "setup Lambda has no MCP_VERIFY_ENDPOINT"
    assert "8885" in json.dumps(verify) and "/mcp" in json.dumps(verify), verify
    # The MCP ALB SG admits the setup Lambda's SG on 8885 so the verify reaches it.
    mcp_ingress = [
        r for r in _resources(highbyte_template, "AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"].get("FromPort") == 8885 and r["Properties"].get("ToPort") == 8885
    ]
    assert mcp_ingress, "no ALB ingress rule on the MCP port 8885"


def test_setup_lambda_stops_only_its_own_clusters_tasks(highbyte_template):
    """ecs:StopTask/DescribeTasks reach only tasks in the cluster the HighByte
    hub runs in; ListTasks (no resource type) is the one ECS grant on '*'."""
    (role_lid,) = [lid for lid, r in _resources(highbyte_template, "AWS::IAM::Role").items()
                   if lid.startswith("SetupLambdaRole")]
    policies = [p for p in _resources(highbyte_template, "AWS::IAM::Policy").values()
                if {"Ref": role_lid} in p["Properties"]["Roles"]]
    statements = _allow_statements(policies)
    ecs = [(actions, res) for actions, res in statements if any(a.startswith("ecs:") for a in actions)]
    assert {a for actions, _ in ecs for a in actions} == {
        "ecs:ListTasks", "ecs:StopTask", "ecs:DescribeTasks"}, ecs
    hub_clusters = {
        json.dumps(svc["Properties"]["Cluster"])
        for svc in _resources(highbyte_template, "AWS::ECS::Service").values()
        if "HighByte" in json.dumps(svc["Properties"].get("ServiceName", ""))
    }
    assert hub_clusters, "HighByte hub service not found"
    for actions, res in ecs:
        if res == "*":
            assert actions == {"ecs:ListTasks"}, ecs
        else:
            assert actions == {"ecs:StopTask", "ecs:DescribeTasks"}, ecs
            assert ":task/" in json.dumps(res), res
            assert any(cluster in json.dumps(res) for cluster in hub_clusters), (res, hub_clusters)


def test_every_lambda_uses_a_supported_runtime(highbyte_template, workshop_sized_template):
    """No function in this stack (the setup Lambda and the CDK-provided ones)
    uses a runtime in the "Deprecated runtimes" table at
    https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html, under
    the demo or the workshop context."""
    from _lambda_runtimes import deprecated_lambda_runtimes
    problems = deprecated_lambda_runtimes({
        "demo:" + STACK: highbyte_template, "workshop:" + STACK: workshop_sized_template})
    assert problems == [], problems


def test_setup_lambda_timeout_is_five_minutes(highbyte_template):
    """The setup handler's time budget (index.mjs
    reconcileDeadline from getRemainingTimeInMillis, SETUP_RESERVE_MS kept back)
    is sized for this 5-minute timeout; the MCP credential tests hold the
    reconcile to it. Changing the timeout must be a deliberate, re-tested act."""
    (fn,) = [r for r in _resources(highbyte_template, "AWS::Lambda::Function").values()
             if r["Properties"].get("FunctionName") == "highbyte-setup"]
    assert fn["Properties"]["Timeout"] == 300


def test_four_opc_ua_sites_are_synthesized(highbyte_template):
    """ADR-0083: the fourth simulated site ships its own OPC UA server, the
    setup Lambda receives its endpoint, and the stack exports it."""
    services = _resources(highbyte_template, "AWS::ECS::Service")
    names = sorted(
        r["Properties"].get("ServiceName", "")
        for r in services.values()
        if str(r["Properties"].get("ServiceName", "")).startswith("IDO-OPC-Server-")
    )
    assert names == [
        "IDO-OPC-Server-CedarRapids", "IDO-OPC-Server-Detroit",
        "IDO-OPC-Server-Indianapolis", "IDO-OPC-Server-Milwaukee",
    ]
    # Every OPC task passes SITE_NAME so server.sh loads the matching dataset.
    site_names = set()
    for r in _resources(highbyte_template, "AWS::ECS::TaskDefinition").values():
        for c in r["Properties"].get("ContainerDefinitions", []):
            for kv in c.get("Environment", []):
                if kv.get("Name") == "SITE_NAME":
                    site_names.add(kv.get("Value"))
    assert {"Milwaukee", "Detroit", "CedarRapids", "Indianapolis"} <= site_names
    outputs = highbyte_template.get("Outputs", {})
    assert "OpcUaEndpoint4" in outputs
    setup_env_keys = set()
    for r in _resources(highbyte_template, "AWS::Lambda::Function").values():
        setup_env_keys |= set((r["Properties"].get("Environment") or {}).get("Variables", {}).keys())
    assert "OPC_UA_ENDPOINT_4" in setup_env_keys


def test_opcua_only_deploys_the_four_servers_without_highbyte(opcua_only_template):
    """``opcua_enabled`` without ``highbyte_enabled``: the four OPC UA servers
    and their endpoint outputs, and none of the HighByte layer."""
    names = sorted(
        r["Properties"].get("ServiceName", "")
        for r in _resources(opcua_only_template, "AWS::ECS::Service").values()
    )
    assert names == [
        "IDO-OPC-Server-CedarRapids", "IDO-OPC-Server-Detroit",
        "IDO-OPC-Server-Indianapolis", "IDO-OPC-Server-Milwaukee",
    ]
    for cfn_type in ("AWS::CloudFront::Distribution", "AWS::EFS::FileSystem",
                     "AWS::Lambda::Function", "AWS::Cognito::UserPool",
                     "AWS::SecretsManager::Secret"):
        assert not _resources(opcua_only_template, cfn_type), cfn_type
    outputs = opcua_only_template.get("Outputs", {})
    assert {"OpcUaEndpoint", "OpcUaEndpoint2", "OpcUaEndpoint3", "OpcUaEndpoint4"} <= set(outputs)
    assert not any(k.startswith("HighByte") or k.startswith("MCP") for k in outputs)


def test_enabling_highbyte_later_does_not_replace_the_opcua_servers(
    opcua_only_template, highbyte_template
):
    """Same stack id and logical ids in both modes, so turning HighByte on
    after ``opcua_enabled`` ADDS to the stack instead of recreating the
    OPC UA services (and their NLB DNS names that consumers read)."""
    missing = set(opcua_only_template["Resources"]) - set(highbyte_template["Resources"])
    assert not missing, sorted(missing)


def _services_by_name(template: dict) -> dict[str, tuple[dict, dict]]:
    """ServiceName -> (service properties, its task definition's properties)."""
    resources = template["Resources"]
    out = {}
    for res in _resources(template, "AWS::ECS::Service").values():
        props = res["Properties"]
        task = resources[props["TaskDefinition"]["Ref"]]["Properties"]
        out[props["ServiceName"]] = (props, task)
    return out


def _hub_target_group_delays(template: dict) -> dict[str, str]:
    """Hub target group logical id -> its deregistration delay (UI and MCP)."""
    delays = {}
    for lid, res in _resources(template, "AWS::ElasticLoadBalancingV2::TargetGroup").items():
        if not lid.startswith(("HighByteService", "MCPTargetGroup")):
            continue
        attrs = {a["Key"]: a["Value"] for a in res["Properties"].get("TargetGroupAttributes", [])}
        delays[lid] = attrs.get("deregistration_delay.timeout_seconds")
    return delays


def test_demo_sizing_is_unchanged(highbyte_template):
    """No sizing context: the demo's hub 8 vCPU / 16 GB start-first and the
    OPC UA servers at 1 vCPU / 2 GB with CDK's 60 s grace period."""
    services = _services_by_name(highbyte_template)
    hub, hub_task = services["IDO-HighByteIntelligenceHub"]
    assert (hub_task["Cpu"], hub_task["Memory"]) == ("8192", "16384")
    assert hub["DeploymentConfiguration"] == {"MaximumPercent": 200, "MinimumHealthyPercent": 50}
    for name, (svc, task) in services.items():
        if name.startswith("IDO-OPC-Server-"):
            assert (task["Cpu"], task["Memory"]) == ("1024", "2048"), name
            assert svc["HealthCheckGracePeriodSeconds"] == 60, name
            # Demo: OPC servers deploy start-first (the default).
            assert svc["DeploymentConfiguration"]["MaximumPercent"] == 200, name
            assert svc["DeploymentConfiguration"]["MinimumHealthyPercent"] == 50, name


def test_workshop_option_a_sizing(workshop_sized_template):
    """Option A (2026-10-08 sizing review): hub 2 vCPU / 8 GB stop-first, its
    UI and MCP target groups at a 5 s deregistration delay, and the four OPC UA
    servers at 0.25 vCPU / 1 GB with a 300 s health-check grace period."""
    services = _services_by_name(workshop_sized_template)
    hub, hub_task = services["IDO-HighByteIntelligenceHub"]
    assert (hub_task["Cpu"], hub_task["Memory"]) == ("2048", "8192")
    assert hub["DeploymentConfiguration"] == {"MaximumPercent": 100, "MinimumHealthyPercent": 0}
    delays = _hub_target_group_delays(workshop_sized_template)
    assert len(delays) == 2 and set(delays.values()) == {"5"}, delays
    opc = {name: value for name, value in services.items() if name.startswith("IDO-OPC-Server-")}
    assert len(opc) == 4
    for name, (svc, task) in opc.items():
        assert (task["Cpu"], task["Memory"]) == ("256", "1024"), name
        assert svc["HealthCheckGracePeriodSeconds"] == 300, name
        # The workshop deploys the OPC servers stop-first (100/0) so
        # a redeploy adds no vCPU surge -- the create path needs no quota edge.
        assert svc["DeploymentConfiguration"]["MaximumPercent"] == 100, name
        assert svc["DeploymentConfiguration"]["MinimumHealthyPercent"] == 0, name


def test_workshop_hub_is_health_checked_on_the_port_a_trial_expiry_closes(workshop_sized_template):
    """An unlicensed hub stops the HTTP server on 8885 (MCP, REST, docs) when
    its trial expires, while the UI and config API on 45245 keep answering
    (measured locally and in the demo account's logs). The hub service is
    registered in BOTH target groups, so the 8885 check failing on the closed
    port marks the task unhealthy and ECS replaces it. The check reads
    ``/data/doc``, which a 4.4.2 hub with anonymous REST off answers with 303,
    inside the 200-499 range, so the hub is healthy until expiry."""
    resources = workshop_sized_template["Resources"]
    hub, _task = _services_by_name(workshop_sized_template)["IDO-HighByteIntelligenceHub"]
    checks = {}
    for lb in hub["LoadBalancers"]:
        group = resources[lb["TargetGroupArn"]["Ref"]]["Properties"]
        checks[lb["ContainerPort"]] = (group["HealthCheckPath"], str(group["HealthCheckPort"]),
                                       group["Matcher"]["HttpCode"])
    assert checks[8885] == ("/data/doc", "8885", "200-499"), checks
    assert checks[45245] == ("/ui/", "45245", "200-499"), checks


@pytest.fixture(scope="session")
def opcua_only_stop_first_template(tmp_path_factory) -> dict:
    """The OPC UA servers alone (no HighByte), deployed stop-first."""
    return _synth_template(tmp_path_factory, "opcua_enabled=true", "opcua_stop_first=true")


_OPC_SERVICES = {f"IDO-OPC-Server-{site}"
                 for site in ("Milwaukee", "Detroit", "CedarRapids", "Indianapolis")}


def _stop_first_services(template: dict) -> dict[str, dict]:
    """ServiceName -> service properties, for every MaximumPercent <= 100 service."""
    return {name: svc for name, (svc, _task) in _services_by_name(template).items()
            if svc.get("DeploymentConfiguration", {}).get("MaximumPercent", 200) <= 100}


def test_workshop_stop_first_services_disable_az_rebalancing(workshop_sized_template):
    """ECS refuses AZ rebalancing with maximumPercent <= 100 ("Availability
    Zone Rebalancing does not support maximumPercent <= 100 %"; our
    2026-10-10 update rolled back on it), so the stop-first hub and the four
    OPC UA servers set AvailabilityZoneRebalancing DISABLED explicitly."""
    stop_first = _stop_first_services(workshop_sized_template)
    assert set(stop_first) == {"IDO-HighByteIntelligenceHub", *_OPC_SERVICES}
    for name, svc in stop_first.items():
        assert svc.get("AvailabilityZoneRebalancing") == "DISABLED", name


def test_opcua_only_stop_first_servers_disable_az_rebalancing(opcua_only_stop_first_template):
    """The OPC UA-only path (no hub) sets it too."""
    stop_first = _stop_first_services(opcua_only_stop_first_template)
    assert set(stop_first) == _OPC_SERVICES
    for name, svc in stop_first.items():
        assert svc.get("AvailabilityZoneRebalancing") == "DISABLED", name


def test_demo_services_leave_az_rebalancing_unset(highbyte_template, opcua_only_template):
    """The demo (start-first) templates carry no AvailabilityZoneRebalancing,
    exactly as before."""
    for template in (highbyte_template, opcua_only_template):
        for name, (svc, _task) in _services_by_name(template).items():
            assert "AvailabilityZoneRebalancing" not in svc, name


def _setup_lambda_env(template: dict) -> dict:
    (fn,) = [r for r in _resources(template, "AWS::Lambda::Function").values()
             if r["Properties"].get("FunctionName") == "highbyte-setup"]
    return fn["Properties"]["Environment"]["Variables"]


def _iam_text(template: dict) -> str:
    return json.dumps([r for r in template["Resources"].values()
                       if r["Type"] in ("AWS::IAM::Policy", "AWS::IAM::Role", "AWS::IAM::ManagedPolicy")])


def _conn_highbyte_statements(template: dict) -> list[dict]:
    """Every IAM policy statement whose Resource references the scoped
    ``adt/conn/highbyte*`` secret ARN (across inline Policy/Role docs)."""
    out = []
    for r in template["Resources"].values():
        if r["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::Role", "AWS::IAM::ManagedPolicy"):
            continue
        props = r.get("Properties", {})
        docs = []
        if "PolicyDocument" in props:
            docs.append(props["PolicyDocument"])
        for p in props.get("Policies", []):
            if isinstance(p, dict) and "PolicyDocument" in p:
                docs.append(p["PolicyDocument"])
        for doc in docs:
            for stmt in doc.get("Statement", []):
                if "adt/conn/highbyte" in json.dumps(stmt.get("Resource", "")):
                    out.append(stmt)
    return out


def test_setup_role_can_read_the_conn_highbyte_secret(highbyte_template):
    """Reconciliation begins with GetSecretValue(adt/conn/
    highbyte), so the scoped conn/highbyte statement MUST grant
    secretsmanager:GetSecretValue. With only Put/Create/Describe, every run
    would read AccessDenied as 'secret absent' and
    minted another key."""
    stmts = _conn_highbyte_statements(highbyte_template)
    assert stmts, "no IAM statement scoped to adt/conn/highbyte* was synthesized"

    def _actions(s):
        a = s.get("Action", [])
        return a if isinstance(a, list) else [a]

    granted = {act for s in stmts for act in _actions(s)}
    assert "secretsmanager:GetSecretValue" in granted, (
        "the scoped adt/conn/highbyte* statement does not grant "
        f"secretsmanager:GetSecretValue; it grants {sorted(granted)}")
    # The write grants it already had must survive the change.
    assert {"secretsmanager:PutSecretValue", "secretsmanager:CreateSecret"} <= granted


def test_demo_hub_keeps_its_sitewise_pipeline(highbyte_template):
    """Default: the hub writes SiteWise (its task role keeps the grants) and
    the setup Lambda imports the pipeline as shipped (no switch env)."""
    assert "iotsitewise:BatchPutAssetPropertyValue" in _iam_text(highbyte_template)
    assert "HIGHBYTE_SITEWISE_PIPELINE" not in _setup_lambda_env(highbyte_template)


def test_workshop_hub_has_no_sitewise_grant_and_the_lambda_is_told(workshop_sized_template):
    """highbyte_sitewise_pipeline=disabled: SFC is the only SiteWise writer
    (ADR-0082), so no IAM in this stack mentions SiteWise at all, and the setup
    Lambda imports NameSpace_to_SiteWise with its triggers off."""
    assert "iotsitewise:" not in _iam_text(workshop_sized_template)
    assert _setup_lambda_env(workshop_sized_template)["HIGHBYTE_SITEWISE_PIPELINE"] == "disabled"


def test_setup_resource_uses_a_setup_revision_not_a_fixed_timestamp(highbyte_template):
    """The setup custom resource keys its properties off a
    deterministic SetupRevision (so a stack update re-runs setup), not the old
    fixed Timestamp string (which never changed, so EFS-backed hubs never got
    setup fixes on an update)."""
    # The setup CR is the one carrying SetupRevision; find it by property.
    setup = [r for _lid, r in _all_custom_resources(highbyte_template)
             if "SetupRevision" in (r.get("Properties") or {})]
    assert setup, "no custom resource carries a SetupRevision property"
    props = setup[0]["Properties"]
    assert "Timestamp" not in props, "the fixed Timestamp property is still present"
    assert isinstance(props["SetupRevision"], str) and len(props["SetupRevision"]) >= 32


def _all_custom_resources(template: dict) -> list[tuple[str, dict]]:
    return [(lid, r) for lid, r in template.get("Resources", {}).items()
            if str(r.get("Type", "")).startswith("Custom::")
            or r.get("Type") == "AWS::CloudFormation::CustomResource"]


def test_setup_revision_is_deterministic_and_input_sensitive(tmp_path):
    """setup_revision is a pure function: identical inputs give the same hash,
    and a change to the bundled project JSON, the settings JSON, a Lambda
    source, or the SiteWise flag changes it."""
    sys.path.insert(0, str(REPO_ROOT))
    from stacks.highbyte_opcua_stack import setup_revision

    src = tmp_path / "lambda"
    src.mkdir()
    (src / "index.mjs").write_text("export const handler = () => 1;")
    config = tmp_path / "project.json"
    config.write_text('{"project": {"name": "demo"}}')
    settings = tmp_path / "settings.json"
    settings.write_text('{"servers": {}}')

    base = setup_revision(str(src), str(config), str(settings), {"sitewise_pipeline": "enabled"})
    same = setup_revision(str(src), str(config), str(settings), {"sitewise_pipeline": "enabled"})
    assert base == same, "same inputs must give the same hash"

    # SiteWise flag change
    assert setup_revision(str(src), str(config), str(settings),
                          {"sitewise_pipeline": "disabled"}) != base
    # Project JSON change
    config.write_text('{"project": {"name": "demo", "transform": "changed"}}')
    assert setup_revision(str(src), str(config), str(settings),
                          {"sitewise_pipeline": "enabled"}) != base
    config.write_text('{"project": {"name": "demo"}}')
    # Lambda source change
    (src / "index.mjs").write_text("export const handler = () => 2;")
    assert setup_revision(str(src), str(config), str(settings),
                          {"sitewise_pipeline": "enabled"}) != base
    (src / "index.mjs").write_text("export const handler = () => 1;")
    # Settings JSON change
    settings.write_text('{"servers": {"rest": {"allowAnonymous": false}}}')
    assert setup_revision(str(src), str(config), str(settings),
                          {"sitewise_pipeline": "enabled"}) != base


def _seeded_settings(template: dict) -> dict:
    """The HB_SETTINGS_JSON the settings-seed init container writes to EFS."""
    for r in _resources(template, "AWS::ECS::TaskDefinition").values():
        for c in r["Properties"].get("ContainerDefinitions", []):
            for kv in c.get("Environment", []):
                if kv.get("Name") == "HB_SETTINGS_JSON":
                    return json.loads(kv["Value"])
    raise AssertionError("no settings-seed container with HB_SETTINGS_JSON found")


def test_internet_facing_rest_data_server_is_not_anonymous(highbyte_template):
    """The REST Data Server (port 8885, reachable from the internet
    via CloudFront /data/*) must NOT allow anonymous GETs. API-key auth stays
    on. An unauthenticated client must not be able to enumerate/read OT data."""
    settings = _seeded_settings(highbyte_template)["settings"]
    rest = settings["restDataServer"]
    assert rest["allowAnonymous"] is False, "REST Data Server still allows anonymous access"
    assert rest["authenticationModes"] == ["API_KEY"], "API-key auth must stay on"
    # MCP + REST share this server; the key path still works (verified live).
    assert rest["mcpServerEnabled"] is True


def test_workshop_rest_data_server_is_not_anonymous(workshop_sized_template):
    """The same holds for the workshop sizing (same seeded settings)."""
    rest = _seeded_settings(workshop_sized_template)["settings"]["restDataServer"]
    assert rest["allowAnonymous"] is False


def _settings_seed_container(template: dict) -> dict:
    for r in _resources(template, "AWS::ECS::TaskDefinition").values():
        for c in r["Properties"].get("ContainerDefinitions", []):
            if c.get("Name") == "settings-seed":
                return c
    raise AssertionError("no settings-seed container found")


def test_settings_seed_reconciles_allow_anonymous_on_existing_hubs(highbyte_template):
    """On an UPDATE of an existing EFS-backed hub
    the settings file already exists. The old init command only wrote on first
    boot (echo 'preserving'), so an existing internet-facing hub kept its old
    allowAnonymous:true. The new init command must RECONCILE allowAnonymous on
    an existing file -- it carries a patch program (SETTINGS_PATCH_AWK) and
    runs awk over the existing file -- and must NOT merely preserve it."""
    seed = _settings_seed_container(highbyte_template)
    env = {kv["Name"]: kv["Value"] for kv in seed.get("Environment", [])}
    patch = env.get("SETTINGS_PATCH_AWK")
    assert patch, "settings-seed has no SETTINGS_PATCH_AWK reconcile program"
    # The reconcile targets the REST/MCP server's anonymous flag, never the MQTT one.
    assert "allowAnonymous" in patch
    assert "httpServer" in patch and "restDataServer" in patch
    assert "hbMqttBroker" not in patch, "the MQTT broker's allowAnonymous must not be touched"
    cmd = " ".join(seed.get("Command", []))
    assert "SETTINGS_PATCH_AWK" in cmd and "awk" in cmd, "existing file is not reconciled with the patch"
    assert "already present" not in cmd.lower(), "the old first-boot-only preserve branch is still present"


def test_settings_seed_turns_config_tools_off_on_existing_hubs(highbyte_template, workshop_params_template):
    """The shipped file's mcpConfigTools "disabled" reaches only a first boot;
    an existing hub keeps its persisted serversConfig.mcpServer.configTools.
    The seed's patch program turns the setting off in both shapes the hub
    reads, in the demo and the workshop task alike, and the command tells it
    whether the file ends with a newline (so other bytes are kept)."""
    for template in (highbyte_template, workshop_params_template):
        seed = _settings_seed_container(template)
        patch = {kv["Name"]: kv["Value"] for kv in seed.get("Environment", [])}["SETTINGS_PATCH_AWK"]
        for part in ('"mcpConfigTools"', '"configTools"', '"serversConfig"', '"mcpServer"', "disabled"):
            assert part in patch, part
        assert "final_nl" in " ".join(seed.get("Command", []))


def test_hub_starts_only_after_the_settings_seed_succeeds(highbyte_template):
    """The seed fails closed (exit nonzero) on an
    unrecognised settings file, so the hub must depend on the seed's SUCCESS
    (zero exit), not COMPLETE (any exit) -- otherwise the hub would start on the
    un-reconciled, possibly anonymous, file."""
    for r in _resources(highbyte_template, "AWS::ECS::TaskDefinition").values():
        for c in r["Properties"].get("ContainerDefinitions", []):
            if c.get("Name") == "highbyte":
                deps = {d["ContainerName"]: d["Condition"] for d in c.get("DependsOn", [])}
                assert deps.get("settings-seed") == "SUCCESS", deps
                return
    raise AssertionError("no hub container found")


def _hub_container_images(template: dict) -> dict:
    """{container_name: image ref string} for the hub task's containers."""
    out = {}
    for r in _resources(template, "AWS::ECS::TaskDefinition").values():
        for c in r["Properties"].get("ContainerDefinitions", []):
            if c.get("Name") in ("highbyte", "settings-seed"):
                out[c["Name"]] = json.dumps(c.get("Image"))
    return out


def test_demo_images_are_public_and_no_circuit_breaker(highbyte_template):
    """With no overrides the demo pulls the hub + settings-seed from
    their public registries, by digest, and the hub service has no circuit
    breaker."""
    from stacks._external_images import EXTERNAL_IMAGES
    images = _hub_container_images(highbyte_template)
    hub_digest = EXTERNAL_IMAGES["highbyte-hub"]["platform_digests"]["linux/amd64"]
    seed_digest = EXTERNAL_IMAGES["settings-seed"]["platform_digests"]["linux/amd64"]
    assert json.loads(images["highbyte"]) == f"highbyte/intelligence-hub@{hub_digest}"
    assert json.loads(images["settings-seed"]) == (
        f"public.ecr.aws/docker/library/busybox@{seed_digest}")
    hub, _ = _services_by_name(highbyte_template)["IDO-HighByteIntelligenceHub"]
    assert "DeploymentCircuitBreaker" not in json.dumps(hub.get("DeploymentConfiguration", {}))


def test_the_hub_task_runs_on_the_platform_its_digests_are_pinned_for(highbyte_template):
    """The digests are the linux/amd64 manifests: the hub task declares no
    runtime platform (Fargate's default, LINUX/X86_64) or X86_64 itself."""
    from stacks._external_images import HUB_TASK_PLATFORM
    assert HUB_TASK_PLATFORM == "linux/amd64"
    for r in _resources(highbyte_template, "AWS::ECS::TaskDefinition").values():
        names = {c.get("Name") for c in r["Properties"].get("ContainerDefinitions", [])}
        if "highbyte" not in names:
            continue
        platform = r["Properties"].get("RuntimePlatform") or {}
        assert platform.get("CpuArchitecture", "X86_64") == "X86_64"
        assert platform.get("OperatingSystemFamily", "LINUX") == "LINUX"
        break
    else:
        raise AssertionError("no hub task definition in the template")


#: The account-ECR registry/repo an account-ECR hub image override points at
#: (the CDK bootstrap container-assets repository).
_ACCOUNT_ECR_REPO_REF = (
    "${AWS::AccountId}.dkr.ecr.${AWS::Region}.${AWS::URLSuffix}/"
    "cdk-hnb659fds-container-assets-${AWS::AccountId}-${AWS::Region}")


#: The account-ECR tag format: adt-ext-<name>-<first 12 hex of its amd64 digest>.
_ACCOUNT_ECR_TAG_FMT = "adt-ext-{name}-{digest_hex}"


def _pins():
    """A FRESH copy of the hub image pins (stacks/_external_images.py), so a
    test can change one pin without touching the copy other tests read."""
    import copy

    from stacks._external_images import EXTERNAL_IMAGES
    return copy.deepcopy(EXTERNAL_IMAGES)


def _account_ecr_tag(name: str, pins=None) -> str:
    digest = (pins or _pins())[name]["platform_digests"]["linux/amd64"]
    return _ACCOUNT_ECR_TAG_FMT.format(name=name, digest_hex=digest.split(":", 1)[1][:12])


def _account_ecr_ref(name: str, amd64_digest: str | None = None) -> str:
    """A synth-time account-ECR override for *name* from the real pins (or with
    only its linux/amd64 digest changed to *amd64_digest*): the image's
    digest-derived tag plus that digest, since an override must be pinned by
    digest."""
    pins = _pins()
    if amd64_digest is not None:
        pins[name]["platform_digests"]["linux/amd64"] = amd64_digest
    digest = pins[name]["platform_digests"]["linux/amd64"]
    return f"{_ACCOUNT_ECR_REPO_REF}:{_account_ecr_tag(name, pins)}@{digest}"


@pytest.fixture(scope="session")
def workshop_ecr_template(tmp_path_factory) -> dict:
    """HighByte synthesized with the flags the live package sets: the
    account-ECR hub image, with the settings seed running on that same image
    (package.py's default --settings-seed-image hub)."""
    return _synth_template(
        tmp_path_factory, *WORKSHOP_HIGHBYTE_CONTEXT,
        "highbyte_circuit_breaker=true",
        f"highbyte_image={_account_ecr_ref('highbyte-hub')}",
        f"settings_seed_image={_account_ecr_ref('highbyte-hub')}")


#: A hub linux/amd64 digest other than the pinned one.
_REPINNED_HUB_DIGEST = "sha256:" + "e" * 64


@pytest.fixture(scope="session")
def workshop_ecr_repinned_template(tmp_path_factory) -> dict:
    """The same live flags after a change to ONLY the hub's amd64 pin."""
    ref = _account_ecr_ref("highbyte-hub", _REPINNED_HUB_DIGEST)
    return _synth_template(
        tmp_path_factory, *WORKSHOP_HIGHBYTE_CONTEXT,
        "highbyte_circuit_breaker=true",
        f"highbyte_image={ref}", f"settings_seed_image={ref}")


@pytest.fixture(scope="session")
def workshop_other_seed_template(tmp_path_factory) -> dict:
    """The same hub image with a different settings-seed image: the demo's
    public BusyBox digest ref as a synth-time override (a deployer's choice;
    the workshop package runs the seed on the hub image)."""
    from stacks._external_images import task_image_ref
    return _synth_template(
        tmp_path_factory, *WORKSHOP_HIGHBYTE_CONTEXT,
        "highbyte_circuit_breaker=true",
        f"highbyte_image={_account_ecr_ref('highbyte-hub')}",
        f"settings_seed_image={task_image_ref('settings-seed')}")


def test_workshop_uses_account_ecr_images_and_circuit_breaker(workshop_ecr_template):
    """The workshop points both containers at the account's own ECR
    (bootstrap repo, adt-ext-<name>-<digest> tag): the seed runs on the hub
    image, so the task needs one external image. It turns on the hub
    deployment circuit breaker with rollback. Synthesized with the flags the
    live package sets."""
    template = workshop_ecr_template
    images = _hub_container_images(template)
    assert ":" + _account_ecr_tag("highbyte-hub") in images["highbyte"]
    assert images["settings-seed"] == images["highbyte"]
    hub, _ = _services_by_name(template)["IDO-HighByteIntelligenceHub"]
    cb = hub["DeploymentConfiguration"]["DeploymentCircuitBreaker"]
    assert cb == {"Enable": True, "Rollback": True}


def _hub_task(template: dict) -> tuple[str, dict]:
    """(logical id, properties) of the task definition running the hub."""
    (found,) = [(lid, r["Properties"]) for lid, r in
                _resources(template, "AWS::ECS::TaskDefinition").items()
                if any(c.get("Name") == "highbyte" for c in r["Properties"]["ContainerDefinitions"])]
    return found


def test_a_changed_hub_pin_changes_the_hub_task_definition(
        workshop_ecr_template, workshop_ecr_repinned_template):
    """With a stable adt-ext-highbyte-hub tag a pin change
    re-copied the tag but left the task definition unchanged, so the update
    gave ECS no new task to deploy. The tag now carries the amd64 digest, so
    the pin change moves both container images, nothing else in the task
    definition, and the hub service references that task definition (a
    changed one is a new revision the service deploys)."""
    old_id, old_task = _hub_task(workshop_ecr_template)
    new_id, new_task = _hub_task(workshop_ecr_repinned_template)
    assert old_id == new_id
    old_images = {c["Name"]: json.dumps(c["Image"]) for c in old_task["ContainerDefinitions"]}
    new_images = {c["Name"]: json.dumps(c["Image"]) for c in new_task["ContainerDefinitions"]}
    for name in ("highbyte", "settings-seed"):
        assert old_images[name] != new_images[name]
        assert ":adt-ext-highbyte-hub-" + "e" * 12 in new_images[name]

    def without_images(task):
        task = json.loads(json.dumps(task))
        for container in task["ContainerDefinitions"]:
            container.pop("Image")
        return task
    assert without_images(old_task) == without_images(new_task)
    for template in (workshop_ecr_template, workshop_ecr_repinned_template):
        hub, _ = _services_by_name(template)["IDO-HighByteIntelligenceHub"]
        assert hub["TaskDefinition"] == {"Ref": old_id}


def _hub_execution_policies(template: dict) -> list[dict]:
    """Every AWS::IAM::Policy attached to the hub task definition's execution role."""
    (task,) = [r for r in _resources(template, "AWS::ECS::TaskDefinition").values()
               if any(c.get("Name") == "highbyte" for c in r["Properties"]["ContainerDefinitions"])]
    role = task["Properties"]["ExecutionRoleArn"]["Fn::GetAtt"][0]
    return [p for p in _resources(template, "AWS::IAM::Policy").values()
            if {"Ref": role} in p["Properties"]["Roles"]]


def _allow_statements(policies: list[dict]) -> list[tuple[set, object]]:
    """(actions, resource) for every Allow statement in *policies*."""
    out = []
    for policy in policies:
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Effect") == "Allow":
                action = stmt["Action"]
                out.append(({action} if isinstance(action, str) else set(action), stmt["Resource"]))
    return out


def test_workshop_hub_execution_role_can_pull_its_account_ecr_images(workshop_ecr_template):
    """ECS pulls a task's images with its EXECUTION role, and from_registry
    grants that role nothing. On 2026-10-10 every hub task stopped
    with ecr:GetAuthorizationToken AccessDenied and the circuit breaker rolled
    the deploy back. The role now gets what CDK grants the OPC UA servers'
    asset images: layer/image reads on the bootstrap repo, the auth token on *."""
    policies = _hub_execution_policies(workshop_ecr_template)
    statements = _allow_statements(policies)
    pull = [res for actions, res in statements if {
        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"} <= actions]
    assert len(pull) == 1, statements
    assert ("repository/cdk-hnb659fds-container-assets-${AWS::AccountId}-${AWS::Region}"
            in json.dumps(pull[0])), pull
    assert [res for actions, res in statements if "ecr:GetAuthorizationToken" in actions] == ["*"]
    # checkov passes CKV_AWS_107 on this policy, and a skip would be a finding
    # in the Workshop Studio scanner (ADR-0065, amended 2026-10-19).
    assert "checkov" not in json.dumps([p.get("Metadata") for p in policies])


def test_the_seed_container_is_the_same_either_way_but_its_image(
        workshop_ecr_template, workshop_other_seed_template):
    """A different settings-seed image only swaps the seed container's image:
    the command, env, mount, essential flag and the hub's SUCCESS dependency
    are identical, and a public seed ref needs no grant, so the execution
    role's pull grant is identical too."""
    from stacks._external_images import task_image_ref

    def seed(template):
        return dict(_settings_seed_container(template))
    hub_seed, other_seed = seed(workshop_ecr_template), seed(workshop_other_seed_template)
    assert json.loads(json.dumps(other_seed.pop("Image"))) == task_image_ref("settings-seed")
    assert ":" + _account_ecr_tag("highbyte-hub") in json.dumps(hub_seed.pop("Image"))
    assert hub_seed == other_seed
    assert (_allow_statements(_hub_execution_policies(workshop_ecr_template))
            == _allow_statements(_hub_execution_policies(workshop_other_seed_template)))


def test_demo_hub_execution_role_has_no_ecr_grant(highbyte_template):
    """The demo pulls the hub + settings-seed from public registries, which
    needs no IAM, so its execution role gets no ECR action."""
    statements = _allow_statements(_hub_execution_policies(highbyte_template))
    assert not [a for actions, _ in statements for a in actions if a.startswith("ecr:")], statements


def test_account_ecr_repository_arn_reads_the_repo_out_of_the_ref():
    """The grant is scoped to the repository the ref names, with or without a
    tag or digest; public refs give None."""
    from stacks.highbyte_opcua_stack import _account_ecr_repository_arn as repo_arn
    want = ("arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/"
            "cdk-hnb659fds-container-assets-${AWS::AccountId}-${AWS::Region}")
    digest = "@sha256:" + "0" * 64
    for suffix in (":adt-ext-highbyte-hub", "", digest, ":adt-ext-highbyte-hub-2255d08fd39b" + digest):
        assert repo_arn(_ACCOUNT_ECR_REPO_REF + suffix) == want, suffix
    assert repo_arn("highbyte/intelligence-hub:4.5.3") is None
    assert repo_arn("public.ecr.aws/docker/library/busybox:1.36") is None






# ---------------------------------------------------------------------------
# The workshop switch: the images are deploy-time digest-ref parameters
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def workshop_params_template(tmp_path_factory) -> dict:
    """HighByte with the flags the live package sets now: the images are the
    two deploy-time parameters the root's Custom::ADTExternalImages fills."""
    return _synth_template(tmp_path_factory, *WORKSHOP_HIGHBYTE_CONTEXT,
                           "highbyte_circuit_breaker=true", "highbyte_image_parameters=true")


def test_the_switch_declares_two_digest_ref_parameters_the_containers_run(workshop_params_template):
    from stacks import _highbyte_options as opts
    params = workshop_params_template["Parameters"]
    for name in (opts.HUB_IMAGE_PARAMETER, opts.SETTINGS_SEED_IMAGE_PARAMETER):
        assert params[name]["Type"] == "String"
        assert params[name]["AllowedPattern"] == opts.IMAGE_PARAMETER_PATTERN
        assert "Default" not in params[name]  # the root must pass it
    # The task runs the parameter, so a new value (any content change, any
    # route change) is a new task definition; a moved tag is no change at all.
    images = {c["Name"]: c["Image"] for c in _hub_task(workshop_params_template)[1]["ContainerDefinitions"]}
    assert images == {"highbyte": {"Ref": opts.HUB_IMAGE_PARAMETER},
                      "settings-seed": {"Ref": opts.SETTINGS_SEED_IMAGE_PARAMETER}}
    assert "adt-ext-" not in json.dumps(workshop_params_template)


def test_the_workshop_child_names_no_busybox_image(workshop_params_template):
    """The workshop child's seed container runs only what the root passes
    (the hub image's digest ref), so the template names no BusyBox image and
    no account-ECR settings-seed tag."""
    text = json.dumps(workshop_params_template)
    assert "busybox" not in text.lower()
    assert "adt-ext-settings-seed" not in text


def test_the_switch_keeps_the_static_pull_grant_on_the_bootstrap_repo(workshop_params_template):
    """The copy and load routes pull from the bootstrap repo, so the grant is
    static; the direct route (Docker Hub / ECR Public) needs none."""
    statements = _allow_statements(_hub_execution_policies(workshop_params_template))
    pull = [res for actions, res in statements if {
        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"} <= actions]
    assert pull == [{"Fn::Sub": "arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/"
                                "cdk-hnb659fds-container-assets-${AWS::AccountId}-${AWS::Region}"}]
    assert [res for actions, res in statements if "ecr:GetAuthorizationToken" in actions] == ["*"]




def test_the_switch_with_an_image_key_fails_the_synth(tmp_path_factory):
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp("cdk_synth_bad")
    env = {**os.environ, "AWS_DEFAULT_REGION": "us-east-1",
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [str(CDK_BIN), "synth", STACK, "-c", "region=us-east-1", "-c", "highbyte_enabled=true",
         "-c", "highbyte_accept_eula=true",
         "-c", "highbyte_image_parameters=true", "-c", "highbyte_image=x/y:1",
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode != 0
    assert "highbyte_image_parameters=true takes the images as parameters" in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# HighByte EULA: ACCEPT_EULA=Y only from the deployer's explicit acceptance
# ---------------------------------------------------------------------------

#: HighByte's EULA document (the Docker guide links it as the agreement that
#: ACCEPT_EULA=Y accepts).
_HIGHBYTE_EULA_URL = "https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf"


def _synth_result(tmp_path_factory, *context_flags: str):
    """(completed process, out dir) of a synth of this stack with *context_flags*."""
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp("cdk_synth_eula")
    env = {**os.environ, "AWS_DEFAULT_REGION": "us-east-1",
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    context_args = [arg for flag in context_flags for arg in ("-c", flag)]
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [str(CDK_BIN), "synth", STACK, "-c", "region=us-east-1", *context_args,
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300)
    return result, out_dir


def test_a_highbyte_synth_without_eula_acceptance_fails_with_the_link(tmp_path_factory):
    """Setting ACCEPT_EULA=Y agrees to HighByte's EULA, so enabling HighByte
    alone is not enough: the synth stops and names the agreement and how to
    accept it, and writes no template."""
    result, out_dir = _synth_result(tmp_path_factory, "highbyte_enabled=true")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output[-2000:]
    assert _HIGHBYTE_EULA_URL in output
    assert "highbyte_accept_eula=true" in output
    assert "--accept-highbyte-eula" in output
    assert not (out_dir / f"{STACK}.template.json").exists()


def test_opcua_servers_alone_need_no_eula_acceptance(tmp_path_factory):
    """The OPC UA-only stack runs no HighByte container, so it synthesizes
    without the acceptance."""
    result, out_dir = _synth_result(tmp_path_factory, "opcua_enabled=true")
    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]
    template = json.loads((out_dir / f"{STACK}.template.json").read_text())
    assert "ACCEPT_EULA" not in json.dumps(template)


def test_the_accepted_hub_runs_with_accept_eula(highbyte_template, workshop_params_template):
    """With the explicit acceptance the hub container carries ACCEPT_EULA=Y
    (the container exits immediately without it)."""
    for template in (highbyte_template, workshop_params_template):
        env = {kv["Name"]: kv["Value"] for kv in _hub_container(template).get("Environment", [])}
        assert env.get("ACCEPT_EULA") == "Y", env


def test_the_demo_declares_no_image_parameter(highbyte_template):
    from stacks import _highbyte_options as opts
    params = highbyte_template.get("Parameters") or {}
    assert opts.HUB_IMAGE_PARAMETER not in params and opts.SETTINGS_SEED_IMAGE_PARAMETER not in params






# ---------------------------------------------------------------------------
# The hub image is a setup input: an image-only change re-runs the setup
# ---------------------------------------------------------------------------

def _setup_props(template: dict) -> dict:
    """Properties of the setup custom resource (the one carrying SetupRevision)."""
    (props,) = [r["Properties"] for _lid, r in _all_custom_resources(template)
                if "SetupRevision" in (r.get("Properties") or {})]
    return props


#: The 4.4.2 fallback the hub can roll back to, as a digest ref (a ref no
#: default pin spells, so the comparison below is a real image change).
_ROLLBACK_HUB_REF = ("highbyte/intelligence-hub@sha256:"
                     "0bb085e939253631a427eb9688ad957d6f588454c4f4c14a488956bdec17e716")


@pytest.fixture(scope="session")
def demo_rollback_template(tmp_path_factory) -> dict:
    """The demo synthesized with only the hub image changed (rolled back)."""
    return _synth_template(tmp_path_factory, *HIGHBYTE_CONTEXT,
                           f"highbyte_image={_ROLLBACK_HUB_REF}")


def test_a_demo_hub_image_change_alone_changes_the_setup_revision(
        highbyte_template, demo_rollback_template):
    """4.4.2 cannot read the project file 4.5.3 rewrites: it boots with an
    empty project until setup re-imports it (local rollback probe,
    2026-10-11). So a hub image change, forward or back, must send the setup
    custom resource an Update: the image the hub task runs is hashed into
    SetupRevision, and nothing else about the setup changes."""
    old, new = _setup_props(highbyte_template), _setup_props(demo_rollback_template)
    from stacks.highbyte_opcua_stack import HIGHBYTE_IMAGE
    assert HIGHBYTE_IMAGE in _hub_container_images(highbyte_template)["highbyte"]
    assert HIGHBYTE_IMAGE != _ROLLBACK_HUB_REF
    assert _ROLLBACK_HUB_REF in _hub_container_images(demo_rollback_template)["highbyte"]
    assert old["SetupRevision"] != new["SetupRevision"]
    assert {k: v for k, v in old.items() if k != "SetupRevision"} == \
        {k: v for k, v in new.items() if k != "SetupRevision"}


def test_a_workshop_hub_pin_change_alone_changes_the_setup_revision(
        workshop_ecr_template, workshop_ecr_repinned_template):
    """The same for the account-ECR ref the workshop pins by digest-derived tag."""
    assert (_setup_props(workshop_ecr_template)["SetupRevision"]
            != _setup_props(workshop_ecr_repinned_template)["SetupRevision"])


def test_the_same_hub_image_keeps_the_setup_revision(
        workshop_ecr_template, workshop_other_seed_template):
    """Two synths running the same hub image (only the settings-seed image
    differs) give the same SetupRevision: the seed image is not a setup input,
    and the hash is stable across synths."""
    assert _setup_props(workshop_ecr_template) == _setup_props(workshop_other_seed_template)


def test_the_workshop_setup_resource_carries_the_hub_image_parameter(workshop_params_template):
    """Under the image parameters the hub image is a deploy-time value, unknown
    at synth, so it cannot be hashed: the setup custom resource carries it as
    its own property (like OpcUaEndpoints), the very value the hub container
    runs, so a new image value sends the setup an Update."""
    from stacks import _highbyte_options as opts
    images = {c["Name"]: c["Image"] for c in _hub_task(workshop_params_template)[1]["ContainerDefinitions"]}
    assert _setup_props(workshop_params_template)["HubImage"] == images["highbyte"] == \
        {"Ref": opts.HUB_IMAGE_PARAMETER}


def test_the_demo_setup_resource_hashes_the_image_instead_of_carrying_it(highbyte_template):
    assert "HubImage" not in _setup_props(highbyte_template)


# ---------------------------------------------------------------------------
# The 4.5 knowledge graph lives in the container, not on EFS
# ---------------------------------------------------------------------------

def _hub_container(template: dict) -> dict:
    (container,) = [c for c in _hub_task(template)[1]["ContainerDefinitions"] if c["Name"] == "highbyte"]
    return container


def test_the_hub_knowledge_graph_is_kept_off_efs(highbyte_template, workshop_params_template):
    """HighByte 4.5 keeps a knowledge-graph directory (115 small files on a
    local 4.5.3 hub) that it rebuilds at every start and project import, and
    its release notes call the default appData location "not ideal for
    network drives"; appData here is EFS. HIGHBYTE_GRAPHDB_DIR moves it
    (local 4.5.3 probe: files under the new path, none in appData, graph
    queries still answer). The path must be writable in the container: the
    root filesystem is not read-only, and the 4.5.3 image's /tmp is 1777."""
    for template in (highbyte_template, workshop_params_template):
        hub = _hub_container(template)
        env = {kv["Name"]: kv["Value"] for kv in hub.get("Environment", [])}
        graph_dir = env.get("HIGHBYTE_GRAPHDB_DIR")
        assert graph_dir == "/tmp/graph-db", env
        mounts = [m["ContainerPath"] for m in hub.get("MountPoints", [])]
        assert mounts == ["/usr/local/highbyte/appData"]
        assert not any(graph_dir == m or graph_dir.startswith(m.rstrip("/") + "/") for m in mounts)
        assert hub.get("ReadonlyRootFilesystem") is not True
