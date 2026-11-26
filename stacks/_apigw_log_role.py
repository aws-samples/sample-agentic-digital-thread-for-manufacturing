# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The account's API Gateway CloudWatch Logs role: one owner per account and
region, outside the workshop.

REST API stage access logging needs the account-level API Gateway CloudWatch
role (``AWS::ApiGateway::Account``, one value per account and region). A
stack must not simply declare that resource: it overwrites whatever role the
account already uses and clears it on delete, even while other APIs still
log through it.

So a non-workshop deploy has exactly ONE owner of the setting: a custom
resource in ``ADTNetworkStack`` (:func:`ensure_account_log_role`, applied
from app.py by ``stacks/_sample_hardening.harden``). Every stack with a REST
API stage is made to depend on ADTNetworkStack
(``_sample_hardening.own_account_log_role``), so the owner is created before
any consumer and deleted after the last one.

The account setting is shared account state, so the owner never clears or
replaces a working setting (``HANDLER_SOURCE``):

- Create and Update: if the account setting is empty, or names a role API
  Gateway cannot use (gone, not trusted by API Gateway, or denied the
  CloudWatch Logs actions), set it to this stack's role. Otherwise leave it
  exactly as it is, whoever set it. Nothing is recorded, so a retried
  Create or Update does the same thing again.
- Delete: do nothing to the account setting.

This stack's IAM role is RETAINED when the stack is deleted (the same choice
CDK's ``RestApi`` makes with ``cloudWatchRoleRemovalPolicy`` RETAIN), so a
setting that names it never points at a role that is gone. That one role is
left in the account on purpose: path ``/adt/apigateway-cloudwatch/``, name
starting ``ADTNetworkStack-ApiGatewayAccountLogRole``. cleanup.sh and the
README say how to remove it once no API Gateway in the account and region
logs to CloudWatch.

