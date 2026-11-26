# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the `query-bridge` MCP tool server (Quick integration).

The bridge is a stateless FastMCP tool server registered on the AgentCore
Gateway (only when quick_api_enabled). These tests assert:
  - the server exposes EXACTLY the three async-query tools, with read-only
    annotations on the poll/catalog tools (submit_query starts a job — no
    read-only hint);
  - submit_query / get_query_result / get_prompts send the QUICK_GATEWAY_TOKEN
    as the x-quick-auth header (the app's AuthMiddleware gate);
  - an unset ADT_APP_URL is reported honestly per call, never raised.

Offline: httpx transport is stubbed; no network.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from agents import query_bridge_server as qbs
from agents.query_bridge_server import (
    QUICK_GATEWAY_HEADER,
    TARGET_NAME,
    create_query_bridge_server,
    get_prompts,
    get_query_result,
    submit_query,
)


def _list_tools(mcp):
    """Enumerate the server's registered MCP tools (list_tools is async)."""
    return asyncio.run(mcp.list_tools())


def test_target_name_is_query_bridge():
    # Drives the gateway tool prefix: query-bridge___<tool>.
    assert TARGET_NAME == "query-bridge"


def test_server_exposes_exactly_the_three_async_query_tools():
    mcp = create_query_bridge_server()
    names = {t.name for t in _list_tools(mcp)}
    assert names == {"submit_query", "get_query_result", "get_prompts"}


def test_read_tools_carry_closed_world_read_only_annotations():
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    for name in ("get_query_result", "get_prompts"):
        payload = tools[name].model_dump(by_alias=True, exclude_none=True)
        assert payload["annotations"] == {
            "readOnlyHint": True,
            "openWorldHint": False,
        }


def test_submit_query_does_not_carry_a_read_only_annotation():
    # submit_query MUTATES (it starts a background job) — a readOnlyHint here
    # would let a client treat job creation as a safe idempotent read.
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    payload = tools["submit_query"].model_dump(by_alias=True, exclude_none=True)
    assert "annotations" not in payload


class _RecordingClient:
    """Stub httpx.Client capturing the request; returns a canned response."""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, content=None, headers=None):
        type(self).calls.append({"method": "POST", "url": url,
                                 "json": json, "headers": headers or {}})
        return _Resp({"job_id": "abc123", "status": "running"})

    def get(self, url, headers=None, timeout=None, params=None):
        type(self).calls.append({"method": "GET", "url": url,
                                 "headers": headers or {}})
        return _Resp({"status": "done", "answer": "42"})


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture()
def recording_client(monkeypatch):
    import httpx
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "test-token-value")
    return _RecordingClient


def test_submit_query_sends_the_gateway_token_header(recording_client):
    result = json.loads(submit_query("what changed at the plant?"))
    assert result == {"job_id": "abc123", "status": "running"}
    (call,) = recording_client.calls
    assert call["url"] == "http://internal-alb.example/api/ask/async"
    assert call["headers"][QUICK_GATEWAY_HEADER] == "test-token-value"


def test_get_query_result_sends_the_gateway_token_header(recording_client):
    result = json.loads(get_query_result("abc123"))
    assert result["status"] == "done"
    (call,) = recording_client.calls
    assert call["url"] == "http://internal-alb.example/api/ask/result/abc123"
    assert call["headers"][QUICK_GATEWAY_HEADER] == "test-token-value"


def test_get_prompts_reads_the_app_catalog_with_the_token(recording_client):
    result = json.loads(get_prompts())
    assert "prompts" in result
    (call,) = recording_client.calls
    assert call["url"] == "http://internal-alb.example/api/prompts"
    assert call["headers"][QUICK_GATEWAY_HEADER] == "test-token-value"


def test_unset_app_url_is_reported_honestly_never_raised(monkeypatch):
    monkeypatch.delenv("ADT_APP_URL", raising=False)
    assert json.loads(submit_query("q"))["status"] == "error"
    assert json.loads(get_query_result("j1"))["status"] == "error"
    # The greeting path must never hard-fail.
    assert json.loads(get_prompts())["prompts"] == []


def test_empty_question_and_job_id_are_rejected_without_http(monkeypatch):
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    assert "required" in json.loads(submit_query("  "))["error"]
    assert "required" in json.loads(get_query_result(""))["error"]


def test_gateway_headers_omit_the_header_when_no_token(monkeypatch):
    monkeypatch.delenv("QUICK_GATEWAY_TOKEN", raising=False)
    assert QUICK_GATEWAY_HEADER not in qbs._gateway_headers()


# --------------------------------------------------------------------------- #
# ADR-0079: the popped delegated claim is forwarded VERBATIM as x-adt-identity
# --------------------------------------------------------------------------- #
from agents.auth.delegated_identity import DELEGATED_IDENTITY_HEADER  # noqa: E402
from agents.runtime_asgi import identity_claim_var  # noqa: E402

_CLAIM = "payload-segment.signature-segment"


