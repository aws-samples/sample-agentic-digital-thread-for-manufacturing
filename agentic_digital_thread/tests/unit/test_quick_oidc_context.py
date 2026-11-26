# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the Quick-gateway OIDC context resolver
(stacks/_quick_gateway_auth.py::resolve_quick_oidc, ADR-0079 D1).

Pure-function coverage (no synth, no AWS): both-or-neither, URL shapes, the
multi-audience parse, and the loud failure when the pair is configured while
the Quick integration is off. Skips cleanly without the CDK libs (the module
imports aws_cdk at module level).
"""
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _import_aws_cdk():
    """Import aws_cdk, retrying ONCE on jsii's package-cache extraction race."""
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

from stacks import _quick_gateway_auth as qga  # noqa: E402
from stacks._quick_gateway_auth import (  # noqa: E402
    QuickOidcConfig,
    parse_audience,
    resolve_quick_oidc,
)

DISCOVERY = "https://idp.example.com/.well-known/openid-configuration"
AUDIENCE = "https://gateway.example.com/mcp"


def test_unset_returns_none():
    assert resolve_quick_oidc(None, None, quick_api_enabled=True) is None
    assert resolve_quick_oidc("  ", "  ", quick_api_enabled=False) is None


def test_full_pair_resolves():
    assert resolve_quick_oidc(DISCOVERY, AUDIENCE, quick_api_enabled=True) == \
        QuickOidcConfig(discovery_url=DISCOVERY, audience=AUDIENCE)


def test_audience_is_trimmed_and_a_list_is_refused():
    from stacks._quick_gateway_auth import parse_audience
    assert parse_audience(f" {AUDIENCE} ") == AUDIENCE
    with pytest.raises(ValueError, match="exactly ONE value"):
        parse_audience(f"{AUDIENCE},https://other.example.com/mcp")


@pytest.mark.parametrize("discovery,audience,missing", [
    (DISCOVERY, "", "quick_oidc_audience"),
    ("", AUDIENCE, "quick_oidc_discovery_url"),
])
def test_partial_pair_fails_loud_and_names_the_flags(discovery, audience, missing):
    with pytest.raises(ValueError) as err:
        resolve_quick_oidc(discovery, audience, quick_api_enabled=True)
    message = str(err.value)
    assert missing in message
    assert "--quick-oidc-discovery" in message and "--quick-oidc-audience" in message


def test_pair_without_quick_api_fails_loud_naming_both_remedies():
    with pytest.raises(ValueError) as err:
        resolve_quick_oidc(DISCOVERY, AUDIENCE, quick_api_enabled=False)
    message = str(err.value)
    assert "--quick-api" in message
    assert "/adt/quick-oidc-discovery-url" in message
    assert "/adt/quick-oidc-audience" in message


@pytest.mark.parametrize("bad", [
    "http://idp.example.com/.well-known/openid-configuration",
    "https://idp.example.com",
    "https://idp.example.com/.well-known/jwks.json",
    "https:///.well-known/openid-configuration",
])
def test_discovery_url_shape_is_validated(bad):
    with pytest.raises(ValueError, match="quick_oidc_discovery_url"):
        resolve_quick_oidc(bad, AUDIENCE, quick_api_enabled=True)


@pytest.mark.parametrize("bad", [
    "https://gateway.example.com",
    "https://gateway.example.com/mcp/",
    "http://gateway.example.com/mcp",
    "adt-app-service",
])
def test_the_audience_must_be_an_https_gateway_url_with_mcp(bad):
    with pytest.raises(ValueError, match="INCLUDING /mcp"):
        resolve_quick_oidc(DISCOVERY, bad, quick_api_enabled=True)


def test_stack_side_key_name_matches_the_claim_module():
    from agents.auth.identity_claim import IDENTITY_CLAIM_SECRET_NAME
    assert qga.IDENTITY_CLAIM_SECRET_NAME == IDENTITY_CLAIM_SECRET_NAME


def test_stack_side_key_readable_flag_matches_the_app_reader():
    from agents.auth.delegated_identity import IDENTITY_CLAIM_KEY_READABLE_ENV
    assert qga.IDENTITY_CLAIM_KEY_READABLE_ENV == IDENTITY_CLAIM_KEY_READABLE_ENV


def test_staged_sources_exist():
    from pathlib import Path as _P
    assert (_P(qga._INTERCEPTOR_SRC) / "index.py").is_file()
    assert _P(qga._IDENTITY_CLAIM_SRC).is_file()


# --------------------------------------------------------------------------- #
# The audience is ONE value, the gateway URL. A list widens
# the set of tokens the gateway trusts to every listed resource.
# --------------------------------------------------------------------------- #
import pytest as _pytest


@_pytest.mark.parametrize("aud", [
    "https://gw.example.com/mcp,https://other.example.com/mcp",
    "https://gw.example.com/mcp, https://other.example.com/mcp",
    "https://gw.example.com/mcp https://other.example.com/mcp",
])
def test_audience_list_is_rejected(aud):
    from stacks._quick_gateway_auth import resolve_quick_oidc
    with _pytest.raises(ValueError, match="exactly ONE value"):
        resolve_quick_oidc("https://idp.example.com/.well-known/openid-configuration", aud,
                           quick_api_enabled=True)


def test_single_audience_is_emitted_as_a_one_element_list():
    from stacks._quick_gateway_auth import resolve_quick_oidc
    cfg = resolve_quick_oidc("https://idp.example.com/.well-known/openid-configuration",
                             " https://gw.example.com/mcp ", quick_api_enabled=True)
    assert cfg.audience == "https://gw.example.com/mcp"
