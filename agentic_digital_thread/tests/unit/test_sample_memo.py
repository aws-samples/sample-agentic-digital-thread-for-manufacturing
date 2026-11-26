# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``sample_field_values`` memoizes a field's sample across callers.

A field's bounded identifier sample is the same evidence whichever pair,
direction, gate or miner asks for it. Before the memo every ``discover_pair``
call re-sampled both sides, so one system resolved against N peers sampled its
own fields 2N times and the post-provision miner repeated the whole sweep;
on a workshop account the seed's phase-2 resolve of one eight-table system
took 8.5 minutes. These tests pin the memo's contract: a hit never
reaches the backend, a failure is never memoized, an entry expires, and a
full join-discovery sweep samples each field once.
"""

from __future__ import annotations

import pytest

import agents.join_discovery as jd
import agents.resolve_evidence as re_


@pytest.fixture
def pg_counter(monkeypatch):
    """Count backend samples; return the values keyed by ``(table, field)``."""
    calls: list[tuple[str, str, str]] = []
    values = {
        ("wo", "cell"): ["SITE1", "SITE2", "SITE3", "SITE4", "SITE5"],
        ("asset", "site_name"): ["SITE1", "SITE2", "SITE3", "SITE4", "SITE5"],
        ("wo", "machine_name"): ["SITE1-CS6900Z-02", "SITE1-Canon-09"],
        ("asset", "machine"): ["Diamant1HC1", "Dryer0"],
        ("empty", "col"): [],
    }

    def _fake_pg(schema, table, field, limit):
        calls.append((schema, table, field))
        return list(values.get((table, field), ["v1", "v2", "v3", "v4", "v5"]))

    monkeypatch.setattr(re_, "_sample_postgres", _fake_pg)
    return calls


def _sample(sid, table, field, schema="s1"):
    return re_.sample_field_values(system_id=sid, connection_type="PostgreSQL",
                                   schema=schema, table=table, field=field)


def test_repeat_sample_is_served_from_the_memo(pg_counter):
    first = _sample("sys-a", "wo", "cell")
    second = _sample("sys-a", "wo", "cell")
    assert first == second == ["SITE1", "SITE2", "SITE3", "SITE4", "SITE5"]
    assert len(pg_counter) == 1


def test_memo_returns_a_copy_so_callers_cannot_mutate_it(pg_counter):
    first = _sample("sys-a", "wo", "cell")
    first.append("MUTATED")
    assert "MUTATED" not in _sample("sys-a", "wo", "cell")


def test_a_different_field_or_system_is_its_own_entry(pg_counter):
    _sample("sys-a", "wo", "cell")
    _sample("sys-a", "wo", "machine_name")
    _sample("sys-b", "wo", "cell")
    _sample("sys-a", "wo", "cell", schema="other")
    assert len(pg_counter) == 4


def test_an_empty_legitimate_sample_is_memoized(pg_counter):
    assert _sample("sys-a", "empty", "col") == []
    assert _sample("sys-a", "empty", "col") == []
    assert len(pg_counter) == 1


def test_a_failed_sample_is_never_memoized(monkeypatch):
    attempts = {"n": 0}

    def _flaky(schema, table, field, limit):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("connection reset")
        return ["A", "B"]

    monkeypatch.setattr(re_, "_sample_postgres", _flaky)
    assert _sample("sys-a", "wo", "cell") == []
    assert _sample("sys-a", "wo", "cell") == ["A", "B"]
    assert attempts["n"] == 2


def test_an_expired_entry_is_fetched_again(pg_counter, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(re_, "_memo_clock", lambda: clock["now"])
    _sample("sys-a", "wo", "cell")
    clock["now"] += re_.SAMPLE_MEMO_TTL_SECONDS - 1
    _sample("sys-a", "wo", "cell")
    assert len(pg_counter) == 1
    clock["now"] += 2
    _sample("sys-a", "wo", "cell")
    assert len(pg_counter) == 2


def test_reset_clears_the_memo(pg_counter):
    _sample("sys-a", "wo", "cell")
    re_._reset_sample_memo()
    _sample("sys-a", "wo", "cell")
    assert len(pg_counter) == 2


def test_memo_stats_count_hits_and_fetches(pg_counter):
    re_._reset_sample_memo()
    _sample("sys-a", "wo", "cell")
    _sample("sys-a", "wo", "cell")
    _sample("sys-a", "wo", "machine_name")
    assert re_.sample_memo_stats() == {"fetched": 2, "hits": 1}


def test_join_discovery_sweep_samples_each_field_once(pg_counter, monkeypatch):
    """Two peers, both directions, shared concepts: four identifier fields,
    four backend samples. Before the memo this sweep sampled 16 times (each
    directed pair re-sampled both sides)."""
    fields = {
        "sys-a": [
            {"table": "wo", "field": "cell", "concept": "site", "role": "identifier", "data_type": "varchar"},
            {"table": "wo", "field": "machine_name", "concept": "machine", "role": "identifier", "data_type": "varchar"},
        ],
        "sys-b": [
            {"table": "asset", "field": "site_name", "concept": "site", "role": "identifier", "data_type": "varchar"},
            {"table": "asset", "field": "machine", "concept": "machine", "role": "identifier", "data_type": "varchar"},
        ],
    }
    monkeypatch.setattr(jd, "_onboarded_systems", lambda: ["sys-a", "sys-b"])
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: list(fields.get(sid, [])))
    monkeypatch.setattr(jd, "_timestamp_fields", lambda sid: [])
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: False)
    monkeypatch.setattr(jd, "_system_sample_meta", lambda sid: ("PostgreSQL", "s1", {}))
    proposals: list[dict] = []
    monkeypatch.setattr(jd, "_log_proposed_mapping",
                        lambda **kw: proposals.append(kw) or f"m{len(proposals)}")

    jd.discover_joins_for_system("sys-a")
    # The same sweep again (the post-provision miner repeats it) adds nothing.
    jd.discover_joins_for_system("sys-a")

    assert len(pg_counter) == 4
    assert any({p["from_field"], p["to_field"]} == {"wo.cell", "asset.site_name"}
               for p in proposals if p.get("proposed_type") == "join_path")


def test_join_discovery_logs_one_timing_line_per_sweep(pg_counter, monkeypatch, caplog):
    monkeypatch.setattr(jd, "_onboarded_systems", lambda: ["sys-a", "sys-b", "sys-c"])
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: [
        {"table": "t", "field": "k", "concept": "site", "role": "identifier", "data_type": "varchar"}])
    monkeypatch.setattr(jd, "_timestamp_fields", lambda sid: [])
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: False)
    monkeypatch.setattr(jd, "_system_sample_meta", lambda sid: ("PostgreSQL", "s1", {}))
    monkeypatch.setattr(jd, "_log_proposed_mapping", lambda **kw: None)
    with caplog.at_level("INFO", logger=jd.logger.name):
        jd.discover_joins_for_system("sys-a")
    lines = [r.getMessage() for r in caplog.records if "join discovery sys-a" in r.getMessage()]
    assert len(lines) == 1
    assert "2 peer(s)" in lines[0]
    assert "samples fetched 3" in lines[0]
