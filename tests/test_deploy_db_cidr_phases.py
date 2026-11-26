# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deploy.sh removes the databases' VPC-wide PostgreSQL rule in two phases,
per database stack.

A database stack from before the client rules (stacks/_db_access.py) still
admits the whole VPC, and most clients' rules live in stacks that update
after it. Before anything is changed, deploy.sh reads each database stack in
the deploy and classifies it on its own:

* ``old`` (a settled stack with no ``DatabaseCidrIngressState`` output: from
  before the switch) or ``keep`` (an earlier phase 2 did not finish): it
  already admits the whole VPC, so phase 1 deploys it with ``keep`` and phase 2
  moves it, alone with the other kept stacks, to ``remove``;
* ``remove``, or absent (including a failed create CDK will replace): phase 1
  deploys it with ``remove``; it never gets the rule, not even when phase 1
  stops partway;
* anything else (the read fails other than "does not exist", the stack is
  mid-operation or in a failed state, the output has another value): the
  deploy stops before any change. There is no override: each of those also
  stops ``cdk deploy`` or needs the operator first.

When no stack is kept, phase 1 is the whole deploy. A failed phase 1 stops
before phase 2; a failed phase 2 says which stacks still admit the VPC; a
database that does not report ``remove`` afterwards fails the deploy at its
end.

Offline: runs deploy.sh's flag defaults, argument loop, data-mode block and
the two marked database blocks in bash with a fake ``aws`` (answers each
stack's status and output from a state file) and a fake ``npx`` (records each
``cdk deploy`` and applies its ``--parameters`` to the state file the way
CloudFormation would: all of them on success, or only the stacks named in
``FAKE_NPX_PARTIAL`` before failing, as a ``cdk deploy --all`` that updates
some stacks and then stops on a later one).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 - fixed internal argv lists only
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "agentic_digital_thread" / "deploy.sh"
STATE_BLOCK = ("# >>> database CIDR state", "# <<< database CIDR state")
PHASE_BLOCK = ("# >>> database CIDR phases", "# <<< database CIDR phases")
COMMAND_BLOCK = ("# >>> cdk deploy command", "# <<< cdk deploy command")
SEED, ERP = "ADTSeedDataStack", "ADTERPDatabaseStack"

#: State-file words -> what describe-stacks answers: "status TAB output value"
#: for a query that asks for the status, the output value alone otherwise.
_FAKE_AWS = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_AWS_LOG"
answer() { case "$query" in *StackStatus*) printf '%s\t%s\n' "$1" "$2" ;; *) printf '%s\n' "$2" ;; esac; }
if [ "$1 $2" = "cloudformation describe-stacks" ]; then
  stack="$4" query="$*"
  state=$(grep "^$stack=" "$FAKE_STATE" | head -n 1 | cut -d= -f2)
  case "${state:-absent}" in
    absent) echo "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id $stack does not exist" >&2; exit 254 ;;
    denied) echo "An error occurred (AccessDenied) when calling the DescribeStacks operation: not authorized" >&2; exit 254 ;;
    old) answer UPDATE_COMPLETE None ;;
    failedcreate) answer ROLLBACK_COMPLETE None ;;
    review) answer REVIEW_IN_PROGRESS None ;;
    updating) answer UPDATE_IN_PROGRESS remove ;;
    stuck) answer UPDATE_ROLLBACK_FAILED keep ;;
    odd) answer UPDATE_COMPLETE maybe ;;
    rolledback-keep) answer UPDATE_ROLLBACK_COMPLETE keep ;;
    *) answer UPDATE_COMPLETE "$state" ;;
  esac
