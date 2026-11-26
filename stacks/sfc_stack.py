# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
SFC Runtime Stack

This stack runs Shop Floor Connectivity (SFC) as an ECS Fargate service in the
SAME VPC as the OPC UA servers, so it can read their telemetry and write it to
AWS IoT SiteWise and S3 Tables (ADR-0082). Where the HighByte
``NameSpace_to_SiteWise`` pipeline is off (``highbyte_sitewise_pipeline=disabled``,
the live workshop package) or no hub is deployed, SFC is the only continuous
SiteWise writer (``deploy.sh --highbyte --sfc`` defaults to the pipeline off);
with ``--highbyte-sitewise-pipeline enabled`` the pipeline stays on, so both
write the same properties. A deployed hub keeps the UNS role
(ADR-0082 decision 1).

It consumes, rather than recreates, the network and S3 Tables infrastructure:

- the shared VPC (from ``NetworkStack`` / ``HighByteOpcuaStack``), so the task
  lands beside the OPC UA servers,
- one ``opc.tcp://<host>:26543/UA/ManufacturingServer`` endpoint string per site
  the OPC UA stack exposes as ``opc_ua_endpoints``, and
- ``SfcS3TablesStack``'s exported identifiers (``table_bucket_name``,
  ``namespace_name``, ``table_name``, ``table_arn``) and its narrow
  ``sfc_writer_policy`` (ADR-0082 decision 4).

These arrive as constructor parameters — the flag/packaging wiring (T5) passes
them in — mirroring how ``HighByteOpcuaStack`` receives its ``network_stack``.

ADR-0082 decision 2 — SFC writes SiteWise through the ``Assets`` mapping mode,
    never ``AssetCreation``. The task role therefore gets the SiteWise
    data-plane set only — ``BatchPutAssetPropertyValue``, ``DescribeAsset``,
    ``DescribeAssetModel``, ``ListAssets``,
    ``ListAssetModels`` and ``ListAssetModelProperties`` — and deliberately NOT
    ``CreateAssetModel`` / ``UpdateAssetModel``, so the 28 pre-created asset
    models (``highbyte/DeploySitewiseModels.py``) cannot be mutated by the
    writer (see ``_create_sitewise_policy``).

ADR-0082 decision 6 — the SFC configuration is GENERATED from the tag map at
    synth, straight into the image's staged build context under ``cdk.out``
    (``stacks/_staging.py``). Nothing is written into the checkout, which is
    the repo's actual synth-purity rule, and no deploy.sh step is needed, so
    the same synth works for ``cdk deploy``, deploy.sh and Workshop Studio.
    The generator is loaded BY PATH (it lives outside the stacks' import
    path), the same pattern ``app.py`` uses for the dataset generator.

ADR-0082 decision 9 — the SiteWise asset tree SFC writes into is created by a
    custom resource in this stack (``lambda-sfc-sitewise-tree``): the 28
    models, then HighByte's Enterprise/Site/Area/Line/process tree with a
    unique external id per asset. It runs with its own deploy-time role; the
    SFC task role stays write-only. The service depends on it, so SFC never
    starts before its assets exist.

ADR-0082 decision 8 — SFC runs in the same VPC and adds NO security-group rule.
    ``highbyte_opcua_stack.py`` grants OPC UA ingress to ``vpc.vpc_cidr_block``
    on port 26543 (NOT SG-to-SG, deliberately: the NLB IP targets do not
    preserve the client IP, so an SG-to-SG rule would not match). An in-VPC SFC
    task is therefore already permitted, so this stack creates no ingress rule
    to the OPC UA servers — see the test that asserts their absence.

SFC's runtime image is a build asset, not a public registry image (see
``DEFAULT_SFC_DOCKER_CONTEXT`` and the ``image`` parameter): upstream
``awslabs/industrial-shopfloor-connect`` ships per-adapter tar.gz release
bundles that run on a JVM (``sfc-main -config <file>``), with no official
published container image, so the HighByte ``from_registry`` precedent cannot
apply and the OPC UA ``from_asset`` precedent does.

