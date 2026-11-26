# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared MCP ASGI serving + user-identity claim enforcement (ADR-0049, Wave I2).

ONE home for the ASGI assembly every FastMCP runtime mode uses (the /ping +
/invocations warmup routes, the /mcp vs /mcp/ trailing-slash alias the gateway
depends on, uvicorn on 0.0.0.0:8000) shared by ``runtime_app``'s system-agent /
digital-thread / onboarding runners — plus the
identity-claim middleware that assembly installs.

Identity contract (ADR-0049): the orchestrator attaches a short-lived
app-signed claim under the reserved tool-call argument
``auth.identity_claim.RESERVED_CLAIM_ARG`` on INTERACTIVE turns. This side:

  1. POPS the reserved argument from every MCP ``tools/call`` BEFORE query
     handling, ALWAYS — even with enforcement disabled — so external callers
     (Quick, any M2M MCP client) can never smuggle a value through, and tool
     signatures never see an unexpected argument.
  2. When the per-system operator env ``SYSTEM_REQUIRED_GROUP`` is set, the
     policy is LIVE: the claim is verified (via the key behind
     ``SYSTEM_IDENTITY_CLAIM_SECRET_ARN``) and group membership enforced —
     and any state that prevents verification (no key ARN configured, key
     unreadable, claim missing/invalid) fails CLOSED with an honest deny.
     Group unset ⇒ allow-all (engine-generic default; the group is a
     per-system operator choice, set NOWHERE by deploy tooling).

DENY-TEXT CONTRACT (pinned by tests/unit/test_runtime_identity_hook.py): a
denial is an HONEST, FINAL authorization decision. Its text must NEVER
substring-match the orchestrator's retryable-error marker tuples (the auth
staleness phrase or the transient cold-runtime markers in
``agents/orchestrator.py``) — otherwise the orchestrator would silently retry,
and thereby mask, a genuine denial. Claim contents are never logged (R4).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os

from .auth.identity_claim import (
    RESERVED_CLAIM_ARG as IDENTITY_CLAIM_ARG,
    ClaimVerificationError,
    verify_claim,
)
from .chart_intent import RESERVED_CHART_ARG, parse_chart_intent
from .provenance import (
    RESERVED_PROVENANCE_ARG,
    parse_provenance_context,
    parse_write_token,
)

logger = logging.getLogger(__name__)

#: Wave-5 (F1): the validated ``(session_id, query_ref)`` pair popped from the
#: reserved ``_adt_provenance`` tool-call arg — set valid-or-``None`` on EVERY
#: ``tools/call`` (per-request reset), read ONCE in the outer Mode-A handler
#: frame (``system_agent._run_mode_a_query``; the middleware awaits the
#: downstream app in the same async context). Malformed/absent ⇒ ``None`` —
#: a legacy caller: honest absence, never a deny, never a log of the value.
provenance_context_var: "contextvars.ContextVar[tuple[str, str] | None]" = (
    contextvars.ContextVar("adt_provenance_context", default=None)
)

#: Wave 7 (write capability): the validated per-call write token popped from
#: the reserved ``_adt_provenance`` arg's ``write_token`` field, set
#: valid-or-``None`` on EVERY ``tools/call`` (per-request reset, the exact
#: ``provenance_context_var`` posture). Read ONLY by the system-agent
#: sidecar/trace writers, which pass it to the store so the entry is keyed by
#: a capability only the app holds. Never logged, never reaches a tool
#: signature or any LLM token path. ``None`` ⇒ legacy/local caller: the store
#: falls back to the legacy key.
provenance_write_token_var: "contextvars.ContextVar[str | None]" = (
    contextvars.ContextVar("adt_provenance_write_token", default=None)
)

