# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The browser lane must not report a green run having executed NOTHING.

Twice a deployed verification "passed" with every test skipped: once with
the harness env file present but not exported (51 skipped, exit 0), and
once when a slow-but-alive app missed a 3s probe (58 skipped, exit 0).
An explicitly targeted deployment must therefore FAIL, not skip.
"""
from __future__ import annotations

import re
from pathlib import Path

CONFTEST = Path(__file__).resolve().parents[1] / "e2e" / "conftest.py"


def _source() -> str:
    return CONFTEST.read_text(encoding="utf-8")


def test_explicit_base_url_failure_is_hard_not_a_skip():
    """An operator-supplied ADT_BASE_URL that cannot answer must fail."""
    src = _source()
    body = src.split("def adt_base_url", 1)[1].split("\n@pytest.fixture", 1)[0]
    assert "pytest.fail(" in body, "explicit target must fail, never skip"
    assert "explicit" in body, "the fixture must distinguish an explicit target"
    # The skip path must survive for the bare-pytest/offline case.
    assert "pytest.skip(" in body, "offline default must still skip"


def test_probe_retries_so_a_slow_app_is_not_read_as_absent():
    """A deployed app under load is slow, not gone; one short probe lies."""
    body = _source().split("def adt_base_url", 1)[1].split("\n@pytest.fixture", 1)[0]
    assert "range(3)" in body, "must retry rather than trust a single probe"
    timeouts = [int(t) for t in re.findall(r"timeout=(\d+)", body)]
    assert timeouts and min(timeouts) >= 15, (
        f"probe timeout too short for a deployed app: {timeouts}")


def test_failure_message_names_the_false_green_it_prevents():
    """The message must tell an operator why it refused to skip."""
    body = _source().split("def adt_base_url", 1)[1].split("\n@pytest.fixture", 1)[0]
    assert "zero tests" in body or "run zero" in body
    assert "ADT_BASE_URL" in body
