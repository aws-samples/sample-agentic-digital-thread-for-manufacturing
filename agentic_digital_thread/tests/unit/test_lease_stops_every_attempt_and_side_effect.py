# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A lost lease stops a reset's or a provision's writes on every attempt and
on every path: the SDK's own retries, a graph statement whoever runs it,
AWS clients made before the package was imported, and the provision side
effects that are not AWS API calls.

* The botocore retry loop builds, signs and sends each retry itself. The
  check runs on botocore's ``before-sign`` event, which fires for every
  attempt before the request is signed, so a retry after a lost lease is
  never signed or sent.
* ``db.neptune_query_checked`` runs any statement it is given. A statement is
  a read only when ``cypher_read_only.write_clause`` finds no write in it;
  anything else (a write, a statement it cannot read) runs the check first,
  on both graph backends.
* A client made before ``agents`` is imported copied its handlers already.
  Every process that runs a reset or a provision is started through a module
  of the package, and the install also adds the check to every botocore
  session and client that exists by then.
* The provision's raw MCP warmup POST and the local system agent's server
  start check the lease first; a local agent a provision started is stopped
  and its port released when a later check finds the lease lost; and it
  listens on loopback only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - fixed argv, our own interpreter
import sys
from pathlib import Path

import botocore.signers
import pytest
from botocore.config import Config

import agents.agent_manager as am
import agents.db as db
from agents import lease_guard, reset_lease
from agents.onboarding_session import OnboardingSessionStore
from tests.unit._aws_recorder import AwsRecorder, error
from tests.unit.test_lease_stops_every_aws_write import (  # noqa: F401 - fixtures
    LOSE, Lease, _lost_provision, _lost_reset, aws, lease, manager, provision, reset_all)
from tests.unit.test_one_reset_lease import SID, clock, table  # noqa: F401 - fixtures

ADT = Path(am.__file__).resolve().parents[1]
#: A client that tries each call twice: the SDK retries a 500 once.
TWO_ATTEMPTS = Config(retries={"total_max_attempts": 2, "mode": "standard"})


@pytest.fixture(autouse=True)
def _offline(aws):
    """No request of these tests reaches AWS: every client the code under
    test builds is a recorder client, the cached ones too (``aws``)."""
    return aws


@pytest.fixture
def signatures(monkeypatch):
    """How many attempts were signed: botocore picks the signer for each one
    right before signing it (``RequestSigner.get_auth_instance``)."""
    signed: list = []
    original = botocore.signers.RequestSigner.get_auth_instance

    def _count(self, *args, **kwargs):
        signed.append(kwargs.get("signing_name"))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(botocore.signers.RequestSigner, "get_auth_instance", _count)
    return signed


def _put(client, key: str = "a"):
    return client.put_item(TableName="t", Item={"k": {"S": key}})


# ── the SDK's own retries ──────────────────────────────────────────────────

def test_the_check_runs_on_every_attempt_of_a_call(signatures):
    rec = AwsRecorder()
    client = rec.client("dynamodb", config=TWO_ATTEMPTS)
    rec.answer("PutItem", error("InternalServerError", 500), {})
    checks: list = []
    with lease_guard.write_guard(lambda: checks.append(1)):
        response = _put(client)
    assert response["ResponseMetadata"]["RetryAttempts"] == 1
    assert rec.names() == ["PutItem", "PutItem"] and len(signatures) == 2
    assert len(checks) == 2


