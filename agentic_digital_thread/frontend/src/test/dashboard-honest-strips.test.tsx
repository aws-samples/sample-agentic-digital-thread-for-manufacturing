// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — two honest-dashboard fixes found on a deployed multi-plant stack:
 * (1) the quality-by-line subtitle survives a multi-plant payload (ADR-0094:
 *     `system_id` null + a `sites` breakdown) by naming every plant that
 *     answered, while single-system wording stays byte-identical; the count-bar
 *     panels (work orders by status, output by line) get the same source line;
 * (2) a card whose EVERY metric is withheld pending a steward choice
 *     (ambiguous-binding / unresolved-predicate) leaves the KPI grid for the
 *     strip's "Needs a steward choice:" group, whose chip opens the detail
 *     view with the server's own reason — never a dead tile, never no-data;
 * (3) an SSE `card_update` moves the strip, the KPI tile AND the Operations
 *     Summary together (one current card collection), in both directions. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { DashboardCard, DimensionPayload } from '../api/types/dashboard';
import { KpiRow, UncoveredStrip } from '../views/dashboard/Cards';
import { cardNeedsStewardChoice, computeOperationsSummary } from '../views/dashboard/lib';
import { OpsSummaryContent } from '../views/dashboard/OpsSummary';
import { OutputByLinePanel, QualityByLinePanel, WorkOrdersByStatusPanel } from '../views/dashboard/Panels';
import {
  dashboardStore,
  handleCardUpdate,
  setDashboardData,
  setOutputByLine,
  setQuality,
  setSystemsById,
  setWorkOrders,
  upsertCards,
} from '../views/dashboard/store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  upsertCards([]);
  dashboardStore.update((s) => ({ ...s, dashboardData: null, snapshotFailed: false, systems: null }));
  setQuality(null);
  setWorkOrders(null);
  setOutputByLine(null);
  setSystemsById({});
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  setSystemsById({});
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

const PASS_RATE = ' \u00b7 Pass rate = % of units passing quality checks.';

// ── Fix 1: quality-by-line subtitle on a multi-plant payload ──────────────────

describe('QualityByLinePanel subtitle', () => {
  it('single-system wording is byte-identical: Source: <system label> · Pass rate …', () => {
    setSystemsById({ 'mes-a': { id: 'mes-a', name: 'Plant MES', plant: 'Site A' } });
    setQuality({
      status: 'ok',
      system_id: 'mes-a',
      columns: [{ label: 'Line' }, { label: 'Pass Rate' }],
      rows: [['L1', 0.98], ['L2', 0.91]],
    });
    render(<QualityByLinePanel />);
    const subs = container.querySelectorAll('.dim-bar-subtitle');
    expect(subs).toHaveLength(1);
    expect(subs[0].textContent).toBe('Source: Plant MES \u00b7 Site A' + PASS_RATE);
  });

  it('a multi-plant payload names each plant that answered, in payload order, ok sites only', () => {
    setSystemsById({
      'mes-site-a': { id: 'mes-site-a', name: 'MES', plant: 'Site A' },
      'mes-site-b': { id: 'mes-site-b', name: 'MES', plant: 'Site B' },
      'mes-site-c': { id: 'mes-site-c', name: 'MES', plant: 'Site C' },
    });
    const payload: DimensionPayload = {
      status: 'ok',
      system_id: null,
      group_by: 'production-line',
      columns: [{ label: 'Line' }, { label: 'Pass Rate' }],
      rows: [
        ['Site B \u00b7 L1', 0.97],
        ['Site A \u00b7 L3', 0.88],
      ],
      sites: [
        { site: 'Site B', system_id: 'mes-site-b', status: 'ok', rows: 1 },
        { site: 'Site C', system_id: 'mes-site-c', status: 'uncovered', missing: 'quality-result' },
        { site: 'Site A', system_id: 'mes-site-a', status: 'ok', rows: 1 },
      ],
    };
    setQuality(payload);
    render(<QualityByLinePanel />);
    const subs = container.querySelectorAll('.dim-bar-subtitle');
    // Exactly one subtitle carrying "pass rate" (the deployed e2e contract,
    // tests/e2e/test_dashboard_layout.py::test_quality_panel_has_context_subtitle).
    expect(subs).toHaveLength(1);
    expect(subs[0].textContent).toBe(
      'Source: MES \u00b7 Site B, MES \u00b7 Site A' + PASS_RATE,
    );
    expect(subs[0].textContent!.toLowerCase()).toContain('pass rate');
    expect(subs[0].textContent).not.toContain('Site C');
    expect(container.querySelectorAll('.dim-bar-row')).toHaveLength(2);
  });

  it('falls back to the system id when the systems cache is not loaded yet', () => {
    setQuality({
      status: 'ok',
      system_id: null,
      rows: [['A \u00b7 L1', 0.9]],
      sites: [
        { site: 'A', system_id: 'mes-a', status: 'ok', rows: 1 },
        { site: 'B', system_id: 'mes-b', status: 'error' },
      ],
    });
    render(<QualityByLinePanel />);
    expect(container.querySelector('.dim-bar-subtitle')!.textContent).toBe('Source: mes-a' + PASS_RATE);
  });

  it('renders no subtitle when no system id and no plant answered', () => {
    setQuality({
      status: 'ok',
      system_id: null,
      rows: [['L1', 0.9]],
      sites: [{ site: 'A', system_id: 'mes-a', status: 'error' }],
    });
    render(<QualityByLinePanel />);
    expect(container.querySelector('.dim-bar-subtitle')).toBeNull();
    expect(container.querySelectorAll('.dim-bar-row')).toHaveLength(1);
  });
});

