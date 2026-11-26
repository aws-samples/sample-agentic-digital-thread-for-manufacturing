# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The per-system join sweep runs each probe ONCE (event 28, 2026-10-06).

``discover_joins_for_system`` runs ``discover_pair(new, peer)`` AND
``discover_pair(peer, new)`` so a join is found whichever side was onboarded
first. On the event 28 account the phase-2 resolve of one MES spent 214 s in
1390 probes and 97 s in 327 graph reads, half of it the second direction
repeating the first. The sweep now carries one memo over that IO: each
containment probe and each system's field lists are read once per sweep.

The pure transform ladder still runs for each direction, because it is
order-sensitive: the affix rungs try the first
argument's affix first and a degenerate rung aborts the whole ladder, so the
two directions can win on different transforms, or one can abort where the
other clears. Each direction proposes exactly what ``discover_pair`` alone
proposes.
"""
from agents import join_discovery as jd


def _uuids(n, salt=""):
    return [f"{salt}{i:08x}-0000-4000-8000-000000000000" for i in range(n)]


def _wire(monkeypatch, fields, samples, universe):
    reads, probes, emitted = [], [], []

    def _identifier_fields(sid):
        reads.append(("fields", sid))
        return fields[sid]

    def _timestamp_fields(sid):
        reads.append(("time", sid))
        return []

    def _probe(sid, t, f, values):
        probes.append((sid, t, f))
        return [v for v in values if v in universe[sid]]

    monkeypatch.setattr(jd, "_onboarded_systems", lambda: ["erp", "mes"])
    monkeypatch.setattr(jd, "_identifier_fields", _identifier_fields)
    monkeypatch.setattr(jd, "_timestamp_fields", _timestamp_fields)
    monkeypatch.setattr(jd, "_sample", lambda sid, t, f: samples[(sid, t, f)])
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: False)
    monkeypatch.setattr(jd, "_probe", _probe)
    monkeypatch.setattr(jd, "_log_proposed_mapping", lambda **kw: emitted.append(kw) or f"id-{len(emitted)}")
    return reads, probes, emitted


def _sparse_fk(monkeypatch):
    parent, child = _uuids(400), _uuids(400)[350:] + _uuids(600)[450:]
    fields = {
        "mes": [{"table": "work_orders", "field": "work_order_id", "concept": "", "role": "", "data_type": "uuid"}],
        "erp": [{"table": "lot_mes_tracking", "field": "work_order_id", "concept": "", "role": "", "data_type": "uuid"}],
    }
    samples = {("mes", "work_orders", "work_order_id"): parent[:200],
               ("erp", "lot_mes_tracking", "work_order_id"): child}
    return _wire(monkeypatch, fields, samples, {"mes": set(parent), "erp": set(child)})


def test_sweep_probes_each_unordered_pair_once_and_still_emits_both_directions(monkeypatch):
    reads, probes, emitted = _sparse_fk(monkeypatch)
    out = jd.discover_joins_for_system("mes")
    assert out["joins"] == ["id-1", "id-2"]                 # both directions still propose
    assert {(e["from_system"], e["to_system"]) for e in emitted} == {("mes", "erp"), ("erp", "mes")}
    # The forward direction finds the child's sample in the parent at once; the
    # reverse tries the parent's sample in the child (a miss) and then reuses
    # the forward probe instead of repeating it: two probes, against three
    # for two separate discover_pair calls.
    assert probes == [("mes", "work_orders", "work_order_id"), ("erp", "lot_mes_tracking", "work_order_id")]
    assert sorted(reads) == [("fields", "erp"), ("fields", "mes"), ("time", "erp"), ("time", "mes")]


def test_discover_pair_alone_is_unchanged(monkeypatch):
    """Without a sweep memo each call measures for itself (the existing contract)."""
    _, probes, emitted = _sparse_fk(monkeypatch)
    jd.discover_pair("mes", "erp")
    jd.discover_pair("erp", "mes")
    # Three probes: the forward direction finds the child in the parent at once;
    # the reverse direction tries the parent's sample in the child first (a miss)
    # and then repeats the forward probe. That echo is what the sweep memo removes.
    assert len(emitted) == 2 and len(probes) == 3


def test_memo_probe_is_keyed_on_the_system_field_and_exact_sample(monkeypatch):
    calls = []
    monkeypatch.setattr(jd, "_probe", lambda sid, t, f, vals: calls.append((sid, t, f, tuple(vals))) or [])
    memo = jd._SweepMemo()
    memo.probe("s1", "t", "a", ["x", "y"])
    memo.probe("s1", "t", "a", ["x", "y"])      # the same probe: served from the memo
    memo.probe("s1", "t", "a", ["x", "z"])      # another sample: probed
    memo.probe("s2", "t", "a", ["x", "y"])      # another system: probed
    assert len(calls) == 3


def _ladder_only(monkeypatch, mes_vals, erp_vals):
    """Two fields the containment probe never touches (different names, no concept)."""
    fields = {
        "mes": [{"table": "runs", "field": "run_id", "concept": "", "role": "identifier", "data_type": "varchar"}],
        "erp": [{"table": "lots", "field": "lot_id", "concept": "", "role": "identifier", "data_type": "varchar"}],
    }
    samples = {("mes", "runs", "run_id"): mes_vals, ("erp", "lots", "lot_id"): erp_vals}
    return _wire(monkeypatch, fields, samples, {"mes": set(), "erp": set()})


def _emits(emitted):
    return sorted((e["from_system"], e["to_system"], e["transform"], e["query_context"]) for e in emitted)


def _sweep_matches_two_direct_passes(monkeypatch, mes_vals, erp_vals):
    _, _, swept = _ladder_only(monkeypatch, mes_vals, erp_vals)
    jd.discover_joins_for_system("mes")
    _, _, direct = _ladder_only(monkeypatch, mes_vals, erp_vals)
    jd.discover_pair("mes", "erp")
    jd.discover_pair("erp", "mes")
    assert _emits(swept) == _emits(direct)
    return {(e["from_system"], e["to_system"]): e["transform"] for e in swept}


def test_each_direction_keeps_its_own_winning_transform(monkeypatch):
    """Two equally valid prefixes: each order wins on its own first side's affix."""
    mes = [f"X-a{i:02d}" for i in range(5)] + [f"b{i:02d}" for i in range(5)]
    erp = [f"a{i:02d}" for i in range(5)] + [f"Y-b{i:02d}" for i in range(5)]
    assert _sweep_matches_two_direct_passes(monkeypatch, mes, erp) == {
        ("mes", "erp"): "strip_prefix:X-", ("erp", "mes"): "strip_prefix:Y-"}


