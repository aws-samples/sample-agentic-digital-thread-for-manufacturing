// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Stale-while-revalidate loader guard (loading-state audit) — the house
 * systems.ts cache pattern, factored once for the five lazy panel loaders
 * that every SSE dashboard snapshot re-fires (stream.ts): in-flight dedupe,
 * a short freshness TTL (skip the network when the store data is <15s old),
 * and keep-cached-on-failed-refresh (a refresh failure after a successful
 * load logs and KEEPS the stored panel data; only a failed FIRST load runs
 * the loader's existing fail-safe). NOT a framework — a module-level guard
 * around each loader's own fetch+store body, same as systems.ts.
 *
 * Request-discipline additions (slowload diagnosis — the single-task backend
 * blocked its event loop, so panel requests hung >60s and every snapshot
 * re-fired them into fresh errors):
 *  - FETCH TIMEOUT: each attempt races an AbortController timeout (default
 *    30s, per-loader override). A hung request aborts, takes the loader's
 *    normal failure path, and ALWAYS releases the in-flight lock (no dead
 *    lock — the systems-cache no-dead-lock-after-failure contract).
 *  - ERROR BACKOFF: a failed loader enters a backoff window (60s, doubling
 *    to a 5min cap while failures persist; reset on success). Snapshot-driven
 *    refires inside the window are no-ops, so a broken endpoint is retried
 *    on the backoff schedule instead of on every SSE snapshot.
 *  - ERROR-ONCE SURFACING: the FIRST failure of a streak logs console.error
 *    (loader name + reason) and — on a failed first load — runs the loader's
 *    fail-safe (the panel's error affordance / hidden state). Repeat failures
 *    in the same streak stay silent; a success resets the streak. */

/** Panels already fresh within this window skip the refetch when a repeat
 * snapshot event re-fires their loader (systems.ts uses 30s for its slower
 * 15s poll; snapshots can burst much faster). */
export const PANEL_TTL_MS = 15_000;

/** Default per-attempt fetch timeout (per-loader override via timeoutMs). */
export const FETCH_TIMEOUT_MS = 30_000;

/** Initial error-backoff window after a loader failure. */
export const ERROR_BACKOFF_MS = 60_000;

/** Backoff cap while failures persist (doubles 60s → 120s → 240s → 300s). */
export const ERROR_BACKOFF_MAX_MS = 300_000;

export interface SwrLoader {
  /** The guarded loader — same call signature the snapshot path uses. */
  load: () => Promise<void>;
  /** Test hook — reset freshness + in-flight + backoff state between specs. */
  reset: () => void;
}

export function makeSwrLoader(opts: {
  /** Loader name for the console.error breadcrumbs. */
  name: string;
  /** Fetch + store write; MUST throw on failure (incl. !resp.ok) and SHOULD
   * pass the provided signal to its fetch() so a timeout aborts the network
   * request (the guard's timeout race fails the attempt either way). */
  fetch: (signal: AbortSignal) => Promise<void>;
  /** Existing fail-safe for a failed FIRST load (e.g. store null → panel
   * hidden), receiving the failure. A failed refresh after a success never
   * runs it — the previously stored data is kept (systems.ts
   * keep-cached-on-failed-refresh). Runs once per failure streak. */
  onFirstFailure?: (e: unknown) => void;
  ttlMs?: number;
  /** Per-attempt timeout before the request is aborted and the attempt
   * fails (default FETCH_TIMEOUT_MS). */
  timeoutMs?: number;
}): SwrLoader {
  const ttlMs = opts.ttlMs ?? PANEL_TTL_MS;
  const timeoutMs = opts.timeoutMs ?? FETCH_TIMEOUT_MS;
  let inflight: Promise<void> | null = null;
  let freshAt = 0; // epoch ms of the last successful load; 0 = never
  let failedAt = 0; // epoch ms of the last failed attempt; 0 = not failing
  let backoffMs = 0; // current backoff window; 0 = no active failure streak
  return {
    load(): Promise<void> {
      // In-flight dedupe: concurrent snapshot bursts share one fetch.
      if (inflight) return inflight;
      // Fresh enough — the store already holds the data; skip the network.
      if (freshAt && Date.now() - freshAt < ttlMs) return Promise.resolve();
      // Error backoff — a failed loader is NOT refired by every snapshot;
      // retries happen on the backoff schedule only.
      if (backoffMs && Date.now() - failedAt < backoffMs) return Promise.resolve();
      inflight = (async () => {
        // Timeout race: abort a hung request so the failure path runs and the
        // in-flight lock releases even when fetch never settles (the blocked-
        // backend hang exceeded 60s with no error state at all).
        const controller = new AbortController();
        const timer = setTimeout(
          () => controller.abort(new Error(opts.name + ' timed out after ' + timeoutMs + 'ms')),
          timeoutMs,
        );
        try {
          await new Promise<void>((resolve, reject) => {
            controller.signal.addEventListener('abort', () =>
              reject(
                controller.signal.reason instanceof Error
                  ? controller.signal.reason
                  : new Error(opts.name + ' timed out after ' + timeoutMs + 'ms'),
              ),
            );
            opts.fetch(controller.signal).then(resolve, reject);
          });
          freshAt = Date.now();
          failedAt = 0;
          backoffMs = 0; // success ends the failure streak (backoff resets)
        } catch (e) {
          const firstOfStreak = backoffMs === 0;
          failedAt = Date.now();
          backoffMs = firstOfStreak
            ? ERROR_BACKOFF_MS
            : Math.min(backoffMs * 2, ERROR_BACKOFF_MAX_MS);
          if (firstOfStreak) {
            // Error-once surfacing: log the streak's first failure only —
            // repeats while the endpoint stays broken are silent no-ops.
            if (freshAt) {
              // Failed background refresh — keep serving the stored data.
              console.error(
                '[dashboard] %s refresh failed (keeping cached panel data):',
                opts.name,
                e,
              );
            } else {
              // Failed FIRST load — log + run the loader's own fail-safe.
              console.error('[dashboard] %s load failed:', opts.name, e);
              try {
                opts.onFirstFailure?.(e);
              } catch {
                /* fail-safe: render nothing */
              }
            }
          }
        } finally {
          clearTimeout(timer);
          inflight = null; // ALWAYS release the lock (no dead lock)
        }
      })();
      return inflight;
    },
    reset(): void {
      inflight = null;
      freshAt = 0;
      failedAt = 0;
      backoffMs = 0;
    },
  };
}
