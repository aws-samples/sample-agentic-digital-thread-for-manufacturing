# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``POST /api/onboard/remap`` reports a remap only when it landed.

Found in review: the route ignored every graph write's result, counted each
row as remapped, and bumped the systems' mapping generation best-effort, so
it could answer ``{"remapped": N}`` while no edge landed, or while the
system agent kept serving the old concept mappings until its periodic
reload.

Pinned here (fail closed, like a provision):

* the MAPS_TO edges are ONE returning statement per batch and a row counts
  only when that statement's rows name it; a refused or raising statement
  fails every row, and a row whose field or concept is not in the graph is
  never counted;
* the IdentityRule repoint runs only for rows whose edge landed; a refused
  or raising one, or a readback that does not show the rule on the new
  concept, fails the row;
* every affected system's generation goes through the checked bump
  (``onboard._bump_and_read_generation``), attempted even when a row failed;
* anything unconfirmed answers a retryable 503 (``remap_write_failed`` or
  ``mapping_generation_unbumped``) naming the rows that landed and the ones
  that did not; a field or concept the graph does not hold is a 422
  (``remap_target_missing``); the all-good path returns the count and the
  written field ids.

Neutral fixtures (system-a, table_x). Fully offline.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import agents.onboard as onboard
from agents.models import RemapField, RemapRequest

_A = RemapField(field_id="system-a.table_x.cell", concept_id="site", role="identifier")
_A2 = RemapField(field_id="system-a.table_x.line", concept_id="line", role="identifier",
                 old_concept="station")
_B = RemapField(field_id="system-b.table_y.cell", concept_id="site", role="identifier")


class _Graph:
    """The remap's graph writes and the generation bump, with failures a test
    can switch on. A statement naming a field id in ``refuse`` / ``raise_on``
    (a new concept in ``rule_refuse`` / ``rule_raise``) is refused / raises
    as a whole, as one graph statement does; a field in ``missing`` is simply
    not matched; a field in ``rule_disagree`` reads back with its rule's edge
    still on the old concept."""

    def __init__(self, *, refuse=(), raise_on=(), missing=(), rule_refuse=(),
                 rule_raise=(), rule_disagree=(), bump=None):
        self.refuse, self.raise_on, self.missing = set(refuse), set(raise_on), set(missing)
        self.rule_refuse, self.rule_raise = set(rule_refuse), set(rule_raise)
        self.rule_disagree = set(rule_disagree)
        self.bump = bump or {}
        self.edges: list = []
        self.edge_statements: list = []
        self.rules: list = []
        self.bumped: list = []

    def _edges(self, cypher, rows):
        fids = {r["fid"] for r in rows}
        if fids & self.raise_on:
            raise RuntimeError("graph down")
        if fids & self.refuse:
            return None
        self.edge_statements.append(cypher)
        out = []
        for r in rows:
            if r["fid"] in self.missing:
                continue
            self.edges.append((r["fid"], r["cid"], cypher))
            sid, table, column = r["fid"].split(".")
            out.append({"field_id": r["fid"], "concept_id": r["cid"], "system_id": sid,
                        "field_name": column, "schema_id": f"{sid}.{table}"})
        return out

    def _rules(self, rows):
        news = {r["new"] for r in rows}
        if news & self.rule_raise:
            raise RuntimeError("graph down")
        if news & self.rule_refuse:
            return None
        self.rules.extend(rows)
        return [{"field_id": r["fid"], "rule_id": f"rule-{r['fname']}", "concept": r["new"],
                 "linked": r["old"] if r["fid"] in self.rule_disagree else r["new"]}
                for r in rows]

    def write_returning(self, cypher, params=None):
        rows = (params or {})["rows"]
        if "MAPS_TO" in cypher:
            return self._edges(cypher, rows)
        if "IdentityRule" in cypher:
            return self._rules(rows)
        raise AssertionError(cypher)

    def bump_and_read(self, system_id):
        self.bumped.append(system_id)
        outcome = self.bump.get(system_id, (3, 3))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def graph(monkeypatch):
    g = _Graph()

    def _install(**kw):
        g.__init__(**kw)
        return g

    monkeypatch.setattr(onboard, "neptune_write_returning", g.write_returning)
    # Any write that bypasses the checked seam is refused, as a graph outage
    # would refuse it: a remap counted through it is a false success.
    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: False)
    monkeypatch.setattr(onboard, "_bump_and_read_generation", g.bump_and_read)
    g.install = _install
    return g


