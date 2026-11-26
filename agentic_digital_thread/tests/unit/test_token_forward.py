# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 1.2 (ADR-0033): token-forward plumbing app -> runtime payload.

INERT in Wave 1 — the field is plumbed into the invoke payload but not yet
consumed for gateway auth. No fallback: callers without a token omit the field.
"""
import json
from unittest.mock import MagicMock, patch

from agents.auth.forwarding import (
    USER_ACCESS_TOKEN_FIELD,
    user_access_token_from_session,
    with_user_access_token,
)


# ── helper: user_access_token_from_session ────────────────────────────────

def test_extract_token_from_session():
    assert user_access_token_from_session({"access_token": "abc"}) == "abc"


def test_extract_token_none_session():
    assert user_access_token_from_session(None) is None


def test_extract_token_empty_or_tokenless_session():
    # DEV_NO_AUTH stores {}; a session without a token yields None (no fallback).
    assert user_access_token_from_session({}) is None
    assert user_access_token_from_session({"username": "dev"}) is None


# ── helper: with_user_access_token ────────────────────────────────────────

def test_with_token_adds_field_without_mutating():
    base = {"prompt": "hi", "writeMemory": True}
    out = with_user_access_token(base, "tok")
    assert out[USER_ACCESS_TOKEN_FIELD] == "tok"
    assert USER_ACCESS_TOKEN_FIELD not in base  # original untouched


def test_with_token_omits_when_absent():
    base = {"prompt": "hi", "writeMemory": False}
    out = with_user_access_token(base, None)
    assert USER_ACCESS_TOKEN_FIELD not in out
    assert out == base
    assert out is not base  # always a copy


# ── chat_client payload plumbing (AgentCoreChatClient) ────────────────────

class _FakeStream:
    def iter_lines(self):
        return iter([])


def _capture_payload(user_access_token):
    """Run AgentCoreChatClient.stream with a mocked runtime and capture the payload."""
    from agents.chat_client import AgentCoreChatClient

    captured: dict = {}
    fake_client = MagicMock()

    def _invoke(**kwargs):
        captured["payload"] = json.loads(kwargs["payload"].read().decode())
        return {"response": _FakeStream()}

    fake_client.invoke_agent_runtime.side_effect = _invoke
    with patch("agents.chat_client.boto3.client", return_value=fake_client):
        client = AgentCoreChatClient(runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x", region="us-east-1")
        list(client.stream(prompt="hi", session_id="s", user_id="u",
                           write_memory=True, user_access_token=user_access_token))
    return captured["payload"]


def test_payload_includes_token_for_interactive_chat():
    payload = _capture_payload("user-tok")
    assert payload[USER_ACCESS_TOKEN_FIELD] == "user-tok"
    assert payload["writeMemory"] is True
    assert payload["prompt"] == "hi"


def test_payload_omits_token_for_service_caller():
    payload = _capture_payload(None)
    assert USER_ACCESS_TOKEN_FIELD not in payload