// ── Fix 1b: count-bar panels (work orders by status, output by line) ─────────

/** The source subtitle only — output-by-line also renders a window caption
 * with the same `.dim-bar-subtitle` class. */
function sourceSubtitles(panelId: string): Element[] {
  const panel = document.getElementById(panelId)!;
  return Array.from(panel.querySelectorAll('.dim-bar-subtitle:not(.dim-bar-window-caption)'));
}

const MULTI_SITES: DimensionPayload['sites'] = [
  { site: 'Site B', system_id: 'mes-site-b', status: 'ok', rows: 2 },
  { site: 'Site C', system_id: 'mes-site-c', status: 'error' },
  { site: 'Site A', system_id: 'mes-site-a', status: 'ok', rows: 1 },
];

describe('count-bar panels subtitle', () => {
  beforeEach(() => {
    setSystemsById({
      'mes-site-a': { id: 'mes-site-a', name: 'MES', plant: 'Site A' },
      'mes-site-b': { id: 'mes-site-b', name: 'MES', plant: 'Site B' },
      'mes-site-c': { id: 'mes-site-c', name: 'MES', plant: 'Site C' },
    });
  });

  it('work orders: single-system wording is byte-identical (Source: <system label>)', () => {
    setWorkOrders({
      status: 'ok',
      system_id: 'mes-site-a',
      unit: 'orders',
      columns: [{ label: 'Status' }, { label: 'Open Work Orders' }],
      rows: [['Released', 8], ['Completed', 16]],
    });
    render(<WorkOrdersByStatusPanel />);
    const subs = sourceSubtitles('work-orders-by-status-panel');
    expect(subs).toHaveLength(1);
    expect(subs[0].textContent).toBe('Source: MES \u00b7 Site A');
  });

  it('work orders: a multi-plant payload names the ok sites in payload order', () => {
    setWorkOrders({
      status: 'ok',
      system_id: null,
      unit: 'orders',
      columns: [{ label: 'Status' }, { label: 'Open Work Orders' }],
      rows: [
        ['Site B \u00b7 Released', 3],
        ['Site B \u00b7 Completed', 9],
        ['Site A \u00b7 Released', 5],
      ],
      sites: MULTI_SITES,
    });
    render(<WorkOrdersByStatusPanel />);
    const subs = sourceSubtitles('work-orders-by-status-panel');
    expect(subs).toHaveLength(1);
    expect(subs[0].textContent).toBe('Source: MES \u00b7 Site B, MES \u00b7 Site A');
    expect(subs[0].textContent).not.toContain('Site C');
    expect(document.querySelectorAll('#work-orders-by-status-panel .dim-bar-row')).toHaveLength(3);
  });

  it('output by line: single-system keeps its window caption and byte-identical source line', () => {
    setOutputByLine({
      status: 'ok',
      system_id: 'mes-site-b',
      unit: 'units',
      columns: [{ label: 'Line' }, { label: 'Output' }],
      rows: [['L1', 120]],
    });
    render(<OutputByLinePanel />);
    const panel = document.getElementById('output-by-line-panel')!;
    expect(panel.querySelector('.dim-bar-window-caption')!.textContent).toBe(
      'Units produced \u00b7 lifetime total',
    );
    const subs = sourceSubtitles('output-by-line-panel');
    expect(subs).toHaveLength(1);
    expect(subs[0].textContent).toBe('Source: MES \u00b7 Site B');
  });

  it('output by line: a multi-plant payload names the ok sites in payload order', () => {
    setOutputByLine({
      status: 'ok',
      system_id: null,
      unit: 'units',
      columns: [{ label: 'Line' }, { label: 'Output' }],
      rows: [['Site B \u00b7 L1', 120], ['Site A \u00b7 L3', 80]],
      sites: MULTI_SITES,
    });
    render(<OutputByLinePanel />);
    const subs = sourceSubtitles('output-by-line-panel');
    expect(subs).toHaveLength(1);
    expect(subs[0].textContent).toBe('Source: MES \u00b7 Site B, MES \u00b7 Site A');
  });

  it('renders no source line when no system id and no plant answered', () => {
    setWorkOrders({
      status: 'ok',
      system_id: null,
      rows: [['Released', 2]],
      sites: [{ site: 'A', system_id: 'mes-a', status: 'uncovered', missing: 'lifecycle-state' }],
    });
    render(<WorkOrdersByStatusPanel />);
    expect(sourceSubtitles('work-orders-by-status-panel')).toHaveLength(0);
    expect(document.querySelectorAll('#work-orders-by-status-panel .dim-bar-row')).toHaveLength(1);
  });
});

