# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Round 7 of the provision resend: the c3/c3b ValueMapping write, the
system-owned cleanup deletes and the schema-rewrite marks are sent again
after a failure that leaves open whether they ran (a 422 resubmit request, a
500, a 503 or a 409); the anchored ValueMapping, the cross-system rule-artifact
delete and the other writes left out still fail at once.

Each statement's text is pinned in ``test_neptune_write_resubmit_opt_in``
(``OPTED_IN``); ``tests/integration/test_resubmit_replay_memgraph.py`` runs
them twice on a real openCypher engine and compares the graph.

Labels and properties are invented except in the pinned provision statements.
"""
from __future__ import annotations

import ast
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import agents.db as db
import agents.onboard as onboard

AGENTS = Path(__file__).resolve().parents[2] / "agents"
RESUBMIT_MESSAGE = "Retry for SDK query requests is suppressed, please resubmit the query."


def _client_error(code: str, status: int, message: str = "boom") -> ClientError:
    response = {"Error": {"Code": code, "Message": message},
                "ResponseMetadata": {"HTTPStatusCode": status}}
    return type(code, (ClientError,), {})(response, "ExecuteQuery")


AMBIGUOUS = [
    _client_error("UnprocessableException", 422, RESUBMIT_MESSAGE),
    _client_error("InternalServerException", 500),
    _client_error("ServiceUnavailable", 503),
    _client_error("ConflictException", 409, "Concurrent modification"),
]
IDS = ["resubmit-422", "500", "503", "409"]
SERVER_ERROR = AMBIGUOUS[1]

VM_MERGE = "MERGE (vm:ValueMapping"
MARK = "SET s.schema_rewrite_started = $started"


class _Graph:
    """A neptune-graph client: raises the queued errors on the statements
    containing ``only``, answers a ValueMapping write with its id and the
    start mark with its token, everything else with no rows."""

    def __init__(self, errors, only):
        self.errors, self.only = list(errors), only
        self.calls: list[str] = []

    def execute_query(self, **kwargs):
        query, params = kwargs["queryString"], kwargs.get("parameters") or {}
        self.calls.append(query)
        if self.errors and self.only in query:
            raise self.errors.pop(0)
        rows = []
        if "RETURN vm.id AS id" in query:
            rows = [{"id": params["vmid"]}]
        elif "RETURN s.schema_rewrite_started AS started" in query:
            rows = [{"started": params["started"]}]
        return {"payload": io.BytesIO(json.dumps({"results": rows}).encode())}

    def sent(self, text: str) -> list:
        return [c for c in self.calls if text in c]


@pytest.fixture
def graph(monkeypatch):
    import agents.graph_resubmit as gr
    monkeypatch.setattr(gr, "_sleep", lambda _s: None)
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")

    def use(errors=(), only=""):
        client = _Graph(errors, only)
        monkeypatch.setattr(db.boto3, "client", lambda *a, **k: client)
        return client
    return use


@pytest.fixture
def strict_provision():
    token = onboard._STRICT_PROVISION_WRITES.set(True)
    yield
    onboard._STRICT_PROVISION_WRITES.reset(token)


def _vm(src="erp", dst="cmms", tag="T1"):
    return {"from_system": src, "from_field": "orders.line", "from_value": tag,
            "to_system": dst, "to_field": "lines.code", "to_value": f"L-{tag}",
            "concept": "production-line", "confidence": 0.9, "method": "semantic_similarity"}


# ── ValueMappings: c3 and c3b resend the unanchored write ───────────────────

@pytest.mark.parametrize("error", AMBIGUOUS, ids=IDS)
def test_a_c3_value_mapping_is_sent_again(graph, strict_provision, error):
    client = graph([error], only=VM_MERGE)
    ctx = SimpleNamespace(system_id="erp", value_mappings_written=0, nodes=0)
    assert onboard._dp_write_cross_vm(ctx, _vm(), 0, {"erp", "cmms"}) is True
    assert ctx.value_mappings_written == 1
    sent = client.sent(VM_MERGE)
    assert len(sent) == 2 and sent[0] == sent[1] == onboard._VALUE_MAPPING_UPSERT


@pytest.mark.parametrize("error", AMBIGUOUS, ids=IDS)
def test_a_c3b_value_mapping_is_sent_again(graph, strict_provision, monkeypatch, error):
    monkeypatch.setattr(onboard, "catalog", SimpleNamespace(
        get=lambda sid: {"schema": "cmms_db", "status": "connected"}))
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings",
                        lambda *_a: [_vm("cmms", "hist-x", "E1")])
    client = graph([error], only=VM_MERGE)
    ctx = SimpleNamespace(system_id="cmms", value_mappings_written=0, nodes=0)
    onboard._dp_equipment_vms_legacy(ctx)
    assert ctx.value_mappings_written == 1
    assert len(client.sent(VM_MERGE)) == 2


def test_an_anchored_value_mapping_still_fails_at_once(graph, strict_provision):
    """The anchored form deletes the mapping's MAPS_TO_ASSET edges before it
    MERGEs the one correct edge; it is not opted in."""
    client = graph([SERVER_ERROR], only=VM_MERGE)
    extra = (("to_element_id", "teid", "el-1"), ("to_path", "tpath", "/p"))
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        onboard._upsert_value_mapping("vm-1", _vm(), extra=extra, anchor="el-1")
    assert len(client.sent(VM_MERGE)) == 1


def test_a_stamped_replay_value_mapping_is_not_sent_again(graph):
    """The self-learned replay writes a variant with the accept stamp: not
    c3/c3b, not opted in."""
    client = graph([SERVER_ERROR], only=VM_MERGE)
    stamp = (("accepted_by", "acc_by", ""), ("confirmation_mode", "conf_mode", ""))
    assert onboard._write_value_mapping("vm-learned-1", _vm(), extra=stamp) is False
    assert len(client.sent(VM_MERGE)) == 1


@pytest.mark.parametrize("variant", [
    {"extra": (("to_path", "tpath", "/p"),)},
    {"anchor": "el-1"},
    {"concept_link": True},
    {"write": lambda c, p=None: []},
])
def test_only_the_unanchored_base_mapping_may_be_sent_again(variant):
    with pytest.raises(ValueError):
        onboard._upsert_value_mapping("vm-1", _vm(), resubmit_safe=True, **variant)


def test_the_base_mapping_statement_is_the_pinned_constant():
    assert onboard._value_mapping_upsert() == onboard._VALUE_MAPPING_UPSERT


def test_only_c3_and_c3b_opt_their_mapping_in():
    callers = set()
    for path in sorted(AGENTS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call)
                            and getattr(node.func, "id", None) in (
                                "_write_value_mapping", "_upsert_value_mapping")
                            and any(k.arg == "resubmit_safe" for k in node.keywords)):
                        callers.add((path.name, fn.name, ast.unparse(node.func)))
    assert callers == {
        ("onboard.py", "_dp_write_cross_vm", "_write_value_mapping"),
        ("onboard.py", "_dp_equipment_vms_legacy", "_write_value_mapping"),
        ("onboard.py", "_write_value_mapping", "_upsert_value_mapping"),
    }


# ── The system-owned cleanup deletes ────────────────────────────────────────

CLEANUPS = {
    "schema-subgraph": (onboard._delete_stale_schema_subgraph, "DETACH DELETE se, f"),
    "auto-internal-joinpaths": (onboard._delete_stale_auto_internal_joinpaths,
                                "DETACH DELETE jp"),
    "distinct-identifier-records": (onboard._delete_stale_distinct_identifier_records,
                                    "DIFFERENT_IDENTIFIER_FROM]->() DELETE r"),
}


@pytest.mark.parametrize("error", AMBIGUOUS, ids=IDS)
@pytest.mark.parametrize("cleanup", sorted(CLEANUPS))
def test_each_system_owned_cleanup_delete_is_sent_again(graph, strict_provision, cleanup, error):
    delete, marker = CLEANUPS[cleanup]
    client = graph([error], only=marker)
    delete("sys-a")
    sent = client.sent(marker)
    assert len(sent) == 2 and sent[0] == sent[1]


def test_the_cross_system_rule_artifact_delete_still_fails_at_once(graph, strict_provision):
    """It removes a PEER's rows too (``peer -> this system``), which the
    peer's provision can write while this one runs: not opted in."""
    client = graph([SERVER_ERROR], only="DETACH DELETE ir")
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        onboard._delete_stale_onboarding_rule_artifacts("sys-a")
    assert len(client.sent("DETACH DELETE ir")) == 1


