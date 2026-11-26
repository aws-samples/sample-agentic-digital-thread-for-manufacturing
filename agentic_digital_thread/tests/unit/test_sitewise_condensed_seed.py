# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A SiteWise schema seed larger than its cap is CONDENSED, never cut.

Live 2026-10-03 (28-model estate, ~22.8k chars against an 8k cap): the old
whole-model cut in API order dropped the one model carrying the vibration
properties, and the agent answered that the machine was not onboarded. Every
model must stay named; the system's concept-mapped models keep their
property ids first; and a partial seed gets a policy that does not treat
"missing from the list" as "does not exist".
"""
from agents import connection_types as ct
from agents import system_agent as sa


def _model(i: int, n_props: int = 12, name: str | None = None, assets: bool = True) -> dict:
    m = {
        "id": f"model-{i:04d}-aaaa-bbbb-cccc-dddddddddddd",
        "name": name or f"Model{i}",
        "properties": [
            {"id": f"prop-{i:04d}-{j:02d}-eeee-ffff-000000000000",
             "name": f"Measurement_{j}", "dataType": "DOUBLE", "unit": "mm/s"}
            for j in range(n_props)
        ],
    }
    if assets:
        m["asset_samples"] = {"names": [f"Asset{i}-a", f"Asset{i}-b"], "total": 2}
    return m


def _estate() -> list:
    # 28 models, the concept-mapped one LAST in API order.
    models = [_model(i) for i in range(27)]
    models.append(_model(99, n_props=20, name="Machine"))
    return models


def test_oversized_seed_names_every_model_within_cap():
    out = sa.format_sitewise_inventory(_estate())
    assert len(out) <= sa._SITEWISE_INVENTORY_CAP
    for i in range(27):
        assert f"- Model{i} (model_id=" in out
    assert "- Machine (model_id=" in out
    assert "[inventory condensed:" in out
    assert "inventory truncated" not in out


def test_concept_mapped_model_keeps_property_ids_even_when_listed_last():
    out = sa.format_sitewise_inventory(_estate(), priority_models=("Machine",))
    machine = next(ln for ln in out.splitlines() if ln.startswith("- Machine ("))
    assert "property_id=prop-0099-00" in machine
    assert "(ids: describe)" not in machine
    # Without the priority it is the last, largest model and stays compact.
    plain = sa.format_sitewise_inventory(_estate())
    machine = next(ln for ln in plain.splitlines() if ln.startswith("- Machine ("))
    assert "Measurement_19" in machine and "(ids: describe)" in machine


def test_seed_within_cap_is_unchanged_and_carries_no_note():
    models = [_model(1, n_props=3)]
    out = sa.format_sitewise_inventory(models, priority_models=("Model1",))
    assert out == sa.format_sitewise_inventory(models)
    assert "property_id=prop-0001-02" in out
    assert "[inventory" not in out


def test_priority_models_come_from_model_dot_property_fields():
    items = [
        {"concept": "sensor-reading", "field": "Machine.Measurement_1"},
        {"concept": "sensor-reading", "field": "Machine.Measurement_2"},
        {"concept": "equipment-id", "field": "Other.Name"},
        {"concept": "x", "field": "nodot"},
        "not-a-dict",
    ]
    assert sa._sitewise_priority_models(items) == ("Machine", "Other")
    assert sa._sitewise_priority_models(None) == ()


def test_partial_seed_policy_allows_targeted_describe_and_forbids_absence_claims():
    condensed = sa.format_sitewise_inventory(_estate())
    assert sa._sitewise_seed_is_partial(condensed)
    policy = sa._sitewise_query_policy(True, sa._sitewise_seed_is_partial(condensed))
    assert "NOT evidence that it does not exist" in policy
    assert "sitewise_describe_asset_model once for its model_id" in policy


def test_partial_seed_policy_does_not_also_forbid_its_own_recovery_calls():
    # The seeded policy's "Do NOT call
    # sitewise_list_asset_models or sitewise_describe_asset_model" stayed in
    # front of the partial addendum that asks for exactly those calls.
    policy = ct.sitewise_prompt_policy(True, partial=True)
    assert "Do NOT call sitewise_list_asset_models or sitewise_describe_asset_model" not in policy
    assert "Use the supplied model_id and property_id values exactly" in policy
    assert "sitewise_describe_asset_model once for its model_id" in policy
    assert "call sitewise_list_asset_models once before reporting one absent" in policy


def test_seed_cut_below_even_the_compact_list_still_gets_the_partial_policy():
    # When the compact lines alone exceed the cap the seed falls back to
    # whole-line truncation; the truncated seed must still select the
    # partial policy, so a missing model is never read as absent.
    cut = sa.format_sitewise_inventory(_estate(), cap=400)
    assert "[inventory truncated:" in cut
    assert sa._sitewise_seed_is_partial(cut)
    assert "NOT evidence that it does not exist" in sa._sitewise_query_policy(
        True, sa._sitewise_seed_is_partial(cut))


def test_complete_seed_policy_is_unchanged():
    full = sa.format_sitewise_inventory([_model(1, n_props=3)])
    assert not sa._sitewise_seed_is_partial(full)
    assert ct.sitewise_prompt_policy(True) == ct.sitewise_prompt_policy(True, partial=False)
    assert "NOT evidence" not in ct.sitewise_prompt_policy(True)
    # The unseeded policy never takes the partial addendum.
    assert ct.sitewise_prompt_policy(False, partial=True) == ct.sitewise_prompt_policy(False)
