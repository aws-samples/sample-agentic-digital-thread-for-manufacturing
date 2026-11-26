# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-connection-TYPE default query guidance (base-mode generic connectors).

In base mode the live catalog is EMPTY, so a real system onboarded via
``/api/onboard/connect`` (e.g. a SiteWise-Direct or Athena source) carries no
hardcoded demo ``query_guidance``. The connector layer must therefore supply a
GENERIC default per connection TYPE so the orchestrator still receives correct
usage guidance (SiteWise: list-then-describe, sequential, never fabricate asset
IDs; Athena: information_schema list-then-describe, exact column names).

These defaults must be GENERIC — no dataset/demo literals (no site codes, no
hardcoded system ids, no demo hierarchy names).

Fully offline: pure string helpers, no AWS / no network / no fixtures.
"""
from __future__ import annotations

import re

from agents.connection_types import (
    ATHENA,
    MCP_SERVER,
    POSTGRESQL,
    SITEWISE_DIRECT,
    SITEWISE_I3X,
    default_capabilities,
    default_query_guidance,
)

# Tokens that would indicate a demo/dataset leak into generic guidance text.
_FORBIDDEN = re.compile(
    r"\b(ags|barcode_id|example-workgroup|SITE1|SITE2|example_records_raw|"
    r"milwaukee|detroit|cedar\s*rapids|mke|autoparts)\b",
    re.IGNORECASE,
)


def test_sitewise_direct_guidance_prefers_seeded_exact_reads():
    g = default_query_guidance(SITEWISE_DIRECT)
    assert g, "SiteWise (Direct) must have non-empty default guidance"
    lower = g.lower()
    # With an onboarded schema seed, exact model/property IDs are authoritative:
    # list assets once per needed model, then make one logical batch read.
    assert "onboarded schema" in lower
    assert "sitewise_list_assets" in g
    assert "sitewise_batch_get_property_values" in g
    assert "at most once" in lower
    assert "concrete failed" in lower or "read actually fails" in lower
    # The old mandatory model/root/describe traversal must be gone.
    assert "never skip a step" not in lower
    assert "with no argument first" not in lower
    assert "always list first, then describe" not in lower
    assert "fabricat" in lower or "invent" in lower or "make up" in lower


def test_sitewise_i3x_guidance_is_objects_model_graph_first_delegation():
    """The i3X guidance must teach the OBJECTS data model,
    forbid manual hierarchy traversal (the knowledge graph holds every
    Asset/Property node from onboarding), and steer graph-first THEN one
    self-contained delegated sub-question — the orchestrator reads this
    guidance and can NOT call sitewise_* tools itself (Mode A exposes
    query_<id>, Mode B run_query)."""
    g = default_query_guidance(SITEWISE_I3X)
    assert g, "SiteWise (i3X) must have non-empty default guidance"
    lower = g.lower()
    # Data model is OBJECTS, not assets.
    assert "objects" in lower
    assert "not assets" in lower
    # Manual traversal is forbidden; the graph already holds the spine.
    assert "do not traverse the object hierarchy manually" in lower
    assert "knowledge graph already holds every asset and property" in lower
    assert "resolve names, hierarchy paths and property_count" in lower
    # ONE delegated sub-question, not N narrow ones.
    assert "one self-contained sub-question" in lower
    # The old mandatory-walk wording must be gone.
    assert "sequential traversal required" not in lower
    assert "root_only=true" not in g
    assert "fabricate" in lower


def test_sitewise_i3x_guidance_frames_one_call_tools_as_system_owned():
    """C1 reconciliation: the one-call telemetry tools live on the SYSTEM'S OWN
    agent — the guidance names the capability set but explicitly forbids the
    orchestrator from calling sitewise_* tools itself, and carries the window /
    chart / fuzzy-resolution delegation contract instead."""
    g = default_query_guidance(SITEWISE_I3X)
    lower = g.lower()
    assert "live on its own agent" in lower
    assert "not in your toolset" in lower
    assert "instead of calling sitewise_* tools yourself" in lower
    # Delegation contract: window always explicit, charts on request, fuzzy
    # resolution with a single re-delegate.
    assert "start_time/end_time are always required" in lower
    assert "say when a chart is wanted" in lower
    assert "fuzzy-resolves" in lower
    assert "re-delegate once with the exact name" in lower
    assert "property_count is 0" in lower
    assert "serially" in lower
    assert not _FORBIDDEN.search(g), f"demo/dataset literal leaked into guidance: {g!r}"


def test_athena_guidance_is_list_then_describe():
    g = default_query_guidance(ATHENA)
    assert g
    lower = g.lower()
    assert "information_schema" in lower or "list" in lower
    # Exact column names / no guessing.
    assert "exact" in lower or "schema" in lower


def test_athena_guidance_names_athena_tools_not_execute_sql():
    """Regression (gateway 'Unknown tool'): Athena exposes athena_* tools, so the
    guidance MUST name them and explicitly steer AWAY from execute_sql (which the
    orchestrator's global SQL naming rule would otherwise pick, yielding an
    unknown-tool error at the gateway)."""
    g = default_query_guidance(ATHENA)
    assert "athena_run_query" in g
    assert "athena_list_tables" in g
    assert "athena_describe_table" in g
    # Explicitly forbids the generic execute_sql path for Athena.
    assert "execute_sql" in g
    lower = g.lower()
    assert "not" in lower and "execute_sql" in g  # phrased as a prohibition
    # Uses the generic gateway prefix placeholder, not a concrete system id.
    assert "<tool_prefix>___athena_run_query" in g


def test_athena_guidance_bounds_scan_cost_and_forbids_select_star():
    """FIX 2: the generic Athena guidance must steer the agent to bound scan cost —
    describe/schema before querying, a LIMIT, partition WHERE filters, and NO
    SELECT * across a large federated table — to stay under the workgroup
    bytes-scanned limit. Wording stays GENERIC (no dataset column names)."""
    g = default_query_guidance(ATHENA)
    lower = g.lower()
    assert "limit" in lower
    assert "partition" in lower
    assert "select *" in lower  # explicitly called out (as a prohibition)
    assert "bytes-scanned" in lower or "scan" in lower
    # describe-before-query anti-guessing steer.
    assert "describe" in lower
    assert "column_not_found" in lower or "guess" in lower
    # Still generic — no dataset/demo literals leaked in.
    assert not _FORBIDDEN.search(g), f"demo/dataset literal leaked into guidance: {g!r}"


def test_athena_guidance_prefers_single_level_and_projects_referenced_columns():
    """FIX 2 (mis-scoped columns): the generic Athena guidance must steer the
    agent toward simple single-level queries and, when a subquery is used, to
    PROJECT every column it references in the outer scope — referencing a column
    in an outer scope that the subquery did not select is a common
    COLUMN_NOT_FOUND cause. Wording stays GENERIC (no dataset column names)."""
    g = default_query_guidance(ATHENA)
    lower = g.lower()
    assert "single-level" in lower or "single level" in lower
    assert "subquer" in lower  # matches subquery/subqueries
    assert "project" in lower  # PROJECT/project every referenced column
    assert "outer" in lower    # names the outer-scope reference pitfall
    assert not _FORBIDDEN.search(g), f"demo/dataset literal leaked into guidance: {g!r}"


def test_athena_guidance_recovers_type_and_partition_principles():
    """GUIDANCE (LEARN + RECOVERY): the generic Athena guidance carries the two
    principled corrections: the one partition rule (bound the question's own
    time column; a partition column only when asked for, since it leaves rows
    out) and CAST operands to a common type when comparing/joining different
    types (avoids TYPE_MISMATCH). Wording stays GENERIC (no dataset column names)."""
    from agents.storage_bound_rule import PARTITION_RULE
    g = default_query_guidance(ATHENA)
    lower = g.lower()
    assert PARTITION_RULE in g
    assert "filter on a partition column first" not in lower
    # Cast-to-compare principle for type mismatches.
    assert "cast" in lower
    assert "type" in lower
    assert not _FORBIDDEN.search(g), f"demo/dataset literal leaked into guidance: {g!r}"


def test_athena_guidance_casts_timestamp_date_column_vs_string_literal():
    """FIX 1 (BETTER-INFO — types): the residual TYPE_MISMATCH is comparing a
    timestamp/date COLUMN to a 'YYYY-MM-DD' string LITERAL without a CAST. The
    generic guidance must (a) point the agent at the column TYPES now in the schema
    and (b) tell it to CAST the literal/column when comparing a temporal column to a
    string literal (e.g. CAST(col AS date) or date_parse). Wording stays GENERIC —
    no dataset column names / no literal date values."""
    g = default_query_guidance(ATHENA)
    lower = g.lower()
    # Names the temporal column case explicitly.
    assert "timestamp" in lower
    assert "date" in lower
    # Names the string-literal-comparison pitfall (not just column-vs-column).
    assert "literal" in lower
    # Offers a concrete-but-generic cast recipe.
    assert "cast(" in lower or "date_parse" in lower
    # Steers the agent to USE the column types the schema now reports.
    assert "type" in lower and ("schema" in lower or "get_system_schema" in g)
    assert not _FORBIDDEN.search(g), f"demo/dataset literal leaked into guidance: {g!r}"


def test_athena_guidance_calls_out_columnar_wide_table_and_recovery():
    """SCAN DISCIPLINE (live scorecard: BytesScannedLimit on a very wide table +
    self-recovered COLUMN_NOT_FOUND): the generic Athena guidance must make the
    scan-cost rule emphatic — name that Athena is COLUMNAR (bytes scanned scale
    with the columns SELECTed), that SELECT * on a WIDE table (many columns) alone
    can blow the bytes-scanned limit, and that on a COLUMN_NOT_FOUND error the
    agent must re-check the REAL column names (via schema/describe) rather than
    guess again. Wording stays GENERIC — no dataset column names."""
    g = default_query_guidance(ATHENA)
    lower = g.lower()
    # Columnar + wide-table scan-cost framing.
    assert "columnar" in lower
    assert "wide" in lower
    # Select-only-what-you-need, phrased as a prohibition on SELECT *.
    assert "select *" in lower
    assert "select only" in lower or "only the specific columns" in lower
    # Explicit recovery: on COLUMN_NOT_FOUND, re-check real column names, don't guess.
    assert lower.count("column_not_found") >= 2  # steer + recovery
    assert "real column" in lower or "re-run" in lower
    assert not _FORBIDDEN.search(g), f"demo/dataset literal leaked into guidance: {g!r}"


def test_postgresql_guidance_present_and_generic():
    g = default_query_guidance(POSTGRESQL)
    assert g
    assert "get_system_schema" in g or "schema" in g.lower()


def test_all_default_guidance_is_generic_no_dataset_literals():
    for ct in (POSTGRESQL, SITEWISE_DIRECT, SITEWISE_I3X, MCP_SERVER, ATHENA):
        g = default_query_guidance(ct)
        caps = default_capabilities(ct)
        assert not _FORBIDDEN.search(g), f"{ct} guidance leaks a demo literal: {g!r}"
        assert not _FORBIDDEN.search(caps), f"{ct} capabilities leaks a demo literal: {caps!r}"


def test_unknown_connection_type_returns_empty():
    assert default_query_guidance("SomethingElse") == ""
    assert default_capabilities("SomethingElse") == ""


def test_capabilities_present_for_known_types():
    for ct in (SITEWISE_DIRECT, ATHENA):
        assert default_capabilities(ct), f"{ct} should have default capabilities"
