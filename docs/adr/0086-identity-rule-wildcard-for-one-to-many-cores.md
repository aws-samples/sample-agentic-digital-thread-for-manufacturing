<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0086: An IdentityRule pattern carries a `{*}` wildcard slot when one identifier core maps to several records

## Status

**Accepted: 2026-09-27.** Implemented. Verified offline only; the frozen-story checks in a deployed account (the seed
log and module 2) are listed under Verification and not yet recorded.

> **2026-09-29 note (frozen PLM margin).** The frozen PLM now carries FOUR
> machined cores, three of them multi-revision: `7742` (3 revisions), `8810`
> (2), `9901` (1) and the new Hydraulic Manifold Block `5520`
> (`PN-5520-REV-A` Obsolete, `PN-5520-REV-B` Released;
> `lambda-adt-seed/005_plm_engineering.sql`), built as MES product `5520` on
> new equipment `L7-M2` (`003_mes_line3.sql`, batches `B-2951..B-2958` /
> `WO-9952..WO-9959`, February 2026). The required one-to-many count is
> `max(2, ceil(0.25 * 4)) = 2`, so the frozen wildcard now holds with a
> margin of one core (3 of 4, where it was exactly 2 of 3). The expected
> seed-log reasoning becomes "4 of 4 sampled identifier cores share one
> transform; 3 of 4 cores match several target values (up to 3)"; the
> recorded confidence stays 85%. Pinned by
> `tests/unit/test_frozen_plm_wildcard_margin.py`; B-2847 and the defect
> chain are untouched. The body below is unchanged.

