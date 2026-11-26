# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lane-wide fixtures — apply to EVERY test the unit lane collects.

This file exists because the lane is not just ``tests/unit``: the documented
command also names ``tests/test_orchestrator_memory.py``, which a
``tests/unit/conftest.py`` fixture cannot reach. Anything that must hold for the
whole lane belongs HERE; directory-specific fixtures stay in the subdirectory.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline_aws_env(monkeypatch):
    """Force the unit lane offline at the AWS-SDK layer.

    Some tests construct real boto3 clients through un-stubbed seams. Without
    credentials, botocore falls back to the IMDS/endpoint chain with connect
    timeouts and retries — each leaked call burns seconds of wall time at 0%
    CPU (observed: a CLOSE_WAIT socket to an AWS endpoint mid-lane, inflating
    the ~12-minute suite to hours). Fake creds + disabled metadata + one
    attempt + short timeouts turn any residual leak into a fast, visible
    failure instead of a silent multi-second stall. Tests that legitimately
    exercise client-construction logic still work (construction is local);
    nothing in the unit lane may complete a real AWS call by design.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # noqa: S105 — fake, offline lane
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("AWS_RETRY_MODE", "standard")
    # Belt and suspenders: make sure no ambient profile is picked up.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    yield


@pytest.fixture(autouse=True)
def _offline_concept_binding_reads(monkeypatch):
    """Keep the ADR-0067 concept-binding graph read offline lane-wide.

    ``response_integrity_validation._shadow_concept_check`` resolves server-derived
    bindings through ``concept_bindings._field_concepts_graph_read`` whenever a
    claim proposes a concept id. In the offline lane that seam must never reach a
    real graph: patch it to raise (⇒ no rows ⇒ bindings stay ``unresolved`` ⇒ the
    verdict is honestly ``unknown``, the exact pre-hook behavior) and reset the
    process-global cache/breaker around every test so one test's rows never leak
    into another. Tests that exercise the read re-patch the seam themselves
    (test-level monkeypatch wins over this autouse patch).
    """
    try:
        from agents import concept_bindings
    except Exception:
        yield
        return
    concept_bindings.reset()
    monkeypatch.setattr(
        concept_bindings, "_field_concepts_graph_read",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("offline unit lane: no graph")),
    )
    yield
    concept_bindings.reset()


@pytest.fixture(autouse=True)
def _offline_concept_mapping_state(monkeypatch):
    """Keep the concept-link retirement ledger and the system agent's live
    concept-mapping read offline lane-wide.

    A provision reads which concept links a steward retired and STOPS when
    that ledger is unreadable (``self_learning.accepted_retired_concept_links``
    raises), so every test that provisions without a proposal store would
    fail on, and spend seconds in, a real DynamoDB call: the store seam
    ``_retirement_rows`` answers an empty ledger here. Likewise a provision
    is not reported provisioned until its mapping-generation bump reads back
    (``onboard._mapping_generation_bumped``): the bump seam
    ``onboard._bump_and_read_generation`` answers a confirmed bump here. The
    live mapping read (``live_concept_mappings._graph_read``) is patched to
    raise and its process-global state is reset around every test. Tests
    that exercise any of these seams re-patch it themselves (test-level
    monkeypatch wins)."""
    try:
        from agents import live_concept_mappings, onboard, self_learning
    except Exception:
        yield
        return
    live_concept_mappings.reset()
    monkeypatch.setattr(self_learning, "_retirement_rows", lambda: [])
    monkeypatch.setattr(onboard, "_bump_and_read_generation", lambda _sid: (1, 1))
    monkeypatch.setattr(
        live_concept_mappings, "_graph_read",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("offline unit lane: no graph")))
    yield
    live_concept_mappings.reset()


@pytest.fixture(autouse=True)
def _offline_row_exclusion_graph(monkeypatch):
    """Keep the row-exclusion graph reads offline lane-wide.

    Every system agent bake, and the re-read before a guarded Athena query,
    reads the system's row-exclusion manifest and then each table it lists
    through the CHECKED graph read (``row_exclusion._graph_query``). A read
    that fails, or answers no row, is UNKNOWN, which holds reads back: an
    unknown fact is never taken for no fact. The offline lane has no graph, so
    the seam answers the way a graph with no row exclusion stored does: a
    matched row whose property is null (``[{"row_exclusion": None}]``; for a
    batched read, one such row per SchemaEntry asked about), never ``[]``,
    which is not "none stored" but "not found". The System node's manifest
    property is then null too: no table of the system carries one. A
    re-provision's carry of the row exclusions across its schema rewrite
    (``row_exclusion._provision_graph``) reads the same answer, so it finds
    nothing listed; its write seam answers the rewrite marker's
    compare-and-set (which the stateless read never shows back, so the
    restore finds no marker and writes nothing more) and raises for any
    other write. The process-global registry is reset around every test.
    Tests that exercise the read re-patch the seam themselves (test-level
    monkeypatch wins)."""
    try:
        from agents import row_exclusion
        from agents import row_exclusion_manifest
    except Exception:
        yield
        return

    def _no_row_exclusion_stored(_cypher, params=None):
        seids = (params or {}).get("seids")
        if seids is not None:
            return [{"seid": seid, "row_exclusion": None} for seid in seids]
        return [{"row_exclusion": None}]

    def _marker_write_only(cypher, params=None):
        if cypher == row_exclusion_manifest.SWAP_MANIFEST:
            return [{"id": (params or {}).get("sid")}]
        raise RuntimeError("offline unit lane: no graph write for the row exclusion carry")

    row_exclusion.reset()
    monkeypatch.setattr(row_exclusion, "_graph_query", lambda: _no_row_exclusion_stored)
    monkeypatch.setattr(row_exclusion, "_provision_graph",
                        lambda: (_marker_write_only, _no_row_exclusion_stored))
    yield
    row_exclusion.reset()


@pytest.fixture(autouse=True)
def _offline_deployment_account(monkeypatch):
    """Keep the deployment-account lookup offline lane-wide.

    ``connection_resolver.refuse_foreign_account_secret`` compares a secret
    ARN's account with ``deployment_account_id`` (``AWS_ACCOUNT_ID``, else an
    STS GetCallerIdentity) and refuses every full-ARN read while the account
    is unknown. In the offline lane the STS seam answers the fixtures'
    documentation account ``111122223333`` (no network), and the process
    cache is reset around every test. Tests of the comparison set
    ``AWS_ACCOUNT_ID`` or re-patch ``_sts_account_id`` themselves (test-level
    monkeypatch wins).
    """
    try:
        from agents import connection_resolver as cr
    except Exception:
        yield
        return
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    cr._ACCOUNT_CACHE.update(value=None, until=0.0)
    monkeypatch.setattr(cr, "_sts_account_id", lambda: "111122223333")
    yield
    cr._ACCOUNT_CACHE.update(value=None, until=0.0)


@pytest.fixture(autouse=True)
def _metric_turn_notice_off(monkeypatch):
    """Keep the ADR-0078 per-turn metric-layer resolve OFF lane-wide.

    ``AgentCoreChatClient._stream_impl`` resolves any DEFINED metric a question
    names through the real metric layer (graph + source SQL). Thousands of
    chat-client tests stream canned questions without stubbing that layer;
    with the hook on they would reach the (offline-blocked) graph and grow a
    ``metricLayerResults`` payload key they never asserted. The kill switch
    keeps every such payload byte-identical; the ADR-0078 tests set
    ``METRIC_TURN_NOTICE=on`` themselves (test-level monkeypatch wins).
    """
    monkeypatch.setenv("METRIC_TURN_NOTICE", "off")
    yield


@pytest.fixture
def local_auth_env(monkeypatch):
    """Enable the complete local-only authentication bypass contract explicitly."""
    for marker in (
        "ECS_CONTAINER_METADATA_URI",
        "ECS_CONTAINER_METADATA_URI_V4",
        "AWS_EXECUTION_ENV",
        "COGNITO_USER_POOL_ID",
        "AGENTCORE_GATEWAY_URL",
        "ONBOARDING_GATEWAY_URL",
    ):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("AGENT_MODE", "local")

    from agents.config import get_config
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_athena_schema_cache():
    """The per-process Athena schema cache (athena_tools._SCHEMA_CACHE) must
    never leak an ``information_schema.columns`` read between tests — many
    Athena tests stub ``run_athena_query`` with different rows for the SAME
    table identity."""
    from agents import athena_tools
    athena_tools.clear_schema_cache()
    yield
    athena_tools.clear_schema_cache()


@pytest.fixture(autouse=True)
def _isolated_athena_layout_cache():
    """The exact split keeps ``$files`` layout rows per (table, snapshot,
    SQL) in the process (``athena_exact_split._LAYOUT_CACHE``). The split
    fakes share one table name and snapshot id across tests with different
    files, so the cache is emptied around every test."""
    from agents import athena_exact_split
    athena_exact_split.clear_layout_cache()
    yield
    athena_exact_split.clear_layout_cache()


@pytest.fixture(autouse=True)
def _isolated_sample_memo():
    """``resolve_evidence.sample_field_values`` memoizes a field's sample
    process-wide for a short window. Many tests stub the Postgres/Athena
    seams with different rows for the SAME system/table/field, so the memo is
    emptied around every test."""
    from agents import resolve_evidence
    resolve_evidence._reset_sample_memo()
    yield
    resolve_evidence._reset_sample_memo()


@pytest.fixture(autouse=True)
def _isolated_turn_route_facts():
    """The turn's route-seed facts (``route_facts``) are a per-turn context
    value: one test's seed must never reach another test's delegations."""
    try:
        from agents import route_facts
    except Exception:
        yield
        return
    route_facts.reset_turn()
    yield
    route_facts.reset_turn()


@pytest.fixture(autouse=True)
def _isolated_identifier_sources_request():
    """Whether a call asked for the identifier-sources line
    (``runtime_asgi.identifier_sources_var``) is set per ``tools/call``, and
    a request asked in one test must never reach another test's Mode A
    answer (it would end with the line). The turn's record of unanswered
    requests is reset the same way."""
    try:
        from agents import identifier_provenance, runtime_asgi
    except Exception:
        yield
        return
    runtime_asgi.identifier_sources_var.set(None)
    identifier_provenance._TURN_UNANSWERED.set(None)
    yield
    identifier_provenance._TURN_UNANSWERED.set(None)
    runtime_asgi.identifier_sources_var.set(None)


@pytest.fixture(autouse=True)
def _isolated_route_context_store():
    """The kept route contexts (``route_context_store``) are a process-wide
    store: one test's live turn must never reach another test's reload."""
    try:
        from agents import route_context_store
    except Exception:
        yield
        return
    route_context_store._job_store = None
    yield
    route_context_store._job_store = None


@pytest.fixture(autouse=True)
def _isolated_ssm_clients():
    """``model_config`` builds one SSM client per Region for the process.
    Tests stub ``boto3.client`` differently, so a client cached by one test
    would bypass the next test's stub: the cache is emptied around every
    test."""
    from agents import model_config
    model_config._reset_ssm_clients()
    yield
    model_config._reset_ssm_clients()


@pytest.fixture(autouse=True)
def _isolated_resolved_time_windows():
    """The calendar-policy snapshot (``calendar_policy``) and the turn's
    resolved time windows (``time_windows``) are process-global: one test's
    policy or windows must never reach another test's turn or delegation."""
    try:
        from agents import calendar_policy, time_windows
    except Exception:
        yield
        return
    calendar_policy.reset_snapshot()
    time_windows.reset_turn_windows()
    yield
    calendar_policy.reset_snapshot()
    time_windows.reset_turn_windows()
