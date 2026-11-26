# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shared fixtures for the ADT demo end-to-end browser suite.

The suite runs against a *running* deployment (the CloudFront URL of a
`cdk deploy`d stack, or a local `uvicorn` instance). Configuration is supplied
entirely through environment variables so the same tests run against any
environment without code changes:

    ADT_BASE_URL   Base URL of the app, no trailing slash.
                  Default: http://localhost:8000
    ADT_USERNAME   Demo Cognito username. Default: ADT
    ADT_PASSWORD   Demo password. NOT defaulted — tests that require auth are
                  skipped when it is unset, so the suite never embeds a secret.
    ADT_EDGE_AUTH  Browser authentication mode: on (Cognito hosted UI), off
                  (the app-served login form), or oidc (edge auth whose ONLY
                  sign-in is an external OIDC IdP — ``--oidc-only on``; the
                  browser is seeded with that IdP's session cookies from
                  ADT_OIDC_COOKIE_FILE, a Netscape cookie file exported by
                  your IdP's CLI sign-in; required in this mode). Default: on.

Retrieve the deployed values with:

    aws cloudformation describe-stacks --stack-name ADTComputeStack \\
        --query "Stacks[0].Outputs[?OutputKey=='ADTCloudFrontUrl'].OutputValue" \\
        --output text
    aws secretsmanager get-secret-value --secret-id adt-demo-password \\
        --query SecretString --output text
"""

from __future__ import annotations

import os
import time
from typing import Callable
from urllib.parse import urlsplit

import importlib.util

import pytest

# Gate the whole browser suite on the optional `test` extra being installed
# (`pip install -e '.[test]'`). Without pytest-playwright the `page` fixture does
# not exist, so the suite would ERROR at setup. Collect-ignore it instead so a
# bare `pytest` SKIPS it cleanly — mirroring the "gated by skip, never a hard
# failure" pattern the integration/audit/e2e-auth lanes already follow.
# (`from __future__ import annotations` makes the `Page` annotations strings, so
# the fixtures define fine even when the import below is skipped.)
if importlib.util.find_spec("pytest_playwright") is None:
    collect_ignore_glob = ["*"]
else:
    from playwright.sync_api import Page, expect

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_USERNAME = "ADT"


@pytest.fixture(scope="session")
def adt_base_url() -> str:
    """Base URL of the deployment under test (no trailing slash).

    Reachability policy, and the distinction MATTERS:

    * No ``ADT_BASE_URL`` — the caller is running a bare ``pytest`` with no
      server in mind, so an unreachable default localhost SKIPS. That keeps
      the offline lane green, which is the documented behaviour.
    * ``ADT_BASE_URL`` IS set — the caller deliberately targeted a
      deployment, so an unreachable app is a HARD FAILURE, never a skip.

    Why the split: a skip here silently skipped the whole browser suite and
    the lane still exited 0. That false green was hit twice — once with the
    env file present but not exported (51 skipped, exit 0), and once when a
    slow-but-alive app missed a 3 s probe immediately after a prior run
    (58 skipped in 3.24 s, exit 0). A verification lane that can report
    success having executed NOTHING is worse than one that fails.

    The probe also retries: a deployed app under load is slow, not absent,
    and a single short timeout cannot tell those apart.
    """
    import urllib.request

    explicit = bool(os.environ.get("ADT_BASE_URL"))
    url = os.environ.get("ADT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    last = None
    for attempt in range(3):
        try:
            urllib.request.urlopen(  # nosec B310 - internal health probe
                f"{url}/health", timeout=15)
            return url
        except Exception as exc:  # noqa: BLE001 - reachability, not correctness
            last = exc
            if attempt < 2:
                time.sleep(3)
    detail = f"{type(last).__name__}: {last}"
    if explicit:
        pytest.fail(
            f"ADT_BASE_URL={url} was set explicitly but /health did not answer "
            f"after 3 attempts ({detail}). Refusing to skip: that would report "
            f"a green browser lane having run zero tests. Check the deployment, "
            f"or unset ADT_BASE_URL to run the offline lane."
        )
    pytest.skip(f"no running app at {url} — start uvicorn or set ADT_BASE_URL")


def _edge_auth_mode() -> str:
    value = os.environ.get("ADT_EDGE_AUTH", "on").strip().lower()
    if value not in {"on", "off", "oidc"}:
        pytest.fail("ADT_EDGE_AUTH must be 'on', 'off' or 'oidc'")
    return value


@pytest.fixture(scope="session")
def adt_edge_auth() -> bool:
    """Whether browser entry is protected by Cognito Authorization@Edge
    (``on`` and ``oidc`` both are; ``oidc`` only changes WHO signs in)."""
    return _edge_auth_mode() != "off"


@pytest.fixture(scope="session")
def adt_oidc_only() -> bool:
    """Edge auth whose only sign-in is an external OIDC identity provider."""
    return _edge_auth_mode() == "oidc"


def _netscape_cookies(path: str) -> list[dict]:
    """Parse a Netscape cookie file into Playwright cookie dicts (values are
    never logged). ``#HttpOnly_`` prefixed lines are real cookies."""
    cookies: list[dict] = []
    with open(os.path.expanduser(path), encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            http_only = line.startswith("#HttpOnly_")
            if not line or (line.startswith("#") and not http_only):
                continue
            parts = line.replace("#HttpOnly_", "", 1).split("\t")
            if len(parts) < 7:
                continue
            domain, _sub, cpath, secure, expires, name, value = parts[:7]
            cookie = {"name": name, "value": value, "expires": int(expires or -1),
                      "secure": secure == "TRUE", "httpOnly": http_only}
            if name.startswith("__Host-"):
                cookie["url"] = f"https://{domain.lstrip('.')}/"
            else:
                cookie.update(domain=domain, path=cpath)
            cookies.append(cookie)
    return cookies


@pytest.fixture(scope="session")
def adt_username() -> str:
    return os.environ.get("ADT_USERNAME", DEFAULT_USERNAME)


@pytest.fixture(scope="session")
def adt_password() -> str:
    """Demo password from the environment.

    Skips (rather than fails) any test that requests it when unset, so the
    public/unauthenticated checks can still run in a credential-free CI lane.
    """
    pw = os.environ.get("ADT_PASSWORD")
    if not pw:
        pytest.skip("ADT_PASSWORD not set — skipping tests that require authentication")
    return pw


@pytest.fixture
def page_errors(page: Page) -> list[str]:
    """Collect uncaught JS exceptions and console errors for the active page.

    Attach early (before navigation) by requesting this fixture in a test; the
    returned list accumulates entries that tests can assert against.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None,
    )
    return errors


@pytest.fixture
def login(
    page: Page,
    adt_base_url: str,
    adt_username: str,
    adt_edge_auth: bool,
    adt_oidc_only: bool,
    request: pytest.FixtureRequest,
) -> Callable[[], Page]:
    """Return a callable that authenticates through the configured browser entry.

    Edge-authenticated deployments redirect ``/`` to the Cognito hosted UI.
    Workshop, explicitly edge-off, and local environments keep the app-served
    ``/login.html`` form. Both paths must land in the app exactly once.
    """

    def _fill_first_visible(selectors: tuple[str, ...], value: str) -> None:
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    candidate.fill(value)
                    return
        raise AssertionError(f"no visible Cognito field matched {selectors}")

    def _click_first_visible(selectors: tuple[str, ...]) -> None:
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    candidate.click()
                    return
        raise AssertionError(f"no visible Cognito submit control matched {selectors}")

    def _login_oidc() -> None:
        cookie_file = os.environ.get("ADT_OIDC_COOKIE_FILE", "")
        if not cookie_file or not os.path.exists(os.path.expanduser(cookie_file)):
            pytest.skip(
                "ADT_EDGE_AUTH=oidc needs ADT_OIDC_COOKIE_FILE pointing at an "
                f"IdP session cookie file (got {cookie_file!r})"
            )
        page.context.add_cookies(_netscape_cookies(cookie_file))
        # The OIDC hop (edge -> IdP -> callback -> app) plus the dashboard's
        # full "load" can exceed goto's 30 s default under load, which made
        # login flake (1 in ~3 runs). Only require the first response here;
        # the 90 s wait_for_url below is the real "landed in the app" gate.
        page.goto(f"{adt_base_url}/", wait_until="commit", timeout=90_000)
        app_host = urlsplit(adt_base_url).hostname
        page.wait_for_url(
            lambda url: urlsplit(str(url)).hostname == app_host
            and "/login" not in urlsplit(str(url)).path,
            timeout=90_000,
        )

    def _login() -> Page:
        if adt_oidc_only:
            _login_oidc()
        elif adt_edge_auth:
            adt_password = request.getfixturevalue("adt_password")
            page.goto(f"{adt_base_url}/")
            page.wait_for_url(
                lambda url: (urlsplit(str(url)).hostname or "").endswith(
                    ".amazoncognito.com"
                ),
                timeout=30_000,
            )
            _fill_first_visible(
                ("#signInFormUsername", "input[name='username']", "input[name='email']"),
                adt_username,
            )
            _fill_first_visible(
                ("#signInFormPassword", "input[name='password']"), adt_password
            )
            _click_first_visible(
                (
                    "#signInSubmitButton",
                    "input[name='signInSubmitButton']",
                    "button[type='submit']",
                    "input[type='submit']",
                )
            )
            app_host = urlsplit(adt_base_url).hostname
            page.wait_for_url(
                lambda url: urlsplit(str(url)).hostname == app_host,
                timeout=60_000,
            )
        else:
            adt_password = request.getfixturevalue("adt_password")
            # One bounded retry: the login page occasionally stalls loading through
            # CloudFront; a fresh navigation recovers. Real auth failures still fail
            # on the second attempt.
            for attempt in (1, 2):
                try:
                    page.goto(f"{adt_base_url}/login.html")
                    page.fill("#email", adt_username, timeout=15_000)
                    break
                except Exception:
                    if attempt == 2:
                        raise
            page.fill("#password", adt_password)
            page.click("#submit-btn")
            page.wait_for_url(lambda url: "/login.html" not in str(url), timeout=30_000)

        expect(page.locator("#login-form")).not_to_be_visible()
        return page

    return _login


@pytest.fixture
def authed_page(login: Callable[[], Page]) -> Page:
    """A page that has completed login and is sitting in the authenticated app."""
    return login()


@pytest.fixture
def open_digest_technical() -> Callable[[Page], None]:
    """Return a callable that opens the steward digest's "Technical details".

    The digest SURFACE is plain language by design (2026-08-06 steward
    feedback: "too technical … can't tell what I'm supposed to care about"). The
    lifecycle counters (``.steward-digest-stats``) and the E7 cost-trend line
    (``.steward-digest-cost-trend``) live INSIDE
    ``<details class="steward-digest-technical">``, which is CLOSED on mount.
    A closed ``<details>`` gives its non-``<summary>`` children a zero box, so
    Playwright resolves them but reports ``hidden`` — asserting visibility
    without the reveal asserts the OLD flat card, not the product.

    The reveal is therefore part of the product's own path and the suite walks
    it, exactly as the proposals tests walk the digest's review link instead of
    deep-linking. Structurally pinned offline in
    ``frontend/src/test/steward-digest.test.tsx`` ("collapsed by default").
    """

    def _open(page: Page) -> None:
        tech = page.locator("details.steward-digest-technical")
        expect(tech.first).to_be_visible(timeout=30_000)
        tech.first.locator("summary").first.click()
        # `details.open` is the DOM property the browser flips on the click —
        # asserted (not the `open=""` attribute) so a slow paint cannot race
        # the caller's visibility assertion.
        expect(tech.first).to_have_js_property("open", True, timeout=10_000)

    return _open
