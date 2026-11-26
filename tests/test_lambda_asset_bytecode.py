# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda file assets never carry Python bytecode.

A workshop repackage once shipped 17 ``.pyc`` files in two Lambda zips,
because the packaging checkout held ``__pycache__`` from earlier test runs:
a mock's bundling copied its package wholesale into the asset, and the
seed function's ``Code.from_asset`` had no exclude. Bytecode embeds the
absolute path of the checkout that wrote it, moves the asset hash with no
source change, and ships code nobody reviewed.

* The seed function stages its directory without bytecode (real CDK staging
  of a copy holding planted bytecode).
* Every plain-directory ``Code.from_asset`` in ``stacks/`` excludes
  ``__pycache__`` and ``*.pyc``. Bundled assets (their command decides the
  output) and ``stage_tree`` directories (which already ignore
  ``__pycache__``) are exempt.

Offline: no Docker, no AWS. The bundling options are captured, never run in
a container. Run without writing bytecode or a pytest cache into the checkout:
  PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider tests/test_lambda_asset_bytecode.py -q
"""
from __future__ import annotations

import ast
import shlex
import shutil
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("aws_cdk")

BYTECODE_EXCLUDE = {"__pycache__", "*.pyc"}
_NORMALIZE = "find /asset-output -type d -exec chmod 755"


def _bytecode_under(tree: Path) -> list[str]:
    return sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")
                  if p.name == "__pycache__" or p.suffix == ".pyc")


def _plant(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def test_seed_function_stages_no_bytecode(monkeypatch, tmp_path):
    import aws_cdk as cdk
    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_iam as iam
    from aws_cdk import aws_lambda as real_lambda
    from aws_cdk import aws_secretsmanager as secretsmanager

    from stacks import adt_seed_data_stack as seed

    source = tmp_path / "lambda-adt-seed"
    shutil.copytree(REPO_ROOT / "lambda-adt-seed", source,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    _plant(source, {"__pycache__/handler.cpython-312.pyc": "stale", "stray.pyc": "stale"})
    redirected: list[str] = []

    class _Lambda:
        """aws_lambda with ``Code.from_asset`` pointed at the planted copy."""

        def __getattr__(self, name):
            return getattr(real_lambda, name)

        class Code:
            @staticmethod
            def from_asset(path, **kwargs):
                redirected.append(path)
                return real_lambda.Code.from_asset(str(source), **kwargs)

    class _Probe(cdk.Stack):
        pass

    cdk.AssetStaging.clear_asset_hash_cache()
    outdir = tmp_path / "cdk.out"
    stack = _Probe(cdk.App(outdir=str(outdir)), "SeedProbe",
                   env=cdk.Environment(account="111122223333", region="us-east-1"))
    stack.network_stack = types.SimpleNamespace(vpc=ec2.Vpc(stack, "Vpc"))
    stack.db_secret = secretsmanager.Secret(stack, "DbSecret")
    stack.db_endpoint = "seed.example.internal"
    role = iam.Role(stack, "Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    layer = real_lambda.LayerVersion.from_layer_version_arn(
        stack, "Layer", "arn:aws:lambda:us-east-1:111122223333:layer:pg8000:1")
    monkeypatch.setattr(seed, "lambda_", _Lambda())
    seed._create_seed_function(stack, role, layer)

    assert [Path(p).resolve() for p in redirected] == [(REPO_ROOT / "lambda-adt-seed").resolve()]
    staged = [p for p in outdir.glob("asset.*") if (p / "handler.py").is_file()]
    assert len(staged) == 1, sorted(p.name for p in outdir.iterdir())
    assert _bytecode_under(staged[0]) == [], "the seed Lambda asset ships Python bytecode"


def _staged_names(function: ast.AST) -> set[str]:
    """Names bound to a ``stage_tree(...)`` result in *function*."""
    names = set()
    for node in ast.walk(function):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "stage_tree"):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _code_from_asset_calls():
    """``(file, line, call, names staged in its function)`` for every
    ``Code.from_asset`` in stacks/."""
    for path in sorted((REPO_ROOT / "stacks").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "from_asset"
                        and getattr(node.func.value, "attr", None) == "Code"):
                    yield path.name, node.lineno, node, _staged_names(function)


def test_every_plain_directory_lambda_asset_excludes_bytecode():
    checked, offenders = set(), []
    for name, line, call, staged in _code_from_asset_calls():
        keywords = {k.arg: k.value for k in call.keywords}
        if "bundling" in keywords:
            continue
        if call.args and isinstance(call.args[0], ast.Name) and call.args[0].id in staged:
            continue
        exclude = keywords.get("exclude")
        patterns = set(ast.literal_eval(exclude)) if exclude is not None else set()
        checked.add(f"{name}:{line}")
        if not BYTECODE_EXCLUDE <= patterns:
            offenders.append(f"{name}:{line} excludes {sorted(patterns)}")
    assert {c.split(":")[0] for c in checked} >= {"adt_seed_data_stack.py", "erp_dashboard_stack.py",
                                                   "_db_readers.py"}, sorted(checked)
    assert offenders == [], (
        "Code.from_asset on a source directory must pass "
        f"exclude=['__pycache__', '*.pyc']: {offenders}")
