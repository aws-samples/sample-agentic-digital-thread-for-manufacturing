# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Public error text for HTTP responses, SSE frames and polled job records.

An exception's own text can carry graph queries, hostnames, ARNs, resource
ids, file paths and upstream response bodies. None of that belongs in a
response. A caller gets a fixed message for one of a small set of categories
(:data:`PUBLIC_ERROR_MESSAGES`) plus a short reference; the full detail goes
to the server log under the same reference, built by
:mod:`agents.failure_fingerprint` (exception types and cause chain, the AWS
error code, HTTP status and request id, ``file.py:line:function`` frames and a
head of the message with credentials redacted and digits masked). No traceback
is attached to the log record: the orchestrator runtime's OTel handler drops
it, and it would re-render the unmasked message.

Typed errors this package raises with fixed text (plus ids or the caller's
own input) keep their message: :data:`SAFE_MESSAGE_TYPES` lists them by
qualified class name, each with the reason it is safe. The class must match
exactly; a subclass is not trusted by inheritance.

Every function here is total: building a public error never raises into the
failure path it reports.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Callable

from .failure_fingerprint import exception_fingerprint, format_fields, masked_head

logger = logging.getLogger(__name__)

#: Fixed public text per category. NEVER interpolate exception text into these.
PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "auth_failed": ("Authentication failed. Check the credentials and "
                    "permissions for this connection."),
    "not_reachable": ("The system could not be reached. Check the endpoint "
                      "and network access."),
    "timed_out": "The request timed out. Retry in a moment.",
    "not_found": "The requested resource was not found.",
    "invalid_response": "The system returned a response that could not be read.",
    "upstream_error": ("An upstream service returned an error. Retry in a "
                       "moment; if this keeps happening, report it."),
    "graph_query_failed": ("The knowledge graph query failed. Retry in a "
                           "moment; if this keeps happening, report it."),
    "internal_error": ("The request failed on the server. Retry in a moment; "
                       "if this keeps happening, report it."),
}

#: The closed category vocabulary (the keys above).
PUBLIC_ERROR_CATEGORIES = frozenset(PUBLIC_ERROR_MESSAGES)

#: Repo exception classes whose message is fixed text plus ids or the
#: caller's own input, keyed by ``module.QualName`` with the reason. Matched
#: on the EXACT class.
SAFE_MESSAGE_TYPES: dict[str, str] = {
    "agents.onboard.ResetIncompleteError":
        "fixed removal/reset retry guidance plus system ids and exception type names",
    "agents.connection_security.ConnectionTargetError":
        "fixed SSRF-guard refusals; the only interpolations are the caller's "
        "scheme, the connection type and parse_pg_registration_dsn's fixed reasons",
    "agents.errors.ConnectionSecretUnavailableError":
        "fixed 'the connection secret for <id> ...' reasons; exception type "
        "names only, never secret values",
    "agents.connection_resolver.OwnSecretDeletionError":
        "fixed text plus exception type names (its documented contract)",
    "agents.athena_tools.AthenaConfigError":
        "validates the caller's own Athena settings and quotes back only that input",
    "agents.duckdb_tools.DuckDBConfigError":
        "validates the caller's own S3 Tables settings and quotes back only that input",
    "agents.duckdb_tools.DuckDBAuthorizationError":
        "fixed fix-the-SQL guidance for a refused DuckDB statement (not a SELECT/WITH, a "
        "table function, a file/URL/secret reach, or an out-of-scope relation); never echoes engine internals",
    "agents.entity_rescan.UnknownSystemError": "'Unknown system: <id>'",
    "agents.asset_role_pass.RolePassError":
        "fixed text plus an exception type name or a fixed category",
    "agents.context_interview.refine.RefineUnavailable":
        "an exception type name or fixed text",
    "agents.metric_definition.MetricDefinitionError":
        "validation of the steward's own metric document",
    "agents.metric_preview.PreviewParameterError":
        "validation of the caller's with= parameter",
    "agents.metric_resolver.MetricResolverError":
        "the metric layer's boundary reasons (concept, system, table and column "
        "names), surfaced verbatim by design (metric_turn_notice._classify_error)",
    "agents.metric_resolver.CoverageGapError": "same contract as MetricResolverError",
    "agents.metric_resolver.UnresolvedInputError": "same contract as MetricResolverError",
    "agents.quick_gateway_token.TokenResolutionError":
        "fixed text naming the token store and the AWS error code; never the token",
    "agents.memory_model.MemoryModelError":
        "fixed text plus model ids, memory strategy names and AgentCore status words "
        "read from GetMemory; never an SDK message",
}

#: Reference length (hex characters of a random UUID).
_REF_CHARS = 12
#: Longest digit-masked message head carried into the log line.
_LOG_HEAD_CHARS = 300

_AUTH_CODES = frozenset({
    "AccessDenied", "AccessDeniedException", "AuthFailure", "AuthorizationError",
    "ExpiredToken", "ExpiredTokenException", "IncompleteSignature",
    "InvalidAccessKeyId", "InvalidClientTokenId", "InvalidSignatureException",
    "MissingAuthenticationToken", "NotAuthorized", "NotAuthorizedException",
    "SignatureDoesNotMatch", "UnauthorizedOperation", "UnauthorizedException",
    "UnrecognizedClientException",
})
_NOT_FOUND_CODES = frozenset({
    "NoSuchBucket", "NoSuchEntity", "NoSuchKey", "NotFound", "NotFoundException",
    "ParameterNotFound", "ResourceNotFound", "ResourceNotFoundException",
})
_TIMEOUT_CODES = frozenset({"RequestTimeout", "RequestTimeoutException"})


