// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { mutationOptions, queryOptions, type QueryClient } from '@tanstack/react-query';

import { requestJson } from '../../api/client';
import { queryJson } from '../../api/query-client';
import { queryKeys } from '../../api/query-keys';
import type {
  AdminSettings,
  AdminStatusResult,
  AuditStatusResponse,
  ConceptPacksResponse,
  EvalDecisionInput,
  EvalDecisionResult,
  EvalQueueResponse,
  EvalRunDetail,
  EvalRunsResponse,
  EvalRunStartResult,
  FeedbackExplorationState,
  FindingAnnotateResult,
  FindingProposalResult,
  FindingRow,
  FindingsDigest,
  FindingsImportResult,
  FindingsLedgerResponse,
  GoldenDraftBundle,
  I3xLinkResult,
  LiveUpdatesState,
  LlmSaveResult,
  LlmSettings,
  ModelCatalog,
  PackInstallResult,
  PackUninstallResult,
  QuickLinks,
} from '../../api/types/admin';
import type { VersionInfo } from '../../api/types/core';
import type {
  MappingActionResult,
  ProposedMapping,
  ProposedMappingsResponse,
  UserDefinedMappingRequest,
} from '../../api/types/proposals';
import type {
  AddConceptInput,
  ConceptAssertionResult,
  EditConceptInput,
  RetireConceptResult,
  RetirementCandidatesResponse,
  VocabularyConceptsResponse,
  VocabularyImportPack,
  VocabularyImportPreview,
  VocabularyImportResult,
} from '../../api/types/vocabulary';

const SECOND = 1_000;
const MINUTE = 60 * SECOND;

export const ADMIN_QUERY_POLICY = {
  settings: { staleTime: MINUTE, refetchInterval: false as const },
  models: { staleTime: 10 * MINUTE, refetchInterval: 5 * MINUTE },
  liveUpdates: { staleTime: 30 * SECOND, refetchInterval: false as const },
  feedbackExploration: { staleTime: 30 * SECOND, refetchInterval: false as const },
  llmSettings: { staleTime: 5 * MINUTE, refetchInterval: false as const },
  conceptPacks: { staleTime: 2 * MINUTE, refetchInterval: false as const },
  vocabularyConcepts: { staleTime: MINUTE, refetchInterval: false as const },
  vocabularyRetirementCandidates: { staleTime: MINUTE, refetchInterval: false as const },
  vocabularyValueMappings: { staleTime: 30 * SECOND, refetchInterval: false as const },
  quickLinks: { staleTime: 5 * MINUTE, refetchInterval: false as const },
  version: { staleTime: 10 * MINUTE, refetchInterval: false as const },
  evalRuns: { staleTime: 30 * SECOND, refetchInterval: false as const },
  evalQueue: { staleTime: 30 * SECOND, refetchInterval: false as const },
} as const;

/** Per-run detail poll cadence — applied ONLY while status === 'running'
 * (a finished run's report is immutable, so polling it would be waste). */
export const EVAL_RUN_POLL_MS = 5 * SECOND;

function commonPolicy(enabled: boolean) {
  return {
    enabled,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
  } as const;
}

export function adminSettingsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.settings(),
    queryFn: ({ signal }) => queryJson<AdminSettings>('/api/admin/settings', signal),
    ...ADMIN_QUERY_POLICY.settings,
    ...commonPolicy(enabled),
  });
}

export function adminModelsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.models(),
    queryFn: ({ signal }) => queryJson<ModelCatalog>('/api/admin/models', signal),
    ...ADMIN_QUERY_POLICY.models,
    ...commonPolicy(enabled),
  });
}

export function adminLiveUpdatesQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.liveUpdates(),
    queryFn: ({ signal }) => queryJson<LiveUpdatesState>('/api/admin/live-updates', signal),
    ...ADMIN_QUERY_POLICY.liveUpdates,
    ...commonPolicy(enabled),
  });
}

export function adminFeedbackExplorationQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.feedbackExploration(),
    queryFn: ({ signal }) =>
      queryJson<FeedbackExplorationState>('/api/admin/feedback-exploration', signal),
    ...ADMIN_QUERY_POLICY.feedbackExploration,
    ...commonPolicy(enabled),
  });
}

