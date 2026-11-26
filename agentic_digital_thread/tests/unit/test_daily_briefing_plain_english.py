# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pinned Daily Briefing asks in business terms, not in queries.

The briefing is a demo prompt anyone can open, and it runs against whichever
systems a deployment has onboarded (the corporate CMMS).
It says what each section shows and how to rank it, so the report has one
right answer; working out which system holds the data, and how to ask it, is
the platform's job. So the text names no system, table, column, status code
or SQL.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import pytest  # noqa: E402

from agents.demo import prompt_pack  # noqa: E402

_TITLES = ("PRODUCTION KPIs", "LINE-BY-LINE STATUS", "TOP 5 UNPLANNED DOWNTIME EVENTS",
           "QUALITY SUMMARY", "OPEN MAINTENANCE WORK ORDERS", "CUSTOMER OPEN ORDERS (TOP 5 BY VALUE)",
           "HOURLY OUTPUT TREND CHART")

#: What a query or a schema looks like inside prose.
_QUERY_SHAPES = {
    "SQL clause": r"(?i)\b(?:SELECT|WHERE|GROUP BY|ORDER BY|JOIN|LIMIT \d|INTERVAL|NULLS LAST)\b|\bFROM [a-z]",
    "SQL function": r"(?i)\b(?:COALESCE|NULLIF|ROUND|SUM|AVG|COUNT|EXTRACT|date_trunc|NOW)\s*\(",
    "CASE expression": r"(?i)\bCASE\s+\w+\s+WHEN\b",
    "identifier": r"\b[A-Za-z0-9]+_[A-Za-z0-9_]+\b",
    "camelCase identifier": r"\b[a-z]+[A-Z][A-Za-z0-9]*\b",
    "dotted identifier": r"\b[a-z_]+\.[a-z_]+\b",
    "quoted code value": r"'[A-Za-z_]+'",
    "comparison": r"[<>]=?\s*\d|(?i:\bIN \()",
    "system name": r"(?i)\b(?:MES|CMMS|ERP|PLM|SiteWise|HighByte|UNS)\b",
    "schema word": r"(?i)\b(?:view|column|schema)s?\b",
}


def _briefing() -> dict:
    (briefing,) = [p for p in prompt_pack.DEMO_PROMPTS if p["title"] == "Daily Briefing Report"]
    return briefing


def _sections(text: str) -> dict[int, str]:
    heads = list(re.finditer(r"^\*\*(\d)\. (.+?)\*\*", text, re.MULTILINE))
    out = {}
    for head, nxt in zip(heads, heads[1:] + [None]):
        out[int(head.group(1))] = text[head.start():nxt.start() if nxt else len(text)]
    return out


@pytest.mark.parametrize("shape", sorted(_QUERY_SHAPES))
def test_the_briefing_text_has_no_query_or_schema_in_it(shape):
    hits = re.findall(_QUERY_SHAPES[shape], _briefing()["text"])
    assert not hits, f"{shape}: {hits}"


@pytest.mark.parametrize("shape, sample", [
    ("SQL clause", "select status from orders order by priority"),
    ("SQL function", "sum(actual) per plant"),
    ("camelCase identifier", "sorted by createdDate"),
    ("dotted identifier", "read work_orders.status"),
    ("system name", "ask the cmms first"),
    ("schema word", "the Status Column"),
])
def test_the_shape_checks_catch_lower_case_and_other_spellings(shape, sample):
    assert re.search(_QUERY_SHAPES[shape], sample), (shape, sample)


def test_section_4_pools_yield_instead_of_averaging_rates():
    """Checks have different sample sizes, so a pooled yield and an average of
    each check's rate differ; the section says which one it wants."""
    s4 = _sections(_briefing()["text"])[4]
    assert "pooled across every check rather than an average of each check's own rate" in s4
    assert "every one on record" in s4 and "pooled the same way" in s4


def test_no_heading_word_reads_as_an_undefined_metric_term(monkeypatch):
    """An all-caps display word in a heading ('TABLE') was picked up as an
    unknown acronym in a metric request, which makes the app tell the model to
    call it undefined and ask which definition is meant. The briefing's only
    metric-shaped term must be one the code vocabulary defines."""
    import agents.metric_definition_store as store
    from agents import metric_term_resolution as terms
    store.invalidate()
    # A successful read of a deployment with no graph-defined metrics: the
    # code-defined vocabulary alone (test_metric_term_resolution's fixture).
    monkeypatch.setattr(store, "neptune_query",
                        lambda *_a, **_k: [{"id": None, "definition_json": None}])
    try:
        text = _briefing()["text"]
        assert terms.extract_metric_term_candidates(text) == ["Actual Quantity"]
        assert terms.unresolved_metric_terms(text) == []
    finally:
        store.invalidate()


def test_the_briefing_keeps_seven_sections_in_order_each_once():
    text = _briefing()["text"]
    heads = re.findall(r"^\*\*(\d)\. (.+?)\*\*", text, re.MULTILINE)
    assert heads == [(str(n), title) for n, title in enumerate(_TITLES, start=1)]
    assert "EXACTLY 7 numbered sections" in text


def test_section_5_says_which_orders_count_as_open_and_how_to_rank_them():
    """The before-and-after comparison rests on section 5 having one right
    answer: open or in progress (cancelled orders are not open), priority from
    critical down, and the oldest first among orders of the same priority."""
    s5 = _sections(_briefing()["text"])[5]
    assert "five highest-priority" in s5
    assert "open or in progress" in s5 and "not completed, closed or cancelled" in s5
    assert "Critical first, then high, medium and low" in s5
    assert "within a priority, oldest first" in s5
    assert "Work Order | Asset |" in s5


def test_the_ranked_sections_break_ties_in_words():
    """Downtime events are mostly the same length (many end at the same
    cap), so "the five longest" alone has many right answers; the same holds
    for work orders of the same priority. The tie-break applies to ONE list
    across the three plants, not to each plant's own list."""
    sections = _sections(_briefing()["text"])
    assert "leaving out product changeovers" in sections[3]
    assert "among events of the same length the most recent first" in sections[3]
    assert "all three plants together as ONE list" in sections[3]
    assert "first five of that combined list" in sections[3]
    assert "within a priority, oldest first" in sections[5]


def test_the_kpi_sections_count_every_work_order_and_ask_for_defined_metrics():
    """Sections 1 and 2 total every work order on record, not only today's,
    and ask for 'actual quantity' (a defined all-time metric). Planned output
    is gone: no metric defines it, so the answer could only report it undefined."""
    text = _briefing()["text"]
    sections = _sections(text)
    for n in (1, 2):
        assert "every one on record, not only today's" in sections[n], n
        assert "actual quantity (units produced)" in sections[n], n
        assert "Actual Quantity" in sections[n], n
        assert "Planned" not in sections[n] and "units planned" not in sections[n], n
        assert "output" not in sections[n].lower(), n
    assert "Plant | OEE | Actual Quantity | Scrap Rate." in sections[1]
    assert "Line | Plant | Status | OEE | Actual Quantity," in sections[2]
    # The 24-hour 'Output' wording belongs to section 7 only.
    assert "Output EXACTLY" not in text and "CRITICAL FORMAT RULES" in text


def test_the_briefing_still_needs_the_same_systems():
    assert _briefing()["required_systems"] == ["erp", "mes-mil", "mes-det", "mes-cr", "cmms"]
