// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Entity sections cache + click-to-drill logic (S5.3 W3 / S5.4) — the
 * non-render half of the old sections.ts (jsx-migration W2.1). The section
 * cache, modal open/close state, and drill lineage now live in the dashboard
 * store; the modal markup + tables render in DetailModal.tsx. The sections are
 * CACHED for the drill modal, never painted inline (#entity-sections stays a
 * hidden anchor in DashboardView). */
import { requestJson } from '../../api/client';
import type { EntitySection, EntitySectionsResponse, Lineage } from '../../api/types/dashboard';
import {
  closeModal,
  dashboardStore,
  mergeSections,
  patchModal,
  sectionKey,
  setSections,
} from './store';
import { makeSwrLoader, type SwrLoader } from './swr';

export { closeModal } from './store';

/** Cache the row-shaped sections. Presence/data-driven: empty/absent clears
 * the cache. An open drill modal re-renders against the new cache
 * automatically (DetailModal reads sections[modal.key] from the store). */
export function renderEntitySections(
  sections: EntitySection[] | undefined | null,
  plant = activePlant(),
): void {
  setSections(sections, plant);
}

/** Shared correction POST (Fork F) — used by the lineage correct affordance. */
export function submitCorrection(args: {
  system_id: string;
  concept: string;
  to_field: string;
  from_field?: string;
}): Promise<unknown> {
  return requestJson<unknown>('/api/metrics/correct-mapping', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      system_id: args.system_id,
      concept: args.concept,
      to_field: args.to_field,
      from_field: args.from_field || '',
    }),
  });
}

/** Open the drill modal for a card's (system_id, table) target. Cache-first;
 * cold cache shows a loading state and awaits the deduped sections fetch. */
export async function openDrill(
  systemId: string | undefined,
  table: string | undefined,
  title: string | undefined,
  lineage: Lineage | null,
): Promise<void> {
  const plant = activePlant();
  const key = sectionKey(plant, systemId, table);
  const cached = !!dashboardStore.get().sections[key];
  const failed = _sectionErrors.has(key);
  patchModal({
    open: true,
    title: title || 'Details',
    sub: [systemId, table].filter(Boolean).join(' \u00b7 '),
    key,
    lineage: lineage || null,
    content: cached
      ? { kind: 'section' }
      : failed
        ? endpointError()
        : { kind: 'loading', label: 'Loading\u2026' },
  });
  if (!systemId || !table) {
    patchModal({ content: endpointError() });
    return;
  }
  await loadEntitySections(plant, systemId, table);
  settleOpenDrill(key);
}

function settleOpenDrill(key: string): void {
  const modal = dashboardStore.get().modal;
  if (!modal.open || modal.key !== key) return;
  if (_sectionErrors.has(key)) {
    if (!dashboardStore.get().sections[key]) patchModal({ content: endpointError() });
    return;
  }
  patchModal({ content: { kind: 'section' } });
}

export function closeModalOnOverlay(event: { target?: EventTarget | null }): void {
  const target = event && (event.target as HTMLElement | null);
  if (target && target.id === 'detail-modal-overlay') closeModal();
}

// ── Targeted sections fetch (bounded, per-key SWR) ──────────────────────────

const SECTION_LOADER_MAX = 128;
const ENDPOINT_ERROR = 'Drill-down data is temporarily unavailable. Please try again.';
const _sectionLoaders = new Map<string, SwrLoader>();
const _sectionErrors = new Set<string>();

function activePlant(): string {
  return document.querySelector<HTMLElement>('.plant-tab.active')?.dataset.plant || 'all';
}

function endpointError() {
  return { kind: 'error' as const, message: ENDPOINT_ERROR };
}

function targetUrl(plant: string, systemId: string, table: string): string {
  return '/api/dashboard/sections?plant=' + encodeURIComponent(plant)
    + '&system_id=' + encodeURIComponent(systemId)
    + '&table=' + encodeURIComponent(table);
}

function createSectionsLoader(
  plant: string,
  systemId: string,
  table: string,
  key: string,
): SwrLoader {
  return makeSwrLoader({
    name: 'entity-sections:' + key,
    fetch: async (signal) => {
      const data = await requestJson<EntitySectionsResponse>(
        targetUrl(plant, systemId, table), { signal },
      );
      const exact = (data.sections || []).filter(
        (section) => section.system_id === systemId && section.table === table,
      );
      _sectionErrors.delete(key);
      mergeSections(exact, plant);
    },
    onFirstFailure: () => {
      _sectionErrors.add(key);
      settleOpenDrill(key);
    },
  });
}

function sectionsLoader(plant: string, systemId: string, table: string): SwrLoader {
  const key = sectionKey(plant, systemId, table);
  const existing = _sectionLoaders.get(key);
  if (existing) {
    _sectionLoaders.delete(key);
    _sectionLoaders.set(key, existing);
    return existing;
  }
  while (_sectionLoaders.size >= SECTION_LOADER_MAX) {
    const oldest = _sectionLoaders.keys().next().value as string | undefined;
    if (oldest === undefined) break;
    _sectionLoaders.delete(oldest);
    _sectionErrors.delete(oldest);
  }
  const loader = createSectionsLoader(plant, systemId, table, key);
  _sectionLoaders.set(key, loader);
  return loader;
}

export function loadEntitySections(
  plant: string,
  systemId: string,
  table: string,
): Promise<void> {
  return sectionsLoader(plant, systemId, table).load();
}

/** Test hook — reset every keyed SWR guard between specs. */
export function _resetSectionsLoader(): void {
  _sectionLoaders.forEach((loader) => loader.reset());
  _sectionLoaders.clear();
  _sectionErrors.clear();
}
