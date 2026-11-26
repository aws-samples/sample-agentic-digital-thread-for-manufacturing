# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""EVENT-LOOP OFFLOAD — the ONE home for running blocking work from ``async``
route handlers (single-worker uvicorn protection).

The deployed app is ONE ECS task running ONE uvicorn worker. Any sync I/O
(boto3, PostgreSQL, the Neptune ExecuteQuery HTTPS round-trip, file reads)
executed inline in an ``async def`` handler freezes the only event loop for
its duration: /health times out, the ALB/ECS health checks fail, and ECS
replaces the task (observed as 60 s stalls and a replacement "death loop" —
see the note in ``stacks/adt_compute_stack.py``). The pattern, applied to
EVERY async route that touches sync I/O:

  * the handler stays ``async def``,
  * its ENTIRE sync body lives in a plain module-level function,
  * the handler's only statement is ``return await offload(fn, ...)`` (or
    ``offload_resolver`` for the dashboard/metric resolver family).

Naming, for a hoisted route ``NAME``: the sync body KEEPS the original name
(``def NAME(...)`` — so its history, source-inspection tests and the
maintainability ratchet's function identity are preserved) and the async
handler is ``async def NAME_route(...)``.

Two lanes, each a per-event-loop semaphore in front of ``asyncio.to_thread``:

``offload_resolver``
    The dashboard / metric-resolver family (``metric_resolver``,
    ``dashboard``, ``dashboard_drill``, ``heatmap``). Bounded to
    :data:`RESOLVER_MAX_CONCURRENCY` so a request convoy cannot pile
    unbounded threads onto the source databases.

``offload``
    Every other route (admin, onboarding, graph, eval). Bounded to
    :data:`GENERAL_MAX_CONCURRENCY`, a separate lane so a burst of slow
    onboarding calls cannot starve the dashboard resolvers (and vice versa).

Semaphores are LOOP-BOUND in Python >= 3.10 — they are keyed by the running
loop so multiple loops (tests use repeated ``asyncio.run``) never trip the
"bound to a different event loop" guard; WeakKey => no leak across loops.

/health (server.py) stays trivially non-blocking and is NOT routed through
either lane. Pinned repo-wide by ``tests/unit/test_event_loop_offload.py``.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import weakref
from concurrent.futures import ThreadPoolExecutor

RESOLVER_MAX_CONCURRENCY = 4
GENERAL_MAX_CONCURRENCY = 16

# Each lane owns its thread pool. ``asyncio.to_thread`` would share the loop's
# DEFAULT executor (min(32, cpu+4) workers) between both lanes and with every
# other to_thread caller, so a saturated general lane could starve resolver
# calls — the exact coupling the two lanes exist to prevent.
_RESOLVER_POOL = ThreadPoolExecutor(max_workers=RESOLVER_MAX_CONCURRENCY,
                                    thread_name_prefix="adt-offload-resolver")
_GENERAL_POOL = ThreadPoolExecutor(max_workers=GENERAL_MAX_CONCURRENCY,
                                   thread_name_prefix="adt-offload-general")

_resolver_semaphores: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_general_semaphores: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _semaphore(registry: "weakref.WeakKeyDictionary", limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = registry.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        registry[loop] = sem
    return sem


def _resolver_semaphore() -> asyncio.Semaphore:
    """The per-event-loop resolver semaphore (lazily created)."""
    return _semaphore(_resolver_semaphores, RESOLVER_MAX_CONCURRENCY)


def _general_semaphore() -> asyncio.Semaphore:
    """The per-event-loop general-lane semaphore (lazily created)."""
    return _semaphore(_general_semaphores, GENERAL_MAX_CONCURRENCY)


async def _run_in(pool: ThreadPoolExecutor, fn, args, kwargs):
    """``asyncio.to_thread`` semantics (contextvars propagated) on *pool*."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn, *args, **kwargs)
    return await loop.run_in_executor(pool, call)


async def offload_resolver(fn, /, *args, **kwargs):
    """Run blocking resolver/boto3 work in a worker thread without blocking the
    event loop, bounded to ``RESOLVER_MAX_CONCURRENCY`` concurrent resolves."""
    async with _resolver_semaphore():
        return await _run_in(_RESOLVER_POOL, fn, args, kwargs)


async def offload(fn, /, *args, **kwargs):
    """Run blocking route work in a worker thread without blocking the event
    loop, bounded to ``GENERAL_MAX_CONCURRENCY`` concurrent calls."""
    async with _general_semaphore():
        return await _run_in(_GENERAL_POOL, fn, args, kwargs)


async def offload_holding(release, fn, /, *args, **kwargs):
    """``offload`` for work that runs under a claim the caller took on the loop
    (a slot that keeps a second request from overlapping this one).

    ``release()`` runs when the WORK finishes, not when the awaiting coroutine
    ends: a client disconnect cancels the handler, but the worker thread keeps
    going, and releasing then would let the next request overlap it. The
    caller still sees the cancellation. If the work cannot be scheduled,
    ``release()`` runs at once. A failure of the work is retrieved here, so a
    cancelled caller never leaves an "exception was never retrieved" behind."""
    try:
        work = asyncio.ensure_future(offload(fn, *args, **kwargs))
    except BaseException:
        release()
        raise

    def _done(fut: "asyncio.Future") -> None:
        release()
        if not fut.cancelled():
            fut.exception()

    work.add_done_callback(_done)
    return await asyncio.shield(work)
