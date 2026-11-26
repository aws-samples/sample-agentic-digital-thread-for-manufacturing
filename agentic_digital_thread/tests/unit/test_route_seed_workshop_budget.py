# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The route seed at workshop scale (ADR-0035 amendment 2026-10-08).

Measured offline on the workshop's own manifests: the 8,000-character seed
was over budget at module 6 already, the shrink pass cut the plant systems'
registration guidance first (it cuts the longest prose first), and with a
ninth/tenth system (an MCP Server hub with one exact join) the sensor system's
base went too. Three changes, pinned here on the workshop-shaped fixture
(``_route_seed_workshop_fixture``):

* the budget is 10,000, so every registration base and the shared
  connection-type default arrive whole in every state (the module 6 sensor
  note is a query hint: since 2026-10-16 it reaches the SiteWise agent with each
  question and the seed carries only its count);
* the IDENTIFIER OWNERS block gets room reserved BEFORE the seed is fitted
  (it used to get only what tier-2 trimming left, 8-246 characters, so it was
  never delivered): the whole block when tier-2 detail pays for it, else its
  smallest form, never at the cost of a tier-0/1 rule or a system;
* a shared section cut to nothing no longer leaves its systems pointing at it.
"""
from __future__ import annotations

import logging
import threading

import pytest

from agents import identifier_translation as it
from agents import orchestrator as orch
from agents.connection_types import POSTGRESQL, default_query_guidance
from agents.guidance_scope import DELIVERY_FIELD_CODES, SHARED_GUIDANCE_FIELD, shared_reference
from tests.unit import _route_seed_scale_fixture as scale
from tests.unit import _route_seed_workshop_fixture as wf

_STATES = sorted(wf.STATES)
_HUB_STATES = [name for name in _STATES if wf.HUB_ID in wf.STATES[name]]
_PLAIN_STATES = [name for name in _STATES if name not in _HUB_STATES]


def _delivered(seed: str, ids) -> dict:
    payload, tail = wf.decode(seed)
    systems = {s["id"]: s for s in payload["systems"]}
    tier1 = {row["id"] for pair in wf.tier1_pairs(ids) for row in pair}
    kept = {rule.get("id") for s in payload["systems"]
            for rules in (s.get("rules") or {}).values() for rule in rules}
    return {
        "payload": payload, "tail": tail, "systems": systems,
        "bases_whole": {sid: wf.base_guidance(sid) in (systems[sid].get("query_guidance") or "")
                        for sid in wf.registration_ids(ids)},
        # The module 6 note is a query hint: since 2026-10-16 the SiteWise
        # agent gets it with each question and the seed carries only a count.
        "note_referenced": (wf.SENSOR_NOTE not in seed and str(
            systems[wf.SENSOR_ID].get("query_hints") or "").startswith(
                f"1 query hint is delivered to {wf.SENSOR_ID}")),
        "tier1_kept": len(tier1 & kept), "tier1_total": len(tier1),
        "shared": payload.get("shared_guidance") or {},
        "tier2_kept": len(kept - tier1) + sum(len(s.get("shared_concepts") or [])
                                               for s in payload["systems"]),
    }


@pytest.mark.parametrize("state", _STATES)
def test_every_registration_base_and_shared_default_arrive_whole(state):
    ids = wf.STATES[state]
    seed = wf.build_seed(ids)
    got = _delivered(seed, ids)

    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert wf.registration_ids(ids) and all(got["bases_whole"].values()), got["bases_whole"]
    assert got["note_referenced"]
    assert default_query_guidance(POSTGRESQL).strip() in got["shared"][POSTGRESQL]
    assert got["tier1_kept"] == got["tier1_total"]


#: The tight budget the registration-base raise was measured against. Pinned
#: locally (not scale.LEVER_BUDGET) so the raise of that shared constant for
#: the value-mapping header growth does not move this demonstration.
_PREVIOUS_CUT_BUDGET = 8_000


@pytest.mark.parametrize("state", ["module 8", *_HUB_STATES])
def test_the_previous_budget_cut_a_registration_base(monkeypatch, state):
    """The measurement behind the raise: at the fixture's 8,000 a plant
    system's registration guidance does not arrive whole from module 8 on."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", _PREVIOUS_CUT_BUDGET)
    ids = wf.STATES[state]
    got = _delivered(wf.build_seed(ids), ids)

    assert not all(got["bases_whole"].values()), got["bases_whole"]


