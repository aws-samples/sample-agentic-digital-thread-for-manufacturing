<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0048: Reactive identifier-miss translation loop (Mode A)

## Status

Accepted — 2026-07-17. Complements [ADR-0035](0035-system-agents-own-their-query.md)
and [ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md); interacts with
[ADR-0046](0046-deterministic-response-evidence-contract.md) /
[ADR-0047](0047-response-integrity-enforcement-is-opt-in.md).

## Context

Under Mode A delegation ([ADR-0035](0035-system-agents-own-their-query.md),
[ADR-0038](0038-system-query-mode-default.md)) each system agent composes its
own queries against its own vocabulary. That leaves a cross-system vocabulary
gap: when the orchestrator delegates a question carrying **another system's
identifier** (an order number, asset name, or part code valid in system A but
meaningless in system B), the target agent answered a bare "no data" — a
response indistinguishable from a genuinely empty result. The orchestrator's
anti-thrash rule ("STOP RE-QUERYING THE GRAPH AFTER EMPTY RESULTS") correctly
forbids graph re-query loops on empty results, so no recovery happened: the
turn ended with an honest-looking but wrong "not found", even when the
knowledge graph already held a `ValueMapping` that could have translated the
identifier.

The failure is reactive by nature — proactive translation (workflow Step 3)
covers identifiers the orchestrator *knows* need translating; this gap is the
identifier it did not know about until a system said "that is not my
vocabulary".

## Decision

A bounded, disclosed, marker-gated recovery loop, implemented as prompt
contracts (module-level constants; the only engine-code change is the one-line
`+ _identifier_miss_prompt()` append inside the small ADR-0047 wrapper
`_compose_system_prompt` in `system_agent.py` — the
existing proposal ingest in `runtime_app._ingest_value_mapping_proposals`
already accepts the emitted block format). Three parts:

1. **System-agent marker contract.** A system agent asked about a SPECIFIC
   identifier that matches nothing distinguishes two failures before
   answering. *Wrong vocabulary* — the token does not even resemble the
   values in the column(s)/propert(ies) where it should live: instead of a
   bare "no data", the agent returns a fenced `IDENTIFIER-MISS` block with
   three fields — `token` (the unmatched identifier exactly as asked),
   `looked_in` (the table.column(s) or propert(ies) checked), and `my_format`
   (what its own identifiers for that concept look like, with 2–3 REAL
   example values taken from its in-prompt schema samples or query results,
   never invented). *Valid shape, zero rows* — the identifier looks like the
   agent's own values but the query legitimately returned nothing: that IS
   the answer, reported as an honest zero, never marked. Genuine empty
   results stay honest empty results; the marker is exclusively a vocabulary
   signal. The marker is model-emitted but deterministically **post-filtered
   in engine code** (`_strip_false_identifier_miss` in `system_agent.py`,
   applied to every Mode A answer on the legacy/report-only and annotate
   paths): a block whose `token` shape-signature (separators + per-segment
   length/character-class; short alpha prefixes compare by text) matches any
   of its own `my_format` examples is a false positive — an honest zero in
   the agent's own vocabulary — and is stripped, because live probes showed
   prompt-only enforcement of the shape rule (including an explicit FINAL
   CHECK gate) is unreliable on the small model. Parse failures fail open:
   a block whose token or samples cannot be extracted is kept, so a genuine
   miss signal is never destroyed.

