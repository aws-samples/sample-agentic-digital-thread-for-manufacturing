# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK-native managed AgentCore evaluations for ADTComputeStack.

The orchestrator's online evaluation config is a real CloudFormation resource
(``AWS::BedrockAgentCore::OnlineEvaluationConfig`` via
``aws_bedrockagentcore.CfnOnlineEvaluationConfig``) owned by this stack — it
replaced the imperative deploy.sh ensure step (the former
``scripts/_evaluations_lib.py`` candidate-table CLI). deploy.sh section
``[2c-eval]`` now only PERSISTS the orchestrator runtime ARN to SSM
``/adt/orchestrator-runtime-arn`` once the runtime is READY;
app.py resolves it at synth (sticky pattern: explicit
``-c orchestrator_runtime_arn`` > SSM > "" — fail-soft, offline synth and
missing credentials resolve to "") and passes it here.

When the ARN is non-empty this module creates, on ADTComputeStack:

* an **evaluation execution role** (trust ``bedrock-agentcore.amazonaws.com``,
  bounded to this account's ``online-evaluation-config/*``), carrying the
  service-documented permissions: Logs Insights reads over the trace source,
  result writes under ``/aws/bedrock-agentcore/evaluations/*``, the
  ``aws/spans`` index-policy configuration, Bedrock model invocation for
  the LLM-as-judge evaluators, and ``lambda:InvokeFunction`` /
  ``lambda:GetFunction`` on exactly the figure-check function below;
* the **figure check**: a code-based evaluator
  (``AWS::BedrockAgentCore::Evaluator`` ``adt_figure_check``, level
  ``TRACE``) backed by a Python Lambda that runs
  ``agents/eval_figure_check.py``: it checks each total or count the
  orchestrator's final answer states against what that turn's tools
  returned, with the app's own figure reader, no model call. The Lambda
  runs under its own role, with no managed policy, that may only create
  streams in and write to its own log group. The Lambda
  asset is staged at synth (``stacks/_staging.py``): the handler in
  ``lambda-figure-check/`` plus copies of ``FIGURE_CHECK_MODULES``, the
  check's import closure, all standard library, so nothing is installed or
  built at deploy time (the workshop forbids it);
* the ``adt_orchestrator_eval`` **CfnOnlineEvaluationConfig** (CFN name
  pattern ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$`` — hyphens are invalid, hence the
  underscore name) sampling the orchestrator runtime's observability log
  group at ``DEFAULT_SAMPLING_PERCENTAGE`` with the three built-in judges
  and the figure check, so its scores land in the same results log group
  and ``Bedrock-AgentCore/Evaluations`` metrics;
* the ``EVAL_CONFIG_ORCHESTRATOR`` task env on the adt-app container, taken
  DIRECTLY from the resource's ``attr_online_evaluation_config_id`` (no SSM
  round-trip for the id) — the exact env
  ``agents/agents_routes.py::EVAL_CONFIG_MAP`` reads for the Agents-tab
  evaluation scores (``_get_eval_scores``).

When the ARN is empty (first deploy, offline synth) NOTHING is added — the
template stays byte-identical, and the reader's empty-string default keeps
scores off.

THE EVALUATOR IS LOCKED WHILE THE CONFIG IS ENABLED. AgentCore locks a
code-based evaluator an enabled online config references: it cannot be
modified or deleted until the config is disabled or deleted. The Lambda's
code can change freely on any deploy (the evaluator holds only its ARN), but
a change to the evaluator resource itself (its name, which also forces a
replacement, its level, its Lambda ARN or timeout) needs the config disabled
first (``ExecutionStatus`` ``DISABLED`` on one deploy, the change on the next).

BUILT-IN EVALUATOR IDS — honest mapping. The deployed CloudFormation/CDK
surface (verified against the installed ``aws_cdk.aws_bedrockagentcore``
``BuiltinEvaluator`` catalog) has NO ``Builtin.Groundedness`` or
``Builtin.AnswerRelevance``. The grounding-focused trio the plan asked for
maps onto what the service actually ships: ``Builtin.Faithfulness`` (response
grounded in retrieved context — the groundedness metric),
``Builtin.ResponseRelevance`` (the answer-relevance metric), and
``Builtin.Correctness`` (factual accuracy). The reader strips the
``Builtin.`` prefix generically (``parse_eval_rows``), so these names flow
through end-to-end with no reader change.

FIRST-DEPLOY CHICKEN-AND-EGG (narrowed): deploy #1 synthesizes before
``[2c-eval]`` persists the runtime ARN, so neither the config nor the env
exists yet; on deploy #2's stack update BOTH land together (the env references
the resource, so they cannot drift apart). Teardown is now the ordinary stack
delete — no imperative cleanup.

No reader-side IAM is added here: the app task role already carries the Logs
Insights actions (see the CloudWatch Logs statement in
``adt_compute_stack.py``), which covers the
``/aws/bedrock-agentcore/evaluations/*`` result log groups.

Like ``stacks/_observability.py``, this is an ADDITIVE post-construction
module (applied from app.py after the stack is built) — the feature stays a
self-contained, removable seam.
"""
from __future__ import annotations

import os

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_bedrockagentcore as bac
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs

from stacks._checkov_skips import checkov_skip
from stacks._staging import stage_tree

#: CFN OnlineEvaluationConfigName (pattern forbids hyphens; underscores only).
CONFIG_NAME = "adt_orchestrator_eval"

#: Built-in evaluator ids — mirrors the installed ``BuiltinEvaluator`` catalog
#: (module docstring documents the Groundedness/AnswerRelevance mapping).
BUILTIN_EVALUATOR_IDS = (
    "Builtin.Faithfulness",
    "Builtin.ResponseRelevance",
    "Builtin.Correctness",
)

#: Conservative default trace-sampling percentage (evals bill per evaluated
#: trace; same conservatism as the observability indexing default).
DEFAULT_SAMPLING_PERCENTAGE = 10

#: The code-based figure check (CFN EvaluatorName pattern
#: ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$``); ``agents/eval_figure_check.py``
#: declares the same name, which the Evals page reads results by.
FIGURE_CHECK_EVALUATOR_NAME = "adt_figure_check"
#: Both the evaluator's Lambda timeout and the function's own (1-300 s).
FIGURE_CHECK_TIMEOUT_SECONDS = 60

_REPO = os.path.join(os.path.dirname(__file__), "..")
FIGURE_CHECK_LAMBDA_DIR = os.path.join(_REPO, "lambda-figure-check")
_AGENTS_DIR = os.path.join(_REPO, "agentic_digital_thread", "agents")
#: The app modules the check imports, staged as ``agents/<module>.py``: its
#: whole import closure, standard library only (tests/test_figure_check_lambda.py).
#: ``__init__`` installs the lease write check (``lease_guard``, standard
#: library only), so the package cannot be imported without it.
FIGURE_CHECK_MODULES = (
    "__init__",
    "lease_guard",
    "eval_figure_check",
    "answer_figures",
    "answer_provenance",
    "athena_exact_shape",
    "athena_scope",
    "response_integrity_query",
    "tool_result_capture",
)

_NO_RESERVED_CONCURRENCY = (
    "CKV_AWS_115",
    "No reserved concurrency: the evaluation service invokes the figure check once "
    "per sampled trace, and fresh accounts can have a concurrency quota too low to "
    "reserve any, so a reservation could fail the stack's creation.",
)
_NO_DLQ = (
    "CKV_AWS_116",
    "AgentCore Evaluations invokes the figure check synchronously and records a "
    "failed invocation as an evaluation error; there is no asynchronous event for a "
    "dead-letter queue to receive.",
)
_NO_VPC = (
    "CKV_AWS_117",
    "The figure check reads only the payload it is invoked with and reaches no "
    "network resource, in a VPC or outside one.",
)


def _parse_runtime_arn(runtime_arn: str) -> tuple[str, str]:
    """(runtime_id, runtime_name) from an AgentCore runtime ARN.

    ``arn:aws:bedrock-agentcore:<region>:<acct>:runtime/adt_orchestrator-XYZ``
    → id ``adt_orchestrator-XYZ``, name ``adt_orchestrator`` (runtime names
    match ``[a-zA-Z][a-zA-Z0-9_]*`` — no hyphens — so the trailing short-id
    split on the LAST hyphen is unambiguous). A malformed value fails synth
    loudly (no fallback, per project steering) rather than provisioning an
    evaluation over a nonexistent log group.
    """
    resource = runtime_arn.rsplit(":", 1)[-1]
    if not resource.startswith("runtime/"):
        raise ValueError(
            f"not an AgentCore runtime ARN: {runtime_arn!r} — expected "
            "arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id> "
            "(check -c orchestrator_runtime_arn / SSM "
            "/adt/orchestrator-runtime-arn)"
        )
    runtime_id = resource.split("/", 1)[1]
    if "-" not in runtime_id:
        raise ValueError(
            f"AgentCore runtime id {runtime_id!r} has no <name>-<short-id> "
            f"suffix (from ARN {runtime_arn!r})"
        )
    return runtime_id, runtime_id.rsplit("-", 1)[0]


def _execution_policy_statements(stack) -> list[iam.PolicyStatement]:
    """The service-documented execution-role permissions (least-privilege
    where the actions support resource scoping)."""
    logs_arn = f"arn:aws:logs:{stack.region}:{stack.account}:log-group"
    return [
        iam.PolicyStatement(
            sid="CloudWatchLogReadStatement",
            # DescribeLogGroups/GetQueryResults have no resource-level scoping;
            # StartQuery targets the source trace log group resolved at run
            # time (the service-documented execution-role policy grants these
            # on "*").
            actions=["logs:DescribeLogGroups", "logs:GetQueryResults", "logs:StartQuery"],
            resources=["*"],
        ),
        iam.PolicyStatement(
            sid="CloudWatchLogWriteStatement",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"{logs_arn}:/aws/bedrock-agentcore/evaluations/*"],
        ),
        iam.PolicyStatement(
            sid="CloudWatchIndexPolicyStatement",
            # aws/spans is Transaction Search's fixed span destination.
            actions=["logs:DescribeIndexPolicies", "logs:PutIndexPolicy"],
            resources=[f"{logs_arn}:aws/spans", f"{logs_arn}:aws/spans:*"],
        ),
        iam.PolicyStatement(
            sid="BedrockInvokeStatement",
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/*",
                f"arn:aws:bedrock:{stack.region}:{stack.account}:inference-profile/*",
            ],
        ),
    ]


def _execution_role(stack, runtime_id: str, function_arn: str) -> iam.Role:
    """The service-assumed evaluation execution role (per the AgentCore
    Evaluations prerequisites doc), source-bounded to this account's
    online-evaluation-configs."""
    # Permissions are INLINE in the Role resource (not add_to_policy, which
    # synthesizes a SEPARATE DefaultPolicy resource): the eval config only
    # depends on the role ARN, so CloudFormation raced config creation against
    # the detached policy and AgentCore validated a permissionless role —
    # "The provided execution role does not have permissions to access the
    # specified log groups" (live failure, 2026-07-22). Inline = atomic. The
    # figure check's invoke grant rides the same policy for the same reason.
    return iam.Role(
        stack,
        "OrchestratorEvalExecutionRole",
        assumed_by=iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com",
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": stack.account,
                    "aws:ResourceAccount": stack.account,
                },
                "ArnLike": {
                    "aws:SourceArn": (
                        f"arn:aws:bedrock-agentcore:{stack.region}:"
                        f"{stack.account}:online-evaluation-config/*"
                    ),
                },
            },
        ),
        inline_policies={
            "EvalExecution": iam.PolicyDocument(statements=[
                *_execution_policy_statements(stack), _invoke_statement(function_arn)]),
        },
        description=(
            "AgentCore Evaluations execution role for the adt_orchestrator "
            "online evaluation config (reads traces, writes results, invokes "
            "judge models and the figure check)."
        ),
    )


def _invoke_statement(function_arn: str) -> iam.PolicyStatement:
    """The code-based evaluator grant the AgentCore doc gives, on exactly the
    figure-check function."""
    return iam.PolicyStatement(
        sid="LambdaInvokeStatement",
        actions=["lambda:InvokeFunction", "lambda:GetFunction"],
        resources=[function_arn],
    )


def stage_figure_check(scope) -> str:
    """The figure check's Lambda asset, staged under ``cdk.out``: the handler
    plus ``agents/<module>.py`` for each of ``FIGURE_CHECK_MODULES``. A listed
    module that is gone fails synth (the asset would fail every invocation)."""
    sources = {f"agents/{module}.py": os.path.join(_AGENTS_DIR, f"{module}.py")
               for module in FIGURE_CHECK_MODULES}
    missing = [path for path in sources.values() if not os.path.isfile(path)]
    if missing:
        raise FileNotFoundError(f"figure check modules missing: {missing}")
    return stage_tree(scope, "figure-check", FIGURE_CHECK_LAMBDA_DIR, extra_files=sources)


def _figure_check_function(stack) -> lambda_.Function:
    """The figure check's Lambda: pure Python, no environment, no network.

    Its own role, with no managed policy: CDK's default Lambda role carries
    ``AWSLambdaBasicExecutionRole``, which may create log groups and write to
    every one in the account, and this code reads untrusted span text. The
    role may only create streams in and write to this function's own log
    group, created here (so ``logs:CreateLogGroup`` is not needed)."""
    log_group = logs.LogGroup(stack, "FigureCheckEvaluatorLogs",
                              retention=logs.RetentionDays.ONE_MONTH,
                              removal_policy=RemovalPolicy.DESTROY)
    role = iam.Role(
        stack,
        "FigureCheckEvaluatorRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        inline_policies={"OwnLogGroup": iam.PolicyDocument(statements=[iam.PolicyStatement(
            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[log_group.log_group_arn])])},
        description="The AgentCore figure-check evaluator's Lambda: writes its own log group only",
    )
    function = lambda_.Function(
        stack,
        "FigureCheckEvaluatorFunction",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="handler.handler",
        code=lambda_.Code.from_asset(stage_figure_check(stack),
                                     exclude=["__pycache__", "*.pyc"]),
        timeout=Duration.seconds(FIGURE_CHECK_TIMEOUT_SECONDS),
        memory_size=512,
        role=role,
        log_group=log_group,
        description=("ADT - AgentCore code-based evaluator: the orchestrator's stated "
                     "totals against what its tools returned"),
    )
    checkov_skip(function, [_NO_RESERVED_CONCURRENCY, _NO_DLQ, _NO_VPC])
    return function


def _figure_check_evaluator(stack, function: lambda_.Function) -> bac.CfnEvaluator:
    """The code-based evaluator over *function*, one orchestrator turn (trace)
    at a time. Locked while the config is enabled (module docstring)."""
    return bac.CfnEvaluator(
        stack,
        "FigureCheckEvaluator",
        evaluator_name=FIGURE_CHECK_EVALUATOR_NAME,
        level="TRACE",
        description=("The orchestrator's stated totals and counts against what its "
                     "tools returned that turn (code, no model call)"),
        evaluator_config=bac.CfnEvaluator.EvaluatorConfigProperty(
            code_based=bac.CfnEvaluator.CodeBasedEvaluatorConfigProperty(
                lambda_config=bac.CfnEvaluator.LambdaEvaluatorConfigProperty(
                    lambda_arn=function.function_arn,
                    lambda_timeout_in_seconds=FIGURE_CHECK_TIMEOUT_SECONDS,
                ),
            ),
        ),
    )


def _online_evaluation_config(
    stack, runtime_id: str, runtime_name: str, role: iam.Role,
    figure_check: bac.CfnEvaluator,
) -> bac.CfnOnlineEvaluationConfig:
    """The orchestrator's CfnOnlineEvaluationConfig (props verified against
    the installed ``aws_bedrockagentcore`` module + the CFN reference)."""
    return bac.CfnOnlineEvaluationConfig(
        stack,
        "OrchestratorEvalConfig",
        online_evaluation_config_name=CONFIG_NAME,
        description="Continuous evaluation of the ADT orchestrator agent",
        evaluation_execution_role_arn=role.role_arn,
        data_source_config=bac.CfnOnlineEvaluationConfig.DataSourceConfigProperty(
            cloud_watch_logs=bac.CfnOnlineEvaluationConfig.CloudWatchLogsInputConfigProperty(
                # The runtime's observability trace log group + OTEL service
                # name — the same shapes the fleet reader derives
                # (agents/agents_routes.py::_get_metrics, and the console's
                # documented "<runtime-name>.<endpoint-name>" service format).
                log_group_names=[f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"],
                service_names=[f"{runtime_name}.DEFAULT"],
            ),
        ),
        rule=bac.CfnOnlineEvaluationConfig.RuleProperty(
            sampling_config=bac.CfnOnlineEvaluationConfig.SamplingConfigProperty(
                sampling_percentage=DEFAULT_SAMPLING_PERCENTAGE,
            ),
        ),
        evaluators=[
            bac.CfnOnlineEvaluationConfig.EvaluatorReferenceProperty(evaluator_id=e)
            for e in (*BUILTIN_EVALUATOR_IDS, figure_check.attr_evaluator_id)
        ],
        execution_status="ENABLED",
    )


def wire_eval_config(stack, *, orchestrator_runtime_arn: str) -> None:
    """Create the orchestrator online-evaluation resources + reader env.

    When *orchestrator_runtime_arn* is empty (SSM parameter absent, offline
    synth, or a failed fail-soft read) NOTHING is added — the synthesized
    template stays byte-identical to a pre-evaluations deploy.

    The constructs are located through the constructs tree by their fixed ids
    ("AdtTaskDef" in ``_create_ecs_service``); a missing child raises loudly
    (no fallback, per project steering) so an id rename cannot silently drop
    the wiring.
    """
    if not orchestrator_runtime_arn:
        return
    runtime_id, runtime_name = _parse_runtime_arn(orchestrator_runtime_arn)
    function = _figure_check_function(stack)
    figure_check = _figure_check_evaluator(stack, function)
    role = _execution_role(stack, runtime_id, function.function_arn)
    config = _online_evaluation_config(stack, runtime_id, runtime_name, role, figure_check)

    task_def = stack.node.find_child("AdtTaskDef")
    container = task_def.default_container
    if container is None:  # pragma: no cover - single-container task def
        container = task_def.find_container("adt-app")
    container.add_environment(
        "EVAL_CONFIG_ORCHESTRATOR", config.attr_online_evaluation_config_id
    )
