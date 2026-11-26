# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""No model-facing text tells an Athena caller to always filter on partitions.

A bound on a storage (partition) column the question did not ask about drops
rows and changes the result, and an over-limit COUNT, SUM, MIN or MAX does
not need one: the connector computes it exactly from partition pieces. The
system agent's own prompts already carry ``storage_bound_rule.PARTITION_RULE``.
The same "always add a partition filter" instruction survived in the Mode-B
orchestrator text (the explore_graph schema note, the graph-first seed
header, the shared Mode-B workflow guidance), in the default Athena guidance
for a system, and in the scan-limit recovery text the connector returns. Each
now carries the rule (or text derived from it). Offline.
"""
from __future__ import annotations

import json
import re

import pytest

import agents.athena_tools as at
import agents.connection_types as ct
import agents.orchestrator as orch
import agents.workflow_guidance as wg
from agents.storage_bound_rule import PARTITION_RULE

from .test_orchestrator_modeb_schema_routing import _install_seams

#: The old instruction, in every form it took.
_ALWAYS_FILTER = re.compile(
    r"ALWAYS add a WHERE filter on the partition_keys"
    r"|ALWAYS constrain with a WHERE filter \(partition"
    r"|filter on a partition column FIRST"
    r"|add a partition or time-range predicate"
    r"|add a WHERE filter on (?:a|the table's) partition column",
    re.IGNORECASE)


def _mode_b_note(monkeypatch) -> str:
    import agents.schema_cache as schema_cache
    from types import SimpleNamespace
    schema_cache.reset()
    monkeypatch.setattr(orch, "get_config",
                        lambda: SimpleNamespace(has_gateway=False, agentcore_gateway_url=None))
    _install_seams(monkeypatch, traversal_result=json.dumps({"results": []}),
                   systems=[{"id": "system-a", "name": "System A", "connection_type": "Athena"}])
    return json.loads(orch._explore_graph_with_schema("MATCH (s:System) RETURN s"))[
        "routed_system_schemas_note"]


def _partition_hint(monkeypatch, columns) -> str:
    monkeypatch.setattr(at, "_referenced_tables", lambda _sql: ["table_x"])
    monkeypatch.setattr(at, "_introspect_columns", lambda *_a: columns)
    return at._partition_hint_from_tables(object(), {}, "SELECT col_a FROM table_x")


def _seams(monkeypatch) -> dict[str, str]:
    return {
        "mode B explore_graph schema note": _mode_b_note(monkeypatch),
        "graph-first seed header": orch._GRAPH_FIRST_SEED_HEADER,
        "mode B workflow guidance": wg.MODE_B_WORKFLOW_GUIDANCE,
        "mode B orchestrator prompt": orch._build_orchestrator_prompt("orchestrator"),
        "default Athena guidance": ct._ATHENA_GUIDANCE,
    }


def test_no_prompt_seam_says_to_always_filter_on_partitions(monkeypatch):
    for name, text in _seams(monkeypatch).items():
        flat = " ".join(text.split())
        assert not _ALWAYS_FILTER.search(flat), name
        assert PARTITION_RULE in flat, name


@pytest.mark.parametrize("columns", [
    [("col_d", "string", "partition key"), ("col_a", "string", "")],
    [],
])
def test_scan_limit_recovery_says_to_narrow_the_questions_window(monkeypatch, columns):
    hint = _partition_hint(monkeypatch, columns)
    for text in (at._ENGINE_SCAN_LIMIT_GUIDANCE, hint):
        flat = " ".join(text.split())
        assert not _ALWAYS_FILTER.search(flat), text
        assert "time column the question asks about" in flat, text
    # Known partition columns are still named, as facts.
    if columns:
        assert "col_d" in hint


def test_the_mode_b_text_says_oversized_totals_are_computed_exactly(monkeypatch):
    for name, text in _seams(monkeypatch).items():
        if name == "default Athena guidance":
            continue
        assert "computed exactly" in " ".join(text.split()), name