#: ADR-0079: the RAW ``_adt_identity`` string popped from the current
#: ``tools/call`` (or ``None``) — set on EVERY ``tools/call`` (per-request
#: reset), exactly the ``provenance_context_var`` posture. Read ONLY by the
#: query-bridge, which forwards it VERBATIM to the app (the app is the
#: verifier). Enforcement is unchanged; the value is never parsed or logged
#: here.
identity_claim_var: "contextvars.ContextVar[str | None]" = (
    contextvars.ContextVar("adt_identity_claim", default=None)
)

#: This turn's chart-intent bit, popped from the reserved ``_adt_chart_intent``
#: tool-call arg on EVERY ``tools/call`` (per-request reset, the exact
#: ``provenance_context_var`` posture). ``True``/``False`` from the orchestrator
#: wrapper's value, ``None`` when the arg is absent (a direct/background call).
#: Read ONLY by the SiteWise history path
#: (``system_agent._sitewise_runtime_wants_chart``); never a deny, never logged.
chart_intent_var: "contextvars.ContextVar[bool | None]" = (
    contextvars.ContextVar("adt_chart_intent", default=None)
)

#: Whether the current ``tools/call`` asked for the identifier-sources engine
#: line, popped from the reserved ``_adt_identifier_sources`` arg on EVERY
#: ``tools/call`` (the ``chart_intent_var`` posture): ``True`` for the exact
#: request value, ``None`` otherwise. Nothing here proves the caller is the
#: orchestrator, so the line it unlocks holds no value
#: (``identifier_provenance.with_line``). Read by ``system_agent``; never logged.
identifier_sources_var: "contextvars.ContextVar[bool | None]" = (
    contextvars.ContextVar("adt_identifier_sources", default=None)
)

#: The current ``tools/call``'s pairing request (``form_pairing.RESERVED_ARG``,
#: a JSON string), popped on EVERY ``tools/call`` (the ``chart_intent_var``
#: posture) and set only when the call has the engine's shape
#: (:func:`_admit_pair_forms`: this runtime's own query tool, the fixed
#: question, not in a batch); any other call carrying it is refused before a
#: tool runs. Read by ``system_agent``'s Mode A query tool, which then answers
#: from ``form_pairing`` with no model; the runtime decides every column from
#: its own graph and schema, never the caller. Never logged.
pair_forms_var: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("adt_pair_forms", default=None)
)

IDENTITY_CLAIM_SECRET_ARN_ENV = "SYSTEM_IDENTITY_CLAIM_SECRET_ARN"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
REQUIRED_GROUP_ENV = "SYSTEM_REQUIRED_GROUP"

# Deny texts. Wording is deliberate: "Access denied by identity policy" +
# "final for this request" — nothing here may drift toward the retryable
# marker phrases (see the DENY-TEXT CONTRACT above; pinned by unit test).
IDENTITY_DENY_MISSING = (
    "Access denied by identity policy for system '{system}': this system "
    "requires membership in group '{group}', and the request carried no "
    "verified user identity claim. This denial is final for this request; "
    "service and background flows cannot access this group-restricted system."
)
IDENTITY_DENY_INVALID = (
    "Access denied by identity policy for system '{system}': the user identity "
    "claim could not be verified (malformed, expired, or signed with a "
    "different key). This denial is final for this request."
)
IDENTITY_DENY_GROUP = (
    "Access denied by identity policy for system '{system}': the verified user "
    "is not a member of required group '{group}'. This denial is final for "
    "this request."
)
IDENTITY_DENY_KEY_UNREADABLE = (
    "Access denied by identity policy for system '{system}': the identity "
    "signing key could not be read by this runtime, so the user identity claim "
    "cannot be verified. This denial is final for this request."
)
IDENTITY_DENY_KEY_UNCONFIGURED = (
    "Access denied by identity policy for system '{system}': this system "
    "requires a verified user identity claim, but no identity signing key is "
    "configured on this runtime, so no claim can be verified. This denial is "
    "final for this request; redeploy so the runtime receives the signing-key "
    "ARN, or unset the group requirement."
)


