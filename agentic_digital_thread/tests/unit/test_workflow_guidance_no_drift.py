# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No-drift guard for the canonical Mode-B graph-first workflow guidance.

ONE source of truth (``agents.workflow_guidance.MODE_B_WORKFLOW_GUIDANCE``) must be
what the orchestrator composes into its Mode-B system prompt, what the
``get_query_workflow`` MCP tool on the digital-thread gateway target returns
in-session, AND what ``docs/amazon-quick-skill.md`` is generated from. These tests
fail if a future edit lets any of these consumers diverge — so the AWS Quick
skill reliably gets the same workflow the orchestrator runs.

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.workflow_guidance import (
    MODE_B_WORKFLOW_GUIDANCE,
    compose_quick_system_prompt,
    get_quick_skill_payload,
    get_workflow_guidance,
    render_quick_skill_markdown,
    resolve_deployed_query_mode,
)

_DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "amazon-quick-skill.md"


# --------------------------------------------------------------------------- #
# (a) The orchestrator Mode-B prompt COMPOSES the canonical guidance verbatim.
# --------------------------------------------------------------------------- #
def test_mode_b_prompt_contains_canonical_workflow():
    import agents.orchestrator as orch

    prompt = orch._build_orchestrator_prompt("orchestrator")
    assert MODE_B_WORKFLOW_GUIDANCE in prompt, (
        "orchestrator Mode-B prompt must COMPOSE the canonical MODE_B_WORKFLOW_GUIDANCE "
        "verbatim — a divergence means the orchestrator no longer matches Quick"
    )


def test_mode_a_prompt_does_not_contain_mode_b_workflow():
    """Mode A delegates NL sub-questions and composes no SQL, so it must NOT carry the
    Mode-B compose-SQL workflow (guards the mode split stays intact)."""
    import agents.orchestrator as orch

    assert MODE_B_WORKFLOW_GUIDANCE not in orch._build_orchestrator_prompt("agent")


def test_orchestrator_delegate_block_is_the_canonical_source():
    """The orchestrator's Mode-B Step-4 block IS the canonical constant (single source),
    not an independent copy."""
    import agents.orchestrator as orch

    assert orch._MODE_B_DELEGATE_BLOCK is MODE_B_WORKFLOW_GUIDANCE


# --------------------------------------------------------------------------- #
# (b) The canonical payload helper carries the SAME text (single assembly point).
# --------------------------------------------------------------------------- #
def test_payload_helper_returns_canonical_workflow():
    """``get_quick_skill_payload`` — the single assembly point the doc generator and
    the MCP tool guidance derive from — must carry byte-identical canonical text."""
    payload = get_quick_skill_payload()
    assert payload["workflow"] == MODE_B_WORKFLOW_GUIDANCE, (
        "the payload workflow must be byte-identical to the canonical source (and thus "
        "to the orchestrator prompt) — no drift allowed"
    )
    # The full markdown the payload carries must embed the canonical workflow.
    assert MODE_B_WORKFLOW_GUIDANCE in payload["markdown"]
    assert payload["connection_steps"]  # Quick connection steps are present too.


# --------------------------------------------------------------------------- #
# (b2) The native gateway MCP tool RETURNS the SAME canonical text in-session.
# --------------------------------------------------------------------------- #
def test_query_workflow_tool_returns_canonical_workflow(monkeypatch):
    """``get_query_workflow`` (the read-only guidance tool on the digital-thread
    gateway target) must return byte-identical MODE_B_WORKFLOW_GUIDANCE on a Mode-B
    (``SYSTEM_QUERY_MODE=orchestrator``) deployment — so an in-session MCP client
    (e.g. AWS Quick) gets the SAME workflow the orchestrator runs and the
    endpoint/doc carry, with no out-of-band fetch and no drift."""
    from agents.digital_thread_server import get_query_workflow

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    assert get_query_workflow() == MODE_B_WORKFLOW_GUIDANCE


def test_query_workflow_tool_returns_mode_a_delegate_block(monkeypatch):
    """On a Mode-A (default) deployment the tool returns the orchestrator's own
    Mode-A delegate workflow — the mode-matched sibling of the Mode-B pin above."""
    import agents.orchestrator as orch
    from agents.digital_thread_server import get_query_workflow

    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    assert get_query_workflow() == orch._MODE_A_DELEGATE_BLOCK


