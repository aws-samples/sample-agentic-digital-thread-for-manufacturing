# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The W3 identity-rule gate rejects a rate, and keeps an identifier key whose
name holds a singular time unit.

Rehearsal account, 2026-10-18: the ERP's resolve emitted three pattern
(``value_core_match``) rules from its invoice, payment and purchase-order
numbers to a plant MES's ``production_lines.capacity_per_hour``. That plant's
mapper run had given the capacity column an identifier role, so the affinity
pass sampled it: its three distinct values (``110``, ``130``, ...) are digit
cores inside the ERP's sequential numbers (``INV-<date>-00110``), so every
count gate held (3 of 3 values matched). Provision's semantic-role gate exists
for exactly this ("an identity endpoint can never be a MEASURE: a count /
quantity / rate / duration is not a join key"), but its unit tokens were plural
only (``hours``), and ``capacity_per_hour`` tokenizes to ``{capacity, per,
hour}``.

A singular unit is a measure only in rate grammar (right after ``per``): a key
such as ``business_day_id`` or ``shift_hour_id`` names a day or an hour, not an
amount of time. The gate's role check cannot rescue such a key: the rules the
affinity pass emits name no endpoint role. So every rule here comes out of the
pass's own builders (``affinity_rule`` / ``exact_rule`` / ``_rule_dict``).
"""
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.identity_affinity as aff  # noqa: E402
import agents.onboard as onboard  # noqa: E402


def _field(system, table, column, concept, values):
    return aff.SampledField(system, table, column, concept, tuple(values))


def _probe_over(*columns):
    full = {(c.system_id, c.table, c.field): set(c.values) for c in columns}
    return lambda field, values: sorted(set(values) & full[(field.system_id, field.table, field.field)])


#: The rehearsal shape: one ERP numbering per document type, the plant's three capacities.
_CAPACITY = _field("sys-plant", "production_lines", "capacity_per_hour", "specification",
                   ("110", "130", "150"))


@pytest.mark.parametrize("table,column,concept,prefix", [
    ("invoices", "invoice_number", "invoice", "INV"),
    ("payments", "payment_number", "payment", "PAY"),
    ("purchase_orders", "po_number", "purchase-order", "PO")])
def test_the_rehearsal_rules_to_a_per_hour_rate_are_rejected(table, column, concept, prefix):
    """The pattern pass emits the rule exactly as the rehearsal resolve did;
    provision's gate refuses it."""
    numbers = _field("sys-fin", table, column, concept,
                     (f"{prefix}-20261012-{n:05d}" for n in range(1, 201)))
    rule = aff.affinity_rule(numbers, _CAPACITY)
    assert (rule["method"], rule["source_pattern"], rule["target_pattern"]) == (
        aff.AFFINITY_METHOD, f"{prefix}-20261012-00{{id}}", "{id}")
    valid, reason = onboard._is_valid_identity_rule(rule)
    assert valid is False
    assert "capacity_per_hour" in reason and "measure" in reason


@pytest.mark.parametrize("table,column,prefix", [
    ("calendar", "business_day_id", "BD-2026-"),
    ("shifts", "shift_hour_id", "SH-20261012-")])
def test_an_identifier_key_named_for_a_day_or_hour_is_kept(table, column, prefix):
    """A measured exact join between two such keys, as the pass emits it, passes
    the gate: the unit names WHICH day or hour, not how long."""
    values = [f"{prefix}{n:04d}" for n in range(1001, 1013)]
    left = _field("sys-a", table, column, "schedule", values)
    right = _field("sys-b", f"{table}_log", column, "schedule", values)
    rule = aff.affinity_rule(left, right, _probe_over(left, right))
    assert (rule["method"], rule["shared_count"]) == (aff.EXACT_METHOD, len(values))
    assert onboard._is_valid_identity_rule(rule) == (True, "")


def test_the_rules_the_pass_emits_name_no_endpoint_role():
    """So the gate's identifier-role exemption never applies to them: the name
    alone has to tell a rate from a key."""
    values = [f"BD-2026-{n:04d}" for n in range(1001, 1013)]
    left = _field("sys-a", "calendar", "business_day_id", "schedule", values)
    right = _field("sys-b", "shifts", "business_day_id", "schedule", values)
    exact = aff.affinity_rule(left, right, _probe_over(left, right))
    pattern = aff.affinity_rule(_field("sys-fin", "invoices", "invoice_number", "invoice",
                                       (f"INV-20261012-{n:05d}" for n in range(1, 201))), _CAPACITY)
    for rule in (exact, pattern):
        assert not {"source_role", "target_role"} & set(rule)


def _built(target_column):
    """A rule from the pass's own builder, to a column named ``target_column``."""
    source = _field("sys-a", "orders", "order_number", "sales-order", ("ORD-1001",))
    target = _field("sys-b", "t", target_column, "sales-order", ("1001",))
    return aff._rule_dict(source, target, "ORD-{id}", "{id}", "built for the test")


@pytest.mark.parametrize("leaf", ["capacity_per_hour", "units_per_minute", "cost_per_day",
                                  "feed_per_second", "duration_hours", "elapsed_ms",
                                  "downtime_minutes"])
def test_a_rate_or_a_plural_duration_names_a_measure(leaf):
    valid, reason = onboard._is_valid_identity_rule(_built(leaf))
    assert valid is False and leaf in reason


@pytest.mark.parametrize("leaf", [
    "hour_per_shift", "hourPerShift",                  # the unit before per
    "units_per_machine_hour", "unitsPerMachineHour",   # a token between per and the unit
    "capacityPerHour", "day_output_per_line", "minute_cost_per_part",
    "scans_per_operator_second"])
def test_a_singular_unit_anywhere_in_a_per_name_is_a_rate(leaf):
    valid, reason = onboard._is_valid_identity_rule(_built(leaf))
    assert valid is False and leaf in reason


@pytest.mark.parametrize("leaf", ["business_day_id", "shift_hour_id", "day_code", "hour_slot",
                                  "businessDayId", "shiftHourId", "per_unit_code",
                                  "work_order_number", "run_number", "lot_number", "batch_id"])
def test_identifier_names_pass(leaf):
    assert onboard._is_valid_identity_rule(_built(leaf)) == (True, "")
