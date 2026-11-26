# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""HighByte as a REGISTER-ONLY workshop system.

When the workshop package ships HighByte (the live package: the hub in the
stack, the MCP key minted into ``adt/conn/highbyte``, ``/adt/highbyte-url`` set
to the hub endpoint), the Onboard view must show a HighByte card the attendee
can click once to run the existing built-in Unified Namespace onboarding. When
HighByte is NOT shipped (``_get_highbyte_url()`` empty / "placeholder"),
NOTHING changes: the registered set, the SiteWise guidance and the manifest are
byte-identical to today.

The shipped SIGNAL is the one the app already reads at registration:
``catalog._get_highbyte_url()`` returns a non-empty, non-placeholder URL
(env ``HIGHBYTE_URL`` → SSM ``/adt/highbyte-url``). No new env/SSM is added.

Routing (step 2): once HighByte is registered the SiteWise guidance must not
claim SiteWise is the ONLY sensor source, and must still send trend/history
('is vibration trending up') questions to SiteWise. HighByte's own guidance
scopes it to the instantaneous current value and defers trends to SiteWise.
Both are the systems' OWN registration prose — no per-turn app logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.demo import workshop  # noqa: E402
from agents.demo.systems import DEMO_SYSTEMS, manifest_systems  # noqa: E402

_HB_URL = "https://hb.example.com"


@pytest.fixture
def shipped(monkeypatch):
    """Workshop live, HighByte shipped: HIGHBYTE_URL env set to a real URL
    (``_get_highbyte_url`` strips it and returns it, so SSM is never touched)."""
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.setenv("ADT_WORKSHOP_STORY", "live")
    monkeypatch.setenv("HIGHBYTE_URL", _HB_URL)


@pytest.fixture
def not_shipped(monkeypatch):
    """Workshop live, HighByte NOT shipped: the placeholder sentinel that
    deploy.sh seeds when --highbyte was never passed → ``_get_highbyte_url`` ''."""
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.setenv("ADT_WORKSHOP_STORY", "live")
    monkeypatch.setenv("HIGHBYTE_URL", "placeholder")


# ---------------------------------------------------------------------------
# The shipped signal
# ---------------------------------------------------------------------------

def test_highbyte_shipped_reads_the_existing_url_signal(monkeypatch):
    monkeypatch.setenv("HIGHBYTE_URL", _HB_URL)
    assert workshop.highbyte_shipped() is True
    monkeypatch.setenv("HIGHBYTE_URL", "placeholder")
    assert workshop.highbyte_shipped() is False
    monkeypatch.setenv("HIGHBYTE_URL", "")
    assert workshop.highbyte_shipped() is False


class _SSM:
    def __init__(self, value):
        self.value = value

    def get_parameter(self, Name):
        assert Name == "/adt/highbyte-url"
        if isinstance(self.value, Exception):
            raise self.value
        return {"Parameter": {"Value": self.value}}


@pytest.mark.parametrize("ssm_value", ["pending", RuntimeError("no network")])
def test_pending_means_shipped_before_the_url_is_written(monkeypatch, ssm_value):
    """The workshop root starts the app while the hub's stack is still being
    created: /adt/highbyte-url holds "pending" (shipped, URL on its way), so the
    startup registration still registers the HighByte card, and no caller gets
    "pending" as a URL (workshop_infra/quicklink_writer_root.py)."""
    from unittest.mock import patch
    from agents import catalog
    monkeypatch.setenv("HIGHBYTE_URL", "pending")
    with patch("boto3.client", lambda *a, **kw: _SSM(ssm_value)):
        assert workshop.highbyte_shipped() is True
        assert catalog._get_highbyte_url() == ""
        assert "highbyte" in [s["id"] for s in workshop.registered_systems("live")]


