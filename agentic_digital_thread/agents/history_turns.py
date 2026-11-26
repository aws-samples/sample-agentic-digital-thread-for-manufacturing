# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which stored chat turns a new turn reads back.

AgentCore Memory stores every message of a chat turn as it is added: the
question, each tool call, each tool result, the reply. A turn the app gave up
on (a dropped connection, a Stop, a cancelled runtime) stays stored up to its
last call. Read back as it is, that history ends in a tool call with no
result; strands then adds a "Tool was interrupted." result and the model can
answer the next question from the failed attempt's tool results without
calling a system again, so its answer carries no Sources of its own.

:func:`without_unfinished_turns` is the ONE read rule. The orchestrator's
session-manager read wrapper applies it ONLY on an "Ask again" turn (the
runtime popped the retry flag); every other turn reads the stored history
exactly as before, because a turn the user stopped after seeing partial output
is context the next question may build on (strands closes its open call with
its own "interrupted" result). Over the whole listed history:

* a TURN starts at a user message with text (not a tool-result message, and
  not the runtime's own output-limit notice, which continues the turn it
  belongs to); messages before the first turn start are a leading fragment
  (the listing can start mid-turn) and are left alone;
* a turn FINISHED when its last message is an assistant reply with text and
  no tool call, or the successful result of the structured-answer tool (the
  enforce path's final answer is a tool call);
* every unfinished turn is dropped, wherever it sits. Whole turns go, so a
  tool call and its result are never separated.

"Ask again" on the last turn sends a retry flag: the turn the user is
re-asking may have finished on the server without ever reaching the browser.
With :data:`RETRY_QUESTION_VAR` set to the re-asked question, the trailing
turns whose opening question EXACTLY equals it (route seed and server
preambles peeled, whitespace collapsed; as asked or as its stored copy
redacts it) are dropped as well. Never containment, and never a turn that
asked something else: an attempt that failed before the runtime ran left
nothing stored, and the answer before it is one the user saw. The orchestrator
also skips the same-session summary read on such a turn, since AgentCore
summarises the stored events, the hidden attempt included.

Stdlib-only at import; nothing here reads or logs message text.
"""
from __future__ import annotations

import contextvars
import logging

logger = logging.getLogger(__name__)

#: RESERVED runtime payload key the app adds for "Ask again" on the last turn.
#: Popped unconditionally by the runtime entrypoint, so it never reaches the
#: prompt or the model (the ``_adt_identity`` posture, ADR-0049).
RETRY_PAYLOAD_KEY = "retryUndeliveredTurn"

#: The question an "Ask again" turn re-asks, or ``None`` for every other turn.
#: Set on EVERY invocation by the runtime entrypoint and read by the history
#: read wrapper during the agent build of the same invocation.
RETRY_QUESTION_VAR: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "adt_retry_question", default=None)

#: Tools whose successful result IS the turn's final answer. strands names a
#: structured-output tool after its model class; a test pins this against
#: ``response_integrity.FinalAnswerDraft``.
STRUCTURED_ANSWER_TOOLS = frozenset({"FinalAnswerDraft"})


def retried_question() -> "str | None":
    """The question the current invocation re-asks, or ``None``."""
    return RETRY_QUESTION_VAR.get()


def _message(item) -> dict:
    """The strands message of a SessionMessage or a plain message dict."""
    message = getattr(item, "message", item)
    return message if isinstance(message, dict) else {}


def _blocks(item) -> list:
    content = _message(item).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _texts(item) -> list[str]:
    return [b["text"] for b in _blocks(item)
            if isinstance(b.get("text"), str) and b["text"].strip()]


def _starts_turn(item) -> bool:
    """A user question: text, no tool result, not the output-limit notice."""
    from .output_cap_recovery import RECOVERY_NOTICE_PREFIX
    if _message(item).get("role") != "user":
        return False
    if any("toolResult" in b for b in _blocks(item)):
        return False
    texts = _texts(item)
    return bool(texts) and not texts[0].lstrip().startswith(RECOVERY_NOTICE_PREFIX)


def starts_turn(item) -> bool:
    """True when *item* (a SessionMessage or a message dict) opens a turn:
    the same rule this module's read rule splits turns by, shared with the
    orchestrator's conversation window (``conversation_window``)."""
    return _starts_turn(item)


def _tool_names(turn) -> dict:
    names = {}
    for item in turn:
        for block in _blocks(item):
            use = block.get("toolUse")
            if isinstance(use, dict):
                names[use.get("toolUseId")] = use.get("name")
    return names


def _finished(turn) -> bool:
    """True when the turn's last message is its answer."""
    last = turn[-1]
    blocks = _blocks(last)
    role = _message(last).get("role")
    if role == "assistant":
        return bool(_texts(last)) and not any("toolUse" in b for b in blocks)
    results = [b["toolResult"] for b in blocks if isinstance(b.get("toolResult"), dict)]
    if role != "user" or not results or len(results) != len(blocks):
        return False
    names = _tool_names(turn)
    return all(r.get("status") == "success"
               and names.get(r.get("toolUseId")) in STRUCTURED_ANSWER_TOOLS
               for r in results)


def _split_turns(items) -> tuple[list, list[list]]:
    """(leading fragment, turns) — each turn opens with a user question."""
    head: list = []
    turns: list[list] = []
    for item in items:
        if _starts_turn(item):
            turns.append([item])
        elif turns:
            turns[-1].append(item)
        else:
            head.append(item)
    return head, turns


def _canonical_question(text: str) -> str:
    """The question a stored user turn carries: the route seed and the
    server's temporal/plant preambles peeled (the same rule the history
    display uses), whitespace collapsed."""
    from .memory_labels import strip_turn_preambles
    return " ".join(strip_turn_preambles(str(text or "")).split())


def _question_forms(question: "str | None") -> set[str]:
    """The re-asked question as asked and as its stored copy redacts it, both
    canonical; empty when there is no question."""
    text = _canonical_question(question or "")
    if not text:
        return set()
    forms = {text}
    try:
        from .pii import redact_pii
        forms.add(_canonical_question(redact_pii(question or "")))
    except Exception:  # noqa: BLE001 - the plain form still matches
        pass
    return {form for form in forms if form}


def _asks(turn, forms: set[str]) -> bool:
    """True when the turn's opening question EXACTLY equals one of *forms*
    (never containment: a longer question that starts with the retried one is
    a different question the user saw answered)."""
    return _canonical_question("\n".join(_texts(turn[0]))) in forms


def _drop_retried(turns: list[list], question: "str | None") -> int:
    """Drop the trailing turns that asked *question*; return how many."""
    forms = _question_forms(question)
    dropped = 0
    while forms and turns and _asks(turns[-1], forms):
        turns.pop()
        dropped += 1
    return dropped


def without_unfinished_turns(items, retry_question: "str | None" = None) -> list:
    """*items* (SessionMessages or message dicts, oldest first) without the
    turns that never reached an answer, and — when *retry_question* is set —
    without the trailing turns that asked it. Kept items are the SAME objects
    in the same order; nothing is copied or rewritten."""
    items = list(items or [])
    head, turns = _split_turns(items)
    retried = _drop_retried(turns, retry_question)
    kept = [turn for turn in turns if _finished(turn)]
    unfinished = len(turns) - len(kept)
    if unfinished or retried:
        logger.info("CHAT_HISTORY_TURNS dropped_unfinished=%d dropped_retried=%d kept=%d",
                    unfinished, retried, len(kept))
    return head + [item for turn in kept for item in turn]
