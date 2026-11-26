// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for the admin routes (agents/admin_routes.py), derived from the
 * admin-page client usage. */

/** GET/PUT /api/admin/settings — demo on/off gate (fail-safe: treat unknown as off). */
export interface AdminSettings {
  status?: string;
  demo_enabled?: boolean;
  [k: string]: unknown;
}

/** GET/POST /api/admin/live-updates — runtime pause/resume of background auto-updates.
 * `interval_seconds` is the EFFECTIVE snapshot cadence (override → env → 120s default). */
export interface LiveUpdatesState {
  status?: string;
  live_updates_enabled?: boolean;
  interval_seconds?: number;
  [k: string]: unknown;
}

/** GET/PUT /api/admin/feedback-exploration — runtime toggle for the
 * feedback-triggered background exploration (default OFF). `source` is the
 * provenance of the effective value: 'env-kill' (ops kill switch active —
 * the admin flag cannot override), 'admin-flag', or 'default'. */
export interface FeedbackExplorationState {
  status?: string;
  message?: string;
  feedback_exploration_enabled?: boolean;
  source?: 'env-kill' | 'admin-flag' | 'default' | (string & {});
  env_kill_active?: boolean;
  [k: string]: unknown;
}

/** One entry of the curated model catalog (GET /api/admin/models) — the
 * SINGLE definition for this endpoint; api/types/agents.ts re-exports it as
 * `CatalogModel` (superset merge of the two former duplicates). */
export interface ModelCatalogEntry {
  id: string;
  label: string;
  /** Models not enabled in the region are annotated but still selectable. */
  available?: boolean;
  /** Present only when the deployment has a model allowlist (ALLOWED_MODELS):
   * false for a model outside it, shown disabled; the server refuses to save
   * it. Absent means selectable. */
  allowed?: boolean;
  /** Pricing fields (read by shared/agents/pricing.ts). */
  input_per_1k?: number;
  output_per_1k?: number;
  [k: string]: unknown;
}

/** GET /api/admin/models — curated catalog per role. */
export interface ModelCatalog {
  orchestrator?: ModelCatalogEntry[];
  agent?: ModelCatalogEntry[];
  /** The AgentCore Memory extraction models (agents/model_catalog.py
   * MEMORY_EXTRACTION_FAMILIES); `default` marks the deploy default. */
  memory?: ModelCatalogEntry[];
  /** true when the deployment limits the selectable models (ALLOWED_MODELS);
   * absent when it does not. */
  allowlist_active?: boolean;
  [k: string]: unknown;
}

/** GET /api/admin/llm-settings — currently-configured models. */
export interface LlmSettings {
  orchestrator_model?: string;
  agent_model?: string;
  /** The memory's extraction model id, 'mixed' when its strategies differ,
   * '' when unknown (no memory, or the read failed). */
  memory_model?: string;
  /** false (with a reason) on the workshop, without a memory, or when the
   * allowlist offers no memory model. */
  memory_model_selectable?: boolean;
  memory_model_locked_reason?: string;
  /** Public error text when the memory could not be read. */
  memory_model_error?: string;
  /** Present when SSM /adt/llm-memory-model, as the next deploy resolves it
   * for this Region, names another model than the memory runs now. */
  memory_model_saved?: string;
  /** The saved text (bounded) when it is not a memory model this deployment
   * offers, so the next deploy ignores it. Never drift. */
  memory_model_saved_invalid?: string;
  /** true when nothing valid is saved and the memory runs another model
   * than the one the next deploy would put back, `memory_model_next_deploy`
   * (the stack default for this Region). */
  memory_model_unsaved?: boolean;
  memory_model_next_deploy?: string;
}

/** A memory extraction model switch job: POST
 * /api/admin/llm-settings/memory-model (202, running) and GET
 * /api/admin/llm-settings/memory-model/job[/{job_id}]. `none`: no switch to
 * report. A worker that stopped reads as `error` with `stale`. */
