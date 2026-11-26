# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""agents/failure_signals.py — the ADR-0059 accumulator core (Task 1B).

Offline pins: threshold semantics, per-turn dedup, TTL attribute, per-run
filing cap, sweep supersede with steward-wins conditional writes, the
never-raise boundary, import purity, and the review-list prefix filter.
"""
import inspect
import subprocess  # nosec B404 - import-purity check, fixed argv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.failure_signals as fs  # noqa: E402
import agents.query_intelligence as qi  # noqa: E402
import agents.self_learning as sl  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeTable:
    """Minimal DynamoDB double: update_item with the two conditions the
    module uses (turn-dedup ADD; steward-wins supersede) + paginated scan."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.update_calls = 0

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ConditionExpression="", ExpressionAttributeNames=None):
        self.update_calls += 1
        # SCHEMA-FAITHFUL (live-found 2026-08-07): the real table has a
        # COMPOSITE key (mapping_id, discovered_at) — a hash-only Key is a
        # ValidationException in production, so the fake requires both.
        if "discovered_at" not in Key:
            raise _Validation()
        mid = Key["mapping_id"]
        row = self.rows.get(mid)
        v = ExpressionAttributeValues
        if "contains(turn_ids" in ConditionExpression:
            turn = v[":turn"]
            if row is not None and turn in row.get("turn_ids", set()):
                raise _Conditional()
            row = row or {"mapping_id": mid, "turn_ids": set(), "sig_count": 0,
                          "first_seen": v[":now"]}
            row["turn_ids"] = set(row["turn_ids"]) | set(v[":tset"])
            row["sig_count"] += v[":one"]
            row.update(last_seen=v[":now"], shape=v[":shape"],
                       system_id=v[":sys"], subject_json=v[":subj"],
                       ttl_epoch=v[":ttl"])
            self.rows[mid] = row
            return
        # steward-wins supersede
        if row is None or row.get("status") != v[":pending"] \
                or row.get("source") != v[":agent"]:
            raise _Conditional()
        row.update(status=v[":sup"], superseded_reason=v[":why"],
                   superseded_at=v[":now"])

    def scan(self, **kwargs):
        return {"Items": [dict(r, turn_ids=list(r.get("turn_ids", [])))
                          for r in self.rows.values()]}


class _Conditional(Exception):
    pass


_Conditional.__name__ = "ConditionalCheckFailedException"


class _Validation(Exception):
    pass


_Validation.__name__ = "ValidationException"


class RaisingTable:
    def update_item(self, **kwargs):
        raise RuntimeError("store down")

    def scan(self, **kwargs):
        raise RuntimeError("store down")


@pytest.fixture()
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(sl, "_get_table", lambda: t)
    return t


# ── record + dedup + TTL ─────────────────────────────────────────────────────


def test_threshold_needs_distinct_turns(table):
    for i in range(3):
        assert fs.record_failure_signal(
            "terminology_miss", "sysA", {"term": "cell"}, f"turn-{i}") is True
    assert fs.signals_over_threshold()[0]["shape"] == "terminology_miss"


def test_same_turn_never_double_counts(table):
    assert fs.record_failure_signal("empty_join", "sysA", {"field": "id"}, "t1")
    for _ in range(5):
        assert fs.record_failure_signal(
            "empty_join", "sysA", {"field": "id"}, "t1") is False
    key = fs.shape_key("empty_join", "sysA", {"field": "id"})
    assert table.rows[key]["sig_count"] == 1
    assert fs.signals_over_threshold() == []


def test_below_threshold_not_returned(table):
    fs.record_failure_signal("terminology_miss", "s", {"term": "x"}, "t1")
    fs.record_failure_signal("terminology_miss", "s", {"term": "x"}, "t2")
    assert fs.signals_over_threshold() == []  # threshold is 3


def test_unit_confusion_threshold_is_two(table):
    fs.record_failure_signal("unit_confusion", "s", {"concept": "temp"}, "t1")
    fs.record_failure_signal("unit_confusion", "s", {"concept": "temp"}, "t2")
    assert len(fs.signals_over_threshold()) == 1


def test_rows_carry_ttl_attribute(table):
    fs.record_failure_signal("empty_join", "s", {"field": "f"}, "t1")
    row = next(iter(table.rows.values()))
    import time
    assert row["ttl_epoch"] > time.time() + 89 * 86400


def test_unknown_shape_and_blank_inputs_refused(table):
    assert fs.record_failure_signal("weird_shape", "s", {}, "t") is False
    assert fs.record_failure_signal("empty_join", "", {}, "t") is False
    assert fs.record_failure_signal("empty_join", "s", {}, "") is False
    assert table.update_calls == 0


def test_shape_key_deterministic_and_prefixed():
    a = fs.shape_key("empty_join", "s", {"b": 1, "a": 2})
    b = fs.shape_key("empty_join", "s", {"a": 2, "b": 1})
    assert a == b and a.startswith("failure_signal#empty_join|s|")


# ── never-raise boundary ─────────────────────────────────────────────────────


def test_raising_table_never_raises(monkeypatch):
    monkeypatch.setattr(sl, "_get_table", lambda: RaisingTable())
    assert fs.record_failure_signal("empty_join", "s", {}, "t1") is False
    assert fs.signals_over_threshold() == []
    # Inner layers degrade (over-threshold reads as empty, review list reads
    # as empty), so the sweep completes CLEAN with nothing filed — the
    # never-raise contract is about the caller, not an error field.
    out = fs.sweep_and_file_questions()
    assert out["filed"] == [] and out["superseded"] == []


