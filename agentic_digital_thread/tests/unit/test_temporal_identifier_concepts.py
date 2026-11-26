# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A date or time column never carries a record's identifier concept.

Walks 2026-10-21/22: Map proposed ``work-order`` (85%) on a CMMS work order's
reported date and ``work-order`` (80%) on a vendor ``WorkOrder.create_date`` and
``complete_date``. The temporal backstop
(``surrogate_keys.drop_identifier_tags_on_temporal_columns``) stripped only
an identifier ROLE or a ``*-id`` concept, so a record concept in the
``measure`` or ``correlate`` role passed; both map prompts even gave
"reported_date -> work-order" as a measure example. A concept the vocabulary
declares as naming one record (``concept_tagging.ENTITY_CONCEPT_IDS``) is an
identifier kind, whatever the role.

The strip picks no replacement: which date of a table is the
time windows filter on (``event-time``) and which is a plan (``schedule``)
cannot be read from a column's type, and choosing by row order filtered on a
due date. A time concept the proposer gave survives; a column left with no tag
is unmapped, for review.

The schema here is deliberately unlike the seed: other table, column and value
names. Concepts are the vocabulary's own ids.
"""
from __future__ import annotations

import copy
import json
import re

import agents.onboard as onboard
import agents.onboard_agent as onboard_agent
from agents import surrogate_keys as sk
from agents.concept_tagging import ENTITY_CONCEPT_IDS

TIME = "event-time"
PLANNED = "schedule"            # the vocabulary's planned-date field concept
RECORD = "work-order"           # an entity (record) concept in the vocabulary
BATCH = "batch"                 # another one
PLAN = "maintenance-schedule"   # an entity concept whose name mentions a schedule
MEASURE = "cycle-time"          # a measure concept


def _tables():
    return [
        {"name": "jobs", "columns": [
            {"name": "job_ref", "data_type": "character varying"},
            {"name": "due_date", "data_type": "date"},
            {"name": "occurred_at", "data_type": "timestamp with time zone"}]},
        {"name": "plans", "columns": [
            {"name": "next_due", "data_type": "date"},
            {"name": "plan_ref", "data_type": "text"}]},
        {"name": "Lots", "columns": [
            {"name": "brewedAt", "data_type": "string (ISO-8601 date-time the lot was brewed)"},
            {"name": "lotCode", "data_type": "string (lot code)"}]},
        {"name": "stops", "sample_values": {"began": ["2026-03-01T04:10:00Z"],
                                            "ended": ["2026-03-01T05:00:00Z"]},
         "columns": [{"name": "began", "data_type": "unknown"},
                     {"name": "ended", "data_type": "unknown"}]},
    ]


def _row(field, *tags):
    table = field.split(".")[0]
    return {"field": field, "table": table,
            "mappings": [{"concept": c, "role": r, "confidence": p} for c, r, p in tags]}


def _strip(rows):
    rows = copy.deepcopy(rows)
    sk.drop_identifier_tags_on_temporal_columns(rows, _tables())
    return {r["field"]: r for r in rows}


def _concepts(row):
    return [m["concept"] for m in row["mappings"]]


# --------------------------------------------------------------------------- #
# strip, and choose nothing in its place
# --------------------------------------------------------------------------- #

def _two_dates():
    return [_row("jobs.due_date", (RECORD, "measure", 0.91)),
            _row("jobs.occurred_at", (RECORD, "correlate", 0.8))]


def test_both_record_tagged_dates_are_left_unmapped_in_either_order():
    """Row order must not pick a winner: with due_date first, a by-order pick
    would make due_date event-time at the stripped tag's 0.91 and leave
    occurred_at unmapped. Neither may be chosen: both are unmapped."""
    forward = _strip(_two_dates())
    backward = _strip(list(reversed(_two_dates())))
    for got in (forward, backward):
        assert got["jobs.due_date"]["mappings"] == []
        assert got["jobs.occurred_at"]["mappings"] == []
        assert got["jobs.due_date"]["temporal_identifier_removed"] == [RECORD]
        assert "time_concept_offered" not in got["jobs.due_date"]
    assert forward == backward


def test_time_concepts_the_proposer_gave_survive_in_either_order():
    rows = [_row("jobs.due_date", (RECORD, "measure", 0.91), (PLANNED, "measure", 0.6)),
            _row("jobs.occurred_at", (RECORD, "measure", 0.9), (TIME, "measure", 0.7))]
    for order in (rows, list(reversed(rows))):
        got = _strip(order)
        assert got["jobs.due_date"]["mappings"] == [
            {"concept": PLANNED, "role": "measure", "confidence": 0.6}]
        assert got["jobs.occurred_at"]["mappings"] == [
            {"concept": TIME, "role": "measure", "confidence": 0.7}]


def test_an_open_records_end_time_is_stripped_and_left_unmapped():
    """A stop's end time stays empty while the stop is open. Its record
    concept goes; it gets no time concept beside the start time's."""
    got = _strip([_row("stops.began", (TIME, "measure", 0.9)),
                  _row("stops.ended", ("downtime", "measure", 0.8))])
    assert got["stops.ended"]["mappings"] == []
    assert got["stops.ended"]["temporal_identifier_removed"] == ["downtime"]
    assert _concepts(got["stops.began"]) == [TIME]


