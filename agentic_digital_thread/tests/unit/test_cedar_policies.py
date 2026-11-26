# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0033: Cedar policy source files present + well-formed, Cedar DEFERRED.

The ENTIRE Cedar layer (policy engine + policies + gateway policy-engine
attachment) is DEFERRED and is NOT declared in either CDK stack. A
declarative-in-stack Cedar layer is not possible pre-target-registration:
AgentCore rejects a permit-all baseline as "Overly Permissive" (action = Any
Future Tools, resource = gateway/*) and rejects a specific forbid as
"unrecognized action" (the gateway's tool action ids are only known AFTER its
targets are registered, and targets are registered IMPERATIVELY by
agentic_digital_thread/deploy.sh AFTER `cdk deploy`). Both gateways therefore keep the
``CUSTOM_JWT`` authorizer (the identity-propagation seam) but have NO policy
engine attached.

The Cedar sources stay in the repo, UNATTACHED, for the future attach:

  - ``<gw>_gateway_baseline.cedar`` — a single permit-all baseline (a Cedar
    engine is deny-by-default, so LOG_ONLY needs a baseline permit).
  - ``<gw>_gateway.cedar`` — a single ``forbid`` (the first real authz rule).

Grammar (per the AWS AgentCore Policy docs): tools are referenced as
``AgentCore::Action::"<target>___<tool>"`` and Cognito groups are matched via
principal TAGS (``principal.hasTag("cognito:groups")`` +
``getTag(...).contains("<group>")`` — EXACT group-name element matches, no
substring ``like`` patterns) — NOT ``Group::"..."`` and NOT a bare
``Action::``. These tests assert the deferred .cedar files still exist and are
well-formed, and that neither stack wires a Cedar PolicyEngine/Policy today while
both gateways stay CUSTOM_JWT.
"""
from pathlib import Path

import re
import pytest

# agentic_digital_thread/tests/unit/<this> -> repo root is parents[3].
POLICIES_DIR = Path(__file__).resolve().parents[3] / "policies"
STACKS_DIR = Path(__file__).resolve().parents[3] / "stacks"

BASELINE_FILES = ["query_gateway_baseline.cedar", "onboarding_gateway_baseline.cedar"]
TIER_FILES = ["query_gateway.cedar", "onboarding_gateway.cedar"]
ALL_FILES = BASELINE_FILES + TIER_FILES


def _read(name: str) -> str:
    p = POLICIES_DIR / name
    assert p.exists(), f"missing Cedar policy file {p}"
    return p.read_text()


def _policy_body(text: str) -> str:
    """Strip `//` comments so structural checks only see policy statements."""
    out = []
    for line in text.splitlines():
        idx = line.find("//")
        if idx != -1:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def test_policies_dir_exists():
    assert POLICIES_DIR.is_dir()


@pytest.mark.parametrize("name", ALL_FILES)
def test_exactly_one_statement(name):
    """AgentCore CfnPolicy takes exactly ONE Cedar policy per resource."""
    body = _policy_body(_read(name))
    # Exactly one terminated statement.
    assert body.count(";") == 1, f"{name}: expected exactly one statement (one ';')"
    # Exactly one policy effect keyword (permit XOR forbid), never both.
    effects = body.count("permit") + body.count("forbid")
    assert effects == 1, f"{name}: expected exactly one permit/forbid, found {effects}"


@pytest.mark.parametrize("name", BASELINE_FILES)
def test_baseline_is_permit_all(name):
    body = _policy_body(_read(name))
    assert "permit" in body, f"{name}: baseline must be a permit"
    assert "forbid" not in body, f"{name}: baseline must not contain a forbid"
    # permit-all: no when/unless conditions.
    assert "when" not in body and "unless" not in body, f"{name}: baseline must be permit-all"
    # Scoped to the gateway resource type.
    assert "resource is AgentCore::Gateway" in body, \
        f"{name}: baseline must scope resource is AgentCore::Gateway"


# NOTE: the TIER (forbid) files remain in the repo but are DEFERRED —
# they are NOT attached to the stacks at `cdk deploy` time (the AgentCore policy
# engine validates a forbid's action ids against the gateway's REGISTERED tools,
# which are registered imperatively by deploy.sh AFTER `cdk deploy`). These tests
# therefore assert only that the deferred forbid files still exist and are
# well-formed single-statement forbids; they intentionally do NOT assert that any
# stack wires them in.
@pytest.mark.parametrize("name", TIER_FILES)
def test_tier_is_forbid_with_tag_based_groups(name):
    body = _policy_body(_read(name))
    assert "forbid" in body, f"{name}: tier policy must be a forbid"
    assert "permit" not in body, f"{name}: tier policy must not contain a permit"
    # Actions use the AgentCore::Action namespace, never a bare Action::.
    assert "AgentCore::Action::" in body, f"{name}: must use AgentCore::Action:: ids"
    # No bare `Action::` (i.e. not preceded by `AgentCore::`).
    import re
    assert not re.search(r"(?<!AgentCore::)\bAction::", body), \
        f"{name}: must not use a bare Action:: id"
    # Groups are matched via principal TAGS, never `Group::`.
    assert "Group::" not in body, f"{name}: must not use Group:: — use principal tags"
    assert 'principal.hasTag("cognito:groups")' in body
    # EXACT group-name element matches on the set-valued tag — substring
    # `like "*<group>*"` patterns would also match e.g. "not-integrator".
    assert 'principal.getTag("cognito:groups").contains("integrator")' in body
    assert 'principal.getTag("cognito:groups").contains("Admin")' in body
    assert ' like ' not in body, f"{name}: no substring like-patterns — exact matches only"
    # curator was never a valid group here.
    assert "curator" not in body


def test_query_forbid_names_writeback_tools():
    body = _policy_body(_read("query_gateway.cedar"))
    assert 'AgentCore::Action::"digital-thread___save_value_mapping"' in body
    assert 'AgentCore::Action::"digital-thread___save_correlation"' in body


def test_onboarding_forbid_names_no_commit_tool():
    body = _policy_body(_read("onboarding_gateway.cedar"))
    # The onboarding runtime exports no commit-tier tool: connect_new_system,
    # store_credentials, provision_system and reset_system run in the app and
    # are not on the gateway, so the forbid cannot name them (an action the
    # target does not have is rejected when the policy is attached).
    for tool in ("connect_new_system", "store_credentials", "provision_system",
                 "reset_system"):
        assert f'AgentCore::Action::"onboarding___{tool}"' not in body, \
            f"the forbid names {tool}, which the gateway target does not have"
    assert 'AgentCore::Action::"onboarding___discover"' in body


@pytest.mark.parametrize("name", ALL_FILES)
def test_policy_well_formed(name):
    body = _policy_body(_read(name))
    for open_c, close_c in (("(", ")"), ("[", "]"), ("{", "}")):
        assert body.count(open_c) == body.count(close_c), f"{name}: unbalanced {open_c}{close_c}"


# --------------------------------------------------------------------------- #
# Cedar deferral: the Cedar layer is NOT wired into either stack yet, but both
# gateways keep the CUSTOM_JWT authorizer (the identity-propagation seam). These
# assertions scan the stack source (the unit lane is offline; `cdk synth --all`
# is the separate synth gate) to prove no CfnPolicyEngine/CfnPolicy Cedar
# resource is declared today while CUSTOM_JWT stays intact.
# --------------------------------------------------------------------------- #
QUERY_STACK = STACKS_DIR / "agentcore_stack.py"
ONBOARDING_STACK = STACKS_DIR / "onboarding_stack.py"
GATEWAY_STACKS = [QUERY_STACK, ONBOARDING_STACK]


@pytest.mark.parametrize("stack", GATEWAY_STACKS)
def test_gateway_stack_exists(stack):
    assert stack.exists(), f"missing gateway stack {stack}"


@pytest.mark.parametrize("stack", GATEWAY_STACKS)
def test_gateway_is_custom_jwt(stack):
    """Both gateways keep the CUSTOM_JWT authorizer (identity-propagation seam)."""
    src = stack.read_text()
    assert 'authorizer_type="CUSTOM_JWT"' in src, \
        f"{stack.name}: gateway must keep authorizer_type=CUSTOM_JWT"
    assert "CustomJWTAuthorizerConfigurationProperty" in src, \
        f"{stack.name}: gateway must keep the custom_jwt_authorizer config"


@pytest.mark.parametrize("stack", GATEWAY_STACKS)
def test_no_cedar_policy_engine_attached(stack):
    """Cedar DEFERRED — no PolicyEngine/Policy resource, no gateway attach."""
    src = stack.read_text()
    assert "CfnPolicyEngine(" not in src, \
        f"{stack.name}: no agentcore.CfnPolicyEngine may be declared (Cedar deferred)"
    assert "CfnPolicy(" not in src, \
        f"{stack.name}: no agentcore.CfnPolicy may be declared (Cedar deferred)"
    assert "policy_engine_configuration=" not in src, \
        f"{stack.name}: gateway must not attach a policy_engine_configuration (Cedar deferred)"
    assert "GatewayPolicyEngineConfigurationProperty" not in src, \
        f"{stack.name}: gateway must not reference GatewayPolicyEngineConfigurationProperty"


@pytest.mark.parametrize("name", ALL_FILES)
def test_cedar_files_still_present_for_future_attach(name):
    """The Cedar sources stay in the repo, UNATTACHED, for the future attach."""
    assert (POLICIES_DIR / name).exists(), f"deferred Cedar file {name} must remain in the repo"


def _onboarding_runtime_tools(monkeypatch) -> set:
    """The tool names the onboarding runtime exports to its gateway target
    (built under the local-mode gate, whose boot check needs no env)."""
    import asyncio
    from agents.config import get_config
    from agents.onboarding_runtime_server import create_onboarding_server
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()
    try:
        return {t.name for t in asyncio.run(create_onboarding_server().list_tools())}
    finally:
        get_config.cache_clear()


def test_onboarding_forbid_names_no_tool_the_runtime_does_not_export(monkeypatch):
    """A forbid naming an action the gateway target does not have is rejected
    as "unrecognized action" when the deferred policy is attached. The
    onboarding runtime does not export the tools the app runs itself
    (get_catalog, connect_new_system, store_credentials, provision_system,
    reset_system), so the forbid names only tools it does export."""
    exported = _onboarding_runtime_tools(monkeypatch)
    body = _policy_body(_read("onboarding_gateway.cedar"))
    forbidden = set(re.findall(r'AgentCore::Action::"onboarding___([a-z_]+)"', body))
    assert forbidden <= exported, forbidden - exported
    app_only = {"get_catalog", "connect_new_system", "store_credentials", "provision_system",
                "reset_system"}
    assert not (forbidden & app_only), forbidden & app_only


def test_onboarding_forbid_tiers_match_onboard_tools(monkeypatch):
    """Tier parity with the runtime's exported tools (ADR-0068/0069 follow-up):
    the onboarding runtime exports no COMMIT-tier tool (connect_new_system,
    store_credentials, provision_system and reset_system run in the app), and
    the forbid names no EXPLORE-tier tool, so a new tool cannot land in the
    wrong Cedar tier silently. ``discover`` is the documented exception (probe
    tool listed in the forbid pending Decision 2; LOG_ONLY tolerates it). The
    two ADR-0068 tools (assess_vocabulary_fit, propose_concepts) are
    explore-tier and zero-write, so they ride the baseline permit and must NOT
    be forbidden."""
    import agents.onboard_agent as oa
    wired = {getattr(t, "tool_name", None) or getattr(t, "__name__", "") for t in oa.ONBOARD_TOOLS}
    names = _onboarding_runtime_tools(monkeypatch)
    assert names <= wired, names - wired
    commit = {"connect_new_system", "store_credentials", "provision_system", "reset_system"}
    assert commit <= wired
    assert not (commit & names), commit & names
    explore = set(names)
    body = _policy_body(_read("onboarding_gateway.cedar"))
    forbidden = set(re.findall(r'AgentCore::Action::"onboarding___([a-z_]+)"', body))
    assert forbidden <= {"discover"}, forbidden - {"discover"}
    assert {"assess_vocabulary_fit", "propose_concepts"} <= explore
    assert not ({"assess_vocabulary_fit", "propose_concepts"} & forbidden)
