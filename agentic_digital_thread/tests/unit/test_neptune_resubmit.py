# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A Neptune Analytics request to resubmit a query is resubmitted, bounded.

Neptune Analytics can refuse an ExecuteQuery with ``UnprocessableException``
"Retry for SDK query requests is suppressed, please resubmit the query." The
SDK never retries it (the error is modeled as a 422 sender fault, not
retryable), and the app ran each query exactly once, so a single such reply
failed a whole provision job (``onboard.neptune_write`` raises inside the
provision transaction) and degraded reads to no rows.

The app now resubmits exactly that condition, a fixed small number of times
with jittered backoff and a total time bound, for reads, and for a write only
when its call site states that it is safe to run twice (``db.resubmit_safe_write``,
see ``test_neptune_write_resubmit_opt_in``). Whether a write is safe to run
twice is never judged from its openCypher: every other write that may have
run fails at once, as it did before, and every other error keeps the
behaviour it had.

Every graph label and property here is invented: the fix is generic.
"""
from __future__ import annotations

import io
import json
import logging

import pytest
from botocore.exceptions import ClientError

import agents.db as db

RESUBMIT_MESSAGE = ("Retry for SDK query requests is suppressed, please "
                    "resubmit the query.")


def _client_error(code: str, message: str, reason: str | None = None) -> ClientError:
    """A modeled botocore error the way the neptune-graph client raises one:
    a ``ClientError`` subclass named after the error code."""
    response = {"Error": {"Code": code, "Message": message},
                "ResponseMetadata": {"HTTPStatusCode": 422}}
    if reason:
        response["reason"] = reason
    cls = type(code, (ClientError,), {})
    return cls(response, "ExecuteQuery")


def _resubmit_error() -> ClientError:
    return _client_error("UnprocessableException", RESUBMIT_MESSAGE)


class _Client:
    """Raises the queued errors in order, then answers ``rows``."""

    def __init__(self, errors, rows):
        self.errors = list(errors)
        self.rows = rows
        self.calls = []

    def execute_query(self, **kwargs):
        self.calls.append(kwargs["queryString"])
        if self.errors:
            raise self.errors.pop(0)
        return {"payload": io.BytesIO(json.dumps({"results": self.rows}).encode())}


@pytest.fixture
def no_wait(monkeypatch):
    """No real sleeping; record the delays asked for. (Tolerates the module's
    absence so the reproductions fail on the old code for their own reason.)"""
    slept: list[float] = []
    try:
        import agents.graph_resubmit as gr
    except ImportError:
        return slept
    monkeypatch.setattr(gr, "_sleep", slept.append)
    return slept


@pytest.fixture
def graph(monkeypatch, no_wait):
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")

    def use(errors, rows=None):
        client = _Client(errors, rows if rows is not None else [])
        monkeypatch.setattr(db.boto3, "client", lambda *a, **k: client)
        return client
    return use


READ = "MATCH (w:Widget {code: $code}) RETURN w.code AS code"
MERGE_WRITE = (
    "MERGE (b:Bin {id: $bid}) SET b.label = $label "
    "WITH b MERGE (r:Rack {id: $rid}) ON CREATE SET r.label = $rid "
    "MERGE (r)-[:HOLDS]->(b)")
CREATE_WRITE = "CREATE (e:Event {id: $eid, at: $at})"
COUNTER_WRITE = "MATCH (r:Rack {id: $rid}) SET r.revision = coalesce(r.revision, 0) + 1"
#: The statement the 2026-10-21 walk's provision failed on, verbatim.
PROVISION_CONCEPT_MERGE = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $cid, "
    "c.created_by_system = $created_by_system, c.created_at = $created_at, "
    "c.created_by_run = $created_by_run")


# ── Reproductions: each fails on the unmodified code ─────────────────────────

def test_a_read_asked_to_resubmit_is_resubmitted_and_answers(graph):
    client = graph([_resubmit_error()], rows=[{"code": "W-1"}])
    assert db.neptune_query_checked(READ, {"code": "W-1"}) == [{"code": "W-1"}]
    assert client.calls == [READ, READ]


def test_the_lenient_read_answers_instead_of_degrading_to_no_rows(graph):
    graph([_resubmit_error()], rows=[{"code": "W-1"}])
    with db.recording_incomplete_reads() as sink:
        assert db.neptune_query(READ, {"code": "W-1"}) == [{"code": "W-1"}]
    assert sink == []


def test_an_opted_in_write_asked_to_resubmit_is_resubmitted(graph):
    client = graph([_resubmit_error()])
    with db.resubmit_safe_write():
        assert db.neptune_write(MERGE_WRITE, {"bid": "b1", "label": "x", "rid": "r1"}) is True
    assert len(client.calls) == 2


def test_an_opted_in_returning_write_asked_to_resubmit_answers_its_rows(graph):
    cypher = "MATCH (r:Rack {id: $rid}) SET r.label = $label RETURN r.id AS id"
    client = graph([_resubmit_error()], rows=[{"id": "r1"}])
    with db.resubmit_safe_write():
        assert db.neptune_write_returning(cypher, {"rid": "r1", "label": "x"}) == [{"id": "r1"}]
    assert len(client.calls) == 2


def test_a_provision_write_survives_one_request_to_resubmit(graph):
    """The walk failure: inside the provision transaction a refused write
    raises ``RuntimeError`` and fails the job. One request to resubmit must
    not, for the statement the walk's provision failed on
    (``_dp_commit_concept_edges``' Concept MERGE, which opts in), with
    invented values."""
    import agents.onboard as onboard
    client = graph([_resubmit_error()])
    token = onboard._STRICT_PROVISION_WRITES.set(True)
    try:
        with db.resubmit_safe_write():
            assert onboard.neptune_write(PROVISION_CONCEPT_MERGE, {
                "cid": "bin-code", "created_by_system": "sys-a",
                "created_at": "t", "created_by_run": "run-1"}) is True
    finally:
        onboard._STRICT_PROVISION_WRITES.reset(token)
    assert len(client.calls) == 2


def test_each_resubmit_is_logged_once_with_its_category(graph, caplog):
    graph([_resubmit_error(), _resubmit_error()], rows=[])
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        assert db.neptune_query_checked(READ, {"code": "W-1"}) == []
    lines = [r.getMessage() for r in caplog.records if "resubmit" in r.getMessage()]
    assert len(lines) == 2
    assert all("category=UnprocessableException" in line for line in lines)


# ── Bounds ───────────────────────────────────────────────────────────────────

def test_resubmits_stop_after_the_fixed_attempt_count(graph, no_wait):
    import agents.graph_resubmit as gr
    client = graph([_resubmit_error() for _ in range(10)])
    with pytest.raises(db.GraphQueryError) as err:
        db.neptune_query_checked(READ, {"code": "W-1"})
    assert err.value.category == "UnprocessableException"
    assert len(client.calls) == gr.MAX_ATTEMPTS
    assert len(no_wait) == gr.MAX_ATTEMPTS - 1


def test_the_backoff_is_jittered_and_grows_within_its_cap(graph, no_wait, monkeypatch):
    import agents.graph_resubmit as gr
    draws = iter([0.0, 1.0, 0.0, 1.0])
    monkeypatch.setattr(gr, "_random", lambda: next(draws))
    graph([_resubmit_error() for _ in range(gr.MAX_ATTEMPTS)])
    with db.resubmit_safe_write():
        assert db.neptune_write(MERGE_WRITE, {"bid": "b1", "label": "x", "rid": "r1"}) is False
    for n, delay in enumerate(no_wait, start=1):
        step = min(gr.BACKOFF_CAP_S, gr.BACKOFF_BASE_S * 2 ** (n - 1))
        assert step / 2 <= delay <= step
    # The jitter draws are applied: the first delay is the floor of its step.
    assert no_wait[0] == pytest.approx(gr.BACKOFF_BASE_S / 2)


def test_no_resubmit_starts_past_the_total_time_bound(graph, no_wait, monkeypatch):
    import agents.graph_resubmit as gr
    clock = iter([0.0, gr.TOTAL_BOUND_S + 1.0])
    monkeypatch.setattr(gr, "_monotonic", lambda: next(clock))
    client = graph([_resubmit_error() for _ in range(gr.MAX_ATTEMPTS)])
    with pytest.raises(db.GraphQueryError):
        db.neptune_query_checked(READ, {"code": "W-1"})
    assert len(client.calls) == 1
    assert no_wait == []


# ── Guards: unchanged behaviour (these also pass on the old code) ────────────

@pytest.mark.parametrize("cypher", [CREATE_WRITE, COUNTER_WRITE])
def test_a_write_that_could_double_apply_is_not_resubmitted(graph, cypher):
    client = graph([_resubmit_error()])
    assert db.neptune_write(cypher, {"eid": "e1", "at": "t", "rid": "r1"}) is False
    assert len(client.calls) == 1


@pytest.mark.parametrize("error", [
    _client_error("UnprocessableException", "Query timed out.", reason="QUERY_TIMEOUT"),
    _client_error("UnprocessableException", "Memory limit exceeded.",
                  reason="MEMORY_LIMIT_EXCEEDED"),
    _client_error("ValidationException", "Invalid input"),
])
def test_other_errors_are_not_resubmitted(graph, error):
    client = graph([error])
    with pytest.raises(db.GraphQueryError):
        db.neptune_query_checked(READ, {"code": "W-1"})
    assert len(client.calls) == 1


def test_a_non_client_error_is_not_resubmitted(graph):
    client = graph([RuntimeError("socket closed")])
    with db.resubmit_safe_write():
        assert db.neptune_write(MERGE_WRITE, {"bid": "b1", "label": "x", "rid": "r1"}) is False
    assert len(client.calls) == 1


# ── A write is not judged safe to run twice from its text ───────────────────

def _status_error(code: str, status: int, message: str = "boom") -> ClientError:
    response = {"Error": {"Code": code, "Message": message},
                "ResponseMetadata": {"HTTPStatusCode": status}}
    return type(code, (ClientError,), {})(response, "ExecuteQuery")


#: A failure after which the write may have taken effect.
MAY_HAVE_RUN = [
    _status_error("UnprocessableException", 422, RESUBMIT_MESSAGE),
    _status_error("InternalServerException", 500),
    _status_error("ServiceUnavailable", 503),
]
MAY_HAVE_RUN_IDS = ["resubmit-422", "500", "503"]

#: Writes whose second run differs from the first, which a check of their
#: text read as safe to run twice: a projected alias of the value written
#: (a list tail or slice, a string replace or reverse, also inside FOREACH),
#: a dynamic property read toggled, a MERGE keyed on a count of what it
#: creates, a delete and recreate.
STATEFUL_WRITES = [
    "MATCH (n:Bin {id:$id}) WITH n, tail(n.items) AS rest SET n.items = rest",
    "MATCH (n:Bin {id:$id}) WITH n, n.items[1..] AS rest SET n.items = rest",
    "MATCH (n:Bin {id:$id}) WITH n, replace(n.s, 'a', 'aa') AS next SET n.s = next",
    "MATCH (n:Bin {id:$id}) WITH n, reverse(n.s) AS next SET n.s = next",
    "MATCH (n:Bin {id:$id}) WITH n, tail(n.items) AS rest "
    "FOREACH (_ IN [1] | SET n.items = rest)",
    "MATCH (n:Bin {id:$id}) SET n.flag = NOT n['flag']",
    "MATCH (n:Bin {id:$id}) SET n.flag = n['flag'] IS NULL",
    "MATCH (n:Item) WITH count(n) AS c MERGE (:Item {rank:c})",
    "MATCH (n:Bin {id:$id})-[r:HOLDS]->(m:Item) DELETE r "
    "WITH n, m MERGE (m)-[:HOLDS]->(n)",
]
#: Writes a text check already refused (ON MATCH, a clock, a toggle, a
#: counter between parentheses); they stay refused.
STATEFUL_WRITES_ALREADY_REFUSED = [
    "MERGE (n:Bin {id: $id}) ON CREATE SET n.made = $at ON MATCH SET n.seen = $at",
    "MATCH (n:Bin {id: $id}) SET n.seen_at = datetime()",
    "MATCH (n:Bin {id: $id}) SET n.enabled = NOT n.enabled",
    "MATCH (n:Bin {id: $id}) SET n.value = (n.value) - (1)",
]
#: Ordinary MERGE / SET writes with no opt-in at their call site.
PLAIN_WRITES = [MERGE_WRITE, PROVISION_CONCEPT_MERGE]


@pytest.mark.parametrize("error", MAY_HAVE_RUN, ids=MAY_HAVE_RUN_IDS)
@pytest.mark.parametrize("cypher", STATEFUL_WRITES + PLAIN_WRITES)
def test_a_write_without_the_opt_in_fails_at_once_when_it_may_have_run(graph, cypher, error):
    client = graph([error])
    assert db.neptune_write(cypher, {"id": "b1"}) is False
    assert client.calls == [cypher]


@pytest.mark.parametrize("error", MAY_HAVE_RUN, ids=MAY_HAVE_RUN_IDS)
@pytest.mark.parametrize("cypher", STATEFUL_WRITES_ALREADY_REFUSED)
def test_writes_already_refused_stay_refused(graph, cypher, error):
    client = graph([error])
    assert db.neptune_write(cypher, {"id": "b1"}) is False
    assert client.calls == [cypher]


@pytest.mark.parametrize("error", MAY_HAVE_RUN, ids=MAY_HAVE_RUN_IDS)
def test_a_returning_write_without_the_opt_in_fails_at_once_when_it_may_have_run(graph, error):
    cypher = STATEFUL_WRITES[0] + " RETURN n.id AS id"
    client = graph([error])
    assert db.neptune_write_returning(cypher, {"id": "b1"}) is None
    assert client.calls == [cypher]


@pytest.mark.parametrize("error", MAY_HAVE_RUN, ids=MAY_HAVE_RUN_IDS)
def test_a_write_sent_through_a_read_helper_is_still_a_write(graph, error):
    cypher = "MATCH (n:Bin {id: $id}) SET n.label = $l RETURN n.id AS id"
    client = graph([error])
    with pytest.raises(db.GraphQueryError):
        db.neptune_query_checked(cypher, {"id": "b1", "l": "x"})
    assert client.calls == [cypher]


# ── What counts as a read: no write clause outside literals and comments ────

@pytest.mark.parametrize("cypher", [
    "MATCH (n:Bin) WHERE n.at < datetime() RETURN n.id AS id",
    "MATCH (n:Bin) RETURN n.id AS id, rand() AS r",
    "MATCH (n:Bin) WHERE NOT n.locked RETURN n.id AS id",
    "MATCH (n:Bin) WHERE n.note = 'SET it, then MERGE' RETURN n.id AS id",
    "MATCH (n:Bin) RETURN n.`create` AS c // DELETE later",
    "MATCH (n:Bin) WHERE n.id STARTS WITH $p RETURN n.id AS id",
])
def test_a_read_is_resubmitted_whatever_it_calls(graph, cypher):
    client = graph([_status_error("InternalServerException", 500)], rows=[{"id": "b1"}])
    assert db.neptune_query_checked(cypher, {"p": "b"}) == [{"id": "b1"}]
    assert client.calls == [cypher, cypher]


@pytest.mark.parametrize("cypher", [
    "merge (n:Bin {id: $id})",
    "MATCH (n:Bin {id: $id}) DETACH DELETE n",
    "MATCH (n:Bin {id: $id}) REMOVE n.label",
    "MATCH (n:Bin) FOREACH (x IN $xs | SET n.label = x)",
    "CALL neptune.algo.pageRank.mutate({writeProperty: 'rank'})",
    "MATCH (n:Bin) CALL { WITH n CREATE (:Copy {of: n.id}) } RETURN n",
    "",
])
def test_any_write_clause_makes_a_statement_a_write(graph, cypher):
    client = graph([_status_error("InternalServerException", 500)])
    assert db.neptune_write(cypher, {"id": "b1", "xs": []}) is False
    assert client.calls == [cypher]