def identity_deny_texts(system: str = "example-system", group: str = "example-group") -> tuple:
    """Every concrete deny string this runtime can emit (for the marker pin test)."""
    return (
        IDENTITY_DENY_MISSING.format(system=system, group=group),
        IDENTITY_DENY_INVALID.format(system=system),
        IDENTITY_DENY_GROUP.format(system=system, group=group),
        IDENTITY_DENY_KEY_UNREADABLE.format(system=system),
        IDENTITY_DENY_KEY_UNCONFIGURED.format(system=system),
    )


# Process-constant signing-key cache (successful reads only; failures retried).
_identity_claim_secret = None


def reset_identity_claim_secret_cache() -> None:
    """Clear the cached signing secret (tests + key rotation)."""
    global _identity_claim_secret
    _identity_claim_secret = None


def _fetch_identity_claim_secret():
    """Return the identity-claim signing secret, or None. Value never logged (R4)."""
    global _identity_claim_secret
    if _identity_claim_secret:
        return _identity_claim_secret
    secret_arn = os.environ.get(IDENTITY_CLAIM_SECRET_ARN_ENV, "").strip()
    if not secret_arn:
        return None
    try:
        import boto3  # lazy: only enforcing runtimes reach here

        resp = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
        secret = resp.get("SecretString") or ""
    except Exception:
        logger.warning("identity-claim signing secret read failed", exc_info=True)
        return None
    if not secret:
        return None
    _identity_claim_secret = secret
    return secret


def enforce_identity_claim(arguments, system_id):
    """Pop the reserved claim arg (ALWAYS) and enforce when configured.

    Mutates ``arguments`` in place (the reserved key is removed whenever the
    mapping carries it — enforcement on or off). Returns a deny TEXT when the
    call must be refused, else None (allow). SYSTEM_REQUIRED_GROUP unset ⇒
    allow-all (engine-generic default). Group SET ⇒ the policy is live: a
    missing signing-key ARN, unreadable key, or MISSING/INVALID claim all
    fail CLOSED with an honest deny naming the system (never a
    retryable-looking error — see the DENY-TEXT CONTRACT).
    Claim tokens and contents are never logged.
    """
    token = arguments.pop(IDENTITY_CLAIM_ARG, None) if isinstance(arguments, dict) else None
    required_group = os.environ.get(REQUIRED_GROUP_ENV, "").strip()
    if not required_group:
        return None  # allow-all: the engine-generic default (no per-system policy)
    secret_arn = os.environ.get(IDENTITY_CLAIM_SECRET_ARN_ENV, "").strip()
    if not secret_arn:
        # Half-configured: a group requirement without a signing key cannot be
        # enforced. Fail CLOSED — consistent with the key-unreadable path below;
        # the operator explicitly restricted this system, and silently allowing
        # (e.g. on a freshly provisioned runtime that has not yet received the
        # ARN via the next deploy's env refresh) would void that policy.
        logger.warning(
            "%s is set but %s is not — DENYING access to %s (fail closed)",
            REQUIRED_GROUP_ENV, IDENTITY_CLAIM_SECRET_ARN_ENV, system_id,
        )
        return IDENTITY_DENY_KEY_UNCONFIGURED.format(system=system_id)
    if not token or not isinstance(token, str):
        return IDENTITY_DENY_MISSING.format(system=system_id, group=required_group)
    secret = _fetch_identity_claim_secret()
    if not secret:
        return IDENTITY_DENY_KEY_UNREADABLE.format(system=system_id)
    try:
        claims = verify_claim(token, secret)
    except ClaimVerificationError:
        # Typed failure; never log the token or its contents.
        return IDENTITY_DENY_INVALID.format(system=system_id)
    if required_group not in claims.get("groups", []):
        return IDENTITY_DENY_GROUP.format(system=system_id, group=required_group)
    return None


