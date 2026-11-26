# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An exact split leaves the agent time to answer.

Measured in two dry runs on a rehearsal account (2026-10-23): a query the
engine refused for scanning over the per-query limit went to the exact split
with the question's own deadline, and the split stopped only at that
deadline. One ran 38.2 s of a 60 s question, finished one of three OR
branches and returned no rows (``wall_time``); the agent's next query got a
1 s poll window and the one after it was cancelled. Another probed a plan
already over its byte cap for 13.2 s and then refused (``byte_cap``). No
split in five dry runs finished; eight spent 5-44 s each.

Now:

1. a caller keeps a reserve back from its deadline
   (``CapsLimit.reserve_seconds``); the system agent keeps one for one more
   query and one model turn (``SYSTEM_AGENT_SPLIT_RESERVE_SECONDS``);
2. for a caller that keeps one, the split declines at its start when the time
   left after the reserve is shorter than one piece (``PIECE_SECONDS_FLOOR``);
3. after planning it declines when the plan cannot fit the time left: its
   pieces in waves at the concurrency the slot rule gives, each taking the
   shortest query time this split has measured (the floor before it has
   measured one), and two more pieces for each OR branch still to plan;
4. a plan already over a cap is not probed with less than
   ``PROBE_MIN_SECONDS`` left;
5. each decline is the scan-limit refusal with ``wall_time`` and a reason
   saying not enough time was left for the pieces, so the agent writes a
   narrower query; every text that promises an exact total says so;
6. a caller that keeps no reserve (a dashboard card refresh, a chat turn's
   metric notice) gets none of 2-4: it has nothing else to spend the time on,
   so its split tries to its deadline as before;
7. every split keeps its caller's absolute deadline: client construction and
   planning reads come off it, each planning read is checked against it, the
   catalog and workgroup calls are abandoned at it, and no metadata read
   polls past it.

What a split returns and reads with time to spare, and what a card refresh
returns, is compared with what the commit before these changes did
(``fixtures/exact_split_baseline.json``, ``_split_baseline_cases``). Fully
offline (``FakeLake``); synthetic names only. The test of a split that runs a
piece through the agent's tool runs the split on a clock that starts when the
split does (``_athena_split_clock``).
"""
from __future__ import annotations

import json
import threading
import time
import types
from pathlib import Path

import pytest

import agents.athena_tools as at
import agents.system_agent as sa
from agents import athena_exact_split as sp
from agents import metric_athena_scope as mas

from . import _split_baseline_cases as cases
from . import test_athena_exact_split as base
from . import test_athena_exact_split_or_states as ors
from ._athena_split_clock import split_clock
from ._athena_split_fakes import FakeLake, RunsUntilStopped

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_NO_TIME = "not enough time is left for the pieces"
_OVER_BYTES = {"exact_split_max_total_bytes": 7_000_000}
_RESERVE_ENV = "SYSTEM_AGENT_SPLIT_RESERVE_SECONDS"
_DECLINES_FOR_TIME = "may decline when too little time is left for the pieces"


@pytest.fixture(autouse=True)
def _quick_polls_fresh_caches(monkeypatch):
    monkeypatch.setattr(at, "_DEFAULT_POLL_INTERVAL", 0.01)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    sp.clear_layout_cache()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    sp.clear_layout_cache()


def _lake(**kwargs) -> FakeLake:
    return FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created",
                    cutoff=base._CUTOFF, **kwargs)


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None, limit=None,
           source: str = "lake-a") -> dict:
    with sp.limited(limit):
        return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system=source,
                                         client=lake, glue_client=lake.glue)


def _refusal(lake: FakeLake, sql: str, cfg: dict | None = None, limit=None) -> dict:
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql, cfg, limit)
    payload = at._tool_error(info.value)
    assert payload["error_type"] == "scan_budget_exceeded" and "rows" not in payload
    return payload["exact_split"]


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


def _metadata_reads(lake: FakeLake) -> list[str]:
    return [q for q in lake.started if "$snapshots" in q or "$files" in q]


def _clock(monkeypatch, start: float = 0.0) -> dict:
    """The split's clock, fixed at ``start`` (only for checks that run no piece)."""
    clock = {"t": start}
    monkeypatch.setattr(sp, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))
    return clock


