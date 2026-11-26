# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Preflight samples a REST vendor through its own discovery.

Before, every REST source was sampled with the generic OpenAPI parse, which a
Tulip workspace (no consolidated spec) can never satisfy. ``_pf_rest`` now asks
the vendor registry; only the generic OpenAPI adapter and SiteWise (i3X) keep
the OpenAPI parse. No network: the discovery functions are replaced.
"""
from __future__ import annotations

import time

import pytest

import agents.onboard_agent as onboard_agent
import agents.onboard_preflight as op
from agents import connection_types as ct

SID = "mes-t"
TABLES = {"tables": [{"name": "Work Orders",
                      "columns": [{"name": "id", "data_type": "text"},
                                  {"name": "status", "data_type": "text"}]}]}


def _deadline():
    return time.monotonic() + 30


def _no_openapi(entry):
    raise AssertionError("the generic OpenAPI parse must not sample this vendor")


def test_tulip_is_sampled_by_tulip_discovery(monkeypatch):
    calls = []

    def fake_tulip(entry, resolved):
        calls.append((entry, resolved.get("base_url")))
        return TABLES

    monkeypatch.setattr(onboard_agent, "_discover_tulip", fake_tulip)
    monkeypatch.setattr(onboard_agent, "_discover_openapi", _no_openapi)
    resolved = {"connection_type": ct.TULIP, "base_url": "https://tenant.example.com",
                "token": "t"}
    tables, completeness = op._pf_rest(resolved, SID, _deadline())
    assert calls == [({"id": SID}, "https://tenant.example.com")]
    assert tables == [{"name": "endpoints", "sample_read": "ok", "detail": ""}]
    assert completeness  # column counts come from the vendor's tables


def test_tulip_discovery_error_is_a_failed_sample_not_a_pass(monkeypatch):
    monkeypatch.setattr(onboard_agent, "_discover_tulip",
                        lambda entry, resolved: {"error": "401 Unauthorized"})
    monkeypatch.setattr(onboard_agent, "_discover_openapi", _no_openapi)
    tables, _ = op._pf_rest({"connection_type": ct.TULIP,
                             "base_url": "https://tenant.example.com"}, SID, _deadline())
    assert tables[0]["sample_read"] == "failed", tables


@pytest.mark.parametrize("ctype", [ct.REST_API, ct.SITEWISE_I3X])
def test_generic_rest_and_i3x_keep_the_openapi_parse(monkeypatch, ctype):
    seen = []

    def fake_openapi(entry):
        seen.append(entry["connection_config"].get("base_url"))
        return TABLES

    monkeypatch.setattr(onboard_agent, "_discover_openapi", fake_openapi)
    monkeypatch.setattr(onboard_agent, "_discover_tulip",
                        lambda *a: (_ for _ in ()).throw(AssertionError("not tulip")))
    tables, _ = op._pf_rest({"connection_type": ctype,
                             "base_url": "https://api.example.com"}, SID, _deadline())
    assert seen == ["https://api.example.com"]
    assert tables[0]["sample_read"] == "ok", tables
