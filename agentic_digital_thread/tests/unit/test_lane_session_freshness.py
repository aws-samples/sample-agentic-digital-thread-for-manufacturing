# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lane session freshness: App.login + ensure_fresh_session (both lanes).

The app forwards the signed-in user's access token as the AgentCore gateway
bearer (ADR-0049); that token expires ~60 min after login, and a lane run
crossing the boundary hit a mid-turn gateway 403 burst (2026-09-24) that ate
10 golden runs. Both lanes now re-login via ONE shared path
(smoke_prompts.App.login) whenever the recorded session is older than
SESSION_MAX_AGE_SECONDS, before EVERY ask. Offline: App.call is stubbed;
no network, no sleeps (staleness is injected by rewinding _login_at).
"""

import sys
import time
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import eval_answers  # noqa: E402
from scripts import smoke_prompts as smoke  # noqa: E402


def _stub_login_calls(app, status="ok"):
    """Stub App.call to record login POSTs; returns the recorder list."""
    calls = []

    def fake_call(method, path, body=None, timeout=60):
        calls.append(path)
        return {"status": status}

    app.call = fake_call
    return calls


def _age_session(app):
    app._login_at = time.monotonic() - (smoke.SESSION_MAX_AGE_SECONDS + 1)


def test_session_max_age_is_45_minutes():
    assert smoke.SESSION_MAX_AGE_SECONDS == 45 * 60


def test_login_records_credentials_and_time():
    app = smoke.App("https://example.invalid")
    calls = _stub_login_calls(app)
    result = app.login("user", "pw")
    assert result == {"status": "ok"}
    assert calls == ["/api/auth/login"]
    assert app._login_username == "user" and app._login_password == "pw"  # nosec B105 - a fake test password
    assert app._login_at is not None


def test_failed_login_records_nothing():
    app = smoke.App("https://example.invalid")
    _stub_login_calls(app, status="error")
    app.login("user", "pw")
    assert app._login_at is None


def test_stale_session_relogs_in_exactly_once_across_n_asks():
    """An old login timestamp triggers ONE re-login; the refreshed timestamp
    keeps the following asks within the window (no per-ask login storm)."""
    app = smoke.App("https://example.invalid")
    calls = _stub_login_calls(app)
    app.login("user", "pw")
    _age_session(app)
    refreshed = [app.ensure_fresh_session() for _ in range(5)]
    assert refreshed == [True, False, False, False, False]
    assert calls.count("/api/auth/login") == 2  # initial + exactly one re-login


def test_fresh_session_never_relogs_in():
    app = smoke.App("https://example.invalid")
    calls = _stub_login_calls(app)
    app.login("user", "pw")
    for _ in range(5):
        assert app.ensure_fresh_session() is False
    assert calls.count("/api/auth/login") == 1


def test_never_logged_in_double_is_inert():
    """Test doubles that bypass __init__ (class-level defaults) never re-login."""
    app = smoke.App.__new__(smoke.App)
    assert app.ensure_fresh_session() is False


def test_relogin_failure_raises_and_prints_like_initial_login(capsys):
    app = smoke.App("https://example.invalid")
    _stub_login_calls(app)
    app.login("user", "pw")
    _age_session(app)
    _stub_login_calls(app, status="error")
    with pytest.raises(RuntimeError):
        app.ensure_fresh_session()
    assert "ERROR: login failed" in capsys.readouterr().out


class _FreshnessProbeApp(smoke.App):
    """Real App whose network surface is stubbed; records freshness checks."""

    def __init__(self):
        self.base = "https://example.invalid"
        self.freshness_checks = 0
        self.ask_result = {
            "answer": "grounded answer " * 5, "summary": {
                "total_tool_calls": 2, "system_queries": 2,
                "neptune_queries": 1},
            "integrity": {}, "errors": [], "done": True,
            "tool_summaries": [], "tool_names": ["erp"], "tool_starts": 2,
            "elapsed_seconds": 1.0,
        }

    def ensure_fresh_session(self, max_age_seconds=smoke.SESSION_MAX_AGE_SECONDS):
        self.freshness_checks += 1
        return False

    def ask_stream(self, question, session_id, timeout):
        # Mirror the real App.ask_stream contract: the freshness check lives
        # INSIDE ask_stream so retries/unsticks/warmups are covered too.
        self.ensure_fresh_session()
        return dict(self.ask_result)


def test_real_ask_stream_checks_freshness_before_opening_the_request(monkeypatch):
    """The check is inside App.ask_stream itself — not only in the lanes' loops."""
    app = smoke.App("https://example.invalid")
    order = []
    monkeypatch.setattr(app, "ensure_fresh_session",
                        lambda max_age_seconds=None: order.append("fresh") or False)

    class _Opener:
        def open(self, request, timeout):
            order.append("open")
            raise OSError("stubbed network")
    app.opener = _Opener()
    with pytest.raises(OSError):
        app.ask_stream("q", "s", 1)
    assert order == ["fresh", "open"]


def test_smoke_run_prompt_checks_freshness_before_the_ask():
    app = _FreshnessProbeApp()
    args = types.SimpleNamespace(strict=False, quality_strict=False, timeout=1)
    namespace = smoke.create_session_namespace("run", "smoke")
    row, problems = smoke._run_prompt(
        app, args, namespace, {"title": "t", "text": "q"}, 1, 1)
    assert app.freshness_checks == 1
    assert problems == []


def test_eval_run_question_checks_freshness_per_question_and_per_repeat(monkeypatch):
    app = _FreshnessProbeApp()
    monkeypatch.setattr(
        eval_answers, "_ask_with_unstick",
        lambda app_, args, q, sid, unsticks: (app_.ask_stream(q["text"], sid, 1), sid))
    monkeypatch.setattr(
        eval_answers, "_scored_row",
        lambda q, sid, result, strict: {
            "id": q["id"], "verdict": "pass", "failures": [],
            "transport_failure": False, "metrics": None})
    args = types.SimpleNamespace(repeats=5, no_memory=False,
                                 memory_suppression="marker", strict=False,
                                 timeout=1)
    namespace = smoke.create_session_namespace(prefix="eval")
    row = eval_answers._repeat_rows(
        app, args, namespace, {"id": "q1", "text": "t"}, 1, 1, [])
    # per repeat: 5 repeats => 5 freshness checks, one per ask
    assert app.freshness_checks == 5
    assert row["verdict"] == "pass"
