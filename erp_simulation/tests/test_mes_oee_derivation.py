# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the pure MES OEE / machine-state derivation helpers.

These import-pure helpers (no DB, no I/O) back the MES demo-data enrichment that
supplies OEE Availability/Performance, cumulative runtime hours, and machine/line
state to the dashboard. This lane is SEPARATE from the agentic_digital_thread unit lane
(which does not import erp_simulation) — run both.

    python -m pytest erp_simulation/tests -q
"""
from __future__ import annotations

import pytest

from erp_simulation.mes_simulator import (
    compute_oee_components,
    derive_machine_state,
    derive_good_reject,
)


class TestComputeOeeComponents:
    def test_nominal(self):
        avail, perf, qual, oee = compute_oee_components(
            run_minutes=100, downtime_minutes=10, actual_quantity=900,
            target_rate=10, total_pass=95, total_sample=100)
        assert avail == pytest.approx(0.9)
        assert perf == pytest.approx(0.9)
        assert qual == pytest.approx(0.95)
        assert oee == pytest.approx(76.95)

    def test_zero_target_rate_gives_full_performance(self):
        _, perf, _, _ = compute_oee_components(100, 0, 900, 0, 100, 100)
        assert perf == 1.0

    def test_zero_sample_gives_full_quality(self):
        _, _, qual, _ = compute_oee_components(100, 0, 900, 10, 0, 0)
        assert qual == 1.0

    def test_downtime_exceeding_runtime_clamps_availability_to_zero(self):
        avail, _, _, _ = compute_oee_components(100, 150, 900, 10, 95, 100)
        assert avail == 0.0

    def test_performance_capped_at_one(self):
        _, perf, _, _ = compute_oee_components(100, 0, 99999, 10, 100, 100)
        assert perf == 1.0

    def test_zero_run_minutes_does_not_divide_by_zero(self):
        # run_minutes is clamped to a small positive floor internally.
        avail, _, _, oee = compute_oee_components(0, 0, 0, 10, 100, 100)
        assert 0.0 <= avail <= 1.0
        assert 0.0 <= oee <= 100.0


class TestDeriveMachineState:
    def test_open_downtime_wins(self):
        # DOWN has highest precedence, even over changeover and a running WO.
        assert derive_machine_state(True, True, True) == 'DOWN'
        assert derive_machine_state(False, True, False) == 'DOWN'

    def test_changeover_beats_running(self):
        assert derive_machine_state(True, False, True) == 'CHANGEOVER'

    def test_running_when_active_and_clear(self):
        assert derive_machine_state(True, False, False) == 'RUNNING'

    def test_idle_when_nothing_active(self):
        assert derive_machine_state(False, False, False) == 'IDLE'


class TestDeriveGoodReject:
    def test_sampled_yield_scales_to_full_batch(self):
        # QC sample 20/20 passed (100%) over an actual batch of 512 must NOT make
        # good_quantity=20 (the old bug → ~4% yield). Yield ~= sample rate.
        good, reject = derive_good_reject(512, total_pass=20, total_sample=20, fallback_yield=0.98)
        assert good + reject == 512
        assert good == 512 and reject == 0
        assert good / 512 >= 0.95  # realistic first-pass yield, not ~4%

    def test_sampled_partial_yield_applied_to_batch(self):
        # 19/20 sample (95%) → ~5% reject over 512 (~26), good ~486.
        good, reject = derive_good_reject(512, total_pass=19, total_sample=20, fallback_yield=0.98)
        assert good + reject == 512
        assert reject == round(512 * 0.05)
        assert 0.94 <= good / 512 <= 0.96

    def test_no_sample_uses_fallback_yield(self):
        good, reject = derive_good_reject(500, total_pass=0, total_sample=0, fallback_yield=0.99)
        assert good + reject == 500
        assert reject == 5  # 1% of 500
        assert good == 495

    def test_reject_never_exceeds_actual_and_good_nonnegative(self):
        good, reject = derive_good_reject(100, total_pass=0, total_sample=10, fallback_yield=0.98)
        assert reject == 100 and good == 0  # 0% yield → all reject, still consistent
        assert good >= 0

    def test_zero_actual_quantity(self):
        good, reject = derive_good_reject(0, total_pass=10, total_sample=10, fallback_yield=0.98)
        assert good == 0 and reject == 0
