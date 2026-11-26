# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wheel metadata ships EVERY ``agents`` subpackage and the demo data files.

``[tool.setuptools] packages = ["agents"]`` was an explicit list — it does not
recurse — so a built wheel omitted ``agents.auth``, ``agents.demo``,
``agents.provisioner`` and ``agents.context_interview``. The image masked it
(the Dockerfile COPYs the source tree into the working directory, which
shadows the installed package), but ``pip install`` anywhere else broke.

Offline and stdlib-only (setuptools is not importable in a bare venv): reads
pyproject.toml and checks every on-disk package against the ``include``
patterns with setuptools' fnmatch semantics. A real wheel build
(``pip wheel --no-deps .``) was verified by hand to contain all five packages
and the three demo JSON files.
"""
from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

_ADT = Path(__file__).resolve().parents[2]
_EXPECTED_SUBPACKAGES = {"agents.auth", "agents.context_interview", "agents.demo",
                         "agents.provisioner"}


def _pyproject() -> dict:
    return tomllib.loads((_ADT / "pyproject.toml").read_text(encoding="utf-8"))


def _packages_on_disk() -> set[str]:
    return {
        ".".join(p.parent.relative_to(_ADT).parts)
        for p in (_ADT / "agents").rglob("__init__.py")
        if "__pycache__" not in p.parts
    }


def test_pyproject_uses_recursive_package_discovery():
    tool = _pyproject()["tool"]["setuptools"]
    assert not isinstance(tool.get("packages"), list), \
        "an explicit `packages = [...]` list does not recurse into subpackages"
    assert tool["packages"]["find"]["include"] == ["agents*"]


def test_include_patterns_match_every_agents_package_on_disk():
    include = _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    on_disk = _packages_on_disk()
    assert "agents" in on_disk and _EXPECTED_SUBPACKAGES <= on_disk
    unmatched = {pkg for pkg in on_disk
                 if not any(fnmatch.fnmatchcase(pkg, pat) for pat in include)}
    assert unmatched == set(), unmatched


def test_demo_data_files_are_declared_package_data():
    data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert "*.json" in data["agents.demo"]
    assert any(pattern.startswith("fixtures/") for pattern in data["agents.demo"])
    json_files = sorted(p.name for p in (_ADT / "agents" / "demo").glob("*.json"))
    assert "golden_evals.json" in json_files  # the ONE sanctioned golden set rides the wheel