The workshop (``with_workshop``) never builds this, and its stacks keep their
own settings as rehearsed. A stack-local setter of another REST API stack,
created after this one, finds a usable role set and never owns the setting.
"""

from __future__ import annotations

from aws_cdk import (
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    custom_resources as cr,
)

#: Construct id prefix (role ``<prefix>``, then Fn/FnLogs/Provider/Setting).
ID_PREFIX = "ApiGatewayAccountLogRole"

#: The custom resource's one, constant physical id (it records nothing).
PHYSICAL_ID = "adt-apigateway-cloudwatch-role-setting"

#: The retained role's IAM path, so it is easy to find later.
ROLE_PATH = "/adt/apigateway-cloudwatch/"

HANDLER_SOURCE = '''\
import json
import time
import urllib.parse
import boto3
from botocore.exceptions import ClientError

PHYSICAL_ID = "adt-apigateway-cloudwatch-role-setting"
LOGS = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:DescribeLogGroups",
        "logs:DescribeLogStreams", "logs:PutLogEvents"]


def _list(v):
    return [v] if isinstance(v, str) else (v or [])


def _trusts(doc, account):
    ok = False
    for st in doc.get("Statement", []):
        p = st.get("Principal")
        svc = _list(p.get("Service")) if isinstance(p, dict) else []
        if "apigateway.amazonaws.com" not in svc and p != "*":
            continue
        if not {"sts:assumerole", "sts:*", "*"} & {a.lower() for a in _list(st.get("Action"))}:
            continue
        if st.get("Effect") == "Deny":
            return False
        cond = st.get("Condition") or {}
        if not cond or cond == {"StringEquals": {"aws:SourceAccount": account}}:
            ok = True
    return ok


def _usable(iam, arn, account):
    if not arn:
        return False
    name = arn.rsplit("/", 1)[-1]
    try:
        doc = iam.get_role(RoleName=name)["Role"]["AssumeRolePolicyDocument"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchEntity":
            return False
        raise
    if isinstance(doc, str):
        doc = json.loads(urllib.parse.unquote(doc))
    if not _trusts(doc, account):
        return False
    res = iam.simulate_principal_policy(PolicySourceArn=arn, ActionNames=LOGS)
    return all(r["EvalDecision"] == "allowed" for r in res["EvaluationResults"])


def _patch(apigw, value, retry=("BadRequestException", "TooManyRequestsException")):
    ops = [{"op": "replace", "path": "/cloudwatchRoleArn", "value": value}]
    for attempt in range(12):
        try:
            return apigw.update_account(patchOperations=ops)
        except ClientError as exc:
            # A new role can fail the trust check for a few seconds, and
            # UpdateAccount is throttled hard.
            if exc.response["Error"]["Code"] not in retry or attempt == 11:
                raise
            print("update_account retry %d: %s" % (attempt + 1, exc))
            time.sleep(min(10 * (attempt + 1), 30))


def on_event(event, _context):
    """Create/Update: set the account's API Gateway CloudWatch role to ours
    only when it is empty or unusable. Delete: leave it alone (our role is
    retained by CloudFormation)."""
    if event["RequestType"] == "Delete":
        print("Delete: the account's API Gateway CloudWatch role setting is left as is")
        return {"PhysicalResourceId": PHYSICAL_ID}
    role = event["ResourceProperties"]["RoleArn"]
    account = role.split(":")[4]
    apigw = boto3.client("apigateway")
    current = apigw.get_account().get("cloudwatchRoleArn") or ""
    if current == role or (current and _usable(boto3.client("iam"), current, account)):
        print("API Gateway CloudWatch role already set and usable, left as is: " + current)
    else:
        _patch(apigw, role)
        print("Set the API Gateway CloudWatch role: " + role + " (was " + (current or "unset") + ")")
    return {"PhysicalResourceId": PHYSICAL_ID}
'''


def ensure_account_log_role(stack: Stack) -> CustomResource:
    """Add the account API Gateway CloudWatch role setter to *stack*
    (ADTNetworkStack). Its role is retained on stack delete."""
    role = iam.Role(
        stack, ID_PREFIX,
        assumed_by=iam.ServicePrincipal("apigateway.amazonaws.com"),
        path=ROLE_PATH,
        description="API Gateway CloudWatch Logs role for this account and region (set by "
        "the ADT network stack only when the account had no usable one; RETAINED on delete)",
        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
            "service-role/AmazonAPIGatewayPushToCloudWatchLogs")],
    )
    # Retained: the account setting may name it after this stack is gone,
    # and other APIs may log through it.
    role.apply_removal_policy(RemovalPolicy.RETAIN)
    setter = lambda_.Function(
        stack,
        f"{ID_PREFIX}Fn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.ARM_64,
        handler="index.on_event",
        code=lambda_.Code.from_inline(HANDLER_SOURCE),
        timeout=Duration.minutes(7),  # the UpdateAccount retries sleep up to 5 minutes
        memory_size=128,
        log_group=logs.LogGroup(stack, f"{ID_PREFIX}FnLogs",
                                retention=logs.RetentionDays.ONE_WEEK,
                                removal_policy=RemovalPolicy.DESTROY),
        description="ADT: sets the account's API Gateway CloudWatch role if it has no usable one "
        "(deploy time only)",
    )
    setter.add_to_role_policy(iam.PolicyStatement(
        actions=["apigateway:GET", "apigateway:PATCH"],
        resources=[Fn.sub("arn:${AWS::Partition}:apigateway:${AWS::Region}::/account")]))
    setter.add_to_role_policy(iam.PolicyStatement(
        # Is the role the account names usable? (trust policy read + policy
        # simulator; the role can be any role in this account)
        actions=["iam:GetRole", "iam:SimulatePrincipalPolicy"],
        resources=[Fn.sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:role/*")]))
    setter.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"], resources=[role.role_arn],
        conditions={"StringEquals": {"iam:PassedToService": "apigateway.amazonaws.com"}}))
    provider = cr.Provider(stack, f"{ID_PREFIX}Provider", on_event_handler=setter)
    return CustomResource(
        stack,
        f"{ID_PREFIX}Setting",
        service_token=provider.service_token,
        properties={"RoleArn": role.role_arn},
    )
