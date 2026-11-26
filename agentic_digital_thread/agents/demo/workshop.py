# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Workshop addon seam: deterministic workshop state.

Workshop content is an optional addon layered ON TOP of the demo addon, gated
behind the ``ADT_WORKSHOP`` env flag and isolated here in ``agents.demo`` (the
one sanctioned home for demo/workshop content — ADR-0044). The core runs,
imports, and passes tests with the workshop disabled; a ``deploy.sh --workshop``
deployment enables it (``--workshop`` implies ``--simulator``, which implies
``--seed``, so the demo addon is always active underneath).

Like the demo data flags, ``--workshop`` is deliberately NOT sticky (ADR-0030):
an environment that should stay in workshop mode must pass ``--workshop`` on
every deploy.

One ERP (ADR-0084): every story runs on the corporate ERP; there is no plant
ERP. The storyline is chosen at SYNTH time by the ``workshop_story`` CDK
context (``frozen`` | ``live``), which reaches the app as ``ADT_WORKSHOP_STORY``
(:func:`story`) and decides which storyline systems register.

Three deterministic levers live behind this seam:

1. **Register-only workshop systems** (:func:`registered_systems`) — registered
   at startup through the SAME ``connect_new_system`` path as the demo systems
   (via ``agents.demo.systems.manifest_systems``), so they are present and
   reachable in the catalog (status ``registered``) but NEVER onboarded by any
   bulk default (:data:`ONBOARD_EXCLUDE`). Attendees onboard them live.
2. **The planted wrong-mapping fixture** (``fixtures/`` +
   :func:`mapping_overrides_path`) — consumed by the generic, env-gated
   ``ADT_MAPPING_OVERRIDES_FILE`` seam in ``agents.onboard_agent.map_fields``
   so the mapping trap fires identically for every attendee (never relying on
   the live model to make the mistake).
3. **The pinned refusal** (:data:`PINNED_MAINTENANCE_REFUSAL` +
   :func:`refusal_addendum`) — a workshop-gated orchestrator prompt addendum
   that pins the phrasing of the existing SYSTEM NOT ONBOARDED honesty rule
   for the un-onboarded maintenance system, so every attendee sees the same
   refusal shape. It strengthens the rule; it never weakens it.

