// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Fired-vs-loaded metric copy (T1.6 / G3) — the ONE source for the
 * intelligence-summary semantics wording. `planned_with` and `rules_applied`
 * measure DIFFERENT things and every surface that shows either must say so:
 *
 * - `planned_with.*` counts artifacts LOADED into the Mode-A route seed at
 *   planning time (a graph-state function — "the planner had it").
 * - `rules_applied` records accepted rules that actually FIRED while
 *   answering ("the answer used it").
 *
 * `planned_with` moving while `rules_applied` stays empty is therefore NOT a
 * contradiction — it is the consumption-gap signal (loaded but unused).
 *
 * Reused verbatim by the T3.5 results scorecard — do not fork this wording
 * per surface. Copy only; no state lives here.
 * Backend definitions: docs/experiments.md "Fired vs loaded".
 */

/** Tooltip for `rules_applied` surfaces ("Bridged via", Query Intelligence). */
export const RULES_APPLIED_TIP =
  'Used in this answer: accepted self-learning rules that actually fired while answering this query. Distinct from "loaded at planning" — a rule the planner loaded may never fire. Grows as you accept Self-Learning Discoveries.';

/** Tooltip for `planned_with` surfaces ("Planned with"). */
export const PLANNED_WITH_TIP =
  'Loaded at planning: knowledge-graph routing metadata fetched into the route seed before answering — systems, shared concepts, join paths, value mappings, identity rules and authority rules. Loaded does not mean used in this answer: see "Used in this answer" for the rules that actually fired.';
