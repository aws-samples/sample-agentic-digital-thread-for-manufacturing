# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unified proposal-lifecycle integration suite (S3.1 / Task 4.1, ADR 0003).

These tests exercise the **real** proposal store (``agents.self_learning``)
against the **real** backends it depends on: a *live* DynamoDB
``adt-proposed-mappings`` table (PK ``mapping_id`` / SK ``discovered_at`` +
``subject-key-index`` GSI) and a *live* Neptune Analytics graph (the
subject-keyed applied node + the onboarding ``MAPS_TO`` edge).

Like the sibling resolver/mapping-eval integration suites, every external
dependency is **gated by a skip, never a hard failure**, so the suite stays
green in a credential-free / no-DynamoDB / no-Neptune CI lane:

  * ``live_graph``     (conftest) skips when no reachable graph has an onboarded
                       system — this also proves Neptune is reachable for the
                       subject-keyed applied-node writes.
  * ``proposal_store`` (below) skips when the DynamoDB proposal table is not
                       reachable (no creds / table absent / endpoint error).

No AWS / Neptune call fires at import or collection time: ``_get_table()`` is
lazy, ``pick_target_system()`` runs inside the (session-scoped) ``live_graph``
fixture, and the DynamoDB ``describe`` probe runs inside ``proposal_store`` —
never at module import. Importing ``agents.self_learning`` only pulls in the
pure ``proposal_key`` module + the boto3 *module* (no client/session built).

Four assertions (ADR 0003 C/D/E + the S3.1 version seam):
  (1) subject-key idempotency — logging the same target twice yields exactly one
      pending row for that subject (lookup-before-write, ADR 0003 C).
  (2) supersede — a different-target second proposal on the same subject marks
      the first ``superseded``; exactly one applied node remains (ADR 0003 C/D).
  (3) re-key single-node guard — after accept + supersede + accept, exactly ONE
      applied node exists for the subject (the ADR 0003 D double-read fix),
      carrying the latest ``version``.
  (4) end-to-end ``mapping_version`` — an onboarded field's resolved lineage
      carries ``mapping_version == "1"`` (proves the onboarding ``MAPS_TO``
      ``r.version`` stamp (Task 2.3) + the resolver read (Task 2.2) meet on a
      live graph).

