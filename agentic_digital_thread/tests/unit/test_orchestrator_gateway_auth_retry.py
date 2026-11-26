# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway MCP auth-error transparent retry (Task 1E).

Offline asyncio tests (no AWS / no network / no real sleeps) pinning that
``_wrap_gateway_client_with_retry`` transparently absorbs GATEWAY-OUTBOUND auth
staleness blips (the gateway's cached M2M token for a target going stale — it
re-mints immediately after the failed call, so the next call succeeds):

  * an error result matching an auth marker is retried on the SHORT schedule
    (``_GATEWAY_AUTH_RETRY_BACKOFFS_S`` == (1.0, 3.0)) and the eventual success
    is returned;
  * on exhaustion the ORIGINAL error result is returned unchanged (honest
    fallback, no fabrication);
  * a non-matching application error is NOT retried (regression pin);
  * marker matching is case-insensitive;
  * the ADR-0039 property is preserved — the semaphore is released during the
    auth-backoff sleep, so a retrying call does not starve peer delegations;
  * transient (cold-runtime) errors still ride the LONG 5/15/30s schedule.

Scope note (mirrors the wrapper docstring): these retries cover OUTBOUND auth
staleness only. INBOUND bearer expiry (orchestrator→gateway) is handled by the
service-token cache refreshing pre-expiry, not by these retries.

Task-1E follow-up (persistent auth failures, deployed evidence 2026-07-15): a
client whose bearer goes stale fails EVERY call for its remaining lifetime —
retry-same-client cannot heal it. On AUTH-class retry exhaustion the wrapper now
rebuilds the MCP client via its ``rebuild_client`` seam (fresh bearer; the cached
service token is invalidated first so a fresh one is minted) and retries ONCE,
bounded by a per-wrapper cooldown; if the fresh client also fails, the ORIGINAL
error result is returned unchanged. Pinned below: rebuild-once→success, honest
fallback on double failure, non-auth never rebuilds, cooldown bounding, and the
service-token fresh-mint path in ``_rebuild_gateway_client``.

