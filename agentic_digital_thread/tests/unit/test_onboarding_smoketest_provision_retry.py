# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""provision() must ride out a transient 5xx on its catalog poll.

Live evidence 2026-09-25 (rehearsal of the workshop root): while four systems
resolved in parallel the app answered 502 for a few seconds; the resolve
status polls retried, but provision's catalog check raised and the workshop
seed failed, rolling back the whole stack. Deterministic + offline.
"""
import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "onboarding_smoketest.py"
_spec = importlib.util.spec_from_file_location("onboarding_smoketest", _HARNESS_PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


class _FakeApp:
    """Scripted responses per path; an Exception entry is raised."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}

    def call(self, method, path, body=None, timeout=120):
        key = "catalog" if path == "/api/onboard/catalog" else (
            "provision" if method == "POST" else "status")
        item = self.script[key].pop(0) if len(self.script[key]) > 1 else self.script[key][0]
        if isinstance(item, Exception):
            raise item
        return item

    def catalog(self):
        return smoke.App.catalog(self)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(smoke.time, "sleep", lambda _s: None)


def _provision(app):
    return smoke.provision(app, "sys-a", {"connection_type": "PostgreSQL"}, [], [], max_s=60)


def test_transient_502_on_catalog_poll_is_retried():
    app = _FakeApp({
        "provision": [{"job_id": "j1"}],
        "catalog": [_http_error(502), _http_error(502),
                    [{"id": "sys-a", "status": "onboarded"}]],
        "status": [{"status": "running"}],
    })
    assert _provision(app) is True


def test_non_5xx_on_catalog_poll_still_fails_loudly():
    app = _FakeApp({
        "provision": [{"job_id": "j1"}],
        "catalog": [_http_error(403)],
        "status": [{"status": "running"}],
    })
    with pytest.raises(urllib.error.HTTPError):
        _provision(app)


def test_unbroken_run_of_5xx_is_bounded():
    app = _FakeApp({
        "provision": [{"job_id": "j1"}],
        "catalog": [_http_error(502)],
        "status": [{"status": "running"}],
    })
    with pytest.raises(urllib.error.HTTPError):
        _provision(app)
