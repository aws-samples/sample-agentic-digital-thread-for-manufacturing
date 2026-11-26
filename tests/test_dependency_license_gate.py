# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline dependency-license gate contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest

from scripts import check_dependency_licenses as gate

ROOT = Path(__file__).resolve().parents[1]


def _policy() -> dict:
    return {
        "schema_version": 1,
        "python_locks": ["requirements.txt"],
        "npm_locks": ["package-lock.json"],
        "approved_licenses": ["Apache-2.0", "MIT"],
        "denied_licenses": [
            "AGPL-3.0-only",
            "BUSL-1.1",
            "GPL-3.0-only",
            "SSPL-1.0",
        ],
        "exceptions": [],
        "metadata_overrides": {},
        "attributions": {},
        "attribution_files": ["NOTICE"],
    }


def _rows() -> list[dict]:
    return [
        {
            "ecosystem": "npm",
            "name": "@example/scoped",
            "version": "2.0.0",
            "sources": ["package-lock.json"],
            "license": "Apache-2.0",
            "metadata_source": "synthetic authoritative metadata",
            "attribution_required": False,
        },
        {
            "ecosystem": "pypi",
            "name": "example-pkg",
            "version": "1.0.0",
            "sources": ["requirements.txt"],
            "license": "MIT",
            "metadata_source": "synthetic authoritative metadata",
            "attribution_required": False,
        },
    ]


def _repository(
    tmp_path: Path, *, policy: dict | None = None, rows: list[dict] | None = None
) -> Path:
    selected_policy = policy or _policy()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/dependency-license-policy.json").write_text(
        json.dumps(selected_policy)
    )
    (tmp_path / "config/dependency-licenses.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tool_versions": {
                    "inventory_generator": "1",
                    "npm_lockfile": "3",
                    "python": "3.12.12",
                },
                "packages": rows or _rows(),
            }
        )
    )
    (tmp_path / "requirements.txt").write_text(
        "example_pkg==1.0.0 ; python_version >= '3.11' \\\n"
        "    --hash=sha256:" + "a" * 64 + "\n"
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "fixture"},
                    "node_modules/@example/scoped": {
                        "version": "2.0.0",
                        "license": "Apache-2.0",
                    },
                },
            }
        )
    )
    (tmp_path / "NOTICE").write_text("fixture attribution\n")
    return tmp_path


def test_repository_inventory_covers_every_python_and_npm_surface():
    policy = gate._read_json(ROOT / gate.POLICY_PATH)
    python_paths, npm_locks = gate.discover_lock_paths(ROOT)
    declared = sorted(policy["python_locks"] + list(policy.get("python_manifests", {})))
    assert python_paths == declared
    assert npm_locks == sorted(policy["npm_locks"])
    gate.check_repository(ROOT)


def test_a_nested_requirements_file_must_be_declared(tmp_path):
    root = _repository(tmp_path)
    (root / "svc").mkdir()
    (root / "svc/requirements.txt").write_text("example_pkg==1.0.0\n")
    with pytest.raises(gate.GateError, match="lock discovery drift"):
        gate.check_repository(root)


def test_a_declared_nested_requirements_lock_is_license_checked(tmp_path):
    policy = _policy()
    policy["python_locks"] = ["requirements.txt", "svc/requirements.txt"]
    rows = _rows()
    rows[1]["sources"] = ["requirements.txt", "svc/requirements.txt"]
    root = _repository(tmp_path, policy=policy, rows=rows)
    (root / "svc").mkdir()
    (root / "svc/requirements.txt").write_text("example_pkg==1.0.0\nsurprise==2.0\n")
    with pytest.raises(gate.GateError, match="inventory/lock drift"):
        gate.check_repository(root)


def test_a_manifest_needs_a_reason_and_is_never_also_a_lock(tmp_path):
    for manifests, message in (({"svc/requirements.in": " "}, "needs a reason"),
                               ({"requirements.txt": "x"}, "also a lock")):
        policy = _policy()
        policy["python_manifests"] = manifests
        base = tmp_path / message.replace(" ", "-")
        base.mkdir()
        root = _repository(base, policy=policy)
        (root / "svc").mkdir()
        (root / "svc/requirements.in").write_text("example-pkg>=1\n")
        with pytest.raises(gate.GateError, match=message):
            gate.check_repository(root)


