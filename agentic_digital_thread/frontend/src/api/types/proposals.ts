// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* API types — self-learning proposals + learned query guidance.
 *
 * Derived from the client usage (_slCard,
 * loadProposedMappings, submitUserDefinedMapping, loadLearnedGuidance) with the
 * FastAPI routers as cross-check. Fields are loose/optional on purpose — the
 * old UI treated every field defensively. */

export type ProposalStatus = 'pending' | 'accepted' | 'rejected' | 'superseded';

/** Known proposal types (the server may add more — keep open). */
export type ProposedType =
  | 'join_path'
  | 'value_mapping'
  | 'identity_rule'
  | 'field_annotation'
  | 'crosswalk'
  | 'insight'
  | 'schema_drift'
  | 'coverage_gap'
  | (string & {});

/** One self-learning discovery from GET /api/proposed-mappings. */
export interface ProposedMapping {
  mapping_id: string;
  proposed_type?: ProposedType;
  status?: ProposalStatus | string;
  confidence?: string | number;
  from_system?: string;
  from_field?: string;
  from_value?: string;
  to_system?: string;
  to_field?: string;
  to_value?: string;
  concept?: string;
  description?: string;
  /** Provenance — the question/auto-analysis that triggered the discovery. */
  query_context?: string;
  /** Free-text discovery rationale. */
  analysis?: string;
  /** The discovery's own stated evidence (e.g. the orchestrator proposal's
   * method:/reason: fields — how the mapping was established and why). */
  method?: string;
  reason?: string;
  /** Producing pipeline: 'heuristic' | 'orchestrator' | 'analysis-llm'.
   * Absent on legacy rows — the UI falls back to a proposed_type mapping. */
  provenance?: string;
  /** Proposal source (e.g. 'agent' | 'user' | 'user-feedback'). Rows from
   * the untrusted user-feedback path carry 'user-feedback' and are never
   * auto-accepted server-side. */
  source?: string;
  /** Exploration cost before the discovery ("tool calls last time"). */
  tool_calls_before?: string | number;
  /** Live schema-existence verdict: 'not_found' blocks Accept; 'verified' OK. */
  schema_verdict?: 'not_found' | 'verified' | '' | string;
  /** What column reference the schema check actually checked. */
  schema_checked?: string;
  ai_recommendation?: string;
  ai_confidence?: 'high' | 'medium' | 'low' | string;
  /** Value-overlap signal (join_path / crosswalk). */
  value_overlap?: string | number;
  overlap_ratio?: string | number;
  overlap_count?: string | number;
  overlap_ratio_confidence?: string | number;
  /** Utility-loop usage signal (item C): times the AI applied this saved rule. */
  usage_count?: string | number;
  /** ISO timestamp of the most recent application of this rule. */
  last_used_at?: string;
  /** ADR-0042 D4: why a pending proposal is held for review (server-composed). */
  review_reason?: string;
  /** 'true' when the flip-flop circuit breaker quarantined the subject —
   * needs human adjudication before the subject unfreezes. */
  quarantined?: string;
  /** Prior rejection provenance (ADR-0042 D3) — legacy list of mapping ids or
   * the enriched list of {mapping_id, rejected_at} dicts. */
  prior_rejections?: Array<string | { mapping_id?: string; rejected_at?: string }>;
  /** ADR-0053 E6 reopen provenance (additive) — stamped when a steward
   * re-opens a rejected row back to pending via POST .../{id}/reopen. */
  reopened_at?: string;
  reopened_by?: string;
  /** Graph node id stamped on ACCEPTED rows by the accept path — target for
   * the rule-lifecycle POSTs and the Knowledge Graph focus chip. */
  neptune_node_id?: string;
  /** B2 impact enrichment (task 2.2 pinned contract — additive):
   * provenance tier per orchestrator._provenance_tier semantics
   * (0 user/steward, 1 accepted-proposal, 2 llm/unknown). */
  provenance_tier?: number;
  /** evidence_strength × blast_radius — the pending-list sort key. */
  consequence?: number;
  /** What accepting this row would touch (task 2.2 pinned contract). */
  touches?: ProposalTouches;
  /** B3 usage-ledger evidence classes for this row's usage entry
   * ('relationship-proof' | 'tool-text' | 'route-intersection'). */
  evidence_classes?: string[];
  /** Server-computed degeneracy flags (proposal_scoring.degeneracy_flags) —
   * e.g. 'equal_literal_echo', 'same_system_same_field'. Additive: absent
   * when the row is clean. The UI renders these VERBATIM and never
   * re-derives the verdict client-side. */
  degeneracy?: string[];
  /** Server-computed advisory verdict: true when `degeneracy` is non-empty.
   * Advisory only — never disables Accept/Reject. */
  likely_low_value?: boolean;
  /** ADR-0063 D2 trust signal: did a deterministic graph check actually find
   * this row's target? PRESENT ONLY WHEN THE TARGET WAS CHECKED — absent means
   * "nobody could look" (the graph was unreachable, or the target system is
   * not onboarded), NEVER "unverified". `false` means the check RAN and did not
   * pass, which is a reason for a human to look, not a verdict that the claim
   * is wrong. */
  target_verified?: boolean;
  /** Authority scope for target_verified. Only exact `system` scope can support
   * a target-checked UI claim; absent legacy rows are non-authoritative. */
  verified_scope?: string;
  /** ADR-0063 D2 trust signal: why this pending row would NOT auto-accept,
   * verbatim from the server's single source of truth
   * (`self_learning._auto_accept_block_reason`). Absent when no block applies —
   * the UI renders it and NEVER re-derives the policy client-side. */
  auto_accept_block?: string;
  /** ADR-0063 D4 review family, COMPUTED ON READ by the server
   * (`proposal_scoring.family_key`) at grain proposed_type + method +
   * from_field→to_field. Selection aid only: the batch route never selects
   * rows BY this key — a family action always sends the explicit id list. */
  family_key?: string;
  /** Pending members of this row's family. Absent (never 0) when the family
   * has no pending peers — honest absence, same idiom as the sibling
   * advisory fields. */
  family_count?: number;
  /** Server-computed current-row action eligibility. Absence is not
   * permission: family actions exclude legacy or unenriched rows. */
  action_eligibility?: ProposalActionEligibility;
  /** ADR-0063 guidance-note authority: query hint vs answer requirement. */
  guidance_scope?: GuidanceScope;
}

