# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A query hint that names no column of its system reaches the orchestrator again.

The route seed stopped showing query-hint text to the orchestrator so that a
column named in a hint could not leak into a delegation; the seed carries a
count line instead. That also hid hints about what to ASK a system: a hint
saying which rows are events, and must never be requested as units, reached
only the system agent, which then answered the orchestrator's delegation in
the delegation's own wrong terms.

Now a hint that names none of the system's onboarded tables or columns (and
carries no physical-name shape: a snake_case word, a dotted path, a camelCase
word) rides the seed inside the system's QUERY HINTS section, under the same
per-field cap and packer. A hint that names one stays reference-only, and the
count line counts only those. When the system's names cannot be read, every
hint stays reference-only. The steward delivery projection fits the same
document from the same names.

Offline: graph reads use an injected fake; fixtures are synthetic and generic.
"""
from __future__ import annotations

import json

import pytest

import agents.orchestrator as orch
from agents import learning_health
from agents import query_intelligence
from agents import guidance_scope as gs
from agents.guidance_scope import DELIVERY_FIELD_CAP, GuidanceScope, append_note, content_digest

_BASE = "System A answers which line ran a segment; ask it per segment."
_FREE = "Open rows are events, not stuck units: never ask for them as units."
_COLUMN = "Window finished rows on done_on, failed rows on failed_on."
_PLAIN_COLUMN = "Scope a site by cell; the location value is empty."
_TABLE = "Read every state from the history record, never the summary."
_LEAF = "Defect text sits in the reason of a problem record."
_SHAPED = "Join through partRef when the line is unknown."

_NAMES = [
    {"system_id": "system-a", "table_name": "history",
     "field_names": ["done_on", "failed_on", "location", "payload.problem.reason"]},
    {"system_id": "system-b", "table_name": "other_events", "field_names": ["free"]},
]


def _document(hints) -> str:
    text = _BASE
    for hint in hints:
        text = append_note(text, hint, GuidanceScope.QUERY_HINT)
    return text


def _row(system_id: str, guidance: str) -> dict:
    return {"system_id": system_id, "system_name": system_id.title(),
            "connection_type": "Athena", "query_guidance": guidance}


def _graph(rows, names=_NAMES, calls=None):
    def query(cypher, *_args, **_kwargs):
        if calls is not None:
            calls.append(cypher)
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return rows
        if cypher == getattr(gs, "HINT_SCHEMA_NAMES_CYPHER", None):
            return names
        return []
    return query


def _payload(seed: str) -> dict:
    return json.JSONDecoder().raw_decode(seed.split("RESULT:\n", 1)[1])[0]


def _system(seed: str, system_id: str = "system-a") -> dict:
    return {s["id"]: s for s in _payload(seed)["systems"]}[system_id]


def test_a_column_free_hint_rides_the_seed_and_a_column_hint_does_not():
    seed = orch.build_route_seed(query=_graph([_row("system-a", _document([_FREE, _COLUMN]))]))
    system = _system(seed)

    assert _FREE in system["query_guidance"]
    assert _COLUMN not in seed and "done_on" not in seed and "failed_on" not in seed
    assert system["query_hints"] == (
        "1 more query hint is delivered to system-a with each question; "
        "do not restate it or any column it names")


def test_any_name_of_the_system_keeps_a_hint_reference_only():
    hints = [_FREE, _PLAIN_COLUMN, _TABLE, _LEAF, _SHAPED]
    seed = orch.build_route_seed(query=_graph([_row("system-a", _document(hints))]))
    system = _system(seed)

    assert _FREE in system["query_guidance"]
    for hint in (_PLAIN_COLUMN, _TABLE, _LEAF, _SHAPED):
        assert hint not in seed, hint
    assert system["query_hints"].startswith("4 more query hints are delivered to system-a")


def test_names_are_per_system():
    """``free`` is a column of system-b only, so it does not withhold system-a's hint."""
    hint = "Ask for free capacity per line, never per order."
    rows = [_row("system-a", _document([hint])), _row("system-b", _document([hint]))]
    seed = orch.build_route_seed(query=_graph(rows))

    assert hint in _system(seed, "system-a")["query_guidance"]
    assert hint not in _system(seed, "system-b")["query_guidance"]


