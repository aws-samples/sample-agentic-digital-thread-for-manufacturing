# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Orchestrator admission is OWNERSHIP, not a process-global boolean.

``server._query_in_progress`` was one shared ``bool`` written by three unrelated
owners — every /api/ask drain, the gateway warmup ping, and (by read) the
scheduler and the briefing guard. Three concrete defects fell out of that:

* **Clobbering.** Two overlapping user turns both wrote ``True``; the FIRST to
  finish wrote ``False`` while the second was still streaming, so a briefing was
  admitted on top of a live turn. The warmup ping did the same to a live turn.
* **Check-then-body-start race.** ``ask`` read the flag and returned a
  ``StreamingResponse``, but the flag was only SET later, by the generator body,
  which Starlette starts after the handler has returned. Two briefings arriving
  in that window both passed the guard.
* **No exact release.** A release was ``= False`` — anyone's flag, not the
  caller's own.

The replacement is a lock-protected coordinator issuing idempotent ownership
tokens. Interactive turns keep their existing allowed CONCURRENCY (they use fresh
runtimeSessionIds); the background workloads — briefing and the warmup ping — are
the serialized ones, and they claim atomically or not at all.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from concurrent.futures import ThreadPoolExecutor

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.server as server  # noqa: E402
from agents.events import MessageEvent  # noqa: E402
from agents.models import AskRequest  # noqa: E402
from agents.query_admission import QueryAdmission, get_admission  # noqa: E402


class TestOverlappingOwners:
    def test_releasing_one_owner_leaves_the_other_busy(self):
        """The clobber repro: A and B overlap; A's release must not free B."""
        admission = QueryAdmission()
        a = admission.acquire("query-a")
        b = admission.acquire("query-b")
        assert admission.busy is True

        admission.release(a)

        assert admission.busy is True, "A's release freed B's admission"
        admission.release(b)
        assert admission.busy is False

    def test_tokens_are_distinct_and_release_is_exact(self):
        admission = QueryAdmission()
        a = admission.acquire("query-a")
        b = admission.acquire("query-b")
        assert a != b
        admission.release(b)
        assert admission.owners() == ("query-a",)

    def test_release_is_idempotent(self):
        admission = QueryAdmission()
        a = admission.acquire("query-a")
        b = admission.acquire("query-b")
        admission.release(a)
        admission.release(a)
        admission.release(a)
        assert admission.owners() == ("query-b",)

    def test_release_of_none_is_a_no_op(self):
        admission = QueryAdmission()
        held = admission.acquire("query")
        admission.release(None)
        assert admission.busy is True
        admission.release(held)
        assert admission.busy is False


class TestBriefingAdmissionIsAtomic:
    def test_simultaneous_briefings_admit_exactly_one(self):
        admission = QueryAdmission()
        barrier = threading.Barrier(16)

        def _claim():
            barrier.wait()
            return admission.acquire_if_idle("briefing")

        with ThreadPoolExecutor(max_workers=16) as pool:
            tokens = [f.result() for f in [pool.submit(_claim) for _ in range(16)]]

        granted = [t for t in tokens if t is not None]
        assert len(granted) == 1, f"{len(granted)} briefings were admitted"
        assert admission.owners() == ("briefing",)

    def test_acquire_if_idle_refuses_while_any_owner_holds(self):
        admission = QueryAdmission()
        held = admission.acquire("query")
        assert admission.acquire_if_idle("briefing") is None
        admission.release(held)
        assert admission.acquire_if_idle("briefing") is not None

    def test_unconditional_acquire_still_admits_over_a_briefing(self):
        """Interactive turns keep their existing allowed concurrency."""
        admission = QueryAdmission()
        briefing = admission.acquire_if_idle("briefing")
        assert briefing is not None
        user = admission.acquire("query")
        assert user is not None
        admission.release(briefing)
        assert admission.owners() == ("query",)


class TestWarmupOverlap:
    def test_warmup_release_does_not_free_a_live_query(self):
        admission = QueryAdmission()
        query = admission.acquire("query")
        warmup = admission.acquire("warmup")

        admission.release(warmup)

        assert admission.busy is True, "warmup's release cleared the live query"
        assert admission.owners() == ("query",)

    def test_warmup_does_not_claim_while_a_query_is_live(self):
        admission = QueryAdmission()
        query = admission.acquire("query")
        assert admission.acquire_if_idle("warmup") is None
        admission.release(query)


class TestWorkerPublicationRace:
    def test_worker_attached_after_owner_cancel_is_stopped_immediately(self):
        admission = get_admission()
        admission.reset()
        owner = server._StreamOwner(admission.acquire("query"))
        owner.cancel()
        worker = server._StreamWorker(maxsize=2)

        owner.attach(worker)

        assert worker.stop.is_set() is True
        assert worker.cancel.cancelled is True
        assert admission.busy is False


class _FakeRequestState:
    user = None
    session = None


class _FakeRequest:
    def __init__(self):
        self.state = _FakeRequestState()


class _StubClient:
    def __init__(self):
        self.streams = 0

    def stream(self, **kwargs):
        self.streams += 1
        yield MessageEvent(content="ok")


def _ask(question: str, session_id: str, client):
    body = AskRequest(question=question, session_id=session_id)
    with patch.object(server, "get_chat_client", lambda: client):
        return asyncio.run(server.ask(body, _FakeRequest()))


