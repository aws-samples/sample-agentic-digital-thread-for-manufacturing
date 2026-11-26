# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ERP dashboard's SQL console gets its own login secret.

The console runs operator SQL as a SELECT-only database role, never as the
admin login (erp_simulation/console_login.py). The dashboard stack generates
that role's credential and hands the task its ARN; the task role may read it;
no other role's secret-name wildcard matches its generated name.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _load_synth_helper():
    spec = importlib.util.spec_from_file_location(
        "_cognito_domain_pinning", _HERE / "test_cognito_domain_pinning.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._synth_template


@pytest.fixture(scope="module")
def erp_dashboard_template(tmp_path_factory) -> dict:
    return _load_synth_helper()(
        tmp_path_factory,
        stack="ADTERPDashboardStack",
        context={"region": "us-east-1", "with_simulator": "true"},
        output_slug="cdk_synth_erp_console_login",
    )


def _console_secret(template: dict) -> tuple[str, dict]:
    found = [(lid, r) for lid, r in template["Resources"].items()
             if r["Type"] == "AWS::SecretsManager::Secret" and lid.startswith("ConsoleLoginSecret")]
    assert len(found) == 1, found
    return found[0]


def test_console_secret_is_generated_for_the_fixed_role(erp_dashboard_template):
    lid, secret = _console_secret(erp_dashboard_template)
    props = secret["Properties"]
    gen = props["GenerateSecretString"]
    assert json.loads(gen["SecretStringTemplate"]) == {"username": "erp_console"}
    assert gen["GenerateStringKey"] == "password"
    assert gen["ExcludePunctuation"] is True and gen["IncludeSpace"] is False
    assert "Name" not in props, "read by ARN; a fixed name could fall under another grant"
    assert not lid.startswith(("DatabaseSecret", "AdtDbSecret")), lid


def test_task_gets_the_secret_arn_and_read_access(erp_dashboard_template):
    lid, _ = _console_secret(erp_dashboard_template)
    (taskdef,) = [r for r in erp_dashboard_template["Resources"].values()
                  if r["Type"] == "AWS::ECS::TaskDefinition"]
    env = {e["Name"]: e["Value"] for e in taskdef["Properties"]["ContainerDefinitions"][0]["Environment"]}
    assert env["CONSOLE_DB_SECRET_NAME"] == {"Ref": lid}
    assert "CONSOLE_DB_PASSWORD" not in env, "the plain-env password is for local development only"
    readers = []
    for res in erp_dashboard_template["Resources"].values():
        if res["Type"] != "AWS::IAM::Policy":
            continue
        for st in res["Properties"]["PolicyDocument"]["Statement"]:
            resources = st.get("Resource")
            resources = resources if isinstance(resources, list) else [resources]
            if {"Ref": lid} in resources:
                readers.append((res["Properties"]["Roles"], st["Action"]))
    assert readers, "the dashboard task role must be able to read the console secret"
    for roles, actions in readers:
        assert len(roles) == 1 and "TaskRole" in roles[0]["Ref"], roles
        assert "secretsmanager:GetSecretValue" in actions


def test_console_secret_skips_are_documented(erp_dashboard_template):
    _, secret = _console_secret(erp_dashboard_template)
    meta = secret.get("Metadata", {})
    skips = {e["id"]: e["comment"] for e in meta.get("checkov", {}).get("skip", [])}
    assert {"CKV2_AWS_57", "CKV_AWS_149"} <= set(skips)
    assert all(len(skips[k]) > 40 for k in ("CKV2_AWS_57", "CKV_AWS_149"))
    nag = {r["id"] for r in meta.get("cdk_nag", {}).get("rules_to_suppress", [])}
    assert "AwsSolutions-SMG4" in nag