def test_registered_query_workflow_tool_returns_canonical_workflow(monkeypatch):
    """Exercise it as an actually-registered MCP tool on the built server (not just the
    bare function) so we pin what a connected client would receive on Mode B."""
    from agents.digital_thread_server import create_digital_thread_server

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    mcp = create_digital_thread_server()
    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "get_query_workflow" in tool_names, (
        "the digital-thread gateway target must expose get_query_workflow so an MCP "
        "client discovers the workflow in-session"
    )
    result = asyncio.run(mcp.call_tool("get_query_workflow", {}))
    # FastMCP.call_tool returns (content_blocks, structured_result); the tool returns a
    # plain string, so the canonical text is the structured/text payload.
    assert MODE_B_WORKFLOW_GUIDANCE in _tool_result_text(result)


def test_registered_query_workflow_tool_returns_mode_a_delegate_block(monkeypatch):
    """Mode-A sibling of the registered-tool pin: a connected client on a default
    (``agent``) deployment receives the Mode-A delegate workflow."""
    import agents.orchestrator as orch
    from agents.digital_thread_server import create_digital_thread_server

    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    mcp = create_digital_thread_server()
    result = asyncio.run(mcp.call_tool("get_query_workflow", {}))
    assert orch._MODE_A_DELEGATE_BLOCK in _tool_result_text(result)


def _tool_result_text(result) -> str:
    """Extract the tool's returned text from FastMCP.call_tool's return value across
    versions (it returns a (content, structured) tuple; older ones a content list)."""
    content = result[0] if isinstance(result, tuple) else result
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# (c) The committed doc is GENERATED from the canonical source (no hand-drift).
# --------------------------------------------------------------------------- #
def test_generated_doc_embeds_canonical_workflow():
    assert MODE_B_WORKFLOW_GUIDANCE in render_quick_skill_markdown()


def test_committed_doc_matches_generator():
    """``docs/amazon-quick-skill.md`` must equal ``render_quick_skill_markdown()`` —
    regenerate it (``python -m agents.workflow_guidance``) after editing the constant."""
    assert _DOC_PATH.exists(), f"expected generated doc at {_DOC_PATH}"
    on_disk = _DOC_PATH.read_text(encoding="utf-8")
    assert on_disk == render_quick_skill_markdown(), (
        "docs/amazon-quick-skill.md is stale — regenerate with "
        "`cd agentic_digital_thread && PYTHONPATH=. python -m agents.workflow_guidance "
        "> ../docs/amazon-quick-skill.md`"
    )


def _paste_section(markdown: str) -> str:
    """The fenced block of the doc's section 3 (the text a user pastes)."""
    section = markdown[markdown.index("## 3. Paste this"):markdown.index("## 4. ")]
    fence = "````\n"
    start = section.index(fence) + len(fence)
    return section[start:section.index("\n````", start)]


def test_paste_section_is_the_complete_mode_b_quick_prompt():
    """The pasteable instructions are the full mode-matched Quick prompt, the
    same text ``get_client_instructions`` returns on a Mode-B deployment, so
    the shared Step 1 to Step 5 rules reach a user who pastes the doc."""
    assert _paste_section(render_quick_skill_markdown()) == (
        compose_quick_system_prompt("orchestrator").rstrip("\n"))


#: AWS Quick accepts at most 50,000 characters of custom-agent instructions.
QUICK_INSTRUCTIONS_MAX = 50_000
#: The project's own ceiling, kept below the platform maximum so a shared rule
#: added later fails here before the pasteable block stops fitting. Raised
#: 48,000 -> 48,500 for the compact instructions-in-data rule (Mode A Quick had
#: about 20 characters left), then 48,500 -> 48,900 for the containment rule's
#: interval clauses and the cause records rule (Mode A 48,863); it stays 1,100
#: under the 50,000 platform maximum.
QUICK_INSTRUCTIONS_BUDGET = 48_900


def test_paste_section_fits_the_quick_instruction_limit():
    paste = _paste_section(render_quick_skill_markdown())
    assert len(paste) <= QUICK_INSTRUCTIONS_BUDGET < QUICK_INSTRUCTIONS_MAX, len(paste)


