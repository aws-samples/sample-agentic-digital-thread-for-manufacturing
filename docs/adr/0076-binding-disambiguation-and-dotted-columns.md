<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0076: ONE deterministic concept-to-column binding rule (single → use; 2+ → steward-set `AuthorityRule.field` or WITHHOLD as `ambiguous_binding`) and dotted physical columns rendered as ONE quoted identifier

## Status

**Accepted — 2026-09-26** (proposed and implemented the same day;
deviations from the text below are recorded under *Implementation notes*).

Amends [ADR-0075](0075-predicate-metric-definitions.md) D2/D3 (the predicate
binding rule and the "ambiguously bound" arm of `unresolved_predicate`) and
closes the dotted-column deferral of
[ADR-0081](0081-vocabulary-drift-field-binding-proposals.md) (the drift
pass's `skipped.dotted` counter). Builds on
[ADR-0066](0066-graph-backed-metric-definitions.md) D8 (`AuthorityRule` is
the ONE authority home), [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md)
(the machine never writes authority; proposals never mutate the graph),
[ADR-0015](0015-deterministic-metric-resolver.md) (deterministic resolver),
[ADR-0045](0045-multi-site-metric-fan-out.md) (honest-data statuses),
[ADR-0050](0050-vocabulary-and-artifact-management.md) addendum (rule
lifecycle: soft deactivate / reactivate, never delete), and
[ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — no deployment literal enters the engine).

## Context

Two independent defects, one live deployment, one root cause: the engine has
no rule for a concept that is bound to MORE THAN ONE physical column of the
same system, and no way to address a physical column whose NAME contains
dots.

**Multi-binding.** Onboarding's mapper writes one `(Field)-[:MAPS_TO]->
(Concept)` edge per column it judges to carry a concept
(`onboard._dp_commit_concept_edges`). Nothing stops it from binding the same
concept to several columns of one table — on the live deployment one concept
is bound to three columns of the measure table (a flat column, a one-level
struct sub-field and a three-level struct sub-field) and another to two.
Three consumers then disagree about what that means:

1. The metric's OWN concept column (`metric_resolver._resolve_table_column`)
   reads the system-scoped traversal with `LIMIT 1` and **no `ORDER BY`** —
   whichever edge the graph happens to return first becomes the measure
   column. The number on the card depends on graph row order.
2. A predicate concept (ADR-0075 D2, review revision 2) correctly reads
   `DISTINCT` bindings on the exact measure `SchemaEntry`, `ORDER BY`, and
   WITHHOLDS on 2+ as `unresolved_predicate`. Honest — but the withhold
   names no remediation a steward can actually take short of deleting edges,
   and the status is shared with the genuinely-unbound case.
3. The accept writer for a `concept_mapping` correction
   (`self_learning._repoint_maps_to`) DELETEs every prior `MAPS_TO` edge for
   `(system, concept)` and MERGEs exactly one, i.e. the graph model already
   INTENDS one authoritative column per `(system, concept)` — but only after a
   steward has corrected a mapping; onboarding's multi-edge output never goes
   through it.

A steward who wants to keep all three bindings (they are all real — the
struct sub-fields ARE that concept in their nested records) but compute the
metric on one of them has no door. The existing `AuthorityRule` — "system X is
the authoritative source for concept C" — is the right home for that
decision: it is per `(system, concept)`, steward-writable, soft-deactivatable
with audit stamps (ADR-0050 addendum), already the ONE authority home
(ADR-0066 D8), and already survives re-onboarding (the deploy-time MERGE in
`onboard._dp_authority_rules` SETs `rule/system_id/concept/priority` only).

**Dotted physical columns.** In this Athena data a name such as
`parent.segment_type` is ONE physical column (Glue flattens struct members
into dotted names — live-verified; `athena_tools._segment_quoting_hint`
records the observed failure when a client segment-quotes it as
`"parent"."segment_type"`). The engine currently cannot use such a column
anywhere in the metric path:

- `db.validate_identifier` rejects dots, so the ADR-0075 predicate renderer
  fails closed on a dotted bound field (`test_dotted_bound_field_is_never_
  tail_normalised` pins this — as the *honest* behaviour given no renderer);
- `metric_resolver._mapping_from_row` tail-splits the column
  (`split(".")[-1]`), so a dotted MEASURE column silently becomes its last
  segment — the WRONG column, not a failure;
- the grouped composer and both window predicate composers validate through
  the same dot-rejecting regex;
- the ADR-0081 drift pass skips dotted unbound columns (`skipped.dotted`),
  citing a `rsplit` limitation of `_repoint_maps_to`. The Field id that writer
  MATCHes is the string `f"{system}.{table}.{column}"` — split point
  irrelevant — so the limitation is a documentation artefact, not a code one.

So a struct sub-field can be neither bound after onboarding nor used once
bound, and a bound one is silently mis-resolved on the measure path.

The invariants that constrain the answer: `AuthorityRule` is the ONE
authority home (no parallel disambiguation store); the machine never writes
authority (ADR-0053; only a steward route may write); identifiers and literals
fail closed; a term that does not resolve is NAMED, never proxied; computed, never LLM-judged (ADR-0053).

## Decision

### D1 — ONE binding-selection rule for the metric's own column AND every predicate concept, in `agents/metric_binding.py`

For a concept `C` on system `S`, the **candidates** are the DISTINCT
`(SchemaEntry.name, Field.name)` pairs reached by
`(S)-[:HAS_SCHEMA]->(se)-[:HAS_FIELD]->(f)-[:MAPS_TO]->(C)`, ordered
`ORDER BY table, column`, names EXACTLY as stored (no tail normalisation of
either), capped at `BINDING_CANDIDATE_CAP = 25` PAIRS (one grouped row per
`(table, column)`, provenance collected inside it — the cap counts pairs,
never provenance edges, so it can never hide a candidate below it). The read
asks for `BINDING_CANDIDATE_CAP + 1` pairs: a page that returns the sentinel
pair is NOT exhaustive and is itself an ambiguity — it withholds WITHOUT
reading any rule, and the steward write door rejects a `field` against it
(amended 2026-09-26, see implementation notes). Two scopes, one template:

- the metric's OWN concept: every SchemaEntry of the system (the binding
  DEFINES the measure table, so it cannot be pre-scoped);
- a predicate concept: the exact measure `SchemaEntry {name: $table}`
  (ADR-0075 D2, unchanged scope — v1 still has no cross-table predicate).

The selection is TOTAL and has exactly three outcomes:

1. **Exactly one candidate → use it.** `binding_basis = "single"`.
2. **Two or more candidates → an ACTIVE `AuthorityRule` for `(S, C)` that
   carries a `field` property naming ONE of the candidates decides.**
   `binding_basis = "authority_rule"`. The rule read is scoped
   `{system_id: $sid, concept: $concept}`, filters
   `coalesce(ar.status,'') <> 'deactivated'`, requires `ar.field IS NOT NULL`,
   and returns `DISTINCT (ar.table, ar.field)` — two distinct pairs (two
   active field-carrying rules for the same pair) is a fail-loud ambiguity,
   consistent with `GraphAuthorityResolver`'s multi-rule posture. The rule's
   `(table, field)` must match a candidate EXACTLY; a rule whose `field` names
   nothing in the candidate set (stale after a re-onboard, hand-written) does
   NOT select and the outcome falls to (3) with a detail that says so.
3. **Otherwise → WITHHOLD as `ambiguous_binding`** (D3), the candidates
   NAMED in `detail` in sorted order.

**No role, confidence or first-row heuristic — deliberately.** The MAPS_TO
edge carries `role ∈ {identifier, measure, correlate, derive}` and a mapper
`confidence`; picking "the `measure` role" or "the highest confidence" would
be deterministic, and it would still be a guess: the mapper's confidence is a
statement about its OWN text match, not about which column the deployment
means by the concept, and two bindings with equal role/confidence would need
a further tie-break that ends at column name — a lexicographic coin flip
wearing a justification. A deterministic-but-unjustified preference is a
PROXY (a related quantity under the requested label), exactly what the project rules
forbids. The only justified pick is one a steward made; the only honest
alternative is to say the set is ambiguous and list it.

**The basis is visible.** `MappingProvenance` gains `binding_basis:
str | None` (`"single"` | `"authority_rule"`; `None` where the rule does not
apply — dimension and order-column mappings, which this ADR does not select)
and it serializes into `lineage.mapping` unconditionally. This changes the
golden `mapping` key set pinned by
`test_golden_scalar_lineage_dict_keys_unchanged` by ONE key — a deliberate,
recorded re-pin: the new key is the whole point (a reviewer must be able to
see WHY that column, on every resolve), so it is not stripped for the
`single` case.

`metric_binding.py` is the ONE home: the Cypher templates
(`MEASURE_BINDING_CYPHER`, `PREDICATE_BINDING_CYPHER`, `BINDING_RULE_CYPHER`),
the pure selector `select_binding(candidates, rules) -> BindingChoice |
BindingAmbiguity`, the status constant, the candidate/rule row normalisers,
and the column-identifier renderers of D3. It imports `db.validate_identifier`
and nothing from `metric_resolver` (the resolver imports IT; no cycle), holds
no graph client (the resolver executes the Cypher through its own
`neptune_query` seam so the offline lane's fakes keep working), no LLM, no
demo literal. `metric_resolver._resolve_table_column` and `_bind_predicate`
become thin callers; `metric_predicate.distinct_bindings` (the 0075
normaliser) is superseded by the shared candidate normaliser and removed.

### D2 — The steward door: `POST /api/metrics/set-authority` gains an optional `field`, is steward-gated, offloaded, and MERGE + readback

The EXISTING route is extended, not duplicated. Verified before deciding:
`/api/metrics` is NOT under the `AuthMiddleware.ADMIN_PREFIXES`
(`["/api/admin"]`) and the router is mounted bare
(`app.include_router(metrics_router)`), so today ANY authenticated user can
rewrite concept authority. That is a pre-existing gap this ADR closes as a
precondition for carrying `field`: **the route becomes steward-gated
(`dependencies=[Depends(check_steward)]`, fail-closed on an ABSENT
`is_admin` verdict — the `server._check_steward` shape), and its sync body
moves behind `offload`** (it will now perform graph READS as well as writes;
the `async def` body may contain nothing but the `await`).

Request model (`models.SetAuthorityRequest`):

```python
class SetAuthorityRequest(BaseModel):
    concept: str
    system_id: str
    field: str | None = None    # exact stored Field.name — may contain dots
    table: str | None = None    # exact stored SchemaEntry.name; required only
                                # when `field` alone matches candidates on 2+ tables
```

`field` absent ⇒ today's behaviour, byte-identical (the two writes pinned by
`test_set_authority_issues_expected_writes`; no candidate read at all).
`field` present ⇒ the payload reads the D1 candidates for `(system_id,
concept)` (the same `MEASURE_BINDING_CYPHER`) and validates:

- zero candidates → **400** `{"error": "...no MAPS_TO field for concept ...",
  "candidates": []}`;
- `field` matches no candidate → **400** naming the candidates
  (`"candidates": [{"table", "column"}, ...]`, sorted);
- `field` matches candidates on 2+ tables and `table` is absent → **400**
  asking for `table`, naming them;
- `field` + `table` given but the pair is not a candidate → **400**.

Only an exact candidate reaches the write. `set_authority(concept, system_id,
*, field=None, table=None, actor="")` MERGEs the same `ar-{system}-{concept}`
node and, when `field` is given, additionally SETs `ar.field`, `ar.table`,
`ar.field_set_by`, `ar.field_set_at`; then **re-reads the node and compares
`(ar.field, ar.table)` byte-for-byte to the intent — a mismatch returns
`status: "failed"` with `readback: false`**, the ADR-0066 D3 posture. The
response echoes `field`, `table`, `candidates` (so the steward sees what was
NOT chosen) and `readback: true`.

Why TWO properties (`field` and `table`) rather than one joined
`table.column`: the metric's own concept can bind on several tables, a Field
name is unique only within its SchemaEntry, and a dotted Field name makes any
joined string ambiguous to split (`t.a.b` — which dot?). The candidate
identity is the pair, so the rule stores the pair. Selection (D1) matches on
both when `ar.table` is present; a hand-written rule with `field` but no
`table` matches on `field` alone and must still yield exactly one candidate.

**Lifecycle rides the existing rule lifecycle, unchanged.** Deactivating the
rule (`POST /api/thread/rules/{id}/deactivate`, steward-gated) retires the
`field` with it — the D1 read filters deactivated rules; reactivating
restores it. The deploy-time MERGE in `onboard._dp_authority_rules` and the
machine's discriminated reactivation (`_dp_reactivate_machine_deactivated_
rule`) never touch `field`/`table` (verified: their `SET` lists are
`rule/system_id/concept/priority` and `status/reactivated_*` respectively) —
a steward's choice survives re-onboarding; if the re-onboard drops the chosen
column the resolve WITHHOLDS naming the rule's now-stale `field` rather than
silently picking another (D1 outcome 3). **The machine never writes `field`:**
`set_authority` is the only writer, reachable only through the steward-gated
route (and its direct Python callers, which are tests). Nothing in onboarding,
drift, proposal accept, or any sweep sets it.

**The side effect of setting `field` on a system that is not the concept's
sole authority is the existing per-system rule shape, not a new one.**
`AuthorityRule` doubles as the "which SYSTEM is authoritative" record. A
field-carrying rule for `(sys-b, C)` alongside `(sys-a, C)` is exactly the
shape `onboard._dp_authority_rules` already mints per plant for multi-site
deployments, and multi-site fan-out (ADR-0045) resolves per pinned system
without consulting the cross-system authority read. The single-authority
path's fail-loud posture on two active rules naming different systems
(`GraphAuthorityResolver`) is unchanged and out of scope here.

**Proposal-lifecycle integration is DEFERRED.** v1 has one door — the direct
steward route, already steward-only, validate → MERGE → readback. A
`binding_choice` proposal type (machine-filed "this concept is ambiguous on
system S; candidates …", steward accepts one) would let the review queue
surface the gap proactively; it is recorded in
the maintainers' deferred-work list with this ADR as the seam.
Also deferred: clearing `field` without deactivating the rule (today: set
another field, or deactivate), and stamping `ar.method = 'user_confirmed'`
on the steward write (it would change `concept_authority`'s steward-asserted
labelling — a separate decision).

### D3 — `ambiguous_binding`: ONE new status for both the metric's own column and a predicate concept; `unresolved_predicate` keeps ONLY the no-binding case

A FIFTH honest-data status, `metric_resolver.AMBIGUOUS_BINDING_STATUS =
"ambiguous_binding"`, alongside `no-data`, `unavailable`,
`invariant_violation`, `unresolved_predicate` (and the preview-only
`unresolved_input`). It means: *the concept is bound to 2+ columns here and no
steward has chosen one* — `value None`, `sample_count None`, nothing
executed, `detail` naming the concept, the system, the table(s) and the
sorted candidate columns, plus the rule's stale `field` when one exists.

Why a NEW status rather than reusing `unresolved_predicate` or a
`CoverageGapError`: the three existing carriers each assert something false
here. `no-data` asserts rows were read; `uncovered`/`CoverageGapError` asserts
NO system maps the concept (the opposite is true — it is mapped twice);
`unresolved_predicate` asserts the predicate concept has no usable binding
and points the steward at ADR-0081's bind-a-column remediation, which is
wrong for this case (binding a fourth column does not help — CHOOSING one
does). The remediation for `ambiguous_binding` is D2's route, and the status
name must send the steward there. Two distinct causes with two distinct
fixes get two distinct names.

Why ONE status for both the measure column and a predicate concept: it is
the same condition (2+ distinct bindings of one concept on one system, no
steward choice) decided by the same rule (D1); the `detail` says which
concept — the metric's own or a clause's — and the fix is the same route.
ADR-0075 D3's "ambiguously bound to 2+ distinct fields" arm therefore
MOVES from `unresolved_predicate` to `ambiguous_binding`;
`unresolved_predicate` retains exactly the unbound and bound-elsewhere
arms. This is a status-text migration for a case that has never computed a
value (it withheld either way), so no number changes — only the name of the
gap and the door it points to.

Carriers, mirroring ADR-0075 D3's two-carrier design:

- **Scalar path** (`resolve_metric` / `resolve_definition`): a
  `ResolvedMetric` with `status = AMBIGUOUS_BINDING_STATUS`. For the
  metric's own column the withhold happens BEFORE a `QueryPlan` exists, so
  `query_plan = None`, `lineage.mapping = None`, `lineage.authority` present
  (the system IS known); for a predicate concept the plan and measure mapping
  exist and ride in lineage with `sql: null`, as today's unresolved-predicate
  lineage does.
- **Grouped / heatmap paths** (no status slot): `AmbiguousBindingError(
  CoverageGapError)` carrying `concept` and `candidates`, so the existing
  `CoverageGapError → uncovered + missing_concepts=[concept]` envelope names
  the gap with `detail` carrying the candidates — the ADR-0075 precedent, with
  the same honesty caveat that `uncovered` is the nearest existing grouped
  envelope, not a perfect word (a distinct grouped envelope is a follow-up).
- **Derived metrics**: an input withheld as `ambiguous_binding` fails the
  formula through the existing `_eval_metric_ref` "input withheld (status):
  detail" message — the concept and candidates stay named one level up.
- **Dashboard** (`dashboard.card_entry_from_resolved`): card entry status
  `ambiguous-binding` (the hyphenated register), mapped in the `withheld`
  table BEFORE the `value is None ⇒ no-data` rule. **Multi-site**
  (`metric_multisite._WITHHELD_SITE_STATUS`): site status
  `ambiguous-binding`, NOT in `_READ_SITE_STATUSES` (nothing was read).
  **Frontend**: verified that `frontend/src/views/dashboard/Cards.tsx`
  DOES enumerate withheld statuses (`WITHHELD_LEADS`, added after
  ADR-0075's implementation note said it did not) — one entry
  `'ambiguous-binding': 'Withheld — the concept binds to several columns; a
  steward must choose one'` so the card says what it means instead of the
  generic Unavailable text.

Nothing spells the string twice: consumers import the constant.

### D4 — Dotted physical columns are ADDRESSABLE: ONE quoted identifier, each segment validated, flat names byte-identical

`metric_binding.py` gains the ONE column-identifier home, three functions:

- `validate_column_name(name) -> str` — splits on `.`, requires EVERY
  segment to match `db.validate_identifier`'s regex (`^[a-zA-Z_][a-zA-Z0-9_]*$`),
  rejects empty segments (leading/trailing/double dots), returns `name`
  unchanged. A flat name is the one-segment case and behaves exactly like
  `validate_identifier`.
- `render_column_identifier(name) -> str` — the SCALAR register: a flat name
  renders BARE (`quantity_on_hand`, byte-identical to today's `_compose_sql`
  output); a dotted name renders as ONE double-quoted identifier
  (`"parent.segment_type"`) — never segment-split, never underscored.
- `quote_column_identifier(name) -> str` — the ALREADY-QUOTED register
  (grouped composer, predicate renderer, both window predicates, which emit
  `"{col}"` today): always `"<name>"`; a dotted name quotes identically
  (`"a.b.c"`), so the shape of every existing quoted fragment is unchanged.

Adoption sites, and why the flat output is byte-identical at each:
`metric_resolver._compose_sql` (measure column — bare today → bare via
`render_column_identifier`); `metric_predicate._render_one` (`"{col}"` today →
`quote_column_identifier`); `metric_dimension._compose_grouped_sql` /
`_compose_latest_grouped_sql` (measure column, dimension column, join fields
and order column — all `"{col}"` today); `metric_dimension._athena_window_
predicate` / `_pg_window_predicate` (`"{ident}"` today). Schema and table
names keep `validate_identifier` — nothing here makes a table name dotted.
Pinned: the existing golden SQL tests for every code-registry metric pass
unmodified; a NEW test renders one dotted measure column, one dotted predicate
column and one dotted window column and asserts the exact quoted strings
and the ABSENCE of `"."` (segment-split) and `_` substitution.

`metric_resolver._mapping_from_row` stops tail-splitting the COLUMN (the
table keeps its schema-tail split — onboarding writes bare `se.name`, and a
qualified JoinPath table is normalised elsewhere). The Athena denied-column
retry already matches nested columns by full dotted name
(`test_retry_prunes_nested_column_by_full_dotted_name`), so the measure
column reaches it unchanged. PostgreSQL: a dotted column name is legal when
quoted, so the same rendering is correct on both SQL dialects; the SiteWise
path never composes SQL and is untouched.

`test_dotted_bound_field_is_never_tail_normalised` (ADR-0075) inverts: a
dotted predicate binding now RENDERS as `"payload.color_code" IS NULL` and
executes; the "never tail-normalised" property it protected is kept by the
new exact-name assertion.

### D5 — The ADR-0081 drift pass scores dotted columns; `skipped.dotted` is dropped

`vocabulary_drift._scan_system` no longer partitions dotted unbound columns
out of the candidate list: they are scored by `vocab_fit` tokens like any
other column (`vocab_fit._name_tokens` already splits on every
non-alphanumeric character, so `parent.segment_type` yields
`parent`/`segment`/`type` today — verified; no tokenizer change). A winner is
filed as `to_field = f"{table}.{column}"` exactly as flat winners are; the
accept writer's Field id concatenation resolves it to the right node (see
Context). The per-system `skipped.dotted` counter is **dropped rather than
reported as 0**: a permanent zero is a claim that the pass still classifies
something it no longer classifies. The top-level `skipped` key set stays
EXACTLY `{bound, generic, tombstoned_or_gated}` (pinned). ADR-0081's
docstring/contract line about dotted columns is replaced by the D4 fact.

