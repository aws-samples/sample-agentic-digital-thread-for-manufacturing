# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``GET /api/admin/conversations`` — every app user's web-chat questions and
answers, read from AgentCore Memory, for admins only.

Why: an admin could not see what people ask. Every web-chat turn is saved in
memory per user and per session, but the app only showed each person their own
Recent sessions; a manual review had to read the stores with scripts.

Pins:
  * admin only, through the app's existing check (``_check_admin``): a
    non-admin and an unauthenticated caller get the normal refusal and memory
    is never read;
  * the users are the app's user pool (Cognito ``sub`` = memory actor id),
    never ``ListActors``: a deployed memory holds thousands of automated-lane
    actors, and a first ListActors page could hide every real user;
  * every ListSessions page of every user is read under ONE per-request page
    budget, the complete in-window set is sorted newest first and paged with
    the cursor, so a follow-up page reaches what the first did not (including
    a newer session on a user's second service page); a user whose sessions
    were not all read is NAMED (``incomplete_users``) and the result never
    claims completeness;
  * the question is the one the user typed (route seed / preambles stripped by
    ``memory_labels.strip_turn_preambles``), the answer is the final assistant
    text without the web UI's control blocks;
  * feedback is read by date through the GSI with a per-day page bound
    (``more.feedback`` past it) through a strict path: a read failure shows as
    ratings unavailable (``feedback_available: false``), never as no ratings;
  * a rating attaches to a turn only when exactly one turn in the session has
    its question (memory keeps no id the browser's ``message_id`` could match);
    otherwise it is counted as ``unplaced_ratings``;
  * ``days`` / ``user`` / ``limit`` / ``cursor`` are validated;
  * a memory or Cognito failure is an error, never an empty list.

Offline: memory, Cognito and the feedback table are faked, and the route's
clock is fixed (``clock``), so no test depends on the time of day it runs at.
"""
from __future__ import annotations

import base64
import functools
import json
import math
import types
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_conversations as conv
import agents.admin_routes as admin_routes
import agents.feedback as feedback_mod
import agents.memory_client as mc
from agents import storage as st

#: The instant the route reads as now in these tests (see ``clock``).
_NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
#: Instants a test whose rows must sit on one UTC day also runs at: just
#: after midnight, inside the half hour before a row two hours old changes
#: day, the last second before it does, and the last second of a day.
_DAY_EDGE_CLOCKS = [datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc),
                    datetime(2026, 10, 22, 1, 45, tzinfo=timezone.utc),
                    datetime(2026, 10, 22, 1, 59, 59, tzinfo=timezone.utc),
                    datetime(2026, 10, 22, 23, 59, 59, tzinfo=timezone.utc)]
_LIST_CONVERSATIONS = conv.list_conversations
_SECRET = "arn:aws:bedrock-agentcore:us-east-1:111122223333:memory/adt_memory-SECRET"
_ROUTE_SEED = "[ROUTE SEED: graph-first routing context]\nconcepts: ..."


def _composed(question: str) -> str:
    return (_ROUTE_SEED + "\n\n---\n\n[Temporal context: now is 2026-10-21 15:00 UTC] "
            + question)


def _event(eid, at, role, content):
    """One raw ListEvents entry as the Strands session manager writes it."""
    text = json.dumps({"message": {"role": role, "content": content}, "message_id": 0})
    return {"eventId": eid, "eventTimestamp": at,
            "payload": [{"conversational": {"role": role.upper(), "content": {"text": text}}}]}


def _turn(prefix, at, question, answer):
    """One stored turn: question, narration + tool call, tool result, answer."""
    return [
        _event(f"{prefix}-q", at, "user", [{"text": _composed(question)}]),
        _event(f"{prefix}-n", at + timedelta(seconds=1), "assistant", [
            {"text": "Checking the graph first."},
            {"toolUse": {"toolUseId": f"{prefix}-t", "name": "explore_graph",
                         "input": {"q": question}}}]),
        _event(f"{prefix}-r", at + timedelta(seconds=2), "user", [
            {"toolResult": {"toolUseId": f"{prefix}-t", "status": "success",
                            "content": [{"text": "rows"}]}}]),
        _event(f"{prefix}-a", at + timedelta(seconds=3), "assistant", [{"text": answer}]),
    ]


def _secret_error(op):
    return ClientError({"Error": {"Code": "AccessDeniedException",
                                  "Message": f"denied on {_SECRET}"}}, op)


class _FakeMemory:
    """bedrock-agentcore stand-in. ListSessions pages are served in STORED
    order (not by time, as the real API) chained by a numeric nextToken;
    ListEvents serves NEWEST FIRST, as the real API does."""

    def __init__(self, sessions: dict, events: dict, *, fail=None):
        self.sessions = sessions          # actor -> [{"sessionId", "createdAt"}]
        self.events = events              # (actor, sid) -> raw events, OLDEST first
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def list_actors(self, **kw):
        # The route must never call this; recorded so a test can prove it.
        self.calls.append(("list_actors", kw))
        actors = list(self.sessions)[:kw["maxResults"]]
        return {"actorSummaries": [{"actorId": a} for a in actors]}

    def list_sessions(self, **kw):
        self.calls.append(("list_sessions", kw))
        if self.fail == "ListSessions":
            raise _secret_error("ListSessions")
        if self.fail == "MemoryNotFound":
            raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                         "Message": "Memory adt_memory-TEST not found"}},
                              "ListSessions")
        if kw["actorId"] not in self.sessions:
            # The real API: a pool user who has never chatted has no actor yet
            # (measured on a deployed memory: "Actor <sub> not found").
            raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                         "Message": f"Actor {kw['actorId']} not found"}},
                              "ListSessions")
        rows = self.sessions.get(kw["actorId"], [])
        start = int(kw.get("nextToken") or 0)
        size = kw["maxResults"]
        resp = {"sessionSummaries": rows[start:start + size]}
        if start + size < len(rows):
            resp["nextToken"] = str(start + size)
        return resp

    def list_events(self, **kw):
        self.calls.append(("list_events", kw))
        if self.fail == "ListEvents":
            raise _secret_error("ListEvents")
        newest_first = list(reversed(self.events.get((kw["actorId"], kw["sessionId"]), [])))
        start = int(kw.get("nextToken") or 0)
        size = kw["maxResults"]
        resp = {"events": newest_first[start:start + size]}
        if start + size < len(newest_first):
            resp["nextToken"] = str(start + size)
        return resp

    def ops(self, op):
        return [kw for name, kw in self.calls if name == op]


class _FakeCognito:
    def __init__(self, users, *, page=60, fail=False):
        self.users = users                # [(username, sub, email)]
        self.page = page
        self.fail = fail
        self.calls: list[dict] = []

    def list_users(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise ClientError({"Error": {"Code": "TooManyRequestsException",
                                         "Message": f"throttled {_SECRET}"}}, "ListUsers")
        start = int(kw.get("PaginationToken") or 0)
        rows = self.users[start:start + self.page]
        resp = {"Users": [{"Username": u, "Attributes": [
            {"Name": "sub", "Value": s}, {"Name": "email", "Value": e}]} for u, s, e in rows]}
        if start + self.page < len(self.users):
            resp["PaginationToken"] = str(start + self.page)
        return resp


def _pk_of(condition) -> str:
    """The partition-key value of a boto3 ConditionBase tree."""
    for value in getattr(condition, "_values", ()):
        if isinstance(value, str):
            return value
        nested = _pk_of(value)
        if nested:
            return nested
    return ""


class _FakeFeedbackTable:
    """The boto3 resource Table behind ``adt-feedback``: rows partitioned by
    UTC day (the date GSI), served in pages of ``min(Limit, page_rows)``
    (``page_rows`` stands in for DynamoDB's 1 MB page cap)."""

    def __init__(self):
        self.rows: list[dict] = []
        self.page_rows = 1000
        self.fail = False
        self.calls: list[dict] = []

    def query(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise _secret_error("Query")
        day = _pk_of(kw["KeyConditionExpression"])
        # DynamoDB's sort-key order: ascending unless ScanIndexForward=False.
        rows = sorted((r for r in self.rows if str(r.get("timestamp", ""))[:10] == day),
                      key=lambda r: str(r["timestamp"]),
                      reverse=kw.get("ScanIndexForward", True) is False)
        start = int((kw.get("ExclusiveStartKey") or {}).get("offset", 0))
        size = min(kw.get("Limit", self.page_rows), self.page_rows)
        resp = {"Items": rows[start:start + size]}
        if start + size < len(rows):
            resp["LastEvaluatedKey"] = {"offset": start + size, "feedback_date": day}
        return resp

    def calls_for(self, day: str) -> list:
        return [kw for kw in self.calls if _pk_of(kw["KeyConditionExpression"]) == day]


class _FakeControl:
    """bedrock-agentcore-control stand-in: GetMemory for the configured id."""

    def __init__(self, name="adt_memory", status="ACTIVE"):
        self.name = name
        self.status = status
        self.fail = False
        self.calls: list[dict] = []

    def get_memory(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise _secret_error("GetMemory")
        return {"memory": {"id": kw["memoryId"], "name": self.name, "status": self.status,
                           "arn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:"
                                  f"memory/{kw['memoryId']}"}}


class _AdminInject(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


def _cfg(**overrides):
    base = dict(has_memory=True, memory_id="adt_memory-TEST", aws_region="us-east-1",
                cognito_user_pool_id="us-east-1_TESTPOOL", feedback_table_name="adt-feedback",
                dev_user_id="dev-user")
    base.update(overrides)
    ns = types.SimpleNamespace(**base)
    ns.has_memory = bool(ns.memory_id) if "has_memory" not in overrides else ns.has_memory
    return ns


def _rating(user, session, question, at, sentiment="positive", **extra):
    """One adt-feedback row as ``POST /api/feedback`` stores it."""
    return {"user_id": user, "session_id": session, "sentiment": sentiment,
            "user_message": question, "message_id": extra.pop("message_id", "msg-1"),
            "timestamp": at.isoformat(), **extra}


@pytest.fixture
def clock(request, monkeypatch):
    """The instant the route reads as now, also this module's ``_NOW``: the
    fixed one above, or another through indirect parametrization."""
    now = getattr(request, "param", _NOW)
    monkeypatch.setitem(globals(), "_NOW", now)
    monkeypatch.setattr(conv, "list_conversations",
                        functools.partial(_LIST_CONVERSATIONS, now=now))
    return now


def _clock_id(now: datetime) -> str:
    return now.strftime("%H%M%S")


@pytest.fixture
def world(monkeypatch, clock):
    """Two pool users, one session each; returns a handle to wire the fakes."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    a_at = _NOW - timedelta(hours=5)
    b_at = _NOW - timedelta(hours=2)
    sessions = {
        "sub-a": [{"sessionId": "session-a1", "createdAt": a_at}],
        "sub-b": [{"sessionId": "session-b1", "createdAt": b_at}],
    }
    events = {
        ("sub-a", "session-a1"): _turn("a1", a_at, "Why did Line 3 stop?",
                                       "Line 3 stopped on a jam.\n\n---FOLLOWUPS---\n"
                                       "Q1: What caused the jam?\n"),
        ("sub-b", "session-b1"): _turn("b1", b_at, "Which lots shipped late?",
                                       "Two lots shipped late."),
    }
    handle = types.SimpleNamespace(
        memory=_FakeMemory(sessions, events),
        cognito=_FakeCognito([("alice", "sub-a", "alice@example.com"),
                              ("bob", "sub-b", "bob@example.com")]),
        feedback=_FakeFeedbackTable(),
        control=_FakeControl(),
        cfg=_cfg())
    monkeypatch.setattr(mc, "get_config", lambda: handle.cfg)
    monkeypatch.setattr(conv, "get_config", lambda: handle.cfg)
    monkeypatch.setattr(feedback_mod, "get_config", lambda: handle.cfg)
    monkeypatch.setattr(mc.MemoryClient, "_client", lambda self: handle.memory)
    monkeypatch.setattr(mc.MemoryClient, "_control_client", lambda self: handle.control,
                        raising=False)
    if hasattr(mc, "reset_confirmed_memories"):
        mc.reset_confirmed_memories()
    monkeypatch.setattr(conv, "_cognito_client", lambda region: handle.cognito)
    monkeypatch.setattr(st.DynamoDBStorage, "_get_table", lambda self: handle.feedback)
    st.reset_unconfigured_warnings()
    yield handle
    if hasattr(mc, "reset_confirmed_memories"):
        mc.reset_confirmed_memories()


def _client(is_admin=True) -> TestClient:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_AdminInject, is_admin=is_admin)
    return TestClient(app)


def _get(params=None, is_admin=True):
    return _client(is_admin).get("/api/admin/conversations", params=params or {})


def _all_pages(params=None) -> list[dict]:
    """Every page of one listing, following ``next``."""
    params = dict(params or {})
    pages = [_get(params).json()]
    while pages[-1]["next"]:
        pages.append(_get({**params, "cursor": pages[-1]["next"]}).json())
    return pages


def _many_sessions(world, actor, count, *, newest_at=None, hours_old=24):
    """``count`` older sessions for ``actor``; with ``newest_at`` set, the
    NEWEST one sits at that index of the stored (service) order."""
    rows = [{"sessionId": f"{actor}-old-{i:03d}",
             "createdAt": _NOW - timedelta(hours=hours_old, minutes=i)} for i in range(count)]
    if newest_at is not None:
        rows.insert(newest_at, {"sessionId": f"{actor}-newest",
                                "createdAt": _NOW - timedelta(minutes=5)})
    world.memory.sessions[actor] = rows


# ── who may call it ────────────────────────────────────────────────────────

def test_admin_gets_sessions_across_two_users_newest_first_with_logins(world):
    resp = _get()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["session_id"] for s in body["sessions"]] == ["session-b1", "session-a1"]
    users = {s["session_id"]: s["user"] for s in body["sessions"]}
    assert users["session-a1"] == {"user_id": "sub-a", "username": "alice",
                                   "email": "alice@example.com", "resolved": True}
    assert users["session-b1"]["username"] == "bob"
    assert {u["user_id"] for u in body["users"]} == {"sub-a", "sub-b"}
    assert body["next"] is None
    assert body["complete"] is True and body["incomplete_users"] == []
    assert body["more"] == {"users": False, "feedback": False}
    assert body["feedback_available"] is True
    a1 = body["sessions"][1]
    assert a1["turn_count"] == 1
    assert a1["started_at"] and a1["last_activity_at"]


def test_non_admin_is_refused_and_memory_is_never_read(world):
    resp = _get(is_admin=False)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access required"}
    assert world.memory.calls == [] and world.cognito.calls == []


def test_unauthenticated_caller_is_refused_by_the_auth_middleware(world, monkeypatch):
    from agents.auth.middleware import AuthMiddleware
    from agents.config import get_config
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(admin_routes.router)
    resp = TestClient(app).get("/api/admin/conversations")
    get_config.cache_clear()
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}
    assert world.memory.calls == []


def test_a_request_with_no_admin_flag_at_all_is_refused(world):
    app = FastAPI()
    app.include_router(admin_routes.router)
    resp = TestClient(app).get("/api/admin/conversations")
    assert resp.status_code == 403
    assert world.memory.calls == []


# ── what a turn shows ──────────────────────────────────────────────────────

def test_question_is_what_the_user_typed_and_answer_is_the_final_text(world):
    body = _get().json()
    turn = next(s for s in body["sessions"] if s["session_id"] == "session-a1")["turns"][0]
    assert turn["question"] == "Why did Line 3 stop?"
    assert "ROUTE SEED" not in turn["question"] and "Temporal context" not in turn["question"]
    # The final assistant message, not the narration before the tool call; the
    # web UI's ---FOLLOWUPS--- block becomes plain text (public_answer).
    assert turn["answer"].startswith("Line 3 stopped on a jam.")
    assert "Checking the graph" not in turn["answer"]
    assert "---FOLLOWUPS---" not in turn["answer"]
    assert turn["answer_withheld"] is False
    assert turn["asked_at"] and turn["answered_at"]


def test_a_turn_the_integrity_gate_ran_for_shows_no_answer_text(world, monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    turn = _get().json()["sessions"][0]["turns"][0]
    assert turn["question"] == "Which lots shipped late?"
    assert turn["answer"] is None and turn["answer_withheld"] is True


def test_automated_lane_sessions_are_left_out_and_counted(world):
    world.memory.sessions["sub-a"].append(
        {"sessionId": "smoke-123", "createdAt": _NOW - timedelta(hours=1)})
    body = _get().json()
    assert "smoke-123" not in [s["session_id"] for s in body["sessions"]]
    assert body["excluded"]["automated"] == 1


def test_a_long_answer_is_cut_and_flagged(world, monkeypatch):
    monkeypatch.setattr(conv, "MAX_ANSWER_CHARS", 10)
    turn = _get().json()["sessions"][0]["turns"][0]
    assert len(turn["answer"]) == 10 and turn["answer_truncated"] is True


# ── F1: users come from the pool; every session page is read ──────────────

def test_users_come_from_the_pool_and_automated_actors_are_never_read(world):
    # A deployed memory holds thousands of automated-lane actors ("session_*").
    for i in range(150):
        world.memory.sessions[f"session_eval{i:04d}"] = [
            {"sessionId": f"run-{i}", "createdAt": _NOW - timedelta(minutes=1)}]
    body = _get().json()
    assert world.memory.ops("list_actors") == []
    assert {kw["actorId"] for kw in world.memory.ops("list_sessions")} == {"sub-a", "sub-b"}
    assert [s["session_id"] for s in body["sessions"]] == ["session-b1", "session-a1"]
    assert body["complete"] is True


def test_a_newer_session_on_a_users_second_service_page_is_listed_first(world):
    # The shared login's newest session is not on its first ListSessions page.
    _many_sessions(world, "sub-a", 150, newest_at=120)
    body = _get().json()
    assert body["sessions"][0]["session_id"] == "sub-a-newest"
    tokens = [kw.get("nextToken") for kw in world.memory.ops("list_sessions")
              if kw["actorId"] == "sub-a"]
    assert tokens == [None, "100"]
    assert all(kw["filter"] == {"eventFilter": "HAS_EVENTS"}
               for kw in world.memory.ops("list_sessions"))
    assert body["matched"] == 152 and body["complete"] is True


def test_a_follow_up_cursor_reaches_every_session_the_first_page_did_not(world):
    _many_sessions(world, "sub-a", 140, newest_at=130, hours_old=30)
    pages = _all_pages({"limit": "25"})
    seen = [s["session_id"] for p in pages for s in p["sessions"]]
    assert len(seen) == len(set(seen)) == 142
    assert seen[0] == "sub-a-newest"
    assert "session-b1" in seen and seen.index("session-b1") == 1
    assert all(p["complete"] for p in pages)
    starts = [s["started_at"] for p in pages for s in p["sessions"]]
    assert starts == sorted(starts, reverse=True)


def test_the_session_page_budget_names_users_not_fully_read(world, monkeypatch):
    monkeypatch.setattr(conv, "MAX_SESSION_PAGES", 3)
    _many_sessions(world, "sub-a", 250)        # three service pages
    body = _get().json()
    assert len(world.memory.ops("list_sessions")) == 3
    assert body["complete"] is False
    assert [u["user_id"] for u in body["incomplete_users"]] == ["sub-a"]
    assert body["incomplete_users"][0]["username"] == "alice"


def test_a_budget_that_covers_every_page_reports_a_complete_scan(world, monkeypatch):
    monkeypatch.setattr(conv, "MAX_SESSION_PAGES", 4)
    _many_sessions(world, "sub-a", 250)
    body = _get().json()
    assert len(world.memory.ops("list_sessions")) == 4
    assert body["complete"] is True and body["incomplete_users"] == []


def test_users_left_unread_by_the_budget_are_named_and_never_started(world, monkeypatch):
    monkeypatch.setattr(conv, "MAX_SESSION_PAGES", 1)
    body = _get().json()
    assert [kw["actorId"] for kw in world.memory.ops("list_sessions")] == ["sub-a"]
    assert [u["user_id"] for u in body["incomplete_users"]] == ["sub-b"]
    assert [s["session_id"] for s in body["sessions"]] == ["session-a1"]


def test_every_user_gets_a_first_page_before_any_user_gets_a_second(world, monkeypatch):
    monkeypatch.setattr(conv, "MAX_SESSION_PAGES", 2)
    _many_sessions(world, "sub-a", 250)
    body = _get().json()
    assert sorted(kw["actorId"] for kw in world.memory.ops("list_sessions")) == ["sub-a", "sub-b"]
    assert [u["user_id"] for u in body["incomplete_users"]] == ["sub-a"]


def test_the_user_filter_spends_the_whole_budget_on_that_user(world, monkeypatch):
    monkeypatch.setattr(conv, "MAX_SESSION_PAGES", 3)
    _many_sessions(world, "sub-a", 250)
    body = _get({"user": "sub-a"}).json()
    assert [kw["actorId"] for kw in world.memory.ops("list_sessions")] == ["sub-a"] * 3
    assert body["complete"] is True and body["matched"] == 250


def test_past_the_user_pool_bound_the_result_says_users_were_not_all_read(world, monkeypatch):
    monkeypatch.setattr(conv, "MAX_COGNITO_PAGES", 1)
    world.cognito.page = 1
    body = _get().json()
    assert len(world.cognito.calls) == 1
    assert body["more"]["users"] is True and body["complete"] is False
    assert [s["session_id"] for s in body["sessions"]] == ["session-a1"]


def test_the_whole_pool_is_walked_once_within_its_bound(world):
    world.cognito.page = 1
    world.cognito.users.append(("zed", "sub-z", "z@example.com"))
    body = _get().json()
    assert len(world.cognito.calls) == 3
    assert all(kw["AttributesToGet"] == ["sub", "email"] for kw in world.cognito.calls)
    assert all(kw["UserPoolId"] == "us-east-1_TESTPOOL" for kw in world.cognito.calls)
    assert {u["user_id"] for u in body["users"]} == {"sub-a", "sub-b", "sub-z"}
    assert body["more"]["users"] is False


def test_a_filtered_user_outside_the_pool_shows_its_id(world):
    world.memory.sessions["sub-gone"] = [
        {"sessionId": "session-g1", "createdAt": _NOW - timedelta(hours=1)}]
    body = _get({"user": "sub-gone"}).json()
    assert body["sessions"][0]["user"] == {"user_id": "sub-gone", "username": None,
                                           "email": None, "resolved": False}


def test_the_scope_is_current_app_users_and_complete_means_that_scope(world):
    # A former user's retained sessions are outside the all-users scope (the
    # page says so); a known id still reaches them through the user filter.
    world.memory.sessions["sub-removed"] = [
        {"sessionId": "session-r1", "createdAt": _NOW - timedelta(minutes=30)}]
    body = _get().json()
    assert body["scope"] == "current_app_users"
    assert body["complete"] is True
    assert "session-r1" not in [s["session_id"] for s in body["sessions"]]
    filtered = _get({"user": "sub-removed"}).json()
    assert filtered["scope"] == "current_app_users"
    assert [s["session_id"] for s in filtered["sessions"]] == ["session-r1"]


def test_the_pages_typed_user_id_check_matches_the_routes():
    # The page refuses a typed id the route would 422; both use one pattern.
    from pathlib import Path
    logic = (Path(conv.__file__).resolve().parents[1] / "frontend" / "src" / "pages"
             / "admin" / "tabs" / "conversations-logic.ts").read_text(encoding="utf-8")
    assert f"/{conv.ACTOR_ID_PATTERN}/" in logic


def test_no_user_pool_configured_lists_the_local_user_only(world):
    world.cfg = _cfg(cognito_user_pool_id=None)
    world.memory.sessions["dev-user"] = [
        {"sessionId": "session-dev", "createdAt": _NOW - timedelta(hours=1)}]
    body = _get().json()
    assert world.cognito.calls == []
    assert body["logins_available"] is False
    assert [s["session_id"] for s in body["sessions"]] == ["session-dev"]


# ── F2 + F5: feedback is bounded and its failure is visible ───────────────

def _noon_days_ago(days: int) -> datetime:
    """Noon UTC ``days`` UTC days before ``_NOW``: rows placed within hours
    of it share one UTC day whatever the time of day ``_NOW`` is."""
    return (_NOW - timedelta(days=days)).replace(hour=12, minute=0, second=0, microsecond=0)


def _session_at(world, session_id: str, at: datetime, question: str) -> None:
    """sub-b's only session: one turn asking *question* at *at*."""
    world.memory.sessions["sub-b"] = [{"sessionId": session_id, "createdAt": at}]
    world.memory.events[("sub-b", session_id)] = _turn(session_id, at, question, "Two lots.")


@pytest.mark.parametrize("clock", [_NOW, *_DAY_EDGE_CLOCKS], indirect=True, ids=_clock_id)
def test_the_feedback_read_is_bounded_per_day_and_marked_incomplete(world):
    day = _noon_days_ago(1)
    world.feedback.page_rows = 1
    world.feedback.rows = [_rating("sub-b", "session-b1", "Which lots shipped late?",
                                   day + timedelta(seconds=i), message_id=f"m-{i}")
                           for i in range(125)]
    body = _get().json()
    day_calls = world.feedback.calls_for(day.date().isoformat())
    # Not one Query call per stored page: 125 pages are not all read.
    assert len(day_calls) < len(world.feedback.rows)
    assert len(day_calls) == conv.MAX_FEEDBACK_PAGES_PER_DAY
    assert body["more"]["feedback"] is True and body["feedback_available"] is True


def test_the_feedback_read_uses_the_date_index_with_a_page_limit_and_projection(world):
    _get()
    calls = world.feedback.calls
    assert calls and all(kw["IndexName"] == "feedback-date-index" for kw in calls)
    assert all(kw["Limit"] == conv.FEEDBACK_PAGE_SIZE for kw in calls)
    assert all("ProjectionExpression" in kw for kw in calls)
    assert all("assistant_response" not in json.dumps(kw["ExpressionAttributeNames"])
               for kw in calls)


def test_a_feedback_read_failure_shows_ratings_unavailable_not_none(world):
    world.feedback.fail = True
    resp = _get()
    assert resp.status_code == 200
    body = resp.json()
    assert body["feedback_available"] is False
    assert body["feedback_error"]["error_ref"]
    assert [s["session_id"] for s in body["sessions"]] == ["session-b1", "session-a1"]
    assert all(s["rating"] is None for s in body["sessions"])
    assert _SECRET not in resp.text and "111122223333" not in resp.text


def test_no_feedback_table_configured_shows_ratings_unavailable(world):
    world.cfg = _cfg(feedback_table_name=None)
    body = _get().json()
    assert body["feedback_available"] is False
    assert "not configured" in body["feedback_error"]["error"]
    assert world.feedback.calls == []
    assert all(s["rating"] is None for s in body["sessions"])


def test_the_lenient_feedback_path_other_routes_use_is_unchanged(world):
    world.feedback.fail = True
    store = st.FeedbackStorage("adt-feedback", "us-east-1")
    assert store.get_by_time_range((_NOW - timedelta(days=1)).isoformat(),
                                   _NOW.isoformat()) == []


# ── F4: a rating attaches only to the one turn it can belong to ───────────

def _repeated_session(world, *questions):
    at = _NOW - timedelta(hours=1)
    world.memory.sessions["sub-b"] = [{"sessionId": "session-b2", "createdAt": at}]
    events = []
    for i, q in enumerate(questions):
        events += _turn(f"x{i}", at + timedelta(minutes=5 * i), q, f"Answer {i}.")
    world.memory.events[("sub-b", "session-b2")] = events
    return at


def test_a_delayed_rating_on_a_repeated_question_is_left_unplaced(world):
    # The reviewer's case: the same question twice, then the FIRST answer is
    # rated after the second was asked.
    at = _repeated_session(world, "Same question?", "Same question?")
    world.feedback.rows = [_rating("sub-b", "session-b2", "Same question?",
                                   at + timedelta(minutes=8), message_id="msg-first")]
    session = _get().json()["sessions"][0]
    assert [t["feedback"] for t in session["turns"]] == [None, None]
    assert session["unplaced_ratings"] == 1
    assert session["rating"] == {"positive": 0, "negative": 0}


def test_a_rating_whose_question_matches_exactly_one_turn_is_attached(world):
    at = _repeated_session(world, "Same question?", "Other question?", "Same question?")
    world.feedback.rows = [
        _rating("sub-b", "session-b2", "Other question?", at + timedelta(minutes=20),
                "negative", message_id="msg-other", user_comment="wrong shift"),
        _rating("sub-b", "session-b2", "Same question?", at + timedelta(minutes=1),
                message_id="msg-same"),
        # Another session's and another user's thumbs never land here.
        _rating("sub-a", "session-b2", "Other question?", at + timedelta(minutes=21)),
        _rating("sub-b", "elsewhere", "Other question?", at + timedelta(minutes=21)),
    ]
    session = _get().json()["sessions"][0]
    first, second, third = session["turns"]
    assert second["feedback"] == {"sentiment": "negative", "comment": "wrong shift",
                                  "at": (at + timedelta(minutes=20)).isoformat()}
    assert first["feedback"] is None and third["feedback"] is None
    assert session["rating"] == {"positive": 0, "negative": 1}
    assert session["unplaced_ratings"] == 1


def test_a_rating_matching_no_question_is_left_unplaced(world):
    at = _NOW - timedelta(hours=2)
    world.feedback.rows = [_rating("sub-b", "session-b1", "A question never asked here",
                                   at + timedelta(minutes=1))]
    session = _get().json()["sessions"][0]
    assert session["turns"][0]["feedback"] is None
    assert session["unplaced_ratings"] == 1


def test_a_later_thumb_on_the_same_message_replaces_the_earlier_one(world):
    # The latest row for a message is its rating; the earlier negative
    # row's comment belonged to the rating it replaced.
    at = _NOW - timedelta(hours=2)
    world.feedback.rows = [
        _rating("sub-b", "session-b1", "Which lots shipped late?", at + timedelta(minutes=1),
                "negative", message_id="msg-b", user_comment="missing lot 7"),
        _rating("sub-b", "session-b1", "Which lots shipped late?", at + timedelta(minutes=2),
                "positive", message_id="msg-b"),
    ]
    session = _get().json()["sessions"][0]
    assert session["turns"][0]["feedback"] == {
        "sentiment": "positive", "at": (at + timedelta(minutes=2)).isoformat()}
    assert session["rating"] == {"positive": 1, "negative": 0}
    assert session["unplaced_ratings"] == 0


def test_a_thumb_and_its_later_comment_show_together(world):
    # The chat posts the thumb, then a second row with the comment and the
    # same message id and sentiment.
    at = _NOW - timedelta(hours=2)
    world.feedback.rows = [
        _rating("sub-b", "session-b1", "Which lots shipped late?", at + timedelta(minutes=1),
                "negative", message_id="msg-b"),
        _rating("sub-b", "session-b1", "Which lots shipped late?", at + timedelta(minutes=2),
                "negative", message_id="msg-b", user_comment="missing lot 7"),
    ]
    session = _get().json()["sessions"][0]
    assert session["turns"][0]["feedback"]["sentiment"] == "negative"
    assert session["turns"][0]["feedback"]["comment"] == "missing lot 7"
    assert session["rating"] == {"positive": 0, "negative": 1}


def test_the_feedback_read_is_newest_first(world):
    _get()
    assert world.feedback.calls
    assert all(kw.get("ScanIndexForward") is False for kw in world.feedback.calls)


@pytest.mark.parametrize("clock", [_NOW, *_DAY_EDGE_CLOCKS], indirect=True, ids=_clock_id)
def test_past_the_feedback_bound_a_shown_rating_is_the_latest_reachable(world):
    # Five older negative rows and a newer positive one past the page cap,
    # all on one UTC day. The shown rating must be the newest, positive.
    at = _noon_days_ago(1)
    _session_at(world, "session-b4", at, "Which lots shipped late?")
    world.feedback.page_rows = 1
    world.feedback.rows = [
        _rating("sub-b", "session-b4", "Which lots shipped late?",
                at + timedelta(minutes=1 + i), "negative", message_id="msg-b")
        for i in range(conv.MAX_FEEDBACK_PAGES_PER_DAY)
    ] + [_rating("sub-b", "session-b4", "Which lots shipped late?",
                 at + timedelta(minutes=30), "positive", message_id="msg-b")]
    body = _get().json()
    session = next(s for s in body["sessions"] if s["session_id"] == "session-b4")
    assert body["more"]["feedback"] is True
    assert session["turns"][0]["feedback"]["sentiment"] == "positive"
    assert session["rating"] == {"positive": 1, "negative": 0}


def test_an_answers_rows_either_side_of_utc_midnight_show_the_newest(world):
    # No day holds more than the bound, so every row is read whichever UTC
    # day it sits on: nothing is marked cut and the newest row is shown.
    midnight = _noon_days_ago(1).replace(hour=0)
    _session_at(world, "session-b5", midnight - timedelta(minutes=30), "Which lots shipped late?")
    world.feedback.page_rows = 1
    world.feedback.rows = [
        _rating("sub-b", "session-b5", "Which lots shipped late?",
                midnight - timedelta(minutes=25 - i), "negative", message_id="msg-b")
        for i in range(conv.MAX_FEEDBACK_PAGES_PER_DAY)
    ] + [_rating("sub-b", "session-b5", "Which lots shipped late?",
                 midnight + timedelta(minutes=5), "positive", message_id="msg-b")]
    body = _get().json()
    session = next(s for s in body["sessions"] if s["session_id"] == "session-b5")
    assert body["more"]["feedback"] is False
    assert session["turns"][0]["feedback"]["sentiment"] == "positive"
    assert session["rating"] == {"positive": 1, "negative": 0}
    assert session["unverified_ratings"] == 0


def _two_day_session(world):
    """One question asked three UTC days ago; returns (that noon, the next noon)."""
    noon = _noon_days_ago(3)
    world.memory.sessions["sub-b"] = [{"sessionId": "session-b3",
                                       "createdAt": noon - timedelta(hours=1)}]
    world.memory.events[("sub-b", "session-b3")] = _turn(
        "y", noon - timedelta(hours=1), "Which lots shipped late?", "Two lots.")
    return noon, noon + timedelta(days=1)


def test_a_cut_later_day_never_lets_an_earlier_days_rating_show_as_current(world):
    # The reviewer's cross-day case: a negative rating one day, then on the
    # next day a positive replacement older than that day's read bound. The
    # earlier day's negative row is the newest one read for that answer, but
    # it cannot be shown as current: a newer row may sit past the bound.
    first, second = _two_day_session(world)
    world.feedback.page_rows = 1
    world.feedback.rows = [
        _rating("sub-b", "session-b3", "Which lots shipped late?", first, "negative",
                message_id="target"),
        _rating("sub-b", "session-b3", "Which lots shipped late?",
                second + timedelta(seconds=1), "positive", message_id="target"),
    ] + [_rating("sub-a", "session-other", "Other?", second + timedelta(seconds=10 + i),
                 message_id=f"other-{i}") for i in range(conv.MAX_FEEDBACK_PAGES_PER_DAY)]
    body = _get().json()
    session = next(s for s in body["sessions"] if s["session_id"] == "session-b3")
    assert body["more"]["feedback"] is True
    assert session["turns"][0]["feedback"] is None
    assert session["rating"] == {"positive": 0, "negative": 0}
    assert session["unverified_ratings"] == 1


def test_a_rating_on_the_last_cut_day_or_after_it_is_still_shown(world):
    # Rows a cut day leaves unread are that day's oldest, so its read rows
    # and every later complete day's rows are each answer's newest.
    first, second = _two_day_session(world)
    world.feedback.page_rows = 1
    world.feedback.rows = [
        _rating("sub-a", "session-other", "Other?", first + timedelta(seconds=i),
                message_id=f"old-{i}") for i in range(conv.MAX_FEEDBACK_PAGES_PER_DAY + 1)
    ] + [_rating("sub-b", "session-b3", "Which lots shipped late?", second, "positive",
                 message_id="target")]
    body = _get().json()
    session = next(s for s in body["sessions"] if s["session_id"] == "session-b3")
    assert body["more"]["feedback"] is True
    assert session["turns"][0]["feedback"]["sentiment"] == "positive"
    assert session["unverified_ratings"] == 0


def test_each_message_counts_once_however_many_rows_it_has(world):
    at = _repeated_session(world, "Same question?", "Same question?")
    world.feedback.rows = [
        _rating("sub-b", "session-b2", "Same question?", at + timedelta(minutes=8 + i),
                message_id="msg-first") for i in range(3)]
    session = _get().json()["sessions"][0]
    assert session["unplaced_ratings"] == 1


def test_a_session_whose_earliest_turns_were_not_read_places_no_rating(world, monkeypatch):
    # The rated turn may be one of the unread ones with the same question.
    monkeypatch.setattr(conv, "MAX_EVENTS_PER_SESSION", 4)
    at = _repeated_session(world, "Same question?", "Same question?")
    world.feedback.rows = [_rating("sub-b", "session-b2", "Same question?",
                                   at + timedelta(minutes=1))]
    session = _get().json()["sessions"][0]
    assert session["events_truncated"] is True
    assert [t["feedback"] for t in session["turns"]] == [None]
    assert session["unplaced_ratings"] == 1


# ── validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("days", ["0", "8", "-1", "abc"])
def test_days_outside_one_to_seven_is_rejected_before_any_read(world, days):
    assert _get({"days": days}).status_code == 422
    assert world.memory.calls == []


def test_days_narrows_the_window(world):
    world.memory.sessions["sub-a"].append(
        {"sessionId": "session-a-old", "createdAt": _NOW - timedelta(days=4)})
    ids = [s["session_id"] for s in _get({"days": "3"}).json()["sessions"]]
    assert "session-a-old" not in ids and "session-a1" in ids
    body = _get({"days": "7"}).json()
    assert "session-a-old" in [s["session_id"] for s in body["sessions"]]
    assert body["window"]["days"] == 7


@pytest.mark.parametrize("user", ["bad id", "x;drop", "a" * 129, "-leading-dash", "../etc"])
def test_user_filter_shape_is_validated_before_any_read(world, user):
    assert _get({"user": user}).status_code == 422
    assert world.memory.calls == []


def test_user_filter_reads_only_that_user(world):
    body = _get({"user": "sub-b"}).json()
    assert [s["session_id"] for s in body["sessions"]] == ["session-b1"]
    assert world.memory.ops("list_actors") == []
    assert [kw["actorId"] for kw in world.memory.ops("list_sessions")] == ["sub-b"]


@pytest.mark.parametrize("cursor", ["not-base64!", "e30", "x" * 600])
def test_a_malformed_cursor_is_rejected(world, cursor):
    resp = _get({"cursor": cursor})
    assert resp.status_code == (422 if len(cursor) > 512 else 400)
    assert world.memory.calls == []


def test_a_cursor_from_another_filter_is_rejected(world):
    world.memory.sessions["sub-a"].append(
        {"sessionId": "session-a2", "createdAt": _NOW - timedelta(hours=3)})
    nxt = _get({"limit": "1"}).json()["next"]
    assert nxt
    assert _get({"limit": "1", "cursor": nxt, "days": "6"}).status_code == 400
    assert _get({"limit": "1", "cursor": nxt, "user": "sub-a"}).status_code == 400


# ── paging and the other bounds ────────────────────────────────────────────

def _three_sessions(world):
    world.memory.sessions["sub-a"].append(
        {"sessionId": "session-a2", "createdAt": _NOW - timedelta(hours=3)})
    world.memory.events[("sub-a", "session-a2")] = _turn(
        "a2", _NOW - timedelta(hours=3), "Middle question?", "Middle answer.")


def test_paging_walks_every_session_once_in_order(world):
    _three_sessions(world)
    first = _get({"limit": "2"}).json()
    assert [s["session_id"] for s in first["sessions"]] == ["session-b1", "session-a2"]
    assert first["next"]
    # Only the returned page's sessions had their events read.
    assert {kw["sessionId"] for kw in world.memory.ops("list_events")} == {
        "session-b1", "session-a2"}
    second = _get({"limit": "2", "cursor": first["next"]}).json()
    assert [s["session_id"] for s in second["sessions"]] == ["session-a1"]
    assert second["next"] is None


def test_a_page_exactly_at_the_limit_has_no_next_and_one_past_it_does(world):
    _three_sessions(world)
    assert _get({"limit": "3"}).json()["next"] is None
    assert _get({"limit": "2"}).json()["next"] is not None


def test_page_size_is_bounded(world):
    assert _get({"limit": str(conv.MAX_PAGE_SIZE)}).status_code == 200
    assert _get({"limit": str(conv.MAX_PAGE_SIZE + 1)}).status_code == 422
    assert _get({"limit": "0"}).status_code == 422


def test_events_past_the_bound_flag_the_session_as_cut(world, monkeypatch):
    monkeypatch.setattr(conv, "MAX_EVENTS_PER_SESSION", 4)
    at = _NOW - timedelta(hours=1)
    world.memory.sessions["sub-b"] = [{"sessionId": "session-long", "createdAt": at}]
    world.memory.events[("sub-b", "session-long")] = (
        _turn("l1", at, "First?", "One.") + _turn("l2", at + timedelta(minutes=1), "Second?", "Two."))
    session = _get().json()["sessions"][0]
    assert session["session_id"] == "session-long"
    assert session["events_truncated"] is True
    assert [t["question"] for t in session["turns"]] == ["Second?"]
    exact = _get({"user": "sub-a"}).json()["sessions"][0]
    assert exact["events_truncated"] is False


def test_the_bounds_are_stated_in_the_response(world):
    limits = _get().json()["limits"]
    assert limits == {
        "max_users": conv.MAX_COGNITO_PAGES * conv.COGNITO_PAGE_SIZE,
        "max_session_pages": conv.MAX_SESSION_PAGES,
        "session_page_size": conv.SESSION_PAGE_SIZE,
        "max_events_per_session": conv.MAX_EVENTS_PER_SESSION,
        "max_page_size": conv.MAX_PAGE_SIZE,
        "max_days": conv.MAX_DAYS,
        "max_feedback_pages_per_day": conv.MAX_FEEDBACK_PAGES_PER_DAY,
        "feedback_page_size": conv.FEEDBACK_PAGE_SIZE,
    }
    assert conv.MAX_EVENTS_PER_SESSION == mc._EVENTS_MAX_TOTAL
    # The stated budget covers the measured pool: a shared login with 935
    # sessions plus eight users with one service page each.
    assert conv.MAX_SESSION_PAGES >= math.ceil(935 / conv.SESSION_PAGE_SIZE) + 8


# ── failures are errors, never an empty list ───────────────────────────────

@pytest.mark.parametrize("op", ["ListSessions", "ListEvents"])
def test_a_memory_failure_is_an_error_not_an_empty_list(world, op):
    world.memory.fail = op
    resp = _get()
    assert resp.status_code == 503
    body = resp.json()
    assert body["source"] == "memory" and "sessions" not in body
    assert body["error_ref"] and body["error_category"] == "auth_failed"
    assert _SECRET not in resp.text and "111122223333" not in resp.text


def test_a_pool_user_who_has_never_chatted_is_listed_with_no_sessions(world):
    # Most pool users have never asked a question, so memory has no actor for
    # them. That is an empty history, not a memory failure.
    world.cognito.users.append(("carol", "sub-c", "carol@example.com"))
    resp = _get()
    assert resp.status_code == 200
    body = resp.json()
    assert [s["session_id"] for s in body["sessions"]] == ["session-b1", "session-a1"]
    assert body["complete"] is True and body["incomplete_users"] == []
    assert "sub-c" in {kw["actorId"] for kw in world.memory.ops("list_sessions")}


def test_a_missing_memory_is_still_an_error_not_an_empty_list(world):
    world.memory.fail = "MemoryNotFound"
    resp = _get()
    assert resp.status_code == 503
    assert resp.json()["source"] == "memory"


# ── N2: actor-not-found means "never chatted" only on the app's memory ────

def test_a_wrong_existing_memory_is_an_error_not_an_empty_history(world):
    # The configured id names another, existing memory: it holds none of the
    # pool's users, so every ListSessions answers "Actor <sub> not found".
    world.control.name = "other_team_memory"
    world.memory.sessions = {}
    resp = _get()
    assert resp.status_code == 503
    assert resp.json()["source"] == "memory" and "sessions" not in resp.json()
    assert world.memory.ops("list_sessions") == []


def test_a_memory_that_is_not_active_is_an_error(world):
    world.control.status = "DELETING"
    resp = _get()
    assert resp.status_code == 503
    assert resp.json()["source"] == "memory"


def test_a_failed_memory_check_is_an_error_without_upstream_text(world):
    world.control.fail = True
    resp = _get()
    assert resp.status_code == 503
    assert resp.json()["source"] == "memory"
    assert _SECRET not in resp.text and "111122223333" not in resp.text


def test_the_apps_memory_is_checked_once_per_process(world):
    assert _get().status_code == 200
    assert _get().status_code == 200
    assert world.control.calls == [{"memoryId": "adt_memory-TEST"}]


def test_a_failed_memory_check_is_not_cached(world):
    world.control.fail = True
    assert _get().status_code == 503
    world.control.fail = False
    assert _get().status_code == 200
    assert len(world.control.calls) == 2


def test_the_check_uses_the_discovery_name_prefix():
    # Reused from config (the app's memory discovery), never a copied literal.
    import inspect
    source = inspect.getsource(mc)
    assert "from agents.config import _MEMORY_NAME_PREFIX" in source
    assert '"adt_memory"' not in source and "'adt_memory'" not in source


def test_an_unconfirmed_memory_never_reads_actor_not_found_as_empty(world):
    # The reader on its own: without the app-memory check, "Actor <sub> not
    # found" from some other memory is a failure, never an empty history.
    client = mc.MemoryClient(memory_id="other_existing_memory-123")
    with pytest.raises(mc.MemoryUnavailableError):
        client.list_session_page("real-pool-sub", 100)
    assert world.control.calls == []


def test_a_cognito_failure_is_an_error_not_an_empty_list(world):
    world.cognito.fail = True
    resp = _get()
    assert resp.status_code == 503
    body = resp.json()
    assert body["source"] == "cognito" and "sessions" not in body
    assert body["error_ref"]
    assert _SECRET not in resp.text
    assert world.memory.ops("list_sessions") == []


def test_memory_that_is_not_configured_is_an_error(world):
    world.cfg = _cfg(memory_id=None, has_memory=False)
    resp = _get()
    assert resp.status_code == 503
    assert resp.json()["source"] == "memory"
    assert world.memory.calls == []


def test_no_session_in_the_window_is_an_empty_page_not_an_error(world):
    world.memory.sessions = {"sub-a": [], "sub-b": []}
    body = _get().json()
    assert body["sessions"] == [] and body["next"] is None and body["matched"] == 0
    assert body["complete"] is True


def test_with_no_clock_given_the_window_ends_at_the_current_utc_time(world, monkeypatch):
    monkeypatch.setattr(conv, "list_conversations", _LIST_CONVERSATIONS)
    before = datetime.now(timezone.utc)
    window = _get().json()["window"]
    end = datetime.fromisoformat(window["end"])
    assert before <= end <= datetime.now(timezone.utc)
    assert datetime.fromisoformat(window["start"]) == end - timedelta(days=conv.DEFAULT_DAYS)


# ── cursor shape ───────────────────────────────────────────────────────────

def test_the_cursor_is_opaque_and_round_trips():
    created = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    token = conv.encode_cursor(days=7, user=None,
                               last=conv.Candidate("sub-a", "session-1", created))
    assert conv.decode_cursor(token, days=7, user=None) == (
        -created.timestamp(), "sub-a", "session-1")
    raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert set(raw) == {"v", "d", "u", "t", "a", "s"}
    with pytest.raises(conv.CursorError):
        conv.decode_cursor(token, days=6, user=None)


def test_the_route_handler_only_offloads():
    import ast
    import inspect
    import textwrap
    node = ast.parse(textwrap.dedent(inspect.getsource(
        admin_routes.admin_conversations_route))).body[0]
    assert len(node.body) == 2  # docstring + return await offload(...)
    call = node.body[1].value.value
    assert call.func.id == "offload" and call.args[0].id == "admin_conversations"
