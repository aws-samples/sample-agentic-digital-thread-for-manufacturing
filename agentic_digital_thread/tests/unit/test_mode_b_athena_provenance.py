# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Mode B Athena provenance is what it was before the comma-before-UNNEST work.

Mode B (the orchestrator writes the SQL and calls the system's ``run_query``)
does not rewrite a comma before ``CROSS JOIN``: it refuses that query before
anything is submitted (``test_athena_unnest_scope``). So nothing new records
what Mode B submitted: the provenance sidecar entry of a Mode B call is built
from the tool input, exactly as it was, Mode B writes nothing to any
provenance store, saving a turn's entries reads no other record, and no
DynamoDB operation is used that the app did not use before.

Fully offline: a boto3 Athena double, in-memory stores; synthetic names.
"""
from __future__ import annotations

import ast
import asyncio
import json
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents.athena_tools as at
import agents.chat_client as chat_client
import agents.provenance_store as provenance_store
import agents.runtime_app as runtime_app
import agents.system_agent as sa
from agents.connection_types import ATHENA
from agents.job_store import JobStore
from agents.provenance import mint_query_ref
from agents.runtime_asgi import provenance_context_var

SESSION = "sess-modeb"
_CFG = {"workgroup": "wg_a", "database": "db_a", "region": "us-east-1"}
_DESC = "table_x(items: varchar, site: varchar) (no partition keys)"
_TOOL = "lake___run_query"
_PLAIN = "SELECT site, COUNT(*) AS n FROM table_x GROUP BY site"


class _Athena:
    def __init__(self):
        self.submitted: list[str] = []

    def start_query_execution(self, QueryString, **_kwargs):
        self.submitted.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.submitted)}"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [
            {"Name": "site", "Type": "varchar"}, {"Name": "n", "Type": "bigint"}]},
            "Rows": [{"Data": [{"VarCharValue": "site"}, {"VarCharValue": "n"}]},
                     {"Data": [{"VarCharValue": "site-a"}, {"VarCharValue": "3"}]}]}}


@pytest.fixture
def athena(monkeypatch):
    fake = _Athena()
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: fake)
    monkeypatch.setattr(at, "list_partition_columns", lambda *a, **k: [])
    monkeypatch.setattr(provenance_store, "_job_store", JobStore(provenance_store._KIND, table=None))
    return fake


@pytest.fixture
def store_calls(monkeypatch):
    """Every JobStore write and batch read, whatever the kind."""
    calls: list[tuple] = []
    write, many = JobStore.__setitem__, JobStore.get_many

    def counted_write(self, job_id, state):
        calls.append(("put", self._kind, job_id))
        return write(self, job_id, state)

    def counted_many(self, job_ids):
        calls.append(("get_many", self._kind, tuple(job_ids)))
        return many(self, job_ids)

    monkeypatch.setattr(JobStore, "__setitem__", counted_write)
    monkeypatch.setattr(JobStore, "get_many", counted_many)
    return calls


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, name, description=""):
        def register(fn):
            self.tools[name] = fn
            return fn
        return register


def _system(sql: str, tool_id: str = "t1") -> str:
    """One Mode B ``run_query`` call on the system runtime, under the
    provenance context an interactive gateway call carries."""
    mcp = _Mcp()
    sa._register_mode_b_run(mcp, "Lake", ATHENA, "db_a", None, dict(_CFG), _DESC, "")

    async def call():
        provenance_context_var.set((SESSION, mint_query_ref(SESSION, tool_id)))
        return await mcp.tools["run_query"](query=sql)

    return asyncio.run(call())


def _result(text: str) -> dict:
    return {"toolUseId": "t1", "status": "success", "content": [{"text": text}]}


def _stored() -> str:
    entry = provenance_store.get_query(SESSION, mint_query_ref(SESSION, "t1"))
    return entry["executed_query"] if entry else ""


def _legacy(text: str) -> str:
    state = SimpleNamespace(session_id=SESSION, tool_calls_log=[{"name": _TOOL, "toolUseId": "t1"}],
                            tool_inputs={"t1": json.dumps({"query": _PLAIN})}, tool_results={},
                            rule_ids=set())
    runtime_app._capture_tool_results_from_message({"role": "user", "content": [{"toolResult": _result(text)}]},
                                                   state)
    for frame in runtime_app._provenance_query_sidecar(state):
        chat_client._store_provenance_queries(SESSION, frame["provenance_queries"])
    return _stored()


def _enforce(text: str, monkeypatch) -> str:
    monkeypatch.setattr("agents.runtime_integrity.persist_typed_proposals", lambda *a, **k: 0)
    invocation = runtime_app._RuntimeIntegrityInvocation("how many per site")
    invocation.session_id = SESSION
    invocation.consume({"current_tool_use": {"name": _TOOL, "toolUseId": "t1",
                                             "input": json.dumps({"query": _PLAIN})}})
    invocation.consume({"message": {"role": "user", "content": [{"toolResult": _result(text)}]}})
    for frame in runtime_app._integrity_query_sidecar(invocation):
        chat_client._store_provenance_queries(SESSION, frame["provenance_queries"])
    return _stored()


def _local(text: str) -> str:
    hook = chat_client._ToolResultHook(queue.Queue(), session_id=SESSION)
    hook._on_tool_done(SimpleNamespace(tool_use={"name": _TOOL, "toolUseId": "t1",
                                                 "input": {"query": _PLAIN}},
                                       result=_result(text)))
    chat_client._persist_local_provenance_queries(hook)
    return _stored()


@pytest.mark.parametrize("emitter", ["runtime legacy", "runtime enforce", "local"])
def test_an_ordinary_mode_b_query_stores_its_input_as_before(athena, monkeypatch, emitter):
    text = _system(_PLAIN)
    assert athena.submitted == [_PLAIN + " LIMIT 500"]
    assert "_adt_executed_query" not in json.loads(text)
    stored = {"runtime legacy": lambda: _legacy(text),
              "runtime enforce": lambda: _enforce(text, monkeypatch),
              "local": lambda: _local(text)}[emitter]()
    assert stored == _PLAIN


def test_mode_b_writes_nothing_and_saving_the_turn_reads_nothing_more(athena, store_calls):
    _system(_PLAIN)
    assert store_calls == []                       # the system runtime wrote no record
    text = json.dumps({"rows": [["site-a", "3"]]})
    assert _legacy(text) == _PLAIN
    assert [c[0] for c in store_calls] == ["put"]  # the one entry, and no batch read
    assert not hasattr(provenance_store, "put_executed_query")


#: The DynamoDB-reaching calls the app's agents made before this work, by file.
#: A new one needs its own IAM grant and consistency decision first.
_DYNAMODB_CALLS = {("eval_queue.py", "get_many"), ("job_store.py", "batch_get_item")}
_WATCHED = {"get_many", "batch_get_item", "batch_write_item", "transact_get_items",
            "transact_write_items", "execute_statement", "batch_execute_statement"}


def test_no_new_dynamodb_operation_is_used():
    agents = Path(sa.__file__).parent
    found = set()
    for path in sorted(agents.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in _WATCHED:
                    found.add((path.name, name))
    assert found == _DYNAMODB_CALLS