Out of scope (deferred, recorded): `correct_mapping_route`'s
`to_field.count(".") != 1` guard still refuses a dotted column from the
lineage-view correction form; the steward's dotted door in v1 is the drift
proposal (this ADR) or the `user-define` proposal route.

### D6 — Scope discipline: what is deliberately NOT built

- **No struct traversal semantics.** A dotted name is an opaque physical
  column name that happens to contain dots; nothing here parses `a.b.c` as a
  path, walks a struct type, or maps `a` to a parent. The Glue-flattened name
  IS the column.
- **No role/confidence/first-row/latest-version heuristics** (D1).
- **No auto-authority.** Nothing files or writes `field` but the steward
  route; the drift pass, onboarding, sweeps and the answer auditor never do.
- **No parallel disambiguation store** — `field`/`table` live on the ONE
  `AuthorityRule`.
- **No change to dimension-column selection.**
  `metric_dimension._prefer_colocated_mapping` still picks a co-located
  DIMENSION column by identifier-role → confidence → name, and
  `_resolve_order_column` still takes the first co-located time column.
  Both are the same defect class in principle; both are recorded as deferred
  rather than changed here because (a) a dimension groups rows rather than
  measuring them and (b) the window column degrades to the HONEST lifetime
  fallback (`window: null`, captioned) rather than a wrong number. They get
  their own decision if a deployment shows the ambiguity.
