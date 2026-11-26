# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``agents.connection_types.sanitize_system_id`` and its
application at the system-registration boundary (``connect_new_system``).

Why this guard exists: system-agent tools are exposed to Bedrock/Nova as
``<system_id>___<tool>``. Bedrock Converse ``toolSpec.name`` must match
``^[a-zA-Z0-9_-]{1,64}$`` and Strands does NOT auto-sanitize, so an onboarded
system_id containing a space/dot/slash/unicode char would mint an invalid
prefixed tool name and trigger a Bedrock ValidationException at query time.
``sanitize_system_id`` slugifies-with-validation at the registration boundary so
only tool-name-safe ids are ever persisted.

Fully offline. Synthetic identifiers only (no dataset literals).
"""
import re
from unittest.mock import MagicMock, patch

import pytest

from agents.connection_types import (
    MAX_SYSTEM_ID_LEN,
    sanitize_system_id,
)


@pytest.fixture(autouse=True)
def _athena_role_rule_off(monkeypatch):
    """These tests register Athena systems to pin id/tool-prefix handling, not
    the rule that a deployed app's Athena registration needs a role ARN
    (test_connect_athena_role_required.py): run them as local development,
    where a blank role is accepted."""
    import agents.onboard_agent as onboard_agent
    monkeypatch.setattr(onboard_agent, "local_mode_env", lambda *a, **k: True)

# Bedrock Converse toolSpec.name constraint.
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Longest per-system tool stem embeds the (underscored) id a SECOND time.
def _worst_case_tool_name(system_id: str) -> str:
    safe = system_id.replace("-", "_")
    return f"{system_id}___execute_sql_{safe}"


@pytest.mark.parametrize("system_id", ["ags-segments", "erp", "mes_mil", "a-b_c-1", "x"])
def test_valid_ids_pass_through_unchanged(system_id):
    assert sanitize_system_id(system_id) == system_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("My System", "my-system"),      # space
        ("a.b.c", "a-b-c"),               # dots
        ("a/b\\c", "a-b-c"),              # path separators
        ("Warehouse.v2", "warehouse-v2"), # dot + uppercase
        ("café", "caf"),                  # unicode dropped -> trailing sep trimmed
        ("ERP", "erp"),                   # uppercase -> lowercased
        ("a  -  b", "a-b"),               # collapse runs of separators
        ("a___b", "a-b"),                 # underscore run -> single sep (no '___')
        ("a__b", "a-b"),                  # 2-underscore run collapses
        ("a_-_b", "a-b"),                 # mixed separator run collapses
        ("  spaced  ", "spaced"),         # trim leading/trailing
        ("--edge--", "edge"),             # trim leading/trailing separators
    ],
)
def test_unsafe_ids_are_slugified(raw, expected):
    out = sanitize_system_id(raw)
    assert out == expected
    assert _TOOL_NAME_RE.match(out)


@pytest.mark.parametrize("raw", ["", "   ", "///", "...", "😀"])
def test_fully_invalid_ids_raise(raw):
    with pytest.raises(ValueError):
        sanitize_system_id(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "a___b",              # exact gateway separator inside the id
        "a__b",               # 2-run underscore
        "a_-_b",              # mixed separator run
        "a--__--b",           # long mixed run
        "mes___mil",          # underscore run in an otherwise-valid-looking id
        "a" + "_" * 8 + "b",  # synthetic long underscore run
    ],
)
def test_no_gateway_separator_survives(raw):
    """Defense-in-depth: the gateway builds ``<system_id>___<tool>`` and
    ``runtime_app`` splits it on ``'___'`` — so a sanitized id must NEVER contain
    the ``___`` separator. Stronger still: no run of 2+ separators may survive,
    which makes ``___`` structurally impossible regardless of hyphen/underscore
    mix. The result must remain a valid Bedrock tool-name prefix."""
    sid = sanitize_system_id(raw)
    assert "___" not in sid
    assert not re.search(r"[-_]{2,}", sid)   # no separator run of length >= 2
    assert _TOOL_NAME_RE.match(sid)
    assert _TOOL_NAME_RE.match(_worst_case_tool_name(sid))


def test_none_raises():
    with pytest.raises(ValueError):
        sanitize_system_id(None)  # type: ignore[arg-type]


def test_over_length_id_is_capped():
    out = sanitize_system_id("a" * 100)
    assert len(out) == MAX_SYSTEM_ID_LEN
    assert out == "a" * MAX_SYSTEM_ID_LEN


def test_max_len_keeps_worst_case_tool_name_within_bedrock_limit():
    # Worst case: id appears in BOTH the gateway prefix and the execute_sql stem.
    worst = _worst_case_tool_name("a" * MAX_SYSTEM_ID_LEN)
    assert len(worst) <= 64
    assert _TOOL_NAME_RE.match(worst)


@pytest.mark.parametrize(
    "raw",
    ["ags-segments", "erp", "mes_mil", "My System", "a.b/c", "café", "a" * 100],
)
def test_sanitized_id_yields_valid_worst_case_tool_name(raw):
    sid = sanitize_system_id(raw)
    assert _TOOL_NAME_RE.match(_worst_case_tool_name(sid))


# --- registration boundary applies the guard -------------------------------

def _fake_catalog(existing=None):
    added = {}
    cat = MagicMock()
    cat.get.return_value = existing
    cat.lookup.return_value = existing
    cat.add.side_effect = lambda e: added.update(e)
    return cat, added


def test_connect_new_system_sanitizes_unsafe_name():
    from agents.onboard_agent import connect_new_system

    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="Line 1 / Bldg.A", system_type="Historian", plant="Site One",
            connection_type="Athena",
            connection_config={"workgroup": "wg", "database": "db"},
        )
    assert res["status"] == "created"
    sid = res["system_id"]
    assert _TOOL_NAME_RE.match(sid)
    assert _TOOL_NAME_RE.match(_worst_case_tool_name(sid))
    assert added["id"] == sid
    # No stray unsafe characters survived into the persisted id.
    assert "." not in sid and "/" not in sid and " " not in sid


def test_connect_new_system_preserves_valid_id():
    from agents.onboard_agent import connect_new_system

    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="ags-segments", system_type="ERP", plant="",
            connection_type="Athena",
        )
    assert res["system_id"] == "ags-segments"
    assert added["id"] == "ags-segments"


# --- explicit tool_prefix is sanitized at the registration boundary --------

def test_connect_new_system_sanitizes_explicit_tool_prefix():
    """An EXPLICIT tool_prefix is also a Bedrock/Nova tool-name prefix (the
    gateway builds ``<tool_prefix>___<tool>``), so an unsafe supplied prefix must
    be slugified-with-validation at registration — never persisted raw."""
    from agents.onboard_agent import connect_new_system

    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="Warehouse Analytics", system_type="Historian", plant="Site One",
            connection_type="Athena",
            connection_config={"workgroup": "wg", "database": "db"},
            tool_prefix="My Prefix.v2",
        )
    assert res["status"] == "created"
    tp = added["tool_prefix"]
    assert tp == "my-prefix-v2"
    # The persisted prefix must be a valid Bedrock tool-name prefix, even in the
    # worst-case doubled-id stem the gateway can mint.
    assert _TOOL_NAME_RE.match(tp)
    assert _TOOL_NAME_RE.match(_worst_case_tool_name(tp))
    assert " " not in tp and "." not in tp


def test_connect_new_system_valid_explicit_tool_prefix_unchanged():
    """An already-safe explicit tool_prefix passes through unchanged."""
    from agents.onboard_agent import connect_new_system

    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        connect_new_system(
            name="Warehouse Analytics", system_type="Historian", plant="",
            connection_type="Athena", tool_prefix="whx",
        )
    assert added["tool_prefix"] == "whx"


def test_connect_new_system_gateway_separator_in_tool_prefix_collapsed():
    """A supplied tool_prefix containing the gateway separator ``___`` must not
    survive — it would corrupt ``runtime_app``'s split on ``'___'``."""
    from agents.onboard_agent import connect_new_system

    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        connect_new_system(
            name="Warehouse Analytics", system_type="Historian", plant="",
            connection_type="Athena", tool_prefix="a___b",
        )
    tp = added["tool_prefix"]
    assert "___" not in tp
    assert not re.search(r"[-_]{2,}", tp)
    assert _TOOL_NAME_RE.match(tp)


def test_connect_new_system_unreducible_tool_prefix_raises():
    """A supplied tool_prefix that cannot reduce to a valid slug raises (rather
    than persisting an invalid prefix)."""
    from agents.onboard_agent import connect_new_system

    cat, _ = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        with pytest.raises(ValueError):
            connect_new_system(
                name="Warehouse Analytics", system_type="Historian", plant="",
                connection_type="Athena", tool_prefix="///",
            )
