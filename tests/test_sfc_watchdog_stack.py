# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time tests for the SFC freshness watchdog (``stacks/_sfc_watchdog.py``).

The watchdog is a scheduled Lambda inside the SFC stack that forces a new SFC
deployment when one source's sentinel stops updating while its OPC UA server
runs. These pin its schedule, the configuration it is given, and the scope of
its own IAM role, and that it leaves the SFC task role alone. The stacks are
synthesized in-process (an injected SFC image, small stand-in OPC UA
services), like ``tests/test_sfc_stack.py``.

Run:  PYTHONPATH=. python -m pytest tests/test_sfc_watchdog_stack.py -q
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("aws_cdk") is None:
    pytest.skip("aws-cdk-lib not importable — activate the .venv and `pip install -r requirements.txt`",
                allow_module_level=True)

import aws_cdk as cdk  # noqa: E402
from aws_cdk import assertions  # noqa: E402
from aws_cdk import aws_ec2 as ec2  # noqa: E402
from aws_cdk import aws_ecs as ecs  # noqa: E402
from aws_cdk import aws_ecs_patterns as ecs_patterns  # noqa: E402

from stacks import _workshop_story  # noqa: E402
from stacks._sfc_watchdog import (  # noqa: E402
    CONFIG_ENV, WATCHDOG_LAMBDA_DIR, SfcFreshnessWatchdog, add_sfc_freshness_watchdog)
from stacks.highbyte_opcua_stack import HighByteOpcuaStack  # noqa: E402
from stacks.sfc_s3tables_stack import SfcS3TablesStack  # noqa: E402
from stacks.sfc_stack import SFC_SITE_KEYS, SfcStack, load_sfc_config_generator  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")
_ENDPOINTS = [f"opc.tcp://server-{i}.example:26543/UA/ManufacturingServer" for i in range(4)]
_IMAGE = "public.ecr.aws/docker/library/eclipse-temurin:17-jre"
LIVE = {"with_workshop": "true", "workshop_story": "live"}


class _Servers(cdk.Stack):
    """Stands in for the OPC UA stack: a VPC and one Fargate service per site."""

    def __init__(self, scope, cid, **kwargs):
        super().__init__(scope, cid, **kwargs)
        self.vpc = ec2.Vpc(self, "Vpc", max_azs=2)
        cluster = ecs.Cluster(self, "Cluster", vpc=self.vpc)
        self.services = []
        for site, construct_id in HighByteOpcuaStack.OPC_UA_SITES:
            td = ecs.FargateTaskDefinition(self, f"{construct_id}Td")
            td.add_container("opcua", image=ecs.ContainerImage.from_registry("opc"))
            self.services.append(ecs.FargateService(
                self, f"{construct_id}Service", cluster=cluster, task_definition=td,
                service_name=f"IDO-OPC-Server-{site}"))


def _build(context: dict | None = None, watchdog: bool = True):
    app = cdk.App(context=context or {})
    servers = _Servers(app, "TestServers", env=_ENV)
    s3tables = SfcS3TablesStack(app, "TestSfcS3Tables", env=_ENV)
    sfc = SfcStack(app, "TestSfcStack", env=_ENV, vpc=servers.vpc, opc_ua_endpoints=_ENDPOINTS,
                   s3tables_stack=s3tables, image=ecs.ContainerImage.from_registry(_IMAGE))
    dog = None
    if watchdog:
        dog = SfcFreshnessWatchdog(sfc, "FreshnessWatchdog", sfc_service=sfc.service,
                                   opcua_services=servers.services)
    return sfc, dog, assertions.Template.from_stack(sfc).to_json()


@pytest.fixture(scope="module")
def demo():
    return _build()


@pytest.fixture(scope="module")
def live():
    return _build({**LIVE, "story_loop_seconds": "4800"})


def _of_type(template: dict, kind: str) -> dict:
    return {lid: r for lid, r in template["Resources"].items() if r["Type"] == kind}