def _process_one_mcp_message(msg, system_id):
    """Pop+enforce for ONE JSON-RPC message. Returns (deny, rpc_id, changed).

    Wave 5 (F1): the reserved ``_adt_provenance`` arg is popped
    UNCONDITIONALLY — before identity enforcement, same posture as the claim
    (external callers can never smuggle it into tool signatures) — validated,
    and stashed in ``provenance_context_var`` (valid-or-``None`` on every
    ``tools/call``; the value itself is never logged). ADR-0079: the raw
    claim string is exposed on ``identity_claim_var`` (same reset posture)
    BEFORE enforcement pops it — only the query-bridge reads it.
    """
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return None, None, False
    params = msg.get("params")
    arguments = params.get("arguments") if isinstance(params, dict) else None
    args = arguments if isinstance(arguments, dict) else {}
    had_provenance = RESERVED_PROVENANCE_ARG in args
    provenance_value = args.pop(RESERVED_PROVENANCE_ARG, None)
    provenance_context_var.set(parse_provenance_context(provenance_value))
    provenance_write_token_var.set(parse_write_token(provenance_value))
    had_chart = _pop_chart_intent(args)
    had_sources = _pop_identifier_sources(args)
    had_pairing, pairing = _pop_pair_forms(args)
    had_arg = IDENTITY_CLAIM_ARG in args
    raw_claim = args.get(IDENTITY_CLAIM_ARG)
    identity_claim_var.set(raw_claim if isinstance(raw_claim, str) and raw_claim else None)
    deny = enforce_identity_claim(args, system_id)
    if deny is None and had_pairing:
        deny = _admit_pair_forms(pairing, args, params.get("name"), system_id)
    return deny, msg.get("id"), (had_arg or had_provenance or had_chart or had_sources
                                 or had_pairing)


def _pop_pair_forms(args) -> tuple:
    """Pop the reserved pairing-request arg UNCONDITIONALLY and reset
    :data:`pair_forms_var` (set only by :func:`_admit_pair_forms`). Returns
    ``(present, value)``. ``args`` is mutated in place."""
    from .form_pairing import RESERVED_ARG
    pair_forms_var.set(None)
    had = isinstance(args, dict) and RESERVED_ARG in args
    return had, (args.pop(RESERVED_ARG, None) if had else None)


def _admit_pair_forms(raw, args, tool_name, system_id) -> str | None:
    """Admit a pairing request only on the engine's call shape: the call is
    to the one query tool name this runtime registered for ``system_id``
    (``form_pairing.query_tool_name``: bare as the cloud entry point serves
    it, prefixed in local mode; none registered admits nothing), and after
    every reserved argument is popped its arguments are exactly the fixed
    pairing question (``form_pairing.PAIRING_QUESTION``). Then the request is
    set on :data:`pair_forms_var` and ``None`` returned; otherwise the fixed
    refusal (``form_pairing.SHAPE_REFUSAL``) is returned and no tool runs."""
    from .form_pairing import PAIRING_QUESTION, SHAPE_REFUSAL, query_tool_name
    registered = query_tool_name(system_id)
    if (isinstance(raw, str) and raw and registered and tool_name == registered
            and args == {"question": PAIRING_QUESTION}):
        pair_forms_var.set(raw)
        return None
    return SHAPE_REFUSAL


def _batch_carries_pairing(body) -> tuple:
    """``(True, id)`` when ``body`` is a JSON-RPC batch with a ``tools/call``
    carrying the pairing argument (its id), else ``(False, None)``: a pairing
    request is one call per POST, never part of a batch."""
    from .form_pairing import RESERVED_ARG
    if not isinstance(body, list):
        return False, None
    for item in body:
        params = item.get("params") if isinstance(item, dict) else None
        args = params.get("arguments") if isinstance(params, dict) else None
        if isinstance(args, dict) and RESERVED_ARG in args:
            return True, item.get("id")
    return False, None


