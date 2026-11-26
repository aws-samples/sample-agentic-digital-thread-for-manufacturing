// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for the vocabulary admin routes (agents/vocabulary_routes.py,
 * ADR-0050) — the /api/admin/vocabulary/* contract. */
import type { CardMetric } from './dashboard';

/** One row of GET /api/admin/vocabulary/concepts. */
export interface VocabularyConcept {
  id: string;
  name: string;
  description?: string;
  synonyms?: string[];
  layer?: string;
  /** Concept node `source` property (e.g. `core`, `pack:<id>`, `pack:custom:<slug>`). */
  source?: string;
  /** Derived display label: core | pack:<id> | pack:custom:<slug> | minted | custom. */
  provenance_label?: string;
  mapped_field_count?: number;
  usage_30d?: number;
  last_used_at?: string | null;
  retired?: boolean;
  [k: string]: unknown;
}

/** GET /api/admin/vocabulary/concepts. */
export interface VocabularyConceptsResponse {
  concepts?: VocabularyConcept[];
  total?: number;
}

/** POST /concepts and PATCH /concepts/{id} response — the ADR-0003
 * lifecycle assertion result. */
export interface ConceptAssertionResult {
  status?: 'accepted' | 'pending' | string;
  mapping_id?: string;
  /** Authoring-time evidence score; null when no evidence was checked. */
  evidence_score?: number | null;
}

/** POST /api/admin/vocabulary/concepts body. */
export interface AddConceptInput {
  /** Optional — the server derives a slug from `name` when absent. */
  id?: string;
  name: string;
  description: string;
  synonyms?: string[];
  evidence_system?: string;
  evidence_field?: string;
  /** Informed user override of a low evidence score (recorded verbatim). */
  override_evidence?: boolean;
}

/** PATCH /api/admin/vocabulary/concepts/{id} — `conceptId` is used in the
 * URL only; the id is immutable and must NEVER appear in the body (400). */
export interface EditConceptInput {
  conceptId: string;
  name?: string;
  description?: string;
  synonyms?: string[];
}

/** POST /concepts/{id}/retire response (409 {error} when mapped). */
export interface RetireConceptResult {
  status?: string;
}

/** One row skipped by the normalized-id collision guard (C6, additive). */
export interface VocabularyImportCollision {
  /** The incoming pack concept id that was skipped. */
  id: string;
  /** The existing graph id (or earlier in-batch id) it normalizes into. */
  existing_id: string;
  reason: string;
}

/** POST /api/admin/vocabulary/import response. */
export interface VocabularyImportResult {
  status?: string;
  slug?: string;
  inserted?: number;
  skipped?: number;
  failed?: number;
  /** EXACT-id skips only — collision skips are reported in `collisions`. */
  skipped_ids?: string[];
  /** Rows skipped by the normalized-id collision guard (absent on old servers). */
  collisions?: VocabularyImportCollision[];
}

/** Per-row basis tag of POST /import/preview (C7). */
export type VocabularyImportPreviewAction = 'insert' | 'skip_exact' | 'skip_collision';

/** One row of the POST /api/admin/vocabulary/import/preview dry-run. */
export interface VocabularyImportPreviewRow {
  id: string;
  name: string;
  action: VocabularyImportPreviewAction;
  /** The graph id (or earlier in-batch id) a skip_collision row collides with. */
  collides_with: string | null;
}

/** POST /api/admin/vocabulary/import/preview response — zero graph writes. */
export interface VocabularyImportPreview {
  status?: string;
  slug?: string;
  rows?: VocabularyImportPreviewRow[];
  insert_count?: number;
  skip_count?: number;
  collision_count?: number;
}

/** One row of GET /api/admin/vocabulary/retirement-candidates (C8). */
export interface RetirementCandidate {
  id: string;
  name: string;
  source?: string;
  mapped_field_count?: number;
  usage_30d?: number;
  last_used_at?: string | null;
  /** Human-readable rule clauses that made this concept a candidate. */
  reasons?: string[];
}

/** GET /api/admin/vocabulary/retirement-candidates — report-only sweep. */
export interface RetirementCandidatesResponse {
  candidates?: RetirementCandidate[];
}

// ── Metric definitions (agents/vocabulary_routes.py, ADR-0065) ───────────────
// GET /metrics, POST /metrics/propose, GET /metrics/{mapping_id}/preview.
// The server owns every judgement: the rendered formula, the field-level diff
// and the validation verdict are COMPUTED there and rendered verbatim here.

/** Metric-definition kinds (metric_layer.MetricKind). */
export type MetricDefinitionKind = 'direct' | 'derived';

