// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Task 2C Line State drill-in fan-out:
 * (1) openLineStateDrill calls GET /api/dashboard/line-state/machines and the
 *     modal renders per-system/site groups — machine rows where
 *     kind:'machines', per-state counts + the honest event-time caption where
 *     kind:'distribution';
 * (2) rolling-deploy safety: a 404 falls back to the legacy single-authority
 *     two-step flow (machines, then distribution);
 * (3) XSS-safety: server strings render as TEXT (no element injection);
 * (4) systematic drill-coverage spec table (plan item 3): every drillable
 *     affordance opens the shared modal with its content kind. Dimension
 *     panels (quality/work-orders/output bars) are expand-only by design —
 *     they have no modal drill (see Panels.tsx setExpanded) — so the table
 *     covers section, oee, machines (fan-out), and the legacy
 *     machines/distribution fallbacks. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, requestJson } from '../api/client';
import type { DashboardCard } from '../api/types/dashboard';
import { openOeeDrill } from '../views/dashboard/Cards';
import { DetailModal } from '../views/dashboard/DetailModal';
import { LineStateCard } from '../views/dashboard/LineStateCard';
import {
  isLineStateMachinesEnvelope,
  openLineStateDrill,
  type LineStateMachinesEnvelope,
} from '../views/dashboard/lineState';
import { _resetSectionsLoader, openDrill } from '../views/dashboard/sections';
import {
  dashboardStore,
  patchModal,
  sectionKey,
  setLineStatePayload,
  setSections,
  setSystems,
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
  _resetSectionsLoader();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  patchModal({ open: false, title: '', sub: '', key: null, lineage: null, content: null });
  setSections([]);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

function render() {
  act(() => {
    root.render(<DetailModal />);
  });
}

interface MockJsonResponse {
  status: number;
  body: unknown;
}



describe('LineStateCard — compact async states (Task 6.4)', () => {
  function renderCard() {
    act(() => {
      root.render(<LineStateCard />);
    });
  }

  it('preserves contractual IDs while distinguishing never-loaded, failure, and empty', () => {
    act(() => {
      dashboardStore.update((state) => ({ ...state, lineState: undefined }));
      setSystems([{ id: 'mes', type: 'MES', status: 'connected' }]);
    });
    renderCard();

    const card = document.getElementById('line-state-card')!;
    expect(card).not.toBeNull();
    expect(document.getElementById('ls-running')).not.toBeNull();
    expect(document.getElementById('ls-idle')).not.toBeNull();
    expect(document.getElementById('ls-down')).not.toBeNull();
    expect(document.getElementById('ls-sites')).not.toBeNull();
    expect(document.getElementById('ls-systems-badge')).not.toBeNull();
    expect(document.getElementById('line-state-diag')).not.toBeNull();
    expect(card.querySelector('.async-state--loading.async-state--compact')).not.toBeNull();

    act(() => {
      setLineStatePayload({ status: 'error', error: 'machine-state unavailable' });
    });
    expect(card.querySelector('.async-state--error.async-state--compact')?.textContent).toContain(
      'machine-state unavailable',
    );

    act(() => {
      setLineStatePayload({ status: 'ok', sites: [] });
    });
    expect(card.querySelector('.async-state--empty.async-state--compact')).not.toBeNull();
    expect(card.querySelector('.async-state--error')).toBeNull();
  });

  it('keeps honest zero counts visible when successful state rows exist', () => {
    act(() => {
      setSystems([{ id: 'mes', type: 'MES', status: 'connected' }]);
      setLineStatePayload({
        status: 'ok',
        sites: [{ site: 'Plant A', states: [{ state: 'IDLE', count: 0 }] }],
      });
    });
    renderCard();

    expect(document.getElementById('ls-running')?.textContent).toBe('0');
    expect(document.getElementById('ls-idle')?.textContent).toBe('0');
    expect(document.getElementById('ls-down')?.textContent).toBe('0');
    expect(document.querySelector('.async-state--empty')).toBeNull();
  });
});
function jsonResponse(status: number, body: unknown): MockJsonResponse {
  return { status, body };
}

const ENVELOPE: LineStateMachinesEnvelope = {
  status: 'ok',
  systems: [
    {
      system_id: 'mes-det',
      name: 'Detroit MES',
      site: 'Detroit',
      kind: 'machines',
      rows: [
        ['CNC-1', 'RUNNING'],
        ['CNC-2', 'DOWN'],
      ],
    },
    {
      system_id: 'mes-mil',
      name: 'Milwaukee MES',
      site: '',
      kind: 'distribution',
      rows: [
        ['RUNNING', 2],
        ['IDLE', 1],
      ],
    },
  ],
  error: undefined,
};

/** requestJson mock dispatching on URL: fan-out endpoint + legacy resolve-by-dim. */
function stubRequest(opts: {
  machines?: MockJsonResponse;
  byDim?: (metricId: string) => unknown;
}) {
  requestJsonMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.includes('/api/dashboard/line-state/machines')) {
      const response = opts.machines ?? jsonResponse(200, ENVELOPE);
      if (response.status < 200 || response.status >= 300) {
        throw new ApiError(response.status, url, `HTTP ${response.status}`);
      }
      return response.body;
    }
    if (url.includes('/api/metrics/resolve-by-dimension')) {
      const body = JSON.parse(String(init?.body || '{}')) as { metric_id?: string };
      return opts.byDim ? opts.byDim(body.metric_id || '') : {};
    }
    throw new Error('unexpected request ' + url);
  });
  return requestJsonMock;
}

