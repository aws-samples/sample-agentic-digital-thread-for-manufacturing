# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/provision_runtimes.py — the shared runtime-provisioning driver.

Offline (hand-stub fakes — no moto in this repo). Pins the plan's Task 1
acceptance:

* step-parity goldens — the exact create/update kwargs match the deploy.sh
  §2c/§2e call shapes (orchestrator update has NO protocol/authorizer;
  onboarding carries MCP + the M2M JWT; netcfg round-trips with
  ``requireServiceS3Endpoint`` stripped; env == ``build_env`` byte-for-byte);
* ``strip_unsupported_subnets`` parses multi-subnet failureReasons and
  returns None for non-AZ reasons; the create loop stops at 3 attempts /
  empty subnet list with a raised error naming the reason;
* fake-clock caps — create poll 300 s, delete-wait 120 s (24×5 s), sync
  exhaustion raises GatewaySyncError;
* ``sweep_adt_runtimes`` deletes targets before runtimes, is ``adt_``-prefix
  scoped, tolerates NotFound mid-sweep, raises on a survivor;
* import hygiene (closure = stdlib + boto3 + the agents leaf modules) and
  ``os.environ`` is never mutated;
* CLI: one JSON object on stdout, diagnostics on stderr, nonzero on failure.
"""
import importlib.util
import json
import os
import subprocess  # nosec B404 - import-hygiene measurement, fixed argv
import sys
import types
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.provisioner.errors import (  # noqa: E402
    GatewaySyncError,
    RuntimeCreateFailedError,
    RuntimeNotReadyError,
)
from agents.provisioner.handlers import create_target  # noqa: E402
from agents.provisioner.models import CreateTargetInput  # noqa: E402
from agents.runtime_env_contract import (  # noqa: E402
    RuntimeEnvSources,
    build_env,
)

_DRIVER_PATH = _ADT_ROOT / "scripts" / "provision_runtimes.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "provision_runtimes", _DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("provision_runtimes", module)
    spec.loader.exec_module(module)
    return module


driver = _load_driver()

_CLAIM_ARN_FIXTURE = "arn:aws:secretsmanager:us-east-1:111122223333:secret:idc"
_SOURCES = RuntimeEnvSources(
    region="us-east-1", graph_id="g-1", gateway_url="https://gw",
    gateway_id="GW1", app_service_client_id="c1", app_user_pool_id="p1",
    identity_claim_secret_arn=_CLAIM_ARN_FIXTURE,
    onboarding_sessions_table="adt-onboarding-sessions")

_IMAGE = "111122223333.dkr.ecr.us-east-1.amazonaws.com/adt:abc123"
_ROLE = "arn:aws:iam::111122223333:role/adt-runtime"
_ONB_ROLE = "arn:aws:iam::111122223333:role/adt-onboarding-runtime"
_AUTHORIZER = {"customJWTAuthorizer": {
    "discoveryUrl": "https://cognito/discovery",
    "allowedClients": ["m2m-client"]}}


def _arn(runtime_id):
    return (f"arn:aws:bedrock-agentcore:us-east-1:111122223333:"
            f"runtime/{runtime_id}")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Exceptions:
    class ConflictException(Exception):
        pass

    class ResourceExistsException(Exception):
        pass


class _NotFoundError(Exception):
    """Class name carries 'NotFound' — classified by driver._is_not_found."""


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


class FakeControl:
    """Scripted AgentCore control-plane stub (hand stub — no moto)."""

    def __init__(self, *, runtime_pages=None, status_scripts=None,
                 default_status="READY", targets=None, sync_errors=None,
                 target_status="READY", create_ids=None, survive=(),
                 target_delete_errors=None, runtime_delete_errors=None,
                 update_response=None):
        self.exceptions = _Exceptions
        self._update_response = dict(update_response or {})
        self._runtime_pages = runtime_pages or [{"agentRuntimes": []}]
        self._status_scripts = {k: list(v) for k, v in
                                (status_scripts or {}).items()}
        self._default_status = default_status
        self._targets = {k: list(v) for k, v in (targets or {}).items()}
        self._sync_errors = list(sync_errors or [])
        self._target_status = target_status
        self._create_ids = list(create_ids or [])
        self._survive = set(survive)
        self._target_delete_errors = dict(target_delete_errors or {})
        self._runtime_delete_errors = dict(runtime_delete_errors or {})
        self._deleted = set()
        self._create_seq = 0
        self.ops = []  # ordered (op, key)
        self.create_runtime_kwargs = []
        self.update_runtime_kwargs = None
        self.create_target_kwargs = None
        self.sync_calls = []

    # -- runtimes --
    def list_agent_runtimes(self, **kwargs):
        idx = int(kwargs.get("nextToken", 0))
        page = dict(self._runtime_pages[idx])
        if idx + 1 < len(self._runtime_pages):
            page["nextToken"] = str(idx + 1)
        return page

    def get_agent_runtime(self, agentRuntimeId):
        if agentRuntimeId in self._deleted:
            raise _NotFoundError(agentRuntimeId)
        script = self._status_scripts.get(agentRuntimeId)
        if script:
            return script.pop(0)
        return {"status": self._default_status}

    def create_agent_runtime(self, **kwargs):
        self.create_runtime_kwargs.append(kwargs)
        self._create_seq += 1
        rid = (self._create_ids.pop(0) if self._create_ids
               else f"rt-{self._create_seq}")
        self.ops.append(("create_runtime", rid))
        return {"agentRuntimeArn": _arn(rid), "agentRuntimeId": rid}

    def update_agent_runtime(self, **kwargs):
        self.update_runtime_kwargs = kwargs
        self.ops.append(("update_runtime", kwargs["agentRuntimeId"]))
        return dict(self._update_response)

    def delete_agent_runtime(self, agentRuntimeId):
        self.ops.append(("delete_runtime", agentRuntimeId))
        err = self._runtime_delete_errors.get(agentRuntimeId)
        if err:
            raise err
        if agentRuntimeId not in self._survive:
            self._deleted.add(agentRuntimeId)
            self._status_scripts.pop(agentRuntimeId, None)
        return {}

    # -- gateway targets --
    def list_gateway_targets(self, gatewayIdentifier, **kwargs):
        return {"items": self._targets.get(gatewayIdentifier, [])}

    def create_gateway_target(self, **kwargs):
        self.create_target_kwargs = kwargs
        self.ops.append(("create_target", kwargs.get("name", "")))
        return {"targetId": "tgt-onb-1"}

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.ops.append(("delete_target", targetId))
        err = self._target_delete_errors.get(targetId)
        if err:
            raise err
        return {}

    def synchronize_gateway_targets(self, gatewayIdentifier, targetIdList):
        self.sync_calls.append(list(targetIdList))
        if self._sync_errors:
            raise self._sync_errors.pop(0)
        return {}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return {"status": self._target_status}


def _existing(name, rid):
    return {"agentRuntimeName": name, "agentRuntimeArn": _arn(rid),
            "agentRuntimeId": rid}


def _details(*, protocol=None, authorizer=None):
    d = {
        "status": "READY",
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": "old-image"}},
        "roleArn": "arn:aws:iam::111122223333:role/old",
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": ["subnet-0aa"], "securityGroups": ["sg-0aa"],
                "requireServiceS3Endpoint": True}},
    }
    if protocol:
        d["protocolConfiguration"] = protocol
    if authorizer:
        d["authorizerConfiguration"] = authorizer
    return d


def _orch_spec(**overrides):
    kwargs = dict(
        name="adt_orchestrator", image_uri=_IMAGE, role_arn=_ROLE,
        subnet_ids=["subnet-0aa", "subnet-0bb"], security_group_id="sg-0aa",
        env=build_env("orchestrator", _SOURCES))
    kwargs.update(overrides)
    return driver.RuntimeSpec(**kwargs)


def _onb_spec(**overrides):
    kwargs = dict(
        name="adt_onboarding", image_uri=_IMAGE, role_arn=_ONB_ROLE,
        subnet_ids=["subnet-0aa", "subnet-0bb"], security_group_id="sg-0bb",
        env=build_env("onboarding", _SOURCES),
        protocol_mcp=True, authorizer=_AUTHORIZER)
    kwargs.update(overrides)
    return driver.RuntimeSpec(**kwargs)


# --------------------------------------------------------------------------- #
# Step-parity goldens — update path
# --------------------------------------------------------------------------- #
def test_orchestrator_update_golden_no_protocol_no_authorizer():
    """§2c update: artifact/role/netcfg/env only — NO protocolConfiguration
    (the orchestrator is legitimately HTTP, ADR-0056), NO authorizer."""
    control = FakeControl(
        runtime_pages=[{"agentRuntimes": [
            _existing("adt_orchestrator", "rt_orch")]}],
        status_scripts={"rt_orch": [_details()]})
    clock = FakeClock()
    result = driver.ensure_runtime(
        _orch_spec(), control=control, region="us-east-1",
        sleep=clock.sleep, monotonic=clock.monotonic)

    kwargs = control.update_runtime_kwargs
    assert set(kwargs) == {"agentRuntimeId", "agentRuntimeArtifact", "roleArn",
                           "networkConfiguration", "environmentVariables"}
    assert kwargs["agentRuntimeId"] == "rt_orch"
    assert kwargs["agentRuntimeArtifact"] == {
        "containerConfiguration": {"containerUri": _IMAGE}}
    assert kwargs["roleArn"] == _ROLE
    # netcfg round-trips from the details with requireServiceS3Endpoint stripped.
    assert kwargs["networkConfiguration"] == {
        "networkMode": "VPC",
        "networkModeConfig": {"subnets": ["subnet-0aa"],
                              "securityGroups": ["sg-0aa"]}}
    assert (json.dumps(kwargs["environmentVariables"], sort_keys=True)
            == json.dumps(build_env("orchestrator", _SOURCES), sort_keys=True))
    assert result == {"arn": _arn("rt_orch"), "runtime_id": "rt_orch",
                      "action": "updated"}


def test_onboarding_update_golden_mcp_override_and_authorizer():
    """§2e update: MCP protocol_override REPAIRS a wiped protocol (details
    carry none) and the fresh M2M authorizer is authoritative."""
    control = FakeControl(
        runtime_pages=[{"agentRuntimes": [
            _existing("adt_onboarding", "rt_onb")]}],
        status_scripts={"rt_onb": [_details()]})
    clock = FakeClock()
    result = driver.ensure_runtime(
        _onb_spec(), control=control, region="us-east-1",
        sleep=clock.sleep, monotonic=clock.monotonic)

    kwargs = control.update_runtime_kwargs
    assert kwargs["protocolConfiguration"] == {"serverProtocol": "MCP"}
    assert kwargs["authorizerConfiguration"] == _AUTHORIZER
    assert kwargs["roleArn"] == _ONB_ROLE
    assert (json.dumps(kwargs["environmentVariables"], sort_keys=True)
            == json.dumps(build_env("onboarding", _SOURCES), sort_keys=True))
    assert result["action"] == "updated"


def test_update_failure_is_fatal_not_warned():
    """Behavior delta 2 (plan): a failed update RAISES — the bash
    warned-and-continued, leaving the runtime silently stale."""
    control = FakeControl(
        runtime_pages=[{"agentRuntimes": [
            _existing("adt_orchestrator", "rt_orch")]}],
        status_scripts={"rt_orch": [_details()]},
        default_status="UPDATING")
    clock = FakeClock()
    with pytest.raises(RuntimeNotReadyError):
        driver.ensure_runtime(
            _orch_spec(), control=control, region="us-east-1",
            sleep=clock.sleep, monotonic=clock.monotonic)


# --------------------------------------------------------------------------- #
# Update READY wait: READY counts only at the version the update returned
# --------------------------------------------------------------------------- #
def _poll(status, version, image=_IMAGE):
    return {"status": status, "agentRuntimeVersion": version,
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}}}


def _update_with_polls(polls, *, sent="3", spec=None):
    control = FakeControl(
        runtime_pages=[{"agentRuntimes": [
            _existing("adt_orchestrator", "rt_orch")]}],
        status_scripts={"rt_orch": [_details(), *polls]},
        update_response={"agentRuntimeVersion": sent} if sent else None)
    clock = FakeClock()
    result = driver.ensure_runtime(
        spec or _orch_spec(), control=control, region="us-east-1",
        sleep=clock.sleep, monotonic=clock.monotonic)
    return result, clock


def test_update_waits_past_a_ready_that_still_describes_the_previous_version(capsys):
    """A read straight after the update can still show the previous version
    READY; that is not this update finishing."""
    result, clock = _update_with_polls([
        _poll("READY", "2"), _poll("UPDATING", "3"), _poll("READY", "3")])
    assert result["action"] == "updated"
    assert len(clock.sleeps) == 2  # polled past the stale READY and UPDATING
    err = capsys.readouterr().err
    assert "update accepted (version 3)" in err
    assert "READY at version 3" in err and "1 poll(s) still showed the previous version" in err


def test_update_failed_after_a_stale_ready_is_fatal():
    with pytest.raises(RuntimeCreateFailedError):
        _update_with_polls([_poll("READY", "2"), _poll("UPDATE_FAILED", "3")])


def test_stale_ready_until_the_deadline_is_fatal_and_says_so():
    with pytest.raises(RuntimeNotReadyError) as exc:
        _update_with_polls([_poll("READY", "2")] * 40)
    assert "READY at version 2, waiting for version 3" in str(exc.value)


def test_a_later_version_counts_only_on_this_deploys_image():
    # Two reads of the later version: the READY poll, then the image check.
    result, _ = _update_with_polls([_poll("READY", "4")] * 2)
    assert result["action"] == "updated"
    with pytest.raises(driver.ProvisionRuntimeError, match="version 4.*other-image"):
        _update_with_polls([_poll("READY", "4", image="other-image")] * 2)


def test_no_version_returned_first_ready_counts():
    """Unchanged behaviour when the update returns no version (or the read
    has none): the first READY counts, as before."""
    result, clock = _update_with_polls([_poll("READY", "2")], sent=None)
    assert result["action"] == "updated" and clock.sleeps == []
    result, clock = _update_with_polls([{"status": "READY"}])
    assert result["action"] == "updated" and clock.sleeps == []

# --------------------------------------------------------------------------- #
# Step-parity goldens — create path
# --------------------------------------------------------------------------- #
def test_orchestrator_create_golden():
    control = FakeControl(create_ids=["rt_new"])
    clock = FakeClock()
    result = driver.ensure_runtime(
        _orch_spec(), control=control, region="us-east-1",
        sleep=clock.sleep, monotonic=clock.monotonic)

    assert len(control.create_runtime_kwargs) == 1
    kwargs = control.create_runtime_kwargs[0]
    assert set(kwargs) == {"agentRuntimeName", "agentRuntimeArtifact",
                           "roleArn", "networkConfiguration",
                           "environmentVariables"}
    assert kwargs["agentRuntimeName"] == "adt_orchestrator"
    assert kwargs["networkConfiguration"] == {
        "networkMode": "VPC",
        "networkModeConfig": {"subnets": ["subnet-0aa", "subnet-0bb"],
                              "securityGroups": ["sg-0aa"]}}
    assert (json.dumps(kwargs["environmentVariables"], sort_keys=True)
            == json.dumps(build_env("orchestrator", _SOURCES), sort_keys=True))
    assert result == {"arn": _arn("rt_new"), "runtime_id": "rt_new",
                      "action": "created"}


def test_onboarding_create_golden_carries_mcp_and_authorizer():
    control = FakeControl(create_ids=["rt_onb_new"])
    clock = FakeClock()
    driver.ensure_runtime(
        _onb_spec(), control=control, region="us-east-1",
        sleep=clock.sleep, monotonic=clock.monotonic)

    kwargs = control.create_runtime_kwargs[0]
    assert kwargs["protocolConfiguration"] == {"serverProtocol": "MCP"}
    assert kwargs["authorizerConfiguration"] == _AUTHORIZER
    assert kwargs["agentRuntimeName"] == "adt_onboarding"


# --------------------------------------------------------------------------- #
# strip_unsupported_subnets + AZ-strip loop
# --------------------------------------------------------------------------- #
def test_strip_parses_multi_subnet_failure_reason():
    reason = ("Runtime creation failed due to unsupported availability zone: "
              "subnet-0aa1, subnet-0bb2 are not supported")
    out = driver.strip_unsupported_subnets(
        ["subnet-0aa1", "subnet-0bb2", "subnet-0cc3"], reason)
    assert out == ["subnet-0cc3"]


def test_strip_returns_none_for_non_az_failure():
    assert driver.strip_unsupported_subnets(
        ["subnet-0aa1"], "image pull failure: access denied") is None
    assert driver.strip_unsupported_subnets(["subnet-0aa1"], "") is None


def _az_fail(subnet):
    return {"status": "CREATE_FAILED",
            "failureReason": f"unsupported availability zone: {subnet}"}


def test_az_strip_retry_succeeds_with_stripped_subnets():
    control = FakeControl(
        create_ids=["rt-a", "rt-b", "rt-c"],
        status_scripts={"rt-a": [_az_fail("subnet-0aa")],
                        "rt-b": [_az_fail("subnet-0bb")]})
    clock = FakeClock()
    spec = _orch_spec(subnet_ids=["subnet-0aa", "subnet-0bb", "subnet-0cc"])
    result = driver.ensure_runtime(
        spec, control=control, region="us-east-1",
        sleep=clock.sleep, monotonic=clock.monotonic)

    subnet_seq = [k["networkConfiguration"]["networkModeConfig"]["subnets"]
                  for k in control.create_runtime_kwargs]
    assert subnet_seq == [["subnet-0aa", "subnet-0bb", "subnet-0cc"],
                          ["subnet-0bb", "subnet-0cc"],
                          ["subnet-0cc"]]
    # Failed runtimes were deleted between attempts.
    deletes = [k for op, k in control.ops if op == "delete_runtime"]
    assert deletes == ["rt-a", "rt-b"]
    assert result["action"] == "created"


def test_az_strip_stops_after_three_attempts_naming_reason():
    control = FakeControl(
        create_ids=["rt-a", "rt-b", "rt-c"],
        status_scripts={"rt-a": [_az_fail("subnet-0aa")],
                        "rt-b": [_az_fail("subnet-0bb")],
                        "rt-c": [_az_fail("subnet-0cc")]})
    clock = FakeClock()
    spec = _orch_spec(subnet_ids=[
        "subnet-0aa", "subnet-0bb", "subnet-0cc", "subnet-0dd"])
    with pytest.raises(driver.ProvisionRuntimeError) as excinfo:
        driver.ensure_runtime(spec, control=control, region="us-east-1",
                              sleep=clock.sleep, monotonic=clock.monotonic)
    assert "3 AZ-strip attempts" in str(excinfo.value)
    assert "subnet-0cc" in str(excinfo.value)  # names the last reason
    assert len(control.create_runtime_kwargs) == 3


def test_az_strip_raises_on_empty_subnet_list():
    control = FakeControl(
        create_ids=["rt-a"],
        status_scripts={"rt-a": [_az_fail("subnet-0aa, subnet-0bb")]})
    clock = FakeClock()
    spec = _orch_spec(subnet_ids=["subnet-0aa", "subnet-0bb"])
    with pytest.raises(driver.ProvisionRuntimeError) as excinfo:
        driver.ensure_runtime(spec, control=control, region="us-east-1",
                              sleep=clock.sleep, monotonic=clock.monotonic)
    assert "no subnets left" in str(excinfo.value)
    assert len(control.create_runtime_kwargs) == 1


def test_non_az_create_failure_is_fatal_no_retry():
    """Behavior delta 2: non-AZ create failures raise immediately."""
    control = FakeControl(
        create_ids=["rt-a"],
        status_scripts={"rt-a": [{"status": "CREATE_FAILED",
                                  "failureReason": "image pull failure"}]})
    clock = FakeClock()
    with pytest.raises(driver.ProvisionRuntimeError) as excinfo:
        driver.ensure_runtime(_orch_spec(), control=control,
                              region="us-east-1",
                              sleep=clock.sleep, monotonic=clock.monotonic)
    assert "non-AZ" in str(excinfo.value)
    assert "image pull failure" in str(excinfo.value)
    assert len(control.create_runtime_kwargs) == 1


# --------------------------------------------------------------------------- #
# Fake-clock caps
# --------------------------------------------------------------------------- #
def test_create_poll_outlasts_a_slow_runtime_create():
    """AgentCore runtime creation took 5 to 9 minutes on a fresh deploy, and
    a 300 s poll stopped deploy.sh at [2c] and [2e]. The command-line poll
    covers 9 minutes with margin."""
    assert driver.CLI_CREATE_POLL_DEADLINE_SECONDS >= 9 * 60 * 1.5
    control = FakeControl(create_ids=["rt-a"],
                          status_scripts={"rt-a": [{"status": "CREATING"}] * 108
                                          + [{"status": "READY"}]})
    clock = FakeClock()
    result = driver.ensure_runtime(
        _orch_spec(), control=control, region="us-east-1",
        sleep=clock.sleep, monotonic=clock.monotonic,
        create_poll_seconds=driver.CLI_CREATE_POLL_DEADLINE_SECONDS)
    assert result["action"] == "created"
    assert clock.now >= 9 * 60


def test_library_create_poll_keeps_its_300_s_budget():
    """The workshop bootstrap calls ensure_runtime without a poll argument;
    its CodeBuild time limit is sized on the 300 s create poll."""
    assert driver.CREATE_POLL_DEADLINE_SECONDS == 300.0


def test_create_poll_caps_at_its_deadline():
    control = FakeControl(create_ids=["rt-a"], default_status="CREATING")
    clock = FakeClock()
    with pytest.raises(RuntimeNotReadyError):
        driver.ensure_runtime(_orch_spec(), control=control,
                              region="us-east-1",
                              sleep=clock.sleep, monotonic=clock.monotonic)
    assert clock.now <= (driver.CREATE_POLL_DEADLINE_SECONDS
                         + driver.CREATE_POLL_INTERVAL_SECONDS)


def test_delete_wait_caps_at_the_deadline_budget():
    """Bounded wait, expressed against the constants: the budget was raised
    from 120 s after both engine runtimes outlived it in a live teardown
    (2026-08-21) — asserting the derived attempt count keeps this test honest
    when the budget is retuned."""
    control = FakeControl(default_status="DELETING")
    clock = FakeClock()
    gone = driver._wait_runtime_gone(control, "rt-x", sleep=clock.sleep)
    attempts = int(
        driver.DELETE_WAIT_DEADLINE_SECONDS / driver.DELETE_WAIT_INTERVAL_SECONDS
    )
    assert gone is False
    assert clock.sleeps == [driver.DELETE_WAIT_INTERVAL_SECONDS] * attempts
    assert clock.now == driver.DELETE_WAIT_DEADLINE_SECONDS


def test_sync_exhaustion_raises_gateway_sync_error():
    control = FakeControl(
        sync_errors=[Exception("SYNCHRONIZE_UNSUCCESSFUL")] * 10)
    clock = FakeClock()
    with pytest.raises(GatewaySyncError):
        driver.ensure_onboarding_target(
            control=control, gateway_id="GW-ONB",
            runtime_arn=_arn("rt_onb"), provider_arn="arn:provider",
            region="us-east-1", warmup_seconds=0, sleep=clock.sleep)
    assert len(control.sync_calls) == 10


# --------------------------------------------------------------------------- #
# Onboarding gateway target (§2e parity)
# --------------------------------------------------------------------------- #
def test_onboarding_target_golden():
    control = FakeControl()
    clock = FakeClock()
    target_id = driver.ensure_onboarding_target(
        control=control, gateway_id="GW-ONB", runtime_arn=_arn("rt_onb"),
        provider_arn="arn:provider", region="us-east-1", sleep=clock.sleep)

    kwargs = control.create_target_kwargs
    assert kwargs["gatewayIdentifier"] == "GW-ONB"
    assert kwargs["name"] == "onboarding"
    assert kwargs["description"] == (
        "Onboarding MCP tools (single target; JWT auth, Cedar not attached)")
    encoded = _arn("rt_onb").replace(":", "%3A").replace("/", "%2F")
    assert kwargs["targetConfiguration"] == {"mcp": {"mcpServer": {
        "endpoint": (f"https://bedrock-agentcore.us-east-1.amazonaws.com/"
                     f"runtimes/{encoded}/invocations?qualifier=DEFAULT")}}}
    assert kwargs["credentialProviderConfigurations"] == [{
        "credentialProviderType": "OAUTH",
        "credentialProvider": {"oauthCredentialProvider": {
            "providerArn": "arn:provider",
            "scopes": ["mcp/tools.read", "mcp/tools.execute"],
            "grantType": "CLIENT_CREDENTIALS"}}}]
    # 30 s warmup precedes the sync (agent_manager parity).
    assert clock.sleeps[0] == 30
    assert control.sync_calls == [["tgt-onb-1"]]
    assert target_id == "tgt-onb-1"


def test_create_target_description_default_preserved():
    """Additive field: absent description keeps the historical per-system
    text (existing provisioner callers are unchanged)."""
    control = FakeControl()
    create_target(
        CreateTargetInput(system_id="erp", system_name="ERP",
                          gateway_id="GW1", runtime_mcp_url="https://x"),
        control=control)
    assert control.create_target_kwargs["description"] == "System agent for ERP"


# --------------------------------------------------------------------------- #
# sweep_adt_runtimes (DELETE path)
# --------------------------------------------------------------------------- #
def _sweep_pages():
    return [
        {"agentRuntimes": [_existing("adt_orchestrator", "rt_orch"),
                           _existing("someone_elses_runtime", "rt_foreign")]},
        {"agentRuntimes": [_existing("adt_erp_sim", "rt_erp")]},
    ]


def test_sweep_deletes_targets_before_runtimes_prefix_scoped():
    control = FakeControl(
        runtime_pages=_sweep_pages(),
        targets={"GW-Q": [{"targetId": "t1"}, {"targetId": "t2"}],
                 "GW-ONB": [{"targetId": "t3"}]})
    clock = FakeClock()
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1",
        gateway_ids=["GW-Q", "GW-ONB"], sleep=clock.sleep)

    assert sorted(deleted) == ["adt_erp_sim", "adt_orchestrator"]
    runtime_deletes = [k for op, k in control.ops if op == "delete_runtime"]
    assert "rt_foreign" not in runtime_deletes  # NEVER touches non-adt_
    target_idx = [i for i, (op, _) in enumerate(control.ops)
                  if op == "delete_target"]
    runtime_idx = [i for i, (op, _) in enumerate(control.ops)
                   if op == "delete_runtime"]
    assert target_idx and runtime_idx
    assert max(target_idx) < min(runtime_idx)  # targets first
    assert [k for op, k in control.ops if op == "delete_target"] == [
        "t1", "t2", "t3"]


def test_sweep_tolerates_not_found_mid_sweep():
    control = FakeControl(
        runtime_pages=_sweep_pages(),
        targets={"GW-ONB": [{"targetId": "t3"}]},
        target_delete_errors={"t3": _NotFoundError("t3")},
        runtime_delete_errors={"rt_orch": _NotFoundError("rt_orch")})
    # An already-gone runtime: gets raise NotFound immediately.
    control._deleted.add("rt_orch")
    clock = FakeClock()
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=["GW-ONB"],
        sleep=clock.sleep)
    assert sorted(deleted) == ["adt_erp_sim", "adt_orchestrator"]


def test_sweep_raises_listing_survivors():
    control = FakeControl(
        runtime_pages=_sweep_pages(),
        survive={"rt_erp"}, default_status="DELETING")
    clock = FakeClock()
    with pytest.raises(driver.ProvisionRuntimeError) as excinfo:
        driver.sweep_adt_runtimes(
            control=control, region="us-east-1", gateway_ids=[],
            sleep=clock.sleep)
    assert "adt_erp_sim" in str(excinfo.value)


def test_sweep_tolerates_survivors_when_asked():
    """The CloudFormation CR passes tolerate_survivors=True: a runtime that
    outlives its delete wait must NOT fail the sweep, because raising posts
    FAILED and wedges the stack in DELETE_FAILED (live: 2026-08-21) while the
    accepted deletes are still progressing. The tolerated survivor is simply
    absent from the returned 'deleted' list."""
    control = FakeControl(
        runtime_pages=_sweep_pages(),
        survive={"rt_erp"}, default_status="DELETING")
    clock = FakeClock()
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=[],
        sleep=clock.sleep, tolerate_survivors=True)
    assert "adt_erp_sim" not in deleted
    assert "adt_orchestrator" in deleted


def test_sweep_non_not_found_delete_error_propagates():
    control = FakeControl(
        runtime_pages=[{"agentRuntimes": [_existing("adt_x", "rt_x")]}],
        runtime_delete_errors={"rt_x": RuntimeError("throttled")})
    with pytest.raises(RuntimeError, match="throttled"):
        driver.sweep_adt_runtimes(control=control, region="us-east-1",
                                  gateway_ids=[], sleep=FakeClock().sleep)


class _SlowDeleteControl(FakeControl):
    """A runtime is gone ``delete_seconds`` after its own delete call, the way
    AgentCore behaves (about 5 minutes each on a workshop account)."""

    def __init__(self, clock, *, delete_seconds, **kwargs):
        super().__init__(default_status="DELETING", **kwargs)
        self._clock = clock
        self._delete_seconds = delete_seconds
        self._deleted_at = {}

    def delete_agent_runtime(self, agentRuntimeId):
        self.ops.append(("delete_runtime", agentRuntimeId))
        self._deleted_at.setdefault(agentRuntimeId, self._clock.now)
        return {}

    def get_agent_runtime(self, agentRuntimeId):
        self.ops.append(("get_runtime", agentRuntimeId))
        started = self._deleted_at.get(agentRuntimeId)
        if started is not None and (
                self._clock.now - started >= self._delete_seconds):
            raise _NotFoundError(agentRuntimeId)
        return {"status": "DELETING" if started is not None else "READY"}


def test_sweep_waits_for_every_runtime_together():
    """A workshop account ends an event with a dozen or more adt_* runtimes,
    and each takes about 5 minutes to delete. Deleting them one at a time ran
    past the bootstrap build's 55-minute timeout, so the custom resource never
    answered CloudFormation and the stack went DELETE_FAILED (a rehearsal
    account, 2026-10-11: 12 of 13 deleted at 5-minute spacing, then the
    timeout). Every
    delete is issued first and they are waited out together."""
    clock = FakeClock()
    names = [f"adt_sys{i:02d}" for i in range(14)]
    control = _SlowDeleteControl(
        clock, delete_seconds=300,
        runtime_pages=[{"agentRuntimes": [
            _existing(n, f"rt_{n}") for n in names]}])
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=[],
        sleep=clock.sleep, tolerate_survivors=True)

    assert sorted(deleted) == names
    first_get = next(i for i, (op, _) in enumerate(control.ops)
                     if op != "delete_runtime")
    assert first_get >= len(names)  # all deletes before any wait
    assert clock.now <= driver.DELETE_WAIT_DEADLINE_SECONDS


def test_sweep_survivors_share_one_wait_budget():
    """Runtimes that never finish deleting are reported once the shared wait
    budget is spent, not after one budget per runtime."""
    clock = FakeClock()
    control = _SlowDeleteControl(
        clock, delete_seconds=10_000,
        runtime_pages=[{"agentRuntimes": [
            _existing("adt_a", "rt_a"), _existing("adt_b", "rt_b"),
            _existing("adt_c", "rt_c")]}])
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=[],
        sleep=clock.sleep, tolerate_survivors=True)
    assert deleted == []
    assert clock.now <= driver.DELETE_WAIT_DEADLINE_SECONDS + (
        driver.DELETE_WAIT_INTERVAL_SECONDS)


class _ThrottlingError(Exception):
    def __init__(self, op):
        super().__init__(f"{op}: Rate exceeded")
        self.response = {"Error": {"Code": "ThrottlingException"}}


class _LatencyControl(_SlowDeleteControl):
    """Each status read costs ``call_seconds`` of wall time, and the first
    ``throttle_*`` calls of each kind are throttled past the SDK's retries."""

    def __init__(self, clock, *, call_seconds=0.0, delete_call_seconds=0.0,
                 throttle_deletes=0, throttle_gets=0, throttle_targets=0,
                 throttle_runtime_list=0, throttle_target_list=0, **kwargs):
        super().__init__(clock, **kwargs)
        self._call_seconds = call_seconds
        self._delete_call_seconds = delete_call_seconds
        self._throttle = {"delete": throttle_deletes, "get": throttle_gets,
                          "target": throttle_targets,
                          "runtime_list": throttle_runtime_list,
                          "target_list": throttle_target_list}
        self._http5xx_deletes = 0

    def _maybe_throttle(self, kind):
        if self._throttle[kind] > 0:
            self._throttle[kind] -= 1
            raise _ThrottlingError(kind)

    def list_agent_runtimes(self, **kwargs):
        self._maybe_throttle("runtime_list")
        return super().list_agent_runtimes(**kwargs)

    def delete_agent_runtime(self, agentRuntimeId):
        self._clock.now += self._delete_call_seconds
        if self._http5xx_deletes > 0:
            self._http5xx_deletes -= 1
            raise _Http5xxError("SomeUnknownCode", 500)
        self._maybe_throttle("delete")
        return super().delete_agent_runtime(agentRuntimeId)

    def get_agent_runtime(self, agentRuntimeId):
        self._clock.now += self._call_seconds
        self._maybe_throttle("get")
        return super().get_agent_runtime(agentRuntimeId)

    def list_gateway_targets(self, gatewayIdentifier, **kwargs):
        self._maybe_throttle("target_list")
        return super().list_gateway_targets(gatewayIdentifier, **kwargs)

    def delete_gateway_target(self, **kwargs):
        self._maybe_throttle("target")
        return super().delete_gateway_target(**kwargs)


