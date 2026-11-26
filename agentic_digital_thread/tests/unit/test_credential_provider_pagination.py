# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pagination of AgentCore OAuth2 credential-provider listings.

``list_oauth2_credential_providers`` returns one page plus a ``nextToken``.
``AgentManager._ensure_credential_provider`` read only page 1 in BOTH its
lookups, so with ``adt-m2m`` on page 2 the first lookup missed it, create
raised ConflictException, the recovery lookup missed it again, and the call
returned ``""`` — gateway targets were then created WITHOUT outbound auth to
their JWT-protected runtimes. These tests pin the paginating helper and both
lookups. All fixtures are synthetic; no AWS call is made.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import pytest  # noqa: E402

from agents.agent_manager import AgentManager  # noqa: E402

_M2M = "adt-m2m"
_M2M_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/oauth2credentialprovider/adt-m2m"
_FILLER = [f"other-provider-{i:02d}" for i in range(10)]  # fills page 1


class ConflictException(Exception):  # name matters: the code matches the type name
    pass


class _PagedProviders:
    """Credential-provider listing paged 10 at a time. ``appear_on_conflict``
    adds ``adt-m2m`` (at the end, i.e. page 2) when create is called — a
    concurrent creator winning the race."""

    PAGE = 10

    def __init__(self, names, *, appear_on_conflict: bool = False,
                 key: str = "credentialProviders"):
        self.providers = [self._item(n) for n in names]
        self.appear_on_conflict = appear_on_conflict
        self.key = key
        self.list_calls: list[dict] = []
        self.create_calls = 0

    @staticmethod
    def _item(name: str) -> dict:
        arn = _M2M_ARN if name == _M2M else f"arn:aws:x:provider/{name}"
        return {"name": name, "credentialProviderArn": arn,
                "credentialProviderVendor": "CustomOauth2"}

    def list_oauth2_credential_providers(self, nextToken=None):  # noqa: N803
        self.list_calls.append({} if nextToken is None else {"nextToken": nextToken})
        start = int(nextToken or 0)
        resp = {self.key: [dict(p) for p in self.providers[start:start + self.PAGE]]}
        if start + self.PAGE < len(self.providers):
            resp["nextToken"] = str(start + self.PAGE)
        return resp

    def create_oauth2_credential_provider(self, **kwargs):
        self.create_calls += 1
        if self.appear_on_conflict and not any(p["name"] == _M2M for p in self.providers):
            self.providers.append(self._item(_M2M))
        if any(p["name"] == kwargs["name"] for p in self.providers):
            raise ConflictException(f"{kwargs['name']} already exists")
        return {"credentialProviderArn": "arn:new"}


class _FakeCognito:
    def __init__(self):
        self.calls = 0

    def describe_user_pool_client(self, UserPoolId, ClientId):  # noqa: N803
        self.calls += 1
        return {"UserPoolClient": {"ClientSecret": "test-only-secret"}}  # nosec B105


def _manager() -> AgentManager:
    mgr = AgentManager.__new__(AgentManager)  # no __init__ side effects needed
    mgr._credential_provider_arn = ""
    mgr._m2m_client_id = "client-id"
    mgr._m2m_discovery_url = "https://idp.example.com/.well-known/openid-configuration"
    mgr._m2m_pool_id = "us-east-1_example"
    return mgr


def _ensure(control) -> tuple[str, _FakeCognito]:
    cognito = _FakeCognito()
    with patch("agents.agent_manager.boto3.client", lambda *a, **k: cognito):
        arn = _manager()._ensure_credential_provider(control, "us-east-1")
    return arn, cognito


# --------------------------------------------------------------------------- #
# The helper
# --------------------------------------------------------------------------- #

def test_helper_collects_every_page_first_call_without_arguments():
    from agents.runtime_provision_helpers import list_all_oauth2_credential_providers

    control = _PagedProviders(_FILLER + [_M2M])
    names = [p["name"] for p in list_all_oauth2_credential_providers(control)]
    assert names == _FILLER + [_M2M]
    assert control.list_calls == [{}, {"nextToken": "10"}]


def test_helper_works_with_a_no_argument_fake_and_the_fallback_key():
    from agents.runtime_provision_helpers import list_all_oauth2_credential_providers

    class _Strict:
        def list_oauth2_credential_providers(self):
            return {"oauth2CredentialProviders": [{"name": _M2M}]}

    assert [p["name"] for p in list_all_oauth2_credential_providers(_Strict())] == [_M2M]


def test_helper_raises_on_a_repeated_token():
    from agents.runtime_provision_helpers import list_all_oauth2_credential_providers

    class _Looping:
        def list_oauth2_credential_providers(self, nextToken=None):  # noqa: N803
            return {"credentialProviders": [], "nextToken": "same"}

    with pytest.raises(RuntimeError, match="repeated nextToken"):
        list_all_oauth2_credential_providers(_Looping())


# --------------------------------------------------------------------------- #
# _ensure_credential_provider: both lookups
# --------------------------------------------------------------------------- #

def test_first_lookup_reuses_a_page_two_provider():
    control = _PagedProviders(_FILLER + [_M2M])
    arn, cognito = _ensure(control)
    assert arn == _M2M_ARN
    assert control.create_calls == 0      # never reached create
    assert cognito.calls == 0             # never read the client secret
    assert {"nextToken": "10"} in control.list_calls


def test_conflict_recovery_finds_a_page_two_provider():
    """A concurrent creator wins: create raises ConflictException and the
    provider now sits on page 2. The recovery lookup must find it."""
    control = _PagedProviders(_FILLER, appear_on_conflict=True)
    arn, cognito = _ensure(control)
    assert arn == _M2M_ARN
    assert control.create_calls == 1
    assert cognito.calls == 1
    assert control.list_calls[-1] == {"nextToken": "10"}  # recovery read page 2


def test_absent_provider_is_created_once():
    control = _PagedProviders(_FILLER)
    arn, _ = _ensure(control)
    assert arn == "arn:new"
    assert control.create_calls == 1


def test_cached_arn_short_circuits():
    mgr = _manager()
    mgr._credential_provider_arn = _M2M_ARN
    control = _PagedProviders(_FILLER + [_M2M])
    assert mgr._ensure_credential_provider(control, "us-east-1") == _M2M_ARN
    assert control.list_calls == []
