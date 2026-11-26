# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""End-to-end onboarding smoke test (Phase-0 onboarding audit, Wave 0b).

Drives the **deterministic** catalog "Onboard" flow in
`frontend/src/views/onboard/flow.ts` (`obStart(sys)`) — NOT the LLM welcome-chat path. Clicking a
catalog card's Onboard button runs discover → map → resolve → provision → verify,
gated by three `.ob-confirm-btn` checkpoints (Confirm Schema, Confirm Mappings,
Identity Rules / Confirm & Provision). Provision + verify then run automatically and
the catalog card gains the `onboarded` class.

Two birds: this proves the Admin onboarding UI works *and* leaves `cmms` with
`status='onboarded'` plus `MAPS_TO` edges in the knowledge graph — exactly the
precondition the Wave 1 graph audit (`pick_target_system()`) probes for.

Credential-gated like the rest of the suite: requesting `login`/`adt_password` makes
this skip cleanly when `ADT_PASSWORD` is unset. The map/resolve steps call Bedrock and
are polled server-side, so the checkpoint/finish waits use minute-scale timeouts.

Run against a live deploy:
    export ADT_BASE_URL=$(aws cloudformation describe-stacks --stack-name ADTComputeStack \\
        --profile <profile> --region us-east-1 \\
        --query "Stacks[0].Outputs[?OutputKey=='ADTCloudFrontUrl'].OutputValue" --output text)
    export ADT_PASSWORD=$(aws secretsmanager get-secret-value --secret-id adt-demo-password \\
        --profile <profile> --region us-east-1 --query SecretString --output text)
    pytest tests/e2e/test_onboarding.py -m e2e
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable

import pytest
from playwright.sync_api import Page, expect

# The system to onboard. Defaults to `cmms` (the Phase-0 (S0.1) audit target, a
# direct-concept CMMS in the seed catalog) so the default behaviour is unchanged.
# S0.2 Wave 4 parameterizes this via ADT_ONBOARD_SYSTEM so the harness `warm2`
# tier can drive a 2nd onboard (e.g. `erp`) through the same e2e flow to exercise
# the cross-system identity graph (ADR-0011 F1) without forking this test.
TARGET_SYSTEM = os.environ.get("ADT_ONBOARD_SYSTEM", "cmms")

# Whole-pipeline budget: discover + three LLM-backed checkpoints (map/resolve are polled
# Bedrock jobs) + provision + verify. Generous so a cold backend doesn't false-fail.
# Env-overridable (ADT_ONBOARD_FINISH_TIMEOUT_MS) because a LARGE cross-account source
# (e.g. an Athena database with 150+ deeply-nested columns) legitimately takes well
# past the 600s default to sample, provision, and stand up its AgentCore runtime —
# the pipeline PROGRESSES the whole time, it is just slow, so the budget (not the
# flow) is what needs to flex. Default unchanged so existing runs behave identically.
# Above the server's async delete-wait bound (onboard._ASYNC_DELETE_WAIT_DEADLINE_S,
# 900 s) so a slow-but-correct VPC-mode runtime delete never fails the run.
FINISH_TIMEOUT_MS = int(os.environ.get("ADT_ONBOARD_FINISH_TIMEOUT_MS", "1200000"))
# UI/navigation waits.
UI_TIMEOUT_MS = 30_000


def _wait_for_reset_completion(page: Page, adt_base_url: str, deadline: float) -> None:
    """Fallback poll: wait for the server-side reset to finish via the catalog.

    Used when the async job status is unavailable (a reset 504 on an old
    synchronous-only image, or a ``none``/unreachable status during a rollout).
    A finished reset re-lists the system in ``GET /api/onboard/catalog`` with
    status ``registered``. If the entry VANISHES past the deadline instead, the
    interrupted cycle stranded a husk record — fail with the documented repair
    path (scripts/repair_husk.sh) rather than a bare timeout.
    """
    poll_seconds = 15
    while time.time() < deadline:
        resp = page.request.get(f"{adt_base_url}/api/onboard/catalog", timeout=60_000)
        if resp.ok:
            payload = resp.json()
            entries = payload if isinstance(payload, list) else (
                payload.get("systems") or payload.get("catalog") or []
            )
            for entry in entries:
                if entry.get("id") == TARGET_SYSTEM and entry.get("status") == "registered":
                    return  # reset completed server-side; safe to re-onboard
        page.wait_for_timeout(poll_seconds * 1000)
    pytest.fail(
        f"offboard reset 504'd and '{TARGET_SYSTEM}' never returned to the catalog as "
        f"'registered' — the interrupted cycle likely stranded a husk record. "
        f"Repair: scripts/repair_husk.sh --systems {TARGET_SYSTEM} ("
        f"'Offboard reset through CloudFront can 504')."
    )