async def _first_chunk(response) -> str:
    async for chunk in response.body_iterator:
        return chunk if isinstance(chunk, str) else chunk.decode()
    return ""


class TestAskAdmitsBeforeItReturns:
    """No check-then-body-start window: the claim happens in the HANDLER."""

    def test_handler_error_before_response_release_does_not_leak(self):
        body = AskRequest(question="why?", session_id="s-error")
        with patch.object(server, "get_chat_client",
                          side_effect=RuntimeError("client unavailable")):
            with pytest.raises(RuntimeError, match="client unavailable"):
                asyncio.run(server.ask(body, _FakeRequest()))
        assert self._admission.busy is False

    def setup_method(self):
        self._admission = get_admission()
        self._admission.reset()

    def teardown_method(self):
        self._admission.reset()

    def test_briefing_claims_before_the_response_is_returned(self):
        _ask("morning briefing", "briefing-a", _StubClient())
        assert self._admission.busy is True, \
            "the briefing had not claimed admission when ask returned"
        assert self._admission.owners() == ("briefing",)

    def test_second_simultaneous_briefing_is_skipped_without_streaming(self):
        _ask("morning briefing", "briefing-a", _StubClient())
        forbidden = _StubClient()
        second = _ask("morning briefing", "briefing-b", forbidden)
        chunk = asyncio.run(_first_chunk(second))
        assert "[Briefing skipped" in chunk
        assert forbidden.streams == 0

    def test_user_turn_claims_and_never_leaks(self):
        client = _StubClient()
        response = _ask("why did lot 9 fail?", "s-user", client)
        assert self._admission.busy is True
        asyncio.run(_drain_all(response))
        assert self._admission.busy is False

    def test_two_user_turns_overlap_and_each_releases_its_own(self):
        a = _ask("first?", "s-a", _StubClient())
        b = _ask("second?", "s-b", _StubClient())
        assert len(self._admission.owners()) == 2

        asyncio.run(_drain_all(a))
        assert self._admission.busy is True, "A's teardown released B"

        asyncio.run(_drain_all(b))
        assert self._admission.busy is False


async def _drain_all(response) -> None:
    async for _ in response.body_iterator:
        pass


class _Clock:
    def __init__(self):
        from datetime import datetime, timezone
        self.monotonic_value = 100.0
        self.utc_value = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    def monotonic(self):
        return self.monotonic_value

    def utc_now(self):
        return self.utc_value


class TestAdmissionTelemetry:
    def test_idle_snapshot_omits_unmeasured_counters_and_release_time(self):
        clock = _Clock()
        admission = QueryAdmission(monotonic=clock.monotonic, utc_now=clock.utc_now)

        snapshot = admission.snapshot()

        assert snapshot == {
            "state": "idle",
            "tokens_outstanding": 0,
            "leak_signal": False,
            "leak_threshold_seconds": 3600,
        }
        assert "briefings_admitted" not in snapshot
        assert "briefings_skipped_busy" not in snapshot
        assert "last_release_at" not in snapshot

    def test_busy_idle_transitions_and_counters_follow_real_events_only(self):
        clock = _Clock()
        admission = QueryAdmission(monotonic=clock.monotonic, utc_now=clock.utc_now)
        token = admission.acquire_if_idle("briefing")
        busy = admission.snapshot()

        assert token is not None
        assert busy["state"] == "busy"
        assert busy["current_owner_kind"] == "briefing"
        assert busy["tokens_outstanding"] == 1
        assert busy["briefings_admitted"] == 1
        assert "briefings_skipped_busy" not in busy

        assert admission.acquire_if_idle("briefing") is None
        assert admission.snapshot()["briefings_skipped_busy"] == 1
        admission.release(token)
        idle = admission.snapshot()
        assert idle["state"] == "idle"
        assert idle["tokens_outstanding"] == 0
        assert idle["last_release_at"] == "2026-08-25T12:00:00+00:00"

    def test_only_real_briefing_attempts_increment_briefing_counters(self):
        clock = _Clock()
        admission = QueryAdmission(monotonic=clock.monotonic, utc_now=clock.utc_now)
        query = admission.acquire("query")
        assert admission.acquire_if_idle("warmup") is None
        snapshot = admission.snapshot()
        assert "briefings_admitted" not in snapshot
        assert "briefings_skipped_busy" not in snapshot
        admission.release(query)
        first_release_at = admission.snapshot()["last_release_at"]
        from datetime import timedelta
        clock.utc_value += timedelta(hours=5)
        admission.release(query)
        assert first_release_at == "2026-08-25T12:00:00+00:00"
        assert admission.snapshot()["last_release_at"] == first_release_at

    def test_old_token_raises_computed_leak_signal_without_releasing_it(self):
        clock = _Clock()
        admission = QueryAdmission(monotonic=clock.monotonic, utc_now=clock.utc_now)
        token = admission.acquire("query")
        clock.monotonic_value += 3599
        assert admission.snapshot()["leak_signal"] is False
        clock.monotonic_value += 2

        leaked = admission.snapshot()

        assert leaked["leak_signal"] is True
        assert leaked["oldest_token_age_seconds"] == 3601.0
        assert admission.owners() == ("query",)
        admission.release(token)
