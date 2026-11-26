# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ADTERPLambdaStack ships the live-story history custom resource ONLY for
``-c with_workshop=true -c workshop_story=live`` (ADR-0085).

* live: ``Custom::ADTStoryHistory`` + function ``erp-story-history`` running the
  SAME image asset as the simulation Lambda (CMD override only), so the stack's
  Docker image asset count is unchanged;
* frozen workshop and plain ``--simulator``: neither exists, and the two
  templates carry exactly the same resources.

Synthesized offline (no AWS calls, Docker stubbed); needs the vendored CDK CLI
under node_modules and skips without it (mirrors test_adt_compute_env_wiring).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
STACK = "ADTERPLambdaStack"
DATABASE_STACK = "ADTERPDatabaseStack"
DB_INIT_ID_PREFIX = "DatabaseInitResource"
HISTORY_TYPE = "Custom::ADTStoryHistory"
HISTORY_FUNCTION = "erp-story-history"
SIMULATION_FUNCTION = "erp-simulation-orchestrator"
PACKAGED_ANCHOR = 1790000010

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


def _offline_docker_stub(out_dir: Path) -> Path:
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


def _synth(tmp_path_factory, name: str, context: tuple[str, ...]) -> dict:
    """Synth the app for ``context``; return the stack's template and assets."""
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")
    out_dir = tmp_path_factory.mktemp(f"cdk_synth_erp_lambda_{name}")
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CDK_DOCKER": str(_offline_docker_stub(out_dir)),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [cdk_bin, "synth", STACK, "-c", "region=us-east-1", "-c", "cdk_nag_disabled=true",
         *context, "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=900,
    )
    template_path = out_dir / f"{STACK}.template.json"
    if result.returncode != 0 or not template_path.exists():
        pytest.fail(f"synth did not produce a template:\n{result.stderr[-4000:]}")
    database_path = out_dir / f"{DATABASE_STACK}.template.json"
    return {
        "template": json.loads(template_path.read_text()),
        "assets": json.loads((out_dir / f"{STACK}.assets.json").read_text()),
        "database": json.loads(database_path.read_text()) if database_path.exists() else None,
    }


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    return _synth(tmp_path_factory, "live", ("-c", "with_workshop=true", "-c", "with_simulator=true", "-c", "workshop_story=live"))


@pytest.fixture(scope="module")
def live_again(tmp_path_factory):
    return _synth(tmp_path_factory, "live_again", ("-c", "with_workshop=true", "-c", "with_simulator=true", "-c", "workshop_story=live"))


@pytest.fixture(scope="module")
def live_anchored(tmp_path_factory):
    return _synth(tmp_path_factory, "live_anchored", (
        "-c", "with_workshop=true", "-c", "with_simulator=true", "-c", "workshop_story=live",
        "-c", f"story_anchor={PACKAGED_ANCHOR}"))


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    return _synth(tmp_path_factory, "frozen", ("-c", "with_workshop=true", "-c", "with_simulator=true"))


@pytest.fixture(scope="module")
def simulator(tmp_path_factory):
    return _synth(tmp_path_factory, "simulator", ("-c", "with_simulator=true"))


def _resources(synth: dict) -> dict:
    return synth["template"]["Resources"]


def _of_type(synth: dict, rtype: str) -> dict:
    return {k: v for k, v in _resources(synth).items() if v["Type"] == rtype}


def _function(synth: dict, name: str):
    for lid, res in _of_type(synth, "AWS::Lambda::Function").items():
        if res["Properties"].get("FunctionName") == name:
            return lid, res["Properties"]
    return None, None


def _image_uri(props: dict) -> str:
    return json.dumps(props["Code"]["ImageUri"], sort_keys=True)


def test_live_story_ships_the_history_custom_resource(live):
    crs = _of_type(live, HISTORY_TYPE)
    assert len(crs) == 1
    (props,) = [r["Properties"] for r in crs.values()]
    assert props["Days"] == "3"
    lid, fn = _function(live, HISTORY_FUNCTION)
    assert lid is not None
    assert fn["ImageConfig"]["Command"] == ["lambda_story_history.lambda_handler"]
    assert fn["Timeout"] == 600
    env = fn["Environment"]["Variables"]
    assert {"DB_SECRET_NAME", "SEED_DB_SECRET_NAME"} <= set(env)
    assert fn["VpcConfig"]["SubnetIds"]


def _db_init_properties(synth: dict) -> dict:
    assert synth["database"] is not None, f"{DATABASE_STACK} template not synthesized"
    (res,) = [r for lid, r in synth["database"]["Resources"].items()
              if lid.startswith(DB_INIT_ID_PREFIX) and r["Type"] != "AWS::Lambda::Function"]
    return res["Properties"]


def test_history_reruns_whenever_the_db_init_reruns(live):
    """db-init DROPS and reseeds every ERP table whenever ITS properties
    change, so the history CR carries the same literal trigger (Timestamp +
    DatabaseName) and secret ARN: a db-init bump is also a history Update,
    which (no stored anchor any more) rewrites the history."""
    from stacks import erp_database_stack

    init = _db_init_properties(live)
    (props,) = [r["Properties"] for r in _of_type(live, HISTORY_TYPE).values()]
    assert init["Timestamp"] == erp_database_stack.DB_INIT_TIMESTAMP
    assert props["ErpDbInit"] == f"{init['Timestamp']}|{init['DatabaseName']}"
    # The secret ARN rides the SAME cross-stack import the simulation Lambda
    # already uses (no new export, so live -> frozen can drop it safely).
    secret_import = props["ErpDbSecretArn"]
    assert "Fn::ImportValue" in secret_import
    _lid, sim_fn = _function(live, "erp-simulation-orchestrator")
    assert sim_fn["Environment"]["Variables"]["DB_SECRET_NAME"] == secret_import


def test_history_trigger_follows_a_db_init_bump(monkeypatch):
    """A new db-init Timestamp changes the history token (no synth)."""
    pytest.importorskip("aws_cdk")
    from stacks import erp_database_stack

    before = erp_database_stack.db_init_trigger("erp_simulation")
    monkeypatch.setattr(erp_database_stack, "DB_INIT_TIMESTAMP", "2099-01-01-bump")
    after = erp_database_stack.db_init_trigger("erp_simulation")
    assert before != after and after == "2099-01-01-bump|erp_simulation"


# ---------------------------------------------------------------------------
# Version = content hash of the history writer: a stack update that ships a
# NEW writer is a CR property change, so CloudFormation sends it an Update.
# ---------------------------------------------------------------------------

HISTORY_SOURCES = (
    "erp_simulation/story_history.py",
    "lambda_/lambda_story_history.py",
    "erp_simulation/story_replan.py",
    "erp_simulation/condition_monitor.py",
    "erp_simulation/story_live.py",
    "erp_simulation/mes_simulator.py",
)