Fix-pass pins (rebuild lifecycle/teardown/token-gate):
  * LIFECYCLE — ``_rebuild_gateway_client`` STARTS the client it builds (strands
    1.42 ``call_tool_async`` raises on an unstarted client, which would make the
    recovery dead code). Pinned end-to-end with a fake whose ``call_tool_async``
    raises unless ``.start()`` ran, plus a negative control proving an unstarted
    rebuild cannot recover.
  * TEARDOWN — a FAILED fresh client (error result or raise) is ``stop()``ed
    best-effort; on ADOPTION the PREVIOUS (stale-bearer) client is stopped
    best-effort (safe: MCPClient.stop resets ``_tool_provider_started``, so the
    Agent's later remove_consumer->stop no-ops), and the ADOPTED client is
    stopped via the wrapped client's ``remove_consumer`` hook when the Agent's
    tool registry releases the wrapper (the existing provider-lifecycle path).
  * TOKEN GATE — the seam is wired by ``_get_orchestrator_tools`` ONLY for
    service-token turns; a forwarded end-user token gets ``rebuild_client=None``
    (rebuild cannot re-mint a per-request user token, so the original error
    surfaces with no pointless mint+start).
"""
from __future__ import annotations

import asyncio
import copy
import time

import pytest

orch = pytest.importorskip("agents.orchestrator")

AUTH_ERROR = {
    "status": "error",
    "content": [{"text": "McpException - MCP invocation failed: Authorization error when sending message"}],
}
SUCCESS = {"status": "success", "content": [{"text": "42"}]}
APP_ERROR = {"status": "error", "content": [{"text": "column foo does not exist"}]}


@pytest.fixture()
def recorded_sleeps(monkeypatch):
    """Replace asyncio.sleep with a no-op that records requested durations."""
    sleeps: list[float] = []

    async def _fake_sleep(delay, *a, **k):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return sleeps


# --------------------------------------------------------------------------- #
# constants / classification
# --------------------------------------------------------------------------- #
def test_auth_marker_constants_pinned():
    """The marker set is EXACTLY the one observed gateway-outbound phrase (fix-pass
    narrowing) — broader wording ('unauthorized' / bare 'authorization error')
    would silently retry, and thereby mask, genuine authN/authZ denials."""
    assert orch._AUTH_TOOL_ERROR_MARKERS == (
        "authorization error when sending message",
    )
    assert orch._GATEWAY_AUTH_RETRY_BACKOFFS_S == (1.0, 3.0)
    assert not any("access denied" in m for m in orch._AUTH_TOOL_ERROR_MARKERS)


@pytest.mark.parametrize(
    "text",
    [
        "McpException - MCP invocation failed: Authorization error when sending message",
        "AUTHORIZATION ERROR WHEN SENDING MESSAGE",
    ],
)
def test_auth_classifier_matches_case_insensitively(text):
    result = {"status": "error", "content": [{"text": text}]}
    assert orch._is_auth_tool_error(result) is True
    assert orch._retry_backoffs_for(result) == orch._GATEWAY_AUTH_RETRY_BACKOFFS_S


@pytest.mark.parametrize(
    "result",
    [
        SUCCESS,  # success -> never retried
        APP_ERROR,  # genuine application error -> must surface (honest fallback)
        {"status": "error", "content": [{"text": "access denied to table foo"}]},  # NARROW: not auth
        # Fix-pass NEGATIVE pins: generic auth wording is NOT the self-healing
        # gateway-outbound blip — these must surface, not retry.
        {"status": "error", "content": [{"text": "Authorization Error"}]},
        {"status": "error", "content": [{"text": "Unauthorized"}]},
        {"status": "error", "content": [{"text": "401 UNAUTHORIZED"}]},
    ],
)
def test_auth_classifier_rejects_non_auth(result):
    assert orch._is_auth_tool_error(result) is False


def test_classification_picks_schedule_per_error_class():
    """Auth -> short schedule; transient -> long schedule; app error -> None."""
    transient = {"status": "error", "content": [{"text": "No endpoint or agent found"}]}
    assert orch._retry_backoffs_for(AUTH_ERROR) == orch._GATEWAY_AUTH_RETRY_BACKOFFS_S
    assert orch._retry_backoffs_for(transient) == orch._GATEWAY_RETRY_BACKOFFS_S
    assert orch._retry_backoffs_for(APP_ERROR) is None
    assert orch._retry_backoffs_for(SUCCESS) is None


# --------------------------------------------------------------------------- #
# (a) auth error retried on the SHORT schedule; eventual success returned
# --------------------------------------------------------------------------- #
def test_auth_error_retried_on_short_schedule_returns_success(recorded_sleeps):
    calls = {"n": 0}

    class _C:
        async def call_tool_async(self, *a, **k):
            calls["n"] += 1
            return AUTH_ERROR if calls["n"] <= 2 else SUCCESS

    c = _C()
    orch._wrap_gateway_client_with_retry(c)
    out = asyncio.run(c.call_tool_async(name="digital-thread___explore_graph"))
    assert out == SUCCESS
    assert calls["n"] == 3  # initial + 2 retries
    assert recorded_sleeps == [1.0, 3.0]  # SHORT schedule, not 5/15/30


def test_auth_error_healed_on_first_retry_sleeps_once(recorded_sleeps):
    calls = {"n": 0}

    class _C:
        async def call_tool_async(self, *a, **k):
            calls["n"] += 1
            return AUTH_ERROR if calls["n"] == 1 else SUCCESS

    c = _C()
    orch._wrap_gateway_client_with_retry(c)
    out = asyncio.run(c.call_tool_async(name="digital-thread___run_query"))
    assert out == SUCCESS and calls["n"] == 2
    assert recorded_sleeps == [1.0]


# --------------------------------------------------------------------------- #
# (b) exhaustion returns the ORIGINAL error result unchanged
# --------------------------------------------------------------------------- #
def test_auth_exhaustion_returns_original_error_unchanged(recorded_sleeps):
    pristine = copy.deepcopy(AUTH_ERROR)
    calls = {"n": 0}

    class _C:
        async def call_tool_async(self, *a, **k):
            calls["n"] += 1
            return AUTH_ERROR

    c = _C()
    orch._wrap_gateway_client_with_retry(c)
    out = asyncio.run(c.call_tool_async(name="digital-thread___save_correlation"))
    assert out is AUTH_ERROR  # the underlying result object, not a wrapper/copy
    assert out == pristine  # ... and byte-identical content (nothing mutated)
    assert calls["n"] == 1 + len(orch._GATEWAY_AUTH_RETRY_BACKOFFS_S)
    assert recorded_sleeps == list(orch._GATEWAY_AUTH_RETRY_BACKOFFS_S)


# --------------------------------------------------------------------------- #
# (c) non-auth application errors are NOT retried (regression pin)
# --------------------------------------------------------------------------- #
def test_application_error_not_retried(recorded_sleeps):
    calls = {"n": 0}

    class _C:
        async def call_tool_async(self, *a, **k):
            calls["n"] += 1
            return APP_ERROR

    c = _C()
    orch._wrap_gateway_client_with_retry(c)
    out = asyncio.run(c.call_tool_async(name="query_x"))
    assert out is APP_ERROR
    assert calls["n"] == 1  # single attempt, surfaced to the model
    assert recorded_sleeps == []


# --------------------------------------------------------------------------- #
# transient class still rides the LONG schedule (regression pin)
# --------------------------------------------------------------------------- #
def test_transient_error_still_uses_long_schedule(recorded_sleeps):
    transient = {"status": "error", "content": [{"text": "No endpoint or agent found"}]}
    calls = {"n": 0}

    class _C:
        async def call_tool_async(self, *a, **k):
            calls["n"] += 1
            return transient if calls["n"] == 1 else SUCCESS

    c = _C()
    orch._wrap_gateway_client_with_retry(c)
    out = asyncio.run(c.call_tool_async(name="query_x"))
    assert out == SUCCESS and calls["n"] == 2
    assert recorded_sleeps == [orch._GATEWAY_RETRY_BACKOFFS_S[0]]  # 5.0, not 1.0


# --------------------------------------------------------------------------- #
# (e) ADR-0039: semaphore released during the auth-backoff sleep
# --------------------------------------------------------------------------- #
def test_semaphore_released_during_auth_backoff(monkeypatch):
    """With cap 1 and a real (small) auth backoff, a call sleeping in its auth
    backoff must NOT hold the single slot: a second concurrent call acquires it
    and completes during the first call's backoff window."""
    monkeypatch.setenv(orch.DELEGATION_CONCURRENCY_ENV, "1")
    monkeypatch.setattr(orch, "_GATEWAY_AUTH_RETRY_BACKOFFS_S", (0.2, 0.2))

    order: list[str] = []

    class _SlowAuthRetry:
        def __init__(self):
            self.first_started = False

        async def call_tool_async(self, *a, **k):
            if k.get("name") == "slow":
                if not self.first_started:
                    self.first_started = True
                    return AUTH_ERROR  # wrapper enters its auth-backoff sleep
                order.append("slow_done")
                return SUCCESS
            order.append("fast_done")
            return SUCCESS

    client = _SlowAuthRetry()
    orch._wrap_gateway_client_with_retry(client)

    async def _go():
        slow = asyncio.create_task(client.call_tool_async(name="slow"))
        await asyncio.sleep(0.05)  # let 'slow' take + release the slot (now in backoff)
        fast = asyncio.create_task(client.call_tool_async(name="fast"))
        await asyncio.gather(slow, fast)

    asyncio.run(_go())
    assert order and order[0] == "fast_done", (
        f"fast call should finish during slow's auth backoff (slot released); order={order}"
    )


# --------------------------------------------------------------------------- #
# prompt contract: authorization errors named as transient/self-healing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_prompt_names_authorization_errors_as_transient(mode):
    """The HANDLING SYSTEM ERRORS transient enumeration names EXACTLY the
    observed self-healing phrase (fix-pass narrowing) — and explicitly does NOT
    bless generic 'unauthorized' wording as transient, so the model reports
    genuine auth denials honestly instead of retrying around them."""
    prompt = orch._build_orchestrator_prompt(mode)
    lower = prompt.lower()
    errors_idx = lower.index("handling system errors")
    section = lower[errors_idx:errors_idx + 2000]
    assert "authorization error when sending message" in section
    # Negative pin: the broad marker wording is gone from the transient list
    # (the word appears ONLY inside the specific phrase / the not-transient
    # caveat, never as a standalone transient marker).
    assert '"unauthorized"' not in section
    assert '"authorization error" /' not in section


# --------------------------------------------------------------------------- #
# Task-1E follow-up: persistent-auth client REBUILD with a fresh bearer
# --------------------------------------------------------------------------- #
class _ScriptedClient:
    """Fake MCP client returning scripted results; the LAST result repeats.

    Records ``stop`` invocations so tests can assert the wrapper's teardown
    contract (failed fresh clients and replaced stale-bearer clients are stopped
    best-effort).
    """

    def __init__(self, results):
        self._results = list(results)
        self.calls: list = []
        self.stops = 0

    async def call_tool_async(self, *a, **k):
        self.calls.append(k.get("name"))
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]

    def stop(self, exc_type, exc_val, exc_tb):
        self.stops += 1