- **No new grouped envelope** for ambiguity (reuses `uncovered +
  missing_concepts`, D3).
- **No proposal-type integration** (D2, deferred).
- **No LLM** anywhere: candidate reads, rule reads, selection, validation and
  rendering are all computed.

## Worked example (synthetic placeholders)

System `sys-a`, measure table `widget_events`, concept `widget-kind` bound to
three columns: `kind_code`, `parent.kind_code`, `detail.trigger.kind_code`.
Metric `count(widget-kind)`.

Before: `_resolve_table_column` returns whichever edge the graph lists first
and, for a dotted one, tail-splits it to `kind_code` — a silent proxy either
way.

After, no rule: `status: "ambiguous_binding"`, `value: null`, `detail:
"concept 'widget-kind' is bound to 3 distinct columns on system 'sys-a'
(widget_events: 'detail.trigger.kind_code', 'kind_code', 'parent.kind_code');
a steward must choose one via POST /api/metrics/set-authority {field}"`.
Card: `ambiguous-binding`. Nothing executed.

Steward: `POST /api/metrics/set-authority {"concept": "widget-kind",
"system_id": "sys-a", "field": "parent.kind_code"}` → candidates read →
match → MERGE `ar-sys-a-widget-kind` SET `field='parent.kind_code',
table='widget_events'` → readback equal → `200 {"status": "ok", "field":
"parent.kind_code", "table": "widget_events", "readback": true,
"candidates": [...]}`.

