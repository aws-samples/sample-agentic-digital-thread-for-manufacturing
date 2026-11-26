# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""End-to-end verification of the S5.4 dashboard restoration.

Codifies the browser-level contract for the reference-style dashboard rebuilt on
the dynamic engine (server ``drill`` hint + client layout/modal/diagnostics):

  1. LAYOUT — the reference shell is present: the 2-column Operations Summary +
     AI Operations Briefing top row, the KPI row, the bottom OEE-gauge + Line
     State metrics row, and the Live Factory Events footer.
  2. NO INLINE TABLE LEAK — ``#entity-sections`` no longer paints entity tables
     onto the page; it is the hidden modal cache. This is the regression that
     motivated the work (tables used to spill inline instead of living behind a
     card click).
  3. CLICK-TO-DRILL — a KPI card carrying the server ``drill`` hint is clickable
     and opens the detail modal with that entity's table (data-tolerant: skips
     when no drillable card is present in the target environment).
  4. DIAGNOSTIC MODE — ``?diag=1`` reveals the "why didn't this render" traces
     (``.diag-note`` / ``.diag-badge``) for cards and panels.

Data-driven checks degrade to ``skip`` (never fail) when the target environment
has nothing onboarded, mirroring the presence-driven philosophy of the app and
the "gated by skip, never a hard failure" pattern of the rest of the suite.

Runs against a *deployment that includes these changes* — set ``ADT_BASE_URL`` /
``ADT_PASSWORD`` (see ``conftest.py`` / ``tests/harness``). Skips cleanly with no
running app or no password.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_dashboard_shell_layout(authed_page: Page, page_errors: list[str]) -> None:
    """The reference-style shell renders with no uncaught JS errors."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)

    # Top row: Operations Summary (1fr) + AI Briefing (2fr) in a 2-column grid.
    ops = page.locator("#ops-summary-card")
    briefing = page.locator("#ai-briefing-card")
    expect(ops).to_be_visible()
    expect(briefing).to_be_visible()
    grid_cols = page.eval_on_selector(
        "#ops-summary-card",
        "el => getComputedStyle(el.parentElement).gridTemplateColumns",
    )
    tracks = grid_cols.split()
    assert len(tracks) == 2, f"top row is not a 2-column grid: {grid_cols!r}"
    # The right (briefing) track is wider than the left (summary) track — the 1fr/2fr split.
    assert float(tracks[1].rstrip("px")) > float(tracks[0].rstrip("px")), (
        f"briefing track should be wider than summary track: {grid_cols!r}"
    )

    # KPI row + bottom metrics row (OEE gauge slot + Line State) + live-feed footer.
    expect(page.locator(".kpi-row")).to_be_attached()
    expect(page.locator("#metrics-row")).to_be_attached()
    expect(page.locator("#metrics-oee")).to_be_attached()
    expect(page.locator("#line-state-card")).to_be_visible()
    for sel in ("#ls-running", "#ls-idle", "#ls-down"):
        expect(page.locator(sel)).to_be_attached()
    # Live Factory Events: the contractual nodes are ALWAYS attached, but the
    # panel self-hides (display:none) until event data actually flows
    # (empty-env UX gate in LiveFeed.tsx). Data-tolerant: visibility must
    # simply be consistent with the presence of feed lines.
    expect(page.locator("#live-feed-panel")).to_be_attached()
    expect(page.locator("#live-feed")).to_be_attached()
    if page.locator("#live-feed .feed-line").count() > 0:
        expect(page.locator("#live-feed-panel")).to_be_visible()

    # Awaiting-data strip (when present) renders at the BOTTOM of the view —
    # below the metrics row (deliberate deviation from the legacy
    # after-KPI-row anchor: empty-env UX, gap chips must not dominate the top).
    if page.locator("#uncovered-strip").count() > 0:
        strip_top = page.eval_on_selector(
            "#uncovered-strip", "el => el.getBoundingClientRect().top + window.scrollY"
        )
        metrics_top = page.eval_on_selector(
            "#metrics-row", "el => el.getBoundingClientRect().top + window.scrollY"
        )
        assert strip_top > metrics_top, (
            f"awaiting-data strip should sit below the metrics row "
            f"(strip y={strip_top}, metrics y={metrics_top})"
        )

    # Drill modal is closed on load. The React DetailModal renders through a
    # portal ONLY while open (conditional mount — pinned by
    # inv-drilldown-modal unit tests), so closed == not in the DOM at all.
    overlay = page.locator("#detail-modal-overlay")
    expect(overlay).to_have_count(0)

    assert page_errors == [], f"unexpected JS errors on dashboard: {page_errors}"


@pytest.mark.e2e
def test_entity_tables_do_not_leak_inline(authed_page: Page) -> None:
    """Regression guard: entity table data lives behind the drill modal, NOT
    painted inline. ``#entity-sections`` is the hidden cache — it must never
    render a visible <table> onto the dashboard."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)
    # Sections are loaded only after a table-drill click; the hidden anchor remains.
    host = page.locator("#entity-sections")
    expect(host).to_be_attached()
    # The cache anchor is display:none, so nothing inside it is visible even if present.
    assert page.eval_on_selector(
        "#entity-sections", "el => getComputedStyle(el).display === 'none'"
    ), "#entity-sections must stay hidden (modal cache, not an inline table dump)"
    # And it paints no table rows inline (renderEntitySections no longer appends DOM).
    inline_tables = page.eval_on_selector_all("#entity-sections table", "els => els.length")
    assert inline_tables == 0, f"entity tables leaked inline: {inline_tables} table(s) found"


