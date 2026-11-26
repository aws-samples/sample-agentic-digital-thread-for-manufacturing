# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every CDK-built image whose Dockerfile runs an OS package upgrade carries
``stacks/_os_patch.patched_image_options()``, so bumping ``OS_PATCH_LEVEL``
rebuilds it with ``--no-cache`` and the upgrade step actually runs."""
from __future__ import annotations

import ast
import datetime
import re
from pathlib import Path

from stacks import _os_patch

REPO_ROOT = Path(__file__).resolve().parents[1]

#: stack file -> the Dockerfile its from_asset builds (each runs an upgrade).
PATCHED_SITES = {
    "adt_compute_stack.py": "agentic_digital_thread/Dockerfile",
    "erp_dashboard_stack.py": "docker/Dockerfile.dashboard",
    "highbyte_opcua_stack.py": "highbyte/components/opc-server/Dockerfile",
    "sfc_stack.py": "docker/sfc/Dockerfile",
}

_UPGRADE_RE = re.compile(r"apt-get upgrade|apk upgrade|dnf -y upgrade|dnf upgrade")


def test_patch_level_is_a_hand_edited_date_literal():
    """A computed value would rebuild every image on every synth."""
    datetime.date.fromisoformat(_os_patch.OS_PATCH_LEVEL)
    tree = ast.parse((REPO_ROOT / "stacks" / "_os_patch.py").read_text(encoding="utf-8"))
    assigns = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
               and any(getattr(t, "id", None) == "OS_PATCH_LEVEL"
                       for t in getattr(n, "targets", [getattr(n, "target", None)]))]
    assert len(assigns) == 1
    assert isinstance(assigns[0].value, ast.Constant)


def test_options_change_the_hash_and_disable_the_cache():
    opts = _os_patch.patched_image_options()
    assert opts["cache_disabled"] is True
    assert _os_patch.OS_PATCH_LEVEL in opts["extra_hash"]


def test_each_upgrading_dockerfile_still_upgrades():
    for dockerfile in PATCHED_SITES.values():
        text = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
        assert _UPGRADE_RE.search(text), dockerfile


def _from_asset_calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "from_asset"
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "ContainerImage"]


def _unpacks_patch_options(call: ast.Call) -> bool:
    return any(kw.arg is None and isinstance(kw.value, ast.Call)
               and getattr(kw.value.func, "id", None) == "patched_image_options"
               for kw in call.keywords)


def test_each_upgrading_image_asset_uses_the_patch_options():
    for stack_file in PATCHED_SITES:
        tree = ast.parse((REPO_ROOT / "stacks" / stack_file).read_text(encoding="utf-8"))
        calls = _from_asset_calls(tree)
        assert calls, stack_file
        assert all(_unpacks_patch_options(c) for c in calls), stack_file