def _with_manifest(tmp_path, manifest_body, *, manifest="svc/requirements.in"):
    policy = _policy()
    policy["python_manifests"] = {manifest: "compile input, not installed by any build"}
    root = _repository(tmp_path, policy=policy)
    path = root / manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest_body)
    return root


def test_a_manifest_package_is_license_checked_by_name(tmp_path):
    # example-pkg is in the inventory; an absent name must fail.
    gate.check_repository(_with_manifest(tmp_path, "example-pkg>=1.0\n"))
    (tmp_path / "x").mkdir()
    root = _with_manifest(tmp_path / "x", "example-pkg>=1.0\nabsent-pkg>=2.0\n")
    with pytest.raises(gate.GateError, match="missing license: .*absent-pkg"):
        gate.check_repository(root)


def test_a_manifest_package_with_a_denied_license_fails(tmp_path):
    rows = _rows()
    rows[1]["license"] = "GPL-3.0-only"
    policy = _policy()
    policy["python_manifests"] = {"svc/requirements.in": "compile input"}
    root = _repository(tmp_path, policy=policy, rows=rows)
    (root / "svc").mkdir()
    (root / "svc/requirements.in").write_text("example-pkg>=1.0\n")
    with pytest.raises(gate.GateError, match="unapproved license GPL-3.0-only"):
        gate.check_repository(root)


def test_a_pinned_manifest_version_must_be_in_the_inventory(tmp_path):
    root = _with_manifest(tmp_path, "example-pkg==9.9.9\n")
    with pytest.raises(gate.GateError, match=r"missing license: .*example-pkg==9\.9\.9"):
        gate.check_repository(root)


def test_a_build_installed_requirements_file_declared_as_a_manifest_fails(tmp_path):
    # A Lambda/Dockerfile-installed requirements file must be a lock, not a
    # manifest: declaring it as a manifest must fail the gate.
    policy = _policy()
    policy["python_manifests"] = {"lambda-svc/svc/requirements.txt": "falsely declared manifest"}
    root = _repository(tmp_path, policy=policy)
    (root / "lambda-svc/svc").mkdir(parents=True)
    (root / "lambda-svc/svc/requirements.txt").write_text("example_pkg==1.0.0\n")
    (root / "stacks").mkdir()
    (root / "stacks/svc_stack.py").write_text(
        'cmd = "python -m pip install -r /asset-input/svc/requirements.txt"\n'
    )
    with pytest.raises(gate.GateError, match="installed by a build"):
        gate.check_repository(root)


def test_a_dockerfile_installed_requirements_file_declared_as_a_manifest_fails(tmp_path):
    policy = _policy()
    policy["python_manifests"] = {"svc/requirements.in": "falsely declared manifest"}
    root = _repository(tmp_path, policy=policy)
    (root / "svc").mkdir()
    (root / "svc/requirements.in").write_text("example-pkg>=1.0\n")
    (root / "Dockerfile.svc").write_text(
        "FROM python:3.12\nRUN pip install --require-hashes -r svc/requirements.in\n"
    )
    with pytest.raises(gate.GateError, match="installed by a build"):
        gate.check_repository(root)


def test_an_installed_requirements_file_with_any_name_must_be_a_declared_lock(tmp_path):
    # deps.txt matches no discovery glob; installing it must still fail.
    root = _repository(tmp_path)
    (root / "deps.txt").write_text("unchecked==9.9.9\n")
    (root / "Dockerfile").write_text(
        "FROM python:3.12\nCOPY deps.txt /tmp/deps.txt\n"
        "RUN pip install --require-hashes -r /tmp/deps.txt\n"
    )
    with pytest.raises(gate.GateError, match=r"not in python_locks \(Dockerfile: -r deps\.txt\)"):
        gate.check_repository(root)


def _declared_deps(tmp_path, license_value):
    """A Dockerfile installs deps.txt, declared in python_locks, pinning
    example-pkg (inventoried with *license_value*)."""
    policy = _policy()
    policy["python_locks"] = ["deps.txt", "requirements.txt"]
    rows = _rows()
    rows[1]["sources"] = ["deps.txt", "requirements.txt"]
    rows[1]["license"] = license_value
    root = _repository(tmp_path, policy=policy, rows=rows)
    (root / "deps.txt").write_text("example_pkg==1.0.0\n")
    (root / "Dockerfile").write_text(
        "FROM python:3.12\nCOPY deps.txt /tmp/deps.txt\n"
        "RUN pip install --require-hashes -r /tmp/deps.txt\n"
    )
    return root


