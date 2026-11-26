# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0063 D6 — onboarding schema-shape facts. Offline: no AWS, no graph,
no connector, no LLM.

Covers, for each of the four detectors, a POSITIVE case, a NEGATIVE case and
the stated boundary:

* dotted physical column names — a dot-free schema yields no fact;
* serialized-collection columns — a column whose empties are NULL/'' is NOT
  the literal-token trap;
* case-uniform enum vocabularies — a mixed-case vocabulary is not uniform;
* sparsely-populated columns — a dense column is NOT sparse (and distinct-mode
  samples make NO density claim at all).

Plus the wiring contract: the post-provision hook fires after mining and
before the job is reported done, a raising derivation never fails
provisioning, filing rides
``self_learning.log_proposed_mapping`` with never-auto-accept types only, and
NOTHING is written to business context.

Provision-job seams mirror ``test_post_provision_mining.py``. All table and
column names are generic engine placeholders (engine-neutrality ratchet).
"""
from __future__ import annotations

import inspect
import types

import pytest

import agents.onboard as onboard
import agents.schema_shape as shape


# --------------------------------------------------------------------------- #
# 1. Dotted physical column names
# --------------------------------------------------------------------------- #
def test_dotted_columns_are_reported_per_table():
    schema = {
        "records": {"columns": ["record_id", "details.attribute", "details.unit"]},
        "entries": {"columns": ["entry_id", "amount"]},
    }

    facts = shape.dotted_column_facts(schema)

    assert [f["table"] for f in facts] == ["records"]
    assert facts[0]["column"] == ""          # TABLE-grained by ADR-0063 D6
    assert facts[0]["dotted"] == ["details.attribute", "details.unit"]
    assert facts[0]["dotted_total"] == 2


def test_a_dot_free_schema_yields_no_dotted_fact():
    """BOUNDARY: no dots anywhere ⇒ no fact (never a vacuous row)."""
    schema = {"records": {"columns": ["record_id", "amount", "status_code"]}}

    assert shape.dotted_column_facts(schema) == []
    assert shape.dotted_column_facts({}) == []


def test_dotted_fact_text_defers_the_dialect_rule_to_the_engine():
    """The engine-generic HOW (quote it as one identifier) stays prompt-owned —
    the steward-facing fact records WHICH table carries dotted names."""
    text = shape.fact_text(shape.dotted_column_facts(
        {"records": {"columns": ["details.attribute"]}})[0])

    assert "records" in text and "details.attribute" in text
    assert "double-quoted" not in text.lower()
    assert len(text) <= shape.MAX_FACT_CHARS


def test_column_names_tolerate_dict_and_tuple_schema_shapes():
    schema = {"records": {"columns": [{"name": "details.attribute"},
                                      ("plain_column", "text")]}}

    assert shape.dotted_column_facts(schema)[0]["dotted"] == ["details.attribute"]


# --------------------------------------------------------------------------- #
# 2. Serialized-collection columns (empty collection is a literal token)
# --------------------------------------------------------------------------- #
def test_serialized_collection_column_with_literal_empty_token_is_flagged():
    samples = {"records": {"tag_list": ['["a","b"]', "[]", '["c"]']}}

    facts = shape.serialized_collection_facts(samples)

    assert len(facts) == 1
    assert facts[0]["kind"] == shape.KIND_SERIALIZED
    assert facts[0]["empty_token"] == "[]"
    assert (facts[0]["populated"], facts[0]["sampled"]) == (2, 3)


def test_serialized_object_column_uses_the_object_token():
    samples = {"records": {"attribute_map": ['{"k":1}', "{}"]}}

    assert shape.serialized_collection_facts(samples)[0]["empty_token"] == "{}"


def test_null_or_empty_string_emptiness_is_not_the_literal_token_trap():
    """BOUNDARY: a column that expresses emptiness as NULL/'' is representable
    the ordinary way — flagging it would be noise."""
    assert shape.serialized_collection_facts(
        {"records": {"tag_list": ['["a"]', None]}}) == []
    assert shape.serialized_collection_facts(
        {"records": {"tag_list": ['["a"]', ""]}}) == []


def test_a_plain_text_column_is_never_a_serialized_collection():
    assert shape.serialized_collection_facts(
        {"records": {"status_code": ["OPEN", "CLOSED"]}}) == []


def test_an_all_populated_collection_column_is_not_the_trap():
    """No empty token observed ⇒ nothing to warn about."""
    assert shape.serialized_collection_facts(
        {"records": {"tag_list": ['["a"]', '["b"]']}}) == []


def test_empty_collection_token_predicate_boundaries():
    assert shape.is_empty_collection_token("[]")
    assert shape.is_empty_collection_token("[ ]")
    assert shape.is_empty_collection_token("{}")
    assert not shape.is_empty_collection_token('["a"]')
    assert not shape.is_empty_collection_token("")
    assert not shape.is_empty_collection_token(None)
    assert not shape.is_empty_collection_token("[")


def test_empty_collection_counts_as_unpopulated():
    """The trap in one predicate: '[]' is neither NULL nor '' but carries no
    content, so a naive populated-count over-reports."""
    assert shape.is_populated('["a"]')
    assert not shape.is_populated("[]")
    assert not shape.is_populated(None)
    assert not shape.is_populated("   ")


# --------------------------------------------------------------------------- #
# 3. Case-uniform enum vocabularies
# --------------------------------------------------------------------------- #
def test_upper_case_uniform_vocabulary_is_flagged():
    samples = {"records": {"status_code": ["OPEN", "CLOSED", "OPEN", "HELD"]}}

    facts = shape.case_uniform_enum_facts(samples)

    assert len(facts) == 1
    assert facts[0]["case"] == "upper"
    assert facts[0]["vocabulary"] == ["CLOSED", "HELD", "OPEN"]
    assert facts[0]["distinct"] == 3
    assert "zero rows" in shape.fact_text(facts[0])


def test_lower_case_uniform_vocabulary_is_flagged():
    samples = {"records": {"status_code": ["open", "closed", "held"]}}

    assert shape.case_uniform_enum_facts(samples)[0]["case"] == "lower"


def test_mixed_case_vocabulary_is_not_case_uniform():
    """BOUNDARY: 'Open'/'CLOSED' proves nothing about a predicate's case."""
    assert shape.case_uniform_enum_facts(
        {"records": {"status_code": ["Open", "CLOSED", "held"]}}) == []