@pytest.mark.e2e
def test_click_to_drill_opens_modal(authed_page: Page, page_errors: list[str]) -> None:
    """A KPI card with a server drill hint opens the entity table in the modal.

    Data-tolerant: when the target environment has no drillable card (nothing
    onboarded resolves to a physical table), this skips rather than fails."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)
    # Cards + drill hints arrive via the SSE snapshot; wait for at least one card.
    try:
        page.wait_for_selector('[id^="kpi-card-"]', timeout=20_000)
    except Exception:
        pytest.skip("no KPI cards rendered — nothing onboarded in this environment")

    # Target a TABLE-drill card specifically: gauge cards are also `.drillable`
    # but carry the 'choose for breakdown' hint (custom OEE drill), and in a
    # base/pilot env the first drillable card can be the gauge.
    drillable = page.locator(".kpi-card.drillable").filter(
        has=page.locator(".kpi-card-hint", has_text="choose to drill in")
    )
    if drillable.count() == 0:
        pytest.skip("no table-drillable KPI card (no metric resolved to a physical table)")

    card = drillable.first
    expect(card.locator(".kpi-card-hint")).to_have_text("choose to drill in")
    card.click()

    overlay = page.locator("#detail-modal-overlay")
    expect(overlay).to_have_class(re.compile(r"\bopen\b"), timeout=10_000)
    expect(page.locator("#detail-modal-title")).not_to_be_empty()
    # Body shows either a real table or an honest fail-soft note — never blank.
    body_text = page.locator("#detail-modal-body").inner_text().strip()
    has_table = page.eval_on_selector_all("#detail-modal-body table", "els => els.length") > 0
    assert has_table or body_text, "drill modal body is empty (expected a table or a note)"

    # Close unmounts the portal-rendered modal (conditional mount).
    page.click(".detail-modal-close")
    expect(overlay).to_have_count(0, timeout=5_000)

    assert page_errors == [], f"unexpected JS errors during drill: {page_errors}"


@pytest.mark.e2e
def test_diagnostic_mode_reveals_traces(
    authed_page: Page, adt_base_url: str, page_errors: list[str]
) -> None:
    """``?diag=1`` turns on diagnostic mode: body.diag is set and the diagnostic
    badges/notes become visible so an operator can step through why a card or
    panel did not render."""
    page = authed_page
    page.goto(f"{adt_base_url}/index.html?diag=1")
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)

    # body.diag is set by initDiagMode() inside the async boot callback
    # (requestAnimationFrame) — poll rather than assert instantly, since
    # #dashboard-view is visible from raw HTML before the boot JS runs.
    try:
        page.wait_for_function(
            "document.body.classList.contains('diag')", timeout=10_000
        )
    except Exception:
        raise AssertionError(
            "?diag=1 did not enable diagnostic mode (body.diag missing after 10s)"
        )
    # The Line State panel carries a diag badge that is display:none until diag mode.
    badge_visible = page.eval_on_selector(
        "#line-state-card .diag-badge",
        "el => getComputedStyle(el).display !== 'none'",
    )
    assert badge_visible, "diag badge did not reveal under ?diag=1"

    # toggleDiag() flips it back off (guards the toggle affordance).
    assert page.evaluate("toggleDiag()") is False
    assert not page.evaluate("document.body.classList.contains('diag')")

    # Transient boot-time fetch failures (CloudFront jitter) surface as
    # 'Systems load error: TypeError: Failed to fetch' console noise via the
    # app's fail-soft catch — unrelated to diagnostic mode. Keep asserting on
    # everything else (real uncaught errors must still fail).
    real_errors = [e for e in page_errors if "Failed to fetch" not in e]
    assert real_errors == [], f"unexpected JS errors in diagnostic mode: {real_errors}"


# --------------------------------------------------------------------------- #
# Dashboard UI/UX fixes (Issues 1–5) — env-gated, data-tolerant.
# --------------------------------------------------------------------------- #
@pytest.mark.e2e
def test_no_bar_cards_render_as_unavailable_kpi(authed_page: Page) -> None:
    """Issue 1 regression guard — the by-dimension BAR cards (`machine-state`,
    `quality-by-line`) must NEVER render as broken yellow "Unavailable" KPI tiles.
    They belong in the Line State / quality panels, not the KPI row."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(3_000)  # let the SSE snapshot + any card_update settle
    # The two bar cards must have NO KPI tile in the row.
    assert page.locator("#kpi-card-machine-state").count() == 0, (
        "machine-state rendered as a KPI tile (should be the Line State panel)"
    )
    assert page.locator("#kpi-card-quality-by-line").count() == 0, (
        "quality-by-line rendered as a KPI tile (should be the quality panel)"
    )


