# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The lease an operation writes under, bound for everything it calls.

A removal, a Reset All and a provision each hold a lease (``reset_lease``)
and bind its check here for their whole run (:func:`write_guard`). Every
write they make checks it first, so a lost lease stops the operation before
its next write, wherever that write is made:

* every attempt of every AWS API call goes through one botocore
  ``before-sign`` handler (:func:`_before_aws_attempt`): the SDK's own
  retries build and sign a new request each, and the event fires for each
  before it is signed. Under a bound check it raises the check's error for
  any operation that is not a known read (:func:`is_read_operation`): an
  operation it does not know is a write. :func:`install` (run when the
  ``agents`` package is imported) adds it to every botocore session created
  from then on, and to every session and client that already exists;
* graph statements: ``db.neptune_write*`` call :func:`check_write_guard`, and
  ``db.neptune_query_checked`` runs a statement as a read only when the
  read-only classifier finds no write in it (:func:`graph_read`; the local
  graph backend is not AWS, and a Neptune Analytics statement is one
  ``ExecuteQuery`` whether it reads or writes);
* side effects that are not AWS API calls (the runtime warmup POST, the local
  system agent's process) and this process's own state (the catalog, the
  agent registry) call :func:`check_write_guard` first. Something an
  operation started that must not outlive a lost lease registers how to undo
  it (:func:`release_on_loss`).

The binding is a ``ContextVar``: ``asyncio.to_thread`` and ``offload`` carry
it to their worker thread; a ``ThreadPoolExecutor.submit`` or a
``threading.Thread`` does not, so the pools a job's work runs on submit
through :func:`submit_in_context`. A long-lived thread a job starts (the MQTT
subscriber, the re-derivation worker) does not inherit it on purpose: it
outlives the job. Outside a bound check nothing changes.
"""
from __future__ import annotations

import contextvars
import gc
import importlib.abc
import importlib.util
import logging
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

#: The check every write runs before it is sent (:func:`write_guard`).
_WRITE_GUARD: "ContextVar[object]" = ContextVar("graph_write_guard", default=None)
#: Whether the Neptune Analytics statement being sent is a read (:func:`graph_read`).
_GRAPH_READ: "ContextVar[bool]" = ContextVar("graph_read", default=False)


class _Scope:
    """One operation's run under its check: whether the check has failed
    (the lease is lost) and what to undo then (:func:`release_on_loss`).
    Shared by every copy of the context, so a failure on a worker thread
    counts too, and by every nested :func:`write_guard` of the same check,
    so an undo registered inside one survives its exit."""

    def __init__(self) -> None:
        self.lost = False
        self.on_loss: list = []


_SCOPE: "ContextVar[_Scope | None]" = ContextVar("lease_scope", default=None)


@contextmanager
def write_guard(check):
    """Run ``check()`` before every write inside: every graph write
    (``db.neptune_write*``), every graph statement that is not a read, every
    attempt of every AWS API call that is not a known read
    (:func:`_before_aws_attempt`), the side effects that are not AWS calls
    and every change to this process's catalog and agent registry. Whatever
    it raises stops the write before it is sent, and reaches the caller. An
    operation that may write only while it holds a lease binds the lease's
    check (``reset_lease.Renewal.check``), so a lost lease stops it before its
    next write. Once the check has failed, what the operation registered
    with :func:`release_on_loss` is undone on leaving its outermost guard. A
    guard nested in one with the same check (a provision's claim, then its
    ``_provision_in_flight``) is the same operation: it joins the enclosing
    scope instead of opening its own, so an undo registered inside it runs
    when a later step of the operation loses the lease. A guard with another
    check opens a scope of its own. Context-local: ``asyncio.to_thread``
    carries it to its thread, a plain thread or pool does not
    (:func:`submit_in_context`)."""
    enclosing = _SCOPE.get()
    owner = enclosing is None or _WRITE_GUARD.get() != check
    scope = _Scope() if owner else enclosing
    token = _WRITE_GUARD.set(check)
    scope_token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(scope_token)
        _WRITE_GUARD.reset(token)
        if owner and scope.lost:
            _release(scope)


def _run_check(check) -> None:
    """Run *check*; when it raises, the bound operation may not write any
    more, so its scope counts as lost before the error propagates."""
    try:
        check()
    except BaseException:
        scope = _SCOPE.get()
        if scope is not None:
            scope.lost = True
        raise


def check_write_guard() -> None:
    """Run the bound ``write_guard`` check now (nothing when none is bound):
    for a write that is neither an AWS API call nor a graph write, and for a
    step boundary that must not start once the operation's lease is lost."""
    check = _WRITE_GUARD.get()
    if check is not None:
        _run_check(check)


def release_on_loss(undo) -> None:
    """Have ``undo()`` run when the bound operation's check fails, here or in
    any later step of the operation: on leaving its outermost ``write_guard``
    after a lost lease, newest first. For something the operation started
    that must not outlive it (a local agent process). Nothing outside a bound
    check."""
    scope = _SCOPE.get()
    if scope is not None:
        scope.on_loss.append(undo)


def _release(scope: _Scope) -> None:
    for undo in reversed(scope.on_loss):
        try:
            undo()
        except Exception:  # noqa: BLE001 - every undo runs; a failure is logged
            logger.warning("Undoing a step of an operation whose lease was lost failed",
                           exc_info=True)


@contextmanager
def graph_read(cypher):
    """Run the openCypher statement *cypher* inside. Its Neptune Analytics
    ``ExecuteQuery`` is a read for :func:`is_read_operation` only when
    ``cypher_read_only.write_clause`` finds no write in it. Fail closed: a
    statement that writes, or that the classifier cannot read, is not marked
    and runs the bound check now, before it reaches either graph backend."""
    from .cypher_read_only import write_clause
    read = write_clause(cypher) is None
    if not read:
        check_write_guard()
    token = _GRAPH_READ.set(read)
    try:
        yield
    finally:
        _GRAPH_READ.reset(token)


def submit_in_context(pool, fn, /, *args, **kwargs):
    """``pool.submit(fn, *args, **kwargs)`` run in a copy of the caller's
    context, as ``asyncio.to_thread`` runs its function, so the bound write
    check reaches the worker thread."""
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)


