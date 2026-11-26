# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo status route (ADR 0019 — demo-as-addon).

Exposes a single read-only endpoint, ``GET /api/demo/status``, that reports
whether the demo addon is currently enabled. The route is **session-authed**:
it is neither public nor under ``/api/admin``, so ``AuthMiddleware`` requires a
valid session for any logged-in user (admin not required). It is read-only — it
never mutates demo state. The dashboard uses it to show/hide the ``▶ Demo``
button; toggling the flag is the admin-gated ``PUT /api/admin/settings``.

**Workshop mode hides the button without disabling the addon.** ``--workshop``
implies ``--simulator`` (so ``ADT_DEMO`` is on), but the ``▶ Demo`` button is a
kiosk driver: it auto-plays the curated prompt pack through the attendee's chat
and starts new sessions between prompts, which hijacks the module storyline.
The status therefore reports ``enabled`` only when the demo addon is on AND
workshop mode is off. Demo system registration and the prompt pack — which the
workshop seed relies on — stay untouched; ``demo.is_enabled()`` is unchanged.
"""

from fastapi import APIRouter

from . import demo
from .demo import workshop

router = APIRouter(prefix="/api/demo")


@router.get("/status")
async def demo_status():
    """Return whether the ``▶ Demo`` kiosk button should be shown, and whether
    workshop mode is on (the UI calls the workshop story routes only then)."""
    return {"enabled": demo.is_enabled() and not workshop.is_enabled(),
            "workshop": workshop.is_enabled()}
