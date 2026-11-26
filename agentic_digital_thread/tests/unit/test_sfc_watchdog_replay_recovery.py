# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SFC watchdog and the story server together recover a stuck history replay.

The story server replays its history window once; every pass with a gap
starts over, and after ``REPLAY_MAX_PASSES`` it STOPS the replay. A stopped
replay is revived once, for a reader that arrives after it stopped. The
watchdog's first restart for an overdue replay (after two clean passes could
have finished) only adds an ordinary pass; its one later restart, after every
pass must have failed, is what revives the replay.

The watchdog handler runs every three minutes against fake AWS clients whose
SFC task starts a minute after each successful ``UpdateService``. Every new
SFC task is a new reader, and those reader times then drive the server's own
replay state machine (``clock.mjs``, evaluated by node over the story mill's
dataset), with one value never delivered on any pass. The watchdog does not
read the server's state, so running the two in sequence is exact.
"""
from __future__ import annotations

import json

import pytest

from tests.unit import _sfc_watchdog_modules
from tests.unit import test_opcua_history_replay as replay
from tests.unit.test_sfc_watchdog_handler import (
    ANCHOR_PARAMETER, BACKFILL, NOW as FAKE_NOW, STATE, FakeEcs, FakeSitewise, FakeSsm, _replay_config,
)

handler = _sfc_watchdog_modules.handler()
pytestmark = replay.pytestmark

ID = replay.ANCHOR + 50                       # the window's first publication (the simulator's t0)
WINDOW = f"{replay.ANCHOR};{replay.FROM};{replay.TO};{ID}"
RUN_EVERY = 180
SFC_START_DELAY = 60                          # a new SFC task (a new reader) a minute after the update
HORIZON = 22_000


def _ago(epoch: float) -> float:
    """The fakes' ages are relative to their own fixed NOW."""
    return FAKE_NOW - epoch


def _watch(*, fail_first_update: bool) -> tuple[list, list]:
    """Run the watchdog over the horizon: (actions with times, SFC task starts)."""
    started = ID - 7200
    ecs = FakeEcs(server_ages={f"server-{i}": _ago(started) for i in range(4)},
                  sfc_task_age=_ago(started), sfc_deployed=_ago(started),
                  fail_update="ThrottlingException" if fail_first_update else None)
    ssm = FakeSsm({STATE: "[]", ANCHOR_PARAMETER: str(replay.ANCHOR), BACKFILL: WINDOW})
    sitewise = FakeSitewise({f"asset-{i}": 0 for i in range(4)})
    clients = handler.Clients(sitewise=sitewise, ecs=ecs, ssm=ssm)
    actions, readers = [], []
    for k in range(1, HORIZON // RUN_EVERY):
        now = ID + k * RUN_EVERY
        sitewise.ages = {f"asset-{i}": _ago(now - 5) for i in range(4)}
        before = len(ecs.updates)
        record = handler.run(_replay_config(), clients, float(now), log=lambda _line: None)
        if record["action"] != "none":
            actions.append((now - ID, record["action"], record.get("replay")))
        if record["action"] == "restart_failed":
            ecs.fail_update = None
        if len(ecs.updates) > before:
            readers.append(now - ID + SFC_START_DELAY)
            ecs.sfc_task_age, ecs.sfc_deployed = _ago(now + SFC_START_DELAY), _ago(now)
    return actions, readers


def _server(readers: list) -> dict:
    """The story server's replay over the horizon, with SFC's first reader
    from the start and a new reader at each SFC task start; the alarm's
    leading edge never reaches the reader on any pass."""
    edge = replay._alarm_leading_edge()
    events = {t: [f"subscribe:g{n}"] for n, t in enumerate(readers, start=2)}
    return replay._simulate(events, until=HORIZON, withhold={"2": [edge]}, withhold_all=True)


@pytest.mark.parametrize("fail_first_update", [False, True], ids=["updates-succeed", "first-update-fails"])
def test_the_early_restart_is_a_pass_and_the_later_one_revives_the_stopped_replay(fail_first_update):
    actions, readers = _watch(fail_first_update=fail_first_update)
    expected = [("restart", WINDOW)] * 2
    if fail_first_update:
        expected = [("restart_failed", WINDOW)] + expected
    assert [(a, key) for _t, a, key in actions] == expected
    assert len(readers) == 2
    early, revival = readers
    server = _server(readers)
    assert server["stoppedAt"] is not None and early < server["stoppedAt"] < revival, (server, readers)
    assert server["revivedAt"] == revival, "the later reader, not the early one, revives the replay"
    assert server["revived"] is True and server["done"] is False


def test_without_the_later_restart_the_stopped_replay_is_never_revived():
    """Control: the early reader alone leaves the replay stopped for good."""
    _actions, readers = _watch(fail_first_update=False)
    server = _server(readers[:1])
    assert server["stopped"] is True and server["revived"] is False and server["revivedAt"] is None


def test_the_watchdog_bounds_match_the_server():
    """The two-pass due precedes the server's stop and the stopped bound
    follows it, for a single reader whose every pass fails."""
    clock = _sfc_watchdog_modules.load("watchdog_clock")
    view = clock.replay_view(WINDOW, ID, since=ID)
    server = _server([])
    assert view.due - ID < server["stoppedAt"] <= view.stopped_by - ID, (view, server["stoppedAt"])
    assert json.dumps(server["stopped"]) == "true"
