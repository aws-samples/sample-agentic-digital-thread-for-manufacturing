# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time options for ``ADTHighByteOpcuaStack``: Fargate sizing and the
HighByte SiteWise pipeline switch.

Every option is a CDK context key whose default is the demo's behaviour, so a
deploy.sh synth (no keys) produces the same template as before. A small or
event account sets them to fit the hub, the four OPC UA servers, SFC, the
app and the ERP dashboard into the default Fargate On-Demand vCPU quota of 6
(Option A in the 2026-10-08 sizing review): hub 2 vCPU / 8 GB stop-first,
OPC UA servers 0.25 vCPU / 1 GB with a 300 s health-check grace period.

The SiteWise switch exists because SFC is the only SiteWise writer in the
workshop (ADR-0082): with ``highbyte_sitewise_pipeline=disabled`` the setup
Lambda imports ``NameSpace_to_SiteWise`` with every trigger disabled and the
hub's task role carries no SiteWise permission at all.

One key has no default: ``highbyte_accept_eula``. The hub container runs only
with ``ACCEPT_EULA=Y``, and setting it agrees to the HighByte Intelligence Hub
End User License Agreement, so a synth that includes the hub needs the
deployer's explicit acceptance and fails loud without it (``accept_eula_env``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Context keys (strings, as CDK passes ``-c key=value``).
HUB_CPU_KEY = "highbyte_cpu"
HUB_MEMORY_KEY = "highbyte_memory"
HUB_STOP_FIRST_KEY = "highbyte_stop_first"
OPCUA_CPU_KEY = "opcua_cpu"
OPCUA_MEMORY_KEY = "opcua_memory"
OPCUA_GRACE_KEY = "opcua_health_check_grace_seconds"
#: Deploy the four OPC UA servers (and, via SfcStack, SFC) stop-first
#: (MaximumPercent 100 / MinimumHealthyPercent 0) rather than start-first
#: (200/50). Default off (the demo's behaviour). The workshop turns it on so a
#: redeploy replaces each task in place with no extra vCPU surge, keeping the
#: whole package inside the default Fargate On-Demand quota WITHOUT any
#: workshop-only create-time DependsOn ordering.
#: A workshop OPC/SFC update then has a short telemetry gap, acceptable for
#: rehearsal updates (attendee accounts only ever create).
OPCUA_STOP_FIRST_KEY = "opcua_stop_first"
SITEWISE_PIPELINE_KEY = "highbyte_sitewise_pipeline"
#: Container image overrides (default = the demo's registry refs, so a
#: deploy.sh synth is unchanged). An override must be pinned by digest
#: (images_from_node); the workshop package uses the deploy-time image
#: parameters below instead.
HUB_IMAGE_KEY = "highbyte_image"
SETTINGS_SEED_IMAGE_KEY = "settings_seed_image"
#: What ends a digest-pinned image ref (``<image>[:tag]@sha256:<64 hex>``).
_DIGEST_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}$")
#: Workshop-only switch: take the hub and settings-seed images as DEPLOY-TIME
#: CloudFormation parameters instead of synth-time refs. The Workshop Studio
#: root passes them from its Custom::ADTExternalImages resource, which picks
#: the route at deploy time (account ECR when the copy or the load worked,
#: else a Docker Hub digest ref) and passes the hub image for both. Default
#: off: a deploy.sh synth declares no such parameter and is unchanged.
#: Exclusive with the two image keys above.
IMAGE_PARAMETERS_KEY = "highbyte_image_parameters"
#: The two parameters the switch declares.
HUB_IMAGE_PARAMETER = "HighByteHubImage"
SETTINGS_SEED_IMAGE_PARAMETER = "HighByteSettingsSeedImage"
#: AllowedPattern of both parameters: a DIGEST ref, either in this account's
#: bootstrap container-assets repo (the copy and load routes) or on Docker
#: Hub / ECR Public (the direct route). A first path segment without a dot
#: and no registry prefix is a Docker Hub repo, so no other private registry
#: fits. Never a tag: a tag can be moved under a running task.
IMAGE_PARAMETER_PATTERN = (
    r"^(?:[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?"
    r"/cdk-hnb659fds-container-assets-[0-9]{12}-[a-z0-9-]+"
    r"|(?:public\.ecr\.aws/|docker\.io/)?[a-z0-9-]+(?:/[a-z0-9._-]+)*)"
    r"@sha256:[0-9a-f]{64}$"
)
#: The repository the copy and load routes write (Fn::Sub template). Under the
#: switch the hub's execution role is granted pulls on it statically; the
#: direct route needs no grant.
ACCOUNT_ECR_REPOSITORY_ARN = (
    "arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/"
    "cdk-hnb659fds-container-assets-${AWS::AccountId}-${AWS::Region}"
)
#: Enable the ECS deployment circuit breaker (with rollback) on the hub
#: service. Default off (the demo's behaviour). The workshop turns it on: at
#: 2 vCPU the hub boots in ~4 s, so the old "high CPU on startup" reason for
#: disabling it no longer holds, and a bad deployment should fail fast and roll
#: back rather than hang on the 60-minute stack timeout.
HUB_CIRCUIT_BREAKER_KEY = "highbyte_circuit_breaker"

