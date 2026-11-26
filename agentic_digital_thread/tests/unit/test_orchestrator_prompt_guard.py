# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression guard locking the genericized orchestrator system prompt (S4.4).

The orchestrator is topology-graph-derived: it learns the manufacturing
systems at runtime by querying the knowledge graph, NOT from any hardcoded
plant/system names baked into the prompt. This guard pins that contract so a
future edit cannot silently reintroduce demo-specific literals (Milwaukee,
Detroit, Cedar Rapids, Indianapolis, or the MKE/DET/CRR seed codes) or drop the
mandatory ``MATCH (s:System)`` topology query.

Offline / pure import — no AWS, no Neptune, no network. boto3/Neptune clients in
``orchestrator`` are constructed inside functions, not at import. ``strands`` /
``mcp`` / ``boto3`` are declared deps present in the test lane, so the import
approach is preferred; if they are ever missing offline we fall back to scanning
the module source text for the same tokens (still a valid regression guard).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Topology must be graph-derived: the prompt MUST instruct the System-node query.
_TOPOLOGY_QUERY = "MATCH (s:System)"

# No hardcoded plant / system names may appear (case-insensitive substring).
_BANNED_NAMES = [
    "Milwaukee",
    "Detroit",
    "Cedar Rapids",
    "Cedar",
    "Rapids",
    "Indianapolis",
]

# No standalone seed codes (word-boundary match to avoid substring false positives).
_BANNED_CODES = ["MKE", "DET", "CRR"]


def _load_prompt() -> str:
    """Return the orchestrator system prompt text.

    Prefer importing the module-level ``ORCHESTRATOR_PROMPT`` constant. If the
    offline lane lacks ``strands``/``mcp``/``boto3`` (import-time deps), fall
    back to reading the constant's f-string body straight from the source file
    so the guard still asserts against the real prompt content.
    """
    try:
        from agents.orchestrator import ORCHESTRATOR_PROMPT

        return ORCHESTRATOR_PROMPT
    except Exception:  # pragma: no cover - exercised only when deps are absent
        src_path = Path(__file__).resolve().parents[2] / "agents" / "orchestrator.py"
        source = src_path.read_text(encoding="utf-8")
        match = re.search(
            r'ORCHESTRATOR_PROMPT\s*=\s*f"""\\?\n(.*?)\n"""',
            source,
            re.DOTALL,
        )
        assert match, "could not locate ORCHESTRATOR_PROMPT f-string in source"
        return match.group(1)


@pytest.fixture(scope="module")
def prompt() -> str:
    return _load_prompt()


def test_prompt_instructs_graph_topology_query(prompt: str):
    """Topology is graph-derived: the System-node query instruction must remain."""
    assert _TOPOLOGY_QUERY in prompt, (
        f"orchestrator prompt must instruct {_TOPOLOGY_QUERY!r} so topology is "
        "discovered from the graph, not hardcoded"
    )


@pytest.mark.parametrize("name", _BANNED_NAMES)
def test_prompt_has_no_hardcoded_plant_names(prompt: str, name: str):
    """No demo plant/system name may be baked into the prompt."""
    assert name.lower() not in prompt.lower(), (
        f"hardcoded plant/system name {name!r} found in orchestrator prompt — "
        "topology must stay graph-derived"
    )


@pytest.mark.parametrize("token", _BANNED_CODES)
def test_prompt_has_no_standalone_seed_codes(prompt: str, token: str):
    """No standalone demo seed code (MKE/DET/CRR) may appear in the prompt."""
    assert re.search(r"\b" + token + r"\b", prompt) is None, (
        f"standalone seed code {token!r} found in orchestrator prompt — "
        "topology must stay graph-derived"
    )


def test_prompt_grounds_and_carries_no_sql_composition_guidance(prompt: str):
    """NEW contract (ADR-0035 / Task 1C): the orchestrator ROUTES and GROUNDS; it
    does NOT compose system SQL. The prompt MUST carry the HARD GROUNDING RULE
    (quantitative/aggregate/current-state facts come from a ``query_<system>``
    result, NEVER from graph metadata alone) and MUST NOT carry any SQL-composition
    guidance — no ``execute_sql`` tool naming, no ``CAST(`` / ``partition`` column
    hints, and no ``get_system_schema`` (physical schema access was removed from the
    orchestrator's toolset; the system agent owns its own schema and query)."""
    lower = prompt.lower()
    # The grounding rule must be present and delegate to a per-system query tool.
    assert "query_<system>" in prompt, "prompt must reference the query_<system> delegation tool"
    assert "grounding" in lower, "prompt must carry the HARD GROUNDING RULE"
    assert "graph metadata" in lower and "never" in lower, (
        "grounding rule must forbid reporting numbers from graph metadata alone"
    )
    # None of the removed SQL-composition strings may appear (Task 1C acceptance).
    for banned in ("execute_sql", "cast(", "partition", "get_system_schema"):
        assert banned not in lower, (
            f"SQL-composition guidance string {banned!r} must not appear in the "
            "orchestrator prompt — the system agent owns query composition"
        )


