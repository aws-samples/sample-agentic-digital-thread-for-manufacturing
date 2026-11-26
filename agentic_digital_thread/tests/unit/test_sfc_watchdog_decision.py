# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SFC freshness watchdog's restart decision (``lambda-sfc-watchdog/watchdog_decision.py``).

SFC can keep a source "connected" while its subscription delivers nothing, so
one source's SiteWise values stop while the others continue and SFC's own
health check stays green. Only an SFC restart clears it. The watchdog reads
one sentinel per source every few minutes and decides here, with no AWS call,
whether to force a new SFC deployment. These cases pin when it must restart
and every guard that stops it from restarting when it should not.
"""
from __future__ import annotations

import pytest

from tests.unit._sfc_watchdog_modules import load

decision = load("watchdog_decision")

NOW = 1_790_000_000.0
MIN = 60.0
LIMITS = decision.Limits()


def _source(name: str, *, age: float | None = 30.0, server_age: float = 3600.0, **kw):
    """A source whose sentinel was stored ``age`` s ago (None: never) and whose
    OPC UA server task has run ``server_age`` s."""
    value_time = None if age is None else NOW - age
    return decision.SourceState(source=name, value_time=value_time,
                                server_since=NOW - server_age, **kw)


def _sfc(*, deployed: float = 6 * 3600.0, task_age: float = 6 * 3600.0, **kw):
    """SFC with its newest deployment ``deployed`` s old and its task ``task_age`` s old."""
    defaults = dict(newest_deployment=NOW - deployed, task_started=NOW - task_age,
                    running=1, deployment_in_progress=False)
    defaults.update(kw)
    return decision.SfcState(**defaults)


def _healthy(*stale):
    names = ("site-a", "site-b", "site-c", "site-d")
    return [_source(n, age=(20 * MIN if n in stale else 30.0)) for n in names]


def _by_source(result):
    return {v.source: v for v in result.sources}


def test_the_defaults_are_the_agreed_thresholds():
    assert (LIMITS.stale_seconds, LIMITS.server_min_age_seconds, LIMITS.sfc_min_task_age_seconds,
            LIMITS.deployment_cooldown_seconds, LIMITS.max_restarts_per_hour) == (300, 300, 300, 900, 2)


def test_every_source_fresh_means_no_restart():
    result = decision.decide(NOW, _healthy(), _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is False
    assert {v.decision for v in result.sources} == {"fresh"}


def test_one_source_stale_after_its_server_was_replaced_restarts_sfc():
    """The server task was replaced 20 minutes ago (outside a deploy); SFC's new
    subscription for that source never delivered."""
    sources = _healthy()
    sources[1] = _source("site-b", age=19 * MIN, server_age=20 * MIN)
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is True
    verdicts = _by_source(result)
    assert verdicts["site-b"].decision == "stale"
    assert verdicts["site-b"].age == pytest.approx(19 * MIN)
    assert "site-b" in result.reason
    assert all(verdicts[n].decision == "fresh" for n in ("site-a", "site-c", "site-d"))


def test_a_source_never_fresh_after_an_sfc_restart_restarts_again_once_the_cooldown_passed():
    """The watchdog restarted SFC 20 min ago and the source still has nothing
    newer than before that restart: one more restart is allowed."""
    sources = _healthy()
    sources[2] = _source("site-c", age=45 * MIN)
    result = decision.decide(NOW, sources, _sfc(deployed=20 * MIN, task_age=19 * MIN),
                             restarts=[NOW - 20 * MIN], limits=LIMITS)
    assert result.restart is True
    assert _by_source(result)["site-c"].decision == "stale"


def test_a_source_with_no_value_ever_stored_is_stale():
    sources = _healthy()
    sources[0] = _source("site-a", age=None)
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is True
    verdict = _by_source(result)["site-a"]
    assert verdict.decision == "stale" and verdict.age is None
    assert "no value" in verdict.reason


def test_every_source_stale_while_sfc_is_deploying_does_not_restart():
    sources = _healthy("site-a", "site-b", "site-c", "site-d")
    result = decision.decide(NOW, sources, _sfc(deployment_in_progress=True), restarts=[], limits=LIMITS)
    assert result.restart is False
    assert "in progress" in result.reason
    assert {v.decision for v in result.sources} == {"stale"}


def test_every_source_stale_while_sfc_has_no_running_task_does_not_restart():
    sources = _healthy("site-a", "site-b", "site-c", "site-d")
    result = decision.decide(NOW, sources, _sfc(running=0, task_started=None), restarts=[], limits=LIMITS)
    assert result.restart is False
    assert "no SFC task" in result.reason


def test_every_source_stale_just_after_a_restart_respects_the_cooldown():
    sources = _healthy("site-a", "site-b", "site-c", "site-d")
    result = decision.decide(NOW, sources, _sfc(deployed=10 * MIN, task_age=8 * MIN),
                             restarts=[NOW - 10 * MIN], limits=LIMITS)
    assert result.restart is False
    assert "cooldown" in result.reason


def test_an_sfc_task_that_ecs_just_replaced_gets_its_start_up_time():
    """ECS replaced the SFC task outside a deployment (it exited); the JVM needs a few minutes."""
    sources = _healthy("site-a", "site-b", "site-c", "site-d")
    result = decision.decide(NOW, sources, _sfc(task_age=2 * MIN), restarts=[], limits=LIMITS)
    assert result.restart is False
    assert "SFC task started" in result.reason


def test_no_more_than_two_restarts_an_hour():
    sources = _healthy("site-b")
    sfc = _sfc(deployed=16 * MIN, task_age=15 * MIN)
    capped = decision.decide(NOW, sources, sfc, restarts=[NOW - 50 * MIN, NOW - 16 * MIN], limits=LIMITS)
    assert capped.restart is False
    assert "2 restarts in the last hour" in capped.reason
    # A restart older than an hour no longer counts.
    allowed = decision.decide(NOW, sources, sfc, restarts=[NOW - 61 * MIN, NOW - 16 * MIN], limits=LIMITS)
    assert allowed.restart is True


def test_recent_restarts_keeps_only_the_last_hour():
    kept = decision.recent_restarts([NOW - 3601, NOW - 3599, NOW - 10, "bad", None], NOW)
    assert kept == [NOW - 3599, NOW - 10]


def test_an_unreadable_restart_record_takes_no_action():
    """Without the record the hourly cap cannot be checked, so nothing restarts."""
    result = decision.decide(NOW, _healthy("site-b"), _sfc(), restarts=None, limits=LIMITS)
    assert result.restart is False
    assert "restart record" in result.reason


def test_a_young_server_task_is_not_judged_yet():
    sources = _healthy()
    sources[3] = _source("site-d", age=30 * MIN, server_age=4 * MIN)
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is False
    verdict = _by_source(result)["site-d"]
    assert verdict.decision == "skip"
    assert "server" in verdict.reason and "started" in verdict.reason


def test_a_source_whose_server_has_no_running_task_is_skipped():
    sources = _healthy()
    sources[0] = decision.SourceState(source="site-a", value_time=NOW - 30 * MIN, server_since=None)
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is False
    assert _by_source(result)["site-a"].decision == "skip"


def test_a_server_waiting_for_its_first_story_placement_is_ignored():
    """The story site's server publishes no value until the first placement,
    so its sentinel is old by design."""
    sources = _healthy()
    sources[3] = _source("site-d", age=None, publishing=False,
                         publishing_reason="the server publishes no value until the first placement")
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is False
    verdict = _by_source(result)["site-d"]
    assert verdict.decision == "skip"
    assert "first placement" in verdict.reason


def test_a_server_that_just_started_publishing_gets_the_server_grace():
    """The first placement happened 2 min ago: values are only now expected."""
    sources = _healthy()
    sources[3] = _source("site-d", age=None, server_age=2 * MIN)
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is False
    assert _by_source(result)["site-d"].decision == "skip"


def test_a_sitewise_read_failure_takes_no_action_and_is_logged():
    sources = _healthy("site-a")
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS,
                             sitewise_error="ThrottlingException")
    assert result.restart is False
    assert "SiteWise" in result.reason and "ThrottlingException" in result.reason


def test_a_per_source_read_error_is_never_counted_as_stale():
    sources = _healthy()
    sources[1] = _source("site-b", age=None, read_error="ResourceNotFoundException")
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is False
    verdict = _by_source(result)["site-b"]
    assert verdict.decision == "error"
    assert "ResourceNotFoundException" in verdict.reason


def test_a_read_error_on_one_source_does_not_hide_another_stale_source():
    sources = _healthy("site-c")
    sources[1] = _source("site-b", age=None, read_error="AccessDeniedException")
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert result.restart is True
    assert "site-c" in result.reason and "site-b" not in result.reason


def test_an_unreadable_sfc_service_takes_no_action():
    result = decision.decide(NOW, _healthy("site-a"), decision.SfcState(error="AccessDeniedException"),
                             restarts=[], limits=LIMITS)
    assert result.restart is False
    assert "AccessDeniedException" in result.reason


def test_a_value_exactly_at_the_threshold_is_still_fresh():
    sources = _healthy()
    sources[0] = _source("site-a", age=LIMITS.stale_seconds)
    assert decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS).restart is False


def test_a_value_stamped_slightly_in_the_future_is_fresh():
    sources = _healthy()
    sources[0] = _source("site-a", age=-5.0)
    result = decision.decide(NOW, sources, _sfc(), restarts=[], limits=LIMITS)
    assert _by_source(result)["site-a"].decision == "fresh"


def test_the_summary_is_one_json_ready_record():
    sources = _healthy("site-b")
    result = decision.decide(NOW, sources, _sfc(), restarts=[NOW - 30 * MIN], limits=LIMITS)
    record = result.as_record()
    assert record["action"] == "restart"
    assert record["restarts_last_hour"] == 1
    assert [s["source"] for s in record["sources"]] == ["site-a", "site-b", "site-c", "site-d"]
    assert set(record["sources"][0]) == {"source", "age_seconds", "decision", "reason"}
    assert record["sources"][1]["age_seconds"] == 1200


# --- the watchdog's own last restart is a cooldown too (ECS state can lag)


def test_the_newest_restart_in_the_record_is_also_a_cooldown():
    """A duplicate run that still sees the old SFC deployment (ECS has not
    shown the new one yet) finds its sibling's reservation and stays put."""
    result = decision.decide(NOW, _healthy("site-b"), _sfc(), restarts=[NOW - 2 * MIN], limits=LIMITS)
    assert result.restart is False
    assert "the watchdog restarted SFC 120 s ago" in result.reason
    later = decision.decide(NOW, _healthy("site-b"), _sfc(), restarts=[NOW - 16 * MIN], limits=LIMITS)
    assert later.restart is True