@pytest.mark.e2e
def test_line_state_per_site_groups(authed_page: Page) -> None:
    """Issue 2 — Line State aggregates across sites: a dynamic systems badge plus
    per-site groups of color-coded line chips. Data-tolerant: skips when no MES is
    onboarded / machine-state is unmapped in the target environment."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(3_000)  # after-first-paint line-state fetch
    badge = page.locator("#ls-systems-badge")
    expect(badge).to_be_attached()
    groups = page.locator("#ls-sites .line-state-site-group")
    if groups.count() == 0:
        pytest.skip("no line-state site groups (machine-state unmapped in this env)")
    assert badge.inner_text().strip(), "systems badge is empty despite site groups"
    # At least one group carries color-coded line chips.
    assert page.locator("#ls-sites .line-state-line-chip").count() >= 1, (
        "site groups present but no line chips rendered"
    )


@pytest.mark.e2e
def test_quality_panel_has_context_subtitle(authed_page: Page) -> None:
    """Issue 3 — the quality-by-line panel carries a context subtitle (source
    system·site + what the % means). Data-tolerant: skips when the panel is hidden
    (no quality-by-line data in the target environment)."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)
    # The panel loads via the staggered lazy panel loaders after the first SSE
    # snapshot (~6s cold against CloudFront) — poll up to 15s instead of a fixed
    # 3s sleep (which raced the loader and skipped on healthy envs).
    visible = False
    for _ in range(15):
        page.wait_for_timeout(1_000)
        visible = page.eval_on_selector(
            "#quality-by-line-panel", "el => getComputedStyle(el).display !== 'none'"
        )
        if visible:
            break
    panel = page.locator("#quality-by-line-panel")
    if not visible:
        pytest.skip("quality-by-line panel hidden (no data in this env)")
    # Visibility flips as soon as the LOADING skeleton renders — the subtitle
    # arrives only after the resolve-by-dimension fetch stores its payload
    # (SWR loader). Wait for the LOADED state: either data bars (subtitle
    # contract applies) or the muted resolve-error note (no system maps
    # quality-yield-rate in this env — data-tolerant skip, e.g. pilot envs).
    for _ in range(30):
        if panel.locator(".dim-bar-subtitle").count() > 0:
            break
        if panel.locator(".dim-note, .detail-modal-empty, .async-state--error").count() > 0:
            pytest.skip("quality-by-line metric not resolvable in this env")
        page.wait_for_timeout(1_000)
    if (
        panel.locator(".dim-bar-row").count() == 0
        and panel.locator(".dim-bar-subtitle").count() == 0
    ):
        pytest.skip("quality-by-line produced no data rows in this env")
    expect(panel.locator(".dim-bar-subtitle")).to_have_count(1)
    assert "pass rate" in panel.locator(".dim-bar-subtitle").inner_text().lower()


