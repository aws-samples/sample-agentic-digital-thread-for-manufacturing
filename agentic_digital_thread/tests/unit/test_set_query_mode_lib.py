# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for scripts/_query_mode_lib.py (and the injected-client driver).

Fully offline: no boto3, no live AWS. The pure functions take plain dicts/lists;
the injected-client driver is exercised with a tiny scripted stub, mirroring the
stub style in ``test_reonboard_lib.py`` / ``test_provisioner_states.py``.

Coverage:
  * ``validate_mode`` accepts exactly 'agent'/'orchestrator', rejects anything else;
  * ``merged_environment`` preserves existing keys and overwrites SYSTEM_QUERY_MODE
    (and does not mutate the input);
  * ``select_system_runtime_names`` excludes adt_orchestrator/adt_onboarding/
    adt_digital_thread on the --all path and maps ids -> adt_<id> in order on the
    --systems path;
  * ``build_runtime_update_kwargs`` preserves artifact/roleArn/network (dropping the
    immutable requireServiceS3Endpoint) + only re-sends a real authorizer;
  * ``update_runtime_query_mode`` drives get -> update through a fake control client.

Run:
  * pytest:     pytest agentic_digital_thread/tests/unit/test_set_query_mode_lib.py -q
  * standalone: python agentic_digital_thread/tests/unit/test_set_query_mode_lib.py