def test_history_sources_are_the_writer_import_closure():
    """Offline (no synth): the Version hashes what the history / place path
    RUNS - story_replan imports story_live, which (with story_history) pulls
    condition_monitor and mes_simulator."""
    from stacks import erp_lambda_stack

    assert erp_lambda_stack.STORY_HISTORY_SOURCES == HISTORY_SOURCES
    assert all((REPO_ROOT / rel).is_file() for rel in HISTORY_SOURCES)


def _expected_version(root: Path = REPO_ROOT) -> str:
    """Independent re-derivation of the documented hash contract."""
    import hashlib

    digest = hashlib.sha256()
    for rel in HISTORY_SOURCES:
        digest.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def test_history_version_is_the_writer_content_hash(live):
    from stacks import erp_lambda_stack

    (props,) = [r["Properties"] for r in _of_type(live, HISTORY_TYPE).values()]
    assert erp_lambda_stack.STORY_HISTORY_SOURCES == HISTORY_SOURCES
    assert props["Version"] == _expected_version()
    assert len(props["Version"]) == 16 and int(props["Version"], 16) >= 0


def test_history_version_is_stable_across_synths(live, live_again):
    (first,) = [r["Properties"]["Version"] for r in _of_type(live, HISTORY_TYPE).values()]
    (second,) = [r["Properties"]["Version"] for r in _of_type(live_again, HISTORY_TYPE).values()]
    assert first == second


def test_history_version_follows_a_writer_change(tmp_path, monkeypatch):
    """Editing either writer file changes the Version (no synth, temp copy)."""
    from stacks import erp_lambda_stack

    for rel in HISTORY_SOURCES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((REPO_ROOT / rel).read_bytes())
    monkeypatch.setattr(erp_lambda_stack, "REPO_ROOT", tmp_path)
    base = erp_lambda_stack.story_history_version()
    assert base == _expected_version() == erp_lambda_stack.story_history_version()
    seen = {base}
    for rel in HISTORY_SOURCES:
        with (tmp_path / rel).open("ab") as fh:
            fh.write(b"\n# changed\n")
        seen.add(erp_lambda_stack.story_history_version())
    assert len(seen) == len(HISTORY_SOURCES) + 1


