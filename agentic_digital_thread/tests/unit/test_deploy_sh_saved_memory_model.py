# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deploy.sh keeps the memory extraction model saved on the Admin LLM settings page.

``PUT /api/admin/llm-settings`` switches the AgentCore Memory's strategies to
the chosen model and saves it in SSM (``/adt/llm-memory-model``). Without
reading it back, the next deploy would put the stack's model back on the
strategies. A saved model is passed as ``-c memory_extraction_model=`` when
MEMORY_EXTRACTION_MODEL is unset, unless
ALLOWED_MODELS offers no such memory model. The read is ``get-parameters``, so
a parameter that does not exist (listed in ``InvalidParameters``, the call
succeeds) is told apart from a read that failed (AccessDenied, expired
credentials): only the first keeps the default; the second stops the deploy
before CDK, since deploying the default would undo the saved choice.

The section is EXECUTED in bash with ``aws`` stubbed. Offline: bash + python.
"""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed argv (bash), no shell string interpolation of inputs
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
_START = "# ── Saved memory extraction model (Admin LLM settings) ──"
_END = "# ── End saved memory extraction model ──"
_PARAM = "/adt/llm-memory-model"
_WORKSHOP_PAIR = "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")


def _section() -> str:
    src = (_ADT_ROOT / "deploy.sh").read_text()
    start = src.index(_START)
    return src[start:src.index(_END, start)]


def _run(tmp_path, *, saved: str | None, workshop: bool = False,
         env: dict | None = None, read_fails: bool = False,
         expect_ok: bool = True) -> tuple[str, str, str]:
    """Returns (_MODEL_CDK_FLAG, stdout, aws calls); with ``expect_ok=False``
    the flags are "" and stdout carries stdout + stderr of the failed run."""
    calls = tmp_path / "aws-calls.log"
    answer = (
        'echo "An error occurred (AccessDeniedException) when calling the GetParameters '
        'operation" >&2; return 254' if read_fails else f"echo {saved or 'None'!r}; return 0")
    script = (
        "set -euo pipefail\n"
        "REGION=us-east-1\n_AWS_PROFILE_FLAG=''\n_MODEL_CDK_FLAG=' -c bedrock_model=x'\n"
        f'WITH_WORKSHOP="{"true" if workshop else ""}"\n'
        "aws() {\n"
        f'  echo "$*" >> {str(calls)!r}\n'
        f'  if [[ "$1 $2 $3" == "ssm get-parameters --names" && "$4" == "{_PARAM}" ]]; then {answer}; fi\n'
        "  return 1\n}\n"
        f"{_section()}\n"
        'echo "FLAGS=[$_MODEL_CDK_FLAG]"\n'
    )
    path = tmp_path / "saved_memory_model_section.sh"
    path.write_text(script, encoding="utf-8")
    run_env = {k: v for k, v in os.environ.items()
               if k not in ("MEMORY_EXTRACTION_MODEL", "ALLOWED_MODELS")}
    run_env.update(env or {})
    result = subprocess.run(["bash", str(path)], cwd=_ADT_ROOT, env=run_env,  # nosec B603 B607
                            capture_output=True, text=True, timeout=60)
    called = calls.read_text() if calls.exists() else ""
    if not expect_ok:
        assert result.returncode != 0, result.stdout
        assert "FLAGS=[" not in result.stdout
        return "", result.stdout + result.stderr, called
    assert result.returncode == 0, result.stderr
    flags = next(ln for ln in result.stdout.splitlines() if ln.startswith("FLAGS=["))[7:-1]
    return flags, result.stdout, called


def test_saved_memory_model_is_reused(tmp_path):
    flags, out, called = _run(tmp_path, saved="global.anthropic.claude-sonnet-5-5")
    assert flags.split() == ["-c", "bedrock_model=x",
                             "-c", "memory_extraction_model=global.anthropic.claude-sonnet-5-5"]
    assert "global.anthropic.claude-sonnet-5-5" in out
    assert _PARAM in called


def test_nothing_saved_keeps_the_default(tmp_path):
    flags, _, called = _run(tmp_path, saved=None)
    assert flags == " -c bedrock_model=x"
    assert _PARAM in called


def test_explicit_env_wins_and_ssm_is_not_read(tmp_path):
    flags, _, called = _run(tmp_path, saved="global.anthropic.claude-sonnet-5-5",
                            env={"MEMORY_EXTRACTION_MODEL": "us.amazon.nova-pro-v1:0"})
    assert flags == " -c bedrock_model=x"
    assert called == ""


def test_saved_model_outside_the_allowlist_is_ignored(tmp_path):
    flags, out, _ = _run(tmp_path, saved="global.anthropic.claude-haiku-5-5",
                         env={"ALLOWED_MODELS": _WORKSHOP_PAIR})
    assert flags == " -c bedrock_model=x"
    assert "ignored" in out


def test_saved_model_inside_the_allowlist_is_reused(tmp_path):
    flags, _, _ = _run(tmp_path, saved="global.anthropic.claude-haiku-4-5-20251001-v1:0",
                       env={"ALLOWED_MODELS": _WORKSHOP_PAIR})
    assert flags.endswith(" -c memory_extraction_model=global.anthropic.claude-haiku-4-5-20251001-v1:0")


def test_a_saved_model_that_is_no_memory_model_is_ignored(tmp_path):
    """A hand-written value the stack would refuse never reaches the synth."""
    flags, out, _ = _run(tmp_path, saved="global.anthropic.claude-opus-5-5")
    assert flags == " -c bedrock_model=x"
    assert "ignored" in out


def test_the_block_sits_next_to_the_saved_orchestrator_block():
    src = (_ADT_ROOT / "deploy.sh").read_text()
    orch_end = src.index("# ── End saved orchestrator model ──")
    assert orch_end < src.index(_START) < src.index("npx cdk deploy")


# -- a failed read is not "nothing saved" ----------------------------------------------

def test_the_read_tells_a_missing_parameter_from_a_failed_call(tmp_path):
    _, _, called = _run(tmp_path, saved=None)
    assert f"ssm get-parameters --names {_PARAM}" in called


def test_a_failed_read_stops_the_deploy_before_cdk(tmp_path):
    _, out, called = _run(tmp_path, saved=None, read_fails=True, expect_ok=False)
    assert _PARAM in called
    assert "could not be read" in out
    assert "MEMORY_EXTRACTION_MODEL" in out
    assert "AccessDeniedException" in out   # the aws error itself is shown


def test_a_failed_read_does_not_matter_with_an_explicit_model(tmp_path):
    flags, _, called = _run(tmp_path, saved=None, read_fails=True,
                            env={"MEMORY_EXTRACTION_MODEL": "us.amazon.nova-pro-v1:0"})
    assert flags == " -c bedrock_model=x" and called == ""


def test_the_saved_orchestrator_block_is_unchanged():
    """Finding 4 is about the memory block only."""
    src = (_ADT_ROOT / "deploy.sh").read_text()
    start = src.index("# ── Saved orchestrator model")
    block = src[start:src.index("# ── End saved orchestrator model ──", start)]
    assert 'aws ssm get-parameter --name "/adt/llm-orchestrator-model"' in block
