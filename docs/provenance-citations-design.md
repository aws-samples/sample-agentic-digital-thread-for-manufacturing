<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Per-claim provenance in answers—three-tier citation surfacing

> **Maintainer documentation**—working notes for contributors and AI agents,
> not part of the user-facing guide.

How a citation in an answer goes from "queried a system agent" to a concrete,
inspectable evidence trail. This is a **design doc**: the *Today* section
describes the current state with code anchors; the rest is the proposed
contract and the wave plan to ship it.

## The one-sentence contract

Every inline citation resolves to a **server-assembled provenance record**—source
system, physical entity, scope, result shape, knowledge-graph route,
and capture timestamp—surfaced in three tiers of increasing detail, and a
claim with **no** captured evidence renders as *unverified* rather than
disappearing.

## The three tiers

| Tier | Surface | Content | Trust posture |
|---|---|---|---|
| 1 | Inline citation chip | Concrete source: system + entity (table) + time window | One line, always visible |
| 2 | Click-open evidence card | System, table/column(s), filter/window, row scope, query timestamp, which KG rules routed/translated—**plain language, no SQL** | Untrusted text, React text nodes only |
| 3 | "Technical detail" expander | Exact executed query text, raw-result access through the claim-check `dataset_ref` handle, KG artifact ids + provenance stamps | Gated; own wave |

## Today—the existing machinery (current implementation)

The parts already exist in separated form. The gap
is purely **assembly and surfacing**.

### (a) Per-tool-call capture—two parallel captures per call

`InvocationToolResultCapture` (`agents/tool_result_capture.py:264`) hooks every
system-agent tool call and performs two captures:

- **Public, metadata-only**—`safe_tool_result_metadata`
  (`agents/tool_result_capture.py:219`) retains an allowlist of counts/statuses
  (`row_count`, `item_count`, `truncated`, a coarse `safe_error_category`) and
  never copies payload content. Capped at 4 results × 1 KB
  (`agents/tool_result_capture.py:15-16`).
- **Trusted, server-side**—`capture_trusted_tool_result`
  (`agents/response_integrity_capture.py:583`) mints evidence observations from
  the server-owned connector boundary: `system_id`, `boundary`, `tool_name`,
  `queried_at`, the **executed query text** (`_trusted_executed_query`,
  `agents/tool_result_capture.py:254`, reads the connector's
  `_adt_executed_query` field), and the physical schema for projection
  validation. Budget-bounded (`MAX_TOOL_CALLS_PER_TURN`,
  `MAX_LINEAGE_RECORDS`).

Everything Tier 2 needs already flows through this seam once per tool call.

### (b) Typed Mode-A evidence arrays

`SystemAnswer` (`agents/response_integrity_contracts.py:270`) carries
`evidence: tuple[SourceEvidence, ...]`. `SourceEvidence`
(`agents/response_integrity_contracts.py:141`, extending `SourceMetadata`
at `:127`) is the closest existing thing to a provenance record:
`evidence_id`, `system_id`, `boundary`, `tool_name`, `queried_at`,
`source_observed_start/end`, `freshness`, `completeness`, `row_count`,
`item_count`, plus `semantic_field`, `physical_field`, `locator`,
`canonical_type`. Claims bind to it through `evidence_ids` / `ClaimInput`
(`agents/response_integrity_contracts.py:150`). The envelope is emitted by
`extract_mode_a_result` (`agents/tool_result_capture.py:400`) and crosses the
gateway as the system-agent tool result—today the orchestrator consumes it
and the structure is **discarded before the client**.

### (c) Claims-vs-evidence comparison (ADR-0046/0047)

