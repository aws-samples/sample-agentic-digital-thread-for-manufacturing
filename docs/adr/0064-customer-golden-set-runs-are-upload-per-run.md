<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0064: Customer golden-set runs are UPLOAD-PER-RUN — transient, zero server-side persistence

## Status

**Accepted — 2026-08-19.** Written before implementation to pin the contract,
then verified against a deployed environment. Measured facts (the ADR-0062
convention: facts, not claims):

- **Byte-exact sha binding.** The external customer golden set (17 questions,
  17,859 bytes) uploaded through the route reported
  `golden_set_sha` **identical** to `sha256(file bytes)[:12]` computed locally
  (`79af30f7b519`), and `question_count: 17`.
- **Run-ALL held.** All 17 rows present; a subset-shaped query parameter
  (`?count=3`) was refused **422 `unknown_query_params`**.
- **Provenance unmistakable.** The report carried `golden_set: "uploaded"` (the
  literal, never a path or filename), `golden_set_source: "uploaded"`,
  `answer_text_redacted: true`. The history list showed uploaded rows badged
  distinctly; a pre-provenance run renders as unstamped rather than being
  mislabeled.
- **Data boundary held.** **Zero** customer question text appeared anywhere in
  the persisted report (checked against all 17 question texts), and all 17
  `answer_excerpt` values were empty.
- **CLI/upload equivalence.** The packaged demo set (11 questions)
  uploaded through the route scored **11 pass / 0 fail** —
  matching the recorded CLI and packaged-server baseline for the same bytes on
  the same environment, at the same sha. A set that parses server-side runs on
  the CLI by construction, as D2 requires.
- **No residue.** `audit_learning.py --strict` exit 0, learning-health gate
  clean after two uploaded runs.
- **Gates.** the local CI parity run ALL PASSED; unit lane 7,980 passed;
  frontend `tsc` + 1,796 vitest + build green; boundary gate (including the new
  pin (iv)) green with `git status` clean of any JSON.

An externally supplied golden set scores poorly against the demo environment
(in the validating run, 6 of 17 questions passed), because its questions target
the supplying deployment's own systems. That is the expected outcome of running
a set against the wrong environment — and precisely why D3 stamps provenance so
the two can never be compared.

(Numbering note: the plan that proposed this decision called it 0063. That
number was taken on a parallel branch by an unrelated record —
`0063-proposal-precision-and-requirement-promotion.md` — so this decision is
0064. No content changed with the renumber.)

Builds on [ADR-0062](0062-review-queue-promote-to-golden-findings-ledger.md) —
this record supersedes, IN PART and in exactly one direction, the D1
"Evals-tab limitation" that said the tab runs only the packaged demo golden set;
every other ADR-0062 invariant is inherited unchanged, and the repository
boundary it established gets two additional pins here. Also builds on
[ADR-0061](0061-evals-admin-tab-and-answer-auditor.md) (async JobStore-backed
golden runs with a one-run lock; the scoring core in `agents/eval_core.py`;
run-ALL only; the required `run_path` tag; `eval-run-` synthetic containment),
[ADR-0054](0054-measurement-honesty-and-experiment-harness.md) (untrusted text
is bounded and secret-shape-rejected at write; measurement honesty — runs whose
inputs differ are not comparable),
[ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) (computed
verdicts — an LLM never produces one; measured-only usage), and
[ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — no demo or customer literals in `agents/*.py` outside
`agents/demo/`).

## Context

A steward evaluating ADT against their own operation has a golden set of their
own — real questions over their real systems, held in their own private
repository. Two things are true at once today, and they conflict:

- The scoring lane that would tell them whether ADT answers their questions
  correctly is CLI-only: `scripts/eval_answers.py --golden-set <path>`. It needs
  a checkout, a Python environment, and the deployment password. ADR-0061 built
  the Evals tab precisely because "a steward without a checkout cannot see
  evaluation health at all", and then left the one set the steward actually cares
  about outside it.
- Customer data must never enter this repository's git. That is ADR-0062's lead
  constraint, and it is a design problem rather than a discipline problem for
  verified reasons: the neutrality ratchet scans no JSON by design, and
  `.gitignore` is not a boundary (`git add -f` defeats it, and a rename defeats
  it silently). What holds the boundary is a test —
  `tests/unit/test_customer_golden_boundary.py`.

