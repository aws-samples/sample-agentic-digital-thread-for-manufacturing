# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identity-rule patterns: what 46fe387f still got wrong.

* Reader agreement at write time. The route seed (a person's question: one
  word, any case) and the resolver (one whole stored value: any characters,
  exact case) keep their own reading; a rule is only written when, on the
  values provision reads, the two readers translate every value alike
  (``resolve.reader_disagreement``). ``LOT-{id} -> B-{id}`` over a column
  holding ``LOT-A-B`` (resolver only) or ``lot-123`` (route seed only) is
  refused with the value named.
* Legacy rows keep their old meaning. Every IdentityRule this code writes
  carries ``pattern_grammar = 'literal'``; a stored row without it is read
  exactly as before: the resolver reads its literal text as a regular
  expression, the route seed literally, as it always did. A re-provision
  rewrites onboarding rules with the marker; a taught rule moves when it is
  taught again.

Neutral fixture: a shift log (``log-sys``) and a line's stop records
(``line-sys``). SQLite stands in for PostgreSQL; the graph is a fake.
"""
from __future__ import annotations

import logging
import os
import re
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents import identifier_translation, resolve  # noqa: E402
from agents.models import LearnRequest, ProvisionRequest  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog, RuleCheckDb  # noqa: E402

SRC_SYS, SRC_SCHEMA = "log-sys", "log_db"
TGT_SYS, TGT_SCHEMA = "line-sys", "line_db"


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


@pytest.fixture
def world(monkeypatch):
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})) or True)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links", lambda _sid: [])
    monkeypatch.setattr(onboard, "catalog", FakeCatalog({
        TGT_SYS: {"id": TGT_SYS, "schema": TGT_SCHEMA, "connection_type": "PostgreSQL"}}))
    state = SimpleNamespace(writes=writes)

    def build(source):
        db = RuleCheckDb({
            SRC_SCHEMA: {"shift_notes": (["note_id", "code"], _rows(source))},
            TGT_SCHEMA: {"stops": (["stop_id", "ref"], _rows(["B-1"]))},
        })
        monkeypatch.setattr(onboard, "pg", db)
        return db

    state.build = build
    return state


def _rule(src, tgt, **over) -> dict:
    base = {"concept": "lot-ref", "source_system": SRC_SYS, "source_field": "shift_notes.code",
            "source_pattern": src, "target_system": TGT_SYS, "target_field": "stops.ref",
            "target_pattern": tgt, "confidence": 0.8, "reasoning": "model rule"}
    base.update(over)
    return base


def _run(*rules) -> onboard._ProvisionState:
    body = ProvisionRequest(system_id=SRC_SYS, system_name="Shift Log", schema=SRC_SCHEMA,
                            rules=[dict(r) for r in rules],
                            mappings=[{"table": "shift_notes", "field": "shift_notes.code",
                                       "concept": "lot-ref"}],
                            connection_type="PostgreSQL", rules_from_checkpoint=True,
                            resolve_job_id=onboard._bind_resolve_rules(SRC_SYS, rules))
    ctx = onboard._make_provision_state(body)
    ctx.tables_seen = {"shift_notes"}
    onboard._dp_identity_rules(ctx)
    return ctx


def _merges(writes) -> list:
    return [(c, p) for c, p in writes if c.startswith("MERGE (ir:IdentityRule")]


def _reasons(ctx) -> list:
    return [r["reason"] for r in ctx.rules_rejected]


# ── 1. the two readers must agree on the values provision reads ─────────────

@pytest.mark.parametrize("values,odd", [
    (["LOT-123", "LOT-A-B"], "LOT-A-B"),    # the resolver translates it, the route seed does not
    (["LOT-123", "lot-456"], "lot-456"),    # the route seed translates it, the resolver does not
    (["LOT-123", "LOT-456"], None),
])
def test_reader_disagreement_names_the_first_value_read_differently(values, odd):
    """REPRODUCTION (the check did not exist): one function compares the
    route seed's reading (the value as a whole question) with the resolver's
    (``transform_id`` on the value)."""
    assert resolve.reader_disagreement("LOT-{id}", "B-{id}", values) == odd


def test_the_reviewers_case_is_rejected_naming_the_value(world):
    """REPRODUCTION (the reviewer's case). A column holding LOT-A-B and
    lot-123: 46fe387f wrote LOT-{id} -> B-{id}, which the resolver applies to
    LOT-A-B and the route seed to lot-123."""
    world.build(["LOT-A-B", "lot-123"])
    ctx = _run(_rule("LOT-{id}", "B-{id}"))
    assert _merges(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "the two identifier readers would read value 'LOT-A-B' differently" in reason


def test_a_value_only_the_route_seed_reads_is_found_and_rejected(world):
    """REPRODUCTION. The column also holds lower-case lot-456, which only the
    route seed (case-insensitive) translates; 46fe387f's case-sensitive
    candidate read never saw it and wrote the rule."""
    world.build(["LOT-123", "lot-456"])
    ctx = _run(_rule("LOT-{id}", "B-{id}"))
    assert _merges(world.writes) == []
    (reason,) = _reasons(ctx)
    assert "would read value 'lot-456' differently" in reason


def test_a_hyphenated_work_order_rule_is_written(world):
    """Guard: WO-{id} -> MFG-WO-{id} over WO-9921: both readers translate it
    to MFG-WO-9921."""
    world.build(["WO-9921", "WO-9935"])
    ctx = _run(_rule("WO-{id}", "MFG-WO-{id}"))
    assert ctx.rules_rejected == [] and len(_merges(world.writes)) == 1


def test_the_workshop_batch_rule_is_written(world):
    """Guard (workshop): B-{id} -> LOT-2026-0{id} over B-2847."""
    world.build(["B-2847", "B-2848", "B-3150"])
    ctx = _run(_rule("B-{id}", "LOT-2026-0{id}"))
    assert ctx.rules_rejected == [] and len(_merges(world.writes)) == 1


def test_the_learn_route_checks_the_readers_on_a_value_built_from_the_pattern(monkeypatch):
    """REPRODUCTION: the taught-rule route (``/api/thread/learn``) carries no
    example identifier, so it checks a value built from the pattern itself
    (each placeholder an alphanumeric token). That value is in the pattern's
    own case and holds only word characters, so it cannot expose a case or
    placeholder-width difference; the test pins that the check runs (a
    reader that disagrees on it refuses the rule) and that an ordinary pair
    is created."""
    writes = []
    monkeypatch.setattr(resolve, "neptune_query", lambda q, p=None: [{"s.id": TGT_SYS}])
    monkeypatch.setattr(resolve, "get_schema", lambda sid: "")
    monkeypatch.setattr(resolve, "neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    checked = []
    real = resolve.reader_disagreement
    monkeypatch.setattr(resolve, "reader_disagreement",
                        lambda s, t, values: checked.append(list(values)) or real(s, t, values))
    body = LearnRequest(concept="batch", source_system=SRC_SYS, source_field="run_code",
                        source_pattern="R-{id}", target_system=TGT_SYS, target_field="lot_ref",
                        target_pattern="L-{id}")
    assert resolve.thread_learn(body)["status"] == "created"
    assert checked == [["R-A1"]]
    monkeypatch.setattr(resolve, "reader_disagreement", lambda *_a: "R-A1")
    writes.clear()
    refused = resolve.thread_learn(body)
    assert "would read value 'R-A1' differently" in refused.get("error", "") and writes == []


# ── 2. the pattern grammar marker ───────────────────────────────────────────

def _row(**over) -> dict:
    """A stored IdentityRule as ``resolve._fetch_identity_rules`` returns it."""
    base = {"id": "ir-legacy", "src_sys": SRC_SYS, "src_field": "code", "src_pat": "LINE[0-9]+-{id}",
            "tgt_sys": TGT_SYS, "tgt_field": "ref", "tgt_pat": "L-{id}", "conf": 0.9,
            "rule_type": None}
    base.update(over)
    return base


def test_an_unmarked_legacy_row_keeps_its_regular_expression_reading_in_the_resolver():
    """REPRODUCTION. A row stored before this change (no pattern_grammar):
    ``LINE[0-9]+-{id}`` translated LINE7-42 in the resolver, and 46fe387f
    silently stopped translating it."""
    hit = resolve._rule_target(_row(), "LINE7-42")
    assert hit is not None and hit[0] == "L-42"


def test_a_marked_row_is_read_literally_in_the_resolver():
    """Guard: a row written with the literal grammar reads ``[0-9]+`` as the
    characters themselves."""
    assert resolve._rule_target(_row(grammar="literal"), "LINE7-42") is None
    assert resolve._rule_target(_row(grammar="literal"), "LINE[0-9]+-42")[0] == "L-42"


def test_the_route_seed_reads_every_row_literally_as_it_always_did():
    """Guard: the route seed's reading of an unmarked row is unchanged."""
    assert identifier_translation.apply("LINE[0-9]+-{id}", "L-{id}", "LINE7-42") is None


def test_the_join_path_transform_reads_a_legacy_row_with_its_old_grammar(monkeypatch):
    """REPRODUCTION: ``_join_transform_value`` (a joined row's value through a
    stored rule) applies an unmarked row as before; a marked row literally."""
    rows = [{"src_pat": "LINE[0-9]+-{id}", "tgt_pat": "L-{id}"}]
    monkeypatch.setattr(resolve, "neptune_query", lambda *_a, **_k: rows)
    assert resolve._join_transform_value(SRC_SYS, TGT_SYS, "LINE7-42") == "L-42"
    rows[0]["grammar"] = "literal"
    assert resolve._join_transform_value(SRC_SYS, TGT_SYS, "LINE7-42") == "LINE7-42"


def test_the_stored_row_reads_return_the_grammar_marker(monkeypatch):
    """REPRODUCTION: both stored-row reads return the marker."""
    seen = []
    monkeypatch.setattr(resolve, "neptune_query", lambda cypher, p=None: seen.append(cypher) or [])
    resolve._fetch_identity_rules("lot-ref")
    resolve._join_transform_value(SRC_SYS, TGT_SYS, "X-1")
    assert all("ir.pattern_grammar AS grammar" in cypher for cypher in seen) and len(seen) == 2


def test_reading_a_legacy_row_with_regex_characters_is_logged_once(caplog):
    """REPRODUCTION: a legacy row read with the pre-2026-10-21 grammar is
    logged once per pattern; a marked row and an ordinary pattern are not."""
    pattern = "STAMP(9)+-{id}"
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            resolve.transform_id(pattern, "S-{id}", "STAMP9-1", literal=False)
        resolve.transform_id(pattern, "S-{id}", "STAMP9-1")
        resolve.transform_id("B-{id}", "LOT-2026-0{id}", "B-2847", literal=False)
        identifier_translation.apply(pattern, "S-{id}", "STAMP9-1")
    lines = [r.getMessage() for r in caplog.records if "pre-2026-10-21 grammar" in r.getMessage()]
    assert len(lines) == 1 and pattern in lines[0]
    assert "re-provision or re-teach it to update" in lines[0]


def test_every_provision_write_carries_the_marker(world):
    """REPRODUCTION: the provision writer's IdentityRule MERGE sets
    pattern_grammar = 'literal'."""
    world.build(["B-2847", "B-3150"])
    _run(_rule("B-{id}", "LOT-2026-0{id}"), _rule("", "", concept="batch"))
    merges = _merges(world.writes)
    assert len(merges) == 2
    for cypher, params in merges:
        assert "ir.pattern_grammar = $grammar" in cypher and params["grammar"] == "literal"


def test_a_taught_rule_carries_the_marker(monkeypatch):
    """REPRODUCTION: the /api/thread/learn CREATE sets the marker."""
    writes = []
    monkeypatch.setattr(resolve, "neptune_query", lambda q, p=None: [{"s.id": TGT_SYS}])
    monkeypatch.setattr(resolve, "get_schema", lambda sid: "")
    monkeypatch.setattr(resolve, "neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    resolve.thread_learn(LearnRequest(concept="batch", source_system=SRC_SYS, source_field="run_code",
                                      source_pattern="R-{id}", target_system=TGT_SYS,
                                      target_field="lot_ref", target_pattern="L-{id}"))
    create, params = writes[0]
    assert "pattern_grammar: $grammar" in create and params["grammar"] == "literal"


def test_an_accepted_proposal_carries_the_marker(monkeypatch):
    """REPRODUCTION: the proposal-accept writer's MERGE sets the marker."""
    from agents import self_learning
    writes = []
    monkeypatch.setattr(self_learning, "_neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    self_learning._apply_identity_rule(
        {"from_system": SRC_SYS, "from_field": "t.code", "to_system": TGT_SYS, "to_field": "u.ref"},
        "subj-1", 1, "")
    merge, params = writes[0]
    assert re.search(r"ir\.pattern_grammar = \$grammar", merge) and params["grammar"] == "literal"
