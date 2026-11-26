# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Saved-prompt runs carry the same per-turn time line as /api/ask.

Running a saved prompt (POST /prompts/{id}/execute), refreshing a pinned card
(POST /dashboard-cards/{id}/refresh) and a scheduled run used to hand the raw
prompt text to the orchestrator, with no clock and no user zone, so "today"
or "this week" in a saved prompt had nothing to resolve against. Now:

- execute and refresh take the browser's zone and locale (optional body) and
  enrich the text through ``time_context.with_time_context``, the helper
  ``server._prepare_question`` uses;
- a scheduled run gets the UTC-only line with a fresh clock (no stored zone);
- the raw prompt text still rides as the turn's intent (metric lookup).

Fully offline (no Bedrock, no AWS, no sockets).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.prompts as prompts
import agents.server as server
import agents.time_context as tc
from agents.models import AskRequest

_TEXT = "Which lines missed plan today?"


class _PromptStore:
    def __init__(self):
        self.rows = {"t1": {"template_id": "t1", "text": _TEXT, "pinned": True, "title": "T"}}

    def get(self, template_id):
        return self.rows.get(template_id)


class _CardStore:
    def __init__(self):
        self.saved = []

    def get(self, card_id):
        return {"card_id": card_id, "prompt_id": "t1"}

    def get_by_prompt(self, _template_id):
        return []

    def save(self, card):
        self.saved.append(card)


@pytest.fixture
def seen(monkeypatch):
    calls: list = []
    monkeypatch.setattr(prompts, "_prompt_store", _PromptStore())
    monkeypatch.setattr(prompts, "_card_store", _CardStore())
    monkeypatch.setattr(prompts, "_execute_fn", lambda text: calls.append(text) or "answer")
    monkeypatch.setattr(prompts, "_video_fn", None)
    return calls


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(prompts.router)
    return TestClient(app)


def _zoned_body():
    return {"timezone": "America/Denver", "locale": "en-US"}


def _assert_zoned(prompt):
    assert prompt.startswith("[Temporal context: ")
    assert "User timezone: America/Denver" in prompt
    assert "User locale: en-US." in prompt
    assert prompt.endswith(_TEXT)
    assert prompt.raw_question == _TEXT


def test_execute_with_the_browser_zone_carries_the_zoned_line(seen):
    response = _client().post("/api/prompts/t1/execute", json=_zoned_body())
    assert response.status_code == 200
    (prompt,) = seen
    _assert_zoned(prompt)


def test_refresh_with_the_browser_zone_carries_the_zoned_line(seen):
    response = _client().post("/api/dashboard-cards/c1/refresh", json=_zoned_body())
    assert response.status_code == 200
    (prompt,) = seen
    _assert_zoned(prompt)


@pytest.mark.parametrize("path", ["/api/prompts/t1/execute", "/api/dashboard-cards/c1/refresh"])
def test_a_run_with_no_body_gets_the_utc_only_line(seen, path):
    assert _client().post(path).status_code == 200
    (prompt,) = seen
    assert prompt.startswith("[Temporal context: Current UTC time: ")
    assert "User timezone" not in prompt
    assert prompt.endswith(_TEXT)


def test_scheduled_run_gets_a_fresh_utc_only_line(seen, monkeypatch):
    class _Clock:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(tc, "datetime", _Clock)
    asyncio.run(prompts._execute_scheduled_prompt("t1"))
    (prompt,) = seen
    assert prompt.startswith("[Temporal context: Current UTC time: 2026-11-01T10:00:00Z (Sunday).")
    assert "User timezone" not in prompt
    assert prompt.raw_question == _TEXT


def test_ask_and_prompt_runs_share_one_enrichment_helper(seen, monkeypatch):
    marks: list = []

    def _fake(text, raw, tz_name=None, locale=None):
        marks.append((raw, tz_name, locale))
        return tc.PreparedQuestion(f"<timed> {text}", raw)

    monkeypatch.setattr(tc, "with_time_context", _fake)
    out = server._prepare_question(AskRequest(question="q", timezone="Asia/Tokyo", locale="ja"))
    assert out == "<timed> q" and out.raw_question == "q"
    _client().post("/api/prompts/t1/execute", json={"timezone": "Asia/Tokyo"})
    assert marks == [("q", "Asia/Tokyo", "ja"), (_TEXT, "Asia/Tokyo", None)]


def test_local_scheduled_turn_reads_the_metric_layer_with_the_raw_text(monkeypatch):
    import agents.metric_turn_notice as mtn
    import agents.orchestrator as orch
    asked: list = []
    monkeypatch.setattr(server, "get_config", lambda: type("C", (), {"has_runtime": False})())
    monkeypatch.setattr(mtn, "results_for_turn", lambda q, s: asked.append(q) or [])
    monkeypatch.setattr(orch, "get_orchestrator", lambda: (lambda p: "ok"))
    server._execute_prompt_admitted(tc.with_time_context(_TEXT, _TEXT))
    assert asked == [_TEXT]
