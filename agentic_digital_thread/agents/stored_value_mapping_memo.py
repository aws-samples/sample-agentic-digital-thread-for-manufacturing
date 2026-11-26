# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-operation memo of the graph reads that decide whether a proposal
restates a ValueMapping already stored (``self_learning``).

An ingestion of one answer's proposal blocks or one mining run files many
candidates, most of them between the same two systems, and each would read
the same ValueMappings again. Inside :func:`operation_scope` each read is made
once per key, and made again when a ValueMapping changed since.

Contract. The suppression these reads feed is best-effort and fails open:
when the answer is unknown the proposal files.

- In this process every function that creates, merges, sets or deletes a
  ValueMapping is decorated with :func:`mutates_value_mappings`, which bumps
  one process-wide generation on every exit (success or a raise part-way). A
  memoized read made under an older generation is read again, so a mutation
  in any request, job or thread of this process is seen by the next lookup
  of every running operation. The generation is taken before the read, so a
  mutation landing while the read is in flight also makes it stale.
- Across processes (the app and the agent runtime are separate processes)
  nothing is shared and the graph carries no version. A mutation in another
  process is seen when the next operation starts. Until then, for at most the
  rest of one operation, a pair read before the change can file one duplicate
  of a mapping written meanwhile, or suppress one restatement of a mapping
  deleted meanwhile.

Outside a scope nothing is memoized. No imports from the app: the reader and
every writer import this module.
"""
from __future__ import annotations

import functools
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Hashable

#: Set on every decorated mutator so a test can pin the full set.
MARKER_ATTR = "__mutates_value_mappings__"

#: The memo of the innermost operation (``None`` outside one): key ->
#: ``(generation the read started under, answer)``.
_MEMO: "ContextVar[dict | None]" = ContextVar("stored_value_mapping_memo",
                                              default=None)

_GENERATION = 0
_GENERATION_LOCK = threading.Lock()


def generation() -> int:
    """The process-wide ValueMapping mutation generation."""
    with _GENERATION_LOCK:
        return _GENERATION


def note_value_mapping_change() -> None:
    """Bump the generation: every memoized read is stale from now on."""
    global _GENERATION
    with _GENERATION_LOCK:
        _GENERATION += 1


def mutates_value_mappings(fn):
    """Decorate a function that creates, merges, sets or deletes a
    ValueMapping: bump the generation on EVERY exit, since a raise part-way
    may already have changed the graph."""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            note_value_mapping_change()
    setattr(_wrapper, MARKER_ATTR, True)
    return _wrapper


@contextmanager
def operation_scope():
    """Memoize the reads made inside, for one operation. A nested scope
    shares the outer one. Context-local, like ``db.graph_statement_limit``;
    usable as a decorator."""
    if _MEMO.get() is not None:
        yield
        return
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def memoized(key: Hashable, read: Callable[[], Any]) -> Any:
    """``read()``, made once per ``key`` and generation inside an operation
    scope (a failed or empty answer is kept too); every time outside one."""
    memo = _MEMO.get()
    if memo is None:
        return read()
    current = generation()
    entry = memo.get(key)
    if entry is not None and entry[0] == current:
        return entry[1]
    answer = read()
    memo[key] = (current, answer)
    return answer