def _keeping(seconds_after_reserve: float, reserve: float = 15.0) -> sp.CapsLimit:
    """A caller's limit that keeps ``reserve`` back and leaves the split
    ``seconds_after_reserve`` from now (the split also keeps back its own
    ``DEADLINE_OVERRUN_SECONDS`` for such a caller)."""
    kept = reserve + sp.DEADLINE_OVERRUN_SECONDS
    return sp.CapsLimit(deadline=time.monotonic() + kept + seconds_after_reserve,
                        reserve_seconds=reserve)


# --------------------------------------------------------------------------- #
# 1. The reserve comes off the caller's deadline
# --------------------------------------------------------------------------- #
def test_the_reserve_comes_off_the_callers_deadline(monkeypatch):
    _clock(monkeypatch, 1000.0)
    with sp.limited(sp.CapsLimit(deadline=1030.0, reserve_seconds=15.0)):
        assert sp.resolve_caps({}).max_seconds == 15.0 - sp.DEADLINE_OVERRUN_SECONDS
    with sp.limited(sp.CapsLimit(deadline=1030.0)):
        assert sp.resolve_caps({}).max_seconds == 30.0          # no reserve: as before
    with sp.limited(sp.CapsLimit(deadline=1010.0, reserve_seconds=15.0)):
        assert sp.resolve_caps({}).max_seconds == 0.0           # a reserve past the time left
    with sp.limited(sp.CapsLimit(max_seconds=40, reserve_seconds=15.0)):
        assert sp.resolve_caps({}).max_seconds == 40.0          # kept back from a deadline only


# --------------------------------------------------------------------------- #
# 2. The decline at the start
# --------------------------------------------------------------------------- #
def _start(monkeypatch, left: float, reserve: float | None = 15.0):
    """A split started at 1000.0 with ``left`` seconds after what its caller
    keeps back (the reserve and, with one, the split's own overrun)."""
    _clock(monkeypatch, 1000.0)
    lake = _lake()
    kept = reserve + sp.DEADLINE_OVERRUN_SECONDS if reserve else 0.0
    limit = sp.CapsLimit(deadline=1000.0 + kept + left, reserve_seconds=reserve)
    with sp.limited(limit):
        return sp._start(base._P1, dict(_CFG), object(), lake), lake


def test_the_split_starts_when_the_time_after_the_reserve_is_exactly_one_piece(monkeypatch):
    (_shape, run), lake = _start(monkeypatch, sp.PIECE_SECONDS_FLOOR)
    assert run.caps.max_seconds == sp.PIECE_SECONDS_FLOOR
    assert run.deadline == 1000.0 + sp.PIECE_SECONDS_FLOOR     # the reserve stays the caller's
    assert lake.started == []


def test_the_split_declines_at_its_start_just_short_of_one_piece(monkeypatch):
    with pytest.raises(sp.ExactSplitRefused) as info:
        _start(monkeypatch, sp.PIECE_SECONDS_FLOOR - 0.01)
    assert (info.value.reason, info.value.detail) == ("wall_time", sp.NO_TIME_DETAIL)


def test_a_split_declined_at_its_start_reads_nothing_and_says_why():
    lake = _lake()
    split = _refusal(lake, base._P1, limit=_keeping(1.5))
    assert split["reason"] == "wall_time"
    assert _NO_TIME in split["detail"] and "narrower query" in split["detail"]
    assert _metadata_reads(lake) == [] and _pieces(lake) == []


def test_a_caller_with_no_reserve_starts_with_what_it_started_with_before(monkeypatch):
    # The floor is for a caller that keeps time back. Without a reserve the
    # split starts with more than 1 s, as on the commit before.
    (_shape, run), _lake_ = _start(monkeypatch, 1.5, reserve=None)
    assert run.deadline == 1001.5
    with pytest.raises(sp.ExactSplitRefused) as info:
        _start(monkeypatch, 1.0, reserve=None)
    assert (info.value.reason, info.value.detail) == ("wall_time", "no time is left for the pieces")


# --------------------------------------------------------------------------- #
# 3. The decline after planning: pieces x per-piece time, at the slot rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pieces, slots, waves", [
    (1, 1, 1), (2, 1, 2), (2, 4, 1), (3, 1, 3), (3, 4, 2), (5, 2, 3), (9, 4, 3)])