def _remap(*rows):
    return onboard.onboard_remap(RemapRequest(remaps=list(rows)))


def _ok(*rows, rules=None):
    """The all-good answer for ``rows`` (``rules``: rules repointed, when
    any row asked for a repoint)."""
    out = {"remapped": len(rows), "written": [r.field_id for r in rows], "not_written": []}
    return out if rules is None else {**out, "identity_rules_updated": rules}


# --------------------------------------------------------------------------- #
# 1. All good: the count, every edge, every system bumped
# --------------------------------------------------------------------------- #
def test_the_all_good_path_returns_the_count(graph):
    assert _remap(_A, _A2, _B) == _ok(_A, _A2, _B, rules=1)
    assert [fid for fid, _c, _q in graph.edges] == [_A.field_id, _A2.field_id, _B.field_id]
    assert len(graph.edge_statements) == 1
    assert graph.rules == [{"fid": _A2.field_id, "sys": "system-a", "fname": "line",
                            "tbl": "table_x", "old": "station", "new": "line"}]
    assert graph.bumped == ["system-a", "system-b"]
    cypher = graph.edges[0][2]
    assert "rel.method = 'user_confirmed', rel.confirmed = true" in cypher
    assert "RETURN f.id AS field_id, c.id AS concept_id" in cypher


# --------------------------------------------------------------------------- #
# 2. A failed edge write is never counted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("failure", ["refuse", "raise_on"])
def test_a_refused_or_raising_edge_write_is_a_retryable_error(graph, failure):
    graph.install(**{failure: {_A.field_id}})
    result = _remap(_A, _B)
    # Before: {"remapped": 2} with no edge written. One statement carries
    # the batch, so a refused or raising one lands no row.
    assert result["reason"] == "remap_write_failed"
    assert result["retryable"] is True and result["http_status"] == 503
    assert result["remapped"] == 0
    assert result["landed"] == [] and result["written"] == []
    assert result["not_written"] == [_A.field_id, _B.field_id]
    why = "refused" if failure == "refuse" else "error"
    assert result["failed"] == [
        {"field_id": _A.field_id, "concept_id": "site", "why": why, "mapping_landed": False},
        {"field_id": _B.field_id, "concept_id": "site", "why": why, "mapping_landed": False}]
    assert result["partially_landed"] == []
    # The bump is still attempted for every affected system.
    assert graph.bumped == ["system-a", "system-b"]
    assert "graph down" not in str(result)


def test_an_edge_whose_field_or_concept_is_missing_is_not_counted(graph):
    graph.install(missing={_A.field_id})
    result = _remap(_A, _B)
    assert result["reason"] == "remap_target_missing"
    assert result["http_status"] == 422 and result["retryable"] is False
    assert result["remapped"] == 1 and result["landed"] == [_B.field_id]
    assert result["written"] == [_B.field_id] and result["not_written"] == [_A.field_id]
    assert [f["why"] for f in result["failed"]] == ["not_in_graph"]


def test_a_failed_edge_skips_its_identity_rule_update(graph):
    graph.install(refuse={_A2.field_id})
    _remap(_A2)
    assert graph.rules == []


@pytest.mark.parametrize("failure", ["rule_refuse", "rule_raise"])
def test_a_failed_identity_rule_update_fails_the_row(graph, failure):
    graph.install(**{failure: {"line"}})
    result = _remap(_A2)
    assert result["reason"] == "remap_write_failed"
    assert result["remapped"] == 0
    assert result["failed"][0]["why"] in ("identity_rule_refused", "identity_rule_error")
    assert result["identity_rules_updated"] == 0