export function adminLlmSettingsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.llmSettings(),
    queryFn: ({ signal }) => queryJson<LlmSettings>('/api/admin/llm-settings', signal),
    ...ADMIN_QUERY_POLICY.llmSettings,
    ...commonPolicy(enabled),
  });
}

export function adminConceptPacksQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.conceptPacks(),
    queryFn: ({ signal }) => queryJson<ConceptPacksResponse>('/api/admin/concept-packs', signal),
    ...ADMIN_QUERY_POLICY.conceptPacks,
    ...commonPolicy(enabled),
  });
}

export function adminQuickLinksQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.quickLinks(),
    queryFn: ({ signal }) => queryJson<QuickLinks>('/api/admin/quick-links', signal),
    ...ADMIN_QUERY_POLICY.quickLinks,
    ...commonPolicy(enabled),
  });
}

export function adminVersionQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.version(),
    queryFn: ({ signal }) => queryJson<VersionInfo>('/api/version', signal),
    ...ADMIN_QUERY_POLICY.version,
    ...commonPolicy(enabled),
  });
}

/** The complete authorized-admin bootstrap list. useQueries starts every query in one render. */
export function adminBootstrapQueryOptions(enabled: boolean) {
  return [
    adminSettingsQueryOptions(enabled),
    adminModelsQueryOptions(enabled),
    adminLiveUpdatesQueryOptions(enabled),
    adminFeedbackExplorationQueryOptions(enabled),
    adminLlmSettingsQueryOptions(enabled),
    adminConceptPacksQueryOptions(enabled),
    adminQuickLinksQueryOptions(enabled),
    adminVersionQueryOptions(enabled),
  ] as const;
}

function jsonBody(method: 'POST' | 'PUT' | 'PATCH', body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

function requireOk<T extends { status?: string; message?: string }>(result: T, fallback: string): T {
  if (result.status !== 'ok') throw new Error(result.message || fallback);
  return result;
}

export function saveDemoSettingMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (demoEnabled: boolean) => requireOk(
      await requestJson<AdminSettings>(
        '/api/admin/settings',
        jsonBody('PUT', { demo_enabled: demoEnabled }),
      ),
      'Server did not confirm the demo setting',
    ),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.settings() }),
  });
}

/** POST /api/admin/live-updates — the flag plus (optionally) the snapshot
 * interval in seconds; both persist through SSM and take effect immediately. */
export interface SaveLiveUpdatesPayload {
  enabled: boolean;
  intervalSeconds?: number;
}

export function saveLiveUpdatesMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (payload: SaveLiveUpdatesPayload) => requireOk(
      await requestJson<LiveUpdatesState>(
        '/api/admin/live-updates',
        jsonBody('POST', {
          live_updates_enabled: payload.enabled,
          ...(payload.intervalSeconds != null
            ? { interval_seconds: payload.intervalSeconds }
            : {}),
        }),
      ),
      'Server did not confirm the live-updates setting',
    ),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.liveUpdates() }),
  });
}

/** PUT /api/admin/feedback-exploration — the runtime admin flag. A 409
 * (env kill switch active) surfaces through ApiError; the tab disables the
 * checkbox under an active kill so this normally never fires then. */
export function saveFeedbackExplorationMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (feedbackExplorationEnabled: boolean) => requireOk(
      await requestJson<FeedbackExplorationState>(
        '/api/admin/feedback-exploration',
        jsonBody('PUT', { feedback_exploration_enabled: feedbackExplorationEnabled }),
      ),
      'Server did not confirm the feedback-exploration setting',
    ),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.feedbackExploration() }),
  });
}

export interface BrandingMutationInput {
  company: string;
  tagline: string;
}

export function saveBrandingMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (branding: BrandingMutationInput) => requireOk(
      await requestJson<AdminStatusResult>('/api/admin/branding', jsonBody('PUT', branding)),
      'Server did not confirm the branding update',
    ),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.branding() }),
  });
}