def test_pieces_run_in_waves_at_the_slot_rule(pieces, slots, waves):
    # More than two pieces: the biggest runs alone first, then the rest
    # ``slots`` at a time (``_execute``).
    assert sp._waves(pieces, slots) == waves


def _planned(monkeypatch, *, left: float, measured=(), over_cap: bool = False,
             spent: int = 5_000_000_000, cap: int = 5_000_000_000,
             reserve: float = 15.0) -> sp._Run:
    _clock(monkeypatch, 0.0)
    run = sp._Run({}, None, sp.Caps(16, 12 * 1024 ** 3, 150.0), left)
    run.spent, run.cap, run.over_cap_plan = spent, cap, over_cap
    run.query_seconds = list(measured)
    run.reserve_seconds = reserve
    return run


def test_the_concurrency_is_the_slot_rules(monkeypatch):
    # The refused query's bytes are counted first: (12 GiB - 5 GB) // 5 GB = 1.
    run = _planned(monkeypatch, left=60.0)
    assert sp._slots(run) == 1
    run.spent = 0
    assert sp._slots(run) == 2
    run.cap = 1_000_000_000
    assert sp._slots(run) == sp.CONCURRENCY


def test_the_per_piece_time_is_the_shortest_query_this_split_measured(monkeypatch):
    assert sp._piece_seconds(_planned(monkeypatch, left=60.0)) == sp.PIECE_SECONDS_FLOOR
    assert sp._piece_seconds(_planned(monkeypatch, left=60.0, measured=(3.4, 2.9, 4.0))) == 2.9


def test_a_plan_that_exactly_fits_the_time_left_runs(monkeypatch):
    # Three pieces at one slot: three waves of 3 s.
    sp._check_time(_planned(monkeypatch, left=9.0, measured=(3.0,)), [object()] * 3, 0)


def test_a_plan_just_short_of_the_time_left_is_declined(monkeypatch):
    with pytest.raises(sp.ExactSplitRefused) as info:
        sp._check_time(_planned(monkeypatch, left=8.99, measured=(3.0,)), [object()] * 3, 0)
    assert (info.value.reason, info.value.detail) == ("wall_time", sp.NO_TIME_DETAIL)


def test_each_or_branch_still_to_plan_needs_two_more_pieces(monkeypatch):
    # This branch's three waves and two later branches of two, at 3 s: 21 s.
    sp._check_time(_planned(monkeypatch, left=21.0, measured=(3.0,)), [object()] * 3, 2)
    with pytest.raises(sp.ExactSplitRefused):
        sp._check_time(_planned(monkeypatch, left=20.99, measured=(3.0,)), [object()] * 3, 2)


def test_more_slots_fit_more_pieces_in_the_same_time(monkeypatch):
    # Five pieces at four slots: the probe, then one wave of four.
    sp._check_time(_planned(monkeypatch, left=6.0, measured=(3.0,), spent=0, cap=10 ** 9),
                   [object()] * 5, 0)


def test_a_caller_that_keeps_no_reserve_gets_no_time_checks(monkeypatch):
    # A card refresh or a metric notice: nine pieces, two branches to come and
    # an over-cap plan, with half a second left, still run to the deadline.
    run = _planned(monkeypatch, left=0.5, measured=(3.0,), over_cap=True, reserve=0.0)
    sp._check_time(run, [object()] * 9, 2)


def test_a_plan_that_cannot_fit_is_declined_before_any_piece_runs(monkeypatch):
    monkeypatch.setattr(sp, "_piece_seconds", lambda _run: 30.0, raising=False)
    lake = _lake()
    split = _refusal(lake, base._P1, limit=_keeping(20))
    assert split["reason"] == "wall_time" and _NO_TIME in split["detail"]
    assert any("$files" in q for q in lake.started)              # it planned
    assert _pieces(lake) == []                                    # and ran no piece


def test_an_or_split_that_cannot_fit_is_declined_at_its_first_branch(monkeypatch):
    monkeypatch.setattr(sp, "_piece_seconds", lambda _run: 30.0, raising=False)
    lake = ors._lake()
    with sp.limited(_keeping(20)):
        split = ors._refused(lake, ors._or_of_states())
    assert split["reason"] == "wall_time" and _NO_TIME in split["detail"]
    assert _pieces(lake) == []