// ── Fan-out envelope → grouped modal body ────────────────────────────────────

describe('openLineStateDrill — fan-out endpoint', () => {
  it('renders per-system/site groups: machine rows + distribution with the honest caption', async () => {
    stubRequest({});
    await act(async () => {
      await openLineStateDrill();
    });
    const modal = dashboardStore.get().modal;
    expect(modal.open).toBe(true);
    expect(modal.content?.kind).toBe('machines');
    expect(isLineStateMachinesEnvelope(modal.content && (modal.content as { payload?: unknown }).payload)).toBe(true);

    render();
    const text = document.body.textContent || '';
    // Group headers: name · site (site omitted when empty).
    expect(text).toContain('Detroit MES \u00b7 Detroit');
    expect(text).toContain('Milwaukee MES');
    // Machines group: per-machine rows with bucket cells.
    expect(text).toContain('CNC-1');
    expect(text).toContain('CNC-2');
    expect(document.body.querySelector('.ls-bucket-down')).not.toBeNull();
    // Distribution group: per-state counts + the HONEST caption naming the system.
    expect(text).toContain('IDLE');
    expect(text).toContain('per-machine detail needs an event-time mapping on Milwaukee MES');
    // Exactly two groups.
    expect(document.body.querySelectorAll('.ls-drill-group').length).toBe(2);
  });

  it('renders the empty state when no system contributes', async () => {
    stubRequest({ machines: jsonResponse(200, { status: 'ok', systems: [], error: null }) });
    await act(async () => {
      await openLineStateDrill();
    });
    render();
    expect(document.body.textContent).toContain('No onboarded system reports machine state.');
  });

  it('is XSS-safe: hostile server strings render as text, never elements', async () => {
    const hostile = '<img src=x onerror=alert(1)>';
    stubRequest({
      machines: jsonResponse(200, {
        status: 'ok',
        systems: [
          {
            system_id: 's1',
            name: '<b>Sys</b>',
            site: '<i>Site</i>',
            kind: 'machines',
            rows: [[hostile, '<script>bad()</script>']],
          },
        ],
        error: null,
      }),
    });
    await act(async () => {
      await openLineStateDrill();
    });
    render();
    expect(document.body.querySelector('img')).toBeNull();
    expect(document.body.querySelector('script')).toBeNull();
    expect(document.body.querySelector('b')).toBeNull();
    const text = document.body.textContent || '';
    expect(text).toContain(hostile);
    expect(text).toContain('<script>bad()</script>');
    expect(text).toContain('<b>Sys</b> \u00b7 <i>Site</i>');
  });

  it('a non-404 failure renders the honest unavailable note (no legacy calls)', async () => {
    const impl = stubRequest({ machines: jsonResponse(500, {}) });
    await act(async () => {
      await openLineStateDrill();
    });
    render();
    expect(document.body.textContent).toContain('Machine-level state unavailable');
    // Only the fan-out endpoint was called — never the legacy two-step flow.
    expect(impl).toHaveBeenCalledTimes(1);
  });
});

// ── 404 → legacy single-authority two-step fallback ──────────────────────────