/** B2 "what it touches" panel data (task 2.2 pinned contract). */
export interface ProposalTouches {
  /** Distinct endpoint systems whose routing seeds would gain this rule. */
  systems?: string[];
  concept?: string;
  /** "" when none — the P3 reverse-conflict flag rides review_reason. */
  conflict?: string;
  /** Prior superseded rows under this subject_key. */
  supersede_count?: number;
}

export interface ProposalActionEligibilityState {
  eligible: boolean;
  code?: string;
}

export interface ProposalActionEligibility {
  accept?: ProposalActionEligibilityState;
  reject?: ProposalActionEligibilityState;
}

/** One id's outcome from the D4 batch route — honest per row: `ok` false
 * carries the reason that id failed, and never implies anything about the
 * siblings in the same request. */
export interface BatchAdjudicationOutcome {
  mapping_id: string;
  ok: boolean;
  status?: string;
  error_code?: string;
}

/** POST /api/proposed-mappings/adjudicate-batch (ADR-0063 D4) — the request
 * always carries an EXPLICIT mapping_ids list; there is no predicate form. */
export interface BatchAdjudicationResult {
  action?: string;
  requested?: number;
  succeeded?: number;
  failed?: number;
  failed_ids?: string[];
  results?: BatchAdjudicationOutcome[];
  error?: string;
  detail?: string;
}

/** GET /api/proposed-mappings */
export interface ProposedMappingsResponse {
  mappings?: ProposedMapping[];
  /** Rows per exact lifecycle status over the (type-filtered) scan —
   * additive; lets a status-scoped caller label buckets it did not fetch. */
  status_counts?: Partial<Record<string, number>>;
}

/** POST /api/proposed-mappings/{id}/accept | /reject | /edit | /requeue |
 * /reopen result. The reopen route returns the updated row itself (status
 * flipped to 'pending') — its fields ride the optional `status` here. */
export interface MappingActionResult {
  status?: string;
  error?: string;
  mapping?: ProposedMapping;
  count?: number;
  /** accept-all only (additive): insight/finding proposals are never
   * bulk-accepted — how many were left pending for review. */
  skipped_insights?: number;
  /** requeue only (additive): what happened to the graph artifact the accept
   * created ('removed' | 'deactivated' | 'none'). */
  graph_artifact?: string;
  /** requeue only (additive): the review_reason stamped on the requeued row. */
  review_reason?: string;
}