def test_a_direction_that_aborts_does_not_silence_the_other(monkeypatch):
    """The first side's affix lands on short sequence numbers (degenerate: the
    whole ladder declines); the other order tries the second side's affix first
    and clears. A verdict shared across directions would drop the reverse join."""
    mes = [f"X-{i:02d}" for i in range(1, 6)] + [f"CORE{i:04d}" for i in range(5)]
    erp = [f"{i:02d}" for i in range(1, 6)] + [f"Y-CORE{i:04d}" for i in range(5)]
    assert jd.discover_transform(mes, erp) is None
    assert _sweep_matches_two_direct_passes(monkeypatch, mes, erp) == {("erp", "mes"): "strip_prefix:Y-"}


def test_a_pair_that_cleared_nothing_is_not_re_probed_in_the_other_direction(monkeypatch):
    """Both containment probes ran in the first direction; the reverse reuses both."""
    parent, other = _uuids(100), _uuids(100, salt="f")
    fields = {
        "mes": [{"table": "work_orders", "field": "work_order_id", "concept": "", "role": "", "data_type": "uuid"}],
        "erp": [{"table": "lot_mes_tracking", "field": "work_order_id", "concept": "", "role": "", "data_type": "uuid"}],
    }
    samples = {("mes", "work_orders", "work_order_id"): parent,
               ("erp", "lot_mes_tracking", "work_order_id"): other}
    _, probes, emitted = _wire(monkeypatch, fields, samples, {"mes": set(parent), "erp": set(other)})
    jd.discover_joins_for_system("mes")
    assert emitted == []
    assert len(probes) == 2  # one containment attempt (both sides) for the whole sweep, not four