def test_record_concepts_go_and_other_tags_stay():
    got = _strip([_row("jobs.job_ref", (RECORD, "identifier", 0.9)),
                  _row("Lots.brewedAt", (BATCH, "measure", 0.8), (MEASURE, "measure", 0.5)),
                  _row("Lots.lotCode", (BATCH, "identifier", 0.9))])
    assert _concepts(got["jobs.job_ref"]) == [RECORD]        # not a date
    assert _concepts(got["Lots.lotCode"]) == [BATCH]         # not a date
    assert _concepts(got["Lots.brewedAt"]) == [MEASURE]      # the record concept went


def test_a_maintenance_schedule_tag_leaves_a_due_date_and_schedule_stays():
    """``maintenance-schedule`` names the recurring maintenance record (an
    entity concept); ``schedule`` is the planned-date field concept."""
    got = _strip([_row("plans.next_due", (PLAN, "measure", 0.88), (PLANNED, "measure", 0.7))])
    assert got["plans.next_due"]["mappings"] == [
        {"concept": PLANNED, "role": "measure", "confidence": 0.7}]
    assert got["plans.next_due"]["temporal_identifier_removed"] == [PLAN]


def test_identifier_kind_is_entity_membership_or_an_id_name():
    for concept in ENTITY_CONCEPT_IDS:
        assert sk.is_identifier_kind_concept(concept), concept
    assert sk.is_identifier_kind_concept(PLAN)
    assert sk.is_identifier_kind_concept("equipment-id")
    assert sk.is_identifier_kind_concept("Work-Order")
    for concept in (PLANNED, TIME, MEASURE, ""):
        assert not sk.is_identifier_kind_concept(concept), concept


# --------------------------------------------------------------------------- #
# through the wizard's map job
# --------------------------------------------------------------------------- #

def _fake_mapper(prompt: str, timeout: int = 90, model=None) -> str:
    return json.dumps([_row("jobs.job_ref", (RECORD, "identifier", 0.9)),
                       *_two_dates()])


def test_the_map_job_leaves_stripped_date_columns_for_review(monkeypatch):
    import agents.catalog as catalog_module
    import agents.concept_suggest as concept_suggest
    monkeypatch.setattr(catalog_module, "concepts_prompt_block", lambda: "concepts")
    monkeypatch.setattr(concept_suggest, "neptune_query", lambda *a, **k: [])
    monkeypatch.setattr(onboard, "neptune_query", lambda *a, **k: [])
    monkeypatch.setattr(onboard, "bedrock_call", _fake_mapper)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [RECORD, PLANNED, TIME, MEASURE])
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: {"connection_type": "PostgreSQL"})
    jobs = {**_tables()[0], "relation_kind": "base_table", "schema_revision": "c" * 32,
            "row_count": 3, "sample_values": {}}
    onboard._map_jobs["tcjob"] = {"status": "running"}
    onboard._run_map_job("tcjob", "jobs-system", [jobs])
    job = onboard._map_jobs.pop("tcjob")
    assert job["status"] == "done", job
    rows = {m["field"]: m for m in job["result"]["mappings"]}
    for field in ("jobs.due_date", "jobs.occurred_at"):
        assert rows[field]["concept"] == "needs_review", rows[field]
        assert rows[field]["confidence"] == 0
        assert rows[field]["concepts"] == []
    assert rows["jobs.job_ref"]["concept"] == RECORD


# --------------------------------------------------------------------------- #
# the prompts no longer teach it
# --------------------------------------------------------------------------- #

_EXAMPLE = re.compile(r"(\w+)\s*→\s*([\w-]+)")
_DATE_NAME = re.compile(r"(^|_)(date|time|ts|at|on)$")


def _measure_role_text(prompt: str) -> str:
    start = prompt.index("measure")
    return prompt[start:prompt.index("correlate", start)]


def _names_a_record(concept: str) -> bool:
    return concept in ENTITY_CONCEPT_IDS or concept.endswith("-id")


def test_no_map_prompt_teaches_a_date_column_to_take_a_record_concept():
    wizard = onboard._build_map_prompt("sys-a", "c", "f")
    chat = onboard_agent._build_mapfields_prompt("sys-a", "c", "h", "f", False)
    for prompt in (wizard, chat):
        role = _measure_role_text(prompt.split("Roles", 1)[1])
        for column, concept in _EXAMPLE.findall(role):
            assert not (_DATE_NAME.search(column) and _names_a_record(concept)), \
                (column, concept)


def test_the_timestamp_rule_gives_no_timestamp_its_records_concept():
    """The open-record exception (an end time empty while the record is open)
    said such a time "is a measure of that record": a record concept on a
    timestamp, which the backstop now removes. The rule leaves it unmapped
    instead, and still keeps it off the time concept."""
    from agents.catalog import MAPPING_DISAMBIGUATION
    rule = next(b for b in MAPPING_DISAMBIGUATION.split("\n- ") if b.startswith("A timestamp"))
    assert "measure of that record" not in rule
    assert "stays empty while the record is still open" in rule
    assert "not 'event-time' and not that record's own concept: leave it UNMAPPED" in rule
