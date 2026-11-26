# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""deploy.sh's post-CDK steps fail loud on lost credentials and clear the APP cache.

Two defects seen on a ``--simulator`` redeploy (2026-10-06):

1. The post-CDK steps ([2f] per-system runtime refresh, [3b] query-target
   reconcile, [4/4] CloudFront invalidation) read AWS with
   ``2>/dev/null || echo ""``. When the credential disappeared after CDK
   finished, each step printed "nothing to refresh" / "skipping" and the deploy
   exited 0 with every per-system agent still on its OLD image.
   ``require_aws_session`` now runs at the start of each of those steps and
   stops the deploy (exit 1) naming the steps that did not run. A failed
   runtime list in [2f] lets [3b]/[4/4] run, then fails the deploy at the end.
2. [4/4] took the first distribution with an ELB origin. On an account with a
   second ALB-fronted distribution (the ERP dashboard on ``--simulator``) both
   qualify and ``| [0]`` picked the ERP one. It now resolves the distribution
   from ADTComputeStack's own ``ADTCloudFrontUrl``.

Further rules this pins:

N1. ``require_aws_session`` maps the step name to the steps still to run. An
    unknown name used to be reported as step [4/4]; it is now an internal error
    naming the bad value.
N2. The remaining post-CDK failures that only warned now fail the deploy at
    the end, naming what failed, while the later steps still run: a per-system
    runtime that could not be refreshed (named), a missing agent image in [2f],
    the [3b] reconcile exiting non-zero or the query gateway id being unreadable,
    and in [4/4] the app distribution not being found or the invalidation
    request failing. Still non-fatal: no per-system runtimes (nothing to
    refresh) and an invalidation that was accepted but had not finished when
    the wait gave up.
N3. Scratch files (the CDK log, captured AWS error text) live in one per-run
    ``mktemp -d`` dir instead of fixed /tmp paths shared by every deploy on the
    host. The dir is removed on a clean exit and kept, with its path printed,
    when the deploy fails or is interrupted (INT -> 130, TERM -> 143).

[2f] waits after its updates: a runtime counts as
refreshed only at READY at the version its update returned; failed/other
statuses, unreadable status and the bounded time limit are named failures. A
ConflictException (already UPDATING) waits, then rebuilds the update from the
runtime's current settings and retries once; no runtime settings are written
to disk.

The behavioral tests run the real deploy.sh text under bash with a fake ``aws``
on PATH (no network, no AWS).
"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]  # agentic_digital_thread/

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _deploy_sh() -> str:
    return (_ADT_ROOT / "deploy.sh").read_text()


def _function(name: str, required: bool = True) -> str:
    src = _deploy_sh()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", src, re.S | re.M)
    if not required and not m:
        return ""
    assert m, f"deploy.sh function {name}() not found"
    return m.group(0)


def _postcdk_helpers() -> str:
    """The helper functions every post-CDK step block needs."""
    return (_function("cfn_output") + _function("require_aws_session")
            + _function("record_postcdk_failure", required=False)
            + 'POSTCDK_FAILED=""\n')


def _step4_block() -> str:
    src = _deploy_sh()
    start = src.find('echo "==> [4/4] Invalidating CloudFront cache..."')
    assert start != -1, "deploy.sh [4/4] step not found"
    return src[start:]


def _step3b_block() -> str:
    src = _deploy_sh()
    start = src.find('echo "==> [3b] Reconciling query gateway targets (auto-heal stale runtime ARNs)..."')
    end = src.find("# ── 4. Invalidate CloudFront cache", start)
    assert start != -1 and end != -1, "deploy.sh [3b] step not found"
    return src[start:end]


