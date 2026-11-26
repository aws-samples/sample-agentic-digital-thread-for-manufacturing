# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guards for how the map prompts describe production COUNTS.

A metric binds one column per system (ADR-0076): a concept mapped to two
columns on one system is withheld until a steward picks one. Three confusions
in the concept text produced exactly that on every seeded MES:

* ``actual-quantity`` (a record's own total output) also took a quantity
  logged on an event row, a counter reading that repeats the record's count;
* ``quality-metric`` listed bad/reject counts and a check's pass tally as its
  own fields, so good counts, bad counts and inspection tallies all landed on it;
* nothing said a sampled check's pass/fail tallies belong to ``inspection``.

The wizard's map prompt (the path the seed onboarding uses) shows each concept
through :func:`catalog.concepts_prompt_block`, which truncates a description to
140 characters, so each rule has to survive that cut. Both prompts also carry
:data:`catalog.MAPPING_DISAMBIGUATION` in full. Pure text: no AWS, graph or LLM.
"""
from __future__ import annotations

import re

import pytest

from agents import catalog
from agents.catalog import CORE_CONCEPT_DEFINITIONS, MAPPING_DISAMBIGUATION

COUNTED_OUTPUT_CONCEPTS = ("actual-quantity", "quality-metric", "scrap-waste", "inspection",
                           "throughput")


@pytest.fixture
def wizard_lines(monkeypatch) -> dict[str, str]:
    """concept id -> the line the wizard's map prompt shows (graph unavailable,
    so the curated core text is what renders)."""
    import agents.db as db

    def _offline(*_a, **_k):
        raise RuntimeError("graph offline in unit tests")

    monkeypatch.setattr(db, "neptune_query", _offline)
    out = {}
    for line in catalog.concepts_prompt_block().splitlines():
        m = re.match(r"- ([a-z0-9-]+): (.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _fields(concept: str) -> set[str]:
    """The example field names a description lists after 'Fields:'."""
    m = re.search(r"Fields: ([^.]*)\.", CORE_CONCEPT_DEFINITIONS[concept])
    assert m, f"{concept} lists no Fields:"
    return {f.strip() for f in m.group(1).split(",") if f.strip()}


def test_wizard_view_of_actual_quantity_rules_out_event_row_quantities(wizard_lines):
    line = wizard_lines["actual-quantity"].lower()
    assert "event" in line, (
        "the 140-char view of actual-quantity must say a quantity on an event row "
        f"isn't a record total: {line!r}")


def test_wizard_view_of_quality_metric_lists_no_bad_counts_or_check_tallies(wizard_lines):
    line = wizard_lines["quality-metric"]
    for bad in ("bad_qty", "bad_quantity", "reject_qty", "pass_count", "fail_count"):
        assert bad not in line, f"quality-metric still offers {bad}: {line!r}"
    # The exclusions themselves are visible in the cut view.
    assert "scrap-waste" in line and "inspection" in line, line


def test_wizard_view_of_inspection_claims_the_check_tallies(wizard_lines):
    line = wizard_lines["inspection"].lower()
    assert "passed" in line and "failed" in line, line


def test_counted_output_concepts_list_disjoint_example_fields():
    """One example field name must never be offered for two counted-output
    concepts, or a system holding it maps it twice."""
    seen: dict[str, str] = {}
    for concept in COUNTED_OUTPUT_CONCEPTS:
        for field in _fields(concept):
            assert field not in seen, (
                f"{field!r} is listed under both {seen[field]} and {concept}")
            seen[field] = concept


def test_bad_unit_counts_are_offered_to_scrap_waste():
    assert {"bad_qty", "bad_quantity", "reject_qty"} <= _fields("scrap-waste")


def test_disambiguation_states_the_three_count_rules():
    text = MAPPING_DISAMBIGUATION
    bullets = [b for b in text.split("\n- ")]
    def _bullet(*needles):
        return [b for b in bullets if all(n in b for n in needles)]
    assert _bullet("'actual-quantity'", "event"), "no event-row rule for actual-quantity"
    assert _bullet("'quality-metric'", "'scrap-waste'", "GOOD"), "no good-vs-bad count rule"
    assert _bullet("'inspection'", "passed", "failed"), "no check-tally rule"
    # The old hint that steered a check's pass tally onto quality-metric is gone.
    assert "pass_count" not in text


@pytest.mark.parametrize("concept", COUNTED_OUTPUT_CONCEPTS)
def test_count_rules_name_no_seeded_table_or_column(concept):
    """The rules are generic: they must not name the seeded MES tables/columns
    whose mapping they fix."""
    for literal in ("quantity_at_event", "production_events", "quality_checks.pass_count",
                    "fail_count"):
        assert literal not in CORE_CONCEPT_DEFINITIONS[concept], (concept, literal)
        assert literal not in MAPPING_DISAMBIGUATION, literal


def test_counter_readings_are_excluded_but_interval_deltas_are_throughput():
    text = MAPPING_DISAMBIGUATION
    assert "counter READING or snapshot" in text
    assert "OWN interval (a delta, not a running count) is 'throughput'" in text
    assert "running count" in CORE_CONCEPT_DEFINITIONS["actual-quantity"][:140]


def test_bare_event_row_quantity_is_not_throughput():
    """Replay 2026-10-16 (live vocabulary): with only names and types to go on,
    the mapper read an event row's bare quantity as one interval's output in 3 of
    12 runs, giving 'throughput' a second column. An event-row quantity is
    'throughput' only when its NAME says it is per interval."""
    bullet = next(b for b in MAPPING_DISAMBIGUATION.split("\n- ")
                  if b.startswith("'actual-quantity'"))
    assert "event or log row whose name does not say it is per interval" in bullet
    assert bullet.rstrip().endswith("leave it UNMAPPED.")


def test_only_sampled_check_tallies_go_to_inspection():
    """A 100%-inspection station's good and reject counts are output, not a sample."""
    text = MAPPING_DISAMBIGUATION
    assert "tallies of a SAMPLED quality check" in text
    assert "inspecting EVERY unit made are output: 'quality-metric' and 'scrap-waste'" in text


