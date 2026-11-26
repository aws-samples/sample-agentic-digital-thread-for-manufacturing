<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0080: A pending metric definition can be DRY-RUN — rendered as the card it would become — before the steward accepts it

## Status

Accepted — 2026-09-25 (implemented; see *Implementation notes (2026-09-25)*
below, which record where the shipped code amends D1–D5). Proposed the same
day. Amends [ADR-0066](0066-graph-backed-metric-definitions.md) D3/D9
with ONE narrow carve-out (a pending definition may be *resolved* read-only
for preview) and leaves every other D-clause intact.
Related: [ADR-0045](0045-multi-site-metric-fan-out.md) (honest-data rule,
card statuses), [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md)
(cost is measured-only), [ADR-0062](0062-review-queue-promote-to-golden-findings-ledger.md)
(queue computed on read; only steward decisions persist).

## Context

ADR-0066 made a metric definition structured graph data that resolves
deterministically, and made the steward queue the only door to authority:
`POST /api/admin/vocabulary/metrics/propose` files a PENDING proposal,
`GET …/metrics/{mapping_id}/preview` shows the steward a **structural**
diff (current formula vs proposed, validation against the current concept
universe, collision flag), and accept is validate → MERGE → exact readback.

Loading the demo semantic pack on 2026-09-24 exposed what the structural
preview cannot answer. The steward is shown

    demo-reprint-count = count(reprint_segment) over segment_record_details_raw

and asked to accept it. Whether that formula *captures the data
meaningfully* — returns a number at all, returns a plausible number, is
`no-data` for the last 7 days because the concept binds to a column that
is always NULL, or resolves to 0 because the value mapping is wrong — is
unknowable from the diff. The steward's only options today are accept
blind and inspect the card afterwards (the definition is then live for
every user), or reproduce the query by hand in Athena. Neither is the
"intuitive onboarding of business semantics" the sample promises.

The seams to answer the question already exist and are all read-only:

- `metric_definition.parse_definition` / `validate_definition` — a pending
  proposal's `query_context` parses into the same closed op-tree the graph
  layer serves.
- `metric_resolver.resolve_metric(metric_id)` — computes a `ResolvedMetric`
  (value, `sample_count`, status, per-site rows under ADR-0045) from the
  metric layer's definition; it is the ONE computation home and is what a
  card displays.
- `card_layer.CardLayer.cards_for_metric` — the presentation seam; ADR-0066
  D9 deliberately kept it code-only ("no graph-backed Cards in v1").

What does not exist is a way to hand `resolve_metric` a definition that is
not yet in the served metric layer, and a place to show the result that is
unmistakably labelled *preview*.

## Decision

### D1 — Preview is a read-only dry-run RESOLVE of the pending definition; it writes nothing and caches nothing

`GET /api/admin/vocabulary/metrics/{mapping_id}/preview` gains a query
parameter `resolve=1` (default absent ⇒ today's structural diff, byte
unchanged). With `resolve=1` the route additionally returns a `dry_run`
block computed by resolving the **proposed** definition against the live
systems, exactly as `resolve_metric` would after accept.

Mechanism: `metric_resolver` gains
`resolve_definition(definition: dict, *, metric_id: str) -> ResolvedMetric`
— the existing resolve body factored so that `resolve_metric(id)` becomes
"look up the definition in the metric layer, then `resolve_definition`".
The pending proposal's parsed definition is passed directly; the served
metric layer is NOT consulted for it and NOT mutated. No TTL snapshot, no
`_record_graph_definition_usage` (ADR-0066 D6 telemetry is for *served*
definitions), no dashboard cache entry, no memory write. A preview is a
request-scoped computation whose result lives in the response body only
(ADR-0062 "computed on read").

### D2 — The dry-run result carries the SAME honesty statuses as a live card, plus `preview: true`

`dry_run` is `{preview: true, metric_id, value, sample_count, status,
sites: [...], window, computed_at, cost}` where `status` is one of the
existing ADR-0045 statuses — `ok`, `no-data` (value None / zero samples),
`unavailable` (a system failed), `invariant_violation` (declared
`valid_range`/`denominator_provenance` violated) — never a new fourth
vocabulary. A preview that returns `no-data` is the point of the feature:
it tells the steward the concept binding or value mapping is wrong
*before* the definition is live.

