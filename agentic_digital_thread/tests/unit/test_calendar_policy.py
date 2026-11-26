# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The calendar policy: a structured graph record, validated, steward-set.

Pins:

* validation: the two rules, the day start, the site list (IANA zones, unique
  names, a plain name charset, an optional per-site day start) and the rule
  that a site working day needs at least one site;
* persistence: validate, MERGE, exact readback, audit stamps; a stored row
  that no longer validates reads as no policy (today's behaviour);
* the per-process snapshot: one read per TTL window, a failed read keeps the
  previous snapshot and is retried;
* the admin route: steward-gated like its neighbours, offloaded, a 400 that
  names every problem, the write and the clear.

Offline: graph reads and writes use injected fakes.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents import calendar_policy as cp
from agents import calendar_policy_routes as routes

GOOD = {
    "bare_date": "utc_day",
    "relative_days": "site_day",
    "site_day_start": "06:30",
    "sites": [
        {"site": "site-a", "timezone": "America/New_York"},
        {"site": "site-b", "timezone": "Europe/London", "day_start": "07:00"},
    ],
}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_a_valid_body_parses():
    policy = cp.parse_policy(GOOD)
    assert policy.bare_date == cp.UTC_DAY and policy.relative_days == cp.SITE_DAY
    assert policy.site_day_start == "06:30"
    assert [(s.site, s.timezone, s.day_start) for s in policy.sites] == [
        ("site-a", "America/New_York", ""), ("site-b", "Europe/London", "07:00")]


def test_defaults_are_the_utc_day_and_midnight():
    policy = cp.parse_policy({"bare_date": "utc_day", "relative_days": "utc_day"})
    assert (policy.site_day_start, policy.sites) == ("00:00", ())


@pytest.mark.parametrize("change, needle", [
    ({"bare_date": "local_day"}, "bare_date"),
    ({"relative_days": ""}, "relative_days"),
    ({"site_day_start": "6:30"}, "site_day_start"),
    ({"site_day_start": "24:00"}, "site_day_start"),
    ({"sites": [{"site": "site-a", "timezone": "Mars/Olympus"}]}, "time zone"),
    ({"sites": [{"site": "site-a", "timezone": "../etc/passwd"}]}, "time zone"),
    ({"sites": [{"site": "", "timezone": "UTC"}]}, "site name"),
    ({"sites": [{"site": "a\nb", "timezone": "UTC"}]}, "site name"),
    ({"sites": [{"site": "x" * 65, "timezone": "UTC"}]}, "site name"),
    ({"sites": [{"site": "site-a", "timezone": "UTC"},
                {"site": "SITE-A", "timezone": "UTC"}]}, "more than once"),
    ({"sites": [{"site": "site-a", "timezone": "UTC", "day_start": "7"}]}, "day start"),
    ({"sites": []}, "at least one site"),
    ({"sites": [{"site": f"s{i}", "timezone": "UTC"} for i in range(cp.MAX_SITES + 1)]},
     "at most"),
])
def test_invalid_bodies_name_the_problem(change, needle):
    with pytest.raises(cp.PolicyError) as err:
        cp.parse_policy({**GOOD, **change})
    assert any(needle in message for message in err.value.errors), err.value.errors


def test_every_problem_is_reported_at_once():
    with pytest.raises(cp.PolicyError) as err:
        cp.parse_policy({"bare_date": "x", "relative_days": "y", "site_day_start": "z"})
    assert len(err.value.errors) == 3


def test_a_utc_only_policy_needs_no_site():
    assert cp.parse_policy({"bare_date": "utc_day", "relative_days": "utc_day",
                            "sites": []}).sites == ()


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
class _Graph:
    """One CalendarPolicy node, written and read back the way Cypher would."""

    def __init__(self, stored=None, *, drop_write=False):
        self.node = stored
        self.writes = []
        self.drop_write = drop_write

    def write(self, cypher, params=None):
        self.writes.append((cypher, params))
        if "DETACH DELETE" in cypher:
            self.node = None
        elif not self.drop_write:
            self.node = {k: v for k, v in (params or {}).items() if k != "id"}
        return True

    def query(self, cypher, params=None):
        if self.node is None:
            return []
        return [{"bare_date": self.node.get("bare_date"),
                 "relative_days": self.node.get("relative_days"),
                 "site_day_start": self.node.get("site_day_start"),
                 "sites_json": self.node.get("sites_json"),
                 "updated_by": self.node.get("actor"),
                 "updated_at": self.node.get("at")}]


def test_write_merges_then_reads_back_with_audit_stamps():
    graph = _Graph()
    result = cp.write_policy(cp.parse_policy(GOOD), "steward@example.com",
                             write=graph.write, query=graph.query)
    assert result["status"] == "ok" and result["readback"] is True
    cypher, params = graph.writes[0]
    assert cypher.startswith("MERGE (p:CalendarPolicy {id: $id})")
    assert params["id"] == cp.POLICY_NODE_ID and params["actor"] == "steward@example.com"
    stored = cp.read_policy(query=graph.query)
    assert stored == cp.parse_policy(GOOD).with_audit("steward@example.com", params["at"])
    assert result["policy"]["updated_by"] == "steward@example.com"


def test_a_write_that_does_not_read_back_reports_failure():
    graph = _Graph(drop_write=True)
    result = cp.write_policy(cp.parse_policy(GOOD), "s", write=graph.write, query=graph.query)
    assert result["status"] == "failed" and result["readback"] is False


def test_clear_removes_the_node():
    graph = _Graph()
    cp.write_policy(cp.parse_policy(GOOD), "s", write=graph.write, query=graph.query)
    assert cp.clear_policy("s", write=graph.write, query=graph.query)["status"] == "cleared"
    assert cp.read_policy(query=graph.query) is None


def test_a_stored_row_that_no_longer_validates_reads_as_no_policy():
    graph = _Graph({"bare_date": "utc_day", "relative_days": "site_day",
                    "site_day_start": "06:30", "sites_json": "not json"})
    assert cp.read_policy(query=graph.query) is None
    graph.node["sites_json"] = json.dumps([{"site": "site-a", "timezone": "Nowhere/City"}])
    assert cp.read_policy(query=graph.query) is None


def test_no_node_reads_as_no_policy():
    assert cp.read_policy(query=_Graph().query) is None


# --------------------------------------------------------------------------- #
# The per-process snapshot
# --------------------------------------------------------------------------- #
def test_the_snapshot_reads_once_per_ttl_window(monkeypatch):
    graph = _Graph()
    cp.write_policy(cp.parse_policy(GOOD), "s", write=graph.write, query=graph.query)
    calls = []

    def query(cypher, params=None):
        calls.append(cypher)
        return graph.query(cypher, params)

    clock = [100.0]
    monkeypatch.setattr(cp, "_monotonic", lambda: clock[0])
    cp.reset_snapshot()
    assert cp.load_policy(query=query) is not None
    assert cp.load_policy(query=query) is not None
    assert len(calls) == 1
    clock[0] += cp.SNAPSHOT_TTL_SECONDS + 1
    cp.load_policy(query=query)
    assert len(calls) == 2


def test_a_failed_read_keeps_the_previous_snapshot(monkeypatch):
    graph = _Graph()
    cp.write_policy(cp.parse_policy(GOOD), "s", write=graph.write, query=graph.query)
    clock = [100.0]
    monkeypatch.setattr(cp, "_monotonic", lambda: clock[0])
    cp.reset_snapshot()
    first = cp.load_policy(query=graph.query)
    clock[0] += cp.SNAPSHOT_TTL_SECONDS + 1

    def broken(cypher, params=None):
        raise RuntimeError("graph down")

    assert cp.load_policy(query=broken) == first
    cp.reset_snapshot()
    assert cp.load_policy(query=broken) is None


# --------------------------------------------------------------------------- #
# The admin route
# --------------------------------------------------------------------------- #
@pytest.fixture()
def graph(monkeypatch):
    graph = _Graph()
    monkeypatch.setattr(cp, "_graph_write", graph.write)
    monkeypatch.setattr(cp, "_graph_read", graph.query)
    return graph


def _client(is_admin=True):
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            request.state.user = type("U", (), {"email": "steward@example.com"})()
            return await call_next(request)
    app.include_router(routes.router)
    return TestClient(app)


def test_the_route_reads_no_policy_then_the_written_one(graph):
    client = _client()
    assert client.get("/api/admin/calendar-policy").json()["policy"] is None
    response = client.post("/api/admin/calendar-policy", json=GOOD)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok" and body["policy"]["updated_by"] == "steward@example.com"
    read = client.get("/api/admin/calendar-policy").json()
    assert read["policy"]["sites"][1] == {"site": "site-b", "timezone": "Europe/London",
                                          "day_start": "07:00"}
    assert set(read["rules"]) == {cp.UTC_DAY, cp.SITE_DAY}


def test_the_route_answers_400_with_every_problem(graph):
    response = _client().post("/api/admin/calendar-policy",
                              json={**GOOD, "bare_date": "x", "site_day_start": "y"})
    assert response.status_code == 400
    assert len(response.json()["errors"]) == 2
    assert graph.writes == []


def test_the_route_refuses_unknown_fields(graph):
    response = _client().post("/api/admin/calendar-policy", json={**GOOD, "extra": 1})
    assert response.status_code == 422
    assert graph.writes == []


def test_an_unconfirmed_write_is_a_503(monkeypatch):
    graph = _Graph(drop_write=True)
    monkeypatch.setattr(cp, "_graph_write", graph.write)
    monkeypatch.setattr(cp, "_graph_read", graph.query)
    response = _client().post("/api/admin/calendar-policy", json=GOOD)
    assert response.status_code == 503 and response.json()["status"] == "failed"


def test_the_clear_route_restores_no_policy(graph):
    client = _client()
    client.post("/api/admin/calendar-policy", json=GOOD)
    assert client.post("/api/admin/calendar-policy/clear").json()["status"] == "cleared"
    assert client.get("/api/admin/calendar-policy").json()["policy"] is None


@pytest.mark.parametrize("is_admin", [False, None], ids=["non-admin", "no-verdict"])
@pytest.mark.parametrize("method, path", [
    ("get", "/api/admin/calendar-policy"),
    ("post", "/api/admin/calendar-policy"),
    ("post", "/api/admin/calendar-policy/clear"),
])
def test_non_stewards_get_403_and_nothing_runs(graph, is_admin, method, path):
    client = _client(is_admin=is_admin)
    response = getattr(client, method)(path, **({"json": GOOD} if method == "post" else {}))
    assert response.status_code == 403
    assert graph.writes == []