@pytest.fixture()
def claim_set():
    token = identity_claim_var.set(_CLAIM)
    yield _CLAIM
    identity_claim_var.reset(token)


@pytest.mark.parametrize("call", [
    lambda: submit_query("what changed?"),
    lambda: get_query_result("abc123"),
    lambda: get_prompts(),
], ids=["submit_query", "get_query_result", "get_prompts"])
def test_every_tool_forwards_the_claim_verbatim(recording_client, claim_set, call):
    call()
    (sent,) = recording_client.calls
    assert sent["headers"][DELEGATED_IDENTITY_HEADER] == _CLAIM
    assert sent["headers"][QUICK_GATEWAY_HEADER] == "test-token-value"


def test_no_claim_keeps_headers_byte_identical(monkeypatch):
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "test-token-value")
    token = identity_claim_var.set(None)
    try:
        assert qbs._gateway_headers() == {
            "Content-Type": "application/json",
            QUICK_GATEWAY_HEADER: "test-token-value",
        }
    finally:
        identity_claim_var.reset(token)


def test_bridge_never_verifies_or_parses_the_claim():
    import inspect
    src = inspect.getsource(qbs)
    for forbidden in ("verify_claim", "mint_claim", "identity_claim.", "secretsmanager"):
        assert forbidden not in src


