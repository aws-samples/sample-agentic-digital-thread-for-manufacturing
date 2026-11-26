# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The reserve an exact split leaves its caller holds against slow SDK calls.

A review of the reserve (``test_athena_exact_split_time_left``) found it held
only between calls: a slow client build, query submission or result page
could run past the split's deadline into the time the agent keeps for one
more query and a model turn, and the catalog calls a split stopped waiting for
were left running with no bound. Now:

1. the split reuses the Athena client that ran the refused query, so in the
   common path it builds no client and assumes no role;
2. a client the split builds itself (Glue, and Athena when there is none to
   reuse) has short connect and read timeouts and one retry, so one call on
   it ends within ``CALL_SECONDS_BOUND``;
3. the split's own thread waits for no SDK call past its deadline: each runs
   in a worker the split stops waiting for at the deadline, and after it the
   split waits at most ``DEADLINE_OVERRUN_SECONDS`` for its stop calls and its
   pieces. A caller that keeps a reserve has the split's deadline set that
   much earlier, so it keeps the whole reserve;
4. a metadata query is submitted with a ClientRequestToken; one whose answer
   was lost is submitted again with the same token (Athena returns that query
   and starts no other) and stopped, and one that answers after the split
   gave up is stopped, so no metadata scan is left running untracked;
5. calls the split stopped waiting for are tracked: while one is still
   running on a client a new split would use, that split is refused
   (``wall_time``) before it reads anything. How many may run in the process
   at once is bounded by permits (``test_athena_exact_split_permits``).

Offline except two tests that drive botocore's own read timeout against a
silent loopback server; the one through the agent's tool runs the split on a
clock that starts when the split does (``_athena_split_clock``). Synthetic
names only.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

import agents.athena_tools as at
from agents import athena_exact_split as sp
from agents import aws_session

from . import test_athena_exact_split as base
from . import test_athena_exact_split_or_states as ors
from . import test_system_agent_call_budget as cb
from ._athena_split_clock import split_clock
from ._athena_split_fakes import FakeLake
from .test_athena_exact_split_time_left import _Blocking, _scan_refusal

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_RESERVE_ENV = "SYSTEM_AGENT_SPLIT_RESERVE_SECONDS"


@pytest.fixture(autouse=True)
def _quick_polls_fresh_state(monkeypatch):
    monkeypatch.setattr(at, "_DEFAULT_POLL_INTERVAL", 0.01)
    for clear in (at.clear_schema_cache, at._PARTITION_COLUMNS_CACHE.clear, sp.clear_layout_cache):
        clear()
    yield
    for clear in (at.clear_schema_cache, at._PARTITION_COLUMNS_CACHE.clear, sp.clear_layout_cache):
        clear()


def _lake(**kwargs) -> FakeLake:
    return FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created",
                    cutoff=base._CUTOFF, **kwargs)


def _exact(athena, sql: str, cfg: dict | None = None, limit=None, glue=None) -> dict:
    lake = getattr(athena, "lake", None) or getattr(athena, "inner", None) or athena
    with sp.limited(limit):
        return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                         client=athena, glue_client=glue or lake.glue)


def _split(athena, glue, sql: str = base._P1, cfg: dict | None = None, limit=None):
    """The split alone (no plain query first), as ``compute_exact`` runs it."""
    lake = getattr(athena, "lake", None) or getattr(athena, "inner", None) or athena
    with sp.limited(limit):
        return sp.compute_exact(sql, {**_CFG, **(cfg or {})}, refusal=_scan_refusal(lake),
                                client=athena, glue_client=glue)


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


def _metadata(lake: FakeLake) -> list[str]:
    return [q for q in lake.started if "$snapshots" in q or "$files" in q]


def _deadline(seconds: float) -> sp.CapsLimit:
    return sp.CapsLimit(deadline=time.monotonic() + seconds)


def _drained(seconds: float = 3.0) -> bool:
    """Whether every call a split stopped waiting for has ended within ``seconds``."""
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if not sp._still_running():
            return True
        time.sleep(0.02)  # nosemgrep: arbitrary-sleep
    return not sp._still_running()


