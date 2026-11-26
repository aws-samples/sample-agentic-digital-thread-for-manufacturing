# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Workshop mode: one parameter picks which stack owns each group of VPC endpoints.

A fresh Workshop Studio create wants the S3 gateway and ECR api/dkr endpoints in
ADTNetworkStack (the root's VPC CodeBuild runs long before ADTComputeStack). A
stack created before that move holds them in ADTComputeStack, and updating it to
a Network-only template failed twice over: Network dropped the route-table
exports Compute's S3 endpoint still imports ("Cannot delete export ... in use"),
and a second private-DNS ECR endpoint / S3 gateway route cannot exist next to
Compute's. stacks/_build_path_endpoints.py keeps both placements in the
templates under a ``BuildPathEndpoints`` condition.

A fresh deployment, 2026-11-11: Compute's private-DNS Secrets Manager endpoint came up while
ADTERPLambdaStack's story Lambda (started at the same moment) was calling
Secrets Manager; the call timed out and the create failed. So a fresh create
(``network-all``) also puts the Secrets Manager and Neptune Analytics endpoints
in Network, and ``network`` keeps the placement stacks created between the two
moves have (service endpoints in Compute).

These tests synthesize the whole app (offline, no AWS calls) in base and workshop
mode and pin: the logical ids each placement already uses, exactly one owner for
every endpoint and every parameter value, and the route-table exports a pre-move
stack imports.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"

PARAMETER = "BuildPathEndpoints"
VALUES = ["network", "compute", "network-all"]
#: The logical ids a pre-move workshop stack holds in ADTComputeStack, and the
#: ids a post-move fresh create holds in ADTNetworkStack (same construct ids).
#: A placement that changes them replaces live endpoints on update.
ENDPOINT_IDS = {
    "S3EndpointD570F362": "s3",
    "EcrApi0554F9AF": "ecr.api",
    "EcrDkrCFBBD657": "ecr.dkr",
}
ENDPOINT_SECURITY_GROUP_IDS = {"EcrApiSecurityGroup329B9EB2", "EcrDkrSecurityGroupC80DDF1B"}
BUILD_PATH_IDS = set(ENDPOINT_IDS) | ENDPOINT_SECURITY_GROUP_IDS
#: Secrets Manager + Neptune Analytics: the ids every workshop stack created so
#: far holds in ADTComputeStack (a fresh create now holds them in Network).
SERVICE_ENDPOINT_IDS = {
    "SecretsEndpointE4AF69EA": "secretsmanager",
    "NeptuneGraphData0827A759": "neptune-graph-data",
    "NeptuneGraphAAAEC4F2": "neptune-graph",
}
SERVICE_SECURITY_GROUP_IDS = {
    "SecretsEndpointSecurityGroup13D33E52",
    "NeptuneGraphDataSecurityGroupB2CC5771",
    "NeptuneGraphSecurityGroupD285011E",
}
SERVICE_IDS = set(SERVICE_ENDPOINT_IDS) | SERVICE_SECURITY_GROUP_IDS
ALL_IDS = BUILD_PATH_IDS | SERVICE_IDS
ALL_ENDPOINT_IDS = {**ENDPOINT_IDS, **SERVICE_ENDPOINT_IDS}
STACKS = {"network": "ADTNetworkStack", "compute": "ADTComputeStack"}
#: {parameter value: {stack: the ids it creates}}: what each placement means.
PLACEMENT = {
    "compute": {"ADTNetworkStack": set(), "ADTComputeStack": ALL_IDS},
    "network": {"ADTNetworkStack": BUILD_PATH_IDS, "ADTComputeStack": SERVICE_IDS},
    "network-all": {"ADTNetworkStack": ALL_IDS, "ADTComputeStack": set()},
}

_CACHE: dict[str, dict[str, dict]] = {}


def _offline_docker_stub(out_dir: Path) -> Path:
    stub = out_dir / "offline-docker"
    stub.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    *:/asset-output:*) out=${arg%%:/asset-output:*}; "
        "mkdir -p \"$out\"; : > \"$out/.synth-placeholder\" ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _synth(tmp_path_factory, mode: str) -> dict[str, dict]:
    """{stack name: template} for ADTNetworkStack and ADTComputeStack."""
    if mode in _CACHE:
        return _CACHE[mode]
    cdk_bin = str(CDK_BIN) if CDK_BIN.exists() else shutil.which("cdk")
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")
    out_dir = tmp_path_factory.mktemp(f"cdk_synth_build_path_{mode}")
    context = ["-c", "with_workshop=true", "-c", "workshop_story=live"] if mode == "workshop" else []
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CDK_DOCKER": str(_offline_docker_stub(out_dir)),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [cdk_bin, "synth", "ADTComputeStack", "-c", "region=us-east-1", *context,
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    templates = {}
    for stack in STACKS.values():
        path = out_dir / f"{stack}.template.json"
        if result.returncode != 0 or not path.exists():
            pytest.fail(f"synth did not produce {path.name}:\n{result.stderr[-4000:]}")
        templates[stack] = json.loads(path.read_text(encoding="utf-8"))
    _CACHE[mode] = templates
    return templates