def test_claim_reaches_the_tool_through_the_real_mcp_stack(monkeypatch):
    """End to end through the assembled ASGI app: the identity middleware pops
    ``_adt_identity`` before FastMCP validates arguments, and the popped value
    reaches the (threaded) tool via the context variable."""
    from starlette.testclient import TestClient
    from agents.auth.identity_claim import RESERVED_CLAIM_ARG
    from agents.runtime_asgi import _assemble_mcp_asgi
    import httpx

    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    app = _assemble_mcp_asgi(create_query_bridge_server(), "query-bridge")
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "submit_query",
                      "arguments": {"question": "q", RESERVED_CLAIM_ARG: _CLAIM}}}
    with TestClient(app) as tc:
        resp = tc.post("/mcp/", content=json.dumps(rpc), headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json"})
    assert resp.status_code == 200
    assert '"isError":false' in resp.text
    (sent,) = _RecordingClient.calls
    assert sent["headers"][DELEGATED_IDENTITY_HEADER] == _CLAIM
    assert sent["json"] == {"question": "q", "plant": ""}  # arg never reaches the body


# --- Quick agent behaviour seen live on event 24 (2026-10-03) ---------------
# Quick's agent gave up polling after ~2 minutes ("taking longer than
# expected") while the digital thread needed 9, and split one question into
# two submit_query calls so the second hit the app's one-at-a-time 429. The
# bridge is the only surface Quick reads, so its descriptions and its replies
# must carry the instruction.


def test_submit_query_description_says_once_and_never_split():
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    desc = tools["submit_query"].description.lower()
    assert "once" in desc and "never split" in desc
    assert "busy" in desc  # what to do when another query is running


def test_get_query_result_description_says_keep_polling_never_ask():
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    desc = tools["get_query_result"].description.lower()
    assert "ten minutes" in desc or "10 minutes" in desc
    assert "never stop" in desc
    assert "never ask the user" in desc


def test_both_descriptions_say_a_query_can_take_ten_minutes_or_more():
    # The Quick persona says a query can take ten minutes or more; the bridge
    # descriptions Quick also reads must not cap it at ten, and must say when
    # to stop polling.
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    for name in ("submit_query", "get_query_result"):
        desc = tools[name].description.lower()
        assert "can take ten minutes or more" in desc, name
        assert ("keep calling get_query_result with the same job_id while the status "
                "is running") in desc, name
        assert "stop on done, error or unknown" in desc, name
        assert "1 to 10 minutes" not in desc and "until its status is 'done'" not in desc, name
    poll = tools["get_query_result"].description.lower()
    assert "never resubmit" in poll
    assert "never ask the user whether to continue" in poll


def test_get_query_result_description_says_show_the_warning_with_the_answer():
    # A 'done' reply can carry a one-line warning (partial answer): Quick
    # learns the contract from this description, so it must say to surface
    # the warning with the answer and not treat the answer as complete.
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    desc = tools["get_query_result"].description.lower()
    assert "'warning'" in desc and "may be incomplete" in desc
    assert "show the warning to the user together with the answer" in desc
    assert "do not present the answer as complete" in desc


class _HoldingClient(_RecordingClient):
    """Base for stubs answering 'running': each GET advances a fake monotonic
    clock by the hold it asked the app for (the ``wait`` param), as the real
    app does. Without it the bridge re-polls the instant stub until its call
    budget runs low, which spent about 22 s of real time per test."""
    clock: list = [0.0]

    def _hold(self, params):
        type(self).clock[0] += float((params or {}).get("wait", 0.0))


def _use_holding_clock(monkeypatch, client):
    client.calls, client.clock = [], [0.0]
    monkeypatch.setattr(qbs.time, "monotonic", lambda: client.clock[0])


class _RunningClient(_HoldingClient):
    def get(self, url, headers=None, timeout=None, params=None):
        type(self).calls.append({"method": "GET", "url": url,
                                 "headers": headers or {}})
        self._hold(params)
        return _Resp({"status": "running", "job_id": "abc123"})


def test_running_poll_carries_next_action_and_elapsed(monkeypatch):
    import httpx
    qb = qbs
    _use_holding_clock(monkeypatch, _RunningClient)
    monkeypatch.setattr(httpx, "Client", _RunningClient)
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setattr(qb, "_JOB_FIRST_SEEN", {"abc123": 1000.0})
    monkeypatch.setattr(qb.time, "time", lambda: 1130.0)
    result = json.loads(get_query_result("abc123"))
    # Two app holds fit one call's budget, as with the real ~20 s hold.
    assert len(_RunningClient.calls) == 2
    assert result["status"] == "running"
    assert result["elapsed_seconds"] == 130
    assert "same job_id" in result["next_action"]
    assert "do not stop" in result["next_action"].lower()
    assert result["expected_total_seconds_up_to"] == 600


# --- 2026-10-19 walks: Quick ended its turn on a progress message ------------
# Twice Quick wrote "I'm continuing to wait for the results ... Hang tight!" to
# the user and stopped calling get_query_result until the user replied. The
# running reply said to call again, but not to call NOW, in the same turn, and
# not that a reply to the user is what stops the polling. It also gave the
# next call only as prose. The running reply now names the next call as data
# (next_tool, next_arguments, retry_after_seconds 0) and says plainly that a
# progress message ends the turn. Every key it carried before is kept.

_RUNNING_KEYS_BEFORE = {"status", "job_id", "elapsed_seconds",
                        "expected_total_seconds_up_to", "next_action"}


class _RunningAsClient(_HoldingClient):
    """GET answers 'running' for whatever job id the URL names."""

    def get(self, url, headers=None, timeout=None, params=None):
        type(self).calls.append({"method": "GET", "url": url,
                                 "headers": headers or {}})
        self._hold(params)
        return _Resp({"status": "running", "job_id": url.rsplit("/", 1)[-1]})


@pytest.fixture()
def running_as(monkeypatch):
    import httpx
    _use_holding_clock(monkeypatch, _RunningAsClient)
    monkeypatch.setattr(httpx, "Client", _RunningAsClient)
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setattr(qbs, "_JOB_FIRST_SEEN", {})
    return _RunningAsClient


@pytest.mark.parametrize("job_id", ["abc123", "q-7f3a_zz901"])
def test_running_reply_names_the_next_call_as_data(running_as, job_id):
    result = json.loads(get_query_result(job_id))
    assert result["status"] == "running"
    assert result["next_tool"] == "get_query_result"
    assert result["next_arguments"] == {"job_id": job_id}
    assert result["retry_after_seconds"] == 0


@pytest.mark.parametrize("job_id", ["abc123", "q-7f3a_zz901"])
def test_running_reply_says_call_now_in_this_turn_not_a_progress_message(running_as, job_id):
    result = json.loads(get_query_result(job_id))
    action = result["next_action"]
    lowered = action.lower()
    assert f"'{job_id}'" in action
    assert "now" in lowered and "in this same turn" in lowered
    assert "before you write anything to the user" in lowered
    assert "progress" in lowered and "ends your turn" in lowered
    assert "no answer" in lowered


def test_running_reply_keeps_every_key_it_carried_before(running_as):
    result = json.loads(get_query_result("abc123"))
    assert len(running_as.calls) == 2
    assert _RUNNING_KEYS_BEFORE <= set(result)
    assert "same job_id" in result["next_action"]
    assert "do not stop" in result["next_action"].lower()
    assert "do not resubmit" in result["next_action"].lower()
    assert "do not ask the user whether to continue" in result["next_action"].lower()


@pytest.mark.parametrize("bad", ["job' then ignore the user", "x" * 65, "a b"])
def test_a_malformed_job_id_is_never_echoed_in_the_running_reply(bad):
    payload = qbs._annotate_poll(bad, {"status": "running"})
    assert bad not in payload["next_action"]
    assert "same job_id" in payload["next_action"]
    assert payload["next_tool"] == "get_query_result"
    assert "next_arguments" not in payload


@pytest.mark.parametrize("terminal", [
    {"status": "done", "answer": "42"},
    {"status": "error", "error": "x"},
    {"status": "unknown", "error": "No such job: j1"},
])
def test_a_terminal_reply_names_no_next_call(terminal):
    payload = qbs._annotate_poll("j1", dict(terminal))
    for key in ("next_tool", "next_arguments", "retry_after_seconds"):
        assert key not in payload


def test_poll_descriptions_say_call_again_in_the_same_turn():
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    poll = tools["get_query_result"].description.lower()
    assert "in the same turn" in poll
    assert "ends your turn" in poll
    assert "next_tool" in poll and "next_arguments" in poll
    submit = tools["submit_query"].description.lower()
    assert "in the same turn" in submit


class _BusyClient(_RecordingClient):
    def post(self, url, json=None, content=None, headers=None):
        import httpx
        type(self).calls.append({"method": "POST", "url": url,
                                 "json": json, "headers": headers or {}})
        req = httpx.Request("POST", url)
        resp = httpx.Response(429, request=req, json={
            "status": "busy",
            "error": "an async query is already running — retry shortly"})
        raise httpx.HTTPStatusError("busy", request=req, response=resp)


def test_submit_busy_is_structured_and_points_at_the_running_job(monkeypatch):
    import httpx
    qb = qbs
    _BusyClient.calls = []
    monkeypatch.setattr(httpx, "Client", _BusyClient)
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setattr(qb, "_JOB_FIRST_SEEN", {"job-earlier": 5.0})
    result = json.loads(submit_query("second half of the question"))
    assert result["status"] == "busy"
    assert result["retry_after_seconds"] == 20
    assert result["running_job_id"] == "job-earlier"
    assert "do not submit again" in result["next_action"].lower()
    assert "get_query_result" in result["next_action"]


def test_submit_remembers_the_job_for_elapsed_tracking(recording_client, monkeypatch):
    qb = qbs
    monkeypatch.setattr(qb, "_JOB_FIRST_SEEN", {})
    json.loads(submit_query("what changed at the plant?"))
    assert "abc123" in qb._JOB_FIRST_SEEN


def test_done_answer_tells_the_client_to_keep_the_figures(recording_client):
    """Event 31: Quick rewrote correct totals when it reused them. A done
    reply with an answer says to keep each stated figure and its label exactly
    and never recompute. It does NOT ask for the whole answer verbatim: an
    answer can carry working notes ("Now I need to find..."), and the client
    may summarize the wording around the figures."""
    result = json.loads(get_query_result("abc123"))
    assert result["status"] == "done" and result["answer"] == "42"
    action = result["next_action"].lower()
    assert "with its label exactly as written" in action
    assert "do not recompute, multiply, re-total" in action
    assert "not confirmed" in action
    assert "you may summarize" in action
    assert "show this answer" not in action  # no whole-answer verbatim order


def test_error_reply_carries_no_figure_instruction():
    payload = qbs._annotate_poll("j1", {"status": "error", "error": "x"})
    assert "next_action" not in payload


def test_done_poll_forgets_the_job(recording_client, monkeypatch):
    qb = qbs
    monkeypatch.setattr(qb, "_JOB_FIRST_SEEN", {"abc123": 1.0})
    assert json.loads(get_query_result("abc123"))["status"] == "done"
    assert "abc123" not in qb._JOB_FIRST_SEEN


# --- the busy reply names only the caller's own job (customer-share S2) -----
# With delegated identity (ADR-0079) several users can share one bridge
# process. The bridge's local memory spans every caller, so with a claim on
# the call the busy reply uses ONLY the job id the app returned (the app names
# a job only to the user who owns it). Without a claim (service auth, one user
# per deployment, the workshop path) the reply is as before.

_BUSY_KEYS = {"status", "running_job_id", "retry_after_seconds", "next_action"}


class _TwoCallerClient:
    """Caller A's submit is accepted; every other submit gets the app's 429.

    ``busy_body`` is the 429 JSON the app returns (set per test)."""

    busy_body: object = {"status": "busy"}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, content=None, headers=None):
        import httpx
        req = httpx.Request("POST", url)
        if (headers or {}).get(DELEGATED_IDENTITY_HEADER) == "claim-A":
            return httpx.Response(200, request=req,
                                  json={"job_id": "job-of-A", "status": "running"})
        body = type(self).busy_body
        resp = (httpx.Response(429, request=req, json=body) if isinstance(body, dict)
                else httpx.Response(429, request=req, content=str(body).encode()))
        raise httpx.HTTPStatusError("busy", request=req, response=resp)


