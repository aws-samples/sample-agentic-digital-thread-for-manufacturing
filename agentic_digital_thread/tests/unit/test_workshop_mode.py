# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for the --workshop deploy profile (docs/workshop-mode.md).

Pins the three deterministic levers:

  1. Register-only workshop systems: ``manifest_systems()`` is exactly the demo
     manifest plus the plant systems when workshop mode is off; with
     ``ADT_WORKSHOP=1`` it appends the story's storyline systems
     (``ADT_WORKSHOP_STORY``: frozen | live) and the plant maintenance system
     (registered but excluded from every bulk-onboard default).
  2. The planted wrong-mapping fixture: the generic env-gated
     ``_apply_mapping_overrides`` seam in ``agents.onboard_agent`` — seam OFF ⇒
     proposals pass through untouched; seam ON ⇒ the planted mapping replaces the
     model row (with its reasoning) / errors propagate; and the bundled fixture
     itself plants EXACTLY ONE wrong mapping (assets.asset_num → work-order).
  3. The pinned refusal: the orchestrator prompt carries the exact pinned
     sentence ONLY under ``ADT_WORKSHOP=1``, and never weakens the general
     SYSTEM NOT ONBOARDED rule.
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.demo import workshop  # noqa: E402
from agents.demo.systems import DEMO_SYSTEMS, manifest_systems  # noqa: E402
from agents.onboard_agent import _apply_mapping_overrides  # noqa: E402

# Load scripts/onboarding_smoketest.py by file path (scripts/ is not a package)
# to pin the bulk-onboard default set against the workshop exclusion.
_REPO_ROOT = _ADT_ROOT.parent
_HARNESS_PATH = _REPO_ROOT / "scripts" / "onboarding_smoketest.py"
_spec = importlib.util.spec_from_file_location("onboarding_smoketest", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)


# ---------------------------------------------------------------------------
# Lever 1 — register-only workshop systems + bulk-onboard exclusion
# ---------------------------------------------------------------------------

_SEED_SQL_DIR = _REPO_ROOT / "lambda-adt-seed"
_DB_INIT_HANDLER = _REPO_ROOT / "lambda-db-init" / "handler.py"
#: The demo five the seed CR onboards first (the demo six minus the CMMS,
#: which stays the attendee exercise — lever 1).
_DEMO_FIVE = tuple(s["id"] for s in DEMO_SYSTEMS if s["system_type"] != "CMMS")
#: Storyline ids per story (docs/workshop-mode.md, ADR-0084).
_STORY_SEEDED_IDS = {
    "frozen": {"mes-ind", "historian", "sitewise-direct"},
    "live": {"mes-ind", "sitewise-direct"},
}


@pytest.fixture(params=workshop.STORIES)
def story_env(request, monkeypatch):
    """Workshop mode ON with ``ADT_WORKSHOP_STORY`` set to each story in turn."""
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.setenv("ADT_WORKSHOP_STORY", request.param)
    return request.param


def _all_workshop_systems() -> list[dict]:
    """Every workshop manifest entry across both stories plus the demo plant
    set. HighByte is forced on (``highbyte_shipped=True``) so the invariants
    below cover the register-only HighByte hub too."""
    entries = [s for st in workshop.STORIES
               for s in workshop.registered_systems(st, highbyte_shipped=True)]
    return entries + list(workshop.DEMO_PLANT_SYSTEMS)


def _schema_is_created_where_it_routes(schema: str) -> bool:
    """A seed-routed schema is created by a lambda-adt-seed file; an ERP-routed
    one by the db-init Lambda (agents.demo.db_routing, ADR-0044)."""
    from agents.demo.db_routing import is_erp_schema
    if is_erp_schema(schema):
        return schema in _DB_INIT_HANDLER.read_text(encoding="utf-8")
    return any(schema in f.read_text(encoding="utf-8") for f in _SEED_SQL_DIR.glob("0*.sql"))


