# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deploy.sh step 1a (host-side SiteWise asset models) is gated on a real consumer.

The 28 SiteWise asset models the step pre-creates are needed host-side by TWO
deploy modes:

  1. HighByte (``_HB_IN_DEPLOY`` — ``--highbyte`` or sticky ``/adt/highbyte-enabled``):
     its pipeline boots with ``create:true`` and creates assets under models that
     must already exist before the hub container starts.
  2. The ERP simulator (``WITH_SIMULATOR`` — ``--simulator``, implied by ``--tulip``
     and ``--workshop``): it ships ``erp-sitewise-degradation-feed``, the supported
     no-HighByte SiteWise writer, which resolves the ``CNC`` model by name and fails
     loudly if absent (``lambda-sitewise-feed/handler.py``) and cannot create the
     model itself (no ``CreateAssetModel`` grant).

SFC creates the same models itself from ``SfcStack``'s ``lambda-sfc-sitewise-tree``
custom resource, so an SFC-only deploy needs no host-side step; and a bare base or
``--seed`` deploy has no SiteWise writer at all, so the models would be orphans.

So step 1a runs ``DeploySitewiseModels.py`` iff ``_HB_IN_DEPLOY`` OR
``WITH_SIMULATOR``; otherwise it prints a one-line skip and the script is not
invoked. On a model-deploy failure it exits non-zero with a diagnostic.

The step is EXECUTED in bash under the SAME strictness deploy.sh uses
(``set -euo pipefail``) with ``python3`` stubbed, so behaviour (invocation, skip,
failure exit), not wording, is pinned. Offline: bash + stubs only.
"""
from __future__ import annotations

import shutil
import subprocess  # nosec B404 - fixed argv (bash), no shell string interpolation of inputs
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
_START = "# ── 1a. Deploy SiteWise asset models"
_END = "# Note: SiteWise asset creation is handled by the HighByte pipeline"
_MODEL_SCRIPT = "DeploySitewiseModels.py"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")


def _deploy_sh() -> str:
    return (_ADT_ROOT / "deploy.sh").read_text()


def _section() -> str:
    src = _deploy_sh()
    start = src.index(_START)
    end = src.index(_END, start)
    return src[start:end]


def _run(tmp_path, *, hb_in_deploy=False, with_simulator=False, model_rc=0):
    """Run step 1a under `set -euo pipefail` with python3 stubbed.

    Returns (exit_code, out, model_called). `model_rc` is the exit code the
    stubbed model script returns, so the failure path can be exercised.
    """
    marker = tmp_path / "model_called.log"
    stub = f'''
set -euo pipefail
REGION=us-east-1
AWS_PROFILE=""
_HB_IN_DEPLOY={1 if hb_in_deploy else 0}
WITH_SIMULATOR={'"true"' if with_simulator else '""'}
# Stub python3 so the real model script never runs; record the invocation, then
# return the configured code so the fail-loud path is testable.
python3() {{
  echo "python3 $*" >> {str(marker)!r}
  return {model_rc}
}}
{_section()}
'''
    script_path = tmp_path / "step_1a.sh"
    script_path.write_text(stub, encoding="utf-8")
    result = subprocess.run(["bash", str(script_path)], capture_output=True, text=True,  # nosec B603 B607
                            timeout=30)
    out = result.stdout + result.stderr
    called = marker.read_text() if marker.exists() else ""
    return result.returncode, out, (_MODEL_SCRIPT in called)


def test_highbyte_in_deploy_runs_the_model_step(tmp_path):
    code, out, model_called = _run(tmp_path, hb_in_deploy=True)
    assert code == 0, out
    assert model_called, "step 1a must run DeploySitewiseModels.py when HighByte is in the deploy"
    assert "Deploying SiteWise asset models" in out


def test_simulator_without_highbyte_runs_the_model_step(tmp_path):
    """A --simulator deploy (no HighByte) ships the erp-sitewise-degradation-feed
    Lambda, which needs the CNC model host-side, so step 1a MUST run."""
    code, out, model_called = _run(tmp_path, hb_in_deploy=False, with_simulator=True)
    assert code == 0, out
    assert model_called, "step 1a must run on --simulator (the OT feed needs the CNC model)"
    assert "Deploying SiteWise asset models" in out


def test_highbyte_and_simulator_runs_the_model_step(tmp_path):
    code, out, model_called = _run(tmp_path, hb_in_deploy=True, with_simulator=True)
    assert code == 0, out
    assert model_called


def test_bare_deploy_skips_the_model_step(tmp_path):
    """No HighByte and no simulator (bare base / --seed): SFC or nothing provides
    the models, so step 1a MUST skip to avoid orphan models."""
    code, out, model_called = _run(tmp_path, hb_in_deploy=False, with_simulator=False)
    assert code == 0, out
    assert not model_called, "step 1a must NOT run without a host-side model consumer"
    assert "Skipping host-side SiteWise asset model deploy" in out


def test_model_deploy_failure_exits_loud(tmp_path):
    """When the step runs and the model script fails, the deploy stops non-zero
    with the diagnostic (the message is reachable via `if ! python3 ...`)."""
    code, out, model_called = _run(tmp_path, hb_in_deploy=True, model_rc=1)
    assert model_called
    assert code != 0, "a model-deploy failure must abort the deploy"
    assert "SiteWise model deployment failed" in out


def test_the_gate_covers_both_highbyte_and_simulator():
    """The gate condition is `_HB_IN_DEPLOY` OR `WITH_SIMULATOR`, and the model
    script sits inside the guarded branch before its else."""
    section = _section()
    assert 'if [[ "$_HB_IN_DEPLOY" -eq 1 || "$WITH_SIMULATOR" == "true" ]]; then' in section
    guard = section.index('if [[ "$_HB_IN_DEPLOY" -eq 1 || "$WITH_SIMULATOR" == "true" ]]; then')
    else_at = section.index("\nelse\n", guard)
    assert guard < section.index(_MODEL_SCRIPT) < else_at


def test_gate_inputs_are_set_before_step_1a_under_nounset():
    """Both gate inputs are initialized unconditionally before step 1a, so the
    gate is safe under `set -u`: `_HB_IN_DEPLOY=0` (line ~645) and
    `WITH_SIMULATOR=""` (line ~190) both precede the step."""
    src = _deploy_sh()
    start = src.index(_START)
    assert src.index("_HB_IN_DEPLOY=0") < start, "_HB_IN_DEPLOY must have an unconditional 0 default before step 1a"
    assert src.index('WITH_SIMULATOR=""') < start, "WITH_SIMULATOR must have an unconditional default before step 1a"


def test_step_1a_runs_before_cdk_deploy():
    """The models must exist before CDK (HighByte hub) and before the simulator's
    SiteWise feed is invoked, so step 1a must textually precede the CDK deploy —
    both the `_cdk_deploy()` definition and its first invocation."""
    src = _deploy_sh()
    start = src.index(_START)
    assert start < src.index("_cdk_deploy() {")
    assert start < src.index("_cdk_deploy ")
