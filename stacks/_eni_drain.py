# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Drain a VPC Lambda's security group before CloudFormation deletes it.

A Lambda function in a VPC reaches it through Hyperplane ENIs that Lambda owns
and releases on its own schedule after the function leaves the VPC or is
deleted, using the function's execution role to do it. CloudFormation deletes
the function, then the function's security group, and the group's delete gives
up after about 17 minutes ("resource sg-... has a dependent object"). Turning an
optional stack off in place hits this: Lambda left two ``available`` interfaces
with no attachment on each of the Tulip mock's groups for more than a day, and
the mock's nested stack ended DELETE_FAILED.

:func:`drain_on_delete` gives a security group a drain: a custom resource
(``Custom::ADTVpcLambdaEniDrain``) that the function depends on and that
references the group, so a delete runs function -> drain -> group. When
CloudFormation state proves the group is being removed, the drain's Delete
waits, through the CDK provider framework's ``is_complete`` polling, until no
network interface references the group. After a short grace period that gives
Lambda the first chance, it deletes the interfaces that are provably Lambda
leftovers on that group (lambda-eni-drain/eni_drain.py lists the gates; anything
else is left alone), and at 55 minutes it answers SUCCESS whatever is left. It
never answers FAILED. Create and Update do nothing and its properties are only
the group's id and logical id and the drain revision (a constant), so a normal
deploy never runs it.

A stack delete runs the drain the stack was last deployed with. Every drain and
detach therefore carries ``EniDrainRevision`` (stacks/_eni_drain_revision.py),
which deploy.sh reads from a mock's deployed template before deleting the mock:
a mock deployed before this drain is first updated with it kept on. The same
read ties each VPC function to its own drain and detach through the links made
here (the drain's group id and logical id, the function's DependsOn on its
drain, the detach's Functions entry), so changing those links means changing
the classifier with them.

The drain also waits for each function's execution role, so CloudFormation
deletes the role after the drain: Lambda releases a function's interfaces with
that role and leaves them behind once it is gone.

One provider per stack serves every drain in it. Its own functions run outside
the VPC. Its is_complete may call ``ec2:DescribeNetworkInterfaces`` (no
resource) and ``ec2:DeleteNetworkInterface`` only on interfaces in the drained
groups' VPC and the drained functions' subnets: the action has no condition
key for a security group, status, attachment or description, so those gates
are the handler's.

Use it only for a group the stack owns and only its own functions use: a drain
on a shared group would wait out the whole bound on every delete, and could
delete an interface another function's Lambda left behind on that group.
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnResource,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    custom_resources as cr,
)
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo
from constructs import Construct

from stacks._checkov_skips import checkov_skip
from stacks._eni_drain_revision import (
    DETACH_RESOURCE_TYPE as _DETACH_RESOURCE_TYPE,
    DRAIN_RESOURCE_TYPE as _DRAIN_RESOURCE_TYPE,
    ENI_DRAIN_REVISION,
    REVISION_PROPERTY,
)

