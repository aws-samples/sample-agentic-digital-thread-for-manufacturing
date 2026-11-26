# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The provenance-store env survives EVERY path that writes runtime env.

Live gap (Management env, 2026-08-04): Waves 5/6 system-agent provenance
writes never happened — provenance-query and investigation-trace redemption
both 404'd because ``get-agent-runtime`` showed the per-system runtimes'
``environmentVariables`` WITHOUT ``ONBOARDING_SESSIONS_TABLE``. Root cause:
the deploy.sh [2f] per-system refresh preserves env verbatim (never injects
the table — pre-Wave-5 runtimes never had it), and the Python provisioning
paths sourced the value from ambient env only (``os.environ.get(..., "")``)
— the SFN-provisioner env-gap precedent, again, this time in the deploy
shell.

Fix contract pinned here:

1. ``runtime_provision_helpers.provenance_store_env`` — ambient env wins,
   else the CDK-fixed physical table name (``adt-onboarding-sessions``,
   stacks/onboarding_stack.py::ONBOARDING_SESSIONS_TABLE_NAME).
2. ``runtime_update_kwargs`` (the ONE sanctioned full-replacement builder,
   ADR-0056) fills the key when the caller's env lacks it — even when the
   CALLING PROCESS env lacks it too. A caller-supplied non-empty value is
   authoritative.
3. ``agent_manager._prepare_runtime_env`` emits the default when the app
   process env lacks the variable (no more honest-empty starvation).
4. deploy.sh tripwire: every env write routes through the
   ``agents/runtime_env_contract`` shim (build for §2c/§2e, reconcile for
   [2f]) — no inline env-blob spelling may reappear.
