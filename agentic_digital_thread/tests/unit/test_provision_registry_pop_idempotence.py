# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression: provision re-verifies control-plane state even when the in-memory
agent registry carries a stale entry.

Live failure (2026-07-14, mgmt env): scripts/reonboard.py deleted adt_cmms via
the control plane, then re-provisioned. keep-warm had re-registered cmms in
agent_manager._agents while the runtime still existed, so start_agent()'s
in-memory fast path returned early and provision SKIPPED runtime recreation —
200 OK with no runtime (client waited 25 min on ABSENT). The provision path at
onboard.py ~L4142 was missing the registry pop the other provision paths have.
"""
import re
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"


def test_provision_pg_start_agent_site_is_preceded_by_registry_pop():
    """The PostgreSQL provision path's start_agent call (the 2026-07-14 cmms
    incident site — follows the demo_conn_params_for_schema block) must be
    preceded by a registry pop so a stale in-memory entry can never
    short-circuit runtime recreation after out-of-band deletion."""
    src = (AGENTS_DIR / "onboard.py").read_text()
    anchor = src.find("agent_conn_params = demo_conn_params_for_schema(schema)")
    assert anchor != -1, "PostgreSQL provision conn-params block not found"
    site = src.find("mcp_url = _agent_manager.start_agent(", anchor)
    assert site != -1, "start_agent call after the conn-params block not found"
    window = src[anchor:site]
    assert re.search(r"_agents\"?, \{\}\)\.pop\(system_id, None\)|_agents\.pop\(system_id, None\)", window), (
        "the PG provision start_agent site lacks a preceding registry pop — a "
        "stale in-memory entry would skip runtime recreation (2026-07-14 cmms incident)"
    )


def test_start_agent_fast_path_trusts_registry_documented():
    """The early return in agent_manager.start_agent is the trap the pops guard
    against — pin that it still exists so the guard rationale stays valid (if
    this fails, the pops may be removable; revisit deliberately)."""
    src = (AGENTS_DIR / "agent_manager.py").read_text()
    assert re.search(r"if system_id in self\._agents:\s*\n\s*return self\._agents\[system_id\]\.mcp_url", src)