def _poll_async_reset(page: Page, adt_base_url: str, deadline: float) -> None:
    """Poll ``GET /reset/{id}/status`` until the async reset job reaches ``done``.

    Rollout/restart tolerance: a ``none`` state mid-poll (app restarted
    mid-reset, or the poll served by a pre-JobStore replica) falls back to the
    catalog poll, which detects completion via the entry returning to
    ``registered``. An ``error`` state fails pointing at the documented repair
    path (scripts/repair_husk.sh) — same convention as the catalog-poll fail.
    """
    poll_seconds = 5
    while time.time() < deadline:
        resp = page.request.get(
            f"{adt_base_url}/api/onboard/reset/{TARGET_SYSTEM}/status", timeout=60_000
        )
        if not resp.ok:  # old server without the status route, or a transient blip
            _wait_for_reset_completion(page, adt_base_url, deadline)
            return
        state = resp.json().get("state")
        if state == "done":
            return
        if state == "error":
            pytest.fail(
                f"async offboard reset for '{TARGET_SYSTEM}' failed server-side: "
                f"{resp.json().get('error')} — the interrupted cycle may have stranded "
                f"a husk record. Repair: scripts/repair_husk.sh --systems {TARGET_SYSTEM} "
                f"('Offboard reset through CloudFront can 504')."
            )
        if state == "none":
            _wait_for_reset_completion(page, adt_base_url, deadline)
            return
        page.wait_for_timeout(poll_seconds * 1000)
    pytest.fail(
        f"async offboard reset for '{TARGET_SYSTEM}' did not reach 'done' within "
        f"{FINISH_TIMEOUT_MS} ms. If the job is stuck, repair with "
        f"scripts/repair_husk.sh --systems {TARGET_SYSTEM}."
    )


def _reset_target_system(page: Page, adt_base_url: str) -> None:
    """Offboard TARGET_SYSTEM via the ASYNC reset contract (?wait=false + poll).

    The reset cascade blocks for minutes server-side (runtime + gateway-target
    deletion) — long past the CloudFront 120 s origin read_timeout — so start it
    as a background job and poll GET /reset/{id}/status instead of eating a
    504 on the synchronous default. Two rollout tolerances are kept:
    (i) a 504 on the POST (an old deployed image may ignore ?wait=false and
    run the synchronous cascade; the server keeps working past the timeout)
    falls back to the catalog poll; (ii) see _poll_async_reset for the
    none-state fallback. The route now always runs the removal as a job
    (a ``wait`` parameter is ignored); ``?wait=false`` is kept for older
    deployed images.
    """
    deadline = time.time() + FINISH_TIMEOUT_MS / 1000
    resp = page.request.post(
        f"{adt_base_url}/api/onboard/reset/{TARGET_SYSTEM}?wait=false",
        timeout=FINISH_TIMEOUT_MS,
    )
    if resp.status == 504:
        _wait_for_reset_completion(page, adt_base_url, deadline)
        return
    assert resp.ok, f"offboard reset failed: {resp.status} {resp.text()}"
    if resp.json().get("status") in ("started", "already-running"):
        _poll_async_reset(page, adt_base_url, deadline)
    # else: an old image ran the cascade synchronously within the budget — the
    # response already carries the completed reset payload; nothing to poll.


def _offboard_via_ui(page: Page, adt_base_url: str) -> None:
    """Offboard TARGET_SYSTEM through the REAL UI removal path.

    Drives the catalog card's "Remove system" button → the type-to-confirm
    DisconnectModal (#ob-disc-confirm-input / #ob-disc-yes) → the card's
    "Removing…" state, and waits until the card returns to onboardable
    (#btn-<id> visible). This covers the button/modal/removing-state wiring
    that the raw reset API cannot; the underlying async job contract
    (?wait=false + poll) is the same obDisconnectConfirmed uses.

    Falls back to the API reset contract if the card exposes no removal
    button (e.g. an older deployed UI), so the test never dead-ends."""
    remove_btn = page.locator(f"#card-{TARGET_SYSTEM} .sys-card-disconnect-btn")
    if not remove_btn.count() or not remove_btn.first.is_visible():
        _reset_target_system(page, adt_base_url)  # pre-removal-UI image
        return
    remove_btn.first.click()
    confirm_input = page.locator("#ob-disc-confirm-input")
    expect(confirm_input).to_be_visible(timeout=UI_TIMEOUT_MS)
    confirm_input.fill(TARGET_SYSTEM)  # type-to-confirm arms the danger button
    yes = page.locator("#ob-disc-yes")
    expect(yes).to_be_enabled(timeout=UI_TIMEOUT_MS)
    yes.click()
    # The modal closes once the background job starts; the card then shows
    # "Removing…" (disabled) until the poll completes and the catalog
    # re-lists the system as onboardable. Runtime+target deletion cascades
    # for minutes — reuse the whole-pipeline budget.
    onboard_btn = page.locator(f"#btn-{TARGET_SYSTEM}")
    expect(onboard_btn).to_be_visible(timeout=FINISH_TIMEOUT_MS)


