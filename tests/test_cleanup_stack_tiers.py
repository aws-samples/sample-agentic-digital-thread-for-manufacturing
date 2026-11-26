# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""cleanup.sh deletes every stack the app can deploy, in dependency order.

Every stack app.py synthesizes (all opt-ins on) and every stack id app.py
names must be in exactly one of cleanup.sh's tiers, and every stack must be
in the same or a later tier than each stack that depends on it (within a
tier, wait_stack_delete re-issues a delete CloudFormation canceled because
an export is still imported), so no stack is deleted in an earlier tier
than a stack that depends on it, and none (least of all ADTNetworkStack, which owns the account's API Gateway CloudWatch role
setting outside the workshop) is deleted while a dependent stack remains.
The final remaining-stack check covers every tier.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from _app_synth import synth_app

REPO_ROOT = Path(__file__).resolve().parents[1]
CLEANUP = REPO_ROOT / "cleanup.sh"
TIER_NAMES = ("TIER0", "TIER0B", "TIER1", "TIER2", "TIER3")

#: Every opt-in that adds a stack (the custom-domain certificate stack needs
#: an explicit account, so it is covered by the app.py id scan below).
ALL_OPT_INS = {
    "with_simulator": "true", "quick_api_enabled": "true",
    "tulip_enabled": "true", "highbyte_enabled": "true",
    "sfc_enabled": "true", "deploy_identity_center": "true", "highbyte_accept_eula": "true",
    "identity_center_instance_arn": "arn:aws:sso:::instance/ssoins-0000000000000000",
    "identity_center_store_id": "d-0000000000",
}


def tiers() -> list[list[str]]:
    text = CLEANUP.read_text()
    out = []
    for name in TIER_NAMES:
        m = re.search(rf'^{name}="([^"]*)"', text, re.M)
        assert m, f"cleanup.sh has no {name}="
        out.append(m.group(1).split())
    return out


def tier_of() -> dict[str, int]:
    found = {}
    for i, stacks in enumerate(tiers()):
        for s in stacks:
            assert s not in found, f"{s} is in two tiers"
            found[s] = i
    return found


@pytest.fixture(scope="module")
def manifest(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("cleanup_tiers")
    synth_app(out, ALL_OPT_INS)
    return json.loads((out / "manifest.json").read_text())


def _stacks(manifest) -> dict[str, list[str]]:
    arts = manifest["artifacts"]
    return {n: [d for d in a.get("dependencies", []) if arts.get(d, {}).get("type") ==
                "aws:cloudformation:stack"]
            for n, a in arts.items() if a.get("type") == "aws:cloudformation:stack"}


def test_every_synthesized_stack_is_in_a_cleanup_tier(manifest):
    missing = sorted(set(_stacks(manifest)) - set(tier_of()))
    assert not missing, f"cleanup.sh deletes no tier holding {missing}"


def test_every_stack_id_app_py_names_is_in_a_cleanup_tier():
    ids = set(re.findall(r'\(\s*app,\s*"([A-Za-z0-9]+Stack)"', (REPO_ROOT / "app.py").read_text()))
    assert "ADTCertificateStack" in ids and "ADTNetworkStack" in ids
    missing = sorted(ids - set(tier_of()))
    assert not missing, f"cleanup.sh deletes no tier holding {missing}"


def test_a_stack_is_deleted_after_every_stack_that_depends_on_it(manifest):
    order = tier_of()
    for stack, deps in _stacks(manifest).items():
        for dep in deps:
            assert order[stack] <= order[dep], (
                f"{stack} (tier {order[stack]}) depends on {dep} (tier {order[dep]}): "
                f"{dep} must not be in an earlier tier")
            if dep == "ADTNetworkStack":
                assert order[stack] < order[dep], stack


def test_the_certificate_stack_waits_for_its_consumers():
    order = tier_of()
    for consumer in ("ADTComputeStack", "ADTERPDashboardStack", "ADTHighByteOpcuaStack"):
        assert order[consumer] < order["ADTCertificateStack"]


def test_the_network_stack_is_last_and_guarded():
    order = tier_of()
    assert order["ADTNetworkStack"] == max(order.values())
    assert [s for s, t in order.items() if t == order["ADTNetworkStack"]] == ["ADTNetworkStack"]
    text = CLEANUP.read_text()
    assert "Keeping ADTNetworkStack: other ADT stacks remain" in text
    assert 'for STACK in $TIER0 $TIER0B $TIER1 $TIER2; do' in text


def test_every_tier_is_deleted_and_checked_at_the_end():
    text = CLEANUP.read_text()
    for name in TIER_NAMES[:-1]:
        assert re.search(rf"^delete_tier \d+ \${name}$", text, re.M), name
    assert 'ADT_STACKS="$TIER0 $TIER0B $TIER1 $TIER2 $TIER3"' in text


def test_the_certificate_stack_is_looked_up_in_us_east_1():
    text = CLEANUP.read_text()
    assert 'if [ "$1" = "ADTCertificateStack" ]; then echo "us-east-1"' in text


@pytest.mark.parametrize("region,kept", [("us-east-1", True), ("us-west-2", False)])
def test_the_certificate_stack_is_touched_only_from_us_east_1(region, kept):
    """The tier definitions and the guard after them, cut from cleanup.sh and
    run in bash: outside us-east-1 the certificate stack leaves tier 2."""
    import shutil
    import subprocess  # nosec B404 - fixed internal argv list only
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not found")
    text = CLEANUP.read_text()
    block = text[text.index('TIER0="'):text.index("delete_tier() {")]
    script = f"set -euo pipefail\nREGION={region}\n{block}\necho \"TIER2=$TIER2\"\n"
    out = subprocess.run([bash, "-c", script], capture_output=True, text=True,  # nosec B603 - fixed script cut from the repo
                         timeout=60, check=True).stdout
    tier2 = out.rsplit("TIER2=", 1)[1].split()
    assert ("ADTCertificateStack" in tier2) is kept, out
    assert "ADTDatabaseStack" in tier2 and "ADTOpcuaServersStack" in tier2
    assert ("Skipping ADTCertificateStack" in out) is not kept


def test_cleanup_names_the_retained_api_gateway_role_and_how_to_delete_it():
    from stacks import _apigw_log_role
    text = CLEANUP.read_text()
    assert f"--path-prefix {_apigw_log_role.ROLE_PATH}" in text
    assert "ADTNetworkStack-" + _apigw_log_role.ID_PREFIX in text
    assert "aws iam delete-role --role-name" in text
