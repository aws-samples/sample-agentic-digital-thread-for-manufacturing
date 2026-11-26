# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The operator scripts pick the adt-app Cognito client and gateway by NAME.

The adt-app pool has several client_credentials clients: the app's own
``adt-app-service-v2``, the retired ``adt-app-service`` that no gateway accepts
any more, and on a Quick deploy Quick's ``adt-quick-service``, which only
``adt-quick-gw`` accepts. "The first client_credentials client" would pick any
of them depending on list order. So ``scripts/mint-query-token.sh`` and
``scripts/quick-integration.sh`` (and the refresh helper it bakes) select
``CLIENT_NAME`` (default ``adt-app-service-v2``).

``mint-query-token.sh`` names its gateway too: ``adt-query-gw`` with the app's
client by default (row (a) of docs/quick-integration.md), and with ``--quick``
Quick's own client, scope and ``adt-quick-gw`` (row (b)). The client secret
reaches curl on stdin, never in its argv.

The mint script runs end to end against a fake ``aws`` (real JMESPath for
``--query``) and a fake ``curl``; no AWS call, no network.
"""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed argv lists against local fakes only
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_MINT = _REPO / "scripts" / "mint-query-token.sh"
_INTEGRATION = _REPO / "scripts" / "quick-integration.sh"

_QUERY_URL = "https://gw-query.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
_QUICK_URL = "https://gw-quick.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

# Quick's client is listed FIRST, the order that made the old loop pick it.
_FAKE_AWS = r'''#!{python}
import json, sys
import jmespath

args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


clients = [
    {{"ClientId": "quick-id", "ClientName": "adt-quick-service"}},
    {{"ClientId": "retired-id", "ClientName": "adt-app-service"}},
    {{"ClientId": "service-id", "ClientName": "adt-app-service-v2"}},
    {{"ClientId": "login-id", "ClientName": "adt-app-client"}},
]
if "list-user-pools" in args:
    data = {{"UserPools": [{{"Id": "us-east-1_pool", "Name": "adt-app"}}]}}
elif "list-user-pool-clients" in args:
    data = {{"UserPoolClients": clients}}
elif "describe-user-pool-client" in args:
    cid = opt("--client-id")
    flows = ["client_credentials"] if cid != "login-id" else []
    data = {{"UserPoolClient": {{"ClientId": cid, "AllowedOAuthFlows": flows,
                                 "ClientSecret": "secret-" + cid}}}}
elif "describe-user-pool" in args:
    data = {{"UserPool": {{"Domain": "adt-app-test"}}}}
elif "list-gateways" in args:
    data = {{"items": [{{"name": "adt-query-gw", "gatewayId": "gw-query"}},
                       {{"name": "adt-quick-gw", "gatewayId": "gw-quick"}}]}}
else:
    sys.exit("unexpected aws call: " + " ".join(args))
query = opt("--query")
result = jmespath.search(query, data) if query else data
if opt("--output") == "text":
    if result is None:
        print("None")
    elif isinstance(result, list):
        print("\t".join(str(x) for x in result))
    else:
        print(result)
else:
    print(json.dumps(result))
'''

# Echoes what it was given (argv, and stdin when a field is read from it) as
# the token endpoint's JSON.
_FAKE_CURL = r'''#!{python}
import json, sys
argv = sys.argv[1:]
stdin = sys.stdin.read() if any(a.endswith("@-") for a in argv) else ""
fields = [a for a in argv if "=" in a and not a.startswith("http")]
print(json.dumps({{"access_token": "token", "fields": fields, "argv": argv, "stdin": stdin}}))
'''


def _fakes(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        for name, body in (("aws", _FAKE_AWS), ("curl", _FAKE_CURL)):
            path = bin_dir / name
            path.write_text(body.format(python=sys.executable))
            path.chmod(0o755)
        (bin_dir / "python3").symlink_to(sys.executable)
    return {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(tmp_path), "AWS_REGION": "us-east-1"}


def _run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 - fixed argv, local fakes on PATH
        ["bash", str(_MINT), *args], env={**_fakes(tmp_path), **env},
        capture_output=True, text=True, timeout=60, check=False)


def _token_request(tmp_path: Path, *args: str, **env: str) -> dict:
    result = _run(tmp_path, "--json", *args, **env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_mint_uses_the_app_service_client_even_when_quick_is_listed_first(tmp_path):
    request = _token_request(tmp_path)
    assert "client_id=service-id" in request["fields"]
    assert "scope=adt/query" in request["fields"]
    assert "quick-id" not in json.dumps(request)


def test_mint_never_picks_the_retired_service_client(tmp_path):
    """adt-app-service (listed before adt-app-service-v2) is retired: no
    gateway accepts it, so a token minted with it would only be refused."""
    for args in ((), ("--quick",)):
        assert "retired-id" not in json.dumps(_token_request(tmp_path, *args))


def test_mint_sends_the_client_secret_on_stdin_never_in_argv(tmp_path):
    request = _token_request(tmp_path)
    assert request["stdin"] == "secret-service-id"
    assert "client_secret@-" in request["argv"]
    assert not [a for a in request["argv"] if "secret-service-id" in a]


def test_default_mint_prints_the_shared_query_gateway(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Authorization: Bearer token\n"
    assert f"# Gateway MCP URL: {_QUERY_URL}" in result.stderr
    assert "gw-quick" not in result.stderr


def test_quick_mint_uses_quicks_client_scope_and_gateway(tmp_path):
    request = _token_request(tmp_path, "--quick")
    assert "client_id=quick-id" in request["fields"]
    assert "scope=adt-quick/bridge" in request["fields"]
    assert request["stdin"] == "secret-quick-id"
    result = _run(tmp_path, "--quick")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Authorization: Bearer token\n"
    assert f"# Gateway MCP URL: {_QUICK_URL}" in result.stderr
    assert "gw-query" not in result.stderr
    assert "client=quick-id scope=adt-quick/bridge" in result.stderr


def test_the_gateway_can_be_named_explicitly(tmp_path):
    result = _run(tmp_path, GATEWAY_NAME="adt-quick-gw")
    assert result.returncode == 0, result.stderr
    assert f"# Gateway MCP URL: {_QUICK_URL}" in result.stderr


def test_a_missing_gateway_is_reported(tmp_path):
    result = _run(tmp_path, GATEWAY_NAME="adt-nope-gw")
    assert result.returncode == 0, result.stderr
    assert "gateway 'adt-nope-gw' not found" in result.stderr
    assert "Gateway MCP URL" not in result.stderr


def test_mint_fails_loud_on_an_unknown_client_name(tmp_path):
    result = _run(tmp_path, "--json", CLIENT_NAME="nope")
    assert result.returncode != 0
    assert "client 'nope' not found" in result.stderr


def test_mint_refuses_an_unknown_argument(tmp_path):
    result = _run(tmp_path, "--quik")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


@pytest.mark.parametrize("site", ["installer", "baked refresh helper"])
def test_quick_integration_selects_the_client_by_name(site):
    text = _INTEGRATION.read_text()
    helper_start = text.index('cat > "$REFRESH" <<EOF')
    body = text[:helper_start] if site == "installer" else text[helper_start:]
    assert "ClientName==" in body
    assert "*client_credentials*)" not in body
    assert "CLIENT_NAME" in body
    assert 'CLIENT_NAME="${CLIENT_NAME:-adt-app-service-v2}"' in text
