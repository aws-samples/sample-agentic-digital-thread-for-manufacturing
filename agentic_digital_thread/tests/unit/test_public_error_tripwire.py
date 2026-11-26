# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tripwire: no new raw exception text in a response, SSE frame or job record.

A route that answers ``{"error": str(exc)}`` hands the caller whatever the
driver or SDK put in the message: graph queries, hostnames, ARNs, resource
ids. The fix is ``agents.public_errors.public_error``: a fixed category
message plus a reference, the detail logged under that reference.

This AST scan finds every place in ``agents/`` where an exception's text
(``str(exc)``, ``repr(exc)``, ``f"{exc}"``, ``"%s" % exc``, ``"{}".format(exc)``,
``exc.args``, or a local assigned from one of those) flows into a return
value, a dict, a list append, a queue put, ``json.dumps``, ``HTTPException``
or ``JSONResponse``. Logging calls and ``raise`` are not sinks (the outer
handler that answers the caller is scanned instead), and neither is a handler
that catches only the typed errors in ``SAFE_CATCH_NAMES``, whose messages
are fixed text.

``_ALLOWED`` lists the remaining sites by (file, function) with their count
and the reason each never reaches a caller as raw backend text. It only
SHRINKS: a new site fails the scan, and a fixed one fails until its entry is
lowered or removed.

Offline: source parsing only.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_AGENTS = Path(__file__).resolve().parents[2] / "agents"

#: Typed errors whose messages are fixed text (agents.public_errors
#: SAFE_MESSAGE_TYPES, by class name). A handler catching only these is safe.
SAFE_CATCH_NAMES = frozenset({
    "ResetIncompleteError", "ConnectionTargetError", "ConnectionSecretUnavailableError",
    "AthenaConfigError", "DuckDBConfigError", "DuckDBAuthorizationError",
    "UnknownSystemError", "RolePassError",
    "RefineUnavailable", "MetricDefinitionError", "PreviewParameterError",
    "MetricResolverError", "CoverageGapError", "UnresolvedInputError",
    "OwnSecretDeletionError", "TokenResolutionError", "MemoryModelError",
})

_EXC_PARAM_NAMES = frozenset({"e", "ex", "exc", "err", "error", "exception"})
_LOG_OWNERS = frozenset({"logger", "logging", "log", "_log", "LOG", "_logger", "self"})
_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception",
                          "critical", "log"})
_SINK_CALLS = frozenset({"HTTPException", "JSONResponse", "dumps", "append", "put",
                         "put_nowait", "setdefault", "extend"})

#: The helper itself (it reads the message to log it) is not scanned.
_EXCLUDED = frozenset({"public_errors.py"})

_VALIDATION = ("a 4xx answer to the caller's own input: the handler catches a "
               "ValueError raised by this package's validator, whose text describes "
               "that input")
_METRIC_BOUNDARY = ("the metric layer's boundary reasons (MetricResolverError, "
                    "NotImplementedError, KeyError), surfaced verbatim by design, the "
                    "same contract as metric_turn_notice._classify_error")
_MODEL_TOOL = ("a tool result read by the model inside the AgentCore runtime, not an "
               "app response")

