# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SFC freshness watchdog: restarts SFC when one source silently stops delivering.

SFC (v1.11.0) recovers a source only when its OPC UA connection fails. After
an OPC UA server task is replaced, or after hours of SFC near its memory
limit, a source can stay "connected" while its subscription delivers
nothing: that source's SiteWise values stop, every other source continues,
SFC's health check stays green, and only an SFC restart (a new deployment)
brings the source back. The deploy-time coupling in ``sfc_stack.py``
(``OPC_UA_SERVER_REVISIONS``) covers a server replaced by a forward deploy
only.

This construct adds, inside the SFC stack:

* a Lambda (``lambda-sfc-watchdog``) run every three minutes by an
  EventBridge schedule. For each SFC source it reads the newest stored value
  of one sentinel channel, a channel whose value changes on every server
  tick (``sfc_config.freshness_sentinels``), and when that value is more
  than five minutes old while the source's server has run for five minutes,
  it forces a new SFC deployment, at most twice an hour and never within 15
  minutes of the last SFC deployment or watchdog restart
  (``watchdog_decision.py``). On the live story with the one-time history
  replay it also reads the replay's window parameter: a replay still not
  done well after a clean one would have finished restarts SFC once (an
  ordinary pass for the server), and once more after every pass must have
  failed, which is the reader the server's one revival of a stopped replay
  needs;
* its own IAM role: SiteWise reads on this account's assets, ECS describe
  and update on the SFC service only, task reads on the SFC and OPC UA
  clusters, its own log group, its restart record and, on the live story,
  the story clock and replay parameters the story site's server polls. The
  SFC task role is not touched;
* the restart record, an SSM parameter only this role writes. A run
  reserves its restart there before it updates SFC, and only the run whose
  write is the next version does, so duplicate runs restart SFC once. No
  reserved concurrency: fresh accounts can have too low a quota to reserve
  any.

It is built only when SFC is the only SiteWise writer (:func:`skip_reason`).
The configuration (sources, sentinels, services) is computed at synth and
passed in one environment variable; the Lambda holds no site names.
"""
from __future__ import annotations

import os
from typing import Optional, Sequence

from aws_cdk import (
    Annotations,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ecs as ecs,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_ssm as ssm,
)
from constructs import Construct

from stacks import _highbyte_options, _workshop_story
from stacks._checkov_skips import _LAMBDA_ENV_CMK, checkov_skip
from stacks.sfc_stack import SFC_SITE_KEYS, load_sfc_config_generator

WATCHDOG_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda-sfc-watchdog")
#: Environment variable carrying the configuration (``handler.CONFIG_ENV``).
CONFIG_ENV = "WATCHDOG_CONFIG"
SCHEDULE_MINUTES = 3
#: The OPC UA server's tick (server.mjs UPDATE_INTERVAL_MS default).
SERVER_TICK_SECONDS = 5

SITEWISE_READ_ACTIONS = ["iotsitewise:DescribeAsset", "iotsitewise:BatchGetAssetPropertyValue"]

_NO_RESERVED_CONCURRENCY = (
    "CKV_AWS_115",
    "No reserved concurrency: Lambda keeps 100 units of an account's concurrency "
    "unreserved, and fresh accounts can have a quota too low to reserve any, so a "
    "reservation could fail the stack's creation. Restarts are serialized by the "
    "restart record instead: a run reserves its restart by writing the version "
    "right after the one it read, and only that run updates SFC.",
)


def skip_reason(node, *, highbyte_enabled: bool) -> Optional[str]:
    """Why the watchdog is not built for this synth, or None.

    It judges SFC by the newest SiteWise value of each sentinel. With the
    HighByte hub's NameSpace_to_SiteWise pipeline on (``deploy.sh --highbyte
    --sfc --highbyte-sitewise-pipeline enabled``) the hub writes the same
    properties, so they stay fresh while an SFC source is stalled and the
    watchdog would never act. deploy.sh's default with both is ``disabled``."""
    if highbyte_enabled and _highbyte_options.sitewise_pipeline_from_node(node) == "enabled":
        return ("SFC freshness watchdog not built: the HighByte NameSpace_to_SiteWise pipeline writes "
                "the same SiteWise properties as SFC, so a stalled SFC source would still look fresh. "
                f"Synthesize with -c {_highbyte_options.SITEWISE_PIPELINE_KEY}=disabled "
                "(deploy.sh --highbyte-sitewise-pipeline disabled, its default with HighByte and SFC) "
                "for one SiteWise writer and the watchdog.")
    return None


