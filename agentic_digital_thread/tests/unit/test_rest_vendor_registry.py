# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the REST vendor-adapter registry (Model 1).

Pins the dispatch contract that replaced the scattered ``_is_tulip`` short-
circuits and the historical "REST API == i3X" grouping:

  * detection is by the DECLARED connection type:
    i3x (``SiteWise (i3X)``), tulip (``Tulip (Tables API)``), generic (a plain
    ``REST API``, last) — host-sniffing and the ``vendor: tulip`` hint are GONE;
  * ``resolve_vendor`` returns ``None`` for non-REST/i3X types (they keep their
    own dispatch);
  * i3x/tulip hooks DELEGATE to the existing factory/provision functions with
    the right args + default auth scheme (so i3X stays byte-identical);
  * the generic fallback FAILS HONESTLY (``RestVendorUnsupported``) at
    provision/runtime instead of fabricating i3X behaviour (ADR-T3).

Hermetic: no network. Delegation tests monkeypatch the target module funcs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import pytest  # noqa: E402

from agents.rest_vendor_registry import (  # noqa: E402
    REST_VENDOR_REGISTRY,
    RestVendorUnsupported,
    resolve_vendor,
)

from agents.connection_types import TULIP  # noqa: E402

I3X = "SiteWise (i3X)"
REST = "REST API"


def test_resolve_i3x_by_connection_type():
    a = resolve_vendor(I3X, "")
    assert a is not None and a.name == "i3x"
    assert a.telemetry_rich is True


def test_i3x_wins_even_with_a_tulip_host():
    # i3X is type-identified, never host-identified: an i3X system is i3x
    # regardless of URL.
    assert resolve_vendor(I3X, "https://your-instance.tulip.co").name == "i3x"


def test_resolve_tulip_by_connection_type():
    # Tulip is identified by its DECLARED type
    # string, not the host. (Full host/entry-independence, the latent-bug
    # guard, is pinned by test_tulip_resolves_by_type_on_any_host_with_or_without_entry.)
    a = resolve_vendor(TULIP, "https://mes.internal.example.com")
    assert a.name == "tulip"
    assert a.telemetry_rich is False


def test_plain_rest_on_a_tulip_host_stays_generic():
    # A *.tulip.co host on a plain "REST API" system is NO LONGER auto-
    # detected as Tulip — only the declared TULIP type routes to the tulip adapter.
    a = resolve_vendor(REST, "https://your-instance.tulip.co")
    assert a.name == "generic"


def test_resolve_generic_for_other_rest_host():
    a = resolve_vendor(REST, "https://api.example.com/openapi.json")
    assert a.name == "generic"
    assert a.telemetry_rich is False


def test_resolve_none_for_non_rest_connection_types():
    # A tulip host must NOT drag a non-REST type into the REST registry.
    for ct in ("PostgreSQL", "Athena", "SiteWise (Direct)", "MCP Server", "", "Bogus"):
        assert resolve_vendor(ct, "https://your-instance.tulip.co") is None


def test_tulip_resolves_by_type_on_any_host_with_or_without_entry():
    # Tulip identity is the DECLARED type string, so
    # resolve_vendor returns the tulip adapter on ANY host (incl. internal DNS)
    # and WITH OR WITHOUT a catalog entry. This is the fix for a latent bug:
    # provision passed the entry, the runtime dropped it, and
    # a hint-based Tulip on a non-tulip host then failed every query. Now neither
    # base_url nor entry is consulted for identity — the two can never disagree.
    from agents.connection_types import TULIP
    for host in ("https://your-instance.tulip.co", "https://tulip.internal.example.com",
                 "https://10.0.0.5:8443", ""):
        assert resolve_vendor(TULIP, host).name == "tulip"          # no entry
        assert resolve_vendor(TULIP, host, {}).name == "tulip"      # empty entry
        assert resolve_vendor(TULIP, host, None).name == "tulip"    # explicit None


# --------------------------------------------------------------------------- #
# Generic fallback fails honestly (ADR-T3)
# --------------------------------------------------------------------------- #
def test_generic_runtime_tools_raise_honest_error():
    generic = resolve_vendor(REST, "https://api.example.com")
    with pytest.raises(RestVendorUnsupported):
        generic.runtime_tools("https://api.example.com", "", "", "")


def test_generic_provision_agent_raises_honest_error():
    generic = resolve_vendor(REST, "https://api.example.com")
    with pytest.raises(RestVendorUnsupported):
        generic.provision_agent(object(), "https://api.example.com", "", "", "")


