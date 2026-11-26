# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 4 (item E, scoped to SURFACING): the resolve job threads join-candidate
summaries into its result.

``_run_resolve_job`` already runs order-independent join discovery after the
identity analysis; this wave additionally reads the just-logged proposals back
(via ``self_learning.get_pending_mappings`` filtered to the ids discovery
returned) and attaches compact ``{mapping_id, proposed_type, from, to,
transform, confidence}`` summaries as the ADDITIVE ``join_candidates`` key on
the job result — so the onboarding UI can render a NON-BLOCKING review panel
wired to the EXISTING accept/reject proposal routes.

Invariants proven offline (no AWS, no network — every impure seam patched):
  * two discovered ids → two summaries in the job result;
  * discovery failure → the job still succeeds with ``join_candidates == []``;
  * a summary read failure is swallowed → ``join_candidates == []``;
  * ids no longer pending (e.g. deduped/accepted) are omitted, output is
    deterministic (sorted by id), and ``transform`` defaults to ``identity``.

Synthetic identifiers only.
"""
from __future__ import annotations

from unittest.mock import patch

from agents import onboard
from agents.job_store import JobStore
from agents.models import OnboardResolveRequest


class _FakeTable:
    """Minimal shared DynamoDB Table double (put_item/get_item/delete_item)."""

    def __init__(self, store: dict):
        self._store = store

    def put_item(self, Item):  # noqa: N803 (boto3 kwarg)
        self._store[Item["system_id"]] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self._store.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key):  # noqa: N803
        self._store.pop(Key["system_id"], None)


def _pending_item(mid: str, **over) -> dict:
    base = {
        "mapping_id": mid,
        "proposed_type": "join_path",
        "from_system": "sys-a", "from_field": "orders.order_id",
        "to_system": "sys-b", "to_field": "work_orders.wo_number",
        "transform": "casefold",
        "confidence": "0.8",
        "status": "pending",
    }
    base.update(over)
    return base


def _run_job(body, *, disc, pending):
    """Drive _run_resolve_job with every impure seam patched; return the stored job."""
    shared: dict = {}
    store = JobStore("resolve", table=_FakeTable(shared))

    def _discover(system_id):
        if isinstance(disc, Exception):
            raise disc
        return disc

    def _get_pending():
        if isinstance(pending, Exception):
            raise pending
        return pending

    with patch.object(onboard, "_resolve_jobs", store), \
         patch.object(onboard, "_do_resolve", return_value={"rules": []}), \
         patch("agents.join_discovery.discover_joins_for_system", _discover), \
         patch("agents.self_learning.get_pending_mappings", _get_pending):
        onboard._run_resolve_job("job-jc", body)
    return store.get("job-jc")


def test_two_discovered_ids_surface_two_summaries():
    """Discovery returning 2 ids → job result carries 2 compact summaries."""
    body = OnboardResolveRequest(system_id="sys-a", mappings=[])
    job = _run_job(
        body,
        disc={"joins": ["mid-2"], "crosswalks": ["mid-1"]},
        pending=[_pending_item("mid-1", proposed_type="crosswalk", transform=""),
                 _pending_item("mid-2")],
    )
    assert job["status"] == "done"
    cands = job["result"]["join_candidates"]
    assert len(cands) == 2
    # Deterministic (sorted by mapping_id); transform defaults to identity.
    assert [c["mapping_id"] for c in cands] == ["mid-1", "mid-2"]
    assert cands[0]["proposed_type"] == "crosswalk"
    assert cands[0]["transform"] == "identity"
    assert cands[1] == {
        "mapping_id": "mid-2",
        "proposed_type": "join_path",
        "from": "sys-a: orders.order_id",
        "to": "sys-b: work_orders.wo_number",
        "transform": "casefold",
        "confidence": "0.8",
    }


def test_discovery_failure_still_succeeds_with_empty_candidates():
    """A discovery blow-up never fails the resolve job — join_candidates == []."""
    body = OnboardResolveRequest(system_id="sys-a", mappings=[])
    job = _run_job(body, disc=RuntimeError("graph unreachable"), pending=[])
    assert job["status"] == "done"
    assert job["result"]["join_candidates"] == []
    assert job["result"]["rules"] == []


def test_summary_read_failure_swallowed():
    """get_pending_mappings raising is swallowed by the surfacing helper."""
    body = OnboardResolveRequest(system_id="sys-a", mappings=[])
    job = _run_job(
        body,
        disc={"joins": ["mid-1"], "crosswalks": []},
        pending=RuntimeError("dynamo unreachable"),
    )
    assert job["status"] == "done"
    assert job["result"]["join_candidates"] == []


def test_ids_no_longer_pending_are_omitted():
    """An id whose proposal is no longer pending (deduped→accepted) is skipped."""
    body = OnboardResolveRequest(system_id="sys-a", mappings=[])
    job = _run_job(
        body,
        disc={"joins": ["mid-1", "mid-gone"], "crosswalks": []},
        pending=[_pending_item("mid-1")],
    )
    cands = job["result"]["join_candidates"]
    assert [c["mapping_id"] for c in cands] == ["mid-1"]


def test_helper_empty_disc_returns_empty_without_reading():
    """No ids → [] without touching the pending store at all."""
    with patch("agents.self_learning.get_pending_mappings",
               side_effect=AssertionError("must not be called")):
        assert onboard._join_candidate_summaries({"joins": [], "crosswalks": []}) == []
        assert onboard._join_candidate_summaries({}) == []
        assert onboard._join_candidate_summaries(None) == []