`response_integrity_mode()` (`agents/config.py:24-44`) is the three-mode flag:
`report-only` (default—capture runs in shadow), `annotate` (`annotate_answer`,
`agents/response_integrity_annotate.py:161`—text verified against captured
observations, fabrication hard-block), `enforce` (`gate_final_result`,
`agents/response_integrity_rendering.py:140`—typed envelope replaces the
answer). The verdict already reaches the client as a metadata frame
`{"response_integrity": {"verdict": ..., "mode": ...}}` (emitted via
`integrity_metadata`, `agents/runtime_integrity.py:129`; shape pinned by
`tests/unit/test_runtime_entrypoint_integrity_marker.py:30`). Bounded result
shaping (row/cell/byte caps + `partial_reason`) lives in
`BoundedQueryAccumulator` (`agents/response_integrity_query.py:55`).

### (d) The citation pipeline—client-derived today (the core weakness)

- Server: typed SSE events in `agents/events.py` (`tool_start` `:71`,
  `tool_result` `:95`, `metadata` `:112`, `done` `:120`); emitted by
  `chat_client.py:164-169` (tool_start) and `:244-249` (tool_result, with a
  markdown `summary`).
- Client: `thread-store.ts:232-241` pushes one numbered `Citation`
  (`turn-model.ts:96-102`) **per non-`explore_graph` `tool_start`**, labeled by
  `getSystemLabel` (`tool-labels.ts:72-103`)—that is, today's label is derived
  from the *tool name alone* ("Knowledge Graph", a capitalized system id).
  That is the "from the system agent" problem.
- Render: `injectCitationLinks` (`frontend/src/chat/citations.ts:66-79`)
  rewrites `[N]` to `#cite-*` markdown links; the `<Md/>` anchor override
  (`frontend/src/shared/ui/Md.tsx:39-55`) renders `.cite-badge` +
  `data-cite-target`; the Sources row is `AssistantTurn.tsx:198-217`; clicks
  are delegated (`thread-clicks.ts:98-99`).

### (e) The claim-check `dataset_ref` handle—the designed Tier-3 raw path

