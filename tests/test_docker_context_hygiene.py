# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Docker build-context hygiene and synth purity (2026-09 architecture review).

* ``cdk synth`` is PURE: it writes nothing into the checkout. It used to
  ``copytree`` 383 MB of OPC datasets into ``highbyte/components/opc-server/``
  and copy the HighByte config into the setup-Lambda directory.
* The OPC UA image builds from the ``highbyte/`` context with an ALLOWLIST
  ``.dockerignore`` — the staged asset holds the server sources and the three
  gzip datasets, nothing else.
* Repo-root contexts (the four ERP images under ``docker/``) use an ALLOWLIST
  ``.dockerignore``: the staged asset holds what those Dockerfiles COPY and
  nothing else, so editing ``app.py``, docs or ``cdk.json`` leaves their image
  tags alone.
* Anything assembled at synth lives under ``<outdir>/adt-staging`` (never the
  source tree).
* Python bytecode that tests or imports leave in a checkout never reaches an
  image context or its asset hash, so two packages of one commit get the same
  image tags. Synth itself writes no bytecode into the checkout.
* The i3X Explorer bundle under ``static/`` is in the app image only; the
  AgentCore runtime image shares that context but never copies it.

The pure checks need only the repo files. The synth check mirrors
``test_agentcore_app_cognito.py`` (vendored CDK CLI + Node; skips without them).
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import posixpath
import re
import shutil
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"