def test_an_installed_deps_txt_declared_as_a_lock_passes(tmp_path):
    gate.check_repository(_declared_deps(tmp_path, "MIT"))


def test_a_declared_deps_txt_is_license_checked(tmp_path):
    with pytest.raises(gate.GateError, match="unapproved license GPL-3.0-only"):
        gate.check_repository(_declared_deps(tmp_path, "GPL-3.0-only"))


def test_a_declared_deps_txt_is_parsed_as_a_lock(tmp_path):
    root = _declared_deps(tmp_path, "MIT")
    (root / "deps.txt").write_text("example_pkg==1.0.0\nunlisted==2.0\n")
    with pytest.raises(gate.GateError, match="inventory/lock drift"):
        gate.check_repository(root)


def test_a_declared_lock_that_does_not_exist_fails(tmp_path):
    root = _declared_deps(tmp_path, "MIT")
    (root / "deps.txt").unlink()
    with pytest.raises(gate.GateError, match="declared python lock does not exist: deps.txt"):
        gate.check_repository(root)


def test_a_dockerfile_installing_a_declared_lock_passes(tmp_path):
    root = _repository(tmp_path)
    (root / "Dockerfile").write_text(
        "FROM python:3.12\nCOPY requirements.txt /tmp/req.lock\n"
        "RUN pip install --require-hashes -r /tmp/req.lock && cp -r /src /app\n"
    )
    gate.check_repository(root)


def test_a_constant_built_install_path_is_resolved(tmp_path):
    root = _repository(tmp_path)
    (root / "stacks").mkdir()
    (root / "stacks/svc_stack.py").write_text(
        'LOCK = "deps.txt"\ncmd = f"pip install -r /asset-input/{LOCK}"\n'
    )
    with pytest.raises(gate.GateError, match="not in python_locks"):
        gate.check_repository(root)


@pytest.mark.parametrize(("ref", "fails"), [("$DIR/deps.txt", True), ("$DIR/x.lock", False)])
def test_an_unresolved_install_path_passes_only_for_a_lock_file(tmp_path, ref, fails):
    root = _repository(tmp_path)
    (root / "Dockerfile").write_text(f"FROM python:3.12\nRUN pip install -r {ref}\n")
    if fails:
        with pytest.raises(gate.GateError, match="cannot resolve an installed requirements path"):
            gate.check_repository(root)
    else:
        gate.check_repository(root)


def test_package_json_needs_a_declared_lock(tmp_path):
    root = _repository(tmp_path)
    (root / "package.json").write_text("{}\n")
    gate.check_repository(root)
    (root / "web").mkdir()
    (root / "web/package.json").write_text("{}\n")
    with pytest.raises(gate.GateError, match="package.json without a declared package-lock.json"):
        gate.check_repository(root)