def test_record_timestamps_stay_on_event_time():
    """Live 2026-10-16 (and the offline replay with the live vocabulary): once the
    count rules said a check's tallies 'are measures of' its record, the mapper
    moved every start, end and check timestamp onto its record's own concept and
    dropped 'event-time', so 24-hour and 7-day windows lost their time column.
    The rule keeps a record's own timestamps on 'event-time', in general terms."""
    bullets = MAPPING_DISAMBIGUATION.split("\n- ")
    rule = [b for b in bullets if "'event-time'" in b and b.startswith("A timestamp")]
    assert rule, "no event-time rule for record timestamps"
    text = rule[0]
    assert "started, ended" in text
    assert "even when it belongs to a record that has its own concept" in text
    for literal in ("actual_start_time", "actual_end_time", "check_time", "state_since",
                    "work_orders", "production_runs", "quality_checks", "line_status"):
        assert literal not in text, literal


def test_open_record_end_time_is_not_event_time():
    """An active downtime has no end time yet. Mapped as event-time beside the
    start time (which main never did), it could become the window column and
    drop every open stop from a 24-hour count."""
    rule = next(b for b in MAPPING_DISAMBIGUATION.split("\n- ") if b.startswith("A timestamp"))
    assert "stays empty while the record is still open" in rule
    assert "not 'event-time'" in rule
    assert "end_time" not in rule and "downtime_events" not in rule


def test_scheduled_dates_keep_their_schedule_concept():
    """Main maps a work order's scheduled start to 'schedule'; telling the mapper
    only what a scheduled date is NOT left it unmapped in every replay."""
    rule = next(b for b in MAPPING_DISAMBIGUATION.split("\n- ") if b.startswith("A timestamp"))
    assert "is 'schedule' (role 'measure'), not 'event-time'" in rule
    assert "scheduled_start_time" not in rule


def test_event_type_is_not_machine_state():
    """An event row's type (start, stop, changeover) gave 'machine-state' a second
    column beside the line's real state column, so the Line State metrics were
    withheld (ADR-0076). Unmapped, it adds no candidate to any other concept."""
    bullets = MAPPING_DISAMBIGUATION.split("\n- ")
    rule = [b for b in bullets if b.startswith("'machine-state'")]
    assert rule, "no machine-state rule"
    assert "type of an event" in rule[0] and "NOT 'machine-state' in any role" in rule[0]
    assert "leave it UNMAPPED" in rule[0]
    assert "'event-time' only, never 'machine-state'" in rule[0]
    assert "event_type" not in rule[0]


