<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0081: Vocabulary drift — a concept imported after onboarding files PENDING field-binding proposals for every onboarded system; no re-onboard

## Status

Accepted — 2026-09-25; implemented (D1–D3 as amended 2026-09-25, D6, manual
route of D4); UI accept-all and daily secondary trigger deferred.
Extends [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) (b)
"schema drift is a proposal class, never a graph mutation" to the
**vocabulary** axis, and reuses the `concept_mapping` proposal type and its
accept writer. Related: [ADR-0021](0021-two-layer-concept-model.md)
(two-layer concept model), [ADR-0068](0068-conversational-onboarding-context.md)
(vocabulary fit is COMPUTED; the onboarding agent never writes),
[ADR-0066](0066-graph-backed-metric-definitions.md) (metric definitions
resolve through concept bindings).

## Context

A system's columns are bound to concepts ONCE, during onboarding
(`map_fields` writes `(Field)-[:MAPS_TO]->(Concept)`). The vocabulary,
however, keeps growing after onboarding: the steward imports concepts
(`POST /api/admin/vocabulary/import`), the onboarding agent proposes new
ones for a later system (`propose_concepts`, ADR-0068), and a semantic pack
adds domain terms in bulk.

Loading the demo pack on 2026-09-24 made the gap concrete. `reprint_segment`
and `finalized_request` were imported (32/32 concepts live) and two metric
definitions that count them were filed. Both definitions are meaningless
until a column of `segment_record_details_raw` is bound to each concept —
and nothing files that binding. Today the steward's options are:

1. `scripts/reonboard.py --systems demo-athena` — the whole-system path:
   re-discovers, re-maps EVERY field, re-provisions the runtime, blocks
   until the gateway target is READY. A full rebuild to add two edges.
2. Wait for a user to ask a question that happens to make the orchestrator
   file a `concept_mapping` proposal through `log_proposed_mapping`.
3. Nothing — the metric resolves `no-data` forever.

Existing machinery that is *almost* the answer:

- `vocab_fit.compute_fit(tables, concepts)` — computed (never LLM) coverage
  of discovered columns by the vocabulary; a report, not a binder.
- `schema_drift.run_drift_check(system_id)` — daily, compares the graph
  baseline (`load_system_schema`) to a fresh discover and files
  `schema_drift` proposals; steward accept acknowledges, never mutates.
- `concept_mapping` proposals — `accept_mapping` → `_repoint_maps_to`
  MERGEs the authoritative `MAPS_TO` edge for one field. The writer exists.
- `POST /api/neptune/graph/backfill-concepts` — materialises Concept
  nodes referenced by existing edges; the inverse problem (edges for new
  nodes) has no home.

The invariants that constrain the answer: the machine never writes
authority (proposals only, ADR-0053); fit is computed (ADR-0068);
re-onboarding stays the steward's explicit fix for *schema* change
(ADR-0053) — but a *vocabulary* change did not change the schema, so
re-onboarding is the wrong tool.

## Decision

### D1 — A vocabulary change triggers a computed rebinding pass over every onboarded system; the output is PROPOSALS

`vocabulary_drift.propose_bindings(concept_ids: list[str]) -> dict` runs
after the vocabulary import writer (`_run_import`) commits and after
`propose_concepts` items are steward-accepted (both already funnel through
`_run_import` per ADR-0068). For each onboarded system
(`schema_drift.onboarded_system_ids()`), it loads the graph baseline
(`load_system_schema` — the same source the drift check uses; NO live
discover, NO Athena) and scores each **unbound** column against the new
concepts only, using `vocab_fit`'s token/synonym matcher.

Matches above the existing `vocab_fit` signal threshold are resolved
one-to-one across `(system, concept)` AND `(system, column)` and each
winner is filed via
`log_proposed_mapping(proposed_type="concept_mapping", source="vocabulary_drift",
to_system=<system>, to_field=<column>, concept=<concept_id>, confidence=<score>,
method="vocab_fit", provenance="import:<slug>")`; outscored candidate
columns and outcompeted concepts are counted VISIBLY in the pass report
(never silently dropped) — see the 2026-09-25 amendment below for why the
assignment is one-to-one on both axes. The full ADR-0053 pipeline
applies: gates, suppression, tombstone check (a concept the steward
rejected for that field within 90 days is NOT re-proposed), staging,
pending. The pass writes nothing else.

### D2 — Only UNBOUND columns and only the NEW concepts are scored; existing bindings are never touched