export interface MemoryModelJob {
  /** `unknown`: the switch ended without a recorded outcome (or its task
   * stopped); the page reads the memory again. */
  status: 'running' | 'done' | 'error' | 'unknown' | 'none';
  job_id?: string;
  /** The model the job switches the memory to. */
  memory_model?: string;
  /** false: the memory already ran it (only SSM was written). */
  memory_model_changed?: boolean;
  message?: string;
  started_at?: number;
  finished_at?: number;
  stale?: boolean;
}

/** PUT /api/admin/llm-settings response. */
export interface LlmSaveResult {
  status?: string;
  message?: string;
  restart_required?: boolean;
  reonboard_required?: boolean;
  /** Per-outcome system ids from the in-place agent-model switch. */
  agent_runtime_refresh?: {
    ok?: string[];
    failed?: string[];
    skipped?: string[];
    unchanged?: string[];
    error?: string;
  };
}

/** One industry concept pack (GET /api/admin/concept-packs). */
export interface ConceptPack {
  id: string;
  name: string;
  is_installed?: boolean;
  concepts_installed?: number;
  concepts_in_file?: number;
}

export interface ConceptPacksResponse {
  packs?: ConceptPack[];
}

/** POST /api/admin/concept-packs/{id}/install response. */
export interface PackInstallResult {
  status?: string;
  concepts_inserted?: number;
  detail?: string;
  message?: string;
}

/** POST /api/admin/concept-packs/{id}/uninstall response. */
export interface PackUninstallResult {
  status?: string;
  deleted?: number;
  preserved_due_to_mappings?: number;
  detail?: string;
  message?: string;
}

/** GET /api/admin/quick-links — URLs (or absent) + boolean token-configured flags. */
export interface QuickLinks {
  /** True only when the i3X Explorer web build is served at /i3x-explorer/. */
  i3x_explorer?: boolean;
  highbyte?: string;
  highbyte_i3x_token?: boolean;
  erp_dashboard?: string;
  console_start?: string;
  sitewise_i3x_url?: string;
  sitewise_i3x_token?: boolean;
  quick_desktop?: string;
}

/** GET /api/admin/i3x-explorer-link/{which} — one-shot tokenized URL mint. */
export interface I3xLinkResult {
  status?: string;
  href?: string;
  message?: string;
}

/** Generic {status, message} admin mutation response (branding, highbyte-token…). */
export interface AdminStatusResult {
  status?: string;
  message?: string;
  [k: string]: unknown;
}

/** GET /api/admin/feedback[?sentiment=] — thumbs feedback records. */
export interface FeedbackRecord {
  timestamp?: string;
  user_id?: string;
  sentiment?: string;
  user_message?: string;
  user_comment?: string;
}

export interface FeedbackResponse {
  records?: FeedbackRecord[];
}

// ── Conversations admin tab (agents/admin_conversations.py) ─────────────────
// Every user's web-chat questions and answers from AgentCore Memory. The
// strings are user/model text: the tab renders them as text (questions) or
// through <Md/> (answers, react-markdown, no raw HTML), never as HTML.

/** A memory actor and the Cognito login it resolved to (`resolved: false`
 * leaves `username`/`email` null and the page shows `user_id`). */
export interface ConversationUser {
  user_id: string;
  username: string | null;
  email: string | null;
  resolved: boolean;
}

export interface ConversationFeedback {
  sentiment?: 'positive' | 'negative' | (string & {});
  comment?: string;
  at?: string | null;
}

export interface ConversationTurn {
  question: string;
  asked_at: string;
  /** The final answer text; null when none was saved or it is withheld. */
  answer: string | null;
  answered_at: string | null;
  /** The response-integrity gate ran for this turn; its answer is not stored. */
  answer_withheld: boolean;
  question_truncated: boolean;
  answer_truncated: boolean;
  feedback: ConversationFeedback | null;
}

