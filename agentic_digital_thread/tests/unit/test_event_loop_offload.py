# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Event-loop offload gate (the 60s-stall / health-check-death-loop fix).

Sync boto3/DB resolver work used to run INLINE in ``async def`` handlers on the
single-worker uvicorn — one slow SiteWise sweep froze the event loop, /health
timed out, and ECS replaced the only task. The fix is the EVENT-LOOP OFFLOAD
PATTERN (``metric_resolver.offload_resolver``): async handlers do nothing but
``await offload_resolver(<sync payload fn>, ...)``.

This module pins that in two ways (source-tripwire idiom mirrors
``test_intelligence_summary_single_source.py``):

  1. AST tripwire — every resolver-backed async route body contains NO direct
     call other than ``offload_resolver`` (so blocking work cannot silently
     move back inline);
  2. behavioral — the event loop stays responsive while a slow stubbed resolver
     runs, and the shared semaphore bounds offload concurrency to 4.

Offline: no AWS, no network.
"""
from __future__ import annotations

import ast
import asyncio
import threading
import time
from pathlib import Path

import pytest

import agents.metric_resolver as mr

_AGENTS = Path(__file__).resolve().parents[2] / "agents"

# module file -> the resolver-backed async route handlers that MUST be offloaded
_OFFLOADED_ROUTES = {
    "dashboard.py": [
        "dashboard_data",
        "dashboard_sections",
        "dashboard_active_issues",
        "dashboard_line_state",
    ],
    "dashboard_drill.py": ["dashboard_line_state_machines"],
    "metric_resolver.py": [
        "resolve_metric_route",
        "resolve_metric_by_dimension_route",
        "resolve_heatmap_route",
        # Repoints a concept's binding and ripples the affected cards: the
        # metric family's route, so the metric family's lane.
        "correct_mapping_route",
    ],
    # ADR-0080: the metric-definition preview may run a dry-run RESOLVE, so it
    # rides the resolver lane like every other resolver-backed route.
    "vocabulary_routes.py": ["preview_metric_definition_route"],
}


def _async_defs(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)}


def _call_names(fn: ast.AsyncFunctionDef) -> list[str]:
    """Names of every direct call inside the handler BODY (decorators like
    ``@router.get(...)`` excluded; lambdas included — the pattern forbids them
    in routes precisely so this stays exhaustive)."""
    names = []
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    names.append(f.id)
                elif isinstance(f, ast.Attribute):
                    names.append(f.attr)
                else:
                    names.append(ast.dump(f))
    return names


def test_resolver_routes_only_call_offload_resolver():
    """TRIPWIRE: the async handlers contain no call except offload_resolver —
    any direct resolver/DB/boto3 invocation re-added to an async body (even
    via a lambda) trips this."""
    for fname, routes in _OFFLOADED_ROUTES.items():
        defs = _async_defs(_AGENTS / fname)
        for route in routes:
            assert route in defs, f"{fname}: async route {route} not found"
            calls = _call_names(defs[route])
            assert calls, f"{fname}:{route} has no calls — expected offload_resolver"
            offenders = [c for c in calls if c != "offload_resolver"]
            assert not offenders, (
                f"{fname}:{route} calls {offenders} directly in the async path — "
                "move the sync work into the module-level payload fn and go "
                "through offload_resolver (EVENT-LOOP OFFLOAD PATTERN)"
            )


def test_health_route_stays_trivially_non_blocking():
    """/health must never route through the resolver seam — it is the ALB
    health-check and has to answer even when every offload slot is busy."""
    defs = _async_defs(_AGENTS / "server.py")
    assert "health" in defs
    assert _call_names(defs["health"]) == [], \
        "/health grew a call — keep it a trivial dict return"


# --- Behavioral: the loop stays responsive while a slow resolver runs --------

def test_event_loop_responsive_while_slow_resolver_offloaded():
    """A concurrent trivial await completes (many times) WHILE a slow blocking
    resolver runs through offload_resolver — proof the loop is not frozen."""
    ticks = {"n": 0}

    def slow_resolver():
        time.sleep(0.25)               # blocking sync work (the old freeze)
        return "done"

    async def ticker(stop: asyncio.Event):
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def main():
        stop = asyncio.Event()
        t = asyncio.create_task(ticker(stop))
        result = await mr.offload_resolver(slow_resolver)
        stop.set()
        await t
        return result

    assert asyncio.run(main()) == "done"
    # Inline (blocking) execution would have yielded ~0 ticks; offloaded
    # execution lets the loop keep scheduling. Generous floor for slow CI.
    assert ticks["n"] >= 5


def test_offload_concurrency_bounded_by_semaphore():
    """8 concurrent offloads never run more than 4 resolver threads at once —
    the convoy guard (_OFFLOAD_MAX_CONCURRENCY)."""
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def resolver():
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return True

    async def main():
        return await asyncio.gather(*[mr.offload_resolver(resolver) for _ in range(8)])

    results = asyncio.run(main())
    assert results == [True] * 8
    assert state["peak"] <= mr._OFFLOAD_MAX_CONCURRENCY
    assert state["peak"] >= 2          # it did actually run concurrently


def test_offload_semaphore_is_per_event_loop():
    """Repeated asyncio.run() loops (each test/app lifecycle gets its own loop)
    must never trip the 'bound to a different event loop' guard."""
    async def one():
        return await mr.offload_resolver(lambda: 42)

    assert asyncio.run(one()) == 42
    assert asyncio.run(one()) == 42    # second, different loop — still fine


def test_offload_propagates_args_and_exceptions():
    async def main_ok():
        return await mr.offload_resolver(lambda a, b=0: a + b, 40, b=2)

    assert asyncio.run(main_ok()) == 42

    async def main_err():
        def boom():
            raise ValueError("resolver failed")
        await mr.offload_resolver(boom)

    with pytest.raises(ValueError, match="resolver failed"):
        asyncio.run(main_err())


# --- Repo-wide tripwire: NO async route handler runs known sync I/O inline ---
#
# The allowlisted-8-routes tripwire above pins the resolver family. This one
# generalizes it: every ``@router.<verb>`` / ``@app.<verb>`` ``async def`` in
# ``agents/`` is scanned; a known sync I/O call (Neptune, Postgres, Bedrock,
# boto3 client verbs) that runs on the loop thread fails it unless the handler
# is listed in ``_INLINE_SYNC_ALLOWLIST`` with a reason. Only the work handed
# to an offload seam (a function reference, or a lambda) is cleared: another
# blocking call beside the ``await offload(...)``, or one evaluated as an
# offload argument, still runs on the loop and is flagged.
# boto3 counts under any import name, and the scan follows same-module
# module-level sync helpers and ``def``s nested in the handler, from the call
# sites that run them (``_HELPER_DEPTH`` deep), so routing the I/O through
# ``import boto3 as _b3`` or a ``_session()`` helper is still inline.
# Cross-module calls and async helpers are not followed.
# The allowlist only ever SHRINKS: adding a name here is a deliberate edit.

from agents import offload as offload_mod  # noqa: E402

_ROUTE_DECORATORS = {"get", "post", "put", "delete", "patch", "api_route", "websocket"}
_OFFLOAD_SEAMS = {"offload", "offload_holding", "offload_resolver", "to_thread",
                  "run_in_executor", "run_in_threadpool"}
_KNOWN_SYNC_CALLS = {
    # graph / postgres / bedrock helpers
    "neptune_query", "neptune_write", "_nq", "_nw", "query_pg", "query_pg_bounded",
    "pg", "bedrock_call", "connect", "fetchall", "fetchone", "invoke_model", "converse",
    # boto3 client verbs seen on route paths
    "get_item", "put_item", "put_object", "get_object", "start_query_execution",
    "get_query_results", "describe_asset", "batch_get_asset_property_value",
    "list_agent_runtimes", "get_agent_runtime", "update_agent_runtime",
    "get_parameter", "put_parameter", "get_secret_value", "invoke",
    "describe_stacks", "get_caller_identity",
    # AgentCore gateway/credential control-plane verbs and their paginating
    # helpers (agents/runtime_provision_helpers.py) — every page is one more
    # blocking call, so a route that lists inline stalls the loop longer.
    "list_gateway_targets", "list_all_gateway_targets", "synchronize_gateway_targets",
    "list_oauth2_credential_providers", "list_all_oauth2_credential_providers",
    # paginated DynamoDB proposal scans (7 MB live, 2026-09-26)
    "get_all_mappings", "get_pending_mappings", "scan",
    # ADR-0078: per-turn metric-layer resolution (source SQL) — never inline.
    # (``stream`` is deliberately NOT listed: Starlette's ``request.stream()``
    # is async; the ask_sync cloud stream is pinned in test_ask_sync_offload.)
    "resolve_metric", "results_for_turn",
    # Graph-backed business-context reads (agents/business_context.py): each is
    # a Neptune query, cross-module so the helper walk cannot see into it.
    "read_business_context",
}
# "module.py:handler" -> reason. Empty by design; shrink-only.
_INLINE_SYNC_ALLOWLIST: dict[str, str] = {}

# How many same-module sync helpers deep the scan follows from a handler
# (handler -> helper -> helper -> helper). Recursion-guarded per call path.
_HELPER_DEPTH = 3


class _Module:
    """What the scan needs from one ``agents/`` module: its module-level sync
    helpers and the names bound to boto3 at module level."""

    def __init__(self, tree: ast.Module):
        self.helpers = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        top = [s for s in tree.body
               if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        self.boto3_modules, self.boto3_callables = _boto3_bindings(top)


def _boto3_bindings(stmts) -> tuple[set[str], set[str]]:
    """Names bound to boto3 by the imports in *stmts*: module names
    (``import boto3`` / ``import boto3 as _b3``) and callables taken from the
    package root (``from boto3 import client``). ``from boto3.dynamodb...``
    imports condition builders, not I/O, so only the root counts."""
    modules: set[str] = set()
    callables: set[str] = set()
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "boto3" or a.name.startswith("boto3."):
                        modules.add(a.asname or "boto3")
            elif isinstance(node, ast.ImportFrom) and node.module == "boto3":
                callables.update(a.asname or a.name for a in node.names)
    return modules, callables


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


_NESTED_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _on_this_thread(fn):
    """Every call, and every name read, in *fn*'s body that runs on the
    calling thread. A function handed to an offload seam (a bare name or a
    lambda argument) runs in a worker, so it is skipped; every other seam
    argument (``offload(fn, slow())``) is evaluated here first and is scanned
    like any other expression. A nested ``def`` runs only when something on
    this thread uses it, so its body is skipped here and followed from each
    reference to it (a call, ``map(_fetch, ids)``, ``key=_slow``, ``f = _fetch``)."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED_DEFS):
            continue
        if isinstance(node, (ast.Call, ast.Name)):
            yield node
        if isinstance(node, ast.Call) and _call_name(node) in _OFFLOAD_SEAMS:
            stack.append(node.func)
            stack.extend(a for a in (*node.args, *(k.value for k in node.keywords))
                         if not isinstance(a, (ast.Lambda, ast.Name)))
            continue
        stack.extend(ast.iter_child_nodes(node))