#: The deployer's explicit acceptance of the HighByte EULA. No default and
#: never inferred (not from highbyte_enabled, not from an SSM value read at
#: synth): deploy.sh passes it once the deployer accepted with
#: --accept-highbyte-eula (recorded in SSM /adt/highbyte-eula-accepted), and
#: the Workshop Studio live package sets it in its synth context.
EULA_ACCEPTED_KEY = "highbyte_accept_eula"
#: The HighByte Intelligence Hub End User License Agreement, and HighByte's
#: Docker guide, which states that setting ACCEPT_EULA=Y agrees to it.
EULA_URL = "https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf"
DOCKER_GUIDE_URL = "https://guide.highbyte.com/release/setup/docker/"

#: The setup Lambda's environment variable for the SiteWise switch.
SITEWISE_PIPELINE_ENV = "HIGHBYTE_SITEWISE_PIPELINE"
SITEWISE_PIPELINE_VALUES = ("enabled", "disabled")

#: Today's (demo) sizing: the defaults when a key is absent.
DEFAULT_HUB_CPU = 8192
DEFAULT_HUB_MEMORY = 16384
DEFAULT_OPCUA_CPU = 1024
DEFAULT_OPCUA_MEMORY = 2048

#: Fargate task sizes: CPU units -> (min, max, step) memory in MiB.
_FARGATE_MEMORY = {
    256: (512, 2048, None),
    512: (1024, 4096, 1024),
    1024: (2048, 8192, 1024),
    2048: (4096, 16384, 1024),
    4096: (8192, 30720, 1024),
    8192: (16384, 61440, 4096),
    16384: (32768, 122880, 8192),
}
_FARGATE_256_MEMORY = (512, 1024, 2048)


@dataclass(frozen=True)
class OtSizing:
    """Fargate sizing for the hub and the OPC UA servers."""

    hub_cpu: int = DEFAULT_HUB_CPU
    hub_memory: int = DEFAULT_HUB_MEMORY
    hub_stop_first: bool = False
    opcua_cpu: int = DEFAULT_OPCUA_CPU
    opcua_memory: int = DEFAULT_OPCUA_MEMORY
    #: ``None`` keeps CDK's default (60 s with a load balancer).
    opcua_grace_seconds: int | None = None
    #: Deploy the OPC UA servers (and SFC) stop-first (100/0) instead of
    #: start-first (200/50). Default off (demo); the workshop turns it on so a
    #: redeploy adds no vCPU surge and the package needs no create-time quota
    #: ordering edges.
    opcua_stop_first: bool = False
    #: ECS deployment circuit breaker (with rollback) on the hub service.
    #: Default off (demo); the workshop turns it on (2 vCPU boots in ~4 s).
    hub_circuit_breaker: bool = False


def validate_fargate_size(cpu: int, memory: int, what: str) -> None:
    """ValueError unless *cpu*/*memory* is a valid Fargate task size."""
    if cpu not in _FARGATE_MEMORY:
        raise ValueError(f"{what}: cpu {cpu} is not a Fargate CPU size {sorted(_FARGATE_MEMORY)}")
    if cpu == 256:
        if memory not in _FARGATE_256_MEMORY:
            raise ValueError(f"{what}: memory {memory} is not valid for cpu 256 {_FARGATE_256_MEMORY}")
        return
    low, high, step = _FARGATE_MEMORY[cpu]
    if not (low <= memory <= high and (memory - low) % step == 0):
        raise ValueError(f"{what}: memory {memory} is not valid for cpu {cpu} "
                         f"({low}..{high} in steps of {step})")


