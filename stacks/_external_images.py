# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The HighByte hub task's two registry images, pinned by digest.

This file is the ONE place the pins live. ``stacks/highbyte_opcua_stack.py``
runs ``<repository>@<HUB_TASK_PLATFORM digest>`` from here in an ordinary
``deploy.sh --highbyte`` deployment, which pulls BusyBox from Amazon ECR
Public in its own account. A tag can move, and ECS pulls the image again at
every task start, so the deployment never runs a tag. ``tests/test_registry_image_
digest_pins.py`` holds the two together.

No CDK imports: tests load it by path, without the CDK app.

Fields per image: ``source`` is the published tag the digests were read from
(provenance, and the hub release the tests read); ``index_digest`` is the
top-level manifest or index digest; ``platform_digests`` maps each platform the
task runs to that platform's manifest digest. The hub task runs linux/amd64:
its task definition declares no runtime platform, so AWS Fargate runs it on
LINUX/X86_64. The pinned 4.5.3 hub tag is a single-platform amd64 manifest, so
its index digest is its amd64 digest; the settings-seed container runs in that
same amd64 task. ``platform_config_digests`` (hub only) pins the image CONFIG
digest the workshop's hub load mode verifies against (``package.py
--hub-image-source load``): docker load + push writes a new manifest, so the
manifest pin cannot match a loaded image, while the config blob is carried
unchanged. Measured 2026-10-11 from the Docker Hub manifest (``docker buildx
imagetools inspect --raw``, ``config.digest``) and equal to the local image ID
of that manifest.

To move to a new release, read the new tag's digests from the registry and
change them here; both paths follow.
"""
from __future__ import annotations

#: The platform the hub task (and its settings-seed init container) runs.
HUB_TASK_PLATFORM = "linux/amd64"

EXTERNAL_IMAGES = {
    "highbyte-hub": {
        "source": "highbyte/intelligence-hub:4.5.3",
        "index_digest": "sha256:2255d08fd39b225f5cecce2451d1aae5c10e5382ee8ac42719dbb2dcc6c3aa90",
        "platform_digests": {
            "linux/amd64": "sha256:2255d08fd39b225f5cecce2451d1aae5c10e5382ee8ac42719dbb2dcc6c3aa90",
        },
        "platform_config_digests": {
            "linux/amd64": "sha256:9a0c0b8b245df17a2849b18b0e26de5aaa6479e9e8249995df1c2c6a48390435",
        },
    },
    "settings-seed": {
        "source": "public.ecr.aws/docker/library/busybox:1.36",
        "index_digest": "sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662",
        "platform_digests": {
            "linux/amd64": "sha256:b7f3d86d6e84fc17718c48bcde1450807faa2d56704205c697b4bd5df7b9e29f",
            "linux/arm64": "sha256:bd44eb136a95dcc8dc58995e43abc40a413f2e8e3d4a2aae6bccbe94686acb05",
        },
    },
}


def source_repository(source: str) -> str:
    """*source* without its ``:tag``. Only a tag in the LAST path segment is
    removed (a registry host may carry a ``:port``)."""
    head, slash, last = source.rpartition("/")
    repo, colon, _tag = last.partition(":")
    return f"{head}{slash}{repo}" if colon else source


def task_image_ref(name: str, platform: str = HUB_TASK_PLATFORM) -> str:
    """``<repository>@<platform digest>`` for pinned image *name*: the ref the
    hub task pulls. KeyError for a name or platform not pinned here."""
    spec = EXTERNAL_IMAGES[name]
    return f"{source_repository(spec['source'])}@{spec['platform_digests'][platform]}"
