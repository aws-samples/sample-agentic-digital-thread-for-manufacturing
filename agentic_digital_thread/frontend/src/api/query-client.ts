// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { QueryClient } from '@tanstack/react-query';

import { ApiError, requestJson } from './client';

const DEFAULT_RETRY_COUNT = 3;

function retryQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return false;
  }
  return failureCount < DEFAULT_RETRY_COUNT;
}

/** Create one Query client for a page entry. Call this once at module scope. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: retryQuery,
        staleTime: 0,
        refetchOnWindowFocus: false,
      },
    },
  });
}

/** Query-function transport that preserves TanStack Query cancellation. */
export function queryJson<T>(url: string, signal: AbortSignal): Promise<T> {
  return requestJson<T>(url, { signal });
}
