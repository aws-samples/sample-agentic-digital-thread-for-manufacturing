# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The harness waits for a re-provision's own job, and stops at its deadline.

A re-provision starts a new provision job for a system the catalog already
lists as onboarded, so the catalog says nothing about that job. The harness
waited for the catalog, so it returned while the job was still rewriting the
system's graph (the workshop seed reported success minutes before its last
re-provision ended). It now waits for a re-provision's job to end, and fails
with the job's own error. A first provision still returns once the catalog
lists the system, unless ``follow_job=True`` (the workshop seed) asks for its
job's end too. A job whose status cannot be read, or that the app does not
know, is ``unknown``, not an ended job. An ``App`` with a ``deadline``
(``time.monotonic``) caps every call and poll at the time left
(``_monotonic``, a seam on ``time.monotonic``). Offline and deterministic.
"""
import importlib.util
import io
import time
import urllib.error
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "onboarding_smoketest.py"
_spec = importlib.util.spec_from_file_location("onboarding_smoketest_wait", _HARNESS_PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


class _ScriptedApp:
    """Scripted answers per kind of call; the last answer repeats. Records
    every call as (kind, path)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def call(self, method, path, body=None, timeout=120):
        kind = ("catalog" if path == "/api/onboard/catalog" else
                "provision" if method == "POST" else "status")
        self.calls.append((kind, path))
        answers = self.script[kind]
        item = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(item, Exception):
            raise item
        return item

    def catalog(self):
        return smoke.App.catalog(self)

    def status_reads(self):
        return [p for k, p in self.calls if k == "status"]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(smoke.time, "sleep", lambda _s: None)


_ONBOARDED = [{"id": "sys-a", "status": "onboarded"}]


def _provision(app, max_s=60):
    return smoke.provision(app, "sys-a", {"connection_type": "PostgreSQL"}, [], [], max_s=max_s)


def test_a_reprovision_waits_for_its_job_to_end():
    """REPRODUCTION. The catalog lists the system as onboarded before the
    POST. 1a1fb7ca returned True on its first catalog read and never read the
    job; the job was still running."""
    app = _ScriptedApp({
        "catalog": [_ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"status": "running"}, {"status": "running"}, {"status": "done"}],
    })
    assert _provision(app) is True
    assert app.status_reads() == ["/api/onboard/provision/status/j1"] * 3


def test_a_reprovision_whose_job_fails_raises_the_jobs_error():
    """REPRODUCTION: 1a1fb7ca reported the failed re-provision as OK."""
    app = _ScriptedApp({
        "catalog": [_ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"status": "running"},
                   {"status": "error", "error": "provision_failed", "reason": "retired_link_sweep_failed"}],
    })
    with pytest.raises(RuntimeError, match="provision\\[sys-a\\] failed: provision_failed"):
        _provision(app)


def test_a_job_that_ended_in_error_raises_the_typed_job_failure():
    """The workshop seed re-runs a provision once when its job ended in error
    (``ProvisionJobFailed``), and only then: a provision that did not start,
    or did not finish, is not that."""
    assert issubclass(smoke.ProvisionJobFailed, RuntimeError)
    reprovision = _ScriptedApp({
        "catalog": [_ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"status": "error", "error": "provision_failed"}],
    })
    with pytest.raises(smoke.ProvisionJobFailed, match="provision\\[sys-a\\] failed"):
        _provision(reprovision)
    first = _ScriptedApp({
        "catalog": [[{"id": "sys-a", "status": "registered"}]],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"status": "error", "error": "provision_failed"}],
    })
    with pytest.raises(smoke.ProvisionJobFailed, match="provision\\[sys-a\\] failed"):
        _provision(first)


def test_a_provision_the_app_refused_is_not_a_job_failure():
    app = _ScriptedApp({
        "catalog": [_ONBOARDED],
        "provision": [urllib.error.HTTPError("http://x", 409, "Conflict", {}, io.BytesIO(b""))],
        "status": [{"status": "running"}],
    })
    with pytest.raises(urllib.error.HTTPError):
        _provision(app)


