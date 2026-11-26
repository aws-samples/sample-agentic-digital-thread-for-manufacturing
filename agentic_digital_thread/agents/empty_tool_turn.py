# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The model ended a turn with stop reason ``tool_use`` but asked for no tool.

The failure (a live rehearsal account, 2026-10-22, orchestrator on Claude
Sonnet 5.5): the model's first reply carried only a thinking block and the
stop reason ``tool_use``, with no ``toolUse`` block. strands (pinned 1.42.0)
trusts the stop reason, so its event loop:

* appended that reply to the conversation (its blank content became the
  placeholder text ``[blank text]``);
* ran the tool step with no tools and appended a user message with EMPTY
  content;
* called the model again. The Bedrock request formatter drops a message whose
  content is empty, so the request ended on the assistant reply. A model
  without assistant-prefill support rejects that:
  "This model does not support assistant message prefill. The conversation
  must end with a user message." The turn failed with no answer.

:class:`EmptyToolTurnRetry` asks the model again instead. strands'
``AfterModelCallEvent.retry`` discards the reply before it is added to the
conversation, so the retried call is sent exactly the request the empty one
was sent; no tool has run, because tools run only after a reply is accepted.

What counts as empty: stop reason ``tool_use``, no ``toolUse`` block, and no
non-blank answer text (a thinking block alone, or nothing at all). A reply
that wrote answer text but called no tool is left alone: that text has already
been streamed to the user, and a retry would show it twice.

Bounded per invocation: at most :data:`MAX_EMPTY_TOOL_TURN_RETRIES` retries in
the whole invocation, however many replies are accepted in between. Once they
are spent, an empty reply is accepted and the loop is strands' own, unchanged:
the turn fails as it did without this hook on a model that rejects prefill,
and makes exactly the model calls it made without this hook on one that
accepts it. No retry either once the turn was cancelled (``Agent.cancel()``)
or when the caller's window (``model_stream_retry.retry_window``) has less
than :data:`~agents.model_stream_retry.STALL_RETRY_MIN_REMAINING_S` left, the
same admission rule the stream-stall retry uses.

Usage: strands adds a reply's token usage and latency to the invocation's
metrics only when the reply is accepted, so a discarded reply's usage is added
here, once, when it is discarded. The cost ledger reads those totals.

Known limit: strands streams every event of a reply before this hook runs, so
a discarded reply's reasoning text has already reached the stream (the chat's
Reasoning panel shows it, followed by the retried reply's). Holding reasoning
back until the reply is accepted would hold back every turn's reasoning until
the model finished it.

Each retry, and each refusal, logs one line with
:data:`EMPTY_TOOL_TURN_LOG_MARKER` and the model id, never message content.
Fail-soft: an unreadable event is left alone. Model-agnostic: it keys only on
the stop reason and the reply's block types.
"""
from __future__ import annotations

import logging

from strands.hooks import AfterModelCallEvent, HookProvider, HookRegistry

logger = logging.getLogger(__name__)

#: Stable text in the log line, so a live check can count retries.
EMPTY_TOOL_TURN_LOG_MARKER = "model ended a tool_use turn with no tool call"

#: Retries allowed in one invocation.
MAX_EMPTY_TOOL_TURN_RETRIES = 2

#: ``invocation_state`` key: empty replies retried so far in this invocation.
_STATE_KEY = "_adt_empty_tool_turn_retries"


def is_empty_tool_turn(stop_reason, message) -> bool:
    """True when *message* stops for a tool but holds no tool call and no
    answer text."""
    if stop_reason != "tool_use":
        return False
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if "toolUse" in block:
            return False
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return False
    return True


class EmptyToolTurnRetry(HookProvider):
    """Re-ask the model when it stops for a tool without calling one."""

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(AfterModelCallEvent, self._after_model_call)

    def _after_model_call(self, event: AfterModelCallEvent) -> None:
        try:
            self._decide(event)
        except Exception:  # noqa: BLE001  # nosec B110 - never break the agent loop
            logger.debug("empty tool turn check skipped", exc_info=True)

    @staticmethod
    def _decide(event: AfterModelCallEvent) -> None:
        response = event.stop_response
        state = event.invocation_state if isinstance(event.invocation_state, dict) else None
        if response is None or event.retry or state is None:
            return
        if not is_empty_tool_turn(response.stop_reason, response.message):
            return
        done = int(state.get(_STATE_KEY) or 0)
        model_id = _model_id(event)
        skip = _skip_reason(event, done)
        if skip:
            logger.warning("%s; not retrying: %s (model=%s)",
                           EMPTY_TOOL_TURN_LOG_MARKER, skip, model_id)
            return
        state[_STATE_KEY] = done + 1
        logger.warning("%s; asking again (model=%s retry=%d of %d)",
                       EMPTY_TOOL_TURN_LOG_MARKER, model_id, done + 1,
                       MAX_EMPTY_TOOL_TURN_RETRIES)
        event.retry = True
        _count_discarded_usage(event.agent, response.message)


def _skip_reason(event, done: int) -> "str | None":
    from .model_stream_retry import STALL_RETRY_MIN_REMAINING_S, retry_window_remaining

    if done >= MAX_EMPTY_TOOL_TURN_RETRIES:
        return f"{done} retries already in this invocation"
    signal = getattr(getattr(event, "agent", None), "_cancel_signal", None)
    is_set = getattr(signal, "is_set", None)
    if callable(is_set) and is_set():
        return "turn cancelled"
    remaining = retry_window_remaining()
    if remaining is not None and remaining < STALL_RETRY_MIN_REMAINING_S:
        return (f"caller window has {max(remaining, 0.0):.0f} s left, "
                f"a retry needs {STALL_RETRY_MIN_REMAINING_S:.0f} s")
    return None


def _count_discarded_usage(agent, message) -> None:
    """Add the discarded reply's usage and latency to the invocation's metrics
    (strands attaches them to the reply as ``metadata`` before this hook runs,
    and adds them itself only for an accepted reply)."""
    metrics = getattr(agent, "event_loop_metrics", None)
    metadata = message.get("metadata") if isinstance(message, dict) else None
    if metrics is None or not isinstance(metadata, dict):
        return
    usage, timing = metadata.get("usage"), metadata.get("metrics")
    try:
        if isinstance(usage, dict):
            metrics.update_usage(usage)
        if isinstance(timing, dict):
            metrics.update_metrics(timing)
    except Exception:  # noqa: BLE001  # nosec B110 - accounting must not break the retry
        logger.debug("discarded reply usage not recorded", exc_info=True)


def _model_id(event) -> str:
    model = getattr(getattr(event, "agent", None), "model", None)
    config = getattr(model, "config", None)
    if isinstance(config, dict):
        return str(config.get("model_id") or "?")
    return "?"
