# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The onboarding chat's provision_system is the wizard's provision.

The chat's discover, map_fields and resolve_identities keep their state in the
durable onboarding session; the wizard's provision refuses a mapping without
the schema provenance its own discover records. The app's provision_system
re-reads the system's schema with the wizard's discover, joins the session's
mappings to it the way the wizard's map job does, and runs the wizard's
provision job (its locks, provision count and job record) on the body the
wizard would send. These tests run both paths on the same system, offline:
the graph, the agent manager, Bedrock, the MCP server and the session table
are fakes; the provision code itself is real.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import threading
import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents import connection_lock as cl  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402
from agents.job_store import JobStore  # noqa: E402
from agents.models import DiscoverRequest, ProvisionRequest  # noqa: E402
from agents.onboarding_session import OnboardingSessionStore  # noqa: E402
from tests.unit._chat_removal import removed_by_the_chat  # noqa: E402

SID = "line-mcp"
URL = "https://mcp.example.com/mcp"
TABLES = [{"name": "get_tags", "row_count": 3, "columns": [
    {"name": "Ent/Site/Line1/StationA/Down", "data_type": "bool"},
    {"name": "Ent/Site/Line1/StationA/Good", "data_type": "int"},
    {"name": "Ent/Site/Line1/StationB/Speed", "data_type": "float"},
]}]
CONCEPTS = ["downtime", "quality-metric", "sensor-reading", "equipment-id"]

#: How long a test waits for a job thread to reach a step, or a step for the
#: test: generous, for a slow, loaded runner (a passing run never waits it).
JOB_WAIT = 60.0


def _entry() -> dict:
    return {"id": SID, "name": "Line MCP", "system_type": "SCADA", "plant": "Plant A",
            "connection_type": "MCP Server", "connection_string": URL, "schema": "",
            "status": "registered"}


# ── fakes ───────────────────────────────────────────────────────────────────