def _int_context(node, key: str, default: int | None) -> int | None:
    raw = node.try_get_context(key)
    if raw is None or str(raw).strip() == "":
        return default
    text = str(raw).strip()
    if not (text.isascii() and text.isdigit()) or int(text) <= 0:
        raise ValueError(f"context {key}={raw!r} is not a positive whole number")
    return int(text)


def _bool_context(node, key: str) -> bool:
    raw = node.try_get_context(key)
    text = str(raw).strip().lower() if raw is not None else ""
    if text not in ("", "true", "false"):
        raise ValueError(f"context {key}={raw!r} is not true or false")
    return text == "true"


def sizing_from_node(node) -> OtSizing:
    """The sizing from synth context (demo defaults for absent keys)."""
    sizing = OtSizing(
        hub_cpu=_int_context(node, HUB_CPU_KEY, DEFAULT_HUB_CPU),
        hub_memory=_int_context(node, HUB_MEMORY_KEY, DEFAULT_HUB_MEMORY),
        hub_stop_first=_bool_context(node, HUB_STOP_FIRST_KEY),
        opcua_cpu=_int_context(node, OPCUA_CPU_KEY, DEFAULT_OPCUA_CPU),
        opcua_memory=_int_context(node, OPCUA_MEMORY_KEY, DEFAULT_OPCUA_MEMORY),
        opcua_grace_seconds=_int_context(node, OPCUA_GRACE_KEY, None),
        opcua_stop_first=_bool_context(node, OPCUA_STOP_FIRST_KEY),
        hub_circuit_breaker=_bool_context(node, HUB_CIRCUIT_BREAKER_KEY),
    )
    validate_fargate_size(sizing.hub_cpu, sizing.hub_memory, "HighByte hub")
    validate_fargate_size(sizing.opcua_cpu, sizing.opcua_memory, "OPC UA server")
    return sizing


def hub_deployment_configuration(sizing: OtSizing) -> dict:
    """The hub service's DeploymentConfiguration override.

    Default: start-first (200/50), as the demo has always run. Stop-first
    (100/0) costs no extra vCPU when ECS replaces the hub after a trial expiry
    (the health check fails and the old task is stopped BEFORE the new one
    starts), and it keeps two hubs from sharing one EFS appData directory.

    The deployment circuit breaker (with rollback) is added when
    ``hub_circuit_breaker`` is set. It covers DEPLOYMENTS (a service/task-def
    update) only — not the steady-state health-check replacement a trial
    expiry triggers — so with stop-first it lets a bad in-event update fail
    fast and roll back to the last good task definition instead of hanging on
    the nested stack's 60-minute timeout."""
    config = ({"MaximumPercent": 100, "MinimumHealthyPercent": 0}
              if sizing.hub_stop_first
              else {"MaximumPercent": 200, "MinimumHealthyPercent": 50})
    if sizing.hub_circuit_breaker:
        config["DeploymentCircuitBreaker"] = {"Enable": True, "Rollback": True}
    return config


def opcua_deployment_configuration(sizing: OtSizing) -> dict:
    """The rolling deployment config for the OPC UA servers and SFC.

    Default: start-first (200/50), as the demo has always run. When
    ``opcua_stop_first`` is set (the workshop) they deploy stop-first (100/0): a
    redeploy stops the old task before starting the new one, so it adds NO vCPU
    surge and the package fits the default Fargate On-Demand quota with no
    create-time ordering edges. The circuit breaker
    is NOT set here (a slow OPC/SFC JVM start must not be killed mid-boot); each
    call site keeps its own circuit-breaker property exactly as before so the
    demo synth is byte-identical."""
    return ({"MaximumPercent": 100, "MinimumHealthyPercent": 0}
            if sizing.opcua_stop_first
            else {"MaximumPercent": 200, "MinimumHealthyPercent": 50})