def test_a_single_distinct_value_is_a_constant_not_a_vocabulary():
    assert shape.case_uniform_enum_facts(
        {"records": {"status_code": ["OPEN", "OPEN", "OPEN"]}}) == []


def test_a_too_small_sample_makes_no_uniformity_claim():
    assert shape.case_uniform_enum_facts(
        {"records": {"status_code": ["OPEN", "CLOSED"]}}) == []


def test_high_cardinality_free_text_is_not_an_enum():
    values = [f"value-{n}" for n in range(shape.MAX_ENUM_DISTINCT + 2)]

    assert shape.case_uniform_enum_facts({"records": {"note_text": values}}) == []


def test_letter_free_values_are_not_a_case_uniform_vocabulary():
    assert shape.case_uniform_enum_facts(
        {"records": {"amount": ["1", "2", "3"]}}) == []


def test_serialized_collection_values_are_not_treated_as_a_vocabulary():
    assert shape.case_uniform_enum_facts(
        {"records": {"tag_list": ['["a"]', '["b"]', "[]"]}}) == []


# --------------------------------------------------------------------------- #
# 4. Sparsely-populated columns
# --------------------------------------------------------------------------- #
def test_sparse_column_is_flagged_from_row_shaped_samples():
    samples = {"records": {"optional_note": ["present", None, None, None, ""]}}

    facts = shape.sparse_column_facts(samples, shape.SAMPLE_KIND_ROWS)

    assert len(facts) == 1
    assert (facts[0]["populated"], facts[0]["sampled"]) == (1, 5)
    assert "small minority" in shape.fact_text(facts[0])


def test_a_dense_column_is_not_sparse():
    """BOUNDARY (pinned): mostly-populated ⇒ no fact."""
    samples = {"records": {"amount": ["1", "2", "3", None, "5"]}}

    assert shape.sparse_column_facts(samples, shape.SAMPLE_KIND_ROWS) == []