def _render(value) -> str:
    """A template string value with every token replaced by ``<token:...>``."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        sep, parts = value["Fn::Join"]
        return sep.join(_render(p) for p in parts)
    return "<token:" + json.dumps(value, sort_keys=True) + ">"


def _function(template: dict) -> dict:
    (fn,) = [r for lid, r in _of_type(template, "AWS::Lambda::Function").items()
             if lid.startswith("FreshnessWatchdog")]
    return fn


def _config(template: dict) -> dict:
    raw = _render(_function(template)["Properties"]["Environment"]["Variables"][CONFIG_ENV])
    # Tokens sit inside JSON strings; make them plain strings for parsing.
    return json.loads(re.sub(r"<token:(.*?)>(?=\")", lambda m: "TOKEN" + str(len(m.group(1))), raw))


def _role_id(template: dict) -> str:
    (lid,) = [lid for lid in _of_type(template, "AWS::IAM::Role") if lid.startswith("FreshnessWatchdogRole")]
    return lid


def _statements(template: dict, role_id: str) -> list[dict]:
    out = []
    for policy in _of_type(template, "AWS::IAM::Policy").values():
        if {"Ref": role_id} in policy["Properties"].get("Roles", []):
            out.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return out


def _actions(statement: dict) -> list[str]:
    act = statement["Action"]
    return [act] if isinstance(act, str) else list(act)


def _resources(statement: dict) -> list:
    res = statement["Resource"]
    return res if isinstance(res, list) else [res]


# --------------------------------------------------------------------------- schedule, function


def test_the_schedule_runs_the_function_every_three_minutes(demo):
    _sfc, _dog, template = demo
    (rule,) = _of_type(template, "AWS::Events::Rule").values()
    assert rule["Properties"]["ScheduleExpression"] == "rate(3 minutes)"
    assert rule["Properties"]["State"] == "ENABLED"
    (target,) = rule["Properties"]["Targets"]
    (fn_id,) = [lid for lid in _of_type(template, "AWS::Lambda::Function") if lid.startswith("FreshnessWatchdog")]
    assert target["Arn"] == {"Fn::GetAtt": [fn_id, "Arn"]}
    assert target["RetryPolicy"]["MaximumRetryAttempts"] == 0


def test_the_function_runs_on_its_own_role_and_log_group(demo):
    _sfc, _dog, template = demo
    props = _function(template)["Properties"]
    assert props["Role"] == {"Fn::GetAtt": [_role_id(template), "Arn"]}
    assert props["Runtime"] == "python3.12" and props["Handler"] == "handler.handler"
    assert props["Timeout"] == 60
    (log_id, log_group), = [(lid, r) for lid, r in _of_type(template, "AWS::Logs::LogGroup").items()
                            if lid.startswith("FreshnessWatchdog")]
    assert props["LoggingConfig"]["LogGroup"] == {"Ref": log_id}
    assert log_group["Properties"]["RetentionInDays"] == 7
    assert log_group["DeletionPolicy"] == "Delete"
    role = _of_type(template, "AWS::IAM::Role")[_role_id(template)]
    assert not role["Properties"].get("ManagedPolicyArns"), "no AWS-managed policy on the watchdog role"


def test_the_function_documents_its_scanner_skips(demo):
    """The environment skip like the repo's other Lambdas, and why it reserves
    no concurrency: fresh accounts can have a concurrency quota too low to
    reserve any (the restart record serializes restarts instead)."""
    _sfc, _dog, template = demo
    skips = {s["id"]: s["comment"] for s in _function(template)["Metadata"]["checkov"]["skip"]}
    assert set(skips) == {"CKV_AWS_173", "CKV_AWS_115"}
    assert "fresh accounts" in skips["CKV_AWS_115"] and "restart record" in skips["CKV_AWS_115"]
    assert "ReservedConcurrentExecutions" not in _function(template)["Properties"]


def test_the_function_ships_only_its_own_modules():
    shipped = sorted(p.name for p in Path(WATCHDOG_LAMBDA_DIR).iterdir() if p.suffix == ".py")
    assert shipped == ["handler.py", "watchdog_clock.py", "watchdog_decision.py"]


# --------------------------------------------------------------------------- configuration


def test_the_config_names_one_sentinel_per_source_and_its_server(demo):
    _sfc, _dog, template = demo
    config = _config(template)
    sentinels = load_sfc_config_generator().freshness_sentinels()
    assert [s["source"] for s in config["sources"]] == list(SFC_SITE_KEYS)
    for src, sentinel in zip(config["sources"], sentinels):
        assert (src["asset_external_id"], src["property"]) == (sentinel.asset_external_id, sentinel.prop)
        assert set(src["server"]) == {"cluster", "service"}
        assert "story_clock" not in src
    assert set(config) == {"sfc", "state_parameter", "sources"}


def test_the_servers_are_referenced_not_typed_in(demo):
    """Each server's cluster and service come from the OPC UA stack's own
    resources (cross-stack references), in source order."""
    _sfc, _dog, template = demo
    raw = json.dumps(_function(template)["Properties"]["Environment"]["Variables"][CONFIG_ENV])
    assert raw.count("Fn::ImportValue") >= len(SFC_SITE_KEYS) + 1


def test_the_live_story_site_reads_the_parameter_its_server_polls(live):
    """Pinned to HighByteOpcuaStack._attach_story_clock: the source whose
    server gets STORY_ANCHOR_PARAMETER is the one the watchdog reads it for,
    with the same loop."""
    _sfc, _dog, template = live
    clocks = {s["source"]: s.get("story_clock") for s in _config(template)["sources"]}
    env = {}
    for site, _cid in HighByteOpcuaStack.OPC_UA_SITES:
        app = cdk.App(context={**LIVE, "story_loop_seconds": "4800"})
        stack = cdk.Stack(app, "S", env=_ENV)
        td = ecs.FargateTaskDefinition(stack, "Td")
        td.add_container("opcua", image=ecs.ContainerImage.from_registry("opc"))
        HighByteOpcuaStack._attach_story_clock(stack, site, SimpleNamespace(task_definition=td))
        (task_definition,) = _of_type(assertions.Template.from_stack(stack).to_json(),
                                      "AWS::ECS::TaskDefinition").values()
        (container,) = task_definition["Properties"]["ContainerDefinitions"]
        env[site.lower()] = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    for source, clock in clocks.items():
        polled = env[source].get(_workshop_story.ANCHOR_PARAMETER_ENV_NAME)
        if polled is None:
            assert clock is None, source
            continue
        assert clock == {"parameter": polled, "loop_seconds": int(env[source]["STORY_LOOP_SECONDS"]),
                         "rows": load_sfc_config_generator().dataset_rows(), "interval_seconds": 5}
    assert sum(1 for c in clocks.values() if c) == 1


def test_the_default_story_loop_is_the_eight_hour_loop():
    _sfc, _dog, template = _build(LIVE)
    (clock,) = [s["story_clock"] for s in _config(template)["sources"] if "story_clock" in s]
    assert clock["loop_seconds"] == _workshop_story.DEFAULT_LOOP_SECONDS


def test_a_frozen_workshop_has_no_story_clock_source():
    _sfc, _dog, template = _build({"with_workshop": "true", "workshop_story": "frozen"})
    assert all("story_clock" not in s for s in _config(template)["sources"])


def test_the_servers_must_match_the_sources():
    app = cdk.App()
    servers = _Servers(app, "TestServers", env=_ENV)
    sfc = SfcStack(app, "TestSfcStack", env=_ENV, vpc=servers.vpc, opc_ua_endpoints=_ENDPOINTS,
                   image=ecs.ContainerImage.from_registry(_IMAGE))
    with pytest.raises(ValueError):
        SfcFreshnessWatchdog(sfc, "FreshnessWatchdog", sfc_service=sfc.service,
                             opcua_services=servers.services[:-1])


# --------------------------------------------------------------------------- IAM scope


def _statement_map(template: dict) -> dict[tuple[str, ...], dict]:
    return {tuple(sorted(_actions(s))): s for s in _statements(template, _role_id(template))}


def test_the_role_grants_exactly_the_watchdogs_calls(demo):
    _sfc, _dog, template = demo
    assert set(_statement_map(template)) == {
        ("logs:CreateLogStream", "logs:PutLogEvents"),
        ("iotsitewise:BatchGetAssetPropertyValue", "iotsitewise:DescribeAsset"),
        ("ecs:DescribeServices", "ecs:UpdateService"),
        ("ecs:ListTasks",),
        ("ecs:DescribeTasks",),
        ("ssm:GetParameter", "ssm:PutParameter"),
    }


def test_sitewise_is_read_only_on_this_accounts_assets(demo):
    _sfc, _dog, template = demo
    statement = _statement_map(template)[("iotsitewise:BatchGetAssetPropertyValue", "iotsitewise:DescribeAsset")]
    (resource,) = _resources(statement)
    assert _render(resource).endswith(":iotsitewise:us-east-1:111122223333:asset/*")
    actions = {a for s in _statements(template, _role_id(template)) for a in _actions(s)}
    assert {a for a in actions if a.startswith("iotsitewise:")} == {
        "iotsitewise:BatchGetAssetPropertyValue", "iotsitewise:DescribeAsset"}


def test_only_the_sfc_service_can_be_updated(demo):
    _sfc, _dog, template = demo
    statement = _statement_map(template)[("ecs:DescribeServices", "ecs:UpdateService")]
    (service_id,) = _of_type(template, "AWS::ECS::Service")
    assert _resources(statement) == [{"Ref": service_id}]


def test_task_reads_are_confined_to_the_sfc_and_server_clusters(demo):
    _sfc, _dog, template = demo
    statements = _statement_map(template)
    listing = statements[("ecs:ListTasks",)]
    assert _resources(listing) == ["*"]
    clusters = listing["Condition"]["ArnEquals"]["ecs:cluster"]
    assert len(clusters) == 2                                  # SFC's cluster and the servers' one
    describing = [_render(r) for r in _resources(statements[("ecs:DescribeTasks",)])]
    assert len(describing) == 2 and all(":task/" in r and r.endswith("/*") for r in describing)


class _SharedClusterServers(cdk.Stack):
    """The OPC UA stack's real shape: one NetworkLoadBalancedFargateService per
    site on the VPC and no cluster passed, so all four share the VPC's default
    cluster, which CDK creates on the JavaScript side (``layout="default"``).
    ``layout="imported"`` puts two servers on an imported cluster and two on an
    owned one."""

    def __init__(self, scope, cid, *, layout: str, **kwargs):
        super().__init__(scope, cid, **kwargs)
        self.vpc = ec2.Vpc(self, "Vpc", max_azs=2)
        self.services = []
        if layout == "default":
            for site, construct_id in HighByteOpcuaStack.OPC_UA_SITES:
                self.services.append(ecs_patterns.NetworkLoadBalancedFargateService(
                    self, f"{construct_id}Service", service_name=f"IDO-OPC-Server-{site}",
                    task_image_options=ecs_patterns.NetworkLoadBalancedTaskImageOptions(
                        image=ecs.ContainerImage.from_registry("opc"), container_port=26543),
                    public_load_balancer=False, listener_port=26543, vpc=self.vpc).service)
            return
        imported = ecs.Cluster.from_cluster_attributes(
            self, "Imported", cluster_name="opc-imported", vpc=self.vpc, security_groups=[])
        owned = ecs.Cluster(self, "Owned", vpc=self.vpc)
        for n, (site, construct_id) in enumerate(HighByteOpcuaStack.OPC_UA_SITES):
            td = ecs.FargateTaskDefinition(self, f"{construct_id}Td")
            td.add_container("opcua", image=ecs.ContainerImage.from_registry("opc"))
            self.services.append(ecs.FargateService(
                self, f"{construct_id}Service", cluster=imported if n < 2 else owned,
                task_definition=td, service_name=f"IDO-OPC-Server-{site}"))


class _FreshClusterProxies:
    """A service whose ``cluster`` is a new, still-live Python object on every
    read. That is what jsii hands out for a cluster created on the JavaScript
    side (it keeps no Python object for it); holding each one in ``kept``
    removes the luck of a freed proxy's ``id()`` being reused."""

    def __init__(self, service, kept: list):
        self._service, self._kept = service, kept

    def __getattr__(self, name):
        return getattr(self._service, name)

    @property
    def cluster(self):
        proxy = self._service.cluster
        self._kept.append(proxy)
        return proxy