`cost` follows ADR-0053: measured only (bytes scanned per system as
reported by Athena, query count), never estimated, never dollars; absent
when a system does not report it.

### D3 — A derived definition previews ONLY when every input resolves; otherwise it names the missing input

`demo-dpmo` (derived from `demo-reprint-count` / `demo-finalized-request-count`)
is refused at *propose* today while its inputs are pending. For preview the
rule is the same: a `MetricRef` leaf must resolve to an ACCEPTED served
metric OR to another pending proposal named explicitly in the request
(`with=<mapping_id>,<mapping_id>`), in which case the dry-run resolves the
whole pending set together. A leaf that resolves to neither yields
`status: "unresolved_input"` naming the metric id — WITHHOLD, never proxy
(the "metric term that does not resolve is NAMED" invariant applies to
previews too).

### D4 — Preview is steward-gated, bounded, and its traffic is labelled

- Route stays behind `_check_steward` (it already is).
- One dry-run per request; the resolver's existing per-system Athena
  bounds apply (`poll_timeout_seconds`, `max_scanned_bytes`, `RECENT_PARTITIONS_LIMIT`)
  — a preview cannot spend more than one live card refresh.
- `source_system` attribution on the Athena executions carries the suffix
  `:preview` so `audit_runtime_env` / workgroup analysis can separate
  steward previews from user traffic (same pattern as `eval-run-` prefixes
  in ADR-0061).
- Concurrency: previews ride `offload_resolver` (the dashboard/metric
  thread lane) — never inline in the async route (event-loop offload
  invariant).

### D5 — The Self-Learning Discoveries card shows the preview AS the card it would become

The pending `metric_definition` item in the admin queue gains a
"Preview value" action. It renders the `dry_run` block with the SAME
component the dashboard uses for a metric card, inside a frame labelled
*Preview — not accepted* with the `computed_at` timestamp and the window.
Nothing about the rendering is persisted; closing the card discards it;
re-opening recomputes. The accept button is unchanged and remains the only
door (ADR-0066 D3).

This is a *preview* of a card, not a *card*: `CardLayer` stays code-only
and ADR-0066 D9's "no graph-backed Cards" stands. If accepted graph
definitions should later get dashboard cards, that is a separate ADR — the
preview component is deliberately the one they would reuse.

### D6 — Scope discipline: what is deliberately NOT built

- No persistence of previews (no store, no history, no "last previewed").
- No auto-accept, no "accept if preview looks fine" shortcut (D3 of 0066).
- No preview from the conversational capture path — the tool files the
  proposal; the steward previews from the queue.
- No LLM anywhere in the preview: the value is computed, the rendering is
  the card component, the honesty status is the resolver's (ADR-0053).
- No new Athena tunables; the bounds are the existing per-system ones.
- No preview of `query_guidance`, business context or vocabulary imports
  — those have their own preview contracts (ADR-0063 D7, ADR-0068).

## Considered alternatives

1. **Accept, look at the card, reject if wrong.** Rejected: the definition
   is live for every user in between, and a rejection leaves a 90-day
   tombstone (ADR-0053) for what was a steward's exploratory look.
2. **Have the steward run the SQL by hand.** Rejected: it reproduces the
   resolver by hand (site fan-out, authority, honesty statuses) and is the
   "manual by design" posture the sample is moving away from.
3. **Auto-run the dry-run at propose time and attach the number to the
   proposal.** Rejected: it turns every propose into Athena spend
   including from the conversational tool, and a number frozen at propose
   time is stale by review time. On-demand from the queue keeps cost
   steward-initiated and the number current.
4. **Temporarily MERGE the definition and resolve it, then delete.**
   Rejected outright: a write-then-compensate for a read; violates D3's
   "readback is the proof" and would be observable by concurrent users.

## Consequences

- One new resolver entry point (`resolve_definition`), one route query
  parameter, one admin-card action. `resolve_metric` becomes a two-line
  wrapper; its behaviour and tests are unchanged.
- The steward can answer "does this definition capture data meaningfully?"
  before anything is live, using the identical computation and honesty
  statuses the card will show afterwards.
