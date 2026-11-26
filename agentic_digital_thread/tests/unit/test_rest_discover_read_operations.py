# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A REST connector's discovery summary lists only the operations it offers.

The wizard's schema summary for a read-only REST
CMMS once listed "POST /assets: Create a new asset" and "DELETE /workorders/{wo_id}"
for every entity and called them inferred. The wizard asked the model for "key
endpoints" while giving it only entity and column names. The connector knows
its own tools: each REST vendor's runtime toolset is built by its registry
hook, and every tool declares itself a data or discovery read. The summary now
names those tools, says so when every one is a read, and asks the model for no
endpoint list.
"""
from __future__ import annotations

import asyncio

import pytest

import agents.connection_resolver as cr
import agents.onboard as onboard
import agents.onboard_agent as onboard_agent
from agents.connection_types import REST_API, TULIP
from agents.models import DiscoverRequest

URL = "https://cmms.example.test"
_WRITE_VERBS = ("POST ", "PUT ", "PATCH ", "DELETE ")


def _tool_names(tools) -> list[str]:
    return [t.tool_name for t in tools]


# --------------------------------------------------------------------------- #
# the connector's own read operations
# --------------------------------------------------------------------------- #


def test_a_tulip_connector_reports_its_table_reads():
    from agents.connector_read_operations import read_operations
    from agents.system_agent import _make_tulip_tools
    ops = read_operations(TULIP, {})
    assert ops is not None and ops.only_reads
    assert [name for name, _ in ops.reads] == _tool_names(_make_tulip_tools(""))


def test_a_connector_without_its_own_toolset_has_no_operation_list():
    """The generic REST API has no runtime toolset (provision is unsupported):
    nothing is claimed for it, and the summary keeps the spec analysis."""
    from agents.connector_read_operations import read_operations
    assert read_operations(REST_API, {}) is None
    assert read_operations("PostgreSQL", {}) is None


class _Tool:
    def __init__(self, name, description):
        self.tool_name = name
        self.tool_spec = {"name": name, "description": description}


class _MixedAdapter:
    """A vendor whose toolset holds one declared read and one other tool."""

    @staticmethod
    def runtime_tools(*_a, **_k):
        from agents.tool_result_capture import DATA_READ, declare_read
        return [declare_read(DATA_READ, _Tool("read_widgets", "Read widgets. More text.")),
                _Tool("change_widget", "Change a widget.")]


def _mixed_ops(monkeypatch):
    from agents import connector_read_operations as cro
    monkeypatch.setattr(cro, "resolve_vendor", lambda ct: _MixedAdapter())
    return cro, cro.read_operations("Widget API", {})


def test_a_tool_that_declares_no_read_is_neither_listed_nor_called_a_read(monkeypatch):
    """Read-only is claimed only when every tool declares a read kind; a tool
    without one is left off the list and the no-write sentence is dropped."""
    cro, ops = _mixed_ops(monkeypatch)
    assert ops.reads == (("read_widgets", "Read widgets."),)
    assert not ops.only_reads
    text = cro.operations_text(ops)
    assert "read_widgets" in text and "change_widget" not in text
    assert "no create, update or delete" not in text


def test_the_prompt_for_a_mixed_toolset_does_not_claim_the_reads_are_all(monkeypatch):
    """With reads available but not read-only, the prompt must not call the
    listed reads "the only ones the connector has". It says the list
    holds only the connector's declared reads and may not be all it offers."""
    _cro, ops = _mixed_ops(monkeypatch)
    prompt = onboard._rest_analysis_prompt("  Widget: code string", ops)
    assert "the only ones the connector has" not in prompt
    assert "only the reads this connector declares" in prompt
    assert "may offer other operations" in prompt
    assert "read_widgets" in prompt and "change_widget" not in prompt
    assert "Key endpoints" not in prompt


# --------------------------------------------------------------------------- #
# a known vendor whose operation list cannot be read
# --------------------------------------------------------------------------- #

class _BrokenAdapter:
    """A known vendor adapter whose toolset cannot be built."""

    @staticmethod
    def runtime_tools(*_a, **_k):
        raise RuntimeError("toolset constructor failed")


class _SilentAdapter:
    """A known vendor adapter whose tools declare no read."""

    @staticmethod
    def runtime_tools(*_a, **_k):
        class _Tool:
            tool_name = "do_something"
            tool_spec = {"name": "do_something", "description": "Something."}
        return [_Tool()]


# --------------------------------------------------------------------------- #
# the wizard's REST-family discover summary
# --------------------------------------------------------------------------- #


def test_the_wizard_summary_lists_the_tulip_read_tools(monkeypatch):
    """Tulip takes its own discover branch (one server-side source); its
    summary names the Tulip read tools too."""
    from agents.system_agent import _make_tulip_tools
    prompts: list[str] = []
    tables = [{"name": "Lots", "columns": [{"name": "lotCode", "data_type": "string"}],
               "row_count": 3}]
    monkeypatch.setattr(onboard, "_tulip_rest_discover",
                        lambda sid, url: ({"tables": tables}, url))
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda prompt, **_k: prompts.append(prompt) or "A line app.")
    body = DiscoverRequest(connection_type=TULIP, connection_string=URL)
    out = asyncio.run(onboard._discover_rest_or_i3x(body, "", URL, "", TULIP))
    (prompt,) = prompts
    assert "Key endpoints" not in prompt
    for name in _tool_names(_make_tulip_tools("")):
        assert name in prompt and name in out["ai_analysis"], name