def set_deployment_configuration(cfn_service, config: dict) -> None:
    """Replace *cfn_service*'s DeploymentConfiguration with *config*.

    A stop-first config (MaximumPercent <= 100) also sets
    AvailabilityZoneRebalancing DISABLED: ECS refuses AZ rebalancing with
    maximumPercent <= 100, and a service created with the property absent can
    still read ENABLED (one created 2026-10-01 did, so its 2026-10-10
    stop-first update rolled back). These are desiredCount 1 services, so
    rebalancing has nothing to move anyway. A start-first config gets only the
    two overrides it always had, so the demo template is unchanged."""
    cfn_service.add_property_deletion_override("DeploymentConfiguration")
    cfn_service.add_property_override("DeploymentConfiguration", config)
    if int(config.get("MaximumPercent", 200)) <= 100:
        cfn_service.add_property_override("AvailabilityZoneRebalancing", "DISABLED")


def images_from_node(node, hub_default: str, settings_seed_default: str) -> tuple[str, str]:
    """``(hub_image, settings_seed_image)`` from synth context. Each defaults
    to the demo's registry ref (a digest ref, so a deploy.sh synth is
    unchanged). A deployer may override either one, pinned by digest
    (``<image>[:tag]@sha256:<64 hex>``): ValueError for anything else, since
    ECS pulls the image again at every task start and a tag can move. The
    workshop package passes its images as deploy-time parameters instead
    (``image_parameters_from_node``)."""
    images = []
    for key, default in ((HUB_IMAGE_KEY, hub_default), (SETTINGS_SEED_IMAGE_KEY, settings_seed_default)):
        raw = node.try_get_context(key)
        value = str(raw).strip() if raw is not None else ""
        if not value:
            images.append(default)
            continue
        if not _DIGEST_SUFFIX.search(value):
            raise ValueError(f"context {key}={value!r} is not pinned by digest: give "
                             "<image>[:tag]@sha256:<64 lowercase hex> (a tag alone can move)")
        images.append(value)
    return images[0], images[1]


def image_parameters_from_node(node) -> bool:
    """Whether the hub images are deploy-time parameters (IMAGE_PARAMETERS_KEY).
    ValueError when it is on together with an image key: the parameters ARE
    the images, so a synth-time ref would be ignored."""
    enabled = _bool_context(node, IMAGE_PARAMETERS_KEY)
    if enabled:
        given = [key for key in (HUB_IMAGE_KEY, SETTINGS_SEED_IMAGE_KEY)
                 if str(node.try_get_context(key) or "").strip()]
        if given:
            raise ValueError(f"context {IMAGE_PARAMETERS_KEY}=true takes the images as "
                             f"parameters; drop {', '.join(given)}")
    return enabled


def sitewise_pipeline_from_node(node) -> str:
    """``enabled`` (default, the demo) or ``disabled``."""
    raw = node.try_get_context(SITEWISE_PIPELINE_KEY)
    value = str(raw).strip().lower() if raw is not None else ""
    if not value:
        return "enabled"
    if value not in SITEWISE_PIPELINE_VALUES:
        raise ValueError(f"context {SITEWISE_PIPELINE_KEY}={raw!r} is not one of "
                         f"{', '.join(SITEWISE_PIPELINE_VALUES)}")
    return value


def accept_eula_env(node) -> str:
    """The hub container's ``ACCEPT_EULA`` value: ``"Y"``, only when context
    ``highbyte_accept_eula`` is ``true``.

    The container exits immediately without ``ACCEPT_EULA=Y``, and setting it
    agrees to HighByte's EULA, so the value comes only from the deployer's
    explicit acceptance. ValueError, naming the agreement and how to accept
    it, when the key is absent, false or not a boolean: the synth fails
    instead of accepting third-party terms on the deployer's behalf."""
    raw = node.try_get_context(EULA_ACCEPTED_KEY)
    text = str(raw).strip().lower() if raw is not None else ""
    if text == "true":
        return "Y"
    problem = ("" if text in ("", "false")
               else f"context {EULA_ACCEPTED_KEY}={raw!r} is not true or false. ")
    raise ValueError(
        f"{problem}HighByte is enabled, but the HighByte Intelligence Hub End User "
        "License Agreement has not been accepted. The hub container runs only with "
        "ACCEPT_EULA=Y, and setting it means you agree to that agreement. Read it at "
        f"{EULA_URL} (HighByte's Docker guide: {DOCKER_GUIDE_URL}). To accept it, "
        "deploy once with ./deploy.sh --accept-highbyte-eula, or pass "
        f"-c {EULA_ACCEPTED_KEY}=true to cdk.")