@pytest.mark.e2e
def test_kpi_asof_pinned_and_target_on_own_line(authed_page: Page) -> None:
    """Issue 4 — the "as of" stamp is absolutely positioned (fixed corner) and the
    target readout is its own `.kpi-target` block (not inline in `.kpi-value`).
    Data-tolerant: skips when no resolved card carries an as-of / target."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)
    try:
        page.wait_for_selector('[id^="kpi-card-"]', timeout=20_000)
    except Exception:
        pytest.skip("no KPI cards rendered — nothing onboarded in this environment")
    page.wait_for_timeout(2_000)

    asof = page.locator(".kpi-asof")
    if asof.count() > 0:
        pos = page.eval_on_selector(".kpi-asof", "el => getComputedStyle(el).position")
        assert pos == "absolute", f".kpi-asof should be absolutely pinned, got {pos!r}"

    target = page.locator(".kpi-target")
    if target.count() == 0:
        pytest.skip("no card with a numeric target in this environment")
    # The target is a block-level sibling of .kpi-value, never nested inside it.
    nested = page.eval_on_selector_all(".kpi-value .kpi-target", "els => els.length")
    assert nested == 0, "target readout is still nested inside .kpi-value (wraps awkwardly)"


@pytest.mark.e2e
def test_oee_drill_shows_labeled_breakdown(authed_page: Page, page_errors: list[str]) -> None:
    """Issue 5 — clicking the OEE gauge opens a labeled breakdown table
    (`.oee-drill-table`), not the generic raw-ID entity table. Data-tolerant:
    skips when the OEE card is absent (OEE unmapped in the target environment)."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)
    oee = page.locator("#kpi-card-oee")
    try:
        oee.wait_for(timeout=20_000)
    except Exception:
        pytest.skip("no OEE card rendered — OEE unmapped in this environment")

    oee.click()
    overlay = page.locator("#detail-modal-overlay")
    expect(overlay).to_have_class(re.compile(r"\bopen\b"), timeout=10_000)
    # Data-tolerant: openOeeDrill renders an honest `.detail-modal-empty` note
    # when no OEE components resolved in this environment (e.g. base/pilot envs).
    if page.locator("#detail-modal-body .detail-modal-empty").count() > 0:
        pytest.skip("no OEE components resolved in this environment")
    expect(page.locator("#detail-modal-body .oee-drill-table")).to_have_count(1)
    # Header names the contributing dimensions in human terms (no raw IDs).
    header = page.locator("#detail-modal-body .oee-drill-table thead").inner_text().lower()
    assert "measurement" in header and "source" in header

    page.click(".detail-modal-close")
    expect(overlay).to_have_count(0, timeout=5_000)
    assert page_errors == [], f"unexpected JS errors during OEE drill: {page_errors}"
