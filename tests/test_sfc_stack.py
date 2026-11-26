# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time tests for the SFC runtime stack.

These guard the three ADR-0082 decisions the stack implements:

* **Decision 2** — the SiteWise IAM set is exactly the ``Assets``-mode
  data-plane actions, and ``CreateAssetModel`` / ``UpdateAssetModel`` are NOT
  granted, so the 28 pre-created models cannot be mutated by the writer. The
  T2 ``sfc_writer_policy`` (S3 Tables, data-plane only) is attached to the same
  task role.
* **Decision 6** — the config is generated at synth into the staged build
  context under ``cdk.out`` (never the checkout); the container is told where
  to read it (``SFC_CONFIG_PATH``).
* **Decision 9** — the SiteWise models and asset tree come from an in-stack
  custom resource the service waits for; only that resource's role may create.
* **Decision 8** — SFC runs in the OPC UA servers' VPC and adds NO
  security-group rule opening the OPC UA port (26543). ``highbyte_opcua_stack``
  already grants that ingress to the VPC CIDR (NLB IP targets do not preserve
  the client IP), so an in-VPC task is already permitted.

The stack is synthesized in-process into its own ``cdk.App`` (an explicit
container image is injected so synth is hermetic and needs no Docker build),
and the resulting CloudFormation template is asserted against with
``aws_cdk.assertions.Template``. Requires ``aws-cdk-lib`` to be importable — the
tests skip with an actionable message if it is not (mirrors
``tests/test_sfc_s3tables_stack.py``).

Run:  PYTHONPATH=. python -m pytest tests/test_sfc_stack.py -q
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

if importlib.util.find_spec("aws_cdk") is None:
    pytest.skip(
        "aws-cdk-lib not importable — activate the .venv and "
        "`pip install -r requirements.txt`",
        allow_module_level=True,
    )

import aws_cdk as cdk  # noqa: E402
from aws_cdk import assertions  # noqa: E402
from aws_cdk import aws_ec2 as ec2  # noqa: E402
from aws_cdk import aws_ecs as ecs  # noqa: E402

from stacks.sfc_s3tables_stack import SfcS3TablesStack  # noqa: E402
from stacks.sfc_stack import (  # noqa: E402
    OPC_UA_PORT,
    SFC_CONFIG_PATH,
    SFC_SITEWISE_ACTIONS,
    SfcStack,
)

# Placeholder account id per the repo convention for unit tests/docs.
_ENV = cdk.Environment(account="111122223333", region="us-east-1")

# The OPC UA endpoints the OPC UA stack exposes (one per site, OPC_UA_SITES order).
_OPC_UA_ENDPOINTS = [
    "opc.tcp://milwaukee-nlb.example:26543/UA/ManufacturingServer",
    "opc.tcp://detroit-nlb.example:26543/UA/ManufacturingServer",
    "opc.tcp://cedarrapids-nlb.example:26543/UA/ManufacturingServer",
    "opc.tcp://indianapolis-nlb.example:26543/UA/ManufacturingServer",
]

# The model-mutating actions decision 2 deliberately withholds from the writer.
_FORBIDDEN_SITEWISE_ACTIONS = {
    "iotsitewise:CreateAssetModel",
    "iotsitewise:UpdateAssetModel",
}


class _Net(cdk.Stack):
    """Tiny host stack exposing a ``.vpc`` — stands in for NetworkStack."""

    def __init__(self, scope, cid, **kwargs):
        super().__init__(scope, cid, **kwargs)
        self.vpc = ec2.Vpc(self, "Vpc", max_azs=2)


def _build_app(context: dict | None = None) -> tuple[cdk.App, SfcStack]:
    """Synthesize the runtime stack with an injected image (no Docker build)."""
    app = cdk.App(context=context or {})
    net = _Net(app, "TestNet", env=_ENV)
    s3tables = SfcS3TablesStack(app, "TestSfcS3Tables", env=_ENV)
    sfc = SfcStack(
        app,
        "TestSfcStack",
        env=_ENV,
        vpc=net.vpc,
        opc_ua_endpoints=_OPC_UA_ENDPOINTS,
        s3tables_stack=s3tables,
        # Inject an image so the test synthesizes hermetically — the default
        # path is a `from_asset` build context that is a separate deliverable.
        image=ecs.ContainerImage.from_registry(
            "public.ecr.aws/docker/library/eclipse-temurin:17-jre"
        ),
    )
    return app, sfc


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    """Synthesize the standalone runtime stack once and return its template."""
    _app, sfc = _build_app()
    return assertions.Template.from_stack(sfc)