def _sync_io_hits(fn, mod: _Module, depth: int = 0,
                  path: frozenset = frozenset(),
                  outer: tuple = (frozenset(), frozenset())) -> set[str]:
    """Known sync I/O reached from *fn*'s body: a ``_KNOWN_SYNC_CALLS`` name,
    any call through a boto3 binding (module-level or function-local alias),
    or either of those inside a same-module module-level sync helper the body
    calls, up to ``_HELPER_DEPTH`` helpers deep. A helper that returns a boto3
    session/client counts, since it calls boto3 itself. A ``def`` nested in
    the body is followed the same way from any reference to it that is not
    an offload argument, seeing the enclosing function's boto3 imports
    (*outer*). Awaiting a seam clears only the work handed to it, never
    another call in the same body."""
    local_modules, local_callables = _boto3_bindings(fn.body)
    modules = mod.boto3_modules | local_modules | outer[0]
    callables = mod.boto3_callables | local_callables | outer[1]
    nested = {n.name: n for stmt in fn.body for n in ast.walk(stmt)
              if isinstance(n, _NESTED_DEFS)}
    hits: set[str] = set()

    def follow(name: str, helper, scope) -> None:
        if name not in path and depth < _HELPER_DEPTH:
            hits.update(f"{name}>{h}" for h in sorted(
                _sync_io_hits(helper, mod, depth + 1, path | {name}, scope)))

    for node in _on_this_thread(fn):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id in nested:
                follow(node.id, nested[node.id], (frozenset(modules), frozenset(callables)))
            continue
        f = node.func
        if isinstance(f, ast.Name):
            name = f.id
            if name in callables:
                hits.add(f"boto3.{name}")
            if name not in nested and name in mod.helpers:
                follow(name, mod.helpers[name], (frozenset(), frozenset()))
        elif isinstance(f, ast.Attribute):
            name = f.attr
            if isinstance(f.value, ast.Name) and f.value.id in modules:
                hits.add("boto3.*" if f.value.id == "boto3"
                         else f"boto3.* (as {f.value.id})")
        else:
            continue
        if name in _KNOWN_SYNC_CALLS:
            hits.add(name)
    return hits