def test_check_path_performs_no_network_call(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline --check attempted network access")

    monkeypatch.setattr(gate, "urlopen", forbidden)
    gate.check_repository(ROOT)


def test_kiro_lock_surface_is_ignored_but_root_lock_still_fails_closed(tmp_path):
    root = _repository(tmp_path)
    scratch = root / ".kiro/scratch"
    scratch.mkdir(parents=True)
    (scratch / "undeclared.lock").write_text("private-scratch==1.0\n")
    gate.check_repository(root)

    (root / "undeclared.lock").write_text("surprise==1.0\n")
    with pytest.raises(gate.GateError, match="lock discovery drift"):
        gate.check_repository(root)


def test_python_environment_marker_and_scoped_npm_package_are_supported(tmp_path):
    gate.check_repository(_repository(tmp_path))


def test_unpinned_python_requirement_is_rejected(tmp_path):
    root = _repository(tmp_path)
    (root / "requirements.txt").write_text("example-pkg>=1.0\n")
    with pytest.raises(gate.GateError, match="not exactly pinned"):
        gate.check_repository(root)


def test_malformed_python_lock_is_rejected(tmp_path):
    root = _repository(tmp_path)
    (root / "requirements.txt").write_text("    --hash=sha256:" + "a" * 64 + "\n")
    with pytest.raises(gate.GateError, match="orphan lock continuation"):
        gate.check_repository(root)


def test_extra_lock_surface_is_rejected(tmp_path):
    root = _repository(tmp_path)
    (root / "undeclared.lock").write_text("surprise==1.0\n")
    with pytest.raises(gate.GateError, match="lock discovery drift"):
        gate.check_repository(root)


def test_lock_inventory_version_drift_is_rejected(tmp_path):
    root = _repository(tmp_path)
    inventory_path = root / gate.INVENTORY_PATH
    inventory = json.loads(inventory_path.read_text())
    inventory["packages"][1]["version"] = "1.0.1"
    inventory_path.write_text(json.dumps(inventory))
    with pytest.raises(gate.GateError, match="inventory/lock drift"):
        gate.check_repository(root)


def test_duplicate_inventory_coordinate_is_rejected(tmp_path):
    rows = _rows()
    rows.append(deepcopy(rows[0]))
    root = _repository(tmp_path, rows=rows)
    with pytest.raises(gate.GateError, match="duplicate inventory row"):
        gate.check_repository(root)


@pytest.mark.parametrize(
    "license_value",
    ["GPL-3.0-only", "AGPL-3.0-only", "SSPL-1.0", "BUSL-1.1"],
)
def test_unreviewed_restrictive_license_is_rejected(tmp_path, license_value):
    rows = _rows()
    rows[1]["license"] = license_value
    root = _repository(tmp_path, rows=rows)
    with pytest.raises(gate.GateError, match="unapproved license"):
        gate.check_repository(root)


@pytest.mark.parametrize("license_value", ["", "UNKNOWN", "not a license"])
def test_missing_unknown_or_non_spdx_license_is_rejected(tmp_path, license_value):
    rows = _rows()
    rows[1]["license"] = license_value
    root = _repository(tmp_path, rows=rows)
    with pytest.raises(gate.GateError, match="missing license|invalid SPDX"):
        gate.check_repository(root)


def test_mixed_or_expression_cannot_hide_unreviewed_branch(tmp_path):
    rows = _rows()
    rows[1]["license"] = "MIT OR GPL-3.0-only"
    root = _repository(tmp_path, rows=rows)
    with pytest.raises(gate.GateError, match="unapproved license"):
        gate.check_repository(root)


def test_and_and_fully_reviewed_or_expression_semantics(tmp_path):
    rows = _rows()
    rows[1]["license"] = "MIT AND (Apache-2.0 OR MIT)"
    gate.check_repository(_repository(tmp_path, rows=rows))


def test_expired_package_specific_exception_is_rejected(tmp_path):
    policy = _policy()
    policy["exceptions"] = [
        {
            "ecosystem": "pypi",
            "name": "example-pkg",
            "version": "1.0.0",
            "license": "GPL-3.0-only",
            "owner": "fixture owner",
            "rationale": "fixture rationale",
            "expires": "2026-01-01",
        }
    ]
    rows = _rows()
    rows[1]["license"] = "GPL-3.0-only"
    root = _repository(tmp_path, policy=policy, rows=rows)
    with pytest.raises(gate.GateError, match="expired license exception"):
        gate.check_repository(root, today=date(2026, 8, 27))


def test_current_narrow_package_exception_is_accepted(tmp_path):
    policy = _policy()
    policy["exceptions"] = [
        {
            "ecosystem": "pypi",
            "name": "example-pkg",
            "version": "1.0.0",
            "license": "GPL-3.0-only",
            "owner": "fixture owner",
            "rationale": "fixture rationale",
            "expires": "2026-12-31",
        }
    ]
    rows = _rows()
    rows[1]["license"] = "GPL-3.0-only"
    root = _repository(tmp_path, policy=policy, rows=rows)
    gate.check_repository(root, today=date(2026, 8, 27))


def test_attribution_marker_must_exist_in_retained_text(tmp_path):
    policy = _policy()
    policy["attributions"] = {"pypi:example-pkg": "Required upstream notice"}
    rows = _rows()
    rows[1]["attribution_required"] = True
    rows[1]["attribution_marker"] = "Required upstream notice"
    root = _repository(tmp_path, policy=policy, rows=rows)
    with pytest.raises(gate.GateError, match="missing attribution marker"):
        gate.check_repository(root)


def test_npm_lock_license_drift_is_rejected(tmp_path):
    root = _repository(tmp_path)
    lock_path = root / "package-lock.json"
    lock = json.loads(lock_path.read_text())
    lock["packages"]["node_modules/@example/scoped"]["license"] = "MIT"
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(gate.GateError, match="npm lock license drift"):
        gate.check_repository(root)


def test_refresh_uses_exact_wheel_metadata_and_rechecks(tmp_path, monkeypatch):
    root = _repository(tmp_path)

    def wheel_metadata(name, version):
        assert (name, version) == ("example-pkg", "1.0.0")
        return {
            "license_expression": "MIT",
            "license": None,
            "classifiers": [],
            "wheel": "example_pkg-1.0.0-py3-none-any.whl",
        }

    monkeypatch.setattr(gate, "_fetch_pypi", wheel_metadata)
    gate.refresh_inventory(root)
    inventory = json.loads((root / gate.INVENTORY_PATH).read_text())
    python_row = next(
        row for row in inventory["packages"] if row["ecosystem"] == "pypi"
    )
    assert python_row["metadata_source"].endswith(
        "(example_pkg-1.0.0-py3-none-any.whl)"
    )
    assert inventory["tool_versions"]["npm_lockfile"] == "3"


def _exception(**overrides) -> dict:
    value = {
        "ecosystem": "pypi",
        "name": "example-pkg",
        "version": "1.0.0",
        "license": "GPL-3.0-only",
        "owner": "fixture owner",
        "rationale": "fixture rationale",
        "expires": "2026-12-31",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("exceptions", [None, {}, "not-a-list"])
def test_exceptions_must_be_an_array(tmp_path, exceptions):
    policy = _policy()
    policy["exceptions"] = exceptions
    root = _repository(tmp_path, policy=policy)
    with pytest.raises(gate.GateError, match="exceptions must be an array"):
        gate.check_repository(root, today=date(2026, 8, 27))


@pytest.mark.parametrize("entry", [None, [], "not-an-object"])
def test_every_exception_must_be_an_object(tmp_path, entry):
    policy = _policy()
    policy["exceptions"] = [entry]
    root = _repository(tmp_path, policy=policy)
    with pytest.raises(gate.GateError, match="exception must be an object"):
        gate.check_repository(root, today=date(2026, 8, 27))


@pytest.mark.parametrize(
    "field",
    ["ecosystem", "name", "version", "license", "owner", "rationale", "expires"],
)
@pytest.mark.parametrize("missing_value", [None, "", "   "])
def test_exception_fields_are_required_nonblank_strings(tmp_path, field, missing_value):
    policy = _policy()
    entry = _exception()
    if missing_value is None:
        entry.pop(field)
    else:
        entry[field] = missing_value
    policy["exceptions"] = [entry]
    root = _repository(tmp_path, policy=policy)
    with pytest.raises(gate.GateError, match=rf"exception missing {field}"):
        gate.check_repository(root, today=date(2026, 8, 27))


def test_malformed_exception_expiry_is_rejected_even_when_unused(tmp_path):
    policy = _policy()
    policy["exceptions"] = [_exception(name="absent-pkg", expires="tomorrow")]
    root = _repository(tmp_path, policy=policy)
    with pytest.raises(gate.GateError, match="invalid exception expiry"):
        gate.check_repository(root, today=date(2026, 8, 27))


def test_expired_exception_is_rejected_even_when_unused(tmp_path):
    policy = _policy()
    policy["exceptions"] = [_exception(name="absent-pkg", expires="2026-01-01")]
    root = _repository(tmp_path, policy=policy)
    with pytest.raises(gate.GateError, match="expired license exception"):
        gate.check_repository(root, today=date(2026, 8, 27))


def test_duplicate_exact_exception_coordinate_is_rejected(tmp_path):
    policy = _policy()
    policy["exceptions"] = [_exception(), _exception(rationale="second rationale")]
    rows = _rows()
    rows[1]["license"] = "GPL-3.0-only"
    root = _repository(tmp_path, policy=policy, rows=rows)
    with pytest.raises(gate.GateError, match="duplicate license exception"):
        gate.check_repository(root, today=date(2026, 8, 27))


def test_orphaned_exception_coordinate_is_rejected(tmp_path):
    policy = _policy()
    policy["exceptions"] = [_exception(name="absent-pkg")]
    root = _repository(tmp_path, policy=policy)
    with pytest.raises(gate.GateError, match="orphaned license exception"):
        gate.check_repository(root, today=date(2026, 8, 27))


def test_exception_license_must_match_locked_inventory_row(tmp_path):
    policy = _policy()
    policy["exceptions"] = [_exception(license="GPL-3.0-only")]
    rows = _rows()
    rows[1]["license"] = "MIT"
    root = _repository(tmp_path, policy=policy, rows=rows)
    with pytest.raises(gate.GateError, match="exception license mismatch"):
        gate.check_repository(root, today=date(2026, 8, 27))
