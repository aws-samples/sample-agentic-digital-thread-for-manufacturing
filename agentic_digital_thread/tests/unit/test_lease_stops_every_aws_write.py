# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Once a reset's or a provision's lease is lost, no AWS write of it is sent,
wherever in the operation that write is made.

Checking the lease in each helper that writes left writes out: a Reset All
whose lease was lost after its first runtime delete deleted the second, its
second onboarding session and its second proposal; a provision whose lease
was lost during a wait still created the runtime again after a conflict or a
failed create, deleted the failed runtime, synchronized the gateway target
after its 30 s wait, created the credential provider, and wrote the secret's
``PutSecretValue`` fallback.

Now one botocore ``before-sign`` handler (``lease_guard``), on every botocore
session and client of the process, runs the bound lease check before every
attempt of every AWS operation that is not a known read
(``test_lease_stops_every_attempt_and_side_effect.py`` covers the retries). These tests run real botocore clients
whose requests are recorded and answered at ``before-send``
(``_aws_recorder``), so a request appears in ``sent`` only if botocore would
have put it on the wire. Each loses the lease after a batch's first item, or
during a wait or before a retry, and checks that the next write is never sent
and that the job ends ``reset_lease_lost`` or ``provision_lease_lost``.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess  # nosec B404 - fixed argv, our own interpreter
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import MagicMock

import boto3
import botocore.handlers
import botocore.session
import pytest

import agents.agent_manager as am
import agents.connection_resolver as cr
import agents.db as db
import agents.onboard as onboard
from agents import lease_guard, reset_lease
from agents.catalog import SystemCatalog
from agents.job_store import JobStore
from agents.onboarding_session import OnboardingSessionStore
from tests.unit._aws_recorder import AwsRecorder, error
from tests.unit.test_lease_loss_stops_every_step import ALL, ALL_RESULTS, Steps
from tests.unit.test_one_reset_lease import (  # noqa: F401 - fixtures
    SID, SID2, Replica, _one_process, clock, table)

LOSE = onboard._RESET_LEASE_SECONDS
AGENTS = Path(onboard.__file__).resolve().parent
ROLE = "arn:aws:iam::111122223333:role/adt-runtime"
IMAGE = "111122223333.dkr.ecr.us-east-1.amazonaws.com/adt:latest"
GW = "gw-1"


def _runtime(name: str, rid: str) -> dict:
    return {"agentRuntimeName": name, "agentRuntimeId": rid,
            "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{rid}"}


@pytest.fixture
def aws(monkeypatch):
    """Every boto3 client and resource the code under test builds is a
    recorder client (real botocore, nothing sent), the module-level ones an
    earlier test may have cached included: the proposals store's resource and
    the onboarding session store (rebuilt through the patched factory)."""
    import agents.onboard_agent as oa
    import agents.self_learning as sl
    rec = AwsRecorder()
    monkeypatch.setattr(boto3, "client", rec.client)
    monkeypatch.setattr(boto3, "resource", rec.resource)
    monkeypatch.setattr(sl, "_dynamodb", rec.resource("dynamodb"))
    monkeypatch.setattr(oa, "_store_singleton", None)
    return rec


class Lease:
    """A held lease bound as the write check; :meth:`lose` loses it."""

    def __init__(self, clock):
        store = JobStore("reset", table=None)
        key = reset_lease.provision_key(SID)
        lease = reset_lease.new(SID, LOSE)
        assert store.lease_acquire(key, lease, now=clock()) is None
        self.renewal = reset_lease.Renewal(store, key, lease, LOSE)
        self.clock = clock

    def lose(self, *_args, **_kwargs) -> None:
        self.clock.advance(LOSE)

    def bound(self):
        return lease_guard.write_guard(self.renewal.check)


@pytest.fixture
def lease(clock):
    return Lease(clock)


# ── which operations are reads ─────────────────────────────────────────────

@pytest.mark.parametrize("service,operation", [
    ("secretsmanager", "GetSecretValue"), ("secretsmanager", "DescribeSecret"),
    ("dynamodb", "GetItem"), ("dynamodb", "Query"), ("dynamodb", "Scan"),
    ("dynamodb", "BatchGetItem"), ("dynamodb", "TransactGetItems"),
    ("bedrock-agentcore-control", "ListAgentRuntimes"),
    ("bedrock-agentcore-control", "GetGatewayTarget"),
    ("cognito-idp", "DescribeUserPoolClient"), ("s3", "HeadObject"),
    ("iotsitewise", "BatchGetAssetPropertyValue"), ("iotsitewise", "ExecuteQuery"),
    ("athena", "GetQueryResults")])