describe('openLineStateDrill — 404 legacy fallback (rolling-deploy safety)', () => {
  it('falls back to legacy machines when the endpoint 404s and LATEST resolves', async () => {
    stubRequest({
      machines: jsonResponse(404, {}),
      byDim: (metricId) =>
        metricId === 'machine-state-latest'
          ? { status: 'ok', system_id: 'mes-det', rows: [['CNC-1', 'RUNNING']] }
          : { status: 'error', error: 'unexpected' },
    });
    await act(async () => {
      await openLineStateDrill();
    });
    const modal = dashboardStore.get().modal;
    expect(modal.content?.kind).toBe('machines');
    // Legacy payload has NO systems[] — the plain single table renders.
    expect(
      isLineStateMachinesEnvelope((modal.content as { payload?: unknown }).payload),
    ).toBe(false);
    render();
    expect(document.body.textContent).toContain('CNC-1');
    expect(document.body.querySelectorAll('.ls-drill-group').length).toBe(0);
  });

  it('falls back to legacy distribution when LATEST fails after the 404', async () => {
    stubRequest({
      machines: jsonResponse(404, {}),
      byDim: (metricId) =>
        metricId === 'machine-state-latest'
          ? { status: 'error', error: 'no event-time mapping' }
          : { status: 'ok', system_id: 'mes-det', rows: [['RUNNING', 3]] },
    });
    await act(async () => {
      await openLineStateDrill();
    });
    const modal = dashboardStore.get().modal;
    expect(modal.content?.kind).toBe('distribution');
    expect(modal.sub).toContain('per-machine detail needs an event-time mapping');
    render();
    expect(document.body.textContent).toContain('RUNNING');
  });
});

// ── Targeted entity-table drill loading ─────────────────────────────────────

describe('openDrill — targeted on-demand sections', () => {
  it('opens immediately and makes one exactly encoded target request', async () => {
    let resolveRequest!: (value: unknown) => void;
    requestJsonMock.mockImplementation(
      () => new Promise((resolve) => { resolveRequest = resolve; }),
    );

    const pending = openDrill('sys A', 'work/orders', 'Orders', null);
    expect(dashboardStore.get().modal).toMatchObject({
      open: true,
      content: { kind: 'loading', label: 'Loading…' },
    });
    expect(requestJsonMock).toHaveBeenCalledTimes(1);
    expect(requestJsonMock.mock.calls[0][0]).toBe(
      '/api/dashboard/sections?plant=all&system_id=sys%20A&table=work%2Forders',
    );

    resolveRequest({ sections: [{ system_id: 'sys A', table: 'work/orders', status: 'ok' }] });
    await pending;
    expect(dashboardStore.get().modal.content?.kind).toBe('section');
  });

  it('dedupes concurrent clicks for the same target and partitions different targets', async () => {
    requestJsonMock.mockImplementation(async (url: string) => ({
      sections: [{
        system_id: url.includes('sys-a') ? 'sys-a' : 'sys-b',
        table: 'orders',
        status: 'ok',
      }],
    }));

    await Promise.all([
      openDrill('sys-a', 'orders', 'A', null),
      openDrill('sys-a', 'orders', 'A', null),
    ]);
    await openDrill('sys-b', 'orders', 'B', null);

    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    expect(Object.keys(dashboardStore.get().sections).sort()).toEqual([
      sectionKey('all', 'sys-a', 'orders'),
      sectionKey('all', 'sys-b', 'orders'),
    ].sort());
  });

  it('isolates delimiter-bearing tuples across keys, loaders, caches, and modal targets', async () => {
    const tab = document.createElement('button');
    tab.className = 'plant-tab active';
    document.body.appendChild(tab);
    try {
      requestJsonMock.mockImplementation(async (url: string) => {
        const target = new URL(url, 'https://example.test');
        const plant = target.searchParams.get('plant') || '';
        const systemId = target.searchParams.get('system_id') || '';
        const table = target.searchParams.get('table') || '';
        return {
          sections: [{ system_id: systemId, table, status: 'ok', rows: [[plant]] }],
        };
      });

      const keyA = sectionKey('a::b', 'c', 'd');
      const keyB = sectionKey('a', 'b::c', 'd');
      expect(keyA).not.toBe(keyB);

      tab.dataset.plant = 'a::b';
      const first = openDrill('c', 'd', 'First', null);
      expect(dashboardStore.get().modal.key).toBe(keyA);
      tab.dataset.plant = 'a';
      const second = openDrill('b::c', 'd', 'Second', null);
      expect(dashboardStore.get().modal.key).toBe(keyB);
      await Promise.all([first, second]);

      expect(requestJsonMock.mock.calls.map((call) => call[0])).toEqual([
        '/api/dashboard/sections?plant=a%3A%3Ab&system_id=c&table=d',
        '/api/dashboard/sections?plant=a&system_id=b%3A%3Ac&table=d',
      ]);
      expect(dashboardStore.get().sections[keyA]?.rows).toEqual([['a::b']]);
      expect(dashboardStore.get().sections[keyB]?.rows).toEqual([['a']]);

      tab.dataset.plant = 'a::b';
      await openDrill('c', 'd', 'First', null);
      expect(dashboardStore.get().modal).toMatchObject({ key: keyA, content: { kind: 'section' } });
      tab.dataset.plant = 'a';
      await openDrill('b::c', 'd', 'Second', null);
      expect(dashboardStore.get().modal).toMatchObject({ key: keyB, content: { kind: 'section' } });
      expect(requestJsonMock).toHaveBeenCalledTimes(2);
    } finally {
      tab.remove();
    }
  });

  it('merges successful targeted responses and keeps prior data on refresh failure', async () => {
    let now = 1_000;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    requestJsonMock.mockResolvedValue({
      sections: [{ system_id: 'sys-a', table: 'orders', status: 'ok', rows: [['old']] }],
    });
    await openDrill('sys-a', 'orders', 'A', null);

    now += 16_000;
    requestJsonMock.mockRejectedValue(new Error('endpoint unavailable'));
    await openDrill('sys-a', 'orders', 'A', null);

    expect(dashboardStore.get().sections[sectionKey('all', 'sys-a', 'orders')]?.rows)
      .toEqual([['old']]);
    expect(dashboardStore.get().modal.content?.kind).toBe('section');
  });

  it('shows a friendly endpoint error after a cold request failure', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    requestJsonMock.mockRejectedValue(new Error('endpoint unavailable'));

    await openDrill('sys-a', 'orders', 'A', null);

    expect(dashboardStore.get().modal.content).toEqual({
      kind: 'error',
      message: 'Drill-down data is temporarily unavailable. Please try again.',
    });
  });
});