def test_each_or_branch_is_checked_with_the_branches_still_to_plan(monkeypatch):
    seen = []
    real = getattr(sp, "_check_time", None)

    def spy(run, leaves, later_branches):
        seen.append(later_branches)
        return real(run, leaves, later_branches)
    monkeypatch.setattr(sp, "_check_time", spy, raising=False)
    lake = ors._lake()
    result = ors._exact(lake, ors._or_of_states())
    assert result["exact_split"]["branches"] == 3
    assert seen == [2, 1, 0]


def test_the_split_measures_its_own_queries(monkeypatch):
    lake = _lake()
    runs = []
    real = sp._compute_against

    def keep(run, *args, **kwargs):
        runs.append(run)
        return real(run, *args, **kwargs)
    monkeypatch.setattr(sp, "_compute_against", keep)
    result = _exact(lake, base._P1)
    measured = runs[0].query_seconds
    # the snapshot, the two layout reads and every piece
    assert len(measured) == 3 + result["exact_split"]["pieces"]
    assert all(seconds >= 0 for seconds in measured)


# --------------------------------------------------------------------------- #
# 4. No over-cap probe when time is short
# --------------------------------------------------------------------------- #
def test_an_over_cap_plan_is_probed_with_exactly_the_probe_time_left(monkeypatch):
    sp._check_time(_planned(monkeypatch, left=sp.PROBE_MIN_SECONDS, measured=(1.0,),
                            over_cap=True), [object()] * 3, 0)


def test_an_over_cap_plan_is_not_probed_just_short_of_the_probe_time(monkeypatch):
    with pytest.raises(sp.ExactSplitRefused) as info:
        sp._check_time(_planned(monkeypatch, left=sp.PROBE_MIN_SECONDS - 0.01, measured=(1.0,),
                                over_cap=True), [object()] * 3, 0)
    assert info.value.detail == sp.NO_TIME_DETAIL
    # The same time is ample for a plan within its caps.
    sp._check_time(_planned(monkeypatch, left=sp.PROBE_MIN_SECONDS - 0.01, measured=(1.0,)),
                   [object()] * 3, 0)


def test_an_over_cap_plan_with_ample_time_is_still_probed():
    lake = _lake(estimate_factor=1.0)
    split = _refusal(lake, base._P1, _OVER_BYTES)
    assert split["reason"] == "byte_cap" and len(_pieces(lake)) == 1


def test_an_over_cap_plan_is_not_probed_when_time_is_short():
    lake = _lake(estimate_factor=1.0)
    split = _refusal(lake, base._P1, _OVER_BYTES, limit=_keeping(20))
    assert split["reason"] == "wall_time" and _NO_TIME in split["detail"]
    assert _pieces(lake) == []


def test_an_over_cap_plan_is_probed_with_a_short_cap_and_no_reserve():
    # A 20 s cap with no reserve (a card's own cap is 30 s): the probe runs,
    # as on the commit before, and shows the plan over the byte cap.
    lake = _lake(estimate_factor=1.0)
    split = _refusal(lake, base._P1, _OVER_BYTES, limit=sp.CapsLimit(max_seconds=20))
    assert split["reason"] == "byte_cap" and len(_pieces(lake)) == 1


# --------------------------------------------------------------------------- #
# 5. The reason the agent reads, and every text that promises an exact total
# --------------------------------------------------------------------------- #
def test_the_time_reason_tells_the_agent_to_write_a_narrower_query():
    text = sp.ExactSplitRefused("wall_time", sp.NO_TIME_DETAIL).to_dict()["detail"]
    assert _NO_TIME in text and "run a narrower query instead" in text
    assert "Give no total; do not add numbers yourself." in text
    assert "\u2014" not in text and "\u2013" not in text


def test_the_scan_limit_guidance_says_the_tool_may_decline_for_time():
    text = at._ENGINE_SCAN_LIMIT_GUIDANCE
    assert "computed exactly by the tool" in text
    assert _DECLINES_FOR_TIME in text
    assert "run a narrower query" in text
    assert "\u2014" not in text and "\u2013" not in text


def test_the_new_wording_is_not_read_as_a_transient_error_or_a_timeout():
    # The orchestrator retries a tool error carrying one of its transient
    # markers, and the chat client labels a timeout pattern; neither applies.
    from agents.chat_client import classify_error_text
    from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS
    detail = sp.ExactSplitRefused("wall_time", sp.NO_TIME_DETAIL).to_dict()["detail"]
    for text in (at._ENGINE_SCAN_LIMIT_GUIDANCE, detail):
        low = text.lower()
        assert not any(m in low for m in _TRANSIENT_TOOL_ERROR_MARKERS + _AUTH_TOOL_ERROR_MARKERS)
        assert classify_error_text(text) != "timeout", text


