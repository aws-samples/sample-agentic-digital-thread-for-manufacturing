# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An Athena registration needs a read-only role; say so at registration.

No app or runtime role of a deployed ADT grants Athena actions, so an Athena
system registered with a blank Role ARN can only fail later, at discover or
query time, with an AccessDenied. The Connect New System hint even said the
role was optional ("leave blank to use the app's own account credentials").
A user registration (HTTP route and chat tool) of an Athena system with no
role ARN is now refused up front with what to do: deploy
``stacks/cross-account/adt-data-readonly-role.yaml`` with TrustedAccountId set to
this app's account (DataAccountId defaults to the account the role is deployed
in; S3TablesAccountId is set only for tables in Amazon S3 Tables), deploy the app with
``--cross-account-role convention`` (or that role's ARN), then register with
the role ARN. Nothing is claimed or stored. Local development
(``AGENT_MODE=local`` / ``DEV_MODE``) runs on the developer's own credentials
and keeps accepting a blank role. No IAM changes. Offline.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    SID, _register, cat, no_dns, secrets)

ROLE = "arn:aws:iam::111122223333:role/ADTCrossAccountDataReadOnly"
ATHENA = {"workgroup": "wg", "database": "lake", "region": "us-east-1"}


@pytest.fixture
def deployed_mode(monkeypatch):
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.delenv("DEV_MODE", raising=False)


def _athena(cfg: dict) -> dict:
    return _register({"name": "Quality Lab", "connection_type": "Athena",
                      "connection_config": cfg})


@pytest.mark.parametrize("role", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_an_athena_registration_without_a_role_is_refused_with_the_fix(
        cat, secrets, deployed_mode, role):
    cfg = dict(ATHENA) if role is None else {**ATHENA, "role_arn": role}
    res = _athena(cfg)
    err = res.get("error", "")
    assert "read-only IAM role" in err, res
    assert "stacks/cross-account/adt-data-readonly-role.yaml" in err
    assert "--cross-account-role convention" in err
    assert "TrustedAccountId" in err
    assert "DataAccountId defaults to the account you deploy the role in" in err
    assert "set S3TablesAccountId only when your tables are in Amazon S3 Tables" in err
    assert "set DataBucketName for a plain Amazon S3 source; left empty, the role gets no plain S3 access" in err
    assert cat.get(SID) is None and secrets == {}


def test_guard_an_athena_registration_with_a_role_registers(cat, secrets, deployed_mode):
    res = _athena({**ATHENA, "role_arn": ROLE, "external_id": "x" * 16})
    assert res.get("status") == "created", res


def test_guard_local_development_keeps_accepting_a_blank_role(cat, secrets, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    res = _athena(dict(ATHENA))
    assert res.get("status") == "created", res


def test_the_chat_tool_gives_the_same_refusal(cat, secrets, deployed_mode):
    import agents.onboard_agent as oa
    res = oa.connect_new_system(name="Quality Lab", system_type="Custom", plant="",
                                connection_type="Athena", connection_config=dict(ATHENA))
    assert "read-only IAM role" in res.get("error", ""), res


def test_guard_demo_registration_is_not_affected(cat, deployed_mode):
    import agents.onboard_agent as oa
    res = oa.register_demo_system(name="Lake", system_type="Historian", plant="",
                                  connection_type="Athena", connection_string="",
                                  schema="", system_id="lake",
                                  connection_config=dict(ATHENA))
    assert not (isinstance(res, dict) and res.get("error")), res