def _dockerignore(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def test_repo_root_dockerfiles_copy_only_from_allowed_roots():
    allowed = ("requirements/", "erp_simulation/", "lambda-db-init/", "lambda_/")
    for dockerfile in sorted((REPO_ROOT / "docker").glob("Dockerfile.*")):
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.startswith(("COPY ", "ADD ")) and "--from=" not in line:
                src = line.split()[1]
                assert src.startswith(allowed), f"{dockerfile.name}: {line!r} copies outside {allowed}"


def test_highbyte_context_is_an_allowlist():
    rules = _dockerignore(REPO_ROOT / "highbyte" / ".dockerignore")
    assert rules[0] == "*", "highbyte/.dockerignore must start by excluding everything"
    assert "!components/opc-server/**" in rules
    for site in ("site1_milwaukee_bottling", "site2_detroit_automotive", "site3_cedarrapids_food",
                 "site4_indianapolis_machining"):
        assert f"!datasets/{site}.csv.gz" in rules
    dockerfile = (REPO_ROOT / "highbyte/components/opc-server/Dockerfile").read_text(encoding="utf-8")
    assert "COPY components/opc-server/ " in dockerfile
    assert "COPY datasets/site1_milwaukee_bottling.csv.gz /app/datasets/Milwaukee.csv.gz" in dockerfile
    server_sh = (REPO_ROOT / "highbyte/components/opc-server/server.sh").read_text(encoding="utf-8")
    assert "gunzip -c" in server_sh


def test_sfc_context_is_an_allowlist():
    """The SFC context is pinned the same way the HighByte one is.

    It is a THIRD kind of context alongside the two above — a self-contained
    directory under docker/ rather than a flat docker/Dockerfile.* built from the
    repo root — so the glob in test_repo_root_dockerfiles_copy_only_from_allowed_roots
    does not see it. Without this test it would be the only Docker context in the
    repo that nothing checks.
    """
    rules = _dockerignore(REPO_ROOT / "docker" / "sfc" / ".dockerignore")
    assert rules == ["*", "!sfc-config.json"], (
        "docker/sfc/.dockerignore must exclude everything but the generated config")

    dockerfile = (REPO_ROOT / "docker/sfc/Dockerfile").read_text(encoding="utf-8")
    # The generated config lands at the path stacks/sfc_stack.py tells the
    # container to read; these two must not drift apart.
    assert "COPY --chown=10001:10001 sfc-config.json /etc/sfc/sfc-config.json" in dockerfile
    sfc_stack = (REPO_ROOT / "stacks/sfc_stack.py").read_text(encoding="utf-8")
    assert 'SFC_CONFIG_MOUNT = "/etc/sfc"' in sfc_stack
    assert 'SFC_CONFIG_FILENAME = "sfc-config.json"' in sfc_stack
    # The bundles are fetched in the build, so every one must be pinned by a
    # SHA-512 checked before it is unpacked — a changed download fails the build.
    bundles = ("sfc-main", "opcua", "aws-sitewise-target", "aws-s3-tables-target")
    for bundle in bundles:
        assert re.search(rf"\b[0-9a-f]{{128}} {re.escape(bundle)}\.tar\.gz\b", dockerfile), bundle
    assert "sha512sum -c -" in dockerfile
    assert dockerfile.index("sha512sum -c -") < dockerfile.index("tar -xzf")
    # Unprivileged, and numeric because the base image has no useradd.
    assert "USER 10001:10001" in dockerfile


# Every image asset the app builds: (build context, Dockerfile in it, a file the
# image COPYs from that context). Editing the probe file must change the asset
# hash, which proves the check below can see the context at all.
_IMAGE_CONTEXTS = (
    ("agentic_digital_thread", "Dockerfile", "agents/connection_types.py"),
    ("agentic_digital_thread", "Dockerfile.runtime", "agents/connection_types.py"),
    (".", "docker/Dockerfile.cleanup", "lambda_/lambda_cleanup_old_data.py"),
    (".", "docker/Dockerfile.dashboard", "erp_simulation/__init__.py"),
    (".", "docker/Dockerfile.lambda", "lambda_/lambda_function.py"),
    (".", "docker/Dockerfile.seed", "lambda_/lambda_seed_data.py"),
    ("lambda-db-init", "Dockerfile", "handler.py"),
    ("highbyte", "components/opc-server/Dockerfile", "components/opc-server/server.mjs"),
    # Built from a copy staged under cdk.out (stacks/sfc_stack.py) that keeps
    # this directory's .dockerignore and adds the generated config.
    ("docker/sfc", "Dockerfile", "sfc-config.json"),
)
_DOCKERFILE_RE = re.compile(r"(^|/)(Dockerfile(\.[^/]*)?|[^/]*\.dockerfile|Containerfile[^/]*)$")


def _tracked(context: str) -> list[str]:
    """Tracked files under *context*, relative to it."""
    out = subprocess.run(["git", "ls-files", "-z", "--", context],  # nosec B603 B607
                         cwd=REPO_ROOT, capture_output=True, check=True).stdout
    prefix = "" if context == "." else context.rstrip("/") + "/"
    return [p[len(prefix):] for p in out.decode().split("\0") if p]


def _bytecode_paths(context: str) -> list[str]:
    """Where a test run or an import can leave local residue in *context*: the
    context root and every directory holding tracked Python, each with a
    ``__pycache__``, a stray ``*.pyc``, a ``__pycache__`` one level down, and
    the caches a pytest run leaves where it runs (``.pytest_cache``,
    ``.hypothesis``)."""
    dirs = {""} | {posixpath.dirname(p) for p in _tracked(context) if p.endswith(".py")}
    paths = []
    for directory in sorted(dirs):
        base = f"{directory}/" if directory else ""
        paths += [f"{base}__pycache__/mod.cpython-312.pyc", f"{base}mod.pyc",
                  f"{base}pkg/__pycache__/mod.cpython-311.pyc",
                  f"{base}.pytest_cache/v/cache/lastfailed",
                  f"{base}.hypothesis/examples/0a/1b"]
    return paths


def _context_copy(dest: Path, context: str, dockerfile: str, probe: str, probe_text: str,
                  bytecode: list[str] | tuple[str, ...] = ()) -> Path:
    """A small copy of *context*: its real .dockerignore and Dockerfile, the
    probe file and any *bytecode* files. Never the checkout itself."""
    source = REPO_ROOT / context
    dest.mkdir(parents=True)
    if (source / ".dockerignore").exists():
        shutil.copyfile(source / ".dockerignore", dest / ".dockerignore")
    files = {dockerfile: (source / dockerfile).read_bytes(), probe: probe_text.encode()}
    files.update({rel: b"\x00bytecode" for rel in bytecode})
    for rel, data in files.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(data)
    return dest


def _image_asset(directory: Path, dockerfile: str, outdir: Path) -> tuple[str, Path]:
    """CDK's own fingerprint and staged copy of an image context: the asset
    hash that names the image tag, and the files CDK would send to the build."""
    import aws_cdk as cdk
    from aws_cdk import aws_ecr_assets as ecr_assets

    cdk.AssetStaging.clear_asset_hash_cache()
    app = cdk.App(outdir=str(outdir))
    asset = ecr_assets.DockerImageAsset(cdk.Stack(app, "Probe"), "Image",
                                        directory=str(directory), file=dockerfile)
    return asset.asset_hash, outdir / f"asset.{asset.asset_hash}"


def test_every_tracked_dockerfile_is_in_the_bytecode_check():
    tracked = {p for p in _tracked(".") if _DOCKERFILE_RE.search(p)}
    listed = {posixpath.normpath(posixpath.join(c, d)) for c, d, _ in _IMAGE_CONTEXTS}
    assert listed == tracked, (
        f"add new image contexts to _IMAGE_CONTEXTS: missing {sorted(tracked - listed)}, "
        f"stale {sorted(listed - tracked)}")


@pytest.mark.parametrize(("context", "dockerfile", "probe"), _IMAGE_CONTEXTS,
                         ids=[f"{c}:{d}" for c, d, _ in _IMAGE_CONTEXTS])
def test_python_bytecode_never_changes_an_image_asset_hash(tmp_path, context, dockerfile, probe):
    """CDK fingerprints and stages an image context with Docker ignore rules,
    which anchor at the context root: ``__pycache__/`` and ``*.pyc`` do not
    match ``agents/__pycache__/``. Bytecode left by tests or imports would then
    change the image tag between two packages of the same commit."""
    clean, _ = _image_asset(_context_copy(tmp_path / "clean", context, dockerfile, probe, "a\n"),
                            dockerfile, tmp_path / "out-clean")
    edited, _ = _image_asset(_context_copy(tmp_path / "edited", context, dockerfile, probe, "b\n"),
                             dockerfile, tmp_path / "out-edited")
    assert edited != clean, f"{probe} is not in the {context} fingerprint, so this check proves nothing"

    bytecode = _bytecode_paths(context)
    dirty, staged = _image_asset(
        _context_copy(tmp_path / "dirty", context, dockerfile, probe, "a\n", bytecode),
        dockerfile, tmp_path / "out-dirty")
    assert staged.is_dir(), f"CDK staged no copy of the {context} context at {staged}"
    leaked = sorted(rel for rel in bytecode if (staged / rel).exists())
    assert leaked == [], (
        f"{context}/.dockerignore lets Python bytecode into the image context: {leaked[:6]}")
    assert dirty == clean, f"Python bytecode changed the {context} asset hash"


@pytest.mark.parametrize("dockerfile", sorted(d for c, d, _ in _IMAGE_CONTEXTS if c == "."))
def test_cdk_context_cache_never_enters_a_repo_root_image(tmp_path, dockerfile):
    """``cdk.context.json`` is the CDK CLI's local lookup cache (gitignored).
    It can hold account and Availability Zone values from whoever ran a
    lookup, and none of these images COPY it, so it must not reach their
    contexts, the workshop source archive or the asset hash."""
    probe = next(p for c, d, p in _IMAGE_CONTEXTS if c == "." and d == dockerfile)
    clean, _ = _image_asset(_context_copy(tmp_path / "clean", ".", dockerfile, probe, "a\n"),
                            dockerfile, tmp_path / "out-clean")
    cached = _context_copy(tmp_path / "cached", ".", dockerfile, probe, "a\n")
    (cached / "cdk.context.json").write_text('{"availability-zones:account=111122223333:region=us-east-1": []}\n')
    with_cache, staged = _image_asset(cached, dockerfile, tmp_path / "out-cached")
    assert not (staged / "cdk.context.json").exists(), "cdk.context.json is in the image context"
    assert with_cache == clean, "cdk.context.json changed the asset hash"


# --------------------------------------------------------------------------- #
# The repo-root context is an allowlist
# --------------------------------------------------------------------------- #
#: The images built from the repository root (all under docker/).
_REPO_ROOT_DOCKERFILES = tuple(sorted(d for c, d, _ in _IMAGE_CONTEXTS if c == "."))
#: Files none of those images copies. Editing (or, for an untracked one,
#: creating) any of them must leave every repo-root image hash unchanged.
_NOT_COPIED = ("app.py", "CONTRIBUTING.md", "README.md", "cdk.json", "package.json",
               "docs/deployment.md", "requirements.txt", "requirements/infra.in",
               "stacks/erp_lambda_stack.py", ".ci-diag/samples.jsonl")


def _copy_sources(dockerfile: Path) -> list[str]:
    """Context paths a Dockerfile COPYs or ADDs (stage copies excluded)."""
    text = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    sources = []
    for line in text.splitlines():
        words = line.split()
        if not words or words[0].upper() not in ("COPY", "ADD"):
            continue
        if any(word.startswith("--from=") for word in words[1:]):
            continue
        assert "[" not in line, f"{dockerfile.name}: JSON-form COPY is not parsed here: {line!r}"
        sources += [word for word in words[1:] if not word.startswith("--")][:-1]
    return sources


def _repo_root_sources() -> dict[str, list[str]]:
    return {d: _copy_sources(REPO_ROOT / d) for d in _REPO_ROOT_DOCKERFILES}


def _allowlisted(rel: str) -> bool:
    """Whether a path in a repo-root context is one any repo-root image needs."""
    if rel == ".dockerignore" or rel in _REPO_ROOT_DOCKERFILES:
        return True
    for sources in _repo_root_sources().values():
        for source in sources:
            if rel == source or (source.endswith("/") and rel.startswith(source)):
                return True
    return False


def _tracked_under(source: str) -> list[str]:
    tracked = _tracked(".")
    if source.endswith("/"):
        return [p for p in tracked if p.startswith(source)]
    return [p for p in tracked if p == source]


@contextlib.contextmanager
def _edited(path: Path):
    """Append a line to *path* (creating it if needed), then restore it."""
    existed = path.exists()
    original = path.read_bytes() if existed else b""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original + b"\n# edited\n")
    try:
        yield
    finally:
        if existed:
            path.write_bytes(original)
        else:
            path.unlink()