/** Row status on GET /metrics — the four visible provenance classes:
 * `code` (registry-defined, read-only), `accepted` (live graph definition),
 * `pending` (filed, awaiting the steward's review-queue decision), and
 * `shadowed` (a graph row the store excluded — `reason` says why). */
export type MetricDefinitionStatus = 'code' | 'accepted' | 'pending' | 'shadowed';

/** One row of GET /api/admin/vocabulary/metrics.
 *
 * `definition` is the normalized C1 document for a parseable row and the RAW
 * stored string for a pending row that no longer parses (kept visible rather
 * than dropped); `reason` is present on shadowed rows and on unparseable
 * pending rows. */
export interface MetricDefinitionRow {
  id: string;
  label: string;
  kind: MetricDefinitionKind | '';
  unit: string;
  /** Server-rendered one-line formula (metric_definition.render_formula). */
  formula: string;
  definition?: Record<string, unknown> | string | null;
  source: string;
  status: MetricDefinitionStatus | string;
  editable?: boolean;
  /** Present on pending rows — the proposal this row was filed as. */
  mapping_id?: string;
  /** Why a row is shadowed, or why a pending row failed to parse. */
  reason?: string;
}

/** GET /api/admin/vocabulary/metrics — composed on read, ZERO writes. */
export interface MetricDefinitionsResponse {
  metrics?: MetricDefinitionRow[];
  total?: number;
}

/** POST /api/admin/vocabulary/metrics/propose response. There is deliberately
 * no accepted outcome: acceptance stays behind the existing proposal-queue
 * door, so the only success status is `pending`. */
export interface MetricDefinitionProposeResult {
  status?: 'pending' | string;
  mapping_id?: string;
}

/** Field-level change entry of the preview diff. */
export interface MetricDefinitionFieldChange {
  from: unknown;
  to: unknown;
}

/** The computed structural diff (metric_definition.diff_definitions) —
 * deterministic and pure; never an LLM verdict. */
export interface MetricDefinitionDiff {
  is_new: boolean;
  added: Record<string, unknown>;
  removed: Record<string, unknown>;
  changed: Record<string, MetricDefinitionFieldChange>;
  changed_fields: string[];
  /** Rendered formula before/after; `from` is null when the metric is new. */
  formula: { from: string | null; to: string };
}

/** Current-universe validation verdict carried by the preview. */
export interface MetricDefinitionValidation {
  valid: boolean;
  error: string;
}

/** GET /api/admin/vocabulary/metrics/{mapping_id}/preview — read-only.
 * `diff` is null when the stored proposal no longer parses (the raw string is
 * then returned as `proposed`); 404/409 surface as an ApiError, not this shape. */
export interface MetricDefinitionPreview {
  current?: Record<string, unknown> | null;
  proposed?: Record<string, unknown> | string;
  diff?: MetricDefinitionDiff | null;
  validation?: MetricDefinitionValidation;
  /** True when the proposed id collides with a code-defined metric (C5). */
  collision?: boolean;
  /** ADR-0080: present only when the preview was requested with `resolve=1`. */
  dry_run?: MetricDefinitionDryRun;
}

/** Measured-only cost of one dry run (ADR-0053): value-query count and, per
 * system, engine-reported bytes scanned — absent when not reported. Never an
 * estimate, never dollars. */
export interface MetricDryRunCost {
  query_count: number;
  systems: Array<{ system_id: string; query_count: number; scanned_bytes?: number }>;
}

/** ADR-0080 dry-run block — the card-metric entry the pending definition
 * would render as, stamped `preview: true`. `status` is the dashboard card
 * register (ok / no-data / unavailable / invariant-violation /
 * unresolved-predicate / ambiguous-binding) plus `unresolved-input` and
 * `invalid`; `metric_id`
 * is null only when the stored proposal no longer parses. */
export interface MetricDefinitionDryRun extends Omit<CardMetric, 'metric_id'> {
  preview: true;
  metric_id: string | null;
  window: string | null;
  computed_at: string;
  cost: MetricDryRunCost;
}

/** POST /api/admin/vocabulary/metrics/propose body — a C1 document. Only the
 * fields the form authors are spelled out; the server is the validator. */
export interface MetricDefinitionInput {
  id: string;
  kind: MetricDefinitionKind;
  label: string;
  synonyms?: string[];
  unit?: string;
  /** Direct only. */
  concept?: string;
  /** Direct only. */
  aggregation?: string;
  /** Derived only — the `{op, inputs[]}` tree. */
  formula?: unknown;
  [k: string]: unknown;
}

/** The flat custom-pack import shape (client-side validated before POST). */
export interface VocabularyImportPack {
  metadata: { name: string; version?: string; description?: string; [k: string]: unknown };
  concepts: Array<{ id: string; name: string; description?: string }>;
}
