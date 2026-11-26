# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the ERP simulator dashboard branding helper.

The Streamlit header used to hard-code a beverage company name while the
workshop story is precision machining (test event 18, 2026-09-18). The title
is now neutral by default and overridable through ``ERP_DASHBOARD_TITLE`` so
a deployment can brand it without touching seed data. Pure helper — no
Streamlit import, same lane as the sibling tests:

    python -m pytest erp_simulation/tests -q
"""
from __future__ import annotations

from erp_simulation import branding


def test_default_title_is_neutral_manufacturing_and_ascii(monkeypatch):
    monkeypatch.delenv(branding.TITLE_ENV_VAR, raising=False)
    title = branding.dashboard_title()
    assert title == "AnyCompany Manufacturing - ERP Simulator"
    assert title.isascii()
    assert "Beverage" not in title


def test_env_var_overrides_the_title(monkeypatch):
    monkeypatch.setenv(branding.TITLE_ENV_VAR, "  Example Corp - Plant ERP  ")
    assert branding.dashboard_title() == "Example Corp - Plant ERP"


def test_blank_or_oversized_override_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(branding.TITLE_ENV_VAR, "   ")
    assert branding.dashboard_title() == branding.DEFAULT_TITLE
    monkeypatch.setenv(branding.TITLE_ENV_VAR, "x" * (branding.TITLE_MAX_CHARS + 1))
    assert branding.dashboard_title() == branding.DEFAULT_TITLE


def test_page_title_and_header_derive_from_one_title(monkeypatch):
    monkeypatch.setenv(branding.TITLE_ENV_VAR, "Example Corp")
    assert branding.page_title() == "Example Corp Dashboard"
    assert branding.header_title().endswith("Example Corp")


def test_dashboard_module_has_no_beverage_literal():
    from pathlib import Path
    source = Path(branding.__file__).with_name("streamlit_dashboard.py").read_text(encoding="utf-8")
    assert "AnyCompany Beverage" not in source