class _LifecycleClient(_ScriptedClient):
    """Lifecycle-enforcing fake: ``call_tool_async`` RAISES unless ``.start()`` ran.

    Mirrors strands 1.42 MCPClient semantics, where every session method raises
    ``MCPClientInitializationError`` while the client session is not active —
    the property that makes an unstarted rebuild dead code.
    """

    def __init__(self, results):
        super().__init__(results)
        self.started = False

    def start(self):
        self.started = True
        return self

    async def call_tool_async(self, *a, **k):
        if not self.started:
            raise RuntimeError(
                "MCPClientInitializationError: the client session is not running"
            )
        return await super().call_tool_async(*a, **k)


def test_persistent_auth_rebuilds_once_and_returns_fresh_success(recorded_sleeps, caplog):
    """Auth exhaustion -> rebuild_client called EXACTLY once -> the fresh client's
    success is returned, and the WARNING names the rebuild explicitly."""
    old = _ScriptedClient([AUTH_ERROR])  # stale bearer: fails every call, forever
    fresh = _ScriptedClient([SUCCESS])
    rebuilds: list[int] = []

    def _rebuild():
        rebuilds.append(1)
        return fresh

    orch._wrap_gateway_client_with_retry(old, rebuild_client=_rebuild)
    with caplog.at_level("WARNING"):
        out = asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))
    assert out == SUCCESS
    assert len(rebuilds) == 1
    assert fresh.calls == ["digital-thread___explore_graph"]  # retried ONCE on new client
    # The narrow marker + SHORT schedule are untouched: full 1s/3s exhaustion first.
    assert len(old.calls) == 1 + len(orch._GATEWAY_AUTH_RETRY_BACKOFFS_S)
    assert recorded_sleeps == list(orch._GATEWAY_AUTH_RETRY_BACKOFFS_S)
    assert old.stops == 1  # replaced stale-bearer client stopped best-effort
    assert any(
        "gateway MCP client rebuilt with fresh bearer after persistent auth failure"
        in r.getMessage()
        for r in caplog.records
    )


