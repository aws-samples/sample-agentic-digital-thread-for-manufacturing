# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3.1 T-A — the question bank and the materiality selection rule.

The selection rule is the part worth pinning hard: it decides what a steward is
asked, and the failure modes are asymmetric. Asking an unanswerable question
wastes trust; skipping a material map-time question costs a re-onboard later.
"""
import re

import pytest

from agents.context_interview import questions as qb


# --------------------------------------------------------------------------- #
# Bank shape
# --------------------------------------------------------------------------- #
def test_every_intake_topic_is_represented():
    # Eleven question TOPICS implement the ten intake questions: unit-of-work
    # contributes both the positive and the negative-vocabulary halves. The two
    # 2026-07 additions came from the first customer's misses: equipment-naming
    # (floor names absent from the data) and enum-semantics (coded values whose
    # meaning lives outside the schema). kpi-definitions (2026-08) came from a
    # KPI computed with the wrong denominator — the formula lives in people's
    # heads, never in the schema.
    topics = {q.topic for q in qb.QUESTIONS}
    assert topics == {
        "unit-of-work", "negative-vocabulary", "authority", "retry-semantics",
        "failure-taxonomy", "identifier-family", "coverage-boundary",
        "query-strategy", "equipment-naming", "enum-semantics",
        "kpi-definitions",
    }


def test_ids_unique_and_indexed():
    ids = [q.id for q in qb.QUESTIONS]
    assert len(ids) == len(set(ids))
    assert set(qb.QUESTIONS_BY_ID) == set(ids)


def test_every_system_question_has_a_registered_trigger():
    for q in qb.QUESTIONS:
        if q.scope == qb.SCOPE_SYSTEM:
            assert q.trigger in qb.TRIGGERS, q.id


def test_deployment_questions_have_no_trigger():
    for q in qb.QUESTIONS:
        if q.scope == qb.SCOPE_DEPLOYMENT:
            assert q.trigger is None, q.id


def test_phases_are_from_the_pinned_set():
    for q in qb.QUESTIONS:
        assert q.treatment_phase in (qb.PHASE_MAP_TIME, qb.PHASE_QUERY_TIME)


def test_module_is_import_pure():
    """Importing must not pull in a cloud client or need credentials.

    Checked in a SUBPROCESS: asserting against ``sys.modules`` in-process is
    vacuous once any sibling test has imported boto3.
    """
    import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
    import sys

    code = (
        "import sys;"
        "import agents.context_interview.questions;"
        "bad=[m for m in ('boto3','pg8000','agents.pgdriver','botocore') if m in sys.modules];"
        "print('LEAKED' if bad else 'CLEAN')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,  # nosec B603 - fixed argv list, no shell, internal inputs only
                         text=True, cwd=".", check=False)
    assert "CLEAN" in out.stdout, out.stdout + out.stderr


# --------------------------------------------------------------------------- #
# Neutrality — these strings reach a steward's screen (ADR-0044)
# --------------------------------------------------------------------------- #
# The customer-identity needles are NOT restated here: the R3 ratchet
# (test_engine_domain_neutrality.DENYLIST) is the ONE sanctioned code home
# for those terms; this gate composes them in so both gates share one list.
# (Loaded by file path — the tests/unit modules are not importable as a
# package.)
import importlib.util as _ilu
from pathlib import Path as _Path

_spec = _ilu.spec_from_file_location(
    "_engine_domain_neutrality_denylist",
    _Path(__file__).with_name("test_engine_domain_neutrality.py"))
_ratchet = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_ratchet)

_CUSTOMER_PATTERNS = "|".join(
    _ratchet.DENYLIST[cls].removeprefix("(?i)")
    for cls in _ratchet.DENYLIST if cls.startswith("customer-")
)

_DENY = re.compile(
    r"\bMKE\b|\bDET\b|\bCRR?\b|milwaukee|detroit|cedar\s*rapids|indianapolis"
    r"|\bmes_\w+\.\w+|MILWAUKEE_BOTTLING|Bottling-L\d|AnyCompany"
    r"|" + _CUSTOMER_PATTERNS +
    r"|\b(washer|filler|capper|labeler|checkweigher|case_packer|palletizer)\b",
    re.I)


@pytest.mark.parametrize("q", list(qb.QUESTIONS), ids=[q.id for q in qb.QUESTIONS])
def test_question_text_is_domain_neutral(q):
    blob = " ".join([q.prompt, *q.examples])
    hit = _DENY.search(blob)
    assert hit is None, f"{q.id} leaks a domain literal: {hit.group(0)!r}"


@pytest.mark.parametrize(
    "shape,q", sorted(qb.RUNTIME_QUESTION_TEMPLATES.items()),
    ids=sorted(qb.RUNTIME_QUESTION_TEMPLATES))
def test_runtime_template_text_is_domain_neutral(shape, q):
    """ADR-0059 runtime templates reach the same steward surface — templates
    must be placeholder-shaped; subject facts arrive only at runtime."""
    blob = " ".join([q.prompt, q.goal, *q.examples])
    hit = _DENY.search(blob)
    assert hit is None, f"{shape} leaks a domain literal: {hit.group(0)!r}"


def test_runtime_fallback_composition_is_domain_neutral():
    """The composer's own contribution (neutral fallbacks, id digest) must not
    introduce a literal either — checked on the fully composed prompt."""
    for shape in qb.RUNTIME_QUESTION_TEMPLATES:
        q = qb.runtime_question(shape, {})
        hit = _DENY.search(" ".join([q.id, q.prompt]))
        assert hit is None, f"{shape} composes a domain literal: {hit.group(0)!r}"


# --------------------------------------------------------------------------- #
# Triggers — evidence, not guesses
# --------------------------------------------------------------------------- #
def test_retry_trigger_needs_both_state_and_attempt():
    only_state = {"columns": [{"name": "state"}]}
    assert qb.trig_state_and_attempt_columns(only_state) is None
    both = {"columns": [{"name": "state"}, {"name": "attempt_id"}]}
    ev = qb.trig_state_and_attempt_columns(both)
    assert ev and "state" in ev and "attempt_id" in ev


def test_failure_trigger_matches_common_namings():
    for name in ("failure_code", "error_reason", "fault_type", "reject_reason"):
        assert qb.trig_failure_code_column({"columns": [{"name": name}]})


def test_identifier_trigger_fires_on_structured_values_or_two_id_columns():
    structured = {"columns": [{"name": "equipment", "sample_values": ["A_B_01"]}]}
    assert qb.trig_structured_identifier_values(structured)
    two_ids = {"columns": [{"name": "asset_id"}, {"name": "order_number"}]}
    assert qb.trig_structured_identifier_values(two_ids)
    neither = {"columns": [{"name": "quantity"}]}
    assert qb.trig_structured_identifier_values(neither) is None


def test_coverage_trigger_needs_an_all_null_column():
    assert qb.trig_always_null_column({"columns": [{"name": "aoi", "all_null": True}]})
    assert qb.trig_always_null_column({"columns": [{"name": "aoi", "all_null": False}]}) is None


def test_large_table_trigger_is_threshold_pinned():
    assert qb.trig_large_table({"row_estimate": qb.LARGE_TABLE_ROWS}) is not None
    assert qb.trig_large_table({"row_estimate": qb.LARGE_TABLE_ROWS - 1}) is None
    assert qb.trig_large_table({}) is None


def test_triggers_tolerate_malformed_facts():
    """Discover output is external data — a trigger must never explode on it."""
    for facts in ({}, {"columns": None}, {"columns": ["oops"]},
                  {"columns": [{}]}, {"row_estimate": "many"}):
        for fn in qb.TRIGGERS.values():
            fn(facts)  # must not raise


# --------------------------------------------------------------------------- #
# Selection: answerable ∧ material, ranked, capped
# --------------------------------------------------------------------------- #
_RICH = {
    "columns": [
        {"name": "state"}, {"name": "attempt_id"}, {"name": "failure_code"},
        {"name": "asset_id"}, {"name": "order_number"},
        {"name": "aoi_result", "all_null": True},
    ],
    "row_estimate": 5_000_000,
}


def test_unanswerable_questions_are_not_asked():
    picked = qb.select_questions({"columns": [{"name": "quantity"}]})
    assert picked == []


def test_exact_askable_set_is_pinned():
    """Companion to the cap test. Without an exact pin, a regression asking 1
    question instead of 4 passes. equipment-naming (query-time) fills the 4th
    cap slot on this schema: asset_id names equipment."""
    assert [s.question.id for s in qb.select_questions(_RICH)] == [
        "failure-taxonomy", "identifier-family", "retry-semantics",
        "equipment-naming",
    ]


def test_equipment_trigger_fires_on_equipment_ish_names():
    assert qb.trig_equipment_name_columns(
        {"columns": [{"name": "machine_name"}]})
    assert qb.trig_equipment_name_columns(
        {"columns": [{"name": "resource.equipment.id"}]})  # dotted path segment
    assert qb.trig_equipment_name_columns(
        {"columns": [{"name": "quantity"}, {"name": "created_at"}]}) is None


def test_enum_trigger_needs_name_hint_and_coded_samples():
    coded = {"columns": [{"name": "defect_type",
                          "sample_values": ["StationName: CodeValueHere"]}]}
    ev = qb.trig_enum_coded_column(coded)
    assert ev and "defect_type" in ev and "StationName: CodeValueHere" in ev
    # A name hint without coded samples must NOT fire (free text is not an enum).
    assert qb.trig_enum_coded_column(
        {"columns": [{"name": "defect_type",
                      "sample_values": ["the operator noticed a wrinkle"]}]}) is None
    # Coded samples without a name hint must NOT fire either.
    assert qb.trig_enum_coded_column(
        {"columns": [{"name": "notes",
                      "sample_values": ["UPPER_SNAKE_CODE"]}]}) is None


def test_enum_trigger_recognizes_all_three_code_shapes():
    for sample in ("CamelCaseCode", "UPPER_SNAKE_CODE", "Family: CodeValue"):
        facts = {"columns": [{"name": "status", "sample_values": [sample]}]}
        assert qb.trig_enum_coded_column(facts), sample


def test_every_askable_question_carries_a_goal():
    """The clarifier is anchored on the goal; an asked question without one can
    never be clarified, silently. Derived questions are exempt (never asked)."""
    for q in qb.QUESTIONS:
        if q.deriver is None:
            assert q.goal, q.id


def test_asking_never_exceeds_the_cap():
    """The cap bounds what a STEWARD is asked. Derivable topics are excluded from
    asking entirely (see derive_answers), so a rich schema now asks FEWER than the
    cap — which is the point: only questions needing human intent survive."""
    picked = qb.select_questions(_RICH)
    assert 0 < len(picked) <= qb.MAX_QUESTIONS_PER_SYSTEM
    assert all(s.question.deriver is None for s in picked)


def test_map_time_questions_win_the_cap():
    """The cap must not spend itself on query-time questions — those stay cheap
    to add later, map-time ones do not."""
    picked = qb.select_questions(_RICH)
    phases = [s.question.treatment_phase for s in picked]
    assert phases[0] == qb.PHASE_MAP_TIME
    assert phases == sorted(phases, key=lambda p: 0 if p == qb.PHASE_MAP_TIME else 1)
    assert all(p == qb.PHASE_MAP_TIME for p in phases[:3])


def test_selection_is_deterministic():
    assert qb.select_questions(_RICH) == qb.select_questions(_RICH)


def test_already_answered_topics_are_not_re_asked():
    picked = qb.select_questions(_RICH, {"retry-semantics", "failure-taxonomy"})
    assert "retry-semantics" not in {s.question.topic for s in picked}
    assert "failure-taxonomy" not in {s.question.topic for s in picked}


def test_every_selected_question_carries_its_evidence():
    for s in qb.select_questions(_RICH):
        assert s.evidence, s.question.id


def test_limit_zero_asks_nothing():
    assert qb.select_questions(_RICH, limit=0) == []


def test_deployment_questions_are_scoped_and_ordered():
    picked = qb.deployment_questions()
    assert {s.question.scope for s in picked} == {qb.SCOPE_DEPLOYMENT}
    assert picked[0].question.treatment_phase == qb.PHASE_MAP_TIME


def test_deployment_questions_drop_answered_topics():
    picked = qb.deployment_questions({"unit-of-work"})
    assert "unit-of-work" not in {s.question.topic for s in picked}


# --------------------------------------------------------------------------- #
# Derive vs ask — what makes fully automated onboarding produce real context
# --------------------------------------------------------------------------- #
_DERIVABLE = {"columns": [{"name": "aoi_result", "all_null": True},
                          {"name": "state"}, {"name": "attempt_id"}],
              "row_estimate": 5_000_000}


def test_derivable_questions_are_never_asked():
    """Asking a steward something the schema already answers wastes the trust we
    need for the questions only they can answer."""
    asked = {s.question.topic for s in qb.select_questions(_DERIVABLE)}
    derived = {d.question.topic for d in qb.derive_answers(_DERIVABLE)}
    assert derived, "expected derivable answers for this schema"
    assert not (asked & derived)


def test_derivation_covers_coverage_and_query_strategy():
    topics = {d.question.topic for d in qb.derive_answers(_DERIVABLE)}
    assert topics == {"coverage-boundary", "query-strategy"}


def test_human_only_questions_are_not_derivable():
    """These encode INTENT that is not in the schema — never fabricate them."""
    for topic in ("unit-of-work", "negative-vocabulary", "retry-semantics",
                  "failure-taxonomy"):
        q = next(x for x in qb.QUESTIONS if x.topic == topic)
        assert q.deriver is None, topic


def test_derivation_states_observations_not_interpretations():
    text = {d.question.topic: d.text for d in qb.derive_answers(_DERIVABLE)}
    assert "aoi_result" in text["coverage-boundary"]
    assert "unavailable" in text["coverage-boundary"]
    assert "aggregate" in text["query-strategy"]


def test_derivation_is_empty_when_the_schema_says_nothing():
    assert qb.derive_answers({"columns": [{"name": "qty"}]}) == []


def test_derivation_is_deterministic_and_skips_answered_topics():
    assert qb.derive_answers(_DERIVABLE) == qb.derive_answers(_DERIVABLE)
    left = qb.derive_answers(_DERIVABLE, {"coverage-boundary"})
    assert {d.question.topic for d in left} == {"query-strategy"}


def test_derivers_tolerate_malformed_facts():
    for facts in ({}, {"columns": None}, {"columns": [{}]}, {"row_estimate": "lots"}):
        qb.derive_answers(facts)


def test_derived_text_is_domain_neutral():
    for d in qb.derive_answers(_DERIVABLE):
        assert _DENY.search(d.text) is None, d.question.id


# --------------------------------------------------------------------------- #
# The PRODUCER — pinned against the REAL discover output shape.
#
# The lesson: the rules were correct but unreachable because
# nothing produced their input. These fixtures mirror what discover actually
# emits (name / columns[{name,data_type}] / row_count / sample_values{col:[...]})
# so the consumer can never silently drift from the producer again.
# --------------------------------------------------------------------------- #
_DISCOVER_TABLE = {
    "name": "orders",
    "columns": [{"name": "state", "data_type": "string"},
                {"name": "attempt_id", "data_type": "string"}],
    "row_count": 5_000_000,
    "sample_values": {"state": ["COMPLETED", "FAILED"]},
}


def test_producer_maps_real_discover_output():
    facts = qb.schema_facts_from_table(_DISCOVER_TABLE)
    assert facts["row_estimate"] == 5_000_000
    names = [c["name"] for c in facts["columns"]]
    assert names == ["state", "attempt_id"]
    assert facts["columns"][0]["sample_values"] == ["COMPLETED", "FAILED"]


def test_producer_never_synthesizes_all_null():
    """An absent sample list is NOT evidence a column is empty — inventing that
    flag would make the agent deny data that exists."""
    facts = qb.schema_facts_from_table(_DISCOVER_TABLE)
    assert all("all_null" not in c for c in facts["columns"])


def test_derivation_is_reachable_from_real_discover_output():
    """The reachability defect: derivation must fire on what discover emits."""
    facts = qb.schema_facts_from_tables([_DISCOVER_TABLE])
    assert [d.question.id for d in qb.derive_answers(facts)] == ["query-strategy"]


def test_asking_is_reachable_from_real_discover_output():
    facts = qb.schema_facts_from_tables([_DISCOVER_TABLE])
    assert [s.question.id for s in qb.select_questions(facts)] == ["retry-semantics"]


def test_multi_table_merge_takes_the_largest_row_estimate():
    small = {"name": "s", "columns": [{"name": "note"}], "row_count": 10}
    facts = qb.schema_facts_from_tables([small, _DISCOVER_TABLE])
    assert facts["row_estimate"] == 5_000_000
    assert {c["name"] for c in facts["columns"]} == {"note", "state", "attempt_id"}


def test_producer_tolerates_garbage():
    for bad in (None, "nope", {}, {"columns": "x"}, {"columns": [None, {}]},
                {"row_count": "many"}):
        qb.schema_facts_from_table(bad)
    qb.schema_facts_from_tables(None)
    qb.schema_facts_from_tables(["junk"])


def test_producer_caps_tables_columns_and_samples():
    """Client-supplied tables are untrusted — the producer must bound its work.

    Kills each cap constant individually:
    * MAX_FACT_TABLES — the huge row_count sits on a table BEYOND the cap, so it
      must not become row_estimate; uncapped iteration would take it.
    * MAX_FACT_COLUMNS — 200 read tables x 12 distinct columns = 2400 > 2000;
      uncapped merging would keep them all.
    * MAX_SAMPLE_VALUES — every column carries 20 samples; uncapped copying
      would keep 20.
    """
    n = qb.MAX_FACT_TABLES + 5
    tables = [{
        "name": f"t{i}",
        "columns": [{"name": f"t{i}_col{j}"} for j in range(12)],
        "row_count": 10 if i < qb.MAX_FACT_TABLES else 9_999_999_999,
        "sample_values": {f"t{i}_col{j}": [f"v{k}" for k in range(20)]
                          for j in range(12)},
    } for i in range(n)]
    facts = qb.schema_facts_from_tables(tables)
    assert len(facts["columns"]) <= qb.MAX_FACT_COLUMNS
    assert all(len(c["sample_values"]) <= qb.MAX_SAMPLE_VALUES
               for c in facts["columns"])
    # The huge estimate lives only on tables past the cap: it must be ignored.
    assert facts["row_estimate"] == 10


@pytest.mark.parametrize("rows", [float("inf"), float("-inf"), float("nan"), -5, True])
def test_non_finite_row_estimates_never_raise(rows):
    """JSON accepts Infinity/NaN and int(inf) raises — this reached the endpoint
    as a 500 for any authenticated caller."""
    assert qb.trig_large_table({"row_estimate": rows}) is None
    assert qb.derive_answers({"row_estimate": rows}) == []


def test_coverage_wording_does_not_claim_permanence():
    facts = {"columns": [{"name": "aoi", "all_null": True}]}
    text = qb.derive_answers(facts)[0].text
    assert "never populated" not in text
    assert "at last check" in text.lower()


@pytest.mark.parametrize("rows", [float("inf"), float("-inf"), float("nan"),
                                 -1, True, "many", None])
def test_deriver_guards_itself_not_just_via_its_trigger(rows):
    """Direct call, bypassing derive_answers' trigger gating.

    derive_answers only ever reaches a deriver through its trigger, so a missing
    guard here is invisible end-to-end — which is exactly how it shipped
    unguarded once. int(float("inf")) raises, so the deriver owns its own
    validation rather than inheriting it.
    """
    assert qb.derive_query_strategy({"row_estimate": rows}) is None


def test_deriver_fires_on_a_finite_estimate_at_the_threshold():
    """Companion so the guard above cannot be satisfied by returning None always."""
    text = qb.derive_query_strategy({"row_estimate": qb.LARGE_TABLE_ROWS})
    assert text and "aggregate" in text


# --------------------------------------------------------------------------- #
# kpi-definitions (2026-08) — the formula, especially the DENOMINATOR.
#
# Provenance: a live rate-style KPI answer used defect rows as the basis
# instead of units produced and reported a rate above its theoretical maximum
# (the agent even flagged its own number as suspicious, then reported it
# anyway). The numerator is usually visible in the schema; the denominator
# basis and the exclusions are business decisions that live only in people's
# heads — so this is a HUMAN question, never derived.
# --------------------------------------------------------------------------- #
def _kpi_question():
    return next(q for q in qb.QUESTIONS if q.topic == "kpi-definitions")


def test_kpi_question_is_deployment_scoped_and_never_derived():
    q = _kpi_question()
    assert q.scope == qb.SCOPE_DEPLOYMENT
    assert q.trigger is None       # deployment questions carry no trigger
    assert q.deriver is None       # a formula cannot be read off a schema
    assert q.goal                  # clarifier-eligible


def test_kpi_question_lands_on_business_context():
    """The stated formula belongs in the prompt-visible context, and a BC
    landing is a DRAFT — batched into one steward-confirmed runtime roll."""
    assert _kpi_question().landing == qb.LAND_BUSINESS_CONTEXT


def test_kpi_question_asks_for_the_denominator_and_exclusions():
    """The whole point of the question. A prompt that only says 'how is it
    calculated' reproduces the miss it was added for."""
    q = _kpi_question()
    blob = f"{q.prompt} {q.goal}".lower()
    assert "denominator" in blob
    assert "numerator" in blob
    assert "exclu" in blob        # exclusions / excluded


def test_kpi_question_is_offered_at_deployment_scope():
    ids = [s.question.id for s in qb.deployment_questions()]
    assert "kpi-definitions" in ids


def test_kpi_question_drops_once_answered():
    picked = qb.deployment_questions({"kpi-definitions"})
    assert "kpi-definitions" not in {s.question.topic for s in picked}


def test_kpi_question_never_consumes_a_per_system_slot():
    """Deployment-scoped: it must not compete with the per-system cap."""
    assert "kpi-definitions" not in {
        s.question.topic for s in qb.select_questions(_RICH)}
