# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""query-bridge — FastMCP tool server exposing the ADT async query path over MCP.

A thin, stateless MCP tool server (NO model, NO reasoning loop — not an agent).
Registered on the AgentCore Gateway as its own target so an external MCP client
(e.g. AWS Quick web connector / Quick Suite) gets the ADT digital-thread
query experience as a browsable connector, with timeout-safe long-running
queries. Provisioned only when the Quick integration is enabled
(``quick_api_enabled`` — see ``agent_manager.ensure_query_bridge_target``);
base deploys never build it.

It exposes three tools that translate MCP calls into HTTP calls against the
ADT ECS app (reached over the VPC via ADT_APP_URL):

  * submit_query(question, plant)  -> POST {ADT_APP_URL}/api/ask/async
      returns {job_id, status:"running"} immediately (<1s), so the MCP call
      returns well under AWS Quick's 60s per-call limit.
  * get_query_result(job_id)       -> GET {ADT_APP_URL}/api/ask/result/{job_id}
      the app waits ~20s server-side per GET and the bridge repeats the GET
      while another hold fits one tool call (~40s, POLL_CALL_BUDGET_SECONDS),
      so a 90-120s query resolves in ~3 tool calls and a 10-minute one in
      ~15. Returns {status: running|done|error|unknown, answer?}; a running
      reply also names the next call (next_tool, next_arguments) and says to
      make it now, in the same turn (ADR-0071 amendment 2026-10-19).
  * get_prompts()                  -> GET {ADT_APP_URL}/api/prompts
      the suggested-query catalog, for the greeting. Read over HTTP (the same
      route the web UI uses) so the bridge needs no DynamoDB access of its own.

The orchestrator (the Strands agent in the adt_orchestrator runtime) is NOT
called directly — the app's /api/ask/async runs it in a background thread and
stores the result, and this bridge just submits + polls. This reuses the async
job endpoints the app already exposes (server.py ``_run_ask_job``).

Runtime wiring (the /mcp + /mcp/ alias the gateway needs, /ping, /invocations
warmup) is provided by runtime_app when SYSTEM_ID=query-bridge selects this
server — mirroring how digital_thread_server pairs with
runtime_app._run_digital_thread.

Config (env):
  ADT_APP_URL          Required. The ADT app base URL reachable from the
                       runtime (the internal ALB), e.g.
                       http://internal-...elb.amazonaws.com
  QUICK_GATEWAY_TOKEN_REF
                       Where the x-quick-auth shared secret the app's
                       AuthMiddleware validates is stored: the ARN of a
                       Secrets Manager secret or an SSM parameter. Read with
                       the runtime's own role on first use and cached
                       (agents/quick_gateway_token.py), so the value is not
                       in the runtime env.
  QUICK_GATEWAY_TOKEN  The same secret in plaintext. Used only when no
                       reference is set (a bridge provisioned by an older
                       app or stack).