#: (file relative to agents/, function) -> (count, why it is not a public leak).
_ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("dashboard.py", "unavailable_card_entry"): (1, _METRIC_BOUNDARY),
    ("db.py", "_graph_error_detail"): (
        1, "the existing graph-error masking helper (ARNs and account ids masked, bounded)"),
    ("demo/story_clock.py", "preview"): (1, _VALIDATION),
    ("demo/story_lookup.py", "_live_maintenance"): (
        1, "_Missing: this module's own 'source holds nothing usable' text; any other "
           "exception is reduced to its class name"),
    ("demo/story_lookup.py", "_section"): (1, "_Missing, as above"),
    ("demo/story_lookup.py", "_timeline_or_none"): (1, "_Missing, as above"),
    ("eval_routes.py", "_invalid_golden_refusal"): (
        2, "bounded schema messages about the steward's own uploaded golden set"),
    ("graph_snapshot_routes.py", "get_snapshot_chunk"): (1, _VALIDATION),
    ("pgdriver.py", "_translate"): (
        1, "the database driver turning the server's ErrorResponse fields into its own "
           "exception (what a third-party driver did before); no response is built here, "
           "and callers that answer a caller still apply public_error"),
    ("graph_snapshot_routes.py", "start_snapshot_import"): (1, _VALIDATION),
    ("metric_definition_store.py", "_reason"): (
        1, "the parse error of a stored metric document's own JSON"),
    ("metric_preview.py", "_validation_error"): (1, _VALIDATION),
    ("metric_preview.py", "dry_run"): (1, _VALIDATION),
    ("metric_resolver.py", "_grouped_sites_envelope"): (2, _METRIC_BOUNDARY),
    ("metric_resolver.py", "_resolve_by_dimension_payload"): (1, _METRIC_BOUNDARY),
    ("metric_resolver.py", "_resolve_heatmap_payload"): (1, _METRIC_BOUNDARY),
    ("metric_resolver.py", "_resolve_metric_payload"): (2, _METRIC_BOUNDARY),
    ("metric_resolver.py", "_withheld"): (1, _METRIC_BOUNDARY),
    ("metric_turn_notice.py", "_classify_error"): (1, _METRIC_BOUNDARY),
    ("onboard.py", "_try_target_delete_once"): (
        1, "kept for the server log line of a failed gateway-target delete only"),
    ("onboard_agent.py", "_prepare_user_pg"): (
        1, "parse_pg_registration_dsn's fixed, credential-free reasons"),
    ("onboard_chat_lifecycle.py", "discover_for_chat"): (1, _VALIDATION),
    ("onboard_chat_lifecycle.py", "provision_for_chat"): (1, _VALIDATION),
    ("orchestrator.py", "_capture_document"): (1, _MODEL_TOOL),
    ("runtime_app.py", "_redacted_error_head"): (
        1, "the existing digit-masking helper for log lines"),
    ("self_learning.py", "_accept_schema_drift_materialise"): (1, _VALIDATION),
    ("self_learning.py", "_structural_metric_preview"): (1, _VALIDATION),
    ("server.py", "_accept_proposed_mapping_sync"): (1, _VALIDATION),
    ("server.py", "_repair_schema_sync"): (1, _VALIDATION),
    ("server.py", "_restore_failure_reason"): (
        1, "a log-line reason; only GatewayTargetUpdateError text, never a response"),
    ("server.py", "requeue_proposed_mapping"): (1, _VALIDATION),
    ("server.py", "user_define_mapping"): (1, _VALIDATION),
    ("sitewise_read.py", "batch_get_property_values"): (1, _MODEL_TOOL),
    ("sitewise_read.py", "describe_asset_error_message"): (3, _MODEL_TOOL),
    ("sitewise_read.py", "get_property_value"): (1, _MODEL_TOOL),
    ("system_agent.py", "_mcp_reopen_failure"): (1, _MODEL_TOOL),
    ("vocabulary_routes.py", "create_concept"): (1, _VALIDATION),
    ("vocabulary_routes.py", "edit_concept"): (1, _VALIDATION),
    ("vocabulary_routes.py", "propose_metric_definition"): (1, _VALIDATION),
    ("vocabulary_routes.py", "retire_concept"): (1, _VALIDATION),
}


#: The body of an HTTP error: ``exc.response.text`` / ``.content`` (httpx,
#: requests) or ``exc.text`` / ``exc.content`` (clients that put the body on
#: the error itself), and the parsed body, ``.json()`` of either. A body is
#: not the caller's business: it can carry validation input, internal ids,
#: hosts or service detail. The response is followed through a local alias
#: (``response = exc.response``, ``getattr(exc, "response", None)``, and an
#: alias of an alias); its status code and headers stay clear.
_BODY_ATTRS = frozenset({"text", "content"})
_PARSED_BODY_METHODS = frozenset({"json"})


def _is_response_of(node: ast.AST, names: set[str], responses: "set[str] | frozenset") -> bool:
    """``node`` is the caught error's response: ``<exc>.response``,
    ``getattr(<exc>, "response", ...)``, or a local bound to one."""
    if isinstance(node, ast.Name):
        return node.id in responses
    if isinstance(node, ast.Attribute) and node.attr == "response":
        return isinstance(node.value, ast.Name) and node.value.id in names
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "getattr" and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name) and node.args[0].id in names
            and isinstance(node.args[1], ast.Constant) and node.args[1].value == "response")


def _carries_body(owner: ast.AST, names: set[str], responses) -> bool:
    return (isinstance(owner, ast.Name) and owner.id in names) \
        or _is_response_of(owner, names, responses)


def _is_http_error_body(node: ast.AST, names: set[str], responses=frozenset()) -> bool:
    if isinstance(node, ast.Attribute) and node.attr in _BODY_ATTRS:
        return _carries_body(node.value, names, responses)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in _PARSED_BODY_METHODS:
        return _carries_body(node.func.value, names, responses)
    return False