def test_successful_rebuild_adopts_fresh_client_for_subsequent_calls(recorded_sleeps):
    """After a successful rebuild the wrapper ADOPTS the fresh client: the next
    call goes straight to it (the stale client fails for life — never re-hit)."""
    old = _ScriptedClient([AUTH_ERROR])
    fresh = _ScriptedClient([SUCCESS])
    orch._wrap_gateway_client_with_retry(old, rebuild_client=lambda: fresh)

    async def _go():
        first = await old.call_tool_async(name="digital-thread___explore_graph")
        second = await old.call_tool_async(name="digital-thread___save_correlation")
        return first, second

    first, second = asyncio.run(_go())
    assert first == SUCCESS and second == SUCCESS
    assert len(old.calls) == 1 + len(orch._GATEWAY_AUTH_RETRY_BACKOFFS_S)  # only call 1
    assert fresh.calls == ["digital-thread___explore_graph", "digital-thread___save_correlation"]
    # Teardown: the replaced stale-bearer client is stopped best-effort after the
    # swap; the ADOPTED client stays running for the wrapper's remaining calls.
    assert old.stops == 1
    assert fresh.stops == 0


def test_rebuilt_client_second_failure_returns_original_error(recorded_sleeps):
    """If the fresh client's single retry ALSO fails, the ORIGINAL error result is
    returned unchanged (honest fallback — a genuine authz denial must surface)."""
    pristine = copy.deepcopy(AUTH_ERROR)
    old = _ScriptedClient([AUTH_ERROR])
    denial = {"status": "error", "content": [{"text": "AccessDeniedException: not authorized"}]}
    fresh = _ScriptedClient([denial])
    rebuilds: list[int] = []

    def _rebuild():
        rebuilds.append(1)
        return fresh

    orch._wrap_gateway_client_with_retry(old, rebuild_client=_rebuild)
    out = asyncio.run(old.call_tool_async(name="digital-thread___run_query"))
    assert out is AUTH_ERROR  # the ORIGINAL object, not the fresh client's error
    assert out == pristine  # ... and content-identical (nothing mutated)
    assert len(rebuilds) == 1 and len(fresh.calls) == 1
    # Teardown: the discarded (NOT adopted) fresh client is stopped best-effort;
    # the wrapped client keeps running (its lifecycle is Agent-owned).
    assert fresh.stops == 1
    assert old.stops == 0


