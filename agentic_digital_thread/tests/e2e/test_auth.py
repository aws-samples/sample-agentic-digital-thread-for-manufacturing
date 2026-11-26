# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Login flow checks — require ADT_PASSWORD (auto-skip when unset)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_invalid_credentials_show_error(
    page: Page,
    adt_base_url: str,
    adt_username: str,
    adt_password: str,
    adt_edge_auth: bool,
) -> None:
    """The edge-off app form surfaces a bad-password error inline."""
    if adt_edge_auth:
        pytest.skip("inline #error-msg belongs to the edge-off /login.html form")
    page.goto(f"{adt_base_url}/login.html")
    page.fill("#email", adt_username)
    page.fill("#password", "wrong-password-for-test")
    page.click("#submit-btn")

    # The error div is display:none until the JS adds the `.visible` class on 401.
    expect(page.locator("#error-msg")).to_be_visible(timeout=15_000)
    assert "/login.html" in page.url


@pytest.mark.e2e
def test_valid_login_sets_session_and_enters_app(
    authed_page: Page, adt_edge_auth: bool
) -> None:
    """A successful login lands in the app with the selected session type."""
    assert "/login.html" not in authed_page.url
    cookie_names = {c["name"] for c in authed_page.context.cookies()}
    if adt_edge_auth:
        assert any(
            name.startswith("CognitoIdentityServiceProvider.") for name in cookie_names
        ), f"edge-issued Cognito cookies missing; got {cookie_names}"
        expect(authed_page.locator("#login-form")).not_to_be_visible()
    else:
        assert "adt_session" in cookie_names, f"adt_session cookie missing; got {cookie_names}"
    # The authenticated shell exposes the logout control.
    expect(authed_page.locator("#logout-btn")).to_be_attached()


@pytest.mark.e2e
def test_me_endpoint_returns_logged_in_user(
    authed_page: Page, adt_base_url: str, adt_username: str, adt_oidc_only: bool
) -> None:
    """/api/auth/me reflects the authenticated user after login."""
    resp = authed_page.request.get(f"{adt_base_url}/api/auth/me")
    assert resp.status == 200, resp.text()
    if adt_oidc_only:
        # The federated identity signs in, not the demo Cognito user.
        assert resp.json().get("username"), resp.json()
    else:
        assert resp.json().get("username") == adt_username