def test_story_defaults_to_frozen_and_rejects_unknown_values(monkeypatch):
    assert workshop.STORIES == ("frozen", "live")
    assert workshop.DEFAULT_STORY == "frozen"
    monkeypatch.delenv("ADT_WORKSHOP_STORY", raising=False)
    assert workshop.story() == "frozen"
    monkeypatch.setenv("ADT_WORKSHOP_STORY", "")
    assert workshop.story() == "frozen"
    for name in workshop.STORIES:
        monkeypatch.setenv("ADT_WORKSHOP_STORY", name)
        assert workshop.story() == name
    monkeypatch.setenv("ADT_WORKSHOP_STORY", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        workshop.story()
    with pytest.raises(ValueError):
        workshop.seeded_systems("bogus")


def test_workshop_disabled_manifest_is_the_demo_manifest_plus_the_plant_systems(monkeypatch):
    """Demo (non-workshop) deploys also REGISTER the plant MES + maintenance —
    registered only, never in a bulk default; DEMO_SYSTEMS itself stays six."""
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    monkeypatch.delenv("ADT_WORKSHOP_STORY", raising=False)
    assert manifest_systems() == DEMO_SYSTEMS + workshop.DEMO_PLANT_SYSTEMS
    assert len(DEMO_SYSTEMS) == 6
    plant_ids = [s["id"] for s in workshop.DEMO_PLANT_SYSTEMS]
    assert plant_ids == ["mes-ind", "cmms-ind", "supplier-master"]
    assert workshop.DEMO_PLANT_SYSTEMS[0] == workshop.LIVE_MES_SYSTEM
    assert not set(plant_ids) & set(_harness.DEFAULT_SYSTEMS)
    assert not set(plant_ids) & {s["id"] for s in DEMO_SYSTEMS}


def test_workshop_enabled_appends_the_story_systems(story_env):
    systems = manifest_systems()
    assert systems[: len(DEMO_SYSTEMS)] == DEMO_SYSTEMS
    extras = {s["id"]: s for s in systems[len(DEMO_SYSTEMS):]}
    assert set(extras) == _STORY_SEEDED_IDS[story_env] | {"cmms-ind"}
    for sid, entry in extras.items():
        if entry["schema"]:
            assert _schema_is_created_where_it_routes(entry["schema"]), (sid, entry["schema"])
    sw = extras["sitewise-direct"]
    assert sw["connection_type"] == "SiteWise (Direct)" and sw["schema"] == ""
    # One plant for the whole storyline group — the register-only CMMS's plant
    # is the reference (docs/architecture.md: the B-2847 defect chain).
    plant = workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS[0]["plant"]
    for sid, entry in extras.items():
        assert entry["plant"] == plant, sid
    # The MES takes the demo's per-plant form ("<plant> MES"): no shared MES
    # exists to be a filter on (ADR-0073).
    assert extras["mes-ind"]["name"] == f"{plant} MES"
    ind = extras["cmms-ind"]
    assert ind["name"] == f"{plant} Plant Maintenance"
    assert ind["system_type"] == "CMMS" and ind["connection_type"] == "PostgreSQL"
    # Routes to the seed DB via the existing cmms_ prefix rule (ADR-0044).
    assert ind["schema"].startswith("cmms_")


def test_story_mes_routes_frozen_to_the_seed_and_live_to_the_erp():
    """Same system id, two schemas: the frozen storyline MES is the seeded
    archive (seed DB); the live MES is the simulator's per-site view (ERP)."""
    from agents.demo.db_routing import is_erp_schema
    frozen = {s["id"]: s for s in workshop.seeded_systems("frozen")}
    live = {s["id"]: s for s in workshop.seeded_systems("live")}
    assert is_erp_schema(frozen["mes-ind"]["schema"]) is False
    assert is_erp_schema(live["mes-ind"]["schema"]) is True
    assert live["mes-ind"] == workshop.LIVE_MES_SYSTEM
    assert live["sitewise-direct"] == workshop.LIVE_SITEWISE_SYSTEM


def test_frozen_storyline_schemas_route_to_the_seed_database():
    """The frozen storyline schemas live in the seed DB (lambda-adt-seed) —
    the routing exception is what makes the registered connection strings
    point at a database where they exist."""
    from agents.demo.db_routing import is_erp_schema
    for s in workshop.seeded_systems("frozen"):
        if s["schema"]:
            assert is_erp_schema(s["schema"]) is False, s["schema"]
    # The demo's simulator schemas still route to the ERP Aurora.
    assert is_erp_schema("erp_data") and is_erp_schema(DEMO_SYSTEMS[1]["schema"])


def test_story_helpers_read_the_env_and_return_copies(story_env):
    assert workshop.seeded_systems() == workshop.seeded_systems(story_env)
    assert workshop.registered_systems() == workshop.registered_systems(story_env)
    assert workshop.seeded_system_ids() == workshop.seeded_system_ids(story_env)
    first = workshop.registered_systems()
    first[0]["name"] = "mutated"
    assert workshop.registered_systems()[0]["name"] != "mutated"


def test_workshop_ids_are_tool_name_safe_and_verbatim():
    from agents.connection_types import sanitize_system_id
    for s in _all_workshop_systems():
        assert sanitize_system_id(s["id"]) == s["id"], s["id"]
        assert "status" not in s, s  # status is a live-catalog concern


def test_workshop_system_names_read_as_distinct_products():
    """A workshop system name must not be a demo system name, nor the demo
    name prefixed with the plant ("<plant> CMMS" reads as a site FILTER on
    the demo's all-sites CMMS, which is exactly how the channel lead read it,
    ADR-0073). The storyline systems are separate products in their own
    conventions; their names have to say so."""
    demo_names = {s["name"] for s in DEMO_SYSTEMS}
    for s in _all_workshop_systems():
        name, plant = s["name"], s["plant"]
        assert name not in demo_names, name
        if name.startswith(plant + " "):
            assert name[len(plant) + 1:] not in demo_names, (
                f"{name!r} reads as a plant filter on the demo's {name[len(plant) + 1:]!r}"
            )


def test_workshop_systems_excluded_from_every_bulk_onboard_default():
    """The declarative exclusion (workshop.ONBOARD_EXCLUDE) covers the
    register-only ids: they never appear in the demo manifest NOR in the
    onboarding harness's DEFAULT_SYSTEMS (the bulk set operators use
    post-deploy) — so they stay 'registered' until an attendee onboards them.
    The seeded storyline ids are new ids too (no collision with the demo six
    or the harness's OT set)."""
    register_only = {s["id"] for s in workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS}
    # cmms-ind is the register-only CMMS; highbyte is the register-only HighByte
    # hub (its own constant, present only when the package shipped HighByte);
    # supplier-master is the demo-only crosswalk source, excluded from bulk
    # onboarding (connected live to show the ValueMapping flow).
    assert register_only == {"cmms-ind"}
    assert workshop.ONBOARD_EXCLUDE == register_only | {"highbyte", "supplier-master"}
    workshop_ids = {s["id"] for s in _all_workshop_systems()}
    assert "highbyte" in workshop_ids
    assert not workshop_ids & {s["id"] for s in DEMO_SYSTEMS}
    assert not workshop_ids & set(_harness.DEFAULT_SYSTEMS)
    # The harness's OT set names the SAME SiteWise (Direct) and HighByte ids on
    # purpose — one registration, two seeding front doors (CR / workshop seam
    # and the operator onboarding script).
    assert workshop_ids & set(_harness.OT_SYSTEMS) == {"sitewise-direct", "highbyte"}
    # Neither register-only CMMS reaches an OT bulk set.
    assert not register_only & set(_harness.OT_SYSTEMS)
    # The onboarding harness's bulk default stays exactly the demo six.
    assert list(_harness.DEFAULT_SYSTEMS) == [s["id"] for s in DEMO_SYSTEMS]


def test_is_enabled_default_off_and_truthy_values(monkeypatch):
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    assert workshop.is_enabled() is False
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("ADT_WORKSHOP", val)
        assert workshop.is_enabled() is True, val
    monkeypatch.setenv("ADT_WORKSHOP", "0")
    assert workshop.is_enabled() is False


# ---------------------------------------------------------------------------
# Lever 2 — the mapping-override seam (generic, env-gated, inert when unset)
# ---------------------------------------------------------------------------

_MODEL_PROPOSALS = [
    {"field": "assets.asset_num", "table": "assets",
     "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.95}],
     "reasoning": "asset_num is the asset registry primary key."},
    {"field": "work_orders.wo_num", "table": "work_orders",
     "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.97}],
     "reasoning": "wo_num is the work order number."},
]


def test_seam_off_proposals_pass_through_untouched(monkeypatch):
    monkeypatch.delenv("ADT_MAPPING_OVERRIDES_FILE", raising=False)
    proposals = copy.deepcopy(_MODEL_PROPOSALS)
    out = _apply_mapping_overrides("cmms-ind", proposals)
    assert out is proposals          # same object — zero behavior change
    assert out == _MODEL_PROPOSALS   # and untouched content


def test_seam_on_planted_mapping_replaces_model_row(monkeypatch, tmp_path):
    planted = {"field": "assets.asset_num", "table": "assets",
               "mappings": [{"concept": "work-order", "role": "identifier",
                             "confidence": 0.82}],
               "reasoning": "Planted plausible-but-wrong reasoning."}
    f = tmp_path / "overrides.json"
    f.write_text(json.dumps({"cmms-ind": [planted]}))
    monkeypatch.setenv("ADT_MAPPING_OVERRIDES_FILE", str(f))
    out = _apply_mapping_overrides("cmms-ind", copy.deepcopy(_MODEL_PROPOSALS))
    assert len(out) == 2
    by_field = {m["field"]: m for m in out}
    # The planted row REPLACED the model's (deterministic — never model-dependent).
    assert by_field["assets.asset_num"]["mappings"][0]["concept"] == "work-order"
    assert by_field["assets.asset_num"]["reasoning"] == planted["reasoning"]
    # Untargeted rows are untouched.
    assert by_field["work_orders.wo_num"] == _MODEL_PROPOSALS[1]


def test_seam_on_planted_mapping_appended_when_model_missed_the_field(monkeypatch, tmp_path):
    planted = {"field": "assets.asset_num", "table": "assets",
               "mappings": [{"concept": "work-order", "role": "identifier",
                             "confidence": 0.82}],
               "reasoning": "Planted."}
    f = tmp_path / "overrides.json"
    f.write_text(json.dumps({"cmms-ind": [planted]}))
    monkeypatch.setenv("ADT_MAPPING_OVERRIDES_FILE", str(f))
    out = _apply_mapping_overrides("cmms-ind", [copy.deepcopy(_MODEL_PROPOSALS[1])])
    assert len(out) == 2
    assert out[-1] == planted


def test_seam_on_other_system_untouched(monkeypatch, tmp_path):
    f = tmp_path / "overrides.json"
    f.write_text(json.dumps({"cmms-ind": [{"field": "assets.asset_num"}]}))
    monkeypatch.setenv("ADT_MAPPING_OVERRIDES_FILE", str(f))
    proposals = copy.deepcopy(_MODEL_PROPOSALS)
    assert _apply_mapping_overrides("erp", proposals) == _MODEL_PROPOSALS


def test_seam_errors_propagate_no_silent_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("ADT_MAPPING_OVERRIDES_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        _apply_mapping_overrides("cmms-ind", [])
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("ADT_MAPPING_OVERRIDES_FILE", str(bad))
    with pytest.raises(json.JSONDecodeError):
        _apply_mapping_overrides("cmms-ind", [])


def test_seam_is_wired_into_map_fields():
    """The override seam must sit in the map_fields path (between the model
    batches and best-concept annotation) — pinned by source inspection so a
    refactor cannot silently drop the workshop trap."""
    import inspect
    from agents import onboard_agent
    src = inspect.getsource(onboard_agent.map_fields._tool_func
                            if hasattr(onboard_agent.map_fields, "_tool_func")
                            else onboard_agent.map_fields)
    assert "_apply_mapping_overrides(system_id, mappings)" in src


def test_seam_is_wired_into_the_wizard_map_job():
    """The Onboard wizard's Map step runs ``onboard._run_map_job`` (the
    ``/api/onboard/map`` background thread), NOT ``onboard_agent.map_fields``.
    Both pipelines must consult the seam BEFORE concept selection — the trap
    was observed dead on the wizard path in Workshop Studio test event 9
    (``assets.asset_num → equipment-id 95%``) because only map_fields did."""
    import inspect
    from agents import onboard
    src = inspect.getsource(onboard._run_map_job_base)
    reconcile = src.index("_reconcile_map_coverage(system_id")
    propose = src.index("_propose_row_concepts(system_id")
    assert reconcile < propose, "concept proposal must run after coverage reconciliation"
    helper = inspect.getsource(onboard._propose_row_concepts)
    seam = helper.index("_apply_mapping_overrides(system_id, mappings)")
    select = helper.index("_select_field_concepts(mappings")
    assert seam < select, "seam must run before concept selection"


def test_wizard_map_job_replaces_model_row_with_planted_mapping(monkeypatch, tmp_path):
    """Behavioral: drive ``_run_map_job_base`` with the model batches stubbed to
    the CMMS proposals; with the bundled fixture configured the terminal job
    result carries the planted ``work-order`` row for ``assets.asset_num``
    (and its reasoning), exactly as the review checkpoint will render it."""
    from agents import onboard

    monkeypatch.setenv("ADT_MAPPING_OVERRIDES_FILE", str(workshop.mapping_overrides_path()))
    proposals = copy.deepcopy(_MODEL_PROPOSALS)
    monkeypatch.setattr(onboard, "_run_map_batches",
                        lambda job_id, system_id, tables, telemetry_rich, concepts, job_start: (proposals, None))
    monkeypatch.setattr(onboard, "_reconcile_map_coverage", lambda *a, **k: None)
    monkeypatch.setattr(onboard, "_select_field_concepts", lambda *a, **k: None)
    monkeypatch.setattr(onboard, "_default_unmatched_to_sensor_reading", lambda *a, **k: None)
    monkeypatch.setattr(onboard, "_split_virtual_tables", lambda job_id, tables: (tables, {}))
    monkeypatch.setattr(onboard, "_is_telemetry_rich", lambda *a, **k: False)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: ["equipment-id", "work-order"])
    monkeypatch.setattr(onboard, "_map_job_result",
                        lambda system_id, mappings, tables=None, vocabulary=None: {"mappings": mappings})
    import agents.concept_suggest as _cs
    monkeypatch.setattr(_cs, "suggest_concepts_by_value_overlap", lambda *a, **k: None)
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: {"connection_type": "PostgreSQL"})
    jobs: dict = {}
    monkeypatch.setattr(onboard, "_map_jobs", jobs)

    onboard._run_map_job_base("job1", "cmms-ind", [{"name": "assets", "columns": []}])

    assert jobs["job1"]["status"] == "done", jobs["job1"]
    by_field = {m["field"]: m for m in jobs["job1"]["result"]["mappings"]}
    planted = by_field["assets.asset_num"]
    assert planted["mappings"][0]["concept"] == "work-order"
    assert "work order" in planted["reasoning"].lower()
    # Every other proposal passes through untouched (the trap is exactly one row).
    assert by_field["work_orders.wo_num"]["mappings"][0]["concept"] == "work-order"