# ---------------------------------------------------------------------------
# The service exists and is a Fargate service
# ---------------------------------------------------------------------------
def test_creates_one_fargate_service(template):
    services = template.find_resources("AWS::ECS::Service")
    assert len(services) == 1, f"expected one ECS service, found {len(services)}"
    props = next(iter(services.values()))["Properties"]
    assert props.get("LaunchType") == "FARGATE"


def _service_props(template: assertions.Template) -> dict:
    (service,) = template.find_resources("AWS::ECS::Service").values()
    return service["Properties"]


def test_demo_sfc_is_start_first_with_az_rebalancing_unset(template):
    """No opcua_stop_first (the demo): start-first, no AvailabilityZoneRebalancing."""
    props = _service_props(template)
    assert props["DeploymentConfiguration"]["MaximumPercent"] == 200
    assert "AvailabilityZoneRebalancing" not in props


def test_workshop_stop_first_sfc_disables_az_rebalancing():
    """opcua_stop_first (the workshop): SFC deploys 100/0, and ECS refuses AZ
    rebalancing with maximumPercent <= 100, so it sets
    AvailabilityZoneRebalancing DISABLED explicitly."""
    _app, sfc = _build_app({"opcua_stop_first": "true"})
    props = _service_props(assertions.Template.from_stack(sfc))
    assert props["DeploymentConfiguration"]["MaximumPercent"] == 100
    assert props["DeploymentConfiguration"]["MinimumHealthyPercent"] == 0
    assert props["AvailabilityZoneRebalancing"] == "DISABLED"


def test_task_definition_is_fargate_sized(template):
    tasks = template.find_resources("AWS::ECS::TaskDefinition")
    assert len(tasks) == 1, f"expected one task definition, found {len(tasks)}"
    props = next(iter(tasks.values()))["Properties"]
    assert "FARGATE" in props["RequiresCompatibilities"]

def test_task_runs_on_the_architecture_the_image_is_built_for(template):
    """The image asset is LINUX_ARM64, so the task must pin ARM64: Fargate's
    default is X86_64, and the mismatch crash-loops the JVM with
    ``exec /usr/bin/java: exec format error`` (found on a sandbox deploy)."""
    props = next(iter(template.find_resources("AWS::ECS::TaskDefinition").values()))["Properties"]
    assert props.get("RuntimePlatform", {}).get("CpuArchitecture") == "ARM64"
    assert props["RuntimePlatform"].get("OperatingSystemFamily") == "LINUX"


