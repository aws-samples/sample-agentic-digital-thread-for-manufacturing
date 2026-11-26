# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How long one question may run, and how long its gateway bearer must last.

The ONE home for the app's turn ceiling. Stdlib only, no imports: both the app
(``server``, ``auth.middleware``) and the orchestrator runtime read it.

A turn's gateway bearer is chosen once, when the turn starts, and every gateway
call in the turn sends it: the user's forwarded access token on an interactive
turn (ADR-0049), the cached client-credentials token on a service turn. So a
bearer must have at least a whole turn left when the turn starts. Observed
2026-10-14: a turn that started with about 2.5 minutes left on a 60-minute
user token (outside the old 120 s leeway) lost its gateway session mid-turn.
"""
from __future__ import annotations

#: The longest a question may run. An async ask still running this long after
#: it started is abandoned and reported as an error (``server._ask_job_overdue``;
#: a real query can take 11+ minutes). An interactive turn has no cap of its
#: own; this is the length its bearer is sized to cover.
ASK_RUN_CEILING_SECONDS = 20 * 60

#: Added to the ceiling: the time from the bearer check to the turn's start,
#: plus clock skew between this host and the token issuer. It is the previous
#: refresh leeway, which covered the longest observed turn start-up.
BEARER_REFRESH_MARGIN_SECONDS = 120

#: Replace a bearer with less than this left before a turn starts with it.
BEARER_REFRESH_LEEWAY_SECONDS = ASK_RUN_CEILING_SECONDS + BEARER_REFRESH_MARGIN_SECONDS


def refresh_window(leeway: float, lifetime: "float | None") -> float:
    """The time left before expiry at which a bearer is replaced.

    ``leeway``, but at most half the token's own ``lifetime`` (``exp - iat``)
    when that is known: a token whose lifetime is no longer than the leeway
    would otherwise be inside it from the moment it is issued, and be replaced
    on every request. With 60-minute tokens and the default leeway (22 min)
    this changes nothing.
    """
    if lifetime is not None and lifetime > 0:
        return min(leeway, lifetime / 2)
    return leeway
