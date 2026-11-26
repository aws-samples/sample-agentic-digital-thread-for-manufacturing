// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for the dashboard payloads (routers: agents/dashboard.py,
 * agents/card_*.py, agents/entity_render.py, agents/metric_*.py).
 * Shapes derived from the dashboard client usage (the spec),
 * cross-checked against the FastAPI routers. All fields optional/tolerant —
 * the client is presence-driven and fail-soft by design. */

/** Lineage record on a resolved metric — 'direct' chain or 'derived' tree. */
export interface Lineage {
  kind?: 'direct' | 'derived' | string;
  metric_id?: string;
  op?: string;
  inputs?: Lineage[];
  authority?: { system_id?: string };
  mapping?: { table?: string; column?: string };
  concept?: string;
  executed_at?: string;
  [k: string]: unknown;
}

/** One per-site contribution on a multi-site metric (Task 2A, conditional
 * `sites` key — present only when >1 onboarded system covers the concept). */
export interface CardMetricSite {
  site?: string;
  system_id?: string;
  system_name?: string;
  value?: unknown;
  status?: string; // 'ok' | 'error' | 'no-data'
}

/** One metric entry on a declarative card (ADR 0018). */
export interface CardMetric {
  metric_id?: string;
  label?: string;
  /** 'ok' | error states | 'no-data' (Task 2A: resolved but zero samples). */
  status?: string;
  value?: unknown;
  unit?: string;
  error?: string;
  system_id?: string;
  lineage?: Lineage | null;
  /** Non-null sample count behind a scalar resolve (Task 2A, conditional). */
  sample_count?: number | null;
  /** Per-site breakdown (Task 2A, conditional — only for multi-site cards). */
  sites?: CardMetricSite[];
  /** Why a value is WITHHELD (invariant-violation / unresolved-predicate /
   * ambiguous-binding / unresolved-input / invalid) — names what is missing,
   * never a proxy. */
  detail?: string;
  /** ADR-0080 preview only: the derived inputs that resolve nowhere. */
  missing_inputs?: string[];
}

/** Ordered threshold band: value cutoff (null = catch-all) + color token. */
export interface ThresholdBand {
  value?: number | null;
  color?: string;
}

/** Server drill hint — (system_id, table) target for the detail modal. */
export interface DrillHint {
  system_id?: string;
  table?: string;
  entity_label?: string;
}

/** Declarative resolver KPI card (ADR 0018). */
export interface DashboardCard {
  card_id?: string;
  title?: string;
  /** 'uncovered' | resolved states | 'no-data' (Task 2A/2B: covered but the
   * source returned zero samples — routed to the strip, never a dead tile). */
  status?: string;
  viz_type?: string; // 'kpi' | 'gauge' | 'bar' | ...
  group_by?: string;
  resolver?: string;
  metrics?: CardMetric[];
  thresholds?: ThresholdBand[];
  target?: number;
  drill?: DrillHint | null;
  context?: { system?: string; site?: string; cell?: string };
  missing_concepts?: string[];
  suggestion?: string;
  suggestions?: string[];
  correctable?: boolean;
}

/** GET /api/dashboard and the SSE 'dashboard' snapshot — cards at top level. */
export interface DashboardPayload {
  connected_types?: string[];
  cards?: DashboardCard[];
}

export interface EntityColumn {
  column?: string;
  label?: string;
}

/** One (system, table) row-shaped section from /api/dashboard/sections. */
export interface EntitySection {
  system_id?: string;
  table?: string;
  entity_label?: string;
  status?: string;
  error?: string;
  columns?: EntityColumn[];
  rows?: Array<unknown[] | Record<string, unknown>>;
}

export interface EntitySectionsResponse {
  sections?: EntitySection[];
}

export interface LineStateSiteState {
  state?: string;
  count?: number;
}

export interface LineStateSite {
  site?: string;
  name?: string;
  system_id?: string;
  states?: LineStateSiteState[];
}

/** GET /api/dashboard/line-state. */
export interface LineStatePayload {
  status?: string;
  error?: string;
  /** status === 'uncovered' (see DimensionPayload). */
  missing_concepts?: string[];
  detail?: string;
  sites?: LineStateSite[];
  source_system_types?: string[];
}