class _Tokens:
    """``lake`` as Athena answers a ClientRequestToken: a submission with a
    token it has seen returns that query and starts no other. The first
    submission of SQL containing ``fail_on`` raises ``error``, after creating
    the query when ``created`` (an answer lost on the way back) or before."""

    def __init__(self, lake: FakeLake, fail_on: str | None = None, error=None,
                 created: bool = True):
        self.lake, self.fail_on, self.error, self.created = lake, fail_on, error, created
        self.by_token: dict = {}
        self.submitted: list = []
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.lake, name)

    def start_query_execution(self, QueryString, ClientRequestToken=None, **kwargs):  # noqa: N803
        self.submitted.append((QueryString, ClientRequestToken))
        if ClientRequestToken is not None and ClientRequestToken in self.by_token:
            return {"QueryExecutionId": self.by_token[ClientRequestToken]}
        failing = bool(self.fail_on and self.fail_on in QueryString and not self.failed)
        if failing and not self.created:
            self.failed = True
            raise self.error
        out = self.lake.start_query_execution(QueryString=QueryString, **kwargs)
        if ClientRequestToken is not None:
            self.by_token[ClientRequestToken] = out["QueryExecutionId"]
        if failing:
            self.failed = True
            raise self.error
        return out


class _SlowSubmit:
    """``lake`` whose submission of SQL containing ``slow_on`` waits for
    ``release`` (at most ``seconds``) before it is made."""

    def __init__(self, lake: FakeLake, slow_on: str, seconds: float):
        self.lake, self.slow_on, self.seconds = lake, slow_on, seconds
        self.release = threading.Event()

    def __getattr__(self, name):
        return getattr(self.lake, name)

    def start_query_execution(self, QueryString, **kwargs):  # noqa: N803
        if self.slow_on in QueryString:
            self.release.wait(self.seconds)
        return self.lake.start_query_execution(QueryString=QueryString, **kwargs)


