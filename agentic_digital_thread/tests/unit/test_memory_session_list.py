# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the SESSION-LIST read path
(``MemoryClient.list_sessions``) — the '+ New' picker's backend.

No AWS, no network: a fake PAGINATED bedrock-agentcore client drives every
test. Pins the four measured live-environment defects:

  (a) the API does NOT return sessions newest-first and pages beyond the
      display limit — the FULL paged set must be collected and sorted BEFORE
      truncating (truncate-before-sort was the regression that hid the user's
      recent chats behind a stale first page);
  (b) automated-lane sessions (smoke/eval/... — ``AUTOMATED_SESSION_PREFIXES``)
      are excluded from the user-facing listing, ANCHORED at the id start so a
      real ``session-...`` chat always survives;
  (c) a session whose events aged out (7-day TTL) rehydrates nothing and is
      not offered — filtered SERVER-SIDE via
      ``filter={"eventFilter": "HAS_EVENTS"}`` on ``list_sessions`` itself, so
      the client never needs a per-candidate existence probe; a LABEL fetch
      failure (a separate, best-effort call) never drops a session (fail
      open);
  (d) labels come from the first GENUINE user question, never the ROUTE SEED
      or Temporal-context preambles, with a readable date fallback;
  (e) missing/garbage ``createdAt`` never crashes and never sorts above real
      timestamps;
  (f) pagination is bounded — a client that would page forever terminates.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from unittest.mock import patch

import agents.memory_client as mc
from agents.memory_client import MemoryClient


def _fake_config(*, has_memory: bool = True, memory_id: str = "mem-123",
                 region: str = "us-east-1"):
    return types.SimpleNamespace(
        has_memory=has_memory, memory_id=memory_id, aws_region=region,
    )


def _user_event(text: str, ts: str = "2026-08-01T00:00:00Z", eid: str = "ev"):
    return {
        "eventId": eid,
        "createdAt": ts,
        "payload": [{"conversational": {"role": "USER", "content": {"text": text}}}],
    }


def _tool_turn_event(ts: str = "2026-08-01T00:00:00Z"):
    envelope = json.dumps(
        {"message": {"role": "user", "content": [{"toolResult": {"toolUseId": "t1"}}]}}
    )
    return _user_event(envelope, ts=ts)


class _FakePagedClient:
    """Paginated bedrock-agentcore stand-in.

    * ``pages`` — list of sessionSummaries lists; ``nextToken`` chains them.
    * ``events_by_session`` — per-session ``list_events`` responses (default
      one plain user event, used for labeling).
    * ``label_probe_errors`` — session ids whose LABEL ``list_events`` call
      raises (production reads this as "no label", never as "drop the
      session" — existence is no longer decided client-side).
    * ``never_ending`` — every ``list_sessions`` response carries a nextToken
      (a client that would page FOREVER).

    ``list_sessions`` honors ``filter={"eventFilter": "HAS_EVENTS"}`` exactly
    as real AgentCore Memory does: sessions with an EMPTY events list for
    their id are excluded from the page server-side, mirroring the real API
    rather than requiring the client to probe for the same fact.
    """

    def __init__(self, pages, events_by_session=None, label_probe_errors=(),
                 never_ending=False, default_events=None):
        self.pages = pages
        self.events_by_session = events_by_session or {}
        self.label_probe_errors = set(label_probe_errors)
        self.never_ending = never_ending
        self.default_events = (
            default_events if default_events is not None
            else [_user_event("What is the OEE on Line 3?")]
        )
        self.list_sessions_calls = 0
        self.list_events_calls = []

    def _has_events(self, sid):
        return len(self.events_by_session.get(sid, self.default_events)) > 0

    def list_sessions(self, **kwargs):
        self.list_sessions_calls += 1
        assert kwargs.get("filter") == {"eventFilter": "HAS_EVENTS"}, (
            "list_sessions must request the server-side HAS_EVENTS filter")
        if self.never_ending:
            page = [s for s in self.pages[0] if self._has_events(s["sessionId"])]
            return {"sessionSummaries": page, "nextToken": "more"}
        token = kwargs.get("nextToken")
        idx = int(token) if token else 0
        raw_page = self.pages[idx]
        page = [s for s in raw_page if self._has_events(s["sessionId"])]
        resp = {"sessionSummaries": page}
        if idx + 1 < len(self.pages):
            resp["nextToken"] = str(idx + 1)
        return resp

    def list_events(self, **kwargs):
        self.list_events_calls.append(kwargs)
        sid = kwargs["sessionId"]
        if sid in self.label_probe_errors:
            raise RuntimeError("label probe boom")
        return {"events": self.events_by_session.get(sid, self.default_events)}


