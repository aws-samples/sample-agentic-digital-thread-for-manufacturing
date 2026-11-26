// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Stale-while-revalidate cache for the memory panel's data (sessions +
 * facts/summaries/preferences) — mirrors the loadSystems cache in
 * views/dashboard/systems.ts (loading-state audit): module-level, short TTL,
 * in-flight dedupe, resp.ok checks, keep-cached-on-failed-refresh. NOT the
 * full AsyncResource framework — a small cache in the chat module.
 *
 * Semantics per getter call:
 *   • fresh cache (inside TTL, no force) → resolve cached, NO network;
 *   • stale cache → resolve cached IMMEDIATELY and revalidate in the
 *     background (`onFresh` fires with the new data when it lands; a failed
 *     refresh KEEPS the cached data, log-only);
 *   • no cache (or force) → await the fetch; on force-failure with a cached
 *     copy, the cached copy is served (log-only) instead of erroring.
 *   • requestJson owns checked HTTP/auth handling. A 401 is translated back
 *     to Error('401') for MemoryPanel's established empty-state contract.
 *
 * Mutations (record delete) call invalidateMemoryCache() so the next read
 * refetches. */
import type {
  MemorySession,
  SemanticItem,
  SemanticResponse,
  SemanticType,
  SessionsResponse,
} from '../api/types/memory';
import { ApiError, requestJson } from '../api/client';

export const MEMORY_CACHE_TTL_MS = 30_000;

interface Entry<T> {
  data: T;
  at: number;
}

export interface MemoryGetOpts<T> {
  /** Bypass the TTL (refresh button / end-of-turn refreshToken bump). */
  force?: boolean;
  /** Called with revalidated data when a stale-serve's background refresh lands. */
  onFresh?: (data: T) => void;
}

let _sessionsCache: Entry<MemorySession[]> | null = null;
let _sessionsInflight: Promise<MemorySession[]> | null = null;
const _semanticCache = new Map<string, Entry<SemanticItem[]>>();
const _semanticInflight = new Map<string, Promise<SemanticItem[]>>();

/** Drop every cached memory read (call after ANY memory mutation). */
export function invalidateMemoryCache(): void {
  _sessionsCache = null;
  _semanticCache.clear();
}

/** Test hook — reset the module cache (including in-flight) between specs. */
export function _resetMemoryCache(): void {
  invalidateMemoryCache();
  _sessionsInflight = null;
  _semanticInflight.clear();
}

async function fetchSessions(): Promise<MemorySession[]> {
  try {
    const data = await requestJson<SessionsResponse>('/api/memory/sessions');
    return data.sessions || [];
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) throw new Error('401');
    throw error;
  }
}

async function fetchSemantic(sessionId: string, type: SemanticType): Promise<SemanticItem[]> {
  try {
    const data = await requestJson<SemanticResponse | SemanticItem[]>(
      `/api/memory/semantic?session_id=${encodeURIComponent(sessionId)}&type=${encodeURIComponent(type)}`,
    );
    return (Array.isArray(data) ? data : data.items) || [];
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) throw new Error('401');
    throw error;
  }
}

/** Shared SWR core for one cache slot. */
function swrGet<T>(
  cached: Entry<T> | null,
  getInflight: () => Promise<T> | null,
  setInflight: (p: Promise<T> | null) => void,
  setCache: (e: Entry<T>) => void,
  fetcher: () => Promise<T>,
  opts: MemoryGetOpts<T>,
): Promise<T> {
  const fresh = !!cached && Date.now() - cached.at < MEMORY_CACHE_TTL_MS;
  if (cached && fresh && !opts.force) return Promise.resolve(cached.data);
  // In-flight dedupe: concurrent callers share one fetch.
  let inflight = getInflight();
  if (!inflight) {
    inflight = fetcher()
      .then((data) => {
        setCache({ data, at: Date.now() });
        return data;
      })
      .finally(() => setInflight(null));
    setInflight(inflight);
  }
  if (cached && !opts.force) {
    // Stale-while-revalidate: serve the cached copy NOW; notify when the
    // background refresh lands. A failed refresh keeps the cache (log-only).
    inflight
      .then((data) => opts.onFresh?.(data))
      .catch((e) => console.error('Memory refresh failed (serving cached):', e));
    return Promise.resolve(cached.data);
  }
  // No cache, or an explicit force: await the fetch. A FORCED refresh that
  // fails with a cached copy available keeps serving the cache (log-only);
  // a failed first load propagates so the panel can show its error state.
  return inflight.catch((e) => {
    if (cached) {
      console.error('Memory refresh failed (serving cached):', e);
      return cached.data;
    }
    throw e;
  });
}

/** GET /api/memory/sessions through the SWR cache. */
export function getMemorySessions(
  opts: MemoryGetOpts<MemorySession[]> = {},
): Promise<MemorySession[]> {
  return swrGet(
    _sessionsCache,
    () => _sessionsInflight,
    (p) => {
      _sessionsInflight = p;
    },
    (e) => {
      _sessionsCache = e;
    },
    fetchSessions,
    opts,
  );
}

/** GET /api/memory/semantic through the SWR cache (keyed session|type). */
export function getMemorySemantic(
  sessionId: string,
  type: SemanticType,
  opts: MemoryGetOpts<SemanticItem[]> = {},
): Promise<SemanticItem[]> {
  const key = `${sessionId}|${type}`;
  return swrGet(
    _semanticCache.get(key) || null,
    () => _semanticInflight.get(key) || null,
    (p) => {
      if (p) _semanticInflight.set(key, p);
      else _semanticInflight.delete(key);
    },
    (e) => {
      _semanticCache.set(key, e);
    },
    () => fetchSemantic(sessionId, type),
    opts,
  );
}

/** DELETE /api/memory/record — remove ONE semantic record the user judged
 * unhelpful, then drop the cache so the next read refetches. Resolves true
 * on success; preserves legacy Error('404'|'401') and otherwise propagates
 * the checked ApiError. */
export async function deleteMemoryRecord(recordId: string): Promise<boolean> {
  try {
    await requestJson<unknown>(`/api/memory/record?record_id=${encodeURIComponent(recordId)}`, {
      method: 'DELETE',
    });
    invalidateMemoryCache();
    return true;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 404)) {
      throw new Error(String(error.status));
    }
    throw error;
  }
}