A column that already has a `MAPS_TO` edge is skipped — the pass adds
bindings, it never re-litigates accepted ones. (Repointing an existing
binding remains a steward action via a `concept_mapping` proposal filed by
the orchestrator or by hand.) Only the concept ids in the triggering import
are candidates; the pass does not re-score the whole vocabulary, so a
20-concept import over 10 systems is bounded by
`new_concepts × unbound_columns`, all in-memory over data already in the
graph. `is_generic_column` exclusions from `vocab_fit` apply (an `id` or
`created_at` column never binds to a domain concept by name alone).

### D3 — The pass is idempotent and replace-not-accrete

Proposals carry the proposal store's pinned `concept_mapping` grain:
subject key `(to_system, concept)` with the chosen column in the target key
(`proposal_key.py` — amended 2026-09-25, below); re-running
the pass (a second import touching the same concept, a manual
`POST /api/admin/vocabulary/drift/run`) supersedes a still-pending
proposal with the same subject instead of duplicating it — the existing
subject-key supersede semantics of the proposal store. Accepted rows are
steward rows and always survive (ADR-0052 replace-not-accrete).

#### Amendment — 2026-09-25: one-to-one across `(system, concept)` AND `(system, column)`; losers are reported, never filed

As originally accepted, D1 read "every match above the threshold is filed"
and D3 named subject key `(to_system, to_field, concept)`. Implementation
surfaced a conflict with THREE pre-existing, test-pinned contracts that
this ADR reuses rather than owns:

1. **The accept writer enforces ONE authoritative column per
   `(system, concept)`.** `self_learning._repoint_maps_to` — the ONLY
   `concept_mapping` accept path, which D4 deliberately reuses — DELETEs
   every prior `MAPS_TO` edge for the `(system, concept)` pair before
   MERGEing the chosen edge, because the resolver reads system-scoped
   `LIMIT 1` and the chosen column must be the SOLE authoritative edge.
   Had the pass filed coexisting proposals for two columns matching one
   concept, D4's "accept all from this import" would accept both: the
   second accept silently deletes the first's just-merged edge while the
   first's store row stays `accepted` — exactly the silent-partial-state
   class of the 2026-07-14 live incident that writer's fail-loud guard
   exists to prevent.
