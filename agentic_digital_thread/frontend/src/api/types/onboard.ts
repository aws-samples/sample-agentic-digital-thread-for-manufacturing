// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboarding API types — derived from the onboarding client usage
 * (router cross-check: agents/onboard.py). Shapes are permissive (index
 * signatures) because the source treats responses as loosely-typed JSON. */

/** One entry of GET /api/onboard/catalog. */
export interface CatalogSystem {
  id?: string;
  system_id?: string;
  name: string;
  system_type?: string;
  plant?: string;
  connection_type?: string;
  connection_string?: string;
  schema?: string;
  connection_config?: Record<string, unknown>;
  status?: string;
  secret_arn?: string;
  has_secret?: boolean;
  /** 'user' for a Connect New System registration, 'demo' for a bundled one. */
  connection_origin?: string;
  /** Of a 'removal_failed' system: the mode of the removal that did not
   * finish ('full' forgets the registration, 'default' keeps it), so
   * "Remove again" finishes it the same way, also after a restart. */
  removal_mode?: 'full' | 'default';
  [k: string]: unknown;
}

/** GET /api/onboard/catalog — array OR {systems:[...]} (both shapes live). */
export type CatalogResponse =
  | CatalogSystem[]
  | { systems?: CatalogSystem[]; error?: string };

export interface DiscoveredColumn {
  name: string;
  [k: string]: unknown;
}

export interface DiscoveredTable {
  name: string;
  row_count?: number;
  column_count?: number;
  columns?: DiscoveredColumn[];
  [k: string]: unknown;
}

/** POST /api/onboard/discover response. */
export interface DiscoverResponse {
  error?: string;
  tables?: DiscoveredTable[];
  ai_analysis?: string;
  schema_name?: string;
  [k: string]: unknown;
}

/** A ranked concept candidate on a mapping (may carry a suggestion basis). */
export interface ConceptCandidate {
  concept?: string;
  confidence?: number;
  basis?: string;
}

/** One field→concept mapping row (map step / mappings review). */
export interface FieldMapping {
  field?: string;
  table?: string;
  concept?: string;
  confidence?: number;
  role?: string;
  concepts?: ConceptCandidate[];
  mappings?: { role?: string }[];
  also?: string[];
  candidates?: ConceptCandidate[];
  suggestion_basis?: string;
  [k: string]: unknown;
}

export interface ProposedConcept {
  id?: string;
  name?: string;
  description?: string;
}

/** POST /api/onboard/map — returns a job id. */
export interface JobStartResponse {
  error?: string;
  job_id?: string;
  [k: string]: unknown;
}

// ── E1 preflight (ADR-0053, pinned Task 1.2 contract) ────────────────────────

export type PreflightVerdict = 'ready' | 'degraded' | 'blocked';

/** Connection probe block of the preflight result. `reason` is the probe
 * reason enum (e.g. assume_role_denied); `detail` is actionable and carries
 * no ARN/host/secret echo (the probe_connection posture). */
export interface PreflightConnection {
  status?: 'pass' | 'fail';
  reason?: string;
  detail?: string;
  latency_ms?: number;
  [k: string]: unknown;
}

/** One bounded per-table sample-read outcome. `detail` is an error CLASS
 * only — never values or rows. */
export interface PreflightTableCheck {
  name?: string;
  sample_read?: 'ok' | 'failed' | 'skipped';
  detail?: string;
  [k: string]: unknown;
}

export interface PreflightCompleteness {
  tables_visible?: number;
  tables_sample_read?: number;
  columns_discovered?: number;
  /** "ok" | "skipped:<reason>" | "n/a" (Athena physical-column discovery). */
  physical_columns?: string;
  [k: string]: unknown;
}

/** GET /api/onboard/preflight/{jobId} `result` block. */
export interface PreflightResult {
  system_id?: string;
  verdict?: PreflightVerdict;
  connection?: PreflightConnection;
  tables?: PreflightTableCheck[];
  completeness?: PreflightCompleteness;
  warnings?: string[];
  elapsed_seconds?: number;
  [k: string]: unknown;
}

/** GET /api/onboard/preflight/{jobId}. On error jobs the honest sanitized
 * line is surfaced AS `error` (wave-1 amendment — the UI reads job.error). */
export interface PreflightStatusResponse {
  status?: string;
  error?: string;
  result?: PreflightResult;
  [k: string]: unknown;
}

/** Wave 2 (item 4, contract C5): additive vocabulary-fit block on the map
 * result. Absent on older backends ⇒ the frontend renders no banner. */
export interface VocabFit {
  total_columns: number;
  rows_emitted: number;
  mapped_confident: number;
  needs_review: number;
  reconciled: number;
  distinct_concepts: number;
  vocabulary_size: number;
  coverage_pct: number;
}

