# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure i3X 1.0 envelope/shape helpers — the ONE importable home.

Shared by the onboarding walker/discovery (``agents/onboard.py``,
``agents/onboard_agent.py``) and the system agent's i3X tools
(``agents/system_agent.py``). Everything here is pure (no I/O, no graph, no
LLM) except :func:`i3x_tls_verify`, which reads a single env toggle.

i3X 1.0 wire shapes handled here:

* **Success envelope** — a single result is ``{"success": true, "result":
  <object>}``; a bulk result is ``{"success": true, "results": [<entry>,
  ...]}``; an error is ``{"success": false, "responseDetail": {...}}``.
  :func:`i3x_unwrap` strips the envelope and RAISES on the error shape so
  callers' non-fatal try/except blocks log and continue.
* **Double-wrapped relationship responses** — ``POST /v1/objects/related``
  responses are doubly wrapped: the outer ``{"success", "results"}`` envelope
  (stripped by :func:`i3x_unwrap`) still contains per-elementId wrappers, each
  with an inner ``result`` list of ``{"sourceRelationship", "object"}``
  entries. :func:`flatten_related_entries` flattens to those entries and
  tolerates the alternate already-flat shape some servers emit.
* **Property vs composition children** — a HasChildren entry is either a
  composition asset (a real ObjectType) or a property child (the telemetry
  holder). :func:`is_property_child` applies the spec-authoritative
  ``isComposition`` flag with the property-URN ``typeElementId`` fallback.
* **Scalar property-container types** — synthetic ObjectTypes registered so
  every property has a ``typeElementId`` (e.g. the ``DOUBLE`` container).
  :func:`is_i3x_scalar_type` recognises both the bare form and the URN form.
"""
from __future__ import annotations

import os

# Synthetic property-type ObjectTypes registered by the SiteWise adapter to
# satisfy the "every property must have a typeElementId pointing at an
# ObjectType" conformance rule. They aren't real asset models, just the scalar
# containers (a property of type DOUBLE points at the DOUBLE ObjectType).
# The AWS i3X reference server surfaces these with full URIs like
# ``urn:aws:iotsitewise:property:DOUBLE``; other servers may emit just the
# scalar literal (``DOUBLE``). Both forms are accepted.
I3X_SCALAR_TYPE_IDS = {"DOUBLE", "INTEGER", "BOOLEAN", "STRING", "STRUCT"}
I3X_SCALAR_TYPE_PREFIX = "urn:aws:iotsitewise:property:"


def i3x_tls_verify() -> bool:
    """TLS-verification toggle for all outbound i3X HTTP calls.

    ``I3X_TLS_VERIFY`` env, default ON. Deployments that serve HTTPS with a
    self-signed/imported certificate set it to ``false``; without the toggle
    the graph walker and data-model discovery fail certificate validation and
    silently skip Asset/Property provisioning.

    Blast radius (security review): the toggle is PROCESS-GLOBAL — ``false``
    disables certificate verification for EVERY i3X connection this runtime
    makes, not per-system. Design-accepted for the demo (default ON, explicit
    opt-out, SSM-seeded ``"true"``); a per-connection ``tls_verify`` flag is
    the follow-up if mixed-trust i3X servers ever share a runtime.
    """
    return os.environ.get("I3X_TLS_VERIFY", "true").lower() != "false"


def i3x_unwrap(payload):
    """Strip the i3X 1.0 success envelope; raise on an error envelope.

    Single result : ``{"success": true, "result": <object>}``
    Bulk result   : ``{"success": true, "results": [<entry>, ...]}``
    Error envelope: ``{"success": false, "responseDetail": {...}}`` → RAISES
    Anything else passes through unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    if payload.get("success") is False:
        rd = payload.get("responseDetail") or {}
        raise RuntimeError(
            f"i3X error: {rd.get('title', 'Error')} "
            f"({rd.get('status', '')}): {rd.get('detail', '')}"
        )
    if "result" in payload and "success" in payload:
        return payload["result"]
    if "results" in payload and "success" in payload:
        return payload["results"]
    return payload