def test_module_import_pure():
    code = ("import sys; sys.modules['boto3']=None; "
            "import agents.failure_signals")
    proc = subprocess.run(  # nosec B603 - fixed argv
        [sys.executable, "-c", code],
        capture_output=True, cwd=str(Path(__file__).resolve().parents[2]))
    assert proc.returncode == 0, proc.stderr.decode()[:400]


# ── sweep: filing cap + telemetry + supersede ────────────────────────────────


def _seed_over_threshold(table, n):
    for i in range(n):
        for t in range(3):
            fs.record_failure_signal(
                "terminology_miss", f"sys{i}", {"term": f"w{i}"}, f"t{i}-{t}")


def test_sweep_files_capped_and_bumps_telemetry(table, monkeypatch):
    _seed_over_threshold(table, 8)
    filed, bumps = [], []
    monkeypatch.setattr(fs, "_file_question",
                        lambda row: filed.append(row["system_id"]) or f"m-{len(filed)}")
    monkeypatch.setattr(fs, "_bump_question_telemetry",
                        lambda mid: bumps.append(mid))
    out = fs.sweep_and_file_questions()
    assert len(out["filed"]) == fs.SWEEP_FILE_CAP == len(bumps)
    assert out["skipped"] == 8 - fs.SWEEP_FILE_CAP


def test_sweep_orders_by_count_desc(table, monkeypatch):
    _seed_over_threshold(table, 2)
    fs.record_failure_signal("terminology_miss", "sys1", {"term": "w1"}, "extra")
    seen = []
    monkeypatch.setattr(fs, "_file_question",
                        lambda row: seen.append(row["system_id"]) or "m")
    monkeypatch.setattr(fs, "_bump_question_telemetry", lambda mid: None)
    fs.sweep_and_file_questions()
    assert seen[0] == "sys1"  # 4 observations beats 3


def test_supersede_only_stale_pending_agent_rows(table, monkeypatch):
    # live signal for sysA; stale pending question for sysB; steward-touched
    # row for sysC must survive (condition fails → skipped, never raises).
    _seed_over_threshold(table, 1)  # sys0 live
    questions = [
        {"mapping_id": "q-b", "proposed_type": "steward_question",
         "status": "pending", "source": "agent",
         "from_system": "sysB", "reason": "terminology_miss"},
        {"mapping_id": "q-c", "proposed_type": "steward_question",
         "status": "pending", "source": "steward",
         "from_system": "sysC", "reason": "terminology_miss"},
        {"mapping_id": "q-live", "proposed_type": "steward_question",
         "status": "pending", "source": "agent",
         "from_system": "sys0", "reason": "terminology_miss"},
    ]
    for q in questions:
        q["discovered_at"] = "2026-08-07T00:00:00Z"
    table.rows["q-b"] = dict(questions[0])
    table.rows["q-c"] = dict(questions[1])
    monkeypatch.setattr(sl, "get_all_mappings", lambda: questions)
    monkeypatch.setattr(fs, "_file_question", lambda row: "m-1")
    monkeypatch.setattr(fs, "_bump_question_telemetry", lambda mid: None)
    out = fs.sweep_and_file_questions()
    assert out["superseded"] == ["q-b"]
    assert table.rows["q-b"]["status"] == "superseded"
    assert table.rows["q-b"]["superseded_reason"] == "signal-decayed"
    assert table.rows["q-c"]["status"] == "pending"  # steward-wins


# ── classify_empty_join (pure) ───────────────────────────────────────────────


def test_classify_empty_join_one_failed_leg():
    assert qi.classify_empty_join(
        {"a": "3 rows returned", "b": "no results found"}) == "b"


def test_classify_empty_join_none_cases():
    assert qi.classify_empty_join({"a": "rows"}) is None  # single system
    assert qi.classify_empty_join(
        {"a": "no results found", "b": "no results found"}) is None  # outage
    assert qi.classify_empty_join({"a": "rows", "b": "rows"}) is None
    assert qi.classify_empty_join("not a dict") is None


# ── hygiene ──────────────────────────────────────────────────────────────────


def test_signal_rows_invisible_to_review_list(monkeypatch):
    class _T:
        def scan(self, **kwargs):
            return {"Items": [
                {"mapping_id": "failure_signal#empty_join|s|abc", "sig_count": 3},
                {"mapping_id": "real-1", "status": "pending"}]}
    monkeypatch.setattr(sl, "_get_table", lambda: _T())
    assert [r["mapping_id"] for r in sl.get_all_mappings()] == ["real-1"]


def test_all_functions_under_50_lines():
    for name, obj in inspect.getmembers(fs, inspect.isfunction):
        if obj.__module__ != fs.__name__:
            continue
        body = inspect.getsource(obj).splitlines()
        assert len(body) <= 60, f"{name} too long ({len(body)} incl. docstring)"


def test_signal_writes_carry_the_composite_key(table):
    """Live-found 2026-08-07: the table key is (mapping_id, discovered_at);
    counter rows use a STABLE sort value so every update hits ONE row."""
    fs.record_failure_signal("empty_join", "s", {"f": 1}, "t1")
    fs.record_failure_signal("empty_join", "s", {"f": 1}, "t2")
    row = next(iter(table.rows.values()))
    assert row["sig_count"] == 2  # same row updated twice, never forked


def test_steward_source_bypasses_staging():
    """Live-found 2026-08-07: a steward's interview answer (source=steward)
    was held in the co-reference staging buffer awaiting a corroborating
    observation that can never come — human assertions are already
    confirmed and must file directly, like source=user."""
    assert sl._staging_applies("value_mapping", "steward", "", "a", "b") is False
    assert sl._staging_applies("value_mapping", "user", "", "a", "b") is False
    assert sl._staging_applies("value_mapping", "agent", "", "a", "b") is True