`_cap_history_payload` (`agents/system_agent.py:862`) replaces every long
telemetry series with stats + landmarks + a stable handle
`dataset_ref: "tel-<uuid12>"` (`_summarize_vqt_series`,
`agents/system_agent.py:824,847`). The S3-backed resolver that redeems the
handle is **deliberately deferred** (the maintainers' deferred-work list, "S3-backed
`dataset_ref` resolver")—*nothing at the tool contract changes when it
lands; do not "fix" the handle away*. The claim-check summary line the trace
shows today is `tool_result_summary.py:190-211`.

### (f) KG-side provenance—rules_applied / planned_with / usage ledger

- Rule ids fire per turn through `extract_rule_node_ids`
  (`agents/usage_tracking.py:32`, id shapes pinned at `:27-30`).
- `rules_applied` (id + human label) and the additive `planned_with`
  route-seed digest ride the `intelligence_summary` metadata event—one
  builder, `build_intelligence_summary`
  (`agents/query_intelligence.py:208`, caps at `:31-40`), fed by
  `runtime_app.py:2055-2096` (`_build_summary_payload`) and
  `_planned_with_from_seed` (`runtime_app.py:978`); local path
  `chat_client.py:319-332`.
- The day-bucketed artifact-usage ledger (`agents/artifact_usage.py`,
  ADR-0050/0052) records which artifacts actually fired, with an additive
  evidence class.

### (g) Existing drilldown surfaces to reuse

Per-tool thread traces already render call args + result summary
(`ToolTrace`, `turn-model.ts:83-93`, rendered by `<ToolInputBlock/>`); the
done-time analysis panel is structured data (`analysis-panel.tsx`); the
opt-in per-session trace drilldown is `agents/agents_trace_routes.py`
(`/api/admin/agents/traces/sessions[/{id}]`). Tier 2/3 must **extend these
surfaces**, not add a parallel trace stack.

## The data contract—one provenance record per citation

A new additive, bounded, pydantic-validated record, assembled server-side:

```
ProvenanceRecord:
  citation_num:  int                     # binds to the [N] marker
  source_system: str                     # system_id (≤100, as SourceMetadata)
  entity:        {table: str, columns: [str]}   # from physical_schema/projection
  scope:         {filters: [str], window: {start, end} | null}  # plain-language
  query_ref:     str                     # opaque per-turn id; Tier 3 redeems it
  result_shape:  {rows: int | null, truncated: bool, partial_reason: str}
  kg_route:      {rules_applied: [{id, label}], value_mappings: [id]}
  captured_at:   datetime                # queried_at from the trusted capture
  verification:  "verified" | "unverified" | "partial"
  row_exclusions: [{table, label, conditions: [{column, op, values?}],
                    at, source, source_shortened?}]  # additive; only when applied
  row_exclusions_omitted: true           # additive; a rule left off for size
```

`row_exclusions` (additive, present only when non-empty) is the source
owner's row exclusions the cited call's Athena queries ran WITH, each as it
was applied to that query: read with the fact at bake time (when it was set,
from what source) and kept per Athena tool call under its toolUseId
(`system_agent._keep_applied_exclusion`, invocation state
`_adt_applied_row_exclusions`; never in a tool result), moved onto that call's
sidecar row by its own capture only, carried on the system agent's provenance
entry, and merged onto the record. It holds one entry per table read the
enforcement guard PROVED carries its rule (`row_exclusion_read.classify`
`applied`). The guard decides only a statement that scans exactly one table:
a join, set operation or other several-table read is left undecided (never
refused, never disclosed), so it records nothing. A failed call, a read
without the rule and a call without an id keep nothing, so a record never
shows a rule that was not applied.

Who set the rule (`row_exclusion_by`, a sign-in identity) is never read for
the card and never rides the frame; it stays in the steward audit trail, and
the card says "Set by an administrator".

One validator (`provenance.normalize_row_exclusions`) bounds it at the
store's write and at the merge: at most 4 rules; each rule ≤1 KB with its
table, label and conditions WHOLE (a rule that cannot fit whole is left
out); its source is kept whole when the rule fits, else shortened to the
longest prefix that fits with `source_shortened: true`, which the card shows
as an ellipsis and "(source shortened)". A value holding a double quote is
out of shape (the store refuses quotes, and the card shows each value in
double quotes). The merge adds a record's rules only while the frame stays
within its 4 KB cap, so a rule never pushes a citation off the frame.

`row_exclusions_omitted` (additive, present only when true) says a rule the
call's queries ran with was captured but left off this record for size: the
rule did not fit its 1 KB cap, there were more than 4, or the frame had no
room for it. The card then says "A source owner's rule left rows out of this
result; its details didn't fit on this card." The marker is added only when it
fits the frame without removing or changing any citation; otherwise the record
says nothing, as before. A malformed rule and a rejected turn get no marker.

Field sources are all existing: `entity`/`captured_at`/`source_system` from
the trusted capture (§a), `result_shape` from `safe_tool_result_metadata` and
`BoundedQueryAccumulator` fields (§c), `kg_route` from the
`intelligence_summary` inputs (§f), `verification` from the integrity verdict
(§c). `query_ref` is a server-held reference to the executed query text—the
SQL **never** rides the Tier-1/2 payload (see the Tier-3 gate). Caps
mirror the existing bounded models (label ≤160, ≤20 rules, record count ≤
`MAX_EVIDENCE_COUNT`).

## Assembly—server-side, at the answer boundary

The record is assembled **only** in the runtime answer path (the same
boundary that emits `intelligence_summary`, `runtime_app.py:2099` /
`chat_client.py:453`), by joining, per orchestrator tool call:

1. the tool_start/tool_result pair (tool id, system id, timestamps);
2. the trusted-capture observations for that call (§a);
3. the Mode-A `SystemAnswer.evidence` array when the tool result parses as a
   typed envelope (§b)—richer `semantic_field`/`locator` binding;
4. the turn's `rules_applied` intersection (§f).

The client **never derives provenance**—today's client-side
`citations.push` on `tool_start` remains only as the numbering/anchor
mechanism, and the server record binds to it by `citation_num` (same
per-non-graph-tool ordinal, computable on both ends from the event stream).
Digit-unsafe or free-text payload content never enters the record: the
assembly consumes only the already-bounded metadata surfaces listed above.

## Transport—additive on the existing SSE payload

One new key on the existing `metadata` frame at done (sibling of
`intelligence_summary`—same event class, `agents/events.py:112`):

```
event: metadata
data: {"provenance": {"records": [ProvenanceRecord, ...]}}
```

Additive means: absent key ⇒ legacy client renders exactly today's Sources
row (the `useAskStream.ts:120-122` metadata dispatch pattern is the
template). No change to `tool_start`/`tool_result`/`done` semantics, no new
event type, bounded to ≈4 KB like the intelligence summary
(`query_intelligence.py:19-22` precedent).

The payload carries one additional additive key, `graph_queries` (present
only when non-empty): `[{query_ref, tool_name}]` rows for the turn's
**uncited** query-bearing calls—in the default Mode A deployment the bare
`explore_graph` cypher is the only executed query the orchestrator ever
sees, and it is deliberately never cited (D2), so without this key the
turn's only redeemable Tier-3 handle had no client-visible surface (live
gap, 2026-08-04). Refs and tool names only—query text stays
sidecar-only (D6 unchanged).

