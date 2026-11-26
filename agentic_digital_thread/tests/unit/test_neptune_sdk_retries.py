# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Neptune Analytics client never resends a query; the app decides.

AWS's guidance for ``execute_query`` is one attempt per call
(``retries={"total_max_attempts": 1, "mode": "standard"}``) and a read timeout
the query cannot outlast. The app built the client with SDK defaults: in the
pinned botocore that is the ``legacy`` retry mode, 5 attempts, resending a
429, a 5xx and a read timeout itself with ``amz-sdk-request: attempt=2`` under
the same invocation id, so the app only ever saw the LAST error. Neptune
answered such resends on the 2026-10-21 walk with "Retry for SDK query
requests is suppressed, please resubmit the query".

Now the client makes one attempt, every query carries a server-side limit
below the client read timeout, and the transient errors botocore used to
resend are resubmitted by ``graph_resubmit`` as NEW requests, under the same
bounds as the resubmit request. A read is resubmitted after any of them but a
timeout. A write is resubmitted only when it certainly never ran (a 429
throttle, a connection never made), or, when its call site opts in as safe to
run twice (``db.resubmit_safe_write``), also after a 422 resubmit request, a
500, a 503 or a 409 conflict; never after a dropped connection or a timeout.

Every graph label and property here is invented: the fix is generic.
"""
from __future__ import annotations

import io
import json
import logging

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    ProxyConnectionError,
    ReadTimeoutError,
)

import agents.db as db

URL = "https://g-abcdefghij.example/queries"
READ = "MATCH (w:Widget {code: $code}) RETURN w.code AS code"
MERGE_WRITE = "MERGE (b:Bin {id: $bid}) SET b.label = $label"
CREATE_WRITE = "CREATE (e:Event {id: $eid})"
WRITES = [MERGE_WRITE, CREATE_WRITE]
PARAMS = {"code": "W-1", "bid": "b1", "label": "x", "eid": "e1"}


def _client_error(code: str, status: int, message: str = "boom",
                  reason: str | None = None) -> ClientError:
    """A botocore error the way the neptune-graph client raises one: a
    ``ClientError`` subclass named after the code, the modeled ``reason`` at
    the top of the response (as the pinned botocore parses it)."""
    response = {"Error": {"Code": code, "Message": message},
                "ResponseMetadata": {"HTTPStatusCode": status}}
    if reason:
        response["reason"] = reason
    return type(code, (ClientError,), {})(response, "ExecuteQuery")


class _Client:
    def __init__(self, errors, rows):
        self.errors = list(errors)
        self.rows = rows
        self.calls: list[dict] = []

    def execute_query(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return {"payload": io.BytesIO(json.dumps({"results": self.rows}).encode())}


@pytest.fixture
def no_wait(monkeypatch):
    import agents.graph_resubmit as gr
    slept: list[float] = []
    monkeypatch.setattr(gr, "_sleep", slept.append)
    return slept


@pytest.fixture
def graph(monkeypatch, no_wait):
    """A fake neptune-graph client; records the arguments the app builds it
    with in ``graph.built``."""
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    built: list[tuple] = []

    def use(errors=(), rows=None):
        client = _Client(errors, rows if rows is not None else [{"code": "W-1"}])

        def factory(*args, **kwargs):
            built.append((args, kwargs))
            return client
        monkeypatch.setattr(db.boto3, "client", factory)
        return client
    use.built = built
    return use


# ── The client: one attempt, bounded timeouts, a server-side limit ──────────

def test_the_graph_client_makes_one_attempt_with_bounded_timeouts(graph):
    graph()
    db.neptune_query_checked(READ, PARAMS)
    (args, kwargs), = graph.built
    assert args == ("neptune-graph",)
    config = kwargs.get("config")
    assert config is not None, "the client is built with SDK defaults"
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert config.read_timeout == db.GRAPH_READ_TIMEOUT_S
    assert config.connect_timeout == db.GRAPH_CONNECT_TIMEOUT_S


def test_the_graph_client_region_comes_from_aws_region_else_us_east_1(graph, monkeypatch):
    graph()
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    db.neptune_query_checked(READ, PARAMS)
    monkeypatch.delenv("AWS_REGION")
    db.neptune_query_checked(READ, PARAMS)
    assert [kwargs["region_name"] for _args, kwargs in graph.built] == ["eu-west-2", "us-east-1"]


def test_every_query_carries_the_server_side_limit(graph):
    client = graph()
    db.neptune_query_checked(READ, PARAMS)
    db.neptune_write(MERGE_WRITE, PARAMS)
    db.neptune_write_returning(MERGE_WRITE + " RETURN b.id AS id", PARAMS)
    limits = [call.get("queryTimeoutMilliseconds") for call in client.calls]
    assert limits == [db.GRAPH_QUERY_TIMEOUT_MS] * 3


def test_the_server_stops_a_query_before_the_client_gives_up():
    """Neptune measures the limit from when the query enters its queue, so it
    answers QUERY_TIMEOUT by about the limit after receipt; the read timeout
    leaves a margin for the network and the reply."""
    assert db.GRAPH_QUERY_TIMEOUT_MS / 1000 + 5 <= db.GRAPH_READ_TIMEOUT_S


def test_the_server_limit_keeps_the_window_queries_had_before():
    """With SDK defaults a query could run until the 60 s read timeout; the
    server limit does not take any of that window away (nobody has measured
    how many queries finish between 55 and 60 s)."""
    assert db.GRAPH_QUERY_TIMEOUT_MS >= 60_000


#: Time a web request needs beyond its graph queries (its own work and the
#: response), kept free under the ALB / CloudFront ceiling.
WEB_MARGIN_S = 15


def _web_ceiling_s() -> int:
    """The web app's response ceiling: the lower of the internal ALB's idle
    timeout and the CloudFront VPC-origin read timeout, read from the stack
    that deploys them."""
    import re
    from pathlib import Path
    stack = (Path(__file__).resolve().parents[3] / "stacks" / "adt_compute_stack.py").read_text()
    alb = re.search(r"ApplicationLoadBalancer\(self, \"AdtALB\".*?idle_timeout=Duration\.seconds\((\d+)\)",
                    stack, re.S)
    origin = re.search(r"VpcOrigin\.with_application_load_balancer\(.*?read_timeout="
                       r"Duration\.seconds\((\d+)\)", stack, re.S)
    assert alb and origin, "the ALB idle timeout or the origin read timeout moved"
    return min(int(alb.group(1)), int(origin.group(1)))


def test_one_query_with_its_resubmits_fits_a_web_request():
    """A resubmit starts no later than TOTAL_BOUND_S after the first attempt,
    and one attempt is bounded by the connect and read timeouts, so one
    query, resubmits included, ends with WEB_MARGIN_S to spare under the web
    ceiling (120 s today)."""
    import agents.graph_resubmit as gr
    worst = gr.TOTAL_BOUND_S + db.GRAPH_CONNECT_TIMEOUT_S + db.GRAPH_READ_TIMEOUT_S
    assert worst + WEB_MARGIN_S <= _web_ceiling_s()


# ── Real botocore: what is sent on the wire ──────────────────────────────────

class _Raw:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, amt=None, **_):
        return self._buf.read() if amt is None else self._buf.read(amt)

    def stream(self, **_):
        yield self._buf.read()

    def close(self):
        pass


def _reply(status, body, errortype=None):
    headers = {"Content-Type": "application/json"}
    if errortype:
        headers["x-amzn-errortype"] = errortype
    return AWSResponse(URL, status, headers, _Raw(json.dumps(body).encode()))


@pytest.fixture
def wire(monkeypatch, no_wait, tmp_path):
    """The real botocore client under the DEPLOYED environment (no
    AWS_RETRY_MODE / AWS_MAX_ATTEMPTS and no config file: the stacks set
    neither), answered offline by a before-send hook. ``sent`` records each
    request's retry headers."""
    for name in ("AWS_RETRY_MODE", "AWS_MAX_ATTEMPTS", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "aws-config"
    empty.write_text("")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(empty))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty))
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-abcdefghij")
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    sent: list[dict] = []

    def use(script):
        queue = list(script)

        def hook(request, **_):
            sent.append({k: request.headers.get(k)
                         for k in ("amz-sdk-request", "amz-sdk-invocation-id")})
            step = queue.pop(0) if queue else None
            if step is None:
                return _reply(200, {"results": [{"code": "W-1"}]})
            if isinstance(step, Exception):
                raise step
            return _reply(*step)

        def factory(*args, **kwargs):
            session = boto3.session.Session(
                aws_access_key_id="testing", aws_secret_access_key="testing",  # nosec B106 - fake, offline
                region_name="us-east-1")
            client = session.client(*args, **kwargs)
            client.meta.events.register("before-send", hook)
            return client
        monkeypatch.setattr(db.boto3, "client", factory)
        return sent
    return use


