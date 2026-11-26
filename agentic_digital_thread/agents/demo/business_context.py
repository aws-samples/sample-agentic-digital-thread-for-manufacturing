# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo/workshop business context — CONTENT, not engine (ADR-0044).

The guided interview captures domain context from a steward. A workshop has no
steward and no time for an interview, so this module ships pre-written context
for the demo systems, letting the workshop demonstrate the WITH-context
experience immediately.

Two deliberate constraints:

* **Opt-in, not always-on.** Gated behind the demo addon AND an explicit seed
  call. If the demo environment were permanently context-on, the workshop would
  lose its most persuasive moment — showing the same question answered badly
  without context and well with it. The contrast IS the demo.
* **Lands through the ordinary lifecycle.** Seeding calls the same
  business-context write path a steward's confirm uses (sanitize → validate →
  write → batched roll), so nothing here is a side door and the workshop is
  exercising the real mechanism.

Text is written to the same standard the intake guide asks of customers
(``docs/domain-context-intake.md``): unit of work, state semantics, real-vs-noise
failure signals, identifier shapes, authority, coverage boundaries, query
strategy — stated plainly, no scored literals, no credentials.
"""
from __future__ import annotations

# system_id -> business context. Kept under the 2000-char ADR-0054 bound.
DEMO_BUSINESS_CONTEXT: dict[str, str] = {
    "erp": (
        "This is the ERP system of record for commercial documents: sales orders, "
        "purchase orders, inventory positions, material lots and shipments. The "
        "unit of work is an ORDER or a LOT, not a machine event — it has no live "
        "equipment state and no telemetry. Order and lot identifiers are the "
        "cross-system keys: the same lot appears in MES production records under a "
        "batch or run identifier, so a question spanning commercial and shop-floor "
        "data joins on those. ERP is authoritative for customer, price, quantity "
        "and due-date questions; it is NOT authoritative for what a machine was "
        "doing at a point in time. Financial values are as-booked, so they can lag "
        "physical movement by a day. For counts and distributions prefer aggregate "
        "queries over reading rows page by page."
    ),
    "cmms": (
        "This is the maintenance system of record. The unit of work is a WORK "
        "ORDER against an ASSET — corrective, preventive or inspection. Its state "
        "column is the work-order lifecycle (open, in progress, completed, "
        "deferred), NOT live machine state: a completed work order says a repair "
        "finished, not that the machine is running. A DEFERRED preventive task is "
        "an important signal and is easy to misread as done. Assets carry a "
        "criticality ranking and a facility/area location; the area value is the "
        "cross-system link to production lines in MES. CMMS is authoritative for "
        "maintenance history, asset criticality and spare-part inventory. It has "
        "no process telemetry — sensor and machine-state questions belong to the "
        "historian. Failure codes describe the diagnosed cause of a repair, not "
        "every stoppage: brief stops may never generate a work order at all."
    ),
    "plm": (
        "This is the product-lifecycle system: part definitions, revisions, bills "
        "of material and engineering change records. The unit of work is a PART "
        "REVISION, not a produced item — it describes what should be built, never "
        "what was built. Part numbers are the join key to ERP order lines and MES "
        "production records. PLM is authoritative for the current revision of a "
        "design and for BOM structure; production quantities and quality outcomes "
        "live elsewhere. An engineering change has an effective date, so asking "
        "'what is the BOM' without a date is ambiguous for parts mid-change."
    ),
}

# The three plant MES systems share their meaning; only the plant differs.
_MES_CONTEXT = (
    "This is a plant MES holding shop-floor execution records: production runs, "
    "production events, quality checks, downtime events and material "
    "consumption. The unit of work is a PRODUCTION RUN on a LINE. Its state "
    "column is the run lifecycle, not live equipment state — a failed run is a "
    "production outcome, while the reason a machine stopped lives in the "
    "downtime records and the historian. Line identifiers are the key "
    "cross-system link: the same physical line appears as an area value in the "
    "maintenance system. This MES is authoritative for what was produced, when, "
    "on which line, and with what quality result. It is NOT authoritative for "
    "asset maintenance history or for live sensor values. Downtime records "
    "capture stoppages with a reason code; short stops may be aggregated rather "
    "than recorded individually, so downtime counts are a lower bound. For "
    "counts and distributions prefer aggregate queries over paging raw rows."
)

for _sys in ("mes-mil", "mes-det", "mes-cr"):
    DEMO_BUSINESS_CONTEXT[_sys] = _MES_CONTEXT


def demo_business_context(system_id: str) -> str:
    """Return the demo context for ``system_id``, or ``""`` when none is shipped."""
    return DEMO_BUSINESS_CONTEXT.get(system_id, "")


def demo_business_context_systems() -> list[str]:
    """System ids for which demo context exists, in a stable order."""
    return sorted(DEMO_BUSINESS_CONTEXT)
