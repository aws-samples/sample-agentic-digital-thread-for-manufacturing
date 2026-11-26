# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deploy.sh delegated-user-token pre-check on the Quick gateway (ADR-0079 D1).

The deploy.sh section is EXECUTED in bash with ``aws`` and ``cfn_output``
stubbed, so the behaviour — not the wording — is pinned:

  * a partial pair, a pair without ``--quick-api``, a missing Quick gateway,
    and an audience that is not the gateway URL all exit 1 BEFORE anything is
    persisted (no SSM write);
  * a valid pair persists both sticky parameters and emits the CDK flags;
  * with no flags, the sticky SSM values are re-applied (no write);
  * nothing configured ⇒ no flags, no writes.

Offline: no AWS, no network (bash + stubs only).
"""
from __future__ import annotations

import shutil
import subprocess  # nosec B404 - fixed argv (bash), no shell string interpolation of inputs
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
_START = "# ── Delegated user tokens on the Quick gateway (ADR-0079"
_END = '_QUICKLINK_PARAMS=("/adt/highbyte-url"'
DISCOVERY = "https://idp.example.com/.well-known/openid-configuration"
GATEWAY_URL = "https://quick-gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")


def _section() -> str:
    src = (_ADT_ROOT / "deploy.sh").read_text()
    start = src.index(_START)
    return src[start:src.index(_END, start)]


def _run(tmp_path, *, discovery="", audience="", quick=True, gateway_url=GATEWAY_URL,
         sticky=None):
    """Run the section; returns (exit_code, stdout+stderr, ssm_writes)."""
    writes = tmp_path / "writes.log"
    sticky = sticky or {}
    stub = f'''
set -eo pipefail
REGION=us-east-1
_AWS_PROFILE_FLAG=""
QUICK_OIDC_DISCOVERY={discovery!r}
QUICK_OIDC_AUDIENCE={audience!r}
_QUICK_CDK_FLAGS={" -c quick_api_enabled=true" if quick else ""!r}
aws() {{
  if [[ "$2" == "put-parameter" ]]; then echo "$4=$6" >> {str(writes)!r}; return 0; fi
  if [[ "$2" == "get-parameter" ]]; then
    case "$4" in
      /adt/quick-oidc-discovery-url) echo {sticky.get("discovery", "None")!r} ;;
      /adt/quick-oidc-audience) echo {sticky.get("audience", "None")!r} ;;
    esac
    return 0
  fi
}}
cfn_output() {{ echo {gateway_url!r}; }}
{_section()}
echo "FLAGS=[$_QUICK_OIDC_CDK_FLAGS]"
'''
    script_path = tmp_path / "quick_oidc_section.sh"
    script_path.write_text(stub, encoding="utf-8")
    result = subprocess.run(["bash", str(script_path)], capture_output=True, text=True,  # nosec B603 B607
                            timeout=30)
    logged = writes.read_text().splitlines() if writes.exists() else []
    return result.returncode, result.stdout + result.stderr, logged


def test_nothing_configured_adds_no_flags_and_writes_nothing(tmp_path):
    code, out, writes = _run(tmp_path)
    assert code == 0
    assert "FLAGS=[]" in out
    assert writes == []


def test_valid_pair_persists_and_emits_the_cdk_flags(tmp_path):
    code, out, writes = _run(tmp_path, discovery=DISCOVERY, audience=GATEWAY_URL)
    assert code == 0, out
    assert writes == [f"/adt/quick-oidc-discovery-url={DISCOVERY}",
                      f"/adt/quick-oidc-audience={GATEWAY_URL}"]
    assert (f"FLAGS=[ -c quick_oidc_discovery_url={DISCOVERY} "
            f"-c quick_oidc_audience={GATEWAY_URL}]") in out


def test_sticky_values_are_reapplied_without_writing(tmp_path):
    code, out, writes = _run(tmp_path, sticky={"discovery": DISCOVERY, "audience": GATEWAY_URL})
    assert code == 0, out
    assert writes == []
    assert f"-c quick_oidc_audience={GATEWAY_URL}" in out


@pytest.mark.parametrize("kwargs,needle", [
    ({"discovery": DISCOVERY}, "must be provided together"),
    ({"audience": GATEWAY_URL}, "must be provided together"),
    ({"discovery": DISCOVERY, "audience": GATEWAY_URL, "quick": False}, "--quick-api"),
    ({"discovery": DISCOVERY, "audience": GATEWAY_URL, "gateway_url": ""}, "Deploy with --quick-api or --quick-connector first"),
    ({"discovery": DISCOVERY, "audience": GATEWAY_URL, "gateway_url": "None"}, "Deploy with --quick-api or --quick-connector first"),
    ({"discovery": DISCOVERY, "audience": "https://other.example.com/mcp"}, "must be EXACTLY the Quick gateway URL"),
    ({"sticky": {"discovery": DISCOVERY}}, "must be provided together"),
    ({"sticky": {"discovery": DISCOVERY, "audience": GATEWAY_URL}, "quick": False}, "--quick-api"),
], ids=["discovery-only", "audience-only", "no-quick-api", "no-gateway", "gateway-None",
        "audience-mismatch", "sticky-partial", "sticky-no-quick-api"])
def test_misconfiguration_fails_before_anything_is_persisted(tmp_path, kwargs, needle):
    code, out, writes = _run(tmp_path, **kwargs)
    assert code == 1
    assert needle in out
    assert writes == []


def test_audience_list_is_refused_even_when_it_contains_the_gateway_url(tmp_path):
    """Exactly one audience, equal to the gateway URL."""
    audience = f"https://other.example.com/mcp,{GATEWAY_URL}"
    code, out, writes = _run(tmp_path, discovery=DISCOVERY, audience=audience)
    assert code == 1
    assert "EXACTLY the Quick gateway URL" in out
    assert writes == []