def _build_shared(layout: str, kept: list | None = None) -> dict:
    app = cdk.App()
    servers = _SharedClusterServers(app, "TestServers", layout=layout, env=_ENV)
    s3tables = SfcS3TablesStack(app, "TestSfcS3Tables", env=_ENV)
    sfc = SfcStack(app, "TestSfcStack", env=_ENV, vpc=servers.vpc, opc_ua_endpoints=_ENDPOINTS,
                   s3tables_stack=s3tables, image=ecs.ContainerImage.from_registry(_IMAGE))
    services = servers.services if kept is None else [_FreshClusterProxies(s, kept)
                                                      for s in servers.services]
    SfcFreshnessWatchdog(sfc, "FreshnessWatchdog", sfc_service=sfc.service, opcua_services=services)
    return assertions.Template.from_stack(sfc).to_json()


def _listed_clusters(template: dict) -> tuple[list[str], list[str]]:
    """The ListTasks ``ecs:cluster`` condition values and the DescribeTasks
    resources, each rendered as a comparable string."""
    statements = _statement_map(template)
    listing = statements[("ecs:ListTasks",)]["Condition"]["ArnEquals"]["ecs:cluster"]
    describing = _resources(statements[("ecs:DescribeTasks",)])
    return ([json.dumps(c, sort_keys=True) for c in listing],
            [_render(r) for r in describing])


