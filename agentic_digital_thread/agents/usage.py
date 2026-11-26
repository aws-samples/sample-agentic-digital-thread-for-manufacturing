# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from agents.config import get_config
from agents.storage import UsageStorage

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    user_id: str
    timestamp: str
    session_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int
    tool_usage: dict
    user_email: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "tool_usage": self.tool_usage,
        }
        if self.user_email is not None:
            d["user_email"] = self.user_email
        return d


class UsageService:
    def __init__(self):
        cfg = get_config()
        self._storage = UsageStorage(cfg.usage_table_name, cfg.aws_region)

    def store(self, record: UsageRecord) -> bool:
        return self._storage.store(record.to_dict())

    def get_by_time_range(self, start: str, end: str) -> list[dict]:
        return self._storage.get_by_time_range(start, end)


_service = UsageService()


async def track_usage(
    metrics: dict,
    session_id: str,
    user_id: str,
    model_id: str,
    user_email: str | None = None,
) -> None:
    try:
        from datetime import datetime, timezone
        record = UsageRecord(
            user_id=user_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            session_id=session_id,
            model_id=model_id,
            input_tokens=metrics.get("input_tokens", 0),
            output_tokens=metrics.get("output_tokens", 0),
            total_tokens=metrics.get("total_tokens", 0),
            latency_ms=metrics.get("latency_ms", 0),
            tool_usage=metrics.get("tool_usage", {}),
            user_email=user_email,
        )
        _service.store(record)
    except Exception as e:
        logger.debug("track_usage failed (non-fatal): %s", e)