So ADR-0061 deferred "custom golden sets" and ADR-0062 D1 restated the
limitation plainly and said changing it "is a separate ADR — not a follow-up
commit". This is that ADR.

**Why revisiting is safe now, and was not before.** The deferral was not
arbitrary: in the Phase-2 shape, the obvious way to let the tab run a customer
set was to *store* the set, and storing it would have created a durable store of
customer question text inside the application's own tables — exactly the class
ADR-0062 D1 forbids derived stores from holding. What changed is that the
storage step turns out to be unnecessary. `eval_runs.start_run` already loads
the goldens and hands the parsed list into the daemon thread
(`threading.Thread(target=_run_suite, args=(run_id, variant, goldens,
_golden_set_path()))`) — the questions are ALREADY only in process memory during
a run. A path travels alongside them for exactly two reasons: to stamp
`golden_set` and to compute `golden_set_sha` in `_persist_run_results`. Replace
those two uses with a fixed label and a pre-computed sha, and the entire
"where does the customer's set live" problem dissolves: it lives where it
already lived, in the steward's own private repository, and it visits the server
for the length of one run.

The cross-replica question resolves the same way. Status/progress
(`eval-run`) and finished reports (`eval-report`) already ride `JobStore`, so a
poll that lands on another replica still resolves. The golden BYTES are needed
only by the worker thread, which runs in the replica that accepted the POST. No
shared storage is required for any of it.

## Decision

### D1 — Upload-per-run: the golden bytes are transient request/process data, persisted NOWHERE

The steward POSTs their golden file's raw bytes to a distinct route; the server
validates and parses them through the existing `eval_core` loader, holds the
parsed questions **only in the worker thread's locals for the run's lifetime**,
and persists **nothing** of the file: no S3 object, no `JobStore` kind, no
DynamoDB item, no `/tmp` file, no log record, no echo in any response body.

The consequence is stated up front rather than buried: **a re-run requires a
re-upload, and that is the design, not a gap.** The authoritative copy of a
customer golden set is the steward's own private repository — which is exactly
where ADR-0062 D1 puts it — and re-attaching from there is the act that keeps it
authoritative. There is no "save this set" affordance, and adding one requires a
new ADR, not a follow-up commit.

