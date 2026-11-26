# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The startup restore's graph writes are sent again when a new request can
fix how they failed, the seed is bounded, and the restore's logs say what
happened.

``server._restore`` runs on every app start, before the app answers its health
checks, and can reach a graph endpoint that is not answering yet. It restores
the onboarded systems first and seeds the core concepts after them, within a
time budget (``CORE_CONCEPT_SEED_BUDGET_S``); startup waits for the restore up
to ``STARTUP_RESTORE_WAIT_S``, logs when it stops waiting, and the restore logs
when it ended (test_startup_restore_background.py covers the app serving
meanwhile). The graph client
makes one attempt per call (``db._neptune_client``), so a write is sent again
only when it certainly never ran, or when its call site opts in as safe to run
twice (``db.resubmit_safe_write``).

The startup restore sends three statements (``report-neptune-resubmit.md``
lists every graph write it runs):

* the core-concept seed, ``MERGE (c:Concept {id: $cid}) ON CREATE SET ...``
  once per core concept. ON CREATE is its only SET and every value is a
  parameter or a constant, so a second run changes nothing; Neptune Analytics
  creates at most one node for concurrent MERGEs of one key, so a copy still
  running when the connection dropped leaves the same graph too. It is opted in
  with ``after_drop=True``: also sent again after a dropped connection or a 502;
* the two provenance SETs of ``onboard._persist_schema_relation_kinds``, which
  the restore runs for a system whose graph schema lacks its types. Each SETs a
  parameter on a node matched by id, so it is opted in, but not after a drop:
  a copy still running could land after a later write of the same property.

Labels and properties are invented except in the pinned startup statements.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time

import pytest
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError

import agents.db as db
import agents.server as server
from agents.catalog import CORE_CONCEPT_DEFINITIONS

URL = "https://g-abcdefghij.example/queries"
RESUBMIT_MESSAGE = "Retry for SDK query requests is suppressed, please resubmit the query."
#: The seed statement, as the restore sends it.
CORE_CONCEPT_SEED = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $desc, "
    "c.source = 'core'")
RELATION_KIND_SET = "MATCH (se:SchemaEntry {id: $seid}) SET se.relation_kind = $kind"
FIELD_DATA_TYPE_SET = "MATCH (f:Field {id: $fid}) SET f.data_type = $dtype"


def _client_error(code: str, status: int, message: str = "boom") -> ClientError:
    response = {"Error": {"Code": code, "Message": message},
                "ResponseMetadata": {"HTTPStatusCode": status}}
    return type(code, (ClientError,), {})(response, "ExecuteQuery")


#: The service answered, but whether the query took effect is open.
ANSWERED = [
    _client_error("InternalServerException", 500),
    _client_error("ServiceUnavailable", 503),
    _client_error("UnprocessableException", 422, RESUBMIT_MESSAGE),
    _client_error("ConflictException", 409, "Concurrent modification"),
]
#: The connection dropped after the request may have reached the service.
DROPPED = [ConnectionClosedError(endpoint_url=URL), _client_error("BadGateway", 502)]
#: The query outlasted the wait and may still be running: never sent again.
TIMED_OUT = [ReadTimeoutError(endpoint_url=URL), _client_error("GatewayTimeout", 504)]


def _ids(errors):
    return [type(e).__name__ + str((getattr(e, "response", None) or {}).get(
        "ResponseMetadata", {}).get("HTTPStatusCode", "")) for e in errors]


def _answer():
    return {"payload": io.BytesIO(json.dumps({"results": []}).encode())}


