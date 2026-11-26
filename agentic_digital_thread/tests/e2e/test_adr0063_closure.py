# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ADR-0063 deployed closure checks.

Default tests are read-only: they click navigation/reveal controls but never one
that changes steward or graph state. "State-changing" is decided semantically and
FAIL-CLOSED by ``tests.harness.adr0063_e2e_safety.is_state_changing_request`` —
read-shaped methods and two audited POST-shaped panel READS
(``/api/metrics/resolve-by-dimension``, ``/api/metrics/resolve-heatmap``) pass;
everything else, INCLUDING an unknown POST, is recorded as a mutation, so a newly
added mutating endpoint fails these checks until a human audits it. ``/api/ask``
is deliberately NOT audited as a read.

The two mutation checks require ``ADT_E2E_ADR0063_MUTATIONS=1`` and the safety
fixture below. They create only pending work, never accept it, and reject only
their exact run-created ids through the ordinary steward lifecycle.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import Counter
from typing import Callable, NoReturn
from urllib.parse import quote, urlsplit

import pytest
from pytest import skip as _skip
from playwright.sync_api import APIResponse, Page, Response, expect

from tests.harness.adr0063_e2e_safety import (
    BRIEFING_STUB_SSE,
    MUTATION_FLAG,
    is_dashboard_briefing_turn,
    is_state_changing_request,
    mutation_request_allowed,
    protected_mutation_refusal,
    synthetic_system_id,
)

_TERMINAL_RECONCILIATION = ("delivered", "pending", "unavailable", "zero")
_UI_TIMEOUT_MS = 30_000
_JOB_TIMEOUT_SECONDS = 300


def _response_json(response: APIResponse | Response) -> object:
    assert response.ok, f"{response.status} {response.url}: {response.text()[:500]}"
    return response.json()


def _mapping_rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("mappings", [])
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


#: The mapping ids of the cards that follow a family header, up to the next
#: element that is not a card. The UI renders a family as its header followed
#: directly by its member cards (a Fragment in ProposalsPanel.SectionCards);
#: a one-card family has no header, so the list can run on into later groups.
_FAMILY_MEMBER_IDS_JS = """
head => {
  const ids = [];
  for (let el = head.nextElementSibling;
       el && el.matches('.sl-card[data-mapping-id]');
       el = el.nextElementSibling) {
    ids.push(el.getAttribute('data-mapping-id'));
  }
  return ids;
}
"""

#: Every rendered card's mapping id, so a card shown twice is visible.
_ALL_CARD_IDS_JS = """
() => [...document.querySelectorAll('.sl-card[data-mapping-id]')]
  .map(el => el.getAttribute('data-mapping-id'))
"""

#: The exact list request the review panel makes for the Pending tab
#: (proposals.ts scopedMappingsUrl).
_PENDING_LIST_PATH = "/api/proposed-mappings"
_PENDING_LIST_QUERY = "status=pending"


def _is_pending_list_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path == _PENDING_LIST_PATH and parts.query == _PENDING_LIST_QUERY


def _shown_as_pending(row: dict) -> bool:
    """The rows the Pending tab renders: SlCards.validMappings drops the
    detected_gap placeholders and proposalStatus treats a missing status as
    pending."""
    status = str(row.get("status") or "pending").lower()
    return (
        status == "pending"
        and row.get("from_field") != "detected_gap"
        and row.get("to_field") != "detected_gap"
    )

def _assert_read_only(writes: list[str]) -> None:
    assert writes == [], f"read-only closure check issued mutation requests: {writes}"


def skip_read_only(writes: list[str], reason: str) -> NoReturn:
    _assert_read_only(writes)
    _skip(reason)


def _stub_dashboard_briefing(route) -> None:
    request = route.request
    if is_dashboard_briefing_turn(request.method, request.url, request.post_data):
        route.fulfill(status=200, content_type="text/event-stream", body=BRIEFING_STUB_SSE)
    else:
        route.continue_()