def test_bundled_fixture_plants_exactly_one_wrong_mapping():
    """The shipped fixture: EXACTLY ONE planted row for the workshop CMMS —
    assets.asset_num (the IBM Maximo ASSETNUM, really equipment-id) wrongly mapped
    to work-order, with plausible reasoning text."""
    with open(workshop.mapping_overrides_path(), encoding="utf-8") as fh:
        fixture = json.load(fh)
    systems = [k for k in fixture if not k.startswith("_")]
    assert systems == ["cmms-ind"]
    rows = fixture["cmms-ind"]
    assert len(rows) == 1
    row = rows[0]
    assert row["field"] == "assets.asset_num"
    assert row["table"] == "assets"
    assert [m["concept"] for m in row["mappings"]] == ["work-order"]
    assert row["mappings"][0]["role"] == "identifier"
    assert 0 < row["mappings"][0]["confidence"] < 1
    assert len(row["reasoning"]) > 40  # plausible reasoning text, not a stub


# ---------------------------------------------------------------------------
# Lever 3 — the pinned refusal (workshop-gated prompt addendum)
# ---------------------------------------------------------------------------

def _build_prompt(monkeypatch, enabled: bool) -> str:
    from agents.orchestrator import _build_orchestrator_prompt
    if enabled:
        monkeypatch.setenv("ADT_WORKSHOP", "1")
    else:
        monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    return _build_orchestrator_prompt("agent")