def test_an_sdk_retry_after_the_lease_is_lost_is_never_signed_or_sent(lease, signatures, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    rec = AwsRecorder()
    client = rec.client("dynamodb", config=TWO_ATTEMPTS)
    rec.answer("PutItem", error("InternalServerError", 500))
    rec.on_send["PutItem"] = lambda _s: lease.lose()
    sitewise = rec.client("iotsitewise")
    rec.answer("ExecuteQuery", {"columns": [], "rows": []})
    with lease.bound():
        with pytest.raises(reset_lease.LeaseLostError):
            _put(client)
        sitewise.execute_query(queryStatement="SELECT asset_id FROM asset")   # a read still runs
    assert rec.names() == ["PutItem", "ExecuteQuery"]
    assert len(signatures) == 2                       # the PutItem once, the query once


def test_a_provision_whose_retried_write_finds_the_lease_lost_records_it(
        provision, aws, clock, signatures):
    store = OnboardingSessionStore(dynamodb_resource=aws.resource("dynamodb", config=TWO_ATTEMPTS))
    aws.answer("UpdateItem", error("InternalServerError", 500))
    aws.on_send["UpdateItem"] = lambda _s: clock.advance(LOSE)
    record = provision(lambda: store.finalize(SID, "onboarded"))
    assert aws.names() == ["UpdateItem"] and len(signatures) == 1
    _lost_provision(record)


def test_a_reset_all_whose_retried_delete_finds_the_lease_lost_records_it(
        reset_all, aws, clock, monkeypatch):
    import agents.onboard_agent as oa
    store = OnboardingSessionStore(dynamodb_resource=aws.resource("dynamodb", config=TWO_ATTEMPTS))
    monkeypatch.setattr(oa, "_store", lambda: store)
    aws.answer("DeleteItem", error("InternalServerError", 500))
    aws.on_send["DeleteItem"] = lambda _s: clock.advance(LOSE)
    record = reset_all("_delete_onboarding_sessions")
    assert len(aws.ops("DeleteItem")) == 1, aws.names()
    _lost_reset(record)


# ── a graph statement is a read only when it reads ─────────────────────────

WRITES = [
    "CREATE (n:ShouldNotWrite) RETURN n",
    "MATCH (n:System) SET n.status = 'x' RETURN n",
    "MERGE (n:System {id: $id}) RETURN n",
    "MATCH (n:System) DETACH DELETE n",
    "MATCH (n) REMOVE n.status RETURN n",
    "CALL db.labels() YIELD label RETURN label",
    "MATCH (n) RETURN n UNION MATCH (m) CREATE (x) RETURN x",
    "MATCH (n) WHERE n.name = 'never closed RETURN n",      # unreadable
]
READS = ["MATCH (n:System) RETURN n.id AS id", "MATCH (n) WHERE n.createdAt > 1 RETURN n"]


@pytest.fixture
def cloud_graph(aws, monkeypatch):
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-1")
    aws.answer("ExecuteQuery", {"results": [{"n": 1}]})
    return lambda: aws.names()


@pytest.fixture
def local_graph(monkeypatch):
    ran: list = []
    monkeypatch.setattr(db, "_graph_backend", lambda: "local")
    monkeypatch.setattr(db, "_memgraph_run", lambda cypher, _p=None: ran.append(cypher) or [{"n": 1}])
    return lambda: ["run"] * len(ran)


@pytest.fixture(params=["cloud", "local"])
def graph(request):
    return request.getfixturevalue(f"{request.param}_graph")


@pytest.mark.parametrize("cypher", WRITES)
def test_a_graph_statement_that_is_not_a_read_is_stopped_once_the_lease_is_lost(
        graph, lease, cypher):
    with lease.bound():
        lease.lose()
        with pytest.raises(reset_lease.LeaseLostError):
            db.neptune_query_checked(cypher, {"id": "x"})
        with pytest.raises(reset_lease.LeaseLostError):
            db.neptune_query(cypher, {"id": "x"})       # the degrading reader does not hide it
        for read in READS:                              # reads still run
            assert db.neptune_query_checked(read) == [{"n": 1}]
    assert len(graph()) == len(READS)
    assert db.neptune_query_checked(cypher, {"id": "x"}) == [{"n": 1}]   # no lease bound: runs
    assert len(graph()) == len(READS) + 1


# ── clients made before the package is imported ────────────────────────────

def _entrypoint_modules() -> list:
    """The module each process that runs this package starts from: the two
    images' CMD and the local launcher (local system agents are served from
    the launcher's process)."""
    found = []
    for name in ("Dockerfile", "Dockerfile.runtime"):
        cmd = [line for line in (ADT / name).read_text().splitlines() if line.startswith("CMD ")]
        assert len(cmd) == 1, (name, cmd)
        argv = json.loads(cmd[0][4:])
        target = argv[argv.index("-m") + 1] if "-m" in argv else next(
            a for a in argv if a.startswith("agents."))
        found.append(target.split(":")[0] if ":" in target else target)
    launcher = (ADT.parent / "scripts" / "local_dev.sh").read_text()
    found += re.findall(r"uvicorn\"? (agents\.[\w.]+):app", launcher)
    return found


_EARLY_CLIENTS = """
import importlib.util, sys
import boto3, botocore.session
creds = dict(region_name='us-east-1', aws_access_key_id='t', aws_secret_access_key='t')
early = [boto3.client('secretsmanager', **creds),
         botocore.session.get_session().create_client('secretsmanager', **creds)]
early_session = boto3.Session(**creds)
assert 'agents' not in sys.modules
assert importlib.util.find_spec(sys.argv[1]) is not None   # what launching it imports first
assert sys.argv[1] not in sys.modules                      # before any line of it runs
from agents import lease_guard, reset_lease
clients = early + [early_session.client('secretsmanager')]
sent = []
def _send(request, **_kwargs):
    sent.append(request.url)
    raise RuntimeError('not sent in a test')
for client in clients:
    client.meta.events.register('before-send', _send)
def _lost():
    raise reset_lease.LeaseLostError('lost')
blocked = 0
with lease_guard.write_guard(_lost):
    for client in clients:
        try:
            client.delete_secret(SecretId='x', RecoveryWindowInDays=7)
        except reset_lease.LeaseLostError:
            blocked += 1
        except RuntimeError:
            pass                                       # it reached the wire
print(json.dumps({'blocked': blocked, 'sent': len(sent)}))
"""


def test_a_client_made_before_the_package_is_imported_is_checked():
    """Every process the package runs as starts from one of its modules, so
    launching it installs the check before any line of it runs; a client
    its launcher (or an instrumentation layer) made earlier is checked too."""
    entrypoints = sorted(set(_entrypoint_modules()))
    assert entrypoints == ["agents.runtime_app", "agents.server"]
    for entrypoint in entrypoints:
        proc = subprocess.run(  # nosec B603 - fixed argv, our own interpreter
            [sys.executable, "-c", "import json\n" + _EARLY_CLIENTS, entrypoint],
            cwd=str(ADT), capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(ADT), "AWS_EC2_METADATA_DISABLED": "true"})
        assert proc.returncode == 0, (entrypoint, proc.stderr[-2000:])
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result == {"blocked": 3, "sent": 0}, (entrypoint, result)


