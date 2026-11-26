<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0087: Exact-agreement cross-system joins are derived from measured evidence

## Status

**Accepted: 2026-09-29.**
Verified offline (unit lane) and on the PG lane (`tests/integration/test_exact_affinity_pg.py`).
**Amended 2026-09-29** (concept gate = SHARE a concept; see "Amendment" below)
after the first live re-onboard derived nothing. **Noted 2026-09-29**: the
site run view is now a realistic MES run view (see "Note 2026-09-29" at the
end). **Amended 2026-09-30**: catalog-related identifier concepts agree
(see "Amendment 2026-09-30" at the end). **Amended 2026-10-07**: `work-order`
~ `batch` is a related pair too (see "Amendment 2026-10-07"). **Amended
2026-10-15**: the seed names which field holds which identifier form (see
"Amendment 2026-10-15"). **Amended 2026-10-18**: the existing side admits
demoted identifiers too (see "Amendment 2026-10-18"). **Amended 2026-10-21**:
`production-order` is related to `production-run` and to `batch` (see
"Amendment 2026-10-21"). Amends
[ADR-0085](0085-live-story-clock-history-and-lookup.md) (the MES->ERP
traceability hop) and extends [ADR-0065](0065-unattended-provisioning-fresh-account-findings.md) /
[ADR-0086](0086-identity-rule-wildcard-for-one-to-many-cores.md) (identifier affinity).

## Context

The live story needs one hop the engine could not see: the MES run number
equals the ERP shipment line's `batch_id`. Both columns hold the SAME format
(`RUN-<date>-<n>`), so the affinity pass refused it by design (a same-format
pair has no pattern to infer), and the history shipped only ONE run, so every
value-count floor (`JOIN_MIN_OVERLAP_COUNT = 5`) was out of reach. The first
fix was a curated, pre-accepted JoinPath seeded by the demo addon. That is a
hardcoded answer: the product principle is that the semantic layer derives
this hop from evidence, or a steward accepts it.

## Decision

1. **Realistic evidence, not lowered thresholds.** The live-story history
   ships every completed run (one delivered order each, the site's customers in
   rotation, `shipment_items.batch_id` = the run number), as a plant does. A
   fresh deploy has 19 shipped runs.
