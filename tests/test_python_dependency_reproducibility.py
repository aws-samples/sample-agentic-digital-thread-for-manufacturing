# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Static supply-chain ratchets for Python deployment locks and installers."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = (
    ROOT / "requirements.txt",
    ROOT / "agentic_digital_thread/requirements/app-py312-linux-arm64.lock",
    ROOT / "agentic_digital_thread/requirements/runtime-py312-linux-arm64.lock",
    ROOT / "lambda-layers/pg8000/requirements-py312-linux-x86_64.lock",
    ROOT / "requirements/erp-simulation-py311-linux-arm64.lock",
    ROOT / "requirements/erp-dashboard-py311-linux-arm64.lock",
    ROOT / "lambda-db-init/requirements-py311-linux-arm64.lock",
)


def _requirement_blocks(text: str) -> list[str]:
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            assert blocks, f"orphan lock continuation: {line!r}"
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return ["\n".join(lines) for lines in blocks]


def test_every_lock_entry_is_exact_and_sha256_hashed():
    for path in LOCKS:
        text = path.read_text()
        blocks = _requirement_blocks(text)
        assert blocks, f"empty lock: {path.relative_to(ROOT)}"
        for block in blocks:
            first = block.splitlines()[0]
            assert re.match(r"^[A-Za-z0-9_.-]+==[^ ;\\]+(?:\s*[;\\])?", first), (
                f"non-exact entry in {path.relative_to(ROOT)}: {first}"
            )
            assert "--hash=sha256:" in block, (
                f"unhashed entry in {path.relative_to(ROOT)}: {first}"
            )
        assert "--index-url" not in text and " @ " not in text


def test_declared_manifests_pin_every_dependency_exactly():
    """The manifests a reader installs from name one version each, the same as
    the lock: no open range resolves to a different release at install time."""
    import tomllib

    project = tomllib.loads((ROOT / "agentic_digital_thread/pyproject.toml").read_text())["project"]
    declared = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        declared += extra
    declared += [
        line.strip()
        for line in (ROOT / "erp_simulation/requirements-erp-streamlit.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for spec in declared:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+(\[[a-z,]+\])?==[0-9][0-9A-Za-z.]*", spec), spec


def test_strands_agents_load_bearing_pin_is_preserved():
    pyproject = (ROOT / "agentic_digital_thread/pyproject.toml").read_text()
    assert '"strands-agents==1.48.0"' in pyproject
    for name in ("app", "runtime"):
        lock = (
            ROOT / f"agentic_digital_thread/requirements/{name}-py312-linux-arm64.lock"
        ).read_text()
        assert lock.splitlines().count("strands-agents==1.48.0 \\") == 1
        assert not re.search(r"(?m)^strands-agents==(?!1\.48\.0)", lock)


def _lock_versions(path: Path) -> dict[str, str]:
    return dict(re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^ ;\\]+)", path.read_text()))


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


# Selected security-advisory floors: for each package, the lowest release that
# clears the advisories listed beside it (osv-scanner, 2026-11-17, and later
# fixes such as opentelemetry-api). A lock may carry a newer release; it may
# never carry an older one.
SECURITY_FLOORS = {
    "aiohttp": "3.14.4",
    "authlib": "1.9.1",
    # GHSA-j6g5-3hh3-pgw8 / PYSEC-2026-3551 (install_packages() specifier check).
    "bedrock-agentcore": "1.18.1",
    "certifi": "2026.10.18",
    "dnspython": "2.9.2",
    "filelock": "4.2.1",
    "joserfc": "1.7.5",
    "mcp": "1.30.0",
    # CVE-2026-126360 (TraceContextTextMapPropagator.extract() backtracking).
    "opentelemetry-api": "1.45.2",
    "pillow": "13.0.0",
    "pyasn1": "0.6.5",
    "pyjwt": "2.15.0",
    "soupsieve": "4.0",
    "strands-agents": "1.48.0",
    "urllib3": "2.8.0",
    "virtualenv": "21.14.13",
}


def test_locks_stay_at_or_above_the_security_fix_floors():
    for path in LOCKS:
        for name, version in _lock_versions(path).items():
            floor = SECURITY_FLOORS.get(name)
            if floor:
                assert _version_tuple(version) >= _version_tuple(floor), (
                    f"{path.relative_to(ROOT)} pins {name}=={version}, "
                    f"below the security floor {floor}"
                )


def test_python_jose_is_gone_and_pyjwt_is_a_direct_dependency():
    """python-jose has no release that fixes its open advisory; Cognito token
    verification uses PyJWT (agents/auth/cognito.py)."""
    pyproject = (ROOT / "agentic_digital_thread/pyproject.toml").read_text()
    assert "python-jose" not in pyproject
    assert '"pyjwt[crypto]' in pyproject
    for path in LOCKS:
        versions = _lock_versions(path)
        assert "python-jose" not in versions, path.relative_to(ROOT)
        assert "ecdsa" not in versions, path.relative_to(ROOT)
    cognito = (ROOT / "agentic_digital_thread/agents/auth/cognito.py").read_text()
    assert "from jose" not in cognito and "import jose" not in cognito



