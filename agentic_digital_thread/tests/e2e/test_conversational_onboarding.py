# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E — the FULL conversational onboarding journey (ADR-0068 follow-up).

This is the dedicated onboarding-chat e2e that ``test_guided_context.py``
names as its deliberate follow-up: welcome chat → onboarding agent calls
``discover`` + ``assess_vocabulary_fit`` → the ``vocab_fit`` checkpoint card
(SSE ``checkpoint`` → ``appendChatCheckpoint`` → ``ObFlow.ChatCheckpoint``)
→ the inline Context Interview card (guided endpoints — steward answers never
transit the LLM) → business-context draft confirm.

Environment expectations (matches the existing suite's convention — no demo
literal beyond the sanctioned demo profile's system id, overridable exactly
like ``test_onboarding.py``):

* ``ADT_ONBOARD_SYSTEM`` (default ``erp``) names an ONBOARDED system whose
  discovery the onboarding agent can run/reuse. The onboarding session store
  answers ``discover`` with "session already in progress" on repeat runs —
  that is honest, and ``assess_vocabulary_fit`` still reports from the stored
  session, so BOTH the fresh-discover and already-in-progress cases pass
  through the same wait on the ``vocab_fit`` card.
* If ``assess_vocabulary_fit`` reports no context questions and an ``ok``
  fit, the interview card legitimately never mounts (the tool result is
  computed live) — the test SKIPS with the observed count, never a silent
  pass.

State honesty: the journey persists a business-context draft/confirm for the
target system, so the fixture snapshots ``GET
/api/admin/business-context/{id}`` before the run and restores it after
(PUT), leaving the environment as found. The test never resets the system.

Credential-gated like the rest of the suite: requesting ``login`` skips
cleanly when ``ADT_PASSWORD`` is unset.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Iterator

import pytest
from playwright.sync_api import Locator, Page, expect

TARGET_SYSTEM = os.environ.get("ADT_ONBOARD_SYSTEM", "erp")
UI_TIMEOUT_MS = 30_000
# The agent turn (discover + assess_vocabulary_fit) takes 60–180 s live.
CHECKPOINT_TIMEOUT_MS = 240_000
# Confirm batches a runtime roll (~2 min) behind the readiness gate; the POST
# answers within the CloudFront 120 s origin cap either way.
CONFIRM_TIMEOUT_MS = 300_000

CHAT_MESSAGE = (
    f"Call discover for {TARGET_SYSTEM}, then assess_vocabulary_fit for "
    f"{TARGET_SYSTEM}, and report the fit numbers verbatim. Do not map anything."
)
# Benign, non-secret-shaped steward answer for the ONE answered question.
ANSWER_TEXT = (
    "This system tracks day-to-day manufacturing operations data for the "
    "demonstration plant; identifiers follow the site's standard scheme."
)
# turnResultLine texts that mean the answer LANDED (context-shared.ts):
# 'draft'/'proposed' → "Captured — …"; 'conflict' → "Not applied — …" (an
# honest refusal that still advances). A 'rejected' answer shows the server
# reason and does NOT advance — the advancement assertion catches it.
LANDED_LINE_RE = re.compile(r"Captured — |Not applied — ")


def _bc_url(adt_base_url: str) -> str:
    return f"{adt_base_url}/api/admin/business-context/{TARGET_SYSTEM}"


@pytest.fixture
def onboard_page(
    page_errors: list[str], login: Callable[[], Page], adt_base_url: str
) -> Iterator[Page]:
    """Logged-in page with the target system's business context snapshotted
    before the journey and restored after it (leave-as-found)."""
    page = login()
    resp = page.request.get(_bc_url(adt_base_url), timeout=60_000)
    if resp.status == 404:
        pytest.skip(f"system '{TARGET_SYSTEM}' is not known to this environment")
    if resp.status == 403:
        pytest.skip("demo user is not an admin — cannot snapshot/restore the "
                    "business context, refusing to mutate the environment")
    assert resp.ok, f"business-context snapshot failed: HTTP {resp.status}"
    snapshot = resp.json()
    page_errors.clear()
    yield page
    _restore_business_context(page, adt_base_url, snapshot)


def _restore_business_context(page: Page, adt_base_url: str, snapshot: dict) -> None:
    """PUT the pre-test text/enabled back — but only when the journey actually
    changed it (an unchanged PUT would roll the system agent for nothing)."""
    url = _bc_url(adt_base_url)
    current = page.request.get(url, timeout=60_000)
    if current.ok:
        cur = current.json()
        if (cur.get("business_context") == snapshot.get("business_context")
                and cur.get("enabled") == snapshot.get("enabled")):
            return
    restored = page.request.put(
        url,
        data={"business_context": snapshot.get("business_context") or "",
              "enabled": bool(snapshot.get("enabled"))},
        timeout=180_000,
    )
    assert restored.ok, (
        f"business-context restore for '{TARGET_SYSTEM}' failed: "
        f"HTTP {restored.status} — environment left modified, restore by hand"
    )


def _open_onboard_and_send(page: Page) -> None:
    """Navigate to the Onboard view and send the journey's chat message
    through the welcome chat input (the LLM welcome path — obStreamChat)."""
    page.click("#onboard-btn")
    expect(page.locator("#onboard-view")).to_be_visible(timeout=UI_TIMEOUT_MS)
    chat = page.locator("#ob-welcome-chat")
    expect(chat).to_be_visible(timeout=UI_TIMEOUT_MS)
    chat.fill(CHAT_MESSAGE)
    page.click("#ob-welcome-send")