def add_sfc_freshness_watchdog(scope: Construct, *, sfc_service: ecs.BaseService,
                               opcua_services: Sequence[ecs.BaseService],
                               highbyte_enabled: bool) -> Optional["SfcFreshnessWatchdog"]:
    """Build the watchdog in ``scope`` (the SFC stack), or, when SFC is not the
    only SiteWise writer (:func:`skip_reason`), add a synth warning saying why
    and build nothing."""
    reason = skip_reason(scope.node, highbyte_enabled=highbyte_enabled)
    if reason:
        Annotations.of(scope).add_warning(reason)
        return None
    return SfcFreshnessWatchdog(scope, "FreshnessWatchdog", sfc_service=sfc_service,
                                opcua_services=opcua_services)


class SfcFreshnessWatchdog(Construct):
    """The scheduled watchdog for one SFC service and its OPC UA servers.

    ``opcua_services`` are the OPC UA servers' ECS services in SFC source
    order (``sfc_stack.SFC_SITE_KEYS``, the order ``HighByteOpcuaStack``
    builds them in)."""

    def __init__(self, scope: Construct, construct_id: str, *, sfc_service: ecs.BaseService,
                 opcua_services: Sequence[ecs.BaseService]) -> None:
        super().__init__(scope, construct_id)
        self._stack = Stack.of(self)
        self._sfc_service = sfc_service
        self._servers = dict(zip(SFC_SITE_KEYS, opcua_services, strict=True))
        self.log_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK,
                                       removal_policy=RemovalPolicy.DESTROY)
        self.restart_record = ssm.StringParameter(
            self, "RestartRecord", string_value="[]",
            description="SFC freshness watchdog: its SFC restarts of the last day (epoch seconds)")
        self.config = self._config()
        self.role = iam.Role(
            self, "Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="SFC freshness watchdog: reads SiteWise and ECS, restarts the SFC service")
        self._grant()
        self.function = lambda_.Function(
            self, "Function",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(WATCHDOG_LAMBDA_DIR, exclude=["__pycache__", "*.pyc"]),
            timeout=Duration.seconds(60),
            memory_size=256,
            role=self.role,
            log_group=self.log_group,
            retry_attempts=0,
            environment={CONFIG_ENV: self._stack.to_json_string(self.config)},
            description="ADT - SFC freshness watchdog (restarts SFC when a source stops delivering)",
        )
        checkov_skip(self.function, [_LAMBDA_ENV_CMK, _NO_RESERVED_CONCURRENCY])
        self.schedule = events.Rule(
            self, "Schedule",
            schedule=events.Schedule.rate(Duration.minutes(SCHEDULE_MINUTES)),
            targets=[targets.LambdaFunction(self.function, retry_attempts=0)],
            description="Runs the SFC freshness watchdog",
        )

    # ------------------------------------------------------------------ config

    def _story_clock(self, source: str, rows: int) -> Optional[dict]:
        """The clock config of a source whose server follows the story anchor
        (the live story's site, as ``HighByteOpcuaStack._attach_story_clock``
        configures it), else None."""
        if (_workshop_story.from_node(self.node) != "live"
                or source != _workshop_story.ANCHOR_STORY_SITE.lower()):
            return None
        loop = _workshop_story.loop_from_node(self.node) or _workshop_story.DEFAULT_LOOP_SECONDS
        return {"parameter": _workshop_story.ANCHOR_PARAMETER_NAME, "loop_seconds": loop,
                "rows": rows, "interval_seconds": SERVER_TICK_SECONDS}

    def _config(self) -> dict:
        generator = load_sfc_config_generator()
        sentinels = generator.freshness_sentinels()
        rows = generator.dataset_rows()
        if [s.source for s in sentinels] != list(self._servers):
            raise ValueError(f"sentinel sources {[s.source for s in sentinels]} "
                             f"do not match the OPC UA servers {list(self._servers)}")
        sources = []
        for sentinel in sentinels:
            server = self._servers[sentinel.source]
            source = {"source": sentinel.source, "asset_external_id": sentinel.asset_external_id,
                      "property": sentinel.prop,
                      "server": {"cluster": server.cluster.cluster_arn, "service": server.service_name}}
            clock = self._story_clock(sentinel.source, rows)
            if clock:
                source["story_clock"] = clock
            sources.append(source)
        config = {"sfc": {"cluster": self._sfc_service.cluster.cluster_arn,
                          "service": self._sfc_service.service_name},
                  "state_parameter": self.restart_record.parameter_name,
                  "sources": sources}
        replay = self._replay(generator, sources)
        if replay:
            config["replay"] = replay
        return config

    def _replay(self, generator, sources: list[dict]) -> Optional[dict]:
        """The one-time history replay's check, when this synth packages the
        replay (the live story with ``story_feed_backfill`` on): SFC reads the
        replay through its own source, which no live sentinel covers, so the
        watchdog reads the window parameter the story server polls."""
        if not _workshop_story.backfill_from_node(self.node):
            return None
        story = [s for s in sources if "story_clock" in s]
        if len(story) != 1:
            return None
        return {"source": generator.HISTORY_SOURCE_ID, "parameter": _workshop_story.BACKFILL_PARAMETER_NAME,
                "server": story[0]["server"]}

    # ------------------------------------------------------------------ IAM

    def _clusters(self) -> list[ecs.ICluster]:
        """The SFC and OPC UA clusters, each once, in service order.

        Keyed on the cluster construct's path: owned and imported clusters
        both have one, fixed when the construct is created, and it needs no
        token resolution (a cross-stack ARN becomes Fn::ImportValue only at
        synth). Not ``id()``: jsii keeps no Python object for a cluster made
        on the JavaScript side (the OPC UA patterns' VPC default cluster),
        so every ``service.cluster`` read is a new proxy, and a freed one's
        id can be reused or not, which made the list differ run to run."""
        clusters: dict[str, ecs.ICluster] = {}
        for service in [self._sfc_service, *self._servers.values()]:
            cluster = service.cluster
            clusters.setdefault(cluster.node.path, cluster)
        return list(clusters.values())

    def _grant(self) -> None:
        stack = self._stack
        self.log_group.grant_write(self.role)
        self._allow(SITEWISE_READ_ACTIONS,
                    [stack.format_arn(service="iotsitewise", resource="asset", resource_name="*")])
        self._allow(["ecs:DescribeServices", "ecs:UpdateService"], [self._sfc_service.service_arn])
        clusters = self._clusters()
        # ListTasks authorizes on the cluster through its condition key; there
        # is no task or service resource to name.
        self.role.add_to_policy(iam.PolicyStatement(
            actions=["ecs:ListTasks"], resources=["*"],
            conditions={"ArnEquals": {"ecs:cluster": [c.cluster_arn for c in clusters]}}))
        self._allow(["ecs:DescribeTasks"],
                    [stack.format_arn(service="ecs", resource="task", resource_name=f"{c.cluster_name}/*")
                     for c in clusters])
        self._allow(["ssm:GetParameter", "ssm:PutParameter"], [self.restart_record.parameter_arn])
        story = {s["story_clock"]["parameter"] for s in self.config["sources"] if "story_clock" in s}
        if "replay" in self.config:
            story.add(self.config["replay"]["parameter"])
        if story:
            self._allow(["ssm:GetParameter"], [
                stack.format_arn(service="ssm", resource="parameter", resource_name=name.lstrip("/"))
                for name in sorted(story)])

    def _allow(self, actions: list[str], resources: list[str]) -> None:
        self.role.add_to_policy(iam.PolicyStatement(actions=actions, resources=resources))