def _fake_aws(tmp_path: Path, body: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    aws = bindir / "aws"
    aws.write_text("#!/usr/bin/env bash\n" + body)
    aws.chmod(0o755)
    return bindir


def _script_file(directory: Path, text: str) -> Path:
    """Write *text* to a new script file in *directory*; bash runs it by path."""
    fd, name = tempfile.mkstemp(prefix="harness-", suffix=".sh", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return Path(name)


def _run(tmp_path: Path, bindir: Path, script: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
    }
    prelude = ('set -euo pipefail\nREGION=us-east-1\n_AWS_PROFILE_FLAG=""\n'
               'DEPLOY_TMP="$HOME/deploy-tmp"; mkdir -p "$DEPLOY_TMP"\n')
    return subprocess.run(
        ["bash", str(_script_file(tmp_path, prelude + script))],
        env=env, capture_output=True, text=True, timeout=60, cwd=cwd,
    )


# --- tripwires ----------------------------------------------------------------

@pytest.mark.parametrize("header,step", [
    ('echo "==> [2f] Refreshing per-system agent runtimes to the current image..."', "2f"),
    ('echo "==> [3b] Reconciling query gateway targets (auto-heal stale runtime ARNs)..."', "3b"),
    ('echo "==> [4/4] Invalidating CloudFront cache..."', "4/4"),
])
def test_each_postcdk_step_checks_the_session_first(header, step):
    src = _deploy_sh()
    i = src.find(header)
    assert i != -1, f"{header!r} not found in deploy.sh"
    nxt = src[i + len(header):].lstrip("\n").splitlines()[0]
    assert nxt.strip() == f"require_aws_session {step}", (
        f"[{step}] must call require_aws_session right after its header; found {nxt!r}"
    )


def test_session_check_is_defined_before_first_use():
    src = _deploy_sh()
    assert src.find("require_aws_session() {") < src.find("require_aws_session 2f")


def test_step4_no_longer_guesses_by_elb_origin():
    block = _step4_block()
    assert "contains(Origins.Items[0].DomainName" not in block
    assert '"ADTComputeStack" "ADTCloudFrontUrl"' in block


def test_2f_failed_list_is_not_reported_as_empty():
    section = _step2f_block()
    assert "2>/dev/null || echo \"\")\n  if [ -z \"${RT_NAMES// /}\" ]" not in section
    assert "RT_LIST_FAILED=1" in section
    assert "were NOT refreshed" in section


def _step2f_block() -> str:
    src = _deploy_sh()
    start = src.find('echo "==> [2f] Refreshing per-system agent runtimes to the current image..."')
    end = src.find("# ── 3. Summary", start)
    assert start != -1 and end != -1, "deploy.sh [2f] step not found"
    return src[start:end]


def _final_status_block() -> str:
    src = _deploy_sh()
    start = src.find('if [ -n "${POSTCDK_FAILED:-}" ]; then')
    assert start != -1, "deploy.sh end-of-run POSTCDK_FAILED check not found"
    return src[start:]


def _run_2f(tmp_path: Path, list_body: str) -> subprocess.CompletedProcess:
    bindir = _fake_aws(tmp_path, (
        'case "$1 $2" in\n'
        '  "sts get-caller-identity") echo \'{"Account":"111122223333"}\' ;;\n'
        f'  "bedrock-agentcore-control list-agent-runtimes") {list_body} ;;\n'
        'esac\n'
    ))
    script = (_postcdk_helpers() + 'ORCH_IMAGE=img:1\n'
              + _step2f_block() + "\necho REACHED_AFTER_2F\n" + _final_status_block())
    return _run(tmp_path, bindir, script)


def test_2f_list_failure_fails_the_deploy_after_later_steps_run(tmp_path):
    r = _run_2f(tmp_path, 'echo "ExpiredTokenException: token expired" >&2; exit 254')
    assert r.returncode == 1
    assert "REACHED_AFTER_2F" in r.stdout  # [3b]/[4/4] still get their turn
    assert "per-system runtimes were NOT refreshed" in r.stdout
    assert "ExpiredTokenException" in r.stdout
    assert "nothing to refresh" not in r.stdout
    assert "post-CDK steps did not complete" in r.stderr
    assert "[2f] per-system runtime refresh (could not list runtimes)" in r.stderr


def test_2f_empty_list_is_still_a_clean_no_op(tmp_path):
    r = _run_2f(tmp_path, "echo ''")
    assert r.returncode == 0, r.stderr
    assert "nothing to refresh (no-op)" in r.stdout
    assert "REACHED_AFTER_2F" in r.stdout


# --- behavior -----------------------------------------------------------------

def test_require_aws_session_exits_nonzero_when_credentials_are_gone(tmp_path):
    bindir = _fake_aws(tmp_path, (
        'if [ "$1 $2" = "sts get-caller-identity" ]; then\n'
        '  echo "The config profile (adtdemo) could not be found" >&2; exit 255\n'
        'fi\nexit 0\n'
    ))
    r = _run(tmp_path, bindir, _function("require_aws_session")
             + 'require_aws_session 2f\necho REACHED_NEXT_STEP\n')
    assert r.returncode == 1
    assert "REACHED_NEXT_STEP" not in r.stdout
    assert "AWS credentials stopped working before step [2f]" in r.stderr
    assert "could not be found" in r.stderr
    assert "did NOT run" in r.stderr


@pytest.mark.parametrize("step,named,not_named", [
    ("2f", ["[2f]", "[3b]", "[4/4]"], []),
    ("3b", ["[3b]", "[4/4]"], ["[2f] per-system"]),
    ("4/4", ["[4/4]"], ["[2f] per-system", "[3b] query-target"]),
])
def test_require_aws_session_names_only_the_steps_left(tmp_path, step, named, not_named):
    bindir = _fake_aws(tmp_path, 'echo "Unable to locate credentials" >&2; exit 255\n')
    r = _run(tmp_path, bindir, _function("require_aws_session") + f"require_aws_session {step}\n")
    assert r.returncode == 1
    tail = r.stderr.split("The CloudFormation stacks are deployed, but", 1)[1]
    for s in named:
        assert s in tail
    for s in not_named:
        assert s not in tail


def test_require_aws_session_passes_with_a_working_session(tmp_path):
    bindir = _fake_aws(tmp_path, (
        'if [ "$1 $2" = "sts get-caller-identity" ]; then\n'
        '  echo \'{"Account":"111122223333"}\'; exit 0\n'
        'fi\nexit 0\n'
    ))
    r = _run(tmp_path, bindir, _function("require_aws_session")
             + 'require_aws_session 3b\necho REACHED_NEXT_STEP\n')
    assert r.returncode == 0, r.stderr
    assert "REACHED_NEXT_STEP" in r.stdout


_STEP4_FAKE = r'''
log="$HOME/aws_calls.log"; echo "$*" >> "$log"
case "$1 $2" in
  "sts get-caller-identity") echo '{"Account":"111122223333"}' ;;
  "cloudformation describe-stacks") echo "https://dapp1234567.cloudfront.net" ;;
  "cloudfront list-distributions")
    # Return the app distribution only when asked for the app's own domain.
    case "$*" in
      *"DomainName=='dapp1234567.cloudfront.net'"*) echo "EAPPDIST0001" ;;
      *) echo "EERPDIST0002" ;;
    esac ;;
  "cloudfront create-invalidation") echo "IINV0001" ;;
  "cloudfront wait") exit 0 ;;
esac
'''


def test_step4_invalidates_the_app_distribution_from_the_stack_output(tmp_path):
    bindir = _fake_aws(tmp_path, _STEP4_FAKE)
    r = _run(tmp_path, bindir, _postcdk_helpers() + _step4_block())
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "aws_calls.log").read_text()
    assert "--distribution-id EAPPDIST0001" in calls
    assert "EERPDIST0002" not in r.stdout
    assert "CloudFront cache cleared" in r.stdout


def test_step4_unreadable_output_fails_the_deploy(tmp_path):
    bindir = _fake_aws(tmp_path, _STEP4_FAKE.replace(
        'echo "https://dapp1234567.cloudfront.net"', "exit 254"))
    r = _run(tmp_path, bindir, _postcdk_helpers() + _step4_block())
    assert r.returncode == 1
    assert "the app cache was NOT cleared" in r.stdout
    assert "create-invalidation" not in (tmp_path / "aws_calls.log").read_text()
    assert "[4/4] CloudFront invalidation" in r.stderr


def test_step4_distribution_not_found_fails_the_deploy(tmp_path):
    bindir = _fake_aws(tmp_path, _STEP4_FAKE.replace(
        'echo "EAPPDIST0001"', 'echo "None"'))
    r = _run(tmp_path, bindir, _postcdk_helpers() + _step4_block())
    assert r.returncode == 1
    assert "App CloudFront distribution not found" in r.stdout
    assert "create-invalidation" not in (tmp_path / "aws_calls.log").read_text()
    assert "[4/4] CloudFront invalidation (app distribution not found)" in r.stderr


def test_step4_failed_invalidation_request_fails_the_deploy(tmp_path):
    bindir = _fake_aws(tmp_path, _STEP4_FAKE.replace(
        '"cloudfront create-invalidation") echo "IINV0001" ;;',
        '"cloudfront create-invalidation") echo "AccessDenied: not allowed" >&2; exit 254 ;;'))
    r = _run(tmp_path, bindir, _postcdk_helpers() + _step4_block())
    assert r.returncode == 1
    assert "invalidation request FAILED for EAPPDIST0001" in r.stdout
    assert "AccessDenied: not allowed" in r.stdout  # the AWS error is shown, not swallowed
    assert "CloudFront cache cleared" not in r.stdout
    assert "[4/4] CloudFront invalidation (request failed for EAPPDIST0001)" in r.stderr


def test_step4_unfinished_wait_is_not_a_failure(tmp_path):
    # The invalidation was accepted; CloudFront finishes it on its own.
    bindir = _fake_aws(tmp_path, _STEP4_FAKE.replace(
        '"cloudfront wait") exit 0 ;;', '"cloudfront wait") exit 255 ;;'))
    r = _run(tmp_path, bindir, _postcdk_helpers() + _step4_block())
    assert r.returncode == 0, r.stderr
    assert "IINV0001" in r.stdout
    assert "still in progress" in r.stdout
    assert "CloudFront cache cleared" not in r.stdout


# --- N1: step names ------------------------------------------------------------

def _session_case_labels() -> set[str]:
    body = _function("require_aws_session")
    return set(re.findall(r"^\s*([0-9a-z/]+)\)\s", body, re.M))


def test_every_require_aws_session_call_names_a_known_step():
    calls = set(re.findall(r"^require_aws_session (\S+)$", _deploy_sh(), re.M))
    assert calls == {"2f", "3b", "4/4"}
    assert calls <= _session_case_labels(), (
        f"require_aws_session has no explicit case for {sorted(calls - _session_case_labels())}"
    )


@pytest.mark.parametrize("bad", ["4", "2F", "3c", ""])
def test_require_aws_session_rejects_an_unknown_step_name(tmp_path, bad):
    bindir = _fake_aws(tmp_path, (
        'if [ "$1 $2" = "sts get-caller-identity" ]; then\n'
        '  echo \'{"Account":"111122223333"}\'; exit 0\n'
        'fi\nexit 0\n'
    ))
    r = _run(tmp_path, bindir, _function("require_aws_session")
             + f'require_aws_session "{bad}"\necho REACHED_NEXT_STEP\n')
    assert r.returncode != 0
    assert "REACHED_NEXT_STEP" not in r.stdout
    assert "internal error" in r.stderr
    assert f"unknown step name '{bad}'" in r.stderr
    assert "[4/4] CloudFront invalidation" not in r.stderr  # no silent default


# --- N3: per-run scratch dir ---------------------------------------------------

def _code_lines() -> list[str]:
    return [ln for ln in _deploy_sh().splitlines() if not ln.lstrip().startswith("#")]


def test_deploy_sh_writes_no_fixed_tmp_paths():
    offenders = [ln.strip() for ln in _code_lines() if "/tmp/" in ln]
    assert not offenders, f"fixed /tmp paths in deploy.sh: {offenders}"


def test_deploy_sh_has_exactly_one_exit_trap():
    traps = [ln.strip() for ln in _code_lines() if re.search(r"\btrap\b.*\bEXIT\b", ln)]
    assert len(traps) == 1, traps


def test_scratch_dir_is_created_once_near_the_top():
    src = _deploy_sh()
    assert len(re.findall(r"^setup_deploy_tmp$", src, re.M)) == 1
    assert src.count("mktemp -d") == 1
    # Before anything writes to it (the CDK log) and before any step can fail.
    # (Its own exit trap, defined just above the call, cleans part of it.)
    setup_call = src.find("\nsetup_deploy_tmp\n")
    first_use = src.find("$DEPLOY_TMP/", setup_call)
    assert -1 < setup_call < src.find("check_prereqs\n") < first_use


def test_cdk_log_lives_in_the_run_dir_and_is_named_on_failure():
    src = _deploy_sh()
    # Appended, so a second deploy phase in the same run keeps the first one's output.
    assert '| tee -a "$DEPLOY_TMP/cdk_deploy.log"' in src
    fail = src[src.find('if [ "$CDK_EXIT" -ne 0 ]; then'):]
    fail = fail[:fail.find("\nfi\n")]
    assert "$DEPLOY_TMP/cdk_deploy.log" in fail


def _run_tmp_lifecycle(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    script = ("set -euo pipefail\n" + _function("_deploy_tmp_on_exit")
              + _function("setup_deploy_tmp") + "setup_deploy_tmp\n"
              + 'echo "DIR=$DEPLOY_TMP"\n' + body)
    return subprocess.run(
        ["bash", str(_script_file(tmp_path, script))], capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(tmp_path)},
    )


def _dir_of(r: subprocess.CompletedProcess) -> Path:
    m = re.search(r"^DIR=(.+)$", r.stdout, re.M)
    assert m, r.stdout
    return Path(m.group(1))


def test_run_dir_is_private_per_run_and_removed_on_success(tmp_path):
    a = _run_tmp_lifecycle(tmp_path, 'echo x > "$DEPLOY_TMP/cdk_deploy.log"\n')
    b = _run_tmp_lifecycle(tmp_path, 'echo y > "$DEPLOY_TMP/cdk_deploy.log"\n')
    assert a.returncode == 0 and b.returncode == 0, a.stderr + b.stderr
    assert _dir_of(a) != _dir_of(b)
    assert _dir_of(a).parent == tmp_path  # honors TMPDIR
    assert not _dir_of(a).exists() and not _dir_of(b).exists()


def test_run_dir_is_kept_and_named_when_the_deploy_fails(tmp_path):
    r = _run_tmp_lifecycle(tmp_path, 'echo boom > "$DEPLOY_TMP/cdk_deploy.log"\nexit 3\n')
    assert r.returncode == 3  # the trap does not change the exit status
    d = _dir_of(r)
    assert (d / "cdk_deploy.log").read_text() == "boom\n"
    assert str(d) in r.stderr


def test_empty_run_dir_is_removed_even_on_failure(tmp_path):
    # e.g. a bad flag: nothing was written, so there is nothing to point at.
    r = _run_tmp_lifecycle(tmp_path, "exit 1\n")
    assert r.returncode == 1
    assert not _dir_of(r).exists()
    assert str(_dir_of(r)) not in r.stderr


def test_failed_run_keeps_error_text_but_not_the_2f_wait_state(tmp_path):
    r = _run_tmp_lifecycle(tmp_path, (
        'mkdir -p "$DEPLOY_TMP/rt_refresh/state"\n'
        'echo adt_erp-BBB > "$DEPLOY_TMP/rt_refresh/state/adt_erp.id"\n'
        'echo "AccessDenied" > "$DEPLOY_TMP/rt_refresh/adt_erp.err"\n'
        "exit 1\n"))
    assert r.returncode == 1
    d = _dir_of(r)
    assert (d / "rt_refresh" / "adt_erp.err").read_text() == "AccessDenied\n"
    assert not (d / "rt_refresh" / "state").exists()


def _default_int_term() -> None:
    """Run in the child before bash starts: default INT/TERM dispositions.

    A signal ignored on entry cannot be trapped by a non-interactive shell,
    and the test runner may ignore one.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


@pytest.mark.parametrize("sig,rc", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_interrupt_keeps_the_run_dir_and_exits_with_the_signal_status(tmp_path, sig, rc):
    ready = tmp_path / "dir.txt"
    script = ("set -euo pipefail\n" + _function("_deploy_tmp_on_exit")
              + _function("setup_deploy_tmp") + "setup_deploy_tmp\n"
              + 'echo "cdk output" > "$DEPLOY_TMP/cdk_deploy.log"\n'
              + 'mkdir -p "$DEPLOY_TMP/rt_refresh/state"\n'
              + 'echo adt_erp-BBB > "$DEPLOY_TMP/rt_refresh/state/adt_erp.id"\n'
              + f'printf "%s" "$DEPLOY_TMP" > "{ready}"\n'
              + "sleep 30\necho NOT_INTERRUPTED\n")
    p = subprocess.Popen(
        ["bash", str(_script_file(tmp_path, script))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        preexec_fn=_default_int_term,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(tmp_path)},
    )
    try:
        deadline = time.monotonic() + 15
        while not (ready.exists() and ready.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "harness never reached its sleep"
        time.sleep(0.2)
        os.killpg(p.pid, sig)  # the whole group, as Ctrl-C or a supervisor does
        out, err = p.communicate(timeout=20)
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
    d = Path(ready.read_text())
    assert p.returncode == rc, (p.returncode, out, err)
    assert "NOT_INTERRUPTED" not in out
    assert (d / "cdk_deploy.log").read_text() == "cdk output\n"
    assert f"Logs from this run (CDK output, AWS error text): {d}" in err
    assert not (d / "rt_refresh").exists()  # the 2f wait state never outlives the run


# --- N2: [2f] per-system update failures ---------------------------------------

_RT_JSON = ('{"roleArn":"arn:aws:iam::111122223333:role/r",'
            '"environmentVariables":{"SYSTEM_ID":"x","SYSTEM_AGENT_MODEL":"old-model"},'
            '"authorizerConfiguration":{"customJWTAuthorizer":{"discoveryUrl":"https://old.example/d"}},'
            '"networkConfiguration":{"networkMode":"PUBLIC"}}')

# What another writer (a Settings model swap, an authorizer repair) leaves
# behind while 2f waits on its ConflictException.
_RT_JSON_AFTER = ('{"roleArn":"arn:aws:iam::111122223333:role/r",'
                  '"environmentVariables":{"SYSTEM_ID":"x","SYSTEM_AGENT_MODEL":"new-model"},'
                  '"authorizerConfiguration":{"customJWTAuthorizer":{"discoveryUrl":"https://new.example/d"}},'
                  '"networkConfiguration":{"networkMode":"PUBLIC"}}')

_2F_FAKE = r'''
log="$HOME/aws_calls.log"; echo "$*" >> "$log"
rt_id=""; prev=""
for a in "$@"; do [ "$prev" = "--agent-runtime-id" ] && rt_id="$a"; prev="$a"; done
updates() { cat "$HOME/updates_$rt_id" 2>/dev/null || echo 0; }
case "$1 $2" in
  "sts get-caller-identity") echo '{"Account":"111122223333"}' ;;
  "bedrock-agentcore-control list-agent-runtimes")
    case "$*" in
      *"agentRuntimeName=='adt_mes'"*) echo "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_mes-AAA" ;;
      *"agentRuntimeName=='adt_erp'"*) echo "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_erp-BBB" ;;
      *"agentRuntimeName=='adt_gone'"*) echo "None" ;;
      *) echo "__NAMES__" ;;
    esac ;;
  "bedrock-agentcore-control get-agent-runtime")
    case "$*" in
      *"containerUri"*)
        # Image read: $HOME/image_<id>, else this deploy's test image.
        if [ -f "$HOME/image_err_$rt_id" ]; then cat "$HOME/image_err_$rt_id" >&2; exit 254; fi
        if [ -f "$HOME/image_$rt_id" ]; then cat "$HOME/image_$rt_id"; else echo "img:1"; fi ;;
      *"--query"*)
        # Status poll: pop the next "STATUS<tab>VERSION<tab>REASON" line of
        # $HOME/status_<id> (the last line repeats); READY at the version the
        # latest update returned when no sequence was given.
        [ -f "$HOME/slow_poll_$rt_id" ] && sleep "$(cat "$HOME/slow_poll_$rt_id")"
        seq="$HOME/status_$rt_id"
        if [ -f "$seq.err" ]; then cat "$seq.err" >&2; exit 254; fi
        if [ -s "$seq" ]; then
          head -1 "$seq"
          if [ "$(wc -l < "$seq")" -gt 1 ]; then tail -n +2 "$seq" > "$seq.tmp"; mv "$seq.tmp" "$seq"; fi
        else
          printf 'READY\t%s\tNone\n' "$(( $(updates) + 1 ))"
        fi ;;
      *)
        # Full read: another writer's settings once any update was attempted.
        if [ -f "$HOME/rtjson_after_$rt_id" ] && [ "$(updates)" -ge 1 ]; then
          cat "$HOME/rtjson_after_$rt_id"
        else
          echo '__RT_JSON__'
        fi ;;
    esac ;;
  "bedrock-agentcore-control update-agent-runtime")
    n=$(( $(updates) + 1 )); echo "$n" > "$HOME/updates_$rt_id"
    # Record any runtime settings 2f has written into its run dir.
    grep -rlE 'old-model|new-model|old\.example|new\.example' "$HOME/deploy-tmp" \
      >> "$HOME/settings_on_disk.log" 2>/dev/null || true
    # Update call n returns runtime version n+1 (--query agentRuntimeVersion).
    case "$*" in
      *"adt_erp-BBB"*) __ERP_UPDATE__ ;;
      *) echo "$((n + 1))" ;;
    esac ;;
esac
'''

_CONFLICT = ('echo "An error occurred (ConflictException) when calling the UpdateAgentRuntime '
             'operation: Agent runtime adt_erp-BBB is in UPDATING state" >&2; exit 254')
_ACCEPT = 'echo "$((n + 1))"'


def _run_2f_full(tmp_path: Path, names: str, erp_update: str = _ACCEPT,
                 image: str = "img:1", statuses: dict | None = None,
                 status_err: dict | None = None, timeout: int | str = 30,
                 interval: int | str = 1, after_json: dict | None = None,
                 slow_poll: dict | None = None,
                 rt_json: str = _RT_JSON) -> subprocess.CompletedProcess:
    body = (_2F_FAKE.replace("__NAMES__", names).replace("__RT_JSON__", rt_json)
            .replace("__ERP_UPDATE__", erp_update))
    bindir = _fake_aws(tmp_path, body)
    for rt_id, lines in (statuses or {}).items():
        (tmp_path / f"status_{rt_id}").write_text("".join(f"{ln}\n" for ln in lines))
    for rt_id, text in (status_err or {}).items():
        (tmp_path / f"status_{rt_id}.err").write_text(text + "\n")
    for rt_id, text in (after_json or {}).items():
        (tmp_path / f"rtjson_after_{rt_id}").write_text(text + "\n")
    for rt_id, secs in (slow_poll or {}).items():
        (tmp_path / f"slow_poll_{rt_id}").write_text(f"{secs}\n")
    script = (_postcdk_helpers() + f'ORCH_IMAGE="{image}"\nIDENTITY_CLAIM_SECRET_ARN=""\n'
              + "SPAN_EMISSION=false\n"
              + f"ADT_RUNTIME_READY_TIMEOUT_SECONDS={timeout}\n"
              + f"ADT_RUNTIME_READY_POLL_SECONDS={interval}\n"
              + _step2f_block() + "\necho REACHED_AFTER_2F\n" + _final_status_block())
    return _run(tmp_path, bindir, script, cwd=_ADT_ROOT)


def _calls(tmp_path: Path, op: str, rt_id: str, polls: bool | None = None) -> list[str]:
    """aws calls of *op* against *rt_id*; polls=True/False keeps only status
    polls (--query) / only full reads."""
    out = []
    for ln in (tmp_path / "aws_calls.log").read_text().splitlines():
        if not ln.startswith(f"bedrock-agentcore-control {op} ") or f"--agent-runtime-id {rt_id} " not in ln:
            continue
        if op == "get-agent-runtime" and polls is not None and (" --query " in ln) != polls:
            continue
        out.append(ln)
    return out


def _updates(tmp_path: Path, rt_id: str) -> int:
    f = tmp_path / f"updates_{rt_id}"
    return int(f.read_text()) if f.exists() else 0


def _failure_tail(r: subprocess.CompletedProcess) -> str:
    assert "post-CDK steps did not complete" in r.stderr, r.stdout + r.stderr
    return r.stderr.split("post-CDK steps did not complete", 1)[1]


def test_2f_accepted_then_ready_is_success(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp",
                     statuses={"adt_erp-BBB": ["UPDATING\t2\tNone", "UPDATING\t2\tNone", "READY\t2\tNone"]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 refreshed (READY), 0 failed" in r.stdout
    assert "adt_erp: READY on the current image" in r.stdout
    polls = _calls(tmp_path, "get-agent-runtime", "adt_erp-BBB", polls=True)
    assert len(polls) == 3
    # Each poll is a bounded CLI call.
    assert all("--cli-connect-timeout 10 --cli-read-timeout 30" in p for p in polls)


def test_2f_every_control_plane_call_is_bounded(tmp_path):
    """The list, the per-runtime ARN lookup and full read, and the update are
    bounded CLI calls too, so one hung call cannot hold 2f past its wait."""
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = [ln for ln in (tmp_path / "aws_calls.log").read_text().splitlines()
             if ln.startswith("bedrock-agentcore-control ")]
    ops = {ln.split()[1] for ln in calls}
    assert ops == {"list-agent-runtimes", "get-agent-runtime", "update-agent-runtime"}
    unbounded = [ln for ln in calls
                 if "--cli-connect-timeout 10" not in ln or "--cli-read-timeout" not in ln]
    assert unbounded == []
    updates = [ln for ln in calls if ln.split()[1] == "update-agent-runtime"]
    assert len(updates) == 2 and all("--cli-read-timeout 60" in ln for ln in updates)
    assert "post-CDK steps did not complete" not in r.stderr
    # Only AWS error text is left in the run dir; the wait's bookkeeping is gone.
    rt_dir = tmp_path / "deploy-tmp" / "rt_refresh"
    assert not (rt_dir / "state").exists()
    assert all(p.name.endswith((".err", ".status_err")) for p in rt_dir.iterdir())


def test_2f_failed_update_names_the_system_and_fails_the_deploy(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp",
                     erp_update='echo "An error occurred (ValidationException): bad env" >&2; exit 254')
    assert r.returncode == 1
    assert "REACHED_AFTER_2F" in r.stdout  # later steps still run
    assert "adt_mes: update accepted" in r.stdout
    assert "adt_erp: update FAILED" in r.stdout
    assert "ValidationException" in r.stdout
    assert "1 refreshed (READY), 1 failed" in r.stdout
    assert _updates(tmp_path, "adt_erp-BBB") == 1  # a non-conflict rejection is not retried
    tail = _failure_tail(r)
    assert "[2f] per-system runtime refresh failed for: adt_erp (update rejected)" in tail
    assert "adt_mes" not in tail


@pytest.mark.parametrize("status,reason", [
    ("UPDATE_FAILED", "UPDATE_FAILED: image pull failed"),
    ("CREATE_FAILED", "CREATE_FAILED: image pull failed"),
    ("DELETING", "unexpected status DELETING"),
])
def test_2f_accepted_then_failed_status_fails_the_deploy(tmp_path, status, reason):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp",
                     statuses={"adt_erp-BBB": ["UPDATING\t2\tNone", f"{status}\t2\timage pull failed"]})
    assert r.returncode == 1
    assert "REACHED_AFTER_2F" in r.stdout
    assert "1 refreshed (READY), 1 failed" in r.stdout
    tail = _failure_tail(r)
    assert f"adt_erp ({reason})" in tail
    assert "adt_mes" not in tail


def test_2f_never_ready_within_the_timeout_fails_the_deploy(tmp_path):
    import time
    t0 = time.monotonic()
    # 3 s, not 1: bash SECONDS is whole seconds, so a 1 s limit can expire
    # within milliseconds, before the first status read this test needs.
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", statuses={"adt_erp-BBB": ["UPDATING\t2\tNone"]},
                     timeout=3, interval=1)
    assert time.monotonic() - t0 < 20  # the wait is bounded
    assert r.returncode == 1
    assert "REACHED_AFTER_2F" in r.stdout
    tail = _failure_tail(r)
    assert "adt_erp (not READY after 3s, last status UPDATING)" in tail
    assert "adt_mes" not in tail


def test_2f_status_read_failure_fails_the_deploy(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", status_err={
        "adt_erp-BBB": "An error occurred (AccessDeniedException) when calling the GetAgentRuntime operation"})
    assert r.returncode == 1
    assert "AccessDeniedException" in r.stdout
    tail = _failure_tail(r)
    assert "adt_erp (could not read status)" in tail
    assert "adt_mes" not in tail


def test_2f_conflict_waits_for_updating_then_retries_once(tmp_path):
    erp_update = 'if [ "$n" -eq 1 ]; then ' + _CONFLICT + '; fi; ' + _ACCEPT
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", erp_update=erp_update, statuses={
        "adt_erp-BBB": ["UPDATING\t2\tNone", "READY\t2\tNone", "UPDATING\t3\tNone", "READY\t3\tNone"]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _updates(tmp_path, "adt_erp-BBB") == 2
    assert "adt_erp: already UPDATING" in r.stdout
    assert "adt_erp: retrying the update" in r.stdout
    assert "2 refreshed (READY), 0 failed" in r.stdout


def test_2f_conflict_retry_rejected_again_fails_the_deploy(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", erp_update=_CONFLICT, statuses={
        "adt_erp-BBB": ["UPDATING\t2\tNone", "READY\t2\tNone"]})
    assert r.returncode == 1
    assert _updates(tmp_path, "adt_erp-BBB") == 2  # retried exactly once
    tail = _failure_tail(r)
    assert "adt_erp (update rejected again after waiting)" in tail
    assert "adt_mes" not in tail


def test_2f_conflict_that_never_leaves_updating_fails_the_deploy(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", erp_update=_CONFLICT,
                     statuses={"adt_erp-BBB": ["UPDATING\t2\tNone"]}, timeout=3, interval=1)
    assert r.returncode == 1
    assert _updates(tmp_path, "adt_erp-BBB") == 1  # never retried
    tail = _failure_tail(r)
    assert "adt_erp (still UPDATING after 3s, update not retried)" in tail


# --- the retry rebuilds the update from the runtime's current state ---

def test_2f_retry_rebuilds_the_update_from_the_runtimes_current_settings(tmp_path):
    # 2f reads adt_erp, another writer's update lands first (ConflictException),
    # and changes the agent model and the authorizer. The retry must send THOSE
    # settings with the deploy's image, not what 2f read before the conflict.
    erp_update = 'if [ "$n" -eq 1 ]; then ' + _CONFLICT + '; fi; ' + _ACCEPT
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", erp_update=erp_update,
                     after_json={"adt_erp-BBB": _RT_JSON_AFTER},
                     statuses={"adt_erp-BBB": ["UPDATING\t2\tNone", "READY\t2\tNone", "READY\t3\tNone"]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_calls(tmp_path, "get-agent-runtime", "adt_erp-BBB", polls=False)) == 2  # re-read
    first, retry = _calls(tmp_path, "update-agent-runtime", "adt_erp-BBB")
    assert "old-model" in first and "https://old.example/d" in first
    assert "new-model" in retry and "https://new.example/d" in retry
    assert "old-model" not in retry and "https://old.example/d" not in retry
    assert '"containerUri":"img:1"' in retry
    assert "2 refreshed (READY), 0 failed" in r.stdout


def test_2f_never_writes_runtime_settings_to_disk(tmp_path):
    erp_update = 'if [ "$n" -eq 1 ]; then ' + _CONFLICT + '; fi; ' + _ACCEPT
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", erp_update=erp_update,
                     after_json={"adt_erp-BBB": _RT_JSON_AFTER},
                     statuses={"adt_erp-BBB": ["UPDATING\t2\tNone", "READY\t2\tNone", "READY\t3\tNone"]})
    assert r.returncode == 0, r.stdout + r.stderr
    on_disk = tmp_path / "settings_on_disk.log"
    assert not on_disk.exists() or on_disk.read_text() == ""  # checked at every update call


# --- the update keeps every optional setting the runtime carries ------------
# update-agent-runtime replaces the whole configuration, so a setting the call
# leaves out is reset.

_OPTIONAL_SETTINGS = {
    "description": "Answers questions about one source system",
    "requestHeaderConfiguration": {"requestHeaderAllowlist": ["X-Request-Id"]},
    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 1800, "maxLifetime": 14400},
    "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/session"}}],
    "capacityProviderConfiguration": {"capacityProviderArn": (
        "arn:aws:bedrock-agentcore:us-east-1:111122223333:capacity-provider/cp_one-abcdefghij")},
    "metadataConfiguration": {"requireMMDSV2": True},
}


def _carried_settings(update_call: str) -> dict:
    m = re.search(r" --cli-input-json (.*) --query agentRuntimeVersion", update_call)
    assert m, f"update call carries no --cli-input-json: {update_call}"
    return json.loads(m.group(1))


def test_2f_update_keeps_the_runtimes_optional_settings(tmp_path):
    rt_json = json.dumps({**json.loads(_RT_JSON), **_OPTIONAL_SETTINGS,
                          "status": "READY", "createdAt": "2026-10-01T00:00:00Z",
                          "workloadIdentityDetails": {"workloadIdentityArn": "arn:w"}})
    r = _run_2f_full(tmp_path, "adt_mes", rt_json=rt_json)
    assert r.returncode == 0, r.stdout + r.stderr
    (update,) = _calls(tmp_path, "update-agent-runtime", "adt_mes-AAA")
    assert _carried_settings(update) == _OPTIONAL_SETTINGS  # no read-only field


def test_2f_update_without_optional_settings_carries_none(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes")
    assert r.returncode == 0, r.stdout + r.stderr
    (update,) = _calls(tmp_path, "update-agent-runtime", "adt_mes-AAA")
    assert _carried_settings(update) == {}


# --- READY counts only at the version the update returned ------------

def test_2f_stale_ready_at_an_older_version_keeps_waiting(tmp_path):
    # Update call 1 returns version 2; the first read still shows version 1.
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", statuses={
        "adt_erp-BBB": ["READY\t1\tNone", "UPDATING\t2\tNone", "READY\t2\tNone"]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_calls(tmp_path, "get-agent-runtime", "adt_erp-BBB", polls=True)) == 3
    assert "adt_erp: update accepted (version 2)" in r.stdout
    assert "2 refreshed (READY), 0 failed" in r.stdout


def test_2f_ready_that_never_reaches_the_new_version_fails(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", statuses={"adt_erp-BBB": ["READY\t1\tNone"]},
                     timeout=3, interval=1)
    assert r.returncode == 1
    tail = _failure_tail(r)
    assert ("adt_erp (not READY after 3s, last status READY at version 1, "
            "waiting for version 2)") in tail
    assert "adt_mes" not in tail


def test_2f_later_version_counts_as_ready(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", statuses={"adt_erp-BBB": ["READY\t7\tNone"]})
    assert r.returncode == 0, r.stdout + r.stderr
    reads = _calls(tmp_path, "get-agent-runtime", "adt_erp-BBB", polls=True)
    # One status poll, then one image read because version 7 is later than 2.
    assert len(reads) == 2 and "containerUri" in reads[1] and "containerUri" not in reads[0]


def test_2f_later_version_on_another_image_fails_the_deploy(tmp_path):
    """An in-app bump that read the runtime before this
    deploy's update re-sends the OLD image as a later version; that is not a
    refresh."""
    (tmp_path / "image_adt_erp-BBB").write_text("img:0\n")
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", statuses={"adt_erp-BBB": ["READY\t7\tNone"]})
    assert r.returncode == 1
    tail = _failure_tail(r)
    assert "adt_erp (a later update (version 7) replaced this deploy's image)" in tail
    assert "adt_mes" not in tail


def test_2f_exact_version_does_not_read_the_image(tmp_path):
    (tmp_path / "image_adt_erp-BBB").write_text("img:0\n")  # never consulted
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", statuses={"adt_erp-BBB": ["READY\t2\tNone"]})
    assert r.returncode == 0, r.stdout + r.stderr


def test_2f_image_read_error_is_reported_as_a_read_error(tmp_path):
    """A failed image read is not an image replacement."""
    (tmp_path / "image_err_adt_erp-BBB").write_text(
        "An error occurred (ThrottlingException) when calling the GetAgentRuntime operation\n")
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", statuses={"adt_erp-BBB": ["READY\t7\tNone"]})
    assert r.returncode == 1
    tail = _failure_tail(r)
    assert "adt_erp (could not read its image after a later update (version 7))" in tail
    assert "replaced this deploy's image" not in tail
    assert "ThrottlingException" in r.stdout


@pytest.mark.parametrize("cdpath", ["", ".:/nonexistent"])
def test_relative_tmpdir_still_keeps_an_absolute_run_dir(tmp_path, cdpath):
    """With a relative TMPDIR the run dir broke after a cd; an exported
    CDPATH must not leak cd's output into the path either."""
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    script = (_function("_deploy_tmp_on_exit") + _function("setup_deploy_tmp")
              + "setup_deploy_tmp\n"
              + 'case "$DEPLOY_TMP" in /*) echo ABSOLUTE ;; *) echo RELATIVE ;; esac\n'
              + '[ -d "$DEPLOY_TMP" ] && echo ISDIR\n'
              + 'cd sub && echo log > "$DEPLOY_TMP/cdk_deploy.log" && echo WROTE\n')
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": "."}
    if cdpath:
        env["CDPATH"] = cdpath
    r = subprocess.run(["bash", str(_script_file(tmp_path, "set -euo pipefail\n" + script))],
                       cwd=work, timeout=30, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ABSOLUTE" in r.stdout and "ISDIR" in r.stdout and "WROTE" in r.stdout


@pytest.mark.parametrize("returned", ["None", ""])
def test_2f_update_without_a_version_falls_back_to_the_first_ready(tmp_path, returned):
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", erp_update=f'echo "{returned}"',
                     statuses={"adt_erp-BBB": ["READY\t1\tNone"]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "adt_erp: update accepted (no version returned" in r.stdout
    assert len(_calls(tmp_path, "get-agent-runtime", "adt_erp-BBB", polls=True)) == 1


# --- the wait's wall time stays bounded --------------------------------

def test_2f_default_refresh_limit_matches_the_create_wait():
    """An in-place update refreshes every per-system runtime inside one shared
    limit, and an update of six runtimes can take longer than 600 s. The
    default matches the [2c]/[2e] command-line create wait (900 s)."""
    from scripts import provision_runtimes as pr
    src = (_ADT_ROOT / "deploy.sh").read_text()
    want = int(pr.CLI_CREATE_POLL_DEADLINE_SECONDS)
    assert (f'rt_seconds_setting ADT_RUNTIME_READY_TIMEOUT_SECONDS '
            f'"${{ADT_RUNTIME_READY_TIMEOUT_SECONDS:-{want}}}" {want}') in src
    # The engine bootstrap custom resource runs the same refresh on a workshop
    # stack update through the Python driver, with the same default.
    assert pr.SYSTEM_REFRESH_DEADLINE_SECONDS == want
    assert pr.refresh_system_runtimes.__kwdefaults__["deadline_seconds"] == want


@pytest.mark.parametrize("var,value,default", [
    ("timeout", "ten", 900),
    ("timeout", "0", 900),
    ("timeout", "86401", 900),
    ("timeout", "99999999999999999999", 900),
    ("interval", "0", 10),
    ("interval", "-5", 10),
])
def test_2f_bad_wait_setting_warns_and_uses_the_default(tmp_path, var, value, default):
    name = {"timeout": "ADT_RUNTIME_READY_TIMEOUT_SECONDS",
            "interval": "ADT_RUNTIME_READY_POLL_SECONDS"}[var]
    r = _run_2f_full(tmp_path, "adt_mes", **{var: f"'{value}'"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (f"{name}='{value}' is not a whole number of seconds from 1 to 86400; "
            f"using {default}.") in r.stdout
    assert "1 refreshed (READY), 0 failed" in r.stdout


def test_2f_deadline_is_checked_before_each_runtime_poll(tmp_path):
    import time
    t0 = time.monotonic()
    # adt_mes's poll takes 4 s, past the 3 s limit: adt_erp is not polled at all.
    # 3 s, not 1: bash SECONDS is whole seconds and the limit now starts before
    # the updates, so a 1 s limit can run out before adt_mes's first poll.
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", timeout=3, interval=1,
                     statuses={"adt_mes-AAA": ["UPDATING\t2\tNone"]}, slow_poll={"adt_mes-AAA": 4})
    assert time.monotonic() - t0 < 20
    assert r.returncode == 1
    assert _calls(tmp_path, "get-agent-runtime", "adt_erp-BBB", polls=True) == []
    tail = _failure_tail(r)
    assert "adt_mes (not READY after 3s, last status UPDATING)" in tail
    assert "adt_erp (not READY after 3s, not polled since its update was sent)" in tail


def test_2f_limit_covers_the_whole_refresh_not_only_the_ready_wait(tmp_path):
    """ADT_RUNTIME_READY_TIMEOUT_SECONDS bounds the whole refresh, counted from
    the first update: once it has run out, no further runtime is prepared or
    updated, and each one left is named. adt_erp's update takes 4 s against a
    2 s limit, so adt_mes (after it) is never sent. (2 s, not 1: bash SECONDS
    is whole seconds, so a 1 s limit could run out before adt_erp is sent.)"""
    t0 = time.monotonic()
    r = _run_2f_full(tmp_path, "adt_erp\tadt_mes", erp_update="sleep 4; " + _ACCEPT,
                     timeout=2, interval=1)
    took = time.monotonic() - t0
    assert r.returncode == 1, r.stdout + r.stderr
    assert _updates(tmp_path, "adt_erp-BBB") == 1
    assert _updates(tmp_path, "adt_mes-AAA") == 0
    assert _calls(tmp_path, "get-agent-runtime", "adt_mes-AAA") == []
    tail = _failure_tail(r)
    assert "adt_mes (not sent: the 2s refresh limit ran out before its update)" in tail
    assert "adt_erp (not READY after 2s, not polled since its update was sent)" in tail
    # The limit plus the one runtime's calls that were already under way.
    assert took < 12, f"2f took {took:.1f}s"


def test_2f_retry_sent_in_the_last_round_does_not_quote_its_old_status(tmp_path):
    # The retry's update call takes 4 s, so the 3 s limit passes before it is
    # polled. (3 s: the limit starts before the updates and bash SECONDS is
    # whole seconds, so adt_erp's first poll needs the margin.)
    erp_update = ('if [ "$n" -eq 1 ]; then ' + _CONFLICT + '; fi; sleep 4; ' + _ACCEPT)
    r = _run_2f_full(tmp_path, "adt_mes\tadt_erp", erp_update=erp_update, timeout=3, interval=1,
                     statuses={"adt_erp-BBB": ["READY\t2\tNone"]})
    assert r.returncode == 1
    assert _updates(tmp_path, "adt_erp-BBB") == 2
    tail = _failure_tail(r)
    assert "adt_erp (not READY after 3s, not polled since its update was sent)" in tail
    assert "last status READY" not in tail


def test_2f_unresolvable_runtime_is_named_and_fails_the_deploy(tmp_path):
    r = _run_2f_full(tmp_path, "adt_gone\tadt_mes")
    assert r.returncode == 1
    assert "adt_mes: update accepted" in r.stdout
    assert "[2f] per-system runtime refresh failed for: adt_gone (could not resolve ARN)" in r.stderr


def test_2f_missing_agent_image_fails_the_deploy(tmp_path):
    r = _run_2f_full(tmp_path, "adt_mes", image="None")
    assert r.returncode == 1
    assert "REACHED_AFTER_2F" in r.stdout
    assert "update-agent-runtime" not in (tmp_path / "aws_calls.log").read_text()
    assert "[2f] per-system runtime refresh (agent image URI missing)" in r.stderr


# --- N2: [3b] reconcile --------------------------------------------------------

def _run_3b(tmp_path: Path, reconcile_rc: int, gw_output: str = "gw-123") -> subprocess.CompletedProcess:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "reconcile_query_gateway.py").write_text(
        "import sys\n"
        f"if {reconcile_rc}:\n"
        "    print('reconcile FAILED: target mes points at a deleted runtime', file=sys.stderr)\n"
        f"sys.exit({reconcile_rc})\n"
    )
    bindir = _fake_aws(tmp_path, (
        'case "$1 $2" in\n'
        '  "sts get-caller-identity") echo \'{"Account":"111122223333"}\' ;;\n'
        f'  "cloudformation describe-stacks") echo "{gw_output}" ;;\n'
        'esac\n'
    ))
    script = (_postcdk_helpers() + f'REPO_ROOT="{repo}"\nGW_ID=""\n'
              + _step3b_block() + "\necho REACHED_AFTER_3B\n" + _final_status_block())
    return _run(tmp_path, bindir, script)


def test_3b_clean_reconcile_is_success(tmp_path):
    r = _run_3b(tmp_path, 0)
    assert r.returncode == 0, r.stderr
    assert "Query gateway targets reconciled" in r.stdout


def test_3b_failed_reconcile_fails_the_deploy_after_step4_runs(tmp_path):
    r = _run_3b(tmp_path, 1)
    assert r.returncode == 1
    assert "REACHED_AFTER_3B" in r.stdout
    assert "points at a deleted runtime" in r.stderr
    assert "[3b] query-target reconcile (reconcile_query_gateway.py exited 1)" in r.stderr


def test_3b_unreadable_gateway_id_fails_the_deploy(tmp_path):
    r = _run_3b(tmp_path, 0, gw_output="")
    assert r.returncode == 1
    assert "REACHED_AFTER_3B" in r.stdout
    assert "[3b] query-target reconcile (query gateway id unreadable)" in r.stderr