def _make_client(fake, cfg=None):
    cfg = cfg or _fake_config()
    with patch.object(mc, "get_config", return_value=cfg):
        client = MemoryClient()
    client._client = lambda: fake
    return client, cfg


def _sess(sid, created):
    return {"sessionId": sid, "createdAt": created, "actorId": "u1"}


# ---------------------------------------------------------------------------
# (a) Full paged set is collected and sorted BEFORE truncation — an API that
#     returns pages in NON-newest-first order still yields newest-first output
#     with the newest item present. This is THE live regression: the newest
#     session lives on the LAST page; truncating any earlier subset loses it.
# ---------------------------------------------------------------------------
def test_newest_session_on_last_page_survives_and_output_is_newest_first():
    # 30 stale sessions on page 1 (more than max_results=20), newest on page 2.
    page1 = [_sess(f"session-old-{i:02d}", f"2026-07-{(i % 28) + 1:02d}T00:00:00Z")
             for i in range(30)]
    page2 = [_sess("session-newest", "2026-08-06T09:09:00Z"),
             _sess("session-second", "2026-08-05T08:00:00Z")]
    fake = _FakePagedClient(pages=[page1, page2])
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1", max_results=20)
    ids = [s.session_id for s in result]
    # The newest session (last API page) MUST be present and FIRST.
    assert ids[0] == "session-newest"
    assert ids[1] == "session-second"
    # Strictly newest-first throughout.
    keys = [mc._created_sort_key(s.created_at) for s in result]
    assert keys == sorted(keys, reverse=True)
    # And the display limit still applies (truncation AFTER the full sort).
    assert len(result) == 20
    # Both pages were actually fetched.
    assert fake.list_sessions_calls == 2


# ---------------------------------------------------------------------------
# (b) Automated prefixes are excluded; a real `session-...` id survives; the
#     match is ANCHORED (an automated token mid-id never excludes a real chat).
# ---------------------------------------------------------------------------
def test_automated_prefixes_excluded_real_sessions_survive():
    automated = [
        _sess(f"{p}-123", "2026-08-01T00:00:00Z")
        for p in mc.AUTOMATED_SESSION_PREFIXES
    ]
    real = [
        _sess("session-1754400000-abc", "2026-08-02T00:00:00Z"),
        # 'eval' appears mid-id: anchoring means this real chat SURVIVES.
        _sess("session-eval-999", "2026-08-03T00:00:00Z"),
    ]
    fake = _FakePagedClient(pages=[automated + real])
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    ids = {s.session_id for s in result}
    assert ids == {"session-1754400000-abc", "session-eval-999"}


def test_prefix_constant_covers_required_lanes():
    required = {"smoke", "eval", "eval-run-", "fx-eval", "diag", "tryit",
                "warmup", "briefing", "scheduler", "feedback-explore",
                "validate", "repro", "raw", "adhoc"}
    assert required <= set(mc.AUTOMATED_SESSION_PREFIXES)


# ---------------------------------------------------------------------------
# (b2) CROSS-PINS against the ACTUAL minting sites — behavior through the
#      public ``list_sessions``, not a restatement of the constant. The lanes
#      below authenticate as the SAME Cognito user as the interactive UI, so
#      any id they mint that reaches the picker is a
#      regression. Adding a prefix to ``runtime_app._SYNTHETIC_SESSION_PREFIXES``
#      (or changing a lane's minting format) without covering it in
#      ``AUTOMATED_SESSION_PREFIXES`` fails HERE.
# ---------------------------------------------------------------------------
def _assert_ids_never_reach_picker(minted_ids):
    """Run the minted ids through the real list path next to a genuine chat;
    only the genuine chat may surface."""
    sessions = [_sess(sid, "2026-08-01T00:00:00Z") for sid in minted_ids]
    sessions.append(_sess("session-1754400000-abc", "2026-08-02T00:00:00Z"))
    fake = _FakePagedClient(pages=[sessions])
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert [s.session_id for s in result] == ["session-1754400000-abc"], minted_ids


