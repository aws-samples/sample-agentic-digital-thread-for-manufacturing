# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The chat client can invoke with a caller's runtime session id, and stop it.

The async ask path mints the AgentCore runtime session id itself so it can
stop an abandoned run later (test_ask_async_stop_abandoned.py). The client
uses a given id verbatim, mints its usual one otherwise, refuses a malformed
one before any invoke, and ``stop_runtime_session`` reports a closed outcome
token instead of raising. Offline: boto3 is mocked.
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import agents.chat_client as cc
from agents.events import ErrorEvent

ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_orchestrator-abc"
GIVEN = "session-" + "c" * 28


class _EmptyStream:
    def iter_lines(self):
        return iter([])


def _invoke(runtime_session_id=None):
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"response": _EmptyStream()}
    with patch("agents.chat_client.boto3.client", return_value=fake):
        client = cc.AgentCoreChatClient(runtime_arn=ARN, region="us-east-1")
        events = list(client.stream(prompt="q", session_id="s-1", user_id="u",
                                    runtime_session_id=runtime_session_id))
    return fake, events


def test_a_given_runtime_session_id_is_used_verbatim():
    fake, _events = _invoke(GIVEN)
    assert fake.invoke_agent_runtime.call_args.kwargs["runtimeSessionId"] == GIVEN


def test_without_one_the_client_mints_its_usual_id():
    fake, _events = _invoke(None)
    sent = fake.invoke_agent_runtime.call_args.kwargs["runtimeSessionId"]
    assert re.fullmatch(r"session-[0-9a-f]{28}", sent)


def test_the_minted_id_meets_the_runtime_session_id_bounds():
    for _ in range(20):
        rsid = cc.new_runtime_session_id()
        assert re.fullmatch(r"session-[0-9a-f]{28}", rsid)
        assert 33 <= len(rsid) <= 256
    assert cc.new_runtime_session_id() != cc.new_runtime_session_id()


@pytest.mark.parametrize("bad", ["short", "session-" + "x" * 300, "session-" + "a" * 27 + " ",
                                 "session-../../" + "a" * 20])
def test_a_malformed_given_id_is_refused_before_any_invoke(bad):
    fake, events = _invoke(bad)
    fake.invoke_agent_runtime.assert_not_called()
    assert any(isinstance(e, ErrorEvent) for e in events)


def _stop_with(side_effect=None):
    fake = MagicMock()
    if side_effect is not None:
        fake.stop_runtime_session.side_effect = side_effect
    else:
        fake.stop_runtime_session.return_value = {"runtimeSessionId": GIVEN, "statusCode": 200}
    with patch("agents.chat_client.boto3.client", return_value=fake) as factory:
        outcome = cc.stop_runtime_session(GIVEN, runtime_arn=ARN, region="us-east-1")
    return fake, factory, outcome


def test_stop_targets_the_session_on_the_runtime():
    fake, factory, outcome = _stop_with()
    assert outcome == "stopped"
    fake.stop_runtime_session.assert_called_once_with(agentRuntimeArn=ARN,
                                                      runtimeSessionId=GIVEN)
    assert factory.call_args.args[0] == "bedrock-agentcore"
    assert factory.call_args.kwargs["region_name"] == "us-east-1"


def test_stop_of_a_session_that_already_ended_is_not_found():
    err = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
                      "StopRuntimeSession")
    assert _stop_with(err)[2] == "not_found"


def test_a_refused_stop_reports_its_error_code():
    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                      "StopRuntimeSession")
    assert _stop_with(err)[2] == "failed:AccessDeniedException"


def test_any_other_failure_is_reported_by_type_never_raised():
    assert _stop_with(RuntimeError("boom"))[2] == "failed:RuntimeError"


@pytest.mark.parametrize("bad", ["", "short", None])
def test_stop_never_calls_aws_with_a_malformed_id(bad):
    with patch("agents.chat_client.boto3.client") as factory:
        assert cc.stop_runtime_session(bad, runtime_arn=ARN, region="us-east-1") == "invalid"
    factory.assert_not_called()


def test_stop_without_a_runtime_arn_calls_nothing():
    with patch("agents.chat_client.boto3.client") as factory:
        assert cc.stop_runtime_session(GIVEN, runtime_arn="", region="us-east-1") == "no_runtime"
    factory.assert_not_called()


# --- the close is retried until it succeeds -------------------------------

class _FlakyCloseBody:
    """A body whose first ``close()`` raises: the close never happened."""

    def __init__(self, failures: int = 1):
        self.close_calls = 0
        self.closed = False
        self._failures = failures

    def close(self):
        self.close_calls += 1
        if self.close_calls <= self._failures:
            raise OSError("close failed")
        self.closed = True


def test_a_cancel_whose_close_failed_is_retried_by_the_next_cancel():
    handle = cc.StreamCancellation()
    body = _FlakyCloseBody()
    handle.arm(body)
    handle.cancel()
    assert not body.closed
    handle.cancel()
    assert body.closed and body.close_calls == 2
    handle.cancel()
    assert body.close_calls == 2, "a body that closed is never closed again"


def test_a_release_after_a_failed_cancel_retries_the_close():
    handle = cc.StreamCancellation()
    body = _FlakyCloseBody()
    handle.arm(body)
    handle.cancel()
    handle.release()
    assert body.closed and body.close_calls == 2
    assert handle.cancelled is True


def test_guard_a_close_that_works_still_happens_exactly_once():
    handle = cc.StreamCancellation()
    body = _FlakyCloseBody(failures=0)
    handle.arm(body)
    handle.cancel()
    handle.cancel()
    handle.release()
    assert body.close_calls == 1


# --- a cancelled handle refuses the invoke --------------------------------

def test_a_handle_cancelled_before_the_invoke_refuses_it():
    handle = cc.StreamCancellation()
    assert handle.prevent_invoke() is True
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"response": _EmptyStream()}
    with patch("agents.chat_client.boto3.client", return_value=fake):
        list(cc.AgentCoreChatClient(runtime_arn=ARN, region="us-east-1").stream(
            prompt="q", session_id="s-1", user_id="u", cancel=handle,
            runtime_session_id=GIVEN))
    fake.invoke_agent_runtime.assert_not_called()


def test_a_cancel_after_the_invoke_began_cannot_prevent_it():
    handle = cc.StreamCancellation()
    assert handle.begin_invoke() is True
    assert handle.prevent_invoke() is False
    assert not handle.cancelled


def test_guard_an_uncancelled_handle_still_invokes():
    handle = cc.StreamCancellation()
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"response": _EmptyStream()}
    with patch("agents.chat_client.boto3.client", return_value=fake):
        list(cc.AgentCoreChatClient(runtime_arn=ARN, region="us-east-1").stream(
            prompt="q", session_id="s-1", user_id="u", cancel=handle,
            runtime_session_id=GIVEN))
    fake.invoke_agent_runtime.assert_called_once()
