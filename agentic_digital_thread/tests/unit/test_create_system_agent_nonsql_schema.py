# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression: create_system_agent must NOT crash for non-SQL systems whose "schema"
is not a SQL identifier.

Once SiteWise (Direct) / Athena / i3X / REST / MCP route through create_system_agent
(ADR-0035 Mode A), the PostgreSQL-only ``information_schema`` discovery prompt +
``validate_identifier(schema)`` must be gated to PostgreSQL. Otherwise a SiteWise
(Direct) system (schema='sitewise-direct') raises ``ValueError: Invalid identifier:
'sitewise-direct'`` at agent construction and the runtime crash-loops (observed live:
gateway target SYNCHRONIZE_UNSUCCESSFUL).
"""
import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.system_agent as sa  # noqa: E402


def test_sitewise_direct_agent_builds_with_non_identifier_schema():
    """SiteWise (Direct) with the hyphenated pseudo-schema 'sitewise-direct' must build
    an agent without raising (the PG information_schema prompt is now PostgreSQL-only)."""
    agent = sa.create_system_agent(
        system_id="example-sitewise",
        system_name="Example SiteWise",
        schema="sitewise-direct",
        system_prompt="",
        connection_type="SiteWise (Direct)",
        connection_config={},
    )
    assert agent is not None


def test_athena_agent_builds_with_hyphenatable_context():
    """Athena also routes through create_system_agent; building it must not depend on
    the PG information_schema discovery prompt."""
    agent = sa.create_system_agent(
        system_id="example-athena",
        system_name="Example Athena",
        schema="federated_process_db",
        system_prompt="",
        connection_type="Athena",
        connection_config={"workgroup": "wg", "database": "db", "region": "us-east-1"},
    )
    assert agent is not None


if __name__ == "__main__":
    test_sitewise_direct_agent_builds_with_non_identifier_schema()
    print("  ok  sitewise (direct) builds with non-identifier schema")
    test_athena_agent_builds_with_hyphenatable_context()
    print("  ok  athena builds")
    print("RESULT: PASS")
