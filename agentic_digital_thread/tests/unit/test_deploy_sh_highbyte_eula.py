# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deploy.sh HighByte EULA acceptance (one time, explicit, sticky in SSM).

The HighByte hub container runs only with ``ACCEPT_EULA=Y``, and setting it
agrees to the HighByte Intelligence Hub EULA. So a deploy that includes
HighByte (``--highbyte`` or the sticky ``/adt/highbyte-enabled``) needs the
deployer's explicit ``--accept-highbyte-eula`` once: it prints the EULA link
and records ``/adt/highbyte-eula-accepted``. Later deploys pass the recorded
acceptance to CDK (``-c highbyte_accept_eula=true``). Without a recorded
acceptance the deploy stops with the link BEFORE anything is persisted.

The deploy.sh HighByte section is EXECUTED in bash with ``aws`` stubbed, so the
behaviour (exit code, SSM writes, CDK flags), not the wording, is pinned.
Offline: no AWS, no network (bash + stubs only).
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess  # nosec B404 - fixed argv (bash), no shell string interpolation of inputs
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _ADT_ROOT.parent
_START = "# ── HighByte (optional OT/UNS layer)"
# The HighByte section ends where the next deploy.sh step starts: the CDK CLI
# install when the section runs right after the credential check, or the OPC
# UA servers block when it runs after the dependency and bundle steps.
_ENDS = ("# ── Pre-flight: ensure local AWS CDK CLI is installed",
         "# OPC UA servers without HighByte")
_CDK_CLI_STEP = "# ── Pre-flight: ensure local AWS CDK CLI is installed"
_I3X_STEP = "# ── Pre-flight: build the embedded i3X Explorer web bundle"
_EULA_URL = "https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf"
_ENABLED = "/adt/highbyte-enabled"
_ACCEPTED = "/adt/highbyte-eula-accepted"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")


def _deploy_sh() -> str:
    return (_ADT_ROOT / "deploy.sh").read_text()


def _section() -> str:
    src = _deploy_sh()
    start = src.index(_START)
    end = min(pos for pos in (src.find(marker, start) for marker in _ENDS) if pos != -1)
    return src[start:end]


def _run(tmp_path, *, highbyte=False, accept=False, sticky_enabled=None, sticky_accepted=None):
    """Run the section; returns (exit_code, stdout+stderr, ssm_writes, cdk_flags)."""
    writes = tmp_path / "writes.log"
    stub = f'''
set -eo pipefail
REGION=us-east-1
_AWS_PROFILE_FLAG=""
HB_ENABLE={1 if highbyte else 0}
HB_EULA_ACCEPT={1 if accept else 0}
aws() {{
  if [[ "$2" == "put-parameter" ]]; then echo "$4=$6" >> {str(writes)!r}; return 0; fi
  if [[ "$2" == "get-parameter" ]]; then
    # Like the real CLI: a value prints it; a missing parameter exits 254
    # with ParameterNotFound on stderr.
    local v=""
    case "$4" in
      {_ENABLED}) v={sticky_enabled or ""!r} ;;
      {_ACCEPTED}) v={sticky_accepted or ""!r} ;;
    esac
    if [[ -z "$v" ]]; then
      echo "An error occurred (ParameterNotFound) when calling the GetParameter operation: Parameter $4 not found." >&2
      return 254
    fi
    echo "$v"
    return 0
  fi
}}
{_section()}
echo "FLAGS=[$_HB_CDK_FLAGS]"
'''
    script_path = tmp_path / "highbyte_section.sh"
    script_path.write_text(stub, encoding="utf-8")
    result = subprocess.run(["bash", str(script_path)], capture_output=True, text=True,  # nosec B603 B607
                            timeout=30)
    out = result.stdout + result.stderr
    logged = writes.read_text().splitlines() if writes.exists() else []
    flags = out.split("FLAGS=[", 1)[1].split("]", 1)[0].split() if "FLAGS=[" in out else None
    return result.returncode, out, logged, flags


def _cdk_context(flags) -> set[str]:
    return {flags[i + 1] for i, word in enumerate(flags) if word == "-c"}


def test_no_highbyte_adds_nothing_and_writes_nothing(tmp_path):
    code, out, writes, flags = _run(tmp_path)
    assert code == 0, out
    assert flags == []
    assert writes == []


def test_highbyte_without_acceptance_stops_with_the_link_and_persists_nothing(tmp_path):
    code, out, writes, flags = _run(tmp_path, highbyte=True)
    assert code == 1, out
    assert _EULA_URL in out
    assert "--accept-highbyte-eula" in out
    assert writes == []          # not even the sticky enable flag
    assert flags is None         # stopped before the CDK flags were emitted


def test_first_acceptance_prints_the_link_records_it_and_enables(tmp_path):
    code, out, writes, flags = _run(tmp_path, highbyte=True, accept=True)
    assert code == 0, out
    assert _EULA_URL in out
    assert writes == [f"{_ACCEPTED}=true", f"{_ENABLED}=true"]
    assert _cdk_context(flags) == {"highbyte_enabled=true", "highbyte_accept_eula=true"}


