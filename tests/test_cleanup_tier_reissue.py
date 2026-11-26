# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""cleanup.sh's delete_tier finishes a tier after a retained retry.

On a Quick deploy with Lambda@Edge, ADTComputeStack's first delete fails on
the edge replicas, and ADTOnboardingStack's delete is canceled again and
again while ADTComputeStack still imports its export, until the tier times
out. delete_tier then retries ADTComputeStack retaining the failed
resources. The stacks its importer held back must be deleted again before
the next tier, or they stay behind (and hold up ADTAgentCoreStack).

The helpers are cut from cleanup.sh and run against a fake ``aws`` that keeps
each stack's status in a file. ``sleep`` is a no-op.
"""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed internal argv lists only
from pathlib import Path

import pytest

CLEANUP = Path(__file__).resolve().parents[1] / "cleanup.sh"

# State: $STATE/<stack> holds its status; no file = the stack does not exist.
# ADTComputeStack: a plain delete fails (edge replicas); with --retain-resources
# it is deleted. ADTOnboardingStack: a delete is canceled (UPDATE_COMPLETE)
# while ADTComputeStack exists, else it is deleted.
_FAKE_AWS = r"""#!/usr/bin/env bash
echo "$*" >> "$STATE/calls"
args=" $* "
stack=""
prev=""
for a in "$@"; do
  [ "$prev" = "--stack-name" ] && stack="$a"
  prev="$a"
done
case "$args" in
  *" describe-stack-resources "*)
    [ "$stack" = ADTComputeStack ] && echo "EdgeFunctionReplica" ;;
  *" describe-stacks "*)
    if [ -f "$STATE/$stack" ]; then cat "$STATE/$stack"; else
      echo "Stack with id $stack does not exist" >&2; exit 255; fi ;;
  *" delete-stack "*)
    [ -f "$STATE/$stack" ] || exit 0
    case "$stack" in
      ADTComputeStack)
        case "$args" in *" --retain-resources "*) rm -f "$STATE/$stack" ;;
          *) echo DELETE_FAILED > "$STATE/$stack" ;; esac ;;
      ADTOnboardingStack)
        if [ -f "$STATE/ADTComputeStack" ]; then echo UPDATE_COMPLETE > "$STATE/$stack";
        else rm -f "$STATE/$stack"; fi ;;
      *) rm -f "$STATE/$stack" ;;
    esac ;;
esac
"""


def _helpers() -> str:
    """cfn_region, wait_stack_delete, retry_with_retain, stack_status and
    delete_tier, cut from cleanup.sh."""
    text = CLEANUP.read_text(encoding="utf-8")
    first = text[text.index("cfn_region() {"):text.index("# ── 1. Delete AgentCore Runtimes")]
    second = text[text.index("stack_status() {"):text.index("# Tier 0 and 0b:")]
    return first + "\n" + second


def _run(tmp_path: Path, timeout: int = 30) -> tuple[int, str, Path]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not found")
    state = tmp_path / "state"
    state.mkdir()
    for stack in ("ADTComputeStack", "ADTOnboardingStack"):
        (state / stack).write_text("CREATE_COMPLETE\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "aws"
    fake.write_text(_FAKE_AWS)
    fake.chmod(0o755)
    script = ("set -euo pipefail\nREGION=us-east-1\n_PROFILE_FLAG=''\nsleep() { :; }\n"
              + _helpers()
              + f"\ndelete_tier {timeout} ADTComputeStack ADTOnboardingStack\n")
    env = {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "STATE": str(state)}
    result = subprocess.run([bash, "-c", script], env=env, capture_output=True,  # nosec B603 - fixed script cut from the repo
                            text=True, timeout=120)
    return result.returncode, result.stdout + result.stderr, state


def test_a_stack_held_back_by_a_retried_importer_is_deleted_in_the_same_tier(tmp_path):
    rc, out, state = _run(tmp_path)
    assert rc == 0, out
    assert not (state / "ADTComputeStack").exists(), out
    assert not (state / "ADTOnboardingStack").exists(), out


def test_a_retried_stack_is_not_deleted_again_without_retain(tmp_path):
    _, _, state = _run(tmp_path)
    calls = (state / "calls").read_text().splitlines()
    plain = [c for c in calls if "delete-stack" in c and "ADTComputeStack" in c
             and "--retain-resources" not in c]
    assert len(plain) == 1, calls


# --------------------------------------------------------------------------- #
# Step 6 deletes every /adt/* parameter, nested ones included.
# --------------------------------------------------------------------------- #
_FAKE_SSM = r"""#!/usr/bin/env bash
echo "$*" >> "$STATE/calls"
case " $* " in
  *" get-parameters-by-path "*)
    case " $* " in
      *" --recursive "*) echo "/adt/edge-auth-enabled /adt/demo/seed-db /adt/demo/erp-db" ;;
      *) echo "/adt/edge-auth-enabled" ;;
    esac ;;
esac
"""


def test_step_6_deletes_nested_adt_parameters(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not found")
    text = CLEANUP.read_text(encoding="utf-8")
    step = text[text.index("# ── 6. Delete SSM parameters"):text.index("# Delete agent config secrets")]
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "aws"
    fake.write_text(_FAKE_SSM)
    fake.chmod(0o755)
    script = "set -euo pipefail\nREGION=us-east-1\n_PROFILE_FLAG=''\n" + step
    env = {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "STATE": str(state)}
    result = subprocess.run([bash, "-c", script], env=env, capture_output=True,  # nosec B603 - fixed script cut from the repo
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    deleted = [c.split("--name ")[1].split()[0]
               for c in (state / "calls").read_text().splitlines() if "delete-parameter" in c]
    assert deleted == ["/adt/edge-auth-enabled", "/adt/demo/seed-db", "/adt/demo/erp-db"]