class _SeedGraph:
    """A neptune-graph client that runs the seed MERGE on a dict of Concept
    nodes. ``faults`` maps a concept id to ``(error, ran)`` pairs its sends
    raise in turn: ``ran`` raises the error after the MERGE took effect (the
    connection dropped after the query committed), else before it ran."""

    def __init__(self, faults=None):
        self.faults = {cid: list(pairs) for cid, pairs in (faults or {}).items()}
        self.nodes: dict = {}
        self.creates = 0
        self.sends: list[str] = []

    def execute_query(self, **kwargs):
        assert kwargs["queryString"] == CORE_CONCEPT_SEED
        params = kwargs["parameters"]
        cid = params["cid"]
        self.sends.append(cid)
        pending = self.faults.get(cid) or []
        error, ran = pending.pop(0) if pending else (None, False)
        if error is not None and not ran:
            raise error
        if cid not in self.nodes:
            self.nodes[cid] = {"name": cid, "description": params["desc"], "source": "core"}
            self.creates += 1
        if error is not None:
            raise error
        return _answer()


@pytest.fixture
def graph(monkeypatch):
    import agents.graph_resubmit as gr
    monkeypatch.setattr(gr, "_sleep", lambda _s: None)
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")

    def use(client):
        monkeypatch.setattr(db.boto3, "client", lambda *a, **k: client)
        return client
    return use


FIRST = next(iter(CORE_CONCEPT_DEFINITIONS))
TOTAL = len(CORE_CONCEPT_DEFINITIONS)


def _seed_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "agents.server" and "core concepts" in r.getMessage()]


ALL_CONFIRMED = f"{TOTAL} of {TOTAL} core concepts confirmed"


def _again(left: int) -> str:
    """How a seed line that left ``left`` core concepts ends: they go to the
    background top-up (test_startup_restore_core_concept_topup.py)."""
    return f"a background top-up sends the {left} left again, for up to 60 min"


# ── The core-concept seed ───────────────────────────────────────────────────

@pytest.mark.parametrize("ran", [False, True], ids=["before-it-ran", "after-it-ran"])
@pytest.mark.parametrize("error", DROPPED + ANSWERED, ids=_ids(DROPPED + ANSWERED))
def test_a_seed_write_that_dropped_or_failed_is_sent_again_and_written_once(
        graph, caplog, error, ran):
    """A dropped connection, a 502, a 500, a 503, a 422 resubmit request or a
    409 on one seed write no longer leaves that core concept out: the MERGE is
    sent again, and whether the first send took effect or not, the concept
    ends up written once."""
    client = graph(_SeedGraph({FIRST: [(error, ran)]}))
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
    assert client.sends.count(FIRST) == 2
    assert set(client.nodes) == set(CORE_CONCEPT_DEFINITIONS)
    assert client.creates == TOTAL
    assert client.nodes[FIRST]["description"] == CORE_CONCEPT_DEFINITIONS[FIRST]
    assert _seed_lines(caplog) == [ALL_CONFIRMED]


@pytest.mark.parametrize("error", TIMED_OUT, ids=_ids(TIMED_OUT))
def test_a_timed_out_seed_write_is_not_sent_again_until_the_graph_has_stopped_it(
        graph, monkeypatch, error):
    """A read timeout or a 504 may leave the first copy running, so the seed
    write is not sent again: the restore carries on with the next concept,
    and the background top-up (test_startup_restore_core_concept_topup.py)
    does not send it before the graph's own limit has stopped that copy
    (``db.graph_query_timeout_ms()`` from when it entered the graph's queue,
    which was before the write returned, plus ``db.GRAPH_READ_MARGIN_S``).
    Its first two rounds, 15 and 45 s later, fall inside that window; the
    third, at 105 s, sends it once, and the concept is written once."""
    clock = _Clock()
    monkeypatch.setattr(server, "_restore_clock", clock, raising=False)

    def pause(stop, seconds):
        clock.now += seconds
        return stop.is_set()
    monkeypatch.setattr(server, "_topup_wait", pause, raising=False)
    client = graph(_SeedGraph({FIRST: [(error, False)]}))
    sent_at = []
    real = client.execute_query

    def execute_query(**kwargs):
        sent_at.append((kwargs["parameters"]["cid"], clock.now))
        return real(**kwargs)
    client.execute_query = execute_query
    server._restore_seed_core_concepts()
    topup = server._core_concept_topup.thread
    assert topup is not None, "nothing sends the timed-out concept again"
    topup.join(5)
    assert not topup.is_alive()
    hold_s = db.GRAPH_QUERY_TIMEOUT_MS / 1000 + db.GRAPH_READ_MARGIN_S
    assert hold_s == 65
    first_sends = [at for cid, at in sent_at if cid == FIRST]
    assert first_sends == [1000.0, 1000.0 + 15 + 30 + 60]
    assert all(not 1000.0 < at < 1000.0 + hold_s for at in first_sends)
    assert set(client.nodes) == set(CORE_CONCEPT_DEFINITIONS)
    assert client.creates == TOTAL


