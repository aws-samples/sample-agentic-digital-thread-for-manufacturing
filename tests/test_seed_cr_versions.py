# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The two seed custom resources re-run when the seed they apply changes.

CloudFormation re-invokes a custom resource only when its properties change,
so each carries a content hash of what it seeds:

* ``AdtSeedResource`` (ADTSeedDataStack): ``Version`` =
  ``adt_seed_version()``, sha256 over (file name, bytes) of every
  ``lambda-adt-seed/*.sql``, hex[:16]. The files are re-runnable on an
  existing schema (tests/test_adt_seed_rerun_idempotent.py), so an Update
  lands new seed rows and changes nothing else. The function behind it gets
  the same value as ``ADT_SEED_VERSION``, so it can tell its own version from
  the previous one an update rollback asks for.
* ``DatabaseInitResource`` (ADTERPDatabaseStack): ``SeedVersion`` =
  ``db_init_seed_version()``, the same hash over ``lambda-db-init/handler.py``
  (the catalogue lives inline in it). A SeedVersion-only Update runs the
  handler's ADDITIVE catalogue upsert, never the drop-and-reseed, so it is
  deliberately NOT part of ``db_init_trigger()`` (the live-story history CR's
  re-run token for a reset). The function behind it gets the same value as
  ``DB_INIT_SEED_VERSION``, so it can tell its own version from the previous
  one an update rollback asks for, and ``DB_INIT_TIMESTAMP`` (with
  ``DB_NAME``), so it can tell the same for a full-reset Update.

One offline synth (``-c with_simulator=true``, Docker stubbed) for both
templates; skips without the vendored CDK CLI.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
SEED_STACK = "ADTSeedDataStack"
DATABASE_STACK = "ADTERPDatabaseStack"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("aws_cdk")


def _expected(root: Path, rels) -> str:
    """Independent re-derivation of the documented hash contract."""
    digest = hashlib.sha256()
    for rel in rels:
        digest.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def _sql_rels(root: Path) -> list:
    return sorted(f"lambda-adt-seed/{p.name}" for p in (root / "lambda-adt-seed").glob("*.sql"))


# --------------------------------------------------------------------------
# Offline (no synth)
# --------------------------------------------------------------------------

def test_adt_seed_version_is_the_sql_content_hash():
    from stacks import adt_seed_data_stack as stack

    rels = _sql_rels(REPO_ROOT)
    assert len(rels) >= 10 and "lambda-adt-seed/003_mes_line3.sql" in rels
    assert stack.adt_seed_version() == _expected(REPO_ROOT, rels)


def _copy(tmp_path: Path, rels) -> None:
    for rel in rels:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((REPO_ROOT / rel).read_bytes())


def test_adt_seed_version_follows_every_sql_file_and_ignores_the_handler(tmp_path, monkeypatch):
    from stacks import adt_seed_data_stack as stack

    rels = _sql_rels(REPO_ROOT)
    _copy(tmp_path, rels + ["lambda-adt-seed/handler.py"])
    monkeypatch.setattr(stack, "REPO_ROOT", tmp_path)
    seen = {stack.adt_seed_version()}
    for rel in rels:
        with (tmp_path / rel).open("ab") as fh:
            fh.write(b"\n-- changed\n")
        seen.add(stack.adt_seed_version())
    assert len(seen) == len(rels) + 1
    (tmp_path / "lambda-adt-seed" / "012_new.sql").write_text("SELECT 1;\n")
    assert stack.adt_seed_version() not in seen, "a new seed file re-runs the seed"
    before = stack.adt_seed_version()
    (tmp_path / "lambda-adt-seed" / "handler.py").write_text("# changed\n")
    assert stack.adt_seed_version() == before


def test_adt_seed_version_fails_loud_without_sql(tmp_path, monkeypatch):
    from stacks import adt_seed_data_stack as stack

    (tmp_path / "lambda-adt-seed").mkdir()
    monkeypatch.setattr(stack, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        stack.adt_seed_version()


def test_db_init_seed_version_is_the_handler_content_hash(tmp_path, monkeypatch):
    from stacks import erp_database_stack as stack

    rel = "lambda-db-init/handler.py"
    assert stack.DB_INIT_SEED_SOURCES == (rel,)
    assert stack.db_init_seed_version() == _expected(REPO_ROOT, [rel])
    _copy(tmp_path, [rel])
    monkeypatch.setattr(stack, "REPO_ROOT", tmp_path)
    before = stack.db_init_seed_version()
    with (tmp_path / rel).open("ab") as fh:
        fh.write(b"\n# changed\n")
    assert stack.db_init_seed_version() != before
    (tmp_path / rel).unlink()
    with pytest.raises(FileNotFoundError):
        stack.db_init_seed_version()


def test_seed_version_is_not_part_of_the_reset_trigger():
    """db_init_trigger() (the history CR's reset token) is Timestamp|name
    only: a catalogue upsert must not rewrite the live-story history."""
    from stacks import erp_database_stack as stack

    assert stack.db_init_trigger("erp_simulation") == f"{stack.DB_INIT_TIMESTAMP}|erp_simulation"


# --------------------------------------------------------------------------
# One synth: both CR property sets
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
    out_dir = tmp_path_factory.mktemp("cdk_synth_seed_versions")
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CDK_DOCKER": str(_offline_docker_stub(out_dir)),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [cdk_bin, "synth", SEED_STACK, "-c", "region=us-east-1", "-c", "cdk_nag_disabled=true",
         "-c", "with_simulator=true", "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=900,
    )
    paths = {name: out_dir / f"{name}.template.json" for name in (SEED_STACK, DATABASE_STACK)}
    if result.returncode != 0 or not all(p.exists() for p in paths.values()):
        pytest.fail(f"synth did not produce both templates:\n{result.stderr[-4000:]}")
    return {name: json.loads(p.read_text()) for name, p in paths.items()}


def _custom_resource(template: dict, prefix: str) -> dict:
    (props,) = [r["Properties"] for lid, r in template["Resources"].items()
                if lid.startswith(prefix) and r["Type"] == "AWS::CloudFormation::CustomResource"]
    return props


def test_seed_resource_carries_the_sql_version(templates):
    from stacks import adt_seed_data_stack as stack

    props = _custom_resource(templates[SEED_STACK], "AdtSeedResource")
    assert props["Version"] == stack.adt_seed_version()
    assert set(props) == {"ServiceToken", "Version"}


def test_db_init_resource_carries_the_seed_version(templates):
    from stacks import erp_database_stack as stack

    props = _custom_resource(templates[DATABASE_STACK], "DatabaseInitResource")
    assert props["SeedVersion"] == stack.db_init_seed_version()
    assert props["Timestamp"] == stack.DB_INIT_TIMESTAMP
    assert {"ClusterEndpoint", "SecretArn", "DatabaseName"} <= set(props)


def _get_att_arn(template: dict, value) -> dict:
    """The resource an ``{"Fn::GetAtt": [<id>, "Arn"]}`` points at."""
    lid, attribute = value["Fn::GetAtt"]
    assert attribute == "Arn"
    return template["Resources"][lid]


def _db_init_function_variables(template: dict) -> dict:
    """The environment of the function behind DatabaseInitResource
    (ServiceToken -> the Provider's framework onEvent function ->
    USER_ON_EVENT_FUNCTION_ARN)."""
    props = _custom_resource(template, "DatabaseInitResource")
    framework = _get_att_arn(template, props["ServiceToken"])
    function = _get_att_arn(template, framework["Properties"]["Environment"]["Variables"]
                            ["USER_ON_EVENT_FUNCTION_ARN"])
    assert function["Type"] == "AWS::Lambda::Function"
    assert function["Properties"]["FunctionName"] == "erp-database-init"
    return function["Properties"]["Environment"]["Variables"]


def test_db_init_function_knows_the_seed_version_of_its_own_code(templates):
    """The function behind DatabaseInitResource gets DB_INIT_SEED_VERSION,
    equal to the SeedVersion the custom resource carries:
    lambda-db-init/handler.py runs a seed-only Update only for its own
    version, so an update rollback that reaches the new code is a no-op."""
    from stacks import erp_database_stack as stack

    template = templates[DATABASE_STACK]
    props = _custom_resource(template, "DatabaseInitResource")
    variables = _db_init_function_variables(template)
    assert variables["DB_INIT_SEED_VERSION"] == props["SeedVersion"] == stack.db_init_seed_version()


def _load_db_init_handler():
    spec = importlib.util.spec_from_file_location(
        "db_init_handler_cr_versions", REPO_ROOT / "lambda-db-init" / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_db_init_function_knows_what_a_full_reset_of_its_own_code_deploys(templates):
    """The function gets DB_INIT_TIMESTAMP equal to the custom resource's
    Timestamp, and every property of the custom resource but ServiceToken
    (and the seed-only SeedVersion, which is pinned anyway) is one the
    handler pins for a full reset (FULL_RESET_PINS), each equal to the
    variable the handler reads it from: so a forward full-reset Update always
    runs and the rollback of a failed one that reaches the new code is
    skipped, whichever property it changes (lambda-db-init/handler.py). A
    property added to the custom resource without a pin fails here."""
    from stacks import erp_database_stack as stack

    template = templates[DATABASE_STACK]
    props = _custom_resource(template, "DatabaseInitResource")
    variables = _db_init_function_variables(template)
    assert variables["DB_INIT_TIMESTAMP"] == props["Timestamp"] == stack.DB_INIT_TIMESTAMP
    assert variables["DB_NAME"] == props["DatabaseName"]
    handler = _load_db_init_handler()
    pinned = {prop for prop, _env in handler.FULL_RESET_PINS}
    carried = set(props) - {"ServiceToken"}
    assert carried - handler.SEED_ONLY_UPDATE_PROPERTIES <= pinned <= carried
    for prop, env in handler.FULL_RESET_PINS:
        assert variables[env] == props[prop], (prop, env)


def test_db_init_pins_the_cluster_and_secret_as_the_same_expressions(templates):
    """ClusterEndpoint and SecretArn are tokens: the custom resource and the
    function's environment carry the same intrinsic (the cluster's
    Endpoint.Address, the secret's Ref), so CloudFormation resolves both to
    the same string."""
    template = templates[DATABASE_STACK]
    props = _custom_resource(template, "DatabaseInitResource")
    variables = _db_init_function_variables(template)
    (cluster,) = [lid for lid, r in template["Resources"].items() if r["Type"] == "AWS::RDS::DBCluster"]
    assert props["ClusterEndpoint"] == variables["DB_CLUSTER_ENDPOINT"] == {
        "Fn::GetAtt": [cluster, "Endpoint.Address"]}
    secret = props["SecretArn"]["Ref"]
    assert template["Resources"][secret]["Type"] == "AWS::SecretsManager::Secret"
    assert variables["DB_SECRET_ARN"] == props["SecretArn"] == {"Ref": secret}


def test_seed_rds_uses_an_orderable_instance_class(templates):
    """The seed RDS instance is db.t4g.small, never db.t4g.micro.

    On 2026-10-02 RDS stopped offering db.t4g.micro in every us-east-1 AZ
    (``DescribeOrderableDBInstanceOptions`` returned nothing for any engine,
    and CreateDBInstance failed for lack of AZ capacity), which rolled back a
    fresh deploy. The workshop package deploys only to us-east-1.
    """
    (props,) = [r["Properties"] for lid, r in templates[SEED_STACK]["Resources"].items()
                if lid.startswith("AdtDatabase") and r["Type"] == "AWS::RDS::DBInstance"]
    assert props["DBInstanceClass"] == "db.t4g.small"


def test_seed_rds_has_iam_database_authentication(templates):
    """Password logins keep working; IAM database auth is on as well, as on
    the ERP Aurora cluster (checkov CKV_AWS_161, a Workshop Studio scanner
    finding until 2026-10-19)."""
    (props,) = [r["Properties"] for lid, r in templates[SEED_STACK]["Resources"].items()
                if lid.startswith("AdtDatabase") and r["Type"] == "AWS::RDS::DBInstance"]
    assert props.get("EnableIAMDatabaseAuthentication") is True


def _load_adt_seed_handler():
    stand_ins = [name for name in ("boto3",) if importlib.util.find_spec(name) is None]
    for name in stand_ins:
        sys.modules[name] = types.ModuleType(name)
    try:
        spec = importlib.util.spec_from_file_location(
            "adt_seed_handler_cr_versions", REPO_ROOT / "lambda-adt-seed" / "handler.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name in stand_ins:
            del sys.modules[name]


def test_adt_seed_function_knows_the_version_of_its_own_sql(templates):
    """The function behind AdtSeedResource (ServiceToken -> the Provider's
    framework onEvent function -> USER_ON_EVENT_FUNCTION_ARN) gets
    ADT_SEED_VERSION, the same literal as the Version the custom resource
    carries: lambda-adt-seed/handler.py runs a CloudFormation Update only for
    its own Version, so an update rollback that reaches the new code is a
    no-op."""
    from stacks import adt_seed_data_stack as stack

    template = templates[SEED_STACK]
    props = _custom_resource(template, "AdtSeedResource")
    framework = _get_att_arn(template, props["ServiceToken"])
    function = _get_att_arn(template, framework["Properties"]["Environment"]["Variables"]
                            ["USER_ON_EVENT_FUNCTION_ARN"])
    assert function["Type"] == "AWS::Lambda::Function"
    assert function["Properties"]["FunctionName"] == "adt-seed-data"
    value = function["Properties"]["Environment"]["Variables"]["ADT_SEED_VERSION"]
    assert isinstance(value, str) and value == props["Version"] == stack.adt_seed_version()
    assert _load_adt_seed_handler().SEED_VERSION_ENV == "ADT_SEED_VERSION"