def test_sweep_wait_budget_counts_the_time_calls_take():
    """The wait ends on elapsed time, not a count of polling rounds, so slow
    status reads can't stretch it past the budget."""
    clock = FakeClock()
    control = _LatencyControl(
        clock, call_seconds=2.0, delete_seconds=10_000,
        runtime_pages=[{"agentRuntimes": [
            _existing(f"adt_s{i}", f"rt_s{i}") for i in range(14)]}])
    driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=[],
        sleep=clock.sleep, monotonic=clock.monotonic, tolerate_survivors=True)
    assert clock.now <= driver.DELETE_WAIT_DEADLINE_SECONDS + 14 * 2.0 + (
        driver.DELETE_WAIT_INTERVAL_SECONDS)


def test_sweep_retries_throttled_calls_inside_the_budget():
    """A throttle that outlasts the SDK's own retries on a target delete, a
    runtime delete or a status read is retried until the budget runs out
    instead of failing the teardown."""
    clock = FakeClock()
    control = _LatencyControl(
        clock, delete_seconds=60, throttle_deletes=2, throttle_gets=3,
        throttle_targets=1,
        targets={"GW-Q": [{"targetId": "t1"}]},
        runtime_pages=[{"agentRuntimes": [
            _existing("adt_a", "rt_a"), _existing("adt_b", "rt_b")]}])
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=["GW-Q"],
        sleep=clock.sleep, monotonic=clock.monotonic, tolerate_survivors=True)
    assert sorted(deleted) == ["adt_a", "adt_b"]
    assert ("delete_target", "t1") in control.ops


