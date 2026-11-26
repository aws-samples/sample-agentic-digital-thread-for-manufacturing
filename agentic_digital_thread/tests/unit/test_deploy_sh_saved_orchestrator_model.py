# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deploy.sh keeps the orchestrator model saved on the Admin LLM settings page.

``PUT /api/admin/llm-settings`` saves the orchestrator model in SSM
(``/adt/llm-orchestrator-model``). deploy.sh used to ignore it: a redeploy
with no ``BEDROCK_MODEL`` rebuilt the orchestrator and onboarding runtimes on
the built-in default while the Settings page kept showing the saved model.
That was invisible while the saved model and the default were the same; with
Claude Sonnet 5.5 as the default it would silently move a deployment that
saved Sonnet 5. Now a saved model is reused when BEDROCK_MODEL is unset and
unless ALLOWED_MODELS excludes it.

The section is EXECUTED in bash with ``aws`` stubbed, so the exported model
and the CDK flags are pinned. Offline: bash + python only.
"""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed argv (bash), no shell string interpolation of inputs
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
_START = "# ── Saved orchestrator model (Admin LLM settings) ──"
_END = "# ── End saved orchestrator model ──"
_PARAM = "/adt/llm-orchestrator-model"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")


def _section() -> str:
    src = (_ADT_ROOT / "deploy.sh").read_text()
    start = src.index(_START)
    return src[start:src.index(_END, start)]


def _run(tmp_path, *, saved: str | None, workshop: bool = False,
         env: dict | None = None) -> tuple[str, str, str]:
    """Returns (exported BEDROCK_MODEL, _MODEL_CDK_FLAG, stdout)."""
    calls = tmp_path / "aws-calls.log"
    script = (
        "set -euo pipefail\n"
        "REGION=us-east-1\n_AWS_PROFILE_FLAG=''\n_MODEL_CDK_FLAG=''\n"
        f'WITH_WORKSHOP="{"true" if workshop else ""}"\n'
        "aws() {\n"
        f'  echo "$*" >> {str(calls)!r}\n'
        f'  if [[ "$2" == "get-parameter" && "$4" == "{_PARAM}" ]]; then echo {saved or "None"!r}; return 0; fi\n'
        "  return 1\n}\n"
        f"{_section()}\n"
        'echo "MODEL=[${BEDROCK_MODEL:-}]"\necho "FLAGS=[$_MODEL_CDK_FLAG]"\n'
    )
    path = tmp_path / "saved_model_section.sh"
    path.write_text(script, encoding="utf-8")
    run_env = {k: v for k, v in os.environ.items() if k not in ("BEDROCK_MODEL", "ALLOWED_MODELS")}
    run_env.update(env or {})
    result = subprocess.run(["bash", str(path)], cwd=_ADT_ROOT, env=run_env,  # nosec B603 B607
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    model = next(ln for ln in lines if ln.startswith("MODEL=["))[7:-1]
    flags = next(ln for ln in lines if ln.startswith("FLAGS=["))[7:-1]
    called = calls.read_text() if calls.exists() else ""
    return model, flags, called


def test_saved_model_is_reused(tmp_path):
    model, flags, called = _run(tmp_path, saved="us.anthropic.claude-sonnet-5")
    assert model == "us.anthropic.claude-sonnet-5"
    assert flags.split() == ["-c", "bedrock_model=us.anthropic.claude-sonnet-5"]
    assert _PARAM in called


def test_nothing_saved_keeps_the_default(tmp_path):
    model, flags, _ = _run(tmp_path, saved=None)
    assert model == "" and flags == ""


def test_explicit_bedrock_model_wins_and_ssm_is_not_read(tmp_path):
    model, flags, called = _run(tmp_path, saved="us.anthropic.claude-sonnet-5",
                                env={"BEDROCK_MODEL": "global.anthropic.claude-opus-5-5"})
    assert model == "global.anthropic.claude-opus-5-5"
    assert flags == ""  # the explicit value already rode the earlier flag line
    assert called == ""


def test_saved_model_outside_the_allowlist_is_ignored(tmp_path):
    model, flags, _ = _run(
        tmp_path, saved="global.anthropic.claude-opus-5-5",
        env={"ALLOWED_MODELS": "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0"})
    assert model == "" and flags == ""


def test_saved_model_inside_the_allowlist_is_reused(tmp_path):
    model, _, _ = _run(
        tmp_path, saved="global.anthropic.claude-sonnet-5",
        env={"ALLOWED_MODELS": "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0"})
    assert model == "global.anthropic.claude-sonnet-5"


def test_saved_agent_model_in_the_orchestrator_slot_is_ignored(tmp_path):
    """Allowed, but not for the orchestrator: the allowlist offers each role
    its own models (Haiku 4.5 is the system-agent model here)."""
    model, flags, _ = _run(
        tmp_path, saved="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        env={"ALLOWED_MODELS": "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0"})
    assert model == "" and flags == ""