def test_runtime_synthetic_session_prefixes_never_reach_the_picker():
    # The no-learning-writes tuple (runtime_app) and the picker filter
    # (memory_client) are SEPARATE policies that must stay in sync: every
    # synthetic-run prefix the runtime suppresses learning for is also an
    # automated lane the picker must hide.
    from agents.runtime_app import _SYNTHETIC_SESSION_PREFIXES

    _assert_ids_never_reach_picker(
        [f"{p}job-42-a" for p in _SYNTHETIC_SESSION_PREFIXES])


def test_feedback_exploration_minted_ids_never_reach_the_picker():
    # The EXACT f-string shape feedback_exploration mints:
    #   f"{SESSION_PREFIX}{exploration_id}-r1" / "-r2"
    from agents.feedback_exploration import SESSION_PREFIX

    _assert_ids_never_reach_picker(
        [f"{SESSION_PREFIX}exp-7-r1", f"{SESSION_PREFIX}exp-7-r2"])


def test_eval_run_minted_ids_never_reach_the_picker():
    # The EXACT f-string shape the Evals admin runner mints:
    #   f"eval-run-{run_id}-q{i}" — synthetic traffic
    # (runtime_app._SYNTHETIC_SESSION_PREFIXES) that authenticates as the
    # same Cognito user; it must never surface in the user-facing picker.
    _assert_ids_never_reach_picker(["eval-run-r42-q0", "eval-run-r42-q12"])


def test_smoke_and_eval_namespace_ids_never_reach_the_picker():
    # Ids minted by the REAL helper both scripted lanes use
    # (scripts/smoke_prompts.py, shared by scripts/eval_answers.py) — the
    # same repo-root import mechanism as test_smoke_prompts_sessions.py.
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts.smoke_prompts import create_session_namespace  # noqa: E402

    _assert_ids_never_reach_picker([
        create_session_namespace().session_id(1),            # smoke-<run>-1
        create_session_namespace(prefix="eval").session_id(3),  # eval-<run>-3
    ])


# ---------------------------------------------------------------------------
# (c) Zero-event (TTL-expired) sessions are excluded SERVER-SIDE by the
#     HAS_EVENTS filter on list_sessions — never offered, and never even
#     reach the client as a candidate. A LABEL fetch error is a SEPARATE,
#     best-effort call that must never drop the session (fail open — never
#     hide a real chat because its label lookup failed).
# ---------------------------------------------------------------------------
def test_zero_event_session_excluded_server_side_label_error_kept():
    sessions = [
        _sess("session-live", "2026-08-03T00:00:00Z"),
        _sess("session-expired", "2026-08-02T00:00:00Z"),
        _sess("session-label-err", "2026-08-01T00:00:00Z"),
    ]
    fake = _FakePagedClient(
        pages=[sessions],
        events_by_session={"session-expired": []},
        label_probe_errors={"session-label-err"},
    )
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    ids = [s.session_id for s in result]
    assert "session-expired" not in ids       # TTL-expired: excluded server-side
    assert "session-live" in ids              # live: offered
    assert "session-label-err" in ids         # label error: FAIL OPEN, kept


