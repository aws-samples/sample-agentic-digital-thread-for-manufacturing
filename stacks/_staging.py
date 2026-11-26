# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time asset STAGING — assembled under ``cdk.out``, never in the source tree.

``cdk synth`` is PURE: no stack writes into the checkout. Writing into the
source tree (an image context receiving a ``copytree`` of generated datasets,
a Lambda directory receiving a config copy) leaves gitignored residue, makes
``cdk synth`` a mutating operation, and doubles the bytes in every checkout.
When an asset genuinely needs files from two places, stage
them HERE — a directory under the app's ``outdir`` that lives and dies with
``cdk.out`` — and point ``Code.from_asset`` / ``from_asset`` at the staging
directory.

The OPC image needs no staging (its context is ``highbyte/``
with an allowlist ``.dockerignore``); the HighByte setup Lambda is the one
caller. Pinned by ``tests/test_docker_context_hygiene.py``.
"""
from __future__ import annotations

import os
import shutil

from constructs import Construct

STAGING_DIRNAME = "adt-staging"


def staging_dir(scope: Construct, name: str) -> str:
    """A fresh, empty ``<outdir>/adt-staging/<name>`` for *scope*'s app.

    Recreated on every synth so stale files cannot leak between runs. Falls
    back to a temporary directory when the construct tree has no ``outdir``
    (bare unit-test scopes).
    """
    root = scope.node.root
    outdir = getattr(root, "outdir", None)
    if not outdir:
        import tempfile
        outdir = tempfile.mkdtemp(prefix="adt-cdk-")
    path = os.path.join(outdir, STAGING_DIRNAME, name)
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


def stage_tree(scope: Construct, name: str, source_dir: str,
               extra_files: dict[str, str] | None = None,
               ignore: tuple[str, ...] = ("node_modules", "__pycache__", ".DS_Store"),
               extra_texts: dict[str, str] | None = None) -> str:
    """Copy *source_dir* (minus *ignore*) into a fresh staging dir and add
    *extra_files* (``{arcname: src_path}``; a missing source is skipped) and
    *extra_texts* (``{arcname: content}`` — a file generated at synth, written
    here under cdk.out and never into the checkout)."""
    dest = staging_dir(scope, name)
    shutil.copytree(source_dir, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*ignore))
    for arcname, src in (extra_files or {}).items():
        if os.path.isfile(src):
            target = os.path.join(dest, arcname)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
    for arcname, text in (extra_texts or {}).items():
        target = os.path.join(dest, arcname)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
    return dest