/** GET /api/onboard/map/status/{jobId}. */
export interface MapStatusResponse {
  status?: string;
  error?: string;
  mappings?: FieldMapping[];
  proposed_concepts?: ProposedConcept[];
  batch?: number;
  total_batches?: number;
  mappings_so_far?: number;
  vocab_fit?: VocabFit;
  [k: string]: unknown;
}

/** Identity rule — alias of the single superset definition in
 * api/types/thread.ts (DRY type merge; same wire shape). */
import type { IdentityRule } from './thread';
export type { IdentityRule };

/** Measured cross-system value overlap (resolve evidence). */
export interface ResolveEvidence {
  concept?: string;
  source_field?: string;
  target_system?: string;
  target_field?: string;
  overlap_count?: number;
  /** How many of the shared values are the same whole value in both fields
   * (onboard._unchanged_count): what the gate judges a rule that reads values
   * unchanged on. Server-built. */
  unchanged_count?: number;
  /** Every shared value the measurement kept, for the server's gates; never rendered. */
  examples?: string[];
  /** The examples a page shows, as they are: the server's display policy
   * (resolve_evidence.prompt_values) leaves out a value over 256 characters
   * or holding a control or format character. */
  shown_examples?: string[];
  ratio?: number;
  /** The concept the target field sits under when it is declared related to
   * `concept` (narrower or broader), not the same concept. */
  related_concept?: string;
}

/** What a translation rule's pattern made of the new side's sampled values
 * on a measured pair (resolve_gates.TRANSFORM_CHECKS_KEY): how many
 * translated values the target holds. Server-built. */
export interface TransformCheck {
  concept?: string;
  source_field?: string;
  target_system?: string;
  target_field?: string;
  source_pattern?: string;
  target_pattern?: string;
  overlap_count?: number;
  /** The translated values found, for the server's gates; never rendered. */
  examples?: string[];
  /** The examples a page shows, as they are (see ResolveEvidence). */
  shown_examples?: string[];
  ratio?: number;
}

/** Coverage-gap recommendation (resolve step). */
export interface CoverageRecommendation {
  concept?: string;
  severity?: string;
  message?: string;
}

/** Wave 4 (item E): one cross-system join candidate discovered during the
 * resolve step, offered for optional accept/reject through the EXISTING
 * /api/proposed-mappings/{id}/accept|reject routes. */
export interface JoinCandidate {
  mapping_id?: string;
  from?: string;
  to?: string;
  proposed_type?: string;
  transform?: string;
  confidence?: string | number;
}

/** GET /api/onboard/resolve/status/{jobId}. */
/** An identity-rule candidate the resolve evidence gates withheld
 * (``resolve_gates.gate_overlap_rules``): the rule plus why it was dropped. */
export interface RejectedIdentityRule extends IdentityRule {
  rejected_reason?: string;
}

export interface ResolveStatusResponse {
  status?: string;
  error?: string;
  rules?: IdentityRule[];
  /** Candidates the evidence gates dropped, with reasons (may be absent on
   * older servers). */
  rejected_rules?: RejectedIdentityRule[];
  evidence?: ResolveEvidence[];
  /** Translation checks the evidence gate judged translation rules on. */
  transform_checks?: TransformCheck[];
  recommendations?: CoverageRecommendation[];
  join_candidates?: JoinCandidate[];
  /** This job's id, which the provision sends back so the server can find
   * the rules this result listed (absent on older servers). */
  resolve_job_id?: string;
  [k: string]: unknown;
}

/** What proved a join path (``jp.basis``, agents/connection_evidence.py).
 * Absent on a join path written before evidence was recorded. */
export type JoinBasis =
  | 'declared_foreign_key'
  | 'column_name_pattern'
  | 'measured_reference'
  | 'value_overlap'
  | 'same_row_identifiers'
  | 'identity_rule';

export interface JoinPath {
  id?: string;
  from_system?: string;
  from_table?: string;
  from_field?: string;
  to_system?: string;
  to_table?: string;
  to_field?: string;
  method?: string;
  description?: string;
  /** Evidence (absent on older join paths): what proved the join. */
  basis?: JoinBasis | string;
  /** Declared foreign key: the constraint's name. */
  constraint_name?: string;
  /** Shared values: how many, and the share of the smaller sampled column
   * (0..1). An identity rule's join carries the rule's shared-value count. */
  overlap_count?: number;
  overlap_ratio?: number;
  /** Same-row pair: distinct value pairs that read one to one. */
  pair_count?: number;
  /** Same-row pair: both columns carry a UNIQUE constraint. */
  unique_constraints?: boolean;
  /** Identity-rule join: the rule it came from and that rule's method. */
  rule_id?: string;
  rule_method?: string;
  /** Measured reference: distinct values of this column, all in the target
   * column, and the target column's rows, each a different value. */
  reference_count?: number;
  key_count?: number;
  /** Measured reference: when those were read (UTC, ISO 8601). */
  measured_at?: string;
  /** Column-name guess: why onboarding left it a guess (agents/onboard.py). */
  reference_check?: string;
  transform?: string;
}

