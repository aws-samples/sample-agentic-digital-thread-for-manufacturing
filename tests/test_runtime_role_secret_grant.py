# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth tests for the runtime-role demo-DB-secret grants (case-sensitivity fix).

IAM resource matching is CASE-SENSITIVE. Records-first demo System nodes carry a
``secret_arn`` pointer to the CDK-generated demo DB owner secrets, whose
CloudFormation-generated names start with a CAPITAL letter:

* seed-DB owner  → ``AdtDbSecret…``   (``adt_seed_data_stack.py``, no secret_name);
* ERP owner      → ``DatabaseSecret…`` (``erp_database_stack.py``, no secret_name).

The runtime role's ``secret:adt*`` wildcard therefore does NOT cover them.
The onboarding runtime's discover path reads them, so its role carries
account/region-bounded ``secret:AdtDbSecret*`` and ``secret:DatabaseSecret*``
name-wildcard variants in a read-only statement, deliberately NOT object
references, so the engine stays demo-agnostic and the demo stacks remain
removable on a flag-less deploy (ADR-0030: a cross-stack export would block
that removal with export-in-use).

The shared orchestrator/system runtime role reads neither: a system agent
connects with its schema's read-only login, carried in its agent-config
secret (stacks/_db_readers.py), never with the owner's.

Synth assertions go through the CDK CLI subprocess with
AWS config pointed at /dev/null (mirrors tests/test_cross_account_grant.py) so
the SSM-backed sticky-context read fails soft and cannot leak ambient state.

Run:  PYTHONPATH=. python -m pytest tests/test_runtime_role_secret_grant.py -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
AGENTCORE_STACK = "ADTAgentCoreStack"
ONBOARDING_STACK = "ADTOnboardingStack"

# Case-sensitive resource suffixes under test (account/region prefix varies
# with the synth account — placeholder accounts like 111122223333 in fixtures).
LOWER_ADT = ":secret:adt*"
UPPER_ADT = ":secret:AdtDbSecret*"
ERP_OWNER = ":secret:DatabaseSecret*"


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


def _cached_lookup_account() -> str | None:
    """Account id whose context lookups are already cached in cdk.context.json.

    Mirrors tests/test_cross_account_grant.py's approach: the app constructs
    stacks that perform context lookups (availability-zones for the VPC); with
    the credential env scrubbed, pinning CDK_DEFAULT_ACCOUNT to an account with
    cached lookups keeps the synth fully offline. Parsed from the repo's own
    lookup cache, never hardcoded.
    """
    ctx_path = REPO_ROOT / "cdk.context.json"
    if not ctx_path.exists():
        return None
    for key in json.loads(ctx_path.read_text()):
        m = re.match(
            r"availability-zones:account=(\d+):region=us-east-1",
            key,
        )
        if m:
            return m.group(1)
    return None


def _synth_templates(tmp_dir: Path) -> dict[str, dict]:
    """Synthesize both stacks in ONE CDK CLI invocation; return name→template."""
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")

    account = _cached_lookup_account()
    if not account:
        pytest.skip("no cached SSM lookup account in cdk.context.json for offline synth")

    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "CDK_DEFAULT_ACCOUNT": account,
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
        # Deterministic offline synth: block ambient credentials so app.py's
        # SSM sticky-context read fails soft instead of picking up persisted
        # context from a dev account.
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    for key in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN"):
        env.pop(key, None)

    args = [
        cdk_bin, "synth", AGENTCORE_STACK, ONBOARDING_STACK,
        "-c", "region=us-east-1",
        "--output", str(tmp_dir), "--quiet",
    ]
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        args, cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=600,
    )
    templates: dict[str, dict] = {}
    for stack in (AGENTCORE_STACK, ONBOARDING_STACK):
        template_path = tmp_dir / f"{stack}.template.json"
        if not template_path.exists():
            pytest.skip(f"synth did not produce {stack}:\n{result.stderr[-2000:]}")
        templates[stack] = json.loads(template_path.read_text())
    return templates


def _get_secret_value_statements(template: dict) -> list[dict]:
    """All secretsmanager:GetSecretValue statements across IAM roles/policies.

    Covers BOTH declaration shapes: inline_policies (embedded in the
    AWS::IAM::Role ``Policies`` property) and add_to_role_policy
    (standalone AWS::IAM::Policy resources).
    """
    statements: list[dict] = []
    for resource in template.get("Resources", {}).values():
        docs: list[dict] = []
        if resource.get("Type") == "AWS::IAM::Role":
            for policy in resource.get("Properties", {}).get("Policies", []) or []:
                docs.append(policy.get("PolicyDocument", {}))
        elif resource.get("Type") == "AWS::IAM::Policy":
            docs.append(resource.get("Properties", {}).get("PolicyDocument", {}))
        for doc in docs:
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "secretsmanager:GetSecretValue" in actions:
                    statements.append(stmt)
    return statements