def test_the_retired_link_sweep_delete_still_fails_at_once(graph):
    client = graph([SERVER_ERROR], only="DELETE r")
    assert onboard.neptune_write(onboard._RETIRED_LINK_DELETE_CYPHER,
                                 {"fid": "sys-a.t.c", "concept": "c"}) is False
    assert len(client.sent("DELETE r")) == 1


# ── The schema-rewrite marks ────────────────────────────────────────────────

@pytest.mark.parametrize("error", AMBIGUOUS, ids=IDS)
def test_the_start_mark_is_sent_again_and_confirmed(graph, strict_provision, error):
    client = graph([error], only=MARK)
    token = onboard._mark_schema_rewrite("sys-a")
    sent = client.sent(MARK)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert isinstance(token, int)


@pytest.mark.parametrize("settled", [True, False], ids=["settled", "failed"])
@pytest.mark.parametrize("error", AMBIGUOUS, ids=IDS)
def test_the_end_of_the_rewrite_is_sent_again(graph, strict_provision, caplog, error, settled):
    client = graph([error], only="schema_rewrite_started = $token")
    with caplog.at_level(logging.WARNING, logger="agents.onboard"):
        onboard._finish_schema_rewrite("sys-a", 1_700_000_000_000, settled=settled)
    assert len(client.sent("schema_rewrite_started = $token")) == 2
    assert not [r for r in caplog.records if "could not be written" in r.getMessage()]