_PROMISING_TEXTS = ("scan-limit guidance", "partition rule", "raw run tool docstring",
                    "split window recovery", "mode B bound rule", "Quick skill doc")


def _texts_promising_exact_totals() -> dict[str, str]:
    from agents import connection_types as ct
    from agents import workflow_guidance as wg
    tools = {t.tool_name: t for t in at.make_athena_tools(dict(_CFG))}
    skill = Path(__file__).resolve().parents[3] / "docs" / "amazon-quick-skill.md"
    rule_lines = [line for line in skill.read_text(encoding="utf-8").splitlines()
                  if "computed exactly by the tool" in line]
    return dict(zip(_PROMISING_TEXTS, (
        at._ENGINE_SCAN_LIMIT_GUIDANCE,
        ct.ATHENA_PARTITION_RULE,
        tools["athena_run_query"].tool_spec["description"],
        sa._SPLIT_WINDOW_RECOVERY,
        wg.ATHENA_BOUND_RULE,
        "\n".join(rule_lines),
    )))


@pytest.mark.parametrize("name", _PROMISING_TEXTS)
def test_every_text_promising_an_exact_total_says_the_tool_may_decline_for_time(name):
    flat = " ".join(_texts_promising_exact_totals()[name].split())
    assert "computed exactly by the tool" in flat, name
    assert _DECLINES_FOR_TIME in flat and "run a narrower query" in flat, name
    assert "\u2014" not in flat and "\u2013" not in flat, name


# --------------------------------------------------------------------------- #
# 6. Against the commit before: ample time, and a card refresh
# --------------------------------------------------------------------------- #
_BASELINE = json.loads(cases.FIXTURE.read_text(encoding="utf-8"))
_LIMITS = {
    "no limit": lambda: None,
    "a far deadline": lambda: sp.CapsLimit(deadline=time.monotonic() + 600),
    "a far deadline and a reserve": lambda: sp.CapsLimit(deadline=time.monotonic() + 600,
                                                         reserve_seconds=15.0),
}


@pytest.mark.parametrize("limit", sorted(_LIMITS))
@pytest.mark.parametrize("case", ["count", "over-cap probe", "or of states"])
def test_with_time_to_spare_a_split_returns_and_reads_what_it_did_before(case, limit):
    out = cases.run_case(case, _LIMITS[limit]())
    assert "result" in out, out.get("refused")
    assert out == _BASELINE[case]


def test_a_card_refresh_probes_an_over_cap_plan_as_before_and_is_exact():
    # Three metadata reads at 3.5 s leave the card about 19.5 s of its 30 s
    # when the over-cap first plan is probed; the probe shows three pieces fit.
    out = cases.run_case("card, over-cap probe")
    assert "result" in out, out.get("refused")
    assert out == _BASELINE["card, over-cap probe"]
    split = out["result"]["exact_split"]
    assert split["applied"] is True and split["first_plan_over_cap"] is True
    assert split["pieces"] <= mas.CARD_MAX_PIECES
    assert out["split_clock_seconds"] < mas.CARD_MAX_SECONDS
    make_lake, sql, _cfg, _card = cases.CASES["card, over-cap probe"]
    key = json.dumps
    assert sorted(out["result"]["rows"], key=key) == sorted(make_lake().reference(sql), key=key)


def test_the_baseline_was_captured_with_no_time_check_in_the_way():
    # Every baseline case computed its result.
    assert set(_BASELINE) == set(cases.CASES)
    assert all("result" in out for out in _BASELINE.values())


# --------------------------------------------------------------------------- #
# 7. The caller's absolute deadline (real time, no frozen clock)
# --------------------------------------------------------------------------- #
class _Blocking:
    """``inner``, with ``method`` waiting ``seconds`` (or until ``release``
    is set) before it answers."""

    def __init__(self, inner, method: str, seconds: float):
        self.inner, self.method, self.seconds = inner, method, seconds
        self.release, self.calls = threading.Event(), 0

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if name != self.method:
            return attr

        def slow(*args, **kwargs):
            self.calls += 1
            self.release.wait(self.seconds)
            return attr(*args, **kwargs)
        return slow