# ── provision side effects that are not AWS API calls ──────────────────────

@pytest.fixture
def warmup(manager, monkeypatch):
    posts: list = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, _n=None):
            return b"{}"

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, **_kw: posts.append(req.full_url) or _Resp())
    monkeypatch.setattr(manager, "_get_m2m_token", lambda _region: "token")
    return posts


def test_the_warmup_post_is_not_sent_once_the_lease_is_lost(provision, manager, warmup, clock):
    url = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/r-1/invocations"

    def _two_warmups():
        manager._warmup_runtime(url, "us-east-1")         # lease held: sent
        clock.advance(LOSE)
        manager._warmup_runtime(url, "us-east-1")
    record = provision(_two_warmups)
    assert warmup == [url]
    _lost_provision(record)


class _Server:
    """A started local system agent server that keeps running."""

    def __init__(self, system_id="", host="", port=0, build=None, stop_wait=0):
        self.system_id, self.host, self.port, self.build = system_id, host, port, build
        self.stopped = False

    def start(self, _timeout):
        pass

    def is_running(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


@pytest.fixture
def local(monkeypatch):
    started: list = []
    import agents.business_context as bc
    monkeypatch.setattr(bc, "read_business_context", lambda _sid: None)
    monkeypatch.setattr(am, "LocalAgentServer",
                        lambda *args: started.append(_Server(*args)) or started[-1])
    mgr = am.AgentManager(mode="local")
    monkeypatch.setattr(mgr, "_port_is_free", lambda _port: True)
    return mgr, started


def _start(mgr):
    return mgr._start_local(SID, "Line MES", "mes", "prompt", "tables", {"host": "h"})


def test_no_local_agent_starts_once_the_lease_is_lost(provision, local, clock):
    mgr, started = local
    record = provision(lambda: clock.advance(LOSE) or _start(mgr))
    assert started == [] and mgr._used_ports == set() and SID not in mgr._agents
    _lost_provision(record)


def test_a_local_agent_listens_on_loopback_only(provision, local):
    mgr, started = local
    record = provision(lambda: _start(mgr))
    assert record["status"] == "done", record
    assert started[0].host == started[0].build.keywords["host"] == "127.0.0.1"
    assert mgr._agents[SID].server is started[0]
    assert not started[0].stopped                     # a provision that ends well keeps it


def test_a_local_agent_is_stopped_when_a_later_check_finds_the_lease_lost(provision, local, clock):
    mgr, started = local

    def _start_then_lose():
        _start(mgr)
        clock.advance(LOSE)
        lease_guard.check_write_guard()                   # the provision's next step
    record = provision(_start_then_lose)
    assert started[0].stopped
    assert SID not in mgr._agents and mgr._used_ports == set()
    _lost_provision(record)


# ── the local agent of a real provision, whatever stops it ─────────────────

#: The real server class (the ``local`` fixture replaces it).
_REAL_SERVER = am.LocalAgentServer
MAPPINGS = [{"table": "orders", "field": "orders.qty", "relation_kind": "table",
             "data_type": "int"}]


@pytest.fixture
def real_provision(table, clock, local, monkeypatch):
    """A provision job through the real ``_do_provision`` nesting (its claim's
    guard, then ``_do_provision_integrity``'s ``_provision_in_flight`` guard),
    whose writes start the local agent; the post-provision steps are real and
    write the local graph, where the first write loses the lease. Returns
    ``run() -> (record, graph writes)``."""
    import agents.onboard as onboard
    from unittest.mock import MagicMock
    from tests.unit.test_one_reset_lease import Replica
    mgr, _started = local
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision_writes", lambda body, dry_run=False: {
        "status": "provisioned", "mcp_url": mgr.start_agent(
            SID, "Line MES", "mes", "prompt", "tables", conn_params={"host": "h"})})
    monkeypatch.setattr(onboard, "_mapping_generation_bumped", lambda _sid: True)
    monkeypatch.setattr(db, "_graph_backend", lambda: "local")
    writes: list = []

    def _graph(cypher, _params=None):
        writes.append(cypher)
        if len(writes) == 1:
            clock.advance(LOSE)                       # lost during the first post-provision write
        return []
    monkeypatch.setattr(db, "_memgraph_run", _graph)

    def _run():
        assert onboard._do_provision is onboard._do_provision_integrity
        with Replica(table):
            claim = onboard._claim_provision(SID)
            onboard._run_provision_job("job-p", MagicMock(system_id=SID, mappings=MAPPINGS,
                                                          connection_type="PostgreSQL"),
                                       claim=claim)
            return onboard._provision_jobs["job-p"], writes
    return _run