2. **Orchestrator carve-out — bounded, disclosed, never silent.** Two signals,
   and only these two, trigger recovery: the fenced marker, or a wrong-form
   miss (no rows after the orchestrator sent an identifier in the form
   another field holds, per the route seed's IDENTIFIER FORMS block, with or
   without the marker). A bare empty result or a plain "no data" triggers
   nothing. It is declared as the ONE sanctioned exception
   to the anti-thrash rule and the tool-budget follow-up cap (an explicit
   exception cross-referencing the rule by name, not a relaxation of it):
   ONE `explore_graph` translation lookup scoped to exactly that token and
   target system (checking `ValueMapping` from_value → to_value, then
   `IdentityRule` field pairs for the shared concept, then Asset name →
   source_id), then at most ONE re-delegation to the SAME system with the
   translated identifier. Hard cap: 2 such recoveries per turn; further
   misses are reported with their format hints (or, for a miss without the
   marker, the expected field and form), not retried. If the graph has no
   translation, the orchestrator does not retry: it reports the gap honestly,
   with the system's `my_format` hint when the marker supplied one, and
   otherwise the field and form the IDENTIFIER FORMS block expects (no
   example values), so the user can supply the right identifier next time.
   If multiple candidate translations
   exist, it uses the highest-confidence one and DISCLOSES the substitution
   in the final answer; silent substitution is forbidden.

   **Presentation contract on a recovery turn — answer first, learning
   second.** The user asked a question; the answer to THAT question comes
   first, in the normal format, using the recovered data. The substitution
   disclosure and a note that the mapping was proposed for review follow as
   a short, visually subordinate footnote AFTER the answer (one or two
   sentences, set off by a horizontal rule or italics — never a heading,
   never before or inside the answer). The self-improvement story is shown
   but must never crowd out the answer itself.

   *Amendment 2026-10-03:* the note originally read "the system just learned
   this mapping". The proposal is filed PENDING (auto-accept defaults off,
   and the workshop package never turns it on), so that wording claimed a
   learning that had not happened. The footnote now says the mapping was
   proposed for review and translates automatically once accepted.

   *Amendment 2026-10-15 (two triggers):* the marker is no longer the only
   trigger. The Mode-A IDENTIFIER FORMS rule (route-seed forms block) made a
   wrong-form miss recoverable without it: no rows after the orchestrator sent
   an identifier in the form another field holds. Its sentence said "recover
   it even without an IDENTIFIER-MISS block" while this contract still said
   "ONLY the marker", and a system agent that follows the new ZERO-ROW LOOKUPS
   rule reports such a lookup in words (identifier, form, field, no rows),
   often without the block. The recovery contract now names both triggers:
   (a) the fenced block, or (b) a wrong-form miss, where the identifier sent
   is the token and the field the reply looked in is `looked_in`. Any other
   empty result still triggers nothing, so a genuine empty result in the
   system's own form stays an honest zero. Steps 1 to 4, the cap of two
   recoveries per turn, disclosure and the learning mandate are unchanged.
   A miss without the block carries no `my_format` value, so step 3 and the
   over-cap line ask for the field and form the IDENTIFIER FORMS block
   expects instead, with no example values.
   The fix makes the miss honest; whether the orchestrator then sends the
   paired form comes from this prompt contract and is shown only by a live
   run. Pinned in `tests/unit/test_miss_recovery_triggers.py`.

   *Amendment 2026-10-19 (no record under a known link):* an empty result in
   the form the looked-in field holds is not a miss of either kind. Workshop
   walks: a machine's live run had not shipped, the orchestrator carried its
   run number along a confirmed join (`value_exact_match`) to the ERP field
   that holds run numbers, found no rows, ran the recovery and answered that
   "no mapping exists to translate it", and left the question's other ask
   (open orders and shipments of the part) unanswered. Three decision points
   now say it is "no record there yet". (1) The system agent's tool result:
   `system_agent._resolve_known_link_misses` replaces an IDENTIFIER-MISS
   block with one fixed line, "No record of <value> in <field> yet: the
   lookup returned no rows.", when the request carries the ROUTE block exactly
   as the delegate step writes it (a fenced ```` ```ROUTE: ```` block with its
   closing fence, each of the five labels once, indented continuation lines
   only, and a `target_system` equal to this system), every `looked_in` field
   is one of this system's fields on that block's `join_rule:` line (the same
   path, the same path behind the system's own schema or id, or a
   `table.column` ending exactly one route field; another schema stays a
   miss), the request sent the token exactly, and `my_format` shows no
   observed value of another shape (the join's measurement is the shape
   evidence zero rows cannot give). The fenced block is the boundary of what
   is replaced: this protocol now puts its one sentence of context on the
   block's own `context:` line, so the context goes with the block, and every
   byte outside it stays as written. A block without a context line follows
   the older protocol (the block, then one short sentence of context): it is
   replaced together with exactly that first following sentence, and every
   byte after the sentence stays as written; nothing that opens a fence,
   heading, table row, list item or markup counts as that sentence. The
   shape guard's own refusal block carries a `context:` line too, so every
   producer in the engine writes the current form. The four protocol fields
   are read by one parser with one rule for every protocol label (leading
   whitespace and any letter case allowed); any other labelled line (an
   `Examples:` or `Pattern:` line) continues the field before it, so values
   listed on it stay `my_format` evidence, and nothing after the context
   label ever reaches `my_format`. A block with no `my_format` sample takes
   the no-record path only when `my_format` says, in the protocol's own
   words, "no non-null values observed" (the prompt now names that exact
   phrase); any other format text without a sample stays a miss.
   For the same reason the shape guard replaces a
   false-positive block that carries a context line with the no-record line
   instead of removing it. (2) The
   system agent's ZERO-ROW LOOKUPS rule and the honest-zero case of this
   protocol say a field the join rule names holds the form, and the asks that
   do not depend on the identifier are still answered. (3) The Mode-A
   delegate block's NO RECORD UNDER A KNOWN LINK rule: no translation lookup,
   no follow-up for that value, never called a mapping, translation or format
   gap. An ask about the value's own records (which customers received it)
   depends on it and closes with the no-record finding; only an ask the
   question makes separately at a scope it names itself (open orders for the
   asset or product beside the value) stays open. Trigger (b) names it as a
   non-trigger. Pinned in `tests/unit/test_no_record_under_known_link.py`.

3. **Value-mapping flywheel.** A CONFIRMED recovery (the re-delegation
   returned data) MUST persist the learned mapping — six required fields
   (`from_system`, `from_field`, `from_value`, `to_system`, `to_field`,
   `to_value`), `confidence: high`, and a reason naming the confirmed
   recovery — through whichever learning channel the serving surface
   provides. In the full orchestrator prompt the proposals section is
   present, so the mapping is emitted as the existing
   `---VALUE-MAPPING-PROPOSAL---` block (a block missing any field is
   discarded) and ingested by `runtime_app._ingest_value_mapping_proposals`;
   on the Quick Mode-A surface that proposals section is sliced out, and the
   same six values are saved via the `save_value_mapping` tool instead
   (`_QUICK_LEARNING_TAIL`). Each reactive recovery thereby
   becomes a proactive Step-3 translation on future turns: the loop pays
   its own cost down over time.

**Mode B has no analog — deliberately.** In Mode B
(`SYSTEM_QUERY_MODE=orchestrator`) there is no per-system agent that could
emit a text marker: the orchestrator composes SQL itself, and `run_query`
already returns a structured `error_type: schema_mismatch` with a
`schema_hint` (the "SELF-CORRECT ON SCHEMA ERRORS" contract in
`workflow_guidance.MODE_B_WORKFLOW_GUIDANCE`), so schema-level misses are
covered structurally. Value-vocabulary translation in Mode B remains Step 3's
proactive job. The recovery text lives inside the Mode-A delegate block, so
the Quick prompt composition automatically includes it in Mode A and excludes
it from Mode B.

*Amendment 2026-10-15 (Mode B and empty lookups):* still no loop in Mode B,
on purpose. Mode B has no system agent: `run_query` executes the
orchestrator's own SQL (`_mode_b_run_query`), so the system-agent ZERO-ROW
LOOKUPS rule never runs there, and the orchestrator sees an empty result
directly. That result is a miss under MISS FOLLOW-UP ("A miss is an empty
result"): one follow-up, which may use another identifier the orchestrator
already holds for the same records, then the gap is stated as what was
searched and not found, under the existing honest-absence and no-fabrication
rules. A wrong-form identifier the orchestrator does not already hold in the
other form leaves that part unanswered, and the answer says so; no
paired-form retry is added. Pinned in `tests/unit/test_miss_recovery_triggers.py`.

**Future note — do not build now.** When the response-integrity `annotate`
sidecar ([ADR-0047](0047-response-integrity-enforcement-is-opt-in.md))
matures, `unrecognized_identifiers` becomes a TYPED field in the integrity
metadata and replaces the free-text marker. Under `enforce` mode the typed
`SystemAnswerDraft` contract ([ADR-0046](0046-deterministic-response-evidence-contract.md))
already supersedes the marker today — system agents emit typed envelopes, so
the text marker never appears and the recovery block is dormant; the default
posture is report-only ([ADR-0047](0047-response-integrity-enforcement-is-opt-in.md)).

## Consequences

- Cross-system questions carrying a foreign identifier recover in-turn when
  the graph holds a translation, at a bounded cost (one lookup + one
  re-delegation per miss, at most twice per turn) instead of ending in a
  false "not found".
- When the graph has no translation, the user gets a strictly better failure:
  the gap plus the target system's identifier format (with real example
  values when the marker supplied them, the expected field and form
  otherwise), instead of a bare "no data".
- Every confirmed recovery feeds the learning loop
  ([ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md)) through the
  existing value-mapping proposal ingest — no new persistence machinery.
- The carve-out is a prompt contract, not deterministic code: the hard cap
  and the two-trigger gating are enforced by instruction, and adherence is
  model-dependent. The typed integrity-metadata field (future note above) is
  the deterministic home when it lands.
- The anti-thrash rule text is unchanged; the exception is declared from the
  new recovery block by exact rule-name cross-reference, so the general
  prohibition on empty-result re-query loops stands.
- Honest zeros are preserved by construction: a valid-shape, zero-row result
  is never marked and never triggers recovery.
