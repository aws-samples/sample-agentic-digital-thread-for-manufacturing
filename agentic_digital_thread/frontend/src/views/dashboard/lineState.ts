// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Line State loaders + drill (S5.4) — the non-render half of the old
 * lineState.ts (jsx-migration W2.1). loadLineState stores the payload
 * (LineStateCard renders computeLineState over it; the Ops Summary derives
 * its {running, idle, down} from the same slice); openLineStateDrill drives
 * the shared detail modal (tables render in DetailModal.tsx). */
import { ApiError, requestJson } from '../../api/client';
import type { DimensionPayload, LineStatePayload } from '../../api/types/dashboard';
import { dimensionErrorEnvelope, resolveDimension } from './dimension-loader';
import {
  dashboardStore,
  patchModal,
  registerPanelLoader,
  setLineStatePayload,
  systemLabel,
} from './store';
import { makeSwrLoader } from './swr';

/* SWR-guarded (swr.ts): repeat snapshot events share one in-flight fetch and
 * skip the network while the payload is <15s fresh; a failed refresh keeps
 * the cached payload; only a failed FIRST load stores the error envelope
 * (the card's visible error state — unchanged fail-soft design). */
const _lineStateLoader = makeSwrLoader({
  name: 'line-state',
  fetch: async (signal) => {
    setLineStatePayload(
      await requestJson<LineStatePayload>('/api/dashboard/line-state', { signal }),
    );
  },
  onFirstFailure: (e) =>
    setLineStatePayload({ status: 'error', error: String((e as Error)?.message || e) }),
});

export function loadLineState(): Promise<void> {
  return _lineStateLoader.load();
}

/** Test hook — reset the SWR guard between specs. */
export function _resetLineStateLoader(): void {
  _lineStateLoader.reset();
}

// By-dimension card routing (Issue 1): 'machine-state' snapshots re-resolve
// through this loader instead of ever becoming a KPI tile.
registerPanelLoader('machine-state', loadLineState);

/** POST a resolve-by-dimension request; returns the payload or a friendly
 * error envelope (dashboard.js `_resolveByDim`). */
export async function resolveByDim(
  metricId: string,
  dimensionConcept: string,
): Promise<DimensionPayload> {
  try {
    return await resolveDimension({ metricId, dimensionConcept });
  } catch (error) {
    return dimensionErrorEnvelope(error);
  }
}

/** One system's contribution to the fan-out drill envelope (Task 2C).
 * `kind: 'machines'` rows are [asset, state]; `kind: 'distribution'` rows are
 * [state, count] (that system lacks an event-time mapping — the modal renders
 * the honest caption). */
export interface LineStateDrillSystem {
  system_id?: string;
  name?: string;
  site?: string;
  kind?: string; // 'machines' | 'distribution'
  rows?: unknown[][];
}

/** GET /api/dashboard/line-state/machines envelope (Task 2C). Structurally a
 * superset of DimensionPayload (all-optional fields), so it rides the existing
 * `machines` modal content kind without touching the store's ModalContent
 * union — DetailModal routes on `isLineStateMachinesEnvelope`. */
export interface LineStateMachinesEnvelope {
  status?: string;
  systems?: LineStateDrillSystem[];
  error?: string;
}

/** Type guard: grouped fan-out envelope vs the legacy single-system payload. */
export function isLineStateMachinesEnvelope(p: unknown): p is LineStateMachinesEnvelope {
  return !!p && typeof p === 'object' && Array.isArray((p as LineStateMachinesEnvelope).systems);
}

/** Line State click-to-drill: WHICH machines are running/idle/down, across
 * ALL covering systems (Task 2C). Calls the fan-out endpoint (per-machine
 * LATEST rows where a system maps event-time, per-state distribution
 * fallback per system); falls back to the legacy single-authority two-step
 * flow ONLY when the endpoint 404s (rolling-deploy safety: older backend
 * without the route). */
export async function openLineStateDrill(): Promise<void> {
  patchModal({
    open: true,
    title: 'Line State — machines by current state',
    sub: 'machine-state \u00b7 latest per asset',
    key: null,
    lineage: null,
    content: { kind: 'loading', label: 'Loading machines\u2026' },
  });

  try {
    const envelope = await requestJson<LineStateMachinesEnvelope>(
      '/api/dashboard/line-state/machines',
    );
    if (!dashboardStore.get().modal.open) return; // user closed while loading
    if (envelope && Array.isArray(envelope.systems)) {
      patchModal({
        sub: 'machine-state \u00b7 per machine across covering systems',
        content: { kind: 'machines', payload: envelope },
      });
      return;
    }
  } catch (error) {
    if (!dashboardStore.get().modal.open) return;
    if (error instanceof ApiError && error.status === 404) {
      // Rolling-deploy safety: backend predates the fan-out endpoint.
      return legacyLineStateDrill();
    }
  }
  // Non-404 failure (network / 5xx / malformed body): honest error state —
  // MachineStateTable renders the friendly unavailable note.
  patchModal({
    content: {
      kind: 'machines',
      payload: { status: 'error', error: 'line-state machines unavailable' },
    },
  });
}

/** Legacy single-authority two-step drill (pre-Task-2C flow, kept verbatim as
 * the 404 fallback): per-asset LATEST first, then the per-state distribution
 * so the drill is never emptier than the card. */
async function legacyLineStateDrill(): Promise<void> {
  // 1) Preferred: per-machine current state (LATEST per asset).
  const latest = await resolveByDim('machine-state-latest', 'asset');
  if (!dashboardStore.get().modal.open) return; // user closed while loading
  const hasMachines =
    latest && latest.status !== 'error' && Array.isArray(latest.rows) && latest.rows.length;
  if (hasMachines) {
    patchModal({
      sub: systemLabel(latest.system_id) + ' \u00b7 machines by current state',
      content: { kind: 'machines', payload: latest },
    });
    return;
  }
  // 2) Fallback: per-state distribution (COUNT) — the SAME data the card shows.
  const dist = await resolveByDim('machine-state-distribution', 'machine-state');
  if (!dashboardStore.get().modal.open) return;
  patchModal({
    sub:
      systemLabel(dist && dist.system_id) +
      ' \u00b7 counts by state (per-machine detail needs an event-time mapping)',
    content: { kind: 'distribution', payload: dist },
  });
}