#: Distinct clusters per layout: SFC's own cluster plus the servers' ones.
_SHARED_LAYOUTS = {"default": 2, "imported": 3}


@pytest.mark.parametrize("layout", sorted(_SHARED_LAYOUTS))
def test_each_cluster_is_listed_once_however_the_cluster_objects_arrive(layout):
    """Two packages of one commit once differed: the OPC UA servers' shared
    cluster appeared four times in ListTasks in one and once in the other.
    The dedup keyed on ``id()`` of the cluster object, and every read of
    ``service.cluster`` here is a new Python object for the same cluster."""
    kept: list = []
    listing, describing = _listed_clusters(_build_shared(layout, kept))
    objects_per_cluster: dict[str, set[int]] = {}
    for proxy in kept:
        objects_per_cluster.setdefault(proxy.node.path, set()).add(id(proxy))
    assert any(len(ids) > 1 for ids in objects_per_cluster.values()), (
        "precondition: one cluster reached the watchdog as several live Python objects")
    assert len(listing) == len(set(listing)) == _SHARED_LAYOUTS[layout], listing
    assert len(describing) == len(set(describing)) == _SHARED_LAYOUTS[layout], describing


def test_the_production_shape_lists_the_shared_default_cluster_once():
    """The real objects, no wrapper: the four servers' VPC default cluster is
    imported into the SFC stack and listed exactly once, next to SFC's own."""
    listing, describing = _listed_clusters(_build_shared("default"))
    assert len(listing) == len(set(listing)) == 2, listing
    assert sum("Fn::ImportValue" in c and "EcsDefaultCluster" in c for c in listing) == 1, listing
    assert len(describing) == len(set(describing)) == 2, describing