# --- the one-time history replay (a fifth SFC source with no live sentinel)


def _replay(status: str, **kw):
    return decision.ReplayState(source="site-d-history", status=status, key="k1", **kw)


def test_an_overdue_history_replay_restarts_sfc_and_names_the_replay():
    result = decision.decide(NOW, _healthy(), _sfc(), restarts=[], limits=LIMITS,
                             replay=_replay("overdue", reason="not done 900 s after it was due"))
    assert result.restart is True
    assert result.replay_key == "k1"
    assert "site-d-history" in result.reason
    verdict = _by_source(result)["site-d-history"]
    assert verdict.decision == "stale" and "900 s" in verdict.reason


@pytest.mark.parametrize("status,decided", [("running", "fresh"), ("none", "skip"), ("done", "skip"),
                                            ("error", "error")])
def test_a_replay_that_is_running_absent_done_or_unreadable_is_not_acted_on(status, decided):
    result = decision.decide(NOW, _healthy(), _sfc(), restarts=[], limits=LIMITS, replay=_replay(status))
    assert result.restart is False and result.replay_key is None
    assert _by_source(result)["site-d-history"].decision == decided


def test_an_overdue_replay_restarts_sfc_only_once_per_replay():
    """The server replays a stopped window at most once more for a new
    reader, so a second SFC restart for the same replay would do nothing."""
    result = decision.decide(NOW, _healthy(), _sfc(), restarts=[], limits=LIMITS,
                             replay=_replay("overdue", acted=True))
    assert result.restart is False
    verdict = _by_source(result)["site-d-history"]
    assert verdict.decision == "skip"
    assert "already restarted SFC once for this replay" in verdict.reason


