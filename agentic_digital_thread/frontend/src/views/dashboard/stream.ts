// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Dashboard SSE + snapshot processing — connectDashboardSSE /
 * disconnectDashboardSSE / processDashboardData / loadDashboardData
 * (handlers dispatch into the
 * dashboard store; React renders). Uses the Wave-1 openEventSource wrapper
 * for the native EventSource stream (/api/dashboard/stream?plant=). The
 * EventSource lifecycle stays gated on the dashboard view being active
 * (DashboardView's activation effect connects/disconnects). */
import { requestJson } from '../../api/client';
import { openEventSource } from '../../api/sse';
import type {
  CardUpdatePayload,
  DashboardPayload,
  IotMessage,
  IotStats,
} from '../../api/types/dashboard';
import { appendLiveFeedMessage } from './feed';
import { loadLineState } from './lineState';
import {
  loadActiveIssues,
  loadOutputByLine,
  loadQualityByLine,
  loadSensorHeatmap,
  loadWorkOrdersByStatus,
} from './Panels';
import {
  applyHeroFromSnapshot,
  dashboardStore,
  handleCardUpdate,
  setDashboardData,
  setIotStats,
  setSnapshotFailed,
  upsertCards,
} from './store';
import { loadSystems, setPlantReconnectHook } from './systems';

declare global {
  interface Window {
    _dashboardSSE?: EventSource | null;
    _systemsRefresh?: ReturnType<typeof setInterval> | null;
  }
}

/** Stagger increment between the lazy panel loaders. The backend is a single
 * ECS task; at first paint the convoy of 7 lazy requests used to hit the
 * origin in one burst, which (with a blocked event loop) queued them all
 * behind the same stall. Spacing them 250ms apart is a cheap client-side
 * mitigation — steady-state cost is nil because the SWR guards no-op fresh
 * loaders anyway. */
export const PANEL_STAGGER_MS = 250;

/* The lazy panel convoy, in fire order (each self-guarded — SWR dedupe/TTL/
 * backoff in swr.ts — so a repeat schedule is a cheap no-op). */
const LAZY_PANEL_LOADERS: Array<() => Promise<void>> = [
  loadLineState,
  loadQualityByLine,
  // Concept-driven dimension bars (work-orders by lifecycle-state, output by
  // production-line) — same declarative resolve-by-dimension seam +
  // contract.
  loadWorkOrdersByStatus,
  loadOutputByLine,
  loadActiveIssues,
  loadSensorHeatmap,
];

let _staggerPending = false;

/** Fire the lazy panel loaders on a 250ms stagger. One schedule at a time:
 * snapshots arriving while a stagger is in flight are dropped (the loaders
 * they would fire are the ones already scheduled). */
function fireLazyPanelLoads(): void {
  if (_staggerPending) return;
  _staggerPending = true;
  LAZY_PANEL_LOADERS.forEach((load, i) => {
    setTimeout(() => {
      if (i === LAZY_PANEL_LOADERS.length - 1) _staggerPending = false;
      void load(); // fire-and-forget, self-guarded
    }, i * PANEL_STAGGER_MS);
  });
}

/** Test hook — clear the pending-stagger latch between specs. */
export function _resetPanelStagger(): void {
  _staggerPending = false;
}

/** How long the Ops Summary keeps "Loading operations summary" after a stream
 * error that arrives before the first snapshot. A single early error is often
 * transient (a cold first snapshot; EventSource retries on its own), so the
 * error + Retry state waits for this window, or for a second stream error in a
 * row, before it shows. */
export const SNAPSHOT_GRACE_MS = 20_000;

/** Snapshot-less stream errors since the last snapshot, the window timer
 * started by the first of them, and the abort for that error's GET fallback. */
const _grace: {
  errors: number;
  timer: ReturnType<typeof setTimeout> | null;
  abort: AbortController | null;
} = {
  errors: 0,
  timer: null,
  abort: null,
};

/** Stream connection generation, bumped on every connect and disconnect. The
 * stream handlers, the grace timer and the GET fallback capture the value of
 * the connection that created them and do nothing once it is no longer the
 * current one: a closed source's queued error, or an old connection's late
 * GET, must not open, count against or close the current connection's grace
 * window (or fill the summary after a plant change or unmount). */
let _connGen = 0;

function clearSnapshotGrace(): void {
  if (_grace.timer) clearTimeout(_grace.timer);
  _grace.timer = null;
  _grace.errors = 0;
  _grace.abort?.abort();
  _grace.abort = null;
}

/** Test hook — clear the grace window between specs. */
export function _resetSnapshotGrace(): void {
  clearSnapshotGrace();
}

function snapshotMissing(): boolean {
  return dashboardStore.get().dashboardData === null;
}

function showSnapshotFailure(): void {
  clearSnapshotGrace();
  if (snapshotMissing()) setSnapshotFailed(true);
}

/** The GET fallback fired inside the grace window for connection `gen`. Its
 * failure is logged, not shown: the window (or a second stream error) decides
 * when the error shows. It is aborted when its connection ends, and a reply
 * that arrives after that is dropped. */
async function loadDashboardDataQuietly(gen: number, signal: AbortSignal): Promise<void> {
  try {
    const data = await requestJson<DashboardPayload>('/api/dashboard', { signal });
    if (gen === _connGen) processDashboardData(data);
  } catch (error) {
    if (gen === _connGen && !signal.aborted) console.error('Failed to load dashboard data:', error);
  }
}

/** Stream error handler for connection `gen`. With a snapshot on screen it is
 * a silent reconnect. Before the first snapshot: the first error starts the
 * grace window and fires ONE GET fallback; a second error in a row, or the
 * window ending with still no snapshot, shows the error + Retry state. */
function onDashboardStreamError(gen: number): void {
  if (gen !== _connGen) return;
  if (!snapshotMissing() || dashboardStore.get().snapshotFailed) return;
  _grace.errors += 1;
  if (_grace.errors > 1) {
    showSnapshotFailure();
    return;
  }
  _grace.timer = setTimeout(() => {
    if (gen === _connGen) showSnapshotFailure();
  }, SNAPSHOT_GRACE_MS);
  const abort = new AbortController();
  _grace.abort = abort;
  void loadDashboardDataQuietly(gen, abort.signal);
}

/** S5.3 Wave 3 — mapping-driven cutover: the SSE snapshot / GET payload carries
 * the resolver cards at the TOP LEVEL ({connected_types, cards}). Upserts the
 * cards into the store (the Ops Summary derives from the same snapshot), then
 * fire-and-forgets the lazy panels (each self-guarded — never blocks or breaks
 * the snapshot render). */
export function processDashboardData(raw: DashboardPayload | null | undefined): void {
  const data = raw || {};
  clearSnapshotGrace();
  setDashboardData(data);
  try {
    upsertCards(data.cards);
  } catch (e) {
    console.error('Cards render error:', e);
  }
  const lr = document.getElementById('last-refreshed');
  if (lr) lr.textContent = 'Updated ' + new Date().toLocaleTimeString();
  // Lazy visible panels AFTER first paint — drill sections load only on click.
  fireLazyPanelLoads();
}

/** GET /api/dashboard fallback loader (the SSE snapshot is the primary path).
 * Failure flips the store's snapshotFailed flag so the Ops Summary can show
 * an error/retry state instead of loading forever. */
export async function loadDashboardData(): Promise<void> {
  try {
    processDashboardData(await requestJson<DashboardPayload>('/api/dashboard'));
  } catch (error) {
    console.error('Failed to load dashboard data:', error);
    setSnapshotFailed(true);
  }
}

export function connectDashboardSSE(): void {
  disconnectDashboardSSE();
  const gen = ++_connGen;
  // A source's events count only while it is the current connection.
  const scoped = (fn: (data: unknown) => void) => (data: unknown) => {
    if (gen === _connGen) fn(data);
  };
  const plant =
    document.querySelector<HTMLElement>('.plant-tab.active')?.dataset.plant || 'all';
  const es = openEventSource(
    '/api/dashboard/stream?plant=' + plant,
    {
      dashboard: scoped((data) => {
        try {
          const raw = data as DashboardPayload;
          processDashboardData(raw);
          // Toggle empty hero based on connected_types
          if (raw.connected_types) {
            applyHeroFromSnapshot(raw.connected_types);
          }
        } catch (err) {
          console.error('SSE dashboard error:', err);
        }
      }),
      mqtt: scoped((data) => {
        try {
          appendLiveFeedMessage(data as IotMessage);
        } catch {
          /* tolerate malformed messages */
        }
      }),
      stats: scoped((data) => {
        try {
          // Task 1C: stats go into the store (LiveFeed renders the counts and
          // the honest empty states) — the direct #mqtt-stats DOM write is gone.
          setIotStats(data as IotStats);
        } catch {
          /* tolerate malformed stats */
        }
      }),
      // S3.2 re-derivation ripple — updates ONLY the named cards in place.
      card_update: scoped((data) => {
        try {
          handleCardUpdate(data as CardUpdatePayload);
        } catch (err) {
          console.error('card_update', err);
        }
      }),
    },
    () => {
      if (gen !== _connGen) return; // a closed source's queued error
      console.warn('Dashboard SSE reconnecting...');
      // Never received a snapshot AND the stream is erroring → after the grace
      // window (or a second error) break the Ops Summary out of the eternal
      // "Loading…" (error + retry state). Once a snapshot exists this is a
      // silent reconnect, exactly as before.
      onDashboardStreamError(gen);
    },
  );
  window._dashboardSSE = es;
  // Periodically refresh system chips (picks up newly onboarded systems)
  window._systemsRefresh = setInterval(loadSystems, 15000);
  // Plant tab clicks reconnect with the new filter (dashboard.js selectPlant).
  setPlantReconnectHook(connectDashboardSSE);
}

export function disconnectDashboardSSE(): void {
  _connGen += 1; // anything the closed connection started is now stale
  clearSnapshotGrace();
  if (window._dashboardSSE) {
    window._dashboardSSE.close();
    window._dashboardSSE = null;
  }
  if (window._systemsRefresh) {
    clearInterval(window._systemsRefresh);
    window._systemsRefresh = null;
  }
}