def test_refusal_sentence_is_pinned_verbatim():
    assert workshop.PINNED_MAINTENANCE_REFUSAL == (
        "I don't have a maintenance system connected for that site, so I "
        "can't answer the maintenance part of this question."
    )


def test_prompt_carries_pinned_refusal_only_in_workshop_mode(monkeypatch):
    off = _build_prompt(monkeypatch, enabled=False)
    assert workshop.PINNED_MAINTENANCE_REFUSAL not in off
    assert "WORKSHOP PINNED REFUSAL" not in off
    on = _build_prompt(monkeypatch, enabled=True)
    assert workshop.PINNED_MAINTENANCE_REFUSAL in on
    assert "WORKSHOP PINNED REFUSAL" in on
    # The addendum is purely additive: the non-workshop prompt is a prefix.
    assert on.startswith(off)


def test_general_refusal_rule_never_weakened(monkeypatch):
    """The addendum pins phrasing on top of the existing grounded-refusal rule;
    the SYSTEM NOT ONBOARDED block must be present and unchanged in BOTH modes."""
    for enabled in (False, True):
        prompt = _build_prompt(monkeypatch, enabled=enabled)
        assert "SYSTEM NOT ONBOARDED — SAY SO, NEVER SUBSTITUTE (NON-NEGOTIABLE):" in prompt
        assert "NEVER REFUSE OR STOP EARLY:" in prompt


