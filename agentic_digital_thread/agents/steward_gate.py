# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Router-importable steward gate (ADR-0050 shape, ADR-0076 D2).

``server._check_steward`` lives in the app module, which router modules
cannot import (import cycle). This is the SAME fail-closed check for routes
declared on an ``APIRouter``: wire it as a registration dependency
(``dependencies=[Depends(check_steward)]``). ``AuthMiddleware`` stamps
``request.state.is_admin`` on every request that reaches a route; an ABSENT
verdict is denied exactly like an explicit non-admin one.
"""
from __future__ import annotations

from fastapi import HTTPException, Request


def is_steward(request: Request) -> bool:
    """The same fail-closed verdict as :func:`check_steward`, as a boolean, for
    an open route whose WRITE differs by role (a steward's assertion applies at
    once, anyone else's waits for steward review). Absent means not a steward."""
    return bool(getattr(request.state, "is_admin", False))


def check_steward(request: Request) -> None:
    """403 unless the request carries an affirmative steward (admin) verdict."""
    if not is_steward(request):
        raise HTTPException(status_code=403,
                            detail="Steward (admin) role required")
