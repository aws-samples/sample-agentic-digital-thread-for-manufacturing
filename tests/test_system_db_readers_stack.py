# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Wiring of the system agents' read-only database logins (stacks/_db_readers.py).

* Each demo database gets one generated reader secret per schema it carries
  (the seed SQL's schemas; the ERP view schemas) and one setup custom resource
  that runs after the database's own seed / initialisation and again whenever
  that re-runs.
* The app task reads exactly those secrets and gets ``{schema: ARN}`` maps in
  its env (agents/demo/db_routing.py); the setup function reads the owner
  secret and the readers only.
* The optional Tulip plant's reader secret is kept while the plant is off,
  with no login: the setup resource does not list it, and neither the setup
  function nor the app task can read it.
* The AgentCore runtime role reads no database owner secret and no reader
  secret: a system runtime gets its schema's login in its agent-config secret.

Offline parity checks run without synth; one credential-blind synth
(``-c with_simulator=true``, Docker stubbed) covers the templates and skips
without the vendored CDK CLI.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
SEED, ERP, COMPUTE, AGENTCORE = ("ADTSeedDataStack", "ADTERPDatabaseStack", "ADTComputeStack",
                                 "ADTAgentCoreStack")
APP_DIR = REPO_ROOT / "agentic_digital_thread"

for path in (REPO_ROOT, APP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

pytest.importorskip("aws_cdk")


# --------------------------------------------------------------------------
# Offline: which schemas get a reader
# --------------------------------------------------------------------------

def test_every_seed_schema_gets_a_reader():
    from stacks.adt_seed_data_stack import SEED_READER_SCHEMAS

    created = set()
    for path in (REPO_ROOT / "lambda-adt-seed").glob("*.sql"):
        created |= set(re.findall(r"CREATE SCHEMA IF NOT EXISTS (\w+)", path.read_text(encoding="utf-8")))
    assert set(SEED_READER_SCHEMAS) == created - {"public"}
    assert len(set(SEED_READER_SCHEMAS)) == len(SEED_READER_SCHEMAS)


def test_every_erp_view_schema_gets_a_reader():
    from agents.demo.site_views import SITE_MES_VIEWS
    from stacks.erp_database_stack import ERP_READER_SCHEMAS

    assert set(ERP_READER_SCHEMAS) == {"erp_data"} | {schema for schema, _s, _l in SITE_MES_VIEWS}
    handler = (REPO_ROOT / "lambda-db-init" / "handler.py").read_text(encoding="utf-8")
    for schema in ERP_READER_SCHEMAS:
        assert f"'{schema}'" in handler or f"{schema}." in handler, schema


def test_every_registered_demo_sql_system_has_a_reader_on_its_database():
    from agents.demo import workshop
    from agents.demo.db_routing import is_erp_schema
    from agents.demo.systems import DEMO_SYSTEMS
    from stacks.adt_seed_data_stack import SEED_READER_SCHEMAS
    from stacks.erp_database_stack import ERP_READER_SCHEMAS

    systems = list(DEMO_SYSTEMS)
    for name in dir(workshop):
        value = getattr(workshop, name)
        if isinstance(value, (list, tuple)):
            systems += [s for s in value if isinstance(s, dict) and "schema" in s]
    schemas = {s["schema"] for s in systems
               if s.get("connection_type", "PostgreSQL") == "PostgreSQL" and s.get("schema")}
    assert schemas, "no demo SQL systems found"
    for schema in schemas:
        readers = ERP_READER_SCHEMAS if is_erp_schema(schema) else SEED_READER_SCHEMAS
        assert schema in readers, schema


def test_the_app_and_the_stacks_spell_the_reader_login_alike():
    """The upgrade audit (scripts/audit_runtime_env.py --reader-logins) names a
    system's reader by the app-side prefix; the stacks create the logins."""
    from agents.demo.db_routing import READER_LOGIN_PREFIX
    from stacks._db_readers import READER_ROLE_PREFIX

    assert READER_LOGIN_PREFIX == READER_ROLE_PREFIX


def test_reader_role_names_are_valid_postgres_identifiers():
    from stacks._db_readers import READER_ROLE_PREFIX, reader_role_name
    from stacks.adt_seed_data_stack import SEED_READER_SCHEMAS
    from stacks.erp_database_stack import ERP_READER_SCHEMAS

    for schema in SEED_READER_SCHEMAS + ERP_READER_SCHEMAS:
        role = reader_role_name(schema)
        assert role == READER_ROLE_PREFIX + schema and len(role) <= 63
        assert re.fullmatch(r"[a-z_][a-z0-9_]*", role)
    for bad in ("Bad", "x;y", "a" * 60):
        with pytest.raises(ValueError):
            reader_role_name(bad)


# --------------------------------------------------------------------------
# One synth
# --------------------------------------------------------------------------

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


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    cdk_bin = str(CDK_BIN) if CDK_BIN.exists() else shutil.which("cdk")
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")
    out_dir = tmp_path_factory.mktemp("cdk_synth_system_readers")
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "CDK_DOCKER": str(_offline_docker_stub(out_dir)),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN", "CDK_DEFAULT_ACCOUNT"):
        env.pop(key, None)
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [cdk_bin, "synth", SEED, "-c", "region=us-east-1", "-c", "cdk_nag_disabled=true",
         "-c", "with_simulator=true", "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=900,
    )
    paths = {name: out_dir / f"{name}.template.json" for name in (SEED, ERP, COMPUTE, AGENTCORE)}
    if result.returncode != 0 or not all(p.exists() for p in paths.values()):
        pytest.fail(f"synth did not produce the templates:\n{result.stderr[-4000:]}")
    return {name: json.loads(p.read_text()) for name, p in paths.items()}


def _resources(template, kind):
    return {lid: r for lid, r in template["Resources"].items() if r["Type"] == kind}


def _reader_secrets(template, prefix):
    return {lid: r for lid, r in _resources(template, "AWS::SecretsManager::Secret").items()
            if lid.startswith(prefix)}


def _reader_resource(template, prefix):
    (found,) = [r for lid, r in _resources(template, "AWS::CloudFormation::CustomResource").items()
                if lid.startswith(prefix)]
    return found


def _statements(template, role_filter=None):
    """(role refs, statement) for every IAM statement in the template."""
    out = []
    for lid, res in template["Resources"].items():
        if res["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            roles = res["Properties"].get("Roles", [])
            for st in res["Properties"]["PolicyDocument"]["Statement"]:
                out.append((roles, st))
        elif res["Type"] == "AWS::IAM::Role":
            for policy in res["Properties"].get("Policies", []) or []:
                for st in policy["PolicyDocument"]["Statement"]:
                    out.append(([{"Ref": lid}], st))
    return out


def _as_list(value):
    return value if isinstance(value, list) else [value]


#: Schemas whose reader secret is kept with no login in this synth (the
#: optional Tulip plant's, off here; stacks/erp_database_stack.py).
_SECRET_ONLY = {SEED: (), ERP: ()}


@pytest.mark.parametrize("stack, prefix, schemas_attr, after", [
    (SEED, "SeedDbReaders", "stacks.adt_seed_data_stack:SEED_READER_SCHEMAS", "AdtSeedResource"),
    (ERP, "ErpDbReaders", "stacks.erp_database_stack:ERP_READER_SCHEMAS", "DatabaseInitResource"),
])
def test_one_generated_reader_secret_per_schema_and_one_setup_resource(templates, stack, prefix,
                                                                      schemas_attr, after):
    import importlib

    from stacks._db_readers import db_reader_version, reader_role_name

    module, attr = schemas_attr.split(":")
    schemas = getattr(importlib.import_module(module), attr)
    template = templates[stack]
    secrets = _reader_secrets(template, prefix)
    assert len(secrets) == len(schemas) + len(_SECRET_ONLY[stack])
    by_username = {}
    for lid, secret in secrets.items():
        props = secret["Properties"]
        gen = props["GenerateSecretString"]
        by_username[json.loads(gen["SecretStringTemplate"])["username"]] = lid
        assert gen["GenerateStringKey"] == "password" and gen["ExcludePunctuation"] is True
        assert "Name" not in props, "read by ARN; a fixed name could fall under another grant"
        # No role's name wildcard (adt*, AdtDbSecret*, DatabaseSecret*) matches the generated name.
        assert not lid.lower().startswith("adt") and not lid.startswith("DatabaseSecret"), lid
    assert set(by_username) == {reader_role_name(s) for s in schemas + _SECRET_ONLY[stack]}
    resource = _reader_resource(template, prefix)
    props = resource["Properties"]
    assert props["Version"] == db_reader_version()
    assert [(r["Schema"], r["Role"], r["SecretArn"]) for r in props["Readers"]] == [
        (s, reader_role_name(s), {"Ref": by_username[reader_role_name(s)]}) for s in schemas]
    depends = resource.get("DependsOn", [])
    assert any(d.startswith(after) for d in _as_list(depends)), depends


def test_the_seed_readers_re_run_with_the_seed(templates):
    from stacks.adt_seed_data_stack import adt_seed_version

    assert _reader_resource(templates[SEED], "SeedDbReaders")["Properties"]["DataVersion"] == \
        adt_seed_version()


def test_the_erp_readers_re_run_with_db_init(templates):
    from stacks.erp_database_stack import db_init_seed_version, db_init_trigger

    props = _reader_resource(templates[ERP], "ErpDbReaders")["Properties"]
    assert props["DataVersion"].startswith(db_init_trigger("erp_simulation") + "|")
    assert props["DataVersion"].endswith("|" + db_init_seed_version())


@pytest.mark.parametrize("stack, prefix, owner", [
    (SEED, "SeedDbReaders", "AdtDbSecret"), (ERP, "ErpDbReaders", "DatabaseSecret"),
])
def test_the_setup_function_reads_the_owner_and_reader_secrets_only(templates, stack, prefix, owner):
    template = templates[stack]
    # The secrets the setup resource lists (a secret kept with no login is not one).
    readers = {repr(r["SecretArn"]) for r in _reader_resource(template, prefix)["Properties"]["Readers"]}
    (fn_role,) = [lid for lid in _resources(template, "AWS::IAM::Role")
                  if lid.startswith(prefix) and "SetupFn" in lid]
    read = set()
    for roles, st in _statements(template):
        if {"Ref": fn_role} in roles and "secretsmanager:GetSecretValue" in _as_list(st["Action"]):
            read |= {repr(r) for r in _as_list(st["Resource"])}
    assert readers <= read
    owners = {r for r in read if owner in r}
    assert len(owners) == 1 and read == readers | owners, read


def _app_env(template):
    (taskdef,) = [r for r in _resources(template, "AWS::ECS::TaskDefinition").values()
                  if any(c.get("Name") == "adt-app" for c in r["Properties"]["ContainerDefinitions"])]
    (container,) = [c for c in taskdef["Properties"]["ContainerDefinitions"] if c["Name"] == "adt-app"]
    return {e["Name"]: e["Value"] for e in container.get("Environment", [])}


@pytest.mark.parametrize("name, schemas_attr", [
    ("DB_READER_SECRET_ARNS", "stacks.adt_seed_data_stack:SEED_READER_SCHEMAS"),
    ("ERP_DB_READER_SECRET_ARNS", "stacks.erp_database_stack:ERP_READER_SCHEMAS"),
])
def test_the_app_gets_a_schema_to_reader_secret_map(templates, name, schemas_attr):
    import importlib

    module, attr = schemas_attr.split(":")
    schemas = getattr(importlib.import_module(module), attr)
    value = json.dumps(_app_env(templates[COMPUTE])[name])
    for schema in schemas:
        assert f'\\"{schema}\\":' in value or f'"{schema}":' in value, schema
    assert value.count("Fn::ImportValue") == len(schemas)


def test_the_app_task_role_reads_every_reader_secret(templates):
    template = templates[COMPUTE]
    imports = set()
    for _roles, st in _statements(template):
        if "secretsmanager:GetSecretValue" in _as_list(st["Action"]):
            for res in _as_list(st["Resource"]):
                text = json.dumps(res)
                if "Readers" in text and "Fn::ImportValue" in text:
                    imports.add(text)
    from stacks.adt_seed_data_stack import SEED_READER_SCHEMAS
    from stacks.erp_database_stack import ERP_READER_SCHEMAS
    assert len(imports) == len(SEED_READER_SCHEMAS) + len(ERP_READER_SCHEMAS)


def test_the_kept_reader_secret_is_read_by_no_one(templates):
    """With the plant off, nothing in the app or the runtimes names its
    reader secret: no import, no grant, no env entry."""
    for stack in (COMPUTE, AGENTCORE):
        assert "ErpDbReadersReaderMesTulip" not in json.dumps(templates[stack]), stack


def test_the_runtime_role_reads_no_database_login_secret(templates):
    """System runtimes connect with the login in their agent-config secret;
    the shared runtime role can read neither a database owner secret nor a
    reader secret."""
    template = templates[AGENTCORE]
    for _roles, st in _statements(template):
        if "secretsmanager:GetSecretValue" not in _as_list(st["Action"]):
            continue
        for res in _as_list(st["Resource"]):
            text = json.dumps(res)
            for name in ("AdtDbSecret", "DatabaseSecret", "SeedDbReaders", "ErpDbReaders"):
                assert f"secret:{name}" not in text, text
            assert ":secret:*" not in text, text


def _db_init_handler():
    """lambda-db-init/handler.py (boto3 stands in when not installed; the
    handler imports its PostgreSQL client only when it connects)."""
    import importlib.util
    import types

    stand_ins = [name for name in ("boto3",) if importlib.util.find_spec(name) is None]
    for name in stand_ins:
        sys.modules[name] = types.ModuleType(name)
    try:
        spec = importlib.util.spec_from_file_location("db_init_for_reader_stack",
                                                      REPO_ROOT / "lambda-db-init" / "handler.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name in stand_ins:
            del sys.modules[name]


def _function(template, predicate):
    (found,) = [(lid, r) for lid, r in _resources(template, "AWS::Lambda::Function").items()
                if predicate(lid, r["Properties"])]
    return found


def _with_markers(value):
    """``value`` with every intrinsic (Ref, Fn::GetAtt, ...) replaced by a
    quote-free marker naming it, so a JSON string built from tokens and the
    properties it came from compare as plain JSON."""
    import hashlib

    if isinstance(value, dict) and len(value) == 1 and next(iter(value)).startswith(("Ref", "Fn::")):
        return "@@" + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() + "@@"
    if isinstance(value, dict):
        return {k: _with_markers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_with_markers(v) for v in value]
    return value


def _rendered(value, intrinsic):
    """A template string value, each intrinsic in it written by ``intrinsic``."""
    if isinstance(value, str):
        return value
    if set(value) == {"Fn::Join"}:
        separator, parts = value["Fn::Join"]
        return separator.join(_rendered(part, intrinsic) for part in parts)
    return intrinsic(value)


#: Where db-init's step and registry settings live (stacks/_direct_reset.py
#: and lambda-db-init/handler.py read the same names; pinned in
#: tests/test_db_init_reader_resync.py).
_STEPS_ENV, _REGISTRY_ENV = "DB_DIRECT_RESET_STEPS", "DB_DIRECT_RESET_REGISTRY"


def _init_and_setup(template):
    _lid, init = _function(template, lambda _l, p: p.get("FunctionName") == "erp-database-init")
    setup_id, setup = _function(template, lambda lid, _p: lid.startswith("ErpDbReaders") and "SetupFn" in lid)
    return init, setup_id, setup


def _dashboard_reset_wait() -> int:
    """The ERP dashboard's wait on its destructive invokes, read from its
    source (importing it needs streamlit)."""
    source = (REPO_ROOT / "erp_simulation" / "streamlit_dashboard.py").read_text(encoding="utf-8")
    match = re.search(r"^RESET_INVOKE_READ_TIMEOUT_SECONDS = (\d+)$", source, re.M)
    assert match, "the dashboard names no RESET_INVOKE_READ_TIMEOUT_SECONDS"
    return int(match.group(1))


def test_the_erp_init_function_re_runs_the_reader_setup_after_a_direct_reset(templates):
    """db-init also resets the ERP database outside CloudFormation (the
    dashboard's Reset All, a hand invoke), which drops the readers' grants,
    so it runs the reader setup itself afterwards (lambda-db-init/handler.py
    run_direct_reset_steps). Its steps name the setup function, carry its
    timeout and the reader custom resource's properties exactly as
    CloudFormation sends them, ServiceToken included; without the optional
    plant no dependent stack registers steps, so it reads no registry; the
    whole environment fits Lambda's 4 KB limit even with every reference 256
    characters long; and its timeouts fit (_assert_the_waits_fit)."""
    template = templates[ERP]
    init, setup_id, setup = _init_and_setup(template)
    variables = init["Properties"]["Environment"]["Variables"]
    assert _STEPS_ENV in variables, sorted(variables)
    steps = json.loads(_rendered(variables[_STEPS_ENV], _with_markers))
    assert steps == [_with_markers({
        "Name": "the system agents' read-only logins", "FunctionName": {"Ref": setup_id},
        "TimeoutSeconds": setup["Properties"]["Timeout"], "PhysicalResourceId": "adt-system-db-readers",
        "ResourceProperties": _reader_resource(template, "ErpDbReaders")["Properties"]})]
    assert _REGISTRY_ENV not in variables
    longest = sum(len(name) + len(_rendered(value, lambda _v: "x" * 256)) for name, value in variables.items())
    assert longest < 4096, longest
    _assert_the_waits_fit(template, init, [setup["Properties"]["Timeout"]])


#: What the stack leaves db-init for reading its steps and the reset itself,
#: before the first step: 30 times the 4.0 s a live reset took.
RESET_ALLOWANCE_SECONDS = 120


def _assert_the_waits_fit(template, init, step_timeouts):
    """db-init's timeouts, against each other. db-init never waits past its
    own deadline whatever the step timeouts add up to (its waits follow the
    time left: tests/test_db_init_reader_resync.py); here the sum fits too:
    the reset allowance, every step's full wait (its own timeout, the margin
    and the connect time) and the reserve fit db-init's timeout, so only a
    reset slower than the allowance can leave a step less than its own
    timeout. db-init answers well inside the 15-minute provider framework
    function that runs it for CloudFormation, and the dashboard waits longer
    than db-init can run."""
    handler = _db_init_handler()
    timeout = init["Properties"]["Timeout"]
    full = sum(t + handler.STEP_MARGIN_SECONDS + handler.STEP_CONNECT_SECONDS for t in step_timeouts)
    assert RESET_ALLOWANCE_SECONDS + full + handler.DEADLINE_RESERVE_SECONDS <= timeout, (step_timeouts, timeout)
    _lid, framework = _function(template, lambda lid, _p: lid.startswith("DatabaseInitProviderframeworkonEvent"))
    assert framework["Properties"]["Timeout"] - timeout >= 60
    assert timeout < _dashboard_reset_wait()


def test_the_erp_init_function_may_invoke_its_steps_only(templates):
    """Least privilege: the db-init role's one Lambda statement allows
    lambda:InvokeFunction on the setup function's own ARN, nothing wider,
    and without the optional plant it reads no SSM parameter."""
    template = templates[ERP]
    init, setup_id, _setup = _init_and_setup(template)
    role = {"Ref": init["Properties"]["Role"]["Fn::GetAtt"][0]}
    statements = [st for roles, st in _statements(template) if role in roles]

    def using(prefix):
        return [(_as_list(st["Action"]), _as_list(st["Resource"]), st["Effect"], st.get("Condition"))
                for st in statements if any(a.startswith(prefix) or a == "*" for a in _as_list(st["Action"]))]
    assert using("lambda:") == [
        (["lambda:InvokeFunction"], [{"Fn::GetAtt": [setup_id, "Arn"]}], "Allow", None)]
    assert using("ssm:") == []


def _checkov_skip_ids(resource):
    skips = ((resource.get("Metadata") or {}).get("checkov") or {}).get("skip") or []
    assert all(len(s.get("comment") or "") > 40 for s in skips), skips   # a written reason each
    return {s["id"] for s in skips}


@pytest.mark.parametrize("stack, prefix", [(SEED, "SeedDbReaders"), (ERP, "ErpDbReaders")])
def test_the_reader_resources_add_no_stock_checkov_failure(templates, stack, prefix):
    """Zero new checkov failures: every finding the reader resources would
    add is either avoided (no log-retention custom resource) or skipped with a
    written reason, as the erp_console login secret does."""
    template = templates[stack]
    resources = template["Resources"]
    assert not [lid for lid in resources if lid.startswith("LogRetention")], \
        "log_retention adds a CDK log-retention Lambda and its IAM policy"
    for lid in _reader_secrets(template, prefix):
        assert "CKV_AWS_149" in _checkov_skip_ids(resources[lid]), lid
    functions = {lid: r for lid, r in _resources(template, "AWS::Lambda::Function").items()
                 if lid.startswith(prefix)}
    (setup,) = [lid for lid in functions if "SetupFn" in lid]
    (framework,) = [lid for lid in functions if "framework" in lid]
    assert {"CKV_AWS_115", "CKV_AWS_116"} <= _checkov_skip_ids(functions[setup])
    assert {"CKV_AWS_115", "CKV_AWS_116", "CKV_AWS_117", "CKV_AWS_173"} <= \
        _checkov_skip_ids(functions[framework])