@pytest.fixture()
def two_callers(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "Client", _TwoCallerClient)
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "t")
    monkeypatch.setattr(qbs, "_JOB_FIRST_SEEN", {})
    _TwoCallerClient.busy_body = {"status": "busy"}
    return _TwoCallerClient


def _submit_as(claim, question):
    token = identity_claim_var.set(claim)
    try:
        return json.loads(submit_query(question))
    finally:
        identity_claim_var.reset(token)


def test_busy_reply_never_points_b_at_a_job(two_callers):
    # The Stage-1 repro, now a regression test: the app names no job to B
    # (A owns it), and the bridge must not fill one in from its own memory.
    assert _submit_as("claim-A", "A's question")["job_id"] == "job-of-A"
    busy = _submit_as("claim-B", "B's question")
    assert set(busy) == _BUSY_KEYS
    assert busy["status"] == "busy"
    assert busy["running_job_id"] == ""
    assert "job-of-A" not in busy["next_action"]
    assert "for this user" not in busy["next_action"]
    assert "on this deployment" in busy["next_action"]
    assert "submit_query once more" in busy["next_action"]
    assert "get_query_result" not in busy["next_action"]
    assert busy["retry_after_seconds"] == 20


def test_delegated_caller_gets_the_job_the_app_names(two_callers):
    _submit_as("claim-A", "A's question")  # bridge memory now holds job-of-A
    two_callers.busy_body = {"status": "busy", "running_job_id": "job-of-b1"}
    busy = _submit_as("claim-B", "B's second half")
    assert set(busy) == _BUSY_KEYS
    assert busy["running_job_id"] == "job-of-b1"
    assert "job_id 'job-of-b1'" in busy["next_action"]
    assert "for this user" in busy["next_action"]
    assert "job-of-A" not in busy["next_action"]


