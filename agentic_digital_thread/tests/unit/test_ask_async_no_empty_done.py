# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The async ask path never reports an empty ``done``.

Workshop test event 2026-10-05: a long cross-system question through the Quick
query bridge came back ``{"status": "done", "answer": ""}`` after 110 s, and
Quick relayed nothing. ``_run_ask_job`` kept only ``MessageEvent`` text, so an
``ErrorEvent`` (or a blocking ``GuardrailEvent``) was dropped and whatever
text there was (none) was stored as ``done``.

Pins:
  * no public text -> ``error`` whose detail names ONLY a closed
    ``ERROR_CATEGORIES`` value: the error's category, else ``unknown``;
  * text + an error or a blocking guardrail -> ``done`` with the text and a
    one-line ``warning`` naming the same closed value; the poll route and the
    query bridge relay it;
  * the precise cause (``empty_answer``, the guardrail action) goes to ONE
    server-side warning line with the job id only — never the record, the
    question or upstream text;
  * plain text -> the unchanged ``done`` reply, no warning line.

Offline: the chat client is stubbed; JobStore runs memory-only.
"""

import ast
import asyncio
import inspect
import json
import logging
import textwrap
import time
import types

import pytest

import agents.server as server
from agents.events import (ERROR_CATEGORIES, DoneEvent, ErrorEvent,
                           GuardrailEvent, MessageEvent, MetadataEvent,
                           public_error_message)
from agents.job_store import JobStore
from agents.models import AskRequest

# Values that must never reach the job record or the log line.
_SECRET_TEXT = "row value 4471 leaked from the runtime"  # nosec B105 - a leak-detection marker, not a credential
_QUESTION = "which customers got lot 9931 from the stopped line?"
BLOCKS_ONLY = ("---VALUE-MAPPING-PROPOSAL---\nfrom_system: a\nto_system: b\n"
               "---END-PROPOSAL---\nadditional_candidate_pairs_observed: 0\n")
_BLOCKED = GuardrailEvent(source="OUTPUT", action="GUARDRAIL_INTERVENED")


def _error(category):
    return ErrorEvent(message=_SECRET_TEXT, details=_SECRET_TEXT,
                      category=category, correlation_id="async-x")


class _ScriptedClient:
    """AgentCoreChatClient stand-in that replays ``script``."""

    script: list = []

    def __init__(self):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True, **_k):
        assert write_memory is False
        yield from type(self).script


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(server, "get_config",
                        lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(server, "_ASK_RESULT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _ScriptedClient)
    _ScriptedClient.script = []
    yield


def _submit(script) -> str:
    """Submit one async ask over ``script``; return its job id once terminal."""
    _ScriptedClient.script = list(script)
    job_id = server.ask_async(AskRequest(question=_QUESTION))["job_id"]
    deadline = time.time() + 5
    while server._ask_jobs().get(job_id, {}).get("status") == "running":
        assert time.time() < deadline, "job never left 'running'"
        time.sleep(0.02)
    return job_id


def _run(script) -> "tuple[dict, str]":
    """The delivered poll reply for one async ask, and its job id."""
    job_id = _submit(script)
    return asyncio.run(server.ask_result(job_id)), job_id


def _failed(category):
    return {"status": "error", "error": "ask_failed",
            "error_detail": f"query failed: {category}"}


def _incomplete(answer, category):
    return {"status": "done", "answer": answer,
            "warning": f"answer may be incomplete: query reported {category}"}


def _log_lines(caplog, job_id) -> list:
    return [r.getMessage() for r in caplog.records
            if r.name == "agents.server" and job_id in r.getMessage()]


def _assert_logged(caplog, job_id, status, reasons):
    """Exactly ONE warning line names the job, its status and the reasons —
    and carries neither the question nor any upstream text."""
    lines = _log_lines(caplog, job_id)
    assert lines == [f"Async ask job {job_id} status={status} reasons={reasons}"]
    assert _QUESTION not in lines[0] and _SECRET_TEXT not in lines[0]


@pytest.fixture
def warnings_log(caplog):
    caplog.set_level(logging.WARNING, logger="agents.server")
    return caplog


def test_an_error_only_stream_is_an_error_naming_the_category_only(warnings_log):
    # The real chat client's failure tail: ErrorEvent, then an empty DoneEvent.
    result, job_id = _run([_error("timeout"), DoneEvent()])
    assert result == _failed("timeout")
    assert _SECRET_TEXT not in json.dumps(result)
    assert public_error_message("timeout") not in json.dumps(result)
    _assert_logged(warnings_log, job_id, "error",
                   "error_category=timeout,empty_answer")


def test_a_category_outside_the_taxonomy_degrades_to_unknown(warnings_log):
    result, job_id = _run([_error("not-a-category"), DoneEvent()])
    assert result == _failed("unknown")
    _assert_logged(warnings_log, job_id, "error",
                   "error_category=unknown,empty_answer")


def test_an_empty_stream_is_an_unknown_error_and_logs_empty_answer(warnings_log):
    result, job_id = _run([MetadataEvent(data={"streamBytes": 0}), DoneEvent()])
    assert result == _failed("unknown")
    _assert_logged(warnings_log, job_id, "error", "empty_answer")
    result, job_id = _run([MessageEvent(content="  \n\t ")])
    assert result == _failed("unknown")
    _assert_logged(warnings_log, job_id, "error", "empty_answer")


def test_a_blocks_only_reply_is_an_unknown_error(warnings_log):
    result, job_id = _run([MessageEvent(content=BLOCKS_ONLY), DoneEvent()])
    assert result == _failed("unknown")
    _assert_logged(warnings_log, job_id, "error", "empty_answer")


def test_a_blocking_guardrail_with_no_text_is_an_unknown_error(warnings_log):
    result, job_id = _run([_BLOCKED, DoneEvent()])
    assert result == _failed("unknown")
    assert "GUARDRAIL" not in json.dumps(result).upper()
    _assert_logged(warnings_log, job_id, "error",
                   "guardrail_action=GUARDRAIL_INTERVENED,empty_answer")


def test_an_unexpected_guardrail_action_is_logged_as_a_safe_token(warnings_log):
    odd = GuardrailEvent(action=f"blocked: {_SECRET_TEXT}")
    result, job_id = _run([odd, DoneEvent()])
    assert result == _failed("unknown")
    _assert_logged(warnings_log, job_id, "error",
                   "guardrail_action=OTHER,empty_answer")


def test_text_with_an_error_is_done_with_a_one_line_warning(warnings_log):
    result, job_id = _run([MessageEvent(content="Two of three lots shipped [1]."),
                           _error("stream_error"), DoneEvent()])
    assert result == _incomplete("Two of three lots shipped [1].", "stream_error")
    assert "\n" not in result["warning"]
    assert _SECRET_TEXT not in json.dumps(result)
    _assert_logged(warnings_log, job_id, "done", "error_category=stream_error")


def test_text_with_a_blocking_guardrail_is_done_with_a_one_line_warning(
        warnings_log):
    result, job_id = _run([MessageEvent(content="Lot 4 shipped to two sites."),
                           _BLOCKED, DoneEvent()])
    assert result == _incomplete("Lot 4 shipped to two sites.", "unknown")
    assert "GUARDRAIL" not in json.dumps(result).upper()
    _assert_logged(warnings_log, job_id, "done",
                   "guardrail_action=GUARDRAIL_INTERVENED")


def test_an_error_over_blocks_only_text_is_an_error_naming_the_category():
    result, _ = _run([MessageEvent(content=BLOCKS_ONLY), _error("runtime_error"),
                      DoneEvent()])
    assert result == _failed("runtime_error")


def test_a_normal_answer_is_unchanged_and_logs_nothing(warnings_log):
    result, job_id = _run([MessageEvent(content="Hello "),
                           MessageEvent(content="world"),
                           GuardrailEvent(action="NONE"), DoneEvent()])
    assert result == {"status": "done", "answer": "Hello world"}
    assert _log_lines(warnings_log, job_id) == []


@pytest.mark.parametrize("script", [
    [_error("timeout"), DoneEvent()],
    [_error("not-a-category")],
    [DoneEvent()],
    [MessageEvent(content=BLOCKS_ONLY)],
    [_BLOCKED],
    [MessageEvent(content="Partial."), _error("gateway_auth")],
    [MessageEvent(content="Partial."), _BLOCKED],
])
def test_every_public_detail_and_warning_is_a_closed_category(script):
    result, _ = _run(script)
    public = result.get("error_detail") or result.get("warning")
    assert public.rsplit(" ", 1)[-1] in ERROR_CATEGORIES


class _AppBackedHttpx:
    """httpx.Client stand-in whose GET is served by the app's poll route."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, timeout=None, params=None):
        payload = asyncio.run(server.ask_result(url.rsplit("/", 1)[-1]))
        return types.SimpleNamespace(raise_for_status=lambda: None,
                                     json=lambda: payload)


