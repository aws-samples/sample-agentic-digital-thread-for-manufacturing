# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The onboarding harness's step timings (seed timing instrumentation).

``discover_and_map`` reports wall seconds per step plus table/mapping counts;
``resolve_detailed`` reports the client wall time and passes through ONLY the
resolve job's ``timings`` (never its evidence or sampled examples), and
``resolve`` keeps returning the bare rule list. Deterministic + offline.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "onboarding_smoketest.py"
_spec = importlib.util.spec_from_file_location("onboarding_smoketest_timing", _HARNESS_PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

_SERVER_TIMINGS = {"total_s": 41.2, "stages_s": {"concept_pass": 30.0, "concept.llm": 22.5},
                   "counts": {"rules": 2}}


class _FakeApp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, method, path, body=None, timeout=120):
        self.calls.append((method, path))
        return self.responses[(method, path.split("/status/")[0])]

    def poll(self, path, label, max_s):
        return smoke.App.poll(self, path, label, max_s)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(smoke.time, "sleep", lambda _s: None)


def test_discover_and_map_reports_step_times_and_counts():
    app = _FakeApp({
        ("POST", "/api/onboard/discover"): {"schema_name": "s", "tables": [
            {"name": "t", "columns": [{"name": "c", "data_type": "text"}]}]},
        ("POST", "/api/onboard/map"): {"mappings": [{"table": "t", "field": "t.c"}]},
    })
    dm = smoke.discover_and_map(app, "sys-a", connection_string="x")
    assert dm["schema"] == "s" and len(dm["mappings"]) == 1
    t = dm["timings"]
    assert set(t) == {"discover_s", "map_s", "tables", "mappings"}
    assert t["tables"] == 1 and t["mappings"] == 1
    assert t["discover_s"] >= 0 and t["map_s"] >= 0


def test_resolve_detailed_passes_through_only_the_server_timings():
    rule = {"concept": "work-order", "source_field": "t.c"}
    app = _FakeApp({
        ("POST", "/api/onboard/resolve"): {"job_id": "j1", "status": "running"},
        ("GET", "/api/onboard/resolve"): {
            "status": "done", "rules": [rule], "timings": _SERVER_TIMINGS,
            "evidence": [{"examples": ["SAMPLED-VALUE-1"]}]},
    })
    out = smoke.resolve_detailed(app, "sys-a", [])
    assert out["rules"] == [rule]
    assert out["timings"]["server"] == _SERVER_TIMINGS
    assert out["timings"]["resolve_s"] >= 0
    assert "SAMPLED-VALUE-1" not in json.dumps(out)


def test_resolve_detailed_without_server_timings_reports_wall_time_only():
    app = _FakeApp({
        ("POST", "/api/onboard/resolve"): {"job_id": "j1", "status": "running"},
        ("GET", "/api/onboard/resolve"): {"status": "done", "rules": []},
    })
    out = smoke.resolve_detailed(app, "sys-a", [])
    assert out == {"rules": [], "timings": {"resolve_s": out["timings"]["resolve_s"]}}


def test_resolve_still_returns_the_bare_rule_list():
    app = _FakeApp({
        ("POST", "/api/onboard/resolve"): {"job_id": "j1", "status": "running"},
        ("GET", "/api/onboard/resolve"): {"status": "done", "rules": [{"r": 1}],
                                          "timings": _SERVER_TIMINGS},
    })
    assert smoke.resolve(app, "sys-a", []) == [{"r": 1}]