"""

import json
import logging
import os
import re
import time

from mcp.server.fastmcp import FastMCP

from .failure_fingerprint import masked_head
from .public_errors import public_error, public_error_text
from .quick_gateway_token import forget_cached_token, resolve_gateway_token

logger = logging.getLogger(__name__)

TARGET_NAME = "query-bridge"

# Header carrying the gateway shared secret (must match
# agents/auth/middleware.py::AuthMiddleware.QUICK_GATEWAY_HEADER).
QUICK_GATEWAY_HEADER = "x-quick-auth"

# Quick's agent abandons a poll loop it believes is taking too long and splits
# a long question into several submits (seen live, workshop event 24). The
# bridge is the only surface Quick reads, so every reply tells it what to do
# next. job_id -> epoch of the submit (or first poll) so a 'running' reply can
# say how long the query has been going; bounded by _JOB_FIRST_SEEN_MAX.
_JOB_FIRST_SEEN: dict = {}
_JOB_FIRST_SEEN_MAX = 64
#: What a caller should expect at most; the app's stale-job takeover is 600 s.
EXPECTED_TOTAL_SECONDS_UP_TO = 600
#: The app admits ONE async ask at a time; a busy answer should be retried
#: after about one server-side poll hold (server._ASK_RESULT_WAIT_SECONDS).
BUSY_RETRY_AFTER_SECONDS = 20
#: One app poll holds about this long server-side (server._ASK_RESULT_WAIT_SECONDS).
_APP_POLL_HOLD_SECONDS = 20.0
#: Wall-clock budget of ONE get_query_result tool call. Quick's agent stops a
#: poll loop after a bounded number of tool calls: with one app hold per call it
#: gave up at about 4 minutes and had to be nudged (workshop event 28,
#: 2026-10-06). The bridge therefore re-polls the app inside one call while a
#: fresh hold still fits this budget, so one Quick call covers about two holds
#: (~40 s) and the same number of calls covers twice the wall time. Kept with
#: margin under Quick's 60 s per-call limit and the AgentCore Runtime's 60 s
#: MCP tool interruption; every app GET's timeout is capped by what is left of it.
POLL_CALL_BUDGET_SECONDS = 44.0
#: Network and serialisation allowance when deciding whether another hold fits.
_POLL_OVERHEAD_SECONDS = 2.0
#: How far inside one GET's timeout the requested app hold ends (the hold's
#: record reads and the network).
_HOLD_MARGIN_SECONDS = 3.0


def _app_base() -> str:
    """The ADT app base URL the tools call, from ADT_APP_URL (no trailing slash)."""
    return os.environ.get("ADT_APP_URL", "").rstrip("/")


def _gateway_headers() -> dict:
    """Headers for calling the app's gateway-protected async query endpoints.

    The app's AuthMiddleware allows /api/ask/async, /api/ask/result/* and the
    read-only GET /api/prompts without a Cognito session ONLY when the request
    carries the shared secret header x-quick-auth == QUICK_GATEWAY_TOKEN (see
    agents/auth/middleware.py). This bridge reaches the app directly over the
    VPC (not via the API Gateway that injects the secret on the kiosk path),
    so it must supply the header itself.

    ADR-0079: when the current ``tools/call`` carried an interceptor-minted
    ``_adt_identity`` claim (delegated user tokens on the Quick gateway), the
    popped raw claim is forwarded VERBATIM as ``DELEGATED_IDENTITY_HEADER``.
    The bridge never parses, verifies or logs it — the app is the verifier.
    No claim ⇒ headers byte-identical to the anonymous path.

    The token comes from :func:`resolve_gateway_token`: the stored value when
    QUICK_GATEWAY_TOKEN_REF is set, else the plaintext env as before. For a
    given token the headers are the same either way. A token that cannot be
    read raises TokenResolutionError; every caller runs this inside its
    try block, so the tool answers with its usual structured error.
    """
    from .auth.delegated_identity import DELEGATED_IDENTITY_HEADER
    from .runtime_asgi import identity_claim_var

    headers = {"Content-Type": "application/json"}
    token = resolve_gateway_token()
    if token:
        headers[QUICK_GATEWAY_HEADER] = token
    claim = identity_claim_var.get()
    if claim:
        headers[DELEGATED_IDENTITY_HEADER] = claim
    return headers


def _forget_token_on_401(exc) -> None:
    """The app refused the token: the stored one may have been replaced since
    it was cached, so read it again on the next call."""
    if exc.response is not None and exc.response.status_code == 401:
        forget_cached_token()


#: Longest head of an upstream error body written to the server log line.
_ERROR_BODY_LOG_CHARS = 300


def _http_error_reply(exc, action: str) -> str:
    """The tool result for an HTTP error from the app: ``"<action> failed HTTP
    <status>: "`` plus a fixed category message and a reference
    (``public_errors``). The upstream body never reaches the caller: it can
    carry validation input, internal ids, hosts or service detail. It goes to
    the server log under the same reference, through the shared credential
    redaction and digit masking (``failure_fingerprint.masked_head``)."""
    response = exc.response
    status = response.status_code if response is not None else "unknown"
    err = public_error(exc, f"query_bridge.{action}", default="upstream_error")
    try:
        body = masked_head(exc.response.text, _ERROR_BODY_LOG_CHARS) if response is not None else ""
    except Exception:  # noqa: BLE001 - an unreadable body still gets the reply
        body = "unreadable"
    logger.warning("QUERY_BRIDGE_HTTP_ERROR ref=%s action=%s status=%s body=%s",
                   err.get("error_ref", "-"), action, status, body)
    return json.dumps({"status": "error",
                       "error": f"{action} failed HTTP {status}: {err['error']}",
                       "error_category": err.get("error_category", ""),
                       "error_ref": err.get("error_ref", "")})


def _remember_job(job_id: str) -> None:
    """Record when a job was first seen so polls can report elapsed time."""
    if not job_id:
        return
    _JOB_FIRST_SEEN.setdefault(job_id, time.time())
    while len(_JOB_FIRST_SEEN) > _JOB_FIRST_SEEN_MAX:
        _JOB_FIRST_SEEN.pop(next(iter(_JOB_FIRST_SEEN)))


def _running_job_id() -> str:
    """The most recent job this bridge submitted that has not finished.

    It spans every caller of this process, so only the no-claim (service
    authentication) busy reply may fall back to it; see ``_busy_reply``."""
    return next(reversed(_JOB_FIRST_SEEN), "") if _JOB_FIRST_SEEN else ""


#: The shape of a job id the busy reply may echo (the app mints short hex
#: ids). Anything else in the 429 body is ignored, never put in next_action.
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _app_running_job_id(body) -> str:
    """The ``running_job_id`` the app put in its 429 body, or ``""``.

    The app names a job only when it is the caller's own (server.py
    ``_busy_running_job_id``), so this is the one source that is safe to
    echo to a caller with a delegated identity."""
    if not isinstance(body, dict):
        return ""
    jid = body.get("running_job_id")
    return jid if isinstance(jid, str) and _JOB_ID_RE.fullmatch(jid) else ""


def _response_json(response):
    """The response body as JSON, or None when it is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _busy_reply(app_body=None) -> str:
    """The app admits one query at a time: say so, and say what to do.

    The app's 429 always carries ``running_job_id``: the caller's own running
    job, or null (server.py ``_busy_running_job_id``). The reply names only
    that job. The bridge's local memory spans every caller, so it is used
    only when the app sent no such key at all (an app older than this
    bridge) and the call has no delegated identity claim (ADR-0079). The
    bridge only checks whether a claim is present; it never reads or
    verifies it."""
    from .runtime_asgi import identity_claim_var

    delegated = bool(identity_claim_var.get())
    running = _app_running_job_id(app_body)
    app_answered = isinstance(app_body, dict) and "running_job_id" in app_body
    if not running and not delegated and not app_answered:
        running = _running_job_id()
    lead = ("A query is already running for this user." if delegated and running
            else "A query is already running on this deployment.")
    if running:
        step = (f"Do not submit again: call get_query_result with job_id "
                f"'{running}'. The running answer covers the whole question "
                "you submitted.")
    else:
        step = (f"Do not submit in parallel: wait {BUSY_RETRY_AFTER_SECONDS} "
                "seconds, then call submit_query once more with the same "
                "question. After three busy replies in a row, stop and tell "
                "the user the digital thread is busy and to ask again in a "
                "few minutes.")
    return json.dumps({
        "status": "busy",
        "running_job_id": running,
        "retry_after_seconds": BUSY_RETRY_AFTER_SECONDS,
        "next_action": f"{lead} {step}",
    })