def _scan_refusal(lake: FakeLake) -> at.AthenaEngineScanLimitError:
    return at.AthenaEngineScanLimitError("over", limit_source="engine",
                                         scanned_bytes=lake.cutoff + 1)


def _slow_athena_client(monkeypatch, lake: FakeLake, seconds: float) -> list:
    """The split's own clients (it is given none): the Athena one takes
    ``seconds`` to build, as an assumed-role session can."""
    made = []

    def build(service, *_args, **_kwargs):
        made.append(service)
        if service == "glue":
            return lake.glue
        time.sleep(seconds)  # nosemgrep: arbitrary-sleep
        return lake
    monkeypatch.setattr(at, "boto3_client_for", build)
    return made


def _runs(monkeypatch) -> list:
    runs = []
    real = sp._compute_against

    def keep(run, *args, **kwargs):
        runs.append(run)
        return real(run, *args, **kwargs)
    monkeypatch.setattr(sp, "_compute_against", keep)
    return runs


@pytest.mark.parametrize("reserve", [0.0, 15.0])
def test_a_slow_client_construction_comes_off_the_callers_deadline(monkeypatch, reserve):
    lake = _lake()
    made = _slow_athena_client(monkeypatch, lake, 0.5)
    runs = _runs(monkeypatch)
    end = time.monotonic() + reserve + 4.0
    with sp.limited(sp.CapsLimit(deadline=end, reserve_seconds=reserve)):
        result = sp.compute_exact(base._P1, dict(_CFG), refusal=_scan_refusal(lake),
                                  glue_client=lake.glue)
    assert "athena" in made and result["exact_split"]["applied"] is True
    assert runs and runs[0].deadline <= end - reserve


def test_a_split_past_its_deadline_after_building_its_client_reads_nothing(monkeypatch):
    lake = _lake()
    _slow_athena_client(monkeypatch, lake, 2.3)
    with sp.limited(sp.CapsLimit(deadline=time.monotonic() + 2.2)):
        with pytest.raises(sp.ExactSplitRefused) as info:
            sp.compute_exact(base._P1, dict(_CFG), refusal=_scan_refusal(lake),
                             glue_client=lake.glue)
    assert info.value.reason == "wall_time"
    assert lake.glue.calls == 0 and _metadata_reads(lake) == [] and _pieces(lake) == []


@pytest.mark.parametrize("which", ["catalog", "workgroup"])
def test_a_slow_catalog_or_workgroup_call_is_abandoned_at_the_deadline(which):
    lake = _lake()
    athena = _Blocking(lake, "get_work_group", 8.0) if which == "workgroup" else lake
    glue = _Blocking(lake.glue, "get_table", 8.0) if which == "catalog" else lake.glue
    slow = athena if which == "workgroup" else glue
    started = time.monotonic()
    try:
        with sp.limited(sp.CapsLimit(deadline=started + 2.5)):
            with pytest.raises(sp.ExactSplitRefused) as info:
                sp.compute_exact(base._P1, dict(_CFG), refusal=_scan_refusal(lake),
                                 client=athena, glue_client=glue)
        took = time.monotonic() - started
    finally:
        slow.release.set()
    assert info.value.reason == "wall_time" and slow.calls == 1
    assert took < 3.5
    assert _metadata_reads(lake) == [] and _pieces(lake) == []


def test_a_metadata_read_polls_no_longer_than_the_time_left(monkeypatch):
    lake = _lake()
    glue = _Blocking(lake.glue, "get_table", 2.0)      # answers after 2 s
    polls = []
    real = at._poll_athena_query

    def spy(athena, query_id, timeout, max_scanned):
        sql = lake._queries.get(query_id, "")
        if "$snapshots" in sql or "$files" in sql:
            polls.append((timeout, end - time.monotonic()))
        return real(athena, query_id, timeout, max_scanned)
    monkeypatch.setattr(at, "_poll_athena_query", spy)
    end = time.monotonic() + 2.6
    with sp.limited(sp.CapsLimit(deadline=end)):
        with pytest.raises(sp.ExactSplitRefused) as info:
            sp.compute_exact(base._P1, dict(_CFG), refusal=_scan_refusal(lake), client=lake,
                             glue_client=glue)
    assert info.value.reason == "wall_time"
    assert polls and all(timeout <= left + 0.05 for timeout, left in polls), polls


