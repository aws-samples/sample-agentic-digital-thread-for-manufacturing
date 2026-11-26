# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the external-OIDC context resolver
(stacks/_edge_auth.py::resolve_external_oidc, ADR-0072).

Pure-function coverage only (no synth, no AWS): the all-or-nothing contract
for the issuer/client-id/secret-ARN trio, the optional provider name, and the
input validation that keeps a half-configured deploy from silently losing its
corporate sign-in button. The module imports aws_cdk at module level, so the
whole test skips cleanly in an environment without the CDK libs.

Run:
  pytest agentic_digital_thread/tests/unit/test_external_oidc_context.py -q
"""
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _import_aws_cdk():
    """Import aws_cdk, retrying ONCE on jsii's package-cache extraction race
    (see test_custom_domain_context.py for the xdist history)."""
    import importlib, time
    try:
        return importlib.import_module("aws_cdk")
    except Exception as exc:  # noqa: BLE001 - classify, then retry or skip
        if "EEXIST" not in str(exc):
            raise
        time.sleep(1.0)
        return importlib.import_module("aws_cdk")


try:
    _import_aws_cdk()
except Exception as exc:  # ImportError, or FileNotFoundError('node') from jsii
    pytest.skip(f"aws-cdk-lib/jsii unavailable in this env: {exc}", allow_module_level=True)

from stacks._edge_auth import (  # noqa: E402
    DEFAULT_EXTERNAL_OIDC_PROVIDER_NAME,
    ExternalOidcConfig,
    resolve_external_oidc,
)

ISSUER = "https://login.example.com"
CLIENT_ID = "example-client-id"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/oidc-client-secret-AbCdEf"  # nosec B105 - a secret's ARN with a placeholder account, not a value


def test_all_unset_returns_none():
    assert resolve_external_oidc(None, None, None) is None
    assert resolve_external_oidc("", "", "") is None
    assert resolve_external_oidc("  ", "", None, "") is None


def test_full_trio_resolves_with_default_name():
    cfg = resolve_external_oidc(ISSUER, CLIENT_ID, SECRET_ARN)
    assert cfg == ExternalOidcConfig(
        issuer_url=ISSUER,
        client_id=CLIENT_ID,
        client_secret_arn=SECRET_ARN,
        provider_name=DEFAULT_EXTERNAL_OIDC_PROVIDER_NAME,
    )
    assert cfg.provider_name == "CorporateIdP"


def test_inputs_are_normalized():
    cfg = resolve_external_oidc(
        f"  {ISSUER}/  ", f" {CLIENT_ID} ", f" {SECRET_ARN} ", " ExampleCorp-SSO "
    )
    assert cfg.issuer_url == ISSUER          # trailing slash dropped
    assert cfg.client_id == CLIENT_ID
    assert cfg.client_secret_arn == SECRET_ARN
    assert cfg.provider_name == "ExampleCorp-SSO"


@pytest.mark.parametrize(
    "issuer, client_id, secret_arn, expected_missing",
    [
        (ISSUER, "", "", "oidc_client_id, oidc_client_secret_arn"),
        ("", CLIENT_ID, "", "oidc_client_secret_arn, oidc_issuer_url"),
        ("", "", SECRET_ARN, "oidc_client_id, oidc_issuer_url"),
        (ISSUER, CLIENT_ID, "", "oidc_client_secret_arn"),
    ],
)
def test_partial_trio_fails_loud_and_names_the_missing_keys(
    issuer, client_id, secret_arn, expected_missing
):
    with pytest.raises(ValueError) as exc:
        resolve_external_oidc(issuer, client_id, secret_arn)
    message = str(exc.value)
    assert "Incomplete external-OIDC configuration" in message
    assert f"missing {expected_missing}" in message
    # The error tells the operator which deploy.sh flags to pass.
    assert "--oidc-issuer" in message and "--oidc-secret-arn" in message


def test_name_without_trio_fails_loud():
    with pytest.raises(ValueError, match="oidc_provider_name .* without the provider trio"):
        resolve_external_oidc(None, None, None, "CorporateIdP")


def test_issuer_must_be_https():
    with pytest.raises(ValueError, match="must be an https:// URL"):
        resolve_external_oidc("http://login.example.com", CLIENT_ID, SECRET_ARN)
    with pytest.raises(ValueError, match="must be an https:// URL"):
        resolve_external_oidc("login.example.com", CLIENT_ID, SECRET_ARN)


def test_secret_must_be_a_secrets_manager_arn():
    with pytest.raises(ValueError, match="not an AWS Secrets Manager secret ARN"):
        resolve_external_oidc(ISSUER, CLIENT_ID, "adt/oidc-client-secret")
    with pytest.raises(ValueError, match="not an AWS Secrets Manager secret ARN"):
        resolve_external_oidc(
            ISSUER, CLIENT_ID, "arn:aws:ssm:us-east-1:111122223333:parameter/adt/oidc"
        )


@pytest.mark.parametrize(
    "bad_name",
    [
        "COGNITO",            # reserved local-directory provider name
        "cognito",
        "Corp_IdP",           # underscore would make <Provider>_<subject> ambiguous
        "Corp IdP",           # whitespace
        "ab",                 # too short
        "x" * 33,             # too long
        "-leading",           # must start alphanumeric
    ],
)
def test_invalid_provider_names_fail_loud(bad_name):
    with pytest.raises(ValueError, match="not a valid Cognito provider name"):
        resolve_external_oidc(ISSUER, CLIENT_ID, SECRET_ARN, bad_name)


@pytest.mark.parametrize("good_name", ["CorporateIdP", "ExampleCorp-SSO", "login.example", "Okta1"])
def test_valid_provider_names_accepted(good_name):
    cfg = resolve_external_oidc(ISSUER, CLIENT_ID, SECRET_ARN, good_name)
    assert cfg.provider_name == good_name


# ---------------------------------------------------------------------------
# oidc_only — external provider as the SOLE sign-in (no local Cognito form)
# ---------------------------------------------------------------------------

def test_oidc_only_defaults_to_false():
    cfg = resolve_external_oidc(ISSUER, CLIENT_ID, SECRET_ARN)
    assert cfg.oidc_only is False
    assert resolve_external_oidc(ISSUER, CLIENT_ID, SECRET_ARN, None, "").oidc_only is False


@pytest.mark.parametrize("word", ["true", "TRUE", " on ", "yes", "1", True])
def test_oidc_only_true_words(word):
    assert resolve_external_oidc(ISSUER, CLIENT_ID, SECRET_ARN, oidc_only=word).oidc_only is True


@pytest.mark.parametrize("word", ["false", "off", "no", "0", "", None, False])
def test_oidc_only_false_words(word):
    assert resolve_external_oidc(ISSUER, CLIENT_ID, SECRET_ARN, oidc_only=word).oidc_only is False


def test_oidc_only_junk_fails_loud():
    with pytest.raises(ValueError, match="oidc_only 'maybe' is not a boolean"):
        resolve_external_oidc(ISSUER, CLIENT_ID, SECRET_ARN, oidc_only="maybe")


def test_oidc_only_without_trio_fails_loud():
    """An external-only client with no external provider locks everyone out."""
    with pytest.raises(ValueError, match="oidc_only=true was set without the provider trio"):
        resolve_external_oidc(None, None, None, oidc_only="true")


def test_oidc_only_false_without_trio_is_still_none():
    assert resolve_external_oidc(None, None, None, oidc_only="false") is None


def test_client_builder_rejects_oidc_only_without_provider():
    """Guard at the construct level too: the stacks derive the flag from the
    config, but a direct caller must not be able to build a provider-less
    client (SupportedIdentityProviders=[] is accepted by CloudFormation and
    would deploy a sign-in nobody can complete)."""
    from stacks._edge_auth import create_edge_oauth_client
    with pytest.raises(ValueError, match="oidc_only requires an external identity provider"):
        create_edge_oauth_client(
            object(), construct_id="X", user_pool=object(), external_idp=None, oidc_only=True,
        )
