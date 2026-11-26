# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for tests.harness.graph_reset — no live AWS, no live app.

These prove the two properties the destructive path can't demonstrate live here:

  1. The safety guards SHORT-CIRCUIT *before any write*. We monkeypatch
     ``neptune_write`` (and the endpoint helper / ``_guard.boto3`` STS) to
     fail-if-called and assert the relevant ``GuardError`` is raised first:
       - no opt-in (no ``--yes`` / ``ADT_HARNESS_ALLOW_DESTRUCTIVE``);
       - opt-in but a non-dev account;
       - opt-in + dev account but unset/placeholder ``GRAPH_IDENTIFIER``.
  2. ``verify_graph`` delegates to ``run_audit`` and the CLI maps
     ``status`` -> exit code (pass/nothing_to_audit -> 0, fail -> 1).

Everything is monkeypatched; nothing touches AWS or a running app.
"""

from __future__ import annotations

import sys

import pytest

from tests.audit import audit_onboarding_graph
from tests.harness import _guard, graph_reset


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

def _boom_write(*_args, **_kwargs):
    raise AssertionError("neptune_write must NOT be called before guards pass")


class _FakeSTS:
    def __init__(self, account: str):
        self._account = account

    def get_caller_identity(self):
        return {"Account": self._account}


class _FakeSession:
    def __init__(self, account: str, region: str):
        self.region_name = region
        self._account = account

    def client(self, _name, region_name=None):
        return _FakeSTS(self._account)


class _FakeBoto3:
    """Minimal stand-in for the ``boto3`` module used inside ``_guard``."""

    def __init__(self, account: str, region: str):
        self._account = account
        self._region = region

    def Session(self, profile_name=None):  # noqa: N802 — mirror boto3.Session
        return _FakeSession(self._account, self._region)


@pytest.fixture
def dev_creds(monkeypatch):
    """Make ``assert_dev_account`` pass (dev account + region)."""
    monkeypatch.setattr(_guard, "boto3", _FakeBoto3(_guard.DEV_ACCOUNT, _guard.DEV_REGION))
    monkeypatch.setenv("AWS_REGION", _guard.DEV_REGION)


# ---------------------------------------------------------------------------
# Guard short-circuit (no write) tests
# ---------------------------------------------------------------------------

def test_reset_without_optin_raises_before_any_write(monkeypatch):
    monkeypatch.delenv("ADT_HARNESS_ALLOW_DESTRUCTIVE", raising=False)
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    # Prove require_opt_in is the FIRST guard: later guards must not even run.
    monkeypatch.setattr(_guard, "assert_dev_account",
                        lambda *a, **k: pytest.fail("assert_dev_account ran before opt-in"))

    with pytest.raises(_guard.GuardError, match="without opt-in"):
        graph_reset.reset_graph(mode="direct", yes=False)


def test_reset_endpoint_without_optin_never_calls_endpoint(monkeypatch):
    monkeypatch.delenv("ADT_HARNESS_ALLOW_DESTRUCTIVE", raising=False)
    monkeypatch.setattr(graph_reset, "_reset_via_endpoint",
                        lambda *a, **k: pytest.fail("endpoint POST ran before opt-in"))

    with pytest.raises(_guard.GuardError, match="without opt-in"):
        graph_reset.reset_graph(mode="endpoint", yes=False)


def test_reset_non_dev_account_refuses_without_write(monkeypatch):
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setattr(_guard, "boto3", _FakeBoto3("999999999999", _guard.DEV_REGION))
    monkeypatch.setenv("AWS_REGION", _guard.DEV_REGION)
    monkeypatch.setenv("GRAPH_IDENTIFIER", "some-real-looking-graph")  # must not be reached

    with pytest.raises(_guard.GuardError, match="dev account"):
        graph_reset.reset_graph(mode="direct", yes=True)


def test_reset_unset_graph_refuses_without_write(monkeypatch, dev_creds):
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)

    with pytest.raises(_guard.GuardError, match="GRAPH_IDENTIFIER is unset"):
        graph_reset.reset_graph(mode="direct", yes=True)


def test_reset_placeholder_graph_refuses_without_write(monkeypatch, dev_creds):
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setenv("GRAPH_IDENTIFIER", _guard.GRAPH_PLACEHOLDER)

    with pytest.raises(_guard.GuardError, match="placeholder"):
        graph_reset.reset_graph(mode="direct", yes=True)


# ---------------------------------------------------------------------------
# Positive: writes only AFTER all guards pass
# ---------------------------------------------------------------------------

def test_reset_direct_writes_after_guards_pass(monkeypatch, dev_creds):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    calls: list[str] = []
    monkeypatch.setattr(graph_reset, "neptune_write",
                        lambda cypher, params=None: calls.append(cypher) or True)

    result = graph_reset.reset_graph(mode="direct", yes=True)

    assert result["mode"] == "direct"
    assert result["labels_cleared"] == list(graph_reset.ONBOARDING_LABELS)
    deletes = [c for c in calls if "DETACH DELETE" in c]
    assert len(deletes) == len(graph_reset.ONBOARDING_LABELS)
    # Every onboarding label is wiped, in order, before reseeding runs.
    for label, cypher in zip(graph_reset.ONBOARDING_LABELS, deletes):
        assert f"(n:{label})" in cypher
    # Core concepts reseeded from catalog.core_concept_merges() (all writes beyond the deletes).
    assert result["concepts_seeded"] == len(calls) - len(deletes)
    assert result["concepts_seeded"] >= 1


# ---------------------------------------------------------------------------
# Endpoint mode happy path (fake httpx — auth then reset POST)
# ---------------------------------------------------------------------------

class _FakeResp:
    status_code = 200

    def __init__(self, payload, status_code=None):
        self._payload = payload
        if status_code is not None:
            self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    #: What the reset job's status poll answers, in order (the last repeats):
    #: a job record, or a bare HTTP status (an answer with no job in it).
    statuses = [{"state": "done", "job_id": "job-1",
                 "result": {"status": "reset", "systems_cleared": 2}}]

    #: The status the job start answers (405: an app of the previous release).
    start_status = 200

    def __init__(self, posts, **_kw):
        self._posts = posts
        self._polls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def post(self, url, json=None, **_kw):
        self._posts.append(url)
        if url.endswith(graph_reset.RESET_JOB_PATH):
            if self.start_status != 200:
                return _FakeResp({"detail": "Method Not Allowed"}, self.start_status)
            return _FakeResp({"status": "started", "job_id": "job-1"})
        if url.endswith(graph_reset.RESET_PATH):
            return _FakeResp({"status": "reset", "systems_cleared": 3})
        return _FakeResp({"status": "ok"})

    def get(self, url):
        self._posts.append(url)
        status = self.statuses[min(self._polls, len(self.statuses) - 1)]
        self._polls += 1
        if isinstance(status, int):
            return _FakeResp({"detail": "Not Found"}, status)
        return _FakeResp(status)


class _FakeHttpx:
    def __init__(self, posts):
        self._posts = posts

    def Client(self, **kw):  # noqa: N802 — mirror httpx.Client
        return _FakeClient(self._posts, **kw)


def test_reset_endpoint_logs_in_then_posts_reset(monkeypatch, dev_creds):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    monkeypatch.setenv("ADT_BASE_URL", "https://app.example.com/")
    monkeypatch.setenv("ADT_PASSWORD", "pw")
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)  # endpoint mode never writes directly
    posts: list[str] = []
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx(posts))

    result = graph_reset.reset_graph(mode="endpoint", yes=True)

    assert result["status"] == "reset"
    assert result["systems_cleared"] == 2
    assert result["mode"] == "endpoint"
    assert posts == [
        f"https://app.example.com{graph_reset.LOGIN_PATH}",
        f"https://app.example.com{graph_reset.RESET_JOB_PATH}",
        f"https://app.example.com{graph_reset.RESET_JOB_PATH}/job-1",
    ]


@pytest.mark.parametrize("status", [405, 404])
def test_reset_endpoint_falls_back_to_the_synchronous_reset_on_an_older_app(
        monkeypatch, dev_creds, status):
    """An app of the previous release has no job route: the synchronous reset
    runs and its answer is the result."""
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    monkeypatch.setenv("ADT_BASE_URL", "https://app.example.com/")
    monkeypatch.setenv("ADT_PASSWORD", "pw")
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setattr(_FakeClient, "start_status", status)
    posts: list[str] = []
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx(posts))
    result = graph_reset.reset_graph(mode="endpoint", yes=True)
    assert result["status"] == "reset" and result["systems_cleared"] == 3
    assert posts == [
        f"https://app.example.com{graph_reset.LOGIN_PATH}",
        f"https://app.example.com{graph_reset.RESET_JOB_PATH}",
        f"https://app.example.com{graph_reset.RESET_PATH}",
    ]


def test_reset_endpoint_polls_the_job_until_it_ends(monkeypatch, dev_creds):
    """Reset All is a background job: the POST answers once it started, and
    the result comes from its status, polled while it runs."""
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    monkeypatch.setenv("ADT_BASE_URL", "https://app.example.com/")
    monkeypatch.setenv("ADT_PASSWORD", "pw")
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setattr(graph_reset, "RESET_POLL_SECONDS", 0.0)
    monkeypatch.setattr(_FakeClient, "statuses", [
        {"state": "running", "job_id": "job-1"}, {"state": "running", "job_id": "job-1"},
        {"state": "done", "job_id": "job-1", "result": {"status": "reset"}}])
    posts: list[str] = []
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx(posts))
    assert graph_reset.reset_graph(mode="endpoint", yes=True)["status"] == "reset"
    assert posts.count(f"https://app.example.com{graph_reset.RESET_JOB_PATH}/job-1") == 3


@pytest.mark.parametrize("status,match", [
    ({"state": "error", "reason": "reset_incomplete", "error": "still deleting"},
     "reset incomplete, retry: still deleting"),
    ({"state": "error", "error": "boom"}, "reset failed: boom"),
    ({"state": "none"}, "was not found"),
])
def test_reset_endpoint_raises_when_the_job_does_not_finish(monkeypatch, dev_creds,
                                                            status, match):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    monkeypatch.setenv("ADT_BASE_URL", "https://app.example.com/")
    monkeypatch.setenv("ADT_PASSWORD", "pw")
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setattr(_FakeClient, "statuses", [status])
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx([]))
    with pytest.raises(RuntimeError, match=match):
        graph_reset.reset_graph(mode="endpoint", yes=True)


class _Resp503(_FakeResp):
    status_code = 503


def test_reset_endpoint_surfaces_503_as_retryable(monkeypatch, dev_creds):
    """The server 503s BEFORE wiping anything while runtimes are still deleting."""
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    monkeypatch.setenv("ADT_BASE_URL", "https://app.example.com/")
    monkeypatch.setenv("ADT_PASSWORD", "pw")
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    posts: list[str] = []
    fake = _FakeHttpx(posts)
    monkeypatch.setattr(_FakeClient, "post", lambda self, url, json=None, **_kw: (
        posts.append(url) or (_Resp503({"detail": "still deleting"})
                              if url.endswith(graph_reset.RESET_JOB_PATH)
                              else _FakeResp({"status": "ok"}))))
    monkeypatch.setitem(sys.modules, "httpx", fake)
    with pytest.raises(RuntimeError, match="retry: still deleting"):
        graph_reset.reset_graph(mode="endpoint", yes=True)


# ---------------------------------------------------------------------------
# verify_graph delegation + CLI exit-code mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected_exit", [
    ("pass", 0),
    ("fail", 1),
    ("nothing_to_audit", 0),
])
def test_verify_cli_maps_status_to_exit_code(monkeypatch, status, expected_exit):
    captured = {}

    def _fake_run_audit(system_id=None):
        captured["system_id"] = system_id
        return {"status": status}

    monkeypatch.setattr(audit_onboarding_graph, "run_audit", _fake_run_audit)

    assert graph_reset.main(["verify", "--system-id", "cmms"]) == expected_exit
    assert captured["system_id"] == "cmms"


def test_verify_graph_returns_report_dict(monkeypatch):
    report = {"status": "pass", "target_system": "cmms"}
    monkeypatch.setattr(audit_onboarding_graph, "run_audit", lambda system_id=None: report)
    assert graph_reset.verify_graph("cmms") is report


def test_reset_cli_without_optin_exits_nonzero(monkeypatch):
    monkeypatch.delenv("ADT_HARNESS_ALLOW_DESTRUCTIVE", raising=False)
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setattr(graph_reset, "_reset_via_endpoint",
                        lambda *a, **k: pytest.fail("endpoint POST ran before opt-in"))

    assert graph_reset.main(["reset", "--mode", "direct"]) == 1


@pytest.mark.parametrize("unknown", [404, 405, 503])
def test_a_status_poll_a_previous_release_task_answers_is_asked_again(
        monkeypatch, dev_creds, unknown):
    """A rolling deploy: the job started on a new task, a poll reaches an old
    one (no status route) or the status route cannot read the job right now.
    The harness asks again until the job ends, and never starts the
    synchronous reset once a job exists."""
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    monkeypatch.setenv("ADT_BASE_URL", "https://app.example.com/")
    monkeypatch.setenv("ADT_PASSWORD", "pw")
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setattr(graph_reset, "RESET_POLL_SECONDS", 0.0)
    monkeypatch.setattr(_FakeClient, "statuses", [
        unknown, {"state": "running", "job_id": "job-1"}, unknown,
        {"state": "done", "job_id": "job-1", "result": {"status": "reset", "systems_cleared": 4}}])
    posts: list[str] = []
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx(posts))
    result = graph_reset.reset_graph(mode="endpoint", yes=True)
    assert result["status"] == "reset" and result["systems_cleared"] == 4
    assert posts.count(f"https://app.example.com{graph_reset.RESET_JOB_PATH}/job-1") == 4
    assert f"https://app.example.com{graph_reset.RESET_PATH}" not in posts


def test_a_job_whose_status_stays_unknown_ends_at_the_deadline_without_a_second_reset(
        monkeypatch, dev_creds):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    monkeypatch.setenv("ADT_BASE_URL", "https://app.example.com/")
    monkeypatch.setenv("ADT_PASSWORD", "pw")
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setattr(graph_reset, "RESET_POLL_SECONDS", 0.0)
    monkeypatch.setattr(graph_reset, "RESET_JOB_DEADLINE_S", 0.05)
    monkeypatch.setattr(_FakeClient, "statuses", [404])
    posts: list[str] = []
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx(posts))
    with pytest.raises(RuntimeError, match="did not finish"):
        graph_reset.reset_graph(mode="endpoint", yes=True)
    assert f"https://app.example.com{graph_reset.RESET_PATH}" not in posts


def test_a_status_poll_that_fails_otherwise_still_raises(monkeypatch, dev_creds):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "graph-abc123")
    monkeypatch.setenv("ADT_BASE_URL", "https://app.example.com/")
    monkeypatch.setenv("ADT_PASSWORD", "pw")
    monkeypatch.setattr(graph_reset, "neptune_write", _boom_write)
    monkeypatch.setattr(_FakeClient, "statuses", [500])
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx([]))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        graph_reset.reset_graph(mode="endpoint", yes=True)
