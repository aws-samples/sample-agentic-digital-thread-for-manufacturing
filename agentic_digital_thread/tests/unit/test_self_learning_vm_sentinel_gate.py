# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Failed-lookup sentinel gate (3f) + method/reason/provenance evidence threading.

Live evidence (2026-07): a value_mapping proposal storing a FAILED
lookup (``to_value='NOT_FOUND'``, malformed to_field) reached the queue with
``ai_confidence=high`` because the scorer verified the from-side only. The
ingest gate must drop such proposals — they record a failed search, not a
learned mapping. Separately, the orchestrator's parsed ``method:``/``reason:``
proposal fields were being DROPPED after parse; they now persist on the item
(with ``provenance`` naming the producing pipeline) so review cards can show WHY.

Offline: DynamoDB behind a FakeTable, Neptune writes behind a no-op spy.
GENERIC — synthetic systems/fields/values only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import agents.self_learning as self_learning


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


_RUNTIME_APP = Path(__file__).resolve().parents[2] / "agents" / "runtime_app.py"


class FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        result.sort(key=lambda x: x.get("discovered_at", ""),
                    reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(i) for i in result]}

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)


@pytest.fixture()
def table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", lambda *a, **k: True)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return table


def _log_vm(to_value: str, to_field: str = "asset.machine_code", **kw):
    return self_learning.log_proposed_mapping(
        "sys-a", "wo.machine_id", "MX-100",
        "sys-b", to_field, to_value,
        "equipment-id", 0.9, "which machine ran order 7?",
        proposed_type="value_mapping", **kw,
    )


# --------------------------------------------------------------------------- #
# Pure sentinel predicate
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", [
    "NOT_FOUND", "not found", "Not-Found", "N/A", "n-a", "NA",
    "None", "null", "NIL", "unknown", "MISSING", "error", "-", "//",
])
def test_sentinel_shapes_detected(value):
    assert self_learning._is_failed_lookup_sentinel(value) is True


@pytest.mark.parametrize("value", [
    # Live case (2026-07): speculative orchestrator proposal after a FAILED
    # identifier recovery — fully parenthesized annotation.
    "(unknown — pending resolution)",
    "unknown — pending resolution",   # no parens: token UNKNOWN fires
    "NOT_FOUND_IN_MES",               # token NOT_FOUND fires
    "TBD",                            # new sentinel vocabulary
    "pending", "Unresolved",          # new sentinel vocabulary
    "(see note)",                     # any fully parenthesized "value"
    "PENDING_LOOKUP",                 # token PENDING fires
])
def test_annotation_shapes_detected(value):
    assert self_learning._is_failed_lookup_sentinel(value) is True


@pytest.mark.parametrize("value", [
    "", "  ",              # empty input is EXEMPT (reason-code empty-label contract)
    "MX-100", "Press Line 1", "NONESUCH", "ERR-42", "unavailable-zone",
    "WO-20260707-00317",                       # real work-order identifier
    "0292d6db-98e4-4f6d-8791-e2ebd53a1288",    # UUID
    "ANYNAVCO-42",     # token ANYNAVCO is not a sentinel (substring 'NA' safe)
    "PENDINGTON-9",    # tokens PENDINGTON/9 — whole-token matching only
])
def test_non_sentinels_pass(value):
    assert self_learning._is_failed_lookup_sentinel(value) is False


def test_field_side_whole_match_only():
    """token_match=False (the to_field path): a legitimate table.column name
    carrying a sentinel token must NOT be flagged; a whole-match still is."""
    assert self_learning._is_failed_lookup_sentinel(
        "orders.missing_field", token_match=False) is False
    assert self_learning._is_failed_lookup_sentinel(
        "NOT_FOUND", token_match=False) is True


# --------------------------------------------------------------------------- #
# Ingest gate over log_proposed_mapping
# --------------------------------------------------------------------------- #

def test_vm_with_not_found_to_value_is_dropped(table):
    assert _log_vm("NOT_FOUND") is None
    assert table.all_items() == []


@pytest.mark.parametrize("sentinel", ["N/A", "None", "null", "not found", "-"])
def test_vm_with_sentinel_variants_is_dropped(table, sentinel):
    assert _log_vm(sentinel) is None
    assert table.all_items() == []


def test_vm_with_sentinel_to_field_is_dropped(table):
    assert _log_vm("MX-200", to_field="NOT_FOUND") is None
    assert table.all_items() == []


def test_vm_with_pending_resolution_annotation_is_dropped(table):
    """LIVE CASE (2026-07): a speculative value_mapping with
    ``to_value='(unknown — pending resolution)'`` — emitted after a FAILED
    identifier recovery — must be dropped by gate 3f at ingestion."""
    assert _log_vm("(unknown — pending resolution)") is None
    assert table.all_items() == []


