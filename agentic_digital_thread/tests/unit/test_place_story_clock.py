# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``scripts/place_story_clock.py`` credential handling.

The steward password comes from ``$ADT_PASSWORD`` ONLY (like
``preflight_workshop.py``): there is no ``--password`` flag (a flag lands in
shell history and ``ps``), a missing env var is a usage error (exit 2, no
call made) and a failed login never echoes the login response body.
Offline: a fake ``app_factory``.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("place_story_clock_env_cli",
                                               _SCRIPTS / "place_story_clock.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_PW = "pw-env-fixture-S3cret"  # nosec B105 - offline test fixture
_ARGS = ["--base-url", "https://x.example", "--alarm-at", "2026-10-01T14:10:00Z",
         "--expect-anchor", "1790000010", "--dry-run"]


class _FakeApp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    def __call__(self, base_url):
        return self

    def call(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_password_is_read_from_the_env_only(monkeypatch):
    monkeypatch.setenv("ADT_PASSWORD", _PW)
    app = _FakeApp([{"status": "ok"}, {"dry_run": True}])
    assert cli.main(list(_ARGS), app_factory=app) == 0
    assert app.calls[0] == ("POST", "/api/auth/login", {"email": "ADT", "password": _PW})


def test_there_is_no_password_flag(monkeypatch):
    monkeypatch.setenv("ADT_PASSWORD", _PW)
    assert "--password" not in cli._build_arg_parser().format_help()
    app = _FakeApp([])
    with pytest.raises(SystemExit) as exc:
        cli.main([*_ARGS, "--password", _PW], app_factory=app)
    assert exc.value.code == 2 and app.calls == []


@pytest.mark.parametrize("value", [None, ""])
def test_missing_password_env_is_exit_2_before_any_call(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv("ADT_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("ADT_PASSWORD", value)
    app = _FakeApp([])
    assert cli.main(list(_ARGS), app_factory=app) == 2
    assert app.calls == []
    assert "ADT_PASSWORD" in capsys.readouterr().err


@pytest.mark.parametrize("login", [
    {"status": "error", "detail": "bad", "echo": _PW, "token": "tok-SECRET"},
    urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(f'{{"echo": "{_PW}"}}'.encode())),
], ids=["rejected-body", "http-401"])
def test_failed_login_is_exit_2_and_never_echoes_the_body(monkeypatch, capsys, login):
    monkeypatch.setenv("ADT_PASSWORD", _PW)
    app = _FakeApp([login])
    assert cli.main(list(_ARGS), app_factory=app) == 2
    assert len(app.calls) == 1
    captured = capsys.readouterr()
    for secret in (_PW, "tok-SECRET", "detail", "echo"):
        assert secret not in captured.out and secret not in captured.err
    assert "login failed" in captured.err