fi
exit 0
"""

_FAKE_NPX = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_NPX_LOG"
calls=$(wc -l < "$FAKE_NPX_LOG")
rc=$(head -n 1 "$FAKE_NPX_RCS"); tail -n +2 "$FAKE_NPX_RCS" > "$FAKE_NPX_RCS.next"; mv "$FAKE_NPX_RCS.next" "$FAKE_NPX_RCS"
rc=${rc:-0}
apply_to=all
if [ -n "${FAKE_NPX_PARTIAL:-}" ] && [ "$calls" -eq 1 ]; then apply_to=" $FAKE_NPX_PARTIAL "; rc=1; fi
if [ "$rc" -eq 0 ] || [ "$apply_to" != all ]; then
  if [ "${FAKE_NPX_APPLY:-1}" -eq 1 ]; then
    for arg in "$@"; do
      case "$arg" in
        *:DatabaseCidrIngress=*)
          stack=${arg%%:*}; value=${arg##*=}
          case "$apply_to" in all|*" $stack "*) ;; *) continue ;; esac
          grep -v "^$stack=" "$FAKE_STATE" > "$FAKE_STATE.next" || true
          echo "$stack=$value" >> "$FAKE_STATE.next"; mv "$FAKE_STATE.next" "$FAKE_STATE" ;;
      esac
    done
  fi
fi
exit "$rc"
"""


def _block(text: str, markers: tuple[str, str]) -> str:
    return text[text.index(markers[0]):text.index(markers[1])]


def _script() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    defaults = text[text.index("# Parse flags\n"):text.index("while [[ $# -gt 0 ]]; do")]
    loop_start = text.index("while [[ $# -gt 0 ]]; do")
    loop = text[loop_start:text.index("\ndone\n", loop_start) + len("\ndone\n")]
    # The deploy command line the phases run is its own block (the ENI drain
    # update before a mock's teardown runs it too).
    command = _block(text, COMMAND_BLOCK) if COMMAND_BLOCK[0] in text else ""
    phases = command + _block(text, PHASE_BLOCK)
    flags = sorted(set(re.findall(r"\$(_[A-Z0-9_]+_CDK_FLAGS?)\b", phases)) - {"_DATA_CDK_FLAGS"})
    preamble = ("".join(f'{name}=""\n' for name in flags)
                + 'REGION="us-west-2"\n_AWS_PROFILE_FLAG=""\n_CDK_OUT_FLAG=""\n'
                + 'APP_VERSION="abc1234 · 2026-10-23"\nDEPLOY_TMP="$PWD"\nPOSTCDK_FAILED=""\n'
                + 'record_postcdk_failure() { POSTCDK_FAILED="${POSTCDK_FAILED}$1;"; }\n')
    return ("set -euo pipefail\n" + defaults + loop + 'STORY_ANCHOR_EPOCH=""\n'
            + _block(text, ("# >>> data-mode context", "# <<< data-mode context")) + preamble
            + _block(text, STATE_BLOCK) + 'echo "STATE_READ" >&2\n'
            + phases + 'echo "BLOCK_DONE POSTCDK=$POSTCDK_FAILED" >&2\n')