def test_no_client_side_existence_probe_only_label_calls_bounded_by_page():
    # 200 candidates, every one TTL-expired: list_sessions itself returns NONE
    # of them (server-side filter), so there is nothing to probe and zero
    # list_events calls happen — no client-side existence fan-out exists.
    sessions = [_sess(f"session-{i:03d}", "2026-08-01T00:00:00Z") for i in range(200)]
    fake = _FakePagedClient(pages=[sessions], default_events=[])
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1", max_results=20)
    assert result == []
    assert len(fake.list_events_calls) == 0

    # A page of LIVE candidates only fetches labels for the DISPLAY PAGE
    # (max_results), never an over-fetch multiplier — the old
    # ``_SESSION_PROBE_MULTIPLIER`` existence-probe budget no longer exists.
    assert not hasattr(mc, "_SESSION_PROBE_MULTIPLIER")
    live_sessions = [_sess(f"session-live-{i:03d}", "2026-08-01T00:00:00Z") for i in range(50)]
    fake2 = _FakePagedClient(pages=[live_sessions])
    client2, cfg2 = _make_client(fake2)
    with patch.object(mc, "get_config", return_value=cfg2):
        result2 = client2.list_sessions(user_id="u1", max_results=20)
    assert len(result2) == 20
    assert len(fake2.list_events_calls) == 20
    assert all(c["maxResults"] <= mc._LABEL_SCAN_EVENTS for c in fake2.list_events_calls)


# ---------------------------------------------------------------------------
# (d) The label is the first GENUINE user question — never the ROUTE SEED or
#     Temporal-context preamble — trimmed on a word boundary; when no usable
#     question exists the fallback is a readable date, never the raw id
#     (though the id still travels in the payload).
# ---------------------------------------------------------------------------
_ROUTE_SEED = "[ROUTE SEED - connected-system routing was resolved before delegation] erp"
_TEMPORAL = "[Temporal context: today is 2026-08-06] ignore me"

# The REAL persisted interactive turn shape (runtime_app._graph_first_prelude
# composes seed + "\n\n---\n\n" + server._prepare_question's
# "[Temporal context: ...] {question}"). The question is INSIDE this blob —
# measured live 2026-08-06: no interactive session stores it anywhere else.
_COMPOSED_TURN = (
    "[ROUTE SEED — connected-system routing was resolved before delegation]\n"
    "Do NOT re-run the systems inventory.\nROUTE SEED RESULT:\n{\"systems\": []}"
    "\n\n---\n\n"
    "[Temporal context: Current UTC time: 2026-08-06T18:00:00Z] "
    "Why did Line 3 stop overnight?"
)


def test_label_extracts_question_from_composed_seeded_turn():
    """The seeded turn is the ONLY place the question exists — the label must
    RECOVER it (strip seed at the separator, peel bracketed preambles), never
    skip the whole turn (the old behavior, which left only the date
    fallback), and never surface the seed or preamble text itself."""
    events = [_user_event(_COMPOSED_TURN, ts="2026-08-01T00:00:00Z", eid="e1")]
    fake = _FakePagedClient(
        pages=[[_sess("session-x", "2026-08-01T00:00:00Z")]],
        events_by_session={"session-x": events},
    )
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert result[0].label == "Why did Line 3 stop overnight?"
    assert "ROUTE SEED" not in result[0].label
    assert "Temporal context" not in result[0].label


def test_label_never_surfaces_seed_or_preamble_text():
    """A plain (unseeded) turn with the temporal preamble glued on — the
    server composes f'{time_line} {question}' even without a seed — must
    yield the question, not the preamble."""
    events = [
        _user_event(_TEMPORAL.replace("ignore me", "Why did Line 3 stop overnight?"),
                    ts="2026-08-01T00:00:01Z", eid="e2"),
    ]
    fake = _FakePagedClient(
        pages=[[_sess("session-x", "2026-08-01T00:00:00Z")]],
        events_by_session={"session-x": events},
    )
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert result[0].label == "Why did Line 3 stop overnight?"


def test_label_fallback_is_readable_date_not_raw_id():
    # A seed-only turn (no separator, no question anywhere) -> no genuine
    # question -> readable date fallback.
    events = [_user_event(_ROUTE_SEED)]
    fake = _FakePagedClient(
        pages=[[_sess("session-1754400000-abc", "2026-08-06T09:09:00Z")]],
        events_by_session={"session-1754400000-abc": events},
    )
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert result[0].label == "Chat from Aug 06, 2026 09:09"
    # The session id still travels in the payload for the client.
    assert result[0].session_id == "session-1754400000-abc"