def test_a_seed_write_stays_within_the_bound_and_counts_as_not_confirmed(graph, caplog):
    import agents.graph_resubmit as gr
    client = graph(_SeedGraph({FIRST: [(DROPPED[0], False)] * gr.MAX_ATTEMPTS}))
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
    assert client.sends.count(FIRST) == gr.MAX_ATTEMPTS
    assert FIRST not in client.nodes and len(client.nodes) == TOTAL - 1
    assert _seed_lines(caplog) == [
        f"{TOTAL - 1} of {TOTAL} core concepts confirmed; 1 not confirmed: {FIRST}; "
        f"{_again(1)}"]


def test_the_seed_log_counts_only_the_concepts_confirmed(graph, caplog):
    """The log line counts the writes ``neptune_write`` reported as done, not
    the calls made, and names the concepts that were not confirmed."""
    failing = list(CORE_CONCEPT_DEFINITIONS)[3:7]
    client = graph(_SeedGraph({cid: [(TIMED_OUT[0], False)] for cid in failing}))
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
    assert len(client.nodes) == TOTAL - 4
    assert _seed_lines(caplog) == [
        f"{TOTAL - 4} of {TOTAL} core concepts confirmed; 4 not confirmed: "
        f"{', '.join(failing)}; {_again(4)}"]


def test_a_concept_whose_first_send_committed_is_reported_not_confirmed_not_failed(
        graph, caplog):
    """The first MERGE commits and then the connection drops; both resends
    drop before they run. ``neptune_write`` answers False, yet the concept is
    in the graph: the log says it was not confirmed, never that it failed or
    is missing."""
    client = graph(_SeedGraph({FIRST: [(DROPPED[0], True), (DROPPED[0], False),
                                       (DROPPED[0], False)]}))
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
    assert client.sends.count(FIRST) == 3
    assert FIRST in client.nodes and len(client.nodes) == TOTAL
    (line,) = _seed_lines(caplog)
    assert line == (f"{TOTAL - 1} of {TOTAL} core concepts confirmed; 1 not confirmed: {FIRST}; "
                    f"{_again(1)}")
    assert "fail" not in line.lower() and "seeded" not in line.lower()


# ── The seed budget ─────────────────────────────────────────────────────────