def test_the_or_column_type_read_is_bounded_by_the_deadline():
    lake = ors._lake()
    glue = _Blocking(lake.glue, "get_table", 8.0)
    started = time.monotonic()
    try:
        with sp.limited(sp.CapsLimit(deadline=started + 2.5)):
            with pytest.raises(sp.ExactSplitRefused) as info:
                sp.compute_exact(ors._or_of_states(), dict(ors._CFG), refusal=_scan_refusal(lake),
                                 client=lake, glue_client=glue)
        took = time.monotonic() - started
    finally:
        glue.release.set()
    assert info.value.reason == "wall_time" and took < 3.5
    assert _pieces(lake) == []


# --------------------------------------------------------------------------- #
# 8. The system agent keeps the reserve
# --------------------------------------------------------------------------- #
def test_the_agents_split_reserve_defaults_and_is_env_tunable(monkeypatch):
    monkeypatch.delenv(_RESERVE_ENV, raising=False)
    assert sa._split_reserve_seconds() == 15.0
    monkeypatch.setenv(_RESERVE_ENV, "20")
    assert sa._split_reserve_seconds() == 20.0
    monkeypatch.setenv(_RESERVE_ENV, "0")
    assert sa._split_reserve_seconds() == 0.0
    for bad in ("-3", "soon", ""):
        monkeypatch.setenv(_RESERVE_ENV, bad)
        assert sa._split_reserve_seconds() == 15.0


def _agent_call(monkeypatch, *, left: float, cfg: dict | None = None):
    from . import test_system_agent_call_budget as cb
    lake = _lake()
    guarded, state = cb._lake_tool(monkeypatch, lake, cfg or {}, left=left)
    started = time.monotonic()
    payload = json.loads(guarded._tool_func(sql=base._P1, tool_context=cb._Ctx(state)))
    return payload, lake, time.monotonic() - started


def test_a_split_in_an_agent_call_with_only_the_reserve_left_declines_at_once(monkeypatch):
    monkeypatch.delenv(_RESERVE_ENV, raising=False)
    payload, lake, took = _agent_call(monkeypatch, left=10.0)
    assert payload["error_type"] == "scan_budget_exceeded", payload
    assert payload["exact_split"]["reason"] == "wall_time"
    assert _NO_TIME in payload["exact_split"]["detail"]
    assert _metadata_reads(lake) == [] and took < 5.0


def test_a_split_in_an_agent_call_stops_before_the_reserve(monkeypatch):
    monkeypatch.setenv(_RESERVE_ENV, "2")
    from . import test_system_agent_call_budget as cb
    lake = _lake()
    athena = RunsUntilStopped(lake)
    split_seconds = 3.0
    left = 2.0 + sp.DEADLINE_OVERRUN_SECONDS + split_seconds
    guarded, state = cb._lake_tool(monkeypatch, athena, {}, left=left)
    # The split starts with ``split_seconds``, more than one piece's time,
    # however long the plain query before it took, so it plans and runs a
    # piece rather than declining at its start for want of time.
    clock = split_clock(monkeypatch, state, left)
    started = clock.now()
    payload = json.loads(guarded._tool_func(sql=base._P1, tool_context=cb._Ctx(state)))
    took = clock.now() - started
    split = payload["exact_split"]
    assert split["reason"] == "wall_time", payload
    # A piece was running at the split's deadline, and the split stopped it there.
    pieces = [qid for qid, sql in lake._queries.items() if "FOR VERSION AS OF" in sql]
    assert pieces and set(pieces) <= set(lake.stopped)
    assert sp._PIECES_LATE_DETAIL in split["detail"] and _NO_TIME not in split["detail"]
    # The split's deadline is the reserve and its overrun before the
    # question's: the call returns with the reserve still left.
    assert split_seconds - 1e-6 <= took <= split_seconds + sp.DEADLINE_OVERRUN_SECONDS


def test_a_split_in_an_agent_call_with_ample_time_is_computed(monkeypatch):
    monkeypatch.delenv(_RESERVE_ENV, raising=False)
    payload, lake, _took = _agent_call(monkeypatch, left=55.0)
    assert payload["exact_split"]["applied"] is True, payload
    assert payload["rows"] == lake.reference(base._P1)
