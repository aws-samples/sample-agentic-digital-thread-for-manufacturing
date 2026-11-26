# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/provision_runtimes.refresh_system_runtimes: move every per-system
``adt_*`` runtime to the current engine image.

The packaged stack's engine bootstrap custom resource calls this on a stack
update; deploy.sh step [2f] does the same job in bash. These tests pin the
behaviour [2f] pins in tests/unit/test_deploy_sh_postcdk_fail_loud.py, on a
scripted control plane (no AWS, fake clock):

* every ``adt_*`` runtime except the two engine runtimes is refreshed; one
  already READY on the image is skipped; an empty list is a no-op and a failed
  list is an error, never an empty list;
* each update is rebuilt from the runtime's CURRENT state: its own role, its
  env reconciled exactly as the [2f] shim reconciles it, its authorizer, its
  network config without the immutable S3-endpoint flag, MCP protocol;
* a refresh counts only at READY at the version its update returned (or a
  later version still on the image); failed or unexpected statuses, an
  unreadable status and the one shared time limit are named failures; a
  ConflictException waits, re-reads the runtime and retries once.

Runtime names are made up (``adt_alpha``...): nothing here depends on which
systems a deployment onboarded.
"""
import copy
import dataclasses
import importlib.util
import json
import os
import subprocess  # nosec B404 - fixed argv, our own interpreter
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.runtime_env_contract import (  # noqa: E402
    SYSTEM_GUIDANCE_TABLE_DEFAULT,
    RuntimeEnvSources,
    reconcile_env,
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

ACCOUNT = "111122223333"
OLD_IMAGE = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adt-runtime:old"
NEW_IMAGE = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adt-runtime:new"
SYSTEM_ROLE = f"arn:aws:iam::{ACCOUNT}:role/adt-system-runtime"
OTHER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/adt-other-runtime"
AUTHORIZER = {"customJWTAuthorizer": {
    "discoveryUrl": "https://idp.example.com/.well-known/openid-configuration",
    "allowedClients": ["m2m-client"]}}
SOURCES = RuntimeEnvSources(
    region="us-east-1",
    identity_claim_secret_arn=(
        f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:claim"),
    onboarding_sessions_table="sessions-table",
    account_id=ACCOUNT,
)


def _arn(rid):
    return f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:runtime/{rid}"


def _details(*, image=OLD_IMAGE, role=SYSTEM_ROLE, env=None, authorizer=AUTHORIZER,
             status="READY", version="1", network=None):
    d = {
        "status": status,
        "agentRuntimeVersion": version,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}},
        "roleArn": role,
        "environmentVariables": dict(env if env is not None else {
            "SYSTEM_ID": "alpha", "CONNECTION_SECRET_ARN": "arn:secret:alpha"}),
        "networkConfiguration": network or {
            "networkMode": "VPC",
            "networkModeConfig": {"subnets": ["subnet-0aa"],
                                  "securityGroups": ["sg-0aa"],
                                  "requireServiceS3Endpoint": True}},
        "protocolConfiguration": {"serverProtocol": "MCP"},
    }
    if authorizer:
        d["authorizerConfiguration"] = copy.deepcopy(authorizer)
    if role is None:
        d.pop("roleArn")
    return d


class ClientError(Exception):
    """The botocore ClientError shape the driver classifies on."""

    def __init__(self, code, message="rejected"):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


class Control:
    """Scripted AgentCore control plane.

    ``runtimes``: (name, runtime id, details). After each update attempt on a
    runtime, every ``get_agent_runtime`` takes the next state from that
    attempt's script (a dict merged onto the runtime; the last one repeats);
    with no script the runtime reads READY at the version its update returned.
    ``outcomes``: per runtime id, one entry per update attempt: ``"accept"``,
    ``("accept", version or None)`` or an exception to raise. ``writer``: per
    runtime id, details another writer applies when an update is rejected.
    ``cost``: seconds the fake clock advances per ("get"|"update", id) call.

    An accepted update REPLACES the runtime's whole configuration, as
    UpdateAgentRuntime does: every setting the update leaves out is gone
    afterwards. Only the service-owned fields (status, version) carry over.
    """

    def __init__(self, runtimes, *, clock, scripts=None, outcomes=None,
                 writer=None, list_error=None, page_size=None, get_errors=None,
                 cost=None, loop_token=False):
        self.clock = clock
        self.names = [(name, rid) for name, rid, _ in runtimes]
        self.state = {rid: copy.deepcopy(d) for _, rid, d in runtimes}
        self.scripts = {k: [list(s) for s in v] for k, v in (scripts or {}).items()}
        self.outcomes = {k: list(v) for k, v in (outcomes or {}).items()}
        self.writer = dict(writer or {})
        self.list_error = list_error
        self.page_size = page_size
        self.get_errors = {k: list(v) for k, v in (get_errors or {}).items()}
        self.cost = dict(cost or {})
        self.loop_token = loop_token
        self.attempts = {}
        self.calls = []
        self.updates = []

    def list_agent_runtimes(self, **kwargs):
        self.calls.append(("list", kwargs.get("nextToken")))
        if self.list_error:
            raise self.list_error
        items = [{"agentRuntimeName": n, "agentRuntimeId": r,
                  "agentRuntimeArn": _arn(r)} for n, r in self.names]
        if self.loop_token:
            return {"agentRuntimes": items[:1], "nextToken": "again"}
        if not self.page_size:
            return {"agentRuntimes": items}
        start = int(kwargs.get("nextToken") or 0)
        page = {"agentRuntimes": items[start:start + self.page_size]}
        if start + self.page_size < len(items):
            page["nextToken"] = str(start + self.page_size)
        return page

    def get_agent_runtime(self, agentRuntimeId):
        rid = agentRuntimeId
        self.calls.append(("get", rid))
        self.clock.now += self.cost.get(("get", rid), 0)
        if self.get_errors.get(rid):
            raise self.get_errors[rid].pop(0)
        attempt = self.attempts.get(rid, 0)
        scripts = self.scripts.get(rid, [])
        if attempt and attempt <= len(scripts) and scripts[attempt - 1]:
            script = scripts[attempt - 1]
            step = script.pop(0) if len(script) > 1 else script[0]
            self.state[rid].update(step)
        return copy.deepcopy(self.state[rid])

    def update_agent_runtime(self, **kwargs):
        rid = kwargs["agentRuntimeId"]
        self.calls.append(("update", rid))
        self.clock.now += self.cost.get(("update", rid), 0)
        self.attempts[rid] = self.attempts.get(rid, 0) + 1
        self.updates.append(copy.deepcopy(kwargs))
        queue = self.outcomes.get(rid)
        outcome = queue.pop(0) if queue else "accept"
        if isinstance(outcome, Exception):
            if rid in self.writer:
                self.state[rid].update(copy.deepcopy(self.writer.pop(rid)))
            raise outcome
        returned = str(int(self.state[rid].get("agentRuntimeVersion") or 0) + 1)
        if isinstance(outcome, tuple):
            returned = outcome[1]
        version = returned or self.state[rid].get("agentRuntimeVersion")
        self.state[rid] = {
            "status": "READY", "agentRuntimeVersion": version,
            **{k: copy.deepcopy(v) for k, v in kwargs.items() if k != "agentRuntimeId"},
        }
        return {"agentRuntimeVersion": returned} if returned else {}

    def updates_for(self, rid):
        return [u for u in self.updates if u["agentRuntimeId"] == rid]


ENGINE = [
    ("adt_orchestrator", "adt_orchestrator-O", _details(authorizer=None)),
    ("adt_onboarding", "adt_onboarding-N", _details()),
]


def _refresh(control, clock, **kwargs):
    kwargs.setdefault("deadline_seconds", 30)
    kwargs.setdefault("poll_seconds", 1)
    return driver.refresh_system_runtimes(
        control=control, image_uri=NEW_IMAGE, sources=SOURCES,
        sleep=clock.sleep, monotonic=clock.monotonic, **kwargs)


def _two(**control_kwargs):
    clock = Clock()
    control = Control(ENGINE + [
        ("adt_alpha", "adt_alpha-A", _details()),
        ("adt_beta", "adt_beta-B", _details(env={"SYSTEM_ID": "beta"})),
    ], clock=clock, **control_kwargs)
    return control, clock


# --------------------------------------------------------------------------- #
# What is refreshed, and how each update is built
# --------------------------------------------------------------------------- #
def test_each_stale_runtime_is_rebuilt_from_its_current_state():
    clock = Clock()
    env = {"SYSTEM_ID": "alpha", "SYSTEM_REQUIRED_GROUP": "ops",
           "CONNECTION_SECRET_ARN": "arn:secret:alpha", "ONBOARDING_SESSIONS_TABLE": ""}
    control = Control(ENGINE + [
        ("adt_alpha", "adt_alpha-A", _details(env=env)),
        ("adt_beta", "adt_beta-B", _details(role=OTHER_ROLE, authorizer=None,
                                            network={"networkMode": "PUBLIC"})),
        ("adt_current", "adt_current-C", _details(image=NEW_IMAGE)),
    ], clock=clock)
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha", "adt_beta"]
    assert result.current == ["adt_current"]
    assert result.failed == {}
    (alpha,) = control.updates_for("adt_alpha-A")
    assert alpha["agentRuntimeArtifact"] == {
        "containerConfiguration": {"containerUri": NEW_IMAGE}}
    assert alpha["roleArn"] == SYSTEM_ROLE
    assert alpha["authorizerConfiguration"] == AUTHORIZER
    assert alpha["protocolConfiguration"] == {"serverProtocol": "MCP"}
    assert alpha["networkConfiguration"] == {
        "networkMode": "VPC",
        "networkModeConfig": {"subnets": ["subnet-0aa"], "securityGroups": ["sg-0aa"]}}
    guidance = dataclasses.replace(SOURCES, system_guidance_table=SYSTEM_GUIDANCE_TABLE_DEFAULT)
    assert alpha["environmentVariables"] == reconcile_env(
        "system", env, guidance, span_emission=guidance.observability)
    assert alpha["environmentVariables"]["SYSTEM_REQUIRED_GROUP"] == "ops"
    # The span keys follow the deploy's option, as on a new runtime.
    assert alpha["environmentVariables"]["AGENT_OBSERVABILITY_ENABLED"] == "false"
    (beta,) = control.updates_for("adt_beta-B")
    assert beta["roleArn"] == OTHER_ROLE  # its own role, never a shared one
    assert "authorizerConfiguration" not in beta  # preserved absence, never invented
    assert beta["networkConfiguration"] == {"networkMode": "PUBLIC"}
    for engine_id in ("adt_orchestrator-O", "adt_onboarding-N"):
        assert ("get", engine_id) not in control.calls
        assert control.updates_for(engine_id) == []
    assert control.updates_for("adt_current-C") == []


#: Optional settings a runtime can carry, set by an operator or another tool.
#: UpdateAgentRuntime replaces the whole configuration, so an update that
#: leaves one out resets it.
OPTIONAL_SETTINGS = {
    "description": "Answers questions about one source system",
    "requestHeaderConfiguration": {"requestHeaderAllowlist": ["X-Request-Id"]},
    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 1800, "maxLifetime": 14400},
    "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/session"}}],
    "capacityProviderConfiguration": {"capacityProviderArn": (
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:capacity-provider/cp_one-abcdefghij")},
    "metadataConfiguration": {"requireMMDSV2": True},
}


@pytest.mark.parametrize("setting", sorted(OPTIONAL_SETTINGS))
def test_an_optional_setting_survives_the_image_change(setting):
    clock = Clock()
    control = Control(ENGINE + [
        ("adt_alpha", "adt_alpha-A", {**_details(), **copy.deepcopy(OPTIONAL_SETTINGS)}),
    ], clock=clock)
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha"], result.failed
    after = control.state["adt_alpha-A"]
    assert after["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == NEW_IMAGE
    assert after.get(setting) == OPTIONAL_SETTINGS[setting], (
        f"{setting} was reset by the full-replacement update")


def test_a_runtime_on_the_image_that_is_not_ready_is_still_updated():
    clock = Clock()
    control = Control(ENGINE + [
        ("adt_alpha", "adt_alpha-A", _details(image=NEW_IMAGE, status="UPDATE_FAILED")),
    ], clock=clock)
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha"] and result.current == []
    assert len(control.updates_for("adt_alpha-A")) == 1


def test_no_per_system_runtimes_is_a_clean_no_op(capsys):
    clock = Clock()
    control = Control(ENGINE, clock=clock)
    result = _refresh(control, clock)
    assert (result.refreshed, result.current, result.failed) == ([], [], {})
    assert control.updates == []
    assert "nothing to refresh" in capsys.readouterr().err


def test_a_failed_list_is_an_error_not_an_empty_list():
    control, clock = _two(list_error=ClientError("ExpiredTokenException", "token expired"))
    with pytest.raises(driver.ProvisionRuntimeError) as err:
        _refresh(control, clock)
    assert "could not list agent runtimes" in str(err.value)
    assert "ExpiredTokenException" in str(err.value)
    assert control.updates == []


def test_a_listing_that_repeats_its_token_is_bounded():
    clock = Clock()
    control = Control(ENGINE, clock=clock, loop_token=True)
    with pytest.raises(driver.ProvisionRuntimeError, match="could not list"):
        _refresh(control, clock)
    assert len([c for c in control.calls if c[0] == "list"]) <= 100


def test_every_page_of_the_listing_is_read():
    clock = Clock()
    control = Control(ENGINE + [
        (f"adt_s{i}", f"adt_s{i}-X", _details()) for i in range(5)
    ], clock=clock, page_size=2)
    result = _refresh(control, clock)
    assert result.refreshed == [f"adt_s{i}" for i in range(5)]


def test_a_missing_image_refreshes_nothing():
    control, clock = _two()
    with pytest.raises(driver.ProvisionRuntimeError, match="image"):
        driver.refresh_system_runtimes(control=control, image_uri="", sources=SOURCES,
                                       sleep=clock.sleep, monotonic=clock.monotonic)
    assert control.calls == []


def test_env_reconcile_matches_the_deploy_sh_shim():
    """[2f] reconciles each runtime's env through
    ``python3 -m agents.runtime_env_contract --role system --reconcile`` with
    the deploy's identity-claim ARN, sessions table and account in the env.
    The refresh, given the same values as sources, sends the same env."""
    existing = {"SYSTEM_ID": "alpha", "SYSTEM_REQUIRED_GROUP": "ops",
                "ONBOARDING_SESSIONS_TABLE": "", "ADT_SYSTEM_GUIDANCE_TABLE": "old-guidance",
                "SYSTEM_IDENTITY_CLAIM_SECRET_ARN": "arn:old", "AWS_ACCOUNT_ID": "999999999999",
                "SYSTEM_AGENT_MODEL": "us.vendor.model-small-v1"}
    shim_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": ".",
        "SYSTEM_IDENTITY_CLAIM_SECRET_ARN": SOURCES.identity_claim_secret_arn,
        "ONBOARDING_SESSIONS_TABLE": SOURCES.onboarding_sessions_table,
        "AWS_ACCOUNT_ID": ACCOUNT,
        "SYSTEM_AGENT_MODEL": "global.vendor.model-small-v1",
        "ALLOWED_MODELS": "vendor.model-small-v1",
        "RESPONSE_INTEGRITY_MODE": "annotate",
        "AGENT_SPAN_EMISSION": "false",
    }
    proc = subprocess.run(  # nosec B603 - fixed argv, our own interpreter
        [sys.executable, "-m", "agents.runtime_env_contract", "--role", "system",
         "--reconcile"], input=json.dumps(existing), capture_output=True, text=True,
        env=shim_env, cwd=str(_ADT_ROOT), timeout=60)
    assert proc.returncode == 0, proc.stderr
    sources = RuntimeEnvSources(
        identity_claim_secret_arn=SOURCES.identity_claim_secret_arn,
        onboarding_sessions_table=SOURCES.onboarding_sessions_table,
        account_id=ACCOUNT, system_agent_model="global.vendor.model-small-v1",
        allowed_models="vendor.model-small-v1", response_integrity_mode="annotate")
    clock = Clock()
    control = Control(ENGINE + [("adt_alpha", "adt_alpha-A", _details(env=existing))],
                      clock=clock)
    driver.refresh_system_runtimes(control=control, image_uri=NEW_IMAGE, sources=sources,
                                   sleep=clock.sleep, monotonic=clock.monotonic)
    (sent,) = control.updates_for("adt_alpha-A")
    assert sent["environmentVariables"] == json.loads(proc.stdout)


# --------------------------------------------------------------------------- #
# Per-runtime failures are named; the others carry on
# --------------------------------------------------------------------------- #
def test_a_rejected_update_is_named_and_not_retried():
    control, clock = _two(outcomes={"adt_beta-B": [ClientError("ValidationException", "bad env")]})
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha"]
    assert list(result.failed) == ["adt_beta"]
    assert result.failed["adt_beta"].startswith("update rejected")
    assert "ValidationException" in result.failed["adt_beta"]
    assert len(control.updates_for("adt_beta-B")) == 1


@pytest.mark.parametrize("status,reason", [
    ("UPDATE_FAILED", "UPDATE_FAILED: image pull failed"),
    ("CREATE_FAILED", "CREATE_FAILED: image pull failed"),
    ("DELETING", "unexpected status DELETING"),
])
def test_a_failed_status_after_the_update_is_named(status, reason):
    control, clock = _two(scripts={"adt_beta-B": [[
        {"status": "UPDATING", "agentRuntimeVersion": "2"},
        {"status": status, "agentRuntimeVersion": "2", "failureReason": "image pull failed"},
    ]]})
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha"]
    assert result.failed == {"adt_beta": reason}


def test_a_runtime_never_ready_within_the_limit_is_named_and_the_wait_is_bounded():
    control, clock = _two(scripts={"adt_beta-B": [[
        {"status": "UPDATING", "agentRuntimeVersion": "2"}]]})
    result = _refresh(control, clock, deadline_seconds=30, poll_seconds=10)
    assert result.failed == {"adt_beta": "not READY after 30s, last status UPDATING"}
    assert clock.now <= 30


def test_an_unreadable_status_is_named():
    control, clock = _two()
    original = control.get_agent_runtime

    def get(agentRuntimeId):
        # The read that builds the update succeeds; the status poll fails.
        if agentRuntimeId == "adt_beta-B" and control.attempts.get(agentRuntimeId):
            raise ClientError("AccessDeniedException")
        return original(agentRuntimeId)

    control.get_agent_runtime = get
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha"]
    assert result.failed == {"adt_beta": "could not read status"}


def test_an_unreadable_runtime_or_role_is_named_before_any_update():
    clock = Clock()
    control = Control(ENGINE + [
        ("adt_alpha", "adt_alpha-A", _details(role=None)),
        ("adt_beta", "adt_beta-B", _details()),
    ], clock=clock, get_errors={"adt_beta-B": [ClientError("ThrottlingException")]})
    result = _refresh(control, clock)
    assert result.failed["adt_alpha"] == "could not read roleArn"
    assert result.failed["adt_beta"].startswith("could not read runtime")
    assert control.updates == []


# --------------------------------------------------------------------------- #
# ConflictException: wait, re-read, retry once
# --------------------------------------------------------------------------- #
_CONFLICT = ClientError("ConflictException", "runtime is in UPDATING state")


def test_a_conflict_waits_then_rebuilds_from_current_settings_and_retries_once():
    writer = {"environmentVariables": {"SYSTEM_ID": "beta", "SYSTEM_AGENT_MODEL": "new-model"},
              "authorizerConfiguration": {"customJWTAuthorizer": {
                  "discoveryUrl": "https://new.example.com/d", "allowedClients": ["c"]}}}
    control, clock = _two(
        outcomes={"adt_beta-B": [_CONFLICT, "accept"]}, writer={"adt_beta-B": writer},
        scripts={"adt_beta-B": [[{"status": "UPDATING", "agentRuntimeVersion": "2"},
                                 {"status": "READY", "agentRuntimeVersion": "2"}]]})
    result = _refresh(control, clock)
    assert result.failed == {}
    assert result.refreshed == ["adt_alpha", "adt_beta"]
    first, retry = control.updates_for("adt_beta-B")
    assert retry["environmentVariables"]["SYSTEM_AGENT_MODEL"] == "new-model"
    assert retry["authorizerConfiguration"] == writer["authorizerConfiguration"]
    assert "SYSTEM_AGENT_MODEL" not in first["environmentVariables"]
    assert retry["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == NEW_IMAGE


def test_a_retry_rejected_again_is_named():
    control, clock = _two(outcomes={"adt_beta-B": [_CONFLICT, _CONFLICT]},
                          scripts={"adt_beta-B": [[{"status": "READY"}]]})
    result = _refresh(control, clock)
    assert result.failed == {"adt_beta": "update rejected again after waiting"}
    assert len(control.updates_for("adt_beta-B")) == 2


def test_a_conflict_that_never_leaves_updating_is_never_retried():
    control, clock = _two(outcomes={"adt_beta-B": [_CONFLICT]},
                          scripts={"adt_beta-B": [[{"status": "UPDATING"}]]})
    result = _refresh(control, clock)
    assert result.failed == {"adt_beta": "still UPDATING after 30s, update not retried"}
    assert len(control.updates_for("adt_beta-B")) == 1


# --------------------------------------------------------------------------- #
# READY counts only at the version the update returned
# --------------------------------------------------------------------------- #
def test_a_stale_ready_at_an_older_version_keeps_waiting():
    control, clock = _two(scripts={"adt_beta-B": [[
        {"status": "READY", "agentRuntimeVersion": "1"},
        {"status": "UPDATING", "agentRuntimeVersion": "2"},
        {"status": "READY", "agentRuntimeVersion": "2"}]]})
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha", "adt_beta"]


def test_ready_that_never_reaches_the_new_version_is_named():
    control, clock = _two(scripts={"adt_beta-B": [[
        {"status": "READY", "agentRuntimeVersion": "1"}]]})
    result = _refresh(control, clock)
    assert result.failed == {"adt_beta": (
        "not READY after 30s, last status READY at version 1, waiting for version 2")}


def test_a_later_version_counts_only_on_this_image():
    control, clock = _two(scripts={
        "adt_alpha-A": [[{"status": "READY", "agentRuntimeVersion": "7"}]],
        "adt_beta-B": [[{"status": "READY", "agentRuntimeVersion": "7",
                         "agentRuntimeArtifact": {"containerConfiguration": {
                             "containerUri": OLD_IMAGE}}}]]})
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha"]
    assert result.failed == {
        "adt_beta": "a later update (version 7) replaced this deploy's image"}


@pytest.mark.parametrize("returned", [None, ""])
def test_an_update_without_a_version_takes_the_first_ready(returned):
    control, clock = _two(outcomes={"adt_beta-B": [("accept", returned)]})
    result = _refresh(control, clock)
    assert result.refreshed == ["adt_alpha", "adt_beta"]


# --------------------------------------------------------------------------- #
# One time limit for the whole refresh
# --------------------------------------------------------------------------- #
def test_the_limit_covers_the_whole_refresh_not_only_the_ready_wait():
    clock = Clock()
    control = Control(ENGINE + [
        ("adt_alpha", "adt_alpha-A", _details()),
        ("adt_beta", "adt_beta-B", _details()),
    ], clock=clock, cost={("update", "adt_alpha-A"): 40})
    result = _refresh(control, clock, deadline_seconds=30)
    assert result.failed == {
        "adt_beta": "not sent: the 30s refresh limit ran out before its update",
        "adt_alpha": "not READY after 30s, not polled since its update was sent",
    }
    assert control.updates_for("adt_beta-B") == []
    assert ("get", "adt_beta-B") not in control.calls


def test_a_sleep_only_caller_still_gets_a_bounded_wait():
    control, clock = _two(scripts={"adt_beta-B": [[{"status": "UPDATING"}]]})
    result = driver.refresh_system_runtimes(
        control=control, image_uri=NEW_IMAGE, sources=SOURCES, sleep=clock.sleep,
        deadline_seconds=30, poll_seconds=10)
    assert "adt_beta" in result.failed
    assert clock.now <= 30