@pytest.mark.parametrize("mode", ("agent", "orchestrator"))
def test_each_quick_prompt_fits_the_quick_instruction_limit(mode):
    """``get_client_instructions`` serves ``compose_quick_system_prompt`` for the
    deployed mode, and Mode A is the default, so both modes must fit, not only
    the Mode B block the doc pastes."""
    prompt = compose_quick_system_prompt(mode)
    assert len(prompt) <= QUICK_INSTRUCTIONS_BUDGET < QUICK_INSTRUCTIONS_MAX, (mode, len(prompt))


#: Standing rules that act only on what the ADT orchestrator's own turn
#: carries: the per-turn blocks its runtime writes (the route seed's concept
#: notes and unrecorded-terms line, the metric-layer result, the resolved time
#: windows) and the citation stamp its own agent hook adds to every tool
#: result. A client on the query gateway receives none of them.
_ORCHESTRATOR_TURN_ONLY = (
    "_CONCEPT_NOTES_RULE", "_METRIC_LAYER_RESULT_RULE", "_WITHHELD_SCOPE_RULE",
    "_UNRECORDED_TERMS_RULE", "_CITATION_STAMP_RULE", "_RESOLVED_TIME_WINDOWS_RULE",
)


@pytest.mark.parametrize("name", _ORCHESTRATOR_TURN_ONLY)
@pytest.mark.parametrize("mode", ("agent", "orchestrator"))
def test_orchestrator_turn_only_rules_stay_in_the_orchestrator_and_leave_quick(mode, name):
    import agents.orchestrator as orch

    rule = getattr(orch, name)
    assert orch._build_orchestrator_prompt(mode).count(rule) == 1, (mode, name)
    assert rule not in compose_quick_system_prompt(mode), (mode, name)


def test_quick_drops_exactly_the_orchestrator_turn_only_rules():
    """The Mode A Quick prompt is the orchestrator's own workflow slice with
    only those rules taken out: nothing else is cut or reworded."""
    import agents.orchestrator as orch
    from agents.workflow_guidance import _QUICK_WORKFLOW_END, _QUICK_WORKFLOW_START

    full = orch.ORCHESTRATOR_PROMPT
    start = full.index(_QUICK_WORKFLOW_START)
    workflow = full[start:full.index(_QUICK_WORKFLOW_END, start)].rstrip()
    for name in _ORCHESTRATOR_TURN_ONLY:
        workflow = workflow.replace(getattr(orch, name), "", 1)
    # The one rule Quick rewords: the instructions-in-data rule, in compact form.
    from agents.workflow_guidance import _QUICK_INSTRUCTIONS_IN_DATA_RULE
    workflow = workflow.replace(orch._INSTRUCTIONS_IN_DATA_RULE,
                                _QUICK_INSTRUCTIONS_IN_DATA_RULE, 1)
    assert workflow in compose_quick_system_prompt("agent")


def test_quick_drop_fails_loud_when_a_rule_leaves_the_workflow(monkeypatch):
    import agents.orchestrator as orch

    monkeypatch.setattr(orch, "ORCHESTRATOR_PROMPT",
                        orch.ORCHESTRATOR_PROMPT.replace(orch._CITATION_STAMP_RULE, ""))
    with pytest.raises(RuntimeError, match="orchestrator-only rule"):
        compose_quick_system_prompt("agent")


def test_doc_carries_each_shared_step_one_rule_exactly_once():
    import agents.orchestrator as orch

    markdown = render_quick_skill_markdown()
    for rule in (orch._DEFECT_RECORD_RULE, orch._QUOTED_VALUE_RULE, orch._CONTAINMENT_RULE):
        assert markdown.count(rule) == 1, rule[:60]
    assert markdown.count("QUALITY CONTAINMENT:") == 1


# --------------------------------------------------------------------------- #
# (d) Complete mode-appropriate Quick prompts derive from the shared workflow.
# --------------------------------------------------------------------------- #
def _assert_no_unresolved_prompt_placeholders(prompt: str) -> None:
    for placeholder in (
        "{delegate_block}",
        "{query_tool}",
        "{route_tool_bullet}",
        "{_ROUTE_SEED_CYPHER}",
    ):
        assert placeholder not in prompt
    assert "{{" not in prompt
    assert "}}" not in prompt