2. **The same writer's R5 column-keyed cleanup enforces ONE live concept
   per column.** After the concept-keyed delete it also drops EVERY other
   `MAPS_TO` edge leaving the chosen column ("the most recently applied
   edge for a column is the only live one" — the S5.2 W2 win-rule). Two
   concepts from one import matching the SAME unbound column (synonym
   overlap is common in a semantic pack) would therefore produce two
   proposals whose accepts destroy each other the same way: both rows end
   `accepted`, only the last edge survives.
3. **The proposal store's `concept_mapping` subject grain is
   `(to_system, concept)`** (`proposal_key.py`, pinned by
   `test_proposal_key.py` and the user-recorrection supersede lifecycle
   tests): a second candidate column on the same subject supersedes the
   first, so "file every match" collapses to last-writer-wins churn that
   feeds the ADR-0042 flip-flop quarantine — not steward review.

Amended decision: the pass resolves matches ONE-TO-ONE across both axes —
a greedy best-score-first assignment over the deterministic candidate
order (score, then concept id, then field, lexicographic) in which an
already-assigned concept OR an already-claimed column drops the candidate.
Every drop is visible in the pass report: an outscored column whose
concept still won another column counts under
`per_system[].skipped.runner_up`; a concept that lost every matched column
to another concept counts distinctly under
`per_system[].skipped.column_conflict` — never silent. A steward who
judges a loser the right binding uses the existing manual correction path
(a USER `concept_mapping` proposal — user wins and supersedes, ADR-0003
D), or rejects the filed proposal and re-runs the pass after the tombstone
decays. Widening the store grain to `(to_system, to_field, concept)` was
rejected: it would detach proposal-store supersede semantics from the
one-edge graph authority model above and make bulk accept destructive.

### D4 — The steward sees the proposals grouped by import, previewable, and accepts them one at a time or all at once

The Self-Learning Discoveries queue already lists `concept_mapping` items.
This ADR adds a `provenance` filter chip ("from import `demo-semantics`")
and an "accept all from this import" action that calls the EXISTING
`accept_mapping` per item — no new bulk writer, no transaction across
items, each accept is its own validate → MERGE → readback. A partial
failure leaves the remaining items pending and reports which.

Each item's preview shows the column's sampled values from the stored
discovery profile when one exists (onboarding already stores a bounded
sample per column) so the steward can judge "does `rp_flag` really mean
`reprint_segment`?" without a query. No live data read for preview in v1.

### D5 — After accept, the affected systems' runtimes are NOT re-provisioned; the binding is live on the next resolve

`MAPS_TO` edges are read at query time by the orchestrator and by
`metric_resolver`; the per-system runtime's baked `SCHEMA` env does not
carry bindings. Therefore accepting a binding needs no `reonboard`, no
runtime bump, no gateway target change. This is the property that makes
the ADR worth having: vocabulary change is a graph-only change.

If a future runtime bakes bindings into its environment, the accept path
must bump it through the ADR-0055 live-swap mechanism (`reonboard_required`
signal) — recorded here so the assumption is checked, not forgotten.

### D6 — The onboarding agent gains a read-only report, not a writer

`assess_vocabulary_fit(system_id)` (ADR-0068) gains a `pending_bindings`
count so the agent can say "3 new concepts match columns of this system —
review them in Self-Learning Discoveries". The agent still NEVER writes;
the count is read from the proposal store.

### D7 — Scope discipline: what is deliberately NOT built

- No live discover in the pass (the graph baseline is the input; schema
  change is the daily drift check's job, ADR-0053).
- No LLM scoring — `vocab_fit` tokens/synonyms only; a concept whose
  definition text carries no synonyms binds by name match or not at all,
  and the steward can add synonyms to the definition and re-run.
- No auto-accept at any confidence (ADR-0053 / ADR-0066 D3).
- No rebinding of already-bound columns (D2).
- No concept overlays (ADR-0068 Flag 4 deferral stands).
- No bulk-accept transaction — per-item accepts through the one writer.
- No new proposal type — `concept_mapping` is reused; the `source` and
  `provenance` fields distinguish origin.

## Considered alternatives

1. **Extend `reonboard.py` with `--rebind-only`.** Rejected: the driver's
   contract is "block until runtime AND gateway target READY" for a
   re-provision; a graph-only pass does not belong in it, and running it
   still requires operator shell access rather than the steward UI.
2. **Bind automatically above a confidence threshold.** Rejected: machine
   claims never become authority without a steward (ADR-0053); a wrong
   silent binding produces confidently wrong metrics, the worst failure
   mode the sample has.
3. **Let the orchestrator discover bindings lazily from user questions.**
   This is the status quo and stays available; it is too slow and too
   incidental to be the answer to a deliberate import.
4. **Run the pass on the daily drift schedule instead of on import.**
   Rejected as the only trigger — the steward who imports wants the
   proposals in the same session. Kept as a secondary trigger: the daily
   job also runs the pass for concepts imported in the last 24 h whose
   proposals are missing (e.g. a system onboarded after the import).

## Consequences

- One new module (`agents/vocabulary_drift.py`), one post-commit hook in
  `_run_import`, one manual steward route, one filter/action in the
  existing queue UI, one field on `assess_vocabulary_fit`'s report. No new
  writers, no new proposal types, no runtime changes.
- Importing concepts after onboarding produces the partial re-onboard the
  steward expects — as a review list, in minutes, with no rebuild.
- Tests to add: pass over a fixture graph files exactly the expected
  `concept_mapping` proposals and NOTHING else (no `MAPS_TO` write —
  extend the `test_graph_maintenance_routes.py` GET/POST scan pattern to
  the new module); bound columns skipped; tombstoned pairs skipped;
  generic columns skipped; idempotent re-run supersedes; accept-all with
  one failing item leaves the rest pending; `assess_vocabulary_fit`
  reports the count and still writes nothing.

## Note (2026-09-26) — dotted-column deferral closed by ADR-0076

The implementation skipped dotted struct columns (`oee.availability`) and
counted them per system as `skipped.dotted`, citing the accept writer's
`rsplit('.', 1)`. That limitation was documentation-only: `_repoint_maps_to`
MATCHes the Field id `f"{system}.{table}.{column}"`, a plain concatenation, so
the split point is irrelevant. [ADR-0076](0076-binding-disambiguation-and-dotted-columns.md)
D5 lifts the skip — dotted columns are scored like any other (vocab_fit
already tokenizes on every non-alphanumeric) and a winner files
`to_field = table.a.b`; the metric path renders it as ONE quoted identifier
(ADR-0076 D4). `skipped.dotted` is DROPPED (not reported as a permanent 0);
the top-level `skipped` key set stays exactly `{bound, generic,
tombstoned_or_gated}`.