def test_the_literal_empty_token_does_not_hide_sparsity():
    """The ADR's observed trap: '[]' is not NULL, so an IS NOT NULL count calls
    this column fully populated — the derivation does not."""
    samples = {"records": {"tag_list": ['["a"]', "[]", "[]", "[]"]}}

    facts = shape.sparse_column_facts(samples, shape.SAMPLE_KIND_ROWS)

    assert (facts[0]["populated"], facts[0]["sampled"]) == (1, 4)


def test_a_fully_empty_column_makes_no_sparsity_claim():
    """Zero populated is 'no data in this sample', a DIFFERENT claim."""
    assert shape.sparse_column_facts(
        {"records": {"optional_note": [None, None, None]}},
        shape.SAMPLE_KIND_ROWS) == []


def test_distinct_samples_carry_no_density_information():
    """HONEST ABSENCE: de-duplicated non-null values cannot measure density, so
    the distinct-mode derivation claims nothing (the alternative would be a new
    COUNT round-trip, which ADR-0063 D6 forbids)."""
    samples = {"records": {"optional_note": ["present"]}}

    assert shape.sparse_column_facts(samples, shape.SAMPLE_KIND_DISTINCT) == []
    assert shape.sparse_column_facts(samples) == []          # default is distinct
    assert shape.sparse_column_facts(samples, shape.SAMPLE_KIND_ROWS) == []  # too few


# --------------------------------------------------------------------------- #
# Aggregation, ordering and bounds
# --------------------------------------------------------------------------- #
def test_derive_returns_every_kind_in_a_deterministic_order():
    """Kind order is pinned (the COUNT-corrupting traps first), and ONE column
    may legitimately carry two traps — the serialized-collection column here is
    also sparse once its empty tokens stop counting as data."""
    schema = {"records": {"columns": ["details.attribute"]}}
    samples = {
        "records": {
            "tag_list": ['["a"]', "[]", "[]", "[]"],
            "status_code": ["OPEN", "CLOSED", "HELD"],
            "optional_note": ["present", None, None, None],
        }
    }

    facts = shape.derive_schema_shape_facts(schema, samples,
                                            shape.SAMPLE_KIND_ROWS)

    assert [(f["kind"], f["column"]) for f in facts] == [
        (shape.KIND_SERIALIZED, "tag_list"),
        (shape.KIND_CASE_ENUM, "status_code"),
        (shape.KIND_SPARSE, "optional_note"),
        (shape.KIND_SPARSE, "tag_list"),
        (shape.KIND_DOTTED, ""),
    ]


def test_output_is_bounded_for_a_wide_schema():
    schema = {f"table_{n}": {"columns": ["details.attribute"]} for n in range(40)}
    samples = {f"table_{n}": {"status_code": ["OPEN", "CLOSED", "HELD"]}
               for n in range(40)}

    facts = shape.derive_schema_shape_facts(schema, samples)

    assert len(facts) == shape.MAX_FACTS
    assert all(len(shape.fact_text(f)) <= shape.MAX_FACT_CHARS for f in facts)


def test_fact_text_of_an_unknown_kind_is_empty():
    assert shape.fact_text({"kind": "not-a-kind"}) == ""


def test_derive_tolerates_absent_schema_and_samples():
    assert shape.derive_schema_shape_facts({}, {}) == []
    assert shape.derive_schema_shape_facts(None, None) == []


# --------------------------------------------------------------------------- #
# Persistence type — metadata-only insight
# --------------------------------------------------------------------------- #
def test_metadata_shape_type_is_never_auto_accepted():
    from agents.self_learning import _NEVER_AUTO_ACCEPT_TYPES

    assert "insight" in _NEVER_AUTO_ACCEPT_TYPES


# --------------------------------------------------------------------------- #
# Filing — the existing lifecycle, and nothing else
# --------------------------------------------------------------------------- #
@pytest.fixture
def _recording_lifecycle(monkeypatch):
    """Record ``log_proposed_mapping`` calls; fail loudly on any business-context
    or graph write."""
    import agents.business_context as bc
    import agents.self_learning as sl

    calls: list = []

    def _log(*args, **kwargs):
        calls.append((args, kwargs))
        return f"map-{len(calls)}"

    def _forbidden(*_a, **_k):
        raise AssertionError("schema-shape facts must never write steward text")

    monkeypatch.setattr(sl, "log_proposed_mapping", _log)
    monkeypatch.setattr(bc, "write_business_context", _forbidden)
    return calls