def test_sweep_strict_mode_raises_on_a_throttle_that_never_clears():
    clock = FakeClock()
    control = _LatencyControl(
        clock, delete_seconds=60, throttle_deletes=10_000,
        runtime_pages=[{"agentRuntimes": [_existing("adt_a", "rt_a")]}])
    with pytest.raises(driver.ProvisionRuntimeError, match="adt_a"):
        driver.sweep_adt_runtimes(
            control=control, region="us-east-1", gateway_ids=[],
            sleep=clock.sleep, monotonic=clock.monotonic)


def test_sweep_tolerate_mode_raises_when_a_runtime_delete_is_never_accepted():
    """A runtime whose delete AgentCore never accepted (every delete call
    throttled past the budget) is NOT a tolerated survivor even in
    tolerate_survivors mode: returning normally would drop the CR from
    CloudFormation's cleanup-retry path while the runtime (and its ENIs) can
    still block subnet teardown. The sweep must raise."""
    clock = FakeClock()
    control = _LatencyControl(
        clock, delete_seconds=60, throttle_deletes=10_000,
        runtime_pages=[{"agentRuntimes": [_existing("adt_a", "rt_a")]}])
    with pytest.raises(driver.ProvisionRuntimeError, match="adt_a"):
        driver.sweep_adt_runtimes(
            control=control, region="us-east-1", gateway_ids=[],
            sleep=clock.sleep, monotonic=clock.monotonic,
            tolerate_survivors=True)