#: The drain's resource type (tests and tooling find drains by it).
RESOURCE_TYPE = _DRAIN_RESOURCE_TYPE
#: The detach's resource type (deleted before its function; see detach_before_delete).
DETACH_RESOURCE_TYPE = _DETACH_RESOURCE_TYPE
#: Every drain and detach carries ENI_DRAIN_REVISION under this property, so an
#: operator path can tell from a deployed template whether deleting the stack
#: would run this drain (stacks/_eni_drain_revision.py).
_REVISION_PROPERTIES = {REVISION_PROPERTY: str(ENI_DRAIN_REVISION)}
#: The provider framework's maximum total timeout. The handler's own budget
#: (DRAIN_BUDGET_SECONDS) ends before it, so the framework never answers
#: FAILED with "Operation timed out".
TOTAL_TIMEOUT = Duration.hours(1)
QUERY_INTERVAL = Duration.seconds(30)
#: Pinned equal to eni_drain.IS_COMPLETE_TIMEOUT_SECONDS by a test. A drain
#: poll is one describe plus up to eni_drain.MAX_DELETES_PER_POLL deletes, and
#: a test checks they fit with margin at the clients' bounds. Shared by the
#: detach's is_complete, whose poll is shorter.
IS_COMPLETE_TIMEOUT = Duration.seconds(120)
#: on_event reads CloudFormation state (DescribeStacks, and GetTemplate on an
#: update cleanup) and, for the detach, a function get and update. The longest
#: path is the detach's four calls per protected function; at the clients'
#: bounds each call costs attempts x (connect + read) + (attempts - 1) x 20 s of
#: standard-mode retry backoff, so four calls for one function fit inside this
#: with at least 10 s of margin, and the drain's two-call path fits with room to
#: spare. A test per handler recomputes the bound from the real Config values
#: and synthesized function counts, so a change to either breaks the test.
ON_EVENT_TIMEOUT = Duration.seconds(150)
#: The construct id of the per-stack provider (and the drains' id prefix).
SINGLETON_ID = "VpcLambdaEniDrain"
_HANDLER_DIR = str(Path(__file__).resolve().parents[1] / "lambda-eni-drain")
#: cdk-nag's finding for the leftover delete's resource (this account's
#: network-interface/*, whatever the Region and account render as).
_NETWORK_INTERFACES_NAG = RegexAppliesTo(
    regex=("/^Resource::arn:<AWS::Partition>:ec2:[^:]+:(<AWS::AccountId>|\\d{12}):"
           "network-interface\\/\\*$/g"))

_CHECKOV_SKIPS = [
    ("CKV_AWS_115", "Runs only when CloudFormation creates, updates or deletes one of the stack's "
                    "drain custom resources; reserved concurrency would take capacity from the "
                    "account pool for a function that runs a few times per stack lifetime."),
    ("CKV_AWS_116", "Invoked synchronously by the CDK provider framework, which answers "
                    "CloudFormation itself; a dead-letter queue would never receive an event."),
    ("CKV_AWS_117", "Deliberately outside the VPC: it only calls the EC2 API to list the "
                    "network interfaces on a security group and delete the ones Lambda left "
                    "behind, and an in-VPC drain would add an ENI of its own to the subnets it "
                    "is waiting on."),
]