def test_registration_hook_registers_the_story_systems_when_enabled(story_env):
    """server._register_demo_systems registers the demo six plus the story's
    workshop systems under ADT_WORKSHOP=1 through the SAME connect_new_system
    path (id VERBATIM, dsn from the schema; the schema-less SiteWise (Direct)
    entry registers with an empty connection string — the seed harness
    supplies the regional endpoint at discover)."""
    from unittest.mock import MagicMock, patch
    import agents.server as server
    cat = MagicMock()
    cat.get.return_value = None
    connect = MagicMock(return_value={"status": "created"})
    with patch("agents.demo.is_enabled", return_value=True), \
         patch.object(server, "catalog", cat), \
         patch("agents.onboard_agent.register_demo_system", connect), \
         patch("agents.demo.db_routing.demo_pg_dsn", side_effect=lambda s: f"dsn::{s}"):
        server._register_demo_systems()
    story_systems = workshop.registered_systems(story_env)
    assert connect.call_count == len(DEMO_SYSTEMS) + len(story_systems)
    by_id = {c.kwargs["system_id"]: c.kwargs for c in connect.call_args_list}
    manifest = {s["id"]: s for s in story_systems}
    cmms_schema = manifest["cmms-ind"]["schema"]
    assert by_id["cmms-ind"]["schema"] == cmms_schema
    assert by_id["cmms-ind"]["connection_string"] == f"dsn::{cmms_schema}"
    assert by_id["mes-ind"]["connection_string"] == f"dsn::{manifest['mes-ind']['schema']}"
    assert by_id["sitewise-direct"]["connection_type"] == "SiteWise (Direct)"
    assert by_id["sitewise-direct"]["connection_string"] == ""
    # Lever 3 (test event 13): the manifest's routing prose reaches
    # connect_new_system so the System node carries it at provision; demo
    # systems keep the per-type default ("" here).
    for sid, entry in manifest.items():
        assert by_id[sid]["query_guidance"] == entry["query_guidance"], sid
        assert by_id[sid]["capabilities"] == entry["capabilities"], sid
    assert by_id["erp"]["query_guidance"] == "" and by_id["erp"]["capabilities"] == ""