def test_a_sticky_highbyte_account_without_acceptance_stops_on_its_next_deploy(tmp_path):
    """An account deployed with --highbyte before the acceptance existed
    keeps HighByte sticky; its next deploy (even with no flags) stops."""
    code, out, writes, flags = _run(tmp_path, sticky_enabled="true")
    assert code == 1, out
    assert _EULA_URL in out
    assert "--accept-highbyte-eula" in out
    assert writes == []
    assert flags is None


def test_a_sticky_highbyte_account_accepts_once_without_repeating_highbyte(tmp_path):
    code, out, writes, flags = _run(tmp_path, accept=True, sticky_enabled="true")
    assert code == 0, out
    assert _EULA_URL in out
    assert writes == [f"{_ACCEPTED}=true"]
    assert _cdk_context(flags) == {"highbyte_enabled=true", "highbyte_accept_eula=true"}


@pytest.mark.parametrize("kwargs,expected_writes", [
    ({"highbyte": True}, [f"{_ENABLED}=true"]),
    ({}, []),
], ids=["highbyte-flag", "no-flags"])
def test_a_recorded_acceptance_is_reapplied_without_the_flag(tmp_path, kwargs, expected_writes):
    code, out, writes, flags = _run(tmp_path, sticky_enabled="true", sticky_accepted="true", **kwargs)
    assert code == 0, out
    assert _EULA_URL in out
    assert writes == expected_writes
    assert _cdk_context(flags) == {"highbyte_enabled=true", "highbyte_accept_eula=true"}


def test_a_recorded_acceptance_alone_adds_no_highbyte_context(tmp_path):
    """Acceptance without HighByte enabled deploys no hub: no flags."""
    code, out, writes, flags = _run(tmp_path, sticky_accepted="true")
    assert code == 0, out
    assert flags == []
    assert writes == []


def test_the_acceptance_flag_without_highbyte_is_refused(tmp_path):
    code, out, writes, flags = _run(tmp_path, accept=True)
    assert code == 1, out
    assert "--accept-highbyte-eula" in out
    assert writes == []


def test_the_flag_parser_accepts_the_acceptance_flag(tmp_path):
    """``--accept-highbyte-eula`` is a known flag (not the usage error)."""
    src = _deploy_sh()
    start = src.index("while [[ $# -gt 0 ]]; do")
    end = src.index("\ndone\n", start) + len("\ndone\n")
    script = ("set -eo pipefail\nHB_ENABLE=0\nHB_EULA_ACCEPT=0\n"
              "set -- --accept-highbyte-eula --highbyte\n"
              + src[start:end] + 'echo "ACCEPT=$HB_EULA_ACCEPT ENABLE=$HB_ENABLE"\n')
    script_path = tmp_path / "flag_parser.sh"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(["bash", str(script_path)], capture_output=True, text=True,  # nosec B603 B607
                            timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACCEPT=1 ENABLE=1" in result.stdout
    assert "[--accept-highbyte-eula]" in src[start:end]   # listed in the usage line


def test_no_ssm_write_comes_before_the_eula_gate():
    """The gate stops a deploy before anything is persisted: the first SSM
    write in deploy.sh is inside the HighByte section, after the check."""
    src = _deploy_sh()
    section_start = src.index(_START)
    assert "put-parameter" not in src[:section_start]
    section = _section()
    assert section.index("exit 1") < section.index("put-parameter")


def test_the_eula_gate_runs_before_the_dependency_and_bundle_steps():
    """A deploy without a recorded acceptance fails fast: the gate runs right
    after the Region and credential check, before the CDK CLI install
    (npm install) and the i3X Explorer bundle build (npm ci, static/ copy)."""
    src = _deploy_sh()
    section_start = src.index(_START)
    gate = section_start + _section().index("exit 1")
    assert src.index('REGION="${AWS_DEFAULT_REGION') < section_start
    assert src.index("aws sts get-caller-identity") < section_start
    for later in (_CDK_CLI_STEP, "npm install --silent", _I3X_STEP, "npm ci --silent",
                  'cp -R "$I3X_SRC/dist/." "$I3X_DEST/"'):
        assert gate < src.index(later), later


def test_the_other_sticky_flags_append_after_the_highbyte_flags_start():
    """The OPC UA and SFC sticky blocks add to the HighByte CDK flags, so they
    must come after the HighByte section starts that variable."""
    src = _deploy_sh()
    reset = src.index('_HB_CDK_FLAGS=""')
    assert src.count('_HB_CDK_FLAGS=""') == 1
    for flag in ('_HB_CDK_FLAGS+=" -c opcua_enabled=true"', '_HB_CDK_FLAGS+=" -c sfc_enabled=true"'):
        assert reset < src.index(flag), flag


def test_deploy_sh_names_the_same_eula_as_the_stack():
    """deploy.sh prints the agreement the CDK synth error names."""
    spec = importlib.util.spec_from_file_location(
        "adt_test_highbyte_options", _REPO_ROOT / "stacks" / "_highbyte_options.py")
    opts = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = opts   # dataclasses resolve their module by name
    try:
        spec.loader.exec_module(opts)
    finally:
        sys.modules.pop(spec.name, None)
    assert opts.EULA_URL == _EULA_URL
    assert opts.EULA_URL in _section()
    assert f"-c {opts.EULA_ACCEPTED_KEY}=true" in _section()