## Tier-2 card UX—plain language, no SQL

The evidence card opens from the existing `.cite-badge` /
`data-cite-target` click contract (`thread-clicks.ts:98-99`)—a new panel,
not a new click system. Rendering rules:

- **No SQL in Tier 2.** Scope renders as sentences built from structured
  fields ("rows where *status* is one of …, between *start* and *end*"),
  never by echoing query text.
- **Row scope honesty**: `result_shape.truncated` renders the cap explicitly
  ("first N rows of a larger result—result was capped"), reusing the
  `partial_reason` vocabulary (`response_integrity_query.py` `row_limit` /
  `byte_limit` / `cell_limit` / `column_limit`).
- **KG route in one line per rule**: the `rules_applied` human label (already
  formatted by `format_rule_label`) plus a "translated <field> between
  systems" sentence for value mappings.
- **Timestamps** render as "queried at <t>" and, when
  `source_observed_start/end` exist, "data observed <window>".
- **Rows left out**: each `row_exclusions` rule renders as its label and
  table, its conditions in plain words with every value in double quotes
  (`status is one of "draft, void", "final"`, "kind is empty or null"; the
  rule reads a column as `COALESCE(col, '')`, so a `not_in` adds "or is empty
  or null"), and "Set by an administrator on <at>. Source: <source>" (a
  shortened source ends "… (source shortened)"); `row_exclusions_omitted`
  adds the omission line. Shown to every signed-in user (Tier 2, no fetch); a rule the
  card cannot state exactly is left out whole, never shown in part.
- Every string renders as React text nodes (zero-innerHTML policy—the
  frontend invariant in the maintainer notes; markdown only through the shared
  `<Md/>`).

## Tier-3 gate—technical detail, own wave

Tier 3 exposes three things behind an explicit "technical detail" expander:

1. **Exact executed query text**—redeemed server-side by `query_ref` from
   the trusted capture (§a). Never embedded in the streamed answer payload;
   fetched on demand from a session-scoped endpoint under the existing
   trace-drilldown route family (§g), subject to the same auth as the trace
   routes.
