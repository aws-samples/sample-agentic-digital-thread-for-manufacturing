# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E: the three-tier provenance UX in a real browser.

Drives one chat turn against a live deployment and walks the provenance
surface the way a user would:

  Tier 1 — the Sources row chips (``a[data-cite-target]``) and the inline
           ``.cite-badge`` anchors rendered into the answer body.
  Tier 2 — clicking a provenance-bound Sources chip opens the per-claim
           evidence card (``.evidence-card-host`` / ``.evidence-card``).
           The card must never leak raw query text ("NO SQL in Tier 2",
           frozen contract D6); the honest "No captured evidence for this
           claim" banner is a PASS, not a failure.
  Tier 3 — the "Technical detail" expander settles into EITHER real detail
           (executed query / not-captured note) OR the honest
           "Technical detail not available for this claim" line — both PASS.

Honest-sparse resilience: every assertion tolerates Mode-A sparse records.
No specific table/window/value is ever asserted. A chip WITHOUT a bound
provenance record legitimately falls back to the legacy architecture-view
focus (thread-clicks.ts contract) — the test asserts that path too instead
of failing.

Selector contract (pinned by the frontend unit suite):
  - Sources-row chips:     ``a[data-cite-target][data-cite-num]`` (no class);
    bound chips additionally carry ``data-verification`` (AssistantTurn.tsx —
    the attribute is present IFF a record bound, so it doubles as the
    card-will-open signal).
  - Inline answer badges:  ``a.cite-badge[data-cite-target]`` (Md.tsx).
  - Evidence card:         ``.evidence-card-host`` > ``.evidence-card``
    (role=dialog), close via ``button[aria-label="Close evidence card"]``.
  - Tier 3:                ``details.evidence-tier3``; terminal states are
    ``.evidence-executed-query`` / ``.evidence-query-absent`` /
    ``.evidence-tier3-unavailable`` (loading spinner is
    ``.evidence-tier3-loading``).
