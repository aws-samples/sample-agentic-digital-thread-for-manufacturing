# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared fixtures for the ADT demo integration harness (S0.2).

This suite exercises the **real** deterministic resolver against the **real**
backends it depends on:

  * a *live* Neptune Analytics graph that has >=1 onboarded system
    (read via ``agents.db.neptune_query``), and
  * a *local* disposable Postgres seeded with the ``cmms_maintenance`` schema
    (see ``tests/integration/docker-compose.yml`` + ``seed_pg.py``).

Like the e2e (``ADT_PASSWORD``) and audit (``pick_target_system``) suites, every
external dependency is **gated by a skip, never a hard failure**, so the suite
stays green in a credential-free / no-Postgres CI lane:

  * ``live_graph``     skips when no reachable graph has an onboarded system
                       (no ``GRAPH_IDENTIFIER`` / ``AWS_REGION`` / credentials,
                       or nothing onboarded).
  * ``local_postgres`` skips when no local Postgres is reachable.

CRITICAL — these fixtures hit *real* backends. The resolver seam
(``resolve_entity``) is returned UNCHANGED: ``pg`` / ``neptune_query`` /
``neptune_write`` are NOT monkeypatched (they are the integration under test).
Only the LLM path is guarded (``no_llm_guard``) to enforce ADR-0001
render/resolve determinism — the Neptune ``neptune-graph`` reads are legitimate
and stay live.

No AWS / Postgres call fires at import or collection time: ``pick_target_system``
and the Postgres connect happen inside the (session-scoped) fixtures, not at
module import.
"""

from __future__ import annotations

from typing import Callable

import pytest

from agents import pgdriver
from agents.db import get_conn_params
from agents.resolve import resolve_entity
from tests.audit.audit_onboarding_graph import pick_target_system

# Skip messages (module constants so tests/readers can reference the exact text).
NO_GRAPH_SKIP = (
    "no reachable Neptune graph with an onboarded system (no MAPS_TO edges "
    "found) — set GRAPH_IDENTIFIER + AWS_REGION and AWS credentials for a dev "
    "graph that has been onboarded (e.g. 'cmms')"
)
NO_PG_SKIP = "no local Postgres — run docker compose up + seed_pg.py"


@pytest.fixture(scope="session")
def live_graph() -> str:
    """Yield a system id (e.g. ``'cmms'``) that has MAPS_TO edges, else skip.

    Uses ``audit_onboarding_graph.pick_target_system()`` — which returns
    ``None`` when no graph is reachable (missing ``GRAPH_IDENTIFIER`` /
    ``AWS_REGION`` / credentials) or nothing has been onboarded. In that case we
    **skip** (mirroring the ``audit_target`` fixture) rather than fail, so the
    suite stays green without cloud access. The picked id is the same target the
    audit uses, so resolver tests read the system the audit already verified.
    """
    target = pick_target_system()
    if target is None:
        pytest.skip(NO_GRAPH_SKIP)
    return target


@pytest.fixture(scope="session")
def local_postgres() -> dict:
    """Yield connection params for a reachable, seeded local Postgres, else skip.

    Connects via ``agents.db.get_conn_params()`` (the same params the resolver's
    ``pg`` seam uses). On any connection error we **skip** with a clear
    remediation message. On success we assert the credential-free value-read
    lane is actually seeded by requiring ``cmms_maintenance.spare_parts`` to be
    non-empty (the table the S0.1 F9 ``inventory-level`` tracer reads from);
    an empty table is a *failure*, not a skip, because it means PG is up but the
    seed never ran.
    """
    params = get_conn_params()
    try:
        conn = pgdriver.connect(**params)
    except pgdriver.Error as exc:
        pytest.skip(f"{NO_PG_SKIP} ({type(exc).__name__})")

    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM cmms_maintenance.spare_parts")
            (count,) = cur.fetchone()
    finally:
        conn.close()

    assert count and count > 0, (
        "local Postgres is reachable but cmms_maintenance.spare_parts is empty "
        "— run tests/integration/seed_pg.py to seed the value-read lane"
    )
    return params


@pytest.fixture
def resolve_fn() -> Callable[[str, str], tuple]:
    """Return the real ``agents.resolve.resolve_entity`` UNCHANGED.

    This is the integration seam under test. We deliberately do NOT monkeypatch
    ``pg`` / ``neptune_query`` / ``neptune_write`` (resolve.py imports them from
    ``agents.db``) — those must hit the real local Postgres + live Neptune for
    the concept->SQL->value loop to mean anything.
    """
    return resolve_entity


@pytest.fixture
def no_llm_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any LLM invocation during resolve/render a hard error (ADR-0001).

    Render/resolve must be deterministic: they read the already-onboarded graph
    and the source databases, never an LLM. We monkeypatch the two Bedrock entry
    points in ``agents.db`` so an accidental call fails loudly instead of
    silently introducing non-determinism.

    Guarded symbols (the exact LLM-call names in ``agents/db.py``):
      * ``agents.db.bedrock_call``   (synchronous invoke)
      * ``agents.db.bedrock_stream`` (streaming invoke)

    NOT guarded: ``neptune_query`` / ``neptune_write`` (boto3 ``neptune-graph``
    reads are legitimate) and ``pg`` (real Postgres read).
    """

    def _no_bedrock_call(*args, **kwargs):
        raise AssertionError(
            "bedrock_call invoked during a deterministic resolve/render path "
            "(ADR-0001 violation): the resolver must not call an LLM"
        )

    def _no_bedrock_stream(*args, **kwargs):
        raise AssertionError(
            "bedrock_stream invoked during a deterministic resolve/render path "
            "(ADR-0001 violation): the resolver must not call an LLM"
        )

    monkeypatch.setattr("agents.db.bedrock_call", _no_bedrock_call)
    monkeypatch.setattr("agents.db.bedrock_stream", _no_bedrock_stream)