/** One plant on a multi-plant resolve-by-dimension payload (ADR-0094 per-site
 * envelope, `agents/metric_resolver.py::_grouped_sites_envelope`): each site
 * answered in its own register. `rows` is that site's row COUNT. */
export interface DimensionSite {
  site?: string;
  system_id?: string;
  system_name?: string;
  status?: 'ok' | 'uncovered' | 'error' | string;
  rows?: number;
  /** status === 'uncovered': the concept that site does not map. */
  missing?: string;
}

/** POST /api/metrics/resolve-by-dimension response. */
export interface DimensionPayload {
  kind?: string;
  group_by?: string;
  /** Metric label — renderCountBar's value-column fallback. */
  label?: string;
  /** Value unit suffix for COUNT/quantity dimension bars. */
  unit?: string;
  columns?: EntityColumn[];
  rows?: unknown[][];
  status?: string;
  error?: string;
  /** Present (true) only for data-model constraints no retry can fix —
   * the UI drops the Retry affordance for these. Absent = retryable. */
  structural?: boolean;
  /** status === 'uncovered': concepts no onboarded system maps yet. */
  missing_concepts?: string[];
  /** status === 'uncovered': technical cause for the details expander. */
  detail?: string;
  /** Null on a multi-plant payload: the rows are site-prefixed and `sites`
   * names each plant's source system instead. */
  system_id?: string | null;
  lineage?: Lineage | null;
  /** Multi-plant only (ADR-0094): one entry per plant, in payload order. */
  sites?: DimensionSite[];
}

/** POST /api/metrics/resolve-heatmap response —
 * rows are [d1, d2, value, threshold, severity]. */
export interface HeatmapPayload {
  kind?: string;
  dims?: string[];
  columns?: EntityColumn[];
  rows?: unknown[][];
  threshold_status?: string;
  status?: string;
  error?: string;
  missing_concepts?: string[];
  suggestion?: string;
}

export interface ActiveIssue {
  severity?: string; // closed vocabulary: 'critical' | 'warning'
  title?: string;
  detail?: string;
  source_system?: string;
}

/** GET /api/dashboard/active-issues. */
export interface ActiveIssuesPayload {
  kind?: string;
  issues?: ActiveIssue[];
  status?: string;
  error?: string;
}

export interface CoverageGap {
  concept?: string;
  suggestion?: string;
}

/** GET /api/coverage. */
export interface CoveragePayload {
  cards?: unknown[];
  gaps?: CoverageGap[];
  summary?: { uncovered?: number; total?: number };
}

/** MQTT message (SSE 'mqtt' event / GET /api/iot/recent). */
export interface IotMessage {
  timestamp?: number | string;
  topic?: string;
  payload?: unknown;
}

/** Last subscriber connect/setup failure (GET /api/iot/stats `last_error`). */
export interface IotLastError {
  message?: string;
  timestamp?: string;
}

/** GET /api/iot/stats (also the SSE 'stats' event). Lifecycle fields (Task 1C,
 * additive) distinguish "not configured" vs "started, can't connect" vs
 * "connected, silent" — LiveFeed's honest empty states key off them. */
export interface IotStats {
  enabled?: boolean;
  endpoint_configured?: boolean;
  started?: boolean;
  connected?: boolean;
  last_error?: IotLastError | null;
  total_messages?: number;
  unique_topics?: number;
  buffer_size?: number;
}

/** GET /api/dashboard-cards entry (pinned prompt cards). */
export interface PinnedCard {
  card_id?: string;
  title?: string;
  status?: string; // 'running' | done states
  prompt_text?: string;
  display_text?: string;
  result_markdown?: string;
  result_video_url?: string;
  last_executed?: string;
  error?: string;
}

/** SSE 'card_update' event (S3.2 re-derivation ripple). */
export interface CardUpdatePayload {
  status?: 'updating' | 'updated' | string;
  card_ids?: string[];
  cards?: DashboardCard[];
  audit?: { text?: string };
}

/** GET /api/systems entry. */
export interface ConnectedSystemInfo {
  id?: string;
  name?: string;
  type?: string;
  status?: string;
  plant?: string;
}

export interface SystemsResponse {
  systems?: ConnectedSystemInfo[];
}

/** GET /api/prompts entry (demo mode reads registry prompts). */
export interface RegistryPrompt {
  source?: string;
  text?: string;
  title?: string;
  display_text?: string;
}