THROTTLED = (429, {"message": "Too many requests"}, "ThrottlingException")


def test_a_throttled_query_is_resubmitted_as_a_new_request_not_an_sdk_retry(wire, caplog):
    sent = wire([THROTTLED])
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        assert db.neptune_query_checked(READ, PARAMS) == [{"code": "W-1"}]
    assert len(sent) == 2
    assert [s["amz-sdk-request"].split(b";")[0] for s in sent] == [b"attempt=1", b"attempt=1"]
    assert sent[0]["amz-sdk-invocation-id"] != sent[1]["amz-sdk-invocation-id"]
    assert any("first_error=ThrottlingException" in r.getMessage() for r in caplog.records)


def test_a_read_that_timed_out_is_not_sent_again(wire):
    sent = wire([ReadTimeoutError(endpoint_url=URL)])
    with pytest.raises(db.GraphQueryError) as err:
        db.neptune_query_checked(READ, PARAMS)
    assert err.value.category == "ReadTimeoutError"
    assert len(sent) == 1


def test_a_write_that_timed_out_is_not_sent_again(wire):
    sent = wire([ReadTimeoutError(endpoint_url=URL)])
    assert db.neptune_write(MERGE_WRITE, PARAMS) is False
    assert len(sent) == 1


def test_an_opted_in_write_that_timed_out_is_not_sent_again(wire):
    sent = wire([ReadTimeoutError(endpoint_url=URL)])
    with db.resubmit_safe_write():
        assert db.neptune_write(MERGE_WRITE, PARAMS) is False
    assert len(sent) == 1