# --------------------------------------------------------------------------- #
# Room for the IDENTIFIER OWNERS block
# --------------------------------------------------------------------------- #
def _run_line(tail: str) -> bool:
    return any(line.startswith(f"- {wf.RUN_NUMBER} → ") for line in tail.splitlines())


def _candidate_systems_for_run_number(ids) -> set:
    """Systems that own a run-number-shaped identifier: every MES in the state
    (run_number -> ERP batch) plus the ERP target."""
    owners = it.find_owners(wf.IDENTIFIER_QUESTION, orch._seed_identity_rules(wf.graph(ids)(orch._ROUTE_SEED_CYPHER)),
                            orch._seed_rule_dicts(wf.graph(ids)(orch._ROUTE_SEED_CYPHER), "join_paths"))
    return {o["from_system"] for o in owners} | {o["to_system"] for o in owners}


def _compact_owners_name_every_candidate(tail: str, ids) -> bool:
    """True when the tail carries the compact smallest form and it names every
    candidate owner system."""
    compact = [line for line in tail.splitlines()
               if line.startswith("- candidate owner systems")]
    if len(compact) != 1:
        return False
    return all(sid in compact[0] for sid in _candidate_systems_for_run_number(ids))


@pytest.mark.parametrize("state", _STATES)
def test_an_identifier_question_delivers_the_owners_block_inside_the_budget(state):
    ids = wf.STATES[state]
    seed = wf.build_seed(ids, wf.IDENTIFIER_QUESTION)
    got = _delivered(seed, ids)

    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    # Either the full per-identifier lines, or the smallest compact form — and
    # the compact form names every candidate system, never a run line.
    assert it.OWNERS_HEADER in got["tail"]
    assert _run_line(got["tail"]) or _compact_owners_name_every_candidate(got["tail"], ids)
    assert got["tier1_kept"] == got["tier1_total"]  # the reserve never costs a tier-0/1 rule
    assert len(got["systems"]) == len(ids) and "systems_omitted" not in got["payload"]


@pytest.mark.parametrize("state", _PLAIN_STATES)
def test_tier2_detail_pays_for_the_whole_owners_block(state):
    """Without the hub there is room to spare once the budget is 10,000: the
    whole block arrives, every base stays whole, the note stays referenced, and
    the prose is exactly what the same seed delivers without the question; only
    tier-2 detail gave way."""
    ids = wf.STATES[state]
    plain, asked = (_delivered(wf.build_seed(ids, q), ids)
                    for q in (None, wf.IDENTIFIER_QUESTION))

    assert it.OWNERS_OMITTED.format(n=1)[:8] not in asked["tail"]
    assert asked["tail"].count(f"- {wf.RUN_NUMBER} → ") == len(wf._of_type("MES"))
    assert all(asked["bases_whole"].values()) and asked["note_referenced"]
    for sid, system in plain["systems"].items():
        assert asked["systems"][sid].get("query_guidance") == system.get("query_guidance")
    assert asked["shared"] == plain["shared"]
    assert asked["tier2_kept"] < plain["tier2_kept"]