class _Clock:
    """A monotonic clock a test moves forward by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_seed_budget_stops_further_sends_and_names_what_was_not_sent(
        graph, caplog, monkeypatch):
    """Each send takes 10 s here. The budget is checked before each statement,
    so with a 60 s budget six concepts are sent (at 0, 10, ..., 50 s) and none
    after; the log names every concept not sent."""
    clock = _Clock()
    monkeypatch.setattr(server, "_restore_clock", clock, raising=False)
    client = graph(_SeedGraph())
    real = client.execute_query

    def execute_query(**kwargs):
        clock.now += 10
        return real(**kwargs)
    client.execute_query = execute_query
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
    ids = list(CORE_CONCEPT_DEFINITIONS)
    assert client.sends == ids[:6]
    assert server.CORE_CONCEPT_SEED_BUDGET_S == 60
    assert _seed_lines(caplog) == [
        f"6 of {TOTAL} core concepts confirmed; {TOTAL - 6} not sent "
        f"(the 60s seed budget was spent): {', '.join(ids[6:])}; {_again(TOTAL - 6)}"]


def test_a_fast_seed_sends_every_core_concept(graph, caplog, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(server, "_restore_clock", clock, raising=False)
    client = graph(_SeedGraph())
    with caplog.at_level(logging.INFO, logger="agents.server"):
        server._restore_seed_core_concepts()
    assert client.sends == list(CORE_CONCEPT_DEFINITIONS) and len(client.sends) == 65
    assert _seed_lines(caplog) == [ALL_CONFIRMED]


def test_the_seed_worst_case_stated_in_the_code_matches_its_bounds():
    """The seed's comment states its worst case from the client's own bounds:
    no resend starts later than ``TOTAL_BOUND_S`` after the first send, and
    one send is at most the connect timeout plus the read timeout, so one
    statement is at most 105 s and the seed at most its budget plus that."""
    import agents.graph_resubmit as gr
    statement = gr.TOTAL_BOUND_S + db.GRAPH_CONNECT_TIMEOUT_S + db.GRAPH_READ_TIMEOUT_S
    assert statement == 105
    assert server.CORE_CONCEPT_SEED_BUDGET_S + statement == 165


# ── The restore order and its log ───────────────────────────────────────────

def test_the_system_restores_run_before_the_core_concept_seed(monkeypatch):
    ran = []
    monkeypatch.setattr(server, "_restore_seed_core_concepts", lambda: ran.append("seed"))
    monkeypatch.setattr(server, "_restore_sql_systems",
                        lambda: ran.append("sql") or {"sys-pg"})
    monkeypatch.setattr(server, "_restore_direct_source_systems",
                        lambda ids: ran.append(("direct", ids)))
    server._restore_base()
    assert ran == ["sql", ("direct", {"sys-pg"}), "seed"]


def _restore_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "agents.server" and "Neptune restore" in r.getMessage()]


class _Noticed(logging.Handler):
    """Sets ``seen`` once startup logs that it is waiting for the restore."""

    def __init__(self):
        super().__init__()
        import threading
        self.seen = threading.Event()

    def emit(self, record):
        if "still running" in record.getMessage():
            self.seen.set()


def test_startup_stops_waiting_at_the_budget_and_the_restore_finishes(monkeypatch, caplog):
    """A restore still running at ``STARTUP_RESTORE_WAIT_S`` is not cut:
    startup stops waiting and says so once, the restore runs on to its end in
    the background and logs that it finished and how long it took, and what
    it did stands."""
    monkeypatch.setattr(server, "STARTUP_RESTORE_WAIT_S", 0.01)
    noticed = _Noticed()
    log = logging.getLogger("agents.server")
    log.addHandler(noticed)
    registered = []

    def slow_restore():
        noticed.seen.wait(timeout=2)
        time.sleep(0.2)  # still running well past the wait
        registered.append("sys-a")
    monkeypatch.setattr(server, "_restore", slow_restore)
    try:
        with caplog.at_level(logging.INFO, logger="agents.server"):
            restore = server._restore_graph()
            assert restore.ended_in_time is False
            assert registered == []
            assert restore.wait(timeout=5)
    finally:
        log.removeHandler(noticed)
    assert registered == ["sys-a"]
    lines = _restore_lines(caplog)
    assert len(lines) == 2, lines
    assert re.fullmatch(r"Neptune restore still running after \d+s; the app starts serving "
                        r"and the restore finishes in the background \(changes to onboarded "
                        r"systems answer 503 until it ends\)", lines[0]), lines[0]
    finished = re.fullmatch(r"Neptune restore finished after (\d+\.\d)s", lines[1])
    assert finished and float(finished.group(1)) >= 0.2, lines[1]


def test_a_restore_that_ends_in_time_logs_only_that_it_finished(monkeypatch, caplog):
    registered = []
    monkeypatch.setattr(server, "_restore", lambda: registered.append("sys-a"))
    with caplog.at_level(logging.INFO, logger="agents.server"):
        assert server._restore_graph().ended_in_time is True
    assert registered == ["sys-a"]
    lines = _restore_lines(caplog)
    assert len(lines) == 1 and re.fullmatch(r"Neptune restore finished after \d+\.\ds", lines[0])


def test_a_restore_that_raises_is_logged_as_failed_not_skipped(monkeypatch, caplog):
    def broken():
        raise RuntimeError("no graph")
    monkeypatch.setattr(server, "_restore", broken)
    with caplog.at_level(logging.INFO, logger="agents.server"):
        assert server._restore_graph().ended_in_time is True
    lines = _restore_lines(caplog)
    assert len(lines) == 1, lines
    assert re.fullmatch(r"Neptune restore failed after \d+\.\ds \(RuntimeError: no graph\)",
                        lines[0]), lines[0]
    assert all("skipped" not in r.getMessage() for r in caplog.records)


def test_the_startup_wait_derivation_matches_the_deployed_health_checks():
    """The numbers the ``server.STARTUP_RESTORE_WAIT_S`` comment derives the
    wait from, read from the compute stack: the service's 300 s grace and the
    ALB target group's 2 passing checks 15 s apart, so /health has to answer
    by 270 s and the 60 s wait leaves 210 s. A change to either side fails
    here, so the comment is derived again."""
    from pathlib import Path
    stack = (Path(server.__file__).resolve().parents[2] / "stacks"
             / "adt_compute_stack.py").read_text(encoding="utf-8")
    grace_s = int(re.search(r"health_check_grace_period=Duration\.seconds\((\d+)\)",
                            stack).group(1))
    target = re.search(r'path="/health", healthy_http_codes="200",\s*'
                       r"interval=Duration\.seconds\((\d+)\).*?"
                       r"healthy_threshold_count=(\d+)", stack, re.S)
    interval_s, healthy_checks = int(target.group(1)), int(target.group(2))
    answer_by_s = grace_s - healthy_checks * interval_s
    assert (grace_s, healthy_checks, interval_s, answer_by_s) == (300, 2, 15, 270)
    assert answer_by_s - server.STARTUP_RESTORE_WAIT_S == 210


# ── The relation-kind provenance writes ─────────────────────────────────────

class _Client:
    """Raises the queued errors in order on the statements equal to ``only``,
    then answers no rows; records every statement sent."""

    def __init__(self, errors, only):
        self.errors = list(errors)
        self.only = only
        self.calls: list[str] = []

    def execute_query(self, **kwargs):
        self.calls.append(kwargs["queryString"])
        if self.errors and kwargs["queryString"] == self.only:
            raise self.errors.pop(0)
        return _answer()


def _reconcile(monkeypatch):
    """The restore's provenance backfill for one system with one table."""
    monkeypatch.setattr(server, "_restore_relation_kinds", lambda *_a: {"bins": "base_table"})
    server._reconcile_restored_sql_provenance(
        "sys-a", "shop", "PostgreSQL", {}, "bins(code: text, qty: integer)")


