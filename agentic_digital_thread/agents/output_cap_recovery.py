# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ONE bounded recovery when the orchestrator's model stops at its output cap.

strands (pinned 1.42.0) raises ``MaxTokensReachedException`` when a model
reply ends with stop reason ``max_tokens``. Before it raises, its event loop
has already:

* replaced every ``toolUse`` block of the cut-off reply with a text block
  saying the tool use was incomplete
  (``event_loop._recover_message_on_max_tokens_reached``), so no cut-off tool
  call runs and no ``toolResult`` is owed;
* appended that reply to ``agent.messages`` (a session manager has persisted
  it through ``MessageAddedEvent``) and recorded its token usage.

So the agent holds a valid conversation that ends on an assistant reply.
Uncaught, the raise ended the turn as a generic "Query failed." even when a
shorter second attempt would have answered (seen live: three long system
lists, then the next reply ran out while still writing tool calls).

:func:`stream_with_output_cap_recovery` continues the SAME conversation once,
with a short generic user-role notice (:data:`RECOVERY_NOTICE`). The model
sees its own cut-off reply, so a cut while writing tool calls is re-asked as
narrower calls and a cut while writing the answer is continued where it
stopped. A second cut raises :class:`OutputLimitExceeded`; the runtime turns
that into the fixed :data:`OUTPUT_LIMIT_ERROR_CODE` frame, which the chat
client shows as the honest "answer was too long" message rather than the
generic one.

Tool calls the cut reply had started never ran: ``on_cut`` hands their exact
ids to the caller so they leave the turn's counts and learning inputs.

A cut inside thinking can leave an unsigned reasoning block, which Bedrock
rejects when it is sent back. :func:`without_unsigned_reasoning` is the ONE
rule for that, applied to the live cut reply before the retry here and to
every message the chat-memory session manager stores and rehydrates
(``orchestrator._persistable_session_message``), so the retry sees exactly
the history the next turn will rehydrate.

Scope: any provider whose strands model reports a cut as stop reason
``max_tokens`` (so strands raises ``MaxTokensReachedException``); it keys only
on that exception and the message shapes strands writes. Not every provider
does so in every case: strands' OpenAI Responses provider (GPT-5.x on Bedrock
Mantle) lets an accumulated tool call outrank a ``length`` stop, so a GPT-5.x
reply cut while writing a tool call arrives as ``tool_use`` and this recovery
never runs. Bounded by construction: one retry invocation, nothing else. One
log line per recovery, with counts and the outcome, never data values.
Stdlib-only at import (strands is imported when a stream runs).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Fixed error code the runtime streams when the retry is cut off too. The
#: chat client maps it (word-boundary, case-sensitive) to the public
#: ``output_limit`` category; nothing else ever emits it.
OUTPUT_LIMIT_ERROR_CODE = "ORCHESTRATOR_OUTPUT_LIMIT"

#: Leading tag of the notice turn. ``memory_labels.strip_turn_preambles``
#: drops a turn that starts with it, so restored chat history never shows the
#: notice as something the user typed.
RECOVERY_NOTICE_PREFIX = "[OUTPUT LIMIT NOTICE]"

#: The user-role notice for the one retry. Generic: no answer content, no
#: system or domain names. It is persisted with the conversation like the
#: route seed already is, so it says plainly that it is not from the user.
RECOVERY_NOTICE = (
    f"{RECOVERY_NOTICE_PREFIX} Automatic notice, not from the user: your "
    "previous reply was cut off at the output-token limit before it finished, "
    "so any tool call it was still writing was not sent. Continue the same "
    "request. If you were writing tool calls, keep each one short: ask each "
    "system a focused sub-question it can answer by filtering, counting or "
    "summarizing, and split a big request into several small calls instead of "
    "asking for or passing long lists. If you were writing the answer, "
    "continue from exactly where it stopped without repeating what is already "
    "written. Keep the rest concise and do not mention this notice."
)

#: Stands in for a cut-off reply that held nothing but unsigned thinking: an
#: assistant turn must carry content, and an unsigned thinking block cannot be
#: sent back. Used for the live retry and for the stored copy alike.
CUT_REPLY_PLACEHOLDER = "[reply cut off at the output limit]"

_LOG_TAG = "ORCHESTRATOR_OUTPUT_LIMIT_RECOVERY"


class OutputLimitExceeded(Exception):
    """The orchestrator's reply was cut off at its output cap and the one
    recovery could not finish it (cut again, or no recoverable state)."""


class _AttemptTally:
    """Tool calls started in the stream that have not had a result yet.

    Only the reply in flight can leave ids here (each result clears its id),
    so after a cut it holds exactly the calls the cut reply started.
    """

    __slots__ = ("pending",)

    def __init__(self):
        self.pending: set[str] = set()

    def see(self, event) -> None:
        if not isinstance(event, dict):
            return
        current = event.get("current_tool_use")
        if isinstance(current, dict) and current.get("toolUseId"):
            self.pending.add(str(current["toolUseId"]))
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "user":
            for block in message.get("content") or []:
                result = block.get("toolResult") if isinstance(block, dict) else None
                if isinstance(result, dict) and result.get("toolUseId"):
                    self.pending.discard(str(result["toolUseId"]))

    def take_cut_ids(self) -> frozenset:
        cut, self.pending = frozenset(self.pending), set()
        return cut


