<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0078: A question that names a DEFINED metric is answered from the metric layer — the turn carries the computed result (or the honest withhold) BEFORE the model runs, and nothing is re-derived from raw columns

## Status

**Proposed — 2026-09-27.** Amends [ADR-0066](0066-graph-backed-metric-definitions.md)
(D6/D7: a per-turn *result* of a resolved metric is delivery data, not a
definition mirror) and closes the conversational gap it left open. Builds on
[ADR-0015](0015-deterministic-metric-resolver.md) / [ADR-0016](0016-derived-metric-resolution.md)
(the ONE deterministic resolver — reused, never re-implemented),
[ADR-0045](0045-multi-site-metric-fan-out.md) (honest-data statuses),
[ADR-0049](0049-user-identity-propagation-app-signed-claim.md) (reserved payload-key discipline),
[ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(telemetry proves delivery, never obedience), [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md)
(measured-only cost), [ADR-0080](0080-metric-definition-dry-run-preview.md)
(request-scoped execution trace), [ADR-0075](0075-predicate-metric-definitions.md) /
[ADR-0076](0076-binding-disambiguation-and-dotted-columns.md) (the withhold
statuses this notice relays verbatim) and [ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — every fixture here is `widget_*`-shaped).

## Context

### The defect (measured live 2026-09-27, customer golden lane)

A steward accepted a DERIVED metric definition through the queue. The metric
layer resolves it honestly: `metric_resolver.resolve_metric` raises
`MetricResolverError("derived metric unavailable: input '<sub-metric>'
unresolvable: no onboarded system maps concept '<concept>' yet")` — the text
is built in `_eval_metric_ref` (`agents/metric_resolver.py`) from the
`CoverageGapError` `_resolve_authority` raises when no `MAPS_TO` edge exists.
The dashboard renders exactly that as status `unavailable`
(`agents/dashboard.py::unavailable_card_entry`).

Yet when a user ASKS about that metric in chat, the orchestrator never
consults the metric layer. It delegates to a system agent, composes SQL,
invents a denominator from raw columns and presents a number ("383.2 → 329.8,
a 13.9 % reduction"). Three runs produced three framings. ADR-0066 promised
that "metric definitions are structured graph data resolved deterministically,
NEVER prompt text" — and that promise holds on every surface EXCEPT the one
users actually talk to.

### Why the existing honesty machinery cannot fire

The conversational path uses definitions in exactly two ways today:

1. **The deterministic prompt-composition pre-check**
   (`agents/metric_term_resolution.py`). `unresolved_metric_terms` extracts
   metric-shaped candidates and keeps only those that do NOT resolve against
   `known_metric_vocabulary()` (code metric ids/labels, concept vocabulary,
   and graph-definition id/label/synonyms from the `metric_definition_store`
   TTL snapshot). Only an UNRESOLVED term produces the
   `[UNRESOLVED METRIC TERM — DETERMINISTIC PRE-CHECK]` notice
   (`NOTICE_HEADER`, `render_metric_term_gap_notice`). The runtime composes it
   in `runtime_app._compose_seeded_turn` between the route seed and the
   `\n\n---\n\n` separator.
2. **The reactive prompt rule** `orchestrator._METRIC_TERM_MISS_RULE` (plus
   the ADR-0066 D5 capture paragraph, byte-pinned by
   `tests/unit/test_metric_term_miss.py`) which tells the model what to do when
   a vocabulary lookup MISSES.

A DEFINED metric is therefore the WORST case: the term resolves (the
pre-check goes silent by design — `test_prelude_notice_absent_for_a_resolved_term`),
nothing "missed" (the reactive rule has nothing to fire on), and the model is
free to compute a proxy. Defining a metric currently REMOVES the only
warning the model would have received.

### The constraint that shapes the design: WHERE the metric layer can run

The metric layer executes source SQL: `_execute_row` goes through
`db.query_pg` (needs `POSTGRES_HOST`/`POSTGRES_PASSWORD`), the Athena
connector with connection-secret credentials (`_execute_athena`,
`_full_connection_config`) or SiteWise. Those live in the ECS app process —
the dashboard, `POST /api/metrics/resolve`, and the ADR-0080 preview all run
there. The orchestrator AgentCore runtime does NOT have them: its env contract
`runtime_env_contract._orchestrator_env` carries `GRAPH_IDENTIFIER`, gateway,
model and identity keys only — no connection env, no connection secrets, no
Athena workgroup. The prelude that composes the existing notice
(`runtime_app._graph_first_prelude`) runs inside that runtime. So the
metric-layer result cannot be computed where today's notice is composed; it
must be computed app-side and DELIVERED to the runtime. The app already has a
reserved-payload-key channel for exactly this: `tryItCandidate`
(`runtime_app.TRYIT_CANDIDATE_KEY`, popped unconditionally by
`_pop_tryit_candidate`; merged app-side by
`chat_client._compose_runtime_payload`).

The same fact decides the tool question: an in-runtime `resolve_metric` tool
would have no database to execute against (D8).

### Existing pieces this ADR reuses (never re-implements)

- `metric_resolver.resolve_metric(metric_id, *, authority)` → `ResolvedMetric`
  (`value`, `unit`, `label`, `system_id`, `lineage`, `sample_count`,
  `status`, `detail`) — the ONE computation home (ADR-0015/0016/0066).
- The honest-data register: `dashboard.card_entry_from_resolved` maps
  `invariant_violation` / `unresolved_predicate` / `ambiguous_binding` /
  `unresolved_input` to withheld entries and `value None`/`sample_count == 0`
  to `no-data`; `unavailable_card_entry` carries a resolver error.
- `metric_execution_trace.execution_trace(suffix)` +
  `ExecutionTrace.cost()`: the MEASURED query count per system (ADR-0080 D2)
  — `_note_execution` records every backend.
- `metric_dimension.WINDOW_VOCAB` (`1h`, `24h`, `7d`, `30d`) and the lineage
  window-honesty fields (`Lineage.window_requested` / `window`).
- `metric_term_resolution`'s normalization and word-boundary matching
  (`normalize_term`, `_add_vocabulary_forms`, `_resolves`,
  `_in_metric_context`) and its `NEVER_SUBSTITUTE_SENTENCE`.
- `agents/offload.py` (`offload` / `offload_resolver`) for any async route
  that must not run this inline.

## Decision

### D1 — The hook is a PAIR: app-side deterministic resolution, runtime-side composition; matching extends `metric_term_resolution`, resolution lives in a NEW module

**Matching** (which defined metrics a question names) EXTENDS
`agents/metric_term_resolution.py` — it is the ONE vocabulary home
(normalize/collapse/initialism forms, the metric-request context gate, the
store-snapshot vocabulary). A second matcher would drift. New pure function:
`defined_metric_terms(question) -> list[DefinedMetricMatch]` (D2 matching
rules).

**Resolution + notice** (per-turn I/O: SQL executes) lives in a NEW module
`agents/metric_turn_notice.py`. `metric_term_resolution`'s module contract is
"NO per-turn graph round trip … the vocabulary is the seed already built plus
TTL snapshots"; executing source SQL per turn there would silently break that
stated bound. The new module owns: the per-turn result computation
(`results_for_turn`), the TTL cache, the timebox, the structured result shape,
the sanitizer the runtime applies on receipt (`sanitize_result`), and the pure
renderer (`render_metric_layer_notice`).

**Where it runs.** App-side, in `chat_client.AgentCoreChatClient._stream_impl`
immediately before `_compose_runtime_payload`: the ONE place every cloud
turn's payload is built — web SSE (`server._start_stream_worker`, a daemon
thread), the server-side eval runner (`eval_runs._default_stream`, thread),
try-it, feedback exploration, the async-ask job, `ask_sync`. Computing in
`server.ask` alone would leave the Evals tab — the lane that MEASURES this fix
— without the notice. The results ride the payload under the RESERVED key
`metricLayerResults` as STRUCTURED data (a list of result dicts, D2), never as
prompt text: the runtime renders the text itself through the shared pure
renderer, so no caller can smuggle prose into the model's token path (the
`tryItCandidate` / `_adt_identity` posture, ADR-0049).

