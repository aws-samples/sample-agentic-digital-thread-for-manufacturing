// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { queryOptions } from '@tanstack/react-query';

import { ApiError } from '../../api/client';
import { queryJson } from '../../api/query-client';
import { queryKeys } from '../../api/query-keys';
import type {
  AgentsResponse,
  LogTailLine,
  LogTailResponse,
  ModelCatalog,
  NeptuneStats,
} from '../../api/types/agents';
import type { FetchLike } from './pricing';

const MODEL_STALE_TIME_MS = 10 * 60 * 1000;
export const LOG_TAIL_INTERVAL_MS = 10 * 1000;
export const LOG_TAIL_MAX_LINES = 100;

/** queryJson with an optional injectable transport — the `fetchFn` seam keeps
 * focused tests transport-free. Shared with trace-queries.ts. */
export async function injectedQueryJson<T>(
  url: string,
  signal: AbortSignal,
  fetchFn?: FetchLike,
): Promise<T> {
  if (!fetchFn) return queryJson<T>(url, signal);

  const response = await fetchFn(url, { signal });
  if (!response) throw new Error('Authentication required');
  if (!response.ok) {
    throw new ApiError(
      response.status,
      url,
      response.statusText || `HTTP ${response.status}`,
    );
  }
  return (await response.json()) as T;
}

export function fleetQueryOptions(hours: number, fetchFn?: FetchLike) {
  return queryOptions({
    queryKey: queryKeys.admin.fleet(hours),
    queryFn: ({ signal }) =>
      injectedQueryJson<AgentsResponse>(`/api/admin/agents?hours=${hours}`, signal, fetchFn),
  });
}

export function neptuneQueryOptions(hours: number, fetchFn?: FetchLike) {
  return queryOptions({
    queryKey: queryKeys.admin.neptune(hours),
    queryFn: ({ signal }) =>
      injectedQueryJson<NeptuneStats>(
        `/api/admin/agents/neptune?hours=${hours}`,
        signal,
        fetchFn,
      ),
  });
}

export function modelsQueryOptions(fetchFn?: FetchLike) {
  return queryOptions({
    queryKey: queryKeys.admin.models(),
    queryFn: ({ signal }) =>
      injectedQueryJson<ModelCatalog>('/api/admin/models', signal, fetchFn),
    staleTime: MODEL_STALE_TIME_MS,
  });
}

export function logTailQueryOptions(fetchFn?: FetchLike) {
  return queryOptions({
    queryKey: queryKeys.admin.logTail(),
    queryFn: ({ signal }) =>
      injectedQueryJson<LogTailResponse>(
        '/api/admin/agents/log-tail?lines=30',
        signal,
        fetchFn,
      ),
    refetchInterval: LOG_TAIL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

function logLineKey(line: LogTailLine): string {
  return `${line.ts}\u0000${line.msg}`;
}

/** Merge polling snapshots without duplicate lines and retain only the newest tail. */
export function mergeLogTailLines(
  previous: LogTailLine[],
  incoming: LogTailLine[],
  limit = LOG_TAIL_MAX_LINES,
): LogTailLine[] {
  const merged = [...previous];
  const seen = new Set(previous.map(logLineKey));
  for (const line of incoming) {
    const key = logLineKey(line);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(line);
  }
  return merged.slice(-limit);
}