class VpcLambdaEniDrain(Construct):
    """The per-stack provider every drain in the stack shares (see the module
    docstring). Use :func:`drain_on_delete` rather than this class directly."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        log_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK,
                                  removal_policy=RemovalPolicy.DESTROY)
        self.on_event_fn = self._function("OnEvent", "eni_drain.on_event", ON_EVENT_TIMEOUT,
                                          log_group)
        self.is_complete_fn = self._function("IsComplete", "eni_drain.is_complete",
                                             IS_COMPLETE_TIMEOUT, log_group)
        # DescribeNetworkInterfaces takes no resource; the handler filters it
        # to the drained group at call time. The leftover delete is granted per
        # drained group in add(), scoped to that group's VPC and subnets.
        self.is_complete_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ec2:DescribeNetworkInterfaces"], resources=["*"]))
        # on_event reads CloudFormation state before waiting (eni_removal.py):
        # DescribeStacks (no resource) and, on an update cleanup, GetTemplate on
        # this stack's own ARN.
        self.on_event_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks"], resources=["*"]))
        self.on_event_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudformation:GetTemplate"], resources=[Stack.of(self).stack_id]))
        self.provider = cr.Provider(
            self, "Provider",
            on_event_handler=self.on_event_fn,
            is_complete_handler=self.is_complete_fn,
            total_timeout=TOTAL_TIMEOUT,
            query_interval=QUERY_INTERVAL,
            log_group=log_group,
        )
        self._document_findings()

    def _function(self, construct_id: str, handler: str, timeout: Duration,
                  log_group: logs.ILogGroup) -> lambda_.Function:
        return lambda_.Function(
            self, construct_id,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler=handler,
            code=lambda_.Code.from_asset(_HANDLER_DIR, exclude=["__pycache__", "*.pyc"]),
            timeout=timeout,
            memory_size=128,
            log_group=log_group,
            description=("ADT: on delete, waits until no network interface references a VPC "
                         "Lambda's security group, deleting the ones Lambda left behind "
                         "(custom resource handler)"),
        )

    def _document_findings(self) -> None:
        """cdk-nag and checkov reasons for this construct's own resources, so
        a stack that uses it needs no new stack-level suppression."""
        stack = Stack.of(self)
        user_fns = (self.on_event_fn, self.is_complete_fn)
        framework_fns = [self.provider.node.find_child(name) for name in
                         ("framework-onEvent", "framework-isComplete", "framework-onTimeout")]
        for fn in (*user_fns, *framework_fns):
            checkov_skip(fn, _CHECKOV_SKIPS)
        function_ids = [stack.get_logical_id(fn.node.default_child)
                        for fn in (*user_fns, *framework_fns)]
        NagSuppressions.add_resource_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason=("The drain's functions and the provider framework's use the AWS-managed "
                        "basic execution role (CloudWatch Logs only)."),
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason=("ec2:DescribeNetworkInterfaces and cloudformation:DescribeStacks take no "
                        "resource (the handler filters the first to the drained security group "
                        "and reads only its own stack with the second); "
                        "ec2:DeleteNetworkInterface names no interface until Lambda creates it, "
                        "so it is on this account's network-interface/* and conditioned to the "
                        "drained groups' VPC and the drained functions' subnets; and the CDK "
                        "provider framework's roles may invoke every version of the drain's own "
                        "functions (<function ARN>:*)."),
                applies_to=["Resource::*", _NETWORK_INTERFACES_NAG, *(
                    RegexAppliesTo(regex=f"/^Resource::<{lid}\\.Arn>:\\*$/g")
                    for lid in function_ids)],
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason=("The drain pins Python 3.12 like the repository's other Python Lambdas; "
                        "the provider framework's Node.js runtime is chosen by CDK."),
            ),
            NagPackSuppression(
                id="AwsSolutions-SF1",
                reason=("The provider framework's waiter state machine only polls the drain's "
                        "is_complete function; each poll logs to the drain's own log group, so "
                        "state machine execution logging would repeat it."),
            ),
            NagPackSuppression(
                id="AwsSolutions-SF2",
                reason=("X-Ray tracing on the provider framework's waiter state machine would "
                        "trace a 30-second poll loop that only runs while a stack deletes."),
            ),
        ], apply_to_children=True)

    @classmethod
    def of(cls, stack: Stack) -> "VpcLambdaEniDrain":
        """The stack's drain provider, created on first use."""
        existing = stack.node.try_find_child(SINGLETON_ID)
        return existing if isinstance(existing, cls) else cls(stack, SINGLETON_ID)

    def add(self, security_group: ec2.SecurityGroup,
            functions: tuple[lambda_.Function, ...]) -> CustomResource:
        resource = CustomResource(
            self, _drain_id(security_group),
            service_token=self.provider.service_token,
            resource_type=RESOURCE_TYPE,
            # The group id (the Ref makes the drain wait for the group, deleted
            # after it), the group's logical id (for the per-resource removal
            # decision, eni_removal.py) and the drain revision. A deploy never
            # re-runs the drain: only a new revision sends it an Update, which
            # makes no AWS call.
            properties={"SecurityGroupId": security_group.security_group_id,
                        "SecurityGroupLogicalId": Stack.of(security_group).get_logical_id(
                            security_group.node.default_child),
                        **_REVISION_PROPERTIES},
        )
        drain = resource.node.default_child
        for fn in functions:
            # On the function's own resource, not the construct: a CDK-made
            # group is the function construct's child and must not wait for
            # its drain (a cycle).
            function = fn.node.default_child
            if not isinstance(function, CfnResource):
                raise TypeError(f"{fn.node.path} has no function resource to order")
            function.add_dependency(drain)
            # Lambda releases the function's interfaces with this role and
            # leaves them behind once the role is deleted, so the drain waits
            # for the role: a delete then removes the role only after the
            # drain's Delete, giving Lambda's own release a real chance during
            # the grace. The role never depends on the drain (no cycle). A role
            # from outside this stack is not deleted with it: nothing to order.
            role = fn.role.node.default_child if fn.role is not None else None
            if isinstance(role, CfnResource) and Stack.of(role) is Stack.of(drain):
                drain.add_dependency(role)
        self._grant_leftover_delete(security_group, functions)
        return resource

    def _grant_leftover_delete(self, security_group: ec2.SecurityGroup,
                               functions: tuple[lambda_.Function, ...]) -> None:
        """Let is_complete delete network interfaces, but only ones in this
        group's VPC and in the subnets its functions run in (ec2:Vpc and
        ec2:Subnet are the narrowing condition keys DeleteNetworkInterface
        supports; nothing in IAM can name the group, the status, the
        attachment or the description, so the handler checks those). Every
        drain in a stack shares the workshop VPC and its private subnets, so
        the per-group statements are identical and the policy carries one."""
        stack = Stack.of(self)
        group = security_group.node.default_child
        if not isinstance(group, ec2.CfnSecurityGroup) or not group.vpc_id:
            raise TypeError(f"{security_group.node.path} has no VPC to scope the delete to")
        subnets: list[str] = []
        for fn in functions:
            config = fn.node.default_child.vpc_config
            subnet_ids = getattr(config, "subnet_ids", None)
            if not isinstance(subnet_ids, list) or not subnet_ids:
                raise TypeError(f"{fn.node.path} has no subnets to scope the delete to")
            subnets += [subnet for subnet in subnet_ids if subnet not in subnets]
        self.is_complete_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ec2:DeleteNetworkInterface"],
            resources=[stack.format_arn(service="ec2", resource="network-interface",
                                        resource_name="*")],
            conditions={"ArnEquals": {
                "ec2:Vpc": stack.format_arn(service="ec2", resource="vpc",
                                            resource_name=group.vpc_id),
                "ec2:Subnet": [stack.format_arn(service="ec2", resource="subnet",
                                                resource_name=subnet) for subnet in subnets],
            }}))


