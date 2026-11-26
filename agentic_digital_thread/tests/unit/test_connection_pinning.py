# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shared credential-source layer: the ARN allowlist and the pinned-field
rule. Pure functions, no IO."""
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents import connection_pinning as cp  # noqa: E402

ACCT = "111122223333"


def _arn(name, region="us-east-1", account=ACCT):
    return f"arn:aws:secretsmanager:{region}:{account}:secret:{name}-AbCdEf"


# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #
def test_empty_allowlist_refuses_every_arn_and_hides_the_option():
    env = {"AWS_REGION": "us-east-1"}            # ADT_SECRET_ARN_ALLOWLIST unset
    assert cp.allowlist_prefixes(env) == ()
    assert cp.arn_source_enabled(env) is False
    with pytest.raises(cp.SecretArnNotAllowedError):
        cp.check_arn_allowed(_arn("customer/tulip/tulip-connection"), environ=env)


def test_allowlisted_prefix_is_accepted():
    env = {"AWS_REGION": "us-east-1", "ADT_SECRET_ARN_ALLOWLIST": "customer/tulip/"}
    assert cp.arn_source_enabled(env) is True
    cp.check_arn_allowed(_arn("customer/tulip/tulip-connection"), environ=env)  # no raise


def test_a_name_outside_the_allowed_prefix_is_refused():
    env = {"AWS_REGION": "us-east-1", "ADT_SECRET_ARN_ALLOWLIST": "customer/tulip/"}
    with pytest.raises(cp.SecretArnNotAllowedError):
        cp.check_arn_allowed(_arn("team/other/thing"), environ=env)


def test_allowlist_parses_comma_and_whitespace():
    env = {"ADT_SECRET_ARN_ALLOWLIST": "customer/tulip/,  team/shared/\tevent/x/"}
    assert cp.allowlist_prefixes(env) == ("customer/tulip/", "team/shared/", "event/x/")


@pytest.mark.parametrize("name", [
    "adt/conn/tulip-1",
    "adt/systems/foo",
    "adt/agent-config/bar",
    "adt-demo-password",
    "ReaderErpData",                 # _db_readers reader login
    "AdtDbSecret",
    "DatabaseSecret1AB2C3",
])
def test_adt_own_secrets_are_refused_even_inside_an_allowed_prefix(name):
    # Allow a prefix that would otherwise cover it, to prove the own-secret deny
    # is independent of the allowlist.
    env = {"AWS_REGION": "us-east-1",
           "ADT_SECRET_ARN_ALLOWLIST": "adt/,Reader,AdtDbSecret,DatabaseSecret,"}
    assert cp.is_forbidden_secret_name(name) is True
    with pytest.raises(cp.SecretArnNotAllowedError):
        cp.check_arn_allowed(_arn(name), environ=env)


def test_a_secret_in_another_region_is_refused():
    env = {"AWS_REGION": "us-east-1", "ADT_SECRET_ARN_ALLOWLIST": "customer/tulip/"}
    with pytest.raises(cp.SecretArnNotAllowedError):
        cp.check_arn_allowed(_arn("customer/tulip/x", region="eu-west-1"), environ=env)


def test_the_refusal_text_never_echoes_a_region_that_is_not_a_region_name():
    # The region field of a submitted ARN is free text; the refusal is logged
    # and shown, so only a well-formed region name may appear in it.
    env = {"AWS_REGION": "us-east-1", "ADT_SECRET_ARN_ALLOWLIST": "adt/workshop/"}
    for odd in ("AKIAEXAMPLE", "x\ninjected", "a" * 80):
        with pytest.raises(cp.SecretArnNotAllowedError) as err:
            cp.check_arn_allowed(_arn("adt/workshop/x", region=odd), environ=env)
        assert odd not in str(err.value)
    with pytest.raises(cp.SecretArnNotAllowedError) as err:
        cp.check_arn_allowed(_arn("adt/workshop/x", region="eu-west-1"), environ=env)
    assert "eu-west-1" in str(err.value)


def test_a_non_arn_is_refused():
    env = {"AWS_REGION": "us-east-1", "ADT_SECRET_ARN_ALLOWLIST": "customer/tulip/"}
    for bad in ("", "customer/tulip/x", "not-an-arn", "arn:aws:s3:::bucket"):
        with pytest.raises(cp.SecretArnNotAllowedError):
            cp.check_arn_allowed(bad, environ=env)


# --------------------------------------------------------------------------- #
# Pinned fields
# --------------------------------------------------------------------------- #
_SECRET = {"api_key": "K", "api_secret": "S", "base_url": "https://acme.tulip.co",
           "workspace_id": "ws1", "key_scope": "workspace"}


def test_pinned_values_lists_only_present_non_empty_fields():
    assert cp.pinned_values(_SECRET) == {"base_url": "https://acme.tulip.co",
                                         "workspace_id": "ws1", "key_scope": "workspace"}
    assert cp.pinned_values({"api_key": "K"}) == {}
    assert cp.pinned_values({"base_url": "   ", "workspace_id": "ws1"}) == {"workspace_id": "ws1"}


def test_a_matching_url_or_workspace_is_allowed():
    cp.refuse_pin_conflict(_SECRET, {"base_url": "https://acme.tulip.co/", "workspace_id": "ws1"})
    cp.refuse_pin_conflict(_SECRET, {})            # omitted -> fine
    cp.refuse_pin_conflict(_SECRET, {"base_url": "", "workspace_id": None})  # blank -> fine


def test_a_conflicting_url_is_refused():
    with pytest.raises(cp.PinnedFieldConflictError):
        cp.refuse_pin_conflict(_SECRET, {"base_url": "https://attacker.example.com"})


def test_a_conflicting_workspace_is_refused():
    with pytest.raises(cp.PinnedFieldConflictError):
        cp.refuse_pin_conflict(_SECRET, {"workspace_id": "ws999"})


def test_a_conflicting_key_scope_is_refused():
    with pytest.raises(cp.PinnedFieldConflictError):
        cp.refuse_pin_conflict(_SECRET, {"key_scope": "instance"})


def test_a_secret_with_no_pins_never_conflicts():
    cp.refuse_pin_conflict({"api_key": "K"}, {"base_url": "https://anything.example.com",
                                              "workspace_id": "zzz"})


def test_apply_pins_forces_the_secret_values_in():
    out = cp.apply_pins(_SECRET, {"base_url": "", "other": "kept"})
    assert out["base_url"] == "https://acme.tulip.co"
    assert out["workspace_id"] == "ws1" and out["key_scope"] == "workspace"
    assert out["other"] == "kept"
