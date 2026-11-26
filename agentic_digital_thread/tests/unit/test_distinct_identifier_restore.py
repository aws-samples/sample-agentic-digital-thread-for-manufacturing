# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A restore that rebuilds a system's agent config keeps its measured
different-identifier records (offline).

Onboarding stores each measured pair of same-named identifier columns that
hold different identifiers in the graph (ADR-0098) and hands the records to
the system agent in its agent config (``distinct_identifiers``), where the
query tool's shape guard is built from them. The startup restore
(``server._restore``; the graph-snapshot repair runs the same one) rebuilds a
PostgreSQL system's schema from the graph and, when the system's runtime is
gone or not READY, rewrites the ``adt/agent-config/<id>`` secret from what
the restore passes (local mode: the subprocess config). It used to pass no
records, so the rewritten config had none, the restored schema had no note
to fall back on, and a lookup the guard refuses ran.

Pinned here, with the graph as ``_field_graph_fake.FieldGraph``:

* cloud, runtime lost: the rewritten secret carries the records the graph
  holds and the schema note rendered from them, and a runtime started from
  that secret refuses the wrong-form lookup;
* local mode: the subprocess config carries them too;
* a system the graph holds no record for gets no key and no note, unless it
  was provisioned before the graph held the pairs: then the restore carries
  forward what its current secret holds for the guard (valid records, else a
  note the fallback parser reads, for the same database and tables), and the
  recreated tool still refuses the wrong-form lookup;
* a current secret that cannot be read (any error but "not found") is not
  rewritten: with the runtime gone that one system stays unregistered; a
  READY runtime still registers by the fast path, which writes no secret;
* a record read that fails, or that returns an edge without two valid shape
  signatures (one missing or invalid, or a shape-only edge), leaves
  that one system unregistered (logged) and restores the others.

All seams are mocked; no AWS, database or graph call is made.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

import agents.agent_manager as am
import agents.connection_resolver as cr
import agents.db as db
import agents.server as server
from agents import schema_format
from agents import system_agent as sa
from agents.db import GraphQueryError
from tests.unit._field_graph_fake import FieldGraph

SID, OTHER = "sys-a", "sys-b"
_URL = "https://gateway.example.com/mcp"
_DETAILS = {
    "consumption": [("consumption_id", "uuid"), ("lot_id", "uuid"),
                    ("ref_code", "character varying")],
    "dispatch_lines": [("line_id", "uuid"), ("ref_code", "character varying"),
                       ("qty", "integer")],
}
_DESC = schema_format.format_tables_desc(_DETAILS)
_SIG_A = '[["-"],[["prefix","kx"],[4,"digit"]]]'
_SIG_B = '[["-"],[["prefix","qz"],[4,"digit"]]]'
_CONN = {"host": "db.example.com", "port": "5432", "dbname": "d", "user": "u",
         "password": "p"}  # nosec B105 - fake fixture
_WRONG = "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX-0042'"
_NOTE_TAIL = ("unit: consumption.ref_code and dispatch_lines.ref_code, 40 and 37 values read, "
              "shaped like KX-9999 and QZ-9999.")


def _graph(edge: dict | None = None, *, with_edge: bool = True) -> FieldGraph:
    """Both systems' Fields; one measured pair on ``SID`` only (``edge``: its
    properties; ``with_edge=False``: none, as for a system provisioned before
    the graph held the pairs)."""
    graph = FieldGraph()
    for sid in (SID, OTHER):
        for table, columns in _DETAILS.items():
            for column, _type in columns:
                graph.add_field(sid, table, column)
    if with_edge:
        graph.add_edge(f"{SID}.consumption.ref_code", f"{SID}.dispatch_lines.ref_code",
                       **(edge if edge is not None else _SIGNED_EDGE))
    return graph


_SIGNED_EDGE = {"concept": "unit", "read_a": 40, "read_b": 37, "signature_a": _SIG_A,
                "signature_b": _SIG_B, "measured_at": "2026-10-18T00:00:00+00:00",
                "basis": "measured"}


def _systems(*ids):
    return [{"id": i, "name": i.upper(), "schema": i.replace("-", "_"), "plant": "P1",
             "connection_type": "PostgreSQL", "status": "onboarded"} for i in ids]


