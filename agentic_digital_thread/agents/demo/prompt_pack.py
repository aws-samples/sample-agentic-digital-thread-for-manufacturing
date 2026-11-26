# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo prompt pack (demo-only content).

These prompts and CLI questions are DEMO-ONLY curated content, gated by the
``ADT_DEMO`` environment flag (see ``agents.demo.is_enabled`` and ADR 0019). The
core runs, imports, and passes tests with the demo disabled.

``DEMO_PROMPTS`` is the curated registry pack moved verbatim from the former
``agents.prompts._REGISTRY``. ``DEMO_QUESTIONS`` mirrors the prior CLI fallback
(the former ``agents.main._FALLBACK_DEMO_QUESTIONS``).

``required_systems`` entries are CANONICAL catalog ids per ``agents.demo.systems``
(``erp``, ``mes-mil``/``mes-det``/``mes-cr``, ``cmms``, ``plm``) so the
availability filter in ``agents.prompts`` (``_filter_satisfiable``) is a clean
set check. The deployment-dependent OT/UNS systems stay TYPE-shaped
(``"SiteWise"``, ``"IIoT"``, ``"Historian"``) because their system ids are only
known at runtime (onboarded at deploy time, not in the demo seam) — the filter's
id-OR-type match handles them either way.

``WHAT_THIS_SHOWS`` is the demo's closing interpretation ask. The core chart
rule stopped asking for post-result analysis (it made answers narrate the app's
own chart plumbing), so the demo prompts that end on data a presenter has to
explain ask for it themselves. It defers to the core CAUSE rule: a likely cause
is labelled an inference and backed by evidence, never stated as recorded.
"""

#: Closing instruction appended to the demo prompts whose results need a
#: presenter-facing reading. Pinned in tests/unit/test_prompts_demo_gate.py.
#: Not shown under RESPONSE_INTEGRITY_MODE=enforce, which renders only the
#: Answer and Freshness and limitations sections (response_integrity_rendering).
WHAT_THIS_SHOWS = (
    "\n\nEnd with a short 'What this shows' section interpreting the results. "
    "Label any likely cause as an inference and name the evidence it rests on; "
    "if the results do not point to a cause, say so."
)

#: Titles of the prompts that carry ``WHAT_THIS_SHOWS``. Kept here (the demo
#: home) so the test that pins them carries no demo literals.
INTERPRETED_TITLES = (
    "Filler Pump Outage Review",
    "Milwaukee Filler Pump Health",
    "Supplier Quality Traceability",
    "Equipment Health with Cross-System Correlation",
    "Downtime Root Cause",
)

DEMO_PROMPTS = [
    # ── Plant Manager ──────────────────────────────────────────────────────
    {
        "title": "Daily Briefing Report",
        "category": "cross-system",
        "required_systems": ["erp", "mes-mil", "mes-det", "mes-cr", "cmms"],
        "persona": "Plant Manager",
        "pinned": True,
        "display_text": "Generate a daily production meeting report in SQCDP format for Milwaukee, Detroit and Cedar Rapids",
        "text": "Generate a daily production meeting report in SQCDP format for all three plants (Milwaukee, Detroit, Cedar Rapids).\n\nCRITICAL FORMAT RULES (read before writing anything):\n- Write EXACTLY 7 numbered sections, each appearing EXACTLY ONCE.\n- Gather the data for every section first, asking for independent pieces in parallel, then write each section once.\n- Do NOT add commentary, transitions, or re-state results between sections.\n- After writing section 7, STOP. No follow-up questions, no summaries.\n\n**1. PRODUCTION KPIs**: for each plant, across ALL of its production work orders (every one on record, not only today's): OEE (the average of its work orders' OEE, to one decimal), actual quantity (units produced) and scrap rate (rejected units as a percentage of actual quantity, to one decimal). ONE table: Plant | OEE | Actual Quantity | Scrap Rate. One row per plant, 3 rows total.\n\n**2. LINE-BY-LINE STATUS**: every production line at the three plants (3 per plant, 9 in all): the line's current status, then across ALL of that line's production work orders (every one on record, not only today's) the average OEE (to one decimal) and the actual quantity (units produced). ONE table: Line | Plant | Status | OEE | Actual Quantity, lowest OEE first. Exactly 9 rows, each line once.\n\n**3. TOP 5 UNPLANNED DOWNTIME EVENTS**: the five longest downtime events across the three plants, leaving out product changeovers (they're planned). An event that hasn't ended yet counts up to now. Rank the events of all three plants together as ONE list: longest first, and among events of the same length the most recent first. Then take the first five of that combined list, whichever plants they come from. ONE table: Rank | Reason | Line | Duration (min) | Category. If there are none, write 'No downtime events recorded' and move on.\n\n**4. QUALITY SUMMARY**: for each plant: first-pass yield (all units that passed its quality checks as a percentage of all units checked, pooled across every check rather than an average of each check's own rate, to one decimal), scrap units (rejected units across ALL its production work orders, every one on record), first-pass yield over the last 7 days (pooled the same way), and how that 7-day yield compares with the 95% target. ONE table: Plant | FPY (%) | Scrap Units | 7-Day FPY | vs Target.\n\n**5. OPEN MAINTENANCE WORK ORDERS**: the five highest-priority maintenance work orders at the three plants that are still open or in progress (not completed, closed or cancelled). Critical first, then high, medium and low; within a priority, oldest first. ONE table: Work Order | Asset | Issue | Priority | Status | Plant.\n\n**6. CUSTOMER OPEN ORDERS (TOP 5 BY VALUE)**: the five customer orders with the highest total value that haven't shipped yet (not shipped, delivered or cancelled). ONE table: Order | Customer | Status | Order Date | Total.\n\n**7. HOURLY OUTPUT TREND CHART**: units produced at each plant in each hour of the last 24 hours, counting each production work order's output in the hour it started. Render as ONE multi-line Chart.js chart, one line per plant. X-axis: hour (HH:MM), Y-axis: units produced.\n\nStatus indicators: 🟢 on/above target, 🟡 within 5%, 🔴 below target. OEE target: 85%.",
    },
    {
        "title": "Plant Attainment vs Target",
        "category": "cross-system",
        "required_systems": ["erp", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Plant Manager",
        "text": "Compare production attainment against order targets across all three plants. Which lines are behind schedule and what is the impact on open customer orders?",
    },
    {
        "title": "Cross-Plant Quality Comparison",
        "category": "cross-system",
        "required_systems": ["mes-mil", "mes-det", "mes-cr", "plm"],
        "persona": "Plant Manager",
        "text": "Compare quality pass rates across Milwaukee, Detroit, and Cedar Rapids. Which plant has the most failures and are any linked to the same product specification?",
    },
    {
        "title": "Maintenance Impact on Output",
        "category": "cross-system",
        "required_systems": ["mes-mil", "mes-det", "mes-cr", "cmms"],
        "persona": "Plant Manager",
        "text": "Which production lines have open critical maintenance work orders right now, and how much output have we lost due to downtime this week?",
    },
    {
        "title": "Milwaukee Filler Pump Health",
        "category": "cross-system",
        "required_systems": ["SiteWise", "cmms", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Maintenance Engineer",
        "text": "For the filler pump on Milwaukee Bottling Line 1, show me equipment health over the last hour. Chart its telemetry grouped by unit of measure on shared charts — all temperatures (°F) together on one chart, speed (RPM), flow (L/min), and pressure (kPa) each on their own — never one chart per individual sensor. Also show any open CMMS maintenance work orders for that equipment and today's MES production output for the line." + WHAT_THIS_SHOWS,
    },
    {
        "title": "Filler Pump Outage Review",
        "category": "cross-system",
        "required_systems": ["SiteWise", "cmms"],
        "persona": "Maintenance Engineer",
        "display_text": "Review the most recent completed maintenance outage on the Milwaukee Line 1 filler pump with telemetry for the outage window",
        "text": "Find the most recent COMPLETED maintenance work order for the filler pump on Milwaukee Bottling Line 1 in CMMS. Summarize what happened (description, failure code, priority, technician, duration from created to completed). Then chart the equipment's telemetry for exactly that work order's created-to-completed time window, grouped by unit of measure on shared charts. Also fetch the raw telemetry statistics for that same window and present a per-channel table (channel, min, max, mean) with a factual assessment of each channel. Report ONLY what the numbers actually show — never speculate about what the charts 'should' or 'might' display. If nothing in a channel is abnormal, say so plainly." + WHAT_THIS_SHOWS,
    },
    # ── Quality Engineer ───────────────────────────────────────────────────
    {
        "title": "Supplier Quality Traceability",
        "category": "cross-system",
        "required_systems": ["erp", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Quality Engineer",
        # Bounded and aggregate: the open form ("which suppliers are linked
        # to batches with quality failures") matched hundreds of failed
        # batches with several supplier lots each, so an answer could only
        # show a sample and read as inconclusive. Bounded by COUNT, not by a
        # date window: a fresh frozen deployment's seeded failures are months
        # old and new simulator failures are probabilistic, so "the last N
        # days" can come back empty while "the N most recent" never does.
        "text": "Take the 10 most recent production batches with failed quality checks. Which suppliers' material lots went into them? Count those batches per supplier, worst first, and give one example batch with its failure for each." + WHAT_THIS_SHOWS,
    },
    {
        "title": "Customer Impact from Quality Issue",
        "category": "cross-system",
        "required_systems": ["erp", "mes-mil", "mes-det", "mes-cr", "plm"],
        "persona": "Quality Engineer",
        "text": "If a quality issue is found in a production run, which customers received that product and need to be notified? Trace from batch to order to customer.",
    },
    {
        "title": "Spec Deviation Analysis",
        "category": "cross-system",
        "required_systems": ["mes-mil", "mes-det", "mes-cr", "plm"],
        "persona": "Quality Engineer",
        "text": "Which production runs have quality checks that deviate from the engineering specification? Show the spec tolerance, actual measurement, and which product revision was in effect.",
    },
    {
        "title": "Equipment-Quality Correlation",
        "category": "cross-system",
        "required_systems": ["mes-mil", "mes-det", "mes-cr", "cmms"],
        "persona": "Quality Engineer",
        "text": "Do any equipment maintenance issues correlate with quality failures? Show the equipment, its maintenance history, and the production runs with failures that ran on that equipment.",
    },

    # ── Supply Chain Manager ───────────────────────────────────────────────
    {
        "title": "Orders at Risk",
        "category": "cross-system",
        "required_systems": ["erp", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Supply Chain Manager",
        "text": "Which customer orders are at risk of missing their delivery date? Cross-reference order due dates with current production run status and line availability.",
    },
    {
        "title": "Supplier Risk Assessment",
        "category": "cross-system",
        "required_systems": ["erp", "cmms"],
        "persona": "Supply Chain Manager",
        "text": "Which suppliers have the most delayed or backordered orders, and is any of their supplied material used in equipment with critical maintenance alerts?",
    },
    {
        "title": "Active Material in Production",
        "category": "cross-system",
        "required_systems": ["erp", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Supply Chain Manager",
        "text": "Which suppliers delivered materials that are currently in active production runs? Show the supplier, material lot, and which lines are using it right now.",
    },
    {
        "title": "Full Digital Thread Trace",
        "category": "cross-system",
        "required_systems": ["erp", "mes-mil", "mes-det", "mes-cr", "cmms", "plm", "SiteWise"],
        "persona": "Supply Chain Manager",
        "text": "Trace the complete digital thread for the most recent production batch across ALL systems simultaneously. Query ERP for the purchase order and supplier, query ALL THREE MES systems (Milwaukee, Detroit, Cedar Rapids) for production runs and quality checks, query CMMS for equipment and maintenance, query PLM for engineering specs, and query SiteWise for the live equipment telemetry on the production line that ran the batch — all in parallel. Show results from every system.",
    },
    {
        "title": "Lot Traceability",
        "category": "cross-system",
        "required_systems": ["erp", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Supply Chain Manager",
        "display_text": "Pick a shipment and show me all the other customers who received product from the same supplier lot",
        "text": "Pick one shipment that was delivered in the last 7 days and tell me all the other customer company names (from the customers table, same product type only) who received product from that same batch (with batch number), the suppliers who supplied the raw materials for that batch (with lot numbers), and which production line and MES work order produced it including any quality checks performed during that run.",
    },

    # ── Maintenance Engineer ───────────────────────────────────────────────
    {
        "title": "Critical Work Orders by Plant",
        "category": "cross-system",
        "required_systems": ["cmms", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Maintenance Engineer",
        "text": "Which production lines have critical or high-priority maintenance work orders open right now across all three plants? Show the asset, priority, and current line status.",
    },
    {
        "title": "Downtime Root Cause",
        "category": "cross-system",
        "required_systems": ["cmms", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Maintenance Engineer",
        "text": "Which equipment has caused the most production downtime this week? Cross-reference maintenance work orders with production run interruptions to identify the root cause." + WHAT_THIS_SHOWS,
    },
    {
        "title": "Preventive Maintenance Backlog",
        "category": "cross-system",
        "required_systems": ["cmms", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Maintenance Engineer",
        "text": "Which assets are overdue for preventive maintenance and are currently running in production? Show the asset, overdue work order, and which line it is on.",
    },
    {
        "title": "Engineering Change Impact on Maintenance",
        "category": "cross-system",
        "required_systems": ["plm", "cmms", "mes-mil", "mes-det", "mes-cr"],
        "persona": "Maintenance Engineer",
        "text": "What products have recent engineering changes that affect maintenance procedures? Show the change, the affected equipment, and any open work orders related to those assets.",
    },

    # ── Self-Learning Demo Queries ─────────────────────────────────────────
    {
        "title": "Map CMMS Assets to UNS Names",
        "category": "cross-system",
        "required_systems": ["cmms", "IIoT"],
        "persona": "Digital Thread Builder",
        "display_text": "Check CMMS asset names against UNS equipment names and propose value mappings",
        "text": "Check all CMMS asset serial numbers against the UNS (HighByte) equipment names. For each match you find, emit a VALUE-MAPPING-PROPOSAL block. Show a table of all matches with confidence levels.",
    },
    {
        "title": "Equipment Health with Cross-System Correlation",
        "category": "cross-system",
        "required_systems": ["cmms", "mes-mil", "mes-det", "mes-cr", "Historian"],
        "persona": "Reliability Engineer",
        "display_text": "Show maintenance history, production impact, and live telemetry for Milwaukee Line 1 Filler",
        "text": "For the Milwaukee Line 1 Filler equipment: show me the CMMS maintenance work order history, the MES production OEE for that line, and the historian telemetry (pump speed, bearing temperature) for the last hour. Correlate any maintenance events with production dips." + WHAT_THIS_SHOWS,
    },
]

DEMO_QUESTIONS: list[str] = [
    "Compare production attainment against order targets across all three plants "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
    "(Milwaukee, Detroit, Cedar Rapids). Which lines are behind schedule?",
    "Which production lines have critical or high-priority maintenance work orders "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
    "open right now across all three plants? Show the asset, priority, and line status.",
    "Take the 10 most recent production batches with failed quality checks. Which "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
    "suppliers' material lots went into them? Count those batches per supplier, worst first.",
]