export interface ValueMapping {
  from_system?: string;
  from_value?: string;
  to_system?: string;
  to_value?: string;
  concept?: string;
  confidence?: number;
  method?: string;
}

/** One identity rule of the provision result with the check's reason: one
 * it did not write (``rejected_details``, ``onboard._reject_identity_rule``)
 * or one it wrote without the live re-check (``unchecked_details``,
 * ``onboard._note_unchecked_rule``). */
export interface ProvisionRuleReason {
  rule?: IdentityRule;
  reason?: string;
}

/** GET /api/onboard/provision/status/{jobId}. */
export interface ProvisionStatusResponse {
  status?: string;
  error?: string;
  system_id?: string;
  /** Things the provision could not check, e.g. a user PostgreSQL login whose
   * privileges could not be read (so it was not refused). */
  warnings?: string[];
  /** Fields of the system mapped to a concept, read back after provision. */
  mapped_fields?: number;
  /** Cloud agents only: the AgentCore runtime and gateway target names. */
  runtime_name?: string;
  gateway_target?: string;
  /** Seconds the provision step took on the server. */
  provision_seconds?: number;
  /** A retryable failure the server explains (e.g. "busy": another change to
   * the system's connection was in progress). */
  reason?: string;
  /** The server's message for a retryable failure; show it as written. */
  detail?: string;
  retryable?: boolean;
  neptune_nodes_created?: number;
  identity_rules_created?: number;
  /** Catalog confirmed completion after the provision job result was lost. */
  stats_unavailable?: boolean;
  identity_rules_rejected?: number;
  /** Each rule provision did not write, with the reason the check gave
   * (may be absent on older servers or a recovered job record). */
  rejected_details?: ProvisionRuleReason[];
  /** Rules written without the live re-check (a read that failed, a target
   * that is not PostgreSQL, a bare `*`), each with why. */
  identity_rules_unchecked?: number;
  unchecked_details?: ProvisionRuleReason[];
  tool_name?: string;
  /** The provision run that just finished; the reconcile panel compares
   * against the concepts THIS run created. */
  provision_run_id?: string;
  join_paths?: JoinPath[];
  value_mappings?: ValueMapping[];
  join_paths_written?: number;
  value_mappings_written?: number;
  [k: string]: unknown;
}

/** One join path on the system detail page (GET /api/onboard/connections). */
export interface ConnectionJoinPath extends JoinPath {
  confidence?: number;
  origin_context?: string;
}

export interface ConnectionValueMapping extends ValueMapping {
  id?: string;
  from_field?: string;
  to_field?: string;
}

export interface ConnectionIdentityRule {
  id?: string;
  concept?: string;
  source_system?: string;
  source_field?: string;
  source_pattern?: string;
  target_system?: string;
  target_field?: string;
  target_pattern?: string;
  method?: string;
  status?: string;
  confidence?: number;
  origin_context?: string;
  provenance?: string;
  approved_by?: string;
}

/** GET /api/onboard/connections/{systemId}: each list cut at its limit and
 * reported with the total it was cut from. A failed read carries `error`. */
export interface SystemConnectionsResponse {
  system_id?: string;
  error?: string;
  join_paths?: ConnectionJoinPath[];
  join_paths_total?: number;
  value_mappings?: ConnectionValueMapping[];
  value_mappings_total?: number;
  identity_rules?: ConnectionIdentityRule[];
  identity_rules_total?: number;
  limits?: { join_paths?: number; value_mappings?: number; identity_rules?: number };
}

/** POST /api/onboard/verify response. `check` names how the system was
 * checked: `database` (PostgreSQL: tables listed, rows totalled, so
 * `rows_counted` is true) or `knowledge_graph` (every other type: its System
 * node and the entities and `fields` provision wrote; `rows_counted` false and
 * no `total_rows`). `tables_accessible` is the tables or graph entities found. */
export interface VerifyResponse {
  error?: string;
  system_id?: string;
  verified?: boolean;
  check?: 'database' | 'knowledge_graph';
  connection_type?: string;
  rows_counted?: boolean;
  tables_accessible?: number;
  total_rows?: number;
  fields?: number;
  [k: string]: unknown;
}

/** The verify step's agent test (agents/onboard_verify_agent.py): the new
 * system's own query tool, called through the gateway. `step` names where a
 * failure happened. `no_read`: the tool answered, but no captured statement
 * confirmed it read the named table (not a pass, not a failure of a step).
 * `rows`: for `query_kind` sql the query's own count; for question the
 * agent's answer (the capture proves the read, not the count), or null when
 * the answer gave no number. */