def test_history_version_fails_loud_on_a_missing_writer(tmp_path, monkeypatch):
    from stacks import erp_lambda_stack

    monkeypatch.setattr(erp_lambda_stack, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        erp_lambda_stack.story_history_version()


def test_history_function_reuses_the_simulation_image_asset(live, simulator):
    _lid, sim_fn = _function(live, "erp-simulation-orchestrator")
    _lid, story_fn = _function(live, HISTORY_FUNCTION)
    assert _image_uri(story_fn) == _image_uri(sim_fn)
    assert len(live["assets"]["dockerImages"]) == len(simulator["assets"]["dockerImages"])
    assert set(live["assets"]["dockerImages"]) == set(simulator["assets"]["dockerImages"])


@pytest.mark.parametrize("name", ["frozen", "simulator"])
def test_no_history_outside_the_live_story(name, request):
    synth = request.getfixturevalue(name)
    assert not _of_type(synth, HISTORY_TYPE)
    assert _function(synth, HISTORY_FUNCTION) == (None, None)


def test_frozen_and_simulator_templates_carry_the_same_resources(frozen, simulator):
    assert {k: v["Type"] for k, v in _resources(frozen).items()} == \
        {k: v["Type"] for k, v in _resources(simulator).items()}


def test_live_only_adds_story_history_resources(live, frozen):
    """Live adds the history writer, the story-anchor mirror parameter and the
    one-time history-replay backfill parameter (ADR-0092 + the replay ADR)."""
    added = set(_resources(live)) - set(_resources(frozen))
    assert added and all(
        lid.startswith(("StoryHistory", "StoryAnchorParameter", "StoryFeedBackfillParameter"))
        for lid in added), sorted(added)
    assert sum(lid.startswith("StoryAnchorParameter") for lid in added) == 1
    assert sum(lid.startswith("StoryFeedBackfillParameter") for lid in added) == 1
    assert set(_resources(frozen)) <= set(_resources(live))


ERP_FUNCTIONS = ("erp-simulation-orchestrator", "erp-cleanup-old-data", "erp-seed-data-deployer")


@pytest.mark.parametrize("name, functions", [
    ("simulator", ERP_FUNCTIONS),
    ("live", ERP_FUNCTIONS + (HISTORY_FUNCTION,)),
])
def test_every_erp_function_logs_to_its_own_one_week_group(name, functions, request):
    """Without its own group a function writes to ``/aws/lambda/<function>``,
    which never expires (2.25 TB of simulator tracebacks on a demo account)."""
    synth = request.getfixturevalue(name)
    groups = _of_type(synth, "AWS::Logs::LogGroup")
    used = []
    for function in functions:
        lid, props = _function(synth, function)
        assert lid is not None, function
        ref = props["LoggingConfig"]["LogGroup"]["Ref"]
        assert groups[ref]["Properties"]["RetentionInDays"] == 7, function
        assert "LogGroupName" not in groups[ref]["Properties"]   # generated: the old group still exists
        used.append(ref)
    assert len(set(used)) == len(functions)                       # one group per function


# ---------------------------------------------------------------------------
# The guard itself (no synth): live without the seed stack is a synth error.
# ---------------------------------------------------------------------------

class _Node:
    def __init__(self, **ctx):
        self._ctx = ctx

    def try_get_context(self, key):
        return self._ctx.get(key)


def test_live_without_seed_stack_fails_synth():
    pytest.importorskip("aws_cdk")
    from stacks.erp_lambda_stack import LambdaStack

    fake = SimpleNamespace(node=_Node(with_workshop="true", workshop_story="live"), seed_data_stack=None)
    with pytest.raises(ValueError, match="seed-data stack"):
        LambdaStack._create_story_history(fake)


@pytest.mark.parametrize("ctx", [{}, {"with_workshop": "true", "with_simulator": "true"}, {"with_workshop": "true", "with_simulator": "true", "workshop_story": "frozen"}])
def test_non_live_story_adds_nothing(ctx):
    pytest.importorskip("aws_cdk")
    from stacks.erp_lambda_stack import LambdaStack

    fake = SimpleNamespace(node=_Node(**ctx), seed_data_stack=None)
    LambdaStack._create_story_history(fake)
    assert not hasattr(fake, "story_history")


def test_anchor_on_a_non_live_story_fails_synth():
    """A packaged anchor only means something on the live story clock."""
    pytest.importorskip("aws_cdk")
    from stacks.erp_lambda_stack import LambdaStack

    fake = SimpleNamespace(
        node=_Node(with_workshop="true", workshop_story="frozen", story_anchor=str(PACKAGED_ANCHOR)),
        seed_data_stack=None)
    with pytest.raises(ValueError, match="story_anchor"):
        LambdaStack._create_story_history(fake)


# ---------------------------------------------------------------------------
# Anchor (D14): the packaged story anchor rides the CR as an epoch string;
# "" when the package carries none. The Lambda re-plans only when it changes.
# ---------------------------------------------------------------------------

def _history_props(synth: dict) -> dict:
    (props,) = [r["Properties"] for r in _of_type(synth, HISTORY_TYPE).values()]
    return props


def test_history_anchor_is_empty_without_a_packaged_anchor(live):
    assert _history_props(live)["Anchor"] == ""


def test_history_anchor_carries_the_packaged_epoch(live_anchored, live):
    props = _history_props(live_anchored)
    assert props["Anchor"] == str(PACKAGED_ANCHOR)
    # Version is the writer hash only: an anchor never masquerades as a code change.
    assert props["Version"] == _history_props(live)["Version"]


@pytest.mark.parametrize(("raw", "expected"), [
    (None, ""), ("", ""), (str(PACKAGED_ANCHOR), str(PACKAGED_ANCHOR)),
    ("2026-10-14T08:00:00-05:00", "1791982800"),
])
def test_anchor_property_from_context(raw, expected):
    pytest.importorskip("aws_cdk")
    from stacks.erp_lambda_stack import story_anchor_property

    ctx = {"with_workshop": "true", "with_simulator": "true", "workshop_story": "live", "story_anchor": raw}
    assert story_anchor_property(_Node(**ctx)) == expected


# ---------------------------------------------------------------------------
# ADR-0092: no Lambda writes SiteWise. Under live ONLY the simulation role may
# read and overwrite exactly the story-anchor parameter the story site's OPC
# UA server follows, and the function carries its name.
# ---------------------------------------------------------------------------

ANCHOR_PARAMETER = "/adt/workshop/story-anchor-epoch"


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


def _role_statements(synth: dict, function_name: str) -> list[dict]:
    """Every statement of every AWS::IAM::Policy attached to *function_name*'s role."""
    _lid, fn = _function(synth, function_name)
    role_lid = fn["Role"]["Fn::GetAtt"][0]
    statements = []
    for res in _of_type(synth, "AWS::IAM::Policy").values():
        if {"Ref": role_lid} in res["Properties"].get("Roles", []):
            statements.extend(res["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _actions(statement: dict) -> list[str]:
    actions = statement["Action"]
    return sorted([actions] if isinstance(actions, str) else actions)


def _anchor_parameters(synth: dict) -> list[dict]:
    return [r["Properties"] for r in _of_type(synth, "AWS::SSM::Parameter").values()
            if r["Properties"].get("Name") == ANCHOR_PARAMETER]


BACKFILL_PARAMETER = "/adt/workshop/story-feed-backfill"


def _backfill_parameters(synth: dict) -> list[dict]:
    return [r["Properties"] for r in _of_type(synth, "AWS::SSM::Parameter").values()
            if r["Properties"].get("Name") == BACKFILL_PARAMETER]


def _ssm_actions(synth: dict) -> list[str]:
    return sorted(a for s in _role_statements(synth, SIMULATION_FUNCTION)
                  for a in _actions(s) if a.startswith("ssm:"))


def test_live_simulation_role_writes_no_sitewise(live):
    statements = _role_statements(live, SIMULATION_FUNCTION)
    assert not any(a.startswith("iotsitewise:") for s in statements for a in _actions(s))
    assert "iotsitewise:" not in json.dumps(live["template"])


def test_live_mirrors_the_anchor_into_one_parameter(live):
    (param,) = _anchor_parameters(live)
    assert param["Type"] == "String" and param["Value"] == "pending"
    param_lid = next(lid for lid, r in _of_type(live, "AWS::SSM::Parameter").items()
                     if r["Properties"].get("Name") == ANCHOR_PARAMETER)
    backfill_lid = next(lid for lid, r in _of_type(live, "AWS::SSM::Parameter").items()
                        if r["Properties"].get("Name") == BACKFILL_PARAMETER)
    assert "ssm:PutParameter" in _ssm_actions(live) and "ssm:GetParameter" in _ssm_actions(live)
    # every SSM grant names exactly the anchor parameter or the replay's second
    # parameter (the replay ADR), never a wildcard or another parameter
    allowed = {f":parameter${{{lid}}}" for lid in (param_lid, backfill_lid)}
    granted = set()
    for statement in _role_statements(live, SIMULATION_FUNCTION):
        if any(a.startswith("ssm:") for a in _actions(statement)):
            resources = statement["Resource"]
            rendered = [_render(r) for r in (resources if isinstance(resources, list) else [resources])]
            assert all(any(r.endswith(a) for a in allowed) for r in rendered), rendered
            granted.update(a for r in rendered for a in allowed if r.endswith(a))
    assert f":parameter${{{param_lid}}}" in granted
    _lid, fn = _function(live, SIMULATION_FUNCTION)
    assert fn["Environment"]["Variables"]["STORY_ANCHOR_PARAMETER"] == {"Ref": param_lid}


def test_live_mirrors_the_backfill_window_into_a_second_parameter(live):
    """The replay ADR: the default live profile ships the second parameter the
    story server polls, read+write to the simulation Lambda, env wired."""
    (param,) = _backfill_parameters(live)
    assert param["Type"] == "String" and param["Value"] == "pending"
    param_lid = next(lid for lid, r in _of_type(live, "AWS::SSM::Parameter").items()
                     if r["Properties"].get("Name") == BACKFILL_PARAMETER)
    _lid, fn = _function(live, SIMULATION_FUNCTION)
    assert fn["Environment"]["Variables"]["STORY_FEED_BACKFILL_PARAMETER"] == {"Ref": param_lid}
    # the simulation role can read AND write exactly the two story parameters
    rendered = [_render(r) for s in _role_statements(live, SIMULATION_FUNCTION)
                for a in _actions(s) if a == "ssm:PutParameter"
                for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])]
    assert any(param_lid in r for r in rendered), rendered


@pytest.mark.parametrize("name", ["frozen", "simulator"])
def test_no_anchor_mirror_and_no_sitewise_outside_the_live_story(name, request):
    synth = request.getfixturevalue(name)
    assert _anchor_parameters(synth) == []
    assert _backfill_parameters(synth) == []
    assert _ssm_actions(synth) == []
    assert "iotsitewise:" not in json.dumps(synth["template"])
    _lid, fn = _function(synth, SIMULATION_FUNCTION)
    assert "STORY_ANCHOR_PARAMETER" not in fn["Environment"]["Variables"]
    assert "STORY_FEED_BACKFILL_PARAMETER" not in fn["Environment"]["Variables"]


# ---------------------------------------------------------------------------
# The packaged loop length. ``-c story_loop_seconds=<s>`` (live only)
# rides as env STORY_LOOP_SECONDS on the simulation orchestrator and the
# history writer, and as the CR's ``LoopSeconds`` (a loop change re-runs it).
# Unset: the env dicts and the CR properties are EXACTLY today's (pinned
# synth-free below, both ways).
# ---------------------------------------------------------------------------

LOOP_SECONDS = 10800
_LIVE_CTX = {"with_workshop": "true", "with_simulator": "true", "workshop_story": "live"}


@pytest.mark.parametrize(("ctx", "expected_env", "expected_prop"), [
    ({}, {}, {}),
    (_LIVE_CTX, {}, {}),
    ({**_LIVE_CTX, "story_loop_seconds": ""}, {}, {}),
    ({**_LIVE_CTX, "story_loop_seconds": "28800"}, {}, {}),
    ({**_LIVE_CTX, "story_loop_seconds": str(LOOP_SECONDS)},
     {"STORY_LOOP_SECONDS": str(LOOP_SECONDS)}, {"LoopSeconds": str(LOOP_SECONDS)}),
])
def test_loop_env_and_property_from_context(ctx, expected_env, expected_prop):
    pytest.importorskip("aws_cdk")
    from stacks.erp_lambda_stack import story_loop_env, story_loop_property

    assert story_loop_env(_Node(**ctx)) == expected_env
    assert story_loop_property(_Node(**ctx)) == expected_prop


@pytest.mark.parametrize("ctx", [
    {"story_loop_seconds": str(LOOP_SECONDS)},
    {"with_workshop": "true", "with_simulator": "true", "workshop_story": "frozen", "story_loop_seconds": str(LOOP_SECONDS)},
])
def test_loop_on_a_non_live_synth_fails(ctx):
    pytest.importorskip("aws_cdk")
    from stacks.erp_lambda_stack import story_loop_env, story_loop_property

    for helper in (story_loop_env, story_loop_property):
        with pytest.raises(ValueError, match="story_loop_seconds requires workshop_story=live"):
            helper(_Node(**ctx))


class _Recorder:
    """Stands in for a CDK construct class: records ``(id, kwargs)`` per call."""

    def __init__(self):
        self.calls = []

    def __call__(self, _scope, construct_id, **kwargs):
        self.calls.append((construct_id, kwargs))
        return SimpleNamespace(node=SimpleNamespace(add_dependency=lambda *_a: None),  # nosec B106 - a fake service token for the stack test
                               service_token="token")


def _fake_stack(ctx):
    secret = SimpleNamespace(secret_arn="arn:secret:erp", grant_read=lambda *_a: None)  # nosec B106 - a fake secret ARN for the stack test
    seed_secret = SimpleNamespace(secret_arn="arn:secret:seed", grant_read=lambda *_a: None)  # nosec B106 - a fake secret ARN for the stack test
    return SimpleNamespace(
        node=_Node(**ctx), region="us-east-1", timeout_seconds=300, memory_mb=512,
        network_stack=SimpleNamespace(vpc="vpc"),
        database_stack=SimpleNamespace(db_secret=secret, lambda_sg="sg", init_trigger="trigger",
                                       init_resource="init"),
        seed_data_stack=SimpleNamespace(db_secret=seed_secret),
        _one_week_log_group=lambda construct_id: f"log-group:{construct_id}")


def _built(monkeypatch, ctx, build):
    """Run one LambdaStack builder against fakes; return the recorder calls.

    The story history direct-reset registration (register_reset_steps /
    custom_resource_step) is captured on ``mod``: its calls are recorded on
    ``_built.registry``, so a builder that registers the step does not try to
    make real SSM constructs against the fake scope."""
    pytest.importorskip("aws_cdk")
    from stacks import erp_lambda_stack as mod

    functions, resources = _Recorder(), _Recorder()
    monkeypatch.setattr(mod.lambda_, "DockerImageFunction", functions)
    monkeypatch.setattr(mod, "CustomResource", resources)
    monkeypatch.setattr(mod.cr, "Provider", _Recorder())
    registry = SimpleNamespace(steps=None, parameter=None, physical_id=None, step_name=None)

    def _custom_resource_step(name, function, physical_id, properties):
        registry.step_name, registry.physical_id = name, physical_id
        return {"Name": name, "FunctionName": "erp-story-history",
                "PhysicalResourceId": physical_id, "ResourceProperties": properties}, function

    def _register(_scope, _construct_id, steps, *, parameter=None):
        registry.steps, registry.parameter = steps, parameter

    monkeypatch.setattr(mod, "custom_resource_step", _custom_resource_step)
    monkeypatch.setattr(mod, "register_reset_steps", _register)
    fake = _fake_stack(ctx)
    fake._create_story_history_lambda = lambda: SimpleNamespace()
    fake._register_story_history_reset = lambda provider, props: \
        mod.LambdaStack._register_story_history_reset(fake, provider, props)
    _built.registry = registry
    build(mod.LambdaStack, fake)
    return functions.calls, resources.calls


_TODAY_SIMULATION_ENV = {
    "DB_SECRET_NAME": "arn:secret:erp", "REGION": "us-east-1", "USE_SECRETS_MANAGER": "true",
    "SEED_DB_SECRET_NAME": "arn:secret:seed",
}
_TODAY_HISTORY_ENV = {
    "DB_SECRET_NAME": "arn:secret:erp", "SEED_DB_SECRET_NAME": "arn:secret:seed",
    "REGION": "us-east-1", "USE_SECRETS_MANAGER": "true",
}


@pytest.mark.parametrize("builder,today", [
    ("_create_simulation_lambda", _TODAY_SIMULATION_ENV),
    ("_create_story_history_lambda", _TODAY_HISTORY_ENV),
])
def test_lambda_env_is_byte_identical_without_the_loop_and_adds_it_when_set(
        monkeypatch, builder, today):
    """Synth-free pin, both ways: the env dict (keys AND order) is today's
    exactly with the context unset; set, it appends STORY_LOOP_SECONDS only."""
    def build(cls, fake):
        getattr(cls, builder)(fake)

    ((_, unset),), _ = _built(monkeypatch, _LIVE_CTX, build)
    assert list(unset["environment"].items()) == list(today.items())
    ((_, looped),), _ = _built(
        monkeypatch, {**_LIVE_CTX, "story_loop_seconds": str(LOOP_SECONDS)}, build)
    assert list(looped["environment"].items()) == [
        *today.items(), ("STORY_LOOP_SECONDS", str(LOOP_SECONDS))]


def test_history_cr_properties_are_byte_identical_without_the_loop(monkeypatch):
    """Synth-free pin, both ways: the CR properties are today's exactly with
    the context unset; set, ``LoopSeconds`` is the only addition."""
    from stacks.erp_lambda_stack import story_history_version

    def build(cls, fake):
        cls._create_story_history(fake)

    today = {"Days": "3", "Version": story_history_version(), "Anchor": "",
             "ErpDbInit": "trigger", "ErpDbSecretArn": "arn:secret:erp"}
    _, ((_, unset),) = _built(monkeypatch, _LIVE_CTX, build)
    assert list(unset["properties"].items()) == list(today.items())
    _, ((_, looped),) = _built(
        monkeypatch, {**_LIVE_CTX, "story_loop_seconds": str(LOOP_SECONDS)}, build)
    assert list(looped["properties"].items()) == [*today.items(), ("LoopSeconds", str(LOOP_SECONDS))]


def test_create_story_history_registers_the_direct_reset_step(monkeypatch):
    """The live builder registers a direct-reset step for the history CR: a
    custom-resource step (physical id adt-story-history) carrying the CR's
    deployed properties plus the ServiceToken, in the story registry
    parameter (not the Tulip mock's)."""
    from stacks import _direct_reset
    from stacks.erp_lambda_stack import STORY_HISTORY_PHYSICAL_ID, story_history_version

    def build(cls, fake):
        cls._create_story_history(fake)

    _, ((_, cr_call),) = _built(monkeypatch, _LIVE_CTX, build)
    reg = _built.registry
    assert reg.parameter == _direct_reset.STORY_REGISTRY_PARAMETER
    assert reg.physical_id == STORY_HISTORY_PHYSICAL_ID
    (step, _function) = reg.steps[0]
    # the step's properties are the CR's, plus the ServiceToken the Update needs
    assert step["ResourceProperties"] == {"ServiceToken": "token", **cr_call["properties"]}
    assert cr_call["properties"]["Version"] == story_history_version()


@pytest.fixture(scope="module")
def live_looped(tmp_path_factory):
    return _synth(tmp_path_factory, "live_looped", (
        "-c", "with_workshop=true", "-c", "with_simulator=true", "-c", "workshop_story=live",
        "-c", f"story_loop_seconds={LOOP_SECONDS}"))


def _loop_env_functions(synth: dict) -> dict:
    return {props.get("FunctionName"): props["Environment"]["Variables"]["STORY_LOOP_SECONDS"]
            for props in (r["Properties"] for r in _of_type(synth, "AWS::Lambda::Function").values())
            if "STORY_LOOP_SECONDS" in (props.get("Environment") or {}).get("Variables", {})}


def test_default_live_synth_carries_no_loop_anywhere(live):
    text = json.dumps(live["template"])
    assert "STORY_LOOP_SECONDS" not in text and "LoopSeconds" not in text


def test_looped_live_synth_sets_the_env_on_exactly_the_two_lambdas_and_the_cr(live_looped, live):
    assert _loop_env_functions(live_looped) == {
        SIMULATION_FUNCTION: str(LOOP_SECONDS), HISTORY_FUNCTION: str(LOOP_SECONDS)}
    props = _history_props(live_looped)
    assert props["LoopSeconds"] == str(LOOP_SECONDS)
    # The loop is a CR property, never a Version (code-hash) change.
    assert props["Version"] == _history_props(live)["Version"]
    assert set(_resources(live_looped)) == set(_resources(live))


# ---------------------------------------------------------------------------
# Lambda: an Update whose LoopSeconds changed re-plans the STORED anchor
# (source kept) under the geometry condition_monitor read from
# STORY_LOOP_SECONDS. The handler itself never reads the env.
# ---------------------------------------------------------------------------

_LAMBDA_PATH = REPO_ROOT / "lambda_" / "lambda_story_history.py"
_NOW = 1790000000
_REPORT = {"new_anchor": 1789999810, "source": "placed", "failed_run_number": "RUN-X"}


class _ConfigCursor:
    def __init__(self, config):
        self._config, self._row, self.rowcount = config, None, 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params):
        key = params[0]
        if sql.lstrip().startswith("INSERT INTO simulation_config"):   # the default-anchor upsert
            self.rowcount = int(self._config.get(key) != params[1])
            self._config[key] = params[1]
            return
        self._row = (self._config[key],) if key in self._config else None

    def fetchone(self):
        return self._row


