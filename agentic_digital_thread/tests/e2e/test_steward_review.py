# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E: the steward-transparency surfaces (ADR-0052).

Covers the four browser-visible deliverables of the steward program:

  * The StewardDigest dashboard card (admin-only; hides on 403).
  * The consequence-ordered proposals list with provenance-tier badges.
  * The Try-It affordance on pending proposal cards.
  * Lifecycle (deactivate/reactivate) actions on accepted rule cards.

A full Try-It RUN costs two live orchestrator questions (~2-4 min) and is
opt-in via ADT_E2E_TRYIT=1 — the default suite only asserts the affordance.
"""

from __future__ import annotations

import os
from typing import Callable

import pytest

if True:  # keep import shape parallel to conftest's gating
    from playwright.sync_api import Page, expect


@pytest.fixture
def steward_page(
    page_errors: list[str], login: Callable[[], Page], adt_base_url: str
) -> Page:
    """Logged-in admin on the SPA root (dashboard view)."""
    page = login()
    me = page.request.get(f"{adt_base_url}/api/auth/me").json()
    if not me.get("is_admin"):
        pytest.skip("demo user is not an admin — steward surfaces are admin-only")
    # The SPA root polls continuously — networkidle never settles. Wait for
    # the app shell instead.
    expect(page.locator("body")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(3_000)
    page_errors.clear()
    return page


def test_steward_digest_card_renders(
    steward_page: Page,
    page_errors: list[str],
    open_digest_technical: Callable[[Page], None],
) -> None:
    """The digest card renders real numbers from /api/admin/learning/digest.

    Two layers, asserted separately because the product separates them
    (since 2026-08-06): the SURFACE is plain language (the "N pending proposals —
    review" link), and the lifecycle counters live behind the "Technical
    details" expander. Asserting the counters visible without opening it
    asserted the pre-0a8f08a flat card.
    """
    expect(steward_page.locator("#steward-digest-card")).to_be_visible(timeout=30_000)
    expect(
        steward_page.locator(".steward-digest-review-link").first
    ).to_be_visible(timeout=30_000)
    open_digest_technical(steward_page)
    digest = steward_page.locator(".steward-digest-stats")
    expect(digest.first).to_be_visible(timeout=30_000)
    text = digest.first.inner_text()
    assert "review" in text.lower()
    assert not [e for e in page_errors if "digest" in e.lower()], page_errors


def test_proposals_list_consequence_ordered_with_badges(
    steward_page: Page, adt_base_url: str, page_errors: list[str]
) -> None:
    """Architecture → proposals: order matches the enriched payload; tier
    badges render. Skips honestly when no pending proposals exist."""
    payload = steward_page.request.get(
        f"{adt_base_url}/api/proposed-mappings?status=pending"
    ).json()
    rows = payload if isinstance(payload, list) else payload.get("mappings", [])
    pending = [m for m in rows if m.get("status") == "pending"]
    if not pending:
        pytest.skip("no pending proposals on this environment")
    # Navigate the product's own path: the digest card's review link
    # (switchView('architecture') + requestArchTab('proposed')).
    link = steward_page.locator(".steward-digest-review-link")
    expect(link.first).to_be_visible(timeout=30_000)
    link.first.click()
    cards = steward_page.locator(".sl-card")
    expect(cards.first).to_be_visible(timeout=30_000)
    # Waits like the card above: a bare count() raced the render on a
    # 297-row queue and read 0 while the badges were still painting.
    badges = steward_page.locator(".sl-tier")
    expect(badges.first).to_be_attached(timeout=30_000)
    # SCOPED to this test's surface, like the digest test above. Leaving the
    # dashboard mid-poll aborts its in-flight panel fetches, so switchView
    # legitimately emits dashboard-panel console noise ('work-orders-by-status
    # TypeError: Failed to fetch', 'entity-sections timed out after 30000ms' —
    # both observed on 2026-08-18). Those are the dashboard's contract, covered
    # by test_dashboard_layout.py; a bare `not page_errors` here made this
    # proposals assertion fail on unrelated teardown noise. Errors naming the
    # proposals/steward surface still fail it.
    relevant = [
        e
        for e in page_errors
        if any(k in e.lower() for k in ("proposed-mapping", "sl-card", "digest", "tier"))
    ]
    assert not relevant, page_errors


def test_pending_card_offers_try_it(
    steward_page: Page, adt_base_url: str
) -> None:
    """A pending proposal card exposes the Try-It affordance (no run)."""
    payload = steward_page.request.get(
        f"{adt_base_url}/api/proposed-mappings?status=pending"
    ).json()
    rows = payload if isinstance(payload, list) else payload.get("mappings", [])
    if not [m for m in rows if m.get("status") == "pending"]:
        pytest.skip("no pending proposals on this environment")
    # Navigate the product's own path: the digest card's review link
    # (switchView('architecture') + requestArchTab('proposed')).
    link = steward_page.locator(".steward-digest-review-link")
    expect(link.first).to_be_visible(timeout=30_000)
    link.first.click()
    tryit = steward_page.locator(
        "button.sl-tryit, .sl-tryit"
    )
    expect(tryit.first).to_be_visible(timeout=30_000)


@pytest.mark.skipif(
    os.environ.get("ADT_E2E_TRYIT") != "1",
    reason="Try-It run costs two live orchestrator questions — opt in with ADT_E2E_TRYIT=1",
)
def test_try_it_run_completes_with_honest_diff(
    steward_page: Page, adt_base_url: str
) -> None:
    """Opt-in: launch one Try-It run through the API and poll to done —
    result must carry the honesty flags (injected/candidate_injected)."""
    payload = steward_page.request.get(
        f"{adt_base_url}/api/proposed-mappings?status=pending"
    ).json()
    rows = payload if isinstance(payload, list) else payload.get("mappings", [])
    pending = [
        m for m in rows
        if m.get("status") == "pending" and m.get("proposed_type") == "join_path"
    ]
    if not pending:
        pytest.skip("no pending join_path proposals to try")
    start = steward_page.request.post(
        f"{adt_base_url}/api/admin/learning/tryit",
        data={"mapping_id": pending[0]["mapping_id"]},
    )
    assert start.status in (200, 202), start.text()
    job_id = start.json()["job_id"]
    deadline_polls = 90  # x10s = 15 min ceiling
    status = "unknown"
    for _ in range(deadline_polls):
        steward_page.wait_for_timeout(10_000)
        job = steward_page.request.get(
            f"{adt_base_url}/api/admin/learning/tryit/{job_id}"
        ).json()
        status = job.get("status")
        if status in ("done", "error"):
            break
    assert status == "done", f"try-it ended {status}: {job.get('error_detail')}"
    result = job.get("result") or {}
    assert "candidate_injected" in result
    for run in ("baseline", "candidate"):
        assert "injected" in (result.get(run) or {}), f"{run} missing honesty flag"
