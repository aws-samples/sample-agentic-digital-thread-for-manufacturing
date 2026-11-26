<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0062: Review queue, promote-to-golden, findings ledger, and the bounded triage annotator

## Status

**Accepted — 2026-08-18.** Written before implementation to pin the contract,
then verified against a deployed environment before acceptance:

- The prerequisite feedback fix landed: `FEEDBACK_TABLE_NAME=adt-feedback` is
  present on the deployed task and a submitted thumbs-down PERSISTED (the same
  call silently discarded the record before).
- Full value chain proven live: feedback → computed queue (surfaced with a
  `triage-only` pre-score) → steward decision → draft download
  (`Cache-Control: private, no-store`, attachment, **no** `answer_must_match`,
  hints only, no identity leaf) → external merge (an in-repo destination was
  REFUSED, nothing written) → `scripts/eval_answers.py --golden-set` scored the
  promoted golden PASS. The interchange format never changed.
- Annotator proven live while scheduling stays OFF: `scheduled_enabled: false`,
  a body override 422s, one-shot POST returned 202, the worker audited 1
  flagged candidate and wrote 1 `data_gap` finding with MEASURED usage
  (394/259 tokens) against a `durable: true` day budget.
- Data boundary held: the durable ledger row carries only references — the
  source answer text is absent from the persisted blob — and no
  dollar/price/estimate-shaped key appears anywhere in the status payload.
- Writer telemetry reads back through the real ledger (`eval-finding` and
  `audit-run` both `fired: true`; `proposed-mapping` correctly false with no
  steward filing yet). Note the read path carries a 60 s in-process cache, so
  the panel can lag a write by up to a minute — expected, not a gap.
- Gates: the local CI parity run ALL PASSED; full `smoke_prompts.py` 15/15
  grounded + cross-system proven; `audit_learning.py --strict` clean (no
  learning/memory residue from queue, draft or annotator traffic).

