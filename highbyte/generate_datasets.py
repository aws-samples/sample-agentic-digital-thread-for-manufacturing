#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generate 24-hour OPC UA dataset CSVs for all three manufacturing sites.

Each CSV follows the format:
    ALIAS,DATA_TYPE,TIMESTAMP_SECONDS,TIMESTAMP_NANO_OFFSET,QUALITY,VALUE

Data updates every 5 seconds = 17,280 rows per tag over 24 hours.
Line state (active/down/idle) is baked into the data so the OPC UA server
just cycles through rows.

Usage:
    python generate_datasets.py
"""
import contextlib
import csv
import gzip
import io
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The story mill's bearing-condition model is shared with the simulator's
# condition monitor (ADR-0073); it lives in the erp_simulation package so the
# Lambda images ship it, and is pure (no package imports) so it loads here.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from erp_simulation import condition_monitor  # noqa: E402

INTERVAL_SEC = condition_monitor.INTERVAL_SEC
TOTAL_SECONDS = condition_monitor.TOTAL_SECONDS  # 8 hours
NUM_STEPS = condition_monitor.NUM_STEPS  # 5760

OUTPUT_DIR = Path(__file__).parent / "datasets"

random.seed(42)  # reproducible


# ---------------------------------------------------------------------------
# Shift / state schedule helpers
# ---------------------------------------------------------------------------

@dataclass
class ShiftSchedule:
    """Defines when a line is active, idle, or down."""
    # List of (start_hour, end_hour) tuples when the line runs
    active_windows: list = field(default_factory=lambda: [(6, 14), (14, 22)])
    # Random downtime events: (start_step, duration_steps)
    downtimes: list = field(default_factory=list)


def make_downtimes(num_events=3, min_dur=12, max_dur=60):
    """Generate random downtime events spread across the 24h period."""
    events = []
    for _ in range(num_events):
        start = random.randint(500, NUM_STEPS - 200)  # nosec B311 - synthetic data generation, not security context
        dur = random.randint(min_dur, max_dur)  # nosec B311 - synthetic data generation, not security context
        events.append((start, dur))
    return sorted(events, key=lambda e: e[0])


def get_line_state(step, schedule):
    """Return 'active', 'down', or 'idle' for a given time step."""
    # Check downtime first
    for start, dur in schedule.downtimes:
        if start <= step < start + dur:
            return "down"
    # Check shift windows
    hour = (step * INTERVAL_SEC) / 3600.0
    for start_h, end_h in schedule.active_windows:
        if start_h <= hour < end_h:
            return "active"
    return "idle"


# ---------------------------------------------------------------------------
# Value generators
# ---------------------------------------------------------------------------

def noisy(base, noise_pct=0.02):
    return base + base * noise_pct * (random.random() * 2 - 1)  # nosec B311 - synthetic data generation, not security context


def ramp_counter(step, rate_per_step, state):
    """Accumulating counter that only increments when active."""
    # We track this externally via closure
    pass


class CounterGen:
    """Generates a monotonically increasing counter that pauses when not active."""
    def __init__(self, rate_per_step=25):
        self.value = 0
        self.rate = rate_per_step

    def step(self, state):
        if state == "active":
            self.value += int(noisy(self.rate, 0.1))
        return self.value


# ---------------------------------------------------------------------------
# Engineering units: the source says what its channels read in
# ---------------------------------------------------------------------------
# A hub agent, given values with no unit, reported the mills' vibration in
# inches per second while SiteWise and the pages read the same values in mm/s.
# The dataset writer (generate_site_csv) adds, to each process node with a
# channel below, one constant STRING tag naming the unit of each of its own
# channels; the OPC UA servers serve it like any tag. SFC writes modelled
# properties only, so its tag map leaves it out. The units are the ones the
# SiteWise asset models declare (DeploySitewiseModels.py, pinned equal by
# agentic_digital_thread/tests/unit/test_highbyte_engineering_units.py).
ENGINEERING_UNITS = {
    "CoolantTemp": "°C",
    "Vibration_X": "mm/s", "Vibration_Y": "mm/s", "Vibration_Z": "mm/s",
    "Spectrum_1X": "mm/s", "Spectrum_BPFO": "mm/s", "Spectrum_BPFI": "mm/s",
    "BPFO_Hz": "Hz", "BPFI_Hz": "Hz",
}
ENGINEERING_UNITS_SUFFIX = "_EngineeringUnits"


def with_engineering_units(tags):
    """``tags`` plus one ``<node>_EngineeringUnits`` tag for every process
    node with a channel in :data:`ENGINEERING_UNITS`: ``<channel>=<unit>`` for
    each such channel, in tag order, joined by ``"; "``. No comma: server.mjs
    splits each dataset row on ','. A node without such a channel gets none."""
    listed = {}
    for alias in tags:
        parent, leaf = alias.rsplit("/", 1)
        node = parent.rsplit("/", 1)[-1]
        prop = leaf[len(node) + 1:] if leaf.startswith(node + "_") else ""
        if prop in ENGINEERING_UNITS:
            listed.setdefault(parent, []).append(f"{prop}={ENGINEERING_UNITS[prop]}")
    for parent, pairs in listed.items():
        node = parent.rsplit("/", 1)[-1]
        value = "; ".join(pairs)
        tags[f"{parent}/{node}{ENGINEERING_UNITS_SUFFIX}"] = ("STRING", lambda st, v=value: v)
    return tags

# ---------------------------------------------------------------------------
# Tag definitions per site type
# ---------------------------------------------------------------------------

def bottling_tags(prefix, line_name):
    """Return tag generators for a bottling line."""
    tags = {}
    p = f"{prefix}/{line_name}"

    # Line-level metadata
    tags[f"{p}/{line_name}_LineOutput"] = ("INTEGER", CounterGen(30))
    tags[f"{p}/{line_name}_MaterialID"] = ("STRING", lambda st: "craft-lager-bottle")
    tags[f"{p}/{line_name}_OrderID"] = ("STRING", lambda st: f"MKE-{line_name[-1]}01")
    tags[f"{p}/{line_name}_Shift"] = ("STRING", None)  # special

    # Equipment stations (no pump telemetry — keeps file size git-friendly)
    stations_rate = ["Washer", "Filler", "Capper", "Labeller", "WeighCheck", "CasePacker", "Palletizer"]

    for station in stations_rate:
        sp = f"{p}/{line_name}{station}/{line_name}{station}"
        tags[f"{sp}_Good"] = ("INTEGER", CounterGen(24))
        tags[f"{sp}_Bad"] = ("INTEGER", CounterGen(1))
        tags[f"{sp}_Total"] = ("INTEGER", None)  # computed = good + bad
        tags[f"{sp}_Down"] = ("BOOLEAN", None)  # from state
        tags[f"{sp}_Blocked"] = ("BOOLEAN", lambda st: st == "idle")
        tags[f"{sp}_Starved"] = ("BOOLEAN", None)  # from state

    # FillerPump telemetry — full pump health suite
    # These map to SiteWise FillerPump model properties via HighByte pipeline
    fp = f"{p}/{line_name}Filler/{line_name}Filler"
    tags[f"{fp}_Pump_pump_general_speed"] = ("DOUBLE", lambda st: noisy(3583, 0.002) if st == "active" else 3583.0)
    tags[f"{fp}_Pump_motor_journal_bearing_de_temperature"] = ("DOUBLE", lambda st: noisy(140, 0.03) if st == "active" else noisy(128, 0.01))
    tags[f"{fp}_Pump_motor_journal_bearing_nde_temperature"] = ("DOUBLE", lambda st: noisy(135, 0.03) if st == "active" else noisy(125, 0.01))
    tags[f"{fp}_Pump_pump_journal_bearing_de_temperature"] = ("DOUBLE", lambda st: noisy(122, 0.03) if st == "active" else noisy(112, 0.01))
    tags[f"{fp}_Pump_pump_journal_bearing_nde_temperature"] = ("DOUBLE", lambda st: noisy(118, 0.03) if st == "active" else noisy(110, 0.01))
    tags[f"{fp}_Pump_motor_journal_bearing_de_x_displacement"] = ("DOUBLE", lambda st: noisy(0.038, 0.05) if st == "active" else noisy(0.034, 0.02))
    tags[f"{fp}_Pump_motor_journal_bearing_de_y_displacement"] = ("DOUBLE", lambda st: noisy(0.037, 0.05) if st == "active" else noisy(0.034, 0.02))
    tags[f"{fp}_Pump_motor_journal_bearing_nde_x_displacement"] = ("DOUBLE", lambda st: noisy(0.038, 0.05) if st == "active" else noisy(0.034, 0.02))
    tags[f"{fp}_Pump_motor_journal_bearing_nde_y_displacement"] = ("DOUBLE", lambda st: noisy(0.037, 0.05) if st == "active" else noisy(0.034, 0.02))
    # Additional pump properties
    tags[f"{fp}_Pump_pump_journal_bearing_de_x_displacement"] = ("DOUBLE", lambda st: noisy(0.042, 0.05) if st == "active" else noisy(0.038, 0.02))
    tags[f"{fp}_Pump_pump_journal_bearing_de_y_displacement"] = ("DOUBLE", lambda st: noisy(0.040, 0.05) if st == "active" else noisy(0.036, 0.02))
    tags[f"{fp}_Pump_pump_journal_bearing_nde_x_displacement"] = ("DOUBLE", lambda st: noisy(0.041, 0.05) if st == "active" else noisy(0.037, 0.02))
    tags[f"{fp}_Pump_pump_journal_bearing_nde_y_displacement"] = ("DOUBLE", lambda st: noisy(0.039, 0.05) if st == "active" else noisy(0.035, 0.02))
    tags[f"{fp}_Pump_pump_process_flow"] = ("DOUBLE", lambda st: noisy(2900, 0.03) if st == "active" else noisy(2887, 0.005))
    tags[f"{fp}_Pump_pump_process_inlet_pressure"] = ("DOUBLE", lambda st: noisy(53.2, 0.02) if st == "active" else noisy(52.9, 0.005))
    tags[f"{fp}_Pump_pump_process_oil_temperature"] = ("DOUBLE", lambda st: noisy(165, 0.04) if st == "active" else noisy(140, 0.01))
    tags[f"{fp}_Pump_pump_thrust_bearing_active_temperature"] = ("DOUBLE", lambda st: noisy(95, 0.03) if st == "active" else noisy(82, 0.01))
    tags[f"{fp}_Pump_pump_thrust_bearing_inactive_temperature"] = ("DOUBLE", lambda st: noisy(72, 0.02) if st == "active" else noisy(65, 0.01))
    tags[f"{fp}_Pump_pump_thrust_bearing_axial_position_1"] = ("DOUBLE", lambda st: noisy(0.15, 0.05) if st == "active" else noisy(0.12, 0.02))
    tags[f"{fp}_Pump_pump_thrust_bearing_axial_position_2"] = ("DOUBLE", lambda st: noisy(-0.15, 0.05) if st == "active" else noisy(-0.12, 0.02))

    # Buffers and conveyors
    tags[f"{p}/{line_name}BufferStart/{line_name}BufferStart_Level"] = ("INTEGER", lambda st: 260000 if st == "active" else 0)
    tags[f"{p}/{line_name}Buffer/{line_name}Buffer_Level"] = ("INTEGER", lambda st: int(noisy(130000, 0.1)) if st == "active" else 0)
    tags[f"{p}/{line_name}BufferEnd/{line_name}BufferEnd_Level"] = ("INTEGER", lambda st: int(noisy(50000, 0.15)) if st == "active" else 0)
    for i in range(1, 6):
        tags[f"{p}/{line_name}Conveyor{i}/{line_name}Conveyor{i}_Level"] = ("INTEGER", lambda st, i=i: int(noisy(20000 * i, 0.1)) if st == "active" else 0)

    return tags


def automotive_tags(prefix, line_name):
    """Return tag generators for an automotive parts line."""
    tags = {}
    p = f"{prefix}/{line_name}"

    tags[f"{p}/{line_name}_LineOutput"] = ("INTEGER", CounterGen(8))
    tags[f"{p}/{line_name}_MaterialID"] = ("STRING", lambda st: "engine-block-v6")
    tags[f"{p}/{line_name}_OrderID"] = ("STRING", lambda st: f"DET-{line_name[-1]}01")
    tags[f"{p}/{line_name}_Shift"] = ("STRING", None)
    tags[f"{p}/{line_name}_MaterialLot"] = ("STRING", lambda st: "CAST-2026-0315")
    tags[f"{p}/{line_name}_WorkOrderStatus"] = ("STRING", None)  # from state

    # CNC Machine
    cn = f"{p}/{line_name}CNC1/{line_name}CNC1"
    tags[f"{cn}_Status"] = ("STRING", None)  # from state
    tags[f"{cn}_SpindleSpeed"] = ("DOUBLE", lambda st: noisy(4500, 0.03) if st == "active" else 0.0)
    tags[f"{cn}_FeedRate"] = ("DOUBLE", lambda st: noisy(250, 0.05) if st == "active" else 0.0)
    tags[f"{cn}_ToolPosition_X"] = ("DOUBLE", lambda st: noisy(125.5, 0.1) if st == "active" else 0.0)
    tags[f"{cn}_ToolPosition_Y"] = ("DOUBLE", lambda st: noisy(80.2, 0.1) if st == "active" else 0.0)
    tags[f"{cn}_ToolPosition_Z"] = ("DOUBLE", lambda st: noisy(15.0, 0.15) if st == "active" else 0.0)
    tags[f"{cn}_Vibration_X"] = ("DOUBLE", lambda st: noisy(0.42, 0.2) if st == "active" else 0.01)
    tags[f"{cn}_Vibration_Y"] = ("DOUBLE", lambda st: noisy(0.38, 0.2) if st == "active" else 0.01)
    tags[f"{cn}_Vibration_Z"] = ("DOUBLE", lambda st: noisy(0.55, 0.2) if st == "active" else 0.01)
    tags[f"{cn}_HydraulicPressure"] = ("DOUBLE", lambda st: noisy(2100, 0.02) if st == "active" else 0.0)
    tags[f"{cn}_CoolantTemp"] = ("DOUBLE", lambda st: noisy(22.5, 0.05) if st == "active" else 20.0)
    tags[f"{cn}_CoolantFlowRate"] = ("DOUBLE", lambda st: noisy(12.8, 0.05) if st == "active" else 0.0)
    tags[f"{cn}_CycleTime"] = ("DOUBLE", lambda st: noisy(45.2, 0.08) if st == "active" else 0.0)
    tags[f"{cn}_PartsCount"] = ("INTEGER", CounterGen(1))
    tags[f"{cn}_ToolLifeRemaining"] = ("DOUBLE", None)  # special: decrements
    tags[f"{cn}_ToolChangeEvent"] = ("BOOLEAN", lambda st: False)
    tags[f"{cn}_PowerConsumption"] = ("DOUBLE", lambda st: noisy(18.5, 0.05) if st == "active" else 0.5)
    tags[f"{cn}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{cn}_Bad"] = ("INTEGER", CounterGen(0))  # rare
    tags[f"{cn}_Total"] = ("INTEGER", None)
    tags[f"{cn}_Down"] = ("BOOLEAN", None)

    # Heat Treatment
    ht = f"{p}/{line_name}HeatTreat/{line_name}HeatTreat"
    tags[f"{ht}_FurnaceTemp"] = ("DOUBLE", lambda st: noisy(870, 0.01) if st == "active" else 25.0)
    tags[f"{ht}_SoakTime"] = ("DOUBLE", lambda st: noisy(45, 0.05) if st == "active" else 0.0)
    tags[f"{ht}_QuenchTemp"] = ("DOUBLE", lambda st: noisy(45, 0.08) if st == "active" else 20.0)
    tags[f"{ht}_Status"] = ("STRING", None)
    tags[f"{ht}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{ht}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{ht}_Total"] = ("INTEGER", None)
    tags[f"{ht}_Down"] = ("BOOLEAN", None)

    # Surface Treatment
    sf = f"{p}/{line_name}SurfaceTreat/{line_name}SurfaceTreat"
    tags[f"{sf}_CoatingThickness"] = ("DOUBLE", lambda st: noisy(0.025, 0.1) if st == "active" else 0.0)
    tags[f"{sf}_BathTemp"] = ("DOUBLE", lambda st: noisy(55, 0.03) if st == "active" else 22.0)
    tags[f"{sf}_BathConcentration"] = ("DOUBLE", lambda st: noisy(12.5, 0.05) if st == "active" else 12.5)
    tags[f"{sf}_Status"] = ("STRING", None)
    tags[f"{sf}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{sf}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{sf}_Total"] = ("INTEGER", None)
    tags[f"{sf}_Down"] = ("BOOLEAN", None)

    # Assembly
    asm = f"{p}/{line_name}Assembly/{line_name}Assembly"
    tags[f"{asm}_TorqueValue"] = ("DOUBLE", lambda st: noisy(85, 0.03) if st == "active" else 0.0)
    tags[f"{asm}_TorqueTarget"] = ("DOUBLE", lambda st: 85.0)
    tags[f"{asm}_Status"] = ("STRING", None)
    tags[f"{asm}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{asm}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{asm}_Total"] = ("INTEGER", None)
    tags[f"{asm}_Down"] = ("BOOLEAN", None)

    # Quality Inspection
    qi = f"{p}/{line_name}QualityInspect/{line_name}QualityInspect"
    tags[f"{qi}_DimensionX"] = ("DOUBLE", lambda st: noisy(100.0, 0.001) if st == "active" else 0.0)
    tags[f"{qi}_DimensionY"] = ("DOUBLE", lambda st: noisy(50.0, 0.001) if st == "active" else 0.0)
    tags[f"{qi}_DimensionZ"] = ("DOUBLE", lambda st: noisy(25.0, 0.001) if st == "active" else 0.0)
    tags[f"{qi}_SurfaceFinish_Ra"] = ("DOUBLE", lambda st: noisy(0.8, 0.1) if st == "active" else 0.0)
    tags[f"{qi}_TolerancePass"] = ("BOOLEAN", lambda st: random.random() > 0.015 if st == "active" else True)  # nosec B311 - synthetic data generation, not security context
    tags[f"{qi}_DefectCode"] = ("STRING", lambda st: "none")
    tags[f"{qi}_FirstPassYield"] = ("DOUBLE", lambda st: noisy(98.5, 0.005))
    tags[f"{qi}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{qi}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{qi}_Total"] = ("INTEGER", None)

    # Packaging
    pk = f"{p}/{line_name}Packaging/{line_name}Packaging"
    tags[f"{pk}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{pk}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{pk}_Total"] = ("INTEGER", None)
    tags[f"{pk}_Down"] = ("BOOLEAN", None)
    tags[f"{pk}_Blocked"] = ("BOOLEAN", lambda st: st == "idle")
    tags[f"{pk}_Starved"] = ("BOOLEAN", None)

    # Environment
    env = f"{p}/{line_name}Environment/{line_name}Environment"
    tags[f"{env}_AmbientTemp"] = ("DOUBLE", lambda st: noisy(21.5, 0.02))
    tags[f"{env}_Humidity"] = ("DOUBLE", lambda st: noisy(42.0, 0.05))
    tags[f"{env}_CompressedAirPressure"] = ("DOUBLE", lambda st: noisy(6.8, 0.02))

    return tags


def food_tags(prefix, line_name):
    """Return tag generators for a food processing line."""
    tags = {}
    p = f"{prefix}/{line_name}"

    tags[f"{p}/{line_name}_LineOutput"] = ("INTEGER", CounterGen(15))
    tags[f"{p}/{line_name}_MaterialID"] = ("STRING", lambda st: "corn-flakes-batch")
    tags[f"{p}/{line_name}_OrderID"] = ("STRING", lambda st: f"CR-{line_name[-1]}01")
    tags[f"{p}/{line_name}_Shift"] = ("STRING", None)
    tags[f"{p}/{line_name}_BatchID"] = ("STRING", lambda st: "BATCH-2026-0320-001")
    tags[f"{p}/{line_name}_RecipeVersion"] = ("STRING", lambda st: "CF-v3.2")
    tags[f"{p}/{line_name}_IngredientLot"] = ("STRING", lambda st: "CORN-LOT-4521")

    # Mixer
    mx = f"{p}/{line_name}Mixer/{line_name}Mixer"
    tags[f"{mx}_Speed"] = ("DOUBLE", lambda st: noisy(120, 0.05) if st == "active" else 0.0)
    tags[f"{mx}_MixTime"] = ("DOUBLE", lambda st: noisy(300, 0.03) if st == "active" else 0.0)
    tags[f"{mx}_BatchWeight"] = ("DOUBLE", lambda st: noisy(2000, 0.02) if st == "active" else 0.0)
    tags[f"{mx}_Status"] = ("STRING", None)
    tags[f"{mx}_Good"] = ("INTEGER", CounterGen(5))
    tags[f"{mx}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{mx}_Total"] = ("INTEGER", None)
    tags[f"{mx}_Down"] = ("BOOLEAN", None)

    # Extruder
    ex = f"{p}/{line_name}Extruder/{line_name}Extruder"
    tags[f"{ex}_TempZone1"] = ("DOUBLE", lambda st: noisy(165, 0.02) if st == "active" else 25.0)
    tags[f"{ex}_TempZone2"] = ("DOUBLE", lambda st: noisy(175, 0.02) if st == "active" else 25.0)
    tags[f"{ex}_TempZone3"] = ("DOUBLE", lambda st: noisy(180, 0.02) if st == "active" else 25.0)
    tags[f"{ex}_Pressure"] = ("DOUBLE", lambda st: noisy(45, 0.05) if st == "active" else 0.0)
    tags[f"{ex}_DieTemp"] = ("DOUBLE", lambda st: noisy(190, 0.02) if st == "active" else 25.0)
    tags[f"{ex}_ScrewSpeed"] = ("DOUBLE", lambda st: noisy(300, 0.03) if st == "active" else 0.0)
    tags[f"{ex}_Status"] = ("STRING", None)
    tags[f"{ex}_Good"] = ("INTEGER", CounterGen(10))
    tags[f"{ex}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{ex}_Total"] = ("INTEGER", None)
    tags[f"{ex}_Down"] = ("BOOLEAN", None)

    # Oven
    ov = f"{p}/{line_name}Oven/{line_name}Oven"
    tags[f"{ov}_Temperature"] = ("DOUBLE", lambda st: noisy(200, 0.015) if st == "active" else 25.0)
    tags[f"{ov}_ConveyorSpeed"] = ("DOUBLE", lambda st: noisy(1.5, 0.05) if st == "active" else 0.0)
    tags[f"{ov}_Status"] = ("STRING", None)
    tags[f"{ov}_Good"] = ("INTEGER", CounterGen(10))
    tags[f"{ov}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{ov}_Total"] = ("INTEGER", None)
    tags[f"{ov}_Down"] = ("BOOLEAN", None)

    # Cooler
    cl = f"{p}/{line_name}Cooler/{line_name}Cooler"
    tags[f"{cl}_Temperature"] = ("DOUBLE", lambda st: noisy(25, 0.05) if st == "active" else 20.0)
    tags[f"{cl}_MoistureContent"] = ("DOUBLE", lambda st: noisy(3.5, 0.08) if st == "active" else 0.0)
    tags[f"{cl}_Status"] = ("STRING", None)
    tags[f"{cl}_Good"] = ("INTEGER", CounterGen(10))
    tags[f"{cl}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{cl}_Total"] = ("INTEGER", None)
    tags[f"{cl}_Down"] = ("BOOLEAN", None)

    # Packager
    pk = f"{p}/{line_name}Packager/{line_name}Packager"
    tags[f"{pk}_Speed"] = ("DOUBLE", lambda st: noisy(60, 0.05) if st == "active" else 0.0)
    tags[f"{pk}_SealTemp"] = ("DOUBLE", lambda st: noisy(145, 0.02) if st == "active" else 25.0)
    tags[f"{pk}_SealPressure"] = ("DOUBLE", lambda st: noisy(3.2, 0.05) if st == "active" else 0.0)
    tags[f"{pk}_FillWeight"] = ("DOUBLE", lambda st: noisy(500, 0.01) if st == "active" else 0.0)
    tags[f"{pk}_Status"] = ("STRING", None)
    tags[f"{pk}_Good"] = ("INTEGER", CounterGen(12))
    tags[f"{pk}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{pk}_Total"] = ("INTEGER", None)
    tags[f"{pk}_Down"] = ("BOOLEAN", None)
    tags[f"{pk}_Blocked"] = ("BOOLEAN", lambda st: st == "idle")
    tags[f"{pk}_Starved"] = ("BOOLEAN", None)

    # Metal Detector
    md = f"{p}/{line_name}MetalDetect/{line_name}MetalDetect"
    tags[f"{md}_Pass"] = ("BOOLEAN", lambda st: random.random() > 0.002 if st == "active" else True)  # nosec B311 - synthetic data generation, not security context
    tags[f"{md}_RejectCount"] = ("INTEGER", CounterGen(0))
    tags[f"{md}_TotalScanned"] = ("INTEGER", CounterGen(12))
    tags[f"{md}_Status"] = ("STRING", None)

    # CheckWeigher
    cw = f"{p}/{line_name}CheckWeigher/{line_name}CheckWeigher"
    tags[f"{cw}_Weight"] = ("DOUBLE", lambda st: noisy(500, 0.008) if st == "active" else 0.0)
    tags[f"{cw}_TargetWeight"] = ("DOUBLE", lambda st: 500.0)
    tags[f"{cw}_Pass"] = ("BOOLEAN", lambda st: random.random() > 0.01 if st == "active" else True)  # nosec B311 - synthetic data generation, not security context
    tags[f"{cw}_RejectCount"] = ("INTEGER", CounterGen(0))

    # Palletizer
    pl = f"{p}/{line_name}Palletizer/{line_name}Palletizer"
    tags[f"{pl}_Good"] = ("INTEGER", CounterGen(2))
    tags[f"{pl}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{pl}_Total"] = ("INTEGER", None)
    tags[f"{pl}_Down"] = ("BOOLEAN", None)
    tags[f"{pl}_Blocked"] = ("BOOLEAN", lambda st: st == "idle")
    tags[f"{pl}_Starved"] = ("BOOLEAN", None)

    # HACCP
    hc = f"{p}/{line_name}HACCP/{line_name}HACCP"
    tags[f"{hc}_CriticalTemp"] = ("DOUBLE", lambda st: noisy(72, 0.01) if st == "active" else 20.0)
    tags[f"{hc}_TempCompliant"] = ("BOOLEAN", lambda st: True)
    tags[f"{hc}_AllergenVerified"] = ("BOOLEAN", lambda st: True)
    tags[f"{hc}_PackageIntegrity"] = ("BOOLEAN", lambda st: random.random() > 0.003 if st == "active" else True)  # nosec B311 - synthetic data generation, not security context
    tags[f"{hc}_LabelVerified"] = ("BOOLEAN", lambda st: True)

    # CIP
    cip = f"{p}/{line_name}CIP/{line_name}CIP"
    tags[f"{cip}_Temperature"] = ("DOUBLE", lambda st: 80.0 if st == "idle" else noisy(80, 0.02))
    tags[f"{cip}_ChemConcentration"] = ("DOUBLE", lambda st: noisy(2.5, 0.05))
    tags[f"{cip}_Duration"] = ("DOUBLE", lambda st: 0.0)
    tags[f"{cip}_Status"] = ("STRING", None)

    # Environment
    env = f"{p}/{line_name}Environment/{line_name}Environment"
    tags[f"{env}_RoomTemp"] = ("DOUBLE", lambda st: noisy(18, 0.03))
    tags[f"{env}_Humidity"] = ("DOUBLE", lambda st: noisy(45, 0.05))
    tags[f"{env}_RefrigerationTemp"] = ("DOUBLE", lambda st: noisy(-18, 0.02))

    # OEE
    oee = f"{p}/{line_name}OEE/{line_name}OEE"
    tags[f"{oee}_Availability"] = ("DOUBLE", lambda st: noisy(95, 0.01) if st == "active" else 0.0)
    tags[f"{oee}_Performance"] = ("DOUBLE", lambda st: noisy(92, 0.02) if st == "active" else 0.0)
    tags[f"{oee}_Quality"] = ("DOUBLE", lambda st: noisy(99.2, 0.003) if st == "active" else 0.0)
    tags[f"{oee}_Overall"] = ("DOUBLE", lambda st: noisy(87.1, 0.02) if st == "active" else 0.0)

    return tags


# ---------------------------------------------------------------------------
# Fourth site machining — CMX-441 degradation cycle (ADR-0083 / ADR-0073)
# ---------------------------------------------------------------------------
# The fourth site carries the B-2847 / CMX-441 story LIVE instead of through the
# lambda-sitewise-feed backfill. Within every dataset loop the 5-axis mill's
# vibration climbs from a healthy baseline to the alarm level, the mill then
# stops for a bearing replacement (a FIXED downtime, not a random one), and the
# post-repair segment runs at the healthy baseline again — so whenever an
# attendee reaches the OT chapter, the recent window shows a climb. Numbers
# follow the seed: post-repair 0.9-1.3 mm/s (cmms work order 1195 log), alarm at
# 2.8 mm/s (work order 1195 description), coolant 68 -> 84 °C (feed handler).
#
# The curve itself lives in erp_simulation/condition_monitor.py, shared with
# the simulator's condition monitor that raises the CMMS work order when the
# BPFO band crosses threshold; the module-level names below stay the knobs
# (tests shrink them) and are passed through on every call.
CMX441_ASSET_NAME = condition_monitor.STORY_SITEWISE_ASSET      # SiteWise asset (model CNC)
CMX441_MES_EQUIPMENT_ID = condition_monitor.STORY_MES_EQUIPMENT_ID  # mes_line3.production_runs.equipment_id
CMX441_CMMS_ASSET_NUM = condition_monitor.STORY_CMMS_ASSET_NUM   # cmms_indianapolis.assets.asset_num
# MaterialLot tags name the raw-material lots the one corporate ERP seeds for
# the machining site (lambda-db-init ``_seed_indianapolis_supply``:
# ``LOT-IND-{item code without dashes}-SEED``). Constants, so every
# generation stays byte-identical.
CMX441_MATERIAL_LOT = "LOT-IND-BARAL7075150-SEED"        # BAR-AL7075-150 (7742)
MACHINING_LINE_MATERIAL_LOTS = {
    "Line1": "LOT-IND-RODST4140100-SEED",                # ROD-ST4140-100
    "Line2": "LOT-IND-SHTTI64125-SEED",                  # SHT-TI64-125
}
# MaterialID tags name the PLM part family each line machines
# (lambda-adt-seed/005_plm_engineering.sql): the line's lot feeds that part.
CMX441_MATERIAL_ID = "precision-shaft-7742"
MACHINING_LINE_MATERIAL_IDS = {
    "Line1": "drive-coupling-8810",
    "Line2": "aerospace-bracket-9901",
}
VIB_HEALTHY_MM_S = condition_monitor.VIB_HEALTHY_MM_S
VIB_ALARM_MM_S = condition_monitor.VIB_ALARM_MM_S
COOLANT_HEALTHY_C = 68.0
COOLANT_ALARM_C = 84.0
# Per-axis scale (X carries the headline trend) — same as the feed handler.
VIBRATION_AXIS_SCALE = {"X": 1.0, "Y": 0.92, "Z": 0.85}
# Bearing replacement: the mill alarms and stops at 90 % of the loop, is down
# for 20 minutes (240 x 5 s steps), then runs healthy until the loop wraps.
BEARING_FAIL_STEP = condition_monitor.BEARING_FAIL_STEP
BEARING_REPAIR_STEPS = condition_monitor.BEARING_REPAIR_STEPS
CMX441_DOWNTIME = condition_monitor.DOWNTIME
# Frequency-domain features (edge FFT output) published next to the RMS axes.
# Tag suffix -> feature key in condition_monitor.spectral_features().
SPECTRAL_TAGS = {
    "Spectrum_1X": "one_x", "Spectrum_BPFO": "bpfo", "Spectrum_BPFI": "bpfi",
    "CrestFactor": "crest", "Kurtosis": "kurtosis",
}


def cmx441_trend(step, healthy, alarm):
    """Pure, deterministic degradation trend for one loop step.

    Linear climb healthy -> alarm over [0, BEARING_FAIL_STEP); the alarm value
    while the bearing is being replaced; healthy again after the repair.
    Monotone non-decreasing before the repair, so a test can pin the shape.
    """
    return condition_monitor.trend(step, healthy, alarm, CMX441_DOWNTIME)


def cmx441_spectral(step):
    """Edge-analyzer features for one loop step (see condition_monitor)."""
    return condition_monitor.spectral_features(step, CMX441_DOWNTIME)


def _det_jitter(step, salt, pct):
    """Deterministic +-pct jitter from (step, salt) — no RNG state involved."""
    x = ((step * 7919 + salt * 104729) % 1000) / 1000.0  # 0 <= x < 1
    return (x * 2 - 1) * pct


class DegradationGen:
    """Emits cmx441_trend(step) with deterministic jitter; one instance per
    tag, advanced once per call exactly like CounterGen (generate_site_csv
    calls every tag once per step). Callable so the generic dispatch in
    _compute_tag_value picks it up like a plain lambda."""

    def __init__(self, healthy, alarm, down_value, scale=1.0, salt=1,
                 jitter_pct=0.02, decimals=4):
        self.healthy, self.alarm, self.down_value = healthy, alarm, down_value
        self.scale, self.salt = scale, salt
        self.jitter_pct, self.decimals = jitter_pct, decimals
        self.step_index = 0

    def __call__(self, state):
        step = self.step_index
        self.step_index += 1
        if state == "down":
            # Stopped for the bearing replacement: spindle not turning.
            return round(self.down_value * self.scale, self.decimals)
        trend = cmx441_trend(step, self.healthy, self.alarm)
        return round(trend * (1 + _det_jitter(step, self.salt, self.jitter_pct)) * self.scale, self.decimals)


class SpectralGen:
    """One edge-FFT feature of the story mill (cmx441_spectral(step)[key])
    with deterministic jitter; advanced once per call like DegradationGen.
    Emits 0.0 while the spindle is stopped and the alarm flag as a bool."""

    def __init__(self, key, salt, jitter_pct=0.03, decimals=4):
        self.key, self.salt = key, salt
        self.jitter_pct, self.decimals = jitter_pct, decimals
        self.step_index = 0

    def __call__(self, state):
        step = self.step_index
        self.step_index += 1
        value = cmx441_spectral(step)[self.key]
        if isinstance(value, bool):
            return value
        if state == "down" or value == 0.0:
            return 0.0
        return round(value * (1 + _det_jitter(step, self.salt, self.jitter_pct)), self.decimals)


def _machining_spectral_tags(cn, degrade):
    """Frequency-domain features next to the RMS axes: what an edge analyzer
    publishes after the FFT (ADR-0073). The story mill follows the
    degradation; the other mills sit at the healthy values, alarm never set."""
    tags = {}
    healthy = condition_monitor.spectral_features(0)
    for suffix, key in SPECTRAL_TAGS.items():
        if degrade:
            gen = SpectralGen(key, salt=sum(map(ord, key)))
        else:
            gen = (lambda base: (lambda st: noisy(base, 0.05) if st == "active" else 0.0))(healthy[key])
        tags[f"{cn}_{suffix}"] = ("DOUBLE", gen)
    tags[f"{cn}_BPFO_Alarm"] = ("BOOLEAN", SpectralGen("bpfo_alarm", salt=0) if degrade else (lambda st: False))
    # Defect frequencies for the running speed, so the bands are legible.
    tags[f"{cn}_BPFO_Hz"] = ("DOUBLE", lambda st: condition_monitor.BPFO_HZ if st == "active" else 0.0)
    tags[f"{cn}_BPFI_Hz"] = ("DOUBLE", lambda st: condition_monitor.BPFI_HZ if st == "active" else 0.0)
    return tags


def _machining_cnc_tags(cn, degrade):
    """CNC-model measurements for one mill. ``degrade`` swaps in the CMX-441
    degradation gauges for vibration and coolant temperature."""
    tags = {}
    tags[f"{cn}_Status"] = ("STRING", None)  # from state ('alarm' when down)
    tags[f"{cn}_SpindleSpeed"] = ("DOUBLE", lambda st: noisy(6200, 0.02) if st == "active" else 0.0)
    tags[f"{cn}_FeedRate"] = ("DOUBLE", lambda st: noisy(180, 0.05) if st == "active" else 0.0)
    tags[f"{cn}_ToolPosition_X"] = ("DOUBLE", lambda st: noisy(210.4, 0.1) if st == "active" else 0.0)
    tags[f"{cn}_ToolPosition_Y"] = ("DOUBLE", lambda st: noisy(96.7, 0.1) if st == "active" else 0.0)
    tags[f"{cn}_ToolPosition_Z"] = ("DOUBLE", lambda st: noisy(31.2, 0.15) if st == "active" else 0.0)
    for axis, scale in VIBRATION_AXIS_SCALE.items():
        if degrade:
            gen = DegradationGen(VIB_HEALTHY_MM_S, VIB_ALARM_MM_S, down_value=0.01, scale=scale, salt=ord(axis))
        else:
            gen = (lambda sc: (lambda st: noisy(VIB_HEALTHY_MM_S * sc, 0.1) if st == "active" else 0.01))(scale)
        tags[f"{cn}_Vibration_{axis}"] = ("DOUBLE", gen)
    tags[f"{cn}_HydraulicPressure"] = ("DOUBLE", lambda st: noisy(1950, 0.02) if st == "active" else 0.0)
    tags.update(_machining_spectral_tags(cn, degrade))
    if degrade:
        tags[f"{cn}_CoolantTemp"] = ("DOUBLE", DegradationGen(COOLANT_HEALTHY_C, COOLANT_ALARM_C, down_value=20.0, salt=99, jitter_pct=0.006, decimals=2))
    else:
        tags[f"{cn}_CoolantTemp"] = ("DOUBLE", lambda st: noisy(COOLANT_HEALTHY_C, 0.01) if st == "active" else 20.0)
    tags[f"{cn}_CoolantFlowRate"] = ("DOUBLE", lambda st: noisy(14.2, 0.05) if st == "active" else 0.0)
    tags[f"{cn}_CycleTime"] = ("DOUBLE", lambda st: noisy(96.0, 0.08) if st == "active" else 0.0)
    tags[f"{cn}_PartsCount"] = ("INTEGER", CounterGen(1))
    tags[f"{cn}_ToolLifeRemaining"] = ("DOUBLE", None)  # special: decrements
    tags[f"{cn}_ToolChangeEvent"] = ("BOOLEAN", lambda st: False)
    tags[f"{cn}_PowerConsumption"] = ("DOUBLE", lambda st: noisy(24.0, 0.05) if st == "active" else 0.5)
    tags[f"{cn}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{cn}_Bad"] = ("INTEGER", CounterGen(0))  # rare
    tags[f"{cn}_Total"] = ("INTEGER", None)
    tags[f"{cn}_Down"] = ("BOOLEAN", None)
    return tags


def machining_tags(prefix, line_name, cmx441=False):
    """Return tag generators for an Indianapolis precision-machining line.

    ``cmx441=True`` makes the line's mill the story asset: identity tags name
    the SiteWise asset, the MES equipment id and the CMMS asset number (so
    the graph can discover the cross-system identity instead of being told),
    and the vibration / coolant gauges follow the degradation cycle.
    """
    tags = {}
    p = f"{prefix}/{line_name}"
    n = line_name[-1]

    tags[f"{p}/{line_name}_LineOutput"] = ("INTEGER", CounterGen(3))
    material = CMX441_MATERIAL_ID if cmx441 else MACHINING_LINE_MATERIAL_IDS[line_name]
    tags[f"{p}/{line_name}_MaterialID"] = ("STRING", lambda st: material)
    tags[f"{p}/{line_name}_OrderID"] = ("STRING", lambda st: f"IND-{n}01")
    tags[f"{p}/{line_name}_Shift"] = ("STRING", None)
    lot = CMX441_MATERIAL_LOT if cmx441 else MACHINING_LINE_MATERIAL_LOTS[line_name]
    tags[f"{p}/{line_name}_MaterialLot"] = ("STRING", lambda st: lot)
    tags[f"{p}/{line_name}_WorkOrderStatus"] = ("STRING", None)  # from state

    cn = f"{p}/{line_name}CNC1/{line_name}CNC1"
    if cmx441:
        tags[f"{cn}_AssetName"] = ("STRING", lambda st: CMX441_ASSET_NAME)
        tags[f"{cn}_EquipmentID"] = ("STRING", lambda st: CMX441_MES_EQUIPMENT_ID)
        tags[f"{cn}_MaintenanceAssetNum"] = ("STRING", lambda st: CMX441_CMMS_ASSET_NUM)
    tags.update(_machining_cnc_tags(cn, degrade=cmx441))
    tags.update(_machining_downstream_tags(p, line_name))
    return tags


def _machining_downstream_tags(p, line_name):
    """Inspection, packaging and environment tags shared by every machining line."""
    tags = {}
    qi = f"{p}/{line_name}QualityInspect/{line_name}QualityInspect"
    tags[f"{qi}_DimensionX"] = ("DOUBLE", lambda st: noisy(120.0, 0.0005) if st == "active" else 0.0)
    tags[f"{qi}_DimensionY"] = ("DOUBLE", lambda st: noisy(64.0, 0.0005) if st == "active" else 0.0)
    tags[f"{qi}_DimensionZ"] = ("DOUBLE", lambda st: noisy(38.0, 0.0005) if st == "active" else 0.0)
    tags[f"{qi}_SurfaceFinish_Ra"] = ("DOUBLE", lambda st: noisy(0.6, 0.1) if st == "active" else 0.0)
    tags[f"{qi}_TolerancePass"] = ("BOOLEAN", lambda st: random.random() > 0.01 if st == "active" else True)  # nosec B311 - synthetic data generation, not security context
    tags[f"{qi}_DefectCode"] = ("STRING", lambda st: "none")
    tags[f"{qi}_FirstPassYield"] = ("DOUBLE", lambda st: noisy(98.9, 0.004))
    tags[f"{qi}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{qi}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{qi}_Total"] = ("INTEGER", None)

    # Packaging
    pk = f"{p}/{line_name}Packaging/{line_name}Packaging"
    tags[f"{pk}_Good"] = ("INTEGER", CounterGen(1))
    tags[f"{pk}_Bad"] = ("INTEGER", CounterGen(0))
    tags[f"{pk}_Total"] = ("INTEGER", None)
    tags[f"{pk}_Down"] = ("BOOLEAN", None)
    tags[f"{pk}_Blocked"] = ("BOOLEAN", lambda st: st == "idle")
    tags[f"{pk}_Starved"] = ("BOOLEAN", None)

    # Environment
    env = f"{p}/{line_name}Environment/{line_name}Environment"
    tags[f"{env}_AmbientTemp"] = ("DOUBLE", lambda st: noisy(21.0, 0.02))
    tags[f"{env}_Humidity"] = ("DOUBLE", lambda st: noisy(40.0, 0.05))
    tags[f"{env}_CompressedAirPressure"] = ("DOUBLE", lambda st: noisy(6.9, 0.02))

    return tags


def machining_site_lines(prefix="AnyCompany/Indianapolis/Machining"):
    """The fourth site's (tags, schedule) pairs. Line3 carries CMX-441 with the
    FIXED bearing-replacement downtime; Lines 1-2 get the usual random ones."""
    return [
        (machining_tags(prefix, "Line1"), ShiftSchedule(active_windows=[(0, 8)], downtimes=make_downtimes(2))),
        (machining_tags(prefix, "Line2"), ShiftSchedule(active_windows=[(0, 8)], downtimes=make_downtimes(2))),
        (machining_tags(prefix, "Line3", cmx441=True), ShiftSchedule(active_windows=[(0, 8)], downtimes=[CMX441_DOWNTIME])),
    ]


# ---------------------------------------------------------------------------
# Generate a single site CSV
# ---------------------------------------------------------------------------

_SKIP = object()  # sentinel: this tag emits no value for the current step


def _shift_for_ts(ts):
    """Shift number ("1"/"2"/"3") for a timestamp offset in seconds."""
    hour = ts / 3600.0
    if 6 <= hour < 14:
        return "1"
    if 14 <= hour < 22:
        return "2"
    return "3"


def _derived_total(alias, latest):
    """Total = Good + Bad from the sibling counters with the same prefix.

    Tag registration order puts *_Good/*_Bad before their *_Total, so their
    current-step values are already in `latest` — guard that rather than
    assume it. (*_TotalScanned has its own CounterGen and does not end with
    "_Total", so it never reaches this branch.)
    """
    tag_prefix = alias[: -len("_Total")]
    good_key = f"{tag_prefix}_Good"
    bad_key = f"{tag_prefix}_Bad"
    if good_key not in latest or bad_key not in latest:
        raise RuntimeError(
            f"{alias}: sibling counters {good_key!r} and "
            f"{bad_key!r} must be registered (and generated) "
            "before the _Total tag"
        )
    return latest[good_key] + latest[bad_key]


def _tool_life_value(alias, state, tool_life):
    """Decaying tool-life gauge; wraps back to 100 on depletion (tool change)."""
    if alias not in tool_life:
        tool_life[alias] = 100.0
    if state == "active":
        tool_life[alias] = max(0, tool_life[alias] - 0.006)
        if tool_life[alias] <= 0:
            tool_life[alias] = 100.0  # tool change
    return round(tool_life[alias], 2)


def _compute_tag_value(alias, gen, state, shift, tool_life, latest):
    """Compute one tag's value for the current step (or _SKIP to omit it)."""
    if alias.endswith("_Shift"):
        return shift
    if alias.endswith("_Down"):
        return state == "down"
    if alias.endswith("_Starved"):
        return state == "idle" or (state == "down")
    if alias.endswith("_Status") and "CNC" in alias:
        return {"active": "running", "down": "alarm", "idle": "idle"}[state]
    if alias.endswith("_Status") and "WorkOrder" not in alias:
        return {"active": "running", "down": "down", "idle": "idle"}[state]
    if alias.endswith("_WorkOrderStatus"):
        return {"active": "running", "down": "paused", "idle": "idle"}[state]
    if alias.endswith("_Total") and isinstance(gen, type(None)):
        return _derived_total(alias, latest)
    if alias.endswith("_ToolLifeRemaining"):
        return _tool_life_value(alias, state, tool_life)
    if isinstance(gen, CounterGen):
        value = gen.step(state)
        latest[alias] = value
        return value
    if callable(gen):
        return gen(state)
    return _SKIP  # skip None generators we haven't handled


def _format_csv_value(dtype_str, value):
    """Format a computed value for the CSV VALUE column."""
    if dtype_str == "BOOLEAN":
        return "True" if value else "False"
    if dtype_str == "DOUBLE":
        return f"{float(value):.6f}" if isinstance(value, (int, float)) else value
    if dtype_str == "INTEGER":
        return int(value) if isinstance(value, (int, float)) else value
    return value


@contextlib.contextmanager
def _open_dataset_for_write(filepath):
    """Text writer for *filepath*: gzip (deterministic — fixed mtime, no
    embedded filename) when it ends in ``.gz``, plain UTF-8 otherwise."""
    if str(filepath).endswith(".gz"):
        with open(filepath, "wb") as raw, \
                gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=raw, mtime=0) as gz, \
                io.TextIOWrapper(gz, encoding="utf-8", newline="") as f:
            yield f
    else:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            yield f


def generate_site_csv(filename, lines_config):
    """Generate a 24-hour dataset CSV for one site.

    lines_config: list of (line_tags_dict, ShiftSchedule) tuples
    """
    # Collect all tags across all lines, paired with their schedule; each
    # line's process nodes also name their channels' units (with_engineering_units).
    all_tags = []  # (alias, data_type_str, generator, schedule)
    for tags_dict, schedule in lines_config:
        for alias, (dtype_str, gen) in with_engineering_units(dict(tags_dict)).items():
            all_tags.append((alias, dtype_str, gen, schedule))

    filepath = OUTPUT_DIR / filename
    # gzip-compressed on disk when the name ends in ``.gz`` (generate_all's
    # three site files): the sites total ~383 MB as plain CSV, which was copied
    # into the Docker build context (and every cdk.out asset staging) at synth.
    # Compressed they are ~5-10x smaller; server.sh gunzips the selected site at
    # container start. Deterministic bytes (fixed mtime, no embedded name). A
    # plain ``.csv`` name writes plain text (test fixtures).
    with _open_dataset_for_write(filepath) as f:
        writer = csv.writer(f)  # writes generated simulation data to a local file; no untrusted input  # nosemgrep: use-defusedcsv
        writer.writerow(["ALIAS", "DATA_TYPE", "TIMESTAMP_SECONDS", "TIMESTAMP_NANO_OFFSET", "QUALITY", "VALUE"])

        # Track tool life per line (for automotive)
        tool_life = {}

        # Latest emitted value per counter alias. Computed tags (e.g. *_Total)
        # derive from sibling counters emitted earlier in registration order
        # within the same step — the order is guarded, not assumed.
        latest = {}

        for step in range(NUM_STEPS):
            ts = step * INTERVAL_SEC
            for alias, dtype_str, gen, schedule in all_tags:
                state = get_line_state(step, schedule)
                value = _compute_tag_value(
                    alias, gen, state, _shift_for_ts(ts), tool_life, latest
                )
                if value is _SKIP:
                    continue
                writer.writerow([
                    alias, dtype_str, ts, 0, "GOOD",
                    _format_csv_value(dtype_str, value),
                ])

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    tag_count = len(all_tags)
    print(f"  {filename}: {tag_count} tags × {NUM_STEPS} steps = {tag_count * NUM_STEPS:,} rows, {size_mb:.1f} MB")


# ---------------------------------------------------------------------------
# Site definitions
# ---------------------------------------------------------------------------

def generate_all():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Generating 24-hour OPC UA datasets (5-second intervals)...\n")

    # --- Site 1: Milwaukee Beverage Bottling ---
    s1_sched_l1 = ShiftSchedule(active_windows=[(0, 8), (8, 16), (16, 24)], downtimes=make_downtimes(3))
    s1_sched_l2 = ShiftSchedule(active_windows=[(0, 8), (8, 16), (16, 24)], downtimes=make_downtimes(2))
    s1_sched_l3 = ShiftSchedule(active_windows=[(22, 6)], downtimes=make_downtimes(2))  # night shift
    s1_prefix = "AnyCompany/Milwaukee/Bottling"
    s1_lines = [
        (bottling_tags(s1_prefix, "Line1"), s1_sched_l1),
        (bottling_tags(s1_prefix, "Line2"), s1_sched_l2),
        (bottling_tags(s1_prefix, "Line3"), s1_sched_l3),
    ]
    generate_site_csv("site1_milwaukee_bottling.csv.gz", s1_lines)

    # --- Site 2: Detroit Automotive Parts ---
    # Start active windows from hour 0 so the OPC server doesn't cycle through
    # a long idle period before showing real data
    s2_sched_l1 = ShiftSchedule(active_windows=[(0, 8)], downtimes=make_downtimes(4))
    s2_sched_l2 = ShiftSchedule(active_windows=[(0, 8)], downtimes=make_downtimes(3))
    s2_sched_l3 = ShiftSchedule(active_windows=[(0, 8)], downtimes=make_downtimes(2))
    s2_prefix = "AnyCompany/Detroit/AutoParts"
    s2_lines = [
        (automotive_tags(s2_prefix, "Line1"), s2_sched_l1),
        (automotive_tags(s2_prefix, "Line2"), s2_sched_l2),
        (automotive_tags(s2_prefix, "Line3"), s2_sched_l3),
    ]
    generate_site_csv("site2_detroit_automotive.csv.gz", s2_lines)

    # --- Site 3: Cedar Rapids Food Processing ---
    s3_sched_l1 = ShiftSchedule(active_windows=[(4, 12), (12, 20)], downtimes=make_downtimes(2))
    s3_sched_l2 = ShiftSchedule(active_windows=[(6, 14), (14, 22)], downtimes=make_downtimes(3))
    s3_sched_l3 = ShiftSchedule(active_windows=[(4, 12), (12, 20)], downtimes=make_downtimes(2))
    s3_prefix = "AnyCompany/CedarRapids/FoodProcessing"
    s3_lines = [
        (food_tags(s3_prefix, "Line1"), s3_sched_l1),
        (food_tags(s3_prefix, "Line2"), s3_sched_l2),
        (food_tags(s3_prefix, "Line3"), s3_sched_l3),
    ]
    generate_site_csv("site3_cedarrapids_food.csv.gz", s3_lines)

    # --- Site 4: Indianapolis Precision Machining (ADR-0083) ---
    # Line3 carries CMX-441 with the deterministic degradation cycle; the
    # bearing-replacement downtime is fixed so CMMS and telemetry agree.
    generate_site_csv("site4_indianapolis_machining.csv.gz", machining_site_lines())

    print("\nDone!")


if __name__ == "__main__":
    generate_all()