# ---------------------------------------------------------------------------
# Mode A usability hardening: bounded-parallel delegation + retry-on-unavailable.
# ---------------------------------------------------------------------------


def test_prompt_permits_bounded_parallel_delegation(prompt: str):
    """Delegation is BOUNDED-PARALLEL (ADR-0039), not sequential. The prompt must NOT
    carry the old sequential mandate / false 'concurrent causes No endpoint' rationale
    / hardcoded SiteWise special-case, and MUST invite delegating to multiple relevant
    systems concurrently (at most one call per system). The real cap is enforced in
    code by an asyncio.Semaphore, not by the prompt.

    NOTE: we deliberately do NOT assert absence of 'no endpoint or agent found' — that
    phrase legitimately stays in HANDLING SYSTEM ERRORS + the transient-marker list."""
    low = prompt.lower()
    for banned in (
        "sequential delegation",
        "one at a time",
        "one system at a time",
        "separate runtime",
        "sequential traversal required",
    ):
        assert banned not in low, (
            f"removed sequential-mandate string {banned!r} must not appear in the prompt"
        )
    assert "parallel delegation" in low, "prompt must carry the PARALLEL DELEGATION note"
    assert "concurrently" in low, "prompt must permit concurrent multi-system delegation"
    assert "you may issue" in low, "prompt must PERMIT (MAY) concurrent delegation, not forbid it"
    assert "at most one" in low, "prompt must still cap at one call per system"


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_prompt_has_no_sitewise_sequential_special_case(mode: str):
    """The hardcoded SiteWise 'sequential traversal required' special-case is removed
    from the generic orchestrator entirely (ADR-0039) — in BOTH modes."""
    orch = pytest.importorskip("agents.orchestrator")
    low = orch._build_orchestrator_prompt(mode).lower()
    assert "sequential traversal required" not in low, (
        f"{mode}: hardcoded SiteWise sequential special-case must be gone"
    )


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_hard_grounding_reconciles_with_injected_seed(mode: str):
    """HARD GROUNDING clause (a) must express the EITHER/OR reconciliation with the
    Mode-B injected seed ('already provided' → don't re-call, OR call explore_graph
    first), NOT the absolute 'no exceptions' contradiction (ADR-0039 / audit fix)."""
    orch = pytest.importorskip("agents.orchestrator")
    p = orch._build_orchestrator_prompt(mode)
    low = p.lower()
    assert "already provided" in low, f"{mode}: clause (a) must allow an already-provided seed"
    assert "explore_graph first" in low, f"{mode}: clause (a) must require explore_graph FIRST otherwise"
    # The old absolute contradiction must be gone.
    assert "no exceptions" not in low, (
        f"{mode}: grounding clause must not carry the absolute 'no exceptions' contradiction"
    )


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_grounding_contract_stated_once(mode: str):
    """Verbosity guard for the prompt collapse: 'HARD GROUNDING RULE' appears EXACTLY
    once per mode (the audit found the grounding contract stated 3x)."""
    orch = pytest.importorskip("agents.orchestrator")
    low = orch._build_orchestrator_prompt(mode).lower()
    assert low.count("hard grounding rule") == 1, (
        f"{mode}: HARD GROUNDING RULE must be stated once, not {low.count('hard grounding rule')}x"
    )


def test_prompt_retries_transient_errors_before_unavailable_fallback(prompt: str):
    """HANDLING SYSTEM ERRORS must instruct retrying the SAME query_<system> up to
    3 times with increasing backoff on a transient/unavailable error BEFORE the
    honest 'unavailable' fallback — while keeping the no-fabrication contract."""
    lower = prompt.lower()
    # Transient markers the model must treat as retryable.
    assert "no endpoint or agent found" in lower, (
        "prompt must name 'No endpoint or agent found' as a retryable transient error"
    )
    # Retry count + increasing backoff.
    assert "up to 3 times" in lower or "3 times" in lower, (
        "prompt must instruct up to 3 retries on a transient error"
    )
    assert "increasing" in lower, "prompt must instruct increasing waits between retries"
    # No-fabrication contract must remain fully intact.
    assert "never fabricate" in lower, "no-fabrication contract must remain"
    assert "unavailable" in lower, "honest unavailable fallback must remain"