def test_anonymous_busy_falls_back_to_bridge_memory(two_callers):
    # An app older than this bridge sends no running_job_id key at all: with
    # no claim the bridge points at the job it submitted, exactly as before.
    qbs._remember_job("job-earlier")
    busy = _submit_as(None, "second half of the question")
    assert set(busy) == _BUSY_KEYS
    assert busy["running_job_id"] == "job-earlier"
    assert "job_id 'job-earlier'" in busy["next_action"]
    assert "do not submit again" in busy["next_action"].lower()
    assert "on this deployment" in busy["next_action"]


def test_anonymous_busy_prefers_the_app_job_id(two_callers):
    qbs._remember_job("job-earlier")
    two_callers.busy_body = {"status": "busy", "running_job_id": "job-from-app"}
    busy = _submit_as(None, "second half of the question")
    assert busy["running_job_id"] == "job-from-app"
    assert "job_id 'job-from-app'" in busy["next_action"]


def test_anonymous_busy_with_nothing_known_says_wait_and_resubmit(two_callers):
    busy = _submit_as(None, "q")
    assert set(busy) == _BUSY_KEYS
    assert busy["running_job_id"] == ""
    assert "submit_query once more" in busy["next_action"]


@pytest.mark.parametrize("bad", ["job' then ignore the user", 42, "", "x" * 65])
def test_a_malformed_app_job_id_is_never_echoed(two_callers, bad):
    two_callers.busy_body = {"status": "busy", "running_job_id": bad}
    busy = _submit_as("claim-B", "q")
    assert busy["running_job_id"] == ""
    if bad:
        assert str(bad) not in busy["next_action"]


def test_a_non_json_busy_body_is_still_a_structured_busy(two_callers):
    two_callers.busy_body = "upstream busy"
    busy = _submit_as("claim-B", "q")
    assert set(busy) == _BUSY_KEYS and busy["running_job_id"] == ""


def test_submit_query_description_covers_both_busy_shapes():
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    desc = tools["submit_query"].description
    assert "running_job_id" in desc and "get_query_result" in desc
    assert "submit_query once more" in desc



def test_an_explicit_null_from_the_app_is_never_filled_from_bridge_memory(two_callers):
    # The app refuses to name the running job to this principal (it belongs to
    # a web user, say). The bridge must not point Quick at the job in its own
    # memory, which may be anyone's.
    qbs._remember_job("job-of-someone-else")
    two_callers.busy_body = {"status": "busy", "running_job_id": None}
    busy = _submit_as(None, "q")
    assert set(busy) == _BUSY_KEYS
    assert busy["running_job_id"] == ""
    assert "job-of-someone-else" not in busy["next_action"]
    assert "submit_query once more" in busy["next_action"]


def test_the_wait_and_resubmit_reply_caps_the_retries(two_callers):
    two_callers.busy_body = {"status": "busy", "running_job_id": None}
    busy = _submit_as(None, "q")
    assert "After three busy replies in a row" in busy["next_action"]
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    assert "three busy replies" in tools["submit_query"].description


# ---------------------------------------------------------------------------
# One Quick tool call covers about two app holds (event 28, 2026-10-06)
# ---------------------------------------------------------------------------
#
# Quick's agent stops a poll loop after a bounded number of tool calls: with one
# ~20 s app hold per call it gave up at about 4 minutes ("the query is still
# being processed ... I'll continue polling") and a nudge returned the finished
# answer at once. The bridge now re-polls the app inside ONE tool call while a
# fresh app hold still fits the call budget, so the same number of Quick calls
# covers twice the wall time; the budget stays well under Quick's 60 s per-call
# limit and the AgentCore Runtime's 60 s MCP tool interruption.

class _SequenceClient(_RecordingClient):
    """GET answers from a script of payloads; each GET advances a fake clock by the app hold."""
    script: list = []
    clock: list = [0.0]

    def get(self, url, headers=None, timeout=None, params=None):
        type(self).calls.append({"method": "GET", "url": url, "headers": headers or {}})
        type(self).clock[0] += qbs._APP_POLL_HOLD_SECONDS + 1.0
        payload = type(self).script.pop(0) if type(self).script else {"status": "running"}
        return _Resp(dict(payload))