def _route_handlers_in(tree: ast.Module):
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
               and d.func.attr in _ROUTE_DECORATORS
               for d in node.decorator_list):
            yield node


def _async_route_handlers():
    for path in sorted(_AGENTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mod = _Module(tree)
        for node in _route_handlers_in(tree):
            yield path.relative_to(_AGENTS).as_posix(), node, mod


def _inline_hits(fn, mod: _Module) -> list[str]:
    """Sync I/O the handler runs on the loop (work handed to a seam excluded)."""
    return sorted(_sync_io_hits(fn, mod))


def test_no_async_route_runs_known_sync_io_inline():
    offenders = []
    for rel, fn, mod in _async_route_handlers():
        hits = _inline_hits(fn, mod)
        if hits and f"{rel}:{fn.name}" not in _INLINE_SYNC_ALLOWLIST:
            offenders.append(f"{rel}:{fn.name} -> {hits}")
    assert offenders == [], (
        "async route handlers call sync I/O inline on the single uvicorn "
        "event loop — hoist the body into a module-level sync fn and "
        "`return await offload(fn, ...)` (agents/offload.py):\n  "
        + "\n  ".join(offenders)
    )


def test_inline_sync_allowlist_only_shrinks():
    """Every allowlisted entry must still exist AND still be an offender —
    a stale entry means the fix landed and the entry must be removed."""
    live = {f"{rel}:{fn.name}": (fn, mod) for rel, fn, mod in _async_route_handlers()}
    for key in _INLINE_SYNC_ALLOWLIST:
        assert key in live, f"allowlisted handler no longer exists: {key}"
        fn, mod = live[key]
        assert _inline_hits(fn, mod), f"{key} is clean now — drop it"


def _scan_source(src: str) -> dict[str, list[str]]:
    tree = ast.parse(src)
    mod = _Module(tree)
    return {fn.name: _inline_hits(fn, mod) for fn in _route_handlers_in(tree)}


def test_tripwire_sees_boto3_aliases():
    """A boto3 imported under another name is still boto3: function-local and
    module-level aliases, and callables taken from the package root."""
    found = _scan_source(
        "import boto3 as top\n"
        "from boto3 import client as mk\n"
        "@router.get('/a')\n"
        "async def local_alias():\n"
        "    import boto3 as _b3\n"
        "    return _b3.client('ssm').describe_parameters()\n"
        "@router.get('/b')\n"
        "async def module_alias():\n"
        "    return top.client('ssm')\n"
        "@router.get('/c')\n"
        "async def root_callable():\n"
        "    return mk('ssm')\n")
    assert found == {"local_alias": ["boto3.* (as _b3)"],
                     "module_alias": ["boto3.* (as top)"],
                     "root_callable": ["boto3.mk"]}


def test_tripwire_follows_same_module_sync_helpers():
    """Sync I/O one or more helpers down is still inline, including a helper
    that only builds the boto3 session the handler then uses; recursion and
    the depth bound terminate; offloading the helper clears the handler."""
    found = _scan_source(
        "import boto3\n"
        "def _session():\n"
        "    return boto3.Session()\n"
        "def _two():\n"
        "    return _session()\n"
        "def _loop_a():\n"
        "    return _loop_b()\n"
        "def _loop_b():\n"
        "    return _loop_a()\n"
        "def _deep1():\n"
        "    return _deep2()\n"
        "def _deep2():\n"
        "    return _deep3()\n"
        "def _deep3():\n"
        "    return _deep4()\n"
        "def _deep4():\n"
        "    return neptune_query('x')\n"
        "def _pure():\n"
        "    from boto3.dynamodb.conditions import Key\n"
        "    return Key('pk').eq('x')\n"
        "@router.get('/session')\n"
        "async def via_session():\n"
        "    s = _session()\n"
        "    return s.client('logs').describe_log_groups()\n"
        "@router.get('/two')\n"
        "async def via_two():\n"
        "    return _two()\n"
        "@router.get('/cycle')\n"
        "async def via_cycle():\n"
        "    return _loop_a()\n"
        "@router.get('/deep')\n"
        "async def via_deep():\n"
        "    return _deep1()\n"
        "@router.get('/pure')\n"
        "async def via_pure():\n"
        "    return _pure()\n"
        "@router.get('/offloaded')\n"
        "async def offloaded():\n"
        "    return await offload(_session)\n")
    assert found == {"via_session": ["_session>boto3.*"],
                     "via_two": ["_two>_session>boto3.*"],
                     "via_cycle": [],
                     "via_deep": [],     # four helpers down: past the bound
                     "via_pure": [],
                     "offloaded": []}


def test_tripwire_flags_inline_io_beside_an_offload():
    """Awaiting a seam clears only the work handed to it: a second blocking
    call in the same handler, or a blocking call evaluated as an offload
    ARGUMENT, still runs on the loop. A lambda handed to the seam runs in the
    worker and is not flagged."""
    found = _scan_source(
        "import boto3\n"
        "def _work():\n"
        "    return 1\n"
        "@router.get('/mixed')\n"
        "async def mixed():\n"
        "    await offload(_work)\n"
        "    return boto3.client('ssm').get_parameter(Name='x')\n"
        "@router.get('/arg')\n"
        "async def argument():\n"
        "    return await offload(_work, neptune_query('x'))\n"
        "@router.get('/lambda')\n"
        "async def via_lambda():\n"
        "    return await offload(lambda: neptune_query('x'))\n"
        "@router.get('/body-then-offload')\n"
        "async def body_then_offload(request):\n"
        "    body = await request.json()\n"
        "    return await offload(_work, body)\n"
        "@router.get('/nested-offloaded')\n"
        "async def nested_offloaded():\n"
        "    import boto3 as _b3\n"
        "    def _invoke():\n"
        "        return _b3.client('lambda').invoke(FunctionName='f')\n"
        "    return await asyncio.to_thread(_invoke)\n"
        "@router.get('/nested-inline')\n"
        "async def nested_inline():\n"
        "    import boto3 as _b3\n"
        "    def _invoke():\n"
        "        return _b3.client('lambda').invoke(FunctionName='f')\n"
        "    return _invoke()\n")
    assert found == {"mixed": ["boto3.*", "get_parameter"],
                     "argument": ["neptune_query"],
                     "via_lambda": [],
                     "body_then_offload": [],
                     "nested_offloaded": [],
                     "nested_inline": ["_invoke>boto3.* (as _b3)", "_invoke>invoke"]}


def test_tripwire_follows_nested_defs_used_without_a_call():
    """A nested ``def`` handed to anything but an offload seam runs on the
    loop: ``map``, a ``key=`` function and a local alias all execute it here.
    Handed to a seam, as a positional or keyword argument, it does not."""
    found = _scan_source(
        "@router.get('/map')\n"
        "async def via_map(ids):\n"
        "    def _fetch(i):\n"
        "        return neptune_query(i)\n"
        "    return list(map(_fetch, ids))\n"
        "@router.get('/key')\n"
        "async def via_key(rows):\n"
        "    def _slow(row):\n"
        "        return pg('s', 'select 1')\n"
        "    return sorted(rows, key=_slow)\n"
        "@router.get('/alias')\n"
        "async def via_alias():\n"
        "    def _fetch():\n"
        "        return neptune_query('x')\n"
        "    f = _fetch\n"
        "    return f()\n"
        "@router.get('/seam-keyword')\n"
        "async def seam_keyword(loop):\n"
        "    def _fetch():\n"
        "        return neptune_query('x')\n"
        "    return await loop.run_in_executor(None, func=_fetch)\n")
    assert found == {"via_map": ["_fetch>neptune_query"],
                     "via_key": ["_slow>pg"],
                     "via_alias": ["_fetch>neptune_query"],
                     "seam_keyword": []}


def test_offload_home_is_agents_offload_and_resolver_reexports_it():
    """ONE implementation: metric_resolver re-exports agents/offload.py."""
    assert mr.offload_resolver is offload_mod.offload_resolver
    assert mr._OFFLOAD_MAX_CONCURRENCY == offload_mod.RESOLVER_MAX_CONCURRENCY == 4


def test_general_lane_is_separate_from_resolver_lane():
    """A saturated general lane must not block a resolver call (and vice
    versa) — the two semaphores are independent."""
    release = threading.Event()

    def hold():
        release.wait(2.0)
        return "held"

    async def main():
        holders = [asyncio.create_task(offload_mod.offload(hold))
                   for _ in range(offload_mod.GENERAL_MAX_CONCURRENCY)]
        await asyncio.sleep(0.05)  # all general slots taken
        resolver_done = await asyncio.wait_for(
            offload_mod.offload_resolver(lambda: "resolver"), timeout=1.0)
        release.set()
        held = await asyncio.gather(*holders)
        return resolver_done, held

    resolver_done, held = asyncio.run(main())
    assert resolver_done == "resolver"
    assert held == ["held"] * offload_mod.GENERAL_MAX_CONCURRENCY


def test_general_lane_bounded_by_its_semaphore():
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def work():
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.02)
        with lock:
            state["now"] -= 1
        return True

    async def main():
        n = offload_mod.GENERAL_MAX_CONCURRENCY * 2
        return await asyncio.gather(*[offload_mod.offload(work) for _ in range(n)])

    results = asyncio.run(main())
    assert all(results)
    assert 2 <= state["peak"] <= offload_mod.GENERAL_MAX_CONCURRENCY