@pytest.fixture
def steward_root(
    page: Page, page_errors: list[str], login: Callable[[], Page], adt_base_url: str
) -> tuple[Page, list[str]]:
    # The landing dashboard auto-streams its AI briefing over /api/ask; stub
    # exactly that turn locally so a read-only check never triggers a server
    # turn it did not ask for (every other /api/ask still records below).
    page.route("**/api/ask", _stub_dashboard_briefing)
    page = login()
    writes: list[str] = []
    # Semantic, FAIL-CLOSED recording (tests/harness/adr0063_e2e_safety.py): every
    # request is a mutation unless it is a read-shaped method or one of the two
    # audited POST-shaped panel READS. The former ANY-POST heuristic both flagged
    # those audited reads and ignored any method outside its hardcoded write set;
    # an unknown POST — including a newly added mutating endpoint — now records as
    # a violation until a human audits it.
    page.on(
        "request",
        lambda request: writes.append(f"{request.method} {request.url}")
        if is_state_changing_request(request.method, request.url)
        and not is_dashboard_briefing_turn(request.method, request.url, request.post_data)
        else None,
    )
    me = _response_json(page.request.get(f"{adt_base_url}/api/auth/me"))
    if not isinstance(me, dict) or not me.get("is_admin"):
        skip_read_only(writes, "admin/steward identity required for ADR-0063 closure checks")
    expect(page.locator("body")).to_be_visible(timeout=_UI_TIMEOUT_MS)
    page.wait_for_timeout(2_000)
    page_errors.clear()
    return page, writes


@pytest.fixture
def steward_admin(
    steward_root: tuple[Page, list[str]], adt_base_url: str, page_errors: list[str]
) -> tuple[Page, list[str]]:
    page, _writes = steward_root
    page.goto(f"{adt_base_url}/admin.html")
    # Product-DOM readiness, NOT transport quiescence: the admin SPA refetches on
    # a React Query interval, so 'networkidle' asserts something that is not a
    # product contract and can never settle (same reasoning as
    # test_steward_review.py). page.goto already awaited the load event.
    expect(page.locator("nav.tab-bar")).to_be_visible(timeout=_UI_TIMEOUT_MS)
    expect(page.locator("#access-denied")).to_be_hidden()
    page_errors.clear()
    return steward_root


def _open_proposals(page: Page) -> None:
    page.locator("#architecture-btn").click()
    expect(page.locator("#architecture-view")).to_be_visible(timeout=_UI_TIMEOUT_MS)
    tab = page.locator('[data-arch-tab="proposed"]')
    expect(tab).to_be_visible(timeout=_UI_TIMEOUT_MS)
    tab.click()
    expect(page.locator("#proposed-mappings-panel")).to_be_visible(timeout=_UI_TIMEOUT_MS)
    expect(page.locator("#self-learning-cards")).to_be_visible(timeout=_UI_TIMEOUT_MS)


def _reveal_all_proposal_cards(page: Page) -> None:
    """Expand every bounded review section (client-side only, no request).

    The panel renders each section in steps with a "Show N more" control, so
    a specific card may not be in the DOM yet. Hidden sections (the collapsed
    insights <details>) cannot take a real click, so this uses DOM clicks and
    repeats until no control remains.

    A non-silent reload blanks the list ("Loading discoveries…") and remounts
    each section at its first step, which undoes an earlier expansion. On a
    large queue (297 pending, 2026-10-02) the reload landed after the first
    pass, so a family past card 100 never rendered. Each pass therefore waits
    for the list to be loaded, and three quiet passes in a row end it."""
    expect(page.locator(".sl-card").first).to_be_attached(timeout=_UI_TIMEOUT_MS)
    quiet = 0
    for _ in range(400):
        expect(page.locator("#self-learning-cards .sl-loading")).to_have_count(
            0, timeout=_UI_TIMEOUT_MS)
        clicked = page.evaluate(
            "() => { const b = [...document.querySelectorAll('.sl-show-more')];"
            " b.forEach(x => x.click()); return b.length; }"
        )
        quiet = 0 if clicked else quiet + 1
        if quiet >= 3:
            return
        page.wait_for_timeout(250)
    raise AssertionError("proposal sections never finished expanding")