def test_pending_registration_reads_no_ssm(monkeypatch):
    """Startup registration (manifest_systems, inside the app's lifespan) with
    a task-start "pending" must not touch SSM: a slow or unreachable SSM would
    otherwise hold the app's health check."""
    from unittest.mock import patch
    from agents.demo.systems import manifest_systems

    created = []

    def no_client(*a, **_kw):
        created.append(a)  # recorded: the fallback swallows any exception
        raise AssertionError("registration must not create an SSM client")

    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.setenv("ADT_WORKSHOP_STORY", "live")
    for value in ("pending", "Pending", _HB_URL):
        monkeypatch.setenv("HIGHBYTE_URL", value)
        with patch("boto3.client", no_client):
            assert workshop.highbyte_shipped() is True, value
            assert "highbyte" in [s["id"] for s in manifest_systems()], value
    assert created == []


def test_onboarding_url_read_is_one_short_request(monkeypatch):
    """Onboarding's HighByte URL read (a sentinel at task start) sends one
    request with short timeouts: botocore's max_attempts=1 would send two.
    Counted on a real client whose send is a connect timeout (no network)."""
    import boto3 as real_boto3
    import botocore.exceptions as be
    from unittest.mock import patch
    from agents import catalog
    sent, configs = [], []

    def client(service, **kw):
        configs.append(kw.get("config"))
        c = real_boto3.session.Session(
            aws_access_key_id="x", aws_secret_access_key="y",  # nosec B106 - fake, never sent
        ).client(service, **kw)

        def timeout(**_kw):
            sent.append(1)
            raise be.ConnectTimeoutError(endpoint_url="https://ssm.example.invalid")

        c.meta.events.register("before-send.ssm", timeout)
        return c

    monkeypatch.setenv("HIGHBYTE_URL", "pending")
    with patch("boto3.client", client):
        assert catalog._get_highbyte_url() == ""
    assert sent == [1]
    (config,) = configs
    assert config.connect_timeout == 2 and config.read_timeout == 2


def test_pending_task_start_value_reads_the_written_url(monkeypatch):
    from unittest.mock import patch
    from agents import catalog
    monkeypatch.setenv("HIGHBYTE_URL", "pending")
    with patch("boto3.client", lambda *a, **kw: _SSM(_HB_URL + "/")):
        assert catalog._get_highbyte_url() == _HB_URL
        assert workshop.highbyte_shipped() is True


def test_placeholder_in_ssm_is_still_not_shipped(monkeypatch):
    from unittest.mock import patch
    monkeypatch.setenv("HIGHBYTE_URL", "placeholder")
    with patch("boto3.client", lambda *a, **kw: _SSM("placeholder")):
        assert workshop.highbyte_shipped() is False


# ---------------------------------------------------------------------------
# Step 1 — the register-only HighByte entry appears only when shipped
# ---------------------------------------------------------------------------

def test_highbyte_entry_is_a_register_only_mcp_system():
    hb = workshop.HIGHBYTE_SYSTEM
    assert hb["id"] == "highbyte"
    assert hb["connection_type"] == "MCP Server"
    assert hb["schema"] == ""
    assert hb["capabilities"] and hb["query_guidance"]
    # Never bulk-onboarded, in BOTH modes (it is the attendee's module-6 click).
    assert "highbyte" in workshop.ONBOARD_EXCLUDE


@pytest.mark.parametrize("story", ["frozen", "live"])
def test_registered_systems_appends_highbyte_only_when_shipped(story):
    without = [s["id"] for s in workshop.registered_systems(story, highbyte_shipped=False)]
    assert "highbyte" not in without
    with_hb = [s["id"] for s in workshop.registered_systems(story, highbyte_shipped=True)]
    assert "highbyte" in with_hb
    # The only addition is highbyte — every other id and its order is preserved.
    assert [s for s in with_hb if s != "highbyte"] == without


def test_registered_systems_auto_detects_the_signal(shipped):
    ids = [s["id"] for s in workshop.registered_systems()]
    assert "highbyte" in ids


def test_registered_systems_unchanged_when_not_shipped(not_shipped):
    ids = [s["id"] for s in workshop.registered_systems()]
    assert "highbyte" not in ids
    assert ids == [s["id"] for s in workshop.registered_systems("live", highbyte_shipped=False)]


