# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agents.config import get_config
from agents.feedback_exploration import COMMENT_MAX_CHARS
from agents.storage import FeedbackStorage
from agents.untrusted_text import sanitize_untrusted, validate_untrusted

logger = logging.getLogger(__name__)

# Bounded cited-context caps (the turn's citations the user saw — server-side
# re-capped regardless of what the client sends).
_CITED_SYSTEMS_MAX = 12
_CITED_SYSTEM_CHARS = 120
_CITED_RULES_MAX = 20
_RULE_ID_CHARS = 64
_RULE_LABEL_CHARS = 160

# H1: cited systems must be id-shaped (word chars + hyphens — the
# systems_from_tool_names alphabet). Display labels are dropped, not stored.
_SYSTEM_ID_SHAPE = re.compile(r"^[\w-]+$")


@dataclass
class FeedbackRecord:
    user_id: str
    timestamp: str
    session_id: str
    message_id: str
    user_message: str
    assistant_response: str
    tools_used: list[str]
    sentiment: Literal["positive", "negative"]
    user_comment: Optional[str] = None
    cited_systems: list[str] = field(default_factory=list)
    cited_rules: list[dict] = field(default_factory=list)
    provenance: str = ""

    def to_dict(self) -> dict:
        d = {
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "user_message": self.user_message,
            "assistant_response": self.assistant_response,
            "tools_used": self.tools_used,
            "sentiment": self.sentiment,
        }
        if self.user_comment is not None:
            d["user_comment"] = self.user_comment
        if self.cited_systems:
            d["cited_systems"] = self.cited_systems
        if self.cited_rules:
            d["cited_rules"] = self.cited_rules
        if self.provenance:
            d["provenance"] = self.provenance
        return d


class FeedbackService:
    def __init__(self):
        cfg = get_config()
        self._storage = FeedbackStorage(cfg.feedback_table_name, cfg.aws_region)

    def store(self, record: FeedbackRecord) -> bool:
        return self._storage.store(record.to_dict())

    def get_by_time_range(self, start: str, end: str, sentiment: str | None = None) -> list[dict]:
        return self._storage.get_by_time_range(start, end, sentiment)

    def get_stats(self, start: str, end: str) -> dict:
        items = self._storage.get_by_time_range(start, end)
        total = len(items)
        positive = sum(1 for i in items if i.get("sentiment") == "positive")
        negative = total - positive
        return {
            "total": total,
            "positive": positive,
            "negative": negative,
            "positive_pct": round(positive / total * 100, 1) if total else 0.0,
        }


class _FeedbackBody(BaseModel):
    message_id: str
    session_id: str
    user_message: str
    assistant_response: str
    tools_used: list[str] = field(default_factory=list)
    sentiment: Literal["positive", "negative"]
    user_comment: Optional[str] = None
    # The citations/rules the ORIGINAL answer showed the user (optional,
    # additive) — re-capped server-side in _bounded_citations.
    cited_systems: list[str] = Field(default_factory=list)
    cited_rules: list[dict] = Field(default_factory=list)


def _validate_comment(raw: "str | None") -> "str | None":
    """Sanitize + validate the UNTRUSTED feedback comment (ADR-0054: the
    shared untrusted_text treatment — bounded, secret-shape-rejected,
    fence-token-rejected). Returns the sanitized text (None when absent/
    empty); raises 400 with the pinned reason. The detail NEVER echoes the
    comment text itself."""
    if raw is None:
        return None
    text = sanitize_untrusted(raw, max_len=COMMENT_MAX_CHARS)
    if isinstance(text, str) and not text:
        return None
    invalid = validate_untrusted(
        text, max_len=COMMENT_MAX_CHARS,
        field="'user_comment'", label="feedback comment")
    if invalid:
        reason, detail = invalid
        raise HTTPException(status_code=400, detail=f"{reason}: {detail}")
    return text


def _reject_invalid(text: str, field: str, label: str, max_len: int) -> None:
    """H2: the SAME untrusted-text discipline the comment gets, applied to a
    client-controlled cited-context string — secret-shape + fence-token
    rejection (400, pinned reason; the detail NEVER echoes the value)."""
    invalid = validate_untrusted(text, max_len=max_len, field=field, label=label)
    if invalid:
        reason, detail = invalid
        raise HTTPException(status_code=400, detail=f"{reason}: {detail}")