def test_an_answer_reaches_the_app_after_one_send(wire):
    sent = wire([])
    assert db.neptune_query_checked(READ, PARAMS) == [{"code": "W-1"}]
    assert len(sent) == 1
    assert sent[0]["amz-sdk-request"].startswith(b"attempt=1")


# ── The transient errors botocore used to resend, now the app's call ────────

#: The service answered and is done with the request (throttled, failed,
#: unavailable or a conflict it aborted), or no connection was made at all.
FINISHED_OR_NOT_SENT = [
    _client_error("ThrottlingException", 429),
    _client_error("InternalServerException", 500),
    _client_error("ServiceUnavailable", 503),
    _client_error("ConflictException", 409, "Concurrent modification",
                  reason="CONCURRENT_MODIFICATION"),
    EndpointConnectionError(endpoint_url=URL),
    ConnectTimeoutError(endpoint_url=URL),
]
#: The connection dropped after the request may have reached the service, so
#: the first attempt may still be running.
DROPPED = [
    ConnectionClosedError(endpoint_url=URL),
    _client_error("BadGateway", 502),
]
#: The query outlasted the wait; it may still be running.
TIMED_OUT = [
    ReadTimeoutError(endpoint_url=URL),
    _client_error("GatewayTimeout", 504),
]
#: The request certainly never ran: no connection was made, or the service
#: rejected it with a 429 before running it.
CERTAINLY_NOT_RUN = [
    _client_error("ThrottlingException", 429),
    EndpointConnectionError(endpoint_url=URL),
    ConnectTimeoutError(endpoint_url=URL),
    ProxyConnectionError(proxy_url="http://proxy.example:3128"),
]
#: The service answered, but the query may have started (a 500 or a 503 says
#: nothing about how far it got), or it asked for a resubmit.
MAY_HAVE_RUN = [
    _client_error("InternalServerException", 500),
    _client_error("ServiceUnavailable", 503),
    _client_error("ConflictException", 409, "Concurrent modification",
                  reason="CONCURRENT_MODIFICATION"),
    _client_error("UnprocessableException", 422,
                  "Retry for SDK query requests is suppressed, please resubmit the query."),
]


