# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``ProvisionRequest.rule_delete_scope`` own: a provision deletes only the
onboarding rules its own system wrote.

The default replace (``_delete_stale_onboarding_rule_artifacts``) deletes
every onboarding rule touching the system, a peer's rules pointing at it
included. The workshop seed's coordinated phase 2 starts every re-provision
back to back, but each POST only starts a background job, so a job whose
delete lands late removes rules a peer already wrote to its system. Only
that seed sends ``own``; every other caller keeps the default ``all``.
"""
from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402


def _body(**kwargs):
    return ProvisionRequest(system_id="erp", schema="erp", connection_type="PostgreSQL",
                            mappings=[{"table": "t", "field": "t.k", "concept": "batch"}],
                            rules=[], **kwargs)


class _RuleGraph:
    """IdentityRules and JoinPaths as rows, with the own-only delete
    statements evaluated on them (their WHERE clauses, as written)."""

    def __init__(self, rows):
        self.rows = list(rows)

    def write(self, cypher, params=None):
        sid = (params or {})["sid"]
        if cypher == onboard._OWN_ONBOARDING_RULE_DELETE_CYPHER:
            self.rows = [r for r in self.rows if not (
                r["label"] == "IdentityRule" and r["src"] == sid and r["id"].startswith("ir-")
                and r["method"] not in ("user_confirmed", onboard.USER_UNVERIFIED_METHOD))]
        elif cypher == onboard._OWN_ONBOARDING_JOINPATH_DELETE_CYPHER:
            self.rows = [r for r in self.rows if not (
                r["label"] == "JoinPath" and r["src"] == sid
                and r["id"].startswith("jp-") and r["method"] != "user_confirmed")]
        else:
            raise AssertionError(cypher)
        return True


def _rule(label, rid, src, tgt, method=""):
    return {"label": label, "id": rid, "src": src, "tgt": tgt, "method": method}


def test_every_caller_but_the_seed_gets_the_delete_both_ways_by_default():
    assert _body().rule_delete_scope == "all"
    assert "rule_delete_scope" not in _body().model_dump(exclude_unset=True)


def test_the_scope_takes_only_own_or_all():
    assert _body(rule_delete_scope="own").rule_delete_scope == "own"
    with pytest.raises(ValidationError):
        _body(rule_delete_scope="some")


def test_the_own_delete_keeps_every_row_another_system_writes(monkeypatch):
    rows = [_rule("IdentityRule", "ir-erp-1", "erp", "plm"),
            _rule("IdentityRule", "ir-erp-self", "erp", "erp"),
            _rule("IdentityRule", "ir-plm-1", "plm", "erp"),            # the peer's rule to erp
            _rule("IdentityRule", "ir-erp-2", "erp", "plm", "user_confirmed"),
            _rule("IdentityRule", "ir-learned-erp", "erp", "plm", onboard.USER_UNVERIFIED_METHOD),
            _rule("IdentityRule", "subj-erp-3", "erp", "plm"),
            _rule("JoinPath", "jp-erp-1", "erp", "plm"),
            _rule("JoinPath", "jp-erp-self", "erp", "erp"),             # its own, as before
            _rule("JoinPath", "jp-plm-1", "plm", "erp"),                # the peer's join to erp
            _rule("JoinPath", "jp-erp-4", "erp", "plm", "user_confirmed")]
    graph = _RuleGraph(rows)
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    onboard._delete_own_onboarding_rule_artifacts("erp")
    assert [r["id"] for r in graph.rows] == ["ir-plm-1", "ir-erp-2", "ir-learned-erp",
                                             "subj-erp-3", "jp-plm-1", "jp-erp-4"]


def test_the_default_delete_still_removes_the_peers_rules_that_point_at_the_system():
    """``scripts/reonboard.py`` and every attendee-facing provision rely on it."""
    assert "ir.source_system = $sid OR ir.target_system = $sid" in onboard._ONBOARDING_RULE_WHERE
    assert "jp.from_system = $sid OR jp.to_system = $sid" in onboard._ONBOARDING_JOINPATH_WHERE
    assert "target_system" not in onboard._OWN_ONBOARDING_RULE_DELETE_CYPHER
    assert "to_system" not in onboard._OWN_ONBOARDING_JOINPATH_DELETE_CYPHER


def test_a_failed_own_delete_fails_the_provision(monkeypatch):
    monkeypatch.setattr(onboard, "neptune_write", lambda cypher, params=None: False)
    with pytest.raises(RuntimeError, match="stale onboarding IdentityRule cleanup failed"):
        onboard._delete_own_onboarding_rule_artifacts("erp")


def test_a_provision_with_an_own_scope_keeps_the_peers_rules(monkeypatch):
    rows = [_rule("IdentityRule", "ir-erp-1", "erp", "plm"),
            _rule("IdentityRule", "ir-plm-1", "plm", "erp"),
            _rule("JoinPath", "jp-plm-1", "plm", "erp")]
    graph = _RuleGraph(rows)
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    token = onboard._OWN_RULE_DELETE.set(True)
    try:
        onboard._delete_stale_onboarding_rule_artifacts("erp")
    finally:
        onboard._OWN_RULE_DELETE.reset(token)
    assert [r["id"] for r in graph.rows] == ["ir-plm-1", "jp-plm-1"]


def test_without_the_scope_the_delete_runs_both_ways(monkeypatch):
    sent = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: sent.append(c) or True)
    monkeypatch.setattr(onboard, "_log_learned_rows", lambda *a, **kw: None)
    onboard._delete_stale_onboarding_rule_artifacts("erp")
    assert sent == [onboard._ONBOARDING_RULE_DELETE_CYPHER,
                    onboard._ONBOARDING_JOINPATH_DELETE_CYPHER]


@pytest.mark.parametrize("kwargs, own", [({"rule_delete_scope": "own"}, True),
                                         ({"rule_delete_scope": "all"}, False), ({}, False)])
def test_the_request_s_scope_reaches_the_delete_and_is_reset(monkeypatch, kwargs, own):
    seen = []

    def base(body, dry_run=False):
        seen.append(onboard._OWN_RULE_DELETE.get())
        return {"error": "stopped here"}
    monkeypatch.setattr(onboard, "_do_provision_base", base)
    assert onboard._do_provision_writes(_body(**kwargs)) == {"error": "stopped here"}
    assert seen == [own] and onboard._OWN_RULE_DELETE.get() is False