def test_a_rule_whose_readback_disagrees_fails_its_row(graph):
    """The repoint ran but the rule's edge still reads back on the old
    concept: resolution would stay on it, so the row did not fully land."""
    graph.install(rule_disagree={_A2.field_id})
    result = _remap(_A, _A2)
    assert result["reason"] == "remap_write_failed" and result["http_status"] == 503
    assert result["landed"] == [_A.field_id]
    assert result["partially_landed"] == [_A2.field_id]
    assert result["failed"] == [{"field_id": _A2.field_id, "concept_id": "line",
                                 "why": "identity_rule_unconfirmed", "mapping_landed": True,
                                 "identity_rule_updated": False}]
    assert result["identity_rules_updated"] == 0
    assert result["written"] == [_A.field_id, _A2.field_id]


@pytest.mark.parametrize("failure", ["rule_refuse", "rule_raise"])
def test_an_edge_that_landed_before_its_rule_failed_is_reported_as_landed(graph, failure):
    """The edge is written first and is already live (the confirmed bump makes
    the agent read it), so the row must not be reported as not applied."""
    graph.install(**{failure: {"line"}})
    result = _remap(_A2)
    row = result["failed"][0]
    assert row["mapping_landed"] is True and row["identity_rule_updated"] is False
    assert result["partially_landed"] == [_A2.field_id]
    assert "not applied" not in result["error"] or "identity rule" in result["error"]
    assert result["retryable"] is True and result["http_status"] == 503


def test_a_row_whose_edge_did_not_land_is_not_partially_landed(graph):
    graph.install(refuse={_A2.field_id})
    result = _remap(_A2)
    assert result["failed"][0]["mapping_landed"] is False
    assert result["partially_landed"] == []


# --------------------------------------------------------------------------- #
# 3. An unconfirmed generation is a retryable error
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("outcome", [
    RuntimeError("generation bump refused"),   # refused write
    RuntimeError("graph down"),                # raising write or readback
    (None, None),                              # no System node to bump
    (5, 4),                                    # readback lower than written
    (5, None),                                 # readback with no value
])
def test_an_unconfirmed_generation_is_a_retryable_error(graph, outcome):
    graph.install(bump={"system-b": outcome})
    result = _remap(_A, _B)
    # Before: {"remapped": 2}, and system-b's agent kept its old mappings.
    assert result["reason"] == "mapping_generation_unbumped"
    assert result["retryable"] is True and result["http_status"] == 503
    assert result["remapped"] == 2
    assert result["landed"] == [_A.field_id, _B.field_id]
    assert result["systems_unbumped"] == ["system-b"]
    assert "graph down" not in str(result)


def test_a_readback_ahead_of_the_bump_is_confirmed(graph):
    graph.install(bump={"system-a": (5, 7)})
    assert _remap(_A) == _ok(_A)


# --------------------------------------------------------------------------- #
# 4. The route answers the HTTP status
# --------------------------------------------------------------------------- #
def _client():
    app = FastAPI()

    @app.middleware("http")
    async def roles(request: Request, call_next):
        request.state.is_admin = True
        request.state.is_integrator = True
        return await call_next(request)

    app.include_router(onboard.router)
    return TestClient(app)


def _post(rows):
    return _client().post("/api/onboard/remap", json={"remaps": [r.model_dump() for r in rows]})


def test_the_route_answers_503_with_the_landed_rows(graph):
    graph.install(rule_refuse={"line"})
    response = _post([_A, _A2])
    assert response.status_code == 503
    body = response.json()
    assert body["reason"] == "remap_write_failed" and body["retryable"] is True
    assert body["landed"] == [_A.field_id]
    assert body["partially_landed"] == [_A2.field_id]
    assert "http_status" not in body
    assert body["error"]


def test_the_route_answers_422_for_a_missing_target(graph):
    graph.install(missing={_A.field_id})
    assert _post([_A]).status_code == 422


def test_the_route_answers_200_with_the_count(graph):
    response = _post([_A])
    assert response.status_code == 200 and response.json() == _ok(_A)