def test_facts_are_filed_as_metadata_only_proposals_with_derived_provenance(
        _recording_lifecycle):
    facts = shape.derive_schema_shape_facts(
        {"records": {"columns": ["details.attribute"]}},
        {"records": {"tag_list": ['["a"]', "[]"]}})

    counts = shape.file_schema_shape_facts("system-alpha", facts)

    assert counts == {"filed": 2, "field_annotation": 0, "insight": 2}
    for args, kwargs in _recording_lifecycle:
        assert kwargs["proposed_type"] == "insight"
        assert kwargs["source"] == "agent"
        assert kwargs["method"] == "onboarding-schema-shape"
        assert kwargs["provenance"] == "onboarding-schema-shape"
        envelope = __import__("json").loads(args[8])
        assert set(envelope) == {"schema_shape"}
        metadata = envelope["schema_shape"]
        assert metadata["shape_class"] in (shape.KIND_SERIALIZED,
                                            shape.KIND_DOTTED)
        assert all(v.startswith("sha256:") for k, v in metadata.items()
                   if k.endswith("_digest"))


def test_all_schema_shape_rows_omit_raw_field_endpoints(
        _recording_lifecycle):
    facts = shape.serialized_collection_facts(
        {"records": {"tag_list": ['["a"]', "[]"]}})

    shape.file_schema_shape_facts("system-alpha", facts)

    args, kwargs = _recording_lifecycle[0]
    assert args[1] == "" and args[4] == ""
    assert args[2] == "" and args[5] == ""
    assert kwargs["proposed_type"] == "insight"
    assert "records" not in args[8] and "tag_list" not in args[8]


def test_insight_rows_carry_no_field_endpoint(_recording_lifecycle):
    facts = shape.dotted_column_facts({"records": {"columns": ["details.attribute"]}})

    shape.file_schema_shape_facts("system-alpha", facts)

    args, _kwargs = _recording_lifecycle[0]
    assert args[1] == "" and args[4] == ""
    assert args[6] == "schema-shape"


def test_a_gate_dropped_proposal_is_not_counted_as_filed(monkeypatch):
    import agents.self_learning as sl
    monkeypatch.setattr(sl, "log_proposed_mapping", lambda *_a, **_k: None)

    counts = shape.file_schema_shape_facts(
        "system-alpha",
        shape.serialized_collection_facts({"records": {"tag_list": ['["a"]', "[]"]}}))

    assert counts == {"filed": 0, "field_annotation": 0, "insight": 0}


# --------------------------------------------------------------------------- #
# The transient sample stash — no new connector round-trips
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_stash():
    shape._sample_stash.clear()
    yield
    shape._sample_stash.clear()


def test_remembered_samples_are_popped_once():
    shape.remember_samples("system-alpha", {"records": {"status_code": ["OPEN"]}})

    assert shape.take_samples("system-alpha") == (
        {"records": {"status_code": ["OPEN"]}}, shape.SAMPLE_KIND_DISTINCT)
    assert shape.take_samples("system-alpha") == ({}, shape.SAMPLE_KIND_DISTINCT)


def test_the_stash_is_bounded_in_systems_columns_and_value_length():
    for n in range(shape._MAX_STASHED_SYSTEMS + 3):
        shape.remember_samples(f"system-{n}", {"records": {"status_code": ["OPEN"]}})
    assert len(shape._sample_stash) <= shape._MAX_STASHED_SYSTEMS

    shape.remember_samples("system-wide", {
        "records": {"note_text": ["x" * (shape._MAX_STASHED_VALUE_CHARS + 50)]
                    + ["y"] * 40}})
    samples, _kind = shape.take_samples("system-wide")
    values = samples["records"]["note_text"]
    assert len(values) <= shape._MAX_STASHED_VALUES
    assert max(len(v) for v in values) <= shape._MAX_STASHED_VALUE_CHARS


