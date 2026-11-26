# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An ARN-sourced Tulip system must name a complete, well-formed secret of this
deployment before it is usable. Two guards (offline; no AWS, no Neptune):

* item 4a: ``connection_resolver._resolve_arn_source`` requires a COMPLETE,
  valid Tulip seat-secret contract (https base_url, a valid workspace_id,
  key_scope in {instance, workspace}, and api_key+api_secret or a token). A
  secret missing or carrying an invalid routing field makes the source
  unusable (``_secret_unresolved``), NEVER a fall back to the registration's
  own base_url beside the secret-derived token.
* item 5: ``connection_pinning.check_arn_allowed`` validates the full ARN
  identity (supported partition, this Region, this account, resource type
  ``secret``, a valid Secrets Manager name) before the prefix/ADT-owned rules,
  so a foreign-account or malformed full-looking ARN is refused up front.

These tests fail on ``98eec927`` (the resolver merged only the pins present and
the ARN check skipped partition/account/resource shape) and pass after.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_pinning as pinning  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402

ALLOWLIST = "customer/tulip/"
OWN_ACCOUNT = "111122223333"  # the offline conftest's deployment account
SEAT_ARN = f"arn:aws:secretsmanager:us-east-1:{OWN_ACCOUNT}:secret:customer/tulip/tulip-connection-AbCdEf"
SID = "tulip-mes"
CALLER_URL = "https://attacker.example.com"

#: The complete, valid seat contract (raw pair, no prebuilt token).
COMPLETE = {"schema": 1, "mode": "mock", "base_url": "https://acme.tulip.co", "workspace_id": "ws-7",
            "key_scope": "workspace", "api_key": "AK", "api_secret": "AS"}  # nosec B106


@pytest.fixture
def arn_enabled(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", ALLOWLIST)


def _arn_entry(stored_cfg=None):
    cfg = {"credential_source": "secret_arn"}
    cfg.update(stored_cfg or {})
    return {"id": SID, "name": "Tulip MES", "connection_type": TULIP,
            "schema": "", "status": "registered", "connection_origin": "user",
            "secret_arn": SEAT_ARN, "connection_config": cfg}


# ── item 4a: an incomplete contract makes the source unusable ──

def test_a_complete_contract_resolves_and_derives_the_basic_token(arn_enabled, monkeypatch):
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(COMPLETE))
    resolved = cr.registration_connection(SID, _arn_entry())
    import base64
    assert resolved.get("_secret_unresolved") is False, resolved
    assert resolved.get("base_url") == "https://acme.tulip.co"
    assert resolved.get("workspace_id") == "ws-7"
    # The Basic token is derived from the raw pair at use time.
    assert resolved.get("token") == base64.b64encode(b"AK:AS").decode()
    assert resolved.get("auth_scheme") == "basic"


@pytest.mark.parametrize("bad", [
    {k: v for k, v in COMPLETE.items() if k != "base_url"},                 # no base_url
    {**COMPLETE, "base_url": "http://acme.tulip.co"},                       # not https
    {k: v for k, v in COMPLETE.items() if k != "workspace_id"},             # no workspace
    {**COMPLETE, "workspace_id": "bad id/../x"},                            # invalid workspace
    {k: v for k, v in COMPLETE.items() if k != "key_scope"},                # no scope
    {**COMPLETE, "key_scope": "global"},                                    # invalid scope
    {k: v for k, v in COMPLETE.items() if k not in ("api_key", "api_secret")},  # no creds/token
])
def test_an_incomplete_contract_is_a_retryable_miss_and_never_unpins(bad, arn_enabled, monkeypatch):
    # The stored config carries a caller base_url; an incomplete secret must NOT
    # leave it in place beside any credential.
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(bad))
    resolved = cr.registration_connection(SID, _arn_entry({"base_url": CALLER_URL}))
    assert resolved.get("_secret_unresolved") is True, resolved
    assert resolved.get("base_url") != CALLER_URL, resolved
    assert not resolved.get("token"), resolved


# ── item 5: full ARN identity is validated before the allowlist ──

def _check(arn, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", ALLOWLIST)
    pinning.check_arn_allowed(arn)


def test_a_wellformed_own_account_arn_passes(monkeypatch):
    _check(SEAT_ARN, monkeypatch)  # no raise


def test_a_foreign_account_arn_is_refused(monkeypatch):
    foreign = f"arn:aws:secretsmanager:us-east-1:444455556666:secret:customer/tulip/tulip-connection-AbCdEf"
    with pytest.raises(pinning.SecretArnNotAllowedError, match="another AWS account"):
        _check(foreign, monkeypatch)


def test_a_bad_partition_is_refused(monkeypatch):
    bad = f"arn:notaws:secretsmanager:us-east-1:{OWN_ACCOUNT}:secret:customer/tulip/tulip-connection-AbCdEf"
    with pytest.raises(pinning.SecretArnNotAllowedError, match="partition"):
        _check(bad, monkeypatch)


def test_a_non_twelve_digit_account_is_refused(monkeypatch):
    bad = "arn:aws:secretsmanager:us-east-1:not-an-account:secret:customer/tulip/tulip-connection-AbCdEf"
    with pytest.raises(pinning.SecretArnNotAllowedError, match="12-digit"):
        _check(bad, monkeypatch)


def test_a_non_secret_resource_type_is_refused(monkeypatch):
    bad = f"arn:aws:secretsmanager:us-east-1:{OWN_ACCOUNT}:parameter:customer/tulip/tulip-connection-AbCdEf"
    with pytest.raises(pinning.SecretArnNotAllowedError):
        _check(bad, monkeypatch)


def test_a_name_that_only_matches_the_prefix_as_a_substring_is_refused(monkeypatch):
    # 'xcustomer/tulip/...' contains the prefix but does not START with it.
    not_prefixed = f"arn:aws:secretsmanager:us-east-1:{OWN_ACCOUNT}:secret:xcustomer/tulip/tulip-connection-AbCdEf"
    with pytest.raises(pinning.SecretArnNotAllowedError, match="allowed set"):
        _check(not_prefixed, monkeypatch)


def test_the_account_check_fails_closed_when_unknown(monkeypatch):
    # No AWS_ACCOUNT_ID and the STS seam returns nothing: the ARN's account
    # cannot be checked, so the ARN is refused (not deferred).
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", ALLOWLIST)
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    cr._ACCOUNT_CACHE.update(value=None, until=0.0)
    monkeypatch.setattr(cr, "_sts_account_id", lambda: "")
    with pytest.raises(pinning.SecretArnNotAllowedError, match="could not be determined"):
        pinning.check_arn_allowed(SEAT_ARN)