@pytest.mark.usefixtures("route_seed_pressure_budget")
@pytest.mark.parametrize("state", _HUB_STATES)
def test_with_the_hub_the_smallest_form_names_every_candidate_system(state):
    """With the hub's exact join the whole per-identifier block would cost
    prose, so the seed makes room for the smallest form: the header plus ONE
    compact line that names every candidate owner system, so the
    question's own-plant MES is never dropped behind equal-ranked owners."""
    ids = wf.STATES[state]
    got = _delivered(wf.build_seed(ids, wf.IDENTIFIER_QUESTION), ids)

    owner_lines = [line for line in got["tail"].splitlines() if line.startswith("- ")]
    assert len(owner_lines) == 1 and owner_lines[0].startswith("- candidate owner systems")
    assert not _run_line(got["tail"])  # not the full per-identifier form
    for sid in _candidate_systems_for_run_number(ids):
        assert sid in owner_lines[0], f"{sid} missing from the smallest owners form"


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_at_module_8_with_the_hub_the_generic_default_yields_not_the_plant_systems(monkeypatch):
    """The shared PostgreSQL default is the longest prose field, so it is cut
    first; once it is empty its references go too, and the plant systems'
    registration guidance arrives whole beside the block (the sensor note
    rides as its count line).

    Budget pinned to 10,100 (just above the fixture's 10,000): the value-mapping
    decision's longer route-seed header shifted this narrow window up ~100
    chars. 10,100 reproduces exactly the state the test demonstrates (the
    generic default yielded, every plant base whole), with the state fixture
    unchanged."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_100)
    ids = wf.STATES["module 8 + hub"]
    got = _delivered(wf.build_seed(ids, wf.IDENTIFIER_QUESTION), ids)

    assert got["shared"][POSTGRESQL] == ""
    assert all(got["bases_whole"].values()) and got["note_referenced"]


def test_a_question_without_an_owned_identifier_is_fitted_exactly_as_before(monkeypatch):
    ids = wf.STATES["module 8 + hub"]

    def no_trial_fit(*_args, **_kwargs):
        raise AssertionError("a reserve fit ran for a question with no owners")
    monkeypatch.setattr(orch, "_held_fit", no_trial_fit)
    question = "Is the CMX-441 vibration trending up this week?"
    rows = wf.graph(ids)(orch._ROUTE_SEED_CYPHER)

    assert orch._owner_room_reserves(question, rows) == ()
    assert orch._owner_room_reserves(None, rows) == ()
    assert wf.build_seed(ids, question).startswith(orch._ROUTE_SEED_HEADER)


def test_reserves_count_what_the_whole_identifier_tail_is_charged_with():
    """The fit reserves room for the WHOLE identifier tail, largest first:
    the translations + the whole owners block + the note, then the
    translations + note + the owners' smallest form. Each carries the
    translations and the wildcard note; capped at two values so the fit never
    exceeds three full fits."""
    ids = wf.STATES["module 6"]
    rows = wf.graph(ids)(orch._ROUTE_SEED_CYPHER)
    translations, owners, note = orch._identifier_block_parts(wf.IDENTIFIER_QUESTION, rows)
    whole, smallest = it.owners_block_sizes(owners)
    base = len(translations) + len(note)

    reserves = orch._owner_room_reserves(wf.IDENTIFIER_QUESTION, rows)
    assert reserves == tuple(
        dict.fromkeys(r for r in (base + whole, base + smallest) if r > 0))
    assert len(reserves) <= 2  # never more than three full fits (one plain + two)
    assert whole > smallest > len(it.OWNERS_HEADER)


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_only_the_fits_behind_the_kept_seed_report_their_truncations(monkeypatch, caplog):
    """A discarded fit's GUIDANCE_DELIVERY_TRUNCATION lines are never emitted.
    The plain fit reached the budget-pressure passes, so each reserve fit
    CONTINUES it; the kept seed is the plain fit plus the smallest-reserve
    continuation, and exactly those two report (the whole-block try does not)."""
    real = orch._fit_bounded_route_seed
    budgets = []

    def marked(payload, budget):
        budgets.append(budget)
        orch.logger.warning("FIT budget=%s", budget)
        return real(payload, budget)
    monkeypatch.setattr(orch, "_fit_bounded_route_seed", marked)
    ids = wf.STATES["module 8 + hub"]
    rows = wf.graph(ids)(orch._ROUTE_SEED_CYPHER)
    budget = orch._ROUTE_SEED_SERIALIZED_BUDGET
    whole, smallest = orch._owner_room_reserves(wf.IDENTIFIER_QUESTION, rows)

    with caplog.at_level(logging.WARNING, logger=orch.logger.name):
        wf.build_seed(ids, wf.IDENTIFIER_QUESTION)
    marks = [r.getMessage() for r in caplog.records if r.getMessage().startswith("FIT ")]

    assert budgets == [budget, budget - whole, budget - smallest]
    assert marks == [f"FIT budget={budget}", f"FIT budget={budget - smallest}"]
    assert any(r.getMessage().startswith("GUIDANCE_DELIVERY_TRUNCATION") for r in caplog.records)


def test_held_logs_hold_only_this_threads_records(caplog):
    other: list = []
    with caplog.at_level(logging.INFO, logger=orch.logger.name):
        with orch._held_seed_logs() as held:
            orch.logger.info("held here")
            worker = threading.Thread(target=lambda: orch.logger.info("from another thread"))
            worker.start()
            worker.join()
            other.extend(r.getMessage() for r in caplog.records)
        orch._replay_seed_logs(held)

    assert other == ["from another thread"]
    assert [r.getMessage() for r in held] == ["held here"]
    assert [r.getMessage() for r in caplog.records] == ["from another thread", "held here"]


def test_held_logs_hold_this_thread_even_with_thread_metadata_disabled(caplog):
    """With logging.logThreads = False a LogRecord carries thread=None,
    so comparing record.thread let this thread's held records leak through. The
    filter now reads threading.get_ident(), which is still correct, so the
    record is held, not emitted immediately. logThreads is restored in teardown."""
    previous = logging.logThreads
    logging.logThreads = False
    try:
        with caplog.at_level(logging.INFO, logger=orch.logger.name):
            with orch._held_seed_logs() as held:
                orch.logger.info("held with no thread metadata")
                leaked = [r.getMessage() for r in caplog.records]
            orch._replay_seed_logs(held)
    finally:
        logging.logThreads = previous

    assert leaked == []  # nothing leaked while held, despite thread=None
    assert [r.getMessage() for r in held] == ["held with no thread metadata"]
    assert [r.getMessage() for r in caplog.records] == ["held with no thread metadata"]


# --------------------------------------------------------------------------- #
# A shared section cut to nothing leaves no reference behind
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("budget", [None, scale.LEVER_BUDGET])
@pytest.mark.parametrize("state", _STATES)
@pytest.mark.parametrize("question", [None, wf.IDENTIFIER_QUESTION])
def test_no_system_points_at_an_emptied_shared_section(monkeypatch, budget, state, question):
    if budget is not None:
        monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    ids = wf.STATES[state]
    got = _delivered(wf.build_seed(ids, question), ids)

    for key, text in got["shared"].items():
        if text:
            continue
        reference = shared_reference(key)
        assert not [sid for sid, s in got["systems"].items()
                    if (s.get("query_guidance") or "").startswith(reference)]


def test_an_emptied_shared_section_is_still_reported_on_every_system_it_served(monkeypatch):
    """Dropping the reference drops no accounting: the cut stays reported on
    each system whose base lived in the section."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", scale.LEVER_BUDGET)
    ids = wf.STATES["module 6"]
    got = _delivered(wf.build_seed(ids), ids)
    generic = [sid for sid in ids if sid not in wf.registration_ids(ids)
               and wf.MANIFEST[sid]["connection_type"] == POSTGRESQL]

    assert got["shared"][POSTGRESQL] == ""
    aggregate = got["payload"]["guidance_delivery_truncation"]["systems"]
    shared_code = DELIVERY_FIELD_CODES[SHARED_GUIDANCE_FIELD]
    base = len(default_query_guidance(POSTGRESQL).strip())
    for sid in generic:
        assert "query_guidance" not in got["systems"][sid]  # nothing left to say
        chars_dropped, _notes, _cut, codes = aggregate[sid][:4]
        assert shared_code in codes and chars_dropped >= base


def test_a_shared_reference_is_not_re_added_when_the_system_is_re_fitted():
    """The shrink pass re-fits a system's own fields; once its shared section
    is empty the reference must not come back with them."""
    rows = [{"system_id": f"system-{i}", "system_name": f"System {i}",
             "connection_type": POSTGRESQL,
             "query_guidance": default_query_guidance(POSTGRESQL)} for i in range(3)]
    payload = {"systems": list(orch._route_seed_inventory(rows).values()),
               "rule_grain_cap_per_kind": 50}
    assert orch._share_default_guidance(payload)
    orch._bound_shared_guidance(payload, orch._ROUTE_SEED_GUIDANCE_FIELD_CAP,
                                only=POSTGRESQL, override=1)
    system = payload["systems"][0]

    assert payload["shared_guidance"][POSTGRESQL] == ""
    assert system["query_guidance"] == ""
    orch._bound_route_seed_guidance(system, orch._ROUTE_SEED_GUIDANCE_FIELD_CAP)
    assert system["query_guidance"] == ""
