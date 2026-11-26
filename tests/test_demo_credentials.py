# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Demo-password change trigger never leaks the override.

``get_demo_password`` feeds the ``PasswordTrigger`` property of three
password-setting custom resources (ADTAgentCoreStack + ADTERPDashboardStack).
It used to return the raw ``-c demo_password`` override, so an explicit
password landed in three synthesized templates and in the custom resources'
invocation events. It now returns a truncated SHA-256 digest: stable, changes
exactly when the password changes, reveals nothing.

The pure tests need only ``aws_cdk`` (offline). The synth test mirrors
``test_agentcore_app_cognito.py`` (vendored CDK CLI + Node; skips without
them) and scans EVERY template of a ``-c demo_password`` synth: the sentinel
may appear ONLY as the documented ``DemoPasswordSecret`` SecretString.
"""
from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest

from stacks._demo_credentials import (
    _STABLE_TRIGGER_TOKEN,
    get_demo_password,
    password_trigger_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
SENTINEL = "S3ntinel-Passw0rd-XYZ"


def test_erp_dashboard_source_has_no_hardcoded_demo_password():
    """The ERP dashboard stack source must not publish a demo login password.

    The demo users (erp, ADT) get their password from the generated Secrets
    Manager secret adt-demo-password, never a literal. A docstring once named a
    demo login as "<user> / <password>", publishing a working-looking
    credential in a file that ships in the public sample. Reject any such
    "name / token" login pattern (structural, so no credential literal lives in
    this test either) and confirm the Secrets Manager mechanism is documented.
    """
    source = (REPO_ROOT / "stacks" / "erp_dashboard_stack.py").read_text()
    # A parenthesised "<user> / <Token2026>"-style login: an identifier, a
    # slash, then a capitalised token that ends in digits (the old literal's
    # shape). No real demo password matches; the generated secret never does.
    login_pattern = re.compile(r"\([A-Za-z_][\w-]*\s*/\s*[A-Za-z][\w-]*\d\s*\)")
    assert not login_pattern.search(source), "docstring must not publish a demo login"
    # The secret-backed mechanism must still be the documented path.
    assert "adt-demo-password" in source


def test_no_override_yields_the_stable_token():
    app = cdk.App(context={})
    assert get_demo_password(app) == _STABLE_TRIGGER_TOKEN


@pytest.mark.parametrize("key", ["demo_password", "highbyte_password"])
def test_override_yields_a_digest_not_the_value(key):
    app = cdk.App(context={key: SENTINEL})
    trigger = get_demo_password(app)
    assert SENTINEL not in trigger
    assert trigger == password_trigger_digest(SENTINEL)
    assert trigger.startswith("sha256:") and len(trigger) == len("sha256:") + 16


def test_digest_is_stable_and_password_sensitive():
    assert password_trigger_digest("a") == password_trigger_digest("a")
    assert password_trigger_digest("a") != password_trigger_digest("b")


@pytest.fixture(scope="session")
def override_templates(tmp_path_factory) -> dict[str, dict]:
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp("cdk_synth_password")
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [str(CDK_BIN), "synth", "--all", "-c", "region=us-east-1",
         "-c", f"demo_password={SENTINEL}", "-c", "with_simulator=true",
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    templates = {p.stem: json.loads(p.read_text()) for p in out_dir.glob("*.template.json")}
    if not templates:
        pytest.skip(f"synth produced no templates:\n{result.stderr[-2000:]}")
    return templates


def test_override_appears_only_as_the_documented_secret_string(override_templates):
    hits: list[str] = []
    for name, template in override_templates.items():
        for rid, res in (template.get("Resources") or {}).items():
            blob = json.dumps(res)
            if SENTINEL in blob:
                hits.append(f"{name}:{rid}:{res.get('Type')}")
    assert hits == [h for h in hits if h.endswith("AWS::SecretsManager::Secret")], hits
    assert len(hits) == 1, hits
    for name, template in override_templates.items():
        for rid, res in (template.get("Resources") or {}).items():
            trigger = (res.get("Properties") or {}).get("PasswordTrigger")
            if trigger is not None:
                assert trigger == password_trigger_digest(SENTINEL), (name, rid)