#: AWS operations that read and change nothing, by name. Fail closed: any
#: operation not matched here is a write.
_READ_OPERATION = re.compile(r"^(?:Get|List|Describe|BatchGet|Head)[A-Z]")
_READ_OPERATIONS = frozenset({"Query", "Scan", "TransactGetItems"})
#: Reads whose name does not say so: SiteWise's ExecuteQuery is only a query.
_READ_SERVICE_OPERATIONS = frozenset({("iotsitewise", "ExecuteQuery")})
#: Neptune Analytics' ExecuteQuery runs a statement that may write; the
#: graph layer marks the reads (:func:`graph_read`).
_GRAPH_STATEMENT = ("neptune-graph", "ExecuteQuery")


def is_read_operation(service: str, operation: str) -> bool:
    """Whether the AWS operation *operation* of *service* (as botocore's
    service id names it, ``neptune-graph`` and ``iotsitewise`` for the two
    the rule names) only reads: a ``Get``/``List``/``Describe``/``BatchGet``/
    ``Head`` operation, ``Query``, ``Scan``, ``TransactGetItems``, SiteWise's
    ``ExecuteQuery``, and Neptune Analytics' ``ExecuteQuery`` of a statement
    :func:`graph_read` classified as a read. Everything else counts as a
    write."""
    if (service, operation) == _GRAPH_STATEMENT:
        return _GRAPH_READ.get()
    if operation in _READ_OPERATIONS or (service, operation) in _READ_SERVICE_OPERATIONS:
        return True
    return bool(_READ_OPERATION.match(operation))