def submit_query(question: str, plant: str = "") -> str:
    """Submit one manufacturing question to the digital thread for async execution. Call this ONCE per user question, with the whole question exactly as the user wrote it; never split a question into several submit_query calls, the digital thread answers every part in one run. Returns a job_id immediately, which does NOT contain the answer. You MUST then call get_query_result with that job_id right away, in the same turn, and repeatedly while its status is 'running'. Queries span connected plant systems (ERP, MES, CMMS, PLM, IoT/UNS) and can take ten minutes or more; keep calling get_query_result with the same job_id while the status is running; stop on done, error or unknown. If the result is status 'busy', a query is already running: when it carries a running_job_id, do not submit again and poll that job_id with get_query_result instead; when running_job_id is empty, wait retry_after_seconds and call submit_query once more, and after three busy replies in a row tell the user the digital thread is busy.

    Args:
        question: The natural-language manufacturing question to run.
        plant: Optional plant filter; "" or "all" for all plants.
    """
    import httpx

    q = (question or "").strip()
    if not q:
        return json.dumps({"status": "error", "error": "'question' is required"})
    base = _app_base()
    if not base:
        return json.dumps({"status": "error", "error": "query app not configured (ADT_APP_URL unset)"})
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{base}/api/ask/async",
                json={"question": q, "plant": plant or ""},
                headers=_gateway_headers(),
            )
            resp.raise_for_status()
            payload = resp.json()
            _remember_job(str(payload.get("job_id") or ""))
            return json.dumps(payload)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            return _busy_reply(_response_json(exc.response))
        _forget_token_on_401(exc)
        return _http_error_reply(exc, "submit")
    except Exception as exc:  # noqa: BLE001 — surface any failure to the caller
        return json.dumps({"status": "error", "error": f"{type(exc).__name__}: "
                           + public_error_text(exc, "query_bridge", default="not_reachable")})