@pytest.mark.parametrize("statement", [RELATION_KIND_SET, FIELD_DATA_TYPE_SET],
                         ids=["relation_kind", "data_type"])
@pytest.mark.parametrize("error", ANSWERED, ids=_ids(ANSWERED))
def test_a_restore_provenance_write_is_sent_again_after_an_answered_failure(
        graph, monkeypatch, error, statement):
    """A 500, a 503, a 422 resubmit request or a 409 on one provenance write no
    longer fails the system's restore (it stayed unregistered until the next
    start): the SET is sent again with the same parameters."""
    client = graph(_Client([error], statement))
    _reconcile(monkeypatch)
    assert client.calls.count(statement) == (2 if statement == RELATION_KIND_SET else 3)
    assert client.calls[client.calls.index(statement) + 1] == statement


@pytest.mark.parametrize("error", DROPPED + TIMED_OUT, ids=_ids(DROPPED + TIMED_OUT))
def test_a_restore_provenance_write_is_not_sent_again_after_a_drop(graph, monkeypatch, error):
    """A copy still running after a dropped connection could land after a
    later write of the same property, so the SET is not sent again."""
    client = graph(_Client([error], RELATION_KIND_SET))
    with pytest.raises(RuntimeError, match="relation provenance write failed"):
        _reconcile(monkeypatch)
    assert client.calls == [RELATION_KIND_SET]