export interface ConversationSession {
  session_id: string;
  user: ConversationUser;
  started_at: string;
  last_activity_at: string | null;
  turn_count: number;
  turns: ConversationTurn[];
  /** Thumbs counted per turn; null when ratings could not be read. */
  rating: { positive: number; negative: number } | null;
  /** Rated answers that could not be matched to exactly one question. */
  unplaced_ratings: number;
  /** Rated answers not shown: their newest read rating is from a day before a
   * day the feedback bound cut, so a newer rating may not have been read. */
  unverified_ratings: number;
  /** More events than the per-session bound: the OLDEST turns are missing. */
  events_truncated: boolean;
}

/** A fixed public error (never upstream text) with its log reference. */
export interface ConversationsPublicError {
  error: string;
  error_category?: string;
  error_ref?: string;
}

/** GET /api/admin/conversations[?days=&user=&limit=&cursor=] — one page. */
export interface ConversationsPage {
  sessions: ConversationSession[];
  /** Opaque cursor for the next page; null on the last one. */
  next: string | null;
  matched: number;
  /** What the listing covers: the app's current pool users (plus a typed
   * user id filter). `complete` is relative to this scope. */
  scope: 'current_app_users' | (string & {});
  /** Every listed user's sessions were read: only then are the order and
   * the count claims true for the whole set. */
  complete: boolean;
  /** Users whose sessions were not all read in this request. */
  incomplete_users: ConversationUser[];
  window: { days: number; start: string; end: string };
  users: ConversationUser[];
  logins_available: boolean;
  feedback_available: boolean;
  feedback_error: ConversationsPublicError | null;
  more: { users: boolean; feedback: boolean };
  excluded: { automated: number; undated: number };
  limits: {
    max_users: number;
    max_session_pages: number;
    session_page_size: number;
    max_events_per_session: number;
    max_page_size: number;
    max_days: number;
    max_feedback_pages_per_day: number;
    feedback_page_size: number;
  };
}

/** GET /api/admin/guardrails — guardrail violation log. */
export interface GuardrailViolation {
  timestamp?: string;
  user_id?: string;
  source?: string;
  action?: string;
  content_preview?: string;
}

export interface GuardrailsResponse {
  violations?: GuardrailViolation[];
}

/** GET /api/admin/dashboard — usage KPI stats. */
export interface AdminDashboardStats {
  total_requests?: number;
  total_tokens?: number;
  unique_users?: number;
  avg_tokens_per_request?: number;
  period_days?: number;
}

// ── Evals admin tab (agents/eval_routes.py — server-side golden lane) ────────
// Shapes mirror the pinned eval-report row (agents/eval_core.py): the report
// is DATA — the frontend renders every field as literal text, never markdown.

/** Suite totals block (eval_core._suite_totals). */
export interface EvalSuiteTotals {
  pass?: number;
  fail?: number;
  skipped?: number;
  total?: number;
  avg_latency_seconds?: number | null;
  max_latency_seconds?: number | null;
}

/** Which KIND of golden set produced a run (ADR-0064 D3). The customer set is
 * NEVER stored server-side: it is uploaded per run and the report keeps only
 * the sha, the ids, the verdicts and the failures. */
export type EvalGoldenSetSource = 'packaged' | 'uploaded' | (string & {});

/** One run summary row from GET /api/admin/evals/runs (newest-first, ≤50). */
export interface EvalRunSummary {
  run_id: string;
  started_at?: string | null;
  finished_at?: string | null;
  status?: 'running' | 'complete' | 'failed' | (string & {});
  /** "server" (internal chat client) vs "cli" — latencies are incomparable. */
  run_path?: string;
  variant?: string | null;
  suite?: EvalSuiteTotals;
  golden_set_sha?: string | null;
  /** ADR-0064 D3 provenance — "packaged" (the demo set) vs "uploaded" (a
   * steward's own set, run once from its bytes). A row badges without
   * fetching the report, so a customer run can never read as a demo run. */
  golden_set_source?: EvalGoldenSetSource;
  question_count?: number;
}

/** GET /api/admin/evals/runs envelope. */
export interface EvalRunsResponse {
  runs?: EvalRunSummary[];
}