Next resolve: `SELECT COUNT("parent.kind_code"), COUNT("parent.kind_code")
FROM plant.widget_events`; `lineage.mapping = {"table": "widget_events",
"column": "parent.kind_code", ..., "binding_basis": "authority_rule"}`.

Steward instead sends `{"field": "kind_cod"}` → `400 {"error": "field
'kind_cod' is not a binding of concept 'widget-kind' on system 'sys-a'",
"candidates": [...]}`. Nothing written.

## Considered alternatives

- **Prefer the `measure` role, then the highest confidence, then the
  shortest/flattest name.** Rejected (D1): deterministic but unjustified —
  the mapper's confidence measures its text match, not the deployment's
  meaning; a lexical tie-break is a coin flip. A proxy under the requested
  label is the failure mode this repo forbids.
- **Have onboarding write only ONE edge per `(system, concept)`.** Rejected
  as the fix: it destroys information (the other columns DO carry the
  concept; the steward may want them for predicates or joins later) and it
  moves the arbitrary choice earlier rather than removing it. It remains a
  legitimate steward correction through the existing `concept_mapping`
  accept writer, which is exactly what that writer does.
- **A new `BindingChoice` node or a property on the `MAPS_TO` edge.**
  Rejected: a second authority home, the flip-flop class ADR-0050/0066 D8
  closed; an edge property has no lifecycle (no deactivate/audit stamps) and
  would be overwritten by onboarding's edge MERGE.