def _response_aliases(scope: ast.AST, names: set[str]) -> set[str]:
    """Locals bound to the caught error's response, followed to a fixpoint
    (an alias of an alias counts). Only a plain ``name = <response>`` binds."""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for stmt in ast.walk(scope):
            if isinstance(stmt, ast.Assign) and _is_response_of(stmt.value, names, aliases):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id not in aliases:
                        aliases.add(t.id)
                        changed = True
    return aliases


def _is_exc_text(node: ast.AST, names: set[str], responses=frozenset()) -> bool:
    if _is_http_error_body(node, names, responses):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in ("str", "repr") and node.args \
            and isinstance(node.args[0], ast.Name) and node.args[0].id in names:
        return True
    if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name) \
            and node.value.id in names:
        return True
    if isinstance(node, ast.Attribute) and node.attr == "args" \
            and isinstance(node.value, ast.Name) and node.value.id in names:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "format" \
            and any(isinstance(a, ast.Name) and a.id in names for a in node.args):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        right = node.right
        elts = right.elts if isinstance(right, ast.Tuple) else [right]
        return any(isinstance(e, ast.Name) and e.id in names for e in elts)
    return False


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_log_call(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id in ("print", "_log")
    if isinstance(f, ast.Attribute) and f.attr in _LOG_METHODS:
        owner = f.value
        while isinstance(owner, ast.Attribute):
            owner = owner.value
        return isinstance(owner, ast.Name) and (
            owner.id in _LOG_OWNERS or owner.id.endswith("log") or owner.id.endswith("logger"))
    return False


def _sink(node: ast.AST, parents: dict, scope: ast.AST) -> bool:
    """True when ``node`` flows into a caller-visible value inside ``scope``."""
    cur = node
    while cur is not scope and cur in parents:
        up = parents[cur]
        if isinstance(up, ast.Call):
            if _is_log_call(up):
                return False
            if _call_name(up) in _SINK_CALLS:
                return True
        if isinstance(up, (ast.Raise, ast.Compare, ast.Assert)):
            return False
        if isinstance(up, (ast.Dict, ast.Return)):
            return True
        if isinstance(up, ast.Assign):
            return any(isinstance(t, ast.Subscript) for t in up.targets)
        cur = up
    return False


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    out = set()
    for e in elts:
        if isinstance(e, ast.Name):
            out.add(e.id)
        elif isinstance(e, ast.Attribute):
            out.add(e.attr)
        else:
            out.add("?")
    return out


def _tainted(scope: ast.AST, names: set[str], responses=frozenset()) -> set[str]:
    out = set(names)
    for stmt in ast.walk(scope):
        if isinstance(stmt, ast.Assign) and any(
                _is_exc_text(n, names, responses) for n in ast.walk(stmt.value)):
            out.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
    return out


def _scope_hits(scope: ast.AST, base: set[str], parents: dict) -> int:
    responses = _response_aliases(scope, base)
    names = _tainted(scope, base, responses)
    derived = names - base
    hits = 0
    for node in ast.walk(scope):
        if _is_exc_text(node, names, responses) and _sink(node, parents, scope):
            hits += 1
        elif isinstance(node, ast.Name) and node.id in derived \
                and isinstance(node.ctx, ast.Load) and _sink(node, parents, scope):
            hits += 1
    return hits


def scan_source(source: str) -> Counter:
    """``Counter[function name]`` of raw-exception-text sinks in ``source``."""
    tree = ast.parse(source)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    owner: dict[ast.AST, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                owner[n] = fn.name  # inner functions overwrite: innermost wins
    found: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name:
            caught = _caught_names(node)
            if caught and caught <= SAFE_CATCH_NAMES:
                continue
            hits = _scope_hits(node, {node.name}, parents)
            if hits:
                found[owner.get(node, "<module>")] += hits
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = {a.arg for a in node.args.args + node.args.kwonlyargs}
            exc_params = params & _EXC_PARAM_NAMES
            if exc_params:
                hits = _scope_hits(node, exc_params, parents)
                if hits:
                    found[node.name] += hits
    return found


def _scan_tree() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(_AGENTS.rglob("*.py")):
        rel = path.relative_to(_AGENTS).as_posix()
        if rel in _EXCLUDED:
            continue
        for fn, count in scan_source(path.read_text(encoding="utf-8")).items():
            found[(rel, fn)] = found.get((rel, fn), 0) + count
    return found


def test_no_new_raw_exception_text_reaches_a_caller():
    found = _scan_tree()
    new = {k: v for k, v in found.items()
           if k not in _ALLOWED or v > _ALLOWED[k][0]}
    assert not new, (
        "raw exception text flows into a response, SSE frame or job record at "
        f"{sorted(new.items())}: answer with agents.public_errors.public_error "
        "(fixed category + reference, detail logged) instead")


def test_the_allowlist_only_shrinks():
    found = _scan_tree()
    stale = {k: (v[0], found.get(k, 0)) for k, v in _ALLOWED.items()
             if found.get(k, 0) < v[0]}
    assert not stale, (
        f"allowlisted sites were fixed or removed: {sorted(stale.items())} "
        "(allowed, found); lower or delete their _ALLOWED entries")


def test_safe_catch_names_match_the_helper_allowlist():
    from agents.public_errors import SAFE_MESSAGE_TYPES
    assert {k.rsplit(".", 1)[1] for k in SAFE_MESSAGE_TYPES} == SAFE_CATCH_NAMES


def test_the_scan_flags_the_leak_shapes_and_ignores_logging():
    src = '''
def leaks(q, jobs):
    try:
        pass
    except Exception as exc:
        jobs["j"] = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
        q.put({"event": "error", "data": {"text": str(exc)}})
        msg = str(exc)
        return {"error": msg}

def quiet():
    try:
        pass
    except Exception as exc:
        logger.warning("failed: %s", exc)
        logger.error(f"failed: {exc}")
        if "throttl" in str(exc).lower():
            return {"error": "throttled"}
        raise RuntimeError(f"wrapped: {exc}") from exc

def typed():
    try:
        pass
    except ResetIncompleteError as exc:
        return {"error": str(exc)}
'''
    found = scan_source(src)
    assert found == {"leaks": 3}, found


def test_the_scan_flags_an_http_error_body_and_ignores_logging_it():
    src = '''
def body_leaks():
    try:
        pass
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response is not None else ""
        return json.dumps({"error": f"failed HTTP {exc.response.status_code}: {detail}"})

def raw_bytes():
    try:
        pass
    except requests.HTTPError as err:
        return {"error": err.response.content}

def on_the_error():
    try:
        pass
    except ClientError as e:
        return {"error": e.text}

def quiet_body():
    try:
        pass
    except httpx.HTTPStatusError as exc:
        logger.warning("upstream said %s", exc.response.text)
        return {"error": "failed", "status": exc.response.status_code}
'''
    found = scan_source(src)
    # A derived local inside an f-string is tallied twice (the formatted value
    # and the name), the scanner's existing count for that shape.
    assert found == {"body_leaks": 2, "raw_bytes": 1, "on_the_error": 1}, found


def test_the_scan_follows_an_alias_of_the_response_and_a_parsed_body():
    """Negative controls: each of these hands the upstream body to the caller
    and must be flagged. The alias and the parsed body are the two shapes a
    routine refactor of a direct ``exc.response.text`` produces."""
    src = '''
def alias():
    try:
        pass
    except httpx.HTTPStatusError as exc:
        response = exc.response
        return {"error": response.text}

def parsed():
    try:
        pass
    except httpx.HTTPStatusError as exc:
        return {"error": exc.response.json()["detail"]}

def alias_of_alias_parsed():
    try:
        pass
    except requests.HTTPError as err:
        resp = getattr(err, "response", None)
        r = resp
        payload = r.json()
        return {"error": payload.get("detail")}

def alias_bytes():
    try:
        pass
    except httpx.HTTPStatusError as exc:
        response = exc.response
        return {"error": response.content}
'''
    found = scan_source(src)
    assert found == {"alias": 1, "parsed": 1, "alias_of_alias_parsed": 1,
                     "alias_bytes": 1}, found


def test_the_scan_leaves_an_aliased_response_status_and_a_logged_body_alone():
    src = '''
def quiet_alias():
    try:
        pass
    except httpx.HTTPStatusError as exc:
        response = exc.response
        logger.warning("upstream said %s", response.text)
        logger.warning("parsed %s", response.json())
        return {"error": "failed", "status": response.status_code}

def other_response():
    try:
        pass
    except httpx.HTTPStatusError as exc:
        response = client.get(url)
        return {"body": response.text, "status": exc.response.status_code}
'''
    assert scan_source(src) == {}, scan_source(src)