def test_a_known_read_is_a_read(service, operation):
    assert lease_guard.is_read_operation(service, operation)


@pytest.mark.parametrize("service,operation", [
    ("bedrock-agentcore-control", "CreateAgentRuntime"),
    ("bedrock-agentcore-control", "DeleteAgentRuntime"),
    ("bedrock-agentcore-control", "UpdateAgentRuntime"),
    ("bedrock-agentcore-control", "CreateGatewayTarget"),
    ("bedrock-agentcore-control", "UpdateGatewayTarget"),
    ("bedrock-agentcore-control", "SynchronizeGatewayTargets"),
    ("bedrock-agentcore-control", "CreateOauth2CredentialProvider"),
    ("secretsmanager", "CreateSecret"), ("secretsmanager", "PutSecretValue"),
    ("secretsmanager", "DeleteSecret"), ("secretsmanager", "RestoreSecret"),
    ("dynamodb", "PutItem"), ("dynamodb", "UpdateItem"), ("dynamodb", "DeleteItem"),
    ("dynamodb", "BatchWriteItem"), ("dynamodb", "TransactWriteItems"),
    ("neptune-graph", "ExecuteQuery"), ("athena", "StartQueryExecution"),
    ("bedrock-runtime", "Converse"), ("sts", "AssumeRole"), ("lambda", "Invoke"),
    ("states", "StartExecution"), ("any", "Getaway"), ("any", "FrobnicateTheThing")])
def test_anything_else_is_a_write(service, operation):
    """Fail closed: an operation not known to be a read counts as a write."""
    assert not lease_guard.is_read_operation(service, operation)


def test_a_graph_statement_is_a_read_only_where_the_graph_layer_marks_it():
    assert not lease_guard.is_read_operation("neptune-graph", "ExecuteQuery")
    with lease_guard.graph_read("MATCH (n) RETURN n"):
        assert lease_guard.is_read_operation("neptune-graph", "ExecuteQuery")
        with lease_guard.graph_read("MATCH (n) SET n.x = 1"):
            assert not lease_guard.is_read_operation("neptune-graph", "ExecuteQuery")
    assert not lease_guard.is_read_operation("neptune-graph", "ExecuteQuery")


# ── every client the process builds runs the check ─────────────────────────

def _secretsmanager(form: str):
    creds = {"region_name": "us-east-1", "aws_access_key_id": "testing",
             "aws_secret_access_key": "testing"}  # nosec B106 - fake
    if form == "boto3.client":
        return boto3.client("secretsmanager", **creds)
    if form == "boto3.Session":
        return boto3.Session(**creds).client("secretsmanager")
    if form == "boto3.session.Session":
        return boto3.session.Session(**creds).client("secretsmanager")
    if form == "botocore.get_session":
        return botocore.session.get_session().create_client("secretsmanager", **creds)
    raise AssertionError(form)


FORMS = ["boto3.client", "boto3.Session", "boto3.session.Session", "botocore.get_session"]


@pytest.mark.parametrize("form", FORMS)
def test_every_client_form_sends_no_write_once_the_lease_is_lost(lease, form):
    rec = AwsRecorder()
    client = rec.attach(_secretsmanager(form))
    with lease.bound():
        lease.lose()
        client.get_secret_value(SecretId="adt/conn/x")                 # a read still runs
        with pytest.raises(reset_lease.LeaseLostError):
            client.put_secret_value(SecretId="adt/conn/x", SecretString="{}")
    assert rec.names() == ["GetSecretValue"]


def test_a_dynamodb_resource_sends_no_write_once_the_lease_is_lost(lease):
    rec = AwsRecorder()
    table = rec.attach(boto3.resource(
        "dynamodb", region_name="us-east-1", aws_access_key_id="testing",
        aws_secret_access_key="testing").meta.client)                  # nosec B106 - fake
    with lease.bound():
        lease.lose()
        table.get_item(TableName="t", Key={"k": {"S": "a"}})
        with pytest.raises(reset_lease.LeaseLostError):
            table.update_item(TableName="t", Key={"k": {"S": "a"}},
                              UpdateExpression="SET v = :v",
                              ExpressionAttributeValues={":v": {"S": "b"}})
    assert rec.names() == ["GetItem"]