> **2026-10-21 amendment (one compiler, two readers).** Every IdentityRule
> pattern is compiled by ONE function, `identifier_translation.pattern_body`:
> `{name}` is a named group, `{*}` one alphanumeric token, and every other
> character is literal text (escaped). Before this, the resolver
> (`resolve.transform_id`) used the literal text as a Python regular
> expression while the route seed read it literally, so `LOT.{id}` matched
> `LOTX123` in one reader only. The two readers keep one deliberate
> difference, because they read different inputs:
>
> - the **route seed** (`identifier_translation.pattern_regex`) finds
>   identifiers inside a person's free-text question: a placeholder is one
>   word (`\w+`), the match is whole-token and case-insensitive;
> - the **resolver** (`resolve.transform_id`) transforms one whole stored
>   value (a row's field, an identifier typed into the Thread panel): a
>   placeholder is `.+` and case is the value's own.
>
> Neither hands the other a value it captured: the route seed's translations
> go only into the orchestrator's prompt text. Because one rule must mean the
> same thing on both paths, it is written only when the two readers translate
> the values it is checked on alike (`resolve.reader_disagreement`): provision
> runs it over the source values its transform check reads (selected in any
> case), and `/api/thread/learn`, which carries no example, over a value
> built from the pattern. Pinned by
> `tests/unit/test_identity_rule_provision_r5.py` and `_r6.py`. The
> affinity pass's `resolve.infer_pattern` now takes only digits as the core,
> so it never writes a pattern like `WO{id}` (core `-9921`) that the
> resolver reads and the route seed can never find. Every IdentityRule
> written since carries `pattern_grammar = 'literal'`; a stored row without
> it keeps its old meaning: the resolver reads its literal text as a regular
> expression (`transform_id(..., literal=False)`), the route seed literally,
> as it always did, and the resolver logs once per such pattern holding
> regular-expression characters (`identifier_translation.note_legacy_reading`).
> A re-provision rewrites onboarding rules with the marker; a taught
> (`user_confirmed`) rule moves when it is taught again.

Builds on [ADR-0065](0065-unattended-provisioning-fresh-account-findings.md)
(identifier-core affinity, `value_core_match`) and
[ADR-0084](0084-one-erp-and-the-workshop-story-setting.md) (the frozen MES →
PLM → ERP leg).

## Context

A frozen-story deployment on 2026-09-27 discovered
`mes-ind.product_code {id} -> plm.item_id PN-{id}-REV-A [value_core_match]`.
The MES product `7742` is carried by three PLM items (`PN-7742-REV-A`,
`-REV-B`, `-REV-C`; only REV-C is Released), `8810` by two and `9901` by one.
Module 2 answers then said batch B-2847 was "built to" the obsolete REV-A.

Root cause, in `identity_affinity._core_index`: it kept the FIRST value per
digit core, and the target sample is `SELECT DISTINCT … LIMIT 200` with no
`ORDER BY`. The pass saw one value per core (all REV-A in that run's row
order), `infer_pattern` turned `7742 ↔ PN-7742-REV-A` into `PN-{id}-REV-A`,
and the varying tail became a constant. With another row order the three
representatives would have disagreed and the rule would have been rejected
(2 of 3 < the 0.8 agreement threshold). The rule was wrong AND depended on
row order.

How a translated identifier reaches the target system agent: no deterministic
filter exists. The route seed carries every IdentityRule with its patterns
plus the optional deterministic IDENTIFIER TRANSLATIONS block (tokens in the
question only). For an identifier learned mid-investigation the orchestrator
LLM applies the rule itself and writes `translated_identifiers` into its
`ROUTE:` block, which reaches the system agent as advisory `kg_context`
(`system_agent._sanitize_kg_context` → `_kg_context_block`); the system
agent writes its own query. So the levers are the rule's pattern, the wording
of the deterministic translation, and one seed note. None of them is SQL.

## Decision

1. **Grammar.** One new pattern token, defined once in
   `agents/identifier_translation.py` (`WILDCARD`, `has_wildcard`):

   | Token | Meaning | Match regex (`pattern_regex`) | SQL side (`resolve.transform_id`) |
   |---|---|---|---|
   | `{name}` (conventionally `{id}`) | captured identifier core (unchanged) | `(?P<name>\w+)` | `(?P<name>.+)` |
   | `{*}` | a part that VARIES across several records sharing one core | ONE alphanumeric token, `[A-Za-z0-9]+` (`WILDCARD_REGEX`), uncaptured | in the source: the same class; in the output: `%`, with `%` `_` `\` in the captured values backslash-escaped |
   | anything else | literal | escaped | as before |

   A pattern still needs literal text outside placeholders and wildcards; a
   wildcard rule keeps at least one alphanumeric literal; `{*}` is never
   emitted next to a `{name}` placeholder (ambiguous, fails closed). The bare
   `"*"` direct-match convention is unrelated (`{*}` is braced).

2. **Emitter (`agents/identity_affinity.py`).** The pass groups ALL sampled
   values per matched core (at most `MAX_VALUES_PER_CORE` per side, the
   field's commonest value shapes first, so rare-shaped siblings that sort
   early cannot crowd the fitting ones out). Then, in this order:
   - **Within each core**, the values that fit one inferred shape are merged
     token by token: the `{*}` slot is exactly the token positions that vary
     among THAT core's fitting values. A `{*}` next to a placeholder, or a
     merge with no alphanumeric literal, is ambiguous and yields no pattern.
   - **Across cores**, the per-core merged patterns must AGREE EXACTLY; the
     only latitude is that a `{*}` in the candidate accepts one alphanumeric
     literal at that position in another core (a core with a single value
     there). Candidates are only patterns some core's own values produced, so
     a token that differs only ACROSS cores (`PRT-` in one core, `DOC-` in
     another) never becomes `{*}`.
   - **Support is counted per core** against the existing thresholds
     (`MIN_MATCHED_PAIRS` and `MIN_PATTERN_AGREEMENT` over all matched cores),
     and the agreeing cores that are themselves one-to-many must number at
     least `max(MIN_WILDCARD_CORES, ceil(MIN_WILDCARD_SHARE × agreeing
     cores))` (`MIN_WILDCARD_CORES` = 2, `MIN_WILDCARD_SHARE` = 0.25;
     `identity_affinity.required_one_to_many`): one coincidental sibling is
     not evidence that a part varies, and neither is a fixed handful among
     many cores. So a token that varies within fewer than the required share
     of cores never becomes `{*}`: two lots of another year
     (`LOT-2025-02850`, `LOT-2025-02851`) among 50 or 200 `LOT-2026-…` lots
     keep the one-to-one `LOT-2026-0{id}`, while the frozen shape (2 of 3
     cores one-to-many) still yields the wildcard.
   - **Two supported candidates with equal support fail closed** for the
     wildcard path.
   - **Fallback.** Whenever the wildcard path yields no rule (no supported
     candidate, a tie, an ambiguous merge), the pre-wildcard one-to-one pass
     runs unchanged over one representative per core, so every rule it emits
     is byte-identical to before. That pass can never itself be a tie: its
     winner must hold `MIN_PATTERN_AGREEMENT` (0.8 > 0.5) of the pairs
     (pinned). So "fall back only if the one-to-one result is unambiguous"
     holds by construction.

   The multiplicity is the first clause of the rule's `reasoning` (persisted
   as `origin_context`, truncated at 300 characters). The wildcard decision
   never depends on sample row order; the fallback keeps its original
   first-value-per-core representatives (and therefore its original row-order
   sensitivity).

3. **Precedence.** A wildcard affinity rule REPLACES an LLM rule for the same
   field pair in the LLM rule's list position: the sample proved the part
   varies, so an LLM constant there is exactly the defect. Its reasoning then
   ends `; replaced proposed constant <the LLM's pattern>`, kept within the
   300-character cap by shortening the tail of the evidence (the multiplicity
   clause stays first). Otherwise the LLM rule still wins and non-wildcard
   affinity rules only add new pairs.

4. **Consumers.** A translation through a wildcard is a CANDIDATE SET, never
   one value: the seed line carries a `(candidate set: …)` suffix and the
   Knowledge Graph trace says "matches every identifier beginning <fixed
   part>". The reverse direction (`PN-7742-REV-C` → `7742`) is exact. The
   SQL-side transform renders `{*}` as LIKE `%`, consumed by the existing
   LIKE branch of `resolve._try_pattern_transform` and by the provision sample
   check. When the target carries `{*}`, `%`, `_` and `\` in the captured
   values are backslash-escaped, so a caller-supplied `51%01` cannot become a
   live LIKE metacharacter (the PostgreSQL default LIKE escape; that branch has
   no explicit `ESCAPE` clause, see deferred.md). Every consumer that reads
   the seed's JSON back (`route_seed_trace._payload`,
   `runtime_app._planned_with_from_seed` / `_composed_context_from_text` /
   `_seed_rule_index_from_text`, `metric_term_resolution.resolution_seed_view`)
   decodes it with `raw_decode` and ignores (the view: keeps verbatim) the
   text after it, so the translations block and the note never turn a parse
   into a fail-open miss.

5. **One conditional seed note.** `orchestrator._identifier_translation_block`
   appends `identifier_translation.wildcard_note(rules)` — the constant
   `WILDCARD_SEED_NOTE`, once, on its own line after the JSON payload and the
   optional translations block — only when a returned IdentityRule carries
   `{*}` in either pattern. It tells the orchestrator never to fill `{*}`, to
   describe the candidate set as "every identifier beginning (or matching) the
   fixed part", to ask the target for every matching record with what
   distinguishes them, and, when the source record does not say which applies,
   to use the record whose effective or release window covers the date of a
   past event the question concerns, otherwise the one the target marks
   current, released or active, always stating which record it used and why.
   It is plain English with no query fragment (Mode A never sends queries), is
   added after the seed budget check like the translations block, and never
   enters the JSON. Seeds without a wildcard rule are byte-identical; the
   emergency-bounded seed carries no note. It lives in the seed, not the
   system prompt, because the Mode-A delegate block and the Quick prompt are
   byte-pinned.

## Consequences

- The frozen rule becomes `mes-ind.product_code {id} -> plm.item_id
  PN-{id}-REV-{*} [value_core_match]`; the Released `PN-7742-REV-C` is chosen
  at answer time from the PLM's own status, and the answer names the choice.
  The choice is prompt-driven, not deterministic; a deployed run checks it.
- The Knowledge Graph trace step runs before any system answers, so it cannot
  truthfully announce a translation for an identifier the MES has not returned
  yet. The candidate set shows on the per-system (PLM) step's ROUTE block. A
  question that names any revision now reverse-translates in the KG step.
- **Sample completeness residual.** Detection needs at least two agreeing
  one-to-many cores (and `MIN_WILDCARD_SHARE` of the agreeing cores) in
  either sample (several source or target values per core). `resolve_evidence._SAMPLE_LIMIT`
  is 200 with no `ORDER BY`; above 200 distinct values the sample's CONTENT,
  not only its order, depends on the database's row order, so siblings can
  be missing, the pass takes the one-to-one path, and the constant can recur
  silently. The same fallback applies when only ONE core is one-to-many (by
  design: one sibling is not evidence), with the one-to-one pass's original
  first-value representative. The frozen story is complete (29 distinct PLM
  item ids). Follow-up: a bounded sibling probe per matched core.
- **The seed note bypasses the serialized budget.** It is appended after
  `_ROUTE_SEED_SERIALIZED_BUDGET` (8,000 characters) is enforced, like the
  translations block: 953 characters (954 with its leading newline when no
  translations block precedes it), only on seeds whose rules carry `{*}`.
- Threads resolve (`/api/thread/resolve`) uses the LIKE branch with `LIMIT 1`,
  so it returns an arbitrary sibling and its check line shows `=` for what
  was a LIKE match (`= PN-7742-REV-% ✓`). Unchanged; the Threads panel is not
  the workshop path.
- `route_seed_trace._payload` and `parse_rendered` are unaffected by the note
  (`raw_decode` stops at the JSON; the rendered-line parser stops at the first
  non-matching line).

## Alternatives rejected

- **Pick the latest or Released revision in the engine.** The engine has no
  lifecycle semantics; encoding "REV" or "Released" would be a demo literal
  in `agents/` (ADR-0044).
- **Field-level tail variability** (mark a tail variable when it varies across
  the whole field). It would rewrite existing one-to-one rules such as
  `LOT-2026-0{id}` and break byte-identity.
- **A system-agent prompt rule.** Not needed unless a deployed run shows the
  PLM agent still filters by equality; kept as a measured contingency.

## Verification

Offline: `tests/unit/test_identity_rule_wildcard.py` (grammar constants and
the note wording), `test_identity_affinity.py` (emitter, order independence,
precedence and the replacement note), `test_identity_affinity_cores.py`
(within-core merge, cross-core exact agreement, the two-one-to-many-core
minimum and the `MIN_WILDCARD_SHARE` share on 50- and 200-core samples, the
legacy fallback byte-identical on the test fixtures, tie fail
closed, the shape-ranked per-core bound, an `infer_pattern` call-count bound
on a 200-value sample), `test_resolve_identity_affinity.py` and
`test_onboard_wildcard_rule.py` (the onboarding path: affinity augmentation,
the semantic-role gate, the live sample check, the JoinPath `transform`),
`test_identifier_translation.py`, `test_route_seed_trace.py`,
`test_resolve_transform_wildcard.py` (consumers, the one-token match class,
LIKE escaping), `test_route_seed_wildcard_note.py` (the seed note: once, own
line, byte-identical without a wildcard rule, fail-soft, absent from the
emergency seed), `test_one_to_many_identifier_chain.py` (affinity → seed →
translation → trace → `transform_id` → `planned_with`),
`test_runtime_planned_with.py` (`planned_with` reads past the seed tail),
`test_metric_term_resolution.py` (`resolution_seed_view` strips guidance
with the translations block and the note appended) and
`test_usage_route_intersection.py` (the seed rule index past the tail).

Not yet verified in a deployed frozen account: the seed-log line above, module 2
answers naming `PN-7742-REV-C` in fresh chats, the PLM step's candidate set.
