# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What a synchronous ``single_cycle`` invoke of the simulation Lambda did.

The simulator steps run one cycle at a time (``SIMULATOR_LOCK_KEY`` in
simulation_orchestrator). An invoke that finds another cycle running them runs
only the clock steps and answers ``simulator: busy``: it succeeded, but no
simulator step ran. The callers that drive cycles themselves (the dashboard's
"Single Cycle" and "Reset All Data", the cleanup Lambda's warmup) read the
reply through :func:`cycle_outcome` and run a fixed number of cycles through
:func:`run_cycles`, so a busy reply is waited out instead of counted.

Stdlib only: the dashboard and the cleanup Lambda import it.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional, Tuple

RAN = "ran"
BUSY = "busy"
FAILED = "failed"

#: Waiting out a busy simulator: one retry every BUSY_RETRY_SECONDS, at most
#: BUSY_BUDGET_SECONDS of waiting per run_cycles call (a running cycle ends
#: within the Lambda's 300 s timeout).
BUSY_RETRY_SECONDS = 10
BUSY_BUDGET_SECONDS = 300


def cycle_outcome(function_error: Optional[str], payload: Any) -> str:
    """``ran`` / ``busy`` / ``failed`` for one invoke's FunctionError and payload.

    A reply without a ``simulator`` field (an image built before the lock)
    ran its steps, as every cycle then did."""
    if function_error:
        return FAILED
    if not isinstance(payload, dict):
        return RAN
    if payload.get("statusCode") == 500:
        return FAILED
    body = payload.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = None
    if isinstance(body, dict) and body.get("simulator") == BUSY:
        return BUSY
    return RAN


def run_cycles(invoke_once: Callable[[], Tuple[Optional[str], Any]], count: int, *,
               on_cycle: Optional[Callable[[int, str], None]] = None,
               on_busy: Optional[Callable[[int], None]] = None,
               retry_seconds: int = BUSY_RETRY_SECONDS,
               budget_seconds: int = BUSY_BUDGET_SECONDS,
               sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Run ``count`` cycles through ``invoke_once() -> (function_error, payload)``.

    A cycle that ran or failed counts toward ``count`` (``on_cycle(n, outcome)``
    after each); a busy reply waits ``retry_seconds`` and retries
    (``on_busy(waited)`` first) until ``budget_seconds`` of waiting is spent,
    then stops early with ``gave_up`` set. Exceptions from ``invoke_once``
    propagate to the caller."""
    ran = failed = waited = 0
    while ran + failed < count:
        outcome = cycle_outcome(*invoke_once())
        if outcome == BUSY:
            if waited + retry_seconds > budget_seconds:
                break
            if on_busy is not None:
                on_busy(waited)
            sleep(retry_seconds)
            waited += retry_seconds
            continue
        if outcome == RAN:
            ran += 1
        else:
            failed += 1
        if on_cycle is not None:
            on_cycle(ran + failed, outcome)
    return {"ran": ran, "failed": failed, "waited_seconds": waited,
            "gave_up": ran + failed < count}
