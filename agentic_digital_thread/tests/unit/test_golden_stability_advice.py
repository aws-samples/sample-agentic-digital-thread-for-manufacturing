# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Late-arrival golden-authoring guard (agents/eval_core.py) — ADR-0063
track 4, extending the ADR-0062 D5 promotion contract.

Offline, stdlib-only, no fixtures from any deployment: every input here is a
generic counter block (engine neutrality — no site codes, no table names).

Pins:

* the magnitude band is deterministic across magnitudes INCLUDING the bucket
  boundaries, and refuses to invent one for an unmeasured input;
* the accrual signal fires ONLY on measured evidence and is ABSENT (no key,
  ``None``) when the inputs cannot tell — never ``False``, never "settled" by
  default;
* nothing in this path can emit a gating assertion: no ``answer_must_match``
  at any nesting, advice keys disjoint from the gate keys, and scoring a
  question with and without the advice yields IDENTICAL verdicts;
* a promoted draft carrying the advice still satisfies the existing promotion
  contract — the exported leaf set stays inside the D5 allowlist extended by
  the ONE exported key set, it leaks no identity, and it still round-trips
  through ``eval_core.load_golden_set``;
* the helpers are pure: inputs are never mutated and repeat calls are equal.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_core as eval_core  # noqa: E402
import agents.eval_promote as ep  # noqa: E402
import agents.eval_queue as eq  # noqa: E402
from agents.storage import feedback_id_for  # noqa: E402

Q_SENTINEL = "ZSTABILITYQUESTIONSENTINEL4411"

#: Identity/text leaves that must never ride an exported draft (the ADR-0062
#: list — re-pinned here because this change adds keys to the export).
FORBIDDEN_LEAVES = {
    "decided_by", "decided_at", "source_ref", "steward_note", "verdict",
    "taxonomy_tags", "user_id", "email", "session_id", "message_id",
    "answer_excerpt", "user_comment", "run_id", "ref_sha",
}


def _leaf_keys(payload) -> set:
    keys: set = set()

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(str(key))
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)

    _walk(payload)
    return keys


# ── band suggestion across magnitudes, including boundaries ─────────────────

@pytest.mark.parametrize("count,label,low,high", [
    (0, "0", 0, 1),
    (7, "7", 7, 8),
    (9, "9", 9, 10),
    (10, "10", 10, 11),          # 2-digit bucket: step 1
    (99, "99", 99, 100),
    (100, "100", 100, 110),      # 3-digit bucket: step 10
    (999, "990", 990, 1_000),    # boundary below the K unit
    (1_000, "1K", 1_000, 1_100),  # boundary AT the K unit
    (5_249, "5.2K", 5_200, 5_300),
    (999_999, "990K", 990_000, 1_000_000),
    (1_000_000, "1M", 1_000_000, 1_100_000),
    (5_222_180, "5.2M", 5_200_000, 5_300_000),
    (5_299_999, "5.2M", 5_200_000, 5_300_000),   # top of the same bucket
    (5_300_000, "5.3M", 5_300_000, 5_400_000),   # first count of the next
    (12_345_678, "12M", 12_000_000, 13_000_000),
    (999_999_999, "990M", 990_000_000, 1_000_000_000),
    (1_000_000_000, "1B", 1_000_000_000, 1_100_000_000),
    (2_500_000_000, "2.5B", 2_500_000_000, 2_600_000_000),
])
def test_band_is_deterministic_across_magnitudes(count, label, low, high):
    band = eval_core.suggest_count_band(count)
    assert band["label"] == label
    assert (band["low"], band["high"]) == (low, high)
    # Half-open bucket containing the observed count, always.
    assert band["low"] <= count < band["high"]
    assert f"{low:,}" in band["paste"] and f"{high:,}" in band["paste"]
    assert set(band) == set(eval_core.COUNT_BAND_KEYS)
    assert eval_core.suggest_count_band(count) == band  # deterministic


