# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The M2M token endpoint, moved out of ``AgentManager._get_m2m_token``
unchanged: named from the account's last 8 digits, else the gateway id's."""

from __future__ import annotations

import types

from agents import agent_manager as am


def _manager(gateway_id: str):
    return types.SimpleNamespace(gateway_id=gateway_id)


def test_the_endpoint_is_named_from_the_account(monkeypatch):
    sts = types.SimpleNamespace(get_caller_identity=lambda: {"Account": "111122223333"})
    monkeypatch.setattr(am.boto3, "client", lambda service, region_name=None: sts)
    url = am.AgentManager._m2m_token_url(_manager("gw-abcdefgh12345678"), "us-west-2")
    assert url == "https://adt-m2m-22223333.auth.us-west-2.amazoncognito.com/oauth2/token"


def test_the_gateway_id_is_the_fallback(monkeypatch):
    def broken(service, region_name=None):
        raise RuntimeError("no credentials")
    monkeypatch.setattr(am.boto3, "client", broken)
    url = am.AgentManager._m2m_token_url(_manager("gw-abcdefgh12345678"), "us-east-1")
    assert url == "https://adt-m2m-12345678.auth.us-east-1.amazoncognito.com/oauth2/token"
    assert am.AgentManager._m2m_token_url(_manager(""), "us-east-1") == (
        "https://.auth.us-east-1.amazoncognito.com/oauth2/token")
