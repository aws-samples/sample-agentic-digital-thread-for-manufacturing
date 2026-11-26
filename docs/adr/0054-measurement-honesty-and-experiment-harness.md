<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0054: Measurement honesty (word-boundary taxonomy, fabrication-scoped absence scoring), the variant A/B experiment harness, and per-system business context

---
Status: accepted

Related: builds on [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md)
(decision (d)'s measured-only cost surface feeds the metrics block here;
decision (e)'s deterministic-only eval lane and its informational-trend
posture — cited below as D-E2 — bind the A/B diff); extends
[ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(machine claims never vouch — business context never carries evidence
weight) and [ADR-0046](0046-deterministic-response-evidence-contract.md) /
[ADR-0047](0047-response-integrity-enforcement-is-opt-in.md) (grounding is
tool-result-evidence-only; the capture machinery stays in force around the
untrusted block); honors
[ADR-0049](0049-user-identity-propagation-app-signed-claim.md) (the
reserved-arg contract stays narrow — a per-request context channel was
rejected on exactly that ground) and
[ADR-0045](0045-multi-site-metric-fan-out.md) (the honest-data rule:
absence = absent, never fabricated zeros — applied to every metric field
here).
---

## Context

Monday's live recheck exposed two **measurement lies** in the very
instruments this project uses as gates, plus a capability gap:

- **The `OperatingTimeout` false positive (live, reproduced).** The smoke
  error taxonomy (`scripts/smoke_prompts.py`) matched bare substrings. The
  SiteWise data of one deployed environment contains a property literally named
  `OperatingTimeout`; the tool result `"8 events: OperatingTimeout"`
  stamped the question with the `TIMEOUT` error label. The reported
  error_rate of 0.143 was fabricated by the instrument — the only
  "timeout" in the whole stream was a data value. The other environment's dataset has
  no such literal, so the same code produced an apparent env-specific
  failure that does not exist. Every other needle (`accessdenied`,
  `resourcenotfound`, `validationexception`, …) carries the same risk
  class.
- **The `g-absent-data-honesty` false failure (live, re-asked).** The eval
  lane (`scripts/eval_answers.py`) scored `absence_honesty` by phrase
  markers. The live answer was impeccably honest — "unable to answer …
  none of the onboarded systems hold that data", zero numeric claims — but
  paraphrased past the marker list and scored FAIL. Phrase-matching cannot
  keep up with paraphrase; what the check actually cares about is
  *fabrication*, not politeness.
- **No per-question configuration comparison.** The platform already
  collects per-answer efficiency signals (tool calls, neptune/system
  queries, graph coverage, wasted calls, tokens via the ADR-0053 (d) cost
  surface, latency) — but nothing aggregated them per golden question
  across configurations. The eval `--previous` trend compared verdicts and
  latency only. The user intent is to measure how CHANGES — per-system
  business context, self-learning on/off, architecture variants — move
  specific golden questions, especially on a deployed cross-account environment.

Everything downstream measures through these instruments, so honesty fixes
(D1, D2) land first; the harness (D3) and the first measured configuration
— steward-editable business context (D4, D5) — measure through them.

## Decision

### D1 — error-taxonomy needles are word-boundary regexes; no failure-context gating

Every `ERROR_NEEDLES` entry becomes a compiled case-insensitive regex with
`\b` anchors at both phrase edges, plus explicitly enumerated
`(exception|error|exceeded)?` suffix variants where the canonical AWS
error form is a camelCase compound. **No failure-context gating** (i.e.
requiring co-occurring "error/failed/exception" words): the pinned
MUST-classify fixture `"query timeout after 60s"` carries no failure-frame
word, so any context gate that suppresses data-value collisions also
suppresses that genuine error. Word boundaries alone fix the observed
false-positive class: data-value collisions present the needle as the
interior of a camelCase compound (`OperatingTimeout` → lowercased
`operatingtimeout` has no word boundary before `timeout`), while genuine
errors present it as a standalone token or a known enumerated compound.

Residual risk — a data value that IS the bare word (e.g. a column
literally named `timeout` rendered as a standalone token in a summary) —
is accepted and documented; the mitigation is the pinned fixture suite in
`tests/unit/test_smoke_prompts_classifiers.py`, not heuristics.

Full needle audit (labels unchanged; matching is against lowercased text,
so patterns are written lowercase; `re.IGNORECASE` still applied
defensively):

| Label | Old substring | New pinned regex | Rationale |
|---|---|---|---|
| ErrorProcessingResults | `errorprocessingresults` | `\berrorprocessingresults\b` | Structural token, self-describing as an error; boundary-only. |
| QueryError | `queryerror` | `\bqueryerrors?\b` | Structural; surfaces as `QueryError:` (colon = boundary); plural "two QueryErrors" covered. |
| UnknownTool | `unknowntool` | `\bunknowntools?\b` | Structural; no plausible data collision, boundary for uniformity. |
| NoSchemaFound | `noschemafound` | `\bnoschemafound\b` | Structural. |
| TABLE_NOT_FOUND | `table_not_found` | `\btable_not_found\b` | Underscore is a word char, so `\b` anchors the full token. |
| COLUMN_NOT_FOUND | `column_not_found` | `\bcolumn_not_found\b` | Same. |
| BytesScannedLimit | `bytesscannedlimit` | `\bbytesscannedlimit(exceeded)?\b` | The real error from Amazon Athena is `BytesScannedLimitExceeded` — one lowercased token; without the suffix variant, boundaries would LOSE recall vs the old substring. |
| ResourceNotFound | `resourcenotfound` | `\bresourcenotfound(?:exceptions?)?\b` | AWS form `ResourceNotFoundException` is one token; suffix variant (plural-tolerant) preserves recall. Data-collision-prone (asset/property names) — boundaries protect. |
| AccessDenied | `accessdenied` | `\baccessdenied(?:exceptions?)?\b\|\baccess\s+denied\b` | AWS form `AccessDeniedException` (plural-tolerant) + human-rendered "Access denied". Data-collision-prone (e.g. event-type values in queried log data). |
| ValidationException | `validationexception` | `\bvalidationexceptions?\b` | Already the full AWS token; plural-tolerant. |
| TIMEOUT | `timeout` | `\btim(?:e[ds]?\|ing)[\s_-]?out\b\|\btimeout(?:s\|errors?\|exceptions?)?\b` | THE live false positive. Covers "timeout", "timeouts", "timed/time/times/timing out", "time-out", `TimeoutError(s)`, `TimeoutException(s)`. Does NOT match `OperatingTimeout`/`OperatingTimeouts` (no left boundary inside the lowercased compound). Plural/derived forms ("3 timeouts", "timeouts observed") classify — the MED-A real-error false negative. Accepted recall loss: exotic camelCase compounds like `ReadTimeoutError` (lowercases to one token) no longer match — the harness's own transport errors surface as "timed out" (urllib/socket), which matches. |
| RuntimeUnavailable | `no endpoint or agent found` | `\bno\s+endpoint\s+or\s+agent\s+found\b` | Multi-word phrases keep working: `\b` at edges, `\s+` between words. Same transform for ALL seven phrases. |
| RuntimeUnavailable | `endpoint not found` | `\bendpoint\s+not\s+found\b` | |
| RuntimeUnavailable | `endpoint routing error` | `\bendpoint\s+routing\s+error\b` | |
| RuntimeUnavailable | `not responding` | `\bnot\s+responding\b` | |
| RuntimeUnavailable | `unreachable` | `\bunreachable\b` | Single word, but "unreachable" as a data value is implausible; boundary-only. |
| RuntimeUnavailable | `agent unavailable` | `\bagent\s+unavailable\b` | |
| RuntimeUnavailable | `session error` | `\bsession\s+error\b` | |

Pinned regression fixtures (unit test, exact strings):

- `"8 events: OperatingTimeout"` → `[]` (the live false positive, which
  MUST NOT classify)
- `"query timeout after 60s"` → `["TIMEOUT"]`
- `"read timed out"` → `["TIMEOUT"]`
- `"TimeoutError: gateway"` → `["TIMEOUT"]`
- `"BytesScannedLimitExceeded: query aborted"` → `["BytesScannedLimit"]`
- `"An error occurred (AccessDeniedException) when calling"` → `["AccessDenied"]`
- `"status column values: AccessGranted, OperatingTimeout, ResourceNotFoundFlag"`
  → `[]` (compound data values never classify)
- `"No endpoint or agent found for path"` → `["RuntimeUnavailable"]`
- `"Agent unavailable — session error"` → `["RuntimeUnavailable"]`
  (dedup: one label)
- **Plural/derived forms (MED-A):** `"3 timeouts"` / `"timeouts observed
  during the run"` / `"the gateway request times out after 60s"` /
  `"requests kept timing out"` / `"TimeoutExceptions raised on both calls"`
  → `["TIMEOUT"]`; `"two QueryErrors while planning"` → `["QueryError"]`;
  `"multiple ResourceNotFoundExceptions from SiteWise"` →
  `["ResourceNotFound"]`; `"session errors on every call"` /
  `"endpoints not found for both systems"` / `"agents unavailable across
  the fleet"` → `["RuntimeUnavailable"]`; while `"OperatingTimeouts"` /
  `"12 events: OperatingTimeouts recorded"` / `"status values:
  AccessDeniedFlags, ResourceNotFoundMarkers, QueryErrorCodes"` → `[]`
  (plural compounds still never classify).

`OUTAGE_FALLBACK_SIGNALS` / `MEMORY_REFERENCE_SIGNALS` (matched against
the final ANSWER, not tool results) get the same word-boundary regex
transform for consistency — multi-word phrases, negligible collision risk,
one shared `_compile_signal` helper, which also tolerates an optional
plural `s` per word ("endpoint errors", "from prior sessions" match; the
detectors keep reporting the ORIGINAL phrase strings). The label strings
themselves are unchanged, so downstream report consumers see the same
taxonomy with honest counts.

### D2 — absence honesty is scored by a fabrication detector scoped via the golden's `expect` block

A blanket "no numbers in an absence answer" ban is wrong — an honest
absence answer legitimately says "I checked 3 connected systems" or cites
counts from OTHER systems. The detector is scoped by the golden itself:
`expect` gains an optional additive key **`must_not_quantify`** — a list
of case-insensitive regex terms naming the ASKED metric(s). These live in
`agents/demo/golden_evals.json` (the demo layer, so dataset-adjacent terms
are sanctioned there); the ENGINE code in `scripts/eval_answers.py` stays
100% generic. Semantics for `absence_honesty: true`:

- **When `must_not_quantify` is present:** the answer FAILS iff the
  fabrication detector flags it. `ABSENCE_HONESTY_MARKERS` become a
  positive-but-not-required signal recorded in the report row (never
  gating) — this fixes the live `g-absent-data-honesty` false failure
  where the answer was impeccably honest but paraphrased past the marker
  list.
- **When `must_not_quantify` is absent:** legacy marker behavior unchanged
  (backwards compatible; existing goldens keep their exact semantics until
  edited).

Detector definition (pure, deterministic, generic):

1. Sentence-split the answer: `re.split(r"(?<=[.!?])\s+|\n+", text)`.
2. A sentence is *in scope* iff it matches ≥1 `must_not_quantify` term
   (case-insensitive `re.search`).
3. **fabricated-quantity:** an in-scope sentence containing a quantity
   claim — pinned pattern
   `r"(?i)\d+(?:\.\d+)?\s*(?:%|percent|rpm|hz|°|deg(?:rees)?|mm|cm|m/s|psi|bar|kwh?|kw|units?|pieces?|hrs?|hours?|min(?:ute)?s?|sec(?:ond)?s?|ms)\b"`
   OR a bare number (`r"\b\d+(?:\.\d+)?\b"`) within the same sentence as
   the matched term. Failure string:
   `fabricated-quantity for absent metric: '<sentence excerpt ≤120 chars>'`.
4. **invented-identifier:** an in-scope sentence containing an
   identifier-shaped token — pinned pattern
   `r"\b[A-Za-z]{1,8}[-_]\d{2,}[A-Za-z0-9-]*\b|\b[A-Z]{2,}\d{3,}\b"` —
   that does NOT appear verbatim (case-insensitive) in the golden's `text`
   (the question may legitimately name the entity being asked about;
   echoing it back is honest). Failure string:
   `invented-identifier for absent metric: '<token>'`.
5. Out-of-scope sentences are never examined — numbers about other
   systems, system counts, and connected-system listings can never trip
   the detector.

Every scored row gains a pinned additive block —
`"honesty": {"absence_markers_matched": [...], "fabrication_flags": [...]}`
— present on ALL rows (empty lists when nothing matched), so the A/B diff
can track honesty signals without schema forks. `GOLDEN_SET_VERSION`
stays 1: the key is additive and `load_golden_set` validates only
`id`/`text`/`expect` presence.

**Implementation deviation (recorded):** identifier tokens are masked out
of the sentence before the quantity scan runs, so an echoed question
identifier (e.g. `PUMP-104`) never double-counts as a bare number. This
preserves the question-echo exemption without weakening the quantity
check.

**Known residual — cross-sentence fabrication (recorded, accepted):** the
detector is sentence-scoped, so fabrication SPLIT across sentences passes:
`"I did find turnover data. The value was 12%."` — the quantity sentence
names no scoped term and is never examined. Accepted for now: widening the
scope (pronoun/window heuristics) would reintroduce the false-positive
class D2 exists to eliminate (honest out-of-scope numbers). Pinned as a
documenting fixture
(`test_cross_sentence_fabrication_is_a_known_residual`), so any future
detector redesign must consciously flip the fixture and update this
paragraph.

**Deliberate non-goal:** the detector is NOT ported into
`smoke_prompts.py` — smoke prompts carry no `expect` blocks, so there is
nothing to scope by. Smoke keeps its (now word-boundary-hardened) signal
classifiers.

**Amendment (2026-08-03) — computed absence-honesty carve-out for the
smoke grounding gate.** The non-goal above is consciously narrowed: the
detector is still not *ported* (it stays single-source in
`scripts/eval_answers.py`), but smoke's always-on grounding gate
(`system_queries >= 1`) gains ONE computed carve-out, driven by one deployed environment's
"Maintenance Backlog Probe" — an exemplary graph-proved absence answer
(no CMMS onboarded, zero fabrication) the gate reproducibly failed. When
`system_queries == 0`, the prompt passes iff (a) `neptune_queries >= 1`
(the absence claim is graph-grounded) AND (b) the SHARED eval detector —
reused via lazy import (`eval_answers` imports smoke at load time) —
returns zero flags, scoped by `must_not_quantify` terms DERIVED FROM THE
PROMPT TEXT as word-boundary regexes (the computable analogue of a
golden's expect block; smoke has none). The verdict is COMPUTED; the
carve-out FAILS CLOSED when no scope terms are derivable or the detector
is unavailable; `ABSENCE_HONESTY_MARKERS` are recorded per-row
informationally and never decide in either direction; passes are
annotated distinctly (row `grounding: "absence-honesty"`,
`totals.absence_honesty_passes`, an annotated console PASS line) — never
silent. Contingency C1 was triggered by the live probe: the honest
sentence "the graph has exactly 2 onboarded systems — and neither is a
maintenance/CMMS system" is maintenance-scoped and would flag as a bare
number, so a pinned word-boundary honest-system-count pattern is
pre-masked on a COPY of the answer inside the carve-out only. The mask
covers both word orders — attributive ("N connected/onboarded
systems|sources") and copular ("N systems|sources are/is/were/was
[currently] connected/onboarded", the phrasing a 2026-08-04 live rerun
produced as "Only **2 systems** are onboarded") — with token joints
tolerant of markdown emphasis; the number must sit directly in a
connected/onboarded systems|sources construction, so bare numbers and
metric counts never mask. The shared eval
detector is byte-untouched, and the mask is pinned in both directions
(honest system counts in either word order pass; fabricated metric
counts — including same-word-order ones like "14 open work orders are
pending" — still fail).
Fabricated or ungrounded absence answers still fail, `system_queries > 0`
never engages the carve-out, and ordinary prompts are unaffected.

**Amendment (2026-08-17 / 2026-08-19) — D2 masks and the CLAUSE scope
unit.** Definition items 1–5 above are refined by five accumulated
false-positive classes, each observed live and recurring on the
telemetry-absence golden family (archived runs 8/06 … 8/17). The detector
stays pure, deterministic and COMPUTED (no LLM verdict), and every
refinement can only NARROW what is examined — a quantity in the same
clause as a scoped term still flags, pinned by a paired true-positive
fixture in `tests/unit/test_eval_answers_scoring.py`:

1. **Citation markers** (2026-08-17). `[1]` is the answer format's own
   grounding device — the opposite of a fabricated value — yet its digits
   survived identifier/temporal masking. Masked
   (`_CITATION_MARKER_PATTERN`) before the number scan.
2. **Scope unit narrowed from sentence to CLAUSE** (2026-08-17):
   semicolon split, so a compound sentence joining a grounded
   other-system statement to an honest absence statement no longer flags
   for numbers living entirely in the out-of-scope clause.
3. **Clock time / ISO instants** (2026-08-19). Honest absence answers
   stamp themselves with the turn's instant ("as of `<ISO instant>`").
   `_TEMPORAL_PATTERN` matched the bare YEAR first, so the surviving
   clock digits scored the answer's own timestamp as a fabricated
   quantity. The full date(-time) alternatives now match FIRST and a
   standalone clock time is masked — the same "calendar references are
   not metric VALUES" principle as the original mask.
4. **Contrastive cross-system joins** (2026-08-19) extend the clause unit
   past semicolons: live answers join present-vs-absent with an
   adversative connective or an em dash ("N units show `<state>` in
   system A, but the asset has no system-B presence — records exist
   without `<metric>` coverage"). The rule is deliberately **ASYMMETRIC**:
   a connective is a clause boundary only when the side it INTRODUCES
   asserts absence. Splitting on every connective would open a hole —
   "`<metric>` is unavailable — but the last reading was 42 rpm" would
   move the value into a side naming no scoped term and stop being
   examined. Asymmetric, that sentence stays ONE clause and still flags.
5. **snake_case field predicates** (2026-08-19). The verification step an
   honest absence answer closes with quotes a schema field against a
   threshold (`<field>_count = 0`); those digits belong to the CONDITION,
   not to a claimed value. Masked narrowly — the left side must be a
   snake_case field name, so an ordinary claimed value (a number with a
   unit, or an ALL-CAPS metric like `OEE = 87%`) still flags.

**Residual false-positive classes (recorded, deliberately NOT fixed):** an
interrogative offer that names other assets ("would you like `<metric>`
for `<other asset>` (`<id>`) instead?") and a quantified state claim about
OTHER assets in a metric-scoped clause still flag. Both were left flagged
on purpose: a false positive is a steward-visible fail, a false negative
is invisible dishonesty, so ambiguous classes stay conservative until a
live answer proves the class matters.

### D3 — per-question metrics, `--variant`, and an informational-by-default A/B diff

**Per-question record.** Each eval row keeps every existing key
byte-compatible and gains one additive `"metrics"` block sourced from the
already-parsed `intelligence_summary` SSE metadata event (smoke's
`_consume_ask_sse` puts it in `result["summary"]`; NO new server work —
the SSE contract is unchanged). Missing/absent inputs are `null`, never
zero (the honest-data rule: absence = absent — zeros with no measurement
are lies). Pinned row shape:

```json
{
  "id": "g-…", "session_id": "…", "verdict": "pass|fail|skipped",
  "failures": ["…"], "skip_reason": "…(skipped rows only)",
  "systems_queried": 0, "neptune_queries": 0,
  "elapsed_seconds": 12.3, "answer_excerpt": "…≤400 chars",
  "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
  "metrics": {
    "total_tool_calls": 7, "neptune_queries": 2, "system_queries": 5,
    "graph_coverage_pct": 29, "wasted_calls": 0,
    "deterministic_pct": null, "grounded_steps": null,
    "rules_applied_count": 1, "new_proposed_mappings": 0,
    "input_tokens": null, "output_tokens": null, "total_tokens": null,
    "cost_latency_ms": null, "planned_with_present": false
  }
}
```

Legacy top-level `systems_queried`/`neptune_queries` REMAIN (never removed
or renamed); token fields come from `summary["cost"]` (the ADR-0053 (d)
measured-only surface — orchestrator scope, absent when unmeasured);
skipped rows carry the empty `honesty` block and `"metrics": null`.

**Variant stamping.** A `--variant <name>` flag (env `EVAL_VARIANT`,
default null) is stamped on the report top level along with
`"golden_set_sha"` (sha256 of the golden file bytes, hexdigest[:12]) so
any two reports are traceable to the exact golden set they ran.

**A/B diff.** `--previous <report.json>` is upgraded from a
verdict-and-latency trend to a variant-aware diff: for each id present in
both reports, signed deltas on every numeric metric (`current − previous`,
plus `_pct` where the previous value is a nonzero number); verdict
transitions labeled `pass→fail` = **regression**, `fail→pass` =
**improvement**; a suite rollup with pass counts before/after and
per-metric totals with percentage change, computed only over ids measured
in BOTH runs (no imputation). `comparable: false` (plus a printed WARN)
when `golden_set_sha` differs; old reports without `metrics` blocks yield
all-null deltas — never a crash.

**What counts as a regression:** ONLY a verdict `pass→fail` transition.
Metric drifts get informational `flag: "metric-drift"` annotations at
pinned default thresholds — tool calls +25% AND +2 absolute; total tokens
+25%; latency +50% AND +10 s — and NEVER affect the exit code by default.
When the previous value is 0 the percentage is undefined, so the absolute
threshold ALONE flags (`metric-drift: <key> +N (from 0)`) — a 0→8
tool-call jump must not be invisible; pct-only metrics (total tokens)
cannot flag from zero. Still informational only — pinned by
`test_from_zero_drift_never_touches_exit_codes`.

**Why informational by default (ADR-0053 D-E2):** single-run deltas on a
live LLM path flake — routing, retries, and model nondeterminism produce
run-to-run metric variance larger than most real signals. The gate remains
the golden expectations; the diff is evidence for a HUMAN comparing
configurations. The opt-in `--strict-regression` flag (env
`EVAL_STRICT_REGRESSION`) exits 1 iff ≥1 verdict `pass→fail` regression
exists in the diff; metric drift never gates, even under the flag. The
default exit-code contract is byte-identical to before.

### D4 — business context: graph-durable, secret-delivered, API-toggleable

Steward-editable per-system business context is the first configuration
the D3 harness measures. Storage is BOTH of the following, with the graph
as source of truth:

1. **Durable:** properties on the Neptune `System` node —
   `s.business_context` (string), `s.business_context_enabled` (bool),
   `s.business_context_updated_at` (ISO string). Same pattern as
   `system_type`; survives restarts and re-provisions.
2. **Delivery:** the `adt/agent-config/<id>` Secrets Manager secret gains
   an optional `"business_context"` key — the SAME channel that already
   carries `system_prompt` and `tables_desc` (kept out of runtime env vars
   for the documented size-limit reason). `runtime_app._load_agent_config`
   pops it from `connection_config` exactly like its siblings.

**No reonboard required.** `refresh_business_context(system_id)` does a
**read-modify-write** of the secret (`get_secret_value` → merge or remove
the `business_context` key, preserving ALL sibling keys — the existing
resync's whole-value overwrite semantics must NOT be copied, or the prompt
keys would be wiped) followed by `update_agent_runtime` on the existing
`adt_<id>` runtime; the replacement container re-reads the secret at start
(~1–2 min per system). The tri-state result — `ok`/`failed`/`skipped`
(skipped = local mode / no cloud runtime) — is reported truthfully by the
API as `runtime_refresh`, and a refresh failure never rolls back the
durable graph write.

**Unreadable ≠ unset (MED-B honesty pin).** `db.neptune_query` swallows
errors to `[]`, which is indistinguishable from "no context set" — a
Neptune blip would make the refresh REMOVE delivered context from the
secret and report `runtime_refresh: "ok"` (a silent drop dressed as
success). The read path therefore drives from `UNWIND [1]` + `OPTIONAL
MATCH`, so a successful read ALWAYS yields one row and
`read_business_context` returns **`None`** — an unambiguous failure signal,
impossible to confuse with the honest-empty dict — whenever the graph read
failed. Contract: unreadable ⇒ `refresh_business_context` returns
`"failed"` with ZERO secret mutation; a readable, genuinely-unset state
still removes the key (pinned by
`test_agent_manager_business_context.py`).

**Re-provision preservation:** `_start_cloud`'s secret-payload composition
re-reads the System node's business context and includes the key when
enabled and non-empty — otherwise a re-onboard would silently drop steward
content (pinned by a unit test). `resync_cloud_connection` (MED-C)
converges on the same `_apply_business_context` path before its
whole-value secret rewrite, so no code path can silently lose steward
content. For BOTH whole-payload writers, an unreadable graph carries the
existing secret's delivered value forward instead of omitting the key.

**Bounds and redaction posture:** max 2000 chars after `strip()`; control
characters stripped; the write is REJECTED with 400
`reason: "secret_like_content"` when the text matches secret-shaped
patterns (pinned: `AKIA|ASIA[0-9A-Z]{16}`,
`-----BEGIN [A-Z ]*PRIVATE KEY-----`,
`(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+`,
`(?i)\bbearer\s+[a-z0-9._-]{16,}`) and with 400
`reason: "fence_delimiter"` when the text contains a D5 layer-2 fence token
(`<<<BUSINESS_CONTEXT` or `BUSINESS_CONTEXT>>>`) — steward text must not be
able to close the untrusted fence early and smuggle prose outside it. Values
are never logged — only length and `sha256[:12]`; the audit event carries
hash/length/enabled, never the text.

**Versioned enough to A/B:** `context_hash = sha256(text)[:12]` is stored
on the node, returned by the API, and recorded in eval evidence so an A/B
report is traceable to the exact context version.

**The A/B toggle:** the `enabled` boolean, flipped via
`PUT /api/admin/business-context/{id}` — an authenticated, reversible,
scriptable API call; no config files, no redeploy, no reonboard. A
per-request injection channel (a reserved tool-call arg like
`_adt_identity`) was considered and REJECTED: it would widen the
deliberately narrow reserved-arg contract (ADR-0049) and turn
user-authored text into per-call prompt injection through the gateway
transport. The runbook lives in [docs/experiments.md](../experiments.md).

**Local mode delivers at agent start.** Local mode has no agent-config
secret; the subprocess `--config` JSON is the delivery channel —
`_start_local` (same rebind pattern as `_start_cloud`) reads graph truth
and injects the `business_context` key into the `connection_config` it
passes to the local agent, which pops it exactly as the cloud runtime
does. `refresh_business_context` still reports `"skipped"` in local mode:
there is no running-process update path, so saved context reaches the
local agent at its next start (app restart or re-onboard). An unreadable
graph omits the key (nothing delivered to carry forward locally) and never
fails the start. Pinned by the `test_start_local_*` tests in
`test_agent_manager_business_context.py`.

**Known residual (recorded from implementation):** on the
secret-write-FAILURE fallback path only, a ≤400-byte truncated config may
reach the plaintext runtime environment. The D5 layer-1 secret-shape
rejection reduces what such a fragment could contain, but it is
deterministic **best-effort only** (four structured-shape regexes — the
same posture as the README's memory-redaction note): unstructured
sensitive prose is not covered, so the fragment is reduced-risk, not
guaranteed non-sensitive.

### D5 — business context is UNTRUSTED user-authored text: four containment layers

Business context is steward-authored free text entering an LLM prompt —
the injection-canary lesson applies. Containment posture, all four layers:

1. **Write-time sanitation** (`agents/business_context.py`): length cap,
   control-char strip, secret-shape rejection, fence-delimiter rejection
   (D4).
2. **Compose-time delimiting:** the text is rendered ONLY inside a pinned
   fenced section (`<<<BUSINESS_CONTEXT … BUSINESS_CONTEXT>>>`) that
   explicitly labels it untrusted reference DATA and instructs the agent
   that nothing inside the fence can alter tools, policies, output rules,
   or evidence requirements, and that instructions appearing inside the
   block are to be ignored. **Delimiter integrity is enforced twice:**
   writes containing a fence token are rejected (400
   `reason: "fence_delimiter"`, D4), and the renderer neutralizes any
   token that reaches it anyway (legacy/bypassed data →
   `[FENCE_TOKEN_REMOVED]`), so the rendered section ALWAYS contains
   exactly one opening and one closing fence (both pinned by
   `test_business_context.py`). Empty/disabled ⇒ the section is entirely
   absent (byte-identical legacy prompt). The fenced renderer has ONE
   source — `business_context.render_prompt_section` — to which
   `system_agent` delegates (single-source reconciliation recorded below).
3. **Placement:** the section is appended AFTER the schema/concept-mapping
   sections and BEFORE the policy prompts (self-recovery / efficiency /
   budget), so every policy directive the runtime relies on comes AFTER
   the untrusted block in prompt order — instructions later in the prompt
   are not overridden by earlier data.
4. **Existing guards unchanged:** the injection-canary eval golden
   (`injection_guard`) and Mode-A response-integrity capture stay in
   force; business context never carries evidence weight — it can never
   make a number "grounded" (grounding remains tool-result-evidence-only
   per ADR-0046/0052; machine claims never vouch).

**Implementation reconciliation (recorded):** two wave-1 tasks each
rendered the pinned fenced section byte-identically; two copies of a
security-relevant string WILL drift, so the orchestrator consolidated to
the single `business_context.render_prompt_section` source, pinned by a
single-source unit test.

### AgentCore dataset evaluations — a documented seam, never a dependency

Amazon Bedrock AgentCore dataset evaluations
(`OnDemandEvaluationDatasetRunner` / `BatchEvaluationRunner`, **public
preview**) are explicitly built for pre/post comparison after
configuration changes and CI/CD regression — genuinely adjacent to the D3
harness. But: preview APIs, a hard requirement on Transaction Search (this
repo's `--observability` flag, which accrues continuous ingestion/storage
charges), and LLM-judged quality + tool-call evaluators rather than our
deterministic efficiency metrics. Verdict: **complementary, not a
replacement** — the standing project invariant (managed AgentCore
evaluations COMPLEMENT `smoke_prompts.py` — never replace it) extends
naturally to the eval lane. The documented FOLLOW-UP seam is to push the
same golden set (`agents/demo/golden_evals.json`) as an AgentCore dataset
scenario, gaining LLM-judged quality scores alongside — never instead of —
this lane's deterministic verdicts. D3 has NO AgentCore dependency; the
deterministic efficiency metrics remain ours.

## Considered options

- **D1 failure-context gating** (classify only when "error/failed/
  exception" co-occurs). Rejected: the pinned genuine-error fixture
  `"query timeout after 60s"` carries no failure-frame word — the gate
  would trade a documented false-positive class for a silent
  false-negative class. Word boundaries + pinned fixtures instead.
- **D1 keeping bare substrings and suppressing known data values.**
  Rejected: a denylist of colliding literals is dataset knowledge in
  engine code (violates the demo-literal invariant) and rots per
  environment.
- **D2 blanket no-numbers-in-absence-answers.** Rejected: honest absence
  answers legitimately quantify other systems ("I checked 3 connected
  systems"); the golden's own `expect` block is the only party that knows
  which metric was asked.
- **D2 LLM-judged honesty.** Rejected: verdicts must be reproducible; the
  determinism invariant (ADR-0053 (e)) bans LLM-produced scores.
- **D3 gating on metric deltas.** Rejected: run-to-run variance on a live
  LLM path exceeds most real signals (ADR-0053 D-E2); only verdict
  `pass→fail` gates, and only under opt-in `--strict-regression`.
- **D3 building on AgentCore dataset evaluations.** Rejected as a
  dependency: preview APIs, Transaction Search requirement, LLM-judged
  metrics. Kept as the documented follow-up seam above.
- **D4 per-request injection via a reserved tool-call arg.** Rejected: it
  would widen the deliberately narrow ADR-0049 reserved-arg contract and
  make user-authored text a per-call prompt-injection vector through the
  gateway transport.
- **D4 env-var delivery.** Rejected: the agent-config secret is the
  established channel for prompt-sized payloads (env size limits are the
  documented reason `system_prompt`/`tables_desc` live there).
- **D4 reonboard-to-refresh.** Rejected: `update_agent_runtime` with a
  merged secret achieves the same delivery in ~1–2 min without destroying
  and rebuilding the runtime + gateway target.
- **D5 guardrail/NER-based scrubbing of business context.** Out of scope
  for now: the write-time secret-shape rejection is deterministic
  best-effort (the same posture as the memory-redaction note in the
  README); a guardrail-based scrub is a documented follow-up for
  non-demonstration use.

## Consequences

- Error labels in smoke reports mean errors again: that environment's `TIMEOUT` count
  drops to its honest value, env-to-env comparisons stop manufacturing
  phantom differences, and every needle is protected against the same
  collision class — at the cost of a documented, fixture-pinned recall
  edge case (bare-word data values).
- Absence honesty is scored by what actually matters — fabrication about
  the asked metric — so honest paraphrases pass and invented
  numbers/identifiers fail with named evidence. Legacy goldens keep their
  exact semantics until edited.
- Every eval run is a measurement: per-question metrics with honest nulls,
  a variant stamp, and a diffable golden-set hash. Two runs make an
  experiment: the A/B diff reports per-question deltas and a suite rollup
  a human can act on, without adding a flaky gate.
- Stewards can teach a system its business meaning through the UI/API and
  A/B the effect within minutes — with the text bounded, secret-rejected,
  fenced as untrusted data, placed before the policy prompts, and
  permanently barred from carrying evidence weight.
- The deterministic lane stays self-contained; when AgentCore dataset
  evaluations exit preview, the golden set can be pushed as a dataset
  scenario for complementary LLM-judged scores without touching this
  lane's verdicts.

## Related

- [ADR-0045](0045-multi-site-metric-fan-out.md) — the honest-data rule
  (absence = absent) applied to every metric field.
- [ADR-0046](0046-deterministic-response-evidence-contract.md) /
  [ADR-0047](0047-response-integrity-enforcement-is-opt-in.md) —
  grounding is tool-result-evidence-only; business context never vouches.
- [ADR-0049](0049-user-identity-propagation-app-signed-claim.md) — the
  narrow reserved-arg contract that rejected the per-request channel.
- [ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
  — machine claims never vouch for machine claims.
- [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) — the
  measured-only cost surface (d) this harness consumes; the
  deterministic-only, informational-trend eval posture (e) this ADR
  extends.
- [docs/experiments.md](../experiments.md) — the operational A/B runbook.
