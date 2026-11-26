<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0077: Additive schema drift is REPAIRED on steward accept — `column_added`/`table_added` materialise Field/SchemaEntry nodes through the ONE physical-field writer; destructive drift stays acknowledge-only; onboarding records discovery completeness

## Status

**Accepted — 2026-09-26.** (Proposed 2026-09-26; implemented and verified the same day — see Implementation notes.)

Amends [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) (b)
("schema drift is a proposal class, never a graph mutation … accepting a
`schema_drift` row is an acknowledge-only accept … re-onboarding — the real
fix for drift — stays the steward's explicit action"): re-onboarding stays
the fix for DESTRUCTIVE drift; ADDITIVE drift is repaired by the steward's
accept. Builds on [ADR-0081](0081-vocabulary-drift-field-binding-proposals.md)
(the drift pass scores Field nodes read from the graph; a graph-only change
needs no re-onboard), [ADR-0076](0076-binding-disambiguation-and-dotted-columns.md)
(dotted physical columns are addressable; `metric_binding.validate_column_name`
is the ONE column-identifier home), [ADR-0003](0003-unified-proposal-lifecycle.md)
(the proposal lifecycle whose collision rules this ADR relies on, unchanged),
[ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(a writer ships with its read path; replace-not-accrete; machine claims never
vouch), [ADR-0045](0045-multi-site-metric-fan-out.md) (honest-data: absence is
absent, never a fabricated value) and [ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — no deployment literal enters the engine).

## Context

Onboarding writes an Athena system's physical columns to the graph in step
b2a (`onboard._dp_athena_physical_fields`): for every table whose
`information_schema`/`GetTableMetadata` introspection succeeded it MERGEs one
`Field` node per flattened physical column (`data_type`, `is_partition`) under
the table's `SchemaEntry`. That discovery (`_discover_athena_physical_columns`)
is deliberately best-effort: a per-table failure is logged at INFO and the
table falls back to the map-derived Field write (only the columns the LLM map
step named). Nothing records that the fallback happened.

On a live deployment that fallback fired for a wide table: the graph
carried only a minority of its physical columns, with no indicator anywhere
that discovery had been partial. Everything downstream then behaved *correctly on
the wrong premise*:

- the daily schema-drift check (ADR-0053 (b)) compared the graph baseline to
  a fresh discover, found the missing columns and filed one
  `column_added` proposal each — the mechanism worked;
- a steward first rejected them, and on a later re-filing accepted them
  all — the review surface worked;
- `self_learning._accept_schema_drift` is acknowledge-only by design, so the
  accepts wrote NOTHING. The columns never became Field nodes; the ADR-0081
  vocabulary-drift pass could not score them; ADR-0075 predicates could not
  reference them; the orchestrator's `get_system_schema` kept advertising only
  the partial subset;
- because an exact-target ACCEPTED row classifies `noop` in
  `_classify_collision` (ADR-0003 C), the next daily check could never
  re-file the same drift — the accepted rows made the gap permanently
  invisible in the queue;
- the only documented fix was a full re-onboard: re-discover, re-map every
  column with the LLM, re-provision the runtime, block until the gateway
  target is READY.

Re-onboarding is the right fix when drift is DESTRUCTIVE or SEMANTIC — a
removed column may be bound to a concept, referenced by a join path or an
identity rule, or be the measure column of a metric; a retyped column changes
what every consumer of it means. Re-onboarding is the WRONG fix for an
ADDITIVE change: a new column is not bound to anything, references nothing,
and changes the meaning of nothing already in the graph. Materialising it is
the same write onboarding would have performed had discovery been complete —
a graph-only change with no runtime component (the ADR-0081 D5 property).

Existing machinery that is almost the answer:

- `onboard._dp_athena_physical_fields` — the Field/SchemaEntry MERGE writer;
  provision-only today.
- `athena_tools.describe_physical_columns_bounded` — metadata-only,
  flattened, partition-flagged, with an affirmative completeness flag; the
  GetTableMetadata path (ADR-0075 era) returns the WHOLE column list.
- `schema_drift.run_drift_check` — detects exactly the additive gap, daily.
- `POST /api/neptune/graph/repair` and `/backfill-concepts` — the steward-
  gated, offloaded, POST-only graph-maintenance precedent
  (`test_graph_maintenance_routes.py`).

The invariants that constrain the answer: proposals never mutate the graph —
only the steward's ACCEPT does, through the lifecycle (ADR-0053); the machine
never writes authority or `MAPS_TO`; GET/HEAD handlers are read-only and graph
mutation is a steward-gated POST; async routes offload; identifiers fail
closed; computed, never LLM-judged; no auto-accept.

## Decision

### D1 — Accepting a `column_added` or `table_added` drift MATERIALISES it through the ONE physical-field writer; `column_removed`/`column_retyped`/`table_removed` stay acknowledge-only

`self_learning._accept_schema_drift` dispatches on the row's `reason`
(the pinned `schema_drift.DRIFT_KINDS` vocabulary — note the retype kind is
`column_retyped`):

- **`column_added`** — the column is materialised as a `Field` node under
  the EXISTING `SchemaEntry`: `f.id = "<system>.<table>.<column>"`,
  `f.name`, `f.schema_id`, `f.system_id`, `f.data_type`, `f.is_partition`,
  plus the `(SchemaEntry)-[:HAS_FIELD]->(Field)` edge. A struct PARENT column
  (the fresh discover reports `oee` where the physical schema flattens to
  `oee.availability`, `oee.performance`) materialises every flattened leaf
  the source describes under that prefix — exactly what onboarding writes.
- **`table_added`** — the `SchemaEntry` is MERGEd (with its `HAS_SCHEMA`
  edge) and every column the source describes for it is materialised as
  above; the table's discovery-completeness properties (D3) are written from
  the same read. NOTHING else: no entity classification (that path is
  LLM-assisted), no `IDENTIFIED_BY`, no join discovery, no `MAPS_TO`.
- **`column_removed`, `column_retyped`, `table_removed`** — UNCHANGED
  acknowledge-only accept (zero graph writes; the existing best-effort
  `reconcile_blocked_proposals` for removals). A removal may orphan a
  binding, a join path, an identity rule or a metric's measure column; a
  retype changes what every consumer means. Those are semantic changes and
  re-onboarding — which REPLACES the schema subgraph and re-runs the mapping
  checkpoint under steward review — remains the only honest fix. The accept
  response now says so explicitly: `"materialised": false`.

**The ONE writer.** The per-column MERGE and the SchemaEntry MERGE are
EXTRACTED from `_dp_athena_physical_fields` into a new import-pure module
`agents/schema_fields.py` — `write_schema_entry(system_id, table, *, write)`,
`write_physical_field(system_id, table, col, *, write)`,
`write_discovery_completeness(...)`, `read_field(...)`, plus the id helpers
and validators — and BOTH onboarding and the accept path call it. The Cypher
text moves byte-identically (a test pins the strings and grep-gates the old
literals out of `onboard.py`). The `write`/`query` seams are REQUIRED
keyword arguments so each caller keeps its own monkeypatch target
(`onboard.neptune_write` for provision; `self_learning._neptune_write_loud`
/ `_neptune_query` for the accept). Onboarding's b2a output is
byte-identical after the extraction.

**Source of truth at accept: the source is RE-READ.** The proposal row stays
small (subject + reason + description); `data_type` and `is_partition` are
never carried on it. At accept, `schema_repair._source_columns(system_id,
table)` reads the CURRENT physical schema:

- Athena systems: `describe_physical_columns_bounded(table, cfg,
  refresh=True)` — the metadata-API path (flattened, partition-flagged,
  `complete=True`) with the 600 s schema-cache READ bypassed (the fresh
  result is still stored), so a describe the drift check warmed minutes
  earlier can never answer the accept; config
  derived through `onboard_agent._athena_discover_cfg` (the same precedence
  and cross-account credential merge discovery uses);
- every other connector: the SAME read-only discover the drift check uses
  (`schema_drift._discover_fresh` → `_fresh_table_map`), `is_partition`
  `False`, `data_type` as reported.

A column (or table) that is no longer visible in the source at accept time is
NOT materialised: the accept fails closed with a structured error and the row
stays pending (the next drift check will sweep it as resolved).

**Validate → MERGE → exact readback.** Identifiers are validated before any
write: `db.validate_system_id`; the table through
`schema_fields.validate_table_name` (a single identifier token — the
`proposal_scoring._FIELD_SEGMENT_PATTERN` register, ≤ 128 chars); the column
through `metric_binding.validate_column_name` (ADR-0076 D4 — every
dot-segment a SQL identifier, so dotted struct leaves pass and anything else
fails closed). For `column_added` the target `SchemaEntry` MUST already exist
(`ValueError` when absent; the `_repoint_maps_to` honesty guard
distinguishes "graph unreachable" → `RuntimeError`). After the MERGEs, every
written Field is read back through
`(System)-[:HAS_SCHEMA]->(SchemaEntry)-[:HAS_FIELD]->(Field {id})` and its
FULL MERGEd shape is compared (`name`, `schema_id`, `system_id`,
`data_type`, `is_partition` — the last by boolean identity); for
`table_added` the SchemaEntry's `name`/`system_id` are compared through
`(System)-[:HAS_SCHEMA]->(SchemaEntry)` first. A missing row or ANY
property mismatch raises (`RuntimeError`) and the row stays pending — never
`accepted` over an unverified write. On success the row flips to `accepted`
WITH `neptune_node_id` (the Field id, or the SchemaEntry id for
`table_added` — honest presence: there IS an applied artifact now) and the
response reports `"materialised": true` and `written = {schema_entry,
fields: [...], field_count, table_created}`. A validation failure returns
`{"error", "http_status": 422, "materialised": false}` with ZERO writes.

**What the accept never does (D4 restated for this path):** no `MAPS_TO`
edge, no `AuthorityRule`, no `IdentityRule`/`JoinPath`, no runtime env bump,
no gateway change, no LLM call. `requeue_mapping` of a materialised drift row
is REFUSED (the existing "non-invertible shared graph state" `ValueError` →
400): a Field node is part of the schema subgraph and is corrected by
re-onboard, never by a DETACH DELETE from the queue.

**Known limitation, recorded honestly.** The per-system agent's baked
`SCHEMA`/`tables_desc` prompt text is produced at provision from the same
physical discovery and is refreshed ONLY by re-onboard (or a deploy). A
materialised column is therefore visible IMMEDIATELY to the graph, to
`get_system_schema` (the orchestrator's graph tool), to the ADR-0081
vocabulary-drift pass, to ADR-0075/0076 binding resolution and to the
schema-drift baseline — but the system agent's prompt will not LIST it until
the next re-onboard. The agent can still describe the table live
(`athena_describe_table`), so queries against the column work; only the
prompt's advertised column list lags. This is the same class of limitation
ADR-0081 D5 records for bindings and is accepted for the same reason: the
repair is graph-only and needs no rebuild.

### D2 — Prior accepted rows never blind detection; a steward-gated `POST /api/neptune/graph/repair-schema` re-applies already-accepted additive drift in bulk

**Why the queue went blind.** `_file_drift_proposals` dedupes against
PENDING rows only (verified) — that is not the problem. The problem is the
lifecycle classifier: an incoming agent proposal whose `target_key` exactly
matches an ACCEPTED row classifies `noop` (ADR-0003 C, pinned in
`_classify_collision`). A `column_added` descriptor has an empty
`description`, so its target key is constant and every re-detection of an
accepted-but-unmaterialised column was a `noop` forever.

**The fix is a marked re-file through the UNCHANGED lifecycle.** Before
filing, `run_drift_check` passes the diff through
`schema_drift._mark_unmaterialised(system_id, drifts, pending_rows)`: for
each `column_added`/`table_added` drift (the two materialisable kinds) it
reuses the description of an already-pending row for the SAME drift
(system, subject, reason — stability on re-runs, no store lookup);
otherwise it consults the subject-GSI history and marks the drift ONLY when
the subject's most recent steward accept (`accepted_at` — it survives the
later `superseded` flip) was the SAME additive reason. The marker is
generational: `REDETECTED_DESCRIPTION` (`"re-detected after a prior accept of
this addition; accept again to materialise"`) is generation 1 and later
generations append ` (generation N)`, where N is one past the highest
generation among the accepted same-reason rows — so the new target key
differs from EVERY accepted one and an accepted marked row can never blind
the next re-detection. An accepted `column_removed`/`column_retyped`/
`table_removed` (or any different reason) as the latest decision leaves the
drift UNMARKED: acknowledging a removal is not acceptance of a later
addition, so it files for fresh review and is never a repair candidate. Everything else is
byte-identical: an ordinary first-time drift still files with an empty
description and the pinned target key.

The marked descriptor has a DIFFERENT target key on the SAME subject, so
`_classify_collision` takes the existing same-subject-different-target path
(`_classify_active_diff`): the pre-ADR accepted agent row is SUPERSEDED (its
`accepted_at`/`accepted_by` stamps survive — the row is never deleted; status
becomes `superseded` exactly as ADR-0003 D specifies for any agent row
displaced on its subject) and a fresh PENDING row lands in the queue whose
description tells the steward WHY it is back. `sweep_resolved_drift` computes
its current-key set from the SAME marked list, so the fresh row is never
swept as a sibling casualty; a second run classifies `dup_pending`. The
machine writes no steward-only status: `superseded` is the lifecycle's own
terminal, and the classifier is not modified. A rejected-only history
(tombstone) is not a prior accept and stays on the ADR-0053 (c) path. The
per-system result entry gains `refiled_after_accept: n` (the pinned shape is
extended, not changed; existing pins update).

**The bulk repair door.** `POST /api/neptune/graph/repair-schema` — body
`{"system_id": "<id>", "detect": true}` — is registered in `server.py` beside
the two graph-maintenance precedents: POST only, `Depends(_check_steward)`
at registration (fails closed), the whole body offloaded
(`return _graph_maintenance_response(await offload(_repair_schema_sync,
payload))`), error dicts → 500 (validation → 422). The sync body
(`schema_repair.repair_schema(system_id, actor, detect)`):

1. validates `system_id` (fail closed);
2. when `detect` (default), runs `run_drift_check(system_id)` — the SAME
   detection the daily job runs, so pre-ADR accepted rows are re-filed with
   the marker (a skipped detection is returned honestly and nothing is
   materialised);
3. selects the system's PENDING `schema_drift` rows whose reason is
   materialisable AND whose description is the marker (any generation) — i.e. exactly the
   drift a steward ALREADY accepted once — sorted by `from_field`, capped
   at `REPAIR_ACCEPT_CAP` (200) per call;
4. calls the EXISTING `accept_mapping(mapping_id, actor=<steward>)` for
   each — the D1 materialising accept, one validate → MERGE → readback per
   row, attributed to the steward who invoked the repair. A per-row failure
   is reported (`failed: [{mapping_id, error_class}]`) and the row stays
   pending; the loop continues;
5. reports counts only: `{system_id, detection, candidates, materialised,
   failed, remaining, pending_unreviewed}`.

Pending drift that was NEVER accepted (`pending_unreviewed`) is NOT touched —
the repair re-applies steward decisions; it never makes one. The route is
idempotent (a second call finds no marked rows) and bounded (the cap, with
`remaining` telling the steward to call again). With `detect: false` it
skips the re-discover and only re-applies rows the daily job already marked.
The CloudFront 120 s origin cap applies as it does to every graph-maintenance
POST: a large repair may 504 at the edge while the server finishes; the next
call is idempotent. The async drift-check job
(`POST /api/admin/learning/drift-check`) remains the preferred detection
trigger for very large systems.

### D3 — Onboarding records per-table discovery completeness; the read path says "partial discovery"

`_dp_discover_athena_physical` passes a `complete_tables` sink
(`_ProvisionState.athena_complete_tables`) into
`_discover_athena_physical_columns` — the sink already exists for the
tables_desc renderer and was simply not wired to the graph write. A new
provision step `_dp_discovery_completeness(ctx)` (Athena systems, after b2a)
writes on every `SchemaEntry` of the system:

- `se.discovery_complete: bool` — `True` only when the table's introspection
  came back provably whole (present in the sink); `False` when it was capped
  OR when physical discovery failed for the table and provision fell back to
  the map-derived subset (the partial-discovery case);
- `se.discovered_columns: int` — the number of Field nodes provision wrote
  for the table.

An incomplete table is logged at WARNING (counts only — no column names, no
values). Non-Athena connectors write neither property (absent = unknown, the
honest-data rule; extending the flag to other connectors is a follow-up).
`graph_tools._schema_query` projects both properties and `get_system_schema`
renders a per-table `"discovery": {"complete": false, "discovered_columns":
2}` block ONLY when the property is present — the existing table record is
byte-identical for graphs that never wrote it. Because `get_system_schema`
is the orchestrator's graph tool AND the drift check's baseline source, the
orchestrator can say "this table's discovery was partial" instead of
answering from a silent subset, and a steward reading the schema sees the
same fact.

The repair path keeps the flag honest: after a `column_added`
materialisation the accept re-reads the table's Field names from the graph
and rewrites `discovery_complete`/`discovered_columns` from the comparison
with the CURRENT source description (complete ⇔ the source read was complete
AND every described column is now present); a `table_added` writes them from
its own read. When discovery is incomplete, `run_drift_check` is the repair
path (the metadata-API describe makes new reads complete), not a re-onboard.

### D4 — Scope discipline: what this ADR deliberately does NOT do

- No LLM anywhere on the accept or repair path (materialisation is a
  deterministic write of what the source describes).
- No auto-accept: a never-reviewed drift row stays pending; the repair route
  re-applies only rows a steward already accepted.
- No `MAPS_TO`, `AuthorityRule`, `IdentityRule`, `JoinPath` or entity
  classification writes. Binding a materialised column is the ADR-0081
  pass's job (`POST /api/admin/vocabulary/drift/run` will now see the
  column) or a steward's `concept_mapping` proposal.
- No re-onboard, no runtime env bump, no gateway change.
- No materialisation of removals or retypes (D1), and no DETACH DELETE from
  the queue (requeue refused).
- No change to `_classify_collision`, `proposal_key`'s `schema_drift`
  grain, or the ingestion gates.
- No default-on scan of every system: repair is per `system_id`, steward-
  invoked.

### D5 — Contract changes pinned by tests

- `test_schema_drift_proposals.py::test_accept_is_acknowledge_only` pins the
  OLD contract for `column_added` (zero writes) and is DELIBERATELY changed
  by this ADR: the zero-write pin moves to `column_removed`,
  `column_retyped` and `table_removed`; `column_added` gains the
  materialisation pin (exactly one Field + one HAS_FIELD, pinned params,
  readback, `neptune_node_id`).
- The per-system drift result entry gains `refiled_after_accept` (equality
  pins in `test_schema_drift_proposals.py`, `test_schema_drift_job.py`,
  `test_drift_sweep.py`, `test_failure_signal_hooks.py` update).
- `test_graph_maintenance_routes.py::ROUTES` gains
  `/api/neptune/graph/repair-schema` (POST-only, 403 fails closed, offload,
  GET/HEAD tripwire unchanged).
- `agents/schema_fields.py` pins the Cypher text byte-identical to the
  pre-ADR b2a writer and grep-gates the literals out of `onboard.py`; a
  property-parity test asserts every `f.<prop>` the readers project
  (`graph_tools._schema_query`, `vocabulary_drift._UNBOUND_FIELDS_CYPHER`)
  is written by `FIELD_MERGE` — the guarantee that a materialised Field is
  scorable immediately.

## Considered alternatives

1. **Keep acknowledge-only; document `reonboard.py` as the fix.** The status
   quo. Rejected: a re-onboard re-runs the LLM mapper over every column,
   re-provisions the runtime and blocks on the gateway — to add nodes that
   carry no semantics. It also re-runs the very discovery that degraded,
   with no guarantee it will not degrade again, and the steward's
   accepts would have meant nothing.
2. **Let the daily drift check materialise `column_added` directly.**
   Rejected outright: drift detection never mutates the graph (ADR-0053 (b),
   restated in the maintainer notes). Only the steward's accept writes, and the review
   surface stays the one door.
3. **Carry `data_type`/`is_partition` on the proposal row and write from
   it.** Rejected: the row would be stale by accept time (a column can be
   retyped or removed between detection and review), the proposal store
   would grow a schema payload it never needed, and the write would no
   longer be "what the source describes NOW". Re-reading at accept costs one
   metadata call and is current by construction.
4. **Re-open the accepted row (`accepted` → `pending`) from the drift
   check.** Rejected: the machine would write a status on a steward row; the
   machine's only legal terminal on this queue is `superseded`
   (`sweep_resolved_drift`'s contract), and re-opening is a STEWARD action
   (ADR-0053 (c) `reopen`).
5. **Special-case accepted `schema_drift` rows in `_classify_collision`.**
   Rejected: the classifier is pure over prior rows and pinned by the
   lifecycle tests; the marked-description re-file achieves the same result
   through the existing same-subject-different-target rule with zero
   classifier change.
6. **A repair route that materialises accepted rows directly, bypassing
   `accept_mapping`.** Rejected: two write paths for one artifact (the
   accept and the repair) would drift apart; funnelling the repair through
   the SAME accept keeps validate → MERGE → readback in one place and
   attributes every write to the steward who triggered it.
7. **Materialise removals as DETACH DELETE on accept.** Rejected: a removal
   may orphan bindings, join paths, identity rules or a metric's measure
   column; re-onboard REPLACES the subgraph under steward review — the only
   honest fix for destructive drift.
8. **Bump the per-system runtime after a materialisation (ADR-0055 live
   swap).** Deferred: the prompt's advertised column list is a convenience;
   the agent describes tables live and every graph reader sees the column
   immediately. Recorded as the D1 limitation so it is checked, not
   forgotten.

## Consequences

- Accepting an additive drift row does what the steward meant: the column
  exists in the graph, is scorable by the ADR-0081 pass, addressable by
  ADR-0075/0076 predicates and bindings, and visible to `get_system_schema`
  — without a rebuild. Destructive drift keeps its honest "acknowledged;
  re-onboard to fix" posture and now SAYS `materialised: false`.
- On the first daily check after this ships, every pre-ADR accepted-but-
  unmaterialised `column_added`/`table_added` row on an affected system is
  superseded and returns to the queue with the re-detected description; the
  steward either accepts them one by one or runs `repair-schema` once.
- One new import-pure module (`agents/schema_fields.py`), one new sync
  module (`agents/schema_repair.py`), one new steward POST, one new
  provision step, two SchemaEntry properties, one additive `get_system_schema`
  block. No new proposal type, no new writer for authority, no runtime
  change.
- The graph-shape contract (`docs/graph-shape-contract.md`) gains the two
  optional SchemaEntry properties and names `schema_fields.py` as the
  SchemaEntry/Field writer home shared by provision and accept.
- `docs/self-learning.md`, `docs/onboarding.md` and the project's invariant
  lines ("Schema drift never mutates the graph"; "GET/HEAD read-only; graph
  mutation is a steward-gated POST") are amended to the new posture.

## Implementation notes

**2026-09-26 — implemented as D1–D5.** New modules
`agents/schema_fields.py` (the ONE writer; Cypher byte-identical to the
pre-ADR b2a text, pinned + grep-gated) and `agents/schema_repair.py`
(`materialise_drift`, `repair_schema`); `schema_drift` gains
`MATERIALISABLE_KINDS`, `REDETECTED_DESCRIPTION`, `_mark_unmaterialised`
and the `refiled_after_accept` count; `self_learning._accept_schema_drift`
dispatches the additive kinds to `_accept_schema_drift_materialise`;
onboarding step b2a-c `_dp_discovery_completeness`; `get_system_schema`
renders the `discovery` block; `POST /api/neptune/graph/repair-schema` in
`server.py`. Verified: full unit lane 10587 passed / 3 skipped (base 10497
+ 90 new), the explicit lane list (test_response_integrity_iteration5/6, event-loop offload, graph
maintenance routes, schema_drift*, drift sweep, vocabulary drift, Athena
physical columns, lifecycle, domain neutrality) 344 passed, root
`tests/test_no_internal_identity_strings.py` green. No deploy; not yet
exercised against a live graph.

Deviations from the plan, fixed against the code:

- **Accept-route offload was REQUIRED, not optional.** The non-Athena source
  re-read uses `schema_drift._discover_fresh`, which calls
  `asyncio.run(onboard_discover(...))` — that raises inside a running event
  loop, and `accept_proposed_mapping` ran `accept_mapping` inline on the
  loop. The handler keeps its name (tests mount it directly) and its body is
  hoisted into `_accept_proposed_mapping_sync` behind `offload`; the
  response mapping is byte-identical.
- **Column validation runs on the SELECTED columns.** `_source_columns`
  returns the raw source list; `column_added` validates only the columns it
  selects (an unrelated odd name elsewhere in the table does not block it);
  `table_added` validates every column (fail closed).
- **`neptune_node_id` for a struct parent is the SchemaEntry id.** A
  single-leaf `column_added` records the written Field id (name as the
  source reports it); a struct parent flattened to several leaves has no
  single Field, so the owning SchemaEntry id is recorded.
- **`repair_schema` also reports `failed_count`** (the `failed` detail list
  is capped at 20). A failed entry carries the exception class, or the
  accept's own error text for a structured (422) refusal.
- **The accept refreshes the completeness flag for every connector** — the
  value is computed from a real graph-vs-source comparison, so it is
  honest; "non-Athena writes neither" applies to onboarding only.

Pre-existing and unchanged: `_file_drift_proposals` counts a `noop` return
as "filed" (it compares against pending ids only).

**2026-09-26 — fixes (four findings, all closed in code + tests).**

- **Generational marker.** The first cut used ONE fixed marker, so an
  accepted, materialised marked row whose Field later disappeared again
  classified `noop` on its exact target — the very blinding D2 forbids.
  The marker now advances per accepted generation
  (`redetected_description(n)` / `redetected_generation(text)`); regression:
  plain accepted → gen-1 accepted → Field absent → fresh pending gen-2 →
  stable → gen-2 accepted → gen-3.
- **Same-reason lineage.** The first cut marked on ANY accepted drift on the
  subject, so an accepted removal/retype authorised bulk repair of a later
  addition. `_accept_generation` now requires the subject's LATEST accept to
  be the same additive reason; reverse-transition tests
  (`column_removed`/`column_retyped` → `column_added`, `table_removed` →
  `table_added`, older added + newer removed) prove the drift stays pending
  and unmarked.
- **Cache-bypassing re-read.** `describe_physical_columns_bounded` and
  `_introspect_columns_bounded` gain keyword-only `refresh=False`; the
  accept passes `refresh=True`. Normal query enrichment keeps the cache.
  Test: a warmed OLD describe, then `refresh=True` returns the CURRENT
  metadata; a gone table (EntityNotFound → no columns) fails closed.
- **Exact readback.** `FIELD_READBACK` projects `schema_id`/`system_id`;
  `schema_fields.expected_field`/`field_mismatches`/`read_schema_entry`
  compare the full shape; per-property mismatch tests raise and write no
  accepted status.

Residual edge (lifecycle-owned, not changed here): a re-detection inside
the ADR-0042 D1 24 h supersede cooldown is rate-limited to the next daily
run, exactly as for every other proposal class.

Re-verified after the fixes: full unit lane 10607 passed / 3 skipped;
the explicit lane list 354 passed; root
`tests/test_no_internal_identity_strings.py` 3 passed.

## Related

- [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) — (b)
  amended by this ADR: re-onboard stays the fix for destructive drift;
  additive drift is repaired on accept.
- [ADR-0003](0003-unified-proposal-lifecycle.md) — the same-subject-
  different-target supersede this ADR's re-file relies on, unchanged.
- [ADR-0081](0081-vocabulary-drift-field-binding-proposals.md) — the drift
  pass that scores the materialised Field; D5's graph-only-change property.
- [ADR-0075](0075-predicate-metric-definitions.md) /
  [ADR-0076](0076-binding-disambiguation-and-dotted-columns.md) — predicates
  and bindings over the materialised columns; the ONE column-identifier
  validator.
- [ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
  — writer ships with its read path (`get_system_schema`'s `discovery`
  block); replace-not-accrete.
- [ADR-0045](0045-multi-site-metric-fan-out.md) — honest-data rule behind
  "absent = unknown" for the completeness properties.

#### Review revision 3 — 2026-09-26 (orchestrator, after the independent review loop)

- **Repair eligibility is PROVEN from lineage, never inferred from marker
  text.** `schema_repair._marked_rows` admits a pending row only when it is
  generated drift (`source='agent'`, `method='schema-drift'`), carries the
  generation marker, AND `schema_drift._accept_generation` finds an accepted
  SAME-reason predecessor on the subject. A marked row without that lineage
  (misfiled, hand-crafted, or following an accepted removal/retype) stays in
  the ordinary review queue.
- **A materialising accept requires a PENDING row.** `_accept_schema_drift_materialise`
  returns 409 with zero writes for accepted/rejected/superseded rows, and the
  terminal transition is conditional on the row still being pending
  (`expect_status="pending"`), so the bulk route and a concurrent steward click
  cannot both win. Acknowledge-only accepts are unchanged.
- **`detect` on `/api/neptune/graph/repair-schema` is a JSON boolean** — any
  other type is a 422; `"false"` is not coerced to `True`.

