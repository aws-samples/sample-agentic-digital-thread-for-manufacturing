<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0075: Predicate-carrying DIRECT metric definitions — a closed `filter` block resolved through concept bindings, withheld as `unresolved_predicate` when a predicate concept is unbound

## Status

**Accepted — 2026-09-25** (proposed and implemented the same day; review
revision recorded under *Implementation notes*). **Amended 2026-09-26 by
[ADR-0076](0076-binding-disambiguation-and-dotted-columns.md)** — D2's
binding read / ambiguous arm and D3's "ambiguously bound" arm (see the
dated amendment at the end).

Builds out the `DirectMetric.filter` seam declared resolver-deferred by
[ADR-0014](0014-metric-layer-design.md) Decision B, inside the
[ADR-0066](0066-graph-backed-metric-definitions.md) metric-definition
document (D1 op-tree, D3 accept, D9 scope). Related:
[ADR-0015](0015-deterministic-metric-resolver.md) (deterministic resolver),
[ADR-0021](0021-two-layer-concept-model.md) /
[ADR-0068](0068-conversational-onboarding-context.md) Flag 4 (concept
overlays — deferred again here, with reasons),
[ADR-0081](0081-vocabulary-drift-field-binding-proposals.md) (the drift pass
that is this ADR's remediation path),
[ADR-0045](0045-multi-site-metric-fan-out.md) (honest-data statuses),
[ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality), [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md)
(computed, never LLM-judged).

## Context

A metric definition today is `aggregation(concept)` (direct) or an op-tree
over metrics (derived). Resolution walks `Field-[:MAPS_TO]->Concept` for the
ONE measure concept, composes `SELECT AGG(col), COUNT(col) FROM schema.table`,
and optionally ANDs a closed-vocabulary time window and an Athena
partition-prune WHERE.

Deployments define vocabulary whose meaning is a **row predicate over
columns that are already bound**, not a column of its own — "an item whose
status code is X", "an item whose category is one of a small set". A steward imports such a concept, then accepts a definition
`count(<that concept>)`. ADR-0081's vocabulary-drift pass is *correct* to
file no binding for it — no column IS that concept — so the metric resolves
`no-data` forever. The honest-data vocabulary has no word for "the
definition is fine, the row condition cannot be expressed". The result is a
metric that looks covered and is permanently empty, which is a worse answer
than a named gap.

The seam for the fix already exists: `DirectMetric.filter: Mapping[str, str]
| None  # ADR 0014 seam — resolver-deferred (S2.x)`. Nothing parses,
validates, composes or resolves it. The seed-registry byte-identity pin
(`tests/unit/test_metric_definition_boundaries.py`) hashes every dataclass
field including `filter: None`, so the field can change TYPE but not name
without a deliberate re-pin.

The alternative shape — a *concept overlay* that says "concept X = concept Y
where …" (ADR-0021 `ConceptOverlay`, ADR-0068 Flag 4) — would redefine what a
concept MEANS for every consumer (pre-check vocabulary, coverage, cards,
join discovery, the orchestrator's prompt vocabulary). A predicate on the
metric changes what ONE number counts. That is the smaller blast radius, and
it lands on the seam ADR-0014 reserved for exactly this.

## Decision

### D1 — A `filter` block on DIRECT definitions: a closed predicate vocabulary, AND-only, no free text

The ADR-0066 D1 document gains ONE optional field, valid on `kind: direct`
only (it joins the `_DIRECT_ONLY` set — a derived document carrying it is
rejected, never ignored):

```json
"filter": [
  {"concept": "<concept id>", "op": "eq",       "value":  <string | number | boolean>},
  {"concept": "<concept id>", "op": "in",       "values": [<string ...> | <number ...>]},
  {"concept": "<concept id>", "op": "is_null"},
  {"concept": "<concept id>", "op": "not_null"}
]
```

- **Closed op vocabulary** `PredicateOp = {eq, in, is_null, not_null}`. No
  `LIKE`, no comparison/range ops, no `OR`, no `NOT`, no expression strings,
  no `eval`. `in` is the ONLY disjunction form; clauses AND together.
- **The column is never in the document.** A clause names a CONCEPT; the
  column is resolved at resolve time through the metric's authoritative
  system's `MAPS_TO` binding (D2). A literal column name has no field to
  live in, so it cannot be smuggled in.
- **Typed literals, validated fail-closed at parse time and re-checked at
  render time.** Strings: 1–64 printable-ASCII characters, REJECTED (never
  escaped, never truncated) when they contain any of `' " ; \` CR LF — the
  `athena_tools._safe_sql_literal` register. Numbers: finite floats and
  integers within ±2^53 (exactly representable in the canonical float form;
  larger integers are REJECTED, never rounded; booleans are not numbers). Booleans: `eq` only. `in`: 1–32 values, homogeneous (all
  strings or all numbers), with a repeated value REJECTED (never silently
  de-duplicated). `is_null`/`not_null`: no value.
- **Caps and canonical form.** At most 8 clauses; at most ONE clause per
  concept (a contradiction is a mistake, not a query). Clauses canonicalize
  sorted by concept, `in` values sorted, numbers as floats — equal-meaning
  documents normalize to identical bytes, so the ADR-0066 D3 content digest
  and readback byte-compare keep working unchanged.
- `filter` requires an aggregation other than `latest` (the resolver has no
  filtered-LATEST shape; rejecting at parse beats failing at resolve).

The ONE home for the predicate types and the literal rule is
`agents/metric_predicate_rule.py` (`PredicateOp`, `Predicate`,
`check_predicate`, `check_filter`) — import-pure, stdlib-only — re-exported
by `agents/metric_layer.py` (the metric-type surface), so the document
validator and the code-registry validator (`validate_metric_layer`) enforce
the same rule. The rule is TOTAL: any malformed value (nested list/object,
`null`, a non-representable number) yields a named problem, never a raw
`TypeError`/`OverflowError`.
`DirectMetric.filter` keeps its NAME and becomes
`tuple[Predicate, ...] | None`; the 23 seed metrics keep `filter=None` and
stay byte-identical under the existing SHA pin.

### D2 — Predicate columns resolve through the SAME system's `MAPS_TO` binding, on the measure table, never by name

For each clause the resolver reads the authoritative system's binding for
the clause concept on the EXACT measure `SchemaEntry` — matched by name under
the system's `HAS_SCHEMA`, never by a dotted-tail comparison (so
`archive.events` is never `events`); v1 has no cross-table predicate — that
would need a join and is out of scope, D9. The read is
`RETURN DISTINCT se.id, f.name ORDER BY … LIMIT 2`: the cap applies AFTER
`DISTINCT`, so it proves the distinct-binding cardinality (0, 1, or 2+) no
matter how many bindings the concept has on other tables. The concept must
bind to EXACTLY ONE distinct field there: duplicate rows collapse, but two or
more distinct bindings are AMBIGUOUS and WITHHOLD (D3) naming the concept and
the first sorted candidates — choosing one would let graph row order decide
which quantity is computed. *(Amended 2026-09-26, ADR-0076 D1/D3: 2+ distinct
bindings resolve through the ONE binding rule — a steward
`AuthorityRule.field` naming one candidate — or WITHHOLD as
`ambiguous_binding`, every candidate named.)* The bound field name is used
EXACTLY as stored (no tail normalisation): it must pass
`db.validate_identifier` or the resolve fails closed (`MetricResolverError`)
before any SQL exists, and is double-quoted, exactly as the window predicate
already is on the scalar path. *(Amended 2026-09-26, ADR-0076 D4: it must pass
`metric_binding.validate_column_name` and renders through
`quote_column_identifier` — a dotted physical column is ONE quoted
identifier.)*
On the withhold path only, a second read names ONE other table the concept is
bound on (sorted, detail only — it never selects a column). One bounded graph
read per clause (≤ 8), inside the existing resolve; no per-turn cost elsewhere.

The predicate SQL fragment is composed by ONE pure renderer
(`agents/metric_predicate.py::render_predicate_sql`) from validated
identifiers and re-validated literals: `"col" = 'X'`, `"col" IN ('A', 'B')`,
`"col" IS NULL`, `"col" IS NOT NULL`, joined by ` AND `. The renderer
re-runs the literal rule, so a hand-built dataclass carrying a hostile
literal fails at render, before any execution — fail-closed twice, by
construction rather than by caller discipline.

### D3 — An unbound predicate concept WITHHOLDS: status `unresolved_predicate`, value `None`, the concept NAMED

When a clause's concept has no accepted binding on the resolved system's
measure table, the resolve does NOT execute. It returns a `ResolvedMetric`
with:

- `value = None`, `sample_count = None`,
- `status = "unresolved_predicate"` — a FOURTH honest-data status alongside
  `no-data` (read succeeded, zero samples), `unavailable` (boundary failure)
  and `invariant_violation` (declared sanity check failed). It is never
  conflated with any of them: `no-data` would claim the rows were counted;
  `unavailable` would invite a retry that cannot help; `invariant_violation`
  is about a computed value. This is "the definition cannot be evaluated
  here yet".
- `detail` that NAMES the concept, the system and the measure table — e.g.
  *predicate concept 'widget-color' has no field bound on system 'sys-a'
  table 'widget_events'* — or, when the concept is bound elsewhere, *is
  bound on table 'x', not the measure table 'y'* — or, when it is bound to
  several measure-table fields, *is ambiguously bound to 2+ distinct fields
  (first [...])*. *(Amended 2026-09-26: that last arm MOVED to ADR-0076's
  distinct `ambiguous_binding` status; `unresolved_predicate` keeps only the
  unbound and bound-elsewhere arms.)*
  The steward knows exactly which binding to file or retire.

The remediation is ADR-0081's path, not this ADR's: bind the concept to a
column (the drift pass proposes when a column name matches; otherwise a
manual `concept_mapping` proposal or the lineage-view correction). Nothing
here auto-binds, guesses a column, or falls back to the unfiltered count —
**a term that does not resolve is NAMED, never proxied.**

Surfaces: `dashboard._build_card_metric` maps the status to the card entry
status `unresolved-predicate` (the hyphenated register `invariant-violation`
already uses) BEFORE the `value is None ⇒ no-data` rule;
`metric_multisite._resolve_per_system` marks such a site
`unresolved-predicate` (not `no-data`, not `error`). The resolver exposes
the constant `UNRESOLVED_PREDICATE_STATUS`; nothing spells the string twice.

For paths that have no status slot — the grouped `resolve_metric_by_dimension`
and any caller that prefers exceptions — the same condition raises
`UnresolvedPredicateError(CoverageGapError)` carrying `concept`. The
by-dimension route already maps `CoverageGapError` to the `uncovered`
envelope with `missing_concepts=[concept]`, which names the same gap through
the existing needs-data presentation. One condition, two honest carriers,
zero new route plumbing.

### D4 — Composition order and dialect scope; a predicate never degrades

Scalar path (`_execute_direct_plan`): `_compose_sql` (unchanged bytes) →
Athena partition-prune / time-window WHERE (unchanged) → the metric's
window WHERE (unchanged) → the predicate WHERE, all appended through ONE
hoisted joiner (`_and_where`: ` WHERE ` if none yet, else ` AND `) — the
joiner `_apply_scalar_window` already contains, extracted rather than
copied. Grouped path (`metric_dimension._grouped_sql`): the predicate is
inserted through the existing `_insert_measure_where`, qualified `m.` in the
joined shape, after the prune and window insertions.

Dialects: PostgreSQL and Athena, the two the window predicates cover. On any
other backend (SiteWise Direct, MCP, unknown) a filtered metric raises
`StructuralMetricError` (routes mark it `structural: True`, no Retry). This
is deliberately DIFFERENT from the window's honest lifetime fallback: a
lifetime total is the same quantity over a wider interval and is captioned
as such; a count without its predicate is a DIFFERENT quantity. Dropping a
predicate would be a proxy, so it is a failure. For the same reason a
predicate column that Lake Formation denies is NEVER pruned and retried
without: predicate columns stay outside the Athena denied-column retry's
prunable list (only the composed measure/dimension columns are in it), so
the original error surfaces as the ordinary `unavailable` failure.

Literal/column type mismatches (a string literal against a numeric column)
are the backend's error and surface as the ordinary `MetricResolverError`
(`unavailable`) — never coerced, never reinterpreted.

### D5 — Scope: DirectMetric only, derived inherit, code metrics may declare it

`filter` is a DIRECT-metric property. A derived formula inherits predicates
through its `MetricRef` inputs (each input resolves with its own filter); a
derived document carrying `filter` is rejected at parse. Code-registry
metrics may declare `filter=` on the same dataclass field, validated by
`validate_metric_layer` (concepts in `CORE_CONCEPTS`, the D1 rule); this ADR
adds NO such seed metric — the 23 stay byte-identical.

### D6 — Lifecycle: parse → validate → diff → preview → accept, all computed, unchanged doors

- `parse_definition` normalizes the block (D1 rules, naming the offending
  `filter[i].<field>` on failure). `validate_definition` additionally
  requires every clause concept to be a known concept (core ∪ graph) in the
  deployment — a filter over an unknown concept is a typo, not a pending
  binding.
- `render_formula` renders the direct form with its predicate —
  `count(concept:widget-event) where widget-color eq 'BLUE' and
  widget-size in ('LARGE')` — so the admin list and the diff's
  before/after strings show it.
- `diff_definitions` reports `filter` in `added`/`removed`/`changed` (it
  iterates the canonical field list); `preview_metric_definition_mapping`
  therefore includes it, and a code-collision preview synthesizes `filter`
  from the code dataclass.
- Accept is UNCHANGED: validate → MERGE → exact readback through the
  steward queue only (ADR-0066 D3). The capture tool and the propose route
  pass the document through the same parser, so a filter authored in
  conversation or through the admin surface lands PENDING like any other.

### D7 — Predicate concepts join the metric's concept closure

`MetricLayer._walk_metric`, `_sat_metric` and `_missing_metric` include a
direct metric's predicate concepts. Consequences, all wanted:
`metrics_for_concept` (re-derivation) fires when a predicate concept's
binding changes; `is_satisfiable` (multi-site `systems_covering`, card
coverage) excludes a system that lacks the predicate binding instead of
resolving it into `unresolved_predicate` per site; `missing_concepts` NAMES
the predicate concept on the coverage strip. A filter-less metric's closure
is `{concept} ∪ ∅` — unchanged.

### D8 — Concept overlays stay DEFERRED (ADR-0021 / ADR-0068 Flag 4)

An overlay ("concept Y is concept X where …") would (a) redefine a concept
for EVERY consumer — pre-check vocabulary, coverage, join discovery, the
orchestrator's prompt vocabulary, value mappings — with no per-consumer
opt-in; (b) need its own resolution semantics in each of those paths;
(c) need a lifecycle for the overlay itself on top of the concept's. A
predicate on the metric touches one artifact, one resolver path, and reuses
a seam that has existed since ADR-0014. If overlays become necessary, they
can be COMPILED into metric predicates rather than the reverse — nothing
here forecloses them.

### D9 — Scope discipline: what is deliberately NOT built

- No `OR`/`NOT`, no `LIKE`, no comparison or range ops (`gt`, `between`) —
  the closed set is the safety property; extend it by ADR, with tests.
- No cross-table predicates (no join) — the clause concept must be bound on
  the measure table; a binding elsewhere is reported by name (D3).
- No predicates on derived metrics or on the `latest` aggregation.
- No fallback to the unfiltered aggregate on any backend (D4).
- No auto-binding of predicate concepts (ADR-0081 D7 stands: proposals
  only, steward accepts).
- No new lineage-visible column names beyond the bound columns already
  exposed by `MappingProvenance`.
- No new dashboard card; the admin metric-definition form's structured
  filter editor is a follow-up — the propose route and the capture tool
  accept the block today.
- No LLM anywhere in parsing, validation, rendering or resolution.

## Worked example (synthetic placeholders)

Deployment vocabulary already bound on the measure table `widget_events` of
system `sys-a`: `widget-event` → `widget_id`, `widget-color` → `color_code`,
`widget-size` → `size_code`.

```json
{
  "id": "blue-large-widgets",
  "kind": "direct",
  "label": "Blue Large Widgets",
  "unit": "widgets",
  "concept": "widget-event",
  "aggregation": "count",
  "filter": [
    {"concept": "widget-color", "op": "eq", "value": "BLUE"},
    {"concept": "widget-size",  "op": "in", "values": ["LARGE"]}
  ]
}
```

Rendered: `count(concept:widget-event) where widget-color eq 'BLUE' and
widget-size in ('LARGE')`.

Composed (PostgreSQL, no window):

```sql
SELECT COUNT(widget_id), COUNT(widget_id) FROM plant.widget_events
WHERE "color_code" = 'BLUE' AND "size_code" IN ('LARGE')
```

With a `24h` window and an Athena partition prune the order is prune →
window → predicate, each ANDed onto the one WHERE. If `widget-size` were
not bound on `widget_events`, the resolve returns
`status: "unresolved_predicate"`, `value: null`, `detail: "predicate concept
'widget-size' has no field bound on system 'sys-a' table 'widget_events'"`
and executes nothing; the steward files (or accepts the drift pass's)
`concept_mapping` for `widget-size`, and the next resolve computes.

## Considered alternatives

- **Concept overlays (ADR-0021).** Deferred (D8): whole-vocabulary blast
  radius, resolution semantics in every consumer, a second lifecycle.
- **A free-text `where` string.** Rejected: unvalidatable, undiffable, an
  injection surface, and "resolution" would be string trust. The closed
  predicate vocabulary is the point, exactly as the closed op-tree was for
  formulas (ADR-0014 C, ADR-0066 D1).
- **Literal column names in the definition.** Rejected: a definition would
  then be system-specific and would rot with the schema; resolution through
  bindings keeps definitions portable and makes ADR-0081 the remediation.
- **Treat an unbound predicate as `no-data`.** Rejected: `no-data` asserts
  the rows were read and there were none. That is a fabricated absence.
- **Treat an unbound predicate as a `CoverageGapError` on the scalar path
  too.** Rejected for the scalar path: the metric IS covered on its measure
  concept, so the card-coverage precheck passes and every `resolve_metric`
  caller would need new exception handling to avoid an `unavailable`
  misclassification. A status rides through unchanged callers and names the
  concept. The grouped path has no status slot, so there the existing
  `CoverageGapError → uncovered + missing_concepts` carrier is reused.
- **Fall back to the unfiltered aggregate where a backend cannot filter.**
  Rejected (D4): a different quantity presented under the same label is the
  proxy ADR-0066 forbid.
- **Escaping quotes in string literals instead of rejecting them.** Rejected:
  rejection is the fail-closed register the Athena introspection guard
  already uses; status/category codes do not contain quotes, and a value that
  does is a signal, not a case to accommodate.

## Consequences

- **Positive:** predicate-shaped vocabulary becomes definable AFTER deploy
  through the existing lifecycle, with no customer literal in the engine;
  the permanently-empty metric becomes a NAMED binding gap with a documented
  fix; the `filter` seam reserved in ADR-0014 is finally load-bearing; every
  judgement stays computed.
- **Negative / risks:** up to 8 extra bounded graph reads per filtered
  resolve (none for filter-less metrics); a new status every honest-data
  consumer must not collapse into `no-data` (pinned by tests on the
  dashboard and multi-site paths); the `unresolved-predicate` card status is
  rendered by the frontend as an unrecognized status today (the same
  treatment `invariant-violation` receives — the frontend does not enumerate
  metric statuses), which is honest but unstyled until a follow-up.
- **Verification (offline, unit lane):** parse/validate clauses (each
  fail-loud path names `filter[i].<field>`; injection-shaped literals
  rejected; derived + filter rejected; `latest` + filter rejected;
  canonical-byte determinism); `to_metric` builds `Predicate` tuples;
  `render_formula`/`diff_definitions` show the filter; resolver: bound →
  exact SQL with the WHERE in the D4 order for PostgreSQL and Athena, with
  and without window/prune; unbound → `unresolved_predicate` naming the
  concept with NO execution; bound-elsewhere → same status, distinct detail;
  non-SQL backend → `StructuralMetricError`; grouped: co-located and joined
  (`m.`) shapes, unbound → `CoverageGapError` with the concept; dashboard
  and multi-site keep the status distinct from `no-data`; **filter-less
  metrics produce byte-identical SQL and lineage key sets** (the existing
  golden pins pass unmodified); the seed-registry SHA is unchanged;
  `validate_metric_layer` rejects a code metric with a malformed filter;
  the concept-closure/satisfiability changes for filtered metrics.
  **Deployed (follow-up, not this change):** a filtered graph definition
  resolving on the real Athena path and the drift-pass → bind → resolve loop.

## Implementation notes (2026-09-25)

Recorded against the real code while implementing; none changes a decision.

- **`in` repeats are rejected, not de-duplicated** (D1 wording aligned): the
  document register rejects repeated `synonyms` rather than repairing them,
  and the module contract is "never a silently repaired document". Numbers
  canonicalize to floats BEFORE the repeat check, so `[1, 1.0]` is a repeat.
- **Binding seam signature.** `metric_resolver._predicate_bindings_for(system_id,
  table, predicates)` takes the ids rather than a `QueryPlan`, so the grouped
  resolver reuses it without synthesizing a scalar plan. It performs the
  backend gate (`StructuralMetricError`) and the per-clause binding read.
- **Grouped binding read** goes through `metric_resolver.neptune_query`
  (like the measure mapping `_resolve_table_column`), not the grouped path's
  injected `query=` seam.
- **Unbound-predicate lineage** carries the measure authority/mapping/plan
  with `sql: null` (nothing executed) and no `predicates` key.
- **Derived inheritance** (D5): a derived formula whose filtered input is
  withheld fails with a `MetricResolverError` that names the input's status
  and detail (`_eval_term`), so the unbound concept stays named one level up.
- **Multi-site** keeps its pre-existing treatment of `invariant_violation`
  (a value-None site reads `no-data` there); only `unresolved_predicate`
  gained a distinct site status, per D3. An all-sites-unresolved fan-out
  raises, so the dashboard falls back to the single-authority path, which
  returns the status.
- **50-line tripwire:** the direct-metric reference checks in
  `validate_definition` moved into `_validate_direct_refs`; the
  `resolve_metric_by_dimension` body stays at its HEAD length.
- **Frontend:** `frontend/src` does not enumerate metric statuses, so no
  renderer change was needed; `unresolved-predicate` renders with the same
  unstyled treatment `invariant-violation` receives today.

### Review revision (2026-09-25)

- **Total literal rule.** `check_predicate` now checks arity, then every
  literal's type/content, and only THEN `in` homogeneity/uniqueness — so an
  unhashable value can no longer reach `set()`. Integers beyond ±2^53 are
  rejected (`MAX_EXACT_INTEGER`) and the document canonicalizer passes them
  through unconverted, so `float(10**400)` can no longer overflow. Both the
  propose route (400 naming `filter[i].value(s)`) and the accept door (named
  `ValueError`, proposal stays pending, nothing written) are regression-tested.
- **Deterministic binding.** A predicate concept bound to two or more
  distinct measure-table columns withholds as `unresolved_predicate`
  (D2/D3) instead of taking the first graph row; the bound-elsewhere table
  list is sorted. Both are pinned with reversed-row-order tests.
- **Module split (file-size limit).** The predicate types + rule moved from
  `metric_layer.py` to the import-pure `metric_predicate_rule.py`
  (re-exported by `metric_layer`, so every import site is unchanged); the
  document `filter` parse/canonicalize/convert/render moved from
  `metric_definition.py` to `metric_filter_document.py` (errors still raise
  through `metric_definition._fail`, and `filter_from_document` /
  `filter_to_document` stay importable from `metric_definition`). Both
  source files are back under 800 lines.
- **Synthetic fixtures.** The worked example and every test fixture use
  deliberately synthetic `widget-*` placeholders.

### Review revision 2 (2026-09-25)

- **Exhaustive, exact-entry binding read.** The first revision's binding read
  was unscoped `LIMIT 50` with no ordering, so 49 other-table bindings could
  push a second measure-table binding off the page and the metric would
  compute on whichever column survived. The read now targets the measure
  `SchemaEntry` by exact name (`_PREDICATE_BINDING_CYPHER`) and returns
  `DISTINCT (se.id, f.name)` ordered with `LIMIT 2`
  (`_PREDICATE_BINDING_PROBE`) — the cap follows `DISTINCT`, so 2 rows prove
  ambiguity. The bound-elsewhere detail is a separate withhold-path read
  (`_PREDICATE_ELSEWHERE_CYPHER`) that never selects a column.
- **No lossy dot-tail normalisation.** `select_binding_rows` /
  `distinct_columns` (last-dotted-segment comparison) are replaced by
  `metric_predicate.distinct_bindings`, which keeps entry ids and field names
  exactly as stored. A dotted field (`payload.status`) fails
  `validate_identifier` and the resolve fails closed instead of querying
  `status`; a same-tail SchemaEntry on another schema is "bound elsewhere".
  Deviation from D2's earlier wording ("compare by table tail"): the measure
  table name comes from `_resolve_table_column`, which still tails
  `se.name`; onboarding writes bare table names (`se.name = tbl`, id
  `<system>.<tbl>`), so the exact-name match is equal in practice, and a
  dotted `se.name` would withhold rather than guess.
- Regressions: page-overflow (49 noise + 2 measure bindings withholds),
  same-tail different entry, dotted field, and a pin on the query shape and
  its `table` parameter.

### Amendment (2026-09-26) — ADR-0076

[ADR-0076](0076-binding-disambiguation-and-dotted-columns.md) supersedes the
predicate-binding mechanics above; nothing about the filter vocabulary,
composition order, lifecycle or the unbound/bound-elsewhere withhold changes.

- **D2 binding read.** The `LIMIT 2` probe (`_PREDICATE_BINDING_PROBE`) and
  `metric_predicate.distinct_bindings` are retired. The predicate concept's
  candidates are read by `metric_binding.PREDICATE_BINDING_CYPHER` (same exact
  `SchemaEntry {name: $table}` scope; one grouped row per `(se.name, f.name)`
  pair ordered, cap 25 PAIRS, all candidates named) and selected by the ONE rule
  `metric_binding.select_binding` shared with the metric's own column.
- **D2/D3 ambiguous arm.** 2+ distinct bindings on the measure table resolve
  through an ACTIVE `AuthorityRule` for `(system, concept)` whose steward-set
  `field` names exactly one candidate, else WITHHOLD as `ambiguous_binding`
  (not `unresolved_predicate`). No number changes — the case never computed.
- **Identifier rule.** "must pass `db.validate_identifier`" becomes "must pass
  `metric_binding.validate_column_name`, rendered by
  `quote_column_identifier`": `test_dotted_bound_field_is_never_tail_normalised`
  inverted into `test_dotted_bound_field_renders_as_one_identifier`
  (`"payload.color_code" IS NULL`).
