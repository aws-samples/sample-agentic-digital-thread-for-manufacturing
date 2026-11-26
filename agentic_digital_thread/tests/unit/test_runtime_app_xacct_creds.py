# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression: the AgentCore QUERY runtime must merge the SECRET-only cross-account
``external_id`` into its ``connection_config`` so its SiteWise/Athena tool STS
``AssumeRole``s WITH the required ``sts:ExternalId`` — the SAME merge the in-app
dashboard path does.

ROOT CAUSE: ``runtime_app`` resolved the config from ``CONNECTION_SECRET_ARN`` ONLY
when ``CONNECTION_CONFIG`` was ENTIRELY EMPTY. For a cross-account system (e.g.
SiteWise Direct) the NON-secret config (``region``/``role_arn``) IS present, so the
config was non-empty, the secret was never fetched, and the secret-only
``external_id`` was never merged -> the role was assumed WITHOUT ``sts:ExternalId``
-> STS ``AccessDenied``. The in-app path
(``metric_resolver._full_connection_config`` ->
``resolve_evidence._with_cross_account_credentials``) ALWAYS merges it, hence the
DRY divergence.

FIX: ``runtime_app._resolve_runtime_connection_config`` (a pure, offline-testable
seam) runs the existing empty-config secret resolution AND, when the creds are
INCOMPLETE, reuses ``resolve_evidence._with_cross_account_credentials`` to fill the
missing cross-account keys. Fully offline: the shared helper and ``_fetch_credentials``
are patched — no AWS, no network.
"""
from __future__ import annotations

from unittest.mock import patch

import agents.runtime_app as runtime_app


def test_populated_config_missing_external_id_gets_merged():
    """A populated config that has ``role_arn`` but NO ``external_id`` (the SiteWise
    Direct case) is passed through the DRY cross-account helper, which fills the
    missing ``external_id`` so the runtime assumes the role WITH ``sts:ExternalId``."""
    populated = {"region": "us-east-1",
                 "role_arn": "arn:aws:iam::111122223333:role/adt-reader"}

    def _fake_merge(system_id, cfg):
        # Simulate the shared helper filling ONLY the missing cross-account key.
        out = dict(cfg)
        out["external_id"] = "ext-abc"
        return out

    with patch("agents.resolve_evidence._with_cross_account_credentials",
               side_effect=_fake_merge) as merge:
        cfg = runtime_app._resolve_runtime_connection_config(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
            "sitewise-sys", populated, secret_arn="")

    merge.assert_called_once_with("sitewise-sys", populated)
    assert cfg["external_id"] == "ext-abc"          # secret-only key now present
    assert cfg["role_arn"] == "arn:aws:iam::111122223333:role/adt-reader"
    assert cfg["region"] == "us-east-1"             # non-credential field preserved


def test_already_complete_config_short_circuits():
    """A config that already carries BOTH ``role_arn`` and ``external_id`` is left
    unchanged — the merge helper is NOT invoked (no needless graph/secret lookup)
    and no secret is fetched."""
    complete = {"region": "us-east-1",
                "role_arn": "arn:aws:iam::111122223333:role/adt-reader",
                "external_id": "already-set"}

    with patch("agents.resolve_evidence._with_cross_account_credentials") as merge, \
            patch("agents.system_agent._fetch_credentials") as fetch:
        cfg = runtime_app._resolve_runtime_connection_config(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
            "sitewise-sys", complete, secret_arn="")

    merge.assert_not_called()
    fetch.assert_not_called()
    assert cfg == complete


def test_local_config_without_role_arn_returned_unchanged():
    """A local system (no ``role_arn``) is returned unchanged: the DRY helper is
    invoked but finds nothing to merge (best-effort, fails soft), so the config is
    unchanged and no secret is fetched from the empty-config branch."""
    local = {"host": "db.example.com", "port": 5432, "dbname": "adt"}

    with patch("agents.resolve_evidence._with_cross_account_credentials",
               side_effect=lambda system_id, cfg: dict(cfg)) as merge, \
            patch("agents.system_agent._fetch_credentials") as fetch:
        cfg = runtime_app._resolve_runtime_connection_config(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
            "pg-sys", local, secret_arn="")

    merge.assert_called_once_with("pg-sys", local)
    fetch.assert_not_called()
    assert cfg == local


def test_empty_config_resolves_from_secret_then_merges():
    """Empty ``CONNECTION_CONFIG`` + a ``CONNECTION_SECRET_ARN``: the config is first
    resolved from the secret (existing behaviour); the agent-config secret already
    carries ``external_id`` so the subsequent DRY merge short-circuits (helper not
    invoked)."""
    fetched = {"arn": None}

    def _fake_fetch(arn):
        fetched["arn"] = arn
        return {"workgroup": "adt-wg", "database": "adt_db",
                "role_arn": "arn:aws:iam::111122223333:role/adt-reader",
                "external_id": "ext-from-secret"}

    with patch("agents.system_agent._fetch_credentials", side_effect=_fake_fetch), \
            patch("agents.resolve_evidence._with_cross_account_credentials") as merge:
        cfg = runtime_app._resolve_runtime_connection_config(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
            "athena-sys", {}, secret_arn="arn:aws:secretsmanager:us-east-1:111:secret:conn")

    assert fetched["arn"] == "arn:aws:secretsmanager:us-east-1:111:secret:conn"
    assert cfg["external_id"] == "ext-from-secret"
    assert cfg["role_arn"] == "arn:aws:iam::111122223333:role/adt-reader"
    merge.assert_not_called()  # secret already complete -> merge short-circuits


def test_empty_config_from_secret_missing_external_id_gets_merged():
    """Empty config resolved from a secret that lacks ``external_id`` still gets the
    key merged by the DRY helper (defence-in-depth for a partial secret)."""
    def _fake_fetch(arn):
        return {"region": "us-east-1",
                "role_arn": "arn:aws:iam::111122223333:role/adt-reader"}

    def _fake_merge(system_id, cfg):
        out = dict(cfg)
        out["external_id"] = "ext-merged"
        return out

    with patch("agents.system_agent._fetch_credentials", side_effect=_fake_fetch), \
            patch("agents.resolve_evidence._with_cross_account_credentials",
                  side_effect=_fake_merge) as merge:
        cfg = runtime_app._resolve_runtime_connection_config(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
            "sitewise-sys", {}, secret_arn="arn:aws:secretsmanager:us-east-1:111:secret:conn")

    merge.assert_called_once()
    assert cfg["external_id"] == "ext-merged"