def test_sweep_tolerate_mode_raises_when_a_target_delete_is_never_accepted():
    """Same contract for gateway targets: a target whose delete never got
    accepted (persistent throttle) blocks gateway deletion, so the sweep
    raises even in tolerate_survivors mode."""
    clock = FakeClock()
    control = _LatencyControl(
        clock, delete_seconds=60, throttle_targets=10_000,
        targets={"GW-Q": [{"targetId": "t1"}]},
        runtime_pages=[{"agentRuntimes": []}])
    with pytest.raises(driver.ProvisionRuntimeError, match="t1"):
        driver.sweep_adt_runtimes(
            control=control, region="us-east-1", gateway_ids=["GW-Q"],
            sleep=clock.sleep, monotonic=clock.monotonic,
            tolerate_survivors=True)


def test_sweep_tolerates_an_accepted_delete_still_deleting():
    """The counter-case that must keep: a delete AgentCore ACCEPTED whose
    runtime is still DELETING at the budget end is a legitimate tolerated
    survivor — it is progressing and CloudFormation has nested-stack teardown
    ahead of the subnets. tolerate_survivors returns it absent, not raised."""
    clock = FakeClock()
    control = _SlowDeleteControl(
        clock, delete_seconds=10_000,
        runtime_pages=[{"agentRuntimes": [_existing("adt_a", "rt_a")]}])
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=[],
        sleep=clock.sleep, monotonic=clock.monotonic,
        tolerate_survivors=True)
    assert deleted == []
    # The delete WAS issued (accepted); it just never finished.
    assert ("delete_runtime", "rt_a") in control.ops


