# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Authenticated SPA smoke checks — dashboard health + main navigation.

These guard against the two failure modes seen in the wild:
  * a 504 / Bad Gateway between CloudFront, the ALB and the ECS task, and
  * uncaught JS exceptions that wedge a view (e.g. an undefined init function).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

# Header nav buttons -> the view container they activate.
NAV = [
    ("#dashboard-btn", "#dashboard-view"),
    ("#architecture-btn", "#architecture-view"),
    ("#threads-btn", "#threads-view"),
    ("#onboard-btn", "#onboard-view"),
]

_GATEWAY_ERRORS = ("Gateway Timeout", "Bad Gateway", "504 ERROR", "502 ERROR")


def _assert_no_gateway_error(page: Page) -> None:
    body = page.locator("body").inner_text()
    for marker in _GATEWAY_ERRORS:
        assert marker not in body, f"gateway/proxy error page detected: {marker!r}"


@pytest.mark.e2e
def test_dashboard_renders_without_gateway_error(
    authed_page: Page, page_errors: list[str]
) -> None:
    """The default dashboard view renders real chrome, no 504, no uncaught JS."""
    page = authed_page
    expect(page.locator("#dashboard-view")).to_be_visible(timeout=20_000)
    # The persistent "AI Operations Briefing" card is part of the dashboard shell.
    # (Renamed from the older #insights-content in the intelligence-panel redesign.)
    expect(page.locator("#ai-briefing-card")).to_be_visible()
    _assert_no_gateway_error(page)
    assert page_errors == [], f"unexpected JS errors on dashboard: {page_errors}"


@pytest.mark.e2e
@pytest.mark.parametrize("button,view", NAV, ids=[v.lstrip("#") for _, v in NAV])
def test_main_views_load(authed_page: Page, button: str, view: str) -> None:
    """Each primary nav view activates and shows no gateway/proxy error."""
    page = authed_page
    page.click(button)
    expect(page.locator(view)).to_be_visible(timeout=15_000)
    _assert_no_gateway_error(page)


@pytest.mark.e2e
def test_navigation_round_trip_has_no_uncaught_errors(
    authed_page: Page, page_errors: list[str]
) -> None:
    """Click through every primary view in sequence and assert no JS exceptions."""
    page = authed_page
    for button, view in NAV:
        page.click(button)
        expect(page.locator(view)).to_be_visible(timeout=15_000)
    assert page_errors == [], f"uncaught JS errors during navigation: {page_errors}"


@pytest.mark.e2e
def test_graph_libraries_load_lazily_with_the_architecture_view(
    authed_page: Page, page_errors: list[str], adt_base_url: str
) -> None:
    """The graph stack (cytoscape + cola + popper, ~177 KB gzip) is bundled and
    reached ONLY through the lazy Architecture view: no entry page preloads it,
    and opening the view actually renders a cytoscape canvas.

    Replaces the retired ``test_vendored_libraries_loaded``, which asserted the
    legacy ``window.marked/DOMPurify/cytoscape/Popper/tippy`` globals that the
    globals-shim attached; the shim is gone (every consumer imports its
    library) and those globals are intentionally undefined now."""
    page = authed_page
    preloads = page.evaluate(
        "Array.from(document.querySelectorAll('link[rel=modulepreload]')).map(l => l.href)"
    )
    graph_preloads = [href for href in preloads if "vendor-graph-" in href]
    assert graph_preloads == [], f"entry page preloads the graph chunk: {graph_preloads}"
    login_html = page.request.get(f"{adt_base_url}/login.html").text()
    assert "vendor-graph-" not in login_html, "login page must not reference the graph chunk"

    page.click("#architecture-btn")
    expect(page.locator("#architecture-view")).to_be_visible(timeout=15_000)
    # cytoscape creates its canvas layers inside the graph container on init.
    expect(page.locator("#architecture-view canvas").first).to_be_attached(timeout=20_000)
    assert page_errors == [], f"uncaught JS errors opening the architecture view: {page_errors}"
