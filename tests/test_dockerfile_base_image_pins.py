# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every base image in every tracked Dockerfile is pinned by digest.

A tag such as ``python:3.12`` is mutable: the registry can serve different
bytes for it tomorrow, so a rebuild of the same revision can silently change
the deployed image. Each ``FROM`` therefore names a ``@sha256:<64 hex>``
digest (the tag may stay in front of it for readability). There is no
per-file exception list.

Use the digest of the multi-architecture image index for the tag, not a
single-platform manifest, so ``docker build --platform`` (and an image whose
``FROM --platform`` pin differs from the build host) still selects the right
platform. To move to a newer base, look up the tag's current index digest in
the registry and replace the digest on every ``FROM`` that uses that tag.

Allowed without a digest: a reference to an earlier build stage of the same
file (``FROM frontend``) and ``scratch``, since neither pulls an image.
"""
from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed internal argv list (git ls-files), no shell
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def _is_dockerfile(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered == "dockerfile"
        or lowered.startswith("dockerfile.")
        or lowered.endswith(".dockerfile")
        or lowered.startswith("containerfile")
    )


def _tracked_dockerfiles() -> list[Path]:
    out = subprocess.run(  # nosec B603 B607 - fixed argv, no shell; git resolved from PATH by design
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True,
    ).stdout
    paths = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        path = REPO_ROOT / raw.decode()
        if _is_dockerfile(path.name) and path.is_file():
            paths.append(path)
    return sorted(paths)


def _instructions(text: str) -> list[str]:
    """Logical Dockerfile instructions: comments and blank lines dropped,
    backslash continuation lines joined."""
    logical: list[str] = []
    buf = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        logical.append(buf + stripped)
        buf = ""
    if buf.strip():
        logical.append(buf.strip())
    return logical


def unpinned_from_lines(text: str) -> list[str]:
    """The ``FROM`` instructions in ``text`` whose image has no sha256 digest."""
    stages: set[str] = set()
    offenders: list[str] = []
    for instruction in _instructions(text):
        words = instruction.split()
        if words[0].upper() != "FROM":
            continue
        args = [word for word in words[1:] if not word.startswith("--")]
        image = args[0] if args else ""
        pulls_nothing = image.lower() in stages or image.lower() == "scratch"
        if not pulls_nothing and not _DIGEST_RE.search(image):
            offenders.append(instruction)
        if len(args) >= 3 and args[1].upper() == "AS":
            stages.add(args[2].lower())
    return offenders


def test_every_tracked_dockerfile_base_image_is_digest_pinned():
    offenders = []
    for path in _tracked_dockerfiles():
        for instruction in unpinned_from_lines(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {instruction}")
    assert offenders == [], "base images must be pinned by @sha256 digest:\n" + "\n".join(offenders)


def from_images(text: str) -> list[str]:
    """The pulled image of every ``FROM`` in ``text`` (stage references and
    ``scratch`` left out)."""
    stages: set[str] = set()
    images: list[str] = []
    for instruction in _instructions(text):
        words = instruction.split()
        if words[0].upper() != "FROM":
            continue
        args = [word for word in words[1:] if not word.startswith("--")]
        image = args[0] if args else ""
        if image and image.lower() not in stages and image.lower() != "scratch":
            images.append(image)
        if len(args) >= 3 and args[1].upper() == "AS":
            stages.add(args[2].lower())
    return images


def digests_by_tag(images: list[str]) -> dict[str, set[str]]:
    """``repository:tag`` -> the digests it is pinned to, for digest-pinned
    images (unpinned ones are the other test's business)."""
    by_tag: dict[str, set[str]] = {}
    for image in images:
        name, sep, digest = image.partition("@")
        if sep:
            by_tag.setdefault(name, set()).add(digest)
    return by_tag


def test_each_base_image_tag_is_pinned_to_one_digest():
    """Two Dockerfiles that name the same tag build on the same bytes, so a
    base-image update moves every one of them together and none is left on
    an older digest without anyone deciding it."""
    images = []
    for path in _tracked_dockerfiles():
        images.extend(from_images(path.read_text(encoding="utf-8")))
    split = {tag: sorted(digests) for tag, digests in digests_by_tag(images).items()
             if len(digests) > 1}
    assert split == {}, "one tag, several digests (pin every FROM of a tag to one digest):\n" + \
        "\n".join(f"{tag}: {', '.join(d)}" for tag, d in sorted(split.items()))


def test_digest_grouping_sees_a_split_tag():
    other = "sha256:" + "1" * 64
    images = from_images(
        f"FROM example.com/base:1@{_DIGEST} AS build\nFROM build\n"
        f"FROM example.com/base:1@{other}\nFROM scratch\nFROM example.com/base:2@{_DIGEST}\n")
    assert digests_by_tag(images) == {
        "example.com/base:1": {_DIGEST, other}, "example.com/base:2": {_DIGEST}}


def test_discovery_reads_both_dockerfile_naming_styles():
    """The scan above proves nothing if it finds no files, so check it sees
    plain ``Dockerfile`` and ``Dockerfile.<name>`` files and that each one
    has a FROM to check."""
    paths = _tracked_dockerfiles()
    names = {path.name for path in paths}
    assert "Dockerfile" in names
    assert any(name.startswith("Dockerfile.") for name in names)
    for path in paths:
        instructions = _instructions(path.read_text(encoding="utf-8"))
        assert any(i.split()[0].upper() == "FROM" for i in instructions), path


_DIGEST = "sha256:" + "0" * 64


@pytest.mark.parametrize("text", [
    "FROM public.ecr.aws/lambda/python:3.12\n",
    "FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim\n",
    "from public.ecr.aws/docker/library/python:3.11-slim\n",
    "FROM \\\n    public.ecr.aws/lambda/python:3.11\n",
    "ARG BASE=public.ecr.aws/lambda/python:3.11\nFROM ${BASE}\n",
    "FROM public.ecr.aws/lambda/python:3.11@sha256:abc123\n",
    "FROM public.ecr.aws/lambda/python@sha512:" + "0" * 128 + "\n",
    f"FROM example.com/base@{_DIGEST} AS build\nFROM other\n",
    "FROM\n",
])
def test_pin_check_flags_an_unpinned_from(text):
    assert unpinned_from_lines(text)


@pytest.mark.parametrize("text", [
    f"FROM public.ecr.aws/lambda/python:3.12@{_DIGEST}\n",
    f"FROM public.ecr.aws/lambda/python@{_DIGEST}\n",
    f"FROM --platform=linux/arm64 example.com/node:20@{_DIGEST} AS frontend\n"
    f"FROM --platform=linux/arm64 example.com/python:3.12@{_DIGEST}\n"
    "COPY --from=frontend /fe/dist dist\n",
    f"FROM example.com/base@{_DIGEST} AS build\nFROM build\n",
    "FROM scratch\n",
    f"# FROM unpinned:latest in a comment\nFROM example.com/base@{_DIGEST}\n",
])
def test_pin_check_accepts_digest_pins_and_stage_references(text):
    assert unpinned_from_lines(text) == []