def test_label_trims_on_word_boundary():
    long_q = "Show me the complete maintenance history for every pump on the northern packaging line please"
    events = [_user_event(long_q)]
    fake = _FakePagedClient(
        pages=[[_sess("session-y", "2026-08-01T00:00:00Z")]],
        events_by_session={"session-y": events},
    )
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    label = result[0].label
    assert label.endswith("…")
    assert len(label) <= 81
    # Word-boundary cut: the visible text is whole words from the question.
    assert long_q.startswith(label[:-1].rstrip())
    assert not label[:-1].endswith(" ")


def test_datetime_created_at_from_boto_is_handled():
    # boto3 returns createdAt as datetime objects, not strings.
    dt_new = datetime(2026, 8, 6, 9, 9, tzinfo=timezone.utc)
    dt_old = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    fake = _FakePagedClient(pages=[[
        _sess("session-older", dt_old), _sess("session-newer", dt_new),
    ]])
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert [s.session_id for s in result] == ["session-newer", "session-older"]


# ---------------------------------------------------------------------------
# (e) Missing/garbage createdAt: never crashes, never sorts above real
#     timestamps.
# ---------------------------------------------------------------------------
def test_garbage_created_at_never_crashes_and_sorts_last():
    fake = _FakePagedClient(pages=[[
        _sess("session-garbage", "not-a-timestamp"),
        _sess("session-missing", None),
        _sess("session-real-old", "2026-07-01T00:00:00Z"),
        _sess("session-real-new", "2026-08-06T09:09:00Z"),
    ]])
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")  # must not raise
    ids = [s.session_id for s in result]
    # Real timestamps first (newest-first); garbage/missing at the bottom.
    assert ids[:2] == ["session-real-new", "session-real-old"]
    assert set(ids[2:]) == {"session-garbage", "session-missing"}


# ---------------------------------------------------------------------------
# (f) Pagination is bounded: a client that would page forever terminates, and
#     the collection honors the hard total cap.
# ---------------------------------------------------------------------------
def test_never_ending_pagination_terminates_within_bounds():
    page = [_sess(f"session-{i}", "2026-08-01T00:00:00Z") for i in range(10)]
    fake = _FakePagedClient(pages=[page], never_ending=True)
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")  # must terminate
    assert fake.list_sessions_calls <= mc._SESSION_MAX_PAGES
    assert len(result) <= 20


def test_total_collection_hard_cap():
    # Big pages: the collector must stop once _SESSION_MAX_TOTAL is reached,
    # well before the page-count bound.
    page = [_sess(f"session-{i}", "2026-08-01T00:00:00Z")
            for i in range(mc._SESSION_MAX_TOTAL)]
    fake = _FakePagedClient(pages=[page], never_ending=True)
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        client.list_sessions(user_id="u1")
    assert fake.list_sessions_calls == 1  # cap hit after the first page