@pytest.mark.e2e
def test_live_trust_chips_and_real_family_headers_are_read_only(
    steward_root: tuple[Page, list[str]], adt_base_url: str, page_errors: list[str]
) -> None:
    page, writes = steward_root
    # ONE snapshot of the Pending list feeds both the panel and the oracle.
    # The queue can grow while this test runs (an onboarding that has just
    # finished keeps filing proposals), so the panel's own list request is
    # answered from this snapshot instead of the live queue. Every expected
    # family below is then the COMPLETE family the panel was given, so a
    # card the UI drops, moves, repeats or interleaves fails the check, and a
    # row filed after the snapshot cannot.
    snapshot = page.request.get(f"{adt_base_url}{_PENDING_LIST_PATH}?{_PENDING_LIST_QUERY}")
    pending = [row for row in _mapping_rows(_response_json(snapshot)) if _shown_as_pending(row)]
    snapshot_body = snapshot.body()
    snapshot_type = snapshot.headers.get("content-type", "application/json")
    served: list[str] = []

    def _serve_snapshot(route) -> None:
        if route.request.method == "GET":
            served.append(route.request.url)
            route.fulfill(status=200, content_type=snapshot_type, body=snapshot_body)
        else:
            route.continue_()

    page.route(_is_pending_list_url, _serve_snapshot)
    signals = [
        row
        for row in pending
        if "hierarchy_position" in str(row.get("auto_accept_block", ""))
        or (
            row.get("target_verified") is True
            and row.get("verified_scope") == "system"
        )
    ]
    families: dict[str, list[dict]] = {}
    for row in pending:
        key = str(row.get("family_key") or "")
        if key:
            families.setdefault(key, []).append(row)
    real_families = [key for key, rows in families.items() if len(rows) >= 2]
    if not signals and not real_families:
        skip_read_only(writes, "live queue has no trust-signal rows or multi-row server families")

    _open_proposals(page)
    _reveal_all_proposal_cards(page)
    # Opening the tab issues a non-silent list load, and the reveal waited for
    # it to land, so the rendered list is the snapshot.
    assert served, "the review panel never requested the Pending list"
    for row in signals[:5]:
        mapping_id = str(row.get("mapping_id") or "")
        card = page.locator(f'.sl-card[data-mapping-id={json.dumps(mapping_id)}]')
        expect(card).to_be_attached(timeout=_UI_TIMEOUT_MS)
        chip = card.locator(".sl-trust")
        expected = (
            "positional guess — needs a human check"
            if "hierarchy_position" in str(row.get("auto_accept_block", ""))
            else "target checked"
        )
        expect(chip).to_have_text(expected)
    rendered = Counter(page.evaluate(_ALL_CARD_IDS_JS))
    for family_key in real_families[:5]:
        members = families[family_key]
        expected_ids = [str(row.get("mapping_id") or "") for row in members]
        header = page.locator(f'.sl-family-head[data-family-key={json.dumps(family_key)}]')
        # ATTACHED, not visible — matching the card assertion above. Family
        # headers render into four sibling sections (mapping / annotation /
        # insight / drift) and only the active one is displayed, so a family
        # in another section is legitimately attached-but-hidden. Asserting
        # visibility here failed deterministically on 102 real families while
        # the grouping itself was correct. The read-only intent is unchanged.
        # One header per family: a family split into two groups makes this
        # locator match twice and fail.
        expect(header).to_be_attached(timeout=_UI_TIMEOUT_MS)
        # Each member is rendered exactly once, and all of them sit directly
        # under the header. A one-card family has no header, so the cards
        # after this family's members can be the next group's: only the
        # first len(members) cards belong to this header.
        shown_twice = {mid: rendered[mid] for mid in expected_ids if rendered[mid] != 1}
        assert not shown_twice, f"{family_key}: member cards not rendered once: {shown_twice}"
        following = header.evaluate(_FAMILY_MEMBER_IDS_JS)
        run = following[: len(expected_ids)]
        assert sorted(run) == sorted(expected_ids), (
            f"{family_key}: cards under the header {run} are not the family {expected_ids}"
        )
        expect(header.locator(".sl-family-label")).to_have_text(
            re.compile(rf"^{len(members)} in this family\b")
        )
        for action, verb in (("accept", "Accept"), ("reject", "Reject")):
            eligible = sum(
                row.get("action_eligibility", {}).get(action, {}).get("eligible") is True
                for row in members
            )
            expect(header.locator(f".sl-family-{action}")).to_have_text(
                re.compile(rf"^{verb} {eligible} eligible in this family$")
            )
    _assert_read_only(writes)
    assert page_errors == [], f"JS errors on proposal closure surfaces: {page_errors}"


