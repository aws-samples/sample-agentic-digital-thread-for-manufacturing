# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The freshness sentinels the SFC watchdog reads (``sfc_config.freshness_sentinels``).

One channel per SFC source whose stored value must change on every OPC UA
server tick, so "no new value for five minutes" can only mean SFC stopped
delivering that source. SFC's OPC UA subscription forwards a value only when
it changes, so a channel that holds a value (a machine stopped for a repair,
a counter while a line is idle) is not a sentinel. The sentinels are derived
from the same tag map and generator functions the server's datasets come
from, and checked here against those datasets row by row.
"""
from __future__ import annotations

import csv
import gzip
import importlib.util
from pathlib import Path

import pytest

from agents.demo import sfc_config as gen

STALE_ROWS = 60       # five minutes at the server's 5 s tick


@pytest.fixture(scope="module")
def sentinels():
    return gen.freshness_sentinels()


@pytest.fixture(scope="module")
def datasets_dir(tmp_path_factory):
    """The four site datasets, generated exactly as deploy.sh generates them."""
    spec = importlib.util.spec_from_file_location("sentinel_dataset_gen", gen._DATASETS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path_factory.mktemp("datasets")
    module.OUTPUT_DIR = out
    module.generate_all()
    return out


def _series(datasets_dir: Path, aliases: set[str]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {a: [] for a in aliases}
    for path in sorted(datasets_dir.glob("*.csv.gz")):
        with gzip.open(path, "rt", newline="") as fh:
            for row in csv.reader(fh):
                if row and row[0] in values:
                    values[row[0]].append(row[5])
    return values


def _repeats(series: list[str]) -> tuple[int, int]:
    """(rows equal to the row before, wrap included; the longest run of one value)."""
    equal = sum(1 for i in range(len(series)) if series[i] == series[i - 1])
    best = run = 1
    for prev, cur in zip(series, series[1:]):
        run = run + 1 if cur == prev else 1
        best = max(best, run)
    return equal, best


def test_one_sentinel_per_live_source_and_none_for_the_history_replay(sentinels):
    assert [s.source for s in sentinels] == [site.key for site in gen.SITES]
    assert gen.HISTORY_SOURCE_ID not in {s.source for s in sentinels}


def test_each_sentinel_is_a_double_channel_sfc_writes_to_sitewise(sentinels):
    channels = {(c.site, c.asset_external_id, c.prop): c for c in gen.resolve_channels()}
    config = gen.generate_sfc_config(gen.placeholder_endpoints(), region="${AWS_REGION}",
                                     table_bucket="b", namespace="n", table_name="t")
    (sitewise,) = [t for t in config["Targets"].values() if t["TargetType"] == "AWS-SITEWISE"]
    written = {(a["AssetExternalId"], p["PropertyName"]) for a in sitewise["Assets"] for p in a["Properties"]}
    for s in sentinels:
        channel = channels[(s.source, s.asset_external_id, s.prop)]
        assert channel.dtype == "DOUBLE"
        assert channel.alias == s.alias
        assert (s.asset_external_id, s.prop) in written


def test_no_sentinel_sits_on_the_story_mill(sentinels):
    mill = {c.asset_external_id for c in gen.resolve_channels()
            if c.site == gen.STORY_SITE_KEY and c.node == gen.STORY_MILL_NODE}
    assert mill and not mill & {s.asset_external_id for s in sentinels}


def test_each_sentinel_changes_on_every_row_of_the_generated_datasets(sentinels, datasets_dir):
    series = _series(datasets_dir, {s.alias for s in sentinels})
    for s in sentinels:
        values = series[s.alias]
        assert len(values) == gen.dataset_rows()
        assert _repeats(values) == (0, 1), s


def test_the_probe_rejects_a_channel_that_holds_its_value(datasets_dir):
    """The story mill's vibration holds one value for the whole repair stop
    (longer than five minutes), so SFC stores nothing then: the probe must
    refuse it, and the datasets show why."""
    (vibration,) = [c for c in gen.resolve_channels()
                    if c.site == gen.STORY_SITE_KEY and c.node == gen.STORY_MILL_NODE
                    and c.prop == "Vibration_X"]
    datasets = gen._load_by_path("sentinel_probe_datasets", gen._DATASETS_PATH)
    tags = {a: t for site in gen.SITES if site.key == gen.STORY_SITE_KEY
            for _line, line_tags in site.line_tags(datasets) for a, t in line_tags.items()}
    dtype, generator = tags[vibration.alias]
    assert gen.changes_every_row(datasets, vibration.alias, dtype, generator) is False
    _equal, longest = _repeats(_series(datasets_dir, {vibration.alias})[vibration.alias])
    assert longest > STALE_ROWS


def test_the_probe_leaves_the_random_state_alone():
    import random
    before = random.getstate()
    gen.freshness_sentinels()
    assert random.getstate() == before


def test_dataset_rows_is_the_generators_loop():
    datasets = gen._load_by_path("sentinel_rows_datasets", gen._DATASETS_PATH)
    assert gen.dataset_rows() == datasets.NUM_STEPS
