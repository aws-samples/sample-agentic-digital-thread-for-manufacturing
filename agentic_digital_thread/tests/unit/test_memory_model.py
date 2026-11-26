# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reading and switching the AgentCore Memory extraction model in place.

``agents/memory_model.py`` reads every override ``modelId`` of the memory's
custom strategies from GetMemory, and switches them with ONE UpdateMemory
built from the CURRENT configuration (only ``modelId`` replaced), then polls
GetMemory until the memory and every strategy are ACTIVE on the new model.
The control-plane client is a fake shaped like the live GetMemory answer
(adt-demo, 2026-10-27). Offline.
"""
from __future__ import annotations

import copy

import pytest

from agents import memory_model as mm
from tests.unit._memory_model_fakes import (
    LANG, MEMORY_ID, NEW, OLD, Clock, FakeControl, FakeParameters)
from tests.unit._memory_model_fakes import memory_record as _memory
from tests.unit._memory_model_fakes import override as _override


def _set(control, model_id=NEW, *, timeout=120.0, interval=5.0):
    clock = Clock()
    changed = mm.set_model(control, MEMORY_ID, model_id, timeout=timeout,
                           interval=interval, sleep=clock.sleep, clock=clock)
    return changed, clock


# -- read ----------------------------------------------------------------------

def test_read_reports_the_one_model_and_status():
    state = mm.read_state(FakeControl(), MEMORY_ID)
    assert state.model_id == OLD
    assert state.status == "ACTIVE"
    assert state.model_ids == (OLD,) * 5
    assert state.strategies == (("facts", "ACTIVE"), ("summaries", "ACTIVE"),
                                ("preferences", "ACTIVE"))


def test_read_reports_mixed_when_the_strategies_differ():
    memory = _memory()
    memory["strategies"][1]["configuration"]["consolidation"][
        "customConsolidationConfiguration"]["summaryConsolidationOverride"]["modelId"] = NEW
    state = mm.read_state(FakeControl(memory), MEMORY_ID)
    assert state.model_id == mm.MIXED == "mixed"
    assert set(state.model_ids) == {OLD, NEW}


def test_read_of_a_memory_with_no_override_model_is_empty():
    memory = _memory()
    memory["strategies"] = []
    assert mm.read_state(FakeControl(memory), MEMORY_ID).model_id == ""


# -- the update request ----------------------------------------------------------

def test_modify_strategies_replaces_only_each_model_id():
    memory = _memory()
    before = copy.deepcopy(memory)
    modify = mm.modify_strategies(memory, NEW)
    assert memory == before  # the read is not mutated
    assert [m["memoryStrategyId"] for m in modify] == [
        "facts-b7K2OdBQoY", "summaries-Q1w2E3r4T5", "preferences-Z9x8C7v6B5"]
    facts, summaries, prefs = (m["configuration"] for m in modify)
    assert facts == {
        "extraction": {"customExtractionConfiguration": {
            "semanticExtractionOverride": _override(NEW, LANG + " facts-x")}},
        "consolidation": {"customConsolidationConfiguration": {
            "semanticConsolidationOverride": _override(NEW, LANG + " facts-c")}}}
    assert summaries == {"consolidation": {"customConsolidationConfiguration": {
        "summaryConsolidationOverride": _override(NEW, LANG + " summaries")}}}
    assert prefs == {
        "extraction": {"customExtractionConfiguration": {
            "userPreferenceExtractionOverride": _override(NEW, LANG + " prefs-x")}},
        "consolidation": {"customConsolidationConfiguration": {
            "userPreferenceConsolidationOverride": _override(NEW, LANG + " prefs-c")}}}
    assert all(set(m) == {"memoryStrategyId", "configuration"} for m in modify)


@pytest.mark.parametrize("break_it", [
    lambda s: s[0].update(type="SEMANTIC"),                         # a built-in strategy
    lambda s: s[1]["configuration"]["consolidation"]["customConsolidationConfiguration"][
        "summaryConsolidationOverride"].pop("modelId"),             # no model id
    lambda s: s[2]["configuration"]["extraction"]["customExtractionConfiguration"][
        "userPreferenceExtractionOverride"].pop("appendToPrompt"),  # nothing to carry back
    lambda s: s[0]["configuration"].update(reflection={"x": 1}),    # a part the update drops
    lambda s: s[1]["configuration"].pop("consolidation"),           # no override at all
])
def test_a_strategy_that_is_not_a_custom_override_is_refused_with_no_call(break_it):
    memory = _memory()
    break_it(memory["strategies"])
    control = FakeControl(memory)
    with pytest.raises(mm.MemoryModelError) as err:
        _set(control)
    assert err.value.code == "unsupported"
    assert not err.value.update_sent
    assert control.updates == []


# -- set -----------------------------------------------------------------------

def test_unchanged_model_makes_no_update_call():
    control = FakeControl()
    changed, _ = _set(control, OLD)
    assert changed is False
    assert control.updates == []


# -- a retry on a memory that is not settled -------------------------------------------
#
# "Unchanged" used to mean "every visible override id is the target", read
# before the configuration was checked or the status was. A retry after a
# failed or unfinished switch then answered "unchanged" (and the save wrote
# SSM) while the memory was FAILED or still UPDATING, and an override with no
# model id was simply not counted.

def _failed_memory(model_id=NEW):
    return _memory(model_id, status="FAILED")


def _failed_strategy(model_id=NEW):
    memory = _memory(model_id)
    memory["strategies"][1]["status"] = "FAILED"
    return memory


def _updating_memory(model_id=NEW):
    return _memory(model_id, status="UPDATING")


def _creating_strategy(model_id=NEW):
    memory = _memory(model_id)
    memory["strategies"][0]["status"] = "CREATING"
    return memory


@pytest.mark.parametrize("make", [_failed_memory, _failed_strategy])
@pytest.mark.parametrize("target", [NEW, OLD])
def test_a_failed_memory_or_strategy_is_refused_as_failed_with_no_update(make, target):
    """Whether or not it already names the target: a retry after a failed
    switch never answers "unchanged"."""
    control = FakeControl(make(NEW))
    with pytest.raises(mm.MemoryModelError) as err:
        _set(control, target)
    assert err.value.code == "failed"
    assert not err.value.update_sent
    assert "FAILED" in str(err.value)
    assert control.updates == []


@pytest.mark.parametrize("make", [_updating_memory, _creating_strategy])
@pytest.mark.parametrize("target", [NEW, OLD])
def test_a_memory_or_strategy_still_changing_is_busy_with_no_update(make, target):
    control = FakeControl(make(NEW))
    with pytest.raises(mm.MemoryModelError) as err:
        _set(control, target)
    assert err.value.code == "busy"
    assert not err.value.update_sent
    assert control.updates == []


def test_an_override_missing_its_model_id_is_refused_even_when_the_rest_match():
    """The other four overrides run OLD; the fifth has no model id. Asking
    for OLD is not "unchanged": the strategy cannot be read or switched."""
    memory = _memory(OLD)
    memory["strategies"][1]["configuration"]["consolidation"][
        "customConsolidationConfiguration"]["summaryConsolidationOverride"].pop("modelId")
    control = FakeControl(memory)
    with pytest.raises(mm.MemoryModelError) as err:
        _set(control, OLD)
    assert err.value.code == "unsupported"
    assert "summaries" in str(err.value)
    assert control.updates == []


def test_unchanged_only_when_settled_on_the_target():
    control = FakeControl(_memory(NEW))
    changed, _ = _set(control, NEW)
    assert changed is False
    assert control.updates == []


def test_set_sends_one_update_and_waits_until_active_on_the_new_model():
    control = FakeControl(busy_reads=3)
    expected = mm.modify_strategies(_memory(), NEW)
    changed, clock = _set(control)
    assert changed is True
    assert control.updates == [{"memoryId": MEMORY_ID,
                                "memoryStrategies": {"modifyMemoryStrategies": expected}}]
    state = mm.read_state(control, MEMORY_ID)
    assert state.model_id == NEW and state.status == "ACTIVE"
    assert clock.sleeps and clock.now <= 120


def test_a_mixed_memory_is_set_to_the_one_model():
    memory = _memory()
    memory["strategies"][0]["configuration"]["extraction"]["customExtractionConfiguration"][
        "semanticExtractionOverride"]["modelId"] = NEW
    control = FakeControl(memory)
    changed, _ = _set(control, NEW)
    assert changed is True and len(control.updates) == 1


def test_timeout_names_the_state_read():
    control = FakeControl(busy_reads=10_000)
    with pytest.raises(mm.MemoryModelError) as err:
        _set(control, timeout=30, interval=5)
    assert err.value.code == "timeout"
    assert err.value.update_sent
    message = str(err.value)
    assert "UPDATING" in message and "30" in message
    assert "facts CREATING" in message
    assert len(control.updates) == 1


def test_an_update_that_did_not_take_is_a_mismatch_naming_the_models_read():
    control = FakeControl(busy_reads=0, apply=False)
    with pytest.raises(mm.MemoryModelError) as err:
        _set(control, timeout=20, interval=5)
    assert err.value.code == "mismatch"
    assert OLD in str(err.value) and NEW in str(err.value)
    assert len(control.updates) == 1


def test_a_failed_memory_stops_the_wait_at_once():
    control = FakeControl(busy_reads=0, final_status="FAILED")
    with pytest.raises(mm.MemoryModelError) as err:
        _set(control, timeout=120, interval=5)
    assert err.value.code == "failed"
    assert "FAILED" in str(err.value)
    assert control.gets <= 3


def test_a_failed_heartbeat_stops_the_wait_with_the_update_sent():
    """The save can no longer show it holds its slot: the wait stops at that
    poll (nothing more is read) and says UpdateMemory was sent. (The first
    heartbeat, right before UpdateMemory, still succeeds here.)"""
    control = FakeControl(busy_reads=3)
    clock = Clock()
    beats: list[int] = []

    def heartbeat() -> bool:
        beats.append(1)
        return len(beats) == 1
    with pytest.raises(mm.MemoryModelError) as err:
        mm.set_model(control, MEMORY_ID, NEW, timeout=120.0, interval=5.0,
                     sleep=clock.sleep, clock=clock, heartbeat=heartbeat)
    assert err.value.code == "slot_lost" and err.value.update_sent is True
    message = str(err.value)
    assert "UpdateMemory was sent" in message and NEW in message and "UPDATING" in message
    assert control.gets == 2 and beats == [1, 1] and clock.sleeps == []


def test_a_failed_heartbeat_right_before_the_update_sends_nothing():
    """The ownership check right before UpdateMemory: a save that can no
    longer show it holds its slot does not send it."""
    control = FakeControl()
    beats: list[int] = []

    def heartbeat() -> bool:
        beats.append(1)
        return False
    with pytest.raises(mm.MemoryModelError) as err:
        mm.set_model(control, MEMORY_ID, NEW, heartbeat=heartbeat)
    assert err.value.code == "slot_lost" and err.value.update_sent is False
    assert "did not send UpdateMemory" in str(err.value) and NEW in str(err.value)
    assert control.updates == [] and control.gets == 1 and beats == [1]


def test_no_heartbeat_runs_when_nothing_is_sent():
    """A memory already settled on the model sends nothing, so it needs no
    ownership check."""
    beats: list[int] = []
    assert mm.set_model(FakeControl(_memory(NEW)), MEMORY_ID, NEW,
                        heartbeat=lambda: beats.append(1) or True) is False
    assert beats == []


def test_a_switch_that_takes_131_seconds_settles_within_the_default_wait():
    """adt-demo, 2026-10-27: the memory and its three strategies were ACTIVE
    on the new model about 131 s after UpdateMemory. The default wait (no
    ``timeout``) must outlast that; 120 s gave up 11 s before it."""
    control = FakeControl(busy_reads=27)   # reads at 0..130 s UPDATING, 135 s settled
    clock = Clock()
    assert mm.set_model(control, MEMORY_ID, NEW, interval=5.0, sleep=clock.sleep,
                        clock=clock) is True
    assert clock.now >= 131
    assert mm.read_state(control, MEMORY_ID).settled_on(NEW)


def test_the_ssm_client_is_bounded(monkeypatch):
    """The switch job's longest run (``JOB_MAX_SECONDS``) counts the SSM
    write as one bounded call of two attempts, like the AgentCore ones
    (``total_max_attempts``: botocore reads a bare ``max_attempts`` as
    retries, one more attempt than it says)."""
    made: list[dict] = []

    def fake_client(service_name, **kwargs):
        made.append({"service": service_name, **kwargs})
        return object()
    monkeypatch.setattr("boto3.client", fake_client)
    mm._ssm_client("us-east-1")
    (call,) = made
    config = call["config"]
    assert call["service"] == "ssm" and call["region_name"] == "us-east-1"
    assert config.connect_timeout == 3 and config.read_timeout == 10
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}


def test_the_error_type_is_a_safe_message_type():
    """Its text is fixed wording plus model ids, strategy names and AgentCore
    status words, so the settings save may show it."""
    from agents.public_errors import is_safe_message
    assert is_safe_message(mm.MemoryModelError("x", code="timeout"))


# -- resolve -----------------------------------------------------------------------

@pytest.mark.parametrize("given, region, expected", [
    ("anthropic.claude-haiku-5-5", "us-east-1", "global.anthropic.claude-haiku-5-5"),
    ("us.anthropic.claude-haiku-5-5", "us-east-1", "global.anthropic.claude-haiku-5-5"),
    ("us.amazon.nova-pro-v1:0", "eu-west-1", "eu.amazon.nova-pro-v1:0"),
    (" global.anthropic.claude-sonnet-5 ", "us-east-1", "global.anthropic.claude-sonnet-5"),
])
def test_resolve_spells_the_id_for_the_region(given, region, expected):
    assert mm.resolve(given, region) == expected


# -- selectable ------------------------------------------------------------------

@pytest.fixture
def gates(monkeypatch):
    from agents.demo import workshop
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    monkeypatch.setattr(workshop, "is_enabled", lambda: False)
    monkeypatch.setattr(mm, "configured_memory_id", lambda: MEMORY_ID)
    # The app's config is loaded, as on a running app (the settings view never loads it).
    monkeypatch.setattr(mm, "loaded_memory_id", lambda: (True, MEMORY_ID), raising=False)
    # Nothing saved for the next deploy unless a test says so.
    monkeypatch.setattr(mm, "_ssm_client", lambda region: FakeParameters())
    return monkeypatch


def test_selectable_on_a_base_deploy(gates):
    assert mm.selectable() == (True, "")


def test_not_selectable_on_the_workshop(gates):
    from agents.demo import workshop
    gates.setattr(workshop, "is_enabled", lambda: True)
    ok, reason = mm.selectable()
    assert ok is False and "workshop" in reason.lower()


def test_not_selectable_without_a_memory(gates):
    gates.setattr(mm, "configured_memory_id", lambda: None)
    gates.setattr(mm, "loaded_memory_id", lambda: (True, None), raising=False)
    ok, reason = mm.selectable()
    assert ok is False and "memory" in reason.lower()


def test_not_selectable_when_the_allowlist_offers_no_memory_model(gates):
    gates.setenv("ALLOWED_MODELS", "anthropic.claude-sonnet-4-6")
    ok, reason = mm.selectable()
    assert ok is False and "ALLOWED_MODELS" in reason


def test_selectable_when_the_allowlist_offers_a_memory_model(gates):
    gates.setenv("ALLOWED_MODELS", "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0")
    assert mm.selectable() == (True, "")


# -- the settings view (GET /api/admin/llm-settings) -----------------------------------

def test_settings_view_reports_the_model_and_selectability(gates):
    gates.setattr(mm, "control_client", lambda region: FakeControl())
    view = mm.settings_view("us-east-1")
    assert view == {"memory_model": OLD, "memory_model_selectable": True,
                    "memory_model_locked_reason": ""}


def test_settings_view_on_the_workshop_names_the_pinned_model(gates):
    from agents.demo import workshop
    gates.setattr(workshop, "is_enabled", lambda: True)
    gates.setattr(mm, "control_client",
                  lambda region: FakeControl(_memory("us.amazon.nova-pro-v1:0")))
    view = mm.settings_view("us-east-1")
    assert view["memory_model"] == "us.amazon.nova-pro-v1:0"
    assert view["memory_model_selectable"] is False
    assert "Nova Pro" in view["memory_model_locked_reason"]


def test_settings_view_never_raises_on_a_read_failure(gates):
    class Broken:
        def get_memory(self, **_kwargs):
            raise RuntimeError("arn:aws:bedrock-agentcore:us-east-1:111122223333:memory/x denied")
    gates.setattr(mm, "control_client", lambda region: Broken())
    view = mm.settings_view("us-east-1")
    assert view["memory_model"] == ""
    assert view["memory_model_selectable"] is True
    assert view["memory_model_error"]
    assert "111122223333" not in view["memory_model_error"]


def test_settings_view_without_a_memory_makes_no_call(gates):
    gates.setattr(mm, "configured_memory_id", lambda: None)
    gates.setattr(mm, "loaded_memory_id", lambda: (True, None), raising=False)

    def no_client(region):
        raise AssertionError("no control-plane call without a memory")
    gates.setattr(mm, "control_client", no_client)
    view = mm.settings_view("us-east-1")
    assert view["memory_model"] == ""
    assert view["memory_model_selectable"] is False
    assert "memory_model_error" not in view


# -- the settings view: a saved model the memory does not run ---------------------------
#
# SSM /adt/llm-memory-model is what the next deploy puts on the memory. When
# it names another model than the memory runs (a CloudFormation update or
# another writer moved the memory after a save), the page says so.

def test_settings_view_reports_a_saved_model_the_memory_does_not_run(gates):
    ssm = FakeParameters({mm.SSM_PARAMETER: NEW})
    gates.setattr(mm, "_ssm_client", lambda region: ssm)
    gates.setattr(mm, "control_client", lambda region: FakeControl())
    view = mm.settings_view("us-east-1")
    assert view["memory_model"] == OLD
    assert view["memory_model_saved"] == NEW
    assert "memory_model_error" not in view
    assert ssm.reads == [[mm.SSM_PARAMETER]]


def test_settings_view_with_the_saved_model_running_adds_nothing(gates):
    gates.setattr(mm, "_ssm_client", lambda region: FakeParameters({mm.SSM_PARAMETER: OLD}))
    gates.setattr(mm, "control_client", lambda region: FakeControl())
    view = mm.settings_view("us-east-1")
    assert "memory_model_saved" not in view and "memory_model_error" not in view


def test_settings_view_with_nothing_saved_is_not_an_error(gates):
    gates.setattr(mm, "control_client", lambda region: FakeControl())
    view = mm.settings_view("us-east-1")
    assert "memory_model_saved" not in view and "memory_model_error" not in view


def test_settings_view_reports_a_failed_saved_model_read(gates):
    gates.setattr(mm, "_ssm_client", lambda region: FakeParameters(fail=True))
    gates.setattr(mm, "control_client", lambda region: FakeControl())
    view = mm.settings_view("us-east-1")
    assert view["memory_model"] == OLD
    assert view["memory_model_error"]
    assert "111122223333" not in view["memory_model_error"]
    assert "memory_model_saved" not in view


def test_settings_view_on_the_workshop_does_not_read_the_saved_model(gates):
    """deploy.sh never reads it on the workshop, so it would say nothing true."""
    from agents.demo import workshop
    gates.setattr(workshop, "is_enabled", lambda: True)
    ssm = FakeParameters({mm.SSM_PARAMETER: NEW})
    gates.setattr(mm, "_ssm_client", lambda region: ssm)
    gates.setattr(mm, "control_client",
                  lambda region: FakeControl(_memory("us.amazon.nova-pro-v1:0")))
    view = mm.settings_view("us-east-1")
    assert "memory_model_saved" not in view and ssm.reads == []


# -- the settings view: the saved value as the next deploy resolves it -------------------
#
# deploy.sh passes the SSM value back only when ``_model_allowlist.py allows
# ... memory`` (a memory extraction model the allowlist offers), and the
# stack resolves its family for the deploy Region. The drift line compares
# THAT id with the live one; a value deploy.sh would ignore is reported as
# invalid, never as drift.

@pytest.mark.parametrize("saved, region, live", [
    ("us.amazon.nova-pro-v1:0", "eu-west-1", "eu.amazon.nova-pro-v1:0"),
    ("amazon.nova-pro-v1:0", "ap-southeast-2", "apac.amazon.nova-pro-v1:0"),
    ("anthropic.claude-haiku-5-5", "us-east-1", OLD),
    ("us.anthropic.claude-haiku-5-5", "eu-west-1", OLD),
    (f"  {OLD}  ", "us-east-1", OLD),
])
def test_settings_view_same_family_in_another_spelling_is_not_drift(gates, saved, region, live):
    gates.setattr(mm, "_ssm_client", lambda r: FakeParameters({mm.SSM_PARAMETER: saved}))
    gates.setattr(mm, "control_client", lambda r: FakeControl(_memory(live)))
    view = mm.settings_view(region)
    assert view["memory_model"] == live
    assert "memory_model_saved" not in view and "memory_model_saved_invalid" not in view
    assert "memory_model_error" not in view


@pytest.mark.parametrize("saved, region, effective", [
    ("anthropic.claude-sonnet-5-5", "us-east-1", NEW),
    ("us.anthropic.claude-sonnet-5-5", "eu-west-1", NEW),
    ("us.amazon.nova-pro-v1:0", "eu-west-1", "eu.amazon.nova-pro-v1:0"),
])
def test_settings_view_a_different_family_is_drift_named_as_the_deploy_resolves_it(
        gates, saved, region, effective):
    gates.setattr(mm, "_ssm_client", lambda r: FakeParameters({mm.SSM_PARAMETER: saved}))
    gates.setattr(mm, "control_client", lambda r: FakeControl())
    view = mm.settings_view(region)
    assert view["memory_model"] == OLD
    assert view["memory_model_saved"] == effective
    assert "memory_model_saved_invalid" not in view


@pytest.mark.parametrize("saved, allowlist", [
    ("global.anthropic.claude-sonnet-4-6", ""),      # a catalog model, not a memory one
    ("openai.gpt-5.6-luna", ""),                     # Mantle
    ("made.up-model", ""),
    # A memory model the allowlist does not offer: deploy.sh ignores it too.
    ("global.anthropic.claude-sonnet-5", "anthropic.claude-haiku-5-5,anthropic.claude-sonnet-5-5"),
])
def test_settings_view_an_invalid_saved_value_is_reported_apart_from_drift(gates, saved, allowlist):
    if allowlist:
        gates.setenv("ALLOWED_MODELS", allowlist)
    gates.setattr(mm, "_ssm_client", lambda r: FakeParameters({mm.SSM_PARAMETER: saved}))
    gates.setattr(mm, "control_client", lambda r: FakeControl())
    view = mm.settings_view("us-east-1")
    assert view["memory_model_saved_invalid"] == saved
    assert "memory_model_saved" not in view and "memory_model_error" not in view
    assert view["memory_model_selectable"] is True


def test_settings_view_bounds_an_invalid_saved_value(gates):
    saved = "x" * 5000
    gates.setattr(mm, "_ssm_client", lambda r: FakeParameters({mm.SSM_PARAMETER: saved}))
    gates.setattr(mm, "control_client", lambda r: FakeControl())
    view = mm.settings_view("us-east-1")
    assert len(view["memory_model_saved_invalid"]) <= 200
    assert view["memory_model_saved_invalid"].startswith("x" * 100)


def test_settings_view_a_blank_saved_value_is_nothing_saved(gates):
    gates.setattr(mm, "_ssm_client", lambda r: FakeParameters({mm.SSM_PARAMETER: "   "}))
    gates.setattr(mm, "control_client", lambda r: FakeControl())
    view = mm.settings_view("us-east-1")
    assert "memory_model_saved" not in view and "memory_model_saved_invalid" not in view


# -- the settings view: would the next deploy keep the model the memory runs? -------------
#
# With nothing valid saved, the next deploy that updates the memory puts the
# stack default back (``MEMORY_DEFAULT_FAMILY`` resolved for the Region), so
# a memory running another model is reported as unsaved, with that default.

def test_settings_view_a_model_with_nothing_saved_is_unsaved(gates):
    gates.setattr(mm, "control_client", lambda r: FakeControl(_memory(NEW)))
    view = mm.settings_view("us-east-1")
    assert view["memory_model"] == NEW
    assert view["memory_model_unsaved"] is True
    assert view["memory_model_next_deploy"] == OLD
    assert "memory_model_saved" not in view and "memory_model_error" not in view


def test_settings_view_the_default_with_nothing_saved_says_nothing(gates):
    gates.setattr(mm, "control_client", lambda r: FakeControl())
    view = mm.settings_view("us-east-1")
    assert "memory_model_unsaved" not in view and "memory_model_next_deploy" not in view


def test_settings_view_resolves_the_default_for_the_region(gates):
    """Nova Pro in eu-west-1 with nothing saved: the default the next deploy
    applies is Haiku 5.5's global profile, named as such."""
    gates.setattr(mm, "control_client", lambda r: FakeControl(_memory("eu.amazon.nova-pro-v1:0")))
    view = mm.settings_view("eu-west-1")
    assert view["memory_model_unsaved"] is True
    assert view["memory_model_next_deploy"] == OLD