@pytest.mark.parametrize("bad", [None, True, False, -1, 3.5, "5222180", [5]],
                         ids=["none", "true", "false", "negative", "float",
                              "str", "list"])
def test_band_refuses_to_invent_a_magnitude(bad):
    assert eval_core.suggest_count_band(bad) is None


def test_band_unit_thresholds_are_derived_from_the_bound():
    """The unit comes from the band's own LOW bound, so label and bounds can
    never disagree (999_999 bands into the K unit, not the M unit)."""
    assert eval_core.suggest_count_band(999_999)["unit"] == "K"
    assert eval_core.suggest_count_band(1_000_000)["unit"] == "M"
    assert eval_core.suggest_count_band(999)["unit"] == ""


# ── the accrual signal: evidence-only, ABSENT when it cannot tell ───────────

@pytest.mark.parametrize("observed,expected", [
    ({"late_arrival_rows": 3}, eval_core.ACCRUAL_ACCRUING),
    ({"late_arrival_rows": 1}, eval_core.ACCRUAL_ACCRUING),
    ({"late_arrival_rows": 0}, eval_core.ACCRUAL_SETTLED),
    ({"window_closed_at": "2026-08-10T00:00:00+00:00",
      "latest_ingested_at": "2026-08-13T04:10:00+00:00"},
     eval_core.ACCRUAL_ACCRUING),
    ({"window_closed_at": "2026-08-10T00:00:00+00:00",
      "latest_ingested_at": "2026-08-09T23:59:59+00:00"},
     eval_core.ACCRUAL_SETTLED),
    ({"window_closed_at": "2026-08-10T00:00:00+00:00",
      "latest_ingested_at": "2026-08-10T00:00:00+00:00"},
     eval_core.ACCRUAL_SETTLED),
], ids=["late-rows", "one-late-row", "zero-late-rows", "ingested-after",
        "ingested-before", "ingested-exactly-at-close"])
def test_accrual_signal_fires_on_measured_evidence(observed, expected):
    assert eval_core.accrual_signal(observed) == expected


@pytest.mark.parametrize("observed", [
    {},
    {"observed_systems": 3, "observed_tool_calls": 9},   # counters, no evidence
    {"late_arrival_rows": None},
    {"late_arrival_rows": True},                          # bool is not a count
    {"late_arrival_rows": -2},
    {"late_arrival_rows": "3"},
    {"window_closed_at": "2026-08-10T00:00:00+00:00"},    # one side only
    {"latest_ingested_at": "2026-08-13T00:00:00+00:00"},
    {"window_closed_at": "last week",
     "latest_ingested_at": "2026-08-13T00:00:00+00:00"},
    {"window_closed_at": "2026-08-10T00:00:00+00:00",
     "latest_ingested_at": "2026-08-13T00:00:00"},        # naive vs aware
    {"window_closed_at": "", "latest_ingested_at": "   "},
    None,
    "not-a-dict",
], ids=["empty", "counters-only", "null", "bool", "negative", "str-count",
        "close-only", "latest-only", "unparseable", "naive-vs-aware",
        "blank", "none", "not-a-dict"])
def test_accrual_signal_is_absent_when_the_inputs_cannot_tell(observed):
    """ABSENT, not False: an unmeasured window must never read as settled."""
    assert eval_core.accrual_signal(observed) is None
    advice = eval_core.stability_advice(observed)
    assert advice == {}
    assert "accrual_signal" not in advice
    assert "expectation_stability" not in advice
    assert False not in advice.values()


# ── the advice block ────────────────────────────────────────────────────────

def test_accruing_window_advises_a_tolerant_expectation_with_a_band():
    advice = eval_core.stability_advice({
        "late_arrival_rows": 412, eval_core.OBSERVED_COUNT_KEY: 5_222_180})
    assert advice["accrual_signal"] == eval_core.ACCRUAL_ACCRUING
    assert advice["expectation_stability"] == eval_core.STABILITY_TOLERANT
    assert "magnitude band" in advice["stability_note"]
    assert "named categorical codes" in advice["stability_note"]
    assert advice["suggested_band"]["label"] == "5.2M"


