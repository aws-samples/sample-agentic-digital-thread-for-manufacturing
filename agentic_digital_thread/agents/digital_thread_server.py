# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`digital-thread` MCP tool server.

A thin, stateless FastMCP server exposing the knowledge-graph tools as gateway
tools — NOT an LLM agent and NOT an orchestrator (no model, no reasoning loop). It
hosts the same `explore_graph` and `get_system_schema` implementations the
in-process orchestrator uses (imported from ``graph_tools`` — single source of
truth), so that an MCP client connected to the AgentCore Gateway (e.g. an Amazon
Quick chat agent) can run the orchestrator's mandatory graph-first workflow:
discover systems, fetch exact schema, and traverse identity rules.

Hosted on AgentCore Runtime in MCP mode as a reserved/built-in system
(``SYSTEM_ID=digital-thread`` — dispatched in ``runtime_app``), launched in VPC mode
so it can reach the VPC-private Neptune Analytics graph (ADR-0024). On the gateway
these tools are exposed prefixed as ``digital-thread___explore_graph`` and
``digital-thread___get_system_schema`` (the prefix derives from the SYSTEM_ID-based
target name).

The runtime/ASGI wiring (``/ping``, ``/invocations`` warmup, the ``/mcp`` vs ``/mcp/``
alias) lives in ``runtime_app._run_digital_thread`` — mirroring how
``system_agent.create_mcp_server`` pairs with ``runtime_app._run_system_agent``.
"""

import json
import logging
import re
import threading
import time
import traceback
from collections import deque

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import self_learning
from .connection_types import KNOWLEDGE_GRAPH_CONNECTION_TYPE
from .graph_tools import explore_graph, get_system_schema, verify_relationship
from .workflow_guidance import get_workflow_guidance

logger = logging.getLogger(__name__)

# The gateway target name; the gateway prefixes exposed tools as
# ``<TARGET_NAME>___<tool_name>``.
TARGET_NAME = "digital-thread"

# Connection type for the digital-thread system. Single-sourced in the slim,
# dependency-free ``agents.connection_types`` module so the onboarding
# provisioner (a minimal Lambda image that must NOT install mcp/strands) can
# import the constant without pulling this server's heavy transitive imports.
CONNECTION_TYPE = KNOWLEDGE_GRAPH_CONNECTION_TYPE

_VALUE_MAPPING_BURST_MAX = 10
_VALUE_MAPPING_BURST_WINDOW_SECONDS = 60.0
_value_mapping_bursts: dict[tuple[str, str, str, str], deque[float]] = {}
_value_mapping_burst_lock = threading.Lock()


def _reset_value_mapping_burst_state() -> None:
    """Clear in-process burst windows (offline-test seam)."""
    with _value_mapping_burst_lock:
        _value_mapping_bursts.clear()


def _claim_value_mapping_burst_slot(
    key: tuple[str, str, str, str],
) -> float | None:
    """Atomically reserve one field-pair write slot in the rolling window."""
    now = time.monotonic()
    cutoff = now - _VALUE_MAPPING_BURST_WINDOW_SECONDS
    with _value_mapping_burst_lock:
        window = _value_mapping_bursts.setdefault(key, deque())
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= _VALUE_MAPPING_BURST_MAX:
            return None
        window.append(now)
        return now


def _release_value_mapping_burst_slot(
    key: tuple[str, str, str, str], reservation: float,
) -> None:
    """Release a reservation whose persistence was dropped or failed."""
    with _value_mapping_burst_lock:
        window = _value_mapping_bursts.get(key)
        if window is None:
            return
        try:
            window.remove(reservation)
        except ValueError:
            return
        if not window:
            _value_mapping_bursts.pop(key, None)


def _confidence_to_float(confidence: str) -> float:
    """The agent's confidence word as the stored float. The table is shared
    with the response-text proposal parsers (``proposal_scoring``), so a word
    is stored the same way whichever channel the agent used."""
    from .proposal_scoring import confidence_word_to_float

    return confidence_word_to_float(confidence)


def _normalized_vm_fields(
    from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str,
) -> tuple[dict, list[str]]:
    """Strip the six required endpoint fields → ``(fields, missing names)``."""
    fields = {
        "from_system": (from_system or "").strip(),
        "from_field": (from_field or "").strip(),
        "from_value": (from_value or "").strip(),
        "to_system": (to_system or "").strip(),
        "to_field": (to_field or "").strip(),
        "to_value": (to_value or "").strip(),
    }
    return fields, [name for name, value in fields.items() if not value]


def _persist_value_mapping(
    burst_key: tuple, reservation: float, fields: dict, *,
    concept: str, confidence: str, method: str, reason: str,
) -> str:
    """Store the proposal; release the burst slot on drop/failure.

    ADR audit P12 (the pilot-env method-field leak): ``method`` lands in the TYPED
    proposal field — never a ``[method=...]`` prefix folded into
    ``query_context`` — and ``None`` normalizes to typed-empty (the attribute
    is simply absent), never the string ``"None"``. ``reason`` feeds both the
    typed ``reason`` evidence field and ``query_context``."""
    reason_text = (reason or "").strip()
    self_learning.reset_propose_outcome()
    try:
        mapping_id = self_learning.log_proposed_mapping(
            **fields,
            concept=concept,
            confidence=_confidence_to_float(confidence),
            query_context=reason_text[:500],
            proposed_type="value_mapping",
            source="agent",
            method=(method or "").strip(),
            reason=reason_text[:500],
        )
    except Exception:
        _release_value_mapping_burst_slot(burst_key, reservation)
        raise
    if mapping_id is None:
        return _not_stored_reply(burst_key, reservation)
    return json.dumps({"status": "logged", "mapping_id": mapping_id, "type": "value_mapping"})


def _not_stored_reply(burst_key: tuple, reservation: float) -> str:
    """The tool's answer when the proposal stored no review row, by the
    propose outcome; the burst slot is released unless the observation was
    staged."""
    outcome = self_learning.last_propose_outcome()
    if outcome == "staged":
        # A STAGED first observation is recorded evidence, not a drop:
        # saying "dropped" invited LLM reword-retries that
        # defeat the ≥2-independent-observations bar (ADR-0051 P5). The
        # burst reservation stays claimed: the observation consumed a write.
        return json.dumps({
            "status": "staged",
            "reason": "recorded; needs a second independent observation",
        })
    _release_value_mapping_burst_slot(burst_key, reservation)
    if outcome == "already_stored":
        # The pair is already a ValueMapping in the knowledge graph:
        # nothing to review, and nothing to retry.
        return json.dumps({
            "status": "already_stored",
            "reason": "this mapping is already stored in the knowledge graph",
        })
    return json.dumps({
        "status": "dropped",
        "reason": "not stored — filtered by learning gates",
    })


def save_value_mapping(
    from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str,
    method: str = "", confidence: str = "medium",
    reason: str = "", concept: str = "equipment-id",
) -> str:
    """Record a cross-system value mapping you resolved (e.g. an asset name in one
    system to its UUID/identifier in another) for human review.

    Call this for EVERY cross-system identifier you resolve — once accepted in the
    Review UI it becomes a permanent rule and eliminates future lookups. This is the
    tool-call equivalent of emitting a value-mapping proposal; do not also emit text
    proposal blocks.

    Args:
        from_system: system id where the value originates.
        from_field: table.column (or identifier type) in the from system.
        from_value: the identifier value in the from system.
        to_system: system id the value maps to.
        to_field: table.column (or identifier type) in the to system.
        to_value: the corresponding identifier value in the to system.
        method: how you matched them (semantic_similarity | hierarchy_position |
            value_format_match | foreign_key_match).
        confidence: low | medium | high.
        reason: one sentence explaining how you determined the match.
        concept: the canonical concept the mapping links (e.g. equipment-id, batch,
            work-order); defaults to equipment-id.
    """
    fields, missing = _normalized_vm_fields(
        from_system, from_field, from_value, to_system, to_field, to_value)
    if missing:
        reason_text = f"required mapping fields must be non-empty: {', '.join(missing)}"
        logger.warning("save_value_mapping rejected: %s", reason_text)
        return json.dumps({"status": "rejected", "reason": reason_text})

    burst_key = (fields["from_system"].casefold(), fields["from_field"].casefold(),
                 fields["to_system"].casefold(), fields["to_field"].casefold())
    reservation = _claim_value_mapping_burst_slot(burst_key)
    if reservation is None:
        reason_text = (
            "value-mapping burst limit exceeded; aggregate repeated values "
            "into one field-level rule before calling again"
        )
        logger.warning("save_value_mapping rejected for %s: %s", burst_key, reason_text)
        return json.dumps({"status": "rejected", "reason": reason_text})

    return _persist_value_mapping(
        burst_key, reservation, fields,
        concept=concept, confidence=confidence, method=method, reason=reason)


def save_correlation(
    pattern: str,
    observed: str = "",
    confidence: str = "medium",
    systems: list[str] | None = None,
) -> str:
    """Record a learned cross-system correlation pattern for human review.

    Call this after answering a cross-system question when the data reveals a
    meaningful pattern worth persisting (e.g. "downtime in CMMS correlates with
    quality drops in MES"). Tool-call equivalent of a correlation proposal block.

    Narrative correlations are FINDINGS, not join rules: the proposal is logged
    as ``proposed_type="insight"`` with EMPTY ``from_field``/``to_field`` (a
    sentence is never a table.column pair — same convention as
    ``runtime_app._extract_correlation_insights``). The pattern and observation
    text are preserved in ``query_context`` (``Auto-analysis: <pattern> |
    Observed: <observed>``, 500-char cap) — ingestion derives the annotation
    digest from it, so repeated identical narratives dedup.

    Args:
        pattern: the pattern, e.g. "<concept_a> in <system_a> correlates with
            <concept_b> in <system_b>".
        observed: one sentence describing what the data showed.
        confidence: low | medium | high.
        systems: the two system ids involved (e.g. ["cmms", "mes-mil"]).
    """
    systems = systems or []
    if len(systems) < 2:
        return json.dumps({"status": "skipped", "reason": "need two distinct systems"})
    narrative = f"{pattern} | Observed: {observed}" if observed else pattern
    mapping_id = self_learning.log_proposed_mapping(
        from_system=systems[0],
        from_field="",
        from_value="",
        to_system=systems[1],
        to_field="",
        to_value="",
        concept="correlation",
        confidence=_confidence_to_float(confidence),
        query_context=f"Auto-analysis: {narrative}"[:500],
        proposed_type="insight",
        source="agent",
    )
    return json.dumps({"status": "logged", "mapping_id": mapping_id, "type": "insight"})


def get_query_workflow() -> str:
    """Call this FIRST to learn the graph-first workflow for querying this digital thread; returns step-by-step guidance. This is guidance only — it executes nothing.

    Returns the workflow matched to THIS deployment's query mode (resolved from
    ``SYSTEM_QUERY_MODE`` at call time): on ``orchestrator`` (Mode B) deployments,
    the canonical compose-SQL workflow — the SAME text the ADT orchestrator composes
    into its system prompt and that the generated AWS Quick skill doc carries; on
    ``agent`` (Mode A) deployments, the orchestrator's own natural-language delegate
    workflow (including its identifier-miss recovery guidance). Zero-arg, pure,
    read-only: it queries nothing and mutates nothing — use it to learn HOW to drive
    the real tools this deployment exposes, then call those to actually query.
    """
    return get_workflow_guidance()


# Per-resolved-mode memo for get_client_instructions. The composed prompt is pure
# (mode → text) and moderately expensive (it lazily imports the orchestrator module
# and slices its prompt), so cache it per mode. Test seam mirrors
# ``_reset_value_mapping_burst_state``.
_client_instructions_cache: dict[str, str] = {}


def _reset_client_instructions_cache() -> None:
    """Clear the per-mode client-instructions memo (offline-test seam)."""
    _client_instructions_cache.clear()


def get_client_instructions() -> str:
    """Setup/instructions-fetch ONLY: returns the complete system prompt for configuring an external MCP client (e.g. AWS Quick) for this deployment's query mode. Call it once during integration setup; do NOT call it during normal query conversations. It executes nothing — read-only.

    Returns ``compose_quick_system_prompt`` for the deployed ``SYSTEM_QUERY_MODE``
    (preamble + full workflow + learning tail), memoized per resolved mode. The
    ``workflow_guidance`` import is function-scope to match the lazy-import
    convention on this path (``compose_quick_system_prompt`` itself lazily imports
    ``orchestrator`` — which imports THIS module at module level — so nothing here
    may pull ``orchestrator`` in at import time).
    """
    from .workflow_guidance import compose_quick_system_prompt, resolve_deployed_query_mode

    mode = resolve_deployed_query_mode()
    cached = _client_instructions_cache.get(mode)
    if cached is None:
        cached = compose_quick_system_prompt(mode)
        _client_instructions_cache[mode] = cached
    return cached


# Names of the guidance-only tools (single source of truth; keyed off the function
# names so they never drift). They are exposed on the gateway target for EXTERNAL
# MCP clients (e.g. AWS Quick) but are EXCLUDED from the ADT orchestrator's own
# toolset — the orchestrator already embeds the deployed-mode workflow in its system
# prompt, so discovering/calling either tool would be a wasted round-trip. The
# exclusion seam lives in ``orchestrator._wrap_gateway_client_exclude_guidance``.
GUIDANCE_TOOL_NAMES = frozenset({
    get_query_workflow.__name__,
    get_client_instructions.__name__,
})
# Deprecated singular alias; existing imports still resolve.
GUIDANCE_TOOL_NAME = get_query_workflow.__name__


# Tool registration policy. Names MUST stay `explore_graph` / `get_system_schema`
# so the gateway-prefixed names are predictable and the Quick prompt can reference
# them. Descriptions come from the functions' docstrings (single source of truth
# with the orchestrator). The write-back tools (save_value_mapping /
# save_correlation) replace the legacy response-text proposal blocks for clients
# (e.g. Quick) that cannot run the ADT app's regex parser — the agent CALLS them
# instead. ``get_query_workflow`` and ``get_client_instructions`` are pure,
# read-only GUIDANCE tools (they execute nothing): the former returns the
# deployed-mode workflow so an in-session MCP client (e.g. AWS Quick) discovers
# the graph-first workflow alongside the real tools — no out-of-band HTTP fetch —
# and the latter returns the FULL mode-matched setup prompt, fetched once at
# integration setup. Their meta names/descriptions keep them from being confused
# with or shadowing the real query tools.
_READ_ONLY_TOOLS = frozenset({
    explore_graph,
    get_system_schema,
    get_query_workflow,
    get_client_instructions,
})
_REGISTERED_TOOLS = (
    explore_graph,
    get_system_schema,
    verify_relationship,
    save_value_mapping,
    save_correlation,
    get_query_workflow,
    get_client_instructions,
)


def _register_tools(mcp: FastMCP) -> None:
    """Register the shared implementations as MCP tools (policy: see above)."""
    for fn in _REGISTERED_TOOLS:
        annotations = (
            ToolAnnotations(readOnlyHint=True, openWorldHint=False)
            if fn in _READ_ONLY_TOOLS
            else None
        )
        mcp.tool(
            name=fn.__name__,
            description=fn.__doc__,
            annotations=annotations,
        )(fn)


def _warm_guidance_texts() -> None:
    """WARM both deployed-mode guidance texts (fail-fast at server build).

    Each lazily imports ``orchestrator`` on first use (Mode-A guidance / prompt
    composition), so a broken import must surface at server startup, not on the
    first tool call.
    """
    try:
        get_query_workflow()
        get_client_instructions()
    except Exception:
        # The FULL traceback goes into the MESSAGE, digits masked: the runtime's
        # OTel log handler discards ``logger.exception``'s exc_info,
        # so the cause must be inline or it is invisible.
        masked_traceback = re.sub(r"\d", "#", traceback.format_exc())
        logger.error(
            "failed to warm mode-matched guidance texts at server build:\n%s",
            masked_traceback,
        )
        raise


def create_digital_thread_server(host: str = "0.0.0.0", port: int = 8000) -> FastMCP:  # nosec B104 - container service binds all interfaces
    """Build the `digital-thread` FastMCP server exposing the two graph tools.

    Registers the canonical ``graph_tools`` functions directly, using each
    function's name and docstring as the tool name and description so the gateway
    catalog / semantic-search ranking text matches the in-process orchestrator
    exactly (no drift). Returns the configured ``FastMCP`` instance; the caller
    mounts it (see ``runtime_app._run_digital_thread``).
    """
    mcp = FastMCP(name=f"adt-{TARGET_NAME}", host=host, port=port, stateless_http=True)
    _register_tools(mcp)
    logger.info(
        "digital-thread MCP server built: tools=%s",
        [fn.__name__ for fn in _REGISTERED_TOOLS],
    )
    _warm_guidance_texts()
    return mcp