@pytest.mark.parametrize("which", ["mark", "failed marker"])
@pytest.mark.parametrize("error", AMBIGUOUS, ids=IDS)
def test_a_stale_marker_clear_is_sent_again(graph, caplog, error, which):
    clear = "WHERE s.schema_rewrite_started = $started" if which == "mark" \
        else "WHERE s.schema_rewrite_failed = $failed"
    client = graph([error], only=clear)
    with caplog.at_level(logging.WARNING, logger="agents.onboard"):
        if which == "mark":
            assert onboard._fresh_rewrite_mark("sys-a", 1000) is False
        else:
            assert onboard._fresh_marker(
                "sys-a", 1000, onboard._SCHEMA_REWRITE_CLEAR_STALE_FAILED_CYPHER, "failed",
                "failed schema-rewrite marker") is False
    assert len(client.sent(clear)) == 2
    assert not [r for r in caplog.records if "could not be removed" in r.getMessage()]


# ── The returning helper ────────────────────────────────────────────────────

def test_the_returning_helper_sends_its_statement_again_and_answers_rows(graph):
    client = graph([SERVER_ERROR], only=VM_MERGE)
    params = {"vmid": "vm-1", "fsys": "erp", "tsys": "cmms"}
    rows = onboard.neptune_write_returning_resubmit_safe(onboard._VALUE_MAPPING_UPSERT, params)
    assert rows == [{"id": "vm-1"}] and len(client.sent(VM_MERGE)) == 2


def test_the_plain_returning_write_is_not_sent_again(graph):
    client = graph([SERVER_ERROR], only=VM_MERGE)
    assert onboard.neptune_write_returning(onboard._VALUE_MAPPING_UPSERT, {"vmid": "vm-1"}) is None
    assert len(client.sent(VM_MERGE)) == 1