def _pop_identifier_sources(args) -> bool:
    """Pop the reserved ``_adt_identifier_sources`` arg UNCONDITIONALLY into
    :data:`identifier_sources_var` (``True`` only for the exact request
    value), the ``_pop_chart_intent`` posture. Returns whether it was
    present. ``args`` is mutated in place."""
    from .identifier_provenance import REQUESTED, RESERVED_ARG
    had = isinstance(args, dict) and RESERVED_ARG in args
    raw = args.pop(RESERVED_ARG, None) if isinstance(args, dict) else None
    identifier_sources_var.set(True if raw == REQUESTED else None)
    return had


def _pop_chart_intent(args) -> bool:
    """Pop the reserved ``_adt_chart_intent`` arg UNCONDITIONALLY into
    :data:`chart_intent_var` (``requested``/``none`` -> ``True``/``False``,
    absent/malformed -> ``None``), the exact ``_adt_provenance`` posture: an
    external caller can never smuggle it into a tool signature, and the value
    is never logged. Returns whether the arg was present. ``args`` is mutated
    in place."""
    had = isinstance(args, dict) and RESERVED_CHART_ARG in args
    raw = args.pop(RESERVED_CHART_ARG, None) if isinstance(args, dict) else None
    chart_intent_var.set(parse_chart_intent(raw))
    return had


def _pop_identity_from_mcp_body(raw, system_id):
    """Process one MCP HTTP POST body: pop the claim arg, run enforcement.

    Returns ``(new_body_bytes, deny_text_or_None, jsonrpc_id)``. Non-JSON and
    non-``tools/call`` bodies pass through untouched (initialize, tools/list,
    notifications, pings). Handles a defensive top-level list (legacy JSON-RPC
    batch): every ``tools/call`` item is popped; the first deny wins. A batch
    carrying the pairing argument is refused whole, before any item is read
    (:func:`_batch_carries_pairing`).
    """
    try:
        body = json.loads(raw)
    except Exception:
        return raw, None, None
    messages = body if isinstance(body, list) else [body]
    if not isinstance(body, (dict, list)):
        return raw, None, None
    batched, batch_id = _batch_carries_pairing(body)
    if batched:
        from .form_pairing import SHAPE_REFUSAL
        pair_forms_var.set(None)
        return raw, SHAPE_REFUSAL, batch_id
    any_changed = False
    for item in messages:
        deny, rpc_id, changed = _process_one_mcp_message(item, system_id)
        if deny is not None:
            return raw, deny, rpc_id
        any_changed = any_changed or changed
    if any_changed:
        return json.dumps(body).encode("utf-8"), None, None
    return raw, None, None


def _replay_messages(messages, fallback=None):
    """Return an ASGI ``receive`` that replays ``messages`` then defers to ``fallback``."""
    queue = list(messages)

    async def _receive():
        if queue:
            return queue.pop(0)
        if fallback is not None:
            return await fallback()
        return {"type": "http.disconnect"}

    return _receive


def _identity_deny_asgi_response(rpc_id, deny):
    """JSON-RPC ``tools/call`` error result (``isError: true``) as a Starlette response.

    An application-level tool error the gateway relays verbatim — the
    orchestrator surfaces it to the model (its text never matches a retry
    marker, so it is never silently retried).
    """
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"content": [{"type": "text", "text": deny}], "isError": True},
        }
    )


