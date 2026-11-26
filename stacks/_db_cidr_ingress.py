# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Removing the demo databases' VPC-wide PostgreSQL rule without a gap.

Each demo database (the ERP Aurora cluster, the seed RDS instance) admits
5432 from its client groups only (stacks/_db_access.py). A deployment created
before those client rules existed carries one inline VPC-CIDR 5432 rule on the
database's group, and most of the new client rules live in stacks that
CloudFormation updates AFTER the database stacks (they import the database
group ids, so no DependsOn can reverse the order). Dropping the rule in the
same deploy would leave those clients with no path until their own stacks
update, and with none at all if one of those updates failed.

So each database stack declares ``DatabaseCidrIngress``:

* ``remove`` (default): no VPC-wide rule. A fresh create (a Workshop Studio
  event, a first deploy.sh run) needs nothing else.
* ``keep``: the group keeps the VPC-wide rule exactly as such a deployment
  already holds it, so its update changes no rule.

An in-place update of a deployment that still has the rule runs twice. Phase 1
deploys every stack; a database stack that already has the rule gets
``keep``, so each client rule is created while it stays, and any other
database stack (at ``remove``, or new) gets ``remove`` and never has the rule.
Phase 2 deploys only the kept database stacks with ``remove``; nothing else in
their templates differs, so it changes only their groups, and an update that
fails rolls back to the rule. deploy.sh runs both phases itself, deciding per
database stack from its ``DatabaseCidrIngressState`` output (absent on a stack
from before this switch) and stopping before any change when it cannot read
one. The workshop root declares the same parameter, passes it to both
database children (always present together, so one value fits both) and
re-surfaces the output, for ``tools/rehearse-event.sh update`` in the
workshop repo.

A deploy that ends with ``keep`` says so: the output (cdk deploy prints it; the
workshop root re-surfaces it), and deploy.sh fails at its end while a database
stack still reports anything but ``remove``. ``cdk deploy`` keeps a stack's
previous parameter value when none is given, so deploy.sh always passes one.
"""
from __future__ import annotations

from aws_cdk import Aws, CfnCondition, CfnOutput, CfnParameter, Fn, Stack, aws_ec2 as ec2

PARAMETER = "DatabaseCidrIngress"
KEEP, REMOVE = "keep", "remove"
VALUES = (KEEP, REMOVE)
DEFAULT = REMOVE
CONDITION = "DatabaseCidrIngressKept"
#: The output reporting the value in effect (an output cannot share the
#: parameter's logical id).
STATE_OUTPUT = "DatabaseCidrIngressState"
POSTGRES_PORT = 5432

DESCRIPTION = (
    "remove (default): the database admits PostgreSQL only from its client security groups. "
    "keep: it also keeps the VPC-wide 5432 rule a stack created before those client rules "
    "carries. An in-place update of such a stack deploys keep first, then remove."
)
STATE_DESCRIPTION = (
    "DatabaseCidrIngress in effect. keep means phase 2 has not run: the database still "
    "admits PostgreSQL from the whole VPC; deploy again with remove (deploy.sh does)."
)


def add_legacy_cidr_ingress(stack: Stack, security_group: ec2.SecurityGroup, cidr: str,
                            description: str) -> None:
    """The switch on ``stack``, and ``security_group``'s inline VPC-wide 5432
    rule (``cidr``, ``description``) only while it is ``keep``.

    The rule is the group's whole inline ingress (its other rules are
    standalone resources), so it is written as one ``Fn::If`` on the
    property: with ``keep`` the property is exactly the deployed one."""
    parameter = CfnParameter(stack, PARAMETER, type="String", allowed_values=list(VALUES),
                             default=DEFAULT, description=DESCRIPTION)
    CfnCondition(stack, CONDITION, expression=Fn.condition_equals(parameter.value_as_string, KEEP))
    rule = {"CidrIp": cidr, "Description": description, "FromPort": POSTGRES_PORT,
            "IpProtocol": "tcp", "ToPort": POSTGRES_PORT}
    security_group.node.default_child.add_property_override(
        "SecurityGroupIngress", Fn.condition_if(CONDITION, [rule], Aws.NO_VALUE))
    CfnOutput(stack, STATE_OUTPUT, value=parameter.value_as_string, description=STATE_DESCRIPTION)