def _run(tmp_path: Path, states: dict[str, str], *args: str, rcs=(), apply=True, partial=""):
    """``(exit code, cdk deploy calls, aws calls, output, final states)``."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not found")
    bindir, workdir = tmp_path / "bin", tmp_path / "agentic_digital_thread"
    bindir.mkdir()
    workdir.mkdir()
    for name, body in (("aws", _FAKE_AWS), ("npx", _FAKE_NPX)):
        (bindir / name).write_text(body, encoding="utf-8")
        (bindir / name).chmod(0o755)
    files = {name: tmp_path / name for name in ("aws.log", "npx.log", "state", "rcs")}
    files["aws.log"].write_text("", encoding="utf-8")
    files["npx.log"].write_text("", encoding="utf-8")
    files["state"].write_text("".join(f"{k}={v}\n" for k, v in states.items()), encoding="utf-8")
    files["rcs"].write_text("".join(f"{rc}\n" for rc in rcs), encoding="utf-8")
    env = {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
           "FAKE_AWS_LOG": str(files["aws.log"]), "FAKE_NPX_LOG": str(files["npx.log"]),
           "FAKE_STATE": str(files["state"]), "FAKE_NPX_RCS": str(files["rcs"]),
           "FAKE_NPX_APPLY": "1" if apply else "0", "FAKE_NPX_PARTIAL": partial}
    result = subprocess.run([bash, "-c", _script(), "deploy.sh", *args],  # nosec B603 - fixed script from the repo
                            capture_output=True, text=True, timeout=60, env=env, cwd=workdir)

    def lines(path):
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    final = dict(line.split("=", 1) for line in lines(files["state"]))
    return (result.returncode, lines(files["npx.log"]), lines(files["aws.log"]),
            result.stdout + result.stderr, final)


def _params(call: str) -> dict[str, str]:
    return dict(re.findall(r"--parameters (\w+):DatabaseCidrIngress=(\w+)", call))


def test_a_deploy_without_a_database_passes_no_switch(tmp_path):
    rc, calls, aws, out, _ = _run(tmp_path, {})
    assert rc == 0, out
    (call,) = calls
    assert call.startswith("cdk deploy --all ") and "DatabaseCidrIngress" not in call
    assert aws == []


@pytest.mark.parametrize("states", [
    {},
    {SEED: "remove", ERP: "remove"},
    {SEED: "absent", ERP: "remove"},
    {SEED: "failedcreate", ERP: "review"},   # nothing deployed yet: cdk deploy creates both
])
def test_a_fresh_or_finished_deploy_is_one_deploy_with_remove(tmp_path, states):
    rc, calls, _aws, out, final = _run(tmp_path, states, "--simulator")
    assert rc == 0, out
    (call,) = calls
    assert call.startswith("cdk deploy --all ")
    assert _params(call) == {SEED: "remove", ERP: "remove"}
    assert final == {SEED: "remove", ERP: "remove"} and "POSTCDK=\n" in out + "\n"


#: (states before, phase-1 value per stack, the stacks phase 2 deploys).
_TWO_PHASE_CASES = [
    ({SEED: "old", ERP: "old"}, {SEED: "keep", ERP: "keep"}, [SEED, ERP]),
    ({SEED: "remove", ERP: "keep"}, {SEED: "remove", ERP: "keep"}, [ERP]),
    ({SEED: "old", ERP: "absent"}, {SEED: "keep", ERP: "remove"}, [SEED]),
    ({SEED: "rolledback-keep", ERP: "remove"}, {SEED: "keep", ERP: "remove"}, [SEED]),
]


@pytest.mark.parametrize("states, phase1, phase2", _TWO_PHASE_CASES)
def test_only_a_stack_that_admits_the_vpc_keeps_the_rule(tmp_path, states, phase1, phase2):
    """Phase 1 gives each database its own value: keep only for a stack that
    already admits the whole VPC, remove for one at remove and for a new one.
    Phase 2 deploys only the kept stacks."""
    rc, calls, _aws, out, final = _run(tmp_path, states, "--simulator")
    assert rc == 0, out
    first, second = calls
    assert first.startswith("cdk deploy --all ") and _params(first) == phase1
    assert second.startswith("cdk deploy " + " ".join(phase2) + " --exclusively ")
    assert _params(second) == {stack: "remove" for stack in phase2}
    # Both phases synthesize the same app: only the selector and the switch differ.
    strip = re.compile(r"^cdk deploy (--all|(\S+ )+--exclusively)|--parameters \S+")
    assert " ".join(strip.sub("", first).split()) == " ".join(strip.sub("", second).split())
    assert "-c with_simulator=true" in second
    assert final == {SEED: "remove", ERP: "remove"} and "POSTCDK=\n" in out + "\n"


@pytest.mark.parametrize("states, partial", [
    ({SEED: "remove", ERP: "old"}, SEED),
    ({SEED: "remove", ERP: "keep"}, f"{SEED} {ERP}"),
    ({SEED: "old", ERP: "absent"}, ERP),
    ({SEED: "absent", ERP: "absent"}, SEED),
    ({SEED: "remove", ERP: "absent"}, ERP),
])
def test_a_phase_one_that_stops_partway_opens_no_database(tmp_path, states, partial):
    """``cdk deploy --all`` can update some stacks and then fail on a later
    one. After that, no database that was at remove, and none created in
    this deploy, admits the whole VPC; a kept stack is no wider than before."""
    rc, calls, _aws, out, final = _run(tmp_path, states, "--simulator", partial=partial)
    assert rc == 1 and len(calls) == 1, out
    assert "phase 2" not in calls[0]
    for stack, before in states.items():
        after = final.get(stack, "absent")
        if before in ("remove", "absent"):
            assert after in ("remove", "absent"), (stack, before, after)
        else:
            assert after in ("old", "keep"), (stack, before, after)


@pytest.mark.parametrize("states, why", [
    ({SEED: "denied", ERP: "remove"}, "AccessDenied"),
    ({SEED: "remove", ERP: "updating"}, "UPDATE_IN_PROGRESS"),
    ({SEED: "stuck", ERP: "remove"}, "UPDATE_ROLLBACK_FAILED"),
    ({SEED: "odd", ERP: "remove"}, "maybe"),
])
def test_a_state_that_cannot_be_read_stops_before_any_change(tmp_path, states, why):
    """Unreadable is never read as old or new: the deploy stops before any
    cdk call, having made no AWS call but the reads."""
    rc, calls, aws, out, final = _run(tmp_path, states, "--simulator")
    assert rc == 1
    assert calls == [] and "STATE_READ" not in out
    assert all(call.startswith("cloudformation describe-stacks") for call in aws)
    assert why in out and "Nothing has been changed" in out
    assert final == states


def test_seed_only_switches_only_the_seed_stack(tmp_path):
    rc, calls, _aws, out, _ = _run(tmp_path, {SEED: "old"}, "--seed")
    assert rc == 0, out
    first, second = calls
    assert _params(first) == {SEED: "keep"} and _params(second) == {SEED: "remove"}
    assert second.startswith(f"cdk deploy {SEED} --exclusively ")


def test_a_failed_phase_one_stops_before_phase_two(tmp_path):
    rc, calls, _aws, out, final = _run(tmp_path, {SEED: "remove", ERP: "old"}, "--simulator", rcs=(1,))
    assert rc == 1
    assert len(calls) == 1 and "phase 1" in out and "no client lost its path" in out
    assert f"Deployed with keep, each already admitting the whole VPC: {ERP}." in out
    assert final == {SEED: "remove", ERP: "old"}


def test_a_failed_phase_two_names_the_stacks_still_open(tmp_path):
    rc, calls, _aws, out, final = _run(tmp_path, {SEED: "remove", ERP: "old"}, "--simulator", rcs=(0, 1))
    assert rc == 1 and len(calls) == 2
    assert _params(calls[1]) == {ERP: "remove"}
    assert "phase 2 failed" in out and f"may still admit the whole VPC until phase 2 succeeds: {ERP}." in out
    assert "Re-run deploy.sh" in out
    assert final == {SEED: "remove", ERP: "keep"}


def test_a_database_still_reporting_keep_fails_the_deploy(tmp_path):
    rc, calls, _aws, out, _ = _run(tmp_path, {SEED: "old", ERP: "old"}, "--simulator", apply=False)
    assert rc == 0 and len(calls) == 2
    assert "phase 2 has not run" in out and SEED in out and ERP in out


def test_the_blocks_sit_where_they_must():
    """The state is read with the other preflights, before the first AWS
    write and the first cdk call; the phases run before the post-CDK steps,
    from one command line."""
    text = DEPLOY.read_text(encoding="utf-8")
    state = text.index(STATE_BLOCK[0]), text.index(STATE_BLOCK[1])
    first_write = text.index("aws ssm put-parameter")
    first_cdk = text.index("npx cdk ")
    assert state[0] < state[1] < min(first_write, first_cdk)
    phases = text.index(PHASE_BLOCK[0]), text.index(PHASE_BLOCK[1])
    first_post_cdk = text.index("HIGHBYTE_URL=$(aws cloudformation describe-stacks")
    assert phases[0] < phases[1] < first_post_cdk
    assert text.count("npx cdk deploy ") == 1   # one command line, both phases use it
