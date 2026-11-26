# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Auto-instrumented startup smoke for the AgentCore runtime image.

The image runs ``opentelemetry-instrument python -m agents.runtime_app`` with
``AGENT_OBSERVABILITY_ENABLED=true``. Unit tests never start that path: the
unit environment has no ADOT. These tests start it in the runtime-lane
environment (the runtime image's hash-pinned lock), so an ADOT/OpenTelemetry
upgrade that breaks the configurator, an auto-instrumentor or the X-Ray OTLP
exporter fails CI instead of the first AgentCore boot.

No AWS calls can happen: credentials are static fakes, instance metadata is
disabled, and all HTTP(S) goes to a closed local proxy port, so any export
attempt fails locally.
"""

import json
import os
import re
import subprocess  # nosec B404 - fixed local interpreter commands
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2]
LOCK = APP / "requirements/runtime-py312-linux-arm64.lock"
_BIN = Path(sys.executable).parent
_INSTRUMENT = str(_BIN / "opentelemetry-instrument")

# What a broken auto-instrumented startup prints. Export failures through the
# closed proxy are expected and are not listed.
_STARTUP_FAILURES = (
    "Failed to auto initialize",
    "Configuration of configurator failed",
    "Instrumenting of",
    "DependencyConflictError",
    "Attempting to instrument while already instrumented",
    "Improper configuration",
    "Failed to create AWS OTLP exporter",
    "Sigv4 Auth requires botocore",
    "ModuleNotFoundError",
    "ImportError",
    "detected recursive logging",
)


def _env(**extra) -> dict:
    """AgentCore-shaped observability env, nothing inherited that could reach AWS."""
    env = {
        "PATH": f"{_BIN}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": os.environ.get("HOME", "/tmp"),  # nosec B108 - HOME fallback only
        "PYTHONPATH": str(APP),
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIDBOOTSMOKE",
        "AWS_SECRET_ACCESS_KEY": "boot-smoke-not-a-secret",
        "AWS_SESSION_TOKEN": "boot-smoke-token",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
        "OTEL_EXPORTER_OTLP_TIMEOUT": "2",
        # The ADOT selection AgentCore's observability setup documents. The
        # image also carries the upstream opentelemetry-distro (an ADOT
        # dependency); without these two, opentelemetry-instrument may load
        # that one instead and export to localhost:4317.
        "OTEL_PYTHON_DISTRO": "aws_distro",
        "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_RESOURCE_ATTRIBUTES": (
            "service.name=adt_orchestrator.DEFAULT,"
            "aws.log.group.names=/aws/bedrock-agentcore/runtimes/boot-smoke-DEFAULT"),
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS": (
            "x-aws-log-group=/aws/bedrock-agentcore/runtimes/boot-smoke-DEFAULT,"
            "x-aws-log-stream=otel-rt-logs"),
    }
    env.update(extra)
    return env


def _run(args, **extra):
    return subprocess.run(  # nosec B603 - fixed local interpreter command
        [_INSTRUMENT, sys.executable, *args], cwd=APP, env=_env(**extra),
        capture_output=True, text=True, timeout=180, check=False)


def _startup_failures(output: str) -> list:
    return [line for line in output.splitlines()
            if not line.startswith('{"adot"')  # the probe's own report
            and any(marker in line for marker in _STARTUP_FAILURES)]


def _locked(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}==(\S+) \\$", LOCK.read_text(), re.M)
    assert match, name
    return match.group(1)


def test_runtime_lane_installs_the_runtime_lock_versions():
    """The lane must test what the image installs, not whatever pip resolved."""
    from importlib.metadata import version

    for name in ("aws-opentelemetry-distro", "opentelemetry-sdk",
                 "opentelemetry-instrumentation", "strands-agents", "botocore"):
        assert version(name) == _locked(name), name


@pytest.fixture(scope="module")
def probe():
    result = _run(["tests/runtime_image/instrumented_boot_probe.py"])
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-4000:]
    report_lines = [line for line in result.stdout.splitlines() if line.startswith('{"adot"')]
    assert len(report_lines) == 1, output[-4000:]
    return json.loads(report_lines[0]), output


def test_instrumented_startup_wires_the_xray_otlp_exporter(probe):
    report, output = probe
    assert not _startup_failures(output), _startup_failures(output)
    assert report["adot"] == _locked("aws-opentelemetry-distro")
    assert report["tracer_provider"] == "TracerProvider"
    assert set(report["aws_span_exporters"]) == {"https://xray.us-east-1.amazonaws.com/v1/traces"}
    assert {"logs", "xray"} <= set(report["auth_session_services"])


def test_auto_instrumentors_load_without_version_conflicts(probe):
    report, _ = probe
    inst = report["instrumentors"]
    assert inst["load_errors"] == []
    assert inst["version_conflicts"] == []
    # The libraries the runtime's own calls go through.
    for name in ("botocore", "httpx", "starlette", "logging"):
        assert name in inst["active"], inst["active"]


def test_image_entry_module_boots_under_instrumentation_to_its_env_check():
    """The image CMD itself. With no runtime env set, orchestrator mode fails
    closed at ``_assert_runtime_env`` (ADR-0069) before it serves or calls
    AWS; reaching that message proves the instrumented interpreter loaded ADOT
    and imported the entry module."""
    result = _run(["-m", "agents.runtime_app"])
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "orchestrator runtime env incomplete" in output, output[-4000:]
    assert "RUNTIME IMAGE" in output and "STARTING" in output, output[-4000:]
    assert not _startup_failures(output), _startup_failures(output)