/** Pinned C2 per-row honesty block (present on every question row). */
export interface EvalHonestyBlock {
  absence_markers_matched?: string[];
  fabrication_flags?: string[];
}

/** Per-question metrics from the intelligence_summary (eval_core._metrics_block).
 * Honest-data rule: null = not measured (never coerced to zero). */
export interface EvalQuestionMetrics {
  total_tool_calls?: number | null;
  neptune_queries?: number | null;
  system_queries?: number | null;
  graph_coverage_pct?: number | null;
  wasted_calls?: number | null;
  deterministic_pct?: number | null;
  grounded_steps?: number | null;
  rules_applied_count?: number | null;
  new_proposed_mappings?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  cost_latency_ms?: number | null;
  planned_with_present?: boolean;
  planned_with_authority_rules?: number | null;
  [k: string]: unknown;
}

/** One per-question report row (eval_core._scored_row / _skipped_row shape). */
export interface EvalReportQuestion {
  id: string;
  session_id?: string | null;
  verdict?: 'pass' | 'fail' | 'skipped' | (string & {});
  failures?: string[];
  skip_reason?: string;
  transport_failure?: boolean;
  systems_queried?: number;
  neptune_queries?: number;
  elapsed_seconds?: number | null;
  answer_excerpt?: string;
  honesty?: EvalHonestyBlock;
  metrics?: EvalQuestionMetrics | null;
  [k: string]: unknown;
}

/** One per-id entry of the informational A/B trend block (ab_diff shape). */
export interface EvalTrendQuestion {
  id: string;
  status?: 'compared' | 'new' | 'missing' | (string & {});
  previous_verdict?: string | null;
  verdict?: string | null;
  transition?: 'regression' | 'improvement' | 'stable' | (string & {});
  [k: string]: unknown;
}