@pytest.mark.e2e
def test_precision_is_reachable_only_through_technical_details(
    steward_root: tuple[Page, list[str]], adt_base_url: str, page_errors: list[str]
) -> None:
    page, writes = steward_root
    digest = _response_json(page.request.get(f"{adt_base_url}/api/admin/learning/digest"))
    if not isinstance(digest, dict) or not isinstance(digest.get("precision"), dict):
        skip_read_only(writes, "live digest has no computed precision block")
    expect(page.locator("#steward-digest-card")).to_be_visible(timeout=_UI_TIMEOUT_MS)
    technical = page.locator("details.steward-digest-technical")
    precision = technical.locator(".steward-digest-precision")
    expect(precision).to_be_attached(timeout=_UI_TIMEOUT_MS)
    expect(precision).to_be_hidden()
    technical.locator("summary").click()
    expect(precision).to_be_visible(timeout=_UI_TIMEOUT_MS)
    assert precision.evaluate("node => node.closest('details.steward-digest-technical') !== null")
    text = precision.inner_text()
    assert "Machine-proposed learnings" in text or "decisions on machine-proposed learnings" in text
    _assert_read_only(writes)
    assert page_errors == [], f"JS errors on precision reveal: {page_errors}"


@pytest.mark.e2e
def test_guidance_budget_reconciliation_and_placement_controls_are_read_only(
    steward_admin: tuple[Page, list[str]], page_errors: list[str]
) -> None:
    page, writes = steward_admin
    page.locator('.tab-bar button[data-tab="tab-vocabulary"]').click()
    expect(page.locator("#tab-vocabulary")).to_be_visible(timeout=_UI_TIMEOUT_MS)
    editor = page.locator("#vocab-guidance-editor")
    expect(editor).to_be_visible(timeout=_UI_TIMEOUT_MS)
    # The editor mounts with a loading line and renders the empty state or the
    # system panels only once the guidance request returns; the instant
    # count() below must not run before that, or a no-guidance environment
    # fails 30 s later instead of skipping.
    expect(editor.get_by_text("Loading query guidance")).to_be_hidden(timeout=_UI_TIMEOUT_MS)
    if editor.locator(".vocab-guidance-empty").count():
        skip_read_only(writes, "no onboarded system carries guidance in this environment")

    # The budget line sits behind each system's collapsed "Technical detail"
    # (plain-language surface); a closed <details> hides it from Playwright,
    # so walk the product's reveal first.
    technical = editor.locator("details.vocab-guidance-technical").first
    expect(technical).to_be_visible(timeout=_UI_TIMEOUT_MS)
    technical.locator("summary").click()
    budget = technical.locator(".vocab-guidance-authority")
    expect(budget).to_be_visible(timeout=_UI_TIMEOUT_MS)
    assert re.search(
        r"scope QUERY HINTS \+ ANSWER REQUIREMENTS · character budget "
        r"(?:UNAVAILABLE|\d+/\d+) · headroom (?:UNAVAILABLE|\d+)",
        budget.inner_text(),
    )
    reconciliation = editor.locator('[aria-label="Read-only guidance reconciliation"]')
    expect(reconciliation).to_be_visible(timeout=_UI_TIMEOUT_MS)
    state = reconciliation.locator(".guidance-authority-tag").get_attribute("data-state")
    assert state in _TERMINAL_RECONCILIATION, f"unexpected reconciliation state: {state}"
    assert "read-only" in reconciliation.inner_text()
    assert reconciliation.get_by_text(re.compile("repair", re.I)).count() == 0

    placement = editor.locator('[aria-label="Context placement check"]')
    expect(placement).to_be_visible(timeout=_UI_TIMEOUT_MS)
    expect(placement.get_by_label("System for context placement check")).to_be_visible()
    expect(placement.get_by_role("button", name="Run scoped check")).to_be_visible()
    assert "may file pending proposals" in placement.inner_text()
    assert "never repairs or applies mappings" in placement.inner_text()

    queue = editor.locator('[aria-label="Pending guidance delivery"]')
    expect(queue).to_be_visible(timeout=_UI_TIMEOUT_MS)
    queue_state = queue.locator(".guidance-authority-tag").first.get_attribute("data-state")
    assert queue_state in ("pending", "zero", "unavailable")
    _assert_read_only(writes)
    assert page_errors == [], f"JS errors on guidance closure surfaces: {page_errors}"


