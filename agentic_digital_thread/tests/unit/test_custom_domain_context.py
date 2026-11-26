# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the custom-domain context resolver
(stacks/_custom_domain.py::resolve_custom_domain).

Pure-function coverage only (no synth, no AWS): the all-or-nothing contract for
the three context values, domain/zone containment validation, and input
normalization. The module imports aws_cdk at module level (for the certificate
stack + wire function), so the whole test skips cleanly in an environment
without the CDK libs.

Run:
  pytest agentic_digital_thread/tests/unit/test_custom_domain_context.py -q
"""
import sys
from pathlib import Path

import pytest

# Repo root (for the stacks/ package — the CDK app lives above agentic_digital_thread/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Not importorskip: importing aws_cdk spawns the jsii Node.js child process, so a
# missing `node` raises FileNotFoundError (not ImportError) — catch both and skip.
def _import_aws_cdk():
    """Import aws_cdk, retrying ONCE on jsii's package-cache extraction race.

    Under xdist several workers import aws_cdk simultaneously; jsii extracts
    its package cache with a non-atomic mkdir and one worker can lose with
    ``EEXIST`` (measured in the CI lane: that worker skipped this module while
    its siblings collected it → "Different tests were collected"). The cache is
    complete by the time the loser retries.
    """
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

from stacks._custom_domain import CustomDomainConfig, resolve_custom_domain  # noqa: E402


def test_all_absent_returns_none():
    assert resolve_custom_domain(None, None, None) is None
    assert resolve_custom_domain("", "", "") is None
    assert resolve_custom_domain("  ", "", None) is None


def test_full_config_resolves():
    cfg = resolve_custom_domain("adt.example.com", "Z0123456789ABC", "example.com")
    assert cfg == CustomDomainConfig(
        domain_name="adt.example.com",
        hosted_zone_id="Z0123456789ABC",
        hosted_zone_name="example.com",
    )


def test_domain_may_equal_zone_apex():
    cfg = resolve_custom_domain("example.com", "Z0123456789ABC", "example.com")
    assert cfg is not None
    assert cfg.domain_name == "example.com"


def test_normalizes_case_whitespace_and_trailing_dots():
    cfg = resolve_custom_domain(
        "  ADT.Example.COM. ", " Z0123456789ABC ", " Example.COM. "
    )
    assert cfg == CustomDomainConfig(
        domain_name="adt.example.com",
        hosted_zone_id="Z0123456789ABC",
        hosted_zone_name="example.com",
    )


@pytest.mark.parametrize(
    "domain,zone_id,zone_name,missing",
    [
        ("adt.example.com", "", "", "hosted_zone_id"),
        ("adt.example.com", "Z0123456789ABC", "", "hosted_zone_name"),
        ("", "Z0123456789ABC", "example.com", "custom_domain"),
        ("", "Z0123456789ABC", "", "custom_domain"),
    ],
)
def test_partial_config_fails_loud(domain, zone_id, zone_name, missing):
    with pytest.raises(ValueError, match=missing):
        resolve_custom_domain(domain, zone_id, zone_name)


def test_domain_outside_zone_rejected():
    with pytest.raises(ValueError, match="not within hosted zone"):
        resolve_custom_domain("adt.other.org", "Z0123456789ABC", "example.com")


def test_suffix_match_requires_label_boundary():
    # "badexample.invalid" ends with "example.invalid" as a STRING but is a
    # different domain — must be rejected (containment is label-wise, not
    # substring).
    with pytest.raises(ValueError, match="not within hosted zone"):
        resolve_custom_domain("badexample.invalid", "Z0123456789ABC", "example.invalid")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
