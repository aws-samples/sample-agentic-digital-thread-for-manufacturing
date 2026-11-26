# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pinned invariant: the run-path tag (Evals-admin-tab Task 1).

Every eval report says which path produced it — "cli" (through CloudFront)
vs "server" (internal chat client) latencies are incomparable, so an
untagged report is unusable for comparison. Pins:

(a) eval_core.build_eval_report REQUIRES run_path — calling without it
    fails (TypeError), and an empty/non-str value is rejected (ValueError);
(b) a CLI-built report carries run_path == "cli" — both via the scripts
    build_eval_report wrapper default AND in the report file main() writes;
(c) the key survives a JSON round-trip in the persisted shape.

Offline throughout — canned SSE fixtures for the main() drive (the
test_eval_repeats.py mechanism); repo-root-script import mechanism as in
test_eval_answers_report.py.
"""
import io
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import eval_answers  # noqa: E402
from scripts import smoke_prompts as smoke  # noqa: E402

eval_core = eval_answers.eval_core


class _Namespace:
    run_id = "run-1"
    prefix = "eval-run-1"


_ROWS = [{
    "id": "g-a", "session_id": "s-1", "verdict": "pass", "failures": [],
    "systems_queried": 2, "neptune_queries": 1, "elapsed_seconds": 3.0,
    "answer_excerpt": "excerpt",
    "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
    "metrics": None,
}]


def _core_report(**overrides):
    kwargs = dict(base_url="u", namespace=_Namespace(), golden_set="g",
                  strict=False, rows=list(_ROWS))
    kwargs.update(overrides)
    return eval_core.build_eval_report(**kwargs)


# ── (a) run_path is REQUIRED on the core builder ────────────────────────────

def test_core_build_eval_report_fails_without_run_path():
    with pytest.raises(TypeError):
        _core_report()  # no run_path — required keyword-only, no default


@pytest.mark.parametrize("bad", ["", None, 7])
def test_core_build_eval_report_rejects_empty_or_non_str(bad):
    with pytest.raises(ValueError):
        _core_report(run_path=bad)


def test_core_report_stamps_run_path_top_level():
    report = _core_report(run_path="server")
    assert report["run_path"] == "server"


# ── (b) the CLI stamps run_path == "cli" ────────────────────────────────────

def test_cli_wrapper_defaults_run_path_cli():
    report = eval_answers.build_eval_report(
        base_url="u", namespace=_Namespace(), golden_set="g", strict=False,
        rows=list(_ROWS))
    assert report["run_path"] == "cli"


class _FakeOpener:
    """Scripted urllib opener: login ok; /api/ask pops one canned stream."""

    def __init__(self, streams):
        self.streams = list(streams)

    def open(self, request, timeout=None):
        if request.get_full_url().endswith("/api/auth/login"):
            return io.BytesIO(b'{"status": "ok"}')
        return self.streams.pop(0)


def _sse():
    summary = {"system_queries": 2, "neptune_queries": 1}
    parts = [
        "event: token\ndata: " + json.dumps({"text": "grounded answer"}) + "\n\n",
        "event: metadata\ndata: "
        + json.dumps({"intelligence_summary": summary}) + "\n\n",
        "event: done\ndata: " + json.dumps({"text": ""}) + "\n\n",
    ]
    return io.BytesIO("".join(parts).encode())


def test_main_written_report_carries_cli_run_path(monkeypatch, tmp_path):
    # End-to-end offline: the report FILE the CLI writes carries "cli".
    for var in ("ADT_BASE_URL", "ADT_USERNAME", "ADT_PASSWORD",
                "EVAL_GOLDEN_SET", "EVAL_STRICT", "EVAL_REPORT",
                "EVAL_VARIANT", "EVAL_STRICT_REGRESSION", "EVAL_REPEATS"):
        monkeypatch.delenv(var, raising=False)
    real_app = smoke.App

    def _factory(base_url):
        app = object.__new__(real_app)
        app.base = base_url.rstrip("/")
        app.opener = _FakeOpener([_sse()])
        return app

    monkeypatch.setattr(smoke, "App", _factory)
    goldens = tmp_path / "goldens.json"
    goldens.write_text(json.dumps({"version": 1, "questions": [
        {"id": "g-1", "text": "How many units shipped?",
         "expect": {"systems_min": 1}}]}))
    report_path = tmp_path / "report.json"
    code = eval_answers.main(
        ["--base-url", "https://example.invalid", "--password", "x",
         "--golden-set", str(goldens), "--report", str(report_path),
         "--no-warmup"])
    assert code == 0
    persisted = json.loads(report_path.read_text())
    assert persisted["run_path"] == "cli"


# ── (c) JSON round-trip of the persisted shape ──────────────────────────────

@pytest.mark.parametrize("run_path", ["cli", "server"])
def test_run_path_survives_json_round_trip(run_path):
    report = _core_report(run_path=run_path)
    round_tripped = json.loads(json.dumps(report))
    assert round_tripped["run_path"] == run_path
    # Round trip is lossless for the whole report shape, not just the key.
    assert round_tripped == report