@pytest.mark.e2e
def test_promotion_control_is_reachable_but_not_invoked(
    steward_root: tuple[Page, list[str]], adt_base_url: str, page_errors: list[str]
) -> None:
    page, writes = steward_root
    page.goto(f"{adt_base_url}/#onboard/questions")
    panel = page.locator("#runtime-questions")
    expect(panel).to_be_visible(timeout=_UI_TIMEOUT_MS)
    # The panel mounts with a loading bubble and renders this line only once
    # the questions request returns, so it gets the same UI timeout as the
    # panel. The promote controls render in that same update, so the instant
    # count() below cannot run ahead of them.
    expect(panel.get_by_text("nothing is applied automatically")).to_be_visible(
        timeout=_UI_TIMEOUT_MS
    )
    controls = panel.locator(".rq-promote-requirement")
    if controls.count() == 0:
        skip_read_only(writes, "no structured context-not-reflected question is pending")
    expect(controls.first).to_be_visible()
    card = controls.first.locator("xpath=ancestor::*[contains(@class, 'rq-question')]")
    expect(card).to_contain_text("Creates a PENDING proposal for steward review")
    expect(card).to_contain_text("does not apply a requirement")
    _assert_read_only(writes)
    assert page_errors == [], f"JS errors on promotion control: {page_errors}"


@pytest.mark.e2e
def test_existing_admin_provenance_exposes_query_fingerprint_without_new_turn(
    steward_root: tuple[Page, list[str]], page_errors: list[str]
) -> None:
    page, writes = steward_root
    bound = page.locator(
        "#messages .message.assistant a[data-cite-target][data-verification]:not(.cite-badge)"
    )
    if bound.count() == 0:
        skip_read_only(writes, "no existing provenance-bound turn; the read-only check never asks a new question")
    bound.last.click()
    card = page.locator(".evidence-card")
    expect(card).to_be_visible(timeout=_UI_TIMEOUT_MS)
    responses: list[Response] = []
    page.on(
        "response",
        lambda response: responses.append(response)
        if "/api/admin/agents/traces/queries/" in response.url
        else None,
    )
    card.locator("details.evidence-tier3 summary").click()
    expect(
        card.locator(
            ".evidence-executed-query, .evidence-query-absent, .evidence-tier3-unavailable"
        ).first
    ).to_be_visible(timeout=_UI_TIMEOUT_MS)
    if not responses:
        skip_read_only(writes, "existing bound record has no redeemable query handle")
    response = responses[-1]
    if response.status in (403, 404):
        skip_read_only(writes, f"existing provenance handle is unavailable ({response.status})")
    detail = _response_json(response)
    assert isinstance(detail, dict)
    assert re.fullmatch(r"[0-9a-f]{64}", str(detail.get("query_fingerprint", "")))
    assert detail.get("query_shape_version") == "v1"
    _assert_read_only(writes)
    real_errors = [error for error in page_errors if "Failed to load resource" not in error]
    assert real_errors == [], f"JS errors on provenance fingerprint check: {real_errors}"


@pytest.fixture
def mutation_context(
    page_errors: list[str], login: Callable[[], Page], adt_base_url: str
) -> tuple[Page, str]:
    if os.environ.get(MUTATION_FLAG) != "1":
        skip_read_only([], f"opt in explicitly with {MUTATION_FLAG}=1")
    refusal = protected_mutation_refusal(adt_base_url)
    if refusal:
        pytest.fail(refusal)
    system_id = synthetic_system_id()
    page = login()
    me = _response_json(page.request.get(f"{adt_base_url}/api/auth/me"))
    if not isinstance(me, dict) or not me.get("is_admin"):
        pytest.fail("opt-in closure mutations require an admin/steward identity")
    systems = _response_json(page.request.get(f"{adt_base_url}/api/query-guidance"))
    known = {
        str(row.get("system_id"))
        for row in (systems.get("systems", []) if isinstance(systems, dict) else [])
        if isinstance(row, dict)
    }
    if system_id not in known:
        pytest.fail("the explicitly designated synthetic system is not onboarded")
    page_errors.clear()
    return page, system_id


