# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ERP simulator dashboard branding — neutral by default, env-overridable.

The Streamlit header is the one place the simulator names a company. It used
to hard-code a beverage brand while the workshop story is precision machining
(ADR-0065, 2026-09-18). The default is now industry-neutral and a deployment
can rebrand through ``ERP_DASHBOARD_TITLE`` (container env) without touching
seed data. Pure module — importable without Streamlit so it stays testable in
the ``erp_simulation/tests`` lane.
"""
from __future__ import annotations

import os

TITLE_ENV_VAR = "ERP_DASHBOARD_TITLE"
DEFAULT_TITLE = "AnyCompany Manufacturing - ERP Simulator"
#: Bound on an operator-supplied title (a header, not a paragraph).
TITLE_MAX_CHARS = 80
HEADER_ICON = "\U0001f3ed"  # factory


def dashboard_title() -> str:
    """The configured title, else ``DEFAULT_TITLE``.

    Blank and oversized overrides fall back to the default so a misconfigured
    environment never renders an empty or runaway header."""
    raw = os.environ.get(TITLE_ENV_VAR, "")
    title = raw.strip() if isinstance(raw, str) else ""
    if not title or len(title) > TITLE_MAX_CHARS:
        return DEFAULT_TITLE
    return title


def page_title() -> str:
    """Browser tab title (``st.set_page_config(page_title=...)``)."""
    return f"{dashboard_title()} Dashboard"


def header_title() -> str:
    """In-page header (``st.title``)."""
    return f"{HEADER_ICON} {dashboard_title()}"