def test_settings_view_a_value_the_next_deploy_ignores_is_unsaved_too(gates):
    gates.setattr(mm, "_ssm_client",
                  lambda r: FakeParameters({mm.SSM_PARAMETER: "global.anthropic.claude-sonnet-4-6"}))
    gates.setattr(mm, "control_client", lambda r: FakeControl(_memory(NEW)))
    view = mm.settings_view("us-east-1")
    assert view["memory_model_saved_invalid"] == "global.anthropic.claude-sonnet-4-6"
    assert view["memory_model_unsaved"] is True
    assert view["memory_model_next_deploy"] == OLD


def test_settings_view_a_blank_saved_value_is_unsaved(gates):
    gates.setattr(mm, "_ssm_client", lambda r: FakeParameters({mm.SSM_PARAMETER: "  "}))
    gates.setattr(mm, "control_client", lambda r: FakeControl(_memory(NEW)))
    view = mm.settings_view("us-east-1")
    assert view["memory_model_unsaved"] is True
    assert "memory_model_saved_invalid" not in view


def test_settings_view_a_valid_saved_value_is_never_unsaved(gates):
    """A saved model, even one the memory does not run, is what the next
    deploy applies: the drift line names it, never the default."""
    gates.setattr(mm, "_ssm_client", lambda r: FakeParameters({mm.SSM_PARAMETER: NEW}))
    gates.setattr(mm, "control_client",
                  lambda r: FakeControl(_memory("global.anthropic.claude-haiku-4-5-20251001-v1:0")))
    view = mm.settings_view("us-east-1")
    assert view["memory_model_saved"] == NEW
    assert "memory_model_unsaved" not in view and "memory_model_next_deploy" not in view


def test_settings_view_a_mixed_memory_with_nothing_saved_is_unsaved(gates):
    memory = _memory()
    memory["strategies"][1]["configuration"]["consolidation"]["customConsolidationConfiguration"][
        "summaryConsolidationOverride"]["modelId"] = NEW
    gates.setattr(mm, "control_client", lambda r: FakeControl(memory))
    view = mm.settings_view("us-east-1")
    assert view["memory_model"] == "mixed"
    assert view["memory_model_unsaved"] is True and view["memory_model_next_deploy"] == OLD


@pytest.mark.parametrize("gate", ["workshop", "failed_read"])
def test_settings_view_unsaved_only_where_it_is_known(gates, gate):
    from agents.demo import workshop
    if gate == "workshop":
        gates.setattr(workshop, "is_enabled", lambda: True)
        gates.setattr(mm, "control_client", lambda r: FakeControl(_memory(NEW)))
    else:
        gates.setattr(mm, "_ssm_client", lambda r: FakeParameters(fail=True))
        gates.setattr(mm, "control_client", lambda r: FakeControl(_memory(NEW)))
    view = mm.settings_view("us-east-1")
    assert "memory_model_unsaved" not in view and "memory_model_next_deploy" not in view