@pytest.fixture()
def sequence_client(monkeypatch):
    import httpx
    _SequenceClient.calls = []
    _SequenceClient.clock = [0.0]
    monkeypatch.setattr(httpx, "Client", _SequenceClient)
    monkeypatch.setattr(qbs.time, "monotonic", lambda: _SequenceClient.clock[0])
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setattr(qbs, "_JOB_FIRST_SEEN", {})
    return _SequenceClient


def test_poll_budget_sits_between_quick_and_runtime_limits():
    assert 2 * qbs._APP_POLL_HOLD_SECONDS < qbs.POLL_CALL_BUDGET_SECONDS < 60


class _TimedClient(_RecordingClient):
    """GETs from a script of (seconds the GET takes, payload or exception);
    records each GET's timeout and advances a fake clock. A GET that would
    outlast its timeout raises httpx.ReadTimeout at the timeout instead."""
    script: list = []
    clock: list = [0.0]
    timeouts: list = []

    def get(self, url, headers=None, timeout=None, params=None):
        import httpx
        type(self).calls.append({"method": "GET", "url": url, "headers": headers or {},
                                 "params": params or {}})
        type(self).timeouts.append(timeout)
        took, outcome = type(self).script.pop(0)
        if timeout is not None and took > timeout:
            type(self).clock[0] += timeout
            raise httpx.ReadTimeout("timed out")
        type(self).clock[0] += took
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(dict(outcome))


@pytest.fixture()
def timed_client(monkeypatch):
    import httpx
    _TimedClient.calls, _TimedClient.timeouts, _TimedClient.clock = [], [], [0.0]
    monkeypatch.setattr(httpx, "Client", _TimedClient)
    monkeypatch.setattr(qbs.time, "monotonic", lambda: _TimedClient.clock[0])
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setattr(qbs, "_JOB_FIRST_SEEN", {})
    return _TimedClient


def test_every_get_timeout_is_capped_by_what_is_left_of_the_call_budget(timed_client):
    timed_client.script = [(21.0, {"status": "running"}), (20.0, {"status": "done", "answer": "42"})]
    assert json.loads(get_query_result("abc123")) == {"status": "done", "answer": "42",
                                                      "next_action": qbs._DONE_NEXT_ACTION}
    first, second = timed_client.timeouts
    budget, allowance = qbs.POLL_CALL_BUDGET_SECONDS, qbs._POLL_OVERHEAD_SECONDS
    assert first == pytest.approx(budget - allowance)
    assert second == pytest.approx(budget - 21.0 - allowance)


def test_a_late_first_hold_and_a_slow_second_stay_inside_the_call_budget(timed_client):
    """Running at 21.9 s, then a second GET that would take
    45 s, used to run the call to about 67 s, past Quick's and the runtime's 60 s."""
    timed_client.script = [(21.9, {"status": "running"}), (45.0, {"status": "done", "answer": "late"})]
    result = json.loads(get_query_result("abc123"))
    assert result["status"] == "running" and "same job_id" in result["next_action"]
    assert timed_client.clock[0] <= qbs.POLL_CALL_BUDGET_SECONDS
    assert len(timed_client.calls) == 2


def test_a_lost_connection_on_a_later_hold_returns_the_running_reply_in_hand(timed_client):
    import httpx
    timed_client.script = [(21.0, {"status": "running"}), (1.0, httpx.ConnectError("dropped"))]
    result = json.loads(get_query_result("abc123"))
    assert result["status"] == "running" and "same job_id" in result["next_action"]


def test_a_failure_on_the_first_hold_is_still_an_error(timed_client):
    import httpx
    timed_client.script = [(1.0, httpx.ConnectError("refused"))]
    result = json.loads(get_query_result("abc123"))
    assert result["status"] == "error" and "ConnectError" in result["error"]


def test_every_get_asks_the_app_to_hold_inside_its_own_timeout(timed_client):
    """A hold that outlasts the bridge's timeout sends its reply to a GET the
    bridge no longer reads, and the call ends on a stale 'running'."""
    timed_client.script = [(21.0, {"status": "running"}), (18.0, {"status": "running"})]
    get_query_result("abc123")
    waits = [float(c["params"]["wait"]) for c in timed_client.calls]
    assert waits[0] == qbs._APP_POLL_HOLD_SECONDS
    for wait, timeout in zip(waits, timed_client.timeouts):
        assert wait <= timeout - qbs._HOLD_MARGIN_SECONDS + 0.05


