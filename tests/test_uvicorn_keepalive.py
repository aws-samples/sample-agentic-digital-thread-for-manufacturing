# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The app's HTTP keep-alive must outlive the ALB idle timeout.

If the target closes an idle connection first, the ALB can reuse it and
return 502 (AWS ALB troubleshooting: target keep-alive > ALB idle timeout).
Live 2026-09-27: uvicorn's 5 s default vs the 120 s ALB idle timeout produced
intermittent ELB 502s on dashboard assets and API calls.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _uvicorn_keepalive() -> int:
    text = (ROOT / "agentic_digital_thread" / "Dockerfile").read_text(encoding="utf-8")
    cmd = next(line for line in text.splitlines() if line.startswith("CMD "))
    args = json.loads(cmd[4:])
    assert args[0] == "uvicorn", cmd
    return int(args[args.index("--timeout-keep-alive") + 1])


def _alb_idle_timeout() -> int:
    text = (ROOT / "stacks" / "adt_compute_stack.py").read_text(encoding="utf-8")
    values = [int(v) for v in re.findall(r"idle_timeout=Duration\.seconds\((\d+)\)", text)]
    assert values, "ALB idle_timeout not found in adt_compute_stack.py"
    return max(values)


def test_uvicorn_keepalive_exceeds_alb_idle_timeout():
    assert _uvicorn_keepalive() > _alb_idle_timeout()