**Runtime side.** Both entrypoints pop the key UNCONDITIONALLY
(`_pop_metric_layer_results(payload)`, mirroring `_pop_tryit_candidate`) and
forward it to `_graph_first_prelude(prompt, candidate, metric_results=…)` →
`_compose_seeded_turn(seed, prompt, metric_results=None)`, which appends the
rendered notice AFTER the existing gap notice and BEFORE the `\n\n---\n\n`
separator, so `memory_labels.strip_turn_preambles` still recovers the user's
question byte-for-byte. Absent key or empty list ⇒ byte-identical composition
(the pinned ordinary-turn contract). The runtime never resolves anything.

**Event-loop rule.** `_stream_impl` is a sync generator already running in a
producer thread for every caller except `server.ask_sync`, which iterated the
cloud client INLINE in an `async def` (pre-existing debt the AST tripwire could
not see because `stream` was not in `_KNOWN_SYNC_CALLS`). This ADR hoists that
branch into a module-level sync fn and makes the handler `await offload(...)`
(the ONE offload home) BEFORE the hook lands, and adds
`resolve_metric` and `results_for_turn` to the tripwire's detection set
(`tests/unit/test_event_loop_offload.py::_KNOWN_SYNC_CALLS` — the detection
set may grow; `_INLINE_SYNC_ALLOWLIST` stays empty). `stream` itself is NOT
added: Starlette's `request.stream()` is legitimately async inside two upload
routes, so `ask_sync` is pinned by its own AST test instead
(`tests/unit/test_ask_sync_offload.py`).