def test_sweep_stops_starting_calls_once_the_deadline_is_reached():
    """The monotonic deadline is checked BEFORE each call, so once
    it is reached no further call starts. Overshoot is bounded by at most one
    configured call, not one call per runtime: 14 runtimes whose delete each
    costs 120 s against a 600 s budget must not run all 14 (1,680 s)."""
    clock = FakeClock()
    names = [f"adt_s{i:02d}" for i in range(14)]
    control = _LatencyControl(
        clock, delete_call_seconds=120.0, delete_seconds=10_000,
        runtime_pages=[{"agentRuntimes": [
            _existing(n, f"rt_{n}") for n in names]}])
    with pytest.raises(driver.ProvisionRuntimeError):
        driver.sweep_adt_runtimes(
            control=control, region="us-east-1", gateway_ids=[],
            sleep=clock.sleep, monotonic=clock.monotonic,
            tolerate_survivors=True)
    # One call of overshoot past the budget, never one per runtime.
    assert clock.now <= driver.DELETE_WAIT_DEADLINE_SECONDS + 120.0


def test_sweep_retries_a_one_time_runtime_list_throttle():
    """list_all_agent_runtimes is wrapped in the retry-within-budget,
    so a one-time listing throttle no longer escapes immediately."""
    clock = FakeClock()
    control = _LatencyControl(
        clock, delete_seconds=60, throttle_runtime_list=1,
        runtime_pages=[{"agentRuntimes": [_existing("adt_a", "rt_a")]}])
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=[],
        sleep=clock.sleep, monotonic=clock.monotonic,
        tolerate_survivors=True)
    assert deleted == ["adt_a"]


