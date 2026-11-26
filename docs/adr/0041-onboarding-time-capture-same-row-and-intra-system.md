<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Onboarding-time capture: same-row identifier sets + intra-system join discovery at provision

---
Status: accepted

Related: extends [ADR-0040](0040-dynamic-entity-and-identity-for-any-system.md) additively.
---

## Context

The 2026-07-14 audit of a full six-system environment showed the agent
rediscovering, **at query time and one value at a time**, relationships that
are fully determinable **at onboarding**:

- **Intra-system joins (G1).** `join_discovery.discover_pair` refuses
  same-system pairs by design (cross-system focus), and the FK-based
  `onboard._detect_internal_joinpaths` misses view-backed schemas (no FK
  constraints) and cannot find `production_lines` from `line_id` — its
  pluralization heuristic only tries `line+"s"/"es"` against full table names.
  Result: 9 pending per-value proposals existed only because the
  `work_orders.line_id → production_lines` link was never captured at
  provision, for any of the three MES plants.
- **Same-row identifier sets (G2).** The CMMS `assets` table carries
  `serial_number`, `asset_tag`, and `asset_id` — three interchangeable
  identifiers **on the same row** (all map to the `equipment-id` concept;
  `asset_tag` is DDL-UNIQUE). The graph has no representation for "these
  columns identify the same record," so the agent burned 40 per-value
  proposals rediscovering serial↔tag equivalence pair by pair.

[ADR-0040](0040-dynamic-entity-and-identity-for-any-system.md) D3 gives every
table an authoritative PRIMARY identity key
(`SchemaEntry-[:IDENTIFIED_BY]->Field`). The obvious-looking fix — widen
`IDENTIFIED_BY` into a set — is the wrong relation: which single key is
authoritative for identity is a different question from which columns are
interchangeable aliases of each other on one row.

The building blocks already exist: `SCHEMA_SAMPLE_VALUES` sampling, the
`discover_transform` ladder with overlap/cardinality floors, the
`is_identifier_like`/`is_measure_like` gates, and — critically — a
**direct-write precedent**: FK-detected internal JoinPaths are already written
straight to the graph during `_do_provision` (`method='foreign_key_match'`),
bypassing the proposal queue, because FK constraints are deterministic
evidence.

## Decision

### D1 — Same-row identifier sets are internal JoinPaths with `transform='same_row'`, NOT a widened `IDENTIFIED_BY`

A same-row identifier set is represented as internal JoinPath nodes — one per
column pair in the set — with `from_table == to_table`,
`transform='same_row'`, `method='same_row_identifier_set'`, plus the usual
FROM_SYSTEM/TO_SYSTEM edges. Node ids are deterministic
(`jp-samerow-<system>-<table>-<colA>-<colB>`, columns sorted) so re-onboarding
MERGEs idempotently.

ADR-0040's `IDENTIFIED_BY` edge is **untouched**: it stays the authoritative
PRIMARY identity key (one, or several for composite keys). Alternate
interchangeable identifiers are a *traversal* relation — "if you have a
serial_number you can read the asset_tag off the same row" — which is exactly
what JoinPath already means to its consumers (resolve and the orchestrator's
cross-system exploration). No new node type, no new consumer wiring.

Set membership is detected per table at provision: columns that are
identifier-like (and not measure-like) pair when they map to the SAME concept
via this provision's mappings, OR when BOTH carry a DDL UNIQUE constraint
(`information_schema.table_constraints`/`constraint_column_usage`) — two
unique identifier columns are each 1:1 with the row, hence interchangeable.
Only SINGLE-COLUMN UNIQUE constraints qualify as uniqueness evidence: a
composite `UNIQUE(a, b)` makes the pair jointly unique — neither column is
1:1 with the row on its own, so its member columns never enter the set.

Either-unique pairing (one UNIQUE column pairing with EVERY identifier column
in the table) is deliberately rejected: one-sided uniqueness does not
establish on-row interchangeability, so it writes false `same_row` edges — a
UNIQUE `wo_number` would pair with an unrelated `line_id`, claiming the two
identify the same record when they do not.

### D2 — Intra-system value-overlap discovery at provision