def test_the_only_wildcard_resource_is_the_conditioned_task_listing(demo):
    _sfc, _dog, template = demo
    for statement in _statements(template, _role_id(template)):
        if "*" in _resources(statement):
            assert _actions(statement) == ["ecs:ListTasks"] and statement.get("Condition")


def test_ssm_is_the_restart_record_only_outside_the_live_story(demo):
    _sfc, _dog, template = demo
    statement = _statement_map(template)[("ssm:GetParameter", "ssm:PutParameter")]
    (param_id,) = _of_type(template, "AWS::SSM::Parameter")
    assert param_id.startswith("FreshnessWatchdogRestartRecord")
    assert param_id in json.dumps(_resources(statement))
    assert ("ssm:GetParameter",) not in _statement_map(template)


def test_the_live_story_reads_exactly_the_two_story_parameters(live):
    """The anchor parameter the story server polls, and the replay window it
    replays and marks done; read only."""
    _sfc, _dog, template = live
    statements = [s for s in _statements(template, _role_id(template)) if _actions(s) == ["ssm:GetParameter"]]
    (statement,) = statements
    rendered = sorted(_render(r) for r in _resources(statement))
    prefix = ":ssm:us-east-1:111122223333:parameter"
    assert [r.split(prefix, 1)[1] for r in rendered] == sorted(
        [_workshop_story.ANCHOR_PARAMETER_NAME, _workshop_story.BACKFILL_PARAMETER_NAME])