def _ids(errors):
    return [type(e).__name__ + str((getattr(e, "response", None) or {}).get(
        "ResponseMetadata", {}).get("HTTPStatusCode", "")) for e in errors]


@pytest.mark.parametrize("error", FINISHED_OR_NOT_SENT, ids=_ids(FINISHED_OR_NOT_SENT))
def test_a_read_is_resubmitted_after_a_transient_error(graph, error):
    client = graph([error])
    assert db.neptune_query_checked(READ, PARAMS) == [{"code": "W-1"}]
    assert len(client.calls) == 2


#: The ends an opted-in write is also resubmitted after: the service asked
#: for a resubmit, failed (500), was unavailable (503) or aborted it on a
#: conflict (409).
OPT_IN_COVERS = [MAY_HAVE_RUN[3], MAY_HAVE_RUN[0], MAY_HAVE_RUN[1], MAY_HAVE_RUN[2]]
#: The ends it is not: a dropped connection, a timeout.
OPT_IN_DOES_NOT_COVER = DROPPED + TIMED_OUT


@pytest.mark.parametrize("cypher", WRITES, ids=["merge", "create"])
@pytest.mark.parametrize("error", CERTAINLY_NOT_RUN, ids=_ids(CERTAINLY_NOT_RUN))
def test_a_write_is_resubmitted_when_it_certainly_never_ran(graph, error, cypher):
    """No connection was made, or the service throttled the request (429)
    before running it: sending any write again cannot apply it twice."""
    client = graph([error])
    assert db.neptune_write(cypher, PARAMS) is True
    assert len(client.calls) == 2


@pytest.mark.parametrize("cypher", WRITES, ids=["merge", "create"])
@pytest.mark.parametrize("error", MAY_HAVE_RUN + DROPPED + TIMED_OUT,
                         ids=_ids(MAY_HAVE_RUN + DROPPED + TIMED_OUT))
def test_a_write_is_not_resubmitted_when_it_may_have_run(graph, error, cypher):
    client = graph([error])
    assert db.neptune_write(cypher, PARAMS) is False
    assert len(client.calls) == 1


@pytest.mark.parametrize("error", OPT_IN_COVERS, ids=_ids(OPT_IN_COVERS))
def test_an_opted_in_write_is_resubmitted_after_a_422_500_503_or_409(graph, error):
    client = graph([error])
    with db.resubmit_safe_write():
        assert db.neptune_write(MERGE_WRITE, PARAMS) is True
    assert len(client.calls) == 2


@pytest.mark.parametrize("error", CERTAINLY_NOT_RUN, ids=_ids(CERTAINLY_NOT_RUN))
def test_an_opted_in_write_is_resubmitted_when_it_certainly_never_ran(graph, error):
    client = graph([error])
    with db.resubmit_safe_write():
        assert db.neptune_write(MERGE_WRITE, PARAMS) is True
    assert len(client.calls) == 2


@pytest.mark.parametrize("error", OPT_IN_DOES_NOT_COVER, ids=_ids(OPT_IN_DOES_NOT_COVER))
def test_an_opted_in_write_is_not_resubmitted_after_a_drop_or_a_timeout(graph, error):
    client = graph([error])
    with db.resubmit_safe_write():
        assert db.neptune_write(MERGE_WRITE, PARAMS) is False
    assert len(client.calls) == 1