@pytest.fixture(scope="module")
def repo_root_copy(tmp_path_factory) -> Path:
    return _clean_copy(tmp_path_factory.mktemp("repo_root_context") / "src")


def test_repo_root_context_is_an_allowlist():
    """The repo-root .dockerignore excludes everything, then re-includes
    exactly what the repo-root Dockerfiles COPY, then drops local residue
    inside the re-included trees (after the re-includes, so it applies)."""
    rules = _dockerignore(REPO_ROOT / ".dockerignore")
    assert rules[0] == "*", "the repo-root .dockerignore must start by excluding everything"
    expected = {"!.dockerignore"} | {f"!{d}" for d in _REPO_ROOT_DOCKERFILES}
    for sources in _repo_root_sources().values():
        expected |= {f"!{s}**" if s.endswith("/") else f"!{s}" for s in sources}
    includes = [rule for rule in rules if rule.startswith("!")]
    assert sorted(set(includes)) == sorted(expected), (
        "re-includes must be exactly the repo-root Dockerfiles' COPY sources")
    after = rules[max(i for i, rule in enumerate(rules) if rule.startswith("!")) + 1:]
    for residue in ("**/__pycache__/", "**/*.pyc"):
        assert residue in after, f"{residue} must come after the re-includes"


@pytest.mark.parametrize("dockerfile", _REPO_ROOT_DOCKERFILES)
def test_repo_root_image_context_holds_its_sources_and_nothing_else(
        repo_root_copy, tmp_path, dockerfile):
    """CDK's own staged copy of the context: every COPY source whole, the
    Dockerfile itself, and no file outside the allowlist."""
    _, staged = _image_asset(repo_root_copy, dockerfile, tmp_path / "out")
    assert (staged / dockerfile).is_file(), f"{dockerfile} is not in its own context"
    for source in _copy_sources(REPO_ROOT / dockerfile):
        expected = _tracked_under(source)
        assert expected, f"{dockerfile} copies {source}, which has no tracked file"
        missing = [rel for rel in expected if not (staged / rel).is_file()]
        assert missing == [], f"{dockerfile}: {source} is missing from the context: {missing[:6]}"
    files = sorted(p.relative_to(staged).as_posix() for p in staged.rglob("*")
                   if p.is_file() or p.is_symlink())
    outside = [rel for rel in files if not _allowlisted(rel)]
    assert outside == [], f"{dockerfile} context holds files no repo-root image copies: {outside[:10]}"


