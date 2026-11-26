# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin page (/admin.html) regression checks.

Requires an admin user (the demo `ADT` user is in the Admin group). These tests
lock in the fixes for two real bugs found in the live audit:

  * Clicking the **Thread** tab threw `ReferenceError: initAdminThread is not
    defined`, which aborted before `loadThreadOverview()` ran and left the
    "Loading knowledge graph" spinner stuck forever.
  * The Thread empty-state "Onboard a system" link called `switchView('onboard')`
    — a function that does not exist on admin.html — throwing on click.

Evals-tab DOM contract (ADR-0061 Phase 1, ADR-0062 Phase 2)
-----------------------------------------------------------
The Evals tab tests below assert PRESENCE and the safety-critical labels only.
Nothing here triggers billable or steward-authority work, by design:

  * `#evals-run-btn` ("Run full golden suite") is asserted present and is NEVER
    clicked — a full run is minutes long and billable (the UI confirm()-gates it
    for the same reason). Run-ALL-only also holds structurally: exactly one run
    affordance, and the Phase-2 panels add none.
  * `.evals-queue-verdict-btn` and `.evals-queue-draft-btn` are asserted present
    per item and NEVER clicked — a verdict writes the steward's decision (the
    steward is the sole verdict authority) and the draft button emits transient
    customer-derived data.
  * `#evals-findings-import-*`, `#evals-finding-file-proposal` and any audit-run
    affordance are never exercised — filing is never automatic, and the bounded
    triage annotator costs model calls against a daily budget.
  * `#evals-audit-status` is asserted MEASURED-OR-ABSENT: no money/estimate text
    and no fabricated zero usage (ADR-0053 cost rule).