#: Carried on every ``done`` reply that has an answer. The external client's
#: model reads only this bridge's text, and on workshop event 31 AWS Quick's
#: report and slides carried totals that did not match ADT's (one account a
#: different value column, one quantities 240x). This keeps each figure and its
#: label as stated when the client shows or reuses it. It does NOT ask for the
#: whole answer verbatim: an answer can carry working notes, which the client
#: may summarize away. It names no data and no answer, and it is a second line
#: of defense only: it cannot correct a wrong figure in the answer itself.
_DONE_NEXT_ACTION = (
    "Keep every figure this answer states (quantities, values, totals) "
    "together with its label exactly as written, wherever you show or reuse "
    "it (your reply, a report, slides, a summary): do not recompute, multiply, "
    "re-total, or switch to a different value column. The rest of the wording "
    "you may summarize. Anything this answer does not state is 'not "
    "confirmed'; to fill a gap, call submit_query again rather than estimating.")


#: The tool a 'running' reply names as the next call (``next_tool``).
_POLL_TOOL_NAME = "get_query_result"


def _running_next_action(job_id: str) -> str:
    """The ``next_action`` text of a 'running' reply.

    On the 2026-10-19 walks Quick twice wrote "I'm continuing to wait for the
    results ... Hang tight!" to the user and stopped polling until the user
    replied: the earlier text said to call again, but not to call now, in the
    same turn, nor that a reply to the user is what ends the polling. The job
    id is named only when it has the shape the app mints (``_JOB_ID_RE``);
    the phrase "the same job_id" is kept either way."""
    named = (f" ('{job_id}')" if isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id)
             else "")
    return (
        "The digital thread is still working; this reply has no answer yet. "
        f"Call {_POLL_TOOL_NAME} again now with the same job_id{named}, in "
        "this same turn, before you write anything to the user. Do not send "
        "the user a progress or 'still waiting' message between calls: a "
        "reply ends your turn, the polling stops, and the user never gets the "
        "answer. Do not stop, do not resubmit, and do not ask the user "
        "whether to continue.")