# ---------------------------------------------------------------------------
# (g) Event probes within a batch run CONCURRENTLY (bounded pool) — the
#     sequential worst case measured ~60 serial list_events round-trips per
#     GET /api/memory/sessions. Overlap is observed directly (an in-flight
#     counter inside the fake): a serial implementation can NEVER overlap two
#     probes, so max_inflight == 1 fails this deterministically. Order and
#     the probe CALL bound must survive the fan-out.
# ---------------------------------------------------------------------------
def test_event_probes_run_concurrently_and_preserve_order():
    import threading
    import time

    class _OverlapProbe(_FakePagedClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._gate = threading.Lock()
            self._inflight = 0
            self.max_inflight = 0

        def list_events(self, **kwargs):
            with self._gate:
                self._inflight += 1
                self.max_inflight = max(self.max_inflight, self._inflight)
            time.sleep(0.02)  # hold the probe open so parallel probes overlap
            try:
                return super().list_events(**kwargs)
            finally:
                with self._gate:
                    self._inflight -= 1

    # Distinct descending timestamps: the output must keep newest-first order
    # THROUGH the concurrent fan-out (ThreadPoolExecutor.map order guarantee).
    sessions = [_sess(f"session-{i:02d}", f"2026-08-01T00:00:{59 - i:02d}Z")
                for i in range(20)]
    fake = _OverlapProbe(pages=[sessions])
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1", max_results=20)
    assert fake.max_inflight >= 2, "label fetches ran serially — no two ever overlapped"
    assert fake.max_inflight <= mc._PROBE_MAX_WORKERS
    assert [s.session_id for s in result] == [f"session-{i:02d}" for i in range(20)]
    assert len(fake.list_events_calls) == 20  # exactly the display page, no multiplier


# ---------------------------------------------------------------------------
# (g) Rehydration display: GET /api/memory/events strips the ROUTE SEED /
#     bracketed preambles from USER turns so a rehydrated chat shows what the
#     user actually typed — never the seed blob (steward-visible defect:
#     rehydrated history rendered the composed routing payload as the user's
#     message). Display-only; assistant turns and stored memory stay raw.
# ---------------------------------------------------------------------------
def _run_events_route(events):
    import asyncio
    import agents.memory_routes as routes

    class _FakeMemClient:
        def get_events(self, session_id, user_id):
            return events

    class _FakeReq:
        state = types.SimpleNamespace(user=types.SimpleNamespace(user_id="u1"))

    with patch.object(routes, "_client", _FakeMemClient()):
        return asyncio.run(routes.get_events_route(_FakeReq(), session_id="session-x"))


def _mem_event(role, content, ts="2026-08-01T00:00:00Z"):
    return types.SimpleNamespace(role=role, content=content, timestamp=ts)


def test_rehydration_strips_seed_from_user_turns_only():
    # The assistant fixture STARTS with a strippable preamble prefix — an
    # implementation that strips unconditionally (dropping the ``e.role ==
    # "user"`` guard in memory_routes) would peel it, so BYTE-IDENTICAL
    # survival is the pin.
    assistant_text = (
        "[Temporal context: echoed verbatim by the model] Line 3 stopped "
        "because [ROUTE SEED-looking text stays]"
    )
    resp = _run_events_route([
        _mem_event("user", _COMPOSED_TURN),
        _mem_event("assistant", assistant_text),
    ])
    msgs = resp["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Why did Line 3 stop overnight?"
    # Assistant content is NEVER touched by the user-turn stripper — even when
    # it starts with a peelable preamble it survives byte-identical.
    assert msgs[1]["content"] == assistant_text
    assert resp["totalCount"] == 2


def test_rehydration_drops_pure_preamble_user_turn():
    # A seed-only turn (no separator) strips to '' — dropped, not rendered as
    # an empty user bubble. A plain question passes through byte-identical.
    resp = _run_events_route([
        _mem_event("user", _ROUTE_SEED),
        _mem_event("user", "Why did Line 3 stop overnight?"),
    ])
    msgs = resp["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Why did Line 3 stop overnight?"
    assert resp["totalCount"] == 1


# ---------------------------------------------------------------------------
# (h) The preamble peel (agents.memory_labels.strip_turn_preambles) is
#     ANCHORED to the exact server-composed shapes and BOUNDED — bracketed
#     material belonging to the question itself is never eaten, and a turn
#     that merely LOOKS like a preamble is never dropped.
# ---------------------------------------------------------------------------
def test_plant_name_with_bracket_never_truncates_peel():
    """Composed via the REAL server._prepare_question with an unvalidated
    plant name containing ']' — the peel must anchor on the plant bracket's
    FIXED closing sentence and recover the question exactly (a naive
    first-']' peel cuts inside the plant name and leaks the rest of the
    bracket into the displayed question)."""
    from agents.memory_labels import strip_turn_preambles
    from agents.models import AskRequest
    from agents.server import _prepare_question

    composed = str(_prepare_question(
        AskRequest(question="Why did Line 3 stop overnight?", plant="Plant [7] North")))
    assert strip_turn_preambles(composed) == "Why did Line 3 stop overnight?"


def test_unterminated_bracket_is_kept_as_genuine_user_text():
    # The server never composes an unterminated preamble — a user turn that
    # merely STARTS like one survives byte-identical instead of being dropped
    # from rehydration.
    from agents.memory_labels import strip_turn_preambles

    q = "[Temporal context: I typed this myself and never closed the bracket"
    assert strip_turn_preambles(q) == q


def test_peel_is_bounded_to_the_server_composed_shape():
    # At most TWO bracketed preambles are ever server-composed (temporal,
    # then plant) — a third bracketed chunk belongs to the QUESTION and must
    # survive; an unbounded peel would eat it.
    from agents.memory_labels import strip_turn_preambles

    composed = (
        "[Temporal context: Current UTC time: 2026-08-06T18:00:00Z] "
        "[Plant context: p1 — scope Neptune queries to systems at this plant. "
        "If data isn't found here, mention that and ask if the user wants to "
        "search other plants.] "
        "[Temporal context: quoted by the user] What does this preamble mean?"
    )
    assert strip_turn_preambles(composed) == (
        "[Temporal context: quoted by the user] What does this preamble mean?")


# ---------------------------------------------------------------------------
# (i) The declared boto3 floor guarantees the ListSessions ``filter`` API the
#     server-side HAS_EVENTS filter depends on.
# ---------------------------------------------------------------------------
def test_boto3_floor_guarantees_list_sessions_filter_api():
    """boto3 1.42.0 permits botocore <1.42 (measured: botocore 1.41.6's
    bedrock-agentcore model has NO ListSessions ``filter`` member), on which
    EVERY list_sessions call raises ParamValidationError → wrapped
    MemoryUnavailableError → GET /api/memory/sessions 503s permanently.
    boto3 1.43.0 is the first floor whose botocore requirement (>=1.43.0)
    ships the member — the declared floor must guarantee the API this client
    code requires."""
    import re
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    m = re.search(r'"boto3==([0-9][0-9.]*)"', pyproject.read_text())
    assert m, "boto3 pin not found in pyproject.toml"
    floor = tuple(int(p) for p in m.group(1).split("."))
    assert floor >= (1, 43, 0), f"boto3 floor {m.group(1)} < 1.43.0"


# ---------------------------------------------------------------------------
# (h) BLOB events: AgentCore stores oversized turns (measured: the composed
#     route-seed user turn, >9KB) as {"blob": <json>} instead of
#     {"conversational": ...}. These previously parsed as BLANK events — the
#     session looked like its user turn was never persisted, labels fell back
#     to dates, and rehydration dropped the turn. The parser must decode the
#     blob's double-wrapped envelope: json string -> list -> json string ->
#     {"message": {"role", "content"}}.
# ---------------------------------------------------------------------------
def _blob_event(role, text, ts="2026-08-01T00:00:00Z", eid="bl"):
    envelope = json.dumps({"message": {"role": role, "content": [{"text": text}]},
                           "message_id": 0, "redact_message": None})
    return {"eventId": eid, "createdAt": ts,
            "payload": [{"blob": json.dumps([envelope])}]}


def test_blob_user_turn_is_parsed_and_labels_recover_its_question():
    events = [_blob_event("user", _COMPOSED_TURN)]
    fake = _FakePagedClient(
        pages=[[_sess("session-x", "2026-08-01T00:00:00Z")]],
        events_by_session={"session-x": events},
    )
    client, cfg = _make_client(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert result[0].label == "Why did Line 3 stop overnight?"


def test_blob_turn_appears_in_rehydration_with_seed_stripped():
    parsed = mc._parse_conversational_event(_blob_event("user", _COMPOSED_TURN))
    assert parsed.role == "user"
    resp = _run_events_route([parsed])
    assert resp["messages"][0]["content"] == "Why did Line 3 stop overnight?"


def test_undecodable_blob_degrades_to_blank_never_raises():
    for bad in (None, "", "not json", json.dumps(["not an envelope"]),
                json.dumps([json.dumps({"no_message": 1})])):
        parsed = mc._parse_conversational_event(
            {"eventId": "x", "createdAt": "t", "payload": [{"blob": bad}]})
        assert parsed.content == ""  # degrades exactly as the old blank event
