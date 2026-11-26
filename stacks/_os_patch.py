# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""OS patch level for the container images CDK builds from a Dockerfile.

A CDK image asset is content-addressed: its hash covers the build context and
the build options, not the date. When nothing in the context changes, a deploy
reuses the image already in ECR and never re-runs the Dockerfile's
``apt-get upgrade`` / ``apk upgrade`` / ``dnf upgrade`` step. Even when the
context does change, the local Docker layer cache can replay an old upgrade
layer, because the ``FROM`` digest and the ``RUN`` line above it are
unchanged. Either way the image ships packages that were fixed upstream
weeks earlier (seen 2026-10: a curl image built on 10-17 still carried a
build fixed in Debian stable-security on 10-14).

``OS_PATCH_LEVEL`` is the one deliberate lever for that. Bumping it changes
every patched image's asset hash, so the next deploy rebuilds them, and
``cache_disabled`` makes that rebuild run ``--no-cache`` so the upgrade step
really executes. It is a hand-edited constant, never a computed date: a value
that changes on every synth would rebuild every image on every deploy (the
reason ``APP_VERSION`` is a runtime env var, not a build-arg).

To pick up new OS patches: set ``OS_PATCH_LEVEL`` to today's date and deploy.
"""
from __future__ import annotations

#: Bump to today's date (YYYY-MM-DD) to rebuild every patched image.
OS_PATCH_LEVEL = "2026-10-20"


def patched_image_options() -> dict:
    """Keyword arguments for ``ecs.ContainerImage.from_asset`` /
    ``ecr_assets.DockerImageAsset`` on an image whose Dockerfile runs an OS
    package upgrade."""
    return {
        "extra_hash": f"os-patch-level:{OS_PATCH_LEVEL}",
        "cache_disabled": True,
    }