def _post_mutation(page: Page, base_url: str, path: str, body: object = None) -> object:
    assert mutation_request_allowed(path, body), f"mutation safety allowlist refused {path}"
    response = page.request.post(
        f"{base_url}{path}",
        data=body if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    return _response_json(response)


def _reject_exact(page: Page, base_url: str, mapping_id: str) -> None:
    path = f"/api/proposed-mappings/{quote(mapping_id, safe='')}/reject"
    result = _post_mutation(page, base_url, path)
    assert isinstance(result, dict) and result.get("status") == "rejected"


@pytest.mark.e2e
def test_opt_in_promotion_stays_pending_then_uses_normal_reject_cleanup(
    mutation_context: tuple[Page, str], adt_base_url: str
) -> None:
    page, system_id = mutation_context
    run_id = uuid.uuid4().hex[:12]
    body = {
        "asserted": f"synthetic-required-{run_id}",
        "negated": f"synthetic-disallowed-{run_id}",
    }
    path = (
        "/api/admin/vocabulary/guidance/"
        f"{quote(system_id, safe='')}/promote-requirement"
    )
    mapping_id = ""
    try:
        created = _post_mutation(page, adt_base_url, path, body)
        if isinstance(created, dict):
            mapping_id = str(created.get("mapping_id") or "")
        assert isinstance(created, dict) and created.get("status") == "pending"
        rows = _mapping_rows(
            _response_json(page.request.get(f"{adt_base_url}/api/proposed-mappings"))
        )
        row = next(item for item in rows if item.get("mapping_id") == mapping_id)
        assert row.get("status") == "pending"
        assert row.get("proposed_type") == "guidance_note"
        assert row.get("from_system") == system_id
        assert row.get("guidance_scope") == "answer_requirement"
    finally:
        if mapping_id:
            _reject_exact(page, adt_base_url, mapping_id)


@pytest.mark.e2e
def test_opt_in_context_placement_is_scoped_and_cleans_only_run_created_rows(
    mutation_context: tuple[Page, str], adt_base_url: str
) -> None:
    page, system_id = mutation_context
    before = _mapping_rows(
        _response_json(page.request.get(f"{adt_base_url}/api/proposed-mappings"))
    )
    before_ids = {str(row.get("mapping_id")) for row in before}
    created_ids: list[str] = []
    try:
        started = _post_mutation(
            page,
            adt_base_url,
            "/api/admin/learning/context-placement-check",
            {"system_id": system_id},
        )
        assert isinstance(started, dict)
        job_id = str(started.get("job_id") or "")
        deadline = time.monotonic() + _JOB_TIMEOUT_SECONDS
        job: object = {}
        while time.monotonic() < deadline:
            job = _response_json(
                page.request.get(
                    f"{adt_base_url}/api/admin/learning/context-placement-check/"
                    f"{quote(job_id, safe='')}"
                )
            )
            if isinstance(job, dict) and job.get("status") in ("done", "error"):
                break
            time.sleep(2)
        assert isinstance(job, dict) and job.get("status") == "done", job
        result = job.get("result")
        assert isinstance(result, dict)
        after = _mapping_rows(
            _response_json(page.request.get(f"{adt_base_url}/api/proposed-mappings"))
        )
        created_ids = [
            str(row.get("mapping_id"))
            for row in after
            if str(row.get("mapping_id")) not in before_ids
            and row.get("source") == "context-placement"
            and row.get("status") == "pending"
            and system_id in {row.get("from_system"), row.get("to_system")}
        ]
        assert len(created_ids) == int(result.get("filed", 0))
        assert all("*" not in mapping_id and "%" not in mapping_id for mapping_id in created_ids)
    finally:
        if not created_ids:
            after = _mapping_rows(
                _response_json(page.request.get(f"{adt_base_url}/api/proposed-mappings"))
            )
            created_ids = [
                str(row.get("mapping_id"))
                for row in after
                if str(row.get("mapping_id")) not in before_ids
                and row.get("source") == "context-placement"
                and row.get("status") == "pending"
                and system_id in {row.get("from_system"), row.get("to_system")}
            ]
        for mapping_id in created_ids:
            _reject_exact(page, adt_base_url, mapping_id)
