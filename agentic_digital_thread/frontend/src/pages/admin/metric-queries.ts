// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Metric-definition query/mutation options (ADR-0065) — a focused module in
 * the guidance-queries.ts mould, so admin-queries.ts stays within its
 * source-shape budget (adr0063-source-shape.test.ts).
 *
 * GET /metrics is the composed read model (code ∪ accepted ∪ pending ∪
 * shadowed) and is guaranteed write-free server-side. POST /metrics/propose
 * files a PENDING proposal.
 *
 * ONE DOOR — there is deliberately NO accept mutation here: acceptance stays
 * on the existing proposals queue, so this module exposes no way to apply a
 * metric definition. */
import { mutationOptions, queryOptions, type QueryClient } from '@tanstack/react-query';

import { requestJson } from '../../api/client';
import { queryJson } from '../../api/query-client';
import { queryKeys } from '../../api/query-keys';
import type {
  MetricDefinitionInput,
  MetricDefinitionPreview,
  MetricDefinitionProposeResult,
  MetricDefinitionsResponse,
} from '../../api/types/vocabulary';

const METRICS_BASE = '/api/admin/vocabulary/metrics';
const MINUTE = 60 * 1_000;

function commonPolicy(enabled: boolean) {
  return {
    enabled,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
  } as const;
}

/** GET /metrics — the four-status metric read model. */
export function metricDefinitionsQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.vocabularyMetrics(),
    queryFn: ({ signal }) => queryJson<MetricDefinitionsResponse>(METRICS_BASE, signal),
    staleTime: MINUTE,
    refetchInterval: false as const,
    ...commonPolicy(enabled),
  });
}

/** GET /metrics/{mapping_id}/preview — the computed formula diff. Read-only
 * and fetched on demand, so it is never part of a background poll. */
export function metricDefinitionPreviewQueryOptions(mappingId: string, enabled = true) {
  return queryOptions({
    queryKey: queryKeys.admin.vocabularyMetricPreview(mappingId),
    queryFn: ({ signal }) =>
      queryJson<MetricDefinitionPreview>(
        `${METRICS_BASE}/${encodeURIComponent(mappingId)}/preview`,
        signal,
      ),
    staleTime: 0,
    ...commonPolicy(enabled && !!mappingId),
  });
}

/** GET /metrics/{mapping_id}/preview?resolve=1[&with=…] — the ADR-0080 dry
 * run. Deliberately a plain fetch, NOT a query option (never held in the
 * react-query cache, never polled, discarded on close), and requested with
 * `cache: 'no-store'` so the browser HTTP cache can never answer it either —
 * the server also sends `Cache-Control: private, no-store`. Re-opening or
 * Recompute always reaches the resolver. */
export function fetchMetricDryRun(
  mappingId: string,
  withIds: readonly string[] = [],
): Promise<MetricDefinitionPreview> {
  const params = new URLSearchParams({ resolve: '1' });
  if (withIds.length) params.set('with', withIds.join(','));
  return requestJson<MetricDefinitionPreview>(
    `${METRICS_BASE}/${encodeURIComponent(mappingId)}/preview?${params.toString()}`,
    { cache: 'no-store' },
  );
}

/** POST /metrics/propose — boundary-validated, filed PENDING. A rejected
 * document comes back as 400 {error}; the caller surfaces ApiError.detail
 * VERBATIM (the server names the offending field). */
export function proposeMetricDefinitionMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (input: MetricDefinitionInput) =>
      requestJson<MetricDefinitionProposeResult>(`${METRICS_BASE}/propose`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(input),
      }),
    // A new pending row belongs in the metric read model immediately.
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyMetrics() }),
  });
}
