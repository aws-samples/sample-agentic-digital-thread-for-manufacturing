# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the generic cross-account AWS session helper (agents.aws_session).

Fully synthetic: STS + service clients are mocked (no network, no real creds, no
dataset literals). Covers the two branches (ambient vs. assumed role), ExternalId
handling, malformed-ARN fail-closed behavior, region precedence, and the security
invariant that ``external_id`` is never logged.
"""
import datetime
import logging

import pytest

from agents import aws_session
from agents.aws_session import AwsSessionError, boto3_client_for, session_for

# A well-formed IAM role ARN (fictitious account id — no real resource).
GOOD_ROLE = "arn:aws:iam::111122223333:role/adt-cross-account"
# A secret-ish external id we assert never lands in logs.
SECRET_EXTERNAL_ID = "ext-id-do-not-log-42"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture


class FakeSTS:
    """STS double recording assume_role calls and returning canned temp creds."""

    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "ASIAFAKETEMPKEY",
                "SecretAccessKey": "fake-temp-secret",
                "SessionToken": "fake-temp-token",
                "Expiration": datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc),
            }
        }


class FakeServiceClient:
    """A stand-in for a boto3 service client (ambient path)."""

    def __init__(self, service, region_name=None):
        self.service = service
        self.region_name = region_name


@pytest.fixture
def patched_boto3(monkeypatch):
    """Patch aws_session.boto3.client so STS and ambient service clients are fake.

    Returns (fake_sts, recorded_calls) where recorded_calls is a list of
    (service, region_name) tuples for every boto3.client() invocation.
    """
    fake_sts = FakeSTS()
    calls = []

    def fake_client(service, region_name=None, **kwargs):
        calls.append((service, region_name))
        if service == "sts":
            return fake_sts
        return FakeServiceClient(service, region_name)

    monkeypatch.setattr(aws_session.boto3, "client", fake_client)
    return fake_sts, calls


# --- ambient (no role_arn) branch -------------------------------------------

def test_ambient_client_when_no_role_arn(patched_boto3):
    fake_sts, calls = patched_boto3
    client = boto3_client_for("athena", {}, default_region="us-west-2")
    assert isinstance(client, FakeServiceClient)
    assert client.service == "athena"
    assert client.region_name == "us-west-2"
    # No cross-account assumption happened.
    assert fake_sts.calls == []
    assert ("sts", pytest.approx) not in calls  # sanity: sts never created
    assert all(svc != "sts" for svc, _ in calls)


def test_ambient_client_when_blank_role_arn(patched_boto3):
    fake_sts, _ = patched_boto3
    client = boto3_client_for("iotsitewise", {"role_arn": "   "}, default_region="eu-central-1")
    assert isinstance(client, FakeServiceClient)
    assert fake_sts.calls == []


# --- assumed-role branch -----------------------------------------------------

def test_session_for_assumes_role_with_external_id(patched_boto3):
    fake_sts, _ = patched_boto3
    sess = session_for(
        {"role_arn": GOOD_ROLE, "external_id": SECRET_EXTERNAL_ID, "region": "us-east-1"},
        service="athena",
    )
    assert len(fake_sts.calls) == 1
    call = fake_sts.calls[0]
    assert call["RoleArn"] == GOOD_ROLE
    assert call["ExternalId"] == SECRET_EXTERNAL_ID
    assert call["RoleSessionName"].startswith("adt-athena-")
    # The returned session carries the TEMPORARY credentials from assume_role.
    creds = sess.get_credentials()
    assert creds.access_key == "ASIAFAKETEMPKEY"
    assert creds.token == "fake-temp-token"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture


def test_session_for_omits_external_id_when_absent(patched_boto3):
    fake_sts, _ = patched_boto3
    session_for({"role_arn": GOOD_ROLE, "region": "us-east-1"}, service="iotsitewise")
    assert len(fake_sts.calls) == 1
    assert "ExternalId" not in fake_sts.calls[0]


def test_boto3_client_for_assumes_role(patched_boto3):
    fake_sts, _ = patched_boto3
    client = boto3_client_for(
        "athena", {"role_arn": GOOD_ROLE, "region": "us-west-2"}, default_region="us-east-1"
    )
    assert len(fake_sts.calls) == 1
    # A real botocore client bound to the temp-cred session in the requested region.
    assert client.meta.region_name == "us-west-2"


# --- assumed-session caching (cost control) ----------------------------------

def test_session_for_caches_and_reuses_session(patched_boto3):
    """A second call for the SAME (role_arn, external_id, region) reuses the cached
    session and does NOT trigger a second STS AssumeRole (cost control). The
    RefreshableCredentials keep the reused session valid across expiry."""
    fake_sts, _ = patched_boto3
    cfg = {"role_arn": GOOD_ROLE, "external_id": SECRET_EXTERNAL_ID, "region": "us-east-1"}
    s1 = session_for(cfg, service="iotsitewise")
    s2 = session_for(cfg, service="iotsitewise")
    assert s1 is s2                       # same cached session object
    assert len(fake_sts.calls) == 1       # exactly ONE AssumeRole for both calls


def test_boto3_client_for_reuses_cached_session(patched_boto3):
    """Repeated client builds (the real hot path — one client per backend call)
    share a single AssumeRole via the cached session."""
    fake_sts, _ = patched_boto3
    cfg = {"role_arn": GOOD_ROLE, "region": "us-east-1"}
    for _ in range(5):
        boto3_client_for("iotsitewise", cfg)
    assert len(fake_sts.calls) == 1       # 5 clients, ONE AssumeRole


def test_session_cache_keyed_by_identity(patched_boto3):
    """Different region / external_id / role are DISTINCT cache keys → each still
    assumes once (no cross-identity credential reuse)."""
    fake_sts, _ = patched_boto3
    session_for({"role_arn": GOOD_ROLE, "region": "us-east-1"}, service="s")
    session_for({"role_arn": GOOD_ROLE, "region": "us-west-2"}, service="s")   # diff region
    session_for({"role_arn": GOOD_ROLE, "external_id": SECRET_EXTERNAL_ID,
                 "region": "us-east-1"}, service="s")                          # diff ext id
    assert len(fake_sts.calls) == 3


def test_reset_session_cache_forces_reassume(patched_boto3):
    """Clearing the cache forces a fresh AssumeRole on the next call."""
    fake_sts, _ = patched_boto3
    cfg = {"role_arn": GOOD_ROLE, "region": "us-east-1"}
    session_for(cfg, service="s")
    aws_session._reset_session_cache()
    session_for(cfg, service="s")
    assert len(fake_sts.calls) == 2


# --- malformed role_arn (fail-closed) ---------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        "not-an-arn",
        "arn:aws:iam::123:role/too-short-account",
        "arn:aws:s3:::some-bucket",
        "arn:aws:iam::111122223333:user/not-a-role",
        "arn:aws:iam::111122223333:role/",  # empty role name
    ],
)
def test_malformed_role_arn_raises(patched_boto3, bad):
    fake_sts, _ = patched_boto3
    with pytest.raises(AwsSessionError):
        boto3_client_for("athena", {"role_arn": bad})
    # Fail closed BEFORE any STS call.
    assert fake_sts.calls == []


def test_failed_assume_role_raises_actionable_error(monkeypatch):
    class BoomSTS:
        def assume_role(self, **kwargs):
            raise RuntimeError("AccessDenied: not authorized to assume")

    def fake_client(service, region_name=None, **kwargs):
        if service == "sts":
            return BoomSTS()
        return FakeServiceClient(service, region_name)

    monkeypatch.setattr(aws_session.boto3, "client", fake_client)
    with pytest.raises(AwsSessionError) as exc:
        boto3_client_for("athena", {"role_arn": GOOD_ROLE})
    # Actionable: names the role.
    assert GOOD_ROLE in str(exc.value)


# --- region precedence -------------------------------------------------------

def test_region_precedence_config_wins(patched_boto3, monkeypatch):
    _, calls = patched_boto3
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    boto3_client_for("athena", {"region": "eu-west-1"}, default_region="us-east-1")
    assert calls[-1] == ("athena", "eu-west-1")


def test_region_precedence_default_over_env(patched_boto3, monkeypatch):
    _, calls = patched_boto3
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    boto3_client_for("athena", {}, default_region="us-east-1")
    assert calls[-1] == ("athena", "us-east-1")


def test_region_precedence_env_fallback(patched_boto3, monkeypatch):
    _, calls = patched_boto3
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    boto3_client_for("athena", {})
    assert calls[-1] == ("athena", "ap-southeast-2")


# --- security: external_id must never be logged ------------------------------

def test_external_id_never_logged(patched_boto3, caplog):
    with caplog.at_level(logging.DEBUG, logger="agents.aws_session"):
        session_for(
            {"role_arn": GOOD_ROLE, "external_id": SECRET_EXTERNAL_ID, "region": "us-east-1"},
            service="athena",
        )
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_EXTERNAL_ID not in combined
    # role_arn IS allowed in logs (and useful for debugging).
    assert GOOD_ROLE in combined