@pytest.fixture(scope="module")
def workshop(tmp_path_factory) -> dict[str, dict]:
    return _synth(tmp_path_factory, "workshop")


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> dict[str, dict]:
    return _synth(tmp_path_factory, "base")


def _holds(template: dict, expression, value: str) -> bool:
    """Evaluate a condition expression (Fn::Equals on the parameter, Fn::Or,
    Fn::Not, Condition) for ``BuildPathEndpoints == value``."""
    (fn, args), = expression.items()
    if fn == "Fn::Equals":
        left, right = args
        assert left == {"Ref": PARAMETER}, f"{expression} is not a test of {PARAMETER}"
        return right == value
    if fn == "Fn::Or":
        return any(_holds(template, a, value) for a in args)
    if fn == "Fn::Not":
        return not _holds(template, args[0], value)
    if fn == "Condition":
        return _holds(template, template["Conditions"][args], value)
    raise AssertionError(f"unexpected condition function {fn}")


def _created(template: dict, logical_id: str, value: str) -> bool:
    """Whether ``logical_id`` exists when the stack's BuildPathEndpoints is ``value``."""
    name = template["Resources"][logical_id].get("Condition")
    if name is None:
        return True
    return _holds(template, template["Conditions"][name], value)


def _route_table_exports(template: dict) -> set[str]:
    return {o["Export"]["Name"] for o in template.get("Outputs", {}).values()
            if "Export" in o and "RouteTable" in str(o["Export"]["Name"])}


def test_standalone_templates_keep_the_endpoints_in_compute_unconditionally(base):
    network, compute = base["ADTNetworkStack"], base["ADTComputeStack"]
    assert ALL_IDS <= set(compute["Resources"]), sorted(ALL_IDS - set(compute["Resources"]))
    for logical_id in ALL_IDS:
        assert "Condition" not in compute["Resources"][logical_id], logical_id
    assert not ALL_IDS & set(network["Resources"])
    for template in (network, compute):
        assert PARAMETER not in template.get("Parameters", {})


@pytest.mark.parametrize("owner", ["network", "compute"])
def test_workshop_stacks_declare_the_parameter_with_the_compute_default(workshop, owner):
    """A raw ``cdk deploy`` / deploy.sh --workshop keeps the placement such a
    deploy always had; only the Workshop Studio root sends ``network-all``."""
    parameter = workshop[STACKS[owner]]["Parameters"][PARAMETER]
    assert parameter["Type"] == "String"
    assert parameter["AllowedValues"] == VALUES
    assert parameter["Default"] == "compute"
    assert parameter["Description"].isascii()


@pytest.mark.parametrize("owner", ["network", "compute"])
def test_each_workshop_stack_keeps_the_logical_ids_its_placement_already_used(workshop, owner):
    """Same logical ids as before, so no kind of existing stack sees an
    endpoint replaced: pre-move stacks hold them in Compute, fresh creates
    hold them in Network."""
    resources = workshop[STACKS[owner]]["Resources"]
    assert ALL_IDS <= set(resources), sorted(ALL_IDS - set(resources))


