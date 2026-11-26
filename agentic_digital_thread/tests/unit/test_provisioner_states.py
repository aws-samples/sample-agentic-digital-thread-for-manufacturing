# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the onboarding provisioner handlers (Wave 2, 2.3R).

Each discrete handler is exercised in isolation with a stubbed AgentCore control
client / Secrets Manager client / fake session store — no live AWS. Highlights
(plan §2.3R acceptance):

* ``validate_session`` rejects a non-``provisioning`` session AND a mismatched
  execution ARN (the orphan-execution gate), and accepts the matching case;
* ``wait_ready`` treats ``CREATE_FAILED`` as a terminal failure that surfaces the
  AgentCore ``failureReason``;
* ``write_terminal_status`` writes ``failed`` on an upstream error and
  ``onboarded`` on success (delegating to ``store.finalize``);
* ``create_runtime`` / ``create_target`` / ``synchronize`` / ``verify`` build the
  correct control-plane calls and propagate terminal errors (no fallback).

Run:
  * pytest:     pytest agentic_digital_thread/tests/unit/test_provisioner_states.py
  * standalone: python agentic_digital_thread/tests/unit/test_provisioner_states.py
"""
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import pytest  # noqa: E402

from agents.onboarding_session import (  # noqa: E402
    STATUS_FAILED,
    STATUS_ONBOARDED,
    STATUS_PROVISIONING,
    STATUS_RESOLVED,
    SessionNotFoundError,
)
from agents.provisioner import (  # noqa: E402
    CreateRuntimeInput,
    CreateTargetInput,
    GatewaySyncError,
    OrphanExecutionError,
    ProvisionerError,
    RuntimeCreateFailedError,
    RuntimeNotReadyError,
    SessionValidationError,
    SynchronizeInput,
    TargetNotReadyError,
    ValidateSessionInput,
    VerifyInput,
    WaitReadyInput,
    WriteTerminalStatusInput,
    create_runtime,
    create_target,
    synchronize,
    validate_session,
    verify,
    wait_ready,
    write_terminal_status,
)

_EXEC_ARN = "arn:aws:states:us-east-1:111122223333:execution:onboarding-provisioner:exec-1"
_OTHER_ARN = "arn:aws:states:us-east-1:111122223333:execution:onboarding-provisioner:exec-2"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeStore:
    """Minimal OnboardingSessionStore stand-in (get_session + finalize)."""

    def __init__(self, item=None, *, missing=False):
        self._item = item
        self._missing = missing
        self.finalize_calls = []

    def get_session(self, system_id):
        if self._missing or self._item is None:
            raise SessionNotFoundError(system_id)
        return dict(self._item)

    def finalize(self, system_id, status, **fields):
        self.finalize_calls.append((system_id, status, fields))


class _ConflictException(Exception):
    pass


class _ResourceExistsException(Exception):
    pass


class _Exceptions:
    ConflictException = _ConflictException
    ResourceExistsException = _ResourceExistsException


class _FakeControl:
    """AgentCore control-plane stub; behavior is scripted per-test."""

    def __init__(self):
        self.exceptions = _Exceptions()
        self.created_runtime_kwargs = None
        self.created_target_kwargs = None
        self.sync_calls = []
        self._runtime_statuses = []  # popped left-to-right on get_agent_runtime
        self._target_detail = {"status": "READY"}
        self._sync_errors = []  # exceptions to raise on synchronize, then success
        self._create_target_conflict = False
        self._existing_targets = []
        self._create_runtime_conflict = False
        self._existing_runtimes = []

    # -- runtime --
    def create_agent_runtime(self, **kwargs):
        self.created_runtime_kwargs = kwargs
        if self._create_runtime_conflict:
            raise self.exceptions.ConflictException("runtime exists")
        return {
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_erp-abc",
            "agentRuntimeId": "adt_erp-abc",
        }

    def list_agent_runtimes(self):
        return {"agentRuntimeSummaries": self._existing_runtimes}

    def get_agent_runtime(self, agentRuntimeId):
        if self._runtime_statuses:
            return self._runtime_statuses.pop(0)
        return {"status": "READY"}

    # -- gateway target --
    def create_gateway_target(self, **kwargs):
        self.created_target_kwargs = kwargs
        if self._create_target_conflict:
            raise self.exceptions.ConflictException("target exists")
        return {"targetId": "tgt-123"}

    def list_gateway_targets(self, gatewayIdentifier):
        return {"items": self._existing_targets}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return self._target_detail

    def synchronize_gateway_targets(self, gatewayIdentifier, targetIdList):
        self.sync_calls.append(list(targetIdList))
        if self._sync_errors:
            raise self._sync_errors.pop(0)
        return {}


class _FakeSecrets:
    def __init__(self, *, exists=False):
        self.exceptions = _Exceptions()
        self._exists = exists
        self.created = None
        self.put = None

    def create_secret(self, Name, SecretString):
        if self._exists:
            raise self.exceptions.ResourceExistsException("exists")
        self.created = {"Name": Name, "SecretString": SecretString}
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{Name}"}

    def put_secret_value(self, SecretId, SecretString):
        self.put = {"SecretId": SecretId, "SecretString": SecretString}
        return {}

    def describe_secret(self, SecretId):
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{SecretId}"}


def _provisioning_item(arn=_EXEC_ARN):
    return {
        "system_id": "erp",
        "status": STATUS_PROVISIONING,
        "initiated_by": "user-1",
        "provision_execution_arn": arn,
    }


# --------------------------------------------------------------------------- #
# validate_session
# --------------------------------------------------------------------------- #
def test_validate_session_accepts_provisioning_with_matching_arn():
    store = _FakeStore(_provisioning_item(_EXEC_ARN))
    res = validate_session(
        ValidateSessionInput(system_id="erp", execution_arn=_EXEC_ARN), store=store
    )
    assert res.system_id == "erp"
    assert res.status == STATUS_PROVISIONING
    assert res.initiated_by == "user-1"
    assert res.provision_execution_arn == _EXEC_ARN


def test_validate_session_rejects_non_provisioning_status():
    # Defense in depth: a session that did not win the claim is refused.
    item = _provisioning_item(_EXEC_ARN)
    item["status"] = STATUS_RESOLVED
    store = _FakeStore(item)
    with pytest.raises(SessionValidationError):
        validate_session(
            ValidateSessionInput(system_id="erp", execution_arn=_EXEC_ARN), store=store
        )


def test_validate_session_rejects_mismatched_execution_arn():
    # Orphan-execution gate: the recorded ARN belongs to a DIFFERENT execution,
    # so THIS execution is not the claim winner and must be rejected.
    store = _FakeStore(_provisioning_item(_OTHER_ARN))
    with pytest.raises(OrphanExecutionError):
        validate_session(
            ValidateSessionInput(system_id="erp", execution_arn=_EXEC_ARN), store=store
        )


def test_validate_session_propagates_missing_session():
    store = _FakeStore(missing=True)
    with pytest.raises(SessionNotFoundError):
        validate_session(
            ValidateSessionInput(system_id="erp", execution_arn=_EXEC_ARN), store=store
        )


# --------------------------------------------------------------------------- #
# create_runtime
# --------------------------------------------------------------------------- #
def test_create_runtime_stores_secret_and_creates_runtime():
    control = _FakeControl()
    secrets = _FakeSecrets()
    inp = CreateRuntimeInput(
        system_id="erp",
        system_name="ERP",
        schema="erp_schema",
        connection_type="PostgreSQL",
        connection_config={"host": "db"},
        conn_params={"user": "postgres"},
        region="us-east-1",
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
        vpc_subnets=["subnet-a"],
        vpc_security_group="sg-1",
        m2m_discovery_url="https://issuer/.well-known/openid-configuration",
        m2m_client_id="client-1",
    )
    res = create_runtime(inp, control=control, secrets=secrets)
    assert res.runtime_id == "adt_erp-abc"
    assert res.runtime_mcp_url.startswith("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/")
    assert res.connection_secret_arn.endswith("adt/agent-config/erp")
    # env carries the secret ARN and the JWT authorizer is set; bare repo got :latest
    kw = control.created_runtime_kwargs
    assert kw["environmentVariables"]["CONNECTION_SECRET_ARN"] == res.connection_secret_arn
    assert kw["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == ["client-1"]
    assert kw["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"].endswith(":latest")
    assert kw["networkConfiguration"]["networkMode"] == "VPC"


def test_create_runtime_knowledge_graph_skips_secret():
    control = _FakeControl()
    secrets = _FakeSecrets()
    inp = CreateRuntimeInput(
        system_id="digital-thread",
        connection_type="KnowledgeGraph",
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent:sha-1",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
    )
    res = create_runtime(inp, control=control, secrets=secrets)
    # No DB secret for the knowledge-graph system.
    assert res.connection_secret_arn == ""  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    assert secrets.created is None
    assert "CONNECTION_SECRET_ARN" not in control.created_runtime_kwargs["environmentVariables"]
    # already-tagged image URI is NOT re-suffixed
    uri = control.created_runtime_kwargs["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
    assert uri.endswith(":sha-1")


def test_create_runtime_reuses_existing_on_conflict():
    # SFN retry / re-onboard: create_agent_runtime raises ConflictException because
    # adt_<id> already exists — reuse the existing runtime rather than hard-failing.
    control = _FakeControl()
    control._create_runtime_conflict = True
    control._existing_runtimes = [
        {
            "agentRuntimeName": "adt_erp",
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_erp-xyz",
            "agentRuntimeId": "adt_erp-xyz",
        }
    ]
    secrets = _FakeSecrets(exists=True)
    inp = CreateRuntimeInput(
        system_id="erp",
        connection_type="PostgreSQL",
        connection_config={"host": "db"},
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent:tag",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
    )
    res = create_runtime(inp, control=control, secrets=secrets)
    assert res.runtime_id == "adt_erp-xyz"
    assert res.runtime_arn.endswith("runtime/adt_erp-xyz")


def test_create_runtime_conflict_without_match_raises():
    # ConflictException but no runtime with that name found in list_agent_runtimes:
    # fail fast (typed ProvisionerError) rather than returning an empty id.
    control = _FakeControl()
    control._create_runtime_conflict = True
    control._existing_runtimes = []  # nothing to reuse
    secrets = _FakeSecrets(exists=True)
    inp = CreateRuntimeInput(
        system_id="erp",
        connection_type="PostgreSQL",
        connection_config={"host": "db"},
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent:tag",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
    )
    with pytest.raises(ProvisionerError):
        create_runtime(inp, control=control, secrets=secrets)


def test_create_runtime_sets_system_query_mode_default_agent(monkeypatch):
    # ADR-0035: create_runtime must set SYSTEM_QUERY_MODE, defaulting to "agent"
    # (mirrors agent_manager._base_env_vars) when the env is unset, so the SFN
    # onboarding path agrees with the in-app path.
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    control = _FakeControl()
    secrets = _FakeSecrets()
    inp = CreateRuntimeInput(
        system_id="erp",
        connection_type="PostgreSQL",
        connection_config={"host": "db"},
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent:tag",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
    )
    create_runtime(inp, control=control, secrets=secrets)
    assert control.created_runtime_kwargs["environmentVariables"]["SYSTEM_QUERY_MODE"] == "agent"


def test_create_runtime_honors_system_query_mode_env(monkeypatch):
    # When SYSTEM_QUERY_MODE is set in the provisioner Lambda env (from
    # onboarding_stack's system_query_mode context), create_runtime propagates it.
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    control = _FakeControl()
    secrets = _FakeSecrets()
    inp = CreateRuntimeInput(
        system_id="erp",
        connection_type="PostgreSQL",
        connection_config={"host": "db"},
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent:tag",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
    )
    create_runtime(inp, control=control, secrets=secrets)
    assert (
        control.created_runtime_kwargs["environmentVariables"]["SYSTEM_QUERY_MODE"]
        == "orchestrator"
    )
def test_create_runtime_forwards_onboarding_sessions_table_env(monkeypatch):
    # Wave 5 provenance sidecar (the Wave-4 SFN env-gap precedent): the SFN
    # path must emit ONBOARDING_SESSIONS_TABLE exactly like
    # agent_manager._prepare_runtime_env — ambient env when set, else the
    # CDK-fixed physical table name (runtime_provision_helpers.
    # provenance_store_env), so an env-starved provisioner process can never
    # create a runtime without the provenance store.
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "custom-sessions-table")
    control = _FakeControl()
    secrets = _FakeSecrets()
    inp = CreateRuntimeInput(
        system_id="erp",
        connection_type="PostgreSQL",
        connection_config={"host": "db"},
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent:tag",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
    )
    create_runtime(inp, control=control, secrets=secrets)
    env = control.created_runtime_kwargs["environmentVariables"]
    assert env["ONBOARDING_SESSIONS_TABLE"] == "custom-sessions-table"

    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    control2 = _FakeControl()
    create_runtime(inp, control=control2, secrets=_FakeSecrets())
    env2 = control2.created_runtime_kwargs["environmentVariables"]
    assert env2["ONBOARDING_SESSIONS_TABLE"] == "adt-onboarding-sessions"


def test_wait_ready_returns_on_ready():
    control = _FakeControl()
    control._runtime_statuses = [{"status": "CREATING"}, {"status": "READY"}]
    res = wait_ready(
        WaitReadyInput(runtime_id="adt_erp-abc"), control=control, sleep=lambda s: None
    )
    assert res.status == "READY"


def test_wait_ready_create_failed_is_terminal_with_reason():
    control = _FakeControl()
    control._runtime_statuses = [
        {"status": "CREATE_FAILED", "failureReason": "unsupported availability zone subnet-xyz"}
    ]
    with pytest.raises(RuntimeCreateFailedError) as exc:
        wait_ready(WaitReadyInput(runtime_id="adt_erp-abc"), control=control, sleep=lambda s: None)
    # The AgentCore failureReason is surfaced verbatim.
    assert "unsupported availability zone subnet-xyz" in str(exc.value)
    assert exc.value.failure_reason == "unsupported availability zone subnet-xyz"


def test_wait_ready_times_out():
    control = _FakeControl()
    control._runtime_statuses = [{"status": "CREATING"}] * 5
    # monotonic jumps past the deadline on the 2nd read.
    ticks = iter([0.0, 0.0, 1000.0, 2000.0, 3000.0, 4000.0])
    with pytest.raises(RuntimeNotReadyError):
        wait_ready(
            WaitReadyInput(runtime_id="adt_erp-abc"),
            control=control,
            sleep=lambda s: None,
            monotonic=lambda: next(ticks),
            deadline_seconds=300.0,
        )


def test_wait_ready_min_version_skips_a_ready_at_an_older_version():
    control = _FakeControl()
    control._runtime_statuses = [
        {"status": "READY", "agentRuntimeVersion": "6"},
        {"status": "UPDATING", "agentRuntimeVersion": "7"},
        {"status": "READY", "agentRuntimeVersion": "7"},
    ]
    res = wait_ready(WaitReadyInput(runtime_id="adt_erp-abc"), control=control,
                     sleep=lambda s: None, min_version="7")
    assert (res.status, res.version, res.stale_polls) == ("READY", "7", 1)
    assert res.to_dict() == {"runtime_id": "adt_erp-abc", "status": "READY"}


def test_version_reached_compares_decimal_versions_only():
    from agents.provisioner.handlers import version_reached
    assert version_reached("7", "7") and version_reached("10", "9")
    assert not version_reached("9", "10")
    assert not version_reached("v8", "7") and not version_reached("8", "")
    assert version_reached("v8", "v8")


# --------------------------------------------------------------------------- #
# create_target
# --------------------------------------------------------------------------- #
def test_create_target_creates_with_oauth_credentials():
    control = _FakeControl()
    inp = CreateTargetInput(
        system_id="erp",
        system_name="ERP",
        gateway_id="gw-1",
        runtime_mcp_url="https://runtime/mcp",
        credential_provider_arn="arn:aws:.../adt-m2m",
    )
    res = create_target(inp, control=control)
    assert res.gateway_target_id == "tgt-123"
    kw = control.created_target_kwargs
    assert kw["name"] == "erp"
    assert kw["credentialProviderConfigurations"][0]["credentialProviderType"] == "OAUTH"
    assert kw["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == "https://runtime/mcp"


def test_create_target_reuses_existing_on_conflict():
    control = _FakeControl()
    control._create_target_conflict = True
    control._existing_targets = [{"name": "erp", "targetId": "tgt-existing"}]
    # The existing target already points at this runtime: reused as is.
    control._target_detail = {
        "status": "READY",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": "https://runtime/mcp"}}},
    }
    inp = CreateTargetInput(
        system_id="erp", system_name="ERP", gateway_id="gw-1", runtime_mcp_url="https://runtime/mcp"
    )
    res = create_target(inp, control=control)
    assert res.gateway_target_id == "tgt-existing"


def test_create_target_conflict_without_match_raises():
    # ConflictException but no target with that name in list_gateway_targets:
    # fail fast (typed ProvisionerError) instead of returning an empty target id
    # (which would defer the failure to synchronize with nothing to sync).
    control = _FakeControl()
    control._create_target_conflict = True
    control._existing_targets = []  # conflict, but no name match
    inp = CreateTargetInput(
        system_id="erp", system_name="ERP", gateway_id="gw-1", runtime_mcp_url="https://runtime/mcp"
    )
    with pytest.raises(ProvisionerError):
        create_target(inp, control=control)


# --------------------------------------------------------------------------- #
# synchronize
# --------------------------------------------------------------------------- #
def test_synchronize_succeeds_first_try():
    control = _FakeControl()
    res = synchronize(
        SynchronizeInput(gateway_id="gw-1", gateway_target_id="tgt-123"),
        control=control,
        sleep=lambda s: None,
    )
    assert res.synchronized is True
    assert control.sync_calls == [["tgt-123"]]


def test_synchronize_retries_creating_then_succeeds():
    control = _FakeControl()
    control._sync_errors = [Exception("Target is in Creating state")]
    res = synchronize(
        SynchronizeInput(gateway_id="gw-1", gateway_target_id="tgt-123"),
        control=control,
        sleep=lambda s: None,
    )
    assert res.synchronized is True
    assert len(control.sync_calls) == 2


def test_synchronize_exhausts_retries_raises():
    control = _FakeControl()
    control._sync_errors = [Exception("SYNCHRONIZE_UNSUCCESSFUL")] * 10
    with pytest.raises(GatewaySyncError):
        synchronize(
            SynchronizeInput(gateway_id="gw-1", gateway_target_id="tgt-123"),
            control=control,
            sleep=lambda s: None,
            max_attempts=3,
        )


def test_synchronize_unexpected_error_propagates_without_retry():
    control = _FakeControl()
    control._sync_errors = [ValueError("boom"), ValueError("boom2")]
    with pytest.raises(ValueError):
        synchronize(
            SynchronizeInput(gateway_id="gw-1", gateway_target_id="tgt-123"),
            control=control,
            sleep=lambda s: None,
        )
    # Did not blindly keep retrying: exactly one attempt before the raise.
    assert len(control.sync_calls) == 1


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def test_verify_ready():
    control = _FakeControl()
    control._target_detail = {"status": "READY"}
    res = verify(VerifyInput(gateway_id="gw-1", gateway_target_id="tgt-123"), control=control)
    assert res.status == "READY"


def test_verify_not_ready_raises():
    control = _FakeControl()
    control._target_detail = {"status": "FAILED"}
    with pytest.raises(TargetNotReadyError):
        verify(VerifyInput(gateway_id="gw-1", gateway_target_id="tgt-123"), control=control)


# --------------------------------------------------------------------------- #
# write_terminal_status
# --------------------------------------------------------------------------- #
def test_write_terminal_status_onboarded_on_success():
    store = _FakeStore(_provisioning_item())
    res = write_terminal_status(
        WriteTerminalStatusInput(system_id="erp", status=STATUS_ONBOARDED), store=store
    )
    assert res.status == STATUS_ONBOARDED
    assert store.finalize_calls == [("erp", STATUS_ONBOARDED, {})]


def test_write_terminal_status_failed_on_upstream_error():
    # The state machine catch path: an upstream handler error → failed + reason.
    store = _FakeStore(_provisioning_item())
    res = write_terminal_status(
        WriteTerminalStatusInput(
            system_id="erp",
            status=STATUS_FAILED,
            failure_reason="runtime CREATE_FAILED: unsupported availability zone",
        ),
        store=store,
    )
    assert res.status == STATUS_FAILED
    assert len(store.finalize_calls) == 1
    sid, status, fields = store.finalize_calls[0]
    assert sid == "erp"
    assert status == STATUS_FAILED
    assert fields["failure_reason"].startswith("runtime CREATE_FAILED")


def test_write_terminal_status_rejects_non_terminal_status():
    store = _FakeStore(_provisioning_item())
    with pytest.raises(ValueError):
        write_terminal_status(
            WriteTerminalStatusInput(system_id="erp", status="provisioning"), store=store
        )
    assert store.finalize_calls == []


_TESTS = [
    test_validate_session_accepts_provisioning_with_matching_arn,
    test_validate_session_rejects_non_provisioning_status,
    test_validate_session_rejects_mismatched_execution_arn,
    test_validate_session_propagates_missing_session,
    test_create_runtime_stores_secret_and_creates_runtime,
    test_create_runtime_knowledge_graph_skips_secret,
    test_create_runtime_reuses_existing_on_conflict,
    test_create_runtime_conflict_without_match_raises,
    test_wait_ready_returns_on_ready,
    test_wait_ready_create_failed_is_terminal_with_reason,
    test_wait_ready_times_out,
    test_wait_ready_min_version_skips_a_ready_at_an_older_version,
    test_version_reached_compares_decimal_versions_only,
    test_create_target_creates_with_oauth_credentials,
    test_create_target_reuses_existing_on_conflict,
    test_create_target_conflict_without_match_raises,
    test_synchronize_succeeds_first_try,
    test_synchronize_retries_creating_then_succeeds,
    test_synchronize_exhausts_retries_raises,
    test_synchronize_unexpected_error_propagates_without_retry,
    test_verify_ready,
    test_verify_not_ready_raises,
    test_write_terminal_status_onboarded_on_success,
    test_write_terminal_status_failed_on_upstream_error,
    test_write_terminal_status_rejects_non_terminal_status,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} provisioner-state tests passed")