2. **Raw result access through `dataset_ref`**—the claim-check redemption path.
   Shipped as its **own wave** (Wave 4) respecting the deferred contract
   (the maintainers' deferred-work list): the handle shape (`tel-<uuid12>`) and the tool
   contract did not change; the wave added the S3-backed store
   (`agents/dataset_store.py`) + the admin-gated resolver endpoint the handle
   was designed for, and the Tier-3 expander's "View full data" affordance
   fetches on demand. A record with no `dataset_refs` renders exactly the
   pre-Wave-4 expander (and the trace summary keeps its "full series
   withheld" wording, `tool_result_summary.py:211`).
3. **KG artifact ids + provenance stamps**—the raw `rules_applied` ids
   (`subj-*` / `vm-learned-*`, `usage_tracking.py:27-30`) and the artifact
   ledger's evidence class (§f), linking to the vocabulary admin surface.

## Trust boundaries and redaction

- **Evidence text is untrusted data.** Labels, filter values, and any string
  originating from tool results or steward text render as React text nodes
  only—the zero-innerHTML sink policy applies unchanged; business context
  never carries evidence weight (ADR-0054).
- **Secrets/PII never enter provenance records.** The record is built
  exclusively from the bounded metadata surfaces (§a, §c) that already
  enforce the allowlist posture of `tool_result_capture.py` ("never returns
  arbitrary payload content", `agents/tool_result_capture.py:3-8`). Raw
  values, credentials, and row content are structurally absent, not filtered.
- **Digit-masking in logs.** Any logging of provenance assembly follows the
  existing convention: error text and query fragments are digit-masked
  before logging (`_redact_digits` usage, `chat_client.py` error frame
  handling) because results can quote row values—the claim-check policy
  covers logs too.
- **Executed query text is Tier-3-only** and server-held: it can embed
  literal filter values, so it never rides the broadcast answer payload and
  is only redeemable through the authenticated drilldown route.

## Honest absence—"unverified" is a rendered state

A claim whose citation has **no** captured evidence (no trusted observation,
no typed evidence binding) gets `verification: "unverified"`—the chip and
card render it, they do not hide it. The value is derived from the existing
response-integrity verdict marker (§c)—the `report-only` default still
runs the capture hooks in shadow, so the signal exists in every mode. This
mirrors the honest-data rule (ADR-0045: absence is a distinct rendered
status, never silent) and never blocks the answer: enforcement stays opt-in
per ADR-0046/0047, and enforce-path behavior changes remain gated on
`RESPONSE_INTEGRITY_MODE`.

**Cloud Mode-A emit-site reality (verified live, 2026-08-04).** On the
default cloud deployment (`SYSTEM_QUERY_MODE=agent`, report-only), the
orchestrator runtime's emit site structurally cannot enrich the cited
records, and the sparse record is the honest one:

- `entity.table`/`entity.columns` are empty and `verification` is
  `unverified`: the cited system tools receive **natural-language
  sub-questions** and return **markdown answers**—the SQL, physical
  schema, and typed evidence exist only inside the *system agent's*
  AgentCore runtime (the trusted-capture seam, §a), on the far side of the
  gateway. D6 explicitly forbids widening the `SystemAnswer` envelope or
  the tool contract to carry them back (token-path contamination + pinned
  envelope model). Typed evidence bindings (`evidence_by_tool`) exist only
  on the enforce path (ADR-0046/0047 opt-in).
- `result_shape.rows` is `null` for those calls because a markdown answer
  has no bounded countable shape (`safe_tool_result_metadata` ⇒
  `app_status: "unknown"`); calls whose results are structured JSON (graph,
  schema, Mode-B SQL) do populate counts.
- `kg_route.rules_applied` mirrors the turn's `intelligence_summary`
  extraction (one shared source, `_format_rules_applied`)—empty when the
  turn's results carried no applied-rule node ids, not a wiring gap.
- The turn's **executed queries the orchestrator did see** (bare
  `explore_graph` cypher; Mode-B SQL where enabled) are stored in the
  Wave-3 sidecar and surfaced as redeemable refs through the additive
  `graph_queries` key / the record's own `query_ref`; a cited Mode-A call's
  `query_ref` redeems the D5 route's structured 404 and Tier 3 renders
  "executed query not captured for this call"—by contract, not by bug.

## Wave plan

Each wave is independently shippable; the feature degrades gracefully at
every boundary (absent key ⇒ today's behavior).

### Wave 1—server-side ProvenanceRecord assembly + additive transport

Assemble records at the runtime answer boundary and emit the additive
`metadata.provenance` frame. No frontend change ships in this wave (legacy
clients ignore the key by construction).

- *Tests:* unit tests for the assembler (given a synthetic event stream +
  invocation state, assert record shape, caps, and `citation_num` alignment
  with the non-graph-tool ordinal); a single-source test in the style of
  `tests/unit/test_intelligence_summary_single_source.py` pinning ONE
  builder; absence tests (no capture ⇒ `verification: "unverified"`, never a
  fabricated record).
- *Pinned seams:* metadata-frame shape tests
  (`test_runtime_entrypoint_integrity_marker.py`—exactly one integrity
  marker, unchanged); `test_query_intelligence.py` payload-shape pins;
  enforce-path tests must keep monkeypatching `RESPONSE_INTEGRITY_MODE`
  (invariant). Unit lane offline, network-blocked.

### Wave 2—Tier-1 chips + Tier-2 evidence card (frontend)

Consume `metadata.provenance`; upgrade chip labels from `getSystemLabel` to
`source_system + entity.table + window` when a record is present (fallback:
today's label); add the evidence card panel on the existing
`data-cite-target` click path.

- *Tests:* vitest component tests in the style of
  `frontend/src/test/chat-thread.test.tsx` (chip label with/without record;
  card renders plain-language scope; `unverified` state renders); the
  sink-policy lane stays green (no innerHTML—grep-gated); `npx tsc
  --noEmit && npx vitest run && npm run build`.
- *Pinned seams:* `md-component.test.tsx` / `answer-sections.test.tsx`
  citation-badge contract (`.cite-badge` + `data-cite-target` unchanged);
  `chat-thread.test.tsx` Sources-row contract.

### Wave 3—Tier-3 technical detail (query text + KG artifact ids)

Session-scoped redemption endpoint for `query_ref` under the trace-route
family; Tier-3 expander in the card rendering executed query, KG artifact
ids, and provenance stamps.

- *Tests:* route tests (auth required; unknown/expired `query_ref` ⇒
  structured 404, never a payload echo); digit-masked logging asserted;
  component test for the expander.
- *Pinned seams:* trace-route auth posture (`agents_trace_routes.py`
  patterns); the claim-check policy (raw telemetry never in the LLM token
  path—untouched, this is a client-fetch path).

### Wave 4—`dataset_ref` resolver (claim-check redemption)

The deferred S3-backed store + resolver, shipped exactly as the contract in
the maintainers' deferred-work list anticipates: capped-history writes persist the
withheld series keyed by the existing `tel-<uuid12>` handle; a resolver
endpoint redeems it for Tier-3 raw access. **The handle shape and the tool
contract do not change.**

- *Tests:* unit tests for store/resolver (handle round-trip, TTL expiry,
  cap-failure path still returns the structured error—`system_agent.py:889-899`
  behavior unchanged); deployed-env check for the
  Amazon S3 path (graph/telemetry live checks are deployed-env by convention).
- *Pinned seams:* `tool_result_summary.py` claim-check summary tests; the
  chart-delivery and claim-check invariants in
  the i3X telemetry notes (stats-only to the model—the resolver
  serves the *client*, never the LLM token path).

## Design references

ADR-0046/0047 (typed evidence gate, opt-in modes), ADR-0045 (honest-data
statuses), ADR-0050/0052 (usage ledger, replace-not-accrete), ADR-0053
(measured-only cost; drift never mutates the graph), ADR-0054 (untrusted
steward text, word-boundary classifiers), the maintainers' deferred-work list
(`dataset_ref` contract), `docs/metric-layer.md` (lineage precedent: every
value carries the lineage that produced it—this design extends the same
promise to conversational answers).
