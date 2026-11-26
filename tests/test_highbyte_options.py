# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""stacks/_highbyte_options.py: sizing and SiteWise-switch context parsing.

Pure (no synth): a stand-in node answers ``try_get_context``. The synthesized
shape is pinned by tests/test_highbyte_stack.py and the workshop packaging gate.
"""
from __future__ import annotations

import pytest

from stacks import _highbyte_options as opts


class _Node:
    def __init__(self, **context):
        self._context = context

    def try_get_context(self, key):
        return self._context.get(key)


def test_absent_keys_are_the_demo_sizing():
    sizing = opts.sizing_from_node(_Node())
    assert sizing == opts.OtSizing(hub_cpu=8192, hub_memory=16384, hub_stop_first=False,
                                   opcua_cpu=1024, opcua_memory=2048, opcua_grace_seconds=None)
    assert opts.hub_deployment_configuration(sizing) == {
        "MaximumPercent": 200, "MinimumHealthyPercent": 50}
    # The OPC UA servers + SFC default to start-first (demo behaviour). The
    # helper does NOT carry a circuit breaker (each call site keeps its own, so
    # the demo synth is byte-identical).
    assert sizing.opcua_stop_first is False
    assert opts.opcua_deployment_configuration(sizing) == {
        "MaximumPercent": 200, "MinimumHealthyPercent": 50}
    assert opts.sitewise_pipeline_from_node(_Node()) == "enabled"


def test_workshop_opcua_is_stop_first():
    """The workshop deploys the OPC UA servers (and SFC) stop-first
    (100/0) so a redeploy adds no vCPU surge and the create path needs no quota
    ordering edge. The helper carries no circuit breaker (call sites keep
    theirs)."""
    sizing = opts.sizing_from_node(_Node(opcua_stop_first="true"))
    assert sizing.opcua_stop_first is True
    assert opts.opcua_deployment_configuration(sizing) == {
        "MaximumPercent": 100, "MinimumHealthyPercent": 0}


def test_workshop_option_a_sizing():
    sizing = opts.sizing_from_node(_Node(
        highbyte_cpu="2048", highbyte_memory="8192", highbyte_stop_first="true",
        opcua_cpu="256", opcua_memory="1024", opcua_health_check_grace_seconds="300"))
    assert (sizing.hub_cpu, sizing.hub_memory, sizing.opcua_cpu, sizing.opcua_memory,
            sizing.opcua_grace_seconds) == (2048, 8192, 256, 1024, 300)
    assert opts.hub_deployment_configuration(sizing) == {
        "MaximumPercent": 100, "MinimumHealthyPercent": 0}


@pytest.mark.parametrize("context", [
    {"highbyte_cpu": "3000"},                                  # not a Fargate CPU size
    {"highbyte_cpu": "2048", "highbyte_memory": "2048"},       # below 2 vCPU's floor
    {"opcua_cpu": "256", "opcua_memory": "4096"},              # above 0.25 vCPU's ceiling
    {"opcua_cpu": "512", "opcua_memory": "1536"},              # not a 1 GB step
    {"highbyte_memory": "lots"},
    {"opcua_health_check_grace_seconds": "0"},
    {"highbyte_stop_first": "yes"},
    {"opcua_stop_first": "yes"},
])
def test_invalid_sizing_fails_the_synth(context):
    with pytest.raises(ValueError):
        opts.sizing_from_node(_Node(**context))


def test_sitewise_switch_values():
    assert opts.sitewise_pipeline_from_node(_Node(highbyte_sitewise_pipeline="disabled")) == "disabled"
    assert opts.sitewise_pipeline_from_node(_Node(highbyte_sitewise_pipeline=" Enabled ")) == "enabled"
    with pytest.raises(ValueError):
        opts.sitewise_pipeline_from_node(_Node(highbyte_sitewise_pipeline="off"))


def test_demo_images_and_no_circuit_breaker_by_default():
    """Absent image/circuit-breaker keys keep the demo: the given defaults and
    a DeploymentConfiguration with no circuit breaker."""
    sizing = opts.sizing_from_node(_Node())
    assert sizing.hub_circuit_breaker is False
    assert "DeploymentCircuitBreaker" not in opts.hub_deployment_configuration(sizing)
    hub, seed = opts.images_from_node(_Node(), "hub:demo", "seed:demo")
    assert (hub, seed) == ("hub:demo", "seed:demo")


def test_workshop_circuit_breaker_and_image_overrides():
    """The circuit breaker turns on with rollback, and a deployer override
    pinned by digest points both images elsewhere (here an account-ECR ref)."""
    sizing = opts.sizing_from_node(_Node(highbyte_stop_first="true", highbyte_circuit_breaker="true",
                                         highbyte_cpu="2048", highbyte_memory="8192"))
    cfg = opts.hub_deployment_configuration(sizing)
    assert cfg == {"MaximumPercent": 100, "MinimumHealthyPercent": 0,
                   "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True}}
    pin = "@sha256:" + "c" * 64
    hub, seed = opts.images_from_node(
        _Node(highbyte_image="123.dkr.ecr.us-east-1.amazonaws.com/repo:adt-ext-highbyte-hub" + pin,
              settings_seed_image="123.dkr.ecr.us-east-1.amazonaws.com/repo:adt-ext-settings-seed" + pin),
        "hub:demo", "seed:demo")
    assert hub.endswith("adt-ext-highbyte-hub" + pin) and seed.endswith("adt-ext-settings-seed" + pin)


_OVERRIDE_PIN = "@sha256:" + "b" * 64


@pytest.mark.parametrize("key", ["highbyte_image", "settings_seed_image"])
@pytest.mark.parametrize("ref", [
    "vendor/hub:latest",
    "busybox:latest",
    "highbyte/intelligence-hub:4.5.3",
    "${AWS::AccountId}.dkr.ecr.${AWS::Region}.${AWS::URLSuffix}/repo:adt-ext-highbyte-hub-2255d08fd39b",
    "vendor/hub@sha256:abc",
    "vendor/hub@sha256:" + "B" * 64,
    "vendor/hub" + _OVERRIDE_PIN + ":latest",
], ids=["latest", "bare-latest", "release-tag", "account-ecr-tag", "short-digest", "upper-hex",
        "tag-after-digest"])
def test_an_image_override_must_be_pinned_by_digest(key, ref):
    """A tag can move: ECS pulls the image again at every task start, so a
    moved tag would change what the hub task runs with its role. The demo
    defaults are digest refs; an explicit override must be one too."""
    with pytest.raises(ValueError, match="pinned by digest"):
        opts.images_from_node(_Node(**{key: ref}), "hub:demo", "seed:demo")


@pytest.mark.parametrize("ref", [
    "highbyte/intelligence-hub" + _OVERRIDE_PIN,
    "highbyte/intelligence-hub:4.4.2" + _OVERRIDE_PIN,
    "public.ecr.aws/docker/library/busybox:1.36" + _OVERRIDE_PIN,
    "${AWS::AccountId}.dkr.ecr.${AWS::Region}.${AWS::URLSuffix}/repo:adt-ext-highbyte-hub-2255d08fd39b"
    + _OVERRIDE_PIN,
])
def test_a_digest_pinned_override_is_used_as_given(ref):
    assert opts.images_from_node(_Node(highbyte_image=ref, settings_seed_image="  " + ref + " "),
                                 "hub:demo", "seed:demo") == (ref, ref)


def test_a_blank_override_keeps_the_default():
    assert opts.images_from_node(_Node(highbyte_image="", settings_seed_image="  "),
                                 "hub:demo", "seed:demo") == ("hub:demo", "seed:demo")


def test_circuit_breaker_flag_must_be_boolean():
    with pytest.raises(ValueError):
        opts.sizing_from_node(_Node(highbyte_circuit_breaker="maybe"))


class _CfnService:
    """Stands in for an L1 ``CfnService``: records the overrides applied."""

    def __init__(self):
        self.calls = []

    def add_property_deletion_override(self, path):
        self.calls.append(("delete", path))

    def add_property_override(self, path, value):
        self.calls.append(("set", path, value))


@pytest.mark.parametrize("config", [
    {"MaximumPercent": 100, "MinimumHealthyPercent": 0},
    {"MaximumPercent": 100, "MinimumHealthyPercent": 0,
     "DeploymentCircuitBreaker": {"Enable": False, "Rollback": False}},
])
def test_stop_first_disables_az_rebalancing(config):
    """ECS refuses AZ rebalancing with maximumPercent <= 100, and a service
    created with no AvailabilityZoneRebalancing in its template can still read
    ENABLED (one created 2026-10-01 did), so a stop-first service sets it
    DISABLED explicitly."""
    svc = _CfnService()
    opts.set_deployment_configuration(svc, config)
    assert svc.calls == [("delete", "DeploymentConfiguration"),
                         ("set", "DeploymentConfiguration", config),
                         ("set", "AvailabilityZoneRebalancing", "DISABLED")]


def test_start_first_leaves_az_rebalancing_unset():
    """Start-first (the demo) gets exactly the two overrides it always had,
    so the demo template is unchanged."""
    config = {"MaximumPercent": 200, "MinimumHealthyPercent": 50}
    svc = _CfnService()
    opts.set_deployment_configuration(svc, config)
    assert svc.calls == [("delete", "DeploymentConfiguration"),
                         ("set", "DeploymentConfiguration", config)]


# --- the workshop image-parameter switch ---------------------------------

def test_the_image_parameter_switch_is_off_by_default():
    assert opts.image_parameters_from_node(_Node()) is False
    assert opts.image_parameters_from_node(_Node(highbyte_image_parameters="false")) is False
    assert opts.image_parameters_from_node(_Node(highbyte_image_parameters="true")) is True


@pytest.mark.parametrize("key", ["highbyte_image", "settings_seed_image"])
def test_the_switch_refuses_a_synth_time_image(key):
    with pytest.raises(ValueError, match="takes the images as parameters"):
        opts.image_parameters_from_node(_Node(highbyte_image_parameters="true", **{key: "x/y:1"}))


def test_the_switch_must_be_boolean():
    with pytest.raises(ValueError, match="true or false"):
        opts.image_parameters_from_node(_Node(highbyte_image_parameters="yes"))


_DIGEST = "@sha256:" + "a" * 64
_ACCOUNT_REPO = ("111122223333.dkr.ecr.us-east-1.amazonaws.com/"
                 "cdk-hnb659fds-container-assets-111122223333-us-east-1")


@pytest.mark.parametrize("ref", [
    _ACCOUNT_REPO + _DIGEST,                                     # copy / load route
    "777788889999.dkr.ecr.cn-north-1.amazonaws.com.cn/cdk-hnb659fds-container-assets-777788889999-cn-north-1" + _DIGEST,
    "highbyte/intelligence-hub" + _DIGEST,                       # direct: Docker Hub
    "docker.io/highbyte/intelligence-hub" + _DIGEST,
    "public.ecr.aws/example/tool" + _DIGEST,                     # an Amazon ECR Public ref
])
def test_the_parameter_pattern_accepts_digest_refs(ref):
    import re
    assert re.fullmatch(opts.IMAGE_PARAMETER_PATTERN, ref)


@pytest.mark.parametrize("ref", [
    _ACCOUNT_REPO + ":adt-ext-highbyte-hub-2255d08fd39b",        # a tag can move
    "highbyte/intelligence-hub:4.5.3",
    "public.ecr.aws/docker/library/busybox:1.36",
    "444455556666.dkr.ecr.us-east-1.amazonaws.com/other-repo" + _DIGEST,   # another private repo
    "quay.io/highbyte/intelligence-hub" + _DIGEST,               # another registry
    _ACCOUNT_REPO + "@sha256:" + "a" * 63,
    "",
])
def test_the_parameter_pattern_refuses_anything_else(ref):
    import re
    assert not re.fullmatch(opts.IMAGE_PARAMETER_PATTERN, ref)


# ---------------------------------------------------------------------------
# HighByte EULA acceptance: ACCEPT_EULA=Y only from an explicit context value
# ---------------------------------------------------------------------------

#: HighByte's own EULA document and the Docker guide that says setting
#: ACCEPT_EULA=Y agrees to it (both checked with a GET, 2026-10-12).
_EULA_URL = "https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf"
_DOCKER_GUIDE_URL = "https://guide.highbyte.com/release/setup/docker/"


def test_the_eula_links_are_the_published_ones():
    assert opts.EULA_URL == _EULA_URL
    assert opts.DOCKER_GUIDE_URL == _DOCKER_GUIDE_URL
    assert opts.EULA_ACCEPTED_KEY == "highbyte_accept_eula"


@pytest.mark.parametrize("value", ["true", "TRUE", " true ", True])
def test_an_explicit_acceptance_sets_accept_eula(value):
    assert opts.accept_eula_env(_Node(highbyte_accept_eula=value)) == "Y"


@pytest.mark.parametrize("context", [
    {},                                    # absent: never inferred
    {"highbyte_accept_eula": ""},
    {"highbyte_accept_eula": "false"},
    {"highbyte_accept_eula": False},
    {"highbyte_accept_eula": "yes"},       # not a boolean
    {"highbyte_accept_eula": "Y"},         # the env value is not the context value
    {"highbyte_enabled": "true"},          # enabling HighByte is not accepting its EULA
], ids=["absent", "empty", "false", "bool-false", "yes", "Y", "enabled-only"])
def test_no_explicit_acceptance_fails_loud_with_the_link(context):
    with pytest.raises(ValueError) as excinfo:
        opts.accept_eula_env(_Node(**context))
    message = str(excinfo.value)
    assert _EULA_URL in message
    assert "highbyte_accept_eula=true" in message
    assert "--accept-highbyte-eula" in message