Every selector asserted here is a stable hook in the shipped components
(`frontend/src/pages/admin/tabs/evals/{ReviewQueue,FindingsLedger}.tsx`); the
tests stay data-tolerant (the tab-demoscript precedent) so an environment with
no feedback and no findings renders its honest empty states instead of failing,
and an environment whose bundle/route predates a panel skips cleanly.
"""

from __future__ import annotations

import re
from typing import Callable

import pytest
from playwright.sync_api import Page, expect

# data-tab -> the panel id it activates.
ADMIN_TABS = ["tab-agents", "tab-settings", "tab-demoscript", "tab-thread", "tab-vocabulary", "tab-context", "tab-evals"]


@pytest.fixture
def admin_page(page_errors: list[str], login: Callable[[], Page], adt_base_url: str) -> Page:
    """Logged-in admin sitting on /admin.html.

    Depends on `page_errors` so its console/pageerror listeners are attached
    before any navigation happens. Skips if the demo user is not an admin.
    """
    page = login()
    me = page.request.get(f"{adt_base_url}/api/auth/me").json()
    if not me.get("is_admin"):
        pytest.skip("demo user is not an admin — skipping admin-only tests")
    page.goto(f"{adt_base_url}/admin.html")
    expect(page.locator("nav.tab-bar")).to_be_visible(timeout=20_000)
    expect(page.locator("#access-denied")).to_be_hidden()
    # The login landing page (index.html) starts a background /api/systems poll.
    # Navigating here can abort that in-flight fetch, surfacing a benign
    # "Failed to fetch" console error from the *previous* page. Drop setup-phase
    # noise once the admin page is READY so each test asserts only on the errors
    # produced by its own action. Readiness is the product DOM above, not
    # 'networkidle': the admin SPA refetches on a React Query interval, so
    # transport quiescence is not a product contract and can never settle
    # (same reasoning as test_steward_review.py). page.goto already awaited load.
    page_errors.clear()
    return page


@pytest.mark.e2e
@pytest.mark.parametrize("tab", ADMIN_TABS)
def test_admin_tab_activates_without_js_error(
    admin_page: Page, page_errors: list[str], tab: str
) -> None:
    """Every admin tab activates and raises no uncaught JS error on click.

    Regression: the Thread tab used to throw `initAdminThread is not defined`.
    """
    page = admin_page
    tab_btn = page.locator(f'.tab-bar button[data-tab="{tab}"]')
    # Data-tolerant: some tabs (e.g. demoscript) only apply with bundled demo
    # data; base-mode envs render them absent OR hidden.
    if tab_btn.count() == 0 or not tab_btn.is_visible():
        pytest.skip(f"admin tab '{tab}' not available in this environment")
    tab_btn.click()
    expect(page.locator(f"#{tab}")).to_be_visible(timeout=15_000)
    assert page_errors == [], f"JS errors after activating {tab}: {page_errors}"


@pytest.fixture
def evals_tab(admin_page: Page) -> Page:
    """The Evals tab, activated.

    Data-tolerant: mirror the demoscript pattern — environments serving an older
    frontend bundle render the tab absent OR hidden, and skip rather than fail.
    """
    tab_btn = admin_page.locator('.tab-bar button[data-tab="tab-evals"]')
    if tab_btn.count() == 0 or not tab_btn.is_visible():
        pytest.skip("admin tab 'tab-evals' not available in this environment")
    tab_btn.click()
    expect(admin_page.locator("#tab-evals")).to_be_visible(timeout=15_000)
    # EvalsTab mounts on first activation; #evals-run-form is in its first
    # render pass regardless of query state, so waiting for it means a later
    # panel-absence check reflects the BUNDLE, not a mount race.
    expect(admin_page.locator("#evals-run-form")).to_be_visible(timeout=15_000)
    return admin_page


def _skip_if_panel_absent(page: Page, panel_id: str, what: str) -> None:
    """Skip when a panel is not in this environment's bundle (an older bundle
    predates the Phase-2 panels), rather than failing the contract."""
    if page.locator(f"#{panel_id}").count() == 0:
        pytest.skip(f"{what} (#{panel_id}) not in this environment's frontend bundle")


def _first_visible_or_skip(page: Page, selector: str, scope_id: str, what: str) -> None:
    """Wait for the first of `selector` to show; skip if the panel instead
    rendered its own honest unavailable/alert state (a route this environment
    does not serve yet is an env gap, not a broken DOM contract)."""
    try:
        expect(page.locator(selector).first).to_be_visible(timeout=20_000)
    except AssertionError:
        alert = page.locator(f"#{scope_id} [role=alert]")
        if alert.count() and alert.first.is_visible():
            pytest.skip(f"{what} unavailable here: {alert.first.inner_text()[:160]}")
        raise


@pytest.mark.e2e
def test_evals_tab_shows_history_and_run_button(
    evals_tab: Page, page_errors: list[str]
) -> None:
    """The Evals tab renders the run-history table OR its empty state, and the
    'Run full golden suite' button is PRESENT.

    This test must NEVER click the run button — a full golden run takes
    minutes and is billable (the UI confirm()-gates it for the same reason).
    E2E asserts presence, not execution (the server lane is covered by
    unit/route tests; ADR-0061).
    """
    page = evals_tab
    # Run affordance: the form and its single run-ALL button are present.
    expect(page.locator("#evals-run-form")).to_be_visible(timeout=15_000)
    run_btn = page.locator("#evals-run-btn")
    expect(run_btn).to_be_visible()
    expect(run_btn).to_have_text("Run full golden suite")
    # History: once the runs query resolves, exactly one of the table
    # (#evals-history-table) or the empty state (#evals-history-empty) shows.
    # (If the query errors, neither renders — surfaced via the role=alert.)
    history = page.locator("#evals-history-table, #evals-history-empty")
    expect(history.first).to_be_visible(timeout=20_000)
    assert page_errors == [], f"JS errors on Evals tab: {page_errors}"


@pytest.mark.e2e
def test_evals_review_queue_renders_with_triage_only_prescore(
    evals_tab: Page, page_errors: list[str]
) -> None:
    """The review-queue panel renders (or its honest empty state), and any
    machine pre-score carries the literal 'triage-only' label.

    The pre-score orders attention and is NOT a verdict (ADR-0062): the steward
    is the sole verdict authority, so this test asserts the verdict buttons and
    'Download draft' are PRESENT and NEVER clicks them — a verdict click writes
    a steward decision and a draft click emits transient customer-derived data.
    """
    page = evals_tab
    _skip_if_panel_absent(page, "evals-queue-section", "review-queue panel")
    expect(page.locator("#evals-queue-section")).to_be_visible(timeout=15_000)
    # Computed-on-read counts line — present as soon as the panel mounts.
    expect(page.locator("#evals-queue-summary")).to_be_visible()
    # Resolved state: at least one item card, or the honest empty state.
    _first_visible_or_skip(
        page,
        "#evals-queue-empty, .evals-queue-item",
        "evals-queue-section",
        "review queue",
    )

    items = page.locator(".evals-queue-item")
    if items.count() == 0:
        # No flagged turns and no golden failures in the window — the panel says
        # so, and no mutating affordance exists at all (they are per-item).
        expect(page.locator("#evals-queue-empty")).to_be_visible()
        assert page.locator(".evals-queue-verdict-btn").count() == 0
        assert page.locator(".evals-queue-draft-btn").count() == 0
        assert page_errors == [], f"JS errors on the review queue: {page_errors}"
        return

    first = items.first
    prescore = first.locator(".evals-queue-prescore")
    expect(prescore).to_be_visible()
    prescore_text = prescore.inner_text()
    assert "triage-only" in prescore_text, (
        "machine pre-score must carry the literal 'triage-only' label "
        f"(ADR-0062 D4); got {prescore_text!r}"
    )
    # Sanity: a pre-score must never present itself as a decision.
    assert "verdict" not in prescore_text.lower(), (
        f"pre-score reads like a verdict: {prescore_text!r}"
    )
    # The steward's affordances are present — and stay unclicked.
    assert first.locator(".evals-queue-verdict-btn").count() == 3, (
        "expected the three pinned verdict buttons (correct / incorrect / "
        "needs_context) on a queue item"
    )
    assert first.locator(".evals-queue-draft-btn").count() == 1
    assert page_errors == [], f"JS errors on the review queue: {page_errors}"


@pytest.mark.e2e
def test_evals_findings_ledger_and_digest_render(
    evals_tab: Page, page_errors: list[str]
) -> None:
    """The findings section renders with its 'This week' digest and its ledger
    (or their honest empty states).

    Read-only: the import affordance stays CLOSED (no file is selected, nothing
    is uploaded) and no finding is expanded, so 'File proposal' — the one door a
    suggested mapping reaches storage through — is not even mounted.
    """
    page = evals_tab
    _skip_if_panel_absent(page, "evals-findings-section", "findings-ledger panel")
    expect(page.locator("#evals-findings-section")).to_be_visible(timeout=15_000)

    # Digest: the computed-on-read card, or its honest unavailable line.
    digest_states = page.locator(
        "#evals-findings-digest, #evals-findings-digest-unavailable"
    )
    expect(digest_states.first).to_be_visible(timeout=20_000)
    if page.locator("#evals-findings-digest").count():
        # Inside the card: taxonomy count chips, or the in-window empty state.
        counts = page.locator(
            "#evals-findings-digest-empty, #evals-findings-digest .evals-digest-count"
        )
        expect(counts.first).to_be_visible(timeout=20_000)

    # Ledger: taxonomy groups, the honest empty state, or the unavailable line.
    _first_visible_or_skip(
        page,
        "#evals-findings-ledger, #evals-findings-empty, #evals-findings-unavailable",
        "evals-findings-section",
        "findings ledger",
    )

    # The import affordance exists and is closed: its inputs are unmounted.
    import_toggle = page.locator("#evals-findings-import-toggle")
    expect(import_toggle).to_be_visible()
    expect(import_toggle).to_have_text("Import findings")
    assert page.locator("#evals-findings-file").count() == 0, (
        "the import form must start closed — this test never imports a file"
    )
    assert page.locator("#evals-findings-report").count() == 0
    assert page.locator("#evals-findings-import-btn").count() == 0
    # Nothing expanded ⇒ no resolved detail and no filing affordance.
    assert page.locator("#evals-finding-detail").count() == 0
    assert page.locator("#evals-finding-file-proposal").count() == 0, (
        "'File proposal' must only exist inside an expanded finding detail"
    )
    assert page_errors == [], f"JS errors on the findings ledger: {page_errors}"


# Money/estimate shapes that must never appear in the annotator status line:
# cost is MEASURED-ONLY and never in dollars (ADR-0053).
_MONEY_NEEDLES = ("$", "usd", "dollar", "price", "estimat", "per 1k", "per-token")


@pytest.mark.e2e
def test_evals_audit_status_is_measured_or_absent(
    evals_tab: Page, page_errors: list[str]
) -> None:
    """The bounded-triage-annotator status line carries no money/estimate text
    and no fabricated zero usage.

    Measured-or-absent: with no measured usage the panel renders NO usage
    element at all. 'Never ran' is an explicit state. Triggering a run is not a
    control here, and this test presses nothing.
    """
    page = evals_tab
    _skip_if_panel_absent(page, "evals-audit-status", "annotator status panel")
    status = page.locator("#evals-audit-status")
    expect(status).to_be_visible(timeout=15_000)
    # One of: never-ran, the runs list, or the honest unavailable line.
    states = page.locator(
        "#evals-audit-never-ran, #evals-audit-runs, #evals-audit-status-unavailable"
    )
    expect(states.first).to_be_visible(timeout=20_000)

    text = status.inner_text().lower()
    for needle in _MONEY_NEEDLES:
        assert needle not in text, (
            f"annotator status must not carry money/estimate text; found "
            f"{needle!r} in {text!r}"
        )

    usages = page.locator("#evals-audit-status .evals-audit-run-usage")
    for i in range(usages.count()):
        usage_text = usages.nth(i).inner_text()
        assert "measured tokens" in usage_text, (
            f"usage must be labelled measured; got {usage_text!r}"
        )
        assert not re.search(r"tokens:\s*0\s+in", usage_text), (
            "measured-or-absent: an unmeasured run renders NO usage element — "
            f"never a zero; got {usage_text!r}"
        )
    assert page_errors == [], f"JS errors on the annotator status: {page_errors}"


@pytest.mark.e2e
def test_evals_tab_stays_run_all_only(evals_tab: Page) -> None:
    """Run-ALL-only still holds with the Phase-2 panels mounted.

    Exactly ONE golden-suite run affordance on the tab, it lives outside the
    Phase-2 panels, and the run form carries no subset control — the optional
    variant label is its only input (ADR-0061).
    """
    page = evals_tab
    tab = page.locator("#tab-evals")
    assert tab.locator("#evals-run-btn").count() == 1, (
        "exactly one golden-suite run affordance may exist on the Evals tab"
    )
    for panel_id in ("evals-queue-section", "evals-findings-section"):
        panel = tab.locator(f"#{panel_id}")
        if panel.count() == 0:
            continue
        assert panel.locator("#evals-run-btn").count() == 0, (
            f"#{panel_id} must not host the golden-suite run affordance"
        )
        assert panel.get_by_text("Run full golden suite").count() == 0, (
            f"#{panel_id} must not add a golden-suite run control"
        )
    form = tab.locator("#evals-run-form")
    expect(form).to_be_visible()
    assert form.locator("select").count() == 0, "no golden-subset selector may exist"
    assert form.locator("input").count() == 1, (
        "the optional variant label is the only input on the run form"
    )
    assert form.locator("input").first.get_attribute("type") == "text"
    assert form.locator("button").count() == 1, (
        "the run form carries exactly one button (run ALL) — never clicked here"
    )


@pytest.mark.e2e
def test_thread_tab_clears_loading_spinner(
    admin_page: Page, page_errors: list[str]
) -> None:
    """Thread tab runs loadThreadOverview(): the loading spinner is removed and
    the resolved state (empty-state with zero systems) is shown."""
    page = admin_page
    page.click('.tab-bar button[data-tab="tab-thread"]')
    # loadThreadOverview() removes #thread-loading once the systems/concepts
    # fetches resolve — on the old (broken) code it never ran.
    expect(page.locator("#thread-loading")).to_have_count(0, timeout=20_000)
    assert page_errors == [], f"JS errors on Thread tab: {page_errors}"


@pytest.mark.e2e
def test_thread_empty_state_onboard_link_is_admin_safe(admin_page: Page) -> None:
    """The Thread empty-state 'Onboard a system' link must not rely on the
    SPA-only switchView() — on admin.html it should point at /#onboard."""
    page = admin_page
    page.click('.tab-bar button[data-tab="tab-thread"]')
    empty = page.locator("#thread-empty")
    # With systems onboarded the empty-state is hidden and there is no link.
    if empty.is_visible():
        link = empty.locator("a")
        if link.count():
            href = link.first.get_attribute("href") or ""
            assert "#onboard" in href, f"onboard link href not admin-safe: {href!r}"
            assert href != "#", "onboard link still points at bare '#'"


@pytest.mark.e2e
def test_hidden_quick_links_stay_hidden_when_unconfigured(admin_page: Page) -> None:
    """Quick-links that are only valid when their URL is configured stay hidden
    (so they are never shown as dead '#' links)."""
    page = admin_page
    for link_id in ("link-highbyte", "link-erp-dashboard", "link-console-start", "link-i3x-highbyte", "link-i3x-sitewise"):
        loc = page.locator(f"#{link_id}")
        href = loc.get_attribute("href") or ""
        # Either configured with a real URL, or hidden. Never a visible dead link.
        if href in ("", "#"):
            expect(loc).to_be_hidden()