# ---------------------------------------------------------------------------
# Decision 2: SiteWise IAM — exactly the data-plane set, no model mutation
# ---------------------------------------------------------------------------
def _all_task_role_actions(template: assertions.Template) -> set[str]:
    """Every action across the task role's inline policies + managed policies.

    Scoped to policies attached to the SFC TASK role: the stack also holds the
    asset-tree custom resource's role (ADR-0082 decision 9), which may create
    models and assets and must not be mistaken for the writer's grants."""
    actions: set[str] = set()
    (task_role_id,) = [lid for lid in template.find_resources("AWS::IAM::Role")
                       if "TaskRole" in lid]
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": task_role_id} not in policy["Properties"].get("Roles", []):
            continue
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            act = stmt.get("Action", [])
            actions.update([act] if isinstance(act, str) else act)
    for policy in template.find_resources("AWS::IAM::ManagedPolicy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            act = stmt.get("Action", [])
            actions.update([act] if isinstance(act, str) else act)
    return actions


def test_sitewise_actions_are_exactly_the_decision2_set(template):
    actions = _all_task_role_actions(template)
    sitewise = {a for a in actions if a.startswith("iotsitewise:")}
    assert sitewise == set(SFC_SITEWISE_ACTIONS), (
        f"SiteWise actions differ from ADR-0082 D2: {sorted(sitewise)}"
    )


def test_sitewise_excludes_model_mutation(template):
    leaked = _all_task_role_actions(template) & _FORBIDDEN_SITEWISE_ACTIONS
    assert not leaked, (
        "SFC writes in Assets mode (never AssetCreation); it must NOT be able "  # nosec B608 - assertion message, not SQL
        f"to create/update asset models (ADR-0082 D2) — found {sorted(leaked)}"
    )


def test_sitewise_action_list_matches_adr(template):
    # The actions ADR-0082 decision 2 names, as amended 2026-10-13: no
    # DescribeEndpoint, which is not an AWS IoT SiteWise IAM action.
    assert set(SFC_SITEWISE_ACTIONS) == {
        "iotsitewise:BatchPutAssetPropertyValue",
        "iotsitewise:DescribeAsset",
        "iotsitewise:DescribeAssetModel",
        "iotsitewise:ListAssets",
        "iotsitewise:ListAssetModels",
        "iotsitewise:ListAssetModelProperties",
    }


def _sitewise_statements(template, *, task_role: bool) -> list[dict]:
    """SiteWise statements on the SFC task role (or on every other role)."""
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        on_task_role = any("TaskRole" in r.get("Ref", "") for r in policy["Properties"]["Roles"])
        if on_task_role != task_role:
            continue
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            act = stmt.get("Action", [])
            acts = [act] if isinstance(act, str) else act
            if any(a.startswith("iotsitewise:") for a in acts):
                found.append(stmt)
    return found


def _resources(stmt: dict) -> list:
    res = stmt.get("Resource", [])
    return res if isinstance(res, list) else [res]


def _arn_suffix(resource) -> str:
    """The resource part of a synthesized ARN, after the account (or '*')."""
    if resource == "*":
        return "*"
    if isinstance(resource, dict) and "Fn::Join" in resource:
        sep, parts = resource["Fn::Join"]
        resource = sep.join(p if isinstance(p, str) else "<token>" for p in parts)
    assert isinstance(resource, str) and resource.startswith("arn:"), resource
    arn = resource.split(":", 5)
    assert arn[2] == "iotsitewise", resource
    return arn[5]


def test_the_writer_writes_only_to_this_accounts_assets_and_streams(template):
    """BatchPutAssetPropertyValue is authorized per entry against the asset
    (asset id) or the time series (property alias), so those two ARN types in
    this account and Region are all the writer needs; no write on '*'."""
    statements = _sitewise_statements(template, task_role=True)
    writes = [s for s in statements
              if "iotsitewise:BatchPutAssetPropertyValue" in _resources_actions(s)]
    assert len(writes) == 1, writes
    (write,) = writes
    assert _resources_actions(write) == {"iotsitewise:BatchPutAssetPropertyValue"}
    assert sorted(_arn_suffix(r) for r in _resources(write)) == ["asset/*", "time-series/*"]
    for stmt in statements:
        if "*" in _resources(stmt):
            assert not {a for a in _resources_actions(stmt)
                        if not a.startswith(("iotsitewise:Describe", "iotsitewise:List"))}, stmt


def test_the_tree_role_writes_only_to_this_accounts_models_and_assets(template):
    """The tree custom resource's writes name asset and asset-model ARNs.
    CreateAssetModel has no resource type, so it is the one write on '*'."""
    statements = _sitewise_statements(template, task_role=False)
    for stmt in statements:
        writes = {a for a in _resources_actions(stmt)
                  if not a.startswith(("iotsitewise:Describe", "iotsitewise:List"))}
        if "*" in _resources(stmt):
            assert writes <= {"iotsitewise:CreateAssetModel"}, stmt
        elif writes:
            assert {_arn_suffix(r) for r in _resources(stmt)} <= {"asset/*", "asset-model/*"}, stmt
    granted = set().union(*(_resources_actions(s) for s in statements))
    assert {"iotsitewise:CreateAsset", "iotsitewise:UpdateAsset", "iotsitewise:AssociateAssets",
            "iotsitewise:UpdateAssetModel", "iotsitewise:CreateAssetModel"} <= granted


def _resources_actions(stmt: dict) -> set[str]:
    act = stmt.get("Action", [])
    return {act} if isinstance(act, str) else set(act)


def test_s3tables_writer_policy_attached(template):
    """The T2 S3 Tables writer policy is attached to the SFC task role.

    ``sfc_writer_policy`` is a ManagedPolicy OWNED by ``SfcS3TablesStack``, so
    attaching it to a role in THIS stack is a cross-stack reference: the policy
    resource (and its actions) live in the other stack's template, and this
    template shows only the task role importing that policy's ARN
    (``ManagedPolicyArns: [{Fn::ImportValue: …SfcWriterPolicy…}]``). Assert the
    import, not the actions — the actions are asserted in the T2 stack's tests.
    """
    roles = template.find_resources("AWS::IAM::Role")
    task_role = next(
        (r for lid, r in roles.items() if "TaskRole" in lid), None
    )
    assert task_role is not None, "SFC task role not found"
    arns = json.dumps(task_role["Properties"].get("ManagedPolicyArns", []))
    assert "SfcWriterPolicy" in arns, (
        "the T2 sfc_writer_policy must be attached to the SFC task role "
        "(cross-stack ManagedPolicyArns import)"
    )


# ---------------------------------------------------------------------------
# Decision 8: same VPC, NO security-group rule opening the OPC UA port
# ---------------------------------------------------------------------------
def test_no_ingress_rule_opens_the_opc_ua_port(template):
    """SFC adds no SG rule for 26543 — the in-VPC task is already permitted by
    the OPC UA stack's VPC-CIDR ingress (ADR-0082 decision 8)."""
    def _opens_opc_port(props: dict) -> bool:
        from_p = props.get("FromPort")
        to_p = props.get("ToPort")
        return from_p == OPC_UA_PORT or to_p == OPC_UA_PORT

    standalone = [
        r for r in template.find_resources("AWS::EC2::SecurityGroupIngress").values()
        if _opens_opc_port(r["Properties"])
    ]
    assert not standalone, (
        "SFC must not add a security-group ingress rule for the OPC UA port "
        f"{OPC_UA_PORT} (ADR-0082 decision 8)"
    )

    for sg in template.find_resources("AWS::EC2::SecurityGroup").values():
        for inline in sg["Properties"].get("SecurityGroupIngress", []):
            assert not _opens_opc_port(inline), (
                "SFC must not open the OPC UA port via inline SG ingress "
                "(ADR-0082 decision 8)"
            )


def test_service_runs_in_shared_vpc_private_subnets(template):
    """The task's network config places it in the shared VPC's subnets and
    assigns no public IP (private-with-egress)."""
    services = template.find_resources("AWS::ECS::Service")
    net_cfg = next(iter(services.values()))["Properties"]["NetworkConfiguration"]
    awsvpc = net_cfg["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == "DISABLED", (
        "SFC runs egress-only in private subnets alongside the OPC UA servers"
    )
    assert awsvpc.get("Subnets"), "service must be placed in the shared VPC subnets"


# ---------------------------------------------------------------------------
# Decision 6: config delivered via staging; generator NOT imported at synth
# ---------------------------------------------------------------------------
def test_container_told_where_to_read_config(template):
    tasks = template.find_resources("AWS::ECS::TaskDefinition")
    container = next(iter(tasks.values()))["Properties"]["ContainerDefinitions"][0]
    env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    assert env.get("SFC_CONFIG_PATH") == SFC_CONFIG_PATH, (
        "SFC container must be told the config path it reads (ADR-0082 D6)"
    )



def test_server_revisions_ride_in_the_container_env_only_when_passed(template):
    """SFC must roll when a deploy replaces an OPC UA server task: its OPC UA
    subscriptions do not survive a server restart. app.py passes the server
    task-definition ARNs; the env var carries them (absent when not passed)."""
    tasks = template.find_resources("AWS::ECS::TaskDefinition")
    container = next(iter(tasks.values()))["Properties"]["ContainerDefinitions"][0]
    assert "OPC_UA_SERVER_REVISIONS" not in {e["Name"] for e in container.get("Environment", [])}
    app = cdk.App()
    net = _Net(app, "TestNet2", env=_ENV)
    s3tables = SfcS3TablesStack(app, "TestSfcS3Tables2", env=_ENV)
    sfc = SfcStack(app, "TestSfcStack2", env=_ENV, vpc=net.vpc, opc_ua_endpoints=_OPC_UA_ENDPOINTS,
                   opc_ua_server_revisions=["arn:td/opc-a:3", "arn:td/opc-b:7"], s3tables_stack=s3tables,
                   image=ecs.ContainerImage.from_registry("public.ecr.aws/docker/library/eclipse-temurin:17-jre"))
    tasks = assertions.Template.from_stack(sfc).find_resources("AWS::ECS::TaskDefinition")
    env = {e["Name"]: e["Value"] for e in
           next(iter(tasks.values()))["Properties"]["ContainerDefinitions"][0]["Environment"]}
    assert env["OPC_UA_SERVER_REVISIONS"] == "arn:td/opc-a:3,arn:td/opc-b:7"


def test_app_passes_the_servers_fingerprint_not_their_arns_to_sfc():
    """A task-definition ARN token would become a cross-stack export, which
    CloudFormation refuses to change while SFC imports it."""
    text = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    # opcua_stack: the servers' own stack when split (-c opcua_servers_stack), else the hub's.
    assert "opc_ua_server_revisions=[opcua_stack.opc_ua_server_fingerprint]," in text
    assert "task_definition_arn for svc" not in text


def test_server_fingerprint_follows_the_image_inputs(tmp_path):
    from stacks.highbyte_opcua_stack import server_fingerprint
    server = tmp_path / "components" / "opc-server"
    server.mkdir(parents=True)
    (server / "server.mjs").write_text("a")
    (server / "node_modules").mkdir()
    (server / "node_modules" / "x.js").write_text("ignored")
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "site.csv.gz").write_bytes(b"d1")
    first = server_fingerprint(str(tmp_path), ["live", None])
    assert first == server_fingerprint(str(tmp_path), ["live", None])
    assert len(first) == 16 and not cdk.Token.is_unresolved(first)
    (server / "node_modules" / "x.js").write_text("still ignored")
    assert server_fingerprint(str(tmp_path), ["live", None]) == first
    (tmp_path / "datasets" / "site.csv.gz").write_bytes(b"d2")
    second = server_fingerprint(str(tmp_path), ["live", None])
    assert second != first
    (server / "server.mjs").write_text("b")
    assert server_fingerprint(str(tmp_path), ["live", None]) not in (first, second)
    assert server_fingerprint(str(tmp_path), ["live", 10800]) != server_fingerprint(str(tmp_path), ["live", None])