- **Reuse `unresolved_predicate` for the ambiguous case (status quo of
  ADR-0075).** Rejected (D3): it names the wrong gap and points at the wrong
  remediation; it also has no arm at all for the metric's OWN column.
- **Store the choice as `"table.column"` in one property.** Rejected (D2):
  unsplittable once column names contain dots.
- **Segment-quote dotted names (`"a"."b"`) or underscore them (`a_b`).**
  Rejected (D4): both are live-observed wrong; the name is one physical
  column.
- **Widen `db.validate_identifier` to accept dots.** Rejected: that regex
  guards schema/table names and Cypher identifiers across the codebase; a
  dotted TABLE would be a different bug. The column rule is its own function
  that reuses the segment regex.
- **Keep `skipped.dotted` at a permanent 0.** Rejected (D5): a counter for a
  class the pass no longer has is a false claim of classification.
- **Gate `set-authority` by moving it under `/api/admin`.** Rejected: it
  would break the route's URL for every caller and doc; the
  `Depends(check_steward)` registration dependency is the repo's precedent
  for steward-gating a route outside the admin prefix
  (`/api/thread/rules/*`, `/api/neptune/graph/repair`).

## Consequences

- **Positive:** the measure column is deterministic on every resolve and
  its basis is visible in lineage; a multi-bound concept becomes a NAMED,
  steward-fixable gap with one door instead of a row-order lottery; predicate
  metrics blocked by ambiguity become computable after one steward action;
  struct sub-fields become bindable (drift) and usable (measure, predicate,
  grouped, window); a pre-existing authorization gap on `set-authority` is
  closed; the machine still never writes authority.