**Local mode** (`LocalChatClient`, and `/api/ask/sync`'s local legacy branch)
runs no runtime prelude, so it cannot consume a reserved key. It computes the
SAME structured results in its existing producer thread
(`chat_client._local_metric_layer_prompt` → `metric_turn_notice.local_turn_prompt`;
the sync branch through `offload(_ask_sync_local, …)`) and renders them with the
ONE renderer as a seed-independent preamble before the documented
`\n\n---\n\n` separator; the raw question stays what memory and provenance
see (the hook's intent is computed before composition).

**Seed-less turns.** When the route seed raises or is empty, the runtime still
composes the already-computed notice (`runtime_app._compose_unseeded_turn`):
`<notice>\n\n---\n\n<prompt>`; with no results the pre-ADR degrade
(`prompt` unchanged) is byte-identical. `memory_labels.strip_turn_preambles`
peels a turn beginning with `[METRIC LAYER` exactly as it peels `[ROUTE SEED`,
so a graph outage can never reopen the proxy path.

### D2 — The notice contract: computed, bounded, structured in transit, four outcomes, no LLM anywhere

**Matching (pure, `metric_term_resolution.defined_metric_terms`).** Build an
index over every served metric — `get_metric_layer().list_metrics()` (code +
graph, ADR-0066 D2) contributing normalized/collapsed `id` and `label` plus the
label's initialism, and graph docs from
`metric_definition_store.load_definitions()` contributing `synonyms` for ids
the composed layer serves as GRAPH-defined (a code-shadowed doc contributes
nothing — the code metric wins, ADR-0066). A store failure degrades to the
layer's forms with a log line — never to silence, because silence IS the
defect. Scan the NORMALIZED question with the SAME word-boundary idiom
`_resolves` uses (`(?<![a-z0-9])form(?![a-z0-9])`) — as ONE pass over word
starts with ONE compiled alternation, longest form first
(`metric_term_scan._scan_defined_forms`: at most one span per word), over a
question bounded by `MAX_MATCH_INPUT_CHARS = 8000`. A longer question raises
`MatcherInputTooLong` and the turn carries a FORBIDDING `input_overflow`
block ("the defined metrics it may name were NOT checked or resolved this
turn. Give NO number …") — bounded work, never a silent miss. Admission is
deterministic:

- a matched form of ≥ 2 words (multiword label, id or synonym) is admitted
  outright;
- a single-word form or an initialism is admitted ONLY under the existing
  metric-request gate `_in_metric_context` (a measurement word within two
  words, or the "what is/are the …" shape), an anchored IMPERATIVE request
  (`^[please|can you …] <verb> [me|us] [about] [the|our|…]{0,3} <form>`, verbs
  from the generic `IMPERATIVE_REQUEST_VERBS`: show, report, compare, give,
  list, tell, find, get, …), or when LISTED right after an admitted form
  ("compare <a> and <b>", "report <a>, <b>") — code labels that are ordinary
  English words must not fire on a casual mention. The imperative and list
  gates are defined-metric admission ONLY: the unresolved pre-check keeps its
  own `_in_metric_context` (an unknown acronym after "find"/"list" is rarely a
  metric, and claiming it undefined would be a new false positive);
- forms shorter than 3 characters never match;
- overlapping spans keep the LONGEST match (a shorter match wholly inside it
  is dropped); a span matching 2+ metric ids is an `ambiguous_term`;
- first-appearance order, capped at `MAX_METRIC_TERMS = 3`;
- EVERY turn kind is matched — a briefing included. The session id never
  suppresses composition; only the usage-ledger write (D4) skips synthetic
  and briefing sessions.

**Resolution (`metric_turn_notice.results_for_turn(question, session_id) -> list[dict]`).**
Each admitted metric id resolves through `metric_resolver.resolve_metric`
(default graph authority — `authority=None`) inside
`execution_trace(TURN_SUFFIX)` where `TURN_SUFFIX = ":metric-turn"` (Athena
executions carry `<system>:metric-turn` attribution, separable from user and
preview traffic). The outcome is classified EXACTLY as the dashboard
classifies it — never a new register:

| outcome | when | carried fields |
|---|---|---|
| `computed` | resolve returns `value` not None, `sample_count != 0`, no withhold status | `value` (floats rendered `.6g`), `unit`, `window_requested`, `window_applied`, `kind`, `source` (`code`/`graph`), `system_id`, `shape` (`direct`/`formula`), `lineage` (direct: `system.table.column (aggregation)`; derived-with-`prefer_stored_hit`: the stored input's direct lineage; derived formula: `op` + input metric ids), `systems` + `query_count` from the trace, `sample_count` |
| `withheld` | `status` ∈ {`no-data`, `invariant_violation`, `unresolved_predicate`, `ambiguous_binding`, `unresolved_input`} returned normally, OR `resolve_metric` raises `MetricResolverError`/`NotImplementedError`/`KeyError` (dashboard's `unavailable`), OR a DERIVED metric that declares a window computed a value (`unavailable`, `DERIVED_WINDOW_DETAIL`: the resolver applies no window to a formula, so the value would be reported for a period it does not cover) | `status` (`unavailable` for the raise) and `detail` VERBATIM — `ResolvedMetric.detail` or `str(exc)` — bounded to `MAX_DETAIL_CHARS = 240`, single line, printable (a detail carrying control characters is replaced by the exception class name) |
| `result_unavailable` | timebox elapsed, or a non-resolver exception (connection/botocore/database driver) | `reason` = `"timebox"` or the exception CLASS NAME only — never its message (it can quote hosts or row values; claim-check posture) |
| `ambiguous_term` | one span matches 2+ metric ids | `candidates` (sorted ids) |

Every result dict also carries `outcome`, `term` (the question's normalized
surface form, ≤ 60 chars), `metric_id`, `label`.

**Rendering (pure, `metric_turn_notice.render_metric_layer_notice(results) -> str`).**
Header `METRIC_LAYER_HEADER = "[METRIC LAYER — DETERMINISTIC RESULT]"`, then
one block per result, then the REQUIRED RESPONSE section, then the shared
`metric_term_resolution.NEVER_SUBSTITUTE_SENTENCE` (imported — ONE spelling)
and the pinned scope sentence
`WINDOW_SCOPE_SENTENCE = "The metric layer provides exactly the value above for
its declared window token; arbitrary date ranges and per-dimension breakdowns
are not available from it this turn."`. Block shapes (exact, tested):

```
- '<term>' → metric '<id>' ("<label>", <kind>, <source>-defined): COMPUTED.
  value: <v> <unit> | window: <window line> | scope: deployment-wide scalar; authority system '<sid>' | lineage: <lineage> | queries: <n> across <systems>[ | samples: <k>]
  Scoped/other-window figures: PERMITTED only by applying this exact lineage through a system agent, labelled as computed outside the metric layer at that scope.   ← direct or prefer_stored-hit only
  Scoped/other-window figures: NOT AVAILABLE — the formula is applied only by the metric layer; do not compute one.   ← derived formula
- '<term>' → metric '<id>' ("<label>"): WITHHELD — status '<status>'.
  reason: <detail verbatim>
- '<term>' → metric '<id>' ("<label>"): METRIC-LAYER RESULT UNAVAILABLE THIS TURN (<reason>).
- '<term>' matches more than one defined metric: '<id1>', '<id2>'. Not resolved — ask which is meant.
```

Window lines (direct shape): `<token> (declared)` when the declared token was
applied; `<token> declared, applied as lifetime (no event-time mapping on the
source)` for the honest lifetime fallback; `lifetime (no window declared)` when
the metric declares none. A derived FORMULA has no window of its own (the
resolver applies a window only on DIRECT reads — `_requested_window`), so the
result carries `input_windows` — one `{input, requested, applied}` per EXECUTED
direct read (`metric_turn_lineage.input_windows`, capped at 6, `truncated`
flagged) — and the line names the actual token(s): `<token> on every input
(declared; derived formula)`, `lifetime on every input (derived formula)`, or
`mixed across inputs (derived formula): <input>=<token|lifetime>, …`.

The closing scope sentence is CONDITIONAL on the computed results' window
classes (`metric_turn_render.scope_sentence`): all declared-and-applied ⇒
`WINDOW_SCOPE_SENTENCE`; all lifetime ⇒ `LIFETIME_SCOPE_SENTENCE`; mixed (or a
truncated input list) ⇒ `MIXED_SCOPE_SENTENCE`; no computed value ⇒
`NO_VALUE_SCOPE_SENTENCE`. It never claims a declared token a value does not
have.

REQUIRED RESPONSE text (pinned fragments): COMPUTED ⇒ report THAT value, unit,
window and scope as the metric's value, never recompute it; a narrower scope or
other period follows the block's own PERMITTED/NOT AVAILABLE line. WITHHELD or
UNAVAILABLE ⇒ state the status and reason verbatim and give NO number for that
metric — do not compute, estimate or illustrate a substitute from raw columns,
a related ratio or a neighbouring metric, not even labelled approximate or
"for illustration"; when the reason names an unmapped concept, say the gap is an
onboarding/mapping gap. AMBIGUOUS ⇒ ask which id is meant. False-positive
hatch, narrowed: if the question's use of the term is clearly not the metric,
disregard the block for that term — but never compute a quantity by that name
outside the metric layer.

**Bounds.** Whole notice ≤ `MAX_NOTICE_CHARS = 3200`: blocks are dropped from
the tail (never truncated mid-block) until it fits; the header, the REQUIRED
RESPONSE section and both pinned sentences always survive. Every field is
sanitized before rendering (single line, printable, capped) — on the app side
when the result is built AND on the runtime side on receipt. Nothing is
LLM-judged: matching, classification and rendering are computed.

### D3 — Resolution is bounded per turn: cap, TTL cache, timebox, worker pool, fail-OPEN to a notice that still forbids substitution

- **Cap, never a silent drop:** `MAX_METRIC_TERMS = 3` resolves per turn. The
  matcher scans to `MAX_METRIC_TERMS + 1` (`defined_metric_terms(q, limit=…)`)
  only to DETECT overflow; a question naming more gets a trailing `overflow`
  result that renders "'<4th term>' and any further defined metric are NOT
  resolved by the metric layer this turn. Give NO number for any defined metric
  not listed above — ask the user to narrow the request". The marker is part of
  the notice's always-kept tail, and the runtime receipt check accepts exactly
  one TRAILING marker (a cap+1 list without it is hostile ⇒ the generic
  forbidding `invalid` marker, below).
- **The reserved key is HOOK-OWNED and fails CLOSED on receipt.** App side:
  `extra_payload_with_results` and the chat-client wrapper strip any
  caller-supplied `metricLayerResults` FIRST on every path (matched,
  unmatched, disabled, hook fault) — only `results_for_turn` can put a result
  on the wire. Runtime side: an ABSENT key is the ordinary byte-identical
  turn; a PRESENT key proves app-side matching named a defined metric, so a
  non-list / empty / oversize value, a dropped malformed item, or a sanitizer
  fault yields the generic runtime-only `invalid` result (never accepted from
  transport), rendered in the always-kept tail as "results … did not arrive
  intact: METRIC-LAYER RESULT UNAVAILABLE THIS TURN … Give NO number". A
  renderer fault composes the static `FALLBACK_NOTICE` (header + that line +
  REQUIRED RESPONSE + never-substitute), via `render_or_fallback` — the SAME
  helper local mode uses.
- **Cache:** module-level `dict[(metric_id, authority_context)] → (expires, result)`
  under a lock; `authority_context` is `""` in v1 (default graph authority) and
  is the extension point for site-pinned resolution (D9). TTL
  `METRIC_TURN_CACHE_TTL_SECONDS` default `180` — the dashboard's baseline
  poll — parsed like `metric_definition_store._ttl_seconds` (FINITE, positive,
  fail-loud on `inf`/`nan`/malformed; a non-finite TTL is the ABSENCE of a
  bound). `computed` and `withheld` are cached for the TTL;
  `result_unavailable` for `FAILURE_COOLDOWN_SECONDS = 30` only, so a transient
  outage is neither hammered per turn nor served stale for minutes.
  `metric_layer.invalidate_metric_layer()` additionally calls
  `metric_turn_notice.invalidate()` (lazy import, fail-open) so a steward
  accept is visible on the next turn in-process. `invalidate()` also bumps a
  GENERATION captured at submission; a store whose generation is stale is
  refused, so a resolve that began before the accept can never resurrect its
  pre-accept result for another TTL; other processes converge
  within the TTL — the ADR-0066 D4 staleness posture, restated for values.
  Authority/mapping mutations invalidate too, on every exit path (success,
  readback mismatch, not found, raise): every resolver-affecting graph writer
  carries the ONE decorator
  `metric_turn_invalidation.invalidates_metric_turn_results` (invalidate in a
  `finally`) — `metric_authority.set_authority` (the steward AuthorityRule
  writer), `server._set_rule_lifecycle` (AuthorityRule soft
  deactivate/reactivate), `self_learning._repoint_maps_to` (accepted
  `concept_mapping`, also the provision re-apply),
  `self_learning._apply_field_concept_mint`, `onboard.onboard_remap` (steward
  remap), the WHOLE `onboard._do_provision` (a partial provision may already
  have written `MAPS_TO` edges before a later phase raised) and the offboard
  reset's graph delete (`_reset_delete_graph_artifacts_invalidating`). The
  writer set is pinned by a test; a new `MAPS_TO`/`AuthorityRule` writer must
  carry the decorator. The key's
  `authority_context` stays `""`: the default authority is re-read on the next
  resolve after any such write.
- **Timebox:** `METRIC_TURN_TIMEBOX_SECONDS` default `12` (same finite-positive
  parsing). Resolves are submitted to the module's own
  `ThreadPoolExecutor(max_workers=METRIC_TURN_MAX_WORKERS = 2,
  thread_name_prefix="adt-metric-turn")`; `concurrent.futures.wait` with the
  shared remaining budget; unfinished futures yield `result_unavailable
  (timebox)`. A straggler thread finishes on its own, bounded by the resolver's
  existing per-system query timeouts; the pool bounds convoy pressure (a
  saturated pool means queued futures time out honestly, never unbounded
  threads).
- **Fail-OPEN to the FORBIDDING notice, not to silence.** Any failure in
  resolution yields `result_unavailable`, which renders a block that names the
  metric and forbids substitution. This deliberately differs from the gap
  pre-check's fail-open-to-empty: that notice's failure mode was a FALSE
  ABSENCE claim (ADR-0066 D4), so silence was the honest degrade; here the
  failure mode is a FABRICATED VALUE, so the honest degrade is "unknown this
  turn — do not compute". Only the MATCHING step fails open to no notice (an
  exception in extraction/indexing means we do not know that a defined metric
  was named at all). Once a match exists, every later failure — including a
  malformed `METRIC_TURN_*` bound (reason `invalid-config`, logged at ERROR) and
  any pool/plumbing fault — synthesizes a `result_unavailable` entry per match.
  Malformed bounds are additionally rejected at BOOT
  (`metric_turn_notice.validate_config()`, the first statement of
  `server.lifespan`), so misconfiguration fails loud instead of silently
  disabling the protection.
- **Kill switch:** `METRIC_TURN_NOTICE=off` disables the hook (default on).
  Operational, because the hook adds per-turn source-database load; nothing
  else is configurable.
- **Event loop:** never inline in an async handler (D1).

### D4 — Telemetry proves composition, never obedience (ADR-0052 / ADR-0066 D6)

The runtime bumps `artifact_usage` for each GRAPH-defined metric whose block
was composed into the turn, evidence `"metric-turn-composed"` — a delivery
class alongside `metric-resolve` / `term-precheck-resolved` (documented in
`artifact_usage.py`'s class list); `resolve_metric`'s own D6 `metric-resolve`
bump keeps firing app-side for a successful graph resolve. The bump fires only
when the header is actually present in the composed turn (composition, not
receipt), and synthetic sessions (`runtime_app._SYNTHETIC_SESSION_PREFIXES`)
and briefings write nothing. No evidence string, log or doc claims the model
"followed" the result; the pinned forbidden-word check in
`test_metric_definition_usage.py` extends to the new class. A structured INFO
log per turn carries metric ids, outcomes, elapsed ms and query counts — never
values, details or question text.

### D5 — The prompt rule: a DEFINED metric with a metric-layer result is reported FROM that result; withheld ⇒ no number, no substitute, not even "for illustration"

New constant `orchestrator._METRIC_LAYER_RESULT_RULE`:

```
A DEFINED METRIC IS ANSWERED FROM THE METRIC LAYER — NEVER RE-DERIVED BY YOU:
When the turn carries a [METRIC LAYER — DETERMINISTIC RESULT] block, that block is the
authoritative resolution of every metric it names. COMPUTED ⇒ report THAT value, its
unit, its window token and its scope as the metric's value; never recompute it from
raw columns or another metric, and follow the block's own line on whether a scoped or
other-period figure is permitted. WITHHELD or UNAVAILABLE ⇒ state the status and the
reason verbatim and give NO number for that metric: do not compute, estimate, or
illustrate a substitute from raw columns, a related ratio, or a neighbouring metric —
not even labelled as approximate or "for illustration". A term that RESOLVES is the
worst case for a proxy, because nothing else warned you: the block is the warning.
```

The scoped-figure policy is deliberately split by SHAPE and stated in the
block, not left to the model: a DIRECT metric (or a derived metric whose
`prefer_stored` input was hit) has a fully stated lineage — one system, one
table.column, one aggregation — so a system agent CAN apply exactly it at a
narrower scope with disclosure, and the documented site-scoped metric
questions keep working; a DERIVED formula is applied only by the metric layer
(the defect was precisely an invented denominator), so no scoped figure exists
and the block says so. Windows other than the declared token are never
available from the metric layer (D2 pinned sentence).

### D6 — Interaction with `_METRIC_TERM_MISS_RULE`: APPEND at the anchor, the pinned bytes untouched

`tests/unit/test_metric_term_miss.py::test_pre_existing_rule_text_is_byte_identical_and_a_prefix`
asserts EXACT equality `_METRIC_TERM_MISS_RULE == BEFORE + _METRIC_TERM_CAPTURE_RULE`,
so the new rule is NOT concatenated into `_METRIC_TERM_MISS_RULE`. Instead
`_apply_answer_honesty_prompt_rules` changes its first replacement to
`_METRIC_TERM_MISS_ANCHOR + _METRIC_TERM_MISS_RULE + _METRIC_LAYER_RESULT_RULE`:
the pre-existing constants stay byte-identical, their tests pass unmodified,
and the rendered prompt carries the new paragraph immediately after the
capture paragraph in the SAME shared section — inside the Quick workflow slice,
so both mode prompts and both Quick prompts carry it (Quick turns reach the
runtime through the same payload, so they see the block too). The new rule
never names the fenced identifier-miss marker (the Mode-B prompt is pinned
marker-free). The two rules compose: MISS governs a term with no definition;
this rule governs a term WITH one. The capture paragraph is unchanged — a
pending captured definition still has no authority, and the metric layer never
serves a pending row (ADR-0066 D3), so no block can ever carry one.

### D7 — ADR-0066 D7 amendment: a per-turn RESULT with its lineage is delivery data, not a definition in prompt text

D7 forbade definitions in `query_guidance`, route seeds, or any accumulating
prompt surface — the guidance budget stays out of their path and resolution is
never obedience. This ADR keeps both: the block carries the metric layer's
RESULT (value/status) and the LINEAGE it executed (already exposed by
`/api/metrics/resolve` and the dashboard lineage view), never the definition
document, never a formula for the model to evaluate; it is per-turn and
bounded (≤ 3 metrics, ≤ 3200 chars), consumes zero guidance characters, and
the value is computed before the model runs. The one place a formula's shape
appears — `op` + input ids for a derived result — is paired with the
NOT-AVAILABLE line so it cannot be an invitation to compute.

### D8 — No `resolve_metric` agent tool in v1 — the runtime has nothing to execute against

An in-runtime tool would need the source connections the orchestrator runtime
deliberately lacks (`_orchestrator_env`; graph reads only). Granting them
widens the runtime's least-privilege posture (IAM for every connection secret,
Aurora security-group reach, Athena/Lake Formation grants) for a follow-up
affordance. The honest v1 is the notice. Deferred design, recorded so it is
not improvised: a `metric-layer` gateway MCP target served by the app's
existing `POST /api/metrics/resolve` (the QueryBridge target is the
precedent), whose tool `resolve_metric(metric_id, window)` accepts ONLY a
served metric id and a `WINDOW_VOCAB` token
(`metric_dimension._validate_window`), returns the SAME structured result dict
of D2, and never accepts SQL, columns or concepts. Note the resolver applies
only a metric's DECLARED window today (`_requested_window`); a caller-supplied
window needs a resolver seam first.

### D9 — Scope discipline: deliberately NOT built

> **Amended by [ADR-0094](0094-per-site-authority-answers-per-site.md)
> (2026-10-02):** a metric whose authority is a per-site partition is now
> resolved per site (outcome `computed_by_site`); the first bullet below
> still holds for every other dimension.

- No dimension / site fan-out of the notice (the cache key's
  `authority_context` and `metric_multisite.systems_covering` /
  `PinnedAuthority` are the extension points); the block reports the
  authority system id only, no site label.
- No arbitrary date windows; no caller-supplied window token (D8).
- No trace/SSE event for the metric-layer step (a natural follow-up: a
  visible "Metric layer" step like `_seed_trace_events`; trace-only, no UI
  code).
- (Amended, review iteration 3.) Scheduled prompts DO carry the notice in
  both modes: the cloud scheduler streams through `AgentCoreChatClient`, whose
  hook is session-independent, and the local branch of
  `server._execute_prompt_admitted` composes `local_turn_prompt` in the
  scheduler thread. Scheduled prompts are frequently metric questions, so
  excluding them would reopen the proxy path on the dashboard's own cadence;
  the added source load is at most the TTL-cached resolves of the metrics
  their prompts name.
- No response-integrity gate, no `error_type`, no verdict — prompt
  composition only (the pre-check's posture).
- No new status vocabulary; the dashboard's register is reused verbatim.
- No LLM in matching, classification or rendering.

## Considered alternatives

- **Compute inside the orchestrator runtime prelude, next to the existing
  notice.** Rejected: the runtime has no source connections (Context); it
  would require widening the runtime env/IAM contract for every deployment.
- **Runtime calls the app's `/api/metrics/resolve` over HTTP.** Rejected for
  v1: no app URL/credential in the orchestrator contract, a new
  service-to-service auth path, and a network hop inside the turn; the
  gateway-target form is the right shape and is deferred (D8).
- **Prepend the notice to the question text app-side** (like the plant/time
  brackets). Rejected: `memory_labels.strip_turn_preambles` peels exactly two
  known bracket preambles; a third preamble would leak into memory and
  provenance, and text in transit is injectable — structured data rendered by
  the runtime is not.
- **Fail open to NO notice on resolver failure** (the gap pre-check's rule).
  Rejected (D3): silence here reproduces the defect; the honest degrade is
  "unknown — do not compute".
- **Forbid every scoped figure for computed metrics.** Rejected (D5): it would
  regress the documented site-scoped metric question for direct metrics whose
  lineage is fully known; the derived case — where the proxy risk lives — IS
  forbidden.
- **Concatenate the new rule into `_METRIC_TERM_MISS_RULE`.** Rejected (D6):
  breaks the exact-equality pin; the anchored append gives the same rendered
  placement with zero test edits to the pinned constants.
- **Send rendered text in the payload.** Rejected (D1): any runtime caller
  could then inject prompt text; structured fields rendered by the runtime are
  bounded and sanitized by construction.

## Consequences

- **Positive.** A defined metric can no longer be silently proxied in chat:
  the model either reports the metric layer's number or states the honest
  withhold with the resolver's own reason — the same words the dashboard
  shows. The measured defect becomes an honest "unavailable: input
  unresolvable — no onboarded system maps concept …" answer, identical across
  runs. Every cloud turn path (web, Evals tab, CLI lanes, Quick, try-it) gets
  the same composition, so the golden lane measures the fix.
- **Cost.** Up to 3 resolves per metric-shaped turn, TTL-cached 180 s per
  metric, timeboxed 12 s, 2 worker threads app-wide. A first question about a
  slow-backed metric waits up to the timebox before the model starts (the SSE
  keepalive covers transport). Source-database load rises by at most the
  dashboard's own resolve profile.
- **Behavioral change to accept.** A question naming a DERIVED metric at a
  narrower scope than the metric layer computes now receives the
  deployment-wide value plus an honest "scoped figure not available" — where
  before the model computed something. This is the intended trade (a stable
  honest answer over an unstable proxy); the deferred site fan-out (D9)
  narrows it later.
- **Risks / mitigations.** False-positive matches on common-word code labels
  are gated by the metric-request context (D2) and carry a narrow hatch; the
  `result_unavailable` degrade forbids substitution, so a resolver outage
  makes defined-metric answers stricter, never looser; staleness of a cached
  value is bounded by the TTL and cleared on accept in-process.
- **Documentation.** The maintainer notes gain one invariant line; conventions.md one
  entry; deferred.md the D8/D9 items; `artifact_usage.py` the new evidence
  class.

## Verification

Offline (unit lane, `--disable-socket`): the matcher's admission/gating/
overlap/ambiguity/cap rules on neutral fixtures; classification of every
`ResolvedMetric` shape and every raised class into the four outcomes;
verbatim-but-bounded detail; renderer byte pins (header, both pinned
sentences, block shapes, tail-drop under the cap, well-formedness); TTL and
cooldown behavior with a fake clock; timebox with a stubbed slow resolver; the
finite-positive env parsing; payload key merge and unconditional pop;
runtime composition order and byte-identity without results; the prompt-rule
placement on both modes and both Quick prompts with the pre-existing pins
untouched; the AST tripwire growth; the neutrality ratchet unchanged.
Root: `python3 -m pytest tests/test_no_internal_identity_strings.py -q`.
Deployed: `scripts/smoke_prompts.py --strict` byte-stable on ordinary turns;
`scripts/eval_answers.py --previous <pre-change report>` (informational
diff); the customer golden lane (outside the repo) re-run for the measured
prompt — expected verdict flip to honest absence with the resolver's reason;
one manual turn per outcome (computed direct, computed derived, withheld,
ambiguous) recorded with the block visible in the trace.

## Implementation plan

Function bodies ≤ 50 lines; every new file ≤ 400 lines; fixtures use
`widget_defect_rate` / `widget_units` / `widget_defects` / `Widget Defect
Rate` vocabulary only.

1. `agents/metric_term_resolution.py` — `DefinedMetricMatch`,
   `defined_metric_terms` and its helpers (`MAX_METRIC_TERMS`,
   `MIN_FORM_CHARS`). Tests: `tests/unit/test_metric_term_defined.py`.
2. `agents/metric_turn_notice.py` (new) — results, cache, timebox;
   `agents/metric_turn_render.py` (new) — sanitizer, renderer. Tests:
   `tests/unit/test_metric_turn_notice.py`.
3. `agents/orchestrator.py` — `_METRIC_LAYER_RESULT_RULE`, anchored append.
   Tests appended to `tests/unit/test_metric_term_miss.py`.
4. `agents/server.py` — `ask_sync`'s cloud branch hoisted to
   `_ask_sync_cloud` and offloaded; tripwire detection set grows. Tests:
   `tests/unit/test_ask_sync_offload.py`.
5. `agents/metric_execution_trace.py` (`TURN_SUFFIX`),
   `agents/artifact_usage.py` (evidence class docs), `agents/metric_layer.py`
   (invalidate hook).
6. `agents/chat_client.py` — app-side hook; `LocalChatClient` composes the
   same rendered notice in its producer thread. Tests:
   `tests/unit/test_chat_client_metric_layer_payload.py`.
7. `agents/runtime_app.py` — reserved-key pop, composition, composition bump.
   Tests: `tests/unit/test_runtime_metric_layer_notice.py`.
8. Docs — project invariant, conventions.md entry, deferred.md entries,
   ADR-0066 status line.

### Acceptance criteria

1. A question naming a graph-defined DERIVED metric whose input concept is
   unmapped yields an answer that states `unavailable` and the verbatim
   `no onboarded system maps concept …` reason and contains NO numeric value
   for the metric — in 3/3 repeats.
2. A question naming a computable DIRECT metric yields the metric layer's
   value, unit and window token; a site-scoped variant is answered either from
   that value with the scope stated or from a system-agent query that names
   the block's lineage.
3. Ordinary turns (no defined metric named) produce a byte-identical runtime
   payload and byte-identical composed prompt.
4. With the resolver stubbed to sleep past the timebox, the composed prompt
   carries a `METRIC-LAYER RESULT UNAVAILABLE THIS TURN (timebox)` block within
   timebox + 1 s and the turn proceeds.
5. `_METRIC_TERM_MISS_RULE`/`_METRIC_TERM_CAPTURE_RULE` bytes unchanged; the
   new rule present on both mode prompts and both Quick prompts.
6. No `agents/*.py` or ADR text contains customer or demo literals
   (neutrality ratchet baseline unchanged).

## Implementation notes (2026-09-27)

Deviations from the planning draft, each grounded in the code as found:

- **Composition bump lives at the runtime entrypoints**, not inside
  `_compose_seeded_turn`: the composer has no session id (needed for the
  synthetic/briefing gate), and bumping only when the header is present in the
  composed turn makes the evidence strictly "composed", never "received".
- **No site label in the COMPUTED block** (D9): `metric_multisite.site_of`
  needs a catalog entry, and a catalog read per turn is exactly the extra I/O
  this design bounds; the authority system id is reported instead.
- **Derived-formula window line** names each executed input's applied token
  (review iteration 2); a derived metric that DECLARES a window is withheld
  rather than reported under a period the formula never applied.
- **Two modules, one surface**: the runtime half (sanitizer, renderer, pinned
  text) lives in `agents/metric_turn_render.py` for the file-size cap;
  `metric_turn_notice` re-exports every public name, and the runtime imports
  only the import-pure render half.
- **`LocalChatClient.stream` takes `extra_payload` AFTER `cancel`** (keyword),
  so no existing positional caller shifts.
- **Offline lane default**: the lane-wide `tests/conftest.py` sets
  `METRIC_TURN_NOTICE=off` so the thousands of chat-client tests that never
  stub the metric layer stay byte-identical and offline; the ADR-0078 tests
  enable it explicitly.

## Review iteration 1 (2026-09-27)

Five findings, each fixed with a regression test:

1. Route-seed failure/empty seed dropped the result → `_compose_unseeded_turn`
   (`test_seed_failure_keeps_the_forbidding_metric_layer_preamble`).
2. Over-cap metrics were silently dropped → trailing `overflow` block
   (`test_overflow_beyond_cap_emits_forbidding_overflow_block`,
   `test_overflow_scan_detects_exactly_one_over_cap_match`).
3. Stale resurrection after `invalidate()` → generation-guarded store
   (`test_invalidate_during_inflight_resolve_never_resurrects_stale_result`).
4. Post-match failures went silent → forbidding `result_unavailable` entries +
   boot validation (`test_post_match_resolution_failure_yields_forbidding_blocks_not_silence`,
   `test_malformed_bound_after_a_match_yields_forbidding_block_not_silence`).
5. Local mode had no notice → local producer-thread composition
   (`test_local_turn_carries_the_rendered_notice_before_the_question`).

## Review iteration 2 (2026-09-27)

Five findings, each fixed with a regression test:

1. Briefing-marked turns bypassed the notice → composition is
   session-independent; only the ledger write skips briefings
   (`test_briefing_session_receives_the_same_notice`).
2. A caller could forge `metricLayerResults` → the key is stripped before the
   hook on every path (`test_caller_supplied_reserved_key_never_survives_the_hook`,
   `test_caller_supplied_reserved_key_is_stripped_from_the_runtime_payload`,
   `test_caller_supplied_reserved_key_is_replaced_by_the_hook_owned_results`).
3. Invalid-on-receipt / renderer faults went silent → PRESENT-key fail-closed
   `invalid` marker + static `FALLBACK_NOTICE`
   (`test_present_but_invalid_reserved_key_fails_closed`,
   `test_partially_invalid_payload_keeps_valid_blocks_and_adds_the_forbidding_marker`,
   `test_sanitizer_fault_fails_closed`,
   `test_renderer_exception_uses_the_static_fallback_notice`,
   `test_local_renderer_exception_uses_the_static_fallback`).
4. Cached results survived authority changes → invalidation on every
   AuthorityRule mutation path
   (`test_authority_change_invalidates_cached_turn_result`,
   `test_provision_c4_and_reset_invalidate_the_turn_cache`).
5. Derived results carried no window token → per-input windows, conditional
   scope sentence, declared-derived-window withhold
   (`test_derived_windows_same_mixed_and_absent`,
   `test_derived_metric_declaring_a_window_is_withheld_not_misdated`,
   `test_scope_sentence_is_conditional_on_computed_windows`).

## Review iteration 3 (2026-09-27)

Five findings, each fixed with a regression test:

1. Imperative requests ("show throughput", "report <initialism>") admitted no
   single-word defined form → anchored imperative + list-join admission gates
   (`test_imperative_request_admits_exact_single_word_forms`,
   `test_imperative_list_chains_every_named_single_word_metric`,
   `test_casual_mentions_stay_unadmitted`,
   `test_gap_precheck_gate_is_unchanged_by_the_imperative_admission`).
2. `MAPS_TO` mutations left the value cache stale → the ONE decorator on
   every mapping writer, the whole provision included
   (`test_concept_mapping_repoint_drops_the_turn_cache`,
   `test_field_concept_mint_drops_the_turn_cache`,
   `test_steward_remap_drops_the_turn_cache`,
   `test_provision_failing_before_c4_still_drops_the_turn_cache`,
   `test_every_resolver_affecting_writer_uses_the_one_invalidation_home`).
3. AuthorityRule soft deactivate/reactivate did not invalidate → decorated
   `_set_rule_lifecycle` (`test_rule_lifecycle_drops_the_turn_cache`).
4. Matching work was unbounded → `MAX_MATCH_INPUT_CHARS` with a fail-closed
   `input_overflow` block and a one-pass scan
   (`test_oversized_question_raises_the_matcher_bound_not_a_silent_miss`,
   `test_repeated_form_under_the_bound_never_hides_a_later_term`,
   `test_scan_is_one_pass_one_span_per_word_start`,
   `test_oversized_question_yields_forbidding_input_overflow_block`,
   `test_real_matcher_over_bound_reaches_the_forbidding_block`).
5. Scheduled prompts differed by mode and contradicted D9 → both modes carry
   the notice, D9 amended
   (`test_local_scheduled_prompt_carries_the_notice`,
   `test_local_scheduled_prompt_without_a_defined_metric_is_unchanged`,
   `test_scheduler_session_is_not_excluded_by_the_cloud_hook`).

### Review iteration 4 (orchestrator-closed after the loop cap)

1. **Bare single-word subject.** `throughput?`, `how is throughput`, `did WDR improve`, `WDR over the last week` now admit through `_is_question_target`: anchored on both sides (empty head or a generic interrogative opener; a tail of at most four words that is empty or opens with a generic state/trend predicate; at most two words after an opener). `WDR team meeting notes`, `is the WDR meeting on friday`, prose mentions stay unadmitted. Tests: `test_bare_single_word_metric_as_question_subject_is_admitted` (10 cases), `test_bare_form_inside_prose_stays_unadmitted` (7 cases).
2. **Global reset.** The graph wipe and core-concept reseed in `onboard_reset` lives in `_global_reset_wipe_graph` (now decorated; main hoisted it off the event loop concurrently in `_global_reset_steps`), so a partial wipe still drops the cache and the async route holds no inline graph write. Tests: `test_global_reset_graph_wipe_drops_the_turn_cache` (none/wipe/reseed failure), `test_global_reset_route_calls_the_decorated_wipe_off_the_event_loop`; the writer inventory pins both reset helpers.
3. **`_repoint_maps_to`** (81 lines) split into `_repoint_require_field`, `_repoint_next_version`, `_repoint_replace_edges` and an 18-line decorated coordinator; behaviour byte-identical (existing repoint tests unchanged).

### Review iteration 5 (measured on the deployed build)

A direct question ("what is the <metric>?") now carries the withheld notice and the model states it verbatim and refuses a substitute. A counterfactual question that named the metric as an acronym mid-sentence ("if X had been eliminated, what would the <ACRONYM> impact have been?") did NOT match: the single-word gates (list, imperative, measurement words, question subject) all anchor on the term's neighbourhood. New admission: a declared form written as an all-caps acronym in the ORIGINAL text of an interrogative question admits wherever it sits (`_question_acronyms`; casing is read before normalization). An acronym is not casual prose the way a single English label is, so the false-positive cost is one extra notice block, while the false negative was a fabricated number. Statements and lower-case mentions stay unadmitted. Tests: `test_declared_acronym_anywhere_in_a_question_is_admitted` (4), `test_acronym_admission_needs_question_shape_and_original_casing` (3).

### Review iteration 6 (measured on the deployed build, golden + smoke lanes)

Two residual lane failures were the same defect: the model wrote a number for a quantity it was NOT reporting — the metric's scale factor inside an honest withhold, and "(e.g. <metric> <= 180 seconds)" inside a clarifying question about goals. Both read as data claims to a reader and to the computed fabrication detector. Fixes: the WITHHELD bullet of `REQUIRED_RESPONSE` now forbids formulas, scale factors, example and placeholder numbers; `orchestrator._NO_EXAMPLE_NUMBERS_RULE` is APPENDED to the metric-term miss rule (prefix pins survive) so every miss/withhold/clarify answer carries it on all surfaces. `MAX_NOTICE_CHARS` 3000 → 3200 (the sentence cost 89 characters; three maximal withheld blocks must still fit). Tests: `test_no_example_numbers_rule_is_appended_and_reaches_every_surface`, `test_withheld_response_rule_forbids_formulas_and_example_numbers`.

### Note (2026-10-16): a WITHHELD block withholds the number, not the question

Measured on an analytics deployment: a question asked for a count AND for the rule that decides the counted state. The count's metric was WITHHELD, and most runs answered at once, saying the metric layer had "already resolved this exact question", so the rule was never asked for. A notice that carries a WITHHELD, UNAVAILABLE or invalid block now ends its rules with `metric_turn_render.WITHHELD_SCOPE_LINE` (the block withholds only that metric's number; a part asking how the term is defined, decided or measured is still answered from the concept notes or the system that holds the definition), placed before the never-substitute sentence, which is unchanged; `FALLBACK_NOTICE` carries it too and a computed-only notice is byte-identical. The standing prompt says the same in `orchestrator._WITHHELD_SCOPE_RULE`, right after the absence-lookup rule. `MAX_NOTICE_CHARS` 3200 → 3400 so three maximal withheld blocks still fit (3368 characters). Tests: `tests/unit/test_metric_withheld_scope.py`.
