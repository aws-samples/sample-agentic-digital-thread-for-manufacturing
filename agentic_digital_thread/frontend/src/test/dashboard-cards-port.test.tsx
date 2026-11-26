// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — dashboard client port: (1) uncovered / needs-data cards
 * collapse into the store's "Awaiting data" registry (never a KPI tile) and
 * the <UncoveredStrip/> renders it (chips sorted, tooltip = missing concept
 * ids, tail line); (2) the two new concept-driven count-bar cards
 * (work-orders-by-status, output-by-line) route through registered panel
 * loaders — never tiles — and their panels render fixture payloads with
 * max-proportional bar widths + unit suffix (hidden for error/empty/null
 * payloads, the honest empty state). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, requestJson } from '../api/client';
import type { CardMetric, DashboardCard, DimensionPayload } from '../api/types/dashboard';
import { KpiCard, UncoveredStrip } from '../views/dashboard/Cards';
import {
  _resetPanelLoaders,
  ActiveIssuesPanel,
  loadOutputByLine,
  loadWorkOrdersByStatus,
  OutputByLinePanel,
  QualityByLinePanel,
  SensorHeatmapPanel,
  WorkOrdersByStatusPanel,
} from '../views/dashboard/Panels';
import { OpsSummaryContent } from '../views/dashboard/OpsSummary';
import {
  dashboardStore,
  handleCardUpdate,
  setActiveIssues,
  setDashboardData,
  setHeatmap,
  setLineStatePayload,
  setOutputByLine,
  setQuality,
  setSnapshotFailed,
  setWorkOrders,
  upsertCards,
} from '../views/dashboard/store';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});

const requestJsonMock = vi.mocked(requestJson);

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  requestJsonMock.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  upsertCards([]); // clears cards/order/meta AND the uncovered registry
  dashboardStore.update((state) => ({
    ...state,
    dashboardData: null,
    snapshotFailed: false,
    quality: undefined,
    workOrders: undefined,
    outputByLine: undefined,
    activeIssues: undefined,
    heatmap: undefined,
  }));
  setLineStatePayload({ status: 'error', error: 'test-reset' }); // headline → null
  _resetPanelLoaders();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

const kpi = (id: string, extra: Partial<DashboardCard> = {}): DashboardCard => ({
  card_id: id,
  title: id,
  viz_type: 'kpi',
  metrics: [{ metric_id: id, status: 'ok', value: 1 }],
  ...extra,
});

const uncoveredCard = (id: string, extra: Partial<DashboardCard> = {}): DashboardCard => ({
  card_id: id,
  title: 'Card ' + id,
  status: 'uncovered',
  missing_concepts: ['concept-a', 'concept-b'],
  ...extra,
});

describe('KpiCard motion', () => {
  it('enters once and replays update highlighting on a frame without reading layout', () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.stubGlobal('cancelAnimationFrame', vi.fn());

    render(<KpiCard card={kpi('motion')} meta={{ highlightSeq: 1 }} />);
    const card = document.getElementById('kpi-card-motion')!;
    expect(card.classList.contains('card-enter')).toBe(true);
    expect(card.classList.contains('highlight')).toBe(false);
    expect(frames).toHaveLength(1);

    act(() => frames.shift()!(0));
    expect(card.classList.contains('highlight')).toBe(true);

    render(<KpiCard card={kpi('motion')} meta={{ highlightSeq: 2 }} />);
    expect(card.classList.contains('highlight')).toBe(false);
    expect(frames).toHaveLength(1);
    act(() => frames.shift()!(16));
    expect(card.classList.contains('highlight')).toBe(true);
  });
});

// ── Store routing: uncovered / needs-data → strip registry, never a tile ─────

