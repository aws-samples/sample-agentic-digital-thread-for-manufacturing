# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every onboarding and system runtime is told its own AWS account.

A full Secrets Manager ARN is read only when its account is the deployment's
(``connection_resolver.refuse_foreign_account_secret``), and the deployment
account comes from ``AWS_ACCOUNT_ID`` else one STS GetCallerIdentity. Only
the app task had ``AWS_ACCOUNT_ID``; the onboarding runtime and every system
runtime (which reads its ``CONNECTION_SECRET_ARN`` while it starts) depended
on STS through NAT, so one STS hiccup failed an otherwise healthy start
closed. ``AWS_ACCOUNT_ID`` is now a key of the central runtime env contract
for the onboarding and system roles (written when the writer knows the
account, reconciled onto existing runtimes), fed by every writer: the in-app
provisioner (the app task's own env), the provisioner Lambda (its CDK env),
deploy.sh (the onboarding driver and the system reconcile) and the workshop
runtime bootstrap (a CFN property). Offline.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import agents.connection_resolver as cr
from agents.runtime_env_contract import (
    OVERWRITE_WHEN_SOURCE_NONEMPTY,
    REQUIRED_KEYS,
    STALE_CHECKED_KEYS,
    RuntimeEnvSources,
    build_env,
    known_keys,
    reconcile_env,
)

ACCOUNT = "111122223333"
_ADT = Path(__file__).resolve().parents[2]
_SYSTEM_KW = dict(system_id="erp", system_name="ERP", schema="public",
                  connection_type="PostgreSQL", system_agent_model="m")


def _build(role: str, sources: RuntimeEnvSources) -> dict:
    return build_env(role, sources, **(_SYSTEM_KW if role == "system" else {}))


@pytest.mark.parametrize("role", ["onboarding", "system"])
def test_the_contract_writes_the_account_for_onboarding_and_system(role):
    assert _build(role, RuntimeEnvSources(account_id=ACCOUNT))["AWS_ACCOUNT_ID"] == ACCOUNT


@pytest.mark.parametrize("role", ["onboarding", "system"])
def test_an_unknown_account_is_never_written_empty_but_stays_known(role):
    assert "AWS_ACCOUNT_ID" not in _build(role, RuntimeEnvSources())
    assert "AWS_ACCOUNT_ID" in known_keys(role)


def test_the_account_is_not_boot_required():
    """Optional at boot: a runtime without it still learns the account from
    STS (the pre-existing behaviour); it is never a stale-checked key."""
    for role in STALE_CHECKED_KEYS:
        assert "AWS_ACCOUNT_ID" not in STALE_CHECKED_KEYS[role]


def test_from_ambient_reads_the_account():
    assert RuntimeEnvSources.from_ambient({"AWS_ACCOUNT_ID": ACCOUNT}).account_id == ACCOUNT
    assert RuntimeEnvSources.from_ambient({}).account_id == ""


@pytest.mark.parametrize("role", ["onboarding", "system"])
def test_reconcile_adds_the_account_to_an_existing_runtime(role):
    assert REQUIRED_KEYS[role]["AWS_ACCOUNT_ID"] == OVERWRITE_WHEN_SOURCE_NONEMPTY
    existing = {"SYSTEM_REQUIRED_GROUP": "ops", "CONNECTION_SECRET_ARN": "arn:x"}
    out = reconcile_env(role, existing, RuntimeEnvSources(account_id=ACCOUNT))
    assert out["AWS_ACCOUNT_ID"] == ACCOUNT
    assert out["SYSTEM_REQUIRED_GROUP"] == "ops" and out["CONNECTION_SECRET_ARN"] == "arn:x"
    kept = reconcile_env(role, {"AWS_ACCOUNT_ID": ACCOUNT}, RuntimeEnvSources())
    assert kept["AWS_ACCOUNT_ID"] == ACCOUNT          # empty source: untouched


def test_the_in_app_writer_passes_the_app_tasks_account(monkeypatch):
    from agents.agent_manager import AgentManager
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
    env = AgentManager._base_env_vars("erp", "ERP", "public", "PostgreSQL", "us-east-1", {})
    assert env["AWS_ACCOUNT_ID"] == ACCOUNT


def test_the_provisioner_lambda_writer_passes_its_account(monkeypatch):
    from agents.provisioner.handlers import _runtime_env_vars
    from agents.provisioner.models import CreateRuntimeInput
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
    inp = CreateRuntimeInput(system_id="erp", system_name="ERP", schema="public",
                             connection_type="PostgreSQL", region="us-east-1",
                             system_agent_model="m", bedrock_model="b")
    assert _runtime_env_vars(inp, "arn:secret", {"host": "db"})["AWS_ACCOUNT_ID"] == ACCOUNT


def _deploy_block(marker: str, end: str) -> str:
    text = (_ADT / "deploy.sh").read_text()
    start = text.index(marker)
    return text[start:text.index(end, start)]


def test_deploy_sh_passes_the_account_to_the_onboarding_driver():
    block = _deploy_block("ONB_JSON=$(", "--role onboarding")
    assert 'AWS_ACCOUNT_ID="${ACCOUNT:-}"' in block, block


def test_deploy_sh_passes_the_account_to_the_system_reconcile():
    block = _deploy_block("RT_ENV_NEW=$(", "--role system --reconcile")
    assert 'AWS_ACCOUNT_ID="${ACCOUNT:-}"' in block, block


def test_deploy_sh_learns_the_account_before_either_writer():
    text = (_ADT / "deploy.sh").read_text()
    learned = re.search(r'^ACCOUNT=\$\(aws sts get-caller-identity', text, re.M)
    assert learned and learned.start() < text.index("ONB_JSON=$(")
    assert learned.start() < text.index("RT_ENV_NEW=$(")


def test_the_cli_shim_reconcile_carries_the_account(monkeypatch, capsys):
    from agents import runtime_env_contract as rec
    import io
    import sys
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"SYSTEM_ID": "erp"})))
    assert rec._main(["--role", "system", "--reconcile"]) == 0
    assert json.loads(capsys.readouterr().out)["AWS_ACCOUNT_ID"] == ACCOUNT


