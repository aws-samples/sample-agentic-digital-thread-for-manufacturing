# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Who may reach the demo databases on 5432 (the ERP Aurora cluster and the
seed RDS instance).

Neither database admits Postgres from a CIDR. Each admits exactly the client
security groups that connect to it, one ingress rule per group, so a client
added later without its rule fails :func:`test_every_client_reaches_exactly_its_databases`.
The client list is the inventory of every resource that connects, including
those the app creates at run time, which attach a group the stacks own:

* the app's ECS task (``AdtSvcSg``);
* every AgentCore runtime the app or deploy creates in VPC mode, the system
  agents, the knowledge-graph runtime and the orchestrator
  (``AgentCoreRuntimeSg``, ``AGENTCORE_RUNTIME_SG`` / ``AgentCoreRuntimeSgId``);
* the onboarding runtime (``OnboardingRuntimeSg``);
* the HighByte hub's tasks (the ECS service's own group), whose JDBC
  connections the setup function writes;
* the ERP Lambdas (``LambdaSG``: db-init, the simulation and
  its CMMS bridge into the seed database, cleanup, ERP seed, story history),
  the bastion, the ERP dashboard's tasks;
* each database's own loaders: the seed loader, the reader-login setup
  functions, and the two mocks' functions.

Each consumer CloudFormation runs or keeps (functions, custom resources, ECS
services) also waits for its own rules, so on create the path exists before
it runs and on delete it goes only after.

A deployment created before the client rules existed still carries one
VPC-wide rule per database, and most client rules live in stacks that update
after the database stacks, so an in-place update runs in two phases on the
``DatabaseCidrIngress`` switch: ``keep`` gives back exactly the rule such a
deployment has, beside every client rule; ``remove`` (the default) drops it
and changes nothing else.

