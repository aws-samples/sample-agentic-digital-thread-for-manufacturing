# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A resolve can leave peers out of its join sweep (``join_skip_peers``).

The workshop seed's coordinated phase 2 resolves every seeded system while
no provision runs, so the two sweeps that cover a pair (one from each side)
read the same graph and file the same proposals; the seed's
``Phase2JoinSweep`` once gives each pair to one side and the other names it
in ``join_skip_peers`` (workshop_infra/tests/test_workshop_seed_join_sweep.py
compares what both settings file). Every other caller sends nothing and
sweeps every peer, with the call into the sweep exactly as before.
"""
from types import SimpleNamespace

from agents import join_discovery as jd
from agents import onboard
from agents.models import OnboardResolveRequest


def _wire(monkeypatch, systems):
    pairs = []
    monkeypatch.setattr(jd, "_onboarded_systems", lambda: list(systems))
    monkeypatch.setattr(jd, "discover_pair",
                        lambda a, b, memo=None: pairs.append((a, b)) or {"joins": [],
                                                                         "crosswalks": []})
    return pairs


def test_the_sweep_leaves_out_only_the_named_onboarded_peers(monkeypatch):
    pairs = _wire(monkeypatch, ["a", "b", "c", "d"])
    jd.discover_joins_for_system("a", skip_peers=["c", "not-onboarded"])
    assert pairs == [("a", "b"), ("b", "a"), ("a", "d"), ("d", "a")]


def test_no_skip_list_sweeps_every_peer(monkeypatch):
    pairs = _wire(monkeypatch, ["a", "b", "c"])
    jd.discover_joins_for_system("a")
    assert pairs == [("a", "b"), ("b", "a"), ("a", "c"), ("c", "a")]


def test_a_removed_peer_stays_out_whatever_the_skip_list(monkeypatch):
    """The peer list is still the onboarded systems minus the ones being
    removed; the skip list only takes more away."""
    pairs = _wire(monkeypatch, ["a", "b"])
    jd.discover_joins_for_system("a", skip_peers=[])
    assert pairs == [("a", "b"), ("b", "a")]


def test_the_resolve_job_passes_the_request_s_skip_list(monkeypatch):
    calls = []

    def sweep(system_id, **kwargs):
        calls.append((system_id, kwargs))
        return {"joins": [], "crosswalks": []}
    monkeypatch.setattr(jd, "discover_joins_for_system", sweep)
    onboard._discover_joins_logged("j1", OnboardResolveRequest(
        system_id="erp", mappings=[], join_skip_peers=["plm", "mes-cr"]))
    onboard._discover_joins_logged("j2", OnboardResolveRequest(system_id="erp", mappings=[]))
    # Without a skip list the sweep is called exactly as before (no keyword).
    onboard._discover_joins_logged("j3", OnboardResolveRequest(
        system_id="erp", mappings=[], join_sweep_strict=True))
    assert calls == [("erp", {"skip_peers": ["plm", "mes-cr"], "strict": False}), ("erp", {}),
                     ("erp", {"skip_peers": [], "strict": True})]


def test_a_body_without_the_attribute_sweeps_every_peer(monkeypatch):
    """The chat's resolve builds its own body; one without the field is the
    call before."""
    calls = []
    monkeypatch.setattr(jd, "discover_joins_for_system",
                        lambda system_id, **kw: calls.append(kw) or {"joins": [], "crosswalks": []})
    onboard._discover_joins_logged("j", SimpleNamespace(system_id="erp"))
    assert calls == [{}]


def test_the_request_field_defaults_to_empty():
    assert OnboardResolveRequest(system_id="erp", mappings=[]).join_skip_peers == []
