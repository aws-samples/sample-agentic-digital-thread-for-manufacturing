# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Set-equality cross-pin between ``docs/graph-shape-contract.md`` and
``agents/metamodel.py``.

The vocabulary lives in THREE places: the metamodel module (the graph-side
source of truth), the pinned sets in ``tests/unit/test_metamodel.py``, and the
graph-shape contract doc. The first two are pinned to each other by
test_metamodel.py; this file pins the DOC — its machine-readable
``METAMODEL-VOCAB`` block (§6) must set-equal the metamodel's labels/names,
and every listed name must actually appear in the doc's §2/§3 tables — so a
doc edit that drifts from the code (or vice versa) fails the offline lane.

Also pins the doc↔audit-script verbatim-cypher convention for the equipment-bridge
additions (family (e) + the metamodel probe), the MAPS_TO_ASSET /
ValueMapping field documentation, and the named writers.

Fully offline: reads the doc from disk; imports are monkeypatch-free.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Make `agents...` / `tests.audit...` importable (agentic_digital_thread/).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.metamodel as metamodel  # noqa: E402
from tests.audit import audit_onboarding_graph as audit  # noqa: E402

_DOC_PATH = _ADT_ROOT.parent / "docs" / "graph-shape-contract.md"
_DOC = _DOC_PATH.read_text(encoding="utf-8")

_VOCAB_BEGIN = "<!-- METAMODEL-VOCAB:BEGIN -->"
_VOCAB_END = "<!-- METAMODEL-VOCAB:END -->"


def _vocab_block() -> str:
    assert _VOCAB_BEGIN in _DOC and _VOCAB_END in _DOC, (
        "docs/graph-shape-contract.md lost its METAMODEL-VOCAB block (§6) — "
        "the machine-readable cross-pin anchor"
    )
    start = _DOC.index(_VOCAB_BEGIN) + len(_VOCAB_BEGIN)
    return _DOC[start:_DOC.index(_VOCAB_END)]


def _doc_set(key: str) -> set[str]:
    """Parse ``key: A, B, C`` out of the vocab block into a set."""
    match = re.search(rf"^{key}:\s*(.+)$", _vocab_block(), re.MULTILINE)
    assert match, f"'{key}:' line missing from the METAMODEL-VOCAB block"
    return {item.strip() for item in match.group(1).split(",") if item.strip()}


# ---------------------------------------------------------------------------
# (1) Set equality: doc vocab block == metamodel vocabularies
# ---------------------------------------------------------------------------

def test_doc_node_labels_equal_metamodel():
    doc_labels = _doc_set("node-labels")
    meta_labels = {m["label"] for m in metamodel.META_NODES}
    assert doc_labels == meta_labels, (
        "docs/graph-shape-contract.md §6 node-labels drifted from "
        "agents/metamodel.py META_NODES — edit both together (ownership rule).\n"
        f"missing from doc: {sorted(meta_labels - doc_labels)}\n"
        f"stale in doc: {sorted(doc_labels - meta_labels)}"
    )


def test_doc_edge_types_equal_metamodel():
    doc_edges = _doc_set("edge-types")
    meta_edges = {e["name"] for e in metamodel.META_EDGES}
    assert doc_edges == meta_edges, (
        "docs/graph-shape-contract.md §6 edge-types drifted from "
        "agents/metamodel.py META_EDGES — edit both together (ownership rule).\n"
        f"missing from doc: {sorted(meta_edges - doc_edges)}\n"
        f"stale in doc: {sorted(doc_edges - meta_edges)}"
    )


# ---------------------------------------------------------------------------
# (2) The vocab block cannot drift from the doc BODY: every listed name must
#     appear bolded in the §2 node table / §3 edge table.
# ---------------------------------------------------------------------------

def test_every_vocab_name_appears_in_doc_tables():
    missing = [
        name
        for name in sorted(_doc_set("node-labels") | _doc_set("edge-types"))
        if f"**{name}**" not in _DOC
    ]
    assert not missing, (
        f"vocab names with no bolded §2/§3 table row in the contract doc: {missing}"
    )


# ---------------------------------------------------------------------------
# (3) Equipment-bridge additions documented with writers named
# ---------------------------------------------------------------------------

def test_maps_to_asset_documented_with_writer_and_fields():
    assert "**MAPS_TO_ASSET**" in _DOC
    assert "`(ValueMapping)→(Asset)`" in _DOC
    assert "_persist_equipment_value_mappings" in _DOC  # the named sole writer
    # ValueMapping's new fields are documented in the §2 node row.
    assert "to_element_id" in _DOC and "to_path" in _DOC


def test_metamodel_nodes_documented_with_writer_and_ownership_rule():
    assert "**MetaNode**" in _DOC and "**MetaEdge**" in _DOC
    assert "write_metamodel" in _DOC  # the named sole writer
    assert "idempotent" in _DOC.lower()
    # The ownership rule is quoted from the module docstring (§1) — compare
    # whitespace-normalized because the docstring wraps mid-phrase.
    rule = "MUST add its entry here"
    assert rule in " ".join(_DOC.split())
    assert rule in " ".join((metamodel.__doc__ or "").split())


# ---------------------------------------------------------------------------
# (4) Doc ↔ audit-script verbatim-cypher convention for the equipment-bridge checks
# ---------------------------------------------------------------------------

def test_round2_audit_cypher_copied_verbatim_into_doc():
    for const in (
        audit.CYPHER_EQUIPMENT_VM_ASSET_EDGES,
        audit.CYPHER_MAPS_TO_ASSET_FROM_BARE_VM,
        audit.CYPHER_META_EDGE_NAMES,
        audit.CYPHER_LIVE_EDGE_TYPES,
    ):
        assert const in _DOC, (
            "audit cypher not copied verbatim into docs/graph-shape-contract.md "
            f"§4 (doc and script must assert against identical strings): {const}"
        )
