# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deploy.sh ``--quick-connector`` (ADR-0093 connector path outside the workshop).

The flag section is EXECUTED in bash, so the emitted CDK context is pinned
rather than its wording:

  * no Quick flag ⇒ no Quick context at all;
  * ``--quick-connector`` alone ⇒ ``-c quick_connector_enabled=true`` and an
    EMPTY ``_QUICK_CDK_FLAGS`` (that variable makes deploy.sh seed the SSM
    parameters only the API path's task references);
  * with ``--quick-api`` both contexts are emitted (stacks/_quick_connector.py
    keeps the API path's token);
  * the delegated-user-token pre-check accepts the connector path, since it
    builds the same ``adt-quick-gw`` gateway.

Offline: bash + stubs only.
"""
from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - fixed argv (bash), no shell string interpolation of inputs
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
_SRC = (_ADT_ROOT / "deploy.sh").read_text()
_FLAGS_START = "# ── AWS Quick integration (optional, opt-in"
_FLAGS_END = '# ── Delegated user tokens on the Quick gateway (ADR-0079'
_OIDC_START = "# ── Delegated user tokens on the Quick gateway (ADR-0079"
_OIDC_END = '_QUICKLINK_PARAMS=("/adt/highbyte-url"'
GATEWAY_URL = "https://quick-gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
DISCOVERY = "https://idp.example.com/.well-known/openid-configuration"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")


def _between(start: str, end: str) -> str:
    i = _SRC.index(start)
    return _SRC[i:_SRC.index(end, i)]


def _run(tmp_path, body: str) -> subprocess.CompletedProcess:
    script = tmp_path / "section.sh"
    script.write_text("set -euo pipefail\n" + body, encoding="utf-8")
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,  # nosec B603 B607
                          timeout=30)


def _flags(tmp_path, *, api=0, connector=0) -> tuple[str, str]:
    body = (f"QUICK_API_ENABLE={api}\n"
            f"QUICK_CONNECTOR_ENABLE={connector}\n"
            + _between(_FLAGS_START, _FLAGS_END)
            + 'echo "API=[$_QUICK_CDK_FLAGS]"\necho "CONN=[$_QUICK_CONNECTOR_CDK_FLAG]"\n')
    result = _run(tmp_path, body)
    assert result.returncode == 0, result.stderr
    api_flags = re.search(r"^API=\[(.*)\]$", result.stdout, re.M).group(1)
    conn_flags = re.search(r"^CONN=\[(.*)\]$", result.stdout, re.M).group(1)
    return api_flags, conn_flags


def test_no_quick_flag_emits_no_quick_context(tmp_path):
    assert _flags(tmp_path) == ("", "")


def test_connector_alone_emits_only_the_connector_context(tmp_path):
    assert _flags(tmp_path, connector=1) == ("", " -c quick_connector_enabled=true")


def test_connector_combines_with_the_api_path(tmp_path):
    assert _flags(tmp_path, api=1, connector=1) == (
        " -c quick_api_enabled=true", " -c quick_connector_enabled=true")


def test_the_flag_is_parsed_and_reaches_cdk_deploy():
    assert re.search(r"^\s*--quick-connector\) QUICK_CONNECTOR_ENABLE=1; shift ;;$", _SRC, re.M)
    deploy_lines = [line for line in _SRC.splitlines() if "npx cdk deploy" in line]
    assert deploy_lines and all("$_QUICK_CONNECTOR_CDK_FLAG" in line for line in deploy_lines)


def _oidc(tmp_path, *, api_flags: str, connector_flag: str) -> subprocess.CompletedProcess:
    body = f'''
REGION=us-east-1
_AWS_PROFILE_FLAG=""
QUICK_OIDC_DISCOVERY={DISCOVERY!r}
QUICK_OIDC_AUDIENCE={GATEWAY_URL!r}
_QUICK_CDK_FLAGS={api_flags!r}
_QUICK_CONNECTOR_CDK_FLAG={connector_flag!r}
aws() {{
  if [[ "$2" == "get-parameter" ]]; then echo None; fi
  return 0
}}
cfn_output() {{ echo {GATEWAY_URL!r}; }}
{_between(_OIDC_START, _OIDC_END)}
echo "FLAGS=[$_QUICK_OIDC_CDK_FLAGS]"
'''
    return _run(tmp_path, body)


def test_delegated_tokens_accept_the_connector_gateway(tmp_path):
    result = _oidc(tmp_path, api_flags="", connector_flag=" -c quick_connector_enabled=true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"-c quick_oidc_audience={GATEWAY_URL}" in result.stdout


def test_delegated_tokens_still_need_a_quick_gateway(tmp_path):
    result = _oidc(tmp_path, api_flags="", connector_flag="")
    assert result.returncode == 1
    assert "--quick-connector" in result.stdout + result.stderr


# ── app.py: the delegated-token resolver sees the connector path ─────────────

def test_app_resolves_delegated_tokens_for_either_quick_gateway_flag():
    """resolve_quick_oidc must get the same gateway condition AgentCoreStack
    gets, or a connector-only deploy with /adt/quick-oidc-* fails synth."""
    import ast
    tree = ast.parse((_ADT_ROOT.parent / "app.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "resolve_quick_oidc"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}["quick_api_enabled"]
    assert isinstance(kw, ast.BoolOp) and isinstance(kw.op, ast.Or)
    assert {getattr(v, "id", "") for v in kw.values} == {
        "quick_api_enabled", "quick_connector_enabled"}


def test_resolver_error_names_both_flags():
    import sys
    sys.path.insert(0, str(_ADT_ROOT.parent))
    from stacks._quick_gateway_auth import resolve_quick_oidc
    with pytest.raises(ValueError) as err:
        resolve_quick_oidc(DISCOVERY, GATEWAY_URL, quick_api_enabled=False)
    assert "--quick-api" in str(err.value) and "--quick-connector" in str(err.value)


# ── pre-CDK: Quick gateway present but no Quick flag ─────────────────────────

_GUARD_START = "# ── Quick gateway present but no Quick flag: stop before CDK"
_GUARD_END = "# ── Pre-flight: create AgentCore service-linked role"


def _guard(tmp_path, *, api_flags="", connector_flag="", gateway="quick-gw-abc",
           targets="1", list_rc=0):
    calls = tmp_path / "calls.log"
    body = f'''
REGION=us-east-1
_AWS_PROFILE_FLAG=""
_QUICK_CDK_FLAGS={api_flags!r}
_QUICK_CONNECTOR_CDK_FLAG={connector_flag!r}
cfn_output() {{ echo {gateway!r}; }}
aws() {{
  echo "$*" >> {str(calls)!r}
  if [[ "$2" == "list-gateway-targets" ]]; then echo {targets!r}; return {list_rc}; fi
  return 0
}}
{_between(_GUARD_START, _GUARD_END)}
echo REACHED_CDK
'''
    result = _run(tmp_path, body)
    logged = calls.read_text().splitlines() if calls.exists() else []
    return result, logged


def test_a_forgotten_flag_stops_before_cdk_and_deletes_nothing(tmp_path):
    result, calls = _guard(tmp_path)
    assert result.returncode == 1 and "REACHED_CDK" not in result.stdout
    assert "--quick-connector or --quick-api" in result.stderr
    assert not any("delete-gateway-target" in c for c in calls)


def test_an_unreadable_target_list_fails_closed(tmp_path):
    result, _ = _guard(tmp_path, targets="", list_rc=255)
    assert result.returncode == 1 and "REACHED_CDK" not in result.stdout


def test_an_empty_gateway_is_removed_by_the_deploy(tmp_path):
    result, _ = _guard(tmp_path, targets="0")
    assert result.returncode == 0 and "REACHED_CDK" in result.stdout
    assert "removes it" in result.stdout


@pytest.mark.parametrize("api_flags, connector_flag", [
    (" -c quick_api_enabled=true", ""),
    ("", " -c quick_connector_enabled=true"),
])
def test_a_quick_flag_skips_the_guard(tmp_path, api_flags, connector_flag):
    result, calls = _guard(tmp_path, api_flags=api_flags, connector_flag=connector_flag)
    assert result.returncode == 0 and calls == []


def test_no_quick_gateway_means_no_target_calls(tmp_path):
    result, calls = _guard(tmp_path, gateway="None")
    assert result.returncode == 0 and calls == []


# ── post-deploy: bridge readiness and the connector values ───────────────────

_SUMMARY_START = "# ── AWS Quick MCP connector values (--quick-connector)"
_SUMMARY_END = 'if [ -n "${POSTCDK_FAILED:-}" ]; then'


def _summary(tmp_path, *, status="READY", oidc=""):
    body = f'''
REGION=us-east-1
_AWS_PROFILE_FLAG=""
QUICK_CONNECTOR_ENABLE=1
QUICK_BRIDGE_WAIT_POLLS=2
QUICK_BRIDGE_WAIT_SECONDS=0
_QUICK_OIDC_CDK_FLAGS={oidc!r}
POSTCDK_FAILED=""
record_postcdk_failure() {{ POSTCDK_FAILED="$POSTCDK_FAILED$1;"; }}
cfn_output() {{ echo "OUT:$2"; }}
aws() {{ echo {status!r}; }}
{_between(_SUMMARY_START, _SUMMARY_END)}
echo "FAILED=[$POSTCDK_FAILED]"
'''
    result = _run(tmp_path, body)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_ready_bridge_prints_service_auth_values_and_no_secret(tmp_path):
    out = _summary(tmp_path)
    assert "query-bridge target: READY" in out and "FAILED=[]" in out
    assert "OUT:QuickGatewayUrl" in out and "OUT:QuickTokenUrl" in out
    # Quick's own client, never the app's service client (which adt-query-gw accepts).
    assert "OUT:QuickClientId" in out and "adt-quick/bridge" in out
    assert "OUT:AppServiceClientId" not in out and "adt/query" not in out
    assert "get-secret-value --secret-id OUT:QuickClientSecretName" in out


@pytest.mark.parametrize("status", ["CREATING", "FAILED", "None", ""])
def test_a_bridge_that_is_not_ready_fails_the_deploy(tmp_path, status):
    out = _summary(tmp_path, status=status)
    assert "FAILED=[AWS Quick query-bridge target" in out


def test_delegated_tokens_do_not_print_cognito_service_values(tmp_path):
    out = _summary(tmp_path, oidc=" -c quick_oidc_audience=x")
    assert "external OIDC provider" in out
    assert "OUT:QuickClientId" not in out and "OUT:AppServiceClientId" not in out
    assert "get-secret-value" not in out


# ── cleanup.sh sweeps the Quick gateway too ──────────────────────────────────

def test_cleanup_sweeps_the_quick_gateway_targets():
    src = (_ADT_ROOT.parent / "cleanup.sh").read_text()
    sweep = src[src.index("OutputKey=='QuickGatewayId'"):]
    assert "list-gateway-targets" in sweep.split("delete-gateway-target")[0]
    assert src.index("OutputKey=='QuickGatewayId'") < src.index("[3/6]")
    assert "sleep 15" not in src[src.index("OutputKey=='QuickGatewayId'"):src.index("[3/6]")]


_CLEAN_START = "# Delete the AWS Quick gateway's targets too"
_CLEAN_END = "# Delete OAuth2 credential provider"


def _cleanup(tmp_path, responses):
    """Run cleanup.sh's Quick block; *responses* are (rc, stdout) per list call."""
    src = (_ADT_ROOT.parent / "cleanup.sh").read_text()
    calls = tmp_path / "calls.log"
    cases = "\n".join(f'    {i + 1}) echo {out!r}; return {rc} ;;' for i, (rc, out) in enumerate(responses))
    body = f'''
REGION=us-east-1
_PROFILE_FLAG=""
QUICK_TARGET_POLLS=4
QUICK_TARGET_POLL_SECONDS=0
N=0
aws() {{
  echo "$*" >> {str(calls)!r}
  if [[ "$1" == "cloudformation" ]]; then echo quick-gw-abc; return 0; fi
  if [[ "$2" == "list-gateway-targets" ]]; then
    # Counted in a file: the block calls aws inside $(...), a subshell.
    N=$(( $(cat {str(tmp_path / "n")!r} 2>/dev/null || echo 0) + 1 ))
    echo "$N" > {str(tmp_path / "n")!r}
    case "$N" in
{cases}
      *) echo None; return 0 ;;
    esac
  fi
  return 0
}}
{src[src.index(_CLEAN_START):src.index(_CLEAN_END)]}
'''
    result = _run(tmp_path, body)
    assert result.returncode == 0, result.stderr
    deletes = [c for c in calls.read_text().splitlines() if "delete-gateway-target" in c]
    return result.stdout, deletes


def test_cleanup_does_not_take_an_unreadable_list_as_empty(tmp_path):
    out, deletes = _cleanup(tmp_path, [(255, ""), (0, "t-1"), (0, "None")])
    assert deletes == ["bedrock-agentcore-control delete-gateway-target --gateway-identifier "
                       "quick-gw-abc --target-id t-1 --region us-east-1"]
    assert "Quick gateway has no targets" in out


def test_cleanup_warns_when_the_list_never_reads(tmp_path):
    out, _ = _cleanup(tmp_path, [(255, "")] * 4)
    assert "still lists targets (unknown)" in out


def test_cleanup_warns_when_targets_remain(tmp_path):
    out, _ = _cleanup(tmp_path, [(0, "t-1")] * 4)
    assert "still lists targets (t-1)" in out
