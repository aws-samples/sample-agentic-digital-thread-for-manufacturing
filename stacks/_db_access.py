# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Postgres access to the demo databases by security group, never by CIDR.

The ERP Aurora cluster (``ADTERPDatabaseStack.aurora_sg``) and the seed RDS
instance (``ADTSeedDataStack.db_sg``) each admit 5432 only from the groups of
the resources that connect to them. A client in the database's own stack gets
its rule there; a client in another stack gets a standalone
``CfnSecurityGroupIngress`` on the database group's id in its OWN stack (the
dependent side), so no dependency cycle forms. The clients outside the
database stacks are wired here, after every stack exists:

* the app's ECS task and the AgentCore runtime group (``ADTComputeStack``),
  which every VPC-mode runtime the app or deploy creates attaches (system
  agents, the knowledge-graph runtime, the orchestrator);
* the onboarding runtime group, whose rules live in the
  compute stack too: it already depends on the onboarding stack and on both
  databases, and the onboarding stack depends on neither;
* the HighByte hub's tasks (both databases: the setup function writes the
  hub's JDBC connections);
* the ERP Lambdas' group (``LambdaSG``) on the seed database (the simulation's
  CMMS bridge and the live story's history writer), in the ERP Lambda stack.

Each function, custom resource and ECS service that connects depends on its
own stack's rules for its group: CloudFormation then creates the path before
it runs and removes the path only after it.
"""
from __future__ import annotations

from collections.abc import Iterable

from aws_cdk import aws_ec2 as ec2
from constructs import Construct

POSTGRES_PORT = 5432


def wait_for(rule: Construct, waiters: Iterable[Construct]) -> None:
    """Each waiter's own CloudFormation resource depends on ``rule``.

    Only the waiter's default child: ``node.add_dependency`` on the L2
    construct would also make its children wait, and a function's or a
    service's auto-created security group is one of them, which the rule
    itself references (a circular dependency)."""
    for waiter in waiters:
        (waiter.node.default_child or waiter).node.add_dependency(rule)


def admit_postgres(scope: Construct, construct_id: str, *, database_sg: ec2.ISecurityGroup,
                   client_sg: ec2.ISecurityGroup, description: str,
                   waiters: Iterable[Construct] = ()) -> ec2.CfnSecurityGroupIngress:
    """One 5432 ingress rule on ``database_sg`` from ``client_sg``, created in
    ``scope``; each of ``waiters`` depends on it (:func:`wait_for`)."""
    rule = ec2.CfnSecurityGroupIngress(
        scope, construct_id, group_id=database_sg.security_group_id, ip_protocol="tcp",
        from_port=POSTGRES_PORT, to_port=POSTGRES_PORT,
        source_security_group_id=client_sg.security_group_id, description=description)
    wait_for(rule, waiters)
    return rule


def add_ingress_rule(group: ec2.SecurityGroup, peer: ec2.ISecurityGroup, port: int,
                     description: str) -> Construct:
    """``group.add_ingress_rule`` for a same-stack peer group, returning the
    rule construct it adds, so a consumer can depend on it."""
    before = {child.node.id for child in group.node.children}
    group.add_ingress_rule(peer, ec2.Port.tcp(port), description)
    (rule,) = [child for child in group.node.children if child.node.id not in before]
    return rule


def _databases(seed_data, erp_db) -> list[tuple[str, ec2.ISecurityGroup]]:
    """(construct-id stem, group) of each database this deployment has."""
    found = []
    if erp_db is not None:
        found.append(("ErpAurora", erp_db.aurora_sg))
    if seed_data is not None:
        found.append(("SeedDb", seed_data.db_sg))
    return found


def _wire_compute(compute, onboarding, databases) -> None:
    """The app task, the AgentCore runtimes and the onboarding runtime."""
    service = compute.node.find_child("AdtService")
    clients = (
        ("App", compute.node.find_child("AdtSvcSg"), "the ADT app task", [service]),
        ("AgentCore", compute.node.find_child("AgentCoreRuntimeSg"), "the AgentCore runtimes", []),
        ("Onboarding", onboarding.onboarding_sg, "the onboarding runtime and provisioner", []),
    )
    for stem, database_sg in databases:
        for name, client_sg, label, waiters in clients:
            admit_postgres(compute, f"{stem}IngressFrom{name}", database_sg=database_sg,
                           client_sg=client_sg, description=f"Allow PostgreSQL from {label}",
                           waiters=waiters)


def _wire_highbyte(highbyte, databases) -> None:
    """The HighByte hub's tasks (no hub, no rule: the OPC UA servers alone
    never connect to a database)."""
    if highbyte is None or not highbyte.include_highbyte:
        return
    service = highbyte.highbyte_service.service
    (client_sg,) = service.connections.security_groups
    for stem, database_sg in databases:
        admit_postgres(highbyte, f"{stem}IngressFromHighByteHub", database_sg=database_sg,
                       client_sg=client_sg, description="Allow PostgreSQL from the HighByte hub",
                       waiters=[service])


def _wire_erp_lambdas(erp_lambda, erp_db, seed_data) -> None:
    """The ERP Lambdas' group on the seed database: the simulation's CMMS
    bridge and, on the live story, the history writer and its resource."""
    if erp_lambda is None or seed_data is None:
        return
    waiters = [erp_lambda.lambda_function] + [
        node for node in (getattr(erp_lambda, "story_history_lambda", None),
                          getattr(erp_lambda, "story_history", None)) if node is not None]
    admit_postgres(erp_lambda, "SeedDbIngressFromErpLambdas", database_sg=seed_data.db_sg,
                   client_sg=erp_db.lambda_sg, description="Allow PostgreSQL from the ERP Lambdas",
                   waiters=waiters)


def wire_database_clients(*, seed_data, erp_db, erp_lambda, compute, onboarding,
                          highbyte) -> None:
    """Every cross-stack client's 5432 rule, in the client's own stack. A
    deployment with neither database adds no rule.

    The onboarding group's id is exported in EVERY profile, under the name
    CDK gives the compute stack's import of it: ``cdk deploy`` updates the
    onboarding stack before compute, so a deployment that drops its demo
    databases (seed or simulator back to base) would otherwise remove the
    export while the deployed compute stack still imports it, and stop."""
    onboarding.export_value(onboarding.onboarding_sg.security_group_id)
    databases = _databases(seed_data, erp_db)
    if not databases:
        return
    _wire_compute(compute, onboarding, databases)
    _wire_highbyte(highbyte, databases)
    _wire_erp_lambdas(erp_lambda, erp_db, seed_data)