def test_rated_capacity_is_neither_throughput_nor_target_speed():
    """A line's rated capacity per hour took 'target-machine-speed' (a per-minute
    setpoint) beside the real setpoint, or 'throughput' beside the real output,
    withholding one or the other. Unmapped, it can't become a line dimension
    candidate either."""
    bullets = MAPPING_DISAMBIGUATION.split("\n- ")
    rule = [b for b in bullets if "CAPACITY" in b]
    assert rule, "no capacity rule"
    text = rule[0]
    assert "not 'throughput'" in text and "not 'target-machine-speed'" in text
    assert "leave it UNMAPPED" in text
    assert "capacity_per_hour" not in text


def _mcp_prompt() -> str:
    import agents.onboard_agent as onboard_agent
    return onboard_agent._build_mapfields_prompt(
        "line-mcp", "concept-a", "  concept-a: hint", "Line1Station: a/b/c", is_mcp=True)


def _opc_suffix_rules(prompt: str) -> list[tuple[tuple[str, ...], str]]:
    """``[(suffixes, concept)]`` for every 'Tags ending in ... → concept' rule."""
    rules = []
    for line in prompt.splitlines():
        m = re.match(r"- Tags ending in (.+?) → concept '([a-z0-9-]+)'", line)
        if m:
            rules.append((tuple(s.strip() for s in m.group(1).split(",")), m.group(2)))
    return rules


def _generated_line_tags() -> dict[str, list[str]]:
    """Every line type the repo's OPC UA dataset generator builds, as tag paths."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[3] / "highbyte" / "generate_datasets.py"
    spec = importlib.util.spec_from_file_location("_gen_datasets", path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return {
        "bottling": list(gen.bottling_tags("Ent/SiteA/Area", "Line1")),
        "automotive": list(gen.automotive_tags("Ent/SiteB/Area", "Line1")),
        "food": list(gen.food_tags("Ent/SiteC/Area", "Line1")),
        "machining": list(gen.machining_tags("Ent/SiteD/Area", "Line3", cmx441=True)),
    }


def test_opc_station_counters_are_left_unmapped():
    """A station's Good/Bad/Total/PartsCount/RejectCount tags are running piece
    counters, one per station. No suffix rule may map them to a counted-output
    concept: every unit passes several stations, and several fields under one
    concept on one system are withheld (ADR-0076)."""
    prompt = _mcp_prompt()
    rule_line = next(l for l in prompt.splitlines() if "_PartsCount" in l)
    for suffix in ("_Good", "_Bad", "_RejectCount", "_Total", "_PartsCount"):
        assert suffix in rule_line
    assert "UNMAPPED" in rule_line and "concept '" not in rule_line
    for suffixes, concept in _opc_suffix_rules(prompt):
        assert not ({"_Good", "_Bad", "_Total", "_PartsCount", "_RejectCount"} & set(suffixes)), (
            suffixes, concept)


@pytest.mark.parametrize("line_type", ["bottling", "automotive", "food", "machining"])
def test_opc_suffix_rules_bind_at_most_one_tag_per_counted_concept(line_type):
    """Apply the prompt's suffix rules to a real generated line: each counted-
    output concept gets at most ONE tag, so a metric on it can bind."""
    tags = _generated_line_tags()[line_type]
    rules = _opc_suffix_rules(_mcp_prompt())
    per_concept: dict[str, list[str]] = {}
    for tag in tags:
        for suffixes, concept in rules:
            if any(tag.endswith(s) for s in suffixes):
                per_concept.setdefault(concept, []).append(tag)
    for concept in COUNTED_OUTPUT_CONCEPTS:
        assert len(per_concept.get(concept, [])) <= 1, (line_type, concept, per_concept[concept])
    # The generator really does carry the per-station counters this guards.
    assert sum(t.endswith(("_Good", "_Bad", "_Total", "_PartsCount")) for t in tags) >= 3


def test_actual_quantity_names_station_tallies_as_running_counts(wizard_lines):
    line = wizard_lines["actual-quantity"].lower()
    assert "station counter" not in CORE_CONCEPT_DEFINITIONS["actual-quantity"]
    assert "station tally" in line and "running count" in line, line
    assert "station's piece tally" in MAPPING_DISAMBIGUATION


def test_oee_quality_is_a_rate_not_a_good_count():
    """OEE's quality factor is a ratio; summing it as a good count is wrong."""
    prompt = _mcp_prompt()
    assert "OEE_Quality -> concept 'quality-yield-rate'" in prompt
    assert "OEE_Quality -> concept 'quality-metric'" not in prompt
    assert "quality via `quality-yield-rate`" in CORE_CONCEPT_DEFINITIONS["oee-score"]