def test_an_overdue_replay_is_held_back_by_the_same_guards():
    result = decision.decide(NOW, _healthy(), _sfc(deployed=5 * MIN), restarts=[], limits=LIMITS,
                             replay=_replay("overdue"))
    assert result.restart is False and result.replay_key is None


def test_a_live_restart_with_a_replay_running_does_not_claim_the_replay():
    result = decision.decide(NOW, _healthy("site-a"), _sfc(), restarts=[], limits=LIMITS,
                             replay=_replay("running"))
    assert result.restart is True and result.replay_key is None


# --- the restart record keeps a day of restarts (a replay's recovery is remembered by them)


def test_kept_restarts_keeps_a_day_oldest_first():
    kept = decision.kept_restarts([NOW - 86_401, NOW - 86_399, NOW - 10, NOW - 7200, "bad", True], NOW)
    assert kept == [NOW - 86_399, NOW - 7200, NOW - 10]
    assert decision.RECORD_RETENTION_SECONDS == 86_400


def test_a_full_day_of_restarts_fits_a_standard_ssm_parameter():
    """At the cap (2 an hour) a day is 48 restarts; SSM standard parameters hold 4 KB."""
    import json
    restarts = [1_790_000_000.123456 + 1800 * k for k in range(48)]
    assert len(json.dumps({"restarts": restarts})) < 4096