@pytest.mark.parametrize("rel", _NOT_COPIED)
def test_files_no_repo_root_image_copies_leave_their_hashes_alone(repo_root_copy, tmp_path, rel):
    assert not _allowlisted(rel), f"{rel} is copied by an image, so it cannot be in this list"
    before = {d: _image_asset(repo_root_copy, d, tmp_path / f"a{i}")[0]
              for i, d in enumerate(_REPO_ROOT_DOCKERFILES)}
    with _edited(repo_root_copy / rel):
        after = {d: _image_asset(repo_root_copy, d, tmp_path / f"b{i}")[0]
                 for i, d in enumerate(_REPO_ROOT_DOCKERFILES)}
    moved = [d for d in _REPO_ROOT_DOCKERFILES if after[d] != before[d]]
    assert moved == [], f"editing {rel} changed the asset hash of {moved}"


@pytest.mark.parametrize("dockerfile", _REPO_ROOT_DOCKERFILES)
def test_every_copied_source_moves_its_image_hash(repo_root_copy, tmp_path, dockerfile):
    """The allowlist must not hide a file the image needs: editing one file
    under each COPY source changes that image's hash."""
    before, _ = _image_asset(repo_root_copy, dockerfile, tmp_path / "before")
    for n, source in enumerate(_copy_sources(REPO_ROOT / dockerfile)):
        target = sorted(_tracked_under(source), key=lambda p: (not p.endswith(".py"), p))[0]
        with _edited(repo_root_copy / target):
            after, _ = _image_asset(repo_root_copy, dockerfile, tmp_path / f"edit{n}")
        assert after != before, f"editing {target} did not change the {dockerfile} asset hash"


