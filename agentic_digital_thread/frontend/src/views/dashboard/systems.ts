// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Connected systems + plant selector logic — the non-render half of the old
 * systems.ts (jsx-migration W2.1). loadSystems stores the systems list (chips,
 * the [data-requires] panel gating, and the empty hero all render from the
 * store in SystemsStrip.tsx / LineStateCard.tsx / DashboardView.tsx) and keeps
 * the window._systemsById parity cache for drill labels.
 *
 * updatePlantTabs/selectPlant stay imperative + null-tolerant like the
 * original: the #plant-strip container is optional and not part of the SPA
 * dashboard markup today (all functions no-op without it). */
import { QueryObserver } from '@tanstack/react-query';

import {
  _resetSystemsQueryClient,
  fetchSystems,
  getSystemsQueryClient,
  systemsQueryOptions,
} from '../../api/systems-query';
import { queryKeys } from '../../api/query-keys';
import type { ConnectedSystemInfo } from '../../api/types/dashboard';
import { showToast } from '../../shared/thread/data-modal';
import { applyHeroFromSystems, setSystems, setSystemsById } from './store';

/** Reconnect hook injected by stream.ts (avoids an import cycle). */
let _reconnectSSE: (() => void) | null = null;
export function setPlantReconnectHook(fn: (() => void) | null): void {
  _reconnectSSE = fn;
}

/** Hook for switching to the onboard view (owned by the shell; wired by
 * Task 3.2 through window.switchView — guarded like the original's globals). */
export function switchToOnboard(): void {
  const sw = (window as unknown as { switchView?: (v: string) => void }).switchView;
  if (typeof sw === 'function') sw('onboard');
}

export function selectPlant(plant: string): void {
  document.querySelectorAll<HTMLElement>('.plant-tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.plant === plant);
  });
  // Reconnect SSE with plant filter
  if (_reconnectSSE) _reconnectSSE();
}

export function updatePlantTabs(systems: ConnectedSystemInfo[]): void {
  const strip = document.getElementById('plant-strip');
  if (!strip) return;
  const connected = systems.filter((s) => s.status === 'connected');
  const plants = [...new Set(connected.map((s) => s.plant).filter(Boolean))] as string[];
  const meta = strip.querySelector('.plant-strip-meta');
  const currentActive =
    strip.querySelector<HTMLElement>('.plant-tab.active')?.dataset.plant || 'all';
  strip.textContent = '';
  // All Plants button
  const allBtn = document.createElement('button');
  allBtn.className = 'plant-tab' + (currentActive === 'all' ? ' active' : '');
  allBtn.dataset.plant = 'all';
  allBtn.onclick = () => selectPlant('all');
  const allDot = document.createElement('span');
  allDot.className = 'plant-dot all';
  allBtn.appendChild(allDot);
  allBtn.appendChild(document.createTextNode(' All Plants'));
  strip.appendChild(allBtn);
  // Per-plant buttons
  plants.forEach((p) => {
    const btn = document.createElement('button');
    btn.className = 'plant-tab' + (currentActive === p.toLowerCase() ? ' active' : '');
    btn.dataset.plant = p.toLowerCase();
    btn.onclick = () => selectPlant(p.toLowerCase());
    const count = connected.filter((s) => s.plant === p).length;
    const dot = document.createElement('span');
    dot.className = 'plant-dot';
    btn.appendChild(dot);
    btn.appendChild(document.createTextNode(' ' + p + ' '));
    const countEl = document.createElement('span');
    countEl.style.fontSize = '0.65rem';
    countEl.style.color = 'var(--muted)';
    countEl.textContent = '(' + count + ')';
    btn.appendChild(countEl);
    strip.appendChild(btn);
  });
  // Re-add meta
  if (meta) strip.appendChild(meta);
}

/** Publish a systems list to the stores (chips, [data-requires] gating, empty
 * hero, plant tabs, window._systemsById drill-label cache). */
function applySystems(all: ConnectedSystemInfo[]): void {
  // Cache by id so drills can label a resolving system (S5.4).
  const byId: Record<string, ConnectedSystemInfo> = {};
  all.forEach((s) => {
    if (s && s.id) byId[s.id] = s;
  });
  setSystemsById(byId);
  // Store the list: chips, [data-requires] gating, and the empty hero all
  // derive from it in the React components.
  setSystems(all);
  const connected = all.filter((s) => s.status === 'connected');
  applyHeroFromSystems(connected.length);
  // Update plant tabs from connected systems (optional #plant-strip).
  updatePlantTabs(all);
}

/**
 * Keep Query's interval policy alive only while the dashboard view is active,
 * publishing every successful refresh through the existing store bridge.
 */
export function observeSystems(): () => void {
  const observer = new QueryObserver(
    getSystemsQueryClient(),
    systemsQueryOptions(),
  );
  let errorReported = false;

  const currentData = observer.getCurrentResult().data;
  if (currentData) applySystems(currentData.systems || []);

  const unsubscribe = observer.subscribe((result) => {
    if (result.data) applySystems(result.data.systems || []);
    if (result.isError && !errorReported) {
      errorReported = true;
      if (result.data) {
        console.error('Systems refresh failed (serving cached list):', result.error);
      } else {
        console.error('Systems load error:', result.error);
        showToast('Failed to load connected systems — retrying shortly');
      }
    } else if (!result.isError) {
      errorReported = false;
    }
  });

  return () => {
    unsubscribe();
    observer.destroy();
  };
}

/** Test hook — reset the isolated shared Query client between specs. */
export function _resetSystemsCache(): void {
  _resetSystemsQueryClient();
}

/**
 * Keep the legacy imperative dashboard API while Query owns freshness,
 * in-flight dedupe, and invalidation. The store remains the render bridge for
 * the existing dashboard components.
 */
export async function loadSystems(): Promise<void> {
  const queryClient = getSystemsQueryClient();
  const hadCachedData = queryClient.getQueryData(queryKeys.systems()) !== undefined;
  try {
    const { systems } = await fetchSystems(queryClient);
    applySystems(systems || []);
  } catch (e) {
    if (hadCachedData) {
      // Query retains the previous value after a failed invalidated refresh;
      // the dashboard store already renders that same value.
      console.error('Systems refresh failed (serving cached list):', e);
    } else {
      console.error('Systems load error:', e);
      showToast('Failed to load connected systems — retrying shortly');
    }
  }
}
