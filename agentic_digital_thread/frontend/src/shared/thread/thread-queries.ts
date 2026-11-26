// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { queryOptions } from '@tanstack/react-query';

import { ApiError, requestJson } from '../../api/client';
import { queryKeys } from '../../api/query-keys';
import type {
  ConceptCoverage,
  GraphSummary,
  ThreadEntities,
  VocabularyStats,
} from '../../api/types/thread';
import type { FetchLike } from '../agents/pricing';

export const THREAD_REFERENCE_STALE_TIME_MS = 5 * 60 * 1000;
export const THREAD_REFERENCE_REFETCH_INTERVAL_MS = 60 * 1000;
export const THREAD_DATA_STALE_TIME_MS = 5 * 60 * 1000;

export async function requestThreadJson<T>(
  fetchFn: FetchLike | undefined,
  url: string,
  options?: RequestInit,
): Promise<T> {
  if (!fetchFn) return requestJson<T>(url, options);
  const response = await fetchFn(url, options);
  if (!response) throw new ApiError(0, url, `No response from ${url}`);
  if (!response.ok) {
    let detail = `${url} returned ${response.status}`;
    try {
      const envelope = (await response.json()) as Record<string, unknown>;
      const candidate = envelope.detail ?? envelope.error ?? envelope.message;
      if (typeof candidate === 'string' && candidate.trim()) detail = candidate;
    } catch {
      // Keep the status-bearing fallback when an injected response has no JSON body.
    }
    throw new ApiError(response.status, url, detail);
  }
  return (await response.json()) as T;
}

export function conceptsQueryOptions(fetchFn?: FetchLike) {
  return queryOptions({
    queryKey: queryKeys.concepts(),
    queryFn: ({ signal }) =>
      requestThreadJson<{ concepts?: unknown[] }>(fetchFn, '/api/onboard/concepts', { signal }),
    staleTime: THREAD_REFERENCE_STALE_TIME_MS,
    refetchInterval: THREAD_REFERENCE_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

export function graphSummaryQueryOptions(
  systemsSignature: string,
  fetchFn?: FetchLike,
) {
  return queryOptions({
    queryKey: [...queryKeys.thread.graphSummary(), systemsSignature] as const,
    queryFn: ({ signal }) =>
      requestThreadJson<GraphSummary>(fetchFn, '/api/thread/graph-summary', { signal }),
    staleTime: THREAD_DATA_STALE_TIME_MS,
  });
}

export function entitiesQueryOptions(fetchFn?: FetchLike) {
  return queryOptions({
    queryKey: queryKeys.thread.entities(),
    queryFn: ({ signal }) =>
      requestThreadJson<ThreadEntities>(fetchFn, '/api/thread/entities', { signal }),
    staleTime: THREAD_DATA_STALE_TIME_MS,
  });
}

export interface ThreadAnalytics {
  vocab: VocabularyStats;
  coverage: ConceptCoverage;
}

export function analyticsQueryOptions(fetchFn?: FetchLike) {
  return queryOptions({
    queryKey: queryKeys.thread.analytics(),
    queryFn: async ({ signal }): Promise<ThreadAnalytics> => {
      const [vocab, coverage] = await Promise.all([
        requestThreadJson<VocabularyStats>(fetchFn, '/api/thread/analytics/vocabulary', { signal }),
        requestThreadJson<ConceptCoverage>(fetchFn, '/api/thread/analytics/concept-coverage', { signal }),
      ]);
      return { vocab, coverage };
    },
    staleTime: THREAD_DATA_STALE_TIME_MS,
  });
}
