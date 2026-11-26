# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the ``required_systems`` availability filter (Task 1.3 / E1).

``prompts._filter_satisfiable`` drops prompts whose ``required_systems`` are not
satisfied by the ONBOARDED catalog — a prompt is kept iff EVERY entry matches
(case-insensitively) an onboarded system id OR system_type. FAIL-OPEN: a catalog
read error OR an EMPTY onboarded set serves everything unfiltered.

Also lints the demo pack migration: every ``required_systems`` entry in
``DEMO_PROMPTS`` is either a canonical id from ``agents.demo.systems`` or one of
the deployment-dependent OT type labels (SiteWise / IIoT / Historian).

No AWS, no DynamoDB — catalog seeded in-memory, ``_prompt_store`` monkeypatched.
"""

import pytest

from agents import demo, prompts
from agents.catalog import catalog
from agents.demo.prompt_pack import DEMO_PROMPTS
from agents.demo.systems import DEMO_SYSTEMS


class _FakePromptStore:
    """Tiny in-memory stand-in for PromptTemplateStorage."""

    def __init__(self):
        self._items = {}

    def list_all(self, source=None):
        if source is None:
            return list(self._items.values())
        return [v for v in self._items.values() if v.get("source") == source]

    def save(self, item):
        self._items[item["template_id"]] = item


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakePromptStore()
    monkeypatch.setattr(prompts, "_prompt_store", store)
    return store


@pytest.fixture
def seeded_catalog():
    """Seed the catalog singleton with the six demo PG systems, all onboarded."""
    saved = catalog._systems
    catalog._systems = [{**dict(s), "status": "onboarded"} for s in DEMO_SYSTEMS]
    try:
        yield catalog
    finally:
        catalog._systems = saved


@pytest.fixture
def empty_catalog():
    saved = catalog._systems
    catalog._systems = []
    try:
        yield catalog
    finally:
        catalog._systems = saved


def _prompt(required, tid="t1"):
    return {"template_id": tid, "source": "user", "text": "q",
            "required_systems": required}


# ---------------------------------------------------------------------------
# Core set check: satisfiable vs unsatisfiable
# ---------------------------------------------------------------------------

def test_excluded_when_a_required_system_is_not_onboarded(empty_catalog):
    catalog._systems = [{**dict(DEMO_SYSTEMS[0]), "status": "onboarded"}]  # erp only
    out = prompts._filter_satisfiable([_prompt(["erp", "cmms"])])
    assert out == []


def test_included_when_all_required_systems_onboarded(seeded_catalog):
    out = prompts._filter_satisfiable([_prompt(["erp", "cmms"])])
    assert len(out) == 1


def test_match_is_case_insensitive(seeded_catalog):
    out = prompts._filter_satisfiable([_prompt(["ERP", "Cmms"])])
    assert len(out) == 1


def test_type_label_matches_any_onboarded_system_of_that_type(seeded_catalog):
    # Legacy user prompts saved with type labels keep working: "MES" matches
    # the onboarded mes-* systems' system_type.
    out = prompts._filter_satisfiable([_prompt(["MES", "erp"])])
    assert len(out) == 1


def test_sitewise_type_included_only_when_such_a_system_is_onboarded(seeded_catalog):
    # The live case: [cmms, IIoT] excluded with only the six PG systems...
    assert prompts._filter_satisfiable([_prompt(["cmms", "IIoT"])]) == []
    assert prompts._filter_satisfiable([_prompt(["SiteWise"])]) == []
    # ...included once a highbyte/iiot-typed system is onboarded (any id).
    catalog.add({"id": "highbyte-uns", "name": "HighByte", "plant": "All Sites",
                 "system_type": "IIoT", "connection_type": "I3X",
                 "schema": "", "status": "onboarded"})
    catalog.add({"id": "sw-1", "name": "SiteWise", "plant": "All Sites",
                 "system_type": "SiteWise", "connection_type": "SiteWise",
                 "schema": "", "status": "onboarded"})
    assert len(prompts._filter_satisfiable([_prompt(["cmms", "IIoT"])])) == 1
    assert len(prompts._filter_satisfiable([_prompt(["SiteWise"])])) == 1


def test_registered_but_not_onboarded_does_not_satisfy(empty_catalog):
    catalog._systems = [{**dict(DEMO_SYSTEMS[0]), "status": "registered"},
                        {**dict(DEMO_SYSTEMS[4]), "status": "onboarded"}]  # cmms
    assert prompts._filter_satisfiable([_prompt(["erp"])]) == []
    assert len(prompts._filter_satisfiable([_prompt(["cmms"])])) == 1


def test_empty_required_systems_always_kept(seeded_catalog):
    items = [_prompt([], tid="a"), _prompt(None, tid="b"),
             {"template_id": "c", "source": "user", "text": "q"}]
    assert len(prompts._filter_satisfiable(items)) == 3


# ---------------------------------------------------------------------------
# FAIL-OPEN: catalog error or empty onboarded set → unfiltered
# ---------------------------------------------------------------------------

def test_fail_open_on_empty_onboarded_catalog(empty_catalog):
    items = [_prompt(["erp", "cmms"]), _prompt(["SiteWise"], tid="t2")]
    assert prompts._filter_satisfiable(items) == items


def test_fail_open_on_catalog_read_error(seeded_catalog, monkeypatch):
    def boom():
        raise RuntimeError("catalog unavailable")
    monkeypatch.setattr(catalog, "get_onboarded", boom)
    items = [_prompt(["not-a-system"])]
    assert prompts._filter_satisfiable(items) == items


# ---------------------------------------------------------------------------
# list_prompts wiring: stored rows AND the demo-fallback branch are filtered
# ---------------------------------------------------------------------------

def test_list_prompts_filters_stored_rows(fake_store, seeded_catalog, monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    fake_store.save(_prompt(["erp"], tid="keep"))
    fake_store.save(_prompt(["not-onboarded-sys"], tid="drop"))
    ids = {r["template_id"] for r in prompts.list_prompts()}
    assert ids == {"keep"}


def test_demo_fallback_branch_is_filtered(fake_store, seeded_catalog, monkeypatch):
    # Six PG systems onboarded, no OT system: the two OT-dependent pack prompts
    # (IIoT / Historian) are excluded; every all-PG prompt is served.
    monkeypatch.setenv("ADT_DEMO", "1")
    results = prompts.list_prompts()
    ot_labels = {"sitewise", "iiot", "historian"}
    expected = [e for e in DEMO_PROMPTS
                if not (set(map(str.lower, e["required_systems"])) & ot_labels)]
    assert len(results) == len(expected)
    assert 0 < len(results) < len(DEMO_PROMPTS)


def test_demo_fallback_fail_open_with_no_onboarded_systems(fake_store, empty_catalog,
                                                           monkeypatch):
    # The pre-existing demo-gate contract: with NOTHING onboarded the full pack
    # is served (fail-open) — the library never blanks on a fresh deployment.
    monkeypatch.setenv("ADT_DEMO", "1")
    assert len(prompts.list_prompts()) == len(demo.demo_prompts())


# ---------------------------------------------------------------------------
# Pack lint: canonical ids (or OT type labels) only
# ---------------------------------------------------------------------------

def test_pack_required_systems_are_canonical_ids_or_ot_type_labels():
    canonical = {s["id"] for s in DEMO_SYSTEMS}
    ot_type_labels = {"SiteWise", "IIoT", "Historian"}
    for entry in DEMO_PROMPTS:
        assert entry.get("required_systems"), f"missing required_systems: {entry['title']}"
        for r in entry["required_systems"]:
            assert r in canonical or r in ot_type_labels, (
                f"{entry['title']!r} carries non-canonical entry {r!r}"
            )
