# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the fourth OPC UA site in the dataset generator
(``highbyte/generate_datasets.py``, ADR-0083).

The fourth site's Line3 mill is the story asset of the seeded defect chain.
Its telemetry used to reach SiteWise only through the ``lambda-sitewise-feed``
backfill; the generator now carries the same shape LIVE and deterministically:

  * vibration climbs healthy -> alarm across the loop until a FIXED
    bearing-replacement downtime, reads ~0 while the mill is stopped, and runs
    at the healthy baseline after the repair;
  * coolant temperature follows the same cycle;
  * the trend is monotone non-decreasing before the repair (pinned on the pure
    ``cmx441_trend`` function AND on the emitted CSV, jitter bounded);
  * identity tags name the SiteWise asset, the MES equipment id and the CMMS
    asset number so the graph can DISCOVER the cross-system identity;
  * the CNC-model property names the SiteWise ``CNC`` model expects are all
    present, so the same asset can be fed without changing onboarding;
  * two generations of the same window are byte-identical.

Deliberately literal-free about the city (domain-neutrality ratchet scans
tests/): the site is addressed through the generator's own constants.
"""
import csv
import importlib.util
from pathlib import Path

import pytest

_GENERATOR_PATH = (
    Path(__file__).resolve().parents[3] / "highbyte" / "generate_datasets.py"
)
_MODELS_PATH = (
    Path(__file__).resolve().parents[3] / "highbyte" / "DeploySitewiseModels.py"
)

_TINY_NUM_STEPS = 80
_TINY_DOWNTIME = (60, 10)  # bearing replacement at step 60 for 10 steps


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_datasets_site4_under_test", str(_GENERATOR_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gen_mod():
    mod = _load_generator()
    _shrink(mod)
    return mod


def _shrink(mod):
    """Tiny window: make_downtimes() assumes the full loop (randint up to
    NUM_STEPS - 200), so the two non-story lines get no random downtime."""
    mod.NUM_STEPS = _TINY_NUM_STEPS
    mod.CMX441_DOWNTIME = _TINY_DOWNTIME
    mod.make_downtimes = lambda *a, **k: []


def _story_line(gen_mod):
    """The (tags, schedule) pair of the line that carries the story asset."""
    lines = gen_mod.machining_site_lines()
    assert len(lines) == 3
    story = [(t, s) for t, s in lines if s.downtimes == [_TINY_DOWNTIME]]
    assert len(story) == 1, "exactly one line carries the fixed bearing downtime"
    return story[0]


def _generate(gen_mod, tmp_path, filename="site4.csv"):
    gen_mod.OUTPUT_DIR = tmp_path
    gen_mod.generate_site_csv(filename, gen_mod.machining_site_lines())
    series = {}
    with open(tmp_path / filename, newline="") as f:
        for row in csv.DictReader(f):
            series.setdefault(row["ALIAS"], []).append(row["VALUE"])
    return series


def _alias(tags, suffix):
    matches = [a for a in tags if a.endswith(suffix)]
    assert len(matches) == 1, (suffix, matches)
    return matches[0]


# ---------------------------------------------------------------------------
# Pure trend
# ---------------------------------------------------------------------------

def test_trend_is_monotone_then_alarm_then_healthy(gen_mod):
    fail, dur = _TINY_DOWNTIME
    healthy, alarm = gen_mod.VIB_HEALTHY_MM_S, gen_mod.VIB_ALARM_MM_S
    values = [gen_mod.cmx441_trend(s, healthy, alarm) for s in range(_TINY_NUM_STEPS)]
    assert values[0] == healthy
    for s in range(1, fail):
        assert values[s] >= values[s - 1], f"trend decreased at step {s}"
    assert values[fail - 1] == pytest.approx(alarm, rel=0.02)
    assert all(v == alarm for v in values[fail:fail + dur])
    assert all(v == healthy for v in values[fail + dur:])


def test_story_numbers_follow_the_seed():
    """Alarm 2.8 mm/s and post-repair ~1 mm/s are the seeded story
    (cmms work order 1195); coolant 68 -> 84 mirrors the feed handler."""
    mod = _load_generator()
    assert mod.VIB_ALARM_MM_S == 2.8
    assert 0.9 <= mod.VIB_HEALTHY_MM_S <= 1.3
    assert (mod.COOLANT_HEALTHY_C, mod.COOLANT_ALARM_C) == (68.0, 84.0)
    assert mod.CMX441_ASSET_NAME == "5-Axis-Milling-CMX-441"
    assert mod.CMX441_MES_EQUIPMENT_ID == "L3-M1"
    assert mod.CMX441_CMMS_ASSET_NUM == "1001-CMX-441"


def test_fixed_downtime_is_inside_the_loop():
    """The bearing replacement must complete before the dataset wraps, or the
    post-repair healthy segment (the reset) would never be emitted."""
    mod = _load_generator()
    fail, dur = mod.CMX441_DOWNTIME
    assert 0 < fail < mod.NUM_STEPS
    assert fail + dur < mod.NUM_STEPS


# ---------------------------------------------------------------------------
# Emitted dataset
# ---------------------------------------------------------------------------

def test_site_has_three_lines_and_one_story_mill(gen_mod):
    tags, _sched = _story_line(gen_mod)
    assert _alias(tags, "CNC1_AssetName")
    assert _alias(tags, "CNC1_EquipmentID")
    assert _alias(tags, "CNC1_MaintenanceAssetNum")
    others = [t for t, s in gen_mod.machining_site_lines() if s.downtimes != [_TINY_DOWNTIME]]
    for other in others:
        assert not [a for a in other if a.endswith("_AssetName")], (
            "only the story mill carries the SiteWise asset identity"
        )


def test_story_mill_carries_every_cnc_model_property(gen_mod):
    """Every measurement of the SiteWise CNC model has a matching tag, so the
    pipeline can feed the existing asset without an onboarding change."""
    ns = {}
    exec(  # noqa: S102 - our own repo file, read to extract the CNC property list  # nosec B102
        _MODELS_PATH.read_text(encoding="utf-8").split("def wait_for_active")[0], ns
    )
    cnc_props = {p["name"] for p in ns["PROCESS_MEASUREMENTS"]["CNC"]}
    tags, _ = _story_line(gen_mod)
    cnc_tags = {a.rsplit("CNC1_", 1)[1] for a in tags if "CNC1/" in a}
    missing = cnc_props - cnc_tags
    assert not missing, f"CNC model properties without a tag: {sorted(missing)}"


def test_emitted_vibration_climbs_then_stops_then_resets(gen_mod, tmp_path):
    fail, dur = _TINY_DOWNTIME
    tags, _ = _story_line(gen_mod)
    series = _generate(gen_mod, tmp_path)
    vib_x = [float(v) for v in series[_alias(tags, "CNC1_Vibration_X")]]
    assert len(vib_x) == _TINY_NUM_STEPS

    healthy, alarm = gen_mod.VIB_HEALTHY_MM_S, gen_mod.VIB_ALARM_MM_S
    # Jitter is bounded (+-2 %), so every pre-failure sample sits on the trend.
    for s in range(fail):
        trend = gen_mod.cmx441_trend(s, healthy, alarm)
        assert abs(vib_x[s] - trend) <= trend * 0.02 + 1e-9, f"step {s}"
    # Monotone at the coarse grain (jitter cannot hide the climb).
    coarse = vib_x[0:fail:10]
    assert coarse == sorted(coarse)
    assert vib_x[fail - 1] > 2.5  # at the alarm level right before the stop
    # Stopped for the bearing replacement: spindle not turning.
    assert all(v < 0.05 for v in vib_x[fail:fail + dur])
    # Post-repair: back at the healthy baseline.
    assert all(abs(v - healthy) <= healthy * 0.02 + 1e-9 for v in vib_x[fail + dur:])


def test_axes_scale_and_coolant_follow_the_cycle(gen_mod, tmp_path):
    fail, dur = _TINY_DOWNTIME
    tags, _ = _story_line(gen_mod)
    series = _generate(gen_mod, tmp_path)
    x = [float(v) for v in series[_alias(tags, "CNC1_Vibration_X")]]
    y = [float(v) for v in series[_alias(tags, "CNC1_Vibration_Y")]]
    z = [float(v) for v in series[_alias(tags, "CNC1_Vibration_Z")]]
    # Y/Z track X lower, like the feed handler's axis scaling.
    for s in range(fail):
        assert y[s] < x[s] and z[s] < x[s], f"step {s}"
    temp = [float(v) for v in series[_alias(tags, "CNC1_CoolantTemp")]]
    assert temp[0] == pytest.approx(gen_mod.COOLANT_HEALTHY_C, rel=0.01)
    assert temp[fail - 1] == pytest.approx(gen_mod.COOLANT_ALARM_C, rel=0.03)
    assert temp[fail + dur] == pytest.approx(gen_mod.COOLANT_HEALTHY_C, rel=0.01)
    status = series[_alias(tags, "CNC1_Status")]
    assert set(status[fail:fail + dur]) == {"alarm"}
    assert status[fail - 1] == "running" and status[fail + dur] == "running"


def test_story_identity_tags_are_constant(gen_mod, tmp_path):
    tags, _ = _story_line(gen_mod)
    series = _generate(gen_mod, tmp_path)
    assert set(series[_alias(tags, "CNC1_AssetName")]) == {gen_mod.CMX441_ASSET_NAME}
    assert set(series[_alias(tags, "CNC1_EquipmentID")]) == {gen_mod.CMX441_MES_EQUIPMENT_ID}
    assert set(series[_alias(tags, "CNC1_MaintenanceAssetNum")]) == {gen_mod.CMX441_CMMS_ASSET_NUM}


# ---------------------------------------------------------------------------
# Edge-FFT features (ADR-0073): the band alarm leads the stop
# ---------------------------------------------------------------------------

def test_every_mill_publishes_the_spectral_features(gen_mod):
    expected = set(gen_mod.SPECTRAL_TAGS) | {"BPFO_Alarm", "BPFO_Hz", "BPFI_Hz"}
    for tags, _sched in gen_mod.machining_site_lines():
        suffixes = {a.rsplit("CNC1_", 1)[1] for a in tags if "CNC1/" in a}
        assert expected <= suffixes, sorted(expected - suffixes)


def test_story_mill_band_alarm_fires_before_the_stop_and_clears(gen_mod, tmp_path):
    fail, dur = _TINY_DOWNTIME
    alarm_step = gen_mod.condition_monitor.bpfo_alarm_step(_TINY_DOWNTIME)
    tags, _ = _story_line(gen_mod)
    series = _generate(gen_mod, tmp_path)
    alarm = [v == "True" for v in series[_alias(tags, "CNC1_BPFO_Alarm")]]
    assert len(alarm) == _TINY_NUM_STEPS
    assert not any(alarm[:alarm_step]), "alarm must stay clear while the band is under threshold"
    assert all(alarm[alarm_step:fail]), "alarm must hold from the crossing until the mill stops"
    assert not any(alarm[fail:]), "no alarm while stopped or after the repair"
    # The RMS story alarm (2.8 mm/s) has NOT been reached when the band crosses.
    vib_x = [float(v) for v in series[_alias(tags, "CNC1_Vibration_X")]]
    assert vib_x[alarm_step] < gen_mod.VIB_ALARM_MM_S * 0.95


def test_story_mill_bpfo_band_climbs_then_stops_then_resets(gen_mod, tmp_path):
    fail, dur = _TINY_DOWNTIME
    tags, _ = _story_line(gen_mod)
    series = _generate(gen_mod, tmp_path)
    bpfo = [float(v) for v in series[_alias(tags, "CNC1_Spectrum_BPFO")]]
    healthy = gen_mod.condition_monitor.FEATURES["bpfo"][0]
    assert bpfo[0] == pytest.approx(healthy, rel=0.05)
    assert bpfo[fail - 1] > gen_mod.condition_monitor.BPFO_ALARM_MM_S
    assert all(v == 0.0 for v in bpfo[fail:fail + dur]), "spindle stopped: no band energy"
    assert all(abs(v - healthy) <= healthy * 0.05 for v in bpfo[fail + dur:])
    # Roughly monotone: jitter is +-3 %, so compare against a smoothed neighbour.
    for s in range(8, fail):
        assert bpfo[s] >= bpfo[s - 8] * 0.97, f"BPFO band fell at step {s}"


def test_other_mills_never_alarm(gen_mod, tmp_path):
    series = _generate(gen_mod, tmp_path)
    for tags, sched in gen_mod.machining_site_lines():
        if sched.downtimes == [_TINY_DOWNTIME]:
            continue  # the story mill
        alarm = series[_alias(tags, "CNC1_BPFO_Alarm")]
        assert set(alarm) == {"False"}
        bpfo = [float(v) for v in series[_alias(tags, "CNC1_Spectrum_BPFO")] if float(v) > 0]
        assert max(bpfo) < gen_mod.condition_monitor.BPFO_ALARM_MM_S


def test_defect_frequency_tags_are_constant_while_running(gen_mod, tmp_path):
    tags, _ = _story_line(gen_mod)
    series = _generate(gen_mod, tmp_path)
    running = {float(v) for v in series[_alias(tags, "CNC1_BPFO_Hz")] if float(v) > 0}
    assert running == {gen_mod.condition_monitor.BPFO_HZ}


# ---------------------------------------------------------------------------
# MaterialLot tags name the one corporate ERP's seeded raw-material lots
# ---------------------------------------------------------------------------

_DB_INIT_PATH = Path(__file__).resolve().parents[3] / "lambda-db-init" / "handler.py"


def _erp_seed_raw_lots():
    """Lot numbers db-init seeds for the machining site's raw items, read from
    the module-level ``*_RAW_ITEMS`` list (ast; no database-driver import) and
    formatted the way its supply seeder formats them."""
    import ast

    tree = ast.parse(_DB_INIT_PATH.read_text(encoding="utf-8"))
    codes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.endswith("_RAW_ITEMS") for t in node.targets
        ):
            codes += [row[0] for row in ast.literal_eval(node.value)]
    assert codes, "no *_RAW_ITEMS list found in lambda-db-init/handler.py"
    return {f"LOT-IND-{code.replace('-', '')}-SEED" for code in codes}


def test_material_lot_tags_are_the_erp_seeded_raw_lots(gen_mod):
    lots = _erp_seed_raw_lots()
    emitted = {}
    for tags, sched in gen_mod.machining_site_lines():
        alias = _alias(tags, "_MaterialLot")
        emitted[alias] = {tags[alias][1](st) for st in ("active", "idle", "down")}
        assert len(emitted[alias]) == 1, "a line's MaterialLot is a constant"
        if sched.downtimes == [_TINY_DOWNTIME]:
            assert emitted[alias] == {gen_mod.CMX441_MATERIAL_LOT}
    values = set().union(*emitted.values())
    assert values <= lots, sorted(values - lots)
    assert len(values) == len(emitted), "each line names a distinct lot"


# ---------------------------------------------------------------------------
# MaterialID tags name the PLM part family each line machines
# ---------------------------------------------------------------------------

_PLM_SQL_PATH = Path(__file__).resolve().parents[3] / "lambda-adt-seed" / "005_plm_engineering.sql"
#: Machined SKUs the corporate ERP sells that no OPC UA line machines: the
#: hydraulic manifold (frozen PLM margin, D17) runs on the MES L7-M2 cell and
#: shares its bar-stock lot with the precision shaft, so it is excluded from
#: the lot -> family map rather than given a fourth generator line.
_NOT_ON_AN_OPC_LINE = frozenset({"MACH-MNFD-5520"})


def _machined_family_by_raw_lot():
    """raw-lot tag value -> expected MaterialID ``<plm-family-slug>-<core>``.

    Chain: db-init BOM (machined SKU consumes a raw item) -> db-init
    ``PLM_PART_XREF`` (SKU -> PLM item) -> PLM seed family name."""
    import ast
    import re

    text = _DB_INIT_PATH.read_text(encoding="utf-8")
    xref = next(ast.literal_eval(n.value) for n in ast.parse(text).body
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "PLM_PART_XREF" for t in n.targets))
    names = dict(re.findall(r"\('(PN-[\w-]+)','([^']+)','[^']*','Part'", _PLM_SQL_PATH.read_text()))
    raw_by_sku = dict(re.findall(r"\('(MACH-[A-Z]+-\d+)', '([A-Z]+-[A-Z0-9]+-\d+)', [\d.]+, 'KG'", text))
    assert raw_by_sku, "no machined BOM raw-stock rows found in lambda-db-init/handler.py"
    assert _NOT_ON_AN_OPC_LINE <= set(raw_by_sku), "exclusion names a SKU db-init no longer seeds"
    out = {}
    for sku, raw in sorted(raw_by_sku.items()):
        if sku in _NOT_ON_AN_OPC_LINE:
            continue
        core = re.search(r"\d+$", sku).group(0)
        slug = names[xref[sku]].lower().replace(" ", "-")
        lot = f"LOT-IND-{raw.replace('-', '')}-SEED"
        assert lot not in out, f"two line parts share raw lot {lot}: {out[lot]} and {slug}-{core}"
        out[lot] = f"{slug}-{core}"
    return out


def test_material_id_tags_name_the_plm_family_of_the_line_part(gen_mod):
    expected = _machined_family_by_raw_lot()
    seen = []
    for tags, _sched in gen_mod.machining_site_lines():
        mat = {tags[_alias(tags, "_MaterialID")][1](st) for st in ("active", "idle", "down")}
        lot = tags[_alias(tags, "_MaterialLot")][1]("active")
        assert mat == {expected[lot]}, (lot, mat)
        seen.append(expected[lot])
    assert len(set(seen)) == 3, "each line machines a distinct part family"


def test_the_manifold_family_names_no_generator_line(gen_mod):
    """The excluded SKU's PLM family (the Hydraulic Manifold Block in 005) is
    no line's MaterialID: the generator stays three lines, one per part."""
    import re

    names = set(re.findall(r"\('PN-5520-REV-[A-Z]','([^']+)','[^']*','Part'", _PLM_SQL_PATH.read_text()))
    assert names == {"Hydraulic Manifold Block"}
    slug = "hydraulic-manifold-block-5520"
    for tags, _sched in gen_mod.machining_site_lines():
        assert tags[_alias(tags, "_MaterialID")][1]("active") != slug


def test_dataset_is_reproducible(tmp_path):
    outputs = []
    for i in range(2):
        mod = _load_generator()  # module exec re-seeds random
        _shrink(mod)
        mod.OUTPUT_DIR = tmp_path
        mod.generate_site_csv(f"run{i}.csv", mod.machining_site_lines())
        outputs.append((tmp_path / f"run{i}.csv").read_bytes())
    assert outputs[0] == outputs[1]
