# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What the ERP db-init function runs after a full reset outside CloudFormation.

A CloudFormation run of db-init's full reset is followed by the custom
resources that depend on it: the optional fifth plant re-seeds its rows, the
system agents' read-only logins are re-granted, and the Tulip mock's login is
re-granted in the stack that depends on the ERP database stack. A direct reset
(the ERP dashboard's Reset All, or db-init invoked by hand) has no stack run
after it, so db-init runs those steps itself, in that order
(lambda-db-init/handler.py ``run_direct_reset_steps``). Each step is one
synchronous invoke of the function that already does the work: db-init never
seeds a plant or grants a login itself.

* The stack that owns db-init lists its own steps in db-init's environment
  (``STEPS_ENV``, ``wire_reset_steps``), with ``lambda:InvokeFunction`` on
  each step's function and nothing wider.
* A stack that depends on the ERP database stack cannot add to db-init's
  environment or role (a reference to it from the ERP stack would point back
  at the dependent stack: a cycle; a reference from the dependent stack to
  db-init's role would add an export the ERP stack could not drop while that
  stack is deployed). So it writes its steps to an SSM parameter it owns
  (``register_reset_steps``, the Tulip mock's ``REGISTRY_PARAMETER`` and the
  live story's ``STORY_REGISTRY_PARAMETER``), its step functions carry fixed
  names, and the ERP stack, for each dependent registrant in the deployment,
  lets db-init read that parameter and invoke those named functions only
  (``REGISTRY_ENV``, ``wire_reset_registry``). db-init runs the registered
  steps after its own, in the order the parameters are named, and reads them
  before its reset drops anything: with ``REGISTRY_ENV`` set, a missing
  parameter refuses the reset. Two stacks register today, each with its own
  parameter: the Tulip mock's login, and the live-story history
  (``erp_lambda_stack.py``, re-run on a direct reset so the story clock comes
  back as a CloudFormation db-init re-run would leave it).

A step either sends a fixed payload (``invoke_step``) or the Update event
CloudFormation sends a custom resource whose properties did not change
(``custom_resource_step``): the deployed properties as both new and old.
Each step also carries its function's own timeout (``TimeoutSeconds``):
db-init runs the steps after its reset has committed and inside its own
deadline, so it waits on each step for that timeout plus a margin, cut to
what is left of its own time, and starts no step it cannot wait for.
"""
from __future__ import annotations

from aws_cdk import ArnFormat, Stack, aws_iam as iam, aws_lambda as lambda_, aws_ssm as ssm
from constructs import Construct

#: db-init's environment: its own steps (a JSON list) and the registry
#: parameter name(s). lambda-db-init/handler.py reads the same names. The
#: registry env is one bare parameter name, or a JSON list of them when more
#: than one dependent stack registers (:func:`wire_reset_registry`).
STEPS_ENV = "DB_DIRECT_RESET_STEPS"
REGISTRY_ENV = "DB_DIRECT_RESET_REGISTRY"
#: Each registrant owns its own registry parameter (a JSON list of steps, the
#: same shape). The Tulip mock's login uses :data:`REGISTRY_PARAMETER`; the
#: live-story history uses :data:`STORY_REGISTRY_PARAMETER`.
REGISTRY_PARAMETER = "/adt/erp-database/direct-reset-steps"
STORY_REGISTRY_PARAMETER = "/adt/erp-database/direct-reset-steps-story"

Step = tuple[dict, lambda_.Function]


def _timeout_seconds(function: lambda_.Function) -> int:
    """``function``'s own timeout in seconds, which db-init's wait on the
    step follows. A step function must set one."""
    if function.timeout is None:
        raise ValueError(f"a direct-reset step's function sets no timeout: {function.node.path}")
    return int(function.timeout.to_seconds())


def invoke_step(name: str, function: lambda_.Function, payload: dict) -> Step:
    """A step that invokes ``function`` with ``payload``. ``name`` says what
    the step restores, for db-init's log and its failure message."""
    return {"Name": name, "FunctionName": function.function_name,
            "TimeoutSeconds": _timeout_seconds(function), "Payload": payload}, function


def custom_resource_step(name: str, function: lambda_.Function, physical_id: str,
                         properties: dict) -> Step:
    """A step that invokes a custom resource's handler with the Update event
    CloudFormation sends it when nothing changed: ``properties`` (the deployed
    properties, ServiceToken included) as both new and old."""
    return {"Name": name, "FunctionName": function.function_name,
            "TimeoutSeconds": _timeout_seconds(function),
            "PhysicalResourceId": physical_id, "ResourceProperties": properties}, function


def wire_reset_steps(resetter: lambda_.Function, steps: list[Step]) -> None:
    """Give ``resetter`` its own steps, in order, and lambda:InvokeFunction on
    their functions only."""
    resetter.add_environment(STEPS_ENV, Stack.of(resetter).to_json_string([s for s, _f in steps]))
    resetter.add_to_role_policy(iam.PolicyStatement(
        actions=["lambda:InvokeFunction"], resources=[f.function_arn for _s, f in steps]))


def wire_reset_registry(resetter: lambda_.Function,
                        registries: list[tuple[str, list[str]]]) -> None:
    """Let ``resetter`` read the registry parameters dependent stacks register
    their steps in, and invoke the functions those steps name, nothing wider.

    ``registries`` pairs each registry parameter with the fixed function names
    the step(s) in it carry (one pair per registrant: the Tulip mock's login,
    the live story's history). db-init reads the parameters in the order
    given. A single bare parameter name in the env (the only form before a
    second registrant) stays valid, so an account deployed on the old code
    reads its one registry unchanged; more than one is a JSON list."""
    if not registries:
        return
    stack = Stack.of(resetter)
    names = [parameter for parameter, _functions in registries]
    resetter.add_environment(
        REGISTRY_ENV, names[0] if len(names) == 1 else stack.to_json_string(names))
    resetter.add_to_role_policy(iam.PolicyStatement(
        actions=["ssm:GetParameter"],
        resources=[stack.format_arn(service="ssm", resource="parameter",
                                    resource_name=parameter.lstrip("/"))
                   for parameter in names]))
    function_names = [name for _parameter, functions in registries for name in functions]
    resetter.add_to_role_policy(iam.PolicyStatement(
        actions=["lambda:InvokeFunction"],
        resources=[stack.format_arn(service="lambda", resource="function", resource_name=name,
                                    arn_format=ArnFormat.COLON_RESOURCE_NAME)
                   for name in function_names]))


def register_reset_steps(scope: Construct, construct_id: str, steps: list[Step],
                         *, parameter: str = REGISTRY_PARAMETER) -> ssm.StringParameter:
    """From a stack that depends on the ERP database stack: write ``steps``
    to a registry ``parameter`` (the Tulip mock's :data:`REGISTRY_PARAMETER`
    by default; the live story uses :data:`STORY_REGISTRY_PARAMETER`, because
    each registrant owns its own parameter). Each step's function must carry
    the fixed name the ERP stack passes to :func:`wire_reset_registry`."""
    return ssm.StringParameter(
        scope, construct_id, parameter_name=parameter,
        string_value=Stack.of(scope).to_json_string([s for s, _f in steps]),
        description="Steps the ERP db-init function runs after a full reset outside "
                    "CloudFormation (stacks/_direct_reset.py)")