def _drain_id(security_group: ec2.SecurityGroup) -> str:
    """The group's construct path below its stack, joined (one drain per group)."""
    stack_path = Stack.of(security_group).node.path
    return security_group.node.path[len(stack_path) + 1:].replace("/", "")


def drain_on_delete(security_group: ec2.ISecurityGroup, *functions: lambda_.Function
                    ) -> CustomResource:
    """Give ``security_group`` a drain that runs after ``functions`` are
    deleted and before the group is, and deletes the network interfaces Lambda
    leaves behind on it.

    ``security_group`` must be created by the same stack, used only by
    ``functions``, and every function must run in it. A function may also use
    other groups, each with its own drain."""
    if not isinstance(security_group, ec2.SecurityGroup):
        raise TypeError(f"{security_group.node.path}: drain only a security group this stack "
                        "creates, not an imported one")
    stack = Stack.of(security_group)
    if not functions:
        raise ValueError(f"{security_group.node.path}: name the functions that use the group")
    for fn in functions:
        if Stack.of(fn) is not stack:
            raise ValueError(f"{fn.node.path} is not in {stack.node.path}")
        if security_group.node.path not in {g.node.path for g in fn.connections.security_groups}:
            raise ValueError(f"{fn.node.path} does not run in {security_group.node.path}")
    return VpcLambdaEniDrain.of(stack).add(security_group, functions)


_DETACH_CHECKOV_SKIPS = [
    ("CKV_AWS_115", "Runs only when CloudFormation deletes one of the stack's detach custom "
                    "resources; reserved concurrency would take capacity from the account pool "
                    "for a function that runs a few times per stack lifetime."),
    ("CKV_AWS_116", "Invoked synchronously by the CDK provider framework, which answers "
                    "CloudFormation itself; a dead-letter queue would never receive an event."),
    ("CKV_AWS_117", "Deliberately outside the VPC: on a stack delete it takes the stack's VPC "
                    "functions out of the VPC so Lambda releases their ENIs, which an in-VPC "
                    "function could not do to itself."),
]