def test_stacks_never_copy_into_the_source_tree_at_synth():
    """No shutil copy/copytree/move or open(..., 'w') in stacks/ except the
    staging helper, which writes under cdk.out."""
    offenders = []
    for path in sorted((REPO_ROOT / "stacks").glob("*.py")):
        if path.name == "_staging.py":
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("shutil.copytree(", "shutil.copy2(", "shutil.copy(", "shutil.move(",
                       "shutil.rmtree("):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders
    app_py = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "generate_all()" not in app_py, "app.py must not generate datasets at synth"


def test_staging_helper_writes_under_the_app_outdir(tmp_path):
    import aws_cdk as cdk

    from stacks._staging import STAGING_DIRNAME, stage_tree

    src = tmp_path / "lambda-src"
    src.mkdir()
    (src / "index.mjs").write_text("export const handler = () => 1;\n")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "junk.js").write_text("x")
    extra = tmp_path / "config.json"
    extra.write_text("{}")
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    staged = Path(stage_tree(app, "unit", str(src), {"config.json": str(extra)}))
    assert staged == tmp_path / "cdk.out" / STAGING_DIRNAME / "unit"
    assert sorted(p.name for p in staged.iterdir()) == ["config.json", "index.mjs"]
    assert not (src / "config.json").exists(), "the source tree must stay untouched"


