# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-turn budget for the orchestrator's own ``explore_graph`` tool.

Measured on an analytics deployment: in one turn the orchestrator called
explore_graph 17 times, mostly repeating searches that had come back empty
(the empty result already says not to broaden or retry), never asked a
connected system, and answered that no question had come through.

Two deterministic limits, counted per runtime turn. The runtime entrypoint
opens one budget per turn (:func:`reset_turn_budget`, a ContextVar), so every
agent invocation of the turn shares it, the output-cap continuation's second
invocation included. A call outside a runtime turn (local mode, tests) counts
per agent invocation instead (Strands ``invocation_state``):

* :data:`GRAPH_CALLS_PER_TURN` graph queries run in a turn; a further call is
  not run and returns :func:`spent_result` (``reason: turn_limit``);
* a search whose :func:`query_shape` came back empty
  :data:`EMPTY_REPEATS_PER_SHAPE` times is not run again
  (``reason: repeated_empty``). Another search still runs.

Both refusals return a structured result telling the model to delegate to a
connected system or answer from what it has; neither runs the query. Only the
orchestrator's in-process tool is budgeted: the gateway ``digital-thread``
copy shares the docstring and has no budget. A call with neither a runtime
turn nor an invocation state runs unchanged.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
from collections import Counter

from strands import ToolContext, tool

logger = logging.getLogger(__name__)

#: Most graph queries one orchestrator turn runs. The highest count in a turn
#: that passed, across the recorded golden and smoke runs, is 10; the routing
#: flow the prompt describes (the route seed, at most one batched identifier
#: lookup, at most two empty attempts, a concept read) needs far fewer.
GRAPH_CALLS_PER_TURN = 12
#: Empty results one search shape may return before it is no longer run.
EMPTY_REPEATS_PER_SHAPE = 2
#: Reserved ``invocation_state`` key (``_adt_`` family, as in investigation_trace).
STATE_KEY = "_adt_graph_turn_budget"

_RETURN_RE = re.compile(r"\breturn\b.*?(?=\bunion\b|$)", re.DOTALL)
_SPACE_RE = re.compile(r"(?:\\n|\\t|\s)+")

_TURN_LIMIT_NOTE = (
    "This turn has already run {limit} graph queries, the most one turn runs, so this "
    "query was not run. The graph holds system metadata and rules only. The user's "
    "question is unchanged: delegate it to a connected system now with what you know, "
    "or answer it from what you have and say what you could not find. Do not call "
    "explore_graph again this turn.")
_REPEATED_EMPTY_NOTE = (
    "This search came back empty twice this turn, so it was not run again: it cannot find "
    "anything new. Treat what it looked for as absent from the graph. The user's question "
    "is unchanged: delegate it to a connected system, or answer it from what you have and "
    "say what you could not find.")


def query_shape(cypher: object) -> str:
    """The search a query makes: case and spacing folded, and each RETURN
    projection (with its ORDER BY, SKIP and LIMIT) left out, so the same search
    with other columns, another order or another page has the same shape.
    Every value in MATCH and WHERE is kept, numbers included: another value
    (``a.level = 2`` after ``a.level = 1``) is another search."""
    text = _SPACE_RE.sub(" ", str(cypher or "")).strip().lower()
    return _RETURN_RE.sub("", text).strip()


def _is_empty(result: object) -> bool:
    """True for the graph tool's empty envelope (``empty: true``)."""
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("empty") is True


class _TurnBudget:
    """One turn's counts. Shared by the turn's concurrent tool calls."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = 0
        self.empties: Counter = Counter()

    def claim(self, shape: str) -> "str | None":
        """``None`` when this call may run (and counts it), else the refusal reason."""
        with self.lock:
            if self.empties[shape] >= EMPTY_REPEATS_PER_SHAPE:
                return "repeated_empty"
            if self.calls >= GRAPH_CALLS_PER_TURN:
                return "turn_limit"
            self.calls += 1
            return None

    def record(self, shape: str, result: object) -> None:
        if _is_empty(result):
            with self.lock:
                self.empties[shape] += 1


def spent_result(reason: str, calls: int) -> str:
    """The structured result of a call the budget did not run."""
    note = (_REPEATED_EMPTY_NOTE if reason == "repeated_empty"
            else _TURN_LIMIT_NOTE.format(limit=GRAPH_CALLS_PER_TURN))
    return json.dumps({"graph_budget_spent": True, "not_run": True, "reason": reason,
                       "graph_calls_this_turn": calls, "limit": GRAPH_CALLS_PER_TURN,
                       "results": [], "note": note})


_TURN: "contextvars.ContextVar[_TurnBudget | None]" = contextvars.ContextVar(
    "adt_graph_turn_budget", default=None)


def reset_turn_budget() -> None:
    """Open a fresh budget for this runtime turn (``runtime_app._runtime_entrypoint``,
    at the start of every invocation of the runtime)."""
    _TURN.set(_TurnBudget())


def current_turn_budget() -> "_TurnBudget | None":
    """The open runtime turn's budget, ``None`` outside a runtime turn."""
    return _TURN.get()


def _budget_for(invocation_state) -> "_TurnBudget | None":
    """The runtime turn's budget; else (outside a runtime turn) one per agent
    invocation; else none."""
    turn = _TURN.get()
    if turn is not None:
        return turn
    if isinstance(invocation_state, dict):
        return invocation_state.setdefault(STATE_KEY, _TurnBudget())
    return None


def run_budgeted(run, cypher: str, invocation_state) -> str:
    """``run(cypher)`` unless this turn's budget refuses it. With neither a
    runtime turn nor an invocation state nothing is counted."""
    budget = _budget_for(invocation_state)
    if budget is None:
        return run(cypher)
    shape = query_shape(cypher)
    reason = budget.claim(shape)
    if reason is not None:
        logger.info("GRAPH_TURN_BUDGET reason=%s calls=%d limit=%d",
                    reason, budget.calls, GRAPH_CALLS_PER_TURN)
        return spent_result(reason, budget.calls)
    result = run(cypher)
    budget.record(shape, result)
    return result


def budgeted_graph_tool(run, *, description: "str | None" = None):
    """The orchestrator's ``explore_graph`` tool around ``run``: same name,
    description and input schema as ``tool(run)``, with this turn's budget."""
    def explore_graph(cypher: str, tool_context: "ToolContext | None" = None) -> str:
        return run_budgeted(run, cypher, getattr(tool_context, "invocation_state", None))

    explore_graph.__doc__ = run.__doc__
    return tool(explore_graph, name="explore_graph", description=description, context=True)