@pytest.fixture
def bridge(monkeypatch):
    import httpx
    from agents.query_bridge_server import get_query_result
    monkeypatch.setattr(httpx, "Client", _AppBackedHttpx)
    monkeypatch.setenv("ADT_APP_URL", "http://internal-alb.example")
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "test-token-value")
    return lambda job_id: json.loads(get_query_result(job_id))


def test_the_query_bridge_relays_an_error_tail_warning(bridge):
    job_id = _submit([MessageEvent(content="Partial."), _error("timeout"),
                      DoneEvent()])
    relayed = bridge(job_id)
    assert {k: relayed[k] for k in ("status", "answer", "warning")} == (
        _incomplete("Partial.", "timeout"))


def test_the_query_bridge_relays_a_guardrail_warning(bridge):
    job_id = _submit([MessageEvent(content="Partial."), _BLOCKED, DoneEvent()])
    relayed = bridge(job_id)
    assert {k: relayed[k] for k in ("status", "answer", "warning")} == (
        _incomplete("Partial.", "unknown"))


@pytest.mark.parametrize("name", [
    "_run_ask_job", "_collect_ask_stream", "_ask_job_record",
    "_blocking_guardrail_action", "ask_result"])
def test_changed_functions_stay_within_50_lines(name):
    source = textwrap.dedent(inspect.getsource(getattr(server, name)))
    node = ast.parse(source).body[0]
    assert node.end_lineno - node.lineno + 1 <= 50