@pytest.mark.parametrize("form", FORMS)
def test_no_client_is_touched_outside_a_lease_or_while_it_is_held(lease, form):
    rec = AwsRecorder()
    client = rec.attach(_secretsmanager(form))
    client.delete_secret(SecretId="adt/conn/x", RecoveryWindowInDays=7)
    with lease.bound():
        client.delete_secret(SecretId="adt/conn/y", RecoveryWindowInDays=7)
    lease.lose()
    client.delete_secret(SecretId="adt/conn/z", RecoveryWindowInDays=7)  # no lease bound here
    assert rec.names() == ["DeleteSecret"] * 3


def test_the_check_is_installed_when_the_package_is_imported():
    tree = ast.parse((AGENTS / "__init__.py").read_text())
    calls = [n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert calls == ["_install_lease_guard"]
    assert lease_guard._HANDLER_SPEC in botocore.handlers.BUILTIN_HANDLERS


_FRESH_CHECK = (
    "from agents import lease_guard, reset_lease\n"
    "import botocore.handlers\n"
    "assert botocore.handlers.BUILTIN_HANDLERS.count(lease_guard._HANDLER_SPEC) == 1\n"
    "client = boto3.client('secretsmanager', region_name='us-east-1',\n"
    "                      aws_access_key_id='t', aws_secret_access_key='t')\n"
    "def lost():\n"
    "    raise reset_lease.LeaseLostError('lost')\n"
    "with lease_guard.write_guard(lost):\n"
    "    try:\n"
    "        client.delete_secret(SecretId='x', RecoveryWindowInDays=7)\n"
    "    except reset_lease.LeaseLostError:\n"
    "        print('BLOCKED')\n")


@pytest.mark.parametrize("first", ["agents", "boto3"])
def test_a_fresh_process_runs_the_check_whichever_it_imports_first(first):
    """The package imports neither boto3 nor botocore (modules that must
    import without them still can); the check is in place either way."""
    imports = ("import sys\nimport agents\n"
               "assert 'botocore' not in sys.modules and 'boto3' not in sys.modules\n"
               "import boto3\n") if first == "agents" else "import boto3\nimport agents\n"
    proc = subprocess.run([sys.executable, "-c", imports + _FRESH_CHECK],  # nosec B603
                          cwd=str(AGENTS.parent), capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PYTHONPATH": str(AGENTS.parent),
                               "AWS_EC2_METADATA_DISABLED": "true"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "BLOCKED" in proc.stdout


def test_install_also_covers_a_default_session_made_before_it(lease, monkeypatch):
    spec = lease_guard._HANDLER_SPEC
    monkeypatch.setattr(botocore.handlers, "BUILTIN_HANDLERS",
                        [h for h in botocore.handlers.BUILTIN_HANDLERS if h != spec])
    early = boto3.Session(region_name="us-east-1", aws_access_key_id="testing",
                          aws_secret_access_key="testing")              # nosec B106 - fake
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", early)
    lease_guard.install()
    lease_guard.install()                                               # idempotent
    assert botocore.handlers.BUILTIN_HANDLERS.count(spec) == 1
    rec = AwsRecorder()
    client = rec.attach(early.client("secretsmanager"))
    with lease.bound():
        lease.lose()
        with pytest.raises(reset_lease.LeaseLostError):
            client.delete_secret(SecretId="adt/conn/x", RecoveryWindowInDays=7)
    assert rec.names() == []


#: How the package's modules build AWS clients; each of them makes its client
#: from a botocore session, which runs the check (FORMS above).
_CLIENT_FORMS = re.compile(
    r"\b(?:boto3|_boto3|_b3)\.(?:client|resource|Session|session\.Session)\("
    r"|\b_get_botocore_session\(\)")
_OTHER_CLIENTS = re.compile(r"\b(?:aioboto3|aiobotocore|ClientCreator|BaseClient)\b")


def test_every_aws_client_the_package_builds_comes_from_a_botocore_session():
    """Audit: each of the package's AWS client constructions is one of the
    forms above, all of which come from botocore sessions; none bypasses one."""
    found, other = 0, []
    for path in sorted(AGENTS.rglob("*.py")):
        text = path.read_text()
        found += len(_CLIENT_FORMS.findall(text))
        if path.name != "lease_guard.py":     # it only finds the clients that exist
            other += [f"{path.name}: {m}" for m in _OTHER_CLIENTS.findall(text)]
    assert found >= 80, found
    assert other == []


# ── the bound check reaches every thread the job's work runs on ────────────

def test_the_bound_check_reaches_the_threads_a_job_uses(lease, monkeypatch):
    import agents.concept_bindings as cb
    from agents.offload import offload
    seen: dict = {}

    def _record(name):
        def _fn(*_a, **_kw):
            seen[name] = lease_guard._WRITE_GUARD.get()
            return []
        return _fn
    monkeypatch.setattr(onboard, "neptune_query_checked", _record("learned-rows"))
    monkeypatch.setattr(cb, "_field_concepts_graph_read", _record("concept-bindings"))
    monkeypatch.setattr(onboard, "_learned_rows_inflight", None)

    async def _offloaded():
        await asyncio.to_thread(_record("to_thread"))
        await offload(_record("offload"))
    with lease.bound():
        onboard._learned_rows_read("MATCH (n) RETURN n", {})
        cb._bounded_read(({"system": SID},))
        asyncio.run(_offloaded())
    assert seen == {name: lease.renewal.check for name in
                    ("learned-rows", "concept-bindings", "to_thread", "offload")}, seen


def test_a_plain_thread_a_job_starts_does_not_inherit_the_check(lease):
    """A long-lived thread (the MQTT subscriber) outlives the job, so it must
    not stop on the job's lost lease."""
    seen: dict = {}
    with lease.bound():
        worker = threading.Thread(target=lambda: seen.update(g=lease_guard._WRITE_GUARD.get()))
        worker.start()
        worker.join(10)
    assert seen == {"g": None}


def test_a_graph_read_is_sent_after_the_lease_is_lost_and_a_graph_write_is_not(
        lease, aws, monkeypatch):
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-1")
    aws.answer("ExecuteQuery", {"results": [{"n": 1}]})
    with lease.bound():
        lease.lose()
        assert db.neptune_query_checked("MATCH (n) RETURN n") == [{"n": 1}]
        with pytest.raises(reset_lease.LeaseLostError):
            db._neptune_execute("CREATE (n:X)", None)    # a statement nobody marked a read
    assert aws.names() == ["ExecuteQuery"]


# ── Reset All: no item after the loss ──────────────────────────────────────

@pytest.fixture
def reset_all(clock, aws, monkeypatch):
    """Run Reset All's job with every step a recorder except *real*; the
    catalog holds two onboarded systems. Returns the job's record."""
    monkeypatch.setattr(onboard, "_agent_manager", MagicMock())
    monkeypatch.setattr(onboard, "catalog", SystemCatalog(
        [{"id": SID, "name": "Line MES", "status": "onboarded",
          "connection_type": "PostgreSQL", "schema": "mes"},
         {"id": SID2, "name": "Plant CMMS", "status": "onboarded",
          "connection_type": "PostgreSQL", "schema": "cmms"}]))

    def _run(real: str):
        Steps(monkeypatch, clock, [s for s in ALL if s != real], ALL_RESULTS)
        assert onboard._RESET_DOMAIN.claim_all(True) is None
        asyncio.run(onboard._run_reset_all_job("job-x", True))
        assert not onboard._RESET_DOMAIN.reset_all_running()
        return onboard._RESET_ALL_JOBS.get("job-x")
    return _run


def _lost_reset(record: dict) -> None:
    assert record["state"] == "error" and record["reason"] == "reset_lease_lost", record
    statuses = {e["id"]: e["status"] for e in onboard.catalog.get_all()}
    assert statuses == {SID: "removal_failed", SID2: "removal_failed"}, statuses


def test_reset_all_deletes_no_runtime_after_the_lease_is_lost_mid_batch(
        reset_all, aws, clock, monkeypatch):
    monkeypatch.delenv("AGENTCORE_GATEWAY_ID", raising=False)
    aws.answer("ListAgentRuntimes", {"agentRuntimes": [_runtime("adt_sys_a", "r-1"),
                                                       _runtime("adt_sys_b", "r-2")]})
    aws.on_send["DeleteAgentRuntime"] = lambda _s: clock.advance(LOSE)
    record = reset_all("_global_reset_cleanup_agentcore_sync")
    deletes = [urlparse(s.url).path.rstrip("/").rsplit("/", 1)[-1]
               for s in aws.ops("DeleteAgentRuntime")]
    assert deletes == ["r-1"], aws.names()
    _lost_reset(record)


def test_reset_all_deletes_no_session_after_the_lease_is_lost_mid_batch(
        reset_all, aws, clock, monkeypatch):
    import agents.onboard_agent as oa
    store = OnboardingSessionStore(dynamodb_resource=aws.resource("dynamodb"))
    monkeypatch.setattr(oa, "_store", lambda: store)
    aws.answer("DeleteItem", {"Attributes": {"system_id": {"S": SID}}})
    aws.on_send["DeleteItem"] = lambda _s: clock.advance(LOSE)
    record = reset_all("_delete_onboarding_sessions")
    deleted = [s.body["Key"]["system_id"]["S"] for s in aws.ops("DeleteItem")]
    assert deleted == [SID], aws.names()
    _lost_reset(record)


def test_reset_all_retires_no_proposal_after_the_lease_is_lost_mid_batch(
        reset_all, aws, clock, monkeypatch):
    import agents.self_learning as sl
    monkeypatch.setattr(sl, "_dynamodb", aws.resource("dynamodb"))

    def _row(mid, sid):
        return {"mapping_id": {"S": mid}, "discovered_at": {"S": "2026-10-01"},
                "status": {"S": "pending"}, "from_system": {"S": sid}, "source": {"S": "agent"}}
    aws.answer("Query", {"Items": [_row("m1", SID), _row("m2", SID2)], "Count": 2})
    aws.on_send["UpdateItem"] = lambda _s: clock.advance(LOSE)
    record = reset_all("_retire_many_or_incomplete")
    updated = [s.body["Key"]["mapping_id"]["S"] for s in aws.ops("UpdateItem")]
    assert updated == ["m1"], aws.names()
    _lost_reset(record)


def test_reset_all_schedules_no_secret_after_the_lease_is_lost_mid_batch(
        reset_all, aws, clock, monkeypatch):
    secrets = aws.client("secretsmanager")
    monkeypatch.setattr(cr, "_secrets_client", lambda: secrets)
    monkeypatch.setattr(cr, "is_user_registration", lambda _entry: True)
    monkeypatch.setattr(cr, "_own_pointer_for_removal", lambda sid, _e: f"arn:{sid}")
    monkeypatch.setattr(cr, "_own_payload_for_removal", lambda sid, _arn: {"sid": sid})
    monkeypatch.setattr(cr, "is_bound_user_secret", lambda _p, _sid: True)
    aws.on_send["DeleteSecret"] = lambda _s: clock.advance(LOSE)
    record = reset_all("_delete_user_registration_secrets")
    assert [s.body["SecretId"] for s in aws.ops("DeleteSecret")] == [f"arn:{SID}"]
    _lost_reset(record)


# ── a provision: no write after a wait or a retry once the lease is lost ───

@pytest.fixture
def provision(table, clock, aws, monkeypatch):
    """Run *step* as a provision job's provision, under the job's real claim
    and provision lease; returns the job's record. Waits do not sleep."""
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    def _run(step):
        monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
        monkeypatch.setattr(onboard, "_do_provision", lambda _body: step() or {"status": "x"})
        with Replica(table):
            claim = onboard._claim_provision(SID)
            onboard._run_provision_job("job-p", MagicMock(system_id=SID, mappings=[]),
                                       claim=claim)
            return onboard._provision_jobs["job-p"]
    _run.sleeps = sleeps
    return _run


def _lost_provision(record: dict) -> None:
    assert record.get("reason") == "provision_lease_lost", record
    assert record.get("retryable") is True


@pytest.fixture
def manager():
    mgr = am.AgentManager(mode="cloud", gateway_id=GW, gateway_url="https://gw.example.com/mcp",
                          ecr_image_uri=IMAGE, runtime_role_arn=ROLE)
    mgr._m2m_client_id = "m2m-client"
    mgr._m2m_pool_id = "us-east-1_pool"
    mgr._m2m_discovery_url = "https://cognito.example.com/.well-known/openid-configuration"
    return mgr


def _create_kwargs(name: str = "adt_line_mes") -> dict:
    return {"agentRuntimeName": name,
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE}},
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "MCP"}, "roleArn": ROLE}