Generic dependency-free stdlib module — safe to import from the demo seam
(one-way dependency: core callers route through the seam, never the reverse).
See docs/workshop-mode.md.
"""
from __future__ import annotations

import os
from pathlib import Path

_ENABLED_VALUES = {"1", "true", "yes", "on"}

#: The workshop stories (``workshop_story`` CDK context → ``ADT_WORKSHOP_STORY``).
STORIES: tuple[str, ...] = ("frozen", "live")
DEFAULT_STORY = "frozen"


def is_enabled() -> bool:
    """True iff workshop mode is enabled (``ADT_WORKSHOP`` env; default OFF).

    Deliberately simpler than ``agents.demo.is_enabled``: no runtime override —
    workshop mode is a deploy-time profile, flipped only by redeploying.
    """
    return os.environ.get("ADT_WORKSHOP", "").lower() in _ENABLED_VALUES


def _check_story(name: str) -> str:
    if name not in STORIES:
        raise ValueError(f"unknown workshop story {name!r}; expected one of {STORIES}")
    return name


def story() -> str:
    """The deployed workshop story (``ADT_WORKSHOP_STORY``; unset → frozen).

    A synth-time setting, never a runtime override; an unknown non-empty value
    raises rather than silently falling back to another story.
    """
    raw = os.environ.get("ADT_WORKSHOP_STORY", "").strip().lower()
    return _check_story(raw) if raw else DEFAULT_STORY


# Workshop systems. Two groups, one manifest shape (``agents.demo.systems.
# DEMO_SYSTEMS`` — ids VERBATIM, tool-name-safe):
#
# * PRE-ONBOARDED storyline systems, per story (:func:`seeded_systems`).
#   FROZEN (:data:`WORKSHOP_SEEDED_SYSTEMS`): the Indianapolis B-2847 defect
#   chain that the workshop content traces in modules 2, 5, 6 and 7
#   (docs/architecture.md "The B-2847 defect chain"); the customer-impact leg
#   runs MES -> PLM -> corporate ERP. LIVE (:data:`LIVE_SEEDED_SYSTEMS`): the
#   simulator's Indianapolis MES view and SiteWise; no archived historian.
#   The seed (lambda-adt-seed) has always carried these schemas, but the demo
#   manifest never REGISTERED them, so on the first live dry run (Workshop
#   Studio test event 9, 2026-09-13) the orchestrator answered "B-2847 —
#   IDENTIFIER-MISS from all four systems". The workshop seed CR onboards them
#   before doors open (module-1 starting state).
# * REGISTER-ONLY systems (:data:`WORKSHOP_REGISTER_ONLY_SYSTEMS`): present +
#   reachable in the catalog (status 'registered') but EXCLUDED from every
#   bulk-onboard default — attendees onboard them live (module 4).
#
# Routing: the frozen ``mes_line3`` lives in the SEED database despite its
# ERP-family prefix (``agents.demo.db_routing.
# SEED_RESIDENT_ERP_FAMILY_SCHEMAS``); the live ``mes_indianapolis`` is a
# simulator view in the ERP database (prefix rule); ``iot_historian`` and
# ``cmms_*`` route to the seed DB by the default rule; SiteWise (Direct) carries no schema —
# the seed harness supplies the regional ``iotsitewise://`` endpoint, and the
# seed CR creates the ``CNC`` asset model + ``5-Axis-Milling-CMX-441`` asset
# the historian story and lambda-sitewise-feed resolve BY NAME.
WORKSHOP_SEEDED_SYSTEMS: list[dict] = [
    {"id": "mes-ind", "name": "Indianapolis MES", "plant": "Indianapolis",
     "system_type": "MES", "connection_type": "PostgreSQL",
     "schema": "mes_line3",
     "capabilities": ("Plant-floor execution for this plant's lines L3, L5 and L7: production runs "
                      "(batch_number B-####, work_order_ref WO-####, line_id, "
                      "equipment_id), SPC quality checks."),
     "query_guidance": (
         "Answers which line, machine, shift and work order made a batch (B-####) "
         "and its quality outcome. Equipment L3-M1 is the 5-axis milling center "
         "CMX-441.")},
    {"id": "historian", "name": "Plant Historian", "plant": "Indianapolis",
     "system_type": "Historian", "connection_type": "PostgreSQL",
     "schema": "iot_historian",
     "capabilities": ("Archived sensor history (vibration, temperature) per machine for "
                      "this plant's February 2026 investigation window; NOT live."),
     "query_guidance": (
         "Answers how a machine's sensors behaved during the archived February 2026 "
         "window; machines are keyed by name (the CMX-441 is 5-Axis-Milling-CMX-441). "
         "For current or recent (last hours/days) readings use AWS IoT SiteWise.")},
    {"id": "sitewise-direct", "name": "AWS IoT SiteWise", "plant": "Indianapolis",
     "system_type": "Historian", "connection_type": "SiteWise (Direct)",
     "schema": "",
     "capabilities": ("Live OT telemetry for this plant's machines from AWS IoT "
                      "SiteWise (asset 5-Axis-Milling-CMX-441: vibration, coolant "
                      "temperature)."),
     "query_guidance": (
         "The ONLY live source: what a machine's sensors read NOW and over the last "
         "hours/days. Use for 'is vibration trending up' questions about the CMX-441; "
         "the Plant Historian holds only the archived February 2026 window.")},
]

#: The live story's plant MES: the simulator's per-site view of the corporate
#: ERP's MES tables (lambda-db-init ``get_machining_views_sql``).
LIVE_MES_SYSTEM: dict = {
    "id": "mes-ind", "name": "Indianapolis MES", "plant": "Indianapolis",
    "system_type": "MES", "connection_type": "PostgreSQL",
    "schema": "mes_indianapolis",
    "capabilities": ("Live plant-floor execution for this plant's lines LINE1, LINE2 and "
                     "LINE3: work orders, production events, quality checks, downtime "
                     "events and line status."),
    "query_guidance": (
        "Answers which line and work order built a production run and its quality "
        "outcome. Equipment L3-M1 on LINE3 is the 5-axis milling center CMX-441. "
        "Name work orders by work_order_number and production runs by run_number, "
        "never by their UUID row ids. "
        "Translate other systems' identifiers through the graph's identity rules, "
        "never by guessing."),
}

#: The live story's SiteWise entry: same system as the frozen one, but its
#: guidance names no archived historian (the live story registers none).
LIVE_SITEWISE_SYSTEM: dict = {
    "id": "sitewise-direct", "name": "AWS IoT SiteWise", "plant": "Indianapolis",
    "system_type": "Historian", "connection_type": "SiteWise (Direct)",
    "schema": "",
    "capabilities": ("Live OT telemetry for this plant's machines from AWS IoT "
                     "SiteWise (asset 5-Axis-Milling-CMX-441: vibration, coolant "
                     "temperature)."),
    "query_guidance": (
        "The ONLY sensor source: what a machine's sensors read NOW and over the last "
        "hours/days. Use for 'is vibration trending up' questions about the CMX-441."),
}

LIVE_SEEDED_SYSTEMS: list[dict] = [LIVE_MES_SYSTEM, LIVE_SITEWISE_SYSTEM]

_STORY_SEEDED_SYSTEMS: dict[str, list[dict]] = {
    "frozen": WORKSHOP_SEEDED_SYSTEMS,
    "live": LIVE_SEEDED_SYSTEMS,
}

WORKSHOP_REGISTER_ONLY_SYSTEMS: list[dict] = [
    {"id": "cmms-ind", "name": "Indianapolis Plant Maintenance", "plant": "Indianapolis",
     "system_type": "CMMS", "connection_type": "PostgreSQL",
     "schema": "cmms_indianapolis",
     "capabilities": ("Maintenance for this plant: assets (asset_num 1001-CMX-441), "
                      "work orders, preventive-maintenance schedules and deferrals."),
     "query_guidance": (
         "Answers maintenance history and every open maintenance work order per "
         "asset (PM, deferred, alarm-raised); the CMX-441 is asset 1001-CMX-441."),
     # For the system agent that writes this CMMS's SQL only (see
     # ``demo.systems.agent_query_notes``). Test event, 2026-10-15: a deferral
     # was dated by the day its work order was raised. Rehearsal walk,
     # 2026-10-22: cancelled orders were listed and counted as open work, and
     # nothing the agent reads said what the status codes mean (the seed's
     # column carries only a CHECK list of codes), so the source's own code
     # meanings are stated here (tests/unit/test_cmms_status_codes_described.py).
     "agent_query_notes": (
         "A work order's reported_date is when it was raised; later changes such as "
         "a deferral are dated by their work_order_log entry; target_start is the "
         "original due date and sched_start the current schedule. "
         "work_orders.status codes: open work is WAPPR (waiting for approval), APPR "
         "(approved), INPRG (in progress) and WMATL (waiting for material); COMP "
         "(completed), CLOSE (closed) and CAN (cancelled, never done) are not open work.")},
]

#: The HighByte Intelligence Hub as a REGISTER-ONLY workshop system, present in
#: the catalog ONLY when the workshop package shipped HighByte (:func:`
#: highbyte_shipped`). The built-in system id ``highbyte`` (ADR-0031);
#: connection_type ``MCP Server``, so one Onboard click runs the existing
#: built-in Unified Namespace discovery (``onboard_agent._discover_mcp_entry``
#: bootstraps its URL + token). schema "" (an MCP system carries none).
#:
#: Routing (step 2): HighByte serves the INSTANTANEOUS current value from the
#: Unified Namespace; it defers trend/history ("is it trending up") to AWS IoT
#: SiteWise by name, so modules 5 and 6's vibration-trend questions still route
#: to SiteWise. This is the system's OWN registration prose — no per-turn app
#: logic, no question-word matching.
HIGHBYTE_SYSTEM: dict = {
    "id": "highbyte", "name": "HighByte Intelligence Hub", "plant": "Indianapolis",
    "system_type": "Historian", "connection_type": "MCP Server",
    "schema": "",
    "capabilities": ("Live Unified Namespace for this plant's machines over MCP: the "
                     "current value each OT sensor reads right now (the CMX-441's "
                     "vibration and coolant temperature), folded from the plant OPC UA "
                     "servers."),
    "query_guidance": (
        "The live Unified Namespace: the INSTANTANEOUS value a machine's sensors read "
        "right now. For how a reading TRENDS over the last hours/days use AWS IoT "
        "SiteWise."),
}

#: The exact phrase in a SiteWise (Direct) entry's ``query_guidance`` that
#: claims SiteWise is the sole live sensor source. True in a deploy WITHOUT
#: HighByte; once HighByte is a second live OT source it is no longer the ONLY
#: one, so :func:`_sitewise_with_highbyte` rewrites it. Kept as a literal so the
#: rewrite is exact and a drift in the base text fails the unit test loudly.
_SITEWISE_ONLY_CLAIM = "The ONLY sensor source: what a machine's sensors read NOW and over the last"
_SITEWISE_WITH_HIGHBYTE_LEAD = (
    "The sensor TREND and HISTORY source: what a machine's sensors read over the last")

_ENABLED_HB_PLACEHOLDER = "placeholder"  # nosec B105 - sentinel value, not a credential


def highbyte_shipped() -> bool:
    """True iff the workshop package shipped HighByte (the live package).

    Reads the signal the app ALREADY resolves at registration:
    ``catalog._highbyte_declared()`` (env ``HIGHBYTE_URL`` → SSM
    ``/adt/highbyte-url``) finds a URL, or the workshop root's ``pending``
    (the package ships the hub; the root writes its URL once the hub's stack
    finishes, which can be after this task registers its systems). No new
    env/SSM is introduced. ``catalog`` imports lazily HERE so this module stays
    import-pure stdlib (``catalog`` pulls the concept vocabulary, not boto3 at
    import; boto3 is lazy inside ``_get_highbyte_url`` itself and is reached
    only when the env var is unset — a workshop app task always has
    ``HIGHBYTE_URL`` injected, so the SSM fallback is not exercised at startup).
    """
    from ..catalog import _highbyte_declared
    return _highbyte_declared()


#: Private alias so :func:`registered_systems` can call the detector even though
#: its keyword argument (kept for the public/test call shape) shadows the name.
_highbyte_shipped = highbyte_shipped


def _sitewise_with_highbyte(entry: dict) -> dict:
    """A SiteWise (Direct) entry rewritten for a deployment where HighByte is
    ALSO a live sensor source: drop the "ONLY sensor source" claim, keep the
    trend-routing cue so trend/history questions still land on SiteWise. A copy;
    an entry without the exact base claim is returned unchanged (defensive)."""
    guidance = entry.get("query_guidance", "")
    if _SITEWISE_ONLY_CLAIM not in guidance:
        return dict(entry)
    rewritten = guidance.replace(_SITEWISE_ONLY_CLAIM, _SITEWISE_WITH_HIGHBYTE_LEAD, 1)
    return {**entry, "query_guidance": rewritten}

#: Everything the FROZEN story registers, seeded first (registration order is
#: also the catalog's display order within the workshop group).
WORKSHOP_SYSTEMS: list[dict] = WORKSHOP_SEEDED_SYSTEMS + WORKSHOP_REGISTER_ONLY_SYSTEMS

#: What a demo (non-workshop) deploy registers on top of the demo six: the
#: plant's live MES view, its maintenance system, and the corporate supplier
#: master (the ValueMapping crosswalk source) — registered only, never in a
#: bulk-onboard default (``DEMO_SYSTEMS`` stays the six). The supplier master is
#: demo-only on purpose: it is NOT in any workshop system list, so the workshop
#: content is untouched.
SUPPLIER_MASTER_SYSTEM: dict = {
    "id": "supplier-master", "name": "Supplier Master", "plant": "All Sites",
    "system_type": "MDM", "connection_type": "PostgreSQL",
    "schema": "supplier_master",
    "capabilities": ("Corporate supplier/vendor master and AP spend: vendors "
                     "(vendor_number, legal_name, vendor_group, payment_terms) "
                     "and annual spend per vendor (vendor_spend_summary)."),
    "query_guidance": (
        "Answers who a vendor is and annual spend per vendor. Vendors are keyed "
        "by vendor_number (e.g. VM-1000017) and named by legal_name. The same "
        "companies appear in the corporate ERP under different supplier codes; "
        "match them through the graph's identity rules, never by guessing."),
}

DEMO_PLANT_SYSTEMS: list[dict] = [
    dict(s) for s in [LIVE_MES_SYSTEM, *WORKSHOP_REGISTER_ONLY_SYSTEMS, SUPPLIER_MASTER_SYSTEM]
]

#: The demo systems every story's seed CR pre-onboards first (never ``cmms``
#: — lever 1).
_SEEDED_DEMO_IDS: tuple[str, ...] = ("erp", "mes-mil", "mes-det", "mes-cr", "plm")


def seeded_systems(story_name: str | None = None) -> list[dict]:
    """The storyline systems the seed CR pre-onboards for *story_name*
    (default: the deployed :func:`story`), as copies."""
    name = _check_story(story_name) if story_name else story()
    return [dict(s) for s in _STORY_SEEDED_SYSTEMS[name]]


def seeded_system_ids(story_name: str | None = None) -> tuple[str, ...]:
    """The ids the seed CR pre-onboards for *story_name*, in order: the demo
    five plus the story's storyline systems. Pinned against the vendored CR
    handler and the engine stack by tests/unit/test_workshop_mode.py."""
    return _SEEDED_DEMO_IDS + tuple(s["id"] for s in seeded_systems(story_name))


#: The FROZEN story's pre-onboarded ids (kept for scripts/docs compatibility).
WORKSHOP_SEEDED_SYSTEM_IDS: tuple[str, ...] = seeded_system_ids("frozen")

# Declarative bulk-onboard exclusion: these ids must NEVER appear in a bulk
# onboarding default set (``scripts/onboarding_smoketest.DEFAULT_SYSTEMS``,
# the demo manifest). Pinned by tests/unit/test_workshop_mode.py. They are
# registered in BOTH demo and workshop mode (``DEMO_PLANT_SYSTEMS`` /
# ``registered_systems``), so ``scripts/reonboard.py --all`` drops them from
# its nothing-yet-onboarded fallback in either mode; prefer ``--systems`` when
# re-onboarding a workshop env — see docs/workshop-mode.md.
ONBOARD_EXCLUDE: frozenset = frozenset(
    [s["id"] for s in WORKSHOP_REGISTER_ONLY_SYSTEMS]
    + [HIGHBYTE_SYSTEM["id"], SUPPLIER_MASTER_SYSTEM["id"]])


def registered_systems(story_name: str | None = None, *,
                       highbyte_shipped: bool | None = None) -> list[dict]:
    """Manifest entries to register when workshop mode is enabled (copies):
    the story's seeded systems, then the register-only systems, then — when
    the workshop package shipped HighByte — the register-only HighByte hub.

    ``highbyte_shipped`` defaults to the live signal (the module function of
    the same name); pass an explicit bool to pin it (tests, offline budget
    harness). When HighByte is registered the SiteWise (Direct) entry's
    guidance is rewritten so it no longer claims to be the ONLY sensor source
    (:func:`_sitewise_with_highbyte`); when it is not, every entry is
    byte-identical to the pre-HighByte manifest.
    """
    ship = _highbyte_shipped() if highbyte_shipped is None else highbyte_shipped
    seeded = seeded_systems(story_name)
    register_only = [dict(s) for s in WORKSHOP_REGISTER_ONLY_SYSTEMS]
    if not ship:
        return seeded + register_only
    seeded = [_sitewise_with_highbyte(s) if s["id"] == "sitewise-direct" else s
              for s in seeded]
    return seeded + register_only + [dict(HIGHBYTE_SYSTEM)]


def mapping_overrides_path() -> str:
    """Absolute path of the bundled wrong-mapping fixture (lever 2 data).

    This is what ``ADT_MAPPING_OVERRIDES_FILE`` points at in a workshop deploy
    (deploy.sh / stacks wire it); exposed here so tests and local dev resolve
    the same file without duplicating the path.
    """
    return str(Path(__file__).resolve().parent / "fixtures"
               / "workshop_mapping_overrides.json")


# The EXACT pinned refusal sentence (lever 3). Attendees all see this shape
# when asking maintenance questions against the un-onboarded workshop CMMS.
# Generic on purpose — no site/system literal, so the addendum stays inert
# content-wise and the sentence works for any un-onboarded maintenance system.
PINNED_MAINTENANCE_REFUSAL = (
    "I don't have a maintenance system connected for that site, so I can't "
    "answer the maintenance part of this question."
)


def refusal_addendum() -> str:
    """Workshop-gated orchestrator prompt addendum pinning the refusal phrasing.

    Appended to the orchestrator system prompt ONLY when :func:`is_enabled`.
    It rides on the existing SYSTEM NOT ONBOARDED — SAY SO, NEVER SUBSTITUTE
    rule (grounded-refusal machinery): the rule already forbids substituting
    another system's data; this addendum only pins the SENTENCE the refusal
    must start with, so every attendee sees the same shape.
    """
    return (
        "\n\nWORKSHOP PINNED REFUSAL (exact phrasing required):\n"
        "When the question needs maintenance/CMMS data for a site whose "
        "maintenance system is NOT in the connected-system list, the "
        "maintenance part of your answer MUST begin with EXACTLY this "
        f"sentence: \"{PINNED_MAINTENANCE_REFUSAL}\"\n"
        "Nothing comes before that sentence in the maintenance part: no "
        "narration of your lookup steps and no account of how the "
        "connected-system list was found.\n"
        "Then answer the satisfiable part normally with citations, per the "
        "SYSTEM NOT ONBOARDED rule above — this addendum pins the phrasing; "
        "it does not relax that rule.\n"
    )
