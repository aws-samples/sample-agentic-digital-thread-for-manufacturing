# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The plant CMMS says what its work-order status codes mean, to the agent
that reads them.

Rehearsal walk, 2026-10-22: asked which maintenance work orders were open on
a machine, the answer listed two cancelled orders (status ``CAN``) with the
open ones and said "4 open work orders total". The CMMS agent had listed them
under "already open" earlier in the chat. Nothing the agent reads said what
the codes mean: the seed's column carries only a CHECK list of codes, SQL
comments in a seed file never reach a database, and the registration's
column notes covered dates only.

The fix is the source describing itself, through the lever the app already
has for column meanings: the registration's ``agent_query_notes``, which
provision appends to the CMMS agent's prompt as a QUERY HINTS block. No
per-turn rule, nothing in the orchestrator. These tests tie the note to the
data: every code the seed's CHECK constraint allows is described. Offline: no AWS, no graph, no database.
"""
from __future__ import annotations

import re
from pathlib import Path

from agents import onboard
from agents.connection_resolver import CONNECTION_ORIGIN_KEY, ORIGIN_DEMO
from agents.demo import systems as demo_systems
from agents.demo import workshop


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SITE_CMMS = next(s for s in workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS
                  if s["system_type"] == "CMMS")
_SEED = _REPO_ROOT / "lambda-adt-seed" / f"011_{_SITE_CMMS['schema']}.sql"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _notes() -> str:
    return _flat(demo_systems.agent_query_notes(_SITE_CMMS["id"], _SITE_CMMS["schema"]))


def _seed_status_codes() -> list[str]:
    """The codes the seed's work_orders.status CHECK constraint allows."""
    text = _SEED.read_text(encoding="utf-8")
    body = re.search(r"CREATE TABLE IF NOT EXISTS work_orders \((.*?)\n\);", text, re.S)
    assert body, "work_orders table not found in the seed"
    check = re.search(r"^\s*status\s.*CHECK \(status IN \(([^)]*)\)\)", body.group(1), re.M)
    assert check, "work_orders.status CHECK list not found in the seed"
    return re.findall(r"'([A-Z]+)'", check.group(1))


def _named(code: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", text) is not None


def _status_clauses() -> tuple[str, str]:
    """``(open clause, not-open clause)`` of the note's status sentence."""
    notes = _notes()
    assert "work_orders.status" in notes and " not open" in notes, notes
    start = notes.index("work_orders.status")
    sentence = notes[start:notes.index(".", notes.index(" not open", start))]
    open_part, closed_part = sentence.split(";", 1)
    return open_part, closed_part


def test_every_status_the_seed_allows_is_described():
    codes = _seed_status_codes()
    assert codes, "the seed lists no status codes"
    notes = _notes()
    missing = [code for code in codes if not _named(code, notes)]
    assert not missing, f"status codes the CMMS note does not describe: {missing}"


def test_a_cancelled_order_is_described_as_not_open_work():
    _, closed_part = _status_clauses()
    assert re.search(r"CAN \(cancelled", closed_part), closed_part


def test_the_status_sentence_reaches_the_cmms_agent_whole():
    # The delivery cap shortens a long value; the status sentence must survive it.
    entry = {**_SITE_CMMS, CONNECTION_ORIGIN_KEY: ORIGIN_DEMO}
    block = _flat(onboard._agent_query_notes_block(entry, _SITE_CMMS["id"]))
    open_part, closed_part = _status_clauses()
    assert f"{open_part};{closed_part}" in block
    assert _notes() in block
    assert "[truncated]" not in block