def _wizard_row(table: str, column: str) -> dict:
    """A wizard map row the model left unmapped."""
    return {"table": table, "column": column, "field": f"{table}.{column}",
            "concept": "needs_review", "confidence": 0, "mappings": [], "concepts": []}


def test_wizard_proposal_keeps_station_counters_unmapped_on_telemetry(monkeypatch):
    """End to end through the wizard's per-row proposal: on a telemetry-rich
    system a station piece counter the model left unmapped stays unmapped (it
    is not defaulted to 'sensor-reading'); any other unmapped tag still is."""
    import agents.concept_suggest as concept_suggest
    import agents.onboard as onboard

    monkeypatch.setattr(concept_suggest, "suggest_concepts_by_value_overlap", lambda *a, **k: None)
    monkeypatch.delenv("ADT_MAPPING_OVERRIDES_FILE", raising=False)
    tags = _generated_line_tags()["automotive"]
    counters = [t for t in tags if t.endswith(("_Good", "_Bad", "_Total", "_PartsCount"))]
    other = next(t for t in tags if t.endswith("SpindleSpeed"))
    rows = [_wizard_row("Line1CNC1", t) for t in counters + [other]]
    source = [{"name": "Line1CNC1", "columns": [{"name": r["column"]} for r in rows]}]
    out = onboard._propose_row_concepts("line-mcp", rows, source, True, None)
    by_col = {r["column"]: r for r in out}
    for tag in counters:
        assert by_col[tag]["concept"] == "needs_review", (tag, by_col[tag])
        assert not by_col[tag]["mappings"], tag
    assert by_col[other]["concept"] == "sensor-reading"


def test_station_counter_matching_is_by_signal_name():
    from agents.onboard_agent import is_station_piece_counter
    for col in ("Ent/Site/Area/Line1/CNC1/CNC1_Total", "Line1Washer.Good", "Bad",
                "Line1/QI/QI_RejectCount", "CNC1_PartsCount"):
        assert is_station_piece_counter(col), col
    for col in ("Line1/MD/MD_TotalScanned", "Line1/Line1_LineOutput", "SpindleSpeed",
                "goodwill", None):
        assert not is_station_piece_counter(col), col


def test_no_concept_lists_a_station_counter_the_rule_leaves_unmapped():
    """The tag rule and the concept field lists must agree, or the model gets
    two instructions for one tag."""
    from agents.onboard_agent import is_station_piece_counter
    for concept, text in CORE_CONCEPT_DEFINITIONS.items():
        m = re.search(r"Fields: ([^.]*)\.", text)
        for field in (m.group(1).split(",") if m else []):
            field = field.strip()
            if any(c.isupper() for c in field):  # OPC-style signal names only
                assert not is_station_piece_counter(field), (concept, field)


def test_a_record_average_rate_is_not_machine_speed():
    """Main and every replay: a line's live speed and a work order's average rate
    both took 'machine-speed', so the Machine Speed card was withheld. Unmapped
    (not moved to the order's own concept), it adds no candidate to the
    work-order count either."""
    bullets = MAPPING_DISAMBIGUATION.split("\n- ")
    rule = [b for b in bullets if b.startswith("'machine-speed'")]
    assert rule, "no machine-speed rule"
    assert "ACTUAL rate stored on an order or run" in rule[0] and "NOT 'machine-speed'" in rule[0]
    assert "NOT 'performance'" in rule[0] and "leave it UNMAPPED" in rule[0]
    assert "'work-order'" not in rule[0]
    assert "actual_rate_per_minute" not in rule[0] and "line_speed" not in rule[0]


def test_a_code_and_its_description_are_one_fact():
    """Main and every replay: a reason code and the reason's text on the same
    record both took 'failure-mode', so the Active Alarms count was withheld."""
    bullets = MAPPING_DISAMBIGUATION.split("\n- ")
    rule = [b for b in bullets if "text description" in b]
    assert rule, "no code-vs-description rule"
    assert "map the code to the concept it names" in rule[0]
    assert "is 'failure-mode'" in rule[0] and "description UNMAPPED" in rule[0]
    assert "reason_description" not in rule[0] and "reason_code" not in rule[0]