class _ConfigDB:
    def __init__(self, config=None):
        self.config = dict(config or {})

    def cursor(self):
        return _ConfigCursor(self.config)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def story_lambda(monkeypatch):
    """(module, erp_db, calls): fake connections, fixed clock, spies on the
    re-plan and on the first-write path."""
    import importlib.util

    pytest.importorskip("erp_simulation.story_replan")
    from erp_simulation import story_replan

    spec = importlib.util.spec_from_file_location("lambda_story_history_w4c", _LAMBDA_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    erp, seed = _ConfigDB(), _ConfigDB()
    calls = {"replan": [], "write": []}

    def replan(erp_conn, seed_conn, new_anchor, *, now, source, expected_anchor=None):
        assert (erp_conn, seed_conn) == (erp, seed)
        calls["replan"].append({"new_anchor": new_anchor, "now": now, "source": source,
                                "expected_anchor": expected_anchor})
        return dict(_REPORT, source=source)

    monkeypatch.setattr(module, "_connect", lambda env: erp if env == "DB_SECRET_NAME" else seed)
    monkeypatch.setattr(module, "_now", lambda: _NOW)
    monkeypatch.setattr(story_replan, "replan", replan)
    monkeypatch.setattr(module, "_write_history", lambda event: calls["write"].append(event) or {"w": 1})
    return module, erp, calls


def _update(new, old):
    return {"RequestType": "Update", "ResourceProperties": new, "OldResourceProperties": old,
            "PhysicalResourceId": "adt-story-history"}


@pytest.mark.parametrize(("old", "new"), [
    ({}, {"LoopSeconds": "10800"}),                           # set
    ({"LoopSeconds": "10800"}, {"LoopSeconds": "14400"}),     # changed
    ({"LoopSeconds": "10800"}, {}),                           # unset -> back to 8 h
])
def test_update_with_a_changed_loop_replans_the_stored_anchor_keeping_its_source(story_lambda, old, new):
    module, erp, calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "placed"})
    out = module.lambda_handler(_update({"Days": "3", "Anchor": "", **new},
                                        {"Days": "3", "Anchor": "", **old}), None)
    assert calls["replan"] == [{"new_anchor": 1789990010.0, "now": _NOW, "source": "placed",
                                "expected_anchor": 1789990010.0}]   # CAS on the pre-read anchor
    assert calls["write"] == []
    assert out == {"PhysicalResourceId": module.PHYSICAL_ID, "Data": {
        "AnchorEpoch": str(_REPORT["new_anchor"]), "AnchorSource": "placed",
        "FailedRunNumber": "RUN-X"}}


