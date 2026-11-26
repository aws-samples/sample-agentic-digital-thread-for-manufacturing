# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tulip / i3X / Athena registrations get the target guard MCP and PostgreSQL get.

A user registration of an MCP Server or PostgreSQL system runs its target
through the SSRF/scheme guard (``connection_security.validate_connection_target``)
before anything is claimed or stored. Tulip, SiteWise (i3X) and Athena
registrations skipped it, so a metadata-endpoint or non-resolving URL was
registered (with its secret written) and only refused later by a probe. Now each
URL the registration carries (``connection_string`` and
``connection_config.base_url``) passes the same guard; an Athena registration,
which has no URL (boto3 builds the endpoint from the region), has its region
checked for an AWS Region name, its role ARN for an IAM role ARN and its
output location for ``s3://``. A refused registration claims
nothing and writes no secret. Offline: the guard is a fake.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    SID, _register, cat, secrets)

TULIP = "Tulip (Tables API)"
I3X = "SiteWise (i3X)"
GOOD = "https://ok.example.com"
BAD = "http://169.254.169.254/latest"


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    """The SSRF guard: every target under ok.example.com / amazonaws.com
    resolves, anything else is refused. Records what was checked."""
    seen: list = []
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def _validate(target, *a, **kw):
        seen.append(str(target))
        if "ok.example.com" in str(target) or str(target).endswith(".amazonaws.com"):
            return "203.0.113.10"
        raise cr.ConnectionTargetError("connection target is a cloud metadata endpoint")
    monkeypatch.setattr(cr, "validate_connection_target", _validate)
    return seen


def _refused(res, cat, secrets):
    assert "error" in res and "rejected" in res["error"], res
    assert cat.get(SID) is None and secrets == {}, (cat.get_all(), secrets)


@pytest.mark.parametrize("ctype, cfg", [
    (TULIP, {"api_key": "k", "api_secret": "s"}),
    (I3X, {"auth_scheme": "bearer", "token": "t"}),
])
@pytest.mark.parametrize("where", ["connection_string", "base_url", "url"])
def test_a_rest_family_registration_with_a_blocked_url_is_refused(
        cat, secrets, guard, ctype, cfg, where):
    urls = {"connection_string": GOOD, "base_url": GOOD, "url": GOOD, where: BAD}
    res = _register({"name": "Quality Lab", "connection_type": ctype,
                     "connection_string": urls["connection_string"],
                     "connection_config": {"base_url": urls["base_url"], "url": urls["url"],
                                           **cfg}})
    _refused(res, cat, secrets)
    assert BAD in guard


@pytest.mark.parametrize("ctype", [TULIP, I3X])
def test_guard_a_rest_family_registration_with_good_urls_still_registers(
        cat, secrets, guard, ctype):
    res = _register({"name": "Quality Lab", "connection_type": ctype, "connection_string": GOOD,
                     "connection_config": {"base_url": GOOD, "target": "1001"}})
    assert res.get("status") == "created", res
    assert GOOD in guard


@pytest.mark.parametrize("cfg, why", [
    ({"workgroup": "wg", "database": "lake", "region": "us-east-1.attacker.example#"}, "region"),
    ({"workgroup": "wg", "database": "lake", "role_arn": "arn:aws:s3:::bucket"}, "role ARN"),
    ({"workgroup": "wg", "database": "lake", "output_location": "http://x/y"}, "output location"),
])
def test_an_athena_registration_with_a_malformed_target_is_refused(cat, secrets, cfg, why):
    res = _register({"name": "Quality Lab", "connection_type": "Athena",
                     "connection_config": cfg})
    assert "error" in res, (why, res)
    assert cat.get(SID) is None and secrets == {}


def test_a_well_formed_athena_registration_registers_without_a_network_call(
        cat, secrets, guard):
    res = _register({"name": "Quality Lab", "connection_type": "Athena",
                     "connection_config": {"workgroup": "wg", "database": "lake",
                                           "region": "eu-west-1",
                                           "role_arn": "arn:aws:iam::111122223333:role/reader",
                                           "output_location": "s3://amzn-s3-demo-results-bucket/adt/"}})
    assert res.get("status") == "created", res
    assert guard == [], "Athena has no URL: shape checks only (the probe runs the guard)"


def test_guard_an_athena_registration_without_a_region_registers(cat, secrets, guard):
    res = _register({"name": "Quality Lab", "connection_type": "Athena",
                     "connection_config": {"workgroup": "wg", "database": "lake",
                                           "role_arn": "arn:aws:iam::111122223333:role/reader"}})
    assert res.get("status") == "created", res


def test_the_athena_region_check_is_the_shape_botocore_accepts():
    import agents.onboard_agent as oa
    for ok in ("us-east-1", "eu-west-2", "ap-southeast-4", "us-gov-west-1", "cn-north-1"):
        assert oa._AWS_REGION_SHAPE.fullmatch(ok), ok
    for bad in ("us-east-1.attacker.example", "x", "us_east_1", "us-east-1/", "US-EAST-1"):
        assert not oa._AWS_REGION_SHAPE.fullmatch(bad), bad
