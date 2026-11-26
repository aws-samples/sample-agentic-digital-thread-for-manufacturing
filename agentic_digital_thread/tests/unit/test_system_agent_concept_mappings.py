# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System-agent first-class concept-mapping awareness.

Today the orchestrator owns concept translation (explore_graph's routed schemas
+ the graph's Field-[:MAPS_TO]->Concept edges) and system agents get their
SCHEMA but no concept→field record — only incidental hints. The fix gives the
agent the system's onboarded mapping record: provision flattens body.mappings via
``onboard._concept_mapping_items`` (the SAME parse the graph write uses, so it
matches what /api/onboard/mappings serves) and attaches it to the
connection_config that rides the agent-config secret; ``create_system_agent``
pops it and keeps it as the FALLBACK for the per-question graph read
(``live_concept_mappings``, pinned in test_live_concept_mappings.py). The
renderer ``format_concept_mappings`` is pure (bounded, truncation note).
Empty/missing record → NO section.

All offline: model/Agent stubbed (mirrors test_sitewise_schema_seed.py); the
flattener and renderer are pure.
"""
import json

import pytest

import agents.system_agent as sa
import agents.sitewise_read as sitewise_read
from agents.onboard import _CONCEPT_MAPPING_MAX_ITEMS, _concept_mapping_items


# --------------------------------------------------------------------------- #
# Stubs (mirror test_sitewise_schema_seed's prompt-inspection pattern)
# --------------------------------------------------------------------------- #
class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeAgent:
    """Captures what create_system_agent built without running an LLM."""

    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []


_STUB_ITEMS = [
    {"concept": "throughput", "field": "production_runs.units_produced", "role": "measure"},
    {"concept": "site", "field": "production_runs.plant", "role": "identifier"},
    {"concept": "machine-state", "field": "asset_states.state", "role": "correlate"},
]

_STUB_SW_MODELS = [
    {
        "id": "11111111-aaaa-bbbb-cccc-222222222222",
        "name": "Washer",
        "properties": [
            {"id": "33333333-dddd-eeee-ffff-444444444444", "name": "RunningSpeed",
             "dataType": "DOUBLE", "unit": "rpm"},
        ],
    },
]


def _patch_agent(monkeypatch):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)


# --------------------------------------------------------------------------- #
# 1. format_concept_mappings — the pure renderer
# --------------------------------------------------------------------------- #
def test_renderer_emits_concept_to_field_lines():
    out = sa.format_concept_mappings(_STUB_ITEMS)
    assert "- throughput -> production_runs.units_produced [measure]" in out
    assert "- site -> production_runs.plant [identifier]" in out
    assert "- machine-state -> asset_states.state [correlate]" in out
    # Deterministic: sorted by concept (machine-state < site < throughput).
    assert out.index("machine-state") < out.index("site") < out.index("throughput")


def test_renderer_dedupes_and_tolerates_degenerate_items():
    out = sa.format_concept_mappings(
        [
            {"concept": "site", "field": "runs.plant"},
            {"concept": "site", "field": "runs.plant", "role": ""},  # dup
            {"concept": "", "field": "runs.x"},                      # no concept
            {"concept": "batch", "field": ""},                       # no field
            "not-a-dict",
        ]
    )
    assert out == "- site -> runs.plant"


def test_renderer_empty_returns_empty():
    assert sa.format_concept_mappings([]) == ""
    assert sa.format_concept_mappings(None) == ""


def test_renderer_cap_enforced_with_truncation_note():
    items = [
        {"concept": "throughput", "field": f"table_{i:03d}.units_produced", "role": "measure"}
        for i in range(200)
    ]
    cap = 600
    out = sa.format_concept_mappings(items, cap=cap)
    assert len(out) <= cap
    assert "[concept mappings truncated: showing" in out
    assert "of 200]" in out
    # One concept, one line: its fields are trimmed inside that line, each
    # kept field whole, and the note counts the fields shown.
    lines = [ln for ln in out.split("\n") if ln.startswith("- ")]
    assert len(lines) == 1
    assert lines[0].startswith("- throughput -> table_")
    assert lines[0].endswith("[measure]")
    shown = _entries(out)
    assert all(f in {it["field"] for it in items} for _c, f, _r in shown)
    assert out.endswith(f"showing {len(shown)} of 200]")


def test_renderer_default_cap_is_8kb():
    assert sa._CONCEPT_MAPPINGS_CAP == 8192
    items = [
        {"concept": "throughput", "field": f"a_very_long_table_name_{i:04d}.units_produced_counter"}
        for i in range(500)
    ]
    out = sa.format_concept_mappings(items)
    assert len(out) <= 8192
    assert "truncated" in out


def test_a_single_mapping_longer_than_the_cap_is_cut_not_dropped():
    out = sa.format_concept_mappings(
        [{"concept": "site", "field": "t." + "x" * 200, "role": "identifier"}], cap=120)
    assert out.startswith("- site -> t.xxx")
    assert len(out) <= 120
    assert out.endswith("[concept mappings truncated: showing 1 of 1]")


# --------------------------------------------------------------------------- #
# 2. _compose_system_prompt — section present/absent
# --------------------------------------------------------------------------- #
def _compose(concept_desc, connection_type="PostgreSQL", tables_desc="runs(plant: varchar)"):
    return sa._compose_system_prompt(
        system_id="erp-x",
        system_name="ERP X",
        schema="erp_data",
        system_prompt="You are ERP X.",
        tables_desc=tables_desc,
        connection_type=connection_type,
        concept_mappings_desc=concept_desc,
    )


def test_prompt_section_present_with_guidance_line():
    rendered = sa.format_concept_mappings(_STUB_ITEMS)
    prompt = _compose(rendered)
    assert "KNOWN CONCEPT MAPPINGS" in prompt
    assert "- throughput -> production_runs.units_produced [measure]" in prompt
    # The header says how to read a line that holds several fields.
    assert "one line per concept" in prompt
    assert "each [role] applies to the fields before it" in prompt
    # The behavioral guidance: mapped field directly, never guess.
    assert "use the mapped field directly" in prompt
    assert "do NOT guess column" in prompt


def test_prompt_section_absent_when_empty_and_byte_identical_to_before():
    without_kwarg = sa._compose_system_prompt(
        system_id="erp-x", system_name="ERP X", schema="erp_data",
        system_prompt="You are ERP X.", tables_desc="runs(plant: varchar)",
        connection_type="PostgreSQL",
    )
    with_empty = _compose("")
    assert "KNOWN CONCEPT MAPPINGS" not in with_empty
    assert with_empty == without_kwarg


# --------------------------------------------------------------------------- #
# 3. create_system_agent — the record rides connection_config as the
#    graph-read FALLBACK only (live_concept_mappings); it is never baked into
#    the system prompt any more (a link retired since onboarding kept
#    rendering there). Pinned in test_live_concept_mappings.py too.
# --------------------------------------------------------------------------- #
def test_postgresql_agent_keeps_mappings_as_the_fallback_snapshot(monkeypatch):
    import agents.live_concept_mappings as lcm
    lcm.reset()
    _patch_agent(monkeypatch)
    cfg = {"secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:conn",
           "concept_mappings": list(_STUB_ITEMS)}
    agent = sa.create_system_agent(
        system_id="erp-x", system_name="ERP X", schema="erp_data",
        system_prompt="You are ERP X.",
        tables_desc="production_runs(plant: varchar, units_produced: integer)",
        conn_params={"host": "localhost", "dbname": "vf", "user": "u", "password": "p"},
        connection_type="PostgreSQL",
        connection_config=cfg,
    )
    assert "KNOWN CONCEPT MAPPINGS (concept -> field)" not in agent.system_prompt
    # Popped so it is never mistaken for a connection key downstream.
    assert "concept_mappings" not in cfg
    fallback = lcm.section_for_question("erp-x", query=lambda *_a: (_ for _ in ()).throw(
        RuntimeError("graph down")))
    assert "- throughput -> production_runs.units_produced [measure] (suggested)" in fallback
    lcm.reset()


def test_sitewise_agent_keeps_mappings_as_the_fallback_beside_inventory(monkeypatch):
    import agents.live_concept_mappings as lcm
    lcm.reset()
    _patch_agent(monkeypatch)
    monkeypatch.setattr(
        sitewise_read, "list_asset_models_with_properties",
        lambda cfg, region, max_models=None: _STUB_SW_MODELS,
    )
    cfg = {"concept_mappings": [
        {"concept": "throughput", "field": "Washer.RunningSpeed", "role": "measure"},
    ]}
    agent = sa.create_system_agent(
        system_id="example-sitewise", system_name="Example SiteWise",
        schema="sitewise-direct", system_prompt="", tables_desc="",
        connection_type="SiteWise (Direct)", connection_config=cfg,
    )
    assert "Onboarded asset models and properties:" in agent.system_prompt
    assert "KNOWN CONCEPT MAPPINGS (concept -> field)" not in agent.system_prompt
    assert "concept_mappings" not in cfg
    lcm.reset()


def test_agent_without_record_has_no_section(monkeypatch):
    _patch_agent(monkeypatch)
    agent = sa.create_system_agent(
        system_id="erp-x", system_name="ERP X", schema="erp_data",
        system_prompt="You are ERP X.", tables_desc="runs(plant: varchar)",
        conn_params={"host": "localhost", "dbname": "vf", "user": "u", "password": "p"},
        connection_type="PostgreSQL",
        connection_config={},
    )
    assert "KNOWN CONCEPT MAPPINGS (concept -> field)" not in agent.system_prompt


# --------------------------------------------------------------------------- #
# 4. onboard._concept_mapping_items — the provision-side flattener
# --------------------------------------------------------------------------- #
def test_items_flatten_role_mappings_shape():
    items = _concept_mapping_items(
        [
            {"table": "production_runs", "field": "units_produced",
             "mappings": [{"concept": "throughput", "role": "measure", "confidence": 0.9}]},
            {"table": "production_runs", "field": "plant",
             "mappings": [{"concept": "site", "role": "identifier", "confidence": 0.95}]},
        ],
        default_table="erp_data",
    )
    assert items == [
        {"concept": "site", "field": "production_runs.plant", "role": "identifier"},
        {"concept": "throughput", "field": "production_runs.units_produced", "role": "measure"},
    ]


def test_items_top_level_concept_fallback_and_skips():
    items = _concept_mapping_items(
        [
            # Legacy shape: top-level concept, no role list → role 'identifier'.
            {"table": "runs", "field": "plant", "concept": "site"},
            # needs_review / unknown are never surfaced.
            {"table": "runs", "field": "x", "concept": "needs_review"},
            {"table": "runs", "field": "y",
             "mappings": [{"concept": "unknown", "role": "correlate"}]},
            # Non-core concepts are gated out (mirrors the MAPS_TO write gate).
            {"table": "runs", "field": "z",
             "mappings": [{"concept": "not-a-real-core-concept", "role": "measure"}]},
        ],
        default_table="erp_data",
    )
    assert items == [{"concept": "site", "field": "runs.plant", "role": "identifier"}]


def test_items_dotted_field_recovers_table():
    # Athena-style degraded row: no 'table' key, database-qualified dotted field.
    items = _concept_mapping_items(
        [{"field": "lakehouse.events.machine_state",
          "mappings": [{"concept": "machine-state", "role": "correlate"}]}],
        default_table="lakehouse",
    )
    assert items == [
        {"concept": "machine-state", "field": "events.machine_state", "role": "correlate"}
    ]


def test_items_dedupe_cap_and_empty():
    dup = {"table": "runs", "field": "plant",
           "mappings": [{"concept": "site", "role": "identifier"}]}
    assert len(_concept_mapping_items([dup, dict(dup)])) == 1
    many = [
        {"table": f"t{i:04d}", "field": "plant",
         "mappings": [{"concept": "site", "role": "identifier"}]}
        for i in range(_CONCEPT_MAPPING_MAX_ITEMS + 100)
    ]
    assert len(_concept_mapping_items(many)) == _CONCEPT_MAPPING_MAX_ITEMS
    assert _concept_mapping_items([]) == []
    assert _concept_mapping_items(None) == []


def test_items_round_trip_into_renderer():
    items = _concept_mapping_items(
        [{"table": "production_runs", "field": "units_produced",
          "mappings": [{"concept": "throughput", "role": "measure"}]}],
        default_table="erp_data",
    )
    out = sa.format_concept_mappings(items)
    assert out == "- throughput -> production_runs.units_produced [measure]"


# --------------------------------------------------------------------------- #
# One line per concept, and over the cap every concept keeps a field
# --------------------------------------------------------------------------- #
# The renderer used to emit one line per field and cut the sorted list at the
# cap, so on a wide system every concept after the cut point vanished from the
# prompt. On the onboarded workshop ERP (180 items) the section ended at
# "inventory-transaction": the agent never saw its batch-adjacent concepts such
# as shipment, sales-order, lot-number and work-order. Choosing lines across
# concepts kept every concept but still cut most fields. A line per concept
# names each concept once, so a whole system's fields fit.

def _erp_live_items() -> list:
    import json
    from pathlib import Path
    path = Path(__file__).parent / "fixtures" / "erp_concept_mappings_live.json"
    return json.loads(path.read_text())["items"]


def _entries(out: str) -> list[tuple[str, str, str]]:
    """``(concept, field, role)`` for every field a rendered section shows.

    A line is ``- concept -> f1, f2 [role]; f3 [role2]``: fields separated by
    ", ", role groups by "; ", each group's role in brackets after it (a group
    without a role has no brackets). A field that starts with a double quote
    is a JSON string and is read whole, separators and escapes included."""
    decoder = json.JSONDecoder()
    got = []
    for ln in out.splitlines():
        if not ln.startswith("- "):
            continue
        concept, rest = ln[2:].split(" -> ", 1)
        i, fields = 0, []
        while i < len(rest):
            if rest[i] == '"':
                field, i = decoder.raw_decode(rest, i)
            else:
                ends = [k for k in (rest.find(sep, i) for sep in (", ", "; ", " ["))
                        if k != -1]
                end = min(ends) if ends else len(rest)
                field, i = rest[i:end], end
            fields.append(field)
            if rest.startswith(", ", i):
                i += 2
                continue
            role = ""
            if rest.startswith(" [", i):
                close = rest.index("]", i)
                role, i = rest[i + 2:close], close + 1
            got.extend((concept, f, role) for f in fields)
            fields = []
            if rest.startswith("; ", i):
                i += 2
            elif i < len(rest):
                raise AssertionError(f"unreadable mapping line: {ln!r}")
    return got


def _shown(out: str) -> list[tuple[str, str]]:
    return [(concept, field) for concept, field, _role in _entries(out)]


def _mapping_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith("- ")]


def _as_entries(items: list) -> set:
    return {(it["concept"], it["field"], it["role"]) for it in items}


def test_under_the_cap_each_concept_is_one_line_with_all_its_fields():
    items = [{"concept": "site", "field": "runs.plant", "role": "identifier"},
             {"concept": "batch", "field": "runs.run_number", "role": "identifier"},
             {"concept": "batch", "field": "lots.batch_id", "role": "identifier"},
             {"concept": "batch", "field": "runs.batch_qty", "role": "measure"},
             {"concept": "site", "field": "sites.site_code", "role": ""}]
    # Concepts sorted; within a line, role groups sorted (a group without a
    # role last) and fields sorted within each group.
    assert sa.format_concept_mappings(items) == (
        "- batch -> lots.batch_id, runs.run_number [identifier]; runs.batch_qty [measure]\n"
        "- site -> runs.plant [identifier]; sites.site_code")


def test_every_mapping_of_the_live_erp_reaches_the_prompt():
    items = _erp_live_items()
    out = sa.format_concept_mappings(items)
    assert len(out) <= sa._CONCEPT_MAPPINGS_CAP
    assert "truncated" not in out
    assert set(_entries(out)) == _as_entries(items)
    assert len(_entries(out)) == len(items)
    assert len(_mapping_lines(out)) == len({i["concept"] for i in items})
    shown = {concept for concept, _field in _shown(out)}
    for concept in ("shipment", "sales-order", "lot-number", "work-order", "batch"):
        assert concept in shown


def test_over_the_cap_fields_are_trimmed_inside_each_concepts_line():
    items = ([{"concept": f"c{k:02d}", "field": f"t.f{j:02d}", "role": "measure"}
              for k in range(40) for j in range(8)])
    out = sa.format_concept_mappings(items, cap=1500)
    lines = _mapping_lines(out)
    # Every concept keeps exactly one line; no concept gets a second field
    # before each has one.
    assert sorted(ln[2:].split(" -> ", 1)[0] for ln in lines) == [f"c{k:02d}" for k in range(40)]
    counts = {}
    for concept, _field in _shown(out):
        counts[concept] = counts.get(concept, 0) + 1
    assert len(counts) == 40
    assert max(counts.values()) - min(counts.values()) <= 1
    assert len(out) <= 1500
    assert out.endswith(f"[concept mappings truncated: showing {len(_shown(out))} of {len(items)}]")


def test_over_the_cap_the_kept_lines_stay_sorted_and_whole():
    items = ([{"concept": f"c{k:02d}", "field": f"t.f{j:02d}", "role": "measure"}
              for k in range(20) for j in range(6)])
    out = sa.format_concept_mappings(items, cap=900)
    lines = _mapping_lines(out)
    assert lines == sorted(lines)
    assert all(ln.endswith("[measure]") for ln in lines)
    assert set(_entries(out)) <= _as_entries(items)
    for concept in {c for c, _f in _shown(out)}:
        fields = [f for c, f in _shown(out) if c == concept]
        assert fields == sorted(fields)
    # A concept's first field (in sort order) is the one it keeps first.
    assert {f for _c, f in _shown(out)} >= {"t.f00"}


# --------------------------------------------------------------------------- #
# The provision-side item cap keeps every concept too
# --------------------------------------------------------------------------- #
# Provision attaches at most _CONCEPT_MAPPING_MAX_ITEMS items to the agent
# config. Cutting the sorted list there dropped the concepts that sort last
# before the renderer ever saw them: on the onboarded workshop SiteWise system
# (252 items, 38 concepts) the SiteWise agent lost tool-life, tool-position,
# torque and vibration. At 200 items it still dropped 52 of SiteWise's fields,
# which the one-line-per-concept section has room for.

def _sitewise_live_items() -> list:
    import json
    from pathlib import Path
    path = Path(__file__).parent / "fixtures" / "sitewise_concept_mappings_live.json"
    return json.loads(path.read_text())["items"]


def _as_mapping_rows(items: list) -> list:
    """The provision-body shape ``_concept_mapping_items`` reads."""
    rows = []
    for it in items:
        table, column = it["field"].split(".", 1)
        rows.append({"table": table, "field": f"{table}.{column}",
                     "mappings": [{"concept": it["concept"], "role": it["role"]}]})
    return rows


def test_every_mapping_of_the_live_sitewise_reaches_the_prompt_through_provision():
    live = _sitewise_live_items()
    concepts = {i["concept"] for i in live}
    items = _concept_mapping_items(_as_mapping_rows(live), committable=concepts)
    assert len(items) == len(live) == 252
    assert _as_entries(items) == _as_entries(live)
    out = sa.format_concept_mappings(items)
    assert len(out) <= sa._CONCEPT_MAPPINGS_CAP
    assert "truncated" not in out
    assert set(_entries(out)) == _as_entries(live)
    assert len(_mapping_lines(out)) == len(concepts)
    shown = {concept for concept, _field in _shown(out)}
    for concept in ("vibration", "temperature", "machine-state", "torque", "tool-life"):
        assert concept in shown


def test_the_item_cap_takes_one_item_per_concept_per_round_and_stays_sorted():
    rows = [{"table": "t", "field": f"t.f{j:02d}",
             "mappings": [{"concept": f"c{k:02d}", "role": "measure"}]}
            for k in range(30) for j in range(10)]
    items = _concept_mapping_items(rows, committable={f"c{k:02d}" for k in range(30)},
                                   max_items=75)
    assert len(items) == 75
    counts = {}
    for it in items:
        counts[it["concept"]] = counts.get(it["concept"], 0) + 1
    assert len(counts) == 30
    assert max(counts.values()) - min(counts.values()) <= 1
    assert items == sorted(items, key=lambda it: (it["concept"], it["field"]))


def test_over_the_default_item_cap_every_concept_keeps_an_item():
    n_concepts = 40
    per_concept = _CONCEPT_MAPPING_MAX_ITEMS // n_concepts + 3
    rows = [{"table": "t", "field": f"t.f{j:03d}",
             "mappings": [{"concept": f"c{k:02d}", "role": "measure"}]}
            for k in range(n_concepts) for j in range(per_concept)]
    items = _concept_mapping_items(rows, committable={f"c{k:02d}" for k in range(n_concepts)})
    assert len(items) == _CONCEPT_MAPPING_MAX_ITEMS
    assert {it["concept"] for it in items} == {f"c{k:02d}" for k in range(n_concepts)}


def test_under_the_item_cap_the_items_are_unchanged():
    rows = [{"table": "t", "field": f"t.f{j}",
             "mappings": [{"concept": "c", "role": "measure"}]} for j in range(5)]
    items = _concept_mapping_items(rows, committable={"c"})
    assert [it["field"] for it in items] == [f"t.f{j}" for j in range(5)]


def test_one_line_per_concept_fits_whenever_any_such_selection_fits():
    # alpha's first field in sort order fits the budget on its own but leaves
    # no room for beta; alpha's short field plus beta's fit together.
    items = [{"concept": "alpha", "field": "a." + "x" * 45, "role": "measure"},
             {"concept": "alpha", "field": "a.z", "role": "measure"},
             {"concept": "beta", "field": "b.y", "role": "measure"},
             {"concept": "beta", "field": "b." + "z" * 45, "role": "measure"}]
    out = sa.format_concept_mappings(items, cap=126)
    shown = {concept for concept, _field in _shown(out)}
    assert shown == {"alpha", "beta"}
    assert set(_entries(out)) == {("alpha", "a.z", "measure"), ("beta", "b.y", "measure")}
    assert len(out) <= 126


# --------------------------------------------------------------------------- #
# A field name holding a separator is quoted, so it reads back whole
# --------------------------------------------------------------------------- #
# Fields are joined with ", ", role groups with "; " and roles sit in
# brackets, so a field whose own name holds one of those (an external SiteWise
# property, a connector's column) would read as several fields or as a role.
# Such a field is written as a JSON string; every other field is written as is.

_SEPARATOR_FIELDS = {
    "comma-space": "Model.Sensor, backup",
    "semicolon-space": "Model.Sensor; spare",
    "brackets": "Model.Sensor [raw]",
    "closing-bracket": "Model.Sensor]",
    "opening-bracket": "Model.[Sensor",
    "line-break": "Model.Line\nTwo",
    "carriage-return": "Model.Line\r\nTwo",
    "leading-quote": '"Model.Quoted',
    "trailing-comma": "Model.Trailing,",
    "trailing-semicolon": "Model.Trailing;",
    "backslash-and-comma": "Model.Back\\slash, x",
}


@pytest.mark.parametrize("field", list(_SEPARATOR_FIELDS.values()), ids=list(_SEPARATOR_FIELDS))
def test_a_field_holding_a_separator_reads_back_whole(field):
    items = [{"concept": "speed", "field": field, "role": "measure"},
             {"concept": "speed", "field": "Model.Speed", "role": "measure"},
             {"concept": "speed", "field": "Model.SpeedSetpoint", "role": "correlate"},
             {"concept": "speed", "field": "Model.SpeedNote", "role": ""},
             {"concept": "site", "field": "Model.Site", "role": "identifier"}]
    out = sa.format_concept_mappings(items)
    assert len(_mapping_lines(out)) == 2
    assert len(out.splitlines()) == 2
    assert json.dumps(field, ensure_ascii=False) in out
    assert set(_entries(out)) == _as_entries(items)
    assert len(_entries(out)) == len(items)


#: Unicode line boundaries ``str.splitlines`` breaks on, which ``json.dumps``
#: with ``ensure_ascii=False`` writes literally, plus a non-BMP non-printing
#: character (its escape is a surrogate pair).
_UNICODE_BREAK_FIELDS = {
    "next-line": "Model.Line\u0085Two",
    "line-separator": "Model.Line\u2028Two",
    "paragraph-separator": "Model.Line\u2029Two",
    "file-separator": "Model.Line\u001cTwo",
    "non-bmp-tag": "Model.Tag\U000e0001x",
}


@pytest.mark.parametrize("field", list(_UNICODE_BREAK_FIELDS.values()),
                         ids=list(_UNICODE_BREAK_FIELDS))
def test_a_field_holding_a_unicode_line_break_reads_back_whole(field):
    items = [{"concept": "speed", "field": field, "role": "measure"},
             {"concept": "speed", "field": "Model.Speed", "role": "measure"},
             {"concept": "site", "field": "Model.Site", "role": "identifier"}]
    out = sa.format_concept_mappings(items)
    assert len(out.splitlines()) == 2, out.splitlines()
    assert all(ch.isprintable() for ch in out.replace("\n", "")), repr(out)
    assert set(_entries(out)) == _as_entries(items)
    assert "double quotes" in sa._concept_mappings_prompt(out)


def test_a_field_needing_no_quotes_is_written_as_is():
    items = [{"concept": "speed", "field": f, "role": "measure"}
             for f in ("Model.a,b", "Model.a;b", 'Model.say"hi"', "Model.Back\\slash",
                       "Model.x -> y")]
    out = sa.format_concept_mappings(items)
    assert out == ('- speed -> Model.Back\\slash, Model.a,b, Model.a;b, '
                   'Model.say"hi", Model.x -> y [measure]')
    assert set(_entries(out)) == _as_entries(items)


def test_over_the_cap_a_quoted_field_counts_at_its_shown_width():
    items = ([{"concept": f"c{k:02d}", "field": f"t.f{j:02d}, spare", "role": "measure"}
              for k in range(20) for j in range(6)])
    out = sa.format_concept_mappings(items, cap=900)
    assert len(out) <= 900
    assert "[concept mappings truncated: showing" in out
    assert set(_entries(out)) <= _as_entries(items)
    assert {c for c, _f in _shown(out)} == {f"c{k:02d}" for k in range(20)}


_HEADER = ("KNOWN CONCEPT MAPPINGS (one line per concept: concept -> fields; "
           "each [role] applies to the fields before it):")


def test_the_header_explains_quoting_only_when_a_quoted_field_is_shown():
    plain = sa._concept_mappings_prompt(sa.format_concept_mappings(_STUB_ITEMS))
    assert plain.startswith(f"\n\n{_HEADER}\n- machine-state -> ")
    assert "double quotes" not in plain
    quoted = sa._concept_mappings_prompt(sa.format_concept_mappings(
        [{"concept": "speed", "field": "Model.Sensor, backup", "role": "measure"}]))
    header = quoted.split("\n")[2]
    assert header.startswith("KNOWN CONCEPT MAPPINGS (one line per concept: ")
    assert "a field in double quotes is a JSON string" in header
    assert quoted.split("\n")[3] == '- speed -> "Model.Sensor, backup" [measure]'


# --------------------------------------------------------------------------- #
# The measured workshop systems render byte-identical
# --------------------------------------------------------------------------- #
# sha256 of the rendered section and of the whole prompt section for each
# onboarded workshop system (the 2026-10-18 captures, through provision's item
# cap). None of their fields needs quoting.

_MEASURED_SHA256 = {
    "erp": ("17de5b56c31d56d2ff0083eafa4f068f0a93a0b3111608ecd4d0a2ccbb359877",
            "65a021c34cf2e81ebf9e4d6579ab75128cf4bb1cb8588c7771ec398e53ef5dfe"),
    "mes-ind": ("9bcf660c93302c362970cc6d0c91630c9fbf1270fa1ba47623637d946b720684",
                "ea502e149f14b35493696add3e7374d955c81c9e7248e3f081e8c4d77a3cd867"),
    "mes-mil": ("6ebbc0cf602e0517f325d4708551d5e664365a9d60843c855db93e5161a78f9e",
                "a2fdb9b07d8ffd6ca29c8e8ed5b2aa3b95ec3905c05beb892dcbe2b320b9c9f1"),
    "mes-det": ("a171bf5029c5b147c94730526ee02b152024dd5f12f3ad863ad55841c8823672",
                "0f4802b01529d1c5f83805216b05a05ee729a1bf3a6719f7eb47a6bb6e806f49"),
    "mes-cr": ("036898055daa83a24408e3b6bae481f951ee935b6fa67f6a7c455cf579f865a2",
               "1528c0d6870adab40feac44244c1f00630d47df041d9e6dc241a13211d59d9ad"),
    "plm": ("f5972eab08a311ffe2c68a6e299f8b55005b26cc5882ccf7480cd19a2ccdbf5e",
            "f71c579171f3e3ccd68e7acd489291466a812a461988a95ab3eb23a31f4621b5"),
    "sitewise-direct": ("64333cc839acaed73887f858e9fe3a66a8c3e211fa788220c5e7fa46cd22f2a9",
                        "6e6000dfff23f9881bb63056da5b8515b8fdf3f68e9a218d32acefa92c970f1f"),
}


def _measured_items(system_id: str) -> list:
    import json
    from pathlib import Path
    if system_id == "erp":
        return _erp_live_items()
    if system_id == "sitewise-direct":
        return _sitewise_live_items()
    path = Path(__file__).parent / "fixtures" / "concept_mappings_workshop_systems.json"
    return json.loads(path.read_text())["systems"][system_id]


@pytest.mark.parametrize("system_id", sorted(_MEASURED_SHA256))
def test_the_measured_systems_render_byte_identical(system_id):
    import hashlib
    live = _measured_items(system_id)
    items = _concept_mapping_items(_as_mapping_rows(live),
                                   committable={i["concept"] for i in live})
    out = sa.format_concept_mappings(items)
    section = sa._concept_mappings_prompt(out)
    assert '"' not in out
    assert (hashlib.sha256(out.encode()).hexdigest(),
            hashlib.sha256(section.encode()).hexdigest()) == _MEASURED_SHA256[system_id]