export interface AgentTestResult {
  outcome: 'passed' | 'failed' | 'not_run' | 'no_read';
  step?: 'runtime' | 'gateway_target' | 'tool' | null;
  reason?: string | null;
  system_id?: string;
  tool?: string | null;
  table?: string | null;
  rows?: number | null;
  query_kind?: 'question' | 'sql' | null;
  /** False for system types whose agent records no statement (API types,
   * the built-in hub): their `no_read` is expected, not a warning. */
  read_provable?: boolean | null;
  elapsed_s?: number;
  deadline_s?: number;
}

/** POST /api/onboard/verify/agent-test response. */
export interface AgentTestStartResponse {
  job_id?: string;
  status?: string;
  deadline_s?: number;
  error?: string;
}

/** GET /api/onboard/verify/agent-test/{job_id} response. */
export interface AgentTestStatusResponse {
  status?: 'running' | 'done';
  result?: AgentTestResult;
  deadline_s?: number;
  error?: string;
}

/** POST /api/onboard/reconcile suggestion; accepted rows POST to /remap. */
export interface RemapSuggestion {
  field_id?: string;
  concept_id?: string;
  role?: string;
  confidence?: number;
  old_concept?: string;
  /** Display fields the reconcile check adds; never sent back to /remap. */
  concept_name?: string;
  reasoning?: string;
  system_id?: string;
  system_name?: string;
  table?: string;
  column?: string;
  current_tags?: { concept_id: string; role?: string }[];
}

/** POST /api/onboard/reconcile response. */
export interface ReconcileResponse {
  suggestions?: RemapSuggestion[];
  system_id?: string;
  reason?: string;
  message?: string;
  candidates_considered?: number;
  candidates_sent?: number;
  /** The candidate graph scan hit its hard bound, so the considered count is
   * a sample, not the full total. */
  candidates_truncated?: boolean;
}

/** POST /api/onboard/remap response: `written` / `not_written` are field ids. */
export interface RemapResponse {
  remapped?: number;
  written?: string[];
  not_written?: string[];
  error?: string;
}

export interface ChatTurn {
  role: 'user' | 'assistant';
  content: string;
}

/** POST /api/onboard/chat request body. */
export interface OnboardChatBody {
  step: string;
  system_id: string;
  data: Record<string, unknown>;
  message: string;
  history: ChatTurn[];
}

/** PUT /api/admin/scan-budget/{systemId} body. */
export interface ScanBudgetPayload {
  max_scanned_bytes: number | null;
}

/** Per-connection-type editable field descriptor (admin edit form). */
export interface ConnField {
  key: string;
  label: string;
  secret: boolean;
  placeholder?: string;
  /** A read-only display field (a Secrets Manager secret-sourced connection
   * shows its base URL / workspace / ARN read-only). Never submitted. */
  readOnly?: boolean;
  /** A fixed set of allowed values: the Edit form renders a select, not free text. */
  options?: string[];
  /** Display text per option value (the value itself when absent). */
  optionLabels?: Record<string, string>;
}

// ── Business context (ADR-0054 contract C4a — field names pinned verbatim) ──

/** GET /api/admin/business-context/{systemId}. Unset context is the honest
 * empty shape: `business_context: ""`, `enabled: false`, nulls for the rest. */
export interface BusinessContextResponse {
  system_id?: string;
  business_context: string;
  enabled: boolean;
  updated_at: string | null;
  context_hash: string | null;
}

/** PUT /api/admin/business-context/{systemId} body (pinned keys; `""` clears). */
export interface BusinessContextPayload {
  business_context: string;
  enabled: boolean;
}

/** T1.5 batched-roll coordinator per-system readiness entry (additive). */
export interface RollReadinessEntry {
  status?: 'ok' | 'failed' | 'skipped';
  /** Warm-up verification: READY alone is not proof the runtime serves
   * traffic (ADR-0056), so the coordinator probes after READY. */
  readiness?: 'ready_verified' | 'ready_unverified' | 'not_ready' | null;
  detail?: string | null;
}

/** PUT /api/admin/business-context/{systemId} 200 response. `runtime_refresh`
 * reports the best-effort agent refresh honestly — a `failed` refresh never
 * rolls back the durable write. `readiness` (additive, T1.5) carries the
 * batched-roll coordinator's per-system report; absent/null on backends or
 * managers without the coordinator. */
export interface BusinessContextSaveResponse {
  status?: string;
  system_id?: string;
  enabled?: boolean;
  context_hash?: string | null;
  runtime_refresh?: 'ok' | 'failed' | 'skipped';
  readiness?: Record<string, RollReadinessEntry> | null;
}