def test_every_workshop_system_declares_routing_guidance():
    """Each workshop entry carries non-empty capabilities + query_guidance
    (the orchestrator routes on that prose; a bare storyline system lost the
    routing to the corporate ERP live)."""
    for entry in _all_workshop_systems():
        assert entry.get("capabilities"), entry["id"]
        assert entry.get("query_guidance"), entry["id"]
        assert len(entry["query_guidance"]) < 600, entry["id"]


@pytest.mark.parametrize("story", workshop.STORIES)
def test_workshop_guidance_names_only_systems_registered_in_that_story(story):
    """Routing prose must never send the orchestrator to a system that the
    story does not register (the live story has no archived historian)."""
    registered = workshop.registered_systems(story)
    here = {s["id"] for s in registered}
    absent = {s["id"]: s for s in _all_workshop_systems() if s["id"] not in here}
    for entry in registered:
        prose = f"{entry['capabilities']} {entry['query_guidance']}".lower()
        for other in absent.values():
            assert other["id"].lower() not in prose, (entry["id"], other["id"])
            assert other["name"].lower() not in prose, (entry["id"], other["name"])
    if story == "live":
        assert "historian" in absent


def test_no_workshop_manifest_names_a_plant_erp():
    """One ERP (ADR-0084): the corporate ERP is the only ERP any story registers."""
    for entry in _all_workshop_systems():
        assert entry["system_type"] != "ERP", entry["id"]
        assert not entry["schema"].startswith("erp_"), entry["id"]

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
