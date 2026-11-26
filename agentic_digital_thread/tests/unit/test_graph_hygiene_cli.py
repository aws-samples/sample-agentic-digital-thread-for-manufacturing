# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/graph_hygiene.py — the CLI drives the steward-gated repair route.

Offline: a fake ``App`` records calls; no network.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("graph_hygiene_cli",
                                               _SCRIPTS / "graph_hygiene.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_PW = "pw-fixture"  # nosec B105 - offline test fixture


class _FakeApp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    def __call__(self, base_url):
        self.base_url = base_url
        return self

    def call(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _report(verified=True, dry_run=False, scan_failed=False):
    vm = {"dry_run": dry_run, "malformed_learned_retired": 1,
          "duplicate_generated_retired": 2, "kept": 1, "scan_failed": scan_failed,
          "verified": verified and not scan_failed}
    if not dry_run:
        vm["readback"] = {"verified": verified, "still_present": [], "kept_missing": []}
    return {"repairs": 0, "deletions": 0, "value_mappings": vm, "dry_run": dry_run}


def test_logs_in_then_posts_dry_run_and_prints_report(capsys):
    app = _FakeApp([{"status": "ok"}, _report(dry_run=True)])
    code = cli.main(["--base-url", "https://x.example", "--password", _PW, "--dry-run"],
                    app_factory=app)
    assert code == 0
    assert app.calls == [
        ("POST", "/api/auth/login", {"email": "ADT", "password": _PW}),
        ("POST", "/api/neptune/graph/repair", {"dry_run": True}),
    ]
    assert json.loads(capsys.readouterr().out)["value_mappings"]["dry_run"] is True


def test_apply_is_the_default():
    app = _FakeApp([{"status": "ok"}, _report()])
    assert cli.main(["--base-url", "https://x.example", "--password", _PW],
                    app_factory=app) == 0
    assert app.calls[1] == ("POST", "/api/neptune/graph/repair", {"dry_run": False})


def test_unverified_readback_exits_1():
    app = _FakeApp([{"status": "ok"}, _report(verified=False)])
    assert cli.main(["--base-url", "https://x.example", "--password", _PW],
                    app_factory=app) == 1


def test_http_error_exits_1(capsys):
    err = urllib.error.HTTPError("u", 403, "Forbidden", {},
                                 io.BytesIO(b'{"detail":"Steward (admin) role required"}'))
    app = _FakeApp([{"status": "ok"}, err])
    assert cli.main(["--base-url", "https://x.example", "--password", _PW],
                    app_factory=app) == 1
    assert "HTTP 403" in capsys.readouterr().out


def test_login_failure_and_missing_args_exit_2(monkeypatch):
    monkeypatch.delenv("ADT_PASSWORD", raising=False)
    monkeypatch.delenv("ADT_BASE_URL", raising=False)
    assert cli.main([], app_factory=_FakeApp([])) == 2
    app = _FakeApp([{"status": "denied"}])
    assert cli.main(["--base-url", "https://x.example", "--password", _PW],
                    app_factory=app) == 2
    assert len(app.calls) == 1


def test_login_failure_prints_only_the_status_never_the_reply(capsys):
    reply = {"status": "denied", "detail": "echo pw-fixture", "session": "sess-fixture"}
    assert cli.main(_argv(), app_factory=_FakeApp([reply])) == 2
    err = capsys.readouterr().err
    assert "login failed" in err and "denied" in err
    assert "pw-fixture" not in err and "sess-fixture" not in err


def _argv(*extra):
    return ["--base-url", "https://x.example", "--password", _PW, *extra]


def test_failed_scan_exits_1_even_in_a_dry_run():
    """A failed graph read must not pass for a clean graph."""
    app = _FakeApp([{"status": "ok"}, _report(dry_run=True, scan_failed=True)])
    assert cli.main(_argv("--dry-run"), app_factory=app) == 1


def test_top_level_unverified_exits_1_even_when_the_readback_block_is_absent():
    report = _report()
    report["value_mappings"].pop("readback")
    report["value_mappings"]["verified"] = False
    app = _FakeApp([{"status": "ok"}, report])
    assert cli.main(_argv(), app_factory=app) == 1


def test_missing_value_mappings_section_exits_1():
    app = _FakeApp([{"status": "ok"}, {"repairs": 0, "deletions": 0, "dry_run": False}])
    assert cli.main(_argv(), app_factory=app) == 1


def test_password_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ADT_PASSWORD", _PW)
    app = _FakeApp([{"status": "ok"}, _report(dry_run=True)])
    assert cli.main(["--base-url", "https://x.example", "--dry-run"], app_factory=app) == 0
    assert app.calls[0][2]["password"] == _PW


def test_usage_shows_no_password_on_the_command_line():
    usage = cli.__doc__.split("Usage", 1)[1].split("Exit", 1)[0]
    assert "--password" not in usage and "ADT_PASSWORD" in usage