def _target_kwargs(endpoint: str = "https://rt.example.com/mcp") -> dict:
    return {"gatewayIdentifier": GW, "name": "line-mes", "description": "System agent",
            "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": endpoint}}}}


def _nth(n: int, action):
    """An ``on_send`` hook that runs *action* on the *n*-th send (1-based)."""
    seen = {"n": 0}

    def _hook(_sent):
        seen["n"] += 1
        if seen["n"] == n:
            action()
    return _hook


def test_no_runtime_create_follows_a_conflict_wait_once_the_lease_is_lost(
        provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("CreateAgentRuntime", error("ConflictException", 409))
    aws.answer("ListAgentRuntimes", {"agentRuntimes": [_runtime("adt_line_mes", "r-old")]})
    aws.answer("GetAgentRuntime", error("ResourceNotFoundException", 404))
    aws.on_send["ListAgentRuntimes"] = lambda _s: clock.advance(LOSE)   # the stale-runtime wait
    record = provision(lambda: manager._create_runtime_conflict_retry(
        control, "adt_line_mes", _create_kwargs()))
    assert aws.names() == ["CreateAgentRuntime", "ListAgentRuntimes", "GetAgentRuntime"]
    _lost_provision(record)


def test_no_runtime_create_is_retried_once_the_lease_is_lost(provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("CreateAgentRuntime", _runtime("adt_line_mes", "r-1"))
    aws.answer("GetAgentRuntime", {"status": "CREATE_FAILED", "failureReason": "InternalError"},
               error("ResourceNotFoundException", 404))
    aws.on_send["GetAgentRuntime"] = _nth(2, lambda: clock.advance(LOSE))  # before the retry
    record = provision(lambda: manager._create_runtime_with_az_retry(
        control, "adt_line_mes", {}, None))
    assert aws.names() == ["CreateAgentRuntime", "GetAgentRuntime", "DeleteAgentRuntime",
                           "GetAgentRuntime"]
    _lost_provision(record)


def test_no_failed_runtime_is_deleted_once_the_lease_is_lost(provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("CreateAgentRuntime", _runtime("adt_line_mes", "r-1"))
    aws.answer("GetAgentRuntime", {"status": "CREATE_FAILED", "failureReason": "InternalError"})
    aws.on_send["GetAgentRuntime"] = lambda _s: clock.advance(LOSE)   # the READY poll
    record = provision(lambda: manager._create_runtime_with_az_retry(
        control, "adt_line_mes", {}, None))
    assert aws.names() == ["CreateAgentRuntime", "GetAgentRuntime"]
    _lost_provision(record)


def test_no_gateway_target_is_synchronized_after_its_wait_once_the_lease_is_lost(
        provision, manager, aws, clock, monkeypatch):
    control = aws.client("bedrock-agentcore-control")
    monkeypatch.setattr(time, "sleep", lambda _s: clock.advance(LOSE))  # the 30 s wait
    record = provision(lambda: manager._sync_gateway_target(control, "t-1", "line-mes",
                                                            gateway_id=GW))
    assert aws.names() == []
    _lost_provision(record)


def test_no_credential_provider_is_created_once_the_lease_is_lost(
        provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("ListOauth2CredentialProviders", {"credentialProviders": []})
    aws.answer("DescribeUserPoolClient", {"UserPoolClient": {"ClientSecret": "s"}})
    aws.on_send["DescribeUserPoolClient"] = lambda _s: clock.advance(LOSE)
    record = provision(lambda: manager._ensure_credential_provider(control, "us-east-1"))
    assert aws.names() == ["ListOauth2CredentialProviders", "DescribeUserPoolClient"]
    _lost_provision(record)


def test_no_agent_config_secret_falls_back_to_a_put_once_the_lease_is_lost(
        provision, manager, aws, clock):
    aws.answer("CreateSecret", error("ResourceExistsException"))
    aws.on_send["CreateSecret"] = lambda _s: clock.advance(LOSE)
    record = provision(lambda: manager._upsert_agent_config_secret(
        SID, {"host": "h"}, "prompt", "tables", "us-east-1"))
    assert aws.names() == ["CreateSecret"]
    _lost_provision(record)


def test_no_connection_secret_falls_back_to_a_put_once_the_lease_is_lost(
        provision, aws, clock, monkeypatch):
    secrets = aws.client("secretsmanager")
    monkeypatch.setattr(cr, "_secrets_client", lambda: secrets)
    aws.answer("CreateSecret", error("ResourceExistsException"))
    aws.on_send["CreateSecret"] = lambda _s: clock.advance(LOSE)
    record = provision(lambda: cr._store_connection_secret(SID, {"password": "x"}))
    assert aws.names() == ["CreateSecret"]
    _lost_provision(record)


def test_no_gateway_target_is_deleted_for_a_recreate_once_the_lease_is_lost(
        provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("CreateGatewayTarget", error("ConflictException", 409))
    aws.answer("ListGatewayTargets", {"items": [{"targetId": "t-1", "name": "line-mes",
                                                 "status": "FAILED"}]})
    aws.on_send["CreateGatewayTarget"] = lambda _s: clock.advance(LOSE)
    record = provision(lambda: manager._create_or_reuse_gateway_target(
        control, "line-mes", _target_kwargs()))
    assert aws.names() == ["CreateGatewayTarget", "ListGatewayTargets"]
    _lost_provision(record)


def test_no_gateway_target_is_created_again_after_its_delete_wait_once_the_lease_is_lost(
        provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("CreateGatewayTarget", error("ConflictException", 409))
    aws.answer("ListGatewayTargets", {"items": [{"targetId": "t-1", "name": "line-mes",
                                                 "status": "FAILED"}]})
    aws.answer("GetGatewayTarget", error("ResourceNotFoundException", 404))
    aws.on_send["GetGatewayTarget"] = lambda _s: clock.advance(LOSE)   # the delete wait
    record = provision(lambda: manager._create_or_reuse_gateway_target(
        control, "line-mes", _target_kwargs()))
    assert aws.names() == ["CreateGatewayTarget", "ListGatewayTargets", "DeleteGatewayTarget",
                           "GetGatewayTarget"]
    _lost_provision(record)


def test_no_gateway_target_is_updated_once_the_lease_is_lost(provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("GetGatewayTarget", {
        "targetId": "t-1", "name": "line-mes", "status": "READY", "gatewayArn": "a",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": "https://old.example.com"}}}})
    aws.on_send["GetGatewayTarget"] = lambda _s: clock.advance(LOSE)
    record = provision(lambda: manager._reuse_or_align_gateway_target(
        control, "t-1", "line-mes", _target_kwargs()))
    assert aws.names() == ["GetGatewayTarget"]
    _lost_provision(record)


def _runtime_detail(**extra) -> dict:
    return {**_runtime("adt_line_mes", "r-1"), "status": "READY", "roleArn": ROLE,
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE}},
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "MCP"}, **extra}


def test_no_runtime_image_is_updated_once_the_lease_is_lost(provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("GetAgentRuntime", _runtime_detail())
    aws.on_send["GetAgentRuntime"] = lambda _s: clock.advance(LOSE)
    record = provision(lambda: manager._refresh_existing_runtime_image(
        control, "r-1", "adt_line_mes", {}, None))
    assert aws.names() == ["GetAgentRuntime"]
    _lost_provision(record)


def test_no_stale_authorizer_runtime_is_deleted_once_the_lease_is_lost(
        provision, manager, aws, clock):
    control = aws.client("bedrock-agentcore-control")
    aws.answer("GetAgentRuntime", _runtime_detail(authorizerConfiguration={
        "customJWTAuthorizer": {"discoveryUrl": "https://old.example.com/.well-known"}}))
    aws.on_send["GetAgentRuntime"] = lambda _s: clock.advance(LOSE)
    wanted = {"customJWTAuthorizer": {"discoveryUrl": "https://new.example.com/.well-known"}}
    record = provision(lambda: manager._delete_stale_authorizer_runtime(
        control, "adt_line_mes", "r-1", wanted))
    assert aws.names() == ["GetAgentRuntime"]
    _lost_provision(record)


def test_a_provision_whose_lease_holds_sends_its_writes(provision, manager, aws):
    """The same calls with the lease held: nothing is held back."""
    control = aws.client("bedrock-agentcore-control")
    record = provision(lambda: manager._sync_gateway_target(control, "t-1", "line-mes",
                                                            gateway_id=GW))
    assert aws.names()[0] == "SynchronizeGatewayTargets", aws.names()
    assert len(aws.ops("SynchronizeGatewayTargets")) == 1
    assert record["status"] == "done", record