def _annotate_poll(job_id: str, payload: dict) -> dict:
    """Make a poll reply self-explanatory for an agent that may give up early.

    A 'running' reply keeps the keys it always carried (``status``,
    ``job_id``, ``elapsed_seconds``, ``expected_total_seconds_up_to``,
    ``next_action``) and adds the next call as data: ``next_tool``,
    ``next_arguments`` (only for a well-formed job id) and
    ``retry_after_seconds`` 0, the same field the busy reply uses, here
    meaning "call again at once". A terminal reply names no next call."""
    status = payload.get("status")
    if status == "running":
        _remember_job(job_id)
        elapsed = int(time.time() - _JOB_FIRST_SEEN.get(job_id, time.time()))
        payload["elapsed_seconds"] = elapsed
        payload["expected_total_seconds_up_to"] = EXPECTED_TOTAL_SECONDS_UP_TO
        payload["next_action"] = _running_next_action(job_id)
        payload["next_tool"] = _POLL_TOOL_NAME
        if isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id):
            payload["next_arguments"] = {"job_id": job_id}
        payload["retry_after_seconds"] = 0
    elif status in ("done", "error", "unknown"):
        _JOB_FIRST_SEEN.pop(job_id, None)
        if status == "done" and payload.get("answer"):
            payload["next_action"] = _DONE_NEXT_ACTION
    return payload


def _another_hold_fits(deadline: float) -> bool:
    """Whether one more ~20 s app hold ends before the tool call's deadline."""
    return (deadline - time.monotonic()
            >= _APP_POLL_HOLD_SECONDS + _POLL_OVERHEAD_SECONDS)


def _get_timeout(deadline: float) -> float:
    """One app GET's timeout: what is left of the call budget, less the
    serialisation allowance, so no GET can carry the call past it."""
    return max(1.0, deadline - time.monotonic() - _POLL_OVERHEAD_SECONDS)


def _hold_for(timeout: float) -> float:
    """The app hold to ask for (the result route's ``wait``): never more than
    the app's own hold, and :data:`_HOLD_MARGIN_SECONDS` inside this GET's
    timeout, so the app's reply reaches this GET and the call does not end on
    a stale 'running'. A reply that still arrives after the bridge stopped
    listening is not lost: the app answers a finished job the same way on
    every poll until its record expires, so the next call gets it. The margin
    covers the hold's record reads and the network."""
    return max(0.0, min(_APP_POLL_HOLD_SECONDS, timeout - _HOLD_MARGIN_SECONDS))


def get_query_result(job_id: str) -> str:
    """Poll for the result of a query started with submit_query, using its job_id. Returns status 'running' (still processing), 'done' (with the answer, and sometimes a 'warning'), 'error', or 'unknown' (no such job). A 'done' reply that carries a one-line 'warning' means the answer may be incomplete: show the warning to the user together with the answer, and do not present the answer as complete. Each call waits about 40 seconds server-side before answering 'running'. A query can take ten minutes or more; keep calling get_query_result with the same job_id while the status is running; stop on done, error or unknown. Never stop early, never resubmit the question, and never ask the user whether to continue; the user is waiting for the answer. On 'running', call get_query_result again right away, in the same turn: do not write a progress or 'please wait' message to the user between calls, because a reply ends your turn and the polling stops. A 'running' reply carries elapsed_seconds, next_action, and next_tool with next_arguments: the exact call to make next. Read-only.

    Args:
        job_id: The job_id returned by submit_query.
    """
    import httpx

    jid = (job_id or "").strip()
    if not jid:
        return json.dumps({"status": "error", "error": "'job_id' is required"})
    base = _app_base()
    if not base:
        return json.dumps({"status": "error", "error": "query app not configured (ADT_APP_URL unset)"})
    try:
        deadline = time.monotonic() + POLL_CALL_BUDGET_SECONDS
        last_running = None
        # The app long-polls up to ~20s per GET; the bridge repeats the GET
        # while another hold fits POLL_CALL_BUDGET_SECONDS. Every GET's timeout
        # is capped by what is left of that budget, and the GET asks the app to
        # hold (``wait``) no longer than that timeout less a margin, so the app
        # answers before the bridge stops listening. A later hold that still
        # times out or loses its connection returns the 'running' reply already
        # in hand rather than an error; if the job finished during that hold,
        # the next call gets the answer (the app delivers a finished job on
        # every poll until its record expires).
        with httpx.Client(timeout=POLL_CALL_BUDGET_SECONDS) as client:
            while True:
                timeout = _get_timeout(deadline)
                try:
                    resp = client.get(f"{base}/api/ask/result/{jid}", headers=_gateway_headers(),
                                      params={"wait": f"{_hold_for(timeout):.1f}"},
                                      timeout=timeout)
                except httpx.TransportError:
                    if last_running is None:
                        raise
                    return json.dumps(_annotate_poll(jid, last_running))
                resp.raise_for_status()
                payload = dict(resp.json())
                if payload.get("status") != "running" or not _another_hold_fits(deadline):
                    return json.dumps(_annotate_poll(jid, payload))
                last_running = payload
    except httpx.HTTPStatusError as exc:
        _forget_token_on_401(exc)
        return _http_error_reply(exc, "result")
    except Exception as exc:  # noqa: BLE001 — surface any failure to the caller
        return json.dumps({"status": "error", "error": f"{type(exc).__name__}: "
                           + public_error_text(exc, "query_bridge", default="not_reachable")})