Synthesized offline by running app.py the way the CDK CLI does (context in
``CDK_CONTEXT_JSON``, no bundling, no AWS credentials); skips without node.
"""
from __future__ import annotations

import json

import pytest

from _app_synth import resolved, synth_app

POSTGRES_PORT = 5432
AURORA = ("ADTERPDatabaseStack", "AuroraSG")
SEED_DB = ("ADTSeedDataStack", "AdtDbSg")
#: The two-phase switch on both database stacks (stacks/_db_cidr_ingress.py).
SWITCH = "DatabaseCidrIngress"
SWITCH_STATE = "DatabaseCidrIngressState"
_CIDR_EXPORT = "ADTNetworkStack:ExportsOutputFnGetAttErpVpcAF1ABBB0CidrBlockEB3BDF80"
#: The VPC-wide rule each database group carried before it admitted its
#: clients by group, exactly as deployed stacks hold it: ``keep`` must give
#: the same rule back, so phase 1 changes neither group.
_LEGACY_RULES = {
    AURORA: {"CidrIp": {"Fn::ImportValue": _CIDR_EXPORT},
             "Description": "Allow PostgreSQL from VPC (MES HighByte, etc.)",
             "FromPort": POSTGRES_PORT, "IpProtocol": "tcp", "ToPort": POSTGRES_PORT},
    SEED_DB: {"CidrIp": {"Fn::ImportValue": _CIDR_EXPORT}, "Description": "Allow Postgres from VPC",
              "FromPort": POSTGRES_PORT, "IpProtocol": "tcp", "ToPort": POSTGRES_PORT},
}

#: Every opt-in that reaches a database: HighByte + SFC, the live simulator.
_FULL = {"with_workshop": "true", "with_simulator": "true", "workshop_story": "live", "highbyte_enabled": "true",
         "highbyte_accept_eula": "true", "sfc_enabled": "true"}
#: The seed database alone (no simulator), with HighByte.
_SEED_ONLY = {"with_seed": "true", "highbyte_enabled": "true", "highbyte_accept_eula": "true"}

#: (stack, security-group logical-id prefix) of every client, per database.
_COMMON_CLIENTS = {
    ("ADTComputeStack", "AdtSvcSg"),
    ("ADTComputeStack", "AgentCoreRuntimeSg"),
    ("ADTOnboardingStack", "OnboardingRuntimeSg"),
    ("ADTHighByteOpcuaStack", "HighByteServiceSecurityGroup"),
}
_FULL_CLIENTS = {
    AURORA: _COMMON_CLIENTS | {
        ("ADTERPDatabaseStack", "LambdaSG"),
        ("ADTERPDatabaseStack", "BastionSG"),
        ("ADTERPDatabaseStack", "ErpDbReadersSetupSg"),
        ("ADTERPDashboardStack", "ECSSG"),
    },
    SEED_DB: _COMMON_CLIENTS | {
        ("ADTSeedDataStack", "AdtSeedLambdaSecurityGroup"),
        ("ADTSeedDataStack", "SeedDbReadersSetupSg"),
        ("ADTERPDatabaseStack", "LambdaSG"),
    },
}
_SEED_ONLY_CLIENTS = {
    SEED_DB: _COMMON_CLIENTS | {
        ("ADTSeedDataStack", "AdtSeedLambdaSecurityGroup"),
        ("ADTSeedDataStack", "SeedDbReadersSetupSg"),
    },
}

#: (stack, consumer logical-id prefix) -> the (stack, group prefix) of the
#: client group whose rules it must wait for: functions, custom resources and
#: services CloudFormation runs.
_LAMBDA_SG = ("ADTERPDatabaseStack", "LambdaSG")
_WAITERS = {
    ("ADTSeedDataStack", "AdtSeedLambda"): ("ADTSeedDataStack", "AdtSeedLambdaSecurityGroup"),
    ("ADTSeedDataStack", "AdtSeedResource"): ("ADTSeedDataStack", "AdtSeedLambdaSecurityGroup"),
    ("ADTSeedDataStack", "SeedDbReadersSetupFn"): ("ADTSeedDataStack", "SeedDbReadersSetupSg"),
    ("ADTSeedDataStack", "SeedDbReaders"): ("ADTSeedDataStack", "SeedDbReadersSetupSg"),
    ("ADTERPDatabaseStack", "DatabaseInitLambda"): _LAMBDA_SG,
    ("ADTERPDatabaseStack", "DatabaseInitResource"): _LAMBDA_SG,
    ("ADTERPDatabaseStack", "ErpDbReadersSetupFn"): ("ADTERPDatabaseStack", "ErpDbReadersSetupSg"),
    ("ADTERPDatabaseStack", "ErpDbReaders"): ("ADTERPDatabaseStack", "ErpDbReadersSetupSg"),
    ("ADTERPLambdaStack", "SimulationLambda"): _LAMBDA_SG,
    ("ADTERPLambdaStack", "StoryHistoryLambda"): _LAMBDA_SG,
    ("ADTERPLambdaStack", "StoryHistory"): _LAMBDA_SG,
    ("ADTComputeStack", "AdtService"): ("ADTComputeStack", "AdtSvcSg"),
    ("ADTHighByteOpcuaStack", "HighByteService"): ("ADTHighByteOpcuaStack",
                                                   "HighByteServiceSecurityGroup"),
}


def _named(logical_id: str, prefix: str) -> bool:
    """``logical_id`` is the construct ``prefix``: exactly, or plus CDK's
    eight-character hash."""
    rest = logical_id[len(prefix):]
    return logical_id.startswith(prefix) and (
        rest == "" or (len(rest) == 8 and all(c in "0123456789ABCDEF" for c in rest)))


def _synth(out_dir, context: dict) -> dict[str, dict]:
    return synth_app(out_dir, context)


def _as_applied(templates: dict[str, dict], switch: str | None = None) -> dict[str, dict]:
    """Every template as CloudFormation applies it, with ``DatabaseCidrIngress``
    at ``switch`` (its default when None)."""
    values = {} if switch is None else {SWITCH: switch}
    return {stack: resolved(template, values) for stack, template in templates.items()}


@pytest.fixture(scope="module")
def full_raw(tmp_path_factory) -> dict[str, dict]:
    return _synth(tmp_path_factory.mktemp("db-ingress-full"), _FULL)


@pytest.fixture(scope="module")
def seed_only_raw(tmp_path_factory) -> dict[str, dict]:
    return _synth(tmp_path_factory.mktemp("db-ingress-seed"), _SEED_ONLY)


@pytest.fixture(scope="module")
def full(full_raw) -> dict[str, dict]:
    """A fresh create, or phase 2 of an update: the switch at its default."""
    return _as_applied(full_raw)


@pytest.fixture(scope="module")
def seed_only(seed_only_raw) -> dict[str, dict]:
    return _as_applied(seed_only_raw)


def _exports(templates: dict) -> dict[str, tuple[str, str]]:
    """Export name -> (stack, logical id) of the group id it carries."""
    out = {}
    for stack, template in templates.items():
        for output in template.get("Outputs", {}).values():
            name, value = output.get("Export", {}).get("Name"), output.get("Value")
            if isinstance(name, str) and isinstance(value, dict) and "Fn::GetAtt" in value:
                out[name] = (stack, value["Fn::GetAtt"][0])
    return out


def _group(stack: str, value, exports) -> tuple[str, str] | None:
    """The (stack, logical id) a GroupId / SourceSecurityGroupId names."""
    if isinstance(value, dict):
        if "Fn::GetAtt" in value:
            return stack, value["Fn::GetAtt"][0]
        if "Ref" in value:
            return stack, value["Ref"]
        if "Fn::ImportValue" in value:
            return exports.get(value["Fn::ImportValue"])
    return None


def _covers_postgres(rule: dict) -> bool:
    protocol = str(rule.get("IpProtocol"))
    if protocol == "-1":
        return True
    return protocol == "tcp" and rule.get("FromPort", 0) <= POSTGRES_PORT <= rule.get("ToPort", -1)


def _ingress(templates: dict):
    """Every ingress rule: (target group, rule properties, owning stack)."""
    exports = _exports(templates)
    for stack, template in templates.items():
        for lid, res in template.get("Resources", {}).items():
            props = res.get("Properties", {})
            if res["Type"] == "AWS::EC2::SecurityGroup":
                for rule in props.get("SecurityGroupIngress", []) or []:
                    yield (stack, lid), rule, stack
            elif res["Type"] == "AWS::EC2::SecurityGroupIngress":
                yield _group(stack, props.get("GroupId"), exports), props, stack


def _resolve(templates: dict, prefix: tuple[str, str]) -> tuple[str, str]:
    stack, start = prefix
    (lid,) = [k for k, r in templates[stack]["Resources"].items()
              if _named(k, start) and r["Type"] == "AWS::EC2::SecurityGroup"]
    return stack, lid


def _clients(templates: dict, database: tuple[str, str]) -> set[tuple[str, str]]:
    """The (stack, logical id) of every group that may reach ``database`` on 5432."""
    target = _resolve(templates, database)
    exports = _exports(templates)
    sources = set()
    for group, rule, stack in _ingress(templates):
        if group == target and _covers_postgres(rule) and "SourceSecurityGroupId" in rule:
            source = _group(stack, rule["SourceSecurityGroupId"], exports)
            assert source is not None, (stack, rule)
            sources.add(source)
    return sources


def test_no_database_admits_postgres_from_a_cidr(full, seed_only):
    """The VPC-wide 5432 rules are gone from both databases, in every
    variant, on a fresh create and after phase 2 (the switch's default): any
    address in the VPC used to be able to try a login."""
    for templates in (full, seed_only):
        for database in (AURORA, SEED_DB):
            if database[0] not in templates:
                continue
            target = _resolve(templates, database)
            cidr = [(stack, rule) for group, rule, stack in _ingress(templates)
                    if group == target and _covers_postgres(rule)
                    and ("CidrIp" in rule or "CidrIpv6" in rule)]
            assert cidr == [], (database, cidr)


@pytest.mark.parametrize("variant, expected", [("full", _FULL_CLIENTS), ("seed_only", _SEED_ONLY_CLIENTS)])
def test_every_client_reaches_exactly_its_databases(variant, expected, request):
    templates = request.getfixturevalue(variant)
    assert AURORA[0] in templates or AURORA not in expected
    for database, clients in expected.items():
        want = {_resolve(templates, client) for client in clients}
        assert _clients(templates, database) == want, database
    if AURORA not in expected:
        assert AURORA[0] not in templates


# --------------------------------------------------------------------------
# The two-phase switch: phase 1 (keep) adds every client rule beside the
# VPC-wide rule; phase 2 (remove) then changes the two database groups only.
# --------------------------------------------------------------------------

_DATABASE_STACKS = {AURORA[0], SEED_DB[0]}


@pytest.mark.parametrize("variant", ["full_raw", "seed_only_raw"])
def test_each_database_stack_declares_the_switch_with_remove_as_default(variant, request):
    """Only the database stacks declare it; a stack created by this template
    with no value given (a Workshop Studio event, a first deploy) has no
    VPC-wide rule. Each reports the value in effect."""
    templates = request.getfixturevalue(variant)
    declared = {stack for stack, t in templates.items() if SWITCH in t.get("Parameters", {})}
    assert declared == _DATABASE_STACKS & set(templates)
    for stack in declared:
        parameter = templates[stack]["Parameters"][SWITCH]
        assert (parameter["Type"], parameter["AllowedValues"], parameter["Default"]) == (
            "String", ["keep", "remove"], "remove")
        assert parameter["Description"].isascii()
        output = templates[stack]["Outputs"][SWITCH_STATE]
        assert output["Value"] == {"Ref": SWITCH} and "phase 2" in output["Description"]


@pytest.mark.parametrize("variant, expected", [("full_raw", _FULL_CLIENTS),
                                               ("seed_only_raw", _SEED_ONLY_CLIENTS)])
def test_phase_one_keeps_the_deployed_rule_beside_every_client_rule(variant, expected, request):
    """With ``keep`` each database group holds exactly the inline rule it
    already had (so its update changes nothing) while every client rule is
    created: when phase 1 completes, every client has its own path."""
    templates = _as_applied(request.getfixturevalue(variant), "keep")
    for database, clients in expected.items():
        stack, group = _resolve(templates, database)
        inline = templates[stack]["Resources"][group]["Properties"].get("SecurityGroupIngress")
        assert inline == [_LEGACY_RULES[database]], database
        assert _clients(templates, database) == {_resolve(templates, c) for c in clients}


def test_phase_two_changes_only_the_database_groups(full_raw):
    """Between ``keep`` and ``remove`` nothing differs but the two groups'
    inline rule: phase 2 creates, replaces or re-runs nothing, and a failed
    phase 2 can only roll back to the VPC-wide rule."""
    keep, remove = _as_applied(full_raw, "keep"), _as_applied(full_raw, "remove")
    changed = set()
    for stack in full_raw:
        for section in ("Resources", "Outputs"):
            for lid in set(keep[stack][section]) | set(remove[stack][section]):
                if keep[stack][section].get(lid) != remove[stack][section].get(lid):
                    changed.add((stack, section, lid))
    groups = {(stack, "Resources", lid) for stack, lid in (_resolve(full_raw, AURORA),
                                                            _resolve(full_raw, SEED_DB))}
    assert changed == groups
    for stack, _section, lid in groups:
        kept = dict(keep[stack]["Resources"][lid]["Properties"])
        kept.pop("SecurityGroupIngress")
        assert kept == remove[stack]["Resources"][lid]["Properties"]
        # Nothing else reads the switch: only its condition (used by the one
        # Fn::If) and the reported state.
        raw = full_raw[stack]
        assert json.dumps(raw["Resources"]).count('"Ref": "DatabaseCidrIngress"') == 0
        assert json.dumps(raw["Resources"]).count("DatabaseCidrIngressKept") == 1
        assert raw["Outputs"][SWITCH_STATE]["Value"] == {"Ref": SWITCH}


def _ref_target(node) -> str | None:
    if isinstance(node, dict):
        if isinstance(node.get("Ref"), str):
            return node["Ref"]
        if "Fn::GetAtt" in node:
            return node["Fn::GetAtt"][0]
    return None


def _waits_for(resources: dict, start: str) -> set[str]:
    """Everything ``start`` waits for: DependsOn plus Ref/GetAtt, transitively."""
    seen, todo = set(), [start]

    def refs(node, out):
        target = _ref_target(node)
        if target in resources:
            out.add(target)
        if isinstance(node, dict):
            for value in node.values():
                refs(value, out)
        elif isinstance(node, list):
            for value in node:
                refs(value, out)

    while todo:
        res = resources[todo.pop()]
        direct = set(res.get("DependsOn", []))
        refs(res.get("Properties", {}), direct)
        for dep in direct - seen:
            seen.add(dep)
            todo.append(dep)
    return seen


def _database_rules_from(templates: dict, stack: str, client: tuple[str, str]) -> set[str]:
    """This stack's standalone 5432 ingress rules on either database whose
    source is the client group."""
    exports = _exports(templates)
    targets = {_resolve(templates, db) for db in (AURORA, SEED_DB) if db[0] in templates}
    rules = set()
    for lid, res in templates[stack]["Resources"].items():
        props = res.get("Properties", {})
        if (res["Type"] == "AWS::EC2::SecurityGroupIngress" and _covers_postgres(props)
                and _group(stack, props.get("GroupId"), exports) in targets
                and _group(stack, props.get("SourceSecurityGroupId"), exports) == client):
            rules.add(lid)
    return rules


_CONSUMER_TYPES = ("AWS::Lambda::Function", "AWS::ECS::Service", "AWS::CloudFormation::CustomResource")


def _direct_dependencies(resources: dict, lid: str) -> set[str]:
    out = set(resources[lid].get("DependsOn", []))

    def refs(node):
        target = _ref_target(node)
        if target in resources:
            out.add(target)
        if isinstance(node, dict):
            for value in node.values():
                refs(value)
        elif isinstance(node, list):
            for value in node:
                refs(value)

    refs(resources[lid].get("Properties", {}))
    return out


def _cycle(resources: dict) -> list[str]:
    """One dependency cycle among ``resources`` (empty when there is none)."""
    graph = {lid: _direct_dependencies(resources, lid) for lid in resources}
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(lid: str) -> list[str]:
        state[lid] = 1
        path.append(lid)
        for dep in sorted(graph[lid]):
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
            if dep not in state and (found := visit(dep)):
                return found
        path.pop()
        state[lid] = 2
        return []

    for lid in sorted(graph):
        if lid not in state and (found := visit(lid)):
            return found
    return []


def test_no_template_has_a_dependency_cycle(full, seed_only):
    """A consumer waits on its own resource only: making a function or a
    service wait at the construct level would also make its auto-created
    security group wait for a rule that references that group."""
    for templates in (full, seed_only):
        for stack, template in templates.items():
            assert _cycle(template.get("Resources", {})) == [], stack


def test_each_consumer_waits_for_its_database_rules(full):
    """A function, custom resource or service in a client group must not run
    before that group's database rules in its own stack exist, and must be
    deleted before them (CloudFormation orders only what is referenced or
    declared)."""
    for (stack, prefix), client_prefix in _WAITERS.items():
        resources = full[stack]["Resources"]
        (consumer,) = [k for k, r in resources.items() if _named(k, prefix)
                       and (r["Type"] in _CONSUMER_TYPES or r["Type"].startswith("Custom::"))]
        rules = _database_rules_from(full, stack, _resolve(full, client_prefix))
        assert rules, (stack, prefix, "has no database rule of its own to wait for")
        missing = rules - _waits_for(resources, consumer)
        assert not missing, (stack, prefix, sorted(missing))