class _AppSim:
    """The result route's hold, simulated: re-reads every 1.5 s, honours
    ``wait`` against wall time from the request's start (else its own ~21 s
    hold). It models an app from BEFORE repeatable delivery (2026-10-16): it
    frees a finished job as it delivers it (a read plus a delete), even when
    the caller has already stopped listening. The current app never frees
    one, but the bridge (an AgentCore runtime) and the app (ECS) roll out
    separately, and an answer that lands inside the GET that asked for it is
    the stronger property, so the sweep below keeps this stricter model."""

    def __init__(self, finish_at: float, read_latency: float = 0.05):
        self.clock = [0.0]
        self.finish_at = finish_at
        self.read_latency = read_latency
        self.freed = False
        self.calls: list = []
        sim = self

        class _Client(_RecordingClient):
            def get(self, url, headers=None, timeout=None, params=None):
                import httpx
                sim.calls.append({"timeout": timeout, "params": params})
                start = sim.clock[0]
                held = params and "wait" in params
                hold = float(params["wait"]) if held else server_default_hold()
                waited, t = 0.0, start + sim.read_latency
                while not (sim.freed or t >= sim.finish_at):
                    if held:
                        if waited + 1.5 > hold or t - start + 1.5 > hold:
                            break
                    elif waited >= 20.0:
                        break
                    waited += 1.5
                    t += 1.5 + sim.read_latency
                done = not sim.freed and t >= sim.finish_at
                if sim.freed:
                    payload = {"status": "unknown"}
                elif done:
                    sim.freed = True
                    t += sim.read_latency  # the delivery's delete
                    payload = {"status": "done", "answer": "42"}
                else:
                    payload = {"status": "running"}
                if timeout is not None and t - start > timeout:
                    sim.clock[0] = start + timeout
                    raise httpx.ReadTimeout("timed out")
                sim.clock[0] = t
                return _Resp(payload)

        self.client = _Client


def server_default_hold() -> float:
    return 21.0


@pytest.mark.parametrize("read_latency", [0.05, 0.2, 0.5])
@pytest.mark.parametrize("finish_at", [x / 4 for x in range(0, 4 * 130)])
def test_a_finished_answer_is_never_lost_whenever_it_lands(monkeypatch, finish_at, read_latency):
    """Sweep the job's finish time across three call budgets at three record
    read latencies, against the freeing app model: the answer is delivered
    exactly once and no poll ever sees 'unknown'."""
    import httpx
    sim = _AppSim(finish_at, read_latency)
    monkeypatch.setattr(httpx, "Client", sim.client)
    monkeypatch.setattr(qbs.time, "monotonic", lambda: sim.clock[0])
    monkeypatch.setattr(qbs.time, "time", lambda: sim.clock[0])
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setattr(qbs, "_JOB_FIRST_SEEN", {})
    for _ in range(12):
        call_start = sim.clock[0]
        result = json.loads(get_query_result("abc123"))
        assert sim.clock[0] - call_start <= qbs.POLL_CALL_BUDGET_SECONDS
        assert result["status"] in ("running", "done"), result
        if result["status"] == "done":
            assert result["answer"] == "42"
            return
    pytest.fail("never delivered")


# --- 2026-10-16: a done reply that reaches a GET the bridge gave up on ------
# A Quick lot-total ask (on a rehearsal account) finished while the second
# GET of a tool call was held. That GET came back after the bridge's timeout,
# so the call ended on the 'running' already in hand (correct, see
# test_a_late_first_hold_and_a_slow_second_stay_inside_the_call_budget). The
# app had popped the record as it built the 'done' reply for that GET, so the
# next call got 'No such job' and the answer was lost. The app now answers a
# finished job the same way on every poll until its record expires.

class _AppServedClient(_RecordingClient):
    """GETs answered by the app's REAL result route over a memory JobStore,
    from a script of (seconds the GET takes, hook run before the app serves
    it). A GET slower than its timeout is still served in full (the app builds
    and sends its reply; uvicorn does not cancel the handler) and then raises
    httpx.ReadTimeout at the timeout: the reply reached a GET the bridge had
    already given up on."""
    script: list = []
    clock: list = [0.0]
    served: list = []

    def get(self, url, headers=None, timeout=None, params=None):
        import httpx
        from agents import server
        took, before = type(self).script.pop(0)
        if before is not None:
            before()
        wait = float((params or {}).get("wait", 0.0))
        payload = asyncio.run(server.ask_result(url.rsplit("/", 1)[-1], wait=wait))
        type(self).served.append(payload)
        if timeout is not None and took > timeout:
            type(self).clock[0] += timeout
            raise httpx.ReadTimeout("timed out")
        type(self).clock[0] += took
        return _Resp(dict(payload))


@pytest.fixture()
def app_served(monkeypatch):
    import httpx
    import time as _time
    import types
    from agents import server
    from agents.job_store import JobStore

    async def _no_sleep(_seconds):
        return None

    _AppServedClient.script, _AppServedClient.clock, _AppServedClient.served = [], [0.0], []
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)          # the app's hold steps
    monkeypatch.setattr(httpx, "Client", _AppServedClient)
    monkeypatch.setattr(qbs, "time", types.SimpleNamespace(
        monotonic=lambda: _AppServedClient.clock[0], time=_time.time))
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setattr(qbs, "_JOB_FIRST_SEEN", {})
    return _AppServedClient


