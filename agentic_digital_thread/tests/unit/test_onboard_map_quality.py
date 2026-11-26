# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guards for the onboarding map-quality improvements.

Three generic changes, all asserted with SYNTHETIC data (no dataset-specific
literals): (1) the richer per-column fields-summary formatter injects data types
and capped/truncated/deduped sample values with a name-only fallback; (2) the
map-prompt Q1 guidance no longer blanket-excludes business ``*_id`` identifiers
while still excluding the bare ``id`` surrogate key; (3) the pure weak-batch
scorer decides when a batch should escalate to a stronger model.

Pure string/predicate assembly — no AWS, no graph, no DB, no LLM — so these run
in the default (unmarked) offline lane.
"""
from __future__ import annotations

import json

import agents.onboard as onboard


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE #1 — _format_samples / _format_fields_summary
# ─────────────────────────────────────────────────────────────────────────────
def test_format_samples_caps_dedups_and_truncates():
    """At most 3 values, de-duplicated, each truncated to the max length."""
    long_val = "x" * 80
    out = onboard._format_samples(
        ["alpha", "alpha", "beta", "gamma", "delta", long_val],
        max_samples=3,
        sample_maxlen=40,
    )
    parts = [p.strip() for p in out.split(",")]
    # capped to 3
    assert len(parts) == 3
    # de-duplicated: the second "alpha" is dropped, so beta/gamma follow
    assert parts == ["alpha", "beta", "gamma"]


def test_format_samples_truncates_to_maxlen():
    long_val = "y" * 200
    out = onboard._format_samples([long_val], max_samples=3, sample_maxlen=40)
    assert len(out) == 40
    assert out.endswith("…")


def test_format_samples_empty_and_blank():
    assert onboard._format_samples(None) == ""
    assert onboard._format_samples([]) == ""
    assert onboard._format_samples([None, "", "   "]) == ""


def test_format_fields_summary_includes_type_and_samples():
    batch = [{
        "name": "widgets",
        "columns": [
            {"name": "widget_code", "data_type": "varchar"},
            {"name": "temperature_c", "data_type": "double"},
        ],
        "sample_values": {
            "widget_code": ["WGT-1", "WGT-2", "WGT-3", "WGT-4"],
            "temperature_c": [21.5, 22.0],
        },
    }]
    summary = onboard._format_fields_summary(batch)
    # table header + per-column lines
    assert "widgets:" in summary
    # data type surfaced
    assert "[varchar]" in summary
    assert "[double]" in summary
    # sample values surfaced, capped to 3 for widget_code
    assert "e.g. WGT-1, WGT-2, WGT-3" in summary
    assert "WGT-4" not in summary
    # numeric samples stringified
    assert "21.5" in summary


def test_format_fields_summary_name_only_fallback():
    """A column with neither data type nor sample values falls back to name-only."""
    batch = [{
        "name": "events",
        "columns": [
            {"name": "mystery_col"},  # no data_type
            {"name": "typed_col", "data_type": "int"},  # type but no samples
        ],
        # no sample_values key at all
    }]
    summary = onboard._format_fields_summary(batch)
    lines = summary.splitlines()
    # mystery_col line is name-only (no brackets, no 'e.g.')
    mystery = [ln for ln in lines if "mystery_col" in ln][0]
    assert "[" not in mystery
    assert "e.g." not in mystery
    # typed_col carries its type but no samples
    typed = [ln for ln in lines if "typed_col" in ln][0]
    assert "[int]" in typed
    assert "e.g." not in typed


def test_format_fields_summary_missing_columns_graceful():
    assert onboard._format_fields_summary([{"name": "empty"}]) == "  empty:"
    assert onboard._format_fields_summary([]) == ""


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE #2 — Q1 guidance: business *_id mapped, bare id excluded, consistent
# ─────────────────────────────────────────────────────────────────────────────
def _prompt() -> str:
    return onboard._build_map_prompt(
        "anycompany-sys",
        "work-order, equipment-id, spare-part",
        "widgets: widget_code, id",
    )


def test_business_identifiers_no_longer_blanket_excluded():
    prompt = _prompt()
    # The old self-contradictory blanket exclusion of '*_id columns' is GONE.
    assert "*_id columns" not in prompt
    assert "bare \"id\" and *_id columns" not in prompt
    # Business/natural identifiers are explicitly mapped with role identifier.
    assert "BUSINESS / natural identifiers" in prompt
    assert "work_order_id -> work-order" in prompt
    assert "serial_number -> equipment-id" in prompt


def test_bare_id_and_surrogate_keys_still_excluded():
    prompt = _prompt()
    assert "DO NOT MAP" in prompt
    # bare id + surrogate/random/autoincrement keys are still excluded
    assert 'bare "id"' in prompt
    assert "autoincrement" in prompt
    assert "SURROGATE primary keys" in prompt


def test_guidance_is_internally_consistent():
    """The prompt must not both name a business identifier as good AND blanket-skip *_id."""
    prompt = _prompt()
    # Consistency: a _id column is skipped ONLY when surrogate/random, not blanket.
    assert 'skipped ONLY when it is a surrogate/random' in prompt
    assert "NEVER blanket-skip" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE #3 — weak-batch scorer (drives escalation to the stronger model)
# ─────────────────────────────────────────────────────────────────────────────
def test_weak_batch_empty_is_weak():
    assert onboard._is_weak_mapping_batch([]) is True
    assert onboard._is_weak_mapping_batch(None) is True


def test_weak_batch_all_needs_review_is_weak():
    batch = [
        {"field": "t.a", "mappings": []},
        {"field": "t.b", "mappings": []},
    ]
    assert onboard._is_weak_mapping_batch(batch) is True


def test_weak_batch_all_low_confidence_is_weak():
    batch = [
        {"field": "t.a", "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.2}]},
        {"field": "t.b", "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.39}]},
    ]
    assert onboard._is_weak_mapping_batch(batch) is True
    assert onboard._count_confident_fields(batch) == 0


def test_strong_batch_one_confident_field_is_not_weak():
    batch = [
        {"field": "t.a", "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.2}]},
        {"field": "t.b", "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.85}]},
    ]
    assert onboard._is_weak_mapping_batch(batch) is False
    assert onboard._count_confident_fields(batch) == 1


def test_confidence_floor_boundary_is_inclusive():
    """A field exactly at the floor (0.4) counts as usable → not weak."""
    batch = [{"field": "t.a", "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.4}]}]
    assert onboard._is_weak_mapping_batch(batch) is False


# ─────────────────────────────────────────────────────────────────────────────
# _parse_map_array — pure JSON-array extraction used by the batch loop
# ─────────────────────────────────────────────────────────────────────────────
def test_parse_map_array_extracts_embedded_json():
    text = 'Here you go: [{"field": "t.a", "mappings": []}] done.'
    parsed = onboard._parse_map_array(text)
    assert parsed == [{"field": "t.a", "mappings": []}]


def test_parse_map_array_returns_none_on_garbage():
    assert onboard._parse_map_array("no json here") is None
    assert onboard._parse_map_array("") is None
    assert onboard._parse_map_array(None) is None


# ─────────────────────────────────────────────────────────────────────────────
# db.bedrock_call — optional force-model param (used by escalation)
# ─────────────────────────────────────────────────────────────────────────────
class _FakeBody:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()


class _FakeBedrockClient:
    """Records modelIds and returns a shape matching the requested model family."""

    def __init__(self, calls):
        self._calls = calls

    def converse(self, modelId, messages, inferenceConfig, **_kw):  # noqa: N803 - boto3 kwarg name
        self._calls.append(modelId)
        text = "nova-ok" if "nova" in modelId else "claude-ok"
        return {"output": {"message": {"content": [{"text": text}]}}, "stopReason": "end_turn"}


def test_bedrock_call_forced_model_is_the_only_model_called(monkeypatch):
    import agents.db as db
    calls: list[str] = []
    monkeypatch.setattr(db.boto3, "client", lambda *a, **k: _FakeBedrockClient(calls))
    out = db.bedrock_call("hi", model="us.anthropic.claude-sonnet-4-6")
    assert out == "claude-ok"
    # The configured agent model is not tried — only the forced model is called.
    assert calls == ["us.anthropic.claude-sonnet-4-6"]


def test_bedrock_call_default_tries_the_configured_agent_model_first(monkeypatch):
    import agents.db as db
    from agents.model_catalog import resolve_id
    calls: list[str] = []
    monkeypatch.setattr(db.boto3, "client", lambda *a, **k: _FakeBedrockClient(calls))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    agent_model = resolve_id("anthropic.claude-haiku-5-5")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", agent_model)
    out = db.bedrock_call("hi")  # no model → the deployment's system-agent model first
    assert out == "claude-ok"
    assert calls == [agent_model]


# ─────────────────────────────────────────────────────────────────────────────
# Escalation gate — surrogate/audit column heuristic (bounds strong-model cost)
# ─────────────────────────────────────────────────────────────────────────────
def test_column_surrogate_and_audit_names_are_flagged():
    for name in ["id", "ID", "uuid", "guid", "row_id", "rowid", "seq", "oid",
                 "created_at", "updated_at", "modified_by", "deleted_at",
                 "record_version", "etl_batch_id", "dw_load_ts", "row_checksum"]:
        assert onboard._column_is_surrogate_or_audit(name) is True, name
    # empty/None → treated as non-mappable
    assert onboard._column_is_surrogate_or_audit("") is True
    assert onboard._column_is_surrogate_or_audit(None) is True


def test_business_identifiers_are_not_flagged_as_surrogate():
    # Business/natural identifiers must NOT be treated as surrogate/audit columns —
    # they are exactly what the mapper SHOULD land on a concept.
    for name in ["work_order_id", "serial_number", "asset_id", "widget_code",
                 "temperature_c", "location", "cell"]:
        assert onboard._column_is_surrogate_or_audit(name) is False, name


def test_batch_has_mappable_columns():
    audit_only = [{"name": "t", "columns": [
        {"name": "id"}, {"name": "created_at"}, {"name": "updated_by"}]}]
    assert onboard._batch_has_mappable_columns(audit_only) is False
    with_business = [{"name": "t", "columns": [
        {"name": "id"}, {"name": "work_order_id"}]}]
    assert onboard._batch_has_mappable_columns(with_business) is True
    assert onboard._batch_has_mappable_columns([]) is False


# ─────────────────────────────────────────────────────────────────────────────
# _maybe_escalate_batch — gate + per-job cap + adoption wiring
# ─────────────────────────────────────────────────────────────────────────────
_MAPPABLE_BATCH = [{"name": "t", "columns": [{"name": "work_order_id"}]}]
_AUDIT_BATCH = [{"name": "t", "columns": [{"name": "id"}, {"name": "created_at"}]}]
_WEAK = [{"field": "t.work_order_id", "mappings": []}]


def _escalate(monkeypatch, *, batch, batch_mappings, strong_text, budget_left=3):
    calls: list = []

    def _fake(prompt, model=None, timeout=90):
        calls.append(model)
        if isinstance(strong_text, Exception):
            raise strong_text
        return strong_text

    monkeypatch.setattr(onboard, "bedrock_call", _fake)
    out, adopted, escalated = onboard._maybe_escalate_batch(
        batch_mappings, batch, "PROMPT",
        job_id="j", batch_num=1, total_batches=1, system_id="sys",
        budget_left=budget_left,
    )
    return out, adopted, escalated, calls


def test_escalation_adopts_stronger_result_when_it_maps_more(monkeypatch):
    strong = json.dumps([
        {"field": "t.work_order_id",
         "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.9}]}])
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_MAPPABLE_BATCH, batch_mappings=_WEAK, strong_text=strong)
    assert escalated is True and adopted is True
    assert len(calls) == 1  # exactly one strong-model call
    assert out[0]["mappings"][0]["concept"] == "work-order"


def test_escalation_keeps_original_when_stronger_is_no_better(monkeypatch):
    # Strong result still maps nothing confident → not adopted, but a call WAS made.
    strong = json.dumps([{"field": "t.work_order_id", "mappings": []}])
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_MAPPABLE_BATCH, batch_mappings=_WEAK, strong_text=strong)
    assert escalated is True and adopted is False
    assert len(calls) == 1
    assert out is _WEAK  # original mappings retained


def test_escalation_skipped_for_surrogate_audit_only_batch(monkeypatch):
    # Weak but only surrogate/audit columns → correctly empty → NO strong call.
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_AUDIT_BATCH, batch_mappings=_WEAK, strong_text="unused")
    assert escalated is False and adopted is False
    assert calls == []  # no billable strong-model call fired
    assert out is _WEAK


def test_escalation_skipped_when_budget_exhausted(monkeypatch):
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_MAPPABLE_BATCH, batch_mappings=_WEAK,
        strong_text="unused", budget_left=0)
    assert escalated is False and adopted is False
    assert calls == []


def test_escalation_skipped_for_strong_batch(monkeypatch):
    strong_batch = [{"field": "t.work_order_id",
                     "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.9}]}]
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_MAPPABLE_BATCH, batch_mappings=strong_batch, strong_text="unused")
    assert escalated is False and adopted is False
    assert calls == []
    assert out is strong_batch


def test_escalation_best_effort_on_strong_call_failure(monkeypatch):
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_MAPPABLE_BATCH, batch_mappings=_WEAK,
        strong_text=RuntimeError("boom"))
    # A call was attempted (escalated True) but failed → original retained, not adopted.
    assert escalated is True and adopted is False
    assert out is _WEAK


# ─────────────────────────────────────────────────────────────────────────────
# M3 — coverage-ratio escalation trigger (<50% of mappable columns mapped)
# alongside the pinned zero-confident weak check (semantics above unchanged)
# ─────────────────────────────────────────────────────────────────────────────
def _ten_col_batch():
    """10 mappable (business) columns — generic names only."""
    return [{"name": "t", "columns": [{"name": f"metric_{i}_code"} for i in range(10)]}]


def _confident_rows(n):
    return [{"field": f"t.metric_{i}_code",
             "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.9}]}
            for i in range(n)]


def test_mappable_column_count_excludes_surrogate_audit():
    batch = [{"name": "t", "columns": [
        {"name": "id"}, {"name": "created_at"},
        {"name": "work_order_id"}, {"name": "serial_number"}]}]
    assert onboard._mappable_column_count(batch) == 2
    assert onboard._mappable_column_count([]) == 0


def test_low_coverage_two_of_ten_is_low():
    assert onboard._is_low_coverage_batch(_confident_rows(2), _ten_col_batch()) is True


def test_low_coverage_eight_of_ten_is_not_low():
    assert onboard._is_low_coverage_batch(_confident_rows(8), _ten_col_batch()) is False


def test_low_coverage_boundary_exactly_half_is_not_low():
    """5/10 == 0.5 is NOT below the ratio — the trigger is strict (<0.5)."""
    assert onboard._is_low_coverage_batch(_confident_rows(5), _ten_col_batch()) is False


def test_escalation_fires_on_partial_coverage(monkeypatch):
    """2/10 mappable columns confident: NOT weak (old gate never fired), but
    low-coverage — the M3 trigger escalates it."""
    partial = _confident_rows(2)
    assert onboard._is_weak_mapping_batch(partial) is False  # pinned semantics intact
    strong = json.dumps(_confident_rows(9))
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_ten_col_batch(), batch_mappings=partial, strong_text=strong)
    assert escalated is True and adopted is True
    assert len(calls) == 1
    assert len(out) == 9


def test_escalation_still_skipped_at_good_coverage(monkeypatch):
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_ten_col_batch(), batch_mappings=_confident_rows(8),
        strong_text="unused")
    assert escalated is False and adopted is False
    assert calls == []


def test_low_coverage_respects_budget(monkeypatch):
    out, adopted, escalated, calls = _escalate(
        monkeypatch, batch=_ten_col_batch(), batch_mappings=_confident_rows(2),
        strong_text="unused", budget_left=0)
    assert escalated is False and calls == []


# ─────────────────────────────────────────────────────────────────────────────
# _map_model — explicit map model (C4/M1b): env override, agent-role default
# ─────────────────────────────────────────────────────────────────────────────
def test_map_model_env_override(monkeypatch):
    monkeypatch.setenv("MAP_MODEL", "vendor.example-model-v9")
    assert onboard._map_model() == "vendor.example-model-v9"


def test_map_model_defaults_to_agent_role(monkeypatch):
    monkeypatch.delenv("MAP_MODEL", raising=False)
    from agents.model_catalog import AGENT, default_model
    assert onboard._map_model() == default_model(AGENT)