**Rejected: a durable per-deployment store (an S3 object or a `JobStore` kind).**
It buys one file-picker click and costs: a new durable customer-derived data
class holding question text in the application's own table — directly
contradicting ADR-0062 D1 ("derived stores persist *references* … never
values"); a retention horizon plus a steward delete affordance plus a "what is
stored here" read path, none of which exist; ADR-0052 replace-not-accrete
semantics for stored sets; a boundary pin that the store is never exported; a
teardown story proving `cleanup.sh` empties it; and — for the S3 form — a
`stacks/` change, which ADR-0062 explicitly excluded and this record excludes
too. The convenience is also partly illusory: a forgotten set outlives the
engagement, while the steward's private copy does not.

**Rejected: a pointer to an operator-supplied location** (a bucket/prefix the
deployment reads at run time). It needs everything the durable store needs
**plus** cross-account read IAM, a fetch path out of the VPC, and a fetch-time
integrity story — the sha must bind what was *fetched*, not what was *pointed
at* — which introduces a new failure class ("the pointer resolved to something
else today"). Most moving parts, least verifiable, and the data still sits at
rest in a bucket somebody must remember to empty.

### D2 — ONE validator home, and a byte-exact sha, so a server-side set runs on the CLI by construction

The `golden_evals.json` file schema stays the **sole** interchange format;
nothing new is invented, and no second parser exists.

- `eval_core.load_golden_set(path)` splits into `parse_golden_set(payload)` —
  carrying ALL current validation (`version`, non-empty `questions`,
  id/text/expect, duplicate ids, `require_markings`, the golden-markings check) —
  plus a thin `load_golden_set(path)` = read → `json.loads` →
  `parse_golden_set`. The CLI keeps calling `load_golden_set`; the upload route
  calls `parse_golden_set`. A set that parses server-side therefore runs on the
  CLI **by construction**, not by convention, with byte-identical error messages
  from both entry points.
- The upload body is the **raw golden JSON bytes** — not a JSON envelope
  carrying the text as a string, and not multipart — so
  `golden_bytes_sha(raw)` returns exactly what `golden_file_sha(path)` returns
  for the steward's own file, and `golden_file_sha` delegates to
  `golden_bytes_sha` so the two can never diverge. This is the findings-import
  precedent verbatim: the binding is `sha256` over the EXACT bytes, and a
  re-serialized object would not reproduce that digest.
- **Byte-recoverability is satisfied by not storing anything.** The requirement
  that a server-side set stay byte-recoverable so the CLI lane remains
  authoritative is met trivially: nothing is stored, the steward's file *is* the
  recoverable copy, and the run's `golden_set_sha` proves the server scored those
  exact bytes. The steward verifies with
  `python3 -c "from agents import eval_core; print(eval_core.golden_file_sha('<path>'))"`
  and compares against the sha the tab shows.

**Rejected: an envelope carrying the file as a JSON string** (`{"golden_set":
"…"}`) — it reads more conventionally and breaks the byte-exact sha, which is the
one mechanism that lets a steward prove the server scored the file they hold.

### D3 — Run-ALL per set on a DISTINCT route; `/run` stays byte-unchanged; provenance cannot be confused

The ADR-0061 D3 run-ALL rule is inherited without dilution, and it is enforced
structurally rather than by validation: **the upload route has no selection
parameter of any kind.** The only accepted query parameter is `variant`
(explicit allowlist ⇒ 422 `unknown_query_params` on anything else), and the
worker iterates every parsed question in file order, exactly as the packaged run
does. There is no `count`, `category`, `ids`, or `question_ids` to forbid,
because there is nothing to select from.

The existing `POST /api/admin/evals/run` and its `extra="forbid"` body model are
**byte-unchanged** — the ADR-0061 D2 / ADR-0062 D12 bar: `test_eval_routes.py`
passes with zero edits. A second run affordance on a distinct route is the
ADR-0062 D11 precedent (the audit run-now route is a distinct surface, not a
subset run of anything).

| Surface | Method | Input | Success | Refusals (distinct details) | Persists |
|---|---|---|---|---|---|
| `POST /api/admin/evals/run` | POST | `{variant?}`, `extra="forbid"` | 202 `{run_id, status}` | 409 `eval run already active` / `golden set missing or empty`; 422 on any subset key | `eval-run` status (4 h), `eval-report` **30 d**, index |
| `POST /api/admin/evals/run-upload` **(new)** | POST | **raw golden JSON bytes** (`Content-Type: application/json`), ≤ `MAX_GOLDEN_UPLOAD_BYTES`, cap enforced **before** parse; `?variant=<label>` is the ONLY accepted query param | 202 `{run_id, status, golden_set_source:"uploaded", golden_set_sha, question_count}` | 413 `payload_too_large`; 422 `invalid_json` / `invalid_golden_set` (loader message, no content echo) / `too_many_questions` / `unknown_query_params`; 400 `secret_like_content`; 409 `eval run already active` | `eval-run` status (4 h), **redacted** `eval-report` **7 d**, index. **NEVER the uploaded bytes** |
| `GET /api/admin/evals/runs` | GET | — | rows carry `golden_set_source`, `question_count` | — | nothing |
| `GET /api/admin/evals/runs/{id}` | GET | — | report carries `golden_set_source`, `answer_text_redacted?` | 404 | nothing |

Provenance is **additive** (the additive-key discipline `test_eval_repeats.py`
pins) and exists so a customer run can never be mistaken for a demo run in
history:

- `build_eval_report(..., golden_set_source="packaged")` stamps
  `golden_set_source` (`"packaged" | "uploaded"`) at the report top level.
- For an uploaded run, `golden_set` is the **fixed literal `"uploaded"`** — never
  a path, and never a client-supplied filename. A filename like
  `<customer>-plant3-goldens.json` is customer identity, and the current code
  would stamp it straight into a persisted record.
- The bounded index summary gains `golden_set_source` and `question_count`, so
  the history table badges a row without fetching the report.
- **Runs from different sources are never comparable.** The A/B diff already
  sets `comparable` from `golden_set_sha` equality, so a demo↔customer diff is
  already non-comparable; this record pins it explicitly at the source level
  too — a differing `golden_set_source` is never comparable regardless of sha
  (ADR-0054 measurement honesty).

The ONE-run-at-a-time lock is shared across both entry points (the same
`_ACTIVE_KEY` claim), so a demo run and a customer run can never overlap, and the
second one gets the existing distinct 409.

Two inheritances are explicit rather than assumed. **Synthetic containment is
unchanged for uploaded runs** (ADR-0061 D5): every question is asked under an
`eval-run-` prefixed session id — the prefix that sits in BOTH the synthetic and
the automated session-prefix tuples — and every stream call carries
`write_memory=False`, so the system never trains on a customer's test set.
**Packaged-run behavior is unchanged apart from the additive provenance keys**:
exactly one new key on the report (`golden_set_source: "packaged"`) and two on
the index summary (`golden_set_source`, `question_count`). No existing key moves,
changes type, or changes value, and no packaged report ever carries
`answer_text_redacted`.

### D4 — The repository boundary is STRENGTHENED by two new pins; `.gitignore` is still not a boundary

Nothing in this feature can write a file, and that is pinned positively rather
than asserted:

1. **No write seam is reachable from the upload path.** A persistence-seam spy
   test in the ADR-0062 D5 style — spies on `JobStore.__setitem__`, storage
   writes, `tempfile.*`, `open`-for-write in the route and runner module
   namespaces, and the logging tree — asserts the golden bytes appear in **none**
   of them, and that no file is created anywhere under the repository root
   during an uploaded run.
2. **The server has exactly ONE filesystem golden source.** A new pin in
   `test_customer_golden_boundary.py` asserts that `agents/eval_runs.py` and
   `agents/eval_routes.py` contain no `load_golden_set(` / `open(` /
   `read_bytes(` / `read_text(` call with any argument other than the packaged
   default (`eval_core.default_golden_set_path()`), and no `.json` string literal
   reaching a read outside that helper; the upload path reaches
   `parse_golden_set` only. The pin ships with a bites-proof: the same scan over
   a temp copy carrying a planted `load_golden_set(user_supplied_path)` line must
   FAIL.

The existing boundary pins are **untouched and must stay green**: the tracked-JSON
shape scan is what stops an uploaded set from ever being committed here, and it
already catches a renamed file; the sanctioned sample's question-id manifest and
the customer-identity denylist are unaffected by construction, because this
feature adds no tracked JSON at all. `.gitignore` remains not a boundary — this
record does not lean on it for anything.

`scripts/merge_golden_draft.py`'s refusal of any in-repository destination stays
the model for "customer goldens live outside"; the upload route has no
filesystem destination whatsoever, which is the same refusal taken to its limit.

The browser is held to the same posture as the server, and the copy is part of
the decision:

- The attach-and-run affordance is a SECOND, clearly separated control (never a
  subset control on either affordance), `confirm()`-gated like the existing
  golden-run button, and its copy states the data posture in the steward's own
  terms: the file is used for this run only, is never stored on the server and
  never committed to the ADT repository; answer text from the run is not
  persisted; the report keeps verdicts, question ids and failures for 7 days.
- The file bytes go into the request and nowhere else — never into the mutation
  key, the query cache, `localStorage`/`sessionStorage`/IndexedDB, or a second
  request.
- The copy names the steward's own external file, warns that question ids and the
  `variant` label are steward-authored strings that should not carry customer
  identity, tells them how to verify the returned sha against their file, and
  must never suggest committing anything here. A report carrying
  `answer_text_redacted` renders an explicit note rather than an empty excerpt
  block.

### D5 — Data classification is normative: question text nowhere, answer text REDACTED, uploaded reports on the ADR-0062 D6 7-day horizon

A datum belongs to a tier before it belongs to a schema. This table is
normative:

| Datum | Where it MAY live | Retention | Logs / reports / UI | Where it must NEVER live |
|---|---|---|---|---|
| Uploaded golden file bytes (customer question text + expectations) | the HTTP request body; the worker thread's locals | the run's process lifetime | never logged, never echoed in any response, never in a report | JobStore, DynamoDB, S3, `/tmp`, this repo's git, the browser's query cache or web storage |
| `golden_set_sha` (sha256[:12] of those bytes) | run record, report, index summary, UI badge | with the report | yes — it is opaque | — |
| Golden **question ids** | report rows (they are the row key — unavoidable) | 7 d (uploaded runs) | yes, in report + UI | — |
| Golden **question text** | nowhere server-side | — | never | report rows, index, logs |
| **Answer text** produced by an uploaded run (`answer_excerpt`, fabrication-flag excerpts, stream `errors` detail) | nowhere | — | **never persisted**; the report carries `answer_text_redacted: true` | `eval-report` rows |
| Deterministic failure strings (e.g. `answer missing required pattern '<regex>'`) | report rows | 7 d | yes | this repo's git |
| Packaged demo run report (`answer_excerpt` included, as today) | `eval-report` | 30 d, unchanged | yes | — |
| `variant` label | run record + report + index | with the report | yes | — steward text; it is the ONE place a steward could type a customer name, so the UI copy warns and it gets the `untrusted_text` treatment |

Three decisions follow from the table and are part of it:

**Answer text from an uploaded run is NOT persisted.** Today a scored row stamps
a 400-char `answer_excerpt`, and fabrication flags embed up to 120 chars of the
answer clause. For an uploaded run those are real answers over real customer
data; keeping them would make the application's own table a 30-day durable store
of customer operational prose — exactly the class ADR-0062 D6 pushed down to a
short, explicitly labeled tier. So rows are redacted **immediately after
scoring, before they enter the `rows` list**: `answer_excerpt` becomes `""`,
each fabrication flag is truncated to its label with the excerpt dropped, and
any failure entry derived from stream `errors` becomes a fixed
`error event(s) (redacted, n=<count>)`. The report carries
`answer_text_redacted: true`, so it can never be mistaken for a full report.
(The redactor is a pure function returning a NEW dict. It is a distinct concern
from the existing report-shrink path's `answer_excerpts_dropped` size guard —
distinct key, distinct reason; do not conflate them.)

**Deterministic failure strings ARE persisted verbatim.** They are the
diagnostic value of the whole lane, and their content is the steward's own
authored expectations — which the steward already holds externally — not customer
turn text. They live in the customer's own deployment account under a 7-day TTL
and never leave it.

**Uploaded-run reports carry a 7-day TTL**, matching the ADR-0062 D6
customer-derived-detail horizon, written through a second store instance on the
SAME `eval-report` kind (so reads resolve through the existing store unchanged;
`JobStore` writes the `ttl` attribute per item from the instance's
`ttl_seconds`). This is the `eval_findings` per-tier-TTL precedent. Packaged demo
runs keep 30 d.

### D6 — Caps are derived, not guessed, and are enforced before parsing

- `MAX_GOLDEN_UPLOAD_BYTES = 256 * 1024` — the findings-import 512 KiB ingress
  pattern, halved: a 40-question set with expectations is tens of KiB. The cap is
  enforced by a raw `Request` handler in the findings-import idiom — reject an
  oversized declared `Content-Length` with the stream untouched, and abort the
  stream read past the ceiling — because a pydantic body model would run AFTER
  FastAPI parsed the body, which is not a cap at all. Over the cap ⇒ 413
  `payload_too_large`.
- `MAX_UPLOADED_QUESTIONS = 40` — arithmetic from the runner's suite deadline,
  `min(n * 300 + 600, 14400 - 300)`: the deadline stops covering the work past
  n ≈ 45, and 40 questions = 12,600 s = 3.5 h, inside the 4 h status-record
  horizon. Above the cap ⇒ 422 `too_many_questions`, so a run can never silently
  outlive its own status record.
- **Secret-shape rejection over the raw text** via
  `untrusted_text.validate_untrusted(...)` with a pinned reason, and a detail
  that **never echoes the content** ⇒ 400 `secret_like_content`. An uploaded set
  enters the model prompt path, so a pasted credential must not (ADR-0054 D8's
  posture applied to steward-supplied bytes).
- Non-UTF-8 bytes are refused (422 `invalid_json`) rather than decoded lossily,
  because a lossy decode would change the bytes the sha claims to bind.
- Logs carry `reason=`, `sha=`, and `question_count=` only — never a fragment of
  the body. The loader's own error message may name ids and keys; the one message
  that carries a repr of a malformed question is truncated or replaced, so no
  question text reaches a response body or a log record.

## Considered alternatives

- **A durable per-deployment golden store (S3 object or `JobStore` kind).**
  Rejected (D1): buys one file-picker click, costs a new durable
  customer-data class that contradicts ADR-0062 D1, plus a retention/delete
  surface, a teardown proof, and (for S3) the `stacks/` change ADR-0062
  excluded.
- **A pointer to an operator-supplied location.** Rejected (D1): most moving
  parts (cross-account IAM, a VPC egress fetch path, a fetch-time integrity
  story), least verifiable, and the data still sits at rest somewhere someone
  must remember to empty.
- **Extending `POST /run`'s body with an inline golden set instead of a distinct
  route.** Rejected: the body model's `extra="forbid"` pin is the mechanism that
  makes run-ALL structural, and reopening that model to a new key is exactly the
  edit the pin exists to prevent. A distinct route keeps `/run` byte-unchanged
  and keeps `test_eval_routes.py` unedited (the ADR-0061 D2 bar).
- **An envelope carrying the file as a JSON string, or multipart.** Rejected
  (D2): both re-serialize, so `sha256` over the received payload no longer equals
  the sha of the steward's file, and the steward loses the one check that proves
  the server scored what they hold.
- **Keeping customer sets CLI-only (the status quo).** Rejected: it leaves the
  steward who most needs evaluation health — the one without a checkout — unable
  to see it, which is the gap ADR-0061 was written to close.
- **Persisting answer text for uploaded runs "just for debugging".** Rejected
  (D5): deep answer debugging is a CLI-lane job with `scripts/audit_answer.py`
  over a locally held report; the price of that convenience server-side is a
  durable store of customer prose.
- **A UI subset control for large customer sets, with a warning label.**
  Rejected: the same reasoning as ADR-0061 D3 — the cross-system proof is
  suite-level, and a warning label does not make a flipped proof honest. The
  question-count cap is a refusal, not a silent truncation.

## Explicitly out of scope

- Server-side **storage** of a golden set (S3, DynamoDB, `JobStore`) and any
  golden-set editor — that is the whole point of upload-per-run.
- Server-side `--strict`, `--repeats`, or A/B `--previous` for uploaded sets —
  those stay CLI-lane features (ADR-0061's out-of-scope list, otherwise
  unamended).
- Any `stacks/` change, new DynamoDB table, or new bucket.
- Promoting a queue draft directly into an uploaded set inside the browser — the
  draft stays download-only and `scripts/merge_golden_draft.py` stays the only
  landing path (ADR-0062 D5).
- Persisting answer text for customer runs (deliberately dropped — see D5).

## Consequences

- **Positive:** a steward without a checkout can score their OWN golden set
  through the same computed lane, in the same history table, with honest
  provenance badges (`run_path` from ADR-0061 D4 plus `golden_set_source` here)
  and a sha they can verify against their own file — while the set itself never
  acquires a home in this application or this repository. The boundary ends the
  change stronger than it started: two new pins, and no new tracked JSON.
- **Negative / risks:**
  - **A re-run needs a re-upload.** Deliberate (D1). The steward's private
    repository stays the authoritative home.
  - **Question ids are persisted** — they are the report row key, and there is no
    version of this feature where they are not. The UI copy tells the steward
    that ids are steward-authored strings and should not carry customer
    identity.
  - **Uploaded reports lose answer prose**, so deep answer debugging stays a
    CLI-lane job (`scripts/audit_answer.py`) over a report the steward holds
    locally.
  - **Trace spans.** The question text is *asked*, so it transits the
    orchestrator exactly as an interactive turn does. Learning writers and
    AgentCore Memory are already suppressed (`write_memory=False`, and the
    `eval-run-` prefix sits in both the synthetic and automated session-prefix
    tuples — proven per run by `audit_learning.py --strict`), but on a deployment
    launched with `--observability`, trace spans may capture prompt content in
    CloudWatch. That is the same class as any real turn on that deployment, not
    a new one — and it is the reason this is recorded rather than claimed away.
  - **The 7-day TTL is a local no-op without a configured table.** `JobStore` is
    fail-soft and per-process when `ONBOARDING_SESSIONS_TABLE` is absent (offline
    unit lane, local dev), so the TTL attribute is never written and the report
    lives in process memory only. Pre-existing behavior for EVERY JobStore
    kind, recorded here rather than fixed here.
  - **BOM / non-UTF-8 files diverge from a naive local hash.** A file with a
    UTF-8 BOM hashes differently than the same file without it; the route refuses
    non-UTF-8 outright (422) and the tab shows the computed sha so the steward
    can compare rather than guess.
- **Verification:** the pinned-by table below, plus the deployed closure pass
  enumerated in *Status*.

## Pinned-by

| # | Invariant | Test | Wave |
|---|---|---|---|
| 1 | An uploaded golden set is persisted NOWHERE: no JobStore/DDB/S3/tmp/file write, no log record, no response echo of its bytes | `test_eval_upload_routes.py`, `test_eval_runs_uploaded.py` (persistence-seam spies + `caplog` + response scan with a canary) | 2–3 |
| 2 | The server has exactly ONE filesystem golden source (`eval_core.default_golden_set_path()`); the upload path reaches only `parse_golden_set` | `test_customer_golden_boundary.py` new pin + planted-line fixture | 1 |
| 3 | No tracked golden-shaped JSON outside the ONE sanctioned demo path; manifest + denylist pins unchanged | `test_customer_golden_boundary.py` existing pins, **unedited** | 1 |
| 4 | ONE validator home: `load_golden_set` == read + `parse_golden_set`, byte-identical errors; a set that parses server-side runs on the CLI | `test_eval_core_upload_parse.py` | 1 |
| 5 | `golden_bytes_sha(bytes) == golden_file_sha(path)` for the same file — the sha binds the EXACT bytes the steward holds | `test_eval_core_upload_parse.py` | 1 |
| 6 | Run-ALL per set: the upload surface has no selection parameter (`variant` is the only query param; anything else 422) and the worker runs every parsed question; `/run`'s `extra="forbid"` pin is unchanged | `test_eval_upload_routes.py` + `test_eval_routes.py` (unedited) | 3 |
| 7 | Provenance cannot be confused: report + index carry `golden_set_source`; an uploaded report's `golden_set` is the literal `"uploaded"` (never a path or filename); a diff across differing sources is never `comparable` | `test_eval_runs_uploaded.py`, `test_eval_answers_report.py` (unedited), `evals-logic.test.ts` | 2–4 |
| 8 | Uploaded-run reports persist NO answer-derived text (`answer_excerpt == ""`, fabrication excerpts stripped, `errors` detail redacted) and carry `answer_text_redacted: true`; verdicts/failures/ids/metrics survive intact | `test_eval_core_upload_parse.py` (pure redactor), `test_eval_runs_uploaded.py` | 1–2 |
| 9 | Uploaded-run reports carry the 7 d TTL attribute; packaged runs keep 30 d | `test_eval_runs_uploaded.py` (fake table, `ttl` attribute) | 2 |
| 10 | Ingress cap enforced BEFORE parse (oversized `Content-Length` ⇒ 413 with the stream untouched; a lying/absent header still capped mid-stream) | `test_eval_upload_routes.py` | 3 |
| 11 | Question-count cap ⇒ 422 `too_many_questions` (a run can never outlive its own status record) | `test_eval_upload_routes.py` | 3 |
| 12 | Secret-shaped upload rejected with a pinned reason; the detail and the logs never echo content | `test_eval_upload_routes.py` | 3 |
| 13 | Synthetic containment unchanged for uploaded runs: `eval-run-` session prefix on every question and `write_memory=False` on every seam call | `test_eval_runs_uploaded.py` + `test_eval_run_synthetic_markers.py` (unedited) | 2 |
| 14 | ONE run at a time across BOTH entry points (a customer run and a demo run cannot overlap) — the shared `_ACTIVE_KEY` claim, distinct 409 detail | `test_eval_runs_uploaded.py` | 2 |
| 15 | Packaged-run behavior is byte-unchanged (report + index row equal a pinned dict; `test_eval_runs.py` / `test_eval_run_path.py` / `test_eval_repeats.py` unedited) | existing suite + `test_eval_runs_uploaded.py` | 2 |
| 16 | UI: no subset control on either run affordance; file bytes never enter storage/query cache; confirm-gate + data-posture copy present; redacted reports render the note | `EvalsTab.test.tsx`, `evals-logic.test.ts` | 4 |

All test paths are relative to `agentic_digital_thread/tests/unit/` except the
two frontend specs, which live under
`agentic_digital_thread/frontend/src/test/`.
