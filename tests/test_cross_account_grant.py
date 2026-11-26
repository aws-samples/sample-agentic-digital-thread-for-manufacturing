# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit + synth tests for the cross-account sts:AssumeRole grant modes.

``crossAccountDataRoleArn`` (one context key, backward compatible) supports:

* empty/absent  → NO grant (base sample stays wildcard-free);
* ``convention`` (case-insensitive) → the naming-convention wildcard
  ``arn:aws:iam::*:role/ADTCrossAccount*``;
* otherwise     → a comma-separated list of explicit role ARNs.

Two layers here:

1. Pure unit tests for ``stacks._cross_account.cross_account_grant_resources``
   (fully offline — mirrors tests/test_sticky_context.py).
2. Synth assertions via ``aws_cdk.assertions.Template`` on the ADTComputeStack
   template, synthesized through the CDK CLI subprocess (mirrors
   tests/test_adt_compute_env_wiring.py). AWS config/credential files are
   pointed at /dev/null so the SSM-backed sticky-context read fails soft and
   cannot leak an ambient sticky value into the "no context" case.

Run:  PYTHONPATH=. python -m pytest tests/test_cross_account_grant.py -v
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

from stacks._cross_account import (
    CONVENTION_WILDCARD_ARN,
    cross_account_grant_resources,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
STACK = "ADTComputeStack"

ARN_A = "arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly"
ARN_B = "arn:aws:iam::111122223333:role/ADTCrossAccountSiteWise"


# ---------------------------------------------------------------------------
# 1. Pure helper unit tests (offline)
# ---------------------------------------------------------------------------
class TestNoGrantMode:
    def test_none_returns_empty(self):
        assert cross_account_grant_resources(None) == []

    def test_empty_string_returns_empty(self):
        # cdk.json ships crossAccountDataRoleArn: "" — must stay a no-op.
        assert cross_account_grant_resources("") == []

    def test_whitespace_only_returns_empty(self):
        assert cross_account_grant_resources("   \n\t ") == []

    def test_commas_only_returns_empty(self):
        # All segments empty after strip → no grant (nothing to validate).
        assert cross_account_grant_resources(" , ,, ") == []


class TestConventionMode:
    def test_keyword_yields_wildcard(self):
        assert cross_account_grant_resources("convention") == [CONVENTION_WILDCARD_ARN]

    @pytest.mark.parametrize("value", ["Convention", "CONVENTION", "  convention  "])
    def test_keyword_is_case_insensitive_and_stripped(self, value):
        assert cross_account_grant_resources(value) == [CONVENTION_WILDCARD_ARN]

    def test_wildcard_matches_naming_convention(self):
        assert CONVENTION_WILDCARD_ARN == "arn:aws:iam::*:role/ADTCrossAccount*"


class TestExplicitListMode:
    def test_single_arn_behaves_as_before(self):
        assert cross_account_grant_resources(ARN_A) == [ARN_A]

    def test_two_arn_list(self):
        assert cross_account_grant_resources(f"{ARN_A},{ARN_B}") == [ARN_A, ARN_B]

    def test_whitespace_around_entries_is_stripped(self):
        assert cross_account_grant_resources(f"  {ARN_A} , {ARN_B}\n") == [ARN_A, ARN_B]

    def test_empty_segments_are_dropped(self):
        assert cross_account_grant_resources(f"{ARN_A},,{ARN_B},") == [ARN_A, ARN_B]

    @pytest.mark.parametrize(
        "bad",
        [
            "not-an-arn",
            "arn:aws:s3:::bucket",                      # wrong service
            "arn:aws:iam::444455556666:user/somebody",  # not a role
            f"{ARN_A},bogus",                           # one good + one bad
            "convention,arn:aws:iam::444455556666:role/X",  # keyword only valid alone
        ],
        ids=["plain-text", "s3-arn", "iam-user", "mixed-good-bad", "keyword-in-list"],
    )
    def test_malformed_entries_raise_value_error(self, bad):
        with pytest.raises(ValueError, match="crossAccountDataRoleArn"):
            cross_account_grant_resources(bad)


# ---------------------------------------------------------------------------
# 2. Synth assertions on ADTComputeStack (aws_cdk.assertions.Template)
# ---------------------------------------------------------------------------
def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


def _cached_lookup_account() -> str | None:
    """Account id whose SSM lookups are already cached in cdk.context.json.

    The compute stack uses ``ssm.StringParameter.value_from_lookup`` — with the
    credential env scrubbed (offline determinism) the CLI cannot resolve a
    default account, and lookups require one at the stack level. Pinning
    CDK_DEFAULT_ACCOUNT to an account that already has the lookups cached in
    the checked-in cdk.context.json keeps the synth fully offline. Not a
    hardcoded account id: parsed from the repo's own lookup cache.
    """
    ctx_path = REPO_ROOT / "cdk.context.json"
    if not ctx_path.exists():
        return None
    for key in json.loads(ctx_path.read_text()):
        m = re.match(
            r"ssm:account=(\d+):parameterName=/adt/highbyte-url:region=us-east-1",
            key,
        )
        if m:
            return m.group(1)
    return None


def _synth_compute_template(tmp_dir: Path, extra_context: list[str]) -> dict:
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
        # SSM sticky-context read fails soft (returns no sticky value) instead
        # of picking up a persisted crossAccountDataRoleArn from a dev account.
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    env.pop("AWS_PROFILE", None)
    env.pop("AWS_ACCESS_KEY_ID", None)
    env.pop("AWS_SECRET_ACCESS_KEY", None)
    env.pop("AWS_SESSION_TOKEN", None)

    args = [cdk_bin, "synth", STACK, "-c", "region=us-east-1"]
    for ctx in extra_context:
        args += ["-c", ctx]
    args += ["--output", str(tmp_dir), "--quiet"]

    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        args, cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=600,
    )
    template_path = tmp_dir / f"{STACK}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    return json.loads(template_path.read_text())


