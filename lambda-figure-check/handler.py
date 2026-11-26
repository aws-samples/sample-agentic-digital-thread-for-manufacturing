# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Evaluations code-based evaluator ``adt_figure_check``: the Lambda
entry point.

The check is ``agentic_digital_thread/agents/eval_figure_check.py``.
``stacks/_evaluations.py`` stages it at synth beside this file, with copies of
the app modules it imports (all standard library), so the asset needs no
install step and no build image.
"""
from __future__ import annotations

from agents.eval_figure_check import handler  # noqa: F401  # the Lambda handler