export function saveHighByteTokenMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (token: string) => requireOk(
      await requestJson<AdminStatusResult>(
        '/api/admin/highbyte-token',
        jsonBody('PUT', { token }),
      ),
      'Error saving token',
    ),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.quickLinks() }),
  });
}

export interface LlmMutationInput {
  orchestrator_model: string;
  agent_model: string;
}

export function saveLlmSettingsMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (models: LlmMutationInput) => requireOk(
      await requestJson<LlmSaveResult>('/api/admin/llm-settings', jsonBody('PUT', models)),
      'Server did not confirm the model update',
    ),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.llmSettings() }),
  });
}

function packPath(packId: string, action: 'install' | 'uninstall'): string {
  return `/api/admin/concept-packs/${encodeURIComponent(packId)}/${action}`;
}

function requirePackOk<T extends { status?: string; detail?: string; message?: string }>(result: T, fallback: string): T {
  if (result.status !== 'ok') throw new Error(result.detail || result.message || fallback);
  return result;
}

export function installConceptPackMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (packId: string) => requirePackOk(
      await requestJson<PackInstallResult>(packPath(packId, 'install'), { method: 'POST' }),
      'Error installing concept pack',
    ),
    onSuccess: () => Promise.all([
      client.invalidateQueries({ queryKey: queryKeys.admin.conceptPacks() }),
      client.invalidateQueries({ queryKey: queryKeys.concepts() }),
    ]),
  });
}

export function uninstallConceptPackMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (packId: string) => requirePackOk(
      await requestJson<PackUninstallResult>(packPath(packId, 'uninstall'), { method: 'POST' }),
      'Error uninstalling concept pack',
    ),
    onSuccess: () => Promise.all([
      client.invalidateQueries({ queryKey: queryKeys.admin.conceptPacks() }),
      client.invalidateQueries({ queryKey: queryKeys.concepts() }),
    ]),
  });
}

export function mintI3xLinkMutationOptions() {
  return mutationOptions({
    mutationFn: (which: 'highbyte' | 'sitewise') =>
      requestJson<I3xLinkResult>('/api/admin/i3x-explorer-link/' + which),
  });
}

// ── Vocabulary admin tab (agents/vocabulary_routes.py, ADR-0050) ─────────────

const VOCABULARY_BASE = '/api/admin/vocabulary';

export function vocabularyConceptsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.vocabularyConcepts(),
    queryFn: ({ signal }) =>
      queryJson<VocabularyConceptsResponse>(`${VOCABULARY_BASE}/concepts`, signal),
    ...ADMIN_QUERY_POLICY.vocabularyConcepts,
    ...commonPolicy(enabled),
  });
}

/** GET /retirement-candidates — the deterministic report-only sweep (C8). */
export function retirementCandidatesQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.vocabularyRetirementCandidates(),
    queryFn: ({ signal }) =>
      queryJson<RetirementCandidatesResponse>(
        `${VOCABULARY_BASE}/retirement-candidates`,
        signal,
      ),
    ...ADMIN_QUERY_POLICY.vocabularyRetirementCandidates,
    ...commonPolicy(enabled),
  });
}

/** Every vocabulary mutation can change both the concept list and the
 * retirement-candidate sweep — invalidate the pair together. */
function invalidateVocabulary(client: QueryClient) {
  return Promise.all([
    client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyConcepts() }),
    client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyRetirementCandidates() }),
  ]);
}

/** POST /concepts — human assertion (op add) through the ADR-0003 lifecycle.
 * The response carries the lifecycle status (accepted|pending) and any
 * authoring-time evidence score — no `status: 'ok'` envelope to check. */
export function addConceptMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (input: AddConceptInput) =>
      requestJson<ConceptAssertionResult>(`${VOCABULARY_BASE}/concepts`, jsonBody('POST', input)),
    onSuccess: () => invalidateVocabulary(client),
  });
}

/** PATCH /concepts/{id} — the id travels in the URL ONLY (immutable; a body
 * carrying `id` is a contract-level 400). */
export function editConceptMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: ({ conceptId, ...body }: EditConceptInput) =>
      requestJson<ConceptAssertionResult>(
        `${VOCABULARY_BASE}/concepts/${encodeURIComponent(conceptId)}`,
        jsonBody('PATCH', body),
      ),
    onSuccess: () => invalidateVocabulary(client),
  });
}

