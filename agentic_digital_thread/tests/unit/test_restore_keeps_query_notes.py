# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A restored PostgreSQL agent keeps the column notes its registration owns.

Provision appends a manifest system's ``agent_query_notes`` (column meanings
such as what the plant CMMS's work-order status codes mean) to the system
agent's prompt as a QUERY HINTS block (``onboard._agent_query_notes_block``).
The startup restore started every PostgreSQL agent with ``system_prompt=""``.
When the restore cannot take the cloud fast path (the runtime was deleted or
failed) or runs in local mode, ``AgentManager`` rewrites
``adt/agent-config/<id>`` as a whole value and writes ``system_prompt`` only
when it is non-empty, so the recreated agent lost the notes until the next
full provision.

The restore now rebuilds the same block from the manifest once the system's
origin is decided: a demo registration gets it, a user's own registration
that reuses the id gets none, and so does a system whose own connection
secret could not be read (its origin is undecided). A system with no notes
keeps ``system_prompt=""``. Offline: no AWS, no graph, no database.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard_preflight as op  # noqa: E402
import agents.server as server  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402
from agents.demo import systems as demo_systems  # noqa: E402
from agents.demo import workshop  # noqa: E402

_SITE_CMMS = next(s for s in workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS
                  if s["system_type"] == "CMMS")
_PLAIN = next(s for s in demo_systems.DEMO_SYSTEMS if s["id"] == "plm")
_REGION = "us-east-1"
_OWN_ARN = (f"arn:aws:secretsmanager:{_REGION}:123456789012:secret:"
            f"adt/conn/{_SITE_CMMS['id']}-AbCdEf")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _row(entry: dict, origin=None, secret_arn: str = "") -> dict:
    return {"id": entry["id"], "name": entry["name"], "schema": entry["schema"],
            "plant": entry.get("plant", ""), "connection_type": "PostgreSQL",
            "system_type": entry.get("system_type", ""), "connection_origin": origin,
            "status": "onboarded", "removal_incomplete": None, "removal_full": None,
            "connection_config": "{}", "secret_arn": secret_arn}


@pytest.fixture
def restore(monkeypatch):
    """Drive ONE system of the SQL restore sweep; returns the start kwargs."""
    monkeypatch.setenv("AWS_REGION", _REGION)

    def _run(entry: dict, origin=None, resolved_extra: dict | None = None,
             secret_arn: str = "") -> list:
        started: list = []
        monkeypatch.setattr(server, "catalog", SystemCatalog([]))
        resolved = {"host": "db.example.internal", "port": "5432", "dbname": "plant",
                    "search_path": entry["schema"], "user": "reader",
                    "password": "pw-reader", "secret_arn": secret_arn,
                    **(resolved_extra or {})}
        monkeypatch.setattr(cr, "resolve_connection", lambda sid: dict(resolved))
        monkeypatch.setattr(op, "check_pg_login_params", lambda params, schema: (None, []))
        monkeypatch.setattr(server, "_restore_sql_table_details",
                            lambda schema: {"work_orders": [("status", "character varying")]})
        monkeypatch.setattr(server, "_restore_one",
                            lambda **kw: started.append(kw) or "mcp://restored")
        monkeypatch.setattr(server, "neptune_query",
                            lambda cypher, params=None: [_row(entry, origin, secret_arn)])
        server._restore_sql_systems()
        assert len(started) == 1, started
        return started[0]
    return _run


def test_a_restored_demo_cmms_agent_gets_its_query_notes(restore):
    kwargs = restore(_SITE_CMMS)
    prompt = kwargs["system_prompt"]
    notes = _flat(demo_systems.agent_query_notes(_SITE_CMMS["id"], _SITE_CMMS["schema"]))
    assert notes and notes in _flat(prompt)
    assert "work_orders.status codes" in prompt and "CAN (cancelled" in prompt
    assert "----- BEGIN QUERY HINTS -----" in prompt
    assert prompt.rstrip().endswith("----- END QUERY HINTS -----")


def test_a_user_registration_reusing_the_id_gets_no_demo_notes(restore):
    kwargs = restore(_SITE_CMMS, origin=cr.ORIGIN_USER)
    assert kwargs["system_prompt"] == ""


def test_a_bound_user_secret_reusing_the_id_gets_no_demo_notes(restore):
    # No node origin: the binding in the system's own secret marks it a user's.
    kwargs = restore(_SITE_CMMS, secret_arn=_OWN_ARN, resolved_extra={
        cr.CONNECTION_ORIGIN_KEY: cr.ORIGIN_USER, "system_id": _SITE_CMMS["id"]})
    assert kwargs["system_prompt"] == ""


def test_an_unreadable_own_secret_gets_no_demo_notes(restore):
    # The node points at the system's own secret and it could not be read:
    # whose registration it is cannot be told, so no manifest notes.
    kwargs = restore(_SITE_CMMS, secret_arn=_OWN_ARN,
                     resolved_extra={"_secret_unresolved": True})
    assert kwargs["system_prompt"] == ""


def test_a_system_without_notes_keeps_an_empty_prompt(restore):
    assert restore(_PLAIN)["system_prompt"] == ""


def test_the_notes_follow_the_restored_schema(restore):
    # The same id on another schema is not the manifest system.
    other = {**_SITE_CMMS, "schema": "another_schema"}
    assert restore(other)["system_prompt"] == ""