Every test cleans up only the rows/nodes IT created (a unique ``adt-test-*``
subject + concept), so the suite is safe to run against a shared dev store.
"""

from __future__ import annotations

import pytest

import agents.self_learning as sl
from agents.db import neptune_query, neptune_write
from tests.audit.audit_onboarding_graph import CYPHER_FIELD_CONCEPT, _row_get

pytestmark = pytest.mark.integration

# --- Test-only identities (distinctive prefixes → never collide with real data,
# and make teardown surgical instead of a blast-radius clear_all_learnings()). ---
TEST_FROM_SYSTEM = "adt-test-lifecycle-src"
TEST_TO_SYSTEM = "adt-test-lifecycle-dst"
TEST_CONCEPT = "adt-test-lifecycle-concept"

# Task 2.3 end-to-end version anchor (mirrors test_resolver_integration's F9 tracer).
MRO_CONCEPT = "mro-inventory"
SPARE_PARTS_METRIC = "spare-parts-on-hand"


# ---------------------------------------------------------------------------
# Cleanup helper (surgical — only the subject this test created)
# ---------------------------------------------------------------------------

def _cleanup_subject(table, subject_key: str) -> None:
    """Delete every DynamoDB row + the Neptune applied node for one subject.

    Best-effort and exception-swallowing (teardown must never mask a test
    result). Deletes the subject-keyed VM/JP/IR node and the fake test concept
    node we may have MERGE-created via ``_apply_to_neptune``.
    """
    try:
        rows = sl._query_subject(table, subject_key)
        for r in rows:
            table.delete_item(
                Key={"mapping_id": r["mapping_id"], "discovered_at": r["discovered_at"]}
            )
    except Exception:  # noqa: BLE001 - teardown best-effort  # nosec B110 - best-effort path; failure is deliberately non-fatal
        pass

    subject_id = sl._subject_id(subject_key)
    for label in ("ValueMapping", "JoinPath", "IdentityRule"):
        try:
            neptune_write(f"MATCH (n:{label} {{id: $sid}}) DETACH DELETE n", {"sid": subject_id})
        except Exception:  # noqa: BLE001 - teardown best-effort  # nosec B110 - best-effort path; failure is deliberately non-fatal
            pass
    try:
        neptune_write("MATCH (c:Concept {id: $cid}) DETACH DELETE c", {"cid": TEST_CONCEPT})
    except Exception:  # noqa: BLE001 - teardown best-effort  # nosec B110 - best-effort path; failure is deliberately non-fatal
        pass


def _applied_nodes(subject_key: str) -> list[dict]:
    """Return the live ValueMapping applied node(s) for a subject (read-only)."""
    subject_id = sl._subject_id(subject_key)
    return neptune_query(
        "MATCH (vm:ValueMapping {id: $sid}) RETURN vm.version AS version", {"sid": subject_id}
    ) or []


# ---------------------------------------------------------------------------
# Gating fixture — skips cleanly when the live DynamoDB store is unreachable
# ---------------------------------------------------------------------------

@pytest.fixture
def proposal_store(live_graph: str):
    """Yield ``(self_learning, table, register)``; skip without a live store.

    Depends on ``live_graph`` (conftest) so Neptune is known-reachable for the
    subject-keyed applied-node writes, then probes DynamoDB with a lazy
    ``describe`` (``Table.load()``). On any backend error we **skip** (mirroring
    the conftest gating philosophy) rather than fail, so the credential-free lane
    stays green. ``register(subject_key)`` records subjects for surgical teardown.
    """
    try:
        table = sl._get_table()
        table.load()  # DescribeTable — fails fast (no creds / table absent / endpoint)
    except Exception as exc:  # noqa: BLE001 - any backend error → skip, never fail
        pytest.skip(
            f"no reachable DynamoDB proposal store ({type(exc).__name__}) — set "
            f"AWS_REGION + credentials + PROPOSED_MAPPINGS_TABLE for a dev table"
        )

    created: list[str] = []

    def register(subject_key: str) -> None:
        created.append(subject_key)

    try:
        yield sl, table, register
    finally:
        for subject_key in created:
            _cleanup_subject(table, subject_key)


def _log_value_mapping(from_value: str, to_value: str) -> tuple[str, str]:
    """Log a test value_mapping proposal; return (mapping_id, subject_key)."""
    keys = sl.canonical_keys(
        "value_mapping",
        from_system=TEST_FROM_SYSTEM, from_field="orders.code", from_value=from_value,
        to_system=TEST_TO_SYSTEM, to_field="accounts.code", to_value=to_value,
        concept=TEST_CONCEPT,
    )
    mapping_id = sl.log_proposed_mapping(
        TEST_FROM_SYSTEM, "orders.code", from_value,
        TEST_TO_SYSTEM, "accounts.code", to_value,
        TEST_CONCEPT, 0.9, "integration-test", proposed_type="value_mapping",
    )
    return mapping_id, keys.subject_key


# ---------------------------------------------------------------------------
# (1) subject-key idempotency (ADR 0003 C — lookup-before-write)
# ---------------------------------------------------------------------------

def test_subject_key_idempotency_single_pending_row(proposal_store) -> None:
    """Logging the SAME target twice yields exactly one pending row on the subject.

    The second ``log_proposed_mapping`` must hit the ``dup_pending`` branch
    (exact ``target_key`` match, prior ``pending``) — no second insert, and the
    existing ``mapping_id`` is returned.
    """
    _sl, table, register = proposal_store

    id1, subject_key = _log_value_mapping("c-100", "acct-7")
    register(subject_key)
    id2, _ = _log_value_mapping("c-100", "acct-7")

    assert id1 == id2, "dup-pending must return the existing mapping_id (no new insert)"
    pending = [r for r in sl._query_subject(table, subject_key) if r.get("status") == "pending"]
    assert len(pending) == 1, (
        f"expected exactly one pending row on subject {subject_key!r}, got {len(pending)}: "
        f"{[r['mapping_id'] for r in pending]}"
    )


# ---------------------------------------------------------------------------
# (2) supersede (ADR 0003 C/D — same subject, different target)
# ---------------------------------------------------------------------------

def test_supersede_marks_prior_and_keeps_single_node(proposal_store) -> None:
    """A diff-target second proposal supersedes the first; one applied node remains.

    Flow: log target A → accept A (creates the subject-keyed applied node v1) →
    log target B (same subject, different ``to_value``). B's lookup-before-write
    finds A active on the subject and marks A's DynamoDB row ``superseded``; the
    single applied node (from A's accept) still exists exactly once.
    """
    _sl, table, register = proposal_store

    id_a, subject_key = _log_value_mapping("c-200", "acct-A")
    register(subject_key)
    sl.accept_mapping(id_a)  # applied node v1

    id_b, _ = _log_value_mapping("c-200", "acct-B")  # diff target, same subject

    rows = {r["mapping_id"]: r for r in sl._query_subject(table, subject_key)}
    assert rows[id_a]["status"] == "superseded", (
        f"prior proposal {id_a} should be 'superseded', got {rows[id_a]['status']!r}"
    )
    assert id_b in rows and rows[id_b]["status"] == "pending"

    nodes = _applied_nodes(subject_key)
    assert len(nodes) == 1, (
        f"expected exactly one applied node for subject {subject_key!r}, got {len(nodes)}"
    )


# ---------------------------------------------------------------------------
# (3) re-key single-node guard (ADR 0003 D — the double-read fix)
# ---------------------------------------------------------------------------

def test_rekey_single_applied_node_carries_latest_version(proposal_store) -> None:
    """After accept + supersede + accept, exactly ONE applied node carries v2.

    This is the ADR 0003 D re-key invariant: both accepts MERGE the SAME
    subject-keyed node in place (no per-proposal id), so superseding A and
    accepting B leaves a single live node stamped with the latest ``version``.
    """
    _sl, table, register = proposal_store

    id_a, subject_key = _log_value_mapping("c-300", "open")
    register(subject_key)
    sl.accept_mapping(id_a)  # v1

    id_b, _ = _log_value_mapping("c-300", "closed")  # supersedes A → v2
    res_b = sl.accept_mapping(id_b)

    assert res_b["version"] == 2, f"accepted B should be version 2, got {res_b['version']!r}"

    nodes = _applied_nodes(subject_key)
    assert len(nodes) == 1, (
        f"ADR 0003 D guard: exactly one applied node must exist for subject "
        f"{subject_key!r}, got {len(nodes)}"
    )
    assert str(nodes[0].get("version")) == "2", (
        f"the single applied node must carry the latest version '2', got "
        f"{nodes[0].get('version')!r}"
    )


# ---------------------------------------------------------------------------
# (4) end-to-end mapping_version (Task 2.2 read + Task 2.3 onboarding stamp)
# ---------------------------------------------------------------------------

def test_onboarded_field_resolves_mapping_version(
    live_graph: str,
    local_postgres: dict,
    no_llm_guard: None,
) -> None:
    """An onboarded field's resolved lineage carries ``mapping_version == "1"``.

    Proves the onboarding ``MAPS_TO`` ``r.version = coalesce(r.version, 1)`` stamp
    (Task 2.3) and the resolver's ``r.version`` read (Task 2.2) meet on a live
    graph: ``resolve_metric('spare-parts-on-hand')`` traverses the real
    ``mro-inventory`` ``MAPS_TO`` edge and surfaces its version into
    ``lineage.mapping.mapping_version``.

    Gated like the sibling F9 tracer: skips when the live graph maps no
    ``mro-inventory`` field, and skips (rather than fails) when the resolved
    mapping carries no version — that means the graph was onboarded before the
    Task 2.3 stamp and must be re-onboarded to surface it.
    """
    # Import inside the test so collection never fails in lanes lacking the
    # resolver's optional deps (the gate fixtures skip before we reach here).
    import agents.metric_resolver as metric_resolver

    rows = neptune_query(CYPHER_FIELD_CONCEPT, {"sid": live_graph}) or []
    if not any(_row_get(r, "concept", "c.id") == MRO_CONCEPT for r in rows):
        pytest.skip(
            f"system '{live_graph}' maps no '{MRO_CONCEPT}' field on the live "
            f"graph — re-onboard so the {SPARE_PARTS_METRIC} mapping exists"
        )

    resolved = metric_resolver.resolve_metric(SPARE_PARTS_METRIC)

    assert resolved.lineage is not None, "resolve_metric produced no lineage"
    assert resolved.lineage.mapping is not None, "direct lineage carries no MAPS_TO mapping"

    version = resolved.lineage.mapping.mapping_version
    if version is None:
        pytest.skip(
            f"live graph's MAPS_TO edge carries no r.version — it was onboarded "
            f"before the Task 2.3 stamp; re-onboard '{live_graph}' to surface "
            f"mapping_version"
        )
    assert version == "1", (
        f"an onboarded field's resolved mapping_version should be '1' "
        f"(onboarding stamps r.version = coalesce(r.version, 1)), got {version!r}"
    )
    # value_mapping_version stays the S3.2 seam — no resolve-time read site.
    assert resolved.lineage.mapping.value_mapping_version is None