"""

from pathlib import Path

from agents.agent_manager import AgentManager
from agents.connection_types import KNOWLEDGE_GRAPH_CONNECTION_TYPE
from agents.runtime_provision_helpers import (
    ONBOARDING_SESSIONS_TABLE_DEFAULT,
    provenance_store_env,
    runtime_update_kwargs,
)

_ADT_ROOT = Path(__file__).resolve().parents[2]  # agentic_digital_thread/


# --- provenance_store_env: ambient wins, constant fallback -------------------

def test_constant_is_the_cdk_fixed_physical_table_name():
    assert ONBOARDING_SESSIONS_TABLE_DEFAULT == "adt-onboarding-sessions"


def test_env_wins_when_set(monkeypatch):
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "custom-sessions-table")
    assert provenance_store_env() == {
        "ONBOARDING_SESSIONS_TABLE": "custom-sessions-table"}


def test_falls_back_to_constant_when_process_env_unset(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    assert provenance_store_env() == {
        "ONBOARDING_SESSIONS_TABLE": ONBOARDING_SESSIONS_TABLE_DEFAULT}


def test_falls_back_to_constant_when_process_env_blank(monkeypatch):
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "   ")
    assert provenance_store_env() == {
        "ONBOARDING_SESSIONS_TABLE": ONBOARDING_SESSIONS_TABLE_DEFAULT}


# --- runtime_update_kwargs: the refresh path can never strip the store -------

_DETAILS = {
    "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "img:1"}},
    "roleArn": "arn:aws:iam::111122223333:role/runtime",
    "networkConfiguration": {"networkMode": "PUBLIC"},
    "protocolConfiguration": {"serverProtocol": "MCP"},
}


def test_update_kwargs_fills_table_when_caller_and_process_env_lack_it(monkeypatch):
    """THE live-gap pin: a full-replacement update built by a process whose
    env lacks the variable (deploy shell / ad-hoc refresh) still carries the
    provenance store."""
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    kwargs = runtime_update_kwargs("rt-1", _DETAILS, {"SYSTEM_ID": "cmms"})
    env = kwargs["environmentVariables"]
    assert env["ONBOARDING_SESSIONS_TABLE"] == ONBOARDING_SESSIONS_TABLE_DEFAULT
    assert env["SYSTEM_ID"] == "cmms"  # every other key preserved verbatim
    # Full-replacement invariants stay intact (ADR-0056).
    assert kwargs["protocolConfiguration"] == {"serverProtocol": "MCP"}


def test_update_kwargs_fills_table_when_caller_value_is_empty(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    kwargs = runtime_update_kwargs(
        "rt-1", _DETAILS, {"ONBOARDING_SESSIONS_TABLE": ""})
    assert (kwargs["environmentVariables"]["ONBOARDING_SESSIONS_TABLE"]
            == ONBOARDING_SESSIONS_TABLE_DEFAULT)


def test_update_kwargs_preserves_caller_supplied_table(monkeypatch):
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "ambient-table")
    kwargs = runtime_update_kwargs(
        "rt-1", _DETAILS, {"ONBOARDING_SESSIONS_TABLE": "caller-table"})
    assert (kwargs["environmentVariables"]["ONBOARDING_SESSIONS_TABLE"]
            == "caller-table")


def test_update_kwargs_does_not_mutate_caller_env_dict(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    caller_env = {"SYSTEM_ID": "cmms"}
    runtime_update_kwargs("rt-1", _DETAILS, caller_env)
    assert caller_env == {"SYSTEM_ID": "cmms"}


# --- agent_manager._prepare_runtime_env: in-app path never honest-empty ------

def test_prepare_runtime_env_emits_default_when_app_env_lacks_table(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    env = AgentManager(mode="cloud")._prepare_runtime_env(
        "digital-thread", "Digital Thread", "adt",
        "prompt", "tables", KNOWLEDGE_GRAPH_CONNECTION_TYPE,
        None, None, "us-east-1",
    )
    assert env["ONBOARDING_SESSIONS_TABLE"] == ONBOARDING_SESSIONS_TABLE_DEFAULT


def test_prepare_runtime_env_ambient_env_still_wins(monkeypatch):
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "custom-sessions-table")
    env = AgentManager(mode="cloud")._prepare_runtime_env(
        "digital-thread", "Digital Thread", "adt",
        "prompt", "tables", KNOWLEDGE_GRAPH_CONNECTION_TYPE,
        None, None, "us-east-1",
    )
    assert env["ONBOARDING_SESSIONS_TABLE"] == "custom-sessions-table"


# --- deploy.sh: env comes from the contract, never inline --------------------
# The two literal-spelling grep pins (2f-merge + ORCH_ENV-carries-the-table)
# are RETIRED: deploy.sh no longer contains an env spelling to drift. Replaced
# by (i) the driver/shim-marker pins below, (ii) the golden byte-parity tests
# in test_runtime_env_contract.py, and (iii) the live drift lane
# (scripts/audit_runtime_env.py).

def test_deploy_sh_builds_env_through_the_contract_shim():
    """§2c and §2e BUILD through the provisioning driver
    (scripts/provision_runtimes.py — which builds env INSIDE via
    runtime_env_contract.build_env); §2f RECONCILES through the shim
    (agents/runtime_env_contract.py stays the ONE env source of truth)."""
    deploy = (_ADT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    # §2c/§2e: one driver invocation each (env built inside the driver).
    assert deploy.count("python3 scripts/provision_runtimes.py") == 2, (
        "deploy.sh §2c/§2e must each provision through ONE "
        "scripts/provision_runtimes.py driver invocation"
    )
    assert "--role orchestrator" in deploy
    assert "--role onboarding" in deploy
    # §2f: the one remaining direct shim call site (per-system reconcile).
    assert deploy.count("python3 -m agents.runtime_env_contract") == 1, (
        "the ONLY direct contract-shim call left in deploy.sh is the §2f "
        "per-system reconcile — §2c/§2e env moved inside the driver"
    )
    assert "--role system --reconcile" in deploy


def test_deploy_sh_2c_2e_control_flow_lives_in_the_driver():
    """The extracted §2c/§2e bash may not creep back: NO aws-CLI runtime
    create / gateway-target calls anywhere in deploy.sh, and
    ``update-agent-runtime`` survives ONLY in the §2f per-system refresh
    loop (which keeps its own update path until Phase 2)."""
    deploy = (_ADT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    for needle in ("create-agent-runtime", "delete-agent-runtime",
                   "create-gateway-target", "synchronize-gateway-targets"):
        assert needle not in deploy, (
            f"deploy.sh regrew an aws-CLI `{needle}` call — that control "
            "flow lives in scripts/provision_runtimes.py now"
        )
    marker = "[2f] Refreshing per-system agent runtimes"
    assert marker in deploy, "deploy.sh §2f header not found"
    before_2f = deploy.split(marker, 1)[0]
    assert "update-agent-runtime" not in before_2f, (
        "an update-agent-runtime call appeared BEFORE §2f — §2c/§2e updates "
        "must go through the provision_runtimes.py driver"
    )


def test_deploy_sh_carries_no_inline_env_spelling():
    """No hand-built JSON env blob may reappear: an inline
    \\"ONBOARDING_SESSIONS_TABLE\\": literal is the 4-way-spelling hazard
    coming back (any key added there but not in the contract is wiped on the
    next full-replacement write — the 2026-08-04 live failure, twice)."""
    deploy = (_ADT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert '\\"ONBOARDING_SESSIONS_TABLE\\"' not in deploy, (
        "deploy.sh grew an inline env-blob spelling again — build it through "
        "agents/runtime_env_contract instead"
    )
