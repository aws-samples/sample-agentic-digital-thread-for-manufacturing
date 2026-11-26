<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Expected-mapping ground-truth keys (ADR-0012)

This directory holds the **authored ground-truth keys** the mapping-quality eval
(`tests/integration/mapping_eval.py`) scores the live LLM-produced field→concept
mappings against. Governed by `docs/adr/0012-mapping-quality-eval.md`.

## Integrity rule (ADR-0012) — verbatim

> This key is authored from what the fields **MEAN**, never by transcribing
> observed LLM output. A key written from model output is a circular,
> self-fulfilling eval and is explicitly disallowed.

Each `<system>.yaml` was written by reading the seed schema's DDL
(`lambda-adt-seed/004_cmms_maintenance.sql`) and deciding, from the real-world
semantics of each column, which canonical concept id(s) a correct mapping may
land on — **independently of, and blind to, anything the mapper produced.**

## Format

```yaml
system: cmms
schema: cmms_maintenance
overall_floor: 0.90            # min fraction of scored fields mapped into acceptable
must_map_min_confidence: 0.60  # min r.confidence on every must-map tracer
fields:
  "table.column":
    acceptable: [concept-id, ...]   # a correct mapping lands on ANY of these
    must_map: true|false            # tracer fields the demo narrative depends on
    min_confidence: 0.70            # optional per-field override (>= must_map_min_confidence)
ignore:                             # surrogate/duplicate/audit fields, not scored
  - table.column
```

- **Per-field acceptable SET, not a single concept** (ADR-0012): a field may
  legitimately map to one of several defensible concepts; only a mapping
  *outside* the set is a real miss.
- **Two-level threshold** (ADR-0012): the `must_map` tracer set is asserted at
  100% (each present, in-acceptable-set, and meeting the confidence gate); the
  rest are covered by `overall_floor`.
- Concept ids are validated at load time against `agents.catalog.CORE_CONCEPTS`;
  `load_key` raises on any unknown id (guards key drift).

## Scoring of unlisted / `*_raw` / view fields

`discover` surfaces both the perpetual-freshness **views** (`work_orders`,
`maintenance_schedules`) and their `*_raw` base tables. The eval keys off
`se.name` (bare table name). Any live field that is **neither** in `fields`
**nor** in `ignore` is treated as **OUT-OF-KEY**: reported separately and
**excluded from the accuracy denominator**, so `*_raw` duplicates and any
unforeseen columns never fail the overall floor. `ignore` is for fields we
positively know are surrogate keys / audit timestamps / derived view columns.

## cmms.yaml rationale notes

- `assets.asset_tag → equipment-id`: a tag modeled after IBM Maximo (`MKE-FILL-001`); the
  cross-system identity anchor (CMMS `asset_tag` ↔ MES `line_id` ↔ OPC UA path).
- `assets.area → [production-line, location]` — `Bottling-L1`/`AutoParts-L2` is
  the canonical cross-system `production-line` link per its concept def; a
  `location` (physical area) reading is defensible, so both are acceptable.
- `assets.manufacturer → [supplier, equipment-id]` — a manufacturer is a kind of
  supplier/vendor; tying it to the equipment identity is also defensible.
- `work_orders.wo_number → [work-order, maintenance-event]` — a CMMS work order
  is both a `work-order` and the `maintenance-event` it represents.
- `spare_parts.quantity_on_hand → inventory-level` — the S0.1 F9 tracer
  (`inventory-level` concept def explicitly lists `quantity_on_hand`).
- Surrogate PKs (`*_id`), audit timestamps (`created_at`/`completed_at`),
  offset/`*_raw` columns, and the view-computed `status`/`last_performed` are in
  `ignore` (or out-of-key) because they carry no source-domain concept meaning.

## Must-map tracer set (cmms) — the demo-narrative critical mappings

| field | concept | why critical |
|---|---|---|
| `assets.asset_tag` | `equipment-id` | cross-system identity anchor |
| `assets.asset_type` | `asset-class` | equipment classification |
| `assets.operational_status` | `lifecycle-state` | asset lifecycle |
| `assets.criticality` | `criticality` | risk prioritization |
| `work_orders.wo_number` | `work-order` | maintenance work tracking |
| `work_orders.failure_code` | `failure-mode` | failure analysis |
| `maintenance_schedules.maintenance_type` | `maintenance-schedule` | PM cadence |
| `spare_parts.part_number` | `spare-part` | parts identity |
| `spare_parts.quantity_on_hand` | `inventory-level` | S0.1 F9 value-read tracer |

## Where the eval runs

Per ADR-0012, the eval runs in the **cloud WARM/HOT loop, NOT as a gating CI
check** (the offline/deterministic graph lane is deferred per the S0.2 plan).
Threshold flakiness is not a release blocker, and the score doubles as a **demo
artifact** (`mapping-eval-<system>.json` + the "mapped N/M fields correctly"
line).

## Drift maintenance

These keys are maintained **alongside** the seed data. If a seed schema changes,
re-author its key **from meaning** — never from observed model output.
