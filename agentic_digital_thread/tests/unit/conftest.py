# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared unit-test fixtures."""

from __future__ import annotations

import threading

import pytest

#: The onboarding chat's background jobs (``onboard_chat_lifecycle._start``:
#: the provision; ``onboard.start_removal_for_chat``: the removal).
_CHAT_JOB_THREADS = ("chat-provision-", "chat-removal-")


@pytest.fixture(autouse=True)
def _chat_jobs_end_with_their_test(monkeypatch):
    """A chat job a test started ends before the test's fakes are undone, so
    it never runs on into the next test: there it would still hold the
    process-wide provision claim, and write its terminal status to the next
    test's session store. Torn down before ``monkeypatch`` (it depends on
    it). A job is given 60 s, for a slow, loaded runner."""
    yield
    for t in [t for t in threading.enumerate() if t.name.startswith(_CHAT_JOB_THREADS)]:
        t.join(60)
        assert not t.is_alive(), f"the chat job {t.name} did not end with its test"


@pytest.fixture(autouse=True)
def _core_concept_topup_ends_with_its_test(monkeypatch):
    """A test that runs the startup seed (``server._restore``, the seed
    itself) with writes that fail starts the background core-concept top-up
    (``server._CoreConceptTopUp``). It is stopped and its thread joined
    before the test's fakes are undone, so it never sends into the next
    test's fakes or logs into its captured log, and the next test gets a
    top-up of its own (one runs per process) and no concept held by its
    timed-out writes (``server._CORE_CONCEPT_HELD``). Only when the test
    loaded ``agents.server``; 30 s for a slow, loaded runner."""
    yield
    import sys
    server = sys.modules.get("agents.server")
    topup = getattr(server, "_core_concept_topup", None)
    if topup is None:
        return
    (getattr(topup, "shut_down", None) or topup.stop.set)()
    if topup.thread is not None and topup.thread.ident is not None:  # started
        topup.thread.join(30)
        assert not topup.thread.is_alive(), "the core-concept top-up did not end with its test"
    getattr(server, "_CORE_CONCEPT_HELD", {}).clear()
    server._core_concept_topup = server._CoreConceptTopUp()


class _OfflineSessionStore:
    """An empty onboarding session store: no system has a session, and a
    delete is a no-op. Only the calls a removal makes; any other call fails
    loudly, so a test that needs more fakes the store itself."""

    def get_session(self, system_id, **_read):
        from agents.onboarding_session import SessionNotFoundError
        raise SessionNotFoundError(f"offline unit lane: no session for {system_id!r}")

    def delete_session(self, system_id):
        return None