def test_a_throttled_write_stays_within_the_bounds(graph, no_wait):
    import agents.graph_resubmit as gr
    client = graph([_client_error("ThrottlingException", 429) for _ in range(gr.MAX_ATTEMPTS)])
    assert db.neptune_write(CREATE_WRITE, PARAMS) is False
    assert len(client.calls) == gr.MAX_ATTEMPTS


@pytest.mark.parametrize("error", DROPPED, ids=_ids(DROPPED))
def test_a_read_is_resubmitted_after_a_dropped_connection(graph, error):
    client = graph([error])
    assert db.neptune_query_checked(READ, PARAMS) == [{"code": "W-1"}]
    assert len(client.calls) == 2


@pytest.mark.parametrize("error", TIMED_OUT, ids=_ids(TIMED_OUT))
def test_a_read_is_not_resubmitted_after_a_timeout(graph, error, caplog):
    client = graph([error])
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        with pytest.raises(db.GraphQueryError):
            db.neptune_query_checked(READ, PARAMS)
    assert len(client.calls) == 1
    lines = [r.getMessage() for r in caplog.records if "not resubmitted" in r.getMessage()]
    assert len(lines) == 1 and "timed out" in lines[0]


@pytest.mark.parametrize("error", [
    _client_error("ValidationException", 400),
    _client_error("AccessDeniedException", 403),
    _client_error("UnprocessableException", 422, "Query timed out.", reason="QUERY_TIMEOUT"),
    HTTPClientError(error=RuntimeError("an unexpected failure while sending")),
    RuntimeError("not a botocore error"),
])
def test_other_errors_are_still_not_resubmitted(graph, error):
    client = graph([error])
    with pytest.raises(db.GraphQueryError):
        db.neptune_query_checked(READ, PARAMS)
    assert len(client.calls) == 1


# ── Logging: the first attempt's error, once per resubmitted query ──────────

def _lines(caplog, text):
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


def test_the_first_error_and_its_reason_are_logged_once_per_resubmitted_query(graph, caplog):
    graph([_client_error("ConflictException", 409, reason="CONCURRENT_MODIFICATION"),
           _client_error("UnprocessableException", 422,
                         "Retry for SDK query requests is suppressed, please resubmit the query.")])
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        assert db.neptune_query_checked(READ, PARAMS) == [{"code": "W-1"}]
    firsts = _lines(caplog, "first_error=")
    assert len(firsts) == 1
    assert "first_error=ConflictException" in firsts[0]
    assert "first_status=409" in firsts[0]
    assert "first_reason=CONCURRENT_MODIFICATION" in firsts[0]
    assert len(_lines(caplog, "resubmitted")) == 2


@pytest.mark.parametrize("cypher", WRITES, ids=["merge", "create"])
@pytest.mark.parametrize("error", OPT_IN_COVERS, ids=_ids(OPT_IN_COVERS))
def test_a_write_that_fails_at_once_logs_its_error_once(graph, caplog, error, cypher):
    graph([error])
    code, status = error.response["Error"]["Code"], error.response["ResponseMetadata"][
        "HTTPStatusCode"]
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        assert db.neptune_write(cypher, PARAMS) is False
    lines = _lines(caplog, "not resubmitted")
    assert len(lines) == 1
    assert f"code={code}" in lines[0] and f"status={status}" in lines[0]
    assert _lines(caplog, "resubmitted category=") == []


def test_a_write_resubmitted_then_refused_logs_the_first_error(graph, caplog):
    """A throttled write is resubmitted (its line names the 429 as the first
    error); a 500 on the resubmit fails it, logged once with its own code."""
    graph([_client_error("ThrottlingException", 429), _client_error("InternalServerException", 500)])
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        assert db.neptune_write(MERGE_WRITE, PARAMS) is False
    firsts = _lines(caplog, "first_error=")
    assert len(firsts) == 1 and "first_error=ThrottlingException" in firsts[0]
    refused = _lines(caplog, "not resubmitted")
    assert len(refused) == 1 and "code=InternalServerException" in refused[0]