def test_update_with_a_changed_loop_and_no_stored_anchor_is_the_first_write(story_lambda):
    module, _erp, calls = story_lambda
    out = module.lambda_handler(_update({"LoopSeconds": "10800"}, {}), None)
    assert calls["replan"] == [] and len(calls["write"]) == 1
    assert out == {"PhysicalResourceId": module.PHYSICAL_ID, "Data": {"w": 1}}


def test_update_with_a_changed_anchor_and_loop_replans_onto_the_packaged_anchor(story_lambda):
    module, erp, calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "placed"})
    module.lambda_handler(_update({"Anchor": "1789980010", "LoopSeconds": "10800"}, {"Anchor": ""}), None)
    assert calls["replan"] == [{"new_anchor": 1789980010, "now": _NOW, "source": "packaged",
                                "expected_anchor": None}]


@pytest.mark.parametrize(("old", "new"), [
    ({}, {}), ({"LoopSeconds": "10800"}, {"LoopSeconds": " 10800 "}), ({"LoopSeconds": ""}, {}),
])
def test_update_with_the_same_loop_writes_only(story_lambda, old, new):
    module, erp, calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010"})
    module.lambda_handler(_update(new, old), None)
    assert calls["replan"] == [] and len(calls["write"]) == 1


def test_create_with_a_loop_is_the_first_write(story_lambda):
    module, _erp, calls = story_lambda
    module.lambda_handler({"RequestType": "Create", "ResourceProperties": {"LoopSeconds": "10800"}}, None)
    assert calls["replan"] == [] and len(calls["write"]) == 1