def test_a_local_agent_is_stopped_when_a_post_provision_step_loses_the_lease(
        real_provision, local):
    mgr, started = local
    record, writes = real_provision()
    assert len(writes) == 1 and "relation_kind" in writes[0]   # the next write was refused
    assert len(started) == 1 and started[0].stopped
    assert SID not in mgr._agents and mgr._used_ports == set()
    _lost_provision(record)


def test_a_stopped_agent_frees_its_port_even_when_its_thread_does_not_end(local):
    mgr, _started = local

    class _Unending(_Server):
        def stop(self):
            raise TimeoutError("Local agent did not stop")
    server = _Unending()
    port = mgr._allocate_port()
    mgr._agents[SID] = am.AgentInstance(SID, "Line MES", "mes", "p", "t", "u", port=port,
                                       server=server)
    with pytest.raises(TimeoutError):
        mgr.stop_agent(SID)
    assert SID not in mgr._agents and mgr._used_ports == set()


@pytest.mark.enable_socket   # a real 127.0.0.1 server, no external network
def test_a_real_server_with_a_request_in_flight_is_stopped_on_a_lost_lease(local, lease):
    """A lost lease stops a real in-process agent server even while one of
    its requests never finishes: it is cancelled after STOP_WAIT_SECONDS."""
    import threading

    import httpx
    from mcp.server.fastmcp import FastMCP
    mgr, _started = local
    mgr.STOP_WAIT_SECONDS = 0.5
    port = mgr._allocate_port()
    hanging = threading.Event()

    def _build():
        mcp = FastMCP(name="adt-mes", host="127.0.0.1", port=port, stateless_http=True)

        @mcp.tool(name="hang")
        async def hang() -> str:
            import asyncio
            import time

            def _block():                              # blocking, as a connector call
                hanging.set()
                time.sleep(30)
            await asyncio.to_thread(_block)
            return "never"
        return mcp
    server = _REAL_SERVER(SID, "127.0.0.1", port, _build, mgr.STOP_WAIT_SECONDS)
    server.start(30)
    mgr._agents[SID] = am.AgentInstance(SID, "Line MES", "mes", "p", "t", "u", port=port,
                                       server=server)

    def _ask():
        try:
            httpx.post(f"http://127.0.0.1:{port}/mcp", timeout=30, headers={
                "accept": "application/json, text/event-stream"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "hang", "arguments": {}}})
        except httpx.HTTPError:
            pass
    asking = threading.Thread(target=_ask, daemon=True)
    asking.start()
    assert hanging.wait(10)                               # its worker is blocked
    with lease.bound():
        lease_guard.release_on_loss(lambda: mgr._stop_local_server(SID, server))
        lease.lose()
        with pytest.raises(reset_lease.LeaseLostError):
            lease_guard.check_write_guard()
    assert not server.is_running()
    assert SID not in mgr._agents and mgr._used_ports == set()
    assert mgr._port_is_free(port)


