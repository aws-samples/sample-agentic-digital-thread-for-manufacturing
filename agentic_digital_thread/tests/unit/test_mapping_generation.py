# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every Field-MAPS_TO writer bumps the system's mapping generation, and a
steward-approved edge carries its approval as its own property.

The system agent reads a small per-system ``mapping_generation`` with each
delegated question and re-reads its concept mappings only when it moved
(``live_concept_mappings``), so a writer that forgets the bump leaves the
agent on the old mappings until the safety reload. And the agent tells a
user-confirmed mapping from an inferred one by ``r.confirmed``, not by the
method name: a steward-accepted field-concept mint is stamped ``minted``.

Pinned here: the repoint, the mint, the retirement, the steward remap and
every provision (also one that raises part-way) bump the generation; the
repoint, the mint and the remap stamp ``confirmed``.

Neutral fixtures (system-a, table_x). Fully offline.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
import agents.self_learning as sl


def _bump():
    import importlib
    return importlib.import_module("agents.live_concept_mappings").BUMP_GENERATION_CYPHER


class _Graph:
    def __init__(self, edges=()):
        self.edges = set(edges)
        self.writes: list = []

    def query(self, cypher, params=None):
        params = params or {}
        if "count(f)" in cypher:
            return [{"n": 1}]
        if "count(s)" in cypher:
            return [{"n": 1}]
        if "count(r)" in cypher:
            return [{"n": int((params.get("fid"), params.get("concept")) in self.edges)}]
        if "max(r.version)" in cypher:
            return [{"v": 1}]
        raise AssertionError(cypher)

    def write(self, cypher, params=None):
        self.writes.append((cypher, params))
        if "DELETE r" in cypher and params and "concept" in params:
            self.edges.discard((params.get("fid"), params.get("concept")))
        return True

    def bumped(self):
        return [p for c, p in self.writes if c == _bump()]

    def edge_merge(self):
        return next(c for c, _ in reversed(self.writes) if "MERGE (f)-[r:MAPS_TO]->(c)" in c)


@pytest.fixture
def graph(monkeypatch):
    g = _Graph()
    monkeypatch.setattr(sl, "_neptune_query", g.query)
    monkeypatch.setattr(sl, "_neptune_query_checked", g.query, raising=False)
    monkeypatch.setattr(sl, "_neptune_write", g.write)
    return g


_ITEM = {"to_system": "system-a", "concept": "site", "to_field": "table_x.cell",
         "confidence": 1.0}


def test_a_concept_repoint_bumps_the_generation_and_is_confirmed(graph):
    sl._repoint_maps_to(dict(_ITEM, accepted_by="steward-1"))
    assert graph.bumped() == [{"sid": "system-a"}]
    merge = graph.edge_merge()
    assert "r.confirmed = true" in merge and "r.approved_by = $approved_by" in merge
    assert any((p or {}).get("approved_by") == "steward-1" for _c, p in graph.writes)


def test_an_accepted_field_concept_mint_bumps_the_generation_and_is_confirmed(graph):
    sl._apply_field_concept_mint(dict(_ITEM, concept="ink_kind", to_field="table_x.ink"), 2)
    assert graph.bumped() == [{"sid": "system-a"}]
    # Before: method 'minted' only, so the agent read a steward-approved mint
    # as "suggested".
    assert "r.confirmed = true" in graph.edge_merge()


def test_a_retirement_bumps_the_generation(graph):
    graph.edges.add(("system-a.table_x.cell", "site"))
    sl._retire_maps_to(dict(_ITEM, proposed_type="concept_mapping_retire"))
    assert graph.bumped() == [{"sid": "system-a"}]


def test_a_steward_remap_bumps_each_systems_generation(monkeypatch):
    from agents.models import RemapField, RemapRequest
    bumped = []
    monkeypatch.setattr(onboard, "neptune_write_returning", lambda _c, params=None: [
        {"field_id": r["fid"], "concept_id": r["cid"]} for r in params["rows"]])
    monkeypatch.setattr(onboard, "_bump_and_read_generation",
                        lambda sid: bumped.append(sid) or (1, 1))
    fids = ["system-a.table_x.cell", "system-a.table_x.line", "system-b.table_y.cell"]
    assert onboard.onboard_remap(RemapRequest(remaps=[
        RemapField(field_id=fids[0], concept_id="site", role="identifier"),
        RemapField(field_id=fids[1], concept_id="line", role="identifier"),
        RemapField(field_id=fids[2], concept_id="site", role="identifier"),
    ])) == {"remapped": 3, "written": fids, "not_written": []}
    assert bumped == ["system-a", "system-b"]


#: The real provision bump seam, captured at import (the lane-wide conftest
#: replaces it with a successful bump for every other provision test).
_REAL_BUMP_AND_READ = getattr(onboard, "_bump_and_read_generation", None)