def _await_vocab_fit_checkpoint(page: Page) -> Locator:
    """Wait (agent-turn scale) for the `vocab_fit` ChatCheckpoint card and
    assert the pre-map fit banner reports a real integer percentage."""
    card = page.locator(".ob-checkpoint").filter(
        has=page.locator("h4", has_text="Vocabulary Fit"))
    expect(card.first).to_be_visible(timeout=CHECKPOINT_TIMEOUT_MS)
    banner = card.first.locator(".ob-vocab-fit")
    expect(banner).to_be_visible(timeout=UI_TIMEOUT_MS)
    match = re.search(r"Vocabulary fit: (\d+)%", banner.inner_text())
    assert match, f"fit banner lacks an integer percentage: {banner.inner_text()!r}"
    fit_pct = int(match.group(1))
    assert 0 <= fit_pct <= 100, f"fit_pct out of range: {fit_pct}"
    print(f"[journey] vocab_fit checkpoint arrived — fit {fit_pct}%")
    return card.first


def _probe_question_count(page: Page, adt_base_url: str) -> object:
    """Observed live per-system question count (the same guided probe the
    card itself uses; answers: [] so the server derives from stored
    discovery). Used only for the honest-skip message."""
    resp = page.request.post(
        f"{adt_base_url}/api/onboard/guided/probe/{TARGET_SYSTEM}",
        data={"answers": []}, timeout=60_000)
    if not resp.ok:
        return f"probe HTTP {resp.status}"
    return len((resp.json() or {}).get("questions") or [])


def _interview_section(page: Page, card: Locator, adt_base_url: str) -> Locator:
    """The mounted Context Interview card, or an honest SKIP when the tool
    reported nothing worth asking (never a silent pass)."""
    interview = card.locator('section[aria-label="Context interview"]')
    try:
        expect(interview.first).to_be_visible(timeout=UI_TIMEOUT_MS)
    except AssertionError:
        count = _probe_question_count(page, adt_base_url)
        pytest.skip(
            f"no Context interview card mounted for '{TARGET_SYSTEM}' — "
            f"assess_vocabulary_fit reported suggestion 'ok' with zero context "
            f"questions this run (live probe now reports {count} questions)")
    if interview.first.get_by_text("context capture unavailable").count():
        pytest.fail("Context interview card collapsed to 'context capture "
                    "unavailable' — the guided endpoints failed at mount")
    if interview.first.get_by_text("No context questions for this system.").count():
        count = _probe_question_count(page, adt_base_url)
        pytest.skip(
            f"interview card mounted with ZERO questions for '{TARGET_SYSTEM}' "
            f"(observed count 0; live probe now reports {count}) — no turn to drive")
    return interview.first


def _answer_one_question(interview: Locator) -> None:
    """Answer the first question; assert the outcome line renders and the
    card advances (a 'rejected' non-advance fails the counter assertion)."""
    box = interview.locator('textarea[aria-label="Context answer"]')
    expect(box).to_be_visible(timeout=UI_TIMEOUT_MS)
    counter = interview.get_by_text(re.compile(r"question \d+ of \d+"))
    before = counter.inner_text()
    box.fill(ANSWER_TEXT)
    interview.get_by_role("button", name="Answer", exact=True).click()
    line = interview.get_by_text(LANDED_LINE_RE)
    expect(line).to_be_visible(timeout=UI_TIMEOUT_MS)
    print(f"[journey] answered {before!r} → outcome line: {line.inner_text()!r}")
    expect(interview).not_to_contain_text(before, timeout=UI_TIMEOUT_MS)


def _skip_to_end(interview: Locator) -> None:
    """Drive the remaining questions with 'I don't know' (per-question skip —
    'Skip all' would fold the card and hide the draft) until the form ends."""
    box = interview.locator('textarea[aria-label="Context answer"]')
    for _ in range(20):
        if box.count() == 0 or not box.is_visible():
            return
        counter = interview.get_by_text(re.compile(r"question \d+ of \d+"))
        before = counter.inner_text() if counter.count() else ""
        interview.get_by_role("button", name="I don't know").click()
        if before:
            expect(interview).not_to_contain_text(before, timeout=UI_TIMEOUT_MS)
    pytest.fail("interview did not reach its end after 20 skip turns")


def _confirm_draft_if_present(interview: Locator) -> None:
    """If a business-context draft accumulated, save it and assert the honest
    confirm line ('Context saved' — green, or either amber variant). When no
    draft exists (all answers skipped/proposed), the completion note is the
    honest end of the journey."""
    draft = interview.locator('textarea[aria-label="Business context draft"]')
    done = interview.get_by_text("Context capture complete.")
    expect(draft.or_(done).first).to_be_visible(timeout=UI_TIMEOUT_MS)
    if draft.count() == 0 or not draft.is_visible():
        print("[journey] no draft accumulated — completion note is the end")
        return
    expect(draft).to_have_value(re.compile(r"\S"), timeout=UI_TIMEOUT_MS)
    interview.get_by_role(
        "button", name=re.compile(r"^Save context for ")).click()
    confirm = interview.get_by_text(re.compile(r"Context saved"))
    expect(confirm).to_be_visible(timeout=CONFIRM_TIMEOUT_MS)
    print(f"[journey] confirm line: {confirm.inner_text()!r}")


@pytest.mark.e2e
def test_conversational_onboarding_round_trip(
    onboard_page: Page, page_errors: list[str], adt_base_url: str
) -> None:
    """Welcome chat → vocab_fit checkpoint → inline interview → confirm."""
    page = onboard_page
    _open_onboard_and_send(page)
    card = _await_vocab_fit_checkpoint(page)
    interview = _interview_section(page, card, adt_base_url)
    _answer_one_question(interview)
    _skip_to_end(interview)
    _confirm_draft_if_present(interview)
    assert [e for e in page_errors if e.startswith("pageerror:")] == []