def test_handler_never_reads_the_loop_env_itself():
    """STORY_LOOP_SECONDS reaches the writer ONLY through condition_monitor."""
    import ast

    tree = ast.parse(_LAMBDA_PATH.read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and n.value == "STORY_LOOP_SECONDS"]


def _stale_then(story_lambda, monkeypatch, outcomes):
    """Make the re-plan raise StaleAnchor / move the stored anchor per ``outcomes``
    (``"stale:<anchor>"`` moves the stored anchor then raises; ``"ok"`` reports)."""
    from erp_simulation import story_replan

    module, erp, calls = story_lambda
    queue = list(outcomes)

    def replan(erp_conn, seed_conn, new_anchor, *, now, source, expected_anchor=None):
        calls["replan"].append({"new_anchor": new_anchor, "source": source, "expected_anchor": expected_anchor})
        outcome = queue.pop(0)
        if outcome.startswith("stale:"):
            erp.config["story_anchor_epoch"] = outcome.split(":", 1)[1]
            raise story_replan.StaleAnchor("the story clock was re-placed")
        return dict(_REPORT, new_anchor=int(new_anchor), source=source)

    monkeypatch.setattr(story_replan, "replan", replan)
    return module, erp, calls


def test_a_loop_replan_that_loses_the_cas_rereads_once_and_retries(story_lambda, monkeypatch, caplog):
    module, erp, calls = _stale_then(story_lambda, monkeypatch, ["stale:1789999810", "ok"])
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "placed"})
    out = module.lambda_handler(_update({"LoopSeconds": "10800"}, {}), None)
    assert [c["expected_anchor"] for c in calls["replan"]] == [1789990010.0, 1789999810.0]
    assert [c["new_anchor"] for c in calls["replan"]] == [1789990010.0, 1789999810.0]
    assert out["Data"]["AnchorEpoch"] == "1789999810" and calls["write"] == []
    assert "stale" in caplog.text.lower()


def test_a_loop_replan_that_loses_the_cas_twice_gives_up_with_a_logged_report(
        story_lambda, monkeypatch, caplog):
    """A concurrent placement owns the clock (and ran under the same new
    geometry): the Update reports the STORED clock and does not fail the stack."""
    module, erp, calls = _stale_then(story_lambda, monkeypatch, ["stale:1789999810", "stale:1790000110"])
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "placed",
                       "story_failed_run_number": "RUN-20260920-90001"})
    with caplog.at_level("WARNING"):
        out = module.lambda_handler(_update({"LoopSeconds": "10800"}, {}), None)
    assert len(calls["replan"]) == 2 and calls["write"] == []
    assert out["Data"] == {"AnchorEpoch": "1790000110", "AnchorSource": "placed",
                           "FailedRunNumber": "RUN-20260920-90001"}
    assert "gave up" in caplog.text and "1790000110" in caplog.text


def test_a_placement_that_changes_nothing_else_is_not_retried(story_lambda, monkeypatch):
    module, erp, calls = _stale_then(story_lambda, monkeypatch, ["ok"])
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "deploy"})
    module.lambda_handler(_update({"LoopSeconds": "10800"}, {}), None)
    assert len(calls["replan"]) == 1 and calls["replan"][0]["source"] == "deploy"


# ---------------------------------------------------------------------------
# ADR-0091 Design C: a loop re-plan records the reset default it planned onto
# ---------------------------------------------------------------------------

def _stored_default(erp):
    return erp.config.get("story_default_anchor"), erp.config.get("story_default_source")


@pytest.mark.parametrize("source", ["deploy", "packaged"])
def test_a_loop_replan_records_the_replanned_default(story_lambda, source):
    module, erp, _calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": source})
    module.lambda_handler(_update({"LoopSeconds": "10800"}, {}), None)
    assert _stored_default(erp) == (str(_REPORT["new_anchor"]), source)


def test_a_loop_replan_of_a_placed_clock_keeps_the_stored_default(story_lambda):
    module, erp, _calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "placed",
                       "story_default_anchor": "1789980010", "story_default_source": "deploy"})
    module.lambda_handler(_update({"LoopSeconds": "10800"}, {}), None)
    assert _stored_default(erp) == ("1789980010", "deploy")


def test_a_loop_replan_of_a_placed_clock_records_the_packaged_anchor(story_lambda):
    from erp_simulation import story_replan

    module, erp, _calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "placed"})
    module.lambda_handler(_update({"Anchor": "1789980010", "LoopSeconds": "10800"},
                                  {"Anchor": "1789980010"}), None)
    assert _stored_default(erp) == (str(story_replan.normalize_anchor(1789980010, _NOW)), "packaged")


# ---------------------------------------------------------------------------
# In-place upgrade: EVERY Update back-fills a missing default (fill-if-absent)
# ---------------------------------------------------------------------------