class VpcLambdaEniDetach(Construct):
    """One detach provider and its custom resource, scoped to the functions it
    protects. On a delete it takes each of those functions out of the VPC when
    CloudFormation state proves that function is being removed, before the
    function is deleted, so Lambda starts releasing its ENIs while the function
    still exists. One per
    :func:`detach_before_delete` call rather than one per stack, so the
    provider's role references only its own functions: a shared role would make
    every detach depend on every function and, with a function that other
    resources depend on, form a cycle. Use :func:`detach_before_delete`."""

    def __init__(self, scope: Construct, construct_id: str,
                 functions: tuple[lambda_.Function, ...],
                 invokers: tuple[CustomResource, ...]) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        log_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK,
                                  removal_policy=RemovalPolicy.DESTROY)
        self.on_event_fn = self._function("OnEvent", "eni_detach.on_event", ON_EVENT_TIMEOUT,
                                          log_group)
        self.is_complete_fn = self._function("IsComplete", "eni_detach.is_complete",
                                             IS_COMPLETE_TIMEOUT, log_group)
        # on_event reads CloudFormation state before any detach (eni_removal.py):
        # DescribeStacks (no resource) for the stack status, and, on an update
        # cleanup, GetTemplate to confirm the function's logical id is gone.
        # GetTemplate is scoped to this stack's own ARN; DescribeStacks is not
        # resource-scopable.
        self.on_event_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks"], resources=["*"]))
        self.on_event_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudformation:GetTemplate"], resources=[stack.stack_id]))
        for fn in functions:
            for action in ("lambda:GetFunctionConfiguration",
                           "lambda:UpdateFunctionConfiguration"):
                self.on_event_fn.add_to_role_policy(iam.PolicyStatement(
                    actions=[action], resources=[fn.function_arn]))
            self.is_complete_fn.add_to_role_policy(iam.PolicyStatement(
                actions=["lambda:GetFunctionConfiguration"], resources=[fn.function_arn]))
        self.provider = cr.Provider(
            self, "Provider",
            on_event_handler=self.on_event_fn,
            is_complete_handler=self.is_complete_fn,
            total_timeout=TOTAL_TIMEOUT,
            query_interval=QUERY_INTERVAL,
            log_group=log_group,
        )
        self._document_findings()
        self.resource = self._add(functions, invokers)

    def _function(self, construct_id: str, handler: str, timeout: Duration,
                  log_group: logs.ILogGroup) -> lambda_.Function:
        return lambda_.Function(
            self, construct_id,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler=handler,
            code=lambda_.Code.from_asset(_HANDLER_DIR, exclude=["__pycache__", "*.pyc"]),
            timeout=timeout,
            memory_size=128,
            log_group=log_group,
            description=("ADT: on a stack delete, takes a VPC Lambda out of the VPC so Lambda "
                         "releases its ENIs (custom resource handler)"),
        )

    def _document_findings(self) -> None:
        """cdk-nag and checkov reasons for this construct's own resources."""
        stack = Stack.of(self)
        user_fns = (self.on_event_fn, self.is_complete_fn)
        framework_fns = [self.provider.node.find_child(name) for name in
                         ("framework-onEvent", "framework-isComplete", "framework-onTimeout")]
        for fn in (*user_fns, *framework_fns):
            checkov_skip(fn, _DETACH_CHECKOV_SKIPS)
        function_ids = [stack.get_logical_id(fn.node.default_child)
                        for fn in (*user_fns, *framework_fns)]
        NagSuppressions.add_resource_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason=("The detach's functions and the provider framework's use the AWS-managed "
                        "basic execution role (CloudWatch Logs only)."),
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason=("cloudformation:DescribeStacks takes no resource (the handler reads its "
                        "own stack by StackId), the Lambda configuration actions are scoped to "
                        "the named function ARNs, and the CDK provider framework's roles may "
                        "invoke every version of the detach's own functions (<function ARN>:*)."),
                applies_to=["Resource::*", *(
                    RegexAppliesTo(regex=f"/^Resource::<{lid}\\.Arn>:\\*$/g")
                    for lid in function_ids)],
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason=("The detach pins Python 3.12 like the repository's other Python Lambdas; "
                        "the provider framework's Node.js runtime is chosen by CDK."),
            ),
            NagPackSuppression(
                id="AwsSolutions-SF1",
                reason=("The provider framework's waiter state machine only polls the detach's "
                        "is_complete function; each poll logs to the detach's own log group, so "
                        "state machine execution logging would repeat it."),
            ),
            NagPackSuppression(
                id="AwsSolutions-SF2",
                reason=("X-Ray tracing on the provider framework's waiter state machine would "
                        "trace a short poll loop that only runs while a stack deletes."),
            ),
        ], apply_to_children=True)

    def _add(self, functions: tuple[lambda_.Function, ...],
             invokers: tuple[CustomResource, ...]) -> CustomResource:
        stack = Stack.of(self)
        resource = CustomResource(
            self, "Resource",
            service_token=self.provider.service_token,
            resource_type=DETACH_RESOURCE_TYPE,
            # Each function by ARN (for the detach call) and logical id (for the
            # per-function removal decision, eni_removal.py), and the drain
            # revision (an Update for a new one makes no AWS call).
            properties={"Functions": [
                {"Arn": fn.function_arn,
                 "LogicalId": stack.get_logical_id(fn.node.default_child)}
                for fn in functions], **_REVISION_PROPERTIES},
        )
        detach = resource.node.default_child
        if not isinstance(detach, CfnResource):
            raise TypeError("the detach custom resource has no CfnResource to order")
        for fn in functions:
            function = fn.node.default_child
            if not isinstance(function, CfnResource):
                raise TypeError(f"{fn.node.path} has no function resource to order")
            # The detach waits for the function, so a delete removes the detach
            # (its Delete takes the function out of the VPC while it still
            # exists) before it deletes the function. The function waits for
            # nothing of the detach, so no cycle forms.
            detach.add_dependency(function)
        for invoker in invokers:
            # Each custom resource that invokes a function on its own Delete
            # (dropping its database login) must finish that Delete before the
            # detach cuts the function off from the VPC: invoker DependsOn the
            # detach, so the delete runs the invoker first, then the detach.
            invoker_resource = invoker.node.default_child
            if not isinstance(invoker_resource, CfnResource):
                raise TypeError(f"{invoker.node.path} has no resource to order")
            invoker_resource.add_dependency(detach)
        return resource