class _SilentServer:
    """A loopback HTTP server that reads each request and never answers,
    counting requests, until it is closed."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self.requests = 0
        self._open: list = []
        self._closed = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            conn.settimeout(5)
            try:
                if conn.recv(65536):
                    self.requests += 1
            except OSError:
                pass
            self._open.append(conn)

    def __enter__(self) -> "_SilentServer":
        return self

    def __exit__(self, *_exc) -> None:
        self._closed.set()
        for conn in self._open:
            conn.close()
        self._sock.close()


# --------------------------------------------------------------------------- #
# 1. The client that ran the refused query is reused
# --------------------------------------------------------------------------- #
def _counting(monkeypatch, lake: FakeLake) -> list:
    built = []

    def build(service, *_args, **kwargs):
        built.append((service, kwargs.get("config")))
        return lake.glue if service == "glue" else lake
    monkeypatch.setattr(at, "boto3_client_for", build)
    return built


def test_the_split_reuses_the_client_that_ran_the_refused_query(monkeypatch):
    lake = _lake()
    built = _counting(monkeypatch, lake)
    result = at.run_athena_query_exact(base._P1, dict(_CFG), source_system="lake-a",
                                       glue_client=lake.glue)
    assert result["exact_split"]["applied"] is True
    assert result["rows"] == lake.reference(base._P1)
    assert [service for service, _ in built] == ["athena"]       # the refused query's own


def test_an_or_split_reuses_the_client_that_ran_the_refused_query(monkeypatch):
    lake = ors._lake()
    built = _counting(monkeypatch, lake)
    result = at.run_athena_query_exact(ors._or_of_states(), dict(ors._CFG), glue_client=lake.glue)
    assert result["exact_split"]["branches"] == 3
    assert [service for service, _ in built] == ["athena"]


def test_the_refusal_carries_the_client_that_ran_the_query(monkeypatch):
    lake = _lake()
    _counting(monkeypatch, lake)
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        at.run_athena_query(base._P1, dict(_CFG))
    assert info.value.athena_client is lake
    assert "athena_client" not in json.dumps(info.value.to_dict())


# --------------------------------------------------------------------------- #
# 2. A client the split builds has short timeouts and one retry
# --------------------------------------------------------------------------- #
def _bounded(config) -> bool:
    return (config is not None
            and config.connect_timeout == sp.CLIENT_CONNECT_TIMEOUT_SECONDS
            and config.read_timeout == sp.CLIENT_READ_TIMEOUT_SECONDS
            and config.retries == {"total_max_attempts": sp.CLIENT_MAX_ATTEMPTS, "mode": "standard"})


def test_the_glue_client_the_split_builds_is_bounded(monkeypatch):
    lake = _lake()
    built = _counting(monkeypatch, lake)
    at.run_athena_query_exact(base._P1, dict(_CFG))
    glue = [config for service, config in built if service == "glue"]
    assert glue and all(_bounded(config) for config in glue)
    assert [config for service, config in built if service == "athena"] == [None]   # as before


def test_an_athena_client_the_split_must_build_is_bounded(monkeypatch):
    lake = _lake()
    built = _counting(monkeypatch, lake)
    result = sp.compute_exact(base._P1, dict(_CFG), refusal=_scan_refusal(lake),
                              glue_client=lake.glue)
    assert result["exact_split"]["applied"] is True
    athena = [config for service, config in built if service == "athena"]
    assert athena and all(_bounded(config) for config in athena)


def test_the_bound_is_the_timeouts_the_attempts_and_the_wait_between_them():
    assert sp.CALL_SECONDS_BOUND == (
        sp.CLIENT_MAX_ATTEMPTS * (sp.CLIENT_CONNECT_TIMEOUT_SECONDS + sp.CLIENT_READ_TIMEOUT_SECONDS)
        + (sp.CLIENT_MAX_ATTEMPTS - 1) * sp.CLIENT_RETRY_WAIT_SECONDS)


def test_a_client_config_reaches_the_client_and_none_keeps_the_old_one(monkeypatch):
    config = sp._client_config()
    cfg = {"region": "us-east-1"}
    assert aws_session.boto3_client_for("glue", cfg, config=config).meta.config.read_timeout == \
        sp.CLIENT_READ_TIMEOUT_SECONDS
    assert aws_session.boto3_client_for("glue", cfg).meta.config.read_timeout == 60
    # A cross-account client comes from the session the refused query already
    # assumed: no second AssumeRole.
    role = "arn:aws:iam::111122223333:role/reader"
    import boto3
    monkeypatch.setitem(aws_session._session_cache, (role, "", "us-east-1"),
                        boto3.Session(region_name="us-east-1"))
    monkeypatch.setattr(aws_session, "_assume_role_fetcher",
                        lambda *_a: pytest.fail("assumed a role"))
    client = aws_session.boto3_client_for("glue", {**cfg, "role_arn": role}, config=config)
    assert client.meta.config.connect_timeout == sp.CLIENT_CONNECT_TIMEOUT_SECONDS


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_one_call_on_a_client_the_split_builds_ends_within_the_bound(monkeypatch):
    # botocore's own read timeout, against a server that never answers.
    with _SilentServer() as server:
        monkeypatch.setenv("AWS_ENDPOINT_URL_GLUE", server.url)
        glue = sp._split_client("glue", at._resolve_config(dict(_CFG)))
        started = time.monotonic()
        with pytest.raises(ReadTimeoutError):
            glue.get_table(DatabaseName="lake_db", Name="events")
        took = time.monotonic() - started
    assert server.requests == sp.CLIENT_MAX_ATTEMPTS
    assert sp.CLIENT_MAX_ATTEMPTS * sp.CLIENT_READ_TIMEOUT_SECONDS <= took <= sp.CALL_SECONDS_BOUND


# --------------------------------------------------------------------------- #
# 3. The caller keeps its whole reserve
# --------------------------------------------------------------------------- #
def test_a_caller_that_keeps_a_reserve_also_keeps_the_overrun(monkeypatch):
    import types
    clock = {"t": 1000.0}
    monkeypatch.setattr(sp, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))
    with sp.limited(sp.CapsLimit(deadline=1030.0, reserve_seconds=15.0)):
        assert sp.resolve_caps({}).max_seconds == 15.0 - sp.DEADLINE_OVERRUN_SECONDS
    with sp.limited(sp.CapsLimit(deadline=1030.0)):
        assert sp.resolve_caps({}).max_seconds == 30.0          # a card or a notice: as before


def _bounded_waits(monkeypatch) -> list:
    """The timeouts the split gives its waits once it stops its pieces
    (``_stop_running``), in order: ``("stop", t)`` for the join of the thread
    stopping their queries, ``("pieces", t)`` for the wait for the running
    pieces. Read from the calls, so no real-time measurement is involved."""
    waits: list = []
    real_wait, real_start = sp.wait, sp._start_stopper

    def wait(fs, timeout=None, **kwargs):
        if kwargs.get("return_when") != sp.FIRST_COMPLETED:
            waits.append(("pieces", timeout))
        return real_wait(fs, timeout=timeout, **kwargs)

    def start_stopper(run, ids):
        stopper = real_start(run, ids)
        if stopper is not None:
            join = stopper.join

            def timed_join(timeout=None):
                waits.append(("stop", timeout))
                return join(timeout)
            stopper.join = timed_join
        return stopper
    monkeypatch.setattr(sp, "wait", wait)
    monkeypatch.setattr(sp, "_start_stopper", start_stopper)
    return waits


def test_after_its_deadline_the_split_waits_at_most_the_overrun_for_its_pieces(monkeypatch):
    # The pieces stay running, and stopping them hangs: the split returns
    # after waiting at most the overrun for its stop calls and its pieces,
    # and leaves them tracked. The bound is the timeouts the split gives
    # those waits, so a thread that wakes late cannot fail it.
    lake = _lake()
    lake.slow_piece_containing = "FOR VERSION AS OF"
    athena = _Blocking(lake, "stop_query_execution", 8.0)
    waits = _bounded_waits(monkeypatch)
    try:
        with pytest.raises(sp.ExactSplitRefused) as info:
            _split(athena, lake.glue, limit=_deadline(2.0))
        assert info.value.reason == "wall_time"
        assert [kind for kind, _ in waits] == ["stop", "pieces"], waits
        timeouts = [timeout for _, timeout in waits]
        assert all(t is not None and 0.0 <= t <= sp.DEADLINE_OVERRUN_SECONDS for t in timeouts), waits
        # One allowance for both: the wait for the pieces gets what the join left.
        assert timeouts[1] <= timeouts[0], waits
        assert sp._still_running()
    finally:
        athena.release.set()
    assert _drained()


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_a_hung_catalog_read_leaves_the_agent_its_reserve_and_is_tracked(monkeypatch):
    # The split builds its Glue client and the catalog read meets a server
    # that never answers: the agent's call returns before its reserve, and
    # the abandoned read ends on the client's own timeout.
    monkeypatch.delenv(_RESERVE_ENV, raising=False)
    lake = _lake()
    left = 15.0 + sp.DEADLINE_OVERRUN_SECONDS + 3.0
    guarded, state = cb._lake_tool(monkeypatch, lake, {}, left=left)
    real = aws_session.boto3_client_for
    monkeypatch.setattr(at, "boto3_client_for",
                        lambda service, *a, **k: real(service, *a, **k) if service == "glue" else lake)
    # The split starts with its 3 s however long the plain query before it
    # took, so it always reaches the catalog read rather than declining at
    # its start for want of time.
    clock = split_clock(monkeypatch, state, left)
    with _SilentServer() as server:
        monkeypatch.setenv("AWS_ENDPOINT_URL_GLUE", server.url)
        started = clock.now()
        payload = json.loads(guarded._tool_func(sql=base._P1, tool_context=cb._Ctx(state)))
        took = clock.now() - started
        assert payload["exact_split"]["reason"] == "wall_time", payload
        assert took <= left - 15.0                                 # the reserve is kept
        assert len(sp._still_running()) == 1
        assert _drained(sp.CALL_SECONDS_BOUND)                     # ended on its own timeout
        ended = clock.now() - started
        assert server.requests == sp.CLIENT_MAX_ATTEMPTS           # the read reached the server
    assert ended <= sp.CALL_SECONDS_BOUND + 1.0
    assert _metadata(lake) == [] and _pieces(lake) == []


# --------------------------------------------------------------------------- #
# 4. No metadata scan is left running untracked
# --------------------------------------------------------------------------- #
def test_each_metadata_read_is_submitted_with_its_own_token():
    lake = _lake()
    athena = _Tokens(lake)
    assert _exact(athena, base._P1)["exact_split"]["applied"] is True
    meta = [token for sql, token in athena.submitted if "$snapshots" in sql or "$files" in sql]
    rest = [token for sql, token in athena.submitted if not ("$snapshots" in sql or "$files" in sql)]
    assert meta and all(isinstance(t, str) and 32 <= len(t) <= 128 for t in meta)
    assert len(set(meta)) == len(meta)
    assert set(rest) == {None}                     # the plain query and the pieces: as before


def test_a_metadata_submission_whose_answer_was_lost_is_found_by_its_token_and_stopped():
    lake = _lake()
    athena = _Tokens(lake, fail_on="$snapshots", error=ReadTimeoutError(endpoint_url="https://x"))
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(athena, base._P1)
    assert info.value.exact_split["reason"] == "snapshot_unreadable"
    snapshots = [qid for qid, sql in lake._queries.items() if "$snapshots" in sql]
    assert len(snapshots) == 1 and snapshots[0] in lake.stopped    # found, not started twice
    tokens = [token for sql, token in athena.submitted if "$snapshots" in sql]
    assert len(tokens) == 2 and len(set(tokens)) == 1


def test_a_submission_the_service_refused_is_not_submitted_again():
    error = ClientError({"Error": {"Code": "InvalidRequestException", "Message": "no"},
                         "ResponseMetadata": {"HTTPStatusCode": 400}}, "StartQueryExecution")
    lake = _lake()
    athena = _Tokens(lake, fail_on="$snapshots", error=error, created=False)
    with pytest.raises(at.AthenaEngineScanLimitError):
        _exact(athena, base._P1)
    assert len([1 for sql, _ in athena.submitted if "$snapshots" in sql]) == 1
    assert not [qid for qid, sql in lake._queries.items() if "$snapshots" in sql]


def test_a_submission_that_answers_after_the_split_gave_up_is_stopped():
    lake = _lake()
    athena = _SlowSubmit(lake, "$snapshots", 8.0)
    started = time.monotonic()
    try:
        with pytest.raises(sp.ExactSplitRefused) as info:
            _split(athena, lake.glue, limit=_deadline(1.5))
        took = time.monotonic() - started
    finally:
        athena.release.set()
    assert info.value.reason == "wall_time" and took < 2.5
    assert _drained()
    snapshots = [qid for qid, sql in lake._queries.items() if "$snapshots" in sql]
    assert len(snapshots) == 1 and snapshots[0] in lake.stopped


# --------------------------------------------------------------------------- #
# 5. Calls the split stopped waiting for are bounded
# --------------------------------------------------------------------------- #
def test_a_client_with_a_call_still_running_is_not_used_again_until_it_ends():
    lake = _lake()
    athena = _Blocking(lake, "get_work_group", 30.0)
    try:
        with pytest.raises(sp.ExactSplitRefused):
            _split(athena, lake.glue, limit=_deadline(1.5))
        assert athena.calls == 1 and len(sp._still_running()) == 1
        # The same client, with time to spare: refused before it reads anything.
        started = time.monotonic()
        with pytest.raises(sp.ExactSplitRefused) as info:
            _split(athena, lake.glue, limit=_deadline(3.0))
        assert info.value.reason == "wall_time" and time.monotonic() - started < 0.5
        assert athena.calls == 1 and _metadata(lake) == []
        # Another client is not held up by it.
        other = _lake()
        assert _split(other, other.glue)["exact_split"]["applied"] is True
    finally:
        athena.release.set()
    assert _drained()
    assert _split(athena, lake.glue)["exact_split"]["applied"] is True


def test_an_or_type_read_out_of_time_refuses_and_its_glue_client_waits():
    # No caller deadline: the split's own 2 s cap. The OR combine's column
    # type read hangs; the split refuses rather than going on to a plain split
    # on the same Glue client, and that client is not used again while the
    # read runs.
    lake = ors._lake()
    glue = _Blocking(lake.glue, "get_table", 30.0)
    capped = {**ors._CFG, "exact_split_max_seconds": 2}
    try:
        with pytest.raises(sp.ExactSplitRefused) as info:
            sp.compute_exact(ors._or_of_states(), capped, refusal=_scan_refusal(lake),
                             client=lake, glue_client=glue)
        assert info.value.reason == "wall_time" and glue.calls == 1
        with pytest.raises(sp.ExactSplitRefused) as again:
            sp.compute_exact(ors._or_of_states(), {**ors._CFG, "exact_split_max_seconds": 3},
                             refusal=_scan_refusal(lake), client=lake, glue_client=glue)
        assert again.value.reason == "wall_time" and glue.calls == 1
        assert _metadata(lake) == [] and _pieces(lake) == []
    finally:
        glue.release.set()
    assert _drained()


def test_the_refusal_while_calls_run_is_not_read_as_a_transient_error():
    from agents.chat_client import classify_error_text
    from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS
    for reserve in (0.0, 15.0):
        text = sp._busy(reserve).to_dict()["detail"]
        low = text.lower()
        assert not any(m in low for m in _TRANSIENT_TOOL_ERROR_MARKERS + _AUTH_TOOL_ERROR_MARKERS)
        assert classify_error_text(text) != "timeout"
        assert "\u2014" not in text and "\u2013" not in text
    assert "narrower query" in sp._busy(15.0).to_dict()["detail"]