def _flat_resources(stmt: dict) -> list[str]:
    """Statement resources as flat strings (tolerates Fn::Join token shapes)."""
    res = stmt.get("Resource", [])
    if isinstance(res, (str, dict)):
        res = [res]
    return [r if isinstance(r, str) else json.dumps(r) for r in res]


def _statements_granting(template: dict, suffix: str) -> list[dict]:
    return [
        s for s in _get_secret_value_statements(template)
        if any(suffix in r for r in _flat_resources(s))
    ]


@pytest.fixture(scope="session")
def templates(tmp_path_factory) -> dict[str, dict]:
    return _synth_templates(tmp_path_factory.mktemp("secret_grant"))


# ---------------------------------------------------------------------------
# Runtime role (ADTAgentCoreStack): orchestrator and system runtimes
# ---------------------------------------------------------------------------
def test_runtime_role_keeps_the_lowercase_adt_grant(templates):
    """``secret:adt*`` covers the per-system agent-config and connection
    secrets every system runtime reads at start."""
    assert _statements_granting(templates[AGENTCORE_STACK], LOWER_ADT)


def test_runtime_role_reads_no_demo_database_owner_secret(templates):
    """A system agent connects with its schema's read-only login, which the
    app copies into its agent-config secret (stacks/_db_readers.py); a
    PostgreSQL runtime makes no other secret read at start. So the shared
    runtime role must not be able to read either owner secret: model-written
    SQL can then never run as the owner, whatever the runtime holds."""
    template = templates[AGENTCORE_STACK]
    for suffix in (UPPER_ADT, ERP_OWNER):
        assert not _statements_granting(template, suffix), (
            f"{AGENTCORE_STACK} still grants {suffix}: system runtimes must not read the "
            "database owner's secret")


# ---------------------------------------------------------------------------
# Onboarding runtime role (ADTOnboardingStack) — discover-path reader
# ---------------------------------------------------------------------------
def test_onboarding_role_grants_owner_variants_read_only(templates):
    """The onboarding runtime's discover path resolves records-first nodes
    (onboard_agent.discover → resolve_connection → _discover_postgres), so it
    needs READ on Adt*/DatabaseSecret* — and MUST NOT get write actions on the
    owner secrets (they live in a separate GetSecretValue-only statement)."""
    template = templates[ONBOARDING_STACK]
    for suffix in (UPPER_ADT, ERP_OWNER):
        granting = _statements_granting(template, suffix)
        assert granting, f"no GetSecretValue statement grants {suffix} in {ONBOARDING_STACK}"
        for stmt in granting:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            write_actions = {a for a in actions if a not in (
                "secretsmanager:GetSecretValue",
            )}
            assert not write_actions, (
                f"owner-secret statement for {suffix} must be read-only, "
                f"found extra actions: {sorted(write_actions)}"
            )


def _neptune_actions(template: dict) -> set[str]:
    """Every neptune-graph:* action granted anywhere in one synthesized template."""
    return set(re.findall(r"neptune-graph:([A-Za-z]+)", json.dumps(template)))


def test_agentcore_runtime_role_graph_tier_is_write_not_delete(templates):
    """ADR-0069 least privilege: the orchestrator/system runtime only
    CREATE/MERGE/SETs proposals and usage (self_learning.log_proposed_mapping /
    record_rule_usage); accept and replace-not-accrete deletes run in the app
    process. The shared runtime role therefore carries the write tier from the
    ONE stacks/_graph_access.py helper and must NEVER gain DeleteDataViaQuery."""
    actions = _neptune_actions(templates[AGENTCORE_STACK])
    assert {"ExecuteQuery", "ReadDataViaQuery", "WriteDataViaQuery"} <= actions, actions
    assert "DeleteDataViaQuery" not in actions, actions


def test_onboarding_runtime_role_graph_tier_includes_delete(templates):
    """ADR-0069: the onboarding runtime role carries the delete tier (the only
    runtime role that does). It was granted for reset_system's DETACH DELETE;
    reset_system now runs in the app, not on this runtime (RUNTIME_TOOLS). The
    tier is kept until a follow-up checks whether any RUNTIME_TOOLS path still
    deletes."""
    actions = _neptune_actions(templates[ONBOARDING_STACK])
    assert "DeleteDataViaQuery" in actions, actions
    assert "WriteDataViaQuery" in actions, actions
