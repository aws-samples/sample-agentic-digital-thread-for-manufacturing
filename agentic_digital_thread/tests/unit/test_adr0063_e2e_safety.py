# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline safety contracts for the opt-in ADR-0063 browser mutations."""
from __future__ import annotations

import pytest

from tests.harness.adr0063_e2e_safety import (
    PROTECTED_MARKERS_ENV,
    PROTECTED_OVERRIDE_FLAG,
    SYNTHETIC_SYSTEM_ENV,
    is_dashboard_briefing_turn,
    is_state_changing_request,
    mutation_request_allowed,
    protected_mutation_refusal,
    synthetic_system_id,
)


def test_named_and_configured_protected_urls_require_separate_authorization():
    short_tier = bytes((112, 114, 111, 100)).decode("ascii")
    long_tier = bytes((112, 114, 111, 100, 117, 99, 116, 105, 111, 110)).decode("ascii")
    encoded_short = "%70%72%6F%64"
    env = {PROTECTED_MARKERS_ENV: "locked-zone"}
    for url in (
        f"https://{short_tier}.example.test",
        f"https://example.test/environments/{long_tier.upper()}/run",
        f"https://example.test/environments/{encoded_short}/run",
        "https://example.test/environments/locked%2Dzone/run",
    ):
        refusal = protected_mutation_refusal(url, env)
        assert refusal and refusal.startswith("REFUSED:")
        assert PROTECTED_OVERRIDE_FLAG in refusal


def test_named_protected_tokens_do_not_match_larger_words():
    env = {PROTECTED_MARKERS_ENV: ""}
    assert protected_mutation_refusal("https://product.example.test", env) is None
    assert protected_mutation_refusal("https://example.test/reproduction/run", env) is None


def test_protected_override_is_independent_from_mutation_opt_in():
    env = {
        PROTECTED_MARKERS_ENV: "locked-zone",
        PROTECTED_OVERRIDE_FLAG: "1",
    }
    assert protected_mutation_refusal("https://locked-zone.example.test", env) is None


def test_synthetic_system_must_be_explicit_and_placeholder_shaped():
    assert synthetic_system_id({SYNTHETIC_SYSTEM_ENV: "synthetic-system-01"}) == (
        "synthetic-system-01"
    )
    for value in ("", "*", "all", "has spaces", "../other"):
        with pytest.raises(ValueError):
            synthetic_system_id({SYNTHETIC_SYSTEM_ENV: value})


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/admin/vocabulary/guidance/synthetic-system-01/promote-requirement",
            {
                "asserted": "synthetic-required-value",
                "negated": "synthetic-disallowed-value",
            },
        ),
        (
            "/api/admin/learning/context-placement-check",
            {"system_id": "synthetic-system-01"},
        ),
        ("/api/proposed-mappings/synthetic-run-id/reject", None),
    ],
)
def test_narrow_mutation_allowlist_accepts_only_safe_shapes(path, body):
    assert mutation_request_allowed(path, body)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/proposed-mappings/synthetic-run-id/accept", {}),
        ("/api/proposed-mappings/adjudicate-batch", {"mapping_ids": ["*"]}),
        ("/api/proposed-mappings/reject-degenerate", {}),
        ("/api/admin/business-context/synthetic-system-01", {"text": "x"}),
        ("/api/admin/learning/context-placement-check", {}),
        (
            "/api/admin/vocabulary/guidance/synthetic-system-01/promote-requirement",
            {"asserted": "real-value", "negated": "synthetic-value"},
        ),
        ("/api/proposed-mappings/*/reject", None),
    ],
)
def test_narrow_mutation_allowlist_refuses_accept_wildcard_repair_and_context_edit(
    path, body
):
    assert not mutation_request_allowed(path, body)


# --- read-only classification for the default closure checks ----------------
# The closure checks assert that a read-only surface changes NO steward/graph
# state. They used to classify by METHOD SHAPE alone (any POST/PUT/PATCH/DELETE
# is a mutation), which is dishonest in both directions: it flags two audited
# POST-shaped READS that the panels legitimately issue, and it silently ignores
# any method outside that hardcoded set. The classifier below is semantic and
# FAIL-CLOSED — an endpoint is a mutation unless it is explicitly audited and
# named here, so a newly added mutating POST breaks the closure checks until a
# human audits it.


@pytest.mark.parametrize(
    "url",
    [
        "https://app.example.test/api/metrics/resolve-by-dimension",
        "https://app.example.test/api/metrics/resolve-by-dimension?trace=1",
        "https://app.example.test/api/metrics/resolve-heatmap",
        "https://app.example.test/api/metrics/resolve-heatmap#frag",
    ],
)
def test_audited_post_shaped_reads_are_not_state_changing(url):
    """The grouped dimension panels and the sensor heatmap are POST only because
    their request carries a body; they resolve and compose, and write nothing."""
    assert not is_state_changing_request("POST", url)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get", "head"])