def test_remember_samples_ignores_empty_and_malformed_input():
    shape.remember_samples("system-alpha", {})
    shape.remember_samples("", {"records": {"status_code": ["OPEN"]}})
    shape.remember_samples("system-beta", {"records": "not-a-mapping"})

    assert shape._sample_stash == {}


def test_provision_sample_collection_stashes_for_the_derivation(monkeypatch):
    """The ONE seam that avoids a second connector round-trip: the values
    provisioning already sampled are handed to the derivation."""
    import agents.resolve_evidence as re_mod
    monkeypatch.setattr(re_mod, "sample_field_values",
                        lambda **_k: ["OPEN", "CLOSED"])

    out = onboard._collect_provision_samples(
        system_id="system-alpha", connection_type="PostgreSQL",
        table_details={"records": [("status_code", "text")]}, schema="s")

    assert out == {"records": {"status_code": ["OPEN", "CLOSED"]}}
    assert shape.take_samples("system-alpha") == (out, shape.SAMPLE_KIND_DISTINCT)


# --------------------------------------------------------------------------- #
# Entrypoint + provision-job wiring (test_post_provision_mining.py seams)
# --------------------------------------------------------------------------- #
def test_capture_derives_from_the_graph_schema_and_stashed_samples(
        monkeypatch, _recording_lifecycle):
    import agents.schema_check as schema_check
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: {"records": {"columns": ["details.attribute"]}})
    shape.remember_samples("system-alpha", {"records": {"tag_list": ['["a"]', "[]"]}})

    result = shape.run_schema_shape_capture("system-alpha")

    assert result == {"facts": 2, "filed": 2, "field_annotation": 0, "insight": 2}


def test_capture_returns_an_honest_zero_when_nothing_is_derivable(monkeypatch):
    import agents.schema_check as schema_check
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: None)

    assert shape.run_schema_shape_capture("system-alpha") == {
        "facts": 0, "filed": 0, "field_annotation": 0, "insight": 0}


def test_capture_never_raises(monkeypatch):
    import agents.schema_check as schema_check

    def boom(_sid):
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(schema_check, "load_system_schema", boom)

    assert shape.run_schema_shape_capture("system-alpha") == {
        "facts": 0, "filed": 0, "field_annotation": 0, "insight": 0}


@pytest.fixture
def _provision_seams(monkeypatch):
    """Neutralize every provision-job side effect except the hook under test
    (the ``test_post_provision_mining.py`` seam set)."""
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", lambda body: {"ok": True})
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_provision_jobs", {})
    monkeypatch.setattr(onboard, "_post_provision_mining", lambda *_a, **_k: {})

    import agents.coverage_gap_analyzer as cga
    import agents.schema_drift as schema_drift
    monkeypatch.setattr(schema_drift, "sweep_resolved_drift", lambda *_a, **_k: None)
    monkeypatch.setattr(cga, "run_coverage_gap_check", lambda ids: {})
    return types.SimpleNamespace(system_id="system-new", mappings=[])


def test_provision_job_captures_schema_shape_for_the_new_system(
        monkeypatch, _provision_seams):
    seen: list = []
    monkeypatch.setattr(shape, "run_schema_shape_capture",
                        lambda sid: seen.append(sid) or {})

    onboard._run_provision_job("job-1", _provision_seams)

    assert seen == ["system-new"]
    assert onboard._provision_jobs["job-1"]["status"] == "done"


def test_the_capture_runs_after_mining_and_before_the_job_is_reported_done(
        monkeypatch, _provision_seams):
    """The job is reported done only once its last phase (this capture) is
    over, so a client that sees done can act on the system at once."""
    events: list = []
    onboard._provision_jobs["job-1"] = {"status": "running"}     # as the route records it
    monkeypatch.setattr(onboard, "_post_provision_mining",
                        lambda sid: events.append("mine") or {})
    monkeypatch.setattr(
        shape, "run_schema_shape_capture",
        lambda sid: events.append(
            ("shape", onboard._provision_jobs["job-1"]["status"])) or {})

    onboard._run_provision_job("job-1", _provision_seams)

    assert events == ["mine", ("shape", "running")]
    assert onboard._provision_jobs["job-1"]["status"] == "done"