class IdentityClaimASGIMiddleware:
    """Pure-ASGI middleware: pop the reserved identity arg on /mcp POSTs, ALWAYS.

    Sits in front of the FastMCP streamable-HTTP app so the reserved argument is
    removed BEFORE FastMCP's argument validation and before any tool runs (the
    tools' typed signatures never see it, and external callers cannot smuggle
    it). On an enforcement deny it short-circuits with the JSON-RPC error
    result above. Everything else (GET/SSE streams, /ping, /invocations,
    notifications) passes through untouched.
    """

    def __init__(self, app, system_id):
        self.app = app
        self.system_id = system_id

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in ("/mcp", "/mcp/")
        ):
            return await self.app(scope, receive, send)
        chunks = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # client disconnected mid-body — nothing to enforce
                return await self.app(scope, _replay_messages([message]), send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        raw = b"".join(chunks)
        new_body, deny, rpc_id = _pop_identity_from_mcp_body(raw, self.system_id)
        if deny is not None:
            return await _identity_deny_asgi_response(rpc_id, deny)(scope, receive, send)
        if new_body != raw:
            # keep content-length honest for the downstream ASGI app
            headers = [(k, v) for k, v in scope.get("headers", []) if k != b"content-length"]
            headers.append((b"content-length", str(len(new_body)).encode("ascii")))
            scope = dict(scope)
            scope["headers"] = headers
        replay = _replay_messages(
            [{"type": "http.request", "body": new_body, "more_body": False}],
            fallback=receive,
        )
        await self.app(scope, replay, send)


# ---------------------------------------------------------------------------
# Shared FastMCP ASGI assembly + uvicorn serving (extracted from runtime_app)
# ---------------------------------------------------------------------------
def _assemble_mcp_asgi(mcp, identity_label):
    """Build the FastMCP ASGI app with warmup routes + the identity middleware.

    The AgentCore gateway uses ``/mcp`` (no slash) for discovery (ListTools) but
    ``/mcp/`` (trailing slash) for actual tool calls (CallTool). FastMCP only
    registers ``/mcp``, so tool calls would 404 — we disable redirect_slashes
    AND alias ``/mcp/`` to the same handler. ``/ping`` + ``/invocations``
    answer the AgentCore runtime health checks and ``init_event_loop`` warmup.
    Returns the app WRAPPED in ``IdentityClaimASGIMiddleware`` (ADR-0049:
    popping the reserved arg is unconditional; enforcement only activates when
    the per-runtime policy envs are set).
    """
    from starlette.responses import JSONResponse

    mcp_app = mcp.streamable_http_app()
    if hasattr(mcp_app, "router"):
        mcp_app.router.redirect_slashes = False
    try:
        mcp_routes = [r for r in mcp_app.routes if hasattr(r, "path") and r.path == "/mcp"]
        if mcp_routes:
            mcp_handler = mcp_routes[0].endpoint
            mcp_app.add_route("/mcp/", mcp_handler, methods=["POST", "GET", "DELETE", "PUT"])
    except Exception as e:
        logger.warning("Could not add /mcp/ alias: %s", e)

    async def ping(request):
        return JSONResponse({"status": "healthy"})

    async def invocations(request):
        """Handle AgentCore runtime warmup and init messages."""
        try:
            body = await request.json()
            if body.get("init_event_loop"):
                return JSONResponse({"status": "ready"})
        except Exception as e:
            # The warmup body is best-effort (a non-JSON/empty warmup ping still
            # returns ok), but log the parse error rather than discarding it.
            logger.debug("/invocations warmup body parse failed: %s", e)
        return JSONResponse({"status": "ok"})

    mcp_app.add_route("/ping", ping, methods=["GET"])
    mcp_app.add_route("/invocations", invocations, methods=["POST"])
    return IdentityClaimASGIMiddleware(mcp_app, system_id=identity_label)


def serve_mcp_app(mcp, identity_label, banner):
    """Assemble the MCP ASGI app and serve it on 0.0.0.0:8000 (blocking).

    The single serving path for every FastMCP runtime mode (system agent /
    digital-thread / onboarding). ``identity_label`` names the runtime in
    identity-policy deny texts; ``banner`` is the startup log line. Assembly
    happens INSIDE ``run()`` (as it did when this lived in runtime_app) so
    tests that stub ``anyio.run`` never touch the FastMCP app.
    """
    import anyio
    import uvicorn

    async def run():
        app = _assemble_mcp_asgi(mcp, identity_label)
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")  # nosec B104 - container service must bind to all interfaces
        server = uvicorn.Server(config)
        await server.serve()

    logger.info("%s", banner)
    anyio.run(run)