def test_jvm_heap_sized_to_the_container(template):
    """The JVM default heap (25% of the limit) starves SFC's target writers;
    the container must raise it via JAVA_TOOL_OPTIONS. It must also leave
    room outside the heap: at 2048 MB / 75% ECS killed the task for memory
    (exit 137, the container limit, not a heap error), because memory beside
    the heap outgrew the ~512 MB left over."""
    import re

    tasks = template.find_resources("AWS::ECS::TaskDefinition")
    props = next(iter(tasks.values()))["Properties"]
    container = props["ContainerDefinitions"][0]
    env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    opts = env.get("JAVA_TOOL_OPTIONS", "")
    assert "MaxRAMPercentage" in opts, (
        "SFC container must size the JVM heap to its memory limit"
    )
    assert props["Cpu"] == "1024" and props["Memory"] == "4096"
    percent = float(re.search(r"MaxRAMPercentage=([0-9.]+)", opts).group(1))
    assert percent == 37.5
    heap_mb = int(props["Memory"]) * percent / 100
    native_mb = int(props["Memory"]) - heap_mb
    # Same heap as the 2048 MB / 75% sizing that kept the writers fed; every
    # added MB goes to the native side, which is what ran out.
    assert heap_mb == 1536, f"heap {heap_mb:.0f} MB"
    assert native_mb >= 2048, f"only {native_mb:.0f} MB native headroom"


