# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the S4.3 onboarding ``expected_unmapped`` field (ADR 0007).

The map-PROPOSAL payload built in ``agents.onboard._run_map_job`` gains an ADDITIVE,
GATED, non-blocking ``expected_unmapped`` key. The gate is the established impure-boundary
flag ``RESOLVER_AFFINITY`` (read with ``os.environ.get("RESOLVER_AFFINITY", "").strip()``).

Contract:
  * flag OFF  => the proposal keys are EXACTLY ``{"mappings", "vocab_fit"}`` (the C5
                 ``vocab_fit`` block is ALWAYS-ON additive — ``_run_map_job`` passes the
                 discovered tables; ``expected_unmapped`` stays flag-gated).
  * flag ON   => ``expected_unmapped`` == sorted(CONCEPT_AFFINITY[system_type] - proposed),
                 unknown/absent system_type => ``[]``; the ``mappings`` list is unchanged.

These drive ``_run_map_job`` SYNCHRONOUSLY (the threading lives only in the ``/map`` route),
with ``bedrock_call`` monkeypatched to a canned JSON array and ``catalog.get`` monkeypatched
to a controlled entry — no DB, no AWS, no network, no LLM (mirrors the module-global
monkeypatch style in ``test_discover_gate.py`` / ``test_rederive.py``).
"""
from __future__ import annotations

import json

import pytest

import agents.onboard as onboard
from agents.catalog import CONCEPT_AFFINITY, missing_expected

# Canned LLM response: one field mapped to a single CORE concept ("equipment-id").
_CANNED_MAPPINGS = [
    {
        "field": "assets.serial_number",
        "table": "assets",
        "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.95}],
        "reasoning": "asset serial is the equipment identity",
    }
]
_TABLES = [{
    "name": "assets", "relation_kind": "base_table", "schema_revision": "a" * 32,
    "columns": [{"name": "serial_number", "data_type": "varchar"}],
}]


def _run_map(monkeypatch, *, system_entry):
    """Drive ``_run_map_job`` synchronously and return the stored result dict.

    ``bedrock_call`` is canned to ``_CANNED_MAPPINGS`` (kwargs-tolerant — map
    calls now pass an explicit ``model=``); ``catalog.get`` returns
    ``system_entry`` (or None) so the test controls the resolved ``system_type``.
    """
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda prompt, **_kw: json.dumps(_CANNED_MAPPINGS))
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: system_entry)

    job_id = "testjob"
    onboard._map_jobs.pop(job_id, None)
    onboard._run_map_job(job_id, "any-system", _TABLES)

    job = onboard._map_jobs[job_id]
    assert job["status"] == "done", f"job did not complete: {job}"
    return job["result"]


_CMMS_ENTRY = {"system_type": "CMMS", "connection_type": "PostgreSQL"}


def test_flag_off_result_keys_exactly_mappings(monkeypatch):
    """Flag OFF => keys == {'mappings', 'vocab_fit'} (C5 additive) — no affinity field."""
    monkeypatch.delenv("RESOLVER_AFFINITY", raising=False)
    result = _run_map(monkeypatch, system_entry=_CMMS_ENTRY)
    assert set(result.keys()) == {"mappings", "vocab_fit"}
    assert "expected_unmapped" not in result


def test_flag_off_blank_value_treated_as_off(monkeypatch):
    """A whitespace-only flag value is stripped to falsy => still OFF (no field)."""
    monkeypatch.setenv("RESOLVER_AFFINITY", "   ")
    result = _run_map(monkeypatch, system_entry=_CMMS_ENTRY)
    assert set(result.keys()) == {"mappings", "vocab_fit"}


def test_flag_on_expected_unmapped_is_sorted_affinity_minus_proposed(monkeypatch):
    """Flag ON => expected_unmapped == sorted(CONCEPT_AFFINITY[type] - proposed)."""
    monkeypatch.setenv("RESOLVER_AFFINITY", "1")
    result = _run_map(monkeypatch, system_entry=_CMMS_ENTRY)

    proposed = {"equipment-id"}
    expected = sorted(CONCEPT_AFFINITY["CMMS"] - proposed)
    assert result["expected_unmapped"] == expected
    assert result["expected_unmapped"] == missing_expected("CMMS", proposed)
    # Sorted, deterministic, and equipment-id (the proposed concept) is excluded.
    assert result["expected_unmapped"] == sorted(result["expected_unmapped"])
    assert "equipment-id" not in result["expected_unmapped"]


def test_flag_on_mappings_list_unchanged(monkeypatch):
    """Flag ON must not alter the mappings list — only add the additive key."""
    monkeypatch.setenv("RESOLVER_AFFINITY", "1")
    result = _run_map(monkeypatch, system_entry=_CMMS_ENTRY)
    assert set(result.keys()) == {"mappings", "expected_unmapped", "vocab_fit"}
    assert len(result["mappings"]) == 1
    assert result["mappings"][0]["concepts"] == [{"concept": "equipment-id", "confidence": 0.95}]


def test_flag_on_unknown_system_type_returns_empty(monkeypatch):
    """Flag ON + a system_type absent from CONCEPT_AFFINITY => []."""
    monkeypatch.setenv("RESOLVER_AFFINITY", "1")
    result = _run_map(
        monkeypatch,
        system_entry={"system_type": "NotARealType", "connection_type": "PostgreSQL"},
    )
    assert result["expected_unmapped"] == []


def test_flag_on_absent_catalog_entry_returns_empty(monkeypatch):
    """Flag ON + no catalog entry (system_type unresolved) => []."""
    monkeypatch.setenv("RESOLVER_AFFINITY", "1")
    result = _run_map(monkeypatch, system_entry=None)
    assert result["expected_unmapped"] == []
