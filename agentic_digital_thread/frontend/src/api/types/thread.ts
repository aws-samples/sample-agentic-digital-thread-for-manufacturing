// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for the Digital Thread explorer endpoints, derived from the explorer
 * consumer usage (cross-checked against agents/thread routes).
 * All fields the JS reads defensively are optional. */

// ── /api/thread/graph-summary ────────────────────────────────────────────────

/** Identity rule — SINGLE superset definition (merged with the former
 * duplicate in api/types/onboard.ts, which re-exports it). */
export interface IdentityRule {
  id?: string;
  concept?: string;
  source_system?: string;
  source_field?: string;
  target_system?: string;
  target_field?: string;
  source_pattern?: string;
  target_pattern?: string;
  transform?: string;
  method?: string;
  status?: string;
  confidence?: number;
  /** Provenance (maintain-side audit) — producing pipeline, trigger context,
   * approver, timestamps, DynamoDB proposal back-pointer, lifecycle actors. */
  provenance?: string;
  origin_context?: string;
  /** Set by the Resolve gate when it stored the rule with no pattern because
   * the model's pattern pair could not be stored and the shared values match
   * unchanged (resolve_gates.PATTERN_ORIGIN_KEY). Server-owned. */
  pattern_origin?: string;
  approved_by?: string;
  created_at?: string;
  proposal_id?: string;
  deactivated_by?: string;
  deactivated_at?: string;
  [k: string]: unknown;
}

export interface JoinPathDetail {
  id?: string;
  from_system?: string;
  from_field?: string;
  to_system?: string;
  to_field?: string;
  description?: string;
  transform?: string;
  /** Effective lifecycle status: `deactivated` when the JoinPath itself or
   * the identity rule it was made from is switched off (kept for audit). */
  status?: string;
}

export interface ValueMappingDetail {
  concept?: string;
  from_system?: string;
  from_value?: string;
  to_system?: string;
  to_value?: string;
  confidence?: number;
}

export interface AuthorityRuleDetail {
  id?: string;
  concept?: string;
  rule?: string;
  system_id?: string;
  method?: string;
  approved_by?: string;
  evidence?: string;
  provenance?: string;
  /** Lifecycle (T1.4): soft deactivate/reactivate with audit stamps. */
  status?: string;
  deactivated_by?: string;
  deactivated_at?: string;
}

export interface CorrelationDetail {
  pattern?: string;
  confidence?: number;
}

export interface DecisionBoundaryDetail {
  concept?: string;
  threshold?: number | string;
  action?: string;
}

/** GET /api/thread/graph-summary. ``identity_rules`` and ``join_paths``
 * count the rules in use; the switched-off ones (deactivated or retired)
 * stay in the detail lists for audit and are counted in the ``_inactive``
 * fields. */
export interface GraphSummary {
  identity_rules?: number;
  identity_rules_inactive?: number;
  join_paths?: number;
  join_paths_inactive?: number;
  correlations?: number;
  rules?: IdentityRule[];
  join_paths_detail?: JoinPathDetail[];
  value_mappings_detail?: ValueMappingDetail[];
  authority_rules_detail?: AuthorityRuleDetail[];
  correlations_detail?: CorrelationDetail[];
  decision_boundaries_detail?: DecisionBoundaryDetail[];
}

// ── /api/thread/entities ─────────────────────────────────────────────────────

export interface ThreadEntity {
  id: string;
  label: string;
}

/** GET /api/thread/entities — {Batches:[{id,label},...], Equipment:[...], ...} */
export type ThreadEntities = Record<string, ThreadEntity[]>;

// ── /api/thread/resolve ──────────────────────────────────────────────────────

export interface ResolveField {
  label: string;
  value: string;
}

export interface RawSystemCard {
  system: string;
  fields: ResolveField[];
}

/** A learnable identity-rule proposal attached to a resolution step. */
export interface LearnProposal {
  source_pattern: string;
  target_pattern: string;
  confidence: number;
  /** The identifier the pattern was learned from (the one typed into the
   * trace) and the value it matched in the target system. Sent back with
   * POST /api/thread/learn, which checks both identifier readers read them
   * alike. Absent on an older server's proposal. */
  example_identifier?: string;
  example_matched_value?: string;
  [k: string]: unknown;
}

export interface ResolutionStep {
  rule: string;
  transform: string;
  check: string;
  status?: 'matched' | 'fuzzy' | 'failed' | string;
  proposal?: LearnProposal;
}

export interface UnifiedSection {
  title: string;
  fields: ResolveField[];
}

/** POST /api/thread/resolve {entity_type, identifier} */
export interface ResolveResponse {
  raw: RawSystemCard[];
  resolution: ResolutionStep[];
  unified: UnifiedSection[];
}

/** POST /api/thread/learn response. */
export interface LearnResponse {
  error?: string;
  [k: string]: unknown;
}

// ── /api/thread/analytics/* ──────────────────────────────────────────────────

export interface VocabularySource {
  source?: string;
  concepts?: number;
}

/** GET /api/thread/analytics/vocabulary */
export interface VocabularyStats {
  total_concepts?: number;
  mapped_concepts?: number;
  coverage_pct?: number;
  total_fields?: number;
  by_source?: VocabularySource[];
}

export interface CoverageConcept {
  id: string;
  name?: string;
  level?: number;
}

export interface CoverageSystem {
  id: string;
  name?: string;
}

export interface CoverageRole {
  role: string;
  confidence?: number;
}

export interface CoverageCell {
  concept: string;
  system: string;
  roles?: CoverageRole[];
}

/** GET /api/thread/analytics/concept-coverage */
export interface ConceptCoverage {
  concepts?: CoverageConcept[];
  systems?: CoverageSystem[];
  coverage?: CoverageCell[];
  unmapped_count?: number;
}

export interface ImpactField {
  name: string;
  role?: string;
}

export interface ImpactDirectSystem {
  system_id: string;
  system_name: string;
  fields: ImpactField[];
}

export interface ImpactJoinPath {
  from_sys?: string;
  to_sys?: string;
  from_field?: string;
  to_field?: string;
  description?: string;
}

export interface ImpactReachableConcept {
  concept_name: string;
  via_system: string;
  role: string;
}

export interface ImpactBoundary {
  rule?: string;
  threshold?: number | string;
  action?: string;
}

/** GET /api/thread/analytics/impact/{conceptId} */
export interface ImpactAnalysis {
  concept: { name: string };
  hop_summary?: {
    hop_1_systems?: number;
    hop_2_systems?: number;
    hop_3_concepts?: number;
  };
  direct_systems?: ImpactDirectSystem[];
  join_paths?: ImpactJoinPath[];
  reachable_concepts?: ImpactReachableConcept[];
  correlations?: CorrelationDetail[];
  boundaries?: ImpactBoundary[];
}

export interface DirectPath {
  concept?: string;
  source_pattern?: string;
  target_pattern?: string;
  confidence?: number;
}

export interface IndirectPathHop {
  via_system: string;
  concept: string;
}

export interface IndirectPath {
  aggregate_confidence?: number;
  hops?: IndirectPathHop[];
}

/** GET /api/thread/analytics/paths?from_system&to_system */
export interface PathsResponse {
  total_paths?: number;
  direct_paths?: DirectPath[];
  indirect_paths?: IndirectPath[];
}