_LIVE = {"with_workshop": "true", "workshop_story": "live"}


def _task_definition(context: dict) -> dict:
    _app, sfc = _build_app(context)
    tasks = assertions.Template.from_stack(sfc).find_resources("AWS::ECS::TaskDefinition")
    (task,) = tasks.values()
    return task["Properties"]


def test_the_history_replay_doubles_the_sfc_memory_not_its_vcpu():
    """The replay adds a second S3 Tables target (its own copy of the writer
    libraries and Iceberg catalog client) to a task that already peaked at
    99.95 % memory on 4 GB. With the replay on, the task gets 8 GB at the same
    1 vCPU (a valid Fargate size; no added vCPU quota), and the same
    MaxRAMPercentage gives a 3 GB heap and 5 GB beside it, both doubled."""
    import re

    props = _task_definition(_LIVE)
    assert props["Cpu"] == "1024" and props["Memory"] == "8192"
    env = {e["Name"]: e["Value"] for e in props["ContainerDefinitions"][0]["Environment"]}
    percent = float(re.search(r"MaxRAMPercentage=([0-9.]+)", env["JAVA_TOOL_OPTIONS"]).group(1))
    heap_mb = 8192 * percent / 100
    assert heap_mb == 3072 and 8192 - heap_mb >= 2 * 2048


