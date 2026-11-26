# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A table's steward-declared ROW EXCLUSION: rows the source owner says are not records.

Measured on an analytics deployment: the source owner of a table said a
temporary dual-id mode writes a second, "shadow" copy of some rows, and gave the
rule that tells them apart. Nothing applied it, so failure counts over-counted
by up to a third at two sites. A prose hint to the agent does not hold (it is
advisory query guidance and was measured not to). The app already has the
pattern for an owner-stated table fact the catalog cannot hold
(``record_key``), so the row exclusion is built the same way:

* a steward declares a table's exclusion as an ordered list of structured
  conditions (no free SQL), all of which hold for a row that is not a record,
  with a short owner label and the source text, stored on the table's
  ``SchemaEntry`` with provenance (``row_exclusion.set_row_exclusion``);
* ONE canonical SQL rendering, ``NOT (<c1> AND <c2> ...)``
  (``row_exclusion.predicate_sql``), used by the schema line, the refusal and
  the check;
* the system agent sees it as a schema FACT (``row_exclusion.schema_lines``);
* an engine check (``row_exclusion.decide``) refuses, once, a query that
  certainly reads the table without the predicate as a top-level AND conjunct
  of that read's WHERE; the identical resend runs and is disclosed; a query
  that carries it runs with a short "excluded" disclosure; a statement that may
  read the table but cannot be read with certainty is refused once the same way.