class _ProvisionGraph:
    """Every graph write a provision step could send, and the generation the
    bump wrote and the checked readback answers."""

    def __init__(self, *, written=1, read=1, refuse=False, write_raises=False,
                 read_raises=False):
        self.writes: list = []
        self.written, self.read = written, read
        self.refuse, self.write_raises, self.read_raises = refuse, write_raises, read_raises

    def write(self, cypher, params=None):
        self.writes.append((cypher, params))
        return True

    def write_returning(self, cypher, params=None):
        self.writes.append((cypher, params))
        if self.write_raises:
            raise RuntimeError("graph down")
        if self.refuse:
            return None
        return [] if self.written is None else [{"g": self.written}]

    def checked(self, cypher, params=None):
        if self.read_raises:
            raise RuntimeError("graph down")
        assert "mapping_generation" in cypher and params == {"sid": "system-a"}
        return [] if self.read is None else [{"g": self.read}]

    def bumps(self):
        return [p for c, p in self.writes if "mapping_generation" in c]


def _steps(monkeypatch, base, graph=None):
    from agents.models import ProvisionRequest
    graph = graph or _ProvisionGraph()
    monkeypatch.setattr(onboard, "_bump_and_read_generation", _REAL_BUMP_AND_READ,
                        raising=False)
    monkeypatch.setattr(onboard, "_do_provision_base", base)
    monkeypatch.setattr(onboard, "_provision_knowledge_post_pass", lambda *_a, **_k: None)
    monkeypatch.setattr("agents.asset_role_pass.run_after_provision", lambda: None)
    monkeypatch.setattr(onboard, "_sweep_retired_concept_links", lambda _sid: True)
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    monkeypatch.setattr(onboard, "neptune_write_returning", graph.write_returning)
    monkeypatch.setattr(onboard, "neptune_query_checked", graph.checked)
    body = ProvisionRequest(system_id="system-a", connection_type="Athena")
    return body, graph


def _provisioned(body, dry_run=False):
    return {"status": "provisioned"}


def _outcome(result: dict) -> dict:
    """The provision result without its run id (minted fresh every run)."""
    return {k: v for k, v in result.items() if k != "provision_run_id"}


def test_a_provision_bumps_the_generation_once_after_its_writes(monkeypatch):
    body, graph = _steps(monkeypatch, _provisioned)
    assert _outcome(onboard._do_provision_steps(body)) == {"status": "provisioned"}
    assert graph.bumps() == [{"sid": "system-a"}]
    assert "mapping_generation" in graph.writes[-1][0]


def test_a_dry_run_makes_no_graph_write(monkeypatch):
    calls = []
    body, graph = _steps(monkeypatch, lambda body, dry_run=False: calls.append(dry_run)
                         or {"status": "preview", "edges": []})
    assert _outcome(onboard._do_provision_steps(body, dry_run=True)) == {
        "status": "preview", "edges": []}
    assert calls == [True]
    # Before: the finally clause bumped mapping_generation on a dry run too.
    assert graph.writes == []


@pytest.mark.parametrize("failure", [
    dict(refuse=True),                    # the bump write was refused
    dict(write_raises=True),              # the bump write raised
    dict(written=None),                   # no System node to bump
    dict(written=5, read=4),              # the readback does not show the new generation
    dict(written=5, read=None),           # the readback answers no generation
    dict(read_raises=True),               # the readback raised
])
def test_a_provision_whose_bump_cannot_be_confirmed_is_not_provisioned(monkeypatch, failure):
    body, graph = _steps(monkeypatch, _provisioned, _ProvisionGraph(**failure))
    result = onboard._do_provision_steps(body)
    # Before: {"status": "provisioned"}, and the system agent kept the old
    # mappings until its 10-minute safety reload.
    assert result.get("status") != "provisioned"
    assert result["reason"] == "mapping_generation_unbumped"
    record = onboard._provision_error_record(result)
    assert record["retryable"] is True and record["status"] == "error"


def test_a_readback_ahead_of_the_bump_is_confirmed(monkeypatch):
    # Another writer bumped it again in between: still at least our generation.
    body, _graph = _steps(monkeypatch, _provisioned, _ProvisionGraph(written=5, read=7))
    assert _outcome(onboard._do_provision_steps(body)) == {"status": "provisioned"}


def test_a_provision_that_raises_part_way_still_bumps_and_keeps_its_raise(monkeypatch):
    def _boom(body, dry_run=False):
        raise RuntimeError("b2 failed after deleting the old edges")
    body, graph = _steps(monkeypatch, _boom, _ProvisionGraph(refuse=True))
    with pytest.raises(RuntimeError, match="b2 failed"):
        onboard._do_provision_steps(body)
    assert graph.bumps() == [{"sid": "system-a"}]


def test_a_provision_error_keeps_its_own_outcome_when_the_bump_fails(monkeypatch):
    error = {"error": "x", "reason": "retired_link_sweep_failed"}
    body, graph = _steps(monkeypatch, lambda body, dry_run=False: dict(error),
                         _ProvisionGraph(refuse=True))
    assert onboard._do_provision_steps(body) == error
    assert graph.bumps() == [{"sid": "system-a"}]