def _bounded_citations(body: _FeedbackBody) -> "tuple[list[str], list[dict]]":
    """Server-side treatment of the client-supplied cited context (never trust
    the client's own capping):

    * caps — ≤12 systems × 120 chars, ≤20 rules with id ≤64 / label ≤160;
    * untrusted-text validation (H2) — every capped string gets the shared
      secret-shape + fence-token rejection (400; detail never echoes);
    * id-shape normalization (H1) — cited systems are lowercased and entries
      that are not id-shaped (word chars + hyphens) are DROPPED, so a hostile
      or stale client sending display labels cannot recreate the label-vs-id
      unit mismatch in the exploration diff.
    """
    systems: list[str] = []
    for s in (body.cited_systems or [])[:_CITED_SYSTEMS_MAX]:
        if not isinstance(s, str) or not s.strip():
            continue
        capped = sanitize_untrusted(s)[:_CITED_SYSTEM_CHARS]
        _reject_invalid(capped, "'cited_systems'", "cited system",
                        _CITED_SYSTEM_CHARS)
        token = capped.strip().lower()
        if _SYSTEM_ID_SHAPE.match(token) and token not in systems:
            systems.append(token)
    rules: list[dict] = []
    for r in (body.cited_rules or [])[:_CITED_RULES_MAX]:
        if not isinstance(r, dict) or not (r.get("id") or r.get("label")):
            continue
        rule_id = sanitize_untrusted(str(r.get("id") or ""))[:_RULE_ID_CHARS]
        rule_label = sanitize_untrusted(
            str(r.get("label") or ""))[:_RULE_LABEL_CHARS]
        _reject_invalid(rule_id, "'cited_rules'", "cited rule id",
                        _RULE_ID_CHARS)
        _reject_invalid(rule_label, "'cited_rules'", "cited rule label",
                        _RULE_LABEL_CHARS)
        if rule_id or rule_label:
            rules.append({"id": rule_id, "label": rule_label})
    return systems, rules


async def _maybe_explore(user_id: str, body: _FeedbackBody, comment: str,
                         systems: list[str], rules: list[dict]) -> "str | None":
    """Kick the bounded background exploration; returns the exploration id
    (``None`` when a gate refused or the trigger failed). The
    negative-sentiment + non-empty-comment gate lives in the CALLER
    (``post_feedback``) — this helper only bridges to the exploration entry
    point. Fail-open: an exploration failure NEVER breaks the feedback POST."""
    try:
        from agents import feedback_exploration
        return await asyncio.to_thread(
            feedback_exploration.maybe_start_exploration,
            user_id, body.session_id, body.message_id,
            body.user_message, comment, systems, rules)
    except Exception as exc:  # noqa: BLE001 - feedback capture must survive
        logger.warning("feedback_exploration_trigger_failed: category=%s",
                       type(exc).__name__)
        return None


router = APIRouter(prefix="/api")
_service = FeedbackService()


@router.post("/feedback")
async def post_feedback(body: _FeedbackBody, request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    comment = _validate_comment(body.user_comment)
    systems, rules = _bounded_citations(body)

    from datetime import datetime, timezone
    user_id = user.user_id if hasattr(user, 'user_id') else str(user)
    record = FeedbackRecord(
        user_id=user_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        session_id=body.session_id,
        message_id=body.message_id,
        user_message=body.user_message,
        assistant_response=body.assistant_response,
        tools_used=body.tools_used,
        sentiment=body.sentiment,
        user_comment=comment,
        cited_systems=systems,
        cited_rules=rules,
        provenance="chat-feedback",
    )
    if not _service.store(record):
        # The return value is checked so a genuine DynamoDB failure (throttle,
        # AccessDenied) is as visible as a missing table. Stable log key; the
        # record itself is never logged.
        logger.warning("feedback_store_failed: sentiment=%s message_id_present=%s",
                       body.sentiment, bool(body.message_id))
    exploration_id = None
    if body.sentiment == "negative" and comment:
        exploration_id = await _maybe_explore(
            user_id, body, comment, systems, rules)
    return {"status": "success", "message_id": body.message_id,
            "sentiment": body.sentiment,
            "exploration_started": exploration_id is not None,
            "exploration_id": exploration_id}
