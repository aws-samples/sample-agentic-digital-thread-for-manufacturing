# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Predefined simulation scenarios — the single source of truth.

Used by both the Streamlit ERP dashboard buttons
(`erp_simulation/streamlit_dashboard.py`) and the local CLI
(`scripts/simulate_local.py --scenario` / `scripts/local_dev.sh`).

A scenario is a set of `simulation_config` overrides the simulators read each
cycle (downtime probability, quality pass rate, lead times, payment rate, …).
Applying one writes those rows; the effect appears as subsequent cycles run.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Scenario name → {"name": <display>, "config": {config_key: value}}.
SCENARIOS: dict[str, dict] = {
    "normal": {
        "name": "Normal Operations",
        "config": {
            "orders_per_cycle_min": 4,
            "orders_per_cycle_max": 8,
            "order_quantity_min": 50,
            "order_quantity_max": 200,
            "production_batch_size_min": 500,
            "production_batch_size_max": 600,
            "fg_reorder_point": 200,
            "mes_line1_rate": 120,
            "mes_line2_rate": 120,
            "mes_line3_rate": 100,
            "mes_downtime_probability": 0.10,
            # The seeded floor and cap (mes_simulator.DEFAULT_DOWNTIME_DURATION_MIN / _MAX).
            "mes_downtime_duration_min": 5,
            "mes_downtime_duration_max": 60,
            # Restores the seeded default after Quality Issues.
            "mes_quality_pass_rate_min": 0.998,  # nosec B105 - not a credential
            "mes_quality_pass_rate_max": 1.0,  # nosec B105 - not a credential
            "mes_outage_enabled": "false",
            "procurement_lead_time_min": 1,
            "procurement_lead_time_max": 3,
            "procurement_delivery_reliability": 1.0,
            "procurement_delivery_variability_minutes": 120.0,
            "finance_payment_probability": 0.30,
        },
    },
    "demand_surge": {
        "name": "Demand Surge",
        "config": {
            "orders_per_cycle_min": 5,
            "orders_per_cycle_max": 10,
            "order_quantity_min": 200,
            "order_quantity_max": 500,
        },
    },
    "mes_outage": {
        "name": "MES Outage",
        "config": {
            "mes_outage_enabled": "true",
            "mes_outage_duration_minutes": 120,
        },
    },
    "supply_crisis": {
        "name": "Supply Crisis",
        "config": {
            "procurement_lead_time_min": 10,
            "procurement_lead_time_max": 30,
            "procurement_delivery_reliability": 0.5,
        },
    },
    "equipment_down": {
        "name": "Equipment Down",
        "config": {
            "mes_downtime_probability": 0.5,
            "mes_downtime_duration_min": 30,
            "mes_downtime_duration_max": 120,
        },
    },
    "cash_crunch": {
        "name": "Cash Crunch",
        "config": {
            "finance_payment_probability": 0.05,
        },
    },
    "quality_issues": {
        "name": "Quality Issues",
        "config": {
            "mes_quality_pass_rate_min": 0.5,  # nosec B105 - not a credential
            "mes_quality_pass_rate_max": 0.7,  # nosec B105 - not a credential
        },
    },
    "capacity_plus": {
        "name": "Capacity+",
        "config": {
            "mes_line1_rate": 200,
            "mes_line2_rate": 200,
            "mes_line3_rate": 180,
            "mes_downtime_probability": 0.02,
        },
    },
}


def list_scenarios() -> list[str]:
    """Return the available scenario names (insertion order)."""
    return list(SCENARIOS.keys())


def _infer_data_type(value) -> str:
    """Map a Python value to the simulation_config.data_type enum."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if str(value).lower() in ("true", "false"):
        return "boolean"
    return "string"


def _upsert_config(cur, key: str, value, *, category: str, updated_by: str) -> None:
    """Insert-or-update one simulation_config row.

    The table marks data_type/category/default_value NOT NULL, so they must be
    supplied on insert; ON CONFLICT only refreshes the live value. This is more
    robust than a bare UPDATE (it also works if a key wasn't pre-seeded).
    """
    cur.execute(
        """
        INSERT INTO simulation_config
            (config_key, config_value, data_type, category, default_value, updated_at, updated_by)
        VALUES (%s, %s, %s, %s, %s, NOW(), %s)
        ON CONFLICT (config_key) DO UPDATE
            SET config_value = EXCLUDED.config_value,
                updated_at   = NOW(),
                updated_by   = EXCLUDED.updated_by
        """,
        (key, str(value), _infer_data_type(value), category, str(value), updated_by),
    )


def apply_scenario(conn, scenario_name: str) -> dict:
    """Apply a scenario's config overrides + record it as the active scenario.

    Writes to `simulation_config` using *conn* but does NOT commit — the caller
    controls the transaction (the Streamlit dashboard's connection() context
    auto-commits; the CLI commits explicitly).

    Returns the scenario dict. Raises KeyError for an unknown scenario.
    """
    if scenario_name not in SCENARIOS:
        raise KeyError(scenario_name)
    scenario = SCENARIOS[scenario_name]
    updated_by = f"scenario:{scenario_name}"
    with conn.cursor() as cur:
        for key, value in scenario["config"].items():
            _upsert_config(cur, key, value, category="scenario", updated_by=updated_by)
        _upsert_config(cur, "active_scenario", scenario_name, category="dashboard", updated_by=updated_by)
    logger.info("Applied scenario '%s' (%d config keys)", scenario_name, len(scenario["config"]))
    return scenario