def test_a_reprovision_whose_job_the_app_lost_raises():
    """REPRODUCTION: a job the app no longer knows (it restarted) is not a
    finished re-provision."""
    app = _ScriptedApp({
        "catalog": [_ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"error": "Unknown job_id 'j1'"}],
    })
    with pytest.raises(RuntimeError, match="does not know provision job j1"):
        _provision(app)


def test_a_reprovision_still_running_at_max_s_is_not_reported_done(monkeypatch):
    """REPRODUCTION: the wait is bounded, and a job still running at the
    bound is False (not provisioned), never True."""
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(smoke, "_monotonic", lambda: float(next(clock)), raising=False)
    app = _ScriptedApp({
        "catalog": [_ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"status": "running"}],
    })
    assert _provision(app, max_s=100) is False


def test_a_first_provision_still_returns_once_the_catalog_lists_it():
    """Guard: the system was not onboarded before the POST, so the catalog
    flip is the signal, as before (the job keeps running through the runtime
    warmup)."""
    app = _ScriptedApp({
        "catalog": [[{"id": "sys-a", "status": "registered"}], _ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"status": "running"}],
    })
    assert _provision(app) is True                           # the job itself never ended


def test_a_first_provision_that_follows_its_job_waits_past_the_catalog_flip():
    """REPRODUCTION (round 2). The workshop seed's phase 1 needs the job's own
    end: the catalog lists the system as onboarded while the job still runs,
    so a job error after the flip went unseen and was never re-run. With
    ``follow_job=True`` the job is followed to its end and its error raised;
    1a813d9e had no such mode."""
    failing = _ScriptedApp({
        "catalog": [[{"id": "sys-a", "status": "registered"}], _ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"status": "running"}, {"status": "running"},
                   {"status": "error", "error": "provision_failed"}],
    })
    with pytest.raises(smoke.ProvisionJobFailed, match="provision\\[sys-a\\] failed: provision_failed"):
        smoke.provision(failing, "sys-a", {}, [], [], max_s=60, follow_job=True)
    assert failing.status_reads() == ["/api/onboard/provision/status/j1"] * 3
    finishing = _ScriptedApp({
        "catalog": [[{"id": "sys-a", "status": "registered"}], _ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"status": "running"}, {"status": "done"}],
    })
    assert smoke.provision(finishing, "sys-a", {}, [], [], max_s=60, follow_job=True) is True
    assert finishing.status_reads() == ["/api/onboard/provision/status/j1"] * 2


def test_a_job_that_cannot_be_read_or_that_the_app_does_not_know_has_not_ended():
    """REPRODUCTION (round 2): 1a813d9e reported an unknown job id and an
    unreadable status as ``error``, the job's end, and the workshop seed
    re-ran it. Neither shows the job ended, so both are ``unknown``."""
    def http(code):
        return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))
    answers = {"lost": iter([{"error": "Unknown job_id 'lost'"}]),
               "busy": iter([http(502)] * (smoke._JOB_READ_5XX_LIMIT + 1)),
               "denied": iter([http(403)])}

    class _App:
        def call(self, method, path, body=None, timeout=120):
            answer = next(answers[path.rsplit("/", 1)[-1]])
            if isinstance(answer, Exception):
                raise answer
            return answer
    out = smoke.wait_provision_jobs(_App(), {"a": "lost", "b": "busy", "c": "denied"}, max_s=600)
    assert out == {"a": ("unknown", "the app does not know provision job lost (did it restart?)"),
                   "b": ("unknown", "its job status could not be read (HTTP 502)"),
                   "c": ("unknown", "its job status could not be read (HTTP 403)")}