This stack imports the VPC / S3 Tables handles it is given but owns its own
task definition, IAM and service, so it synthesizes on its own for the unit
lane when those handles are supplied.
"""

import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import asdict
from types import ModuleType
from typing import Optional, Sequence

from aws_cdk import (
    Stack,
    CfnOutput,
    CustomResource,
    Duration,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_lambda as lambda_,
    custom_resources as cr,
)
from constructs import Construct

from stacks import _highbyte_options
from stacks._os_patch import patched_image_options
from stacks._staging import stage_tree
from stacks.highbyte_opcua_stack import HighByteOpcuaStack
from stacks import _workshop_story

# OPC UA listener/container port — the value highbyte_opcua_stack.py uses and
# the port its VPC-CIDR ingress rule already opens (ADR-0082 decision 8).
OPC_UA_PORT = 26543

# SFC task sizing. SFC is a JVM process reading four OPC UA sources on a 5 s
# schedule and batching to two AWS targets. 1 vCPU / 4 GB (a valid Fargate
# pairing at 1 vCPU): at 2 GB the task was killed by ECS for memory (exit 137)
# after about 70 minutes, and a second account sat at 94-95% memory. Exit 137
# is the container limit, not a Java heap error: memory outside the heap
# (metaspace, threads, direct buffers, probably the Parquet/Iceberg writers
# behind the S3 Tables target; not yet measured) outgrew the ~512 MB the 2 GB
# limit left over.
SFC_CPU = 1024
SFC_MEMORY = 4096
# With the one-time story history replay packaged (the replay ADR) SFC carries
# a second S3 Tables target: its own class loader with its own copy of the
# S3 Tables writer libraries and its own Iceberg catalog client, on a task
# that already peaked at 99.95 % of 4 GB. That target was not measured, so the
# task gets 8 GB at the same 1 vCPU (a valid Fargate size, no added vCPU
# quota). SFC_JAVA_TOOL_OPTIONS is unchanged: 37.5 % of 8192 is a 3 GB heap
# (and the same default direct-buffer limit) with 5 GB beside it, both twice
# what ran at 4 GB. Replay off keeps 4 GB and an unchanged task definition.
SFC_MEMORY_WITH_REPLAY = 8192

# JVM heap sizing for the SFC container. Without it the JVM defaults to 25% of
# the container limit, which SFC fills within hours: target writers start
# timing out and S3 Tables / SiteWise writes stall. 37.5% of 4096 keeps the
# 1.5 GB heap that already worked at 2 GB / 75% and gives all the added memory
# to the native side (about 2.5 GB), which is what ran out (exit 137). A larger
# heap would also raise the JVM's default direct-buffer limit, which follows
# the maximum heap.
SFC_JAVA_TOOL_OPTIONS = "-XX:MaxRAMPercentage=37.5"

# Container path of the generated SFC configuration (decision 6), baked into
# the image; the container reads it with `-config <SFC_CONFIG_PATH>`.
SFC_CONFIG_MOUNT = "/etc/sfc"
SFC_CONFIG_FILENAME = "sfc-config.json"
SFC_CONFIG_PATH = f"{SFC_CONFIG_MOUNT}/{SFC_CONFIG_FILENAME}"

# ADR-0082 decision 2: the SiteWise data-plane action set for the SFC writer.
# `Assets` mapping mode (never `AssetCreation`) needs to put property values and
# read the existing asset/model shape — nothing that CREATES or UPDATES a model.
# `CreateAssetModel` / `UpdateAssetModel` are DELIBERATELY excluded so the 28
# pre-created models (highbyte/DeploySitewiseModels.py) cannot be mutated by the
# writer; excluding them is the whole point of decision 2.
SFC_SITEWISE_WRITE_ACTIONS = [
    "iotsitewise:BatchPutAssetPropertyValue",
]
SFC_SITEWISE_READ_ACTIONS = [
    "iotsitewise:DescribeAsset",
    "iotsitewise:DescribeAssetModel",
    "iotsitewise:ListAssets",
    "iotsitewise:ListAssetModels",
    "iotsitewise:ListAssetModelProperties",
]
SFC_SITEWISE_ACTIONS = SFC_SITEWISE_WRITE_ACTIONS + SFC_SITEWISE_READ_ACTIONS

# Site keys, in the ORDER highbyte_opcua_stack builds its OPC UA services and
# therefore the order `opc_ua_endpoints` arrives in — derived from that stack's
# own OPC_UA_SITES, so a new server (Indianapolis, ADR-0072) cannot be dropped
# here. They must also match the generator's SITES keys, because the config's
# ${SFC_OPCUA_HOST_<SITE>} placeholders are derived from those;
# tests/test_sfc_stack.py pins the agreement. The generator itself is still
# never imported by this stack (ADR-0082 decision 6).
SFC_SITE_KEYS: tuple[str, ...] = tuple(
    name.lower() for name, _construct_id in HighByteOpcuaStack.OPC_UA_SITES
)

# Default SFC image build context. SFC has NO official published container image
# — upstream ships per-adapter tar.gz release bundles that run on a JVM
# (`sfc-main -config`). The image is therefore a build asset (the OPC UA
# `from_asset` precedent), never a registry pull (the HighByte `from_registry`
# precedent). The Dockerfile that assembles a JVM base + the extracted SFC
# bundles is a SEPARATE deliverable; this path names its intended home so the
# default is honest rather than an invented registry name, and the caller (T5 /
# deploy) can override `image` explicitly.
DEFAULT_SFC_DOCKER_CONTEXT = os.path.join(
    os.path.dirname(__file__), "..", "docker", "sfc"
)

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
# The generator (ADR-0082 decision 6) and the asset-tree custom resource
# (decision 9). The generator is outside the stacks' import path, so it is
# loaded by path; the Lambda ships the tree applier and the model script.
SFC_CONFIG_GENERATOR = os.path.join(
    _REPO_ROOT, "agentic_digital_thread", "agents", "demo", "sfc_config.py")
SITEWISE_TREE_MODULE = os.path.join(
    _REPO_ROOT, "agentic_digital_thread", "agents", "demo", "sitewise_tree.py")
SITEWISE_MODELS_SCRIPT = os.path.join(_REPO_ROOT, "highbyte", "DeploySitewiseModels.py")
SITEWISE_TREE_LAMBDA_DIR = os.path.join(_REPO_ROOT, "lambda-sfc-sitewise-tree")


def _models_script_hash() -> str:
    """sha256 of DeploySitewiseModels.py, for the asset-tree CR's ModelsHash.

    The tree custom resource's handler runs ``DeploySitewiseModels.deploy_models``
    (the model schemas) as well as ``ensure_tree`` (the asset tree). The script
    is staged into the Lambda but is NOT part of the asset-tree specs, and
    CloudFormation only re-invokes the resource when a property changes. Without
    this hash, a schema-only change (a new measurement, a corrected dataType)
    would update the Lambda code yet never re-run, leaving existing SFC
    environments on stale model schemas. Hashing the script makes such a change
    re-trigger the reconcile too.
    """
    with open(SITEWISE_MODELS_SCRIPT, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()

# What the tree custom resource may do. Deploy-time only, on its own role:
# models (create, or add the properties an older model lacks) and assets
# (create, tag with the external id, associate). Nothing deletes. The SFC
# task role gets none of this. Reads go on '*'; the writes name this
# account's asset and asset-model ARNs, except CreateAssetModel, which has no
# resource type in the Service Authorization Reference.
SITEWISE_TREE_READ_ACTIONS = [
    "iotsitewise:ListAssetModels",
    "iotsitewise:DescribeAssetModel",
    "iotsitewise:ListAssets",
    "iotsitewise:ListAssociatedAssets",
    "iotsitewise:DescribeAsset",
]
SITEWISE_TREE_SCOPED_WRITE_ACTIONS = [
    "iotsitewise:UpdateAssetModel",
    "iotsitewise:CreateAsset",
    "iotsitewise:UpdateAsset",
    "iotsitewise:AssociateAssets",
]
SITEWISE_TREE_UNSCOPED_WRITE_ACTIONS = [
    "iotsitewise:CreateAssetModel",
]
SITEWISE_TREE_ACTIONS = (SITEWISE_TREE_READ_ACTIONS + SITEWISE_TREE_SCOPED_WRITE_ACTIONS
                         + SITEWISE_TREE_UNSCOPED_WRITE_ACTIONS)


def sitewise_arns(stack: Stack, *resource_types: str) -> list[str]:
    """This account's and Region's SiteWise ARNs of each resource type."""
    return [stack.format_arn(service="iotsitewise", resource=kind, resource_name="*")
            for kind in resource_types]


def load_sfc_config_generator() -> ModuleType:
    """The SFC config generator, loaded by path once per process."""
    name = "adt_sfc_config_generator"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SFC_CONFIG_GENERATOR)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ImportError(f"cannot load {SFC_CONFIG_GENERATOR}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses resolve their module by name
        spec.loader.exec_module(module)
    return sys.modules[name]


class SfcStack(Stack):
    """
    SFC runtime: an ECS Fargate service running SFC in the OPC UA servers' VPC,
    with SiteWise + S3 Tables IAM and the generated config staged in (ADR-0082
    decisions 2, 6 and 8).

    - Places the Fargate task in the shared VPC (decision 8; no new SG rule).
    - Attaches the T2 S3 Tables writer policy and the SiteWise data-plane set
      (decision 2; no model-mutating actions).
    - Generates the config at synth into the staged image context under
      ``cdk.out`` (decision 6), and creates the SiteWise models and asset tree
      with a custom resource before the service starts (decision 9).

    Parameters (supplied by the flag/packaging wiring, T5):
        network_stack: the stack exposing ``.vpc`` (NetworkStack / HighByte).
        vpc: the shared VPC directly, as an alternative to ``network_stack``.
        opc_ua_endpoints: the ``opc.tcp://…`` endpoint string per site from
            ``HighByteOpcuaStack.opc_ua_endpoints`` — carried into the config /
            container env so SFC knows which servers to read.
        s3tables_stack: ``SfcS3TablesStack``; its ``table_bucket_name``,
            ``namespace_name``, ``table_name``, ``table_arn`` and
            ``sfc_writer_policy`` are consumed.
        image: the SFC container image; defaults to a build asset from
            ``DEFAULT_SFC_DOCKER_CONTEXT`` (see the module note on the image).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network_stack=None,
        vpc: Optional[ec2.IVpc] = None,
        opc_ua_endpoints: Optional[Sequence[str]] = None,
        opc_ua_server_revisions: Optional[Sequence[str]] = None,
        s3tables_stack=None,
        image: Optional[ecs.ContainerImage] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description="ADT - SFC runtime (Fargate; SiteWise + S3 Tables writer)",
            **kwargs,
        )

        self._network_stack = network_stack
        self._s3tables_stack = s3tables_stack
        self.opc_ua_endpoints = list(opc_ua_endpoints or [])
        self.opc_ua_server_revisions = list(opc_ua_server_revisions or [])

        self._resolve_vpc(vpc)
        self._create_task_definition(image)
        self._create_service()
        self._create_asset_tree()
        self._create_exports()

    def _resolve_vpc(self, vpc: Optional[ec2.IVpc]) -> None:
        """Use the VPC handed in directly, or the network stack's ``.vpc``.

        SFC shares the OPC UA servers' VPC (ADR-0082 decision 8), so the VPC is
        never created here — it is one of the things the caller passes in.
        """
        if vpc is not None:
            self.vpc = vpc
        elif self._network_stack is not None:
            self.vpc = self._network_stack.vpc
        else:
            raise ValueError(
                "SfcStack needs the shared VPC — pass `vpc=` or `network_stack=` "
                "(SFC runs in the OPC UA servers' VPC; ADR-0082 decision 8)."
            )

    def _create_task_definition(self, image: Optional[ecs.ContainerImage]) -> None:
        """Build the Fargate task definition: image, IAM, config mount, env."""
        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "SfcTaskDefinition",
            cpu=SFC_CPU,
            memory_limit_mib=(SFC_MEMORY_WITH_REPLAY if _workshop_story.backfill_from_node(self.node)
                              else SFC_MEMORY),
            # The image is built LINUX_ARM64 (below); without this Fargate
            # defaults to X86_64 and the JVM dies with "exec format error".
            runtime_platform=ecs.RuntimePlatform(
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
                cpu_architecture=ecs.CpuArchitecture.ARM64,
            ),
        )

        self._attach_writer_iam()

        # ADR-0082 decision 6: for the default build-asset image the config is
        # generated here and written, with the Dockerfile, into ONE staging
        # directory under ``cdk.out`` (stage_tree); the image build COPYs it
        # in. A caller-supplied image has no build context to COPY into, so
        # staging is skipped for it; either way the container reads the file
        # at ``SFC_CONFIG_PATH``.
        if image is not None:
            container_image = image
            self._staged_context_path = None
        else:
            self._staged_context_path = self._stage_build_context()
            container_image = ecs.ContainerImage.from_asset(
                self._staged_context_path,
                platform=ecr_assets.Platform.LINUX_ARM64,
                **patched_image_options(),
            )

        self.container = self.task_definition.add_container(
            "SfcContainer",
            image=container_image,
            container_name="sfc",
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Sfc"),
            environment=self._container_environment(),
        )

    def _container_environment(self) -> dict[str, str]:
        """Environment for the SFC container: config path, endpoints, per-site hosts."""
        # Per-site OPC UA host, one env var each. The generated config carries
        # ${SFC_OPCUA_HOST_<SITE>} placeholders instead of hostnames, because the
        # internal NLB DNS names are deploy-time tokens, not synth-time strings —
        # SFC expands ${name} from this environment at startup (its documented
        # placeholder feature, the same one ${MODULES_DIR} uses). This is what
        # keeps the deploy single-pass with no post-deploy config rewrite.
        host_env = {
            f"SFC_OPCUA_HOST_{site.upper()}": host
            for site, host in self._opcua_hosts_by_site().items()
        }

        return {
            # SFC reads its config from this path (`sfc-main -config …`),
            # baked into the image from the staged build context (decision 6).
            "SFC_CONFIG_PATH": SFC_CONFIG_PATH,
            # The OPC UA endpoints SFC reads. Passed as a container env var
            # for observability / entrypoint use; the authoritative copy is
            # in the generated config (decision 6), which is derived from
            # these same endpoints.
            "OPC_UA_ENDPOINTS": ",".join(self.opc_ua_endpoints),
            "AWS_REGION": self.region,
            # Picked up by the JVM the image's CMD launches (see constant).
            "JAVA_TOOL_OPTIONS": SFC_JAVA_TOOL_OPTIONS,
            **host_env,
            **self._server_revision_env(),
        }

    def _server_revision_env(self) -> dict[str, str]:
        """The OPC UA servers' task-definition revisions, when the caller passes
        them. SFC does not rebuild its OPC UA subscriptions after a server
        restarts (seen on a rehearsal update, 2026-10-02: nothing reached
        SiteWise until SFC itself restarted), so a deploy that replaces a
        server task must replace the SFC task too. A new revision changes this
        value, which gives SFC a new task definition, and the stack order
        (SFC after the OPC UA stack) rolls it after the servers are up.
        Absent when not passed, so the template is otherwise unchanged."""
        if not self.opc_ua_server_revisions:
            return {}
        return {"OPC_UA_SERVER_REVISIONS": ",".join(self.opc_ua_server_revisions)}

    def _opcua_hosts_by_site(self) -> dict[str, str]:
        """Map each site key to the bare OPC UA host SFC should dial.

        The endpoints arrive in SITE ORDER from the HighByte stack's
        ``opc_ua_endpoints``; the site keys are the generator's own, so the two
        lists are zipped positionally. The value is the host ONLY: the
        generated config's ``Address`` is already
        ``opc.tcp://${SFC_OPCUA_HOST_<SITE>}``, so a scheme here would make SFC
        dial ``opc.tcp://opc.tcp://…`` and fail with ``unsupported protocol:
        null`` (seen against SFC with the stack's own env values).
        """
        hosts: dict[str, str] = {}
        for site, endpoint in zip(SFC_SITE_KEYS, self.opc_ua_endpoints, strict=True):
            hosts[site] = endpoint.split("//", 1)[-1].split(":", 1)[0]
        return hosts

    def _attach_writer_iam(self) -> None:
        """Attach the S3 Tables writer policy (T2) and the SiteWise data-plane
        set (ADR-0082 decision 2) to the task role.

        The S3 Tables policy is the ``sfc_writer_policy`` managed policy owned
        by ``SfcS3TablesStack`` (data-plane only, no Create* — ADR-0082
        decision 4); this stack only attaches it. The SiteWise statement grants
        the ``Assets``-mode data-plane actions and NOTHING that creates or
        updates an asset model, so the 28 pre-created models stay immutable to
        the writer.
        """
        if self._s3tables_stack is not None:
            self.task_definition.task_role.add_managed_policy(
                self._s3tables_stack.sfc_writer_policy
            )

        self._create_sitewise_policy()

    def _create_sitewise_policy(self) -> None:
        """Grant the SiteWise ``Assets``-mode data-plane actions (decision 2).

        ``CreateAssetModel`` / ``UpdateAssetModel`` are DELIBERATELY absent so
        SFC (which writes in ``Assets`` mapping mode, never ``AssetCreation``)
        cannot mutate the 28 pre-created asset models. The one write,
        ``BatchPutAssetPropertyValue``, is authorized per entry against the
        asset (when the entry names an asset id) or the time series (when it
        names a property alias), so it is granted on this account's asset and
        time-series ARNs. An entry that fails authorization is dropped from
        the batch without failing the call, so these two types must stay in
        step with how SFC addresses its entries. The reads stay on ``*``:
        ``ListAssetModels`` has no resource type, and the reads change nothing.
        """
        self.task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=SFC_SITEWISE_WRITE_ACTIONS,
                resources=sitewise_arns(self, "asset", "time-series"),
            )
        )
        self.task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=SFC_SITEWISE_READ_ACTIONS,
                resources=["*"],
            )
        )

    def sfc_config(self) -> dict:
        """The SFC configuration, generated from the tag map (decision 6).

        Region and OPC UA hosts are SFC ``${name}`` placeholders the container
        environment resolves at startup, so the config does not depend on a
        deploy-time value; the S3 Tables identifiers are the CDK-owned ones."""
        gen = load_sfc_config_generator()
        s3t = self._s3tables_stack
        return gen.generate_sfc_config(
            gen.placeholder_endpoints(),
            region="${AWS_REGION}",
            table_bucket=s3t.table_bucket_name if s3t else "adt-sfc-telemetry",
            namespace=s3t.namespace_name if s3t else "sfc",
            table_name=s3t.table_name if s3t else "readings",
            # The one-time history replay (the replay ADR): a second source +
            # Channel-timestamped schedule writing through its own SiteWise and
            # S3 Tables targets, only on the live story with the switch on.
            # Off = a byte-identical config (no history source or targets).
            backfill=_workshop_story.backfill_from_node(self.node),
            replay_table_name=s3t.replay_table_name if s3t else "readings_replay",
        )

    def _stage_build_context(self) -> str:
        """The SFC Dockerfile plus the generated config, staged under ``cdk.out``.

        Written into the staging dir only (``stacks/_staging.py``), never the
        checkout. The Dockerfile downloads and verifies the SFC bundles itself,
        so nothing else needs staging."""
        return stage_tree(
            self,
            "sfc-build",
            DEFAULT_SFC_DOCKER_CONTEXT,
            extra_texts={SFC_CONFIG_FILENAME: json.dumps(self.sfc_config(), indent=2)},
            ignore=("bundles", "__pycache__", ".DS_Store"),
        )

    def _create_asset_tree(self) -> None:
        """Custom resource: the models and asset tree SFC writes into (decision 9).

        The specs are computed here at synth from the tag map and passed as a
        property, so the Lambda carries only the applier and the model script.
        A change to the tree changes the property and re-runs it (idempotent);
        the service waits for it, so SFC never writes before its assets exist.
        """
        specs = [asdict(s) for s in load_sfc_config_generator().asset_tree()]
        specs_json = json.dumps(specs, sort_keys=True)
        models_hash = _models_script_hash()
        code_dir = stage_tree(
            self,
            "sfc-sitewise-tree",
            SITEWISE_TREE_LAMBDA_DIR,
            extra_files={
                "sitewise_tree.py": SITEWISE_TREE_MODULE,
                "DeploySitewiseModels.py": SITEWISE_MODELS_SCRIPT,
            },
        )
        fn = lambda_.Function(
            self,
            "SitewiseTreeFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(code_dir),
            # 28 models + ~135 assets, each polled to ACTIVE: minutes on a
            # fresh account, seconds on a re-run.
            timeout=Duration.minutes(15),
            memory_size=256,
        )
        self._grant_tree_role(fn)
        provider = cr.Provider(self, "SitewiseTreeProvider", on_event_handler=fn)
        self.asset_tree_resource = CustomResource(
            self,
            "SitewiseAssetTree",
            service_token=provider.service_token,
            properties={
                "Specs": specs_json,
                "SpecsHash": hashlib.sha256(specs_json.encode()).hexdigest(),
                # Re-run the resource when the model schemas change, not only
                # when the asset tree changes (deploy_models runs in the handler).
                "ModelsHash": models_hash,
            },
        )
        self.service.node.add_dependency(self.asset_tree_resource)

    def _grant_tree_role(self, fn: lambda_.Function) -> None:
        """Deploy-time SiteWise permissions for the asset-tree Lambda's role."""
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=SITEWISE_TREE_READ_ACTIONS + SITEWISE_TREE_UNSCOPED_WRITE_ACTIONS,
            resources=["*"],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=SITEWISE_TREE_SCOPED_WRITE_ACTIONS,
            # Asset and model ids are generated, so the ARNs end in a wildcard,
            # but they stay inside this account and Region.
            resources=sitewise_arns(self, "asset", "asset-model"),
        ))

    def _create_service(self) -> None:
        """Create the Fargate service in the shared VPC (ADR-0082 decision 8).

        The task runs in the OPC UA servers' VPC, so it is ALREADY permitted to
        reach them: ``highbyte_opcua_stack.py`` grants OPC UA ingress to
        ``vpc.vpc_cidr_block`` on port 26543 (deliberately a VPC-CIDR rule, not
        SG-to-SG, because the NLB IP targets do not preserve the client IP). We
        therefore add NO security-group rule to the OPC UA servers here — an
        in-VPC task already matches that ingress. The service's own
        construct-managed security group needs only egress (to reach the OPC UA
        NLBs, SiteWise and S3 Tables), which Fargate allows by default.
        """
        self.cluster = ecs.Cluster(self, "SfcCluster", vpc=self.vpc,
                                   container_insights=True)

        self.service = ecs.FargateService(
            self,
            "SfcService",
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=1,
            # In private subnets alongside the OPC UA servers; egress-only, no
            # inbound listener (SFC is a poller/writer, not a server).
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            circuit_breaker=None,
        )

        # Match the OPC UA / HighByte services' non-circuit-breaker deployment
        # config so a slow JVM start does not get killed mid-boot. In the
        # workshop (opcua_stop_first) SFC deploys stop-first (100/0) like the
        # OPC servers so a redeploy adds no vCPU surge and the package needs no
        # create-time quota ordering edge. A
        # workshop SFC update then has a short telemetry gap (rehearsal only).
        # Stop-first also sets AvailabilityZoneRebalancing DISABLED (the helper).
        stop_first = str(self.node.try_get_context("opcua_stop_first") or "").strip().lower() == "true"
        rolling = ({"MaximumPercent": 100, "MinimumHealthyPercent": 0} if stop_first
                   else {"MaximumPercent": 200, "MinimumHealthyPercent": 50})
        _highbyte_options.set_deployment_configuration(
            self.service.node.default_child,
            {
                **rolling,
                "DeploymentCircuitBreaker": {"Enable": False, "Rollback": False},
            },
        )

    def _create_exports(self) -> None:
        """Add stack outputs for the SFC runtime."""
        CfnOutput(
            self,
            "SfcClusterArn",
            value=self.cluster.cluster_arn,
            description="Amazon ECS cluster ARN for the SFC runtime",
        )
        CfnOutput(
            self,
            "SfcServiceName",
            value=self.service.service_name,
            description="Amazon ECS service name for the SFC runtime",
        )
        CfnOutput(
            self,
            "SfcConfigPath",
            value=SFC_CONFIG_PATH,
            description="In-container path SFC reads its generated config from",
        )
