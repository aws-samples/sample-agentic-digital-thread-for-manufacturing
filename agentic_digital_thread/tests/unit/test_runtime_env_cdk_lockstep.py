# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK feeder lockstep (name-level, not absorption).

The CDK env feeder (stacks/adt_compute_stack.py::_env_data_stores for the
ECS app task) seeds the AMBIENT env the contract's ``from_ambient()`` reads.
(stacks/onboarding_stack.py's provisioner Lambda was the second feeder until
it was removed with the unused Step Functions provisioner, 2026-10-29.) They are
CFN-managed (declarative, diffed — never part of the full-replacement wipe
hazard), so they stay CDK-owned; this test only prevents the inversion
"added to the contract, forgot the CDK feeder".

Source-level key extraction (grep, not synth) so the check runs in the app
unit lane on machines without aws_cdk — per the plan's placement note.
"""

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPUTE_STACK = _REPO_ROOT / "stacks" / "adt_compute_stack.py"
_ONBOARDING_STACK = _REPO_ROOT / "stacks" / "onboarding_stack.py"

pytestmark = pytest.mark.skipif(
    not (_COMPUTE_STACK.exists() and _ONBOARDING_STACK.exists()),
    reason="stacks/ not present (packaged app checkout)")

# Contract keys the CDK feeder must seed into its process env. The app task
# feeds agent_manager's from_ambient(). (BEDROCK_MODEL reaches the app via CDK context →
# compute stack; SYSTEM_IDENTITY_CLAIM_SECRET_ARN via the compute stack's
# identity wiring — both asserted at the compute-stack level only.)
_APP_TASK_MUST_SEED = (
    "ONBOARDING_SESSIONS_TABLE",
    "GRAPH_IDENTIFIER",
    "SYSTEM_QUERY_MODE",
    "ADT_SYSTEM_GUIDANCE_TABLE",
)


@pytest.mark.parametrize("key", _APP_TASK_MUST_SEED)
def test_compute_stack_seeds_contract_key(key):
    source = _COMPUTE_STACK.read_text(encoding="utf-8")
    assert f'"{key}"' in source, (
        f"stacks/adt_compute_stack.py no longer emits {key} — the app task "
        f"env feeds agents/runtime_env_contract.from_ambient(); dropping the "
        f"feeder starves every in-app-provisioned runtime (the wipe-class "
        f"inversion this test exists to catch)."
    )


def test_required_key_default_matches_the_cdk_fixed_table_name():
    """The contract's default and onboarding_stack's pinned physical name
    must be THE SAME literal (never suffixed)."""
    from agents.runtime_env_contract import ONBOARDING_SESSIONS_TABLE_DEFAULT
    source = _ONBOARDING_STACK.read_text(encoding="utf-8")
    assert (f'ONBOARDING_SESSIONS_TABLE_NAME = '
            f'"{ONBOARDING_SESSIONS_TABLE_DEFAULT}"') in source


def test_system_guidance_default_matches_database_fixed_table_name():
    from agents.runtime_env_contract import SYSTEM_GUIDANCE_TABLE_DEFAULT
    database_stack = _REPO_ROOT / "stacks" / "adt_database_stack.py"
    source = database_stack.read_text(encoding="utf-8")
    assert f'table_name="{SYSTEM_GUIDANCE_TABLE_DEFAULT}"' in source