def test_rebuild_raising_returns_original_error(recorded_sleeps):
    """A rebuild that raises (e.g. token mint failure) is absorbed: the original
    error result is returned unchanged, never an exception into the agent loop."""
    old = _ScriptedClient([AUTH_ERROR])

    def _rebuild():
        raise RuntimeError("mint failed")

    orch._wrap_gateway_client_with_retry(old, rebuild_client=_rebuild)
    out = asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))
    assert out is AUTH_ERROR


@pytest.mark.parametrize(
    "error",
    [
        APP_ERROR,  # genuine application error: no retry, no rebuild
        {"status": "error", "content": [{"text": "No endpoint or agent found"}]},  # transient class
    ],
)
def test_non_auth_errors_never_trigger_rebuild(recorded_sleeps, error):
    """Rebuild is AUTH-class only: app errors and cold-runtime transients (even on
    LONG-schedule exhaustion) must never mint tokens / rebuild the client."""
    old = _ScriptedClient([error])
    rebuilds: list[int] = []

    def _rebuild():
        rebuilds.append(1)
        return _ScriptedClient([SUCCESS])

    orch._wrap_gateway_client_with_retry(old, rebuild_client=_rebuild)
    out = asyncio.run(old.call_tool_async(name="query_x"))
    assert out is error
    assert rebuilds == []


def test_cooldown_prevents_second_rebuild_within_window(recorded_sleeps):
    """A hard authz denial (fresh client also errors -> no adoption) triggers at
    most ONE rebuild per cooldown window — no rebuild storm of token mints."""
    old = _ScriptedClient([AUTH_ERROR])
    rebuilds: list[int] = []

    def _rebuild():
        rebuilds.append(1)
        return _ScriptedClient([AUTH_ERROR])  # still failing: NOT adopted

    orch._wrap_gateway_client_with_retry(old, rebuild_client=_rebuild)

    async def _go():
        r1 = await old.call_tool_async(name="digital-thread___explore_graph")
        r2 = await old.call_tool_async(name="digital-thread___explore_graph")
        return r1, r2

    r1, r2 = asyncio.run(_go())
    assert r1 is AUTH_ERROR and r2 is AUTH_ERROR  # honest fallback both times
    assert len(rebuilds) == 1  # second exhaustion is inside the cooldown window


