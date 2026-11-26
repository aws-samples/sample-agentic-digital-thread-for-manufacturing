# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for orchestrator memory gating + the resilient-but-LOUD write fallback.

Verifies build_orchestrator only wires the AgentCore Memory session manager when
memory is CONFIGURED (has_memory) AND the caller opts in (write_memory) — and that
a CONFIGURED-but-failing session manager degrades LOUDLY (ERROR + _memory_degraded
flag) while keeping chat resilient (Agent still built, conversation continues).

No AWS / LLM: the Strands Agent and the session-manager construction are mocked.

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/test_orchestrator_memory.py
  * standalone:  python agentic_digital_thread/tests/test_orchestrator_memory.py
"""
from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Make `agents...` importable (agentic_digital_thread/ is one level up from tests/).
_ADT_ROOT = Path(__file__).resolve().parents[1]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.orchestrator as orch  # noqa: E402

_SM_PATH = (
    "bedrock_agentcore.memory.integrations.strands."
    "session_manager.AgentCoreMemorySessionManager"
)


def _fake_config(has_memory: bool, *, memory_id=None):
    """Minimal stand-in for AppConfig used by build_orchestrator / _get_orchestrator_tools."""
    return SimpleNamespace(
        has_memory=has_memory,
        has_gateway=False,           # local-mode tool list (no SigV4 gateway client)
        agentcore_gateway_url=None,
        memory_id=memory_id or ("mem-123" if has_memory else None),
        aws_region="us-east-1",
    )


def _patch_agent_capture():
    """Patch orchestrator.Agent so construction records kwargs and returns a stub.

    Returns (patcher, captured_kwargs_list). The stub stands in for a usable Agent.

    Also keeps MODEL construction offline. ``build_orchestrator`` builds a real
    ``BedrockModel``, and boto3 client creation resolves credentials — on a host
    with none (CI) that reaches the IMDS endpoint at 169.254.169.254, so the
    socket-disabled lane failed while passing on a developer machine. This
    module's contract is "No AWS / LLM": the model is an INPUT to the Agent, not
    the thing under test. Patched here rather than via an autouse fixture so the
    standalone ``python tests/test_orchestrator_memory.py`` path is covered too.
    """
    captured: list[dict] = []

    def _fake_agent(**kwargs):
        captured.append(kwargs)
        return MagicMock(name="Agent")

    @contextlib.contextmanager
    def _patches():
        with patch.object(orch, "Agent", side_effect=_fake_agent), \
             patch.object(orch, "build_model", return_value=MagicMock(name="Model")):
            yield

    return _patches(), captured


def setup_function(_fn):
    # Each test starts from a clean degradation signal and no stray MCP clients.
    orch.reset_memory_degraded()
    orch._mcp_clients.clear()


# ---------------------------------------------------------------------------
# (a) has_memory False -> NO session manager, _make_session_manager NOT called
# ---------------------------------------------------------------------------
def test_no_memory_skips_session_manager():
    agent_patch, captured = _patch_agent_capture()
    with patch.object(orch, "get_config", return_value=_fake_config(has_memory=False)), \
         patch.object(orch, "_make_session_manager") as msm, \
         agent_patch:
        agent = orch.build_orchestrator("sess-1", "user-1", write_memory=True)

    assert agent is not None                      # usable Agent returned
    msm.assert_not_called()                       # never attempted
    assert "session_manager" not in captured[0]   # no session manager wired
    assert captured[0]["conversation_manager"].per_turn is True  # (d)


# ---------------------------------------------------------------------------
# (b) has_memory True + write_memory False -> NO session manager
# ---------------------------------------------------------------------------
def test_configured_but_write_disabled_skips_session_manager():
    agent_patch, captured = _patch_agent_capture()
    with patch.object(orch, "get_config", return_value=_fake_config(has_memory=True)), \
         patch.object(orch, "_make_session_manager") as msm, \
         agent_patch:
        agent = orch.build_orchestrator("sess-1", "user-1", write_memory=False)

    assert agent is not None
    msm.assert_not_called()
    assert "session_manager" not in captured[0]
    assert captured[0]["conversation_manager"].per_turn is True  # (d)


# ---------------------------------------------------------------------------
# (c) has_memory True + write_memory True -> attempts session manager and wires it
# ---------------------------------------------------------------------------
def test_configured_and_write_enabled_attaches_session_manager():
    agent_patch, captured = _patch_agent_capture()
    sentinel_sm = object()
    cfg = _fake_config(has_memory=True)
    with patch.object(orch, "get_config", return_value=cfg), \
         patch.object(orch, "_make_session_manager", return_value=sentinel_sm) as msm, \
         agent_patch:
        agent = orch.build_orchestrator("sess-1", "user-1", write_memory=True)

    assert agent is not None
    msm.assert_called_once_with("sess-1", "user-1", cfg)
    assert captured[0]["session_manager"] is sentinel_sm
    assert captured[0]["conversation_manager"].per_turn is True  # (d)


# ---------------------------------------------------------------------------
# (e1) configured-but-failing session manager -> resilient Agent, NO crash
# ---------------------------------------------------------------------------
def test_resilient_when_session_manager_returns_none():
    agent_patch, captured = _patch_agent_capture()
    with patch.object(orch, "get_config", return_value=_fake_config(has_memory=True)), \
         patch.object(orch, "_make_session_manager", return_value=None), \
         agent_patch:
        agent = orch.build_orchestrator("sess-1", "user-1", write_memory=True)

    assert agent is not None                      # chat continues (resilient)
    assert "session_manager" not in captured[0]   # not wired when None
    assert captured[0]["conversation_manager"].per_turn is True  # (d)


# ---------------------------------------------------------------------------
# (e2) the LOUD + observable degradation signal fires on a real wiring failure
# ---------------------------------------------------------------------------
def test_make_session_manager_degrades_loudly_on_failure(caplog):
    cfg = _fake_config(has_memory=True, memory_id="mem-xyz")
    assert orch.memory_degraded() is False

    boom = MagicMock(side_effect=RuntimeError("simulated wiring failure"))
    with patch(_SM_PATH, boom), caplog.at_level(logging.ERROR, logger=orch.logger.name):
        result = orch._make_session_manager("sess-1", "user-1", cfg)

    assert result is None                         # resilient: returns None
    assert orch.memory_degraded() is True         # observable flag flipped
    assert orch._memory_degraded_count == 1       # counter bumped
    # LOUD: logged at ERROR, names the configured memory_id, distinguishes from
    # the unconfigured case.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log for the configured-memory failure"
    msg = errors[0].getMessage()
    assert "CONFIGURED" in msg and "mem-xyz" in msg
    assert errors[0].exc_info is not None         # traceback captured (exc_info=True)


# ---------------------------------------------------------------------------
# end-to-end resilience: a real failure inside build_orchestrator still yields an Agent
# ---------------------------------------------------------------------------
def test_build_orchestrator_resilient_through_real_failure():
    agent_patch, captured = _patch_agent_capture()
    boom = MagicMock(side_effect=RuntimeError("simulated wiring failure"))
    with patch.object(orch, "get_config", return_value=_fake_config(has_memory=True)), \
         patch(_SM_PATH, boom), \
         agent_patch:
        agent = orch.build_orchestrator("sess-1", "user-1", write_memory=True)

    assert agent is not None                      # resilient end-to-end
    assert "session_manager" not in captured[0]   # failed SM not wired
    assert orch.memory_degraded() is True         # but the failure is observable


# ---------------------------------------------------------------------------
# LTM injection repair: keep the conversation ending on a user message so models
# without assistant-prefill support (e.g. claude-sonnet-4-6) don't reject the turn.
# ---------------------------------------------------------------------------
def test_inject_ltm_merges_trailing_assistant_into_user_turn():
    # Simulate what the SDK does: append a trailing assistant <user_context> message.
    messages = [
        {"role": "user", "content": [{"text": "How is the Dallas line?"}]},
        {"role": "assistant", "content": [{"text": "<user_context>prefers Celsius</user_context>"}]},
    ]
    repaired = orch._inject_ltm_into_user_turn(messages)

    assert repaired is True
    assert len(messages) == 1                      # trailing assistant removed
    assert messages[-1]["role"] == "user"          # conversation ends on user
    texts = [c["text"] for c in messages[-1]["content"]]
    assert "<user_context>prefers Celsius</user_context>" in texts  # context preserved
    assert "How is the Dallas line?" in texts                       # original query kept


def test_inject_ltm_noop_when_no_trailing_context():
    messages = [{"role": "user", "content": [{"text": "hi"}]}]
    # Only the user turn present (SDK retrieved nothing) -> nothing to repair.
    assert orch._inject_ltm_into_user_turn(messages) is False
    assert messages == [{"role": "user", "content": [{"text": "hi"}]}]


def test_inject_ltm_does_not_clobber_a_genuine_assistant_turn():
    # A real assistant turn (no <user_context> signature) must be left untouched.
    messages = [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "Hi there!"}]},
    ]
    assert orch._inject_ltm_into_user_turn(messages) is False
    assert len(messages) == 2 and messages[-1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# PII redaction on memory writes (H4): redact structured identifiers from the
# persisted copy without mutating the live conversation.
# ---------------------------------------------------------------------------
def test_redact_pii_structured_identifiers():
    out = orch._redact_pii(
        "I am Jane Doe, SSN 123-45-6789, email jane.doe@example.com, phone 202-555-0100."
    )
    assert "123-45-6789" not in out and "[REDACTED_SSN]" in out
    assert "jane.doe@example.com" not in out and "[REDACTED_EMAIL]" in out
    assert "202-555-0100" not in out and "[REDACTED_PHONE]" in out
    assert "Jane Doe" in out                          # names are NOT redacted (documented)


def test_redact_pii_leaves_clean_text_untouched():
    clean = "Show me open work orders for badge 7788 on the Aurora line."
    assert orch._redact_pii(clean) == clean        # short IDs / words not over-matched


def test_redact_session_message_redacts_copy_only():
    # Minimal stand-in for a Strands SessionMessage (only .message is touched).
    class _SM:
        def __init__(self, message):
            self.message = message

    original = _SM({"role": "user", "content": [{"text": "SSN 123-45-6789 here"}]})
    redacted = orch._redact_session_message(original)

    assert redacted is not original                                    # a copy was made
    assert "[REDACTED_SSN]" in redacted.message["content"][0]["text"]  # copy redacted
    assert original.message["content"][0]["text"] == "SSN 123-45-6789 here"  # live untouched


def test_redact_session_message_noop_without_text():
    class _SM:
        def __init__(self, message):
            self.message = message

    sm = _SM({"role": "user", "content": [{"toolResult": {"x": 1}}]})
    # No text blocks -> returns the same object (no needless copy).
    assert orch._redact_session_message(sm) is sm


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            setup_function(fn)
            # caplog-dependent tests only run under pytest.
            if "caplog" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                continue
            fn()
            print(f"PASS {name}")
    print("RESULT: PASS — orchestrator memory gating + resilient-but-loud fallback")
