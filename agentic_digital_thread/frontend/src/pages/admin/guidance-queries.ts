// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { mutationOptions, queryOptions, type QueryClient } from '@tanstack/react-query';

import { requestJson } from '../../api/client';
import { queryJson } from '../../api/query-client';
import { queryKeys } from '../../api/query-keys';
import type {
  ContextPlacementJob,
  ContextPlacementStartInput,
  ContextPlacementStartResult,
  GuidanceAcceptInput,
  GuidanceAcceptResult,
  GuidanceActionResult,
  GuidancePreviewResponse,
  GuidanceReconciliationResult,
  PromoteRequirementInput,
  PromoteRequirementResult,
  ProposedMapping,
  ProposedMappingsResponse,
  QueryGuidanceResponse,
  QueryGuidanceSystem,
} from '../../api/types/proposals';

const VOCABULARY_BASE = '/api/admin/vocabulary';
const CONTEXT_PLACEMENT_BASE = '/api/admin/learning/context-placement-check';
const SECOND = 1_000;
export const CONTEXT_PLACEMENT_POLL_MS = SECOND;

export interface GuidanceSystemWithBase extends QueryGuidanceSystem {
  base?: string;
  learned_count?: number;
}

export interface GuidanceProposalResult {
  status?: string;
  mapping_id?: string;
  error?: string;
}

export interface GuidanceNoteInput {
  systemId: string;
  note: string;
}

function commonPolicy(enabled: boolean) {
  return {
    enabled,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
  } as const;
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

function invalidateGuidance(client: QueryClient) {
  return client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyGuidance() });
}

function requireStatus<T extends { status?: string }>(
  result: T,
  expected: string,
  fallback: string,
): T {
  if (result.status !== expected) throw new Error(fallback);
  return result;
}

export function queryGuidanceQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.vocabularyGuidance(),
    queryFn: async ({ signal }) => {
      const data = await queryJson<QueryGuidanceResponse>('/api/query-guidance', signal);
      return (data.systems ?? []) as GuidanceSystemWithBase[];
    },
    staleTime: 30 * SECOND,
    refetchInterval: false,
    ...commonPolicy(enabled),
  });
}

export function pruneGuidanceNoteMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async ({ systemId, note }: GuidanceNoteInput) => requireStatus(
      await requestJson<GuidanceActionResult>(
        `/api/query-guidance/${encodeURIComponent(systemId)}/prune`,
        jsonBody({ note }),
      ),
      'pruned',
      'Guidance note could not be pruned.',
    ),
    onSuccess: () => invalidateGuidance(client),
  });
}

export function isGuidanceProposal(mapping: ProposedMapping): boolean {
  return mapping.proposed_type === 'guidance_note';
}

export function guidanceProposalsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.vocabularyGuidanceProposals(),
    queryFn: async ({ signal }) => {
      const data = await queryJson<ProposedMappingsResponse>('/api/proposed-mappings?proposed_type=guidance_note', signal);
      return (data.mappings ?? []).filter(isGuidanceProposal);
    },
    staleTime: 30 * SECOND,
    refetchInterval: false,
    ...commonPolicy(enabled),
  });
}

export function guidancePreviewQueryOptions(mappingId: string, enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.guidancePreview(mappingId),
    queryFn: ({ signal }) => queryJson<GuidancePreviewResponse>(
      `/api/proposed-mappings/${encodeURIComponent(mappingId)}/guidance-preview`,
      signal,
    ),
    staleTime: 0,
    ...commonPolicy(enabled && !!mappingId),
  });
}

export function acceptGuidanceMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: ({ mappingId, confirmation }: GuidanceAcceptInput) =>
      requestJson<GuidanceAcceptResult>(
        `/api/proposed-mappings/${encodeURIComponent(mappingId)}/accept`,
        confirmation ? jsonBody(confirmation) : { method: 'POST' },
      ),
    onSuccess: () => Promise.all([
      client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyGuidanceProposals() }),
      client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyGuidance() }),
      client.invalidateQueries({ queryKey: queryKeys.admin.guidanceReconciliation() }),
    ]),
  });
}

export function guidanceReconciliationQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.guidanceReconciliation(),
    queryFn: ({ signal }) =>
      queryJson<GuidanceReconciliationResult>('/api/query-guidance/reconciliation', signal),
    staleTime: 15 * SECOND,
    refetchInterval: false,
    retry: false,
    ...commonPolicy(enabled),
  });
}

export function promoteRequirementMutationOptions() {
  return mutationOptions({
    mutationFn: ({ systemId, asserted, negated }: PromoteRequirementInput) =>
      requestJson<PromoteRequirementResult>(
        `${VOCABULARY_BASE}/guidance/${encodeURIComponent(systemId)}/promote-requirement`,
        jsonBody({ asserted, negated }),
      ),
  });
}

export function startContextPlacementMutationOptions() {
  return mutationOptions({
    mutationFn: ({ systemId }: ContextPlacementStartInput) =>
      requestJson<ContextPlacementStartResult>(
        CONTEXT_PLACEMENT_BASE,
        jsonBody({ system_id: systemId }),
      ),
  });
}

export function contextPlacementJobQueryOptions(jobId: string, enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.contextPlacementJob(jobId),
    queryFn: ({ signal }) => queryJson<ContextPlacementJob>(
      `${CONTEXT_PLACEMENT_BASE}/${encodeURIComponent(jobId)}`,
      signal,
    ),
    refetchInterval: (query) =>
      query.state.data?.status === 'running' ? CONTEXT_PLACEMENT_POLL_MS : false,
    staleTime: 0,
    ...commonPolicy(enabled && !!jobId),
  });
}

export function clearGuidanceLearnedMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async (systemId: string) => requireStatus(
      await requestJson<GuidanceActionResult>(
        `/api/query-guidance/${encodeURIComponent(systemId)}/clear`,
        { method: 'POST' },
      ),
      'cleared',
      'Learned guidance could not be cleared.',
    ),
    onSuccess: () => invalidateGuidance(client),
  });
}

/** File a guidance note for steward review. On success the delivery queue
 * (`vocabularyGuidanceProposals`) is invalidated so the new PENDING card
 * appears without a page reload — the queue has a 30 s staleTime and no
 * polling, so without this the proposer sees "ZERO" until they refresh. */
export function proposeGuidanceNoteMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: async ({ systemId, note }: GuidanceNoteInput) => requireStatus(
      await requestJson<GuidanceProposalResult>(
        `${VOCABULARY_BASE}/guidance/${encodeURIComponent(systemId)}/propose`,
        jsonBody({ note }),
      ),
      'pending',
      'Guidance proposal could not be filed.',
    ),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyGuidanceProposals() }),
  });
}