// ── Fix 2: steward-choice withholds go to the strip ───────────────────────────

const CANDIDATES_DETAIL =
  "concept 'spare-parts-on-hand' is bound to 2 distinct columns on system 'cmms' "
  + '(parts: on_hand_qty, reserved_qty); a steward must choose one via POST '
  + '/api/metrics/set-authority {field}';

const sparePartsCard = (over: Partial<DashboardCard> = {}): DashboardCard => ({
  card_id: 'spare-parts-on-hand',
  title: 'Spare Parts On Hand',
  viz_type: 'kpi',
  metrics: [
    {
      metric_id: 'spare-parts-on-hand',
      status: 'ambiguous-binding',
      value: null,
      detail: CANDIDATES_DETAIL,
    },
  ],
  ...over,
});

const machineSpeedCard: DashboardCard = {
  card_id: 'machine-speed',
  title: 'Machine Speed',
  viz_type: 'kpi',
  metrics: [
    {
      metric_id: 'machine-speed',
      status: 'ambiguous-binding',
      value: null,
      detail: "concept 'machine-speed' is bound to 2 distinct columns on system 'mes'",
    },
  ],
};

describe('store — steward-choice cards route to the strip', () => {
  it('a card whose every metric is ambiguous-binding never becomes a tile', () => {
    upsertCards([sparePartsCard(), machineSpeedCard]);
    const s = dashboardStore.get();
    expect(s.cardOrder).toEqual([]);
    expect(s.cards['spare-parts-on-hand']).toBeUndefined();
    expect(s.uncovered['spare-parts-on-hand']).toEqual({
      title: 'Spare Parts On Hand',
      missing: [],
      kind: 'steward-choice',
      reasons: [
        { metric_id: 'spare-parts-on-hand', status: 'ambiguous-binding', detail: CANDIDATES_DETAIL },
      ],
    });
    expect(s.uncovered['machine-speed']?.kind).toBe('steward-choice');
  });

  it('unresolved-predicate (and a mix of the two) routes the same way, never as no-data', () => {
    upsertCards([
      sparePartsCard({
        card_id: 'mixed',
        metrics: [
          { metric_id: 'a', status: 'ambiguous-binding', detail: 'two columns' },
          { metric_id: 'b', status: 'unresolved-predicate', detail: "filter concept 'shift' has no bound field" },
        ],
      }),
    ]);
    const entry = dashboardStore.get().uncovered['mixed'];
    expect(entry.kind).toBe('steward-choice');
    expect(entry.reasons!.map((r) => r.status)).toEqual(['ambiguous-binding', 'unresolved-predicate']);
  });

  it('a speed-vs-target card withheld on every metric is a steward choice, not "No data yet"', () => {
    upsertCards([
      {
        card_id: 'performance',
        title: 'Performance',
        viz_type: 'gauge',
        metrics: [
          { metric_id: 'performance-ratio', status: 'ambiguous-binding' },
          { metric_id: 'machine-speed', status: 'ambiguous-binding' },
          { metric_id: 'target-machine-speed', status: 'ambiguous-binding' },
        ],
      },
    ]);
    expect(dashboardStore.get().uncovered.performance?.kind).toBe('steward-choice');
  });

  it('a card with at least one ok metric stays a tile, unchanged', () => {
    const performance: DashboardCard = {
      card_id: 'performance',
      title: 'Performance',
      viz_type: 'gauge',
      metrics: [
        { metric_id: 'performance-ratio', status: 'ok', value: 0.82, sample_count: 8 },
        { metric_id: 'machine-speed', status: 'ambiguous-binding', detail: 'two columns' },
        { metric_id: 'target-machine-speed', status: 'ok', value: 60, sample_count: 8 },
      ],
    };
    upsertCards([performance]);
    const s = dashboardStore.get();
    expect(s.cardOrder).toEqual(['performance']);
    expect(s.cards.performance).toBe(performance);
    expect(s.uncovered.performance).toBeUndefined();
  });

  it('an invariant-violation card stays a tile (a data fault, not a choice)', () => {
    upsertCards([
      sparePartsCard({
        card_id: 'inv',
        metrics: [{ metric_id: 'inv', status: 'invariant-violation', detail: 'value 1.4 outside [0, 1]' }],
      }),
    ]);
    expect(dashboardStore.get().cardOrder).toEqual(['inv']);
    expect(dashboardStore.get().uncovered.inv).toBeUndefined();
  });

  it('a card resolving after the steward chooses leaves the strip and becomes a tile', () => {
    upsertCards([sparePartsCard()]);
    expect(dashboardStore.get().uncovered['spare-parts-on-hand']).toBeDefined();
    handleCardUpdate({
      status: 'updated',
      cards: [sparePartsCard({ metrics: [{ metric_id: 'spare-parts-on-hand', status: 'ok', value: 412 }] })],
    });
    const s = dashboardStore.get();
    expect(s.uncovered['spare-parts-on-hand']).toBeUndefined();
    expect(s.cardOrder).toEqual(['spare-parts-on-hand']);
  });

  it('a tile flipping to a steward-choice withhold loses its tile', () => {
    upsertCards([sparePartsCard({ metrics: [{ metric_id: 'spare-parts-on-hand', status: 'ok', value: 5 }] })]);
    expect(dashboardStore.get().cardOrder).toEqual(['spare-parts-on-hand']);
    upsertCards([sparePartsCard()]);
    const s = dashboardStore.get();
    expect(s.cardOrder).toEqual([]);
    expect(s.cards['spare-parts-on-hand']).toBeUndefined();
    expect(s.uncovered['spare-parts-on-hand']?.kind).toBe('steward-choice');
  });

  it('cardNeedsStewardChoice is false for empty or metric-less cards', () => {
    expect(cardNeedsStewardChoice(null)).toBe(false);
    expect(cardNeedsStewardChoice({ card_id: 'x', metrics: [] })).toBe(false);
    expect(cardNeedsStewardChoice({ card_id: 'x' })).toBe(false);
  });
});

