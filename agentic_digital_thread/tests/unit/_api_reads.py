# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run a system agent's real tool through its capture hook, the way the
strands tool executor does: BeforeToolCallEvent, the tool function, then
AfterToolCallEvent with the result the tool returned. The executor runs a
sync tool in a thread that copies the task's context; here it runs in the
caller's context, which shares the same per-call slot."""
from __future__ import annotations

import itertools
from types import SimpleNamespace

_TOOL_USE_IDS = itertools.count(1)


def tool_result(raw) -> dict:
    """The ToolResult strands builds from a tool function's return value."""
    if isinstance(raw, dict) and "content" in raw:
        return {"status": raw.get("status", "success"), "content": raw["content"]}
    return {"status": "success", "content": [{"text": raw}]}


def run_tool(hook, state: dict, tool, tool_input: dict, *, retry: bool = False):
    """One call of ``tool`` with ``tool_input`` through ``hook``; the raw
    return value of the tool function."""
    tool_use = {"toolUseId": f"t-{next(_TOOL_USE_IDS)}", "name": tool.tool_name,
                "input": tool_input}
    hook._on_before_tool_call(SimpleNamespace(
        selected_tool=tool, tool_use=tool_use, invocation_state=state))
    raw = tool._tool_func(**tool_input)
    hook._on_after_tool_call(SimpleNamespace(
        selected_tool=tool, tool_use=tool_use, invocation_state=state,
        result=tool_result(raw), exception=None, retry=retry,
        agent=SimpleNamespace(messages=[])))
    return raw