@pytest.fixture(scope="session")
def highbyte_synth(tmp_path_factory):
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    before = subprocess.run(["git", "status", "--porcelain", "--", "highbyte", "config", "stacks"],  # nosec B603 B607
                            cwd=REPO_ROOT, capture_output=True, text=True).stdout
    out_dir = tmp_path_factory.mktemp("cdk_synth_hygiene")
    env = {**os.environ, "AWS_DEFAULT_REGION": "us-east-1",
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [str(CDK_BIN), "synth", "ADTHighByteOpcuaStack", "-c", "region=us-east-1",
         "-c", "highbyte_enabled=true", "-c", "highbyte_accept_eula=true",
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    if not (out_dir / "ADTHighByteOpcuaStack.template.json").exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    after = subprocess.run(["git", "status", "--porcelain", "--", "highbyte", "config", "stacks"],  # nosec B603 B607
                           cwd=REPO_ROOT, capture_output=True, text=True).stdout
    return out_dir, before, after


def test_synth_leaves_the_checkout_untouched(highbyte_synth):
    out_dir, before, after = highbyte_synth
    assert after == before, f"synth changed the working tree:\n{after}"
    assert not (REPO_ROOT / "highbyte/components/opc-server/datasets").exists()
    assert not (REPO_ROOT / "highbyte/lambda/highbyte-setup/intelligencehub-configuration.json").exists()


def test_synth_stages_the_opc_context_from_the_allowlist(highbyte_synth):
    out_dir, _, _ = highbyte_synth
    assets = json.loads((out_dir / "ADTHighByteOpcuaStack.assets.json").read_text())
    opc = [a for a in assets["dockerImages"].values()
           if a["source"].get("dockerFile") == "components/opc-server/Dockerfile"]
    assert len(opc) == 1, "exactly one OPC image asset shared by the three sites"
    staged = out_dir / opc[0]["source"]["directory"]
    files = sorted(p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file())
    assert all(f == ".dockerignore" or f.startswith(("components/opc-server/", "datasets/"))
               for f in files), files
    assert not any("node_modules" in f or "lambda/" in f for f in files)
    staging = out_dir / "adt-staging" / "highbyte-setup"
    assert (staging / "index.mjs").exists() and (staging / "intelligencehub-configuration.json").exists()


@pytest.fixture(scope="session")
def quick_interceptor_synth(tmp_path_factory):
    """ADR-0079: synth the agentcore stack in delegated-token mode."""
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    watched = ["lambda-quick-interceptor", "agentic_digital_thread/agents/auth", "stacks"]
    before = subprocess.run(["git", "status", "--porcelain", "--", *watched],  # nosec B603 B607
                            cwd=REPO_ROOT, capture_output=True, text=True).stdout
    out_dir = tmp_path_factory.mktemp("cdk_synth_quick_interceptor")
    env = {**os.environ, "AWS_DEFAULT_REGION": "us-east-1",
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [str(CDK_BIN), "synth", "ADTAgentCoreStack", "--no-lookups", "-c", "region=us-east-1",
         "-c", "quick_api_enabled=true",
         "-c", "quick_oidc_discovery_url=https://idp.example.com/.well-known/openid-configuration",
         "-c", "quick_oidc_audience=https://gateway.example.com/mcp",
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    if not (out_dir / "ADTAgentCoreStack.template.json").exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    after = subprocess.run(["git", "status", "--porcelain", "--", *watched],  # nosec B603 B607
                           cwd=REPO_ROOT, capture_output=True, text=True).stdout
    return out_dir, before, after


def test_quick_interceptor_is_staged_under_the_outdir(quick_interceptor_synth):
    out_dir, before, after = quick_interceptor_synth
    assert after == before, f"synth changed the working tree:\n{after}"
    assert not (REPO_ROOT / "lambda-quick-interceptor" / "identity_claim.py").exists()
    staging = out_dir / "adt-staging" / "quick-interceptor"
    assert sorted(p.name for p in staging.iterdir()) == ["identity_claim.py", "index.py"]


# --------------------------------------------------------------------------- #
# Full synth from a clean copy of the tracked files
# --------------------------------------------------------------------------- #
#: The i3X Explorer bundle (gitignored, built by deploy.sh). Only the app
#: Dockerfile copies static/.
_I3X_BUNDLE = Path("agentic_digital_thread/static/i3x-explorer")
#: (stack, Dockerfile) of the two images built from agentic_digital_thread/.
_APP_IMAGE = ("ADTComputeStack", "Dockerfile")
_NON_APP_IMAGES = (("ADTComputeStack", "Dockerfile.runtime"),)


def _clean_copy(dest: Path) -> Path:
    """The working-tree content of every tracked file, and nothing else:
    no bytecode, no bundle, no local CDK context."""
    out = subprocess.run(["git", "ls-files", "-z"],  # nosec B603 B607
                         cwd=REPO_ROOT, capture_output=True, check=True).stdout
    for rel in filter(None, out.decode().split("\0")):
        source = REPO_ROOT / rel
        if not source.exists() and not source.is_symlink():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)
    return dest


def _offline_env(tmp: Path) -> dict[str, str]:
    """The synth env with every AWS credential source blocked, and with no
    PYTHONDONTWRITEBYTECODE, so app.py's own setting is what is tested."""
    empty = tmp / "empty-aws-config"
    empty.write_text("")
    env = {key: value for key, value in os.environ.items()
           if key not in {"AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
                          "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ROLE_ARN",
                          "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                          "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "CDK_DEFAULT_ACCOUNT",
                          "PYTHONDONTWRITEBYTECODE"}}
    env.update({"AWS_CONFIG_FILE": str(empty), "AWS_SHARED_CREDENTIALS_FILE": str(empty),
                "AWS_EC2_METADATA_DISABLED": "true", "AWS_DEFAULT_REGION": "us-east-1",
                "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")})
    return env


def _synth(copy: Path, out_dir: Path, env: dict[str, str]) -> dict[tuple[str, str], str]:
    """``cdk synth`` the copy into *out_dir*; {(stack, Dockerfile): asset hash}."""
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [str(CDK_BIN), "synth", "--all", "-c", "region=us-east-1",
         "--output", str(out_dir), "--quiet"],
        cwd=str(copy), env=env, capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, f"synth of the clean copy failed:\n{result.stderr[-3000:]}"
    hashes = {}
    for manifest in sorted(out_dir.glob("*.assets.json")):
        stack = manifest.name[: -len(".assets.json")]
        for asset_hash, spec in json.loads(manifest.read_text()).get("dockerImages", {}).items():
            hashes[(stack, spec["source"].get("dockerFile", "Dockerfile"))] = asset_hash
    return hashes


def _bytecode_in(tree: Path) -> list[str]:
    return sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")
                  if p.name == "__pycache__" or p.suffix == ".pyc")


@pytest.fixture(scope="session")
def clean_copy_synths(tmp_path_factory):
    """Two offline synths of one clean copy: the first with no bundle, the
    second after adding a bundle file and changing another."""
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    root = tmp_path_factory.mktemp("clean_copy_synth")
    copy = _clean_copy(root / "src")
    env = _offline_env(root)
    assert _bytecode_in(copy) == [], "the clean copy started with bytecode"
    first = _synth(copy, root / "out1", env)
    bytecode = _bytecode_in(copy)
    bundle = copy / _I3X_BUNDLE
    (bundle / "assets").mkdir(parents=True, exist_ok=True)
    (bundle / "index.html").write_text("<!doctype html><title>i3X Explorer</title>\n")
    (bundle / "assets" / "index.js").write_text("console.log('a');\n")
    second = _synth(copy, root / "out2", env)
    return first, second, bytecode


def test_synth_writes_no_bytecode_into_the_checkout(clean_copy_synths):
    """``cdk synth`` writes nothing into the checkout, bytecode included. The
    stacks load app modules by path (model catalog, SFC config), and Python
    would cache their bytecode beside the sources."""
    _, _, bytecode = clean_copy_synths
    assert bytecode == [], f"synth wrote bytecode into the checkout: {bytecode[:8]}"


def test_i3x_bundle_moves_only_the_app_image(clean_copy_synths):
    """Only the app Dockerfile copies static/. The AgentCore runtime shares
    its context, so without its own exclude a rebuilt bundle would give it a
    new tag and an update for nothing."""
    first, second, _ = clean_copy_synths
    for key in (_APP_IMAGE, *_NON_APP_IMAGES):
        assert key in first and key in second, f"no image asset for {key}"
    assert second[_APP_IMAGE] != first[_APP_IMAGE], (
        "the bundle is not in the app image fingerprint, so this check proves nothing")
    moved = [key for key in _NON_APP_IMAGES if second[key] != first[key]]
    assert moved == [], f"the i3X bundle changed the asset hash of {moved}"


# --------------------------------------------------------------------------- #
# Optional opt-in parity check: pin the two always-on agentic_digital_thread
# image asset hashes against a chosen baseline ref, so nothing slips into the
# always-on Docker build context between two trees.
# --------------------------------------------------------------------------- #
#: Env var naming an OPT-IN baseline ref to pin the two always-on
#: agentic_digital_thread image asset hashes against (for example
#: ADT_PARITY_BASELINE=a0a4cf3c). Unset: the comparison is skipped. A
#: legitimate later app change moves these hashes by design, and a shallow CI
#: clone may not even contain the baseline ref, so there is no permanent pin.
_IMAGE_PARITY_BASELINE_ENV = "ADT_PARITY_BASELINE"
_ALWAYS_ON_IMAGES = tuple((c, d) for c, d, _ in _IMAGE_CONTEXTS if c == "agentic_digital_thread")


def _archive_context(ref: str, context: str, dest: Path) -> Path:
    """Extract the tracked files of *context* at git *ref* into *dest* — the
    baseline build context, with no working-tree residue."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(  # nosec B603 B607 - fixed argv, internal ref
        ["git", "archive", ref, "--", context], cwd=REPO_ROOT,
        capture_output=True, check=True).stdout
    tar = dest / "ctx.tar"
    tar.write_bytes(archive)
    subprocess.run(["tar", "-xf", str(tar), "-C", str(dest)],  # nosec B603 B607
                   check=True)
    tar.unlink()
    return dest / context


def _ref_in_clone(ref: str) -> bool:
    """Whether ``ref`` resolves in this clone (a shallow CI clone may lack it)."""
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],  # nosec B603 B607
                          cwd=REPO_ROOT, capture_output=True).returncode == 0


@pytest.mark.skipif(importlib.util.find_spec("aws_cdk") is None,
                    reason="aws-cdk-lib not importable")
@pytest.mark.parametrize(("context", "dockerfile"), _ALWAYS_ON_IMAGES,
                         ids=[d for _c, d in _ALWAYS_ON_IMAGES])
def test_always_on_image_hashes_match_an_opt_in_baseline(tmp_path, context, dockerfile):
    """OPT-IN: when ``ADT_PARITY_BASELINE=<ref>`` is set and that ref is
    in the clone, CDK's own asset hash of each always-on agentic_digital_thread
    image must equal the ref's, proving nothing entered the always-on Docker
    build context between the two trees. Skipped when the env var is unset or
    when the ref is absent from the clone: a legitimate later app change moves
    these hashes by design, so pinning a fixed baseline forever would fail on
    every such tree."""
    baseline_ref = os.environ.get(_IMAGE_PARITY_BASELINE_ENV, "").strip()
    if not baseline_ref:
        pytest.skip(f"set {_IMAGE_PARITY_BASELINE_ENV}=<ref> to pin the always-on "
                    "image hashes against that ref")
    if not _ref_in_clone(baseline_ref):
        pytest.skip(f"{_IMAGE_PARITY_BASELINE_ENV}={baseline_ref} is not in this clone "
                    "(e.g. a shallow CI checkout); nothing to compare against")
    baseline_ctx = _archive_context(baseline_ref, context, tmp_path / "base")
    head_ctx = _clean_copy(tmp_path / "head") / context
    base_hash, _ = _image_asset(baseline_ctx, dockerfile, tmp_path / "out-base")
    head_hash, _ = _image_asset(head_ctx, dockerfile, tmp_path / "out-head")
    assert head_hash == base_hash, (
        f"{context}/{dockerfile} asset hash moved from {baseline_ref} ({base_hash}) to "
        f"{head_hash}: something entered the always-on Docker build context")