def test_a_done_reply_the_bridge_gave_up_on_is_delivered_by_the_next_call(app_served):
    import time as _time
    from agents import server
    jobs = server._ask_jobs()
    jobs["lot4"] = {"status": "running", "started_at": _time.time()}

    def finish():
        assert jobs.finish_if_running("lot4", {"status": "done",
                                               "answer": "Lot 4 total: 13,233,115"})

    app_served.script = [(21.9, None),     # call 1, first hold: still running
                         (45.0, finish),   # second hold: done, but the reply misses the GET
                         (1.0, None)]      # call 2
    first = json.loads(get_query_result("lot4"))
    assert first["status"] == "running" and "same job_id" in first["next_action"]
    assert app_served.served[-1] == {"status": "done", "answer": "Lot 4 total: 13,233,115"}
    second = json.loads(get_query_result("lot4"))
    assert second == {"status": "done", "answer": "Lot 4 total: 13,233,115",
                      "next_action": qbs._DONE_NEXT_ACTION}


def test_one_tool_call_re_polls_the_app_while_a_hold_still_fits(sequence_client):
    sequence_client.script = [{"status": "running"}, {"status": "done", "answer": "42"}]
    result = json.loads(get_query_result("abc123"))
    assert result == {"status": "done", "answer": "42", "next_action": qbs._DONE_NEXT_ACTION}
    assert len(sequence_client.calls) == 2  # running at ~21 s, done at ~42 s: one Quick call


def test_one_tool_call_stops_re_polling_when_the_next_hold_would_overrun(sequence_client):
    sequence_client.script = [{"status": "running"}, {"status": "running"}, {"status": "running"}]
    result = json.loads(get_query_result("abc123"))
    assert result["status"] == "running" and "same job_id" in result["next_action"]
    # Two holds fit the budget; a third (~63 s) would cross the 60 s limits.
    assert len(sequence_client.calls) == 2


def test_description_states_the_longer_hold():
    tools = {t.name: t for t in _list_tools(create_query_bridge_server())}
    desc = tools["get_query_result"].description.lower()
    assert "about 40 seconds" in desc


# --- an HTTP error never hands the upstream body to the caller --------------
# The app's (or an intermediary's) error body can carry validation input,
# internal ids, hosts or service detail. The tool result carries the HTTP
# status, a fixed category message and a reference; the body is logged
# server-side under that reference, through the shared credential redaction.

_LEAKY_BODY = ('{"detail": "upstream db internal-db.corp.example:5432 refused; '
               'password=swordfish Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig"}')


class _ErrorClient:
    status = 500

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _raise(self, method, url):
        import httpx
        req = httpx.Request(method, url)
        resp = httpx.Response(type(self).status, request=req, content=_LEAKY_BODY.encode())
        raise httpx.HTTPStatusError("boom", request=req, response=resp)

    def post(self, url, json=None, content=None, headers=None):
        self._raise("POST", url)

    def get(self, url, headers=None, timeout=None, params=None):
        self._raise("GET", url)


@pytest.fixture()
def error_client(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "Client", _ErrorClient)
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "t")
    _ErrorClient.status = 500
    return _ErrorClient


@pytest.mark.parametrize("call,label", [
    (lambda: submit_query("what changed?"), "submit failed HTTP"),
    (lambda: get_query_result("abc123"), "result failed HTTP"),
], ids=["submit", "result"])
@pytest.mark.parametrize("status,category", [
    (500, "upstream_error"), (502, "upstream_error"), (400, "upstream_error"),
    (404, "not_found"), (403, "auth_failed")])
def test_http_error_reply_carries_status_and_reference_not_the_body(
        error_client, caplog, call, label, status, category):
    error_client.status = status
    with caplog.at_level("WARNING"):
        result = json.loads(call())
    assert result["status"] == "error"
    assert result["error"].startswith(f"{label} {status}: ")
    assert "Reference: " in result["error"]
    assert result["error_category"] == category
    assert result["error_ref"] and result["error_ref"] in result["error"]
    raw = json.dumps(result)
    for leaked in ("internal-db", "swordfish", "eyJhbGci", "refused", "detail"):
        assert leaked not in raw
    # The body is logged under the same reference, credentials redacted.
    line = " ".join(r.getMessage() for r in caplog.records
                    if result["error_ref"] in r.getMessage())
    assert "refused" in line
    assert "swordfish" not in line and "eyJhbGciOiJIUzI1NiJ9" not in line


def test_http_401_still_drops_the_cached_token(error_client, monkeypatch):
    dropped = []
    monkeypatch.setattr(qbs, "forget_cached_token", lambda: dropped.append(True))
    error_client.status = 401
    result = json.loads(submit_query("what changed?"))
    assert result["error"].startswith("submit failed HTTP 401: ")
    assert result["error_category"] == "auth_failed"
    assert dropped == [True]