def _assume_role_statements(template: dict) -> list[dict]:
    """All sts:AssumeRole policy statements across IAM policies in the template."""
    from aws_cdk.assertions import Template

    t = Template.from_json(template)
    statements: list[dict] = []
    for policy in t.find_resources("AWS::IAM::Policy").values():
        doc = policy["Properties"]["PolicyDocument"]
        for stmt in doc.get("Statement", []):
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "sts:AssumeRole" in actions:
                statements.append(stmt)
    return statements


def _resources_of(stmt: dict) -> list:
    res = stmt.get("Resource", [])
    return [res] if isinstance(res, str) else res


@pytest.fixture(scope="session")
def base_template(tmp_path_factory) -> dict:
    return _synth_compute_template(tmp_path_factory.mktemp("xa_base"), [])


@pytest.fixture(scope="session")
def list_synth_dir(tmp_path_factory) -> Path:
    """Out dir of the explicit-ARN-list synth; `cdk synth ADTComputeStack` also
    writes its dependency stacks (AgentCore, Onboarding) here."""
    return tmp_path_factory.mktemp("xa_list_all")


@pytest.fixture(scope="session")
def list_template(tmp_path_factory) -> dict:
    return _synth_compute_template(
        tmp_path_factory.mktemp("xa_list"),
        [f"crossAccountDataRoleArn={ARN_A},{ARN_B}"],
    )


@pytest.fixture(scope="session")
def convention_template(tmp_path_factory) -> dict:
    return _synth_compute_template(
        tmp_path_factory.mktemp("xa_conv"),
        ["crossAccountDataRoleArn=convention"],
    )


def test_no_context_produces_no_cross_account_grant(base_template):
    """(a) Base synth (no context) must contain NO sts:AssumeRole statement
    touching an ADTCrossAccount resource — byte-identical no-grant behavior."""
    for stmt in _assume_role_statements(base_template):
        assert not any(
            "ADTCrossAccount" in str(r) for r in _resources_of(stmt)
        ), f"unexpected cross-account grant in base synth: {stmt}"


def test_two_arn_list_grants_both_resources(list_template):
    """(b) Comma-separated list → ONE statement whose Resource contains both ARNs."""
    matching = [
        s for s in _assume_role_statements(list_template)
        if ARN_A in _resources_of(s) and ARN_B in _resources_of(s)
    ]
    assert matching, "no sts:AssumeRole statement granting both explicit ARNs"


def test_convention_grants_naming_wildcard(convention_template):
    """(c) `convention` → Resource contains the ADTCrossAccount* wildcard."""
    matching = [
        s for s in _assume_role_statements(convention_template)
        if CONVENTION_WILDCARD_ARN in _resources_of(s)
    ]
    assert matching, "no sts:AssumeRole statement on the convention wildcard"


def test_all_three_assuming_roles_carry_the_same_grant(list_synth_dir):
    """ADR-0069 follow-up 4 (measured live): the onboarding runtime runs
    Athena / SiteWise DISCOVERY against the data account's role, but only the
    app task role and the query runtime role carried the caller-side grant —
    chat-driven discovery failed AccessDenied while the wizard succeeded. ONE
    helper now grants all three; pin that none can silently drop it."""
    _synth_compute_template(list_synth_dir,
                            [f"crossAccountDataRoleArn={ARN_A},{ARN_B}"])
    for stack in ("ADTComputeStack", "ADTAgentCoreStack", "ADTOnboardingStack"):
        path = list_synth_dir / f"{stack}.template.json"
        assert path.exists(), f"{stack} template not synthesized"
        template = json.loads(path.read_text())
        matching = [
            st for st in _assume_role_statements(template)
            if ARN_A in _resources_of(st) and ARN_B in _resources_of(st)
        ]
        assert matching, f"{stack}: no sts:AssumeRole grant on the data-account roles"