def flatten_related_entries(related) -> list:
    """Flatten an unwrapped ``/objects/related`` response to relationship entries.

    The response is doubly wrapped: :func:`i3x_unwrap` strips only the
    outermost ``{"success", "results"}`` envelope, so the caller still holds a
    list of per-elementId wrappers, each with an inner ``result`` list::

        [{"elementId": <parent>, "result": [
            {"sourceRelationship": "HasChildren", "object": {...child...}},
            ...
        ]}]

    Returns the flat list of ``{sourceRelationship, object}`` entries.
    Tolerates the alternate already-flat shape some servers emit (the wrapper
    IS the relationship entry). Non-list/non-dict noise is dropped.
    """
    if not isinstance(related, list):
        return []
    entries: list = []
    for wrapper in related:
        if not isinstance(wrapper, dict):
            continue
        # Doubly-wrapped form: per-elementId wrapper with an inner "result" list.
        inner = wrapper.get("result")
        if isinstance(inner, list):
            entries.extend(e for e in inner if isinstance(e, dict))
            continue
        # Already-flat form: the wrapper IS the relationship entry.
        if "object" in wrapper or "sourceRelationship" in wrapper:
            entries.append(wrapper)
    return entries


def is_property_child(obj: dict) -> bool:
    """True if this HasChildren entry is a measurement, not a composition asset.

    Per the i3X spec the authoritative signal is ``isComposition``:
    composition assets are ``isComposition=true``, measurements are
    ``isComposition=false``. Fall back to the property-URN ``typeElementId``
    shape only when a server omits the flag (older / non-conformant
    implementations).
    """
    ic = obj.get("isComposition")
    if isinstance(ic, bool):
        return ic is False
    tid = (obj.get("typeElementId") or "").lower()
    return tid.startswith(I3X_SCALAR_TYPE_PREFIX)


def is_i3x_scalar_type(type_id: str) -> bool:
    """True if this ObjectType id is a synthetic scalar property container,
    not a real asset model a user would map to manufacturing concepts.
    Recognises both bare names (``DOUBLE``) and the SiteWise URN form
    (``urn:aws:iotsitewise:property:DOUBLE``)."""
    if not type_id:
        return False
    if type_id in I3X_SCALAR_TYPE_IDS:
        return True
    return type_id.startswith(I3X_SCALAR_TYPE_PREFIX)


def i3x_auth_headers(token: str, auth_scheme: str) -> dict:
    """Authorization header for an i3X server (empty dict when no token)."""
    if not token:
        return {}
    return {"Authorization": token if auth_scheme == "bearer_literal"
            else f"Bearer {token}"}


def i3x_api_base(base_url: str) -> str:
    """Canonical i3X API base: ``<server>/v1`` exactly once.

    The i3X 1.0 API is versioned under ``/v1``. Operators supply the server
    base URL with OR without the ``/v1`` suffix (the UI connect field, the
    demo SSM param, and registered records have carried both forms) — both
    clients (:func:`make_i3x_client` and the system agent's ``_I3xClient``)
    normalize through THIS helper so the two transports cannot drift (a
    verbatim base_url 404'd every runtime tool call while the walker, which
    appended ``/v1``, succeeded against the same record).
    """
    base = (base_url or "").rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def make_i3x_client(base_url: str, token: str = "", auth_scheme: str = "bearer"):  # nosec B107 - empty default sentinel; real value supplied by caller
    """Return ``(get, post)`` callables for an i3X 1.0 server.

    The ONE shared transport for the onboarding walker and the data-model
    discovery: ``/v1`` prefixing, auth header, the ``I3X_TLS_VERIFY`` toggle,
    ``raise_for_status``, and the :func:`i3x_unwrap` envelope strip (so an
    error envelope RAISES into the caller's non-fatal try/except). Requests go
    through ``pinned_http`` (the validated address, no redirects), imported
    lazily so importing this module never requires httpx.
    """
    from . import pinned_http

    api_base = i3x_api_base(base_url)
    headers = i3x_auth_headers(token, auth_scheme)
    verify = i3x_tls_verify()

    def _get(path, **params):
        r = pinned_http.get(api_base + path, connection_type="SiteWise (i3X)",
                            headers=headers,
                            params={k: v for k, v in params.items() if v is not None},
                            timeout=30.0, verify=verify)
        r.raise_for_status()
        return i3x_unwrap(r.json())

    def _post(path, body):
        r = pinned_http.post(api_base + path, connection_type="SiteWise (i3X)",
                             headers=headers, json=body, timeout=30.0, verify=verify)
        r.raise_for_status()
        return i3x_unwrap(r.json())

    return _get, _post