describe('store — uncovered cards collapse into the awaiting-data registry', () => {
  it("an 'uncovered' card never becomes a tile and lands in the registry", () => {
    upsertCards([kpi('ok1'), uncoveredCard('u1')]);
    const s = dashboardStore.get();
    expect(s.cardOrder).toEqual(['ok1']);
    expect(s.cards['u1']).toBeUndefined();
    expect(s.uncovered['u1']).toEqual({
      title: 'Card u1',
      missing: ['concept-a', 'concept-b'],
      kind: 'structural',
    });
  });

  it("a 'needs-data' card routes the same way", () => {
    upsertCards([uncoveredCard('n1', { status: 'needs-data' })]);
    const s = dashboardStore.get();
    expect(s.cardOrder).toEqual([]);
    expect(s.uncovered['n1']).toBeDefined();
  });

  it('a card flipping TO uncovered removes its stale tile', () => {
    upsertCards([kpi('flip')]);
    expect(dashboardStore.get().cardOrder).toEqual(['flip']);
    upsertCards([uncoveredCard('flip')]);
    const s = dashboardStore.get();
    expect(s.cardOrder).toEqual([]);
    expect(s.cards['flip']).toBeUndefined();
    expect(s.uncovered['flip']).toBeDefined();
  });

  it('a card flipping OUT of uncovered is dropped from the registry and becomes a tile', () => {
    upsertCards([uncoveredCard('back')]);
    expect(dashboardStore.get().uncovered['back']).toBeDefined();
    upsertCards([kpi('back')]);
    const s = dashboardStore.get();
    expect(s.uncovered['back']).toBeUndefined();
    expect(s.cardOrder).toEqual(['back']);
  });

  it('an empty snapshot clears the registry (strip removed)', () => {
    upsertCards([uncoveredCard('u1')]);
    upsertCards([]);
    expect(dashboardStore.get().uncovered).toEqual({});
  });

  it('routes speed-vs-target with a missing target to No data yet, but keeps sampled zero with a target', () => {
    const performanceCard: DashboardCard = {
      card_id: 'performance',
      title: 'Performance',
      viz_type: 'gauge',
      metrics: [
        { metric_id: 'performance-ratio', status: 'ok', value: 0, sample_count: 8 },
        { metric_id: 'machine-speed', status: 'ok', value: 0, sample_count: 8 },
        { metric_id: 'target-machine-speed', status: 'error', value: null },
      ],
    };
    upsertCards([performanceCard]);
    expect(dashboardStore.get().cardOrder).toEqual([]);
    expect(dashboardStore.get().uncovered.performance?.kind).toBe('no-data');

    upsertCards([
      {
        ...performanceCard,
        metrics: [
          { metric_id: 'performance-ratio', status: 'ok', value: 0, sample_count: 8 },
          { metric_id: 'machine-speed', status: 'ok', value: 0, sample_count: 8 },
          { metric_id: 'target-machine-speed', status: 'ok', value: 60, sample_count: 8 },
        ],
      },
    ]);
    expect(dashboardStore.get().cardOrder).toEqual(['performance']);
    expect(dashboardStore.get().uncovered.performance).toBeUndefined();
  });

  it("the card_update ripple routes uncovered cards to the registry with no pulse", () => {
    upsertCards([kpi('r1')]);
    handleCardUpdate({ status: 'updated', cards: [uncoveredCard('r1')] });
    const s = dashboardStore.get();
    expect(s.cards['r1']).toBeUndefined();
    expect(s.uncovered['r1']).toBeDefined();
    expect(s.cardMeta['r1']).toBeUndefined(); // no highlight pulse
  });

  it('missing title falls back to the card id', () => {
    upsertCards([{ card_id: 'u2', status: 'uncovered' }]);
    expect(dashboardStore.get().uncovered['u2']).toEqual({
      title: 'u2',
      missing: [],
      kind: 'structural',
    });
  });
});

// ── <UncoveredStrip/> render ──────────────────────────────────────────────────