def test_manifest_registers_highbyte_card_only_when_shipped(shipped, monkeypatch):
    ids = [s["id"] for s in manifest_systems()]
    assert ids[: len(DEMO_SYSTEMS)] == [s["id"] for s in DEMO_SYSTEMS]
    assert "highbyte" in ids
    # Not shipped → identical to the pre-HighByte manifest.
    monkeypatch.setenv("HIGHBYTE_URL", "placeholder")
    ids2 = [s["id"] for s in manifest_systems()]
    assert "highbyte" not in ids2


def test_manifest_byte_identical_when_not_shipped(not_shipped):
    """The register-only HighByte feature is inert without the signal: the
    manifest equals the demo six + the live story systems + cmms-ind, exactly
    as before this change."""
    expected = DEMO_SYSTEMS + workshop.registered_systems("live", highbyte_shipped=False)
    assert manifest_systems() == expected


# ---------------------------------------------------------------------------
# Step 2 — routing/guidance once HighByte is registered
# ---------------------------------------------------------------------------

def _sitewise(entries):
    return next(s for s in entries if s["id"] == "sitewise-direct")


def test_sitewise_drops_the_only_claim_when_highbyte_is_registered():
    with_hb = workshop.registered_systems("live", highbyte_shipped=True)
    sw = _sitewise(with_hb)
    # No longer the "ONLY" sensor source once a second live OT source exists.
    assert "ONLY sensor source" not in sw["query_guidance"]
    assert "only sensor source" not in sw["query_guidance"].lower()
    # Trend/history questions STILL route to SiteWise (modules 5 and 6).
    assert "trending up" in sw["query_guidance"]


def test_sitewise_guidance_unchanged_without_highbyte():
    without = workshop.registered_systems("live", highbyte_shipped=False)
    assert _sitewise(without)["query_guidance"] == workshop.LIVE_SITEWISE_SYSTEM["query_guidance"]
    assert "ONLY sensor source" in _sitewise(without)["query_guidance"]


def test_highbyte_guidance_defers_trends_to_sitewise():
    hb = workshop.HIGHBYTE_SYSTEM
    g = hb["query_guidance"].lower()
    # HighByte is the instantaneous/current value; trends go to SiteWise.
    assert "sitewise" in g
    assert "current" in g or "now" in g or "instant" in g
    # Routing prose length stays bounded (the orchestrator route-seed budget).
    assert len(hb["query_guidance"]) < 400


# ---------------------------------------------------------------------------
# Step 4 — the discovery/onboarding MCP path opens a fresh session per call
# ---------------------------------------------------------------------------

def test_discover_mcp_opens_a_fresh_session_per_operation(monkeypatch):
    """``_discover_mcp`` opens a fresh httpx.Client and a fresh ``initialize``
    for each operation (the tool list, and every tool sample), so no session
    is held across the hub's 2-hour trial reset. Checked by what the connector
    sends: every client's first request is ``initialize``, and there is one
    client for the list plus one per sampled tool."""
    import json

    import httpx
    from agents import onboard_agent

    tools = [{"name": "tool_a", "inputSchema": {"type": "object", "properties": {}}},
             {"name": "tool_b", "inputSchema": {"type": "object", "properties": {}}}]
    clients: list = []

    def _handle(request):
        body = json.loads(request.content)
        clients[-1].append(body.get("method"))
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                                  headers={"mcp-session-id": f"s-{len(clients)}"})
        if body.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": tools}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps({"v": 1})}]}})

    real_client = httpx.Client

    class _Client(real_client):
        def __init__(self, *a, **kw):
            clients.append([])
            kw["transport"] = httpx.MockTransport(_handle)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "Client", _Client)
    found = onboard_agent._discover_mcp(
        {"connection_string": "https://hub.example.com/mcp",
         "connection_config": {"token": "k", "auth_scheme": "bearer"}})
    assert "error" not in found, found
    assert [t["name"] for t in found["tables"]] == ["tool_a", "tool_b"]
    assert clients == [["initialize", "tools/list"],
                       ["initialize", "tools/call"],
                       ["initialize", "tools/call"]]