def test_the_replay_switched_off_keeps_the_4_gb_task_byte_identical():
    off = _task_definition({**_LIVE, "story_feed_backfill": "false"})
    assert off["Cpu"] == "1024" and off["Memory"] == "4096"
    assert off == _task_definition({}), "replay off: the same task definition as a non-workshop synth"


def test_config_is_generated_into_cdk_out_not_the_checkout(tmp_path):
    """Decision 6: the default image's build context is staged under cdk.out
    with a config generated at synth — four sources, region and OPC UA hosts as
    SFC placeholders — and nothing is written into the checkout."""
    from pathlib import Path

    from stacks import sfc_stack

    repo = Path(sfc_stack.__file__).resolve().parents[1]
    before = sorted(p.name for p in (repo / "config").iterdir())
    app = cdk.App(outdir=str(tmp_path))
    net = _Net(app, "TestNet", env=_ENV)
    sfc = SfcStack(app, "TestSfcStack", env=_ENV, vpc=net.vpc,
                   opc_ua_endpoints=_OPC_UA_ENDPOINTS,
                   s3tables_stack=SfcS3TablesStack(app, "TestSfcS3Tables", env=_ENV))
    staged = Path(sfc._staged_context_path)
    assert tmp_path in staged.parents
    assert sorted(p.name for p in staged.iterdir()) == [
        ".dockerignore", "Dockerfile", "sfc-config.json"]
    config = json.loads((staged / "sfc-config.json").read_text())
    assert len(config["Sources"]) == len(_OPC_UA_ENDPOINTS)
    assert config["Targets"]["sitewise-target"]["Region"] == "${AWS_REGION}"
    servers = config["ProtocolAdapters"]["OPCUA"]["OpcuaServers"].values()
    assert all(s["Address"].startswith("opc.tcp://${SFC_OPCUA_HOST_") for s in servers)
    assert sorted(p.name for p in (repo / "config").iterdir()) == before

    # The container env fills the config's opc.tcp://${SFC_OPCUA_HOST_<SITE>}
    # placeholders, so together they must name the scheme exactly once.
    tmpl = assertions.Template.from_stack(sfc)
    (task,) = tmpl.find_resources("AWS::ECS::TaskDefinition").values()
    env = {e["Name"]: e["Value"] for e in
           task["Properties"]["ContainerDefinitions"][0]["Environment"]}
    for server in config["ProtocolAdapters"]["OPCUA"]["OpcuaServers"].values():
        var = server["Address"].removeprefix("opc.tcp://${").rstrip("}")
        resolved = server["Address"].replace("${" + var + "}", env[var])
        assert resolved.count("opc.tcp://") == 1, resolved
        assert resolved.split("//", 1)[1] in {e.split("//", 1)[1].split(":", 1)[0]
                                            for e in _OPC_UA_ENDPOINTS}