# ── The after-drop opt-in ───────────────────────────────────────────────────

MERGE_WRITE = "MERGE (b:Bin {id: $bid}) ON CREATE SET b.label = $label"
PARAMS = {"bid": "b1", "label": "x"}


class _Plain:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def execute_query(self, **_kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return _answer()


@pytest.mark.parametrize("error", DROPPED + ANSWERED, ids=_ids(DROPPED + ANSWERED))
def test_an_after_drop_opt_in_is_sent_again_after_a_drop_or_an_answered_failure(graph, error):
    client = graph(_Plain([error]))
    with db.resubmit_safe_write(after_drop=True):
        assert db.neptune_write(MERGE_WRITE, PARAMS) is True
    assert client.calls == 2


@pytest.mark.parametrize("error", TIMED_OUT, ids=_ids(TIMED_OUT))
def test_an_after_drop_opt_in_is_not_sent_again_after_a_timeout(graph, error):
    client = graph(_Plain([error]))
    with db.resubmit_safe_write(after_drop=True):
        assert db.neptune_write(MERGE_WRITE, PARAMS) is False
    assert client.calls == 1


@pytest.mark.parametrize("error", DROPPED, ids=_ids(DROPPED))
def test_the_after_drop_opt_in_ends_with_its_block(graph, error):
    with db.resubmit_safe_write(after_drop=True):
        with db.resubmit_safe_write():
            client = graph(_Plain([error]))
            assert db.neptune_write(MERGE_WRITE, PARAMS) is False
        client = graph(_Plain([error]))
        assert db.neptune_write(MERGE_WRITE, PARAMS) is True
    client = graph(_Plain([error]))
    with db.resubmit_safe_write():
        assert db.neptune_write(MERGE_WRITE, PARAMS) is False
    assert client.calls == 1


# ── How a write ended ───────────────────────────────────────────────────────

VALIDATION = _client_error("ValidationException", 400, "bad query")


@pytest.mark.parametrize("errors, outcome", [
    ([], None),
    ([TIMED_OUT[0]], "timed_out"),
    ([TIMED_OUT[1]], "timed_out"),
    ([DROPPED[0]], "dropped"),
    ([ANSWERED[0]], "failed"),
    ([VALIDATION], "unclassified"),
], ids=["confirmed", "read-timeout", "504", "dropped", "500", "validation"])
def test_the_outcome_write_says_how_a_write_ended(graph, errors, outcome):
    """``db.neptune_write_outcome`` is ``db.neptune_write`` for a caller that
    must know how a write that was not confirmed ended: ``None`` when it was
    confirmed, else ``graph_resubmit.failure_kind`` of the error that ended
    it, or ``unclassified``. ``neptune_write`` still answers a bool."""
    import agents.graph_resubmit as gr
    assert gr.TIMED_OUT == "timed_out" and gr.DROPPED == "dropped" and gr.FAILED == "failed"
    client = graph(_Plain(errors))
    assert db.neptune_write_outcome(MERGE_WRITE, PARAMS) == outcome
    assert client.calls == 1
    client = graph(_Plain(errors))
    assert db.neptune_write(MERGE_WRITE, PARAMS) is (outcome is None)
