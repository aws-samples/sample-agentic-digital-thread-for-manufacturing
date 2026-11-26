# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the ADT_DEMO gate on prompts.list_prompts / seed_registry.

The demo prompt pack now lives in ``agents.demo``; ``prompts.py`` routes through
the seam and only surfaces it when ``ADT_DEMO`` is enabled. These tests exercise
both demo-OFF (clean core) and demo-ON states with an in-memory prompt store —
no AWS, no DynamoDB.
"""

import pytest

from agents import demo, prompts


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

    def delete(self, template_id):
        self._items.pop(template_id, None)

    def get(self, template_id):
        return self._items.get(template_id)


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakePromptStore()
    monkeypatch.setattr(prompts, "_prompt_store", store)
    return store


# ---------------------------------------------------------------------------
# demo-OFF: clean core has no registry/demo prompts
# ---------------------------------------------------------------------------

def test_seed_registry_noop_when_demo_disabled(fake_store, monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    prompts.seed_registry()
    assert fake_store.list_all() == []


def test_list_prompts_empty_when_demo_disabled(fake_store, monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    assert prompts.list_prompts() == []


def test_user_prompt_still_lists_when_demo_disabled(fake_store, monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    fake_store.save({"template_id": "u1", "source": "user", "text": "my prompt"})
    results = prompts.list_prompts()
    assert len(results) == 1
    assert results[0]["template_id"] == "u1"
    assert results[0]["source"] == "user"


# ---------------------------------------------------------------------------
# demo-ON: curated pack seeds and surfaces
# ---------------------------------------------------------------------------

def test_seed_registry_seeds_pack_when_demo_enabled(fake_store, monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    prompts.seed_registry()
    seeded = fake_store.list_all()
    assert len(seeded) == len(demo.demo_prompts())
    assert all(item["source"] == "registry" for item in seeded)


def test_list_prompts_fallback_returns_pack_when_demo_enabled(fake_store, monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    results = prompts.list_prompts()
    assert len(results) == len(demo.demo_prompts())
    for item in results:
        assert item["source"] == "registry"
        assert item["template_id"]
        assert "created" in item
        assert "pinned" in item
        assert item["schedule"] is None


# ---------------------------------------------------------------------------
# Dedupe / idempotency: registry seed must not accumulate duplicate rows
# ---------------------------------------------------------------------------

def test_seed_registry_is_idempotent_under_repeated_seeds(fake_store, monkeypatch):
    """Re-seeding overwrites the same deterministic ids — no dup accumulation."""
    monkeypatch.setenv("ADT_DEMO", "1")
    prompts.seed_registry()
    first = fake_store.list_all()
    prompts.seed_registry()
    prompts.seed_registry()
    after = fake_store.list_all()
    assert len(after) == len(demo.demo_prompts())
    assert len(after) == len(first)
    # ids are stable across seeds
    assert {i["template_id"] for i in first} == {i["template_id"] for i in after}


def test_registry_template_id_is_deterministic():
    entry = {"persona": "Plant Manager", "category": "cross-system",
             "title": "Daily Briefing Report", "text": "do the thing"}
    assert prompts._registry_template_id(entry) == prompts._registry_template_id(dict(entry))
    other = {**entry, "title": "Different Title"}
    assert prompts._registry_template_id(entry) != prompts._registry_template_id(other)


def test_dedupe_by_template_id_collapses_and_prefers_registry():
    rows = [
        {"template_id": "a", "source": "user", "title": "keep-me"},
        {"template_id": "b", "source": "registry", "title": "reg"},
        {"template_id": "b", "source": "user", "title": "dup-of-reg"},
        {"template_id": "c", "source": "user", "title": "custom"},
    ]
    out = prompts._dedupe_by_template_id(rows)
    ids = [r["template_id"] for r in out]
    assert ids == ["a", "b", "c"]  # order preserved, single 'b'
    b = next(r for r in out if r["template_id"] == "b")
    assert b["source"] == "registry"  # registry preferred on collision


def test_list_prompts_dedupes_residual_duplicate_rows(fake_store, monkeypatch):
    """Pre-fix random-id dup rows sharing a template_id collapse in list_prompts."""
    monkeypatch.setenv("ADT_DEMO", "1")
    fake_store.save({"template_id": "dup", "source": "registry", "text": "t",
                     "persona": "Plant Manager", "title": "X"})
    # Simulate a residual dup that shares the id (registry preferred kept).
    results = prompts.list_prompts()
    assert len([r for r in results if r["template_id"] == "dup"]) == 1
    # user customs always survive dedupe
    fake_store.save({"template_id": "u1", "source": "user", "text": "mine"})
    results = prompts.list_prompts()
    assert any(r["template_id"] == "u1" for r in results)


# ---------------------------------------------------------------------------
# Pack shape: outage-review prompt + canonical required_systems vocabulary
# ---------------------------------------------------------------------------

_CANONICAL_REQUIRED = {
    # canonical catalog ids (agents.demo.systems)
    "erp", "mes-mil", "mes-det", "mes-cr", "cmms", "plm",
    # deployment-dependent OT/UNS entries stay TYPE-shaped (module docstring)
    "SiteWise", "IIoT", "Historian",
}


def _pack():
    from agents.demo import prompt_pack
    return prompt_pack.DEMO_PROMPTS


def test_outage_review_prompt_present_with_expected_shape():
    entry = next(p for p in _pack() if p["title"] == "Filler Pump Outage Review")
    assert entry["persona"] == "Maintenance Engineer"
    assert entry["required_systems"] == ["SiteWise", "cmms"]
    assert "COMPLETED maintenance work order" in entry["text"]
    # facts-only stats table requirement
    assert "min, max, mean" in entry["text"]
    assert "ONLY what the numbers actually show" in entry["text"]


def test_pack_required_systems_use_canonical_vocabulary():
    for entry in _pack():
        for req in entry.get("required_systems", []):
            assert req in _CANONICAL_REQUIRED, (
                f"{entry['title']!r} uses non-canonical required_systems "
                f"entry {req!r}"
            )


def test_no_sitewise_direct_mechanics_remain_in_pack():
    """The clamped chart tool obsoleted the raw-mechanics instructions."""
    for entry in _pack():
        assert "max_results=720" not in entry["text"], entry["title"]
        assert "_path attribute" not in entry["text"], entry["title"]


def test_filler_pump_health_requires_unit_grouped_charts():
    entry = next(p for p in _pack() if p["title"] == "Milwaukee Filler Pump Health")
    assert "grouped by unit of measure" in entry["text"]
    assert "never one chart per individual sensor" in entry["text"]


#: Demo prompts that end on results a presenter has to explain. The core chart
#: rule no longer asks for post-result analysis, so these carry their own
#: closing interpretation ask. The titles live in the demo pack.
def _interpreted_titles():
    from agents.demo.prompt_pack import INTERPRETED_TITLES
    return INTERPRETED_TITLES


def test_interpreted_prompts_end_with_what_this_shows():
    from agents.demo.prompt_pack import WHAT_THIS_SHOWS

    titles = _interpreted_titles()
    assert len(titles) == 5 and len(set(titles)) == 5
    by_title = {p["title"]: p for p in _pack()}
    for title in titles:
        entry = by_title[title]  # KeyError = a renamed or dropped prompt
        assert entry["text"].endswith(WHAT_THIS_SHOWS), title
        # The original ask is kept; the closing instruction only follows it.
        assert len(entry["text"]) > len(WHAT_THIS_SHOWS) + 40, title


def test_supplier_traceability_prompt_is_bounded_and_aggregate():
    """The open form matched hundreds of failed batches with several supplier
    lots each, so an answer could only show a sample. The bounded form asks
    for a fixed count of the most recent failed batches (never a date window,
    which a fresh deployment can leave empty), a per-supplier count and one
    example each."""
    import json
    import re
    from pathlib import Path

    entry = next(p for p in _pack() if p["title"] == "Supplier Quality Traceability")
    assert entry["persona"] == "Quality Engineer"
    assert entry["required_systems"] == ["erp", "mes-mil", "mes-det", "mes-cr"]
    text = entry["text"]
    assert "10 most recent production batches with failed quality checks" in text
    assert "Count those batches per supplier, worst first" in text
    assert "one example batch with its failure for each" in text
    assert not re.search(r"(?i)last \d+ days|this week|today", text)
    # The golden copy asks the same bounded question (id unchanged).
    golden = Path(prompts.__file__).parent / "demo" / "golden_evals.json"
    (q,) = [q for q in json.loads(golden.read_text(encoding="utf-8"))["questions"]
            if q["id"] == "g-supplier-quality-trace"]
    assert text.startswith(q["text"])
    # The scoring stays as loose as before: whether a failed batch exists
    # depends on the live simulator (routine rejects are probabilistic), so a
    # stricter golden would fail correct fresh deployments.
    assert q["expect"]["answer_must_match"] == ["(?i)(supplier|batch)"]


def test_what_this_shows_labels_causes_as_inference():
    """The closing ask defers to the core CAUSE rule (orchestrator.py): a likely
    cause is an inference backed by named evidence, and no cause is invented."""
    from agents.demo.prompt_pack import WHAT_THIS_SHOWS

    assert "'What this shows'" in WHAT_THIS_SHOWS
    assert "likely cause as an inference" in WHAT_THIS_SHOWS
    assert "name the evidence" in WHAT_THIS_SHOWS
    assert "do not point to a cause, say so" in WHAT_THIS_SHOWS


def test_what_this_shows_is_demo_only():
    """The ask lives only in the demo pack; the core orchestrator prompt and the
    golden set's own question copies do not carry it."""
    import json
    from pathlib import Path

    from agents.demo.prompt_pack import WHAT_THIS_SHOWS

    agents_dir = Path(prompts.__file__).parent
    orchestrator_src = (agents_dir / "orchestrator.py").read_text(encoding="utf-8")
    assert "What this shows" not in orchestrator_src
    golden = agents_dir / "demo" / "golden_evals.json"
    for q in json.loads(golden.read_text(encoding="utf-8"))["questions"]:
        assert "What this shows" not in q["text"], q["id"]
    untouched = [p for p in _pack() if p["title"] not in _interpreted_titles()]
    assert untouched and all(WHAT_THIS_SHOWS not in p["text"] for p in untouched)


def test_outage_review_filtered_when_required_systems_absent(monkeypatch):
    """Availability filter drops the prompt when SiteWise/cmms are not onboarded."""
    from agents.catalog import catalog

    monkeypatch.setattr(
        catalog, "get_onboarded",
        lambda: [{"id": "erp", "system_type": "ERP", "connection_type": "postgres"}],
    )
    entry = next(p for p in _pack() if p["title"] == "Filler Pump Outage Review")
    assert prompts._filter_satisfiable([entry]) == []
    # ...and kept when a SiteWise-technology system plus cmms are onboarded
    monkeypatch.setattr(
        catalog, "get_onboarded",
        lambda: [
            {"id": "cmms", "system_type": "CMMS", "connection_type": "postgres"},
            {"id": "ot-1", "system_type": "Historian",
             "connection_type": "SiteWise (Direct)"},
        ],
    )
    assert prompts._filter_satisfiable([entry]) == [entry]