Two neutral schemas: ``job_events`` (shaped like the measured table:
``pipeline``, ``run_ref``, ``step_kind``) and ``ledger_lines``. Fully offline.
"""
from __future__ import annotations

import importlib
import json

import pytest


def _rx():
    return importlib.import_module("agents.row_exclusion")


# --------------------------------------------------------------------------- #
# A fake graph: SchemaEntry + typed Field, and the row-exclusion properties.
# --------------------------------------------------------------------------- #
class _Graph:
    """Enough of ``(System)-[:HAS_SCHEMA]->(SchemaEntry)-[:HAS_FIELD]->(Field)``
    for ``schema_fields`` and ``row_exclusion``, plus the ``se.row_exclusion*``
    provenance props the steward sets and the System node's row-exclusion
    manifest (``row_exclusion_manifest``, written by compare-and-set)."""

    def __init__(self, tables):
        # tables: {table: {column: data_type}}
        self.fields = {t: dict(cols) for t, cols in tables.items()}
        self.props: dict[str, dict] = {}
        self.writes: list = []
        self.manifest = None

    def write(self, cypher, params):
        from agents import row_exclusion_manifest
        self.writes.append((cypher, dict(params)))
        if cypher == row_exclusion_manifest.SWAP_MANIFEST:
            if (self.manifest or "") != params["expected"]:
                return []
            self.manifest = params["manifest"]
            return [{"id": params["sid"]}]
        seid = params.get("seid", "")
        if "SET se.row_exclusion" in cypher:
            if params.get("row_exclusion"):
                self.props[seid] = {"row_exclusion": params["row_exclusion"],
                                    "row_exclusion_by": params.get("by"),
                                    "row_exclusion_at": params.get("at"),
                                    "row_exclusion_source": params.get("source")}
            else:
                self.props.pop(seid, None)
        return True

    def query(self, cypher, params):
        from agents import row_exclusion_manifest
        if cypher == row_exclusion_manifest.READ_MANIFEST:
            return [{"row_exclusion_manifest": self.manifest}]
        seid = params.get("seid", "")
        table = seid.split(".", 1)[-1]
        if "f.data_type AS data_type" in cypher and "HAS_FIELD" in cypher:
            return [{"name": c, "data_type": t} for c, t in self.fields.get(table, {}).items()]
        if "f.name AS name" in cypher and "HAS_FIELD" in cypher:
            return [{"name": c} for c in self.fields.get(table, {})]
        if "count(se)" in cypher:
            return [{"n": 1 if table in self.fields else 0}]
        if "se.row_exclusion" in cypher:
            return [self.props.get(seid) or {"row_exclusion": None, "row_exclusion_by": None,
                                             "row_exclusion_at": None,
                                             "row_exclusion_source": None}]
        return []


_JOB = {"pipeline": "varchar", "run_ref": "varchar", "step_kind": "varchar(64)",
        "state": "varchar", "qty": "integer", "finished_at": "timestamp(6) with time zone",
        "detail.reason_code": "varchar"}
_LEDGER = {"batch_tag": "string", "channel": "char(8)", "entry_kind": "varchar",
           "amount": "decimal(12,2)", "posted_at": "date"}
_KEEP = ["ASSEMBLE", "ASSEMBLE_BUFFER", "PACK", "SHIP_BUFFER"]
#: The measured rule's shape: a row is not a record when pipeline is non-empty,
#: run_ref is non-empty, and step_kind is not one of the kept kinds.
_CONDITIONS = [{"column": "pipeline", "op": "not_empty"},
               {"column": "run_ref", "op": "not_empty"},
               {"column": "step_kind", "op": "not_in", "values": list(_KEEP)}]
_LABEL = "shadow copies"
_PRED = ("NOT (COALESCE(pipeline, '') <> '' AND COALESCE(run_ref, '') <> '' AND "
         "COALESCE(step_kind, '') NOT IN ('ASSEMBLE', 'ASSEMBLE_BUFFER', 'PACK', 'SHIP_BUFFER'))")


@pytest.fixture
def graph():
    g = _Graph({"job_events": _JOB, "ledger_lines": _LEDGER})
    _rx().reset()
    yield g
    _rx().reset()


def _fact(conditions=None, label=_LABEL):
    return _rx().exclusion_from(label, conditions if conditions is not None else _CONDITIONS)


def _set(graph, conditions, table="job_events", label=_LABEL, **kwargs):
    return _rx().set_row_exclusion("sys", table, conditions, label=label, actor="alice",
                                   source=kwargs.pop("source", "owner note"),
                                   write=graph.write, query=graph.query, **kwargs)


# --------------------------------------------------------------------------- #
# Validation: structured conditions only, every reject a reason, nothing written.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("conditions,reason", [
    ([{"column": "pipeline", "op": "like"}], "invalid_op"),
    ([{"column": "pipeline", "op": "= '' OR 1=1"}], "invalid_op"),
    ([{"column": "qty", "op": "not_empty"}], "column_not_string"),
    ([{"column": "finished_at", "op": "empty"}], "column_not_string"),
    ([{"column": "no_such", "op": "empty"}], "unknown_column"),
    ([{"column": "step_kind", "op": "in", "values": ["A'B"]}], "invalid_value"),
    ([{"column": "step_kind", "op": "in", "values": ['A"B']}], "invalid_value"),
    ([{"column": "step_kind", "op": "in", "values": ["A\nB"]}], "invalid_value"),
    ([{"column": "step_kind", "op": "in", "values": ["A\\B"]}], "invalid_value"),
    ([{"column": "step_kind", "op": "in", "values": [""]}], "invalid_value"),
    ([{"column": "step_kind", "op": "in", "values": ["x" * 200]}], "invalid_value"),
    ([{"column": "step_kind", "op": "in", "values": []}], "invalid_values"),
    ([{"column": "step_kind", "op": "in", "values": [f"v{i}" for i in range(33)]}],
     "invalid_values"),
    ([{"column": "step_kind", "op": "in"}], "invalid_values"),
    ([{"column": "pipeline", "op": "empty", "values": ["A"]}], "invalid_values"),
    ([{"column": "pipeline", "op": "empty"}, {"column": "pipeline", "op": "empty"}],
     "duplicate_condition"),
    ([{"column": "pipeline", "op": "not_empty"}] * 1
     + [{"column": f"c{i}", "op": "empty"} for i in range(8)], "too_many_conditions"),
    ([], "empty_conditions"),
])
def test_an_invalid_exclusion_is_refused_and_nothing_is_written(graph, conditions, reason):
    wide = _Graph({"job_events": {**_JOB, **{f"c{i}": "varchar" for i in range(8)}}})
    out = _set(wide, conditions)
    assert out["status"] == "validation_failed", out
    assert out["reason"] == reason, out
    assert not [w for w in wide.writes if "SET se.row_exclusion" in w[0]]


@pytest.mark.parametrize("label", ["", "a" * 81, "shadow\ncopies", "it's", "x; DROP"])
def test_an_unplain_label_is_refused(graph, label):
    out = _set(graph, _CONDITIONS, label=label)
    assert out["status"] == "validation_failed" and out["reason"] == "invalid_label"


def test_a_malformed_condition_list_is_refused(graph):
    for bad in ("pipeline", [["pipeline", "empty"]], [{"op": "empty"}],
                [{"column": "pipeline", "op": "empty", "extra": 1}]):
        out = _set(graph, bad)
        assert out["status"] == "validation_failed" and out["reason"] == "invalid_input"


def test_an_unknown_table_is_refused(graph):
    out = _set(graph, _CONDITIONS, table="ghost")
    assert out["status"] == "validation_failed" and out["reason"] == "unknown_table"


def test_string_typed_columns_of_every_spelling_are_accepted(graph):
    conditions = [{"column": "batch_tag", "op": "empty"},
                  {"column": "channel", "op": "in", "values": ["WEB"]},
                  {"column": "entry_kind", "op": "not_empty"}]
    assert _set(graph, conditions, table="ledger_lines")["status"] == "ok"


# --------------------------------------------------------------------------- #
# One canonical rendering.
# --------------------------------------------------------------------------- #
def test_the_canonical_predicate_is_one_not_of_the_anded_conditions():
    assert _rx().predicate_sql(_fact()) == _PRED


def test_each_op_renders_its_coalesced_form():
    rx = _rx()
    rendered = rx.predicate_sql(_fact([
        {"column": "pipeline", "op": "empty"},
        {"column": "run_ref", "op": "not_empty"},
        {"column": "step_kind", "op": "in", "values": ["A", "B"]},
        {"column": "state", "op": "not_in", "values": ["C"]}]))
    assert rendered == ("NOT (COALESCE(pipeline, '') = '' AND COALESCE(run_ref, '') <> '' AND "
                        "COALESCE(step_kind, '') IN ('A', 'B') AND "
                        "COALESCE(state, '') NOT IN ('C'))")


def test_a_dotted_or_reserved_column_is_double_quoted():
    rx = _rx()
    rendered = rx.predicate_sql(_fact([{"column": "detail.reason_code", "op": "not_empty"},
                                       {"column": "order", "op": "empty"}]))
    assert rendered == ("NOT (COALESCE(\"detail.reason_code\", '') <> '' AND "
                        "COALESCE(\"order\", '') = '')")


# --------------------------------------------------------------------------- #
# Set / read / clear, with provenance.
# --------------------------------------------------------------------------- #
def test_setting_stores_it_with_provenance_and_reads_back(graph):
    rx = _rx()
    out = _set(graph, _CONDITIONS, source="owner statement 2026-10-15")
    assert out["status"] == "ok"
    assert out["predicate"] == _PRED
    stored = rx.read_row_exclusion("sys", "job_events", query=graph.query)
    assert stored["label"] == _LABEL
    assert stored["conditions"] == _CONDITIONS
    assert stored["predicate"] == _PRED
    assert stored["row_exclusion_by"] == "alice"
    assert stored["row_exclusion_at"]
    assert stored["row_exclusion_source"] == "owner statement 2026-10-15"


def test_columns_and_ops_are_case_folded_and_the_order_kept(graph):
    rx = _rx()
    _set(graph, [{"column": "Run_Ref", "op": "NOT_EMPTY"}, {"column": "PIPELINE", "op": "empty"}])
    stored = rx.read_row_exclusion("sys", "job_events", query=graph.query)
    assert stored["conditions"] == [{"column": "run_ref", "op": "not_empty"},
                                    {"column": "pipeline", "op": "empty"}]


def test_clearing_removes_it(graph):
    rx = _rx()
    _set(graph, _CONDITIONS)
    out = rx.clear_row_exclusion("sys", "job_events", actor="bob",
                                 write=graph.write, query=graph.query)
    assert out["status"] == "ok"
    stored = rx.read_row_exclusion("sys", "job_events", query=graph.query)
    assert stored["conditions"] == [] and stored["predicate"] == ""


def test_an_unset_table_reads_empty(graph):
    stored = _rx().read_row_exclusion("sys", "ledger_lines", query=graph.query)
    assert stored["conditions"] == [] and stored["label"] == "" and stored["predicate"] == ""


def test_a_malformed_stored_value_reads_as_none(graph):
    graph.props["sys.job_events"] = {"row_exclusion": "{not json",
                                     "row_exclusion_by": "x"}
    assert _rx().read_row_exclusion("sys", "job_events", query=graph.query)["conditions"] == []


# --------------------------------------------------------------------------- #
# The schema line.
# --------------------------------------------------------------------------- #
def test_the_schema_line_states_the_fact_with_the_predicate_once():
    line = _rx().schema_lines({"job_events": _fact()})
    assert line.startswith("\n\nROW EXCLUSIONS (facts")
    assert line.count(_PRED) == 1
    assert "job_events" in line and _LABEL in line
    assert "not records" in line and "source owner" in line
    assert "\u2014" not in line and "\u2013" not in line


def test_no_schema_line_without_an_exclusion():
    assert _rx().schema_lines({}) == ""


def test_a_non_printable_table_name_is_withheld_from_the_schema_line():
    line = _rx().schema_lines({"bad name!": _fact()})
    assert "bad name!" not in line and "withheld" in line


# --------------------------------------------------------------------------- #
# The check: present (each normalized variant), missing, uncertain.
# --------------------------------------------------------------------------- #
_WIN = ("finished_at >= TIMESTAMP '2026-10-01 00:00:00 UTC' "
        "AND finished_at < TIMESTAMP '2026-10-02 00:00:00 UTC'")
_COUNT_MISSING = f"SELECT COUNT(*) AS n FROM job_events WHERE state = 'FAILED' AND {_WIN}"


def _with(pred: str, table: str = "job_events") -> str:
    return f"SELECT COUNT(*) AS n FROM {table} WHERE state = 'FAILED' AND {_WIN} AND {pred}"


@pytest.mark.parametrize("pred", [
    _PRED,
    _PRED.lower().replace("'assemble'", "'ASSEMBLE'").replace("'assemble_buffer'",
                                                              "'ASSEMBLE_BUFFER'")
    .replace("'pack'", "'PACK'").replace("'ship_buffer'", "'SHIP_BUFFER'"),
    " ".join(_PRED.replace("(", " ( ").replace(")", " ) ").split()),
    f"({_PRED})",
    f"(({_PRED}))",
    _PRED.replace("<>", "!="),
    _PRED.replace("pipeline", '"pipeline"').replace("run_ref", '"run_ref"'),
    _PRED.replace("pipeline", "job_events.pipeline"),
    ("NOT ((COALESCE(pipeline, '') <> '') AND (COALESCE(run_ref, '') <> '') AND "
     "(COALESCE(step_kind, '') NOT IN ('ASSEMBLE', 'ASSEMBLE_BUFFER', 'PACK', 'SHIP_BUFFER')))"),
    # the same conditions in another order, and the IN list reordered
    ("NOT (COALESCE(step_kind, '') NOT IN ('PACK', 'ASSEMBLE', 'SHIP_BUFFER', "
     "'ASSEMBLE_BUFFER') AND COALESCE(run_ref, '') <> '' AND COALESCE(pipeline, '') <> '')"),
])
def test_a_query_carrying_the_predicate_runs_with_the_exclusion_disclosed(pred):
    found = _rx().decide(_with(pred), "job_events", _fact())
    assert found is not None and found.applied is True and found.refuse is False


def test_the_predicate_first_or_alone_is_read():
    rx = _rx()
    for sql in (f"SELECT COUNT(*) FROM job_events WHERE {_PRED}",
                f"SELECT COUNT(*) FROM job_events WHERE {_PRED} AND state = 'FAILED'",
                f"SELECT COUNT(*) FROM job_events WHERE (state = 'FAILED' AND {_PRED})"):
        assert rx.decide(sql, "job_events", _fact()).applied is True


def test_an_alias_qualifier_that_binds_to_the_table_is_read():
    sql = (f"SELECT COUNT(*) FROM job_events j WHERE j.state = 'FAILED' AND "
           + _PRED.replace("pipeline", "j.pipeline").replace("run_ref", "j.run_ref")
           .replace("step_kind", "j.step_kind"))
    assert _rx().decide(sql, "job_events", _fact()).applied is True


def test_a_query_without_the_predicate_is_refused_once():
    found = _rx().decide(_COUNT_MISSING, "job_events", _fact())
    assert found is not None and found.refuse is True and found.applied is False


@pytest.mark.parametrize("pred", [
    # one condition missing
    "NOT (COALESCE(pipeline, '') <> '' AND COALESCE(run_ref, '') <> '')",
    # one value missing
    _PRED.replace(", 'SHIP_BUFFER'", ""),
    # a value of another case is another value
    _PRED.replace("'PACK'", "'pack'"),
    # IN where the owner said NOT IN
    _PRED.replace("NOT IN", "IN"),
    # a different comparison
    _PRED.replace("COALESCE(pipeline, '') <> ''", "pipeline IS NOT NULL"),
    # the inner conjunction without its NOT selects only the excluded rows
    _PRED[len("NOT "):],
    # a qualifier that binds to no relation of the query
    _PRED.replace("pipeline", "other.pipeline"),
])
def test_a_near_miss_predicate_is_refused(pred):
    found = _rx().decide(_with(pred), "job_events", _fact())
    assert found is not None and found.refuse is True


def test_the_predicate_or_ed_in_is_refused():
    sql = (f"SELECT COUNT(*) FROM job_events WHERE state = 'FAILED' AND "
           f"({_WIN} OR {_PRED})")
    assert _rx().decide(sql, "job_events", _fact()).refuse is True
    sql = f"SELECT COUNT(*) FROM job_events WHERE state = 'FAILED' OR {_PRED}"
    assert _rx().decide(sql, "job_events", _fact()).refuse is True


def test_and_binds_tighter_than_or_so_a_predicate_after_an_or_is_not_carried():
    """``a OR b AND <pred>`` is ``a OR (b AND <pred>)``: rows matching ``a``
    are not filtered, so the predicate is not a conjunct of the whole WHERE."""
    sql = f"SELECT COUNT(*) FROM job_events WHERE state = 'A' OR state = 'B' AND {_PRED}"
    assert _rx().decide(sql, "job_events", _fact()).refuse is True
    sql = f"SELECT COUNT(*) FROM job_events WHERE (state = 'A' OR state = 'B') AND {_PRED}"
    assert _rx().decide(sql, "job_events", _fact()).applied is True


def test_a_read_inside_a_cte_is_read_on_that_reads_where():
    rx = _rx()
    missing = ("WITH f AS (SELECT step_kind FROM job_events WHERE state = 'FAILED') "
               "SELECT step_kind, COUNT(*) FROM f GROUP BY step_kind")
    assert rx.decide(missing, "job_events", _fact()).refuse is True
    present = (f"WITH f AS (SELECT step_kind FROM job_events WHERE state = 'FAILED' AND {_PRED}) "
               "SELECT step_kind, COUNT(*) FROM f GROUP BY step_kind")
    assert rx.decide(present, "job_events", _fact()).applied is True


def test_a_derived_table_read_is_read_on_its_own_where():
    rx = _rx()
    present = (f"SELECT COUNT(*) FROM (SELECT id FROM job_events WHERE {_PRED}) t")
    assert rx.decide(present, "job_events", _fact()).applied is True
    missing = "SELECT COUNT(*) FROM (SELECT id FROM job_events WHERE state = 'X') t"
    assert rx.decide(missing, "job_events", _fact()).refuse is True


def test_a_list_expansion_is_not_a_join():
    rx = _rx()
    sql = ("SELECT e.v, COUNT(*) FROM job_events CROSS JOIN UNNEST(split(qty, ',')) AS e(v) "
           f"WHERE state = 'FAILED' AND {_PRED} GROUP BY 1")
    assert rx.decide(sql, "job_events", _fact()).applied is True
    assert rx.decide(sql.replace(f" AND {_PRED}", ""), "job_events", _fact()).refuse is True


@pytest.mark.parametrize("sql", [
    # a join with another table
    "SELECT COUNT(*) FROM job_events j JOIN ledger_lines l ON j.run_ref = l.batch_tag",
    # a self-join (two reads)
    "SELECT COUNT(*) FROM job_events a JOIN job_events b ON a.run_ref = b.run_ref",
    # a join with a CTE
    ("WITH k AS (SELECT 'A' AS c) SELECT COUNT(*) FROM job_events j JOIN k ON j.state = k.c"),
    # a union of two reads, and of one read with a literal row
    "SELECT state FROM job_events UNION ALL SELECT state FROM job_events",
    "SELECT state FROM job_events WHERE state = 'X' UNION ALL SELECT 'Y'",
    # the table read only in an expression subquery
    "SELECT (SELECT COUNT(*) FROM job_events) AS n",
    "SELECT 1 AS n WHERE EXISTS (SELECT 1 FROM job_events)",
    # a comma join
    "SELECT COUNT(*) FROM job_events, ledger_lines",
    # uncertain tokenisation
    "SELECT COUNT(*) FROM job_events WHERE state = 'open",
    "SELECT COUNT(*) FROM job_events /* a /* nested */ comment */",
    # two statements
    "SELECT COUNT(*) FROM job_events; SELECT 1",
    # the conditions applied outside the read: a pass-through CTE filtered later
    (f"WITH x AS (SELECT * FROM job_events) SELECT COUNT(*) FROM x WHERE "
     + _PRED.replace("pipeline", "x.pipeline")),
    # the conditions measured in the select list
    ("SELECT COUNT(*), SUM(CASE WHEN COALESCE(pipeline, '') <> '' THEN 1 ELSE 0 END) "
     "FROM job_events"),
])
def test_a_shape_with_several_or_uncertain_reads_is_refused_not_run_silently(sql):
    """Each of these reads, or may read, the table without the predicate in
    the WHERE of every read: refused once like a single read without it
    (``test_row_exclusion_every_read`` covers each shape with the predicate)."""
    found = _rx().decide(sql, "job_events", _fact())
    assert found is not None and found.refuse is True


def test_another_table_on_the_same_system_is_untouched():
    rx = _rx()
    assert rx.decide("SELECT COUNT(*) FROM ledger_lines WHERE amount > 0", "job_events",
                     _fact()) is None
    assert rx.decide("SELECT COUNT(*) FROM information_schema.columns "
                     "WHERE table_name = 'job_events'", "job_events", _fact()) is None
    assert rx.decide('SELECT * FROM "job_events$partitions"', "job_events", _fact()) is None


def test_a_quoted_table_name_is_matched_exactly():
    rx = _rx()
    assert rx.decide('SELECT COUNT(*) FROM "lake"."job_events"', "job_events",
                     _fact()).refuse is True
    assert rx.decide('SELECT COUNT(*) FROM "Job_Events"', "job_events", _fact()) is None
    assert rx.decide("SELECT COUNT(*) FROM Job_Events", "job_events", _fact()).refuse is True


def test_one_trailing_semicolon_is_still_read():
    assert _rx().decide(_COUNT_MISSING + ";", "job_events", _fact()).refuse is True


def test_classify_names_why_a_shape_was_not_decided():
    rx = _rx()
    assert rx.classify(_COUNT_MISSING, "job_events", _fact()).kind == "missing"
    assert rx.classify(_with(_PRED), "job_events", _fact()).kind == "applied"
    joined = rx.classify("SELECT 1 FROM job_events j JOIN ledger_lines l ON 1 = 1",
                         "job_events", _fact())
    assert joined.kind == "missing"
    comma = rx.classify("SELECT 1 FROM job_events, ledger_lines", "job_events", _fact())
    assert comma.kind == "undecided" and comma.reason == "uncertain_relations"
    assert rx.classify("SELECT 1 FROM ledger_lines", "job_events", _fact()).kind == "not_read"


# --------------------------------------------------------------------------- #
# A differently named schema generalizes.
# --------------------------------------------------------------------------- #
def test_a_differently_named_schema_is_checked_the_same_way():
    rx = _rx()
    fact = rx.exclusion_from("test postings", [
        {"column": "channel", "op": "in", "values": ["TEST", "SANDBOX"]},
        {"column": "batch_tag", "op": "empty"}])
    pred = rx.predicate_sql(fact)
    assert pred == ("NOT (COALESCE(channel, '') IN ('TEST', 'SANDBOX') AND "
                    "COALESCE(batch_tag, '') = '')")
    missing = "SELECT SUM(amount) FROM ledger_lines WHERE posted_at = DATE '2026-10-01'"
    assert rx.decide(missing, "ledger_lines", fact).refuse is True
    assert rx.decide(f"{missing} AND {pred}", "ledger_lines", fact).applied is True
    assert rx.decide(_COUNT_MISSING, "ledger_lines", fact) is None


# --------------------------------------------------------------------------- #
# The refusal, the disclosure and the exclusion-applied text read as facts.
# --------------------------------------------------------------------------- #
def test_the_refusal_states_the_label_and_the_exact_predicate_to_and_in():
    rx = _rx()
    found = rx.decide(_COUNT_MISSING, "job_events", _fact())
    payload = rx.RowExclusionError(found).to_dict()
    assert payload["error_name"] == "ROW_EXCLUSION"
    assert payload["error_type"] == "invalid_request"
    assert payload["reason"] == "row_exclusion_not_applied"
    assert payload["retryable"] is True
    assert payload["predicate"] == _PRED
    text = payload["guidance"]
    assert _LABEL in text and f"AND {_PRED}" in text
    assert "send the same query again unchanged" in text
    assert "\u2014" not in text and "\u2013" not in text


def test_the_resend_disclosure_says_the_figures_may_include_the_excluded_rows():
    rx = _rx()
    found = rx.decide(_COUNT_MISSING, "job_events", _fact())
    line, block = rx.engine_line([found]), rx.requirement_block([found])
    assert line.startswith(rx.ENGINE_PREFIX)
    assert "without the source owner's row exclusion" in line and _LABEL in line
    assert "REQUIRED" in block and "may include rows" in block and _LABEL in block


def test_the_applied_disclosure_asks_for_one_short_clause():
    rx = _rx()
    found = rx.decide(_with(_PRED), "job_events", _fact())
    line, block = rx.engine_line([found]), rx.requirement_block([found])
    assert "with the source owner's row exclusion" in line and _LABEL in line
    assert "one short clause" in block and "excluded" in block and _LABEL in block
    assert rx.engine_line([]) == "" and rx.requirement_block([]) == ""


def test_a_copied_engine_line_is_neutralized():
    rx = _rx()
    found = rx.decide(_COUNT_MISSING, "job_events", _fact())
    copied = "As the engine said: " + rx.engine_line([found])
    assert rx.ENGINE_PREFIX not in rx.neutralize(copied)


# --------------------------------------------------------------------------- #
# The registry and the bake.
# --------------------------------------------------------------------------- #
def test_the_registry_remembers_and_forgets_per_system():
    rx = _rx()
    rx.remember("sys", {"job_events": _fact()})
    assert rx.has_exclusions("sys")
    assert rx.exclusion_for("sys", "JOB_EVENTS") == _fact()
    assert rx.exclusion_for("sys", "JOB_EVENTS", quoted=True) is None
    assert rx.exclusion_for("other", "job_events") is None
    rx.remember("sys", {})
    assert not rx.has_exclusions("sys")


def test_decide_for_reads_the_systems_remembered_tables():
    rx = _rx()
    rx.remember("sys", {"job_events": _fact()})
    assert rx.decide_for("sys", _COUNT_MISSING).refuse is True
    assert rx.decide_for("sys", "SELECT 1 FROM ledger_lines") is None
    assert rx.decide_for("other", _COUNT_MISSING) is None


_DESC = ("job_events [base_table](pipeline: varchar; run_ref: varchar; step_kind: varchar; "
         "finished_at: timestamp(6) with time zone), "
         "ledger_lines(batch_tag: varchar; posted_at: date)")


def _stored(fact_conditions=_CONDITIONS, label=_LABEL):
    return json.dumps({"label": label, "conditions": fact_conditions})


def _listed(tables=("job_events",), *, job_types=None, pending=False, store=True):
    """A graph whose System lists ``tables`` in its row-exclusion manifest,
    with the measured-shape fact stored on job_events (and a one-condition
    fact on ledger_lines when it is listed); job_events' Fields are
    ``job_types`` (the same dict, read live)."""
    from tests.unit._row_exclusion_graph import RxGraph
    g = RxGraph({"sys": {"job_events": _JOB, "ledger_lines": _LEDGER}})
    if job_types is not None:
        g.entries["sys.job_events"]["fields"] = job_types
    facts = {"job_events": {"label": _LABEL, "conditions": _CONDITIONS},
             "ledger_lines": {"label": "test rows", "conditions": _LEDGER_FACT}}
    for table in tables:
        g.list_table("sys", table, pending=pending)
        if store:
            g.store("sys", table, facts[table])
    return g


def _fact_reads(g) -> list:
    return [p["seid"] for kind, p in g.calls if kind == "fact"] + [
        seid for kind, p in g.calls if kind == "facts" for seid in p["seids"]]


def test_the_bake_reads_the_tables_the_manifest_lists_not_the_table_list(monkeypatch):
    rx = _rx()
    g = _listed(("job_events",))
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    rx.remember_from_graph("sys", _DESC)
    assert _fact_reads(g) == ["sys.job_events"]
    assert rx.exclusion_for("sys", "job_events") == _fact()
    assert rx.exclusion_for("sys", "ledger_lines") is None


@pytest.mark.parametrize("fault", ["import", "manifest", "facts"])
def test_each_bake_read_failure_leaves_the_tables_unavailable_not_absent(monkeypatch, fault):
    """A bake that could not read the graph does not know whether a table has a
    row exclusion: an unreadable manifest leaves the system unknown and a
    failed fact read leaves each listed table UNAVAILABLE (never absent), the
    schema line is empty, and a read of either table is held back
    (``unavailable_for``); structural discovery is not."""
    rx = _rx()
    g = _listed(("job_events", "ledger_lines"))
    if fault == "import":
        monkeypatch.setattr(rx, "_graph_query",
                            lambda: (_ for _ in ()).throw(ImportError("no db")))
    else:
        g.fail.update({"manifest"} if fault == "manifest" else {"fact", "facts"})
        monkeypatch.setattr(rx, "_graph_query", g.seam())
    rx.remember_from_graph("sys", _DESC)
    assert not rx.has_exclusions("sys")
    assert rx.schema_lines(rx.exclusions_for("sys")) == ""
    for table in ("job_events", "ledger_lines"):
        held = rx.unavailable_for("sys", f"SELECT COUNT(*) FROM {table}")
        assert held is not None and held.kind == "read_failed"
        assert held.table == (table if fault == "facts" else rx.ALL_TABLES)
    assert rx.unavailable_for("sys", "SELECT 1 AS n FROM information_schema.columns "
                                     "WHERE table_name = 'job_events'") is None


def test_a_successful_empty_read_is_absent_not_unavailable(monkeypatch):
    rx = _rx()
    monkeypatch.setattr(rx, "_graph_query", lambda: (lambda _c, _p: [{}]))
    rx.remember_from_graph("sys", _DESC)
    assert not rx.has_exclusions("sys")
    assert rx.unavailable_for("sys", _COUNT_MISSING) is None


def test_the_next_check_reads_the_fact_again_and_heals(monkeypatch):
    """A transient graph error heals without a rebuild: each check of a system
    in the unavailable state reads its state once more."""
    rx = _rx()
    g = _listed(("job_events",))
    g.fail.add("manifest")
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    rx.remember_from_graph("sys", _DESC)
    assert rx.unavailable_for("sys", _COUNT_MISSING) is not None   # re-read, still down
    g.fail.clear()
    reads = len(g.calls)
    assert rx.unavailable_for("sys", _COUNT_MISSING) is None        # one re-read heals it
    assert len(g.calls) > reads
    assert rx.exclusion_for("sys", "job_events") == _fact()
    reads = len(g.calls)
    assert rx.unavailable_for("sys", _COUNT_MISSING) is None        # healed: no fact re-read
    assert g.kinds()[reads:] == ["manifest"]


def _graph_down(monkeypatch, rx):
    def _raises(*_a, **_k):
        raise RuntimeError("graph down")
    monkeypatch.setattr(rx, "_graph_query", lambda: _raises)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM job_events j JOIN ledger_lines l ON j.run_ref = l.batch_tag",
    "SELECT COUNT(*) FROM job_events WHERE state = 'open",
    "SELECT state FROM job_events UNION ALL SELECT 'x'",
    "SELECT COUNT(*) FROM lake.job_events",
])
def test_any_read_that_may_reach_an_unavailable_table_is_held(monkeypatch, sql):
    rx = _rx()
    _graph_down(monkeypatch, rx)
    rx.mark_unavailable("sys", _DESC)
    assert rx.unavailable_for("sys", sql) is not None


def test_the_unavailable_refusal_offers_no_resend(monkeypatch):
    rx = _rx()
    _graph_down(monkeypatch, rx)
    rx.mark_unavailable("sys", _DESC)
    payload = rx.RowExclusionUnavailableError(
        rx.unavailable_for("sys", _COUNT_MISSING)).to_dict()
    assert payload["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert payload["reason"] == "row_exclusion_unavailable"
    assert payload["error_type"] == "invalid_request"
    assert payload["retryable"] is False
    text = payload["guidance"]
    assert "could not be read" in text and "cannot be confirmed" in text
    assert "send the same query again unchanged" not in text
    assert "Do not send this query again" in text
    assert "\u2014" not in text and "\u2013" not in text


def test_a_stored_exclusion_that_no_longer_validates_is_unavailable_not_absent(monkeypatch):
    """A stored value carrying a quote (written around the route, say) never
    reaches the agent as SQL, and is not taken for no fact either: the bake
    re-validates the shape and the table is unavailable, its reads held."""
    rx = _rx()
    g = _listed(("job_events",))
    g.entries["sys.job_events"]["props"]["row_exclusion"] = json.dumps(
        {"label": _LABEL, "conditions": [
            {"column": "step_kind", "op": "in", "values": ["A' OR '1'='1"]}]})
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    rx.remember_from_graph("sys", _DESC)
    assert not rx.has_exclusions("sys")
    held = rx.unavailable_for("sys", _COUNT_MISSING)
    assert held is not None and held.kind == "invalid_stored"
    assert rx.unavailable_for("sys", "SELECT COUNT(*) FROM ledger_lines") is None


# --------------------------------------------------------------------------- #
# Schema drift: a stored fact whose columns no longer fit the table.
# --------------------------------------------------------------------------- #
_LEDGER_FACT = [{"column": "batch_tag", "op": "empty"}]


def _drift_graph(job_types: dict, *, field_read_fails=False):
    """A graph read seam: the manifest lists job_events (its stored exclusion
    the measured shape) and ledger_lines, job_events' Fields now
    ``job_types`` (read live)."""
    g = _listed(("job_events", "ledger_lines"), job_types=job_types)
    if field_read_fails:
        g.fail.update({"field_types", "fields"})
    return g.query


@pytest.mark.parametrize("job_types,kind,column,wording", [
    ({c: t for c, t in _JOB.items() if c != "run_ref"},
     "unknown_column", "run_ref", "run_ref is no longer a column of it"),
    ({("run_reference" if c == "run_ref" else c): t for c, t in _JOB.items()},
     "unknown_column", "run_ref", "run_ref is no longer a column of it"),
    ({**_JOB, "pipeline": "bigint"},
     "column_not_string", "pipeline", "pipeline is now bigint, not a string column"),
], ids=["column_removed", "column_renamed", "string_to_non_string"])
def test_a_drifted_fact_is_unavailable_for_that_table_only(monkeypatch, job_types, kind,
                                                           column, wording):
    rx = _rx()
    monkeypatch.setattr(rx, "_graph_query", lambda: _drift_graph(job_types))
    rx.remember_from_graph("sys", _DESC)
    assert rx.exclusion_for("sys", "job_events") is None
    held = rx.unavailable_for("sys", _COUNT_MISSING)
    assert held is not None and held.table == "job_events"
    assert held.kind == kind and held.columns == (column,)
    # The other table's valid fact is still checked as usual.
    assert rx.unavailable_for("sys", "SELECT SUM(amount) FROM ledger_lines") is None
    assert rx.decide_for("sys", "SELECT SUM(amount) FROM ledger_lines").refuse is True
    # The refusal names the column, what is wrong and the steward repair.
    text = rx.RowExclusionUnavailableError(held).to_dict()["guidance"]
    assert "no longer fits the table" in text and wording in text
    assert "A steward must repair or clear the row exclusion of job_events" in text
    assert f"condition on {column}" in text
    assert "send the same query again unchanged" not in text


def test_a_failed_field_read_at_bake_is_unavailable(monkeypatch):
    rx = _rx()
    monkeypatch.setattr(rx, "_graph_query", lambda: _drift_graph(_JOB, field_read_fails=True))
    rx.remember_from_graph("sys", _DESC)
    held = rx.unavailable_for("sys", _COUNT_MISSING)
    assert held is not None and held.kind == "read_failed"


def test_a_fact_that_still_fits_its_table_is_present(monkeypatch):
    rx = _rx()
    monkeypatch.setattr(rx, "_graph_query", lambda: _drift_graph(dict(_JOB)))
    rx.remember_from_graph("sys", _DESC)
    assert rx.exclusion_for("sys", "job_events") == _fact()
    assert rx.unavailable_for("sys", _COUNT_MISSING) is None


def test_a_repaired_fact_heals_on_the_next_check(monkeypatch):
    rx = _rx()
    types = {**_JOB, "pipeline": "bigint"}
    monkeypatch.setattr(rx, "_graph_query", lambda: _drift_graph(types))
    rx.remember_from_graph("sys", _DESC)
    assert rx.unavailable_for("sys", _COUNT_MISSING) is not None
    types["pipeline"] = "varchar"          # the Field is a string column again
    assert rx.unavailable_for("sys", _COUNT_MISSING) is None
    assert rx.exclusion_for("sys", "job_events") == _fact()


def test_the_bake_reads_through_the_checked_graph_read(monkeypatch):
    """The unchecked graph read answers no rows when the read FAILED, which
    would look like a system with no row exclusion; the bake (and its re-read
    before each guarded query) uses the CHECKED short-deadline read the system
    agent already makes per question, so a failed read is unknown, never
    absent."""
    from agents import db, live_concept_mappings
    rx = _rx()
    assert rx._checked_graph_read() is live_concept_mappings._graph_read
    monkeypatch.setenv("GRAPH_BACKEND", "local")
    monkeypatch.setattr(db, "neptune_query", lambda *_a, **_k: [])

    def _checked(*_a, **_k):
        raise db.GraphQueryError("ThrottlingException", "slow down")
    monkeypatch.setattr(db, "neptune_query_checked", _checked)
    monkeypatch.setattr(rx, "_graph_query", rx._checked_graph_read)
    rx.remember_from_graph("sys", _DESC)
    held = rx.unavailable_for("sys", _COUNT_MISSING)
    assert held is not None and held.kind == "read_failed"
