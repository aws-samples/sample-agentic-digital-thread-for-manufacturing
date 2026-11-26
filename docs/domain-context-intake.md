<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Domain-context intake—the ten questions and where each answer lands

Accurate answers depend on domain context that no schema can carry on its own:
what the words mean, which system is authoritative, and what the data does not
cover. This document is the standard intake: ten questions, why each matters
(with a generic illustrative pattern for each), and the engine surface each
answer lands on. The answers can come from your own system documentation, data
dictionaries, subject-matter-expert (SME) interviews, or incident postmortems—the
source matters less than getting each question answered and verified.

This drives three consumers: the business-context card's intake hints (below),
the pack format's section design (each question = a pack section, planned
capability), and the day-0 interview (each question = an interview topic —
live in the guided onboarding flow).

These questions feed the onboarding pipeline — the guided flow asks them at
day 0 and between discover and map; see [`onboarding.md`](onboarding.md).

## The ten questions

### 1. What is the unit of work—and which common words do not apply?
*Why:* the model maps unfamiliar schemas onto familiar vocabulary. For
example, a discrete flow with no batching requires an explicit "this is X,
not Y" statement—otherwise questions about "batches" are silently
reinterpreted, and the agent answers a different question than the one asked.
*Lands on:* business context (the "this is X, not Y" sentence) + concept
retirement / overlays.

### 2. What do lifecycle states mean—and which are bookkeeping, not events?
*Why:* state columns often encode workflow mechanics rather than physical
events. If retries re-enter the workflow under a new attempt id, closing the
previous attempt's records as FAILED/CANCELLED, then raw failure counts
overstate real problems—every failure-rate answer is wrong in the business's
eyes and correct by the data. Ask directly: "when something is retried, what
happens to the old record?"
*Lands on:* business context + field annotations on the state column.

### 3. Which failure/exception codes are real problems, and which are workflow noise?
*Why:* failure taxonomies commonly mix genuine signals (hardware faults,
quality-check fails, SLA breaches, operator actions) with routine workflow
codes—and a handful of workflow codes can account for the vast majority of
recorded "failures". An agent that cannot make this cut reports noise as news.
*Lands on:* business context (the exclusion rule) + query guidance.

### 4. What identifier families exist, and how do systems' ids relate?
*Why:* the same machine can appear as a structured code such as
`MOD_<SITE>_<MACHINE>_<NN>` in one system and an opaque UUID in another.
Cross-system questions are impossible until the crosswalk exists, and
name-similarity guessing produces wrong joins.
*Lands on:* identifier-semantics patterns + value-mapping crosswalks (the
proposal lifecycle; machine-verifiable, so seed aggressively).

### 5. For each shared concept, which system is the authority?
*Why:* two systems can both "have" machine state—one holding record status,
the other live telemetry. Without an arbitration rule, answers flip depending
on tool-call order, and a wrong pre-seeded authority becomes a standing defect
that skews every answer touching that concept.
*Lands on:* authority rules (deployment-scoped, one system per concept—collisions
fail loud by design).

### 6. What does the data not cover?
*Why:* absence is where fabrication lives. If only a few facilities have OT
telemetry, or inspection columns exist but are entirely NULL, an agent without
declared boundaries invents plausible numbers for the uncovered sites. With
declared boundaries it says "no coverage"—the honest answer that builds
trust.
*Lands on:* business context (coverage statement) + domain notes.

### 7. How should this system be queried well?
*Why:* engine-visible symptoms (timeouts, capped row scans, hedged "at least
N" answers) often trace to paged SELECTs against a schema that wants
aggregates. An operator familiar with the system recognizes this instantly;
the agent needs to be told once, in a note, so every later query benefits.
*Lands on:* query guidance notes (per system or connection type; no runtime
roll needed).

### 8. What does the shop floor call the equipment—and what is not in the data?
*Why:* operators ask questions in operational vocabulary. A floor name for a
whole equipment family may appear in no connected system—in which case
equipment-association answers silently substitute a different machine, and
downtime questions have no honest "that equipment has no telemetry" answer.
Ask directly: "what do people call the machines here, and which of those
names will I not find in this data?"
*Lands on:* domain notes (guidance) + business context; a declared absence is
what lets the assistant say "not modeled" instead of guessing.

### 9. What do the coded values mean—and which code family answers what?
*Why:* one table can carry several distinct code populations—for example
quality reason codes, machine fault codes, and free-text fault detail in three
columns. Without a "use family X for question type Y" rule, defect questions
get answered from the wrong family. Sampled values make this recognition
rather than recall: show the steward the codes, ask what they mean.
*Lands on:* query guidance (the "use family X for question type Y" rule) +
field annotations.

### 10. How is each KPI calculated—especially its denominator?
*Why:* KPI names are shared vocabulary; KPI *formulas* are not. Asked for a
rate-style KPI, an agent will pick some plausible denominator from the schema
and compute a number that looks authoritative and is wrong—for example a
defect rate above its theoretical maximum because defect rows were used as
the basis instead of units produced. The numerator is usually obvious from
the data; **the denominator, the basis, and the exclusions are business
decisions that live only in people's heads.** Ask for each KPI: numerator,
denominator basis, exclusions, and any synonyms people use for it.
*Lands on:* business context (the stated formula)—and, once confirmed, a
`Metric` definition in the deterministic metric layer, which is what makes the
same question return the same number every time (see
[metric-layer.md](metric-layer.md)).

## Where to source the answers

Ranked by signal-per-hour:

1. **The system's own state-machine / API documentation**—questions 2, 3, 5
   usually have authoritative answers written down somewhere; ask for the
   docs before scheduling SMEs.
2. **A one-hour SME interview** structured as the ten questions (the planned
   day-0 interview capability automates exactly this).
3. **Data-dictionary + a day of live reads**—questions 1, 4, 6 are
   verifiable directly against the data; never seed a claim you can check and
   didn't.
4. **Incident postmortems / oncall runbooks**—the fastest source for
   question 3 (what pages people vs what's noise).

**Verification rule (non-negotiable):** every seeded claim that *can* be
verified against live data *must* be, before landing. Even when documentation
and data agree, the check is what makes the context trustworthy—stale docs
are a known failure mode. Claims that cannot be verified (pure semantics)
carry their source in provenance.

## UI surfaces, current and planned

- **Now:** the business-context card carries an intake-hints disclosure
  derived from this document (generic wording—the engine neutrality gate
  applies to UI strings too).
- **Planned capability (pack install):** each pack section maps to one
  question; the install report shows which questions a pack answered and
  which are still blank—"context coverage" as a first-class readout.
- **Now (day-0 interview / guided flow):** the guided flow walks a
  deterministic selection of these questions against the freshly-discovered
  schema—deployment scope at day 0, per-system probes between discover and
  map—turning answers into proposals through the same lifecycle (nothing
  lands without review). See [`onboarding.md`](onboarding.md).
