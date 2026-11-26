# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Agents tab's fleet totals and overlapping model buckets.

Bedrock reports tokens per ModelId, not per runtime. When a model bucket is in
both roles' pools (the same model picked for both roles, or a legacy bucket),
its tokens appear in both roles' rows and cannot be split. The rows keep their
role's usage and are flagged; the fleet totals come from the model-level
union of both pools, each bucket counted exactly once.

Offline: no AWS, no network.
"""
from __future__ import annotations

import pytest

from agents import agents_routes
from agents.model_catalog import resolve_id

REGION = "us-east-1"
SONNET_5 = resolve_id("anthropic.claude-sonnet-5", REGION)
SONNET_46 = resolve_id("anthropic.claude-sonnet-4-6", REGION)
HAIKU = resolve_id("anthropic.claude-haiku-4-5-20251001-v1:0", REGION)


def _parsed(models: dict) -> dict:
    return {"models": {mid: {"input": i, "output": o, "latency_ms": None}
                       for mid, (i, o) in models.items()}}


def _row(name: str, invocations: int, no_llm: bool = False) -> dict:
    return {"name": name, "invocations_24h": invocations, "no_llm": no_llm,
            "input_tokens_24h": 0, "output_tokens_24h": 0, "cost_usd_24h": 0.0,
            "cost_per_request": 0.0}


def _fleet(models: dict, orch_model: str, agent_model: str,
           system_invocations: tuple[int, ...] = (10,)) -> list[dict]:
    orch, agent, overlap, fleet = agents_routes._fleet_token_pools(
        _parsed(models), REGION, orch_model, agent_model)
    rows = [_row("adt_orchestrator", 5)]
    rows[0].update(input_tokens_24h=orch["input"], output_tokens_24h=orch["output"],
                   cost_usd_24h=orch["cost"])
    rows += [_row(f"adt_sys{i}", n) for i, n in enumerate(system_invocations)]
    rows.append(_row("adt_digital_thread", 3, no_llm=True))
    agents_routes._share_agent_pool(rows, agent)
    agents_routes._mark_fleet_totals(rows, overlap, fleet)
    return rows


def _totals(rows: list[dict]) -> tuple[int, int, float]:
    r = rows[0]
    return r["fleet_input_tokens_24h"], r["fleet_output_tokens_24h"], r["fleet_cost_usd_24h"]


def test_one_model_in_both_roles_shows_in_both_rows_and_totals_once():
    rows = _fleet({SONNET_5: (1000, 100)}, SONNET_5, SONNET_5)
    orch, sys0, graph = rows
    assert (orch["input_tokens_24h"], sys0["input_tokens_24h"]) == (1000, 1000)
    assert orch["model_shared_across_roles"] and sys0["model_shared_across_roles"]
    assert "model_shared_across_roles" not in graph  # tools-only, no model
    assert _totals(rows)[:2] == (1000, 100)


def test_idle_system_agents_never_erase_the_measured_total():
    """No system-agent invocations: the shared tokens are on the orchestrator
    row only, and the fleet total still reports every measured token."""
    rows = _fleet({SONNET_5: (1000, 100)}, SONNET_5, SONNET_5, system_invocations=(0,))
    assert rows[0]["input_tokens_24h"] == 1000
    assert _totals(rows)[:2] == (1000, 100)
    assert _totals(rows)[2] > 0


def test_share_rounding_does_not_lose_tokens_from_the_total():
    rows = _fleet({SONNET_5: (5, 1), HAIKU: (10, 0)}, SONNET_5, HAIKU,
                  system_invocations=(1, 1, 1))
    assert sum(r["input_tokens_24h"] for r in rows) == 5 + 9  # 3 + 3 + 3 per row
    assert _totals(rows)[:2] == (15, 1)                      # exact


def test_a_legacy_bucket_overlap_is_flagged_and_counted_once():
    """Orchestrator on Sonnet 5, agents on Sonnet 4.6: the Sonnet 4.6 bucket is
    also the orchestrator pool's legacy bucket."""
    rows = _fleet({SONNET_5: (1000, 100), SONNET_46: (200, 20)}, SONNET_5, SONNET_46)
    assert rows[0]["model_shared_across_roles"]
    assert _totals(rows)[:2] == (1200, 120)


def test_distinct_models_are_unflagged_and_total_the_sum():
    rows = _fleet({SONNET_5: (1000, 100), HAIKU: (500, 50)}, SONNET_5, HAIKU)
    orch, sys0, _graph = rows
    assert (orch["input_tokens_24h"], sys0["input_tokens_24h"]) == (1000, 500)
    assert not any("model_shared_across_roles" in r for r in rows)
    assert _totals(rows)[:2] == (1500, 150)
    assert _totals(rows)[2] == pytest.approx(orch["cost_usd_24h"] + sys0["cost_usd_24h"], abs=1e-4)



def test_an_unrelated_catalog_model_is_not_counted():
    """CloudWatch AWS/Bedrock is account-wide. A catalog model ADT is not
    configured to run (another workload's Opus 4.8) stays out of the totals."""
    opus_48 = resolve_id("anthropic.claude-opus-4-8", REGION)
    rows = _fleet({SONNET_5: (100, 10), HAIKU: (200, 20), opus_48: (1_000_000, 0)},
                  SONNET_5, HAIKU)
    assert _totals(rows)[:2] == (300, 30)
