# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The rule merge and provision read a dotted column the same way.

A rule names a dotted column (a flattened struct column such as
``site1.billing.customer.id`` in table ``T``, or a tag path) in several
spellings: the column, ``T.<column>``, ``db.T.<column>`` or a trailing part
of it. The resolve step merges an affinity rule with the LLM rule for the
same column, and provision binds every rule it writes to the column it
names; two rules provision binds to one column get one IdentityRule id. So
for every spelling the merge pairs the LLM rule with the affinity rule
exactly when provision binds both to one column: otherwise one rule would
silently overwrite the other at provision. A spelling two columns share
pairs with neither and provision rejects it.
"""
from __future__ import annotations

import copy
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents import identity_affinity as aff  # noqa: E402
from agents.identity_affinity import SampledField  # noqa: E402

RUNS = tuple(f"RUN-2026092{d}-9{d}{k:03d}" for d in (1, 2, 3) for k in range(6))
C1 = "site1.billing.customer.id"
C2 = "site2.shipping.customer.id"
SID = "sys-a"


def _field(system, table, field, concept, values=RUNS):
    return SampledField(system, table, field, concept, tuple(values))


def _probe_over(*columns):
    full = {(c.system_id, c.table, c.field): set(c.values) for c in columns}

    def probe(field, values):
        return sorted(set(values) & full[(field.system_id, field.table, field.field)])
    return probe


def _exact(source, target):
    rule = aff.exact_rule(source, target, _probe_over(source, target))
    assert rule is not None and rule["method"] == aff.EXACT_METHOD
    return rule


ORDERS = _field("erp", "orders", "id", "customer")
E1 = _exact(_field(SID, "T", C1, "customer"), ORDERS)
E2 = _exact(_field(SID, "T", C2, "customer"), ORDERS)
#: What provision reads the source columns from: the confirmed mappings.
MAPPINGS = [{"table": "T", "field": f"T.{C1}"}, {"table": "T", "field": f"T.{C2}"},
            {"table": "T2", "field": "T2.lot_code"}]


def _llm(source_field, *, source_table=None, pattern=""):
    rule = {"concept": "customer", "source_field": source_field, "target_system": "erp",
            "target_field": "orders.id", "source_pattern": pattern,
            "target_pattern": "{id}" if pattern else "", "method": "", "reasoning": "llm",
            "confidence": 0.8}
    if source_table:
        rule["source_table"] = source_table
    return rule


def _provision(rules):
    """``[(rule, digest or the rejection reason)]`` as provision binds them."""
    out = []
    for rule in copy.deepcopy(rules):
        reason = onboard._bind_rule_source(rule, MAPPINGS)
        out.append((rule, reason or onboard._rule_digest_for(SID, rule)))
    return out


#: (spelling, its table, the column it names: C1, or None when it names no
#: one column; "two" when two columns share it).
SPELLINGS = [
    (C1, None, "C1"),
    (f"T.{C1}", None, "C1"),
    (f"db.T.{C1}", None, "C1"),
    (C1, "T", "C1"),
    ("billing.customer.id", None, "C1"),
    ("billing.customer.id", "T", "C1"),
    ("billing.customer.id", "U", None),
    ("customer.id", None, "two"),
    ("id", None, "two"),
    ("order_no", None, None),
]


@pytest.mark.parametrize("pattern", ["", "CUS-{id}"], ids=["adds-nothing", "transforms"])
@pytest.mark.parametrize("spelling,table,names", SPELLINGS)
def test_the_merge_pairs_a_spelling_exactly_when_provision_binds_it(spelling, table, names,
                                                                    pattern):
    llm = _llm(spelling, source_table=table, pattern=pattern)
    merged = aff.merge_affinity_rules([llm], [E1, E2])
    paired = llm not in merged or E1 not in merged
    llm_bound, e1_bound = _provision([llm])[0][1], _provision([E1])[0][1]
    assert paired == (llm_bound == e1_bound)
    assert paired == (names == "C1")
    if names == "two":
        assert "more than one discovered column" in llm_bound
    if paired:
        expected = [E1, E2] if not pattern else [llm, E2]
        assert merged == expected
    else:
        assert merged == [llm, E1, E2]
    written = [d for _r, d in _provision(merged) if not str(d).startswith("source_field")]
    assert len(written) == len(set(written)), "two rules provision writes on one id"


# ── a unique trailing part of a dotted column, on either end ───────────────

def test_a_unique_trailing_part_of_a_dotted_source_column_pairs():
    llm = _llm("customer.id")
    one = _exact(_field(SID, "T", "site1.billing.customer.id", "customer"), ORDERS)
    other = _exact(_field(SID, "T", "site2.shipping.contact.id", "customer"), ORDERS)
    merged = aff.merge_affinity_rules([llm], [one, other])
    assert merged == [one, other]
    (note,) = aff.exact_replacement_notes([llm], merged)
    assert note.startswith("T.site1.billing.customer.id -> erp.orders.id (")


def test_a_unique_trailing_part_of_a_dotted_target_column_pairs():
    runs = _field(SID, "runs", "run_number", "customer")
    header = _field("erp", "orders", "header.customer.id", "customer")
    line = _field("erp", "orders", "line.id", "customer")
    found = [_exact(runs, t) for t in (header, line)]
    llm = {**_llm("runs.run_number"), "target_field": "customer.id"}
    merged = aff.merge_affinity_rules([llm], found)
    assert merged == found
    (note,) = aff.exact_replacement_notes([llm], merged)
    assert note.startswith("runs.run_number -> erp.orders.header.customer.id (")


@pytest.mark.parametrize("target_field", ["customer.id", "id", "orders.customer.id"])
def test_a_trailing_part_two_dotted_target_columns_share_pairs_with_neither(target_field):
    runs = _field(SID, "runs", "run_number", "customer")
    header = _field("erp", "orders", "header.customer.id", "customer")
    line = _field("erp", "orders", "line.customer.id", "customer")
    found = [_exact(runs, t) for t in (header, line)]
    llm = {**_llm("runs.run_number"), "target_field": target_field}
    assert aff.merge_affinity_rules([llm], found) == [llm, *found]


def test_the_merge_and_provision_read_a_spelling_with_one_function(monkeypatch):
    from agents import dotted_columns
    calls: list = []
    real = dotted_columns.read_column

    def spy(spelling, columns, table_hint=""):
        calls.append(spelling)
        return real(spelling, columns, table_hint)
    monkeypatch.setattr(dotted_columns, "read_column", spy)
    aff.merge_affinity_rules([_llm("billing.customer.id")], [E1, E2])
    assert "billing.customer.id" in calls
    calls.clear()
    onboard._bind_rule_source(_llm("billing.customer.id"), MAPPINGS)
    assert calls == ["billing.customer.id"]