def test_accruing_window_without_a_count_carries_no_band():
    """Advice about instability does not depend on a magnitude being
    measured — but the band is ABSENT rather than invented."""
    advice = eval_core.stability_advice({"late_arrival_rows": 1})
    assert advice["expectation_stability"] == eval_core.STABILITY_TOLERANT
    assert "suggested_band" not in advice


def test_settled_window_advises_exact_and_suggests_no_band():
    advice = eval_core.stability_advice({
        "late_arrival_rows": 0, eval_core.OBSERVED_COUNT_KEY: 5_222_180})
    assert advice["accrual_signal"] == eval_core.ACCRUAL_SETTLED
    assert advice["expectation_stability"] == eval_core.STABILITY_EXACT
    assert "suggested_band" not in advice
    assert "ingestion-vs-event" in advice["stability_note"]


def test_advice_keys_are_fully_declared_for_a_wiring_allowlist():
    """Every leaf the advice can emit is inside the ONE exported key set, so
    a wiring layer extends its recursive leaf allowlist by one import."""
    emitted: set = set()
    for observed in ({"late_arrival_rows": 9,
                      eval_core.OBSERVED_COUNT_KEY: 5_222_180},
                     {"late_arrival_rows": 0},
                     {"window_closed_at": "2026-08-10T00:00:00+00:00",
                      "latest_ingested_at": "2026-08-13T00:00:00+00:00",
                      eval_core.OBSERVED_COUNT_KEY: 41}):
        emitted |= _leaf_keys(eval_core.stability_advice(observed))
    assert emitted
    assert emitted <= eval_core.STABILITY_ADVICE_KEYS
    # And the export is not padded with keys nothing can emit.
    assert eval_core.STABILITY_ADVICE_KEYS == emitted


def test_helpers_are_pure_and_never_mutate_their_inputs():
    observed = {"late_arrival_rows": 2, eval_core.OBSERVED_COUNT_KEY: 900}
    hints = {"observed_systems": 2}
    snapshot = copy.deepcopy((observed, hints))
    first = eval_core.with_stability_advice(hints, observed)
    second = eval_core.with_stability_advice(hints, observed)
    assert first == second
    assert (observed, hints) == snapshot
    assert hints == {"observed_systems": 2}          # advice is a NEW block
    assert first["observed_systems"] == 2            # counters preserved
    assert first["expectation_stability"] == eval_core.STABILITY_TOLERANT


def test_with_stability_advice_reads_the_hints_block_itself_by_default():
    hints = {"observed_systems": 1, "late_arrival_rows": 5,
             eval_core.OBSERVED_COUNT_KEY: 1_240}
    merged = eval_core.with_stability_advice(hints)
    assert merged["suggested_band"]["label"] == "1.2K"
    # No evidence in the block -> the block comes back unchanged.
    assert eval_core.with_stability_advice({"observed_systems": 1}) == {
        "observed_systems": 1}
    assert eval_core.with_stability_advice(None) == {}


# ── nothing here can generate a gating assertion (ADR-0062 D5) ──────────────

def test_no_answer_must_match_is_emitted_anywhere():
    matrix = [
        {"late_arrival_rows": 7, eval_core.OBSERVED_COUNT_KEY: 5_222_180},
        {"late_arrival_rows": 0, eval_core.OBSERVED_COUNT_KEY: 12},
        {"window_closed_at": "2026-08-01T00:00:00Z",
         "latest_ingested_at": "2026-08-06T00:00:00Z",
         eval_core.OBSERVED_COUNT_KEY: 999_999},
        {},
    ]
    for observed in matrix:
        advice = eval_core.stability_advice(observed)
        merged = eval_core.with_stability_advice({"observed_systems": 1},
                                                observed)
        for payload in (advice, merged,
                        eval_core.suggest_count_band(
                            observed.get(eval_core.OBSERVED_COUNT_KEY))):
            leaves = _leaf_keys(payload)
            assert ep.FORBIDDEN_EXPECT_KEY not in leaves
            assert "expect" not in leaves
            assert not (FORBIDDEN_LEAVES & leaves)