def test_complete_agent_quick_prompt_has_all_workflow_steps():
    from agents.orchestrator import _ROUTE_SEED_CYPHER

    prompt = compose_quick_system_prompt("agent")
    for step in range(1, 6):
        assert f"Step {step} —" in prompt
    assert "```ROUTE:" in prompt
    assert "optional `kg_context`" in prompt
    assert "older tools without it" in prompt
    assert "one-string `question` contract" in prompt
    assert "---VALUE-MAPPING-PROPOSAL---" not in prompt
    assert "Never emit proposal text blocks" in prompt
    assert _ROUTE_SEED_CYPHER in prompt
    _assert_no_unresolved_prompt_placeholders(prompt)


def test_complete_orchestrator_quick_prompt_uses_canonical_mode_b_workflow():
    from agents.orchestrator import _ROUTE_SEED_CYPHER

    prompt = compose_quick_system_prompt("orchestrator")
    assert "run_query" in prompt
    assert MODE_B_WORKFLOW_GUIDANCE in prompt
    assert _ROUTE_SEED_CYPHER in prompt
    assert "---VALUE-MAPPING-PROPOSAL---" not in prompt
    _assert_no_unresolved_prompt_placeholders(prompt)


def test_complete_quick_prompt_accepts_short_mode_aliases():
    assert compose_quick_system_prompt("a") == compose_quick_system_prompt("agent")
    assert compose_quick_system_prompt("b") == compose_quick_system_prompt("orchestrator")


# --------------------------------------------------------------------------- #
# (e) get_workflow_guidance returns the DEPLOYED-MODE-MATCHED workflow text.
# --------------------------------------------------------------------------- #
def test_get_workflow_guidance_orchestrator_is_canonical_mode_b():
    """Mode B returns the canonical constant ITSELF (identity — single source, no
    copy that could drift)."""
    assert get_workflow_guidance("orchestrator") is MODE_B_WORKFLOW_GUIDANCE
    assert get_workflow_guidance("b") is MODE_B_WORKFLOW_GUIDANCE


def test_get_workflow_guidance_agent_is_orchestrator_delegate_block():
    """Mode A returns the orchestrator's own Mode-A Step-4 delegate block (identity),
    which by construction includes the identifier-miss recovery text."""
    import agents.orchestrator as orch

    guidance = get_workflow_guidance("agent")
    assert guidance is orch._MODE_A_DELEGATE_BLOCK
    assert "IDENTIFIER-MISS RECOVERY" in guidance
    assert get_workflow_guidance("a") is orch._MODE_A_DELEGATE_BLOCK


def test_get_workflow_guidance_mode_split_holds():
    """The Mode-A text must not carry the Mode-B compose-SQL workflow."""
    assert MODE_B_WORKFLOW_GUIDANCE not in get_workflow_guidance("agent")


def test_get_workflow_guidance_explicit_unknown_mode_raises():
    """Explicit arguments are strict (same contract as compose_quick_system_prompt) —
    only env resolution fail-safes."""
    with pytest.raises(ValueError):
        get_workflow_guidance("bogus")


def test_get_workflow_guidance_env_orchestrator(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    assert get_workflow_guidance() is MODE_B_WORKFLOW_GUIDANCE


def test_get_workflow_guidance_env_unset_defaults_to_agent(monkeypatch):
    import agents.orchestrator as orch

    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    assert get_workflow_guidance() is orch._MODE_A_DELEGATE_BLOCK


def test_get_workflow_guidance_env_garbage_fail_safes_to_agent(monkeypatch):
    """Env resolution NEVER raises — a garbage deployed value renders Mode A, exactly
    mirroring orchestrator._build_orchestrator_prompt's env handling."""
    import agents.orchestrator as orch

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "bogus")
    assert get_workflow_guidance() is orch._MODE_A_DELEGATE_BLOCK


def test_resolve_deployed_query_mode_mirrors_orchestrator_env_handling(monkeypatch):
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    assert resolve_deployed_query_mode() == "agent"
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "  Orchestrator  ")
    assert resolve_deployed_query_mode() == "orchestrator"
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "bogus")
    assert resolve_deployed_query_mode() == "agent"
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "")
    assert resolve_deployed_query_mode() == "agent"