def _detach_id(function: lambda_.Function) -> str:
    stack_path = Stack.of(function).node.path
    return "Detach" + function.node.path[len(stack_path) + 1:].replace("/", "")


def detach_before_delete(*functions: lambda_.Function,
                         invokers: tuple[CustomResource, ...] = ()) -> CustomResource:
    """Give ``functions`` a detach resource, deleted before them, that takes
    each out of the VPC so Lambda starts releasing its ENIs while the function
    still exists. The handler
    decides per function, through eni_removal.removal_proven: it detaches a
    function only when CloudFormation state proves that function is being
    removed, so a function that stays (an update rollback, or a replacement) is
    never cut off from its database (lambda-eni-drain/eni_detach.py).

    ``invokers`` are custom resources that invoke a function on their own Delete
    (to drop a database login); each is ordered to finish its Delete before the
    detach runs. Every function must be in the same stack and run in a VPC. Pair
    it with :func:`drain_on_delete`, which deletes what Lambda leaves behind."""
    if not functions:
        raise ValueError("name the functions to detach before deleting")
    stack = Stack.of(functions[0])
    for fn in functions:
        if Stack.of(fn) is not stack:
            raise ValueError(f"{fn.node.path} is not in {stack.node.path}")
        if not fn.connections.security_groups:
            raise ValueError(f"{fn.node.path} does not run in a VPC, so it has nothing to detach")
    return VpcLambdaEniDetach(stack, _detach_id(functions[0]), functions, invokers).resource
