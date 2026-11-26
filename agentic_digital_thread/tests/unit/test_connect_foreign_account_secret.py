# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A secret ARN in another AWS account is never read.

``is_own_connection_secret`` checks a pointer's region and name but not its
account, and nothing else compared it either: a node or catalog pointer naming
``adt/conn/<id>`` in another account was fetched like the system's own (the
payload binding was the only defence). Every secret read now refuses a full ARN
whose account is not the deployment's (``AWS_ACCOUNT_ID``, else STS
GetCallerIdentity): resolve, the registration reads, the admin Edit's
read-modify-write and the runtime credential fetchers. A refused read is an
unreadable secret, so every path fails closed (retryable error, nothing
written, never a fallback). A bare secret name and an unknown deployment account
are not compared. Offline: Secrets Manager and STS are fakes.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture

_REAL_FETCH = cr._fetch_secret          # before the secrets fixture replaces it
OWN_ACCOUNT = "111122223333"
FOREIGN = "444455556666"


def _foreign(system_id: str) -> str:
    return f"arn:aws:secretsmanager:us-east-1:{FOREIGN}:secret:adt/conn/{system_id}-AbCdEf"


@pytest.fixture
def own_account(monkeypatch):
    monkeypatch.setattr(cr, "_sts_account_id", lambda: OWN_ACCOUNT, raising=False)


@pytest.fixture
def sm(monkeypatch):
    """The REAL cr._fetch_secret / read_secret_uncached over a fake client
    that answers every id with a usable, bound payload (so a read that is not
    refused succeeds)."""
    calls: list = []

    class _SM:
        def get_secret_value(self, SecretId):  # noqa: N803 - boto3 name
            calls.append(SecretId)
            sid = str(SecretId).split("adt/conn/", 1)[-1].rsplit("-", 1)[0]
            return {"SecretString": json.dumps({
                "token": "t", "connection_string": DSN, "user": "lab_reader",
                "password": PASSWORD, "connection_origin": "user", "system_id": sid})}
    monkeypatch.setattr(cr, "_secrets_client", lambda: _SM())
    monkeypatch.setattr(cr, "_fetch_secret", _REAL_FETCH)
    cr._secret_cache.clear()
    yield calls
    cr._secret_cache.clear()


def _refused(fn) -> None:
    with pytest.raises(Exception) as caught:
        fn()
    assert type(caught.value).__name__ == "ForeignAccountSecretError", repr(caught.value)


def test_a_foreign_account_arn_is_never_fetched(own_account, sm):
    _refused(lambda: cr._fetch_secret(_foreign(SID)))
    _refused(lambda: cr.read_secret_uncached(_foreign(SID)))
    assert sm == []


def test_guard_own_account_arns_and_bare_names_are_fetched(own_account, sm):
    cr._fetch_secret(CONN_ARN)
    cr.read_secret_uncached("adt/conn/highbyte")
    assert sm == [CONN_ARN, "adt/conn/highbyte"]


def _sts_fails(monkeypatch):
    def _raise():
        raise RuntimeError("EndpointConnectionError")
    monkeypatch.setattr(cr, "_sts_account_id", _raise)


@pytest.mark.parametrize("arn", [_foreign(SID), CONN_ARN], ids=["foreign", "own-account"])
def test_an_unknown_deployment_account_refuses_every_full_arn_read(monkeypatch, sm, arn):
    """STS cannot say which account this is: no full-ARN secret is read, not
    even one that names the right account (it cannot be checked), and the
    error says to retry. No Secrets Manager call is made."""
    _sts_fails(monkeypatch)
    for read in (cr._fetch_secret, cr.read_secret_uncached):
        with pytest.raises(Exception) as caught:
            read(arn)
        assert type(caught.value).__name__ == "AccountUnknownSecretError", repr(caught.value)
        assert isinstance(caught.value, cr.ForeignAccountSecretError)
        assert "retry" in str(caught.value).lower()
    assert sm == [], "a Secrets Manager call was made with the account unknown"


def test_an_unknown_account_makes_resolve_fail_closed(cat, graph, monkeypatch, sm):
    _sts_fails(monkeypatch)
    graph["mcp-x"] = _node_row({"url": "https://mcp.example.com/mcp"}, _foreign("mcp-x"),
                               "MCP Server")
    resolved = cr.resolve_connection("mcp-x")
    assert resolved["_secret_unresolved"] is True and "token" not in resolved, resolved
    assert sm == []


def test_guard_a_bare_name_is_still_read_with_the_account_unknown(monkeypatch, sm):
    """A bare name resolves in the caller's own account: nothing to compare."""
    _sts_fails(monkeypatch)
    cr.read_secret_uncached("adt/conn/highbyte")
    assert sm == ["adt/conn/highbyte"]


def test_the_account_comes_from_the_env_first(monkeypatch, sm):
    monkeypatch.setenv("AWS_ACCOUNT_ID", OWN_ACCOUNT)
    _sts_fails(monkeypatch)
    _refused(lambda: cr._fetch_secret(_foreign(SID)))
    cr._fetch_secret(CONN_ARN)
    assert sm == [CONN_ARN]


def test_a_failed_lookup_is_retried_not_cached_forever(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cr, "_monotonic", lambda: clock[0])
    answers = iter([RuntimeError("throttled"), OWN_ACCOUNT])

    def _sts():
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(cr, "_sts_account_id", _sts)
    assert cr.deployment_account_id() == ""
    assert cr.deployment_account_id() == ""                 # inside the retry window
    assert cr._ACCOUNT_RETRY_SECONDS <= 10, "a blip must not refuse secrets for long"
    clock[0] += cr._ACCOUNT_RETRY_SECONDS + 1
    assert cr.deployment_account_id() == OWN_ACCOUNT


def test_a_node_pointing_at_a_foreign_account_secret_fails_closed(
        cat, graph, own_account, sm):
    """resolve marks the secret unresolved, and a PostgreSQL connect refuses:
    never the deployment's credentials, never the foreign ones."""
    from agents.errors import ConnectionSecretUnavailableError
    graph["mcp-x"] = _node_row({"url": "https://mcp.example.com/mcp"}, _foreign("mcp-x"),
                               "MCP Server")
    resolved = cr.resolve_connection("mcp-x")
    assert resolved["_secret_unresolved"] is True and "token" not in resolved, resolved
    graph["pg-x"] = _node_row({"host": "db.example.internal", "dbname": "lims"},
                              _foreign("pg-x"), "PostgreSQL")
    with pytest.raises(ConnectionSecretUnavailableError):
        cr.pg_conn_params(cr.resolve_connection("pg-x"))
    assert sm == []


def test_a_registration_whose_pointer_is_in_another_account_is_unreadable(
        cat, secrets, own_account, sm):
    from agents.errors import ConnectionSecretUnavailableError
    _register_pg()
    cat.get(SID)["secret_arn"] = _foreign(SID)
    with pytest.raises(ConnectionSecretUnavailableError):
        cr.registered_pg_dsn(SID, cat.get(SID))
    assert sm == []


def test_the_runtime_credential_fetchers_refuse_it(own_account, monkeypatch):
    import agents.onboard_agent as oa
    import agents.system_agent as sa
    for mod in (oa, sa):
        _refused(lambda: mod._fetch_credentials(_foreign(SID)))