/** POST /concepts/{id}/retire — soft retire; a mapped concept is refused with
 * 409 {error} (surfaced through ApiError.detail). */
export function retireConceptMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (conceptId: string) =>
      requestJson<RetireConceptResult>(
        `${VOCABULARY_BASE}/concepts/${encodeURIComponent(conceptId)}/retire`,
        { method: 'POST' },
      ),
    onSuccess: () => invalidateVocabulary(client),
  });
}

/** POST /import — bulk flat-pack import (the sanctioned pack-install path). */
export function importVocabularyMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (pack: VocabularyImportPack) =>
      requestJson<VocabularyImportResult>(`${VOCABULARY_BASE}/import`, jsonBody('POST', pack)),
    onSuccess: () => invalidateVocabulary(client),
  });
}

/** POST /import/preview — dry-run classification of a pack (C7). ZERO graph
 * writes on the server, so nothing to invalidate on success. */
export function previewVocabularyImportMutationOptions() {
  return mutationOptions({
    mutationFn: (pack: VocabularyImportPack) =>
      requestJson<VocabularyImportPreview>(
        `${VOCABULARY_BASE}/import/preview`,
        jsonBody('POST', pack),
      ),
  });
}

// Metric definitions (ADR-0065) live in ./metric-queries.ts (focused module).

// ── Vocabulary artifact surfaces (W4.1) ──────────────────────────────────────
// ValueMapping proposals + query-guidance editor for the Vocabulary tab.
// EXISTING routes only: GET /api/proposed-mappings (filtered client-side to
// value_mapping), POST /api/proposed-mappings/user-define (open — human
// origination), POST /api/proposed-mappings/{id}/accept|reject (steward-gated,
// 403 for non-admins), GET /api/query-guidance and its steward-gated
// /prune + /clear, plus the ADR-0050 POST
// /api/admin/vocabulary/guidance/{sid}/propose (open lifecycle).
// Query keys come from the central api/query-keys.ts factory
// (queryKeys.admin.vocabularyValueMappings / vocabularyGuidance).

/** True when a proposal row is a value mapping (absent type defaults to
 * value_mapping — same normalization as SlCards). */
export function isValueMappingProposal(mapping: ProposedMapping): boolean {
  return (mapping.proposed_type || 'value_mapping') === 'value_mapping';
}

export function valueMappingProposalsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.vocabularyValueMappings(),
    queryFn: async ({ signal }) => {
      const data = await queryJson<ProposedMappingsResponse>('/api/proposed-mappings?proposed_type=value_mapping', signal);
      return (data.mappings ?? []).filter(isValueMappingProposal);
    },
    ...ADMIN_QUERY_POLICY.vocabularyValueMappings,
    ...commonPolicy(enabled),
  });
}

function invalidateValueMappings(client: QueryClient) {
  return client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyValueMappings() });
}


/** The lifecycle routes confirm with a specific `status` value; anything else
 * carries a server-composed `error` message. */
function requireLifecycleStatus<T extends { status?: string; error?: string }>(
  result: T,
  expected: string,
  fallback: string,
): T {
  if (result.status !== expected) throw new Error(result.error || fallback);
  return result;
}

/** POST /api/proposed-mappings/user-define — human-asserted value mapping.
 * OPEN to any authenticated user (NOT steward-gated). A steward's assertion
 * enters `accepted` at once; anyone else's is filed `pending` for steward
 * review. Either status is a success; the caller words the result. */
export function assertValueMappingMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (input: UserDefinedMappingRequest) => {
      const result = await requestJson<MappingActionResult>(
        '/api/proposed-mappings/user-define',
        jsonBody('POST', input),
      );
      if (result.status === 'pending') return result;
      return requireLifecycleStatus(result, 'accepted', 'Server did not confirm the value mapping');
    },
    onSuccess: () => invalidateValueMappings(client),
  });
}

/** POST /api/proposed-mappings/{id}/accept — steward-gated (admin-only, 403
 * otherwise); the UI hides the action for non-admins. */
