# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression test for the ADT app task-def env wiring (P5 / P4-A fix pass).

Cloud mode fails CLOSED without two env vars, so the CDK task-def MUST inject both
into the ``adt-app`` container (rather than deploy.sh, since both values are known at
synth):

* ``ADT_APP_SERVICE_CLIENT_ID`` — the adt-app-service (client_credentials) client id
  (ADTAgentCoreStack ``AppServiceClientId`` output), used by the orchestrator to mint
  its background/service bearer token for the CUSTOM_JWT query gateway.
* ``ONBOARDING_GATEWAY_URL`` — the onboarding CfnGateway URL (ADTOnboardingStack
  ``OnboardingGatewayUrl`` output), used by ``onboard_chat``'s gateway MCP client.

Synthesized once offline (no AWS calls); requires the vendored CDK CLI + Node under
node_modules — skips if unavailable (mirrors tests/test_query_gateway_cutover.py).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest
from _role_policies import by_name

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
STACK = "ADTComputeStack"


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


def _offline_docker_stub(out_dir: Path) -> Path:
    """Return a Docker command that emits only the bundling placeholder CDK needs."""
    docker_stub = out_dir / "offline-docker"
    docker_stub.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    *:/asset-output:*) out=${arg%%:/asset-output:*}; "
        "mkdir -p \"$out\"; : > \"$out/.synth-placeholder\" ;;\n"
        "  esac\n"
        "done\n"
    )
    docker_stub.chmod(0o755)
    return docker_stub


#: One synth per (mode, context) per session: several tests read the same
#: template, and each synth is minutes of CPU and GBs of memory.
_SYNTH_CACHE: dict[tuple[str, tuple[str, ...]], dict] = {}


def _synth_compute_template(tmp_path_factory, mode: str, context: tuple[str, ...],
                            env_extra: tuple[tuple[str, str], ...] = ()) -> dict:
    key = (mode, tuple(context), tuple(env_extra))
    if key not in _SYNTH_CACHE:
        _SYNTH_CACHE[key] = _synth_compute_template_uncached(
            tmp_path_factory, mode, context, dict(env_extra))
    return _SYNTH_CACHE[key]


def _synth_compute_template_uncached(tmp_path_factory, mode: str, context: tuple[str, ...],
                                     env_extra: dict[str, str] | None = None) -> dict:
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")

    out_dir = tmp_path_factory.mktemp(f"cdk_synth_compute_env_{mode}")
    # Synth only needs asset fingerprints to render the task definition. Avoid
    # registry/network access for optional data-mode Lambda layer bundling while
    # still requiring CDK to construct and emit the real CloudFormation template.
    docker_stub = _offline_docker_stub(out_dir)
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CDK_DOCKER": str(docker_stub),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
        **(env_extra or {}),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [
            cdk_bin, "synth", STACK,
            "-c", "region=us-east-1",
            *context,
            "--output", str(out_dir),
            "--quiet",
        ],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=600,
    )
    template_path = out_dir / f"{STACK}.template.json"
    if result.returncode != 0 or not template_path.exists():
        pytest.fail(f"synth did not produce a template:\n{result.stderr[-4000:]}")
    return json.loads(template_path.read_text())


@pytest.fixture(scope="session")
def compute_template(tmp_path_factory) -> dict:
    return _synth_compute_template(tmp_path_factory, "base", ())