def test_an_unchanged_update_backfills_the_packaged_anchor_first(story_lambda, caplog):
    from erp_simulation import story_replan

    module, erp, calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "deploy"})
    with caplog.at_level("INFO"):
        module.lambda_handler(_update({"Anchor": "1789980010"}, {"Anchor": "1789980010"}), None)
    assert calls["replan"] == [] and len(calls["write"]) == 1
    assert _stored_default(erp) == (str(story_replan.normalize_anchor(1789980010, _NOW)), "packaged")
    assert "story default anchor recorded (backfill)" in caplog.text


@pytest.mark.parametrize("source", ["deploy", "packaged"])
def test_an_unchanged_update_backfills_the_stored_default_source_anchor(story_lambda, source):
    module, erp, _calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010.0", "story_anchor_source": source})
    module.lambda_handler(_update({"Days": "3", "Anchor": ""}, {"Days": "3", "Anchor": ""}), None)
    assert _stored_default(erp) == ("1789990010", source)


@pytest.mark.parametrize(("new", "old"), [
    ({"Days": "3"}, {"Days": "3"}),              # unchanged
    ({"LoopSeconds": "10800"}, {}),              # a loop re-plan of a placed clock records nothing itself
])
def test_an_update_of_a_placed_clock_backfills_the_deploy_time_default(story_lambda, new, old):
    from erp_simulation import condition_monitor, story_replan

    module, erp, _calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "placed"})
    module.lambda_handler(_update(new, old), None)
    deploy = condition_monitor.snap_anchor(_NOW, "down")
    assert deploy == story_replan.normalize_anchor(deploy, _NOW)     # the current loop, deploy-time phase
    assert _stored_default(erp) == (str(deploy), "deploy")


@pytest.mark.parametrize(("new", "old"), [
    ({"Days": "3"}, {"Days": "3"}),
    ({"Anchor": "1789980010"}, {"Anchor": "1789980010"}),
    ({"LoopSeconds": "10800"}, {}),
])
def test_an_update_never_overwrites_a_stored_default(story_lambda, caplog, new, old):
    module, erp, _calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "placed",
                       "story_default_anchor": "1789970010", "story_default_source": "deploy"})
    with caplog.at_level("INFO"):
        module.lambda_handler(_update(new, old), None)
    assert _stored_default(erp) == ("1789970010", "deploy")
    assert "(backfill)" not in caplog.text


def test_a_backfill_failure_is_logged_and_never_fails_the_stack(story_lambda, monkeypatch, caplog):
    from erp_simulation import story_replan

    module, erp, _calls = story_lambda
    erp.config.update({"story_anchor_epoch": "1789990010", "story_anchor_source": "deploy"})

    def boom(*_a, **_k):
        raise RuntimeError("deadlock on 10.0.0.5")
    monkeypatch.setattr(story_replan, "write_default_anchor", boom)
    with caplog.at_level("WARNING"):
        out = module.lambda_handler(_update({}, {}), None)
    assert out["Data"] == {"w": 1}
    assert "backfill FAILED (RuntimeError)" in caplog.text