describe('UncoveredStrip — "Needs a steward choice" group', () => {
  it('renders the label and openable chips; the chip shows the server reason with candidate columns', () => {
    upsertCards([sparePartsCard(), machineSpeedCard]);
    render(<UncoveredStrip />);
    const strip = document.getElementById('uncovered-strip')!;
    const label = strip.querySelector('.uncovered-strip-label-steward')!;
    expect(label.textContent).toBe('Needs a steward choice:');
    // No "Awaiting data:" or "No data yet:" group for these cards.
    expect(strip.textContent).not.toContain('Awaiting data:');
    expect(strip.textContent).not.toContain('No data yet:');
    const chips = Array.from(strip.querySelectorAll<HTMLButtonElement>('.uncovered-strip-chip'));
    expect(chips.map((c) => c.textContent)).toEqual(['Machine Speed', 'Spare Parts On Hand']);
    expect(chips.every((c) => c.tagName === 'BUTTON')).toBe(true);

    act(() => chips[1].click());
    const dialog = document.querySelector('[role="dialog"]')!;
    expect(dialog.getAttribute('aria-label')).toBe('Why Spare Parts On Hand is withheld');
    expect(dialog.textContent).toContain('Spare Parts On Hand');
    expect(dialog.textContent).toContain(
      'Withheld \u2014 the concept binds to several columns; a steward must choose one',
    );
    expect(dialog.textContent).toContain('parts: on_hand_qty, reserved_qty');
    // The coverage-gap Investigate check is about unmapped concepts — not offered here.
    expect(dialog.textContent).not.toContain('Investigate');
    expect(dialog.textContent).not.toContain('Waiting for data');
    const closeBtn = Array.from(dialog.querySelectorAll('button')).find((b) => b.textContent === 'Close')!;
    act(() => closeBtn.click());
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('sits beside the other groups as a third group', () => {
    upsertCards([
      sparePartsCard(),
      { card_id: 'u1', title: 'Throughput', status: 'uncovered', missing_concepts: ['throughput'] },
      { card_id: 'n1', title: 'Scrap', status: 'no-data' },
    ]);
    render(<UncoveredStrip />);
    const labels = Array.from(document.querySelectorAll('#uncovered-strip .uncovered-strip-label')).map(
      (n) => n.textContent,
    );
    expect(labels).toEqual(['Awaiting data:', 'No data yet:', 'Needs a steward choice:']);
  });

  it('hostile server detail stays text', () => {
    upsertCards([
      sparePartsCard({
        metrics: [
          { metric_id: 'x', status: 'ambiguous-binding', detail: '<img src=x onerror=alert(1)>' },
        ],
      }),
    ]);
    render(<UncoveredStrip />);
    act(() => document.querySelector<HTMLButtonElement>('.uncovered-strip-chip')!.click());
    const dialog = document.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain('<img src=x onerror=alert(1)>');
    expect(dialog.querySelector('img')).toBeNull();
  });
});

describe('computeOperationsSummary — steward-choice rollup', () => {
  it('counts a steward-choice card on its own line, not as unavailable or no-data', () => {
    const model = computeOperationsSummary([sparePartsCard()], null);
    expect(model.kind).toBe('insights');
    if (model.kind !== 'insights') return;
    expect(model.insights).toEqual([
      { level: 'warning', text: '1 metric needs a steward choice' },
    ]);
  });

  it('pluralizes', () => {
    const model = computeOperationsSummary([sparePartsCard(), machineSpeedCard], null);
    if (model.kind !== 'insights') throw new Error('expected insights');
    expect(model.insights.map((i) => i.text)).toEqual(['2 metrics need a steward choice']);
  });
});

// ── Fix 3: a card_update moves every view together ────────────────────────────

const throughputCard: DashboardCard = {
  card_id: 'throughput',
  title: 'Throughput',
  viz_type: 'kpi',
  metrics: [{ metric_id: 'throughput', status: 'ok', value: 90, unit: 'units', sample_count: 12 }],
};

const resolvedSpareParts = sparePartsCard({
  metrics: [{ metric_id: 'spare-parts-on-hand', status: 'ok', value: 412, sample_count: 3 }],
});

/** The dashboard's first snapshot, exactly as processDashboardData applies it
 * (minus the lazy panel convoy, which would hit the network). */
function applySnapshot(cards: DashboardCard[]) {
  act(() => {
    setDashboardData({ cards });
    upsertCards(cards);
  });
}

function renderDashboardViews() {
  render(
    <>
      <div id="t-kpi"><KpiRow /></div>
      <UncoveredStrip />
      <div id="t-ops"><OpsSummaryContent /></div>
    </>,
  );
}

const opsText = () => document.getElementById('t-ops')!.textContent || '';
const stewardChips = () =>
  Array.from(document.querySelectorAll('#uncovered-strip .uncovered-strip-chip')).map((c) => c.textContent);

describe('card_update ripple — strip, tile and Operations Summary move together', () => {
  it('steward-choice -> ok: chip leaves the strip, the tile appears, the summary line clears', () => {
    applySnapshot([throughputCard, sparePartsCard()]);
    renderDashboardViews();
    expect(stewardChips()).toEqual(['Spare Parts On Hand']);
    expect(document.getElementById('kpi-card-spare-parts-on-hand')).toBeNull();
    expect(opsText()).toContain('1 metric needs a steward choice');

    act(() => handleCardUpdate({ status: 'updated', cards: [resolvedSpareParts] }));

    expect(document.getElementById('uncovered-strip')).toBeNull();
    expect(document.getElementById('kpi-card-spare-parts-on-hand')).not.toBeNull();
    expect(opsText()).not.toContain('steward choice');
    expect(opsText()).toContain('All clear \u2014 no exceptions across 2 metrics.');
  });

  it('ok -> steward-choice: the tile leaves, the chip appears, the summary gains the line', () => {
    applySnapshot([throughputCard, resolvedSpareParts]);
    renderDashboardViews();
    expect(document.getElementById('kpi-card-spare-parts-on-hand')).not.toBeNull();
    expect(document.getElementById('uncovered-strip')).toBeNull();
    expect(opsText()).toContain('All clear');

    act(() => handleCardUpdate({ status: 'updated', cards: [sparePartsCard()] }));

    expect(document.getElementById('kpi-card-spare-parts-on-hand')).toBeNull();
    expect(stewardChips()).toEqual(['Spare Parts On Hand']);
    expect(opsText()).toContain('1 metric needs a steward choice');
    expect(opsText()).not.toContain('All clear');
  });

  it('the pre-existing rollups follow a card_update too (ok -> unavailable, then -> no-data)', () => {
    applySnapshot([throughputCard, resolvedSpareParts]);
    renderDashboardViews();
    expect(opsText()).toContain('All clear');

    act(() =>
      handleCardUpdate({
        status: 'updated',
        cards: [sparePartsCard({
          metrics: [{ metric_id: 'spare-parts-on-hand', status: 'unavailable', error: 'timeout' }],
        })],
      }),
    );
    expect(opsText()).toContain('1 metric unavailable');

    act(() =>
      handleCardUpdate({
        status: 'updated',
        cards: [sparePartsCard({ metrics: [{ metric_id: 'spare-parts-on-hand', status: 'no-data' }] })],
      }),
    );
    expect(opsText()).not.toContain('unavailable');
    expect(opsText()).toContain('1 metric need data (mapped, no rows yet)');
  });

  it('replaces by card_id in place and never adds a card the snapshot did not carry', () => {
    applySnapshot([throughputCard, sparePartsCard()]);
    act(() =>
      handleCardUpdate({
        status: 'updated',
        cards: [resolvedSpareParts, { ...machineSpeedCard }],
      }),
    );
    const cards = dashboardStore.get().dashboardData!.cards!;
    // Replace-only, like the server's update_cards_cache: machine-speed was
    // not in the snapshot, so it does not enter the summary's collection.
    expect(cards.map((c) => c.card_id)).toEqual(['throughput', 'spare-parts-on-hand']);
    expect(cards[1]).toBe(resolvedSpareParts);
  });

  it('a by-dimension descriptor in an updated batch adds no "unavailable" line', () => {
    applySnapshot([throughputCard, resolvedSpareParts]);
    renderDashboardViews();
    expect(opsText()).toContain('All clear \u2014 no exceptions across 2 metrics.');

    // The corrections ripple (RESOLVER_REDERIVE) publishes group_by bar cards
    // the snapshot excludes: metrics [] and no status.
    act(() =>
      handleCardUpdate({
        status: 'updated',
        cards: [
          {
            card_id: 'quality-by-line',
            title: 'Quality by line',
            viz_type: 'bar',
            group_by: 'production-line',
            resolver: 'by-dimension',
            metrics: [],
          },
        ],
      }),
    );

    expect(dashboardStore.get().dashboardData!.cards!.map((c) => c.card_id)).toEqual([
      'throughput',
      'spare-parts-on-hand',
    ]);
    expect(opsText()).not.toContain('unavailable');
    expect(opsText()).toContain('All clear \u2014 no exceptions across 2 metrics.');
    expect(document.getElementById('kpi-card-quality-by-line')).toBeNull();
  });

  it('before the first snapshot a card_update leaves the summary loading (no partial summary)', () => {
    act(() => handleCardUpdate({ status: 'updated', cards: [resolvedSpareParts] }));
    expect(dashboardStore.get().dashboardData).toBeNull();
  });

  it("an 'updating' ripple does not touch the summary's cards", () => {
    applySnapshot([throughputCard, sparePartsCard()]);
    const before = dashboardStore.get().dashboardData;
    act(() => handleCardUpdate({ status: 'updating', card_ids: ['spare-parts-on-hand'] }));
    expect(dashboardStore.get().dashboardData).toBe(before);
  });
});
