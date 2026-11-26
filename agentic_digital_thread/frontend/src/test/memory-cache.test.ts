// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the memory panel stale-while-revalidate cache (memory-cache.ts):
 * fresh cache serves with NO refetch inside the 30s TTL, a stale cache serves
 * immediately and revalidates in the background (onFresh), concurrent callers
 * share one in-flight fetch, a failed refresh KEEPS the cached data, a failed
 * FIRST load propagates (401 as Error('401'), never cached), force bypasses
 * the TTL, and invalidateMemoryCache()/deleteMemoryRecord() drop the cache. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import type { MemorySession, SemanticItem } from '../api/types/memory';
import {
  _resetMemoryCache,
  deleteMemoryRecord,
  getMemorySemantic,
  getMemorySessions,
  invalidateMemoryCache,
  MEMORY_CACHE_TTL_MS,
} from '../chat/memory-cache';

const SESS_A: MemorySession[] = [{ sessionId: 's1', label: 'first', createdAt: '2026-01-01' }];
const SESS_B: MemorySession[] = [...SESS_A, { sessionId: 's2', label: 'second' }];
const FACTS: SemanticItem[] = [{ id: 'rec-1', content: 'line 2 runs Freezers' }];

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}
function status(code: number, body: unknown = {}): Response {
  return {
    ok: false,
    status: code,
    statusText: '',
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  vi.useFakeTimers();
  _resetMemoryCache();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('getMemorySessions — SWR cache', () => {
  it('first load fetches; a call inside the TTL serves cached with NO refetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ sessions: SESS_A }));
    vi.stubGlobal('fetch', fetchMock);

    expect(await getMemorySessions()).toEqual(SESS_A);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith('/api/memory/sessions', {
      credentials: 'same-origin',
    });

    expect(await getMemorySessions()).toEqual(SESS_A);
    expect(fetchMock).toHaveBeenCalledTimes(1); // cache hit — no network
  });

  it('after the TTL, serves stale immediately and revalidates via onFresh', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ sessions: SESS_A }))
      .mockResolvedValueOnce(ok({ sessions: SESS_B }));
    vi.stubGlobal('fetch', fetchMock);

    await getMemorySessions();
    vi.advanceTimersByTime(MEMORY_CACHE_TTL_MS + 1);

    const onFresh = vi.fn();
    // Stale-while-revalidate: resolves with the CACHED copy synchronously…
    expect(await getMemorySessions({ onFresh })).toEqual(SESS_A);
    // …and the background refresh lands via onFresh.
    await vi.runAllTimersAsync();
    expect(onFresh).toHaveBeenCalledWith(SESS_B);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // The revalidated copy is now the cached one.
    expect(await getMemorySessions()).toEqual(SESS_B);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('concurrent callers share ONE in-flight fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ sessions: SESS_A }));
    vi.stubGlobal('fetch', fetchMock);

    const [a, b] = await Promise.all([getMemorySessions(), getMemorySessions()]);
    expect(a).toEqual(SESS_A);
    expect(b).toEqual(SESS_A);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a failed FORCED refresh keeps serving the cached copy (log-only)', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ sessions: SESS_A }))
      .mockRejectedValueOnce(new Error('network down'));
    vi.stubGlobal('fetch', fetchMock);

    await getMemorySessions();
    expect(await getMemorySessions({ force: true })).toEqual(SESS_A); // kept
    expect(errSpy).toHaveBeenCalled();
  });

  it('a failed FIRST load propagates typed non-2xx detail; 401 is Error(401) and never cached', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(status(503, { detail: 'memory service unavailable' })),
    );
    const failed = getMemorySessions();
    await expect(failed).rejects.toBeInstanceOf(ApiError);
    await expect(failed).rejects.toMatchObject({
      status: 503,
      url: '/api/memory/sessions',
      detail: 'memory service unavailable',
    });

    _resetMemoryCache();
    const fetch401 = vi.fn().mockResolvedValue(status(401));
    vi.stubGlobal('fetch', fetch401);
    await expect(getMemorySessions()).rejects.toThrow('401');
    // Not cached: the next call fetches again.
    await expect(getMemorySessions()).rejects.toThrow('401');
    expect(fetch401).toHaveBeenCalledTimes(2);
  });

  it('force bypasses the TTL', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ sessions: SESS_A }))
      .mockResolvedValueOnce(ok({ sessions: SESS_B }));
    vi.stubGlobal('fetch', fetchMock);

    await getMemorySessions();
    expect(await getMemorySessions({ force: true })).toEqual(SESS_B);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('invalidateMemoryCache() forces the next read to refetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ sessions: SESS_A }));
    vi.stubGlobal('fetch', fetchMock);

    await getMemorySessions();
    invalidateMemoryCache();
    await getMemorySessions();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('getMemorySemantic — SWR cache (keyed session|type)', () => {
  it('caches per session+type and hits the right endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ items: FACTS }));
    vi.stubGlobal('fetch', fetchMock);

    expect(await getMemorySemantic('s1', 'facts')).toEqual(FACTS);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/memory/semantic?session_id=s1&type=facts',
      { credentials: 'same-origin' },
    );
    await getMemorySemantic('s1', 'facts'); // cache hit
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await getMemorySemantic('s1', 'preferences'); // different type → own slot
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('deleteMemoryRecord', () => {
  it('DELETEs the record and invalidates the cache', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ items: FACTS })) // prime cache
      .mockResolvedValueOnce(ok({ deleted: true, id: 'rec-1' })) // DELETE
      .mockResolvedValueOnce(ok({ items: [] })); // post-delete reload
    vi.stubGlobal('fetch', fetchMock);

    await getMemorySemantic('s1', 'facts');
    expect(await deleteMemoryRecord('rec-1')).toBe(true);
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/memory/record?record_id=rec-1', {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    // Cache dropped — the next read refetches.
    expect(await getMemorySemantic('s1', 'facts')).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('maps 404/401/other to thrown errors and keeps the cache intact', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ items: FACTS }))
      .mockResolvedValueOnce(status(404, { detail: 'record not found' }));
    vi.stubGlobal('fetch', fetchMock);

    await getMemorySemantic('s1', 'facts');
    await expect(deleteMemoryRecord('rec-x')).rejects.toThrow('404');
    // Failed mutation does NOT invalidate — cached read still serves.
    expect(await getMemorySemantic('s1', 'facts')).toEqual(FACTS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