def test_every_story_history_handler_function_is_under_50_lines():
    import ast

    for node in ast.walk(ast.parse(_LAMBDA_PATH.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno - node.lineno + 1 <= 50, node.name


# ---------------------------------------------------------------------------
# ADR-0092 decision 8: a live synth WITH an auto-place policy tells the
# simulation Lambda (yes/no, no minutes) to hold the story mill healthy
# until the first placement. Same predicate as the compute task's
# STORY_AUTO_PLACE_MINUTES (_workshop_story.auto_place_from_node), so the
# two can never disagree within one synth.
# ---------------------------------------------------------------------------

class _Parameter:
    def __init__(self, _scope, _construct_id, **kwargs):
        self.parameter_name = kwargs["parameter_name"]

    def grant_read(self, _function):
        pass

    def grant_write(self, _function):
        pass


class _Function:
    def __init__(self):
        self.env = {}

    def add_environment(self, key, value):
        self.env[key] = value


@pytest.mark.parametrize(("ctx", "held"), [
    ({**_LIVE_CTX, "story_auto_place_minutes": "80"}, True),
    ({**_LIVE_CTX, "story_auto_place_minutes": "5"}, True),
    ({**_LIVE_CTX, "story_auto_place_minutes": "0"}, False),
    ({**_LIVE_CTX, "story_auto_place_minutes": ""}, False),
    (_LIVE_CTX, False),
    ({**_LIVE_CTX, "story_anchor": str(PACKAGED_ANCHOR)}, False),
])
def test_the_feed_hold_rides_only_with_an_auto_place_policy(monkeypatch, ctx, held):
    pytest.importorskip("aws_cdk")
    from stacks import erp_lambda_stack as mod

    monkeypatch.setattr(mod.ssm, "StringParameter", _Parameter)
    fake = SimpleNamespace(node=_Node(**ctx), lambda_function=_Function())
    fake._create_story_backfill_mirror = lambda: mod.LambdaStack._create_story_backfill_mirror(fake)
    mod.LambdaStack._create_story_anchor_mirror(fake)
    expected = {"STORY_ANCHOR_PARAMETER": ANCHOR_PARAMETER}
    if held:
        expected["STORY_FEED_HOLD_UNTIL_PLACED"] = "1"
    # the one-time history replay is on by default for the live story (the replay ADR)
    expected["STORY_FEED_BACKFILL_PARAMETER"] = BACKFILL_PARAMETER
    assert fake.lambda_function.env == expected


def test_the_backfill_mirror_can_be_switched_off(monkeypatch):
    """With story_feed_backfill=false the Lambda gets no second parameter env."""
    pytest.importorskip("aws_cdk")
    from stacks import erp_lambda_stack as mod

    monkeypatch.setattr(mod.ssm, "StringParameter", _Parameter)
    fake = SimpleNamespace(node=_Node(**{**_LIVE_CTX, "story_feed_backfill": "false"}),
                           lambda_function=_Function())
    fake._create_story_backfill_mirror = lambda: mod.LambdaStack._create_story_backfill_mirror(fake)
    mod.LambdaStack._create_story_anchor_mirror(fake)
    assert "STORY_FEED_BACKFILL_PARAMETER" not in fake.lambda_function.env
    assert fake.lambda_function.env["STORY_ANCHOR_PARAMETER"] == ANCHOR_PARAMETER


@pytest.mark.parametrize(("ctx", "reserved"), [
    (_LIVE_CTX, True),
    ({**_LIVE_CTX, "story_auto_place_minutes": "80"}, True),
    ({**_LIVE_CTX, "story_auto_place_minutes": "0"}, True),
    ({**_LIVE_CTX, "story_anchor": str(PACKAGED_ANCHOR)}, True),
    ({}, False),
    ({"with_workshop": "true", "with_simulator": "true"}, False),
    ({"with_simulator": "true"}, False),
])
def test_the_story_line_is_reserved_on_every_live_synth_and_nowhere_else(ctx, reserved):
    """ADR-0085 amendment 2026-11-04: every live synth (with or without an
    auto-place policy or a packaged anchor) tells the simulation Lambda that
    the story machine's line runs only story work; frozen and non-workshop
    synths add nothing."""
    pytest.importorskip("aws_cdk")
    from stacks import erp_lambda_stack as mod

    fake = SimpleNamespace(node=_Node(**ctx), lambda_function=_Function())
    mod.LambdaStack._reserve_story_line(fake)
    assert fake.lambda_function.env == ({"STORY_LINE_RESERVED": "1"} if reserved else {})


def test_live_synth_reserves_the_story_line_on_the_simulation_lambda_only(live, frozen, simulator):
    _lid, fn = _function(live, SIMULATION_FUNCTION)
    assert fn["Environment"]["Variables"]["STORY_LINE_RESERVED"] == "1"
    assert "STORY_LINE_RESERVED" not in json.dumps(
        {lid: r for lid, r in _resources(live).items()
         if (r.get("Properties") or {}).get("FunctionName") != SIMULATION_FUNCTION})
    for synth in (frozen, simulator):
        assert "STORY_LINE_RESERVED" not in json.dumps(synth["template"])


@pytest.mark.parametrize("ctx", [{}, {"with_workshop": "true", "with_simulator": "true"}, {"with_simulator": "true"}])
def test_no_feed_hold_and_no_mirror_off_the_live_story(monkeypatch, ctx):
    pytest.importorskip("aws_cdk")
    from stacks import erp_lambda_stack as mod

    monkeypatch.setattr(mod.ssm, "StringParameter", lambda *_a, **_k: pytest.fail("no parameter off live"))
    fake = SimpleNamespace(node=_Node(**ctx), lambda_function=_Function())
    mod.LambdaStack._create_story_anchor_mirror(fake)
    assert fake.lambda_function.env == {}


# ---------------------------------------------------------------------------
# A direct ERP full reset (the dashboard's Reset All) re-runs the history CR.
# The history CR is a dependent stack's resource, so the Lambda stack
# registers its step in its own SSM registry parameter (the Tulip mock owns
# the first), and the database stack lets db-init read that parameter and
# invoke erp-story-history after a direct reset. Frozen / simulator synths
# register nothing (there is no story CR to re-run).
# ---------------------------------------------------------------------------

STORY_REGISTRY_PARAMETER = "/adt/erp-database/direct-reset-steps-story"
TULIP_REGISTRY_PARAMETER = "/adt/erp-database/direct-reset-steps"


@pytest.fixture(scope="module")
def live_tulip(tmp_path_factory):
    """A live workshop WITH the optional plant on: both the Tulip mock and the
    story register a direct-reset step, so db-init reads both parameters."""
    return _synth(tmp_path_factory, "live_tulip", (
        "-c", "with_workshop=true", "-c", "with_simulator=true", "-c", "workshop_story=live", "-c", "tulip_enabled=true"))


def _ssm_parameters(synth: dict) -> dict:
    return _of_type(synth, "AWS::SSM::Parameter")


def _story_registry_param(synth: dict):
    for lid, res in _ssm_parameters(synth).items():
        if res["Properties"].get("Name") == STORY_REGISTRY_PARAMETER:
            return lid, res["Properties"]
    return None, None


def test_live_registers_the_history_step_in_its_own_registry_parameter(live):
    """The Lambda stack writes a direct-reset step to the story registry
    parameter: a no-change Update of erp-story-history (the step carries its
    physical id and the deployed properties, not a fixed Payload)."""
    lid, props = _story_registry_param(live)
    assert lid is not None, "live synth has no story direct-reset registry parameter"
    value = _render(props["Value"])  # Fn::Join of literal JSON and CloudFormation tokens
    assert "the live workshop story history" in value
    assert "adt-story-history" in value
    assert '"TimeoutSeconds":600' in value.replace(" ", "")
    assert "ResourceProperties" in value and "Payload" not in value
    assert '"ServiceToken"' in value and '"Days":"3"' in value.replace(" ", "")


@pytest.mark.parametrize("name", ["frozen", "simulator"])
def test_no_story_registry_parameter_outside_the_live_story(name, request):
    synth = request.getfixturevalue(name)
    assert _story_registry_param(synth) == (None, None)


def _db_init_environment(synth: dict) -> dict:
    assert synth["database"] is not None, f"{DATABASE_STACK} template not synthesized"
    for res in synth["database"]["Resources"].values():
        if res["Type"] == "AWS::Lambda::Function" and \
                res["Properties"].get("FunctionName") == "erp-database-init":
            return res["Properties"]["Environment"]["Variables"]
    pytest.fail("erp-database-init function not found in the database template")


def _registry_names(env: dict) -> list[str]:
    """The registry parameter name(s) in the db-init env: one bare name, or a
    JSON list of names when more than one stack registers."""
    registry = env.get("DB_DIRECT_RESET_REGISTRY")
    if registry is None:
        return []
    return json.loads(registry) if isinstance(registry, str) and registry.startswith("[") else [registry]


def test_live_db_init_lists_the_story_registry_parameter(live):
    """A live workshop (no optional plant) registers only the story step, so
    db-init's registry env names the story parameter."""
    names = _registry_names(_db_init_environment(live))
    assert STORY_REGISTRY_PARAMETER in names


def test_live_with_the_plant_lists_the_story_registry_parameter(live_tulip):
    """With the plant on in the live story, db-init's registry env names the
    story parameter (a JSON list)."""
    names = _registry_names(_db_init_environment(live_tulip))
    assert STORY_REGISTRY_PARAMETER in names


def test_live_db_init_may_invoke_the_story_function(live):
    """db-init's role may invoke erp-story-history by its fixed name (one
    named function, nothing wider) and read the story registry parameter."""
    db = live["database"]
    policy = json.dumps(db["Resources"])
    assert "erp-story-history" in policy
    assert STORY_REGISTRY_PARAMETER in policy


@pytest.mark.parametrize("name", ["frozen", "simulator"])
def test_db_init_registry_has_no_story_parameter_outside_live(name, request):
    synth = request.getfixturevalue(name)
    if synth["database"] is None:
        pytest.skip(f"{name} synth did not emit the database template")
    assert STORY_REGISTRY_PARAMETER not in _registry_names(_db_init_environment(synth))


def test_the_database_stack_pins_the_story_function_name():
    """The database stack names erp-story-history by the SAME constant the
    Lambda stack uses, so db-init's invoke grant and the registered step
    agree (the Tulip-mock-setup-function precedent)."""
    from stacks import erp_database_stack, erp_lambda_stack

    assert erp_database_stack.STORY_HISTORY_SETUP_FUNCTION == erp_lambda_stack.STORY_HISTORY_FUNCTION
