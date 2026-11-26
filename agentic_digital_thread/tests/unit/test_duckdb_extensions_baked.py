# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every image that opens the S3 Tables connector bakes its DuckDB extensions.

The connector never downloads extension code at runtime (auto-install is off),
so an image that runs it without the baked set fails on the first LOAD. Two
images run it: the system-agent runtime (queries) and the app (onboarding
discover, which introspects the bucket from the ECS task). On 2026-11-16 the
app image had none, so discover of an S3 Tables system failed with
'Extension "aws" not found'.
"""
import re
from pathlib import Path

import pytest

from agents.duckdb_tools import _DUCKDB_EXTENSIONS

PKG = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.runtime"])
def test_image_bakes_every_connector_extension(dockerfile):
    text = (PKG / dockerfile).read_text()
    assert re.search(r"^ENV S3T_DUCKDB_EXTENSION_DIR=/opt/duckdb/extensions$", text, re.M), dockerfile
    install = re.search(r"for e in \(([^)]*)\)", text)
    assert install, f"{dockerfile} does not INSTALL the DuckDB extensions"
    baked = tuple(re.findall(r"'([a-z]+)'", install.group(1)))
    assert set(_DUCKDB_EXTENSIONS) <= set(baked), (dockerfile, baked)