def test_a_provision_whose_job_cannot_be_followed_is_not_a_job_failure():
    """REPRODUCTION (round 2): the seed re-runs only ``ProvisionJobFailed``,
    an explicit job error. 1a813d9e raised it for a job the app no longer
    knows; that raises a plain ``RuntimeError`` now, with the same text."""
    app = _ScriptedApp({
        "catalog": [_ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [{"error": "Unknown job_id 'j1'"}],
    })
    with pytest.raises(RuntimeError, match="does not know provision job j1") as info:
        _provision(app)
    assert not isinstance(info.value, smoke.ProvisionJobFailed)


def test_wait_provision_jobs_follows_every_job_to_its_end():
    """REPRODUCTION (new): several jobs polled together, each outcome kept,
    an ``on_end`` callback per job as it ends."""
    answers = {
        "a1": iter([{"status": "running"}, {"status": "done"}]),
        "b1": iter([{"status": "error", "error": "provision_failed"}]),
        "c1": iter([{"status": "running"}] * 50),
    }

    class _App:
        def call(self, method, path, body=None, timeout=120):
            return next(answers[path.rsplit("/", 1)[-1]])
    ended = []
    out = smoke.wait_provision_jobs(_App(), {"a": "a1", "b": "b1", "c": "c1", "d": ""}, max_s=0,
                                    on_end=lambda sid, status, _d: ended.append((sid, status)))
    assert out["d"] == ("done", "")                         # finished synchronously
    assert out["b"] == ("error", "provision_failed")
    assert out["a"][0] == "timeout" and out["c"][0] == "timeout"   # max_s 0: one round
    assert sorted(ended) == [("a", "timeout"), ("b", "error"), ("c", "timeout"), ("d", "done")]


def test_an_app_call_past_its_deadline_raises_without_a_request():
    """REPRODUCTION (new): the deadline is checked before the request is sent."""
    app = smoke.App("https://example.invalid")
    app.deadline = time.monotonic() - 1
    opened = []
    app.opener = type("O", (), {"open": lambda self, *a, **k: opened.append(a)})()
    with pytest.raises(smoke.DeadlineExceeded, match="GET /api/onboard/catalog"):
        app.call("GET", "/api/onboard/catalog")
    assert opened == []
    assert issubclass(smoke.DeadlineExceeded, TimeoutError)


def test_an_app_call_waits_no_longer_than_the_time_left():
    """REPRODUCTION (new): a call's socket timeout is capped at the time left."""
    app = smoke.App("https://example.invalid")
    app.deadline = time.monotonic() + 7
    seen = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    app.opener = type("O", (), {"open": lambda self, req, timeout: seen.append(timeout) or _Resp(b"{}")})()
    app.call("GET", "/api/onboard/catalog", timeout=120)
    assert 1 <= seen[0] <= 7


def test_a_poll_stops_at_the_deadline(monkeypatch):
    """REPRODUCTION (new): ``App.poll`` returns timeout once the deadline is
    reached, whatever its own ``max_s``."""
    now = {"t": 100.0, "sleeps": 0}

    def sleep(seconds):
        now["t"] += seconds
        now["sleeps"] += 1
        if now["sleeps"] > 200:                              # 1a1fb7ca polled on, unbounded
            raise RuntimeError("the poll ran on past its deadline")
    monkeypatch.setattr(smoke, "_monotonic", lambda: now["t"], raising=False)
    monkeypatch.setattr(smoke.time, "sleep", sleep)
    app = smoke.App("https://example.invalid")
    app.deadline = 130.0
    app.call = lambda *_a, **_k: {"status": "running"}
    assert app.poll("/api/onboard/resolve/status/x", "resolve[x]", max_s=600) == {"status": "timeout"}
    assert now["t"] <= 135.0


def test_a_5xx_on_a_job_read_is_retried():
    """Guard (the existing polls' tolerance): a short run of 5xx on the job
    read is retried; the job's own end decides."""
    err = urllib.error.HTTPError("http://x", 502, "err", {}, io.BytesIO(b""))
    app = _ScriptedApp({
        "catalog": [_ONBOARDED],
        "provision": [{"job_id": "j1", "status": "running"}],
        "status": [err, err, {"status": "done"}],
    })
    assert _provision(app) is True