/** The informational A/B trend block — it NEVER gates (ADR-0053 D-E2). */
export interface EvalTrendBlock {
  informational?: boolean;
  variant?: string | null;
  previous_variant?: string | null;
  comparable?: boolean;
  questions?: EvalTrendQuestion[];
  suite?: {
    pass_previous?: number;
    pass_current?: number;
    regressions?: number;
    improvements?: number;
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

/** The full persisted eval report (eval_core.build_eval_report). */
export interface EvalReport {
  base_url?: string;
  run_id?: string;
  golden_set?: string;
  strict?: boolean;
  /** REQUIRED in the persisted shape — "server" for tab runs, "cli" otherwise. */
  run_path?: string;
  variant?: string | null;
  golden_set_sha?: string | null;
  golden_set_source?: EvalGoldenSetSource;
  /** true on an UPLOADED run: rows carry NO answer-derived text (ADR-0064 D5)
   * — excerpts are empty, fabrication flags are labels only. Distinct from the
   * runner's `answer_excerpts_dropped` size guard; never conflate them. */
  answer_text_redacted?: boolean;
  suite?: EvalSuiteTotals;
  questions?: EvalReportQuestion[];
  trend?: EvalTrendBlock;
  [k: string]: unknown;
}

/** GET /api/admin/evals/runs/{run_id} — status/progress while running, the
 * full report when complete, an error string when failed. */
export interface EvalRunDetail {
  status?: 'running' | 'complete' | 'failed' | (string & {});
  progress?: { current?: number; total?: number };
  report?: EvalReport;
  error?: string;
}

/** POST /api/admin/evals/run → 202 response (409 carries {detail}).
 *
 * POST /api/admin/evals/run-upload returns the same shape plus the three
 * upload fields (ADR-0064 D3). Its refusals carry `{detail: {reason, detail}}`
 * — 413 payload_too_large, 422 invalid_json / invalid_golden_set /
 * too_many_questions / unknown_query_params, 400 secret_like_content,
 * 409 active_run (the SAME one-run lock as the packaged route). */
export interface EvalRunStartResult {
  run_id?: string;
  status?: string;
  golden_set_source?: EvalGoldenSetSource;
  /** sha256[:12] of the EXACT uploaded bytes — compare it against
   * `eval_core.golden_file_sha(<their file>)` to prove what was scored. */
  golden_set_sha?: string;
  question_count?: number;
}

// ── Review queue (agents/eval_queue_routes.py, ADR-0062 D3/D4/D5) ────────────
// The queue is COMPUTED ON READ: question/answer/comment text is joined live
// into a transient item and is never persisted. Only the steward's decision
// block comes from storage.

/** The steward's three verdicts — the server pins the same enum (422 else). */
export type EvalQueueVerdict = 'correct' | 'incorrect' | 'needs_context';

/** Deterministic machine pre-score. TRIAGE ONLY (ADR-0062 D4): `label` is the
 * literal "triage-only" and `signals` are stable ids — never a verdict, never
 * a score, never a confidence. */
export interface EvalMachinePrescore {
  label?: string;
  signals?: string[];
}

/** The persisted steward decision as the queue renders it. */
export interface EvalQueueDecision {
  verdict?: EvalQueueVerdict | (string & {});
  taxonomy_tags?: string[];
  steward_note?: string;
  decided_at?: string;
  decided_by?: string;
  ref_sha?: string;
}

/** One computed queue item. `source_expired: true` is a NORMAL state (the
 * source turn aged out of its retention window): the decision is kept and the
 * text fields come back null — never an error. */
export interface EvalQueueItem {
  source_ref: string;
  ref_sha?: string;
  source?: 'feedback' | 'golden-failure' | (string & {});
  occurred_at?: string | null;
  sentiment?: string | null;
  question?: string | null;
  answer_excerpt?: string | null;
  user_comment?: string;
  cited_systems?: string[];
  run_id?: string;
  question_id?: string;
  run_path?: string;
  failures?: string[];
  machine_prescore?: EvalMachinePrescore;
  decision?: EvalQueueDecision | null;
  source_expired?: boolean;
  [k: string]: unknown;
}

/** GET /api/admin/evals/queue envelope. */
export interface EvalQueueResponse {
  items?: EvalQueueItem[];
  window_days?: number;
  limit?: number;
  counts?: {
    total?: number;
    decided?: number;
    undecided?: number;
    source_expired?: number;
  };
}

/** POST /queue/decision body — `extra="forbid"` server-side. */
export interface EvalDecisionInput {
  source_ref: string;
  verdict: EvalQueueVerdict;
  taxonomy_tags?: string[];
  note?: string;
}

/** POST /queue/decision → the recorded row. */
export interface EvalDecisionResult {
  status?: string;
  decision?: EvalQueueDecision;
}

/** One question inside a promoted golden draft. Deliberately carries NO
 * `answer_must_match` (ADR-0062 D5) and no steward identity. */
export interface GoldenDraftQuestion {
  id?: string;
  text?: string;
  expect?: Record<string, unknown>;
  hints?: Record<string, number>;
  provenance?: { source?: string; source_ref_sha?: string };
}

/** POST /queue/golden-draft → a TRANSIENT one-question golden-set document.
 * It is downloaded to the steward's external, customer-owned golden set and
 * is never retained in browser storage or the query cache. */
export interface GoldenDraftBundle {
  version?: string | number;
  questions?: GoldenDraftQuestion[];
}

// ── Findings ledger + weekly digest + bounded triage annotation (ADR-0062) ───
// The durable finding row is MINIMIZED (taxonomy + opaque refs + status +
// timestamps + measured usage). Generated free text — claim excerpt, rationale,
// suggested mapping — lives ONLY in the 7-day customer-derived detail row and
// resolves ONLY through GET /findings/{id} (D6). Every field renders as a
// literal text node; findings text is DATA, never markdown.

/** MEASURED token usage, or the whole block is ABSENT — never zeros, never a
 * money figure (ADR-0053: cost is measured-only). */
export interface EvalMeasuredUsage {
  input_tokens: number;
  output_tokens: number;
}

/** One minimized index entry from GET /api/admin/evals/findings — refs only. */
export interface FindingIndexEntry {
  finding_id: string;
  taxonomy?: string;
  source_report_sha?: string;
  question_ref?: string;
  created_at?: string;
}

/** GET /api/admin/evals/findings — taxonomy-grouped ledger (computed on read). */
export interface FindingsLedgerResponse {
  total?: number;
  groups?: Record<string, FindingIndexEntry[]>;
  counts?: Record<string, number>;
}

/** GET /api/admin/evals/findings?window=7d — the read-time weekly digest.
 * `ux_gap_refs` are UNRESOLVED (finding_id + question_ref only, ≤20): the
 * which-screens signal, deliberately without clustering or resolution. */
export interface FindingsDigest {
  window_days?: number;
  total?: number;
  taxonomy_counts?: Record<string, number>;
  ux_gap_refs?: { finding_id?: string; question_ref?: string }[];
  ux_gap_refs_truncated?: boolean;
}

/** The 7-day CUSTOMER-DERIVED detail block (eval-finding-detail row). */
export interface FindingDetailBlock {
  data_class?: string;
  claim_excerpt?: string;
  rationale?: string;
  suggested_mapping?: Record<string, string>;
}

/** A steward annotation (separate scope — survives re-import byte-identical). */
export interface FindingStewardAnnotation {
  finding_id?: string;
  annotated_at?: string;
  annotated_by?: string;
  status?: string;
  steward_note?: string;
}

/** GET /api/admin/evals/findings/{id} — the minimized row joined with its
 * detail IF unexpired. `detail_expired: true` is a NORMAL state, never an
 * error, and it makes the finding unfileable. */
export interface FindingRow {
  finding_id: string;
  taxonomy?: string;
  source_report_sha?: string;
  question_ref?: string;
  source_ref_sha?: string;
  run_path?: string;
  status?: string;
  created_at?: string;
  usage?: EvalMeasuredUsage;
  detail?: FindingDetailBlock | null;
  detail_expired?: boolean;
  steward?: FindingStewardAnnotation | null;
}

/** POST /findings/import — replace-not-accrete counters. `discarded_invalid`
 * and `rejected_fields` are what the server THREW AWAY; they are surfaced
 * verbatim rather than folded into the success count. */
export interface FindingsImportResult {
  source_report_sha?: string;
  written?: number;
  deleted?: number;
  discarded_invalid?: number;
  rejected_fields?: Record<string, number>;
  taxonomy_counts?: Record<string, number>;
  usage_bumped?: number;
}

/** POST /findings/{id}/annotate. */
export interface FindingAnnotateResult {
  status?: string;
  annotation?: FindingStewardAnnotation;
}

/** POST /findings/{id}/file-proposal — steward-triggered filing. `gated` means
 * the learning pipeline absorbed it and nothing landed for review. */
export interface FindingProposalResult {
  status?: 'filed' | 'gated' | (string & {});
  finding_id?: string;
  mapping_id?: string | null;
  annotation?: FindingStewardAnnotation;
}

/** One bounded-triage-annotation run (`audit-run` row). */
export interface AuditRunSummary {
  run_id: string;
  trigger?: 'manual' | 'scheduled' | (string & {});
  status?: 'running' | 'complete' | 'failed' | 'skipped_budget' | (string & {});
  counts?: Record<string, number>;
  usage?: EvalMeasuredUsage;
  started_at?: string | null;
  finished_at?: string | null;
}

/** Today's global UTC-day budget consumption (manual + scheduled COMBINED).
 * `durable: false` means the in-process fallback counter is in play. */
export interface AuditBudgetBlock {
  day_key?: string;
  claimed?: number;
  cap?: number;
  durable?: boolean;
}

/** GET /api/admin/evals/audit/status — the annotator's read path. It lands
 * BEFORE the annotator itself, so an unavailable route renders an honest
 * unavailable line and an empty `runs` list renders "never ran" — neither is
 * ever backfilled with invented numbers. */
export interface AuditStatusResponse {
  scheduled_enabled?: boolean;
  runs?: AuditRunSummary[];
  budget?: AuditBudgetBlock;
  telemetry?: Record<string, number>;
}