def test_asset_tree_custom_resource_runs_before_the_service(template):
    """Decision 9: the models + asset tree are created in-stack (so every
    deployment path, Workshop Studio included, gets them) before SFC starts."""
    (cr_id, resource), = [
        (lid, r) for lid, r in template.find_resources("AWS::CloudFormation::CustomResource").items()
        if "SitewiseAssetTree" in lid]
    specs = json.loads(resource["Properties"]["Specs"])
    models = {s["model"] for s in specs}
    assert {"Enterprise", "Site", "Area", "Line"} <= models
    assert len({s["external_id"] for s in specs}) == len(specs)
    (service,) = template.find_resources("AWS::ECS::Service").values()
    assert cr_id in service.get("DependsOn", [])


def test_only_the_tree_role_may_create_sitewise_assets(template):
    """The create/associate grants sit on the custom resource's role alone."""
    tree_actions: set[str] = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        acts: set[str] = set()
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            act = stmt.get("Action", [])
            acts |= {act} if isinstance(act, str) else set(act)
        if "iotsitewise:CreateAsset" in acts:
            tree_actions |= acts
            assert not any("TaskRole" in r.get("Ref", "")
                           for r in policy["Properties"]["Roles"])
    assert {"iotsitewise:CreateAsset", "iotsitewise:AssociateAssets",
            "iotsitewise:CreateAssetModel",
            "iotsitewise:UpdateAssetModel"} <= tree_actions
    assert not any(a.startswith("iotsitewise:Delete") for a in tree_actions)


# ---------------------------------------------------------------------------
# Downstream contract: constructs/attributes exposed for T5
# ---------------------------------------------------------------------------
def test_exposes_service_and_cluster_for_t5():
    _app, sfc = _build_app()
    assert sfc.service is not None
    assert sfc.cluster is not None
    assert sfc.task_definition is not None
    assert sfc.vpc is not None


def test_requires_a_vpc_source():
    """Constructing without a VPC or network stack fails loudly (SFC must share
    the OPC UA servers' VPC — ADR-0082 decision 8)."""
    app = cdk.App()
    with pytest.raises(ValueError, match="shared VPC"):
        SfcStack(app, "NoVpcSfcStack", env=_ENV, opc_ua_endpoints=_OPC_UA_ENDPOINTS)


# ---------------------------------------------------------------------------
# The asset-tree custom resource re-runs on a MODEL-SCHEMA change, not only on
# an asset-tree change. The handler runs DeploySitewiseModels.deploy_models,
# which is staged into the Lambda but is not part of the asset-tree specs, so
# CloudFormation would never re-invoke the resource on a schema-only change
# without a hash of the model script in its properties.
# ---------------------------------------------------------------------------
def _asset_tree_properties(template: assertions.Template) -> dict:
    """The one custom resource that provisions the SiteWise models + tree."""
    matches = [
        props["Properties"]
        for props in template.find_resources("AWS::CloudFormation::CustomResource").values()
        if "SpecsHash" in props.get("Properties", {})
    ]
    assert len(matches) == 1, f"expected exactly one SiteWise asset-tree custom resource, got {len(matches)}"
    return matches[0]


def test_asset_tree_resource_hashes_the_model_script(template):
    """The custom resource carries a ModelsHash of DeploySitewiseModels.py, so a
    schema-only change to that script re-triggers the reconcile (not just an
    asset-tree change)."""
    import hashlib

    from stacks.sfc_stack import SITEWISE_MODELS_SCRIPT

    props = _asset_tree_properties(template)
    assert "SpecsHash" in props, "asset-tree change must still re-trigger (SpecsHash)"
    assert "ModelsHash" in props, "a model-script change must re-trigger too (ModelsHash missing)"

    expected = hashlib.sha256(Path(SITEWISE_MODELS_SCRIPT).read_bytes()).hexdigest()
    models_hash = props["ModelsHash"]
    # The property may synthesize as a literal string or wrapped by CDK tokens;
    # assert the real digest is present either way.
    assert expected in json.dumps(models_hash), (
        "ModelsHash must be the sha256 of the current DeploySitewiseModels.py"
    )