def test_model_allowlist_rides_the_app_env_only_when_set(tmp_path_factory, compute_template):
    """-c allowed_models sets ALLOWED_MODELS on the adt-app container (sorted
    catalog families); without the context the key is absent, so a default
    deployment's task definition is unchanged (stacks/_model_allowlist.py)."""
    assert "ALLOWED_MODELS" not in _adt_app_env(compute_template)
    # The allowlist must cover the deployment's own configured models, or the
    # synth refuses it (stacks/_model_allowlist.py): a pair pinned with
    # -c bedrock_model / -c system_agent_model.
    context = ("-c", "allowed_models=anthropic.claude-sonnet-5,"
                     "us.anthropic.claude-haiku-4-5-20251001-v1:0",
               "-c", "bedrock_model=global.anthropic.claude-sonnet-5",
               "-c", "system_agent_model=us.anthropic.claude-haiku-4-5-20251001-v1:0")
    template = _synth_compute_template(tmp_path_factory, "allowlist", context)
    env = _adt_app_env(template)
    assert env["ALLOWED_MODELS"] == (
        "anthropic.claude-haiku-4-5-20251001-v1:0,anthropic.claude-sonnet-5")
    assert env["BEDROCK_MODEL"] == "global.anthropic.claude-sonnet-5"
    assert env["SYSTEM_AGENT_MODEL"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    outputs = template["Outputs"]
    assert outputs["AllowedModels"]["Value"] == env["ALLOWED_MODELS"]
    assert outputs["SystemAgentModel"]["Value"] == env["SYSTEM_AGENT_MODEL"]


def test_base_deployment_runs_the_catalog_defaults(compute_template):
    """Without model context the app runs the catalog defaults (Claude Sonnet
    5.5 from cdk.json, Claude Haiku 5.5 from the runtime-env contract), and
    the template carries no allowlist output for the workshop CR to read."""
    env = _adt_app_env(compute_template)
    assert env["BEDROCK_MODEL"] == "global.anthropic.claude-sonnet-5-5"
    assert env["SYSTEM_AGENT_MODEL"] == "global.anthropic.claude-haiku-5-5"
    outputs = compute_template["Outputs"]
    assert outputs["SystemAgentModel"]["Value"] == "global.anthropic.claude-haiku-5-5"
    assert "AllowedModels" not in outputs


_DEPLOYED_MODE_CONTEXTS = (
    pytest.param("base", (), id="base"),
    pytest.param("seed", ("-c", "with_seed=true"), id="seed"),
    pytest.param("simulator", ("-c", "with_simulator=true"), id="simulator"),
    pytest.param("workshop", ("-c", "with_workshop=true", "-c", "with_simulator=true"), id="workshop"),
    pytest.param("highbyte", ("-c", "highbyte_enabled=true", "-c", "highbyte_accept_eula=true"),
                 id="highbyte"),
)


def _adt_app_env_names(template: dict) -> set[str]:
    """Collect the env var Names on the adt-app container definition."""
    return set(_adt_app_env(template))


def _adt_app_env(template: dict) -> dict[str, object]:
    """Map env var Name -> Value on the adt-app container definition."""
    env: dict[str, object] = {}
    for r in template.get("Resources", {}).values():
        if r.get("Type") != "AWS::ECS::TaskDefinition":
            continue
        for cdef in r["Properties"].get("ContainerDefinitions", []):
            if cdef.get("Name") != "adt-app":
                continue
            for entry in cdef.get("Environment", []):
                if "Name" in entry:
                    env[entry["Name"]] = entry.get("Value")
    return env


_STORY_ENV_CASES = (
    pytest.param("base", (), None, id="base"),
    pytest.param("seed", ("-c", "with_seed=true"), None, id="seed"),
    pytest.param("simulator", ("-c", "with_simulator=true"), None, id="simulator"),
    pytest.param("workshop", ("-c", "with_workshop=true", "-c", "with_simulator=true"), "frozen", id="workshop-default"),
    pytest.param(
        "workshop_live",
        ("-c", "with_workshop=true", "-c", "with_simulator=true", "-c", "workshop_story=live"),
        "live",
        id="workshop-live",
    ),
)


@pytest.mark.parametrize(("mode", "context", "expected"), _STORY_ENV_CASES)
def test_workshop_story_env_follows_context(tmp_path_factory, mode, context, expected):
    """ADT_WORKSHOP_STORY rides ONLY the workshop env and carries the context story.

    MEMORY_LTM_SCOPE=session rides the same workshop-only env for BOTH stories
    (one shared login ⇒ one actorId; actor-wide LTM retrieval leaked other
    attendees' chats — workshop R2b defect C). Asserted here to reuse the synths.
    """
    env = _adt_app_env(_synth_compute_template(tmp_path_factory, mode, context))
    if expected is None:
        assert "ADT_WORKSHOP_STORY" not in env, sorted(env)
        assert "MEMORY_LTM_SCOPE" not in env, sorted(env)
    else:
        assert env.get("ADT_WORKSHOP_STORY") == expected
        assert env.get("MEMORY_LTM_SCOPE") == "session"


STORY_CLOCK_POLICY = "StoryClockLambda"
STORY_HISTORY_ENV = "STORY_HISTORY_FUNCTION_NAME"
STORY_HISTORY_FUNCTION = "erp-story-history"


def _task_role_inline_policies(template: dict) -> dict[str, list[dict]]:
    """Policy name -> statements of the adt-app task role's policies (the
    former inline names, which the moved managed-policy statements keep)."""
    task_role = None
    for r in template.get("Resources", {}).values():
        if r.get("Type") == "AWS::ECS::TaskDefinition":
            task_role = r["Properties"]["TaskRoleArn"]["Fn::GetAtt"][0]
    return by_name(template, task_role)


def _render(value) -> str:
    """Flatten an Fn::Join / Ref into a comparable string (${Ref} for pseudo params)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        sep, parts = value["Fn::Join"]
        return sep.join(_render(p) for p in parts)
    if isinstance(value, dict) and "Ref" in value:
        return "${" + value["Ref"] + "}"
    return json.dumps(value, sort_keys=True)


@pytest.mark.parametrize(("mode", "context", "story"), _STORY_ENV_CASES)
def test_story_clock_invoke_grant_and_env_only_under_live(tmp_path_factory, mode, context, story):
    """The placement POST (Task M) invokes erp-story-history: the task role
    gets lambda:InvokeFunction on exactly that function and the app env names
    it, under the live story ONLY (engine / demo / frozen carry neither)."""
    template = _synth_compute_template(tmp_path_factory, mode, context)
    env = _adt_app_env(template)
    policies = _task_role_inline_policies(template)
    if story != "live":
        assert STORY_HISTORY_ENV not in env, sorted(env)
        assert STORY_CLOCK_POLICY not in policies, sorted(policies)
        assert f"function:{STORY_HISTORY_FUNCTION}" not in json.dumps(template)
        return
    assert env[STORY_HISTORY_ENV] == STORY_HISTORY_FUNCTION
    (statement,) = policies[STORY_CLOCK_POLICY]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "lambda:InvokeFunction"
    # Region/account render as literals or pseudo-parameter Refs depending on
    # the synth environment (ambient credentials resolve the account).
    assert re.fullmatch(
        r"arn:\$\{AWS::Partition\}:lambda:(us-east-1|\$\{AWS::Region\}):"
        r"(\d{12}|\$\{AWS::AccountId\}):function:" + re.escape(STORY_HISTORY_FUNCTION),
        _render(statement["Resource"]),
    ), statement["Resource"]


def test_story_clock_function_name_matches_the_erp_stack():
    """The compute stack names the function by the SAME constant the ERP
    stack creates it with (no cross-stack export: live -> frozen must drop it)."""
    pytest.importorskip("aws_cdk")
    from stacks import erp_lambda_stack

    assert erp_lambda_stack.STORY_HISTORY_FUNCTION == STORY_HISTORY_FUNCTION


STORY_LOOP_ENV = "STORY_LOOP_SECONDS"
AUTO_PLACE_ENV = "STORY_AUTO_PLACE_MINUTES"


@pytest.mark.parametrize(("mode", "context", "story"), _STORY_ENV_CASES)
def test_no_loop_env_without_the_loop_context(tmp_path_factory, mode, context, story):
    """Without ``-c story_loop_seconds`` no synth carries the loop env
    (reuses the cached synths above)."""
    assert STORY_LOOP_ENV not in _adt_app_env(_synth_compute_template(tmp_path_factory, mode, context))


class _Node:
    def __init__(self, **ctx):
        self._ctx = ctx

    def try_get_context(self, key):
        return self._ctx.get(key)


_LIVE_CTX = {"with_workshop": "true", "with_simulator": "true", "workshop_story": "live"}


@pytest.mark.parametrize(("live", "ctx", "expected"), [
    (False, {}, {}),
    (False, {"with_workshop": "true", "with_simulator": "true"}, {}),
    (True, _LIVE_CTX, {STORY_HISTORY_ENV: STORY_HISTORY_FUNCTION}),
    (True, {**_LIVE_CTX, "story_loop_seconds": "28800"}, {STORY_HISTORY_ENV: STORY_HISTORY_FUNCTION}),
    (True, {**_LIVE_CTX, "story_loop_seconds": "10800"},
     {STORY_HISTORY_ENV: STORY_HISTORY_FUNCTION, STORY_LOOP_ENV: "10800"}),
    # ADR-0091 Design C: the auto-place policy rides the compute task only; 0 = off.
    (True, {**_LIVE_CTX, "story_auto_place_minutes": "0"}, {STORY_HISTORY_ENV: STORY_HISTORY_FUNCTION}),
    (True, {**_LIVE_CTX, "story_auto_place_minutes": "80"},
     {STORY_HISTORY_ENV: STORY_HISTORY_FUNCTION, AUTO_PLACE_ENV: "80"}),
    (True, {**_LIVE_CTX, "story_loop_seconds": "10800", "story_auto_place_minutes": "180"},
     {STORY_HISTORY_ENV: STORY_HISTORY_FUNCTION, STORY_LOOP_ENV: "10800", AUTO_PLACE_ENV: "180"}),
])
def test_env_story_clock_both_ways(live, ctx, expected):
    """Synth-free pin: ``_env_story_clock`` is today's dict exactly (keys and
    order) without the loop context; set, it adds STORY_LOOP_SECONDS only."""
    pytest.importorskip("aws_cdk")
    from types import SimpleNamespace

    from stacks.adt_compute_stack import ADTComputeStack

    fake = SimpleNamespace(node=_Node(**ctx), _is_live_story=lambda: live)
    assert list(ADTComputeStack._env_story_clock(fake).items()) == list(expected.items())


def test_env_story_clock_refuses_a_loop_off_live():
    pytest.importorskip("aws_cdk")
    from types import SimpleNamespace

    from stacks.adt_compute_stack import ADTComputeStack

    fake = SimpleNamespace(node=_Node(with_workshop="true", story_loop_seconds="10800"),
                           _is_live_story=lambda: False)
    with pytest.raises(ValueError, match="story_loop_seconds requires workshop_story=live"):
        ADTComputeStack._env_story_clock(fake)


def test_env_story_clock_refuses_an_auto_place_off_live():
    pytest.importorskip("aws_cdk")
    from types import SimpleNamespace

    from stacks.adt_compute_stack import ADTComputeStack

    fake = SimpleNamespace(node=_Node(with_workshop="true", story_auto_place_minutes="80"),
                           _is_live_story=lambda: False)
    with pytest.raises(ValueError, match="story_auto_place_minutes requires workshop_story=live"):
        ADTComputeStack._env_story_clock(fake)


@pytest.mark.parametrize(("mode", "context", "story"), _STORY_ENV_CASES)
def test_no_auto_place_env_without_the_context(tmp_path_factory, mode, context, story):
    """ADR-0091 Design C: without ``-c story_auto_place_minutes`` no synth
    carries the policy env (reuses the cached synths above)."""
    assert AUTO_PLACE_ENV not in _adt_app_env(_synth_compute_template(tmp_path_factory, mode, context))


def test_task_def_injects_service_client_id(compute_template):
    names = _adt_app_env_names(compute_template)
    assert "ADT_APP_SERVICE_CLIENT_ID" in names, sorted(names)


def test_task_def_injects_onboarding_gateway_url(compute_template):
    names = _adt_app_env_names(compute_template)
    assert "ONBOARDING_GATEWAY_URL" in names, sorted(names)


def test_the_app_container_knows_its_own_account(compute_template):
    """connection_resolver refuses a full-ARN secret read while the deployment
    account is unknown; the task gets it from the stack, so it never depends
    on an STS call (the env value is the stack's own account)."""
    env = _adt_app_env(compute_template)
    assert "AWS_ACCOUNT_ID" in env, sorted(env)
    value = env["AWS_ACCOUNT_ID"]
    assert value == {"Ref": "AWS::AccountId"} or (
        isinstance(value, str) and len(value) == 12 and value.isdigit()), value


def test_base_mode_omits_adt_demo(compute_template):
    """Base mode (default synth, no --seed/--simulator → self.seed_db is None) must
    NOT set ADT_DEMO on the adt-app container, so agents.demo.is_enabled() is False
    and the app ships no demo content (ADR-0030 base-default + ADR-0019 seam)."""
    names = _adt_app_env_names(compute_template)
    assert "ADT_DEMO" not in names, sorted(names)


@pytest.mark.parametrize(("mode", "context"), _DEPLOYED_MODE_CONTEXTS)
def test_the_seed_function_name_rides_only_deploys_that_have_it(tmp_path_factory, mode, context):
    """ADT_SEED_LAMBDA names the sample-database loader, which exists only with
    ADTSeedDataStack (--seed, implied by --simulator and --workshop). A base or
    HighByte-only deploy carries no name, so Reset All skips the reseed there
    (agents/onboard.py onboard_reseed) instead of invoking a function that does
    not exist and reporting sample databases as not reseeded."""
    env = _adt_app_env(_synth_compute_template(tmp_path_factory, mode, context))
    if mode in ("seed", "simulator", "workshop"):
        assert env.get("ADT_SEED_LAMBDA") == "adt-seed-data", sorted(env)
    else:
        assert "ADT_SEED_LAMBDA" not in env, sorted(env)


@pytest.mark.parametrize(("mode", "context"), _DEPLOYED_MODE_CONTEXTS)
def test_deployed_modes_omit_local_auth_bypass(tmp_path_factory, mode, context):
    """Base, sample-data, workshop, and HighByte deploys remain real-auth only."""
    template = _synth_compute_template(tmp_path_factory, mode, context)
    forbidden_names = {"DEV_NO_AUTH", "DEV_MODE"}
    found = []
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") != "AWS::ECS::TaskDefinition":
            continue
        for container in resource["Properties"].get("ContainerDefinitions", []):
            if container.get("Name") != "adt-app":
                continue
            found.extend(container.get("Environment", []))

    names = {entry.get("Name") for entry in found}
    assert names.isdisjoint(forbidden_names), sorted(names & forbidden_names)
    assert not any(
        entry.get("Name") == "AGENT_MODE" and entry.get("Value") == "local"
        for entry in found
    ), found


def test_the_app_role_deletes_and_restores_only_per_system_connection_secrets(compute_template):
    """A full removal of a USER registration schedules deletion of its own
    adt/conn/<id> secret, and a re-registration inside the recovery window
    restores the name (agents/connection_resolver.py). The task role may do
    both ONLY on the adt/conn/* prefix, and never ForceDelete-style actions on
    anything else."""
    statements = [s for stmts in _task_role_inline_policies(compute_template).values()
                  for s in stmts]
    for s in statements:
        actions = s.get("Action", [])
        actions = actions if isinstance(actions, list) else [actions]
        if not {"secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret"} & set(actions):
            continue
        resources = s.get("Resource", [])
        resources = resources if isinstance(resources, list) else [resources]
        rendered = [_render(r) for r in resources]
        assert rendered and all(r.endswith(":secret:adt/conn/*") for r in rendered), rendered
        assert set(actions) <= {"secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret"}
        break
    else:
        raise AssertionError("no DeleteSecret/RestoreSecret statement on the app task role")


def _secret_statements(template: dict) -> list[tuple[set, list[str]]]:
    """(actions, rendered resources) of every Secrets Manager statement on the
    adt-app task role."""
    out = []
    for stmts in _task_role_inline_policies(template).values():
        for s in stmts:
            actions = s.get("Action", [])
            actions = set(actions if isinstance(actions, list) else [actions])
            if not any(a.startswith("secretsmanager:") for a in actions):
                continue
            resources = s.get("Resource", [])
            resources = resources if isinstance(resources, list) else [resources]
            out.append((actions, [_render(r) for r in resources]))
    return out


_THIS_ACCOUNT_SECRET = "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:"


def test_the_app_role_writes_per_system_credential_secrets_under_their_own_prefixes(
        compute_template):
    """The onboarding chat's store_credentials runs in the app (the onboarding
    runtime no longer exports it), so the task role creates and overwrites
    adt/systems/<id> itself. Its secret writes are scoped to the per-system
    prefixes the app writes in this account and Region: adt/systems/* (chat
    and Onboard view credentials), adt/conn/* (registrations) and
    adt/agent-config/* (system agent config), plus the AgentCore identity
    secrets. No adt* wildcard, which would also cover every other adt-named
    secret in the account."""
    writes = {"secretsmanager:CreateSecret", "secretsmanager:PutSecretValue"}
    written = [r for actions, resources in _secret_statements(compute_template)
               if actions & writes for r in resources]
    assert f"{_THIS_ACCOUNT_SECRET}adt/systems/*" in written, written
    allowed = {f"{_THIS_ACCOUNT_SECRET}{p}" for p in (
        "adt/systems/*", "adt/conn/*", "adt/agent-config/*", "bedrock-agentcore*")}
    assert set(written) <= allowed, sorted(set(written) - allowed)
    for actions, resources in _secret_statements(compute_template):
        if f"{_THIS_ACCOUNT_SECRET}adt/systems/*" in resources and actions & writes:
            assert actions <= writes | {"secretsmanager:DescribeSecret"}, actions


def test_the_app_role_can_move_secret_version_stages_only_on_the_cas_prefixes(compute_template):
    """The agent-config compare-and-set write promotes a pending version with
    UpdateSecretVersionStage, and a failed re-provision moves AWSCURRENT back
    with it (agent_manager, connection_resolver); without it a provision
    fails AccessDenied on that call. The task role holds it on
    adt/agent-config/* and adt/conn/* only, alone in its statement."""
    stage = "secretsmanager:UpdateSecretVersionStage"
    granted = [(actions, resources) for actions, resources in _secret_statements(compute_template)
               if stage in actions]
    assert granted, "the task role cannot move a secret version stage"
    resources = {r for _actions, rs in granted for r in rs}
    assert resources == {f"{_THIS_ACCOUNT_SECRET}adt/agent-config/*",
                         f"{_THIS_ACCOUNT_SECRET}adt/conn/*"}, sorted(resources)
    assert all(actions == {stage} for actions, _rs in granted)


def test_the_app_role_reads_the_per_system_credential_secrets_it_writes(compute_template):
    """The app's provision and connection resolution read the secret the
    entry or node points at, so the task role can read adt/systems/* too."""
    read = [r for actions, resources in _secret_statements(compute_template)
            if "secretsmanager:GetSecretValue" in actions for r in resources]
    assert f"{_THIS_ACCOUNT_SECRET}adt/systems/*" in read, read


def test_the_app_role_makes_the_sitewise_reads_discovery_makes(compute_template):
    """The Onboard view's discover of a same-account SiteWise (Direct) system
    runs in the app through the same reader (agents/sitewise_read.py) as the
    onboarding runtime's, including the per-model property list."""
    actions = {a for stmts in _task_role_inline_policies(compute_template).values()
               for s in stmts
               for a in (s.get("Action") if isinstance(s.get("Action"), list)
                         else [s.get("Action")])}
    needed = {"iotsitewise:ListAssetModels", "iotsitewise:DescribeAssetModel",
              "iotsitewise:ListAssetModelProperties", "iotsitewise:ListAssets",
              "iotsitewise:DescribeAsset", "iotsitewise:GetAssetPropertyValue"}
    assert needed <= actions, needed - actions


_BUILD_PATH_ENDPOINTS = {"s3", "ecr.api", "ecr.dkr"}
_SERVICE_ENDPOINTS = {"secretsmanager", "neptune-graph-data", "neptune-graph"}


def _endpoint_services(template: dict) -> set[str]:
    """Service suffixes (``s3``, ``ecr.api`` ...) of the template's VPC endpoints."""
    out = set()
    for res in template["Resources"].values():
        if res["Type"] != "AWS::EC2::VPCEndpoint":
            continue
        service = res["Properties"]["ServiceName"]
        if isinstance(service, dict):
            service = "".join(p if isinstance(p, str) else "<region>" for p in service["Fn::Join"][1])
        out.add(service.split("com.amazonaws.", 1)[-1].split(".", 1)[-1])
    return out


def test_workshop_compute_keeps_every_engine_endpoint_only_under_its_compute_condition(
        tmp_path_factory, compute_template):
    """Test event 2026-10-18: the Workshop Studio root's VPC CodeBuild needs
    S3 + ECR before ADTComputeStack exists. A fresh deployment, 2026-11-11: Compute's
    private-DNS Secrets Manager endpoint came up while the ERP Lambda child's
    story Lambda was calling Secrets Manager, and the call timed out. So a
    fresh workshop create (``network-all``) puts every one of these endpoints
    in ADTNetworkStack. Stacks created before a move hold them here, so
    workshop Compute still declares them, the build path under
    ``BuildPathEndpointsInCompute`` and Secrets Manager / Neptune under
    ``ServiceEndpointsInCompute`` (tests/test_build_path_endpoint_owner.py
    pins the logical ids and the one-owner rule). Standalone deploys create
    them here unconditionally. The mocks' own endpoints (private DNS off) live
    in their own stacks."""
    standalone = _endpoint_services(compute_template)
    workshop_template = _synth_compute_template(
        tmp_path_factory, "workshop", ("-c", "with_workshop=true", "-c", "with_simulator=true"))
    workshop = _endpoint_services(workshop_template)
    assert _BUILD_PATH_ENDPOINTS | _SERVICE_ENDPOINTS <= standalone
    assert standalone == workshop
    for res in compute_template["Resources"].values():
        assert "Condition" not in res or res["Type"] != "AWS::EC2::VPCEndpoint"
    for res in workshop_template["Resources"].values():
        if res["Type"] != "AWS::EC2::VPCEndpoint":
            continue
        service = _endpoint_services({"Resources": {"x": res}}).pop()
        if service in _BUILD_PATH_ENDPOINTS:
            assert res.get("Condition") == "BuildPathEndpointsInCompute", service
        else:
            assert service in _SERVICE_ENDPOINTS, service
            assert res.get("Condition") == "ServiceEndpointsInCompute", service


# --- Model defaults: the ECS task uses the runtime env contract's spelling ---

_CONTRACT_PATH = REPO_ROOT / "agentic_digital_thread" / "agents" / "runtime_env_contract.py"
_MODEL_REGIONS = ("us-east-1", "eu-west-1", "ap-southeast-2")


def _runtime_env_contract():
    """The app's runtime env contract, loaded by path (stdlib-only and
    import-pure; the root suite does not put the app package on sys.path)."""
    import importlib.util

    name = "adt_test_runtime_env_contract"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _CONTRACT_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses resolve their module by name
        spec.loader.exec_module(module)
    return sys.modules[name]


def _synth_in_region(tmp_path_factory, region: str) -> dict:
    """A base synth bound to *region* (a placeholder account makes the stack
    environment-specific, so ``self.region`` is the literal Region rather
    than the ``AWS::Region`` token an account-less synth gets)."""
    return _synth_compute_template(
        tmp_path_factory, f"base_{region}", ("-c", f"region={region}"),
        (("CDK_DEFAULT_ACCOUNT", "111122223333"), ("CDK_DEFAULT_REGION", region),
         ("AWS_DEFAULT_REGION", region)))


@pytest.mark.parametrize("region", _MODEL_REGIONS)
def test_synthesized_model_defaults_are_the_contract_defaults(tmp_path_factory, region):
    """The adt-app container synthesized for each Region carries the
    contract's system-agent default, and the cdk.json orchestrator model is
    the contract's default too."""
    contract = _runtime_env_contract()
    template = _synth_in_region(tmp_path_factory, region)
    env = _adt_app_env(template)
    assert env["SYSTEM_AGENT_MODEL"] == contract.default_system_agent_model(region)
    cdk_json = json.loads((REPO_ROOT / "cdk.json").read_text())
    assert cdk_json["context"]["bedrock_model"] == contract.default_bedrock_model(region)
    assert env["BEDROCK_MODEL"] == cdk_json["context"]["bedrock_model"]
    # The synth really was bound to the Region (the old code keyed on it).
    assert env["AWS_REGION"] == region


def test_account_less_synth_carries_the_contract_default(compute_template):
    """The default (account-less) synth, where the Region is a token."""
    env = _adt_app_env(compute_template)
    assert env["SYSTEM_AGENT_MODEL"] == _runtime_env_contract().default_system_agent_model("us-east-1")


def test_blank_bedrock_model_context_falls_back_to_the_contract_default(tmp_path_factory):
    """cdk.json always supplies ``bedrock_model``, so the base synth never
    reaches the stack's own fallback. A blank context value does: the ECS
    task (and the BedrockModel output the workshop CR reads) must then carry
    the runtime env contract's default_bedrock_model, not an empty id."""
    contract = _runtime_env_contract()
    template = _synth_compute_template(
        tmp_path_factory, "blank-bedrock-model", ("-c", "bedrock_model="))
    env = _adt_app_env(template)
    assert env["BEDROCK_MODEL"] == contract.default_bedrock_model("us-east-1")
    assert template["Outputs"]["BedrockModel"]["Value"] == env["BEDROCK_MODEL"]


def test_task_role_bedrock_grant_covers_the_global_profiles(compute_template):
    """A global inference profile is authorized on three ARNs (the Amazon
    Bedrock user guide, global cross-Region inference): the profile in the
    source Region, the foundation model in the source Region, and the
    Region-less foundation model. The task role's invoke statement must cover
    all three for both default models."""
    from fnmatch import fnmatchcase

    contract = _runtime_env_contract()
    statements = _task_role_inline_policies(compute_template)["Bedrock"]
    (invoke,) = [s for s in statements
                 if "bedrock:InvokeModel" in (s["Action"] if isinstance(s["Action"], list)
                                              else [s["Action"]])]
    patterns = [_render(r).replace("${AWS::Region}", "us-east-1")
                .replace("${AWS::AccountId}", "111122223333")
                for r in (invoke["Resource"] if isinstance(invoke["Resource"], list)
                          else [invoke["Resource"]])]
    for profile in (contract.default_bedrock_model("us-east-1"),
                    contract.default_system_agent_model("us-east-1")):
        assert profile.startswith("global.")
        model = profile.split(".", 1)[1]
        account = "111122223333"
        for arn in (f"arn:aws:bedrock:us-east-1:{account}:inference-profile/{profile}",
                    f"arn:aws:bedrock:us-east-1::foundation-model/{model}",
                    f"arn:aws:bedrock:::foundation-model/{model}"):
            # A synth with no account renders literal ARNs with a real account
            # id; match the account segment loosely in that case.
            assert any(fnmatchcase(arn, p) or fnmatchcase(
                arn, re.sub(r":\d{12}:", f":{account}:", p)) for p in patterns), (arn, patterns)