// ── Systematic drill-coverage spec table (plan item 3) ───────────────────────

describe('drill coverage — every drillable affordance opens the modal with its kind', () => {
  const OEE_CARD: DashboardCard = {
    card_id: 'oee',
    title: 'OEE',
    viz_type: 'gauge',
    metrics: [{ metric_id: 'oee', status: 'ok', value: 76 }],
  };

  const cases: Array<{
    name: string;
    open: () => Promise<void> | void;
    kind: string;
  }> = [
    {
      name: 'OEE breakdown (gauge click) → oee',
      open: () => openOeeDrill(OEE_CARD),
      kind: 'oee',
    },
    {
      name: 'entity-table drill (card drill hint) → section',
      open: async () => {
        // Pre-seed the section cache — openDrill goes straight to 'section'.
        setSections([
          {
            system_id: 'sysA',
            table: 't1',
            entity_label: 'T1',
            status: 'ok',
            columns: [{ column: 'c' }],
            rows: [['v']],
          },
        ]);
        requestJsonMock.mockResolvedValue({
          sections: [{
            system_id: 'sysA', table: 't1', entity_label: 'T1', status: 'ok',
            columns: [{ column: 'c' }], rows: [['v']],
          }],
        });
        await openDrill('sysA', 't1', 'T1', null);
      },
      kind: 'section',
    },
    {
      name: 'line-state fan-out → machines (grouped envelope)',
      open: async () => {
        stubRequest({});
        await openLineStateDrill();
      },
      kind: 'machines',
    },
    {
      name: 'line-state legacy 404 fallback (LATEST ok) → machines',
      open: async () => {
        stubRequest({
          machines: jsonResponse(404, {}),
          byDim: () => ({ status: 'ok', system_id: 's', rows: [['m1', 'RUNNING']] }),
        });
        await openLineStateDrill();
      },
      kind: 'machines',
    },
    {
      name: 'line-state legacy 404 fallback (LATEST empty) → distribution',
      open: async () => {
        stubRequest({
          machines: jsonResponse(404, {}),
          byDim: (metricId) =>
            metricId === 'machine-state-latest'
              ? { status: 'ok', system_id: 's', rows: [] }
              : { status: 'ok', system_id: 's', rows: [['RUNNING', 1]] },
        });
        await openLineStateDrill();
      },
      kind: 'distribution',
    },
  ];

  it.each(cases)('$name', async ({ open, kind }) => {
    await act(async () => {
      await open();
    });
    const modal = dashboardStore.get().modal;
    expect(modal.open).toBe(true);
    expect(modal.content?.kind).toBe(kind);
  });

  it('uses source-reported disclosure when displayed OEE does not match A×P×Q', () => {
    openOeeDrill({
      card_id: 'oee',
      title: 'OEE',
      viz_type: 'gauge',
      metrics: [
        { metric_id: 'oee', status: 'ok', value: 70.97 },
        { metric_id: 'availability', status: 'ok', value: 85.94 },
        { metric_id: 'performance', status: 'ok', value: 89.96 },
        { metric_id: 'quality', status: 'ok', value: 61.42 },
      ],
    });

    expect(dashboardStore.get().modal.sub).toBe(
      'OEE as reported by the source system; contributing readings shown below',
    );
  });
});