def test_rebuild_allowed_again_after_cooldown_expires(recorded_sleeps, monkeypatch):
    """With the cooldown elapsed (patched to 0), a later exhaustion may rebuild again."""
    monkeypatch.setattr(orch, "_GATEWAY_CLIENT_REBUILD_COOLDOWN_S", 0.0)
    old = _ScriptedClient([AUTH_ERROR])
    rebuilds: list[int] = []

    def _rebuild():
        rebuilds.append(1)
        return _ScriptedClient([AUTH_ERROR])

    orch._wrap_gateway_client_with_retry(old, rebuild_client=_rebuild)

    async def _go():
        await old.call_tool_async(name="digital-thread___explore_graph")
        await old.call_tool_async(name="digital-thread___explore_graph")

    asyncio.run(_go())
    assert len(rebuilds) == 2


def test_wrapper_without_rebuild_seam_unchanged(recorded_sleeps):
    """No rebuild_client (default) -> exhaustion behavior is EXACTLY pre-follow-up:
    original error surfaced after the short schedule, nothing else attempted."""
    old = _ScriptedClient([AUTH_ERROR])
    orch._wrap_gateway_client_with_retry(old)
    out = asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))
    assert out is AUTH_ERROR
    assert len(old.calls) == 1 + len(orch._GATEWAY_AUTH_RETRY_BACKOFFS_S)


# --------------------------------------------------------------------------- #
# LIFECYCLE (F1): the rebuilt client must be STARTED before the once-retry —
# strands 1.42 call_tool_async raises MCPClientInitializationError on an
# unstarted client, which would make the whole recovery dead code.
# --------------------------------------------------------------------------- #
def test_rebuilt_client_is_started_before_retry_end_to_end(recorded_sleeps, monkeypatch):
    """END-TO-END through the REAL seam: _rebuild_gateway_client must start() the
    client it builds. The fake enforces the strands-1.42 lifecycle (call raises
    unless started), so recovery succeeding at all proves start() ran first."""
    fresh = _LifecycleClient([SUCCESS])
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda config, tok: "fresh-bearer")
    monkeypatch.setattr(orch, "_create_mcp_client", lambda url, bearer_token=None: fresh)
    old = _ScriptedClient([AUTH_ERROR])
    orch._wrap_gateway_client_with_retry(
        old, rebuild_client=lambda: orch._rebuild_gateway_client(_Cfg())
    )
    try:
        out = asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))
    finally:
        orch.reset_service_token_cache()
    assert out == SUCCESS  # the feature actually recovers
    assert fresh.started is True  # started BEFORE the once-retry
    assert fresh.calls == ["digital-thread___explore_graph"]
    assert old.stops == 1  # replaced stale-bearer client torn down


def test_unstarted_rebuilt_client_cannot_recover_and_is_stopped(recorded_sleeps):
    """NEGATIVE CONTROL (proves the lifecycle pin is not vacuous) + the RAISE
    teardown branch: a seam that skips start() hands back a client whose call
    RAISES — the wrapper absorbs it, surfaces the ORIGINAL error, and stops the
    failed fresh client best-effort."""
    fresh = _LifecycleClient([SUCCESS])  # never started: call_tool_async raises
    old = _ScriptedClient([AUTH_ERROR])
    orch._wrap_gateway_client_with_retry(old, rebuild_client=lambda: fresh)
    out = asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))
    assert out is AUTH_ERROR  # original-error identity preserved on the raise path
    assert fresh.started is False
    assert fresh.stops == 1  # failed fresh client stopped, never leaked
    assert old.stops == 0  # wrapped client untouched (Agent-owned lifecycle)


# --------------------------------------------------------------------------- #
# TEARDOWN (F2): the ADOPTED client is stopped via the wrapped client's
# remove_consumer hook — the same provider-lifecycle event on which the Agent's
# tool registry tears the wrapped client down (ToolRegistry.cleanup ->
# provider.remove_consumer -> stop when no consumers remain).
# --------------------------------------------------------------------------- #
class _ProviderClient(_ScriptedClient):
    """Scripted fake with the MCPClient ToolProvider consumer surface."""

    def __init__(self, results):
        super().__init__(results)
        self._consumers = set()
        self.removed: list = []

    def add_consumer(self, consumer_id, **kwargs):
        self._consumers.add(consumer_id)

    def remove_consumer(self, consumer_id, **kwargs):
        self.removed.append(consumer_id)
        self._consumers.discard(consumer_id)


