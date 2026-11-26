# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Public (unauthenticated) endpoint checks — no credentials required."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_health_ok(page: Page, adt_base_url: str) -> None:
    """/health is public and reports status ok with a version string."""
    resp = page.request.get(f"{adt_base_url}/health")
    assert resp.status == 200, resp.text()
    body = resp.json()
    assert body.get("status") == "ok"
    assert body.get("version"), "expected a non-empty version field"


@pytest.mark.e2e
def test_branding_public(page: Page, adt_base_url: str) -> None:
    """/api/branding is public and returns a JSON object."""
    resp = page.request.get(f"{adt_base_url}/api/branding")
    assert resp.status == 200, resp.text()
    assert isinstance(resp.json(), dict)


@pytest.mark.e2e
@pytest.mark.parametrize(
    "path",
    [
        "/css/fonts.css",
    ],
)
def test_vendored_fonts_follow_browser_auth_boundary(
    page: Page, adt_base_url: str, path: str, adt_edge_auth: bool
) -> None:
    """Static browser assets follow the configured browser-entry boundary.

    The web fonts are hashed Vite assets under /assets/; fonts.css is the
    remaining static stylesheet.
    Edge-on deployments protect it with the default CheckAuth behavior. The
    edge-off login form needs it before authentication, so middleware
    continues to serve it directly in that mode.
    """
    resp = page.request.get(f"{adt_base_url}{path}", max_redirects=0)
    if adt_edge_auth:
        assert resp.status == 307, f"expected Cognito redirect for {path}, got {resp.status}"
        location_host = urlsplit(resp.headers.get("location", "")).hostname or ""
        assert location_host.endswith(".amazoncognito.com"), resp.headers
    else:
        assert resp.status == 200, f"{path} not publicly served (status {resp.status})"


@pytest.mark.e2e
def test_protected_api_requires_auth(page: Page, adt_base_url: str) -> None:
    """A protected API route returns 401 without a session cookie."""
    resp = page.request.get(f"{adt_base_url}/api/version")
    assert resp.status == 401, f"expected 401 unauthenticated, got {resp.status}"


@pytest.mark.e2e
def test_root_uses_configured_browser_login(
    page: Page, adt_base_url: str, adt_edge_auth: bool, adt_oidc_only: bool
) -> None:
    """Unauthenticated browser navigation reaches the configured login surface."""
    page.goto(f"{adt_base_url}/")
    if adt_oidc_only:
        # --oidc-only: Cognito forwards straight to the external IdP — the
        # browser must leave the app and never see the local login form.
        host = urlsplit(page.url).hostname or ""
        assert host and host != urlsplit(adt_base_url).hostname, page.url
        assert "/login.html" not in page.url
    elif adt_edge_auth:
        host = urlsplit(page.url).hostname or ""
        assert host.endswith(".amazoncognito.com"), page.url
        expect(page.locator("#login-form")).not_to_be_visible()
    else:
        expect(page).to_have_url(f"{adt_base_url}/login.html")
        expect(page.locator("#login-form")).to_be_visible()