A new `join_discovery.discover_intra_system(system_id)` compares
identifier-like fields of the same system across **different** tables
(same-table pairs are D1's turf) using the existing sampling seams, the
`discover_transform` ladder, and the same defensive emit gate
(`is_identifier_like and not is_measure_like` on both endpoints).
`discover_pair`'s same-system refusal is kept — cross-system discovery is
unchanged. `discover_intra_system` **returns candidates and does not write**;
the provision caller decides direct-write vs proposal per D3.

Supporting fix: the FK pluralization heuristic also matches snake_case tables
*ending in* the pluralized prefix (`production_lines` from `line_id`), still
requiring the column-existence check.

The degeneracy gate `proposal_scoring.is_degenerate_join_proposal` becomes
table-aware: same system + same table + same column = self-join (dropped);
same system, same column name, **different** tables
(`work_orders.line_id → production_lines.line_id`) is legitimate and kept.
Fields without a table qualifier keep today's conservative bare-column drop.

### D3 — The direct-write-at-provision rule: deterministic evidence writes, weaker evidence proposes

Extending the existing `foreign_key_match` precedent into a stated rule:

- **Deterministic evidence → direct graph write at provision.** FK
  constraints, DDL UNIQUE constraints, shared concept on the same table (D1),
  and the transform ladder clearing its overlap + key-cardinality floors (D2)
  are machine-checkable facts about the system itself. They MERGE graph nodes
  directly during `_do_provision`, with provenance
  (`method`, `overlap_count`, `overlap_ratio`) stored on the node.
- **Weaker evidence → proposal queue.** Crosswalk-grade candidates
  (name-similarity, no transform found) flow through the existing
  [ADR-0003](0003-unified-proposal-lifecycle.md) proposal lifecycle untouched,
  pending human review.

Discovery failures are best-effort: a raising discovery step logs and
provision proceeds (matching the FK block's existing contract).

## Considered Options

- **Widen `IDENTIFIED_BY` into identifier sets.** Rejected: conflates
  key-ness with equivalence and breaks ADR-0040 D3's "additive and
  authoritative for identity" contract — a consumer asking "what is this
  table's identity key" must get ONE answer (or one composite), not a set of
  interchangeable aliases. JoinPath is already the traversal relation and is
  already consumed by resolve/orchestrator.
- **A new dedicated node type (e.g. `IdentifierSet`).** Rejected: every
  consumer (resolve, explore_graph, the orchestrator prompt context) would
  need new wiring; internal JoinPath with `transform='same_row'` rides
  existing consumption for free.
- **Route provision-time discoveries through the proposal queue.** Rejected
  for deterministic evidence: it recreates the exact failure being fixed
  (human-review latency on machine-checkable facts; 40 per-value proposals for
  one UNIQUE-constraint fact) and contradicts the established
  `foreign_key_match` direct-write precedent. Kept for weaker
  (crosswalk-grade) evidence.
- **Relax `discover_pair` to accept same-system pairs.** Rejected: its
  contract, call sites, and tests are cross-system; a separate
  `discover_intra_system` keeps both paths simple and independently testable.

## Consequences

- `line_id` internal JoinPaths (all three MES plants) and the CMMS
  serial↔tag/asset_id same-row set exist in the graph **at onboard** — zero
  query-time proposals needed for them; re-onboarding is the acceptance gate.
- Value-overlap direct writes are sampled evidence without a human gate — a
  skewed sample could write a wrong internal JoinPath. Bounded by: blast
  radius of one system, the cardinality + measure gates, `overlap_*`
  provenance stored on the node, and idempotent MERGE ids (re-onboard heals).
- New graph shapes: internal JoinPaths with `method='value_overlap'`
  (+ transform, overlap provenance) and `transform='same_row'`,
  `method='same_row_identifier_set'`, `from_table == to_table`.
- ADR-0040-additive: no existing edge or payload changes; `IDENTIFIED_BY`,
  the D5 MAP-UI payload, and the curated concept path are byte-identical.
- The table-aware degeneracy refinement re-admits same-system different-table
  proposals from ALL emitters, including the churny query-time ones —
  mitigated in the same release by the measure gates and the
  [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) lifecycle damping.

## Related

- [ADR-0040](0040-dynamic-entity-and-identity-for-any-system.md) — the
  `IDENTIFIED_BY` PRIMARY identity key this ADR deliberately does not widen;
  this ADR is additive to its graph shape.
- [ADR-0003](0003-unified-proposal-lifecycle.md) — the proposal lifecycle that
  weaker-evidence candidates still flow through.
- [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) — lifecycle-level
  damping that backstops the degeneracy-gate refinement made here.
- [ADR-0043](0043-per-value-to-rule-promotion.md) — the queue-side complement:
  what onboarding-time capture misses accumulates as per-value rows and is
  promoted to rules.
- [ADR-0034](0034-evidence-based-concept-suggestion-at-map-time.md) —
  value-overlap evidence as an onboarding-time signal (precedent for sampling
  at provision).
- [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md) — graph
  behavior (the new nodes) is verified against a deployed environment.

## Amendment 2026-10-06: one measurement per field pair in a sweep

**Observed (a fresh workshop deployment's seed, `SEED_TIMING_JSON`).** Phase 2 took 925 s
against about 7 minutes on an earlier fresh deployment. The Indianapolis MES resolve
spent 339 s in join discovery: 214 s in 1390 containment probes and 97 s in
327 graph reads; the SiteWise source 357 s, 227 s of it in 862 graph reads.
`discover_joins_for_system` runs `discover_pair(new, peer)` and
`discover_pair(peer, new)`, which finds a join whichever side was onboarded
first; but the transform ladder and the containment probe are symmetric in
their two arguments, so the second direction re-ran every probe and re-read
every field list for the same verdict.

**Decision.** A sweep (`discover_joins_for_system`, `discover_all_joins`)
carries one `_SweepMemo`: a field pair's transform verdict is keyed on the
unordered pair and measured once (a `None` verdict too), and each system's
identifier and timestamp field lists are read once. Emits are not memoised:
each direction still proposes exactly what it proposed before, so the graph
sees the same proposals. `discover_pair` called alone measures for itself as
before. Expected on that deployment's shape: probe time roughly halved on every
phase-2 resolve; the emit-gate graph reads (most of the SiteWise source's
862) are unchanged and remain the next thing to read in the timing summary.
Why phase 2 varies between accounts (7 min on the earlier deployment, 15 on this one) is
not settled: the probe count scales with the identifier-role assignments the
onboarding model makes, which differ run to run, and the earlier deployment predates the
timing summary.

**Correction 2026-10-07 (post-merge review).** The ladder and the containment
probe are NOT symmetric in their arguments: the affix rungs try the first
argument's affix first, a cleared-but-degenerate rung aborts the whole ladder,
and the containment probe tries the second argument's sample first. Measured
on fixtures: with two equally valid prefixes the two orders win on different
`strip_prefix` labels, and a first-side affix that lands on short sequence
numbers aborts one order while the other clears on the second side's affix.
A verdict cached on the unordered pair therefore changed the reverse proposal
(its transform, or whether it was proposed at all). The memo now keeps the IO
only: each containment probe (system, table, field, sample) and each system's
field lists, once per sweep; the pure ladder runs for each direction, so each
direction proposes exactly what it proposed without the memo. A probe both
directions need still runs once, which is where that deployment's repeated work was. The
saving is "roughly halved" only for a pair where neither sample is found in
the other side (four probes become two); when the first probe already
clears, three become two.

## Amendment 2026-10-13: a shared concept alone doesn't make a same-row set

**Observed.** Onboarding the Indianapolis CMMS in the workshop showed
`work_orders.crew -> work_orders.reported_by (same row identifier set)` under
Connections Discovered. `crew` holds a crew code (`IND-MECH-A`) and
`reported_by` a person (`R. Roe`, `OP-0101 via MES`). The onboarding model
can tag both `operator`, and D1 paired any two identifier columns of one table
that got the same concept, so the graph claimed a crew code identifies the
person who reported the work order. A shared concept says two columns name
the same kind of thing; it doesn't say they name the same thing on each row.

**Decision.** A pair whose only evidence is a shared concept is written when
the table's rows read 1:1: over the rows where both are set, every `a` value
appears with one `b` value and the reverse
(`COUNT(DISTINCT a) = COUNT(DISTINCT b) = COUNT(DISTINCT (a, b))`), over at
least 3 distinct pairs (the same number as the identity-rule bar). Below 3
pairs any two columns read 1:1. Both-unique pairs are unchanged and not
measured: the DDL already proves them.

The read covers the whole table, so the result doesn't depend on row order.
All of a table's concept-only pairs are measured in one read, so a provision
makes one extra read per table that has such pairs. Three cases write no edge
and are reported in the provision payload as `same_row_not_measured`
(`[{table, reason, pairs}]`):

- `failed`: the read failed (also sets `same_row_measure_read_failed`, like
  the UNIQUE read's marker);
- `too_large`: the table has more than 10,000 rows;
- `pair_cap`: the provision already measured 100 pairs (tables and columns
  in name order). The mapping is model output, so a broad concept on a wide
  table could otherwise make the read list grow with the square of the column
  count.

**Effect, measured on every seeded schema with the live story written
(local PostgreSQL, all 10 seed files plus the ERP database).** Every pair of
non-measure columns in every table (4,996 pairs, the widest set a mapping
role of `identifier` could admit) was read: no table is over the row limit,
no read failed, and the slowest table read took 0.15 s. The pairs D1 exists
for read 1:1 and stay: `cmms_maintenance.assets` asset_id / asset_tag /
serial_number / asset_name (40 distinct pairs each) and the plant CMMS
`assets.asset_num` / `serial_number` (6). `work_orders.crew` / `reported_by`
don't (3 crews, 4 reporters, 4 pairs). The gate only removes writes; it never
writes a pair D1 didn't. A 1:1 reading is still a fact about today's rows: two
columns that happen to be 1:1 and share a concept pass, as they did before.