def test_the_live_story_watches_the_one_time_history_replay(live):
    _sfc, _dog, template = live
    config = _config(template)
    (story,) = [s for s in config["sources"] if "story_clock" in s]
    assert config["replay"] == {"source": load_sfc_config_generator().HISTORY_SOURCE_ID,
                                "parameter": _workshop_story.BACKFILL_PARAMETER_NAME,
                                "server": story["server"]}


def test_the_replay_switched_off_has_no_replay_entry_and_no_backfill_read():
    _sfc, _dog, template = _build({**LIVE, "story_feed_backfill": "false"})
    assert "replay" not in _config(template)
    reads = [_render(r) for s in _statements(template, _role_id(template))
             if _actions(s) == ["ssm:GetParameter"] for r in _resources(s)]
    assert len(reads) == 1 and reads[0].endswith(_workshop_story.ANCHOR_PARAMETER_NAME)


# --------------------------------------------------------------------------- one SiteWise writer


def _built(context: dict, *, highbyte_enabled: bool):
    app = cdk.App(context=context)
    servers = _Servers(app, "TestServers", env=_ENV)
    sfc = SfcStack(app, "TestSfcStack", env=_ENV, vpc=servers.vpc, opc_ua_endpoints=_ENDPOINTS,
                   image=ecs.ContainerImage.from_registry(_IMAGE))
    dog = add_sfc_freshness_watchdog(sfc, sfc_service=sfc.service, opcua_services=servers.services,
                                     highbyte_enabled=highbyte_enabled)
    return sfc, dog


def test_no_watchdog_when_the_hub_also_writes_the_same_sitewise_properties():
    """--highbyte-sitewise-pipeline enabled: the hub's pipeline keeps every sentinel
    fresh, so a stalled SFC source would never be seen. The watchdog is not
    built, and the synth says why."""
    sfc, dog = _built({}, highbyte_enabled=True)
    assert dog is None
    template = assertions.Template.from_stack(sfc).to_json()
    assert not [lid for lid in template["Resources"] if lid.startswith("FreshnessWatchdog")]
    warnings = assertions.Annotations.from_stack(sfc).find_warning(
        "*", assertions.Match.string_like_regexp("SFC freshness watchdog not built"))
    (warning,) = warnings
    assert "highbyte_sitewise_pipeline=disabled" in str(warning.entry.data)


@pytest.mark.parametrize("context,highbyte", [({"highbyte_sitewise_pipeline": "disabled"}, True), ({}, False)],
                         ids=["hub-pipeline-off", "no-hub"])
def test_the_watchdog_is_built_when_sfc_is_the_only_sitewise_writer(context, highbyte):
    sfc, dog = _built(context, highbyte_enabled=highbyte)
    assert isinstance(dog, SfcFreshnessWatchdog)
    assert not assertions.Annotations.from_stack(sfc).find_warning(
        "*", assertions.Match.string_like_regexp("SFC freshness watchdog"))


def test_the_restart_record_starts_empty(demo):
    _sfc, _dog, template = demo
    (param,) = _of_type(template, "AWS::SSM::Parameter").values()
    assert param["Properties"]["Value"] == "[]" and param["Properties"]["Type"] == "String"


def test_the_sfc_task_role_is_unchanged():
    """The watchdog grants nothing to SFC itself."""
    def task_role_policies(template: dict) -> list:
        (task_role,) = [lid for lid in _of_type(template, "AWS::IAM::Role") if "TaskRole" in lid]
        return [p["Properties"]["PolicyDocument"] for p in _of_type(template, "AWS::IAM::Policy").values()
                if {"Ref": task_role} in p["Properties"].get("Roles", [])]

    _s, _d, with_dog = _build()
    _s, _d, without = _build(watchdog=False)
    assert task_role_policies(with_dog) == task_role_policies(without)
    assert not [lid for lid in _of_type(with_dog, "AWS::IAM::Role")
                if "TaskRole" in lid and lid.startswith("FreshnessWatchdog")]