"""
import sys
from pathlib import Path

# agentic_digital_thread/ (unused here but keeps parity) and repo-root scripts/.
_ADT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ADT_ROOT.parent / "scripts"
for _p in (str(_ADT_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from _query_mode_lib import (  # noqa: E402
    INFRA_RUNTIME_NAMES,
    QUERY_MODE_ENV_KEY,
    InvalidModeError,
    QueryModeError,
    build_runtime_update_kwargs,
    capture_runtime_create_kwargs,
    capture_target_create_kwargs,
    merged_environment,
    recreate_runtime_with_mode,
    runtime_id_by_name,
    select_system_runtime_names,
    update_runtime_query_mode,
    validate_mode,
)
from _reonboard_lib import RuntimeCreateFailedError  # noqa: E402


# --------------------------------------------------------------------------- #
# validate_mode
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_validate_mode_accepts_valid(mode):
    assert validate_mode(mode) == mode


@pytest.mark.parametrize("mode", ["Agent", "AGENT", "orch", "", "auto", " agent"])
def test_validate_mode_rejects_invalid(mode):
    with pytest.raises(InvalidModeError):
        validate_mode(mode)


# --------------------------------------------------------------------------- #
# merged_environment
# --------------------------------------------------------------------------- #
def test_merged_environment_preserves_and_overwrites():
    existing = {
        "BEDROCK_MODEL": "us.anthropic.claude-sonnet-4-6",
        "AWS_REGION": "us-east-1",
        QUERY_MODE_ENV_KEY: "orchestrator",
    }
    merged = merged_environment(existing, "agent")
    # existing keys preserved
    assert merged["BEDROCK_MODEL"] == "us.anthropic.claude-sonnet-4-6"
    assert merged["AWS_REGION"] == "us-east-1"
    # SYSTEM_QUERY_MODE overwritten
    assert merged[QUERY_MODE_ENV_KEY] == "agent"
    # input not mutated
    assert existing[QUERY_MODE_ENV_KEY] == "orchestrator"


def test_merged_environment_adds_key_when_absent():
    merged = merged_environment({"FOO": "bar"}, "orchestrator")
    assert merged == {"FOO": "bar", QUERY_MODE_ENV_KEY: "orchestrator"}


def test_merged_environment_handles_none():
    assert merged_environment(None, "agent") == {QUERY_MODE_ENV_KEY: "agent"}


def test_merged_environment_validates_mode():
    with pytest.raises(InvalidModeError):
        merged_environment({}, "nope")


# --------------------------------------------------------------------------- #
# select_system_runtime_names
# --------------------------------------------------------------------------- #
def test_select_all_excludes_infra_runtimes():
    all_names = [
        "adt_orchestrator",
        "adt_onboarding",
        "adt_digital_thread",
        "adt_erp",
        "adt_mes",
        "adt_cmms",
        "not_a_adt_runtime",
    ]
    selected = select_system_runtime_names(all_names)
    assert selected == ["adt_cmms", "adt_erp", "adt_mes"]  # sorted, infra excluded
    for infra in INFRA_RUNTIME_NAMES:
        assert infra not in selected
    assert "not_a_adt_runtime" not in selected


def test_select_all_empty_when_only_infra():
    assert select_system_runtime_names(list(INFRA_RUNTIME_NAMES)) == []


def test_select_explicit_maps_ids_in_order():
    # --systems path: preserve the GIVEN order; map id -> adt_<id> (hyphens folded).
    selected = select_system_runtime_names(
        [], explicit_system_ids=["mes", "erp", "shop-floor"]
    )
    assert selected == ["adt_mes", "adt_erp", "adt_shop_floor"]


# --------------------------------------------------------------------------- #
# build_runtime_update_kwargs
# --------------------------------------------------------------------------- #
def _details_with_s3_endpoint():
    return {
        "agentRuntimeId": "adt_erp-abc",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "img:latest"}},
        "roleArn": "arn:aws:iam::111122223333:role/runtime",
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": ["subnet-a"],
                "securityGroups": ["sg-1"],
                "requireServiceS3Endpoint": True,
            },
        },
        "environmentVariables": {"AWS_REGION": "us-east-1", QUERY_MODE_ENV_KEY: "orchestrator"},
    }


def test_build_update_kwargs_preserves_config_and_sets_mode():
    kwargs = build_runtime_update_kwargs(_details_with_s3_endpoint(), "agent")
    assert kwargs["agentRuntimeId"] == "adt_erp-abc"
    assert kwargs["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == "img:latest"
    assert kwargs["roleArn"] == "arn:aws:iam::111122223333:role/runtime"
    assert kwargs["environmentVariables"][QUERY_MODE_ENV_KEY] == "agent"
    assert kwargs["environmentVariables"]["AWS_REGION"] == "us-east-1"


def test_build_update_kwargs_drops_require_service_s3_endpoint():
    kwargs = build_runtime_update_kwargs(_details_with_s3_endpoint(), "agent")
    nmc = kwargs["networkConfiguration"]["networkModeConfig"]
    assert "requireServiceS3Endpoint" not in nmc
    # other network fields preserved
    assert nmc["subnets"] == ["subnet-a"]
    assert nmc["securityGroups"] == ["sg-1"]


def test_build_update_kwargs_omits_empty_authorizer():
    kwargs = build_runtime_update_kwargs(_details_with_s3_endpoint(), "agent")
    assert "authorizerConfiguration" not in kwargs


def test_build_update_kwargs_keeps_real_authorizer():
    details = _details_with_s3_endpoint()
    details["authorizerConfiguration"] = {
        "customJWTAuthorizer": {
            "discoveryUrl": "https://issuer/.well-known/openid-configuration",
            "allowedClients": ["client-1"],
        }
    }
    kwargs = build_runtime_update_kwargs(details, "orchestrator")
    assert kwargs["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == ["client-1"]


def test_build_update_kwargs_defaults_public_network():
    details = {"agentRuntimeId": "adt_x", "environmentVariables": {}}
    kwargs = build_runtime_update_kwargs(details, "agent")
    assert kwargs["networkConfiguration"] == {"networkMode": "PUBLIC"}


def test_build_update_kwargs_requires_runtime_id():
    with pytest.raises(QueryModeError):
        build_runtime_update_kwargs({"environmentVariables": {}}, "agent")


def test_build_update_kwargs_fills_required_env_on_a_starved_runtime(monkeypatch):
    """This module previously mirrored the sanctioned
    builder WITHOUT its required-key fill, so a fleet query-mode flip on a
    starved runtime rewrote env without ONBOARDING_SESSIONS_TABLE. Delegated
    to runtime_provision_helpers.runtime_update_kwargs, the flip now fills it
    (ambient env else the CDK-fixed default) while an existing value stays
    authoritative."""
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    kwargs = build_runtime_update_kwargs(_details_with_s3_endpoint(), "agent")
    assert (kwargs["environmentVariables"]["ONBOARDING_SESSIONS_TABLE"]
            == "adt-onboarding-sessions")
    details = _details_with_s3_endpoint()
    details["environmentVariables"]["ONBOARDING_SESSIONS_TABLE"] = "keep-me"
    kwargs = build_runtime_update_kwargs(details, "agent")
    assert kwargs["environmentVariables"]["ONBOARDING_SESSIONS_TABLE"] == "keep-me"


def test_capture_create_kwargs_fills_required_env_on_recreate(monkeypatch):
    """The recreate path applies the SAME required-key fill — recreating a
    starved runtime must not re-create it starved."""
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    kwargs = capture_runtime_create_kwargs(_full_get_runtime_details(), "agent")
    assert (kwargs["environmentVariables"]["ONBOARDING_SESSIONS_TABLE"]
            == "adt-onboarding-sessions")


# --------------------------------------------------------------------------- #
# update_runtime_query_mode — injected fake control client (dry driver)
# --------------------------------------------------------------------------- #
class _FakeControl:
    """Minimal AgentCore control-plane stub for the update driver."""

    def __init__(self, *, details=None, runtimes=None):
        self._details = details or _details_with_s3_endpoint()
        self._runtimes = runtimes or []
        self.update_kwargs = None
        self.got_runtime_id = None

    def get_agent_runtime(self, agentRuntimeId):
        self.got_runtime_id = agentRuntimeId
        return dict(self._details)

    def update_agent_runtime(self, **kwargs):
        self.update_kwargs = kwargs
        return {}

    def list_agent_runtimes(self):
        return {"agentRuntimeSummaries": self._runtimes}


def test_update_runtime_query_mode_reads_then_updates():
    control = _FakeControl()
    sent = update_runtime_query_mode(control, "adt_erp-abc", "agent")
    assert control.got_runtime_id == "adt_erp-abc"
    assert control.update_kwargs["environmentVariables"][QUERY_MODE_ENV_KEY] == "agent"
    # returns the kwargs actually sent
    assert sent == control.update_kwargs
    # env-preserving update dropped the immutable field
    assert "requireServiceS3Endpoint" not in (
        control.update_kwargs["networkConfiguration"].get("networkModeConfig", {})
    )


def test_update_runtime_query_mode_validates_mode():
    control = _FakeControl()
    with pytest.raises(InvalidModeError):
        update_runtime_query_mode(control, "adt_erp-abc", "bogus")
    assert control.update_kwargs is None  # nothing sent on a bad mode


def test_update_runtime_query_mode_skips_when_already_at_mode():
    """Idempotent: a runtime already at the target mode is NOT updated (no restart)."""
    details = _details_with_s3_endpoint()
    details["environmentVariables"] = {"SYSTEM_QUERY_MODE": "agent", "KEEP": "x"}
    control = _FakeControl(details=details)
    result = update_runtime_query_mode(control, "adt_erp-abc", "agent")
    assert result == {"skipped": True, "current": "agent"}
    assert control.update_kwargs is None  # no update call -> no restart


def test_update_runtime_query_mode_updates_when_mode_differs():
    """A runtime at a different mode IS updated."""
    details = _details_with_s3_endpoint()
    details["environmentVariables"] = {"SYSTEM_QUERY_MODE": "orchestrator"}
    control = _FakeControl(details=details)
    result = update_runtime_query_mode(control, "adt_erp-abc", "agent")
    assert "skipped" not in result
    assert control.update_kwargs["environmentVariables"]["SYSTEM_QUERY_MODE"] == "agent"


def test_update_runtime_query_mode_force_updates_even_when_same():
    """``force=True`` updates regardless of current mode."""
    details = _details_with_s3_endpoint()
    details["environmentVariables"] = {"SYSTEM_QUERY_MODE": "agent"}
    control = _FakeControl(details=details)
    result = update_runtime_query_mode(control, "adt_erp-abc", "agent", force=True)
    assert "skipped" not in result
    assert control.update_kwargs is not None


def test_runtime_id_by_name_matches_and_absent():
    control = _FakeControl(
        runtimes=[
            {"agentRuntimeName": "adt_erp", "agentRuntimeId": "adt_erp-xyz"},
            {"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "orch-1"},
        ]
    )
    assert runtime_id_by_name(control, "adt_erp") == "adt_erp-xyz"
    assert runtime_id_by_name(control, "adt_missing") == ""


def test_runtime_id_by_name_falls_back_to_arn_tail():
    control = _FakeControl(
        runtimes=[
            {
                "agentRuntimeName": "adt_erp",
                "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_erp-tail",
            }
        ]
    )
    assert runtime_id_by_name(control, "adt_erp") == "adt_erp-tail"


# --------------------------------------------------------------------------- #
# capture_runtime_create_kwargs — get_agent_runtime -> create_agent_runtime
# --------------------------------------------------------------------------- #
def _full_get_runtime_details():
    """A get_agent_runtime-shaped response incl. every read-only field."""
    return {
        # read-only / server-assigned (must ALL be stripped on create)
        "agentRuntimeId": "adt_erp-abc",
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_erp-abc",
        "agentRuntimeVersion": "3",
        "status": "READY",
        "failureReason": "",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastUpdatedAt": "2026-01-02T00:00:00Z",
        "workloadIdentityDetails": {"workloadIdentityArn": "arn:aws:bedrock-agentcore:::wid"},
        "metadataConfiguration": {"foo": "bar"},  # update-only, NOT a create field
        "ResponseMetadata": {"HTTPStatusCode": 200},
        # mutable config (must be carried over)
        "agentRuntimeName": "adt_erp",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "img:latest"}},
        "roleArn": "arn:aws:iam::111122223333:role/runtime",
        "protocolConfiguration": {"serverProtocol": "MCP"},
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": ["subnet-a"],
                "securityGroups": ["sg-1"],
                "requireServiceS3Endpoint": True,
            },
        },
        "environmentVariables": {
            "SYSTEM_ID": "erp",
            "AWS_REGION": "us-east-1",
            QUERY_MODE_ENV_KEY: "orchestrator",
        },
    }


_RUNTIME_READONLY = (
    "agentRuntimeId",
    "agentRuntimeArn",
    "agentRuntimeVersion",
    "status",
    "failureReason",
    "createdAt",
    "lastUpdatedAt",
    "workloadIdentityDetails",
    "metadataConfiguration",
    "ResponseMetadata",
)


def test_capture_runtime_create_kwargs_strips_readonly_fields():
    kwargs = capture_runtime_create_kwargs(_full_get_runtime_details(), "agent")
    for field in _RUNTIME_READONLY:
        assert field not in kwargs, f"read-only field {field} leaked into create kwargs"


def test_capture_runtime_create_kwargs_preserves_config_and_merges_env():
    kwargs = capture_runtime_create_kwargs(_full_get_runtime_details(), "agent")
    assert kwargs["agentRuntimeName"] == "adt_erp"
    assert kwargs["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == "img:latest"
    assert kwargs["roleArn"] == "arn:aws:iam::111122223333:role/runtime"
    assert kwargs["protocolConfiguration"] == {"serverProtocol": "MCP"}
    # env merged: SYSTEM_QUERY_MODE flipped, everything else preserved
    assert kwargs["environmentVariables"][QUERY_MODE_ENV_KEY] == "agent"
    assert kwargs["environmentVariables"]["SYSTEM_ID"] == "erp"
    assert kwargs["environmentVariables"]["AWS_REGION"] == "us-east-1"


def test_capture_runtime_create_kwargs_sanitizes_vpc_network():
    kwargs = capture_runtime_create_kwargs(_full_get_runtime_details(), "agent")
    nmc = kwargs["networkConfiguration"]["networkModeConfig"]
    assert "requireServiceS3Endpoint" not in nmc  # immutable — must not be re-sent
    assert nmc["subnets"] == ["subnet-a"]
    assert nmc["securityGroups"] == ["sg-1"]
    assert kwargs["networkConfiguration"]["networkMode"] == "VPC"


def test_capture_runtime_create_kwargs_authorizer_only_when_real():
    details = _full_get_runtime_details()
    assert "authorizerConfiguration" not in capture_runtime_create_kwargs(details, "agent")
    details["authorizerConfiguration"] = {
        "customJWTAuthorizer": {
            "discoveryUrl": "https://issuer/.well-known/openid-configuration",
            "allowedClients": ["client-1"],
        }
    }
    kwargs = capture_runtime_create_kwargs(details, "agent")
    assert kwargs["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == ["client-1"]


def test_capture_runtime_create_kwargs_carries_optional_fields():
    details = _full_get_runtime_details()
    details["description"] = "erp system agent"
    details["requestHeaderConfiguration"] = {"requestHeaderAllowlist": ["X-Session-Id"]}
    details["lifecycleConfiguration"] = {"idleRuntimeSessionTimeout": 900}
    kwargs = capture_runtime_create_kwargs(details, "orchestrator")
    assert kwargs["description"] == "erp system agent"
    assert kwargs["requestHeaderConfiguration"] == {"requestHeaderAllowlist": ["X-Session-Id"]}
    assert kwargs["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 900}


def test_capture_runtime_create_kwargs_requires_name_and_valid_mode():
    with pytest.raises(QueryModeError):
        capture_runtime_create_kwargs({"environmentVariables": {}}, "agent")
    with pytest.raises(InvalidModeError):
        capture_runtime_create_kwargs(_full_get_runtime_details(), "bogus")


# --------------------------------------------------------------------------- #
# capture_target_create_kwargs — get_gateway_target -> create_gateway_target
# --------------------------------------------------------------------------- #
_OLD_ENDPOINT = (
    "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/"
    "arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A111122223333%3Aruntime%2Fadt_erp-OLD"
    "/invocations?qualifier=DEFAULT"
)


def _full_get_target_details():
    """A get_gateway_target-shaped response incl. every read-only field."""
    return {
        # read-only / server-assigned (must ALL be stripped on create)
        "targetId": "TGT-OLD",
        "status": "READY",
        "statusReasons": [],
        "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-1",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "lastSynchronizedAt": "2026-01-02T00:00:00Z",
        "protocolType": "MCP",
        "authorizationData": {"x": "y"},
        "ResponseMetadata": {"HTTPStatusCode": 200},
        # mutable config (must be carried over)
        "name": "erp",
        "description": "System agent for ERP",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": _OLD_ENDPOINT}}},
        "credentialProviderConfigurations": [
            {
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/oauth2credentialprovider/adt-m2m",
                        "scopes": ["adt/invoke"],
                        "grantType": "CLIENT_CREDENTIALS",
                    }
                },
            }
        ],
    }


_TARGET_READONLY = (
    "targetId",
    "status",
    "statusReasons",
    "gatewayArn",
    "createdAt",
    "updatedAt",
    "lastSynchronizedAt",
    "protocolType",
    "authorizationData",
    "ResponseMetadata",
)


def test_capture_target_create_kwargs_round_trip():
    kwargs = capture_target_create_kwargs(_full_get_target_details(), "gw-1")
    assert kwargs["gatewayIdentifier"] == "gw-1"
    assert kwargs["name"] == "erp"
    assert kwargs["description"] == "System agent for ERP"
    assert kwargs["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == _OLD_ENDPOINT
    cp = kwargs["credentialProviderConfigurations"][0]
    assert cp["credentialProviderType"] == "OAUTH"
    assert cp["credentialProvider"]["oauthCredentialProvider"]["grantType"] == "CLIENT_CREDENTIALS"
    for field in _TARGET_READONLY:
        assert field not in kwargs, f"read-only field {field} leaked into create kwargs"


def test_capture_target_create_kwargs_rewrites_endpoint_without_mutation():
    details = _full_get_target_details()
    new_ep = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/NEWENC/invocations?qualifier=DEFAULT"
    kwargs = capture_target_create_kwargs(details, "gw-1", endpoint=new_ep)
    assert kwargs["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == new_ep
    # input untouched (pure)
    assert details["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == _OLD_ENDPOINT


def test_capture_target_create_kwargs_requires_name_and_gateway():
    with pytest.raises(QueryModeError):
        capture_target_create_kwargs({"targetConfiguration": {}}, "gw-1")
    with pytest.raises(QueryModeError):
        capture_target_create_kwargs(_full_get_target_details(), "")


# --------------------------------------------------------------------------- #
# recreate_runtime_with_mode — fake control recording CALL ORDER
# --------------------------------------------------------------------------- #
_MUTATIONS = (
    "delete_gateway_target",
    "delete_agent_runtime",
    "create_agent_runtime",
    "create_gateway_target",
)


class _FakeRecreateControl:
    """Stateful AgentCore control-plane stub for the recreate flow.

    Records every call in ``self.calls`` (order proof). Deletes take effect
    immediately; created runtimes/targets are READY immediately unless
    ``fail_creates`` > 0 (each failing create lands the runtime in
    CREATE_FAILED, consuming one failure).
    """

    def __init__(self, *, system_id="erp", mode_before="orchestrator",
                 has_target=True, target_status="READY",
                 fail_creates=0, create_exc=None):
        self.calls: list[str] = []
        self.created_runtime_kwargs: list[dict] = []
        self.created_target_kwargs: list[dict] = []
        self._fail_creates = fail_creates
        self._create_exc = create_exc

        self._rt_name = f"adt_{system_id}".replace("-", "_")
        self._tgt_name = system_id.replace("_", "-")
        self._details = _full_get_runtime_details()
        self._details["agentRuntimeName"] = self._rt_name
        self._details["environmentVariables"][QUERY_MODE_ENV_KEY] = mode_before

        self.runtimes = {
            self._rt_name: {
                "agentRuntimeName": self._rt_name,
                "agentRuntimeId": self._details["agentRuntimeId"],
                "agentRuntimeArn": self._details["agentRuntimeArn"],
                "status": "READY",
            }
        }
        self.targets = {}
        if has_target:
            self.targets[self._tgt_name] = {
                "name": self._tgt_name, "targetId": "TGT-OLD", "status": target_status,
            }
        self._target_detail = _full_get_target_details()
        self._target_detail["name"] = self._tgt_name

    # -- reads ------------------------------------------------------------- #
    def list_agent_runtimes(self):
        self.calls.append("list_agent_runtimes")
        return {"agentRuntimeSummaries": [dict(v) for v in self.runtimes.values()]}

    def get_agent_runtime(self, agentRuntimeId):
        self.calls.append("get_agent_runtime")
        if agentRuntimeId == self._details["agentRuntimeId"]:
            return dict(self._details)
        for rt in self.runtimes.values():
            if rt["agentRuntimeId"] == agentRuntimeId:
                return {**rt, "failureReason": "stranded-subnet"}
        return {}

    def list_gateway_targets(self, gatewayIdentifier, **_kw):
        self.calls.append("list_gateway_targets")
        return {"items": [dict(v) for v in self.targets.values()]}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        self.calls.append("get_gateway_target")
        return dict(self._target_detail)

    # -- mutations ---------------------------------------------------------- #
    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.calls.append("delete_gateway_target")
        self.targets.pop(self._tgt_name, None)

    def delete_agent_runtime(self, agentRuntimeId):
        self.calls.append("delete_agent_runtime")
        self.runtimes.pop(self._rt_name, None)

    def create_agent_runtime(self, **kwargs):
        self.calls.append("create_agent_runtime")
        self.created_runtime_kwargs.append(kwargs)
        if self._create_exc is not None:
            raise self._create_exc
        n = len(self.created_runtime_kwargs)
        if self._fail_creates > 0:
            self._fail_creates -= 1
            status = "CREATE_FAILED"
        else:
            status = "READY"
        new_id = f"{self._rt_name}-NEW{n}"
        arn = f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{new_id}"
        self.runtimes[self._rt_name] = {
            "agentRuntimeName": self._rt_name, "agentRuntimeId": new_id,
            "agentRuntimeArn": arn, "status": status,
        }
        return {"agentRuntimeArn": arn, "agentRuntimeId": new_id}

    def create_gateway_target(self, **kwargs):
        self.calls.append("create_gateway_target")
        self.created_target_kwargs.append(kwargs)
        self.targets[kwargs["name"]] = {
            "name": kwargs["name"], "targetId": "TGT-NEW", "status": "READY",
        }
        return {"targetId": "TGT-NEW"}

    # -- helpers ------------------------------------------------------------ #
    def mutation_calls(self):
        return [c for c in self.calls if c in _MUTATIONS]


_NO_SLEEP = {"sleep": lambda _s: None}


def test_recreate_calls_mutations_in_order_with_waits():
    control = _FakeRecreateControl()
    result = recreate_runtime_with_mode(
        control, "gw-1", "erp", "agent", region="us-east-1",
        log=lambda _m: None, **_NO_SLEEP,
    )
    # ORDER proof: delete target -> delete runtime -> create runtime -> create target.
    assert control.mutation_calls() == [
        "delete_gateway_target",
        "delete_agent_runtime",
        "create_agent_runtime",
        "create_gateway_target",
    ]
    # waits happened: readiness polls (list_*) occur AFTER each create
    create_rt_idx = control.calls.index("create_agent_runtime")
    create_tgt_idx = control.calls.index("create_gateway_target")
    assert "list_agent_runtimes" in control.calls[create_rt_idx:create_tgt_idx]  # runtime READY wait
    assert "list_gateway_targets" in control.calls[create_tgt_idx:]  # target READY wait
    # the config was captured BEFORE deletion (get_* precede the first delete)
    first_delete = control.calls.index("delete_gateway_target")
    assert control.calls.index("get_agent_runtime") < first_delete
    assert control.calls.index("get_gateway_target") < first_delete
    assert result["recreated"] is True
    assert result["target_recreated"] is True


def test_recreate_sets_mode_and_repoints_target_at_new_arn():
    control = _FakeRecreateControl(mode_before="orchestrator")
    result = recreate_runtime_with_mode(
        control, "gw-1", "erp", "agent", region="us-east-1",
        log=lambda _m: None, **_NO_SLEEP,
    )
    # created runtime: env flipped, everything else preserved, read-only stripped
    ck = control.created_runtime_kwargs[0]
    assert ck["environmentVariables"][QUERY_MODE_ENV_KEY] == "agent"
    assert ck["environmentVariables"]["SYSTEM_ID"] == "erp"
    assert "agentRuntimeId" not in ck and "agentRuntimeArn" not in ck
    assert "requireServiceS3Endpoint" not in ck["networkConfiguration"]["networkModeConfig"]
    # created target: endpoint embeds the NEW runtime ARN (URL-encoded), not the old
    new_arn = result["runtime_arn"]
    tk = control.created_target_kwargs[0]
    endpoint = tk["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"]
    assert new_arn.replace(":", "%3A").replace("/", "%2F") in endpoint
    assert "OLD" not in endpoint
    # credential config carried over
    assert tk["credentialProviderConfigurations"][0]["credentialProviderType"] == "OAUTH"


def test_recreate_skips_fast_when_already_at_mode_and_target_ready():
    control = _FakeRecreateControl(mode_before="agent", target_status="READY")
    result = recreate_runtime_with_mode(
        control, "gw-1", "erp", "agent", region="us-east-1",
        log=lambda _m: None, **_NO_SLEEP,
    )
    assert result == {"skipped": True, "current": "agent"}
    assert control.mutation_calls() == []  # NOTHING was touched


def test_recreate_does_not_skip_when_target_not_ready():
    # Runtime already at mode but target FAILED (e.g. stuck SYNCHRONIZE_UNSUCCESSFUL)
    # -> full recreate heals it.
    control = _FakeRecreateControl(mode_before="agent", target_status="FAILED")
    result = recreate_runtime_with_mode(
        control, "gw-1", "erp", "agent", region="us-east-1",
        log=lambda _m: None, **_NO_SLEEP,
    )
    assert result["recreated"] is True
    assert control.mutation_calls()[0] == "delete_gateway_target"


def test_recreate_propagates_create_failure_and_never_creates_target():
    control = _FakeRecreateControl(create_exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        recreate_runtime_with_mode(
            control, "gw-1", "erp", "agent", region="us-east-1",
            log=lambda _m: None, **_NO_SLEEP,
        )
    assert "create_gateway_target" not in control.calls


def test_recreate_bounded_single_retrigger_on_stranded_create():
    # First create strands terminal CREATE_FAILED -> delete + re-create ONCE, succeed.
    control = _FakeRecreateControl(fail_creates=1)
    result = recreate_runtime_with_mode(
        control, "gw-1", "erp", "agent", region="us-east-1",
        log=lambda _m: None, **_NO_SLEEP,
    )
    assert control.mutation_calls() == [
        "delete_gateway_target",
        "delete_agent_runtime",
        "create_agent_runtime",   # 1st create -> CREATE_FAILED
        "delete_agent_runtime",   # bounded cleanup of the stranded runtime
        "create_agent_runtime",   # exactly-once re-trigger -> READY
        "create_gateway_target",
    ]
    assert result["recreated"] is True


def test_recreate_second_terminal_failure_propagates():
    # BOTH creates strand -> the second RuntimeCreateFailedError propagates
    # (no infinite loop): exactly 2 create attempts, no target created.
    control = _FakeRecreateControl(fail_creates=2)
    with pytest.raises(RuntimeCreateFailedError):
        recreate_runtime_with_mode(
            control, "gw-1", "erp", "agent", region="us-east-1",
            log=lambda _m: None, **_NO_SLEEP,
        )
    assert control.calls.count("create_agent_runtime") == 2
    assert "create_gateway_target" not in control.calls


def test_recreate_absent_target_rebuilds_from_sibling_template():
    """Re-entrant path: target already deleted by an interrupted run → rebuilt
    from a sibling runtime-backed target (name + endpoint overridden)."""
    control = _FakeRecreateControl(has_target=False)
    sib_detail = _full_get_target_details()
    sib_detail["name"] = "sibling-sys"
    control.targets["sibling-sys"] = {
        "name": "sibling-sys", "targetId": "TGT-SIB", "status": "READY",
    }
    control._target_detail = sib_detail  # get_gateway_target returns this
    result = recreate_runtime_with_mode(
        control, "gw-1", "erp", "agent", region="us-east-1",
        log=lambda _m: None, **_NO_SLEEP,
    )
    assert result["recreated"] is True
    assert result["target_recreated"] is True
    assert "create_gateway_target" in control.calls
    created = control.created_target_kwargs[-1]
    assert created["name"] == "erp"  # NOT the sibling's name
    ep = created["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"]
    assert "adt_erp" in ep  # endpoint points at the NEW runtime, not the sibling


def test_recreate_absent_target_no_sibling_fails_loud():
    control = _FakeRecreateControl(has_target=False)
    with pytest.raises(QueryModeError, match="no runtime-backed sibling"):
        recreate_runtime_with_mode(
            control, "gw-1", "erp", "agent", region="us-east-1",
            log=lambda _m: None, **_NO_SLEEP,
        )
    assert control.mutation_calls() == []  # nothing touched before the guard


def test_recreate_missing_runtime_fails_loud():
    control = _FakeRecreateControl()
    control.runtimes.clear()
    with pytest.raises(QueryModeError, match="not found"):
        recreate_runtime_with_mode(
            control, "gw-1", "erp", "agent", region="us-east-1",
            log=lambda _m: None, **_NO_SLEEP,
        )
    assert control.mutation_calls() == []


def test_recreate_requires_gateway_id_and_valid_mode():
    control = _FakeRecreateControl()
    with pytest.raises(QueryModeError):
        recreate_runtime_with_mode(
            control, "", "erp", "agent", region="us-east-1", **_NO_SLEEP,
        )
    with pytest.raises(InvalidModeError):
        recreate_runtime_with_mode(
            control, "gw-1", "erp", "bogus", region="us-east-1", **_NO_SLEEP,
        )
    assert control.mutation_calls() == []


_TESTS = [
    test_validate_mode_accepts_valid,
    test_validate_mode_rejects_invalid,
    test_merged_environment_preserves_and_overwrites,
    test_merged_environment_adds_key_when_absent,
    test_merged_environment_handles_none,
    test_merged_environment_validates_mode,
    test_select_all_excludes_infra_runtimes,
    test_select_all_empty_when_only_infra,
    test_select_explicit_maps_ids_in_order,
    test_build_update_kwargs_preserves_config_and_sets_mode,
    test_build_update_kwargs_drops_require_service_s3_endpoint,
    test_build_update_kwargs_omits_empty_authorizer,
    test_build_update_kwargs_keeps_real_authorizer,
    test_build_update_kwargs_defaults_public_network,
    test_build_update_kwargs_requires_runtime_id,
    test_update_runtime_query_mode_reads_then_updates,
    test_update_runtime_query_mode_validates_mode,
    test_update_runtime_query_mode_skips_when_already_at_mode,
    test_update_runtime_query_mode_updates_when_mode_differs,
    test_update_runtime_query_mode_force_updates_even_when_same,
    test_runtime_id_by_name_matches_and_absent,
    test_runtime_id_by_name_falls_back_to_arn_tail,
    test_capture_runtime_create_kwargs_strips_readonly_fields,
    test_capture_runtime_create_kwargs_preserves_config_and_merges_env,
    test_capture_runtime_create_kwargs_sanitizes_vpc_network,
    test_capture_runtime_create_kwargs_authorizer_only_when_real,
    test_capture_runtime_create_kwargs_carries_optional_fields,
    test_capture_runtime_create_kwargs_requires_name_and_valid_mode,
    test_capture_target_create_kwargs_round_trip,
    test_capture_target_create_kwargs_rewrites_endpoint_without_mutation,
    test_capture_target_create_kwargs_requires_name_and_gateway,
    test_recreate_calls_mutations_in_order_with_waits,
    test_recreate_sets_mode_and_repoints_target_at_new_arn,
    test_recreate_skips_fast_when_already_at_mode_and_target_ready,
    test_recreate_does_not_skip_when_target_not_ready,
    test_recreate_propagates_create_failure_and_never_creates_target,
    test_recreate_bounded_single_retrigger_on_stranded_create,
    test_recreate_second_terminal_failure_propagates,
    test_recreate_absent_target_rebuilds_from_sibling_template,
    test_recreate_absent_target_no_sibling_fails_loud,
    test_recreate_missing_runtime_fails_loud,
    test_recreate_requires_gateway_id_and_valid_mode,
]


# --------------------------------------------------------------------------- #
# delete-deadline defaults match reonboard.py (the SAME runtime delete path)
# --------------------------------------------------------------------------- #
def test_delete_deadline_defaults_match_reonboard_600s():
    """VPC-mode runtime teardown can exceed 180s; reonboard.py waits 600s on
    the same ``delete_runtime`` path, so the set_query_mode CLI (which passes
    the deadline explicitly down ``run`` → ``recreate_system_runtimes`` →
    ``recreate_runtime_with_mode``) defaults to 600s too, as does the
    system-recreate entry point."""
    import inspect as _inspect

    import set_query_mode

    sig = _inspect.signature(set_query_mode.recreate_system_runtimes)
    assert sig.parameters["delete_deadline_s"].default == 600.0

    captured: dict = {}
    import argparse
    real_parse = argparse.ArgumentParser.parse_args

    def _spy(self, argv=None, namespace=None):
        # parse_args succeeded — capture and stop before any AWS work
        captured["delete_deadline"] = real_parse(self, argv, namespace).delete_deadline
        raise SystemExit(0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(argparse.ArgumentParser, "parse_args", _spy)
        with pytest.raises(SystemExit):
            set_query_mode.main(["agent"])
    assert captured["delete_deadline"] == 600.0


if __name__ == "__main__":
    # Standalone runner ignores parametrize; run the non-parametrized ones.
    import inspect

    for t in _TESTS:
        if inspect.signature(t).parameters:
            continue
        t()
        print(f"  ok  {t.__name__}")
    print("RESULT: PASS (run via pytest for full parametrized coverage)")