def test_hints_stay_reference_only_when_the_system_s_names_are_unknown():
    rows = [_row("system-a", _document([_FREE, _COLUMN]))]
    known = orch.build_route_seed(query=_graph(rows))
    unknown = orch.build_route_seed(query=_graph(rows, names=[]))

    assert _FREE in _system(known)["query_guidance"]
    assert _FREE not in unknown
    assert _system(unknown)["query_hints"] == (
        "2 query hints are delivered to system-a with each question; "
        "do not restate them or any column they name")


def test_the_names_are_read_only_for_a_system_with_hints():
    calls: list = []
    orch.build_route_seed(query=_graph([_row("system-a", _document([]))], calls=calls))
    assert len(calls) == len(set(calls))
    without_hints = list(calls)

    calls.clear()
    orch.build_route_seed(query=_graph([_row("system-a", _document([_FREE]))], calls=calls))
    assert [c for c in calls if c not in without_hints] == [gs.HINT_SCHEMA_NAMES_CYPHER]


def _long_hints(count: int) -> list:
    return [(f"Hint {n}: ask for whole segments, never split one across " + "lines " * 50).strip()
            for n in range(count)]


def test_shown_hints_keep_the_cap_and_the_drop_accounting_stays_exact(monkeypatch):
    # The budget this over-budget seed was sized against.
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    hints = [_COLUMN, *_long_hints(30)]
    seed = orch.build_route_seed(query=_graph([_row("system-a", _document(hints))]))
    payload = _payload(seed)
    system = _system(seed)

    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert len(system["query_guidance"]) <= DELIVERY_FIELD_CAP
    assert hints[-1] in system["query_guidance"]          # the newest shown hint survives
    assert hints[1] not in system["query_guidance"]       # the oldest shown hint is dropped
    row = payload["guidance_delivery_truncation"]["systems"]["system-a"]
    assert row[1] >= 1 and row[4]["q"] == row[1]

    composed = query_intelligence.build_composed_context_from_metadata(
        payload["systems"], payload.get("guidance_delivery_truncation"),
        payload.get("shared_guidance"))
    delivered = {a["digest"] for a in composed["artifacts"] if a["scope"] == "query_hint"}
    assert "accounting" not in (composed.get("delivery_truncation") or {})
    dropped = {content_digest(h) for h in hints[1:] if h not in system["query_guidance"]}
    assert len(dropped) == row[1]
    assert delivered == {content_digest(h) for h in hints} - dropped


def test_the_steward_projection_fits_the_document_the_seed_shows(monkeypatch):
    # The budget this over-budget seed was sized against.
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    hints = [_COLUMN, *_long_hints(30)]
    document = _document(hints)
    seed = orch.build_route_seed(query=_graph([_row("system-a", document)]))
    seed_row = _payload(seed)["guidance_delivery_truncation"]["systems"]["system-a"]
    guidance_row = {"system_id": "system-a", "base": _BASE, "query_hints": hints,
                    "answer_requirements": [], "legacy_unscoped": []}
    names = gs.schema_names_from_rows(_NAMES, {"system-a"})

    stats = learning_health.guidance_delivery_stats([guidance_row], schema_names=names)

    assert stats["truncated"] is True
    assert stats["notes_dropped"] == seed_row[1]
    assert learning_health.guidance_delivery_stats([guidance_row])["truncated"] is False


# --------------------------------------------------------------------------- #
# A column name with a space or a hyphen is matched as written
# --------------------------------------------------------------------------- #
#: Names a catalog can hold that are not identifier-shaped.
_SPACED = [{"system_id": "system-a", "table_name": "history",
            "field_names": ["order date", "status-code", "done_on"]}]


@pytest.mark.parametrize("hint", [
    "Window the rows on order date, never on the creation day.",   # plain
    'Window on "Order Date" only.',                                # quoted, other case
    "Window on order-date only.",                                  # hyphen form
    "Count status-code values per line.",                          # as stored
    "Count status code values per line.",                          # space form
    "Never window on done-on.",                                    # hyphen form of a snake name
])
def test_a_hint_naming_a_spaced_or_hyphenated_column_stays_reference_only(hint):
    names = gs.schema_names_from_rows(_SPACED, {"system-a"})["system-a"]

    assert gs.names_a_column(hint, names)
    assert gs.asker_hints([hint, _FREE], names) == [_FREE]


