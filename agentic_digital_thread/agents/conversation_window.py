# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The orchestrator's bounded conversation window, trimmed at turn boundaries.

The stock ``SlidingWindowConversationManager`` keeps ``window_size`` messages
and, when it has to trim, moves its cut forward to the first plain user
question after the overflow point (falling back to a tool call/result pair
when there is none). Two shapes went wrong in chat (walk 2026-10-19):

* A follow-up after a tool-heavy answer. The previous turn alone filled the
  window, so its question was the first message; the follow-up question
  pushed the window one message over and the only plain question left to cut
  to was the new one. Every earlier tool result left the model's context
  before it read the follow-up, although ``CitationOrdinalHook`` had labelled
  them ``[E<k>]`` and the runtime had told the client about them. The model
  answered from memory-summary text, copied the earlier answer's citation
  numbers, and the answer had no Sources row.
* A turn longer than the window. Its own question was cut away mid-turn and
  the history sent to the model began with an assistant tool call.

:class:`TurnKeepingSlidingWindow` keeps whole turns instead: when the stock
cut would fall inside the previous turn (or inside the current one), it cuts
at that turn's question as long as the kept messages stay within
``max_kept_messages`` (twice the window by default). Past that hard cap it
trims exactly like the stock manager, so the context stays bounded. A
reactive context-overflow reduction always uses the stock reduction.

Bookkeeping only: messages are compared by role and block type, never by
their text, and nothing here is specific to a system, question or dataset.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from strands.agent.conversation_manager import SlidingWindowConversationManager

from .history_turns import starts_turn

logger = logging.getLogger(__name__)

#: The name the stored conversation-manager state carries. Chat sessions are
#: persisted with it (strands checks ``__name__`` on restore), so this window
#: writes and accepts the stock name: sessions stored before it shipped still
#: restore, and so do its own sessions on an image without it.
_STATE_NAME = SlidingWindowConversationManager.__name__


class TurnKeepingSlidingWindow(SlidingWindowConversationManager):
    """A sliding window that cuts at the previous or current turn's question.

    ``window_size`` keeps its meaning: management starts once the history is
    longer than it. ``max_kept_messages`` is the hard cap on what a
    turn-boundary cut may keep (default ``2 * window_size``).
    """

    def __init__(self, window_size: int = 20, *, max_kept_messages: int | None = None,
                 **kwargs: Any) -> None:
        super().__init__(window_size=window_size, **kwargs)
        self.max_kept_messages = (max_kept_messages if max_kept_messages is not None
                                  else 2 * window_size)

    def get_state(self) -> dict[str, Any]:
        state = super().get_state()
        state["__name__"] = _STATE_NAME
        return state

    def restore_from_session(self, state: dict[str, Any]) -> list | None:
        if isinstance(state, dict) and state.get("__name__") == _STATE_NAME:
            state = {**state, "__name__": type(self).__name__}
        return super().restore_from_session(state)

    def reduce_context(self, agent: Any, e: Exception | None = None, **kwargs: Any) -> None:
        if e is not None:
            super().reduce_context(agent, e=e, **kwargs)
            return
        messages = agent.messages
        stock_cut, stock_kept = self._stock_cut(messages, **kwargs)
        cut = self._turn_boundary_cut(messages, stock_cut)
        if cut is None:
            self.removed_message_count += stock_cut
            messages[:] = stock_kept
            return
        if cut:
            self.removed_message_count += cut
            messages[:] = messages[cut:]
        logger.debug("CONVERSATION_WINDOW: kept %d message(s) from a turn start "
                     "(stock cut %d)", len(messages), stock_cut)

    def _stock_cut(self, messages: list, **kwargs: Any) -> tuple[int, list]:
        """What the stock reduction would remove, computed on a copy:
        ``(messages removed from the front, the kept list)``."""
        probe = SimpleNamespace(messages=list(messages))
        removed_before = self.removed_message_count
        try:
            super().reduce_context(probe, e=None, **kwargs)
        finally:
            self.removed_message_count = removed_before
        return len(messages) - len(probe.messages), probe.messages

    def _turn_boundary_cut(self, messages: list, stock_cut: int) -> int | None:
        """The turn start to cut at, or ``None`` for the stock cut.

        Candidates are the previous turn's question, then the current one's.
        A candidate the stock cut already keeps needs nothing; one whose
        kept messages would pass the cap is skipped. A history that opens
        with a tool call and its result, before any question, is a turn the
        stock window already trimmed when it was stored (its question is
        behind the restored offset): index 0 is that turn's start."""
        try:
            starts = [i for i, message in enumerate(messages) if starts_turn(message)]
            if messages and (not starts or starts[0] > 0) and _opens_with_tool_pair(messages):
                starts.insert(0, 0)
        except Exception:  # nosec B110 - an unreadable history takes the stock cut
            return None
        for start in starts[-2:]:
            if stock_cut <= start:
                return None
            if len(messages) - start <= self.max_kept_messages:
                return start
        return None


def _opens_with_tool_pair(messages: list) -> bool:
    """True when the history opens with an assistant tool call whose every
    result is in the next (user) message: the boundary the stock window
    falls back to, and a valid start for the model."""
    if len(messages) < 2:
        return False
    first, second = messages[0], messages[1]
    if first.get("role") != "assistant" or second.get("role") != "user":
        return False
    uses = {block["toolUse"].get("toolUseId") for block in first.get("content") or []
            if isinstance(block, dict) and isinstance(block.get("toolUse"), dict)}
    results = {block["toolResult"].get("toolUseId") for block in second.get("content") or []
               if isinstance(block, dict) and isinstance(block.get("toolResult"), dict)}
    return bool(uses) and uses <= results