# ── a system runtime starting while STS is down ───────────────────────────

class _FakeSecretsManager:
    def __init__(self):
        self.reads: list = []

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3 name
        self.reads.append(SecretId)
        return {"SecretString": json.dumps({"host": "db.example.internal", "port": 5432,
                                            "dbname": "erp", "user": "u", "password": "p"})}


@pytest.fixture
def sts_down(monkeypatch):
    def _boom():
        raise RuntimeError("EndpointConnectionError: sts.us-east-1.amazonaws.com")
    monkeypatch.setattr(cr, "_sts_account_id", _boom)
    cr._ACCOUNT_CACHE.update(value=None, until=0.0)
    fake = _FakeSecretsManager()
    import boto3
    monkeypatch.setattr(boto3, "client", lambda service, **kw: fake)
    import agents.resolve_evidence as resolve_evidence
    monkeypatch.setattr(resolve_evidence, "_with_cross_account_credentials",
                        lambda system_id, cfg: cfg)
    return fake


def _start_with_contract_env(monkeypatch, sources: RuntimeEnvSources):
    """Apply the env the contract writes for a system runtime, then run the
    runtime's start-up connection resolution (runtime_app)."""
    from agents import runtime_app
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    for key, value in _build("system", sources).items():
        monkeypatch.setenv(key, value)
    arn = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:adt/agent-config/erp-AbCdEf"
    return runtime_app._resolve_runtime_connection_config("erp", {}, arn), arn


def test_a_system_runtime_reads_its_own_secret_while_sts_is_down(monkeypatch, sts_down):
    cfg, arn = _start_with_contract_env(monkeypatch, RuntimeEnvSources(account_id=ACCOUNT))
    assert sts_down.reads == [arn]
    assert cfg["host"] == "db.example.internal"


def test_guard_without_the_account_the_same_start_fails_closed(monkeypatch, sts_down):
    with pytest.raises(cr.AccountUnknownSecretError):
        _start_with_contract_env(monkeypatch, RuntimeEnvSources())
    assert sts_down.reads == []