2. **A third affinity pass, `identity_affinity.exact_rule`.** It runs only when
   a membership probe is injected and the pattern passes found nothing, and
   emits a same-format rule (`source_pattern == target_pattern == ""`, method
   `value_exact_match`, transform `identity`, computed reasoning) ONLY when:
   - both fields SHARE at least one MAPS_TO concept, of any role (not
     `unknown`/`needs_review`; see the Amendment). The identifier role stays
     the CANDIDACY filter — a field is a join-key candidate only if it has an
     identifier-role mapping — but agreement is the set intersection. The
     rule names the shared concept (several: the new side's most confident,
     tie -> lexically first; superseded by the 2026-10-18 amendment, which
     ranks on both sides' confidence);
   - both columns share one dominant literal value shape (digits `9`, letters
     `A`, delimiters kept; held by at least half the distinct values, no tie)
     that is not all digits and has at least two segments;
   - at least `JOIN_MIN_OVERLAP_COUNT` exact shared values, confirmed by the
     probe (`resolve_evidence.probe_field_values`; a probe that cannot answer
     fails closed);
   - `passes_key_cardinality` holds and `is_sequential_integer_set` is false for
     both sides (the existing join-discovery floors, reused).
3. **Same review point, same writer.** The rule joins the resolver's list at the
   Identity Rules checkpoint and is written by provision c/c2 like the
   frozen PLM<->ERP rule: `ir-<sys>-<digest>` + `jp-<sys>-<digest>` with
   FROM/TO_SYSTEM edges, `jp.method = 'value_exact_match'`, `jp.transform =
   'identity'`, `jp.origin_context` = the evidence text. It replaces a
   same-format LLM rule for the same pair (the measured version of the same
   claim); an LLM pattern rule that transforms the value still wins (an
   identity-shaped one does not — Note 2026-09-30).
4. **Caps and guards.** At most one exact rule per (source field, target
   system): most shared values, then (target table, field) (since the
   2026-10-18 amendment: one rule per pair of physical columns, every
   qualifying pair kept). Onboarding
   (`onboard._exact_rule_refusal`) refuses an exact rule between two systems
   of the same class at DIFFERENT sites (two MES sites; sites are
   `metric_multisite.site_of`, the P8 site notion) — ADR-0051 P8 / ADR-0052: a
   shared site-local format is not identity; the P8 reason is reported when it
   fires. Same class at the same site is allowed. It FAILS CLOSED on an
   unknown class on either side — empty, absent, or the `ConnectRequest`
   default `Custom` — and on a same-class pair with an unsited (empty or
   wildcard) side. Every refusal is logged with its reason.
   When an exact rule replaces an LLM rule, the resolve logs the count
   (`replaced N LLM rule(s) with exact rule(s)`). Every resolve logs ONE
   bounded INFO summary of the pass, counts only (never values):
   `exact pass: N pair(s) considered; refused concept=… shape=…
   probe/cardinality=…; qualified=… emitted=…` (`identity_affinity.ExactPassStats`;
   `qualified` is before the per-(field, system) cap, `emitted` after).

## Invariants touched

- Replace-not-accrete (ADR-0052): the `jp-`/`ir-` rows are onboarding-owned, so
  the provision of either endpoint deletes and rewrites them.
- Machine claims never vouch (ADR-0052): the pass never joins two same-class
  systems at different sites, and fails closed on an unknown class.
- ADR-0044: `agents/` carries no demo literal; the pass is content-free.
- Existing rules are byte-identical when no probe is passed; the frozen story
  gains no exact rule (PG lane).
- New project invariant: no curated cross-system joins or identity rules ship
  in the seed.

## What would have been a cheat

- Seeding the join (the removed `agents/demo/curated_joins.py` and
  `self_learning.seed_preaccepted_join_paths`), or prompt text asserting it
  (the removed `batch` overlay sentence, which was also WRONG:
  `lot_mes_tracking.batch_id` holds MES work-order numbers).
- Lowering `JOIN_MIN_OVERLAP_COUNT` (or a special K) until one shipped run
  counts.
- Joining on a shared concept alone: `lot_mes_tracking.batch_id` and the run
  number share the `batch` concept and no value.
- Adding `batch` as an identifier mapping of the run number by hand, or
  seeding the concept assignment, to force the old best-concept gate open.

## Known limits

- **The probe is PostgreSQL-only.** `resolve_evidence.probe_field_values`
  answers only for `PostgreSQL` connections; for an Athena or MCP system it returns
  `None`, so the pass fails closed and no exact rule is emitted for that pair
  (the pattern passes are unaffected).
- **`dominant_shape` is length-sensitive.** The literal shape keeps every
  digit position, so a column whose numbers vary in width (`RUN-9-1`,
  `RUN-10-12`) can have no majority shape, or a different one than its
  partner. This yields false negatives only (a real join is missed and left
  to the steward), never a false join.
- **A second true join exists in the demo data but is not emitted.** With the
  concept gate removed, exactly two candidates pass every other gate (PG
  lane): the run-number hop, and `erp lot_mes_tracking.batch_id <-> mes-ind
  work_orders.work_order_number` (the column holds MES work-order numbers).
  The second is a real join, and appears only if onboarding maps both columns
  to ONE concept; it names them `batch` and `work-order`, so the concept gate
  holds it back. (Resolved by the 2026-10-07 amendment: `work-order` ~
  `batch` is now a related pair.)

## Verification

- Unit: `tests/unit/test_identity_affinity_exact.py`,
  `tests/unit/test_onboard_rule_writer_quality.py` (writer, delete predicate,
  same-class/site guard incl. the `Custom` default, probe wiring and its
  schema fallback, the replacement log).
- PG lane: `tests/integration/test_exact_affinity_pg.py` samples and probes
  every text column of the site MES and ERP view schemas with the engine's own
  code (the probe is `onboard._affinity_probe`). Since the 2026-10-07
  amendment exactly two hops qualify, each seen from both resolves (four
  directional rules): the run-number hop (19 shared values) and the
  work-order lot hop; with the concept gate removed the candidates are the
  same two pairs; nothing for the frozen plant MES; the raw
  `discover_transform` identity rung clears (19 shared, ratio 1.0).
- The PG lane models the LIVE concept assignment (run number ->
  `production-run` identifier 0.95 + `batch` correlate 0.85; work-order
  number -> `work-order`; both `batch_id` columns -> `batch`) and yields the
  run-number rule and the work-order rule, both named `batch` (the declaring
  concept); the concept-neutral pin stays at two candidates.
- Unit: `tests/unit/test_exact_affinity_concept_set.py` (full concept set on
  both sides through the resolve seam, the summary line, the pre-fix refusal
  pinned as `refused concept=1`).
- Not verified here: the Neptune `OPTIONAL MATCH … collect(DISTINCT …)` that
  gathers an existing field's concept set (the graph is VPC-private); check
  the summary line after the next re-onboard.

## Amendment 2026-09-29: the concept gate is a SHARED concept

**Evidence.** The first live re-onboard of `mes-ind` derived
no exact rule and logged no refusal. Onboarding had mapped
`erp shipment_items.batch_id` -> `batch` (identifier 0.9) and
`mes-ind production_runs.run_number` -> `production-run` (identifier 0.95)
AND `batch` (correlate 0.85). The pass compared only each field's BEST
identifier concept (`production-run` vs `batch`) and refused. The vocabulary
itself (`agents/catalog.py`: `production-run` is "a production run or batch
execution", and `batch` lists `run_number` among its field names and notes
one system's run number can equal another's batch number) says they are the
same thing under different field names — the mapping recorded exactly that
as a correlate.

**Decision.** Concept agreement means the two fields share at least one
MAPS_TO concept of any role; `unknown`/`needs_review` never count. Each side
carries its full concept set (`SampledField.concepts`, excluded from equality
like `schema`): the new system's from every mapping in the resolve body, an
existing system's from every MAPS_TO edge of the field. The identifier role
remains the candidacy filter, and every other gate (shape, probe floor, key
cardinality, 1..N, the cross-site guard) is unchanged — a shared concept is
still not evidence on its own. Pattern rules and every probe-less result are
byte-identical. The live run also had no log line to diagnose from, hence the
one-line summary in Decision 4.

## Note 2026-09-29: the run view is a realistic MES run view

The body above is unchanged. Each site MES view schema's `production_runs`
(`agents/demo/site_views.py::_site_runs_view_sql`, identical in
`lambda-db-init/handler.py::_site_runs_view_sql`) is now the ERP production
order the MES pulls, not identifiers only:

```sql
CREATE OR REPLACE VIEW {schema}.production_runs AS
SELECT pr.run_id AS production_run_id, pr.run_number, p.sku, pr.planned_quantity, pr.status,
       pr.actual_start_time, pr.actual_end_time, pr.line
FROM public.production_runs pr JOIN public.products p ON p.product_id = pr.product_id
WHERE pr.run_id IN (SELECT w.production_run_id FROM public.mes_work_orders w WHERE w.site = '{site_code}');
```

- **Column order is a constraint.** PostgreSQL `CREATE OR REPLACE VIEW`
  accepts new columns only at the END, so the first two keep their name,
  order and type and the view replaces the 2-column one in place (existing
  accounts: `POST /api/admin/migrate/erp-mes-views`). Reverting to 2 columns
  cannot use `CREATE OR REPLACE`; it needs `DROP VIEW` then `CREATE VIEW`.
  No product UUID is exposed, so no new `*_id` competes with the join
  heuristic.
- **No new join qualifies** (PG lane,
  `tests/integration/test_exact_affinity_pg.py`): `status` and `line` hold
  single-segment values and are refused on shape; `sku` shares fewer than
  `JOIN_MIN_OVERLAP_COUNT` (5) values with the ERP part catalog (the plant
  builds at most four machined SKUs) and is refused by shape or the probe.
  The only qualifying pair is still the run-number hop, 19 shared values.
- **Risk, verified only by a live re-onboard.** The wider view may change
  the onboarding model's concept assignment for `run_number` (today
  `production-run` 0.95 plus the `batch` correlate 0.85). If the `batch`
  correlate is lost, the shared-concept gate refuses the hop and module 2
  loses the shipment leg. Check the resolve's `exact pass: ... qualified=1
  emitted=1` summary line after re-onboarding `mes-ind`.

## Amendment 2026-09-30: catalog-related identifier concepts agree

**Evidence.** The risk above happened. After the widened view was migrated
and `mes-ind` re-onboarded on the live account (one-clock walk step 5), the
resolve logged `exact pass: 640 pair(s) considered; refused concept=581
shape=54 probe/cardinality=5; qualified=0 emitted=0` (with the
2-column view: `1280 ... qualified=1 emitted=1`). The live graph:

| field | concept | role | confidence |
|---|---|---|---|
| `mes-ind.production_runs.run_number` | `production-run` | identifier | 0.95 |
| `erp.shipment_items.batch_id` | `batch` | identifier | 0.90 |
| `erp.lot_mes_tracking.batch_id` | `batch` | identifier | 0.90 |
| `erp.inventory_transactions.batch_id` | `batch` | correlate | 0.85 |

The model no longer draws the `batch` correlate for `run_number`, so no
concept is shared. The halved pair count has a separate cause: `mes-ind` now
has 5 identifier-role fields in the graph. 640 / 5 = 1280 / 10 = 128
existing candidates, so the model gave fewer `mes-ind` columns the
identifier role. The candidacy filter and the concept collection
(`_mapping_concepts`, `_affinity_existing_fields`) are correct: they already
carry every MAPS_TO edge, and the graph holds only one edge for the field.

**Decision.** The concept gate is met by a shared concept, or, when none is
shared, by a pair of identifier concepts the vocabulary declares
interchangeable across systems. The declaration is structured data in
`agents/catalog.py::RELATED_IDENTIFIER_CONCEPTS`: `(concept, declaring
concept)` pairs, checked at import (two distinct core concepts). Its one
entry, `("production-run", "batch")`, comes from the existing definitions.
`batch` says "one system's run_number can equal another's batch_number", and
`production-run` is "a production run or batch execution" keyed by
`batch_number`. The relation is symmetric, not transitive, and read, never
inferred from prose or model output.

- A shared concept still wins (same rule, same reasoning as before).
- A related-pair rule names the declaring concept (`batch`), and its reasoning
  says `concept batch (catalog-related production-run)`, so the steward can
  see the agreement was not a shared id.
- Every measured gate is unchanged: shape, probe floor, key cardinality, 1..N
  and the same-class cross-site guard. A related pair is no more evidence on
  its own than a shared concept.

**Verification.** PG lane (`tests/integration/test_exact_affinity_pg.py`)
now models the widened-view assignment (`run_number` -> `production-run`
only). It yields exactly the run-number hop in both resolve directions (19
shared values, `qualified=2 emitted=2`), and the concept-neutral pin stays
at two. With the relation emptied, the same lane reproduces the live result
(`qualified=0 emitted=0`). Unit tests: `test_identity_affinity_exact.py` and
`test_exact_affinity_concept_set.py`.

## Note 2026-09-30: an identity-shaped LLM pattern no longer blocks the exact rule

A re-onboard (frozen-12201093 step 4) emitted one exact rule, but the LLM
had proposed `PN-*` -> `PN-*` for the same pair. `_replacement` let an exact
rule replace only an LLM rule with EMPTY patterns, so the LLM rule was kept
(`onboarding-llm`, calibrated 0.35) and the JoinPath was written as
`foreign_key_match`. The previous run had empty LLM patterns and got
`value_exact_match` 0.85, so the result depended on LLM output.

An exact rule now replaces the LLM rule for its (source field, target
system, target field) whenever `identity_affinity.patterns_add_nothing`
holds: both sides empty, the same text on both sides, or each side empty or
a pure placeholder (`*`, `{*}`, `{id}`). A pattern pair that rewrites the
value (`AB-{id}-REV-{*}` -> `{id}`) is still kept. The resolve log line
`replaced N LLM rule(s) with exact rule(s): …` names each replaced rule and
why. Unit pins: `test_identity_affinity_exact.py` and
`test_onboard_rule_writer_quality.py` (the exact rule is written at 0.85).

## Amendment 2026-10-02: a demoted identifier is an exact-pass candidate

**Evidence.** A fresh workshop deploy (seed of 2026-10-02)
emitted the run-number hop for one of four site MESs, and an in-place
re-onboard of a long-lived deployment that morning for two of four. The
resolve log never failed a gate for the missing three: their run number was
never a candidate. The stored mappings on the fresh account:

| system | `production_runs.production_run_id` | `production_runs.run_number` | hop |
|---|---|---|---|
| mes-det | unmapped | `production-run` identifier 0.95 | emitted |
| mes-cr | unmapped | `production-run` identifier 0.95 | considered, not emitted (a measured gate; the log counts, it does not name the pair) |
| mes-ind | `production-run` identifier 0.95 | `production-run` correlate 0.90 | never considered |
| mes-mil | `production-run` identifier 0.95 | `production-run` correlate 0.90 | never considered |

On every site where the model gave the surrogate `production_run_id` the
identifier role, it gave the business key `run_number` the correlate role
for the same concept. So whether a measured join was even tried depended on
a role the model assigns differently from run to run.

**Decision.** A mapping of the system being resolved whose only roles are
correlate, to a concept another column of THE SAME TABLE holds as
identifier, is an exact-pass candidate (`onboard._is_demoted_identifier`).
It is sampled like any identifier field and marked
`SampledField.exact_only`: the pattern passes never see it, so pattern rules
are byte-identical and only the exact pass's measured gates (concept, shape,
probe floor, key cardinality, 1..N, the cross-site guard) can join it.

- A correlate whose concept no column of its own table holds as identifier
  stays out (a foreign key such as `work_orders.production_run_id`, a
  description), as does every measure and derive mapping.
- Only the new system's side widens. The existing side is still read with
  `r.role = 'identifier'`; the workshop seed's phase-2 re-resolve runs every
  system as the new side once all are onboarded, so the hop is found from the
  MES side. A steward onboarding one system after the others does not get the
  reverse direction (new identifier vs an existing demoted correlate).
  (Superseded by the 2026-10-18 amendment: the existing side widens too.)
- The surrogate itself is still refused on shape (a 1..N enumeration).

**Verification.** Unit: `test_exact_affinity_demoted_identifier.py` models
the fresh-account assignment end to end through the resolve seam (the run
number joins the shipment line, the surrogate is refused on shape, summary
`2 pair(s) considered; refused concept=0 shape=1 ...; qualified=1 emitted=1`)
and pins that an exact-only side never reaches the pattern passes. Live: a
re-onboard of `mes-ind` on a deployed account must log `qualified>=1` and the
preflight `exact_join` check must pass.

## Amendment 2026-10-06: the seed names the owners of an exact-join identifier

**Observed (a fresh workshop deployment).** A question naming a run number in
the exact join's shape, worded "any batch that drew the same supplier lot as
production run RUN-...", fanned out to every MES that shares the
production-run concept (seven calls, three of them to plants whose run
numbers never match) and once returned a different table built from other
plants' numbering. The lot-worded question routed correctly. The exact rule
was in the seed, but an exact rule translates an identifier to itself, so the
deterministic IDENTIFIER TRANSLATIONS block (ADR-0086) had nothing to render
and the seed said nothing about the identifier at all.

**Decision.** The exact rule carries one measured fact about what its
identifiers look like: the dominant literal value shape both probed columns
share, written into its reasoning (`both columns shape AAA-99999999-99999`)
and persisted as the rule's `origin_context`, which the route seed reads back
as `provenance`. `identity_affinity.exact_shape_from_reasoning` is the exact
inverse of that clause and lives beside the writer so the two drift together
(a round-trip test pins it). `identifier_translation.find_owners` scans the
question for identifier-like tokens (two or more alphanumeric segments, the
exact pass's own floor) whose `literal_shape` equals an exact rule's shape and
renders an IDENTIFIER OWNERS block after the translations: the token, the two
fields that key it (`mes-ind.run_number = erp.batch_id`), the method and
confidence, and the rule "delegate to these systems first; widen to another
system that merely shares the concept only when they report no match".
Routing stays the model's; the block is evidence, capped like the
translations block, and absent when no token matches, so every other seed is
byte-identical. No rule property changes, so accounts seeded before this
change get the block too.

**Not decided.** A shape is not a membership test: several plants' run
numbers can share one shape, and the block says only which systems a
confirmed exact join proves key that shape. A per-field shape index for
systems without an exact join would need a new property on Field nodes and is
not done here.

**Correction 2026-10-07 (post-merge review).** The block's header had said the
same value "is a key in BOTH systems", which a shape match does not show. It
now says each identifier has the shape measured on both columns of a confirmed
exact join and that a matching shape does not show the value exists in either
system; the ask-these-first, widen-on-no-match rule is unchanged. The block is
also kept inside the route-seed budget: `build_route_seed` passes the room the
8,000-character budget leaves after the seed, and the owners block (advisory
routing) keeps its first owner lines that fit plus one "N more identifier
owner(s) omitted" line, or is left out. The translations block and the
wildcard note keep their ADR-0086 contract (additive, capped at
`TRANSLATION_CAP` lines).

## Amendment 2026-10-07: work-order ~ batch is a related identifier pair

**Evidence.** The golden lot-to-supplier question ("for the 5 most recently
completed work orders, which lots and suppliers went into them?") passed 23
of 27 runs on a deployed workshop. Traces of a failing and a passing run (on
2026-10-05) took the same first path: the only MES -> ERP join in the graph
was the run-number hop, so both sent RUN numbers to the ERP for lots. The
ERP's lot-tracking rows key on `lot_mes_tracking.batch_id`, which holds the
MES **work-order number** (the "second true join" under Known limits). The
passing run found the lots only on a retry with work-order numbers; the
failing run stopped and answered that the ERP records no lot linkage, which
is false. The live graph maps `mes-*.work_orders.work_order_number` ->
`work-order` and `erp.lot_mes_tracking.batch_id` -> `batch`, so the concept
gate refused the pair on every onboarding. Two misses in five runs on a
deployed workshop (2026-10-07) followed the same path: one answered "no linkage" after
sending RUN numbers to `lot_mes_tracking`, the other built `WO-` numbers from
the run numbers' digits, matched another plant's orders, and the orchestrator
withheld the answer. Both runs had to guess the key because the graph never
named it.

**Decision.** `RELATED_IDENTIFIER_CONCEPTS` gains `("work-order", "batch")`:
a lot-tracking record's batch reference is often the work order that
consumed the lot, so one system's work-order number can equal another's
batch id. The relation stays symmetric and not transitive (`work-order` and
`production-run` remain unrelated). No concept definition text changes, so
the onboarding mapping prompt and every metric binding are unchanged. Every
measured gate still decides: shape, probe floor, key cardinality, 1..N and
the same-class cross-site guard. A related-pair rule names the declaring
concept (`batch`) and says `catalog-related work-order` in its reasoning.

**Effect.** On the PG lane the pass now emits exactly the two true joins,
each from both resolves (`qualified=4 emitted=4`): the run-number hop and the
work-order lot hop. The concept-neutral candidate set is the same two pairs,
so the concept gate no longer holds back a true join in the demo data. The
frozen story still gains no exact rule. On an already-onboarded account the
new JoinPath appears only after a re-onboard of an MES (or of the ERP).

**Owners name the table.** The 2026-10-06 IDENTIFIER OWNERS block rendered
an exact rule's endpoints from the IdentityRule, which stores leaf column
names only, so both hops rendered as `erp.batch_id` even though the ERP has
a `batch_id` on the shipment line AND on the lot-tracking row (the ambiguity
that once sent the planner down the wrong table, see the route seed's
table-qualified JoinPath endpoints). `identifier_translation.find_owners`
now takes each endpoint from the exact JoinPath that joins the same two
columns, which carries the table, and falls back to the rule's leaf names
when the seed holds no such path. The two directions of one join (each
side's resolve writes one rule) render as one owner line. No graph property
changes.

**Size.** The owners block now sits inside the route-seed budget (the
2026-10-07 correction above): it keeps the owner lines that fit in the room
the budget leaves and replaces the rest with one omitted line. Table-qualified
endpoints make each line longer (measured on the demo's longest site: 153
characters vs 99 leaf-only), so under budget pressure fewer owners fit; with
no pressure the block's worst case is about 1.5k characters (1,523 vs 1,091)
at its unchanged `TRANSLATION_CAP` (8) lines. This amendment adds work-order
tokens as triggers and does not raise that ceiling; the direction dedupe
halves the lines of any hop whose two rules are both in the seed. Known edge,
not reachable in the demo data: two different exact hops between the same two
systems over the same leaf column names collapse to one owner line (true
before this amendment too; correlating each rule with the `jp-` id its writer
mints would separate them).

**Verification.** PG lane `tests/integration/test_exact_affinity_pg.py`
(10 passed) and `tests/integration/test_story_traceability_pg.py`; unit
`test_identity_affinity_exact.py` (the pair qualifies only on shared values,
never on run numbers, and stays non-transitive) and `test_identifier_owners.py`
(a work-order token names the lot-tracking table, a run token the shipment
line, one line per join; leaf fallback without an exact path). Tests that used
`work-order` as the stand-in "unrelated" concept now use `equipment`.

## Amendment 2026-10-15: the seed names which field holds which identifier form

**Evidence.** The 2026-10-07 amendment gave the graph both true hops between a
plant MES and the ERP: run number -> `shipment_items.batch_id` and work-order
number -> `lot_mes_tracking.batch_id`. Live checks on a deployed workshop
(2026-10-15, 44 runs of the lot-to-customers questions) still sent the
work-order form to the shipment table: 23 of 30 non-golden runs made at least
one ERP shipment lookup with work-order numbers that returned no rows, and
only 44% of delegated shipment questions carried run numbers. The seed's two
IdentityRule summaries read `mes.run_number -> erp.batch_id` and
`mes.work_order_number -> erp.batch_id` (rules store leaf columns), so they
read as one ERP field taking either form; the table lived only on the
JoinPaths, and the prompt asked for one `join_rule` per hop. A question that
needs both tables (which lot a batch drew, then who received every batch that
drew it) was delegated as one ERP request carrying the form its first hop
needed; the ERP's own list of further batches came back in that form and its
shipment lookup found nothing. 5 of the 24 runs with such a miss (goldens
included) were wrong after it: the ERP agent summed every lot of the same
supplier (Quick exact-10, $12.6M) or every shipment of the two products made
from the bar stock (m7b-5, $1.4M to $6M); it turned work-order numbers into
run numbers for every batch of a date range (m7b rerun-2, $848,560); the
answer kept only the failed batch (m7b-4, 30 units, $10,200); and the golden
picked a newer failing run with no ERP lot record and answered that it could
not be traced. Only the first two reported a total above $1M.

**Decision.** `identifier_translation.find_field_forms` reads every exact
rule and JoinPath the graph returned (before budget trimming) and finds each
pair of systems whose exact rules link one concept through several fields
one-to-one, stated from the side whose fields share a name (the holder; the
other side is the source). Fields come from the exact JoinPath of the same
columns, shapes from the rule's own reasoning. Pattern rules (they translate
instead), different sources, a field holding two forms, and leaves no exact
JoinPath tells apart give no line.

Two exact joins show only that each holder field holds some source values,
not that one item carries both forms. So a line also needs per-item pairing
evidence on one side: the fields' tables are one table (one row holds every
form), or every table is joined to the others by a direct internal JoinPath
whose method pairs rows per item (`identifier_translation.PAIRING_METHODS`:
a declared or measured foreign key, a same-row identifier set, or a
steward-confirmed join; a value overlap is not one, since two tables can share
a product or a date without sharing an item). The source side is checked
first, then the holder; with neither the line is left out. On a graph recorded
from a deployed workshop (96 internal ERP joins)
no ERP join links `lot_mes_tracking` to `shipment_items` per item: they meet
only through product-level value overlaps and their own lookups
(`lot_mes_tracking.lot_id` to `material_lots`, `shipment_items` to `products`,
`shipments` and `order_line_items`). The pairing lives on the MES:
`work_orders.production_run_id` is a foreign key to
`production_runs.production_run_id` (`restore/mes-ind-joinpaths-after.json`,
`restore/mes-mil-before.json`; the other sites' run view is built the same
way). The line therefore says to ask the source for the other form:
`- erp batch, from mes-ind, mes-mil: erp_data.lot_mes_tracking.batch_id holds
work_orders.work_order_number values (AA-99999999-99999);
erp_data.shipment_items.batch_id holds production_runs.run_number values
(AAA-99999999-99999); per item the source pairs them by
production_runs.production_run_id = work_orders.production_run_id, so ask it
for the other form` (the two other MES were not in the recorded snapshots).

Lines that differ only in the source share one line naming every source,
merged BEFORE the line cap (`FORMS_CAP`, 8 lines); lines past the cap, and
lines the route-seed room cannot hold, are counted in one `- N more
identifier-form line(s) omitted` line, never dropped silently. The block rides
in the route seed's identifier tail only when the question names the concept,
with room reserved like the owners block; it outranks the owners block (it is
a correctness fact, owners are advisory) but leaves owners their smallest form
when the room holds both, and keeps as many lines as fit, each full if
possible, else compact (the source field by column name only, no pairing
path). When the plain fit already cost a tier-1 rule (so no room was
reserved) and the tail carries no forms line, the same bounded rescue as a
deterministic translation runs: eligible tier-1 rules yield
(`_yield_rules_for_forms`, sharing `_yield_tier1_rules` with
`_yield_rules_for_translation`) until the tail carries the block, never
removing a tier-0 rule, a rule or JoinPath that produced a line, a rule the
question needs, or a system; if that cannot free enough room nothing is
removed and `ROUTE_SEED_FORMS_DROPPED` is logged.

The standing Mode-A prompt's IDENTIFIER FORMS rule says how to use the block:
send each field only its own form; a request spanning several of those fields
carries every form paired per item (got first from the system its line says
pairs them) or is split per field; never edit one form into another; tell the
target to report identifiers that match nothing and never widen the scope; a
no-rows result after another field's form is a wrong-form miss recovered
through the IDENTIFIER-MISS recovery, whose step 1 now takes the forms block
as its lookup. `join_rule` may name one JoinPath per target field a hop uses.

**Not decided.** The system agents' own prompts are unchanged: their
IDENTIFIER-MISS protocol already forbids presenting a different record as the
one asked for and says scope is the caller's job; the widening seen live broke
that rule rather than following an invitation. The forms rule asks the
orchestrator to say so in the delegated question. Pairing is direct only: two
tables joined through a third are not treated as paired.

**Size.** The instruction lives in the standing prompt, so the seed pays for
a 285-character label and the lines: 636 characters whole and 498 smallest on
the workshop fixture (with each plant MES's work-order to run key join added,
as onboarding writes it), 645 and 507 on the graph recorded from a deployed workshop. Without
the hub, and at module 8 with it, the block arrives whole and every
registration base stays whole. At module 6 with the hub the smallest form
arrives and the prose shrink trims the plant MES registration guidance by 194
characters and the sensor system's by 64 (on main this question cuts no
prose there; an identifier question already makes that same trade for the
owners block). Questions that do not name a concept with two forms are fitted
exactly as before.

**Verification.** Unit `test_identifier_forms.py`: the reproduction from a
two-rule fixture; no line without pairing evidence (no path, a value overlap,
the recorded ERP joins, an LLM-proposed join, another system's key join); the
recorded graph's source-side pairing; holder-side and one-row pairing; any
system pair; the negative cases; ten sources on one line and the omission
line past the cap and the room; the tail priority and budget; the workshop
scale; an eleven-system forms-only tier-1 wall rescued by unrelated rules and,
with nothing to yield, logged and left in budget; the prompt rule in both
Mode-A prompts and not in Mode B.

## Amendment 2026-10-18: the existing side admits demoted identifiers too

**Evidence.** A deployed workshop, with an app build that adds only a guard
that does not touch resolve. After a full re-provision of all seven
systems in seed order (ERP first, then the four plant MESs), every MES carried
both batch hops to the ERP, each written by the MES's own resolve. Then the ERP
alone was re-provisioned. Its provision deleted every rule touching it (by
design, see Invariants touched) and its own resolve re-created the hops for one
MES only: `exact pass: 5997 pair(s) considered; refused concept=5856 shape=136
probe/cardinality=0; qualified=5 emitted=5`. The other three got the hops back
only when each was re-provisioned in turn. The mappings on the graph (read
after those re-provisions; the ERP resolve's outcome fits the same assignment
at the time it ran):

| system | `work_orders.work_order_number` | `production_runs.run_number` | ERP resolve wrote the hops |
|---|---|---|---|
| mes-det | `work-order` identifier 0.95 | `production-run` identifier 0.95 | yes |
| mes-ind, mes-mil, mes-cr | `work-order` correlate 0.95 | `production-run` correlate 0.90 | no |

So the hops a provision produced depended on which endpoint was provisioned
last and on the role the mapper gave each MES's business keys: the limit the
2026-10-02 amendment recorded.

**Decision.** `onboard._affinity_existing_fields` reads the identifier AND
correlate MAPS_TO edges of every other system and admits exactly the fields
that system's own resolve admits: each identifier-role edge is a full
candidate, as before, and a field whose only roles are correlate, to a concept
another field of its table holds as identifier, is one exact-only candidate
(`onboard._peer_demoted_identifiers`, built on the same
`_is_demoted_identifier` and `_table_identifier_concepts` predicates the new
side uses). Every measured gate is unchanged (concept agreement, shape, probe
floor, key cardinality, 1..N, the cross-site guard), and an exact-only side
still never reaches the pattern passes, so pattern rules are byte-identical.

Five later steps also depended on which side resolved, or on row order, and
are now direction-neutral:

- **The cap keeps every qualifying pair of columns.** It kept one rule per
  (source field, target system), so with one holder in A and two in B,
  resolving A kept one rule and resolving B kept two
  (`identity_affinity.cap_exact_rules`). Each pair passed its own membership
  probe, and sampled values cannot prove two columns are aliases: a
  one-to-one extension table holds exactly its base table's keys, and both join
  the peer's column. So every qualifying pair is kept, and the cap keeps one
  rule per pair of physical `(system, table, field)` endpoints, in either
  order, for a column sampled more than once (a field with several
  identifier-role edges); that rule has the most shared values, tie broken on
  its concept. On the workshop data no pair is dropped either way (the PG
  lane still yields exactly the four directional batch-hop rules).
- **The merge keeps affinity rules that differ only in their tables.**
  `merge_affinity_rules` keyed affinity rules by leaf column names, so two
  exact joins between one pair of systems over the same column names in
  different tables (a customer code and a supplier code, both
  `external_id`) kept only the first, in row order, and the concept that
  survived changed with the resolving side. Affinity rules are now
  deduplicated against each other on their full physical endpoints
  (`identity_affinity._physical_key`), and each is judged against the LLM
  rule it pairs with as proposed, so their order does not matter. Main had
  this collapse before this amendment.
- **An LLM rule pairs only with affinity rules on the tables it names.** The
  leaf-name pair also paired an affinity rule with an LLM rule, so a
  transforming LLM rule for `customers.external_id -> accounts.external_id`
  won over the measured exact rule for `vendors.external_id ->
  suppliers.external_id` too. An LLM rule that names a table (its
  `<side>_table`, or a table-qualified field, compared without case or a
  schema prefix) now pairs only with affinity rules on that table
  (`identity_affinity._paired_llm`); one that names no table still pairs on
  the leaf names, and a sampled dotted column already keys on its whole
  column. A rule naming the pair's own tables is judged before a leaf-only
  one. An LLM rule naming a table no affinity rule is on is a claim about
  that table: it is kept, and the affinity rules on other tables are added
  beside it (provision then gives each its own id, below).
- **Provision gives two joins that differ only in a table two ids.** The
  `ir-`/`jp-` content digest reads leaf columns, the target system and the
  concept, so a base table's and its extension's rules to one peer column
  merged into one node holding whichever tables were written last. A rule
  whose digest it shares with a rule on other tables now also reads its
  tables (`onboard._mark_table_twins`; a rule's tables are its
  `<side>_table`, else the table of its table-qualified field, compared like
  the merge does); every other rule keeps its id, so re-provision stays
  idempotent and a rule written twice is still one node.
- **The concept is ranked on both endpoints.** `shared_concept` ranked several
  shared concepts on the source field's confidence alone, and peer fields
  carried none, so the concept a rule names (and its `LINKS_CONCEPT` edge)
  flipped with the resolving side. The peer read now returns each MAPS_TO
  edge's stored confidence (`onboard._edge_confidence`), and the shared concept
  with the highest combined confidence of both fields wins, tie lexically
  first. When either field has no confidence for a shared concept the choice is
  lexical. The new side's numbers come from its mapper rows, the peer's from
  the edges its provision wrote from its own rows, so the two directions see
  the same numbers unless a steward changed a confidence at the mapping
  checkpoint. The workshop batch hops keep the concept main writes from the
  MES resolve: `batch` (one shared concept, or the catalog-related pair when
  none is shared). A pattern rule (`value_core_match`, wildcard or one-to-one)
  named its source field's concept, so the same transform was written under
  one concept from one side and another from the other, and
  `_fetch_identity_rules` filters on that edge. Every affinity rule now names
  `identity_affinity.rule_concept`: a shared concept, else a catalog-related
  pair's declaring concept (as the exact pass already did), else the
  lexically first of the two fields' own concepts.

**Related: a rate is never an identity endpoint.** The same ERP resolve wrote
three pattern rules from the ERP's invoice, payment and purchase-order numbers
to a plant MES's `production_lines.capacity_per_hour`, which that plant's
mapper run had mapped as an identifier. Provision's semantic-role gate
(`onboard._reject_identifier_measure`) held only plural time units. A singular
unit now counts as a measure in a name that also has a `per` token, on either
side of it and with any tokens between (`_is_rate_name`: `capacity_per_hour`,
`hour_per_shift`, `units_per_machine_hour`, `unitsPerMachineHour`), so
identifier keys such as `business_day_id` and `shift_hour_id`, which have no
`per`, still pass. The gate's identifier-role
exemption applies only to a rule that names its endpoint's role; the rules the
affinity pass emits name none, so for them the name decides.

**Not decided.** A correlate whose concept no field of its own table holds as
identifier is still not a candidate on either side (a foreign key such as
`work_orders.production_run_id`, a business key whose table has no identifier
for its concept). Both resolve directions now agree on such a column, but
whether it is tried still depends on the mapper's role output. Admitting every
correlate would add an exact rule for every foreign-key column that holds a
key's values (true joins, but many per key, and the IDENTIFIER FORMS block
gives no line for a field held by several columns), and is left for a
separate decision.

**Verification.** Unit `test_resolve_order_independent_candidates.py` (the
reproduction: the ERP's resolve finds both hops for a MES of either
assignment; the ERP's and the MES's resolves write the same rules, compared on
endpoints, concept, kind, shared values and shape; one holder against two, a
base-table key and its one-to-one extension, two joins on one column name under
two concepts in either mapping order, and two shared concepts ranked
oppositely by the two mappers give the same rules from either side),
`test_identity_affinity_exact.py` (the cap, the merge and the concept ranking
in both directions), `test_onboard_rule_writer_quality.py` (two ids for two
joins that differ only in a table, unchanged ids otherwise) and
`test_identity_rule_rate_endpoint.py` (rules from the pass's own builders:
the three rate rules refused, rate names in either order refused, day and
hour keys kept). PG lane `tests/integration/test_resolve_order_independence_pg.py`: on
the seeded live story, both resolves derive exactly the two true hops for
either live assignment, named `batch`, and write the same rules; a synthetic
assignment with two shared concepts per hop names the same concept from either
side.

## Amendment 2026-10-21: production-order is related to production-run and to batch

**Evidence.** A live onboarding of the Tulip MES connector (2026-10-21, on its
own branch). Its "Production Orders" table has a column labelled "ERP Order
ID" whose values are the ERP's production run numbers (`RUN-20261021-00001`
...). The field mapper mapped it `production-order` (identifier 0.95). That
concept shares nothing with a run-number column (`production-run`) or with the
ERP shipment line's `batch_id` (`batch`), and no related pair named it, so the
concept gate refused every pair it was in. No identity rule was proposed, and
the chat answer matched ERP work orders to the plant's runs by timestamp and
line. The ERP view schema has no production run table: its run numbers sit
only in `shipment_items.batch_id`, which every recorded graph maps `batch`.
So an order column reaches an MES run column through one pair and the ERP
through another.

**Decision.** `RELATED_IDENTIFIER_CONCEPTS` gains two pairs:

- `("production-run", "production-order")`, declared by `production-order`.
  Its definition gains one last sentence: "One system's production order
  number can equal another's production run number: the system that executes
  an order often keys its run by the order's number."
- `("production-order", "batch")`, declared by `batch`. Its definition gains
  one last sentence: "One system's production order number can equal
  another's batch number: a plant that builds each order as one batch keys the
  batch by the order's number."

Each pair is declared by the concept that keeps every rule's name where it is.
A rule names the declaring concept. With no relation, `rule_concept` names the
lexically first of the two fields' concepts, which for these pairs is
`production-order` (against `production-run`) and `batch` (against
`production-order`). So a pattern rule between those concepts names the same
concept before and after, and no `LINKS_CONCEPT` edge moves. Declared the other
way, those rules would change concept on the next provision. The meaning fits
the same way:

- `production-order` says where an order's number turns up: the system that
  runs an order often records the run under the order's number. The simulated
  ERP's own production orders are its `production_runs` rows, keyed by
  `run_number`.
- `batch` already says one system's run number can equal another's batch
  number, and it declares the run and work-order hops. An order column that
  holds run numbers meets the ERP at the same `batch_id`, so the rule names the
  same concept as the run hop: `batch`.
- On the recorded graphs `production-order` is used mostly by HighByte
  `*_OrderID` tags and by measure mappings of planned quantities. The only
  PostgreSQL identifier edges are `production_events.quantity_at_event` on two
  MES in one graph, which the shape gate refuses (see the evidence below).
  `production-run`'s definition, by contrast, is read for every MES run
  column. The new `batch` sentence names no field.

The existing hops keep their names. `related_concept` picks the lexically
first declaring concept among the pairs a field pair spans. A run column
(`production-run`) against a `batch` column spans only `(batch,
production-run)`, and a work-order column only `(batch, work-order)`, so both
hops are still named `batch` with the same reasoning label. A run column that
also carries `production-order` spans two `batch`-declared pairs and is still
named `batch`. An order column against a run column that also carries `batch`
is now named `batch` instead of `production-order`; no recorded graph has
such a pair.

What the mapper sees: both sentences come after their definitions' first 140
characters, so the wizard's map prompt (`catalog.concepts_prompt_block`, which
the workshop seed and the onboarding wizard use) is byte-identical. The
untruncated block `catalog.concept_hints_block` (read by the chat onboarding
agent's `map_fields`) shows the two sentences. The core `Concept` nodes'
`description` takes them at the next core-concept merge (a full reset or the
concept backfill; the startup seed only creates missing concepts), and every
prompt reads core descriptions from the curated definitions, not the graph.
The `Fields:` and `Synonyms (…)` clauses are unchanged, so listed-field support,
the coverage-gap analyser, vocabulary fit and the metric vocabulary read the
same text as before.

The relation is symmetric and not transitive: `production-order` stays
unrelated to `work-order`, `sales-order` and `purchase-order`, and
`work-order` stays unrelated to `production-run`. A shared concept still wins
over a related pair. Every measured gate still decides: shape, probe floor,
key cardinality, 1..N, the same-class cross-site guard and the site gate.

**Effect on the seeded data (PG lane, offline).** No exact rule is added,
removed or renamed.

- With every field given one neutral concept, the pairs that pass every
  measured gate across all seeded PostgreSQL systems (the ERP, the four plant
  MES, PLM and the plant maintenance system) are the two batch hops and the
  ERP part number against three PLM item columns. None pairs an order column
  with a run or batch column. A concept relation can only open a pair from
  this set.
- Replaying nine recorded live-graph concept assignments (graph snapshots
  taken 2026-10-15 and 2026-10-16) onto the seeded data gives the same rules,
  with the same concept names, and the same exact-pass summary under the
  relation with two pairs, with three, and with all four. The only
  `production-order` identifier edges on a PostgreSQL system in those graphs
  are `production_events.quantity_at_event` on two MES in one graph, an
  integer counter the shape gate refuses. The other `production-order` edges
  are measure mappings (not candidates) or HighByte tags, which the probe
  cannot answer for, so they fail closed.
- 236 recorded mapper replays of the plant MES never mapped a field
  `production-order` with any role but measure.

**Effect on the Tulip column.** On the seeded data, run numbers in a column
mapped `production-order` now join `erp_data.shipment_items.batch_id` (19
shared values), from either resolve, named `batch`. On this branch the
membership probe still answers only for PostgreSQL, so a Tulip (Tables API)
column fails closed at the probe until the Tulip connector's probe arrives.

**Verification.** Unit `test_identity_affinity_exact.py`, for each pair: it
qualifies and names the declaring concept from either side; with the relation
as it stood before, the same evidence is refused on concept; shape, probe
floor, a failed probe and key cardinality still refuse through it; a pattern
rule between the two concepts names the same concept with or without it. Also:
`production-order` relates to no other concept and `work-order` stays
unrelated to it; a shared concept still wins; the run and work-order hops are
named `batch` under the relation with two, three and four pairs, as is a run
column that also carries `production-order`. The test that pins
`production-run` as unrelated to other concepts now uses `sales-order` where
it used `production-order`. Unit `test_exact_affinity_concept_set.py`, through
the resolve seam: an MES order column joins an ERP run-number column and the
ERP `batch` column; the ERP's own resolve joins an onboarded order column; two
MES at different sites are refused by the cross-site guard after the pass
qualifies the pair, through either pair; two MES at one site are joined. PG
lane `test_exact_affinity_pg.py`: on the seeded data a column of run numbers
mapped `production-order` joins the MES run number and the ERP shipment
line's `batch_id` (19 shared values each), and each is refused on concept
without its pair; the live assignment derives the same four batch-hop rules,
all named `batch`, under the relation with two, three and four pairs; the
concept-neutral set across every seeded system is the five pairs above.

## Amendment 2026-11-07: the engine crosses a stored pairing of two forms itself

**Evidence.** A workshop test-event seat (2026-11-06 and 11-07, Quick
bridge, 10 asks per wording): seven wordings of the module 7b lot question
passed 2 to 6 times in 10; module 1b passed 10 of 10. The question needs the
customers of every batch made from one supplier lot. The ERP keeps a batch's
lot consumption under the MES work-order number (`lot_mes_tracking.batch_id`)
and its shipment lines under the MES run number (`shipment_items.batch_id`);
the MES pairs the two per item (`work_orders.production_run_id =
production_runs.production_run_id`). The 2026-10-15 amendment put that into
the route seed (the IDENTIFIER FORMS line ends "so ask it for the other
form") and into the standing prompt (the IDENTIFIER FORMS rule and step 1 of
the IDENTIFIER-MISS recovery). Every step of the crossing stayed with the
model, and the answers show three places it stopped:

- after a wrong-form refusal it made the recovery's graph lookup, found no
  stored value mapping, and stopped under step 3 ("no stored or rule-based
  translation", m7b_material #09, m7b_now #09);
- it took the ERP's `DECLINED-BY-RULE kind=not_connected concept=batch` as
  final and did not ask again with run numbers it already had from the MES
  (m7b_material #08, m7b_incl #07);
- it asked again and the decline guard refused the repeat, because
  `delegation_decline.brings_new_facts` accepts a carried value only under a
  table label naming the rule's field, and the MES answer labelled the
  column "Production Run" (m7b_lotq_or #09, "blocked every further request
  this turn that referenced batch identifiers").

When the answer did go through the MES for the pairing, it passed 4 of 5
times (round 3).

**Decision.** The delegation tool crosses the pairing itself
(`agents/form_crossing.py`, wired by
`orchestrator._wrap_gateway_client_with_form_crossing` between the decline
guard and the identifier-provenance guard):

1. For the system a delegation went to (the holder), the turn's IDENTIFIER
   FORMS lines are read from the route seed's own facts
   (`identifier_provenance.turn_facts`, `find_field_forms`). Only a line the
   source pairs, with a distinct measured shape per form, counts.
2. The holder's answer must show it could not cross: a wrong-form
   IDENTIFIER-MISS block (a token of one listed form looked up in another
   listed form's field), or a typed `not_connected` decline about the line's
   concept. Then every token of the delegated question and the answer whose
   literal shape is a listed form's measured shape is paired (miss-block
   protocol lines and orchestrator-only engine lines are not read, the
   blocks' tokens are).
3. Each pairing system on the line is asked, concurrently, through its
   delegation tool with the reserved `_adt_pair_forms` argument
   (`agents/form_pairing.py`) and the fixed pairing question, from below the
   turn's own context wrappers, so the call carries nothing else. The
   request holds only the id of the stored direct pairing JoinPath between
   the two forms' tables (`identifier_translation.pairing_edge`) and the
   values. A line whose forms share one table (no JoinPath to name) is not
   crossed.
4. The runtime decides everything else from its own graph and schema, and
   any other request gets a fixed refusal that reads no data:
   - the JoinPath must be stored, active (its own status and its rule's),
     internal, and pair two of its own tables per item
     (`identifier_translation.pairs_per_item`, the one predicate the forms
     block uses too); its stored key columns are the join;
   - the two columns read are the measured form endpoints: on each table,
     the one column a measured exact join (`value_exact_match`, the exact
     pass's transform) links to the same other system for the same concept,
     with that join's measured shape; none, several, a column read with
     two shapes, or one column pair whose joins record two different form
     concepts refuse, every requested value must have one of the two
     shapes, and an endpoint read past its 200-row cap is refused
     (`endpoint_limit`), never cut, since a row past the cap could hide a
     second column;
   - field names, checked first: no column, as the graph spells it (the
     endpoints' and the pairing JoinPath's stored fields) or as it is read,
     may be named like a person-valued or counterparty field of the
     catalog's own vocabulary (`catalog.PERSON_VALUED_FIELDS`: every field
     the `operator`, `personnel` and `customer` definitions list; the
     identifier and name fields the `supplier` definition lists; the bare
     `supplier` field `spare-part` lists, with `vendor` and `customer`;
     and `assigned_to`, `assigned_team`, `operator_badge`, `requested_by`
     and `approved_by`, which other definitions list). A name is split into
     lowercase words at underscores, hyphens, dots and other separators, at
     camelCase and PascalCase boundaries and around digits
     (`catalog.field_words`), and an entry matches as a contiguous run of
     those words, so `assignedTo`, `operatorBadge`, `customerId`,
     `supplierRef` and `vendorId` match and `assigned_total` does not. It
     refuses `person_field`, whatever concept the column maps to;
   - concepts, against a server-owned allowlist of plant-generated
     production records: every concept each form's exact join records (the
     agreed concept and, when the exact pass agreed the form through a
     catalog-related pair, the related concept its label names), and every
     concept each endpoint and key column maps to (onboarding's
     `Field-[:MAPS_TO]->Concept` edges, `onboard._dp_field_row`), must be on
     `catalog.CROSSABLE_CONCEPTS` (below) and, as a second check, not in
     `catalog.PERSON_CONCEPTS` (`operator`, `personnel`, closed under
     `NARROWER_CONCEPTS`); a label must name a pair
     `RELATED_IDENTIFIER_CONCEPTS` declares; each of the four columns must
     have an identifier or correlate mapping. A person concept (retired or
     not) refuses `person_concept`; a core concept off the list refuses
     `concept_not_allowed`; an unknown or custom concept, an undeclared
     label, an unmapped or measure-only column refuses `concept_unknown`;
     more than 64 mapping rows refuses `concept_limit`;
   - each endpoint's own concept: its column must map exactly one live
     concept, and that concept must be one its form's exact join records
     (the agreed concept, or the related one for a catalog-related pair). A
     column mapped to another concept on the list, a parent or a narrower
     one, or to a second concept as well, refuses `concept_mismatch`. On
     the workshop MES the exact pass agrees `work_order_number`
     (`work-order`) and `run_number` (`production-run`) with the ERP's
     `batch` columns through catalog-related pairs, so the joins record
     `batch (catalog-related work-order)` and
     `batch (catalog-related production-run)`
     (`tests/integration/test_exact_affinity_pg.py`);
   - the system's query scope: own physical columns only (the query tool
     reads only its schema) and no declared row exclusion (`row_exclusion`,
     which the query tool enforces and this read cannot carry);
   - as extra layers, not as the classification: a column named like
     personal data (`pii.column_looks_pii`) refuses, and each returned value
     must match its column's measured shape, the identifier grammar and the
     value-level redaction (`pii.redact_pii` leaves it unchanged), or its
     pair is dropped and counted;
   - the read is one SELECT composed with pgdriver identifiers, both value
     lists bound, on a read-only session with a 5 s connect and a 10 s
     statement timeout, under the system's schema-scoped read-only login. It
     returns every joined row that shares a value with a row of a requested
     value (one hop), so the caller sees a value or a counterpart that pairs
     twice. PostgreSQL only; any other system answers `unsupported`.

   What the read returns, then: the identifier columns of a stored
   exact-match join between concepts on the crossing allowlist, read under
   the system's schema-scoped read-only login. The engine does not claim the
   values are free of personal data beyond that classification.
5. The runtime's ASGI seam pops the argument on every call and admits it only
   on the engine's call shape: the call is to the one query tool name this
   runtime registered (below), and after every reserved argument is popped
   the arguments are exactly the fixed pairing question. Any other call
   carrying it, and any JSON-RPC batch carrying it, is refused whole with a
   fixed reply before a tool runs. The identity policy runs first, as for any
   call. The orchestrator's outermost gateway wrapper strips the argument
   from every call its model makes, so a question or a tool result that
   steers the model cannot set it. On the orchestrator side a forms line
   about a concept off the allowlist, or listing a field named like a
   person-valued field, is not crossed at all.
6. A round is complete only when every pairing read succeeded whole, named
   the two expected fields, dropped nothing, and the pairs (over every
   pairing system) give each selected value exactly one counterpart that
   pairs with nothing else. Then the holder is asked again once with the
   pairs appended to its question (one line per item, `value = value`, under
   a header naming the two fields). If that answer still shows a crossing it
   could not make for values not paired yet, one more round runs. A round
   that starts and does not finish complete (a value without a counterpart,
   a value or counterpart that pairs twice, conflicting pairing systems, a
   read that was cut, refused or failed, a re-ask that failed or ran past
   the deadline) returns the delegation's own result unchanged: no pairs, no
   note, counts logged. A finished crossing carries one fixed note
   (`[engine] Identifier forms crossed:`) naming the pairing system and the
   pairs.
7. Bounds: two rounds per delegation and three per turn; at most three forms
   per line; four pairing calls per round and eight per turn; 16 values per
   form; one round's reads together within 30 s. One deadline per turn: the
   gateway client's build plus the 20-minute ask ceiling
   (`turn_limits.ASK_RUN_CEILING_SECONDS`) less 5 minutes for the rest of the
   answer. A round with less than the pairing window plus 2 minutes for the
   re-ask left does not start, and a re-ask is cut at the deadline.

Since the crossing runs inside the decline guard, that guard records only a
decline the delegation's final answer still carries, and the provenance guard
observes the re-ask like any delegation. Pairing calls carry their own tool
use ids; the re-ask keeps the delegation's, so its trace is the one the
delegation shows.

**Which tool name the seam admits.** Exactly the one name this runtime
registered for its query tool, `<tool_name_prefix>query_<safe id>`:
`system_agent._create_mode_a_mcp_server` records it
(`form_pairing.register_query_tool`) as it registers the tool, and
`runtime_asgi._admit_pair_forms` compares the call's name to it; a runtime
that registered none (Mode B) admits no pairing request. Line numbers below
are at this change:

- The cloud entry point. `Dockerfile.runtime:31` starts
  `python -m agents.runtime_app`; its `__main__` (`runtime_app.py:4141-4170`)
  sends every `SYSTEM_ID` other than the reserved ones to
  `_run_system_agent` (`runtime_app.py:174`), which calls
  `create_mcp_server(...)` with no `tool_name_prefix`
  (`runtime_app.py:209-218`) and serves it through
  `serve_mcp_app(mcp, system_id, ...)` (`runtime_app.py:220`), which wraps it
  in the ASGI seam (`runtime_asgi._assemble_mcp_asgi`, `runtime_asgi.py:502`).
  `create_mcp_server`'s `tool_name_prefix` defaults to `""`
  (`system_agent.py:11458`), and the tool is registered as
  `f"{tool_name_prefix}query_{safe_id}"` (`system_agent.py:11391`, `11396`,
  `11401`). So the served runtime has only the bare `query_<safe id>`; the
  gateway adds `<target>___` in front of the target's tools
  (`digital_thread_server.py:17`, `orchestrator._is_excluded_guidance_tool`
  at `orchestrator.py:6307`), which is the name the orchestrator calls
  (`form_crossing.tool_name`).
- Local mode. `AgentManager._start_local` emulates the gateway's prefix,
  passing the system id followed by `___` as `tool_name_prefix`, so it
  registers the prefixed name. `LocalAgentServer`
  (`agents/local_agent_server.py`) serves the FastMCP streamable-HTTP app
  directly, outside the ASGI seam, so there the reserved
  argument is not popped and the agent answers the call as an ordinary
  question; the crossing then finds no whole pairing read and returns the
  delegation's result unchanged.

**The crossing allowlist.** `catalog.CROSSABLE_CONCEPTS` holds only the
production records a plant generates itself: `production-run`,
`production-order`, `work-order`, `batch`, `lot-number`. Import-time checks
keep every entry a core concept, none a person concept, and both sides of
every `RELATED_IDENTIFIER_CONCEPTS` pair on the list (a form's join can
record either); a unit test pins the set. Left out, by meaning (the 60 other
core concepts):

- records that can carry a person or counterparty reference, or that are
  not records the plant generates: `purchase-order` (a supplier or vendor
  id), `sales-order` (`customer_id`), `shipment` (a consignee), `spare-part`
  (its definition lists `supplier`), `material`, `product`, `asset`,
  `equipment-id`. They can be added later only with a classification
  source: a stored, column-level marking that a column's values name no
  person or counterparty, which the catalog does not have today;
- people and counterparties, whose identifiers can name a person:
  `operator`, `personnel`, `customer` (a buyer can be a person; its
  definition lists `customer_name` and `buyer_id`), `supplier` (a vendor can
  be a sole trader; its definition lists `supplier_name` and `name`);
- records whose definitions carry a person field: `shift`
  (`operator_badge`), `engineering-change` (`requested_by`, `approved_by`),
  `maintenance-event` (`assigned_to`), `maintenance-schedule`
  (`assigned_team`);
- events that happen to an item, not the item: `inspection`,
  `non-conformance`, `downtime`, `alarm`, `inventory-transaction`,
  `material-consumption`;
- places: `production-line`, `facility` (its values are plant or city
  names), `site`, `location`;
- definitions and versions: `bill-of-materials`, `specification`,
  `product-revision`;
- money: `invoice`, `payment`, `cost`;
- categories, states, times and measures, which are not record identifiers:
  `asset-class`, `lifecycle-state`, `criticality`, `runtime-hours`,
  `availability`, `performance`, `oee-score`, `quality-metric`,
  `inspection-result`, `defect-type`, `scrap-waste`, `actual-quantity`,
  `quality-yield-rate`, `failure-mode`, `sensor-reading`, `event-time`,
  `cycle-time`, `tool-life`, `food-safety`, `machine-state`, `throughput`,
  `machine-speed`, `target-machine-speed`, `inventory-level`,
  `mro-inventory`, `order-status`, `schedule`, `product-category`.

`work-order` stays on the list although its definition lists `assigned_to`:
that is a field of the record, not its identifier. The field-level check
refuses a column named like it, and the endpoint check refuses a column
whose own mapping is not exactly its form's concept. Adding a concept to the
list is a catalog change; the unit test pins the set.

**Residual risk.** The classification relies on onboarding's stored concept
mappings. A person column mis-mapped under an allowed production-record
concept, with a measured exact join and a matching value shape, and a name
the field rule does not catch, would still pass and be read. The name rule
is a backstop, not a classification: any denylist of names misses some
spelling. The graph's identity rules and join paths already rely on the same
mappings, so a mis-mapping of that kind already misleads them today; the
crossing does not add a new dependency on them.

**Who can call the pairing read directly.** The boundary for a caller that is
not the orchestrator is the query gateway's own AWS-native authorizer, and the
pairing read adds nothing a caller could not already get through it:

- `adt-query-gw` is `CUSTOM_JWT` against the adt-app user pool and accepts
  tokens of the app's login client, the service client and, when edge auth is
  on, the edge client (`stacks/agentcore_stack.py`, `_build_gateway`). The
  login client is public with the user-password flow, so any adt-app user can
  mint an accepted token, and the app keeps the user's access token in the
  user's own session cookie (`agents/auth/routes.py` login,
  `agents/auth/middleware.py` `_encode_cookie`). The service client's secret is
  held by the app's tasks and, in Cognito mode, copied into the Quick
  connector by `scripts/quick-integration.sh`.
- Quick's connector is pointed at the dedicated `adt-quick-gw`, which holds
  only the query-bridge target (`stacks/agentcore_stack.py`,
  `_build_quick_gateway`; `agents/agent_manager.py`,
  `ensure_query_bridge_target`), so Quick itself never lists a system target.
  A person holding the connector's client secret holds a token the shared
  gateway also accepts.
- Every such caller can already call the system's normal `query_<id>` tool on
  the same target, whose agent runs any read-only SELECT over the system's
  whole schema (`system_agent._make_direct_query_tool`) with no column
  withheld. The pairing read returns a strict subset: two measured identifier
  columns of rows joined by a stored key, under the same identity policy.

**Invariants.** No pairing is inferred from a value's text or digits (two
forms of one item can share digits by design, so a digit check fires on
correct pairs): a pair is only what the pairing system's rows hold. No secret
or capability is minted for the pairing read. The standing orchestrator and
Quick prompts are unchanged. A turn with no forms line for the holder, no
route-seed facts, or `RESPONSE_INTEGRITY_MODE=enforce` is byte-identical, as
is every answer without one of the two signals.

**Not decided.** `brings_new_facts` still reads carried values by label, so a
re-ask the model makes itself after a decline can still be refused when the
answer labels the column differently; the engine's own re-ask does not pass
through that guard. A pairing across more than one JoinPath hop, one table
holding both forms, and pairing systems that are not PostgreSQL fail closed.
The forms line needs a measured shape on both rules, and the pairing system's
exact JoinPaths need the exact pass's reasoning (shape and concept); the four
columns need stored key-role mappings to allowlisted concepts. Without them
nothing is crossed, with one exception: a key column with no stored mapping
at all takes its join partner's mapping (`form_pairing._key_mappings`), and
those copies pass the same checks. Onboarding tells the model to leave a
random-UUID surrogate key unmapped, so the key column on one side of the
stored pairing is often unmapped: the recorded Detroit MES
(`production_runs.production_run_id`,
`tests/unit/fixtures/concept_mappings_workshop_systems.json`), and on the
2026-11-11 test event (a fresh Create account) the Indianapolis MES as well,
where every pairing read refused `concept_unknown` and none of 9 crossing
rounds ran. The two key columns name one item, so the partner's concept is
the column's. Both key columns unmapped, an endpoint column unmapped, or a
key column with any stored row (a retired one included) still refuses. The
classification is by the stored concept mappings, with the catalog's field
vocabulary as a backstop (see Residual risk); the catalog has no
column-level sensitivity attribute. A mixed-case column is checked under its
stored spelling, but the read names every column in lower case, so on
PostgreSQL a quoted mixed-case column fails the read and the delegation's
result is returned unchanged. The synchronous ask endpoint's
120 s CDN cut is not known to the orchestrator; a crossing adds one pairing
read and one holder call to such a turn.

**Verification.** Unit `test_form_crossing.py` (neutral schema, through the
real delegation stack and the fully built gateway client: the reproduction;
both signals; the pairing call carries only the join id and the values; a
model call with the argument reaches the gateway without it; every
no-signal, other-concept, holder-pairs, one-table, no-evidence, name-guess,
enforce and no-facts case byte-identical; four of five paired, conflicting
pairing systems, a counterpart or a value that pairs twice, and every reply
that is not a whole read leave the same result object; a failed first or
second re-ask, an incomplete second round, slow reads and a late re-ask do
too; two rounds, the round, call and form caps, the deadline and concurrent
reads; the decline guard after a crossed and a still-declining re-ask),
`test_form_crossing_workshop.py` (the recorded ERP answer of
`fixtures/system_agent_unreturned_names_exact6.json` with the line and block
today's `unresolved_miss` writes for its recorded calls is crossed at the MES
and the ERP's re-ask gives four customers, 110 units and $37,400, reached only
through the MES rows' pairs, one of which shares no digits; module 2 step 4,
the module 7 work-order lookup and the 8c run lookup are byte-identical; the
key join as onboarding measures it is crossed and a contradicted one is not;
the four MES columns' recorded mappings pass the concept check on all four
seats, the Detroit seat by its key column taking its join partner's mapping,
and so do the 2026-11-11 test event's fresh `mes-ind` columns)
and `test_form_pairing.py` (the runtime picks both columns from its own
stored joins; caller-chosen columns, a join that is not a stored per-item
pairing of two own tables, columns that are not the measured endpoints, an
endpoint read past its cap whose 201st row hides a second column, off-shape
values, a person concept on the join or on any of the four columns (a
measured person identifier join that every name and value check passes
included, and a person concept added to the allowlist by mistake), a
`customer`, `shift`, `engineering-change`, `supplier`, measure concept or
any of the eight concepts taken off the list on the join, in a form's label
or on a column, a label naming an undeclared pair, an endpoint mapped to
another allowed concept than its form's or to a second concept as well, two
peers' joins recording two form concepts for one column pair, a column
named for a person or counterparty in snake_case, camelCase or PascalCase
(`operatorBadge`, `assignedTo`, `requestedBy`, `approvedBy`, `customerId`,
`CustomerID`, `supplier`, `supplier_id`, `supplier_ref`, `vendorId`,
`VendorRef`) whatever it maps to (a supplier column mapped only to
`purchase-order` or `spare-part` included), an assignee key column in either
spelling, an unknown, custom, unmapped or measure-only column, too many
mapping rows, a column named like personal data, an unknown column and a row
exclusion are refused with no read; the allowlist, the person-valued field
set, the word split and the whole-word match are pinned as the catalog's
own, and the forms' concepts round-trip through the exact pass's reasoning;
returned values off
their shape or changed by the redaction are dropped; at the ASGI seam the
bare name is admitted against a bare registration and refused against a
prefixed one, the prefixed name the other way round, no registration admits
neither, and the engine's shape on any other tool name, and any batch
carrying the argument, are refused; the Mode A server registers the name it
serves with and without a prefix, the cloud entry point's
`create_mcp_server` call registers the bare name and local `main()` the
prefixed one; through the runtime's public MCP request path, for both a bare
and a prefixed registration, the registered name is answered without the
agent and the other spelling is refused, and the same request on another
shape, another tool name or in a two-call batch is refused). The
orchestrator side's `test_form_crossing.py` also pins that a forms line about
`personnel`, `operator`, `customer`, `shift`, `engineering-change`,
`supplier`, `purchase-order`, `spare-part`, another concept off the list or
a non-core concept, or listing an assignee, approver, customer, supplier or
vendor field in any spelling, is never crossed. Integration
`test_form_pairing_pg.py` runs the
read on PostgreSQL 16: the stored pairs from either form, the one-hop row of
a counterpart that pairs twice, a read-only session, and no read of a
same-named table in another schema.

## Amendment 2026-11-11: a decline is reported in plain words

**Context.** On the 2026-11-11 test event (the 7b lot question ten times
through the Quick bridge, on two fresh seats) the ERP's shape guard refused
a run number looked up in `lot_mes_tracking.batch_id` and the ERP agent
ended with a `not_connected` decline about `batch`. The decline guard's
refusal of the repeat named the reason code and told the model to "report
its decline and reason", and the standing A DECLINE IS FINAL rule said
"Report the decline and its reason in your answer." The answers then told
the user the ERP "explicitly declined (reason: `not_connected`, concept
`batch`)", which reads like a broken connection.

**Decision.** The refusal of a repeat (`delegation_decline.refusal_for`)
names no reason code. It tells the model to ask another system, or to tell
the user plainly, in their terms, what the system could not provide, and
never to quote `[engine]` lines or their codes. The A DECLINE IS FINAL rule
(both orchestrator modes and both Quick prompts) now says "Report the
decline in plain words; never quote [engine] lines or codes." in place of
"Report the decline and its reason in your answer." (Mode A Quick 48,896 of
its 48,900 budget). Wording only: what the guard records, refuses and
reopens is unchanged.

**Verification.** Unit `test_delegation_decline.py` (the refusal carries no
reason code, for each kind) and
`test_orchestrator_absence_and_decline_rules.py` (the rule's plain-words
sentence, in both orchestrator modes and both Quick prompts).
`docs/amazon-quick-skill.md` regenerated.

## Amendment 2026-11-11: a decline reopens only for a form the pairing system's own results returned

**Context.** On the same test event (both teams, the 7b lot question
through the Quick bridge) the ERP kept one batch under two identifier
forms: `lot_mes_tracking.batch_id` holds work-order numbers and
`shipment_items.batch_id` holds run numbers, and only the MES pairs them.
When the model asked the ERP with the wrong form, the ERP's shape guard
refused the lookup and the ERP agent declined `not_connected` about
`batch`. The decline guard recorded it and refused every later ERP
delegation about `batch` that turn, including the right-form re-ask the
model sent after it had fetched the other form from the MES itself. Team 2's
orchestrator log has `DELEGATION_DECLINE recorded tool=erp___query_erp
kind=not_connected` in 2 of its 4 traced turns; Team 1 had `refused a
repeat delegation` twice in one trial. The MES's answer gave the value
under no IdentityRule's field, so `brings_new_facts` could not reopen it.

Three simpler fixes do not hold. Not recording a `not_connected` decline
whenever a crossable forms line names its concept lets a genuine decline
(the records need a source that is not connected) be worded around, since
the decline's kind and concept are the system model's words. Unlatching on a
runtime line that says the shape guard refused a wrong-form lookup in the
call fails because one call can hold that refusal and a genuine decline.
Reopening for a counterpart value present in the pairing system's answer
text fails because the model can put a made-up value of the right shape in
a question to the pairing system, and a truthful negative answer ("no record
exists for job ...") echoes it.

**Decision.** Every decline is recorded. A recorded `not_connected` decline
reopens, next to `brings_new_facts` (unchanged), only for a value the
pairing system's own tool results returned:

- When the shape guard refused a wrong-form lookup in a call that the call
  did not resolve (no single counterpart recorded for the value, and none
  supplied by the delegated question, `unresolved_miss.resolved`), the
  holder's runtime writes `[engine] Wrong-form lookup refused:
  concept=<concept>` right before its decline line
  (`agents/wrong_form_refused.py`; each refusal `unresolved_miss` keeps now
  carries the measured pair's concept). The decline guard stores on the
  decline's record the concepts that line, in that position, names
  (`wrong_form_concepts`, folded; `orchestrator._wrong_form_evidence`;
  never for an error result or under enforce), apart from the decline's own
  concept, which is the model's word and need not be the refused one
  (for example, the guard refused under
  `batch` and the model declined about `lot-number`). It logs whether there
  were any (`DELEGATION_DECLINE recorded ... wrong_form=0|1`). The line
  alone never reopens anything.
- When the caller asks for the identifier-sources line (the same reserved
  argument, which the orchestrator's delegation wrapper sends whenever the
  turn has route-seed facts), every system runtime makes each copy of the
  opening `[engine] Result identifiers v1:` in its text inert (the model's
  answer, a data cell, another engine line quoting a literal, a relayed
  chart tag), then appends `[engine] Result identifiers v1: ["<value>",...]`
  right before the identifier-sources line, which stays last
  (`agents/result_identifiers.py`,
  `system_agent._with_identifier_sources`). It lists, case-folded and
  sorted, the tokens with a letter and a digit (3 to 64 characters) that
  this call's own tabular SQL results returned and that the answer also
  states as a whole token. The record is its own (`result_identifiers.
  record_result`, kept by the capture hook under `STATE_KEY`): only the row
  cells of a successful result with `columns`, `rows` and the
  `_adt_executed_query` the SQL tool stamps, and only under the result
  columns whose source the executed SELECT list proves is a column of one
  of the system's tables (`identifier_provenance._projected_sources`,
  `select_lineage`). A literal, an expression, a derived table, a WITH or a
  set operation proves no column. Every other result adds nothing: the
  shape guard's refusal (it names the model's own query literal in its
  check, its note and its miss block), any other non-tabular JSON, plain
  text, an error, notes and the executed query's text. A result shape gets
  in only when its data fields are listed here. It needs no schema example
  value, so a runtime a restore rebuilt from the graph's field names and
  types (`server._sql_tables_desc_from_graph`) records as one built at
  onboarding. The unreturned-identifier check's record (`answer_ids`), which
  reads any result's text to show what no result returned, is not used and
  is unchanged, so restored runtimes get no new unreturned-identifier
  lines. No line for a call with no such token, a cut record (past 50,000
  tokens) or more than 32 tokens; none under enforce or when not asked.
  Only answer-stated values are listed, because the runtime cannot tell the
  orchestrator from another caller.
- The orchestrator reads the line right outside the identifier-provenance
  guard, after that guard has removed the identifier-sources line, and only
  as the result's last line, in a successful result, in a turn that asked
  (`orchestrator._wrap_gateway_client_with_result_identifiers`,
  `result_identifiers.TurnReturned`). It removes the line from every text
  block and `structuredContent` string before anything else reads the
  result, so the model, the activity trace and the stored conversation never
  see it; it is also in `internal_lines.MARKERS` for any other surface. It
  keeps the values per delegation tool, less every value the delegation's
  own arguments (question and context) hold, so a tool result that echoes
  its request back as data proves nothing.
- A later delegation to the declining system about the concept is sent only
  when `delegation_decline.brings_paired_form` holds: the record is
  `not_connected` with wrong-form evidence; the turn's IDENTIFIER FORMS
  facts for the declining system hold a crossable line for a refused
  concept (`form_crossing.crossings_for`, read at the turn's client build); the new
  question carries an identifier the declined question did not carry, whose
  literal shape is one of that line's form shapes other than the declined
  question's own; and a pairing system the line names, never the declining
  system, listed that exact value. The refusal wording and the A DECLINE IS
  FINAL rule are unchanged.

**What it does not cover.** A form whose values hold no letter is never
listed, so it never reopens a decline. Only tabular SQL results count, so a
pairing system that is not SQL-backed never reopens a decline, and neither
does a pairing query whose SELECT list proves no column: `SELECT *`, a
WITH, a set operation, an unqualified column with a derived table in the
FROM list, two output columns of one name, a relation named with its
database schema (`<schema>.<table>`), or a relation the system's schema
text does not hold. Each of these errs toward leaving the decline in
place. A value the pairing system's tables hold is listed whichever
question selected it, so the line shows that a table holds the value, not
that it pairs with the declined one; it must also be one of the line's
other form shapes and new to the declined question. Because the decline's
concept is the model's word, the engine cannot tell a decline relabelled
from the refused concept from a genuine `not_connected` decline about
another concept in a call that also had a wrong-form refusal: a later
question about it that carries a value of the refused concept's other form,
listed by a pairing system, is sent too. That errs toward one more
delegation, which the system's own rules still answer; requiring the
pairing system's rows to pair the new value with the refused one would
need row-level pair provenance the line does not carry. Only the PostgreSQL
query tool has the shape guard, so a decline from any other system is
reopened only by `brings_new_facts`.

**Verification.** Unit `test_result_identifiers.py` (the record the capture
hook keeps and a real system agent with scripted model turns, on a schema
unlike any demo dataset, in the schema text onboarding writes and in the
one a restore rebuilds: the line lists exactly what a proven row cell
returned and the answer states, never an echoed value or a value the
answer does not state, none for a cut record, past the bound, a cell longer
than the scan, an over-long result text, an error, a call that did not ask,
or under enforce; a shape-guard refusal of a value the pairing system's own
model made up lists nothing and the holder's re-ask with it stays refused;
a value put in a row by a literal, an expression, a derived table, a WITH or
a set operation, or held by non-tabular JSON, plain text or a tabular
result with no executed query, is never listed; a restored runtime with no
schema example value lists its row's two forms and the measured decline
reopens, and its unreturned-identifier check writes no new line; it sits
right before the identifier-sources line; a copy in
prose, mid-answer, with stacked tags, in a table cell, as a JSON escape, or in
text the engine adds after the answer is made inert, also when the
identifier-sources line faults; the reader takes only an asked, successful
result's last line, keeps no value the delegation carried, reads a malformed
line as none and removes the line from `structuredContent` too),
`test_delegation_decline_forms.py` (through the real delegation stack, every
pairing read refused as on the seat: the measured recovery is sent, also
after a decline the model labelled with another concept than the refused
one; each
variant of it is refused while the recovery itself is sent: a narrower
rewording after a wrong-form refusal beside a genuine unconnected-source
decline, a decline citing an unconnected source, an answer that only echoes
a made-up value, a listed value the pairing delegation's question or context
carried, a line that is not the last or is malformed, a value no pairing
system returned, one only the holder or a system off the forms line
returned, one of the refused form, a decline without the wrong-form line
for a concept the forms line covers (also when relabelled), an error
result, enforce mode, `rule` and `withheld_metric` declines,
no forms line and no turn facts; the pairing system's own decline and an
engine re-ask that still declines latch; the line never reaches the model),
`test_wrong_form_refused_line.py` (a real system agent: the line names the
refused concept exactly when the guard refused a value the call did not
resolve, right before the decline line; copies are made inert; through to
the orchestrator), `test_internal_lines.py` (the five orchestrator-only
markers) and `test_system_agent_unresolved_identifier_miss.py` (the stored
trace drops the wrong-form line). `test_form_crossing.py` is unchanged.