def is_safe_message(exc: BaseException) -> bool:
    """True when ``exc`` is exactly one of :data:`SAFE_MESSAGE_TYPES`."""
    cls = type(exc)
    return f"{cls.__module__}.{cls.__qualname__}" in SAFE_MESSAGE_TYPES


def _mro_names(exc: BaseException) -> list[str]:
    return [c.__name__ for c in type(exc).__mro__]


def _aws_code_and_status(exc: BaseException) -> tuple[str, int | None]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") if isinstance(response.get("Error"), dict) else {}
        meta = (response.get("ResponseMetadata")
                if isinstance(response.get("ResponseMetadata"), dict) else {})
        status = meta.get("HTTPStatusCode")
        return str(error.get("Code") or ""), status if isinstance(status, int) else None
    # httpx.HTTPStatusError and requests.HTTPError carry a response object.
    status = getattr(response, "status_code", None)
    return "", status if isinstance(status, int) else None


def _by_status(status: int | None) -> str | None:
    if status in (401, 403):
        return "auth_failed"
    if status == 404:
        return "not_found"
    if status in (408, 504):
        return "timed_out"
    if isinstance(status, int) and (status == 429 or status >= 500):
        return "upstream_error"
    return None


def classify(exc: BaseException, default: str = "internal_error") -> str:
    """The public category for ``exc``, from its TYPE, AWS error code and HTTP
    status only; the message text is never read. ``default`` covers the rest."""
    try:
        names = _mro_names(exc)
        if "GraphQueryError" in names:
            return "graph_query_failed"
        if any("Timeout" in n for n in names) or isinstance(exc, TimeoutError):
            return "timed_out"
        code, status = _aws_code_and_status(exc)
        if code in _AUTH_CODES:
            return "auth_failed"
        if code in _NOT_FOUND_CODES:
            return "not_found"
        if code in _TIMEOUT_CODES:
            return "timed_out"
        by_status = _by_status(status)
        if by_status is not None:
            return by_status
        if code:
            return "upstream_error"
        if any(n in ("NoCredentialsError", "PartialCredentialsError",
                     "CredentialRetrievalError") for n in names):
            return "auth_failed"
        pgcode = getattr(exc, "pgcode", None)
        if isinstance(pgcode, str) and pgcode.startswith("28"):
            return "auth_failed"  # PostgreSQL class 28: invalid authorization
        if "OperationalError" in names and not pgcode:
            return "not_reachable"
        if isinstance(exc, ConnectionError) or any(
                n in ("ConnectError", "EndpointConnectionError", "NewConnectionError",
                      "ConnectionClosedError", "RemoteProtocolError", "gaierror")
                for n in names):
            return "not_reachable"
        if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) or any(
                n in ("YAMLError", "DecodingError") for n in names):
            return "invalid_response"
    except Exception:  # noqa: BLE001 - classification never raises
        pass
    return default if default in PUBLIC_ERROR_CATEGORIES else "internal_error"


def new_ref() -> str:
    """A short random reference an operator can search the logs for."""
    return uuid.uuid4().hex[:_REF_CHARS]


def public_message(category: str, ref: str) -> str:
    """The fixed text for ``category`` with ``ref`` appended."""
    text = PUBLIC_ERROR_MESSAGES.get(category, PUBLIC_ERROR_MESSAGES["internal_error"])
    return f"{text} Reference: {ref}"


def _log_detail(exc: BaseException, where: str, category: str, ref: str,
                scrub: Callable[[str], str] | None) -> None:
    try:
        fields = exception_fingerprint(exc)
        raw = str(exc)
        if scrub is not None:
            raw = scrub(raw)
        # Order: the call site's scrub, then masked_head (the shared credential
        # redaction over the whole message, then digit masking and the cut).
        fields["head"] = masked_head(raw, _LOG_HEAD_CHARS)
        detail = format_fields(fields)
    except Exception:  # noqa: BLE001 - the log line never masks the failure
        detail = "fingerprint=unavailable"
    safe_where = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(where))[:80] or "-"
    logger.warning("PUBLIC_ERROR ref=%s where=%s category=%s %s",
                   ref, safe_where, category, detail)


def public_error(exc: BaseException, where: str, *, category: str | None = None,
                 default: str = "internal_error",
                 scrub: Callable[[str], str] | None = None) -> dict:
    """``{"error", "error_category", "error_ref"}`` for a response or job record.

    ``where`` names the call site in the log line only. ``category`` forces the
    category; otherwise :func:`classify` picks it, falling back to ``default``.
    ``scrub`` (for example ``connection_security._safe_detail`` bound to the
    resolved config) runs on the message first; the shared credential
    redaction, digit masking and the cut follow, with or without it.
    A :data:`SAFE_MESSAGE_TYPES` exception keeps its own message.
    """
    try:
        if is_safe_message(exc):
            return {"error": str(exc)}
        cat = category if category in PUBLIC_ERROR_CATEGORIES else classify(exc, default)
    except Exception:  # noqa: BLE001 - a public error never raises
        cat = "internal_error"
    ref = new_ref()
    _log_detail(exc, where, cat, ref, scrub)
    return {"error": public_message(cat, ref), "error_category": cat, "error_ref": ref}


def public_error_text(exc: BaseException, where: str, **kwargs) -> str:
    """Just the ``error`` string of :func:`public_error`."""
    return public_error(exc, where, **kwargs)["error"]
