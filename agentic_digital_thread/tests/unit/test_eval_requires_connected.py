# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``requires_connected_types`` golden gate (both eval lanes).

A golden that needs a system TYPE the deployment has not onboarded yet (a
fresh workshop account registers its CMMSs; onboarding them is a module) is
a precondition miss, so it reports ``skipped`` with a named reason instead of
failing. Unknown deployment state (``connected_types`` None, an unreadable
``/api/systems`` or catalog) RUNS the golden: the gate never hides a failure
it cannot justify.
"""
import json
import sys
import urllib.error
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
for p in (str(_ROOT), str(_ROOT / "agentic_digital_thread")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents import eval_core, eval_runs  # noqa: E402
from scripts import eval_answers  # noqa: E402

_EXPECT = {"systems_min": 1}
_GATED = {"id": "g-cmms", "text": "q", "expect": _EXPECT,
          "requires_connected_types": ["CMMS"]}


def test_skips_while_no_system_of_the_type_is_onboarded():
    reason = eval_core.should_skip(_GATED, environ={},
                                   connected_types={"ERP", "MES"})
    assert reason == "requires_connected_types CMMS not onboarded"


def test_runs_once_the_type_is_onboarded_case_insensitively():
    assert eval_core.should_skip(_GATED, environ={},
                                 connected_types={"erp", " cmms "}) is None


def test_unknown_state_runs_the_question():
    assert eval_core.should_skip(_GATED, environ={}, connected_types=None) is None
    assert eval_core.should_skip(_GATED, environ={}) is None


def test_every_listed_type_must_be_onboarded():
    q = dict(_GATED, requires_connected_types=["CMMS", "PLM"])
    assert eval_core.should_skip(q, environ={}, connected_types={"CMMS"}) == (
        "requires_connected_types PLM not onboarded")


def test_ungated_questions_ignore_the_connected_state():
    q = {"id": "g-plain", "text": "q", "expect": _EXPECT}
    assert eval_core.should_skip(q, environ={}, connected_types=set()) is None


@pytest.mark.parametrize("gate", ["CMMS", [], [""], [1], {"CMMS": True}])
def test_a_malformed_gate_raises_at_parse_and_at_skip(gate):
    q = dict(_GATED, requires_connected_types=gate)
    with pytest.raises(ValueError, match="requires_connected_types"):
        eval_core.parse_golden_set({"version": 1, "questions": [q]})
    with pytest.raises(ValueError, match="requires_connected_types"):
        eval_core.should_skip(q, environ={}, connected_types={"CMMS"})


def test_packaged_cmms_goldens_carry_the_gate():
    data = json.loads(Path(eval_core.default_golden_set_path()).read_text())
    gated = {q["id"] for q in data["questions"]
             if q.get("requires_connected_types") == ["CMMS"]}
    assert gated == {"g-critical-work-orders", "g-injection-via-data",
                     "g-workshop-live-m5-q1-maintenance", "g-workshop-live-m6-machine-at-risk"}
    eval_core.parse_golden_set(data)


class _App:
    def __init__(self, body=None, exc=None):
        self.body, self.exc, self.calls = body, exc, []

    def call(self, method, path, body=None, timeout=60):
        self.calls.append((method, path))
        if self.exc:
            raise self.exc
        return self.body


def test_cli_reads_connected_types_from_api_systems():
    app = _App({"systems": [{"type": "ERP", "status": "connected"},
                            {"type": "CMMS", "status": "registered"}]})
    assert eval_answers._connected_types(app) == {"ERP"}
    assert app.calls == [("GET", "/api/systems")]


def test_cli_unreadable_systems_means_unknown(capsys):
    app = _App(exc=urllib.error.URLError("down"))
    assert eval_answers._connected_types(app) is None
    assert "requires_connected_types gates run" in capsys.readouterr().out


def test_cli_run_skips_a_gated_golden_and_reads_systems_once(monkeypatch):
    from types import SimpleNamespace
    app = _App({"systems": [{"type": "ERP", "status": "connected"}]})

    def no_ask(*a, **k):
        raise AssertionError("a skipped golden must not be asked")
    monkeypatch.setattr(eval_answers, "_repeat_rows", no_ask)
    rows = eval_answers._collect_rows(
        app, SimpleNamespace(no_seed_fixtures=False), None, [_GATED, _GATED], [])
    assert [r["verdict"] for r in rows] == ["skipped", "skipped"]
    assert app.calls == [("GET", "/api/systems")]


def test_cli_run_without_gated_goldens_never_reads_systems(monkeypatch):
    from types import SimpleNamespace
    app = _App(exc=AssertionError("must not call /api/systems"))
    monkeypatch.setattr(eval_answers, "_repeat_rows",
                        lambda *a, **k: eval_core._skipped_row(a[3], "stub"))
    plain = {"id": "g-plain", "text": "q", "expect": _EXPECT}
    eval_answers._collect_rows(app, SimpleNamespace(no_seed_fixtures=False),
                               None, [plain], [])
    assert app.calls == []


def test_server_lane_reads_onboarded_types_from_the_catalog(monkeypatch):
    from agents import catalog as catalog_mod
    monkeypatch.setattr(catalog_mod.catalog, "get_all", lambda: [
        {"system_type": "ERP", "status": "onboarded"},
        {"system_type": "CMMS", "status": "registered"}])
    assert eval_runs._connected_types() == {"ERP"}


def test_server_lane_unreadable_catalog_means_unknown(monkeypatch):
    from agents import catalog as catalog_mod

    def boom():
        raise RuntimeError("graph down")
    monkeypatch.setattr(catalog_mod.catalog, "get_all", boom)
    assert eval_runs._connected_types() is None


def test_server_row_skips_a_gated_golden_without_asking(monkeypatch):
    monkeypatch.setattr(eval_runs, "_connected_types", lambda: {"ERP"})

    def no_ask(*a, **k):
        raise AssertionError("a skipped golden must not be asked")
    monkeypatch.setattr(eval_runs, "_ask_question", no_ask)
    row = eval_runs._question_row("rid", 1, _GATED, 1e18, False)
    assert row["verdict"] == "skipped"
    assert row["skip_reason"] == "requires_connected_types CMMS not onboarded"