def get_prompts() -> str:
    """Return the suggested query catalog for this deployment. Call this on a greeting (the user says 'hello' or 'what can I ask?') to present suggested queries grouped by persona. Each suggestion has a short 'display_text' to show the user and an exact 'text' to pass VERBATIM to submit_query so the digital thread runs the same query the ADT app runs and returns the same result. Read-only — it runs no query.
    """
    import httpx

    base = _app_base()
    if not base:
        return json.dumps({"prompts": [], "message": "prompts unavailable: ADT_APP_URL unset"})
    try:
        # The SAME catalog the web UI renders (registry/demo-pack prompts,
        # satisfiability-filtered server-side) — read over HTTP so the answers
        # a Quick user gets match the app byte-for-byte.
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{base}/api/prompts", headers=_gateway_headers())
            resp.raise_for_status()
            return json.dumps({"prompts": resp.json()})
    except httpx.HTTPStatusError as exc:
        _forget_token_on_401(exc)
        return json.dumps({"prompts": [], "message": f"prompts unavailable: {type(exc).__name__}"})
    except Exception as exc:  # noqa: BLE001 — greeting must never hard-fail
        return json.dumps({"prompts": [], "message": f"prompts unavailable: {type(exc).__name__}"})


# Registration policy: read-only tools carry ToolAnnotations(readOnlyHint);
# submit_query mutates (it starts a job) so it carries none.
_READ_ONLY_TOOLS = frozenset({get_query_result, get_prompts})
_REGISTERED_TOOLS = (submit_query, get_query_result, get_prompts)


def create_query_bridge_server(host: str = "0.0.0.0", port: int = 8000) -> FastMCP:  # nosec B104 - container service binds all interfaces
    """Build the query-bridge FastMCP server exposing the 3 async query tools.

    Registers each function using its name + docstring as the MCP tool name and
    description (same convention as digital_thread_server). Returns the
    configured FastMCP instance; the caller (runtime_app) mounts it with the
    shared runtime_asgi.serve_mcp_app wiring.
    """
    from mcp.types import ToolAnnotations

    mcp = FastMCP(name=f"adt-{TARGET_NAME}", host=host, port=port, stateless_http=True)
    for fn in _REGISTERED_TOOLS:
        annotations = (
            ToolAnnotations(readOnlyHint=True, openWorldHint=False)
            if fn in _READ_ONLY_TOOLS else None
        )
        mcp.tool(name=fn.__name__, description=fn.__doc__, annotations=annotations)(fn)
    logger.info("query-bridge MCP server built: tools=%s ADT_APP_URL=%s",
                [fn.__name__ for fn in _REGISTERED_TOOLS],
                "set" if _app_base() else "UNSET")
    return mcp