def test_python_312_images_enforce_context_locks():
    expected = {
        "Dockerfile": "app-py312-linux-arm64.lock",
        "Dockerfile.runtime": "runtime-py312-linux-arm64.lock",
    }
    for dockerfile, lock_name in expected.items():
        text = (ROOT / "agentic_digital_thread" / dockerfile).read_text()
        assert lock_name in text
        assert "--require-hashes" in text
    for dockerfile in ("Dockerfile", "Dockerfile.runtime"):
        text = (ROOT / "agentic_digital_thread" / dockerfile).read_text()
        assert "pip install --no-cache-dir --no-deps ." in text
        assert not re.search(r"pip install(?: --no-cache-dir)? \.($|\s)", text)


def test_runtime_has_no_inline_unresolved_installs():
    runtime = (ROOT / "agentic_digital_thread/Dockerfile.runtime").read_text()
    assert "aws-opentelemetry-distro>=" not in runtime


def test_runtime_pins_the_adot_release_that_retries_a_failed_credential_lookup():
    """aws-opentelemetry-distro 0.19-0.21 cached a None credential lookup for the
    process lifetime, so an idle-woken AgentCore session sent every span batch
    unsigned (X-Ray 403). 0.22.0 retries; 0.23.0 keeps that and pins
    opentelemetry-api 1.45.2, which fixes CVE-2026-126360."""
    assert "aws-opentelemetry-distro==0.23.0" in (ROOT / "requirements/runtime.in").read_text()
    lock = (ROOT / "agentic_digital_thread/requirements/runtime-py312-linux-arm64.lock").read_text()
    assert re.search(r"^aws-opentelemetry-distro==0\.23\.0 \\$", lock, re.M)
    assert re.search(r"^opentelemetry-api==1\.45\.2 \\$", lock, re.M)


def test_duckdb_stays_pinned_in_the_project_and_both_locks():
    """duckdb is version-pinned where it matters and resolves under the global
    upload cutoff, not a per-package exception."""
    assert '"duckdb==2.0.0"' in (
        ROOT / "agentic_digital_thread/pyproject.toml"
    ).read_text()
    for lock in (
        "agentic_digital_thread/requirements/app-py312-linux-arm64.lock",
        "agentic_digital_thread/requirements/runtime-py312-linux-arm64.lock",
    ):
        assert re.search(r"^duckdb==2\.0\.0 \\$", (ROOT / lock).read_text(), re.M)


def test_the_removed_provisioner_lock_and_image_stay_gone():
    """The onboarding provisioner Lambda (its image and its lock) was removed
    on 2026-10-29; nothing may build or check it again."""
    assert not (ROOT / "agentic_digital_thread/Dockerfile.provisioner").exists()
    assert not (ROOT / "agentic_digital_thread/requirements/provisioner-py312-linux-arm64.lock").exists()
    assert not (ROOT / "requirements/provisioner.in").exists()


def test_layer_recipe_is_binary_only_hash_locked_and_architecture_aligned():
    source = (ROOT / "stacks/adt_seed_data_stack.py").read_text()
    assert "--require-hashes --only-binary=:all:" in source
    assert "--platform manylinux2014_x86_64" in source
    assert "--python-version 3.12 --abi cp312" in source
    assert source.count("compatible_architectures=[lambda_.Architecture.X86_64]") == 1
    assert source.count("architecture=lambda_.Architecture.X86_64") == 1
    assert "from_layer_version_arn" not in source
    # The layer carries the sample's client module next to the locked wheels.
    assert "cp /asset-input/pgdriver.py /asset-output/python/" in source
    layer_in = (ROOT / "requirements/pg8000-layer.in").read_text()
    assert "pg8000==1.31.6" in layer_in.splitlines()


def test_erp_python_311_images_enforce_arm64_hash_locks():
    expected = {
        "Dockerfile.lambda": "erp-simulation-py311-linux-arm64.lock",
        "Dockerfile.seed": "erp-simulation-py311-linux-arm64.lock",
        "Dockerfile.cleanup": "erp-simulation-py311-linux-arm64.lock",
        "Dockerfile.dashboard": "erp-dashboard-py311-linux-arm64.lock",
    }
    for dockerfile, lock_name in expected.items():
        text = (ROOT / "docker" / dockerfile).read_text()
        assert lock_name in text
        assert "--require-hashes --only-binary=:all:" in text
        assert "requirements-simulation.txt" not in text
        assert "requirements-erp-streamlit.txt" not in text


def test_opc_server_uses_exact_lock_and_production_npm_ci():
    package = (ROOT / "highbyte/components/opc-server/package.json").read_text()
    lock = (ROOT / "highbyte/components/opc-server/package-lock.json").read_text()
    dockerfile = (ROOT / "highbyte/components/opc-server/Dockerfile").read_text()
    assert '"node-opcua": "2.187.32"' in package
    assert '"lockfileVersion": 3' in lock
    assert '"node-opcua": "2.187.32"' in lock
    assert "npm ci --omit=dev" in dockerfile
    assert "npm install" not in dockerfile


def test_lambda_db_init_uses_image_context_hash_lock():
    direct = (ROOT / "requirements/lambda-db-init.in").read_text().splitlines()
    assert direct == ["pg8000==1.31.6", "boto3==1.43.79"]

    dockerfile = (ROOT / "lambda-db-init/Dockerfile").read_text()
    assert "requirements-py311-linux-arm64.lock" in dockerfile
    assert "--require-hashes --only-binary=:all:" in dockerfile
    assert not re.search(r"pip install[^\n]*(?:\bpg8000\b|\bboto3\b)", dockerfile)
    # The handler's client module ships beside it in the image.
    assert re.search(r"^COPY handler\.py pgdriver\.py \$\{LAMBDA_TASK_ROOT\}/$", dockerfile, re.M)
