# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The HighByte setup Lambda's project transforms, run under node against the
bundled project (config/highbyte/intelligencehub-configuration.json).

``highbyte/lambda/highbyte-setup/project-transform.mjs`` holds the pure part of
the setup Lambda: OPC UA/Bedrock/SiteWise substitutions, the single Bedrock
connection, model prefixes, the SiteWise trigger switch the workshop sets
(``HIGHBYTE_SITEWISE_PIPELINE=disabled``) and the admin password PUT path.
Skips when node is not on PATH (same precedent as test_quick_header_strip.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = REPO_ROOT / "highbyte" / "lambda" / "highbyte-setup"
MODULE = LAMBDA_DIR / "project-transform.mjs"
CONFIG = REPO_ROOT / "config" / "highbyte" / "intelligencehub-configuration.json"

#: Reads its arguments from ADT_TRANSFORM_ARGS (JSON) and prints one JSON result.
_DRIVER = """
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';
const args = JSON.parse(process.env.ADT_TRANSFORM_ARGS);
const m = await import(pathToFileURL(args.module).href);
const out = {};
if (args.config) {
  const logs = [];
  out.project = m.transformProject(JSON.parse(readFileSync(args.config, 'utf-8')), args.options,
                                   (line) => logs.push(line));
  out.logs = logs;
}
out.paths = (args.admins || []).map((admin) => m.adminUserUpdatePath(admin));
out.env = (args.envValues || []).map((raw) => {
  try { return m.sitewisePipelineFromEnv(raw); } catch (e) { return 'ERROR'; }
});
console.log(JSON.stringify(out));
"""

_OPC = {name: f"opc.tcp://{name}.internal:26543/UA/ManufacturingServer"
        for name in ("opc_milwaukee", "opc_detroit", "opc_cedarrapids", "opc_indianapolis")}


def _run(**args) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    env = {**os.environ, "ADT_TRANSFORM_ARGS": json.dumps({"module": str(MODULE), **args})}
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [node, "--input-type=module", "-e", _DRIVER],
        capture_output=True, text=True, timeout=60, env=env, check=True)
    return json.loads(result.stdout)


def _transform(region: str = "us-east-1", sitewise: str = "enabled") -> dict:
    options = {"region": region, "opcEndpoints": _OPC, "sitewisePipeline": sitewise}
    return _run(config=str(CONFIG), options=options)["project"]["project"]


def _triggers(project: dict) -> dict[str, list[bool]]:
    return {p["name"]: [t["config"]["enabled"] for t in p.get("triggers", [])]
            for p in project["pipelines"]}


def _sitewise_writers(project: dict) -> set[str]:
    """Pipelines that write SiteWise: a stage of connection type
    aws.iotsitewise, a {{Connection.<SiteWise connection>}} reference, or an
    output bound to a SiteWise connection."""
    connections = {c["name"] for c in project["connections"]
                   if c.get("uri", "").startswith("aws.iotsitewise://")}
    outputs = {o["name"] for o in project.get("outputs", []) if o.get("connection") in connections}
    needles = ({'"aws.iotsitewise"'} | {f"Connection.{n}" for n in connections}
               | {f'"{n}"' for n in outputs})
    return {p["name"] for p in project["pipelines"]
            if any(needle in json.dumps(p) for needle in needles)}


def test_admin_password_path_uses_the_api_version_without_a_version_id():
    """4.4.2 returns no versionId; the hub UI sends PUT /v1/user/{id}/42."""
    out = _run(admins=[{"id": "u1", "username": "administrator"},
                       {"id": "u1", "versionId": 7}])
    assert out["paths"] == ["/v1/user/u1/42", "/v1/user/u1/7"]


def test_sitewise_switch_reads_the_env():
    out = _run(envValues=[None, "", "enabled", " Disabled ", "off"])
    assert out["env"] == ["enabled", "enabled", "enabled", "disabled", "ERROR"]


def test_one_bedrock_connection_for_the_deployment_region():
    """Both shipped aws.bedrock connections collapse into Bedrock_<region>, and
    every input that used either of them (including the plain `Bedrock` the
    old prefix match missed) points at it."""
    project = _transform()
    names = [c["name"] for c in project["connections"]]
    assert len(names) == len(set(names)), names
    bedrock = [c for c in project["connections"] if c["uri"].startswith("aws.bedrock://")]
    assert [(c["name"], c["settings"]["region"]) for c in bedrock] == [("Bedrock_N_Virginia", "us-east-1")]
    shipped = json.loads(CONFIG.read_text())["project"]
    old = {c["name"] for c in shipped["connections"] if c["uri"].startswith("aws.bedrock://")}
    assert len(old) == 2
    users = [i for i in shipped["inputs"] if i.get("connection") in old]
    assert users, "the bundled project has Bedrock inputs"
    by_name = {i["name"]: i for i in project["inputs"]}
    assert {by_name[i["name"]]["connection"] for i in users} == {"Bedrock_N_Virginia"}
    assert all(i["connection"] in set(names) for i in project["inputs"]), "dangling input connection"


def test_opc_endpoints_and_regions_are_substituted():
    project = _transform(region="eu-west-1")
    uris = {c["name"]: c["uri"] for c in project["connections"]}
    assert {name: uris[name] for name in _OPC} == _OPC
    regions = {c["name"]: c["settings"]["region"] for c in project["connections"]
               if c["uri"].startswith(("aws.bedrock://", "aws.iotsitewise://"))}
    assert set(regions.values()) == {"eu-west-1"} and "Bedrock_Ireland" in regions
    models = {i["qualifier"]["modelType"] for i in project["inputs"] if "modelType" in i.get("qualifier", {})}
    assert models and all(m.startswith("eu.") for m in models), models


def test_default_keeps_the_sitewise_pipeline_as_shipped():
    """The demo (no HIGHBYTE_SITEWISE_PIPELINE): every trigger as shipped."""
    shipped = json.loads(CONFIG.read_text())["project"]
    assert _triggers(_transform()) == _triggers(shipped)
    assert all(_triggers(shipped)["NameSpace_to_SiteWise"])


def test_disabled_imports_every_sitewise_writer_with_its_triggers_off():
    """The workshop: NameSpace_to_SiteWise (the bundled project's only SiteWise
    writer) has every trigger off, and nothing else changes."""
    shipped = json.loads(CONFIG.read_text())["project"]
    project = _transform(sitewise="disabled")
    assert _sitewise_writers(project) == {"NameSpace_to_SiteWise"}
    triggers = _triggers(project)
    assert triggers["NameSpace_to_SiteWise"] and not any(triggers["NameSpace_to_SiteWise"])
    expected = {**_triggers(shipped), "NameSpace_to_SiteWise": triggers["NameSpace_to_SiteWise"]}
    assert triggers == expected


def test_the_handler_uses_the_module():
    """index.mjs delegates to project-transform.mjs (no second copy of the
    substitutions, and no `${adminUser.versionId}` path)."""
    source = (LAMBDA_DIR / "index.mjs").read_text()
    assert 'from "./project-transform.mjs"' in source
    assert "adminUser.versionId}" not in source
    assert "aws.bedrock://" not in source
    assert "process.env.HIGHBYTE_SITEWISE_PIPELINE" in source
