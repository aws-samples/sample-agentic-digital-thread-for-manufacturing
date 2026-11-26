// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import {
  QueryClient,
  QueryClientContext,
  queryOptions,
  useQuery,
} from '@tanstack/react-query';
import { useContext } from 'react';

import { ApiError } from './client';
import { createQueryClient, queryJson } from './query-client';
import { queryKeys } from './query-keys';
import type { SystemsResponse } from './types/dashboard';
import { _resetSystemNames, rememberSystemNames } from '../lib/system-names';

export const SYSTEMS_STALE_TIME_MS = 5 * 60 * 1000;
export const SYSTEMS_REFETCH_INTERVAL_MS = 60 * 1000;

export type SystemsFetch = (url: string, init?: RequestInit) => Promise<Response | null>;

async function fetchSystemsWith(fetchFn: SystemsFetch, signal: AbortSignal): Promise<SystemsResponse> {
  const url = '/api/systems';
  const response = await fetchFn(url, { signal });
  if (!response) throw new ApiError(0, url, `No response from ${url}`);
  if (!response.ok) {
    let detail = `${url} returned ${response.status}`;
    try {
      const envelope = (await response.json()) as Record<string, unknown>;
      const candidate = envelope.detail ?? envelope.error ?? envelope.message;
      if (typeof candidate === 'string' && candidate.trim()) detail = candidate;
    } catch {
      // Keep the status-bearing fallback when the response has no JSON body.
    }
    throw new ApiError(response.status, url, detail);
  }
  return (await response.json()) as SystemsResponse;
}

/** One policy and key for every /api/systems consumer. Every successful
 * read also feeds the shared display-name resolver (lib/system-names.ts),
 * so a surface that names a system reads the catalog name. */
export function systemsQueryOptions(fetchFn?: SystemsFetch) {
  return queryOptions({
    queryKey: queryKeys.systems(),
    queryFn: async ({ signal }) => {
      const data = fetchFn
        ? await fetchSystemsWith(fetchFn, signal)
        : await queryJson<SystemsResponse>('/api/systems', signal);
      rememberSystemNames(data?.systems);
      return data;
    },
    staleTime: SYSTEMS_STALE_TIME_MS,
    refetchInterval: SYSTEMS_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    retry: false,
  });
}

/**
 * Imperative dashboard/architecture modules cannot call React hooks. The app
 * entry registers its provider client before rendering; isolated direct mounts
 * and unit tests retain the fallback client.
 */
const isolatedSystemsQueryClient = createQueryClient();
let activeSystemsQueryClient: QueryClient = isolatedSystemsQueryClient;

export function registerSystemsQueryClient(queryClient: QueryClient): void {
  activeSystemsQueryClient = queryClient;
}

export function useSystemsQueryClient(): QueryClient {
  const providedClient = useContext(QueryClientContext);
  const queryClient = providedClient ?? isolatedSystemsQueryClient;
  registerSystemsQueryClient(queryClient);
  return queryClient;
}

export function getSystemsQueryClient(): QueryClient {
  return activeSystemsQueryClient;
}

export function useSystemsQuery(fetchFn?: SystemsFetch) {
  const queryClient = useSystemsQueryClient();
  return useQuery(systemsQueryOptions(fetchFn), queryClient);
}

/** Shared non-hook read for dashboard and Cytoscape system-map consumers. */
export function fetchSystems(
  queryClient: QueryClient = getSystemsQueryClient(),
): Promise<SystemsResponse> {
  return queryClient.fetchQuery(systemsQueryOptions());
}

/** Test-only reset for direct imperative consumers mounted without a provider
 * (also clears the display names the cleared cache fed). */
export function _resetSystemsQueryClient(): void {
  isolatedSystemsQueryClient.clear();
  activeSystemsQueryClient = isolatedSystemsQueryClient;
  _resetSystemNames();
}