def _fake_pg(*args, **_kwargs):
    sql, params = args[1], (args[2] if len(args) > 2 else ())
    if "information_schema.tables" in sql:
        return {"rows": [[t] for t in _DETAILS]}
    if "information_schema.columns" in sql:
        return {"rows": [list(c) for c in _DETAILS.get(params[1], [])]}
    return {"rows": []}


def _restore(monkeypatch, manager, graph, *ids) -> list:
    """Run the real ``server._restore`` for PostgreSQL systems ``ids``;
    returns the system ids it registered."""
    graph.install(monkeypatch)
    registered: list = []
    catalog = MagicMock()
    catalog.get_all.return_value = [{"id": i} for i in ids]
    monkeypatch.setattr(server, "_agent_manager", manager)
    monkeypatch.setattr(server, "_sql_tables_desc_from_graph", lambda _sid: _DESC)
    monkeypatch.setattr(server, "neptune_query", MagicMock(side_effect=[_systems(*ids), []]))
    monkeypatch.setattr(server, "pg", _fake_pg)
    monkeypatch.setattr(server, "catalog", catalog)
    monkeypatch.setattr(server, "add_system_agent", lambda sid, _url: registered.append(sid))
    monkeypatch.setattr(server, "_reconcile_restored_sql_provenance", MagicMock())
    monkeypatch.setattr(cr, "resolve_connection",
                        lambda sid: {"system_id": sid, "connection_type": "PostgreSQL"})
    monkeypatch.setattr(cr, "pg_conn_params", lambda _resolved: dict(_CONN))
    monkeypatch.setattr(db, "neptune_write", lambda *_a, **_k: True)
    server._restore()
    return registered