def test_sweep_retries_a_one_time_target_list_throttle():
    """The gateway-target listing is wrapped in the same
    retry-within-budget."""
    clock = FakeClock()
    control = _LatencyControl(
        clock, delete_seconds=60, throttle_target_list=1,
        targets={"GW-Q": [{"targetId": "t1"}]},
        runtime_pages=[{"agentRuntimes": []}])
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=["GW-Q"],
        sleep=clock.sleep, monotonic=clock.monotonic,
        tolerate_survivors=True)
    assert deleted == []
    assert ("delete_target", "t1") in control.ops


class _PagedListControl(_LatencyControl):
    """Gateway targets and runtimes served over several pages, each page
    call costing ``page_seconds`` of wall time. ``repeat_token`` makes every
    page after the first hand back the same nextToken (a service that never
    finishes paginating)."""

    def __init__(self, clock, *, page_seconds, target_pages=None,
                 repeat_token=False, **kwargs):
        super().__init__(clock, **kwargs)
        self._page_seconds = page_seconds
        self._target_pages = target_pages or {}
        self._repeat_token = repeat_token
        self.page_calls = []  # (kind, start time)

    def _page(self, kind, pages, token):
        self.page_calls.append((kind, self._clock.now))
        self._clock.now += self._page_seconds
        idx = int(token or 0)
        if self._repeat_token and idx >= 1:
            return {**pages[min(idx, len(pages) - 1)], "nextToken": "1"}
        page = dict(pages[idx])
        if idx + 1 < len(pages) or (self._repeat_token and idx == 0):
            page["nextToken"] = str(idx + 1)
        return page

    def list_gateway_targets(self, gatewayIdentifier, **kwargs):
        self._maybe_throttle("target_list")
        return self._page("target", self._target_pages[gatewayIdentifier],
                          kwargs.get("nextToken"))

    def list_agent_runtimes(self, **kwargs):
        self._maybe_throttle("runtime_list")
        return self._page("runtime", self._runtime_pages, kwargs.get("nextToken"))


def _three_pages(prefix, key, make):
    return [{key: [make(f"{prefix}{p}{i}") for i in range(3)]} for p in range(3)]


def test_sweep_starts_no_target_page_call_after_the_deadline():
    """The deadline is checked before EACH page of a paginated listing, not
    once around the whole listing: three 400 s target pages against a 600 s
    budget must not start the third page at 800 s."""
    clock = FakeClock()
    control = _PagedListControl(
        clock, page_seconds=400.0, delete_seconds=60,
        target_pages={"GW-P": _three_pages("t", "items", lambda n: {"targetId": n})},
        runtime_pages=[{"agentRuntimes": []}])
    with pytest.raises(driver.ProvisionRuntimeError):
        driver.sweep_adt_runtimes(
            control=control, region="us-east-1", gateway_ids=["GW-P"],
            sleep=clock.sleep, monotonic=clock.monotonic,
            tolerate_survivors=True)
    starts = [t for kind, t in control.page_calls if kind == "target"]
    assert starts and all(t < driver.DELETE_WAIT_DEADLINE_SECONDS for t in starts)
    assert not [op for op in control.ops if op[0] == "delete_target"]


def test_sweep_starts_no_runtime_page_call_after_the_deadline():
    clock = FakeClock()
    control = _PagedListControl(
        clock, page_seconds=400.0, delete_seconds=60,
        runtime_pages=_three_pages(
            "adt_r", "agentRuntimes", lambda n: _existing(n, f"rt_{n}")))
    with pytest.raises(driver.ProvisionRuntimeError, match="listing"):
        driver.sweep_adt_runtimes(
            control=control, region="us-east-1", gateway_ids=[],
            sleep=clock.sleep, monotonic=clock.monotonic,
            tolerate_survivors=True)
    starts = [t for kind, t in control.page_calls if kind == "runtime"]
    assert starts and all(t < driver.DELETE_WAIT_DEADLINE_SECONDS for t in starts)
    assert not [op for op in control.ops if op[0] == "delete_runtime"]


def test_sweep_reads_every_page_inside_the_budget():
    """Fast pages: every page is read and every listed resource is deleted,
    so the per-page check doesn't drop a page that fits the budget."""
    clock = FakeClock()
    control = _PagedListControl(
        clock, page_seconds=1.0, delete_seconds=60,
        target_pages={"GW-P": _three_pages("t", "items", lambda n: {"targetId": n})},
        runtime_pages=_three_pages(
            "adt_r", "agentRuntimes", lambda n: _existing(n, f"rt_{n}")))
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=["GW-P"],
        sleep=clock.sleep, monotonic=clock.monotonic, tolerate_survivors=True)
    assert len([op for op in control.ops if op[0] == "delete_target"]) == 9
    assert len(deleted) == 9


@pytest.mark.parametrize("kind", ["target", "runtime"])
def test_sweep_fails_loud_on_a_repeated_pagination_token(kind):
    """A listing whose nextToken repeats would page forever; it fails
    loudly (and fast) instead of spending the budget re-reading one page."""
    clock = FakeClock()
    kwargs = {"target_pages": {"GW-P": _three_pages(
        "t", "items", lambda n: {"targetId": n})},
        "runtime_pages": [{"agentRuntimes": []}]} if kind == "target" else {
        "runtime_pages": _three_pages(
            "adt_r", "agentRuntimes", lambda n: _existing(n, f"rt_{n}"))}
    control = _PagedListControl(clock, page_seconds=1.0, delete_seconds=60,
                                repeat_token=True, **kwargs)
    with pytest.raises(driver.ProvisionRuntimeError, match="repeated"):
        driver.sweep_adt_runtimes(
            control=control, region="us-east-1",
            gateway_ids=["GW-P"] if kind == "target" else [],
            sleep=clock.sleep, monotonic=clock.monotonic,
            tolerate_survivors=True)
    assert len(control.page_calls) <= 4
    assert clock.now < driver.DELETE_WAIT_DEADLINE_SECONDS


class _Http5xxError(Exception):
    """A ClientError-shaped error whose Error.Code is arbitrary but whose
    ResponseMetadata carries an HTTP 5xx/429 status."""

    def __init__(self, code, http_status):
        super().__init__(f"{code}: HTTP {http_status}")
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": http_status},
        }


def test_is_retryable_honors_http_5xx_status_whatever_the_code():
    """_is_retryable retries any ClientError whose
    ResponseMetadata.HTTPStatusCode is 5xx or 429, regardless of its code
    string."""
    assert driver._is_retryable(_Http5xxError("SomeUnknownCode", 500))
    assert driver._is_retryable(_Http5xxError("WeirdCode", 503))
    assert driver._is_retryable(_Http5xxError("Overloaded", 429))
    # A 4xx with an unknown code is NOT retryable.
    assert not driver._is_retryable(_Http5xxError("ValidationException", 400))


def test_sweep_retries_an_arbitrary_code_with_http_500():
    """End-to-end: a delete raising an arbitrary-code ClientError with
    HTTP 500 is retried inside the budget, not propagated."""
    clock = FakeClock()
    control = _LatencyControl(
        clock, delete_seconds=60,
        runtime_pages=[{"agentRuntimes": [_existing("adt_a", "rt_a")]}])
    control._http5xx_deletes = 2  # first two deletes raise HTTP-500
    deleted = driver.sweep_adt_runtimes(
        control=control, region="us-east-1", gateway_ids=[],
        sleep=clock.sleep, monotonic=clock.monotonic,
        tolerate_survivors=True)
    assert deleted == ["adt_a"]