def test_a_server_that_replaced_the_provisions_agent_is_never_stopped(provision, local, clock):
    """The provision's agent was stopped and another start (outside this
    provision) registered a new server; the provision's lost lease stops
    neither the new server nor its registration."""
    import contextvars
    mgr, started = local

    def _replaced_then_lose():
        _start(mgr)
        mgr.stop_agent(SID)
        contextvars.Context().run(_start, mgr)          # not part of this provision
        clock.advance(LOSE)
        lease_guard.check_write_guard()
    record = provision(_replaced_then_lose)
    first, replacement = started
    assert first.stopped and not replacement.stopped
    assert mgr._agents[SID].server is replacement and mgr._used_ports == {mgr._agents[SID].port}
    _lost_provision(record)


# ── which guard an undo belongs to ─────────────────────────────────────────

def test_an_undo_registered_in_a_nested_guard_of_the_same_check_runs_on_a_later_loss(lease):
    undone: list = []
    with lease.bound():
        with lease.bound():                               # the same operation, nested
            lease_guard.release_on_loss(lambda: undone.append("agent"))
        assert undone == []                               # nothing lost yet
        lease.lose()
        with pytest.raises(reset_lease.LeaseLostError):
            lease_guard.check_write_guard()               # a later step
        assert undone == []                               # undone on leaving the operation
    assert undone == ["agent"]


def test_a_guard_of_another_check_keeps_its_own_undos(lease):
    undone: list = []

    def _other_lost():
        raise reset_lease.LeaseLostError("other")
    with lease.bound():
        lease_guard.release_on_loss(lambda: undone.append("outer"))
        with lease_guard.write_guard(_other_lost):        # another operation
            lease_guard.release_on_loss(lambda: undone.append("inner"))
            with pytest.raises(reset_lease.LeaseLostError):
                lease_guard.check_write_guard()
        assert undone == ["inner"]
        lease_guard.check_write_guard()                   # the outer lease is still held
    assert undone == ["inner"]


def test_an_undo_runs_once_when_the_lease_is_lost_inside_a_nested_guard(lease):
    undone: list = []
    with lease.bound():
        with pytest.raises(reset_lease.LeaseLostError):
            with lease.bound():
                lease_guard.release_on_loss(lambda: undone.append("agent"))
                lease.lose()
                lease_guard.check_write_guard()
        assert undone == []                               # not on leaving the nested guard
    assert undone == ["agent"]                            # once, on leaving the operation


class _Replacing(dict):
    """An agent registry where another start registers *replacement* right
    after the stopping code has looked its system up."""

    def __init__(self, *args, replacement=None):
        super().__init__(*args)
        self._replacement = replacement

    def get(self, key, default=None):
        current = super().get(key, default)
        if self._replacement is not None:
            self[key], self._replacement = self._replacement, None
        return current


def test_the_checked_server_is_stopped_never_one_registered_since(local):
    mgr, _started = local
    first, second = _Server("first"), _Server("second")
    port = mgr._allocate_port()
    mine = am.AgentInstance(SID, "Line MES", "mes", "p", "t", "u", port=port, server=first)
    theirs = am.AgentInstance(SID, "Line MES", "mes", "p", "t", "u", port=port + 1,
                              server=second)
    mgr._used_ports.add(port + 1)
    mgr._agents = _Replacing({SID: mine}, replacement=theirs)
    mgr._stop_local_server(SID, first)
    assert first.stopped and not second.stopped
    assert mgr._agents[SID] is theirs and mgr._used_ports == {port + 1}
