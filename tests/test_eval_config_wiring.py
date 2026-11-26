# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""assertions.Template tests for ``stacks/_evaluations.wire_eval_config``.

The CDK-native managed-evaluations wiring must:
  * with a NON-EMPTY orchestrator runtime ARN, create the
    ``AWS::BedrockAgentCore::OnlineEvaluationConfig`` resource (name
    ``adt_orchestrator_eval``, log-group/service-name data source derived
    from the ARN, the built-in evaluator trio, sampling 10) plus its
    execution role, and inject ``EVAL_CONFIG_ORCHESTRATOR`` on the adt-app
    container sourced DIRECTLY from the resource's id attribute (Fn::GetAtt
    — no SSM round-trip) — the env name is pinned to what
    ``agents/agents_routes.py::EVAL_CONFIG_MAP`` reads;
  * with an EMPTY ARN (parameter absent / offline synth / fail-soft read
    failure) synthesize a template byte-identical to before — no resource,
    no role, no env (the reader's empty-string default keeps scores off);
  * fail synth loudly on a malformed ARN or a renamed task-def construct id;
  * with the ARN, add the code-based figure check: its Lambda (a plain
    Python asset, no environment), the ``AWS::BedrockAgentCore::Evaluator``
    over it at level TRACE, listed in the config after the built-ins, and an
    inline ``lambda:InvokeFunction`` / ``lambda:GetFunction`` grant on the
    execution role scoped to exactly that function; the Lambda runs under its
    own role, with no managed policy, that may write only its own log group.

Synthesized fully offline via ``assertions.Template`` against a minimal stack
carrying the same construct id the wiring locates ("AdtTaskDef" — the
ADTComputeStack seam), mirroring tests/test_observability_wiring.py.

Run:  PYTHONPATH=. python -m pytest tests/test_eval_config_wiring.py -v
"""
from __future__ import annotations

import json

import pytest
import aws_cdk as cdk
from aws_cdk import assertions, aws_ecs as ecs

from stacks._evaluations import (
    BUILTIN_EVALUATOR_IDS,
    CONFIG_NAME,
    DEFAULT_SAMPLING_PERCENTAGE,
    FIGURE_CHECK_EVALUATOR_NAME,
    FIGURE_CHECK_TIMEOUT_SECONDS,
    wire_eval_config,
)

ACCOUNT = "111122223333"   # AWS documentation placeholder
REGION = "us-east-1"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/adt_orchestrator-BkJ8vc7IDF"
)
EVAL_RESOURCE = "AWS::BedrockAgentCore::OnlineEvaluationConfig"
EVALUATOR_RESOURCE = "AWS::BedrockAgentCore::Evaluator"
FUNCTION_RESOURCE = "AWS::Lambda::Function"


def _only_logical_id(template: assertions.Template, resource_type: str) -> str:
    (logical_id,) = template.find_resources(resource_type)
    return logical_id


def _bare_stack() -> cdk.Stack:
    """Minimal stack with the ADTComputeStack construct ids the wiring targets."""
    app = cdk.App()
    stack = cdk.Stack(app, "TestStack",
                      env=cdk.Environment(account=ACCOUNT, region=REGION))
    task_def = ecs.FargateTaskDefinition(stack, "AdtTaskDef")
    task_def.add_container(
        "adt-app",
        image=ecs.ContainerImage.from_registry("test/adt-app:offline"),
        memory_limit_mib=512,
    )
    return stack


def _synth(runtime_arn: str) -> assertions.Template:
    stack = _bare_stack()
    wire_eval_config(stack, orchestrator_runtime_arn=runtime_arn)
    return assertions.Template.from_stack(stack)


def _adt_app_env_raw(template: assertions.Template) -> dict:
    """{Name: Value-json} env map of the adt-app container definition."""
    for resource in template.find_resources("AWS::ECS::TaskDefinition").values():
        for cdef in resource["Properties"].get("ContainerDefinitions", []):
            if cdef.get("Name") == "adt-app":
                return {e["Name"]: e["Value"] for e in cdef.get("Environment", [])}
    pytest.fail("adt-app container definition not found in template")


# --------------------------------------------------------------------------- #
# ARN present → resource + role + env from the resource attr.
# --------------------------------------------------------------------------- #
def test_eval_config_resource_created_with_correct_props():
    template = _synth(RUNTIME_ARN)
    template.resource_count_is(EVAL_RESOURCE, 1)
    template.has_resource_properties(EVAL_RESOURCE, {
        "OnlineEvaluationConfigName": CONFIG_NAME,
        "DataSourceConfig": {
            "CloudWatchLogs": {
                "LogGroupNames": [
                    "/aws/bedrock-agentcore/runtimes/adt_orchestrator-BkJ8vc7IDF-DEFAULT"
                ],
                "ServiceNames": ["adt_orchestrator.DEFAULT"],
            },
        },
        "Rule": {
            "SamplingConfig": {"SamplingPercentage": DEFAULT_SAMPLING_PERCENTAGE},
        },
        "Evaluators": [{"EvaluatorId": e} for e in BUILTIN_EVALUATOR_IDS] + [
            {"EvaluatorId": {"Fn::GetAtt": [_only_logical_id(template, EVALUATOR_RESOURCE),
                                            "EvaluatorId"]}}],
        "ExecutionStatus": "ENABLED",
    })


def test_config_name_matches_cfn_pattern():
    """CFN pins OnlineEvaluationConfigName to ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ —
    a hyphen (the old imperative name) would fail the stack update."""
    import re
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", CONFIG_NAME)


def test_evaluator_ids_are_the_installed_builtin_catalog_names():
    """The installed surface ships NO Builtin.Groundedness/AnswerRelevance —
    the honest trio is Faithfulness/ResponseRelevance/Correctness."""
    assert BUILTIN_EVALUATOR_IDS == (
        "Builtin.Faithfulness",
        "Builtin.ResponseRelevance",
        "Builtin.Correctness",
    )


def test_execution_role_created_with_service_trust():
    template = _synth(RUNTIME_ARN)
    template.has_resource_properties("AWS::IAM::Role", {
        "AssumeRolePolicyDocument": {
            "Statement": [{
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": ACCOUNT,
                        "aws:ResourceAccount": ACCOUNT,
                    },
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}"
                            ":online-evaluation-config/*"
                        ),
                    },
                },
            }],
        },
    })


def test_config_references_execution_role_arn():
    template = _synth(RUNTIME_ARN)
    config = next(iter(template.find_resources(EVAL_RESOURCE).values()))
    role_ref = config["Properties"]["EvaluationExecutionRoleArn"]
    assert "Fn::GetAtt" in role_ref
    assert role_ref["Fn::GetAtt"][1] == "Arn"


# --------------------------------------------------------------------------- #
# The code-based figure check (agents/eval_figure_check.py behind a Lambda).
# --------------------------------------------------------------------------- #
def _execution_role(template: assertions.Template) -> dict:
    config = next(iter(template.find_resources(EVAL_RESOURCE).values()))
    role_id = config["Properties"]["EvaluationExecutionRoleArn"]["Fn::GetAtt"][0]
    return template.find_resources("AWS::IAM::Role")[role_id]


def test_figure_check_evaluator_is_code_based_at_trace_level():
    template = _synth(RUNTIME_ARN)
    template.resource_count_is(EVALUATOR_RESOURCE, 1)
    function_id = _only_logical_id(template, FUNCTION_RESOURCE)
    template.has_resource_properties(EVALUATOR_RESOURCE, {
        "EvaluatorName": FIGURE_CHECK_EVALUATOR_NAME,
        "Level": "TRACE",
        "EvaluatorConfig": {"CodeBased": {"LambdaConfig": {
            "LambdaArn": {"Fn::GetAtt": [function_id, "Arn"]},
            "LambdaTimeoutInSeconds": FIGURE_CHECK_TIMEOUT_SECONDS,
        }}},
    })


def test_figure_check_name_matches_the_cfn_pattern_and_the_lambdas_own():
    """CFN pins EvaluatorName to ^[a-zA-Z][a-zA-Z0-9_]{0,47}$, and the Evals
    page finds the results by the name the Lambda module declares."""
    import re
    import ast
    from pathlib import Path
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", FIGURE_CHECK_EVALUATOR_NAME)
    source = (Path(__file__).resolve().parents[1] / "agentic_digital_thread" / "agents"
              / "eval_figure_check.py").read_text(encoding="utf-8")
    declared = {node.targets[0].id: node.value.value for node in ast.parse(source).body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.targets[0], ast.Name)}
    assert declared["EVALUATOR_NAME"] == FIGURE_CHECK_EVALUATOR_NAME


def test_figure_check_function_is_a_plain_python_asset():
    template = _synth(RUNTIME_ARN)
    template.resource_count_is(FUNCTION_RESOURCE, 1)
    function = next(iter(template.find_resources(FUNCTION_RESOURCE).values()))["Properties"]
    assert function["Runtime"] == "python3.12"
    assert function["Handler"] == "handler.handler"
    assert function["Timeout"] == FIGURE_CHECK_TIMEOUT_SECONDS
    assert "Environment" not in function
    assert "S3Key" in function["Code"]      # a file asset, not a container image
    assert "ImageUri" not in function["Code"]


def test_execution_role_may_invoke_exactly_the_figure_check_function():
    template = _synth(RUNTIME_ARN)
    function_id = _only_logical_id(template, FUNCTION_RESOURCE)
    statements = [s for policy in _execution_role(template)["Properties"]["Policies"]
                  for s in policy["PolicyDocument"]["Statement"]]
    invoke = [s for s in statements if any(str(a).startswith("lambda:")
                                           for a in _as_list(s["Action"]))]
    assert len(invoke) == 1
    (statement,) = invoke
    assert statement["Sid"] == "LambdaInvokeStatement" and statement["Effect"] == "Allow"
    assert sorted(_as_list(statement["Action"])) == ["lambda:GetFunction",
                                                     "lambda:InvokeFunction"]
    assert statement["Resource"] == {"Fn::GetAtt": [function_id, "Arn"]}
    assert set(statement) == {"Sid", "Effect", "Action", "Resource"}


def test_the_invoke_grant_is_inline_on_the_execution_role():
    """No detached AWS::IAM::Policy on the execution role: the config would
    race it (the inline-policy reasoning in _execution_role)."""
    template = _synth(RUNTIME_ARN)
    config = next(iter(template.find_resources(EVAL_RESOURCE).values()))
    role_id = config["Properties"]["EvaluationExecutionRoleArn"]["Fn::GetAtt"][0]
    for policy in template.find_resources("AWS::IAM::Policy").values():
        assert {"Ref": role_id} not in policy["Properties"].get("Roles", [])


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _function_role(template: assertions.Template) -> tuple[str, dict]:
    function = next(iter(template.find_resources(FUNCTION_RESOURCE).values()))
    role_id = function["Properties"]["Role"]["Fn::GetAtt"][0]
    return role_id, template.find_resources("AWS::IAM::Role")[role_id]


def test_the_figure_check_function_has_its_own_role_with_no_managed_policy():
    """CDK's default Lambda role carries AWSLambdaBasicExecutionRole, which
    may create log groups and write to every one in the account."""
    template = _synth(RUNTIME_ARN)
    role_id, role = _function_role(template)
    props = role["Properties"]
    assert "ManagedPolicyArns" not in props
    assert props["AssumeRolePolicyDocument"]["Statement"] == [{
        "Action": "sts:AssumeRole", "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"}}]
    assert role_id != _execution_role_id(template)
    assert "AWSLambdaBasicExecutionRole" not in json.dumps(template.to_json())


def _execution_role_id(template: assertions.Template) -> str:
    config = next(iter(template.find_resources(EVAL_RESOURCE).values()))
    return config["Properties"]["EvaluationExecutionRoleArn"]["Fn::GetAtt"][0]


def test_the_figure_check_role_writes_only_its_own_log_group():
    template = _synth(RUNTIME_ARN)
    role_id, role = _function_role(template)
    (log_group_id,) = template.find_resources("AWS::Logs::LogGroup")
    function = next(iter(template.find_resources(FUNCTION_RESOURCE).values()))
    assert function["Properties"]["LoggingConfig"]["LogGroup"] == {"Ref": log_group_id}
    statements = [s for policy in role["Properties"]["Policies"]
                  for s in policy["PolicyDocument"]["Statement"]]
    assert statements == [{
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Effect": "Allow",
        "Resource": {"Fn::GetAtt": [log_group_id, "Arn"]}}]
    for policy in template.find_resources("AWS::IAM::Policy").values():
        assert {"Ref": role_id} not in policy["Properties"].get("Roles", [])


def test_no_figure_check_without_the_runtime_arn():
    template = _synth("")
    template.resource_count_is(EVALUATOR_RESOURCE, 0)
    template.resource_count_is(FUNCTION_RESOURCE, 0)


def test_env_sourced_from_resource_id_attr():
    """EVAL_CONFIG_ORCHESTRATOR comes from Fn::GetAtt on the config resource
    (no SSM round-trip for the id)."""
    template = _synth(RUNTIME_ARN)
    env = _adt_app_env_raw(template)
    value = env.get("EVAL_CONFIG_ORCHESTRATOR")
    assert value is not None, "EVAL_CONFIG_ORCHESTRATOR env missing"
    assert "Fn::GetAtt" in value
    (logical_id, attr) = value["Fn::GetAtt"]
    assert attr == "OnlineEvaluationConfigId"
    assert logical_id in template.find_resources(EVAL_RESOURCE)


# --------------------------------------------------------------------------- #
# ARN empty → nothing added, byte-identical template.
# --------------------------------------------------------------------------- #
def test_no_resource_or_env_when_arn_empty():
    template = _synth("")
    baseline = assertions.Template.from_stack(_bare_stack())
    template.resource_count_is(EVAL_RESOURCE, 0)
    # No EXTRA role beyond the task def's own task role.
    assert len(template.find_resources("AWS::IAM::Role")) == \
        len(baseline.find_resources("AWS::IAM::Role"))
    assert "EVAL_CONFIG_ORCHESTRATOR" not in _adt_app_env_raw(template)


def test_empty_arn_template_identical_to_unwired():
    """Fail-soft read (empty ARN) ⇒ byte-identical template (no drift for base)."""
    wired = _synth("").to_json()
    unwired = assertions.Template.from_stack(_bare_stack()).to_json()
    assert wired == unwired


# --------------------------------------------------------------------------- #
# Loud failures.
# --------------------------------------------------------------------------- #
def test_malformed_arn_fails_synth_loudly():
    with pytest.raises(ValueError, match="not an AgentCore runtime ARN"):
        wire_eval_config(_bare_stack(), orchestrator_runtime_arn="garbage")


def test_runtime_id_without_short_id_suffix_fails():
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/nosuffix"
    with pytest.raises(ValueError, match="short-id"):
        wire_eval_config(_bare_stack(), orchestrator_runtime_arn=arn)


def test_missing_task_def_raises_loudly():
    """An id rename in ADTComputeStack must fail synth, not silently drop env."""
    app = cdk.App()
    stack = cdk.Stack(app, "TestStack",
                      env=cdk.Environment(account=ACCOUNT, region=REGION))
    with pytest.raises(Exception):
        wire_eval_config(stack, orchestrator_runtime_arn=RUNTIME_ARN)