# --------------------------------------------------------------------------- #
# Import hygiene + no-os.environ-mutation
# --------------------------------------------------------------------------- #
_ALLOWED_TOP_LEVEL = {
    # boto3/botocore + their dependency footprint (certifi TLS bundle, CRT).
    "boto3", "botocore", "dateutil", "jmespath", "s3transfer", "six",
    "urllib3", "awscrt", "_awscrt", "certifi",
    # Interpreter bookkeeping: the subprocess registers a __main__ alias.
    "__mp_main__",
    # boto3 transitively pulls it in some venvs (environment-dependent).
    "backports",
}
_ALLOWED_AGENTS = {
    "agents", "agents.lease_guard", "agents.runtime_env_contract",
    "agents.runtime_provision_helpers", "agents.onboarding_session",
    "agents.connection_types", "agents.response_integrity_security",
}


def test_import_closure_is_stdlib_boto3_and_agents_leaves():
    """Importing the driver in a FRESH interpreter pulls nothing outside
    {stdlib, boto3/botocore(+deps), the allowlisted agents leaf modules} —
    the CR-vendorability gate (never agents.config / strands). boto3's own
    dependency closure is measured first and excluded — see the child code."""
    # boto3's OWN transitive closure is measured in the child and subtracted:
    # what botocore drags in depends on the installed urllib3/pyOpenSSL
    # (urllib3 2.7 imports urllib3.contrib.pyopenssl -> OpenSSL -> cryptography)
    # and is not the driver's doing. The gate is the DRIVER-attributable delta.
    code = (
        "import json, sys\n"
        "import boto3, botocore.session\n"
        "baseline = set(sys.modules)\n"
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location("
        f"'provision_runtimes_hygiene', {str(_DRIVER_PATH)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "sys.modules['provision_runtimes_hygiene'] = mod\n"
        "spec.loader.exec_module(mod)\n"
        "print(json.dumps(sorted(set(sys.modules) - baseline)))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_ADT_ROOT)}
    proc = subprocess.run(  # nosec B603 - fixed argv, our own interpreter
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, cwd=str(_ADT_ROOT), timeout=120)
    assert proc.returncode == 0, proc.stderr
    added = json.loads(proc.stdout)

    def allowed(name):
        top = name.split(".")[0]
        if top in sys.stdlib_module_names or top in _ALLOWED_TOP_LEVEL:
            return True
        if name in _ALLOWED_AGENTS or name.startswith("agents.provisioner"):
            return True
        return name == "provision_runtimes_hygiene"

    disallowed = sorted(m for m in added if not allowed(m))
    assert not disallowed, f"driver import pulled in: {disallowed}"


def test_os_environ_never_mutated_by_api_calls():
    snapshot = dict(os.environ)
    # ensure (update), ensure (create + AZ strip), target, sweep, planner.
    clock = FakeClock()
    driver.ensure_runtime(
        _orch_spec(),
        control=FakeControl(runtime_pages=[{"agentRuntimes": [
            _existing("adt_orchestrator", "rt_orch")]}],
            status_scripts={"rt_orch": [_details()]}),
        region="us-east-1", sleep=clock.sleep, monotonic=clock.monotonic)
    driver.ensure_runtime(
        _onb_spec(), control=FakeControl(create_ids=["rt-a"]),
        region="us-east-1", sleep=clock.sleep, monotonic=clock.monotonic)
    driver.ensure_onboarding_target(
        control=FakeControl(), gateway_id="GW-ONB",
        runtime_arn=_arn("rt_onb"), provider_arn="arn:p",
        region="us-east-1", warmup_seconds=0, sleep=clock.sleep)
    driver.sweep_adt_runtimes(control=FakeControl(), region="us-east-1",
                              gateway_ids=[], sleep=clock.sleep)
    driver.strip_unsupported_subnets(["subnet-0aa"], "availability zone x")
    assert dict(os.environ) == snapshot


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #
_ORCH_ARGV = ["--role", "orchestrator", "--region", "us-east-1",
              "--image", _IMAGE, "--role-arn", _ROLE,
              "--subnets", "subnet-0aa,subnet-0bb", "--sg", "sg-0aa"]
_ONB_ARGV = ["--role", "onboarding", "--region", "us-east-1",
             "--image", _IMAGE, "--role-arn", _ONB_ROLE,
             "--subnets", "subnet-0aa", "--sg", "sg-0bb",
             "--gateway-id", "GW-ONB",
             "--m2m-discovery-url", "https://cognito/discovery",
             "--m2m-client-id", "m2m-client",
             "--provider-arn", "arn:provider"]


@pytest.fixture()
def _no_boto(monkeypatch):
    monkeypatch.setattr(
        driver, "boto3",
        types.SimpleNamespace(client=lambda *a, **k: object()))


def test_cli_waits_the_longer_create_poll(monkeypatch, capsys, _no_boto):
    """deploy.sh [2c]/[2e] run this command line, so it passes the 900 s poll."""
    seen = {}

    def fake_ensure(spec, *, control, region, **kw):
        seen.update(kw)
        return {"arn": _arn("rt_orch"), "runtime_id": "rt_orch",
                "action": "created"}

    monkeypatch.setattr(driver, "ensure_runtime", fake_ensure)
    assert driver.main(_ORCH_ARGV) == 0
    capsys.readouterr()
    assert seen["create_poll_seconds"] == driver.CLI_CREATE_POLL_DEADLINE_SECONDS


def test_cli_orchestrator_emits_single_json_object(monkeypatch, capsys,
                                                   _no_boto):
    seen = {}

    def fake_ensure(spec, *, control, region, **kw):
        seen["spec"] = spec
        seen["region"] = region
        return {"arn": _arn("rt_orch"), "runtime_id": "rt_orch",
                "action": "updated"}

    monkeypatch.setattr(driver, "ensure_runtime", fake_ensure)
    rc = driver.main(_ORCH_ARGV)
    out, err = capsys.readouterr()

    assert rc == 0
    assert json.loads(out) == {"orchestrator": {
        "arn": _arn("rt_orch"), "runtime_id": "rt_orch",
        "action": "updated"}}
    spec = seen["spec"]
    assert spec.name == "adt_orchestrator"
    assert spec.subnet_ids == ["subnet-0aa", "subnet-0bb"]
    assert spec.protocol_mcp is False and spec.authorizer is None
    # Env-contract inputs are ambient — byte-equal to the shim's build.
    expected = build_env("orchestrator",
                         RuntimeEnvSources.from_ambient(region="us-east-1"))
    assert spec.env == expected


def test_cli_onboarding_emits_runtime_and_target(monkeypatch, capsys,
                                                 _no_boto):
    monkeypatch.setattr(
        driver, "ensure_runtime",
        lambda spec, **kw: {"arn": _arn("rt_onb"), "runtime_id": "rt_onb",
                            "action": "created"})
    seen = {}

    def fake_target(**kwargs):
        seen.update(kwargs)
        return "tgt-onb-1"

    monkeypatch.setattr(driver, "ensure_onboarding_target", fake_target)
    rc = driver.main(_ONB_ARGV)
    out, _ = capsys.readouterr()

    assert rc == 0
    assert json.loads(out) == {
        "onboarding": {"arn": _arn("rt_onb"), "runtime_id": "rt_onb",
                       "action": "created"},
        "target_id": "tgt-onb-1"}
    assert seen["runtime_arn"] == _arn("rt_onb")
    assert seen["gateway_id"] == "GW-ONB"
    assert seen["provider_arn"] == "arn:provider"


def test_cli_onboarding_spec_carries_mcp_and_authorizer(monkeypatch, capsys,
                                                        _no_boto):
    seen = {}

    def fake_ensure(spec, **kw):
        seen["spec"] = spec
        return {"arn": _arn("rt_onb"), "runtime_id": "rt_onb",
                "action": "updated"}

    monkeypatch.setattr(driver, "ensure_runtime", fake_ensure)
    monkeypatch.setattr(driver, "ensure_onboarding_target",
                        lambda **kw: "tgt-onb-1")
    assert driver.main(_ONB_ARGV) == 0
    capsys.readouterr()
    spec = seen["spec"]
    assert spec.protocol_mcp is True
    assert spec.authorizer == _AUTHORIZER
    assert spec.env == build_env(
        "onboarding", RuntimeEnvSources.from_ambient(region="us-east-1"))


def test_cli_failure_is_nonzero_with_empty_stdout(monkeypatch, capsys,
                                                  _no_boto):
    def boom(spec, **kw):
        raise RuntimeError("injected create failure")

    monkeypatch.setattr(driver, "ensure_runtime", boom)
    rc = driver.main(_ORCH_ARGV)
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""  # NOTHING on stdout on failure
    assert "injected create failure" in err


def test_cli_missing_onboarding_flags_exit_2(capsys, _no_boto):
    argv = ["--role", "onboarding", "--region", "us-east-1",
            "--image", _IMAGE, "--role-arn", _ONB_ROLE,
            "--subnets", "subnet-0aa", "--sg", "sg-0bb"]
    rc = driver.main(argv)
    out, err = capsys.readouterr()
    assert rc == 2
    assert out == ""
    assert "--gateway-id" in err


def test_cli_main_never_mutates_environ(monkeypatch, capsys, _no_boto):
    snapshot = dict(os.environ)
    monkeypatch.setattr(
        driver, "ensure_runtime",
        lambda spec, **kw: {"arn": _arn("rt_orch"), "runtime_id": "rt_orch",
                            "action": "updated"})
    assert driver.main(_ORCH_ARGV) == 0
    capsys.readouterr()
    assert dict(os.environ) == snapshot


# --------------------------------------------------------------------------- #
# Settle-wait between synchronize and verify (live-observed 2026-08-05)
# --------------------------------------------------------------------------- #

class _SettleControl:
    """get_gateway_target returns queued statuses, then READY forever."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.calls = 0

    def get_gateway_target(self, **kwargs):
        self.calls += 1
        status = self._statuses.pop(0) if self._statuses else "READY"
        return {"status": status}


def test_resync_retries_while_target_reports_sync_unsuccessful(monkeypatch):
    """A gateway sync fetches the runtime's MCP tool list, and a cold runtime
    reports READY before its endpoint serves — one 30 s warmup + a single
    synchronize landed SYNCHRONIZE_UNSUCCESSFUL and aborted a whole workshop
    deploy (live: 2026-08-21). The driver must re-synchronize with backoff
    and return once the target reaches READY."""
    control = _SettleControl([
        "SYNCHRONIZE_UNSUCCESSFUL",   # settle read, attempt 1
        "SYNCHRONIZE_UNSUCCESSFUL",   # status check, attempt 1
        "READY",                      # settle read, attempt 2
    ])
    syncs = []
    monkeypatch.setattr(
        driver, "synchronize",
        lambda payload, control=None, sleep=None: syncs.append(payload),
    )
    slept = []
    driver._synchronize_until_ready(
        control, gateway_id="gw", target_id="t", region="us-east-1",
        sleep=slept.append,
    )
    assert len(syncs) == 2, "must re-synchronize, not just re-poll"
    assert driver.SYNC_RETRY_WAITS[0] in slept


def test_resync_is_bounded_and_falls_through_to_verify(monkeypatch):
    """Never-succeeding sync exhausts the bounded retry budget and RETURNS,
    so verify() reports the honest failure — never an infinite loop."""
    control = _SettleControl(["SYNCHRONIZE_UNSUCCESSFUL"] * 999)
    syncs = []
    monkeypatch.setattr(
        driver, "synchronize",
        lambda payload, control=None, sleep=None: syncs.append(payload),
    )
    driver._synchronize_until_ready(
        control, gateway_id="gw", target_id="t", region="us-east-1",
        sleep=lambda _s: None,
    )
    assert len(syncs) == len(driver.SYNC_RETRY_WAITS) + 1


def test_created_wait_polls_through_creating_then_returns():
    """Live failure (Workshop Studio test event, 2026-09-12): the onboarding
    target sat in CREATING for >75 s after create_target; synchronize's own
    10 x 5 s Creating-state retry expired and the whole root went
    CREATE_FAILED after nine green nested stacks. The driver must wait the
    target out of CREATING before the first synchronize."""
    control = _SettleControl(["CREATING"] * 9 + ["READY"])
    slept = []
    driver._wait_target_created(
        control, gateway_id="gw", target_id="t", sleep=slept.append
    )
    assert control.calls == 10
    assert slept == [driver.TARGET_CREATE_POLL_SECONDS] * 9
    assert sum(slept) == 90


def test_created_wait_returns_immediately_when_not_creating():
    control = _SettleControl(["READY"])
    slept = []
    driver._wait_target_created(
        control, gateway_id="gw", target_id="t", sleep=slept.append
    )
    assert control.calls == 1 and slept == []


def test_created_wait_is_bounded_and_falls_through_to_sync():
    control = _SettleControl(["CREATING"] * 999)
    slept = []
    driver._wait_target_created(
        control, gateway_id="gw", target_id="t", sleep=slept.append
    )
    assert sum(slept) == driver.TARGET_CREATE_MAX_SECONDS
    assert driver.TARGET_CREATE_MAX_SECONDS >= 300, "budget must cover a slow cold account"


def test_ensure_onboarding_target_waits_for_creation_before_first_sync(monkeypatch):
    """Ordering pin: create -> warmup -> wait-out-CREATING -> synchronize."""
    order = []
    monkeypatch.setattr(
        driver, "create_target",
        lambda inp, control=None, sleep=None: (order.append("create"), types.SimpleNamespace(gateway_target_id="t"))[1],
    )
    monkeypatch.setattr(
        driver, "_wait_target_created",
        lambda control, *, gateway_id, target_id, sleep: order.append("wait_created"),
    )
    monkeypatch.setattr(
        driver, "_synchronize_until_ready",
        lambda control, *, gateway_id, target_id, region, sleep: order.append("sync"),
    )
    monkeypatch.setattr(driver, "verify", lambda inp, control=None: order.append("verify"))
    driver.ensure_onboarding_target(
        control=object(), gateway_id="gw", runtime_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/x",
        provider_arn="arn:p", region="us-east-1", warmup_seconds=0, sleep=lambda _s: None,
    )
    assert order == ["create", "wait_created", "sync", "verify"]


def test_settle_wait_polls_through_synchronizing_then_returns():
    """A just-synced target reading SYNCHRONIZING is transient, not fatal —
    the wait polls through it and returns once the status leaves the
    transient set (the strict verify() then does the honest final check)."""
    control = _SettleControl(["SYNCHRONIZING", "SYNCHRONIZING", "READY"])
    slept = []
    driver._wait_target_settled(
        control, gateway_id="gw", target_id="t", sleep=slept.append
    )
    assert control.calls == 3
    assert slept == [driver.TARGET_SETTLE_POLL_SECONDS] * 2


def test_settle_wait_is_bounded_and_falls_through_to_verify():
    """Never-settling transient status exhausts the bounded budget and
    RETURNS (verify() then fails loud) — the wait never spins forever."""
    control = _SettleControl(["SYNCHRONIZING"] * 999)
    slept = []
    driver._wait_target_settled(
        control, gateway_id="gw", target_id="t", sleep=slept.append
    )
    assert sum(slept) == driver.TARGET_SETTLE_MAX_SECONDS


def test_settle_wait_returns_immediately_on_terminal_status():
    """FAILED is not transient — no sleeping; verify() reports it honestly."""
    control = _SettleControl(["FAILED"])
    slept = []
    driver._wait_target_settled(
        control, gateway_id="gw", target_id="t", sleep=slept.append
    )
    assert control.calls == 1 and slept == []
