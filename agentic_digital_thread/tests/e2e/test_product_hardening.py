# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E: product-hardening browser surfaces (ADR-0053).

The cost trend line on the steward digest card is the browser-visible tip of
the E7 loop (capture -> ledger -> digest -> UI). It sits behind the digest's
"Technical details" expander (since 2026-08-06), so the test walks that reveal — see the
``open_digest_technical`` fixture. Preflight/drift/tombstone
surfaces are API-verified in the Wave-4 gate battery; their UI states are
unit-pinned (onboard-preflight.test.tsx, sl-cards.test.tsx).
"""

from __future__ import annotations

from typing import Callable

import pytest

from playwright.sync_api import Page, expect


@pytest.fixture
def dashboard_page(
    page_errors: list[str], login: Callable[[], Page], adt_base_url: str
) -> Page:
    page = login()
    me = page.request.get(f"{adt_base_url}/api/auth/me").json()
    if not me.get("is_admin"):
        pytest.skip("demo user is not an admin")
    expect(page.locator("body")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(3_000)
    page_errors.clear()
    return page


def test_digest_cost_trend_renders_when_measured(
    dashboard_page: Page,
    adt_base_url: str,
    open_digest_technical: Callable[[Page], None],
) -> None:
    """The digest shows the avg-tokens line iff the API reports a measured
    cost trend — honest both ways.

    The line lives behind the digest's "Technical details" expander,
    so BOTH branches first render the card and open the expander: that pins the
    DOM to a state where "no cost line" can only mean *unmeasured*, never
    "the card had not loaded yet" (the absent branch's original
    ``count() == 0`` passed vacuously before the digest fetch resolved).
    """
    digest = dashboard_page.request.get(
        f"{adt_base_url}/api/admin/learning/digest"
    ).json()
    trend = digest.get("cost_trend") or {}
    has_usable_day = any(d.get("turns") for d in trend.get("days", []))
    # Anchor on the rendered digest before judging presence/absence.
    expect(dashboard_page.locator("#steward-digest-card")).to_be_visible(
        timeout=30_000
    )
    open_digest_technical(dashboard_page)
    expect(dashboard_page.locator(".steward-digest-stats").first).to_be_visible(
        timeout=30_000
    )
    line = dashboard_page.locator(".steward-digest-cost-trend")
    if has_usable_day:
        expect(line.first).to_be_visible(timeout=30_000)
        assert "tokens/answer" in line.first.inner_text()
    else:
        assert line.count() == 0, "cost line rendered without measured data"