/** POST /api/proposed-mappings/user-define request body. */
export interface UserDefinedMappingRequest {
  from_system: string;
  from_field: string;
  from_value: string;
  to_system: string;
  to_field: string;
  to_value: string;
  concept: string;
  proposed_type: string;
  description: string;
}

/** One system's learned guidance from GET /api/query-guidance. */
export interface QueryGuidanceSystem {
  system_id?: string;
  notes?: string[];
  /** Current guidance length (chars). */
  length?: number;
  /** Server-side cap for the guidance blob (chars). */
  max_len?: number;
}

/** GET /api/query-guidance */
export interface QueryGuidanceResponse {
  systems?: QueryGuidanceSystem[];
}

/** POST /api/query-guidance/{systemId}/prune | /clear result. */
export interface GuidanceActionResult {
  status?: string;
  error?: string;
  removed?: number;
}

/** ADR-0063 structured guidance scope. Legacy rows can omit this field. */
export type GuidanceScope = 'query_hint' | 'answer_requirement' | 'legacy_unscoped';

/** Side-effect-free guidance acceptance plan. Digests are opaque identifiers. */
export type GuidancePreviewOutcome =
  | 'append'
  | 'duplicate'
  | 'no_room'
  | 'would_evict'
  | 'unavailable';

export interface GuidancePreview {
  outcome: GuidancePreviewOutcome;
  current_digest: string | null;
  current_length: number | null;
  note_digest: string | null;
  note_length: number | null;
  scope: GuidanceScope;
  result_digest: string | null;
  result_length: number | null;
  /** Oldest-first, exact confirmation order. */
  evicted_note_digests: string[];
}

export interface GuidancePreviewResponse {
  status: 'preview';
  mapping_id: string;
  preview: GuidancePreview;
}

export interface GuidanceEvictionConfirmation {
  current_digest: string;
  /** Must byte/order-match the latest preview; no wildcard or force form. */
  evicted_note_digests: string[];
}

export interface GuidanceAcceptInput {
  mappingId: string;
  confirmation?: GuidanceEvictionConfirmation;
}

export interface GuidanceAcceptResult {
  status: 'accepted' | 'pending';
  outcome: GuidancePreviewOutcome;
  proposed_type?: 'guidance_note';
  version?: number;
  preview?: GuidancePreview;
  error?: string;
  compensated?: boolean;
  /** Which stores a failed update could not prove back to their prior state.
   * `proposal`: the proposal's own status may still read accepted. */
  drift?: { graph?: boolean; delivery?: boolean; proposal?: boolean };
  graph_digest?: string;
  delivery_digest?: string;
  delivery_version?: number;
}

export type GuidanceReconciliationStatus = 'ok' | 'mismatch' | 'unavailable';
export type GuidanceSourceStatus = 'ok' | 'unavailable';

export interface GuidanceReconciliationMismatch {
  kind: 'graph_not_accepted' | 'accepted_not_graph' | 'accepted_not_delivered';
  system_digest: string;
  note_digest: string;
}

export interface GuidanceReconciliationResult {
  status: GuidanceReconciliationStatus;
  counts: {
    accepted: number | null;
    graph: number | null;
    delivered: number | null;
  };
  sources: {
    accepted: GuidanceSourceStatus;
    graph: GuidanceSourceStatus;
    delivery: GuidanceSourceStatus;
  };
  mismatches: GuidanceReconciliationMismatch[];
  mismatch_count: number;
  truncated: boolean;
}

export interface PromoteRequirementInput {
  systemId: string;
  asserted: string;
  negated: string;
}

export interface PromoteRequirementResult {
  status: 'pending';
  mapping_id: string;
  requirement: string;
}

export interface ContextPlacementStartInput {
  systemId: string;
}

export interface ContextPlacementStartResult {
  job_id: string;
}

export interface ContextPlacementSkip {
  system_id: string;
  reason: string;
}

export interface ContextPlacementResult {
  systems_checked: number;
  terms_checked: number;
  findings: number;
  filed: number;
  resolved_swept: number;
  skipped: ContextPlacementSkip[];
  truncated: boolean;
  elapsed_seconds: number;
}

export type ContextPlacementJob =
  | { status: 'running'; started_at?: number; system_ids?: string[] | null }
  | { status: 'done'; result: ContextPlacementResult }
  | { status: 'error'; error: string; error_detail?: string };
