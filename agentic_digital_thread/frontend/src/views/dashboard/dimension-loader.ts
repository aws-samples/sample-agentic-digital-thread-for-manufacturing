// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/** Shared resolve-by-dimension request plumbing. The panel loader remains a
 * thin specialization of the canonical dashboard SWR guard. */
import { requestJson } from '../../api/client';
import type { DimensionPayload } from '../../api/types/dashboard';
import { makeSwrLoader, type SwrLoader } from './swr';

export const RESOLVE_BY_DIMENSION_URL = '/api/metrics/resolve-by-dimension';

export interface DimensionRequest {
  metricId: string;
  dimensionConcept: string;
  signal?: AbortSignal;
}

/** Resolve one metric grouped by one concept using the canonical request body. */
export function resolveDimension({
  metricId,
  dimensionConcept,
  signal,
}: DimensionRequest): Promise<DimensionPayload> {
  return requestJson<DimensionPayload>(RESOLVE_BY_DIMENSION_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({
      metric_id: metricId,
      dimension_concept: dimensionConcept,
    }),
  });
}

/** Preserve the dashboard's established friendly resolve failure envelope. */
export function dimensionErrorEnvelope(error: unknown): DimensionPayload {
  return { status: 'error', error: String((error as Error)?.message || error) };
}

export interface DimensionLoaderOptions<TPayload extends DimensionPayload = DimensionPayload> {
  name: string;
  metricId: string;
  dimensionConcept: string;
  setPayload: (payload: TPayload | null) => void;
  /** Add client-only metadata without changing the transport payload. */
  transform?: (payload: DimensionPayload) => TPayload;
  /** Defaults to the established resolve error envelope; panels may retain
   * their historical null-on-first-transport-failure contract. */
  firstFailure?: (error: unknown) => TPayload | null;
}

/** Build a resolve-by-dimension panel loader on top of makeSwrLoader. */
export function makeDimensionLoader<TPayload extends DimensionPayload = DimensionPayload>(
  options: DimensionLoaderOptions<TPayload>,
): SwrLoader {
  return makeSwrLoader({
    name: options.name,
    fetch: async (signal) => {
      const payload = await resolveDimension({
        metricId: options.metricId,
        dimensionConcept: options.dimensionConcept,
        signal,
      });
      options.setPayload(options.transform ? options.transform(payload) : (payload as TPayload));
    },
    onFirstFailure: (error) =>
      options.setPayload(
        options.firstFailure
          ? options.firstFailure(error)
          : (dimensionErrorEnvelope(error) as TPayload),
      ),
  });
}
