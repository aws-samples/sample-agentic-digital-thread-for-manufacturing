# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E — the steward context experience (T3.2, reduced per ADR-0068).

Two checks, both about the SAME promise the interview makes ("you can
edit this later"):

* **Admin → Context** must exist on any deployment and, when a system is
  onboarded, render the per-system business-context editor with the LIVE stored
  text — this is the standing edit surface the comfort note points at.
* **The standalone guided-interview panel is RETIRED** (ADR-0068): context
  capture moved into the onboarding conversation. The reduced check asserts
  the guided backend (``/api/onboard/guided/state``) still serves — the
  conversational card and the wizard's PreMapProbe ride those endpoints —
  and that the panel's selectors are GONE from /onboard.

FOLLOW-UP (deliberately not covered here): the full conversational journey —
onboarding chat → ``assess_vocabulary_fit`` checkpoint → inline Context
Interview card → confirm — needs the onboarding agent plus a live connectable
system (~minutes per run) and belongs in a dedicated onboarding-chat e2e.

Credential-gated like the rest of the suite: requesting ``login`` /
``adt_password`` skips cleanly when ``ADT_PASSWORD`` is unset.
"""
from __future__ import annotations

from typing import Callable

import pytest
from playwright.sync_api import Page, expect

UI_TIMEOUT_MS = 30_000


@pytest.fixture
def admin_page(page_errors: list[str], login: Callable[[], Page], adt_base_url: str) -> Page:
    """Logged-in admin on /admin.html (mirrors test_admin.py)."""
    page = login()
    me = page.request.get(f"{adt_base_url}/api/auth/me").json()
    if not me.get("is_admin"):
        pytest.skip("demo user is not an admin — skipping admin-only tests")
    page.goto(f"{adt_base_url}/admin.html")
    # Product-DOM readiness, NOT transport quiescence — the admin SPA refetches
    # on a React Query interval, so 'networkidle' asserts something that is not a
    # product contract and can never settle (test_steward_review.py says the same
    # of the SPA root). page.goto already awaited the load event.
    expect(page.locator("nav.tab-bar")).to_be_visible(timeout=20_000)
    expect(page.locator("#access-denied")).to_be_hidden()
    page_errors.clear()
    return page


def _onboarded_ids(page: Page, adt_base_url: str) -> list[str]:
    resp = page.request.get(f"{adt_base_url}/api/onboard/catalog", timeout=60_000)
    if not resp.ok:
        return []
    payload = resp.json()
    entries = payload if isinstance(payload, list) else (
        payload.get("systems") or payload.get("catalog") or [])
    return [e.get("id") or e.get("system_id") for e in entries
            if e.get("status") == "onboarded" and (e.get("id") or e.get("system_id"))]


@pytest.mark.e2e
def test_admin_context_tab_edits_live_business_context(
    admin_page: Page, page_errors: list[str], adt_base_url: str
) -> None:
    """The standing edit surface: Admin → Context lists onboarded systems and
    loads the STORED text into an editable textarea — proof the interview's
    "edit this later" promise has a real destination."""
    page = admin_page
    onboarded = _onboarded_ids(page, adt_base_url)

    tab_btn = page.locator('.tab-bar button[data-tab="tab-context"]')
    expect(tab_btn).to_be_visible(timeout=UI_TIMEOUT_MS)
    tab_btn.click()
    panel = page.locator("#tab-context")
    expect(panel).to_be_visible(timeout=UI_TIMEOUT_MS)

    if not onboarded:
        # Honest empty state, not a blank panel.
        expect(panel).to_contain_text("No onboarded systems", timeout=UI_TIMEOUT_MS)
        return

    # A system pill per onboarded system; the first is selected by default and
    # its BusinessContextCard GETs the stored value into the textarea. The pills
    # render after the catalog fetch resolves — wait, never instant-count.
    pills = panel.get_by_role("tab")
    expect(pills.first).to_be_visible(timeout=UI_TIMEOUT_MS)
    assert pills.count() >= 1, "expected a system selector pill per onboarded system"
    textarea = panel.locator("textarea")
    expect(textarea.first).to_be_visible(timeout=UI_TIMEOUT_MS)

    # The card must reflect the LIVE stored context: compare against the API.
    sys_id = onboarded[0]
    stored = page.request.get(
        f"{adt_base_url}/api/admin/business-context/{sys_id}", timeout=60_000)
    if stored.ok:
        stored_text = (stored.json() or {}).get("business_context") or ""
        if stored_text.strip():
            expect(textarea.first).to_have_value(stored_text, timeout=UI_TIMEOUT_MS)

    assert [e for e in page_errors if e.startswith("pageerror:")] == []


@pytest.mark.e2e
def test_guided_panel_retired_and_state_api_still_serves(
    login: Callable[[], Page], page_errors: list[str], adt_base_url: str
) -> None:
    """The panel is retired (ADR-0068); the guided backend is not.

    Proves, on ANY deployment (no first-launch gating — absence must hold
    everywhere):

    * ``GET /api/onboard/guided/state`` still answers with the guided-state
      shape — the conversational Context Interview card and the wizard's
      PreMapProbe both depend on these endpoints staying up; and
    * the retired ``GuidedContextFlow`` panel's selectors are absent from
      /onboard: its landmark ``section[aria-label="Guided context setup"]``
      and its review-step "Continue to onboarding" CTA. ("I don't know" is
      deliberately NOT asserted absent — the conversational card legitimately
      reuses that skip affordance.)

    The FULL conversational e2e (onboarding chat → vocab-fit checkpoint →
    inline interview card → confirm) is a follow-up: it needs a live
    connectable system and the onboarding agent, ~minutes per run.
    """
    page = login()
    resp = page.request.get(
        f"{adt_base_url}/api/onboard/guided/state", timeout=60_000)
    assert resp.ok, f"/api/onboard/guided/state must still serve, got {resp.status}"
    state = resp.json()
    assert "first_launch" in state, f"unexpected guided-state shape: {state!r}"

    page.click("#onboard-btn")
    expect(page.locator("#onboard-view")).to_be_visible(timeout=UI_TIMEOUT_MS)
    expect(page.locator('section[aria-label="Guided context setup"]')).to_have_count(0)
    expect(page.get_by_role("button", name="Continue to onboarding")).to_have_count(0)
    assert [e for e in page_errors if e.startswith("pageerror:")] == []
