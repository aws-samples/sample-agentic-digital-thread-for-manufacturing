// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import {
  catalogQueryOptions,
  conceptsQueryOptions,
  fetchCatalog,
  fetchConcepts,
  getCachedConcepts,
  invalidateOnboardingComplete,
  invalidateOnboardingConnect,
  invalidateOnboardingRemap,
  invalidateOnboardingReset,
  ONBOARD_REFETCH_INTERVAL_MS,
  ONBOARD_STALE_TIME_MS,
} from '../views/onboard/onboard-queries';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('onboarding reference queries', () => {
  it('uses the documented five-minute stale and visible-page polling policy', () => {
    for (const options of [catalogQueryOptions(), conceptsQueryOptions()]) {
      expect(options.staleTime).toBe(ONBOARD_STALE_TIME_MS);
      expect(options.staleTime).toBe(5 * 60 * 1000);
      expect(options.refetchInterval).toBe(ONBOARD_REFETCH_INTERVAL_MS);
      expect(options.refetchInterval).toBe(60 * 1000);
      expect(options.refetchIntervalInBackground).toBe(false);
    }
  });

  it('dedupes imperative catalog consumers and normalizes the catalog envelope', async () => {
    const client = createQueryClient();
    let resolveResponse!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      resolveResponse = resolve;
    });
    const fetchMock = vi.fn(() => pending);
    vi.stubGlobal('fetch', fetchMock);

    const first = fetchCatalog(client);
    const second = fetchCatalog(client);
    resolveResponse(
      new Response(
        JSON.stringify({ systems: [{ id: 'cmms', name: 'CMMS', status: 'registered' }] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    await expect(Promise.all([first, second])).resolves.toEqual([
      [{ id: 'cmms', name: 'CMMS', status: 'registered' }],
      [{ id: 'cmms', name: 'CMMS', status: 'registered' }],
    ]);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('normalizes object concepts and keeps unknown exactly once', async () => {
    const client = createQueryClient();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            concepts: ['batch', { id: 'work-order' }, { name: 'equipment-id' }, 'unknown'],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );

    await expect(fetchConcepts(client)).resolves.toEqual([
      'batch',
      'work-order',
      'equipment-id',
      'unknown',
    ]);
  });

  it('does not substitute built-in concepts before or after a failed request', async () => {
    const client = createQueryClient();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ error: 'concept service unavailable' }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    expect(getCachedConcepts(client)).toEqual([]);
    await expect(fetchConcepts(client)).rejects.toThrow('concept service unavailable');
    expect(client.getQueryState(queryKeys.concepts())?.status).toBe('error');
    expect(getCachedConcepts(client)).toEqual([]);
  });

  it('treats a successful empty concept response as an honest minimal vocabulary', async () => {
    const client = createQueryClient();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ concepts: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    await expect(fetchConcepts(client)).resolves.toEqual(['unknown']);
    expect(getCachedConcepts(client)).toEqual(['unknown']);
  });

  it('aborts a forced catalog probe with its caller signal and does not cache a result', async () => {
    const client = createQueryClient();
    const controller = new AbortController();
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('The operation was aborted', 'AbortError')),
          { once: true },
        );
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const probe = fetchCatalog(client, true, controller.signal);
    controller.abort();

    await expect(probe).rejects.toMatchObject({ name: 'AbortError' });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/onboard/catalog',
      expect.objectContaining({ signal: controller.signal }),
    );
    expect(client.getQueryData(queryKeys.catalog())).toBeUndefined();
  });
});

type Invalidator = (client: ReturnType<typeof createQueryClient>) => Promise<void>;

async function expectInvalidationKeys(
  invalidator: Invalidator,
  expected: readonly (readonly unknown[])[],
): Promise<void> {
  const client = createQueryClient();
  const invalidate = vi
    .spyOn(client, 'invalidateQueries')
    .mockResolvedValue(undefined);

  await invalidator(client);

  expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual(expected);
}

describe('onboarding mutation invalidation', () => {
  it('onboarding COMPLETE invalidates exactly systems, catalog, status, concepts', async () => {
    await expectInvalidationKeys(invalidateOnboardingComplete, [
      queryKeys.systems(),
      queryKeys.catalog(),
      queryKeys.onboardStatus(),
      queryKeys.concepts(),
    ]);
  });

  it.each(['reset-all', 'reset-system', 'reseed'])(
    '%s invalidates exactly systems, catalog, status, concepts',
    async () => {
      await expectInvalidationKeys(invalidateOnboardingReset, [
        queryKeys.systems(),
        queryKeys.catalog(),
        queryKeys.onboardStatus(),
        queryKeys.concepts(),
      ]);
    },
  );

  it('remap invalidates exactly catalog, concepts, systems', async () => {
    await expectInvalidationKeys(invalidateOnboardingRemap, [
      queryKeys.catalog(),
      queryKeys.concepts(),
      queryKeys.systems(),
    ]);
  });

  it('connect invalidates exactly catalog, status, systems', async () => {
    await expectInvalidationKeys(invalidateOnboardingConnect, [
      queryKeys.catalog(),
      queryKeys.onboardStatus(),
      queryKeys.systems(),
    ]);
  });

  it('a failed mutation performs no invalidation', async () => {
    const client = createQueryClient();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const mutation = vi.fn(async () => {
      throw new Error('mutation failed');
    });

    await expect(
      mutation().then(() => invalidateOnboardingReset(client)),
    ).rejects.toThrow('mutation failed');
    expect(invalidate).not.toHaveBeenCalled();
  });
});
