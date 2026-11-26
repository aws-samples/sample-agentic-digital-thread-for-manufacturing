# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Route ``self_learning``'s row-returning write seam through a test's bool
``_neptune_write`` spy.

The steward accept writes a ValueMapping (its two System edges and its Concept
link), a JoinPath or a Correlation (its two System edges) in ONE statement
through ``self_learning._neptune_write_returning``, which
returns the rows the statement returned (``None`` when the graph refused it).
A test that stubs only ``_neptune_write`` would otherwise send that statement
to the real graph. ``route_returning_writes`` records it through the same spy:
a ``False`` answer is a refused write, anything else returns the node id,
the readback a successful write gives. ``route_equipment_checked_reads`` does
the same for the equipment post-pass's checked reads (Asset count, anchor
readback, duplicate readback).
"""
from __future__ import annotations

import agents.self_learning as self_learning


def route_returning_writes(monkeypatch, write) -> None:
    monkeypatch.setattr(
        self_learning, "_neptune_write_returning",
        lambda cypher, params=None: None if write(cypher, params) is False
        else [{"id": (params or {}).get("vmid") or (params or {}).get("sid")}], raising=False)


def route_equipment_checked_reads(monkeypatch) -> None:
    """Answer the equipment post-pass's checked reads as a graph where every
    element id names exactly one Asset, every anchor reads back as that Asset
    and no generated duplicate is left after a retirement — for tests that
    stub only the write seams. Any other checked read answers no rows."""
    import agents.onboard as onboard

    answers = {getattr(onboard, name, name): rows for name, rows in (
        ("_EQUIPMENT_ASSET_COUNT", [{"n": 1}]),
        ("_EQUIPMENT_VM_ANCHORS", [{"anchors": 1, "correct": 1}]),
        ("_EQUIPMENT_VM_DUPLICATES_LEFT", [{"n": 0}]),
    )}
    monkeypatch.setattr(onboard, "neptune_query_checked",
                        lambda cypher, params=None: [dict(r) for r in answers.get(cypher, [])])