@pytest.mark.parametrize("value", VALUES)
def test_each_value_creates_exactly_its_placement(workshop, value):
    """Every value keeps its meaning (an update with the value a stack was
    created with moves nothing), and no endpoint is created twice or never."""
    for stack, expected in PLACEMENT[value].items():
        created = {lid for lid in ALL_IDS if _created(workshop[stack], lid, value)}
        assert created == expected, (stack, sorted(created ^ expected))


def test_compute_keeps_the_service_endpoints_for_every_value_but_network_all(workshop):
    """Stacks created with ``network`` or ``compute`` hold Secrets Manager and
    Neptune in Compute; their updates must not move them."""
    compute = workshop["ADTComputeStack"]
    for lid in SERVICE_IDS:
        assert _created(compute, lid, "network") and _created(compute, lid, "compute"), lid
        assert not _created(compute, lid, "network-all"), lid


def test_no_other_endpoint_for_these_services_is_left_unconditional(workshop):
    """A stray unconditioned copy would collide with the owner's endpoint."""
    suffixes = [f".{s}\"" for s in ALL_ENDPOINT_IDS.values()]
    for stack, template in workshop.items():
        for logical_id, resource in template["Resources"].items():
            if resource["Type"] != "AWS::EC2::VPCEndpoint" or logical_id in ALL_ENDPOINT_IDS:
                continue
            if resource["Properties"].get("PrivateDnsEnabled") is False:
                continue  # a mock's own endpoint: no private DNS, no collision
            service = json.dumps(resource["Properties"]["ServiceName"])
            for suffix in suffixes:
                assert suffix not in service, f"{stack}/{logical_id} is another {suffix} endpoint"


def test_every_private_dns_endpoint_is_private_dns_in_either_placement(workshop):
    """The workloads reach these services by their public names; private DNS
    is what routes those names to the endpoint."""
    for stack in STACKS.values():
        for lid in set(ALL_ENDPOINT_IDS) - {"S3EndpointD570F362"}:
            assert workshop[stack]["Resources"][lid]["Properties"]["PrivateDnsEnabled"] is True


def test_workshop_network_keeps_the_route_table_exports_a_pre_move_stack_imports(base, workshop):
    """The update failure: with no Compute reference to the route tables, the
    Network update tried to delete exports the old Compute S3 endpoint uses."""
    expected = _route_table_exports(base["ADTNetworkStack"])
    assert expected, "standalone Network exports no route tables; the pin is stale"
    assert expected <= _route_table_exports(workshop["ADTNetworkStack"])


def test_the_workshop_s3_gateway_routes_the_same_tables_in_either_placement(workshop):
    """Both placements attach the S3 gateway to every route table (public and
    private), so switching owners never changes which subnets it serves."""
    def tables(stack: str) -> list[str]:
        ids = workshop[stack]["Resources"]["S3EndpointD570F362"]["Properties"]["RouteTableIds"]
        return sorted(json.dumps(i, sort_keys=True) for i in ids)
    network = tables("ADTNetworkStack")
    compute = tables("ADTComputeStack")
    assert len(network) == len(compute) == 4
    assert all("Fn::ImportValue" in t for t in compute)
    assert all("Ref" in t for t in network)


def test_the_service_endpoints_match_in_either_placement(workshop):
    """Same service, subnets and security-group ingress wherever they live, so
    a workload sees the same endpoint whichever stack created it."""
    def shape(stack: str, lid: str) -> dict:
        props = dict(workshop[stack]["Resources"][lid]["Properties"])
        props.pop("SecurityGroupIds", None)
        props.pop("SubnetIds", None)
        props.pop("VpcId", None)
        return props
    for lid in SERVICE_ENDPOINT_IDS:
        assert shape("ADTNetworkStack", lid) == shape("ADTComputeStack", lid), lid
        for template in (workshop["ADTNetworkStack"], workshop["ADTComputeStack"]):
            subnets = template["Resources"][lid]["Properties"]["SubnetIds"]
            assert len(subnets) == 2, lid