@pytest.mark.e2e
def test_onboard_cmms_end_to_end_seeds_graph(
    login: Callable[[], Page],
    page_errors: list[str],
    adt_base_url: str,
) -> None:
    """Onboard `cmms` through the catalog flow; it must reach the onboarded state.

    Requesting `page_errors` before `login()` ensures its console/pageerror
    listeners are attached before any navigation (mirrors test_admin.py).
    """
    page = login()

    # Enter the Onboard view (switchView('onboard') → obLoadCatalog()).
    page.click("#onboard-btn")
    catalog = page.locator("#catalog-list")
    expect(catalog).to_be_visible(timeout=UI_TIMEOUT_MS)

    card = page.locator(f"#card-{TARGET_SYSTEM}")
    onboard_btn = page.locator(f"#btn-{TARGET_SYSTEM}")
    # Catalog finished loading once the system's card is rendered. The card is always
    # present (registered or onboarded); the Onboard button only exists when registered.
    # Data-tolerant: `cmms` is a BUNDLED demo system — absent in base-mode envs
    # (no --seed/--simulator), where only user-onboarded systems exist.
    try:
        card.wait_for(timeout=UI_TIMEOUT_MS)
    except Exception:
        pytest.skip(f"'{TARGET_SYSTEM}' not in catalog — bundled demo systems absent (base env)")
    expect(card).to_be_visible(timeout=UI_TIMEOUT_MS)

    # Deterministic clean slate: if a prior run left cmms onboarded, remove it THROUGH
    # THE UI (card's Remove button → type-to-confirm modal → "Removing…" state) so the
    # steward-facing removal path is exercised live, then the full
    # discover→map→resolve→provision→verify pipeline runs from scratch — including the
    # AgentCore runtime creation that this test is here to guard (ADR 0010).
    if not onboard_btn.is_visible():
        _offboard_via_ui(page, adt_base_url)
        page.reload()
        page.click("#onboard-btn")
        expect(onboard_btn).to_be_visible(timeout=UI_TIMEOUT_MS)

    # Kick off the deterministic discover→map→resolve→provision→verify pipeline.
    onboard_btn.click()

    # Drive the checkpoints with a state-driven loop rather than a fixed count, because
    # the flow is not uniform:
    #   - Confirm Schema / Identity Rules / (optional) "New concepts discovered" each
    #     surface an *enabled* `.ob-confirm-btn` → click it.
    #   - The PRE-MAP PROBE step (context interview, T-H) sits between discover and
    #     map; its deterministic smoke path is the header's "Skip and map now"
    #     button (present whether questions exist or not).
    #   - Confirm Mappings renders its button DISABLED until every field is mapped or
    #     skipped; the deterministic smoke path is "Skip All Unmapped", which enables it.
    #   - Between checkpoints, map/resolve/provision/verify run as async (polled) jobs —
    #     no actionable control is present, so we wait and re-check.
    # Loop until the catalog card flips to `onboarded` or the wall-clock budget expires.
    onboarded = re.compile(r"\bonboarded\b")
    deadline = time.time() + FINISH_TIMEOUT_MS / 1000
    while time.time() < deadline:
        if "onboarded" in (card.get_attribute("class") or "").split():
            break
        enabled_confirm = page.locator(".ob-confirm-btn:not([disabled])")
        if enabled_confirm.count() and enabled_confirm.first.is_visible():
            enabled_confirm.first.click()
            page.wait_for_timeout(500)
            continue
        probe_skip = page.get_by_role("button", name=re.compile("Skip and map now"))
        if probe_skip.count() and probe_skip.last.is_visible():
            probe_skip.last.click()  # the probe never blocks mapping by contract
            page.wait_for_timeout(500)
            continue
        skip_all = page.get_by_role("button", name=re.compile("Skip All Unmapped"))
        if skip_all.count() and skip_all.last.is_visible():
            skip_all.last.click()  # enables the gated "Confirm Mappings" button
            page.wait_for_timeout(500)
            continue
        page.wait_for_timeout(2000)  # an async job is running; re-check shortly

    # Provision + verify have run; the catalog card must show the onboarded state.
    expect(card).to_have_class(onboarded, timeout=UI_TIMEOUT_MS)
    # obLoadCatalog() re-renders the onboarded card with a Disconnect control and no
    # Onboard button (the transient "Onboarded" badge from obStart is replaced), so assert
    # the persisted state rather than that short-lived badge. Target the specific
    # disconnect control: the merged connection-overhaul footer also renders Test/Edit
    # buttons that share the generic `.reset-btn` class, so `.reset-btn` alone is ambiguous.
    expect(page.locator(f"#card-{TARGET_SYSTEM} .sys-card-disconnect-btn")).to_be_visible(timeout=UI_TIMEOUT_MS)
    expect(onboard_btn).to_have_count(0)

    # The deterministic flow must not throw uncaught JS exceptions. (Console.error
    # noise is tolerated; an uncaught pageerror is a real regression.)
    page_exceptions = [e for e in page_errors if e.startswith("pageerror:")]
    assert page_exceptions == [], f"uncaught JS during onboarding: {page_exceptions}"
