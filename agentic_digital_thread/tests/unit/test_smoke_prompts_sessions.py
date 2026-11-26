# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure unit tests for smoke prompt session isolation and report provenance."""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.smoke_prompts import create_session_namespace  # noqa: E402


def test_default_session_namespaces_are_unique_across_invocations():
    first = create_session_namespace()
    second = create_session_namespace()

    assert first.run_id != second.run_id
    assert first.session_id(1) == f"smoke-{first.run_id}-1"
    assert second.session_id(1) == f"smoke-{second.run_id}-1"
    assert first.followup_mode is False
    assert second.followup_mode is False


def test_environment_override_creates_deterministic_followup_namespace():
    from scripts.smoke_prompts import build_parser

    parser = build_parser({
        "SMOKE_RUN_ID": "investigation-42",
        "SMOKE_SESSION_PREFIX": "regression",
    })
    args = parser.parse_args([])

    first = create_session_namespace(args.run_id, args.session_prefix)
    second = create_session_namespace(args.run_id, args.session_prefix)
    assert first == second
    assert first.session_id(3) == "regression-investigation-42-3"
    assert first.followup_mode is True


def test_machine_report_includes_run_and_session_provenance():
    from scripts.smoke_prompts import build_report

    namespace = create_session_namespace("repeatable", "smoke")
    session_id = namespace.session_id(2)
    report = build_report(
        base_url="https://example.invalid",
        namespace=namespace,
        totals={"passed": True},
        rows=[{
            "title": "Question",
            "run_id": "must-not-override-invocation",
            "session_id": session_id,
        }],
    )

    assert report["run_id"] == "repeatable"
    assert report["session_prefix"] == "smoke"
    assert report["followup_mode"] is True
    assert report["questions"][0]["run_id"] == "repeatable"
    assert report["questions"][0]["session_id"] == session_id
    assert "password" not in report


def test_cli_override_takes_precedence_for_deterministic_followup_namespace():
    from scripts.smoke_prompts import build_parser

    args = build_parser({
        "SMOKE_RUN_ID": "environment-run",
        "SMOKE_SESSION_PREFIX": "environment-prefix",
    }).parse_args([
        "--run-id", "cli-run",
        "--session-prefix", "cli-prefix",
    ])

    namespace = create_session_namespace(args.run_id, args.session_prefix)
    assert namespace.session_id(4) == "cli-prefix-cli-run-4"
    assert namespace.followup_mode is True


def test_quality_strict_is_additive_and_env_configurable():
    from scripts.smoke_prompts import build_parser

    env_args = build_parser({"SMOKE_QUALITY_STRICT": "true"}).parse_args([])
    assert env_args.quality_strict is True
    assert env_args.strict is False

    cli_args = build_parser({}).parse_args(["--quality-strict"])
    assert cli_args.quality_strict is True


def test_fresh_smoke_turns_are_memory_inert_followup_keeps_memory():
    """Facts are ACTOR-scoped; fresh runs must not feed later runs' memory
    (measured live: 'seen in prior sessions' in a fresh strict run). The
    deliberate follow-up mode keeps writes on — remembering IS what it tests."""
    from smoke_prompts import ask_body
    fresh = ask_body("q", "smoke-abc-1", followup_mode=False)
    assert fresh["write_memory"] is False
    assert fresh["user_id"] == "smoke" and fresh["session_id"] == "smoke-abc-1"
    follow = ask_body("q", "smoke-fixed-1", followup_mode=True)
    assert "write_memory" not in follow