def is_unsigned_reasoning(block) -> bool:
    """A reasoning block a provider will not accept back: thinking text with
    no non-empty signature. Encrypted (redacted) thinking needs none."""
    reasoning = block.get("reasoningContent") if isinstance(block, dict) else None
    if not isinstance(reasoning, dict) or reasoning.get("redactedContent"):
        return False
    text = reasoning.get("reasoningText")
    return not (isinstance(text, dict) and str(text.get("signature") or "").strip())


def _blank_text(block) -> bool:
    return (isinstance(block, dict) and set(block) == {"text"}
            and not str(block.get("text") or "").strip())


def without_unsigned_reasoning(content, role) -> "list | None":
    """*content* without unsigned reasoning, or ``None`` when it has none.

    ``None`` means "leave the message exactly as it is", so a normal turn
    (signed or no thinking) is never rewritten. Otherwise the unsigned blocks
    and any blank text block go, everything else stays as the same objects
    (signed reasoning byte for byte), and an assistant message left empty
    gets :data:`CUT_REPLY_PLACEHOLDER`.
    """
    if not isinstance(content, list) or not any(map(is_unsigned_reasoning, content)):
        return None
    kept = [b for b in content if not is_unsigned_reasoning(b) and not _blank_text(b)]
    if not kept and role == "assistant":
        kept = [{"text": CUT_REPLY_PLACEHOLDER}]
    return kept


def _prepare_cut_reply(agent) -> bool:
    """Make the cut-off reply safe to send back, in the live conversation.

    strands has already replaced its tool uses with text (pinned by the
    premise tests). :func:`without_unsigned_reasoning` drops a reasoning
    block the cut left unsigned; signed thinking stays. Returns False when the
    conversation does not end on an assistant reply, which is not the state
    strands documents, so no retry is attempted.
    """
    messages = getattr(agent, "messages", None)
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return False
    cleaned = without_unsigned_reasoning(last.get("content"), "assistant")
    if cleaned is not None:
        last["content"] = cleaned
    if not last.get("content"):
        last["content"] = [{"text": CUT_REPLY_PLACEHOLDER}]
    return True


def _model_label(agent) -> str:
    config = getattr(getattr(agent, "model", None), "config", None)
    model_id = config.get("model_id") if isinstance(config, dict) else None
    label = str(model_id or type(getattr(agent, "model", None)).__name__)
    return re.sub(r"[^A-Za-z0-9._:/-]", "_", label)[:80]


def _log_recovery(agent, session_id: str, cut_calls: int, outcome: str,
                  exc: "BaseException | None" = None) -> None:
    """The ONE line per recovery: counts, model and outcome, no data values.

    A retry that fails another way carries its traceback IN the message
    (credentials redacted, digits masked): the orchestrator runtime's OTel
    handler drops ``exc_info``.
    """
    trace = ""
    if exc is not None:
        import traceback

        from .failure_fingerprint import redact_secrets
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        trace = " type=%s trace=%s" % (
            type(exc).__name__, re.sub(r"\d", "#", redact_secrets(text, 1200))[:1200])
    logger.warning("%s outcome=%s cut_tool_calls=%d model=%s session=%s%s",
                   _LOG_TAG, outcome, cut_calls, _model_label(agent),
                   re.sub(r"[^A-Za-z0-9._:-]", "_", str(session_id or "default"))[:96],
                   trace)


async def stream_with_output_cap_recovery(agent, prompt, *, session_id: str = "",
                                          on_cut=None, **kwargs):
    """Yield ``agent.stream_async(prompt, **kwargs)``; on an output-cap stop,
    continue the same conversation ONCE with :data:`RECOVERY_NOTICE`.

    Events pass through unchanged and in order. After each cut, ``on_cut``
    (when given) receives the frozenset of tool-use ids that cut reply had
    started; none of them ran. A second cut (or a state that cannot be
    continued) raises :class:`OutputLimitExceeded`; any other exception
    propagates unchanged, from either attempt.
    """
    from strands.types.exceptions import MaxTokensReachedException

    tally = _AttemptTally()
    try:
        async for event in agent.stream_async(prompt, **kwargs):
            tally.see(event)
            yield event
        return
    except MaxTokensReachedException:
        pass  # handled below, outside the except block
    first_cut = tally.take_cut_ids()
    if on_cut is not None:
        on_cut(first_cut)
    if not _prepare_cut_reply(agent):
        _log_recovery(agent, session_id, len(first_cut), "not_retried")
        raise OutputLimitExceeded()
    try:
        async for event in agent.stream_async(RECOVERY_NOTICE, **kwargs):
            tally.see(event)
            yield event
    except MaxTokensReachedException:
        if on_cut is not None:
            on_cut(tally.take_cut_ids())
        _log_recovery(agent, session_id, len(first_cut), "cut_again")
        raise OutputLimitExceeded() from None
    except Exception as exc:
        _log_recovery(agent, session_id, len(first_cut), "error", exc)
        raise
    _log_recovery(agent, session_id, len(first_cut), "answered")