describe('UncoveredStrip', () => {
  it('renders nothing when no cards are awaiting data', () => {
    render(<UncoveredStrip />);
    expect(document.getElementById('uncovered-strip')).toBeNull();
  });

  it('renders label, sorted clickable chips, and a detail popover on click', () => {
    upsertCards([
      uncoveredCard('zeta', { title: 'Zeta Card' }),
      uncoveredCard('alpha', { title: 'Alpha Card', missing_concepts: ['only-one'] }),
    ]);
    render(<UncoveredStrip />);
    const strip = document.getElementById('uncovered-strip')!;
    expect(strip).not.toBeNull();
    expect(strip.querySelector('.uncovered-strip-label')!.textContent).toBe('Awaiting data:');
    const chips = Array.from(strip.querySelectorAll<HTMLButtonElement>('.uncovered-strip-chip'));
    // Sorted by card id: alpha before zeta; chips are BUTTONS (click → detail).
    expect(chips.map((c) => c.textContent)).toEqual(['Alpha Card', 'Zeta Card']);
    expect(chips.every((c) => c.tagName === 'BUTTON')).toBe(true);
    expect(strip.querySelector('.uncovered-strip-tail')!.textContent).toBe(
      '(choose an item to see what data it needs)',
    );
    // Click → popover names the missing concept; Close dismisses it.
    act(() => chips[0].click());
    const dialog = document.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain('Alpha Card');
    expect(dialog.textContent).toContain('only-one');
    const closeBtn = Array.from(dialog.querySelectorAll('button')).find(
      (b) => b.textContent === 'Close',
    )!;
    act(() => closeBtn.click());
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('Investigate posts an unscoped check, polls, and reports filed candidates', async () => {
    upsertCards([uncoveredCard('alpha', { title: 'Alpha Card', missing_concepts: ['throughput'] })]);
    requestJsonMock.mockImplementation((url: string) => {
      if (url === '/api/admin/learning/coverage-gap-check') {
        return Promise.resolve({ job_id: 'job-1' });
      }
      return Promise.resolve({ status: 'done', result: { findings: 2, filed: 1 } });
    });

    render(<UncoveredStrip />);
    act(() => document.querySelector<HTMLButtonElement>('.uncovered-strip-chip')!.click());
    const investigateBtn = Array.from(document.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Investigate'),
    )!;

    await act(async () => {
      investigateBtn.click();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain('Checking connected systems');

    await act(async () => {
      await new Promise((r) => setTimeout(r, 600));
    });
    expect(document.body.textContent).toContain('Found 1 candidate field');
    expect(document.body.textContent).toContain('Recent learning');

    // Unscoped: the POST body carries no system_id (a concept gap is not
    // owned by whichever card happens to display it).
    const postCall = requestJsonMock.mock.calls.find(
      ([url]) => url === '/api/admin/learning/coverage-gap-check',
    )!;
    expect(JSON.parse((postCall[1] as RequestInit).body as string)).toEqual({});
  });

  it('Investigate reports honestly when no candidate is found', async () => {
    upsertCards([uncoveredCard('alpha', { title: 'Alpha Card' })]);
    requestJsonMock.mockImplementation((url: string) =>
      url === '/api/admin/learning/coverage-gap-check'
        ? Promise.resolve({ job_id: 'job-1' })
        : Promise.resolve({ status: 'done', result: { findings: 0, filed: 0 } }),
    );

    render(<UncoveredStrip />);
    act(() => document.querySelector<HTMLButtonElement>('.uncovered-strip-chip')!.click());
    const investigateBtn = Array.from(document.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Investigate'),
    )!;
    await act(async () => {
      investigateBtn.click();
      await new Promise((r) => setTimeout(r, 600));
    });
    expect(document.body.textContent).toContain('No obvious candidate field found');
  });

  it('Investigate surfaces a sanitized error without crashing the popover', async () => {
    upsertCards([uncoveredCard('alpha', { title: 'Alpha Card' })]);
    requestJsonMock.mockImplementation((url: string) =>
      url === '/api/admin/learning/coverage-gap-check'
        ? Promise.resolve({ job_id: 'job-1' })
        : Promise.resolve({ status: 'error', error: 'coverage-gap check failed: TimeoutError' }),
    );

    render(<UncoveredStrip />);
    act(() => document.querySelector<HTMLButtonElement>('.uncovered-strip-chip')!.click());
    const investigateBtn = Array.from(document.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Investigate'),
    )!;
    await act(async () => {
      investigateBtn.click();
      await new Promise((r) => setTimeout(r, 600));
    });
    expect(document.body.textContent).toContain('Could not complete the check');
    expect(document.body.textContent).toContain('TimeoutError');
  });

  it('a chip with no missing concepts has no tooltip; XSS-hostile titles stay text', () => {
    upsertCards([
      uncoveredCard('x', { title: '<img src=x onerror=alert(1)>', missing_concepts: [] }),
    ]);
    render(<UncoveredStrip />);
    const chip = document.querySelector('.uncovered-strip-chip')!;
    expect(chip.textContent).toBe('<img src=x onerror=alert(1)>');
    expect(document.querySelector('#uncovered-strip img')).toBeNull();
  });

  it('disappears when the awaiting cards flip to covered', () => {
    upsertCards([uncoveredCard('u1')]);
    render(<UncoveredStrip />);
    expect(document.getElementById('uncovered-strip')).not.toBeNull();
    act(() => {
      upsertCards([kpi('u1')]);
    });
    expect(document.getElementById('uncovered-strip')).toBeNull();
  });
});

// ── New count-bar cards: loader routing by card_id ────────────────────────────

describe('by-dimension routing — new count-bar cards', () => {
  it('routes work-orders-by-status and output-by-line snapshots to their loaders (never tiles)', async () => {
    requestJsonMock.mockResolvedValue({ status: 'ok', rows: [] });

    upsertCards([
      { card_id: 'work-orders-by-status', group_by: 'lifecycle-state', viz_type: 'bar' },
      { card_id: 'output-by-line', group_by: 'production-line', viz_type: 'bar' },
    ]);
    await Promise.resolve();

    expect(dashboardStore.get().cardOrder).toEqual([]); // never KPI tiles
    const bodies = requestJsonMock.mock.calls
      .filter((c) => String(c[0]).includes('/api/metrics/resolve-by-dimension'))
      .map((c) => String((c[1] as { body?: unknown }).body));
    expect(
      bodies.some(
        (b) => b.includes('"metric_id":"work-order-count"') && b.includes('"dimension_concept":"lifecycle-state"'),
      ),
    ).toBe(true);
    expect(
      bodies.some(
        (b) => b.includes('"metric_id":"actual-quantity-windowed"') && b.includes('"dimension_concept":"production-line"'),
      ),
    ).toBe(true);
  });

  it('loaders store the payload (ok) / null (failed first load)', async () => {
    requestJsonMock.mockResolvedValue({ status: 'ok', rows: [['Open', 3]] });
    await loadWorkOrdersByStatus();
    expect(dashboardStore.get().workOrders).toEqual({ status: 'ok', rows: [['Open', 3]] });

    vi.spyOn(console, 'error').mockImplementation(() => {});
    requestJsonMock.mockRejectedValue(
      new ApiError(503, '/api/metrics/resolve-by-dimension', 'HTTP 503'),
    );
    await loadOutputByLine();
    expect(dashboardStore.get().outputByLine).toBeNull();
  });

});

// ── Count-bar panels render ───────────────────────────────────────────────────

const woFixture: DimensionPayload = {
  kind: 'metric-by-dimension',
  status: 'ok',
  group_by: 'lifecycle-state',
  label: 'Work Orders',
  unit: 'orders',
  system_id: 'sys-mes',
  columns: [{ label: 'Status' }, { label: 'Open Work Orders' }],
  rows: [
    ['Released', 8],
    ['In Progress', 4],
    ['Completed', 16],
  ],
};

describe('WorkOrdersByStatusPanel / OutputByLinePanel', () => {
  it('renders the fixture payload: title, source subtitle, max-proportional bars, unit', () => {
    setWorkOrders(woFixture);
    render(<WorkOrdersByStatusPanel />);
    const panel = document.getElementById('work-orders-by-status-panel')!;
    expect(panel.style.display).toBe('');
    expect(panel.querySelector('.dim-bar-title')!.textContent).toBe(
      'Open Work Orders by Status',
    );
    expect(panel.querySelector('.dim-bar-subtitle')!.textContent).toContain('Source: ');
    const labels = Array.from(panel.querySelectorAll('.dim-bar-label')).map(
      (n) => n.textContent,
    );
    expect(labels).toEqual(['Released', 'In Progress', 'Completed']);
    const fills = Array.from(
      panel.querySelectorAll<HTMLElement>('.dim-bar-fill'),
    ).map((n) => n.style.width);
    // Proportional to the MAX row value (16): 8→50%, 4→25%, 16→100%.
    expect(fills).toEqual(['50%', '25%', '100%']);
    const values = Array.from(panel.querySelectorAll('.dim-bar-value')).map(
      (n) => n.textContent,
    );
    expect(values).toEqual(['8 orders', '4 orders', '16 orders']);
  });

  it('labels output context and its normalized max while preserving missing values', () => {
    setOutputByLine({
      status: 'ok',
      group_by: 'production-line',
      label: 'Output',
      unit: 'units',
      rows: [
        ['Line 1', 154533],
        ['Line 2', 149381],
        ['Line 3', 'n/a'],
      ],
      window: '24h',
    } as DimensionPayload & { window: string });
    render(<OutputByLinePanel />);
    const panel = document.getElementById('output-by-line-panel')!;
    expect(panel.querySelector('.dim-bar-title')!.textContent).toBe(
      'Output by Production Line',
    );
    expect(panel.querySelector('.dim-bar-window-caption')!.textContent).toBe(
      'Units produced · last 24h',
    );
    expect(panel.querySelector('.dim-bar-axis')!.textContent).toContain(
      '154,533 units',
    );
    const fills = Array.from(panel.querySelectorAll<HTMLElement>('.dim-bar-fill')).map(
      (n) => n.style.width,
    );
    expect(fills.slice(0, 2)).toEqual(['100%', expect.stringMatching(/^96\./) ]);
    const values = Array.from(panel.querySelectorAll('.dim-bar-value')).map(
      (n) => n.textContent,
    );
    expect(values).toEqual(['154,533 units', '149,381 units', '\u2014']);
    expect(panel.querySelector('.dim-bar-value.muted')).not.toBeNull();
  });

  it('distinguishes never-loaded, transport failure, resolve failure, empty, and no-data compactly', () => {
    render(<WorkOrdersByStatusPanel />);
    const panel = document.getElementById('work-orders-by-status-panel')!;
    expect(panel.style.display).toBe('');
    expect(panel.querySelector('.async-state--loading.async-state--compact')).not.toBeNull();

    act(() => {
      setWorkOrders(null);
    });
    expect(panel.querySelector('.async-state--error.async-state--compact')?.textContent).toContain(
      'unavailable',
    );

    act(() => {
      setWorkOrders({
        status: 'error',
        error: "ambiguous join: 2 JoinPaths connect 'item_supplier' and 'supplier' in 'erp'",
      });
    });
    const mappingError = panel.querySelector('.panel-error-disclosure')!;
    expect(mappingError.querySelector('.async-state__message')?.textContent).toBe(
      "This chart's data mapping needs attention",
    );
    const mappingDetails = mappingError.querySelector('details')!;
    expect(mappingDetails.open).toBe(false);
    expect(mappingDetails.textContent).toContain(
      "ambiguous join: 2 JoinPaths connect 'item_supplier' and 'supplier' in 'erp'",
    );

    act(() => {
      setWorkOrders({ status: 'ok', rows: [] });
    });
    expect(panel.querySelector('.async-state--empty.async-state--compact')).not.toBeNull();
    expect(panel.querySelector('[data-testid="async-no-data"]')).toBeNull();

    act(() => {
      setWorkOrders({ status: 'no-data', rows: [] });
    });
    expect(panel.querySelector('[data-testid="async-no-data"].async-state--compact')).not.toBeNull();

    act(() => {
      setWorkOrders({ status: 'ok', rows: [['Open', null]] });
    });
    expect(panel.querySelector('[data-testid="async-no-data"]')).not.toBeNull();
    expect(panel.querySelector('.async-state--error')).toBeNull();
  });
});

// ── Task 1D — OEE gauge center honesty (system-reported tag + reconciliation) ─

const gaugeMetric = (
  id: string,
  value: number,
  extra: Partial<CardMetric> = {},
): CardMetric => ({ metric_id: id, status: 'ok', value, ...extra });

const gaugeCard = (metrics: CardMetric[]): DashboardCard => ({
  card_id: 'oee',
  title: 'OEE',
  viz_type: 'gauge',
  metrics,
});

describe('GaugeBody — OEE center honesty (Task 1D)', () => {
  it("labels a computable speed-vs-target gauge 'Machine Speed (of target)'", () => {
    render(
      <KpiCard
        card={{
          card_id: 'performance',
          title: 'Performance',
          viz_type: 'gauge',
          metrics: [
            gaugeMetric('performance-ratio', 0),
            gaugeMetric('machine-speed', 0, { sample_count: 8 }),
            gaugeMetric('target-machine-speed', 60, { sample_count: 8 }),
          ],
        }}
        meta={undefined}
      />,
    );
    expect(document.querySelector('#kpi-card-performance .kpi-title')?.textContent).toBe(
      'Machine Speed (of target)',
    );
    expect(document.querySelector('#kpi-card-performance .gauge-center')?.textContent).toBe('0%');
  });

  it("renders the 'system-reported' tag when the primary lineage has prefer_stored_hit", () => {
    render(
      <KpiCard
        card={gaugeCard([
          gaugeMetric('oee', 76.07, { lineage: { prefer_stored_hit: true } }),
          gaugeMetric('availability', 90, { label: 'Availability' }),
        ])}
        meta={undefined}
      />,
    );
    const tag = document.getElementById('kpi-card-oee-gauge-tag')!;
    expect(tag).not.toBeNull();
    expect(tag.textContent).toBe('system-reported');
  });

  it('no tag when prefer_stored_hit is absent or falsy', () => {
    render(
      <KpiCard
        card={gaugeCard([
          gaugeMetric('oee', 76.07, { lineage: { prefer_stored_hit: false } }),
        ])}
        meta={undefined}
      />,
    );
    expect(document.getElementById('kpi-card-oee-gauge-tag')).toBeNull();

    render(<KpiCard card={gaugeCard([gaugeMetric('oee', 76.07)])} meta={undefined} />);
    expect(document.getElementById('kpi-card-oee-gauge-tag')).toBeNull();
  });

  it('renders the reconciliation line when A×P×Q diverges >2pt from the center', () => {
    // Center 76.07 (stored composite) vs components 0.90×0.85×0.93 = 71.145 → 4.9pt gap.
    render(
      <KpiCard
        card={gaugeCard([
          gaugeMetric('oee', 76.07, { lineage: { prefer_stored_hit: true } }),
          gaugeMetric('availability', 0.9),
          gaugeMetric('performance', 0.85),
          gaugeMetric('oee-quality', 93),
        ])}
        meta={undefined}
      />,
    );
    const line = document.getElementById('kpi-card-oee-gauge-reconcile')!;
    expect(line).not.toBeNull();
    expect(line.textContent).toBe('A\u00d7P\u00d7Q: 71.1%');
  });

  it('non-ratio sub-readings render their own unit, never a bogus percent', () => {
    // machine-speed 0 units/min was rendering '0%' (toPercent treats any
    // number as percent) — a units/min reading must show its unit.
    render(
      <KpiCard
        card={gaugeCard([
          gaugeMetric('speed-vs-target', 0.9),
          gaugeMetric('machine-speed', 0, { unit: 'units/min', label: 'Machine Speed' }),
          gaugeMetric('target-machine-speed', 36.67, { unit: 'units/min', label: 'Target Machine Speed' }),
        ])}
        meta={undefined}
      />,
    );
    const subs = [...document.querySelectorAll('.gauge-sub-value')].map((n) => n.textContent);
    expect(subs).toContain('0 units/min');
    expect(subs).toContain('36.67 units/min');
    expect(subs).not.toContain('0%');
    expect(subs).not.toContain('36.67%');
  });

  it('no reconciliation line when the product matches the center within 2pt', () => {
    // 0.9×0.9×90% = 72.9 — center 72.9, zero divergence.
    render(
      <KpiCard
        card={gaugeCard([
          gaugeMetric('oee', 72.9),
          gaugeMetric('availability', 0.9),
          gaugeMetric('performance', 0.9),
          gaugeMetric('oee-quality', 90),
        ])}
        meta={undefined}
      />,
    );
    expect(document.getElementById('kpi-card-oee-gauge-reconcile')).toBeNull();
  });

  it('no reconciliation line when a sub-reading is missing or unresolved', () => {
    render(
      <KpiCard
        card={gaugeCard([
          gaugeMetric('oee', 76.07),
          gaugeMetric('availability', 0.9),
          gaugeMetric('performance', 0.85),
          { metric_id: 'oee-quality', status: 'error', error: 'boom' },
        ])}
        meta={undefined}
      />,
    );
    expect(document.getElementById('kpi-card-oee-gauge-reconcile')).toBeNull();
  });

  it('sub-reading labels prefer the backend label (trailing "rate" stripped)', () => {
    render(
      <KpiCard
        card={gaugeCard([
          gaugeMetric('oee', 76.07),
          gaugeMetric('availability', 0.9, { label: 'Availability' }),
          gaugeMetric('performance', 0.85, { label: 'Performance' }),
          gaugeMetric('oee-quality', 93, { label: 'Quality Rate' }),
        ])}
        meta={undefined}
      />,
    );
    const labels = Array.from(
      document.querySelectorAll('#kpi-card-oee .gauge-sub-label'),
    ).map((n) => n.textContent);
    expect(labels).toEqual(['Availability', 'Performance', 'Quality']);
  });
});

// ── Task 2B — no-data routing, "No data yet" bucket, site chips, idle note ────

describe('store — no-data cards collapse into the strip (Task 2B)', () => {
  it("a card-level 'no-data' status routes to the registry, never a tile", () => {
    upsertCards([kpi('ok1'), { card_id: 'nd1', title: 'ND One', status: 'no-data' }]);
    const s = dashboardStore.get();
    expect(s.cardOrder).toEqual(['ok1']);
    expect(s.cards['nd1']).toBeUndefined();
    expect(s.uncovered['nd1']).toEqual({ title: 'ND One', missing: [], kind: 'no-data' });
  });

  it('all-metrics-no-data routes the same; a mixed card stays a tile', () => {
    upsertCards([
      {
        card_id: 'nd2',
        title: 'All Degenerate',
        viz_type: 'kpi',
        metrics: [{ metric_id: 'm', status: 'no-data' }],
      },
      {
        card_id: 'mixed',
        title: 'Mixed',
        viz_type: 'kpi',
        metrics: [
          { metric_id: 'a', status: 'no-data' },
          { metric_id: 'b', status: 'ok', value: 5 },
        ],
      },
    ]);
    const s = dashboardStore.get();
    expect(s.uncovered['nd2']).toBeDefined();
    expect(s.uncovered['nd2'].kind).toBe('no-data');
    expect(s.cards['mixed']).toBeDefined();
    expect(s.uncovered['mixed']).toBeUndefined();
  });

  it('a card flipping TO no-data removes its stale tile; flipping back restores it', () => {
    upsertCards([kpi('flip2')]);
    expect(dashboardStore.get().cardOrder).toEqual(['flip2']);
    upsertCards([{ card_id: 'flip2', title: 'flip2', status: 'no-data' }]);
    let s = dashboardStore.get();
    expect(s.cardOrder).toEqual([]);
    expect(s.uncovered['flip2']).toBeDefined();
    upsertCards([kpi('flip2')]);
    s = dashboardStore.get();
    expect(s.uncovered['flip2']).toBeUndefined();
    expect(s.cardOrder).toEqual(['flip2']);
  });
});

describe('UncoveredStrip — "No data yet" wording bucket (Task 2B)', () => {
  it('renders both buckets: structural first (with tail), then no-data with its own label + tooltip', () => {
    upsertCards([
      uncoveredCard('struct1', { title: 'Structural Card' }),
      { card_id: 'nd1', title: 'Empty Card', status: 'no-data' },
    ]);
    render(<UncoveredStrip />);
    const strip = document.getElementById('uncovered-strip')!;
    const labels = Array.from(strip.querySelectorAll('.uncovered-strip-label')).map(
      (n) => n.textContent,
    );
    expect(labels).toEqual(['Awaiting data:', 'No data yet:']);
    const chips = Array.from(strip.querySelectorAll('.uncovered-strip-chip'));
    expect(chips.map((c) => c.textContent)).toEqual(['Structural Card', 'Empty Card']);
    expect(chips[1].getAttribute('title')).toBe(
      'Mapped, but the source returned no rows yet',
    );
    // The onboarding tail belongs to the STRUCTURAL bucket only.
    expect(strip.querySelectorAll('.uncovered-strip-tail')).toHaveLength(1);
  });

  it('renders only the no-data bucket (no structural label/tail) when no structural gaps exist', () => {
    upsertCards([{ card_id: 'nd1', title: 'Empty Card', status: 'no-data' }]);
    render(<UncoveredStrip />);
    const strip = document.getElementById('uncovered-strip')!;
    const labels = Array.from(strip.querySelectorAll('.uncovered-strip-label')).map(
      (n) => n.textContent,
    );
    expect(labels).toEqual(['No data yet:']);
    expect(strip.querySelector('.uncovered-strip-tail')).toBeNull();
  });
});

describe('SiteChips — multi-site per-site chips (Task 2B)', () => {
  it('a KPI metric with 2 sites renders chips under the value', () => {
    render(
      <KpiCard
        card={kpi('multi', {
          metrics: [
            {
              metric_id: 'm',
              status: 'ok',
              value: 30,
              sites: [
                { site: 'Plant A', system_id: 'sys-a', value: 10, status: 'ok' },
                { site: 'Plant B', system_id: 'sys-b', value: 20, status: 'ok' },
              ],
            },
          ],
        })}
        meta={undefined}
      />,
    );
    const chipsEl = document.getElementById('kpi-card-multi-sites')!;
    expect(chipsEl).not.toBeNull();
    const chips = Array.from(chipsEl.querySelectorAll('.kpi-site-chip')).map(
      (n) => n.textContent,
    );
    expect(chips).toEqual(['Plant A 10', 'Plant B 20']);
  });

  it('single-site or absent sites render no chips (conditional-key discipline)', () => {
    render(
      <KpiCard
        card={kpi('single', {
          metrics: [
            {
              metric_id: 'm',
              status: 'ok',
              value: 10,
              sites: [{ site: 'Plant A', system_id: 'sys-a', value: 10, status: 'ok' }],
            },
          ],
        })}
        meta={undefined}
      />,
    );
    expect(document.getElementById('kpi-card-single-sites')).toBeNull();

    render(<KpiCard card={kpi('plain')} meta={undefined} />);
    expect(document.getElementById('kpi-card-plain-sites')).toBeNull();
  });

  it('gauge chips read per-site values off the composite metric and format as %', () => {
    render(
      <KpiCard
        card={{
          card_id: 'oee',
          title: 'OEE',
          viz_type: 'gauge',
          metrics: [
            {
              metric_id: 'oee',
              status: 'ok',
              value: 76,
              sites: [
                { site: 'Plant A', system_id: 'sys-a', value: 0.72, status: 'ok' },
                { site: 'Plant B', system_id: 'sys-b', value: 81.5, status: 'ok' },
              ],
            },
          ],
        }}
        meta={undefined}
      />,
    );
    const chipsEl = document.getElementById('kpi-card-oee-gauge-sites')!;
    expect(chipsEl).not.toBeNull();
    const chips = Array.from(chipsEl.querySelectorAll('.kpi-site-chip')).map(
      (n) => n.textContent,
    );
    // 0–1 ratios and 0–100 values both normalize via toPercent.
    expect(chips).toEqual(['Plant A 72%', 'Plant B 81.5%']);
  });

  it('a non-ok site entry renders an em dash; XSS-hostile site names stay text', () => {
    render(
      <KpiCard
        card={kpi('errsite', {
          metrics: [
            {
              metric_id: 'm',
              status: 'ok',
              value: 10,
              sites: [
                { site: '<img src=x onerror=alert(1)>', value: 10, status: 'ok' },
                { site: 'Plant B', system_id: 'sys-b', status: 'error' },
              ],
            },
          ],
        })}
        meta={undefined}
      />,
    );
    const chipsEl = document.getElementById('kpi-card-errsite-sites')!;
    const chips = Array.from(chipsEl.querySelectorAll('.kpi-site-chip')).map(
      (n) => n.textContent,
    );
    expect(chips).toEqual(['<img src=x onerror=alert(1)> 10', 'Plant B \u2014']);
    expect(chipsEl.querySelector('img')).toBeNull();
  });

  it('two systems at the SAME site get distinct chips naming the system (R2)', () => {
    // Observed: a historian and an MES both at one plant rendered "Plant C —"
    // and "Plant C 135" — two chips that read as one site listed twice.
    render(
      <KpiCard
        card={kpi('dupsite', {
          metrics: [
            {
              metric_id: 'm',
              status: 'ok',
              value: 145,
              sites: [
                { site: '', system_id: 'erp', system_name: 'ERP', value: 10, status: 'ok' },
                { site: 'Plant C', system_id: 'hist', system_name: 'Historian C', status: 'no-data' },
                { site: 'Plant C', system_id: 'mes-c', system_name: 'Plant C MES', value: 135, status: 'ok' },
              ],
            },
          ],
        })}
        meta={undefined}
      />,
    );
    const chipsEl = document.getElementById('kpi-card-dupsite-sites')!;
    const chips = Array.from(chipsEl.querySelectorAll('.kpi-site-chip')).map(
      (n) => n.textContent,
    );
    expect(chips).toEqual([
      'ERP 10',
      'Plant C \u00b7 Historian C \u2014',
      'Plant C \u00b7 Plant C MES 135',
    ]);
  });
});

describe('zero-with-samples idle annotation (Task 2B cross-slice rule)', () => {
  const zeroCard = () =>
    kpi('speed', {
      metrics: [{ metric_id: 'm', status: 'ok', value: 0, unit: 'units/min', sample_count: 24 }],
    });

  it('renders when value is 0 with samples AND the line-state headline shows nothing running', () => {
    act(() => {
      setLineStatePayload({
        sites: [{ site: 'A', states: [{ state: 'IDLE', count: 5 }, { state: 'CHANGEOVER', count: 2 }] }],
      });
    });
    render(<KpiCard card={zeroCard()} meta={undefined} />);
    const note = document.getElementById('kpi-card-speed-idle-note')!;
    expect(note).not.toBeNull();
    expect(note.textContent).toBe('all lines idle/changeover');
  });

  it('absent when something is running', () => {
    act(() => {
      setLineStatePayload({
        sites: [{ site: 'A', states: [{ state: 'RUNNING', count: 1 }, { state: 'IDLE', count: 4 }] }],
      });
    });
    render(<KpiCard card={zeroCard()} meta={undefined} />);
    expect(document.getElementById('kpi-card-speed-idle-note')).toBeNull();
  });

  it('absent without sample_count (a plain zero could be a broken feed — stay silent)', () => {
    act(() => {
      setLineStatePayload({
        sites: [{ site: 'A', states: [{ state: 'IDLE', count: 5 }] }],
      });
    });
    render(
      <KpiCard
        card={kpi('speed', { metrics: [{ metric_id: 'm', status: 'ok', value: 0 }] })}
        meta={undefined}
      />,
    );
    expect(document.getElementById('kpi-card-speed-idle-note')).toBeNull();
  });

  it('absent for non-zero values and when line state never loaded', () => {
    act(() => {
      setLineStatePayload({
        sites: [{ site: 'A', states: [{ state: 'IDLE', count: 5 }] }],
      });
    });
    render(
      <KpiCard
        card={kpi('speed', {
          metrics: [{ metric_id: 'm', status: 'ok', value: 3, sample_count: 24 }],
        })}
        meta={undefined}
      />,
    );
    expect(document.getElementById('kpi-card-speed-idle-note')).toBeNull();

    act(() => {
      setLineStatePayload({ status: 'error', error: 'nope' }); // headline → null
    });
    render(<KpiCard card={zeroCard()} meta={undefined} />);
    expect(document.getElementById('kpi-card-speed-idle-note')).toBeNull();
  });
});

describe('degenerate-hide — all-null dimension rows become no-data without hiding honest zeros (Task 6.4)', () => {
  it('quality panel reports all-null rows as no-data, then shows real values', () => {
    setQuality({ status: 'ok', rows: [['L1', null], ['L2', null]] });
    render(<QualityByLinePanel />);
    const panel = document.getElementById('quality-by-line-panel')!;
    expect(panel.style.display).toBe('');
    expect(panel.querySelector('[data-testid="async-no-data"]')).not.toBeNull();
    expect(panel.querySelector('.dim-bar-row')).toBeNull();

    act(() => {
      setQuality({ status: 'ok', rows: [['L1', null], ['L2', 93.5]] });
    });
    expect(panel.querySelector('[data-testid="async-no-data"]')).toBeNull();
    expect(panel.querySelectorAll('.dim-bar-row')).toHaveLength(2);
  });

  it('count-bar panel reports all-null rows as no-data; all-zero rows stay visible', () => {
    setWorkOrders({ status: 'ok', rows: [['Open', null]] });
    render(<WorkOrdersByStatusPanel />);
    const panel = document.getElementById('work-orders-by-status-panel')!;
    expect(panel.querySelector('[data-testid="async-no-data"]')).not.toBeNull();

    act(() => {
      setWorkOrders({ status: 'ok', rows: [['Open', 0], ['Closed', 0]] });
    });
    expect(panel.querySelector('[data-testid="async-no-data"]')).toBeNull();
    expect(
      Array.from(panel.querySelectorAll('.dim-bar-value')).map((node) => node.textContent),
    ).toEqual(['0', '0']);
  });
});

describe('intentionally compact dashboard panel states (Task 6.4)', () => {
  it('active issues and heatmap render compact honest states instead of vanishing', () => {
    setActiveIssues(null);
    render(<ActiveIssuesPanel />);
    const issues = document.getElementById('active-issues-panel')!;
    expect(issues.querySelector('.async-state--error.async-state--compact')).not.toBeNull();

    act(() => {
      setActiveIssues({ status: 'ok', issues: [] });
    });
    expect(issues.querySelector('.async-state--empty.async-state--compact')).not.toBeNull();

    setHeatmap({
      status: 'error',
      error: "ambiguous join: 2 JoinPaths connect 'sensor_readings' and 'assets'",
    });
    render(<SensorHeatmapPanel />);
    const heatmap = document.getElementById('sensor-heatmap-panel')!;
    const heatmapError = heatmap.querySelector('.panel-error-disclosure')!;
    expect(heatmapError.querySelector('.async-state__message')?.textContent).toBe(
      "This chart's data mapping needs attention",
    );
    const heatmapDetails = heatmapError.querySelector('details')!;
    expect(heatmapDetails.open).toBe(false);
    expect(heatmapDetails.textContent).toContain('ambiguous join');

    act(() => {
      setHeatmap({
        status: 'uncovered',
        missing_concepts: ['sensor-reading'],
        suggestion: 'onboard a system that provides real-time sensor data',
        rows: [],
        columns: [],
      });
    });
    // Uncovered no longer renders a placeholder card — the panel routes into
    // the awaiting-data strip (steward feedback 2026-08-06) and UNMOUNTS from
    // the grid (re-query: the captured node is detached); the strip registry
    // carries the panel entry.
    expect(document.getElementById('sensor-heatmap-panel')).toBeNull();
    expect(dashboardStore.get().uncovered['panel:sensor-heatmap']).toEqual({
      title: 'Sensor heatmap',
      missing: ['sensor-reading'],
      kind: 'structural',
    });

    act(() => {
      setHeatmap({ status: 'no-data', rows: [] });
    });
    const heatmapAfter = document.getElementById('sensor-heatmap-panel')!;
    expect(heatmapAfter.querySelector('[data-testid="async-no-data"]')?.textContent).toContain(
      'Sensors are mapped, but no samples are available yet.',
    );
  });

  it('operations summary renders no-data and unavailable as distinct compact rows', () => {
    setDashboardData({
      cards: [
        { card_id: 'mapped-empty', status: 'no-data' },
        { card_id: 'failed', metrics: [{ metric_id: 'm', status: 'error', error: 'boom' }] },
      ],
    });
    setSnapshotFailed(false);
    render(<OpsSummaryContent />);

    const noData = container.querySelector('[data-testid="async-no-data"]');
    const unavailable = container.querySelector('.async-state--error.async-state--compact');
    expect(noData?.textContent).toContain('mapped, no rows yet');
    expect(unavailable?.textContent).toContain('unavailable');
    expect(noData).not.toBe(unavailable);
  });
});