@pytest.fixture
def cloud(monkeypatch):
    """A cloud AgentManager whose runtimes are gone: every restored system
    goes through ``_start_cloud``, which rewrites its agent-config secret.
    The secrets are kept on ``manager.secrets`` by name (seed one there to
    stand for the secret a system's last provision wrote); the names written
    are listed on ``manager.written``."""
    import agents.demo.db_routing as db_routing
    from botocore.exceptions import ClientError
    manager = am.AgentManager(mode="cloud", gateway_id="gw-1", gateway_url=_URL)
    secrets: dict = {}
    written: list = []
    unreadable: set = set()

    class _Secrets:
        def get_secret_value(self, SecretId):
            if SecretId in unreadable:
                raise ClientError({"Error": {"Code": "ThrottlingException"}},
                                  "GetSecretValue")
            if SecretId not in secrets:
                raise ClientError({"Error": {"Code": "ResourceNotFoundException"}},
                                  "GetSecretValue")
            return {"SecretString": json.dumps(secrets[SecretId])}

    def upsert(_sm, name, value):
        secrets[name] = json.loads(value)
        written.append(name)
        return f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{name}-x1y2z3"

    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _Secrets())
    monkeypatch.setattr(am, "_shared_upsert_connection_secret", upsert)
    monkeypatch.setattr(db_routing, "system_agent_pg_config", lambda _s, cfg, *_a, **_k: dict(cfg))
    monkeypatch.setattr(manager, "_cloud_fast_path_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(manager, "_apply_business_context", lambda _sid, cfg, _region: cfg)
    monkeypatch.setattr(manager, "_base_env_vars", lambda *_a, **_k: {})
    monkeypatch.setattr(manager, "_runtime_connection_env", lambda *_a, **_k: {})
    monkeypatch.setattr(manager, "_m2m_authorizer_config", lambda: None)
    monkeypatch.setattr(manager, "_find_reusable_runtime", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(manager, "_create_runtime_with_az_retry", lambda *_a, **_k: (
        "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_x-1", "adt_x-1"))
    monkeypatch.setattr(manager, "_warmup_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(manager, "_upsert_and_sync_gateway_target", lambda *_a, **_k: "tgt-1")
    manager.secrets = secrets
    manager.written = written
    manager.unreadable = unreadable     # secret names whose read fails (not "not found")
    return manager


class _FakeAgent:
    def __init__(self, **kwargs):
        self.tools = kwargs.get("tools") or []


def _runtime_tool(monkeypatch, secret: dict):
    """The query tool of a runtime started from ``secret``, built the way
    ``runtime_app`` builds it from the agent-config secret."""
    from agents import runtime_app
    monkeypatch.delenv("SYSTEM_PROMPT", raising=False)
    monkeypatch.delenv("TABLES_DESC", raising=False)
    prompt, desc, conn_params, cfg = runtime_app._extract_prompt_and_conn_params(
        json.loads(json.dumps(secret)), "PostgreSQL")
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr(sa, "_system_agent_model", lambda: None)
    connects: list = []
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **c: connects.append(c))
    agent = sa.create_system_agent(SID, "SYS-A", "sys_a", prompt, desc, conn_params,
                                   "PostgreSQL", cfg)
    (tool,) = [t for t in agent.tools if getattr(t, "_tool_func", None)]
    return tool, connects


def test_a_restore_that_recreates_the_runtime_keeps_the_graph_records(monkeypatch, cloud):
    registered = _restore(monkeypatch, cloud, _graph(), SID, OTHER)
    assert registered == [SID, OTHER]
    secret = cloud.secrets[f"adt/agent-config/{SID}"]
    (record,) = secret["distinct_identifiers"]
    assert {k: record[k] for k in ("concept", "column", "table_a", "table_b", "read_a",
                                   "read_b", "signature_a", "signature_b", "basis")} == {
        "concept": "unit", "column": "ref_code", "table_a": "consumption",
        "table_b": "dispatch_lines", "read_a": 40, "read_b": 37, "signature_a": _SIG_A,
        "signature_b": _SIG_B, "basis": "measured"}
    # The restored schema names the pair as the provision did, from the same record.
    assert secret["tables_desc"].startswith(_DESC)
    assert secret["tables_desc"].endswith(schema_format.DISTINCT_IDENTIFIERS_PREFIX
                                          + _NOTE_TAIL)
    # A runtime started from the rewritten secret refuses the wrong-form lookup.
    tool, connects = _runtime_tool(monkeypatch, secret)
    out = json.loads(tool._tool_func(sql=_WRONG))
    assert out["identifier_check"]["kind"] == "wrong_form"
    assert connects == []


def test_a_system_the_graph_holds_no_record_for_gets_no_key_and_no_note(monkeypatch, cloud):
    _restore(monkeypatch, cloud, _graph(), SID, OTHER)
    other = cloud.secrets[f"adt/agent-config/{OTHER}"]
    assert "distinct_identifiers" not in other
    assert schema_format.DISTINCT_IDENTIFIERS_PREFIX not in other["tables_desc"]
    assert "distinct_identifiers" in cloud.secrets[f"adt/agent-config/{SID}"]


def test_a_local_restore_hands_the_records_to_the_agent_server(monkeypatch):
    import agents.business_context as bc
    manager = am.AgentManager(mode="local")
    started: list = []

    class _Server:
        def __init__(self, _sid, _host, _port, build, _stop_wait):
            started.append(build.keywords)

        def start(self, _timeout):
            pass

    monkeypatch.setattr(am, "LocalAgentServer", _Server)
    monkeypatch.setattr(manager, "_allocate_port", lambda: 9301)
    monkeypatch.setattr(bc, "read_business_context", lambda _sid: None)
    assert _restore(monkeypatch, manager, _graph(), SID) == [SID]
    (config,) = started
    (record,) = config["connection_config"]["distinct_identifiers"]
    assert (record["signature_a"], record["signature_b"]) == (_SIG_A, _SIG_B)
    assert config["tables_desc"].endswith(_NOTE_TAIL)


def test_an_unreadable_record_leaves_that_system_unregistered_and_restores_the_rest(
        monkeypatch, cloud, caplog):
    graph = _graph()
    held = graph.query

    def query(cypher, params=None):
        if (params or {}).get("sid") == SID:
            raise GraphQueryError("ServiceUnavailable", "down")
        return held(cypher, params)

    graph.query = query
    with caplog.at_level(logging.WARNING):
        registered = _restore(monkeypatch, cloud, graph, SID, OTHER)
    assert registered == [OTHER]
    assert f"adt/agent-config/{SID}" not in cloud.secrets      # never rewritten without them
    assert f"adt/agent-config/{OTHER}" in cloud.secrets
    assert "distinct-identifier records" in caplog.text and SID in caplog.text


# --------------------------------------------------------------------------- #
# A system provisioned before the graph held the pairs (no edge, a baked note)
# --------------------------------------------------------------------------- #

_LEGACY_PAIR = {"concept": "unit", "column": "ref_code", "table_a": "consumption",
                "table_b": "dispatch_lines", "read_a": 40, "read_b": 37,
                "shape_a": "KX-9999", "shape_b": "QZ-9999"}
#: The note an earlier provision baked, ahead of the graph record.
_LEGACY_DESC = schema_format.with_distinct_identifiers(_DESC, [_LEGACY_PAIR])
_SIGNED_RECORD = {**{k: v for k, v in _LEGACY_PAIR.items() if not k.startswith("shape_")},
                  "signature_a": _SIG_A, "signature_b": _SIG_B,
                  "measured_at": "2026-10-18T00:00:00+00:00", "basis": "measured"}


def _pre_edge_secret(**overrides) -> dict:
    """``adt/agent-config/<SID>`` as a provision before this change wrote it."""
    secret = {**_CONN, "tables_desc": _LEGACY_DESC, "system_prompt": "p",
              "concept_mappings": [{"concept": "unit", "field": "consumption.ref_code",
                                    "role": "identifier"}]}
    secret.update(overrides)
    return {k: v for k, v in secret.items() if v is not None}


def _refuses_wrong_form(monkeypatch, secret: dict) -> bool:
    tool, connects = _runtime_tool(monkeypatch, secret)
    out = json.loads(tool._tool_func(sql=_WRONG))
    return (out.get("identifier_check") or {}).get("kind") == "wrong_form" and connects == []


def test_a_pre_edge_note_survives_a_restore_that_recreates_the_runtime(monkeypatch, cloud):
    cloud.secrets[f"adt/agent-config/{SID}"] = _pre_edge_secret()
    registered = _restore(monkeypatch, cloud, _graph(with_edge=False), SID)
    assert registered == [SID]
    assert f"adt/agent-config/{SID}" in cloud.written          # the runtime was recreated
    secret = cloud.secrets[f"adt/agent-config/{SID}"]
    # The graph's schema, plus the note the system was provisioned with.
    assert secret["tables_desc"] == _LEGACY_DESC
    assert "distinct_identifiers" not in secret                 # the note is the source
    assert _refuses_wrong_form(monkeypatch, secret)


def test_pre_edge_records_in_the_secret_are_carried_forward(monkeypatch, cloud):
    cloud.secrets[f"adt/agent-config/{SID}"] = _pre_edge_secret(
        distinct_identifiers=[_SIGNED_RECORD])
    _restore(monkeypatch, cloud, _graph(with_edge=False), SID)
    secret = cloud.secrets[f"adt/agent-config/{SID}"]
    (record,) = secret["distinct_identifiers"]
    assert (record["signature_a"], record["signature_b"]) == (_SIG_A, _SIG_B)
    assert secret["tables_desc"].endswith(_NOTE_TAIL)
    assert _refuses_wrong_form(monkeypatch, secret)


def test_unusable_records_in_the_secret_are_dropped_and_its_note_is_carried(monkeypatch, cloud):
    # An earlier build put shape-only records in the secret.
    shape_only = {k: v for k, v in _SIGNED_RECORD.items() if not k.startswith("signature_")}
    cloud.secrets[f"adt/agent-config/{SID}"] = _pre_edge_secret(
        distinct_identifiers=[{**shape_only, "shape_a": "KX-9999", "shape_b": "QZ-9999"}])
    _restore(monkeypatch, cloud, _graph(with_edge=False), SID)
    secret = cloud.secrets[f"adt/agent-config/{SID}"]
    assert "distinct_identifiers" not in secret
    assert secret["tables_desc"] == _LEGACY_DESC
    assert _refuses_wrong_form(monkeypatch, secret)


@pytest.mark.parametrize("secret", [
    _pre_edge_secret(host="other-db.example.com"),            # another database
    _pre_edge_secret(tables_desc=schema_format.with_distinct_identifiers(
        _DESC, [{**_LEGACY_PAIR, "table_b": "gone_lines"}])),  # a table no longer there
    _pre_edge_secret(tables_desc=schema_format.with_distinct_identifiers(
        _DESC, [{k: v for k, v in _LEGACY_PAIR.items()
                 if not k.startswith("shape_")}])),          # a note with no shapes
    _pre_edge_secret(tables_desc=None),
], ids=["another database", "a table the schema lacks", "no shapes", "no schema"])
def test_a_note_that_does_not_fit_the_restored_system_is_not_carried(monkeypatch, cloud,
                                                                     secret):
    cloud.secrets[f"adt/agent-config/{SID}"] = secret
    assert _restore(monkeypatch, cloud, _graph(with_edge=False), SID) == [SID]
    rewritten = cloud.secrets[f"adt/agent-config/{SID}"]
    assert rewritten["tables_desc"] == _DESC
    assert "distinct_identifiers" not in rewritten


def test_an_unreadable_secret_is_not_rewritten_when_the_runtime_is_gone(monkeypatch, cloud,
                                                                         caplog):
    # The read fails for a reason other than "not found" (throttled), the
    # runtime is gone, and the upsert would succeed: rewriting would drop the
    # only copy of the note, so this one system stays unregistered instead.
    name = f"adt/agent-config/{SID}"
    cloud.secrets[name] = _pre_edge_secret()
    cloud.unreadable.add(name)
    with caplog.at_level(logging.WARNING):
        registered = _restore(monkeypatch, cloud, _graph(with_edge=False), SID, OTHER)
    assert registered == [OTHER]
    assert cloud.written == [f"adt/agent-config/{OTHER}"]      # SID's secret never rewritten
    assert cloud.secrets[name] == _pre_edge_secret()
    assert "agent config" in caplog.text and SID in caplog.text


def test_an_unreadable_secret_still_registers_a_ready_runtime(monkeypatch, cloud):
    # A READY runtime takes the fast path, which writes no secret: the
    # unreadable secret (and the guard in it) stays as it is.
    name = f"adt/agent-config/{SID}"
    cloud.secrets[name] = _pre_edge_secret()
    cloud.unreadable.add(name)
    monkeypatch.setattr(cloud, "_cloud_fast_path_ready", lambda sid, *_a, **_k: sid == SID)
    registered = _restore(monkeypatch, cloud, _graph(with_edge=False), SID, OTHER)
    assert registered == [SID, OTHER]
    assert cloud.written == [f"adt/agent-config/{OTHER}"]
    assert cloud.secrets[name] == _pre_edge_secret()


def test_an_unreadable_secret_decides_the_fast_path_once(monkeypatch, cloud):
    # The runtime is READY when first checked and gone a moment later: the
    # restore must act on one answer, not check twice and rewrite the config.
    name = f"adt/agent-config/{SID}"
    cloud.secrets[name] = _pre_edge_secret()
    cloud.unreadable.add(name)
    answers = {SID: [True, False]}
    calls: list = []

    def fast_path(sid, *_a, **_k):
        calls.append(sid)
        seq = answers.get(sid)
        return seq.pop(0) if seq else False

    monkeypatch.setattr(cloud, "_cloud_fast_path_ready", fast_path)
    registered = _restore(monkeypatch, cloud, _graph(with_edge=False), SID, OTHER)
    assert registered == [SID, OTHER]
    assert calls.count(SID) == 1
    assert cloud.written == [f"adt/agent-config/{OTHER}"]
    assert cloud.secrets[name] == _pre_edge_secret()


# --------------------------------------------------------------------------- #
# A stored edge the guard cannot use fails the restore of that system
# --------------------------------------------------------------------------- #

_UNUSABLE_EDGES = {
    "one signature missing": {k: v for k, v in _SIGNED_EDGE.items() if k != "signature_b"},
    "one signature invalid": {**_SIGNED_EDGE, "signature_b": "not a signature"},
    "a shape-only edge": {**{k: v for k, v in _SIGNED_EDGE.items()
                          if not k.startswith("signature_")},
                       "shape_a": "KX-9999", "shape_b": "QZ-9999"},
}


@pytest.mark.parametrize("edge", list(_UNUSABLE_EDGES.values()), ids=list(_UNUSABLE_EDGES))
def test_an_unusable_graph_record_leaves_that_system_unregistered(monkeypatch, cloud, caplog,
                                                                  edge):
    # Its old secret holds a valid note; it is not consulted (no migration here).
    cloud.secrets[f"adt/agent-config/{SID}"] = _pre_edge_secret()
    with caplog.at_level(logging.WARNING):
        registered = _restore(monkeypatch, cloud, _graph(edge), SID, OTHER)
    assert registered == [OTHER]
    assert cloud.written == [f"adt/agent-config/{OTHER}"]      # SID's secret untouched
    assert cloud.secrets[f"adt/agent-config/{SID}"] == _pre_edge_secret()
    assert "distinct-identifier records" in caplog.text and SID in caplog.text
