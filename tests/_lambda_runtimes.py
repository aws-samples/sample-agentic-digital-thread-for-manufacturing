# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The deprecated AWS Lambda runtimes, and a template check for them.

The identifiers are the "Deprecated runtimes" table at
https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html
(read 2026-10-09). AWS no longer security-patches these and blocks function
create, then update, on the dates that table gives. Update from that page.
"""
from __future__ import annotations

DEPRECATED_LAMBDA_RUNTIMES = frozenset({
    "provided.al2", "nodejs20.x", "ruby3.2", "python3.9", "nodejs18.x", "dotnet6",
    "python3.8", "nodejs16.x", "dotnet7", "java8", "go1.x", "provided", "ruby2.7",
    "nodejs14.x", "python3.7", "dotnetcore3.1", "nodejs12.x", "python3.6",
    "dotnet5.0", "dotnetcore2.1", "nodejs10.x", "ruby2.5", "python2.7",
    "nodejs8.10", "nodejs4.3", "nodejs4.3-edge", "nodejs6.10", "dotnetcore1.0",
    "dotnetcore2.0", "nodejs",
})


def deprecated_lambda_runtimes(templates: dict[str, dict]) -> list[str]:
    """One problem per Lambda function whose Runtime is deprecated. Image-based
    functions (no Runtime) are skipped."""
    problems = []
    for name, template in sorted(templates.items()):
        for logical, res in sorted((template.get("Resources") or {}).items()):
            if res.get("Type") not in ("AWS::Lambda::Function", "AWS::Serverless::Function"):
                continue
            runtime = (res.get("Properties") or {}).get("Runtime")
            if isinstance(runtime, str) and runtime in DEPRECATED_LAMBDA_RUNTIMES:
                problems.append(f"{name}: {logical} uses deprecated Lambda runtime {runtime}")
    return problems
