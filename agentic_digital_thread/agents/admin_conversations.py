# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin Conversations: every app user's web-chat questions and answers.

``GET /api/admin/conversations`` (admin only; the route lives in
``agents/admin_routes.py``) lists the chat sessions saved in the app's
AgentCore Memory for the users of the app's user pool. The per-user Recent
sessions picker only ever shows a person their own. Read-only: nothing here
writes memory, Cognito or the feedback table.

Who is listed (``scope: "current_app_users"`` in every response): the users
of the app's Cognito user pool as it is now. A memory actor id is the user's
Cognito ``sub``, so each pool user is read by that id. The memory's own actor
list (``ListActors``) is never used: a deployed memory holds thousands of
automated-lane actors (``session_eval...``, ``session_smok...``) beside a
handful of real users, so walking it would read thousands of actors on every
page load, and a first actor page can hide every real user. Users removed
from the user pool are therefore not listed, although memory keeps their
sessions for up to 7 days; an admin who knows such a user's id reaches them
with the ``user`` filter (the page's User id field). ``complete`` means
complete for this scope: every listed user's sessions were read, never that
every actor in memory was. With no user pool configured (local development)
the local development user is listed.

Which memory: before a ListSessions "Actor <sub> not found" may mean "this
user has never chatted", ``MemoryClient.confirm_app_memory`` confirms the
configured memory is the app's own (one ``GetMemory`` per process: status
ACTIVE and a name under the prefix the app's memory discovery binds). A wrong
but existing memory answers "not found" for every user, which would otherwise
read as nobody having chatted; it is a memory error instead.

Reused, never re-implemented: the ``MemoryClient`` readers (paging, event
ordering, payload parsing), ``memory_turns.display_blocks`` (the
response-integrity display policy the Recent sessions reload applies),
``memory_labels.strip_turn_preambles`` (the question the user typed) and
``public_answer.external_answer_text`` (the answer without the web UI's
control blocks). Quick questions never appear: the async path runs with
``write_memory=False``.

Bounds per request, each fixed and each reported when reached:

* window: sessions whose ``createdAt`` falls in the last ``days`` (1 to
  ``MAX_DAYS`` = 7, the memory's raw-event retention);
* users: at most ``MAX_COGNITO_PAGES`` (10) ``ListUsers`` pages of
  ``COGNITO_PAGE_SIZE`` (60), so at most 600 users; past that ``more.users``;
* sessions: EVERY ``HAS_EVENTS`` ``ListSessions`` page of every listed user
  (``SESSION_PAGE_SIZE`` = 100 sessions a page; the API is not ordered by
  time and has no time filter, so a user's newest session can sit on any
  page), within ``MAX_SESSION_PAGES`` (40) calls per request across all
  users, read breadth first: every user gets a first page before any user
  gets a second. A user still unread when the budget runs out is named in
  ``incomplete_users`` and ``complete`` is false; the page then does not
  claim newest-first order or a full count. A ``user`` filter spends the
  whole budget on that user (up to 4,000 sessions);
* events: read only for the returned page (``limit`` <= ``MAX_PAGE_SIZE`` =
  25), at most ``MAX_EVENTS_PER_SESSION`` (400) each, the Recent sessions
  reload's own bound; a cut session carries ``events_truncated`` (its OLDEST
  turns are the ones missing);
* text: a question past ``MAX_QUESTION_CHARS`` or an answer past
  ``MAX_ANSWER_CHARS`` is cut and flagged ``*_truncated``;
* feedback: read by UTC day through the ``feedback-date-index`` GSI, NEWEST
  FIRST, at most ``MAX_FEEDBACK_PAGES_PER_DAY`` (5) Query pages of
  ``FEEDBACK_PAGE_SIZE`` (100) rows a day, projected to the rating fields;
  past that ``more.feedback``, and the rows left unread are a day's OLDEST,
  so a rating shown is always the newest one read for its answer. Because the
  bound is per day, a rating whose newest read row is from a day BEFORE the
  newest cut day may have a newer row past that day's bound: it is not shown
  and is counted in ``unverified_ratings`` instead. The read
  is strict: a failure leaves the sessions listed with ``feedback_available:
  false`` and a fixed public error (``feedback_error``), never as "no
  ratings";
* AWS calls, worst case: 1 GetMemory (the first request of a process) + 10
  ListUsers + 40 ListSessions + 25 x 4 ListEvents + 8 days x 5 feedback Query
  pages = 191.

Ratings: each thumb and each comment is its own feedback row, so the newest
row of an answer (by the browser's ``message_id``) is its rating. Memory
keeps no id that ``message_id`` could be matched to (the ask never sends it),
so a rating attaches to a turn by its question text, and only when exactly
one turn in the session asks that question (asked no later than the rating).
Anything else, and every rating in a session whose earliest turns were not
read, is counted in ``unplaced_ratings`` instead of being put on a turn it
may not belong to.

Paging: the complete in-bound set is sorted by ``createdAt`` newest first,
then user id, then session id. ``next`` is an opaque cursor holding the last
returned session's sort key and the ``days``/``user`` filter it was issued
for; each request rescans, so ``next`` reaches every session the scan finds
and starts strictly after the cursor even when newer sessions arrive in
between. A cursor never reaches an AWS call; it is only compared locally.

A memory or Cognito failure raises ``ConversationsError`` (the route answers
503 through ``public_errors``), never an empty list.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from agents.config import get_config, mode_a_integrity_enforced
from agents.memory_client import (
    _EVENTS_MAX_TOTAL,
    MemoryClient,
    MemoryUnavailableError,
    _is_automated_session,
)
from agents.memory_labels import _parse_created_at, strip_turn_preambles
from agents.memory_turns import _is_question, display_blocks
from agents.public_answer import external_answer_text
from agents.public_errors import public_error
from agents.storage import FeedbackStorage, StorageReadError

MAX_DAYS = 7
DEFAULT_DAYS = 7
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 25
SESSION_PAGE_SIZE = 100
#: ListSessions calls per request across all users. Measured: the shared
#: login held 935 sessions (10 pages) beside 8 one-page users, 18 calls.
MAX_SESSION_PAGES = 40
MAX_EVENTS_PER_SESSION = _EVENTS_MAX_TOTAL
MAX_QUESTION_CHARS = 4_000
MAX_ANSWER_CHARS = 20_000
MAX_COMMENT_CHARS = 2_000
MAX_COGNITO_PAGES = 10
COGNITO_PAGE_SIZE = 60          # the ListUsers ceiling
MAX_FEEDBACK_PAGES_PER_DAY = 5
FEEDBACK_PAGE_SIZE = 100
MAX_CURSOR_CHARS = 512
_MAX_ID_CHARS = 255
_WORKERS = 8

#: Shape of the ``user`` filter: an AgentCore actor id as this app writes it
#: (a Cognito ``sub``, or the local dev user id). Validated before any call.
ACTOR_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$"
_ACTOR_ID_RE = re.compile(ACTOR_ID_PATTERN)

FEEDBACK_UNCONFIGURED = ("Ratings are unavailable: the feedback table is not "
                         "configured for this deployment.")

#: What a listing covers (the ``scope`` field): the users of the app's user
#: pool now, plus a ``user`` filter's id. ``complete`` means complete for it.
SCOPE = "current_app_users"


class MemoryNotConfiguredError(Exception):
    """This deployment has no AgentCore Memory, so there is nothing to list."""


class ConversationsError(Exception):
    """A memory or Cognito read failed. ``source`` names which; ``cause`` is
    the root exception, for ``public_errors`` to classify and log."""

    def __init__(self, source: str, cause: BaseException):
        super().__init__(f"{source} read failed")
        self.source = source
        self.cause = cause


class CursorError(ValueError):
    """The ``cursor`` is malformed or belongs to another filter."""


@dataclass(frozen=True)
class Candidate:
    """One session found in the window, before its events are read."""

    actor: str
    session_id: str
    created: datetime

    def key(self) -> tuple:
        return (-self.created.timestamp(), self.actor, self.session_id)


@dataclass
class _Scan:
    candidates: list = field(default_factory=list)
    #: Actor ids whose sessions were not all read (the page budget ran out).
    incomplete: list = field(default_factory=list)
    automated: int = 0
    undated: int = 0


@dataclass
class _Pool:
    users: dict          # sub -> {"username", "email"}
    cut: bool            # MAX_COGNITO_PAGES reached with more users left
    available: bool      # a user pool is configured


@dataclass
class _Feedback:
    index: dict          # (user id, session id) -> rows, oldest first
    available: bool
    cut: bool = False
    error: Optional[dict] = None
    #: The newest UTC day (``YYYY-MM-DD``) the bound cut, or None. A rating
    #: whose newest read row is from an earlier day may have a newer row past
    #: that day's bound, so it is not shown as current.
    newest_cut_day: Optional[str] = None


def _root_cause(exc: BaseException) -> BaseException:
    seen = 0
    while exc.__cause__ is not None and seen < 10:
        exc, seen = exc.__cause__, seen + 1
    return exc


def _as_utc(value: object) -> Optional[datetime]:
    dt = _parse_created_at(value)
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _fan_out(fn, items: list) -> list:
    """``fn`` over ``items`` on a bounded pool, results in input order; the
    first failure propagates."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


# ── cursor ─────────────────────────────────────────────────────────────────

def encode_cursor(*, days: int, user: Optional[str], last: Candidate) -> str:
    raw = json.dumps({"v": 1, "d": days, "u": user or "", "t": last.created.timestamp(),
                      "a": last.actor, "s": last.session_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(token: str, *, days: int, user: Optional[str]) -> tuple:
    """The sort key a cursor names; ``CursorError`` unless it is exactly the
    shape ``encode_cursor`` writes, for this same ``days`` and ``user``."""
    if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_CHARS:
        raise CursorError("bad cursor")
    try:
        obj = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CursorError("bad cursor") from exc
    if not isinstance(obj, dict) or set(obj) != {"v", "d", "u", "t", "a", "s"}:
        raise CursorError("bad cursor")
    t, actor, session = obj["t"], obj["a"], obj["s"]
    valid = (obj["v"] == 1 and obj["d"] == days and obj["u"] == (user or "")
             and isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
             and isinstance(actor, str) and 0 < len(actor) <= _MAX_ID_CHARS
             and isinstance(session, str) and 0 < len(session) <= _MAX_ID_CHARS)
    if not valid:
        raise CursorError("bad cursor")
    return (-float(t), actor, session)


# ── users (the app user pool) ──────────────────────────────────────────────

def _cognito_client(region: str):
    import boto3
    return boto3.client("cognito-idp", region_name=region)


def _login_page(client, pool_id: str, token: Optional[str]) -> dict:
    kwargs = {"UserPoolId": pool_id, "Limit": COGNITO_PAGE_SIZE,
              "AttributesToGet": ["sub", "email"]}
    if token:
        kwargs["PaginationToken"] = token
    return client.list_users(**kwargs)


def read_user_pool() -> _Pool:
    """The app's users, ``sub -> {username, email}``, from a walk of the user
    pool bounded to ``MAX_COGNITO_PAGES`` pages (``cut`` past it). Raises
    ``ConversationsError("cognito", ...)`` on a Cognito failure."""
    cfg = get_config()
    pool_id = getattr(cfg, "cognito_user_pool_id", None)
    if not pool_id:
        return _Pool({}, False, False)
    found: dict = {}
    token = None
    try:
        client = _cognito_client(cfg.aws_region)
        for _ in range(MAX_COGNITO_PAGES):
            resp = _login_page(client, pool_id, token)
            for user in resp.get("Users") or []:
                attrs = {a.get("Name"): a.get("Value") for a in user.get("Attributes") or []}
                sub = attrs.get("sub")
                if isinstance(sub, str) and sub:
                    found[sub] = {"username": user.get("Username"), "email": attrs.get("email")}
            token = resp.get("PaginationToken")
            if not token:
                return _Pool(found, False, True)
    except Exception as exc:  # noqa: BLE001 - surfaced as an error, never empty
        raise ConversationsError("cognito", _root_cause(exc)) from exc
    return _Pool(found, True, True)


def _listed_actors(user: Optional[str], pool: _Pool) -> list[str]:
    """Whose sessions are read: the ``user`` filter's id, else every pool
    user, else (no pool configured: local development) the local user."""
    if user:
        return [user]
    if pool.available:
        return sorted(pool.users)
    dev_user = getattr(get_config(), "dev_user_id", None)
    return [dev_user] if dev_user else []


def _user_view(actor: str, logins: dict) -> dict:
    info = logins.get(actor)
    return {"user_id": actor, "username": info["username"] if info else None,
            "email": info["email"] if info else None, "resolved": bool(info)}


# ── memory reads ───────────────────────────────────────────────────────────

def _add_summary(scan: _Scan, actor: str, summary: dict, start: datetime, end: datetime):
    sid = summary.get("sessionId")
    if not isinstance(sid, str) or not sid:
        return
    if _is_automated_session(sid):
        scan.automated += 1
        return
    created = _as_utc(summary.get("createdAt"))
    if created is None:
        scan.undated += 1
        return
    if start <= created <= end:
        scan.candidates.append(Candidate(actor, sid, created))


def _scan_sessions(memory: MemoryClient, client, actors: list[str],
                   start: datetime, end: datetime) -> _Scan:
    """Every in-window session of ``actors``, sorted newest first: each
    actor's ListSessions pages read to the end, breadth first (every actor
    gets a first page before any actor gets a second), within
    ``MAX_SESSION_PAGES`` calls in all. Actors still unread when the budget
    runs out are left in ``incomplete``. Each round runs on the bounded pool."""
    scan = _Scan()
    tokens: dict = {}
    pending = list(actors)
    budget = MAX_SESSION_PAGES
    while pending and budget > 0:
        batch, pending = pending[:budget], pending[budget:]
        budget -= len(batch)
        pages = _fan_out(lambda a: memory.list_session_page(
            a, SESSION_PAGE_SIZE, tokens.get(a), client=client), batch)
        for actor, (summaries, next_token) in zip(batch, pages):
            for summary in summaries:
                _add_summary(scan, actor, summary, start, end)
            if next_token:
                tokens[actor] = next_token
                pending.append(actor)
    scan.incomplete = sorted(pending)
    scan.candidates.sort(key=Candidate.key)
    return scan


def _page(candidates: list, after: Optional[tuple], limit: int) -> tuple[list, bool]:
    rest = [c for c in candidates if after is None or c.key() > after]
    return rest[:limit], len(rest) > limit


def _read_memory(actors: list[str], limit: int, after: Optional[tuple],
                 start: datetime, end: datetime):
    memory = MemoryClient()
    try:
        # Before "Actor <sub> not found" may mean "never chatted", the
        # configured memory must be the app's own (once per process).
        memory.confirm_app_memory()
        client = memory._client()
        scan = _scan_sessions(memory, client, actors, start, end)
        page, has_more = _page(scan.candidates, after, limit)
        reads = _fan_out(lambda c: memory.read_session_events(
            c.session_id, c.actor, MAX_EVENTS_PER_SESSION, client=client), page)
    except MemoryUnavailableError as exc:
        raise ConversationsError("memory", _root_cause(exc)) from exc
    return scan, page, has_more, reads


# ── turns ──────────────────────────────────────────────────────────────────

def _cut(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


def session_turns(events: list) -> list[dict]:
    """Each question and its final answer, oldest first. A user event that
    is not a question (a tool result, or a runtime-authored notice that strips
    to nothing) never opens a turn. A turn the response-integrity gate ran for
    keeps no answer text (``answer_withheld``), as the Recent sessions reload."""
    shown = display_blocks(events, enforced=mode_a_integrity_enforced())
    turns: list[dict] = []
    for event, view in zip(events, shown):
        if event.role == "user":
            question = strip_turn_preambles(event.content).strip() if _is_question(event) else ""
            if question:
                turns.append({"question": question, "asked_at": event.timestamp,
                              "answer": None, "answered_at": None,
                              "answer_withheld": False, "feedback": None})
            continue
        if not turns:
            continue
        if view.label is not None:
            turns[-1]["answer_withheld"] = True
            continue
        text = "".join(b.get("text", "") for b in view.blocks if b.get("type") == "text")
        if text.strip():
            turns[-1]["answer"], turns[-1]["answered_at"] = text, event.timestamp
    return turns


def _finish_turn(turn: dict) -> dict:
    answer = external_answer_text(turn["answer"]) if turn["answer"] else None
    question, q_cut = _cut(turn["question"], MAX_QUESTION_CHARS)
    answer_cut = False
    if answer is not None:
        answer, answer_cut = _cut(answer, MAX_ANSWER_CHARS)
    return {**turn, "question": question, "question_truncated": q_cut,
            "answer": answer, "answer_truncated": answer_cut}


# ── feedback ───────────────────────────────────────────────────────────────

def _read_feedback(start: datetime, end: datetime) -> _Feedback:
    """The window's feedback rows by (user id, session id), read through the
    bounded strict path. A missing table or a failed read is ``available:
    False`` with a fixed public error (the detail goes to the log only)."""
    cfg = get_config()
    table = getattr(cfg, "feedback_table_name", None)
    if not table:
        return _Feedback({}, False, error={"error": FEEDBACK_UNCONFIGURED})
    try:
        rows, cut_days = FeedbackStorage(table, cfg.aws_region).get_by_time_range_bounded(
            start.isoformat(), end.isoformat(),
            max_pages_per_day=MAX_FEEDBACK_PAGES_PER_DAY, page_size=FEEDBACK_PAGE_SIZE)
    except StorageReadError as exc:
        return _Feedback({}, False, error=public_error(
            _root_cause(exc), "admin.conversations.feedback", default="upstream_error"))
    index: dict = {}
    for row in rows:
        key = (str(row.get("user_id") or ""), str(row.get("session_id") or ""))
        index.setdefault(key, []).append(row)
    return _Feedback(index, True, bool(cut_days), newest_cut_day=max(cut_days, default=None))


def _norm(text: object) -> str:
    return " ".join(str(text or "").split())


def _turn_for(turns: list, row: dict, events_cut: bool) -> Optional[int]:
    """The ONE turn a rating can belong to, or None. Memory keeps no id the
    browser's ``message_id`` could be matched to, so a rating attaches by its
    question text: only when exactly one turn in the session asks it, asked
    no later than the rating. A session whose earliest turns were not read
    places none (an unread turn may ask the same question)."""
    text = _norm(row.get("user_message"))
    if events_cut or not text:
        return None
    matching = [i for i, t in enumerate(turns) if _norm(t["question"]) == text]
    if len(matching) != 1:
        return None
    when, asked = _as_utc(row.get("timestamp")), _as_utc(turns[matching[0]]["asked_at"])
    if when is not None and asked is not None and asked > when:
        return None
    return matching[0]


def _rating_key(row: dict, position: int) -> str:
    """One rated answer: the browser's ``message_id``, else the row itself."""
    message_id = row.get("message_id")
    return f"m:{message_id}" if isinstance(message_id, str) and message_id else f"r:{position}"


def _latest_per_message(rows: list) -> list:
    """The newest row of each rated answer (``message_id``; a row without one
    stands alone), oldest first. Each thumb and each comment is its own row,
    and the chat posts the comment after the thumb with the same message id
    and sentiment, so the newest row is the answer's current rating."""
    newest_first = sorted(rows, key=lambda r: str(r.get("timestamp", "")), reverse=True)
    latest: dict = {}
    for position, row in enumerate(newest_first):
        latest.setdefault(_rating_key(row, position), row)
    return sorted(latest.values(), key=lambda r: str(r.get("timestamp", "")))


def _row_feedback(row: dict) -> dict:
    feedback: dict = {}
    if row.get("sentiment") in ("positive", "negative"):
        feedback["sentiment"] = row["sentiment"]
    if row.get("user_comment"):
        feedback["comment"] = str(row["user_comment"])[:MAX_COMMENT_CHARS]
    feedback["at"] = row.get("timestamp")
    return feedback


def _attach_feedback(turns: list, rows: list, events_cut: bool,
                     newest_cut_day: Optional[str] = None) -> tuple[dict, int, int]:
    """Join each answer's latest rating onto its turn (``_latest_per_message``;
    on a turn rated more than once, the latest wins whole), returning the
    turns' rating counts, how many rated answers could not be placed on one
    turn, and how many were not shown because their newest read row is from
    a day before ``newest_cut_day`` (a newer row may be past that bound)."""
    unplaced = unverified = 0
    for row in _latest_per_message(rows):
        if newest_cut_day and str(row.get("timestamp", ""))[:10] < newest_cut_day:
            unverified += 1
            continue
        index = _turn_for(turns, row, events_cut)
        if index is None:
            unplaced += 1
            continue
        turns[index]["feedback"] = _row_feedback(row)
    sentiments = [(t["feedback"] or {}).get("sentiment") for t in turns]
    return ({"positive": sentiments.count("positive"),
             "negative": sentiments.count("negative")}, unplaced, unverified)


# ── the listing ────────────────────────────────────────────────────────────

def _session_view(cand: Candidate, events: list, cut: bool, feedback: _Feedback,
                  logins: dict) -> dict:
    turns = session_turns(events)
    rating, unplaced, unverified = None, 0, 0
    if feedback.available:
        rating, unplaced, unverified = _attach_feedback(
            turns, feedback.index.get((cand.actor, cand.session_id), []), cut,
            feedback.newest_cut_day)
    return {
        "session_id": cand.session_id,
        "user": _user_view(cand.actor, logins),
        "started_at": cand.created.isoformat(),
        "last_activity_at": events[-1].timestamp if events else None,
        "turn_count": len(turns),
        "turns": [_finish_turn(t) for t in turns],
        "rating": rating,
        "unplaced_ratings": unplaced,
        "unverified_ratings": unverified,
        "events_truncated": cut,
    }


def _limits() -> dict:
    """The bounds in force (read at call time, so a test that narrows one
    sees it reported)."""
    return {
        "max_users": MAX_COGNITO_PAGES * COGNITO_PAGE_SIZE,
        "max_session_pages": MAX_SESSION_PAGES,
        "session_page_size": SESSION_PAGE_SIZE,
        "max_events_per_session": MAX_EVENTS_PER_SESSION,
        "max_page_size": MAX_PAGE_SIZE,
        "max_days": MAX_DAYS,
        "max_feedback_pages_per_day": MAX_FEEDBACK_PAGES_PER_DAY,
        "feedback_page_size": FEEDBACK_PAGE_SIZE,
    }


def _listing(*, days: int, user: Optional[str], window: tuple, pool: _Pool,
             actors: list, memory: tuple, feedback: _Feedback) -> dict:
    """The response body for one page (see the module docstring)."""
    scan, page, has_more, reads = memory
    users_cut = pool.cut and not user
    listed_users = sorted(pool.users) if pool.available else actors
    return {
        "sessions": [_session_view(c, events, cut, feedback, pool.users)
                     for c, (events, cut) in zip(page, reads)],
        "next": encode_cursor(days=days, user=user, last=page[-1]) if has_more else None,
        "matched": len(scan.candidates),
        "scope": SCOPE,
        "complete": not scan.incomplete and not users_cut,
        "incomplete_users": [_user_view(a, pool.users) for a in scan.incomplete],
        "window": {"days": days, "start": window[0].isoformat(), "end": window[1].isoformat()},
        "users": [_user_view(a, pool.users) for a in listed_users],
        "logins_available": pool.available,
        "feedback_available": feedback.available,
        "feedback_error": feedback.error,
        "more": {"users": pool.cut, "feedback": feedback.cut},
        "excluded": {"automated": scan.automated, "undated": scan.undated},
        "limits": _limits(),
    }


def list_conversations(*, days: int = DEFAULT_DAYS, user: Optional[str] = None,
                       limit: int = DEFAULT_PAGE_SIZE, cursor: Optional[str] = None,
                       now: Optional[datetime] = None) -> dict:
    """One page of sessions across the app's users (see the module docstring
    for the bounds). ``days``, ``user`` and ``limit`` arrive validated by the
    route; they are re-checked here so a direct caller cannot widen a bound."""
    if not (1 <= days <= MAX_DAYS and 1 <= limit <= MAX_PAGE_SIZE) or (
            user is not None and not _ACTOR_ID_RE.match(user)):
        raise ValueError("days, limit or user outside the route's bounds")
    after = decode_cursor(cursor, days=days, user=user) if cursor else None
    if not get_config().has_memory:
        raise MemoryNotConfiguredError()
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    pool = read_user_pool()
    actors = _listed_actors(user, pool)
    memory = _read_memory(actors, limit, after, start, end)
    return _listing(days=days, user=user, window=(start, end), pool=pool, actors=actors,
                    memory=memory, feedback=_read_feedback(start, end))