- **Negative / risks:** metrics that today compute on an arbitrary edge in a
  multi-bound deployment will WITHHOLD as `ambiguous_binding` until a steward
  chooses — honest, but visible on dashboards after deploy (the detail names
  the fix; smoke/eval lanes will flag it as a status change, not a fabrication).
  One golden key-set pin is deliberately re-pinned (`binding_basis`). Up to
  one extra bounded graph read per resolve only when 2+ candidates exist
  (the rule read); zero for single-bound concepts. `set-authority` becomes
  steward-only — any non-admin automation calling it (none found in repo
  scripts or frontend) would now get 403.
- **Verification (offline, unit lane):** candidate selection under REVERSED
  graph row order yields the same choice/withhold; rule-picked column is
  used and lineage says `authority_rule`; a rule naming a non-candidate is
  rejected 400 at the route AND ignored at resolve (withhold, detail names
  the stale field); two active field-carrying rules withhold; deactivated
  rule's field is ignored; the predicate arm withholds as `ambiguous_binding`
  (no longer `unresolved_predicate`) while unbound/bound-elsewhere still say
  `unresolved_predicate`; dotted measure/predicate/window/grouped columns
  render as ONE quoted identifier with segment validation and hostile
  segments rejected; flat SQL for EVERY code-registry metric is byte-identical
  to HEAD; drift scores dotted columns and files `table.a.b.c`, top-level
  `skipped` keys unchanged, `dotted` key absent; dashboard card and
  multi-site site status `ambiguous-binding` distinct from `no-data`;
  steward gate 403 (non-admin and ABSENT verdict) never reaches the writer;
  the async handler body calls only `offload`; every touched function ≤ 50
  lines (test_response_integrity_iteration5/6 tripwires); frontend lane green with the new lead.
  **Deployed (follow-up, not this change):** on the live deployment, set
  `field` for the two multi-bound concepts and confirm the predicate metrics
  compute; confirm a dotted measure column executes on Athena as one quoted
  identifier.

