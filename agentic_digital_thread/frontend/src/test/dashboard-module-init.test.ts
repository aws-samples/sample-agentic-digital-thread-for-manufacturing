// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Module-init smoke test + dashboard-store reducer tests (jsx-migration W2.1).
 *
 * Importing the barrel proves the whole module graph (view + components +
 * store + logic modules) loads without import-time DOM access (all
 * window/document use is guarded or deferred into functions/effects/render).
 *
 * The store tests cover the contracts the old imperative cards.ts enforced in
 * DOM: create-or-update in place (stable ids, first-seen order, no removal on
 * partial snapshots), the empty-snapshot clear, the by-dimension/bar KPI-tile
 * exclusion with panel-loader routing (Issue 1 regression guard), and the
 * card_update ripple ('updating' mark → 'updated' re-render + highlight +
 * audit). */
import { beforeEach, describe, expect, it } from 'vitest';

import type { DashboardCard } from '../api/types/dashboard';
import {
  dashboardStore,
  handleCardUpdate,
  registerPanelLoader,
  upsertCards,
} from '../views/dashboard/store';

describe('dashboard module graph', () => {
  it('imports cleanly outside the browser and exports the mountable view', async () => {
    const mod = await import('../views/dashboard');
    expect(typeof mod.DashboardView).toBe('function');
  });
});

describe('dashboard store — upsertCards (renderCards semantics)', () => {
  beforeEach(() => {
    upsertCards([]); // clears cards/order/meta
  });

  const kpi = (id: string, extra: Partial<DashboardCard> = {}): DashboardCard => ({
    card_id: id,
    title: id,
    viz_type: 'kpi',
    metrics: [{ metric_id: id, status: 'ok', value: 1 }],
    ...extra,
  });

  it('creates cards in first-seen order and updates in place', () => {
    upsertCards([kpi('a'), kpi('b')]);
    expect(dashboardStore.get().cardOrder).toEqual(['a', 'b']);
    upsertCards([kpi('b', { title: 'B2' }), kpi('c')]);
    const s = dashboardStore.get();
    // 'a' is NOT removed by a snapshot that omits it; order is stable.
    expect(s.cardOrder).toEqual(['a', 'b', 'c']);
    expect(s.cards['b'].title).toBe('B2');
  });

  it('clears ALL cards on an empty/absent list', () => {
    upsertCards([kpi('a')]);
    upsertCards(undefined);
    expect(dashboardStore.get().cardOrder).toEqual([]);
    expect(dashboardStore.get().cards).toEqual({});
  });

  it('never creates a KPI tile for a by-dimension/bar card and routes to its panel loader', () => {
    let called = 0;
    registerPanelLoader('test-dim', () => {
      called += 1;
    });
    // group_by, viz_type bar, and resolver by-dimension all excluded:
    upsertCards([
      { card_id: 'test-dim', group_by: 'line' },
      { card_id: 'x', viz_type: 'bar' },
      { card_id: 'y', resolver: 'by-dimension' },
    ]);
    const s = dashboardStore.get();
    expect(s.cardOrder).toEqual([]);
    expect(called).toBe(1); // loader fired for the registered id only
  });

  it('strips a stale tile when a card flips to by-dimension', () => {
    upsertCards([kpi('flip')]);
    expect(dashboardStore.get().cardOrder).toEqual(['flip']);
    upsertCards([{ card_id: 'flip', viz_type: 'bar' }]);
    expect(dashboardStore.get().cardOrder).toEqual([]);
  });
});

describe('dashboard store — handleCardUpdate (S3.2 ripple)', () => {
  beforeEach(() => {
    upsertCards([]);
    upsertCards([
      { card_id: 'oee', title: 'OEE', viz_type: 'gauge', metrics: [] },
    ]);
  });

  it("'updating' marks only existing cards", () => {
    handleCardUpdate({ status: 'updating', card_ids: ['oee', 'ghost'] });
    const meta = dashboardStore.get().cardMeta;
    expect(meta['oee']?.updating).toBe(true);
    expect(meta['ghost']).toBeUndefined();
  });

  it("'updated' re-upserts, clears updating, bumps highlightSeq, stores audit", () => {
    handleCardUpdate({ status: 'updating', card_ids: ['oee'] });
    handleCardUpdate({
      status: 'updated',
      cards: [{ card_id: 'oee', title: 'OEE v2', viz_type: 'gauge', metrics: [] }],
      audit: { text: 'remapped availability' },
    });
    const s = dashboardStore.get();
    expect(s.cards['oee'].title).toBe('OEE v2');
    expect(s.cardMeta['oee'].updating).toBe(false);
    expect(s.cardMeta['oee'].highlightSeq).toBe(1);
    expect(s.cardMeta['oee'].audit).toBe('remapped availability');
    // A second ripple without audit keeps the prior audit and bumps the pulse.
    handleCardUpdate({
      status: 'updated',
      cards: [{ card_id: 'oee', title: 'OEE v3', viz_type: 'gauge', metrics: [] }],
    });
    expect(dashboardStore.get().cardMeta['oee'].highlightSeq).toBe(2);
    expect(dashboardStore.get().cardMeta['oee'].audit).toBe('remapped availability');
  });
});
