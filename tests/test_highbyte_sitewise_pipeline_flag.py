# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deploy.sh ``--highbyte-sitewise-pipeline enabled|disabled`` (ADR-0082).

The option sets CDK context ``highbyte_sitewise_pipeline``
(``stacks/_highbyte_options.py``). Without it, a deploy with both HighByte and
SFC passes ``disabled`` (SFC is the only SiteWise writer, so the SFC freshness
watchdog is built), and a HighByte deploy without SFC passes ``enabled``
(today's behaviour). An explicit value always wins and is sticky in SSM
``/adt/highbyte-sitewise-pipeline``.

The SSM reads this choice depends on (``/adt/highbyte-enabled``,
``/adt/sfc-enabled``, ``/adt/highbyte-sitewise-pipeline``) treat only
``ParameterNotFound`` as "not set". Any other failure (access denied,
throttling, expired credentials, transport) stops the deploy before any SSM
write or CDK call, naming the parameter.

Offline: runs deploy.sh's flag defaults, argument loop, the HighByte section
and the OPC UA / SFC / pipeline sections in bash (skipping the CDK CLI install
and the i3X Explorer build between them), with a fake ``aws`` on PATH that
serves SSM values and errors from the environment and logs every
put-parameter.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "agentic_digital_thread" / "deploy.sh"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"
PARAM = "/adt/highbyte-sitewise-pipeline"
HB_PARAM = "/adt/highbyte-enabled"
SFC_PARAM = "/adt/sfc-enabled"
EULA_PARAM = "/adt/highbyte-eula-accepted"
READS = ("# >>> highbyte sitewise pipeline sticky reads (", "# <<< highbyte sitewise pipeline sticky reads\n")
DECIDE = ("# >>> highbyte sitewise pipeline (", "# <<< highbyte sitewise pipeline\n")
#: Printed on every deploy that applies the default (HighByte and SFC on, no
#: explicit or saved value). Always true; claims nothing about the past.
DEFAULT_LINE = ("HighByte SiteWise pipeline: disabled by default because SFC is on "
                "(SFC is the only SiteWise writer). Pass --highbyte-sitewise-pipeline enabled "
                "to have HighByte write SiteWise too.")
HB_SECTION = "# ── HighByte (optional OT/UNS layer)"
CDK_PREFLIGHT = "# ── Pre-flight: ensure local AWS CDK CLI is installed"
OPCUA_SECTION = "# OPC UA servers without HighByte"

ACCESS_DENIED = ("An error occurred (AccessDeniedException) when calling the GetParameter operation: "
                 "User: arn:aws:sts::111122223333:assumed-role/x is not authorized to perform: ssm:GetParameter")
THROTTLED = "An error occurred (ThrottlingException) when calling the GetParameter operation: Rate exceeded"
EXPIRED = "An error occurred (ExpiredTokenException) when calling the GetParameter operation: token expired"
TRANSPORT = 'Could not connect to the endpoint URL: "https://ssm.us-east-1.amazonaws.com/"'

FAKE_AWS = r"""#!/usr/bin/env bash
# Fake aws CLI: ssm get-parameter / put-parameter only.
name=""; value=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --name) name="${args[$((i + 1))]}" ;;
    --value) value="${args[$((i + 1))]}" ;;
  esac
done
key="$(printf '%s' "$name" | tr '/-' '__')"
val_key="FAKE_SSM_$key"; err_key="FAKE_SSM_ERR_$key"
case "$2" in
  get-parameter)
    printf '%s\n' "$name" >> "$FAKE_SSM_GET_LOG"
    if [ -n "${!err_key-}" ]; then printf '\n%s\n' "${!err_key}" >&2; exit 254; fi
    v="${!val_key-}"
    if [ -z "$v" ]; then
      printf '\nAn error occurred (ParameterNotFound) when calling the GetParameter operation: Parameter %s not found.\n' "$name" >&2
      exit 254
    fi
    printf '%s\n' "$v" ;;
  put-parameter) printf '%s=%s\n' "$name" "$value" >> "$FAKE_SSM_LOG" ;;
  *) echo "fake aws: unexpected $*" >&2; exit 2 ;;
esac
"""


def _env_key(name: str) -> str:
    return name.replace("/", "_").replace("-", "_")


def _run(tmp_path: Path, *args: str, hb: bool = False, sfc: bool = False,
         ssm: dict | None = None, errors: dict | None = None
         ) -> tuple[int, dict, str, str, list[str]]:
    """``(exit code, CDK context, stdout, stderr, pipeline SSM writes)``.

    ``hb`` / ``sfc`` prepend ``--highbyte --accept-highbyte-eula`` / ``--sfc``.
    Every SSM write of the run is in :func:`_all_writes`."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not found")
    text = DEPLOY.read_text(encoding="utf-8")
    defaults = text[text.index("# Parse flags\n"):text.index("while [[ $# -gt 0 ]]; do")]
    loop_start = text.index("while [[ $# -gt 0 ]]; do")
    loop = text[loop_start:text.index("\ndone\n", loop_start) + len("\ndone\n")]
    highbyte = text[text.index(HB_SECTION):text.index(CDK_PREFLIGHT)]
    decide_end = text.index(DECIDE[1], text.index(DECIDE[0]))
    rest = text[text.index(OPCUA_SECTION):decide_end]
    script = ("set -euo pipefail\n" + defaults + loop + 'REGION="us-east-1"\n'
              + highbyte + rest + 'printf "\\nCTX:%s\\n" "$_HB_CDK_FLAGS"\n')
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    aws = fake_bin / "aws"
    aws.write_text(FAKE_AWS, encoding="utf-8")
    aws.chmod(0o755)
    log = tmp_path / "ssm.log"
    log.write_text("", encoding="utf-8")
    get_log = tmp_path / "ssm-get.log"
    get_log.write_text("", encoding="utf-8")
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
           "FAKE_SSM_LOG": str(log), "FAKE_SSM_GET_LOG": str(get_log)}
    for name, value in (ssm or {}).items():
        env["FAKE_SSM_" + _env_key(name)] = value
    for name, value in (errors or {}).items():
        env["FAKE_SSM_ERR_" + _env_key(name)] = value
    prefix = (["--highbyte", "--accept-highbyte-eula"] if hb else []) + (["--sfc"] if sfc else [])
    result = subprocess.run([bash, "-c", script, "deploy.sh", *prefix, *args],  # nosec B603 - fixed script from the repo, fixed args
                            capture_output=True, text=True, timeout=30, env=env)
    context: dict = {}
    if result.returncode == 0:
        ctx_line = result.stdout.rsplit("CTX:", 1)[1].splitlines()[0].strip()
        words = shlex.split(ctx_line)
        assert len(words) % 2 == 0 and set(words[::2]) <= {"-c"}, words
        context = dict(word.split("=", 1) for word in words[1::2])
    writes = [line for line in _all_writes(tmp_path) if line.startswith(PARAM + "=")]
    return result.returncode, context, result.stdout, result.stderr, writes


def _all_writes(tmp_path: Path) -> list[str]:
    return [line for line in (tmp_path / "ssm.log").read_text(encoding="utf-8").splitlines() if line]


def _all_reads(tmp_path: Path) -> list[str]:
    return [line for line in (tmp_path / "ssm-get.log").read_text(encoding="utf-8").splitlines() if line]


#: An existing account where HighByte and SFC were both enabled before this
#: option existed: no saved pipeline value.
OLD_HB_SFC_ACCOUNT = {HB_PARAM: "true", EULA_PARAM: "true", SFC_PARAM: "true"}


# --------------------------------------------------------------------------- defaults


def test_highbyte_with_sfc_defaults_to_disabled(tmp_path):
    """With SFC on, HighByte stays the UNS and SFC is the only SiteWise writer."""
    rc, context, out, _err, writes = _run(tmp_path, hb=True, sfc=True)
    assert rc == 0
    assert context["highbyte_sitewise_pipeline"] == "disabled"
    assert "HighByte SiteWise pipeline: disabled" in out
    assert "SFC" in out and "default" in out
    assert writes == [], "the default is never persisted"


def test_highbyte_without_sfc_keeps_the_pipeline_enabled(tmp_path):
    rc, context, out, _err, writes = _run(tmp_path, hb=True)
    assert rc == 0
    assert context["highbyte_sitewise_pipeline"] == "enabled"
    assert "HighByte SiteWise pipeline: enabled" in out
    assert writes == []


@pytest.mark.parametrize("sfc", [False, True])
def test_no_context_without_highbyte(tmp_path, sfc):
    rc, context, _out, _err, writes = _run(tmp_path, sfc=sfc)
    assert rc == 0
    assert "highbyte_sitewise_pipeline" not in context
    assert writes == []


def test_sticky_highbyte_and_sfc_account_defaults_to_disabled(tmp_path):
    rc, context, _out, _err, writes = _run(tmp_path, ssm=OLD_HB_SFC_ACCOUNT)
    assert rc == 0
    assert context["highbyte_enabled"] == "true" and context["sfc_enabled"] == "true"
    assert context["highbyte_sitewise_pipeline"] == "disabled"
    assert writes == []


def test_default_lets_the_sfc_watchdog_build():
    """The context deploy.sh passes for --highbyte --sfc is the one that
    makes the SFC stack build its freshness watchdog."""
    from stacks import _sfc_watchdog

    class _Node:
        def __init__(self, **ctx):
            self._ctx = ctx

        def try_get_context(self, key):
            return self._ctx.get(key)

    assert _sfc_watchdog.skip_reason(_Node(highbyte_sitewise_pipeline="disabled"), highbyte_enabled=True) is None
    assert _sfc_watchdog.skip_reason(_Node(highbyte_sitewise_pipeline="enabled"), highbyte_enabled=True)


def test_default_context_lets_the_watchdog_build_end_to_end(tmp_path):
    from stacks import _sfc_watchdog

    rc, context, *_ = _run(tmp_path, hb=True, sfc=True)
    assert rc == 0

    class _Node:
        def try_get_context(self, key):
            return context.get(key)

    assert _sfc_watchdog.skip_reason(_Node(), highbyte_enabled=True) is None


# --------------------------------------------------------------------------- migration warning


@pytest.mark.parametrize("kwargs", [{"ssm": OLD_HB_SFC_ACCOUNT}, {"hb": True, "sfc": True}],
                         ids=["sticky-account", "flags"])
def test_every_default_deploy_prints_the_default_line_first_and_repeat(tmp_path, kwargs):
    """No state is kept for it: the same line on the first deploy that takes
    the default and on every later one, always true, no past state claimed."""
    for _ in range(2):
        rc, context, out, _err, writes = _run(tmp_path, **kwargs)
        assert rc == 0
        assert context["highbyte_sitewise_pipeline"] == "disabled"
        assert DEFAULT_LINE in out
        assert out.index(DEFAULT_LINE) < out.index("CTX:")
        assert "previously" not in out and "Until now" not in out and "turns OFF" not in out
        assert writes == [], "the default is never saved as the pipeline value"


def test_the_default_does_not_pin_the_pipeline_value(tmp_path):
    """Turning SFC off later still gives HighByte its pipeline back."""
    rc, context, out, _err, _w = _run(tmp_path, ssm={HB_PARAM: "true", EULA_PARAM: "true"})
    assert rc == 0
    assert context["highbyte_sitewise_pipeline"] == "enabled"
    assert DEFAULT_LINE not in out


@pytest.mark.parametrize("kwargs", [
    {"ssm": {**OLD_HB_SFC_ACCOUNT, PARAM: "disabled"}},             # saved value
    {"ssm": {**OLD_HB_SFC_ACCOUNT, PARAM: "enabled"}},
    {"ssm": OLD_HB_SFC_ACCOUNT, "args": ("--highbyte-sitewise-pipeline", "disabled")},   # explicit
    {"ssm": OLD_HB_SFC_ACCOUNT, "args": ("--highbyte-sitewise-pipeline", "enabled")},
    {"ssm": {HB_PARAM: "true", EULA_PARAM: "true"}},                 # HighByte alone
    {"hb": True},
], ids=["saved-disabled", "saved-enabled", "explicit-disabled", "explicit-enabled", "hb-only-sticky", "hb-only-flag"])
def test_no_default_line_when_the_default_is_not_applied(tmp_path, kwargs):
    kwargs = dict(kwargs)
    args = kwargs.pop("args", ())
    rc, _context, out, _err, _w = _run(tmp_path, *args, **kwargs)
    assert rc == 0
    assert DEFAULT_LINE not in out


@pytest.mark.parametrize("kwargs", [{"ssm": OLD_HB_SFC_ACCOUNT}, {"hb": True, "sfc": True},
                                    {"hb": True, "sfc": True, "args": ("--highbyte-sitewise-pipeline", "enabled")}],
                         ids=["sticky", "flags", "explicit"])
def test_no_ssm_marker_is_read_or_written(tmp_path, kwargs):
    kwargs = dict(kwargs)
    args = kwargs.pop("args", ())
    rc, *_ = _run(tmp_path, *args, **kwargs)
    assert rc == 0
    touched = set(_all_reads(tmp_path)) | {w.split("=", 1)[0] for w in _all_writes(tmp_path)}
    assert touched <= {HB_PARAM, SFC_PARAM, PARAM, EULA_PARAM, "/adt/opcua-enabled"}, touched


def test_deploy_script_and_docs_have_no_marker():
    for path in (DEPLOY, DEPLOYMENT_DOC, REPO_ROOT / "docs" / "adr" / "0082-sfc-writes-sitewise-for-unattended-deployments.md"):
        assert "default-applied" not in path.read_text(encoding="utf-8"), path


# --------------------------------------------------------------------------- explicit value


@pytest.mark.parametrize("hb_sfc", [(True, True), (True, False)], ids=["hb+sfc", "hb-only"])
@pytest.mark.parametrize("value", ["enabled", "disabled"])
def test_explicit_value_wins_and_is_sticky(tmp_path, hb_sfc, value):
    hb, sfc = hb_sfc
    rc, context, out, _err, writes = _run(tmp_path, "--highbyte-sitewise-pipeline", value, hb=hb, sfc=sfc)
    assert rc == 0
    assert context["highbyte_sitewise_pipeline"] == value
    assert f"HighByte SiteWise pipeline: {value}" in out
    assert "--highbyte-sitewise-pipeline" in out
    assert writes == [f"{PARAM}={value}"]


def test_explicit_value_beats_the_sticky_one(tmp_path):
    rc, context, _out, _err, writes = _run(tmp_path, "--highbyte-sitewise-pipeline", "enabled",
                                           hb=True, sfc=True, ssm={PARAM: "disabled"})
    assert rc == 0
    assert context["highbyte_sitewise_pipeline"] == "enabled"
    assert writes == [f"{PARAM}=enabled"]


@pytest.mark.parametrize("sticky,hb_sfc", [("enabled", (True, True)), ("disabled", (True, False))])
def test_sticky_value_beats_the_default(tmp_path, sticky, hb_sfc):
    hb, sfc = hb_sfc
    rc, context, out, _err, writes = _run(tmp_path, hb=hb, sfc=sfc, ssm={PARAM: sticky})
    assert rc == 0
    assert context["highbyte_sitewise_pipeline"] == sticky
    assert PARAM in out
    assert writes == []


def test_two_writers_say_the_watchdog_is_not_built(tmp_path):
    rc, _context, out, _err, _w = _run(tmp_path, "--highbyte-sitewise-pipeline", "enabled", hb=True, sfc=True)
    assert rc == 0
    assert "watchdog" in out


def test_disabled_without_sfc_warns_nothing_writes_sitewise(tmp_path):
    rc, _context, out, err, _w = _run(tmp_path, "--highbyte-sitewise-pipeline", "disabled", hb=True)
    assert rc == 0
    assert "no continuous SiteWise writer" in (out + err)


# --------------------------------------------------------------------------- SSM read failures


#: Each read with a deploy that NEEDS it: (parameter, kwargs for _run).
NEEDED_READS = {
    HB_PARAM: {"sfc": True},                      # no --highbyte: the sticky enable decides
    SFC_PARAM: {"hb": True},                      # no --sfc: the sticky enable decides
    PARAM: {"hb": True, "sfc": True},             # no explicit value
}


@pytest.mark.parametrize("param", list(NEEDED_READS))
@pytest.mark.parametrize("error", [ACCESS_DENIED, THROTTLED, EXPIRED, TRANSPORT],
                         ids=["access-denied", "throttled", "expired", "transport"])
def test_an_ssm_read_failure_stops_before_any_write(tmp_path, param, error):
    """Only ParameterNotFound means "not set". Anything else could hide a
    saved choice (e.g. sticky SFC on, read as off, would leave HighByte's
    pipeline on beside SFC), so the deploy stops before any SSM write or CDK
    call and names the parameter."""
    ssm = {**OLD_HB_SFC_ACCOUNT}
    ssm.pop(param, None)
    rc, context, _out, err, _w = _run(tmp_path, ssm=ssm, errors={param: error}, **NEEDED_READS[param])
    assert rc != 0 and context == {}
    assert param in err
    assert error.split(":")[0] in err, "the AWS error is shown"
    assert _all_writes(tmp_path) == []


@pytest.mark.parametrize("param", [HB_PARAM, SFC_PARAM])
def test_a_read_failure_stops_a_deploy_without_flags_too(tmp_path, param):
    rc, context, _out, err, _w = _run(tmp_path, ssm={}, errors={param: ACCESS_DENIED})
    assert rc != 0 and context == {}
    assert param in err
    assert _all_writes(tmp_path) == []


ALL_PIPELINE_READS = (HB_PARAM, SFC_PARAM, PARAM)


def test_fully_explicit_deploy_reads_none_of_the_sticky_values(tmp_path):
    """--highbyte --sfc --highbyte-sitewise-pipeline disabled decides all
    three, so none of the three reads may stop it."""
    rc, context, _out, err, writes = _run(tmp_path, "--highbyte-sitewise-pipeline", "disabled", hb=True, sfc=True,
                                          errors={p: ACCESS_DENIED for p in ALL_PIPELINE_READS})
    assert rc == 0, err
    assert context["highbyte_enabled"] == "true" and context["sfc_enabled"] == "true"
    assert context["highbyte_sitewise_pipeline"] == "disabled"
    assert writes == [f"{PARAM}=disabled"]


@pytest.mark.parametrize("args,kwargs,skipped", [
    ((), {"hb": True}, HB_PARAM),                                       # --highbyte: enable read skipped
    ((), {"sfc": True}, SFC_PARAM),                                     # --sfc: enable read skipped
    (("--highbyte-sitewise-pipeline", "enabled"), {"hb": True, "sfc": True}, PARAM),
], ids=["hb-flag", "sfc-flag", "explicit-value"])
def test_a_read_made_irrelevant_by_flags_is_skipped(tmp_path, args, kwargs, skipped):
    kwargs = dict(kwargs)
    rc, _context, _out, err, _w = _run(tmp_path, *args, errors={skipped: ACCESS_DENIED}, **kwargs)
    assert rc == 0, err
    assert skipped not in err


@pytest.mark.parametrize("args,kwargs,needed", [
    (("--highbyte-sitewise-pipeline", "enabled"), {"hb": True}, SFC_PARAM),   # explicit value, SFC still unknown
    (("--highbyte-sitewise-pipeline", "disabled"), {"sfc": True, "ssm": {EULA_PARAM: "true"}}, HB_PARAM),
], ids=["sfc-still-needed", "highbyte-still-needed"])
def test_a_read_still_needed_beside_explicit_flags_stops_the_deploy(tmp_path, args, kwargs, needed):
    rc, context, _out, err, _w = _run(tmp_path, *args, errors={needed: ACCESS_DENIED}, **kwargs)
    assert rc != 0 and context == {}
    assert needed in err
    assert _all_writes(tmp_path) == []


def test_missing_parameters_mean_not_set(tmp_path):
    """ParameterNotFound on all three: a plain deploy, no HighByte, no SFC."""
    rc, context, _out, err, _w = _run(tmp_path)
    assert rc == 0
    assert "highbyte_enabled" not in context and "sfc_enabled" not in context
    assert "ERROR" not in err


def test_a_literal_none_value_is_refused_as_corrupt(tmp_path):
    """A successful read of the string "None" is a saved value, not "unset":
    only ParameterNotFound means unset. It is refused before any write."""
    rc, context, _out, err, _w = _run(tmp_path, hb=True, sfc=True, ssm={PARAM: "None"})
    assert rc != 0 and context == {}
    assert PARAM in err and "'None'" in err
    assert _all_writes(tmp_path) == []


@pytest.mark.parametrize("hb_sfc,default", [((True, True), "disabled"), ((True, False), "enabled")],
                         ids=["hb+sfc", "hb-only"])
def test_a_parameter_not_found_read_gets_the_default(tmp_path, hb_sfc, default):
    hb, sfc = hb_sfc
    rc, context, _out, err, writes = _run(tmp_path, hb=hb, sfc=sfc)
    assert rc == 0, err
    assert PARAM in _all_reads(tmp_path), "the read happened and returned ParameterNotFound"
    assert context["highbyte_sitewise_pipeline"] == default
    assert writes == []


def test_bad_sticky_value_is_refused_before_any_write(tmp_path):
    rc, context, _out, err, _w = _run(tmp_path, hb=True, sfc=True, ssm={PARAM: "maybe"})
    assert rc != 0 and context == {}
    assert PARAM in err and "maybe" in err
    assert _all_writes(tmp_path) == []


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("args", [("--highbyte-sitewise-pipeline", "off"),
                                  ("--highbyte-sitewise-pipeline", "Enabled"),
                                  ("--highbyte-sitewise-pipeline", ""),
                                  ("--highbyte-sitewise-pipeline",)])
def test_invalid_value_is_refused_before_any_aws_call(tmp_path, args):
    rc, context, _out, err, _w = _run(tmp_path, *args, hb=True, sfc=True)
    assert rc != 0 and context == {}
    assert "--highbyte-sitewise-pipeline" in err
    assert _all_writes(tmp_path) == []


@pytest.mark.parametrize("sfc", [False, True])
def test_flag_without_highbyte_is_refused(tmp_path, sfc):
    rc, context, _out, err, _w = _run(tmp_path, "--highbyte-sitewise-pipeline", "disabled", sfc=sfc)
    assert rc != 0 and context == {}
    assert "--highbyte" in err
    assert _all_writes(tmp_path) == []


# --------------------------------------------------------------------------- placement and docs


def test_blocks_sit_where_their_inputs_are_known():
    """The sticky reads run inside the HighByte section before its first SSM
    write; the decision runs after SFC and before CDK is called."""
    text = DEPLOY.read_text(encoding="utf-8")
    reads = text.index(READS[0])
    decide = text.index(DECIDE[0])
    first_write = text.index("aws ssm put-parameter")
    assert text.index(HB_SECTION) < reads < first_write
    # The decision reads SFC's resolved state, so it sits after the whole SFC
    # block: its header and its last resolution branch (the sticky keep).
    sfc = text.index("# SFC (optional, sticky; implies the OPC UA servers")
    assert sfc < text.index('echo "==> SFC is enabled (sticky): keeping it."', sfc) < decide
    assert decide < text.index("# OPC UA datasets (gzip CSVs, gitignored)")
    assert decide < text.index("npx cdk deploy")
    # The three reads go through the one helper, inside the reads block.
    reads_end = text.index(READS[1])
    for name in ALL_PIPELINE_READS:
        assert f'aws ssm get-parameter --name "{name}"' not in text, name
        assert reads < text.index(f'_ssm_sticky_get "{name}"') < reads_end, name


def test_usage_and_header_document_the_flag():
    text = DEPLOY.read_text(encoding="utf-8")
    usage = next(line for line in text.splitlines() if 'echo "Usage: ./deploy.sh' in line)
    assert "[--highbyte-sitewise-pipeline enabled|disabled]" in usage
    header = text[:text.index("set -euo pipefail")]
    assert "--highbyte-sitewise-pipeline" in header


def test_deployment_doc_ties_sole_writer_and_watchdog_to_sfc():
    """`disabled` without SFC leaves no SiteWise writer and no watchdog, so
    the doc must not promise either unconditionally."""
    doc = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    start = doc.index("### Optional `--highbyte-sitewise-pipeline")
    section = doc[start:doc.index("\n### ", start + 1)]
    intro = " ".join(section[:section.index("| Deployment |")].split())
    assert "when SFC is enabled" in intro
    assert "without SFC" in intro and "no continuous SiteWise writer" in intro


def test_deploy_and_stack_agree_on_the_key_and_values():
    from stacks import _highbyte_options as opts
    assert opts.SITEWISE_PIPELINE_KEY == "highbyte_sitewise_pipeline"
    assert opts.SITEWISE_PIPELINE_VALUES == ("enabled", "disabled")