def test_transient_tool_error_classifier_and_wrapper(monkeypatch):
    """Code-level retry: the transient-error classifier only flags retryable
    transient gateway/runtime errors (endpoint-not-found etc.), never genuine
    application errors or success results; and the wrapper retries a transient
    result then returns a subsequent success (no fabrication on exhaustion)."""
    import asyncio as _asyncio

    orch = pytest.importorskip("agents.orchestrator")

    # Success result -> not transient.
    assert orch._is_transient_tool_error({"status": "success", "content": [{"text": "ok"}]}) is False
    # Genuine application error -> not transient (must surface for honest fallback).
    assert orch._is_transient_tool_error(
        {"status": "error", "content": [{"text": "column foo does not exist"}]}
    ) is False
    # Endpoint-not-found (even wrapped in Strands' 'Tool execution failed:' prefix) -> transient.
    assert orch._is_transient_tool_error(
        {"status": "error", "content": [{"text": "Tool execution failed: No endpoint or agent found"}]}
    ) is True
    assert orch._is_transient_tool_error(
        {"status": "error", "content": [{"text": "runtime not responding"}]}
    ) is True
    # AgentCore gateway cold-runtime phrasing, verbatim from workshop event 20
    # (2026-09-24): the code-level retry must own this, not the model's judgement.
    assert orch._is_transient_tool_error(
        {"status": "error", "content": [{"text": (
            "McpException - MCP invocation failed: MCP server "
            "'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/"
            "arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A111122223333%3Aruntime"
            "%2Fadt_mes_x-ABCDEF0123/invocations?qualifier=DEFAULT' is not reachable"
        )}]}
    ) is True
    assert orch._retry_backoffs_for(
        {"status": "error", "content": [{"text": "MCP server 'x' is not reachable"}]}
    ) == orch._GATEWAY_RETRY_BACKOFFS_S
    # The same McpException prefix with the auth blip stays on the SHORT schedule.
    assert orch._retry_backoffs_for(
        {"status": "error", "content": [{"text": (
            "McpException - MCP invocation failed: Authorization error when sending message"
        )}]}
    ) == orch._GATEWAY_AUTH_RETRY_BACKOFFS_S

    # Zero out the backoff so the unit test does not actually sleep.
    monkeypatch.setattr(orch, "_GATEWAY_RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))

    # Wrapper: transient on first call, success on retry -> returns success.
    transient = {"status": "error", "content": [{"text": "No endpoint or agent found"}]}
    success = {"status": "success", "content": [{"text": "42"}]}
    calls = {"n": 0}

    class _FakeClient:
        async def call_tool_async(self, *a, **k):
            calls["n"] += 1
            return transient if calls["n"] == 1 else success

    client = _FakeClient()
    orch._wrap_gateway_client_with_retry(client)
    out = _asyncio.run(client.call_tool_async(name="query_x"))
    assert out == success and calls["n"] == 2

    # Wrapper: transient on every attempt -> returns the ORIGINAL transient error
    # unchanged after exhausting retries (no fabrication, honest fallback applies).
    calls2 = {"n": 0}

    class _AlwaysTransient:
        async def call_tool_async(self, *a, **k):
            calls2["n"] += 1
            return transient

    client2 = _AlwaysTransient()
    orch._wrap_gateway_client_with_retry(client2)
    out2 = _asyncio.run(client2.call_tool_async(name="query_x"))
    # initial attempt + 3 retries = 4 calls, and the transient error is surfaced.
    assert out2 == transient and calls2["n"] == 1 + len(orch._GATEWAY_RETRY_BACKOFFS_S)


# ---------------------------------------------------------------------------
# ADR-0035 A/B: SYSTEM_QUERY_MODE branches the orchestrator system prompt.
# Mode A ("agent", default) = NL-delegation to query_<system>; unchanged.
# Mode B ("orchestrator")   = the orchestrator composes read-only SQL and calls
# the generic run_query / describe_schema tools itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_prompt_stops_escalating_empty_explore_graph(mode: str):
    """Anti-thrash guard: after 1-2 empty explore_graph attempts for a concept, the
    prompt must instruct the orchestrator to STOP re-querying the graph with looser
    filters and instead conclude the entity/telemetry is not connected — answering
    from systems that DO hold data or reporting the gap honestly (no hardcoded
    system/plant/concept names). Prevents the 4-5 escalating empty-graph calls
    observed live on data that isn't onboarded."""
    orch = pytest.importorskip("agents.orchestrator")
    low = orch._build_orchestrator_prompt(mode).lower()
    assert "stop re-querying the graph after empty results" in low, (
        f"{mode}: prompt must carry the anti-escalation 'stop re-querying' guidance"
    )
    assert "progressively looser filters" in low, (
        f"{mode}: prompt must forbid re-issuing explore_graph with looser filters"
    )
    assert "not present in the connected systems" in low, (
        f"{mode}: prompt must conclude the entity/telemetry is not connected on empty graph"
    )
    # Guidance must stay generic — no hardcoded plant/system/concept names.
    for name in _BANNED_NAMES:
        assert name.lower() not in low, f"{mode}: anti-escalation guidance must stay generic"


def test_prompt_builder_agent_mode_is_byte_identical_default(prompt: str):
    """Mode A must stay EXACTLY as today: the mode-aware builder rendered for
    'agent' (and its no-arg/env default) must equal the module-level constant the
    legacy prompt-guard tests pin (regression lock for 'do not regress Mode A')."""
    orch = pytest.importorskip("agents.orchestrator")
    assert orch._build_orchestrator_prompt("agent") == prompt


def test_prompt_builder_defaults_to_agent_when_env_unset(monkeypatch):
    """With SYSTEM_QUERY_MODE unset, the no-arg builder renders the Mode-A prompt."""
    orch = pytest.importorskip("agents.orchestrator")
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    assert orch._build_orchestrator_prompt() == orch._build_orchestrator_prompt("agent")


def test_prompt_builder_reads_env_for_mode_b(monkeypatch):
    """SYSTEM_QUERY_MODE=orchestrator makes the no-arg builder render the Mode-B prompt."""
    orch = pytest.importorskip("agents.orchestrator")
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    assert orch._build_orchestrator_prompt() == orch._build_orchestrator_prompt("orchestrator")
    assert orch._build_orchestrator_prompt() != orch._build_orchestrator_prompt("agent")


def test_agent_mode_carries_mode_a_contract_and_not_mode_b():
    """Mode A: NL-delegation contract present (query_<system>, intent-level NL,
    bounded-parallel delegation) and the Mode-B compose-SQL surface ABSENT."""
    orch = pytest.importorskip("agents.orchestrator")
    p = orch._build_orchestrator_prompt("agent")
    low = p.lower()
    # Mode-A contract present.
    assert "query_<system>" in p, "Mode A must delegate to the query_<system> tool"
    assert "natural-language sub-question" in low, "Mode A must send intent-level NL"
    assert "never send column names" in low, "Mode A must keep the 'never send SQL' prohibition"
    assert "parallel delegation" in low and "concurrently" in low, (
        "Mode A must carry the bounded-parallel delegation note"
    )
    # Mode-B compose-SQL surface ABSENT.
    assert "run_query" not in p, "Mode A must not mention the Mode-B run_query tool"
    assert "describe_schema" not in p, "Mode A must not mention the Mode-B describe_schema tool"
    assert "compose a single read-only sql" not in low, (
        "Mode A must not carry Mode-B compose-SQL guidance"
    )


def test_orchestrator_mode_carries_mode_b_contract_and_not_mode_a():
    """Mode B: compose-read-only-SQL contract present (run_query, describe_schema,
    'compose a ... read-only SQL SELECT') and the Mode-A 'never send SQL'-style
    prohibition ABSENT (in this mode the orchestrator writes the SQL)."""
    orch = pytest.importorskip("agents.orchestrator")
    p = orch._build_orchestrator_prompt("orchestrator")
    low = p.lower()
    # Mode-B contract present.
    assert "run_query" in p, "Mode B must expose the run_query tool"
    assert "describe_schema" in p, "Mode B must expose the describe_schema tool"
    assert "compose" in low and "read-only sql" in low, (
        "Mode B must instruct composing a read-only SQL SELECT"
    )
    # Mode-A prohibition and NL-delegation tool ABSENT.
    assert "never send column names" not in low, (
        "Mode B must drop the Mode-A 'never send SQL' prohibition — here YOU compose the SQL"
    )
    assert "query_<system>" not in p, "Mode B must not delegate to a per-system NL tool"


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_hard_grounding_rule_shared_across_modes(mode: str):
    """The HARD GROUNDING RULE (every quantitative claim comes from a tool result,
    NEVER from graph metadata alone) is SHARED and must appear in BOTH modes."""
    orch = pytest.importorskip("agents.orchestrator")
    low = orch._build_orchestrator_prompt(mode).lower()
    assert "hard grounding rule" in low, f"{mode}: HARD GROUNDING RULE must be present"
    assert "grounding" in low, f"{mode}: grounding rule must be present"
    assert "graph metadata" in low and "never" in low, (
        f"{mode}: grounding rule must forbid reporting numbers from graph metadata alone"
    )


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_shared_sections_present_in_both_modes(mode: str):
    """Shared workflow sections (topology query, translate-via-graph, value-mapping
    proposals, never-refuse, length discipline) stay shared across both modes."""
    orch = pytest.importorskip("agents.orchestrator")
    p = orch._build_orchestrator_prompt(mode)
    low = p.lower()
    assert _TOPOLOGY_QUERY in p, f"{mode}: graph-topology query must remain"
    assert "value-mapping-proposal" in low, f"{mode}: value-mapping proposals must remain"
    assert "never refuse" in low, f"{mode}: never-refuse contract must remain"
    assert "parallel delegation" in low, f"{mode}: bounded-parallel delegation note must remain"
