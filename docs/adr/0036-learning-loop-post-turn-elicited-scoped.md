<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Learning loop is post-turn, human-elicited, scoped, and lifecycle-managed

---
Status: accepted
---

> Implementation status: **pending** — decided in the 2026-07-02 design review; pairs with
> [ADR-0035](0035-system-agents-own-their-query.md). (Design brief since removed;
> this ADR is the standalone record.)

## Context

`save_value_mapping` / `save_correlation` are **inline agent tools the
orchestrator calls mid-turn**, inside the request path (traces: Q5 = 8+8, Q7 =
16+4 tool-refs of these per question). This pollutes and slows the interactive
turn, and it makes learning an autonomous write with no human control. Two further
gaps: **end-to-end verification of the learn → improve loop is tracked as
follow-up work** (the accumulate-into-`query_guidance` mechanism was unit-tested
but has not yet been exercised as ask → learn → re-ask → improvement); and there
is **no lifecycle** — proposals only
*create*, with no way to view, edit, retire, or bound the set, so it trends toward
an ever-growing, self-contradicting pile.

## Decision

- **Post-turn only.** Remove the inline learning tools from the request path.
  Candidate learnings are extracted **after the answer is delivered** (including
  from the in-agent self-recovery events introduced in ADR-0035).
- **Human-elicited proposals.** Candidates surface as **proposals** (reuse the
  unified proposal lifecycle — [ADR-0003](0003-unified-proposal-lifecycle.md) —
  and the self-learning UI panel). **Nothing persists without human accept.**
- **Route by scope on accept** (per ADR-0035's scoping): a **system-local**
  correction → that system's **mutable guidance store + version bump** (the agent
  refreshes its in-process cache, zero downtime); a **cross-system** mapping /
  correlation → the **graph** (consumed by the orchestrator).
- **Lifecycle management.** A learnings view grouped by scope with **view / edit /
  retire** (editing a system-local learning bumps the version → cache refresh),
  **usage telemetry** (applied-count + last-used), and **supersede-on-conflict** (a
  new learning targeting the same system + target *replaces* the old rather than
  appending). **Automated staleness/decay retire** (unused N days or low hit-rate →
  human-confirmed) is a **fast-follow** once telemetry accrues.
- **Closed-loop verification is the acceptance gate.** A test that asks a question
  that triggers a proposal → accepts it → re-asks the **same** question → asserts
  the second run is **measurably cleaner** (uses the learned guidance; no
  self-recovery needed; fewer calls). Until this passes, "self-learning helps"
  stays an unverified claim.

## Considered options

- **Autonomous mid-turn writes (status quo) — rejected.** Pollutes the request
  path, gives the human no control, and grows unbounded.
- **Auto-apply empirically-validated system-local corrections** (skip the human
  gate for corrections the agent already proved by a successful retry that turn),
  human-gate only cross-system mappings — **deferred.** Reasonable later, but start
  **fully human-gated** for control; revisit only if the proposal queue proves
  noisy.

## Consequences

- Learning no longer adds tool calls to the interactive turn.
- Adds a proposal + lifecycle surface; reuses ADR-0035's mutable per-system store
  and version check for zero-downtime refresh on accept/edit.
- The learnings store becomes **curated and bounded** (supersede + telemetry +
  review) rather than append-only.
- The closed-loop test is a new acceptance gate in the scorecard lane.