# --------------------------------------------------------------------------- #
# Runtime-tool delegation (byte-identical to the existing factories)
# --------------------------------------------------------------------------- #
def test_i3x_runtime_tools_delegate_with_bearer_default(monkeypatch):
    import agents.system_agent as sa
    monkeypatch.setattr(sa, "_make_i3x_tools", lambda *a: ("i3x-tools", a), raising=True)
    out = resolve_vendor(I3X, "https://i3x.example.com").runtime_tools(
        "https://i3x.example.com", "arn", "tok", "")
    assert out[0] == "i3x-tools"
    # empty auth_scheme falls back to the adapter default 'bearer'
    assert out[1] == ("https://i3x.example.com", "arn", "tok", "bearer")


def test_tulip_runtime_tools_delegate_with_basic_default(monkeypatch):
    import agents.system_agent as sa
    monkeypatch.setattr(sa, "_make_tulip_tools",
                        lambda *a, **kw: ("tulip-tools", a, kw), raising=True)
    # Resolved by the TULIP type; the base_url passed to runtime_tools is
    # an internal (non-tulip.co) host to prove identity no longer depends on it.
    out = resolve_vendor(TULIP, "https://mes.internal.example.com").runtime_tools(
        "https://mes.internal.example.com", "arn", "tok", "")
    assert out[0] == "tulip-tools"
    assert out[1] == ("https://mes.internal.example.com", "arn", "tok", "basic")
    # No connection_config ⇒ empty (unscoped) workspace/scope and no system id
    # or credential-source marker.
    assert out[2] == {"workspace_id": "", "key_scope": "",
                      "system_id": "", "credential_source": ""}


def test_tulip_runtime_tools_passes_workspace_from_config(monkeypatch):
    # The OPTIONAL non-secret workspace_id + key_scope thread from
    # connection_config into _make_tulip_tools so the runtime scopes correctly;
    # so do the system id and the credential-source marker, which tell the
    # client an ARN-sourced seat system from an inline one.
    import agents.system_agent as sa
    monkeypatch.setattr(sa, "_make_tulip_tools",
                        lambda *a, **kw: ("tulip-tools", a, kw), raising=True)
    out = resolve_vendor(TULIP, "https://h").runtime_tools(
        "https://h", "arn", "tok", "",
        connection_config={"base_url": "https://h", "workspace_id": "wsQ",
                           "key_scope": "instance", "system_id": "tulip-1",
                           "credential_source": "secret_arn"})
    assert out[2] == {"workspace_id": "wsQ", "key_scope": "instance",
                      "system_id": "tulip-1", "credential_source": "secret_arn"}


def test_supplied_auth_scheme_overrides_adapter_default(monkeypatch):
    import agents.system_agent as sa
    monkeypatch.setattr(sa, "_make_i3x_tools", lambda *a: a, raising=True)
    out = resolve_vendor(I3X, "https://i3x.example.com").runtime_tools(
        "https://i3x.example.com", "arn", "tok", "bearer_literal")
    assert out[3] == "bearer_literal"


# --------------------------------------------------------------------------- #
# Provision delegation (i3x URL guard + native; tulip)
# --------------------------------------------------------------------------- #
def test_i3x_provision_short_circuits_on_url_error(monkeypatch):
    import agents.onboard as ob
    monkeypatch.setattr(ob, "_dp_i3x_url_error", lambda base_url: {"error": "bad i3x url"}, raising=True)
    called = {}
    monkeypatch.setattr(ob, "_dp_agent_i3x_native",
                        lambda *a: called.setdefault("native", a), raising=True)
    res = resolve_vendor(I3X, "bad").provision_agent(object(), "bad", "", "", "")
    assert res == {"error": "bad i3x url"}
    assert "native" not in called


def test_i3x_provision_delegates_to_native(monkeypatch):
    import agents.onboard as ob
    monkeypatch.setattr(ob, "_dp_i3x_url_error", lambda base_url: None, raising=True)
    monkeypatch.setattr(ob, "_dp_agent_i3x_native",
                        lambda ctx, b, t, a, s: ("native", b, t, a, s), raising=True)
    res = resolve_vendor(I3X, "https://i3x.example.com").provision_agent(
        "CTX", "https://i3x.example.com", "tok", "bearer", "arn")
    assert res == ("native", "https://i3x.example.com", "tok", "bearer", "arn")


def test_tulip_provision_delegates(monkeypatch):
    import agents.onboard as ob
    # _dp_agent_tulip drops auth_scheme (Tulip is always Basic) — signature (ctx, base, token, secret_arn).
    monkeypatch.setattr(ob, "_dp_agent_tulip",
                        lambda ctx, b, t, s: ("tulip", b, t, s), raising=True)
    # Resolved by the TULIP type on an internal (non-tulip.co) host.
    res = resolve_vendor(TULIP, "https://mes.internal.example.com").provision_agent(
        "CTX", "https://mes.internal.example.com", "tok", "basic", "arn")
    assert res == ("tulip", "https://mes.internal.example.com", "tok", "arn")
