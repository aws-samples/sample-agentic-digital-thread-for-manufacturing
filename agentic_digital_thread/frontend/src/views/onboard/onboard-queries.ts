// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import {
  QueryClient,
  QueryClientContext,
  queryOptions,
  useQuery,
} from '@tanstack/react-query';
import { useContext } from 'react';

import { onboardStatusQueryOptions, type OnboardStatus } from '../../api/reference-queries';
import { createQueryClient, queryJson } from '../../api/query-client';
import { queryKeys } from '../../api/query-keys';
import type { CatalogResponse, CatalogSystem } from '../../api/types/onboard';
import { rememberSystemNames } from '../../lib/system-names';
import { reconcileRemovals, removalSeq } from './removal-store';

export const ONBOARD_STALE_TIME_MS = 5 * 60 * 1000;
export const ONBOARD_REFETCH_INTERVAL_MS = 60 * 1000;

interface ConceptsResponse {
  concepts?: (string | { id?: string; name?: string })[];
}

function normalizeCatalog(data: CatalogResponse): CatalogSystem[] {
  if (!Array.isArray(data) && data.error) throw new Error(data.error);
  const systems = Array.isArray(data) ? data : data.systems || [];
  // Feed the shared display-name resolver (lib/system-names.ts).
  rememberSystemNames(systems);
  return systems;
}

/** One catalog read from the server. A successful read reconciles this
 * tab's removal record with it (removal-store.ts): it takes the record's
 * sequence number BEFORE fetching, so its answer can only clear failures
 * recorded before it started. */
async function readCatalog(signal: AbortSignal): Promise<CatalogSystem[]> {
  const readStartedAt = removalSeq();
  const systems = normalizeCatalog(
    await queryJson<CatalogResponse>('/api/onboard/catalog', signal),
  );
  reconcileRemovals(systems, readStartedAt);
  return systems;
}

function normalizeConcepts(data: ConceptsResponse): string[] {
  const concepts = (data.concepts || [])
    .map((concept) =>
      typeof concept === 'object' ? concept.id || concept.name || '' : concept,
    )
    .filter(Boolean);
  return [...concepts.filter((concept) => concept !== 'unknown'), 'unknown'];
}

export function catalogQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.catalog(),
    queryFn: ({ signal }) => readCatalog(signal),
    staleTime: ONBOARD_STALE_TIME_MS,
    refetchInterval: ONBOARD_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

export function conceptsQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.concepts(),
    queryFn: ({ signal }) =>
      queryJson<ConceptsResponse>('/api/onboard/concepts', signal),
    staleTime: ONBOARD_STALE_TIME_MS,
    refetchInterval: ONBOARD_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

/** Isolated mounts have no provider; production replaces this on first hook render. */
const isolatedOnboardQueryClient = createQueryClient();
let activeOnboardQueryClient: QueryClient = isolatedOnboardQueryClient;

function useOnboardQueryClient(): QueryClient {
  const providedClient = useContext(QueryClientContext);
  const queryClient = providedClient ?? isolatedOnboardQueryClient;
  activeOnboardQueryClient = queryClient;
  return queryClient;
}

/** QueryClient registered by the mounted onboarding surfaces. */
export function getOnboardQueryClient(): QueryClient {
  return activeOnboardQueryClient;
}

export function useCatalogQuery() {
  const queryClient = useOnboardQueryClient();
  return useQuery(catalogQueryOptions(), queryClient);
}

export function useConceptsQuery() {
  const queryClient = useOnboardQueryClient();
  return useQuery(conceptsQueryOptions(), queryClient);
}

/** Shared imperative catalog read. Force only for server-completion probes. */
export async function fetchCatalog(
  queryClient: QueryClient,
  force = false,
  signal?: AbortSignal,
): Promise<CatalogSystem[]> {
  if (force && signal) {
    const systems = await readCatalog(signal);
    queryClient.setQueryData<CatalogSystem[]>(queryKeys.catalog(), systems);
    return systems;
  }
  const options = catalogQueryOptions();
  return queryClient.fetchQuery(force ? { ...options, staleTime: 0 } : options);
}

export async function fetchConcepts(queryClient: QueryClient): Promise<string[]> {
  return normalizeConcepts(await queryClient.fetchQuery(conceptsQueryOptions()));
}

/** Reuses the canonical status options from api/reference-queries.ts. */
export function fetchOnboardStatus(queryClient: QueryClient): Promise<OnboardStatus> {
  return queryClient.fetchQuery(onboardStatusQueryOptions());
}

export function setConcepts(queryClient: QueryClient, concepts: string[]): void {
  queryClient.setQueryData<ConceptsResponse>(queryKeys.concepts(), {
    concepts: [...concepts.filter((concept) => concept !== 'unknown'), 'unknown'],
  });
}

export function getCachedConcepts(queryClient: QueryClient): string[] {
  const cached = queryClient.getQueryData<ConceptsResponse>(queryKeys.concepts());
  return cached ? normalizeConcepts(cached) : [];
}

function invalidateKeys(
  queryClient: QueryClient,
  keys: readonly (readonly unknown[])[],
): Promise<void> {
  return Promise.all(
    keys.map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  ).then(() => undefined);
}

const COMPLETE_OR_RESET_KEYS = [
  queryKeys.systems(),
  queryKeys.catalog(),
  queryKeys.onboardStatus(),
  queryKeys.concepts(),
] as const;

export function invalidateOnboardingComplete(queryClient: QueryClient): Promise<void> {
  return invalidateKeys(queryClient, COMPLETE_OR_RESET_KEYS);
}

export function invalidateOnboardingReset(queryClient: QueryClient): Promise<void> {
  return invalidateKeys(queryClient, COMPLETE_OR_RESET_KEYS);
}

export function invalidateOnboardingRemap(queryClient: QueryClient): Promise<void> {
  return invalidateKeys(queryClient, [
    queryKeys.catalog(),
    queryKeys.concepts(),
    queryKeys.systems(),
  ]);
}

export function invalidateOnboardingConnect(queryClient: QueryClient): Promise<void> {
  return invalidateKeys(queryClient, [
    queryKeys.catalog(),
    queryKeys.onboardStatus(),
    queryKeys.systems(),
  ]);
}
