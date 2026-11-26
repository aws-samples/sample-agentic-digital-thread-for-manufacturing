# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Token-forward plumbing — app → orchestrator runtime (ADR-0033, Wave 1).

Carries the interactive user's ``adt-app`` access token from the ADT app to the
orchestrator runtime via the ``invoke_agent_runtime`` **payload** field
``userAccessToken``.

Why the payload (Wave 0 transport decision 0.2): the app→runtime hop is
SigV4/IAM (``AgentCoreChatClient`` signs ``invoke_agent_runtime`` with the ECS
task role) and the orchestrator runtime's inbound auth is IAM, so there is **no
inbound user bearer** for the entrypoint to read — the runtime sees only the
JSON payload. The user token therefore rides in the payload, and Wave 3 will
forward it to the gateway as ``Authorization: Bearer <token>`` on the gateway
MCP client.

**INERT in Wave 1:** the field is plumbed end-to-end (app → payload → runtime →
``build_orchestrator``) but is **not yet consumed** for gateway auth — the
orchestrator's gateway client stays on SigV4 until the Wave 3 cutover.

**No fallback (project steering):** callers without a user token — warmup,
scheduler, briefings, the sync/Quick service path, and the DEV_NO_AUTH local
bypass — simply **omit** the field. Nothing is substituted, so those payloads
are byte-identical to today. The token is never logged or persisted (R4): keep
it out of stream-capture files and the memory store.
"""

from __future__ import annotations

from typing import Any, Optional

# The invoke payload key the orchestrator runtime reads (runtime_app.invoke).
USER_ACCESS_TOKEN_FIELD = "userAccessToken"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture

# ADR-0079: RESERVED invoke-payload key carrying the VERIFIED delegated
# subject of a Quick-gateway async ask (the app verified the interceptor-minted
# claim). The orchestrator runtime pops it unconditionally and mints the
# ADR-0049 claim per tool call from it; it is a subject string, never a token.
DELEGATED_SUBJECT_FIELD = "delegatedSubject"  # nosec B105 - payload key NAME, not a credential


def user_access_token_from_session(session: Optional[dict]) -> Optional[str]:
    """Return the ``adt-app`` access token from a validated app session, or None.

    ``session`` is the dict the auth middleware stores on
    ``request.state.session`` (built in ``auth/routes.py`` /
    ``auth/middleware.py``). Returns ``None`` when there is no session or no
    token — e.g. the DEV_NO_AUTH local bypass stores ``{}``. No fallback: a
    missing token yields ``None`` (the caller omits the field), never a
    substitute principal.
    """
    if not session:
        return None
    return session.get("access_token")


def with_user_access_token(payload: dict[str, Any], token: Optional[str]) -> dict[str, Any]:
    """Return a copy of ``payload`` with the user access token attached when present.

    Additive and inert: when ``token`` is falsy the payload is returned unchanged
    (no field, no placeholder) so service/background/dev paths produce a
    byte-identical payload to today. When present, the token is added under
    ``userAccessToken`` for the orchestrator runtime to read.
    """
    if not token:
        return dict(payload)
    return {**payload, USER_ACCESS_TOKEN_FIELD: token}
