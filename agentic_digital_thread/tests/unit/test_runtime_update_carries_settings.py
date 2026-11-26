# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""agents.runtime_provision_helpers.runtime_update_kwargs keeps every setting a
runtime carries.

UpdateAgentRuntime replaces the whole configuration: a setting the update
leaves out is reset. So the one update builder carries every mutable field
GetAgentRuntime returns that UpdateAgentRuntime accepts, and nothing else.
The set comes from the INSTALLED botocore service model, so an SDK update that
adds a shared field fails here until the builder decides what to do with it.
"""
import copy
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents import runtime_provision_helpers as helpers  # noqa: E402

botocore_session = pytest.importorskip("botocore.session")
botocore_validate = pytest.importorskip("botocore.validate")

ACCOUNT = "111122223333"
RUNTIME_ID = "adt_alpha-abcdefghij"
#: Fields the builder sets on purpose rather than copying (artifact and role
#: overrides, the sanitized network config, the reconciled env, the protocol
#: round-trip and the tagged-union authorizer), plus the update key.
BUILT_FIELDS = frozenset({
    "agentRuntimeId", "agentRuntimeArtifact", "roleArn", "networkConfiguration",
    "environmentVariables", "protocolConfiguration", "authorizerConfiguration",
})
OPTIONAL_SETTINGS = {
    "description": "Answers questions about one source system",
    "requestHeaderConfiguration": {"requestHeaderAllowlist": ["X-Request-Id"]},
    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 1800, "maxLifetime": 14400},
    "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/session"}}],
    "capacityProviderConfiguration": {"capacityProviderArn": (
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:capacity-provider/cp_one-abcdefghij")},
    "metadataConfiguration": {"requireMMDSV2": True},
    # Added to the shared fields by botocore 1.44.
    "hooksConfiguration": {"build": {"warmup": {"action": {
        "httpPost": {"path": "/warmup", "port": 8080}}}}},
    "resourceConfigurations": [{"minimumMemoryInMiB": 2048}],
    "platformVersion": "2",
    "allowedPorts": [{"port": 8000}, {"portRange": {"from": 9000, "to": 9010}}],
}


@pytest.fixture(scope="module")
def model():
    return botocore_session.get_session().get_service_model("bedrock-agentcore-control")


def _get_members(model):
    return set(model.operation_model("GetAgentRuntime").output_shape.members)


def _update_shape(model):
    return model.operation_model("UpdateAgentRuntime").input_shape


def _details(**extra):
    """A GetAgentRuntime response: read-only fields plus the built ones."""
    details = {
        "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:runtime/{RUNTIME_ID}",
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeName": "adt_alpha",
        "agentRuntimeVersion": "3",
        "status": "READY",
        "createdAt": "2026-10-01T00:00:00Z",
        "lastUpdatedAt": "2026-10-02T00:00:00Z",
        "failureReason": "",
        "workloadIdentityDetails": {"workloadIdentityArn": "arn:workload"},
        "agentRuntimeArtifact": {"containerConfiguration": {
            "containerUri": f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adt:old"}},
        "roleArn": f"arn:aws:iam::{ACCOUNT}:role/adt-runtime",
        "networkConfiguration": {"networkMode": "VPC", "networkModeConfig": {
            "subnets": ["subnet-0aa11bb22"], "securityGroups": ["sg-0aa11bb22"],
            "requireServiceS3Endpoint": True}},
        "environmentVariables": {"SYSTEM_ID": "alpha"},
        "protocolConfiguration": {"serverProtocol": "MCP"},
        "authorizerConfiguration": {"customJWTAuthorizer": {
            "discoveryUrl": "https://idp.example.com/.well-known/openid-configuration",
            "allowedClients": ["m2m-client"]}},
    }
    details.update(copy.deepcopy(extra))
    return details


def test_the_carried_fields_actually_sent_are_exactly_the_shared_mutable_ones(model):
    """Every field both shapes share is either built by the builder or carried
    verbatim; nothing outside the INSTALLED UpdateAgentRuntime input is carried.

    The builder gates the declared superset on the installed service model
    (:func:`helpers.supported_carried_fields`), so a field the SDK has not
    shipped yet is never sent even though the superset names it."""
    update = set(_update_shape(model).members)
    shared = _get_members(model) & update
    assert set(helpers.supported_carried_fields()) == shared - BUILT_FIELDS
    assert set(helpers.supported_carried_fields()) <= update


def test_the_declared_superset_is_a_superset_of_what_is_sent():
    """The hand-maintained superset documents every field we would carry when
    the SDK supports it; what the builder actually sends is that set narrowed
    to the installed model and is always a subset of it."""
    assert set(helpers.supported_carried_fields()) <= set(helpers.RUNTIME_UPDATE_CARRIED_FIELDS)


@pytest.mark.parametrize("setting", sorted(OPTIONAL_SETTINGS))
def test_each_supported_optional_setting_is_carried_into_the_update(setting):
    kwargs = helpers.runtime_update_kwargs(
        RUNTIME_ID, _details(**OPTIONAL_SETTINGS), {"SYSTEM_ID": "alpha"},
        artifact={"containerConfiguration": {"containerUri": "img:new"}})
    if setting in helpers.supported_carried_fields():
        assert kwargs.get(setting) == OPTIONAL_SETTINGS[setting]
    else:
        assert setting not in kwargs  # unsupported by the installed SDK: never sent


def test_the_update_is_valid_input_and_sends_no_read_only_field(model):
    kwargs = helpers.runtime_update_kwargs(
        RUNTIME_ID, _details(**OPTIONAL_SETTINGS), {"SYSTEM_ID": "alpha"})
    shape = _update_shape(model)
    assert set(kwargs) <= set(shape.members)
    for read_only in _get_members(model) - set(shape.members):
        assert read_only not in kwargs, read_only
    botocore_validate.validate_parameters(kwargs, shape)  # raises when invalid


def test_empty_settings_are_left_out(model):
    """An empty value is the same as an absent one, and some are invalid input
    (an empty description, an empty tagged union)."""
    details = _details(description="", requestHeaderConfiguration={},
                       lifecycleConfiguration={}, filesystemConfigurations=[],
                       capacityProviderConfiguration=None, metadataConfiguration={},
                       hooksConfiguration={}, resourceConfigurations=[],
                       platformVersion="", allowedPorts=[])
    kwargs = helpers.runtime_update_kwargs(RUNTIME_ID, details, {"SYSTEM_ID": "alpha"})
    assert not set(OPTIONAL_SETTINGS) & set(kwargs)
    botocore_validate.validate_parameters(kwargs, _update_shape(model))


def test_a_runtime_without_optional_settings_sends_the_same_fields_as_before():
    kwargs = helpers.runtime_update_kwargs(RUNTIME_ID, _details(), {"SYSTEM_ID": "alpha"})
    assert set(kwargs) == {
        "agentRuntimeId", "agentRuntimeArtifact", "roleArn", "networkConfiguration",
        "environmentVariables", "protocolConfiguration", "authorizerConfiguration"}


def test_a_carried_setting_is_a_copy_not_the_details_object():
    details = _details(**OPTIONAL_SETTINGS)
    kwargs = helpers.runtime_update_kwargs(RUNTIME_ID, details, {})
    kwargs["filesystemConfigurations"].append({"sessionStorage": {"mountPath": "/x"}})
    assert details["filesystemConfigurations"] == OPTIONAL_SETTINGS["filesystemConfigurations"]


def test_carried_runtime_settings_reads_only_the_supported_carried_fields():
    settings = helpers.carried_runtime_settings(_details(**OPTIONAL_SETTINGS))
    supported = helpers.supported_carried_fields()
    assert settings == {k: v for k, v in OPTIONAL_SETTINGS.items() if k in supported}