def test_adopted_client_stopped_when_registry_releases_wrapper(recorded_sleeps):
    """Adoption registers the fresh client on the wrapper's remove_consumer hook:
    when the LAST consumer is removed (the Agent registry teardown path), the
    adopted client is stopped best-effort. The underlying remove_consumer still
    runs (delegation preserved)."""
    old = _ProviderClient([AUTH_ERROR])
    old.add_consumer("registry-1")
    fresh = _ScriptedClient([SUCCESS])
    orch._wrap_gateway_client_with_retry(old, rebuild_client=lambda: fresh)

    out = asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))
    assert out == SUCCESS and fresh.stops == 0  # adopted + still running

    old.remove_consumer("registry-1")  # the Agent registry's cleanup event
    assert old.removed == ["registry-1"]  # underlying remove_consumer delegated
    assert fresh.stops == 1  # adopted client torn down with the wrapper


def test_adopted_client_kept_while_consumers_remain(recorded_sleeps):
    """The hook mirrors MCPClient's own zero-consumer condition: removing ONE of
    two consumers must NOT stop the adopted client; removing the last one must."""
    old = _ProviderClient([AUTH_ERROR])
    old.add_consumer("registry-1")
    old.add_consumer("registry-2")
    fresh = _ScriptedClient([SUCCESS])
    orch._wrap_gateway_client_with_retry(old, rebuild_client=lambda: fresh)
    asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))

    old.remove_consumer("registry-1")
    assert fresh.stops == 0  # a consumer remains — adopted client stays up
    old.remove_consumer("registry-2")
    assert fresh.stops == 1  # last consumer gone — adopted client stopped


def test_remove_consumer_without_adoption_is_pure_delegation(recorded_sleeps):
    """No rebuild happened -> the hook only delegates; nothing is stopped."""
    old = _ProviderClient([SUCCESS])
    old.add_consumer("registry-1")
    orch._wrap_gateway_client_with_retry(old, rebuild_client=lambda: _ScriptedClient([SUCCESS]))
    asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))
    old.remove_consumer("registry-1")
    assert old.removed == ["registry-1"]
    assert old.stops == 0


