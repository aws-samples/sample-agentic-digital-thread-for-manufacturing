# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The HighByte setup Lambda asset depends only on what the Lambda Node.js
runtime provides.

The asset is the .mjs sources, package.json/package-lock.json and the project
JSON -- no node_modules. Lambda does not install dependencies from a lockfile,
so at run time the runtime-included AWS SDK v3 is what `@aws-sdk/...` resolves
to. Every import must therefore be relative, a `node:` builtin, or one of the
runtime-provided `@aws-sdk/client-*` packages the code uses.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = REPO_ROOT / "highbyte" / "lambda" / "highbyte-setup"

#: Runtime-provided AWS SDK v3 clients the setup code imports.
RUNTIME_PROVIDED = {"@aws-sdk/client-secrets-manager"}

_IMPORT = re.compile(r"""(?:^|\s)(?:import\s[^'"]*?from\s*|import\s*\(\s*|import\s+)['"]([^'"]+)['"]""",
                     re.MULTILINE)


def import_specifiers(source: str) -> set[str]:
    return set(_IMPORT.findall(source))


def disallowed_imports(sources: dict[str, str]) -> list[str]:
    bad = []
    for name, src in sorted(sources.items()):
        for spec in sorted(import_specifiers(src)):
            if spec.startswith((".", "node:")) or spec in {"fs", "path", "url"}:
                continue
            if spec in RUNTIME_PROVIDED:
                continue
            bad.append(f"{name}: imports {spec!r}, which the Lambda runtime does not provide")
    return bad


def test_source_has_no_node_modules():
    assert not (LAMBDA_DIR / "node_modules").exists()


def test_every_import_is_relative_builtin_or_runtime_sdk():
    sources = {p.name: p.read_text(encoding="utf-8") for p in LAMBDA_DIR.glob("*.mjs")}
    assert {"index.mjs", "mcp-credential.mjs"} <= set(sources)
    assert "@aws-sdk/client-secrets-manager" in import_specifiers(sources["index.mjs"])
    assert disallowed_imports(sources) == []


def test_the_import_check_rejects_a_bundled_dependency():
    assert disallowed_imports({"x.mjs": 'import { NodeHttpHandler } from "@smithy/node-http-handler";'})
    assert disallowed_imports({"x.mjs": "import axios from 'axios';"})


def test_package_json_declares_only_runtime_provided_sdk_clients():
    deps = json.loads((LAMBDA_DIR / "package.json").read_text())["dependencies"]
    assert all(d.startswith("@aws-sdk/client-") for d in deps), deps