Builds on [ADR-0061](0061-evals-admin-tab-and-answer-auditor.md) — this ADR
implements the four items 0061 explicitly deferred (review queue,
promote-to-golden, findings ledger/routing, a scheduled auditor) and inherits
every 0061 invariant unchanged. Also builds on
[ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) (measured-only
cost; annotate-never-gate; computed verdicts — an LLM never produces one;
tombstones/rejections are steward claims),
[ADR-0054](0054-measurement-honesty-and-experiment-harness.md) (untrusted text
is bounded and secret-shape-rejected at write),
[ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(a learning writer ships with its read path plus usage telemetry;
replace-not-accrete for generated artifact classes; steward rows always
survive), [ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — nothing customer-specific in engine defaults), and
[ADR-0059](0059-failure-signal-accumulator-and-runtime-steward-questions.md)
(the never-auto-accept proposal-source precedent this ADR extends with
`answer-audit`).

## Context

Two hard requirements shape every decision below, and both arrived after the
v1 plan was written.

**(1) Customer data must never enter this repository's git.** This repository
is a sample that publishes to AWS Samples. The Phase-2 features are, by
construction, machinery that reads *real customer traffic* — thumbs-down
feedback, failed golden rows, LLM-generated root-cause prose — and produces
artifacts a steward wants to keep. Every one of those artifacts is a candidate
leak path, and the leak that matters most is the most attractive one: a
promoted golden entry pasted into the tracked `golden_evals.json`. Two
verified facts make this a design problem rather than a discipline problem:

- `agentic_digital_thread/tests/unit/test_engine_domain_neutrality.py` scans
  `*.py`, `*.ts`, `*.tsx`, `*.js` only — its own comment above `_SCAN_ROOTS`
  says "demo golden JSON is out of scope by extension" — and its denylist
  catches only known term families. Nothing in the repository stopped a
  customer-derived golden JSON from being committed.
- `.gitignore` is not a boundary: `git add -f` and a rename defeat it, and a
  file that was ignored yesterday is tracked the moment someone renames it into
  a non-ignored path.

**(2) The default-off scheduled auditor must be provably testable live.** The
v1 design gated the auditor behind a default-off environment flag with no
non-production path to exercise it; the only "test" available was flipping the
flag in a deployed environment and waiting for a nightly tick.

**A verified blocker sat underneath workstream A.** The queue's primary source
is user feedback, and user feedback was being silently discarded in every
deployed environment: `agents/storage.py`'s `DynamoDBStorage.put_item()`
returns `True` when no table name is configured (a deliberate contract — no
caller inspects the boolean, and the offline lane runs table-less on purpose),
but that no-op was logged at DEBUG only, `FEEDBACK_TABLE_NAME` was read solely
in `agents/config.py` and appeared NOWHERE in `stacks/`, and the live `adt-app`
task definition confirmed the variable was absent — while `POST /api/feedback`
answered `{"status": "success"}`. The deceptive part was the silence, not the
return value. Fixing it is a separate change (the feedback-durability fix),
declared a hard prerequisite below.

Separately, [ADR-0061](0061-evals-admin-tab-and-answer-auditor.md) D6 surfaces
managed AgentCore Evaluations on the same tab. Whether those managed scores
could also *source* the review queue was unresolved (the design review and plan
v1 disagreed about what the result events contain), so Wave 0 settled it
against a deployed environment; the facts are recorded in D13.

## Decision

### D1 — Data classification is the lead constraint: one approved source store, minimized derived stores, a transient draft, and an external destination

Every datum this feature touches is tiered, and the tier decides where it may
live. This table is normative — a new field belongs to a tier before it belongs
to a schema:

| Data | Where it may live | Where it may NEVER live |
|---|---|---|
| Raw customer turn text (question/answer) | ONLY the approved source store: the feedback table (the prerequisite fix: documented TTL + access) | JobStore kinds, logs, /tmp, this repo's git, the golden draft's server-side state |
| Steward queue decisions | `eval-decision` JobStore rows — verdict/tags/bounded note + OPAQUE source refs only | any copy of turn text |
| Findings (durable) | `eval-finding` rows — taxonomy, opaque source/question refs, status, timestamps, usage | `claim_excerpt`, `rationale`, `suggested_mapping` VALUES (until steward files a proposal) |
| Findings detail (customer-derived) | `eval-finding-detail` rows — SHORT TTL 7 d, labeled customer-derived | anything durable; never re-imported into the durable row |
| Golden draft | TRANSIENT response body only (download) | JobStore/DynamoDB/logs/tmp/browser durable state |
| Final promoted golden | the customer-owned EXTERNAL repo/file only | anywhere in this repository (the boundary gate is what holds — `.gitignore` is NOT a boundary: `git add -f`, renames) |
| Sample demo golden | `agentic_digital_thread/agents/demo/golden_evals.json` (the ONE sanctioned path, manifest-pinned) | any second tracked golden-shaped JSON |

The approved source store is the `adt-feedback` table: key `feedback_id`
(HASH, a digest of session id + message id, so it is stable per answer) +
`timestamp` (RANGE), the `feedback-date-index` time index, TTL attribute `ttl`,
retention **90 days** (`FEEDBACK_TTL_DAYS` — the repo's existing decay horizon
for accumulated customer-derived state). Raw turn text lives there and nowhere
else; derived stores persist *references* (a sha-16 opaque hash of the source
ref) and *tags*, never values.

**What actually holds the repository boundary** is a test, not a convention:
`agentic_digital_thread/tests/unit/test_customer_golden_boundary.py`. It
enumerates tracked `*.json` files, parses each, and FAILS if anything with
golden-set shape (a dict carrying `version` plus a `questions` list whose
entries carry `id`/`text`/`expect`) exists outside the one sanctioned demo
path; it pins the sanctioned sample's question ids as an explicit manifest (so
adding or renaming a demo question requires a deliberate gate edit); and it
runs the customer-identity denylist families imported from the neutrality gate
over the sample golden's full text. The gate skips loudly — never silently —
when `.git` is absent (packaged-source runs). It exists precisely because the
neutrality ratchet scans no JSON by design.

**Evals-tab limitation (Phase 2), stated plainly:** the tab reads and runs ONLY
the packaged demo golden set. It does not read, upload, or run customer golden
sets; those stay CLI-only through `scripts/eval_answers.py --golden-set`.
Changing that is a separate ADR — not a follow-up commit.