def _before_aws_attempt(operation_name=None, event_name: str = "", **_kwargs):
    """botocore ``before-sign`` handler. botocore emits it for every attempt
    of a call, the SDK's own retries included (each retry builds a new
    request, ``Endpoint.create_request``, whose ``request-created`` signer
    emits it), before the attempt is signed. Under a bound write check, an
    operation that is not a known read (:func:`is_read_operation`) runs the
    check first, so a lost lease raises before that attempt is signed or
    sent. Answers ``None``: botocore then signs and sends as usual."""
    check = _WRITE_GUARD.get()
    if check is None or not operation_name:
        return None
    parts = event_name.split(".")
    service = parts[1] if len(parts) > 2 else ""
    if not is_read_operation(service, operation_name):
        _run_check(check)
    return None


#: The handler as botocore registers its built-in handlers on each new session.
_EVENT = "before-sign"
_HANDLER_SPEC = (_EVENT, _before_aws_attempt)
_UNIQUE_ID = "adt-lease-write-guard"


def install() -> None:
    """Make :func:`_before_aws_attempt` a handler of every botocore session
    and client: it joins ``botocore.handlers.BUILTIN_HANDLERS``, which each
    new session registers (``boto3.client``, ``boto3.resource``,
    ``boto3.Session`` and ``botocore.session.get_session`` all create theirs
    that way), and every session and client that already exists (a client
    copied its session's handlers when it was created). Run when the
    ``agents`` package is imported: every process that runs a reset or a
    provision starts from one of its modules, so this runs before any line
    of it. It imports neither boto3 nor botocore (modules that must import
    without them still can): when botocore is not loaded yet, a one-shot
    import hook registers the handler the moment ``botocore.handlers`` is,
    and ``botocore.session`` imports it before any session can exist.
    Idempotent."""
    handlers = sys.modules.get("botocore.handlers")
    if handlers is not None:
        _register(handlers, live=True)
    elif not any(isinstance(f, _AfterBotocoreHandlers) for f in sys.meta_path):
        sys.meta_path.insert(0, _AfterBotocoreHandlers())


def _register(handlers, live: bool = False) -> None:
    """Add the handler to the loaded ``botocore.handlers`` module's built-in
    list (once), and with *live* to every botocore session and client this
    process already has."""
    if _HANDLER_SPEC in handlers.BUILTIN_HANDLERS:
        return
    handlers.BUILTIN_HANDLERS.append(_HANDLER_SPEC)
    if live:
        for emitter in _live_emitters():
            emitter.register(_EVENT, _before_aws_attempt, unique_id=_UNIQUE_ID)


def _live_emitters() -> list:
    """The event emitters of every botocore session and client that exists
    now: the default boto3 session's, any other session's, and every client's
    (made by whatever ran before this package was imported)."""
    session_cls = getattr(sys.modules.get("botocore.session"), "Session", None)
    client_cls = getattr(sys.modules.get("botocore.client"), "BaseClient", None)
    emitters = []
    for obj in gc.get_objects():
        try:
            if session_cls is not None and isinstance(obj, session_cls):
                emitters.append(obj.get_component("event_emitter"))
            elif client_cls is not None and isinstance(obj, client_cls):
                emitters.append(obj.meta.events)
        except Exception:  # noqa: BLE001 - an object that cannot be inspected is not one
            continue
    return emitters


class _AfterBotocoreHandlers(importlib.abc.MetaPathFinder):
    """One-shot import hook: finds ``botocore.handlers`` as usual and wraps
    its loader so the handler is registered right after the module's body
    has run (:func:`_register`), then leaves ``sys.meta_path``."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "botocore.handlers":
            return None
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        spec = importlib.util.find_spec(fullname)
        if spec is not None and spec.loader is not None:
            spec.loader = _RegisterAfterLoad(spec.loader)
        return spec


class _RegisterAfterLoad(importlib.abc.Loader):
    """A module's own loader, then :func:`_register` on the loaded module."""

    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        _register(module)

    def __getattr__(self, name):
        return getattr(self._loader, name)
