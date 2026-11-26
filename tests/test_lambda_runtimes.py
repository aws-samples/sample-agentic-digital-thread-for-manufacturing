# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""No packaged Lambda uses a deprecated AWS Lambda runtime.

AWS stops security-patching a runtime on its deprecation date and later blocks
function create and update for it. The deprecated identifiers are the
"Deprecated runtimes" table at
https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html
(``tests/_lambda_runtimes.DEPRECATED_LAMBDA_RUNTIMES``; update it from that page).

Two views, so a deprecated runtime cannot slip in anywhere:
  * source: every ``lambda_.Runtime.<X>`` the CDK app names;
  * synth: tests/test_highbyte_stack.py checks the HighByte/OPC UA stack (the
    only Node.js function the repo defines).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from _lambda_runtimes import (  # noqa: E402
    DEPRECATED_LAMBDA_RUNTIMES, deprecated_lambda_runtimes,
)

#: CDK Runtime constant name -> runtime identifier (nodejs20.x, python3.12, ...).
_RUNTIME_CONST = re.compile(r"\bRuntime\.(NODEJS|PYTHON|RUBY|JAVA|DOTNET|PROVIDED|GO)_([0-9A-Z_]+)\b")


def _identifier(family: str, rest: str) -> str:
    family = family.lower()
    if family == "nodejs":
        return f"nodejs{rest.split('_')[0]}.x"
    if family == "python":
        major, minor = rest.split("_")[:2]
        return f"python{major}.{minor}"
    return f"{family}{rest.lower().replace('_', '.')}"


def test_the_deprecated_table_names_the_known_runtimes():
    assert {"nodejs20.x", "nodejs18.x", "python3.9", "python3.8"} <= DEPRECATED_LAMBDA_RUNTIMES
    assert not {"nodejs22.x", "nodejs24.x", "python3.12"} & DEPRECATED_LAMBDA_RUNTIMES


def test_no_source_names_a_deprecated_runtime():
    sources = [REPO_ROOT / "app.py", *sorted((REPO_ROOT / "stacks").rglob("*.py"))]
    found = []
    for path in sources:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _RUNTIME_CONST.finditer(line):
                ident = _identifier(m.group(1), m.group(2))
                if ident in DEPRECATED_LAMBDA_RUNTIMES:
                    found.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {ident}")
    assert found == [], found


def test_the_checker_flags_a_deprecated_runtime():
    template = {"Resources": {"Fn": {"Type": "AWS::Lambda::Function",
                                     "Properties": {"Runtime": "nodejs20.x"}},
                              "Img": {"Type": "AWS::Lambda::Function",
                                      "Properties": {"PackageType": "Image"}}}}
    (problem,) = deprecated_lambda_runtimes({"T": template})
    assert "nodejs20.x" in problem and "Fn" in problem