- Athena spend from previews is bounded by the same per-system limits as a
  card refresh and is attributable (`:preview` suffix).
- Tests to add: `resolve_definition` equals `resolve_metric` for every
  code-registry metric (parity), preview writes nothing (metric layer
  snapshot digest unchanged, no proposal status change, no memory), the
  four honesty statuses surface unchanged, `unresolved_input` names the id,
  route without `resolve` is byte-identical to today.

## Implementation notes (2026-09-25)

Implemented on top of ADR-0075 (predicate metrics). Where the code as it
stands differed from the draft, the code-level decision is recorded here and
wins over the text above.

- **D1 — entry point.** `metric_resolver.resolve_definition(metric, *,
  metric_id=None, authority=None, layer=None)` takes the built **dataclass**
  (the accept path's `parse_definition` → `validate_definition` →
  `graph_metric_layer.build_metric`/`to_metric`), not a raw dict — so a
  preview cannot resolve anything accept would reject. `resolve_metric` is
  now lookup + `resolve_definition` + the unchanged D6 usage bump; parity for
  every code-registry metric (including the fail-loud deferred `latest`) is
  pinned in `tests/unit/test_metric_resolve_definition.py`. The `layer`
  parameter is the one addition the draft did not name: derived `MetricRef`
  inputs resolve against an explicit, request-scoped layer (served metrics
  with the pending set laid over them — `metric_preview._preview_layer`)
  instead of through `resolve_metric`, which is what keeps D6 telemetry off
  the preview path. `layer=None` is byte-identical to before.
  The orchestration lives in the new `agents/metric_preview.py`;
  `self_learning.preview_metric_definition_mapping(mapping_id, *,
  resolve=False, with_ids=())` only reads the proposal rows and delegates.
- **D2 — shape and statuses.** `dry_run` IS the dashboard card-metric entry
  (rendered by the SAME `dashboard.card_entry_from_resolved` /
  `multi_card_entry` a live card uses), plus `preview: true`, `metric_id`,
  `window` (the declared window token, `null` when none is declared),
  `computed_at` and `cost`. Statuses are therefore the **card register**
  (hyphenated): `ok`, `no-data`, `unavailable` (boundary failure, with
  `error`), `invariant-violation`, `unresolved-predicate` (ADR-0075, which
  landed after this draft) — plus the two preview-only outcomes
  `unresolved-input` (D3) and `invalid` (the pending document no longer
  validates against the current universe, or collides with a code id; nothing
  executes). `sites` is present only when the dashboard's own multi-site gate
  would fan out (≥2 onboarded systems and >1 covering the metric), exactly as
  on a card. `cost` is `{query_count, systems: [{system_id, query_count,
  scanned_bytes?}]}`: it counts the resolver's completed VALUE executions;
  `scanned_bytes` is the Athena-reported figure and is present for a system
  only when every one of its executions reported it (a partial sum would
  understate). Partition/time-window discovery reads are metadata lookups and
  are not counted. Never estimated, never dollars.
- **D3 — inputs.** Missing inputs are pre-checked (transitively through
  pending companions) BEFORE any execution: `status: "unresolved-input"`,
  `missing_inputs: [...]`, a `detail` naming every id, `cost.query_count 0`.
  The resolver itself also withholds as `unresolved_input`
  (`UNRESOLVED_INPUT_STATUS`) when an explicit layer cannot supply an input,
  naming the innermost id. `with=` is parsed by `metric_preview.parse_with_ids`:
  at most 8 ids, charset `[A-Za-z0-9][A-Za-z0-9_-]*`, no duplicates, never
  the previewed proposal — otherwise `400 {"error": "invalid_with_parameter"}`.
  A companion that is unknown or not a pending `metric_definition` returns
  `404`/`409` with `with_mapping_not_found` /
  `with_metric_definition_preview_unavailable` and the offending `mapping_id`.
  Two previewed proposals defining the same metric id are `invalid`.
