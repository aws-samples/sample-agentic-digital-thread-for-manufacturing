# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""NOTICE names every external container image the deployment pulls.

The dependency-license gate (scripts/check_dependency_licenses.py) reads the
Python and npm locks, not container contents, so the images pinned in
stacks/_external_images.py need their own check: each one is attributed in
NOTICE, and the BusyBox entry carries the exact digests the stacks pin, so a
pin move without a NOTICE update fails here. The entry also says how the
image is delivered (only the ordinary deploy pulls it; no workshop package
carries or copies it), and that claim is checked against the packaging code
that decides the workshop's images.
"""
from __future__ import annotations

import re
from pathlib import Path

from stacks._external_images import EXTERNAL_IMAGES, HUB_TASK_PLATFORM

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTICE = (REPO_ROOT / "NOTICE").read_text()

#: Pinned image name -> the NOTICE heading that attributes it.
NOTICE_HEADING = {
    "highbyte-hub": "HighByte Intelligence Hub",
    "settings-seed": "BusyBox 1.36.1",
}

#: The docker-library/busybox commit whose latest-1/glibc/amd64 OCI layout
#: holds the pinned linux/amd64 manifest (the manifest's
#: org.opencontainers.image.revision annotation in the pinned index, read
#: from Amazon ECR Public and checked against the GitHub tree on 2026-10-21).
BUSYBOX_AMD64_REVISION = "c1375496373e76f680b1ef5f713500e98921a45c"


def _entry(heading: str) -> str:
    """The NOTICE block that starts with *heading* (up to the next ``---``)."""
    start = NOTICE.index("\n" + heading + "\n")
    end = NOTICE.find("\n---", start + 1)
    return NOTICE[start:end if end != -1 else len(NOTICE)]


def _flat(text: str) -> str:
    """*text* with its line wrapping folded to single spaces."""
    return re.sub(r"\s+", " ", text)


def test_every_pinned_external_image_is_attributed():
    assert set(EXTERNAL_IMAGES) == set(NOTICE_HEADING)
    for heading in NOTICE_HEADING.values():
        assert "\n" + heading + "\n" in NOTICE, heading


def test_busybox_entry_names_the_exact_image_license_and_source():
    spec = EXTERNAL_IMAGES["settings-seed"]
    entry = _entry(NOTICE_HEADING["settings-seed"])
    flat = _flat(entry)
    assert spec["source"] in entry
    assert spec["index_digest"] in entry
    # The deploy.sh --highbyte task pulls this platform image.
    assert spec["platform_digests"][HUB_TASK_PLATFORM] in entry
    assert "1.36.1-glibc" in entry
    assert BUSYBOX_AMD64_REVISION in entry
    assert "latest-1/glibc" in entry
    assert "GPL-2.0-only" in entry
    assert "https://busybox.net" in entry
    # The glibc build carries GNU C Library files from Debian 13.
    assert "GNU C Library" in flat and "Debian 13" in flat
    assert "LGPL-2.1-or-later" in entry
    assert "published upstream" in flat
    assert entry.isascii()


def test_busybox_entry_says_only_the_customers_own_deploy_pulls_it():
    """BusyBox reaches only the ordinary deploy.sh --highbyte deployment,
    which pulls it from Amazon ECR Public in the customer's own account, and
    the entry says so."""
    flat = _flat(_entry(NOTICE_HEADING["settings-seed"]))
    assert "deploy.sh --highbyte" in flat
    assert "customer's own account" in flat
    assert "Amazon Elastic Container Registry Public (Amazon ECR Public)" in flat
    assert "does not ship, copy or redistribute BusyBox" in flat
    assert "settings-seed-image" not in flat
    assert "copies it" not in flat