def test_advice_keys_never_collide_with_gate_keys():
    gate_keys = set(ep.DEFAULT_EXPECT) | {
        ep.FORBIDDEN_EXPECT_KEY, "must_not_quantify", "answer_must_not_match",
        "absence_honesty", "injection_guard"}
    assert not (eval_core.STABILITY_ADVICE_KEYS & gate_keys)
    assert not (eval_core.STABILITY_ADVICE_KEYS & set(ep.HINT_KEYS))


# ── a promoted draft carrying the advice still honors the D5 contract ───────

def _ref() -> str:
    return eq.feedback_source_ref(feedback_id_for("sess-stability", "msg-1"))


def _decision() -> dict:
    return {"source_ref": _ref(), "source": eq.SOURCE_FEEDBACK,
            "ref_sha": eq.ref_sha(_ref()), "verdict": "incorrect",
            "decided_by": "steward@example.com"}


def _turn() -> dict:
    return {"source_ref": _ref(), "ref_sha": eq.ref_sha(_ref()),
            "source": eq.SOURCE_FEEDBACK,
            "question": f"how many rows landed for {Q_SENTINEL} last week?",
            "cited_systems": ["system-a", "system-b"],
            "intelligence_summary": {"system_queries": 2,
                                     "neptune_queries": 1,
                                     "total_tool_calls": 5}}


def _advised_draft() -> dict:
    draft = ep.draft_golden(_decision(), _turn())
    draft["hints"] = eval_core.with_stability_advice(
        draft["hints"],
        {"late_arrival_rows": 118, eval_core.OBSERVED_COUNT_KEY: 5_222_180})
    return draft


def test_advised_draft_stays_inside_the_extended_leaf_allowlist():
    draft = _advised_draft()
    allowed = frozenset(ep.ALLOWED_DRAFT_LEAVES) | eval_core.STABILITY_ADVICE_KEYS
    assert _leaf_keys(draft) <= allowed
    assert set(draft) == set(ep.DRAFT_TOP_KEYS)          # no new top-level key
    assert draft["expect"] == ep.DEFAULT_EXPECT          # gates untouched
    body = json.dumps(draft)
    assert "steward@example.com" not in body and _ref() not in body


def test_advised_draft_round_trips_through_the_golden_loader(tmp_path):
    draft = _advised_draft()
    bundle = ep.draft_bundle(draft)
    path = tmp_path / "external_golden.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    questions = eval_core.load_golden_set(str(path))
    assert len(questions) == 1
    assert questions[0]["hints"]["suggested_band"]["label"] == "5.2M"
    assert questions[0]["hints"]["observed_systems"] == 2


def test_the_advice_cannot_change_a_verdict():
    """Non-gating, computed: scoring reads ``expect`` only, so adding the
    advice to ``hints`` cannot flip a verdict, a failure list or a flag."""
    draft = _advised_draft()
    bare = {key: value for key, value in draft.items() if key != "hints"}
    answer = "About 5.2 million rows landed in the window."
    summary = {"system_queries": 2, "neptune_queries": 1, "rules_applied": []}
    with_advice = eval_core.score_question(
        draft["expect"], summary, answer, [], question_text=draft["text"])
    without = eval_core.score_question(
        bare["expect"], summary, answer, [], question_text=bare["text"])
    assert with_advice == without == ("pass", [])
    assert eval_core.fabrication_flags(draft["expect"], draft["text"],
                                       answer) == []


def test_draft_build_itself_is_unchanged_by_this_track():
    """The builder is NOT wired to the advice here: an unadvised draft is
    byte-identical to what the ADR-0062 contract tests pin."""
    draft = ep.draft_golden(_decision(), _turn())
    assert set(draft["hints"]) <= set(ep.HINT_KEYS)
    assert not (eval_core.STABILITY_ADVICE_KEYS & set(_leaf_keys(draft)))