@pytest.mark.parametrize("annotation", [
    "unknown — pending resolution", "NOT_FOUND_IN_MES", "TBD", "(see note)",
])
def test_vm_with_annotation_variants_is_dropped(table, annotation):
    assert _log_vm(annotation) is None
    assert table.all_items() == []


def test_vm_with_sentinel_token_in_to_field_column_still_stored(table):
    """Field side stays whole-match: a legitimate column name carrying a
    sentinel token (missing/error/pending) must not be dropped."""
    mid = _log_vm("MX-200", to_field="asset.missing_field")
    assert mid is not None
    assert table.by_id(mid)["status"] == "pending"


def test_legitimate_vm_still_stored(table):
    mid = _log_vm("MX-200")
    assert mid is not None
    assert table.by_id(mid)["status"] == "pending"


def test_empty_label_vm_still_allowed(table):
    """The reason-code path proposes a pending VM with an EMPTY label ('never
    fabricated') for human fill-in — the sentinel gate must NOT drop it."""
    mid = _log_vm("")
    assert mid is not None
    assert table.by_id(mid)["proposed_type"] == "value_mapping"


def test_gate_only_applies_to_value_mappings(table):
    """A non-VM type carrying an incidental 'None'-ish value is untouched."""
    mid = self_learning.log_proposed_mapping(
        "sys-a", "wo.machine_id", "",
        "sys-b", "asset.machine_code", "",
        "equipment-id", 0.8, "Auto-analysis: value-overlap join",
        proposed_type="join_path",
    )
    assert mid is not None


# --------------------------------------------------------------------------- #
# method/reason/provenance evidence threading (round-trip)
# --------------------------------------------------------------------------- #

def test_method_reason_provenance_persist_on_item(table):
    mid = _log_vm(
        "MX-200",
        method="looked up sys-b asset registry by serial",
        reason="serial suffix matched across both systems",
        provenance="orchestrator",
    )
    item = table.by_id(mid)
    assert item["method"] == "looked up sys-b asset registry by serial"
    assert item["reason"] == "serial suffix matched across both systems"
    assert item["provenance"] == "orchestrator"


def test_empty_evidence_fields_not_stored(table):
    """DynamoDB empty-attr convention: absent when empty (like transform etc.)."""
    item = table.by_id(_log_vm("MX-200"))
    assert "method" not in item
    assert "reason" not in item
    assert "provenance" not in item


def test_runtime_app_threads_evidence_into_proposals():
    """SOURCE TRIPWIRE: the orchestrator's VM parse threads its parsed
    method:/reason: fields (previously dropped) and tags provenance; the
    analysis-LLM field_annotation path tags its own provenance."""
    src = _RUNTIME_APP.read_text(encoding="utf-8")
    assert 'provenance="orchestrator"' in src
    assert 'provenance="analysis-llm"' in src
    assert "method=method.group(1).strip() if method else" in src
    assert "reason=reason.group(1).strip() if reason else" in src


def test_runtime_app_scans_past_malformed_blocks_until_five_successes():
    import agents.runtime_app as runtime_app

    malformed = [
        "from_system: sys-a\nfrom_value: A\nto_system: sys-b\nto_value: B"
        for _ in range(5)
    ]
    valid = [
        "\n".join((
            "from_system: sys-a",
            "from_field: records.external_id",
            f"from_value: A-{index}",
            "to_system: sys-b",
            "to_field: entities.canonical_id",
            f"to_value: B-{index}",
            "confidence: high",
        ))
        for index in range(6)
    ]
    writes: list[dict] = []

    def log_mapping(**kwargs):
        writes.append(kwargs)
        return f"mapping-{len(writes)}"

    accepted = runtime_app._ingest_value_mapping_proposals(
        malformed + valid,
        prompt="resolve identifiers",
        tool_calls_count=3,
        log_mapping=log_mapping,
    )

    assert accepted == 5
    assert [write["from_value"] for write in writes] == [
        "A-0", "A-1", "A-2", "A-3", "A-4",
    ]
    assert all(write["from_field"] and write["to_field"] for write in writes)


def test_runtime_app_accepts_valid_blocks_after_twenty_five_malformed_prefixes():
    import agents.runtime_app as runtime_app

    malformed = [
        "from_system: sys-a\nfrom_value: A\nto_system: sys-b\nto_value: B"
        for _ in range(25)
    ]
    valid = [
        "\n".join((
            "from_system: sys-a",
            "from_field: records.external_id",
            f"from_value: A-{index}",
            "to_system: sys-b",
            "to_field: entities.canonical_id",
            f"to_value: B-{index}",
        ))
        for index in range(3)
    ]
    writes: list[dict] = []

    accepted = runtime_app._ingest_value_mapping_proposals(
        malformed + valid,
        prompt="resolve identifiers",
        tool_calls_count=3,
        log_mapping=lambda **kwargs: writes.append(kwargs) or f"mapping-{len(writes)}",
    )

    assert accepted == 3
    assert [write["from_value"] for write in writes] == ["A-0", "A-1", "A-2"]
