# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The provision result's written-artifact rows must be keyed by the bare
property names the wizard's "Connections Discovered" panel reads.

Regression: ``_dp_written_artifacts`` returned ``RETURN jp.from_system, ...``
without aliases, so every row came back keyed ``"jp.from_system"`` and the
panel rendered ``. -> . unknown`` for each JoinPath (live, workshop event
2026-09-22). The frontend contract lives in
``frontend/src/api/types/onboard.ts`` (``JoinPath`` / ``ValueMapping``); this
test pins the two sides to each other so neither can drift silently.

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboard_written_artifacts_shape.py
  * standalone:  python agentic_digital_thread/tests/unit/test_onboard_written_artifacts_shape.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ADT = HERE.parents[2]
sys.path.insert(0, str(ADT))

import agents.onboard as onboard  # noqa: E402

ONBOARD_TS = ADT / "frontend" / "src" / "api" / "types" / "onboard.ts"


def _ts_interface_fields(name: str) -> set[str]:
    src = ONBOARD_TS.read_text(encoding="utf-8")
    m = re.search(rf"export interface {name} \{{(.*?)\n\}}", src, re.S)
    assert m, f"interface {name} not found in {ONBOARD_TS}"
    return set(re.findall(r"^\s*(\w+)\??:", m.group(1), re.M))


def _captured_queries(monkeypatch):
    seen: list[str] = []

    def fake_query(cypher, params=None):
        seen.append(cypher)
        return []

    monkeypatch.setattr(onboard, "neptune_query", fake_query)
    onboard._dp_written_artifacts("cmms-ind")
    assert len(seen) == 2, seen
    return seen


def _returned_aliases(cypher: str) -> list[str]:
    ret = cypher.split("RETURN", 1)[1]
    return re.findall(r"\bAS\s+(\w+)", ret)


def test_every_return_item_is_aliased_to_its_bare_name(monkeypatch):
    for cypher in _captured_queries(monkeypatch):
        ret = cypher.split("RETURN", 1)[1]
        items = [s.strip() for s in ret.split(",")]
        for item in items:
            m = re.fullmatch(r"(\w+)\.(\w+)\s+AS\s+(\w+)", item)
            assert m, f"un-aliased RETURN item {item!r} in {cypher!r}"
            assert m.group(2) == m.group(3), item


def test_join_path_rows_cover_the_frontend_join_path_contract(monkeypatch):
    jp_query, _ = _captured_queries(monkeypatch)
    aliases = set(_returned_aliases(jp_query))
    missing = _ts_interface_fields("JoinPath") - aliases
    assert not missing, f"frontend JoinPath fields not returned by the server: {sorted(missing)}"


def test_value_mapping_rows_cover_the_frontend_value_mapping_contract(monkeypatch):
    _, vm_query = _captured_queries(monkeypatch)
    aliases = set(_returned_aliases(vm_query))
    missing = _ts_interface_fields("ValueMapping") - aliases
    assert not missing, f"frontend ValueMapping fields not returned by the server: {sorted(missing)}"


if __name__ == "__main__":  # pragma: no cover
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