@pytest.fixture(autouse=True)
def _offline_onboarding_session_store(monkeypatch):
    """A system's removal (``onboard._run_reset_cascade``, Reset All's
    ``_global_reset_cascade``) deletes the system's onboarding session through
    ``onboard_agent._store``. A removal test that does not fake the store
    would build the real DynamoDB store and reach an AWS endpoint; give it an
    empty in-memory one instead. Likewise the chat tools' guard in the
    onboarding runtime container reads the reset lease from the shared table
    (``onboard_agent._shared_reset_jobs``, strict reads): here a reset store
    over an empty in-memory table. And every test gets its own, empty,
    memory-only lease store (``onboard._RESET_LEASES``), so a lease one test
    leaves never holds another off. Tests that fake any of them re-patch it
    (test-level monkeypatch wins over this autouse patch)."""
    try:
        from agents import onboard, onboard_agent
        from agents.job_store import JobStore
        from tests.unit._shared_table import SharedTable
    except Exception:
        yield
        return
    monkeypatch.setattr(onboard_agent, "_store", _OfflineSessionStore)
    jobs = JobStore("reset", table=SharedTable())
    monkeypatch.setattr(onboard_agent, "_shared_reset_jobs", lambda: jobs, raising=False)
    monkeypatch.setattr(onboard, "_RESET_LEASES", JobStore("reset", table=None), raising=False)
    monkeypatch.setattr(onboard, "_PROVISION_LEASES", {}, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_dashboard_cards_cache():
    """The dashboard resolves cards into a process-wide TTL cache, and every
    expensive PANEL read shares a process-wide bounded TTL cache (S5.4 cost
    control + the 2026-08-24 per-viewer fix). Reset BOTH around every test so a
    value produced under one test's mocked resolver/composer never leaks into
    another test's route call."""
    try:
        from agents import dashboard
    except Exception:
        yield
        return
    dashboard.invalidate_dashboard_caches()
    yield
    dashboard.invalidate_dashboard_caches()


@pytest.fixture(autouse=True)
def _forget_split_calls_left_running():
    """An exact split tracks the calls it stopped waiting for in the process,
    and bounds every call it starts with process-wide permits
    (``athena_exact_split.MAX_SPLIT_CALLS``). A call or permit a test left
    behind must not refuse or hold up a later test's split."""
    yield
    import sys
    module = sys.modules.get("agents.athena_exact_split")
    clear = getattr(module, "clear_abandoned", None)
    if clear is not None:
        clear()


@pytest.fixture(autouse=True)
def _reset_name_lookup_pause():
    """A failed graph name lookup pauses further lookups in the process for
    ``id_name_resolver.FAILURE_PAUSE_S``. The offline lane has no graph, so
    any test that renders an id would start that pause; reset it around every
    test so a later test's faked graph is still asked."""
    try:
        from agents import id_name_resolver
    except Exception:
        yield
        return
    id_name_resolver._PAUSED_UNTIL = 0.0
    yield
    id_name_resolver._PAUSED_UNTIL = 0.0


@pytest.fixture(autouse=True)
def _reset_aws_session_cache():
    """The cross-account helper caches assembled assumed-role sessions per
    ``(role_arn, external_id, region)`` (cost control). Reset it around every test
    so a session assumed under one test's mocked STS never satisfies another
    test's call (which would skip the expected AssumeRole / log line)."""
    try:
        from agents import aws_session
    except Exception:
        yield
        return
    aws_session._reset_session_cache()
    yield
    aws_session._reset_session_cache()


@pytest.fixture(autouse=True)
def _isolate_known_systems_source(monkeypatch):
    """Keep ``self_learning.known_systems()`` hermetic in the offline lane.

    F6 gave the ingestion gates a Neptune fallback when the in-memory catalog
    is empty (the AgentCore runtime process). In the unit lane that fallback
    must NEVER reach a real graph: patch the dedicated
    ``_known_systems_graph_read`` seam to raise (→ ``known_systems`` returns
    ``[]``, the exact pre-F6 empty-catalog store-raw behavior) and reset the
    module-global TTL cache around every test so one test's cached result
    never leaks into another. Tests that exercise the graph path re-patch the
    seam themselves (test-level monkeypatch wins over this autouse patch)."""
    try:
        from agents import self_learning
    except Exception:
        yield
        return
    self_learning._reset_known_systems_cache()
    self_learning._reset_accepted_coverage_cache()
    from agents import value_promotion
    value_promotion._reset_auto_promotion_state()
    monkeypatch.setattr(
        self_learning, "_known_systems_graph_read",
        lambda: (_ for _ in ()).throw(RuntimeError("offline unit lane: no graph")),
    )
    yield
    self_learning._reset_known_systems_cache()
    self_learning._reset_accepted_coverage_cache()
    value_promotion._reset_auto_promotion_state()


@pytest.fixture(autouse=True)
def _offline_field_descriptions_read(monkeypatch):
    """A startup restore and a connection resync rebuild a system's
    described-field record from the graph (``field_descriptions._graph_read``).
    The offline lane has no graph, so that read raises here, as an unreadable
    graph does, and the record falls back to what the agent-config holds;
    it never reaches a real graph. The read breaker is reset around every
    test, so one test's failed read never skips another's. Tests of the read
    re-patch the seam (test-level monkeypatch wins over this autouse patch)."""
    try:
        from agents import field_descriptions
    except Exception:
        yield
        return

    def _no_graph(cypher, parameters=None):
        raise RuntimeError("offline unit lane: no graph")
    monkeypatch.setattr(field_descriptions, "_graph_read", _no_graph)
    reset = getattr(field_descriptions, "reset", None)
    if reset:
        reset()          # each test starts with the read breaker closed
    yield
    if reset:
        reset()


@pytest.fixture(autouse=True)
def _offline_checked_node_pointer(monkeypatch):
    """The offline lane has no graph, so the CHECKED graph read
    (``db.neptune_query_checked``) would always raise. Fake that I/O boundary
    for exactly four queries, the System-node reads the REAL
    ``connection_resolver.node_secret_arn_checked`` (the PostgreSQL origin
    decision), ``connection_resolver.node_state_checked`` ("not yet
    provisioned"), ``connection_resolver.node_connection_checked`` (the
    credential store) and ``onboard_agent._removal_failed_refusal`` (the chat
    tools' removal guard) issue: answer them from the UNCHECKED node read
    ``connection_resolver._fetch_node_config`` (looked up at call time, so a
    test that fakes the graph through that seam gets the same node here), ``[]``
    when there is no node. Every other query goes to the real
    ``neptune_query_checked`` unchanged. The functions under test are never
    replaced: a broken alias or key in them fails the tests. Tests of the
    checked reads' failure modes re-patch ``db.neptune_query_checked``
    (test-level monkeypatch wins)."""
    try:
        from agents import connection_resolver as cr
        from agents import db
    except Exception:
        yield
        return
    real = db.neptune_query_checked
    pointer_query = "MATCH (s:System {id: $sid}) RETURN s.secret_arn AS secret_arn"
    state_query = ("MATCH (s:System {id: $sid}) "
                   "RETURN s.secret_arn AS secret_arn, s.connection_config AS connection_config")
    # onboard_agent._REMOVAL_MARKER_QUERY (the chat tools' removal guard).
    marker_prefix = ("MATCH (s:System {id: $sid}) RETURN s.removal_incomplete "
                     "AS removal_incomplete")
    # connection_resolver.node_connection_checked (the credential store's read).
    connection_query = getattr(cr, "_NODE_CONNECTION_QUERY", None)
    # connection_resolver._fetch_node_config_checked (the durable-record
    # snapshot read): the full non-secret record.
    snapshot_query = getattr(cr, "_NODE_SNAPSHOT_QUERY", None)
    # record_key.take_for_rewrite (the re-provision's record-key snapshot, a
    # CHECKED read before the schema delete). The offline lane holds no record
    # key on a System node, so answer "no key": a test that declares one fakes
    # the graph at its own route/store seams.
    try:
        from agents import record_key as _rk
        record_key_raw_query = getattr(_rk, "_READ_RAW_RECORD_KEY", None)
        record_key_carry_query = getattr(_rk, "_READ_REWRITE_CARRY", None)
    except Exception:
        record_key_raw_query = None
        record_key_carry_query = None

    def _checked(cypher, parameters=None):
        query = cypher.strip()
        if record_key_raw_query and query == record_key_raw_query:
            return [{}]
        if record_key_carry_query and query == record_key_carry_query:
            return [{}]                       # no record-key rewrite carry in flight
        if snapshot_query and query == snapshot_query:
            node = cr._fetch_node_config((parameters or {}).get("sid", ""))
            return [{k: node.get(k) for k in ("connection_config", "secret_arn",
                                              "connection_type", "connection_string")}] \
                if node else []
        if query.startswith(marker_prefix):
            node = cr._fetch_node_config((parameters or {}).get("sid", ""))
            return [{"removal_incomplete": node.get("removal_incomplete"),
                     "removal_full": node.get("removal_full"),
                     "status": node.get("status"),
                     "connection_config": node.get("connection_config"),
                     "secret_arn": node.get("secret_arn")}] if node else []
        if connection_query and query == connection_query:
            node = cr._fetch_node_config((parameters or {}).get("sid", ""))
            return [{k: node.get(k) for k in ("status", "connection_type",
                                              "connection_origin", "connection_config",
                                              "secret_arn")}] if node else []
        if query not in (pointer_query, state_query):
            return real(cypher, parameters)
        node = cr._fetch_node_config((parameters or {}).get("sid", ""))
        if not node:
            return []
        if query == pointer_query:
            return [{"secret_arn": node.get("secret_arn")}]
        return [{"secret_arn": node.get("secret_arn"),
                 "connection_config": node.get("connection_config")}]
    monkeypatch.setattr(db, "neptune_query_checked", _checked)
    yield


@pytest.fixture(autouse=True)
def _offline_removal_node_read(monkeypatch):
    """A default removal reads the kept registrations' System nodes CHECKED
    (``onboard._keep_registration_connections``), and a failed read stops the
    removal of a user registration. The offline lane has no graph, so answer
    exactly that one query from the UNCHECKED ``onboard.neptune_query``,
    looked up at call time: a test that fakes the graph through that seam
    gets the same nodes here, and one that doesn't gets none (nothing to
    copy). Every other query goes to the real ``neptune_query_checked``
    unchanged. Tests of the read's failure re-patch
    ``onboard.neptune_query_checked`` (test-level monkeypatch wins). Applied
    only once ``agents.onboard`` is imported, so it adds no import."""
    import sys
    onboard = sys.modules.get("agents.onboard")
    query = getattr(onboard, "_KEEP_CONNECTION_READ", None)
    if onboard is None or not query:
        yield
        return
    real = onboard.neptune_query_checked
    # record_key.take_for_rewrite's CHECKED snapshot read (through onboard's own
    # neptune_query_checked); the offline lane holds no record key, so answer
    # "no key" rather than falling through to the real graph.
    try:
        from agents import record_key as _rk
        record_key_raw_query = getattr(_rk, "_READ_RAW_RECORD_KEY", None)
        record_key_carry_query = getattr(_rk, "_READ_REWRITE_CARRY", None)
    except Exception:
        record_key_raw_query = None
        record_key_carry_query = None

    def _checked(cypher, parameters=None):
        if cypher == query:
            return onboard.neptune_query(cypher, parameters) or []
        if record_key_raw_query and cypher.strip() == record_key_raw_query:
            return [{}]
        if record_key_carry_query and cypher.strip() == record_key_carry_query:
            return [{}]                       # no record-key rewrite carry in flight
        return real(cypher, parameters)
    monkeypatch.setattr(onboard, "neptune_query_checked", _checked)
    yield


@pytest.fixture(autouse=True)
def _offline_own_secret_snapshot(monkeypatch, request):
    """The durable snapshot now reads this system's own secret CHECKED
    (``connection_resolver._own_secret_current_version``): a DescribeSecret
    failure that is NOT a confirmed ResourceNotFound raises DurableSnapshotError
    The offline lane has no Secrets Manager, so DescribeSecret
    would raise a non-absent client error and every re-provision test would
    abort. Default the version read to confirmed-absent (``None``), so a test
    that does not set up a secret snapshots as "no own secret"; the secret-aware
    tests re-patch ``_secrets_client`` and opt out with the
    ``no_offline_secret_stub`` marker so they exercise the real version read.
    Applied only once the module is imported, so it adds no import."""
    import sys
    cr = sys.modules.get("agents.connection_resolver")
    if cr is None or request.node.get_closest_marker("no_offline_secret_stub"):
        yield
        return
    monkeypatch.setattr(cr, "_own_secret_current_version", lambda system_id: None)
    yield


@pytest.fixture(autouse=True)
def _offline_target_field_snapshot(monkeypatch):
    """Provision's re-measurement of a measured rule reads its target field
    CHECKED (``onboard._TARGET_FIELD_SNAPSHOT_CYPHER``: the field, its
    concepts and its system's schema-rewrite mark in one read). The offline
    lane has no graph, so answer exactly that query from the UNCHECKED
    ``onboard.neptune_query``, looked up at call time: a test that fakes the
    graph through that seam answers it there, and one that doesn't gets no
    row (no System node). Every other query goes on to the real checked read.
    Tests of the read's failure re-patch ``onboard.neptune_query_checked``
    (test-level monkeypatch wins)."""
    import sys
    onboard = sys.modules.get("agents.onboard")
    query = getattr(onboard, "_TARGET_FIELD_SNAPSHOT_CYPHER", None)
    if onboard is None or not query:
        yield
        return
    real = onboard.neptune_query_checked

    def _checked(cypher, parameters=None):
        if cypher == query:
            return onboard.neptune_query(cypher, parameters) or []
        return real(cypher, parameters)
    monkeypatch.setattr(onboard, "neptune_query_checked", _checked)
    yield


@pytest.fixture(autouse=True)
def _offline_schema_rewrite_mark(monkeypatch):
    """b0 writes the schema-rewrite mark with a RETURNING write
    (``onboard._SCHEMA_REWRITE_MARK_CYPHER``) and stops the provision before
    its schema delete unless the write returns this run's token. The offline
    lane has no graph, so answer exactly that statement from the UNCHECKED
    ``onboard.neptune_write``, looked up at call time: a test that fakes the
    graph through that seam gets the token back when its write succeeds and
    ``None`` (a refused write) when it does not. Every other statement goes to
    the real ``neptune_write_returning``. Tests of the mark's own failure
    modes re-patch ``onboard.neptune_write_returning`` (test-level wins)."""
    import sys
    onboard = sys.modules.get("agents.onboard")
    statement = getattr(onboard, "_SCHEMA_REWRITE_MARK_CYPHER", "")
    if onboard is None or "RETURN" not in statement:
        yield
        return
    real = onboard.neptune_write_returning

    def _returning(cypher, parameters=None):
        if cypher == statement:
            if onboard.neptune_write(cypher, parameters) is False:
                return None
            return [{"started": (parameters or {}).get("started")}]
        return real(cypher, parameters)
    monkeypatch.setattr(onboard, "neptune_write_returning", _returning)
    yield


@pytest.fixture(autouse=True)
def _offline_schema_rewrite_states(monkeypatch):
    """The schema-rewrite mark and failed marker are also kept in a shared
    JobStore (``onboard._SCHEMA_REWRITE_STATES``, ``schema_rewrite_state``).
    Every test gets its own, empty, memory-only one, so a mark or failed
    marker one test leaves never holds another off. Tests that fake it
    re-patch it (test-level monkeypatch wins)."""
    import sys
    onboard = sys.modules.get("agents.onboard")
    if onboard is None or not hasattr(onboard, "_SCHEMA_REWRITE_STATES"):
        yield
        return
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", JobStore("schema-rewrite", table=None))
    yield


# _offline_aws_env MOVED to tests/conftest.py so it covers the WHOLE unit lane.
# The CI command names tests/test_orchestrator_memory.py explicitly, which sits
# OUTSIDE tests/unit/ and therefore never saw this fixture: a boto3 client built
# there reached the real IMDS endpoint and failed the socket-disabled lane in CI
# while passing locally on ambient credentials (2026-07-30). A parent conftest
# still applies to this directory, so unit tests are unaffected.

@pytest.fixture(autouse=True)
def _clear_tulip_secret_cache():
    """system_agent's Tulip secret cache is process-global (the resolver's
    45 s TTL); a value one test's fake Secrets Manager returned must never be
    served to another test."""
    try:
        from agents import system_agent
    except Exception:
        yield
        return
    from agents import resolve_evidence
    system_agent._tulip_secret_cache.clear()
    resolve_evidence._tulip_seat_miss.clear()   # an unavailable seat, remembered for 45 s
    yield
    system_agent._tulip_secret_cache.clear()
    resolve_evidence._tulip_seat_miss.clear()


@pytest.fixture(autouse=True)
def _clear_entity_render_hot_read_memo():
    """entity_render's C2b memo is process-global (15s TTL) — a stale entry
    from one test's monkeypatched seam must never leak into another test."""
    try:
        from agents import entity_render
    except Exception:
        yield
        return
    entity_render._hot_read_memo.clear()
    yield
    entity_render._hot_read_memo.clear()


#: The route-seed budget the pressure tests were built and measured under
#: (ADR-0035 amendment 2026-10-08). The production budget is 16,000 since
#: 2026-10-17; at that budget their graphs no longer overrun it, so tests that
#: exercise the trimming passes (not the number) run under this one.
ROUTE_SEED_PRESSURE_BUDGET = 10_000


@pytest.fixture
def route_seed_pressure_budget(monkeypatch):
    """Run a test under :data:`ROUTE_SEED_PRESSURE_BUDGET`."""
    from agents import orchestrator
    monkeypatch.setattr(orchestrator, "_ROUTE_SEED_SERIALIZED_BUDGET",
                        ROUTE_SEED_PRESSURE_BUDGET)
    return ROUTE_SEED_PRESSURE_BUDGET