export function acceptProposalMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (mappingId: string) =>
      requireLifecycleStatus(
        await requestJson<MappingActionResult>(
          `/api/proposed-mappings/${encodeURIComponent(mappingId)}/accept`,
          { method: 'POST' },
        ),
        'accepted',
        'Server did not confirm the accept',
      ),
    onSuccess: () => invalidateValueMappings(client),
  });
}

/** POST /api/proposed-mappings/{id}/reject — steward-gated like accept. */
export function rejectProposalMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (mappingId: string) =>
      requireLifecycleStatus(
        await requestJson<MappingActionResult>(
          `/api/proposed-mappings/${encodeURIComponent(mappingId)}/reject`,
          { method: 'POST' },
        ),
        'rejected',
        'Server did not confirm the reject',
      ),
    onSuccess: () => invalidateValueMappings(client),
  });
}

// ── Evals admin tab (agents/eval_routes.py — server-side golden lane) ────────
// The tab COMPLEMENTS the CLI lanes (scripts/eval_answers.py), never replaces
// them; runs are run-ALL-only (the POST body accepts an optional `variant`
// label ONLY — any subset-selection key is a server-side 422). A second,
// distinct surface (POST /run-upload) runs an uploaded customer golden set
// per-run, with zero server-side persistence of the file (ADR-0064).

const EVALS_BASE = '/api/admin/evals';

export function evalRunsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.evalRuns(),
    queryFn: ({ signal }) => queryJson<EvalRunsResponse>(`${EVALS_BASE}/runs`, signal),
    ...ADMIN_QUERY_POLICY.evalRuns,
    ...commonPolicy(enabled),
  });
}

/** GET /runs/{run_id} — polls every 5 s WHILE the run is executing; a
 * finished run (complete|failed) is immutable, so polling stops. */
export function evalRunQueryOptions(runId: string, enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.evalRun(runId),
    queryFn: ({ signal }) =>
      queryJson<EvalRunDetail>(`${EVALS_BASE}/runs/${encodeURIComponent(runId)}`, signal),
    refetchInterval: (query) =>
      query.state.data?.status === 'running' ? EVAL_RUN_POLL_MS : false,
    staleTime: 30 * SECOND,
    ...commonPolicy(enabled && !!runId),
  });
}

/** POST /run — starts the FULL golden suite (202 {run_id}). The optional
 * variant label is the only accepted body key. A 409 (a run is already
 * active, or the golden set is missing/empty) surfaces through
 * ApiError.detail for the tab to render verbatim. */
export function startEvalRunMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (variant: string) =>
      requestJson<EvalRunStartResult>(
        `${EVALS_BASE}/run`,
        jsonBody('POST', variant.trim() ? { variant: variant.trim() } : {}),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.evalRuns() }),
  });
}

/** POST /run-upload input. The bytes are read from the chosen file at SUBMIT
 * time and travel in the request body ONLY — never into the mutation key, the
 * query cache, or web storage (ADR-0064 D1/D5). */
export interface UploadedEvalRunInput {
  /** The golden file's RAW bytes. NOT a JSON envelope and NOT multipart: the
   * sha the server returns is sha256 over exactly these bytes, so the steward
   * can compare it against `eval_core.golden_file_sha()` of their own file. A
   * re-serialized object would not reproduce that digest. */
  bytes: ArrayBuffer;
  /** The optional run label — the ONLY query parameter the route accepts. */
  variant: string;
}

/** POST /run-upload — runs an uploaded customer golden set, ALL of it
 * (ADR-0064 D3: `variant` is the only accepted query param, so there is
 * nothing to select from). The 202 comes back in the shared
 * EvalRunStartResult shape plus the upload provenance
 * (`golden_set_source`/`golden_set_sha`/`question_count`).
 *
 * The set is transient: the server persists none of the bytes, and
 * `gcTime: 0` keeps them out of the mutation cache on this side too — the same
 * idiom downloadGoldenDraftMutationOptions uses for the transient golden
 * draft. Refusals (413 / 422 / 400 / 409 / 403) surface through ApiError for
 * evals-logic.uploadRefusalOf to name distinctly. */