def test_stop_failures_are_swallowed(recorded_sleeps, caplog):
    """Teardown is best-effort: a stop() that raises must not mask the tool
    result — the fresh success is still returned and a DEBUG record is logged."""
    class _ExplodingStop(_ScriptedClient):
        def stop(self, exc_type, exc_val, exc_tb):
            super().stop(exc_type, exc_val, exc_tb)
            raise RuntimeError("boom on stop")

    old = _ExplodingStop([AUTH_ERROR])
    fresh = _ScriptedClient([SUCCESS])
    orch._wrap_gateway_client_with_retry(old, rebuild_client=lambda: fresh)
    with caplog.at_level("DEBUG", logger=orch.logger.name):
        out = asyncio.run(old.call_tool_async(name="digital-thread___explore_graph"))
    assert out == SUCCESS  # the exploding stop never surfaced
    assert old.stops == 1  # stop was attempted (then swallowed)
    assert any("best-effort stop" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# _rebuild_gateway_client: service-token cache invalidated -> FRESH mint used
# --------------------------------------------------------------------------- #
class _Cfg:
    aws_region = "us-east-1"
    cognito_user_pool_id = "us-east-1_EXAMPLE"
    agentcore_gateway_url = "https://gw.example.com/mcp"


def test_rebuild_gateway_client_invalidates_cache_and_mints_fresh(monkeypatch):
    """The rebuild seam must NOT reuse the cached (possibly-stale) service token:
    it invalidates the token cache first, so _fetch_service_token takes the
    fresh-mint path and the NEW client is built with the freshly minted bearer —
    and the client is STARTED before being returned (F1)."""
    monkeypatch.setenv(orch.SERVICE_CLIENT_ID_ENV, "service-client-abc")
    monkeypatch.delenv(orch.SERVICE_POOL_ID_ENV, raising=False)
    # Seed a stale cached token FAR from expiry — without invalidation,
    # _fetch_service_token would return it untouched (no mint).
    monkeypatch.setattr(orch, "_service_token", "stale-token")
    monkeypatch.setattr(orch, "_service_token_expires_at", time.monotonic() + 3600.0)
    monkeypatch.setattr(orch, "_service_client_secret_for", lambda *a, **k: "sekrit")
    monkeypatch.setattr(orch, "_service_domain_account", lambda cfg: "111122223333")
    mints: list[dict] = []

    def _mint(**kwargs):
        mints.append(kwargs)
        return ("fresh-token", 3600)

    monkeypatch.setattr(orch, "mint_client_credentials_token", _mint)
    captured: dict = {}
    built = _LifecycleClient([SUCCESS])

    def _fake_create(url, use_sigv4=False, bearer_token=None):
        captured["url"] = url
        captured["bearer"] = bearer_token
        return built

    monkeypatch.setattr(orch, "_create_mcp_client", _fake_create)
    try:
        returned = orch._rebuild_gateway_client(_Cfg())
        assert len(mints) == 1  # fresh-mint path ran (cache was invalidated first)
        assert captured["bearer"] == "fresh-token"  # NOT the stale cached token
        assert captured["url"] == _Cfg.agentcore_gateway_url
        assert returned is built
        assert built.started is True  # F1: the seam start()s the client it builds
    finally:
        orch.reset_service_token_cache()


# --------------------------------------------------------------------------- #
# TOKEN GATE (F3): _get_orchestrator_tools wires the rebuild seam ONLY for
# service-token turns. A forwarded end-user token is per-request by
# construction — a rebuild would reuse the same rejected bearer, so those turns
# skip the seam entirely (no pointless mint+start; original error surfaces).
# --------------------------------------------------------------------------- #
def _gateway_tools_call(monkeypatch, user_access_token):
    """Run _get_orchestrator_tools in gateway mode with the wrap seam captured."""
    from types import SimpleNamespace

    captured: dict = {}
    monkeypatch.setattr(
        orch, "get_config",
        lambda: SimpleNamespace(has_gateway=True, agentcore_gateway_url="https://gw.example.com/mcp"),
    )
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda config, tok: "bearer-token")
    monkeypatch.setattr(orch, "_create_mcp_client", lambda url, bearer_token=None: object())

    def _capture_wrap(client, rebuild_client=None):
        captured["rebuild_client"] = rebuild_client
        return client

    monkeypatch.setattr(orch, "_wrap_gateway_client_with_retry", _capture_wrap)
    monkeypatch.setattr(orch, "_wrap_gateway_client_exclude_guidance", lambda c: c)
    orch._get_orchestrator_tools(user_access_token=user_access_token)
    return captured


def test_forwarded_user_token_turn_gets_no_rebuild_seam(monkeypatch):
    """Interactive turn (forwarded end-user token): rebuild_client is None —
    the wrapper surfaces the original error on auth exhaustion, and no service
    token is ever invalidated or minted for a path that cannot heal."""
    captured = _gateway_tools_call(monkeypatch, user_access_token="user-access-token")  # nosec B106 - offline test fixture/placeholder credential, not a live secret
    assert captured["rebuild_client"] is None


def test_service_token_turn_gets_rebuild_seam_wired_to_rebuild(monkeypatch):
    """Background turn (service token): the seam is wired and delegates to
    _rebuild_gateway_client(config)."""
    captured = _gateway_tools_call(monkeypatch, user_access_token=None)
    seam = captured["rebuild_client"]
    assert callable(seam)
    sentinel = object()
    rebuild_args: list = []

    def _fake_rebuild(config):
        rebuild_args.append(config)
        return sentinel

    monkeypatch.setattr(orch, "_rebuild_gateway_client", _fake_rebuild)
    assert seam() is sentinel
    assert len(rebuild_args) == 1  # service-path rebuild, config-only signature
