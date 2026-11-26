// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* TanStack Query options for the observability drill-down trace routes
 * (GET /api/admin/agents/traces/sessions[/{id}] — Wave O2 frozen contract).
 * Follows the agent-queries.ts convention: queryOptions factories with the
 * injectable `fetchFn` transport seam for tests. Both routes answer
 * `{"enabled": false}` (HTTP 200) when the deploy-time feature gate is off —
 * consumers feature-detect on that field and render nothing. */

import { queryOptions } from '@tanstack/react-query';

import { queryKeys } from '../../api/query-keys';
import type {
  TraceSessionDetail,
  TraceSessionsResponse,
} from '../../api/types/agents';
import { injectedQueryJson } from './agent-queries';
import type { FetchLike } from './pricing';

/** Session lists change slowly and each read is a Logs Insights query —
 * keep results fresh for a minute to avoid re-querying on tab flips. */
const TRACE_STALE_TIME_MS = 60 * 1000;

export function traceSessionsQueryOptions(hours: number, fetchFn?: FetchLike) {
  return queryOptions({
    queryKey: queryKeys.admin.traceSessions(hours),
    queryFn: ({ signal }) =>
      injectedQueryJson<TraceSessionsResponse>(
        `/api/admin/agents/traces/sessions?hours=${hours}`,
        signal,
        fetchFn,
      ),
    staleTime: TRACE_STALE_TIME_MS,
  });
}

export function traceSessionDetailQueryOptions(
  sessionId: string,
  hours: number,
  fetchFn?: FetchLike,
) {
  return queryOptions({
    queryKey: queryKeys.admin.traceSession(sessionId, hours),
    queryFn: ({ signal }) =>
      injectedQueryJson<TraceSessionDetail>(
        `/api/admin/agents/traces/sessions/${encodeURIComponent(sessionId)}?hours=${hours}`,
        signal,
        fetchFn,
      ),
    staleTime: TRACE_STALE_TIME_MS,
  });
}