"""

from __future__ import annotations

from typing import Callable

import pytest
from playwright.sync_api import Page, expect

QUESTION = "Which work orders are currently open, and which machines are they for?"

# Streamed multi-agent answers routinely take minutes; generous by design.
STREAM_TIMEOUT_MS = 300_000
UI_TIMEOUT_MS = 15_000

CARD_SCREENSHOT = "/tmp/e2e-prov-card.png"
TIER3_SCREENSHOT = "/tmp/e2e-prov-tier3.png"


def _ask_and_wait(page: Page, question: str) -> None:
    """Send one chat question and wait for the streamed answer to complete.

    Completion signal: the send button carries class ``streaming`` while a
    stream is active (ChatSidebar.tsx renders ``className={isStreaming ?
    'streaming' : ''}``) and drops it when the turn finishes or errors.
    """
    page.fill("#question-input", question, timeout=UI_TIMEOUT_MS)
    page.click("#send-btn")
    expect(page.locator("#send-btn")).to_have_class("streaming", timeout=30_000)
    expect(page.locator("#send-btn")).not_to_have_class(
        "streaming", timeout=STREAM_TIMEOUT_MS
    )


def _dismiss_auto_opened_modal(page: Page) -> None:
    """Close the pop-out chat modal if it is open.

    The end-of-turn AUTO-open was retired 2026-08-06 (the chat-UX batch
    replaced it with an explicit expand affordance — chat/useAskStream.ts no
    longer calls openModal on done), so on current builds this is a no-op.
    Kept as dismiss-if-present because other legitimate openings (the header
    pop-out, the toast activation) can leave the overlay up, and the modal
    renders the SAME store-backed thread, so closing it loses nothing; the
    test then interacts with the sidebar copy.
    """
    overlay = page.locator("#chat-modal-overlay.open")
    if overlay.count() > 0:
        overlay.locator(".chat-modal-close").click()
        expect(page.locator("#chat-modal-overlay.open")).to_have_count(
            0, timeout=UI_TIMEOUT_MS
        )


@pytest.mark.e2e
class TestThreeTierProvenance:
    def test_provenance_three_tiers(
        self,
        login: Callable[[], Page],
        page_errors: list[str],
    ) -> None:
        page = login()
        expect(page.locator("#question-input")).to_be_visible(timeout=20_000)

        # ── Step 1: ask and wait for the streamed answer to complete ─────
        _ask_and_wait(page, QUESTION)
        _dismiss_auto_opened_modal(page)

        turn = page.locator("#messages .message.assistant").last
        expect(turn).to_be_visible(timeout=UI_TIMEOUT_MS)
        assert "Error:" not in (turn.locator("> div").first.inner_text()[:200]), (
            "the chat turn ended in an error instead of an answer"
        )

        # ── Step 2 (Tier 1): Sources row + inline cite badges ────────────
        # Sources-row chips carry data-cite-num but NOT the .cite-badge class
        # (that class belongs to the inline answer-body badges from Md.tsx).
        chips = turn.locator("a[data-cite-target]:not(.cite-badge)")
        expect(chips.first).to_be_visible(timeout=UI_TIMEOUT_MS)
        chip_count = chips.count()
        assert chip_count >= 1, "Sources row rendered no citation chips"

        inline_badges = turn.locator("a.cite-badge[data-cite-target]")
        inline_count = inline_badges.count()
        assert inline_count >= 1 or chip_count >= 1, (
            "no inline .cite-badge / [data-cite-target] anchors rendered"
        )

        bound_chips = turn.locator(
            "a[data-cite-target][data-verification]:not(.cite-badge)"
        )
        bound_count = bound_chips.count()
        # Recorded, not asserted — Mode-A honest-sparse turns may bind zero
        # records; the chip then follows the legacy fallback contract below.
        print(
            f"\n[tier1] sources chips={chip_count} inline badges={inline_count} "
            f"chips with data-verification={bound_count}"
        )

        # Everything below is interaction-phase: judge console errors from
        # here on (streaming noise, if any, is not what this guard is for).
        # The Tier-3/-dataset redemption routes are DESIGNED to 403/404 on
        # expired or non-captured refs (the card renders the honest line), and
        # Chromium logs any failed load as a console.error — so failed
        # responses are tracked by URL: D5 redemption-route failures
        # (/api/admin/agents/traces/*) are the honest-absence contract;
        # anything else still fails the guard.
        page_errors.clear()
        failed_responses: list[str] = []
        page.on(
            "response",
            lambda r: failed_responses.append(f"{r.status} {r.url}")
            if r.status >= 400
            else None,
        )

        card_host = turn.locator(".evidence-card-host")
        card = card_host.locator(".evidence-card")

        if bound_count > 0:
            # ── Step 3 (Tier 2): bound chip opens the evidence card ──────
            bound_chips.first.click()
            expect(card).to_be_visible(timeout=UI_TIMEOUT_MS)

            # Raw-query leak check BEFORE Tier 3 is expanded — Tier 2 must
            # never show query text; the honest-absence banner is a PASS.
            card_text = card.inner_text()
            assert "SELECT " not in card_text, "raw SQL leaked into the Tier-2 card"
            assert "MATCH (" not in card_text, (
                "raw openCypher leaked into the Tier-2 card"
            )
            banner = card.locator(".evidence-unverified-banner")
            if banner.count():
                print("[tier2] honest 'No captured evidence for this claim' banner shown (PASS)")
            page.screenshot(path=CARD_SCREENSHOT, full_page=False)

            # ── Step 4 (Tier 3): expander settles into detail OR honesty ─
            tier3 = card.locator("details.evidence-tier3")
            expect(tier3).to_be_attached(timeout=UI_TIMEOUT_MS)
            tier3.locator("summary").click()
            # Terminal states: loaded query, honest not-captured note, or the
            # honest unavailable line — all PASS. Wait for any of them.
            settled = card.locator(
                ".evidence-executed-query, .evidence-query-absent, "
                ".evidence-tier3-unavailable"
            )
            expect(settled.first).to_be_visible(timeout=30_000)
            expect(card.locator(".evidence-tier3-loading")).to_have_count(
                0, timeout=30_000
            )
            print(f"[tier3] settled into: {settled.first.get_attribute('class')}")
            page.screenshot(path=TIER3_SCREENSHOT, full_page=False)

            # ── Step 5: close the card — it unmounts ─────────────────────
            card.locator('button[aria-label="Close evidence card"]').click()
            expect(card_host).to_have_count(0, timeout=UI_TIMEOUT_MS)
        else:
            # Honest-sparse legacy path: no bound record ⇒ the chip click
            # runs focusArchNode verbatim (thread-clicks.ts) — the card must
            # NOT open, the architecture view must, and nothing may crash.
            chips.first.click()
            expect(page.locator("#architecture-view")).to_be_visible(
                timeout=UI_TIMEOUT_MS
            )
            expect(card_host).to_have_count(0)
            page.screenshot(path=CARD_SCREENSHOT, full_page=False)
            page.screenshot(path=TIER3_SCREENSHOT, full_page=False)
            print(
                "[tier2/3] no provenance-bound chip on this turn — legacy "
                "architecture fallback verified instead (honest-sparse PASS)"
            )

        # ── Step 6: regression guard — inline cite badge click ───────────
        # Same delegated handler contract: an active-turn badge with a bound
        # record opens the card; otherwise the legacy architecture path runs.
        # Either outcome is a PASS; a crash / console error is the failure.
        # Inline badges can sit inside collapsed answer <details> sections
        # (collapseResponseSections) — pick a VISIBLE one; when none is
        # visible, a Sources chip exercises the identical delegated-handler
        # path (thread-clicks.ts routes both via a[data-cite-target]).
        visible_badges = inline_badges.filter(visible=True)
        if visible_badges.count() > 0:
            guard_target, guard_kind = visible_badges.first, "inline .cite-badge"
        else:
            guard_target, guard_kind = chips.first, "sources chip (no visible inline badge)"
        guard_target.click()
        page.wait_for_timeout(1_500)  # let the store dispatch / view switch settle
        card_open = card_host.count() > 0
        arch_open = page.locator("#architecture-view").is_visible()
        assert card_open or arch_open, (
            "cite-badge click neither opened the evidence card nor fell back "
            "to the architecture view"
        )
        print(
            f"[guard] {guard_kind} click outcome: evidence card={card_open}, "
            f"architecture fallback={arch_open}"
        )

        # Console guard: resource-load console.error entries are acceptable
        # ONLY when every failed response during the interactions was a
        # designed honest-absence status (403/404) on the D5 redemption
        # routes (api/provenance.ts: /api/admin/agents/traces/queries/ and
        # …/traces/datasets/). Any other failed URL, or any real JS error,
        # fails.
        unexpected_failures = [
            f
            for f in failed_responses
            if "/api/admin/agents/traces/" not in f
        ]
        assert unexpected_failures == [], (
            f"non-provenance HTTP failures during interactions: {unexpected_failures}"
        )
        real_errors = [
            e
            for e in page_errors
            if not (
                e.startswith("console.error: Failed to load resource")
                and failed_responses
                and not unexpected_failures
            )
        ]
        assert real_errors == [], (
            f"console/JS errors during provenance interactions: {real_errors} "
            f"(failed responses: {failed_responses})"
        )
        if failed_responses:
            print(
                f"[guard] designed honest-absence provenance responses: {failed_responses}"
            )