## Migration notes

- **ADR-0075 D2/D3** — amended by reference: the "2+ distinct bindings
  WITHHOLD as `unresolved_predicate`" sentence becomes "2+ distinct bindings
  resolve through ADR-0076 D1 (steward `AuthorityRule.field`) or WITHHOLD as
  `ambiguous_binding`"; `unresolved_predicate` keeps the unbound and
  bound-elsewhere arms only; the "must pass `db.validate_identifier`" clause
  becomes "must pass `metric_binding.validate_column_name`, rendered by
  `quote_column_identifier`". The `LIMIT 2` probe is replaced by the D1
  candidate read (cap after DISTINCT, all candidates named).
- **ADR-0081** — the "Dotted struct columns are SKIPPED" contract line in
  `vocabulary_drift` and the `skipped.dotted` counter are removed (D5); the
  ADR gains a dated note closing the deferral and pointing here.
- **Maintainer notes** — one invariant line (binding rule + dotted rendering +
  steward-only `field`); the ADR-0075 line's "2+ distinct measure-table
  columns WITHHOLDS as … `unresolved_predicate`" clause is updated to name
  `ambiguous_binding`.
- **conventions.md** — the ADR-0075 paragraph's binding sentences are
  updated the same way; `deferred.md` gains the D2/D6 deferrals.

## Implementation notes (2026-09-26)

Implemented as decided (D1–D6), offline-verified on the unit lane
(10486 passed / 3 skipped; base 10358 / 3) and the frontend lane. Deviations,
each fixed against the code rather than the text:

