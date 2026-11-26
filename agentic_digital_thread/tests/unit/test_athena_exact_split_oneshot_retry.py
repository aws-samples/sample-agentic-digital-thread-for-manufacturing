# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A plain query whose poll stalls is run once more, like a piece.

A live check saw a plain query's poll stay QUEUED for its whole 60 s window
although the engine ran it in about 4 s. ``run_athena_query_exact`` now runs
such a query once more as a fresh query (the poll already stopped the first);
a second timeout is returned and says it was retried. The piece poll window
stays 60 s; there is no rule on how long a query may sit QUEUED (cold
on-demand queueing was measured at 60 to 135 s).

Fully offline (the file-level ``FakeLake``); synthetic names only.
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at

from . import test_athena_exact_split as base
from .test_athena_exact_split_retry import _FAST, _exact, _lake


@pytest.fixture(autouse=True)
def _quick_polls(monkeypatch):
    monkeypatch.setattr(at, "_DEFAULT_POLL_INTERVAL", 0.01)


def test_a_plain_query_whose_poll_stalls_is_retried_once():
    lake = _lake(cutoff=10 ** 12)
    lake.stall_first_plain = 1
    result = _exact(lake, base._P1, _FAST)
    assert result["rows"] == lake.reference(base._P1) and "exact_split" not in result
    assert lake.data_queries() == [base._P1 + " LIMIT 500"] * 2
    assert len(lake.stopped) == 1


def test_a_plain_query_that_stalls_twice_returns_the_timeout():
    lake = _lake(cutoff=10 ** 12)
    lake.stall_first_plain = 2
    with pytest.raises(at.AthenaQueryTimeoutError) as info:
        _exact(lake, base._P1, _FAST)
    assert info.value.to_dict()["retried_once"] is True
    assert len(lake.data_queries()) == 2


def test_a_plain_query_retried_after_a_stall_still_gets_the_exact_split():
    lake = _lake(cutoff=base._CUTOFF)
    lake.stall_first_plain = 1
    result = _exact(lake, base._P1, _FAST)
    assert result["rows"] == lake.reference(base._P1)
    assert result["exact_split"]["pieces"] > 1


# --------------------------------------------------------------------------- #
# Inside one model tool call (the system agent's question budget)
# --------------------------------------------------------------------------- #
# Measured in an answer eval: a plain query sat 145 s in the engine, its 150 s
# poll window ran out, and the retry ran past a 60 s question. Inside a call
# deadline the poll window is capped by it and the retry is skipped when it
# would not fit; the stopped attempt of a retry is engine waiting, recorded so
# the question's budget can leave it out.
def test_the_stopped_attempt_of_a_retry_is_recorded_as_engine_waiting():
    from agents import athena_call_time as act

    lake = _lake(cutoff=10 ** 12)
    lake.stall_first_plain = 1
    started = act.now()
    with act.recording() as waited:
        result = _exact(lake, base._P1, _FAST)
    took = act.now() - started
    assert result["rows"] == lake.reference(base._P1)
    assert 0.45 <= waited.seconds() <= took


def test_the_retry_is_skipped_when_it_would_not_fit_the_call_deadline():
    from agents import athena_call_time as act

    lake = _lake(cutoff=10 ** 12)
    lake.stall_first_plain = 1
    with act.recording() as waited, act.call_deadline(act.now() + 1.0):
        with pytest.raises(at.AthenaQueryTimeoutError) as info:
            _exact(lake, base._P1, _FAST)
    assert "retried_once" not in info.value.to_dict()
    assert len(lake.data_queries()) == 1
    # Stopped while still QUEUED after its 0.5 s window, not retried: the
    # span the polls saw it queued is recorded, and nothing more.
    assert 0.3 < waited.seconds() <= 0.6


def test_the_poll_window_is_capped_by_the_call_deadline():
    import time

    from agents import athena_call_time as act

    lake = _lake(cutoff=10 ** 12)
    lake.stall_first_plain = 2
    started = time.monotonic()
    with act.call_deadline(act.now() + 1.2):
        with pytest.raises(at.AthenaQueryTimeoutError) as info:
            _exact(lake, base._P1, {"poll_timeout_seconds": 5})
    assert info.value.poll_timeout <= 1.2
    assert len(lake.data_queries()) == 1
    assert time.monotonic() - started < 3.0


def test_outside_a_call_deadline_the_poll_window_and_retry_are_unchanged():
    from agents import athena_call_time as act

    assert act.current_deadline() is None
    assert act.capped_poll_window(150.0) == 150.0
    assert act.retry_fits() is True


# A retried attempt's poll window is never credited as
# such. A query stopped while RUNNING was doing the engine's work; only what
# Athena reports as waiting (queue, service pre-processing) or what the polls
# saw QUEUED counts, and the fake reports neither here.
def test_a_running_plain_query_stopped_and_retried_credits_nothing():
    from agents import athena_call_time as act

    lake = _lake(cutoff=10 ** 12)
    lake.stall_first_plain, lake.stall_bytes = 1, 10
    with act.recording() as waited:
        result = _exact(lake, base._P1, _FAST)
    assert result["rows"] == lake.reference(base._P1)
    assert len(lake.data_queries()) == 2 and len(lake.stopped) == 1
    assert waited.seconds() == 0.0


def test_a_running_piece_stopped_and_retried_credits_nothing():
    from agents import athena_call_time as act

    lake = _lake(cutoff=base._CUTOFF)
    lake.stall_first_pieces, lake.stall_bytes = 1, 10
    with act.recording() as waited:
        result = _exact(lake, base._P1, _FAST)
    assert result["rows"] == lake.reference(base._P1)
    assert result["exact_split"]["retried_pieces"] == 1
    assert waited.seconds() == 0.0