def test_a_raising_derivation_never_fails_the_provision_result(
        monkeypatch, _provision_seams):
    """PINNED: provisioning stands DONE with its result intact."""
    def boom(_sid):
        raise RuntimeError("derivation exploded")

    monkeypatch.setattr(shape, "run_schema_shape_capture", boom)

    onboard._run_provision_job("job-1", _provision_seams)

    assert onboard._provision_jobs["job-1"]["status"] == "done"
    assert onboard._provision_jobs["job-1"]["result"] == {"ok": True}


def test_the_hook_writes_no_business_context(monkeypatch, _provision_seams):
    """Deployment-specific shape facts are PROPOSALS, never steward text."""
    import agents.business_context as bc
    import agents.schema_check as schema_check
    import agents.self_learning as sl

    monkeypatch.setattr(bc, "write_business_context", lambda *_a, **_k: (
        pytest.fail("business context written by the schema-shape hook")))
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: {"records": {"columns": ["details.attribute"]}})
    monkeypatch.setattr(sl, "log_proposed_mapping", lambda *_a, **_k: "map-1")
    shape.remember_samples("system-new", {"records": {"tag_list": ['["a"]', "[]"]}})

    onboard._run_provision_job("job-1", _provision_seams)

    assert onboard._provision_jobs["job-1"]["status"] == "done"


# --------------------------------------------------------------------------- #
# Maintainability + purity bounds
# --------------------------------------------------------------------------- #
def test_every_function_stays_under_fifty_lines():
    source, _start = inspect.getsourcelines(shape)
    limit = 50
    current, name, longest = 0, "", ("", 0)
    for line in source:
        stripped = line.strip()
        if stripped.startswith("def ") and not line.startswith(" "):
            if current > longest[1]:
                longest = (name, current)
            name, current = stripped, 1
        elif name:
            current += 1
    if current > longest[1]:
        longest = (name, current)
    assert longest[1] <= limit, f"{longest[0]} is {longest[1]} lines (> {limit})"


def test_module_stays_stdlib_and_lazy_on_agents_imports():
    """The detectors are importable and runnable with zero AWS: only ``logging``
    and ``re`` at module scope; ``self_learning``/``schema_check`` are imported
    lazily inside the IO functions."""
    src = inspect.getsource(shape)
    header = src.split("def _as_text", 1)[0]

    assert "import boto3" not in src
    assert "from . import self_learning" not in header
    assert "from .schema_check import" not in header


def test_persisted_schema_shape_envelope_recursively_contains_no_raw_needles(
        _recording_lifecycle):
    needles = {"private_records", "secret.column", "raw-value-needle",
               "database-password-needle"}
    facts = shape.derive_schema_shape_facts(
        {"private_records": {"columns": ["secret.column"]}},
        {"private_records": {"secret.column": [
            "raw-value-needle", "database-password-needle", "[]"]}})

    shape.file_schema_shape_facts("system-alpha", facts)

    assert _recording_lifecycle
    for args, kwargs in _recording_lifecycle:
        serialized = __import__("json").dumps(
            {"args": args, "kwargs": kwargs}, sort_keys=True)
        assert not any(needle in serialized for needle in needles)
        assert len(args[8]) <= shape.MAX_ENVELOPE_CHARS
        metadata = __import__("json").loads(args[8])["schema_shape"]
        assert 0 <= metadata.get("populated_ratio", 0) <= 1
        assert all(0 <= value <= shape.MAX_METADATA_COUNT
                   for key, value in metadata.items() if key.endswith("_count"))


def test_malformed_fact_classes_cannot_smuggle_raw_text_into_envelope():
    needle = "database-password-needle"
    facts = [
        {
            "kind": needle,
            "table": needle,
            "column": needle,
            "value_digests": [needle],
        },
        {
            "kind": shape.KIND_CASE_ENUM,
            "case": needle,
            "table": needle,
            "column": needle,
        },
    ]

    envelopes = [shape.proposal_envelope(fact) for fact in facts]
    metadata = [__import__("json").loads(value)["schema_shape"]
                for value in envelopes]

    assert all(needle not in value for value in envelopes)
    assert metadata[0]["shape_class"] == "unknown"
    assert metadata[1]["letter_case"] == "unknown"
    assert all(len(value) <= shape.MAX_ENVELOPE_CHARS for value in envelopes)
