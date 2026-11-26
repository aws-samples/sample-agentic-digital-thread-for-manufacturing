# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Load the SFC freshness watchdog's Lambda modules (``lambda-sfc-watchdog/``) by path.

The Lambda imports its sibling modules by their file names, as the Lambda
runtime puts its code directory on the path, so they are registered here
under those names. A module that fails to load is removed again rather than
left half-initialised for the next test module.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

LAMBDA_DIR = Path(__file__).resolve().parents[3] / "lambda-sfc-watchdog"


def load(name: str, module_name: str | None = None) -> ModuleType:
    """``lambda-sfc-watchdog/<name>.py``, registered as ``module_name`` (default ``name``)."""
    module_name = module_name or name
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, LAMBDA_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    return sys.modules[module_name]


def handler() -> ModuleType:
    """The handler with its two sibling modules loaded first."""
    load("watchdog_decision")
    load("watchdog_clock")
    return load("handler", "sfc_watchdog_handler")
