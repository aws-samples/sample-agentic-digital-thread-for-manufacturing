# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import boto3

from agents.config import get_config
from agents.storage import GuardrailStorage

logger = logging.getLogger(__name__)


class GuardrailClient:
    def __init__(
        self,
        guardrail_id: str | None = None,
        guardrail_version: str | None = None,
        region: str | None = None,
    ):
        cfg = get_config()
        self._guardrail_id = guardrail_id or cfg.guardrail_id
        self._guardrail_version = guardrail_version or cfg.guardrail_version or "DRAFT"
        self._region = region or cfg.aws_region
        self._client = None

    def _get_client(self):
        if self._client is None:
            from .aws_session import bedrock_runtime_client
            self._client = bedrock_runtime_client(self._region)
        return self._client

    def evaluate(self, content: str, source: str = "INPUT") -> dict | None:
        cfg = get_config()
        if not cfg.has_guardrail:
            return None
        try:
            resp = self._get_client().apply_guardrail(
                guardrailIdentifier=self._guardrail_id,
                guardrailVersion=self._guardrail_version,
                source=source,
                content=[{"text": {"text": content}}],
            )
            return {"action": resp.get("action"), "assessments": resp.get("assessments", [])}
        except Exception as e:
            logger.error("GuardrailClient.evaluate failed: %s", e)
            return None


@dataclass
class GuardrailViolationRecord:
    user_id: str
    timestamp: str
    session_id: str
    source: str
    action: str
    assessments: list = field(default_factory=list)
    content_preview: str = ""

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "source": self.source,
            "action": self.action,
            "assessments": self.assessments,
            "content_preview": self.content_preview,
        }


class GuardrailService:
    def __init__(self):
        cfg = get_config()
        self._storage = GuardrailStorage(cfg.guardrail_table_name, cfg.aws_region)

    def store_violation(self, record: GuardrailViolationRecord) -> bool:
        return self._storage.store(record.to_dict())

    def get_by_time_range(self, start: str, end: str) -> list[dict]:
        return self._storage.get_by_time_range(start, end)