# --- Behavioral: PUT /llm-settings no longer freezes the loop ----------------

def test_event_loop_responsive_while_llm_settings_save_runs(monkeypatch):
    """The settings save writes SSM, reads and updates the orchestrator runtime
    and refreshes every system agent one after another. Against a slow AWS
    client the loop must keep ticking for the whole save, and every AWS call
    must run on the general offload lane, not the loop thread."""
    import types
    from unittest.mock import patch

    from agents import admin_routes

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", "us.amazon.nova-lite-v1:0")
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    aws_threads: list[str] = []

    def slow(result=None):
        aws_threads.append(threading.current_thread().name)
        time.sleep(0.1)
        return result

    class SlowSSM:
        def __init__(self):
            self.store: dict[str, str] = {}

        def get_parameter(self, Name):  # noqa: N803 - boto3 API casing
            slow()
            raise Exception(f"ParameterNotFound: {Name}")

        def put_parameter(self, Name, Value, **_kw):  # noqa: N803
            self.store[Name] = Value
            return slow({})

    class SlowControl:
        def list_agent_runtimes(self, **_kw):
            return slow({"agentRuntimes": []})

    class SlowManager:
        mode = "cloud"

        def list_agents(self):
            return [{"system_id": "erp"}, {"system_id": "mes"}]

        def refresh_agent_model(self, system_id, model):
            return slow("ok")

    ssm = SlowSSM()
    clients = {"ssm": ssm, "bedrock-agentcore-control": SlowControl()}
    monkeypatch.setattr(admin_routes, "_get_agent_manager", lambda: SlowManager())
    request = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))

    async def _json():
        return {"orchestrator_model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "agent_model": "us.amazon.nova-pro-v1:0"}
    request.json = _json
    ticks = {"n": 0}

    async def ticker(stop: asyncio.Event):
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def main():
        stop = asyncio.Event()
        t = asyncio.create_task(ticker(stop))
        result = await admin_routes.update_llm_settings_route(request)
        stop.set()
        await t
        return result

    with patch("boto3.client", lambda service, **_kw: clients[service]):
        started = time.monotonic()
        result = asyncio.run(main())
        elapsed = time.monotonic() - started

    assert result["status"] == "ok", result
    assert result["agent_runtime_refresh"]["ok"] == ["erp", "mes"], result
    assert ssm.store["/adt/llm-agent-model"] == "us.amazon.nova-pro-v1:0"
    # SSM reads, two SSM writes, two runtime listings, two system refreshes.
    assert len(aws_threads) >= 7 and elapsed >= 0.6, (aws_threads, elapsed)
    assert all(name.startswith("adt-offload-general") for name in aws_threads), aws_threads
    # Inline, the loop would not have ticked during the save; offloaded it
    # keeps scheduling. Generous floor for slow CI.
    assert ticks["n"] >= 10, ticks