- **D4 — gate, bounds, attribution, offload.** There is no `_check_steward`
  on this router: the route sits under `/api/admin`, which
  `AuthMiddleware.ADMIN_PREFIXES` gates before any handler runs (pinned in
  `tests/unit/test_metric_preview_route.py`). Bounds are the existing ones —
  the preview goes through the unchanged Athena connector
  (`poll_timeout_seconds`, `max_scanned_bytes`, partition pruning). The
  `:preview` attribution is carried by a request-scoped ContextVar trace
  (`agents/metric_execution_trace.py`): while it is active the Athena value
  query runs with `source_system="<system_id>:preview"`, which stamps the
  result's source metadata and headroom telemetry; with no trace the live call
  is byte-identical (no `run` keyword at all). Honest caveat: this attribution
  is response-side — the Athena workgroup query history does not see the
  suffix, so workgroup analysis cannot yet separate preview spend; the
  per-response `cost` block is the measured record. The WHOLE route (both the
  structural and the resolve path) now rides `offload_resolver`
  (`preview_metric_definition_route`, pinned in
  `tests/unit/test_event_loop_offload.py`); the sync body keeps the name
  `preview_metric_definition`. `resolve` is a FastAPI bool (`1`/`true`/…;
  anything else 422); without it the helper is called exactly as before.
- **Writes nothing** is pinned by FAIL-FAST tripwires in
  `tests/unit/test_metric_preview.py`: every writer (`invalidate_metric_layer`,
  `graph_metric_layer.invalidate_definitions`, `metric_definition_store`
  `invalidate`/`write_definition`/`neptune_write`, the usage bump and
  `_record_graph_definition_usage`, `db`/`self_learning` graph writes, the
  proposal-table put/status update and `log_proposed_mapping`, the dashboard
  cards-cache update/invalidation, and `MemoryClient` construction/deletion)
  raises a `BaseException` AT the call site — so a production
  `except Exception` best-effort guard cannot swallow it — and the call log is
  also asserted empty; the served layer's metric set is unchanged after a
  preview.
- **Not HTTP-cacheable.** Every resolved response (200 and error statuses,
  including the 400 `invalid_with_parameter`) carries
  `Cache-Control: private, no-store`, and the client requests it with
  `cache: 'no-store'`; the structural (no-`resolve`) response is unchanged.
- **`computed_at` is the completion time** — stamped when each outcome is
  final (after resolution), never at request start (pinned by an ordering
  test).
- **Multi-site invariant honesty (revision 2026-09-25).** The
  fan-out previously relabelled a per-site `invariant_violation` (value
  `None`) as `no-data`, so a two-site preview — and the live dashboard's
  multi-site card — reported `no-data` for a value its declaration withheld.
  `metric_multisite._resolve_per_system` now keeps `invariant-violation` per
  site (alongside `unresolved-predicate`), an all-withheld fan-out is an
  answer rather than a failure, and `dashboard.multi_card_entry` reports the
  aggregate as `invariant-violation` (value `None`, `detail` naming the
  systems) whenever no site contributed and at least one violated. A
  contributing site keeps the aggregate `ok`; the all-no-data entry is
  unchanged. Because the preview and the dashboard share this renderer, the
  fix applies to both surfaces.
- **D5 — UI.** The "Preview value" action (with an optional companion-ids
  field) appears on the pending `metric_definition` proposal card, on the
  pending row of the admin Metric definitions panel, and on the propose
  modal's "filed for review" pane. It lazily loads `MetricDryRunFrame`, which
  renders the dashboard's own `KpiCard` inside a section labelled
  *Preview — not accepted* with `computed_at`, the window, the status (and
  missing inputs) and the measured cost; Recompute refetches, Close discards,
  and nothing is cached (a plain `no-store` fetch, not a query option). The frame is a
  separate chunk, so the card component never enters an entry bundle. The
  shared `KpiCard` diagnostic now words a withheld status honestly when no
  boundary `error` is present (no-data / invariant-violation /
  unresolved-predicate / unresolved-input / invalid, with the server's
  `detail`) instead of the generic "could not be resolved" — this also
  improves the dashboard's own withheld cards. The accept action is unchanged.
  To keep every touched component within the ≤50-line function rule, the
  proposal card's `metric_definition` branch lives in its own
  `MetricDefinitionCardBody` (`SlCardBody` is now a short per-family
  dispatcher), and the propose modal is a thin shell over a form hook, field
  components and a result pane that owns the preview mount — markup is
  byte-identical (revision 2026-09-25).