def test_read_shaped_methods_are_never_state_changing(method):
    assert not is_state_changing_request(
        method, "https://app.example.test/api/proposed-mappings"
    )


@pytest.mark.parametrize(
    "url",
    [
        # Mutation-capable in general: conversation memory plus the post-turn
        # auto-accept hook. The briefing's write_memory:false body does not make
        # the ENDPOINT a read, so it stays forbidden — a briefing that reappears
        # on a closure surface is a lifecycle bug to fix, never a waiver.
        "https://app.example.test/api/ask",
        # Clearly state-changing families.
        "https://app.example.test/api/metrics/correct-mapping",
        "https://app.example.test/api/metrics/set-authority",
        "https://app.example.test/api/proposed-mappings/abc/accept",
        "https://app.example.test/api/proposed-mappings/abc/reject",
        "https://app.example.test/api/admin/vocabulary/guidance/s/promote-requirement",
        "https://app.example.test/api/admin/learning/context-placement-check",
        "https://app.example.test/api/admin/evals/run",
        "https://app.example.test/api/feedback",
        "https://app.example.test/api/onboard/provision",
        # Unaudited computational POSTs stay forbidden until a closure surface
        # actually needs one AND its implementation has been audited.
        "https://app.example.test/api/metrics/resolve",
        "https://app.example.test/api/kg/analyze",
        "https://app.example.test/api/brand-new-endpoint",
    ],
)
def test_unaudited_and_mutating_posts_are_state_changing(url):
    assert is_state_changing_request("POST", url)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_put_patch_delete_are_always_state_changing(method):
    """Even on an audited read PATH — the audit covers the (method, path) pair."""
    for url in (
        "https://app.example.test/api/admin/llm-settings",
        "https://app.example.test/api/metrics/resolve-by-dimension",
    ):
        assert is_state_changing_request(method, url)


@pytest.mark.parametrize(
    "url",
    [
        # Deceptive prefixes/suffixes must NOT inherit the allowlist.
        "https://app.example.test/api/metrics/resolve-by-dimension/accept",
        "https://app.example.test/api/metrics/resolve-by-dimensionx",
        "https://app.example.test/api/metrics/resolve-by-dimension/",
        "https://app.example.test/evil/api/metrics/resolve-by-dimension",
        "https://app.example.test/api/metrics/resolve-heatmap/reset",
        "https://app.example.test/api/metrics/xresolve-heatmap",
    ],
)
def test_allowlist_matches_exact_paths_only(url):
    assert is_state_changing_request("POST", url)


@pytest.mark.parametrize("method", ["", "  ", "PROPFIND", "TRACE", "unknown"])
def test_unknown_methods_fail_closed(method):
    """Stronger than the old method-set check, which ignored anything outside
    POST/PUT/PATCH/DELETE entirely."""
    assert is_state_changing_request(
        method, "https://app.example.test/api/metrics/resolve-by-dimension"
    )


def test_login_is_not_declared_a_read():
    """``/api/auth/login`` changes session state. The closure fixtures install
    their recorder AFTER login rather than declaring login read-only."""
    assert is_state_changing_request("POST", "https://app.example.test/api/auth/login")


_ASK = "https://example.test/api/ask"


def test_only_the_dashboard_briefing_turn_is_stubbable():
    briefing = '{"question": "q", "session_id": "briefing-ab12-xyz", "write_memory": false}'
    assert is_dashboard_briefing_turn("POST", _ASK, briefing)
    assert is_dashboard_briefing_turn("post", _ASK + "?x=1", briefing)


@pytest.mark.parametrize("method,url,body", [
    ("POST", _ASK, '{"question": "q", "session_id": "sess-1", "write_memory": false}'),
    ("POST", _ASK, '{"question": "q", "session_id": "briefing-ab12"}'),
    ("POST", _ASK, '{"question": "q", "session_id": "briefing-ab12", "write_memory": true}'),
    ("POST", _ASK, '{"session_id": "xbriefing-ab12", "write_memory": false}'),
    ("POST", _ASK, "not json"),
    ("POST", _ASK, None),
    ("POST", _ASK, '["briefing-ab12"]'),
    ("POST", "https://example.test/api/ask/extra",
     '{"session_id": "briefing-ab12", "write_memory": false}'),
    ("PUT", _ASK, '{"session_id": "briefing-ab12", "write_memory": false}'),
])
def test_any_other_ask_turn_still_counts_as_a_mutation(method, url, body):
    assert not is_dashboard_briefing_turn(method, url, body)
    assert is_state_changing_request(method, url)
