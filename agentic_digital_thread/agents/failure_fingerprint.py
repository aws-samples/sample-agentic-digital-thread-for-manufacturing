# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Payload-safe fingerprints for failures the user only ever sees as "Query failed."

The app-side stream read logged ``QUERY_FAILURE category=RUNTIME_UNAVAILABLE``
for every non-AWS exception, so a botocore read timeout, a connection the
service closed mid-chunk and a bug in our own frame parser all looked the same
(13 occurrences on one rehearsal account in four days, none diagnosable,
2026-10-08). These helpers name the failure without logging data:

* exception TYPES (qualified, plus the cause chain) and the AWS error code,
  HTTP status and request id, each matched against a strict character set;
* the code location as ``file.py:line:function`` frames, never their locals;
* a single-line head of the message with credentials redacted
  (:func:`redact_secrets`) and then digits masked (the precedent in
  ``chat_client._redact_digits`` and ``runtime_app._redacted_error_head``:
  digits carry telemetry and business values);
* a frame label built ONLY from a fixed allowlist of top-level stream keys, so
  a key the runtime invents can never carry a value into the log.

Every function is total: a fingerprint never raises into the failure path it
describes.
"""
from __future__ import annotations

import os
import re
import traceback

from .connection_security import mask_url_credentials_for_log
from .response_integrity_security import redact_sensitive

#: Top-level keys a runtime stream frame is known to carry. Anything else is
#: counted as ``other`` and never named.
FRAME_KEYS = frozenset({
    "complete", "current_tool_use", "data", "delta", "error", "event",
    "force_stop", "init_event_loop", "message", "metadata", "reasoning",
    "reasoningText", "result", "start", "start_event_loop", "tool_result",
    "tool_result_metadata", "type", "usage",
})

#: Keys of a nested ``event`` (Bedrock ConverseStream) frame.
EVENT_KEYS = frozenset({
    "contentBlockDelta", "contentBlockStart", "contentBlockStop",
    "messageStart", "messageStop", "metadata",
})

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_MAX_FRAMES = 4
_HEAD_LIMIT = 160


def _token(value: object) -> str:
    """``value`` when it is a short identifier-shaped string, else ``""``."""
    text = str(value) if value is not None else ""
    return text if _TOKEN_RE.match(text) else ""


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    module = cls.__module__
    name = cls.__qualname__
    return name if module == "builtins" else f"{module}.{name}"


def _cause_chain(exc: BaseException, depth: int = 3) -> str:
    """Types of the explicit cause / implicit context chain, innermost last."""
    names: list[str] = []
    current: BaseException | None = exc
    seen = {id(exc)}
    while current is not None and len(names) < depth:
        nxt = current.__cause__
        if nxt is None and not current.__suppress_context__:
            nxt = current.__context__
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        names.append(_type_name(nxt))
        current = nxt
    return "<-".join(names) or "none"


def _frames(exc: BaseException) -> str:
    """The innermost frames as ``file.py:line:function`` (no locals, no paths)."""
    try:
        summary = traceback.extract_tb(exc.__traceback__)[-_MAX_FRAMES:]
    except Exception:  # noqa: BLE001 - a fingerprint never raises
        return "unknown"
    parts = [f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in summary]
    return ">".join(parts) or "none"


#: What a head says when the redactors fail: no message text at all.
REDACTION_UNAVAILABLE = "<redaction-unavailable>"
#: What a head says when its window is one unbroken token too long to judge.
LONG_TOKEN_WITHHELD = "<long-token-withheld>"  # nosec B105 - a redaction marker, not a credential
#: Characters past a head's limit that still reach the redactors, so a secret
#: that starts inside the head is seen whole. The window is bounded because
#: one of the shared patterns is quadratic on long dotted runs (64,000
#: characters took about 15 s on 2026-10-12).
_REDACT_SLACK = 512


def _window(text: str, limit: int) -> str:
    """The first ``limit`` + slack characters, minus any token the cut splits.

    A pattern that needs the whole value (a JWT's three parts, a URL's ``@``)
    misses a partial one, and redaction can shorten the text before it enough
    to pull it into the head, so a split token is dropped, never redacted.
    """
    end = max(int(limit), 0) + _REDACT_SLACK
    if len(text) <= end or text[end].isspace():
        return text[:end]
    i = end - 1
    while i >= 0 and not text[i].isspace():
        i -= 1
    return text[:i] if i >= 0 else LONG_TOKEN_WITHHELD


def redact_secrets(text: str, limit: int = _HEAD_LIMIT) -> str:
    """The start of *text*, enough for a *limit*-character head, with
    credentials redacted by the app's two existing redactors.

    ``connection_security.mask_url_credentials_for_log`` masks each URL
    conservatively (from ``://`` through the last ``@`` on its line, so a raw
    '/', '?', '#' or whitespace in a password cannot end the mask early, and
    one raw line break in it moves the mask to the next line; then secret query
    parameters, and a URL that does not parse from its authority on), then
    ``response_integrity_security.redact_sensitive`` (Authorization values
    under any scheme, also folded onto following lines or written as a YAML
    block, prefixed secret keys such as ``client_secret=`` or ``dbPassword:``
    and their folded or quoted multi-line values,
    bearer tokens, JWTs, access key ids and the other forms it knows) runs over
    the whole window. Callers mask digits and cut at *limit* AFTER this, so a
    secret cannot straddle the cut and a masked digit cannot break a pattern.
    Fails closed: if a redactor raises, no text survives.
    """
    try:
        window = _window(str(text), limit)
        masked = mask_url_credentials_for_log(window)
        return redact_sensitive(masked, max_length=len(masked))
    except Exception:  # noqa: BLE001 - a fingerprint never raises
        return REDACTION_UNAVAILABLE


def masked_head(text: object, limit: int = _HEAD_LIMIT) -> str:
    """One line, credentials redacted, digits masked, at most *limit* chars."""
    try:
        raw = str(text)
    except Exception:  # noqa: BLE001 - an unprintable exception still gets a type
        raw = "<unprintable>"
    flat = re.sub(r"\s+", " ", redact_secrets(raw, limit)).strip()
    return re.sub(r"\d", "#", flat)[:limit]


def _aws_fields(exc: BaseException) -> dict[str, str]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return {"code": "", "http": "", "request_id": ""}
    error = response.get("Error") if isinstance(response.get("Error"), dict) else {}
    meta = (response.get("ResponseMetadata")
            if isinstance(response.get("ResponseMetadata"), dict) else {})
    status = meta.get("HTTPStatusCode")
    return {
        "code": _token(error.get("Code")),
        "http": str(status) if isinstance(status, int) else "",
        "request_id": _token(meta.get("RequestId")),
    }


def exception_fingerprint(exc: BaseException) -> dict[str, str]:
    """Named, value-free description of *exc* (see the module docstring)."""
    try:
        fields = {"exc_type": _type_name(exc), "cause": _cause_chain(exc),
                  "where": _frames(exc)}
        fields.update(_aws_fields(exc))
        fields["head"] = masked_head(exc)
        return fields
    except Exception:  # noqa: BLE001 - a fingerprint never raises
        return {"exc_type": "unknown", "cause": "none", "where": "unknown",
                "code": "", "http": "", "request_id": "", "head": ""}


def frame_label(data: object) -> str:
    """Allowlisted shape of one parsed stream frame, e.g. ``event.contentBlockDelta``."""
    if not isinstance(data, dict):
        return "non-dict"
    known = sorted(k for k in data if isinstance(k, str) and k in FRAME_KEYS)
    parts: list[str] = []
    for key in known:
        nested = data.get(key)
        if key == "event" and isinstance(nested, dict):
            inner = sorted(k for k in nested if isinstance(k, str) and k in EVENT_KEYS)
            parts.append("event." + "|".join(inner) if inner else "event")
        else:
            parts.append(key)
    if len(known) < len(data):
        parts.append("other")
    return "+".join(parts) or "empty"


def format_fields(fields: dict[str, object], tail: str = "head") -> str:
    """``k=v`` pairs on one line; an empty value renders as ``-``.

    Whitespace inside a value becomes ``_`` so every pair stays one token, except
    the *tail* key, which is rendered LAST and as-is (it is already one line):
    a message head reads better with its spaces.
    """
    out = []
    for key, value in fields.items():
        if key == tail:
            continue
        text = re.sub(r"\s+", "_", str(value)) if value not in (None, "") else "-"
        out.append(f"{key}={text}")
    if tail in fields:
        out.append(f"{tail}={fields[tail] if fields[tail] not in (None, '') else '-'}")
    return " ".join(out)