export function startUploadedEvalRunMutationOptions(client: QueryClient) {
  return mutationOptions({
    gcTime: 0,
    mutationFn: ({ bytes, variant }: UploadedEvalRunInput) => {
      const label = variant.trim();
      const query = label ? `?variant=${encodeURIComponent(label)}` : '';
      return requestJson<EvalRunStartResult>(`${EVALS_BASE}/run-upload${query}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: bytes,
      });
    },
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.evalRuns() }),
  });
}

// ── Review queue (agents/eval_queue_routes.py, ADR-0062 D3/D4/D5) ────────────
// The queue is COMPUTED ON READ — there is no refresh route and nothing to
// invalidate but the read itself. Steward DECISIONS are the only mutation on
// this surface; the golden draft is a transient download that is never cached.

/** GET /queue — the computed review queue (window/limit bounds live server-side
 * in eval_queue.compute_queue, so no client parameter re-declares them). */
export function evalQueueQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.evalQueue(),
    queryFn: ({ signal }) => queryJson<EvalQueueResponse>(`${EVALS_BASE}/queue`, signal),
    // The panel offers an explicit reload and surfaces the failure inline —
    // a background retry storm would only hide it.
    retry: false,
    ...ADMIN_QUERY_POLICY.evalQueue,
    ...commonPolicy(enabled),
  });
}

/** POST /queue/decision — the ONLY mutation on the queue surface. A rejected
 * note is a 400 carrying {reason, detail}; the detail NEVER echoes the note
 * (queue-logic.rejectionOf parses it for display). */
export function recordEvalDecisionMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (input: EvalDecisionInput) =>
      requestJson<EvalDecisionResult>(`${EVALS_BASE}/queue/decision`, jsonBody('POST', input)),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.evalQueue() }),
  });
}

/** POST /queue/golden-draft — a TRANSIENT customer-derived draft (ADR-0062
 * D5). `gcTime: 0` keeps the response body out of the mutation cache: the
 * bundle lives only for the download tick, and no query cache, localStorage,
 * sessionStorage or IndexedDB copy is ever made. A 409 {reason} (not_decided |
 * source_expired | no_question_text) surfaces through ApiError. */
export function downloadGoldenDraftMutationOptions() {
  return mutationOptions({
    gcTime: 0,
    mutationFn: (sourceRef: string) =>
      requestJson<GoldenDraftBundle>(
        `${EVALS_BASE}/queue/golden-draft`,
        jsonBody('POST', { source_ref: sourceRef }),
      ),
  });
}

// ── Findings ledger + digest + annotator status (agents/eval_findings_routes.py)
// ADR-0062 D6/D10: the ledger and the digest are COMPUTED ON READ (both GETs
// write nothing server-side), refs resolve ONLY through GET /findings/{id}, and
// the annotator's status endpoint is the read path for the artifact-usage
// ledger. Freshness policy stays local to this block so the shared
// ADMIN_QUERY_POLICY object is untouched.

const FINDINGS_STALE_MS = 30 * SECOND;
const AUDIT_STATUS_STALE_MS = MINUTE;

/** GET /findings — the taxonomy-grouped ledger of MINIMIZED rows. */
export function evalFindingsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.evalFindings(),
    queryFn: ({ signal }) => queryJson<FindingsLedgerResponse>(`${EVALS_BASE}/findings`, signal),
    // The panel surfaces a failure inline with an explicit reload; a retry
    // storm behind a collapsed panel would only hide it.
    retry: false,
    staleTime: FINDINGS_STALE_MS,
    ...commonPolicy(enabled),
  });
}

/** GET /findings?window=7d — the read-time weekly digest. The window bound is
 * clamped server-side (eval_findings.weekly_digest), so this only names it. */
export function evalFindingsDigestQueryOptions(windowDays = 7, enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.evalFindingsDigest(windowDays),
    queryFn: ({ signal }) =>
      queryJson<FindingsDigest>(`${EVALS_BASE}/findings?window=${windowDays}d`, signal),
    retry: false,
    staleTime: FINDINGS_STALE_MS,
    ...commonPolicy(enabled),
  });
}

/** GET /findings/{id} — the ONLY door that resolves a ref against its 7-day
 * customer-derived detail, so it fires only when a steward expands a row. An
 * expired detail comes back as `detail_expired: true`, which is a normal
 * state, not an error. */
export function evalFindingQueryOptions(findingId: string, enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.evalFinding(findingId),
    queryFn: ({ signal }) =>
      queryJson<FindingRow>(`${EVALS_BASE}/findings/${encodeURIComponent(findingId)}`, signal),
    retry: false,
    staleTime: FINDINGS_STALE_MS,
    ...commonPolicy(enabled && !!findingId),
  });
}

/** GET /audit/status — last runs, today's global UTC-day budget, and the
 * ledger-derived telemetry counters. This read path lands before the annotator
 * itself, so an unavailable route is rendered honestly rather than zero-filled. */
export function evalAuditStatusQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.evalAuditStatus(),
    queryFn: ({ signal }) =>
      queryJson<AuditStatusResponse>(`${EVALS_BASE}/audit/status`, signal),
    retry: false,
    staleTime: AUDIT_STATUS_STALE_MS,
    ...commonPolicy(enabled),
  });
}

/** POST /findings/import — the findings document and its source report TEXT
 * travel TOGETHER (D7: the sha binding is fail-closed). The report is sent as
 * the EXACT text that was read from disk — re-serializing it would change the
 * digest and earn a 422.
 *
 * `evidenceDocs` is OPTIONAL and TRANSIENT: the auditor's `--evidence` bundle,
 * sent only so the server can resolve `evidence_refs` that exist in those
 * documents rather than in the report (report ids UNION evidence ids). The
 * server never stores it, and it cannot suppress a discard — a reference that
 * resolves nowhere is still counted in `discarded_invalid`. The key is OMITTED
 * when nothing was chosen, so the default request shape is unchanged. */
export interface FindingsImportInput {
  findingsFile: unknown;
  sourceReport: string;
  evidenceDocs?: unknown[];
}

export function importFindingsMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (input: FindingsImportInput) =>
      requestJson<FindingsImportResult>(
        `${EVALS_BASE}/findings/import`,
        jsonBody('POST', {
          findings_file: input.findingsFile,
          source_report: input.sourceReport,
          ...(input.evidenceDocs && input.evidenceDocs.length > 0
            ? { evidence: input.evidenceDocs }
            : {}),
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.evalFindings() }),
  });
}

export interface FindingAnnotateInput {
  findingId: string;
  status?: 'open' | 'annotated' | 'filed' | 'dismissed';
  note?: string;
}

/** POST /findings/{id}/annotate — the steward's own scope (it survives a
 * re-import byte-identical). A refused note is a 400 whose detail never
 * echoes the note (findings-logic.annotateRejectionMessage maps the reason). */
export function annotateFindingMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: ({ findingId, status, note }: FindingAnnotateInput) =>
      requestJson<FindingAnnotateResult>(
        `${EVALS_BASE}/findings/${encodeURIComponent(findingId)}/annotate`,
        jsonBody('POST', {
          ...(status ? { status } : {}),
          ...(note != null ? { note } : {}),
        }),
      ),
    onSuccess: (_result, input) => Promise.all([
      client.invalidateQueries({ queryKey: queryKeys.admin.evalFinding(input.findingId) }),
      client.invalidateQueries({ queryKey: queryKeys.admin.evalFindings() }),
    ]),
  });
}

/** POST /findings/{id}/file-proposal — steward-TRIGGERED, never automatic: the
 * ONE door through which a suggested mapping enters durable storage, and it
 * enters as a PENDING proposal ("answer-audit" never auto-accepts). The two
 * 409s are distinct: an expired 7-day detail vs. a finding that never carried
 * a mapping. */
export function fileFindingProposalMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (findingId: string) =>
      requestJson<FindingProposalResult>(
        `${EVALS_BASE}/findings/${encodeURIComponent(findingId)}/file-proposal`,
        { method: 'POST' },
      ),
    onSuccess: (_result, findingId) => Promise.all([
      client.invalidateQueries({ queryKey: queryKeys.admin.evalFinding(findingId) }),
      client.invalidateQueries({ queryKey: queryKeys.admin.evalFindings() }),
    ]),
  });
}