- **`set_authority` moved to `agents/metric_authority.py`** (re-exported by
  `metric_resolver`, so every caller and the pinned two-write test are
  unchanged). D2 places the writer and `_set_authority_payload` in the
  resolver; adding them there would have grown `metric_resolver.py` past its
  base length. The module holds the ONE writer (`set_authority`, the
  `field` SET + exact readback) and the route's sync body
  (`set_authority_payload`); graph I/O still goes through
  `metric_resolver.neptune_query/neptune_write` (looked up lazily) so the
  offline seams apply. A source tripwire pins `ar.field = ` to this module.
- **`agents/steward_gate.py::check_steward`** is the router-importable copy
  of `server._check_steward` (routers cannot import the app module). Folding
  the two homes is deferred (pinned by `tests/unit/test_steward_gate_router.py`).
- **`validate_column_name` does not call `db.validate_identifier`**: it
  full-matches every `.`-segment against the same character class.
  `validate_identifier` uses `re.match(...$)`, which admits a trailing
  newline; the column rule is strictly tighter (a flat name differs only on
  that pathological input). Its error text keeps the `Invalid identifier:`
  prefix so existing matchers hold. `metric_binding` therefore imports only
  the stdlib.
- **Candidate read groups at the `(table, column)` PAIR grain (2026-09-26).** The first cut returned `DISTINCT` over
  `(table, column, role, confidence, source, version)` and applied
  `LIMIT 25` to THAT grain — 25 provenance variants of one pair could fill
  the page, hide a second pair, and collapse to a false `single`. The read
  now returns ONE row per pair (implicit grouping, provenance gathered as
  `collect({role, confidence, source, version}) AS edges`), ordered
  `table, column`, and the cap counts pairs. `candidates_from_rows` keeps,
  per pair, the ONE real edge with the smallest key over
  `(role, confidence, source, version)` — confidence under a total numeric
  → text → None order — so the lineage provenance is graph-row-order
  independent and never a field-wise blend. Flat per-edge rows (test fakes)
  are still accepted. Regressions: 30 duplicate-provenance rows preceding a
  second pair withhold; confidence-only reversal keeps the same edge.
- **A non-exhaustive candidate page never selects and never validates a
  write (2026-09-26).** The capped read could return exactly 25
  pairs while a 26th — possibly the SAME field on another table — was hidden;
  `select_binding` still read the rule and picked, and the set-authority door
  validated against the partial page. The read now uses a cap-plus-one
  sentinel (`BINDING_CANDIDATE_READ_LIMIT = BINDING_CANDIDATE_CAP + 1`);
  `is_truncated(candidates)` is the ONE exhaustiveness test. A truncated page
  withholds as `ambiguous_binding` ("more than 25 distinct columns … the
  candidate set is not exhaustive"), names the first 25 sorted candidates and
  performs NO rule read; the route answers 400 and writes nothing. Exactly 25
  candidates is exhaustive and selects normally. Regressions: selection,
  resolver (rule for a visible field, hidden same-field duplicate) and route.
- **Heatmap composer adopts `quote_column_identifier` too** (D4 lists the
  scalar, predicate, grouped and window sites). With the column tail split
  gone, a dotted measure/dimension/threshold column would otherwise reach
  `validate_identifier` and raise an unhandled `ValueError` on the heatmap
  route. Identifier prep was hoisted into `_heatmap_identifiers` (keeps the
  composer ≤ 50 lines); every pinned heatmap SQL string is byte-identical.
  The heatmap `uncovered` envelope names the ambiguous concept via
  `missing_concepts` but carries no candidate detail (deferred).
- **Predicate-arm detail is prefixed `predicate `** (`predicate concept 'x'
  is bound to 2 distinct columns …`) so the steward can tell the metric's own
  concept from a clause's.
- **A `field` written on a DEACTIVATED rule is not reactivated** and the
  resolve ignores it (the D1 read filters deactivated rules) — lifecycle stays
  the rule routes'; recorded in `deferred.md`.
- **Frontend**: `WITHHELD_LEADS` in `frontend/src/views/dashboard/Cards.tsx`
  does enumerate withheld statuses (the task brief said it did not); the
  `ambiguous-binding` lead was added with one vitest.
- **Test fakes** that routed the measure read on `"LIMIT 1" in cypher` now
  route on `cypher == mr._MEASURE_BINDING_CYPHER`
  (`test_metric_resolver.py`, `test_heatmap_resolver.py`); the golden mapping
  key set is re-pinned with `binding_basis` (value `"single"` asserted).
