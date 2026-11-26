# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The route seed passes each system's onboarded asset coverage to the model.

Covers the two inventory reads, the compact rendering, defensive parsing, the
field cap and shrink pass, the Knowledge Graph trace line, the prompt bullet in
every variant, the workshop-scale budget (coverage survives whole and nothing
else is newly trimmed) and the no-literal rule for the changed engine text.
"""
from __future__ import annotations

import inspect
import json
import logging

import pytest

from agents import asset_coverage as ac
from agents import orchestrator as orch
from agents import route_seed_trace, runtime_app
from agents.connection_types import default_capabilities, default_query_guidance
from agents.demo import systems as demo_systems
from agents.demo import workshop
from agents.guidance_scope import GuidanceScope, append_note

_REAL_BUILD_ROUTE_SEED = orch.build_route_seed

_COVERAGE = json.dumps({"anchor": "site", "sites": [
    {"name": "Site A", "total": 16, "levels": {"area": 1, "line": 3, "equipment": 12}},
    {"name": "Site B", "total": 4, "levels": {"area": 1, "line": 1, "equipment": 2}}],
    "more_sites": 2}, separators=(",", ":"))
_RENDERED = ("Site A: 1 area, 3 lines, 12 equipment; Site B: 1 area, 1 line, "
             "2 equipment; +2 more sites")


def _row(system_id, name, **over):
    row = {"system_id": system_id, "system_name": name, "plants": [],
           "capabilities": None, "connection_type": None, "query_guidance": None}
    row.update(over)
    return row


def _query(inventory, rules=()):
    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return list(rules)
        return []
    return query


def _payload(seed: str) -> dict:
    return json.JSONDecoder().raw_decode(seed.split("RESULT:\n", 1)[1])[0]


def _systems(seed: str) -> dict:
    return {s["id"]: s for s in _payload(seed)["systems"]}


# ---------------------------------------------------------------- the reads

def test_both_inventory_reads_return_the_coverage_next_to_capabilities():
    for cypher in (orch._ROUTE_SEED_INVENTORY_CYPHER, orch._ROUTE_SEED_CYPHER):
        assert "s.asset_coverage AS asset_coverage" in cypher
        ret = cypher[cypher.rindex("RETURN s.id AS system_id"):]
        assert ret.index("s.capabilities AS capabilities") < ret.index(
            "s.asset_coverage AS asset_coverage")


# ---------------------------------------------------------------- rendering

def test_the_seed_renders_the_coverage_as_one_compact_string():
    seed = _REAL_BUILD_ROUTE_SEED(query=_query([
        _row("sensors", "Sensor Store", asset_coverage=_COVERAGE),
        _row("ledger", "Ledger")]))
    systems = _systems(seed)
    assert systems["sensors"]["asset_coverage"] == _RENDERED
    assert "asset_coverage" not in systems["ledger"]


@pytest.mark.parametrize("raw", [None, "", "{not json", "[]", json.dumps({"sites": "x"}),
                                 json.dumps({"sites": []}), 42])
def test_missing_or_malformed_coverage_is_omitted_never_raised(raw, caplog):
    with caplog.at_level(logging.DEBUG, logger=orch.__name__):
        seed = _REAL_BUILD_ROUTE_SEED(query=_query([
            _row("sensors", "Sensor Store", asset_coverage=raw)]))
    assert seed is not None
    assert "asset_coverage" not in _systems(seed)["sensors"]


def test_a_seed_without_coverage_is_byte_identical_to_one_without_the_column():
    without = [_row("sensors", "Sensor Store"), _row("ledger", "Ledger")]
    with_none = [dict(r, asset_coverage=None) for r in without]
    assert (_REAL_BUILD_ROUTE_SEED(query=_query(with_none))
            == _REAL_BUILD_ROUTE_SEED(query=_query(without)))


def test_the_field_cap_cuts_coverage_at_a_whole_site_entry():
    sites = [{"name": f"Site {i:02d} " + "x" * 40, "total": 9,
              "levels": {"area": 1, "line": 3, "equipment": 5}} for i in range(8)]
    rendered = ac.render_coverage(json.dumps({"anchor": "site", "sites": sites}))
    assert len(rendered) > orch._ROUTE_SEED_ASSET_COVERAGE_FIELD_CAP
    bounded = orch._truncate_asset_coverage(
        rendered, orch._ROUTE_SEED_ASSET_COVERAGE_FIELD_CAP)
    assert len(bounded) <= orch._ROUTE_SEED_ASSET_COVERAGE_FIELD_CAP
    assert bounded.endswith(orch._ROUTE_SEED_TRUNCATION_MARKER)
    kept = bounded[:-len(orch._ROUTE_SEED_TRUNCATION_MARKER)].rstrip("; ").split("; ")
    assert all(entry.endswith("5 equipment") for entry in kept)


def test_the_coverage_does_not_compete_with_existing_seed_content():
    assert "asset_coverage" not in inspect.getsource(orch._shrink_route_seed_prose)
    assert "asset_coverage" not in inspect.getsource(orch._bound_route_seed_prose)
    assert "asset_coverage" not in inspect.getsource(orch._serialize_route_seed)
    compose = inspect.getsource(orch._fit_and_compose_route_seed)
    # Taken out before the fit (and its reserves and yield) runs, put back last.
    assert compose.index("_strip_asset_coverage(payload)") < compose.index("_fit_route_seed(")
    assert compose.rstrip().endswith("return _with_asset_coverage(payload, seed, tail)")
    assert "_ROUTE_SEED_SERIALIZED_BUDGET + _coverage_allowance(" in inspect.getsource(
        orch._with_asset_coverage)


# ---------------------------------------------------------------- trace

def _step_text(monkeypatch, inventory):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    q = _query(inventory)
    monkeypatch.setattr(orch, "build_route_seed",
                        lambda candidate=None, question=None: _REAL_BUILD_ROUTE_SEED(
                            query=q, question=question))
    _augmented, events, _planned = runtime_app._graph_first_prelude("What is there?")
    return events[1]["message"]["content"][0]["toolResult"]["content"][0]["text"]


def test_the_trace_shows_one_line_per_system_with_coverage(monkeypatch):
    text = _step_text(monkeypatch, [_row("sensors", "Sensor Store", asset_coverage=_COVERAGE),
                                    _row("ledger", "Ledger")])
    assert f"- Assets in Sensor Store: {_RENDERED}" in text.splitlines()
    assert sum(1 for line in text.splitlines() if line.startswith("- Assets in ")) == 1


def test_the_trace_discloses_covered_systems_past_its_line_cap():
    payload = {"systems": [{"id": f"s{i}", "name": f"System {i}", "asset_coverage": "Site A: none"}
                           for i in range(9)]}
    lines = route_seed_trace._coverage_lines(payload, route_seed_trace._system_names(payload))
    assert len(lines) == route_seed_trace.MAX_SYSTEMS + 1
    assert lines[-1] == "- (+1 more systems with asset coverage)"
    assert "System 8" not in "\n".join(lines)


def test_the_trace_is_unchanged_when_no_system_has_coverage(monkeypatch):
    text = _step_text(monkeypatch, [_row("sensors", "Sensor Store"), _row("ledger", "Ledger")])
    assert text == (
        "Looked up this question in the knowledge graph before asking any system.\n"
        "- Connected systems: Sensor Store, Ledger\n"
        "- No identifier in this question needed translating between systems.")


# ---------------------------------------------------------------- prompt

_BULLET = "- asset_coverage:"


def _variants(monkeypatch):
    from agents.workflow_guidance import compose_quick_system_prompt
    out = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("ADT_WORKSHOP", flag)
        for mode in ("agent", "orchestrator"):
            out[f"{mode}-workshop{flag}"] = orch._build_orchestrator_prompt(mode)
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    out["quick-agent"] = compose_quick_system_prompt("agent")
    out["quick-orchestrator"] = compose_quick_system_prompt("orchestrator")
    return out


def test_every_prompt_variant_carries_one_coverage_bullet_after_plant(monkeypatch):
    for name, prompt in _variants(monkeypatch).items():
        assert prompt.count(_BULLET) == 1, name
        plant = prompt.index("- plant: which site/plant this system covers")
        bullet = prompt.index(_BULLET)
        ctype = prompt.index("- connection_type:", plant)
        assert plant < bullet < ctype, name
        # The site-scope rule stays attached to the plant bullet.
        assert prompt.index("SITE SCOPE:", plant) < bullet, name


def test_the_bullet_is_a_factual_description_not_a_rule():
    assert orch._ASSET_COVERAGE_BULLET == (
        "- asset_coverage: how many assets this system holds under each site, by level, "
        "counted from its asset hierarchy when it was last onboarded or reclassified")
    for word in ("MUST", "ALWAYS", "NEVER", "must ", "always ", "never "):
        assert word not in orch._ASSET_COVERAGE_BULLET


# ---------------------------------------------------------------- workshop budget

#: Four generic sites with the per-site shape and name lengths of the workshop
#: SiteWise tree (1 area and 3 lines each; 45/21/36/12 equipment, 134 assets).
_WORKSHOP_SHAPE_COVERAGE = json.dumps({"anchor": "site", "sites": [
    {"name": "Site Alpha1", "total": 40, "levels": {"area": 1, "line": 3, "equipment": 36}},
    {"name": "Site Br", "total": 25, "levels": {"area": 1, "line": 3, "equipment": 21}},
    {"name": "Site Charlie", "total": 16, "levels": {"area": 1, "line": 3, "equipment": 12}},
    {"name": "Site Delt", "total": 49, "levels": {"area": 1, "line": 3, "equipment": 45}}]},
    separators=(",", ":"))


def _workshop_catalog() -> list[dict]:
    return [dict(s) for s in demo_systems.DEMO_SYSTEMS] + workshop.registered_systems("live")


def _workshop_inventory(coverage_on: "str | None", learned_notes: int) -> list[dict]:
    rows = []
    for entry in sorted(_workshop_catalog(), key=lambda e: e["id"]):
        ctype = entry["connection_type"]
        guidance = entry.get("query_guidance") or default_query_guidance(ctype)
        if entry["id"] == "mes-ind":
            for i in range(learned_notes):
                guidance = append_note(
                    guidance, f"Learned note {i}: name work orders by number and runs by "
                    f"run number; report the line, never row ids. ({i})",
                    GuidanceScope.QUERY_HINT)
        row = _row(entry["id"], entry["name"], plants=[entry["plant"]],
                   capabilities=entry.get("capabilities") or default_capabilities(ctype),
                   connection_type=ctype, query_guidance=guidance)
        if coverage_on and entry["id"] == coverage_on:
            row["asset_coverage"] = _WORKSHOP_SHAPE_COVERAGE
        rows.append(row)
    return rows


def _workshop_rules() -> list[dict]:
    ids = sorted(e["id"] for e in _workshop_catalog())
    mes = [i for i in ids if i.startswith("mes-")]
    joins = [{"id": f"jp-{m}", "from_system": m, "from_field": "production_runs.run_number",
              "to_system": "erp", "to_field": "shipment_items.batch_id",
              "transform": "identity", "method": "value_exact_match", "confidence": 0.85,
              "provenance": "measured-overlap"} for m in mes]
    joins.append({"id": "jp-steward", "from_system": "mes-ind",
                  "from_field": "work_orders.work_order_number", "to_system": "erp",
                  "to_field": "lot_mes_tracking.work_order_ref", "transform": "identity",
                  "method": "user_confirmed", "confidence": 1.0, "provenance": "steward"})
    joins.append({"id": "jp-llm", "from_system": "plm", "from_field": "parts.part_number",
                  "to_system": "erp", "to_field": "products.part_number",
                  "transform": "identity", "method": "onboarding-llm", "confidence": 0.6})
    identities = [{"id": f"ir-{m}", "concept": "batch", "source_system": m,
                   "source_field": "run_number", "target_system": "erp",
                   "target_field": "batch_id", "source_pattern": "", "target_pattern": "",
                   "method": "value_exact_match", "confidence": 0.85,
                   "provenance": "measured-overlap"} for m in mes]
    identities.append({"id": "ir-pattern", "concept": "batch", "source_system": "mes-ind",
                       "source_field": "run_number", "source_pattern": "R-{id}",
                       "target_system": "erp", "target_field": "batch_id",
                       "target_pattern": "LOT-{id}", "method": "value_core_match",
                       "confidence": 0.9, "provenance": "48 of 50 sampled value pairs"})
    rows = [{"system_id": "erp", "join_paths": joins, "identity_rules": identities,
             "value_mappings": []}]
    for sid in ids:
        rows.append({"system_id": sid, "shared_concepts": [
            {"concept": c, "peer_system": p} for c in ("batch", "equipment-id", "work-order")
            for p in ids if p != sid][:12]})
    return rows


_QUESTION = "Which shipments came from run R-1001?"


def _workshop_seed(coverage_on, learned_notes=2, question=_QUESTION):
    return _REAL_BUILD_ROUTE_SEED(query=_query(
        _workshop_inventory(coverage_on, learned_notes), _workshop_rules()),
        question=question)


def _split(seed: str) -> "tuple[str, str, str]":
    """(header, serialized payload, identifier-translation tail) of a seed."""
    head, rest = seed.split("RESULT:\n", 1)
    _value, end = json.JSONDecoder().raw_decode(rest)
    return head + "RESULT:\n", rest[:end], rest[end:]


def _without_coverage(serialized: str) -> str:
    """The serialized payload with every asset_coverage key removed, re-encoded
    exactly as ``_encode_route_seed`` encodes."""
    value = json.loads(serialized)
    value.pop("asset_coverage_omitted", None)
    for system in value.get("systems") or []:
        system.pop("asset_coverage", None)
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


#: sha256 of the workshop seed WITHOUT coverage as the route-seed budget
#: change built it (just before the coverage field reached it), per
#: learned-note count. Computed by loading that version's
#: ``agents/orchestrator.py`` as a module and building the same fixture;
#: rebuild these if the fixture or the base seed changes. Rebuilt for the
#: plant CMMS registration guidance (open maintenance orders per asset, event 29).
#: Rebuilt again for 2 and 4 when query-hint text stopped riding the seed (one
#: count line per system instead, guidance_scope.orchestrator_document): with
#: that switched off the previous digests come back exactly; 0 is unchanged.
#: Rebuilt for all three when the seed header stopped saying the seed has no
#: row-level mapping values (a ValueMapping now carries its value pairs): the
#: payload and the identifier tail are byte-identical to the previous build,
#: only the header text differs (and the header keeps its length).
#: Rebuilt again for the value-mapping decision of 2026-10-18 (ADR-0035 amendment):
#: a served ValueMapping entry now carries only value_pair_count, never its values
#: (the values travel in the question-scoped IDENTIFIER VALUE MAPPINGS block), and
#: the header was reworded to state that value-level contract. This fixture's seed
#: has no value mappings, so only the HEADER text changed (and it is longer now);
#: the payload and identifier tail are byte-identical to the earlier build. The
#: digests move by the header rewording alone.
#: Rebuilt once more when the header's value-absence claim
#: was qualified to STORED values (a Try-it candidate may show its one pair), so
#: the header grew ~65 chars. Still header-text-only; payload and tail unchanged.
#: Rebuilt once more when the origin note's matching sentence was tightened
#: (describe how identifiers were matched only when asked, or to disclose a
#: translation applied). The note keeps its length; putting the previous note
#: text back into each new seed gives the previous digests exactly.
_BASE_SHA256 = {
    0: "8e5cdab1147e46eca15ad2db98a7740620852c2c2f8c39030b34c2f1b7eb2f1f",
    2: "ddf0333a7d5e145c730045382949506b29b9ec4307e9be0c3766f146012568c8",
    4: "bf2dfc85c0820e1ef45a31e9869bab837841dafbe618f74e5005b32c39b0ab55",
}


@pytest.mark.usefixtures("route_seed_pressure_budget")
@pytest.mark.parametrize("learned_notes", sorted(_BASE_SHA256))
def test_workshop_seed_without_coverage_is_byte_identical_to_the_base(learned_notes):
    import hashlib
    seed = _workshop_seed(None, learned_notes)
    assert hashlib.sha256(seed.encode()).hexdigest() == _BASE_SHA256[learned_notes]


@pytest.mark.parametrize("learned_notes", [0, 2, 4])
def test_coverage_costs_the_workshop_seed_nothing_that_fits_today(learned_notes):
    before = _workshop_seed(None, learned_notes)
    after = _workshop_seed("sitewise-direct", learned_notes)
    b_head, b_payload, b_tail = _split(before)
    a_head, a_payload, a_tail = _split(after)
    assert a_head == b_head
    assert _without_coverage(a_payload) == b_payload
    # The identifier-translation block is the same text, built with the same room.
    assert a_tail == b_tail and "R-1001" in a_tail
    sensor = {s["id"]: s for s in json.loads(a_payload)["systems"]}["sitewise-direct"]
    assert sensor["asset_coverage"] == ac.render_coverage(_WORKSHOP_SHAPE_COVERAGE)
    allowance = len(after) - len(before)
    assert len(b_head) + len(b_payload) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert len(a_head) + len(a_payload) <= orch._ROUTE_SEED_SERIALIZED_BUDGET + allowance
    assert 0 < allowance <= len(sensor["asset_coverage"]) + len(',"asset_coverage":""')


def test_the_translation_block_gets_the_same_room_with_or_without_coverage(monkeypatch):
    rooms = []
    real = orch._identifier_translation_block

    def spy(question, rule_rows, room=None):
        rooms.append(room)
        return real(question, rule_rows, room)
    monkeypatch.setattr(orch, "_identifier_translation_block", spy)
    _workshop_seed(None, 4)
    _workshop_seed("sitewise-direct", 4)
    assert len(rooms) == 2 and rooms[0] == rooms[1]


def _many_covered(count: int, coverage: str) -> list[dict]:
    return [_row(f"sys-{i:02d}", f"System {i:02d}", asset_coverage=coverage)
            for i in range(count)]


def test_the_total_cap_keeps_a_prefix_of_systems_and_discloses_the_rest():
    raw = json.dumps({"anchor": "site", "sites": [
        {"name": f"Site {i}", "total": 5, "levels": {"line": 1, "equipment": 4}}
        for i in range(8)]})
    rendered = ac.render_coverage(raw)
    seed = _REAL_BUILD_ROUTE_SEED(query=_query(_many_covered(6, raw)))
    payload = _payload(seed)
    covered = [s["id"] for s in payload["systems"] if "asset_coverage" in s]
    fit = orch._ROUTE_SEED_ASSET_COVERAGE_TOTAL_CAP // len(rendered)
    assert orch._ROUTE_SEED_ASSET_COVERAGE_TOTAL_CAP == 1024
    assert covered == [f"sys-{i:02d}" for i in range(fit)]
    assert payload["asset_coverage_omitted"] == 6 - fit
    delivered = sum(len(s["asset_coverage"]) for s in payload["systems"] if "asset_coverage" in s)
    assert delivered <= orch._ROUTE_SEED_ASSET_COVERAGE_TOTAL_CAP


def test_the_allowance_never_exceeds_the_total_cap_plus_key_overhead():
    raw = json.dumps({"anchor": "site", "sites": [
        {"name": f"Site {i} " + "x" * 60, "total": 5, "levels": {"equipment": 5}}
        for i in range(8)]})
    plain = _REAL_BUILD_ROUTE_SEED(query=_query(_many_covered(9, None)))
    seed = _REAL_BUILD_ROUTE_SEED(query=_query(_many_covered(9, raw)))
    payload = _payload(seed)
    covered = sum(1 for s in payload["systems"] if "asset_coverage" in s)
    overhead = covered * len(',"asset_coverage":""') + len(',"asset_coverage_omitted":9')
    assert len(seed) - len(plain) <= orch._ROUTE_SEED_ASSET_COVERAGE_TOTAL_CAP + overhead
    assert "asset_coverage_omitted" in payload


@pytest.mark.parametrize("char", ["\\", '"', "\n", "\x01"])
def test_the_allowance_is_bounded_for_names_json_must_escape(char):
    # The cap is charged on the ENCODED length: a name made of characters JSON
    # escapes must not let the serialized delta outgrow the cap.
    raw = json.dumps({"anchor": "site", "sites": [
        {"name": f"S{i}" + char * 40, "total": 1, "levels": {"equipment": 1}}
        for i in range(5)]})
    plain = _REAL_BUILD_ROUTE_SEED(query=_query(_many_covered(3, None)))
    seed = _REAL_BUILD_ROUTE_SEED(query=_query(_many_covered(3, raw)))
    payload = _payload(seed)
    covered = sum(1 for s in payload["systems"] if "asset_coverage" in s)
    overhead = covered * len(',"asset_coverage":""') + len(',"asset_coverage_omitted":3')
    assert len(seed) - len(plain) <= orch._ROUTE_SEED_ASSET_COVERAGE_TOTAL_CAP + overhead
    # Whatever did not fit is disclosed, never silently dropped.
    assert covered + payload.get("asset_coverage_omitted", 0) == 3


def test_no_omitted_key_when_everything_fits():
    seed = _REAL_BUILD_ROUTE_SEED(query=_query(_many_covered(2, _COVERAGE)))
    assert "asset_coverage_omitted" not in _payload(seed)


def test_the_compact_form_carries_no_coverage(monkeypatch, caplog):
    # 1,300 (raised from 1,200): the route-seed header grew ~65 chars for the
    # stored-values wording, so the compact floor is higher;
    # 1,300 still forces the inventory-only compact form with systems omitted.
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 1_300)
    rows = [_row(f"sys-{i:02d}", f"System {i:02d}", capabilities="c" * 400,
                 asset_coverage=_COVERAGE) for i in range(12)]
    with caplog.at_level(logging.WARNING, logger=orch.__name__):
        seed = _REAL_BUILD_ROUTE_SEED(query=_query(rows))
    payload = _payload(seed)
    assert payload.get("inventory_compact") is True
    assert "asset_coverage" not in seed
    assert len(seed) <= 1_300
    # A real compact prefix of the systems, not the empty emergency seed.
    assert seed.startswith(orch._ROUTE_SEED_PARTIAL_HEADER)
    ids = [s["id"] for s in payload["systems"]]
    assert ids and ids == [r["system_id"] for r in rows[:len(ids)]]
    assert payload["systems_omitted"] == len(rows) - len(ids)
    assert "size invariant failed" not in caplog.text
    assert "asset coverage omitted" not in caplog.text


# ---------------------------------------------------------------- no literals

def _story_literals() -> set[str]:
    out = set()
    for entry in _workshop_catalog() + workshop.WORKSHOP_SYSTEMS:
        out.update({entry["id"], entry["name"], entry["plant"]})
    out.discard("All Sites")
    return out | {"CMX-441", "B-2847", "SiteWise"}


def test_no_site_system_or_story_literal_in_the_changed_engine_and_prompt_text():
    from agents import asset_role_pass as arp
    texts = [inspect.getsource(ac), orch._ASSET_COVERAGE_BULLET,
             inspect.getsource(orch._seed_asset_coverage),
             inspect.getsource(orch._truncate_asset_coverage),
             inspect.getsource(orch._strip_asset_coverage),
             inspect.getsource(orch._reinsert_asset_coverage),
             inspect.getsource(orch._with_asset_coverage),
             inspect.getsource(orch._coverage_allowance),
             inspect.getsource(orch._serialize_route_seed),
             inspect.getsource(route_seed_trace._coverage_lines),
             inspect.getsource(arp._write_coverage), inspect.getsource(arp._clear_coverage),
             arp._READ_SYSTEMS, arp._WRITE_COVERAGE, arp._CLEAR_COVERAGE]
    for text in texts:
        for literal in _story_literals():
            assert literal not in text, literal
