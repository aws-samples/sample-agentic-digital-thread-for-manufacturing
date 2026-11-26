# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Chart labels carry full UTC instants; the page shows them in the viewer's zone.

Answer prose now renders each UTC time in the viewer's zone, but chart labels
were bare ``HH:MM`` in UTC (the orchestrator chart rule asked for that, and
the SiteWise and i3X chart builders wrote it), so a chart said 14:17 beside
prose that said 08:17 MDT, and a label with no date cannot be converted. Now
every builder writes ISO instants ending in Z (minutes, or seconds and
fractions when needed to keep readings apart), the SiteWise title states its
window in the answer's UTC form, and the browser formats both
(frontend ``lib/answer-times.ts``, ``chat/charts.ts``).

Fully offline (no Bedrock, no AWS, no sockets).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import agents.orchestrator as orch
import agents.system_agent as sa

_T0 = int(datetime(2026, 1, 5, 11, 0, tzinfo=timezone.utc).timestamp())
_ISO_MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")


def _group(*series) -> dict:
    members = [{"label": f"ch{i}", "points": points, "partial": False, "left_out": 0}
               for i, points in enumerate(series)]
    return {"asset": "Unit A", "unit": "mm/s", "members": members}


def _points(values, start=_T0, step=60):
    return [(start + i * step, v) for i, v in enumerate(values)]


# --------------------------------------------------------------------------- #
# Orchestrator chart rule (full prompts; Quick carries no chart rule)
# --------------------------------------------------------------------------- #
def test_chart_rule_asks_for_full_utc_instants():
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        assert "labels='[\"HH:MM\",\"HH:MM\"]'" not in prompt, mode
        assert "labels='[\"2026-08-18T14:00Z\",\"2026-08-18T14:05Z\"]'" in prompt, mode
        assert ("- Each label is the reading's full UTC instant, ISO with Z (its \"time_utc\" "
                "value), never a bare HH:MM: the screen shows labels in the viewer's own zone"
                ) in prompt, mode


# --------------------------------------------------------------------------- #
# SiteWise chart builder
# --------------------------------------------------------------------------- #
def test_sitewise_labels_are_utc_minutes():
    (chart,) = sa._sitewise_group_charts(_group(_points([1.0, 2.0, 3.0])))
    assert chart["labels"] == ["2026-01-05T11:00Z", "2026-01-05T11:01Z", "2026-01-05T11:02Z"]


def test_sitewise_labels_across_dates_and_years_stay_full_instants():
    new_year = int(datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc).timestamp())
    (chart,) = sa._sitewise_group_charts(_group(_points([1.0, 2.0], start=new_year, step=3600)))
    assert chart["labels"] == ["2026-12-31T23:00Z", "2027-01-01T00:00Z"]


def test_sitewise_labels_go_to_seconds_then_fractions_to_keep_readings_apart():
    (chart,) = sa._sitewise_group_charts(_group(_points([1.0, 1.1, 1.2], step=20)))
    assert chart["labels"] == ["2026-01-05T11:00:00Z", "2026-01-05T11:00:20Z", "2026-01-05T11:00:40Z"]
    vx = [(_T0 + 5.25, 1.0), (_T0 + 65.25, 1.1)]
    vy = [(_T0 + 5.75, 2.0), (_T0 + 65.75, 2.1)]
    (chart,) = sa._sitewise_group_charts(_group(vx, vy))
    assert chart["labels"] == [
        "2026-01-05T11:00:05.250000Z", "2026-01-05T11:00:05.750000Z",
        "2026-01-05T11:01:05.250000Z", "2026-01-05T11:01:05.750000Z"]


def test_sitewise_title_states_its_window_in_the_answer_form():
    (chart,) = sa._sitewise_group_charts(_group(_points([1.0, 2.0, 3.0], step=150)))
    assert chart["title"] == "Unit A: ch0 (mm/s), 2026-01-05 11:00 UTC to 2026-01-05 11:05 UTC"


# --------------------------------------------------------------------------- #
# i3X chart builder (single channel and bucket-aligned groups)
# --------------------------------------------------------------------------- #
def _labels(block: str) -> list:
    return json.loads(re.search(r"labels='(\[[^']*\])'", block).group(1))


def test_i3x_single_channel_labels_are_utc_minutes():
    series = [(f"2026-04-30T07:{m:02d}:00Z", 10.0 + m) for m in range(3)]
    (block,) = sa._render_chart_blocks("AssetOne", [("prop-1", series)], 100)
    assert _labels(block) == ["2026-04-30T07:00Z", "2026-04-30T07:01Z", "2026-04-30T07:02Z"]


def test_i3x_label_from_an_offset_timestamp_is_converted_to_utc():
    series = [("2026-04-30T09:00:00+02:00", 1.0), ("2026-04-30T09:01:00+02:00", 2.0)]
    (block,) = sa._render_chart_blocks("AssetOne", [("prop-1", series)], 100)
    assert _labels(block) == ["2026-04-30T07:00Z", "2026-04-30T07:01Z"]


def test_i3x_bucket_aligned_labels_are_utc_minutes():
    series = [(f"2026-04-30T07:{m:02d}:00Z", 10.0 + m) for m in range(10)]
    channels = [("Bearing temp 1", series), ("Bearing temp 2", series)]
    (block,) = sa._render_chart_blocks("AssetOne", channels, 100)
    labels = _labels(block)
    assert labels and all(_ISO_MINUTE.match(label) for label in labels), labels
    assert labels[0].startswith("2026-04-30T07:0")