def test_a_spaced_column_hint_does_not_ride_the_seed():
    hint = "Window the rows on order date, never on the creation day."
    seed = orch.build_route_seed(query=_graph([_row("system-a", _document([_FREE, hint]))],
                                              names=_SPACED))

    assert _FREE in _system(seed)["query_guidance"]
    assert hint not in seed


def test_a_name_inside_a_longer_word_does_not_count():
    names = gs.schema_names_from_rows(_SPACED, {"system-a"})["system-a"]

    assert not gs.names_a_column("Reorder dates are steward notes, not rows.", names)


# --------------------------------------------------------------------------- #
# The names read asks only for the hinted systems, and is bounded
# --------------------------------------------------------------------------- #
def _recording_graph(rows, names, calls):
    def query(cypher, params=None, **_kwargs):
        calls.append((cypher, params))
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return rows
        if cypher == getattr(gs, "HINT_SCHEMA_NAMES_CYPHER", None):
            return names
        return []
    return query


def _names_read(calls) -> list:
    return [(c, p) for c, p in calls if c == getattr(gs, "HINT_SCHEMA_NAMES_CYPHER", None)]


def test_the_names_read_is_parameterised_by_the_hinted_systems_and_limited():
    calls: list = []
    rows = [_row("system-a", _document([_FREE])), _row("system-b", _document([])),
            _row("system-c", _document([_FREE]))]
    orch.build_route_seed(query=_recording_graph(rows, _NAMES, calls))

    [(cypher, params)] = _names_read(calls)
    assert params == {"system_ids": ["system-a", "system-c"]}
    assert "$system_ids" in cypher and "LIMIT" in cypher


_TWO_SYSTEMS = [
    {"system_id": "system-a", "table_name": "history",
     "field_names": ["done_on", "failed_on", "location"], "row_count": 3},
    {"system_id": "system-b", "table_name": "other_events",
     "field_names": ["other_col", "x_col"], "row_count": 2},
]


def _seed_two(names):
    rows = [_row("system-a", _document([_FREE])), _row("system-b", _document([_FREE]))]
    return orch.build_route_seed(query=_recording_graph(rows, names, []))


def test_the_system_the_row_cap_cut_keeps_its_hints_reference_only(monkeypatch):
    monkeypatch.setattr(gs, "HINT_NAME_ROWS_CAP", 4, raising=False)
    seed = _seed_two(_TWO_SYSTEMS)                 # 5 rows: the read stopped inside system-b

    assert _FREE in _system(seed, "system-a")["query_guidance"]
    assert _FREE not in _system(seed, "system-b")["query_guidance"]


def test_the_system_the_table_cap_cut_keeps_its_hints_reference_only(monkeypatch):
    monkeypatch.setattr(gs, "HINT_NAME_TABLES_CAP", 1, raising=False)
    seed = _seed_two(_TWO_SYSTEMS)                 # 2 tables: the read stopped at system-b

    assert _FREE in _system(seed, "system-a")["query_guidance"]
    assert _FREE not in _system(seed, "system-b")["query_guidance"]


def test_systems_past_the_system_cap_are_not_read_and_stay_reference_only(monkeypatch):
    monkeypatch.setattr(gs, "HINT_NAME_SYSTEMS_CAP", 1, raising=False)
    calls: list = []
    rows = [_row("system-a", _document([_FREE])), _row("system-b", _document([_FREE]))]
    seed = orch.build_route_seed(query=_recording_graph(rows, _TWO_SYSTEMS, calls))

    [(_cypher, params)] = _names_read(calls)
    assert params == {"system_ids": ["system-a"]}
    assert _FREE in _system(seed, "system-a")["query_guidance"]
    assert _FREE not in _system(seed, "system-b")["query_guidance"]


def test_the_steward_projection_reads_the_same_bounded_names(monkeypatch):
    import agents.db as db
    calls: list = []

    def fake(cypher, params=None):
        calls.append((cypher, params))
        return _NAMES
    monkeypatch.setattr(db, "neptune_query", fake)
    rows = [{"system_id": "system-a", "query_hints": [_FREE]},
            {"system_id": "system-b", "query_hints": []}]

    names = learning_health._read_hint_schema_names(rows)

    [(cypher, params)] = calls
    assert cypher == gs.HINT_SCHEMA_NAMES_CYPHER
    assert params == {"system_ids": ["system-a"]}
    assert set(names) == {"system-a"}