**Superseded in part (2026-08-19, [ADR-0064](0064-customer-golden-set-runs-are-upload-per-run.md)):**
the tab may now RUN a customer golden set the steward uploads per run. The
boundary is unchanged: the set is transient request/process data, is persisted
NOWHERE server-side, and the customer-owned external file remains its only
durable home. `golden_evals.json` remains the sole interchange format, and the
out-of-scope entry below ("Any server-side persistence of a golden draft or a
golden set; a golden-set DynamoDB editor") stands unamended.

Rejected: `.gitignore` plus reviewer vigilance (defeated by `git add -f` and by
renames, and a sample repo's contributors are exactly the population that will
not know the rule); extending the neutrality gate to JSON *instead of* a new
gate (its denylist catches only known term families, so an unknown customer's
identifiers pass clean — shape-based enumeration is the load-bearing check).

### D2: The feedback-durability fix is a hard prerequisite, and no historical feedback is recoverable

Waves 2+ do not start against a deployed environment until
the feedback-durability fix is in place (dedicated `adt-feedback` table with a
stable feedback id, the date/time index, TTL, least-privilege IAM, and
`FEEDBACK_TABLE_NAME` wired into the `adt-app` task). The reason is the
verified blocker above: with no table name configured, `put_item` returned
`True` and said so only at DEBUG, so every thumbs-down in every deployed
environment was accepted by the UI and dropped on the floor. (That fix also
announces the unconfigured no-op at WARNING under the stable log key
`storage_unconfigured` — the breadcrumb whose absence hid the bug for months,
and the precedent this ADR's budget primitive reuses in D11.)

**Nothing is recoverable.** The rows were never written anywhere — there is no
log, no dead-letter, no alternate store to backfill from. The queue therefore
starts EMPTY on every environment and accumulates only post-prerequisite
traffic. This ADR does not restate or duplicate that fix's scope; it depends on
it and pins the field names it consumes (`feedback_id`, `timestamp`,
`feedback-date-index`, `ttl`).

A second verified constraint from the same reading: `FeedbackStorage` exposes
`store()` and `get_by_time_range()` only — a bounded time query with no lookup
by `message_id`. The computed queue therefore reads a BOUNDED window with a
result cap (using the date index, never an unbounded scan), and steward
decisions are keyed by a stable source ref, never by positional queue state.

### D3 — The queue is COMPUTED ON READ; only steward decisions persist

`GET /api/admin/evals/queue?window=…&limit=…` computes its list on every
request: a bounded feedback window (negative first) plus the failed rows of the
newest-N persisted `eval-report`s, minus any session matching
`runtime_app._SYNTHETIC_SESSION_PREFIXES` (the live tuple is ITERATED, not
re-listed, so a fourth prefix auto-extends the exclusion), joined against
persisted `eval-decision` rows. There is no persisted queue, no refresh writer,
no refresh job, and no `/queue/refresh` route.

Display text is joined live from the source store, so it is never copied into a
derived row (D1). Sources may expire underneath a decision (the 90-day feedback
TTL): the item then renders a distinct `source_expired` state — never an error,
never a fabricated placeholder. A `GET` performs zero writes, pinned by spy
persistence seams rather than by inspection.

Rejected: a persisted queue with a refresh job (v1) — it added a writer, a job,
and a route, and created a staleness class where a steward acts on a snapshot
of traffic that no longer matches the sources; computing over a bounded window
at demo volume is cheap.

### D4 — Machine pre-scores are triage-only; the steward is the sole authority

`compute_queue` attaches a deterministic `machine_prescore` labeled
`"triage-only"` — it orders attention and nothing else. It never sets a
verdict, never filters an item out, and no code path lets machine code write an
`eval-decision` row: `decide(source_ref, verdict, tags, note)` is the module's
only writer and is reachable only from the steward route. Verdicts are the
pinned enum `correct | incorrect | needs_context`. Steward decisions and
annotations survive recomputation byte-identical, and the machine never writes
`status=rejected` (ADR-0053).

### D5 — Promote-to-golden is a DOWNLOAD-ONLY transient draft with no generated answer regex

A promoted golden draft is customer-derived data — its `text` is a real
customer question — so it is classified as *transient response data* and never
persisted anywhere server-side:

- `POST /api/admin/evals/queue/golden-draft` (POST, not GET, so no cache layer
  treats it as cacheable) returns the draft as an attachment with
  `Cache-Control: private, no-store`, `Pragma: no-cache`, and an opaque
  sha-16 filename.
- ZERO server-side persistence: no JobStore row, no DynamoDB item, no `/tmp`
  file, no draft content in any log record. This is pinned by a
  persistence-seam test (spies on JobStore, storage writes, `tempfile`,
  `open`-for-write in the route module namespace, and the logging tree), not by
  reading the code.
- The browser holds the draft only for the download tick — no
  localStorage/sessionStorage/IndexedDB/query-cache retention of the body.

The draft's *content* is deliberately weak where v1 was strong:

- **No `answer_must_match`, and no code path that can emit one.** Auto-derived
  phrase gates enshrine current phrasing; a routing identifier that appeared in
  yesterday's answer need not appear in a better answer tomorrow. Absence is
  pinned.
- Observed `intelligence_summary` counters go ONLY into a top-level
  **non-gating** `hints` block (`observed_systems`, `observed_neptune_queries`,
  …). Hints inform the steward who will write the real expectations; they are
  ignored by scoring.
- Provenance is minimized to `{source: "promoted", source_ref_sha}`. There is
  **no `decided_by`** in the external file — steward identity must not leak
  into a customer-owned artifact — and no raw source ref.

The destination is an external, customer-owned golden set.
`scripts/merge_golden_draft.py --draft <file> --golden-set <path>` validates the
merge through `eval_core.load_golden_set` and REFUSES (exit 2, no write) any
`--golden-set` whose resolved path is beneath this repository root — including
the sanctioned demo golden itself. The UI copy names that external destination
and never mentions `golden_evals.json` or any paste-into-this-repo phrasing.

Rejected: rendering the draft JSON in the page with a copy button (v1) — the
affordance's obvious next step is a paste into the tracked demo golden, which is
exactly the boundary violation; a server-side golden editor or draft store
(out of scope, and it would durably persist customer text); generating one
broad `answer_must_match` disjunction from cited system ids (still an
auto-generated phrase gate, just a lenient one).

### D6 — The findings ledger persists a MINIMIZED row; retained generated detail is customer-derived under a 7-day TTL

The v1 claim that findings are "reference-only" was false: `audit_answer.py`
findings carry a `claim_excerpt` (300 chars of the answer) and a free-text
`rationale`, both customer-derived. So the ledger splits:

| Kind | TTL | Contents |
|---|---|---|
| `eval-decision` | 180 d | `dec-<sha256(source_ref)[:16]>` → `{source_ref, source, verdict, taxonomy_tags, steward_note?, decided_at, decided_by}` — steward rows; regeneration never touches them |
| `eval-finding` | 90 d | Minimized durable row: `{finding_id, taxonomy (pinned 7), source_ref_sha, question_ref, run_path, status, created_at, usage?}` + ONE bounded replace-not-accrete `index` item. NO excerpt/rationale/mapping values |
| `eval-finding-detail` | **7 d** | Customer-derived detail for steward review: `{finding_id, claim_excerpt, rationale, suggested_mapping?}` — capped and secret-shape-rejected BEFORE write |
| `steward-<finding_id>` | 180 d | Steward annotations — survive re-import byte-identical |
| `audit-run` | 7 d | `{run_id, trigger, status, counts, usage?, started_at, finished_at}` + the active lock slot |
| `audit-budget` | 48 h | Dedicated item (NOT dict-like JobStore state): `{day_key (UTC), claimed}` |

Consequences that are part of the decision, not accidents: when a detail row
expires the durable row renders `detail expired` (a distinct state, never an
error), and `POST /findings/{id}/file-proposal` then returns **409** — filing a
proposal requires the detail that justifies it. Minimization is pinned by a
RECURSIVE leaf-key allowlist over everything persisted, so a renamed or nested
excerpt leaks nothing past the gate regardless of key name. Re-import DELETES
prior generated rows in its `source_report_sha` scope before writing (5 findings
re-imported as 3 leaves exactly 3), while steward rows survive byte-identical
(replace-not-accrete, ADR-0052). `usage` is the exact measured schema or
ABSENT — never zero, never dollars (ADR-0053).

The single door through which `suggested_mapping` VALUES enter durable storage
is a steward-triggered `file-proposal`, which passes only
`SUGGESTED_MAPPING_KEYS` kwargs to `log_proposed_mapping(source="answer-audit")`
— and `"answer-audit"` joins `_NEVER_AUTO_ACCEPT_SOURCES`, pinned behaviorally
(insert a high-confidence proposal, run the real auto-accept sweep, assert it is
still pending while a non-protected sibling is accepted).

### D7 — Findings import binds its source report in the request; fail-open was rejected

`POST /findings/import` takes `{findings_file, source_report}` TOGETHER and
validates the binding transiently: sha over the uploaded report must equal the
findings file's `source_report_sha`, taxonomy must be the pinned 7, and every
`evidence_refs` entry must resolve inside the supplied bundle. Only minimized
rows (plus 7-day detail rows) are persisted; **the uploaded report is never
handed to any storage call.** The size cap is enforced BEFORE parsing: the raw
`Request` handler rejects an oversized `Content-Length` and reads the stream
under a hard cap (413) before `json.loads` ever runs.

Rejected: v1's "validate against the referenced persisted report when
resolvable" — that is fail-OPEN. Findings carry a `source_report_sha` while
server reports are keyed by `run_id`, and a CLI-produced report may not exist
server-side at all, so "not resolvable" would have become the normal path and
validation would have been skipped exactly when it mattered.

### D8 — LLM output is untrusted text

Generated `rationale`, `claim_excerpt`, and `suggested_mapping` strings pass
through the same treatment ADR-0054 defined for steward prose — type/length caps
(excerpt ≤300, rationale ≤1000, mapping strings ≤200) plus secret-shape
rejection via `agents/untrusted_text.py` — BEFORE any persistence or proposal
filing. Rejected values are dropped and counted, and are never echoed in a
response body or in a log record. v1 applied this to steward notes only, which
was backwards: the LLM is the higher-volume, lower-accountability writer.

### D9 — ONE `/api/admin/evals` router

The new queue, findings, and audit route modules are `include_router`-ed into
the existing `eval_routes.py` parent. `server.py` keeps its single mount and its
single `_check_admin` auth boundary — no new top-level route module, no second
auth posture to keep in sync.

### D10 — Every writer fires the REAL artifact-usage ledger

Findings-import, a completed audit run, and proposal-filed each call
`agents/artifact_usage.py::bump_artifact_usage` with their own ref kind
(`eval-finding`, `audit-run`, `proposed-mapping`), matching the existing
callers' ref conventions; `GET /audit/status` and the findings panel read those
ledger rows back. That is the ADR-0052 contract (a writer ships with its read
path and ledger evidence it fired). v1's ad-hoc "fired-counters on the index
item" was a parallel invented telemetry contract and is rejected.

### D11 — The annotator has ONE entry, a registration-only flag, and a global UTC-day budget claimed atomically

`agents/audit_scheduler.py` exposes exactly one public entry:
`request_audit(trigger: Literal["manual","scheduled"])`. It acquires the
`audit-run` active lock, claims budget slots atomically BEFORE any model call,
then launches a single worker `_run_audit` in a daemon thread. `trigger` is
**provenance only** — it is stored on the run record and drives zero behavioral
branches; the signature accepts no count, cap, model, or source argument
(pinned by introspection), so manual and scheduled runs over the same fixture
produce identical outcomes apart from run id, timestamps, and the trigger label.

`NIGHTLY_AUDIT_ENABLED` (default OFF) gates **registration only**. The admin
one-shot `POST /api/admin/evals/audit/runs` works regardless — 202 with a
`run_id`, 409 `audit run already active`, 429 `daily audit budget exhausted`
(distinct pinned details) — and that route IS the live test path (requirement 2
above). There is no dry-run mode and no separate CLI worker. The scheduled path
is proven in the unit lane by capturing the registered callback and invoking it,
asserting it calls `request_audit(trigger="scheduled")`.

The ADR-0061 D3 run-ALL rule keeps its ORIGINAL scope — the golden-suite
controls: `POST /evals/run` still 422s on any subset key and the tab still
renders exactly one golden-run affordance. The audit run-now route and button
are a DISTINCT surface (separate mutation, separate testid) and are not a
subset run of anything, so the run-ALL pin does not reach them.

The budget is **global across manual and scheduled runs combined**, bucketed by
UTC day, and claimed through a new narrow module-level primitive in
`agents/job_store.py`:

```
claim_budget_slots(kind, day_key, n, cap) -> int          # slots granted, 0 = refused
claim_budget_slots_detailed(...) -> BudgetClaim           # adds .durable
utc_day_key(now=None) -> "YYYY-MM-DD"  (UTC)
```

It writes a dedicated budget item with a real numeric `claimed` attribute and
moves it ONLY by a conditional `UpdateItem` (`ADD claimed :n` with
`ConditionExpression attribute_not_exists(claimed) OR claimed <= :cap - :n`),
with bounded retries: a refusal that still has room retries once for exactly
the room, learning the committed total from the failed condition's
`ReturnValuesOnConditionCheckFailure` payload rather than a `GetItem`. There is
no read-then-write on the path, so two racing claimants cannot jointly exceed
`cap` (under-granting is safe for a spend cap; over-granting is not).

It is deliberately NOT a method on `JobStore`: that class serializes a job's
whole state into one `job_state` JSON attribute (DynamoDB can express no
attribute-level condition over a blob) and is deliberately FAIL-SOFT —
`eval_runs.py`'s own docstring admits its JobStore lock is "a NON-ATOMIC
check-then-set". Both properties are exactly wrong for spend control. Mode
split: with a table configured, a claim that cannot be consulted raises
`BudgetUnavailable` and the caller **FAILS CLOSED** (zero converse calls, run
recorded `skipped_budget`) — a refusal (`granted == 0`, budget consulted and
exhausted) and an unavailable budget are distinguishable and neither can be
mistaken for success. With no table configured (offline unit lane, local dev) an
in-process counter applies; that fallback is announced at WARNING under the
stable key `budget_claim_memory_only` and is visible in
`BudgetClaim.durable`, because a silent fail-soft no-op reporting success is
precisely how the dropped-feedback bug (D2) hid for months.

The worker's isolation is architectural, not conventional: `_run_audit` talks to
Bedrock through the `audit_core` converse seam and imports no chat, session, or
memory module (pinned by an import-graph walk — `audit_learning.py --strict`
does not prove memory absence).

### D12 — Workstream D is "bounded triage annotation", not "deep-context auditing"

The scheduled job's inputs are feedback and report excerpts — not the curated
evidence documents `scripts/audit_answer.py` consumes in the dev lane. Calling
it a deep-context auditor would oversell it in the UI and in the ledger.
Everywhere it is surfaced it is labeled **bounded triage annotation**; the CLI
lane keeps the deep-context name. The parity extraction (`agents/audit_core.py`)
is shared, and all pre-existing `test_audit_answer*` tests must pass with ZERO
edits (the ADR-0061 D2 bar), with the converse seam OBJECT owned by
`audit_core` after re-export.

### D13 — Managed AgentCore Evaluations stay OUT of the queue in v1, unconditionally (Wave-0 observation)

A read-only Wave-0 spike against the deployed Management results log group
(`/aws/bedrock-agentcore/evaluations/results/…`) settled what the events
contain. **The facts correct both the earlier design review and plan v1** —
each had part of it wrong:

- Result events ARE per-evaluation, not aggregate-only: each carries
  `attributes.gen_ai.evaluation.name` (Builtin.Faithfulness / Correctness /
  ResponseRelevance), `.score.value`, `.score.label`, and `.explanation` — all
  populated.
- They DO carry ids: `attributes.session.id` (populated, 36 chars), `traceId`,
  and `attributes.gen_ai.response.id`. Plan v1's "per-event session linkage is
  unverified / absent" was wrong.
- Question and answer text is ABSENT — no input/prompt/output/answer keys
  anywhere; only the judge's explanation. (Good for D1: there is nothing to
  leak.)
- **But that `session.id` is the AgentCore RUNTIME session id, which ADT mints
  fresh per invocation and never persists** (`chat_client.py:1136`
  `"session-" + uuid4().hex[:28]`; `server.py:831` `str(uuid4())`), while ADT's
  own session id travels separately in the payload. Nothing stores the pairing.
  So per-event linkage exists *in the event* and is NOT joinable to a turn
  today.

Decision unchanged, with a stronger reason than "unverified": managed evals are
not a queue source in v1. The **primary** reason remains yield — 10% trace
sampling plus hours of lag means a negligible number of linkable low-score
events at demo volume — and the non-joinable id compounds it. No conditional
follow-up task exists; the tab keeps surfacing managed scores as ADR-0061 D6
already specifies (complement, never replace).

Recorded unlock path, explicitly OUT of Phase 2 scope: `_CloudStreamState`
already holds BOTH ids at stream time (constructed with `session_id` AND
`runtime_session_id`, `chat_client.py` ~line 1139), so persisting a bounded
`runtime_session_id → (session_id, message_id)` mapping would be a small
additive change. It would not change the sampling-and-lag yield argument, which
is why it is not being done now.

## Considered alternatives

- **`.gitignore` plus review discipline for customer goldens.** Rejected — see
  D1: `git add -f` and renames defeat it, and the neutrality ratchet that
  contributors assume covers this scans no JSON.
- **A server-side golden editor / persisted drafts.** Rejected: it durably
  stores customer question text in this application's tables and makes the
  repository the natural home for the result. The file schema remains the sole
  interchange format (ADR-0061 out-of-scope list, unchanged).
- **A persisted queue with a refresh job.** Rejected (D3): extra writer, extra
  route, and a staleness class, for no benefit at demo volume.
- **A bespoke LLM triage judge in front of the queue.** Rejected: managed
  AgentCore Evaluations already does sampled LLM scoring, and the value of the
  deterministic lane is that its verdicts are computed. Machine pre-scores stay
  triage-only ordering hints (D4).
- **A budget counter on `JobStore`.** Rejected (D11): a fail-soft blob store
  cannot express an attribute-level condition and must never authorize spend.
- **A default-off scheduler as the only auditor entry point.** Rejected (D11):
  it is not live-provable — the only test would be flipping a production flag.
  The one-shot admin route is the test path; the flag gates registration only.
- **Normalized-token clustering of `ux_gap` findings in the digest.** Rejected:
  it forces every ref to resolve on digest read (an N+1 over the source store,
  and a needless customer-text join). The digest is taxonomy counts plus a
  bounded ref list; refs resolve only on expand.

## Explicitly out of scope

- Reading, uploading, or running customer golden sets from the Evals tab
  (CLI `--golden-set` only in Phase 2; changing this is a separate ADR).
  **That separate ADR is now [ADR-0064](0064-customer-golden-set-runs-are-upload-per-run.md)
  — RUNNING an uploaded set is in scope there; STORING one is not, here or
  there.**
- Any server-side persistence of a golden draft or a golden set; a golden-set
  DynamoDB editor.
- Managed AgentCore Evaluations as a queue source, and the
  `runtime_session_id → (session_id, message_id)` mapping that would make their
  events joinable.
- Automatic ticket filing or automatic proposal filing from findings — filing
  stays a steward click, and `answer-audit` proposals never auto-accept.
- `ux_gap` clustering, a digest writer, and notifications (read-time
  aggregation only).
- A dry-run auditor mode or a separate CLI audit worker.
- Any `stacks/` change, new DynamoDB table, or EventBridge rule in this
  change: the feedback table is the prerequisite fix's scope; everything here rides
  JobStore kinds on the existing onboarding-sessions table plus the one
  conditional-claim primitive.
- Backfilling historical feedback (D2 — none exists).

## Consequences

- **Positive:** a steward can triage real thumbs-down traffic and golden
  failures from the tab, and carry a confirmed item out to their own golden set
  through a download that leaves no trace on the server and nothing in this
  repository; findings become a durable, minimized, taxonomy-grouped ledger with
  a real usage ledger behind it; the scheduled annotator is exercisable on
  demand from day one instead of on a flag flip in production.
- **Negative / risks:** the computed queue reads a bounded window, so a steward
  cannot see beyond it and a source that ages past the 90-day feedback TTL
  leaves a decision pointing at an expired source (rendered as a distinct
  state); the 7-day detail TTL means a finding left unattended for a week loses
  the excerpt and rationale that justified it, and `file-proposal` then 409s —
  deliberate, and the honest cost of not durably storing customer-derived prose;
  drafts intentionally carry no answer expectations, so a promoted golden is a
  skeleton the steward must finish; the in-process budget fallback is a
  single-process guarantee only (announced, and visible in
  `BudgetClaim.durable`); and the repository boundary gate skips when `.git` is
  absent, so a packaged-source consumer runs without it (loudly).
- **Verification:** the pinned-by table below, plus the deployed closure pass
  (computed queue over real post-prerequisite feedback; decision → download
  draft → `scripts/merge_golden_draft.py` into an EXTERNAL set →
  `scripts/eval_answers.py --golden-set` accepts it; findings import paired with
  a real `audit_answer.py` report; a one-shot audit run with budget decrement
  and a 409 on immediate retry; full `smoke_prompts.py` plus
  `audit_learning.py --strict` proving no learning residue; `pytest tests/e2e`;
  the local CI parity run).

## Pinned-by

| # | Invariant | Test | Wave |
|---|---|---|---|
| 1 | Draft generation persists NOTHING server-side (JobStore/DDB/logs/tmp) and responds `private, no-store` + `Pragma: no-cache` + attachment/opaque filename via POST | `test_eval_promote.py`, `test_eval_queue_routes.py` (spy persistence seams + header assertions) | 2 |
| 2 | No tracked golden-shaped JSON outside the ONE sanctioned demo path; sample golden's question ids manifest-pinned; customer denylist clean over its text | `test_customer_golden_boundary.py` (tracked-file enumeration + parse) | 1 |
| 3 | Generated drafts contain NO `answer_must_match`; observed counters only in a non-gating `hints` block; provenance = `{source:"promoted", source_ref_sha}` with NO `decided_by` | `test_eval_promote.py` (absence + shape) | 2 |
| 4 | `merge_golden_draft.py` refuses any destination beneath this repo root | `test_merge_golden_draft.py` (behavioral exit-2, no write) | 2 |
| 5 | Findings import requires `{findings, source_report}` together; sha binding fail-closed; uploaded report never persisted; size cap enforced BEFORE parse | `test_eval_findings_routes.py` (413/422 + spy seams) | 2 |
| 6 | Durable rows are minimized: recursive leaf-key ALLOWLIST over everything persisted by the queue/findings stores (renamed/nested leaks fail) | `test_eval_queue.py`, `test_eval_findings.py` | 1 |
| 7 | Retained generated detail is customer-derived: 7 d TTL attribute present; expiry ⇒ `detail expired` UI state; file-proposal 409s after expiry | `test_eval_findings.py`, `test_eval_findings_routes.py`, `evals-findings.test.tsx` | 1–3 |
| 8 | LLM output bounded + secret-shape-rejected before persistence/filing; rejected values never echoed in responses OR logs | `test_eval_findings.py` (behavioral + log capture) | 1 |
| 9 | Replacement DELETES stale generated rows in scope (5→3 leaves exactly 3); steward rows survive byte-identical | `test_eval_findings.py` | 1 |
| 10 | `usage` is exact `{input_tokens, output_tokens}` or ABSENT (not zero); recursive absence of dollar/estimate/price-shaped keys in every persisted/rendered payload | `test_audit_scheduler.py`, `test_eval_findings.py`, `evals-findings.test.tsx` | 1–4 |
| 11 | Synthetic exclusion iterates ALL of `runtime_app._SYNTHETIC_SESSION_PREFIXES` — a new prefix auto-extends the pin | `test_eval_queue.py` (tuple-driven + monkeypatched extra prefix) | 1 |
| 12 | `answer-audit` proposals never auto-accept — BEHAVIORAL: insert, run the real sweep, still pending while a sibling accepts | `test_answer_audit_auto_accept.py` | 2 |
| 13 | Global UTC-day budget across manual+scheduled COMBINED, claimed atomically BEFORE model calls; cloud claim failure fails CLOSED with zero converse calls; race never exceeds cap | `test_budget_claim.py`, `test_audit_scheduler.py` | 1+4 |
| 14 | ONE public entry `request_audit(trigger)` (no count/cap/model/source args — introspection); `_run_audit` sole converse-seam consumer + sole findings writer; trigger provenance-only | `test_audit_scheduler.py` | 4 |
| 15 | Scheduled path PROVEN: registered callback captured, invoked, calls `request_audit(trigger="scheduled")`; flag unset ⇒ no registration; job id / 86400 s / ordering after scheduler start pinned | `test_audit_scheduler.py` | 4 |
| 16 | Worker memory-absence is architectural: import graph of `audit_scheduler` + `audit_core` contains no chat/session/memory module | `test_audit_scheduler.py` (import-graph walk) | 4 |
| 17 | Extraction parity + seam ownership: all pre-existing `test_audit_answer*` pass with ZERO edits; script and core share the SAME converse-seam object post-re-export | existing suite + `test_audit_core_extraction.py` | 1 |
| 18 | Steward decisions/annotations survive recomputation/re-import byte-identical; machine has no code path to write decisions or `status=rejected` | `test_eval_queue.py`, `test_eval_findings.py` | 1 |
| 19 | Run-ALL pin scoped to GOLDEN-SUITE controls only: subset keys still 422 on `/run`, no subset affordance in the golden-run UI — the audit run-now button/route is exempt and distinct | existing `test_eval_routes.py` (unchanged) + `evals-findings.test.tsx` | 2–3 |
| 20 | Each telemetry writer fires the REAL `bump_artifact_usage` with its own ref kind; the status endpoint reads the ledger back | `test_evals_artifact_usage.py` (3 writer spies + read path) | 3 |
| 21 | Queue GET and digest GET perform zero writes (computed-on-read) | `test_eval_queue.py`, `test_eval_findings_routes.py` (spy persistence seams) | 1–2 |