class _SessionTable:
    """The DynamoDB onboarding-session table: just enough of the API, with
    its conditional semantics, for the real OnboardingSessionStore."""

    def __init__(self):
        self.items: dict = {}
        self.lock = threading.Lock()

    @staticmethod
    def _fail(op):
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                     "Message": "condition"}}, op)

    def get_item(self, Key):
        with self.lock:
            item = self.items.get(Key["system_id"])
            return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        with self.lock:
            if Item["system_id"] in self.items:
                self._fail("PutItem")
            self.items[Item["system_id"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ConditionExpression,
                    ExpressionAttributeNames, ExpressionAttributeValues):
        with self.lock:
            item = self.items.get(Key["system_id"])
            if not item or item.get("status") != ExpressionAttributeValues[":from"]:
                self._fail("UpdateItem")
            for clause in UpdateExpression[len("SET "):].split(", "):
                name, value = clause.split(" = ")
                item[ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]

    def delete_item(self, Key, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None):
        with self.lock:
            item = self.items.get(Key["system_id"])
            if ConditionExpression and (
                    not item or item.get("status") != ExpressionAttributeValues[":expected"]):
                self._fail("DeleteItem")
            self.items.pop(Key["system_id"], None)
            return {"Attributes": dict(item)} if item and ReturnValues == "ALL_OLD" else {}


class _Resource:
    def __init__(self, table):
        self._table = table

    def Table(self, _name):  # noqa: N802 - boto3 resource API
        return self._table


class _AgentManager:
    def __init__(self):
        self._agents: dict = {}
        self.started: list = []

    def start_agent(self, **kwargs):
        self.started.append(kwargs)
        return "mcp://runtime/line-mcp"

    def get_mcp_url(self, _sid):
        return "mcp://runtime/line-mcp"


def _chat_map_answer(prompt: str) -> str:
    """The chat map_fields model: one mapping per listed column, the table
    echoed as listed (map_fields lists MCP tags by equipment station)."""
    import json
    block = prompt.split("Tables and columns:\n", 1)[1].split("\n\n", 1)[0]
    rows = []
    for line in block.splitlines():
        table, _, cols = line.strip().partition(": ")
        for col in [c for c in cols.split(", ") if c]:
            rows.append({"field": f"{table}.{col}", "table": table,
                         "mappings": [_concept_for(col)], "reasoning": "tag suffix"})
    return json.dumps(rows)


def _concept_for(column: str) -> dict:
    if column.endswith("Down"):
        return {"concept": "downtime", "role": "identifier", "confidence": 0.9}
    if column.endswith("Good"):
        return {"concept": "quality-metric", "role": "measure", "confidence": 0.9}
    return {"concept": "sensor-reading", "role": "measure", "confidence": 0.85}


def _set_lifecycle(monkeypatch, name: str, value) -> None:
    """Set ``agents.onboard_chat_lifecycle.<name>`` for the test, when that
    module exists (a run on a tree without it then fails on the behaviour)."""
    try:
        import agents.onboard_chat_lifecycle as lifecycle
    except ImportError:
        return
    monkeypatch.setattr(lifecycle, name, value, raising=False)


class World:
    """One app process: catalog, graph writes, agent manager, job stores and
    the onboarding-session table, all fresh, with the post-provision steps
    recorded."""

    def __init__(self, monkeypatch, check_wait: float = 0.0):
        self.mp = monkeypatch
        _set_lifecycle(monkeypatch, "CHECK_WAIT_SECONDS", check_wait)
        _set_lifecycle(monkeypatch, "_CHAT_JOBS", {})
        self.writes: list = []
        self.role_passes = 0
        self.after_job: list = []
        self.discovers = 0
        self.discover_tables = copy.deepcopy(TABLES)
        self.discover_error = ""
        self.job_bodies: list = []
        self.table = _SessionTable()
        self.store = OnboardingSessionStore(table_name="t", dynamodb_resource=_Resource(self.table))
        self.manager = _AgentManager()
        self.added_agents: list = []
        self.removed_nodes: set = set()
        self.catalog = SystemCatalog([_entry()])
        self._install()

    def _install(self):
        mp = self.mp
        for mod in (onboard, oa):
            mp.setattr(mod, "catalog", self.catalog)
        mp.setattr(oa, "_store", lambda: self.store)
        mp.setattr(onboard, "_provision_jobs", JobStore("provision", table=None))
        mp.setattr(onboard, "_schema_provenance", JobStore("schema-provenance", table=None))
        mp.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
        mp.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
        mp.setattr(onboard, "neptune_write", self._write)
        mp.setattr(onboard, "neptune_query", lambda *a, **k: [])
        mp.setattr(oa, "neptune_query", lambda *a, **k: [])
        mp.setattr(oa, "neptune_write", self._write)
        mp.setattr(onboard, "bedrock_call", lambda *a, **k: "{}")
        mp.setattr(oa, "bedrock_call", lambda prompt, *a, **k: _chat_map_answer(prompt))
        mp.setattr(onboard, "pg", lambda *a, **k: {"rows": [], "columns": []})
        mp.setattr(onboard, "get_all_concepts", lambda: list(CONCEPTS))
        mp.setattr(onboard, "_agent_manager", self.manager)
        mp.setattr(onboard, "add_system_agent", lambda *a, **k: self.added_agents.append(a))
        mp.setattr(onboard, "_detect_internal_joinpaths", lambda *a, **k: [])
        mp.setattr(onboard.mqtt_subscriber, "start_subscriber", lambda *a, **k: None)
        mp.setattr(oa, "_discover_mcp", self._discover)
        mp.setattr(cr, "_fetch_node_config", self._node)
        mp.setattr(cr, "write_connection", lambda *a, **k: "")
        import agents.catalog as catalog_mod
        mp.setattr(catalog_mod, "_get_highbyte_url", lambda: "")
        mp.setattr(catalog_mod, "_get_highbyte_token", lambda: "")
        mp.setattr(catalog_mod, "concepts_prompt_block", lambda: "- downtime: stops")
        mp.setattr(catalog_mod, "concept_hints_block", lambda: "  downtime: stops")
        import agents.model_config as model_config
        mp.setattr(model_config, "get_configured_models",
                   lambda: {"agent": "test-agent-model", "orchestrator": "test"})
        import agents.self_learning as self_learning
        mp.setattr(self_learning, "get_accepted_for_system", lambda *a, **k: [])
        mp.setattr(self_learning, "accepted_user_concept_mappings", lambda *a, **k: {})
        import agents.metamodel as metamodel
        mp.setattr(metamodel, "write_metamodel", lambda: self.after_job.append("metamodel"))
        for name in ("_seed_demo_curated_annotations", "_replay_accepted_field_annotations",
                     "_persist_equipment_value_mappings"):
            mp.setattr(onboard, name, lambda *a, _n=name, **k: self.after_job.append(_n) or 0)
        import agents.asset_role_pass as role_pass
        mp.setattr(role_pass, "run_after_provision", self._role_pass)
        import agents.coverage_gap_analyzer as gap
        import agents.schema_drift as drift
        mp.setattr(drift, "sweep_resolved_drift", lambda sid, _p: self.after_job.append(("drift", sid)))
        mp.setattr(gap, "run_coverage_gap_check", lambda ids: self.after_job.append(("gap", ids)))
        mp.setattr(onboard, "_post_provision_mining",
                   lambda sid: self.after_job.append(("mining", sid)) or {})
        mp.setattr(onboard, "_post_provision_schema_shape",
                   lambda job_id, sid: self.after_job.append(("shape", sid)))
        real_steps = onboard._run_provision_job_steps

        def _steps(job_id, body):
            self.job_bodies.append(body)
            return real_steps(job_id, body)
        mp.setattr(onboard, "_run_provision_job_steps", _steps)
        import boto3

        def _no_boto(*_a, **_k):
            raise RuntimeError("boto3 disabled in unit tests")
        mp.setattr(boto3, "client", _no_boto)
        with cl._REGISTRY_LOCK:
            cl._LOCKS.clear()
            cl._HOLDERS.clear()

    def _write(self, cypher, params=None):
        self.writes.append((cypher, dict(params or {})))
        return True

    def _node(self, sid):
        """The System node the graph holds for ``sid`` (no connection
        config: the connection stays the registration's): one once the
        provision wrote it, until a removal deleted it."""
        written = any(p.get("sid") == sid for p in self.system_node_writes())
        return ({"id": sid, "status": "onboarded"}
                if written and sid not in self.removed_nodes else {})

    def _role_pass(self):
        self.role_passes += 1

    def _discover(self, _entry, system_id=""):
        self.discovers += 1
        if self.discover_error:
            return {"error": self.discover_error}
        return {"tables": copy.deepcopy(self.discover_tables)}

    def share_sampled_values(self, values=("ST-101", "ST-102", "ST-103")) -> None:
        """Every field's value sample answers ``values``: a rule the chat's
        resolve should keep then has measured shared values, which its
        evidence gate (the wizard's) asks of a rule whose field names do not
        look like identifiers."""
        import agents.resolve_evidence as resolve_evidence
        self.mp.setattr(resolve_evidence, "sample_field_values", lambda **_kw: list(values))

    # -- views --
    def system_node_writes(self) -> list:
        return [p for c, p in self.writes if "s.status = 'onboarded'" in c and "s.agent_model" in c]

    def cyphers(self) -> list:
        return [c for c, _p in self.writes]


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


class _AgentTools:
    """The chat agent's provision_system / reset_system, as the agent gets
    them (local mode: the in-process tool list)."""

    def __getattr__(self, name):
        from agents.config import get_config
        before = os.environ.get("AGENT_MODE")
        os.environ["AGENT_MODE"] = "local"
        get_config.cache_clear()
        try:
            tools = oa._get_onboard_tools(None)
        finally:
            if before is None:
                os.environ.pop("AGENT_MODE", None)
            else:
                os.environ["AGENT_MODE"] = before
            get_config.cache_clear()
        return {t.tool_name: t for t in tools}[name]


def _agent_tools():
    return _AgentTools()


def _chat_until_resolved():
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    assert "error" not in oa.resolve_identities(SID)


def _wait_for(pred, timeout=JOB_WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def _chat_provision_answer() -> dict:
    """The chat's provision as the model sees it: the first call's answer
    when it is not ``provisioning`` (refused at once), else, once its job
    ended (the provision claim released), the next call's: a refusal the job
    found, or how the provision ended."""
    first = _agent_tools().provision_system(SID)
    if first.get("status") != "provisioning":
        return first
    _wait_for(lambda: not onboard._provision_running(SID))
    return _agent_tools().provision_system(SID)


def _chat_provision_to_the_end() -> dict:
    """The chat's provision started and checked until it ended, and its job
    thread done (the session is finalized before the job releases its
    claim)."""
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    finished = _wait_for(lambda: (lambda r: r if r.get("status") != "provisioning" else None)(
        _agent_tools().provision_system(SID)))
    _wait_for(lambda: not onboard._provision_running(SID))
    return finished


def _wizard_provision(world, confirmed_rows: list) -> dict:
    """The wizard's requests for the same system: discover (catalog values,
    as buildDiscoverBody sends them), the map job's provenance attach on the
    confirmed rows, then POST /provision (buildProvisionBody) and its poll."""
    entry = world.catalog.get(SID)
    disc = asyncio.run(onboard.onboard_discover(DiscoverRequest(
        system_id=SID, connection_string=entry["connection_string"],
        connection_type=entry["connection_type"], connection_config={})))
    rows = onboard._attach_map_provenance(
        {"mappings": copy.deepcopy(confirmed_rows)},
        onboard._mapping_schema_metadata(disc["tables"]))["mappings"]
    started = asyncio.run(onboard.onboard_provision(ProvisionRequest(
        system_id=SID, system_name=entry["name"], plant=entry["plant"],
        schema=disc.get("schema_name") or entry["schema"], rules=[], mappings=rows,
        connection_type=entry["connection_type"], connection_string=entry["connection_string"],
        connection_config={}, rules_from_checkpoint=True,  # as buildProvisionBody sends it,
        resolve_job_id=onboard._bind_resolve_rules(SID, []))))  # with its Resolve job's id
    return _wait_for(lambda: (lambda r: r if r.get("status") != "running" else None)(
        asyncio.run(onboard.onboard_provision_status(started["job_id"]))))


def _normalized(body) -> dict:
    dumped = body.model_dump()
    for m in dumped["mappings"]:
        assert m.pop("schema_revision")
    assert dumped.pop("resolve_job_id")  # each run's own binding
    return dumped


# ── the same provision as the wizard, on the same system ────────────────────

def test_a_chat_provision_ends_exactly_as_a_wizard_provision(monkeypatch):
    chat = World(monkeypatch)
    _chat_until_resolved()
    session_rows = chat.store.get_session(SID)["mappings"]
    finished = _chat_provision_to_the_end()

    assert finished["status"] == "onboarded", finished
    assert chat.catalog.get(SID)["status"] == "onboarded"
    assert chat.store.get_session(SID)["status"] == "onboarded"
    assert len(chat.manager.started) == 1 and chat.added_agents
    assert chat.role_passes == 1
    assert len(chat.system_node_writes()) == 1

    wizard = World(monkeypatch)
    wizard_done = _wizard_provision(wizard, session_rows)
    assert wizard_done["status"] == "done", wizard_done

    assert _normalized(chat.job_bodies[0]) == _normalized(wizard.job_bodies[0])
    assert chat.system_node_writes() == wizard.system_node_writes()
    assert chat.manager.started == wizard.manager.started
    assert chat.added_agents == wizard.added_agents
    assert chat.cyphers() == wizard.cyphers()
    assert chat.after_job == wizard.after_job
    assert chat.role_passes == wizard.role_passes == 1
    for key in ("tool_name", "neptune_nodes_created", "identity_rules_created",
                "join_paths_written", "value_mappings_written"):
        assert finished[key] == wizard_done[key], key


def test_the_chat_provision_reads_the_schema_with_the_wizards_discover(world):
    _chat_until_resolved()
    assert world.discovers == 1                       # the chat's own discover
    _chat_provision_to_the_end()
    assert world.discovers == 2                       # the wizard's discover, in the app
    body = world.job_bodies[0]
    assert {m["field"] for m in body.mappings} == {
        f"get_tags.{c['name']}" for c in TABLES[0]["columns"]}
    assert all(m["data_type"] and m["relation_kind"] for m in body.mappings)
    assert body.schema == URL                          # the discover's schema_name, as the wizard sends


# ── the chat can check on the provision it started ──────────────────────────

def test_a_second_provision_call_answers_the_running_provision_and_starts_nothing(world):
    _chat_until_resolved()
    release = _block_the_job(world)
    try:
        first = _agent_tools().provision_system(SID)
        second = _agent_tools().provision_system(SID)
    finally:
        release()
    assert first["status"] == second["status"] == "provisioning"
    assert second.get("job_id") == first["job_id"]
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(world.job_bodies) == 1


def _two_first_calls_together(world) -> list:
    both, real = threading.Barrier(2, timeout=JOB_WAIT), world._discover

    def _together(entry, system_id=""):
        both.wait()
        return real(entry, system_id=system_id)
    world.mp.setattr(oa, "_discover_mcp", _together)
    out: list = []
    provision = _agent_tools().provision_system
    threads = [threading.Thread(target=lambda: out.append(provision(SID))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOB_WAIT)
    return out


def test_a_second_call_while_the_first_ones_job_reads_the_schema_answers_it(world):
    _chat_until_resolved()
    out: list = []
    provision = _agent_tools().provision_system
    gate, real = threading.Event(), world._discover

    def _slow(entry, system_id=""):
        gate.wait(JOB_WAIT)
        return real(entry, system_id=system_id)
    world.mp.setattr(oa, "_discover_mcp", _slow)
    first = threading.Thread(target=lambda: out.append(provision(SID)))
    first.start()
    _wait_for(lambda: onboard._provision_running(SID))
    second = provision(SID)
    gate.set()
    first.join(JOB_WAIT)
    assert out[0]["status"] == "provisioning" and out[0].get("job_id"), out
    assert second.get("status") == "provisioning", second
    assert second.get("job_id") == out[0]["job_id"], (out, second)
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(world.job_bodies) == 1


def test_the_session_claim_starts_one_provision_across_app_processes(world):
    """Two app processes do not share the in-process claim; the session's
    ``resolved -> provisioning`` claim still starts one provision."""
    _chat_until_resolved()

    class _OwnProcess:
        """A claim another app process holds (its own provision lease too)."""

        def release(self):
            pass

        def guard(self):
            return contextlib.nullcontext()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False
    world.mp.setattr(onboard, "_claim_provision", lambda sid: _OwnProcess())
    out = _two_first_calls_together(world)
    assert sorted(r["status"] for r in out) == ["provisioning", "provisioning"], out
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(world.job_bodies) == 1


def test_the_finished_answer_carries_what_the_provision_wrote(world):
    _chat_until_resolved()
    finished = _chat_provision_to_the_end()
    assert finished["tool_name"] == "query_line_mcp"
    assert finished["onboarding_id"] == SID
    again = _agent_tools().provision_system(SID)
    assert again["status"] == "onboarded" and again["tool_name"] == "query_line_mcp"


def test_only_the_finished_provision_becomes_a_provision_card(world):
    _chat_until_resolved()
    started = _agent_tools().provision_system(SID)
    assert onboard.onboard_checkpoint_event("provision_system", started) is None
    finished = _wait_for(lambda: (lambda r: r if r.get("status") != "provisioning" else None)(
        _agent_tools().provision_system(SID)))
    card = onboard.onboard_checkpoint_event("provision_system", finished)
    assert card["data"]["step"] == "provision"
    assert onboard.onboard_checkpoint_event("provision_system", {"error": "x"}) is None


def test_a_check_waits_for_a_running_provision_to_finish(monkeypatch):
    world = World(monkeypatch, check_wait=JOB_WAIT)
    _chat_until_resolved()
    release = _block_the_job(world)
    started = _agent_tools().provision_system(SID)
    assert started["status"] == "provisioning"
    assert world.entered.wait(JOB_WAIT)
    threading.Timer(0.3, release).start()
    t0 = time.monotonic()
    checked = _agent_tools().provision_system(SID)
    assert checked["status"] == "onboarded", checked     # one check, no answer in between
    assert 0.2 < time.monotonic() - t0 < JOB_WAIT


def test_a_check_on_a_provision_still_running_answers_after_the_wait(monkeypatch):
    world = World(monkeypatch, check_wait=0.3)
    _chat_until_resolved()
    release = _block_the_job(world)
    try:
        _agent_tools().provision_system(SID)
        assert world.entered.wait(JOB_WAIT)
        t0 = time.monotonic()
        checked = _agent_tools().provision_system(SID)
        waited = time.monotonic() - t0
    finally:
        release()
    assert checked["status"] == "provisioning" and 0.25 < waited < JOB_WAIT / 2, (checked, waited)


# ── refused like the wizard, with nothing claimed ───────────────────────────

def _block_the_job(world):
    entered, gate = threading.Event(), threading.Event()

    def _shape(job_id, sid):
        entered.set()
        gate.wait(JOB_WAIT)
    world.mp.setattr(onboard, "_post_provision_schema_shape", _shape)
    world.entered = entered
    return gate.set


def test_a_removal_during_a_chat_provision_is_refused_like_during_a_wizard_provision(world):
    _chat_until_resolved()
    release = _block_the_job(world)
    try:
        _agent_tools().provision_system(SID)
        assert world.entered.wait(JOB_WAIT)
        assert world.catalog.get(SID)["status"] == "onboarded"   # the connection phase ran
        with pytest.raises(onboard.HTTPException) as card:
            asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=True))
        chat = _agent_tools().reset_system(SID)
    finally:
        release()
    assert card.value.status_code == 409 and "being provisioned" in card.value.detail
    assert chat.get("reason") == "removal_refused" and "being provisioned" in chat["error"]
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert onboard._RESET_JOBS.get(SID) is None


def test_a_removal_while_the_schema_is_read_again_is_refused(world):
    """The chat provision counts as running from its first read, before the
    connection phase has onboarded the system."""
    _chat_until_resolved()
    reading, go = threading.Event(), threading.Event()
    real = world._discover

    def _slow(entry, system_id=""):
        reading.set()
        go.wait(JOB_WAIT)
        return real(entry, system_id=system_id)
    world.mp.setattr(oa, "_discover_mcp", _slow)
    out: dict = {}
    provision = _agent_tools().provision_system
    t = threading.Thread(target=lambda: out.update(provision(SID)))
    t.start()
    try:
        assert reading.wait(JOB_WAIT)
        refused = _agent_tools().reset_system(SID)
    finally:
        go.set()
        t.join(JOB_WAIT)
    assert refused.get("reason") == "removal_refused" and "being provisioned" in refused["error"]
    assert out.get("status") == "provisioning", out
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")


def test_a_chat_provision_is_refused_while_the_wizard_provisions_the_system(world):
    _chat_until_resolved()
    with onboard._provision_in_flight(SID):
        res = _agent_tools().provision_system(SID)
    assert res.get("reason") == "provisioning", res
    assert world.store.get_session(SID)["status"] == "resolved"
    assert world.job_bodies == []


def test_a_chat_provision_is_refused_while_the_system_is_being_removed(world):
    _chat_until_resolved()
    onboard._RESET_JOBS[SID] = {"state": "running", "full": True}
    res = _agent_tools().provision_system(SID)
    assert res.get("reason") == "reset_in_progress", res
    assert world.store.get_session(SID)["status"] == "resolved"
    assert world.discovers == 1 and world.job_bodies == []


def test_nothing_is_claimed_when_the_source_cannot_be_read(world):
    _chat_until_resolved()
    world.discover_error = "Failed to connect to MCP server"
    res = _chat_provision_answer()
    assert "error" in res and "Failed to connect" in res["error"], res
    assert world.store.get_session(SID)["status"] == "resolved"
    assert world.job_bodies == [] and not onboard._provision_running(SID)


def test_nothing_is_claimed_when_the_source_no_longer_has_the_mapped_fields(world):
    _chat_until_resolved()
    world.discover_tables = [{"name": "get_tags", "row_count": 1,
                              "columns": [{"name": "Other/Tag", "data_type": "int"}]}]
    res = _chat_provision_answer()
    assert res.get("reason") == "schema_changed", res
    assert world.store.get_session(SID)["status"] == "resolved"
    assert world.job_bodies == [] and not onboard._provision_running(SID)


def test_a_session_that_is_not_resolved_is_refused(world):
    assert "error" not in oa.discover(SID)
    res = _agent_tools().provision_system(SID)
    assert res == {"error": "No resolved mappings. Call resolve_identities first."}
    assert world.discovers == 1


# ── a provision that did not finish is reported, and can be started over ────

def test_a_failed_provision_is_reported_with_its_reason(world):
    _chat_until_resolved()
    world.mp.setattr(cl, "CONNECTION_LOCK_TIMEOUT_SECONDS", 0.1)
    held, release = threading.Event(), threading.Event()

    def _hold():
        with cl.connection_lock(SID):
            held.set()
            release.wait(JOB_WAIT)
    t = threading.Thread(target=_hold)
    t.start()
    assert held.wait(JOB_WAIT)
    try:
        finished = _chat_provision_to_the_end()
    finally:
        release.set()
        t.join(JOB_WAIT)
    assert finished["status"] == "failed"
    assert finished["error"] == onboard._CONNECTION_BUSY_ERROR
    assert world.store.get_session(SID)["status"] == "failed"
    cleared = _agent_tools().reset_system(SID)
    assert cleared.get("status") == "session_cleared", cleared


def test_a_provision_the_app_lost_in_a_restart_is_reported_failed(world):
    _chat_until_resolved()
    world.store.claim_provisioning(SID, provision_job_id="gone1234")
    res = _agent_tools().provision_system(SID)
    assert res["status"] == "failed" and res.get("reason") == "provision_interrupted", res
    assert world.store.get_session(SID)["status"] == "failed"


# ── the chat's reset is the card's removal; start-over stays ────────────────

def _fake_cascade(world):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    deletes: list = []

    def _delete_graph(sid):
        deletes.append(sid)
        world.removed_nodes.add(sid)
    for target, value in (("_reset_cleanup_agentcore_sync", cleanup),
                          ("_retire_or_incomplete", lambda sid: 0),
                          ("_reset_delete_graph_artifacts_invalidating", _delete_graph),
                          ("remove_system_agent", MagicMock()),
                          ("_reregister_demo_system", lambda sid: False)):
        world.mp.setattr(onboard, target, value)
    return cleanup, deletes


def test_chat_reset_of_a_chat_onboarded_system_is_the_cards_removal(monkeypatch):
    chat = World(monkeypatch)
    _chat_until_resolved()
    _chat_provision_to_the_end()
    cleanup, deletes = _fake_cascade(chat)
    removed = removed_by_the_chat(_agent_tools().reset_system, SID)
    assert removed.get("status") == "reset", removed
    assert cleanup.call_args.args[0] == SID          # its agent runtime and gateway target
    assert deletes == [SID]                           # its graph data and System node
    assert chat.catalog.get(SID) is None              # its registration
    with pytest.raises(oa.SessionNotFoundError):
        chat.store.get_session(SID)

    card = World(monkeypatch)
    _chat_until_resolved()
    _chat_provision_to_the_end()
    card_cleanup, card_deletes = _fake_cascade(card)

    async def _card_removal():
        """Remove system (full) on the card: ``?wait=false``, then its job."""
        started = await onboard.onboard_reset_system(SID, wait=False, full=True)
        assert started == {"status": "started", "system_id": SID}, started
        while onboard._RESET_JOBS.get(SID)["state"] == "running":
            await asyncio.sleep(0.01)
    asyncio.run(_card_removal())
    assert card_cleanup.call_args == cleanup.call_args
    assert card_deletes == deletes
    assert card.catalog.get(SID) is None


def test_after_its_card_removed_it_a_chat_onboarding_starts_from_discover(world):
    _chat_until_resolved()
    _chat_provision_to_the_end()
    _fake_cascade(world)
    asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=False))  # the card, keeping it
    assert world.catalog.get(SID)["status"] == "registered"
    assert world._node(SID) == {}                                             # no System node
    assert "error" not in oa.discover(SID)      # the removal deleted the onboarding session


def test_chat_reset_of_a_half_done_chat_onboarding_starts_it_over(world):
    _chat_until_resolved()
    res = _agent_tools().reset_system(SID)
    assert res.get("status") == "session_cleared", res
    assert "error" not in oa.discover(SID)            # discover can run again
