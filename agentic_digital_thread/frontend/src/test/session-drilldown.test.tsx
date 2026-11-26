// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests for <SessionDrilldown/> (Wave O3 observability drill-down).
 * Fixtures follow the FROZEN Wave O2 contract for
 * GET /api/admin/agents/traces/sessions[/{id}]:
 *   sessions: {"enabled": bool, "sessions": [{session_id, ...}]}
 *   detail:   {"session_id", "spans": [{name, start_ms, duration_ms, status,
 *              tokens_in, tokens_out, kind}],
 *              "totals": {tokens_in, tokens_out, tool_calls}}
 * Gate-off payload for BOTH routes: {"enabled": false} (HTTP 200). */
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { TraceSessionDetail, TraceSessionsResponse } from '../api/types/agents';
import { SessionDrilldown } from '../shared/agents/SessionDrilldown';
import { traceSessionDetailQueryOptions } from '../shared/agents/trace-queries';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let queryClient: QueryClient;

beforeEach(() => {
  vi.useRealTimers();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, refetchOnWindowFocus: false },
    },
  });
});

afterEach(() => {
  act(() => root.unmount());
  queryClient.clear();
  container.remove();
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(<QueryClientProvider client={queryClient}>{el}</QueryClientProvider>);
  });
}

async function flushQueries() {
  await act(async () => {
    await Promise.resolve();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    await Promise.resolve();
  });
}

/** Bounded settle loop (same discipline as agents-fleet renderFleet): wait
 * for the rendered DOM, not a fixed number of scheduler turns — queries need
 * extra turns under parallel suite load. */
async function waitForDom(ready: () => boolean) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await flushQueries();
    if (ready()) return;
  }
}

/** Wait until every query settled — render-nothing assertions must observe
 * the post-fetch state, not the (also-empty) pending phase. */
async function waitForSettled() {
  await waitForDom(() =>
    queryClient.getQueryCache().getAll().every((query) => query.state.status !== 'pending'),
  );
}

function jsonResponse(value: unknown, init: Partial<Response> = {}): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => value,
    ...init,
  } as Response;
}

/* ---- Fixtures (frozen O2 schema) --------------------------------------- */

const SESSIONS_FIXTURE: TraceSessionsResponse = {
  enabled: true,
  sessions: [
    { session_id: 'sess-alpha', span_count: 4, last_seen_ms: 1_760_000_000_000 },
    { session_id: 'sess-beta', span_count: 2 },
  ],
};

const DETAIL_FIXTURE: TraceSessionDetail = {
  session_id: 'sess-alpha',
  spans: [
    {
      name: 'orchestrator.invoke',
      start_ms: 1_760_000_000_000,
      duration_ms: 5230,
      status: 'OK',
      tokens_in: 1200,
      tokens_out: 480,
      kind: 'llm',
    },
    {
      name: 'query_erp',
      start_ms: 1_760_000_001_000,
      duration_ms: 640,
      status: 'OK',
      tokens_in: null,
      tokens_out: null,
      kind: 'tool',
    },
    {
      name: 'query_mes',
      start_ms: 1_760_000_002_000,
      duration_ms: 1400,
      status: 'ERROR',
      tokens_in: null,
      tokens_out: null,
      kind: 'tool',
    },
    {
      name: 'final.answer',
      start_ms: 1_760_000_004_000,
      duration_ms: 800,
      status: 'OK',
      tokens_in: 300,
      tokens_out: 950,
      kind: 'llm',
    },
  ],
  totals: { tokens_in: 1500, tokens_out: 1430, tool_calls: 2 },
};

function mockTraceFetch(
  sessions: unknown = SESSIONS_FIXTURE,
  detail: unknown = DETAIL_FIXTURE,
) {
  return vi.fn(async (url: string): Promise<Response | null> => {
    // Detail route now carries the same look-back window as the list.
    if (/\/api\/admin\/agents\/traces\/sessions\/[^/]+\?hours=\d+$/.test(url)) {
      return jsonResponse(detail);
    }
    if (url.startsWith('/api/admin/agents/traces/sessions?hours=')) {
      return jsonResponse(sessions);
    }
    throw new Error(`unexpected url: ${url}`);
  });
}

describe('SessionDrilldown — feature gate (graceful degrade)', () => {
  it('renders NOTHING when the backend answers {"enabled": false}', async () => {
    const fetchFn = mockTraceFetch({ enabled: false });
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForSettled();

    expect(fetchFn).toHaveBeenCalledWith(
      '/api/admin/agents/traces/sessions?hours=24',
      expect.anything(),
    );
    expect(container.innerHTML).toBe('');
    expect(container.querySelector('#t-drilldown')).toBeNull();
  });

  it('renders NOTHING on a 404 (older backend without the trace routes)', async () => {
    const fetchFn = vi.fn(async (): Promise<Response | null> =>
      jsonResponse({}, { ok: false, status: 404, statusText: 'Not Found' }),
    );
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForSettled();

    expect(container.innerHTML).toBe('');
  });

  it('renders NOTHING when the payload lacks the frozen sessions array', async () => {
    const fetchFn = mockTraceFetch({ enabled: true });
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForSettled();

    expect(container.innerHTML).toBe('');
  });
});

describe('SessionDrilldown — populated render (frozen-schema fixture)', () => {
  async function renderExpanded(fetchFn = mockTraceFetch()) {
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);
    const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
    act(() => toggle.click());
    return { fetchFn, toggle };
  }

  it('lists sessions behind a collapsed, aria-expanded toggle', async () => {
    const fetchFn = mockTraceFetch();
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);

    const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
    expect(toggle).not.toBeNull();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toContain('Session Drill-down (2 sessions)');
    // Collapsed: no session buttons yet.
    expect(container.querySelector('#t-drilldown-panel')).toBeNull();

    act(() => toggle.click());
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const panel = container.querySelector('#t-drilldown-panel')!;
    expect(panel).not.toBeNull();
    const list = panel.querySelector('ul[aria-label="Recent sessions"]')!;
    const buttons = list.querySelectorAll('button');
    expect(buttons).toHaveLength(2);
    expect(buttons[0].textContent).toContain('sess-alpha');
    expect(buttons[0].textContent).toContain('4 spans');
    expect(buttons[0].getAttribute('aria-pressed')).toBe('false');
  });

  it('selecting a session renders the summary strip and the span waterfall', async () => {
    const { fetchFn } = await renderExpanded();
    const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('sess-alpha'),
    )!;
    act(() => sessionButton.click());
    await waitForDom(() => container.querySelector('#t-drilldown-detail') !== null);

    expect(fetchFn).toHaveBeenCalledWith(
      '/api/admin/agents/traces/sessions/sess-alpha?hours=24',
      expect.anything(),
    );
    expect(sessionButton.getAttribute('aria-pressed')).toBe('true');

    const detail = container.querySelector('#t-drilldown-detail')!;
    expect(detail).not.toBeNull();

    // Summary strip: wall-clock duration, spans, tokens (k-formatted from the
    // frozen `totals` object), tool calls, errors — as compact stat cells.
    const totals = detail.querySelector('dl[aria-label="Session totals"]')!;
    expect(totals.textContent).toContain('Duration');
    expect(totals.textContent).toContain('5.2s'); // T0 → T0+5230 wall clock
    expect(totals.textContent).toContain('Spans');
    expect(totals.textContent).toContain('Tokens In');
    expect(totals.textContent).toContain('1.5k');
    expect(totals.textContent).toContain('Tokens Out');
    expect(totals.textContent).toContain('1.4k');
    expect(totals.textContent).toContain('Tool Calls');
    expect(totals.textContent).toContain('2');
    expect(totals.textContent).toContain('Errors');
    expect(totals.textContent).toContain('1'); // the ERROR query_mes span

    // Waterfall: one listitem per span, delivered order, aria-label carrying
    // name + duration + status.
    const list = detail.querySelector('[role="list"][aria-label="Span waterfall"]')!;
    const rows = [...list.querySelectorAll<HTMLElement>('[role="listitem"]')];
    expect(rows).toHaveLength(4);
    expect(rows[0].getAttribute('aria-label')).toBe('orchestrator.invoke, 5.2s, OK');
    expect(rows[1].getAttribute('aria-label')).toBe('query_erp, 640 ms, OK');
    expect(rows[2].getAttribute('aria-label')).toBe('query_mes, 1.4s, ERROR');

    // kind → color class mapping (llm=cyan, tool=violet).
    expect(rows[0].className).toContain('sdd-kind-llm');
    expect(rows[1].className).toContain('sdd-kind-tool');

    // Proportional geometry: left = (start − min)/range, width = duration/range.
    const range = 5230; // window: T0 → T0+5230
    const bar0 = rows[0].querySelector<HTMLElement>('.sdd-bar')!;
    expect(bar0.style.left).toBe('0%');
    expect(bar0.style.width).toBe('100%');
    const bar1 = rows[1].querySelector<HTMLElement>('.sdd-bar')!;
    expect(parseFloat(bar1.style.left)).toBeCloseTo((1000 / range) * 100, 1);
    expect(parseFloat(bar1.style.width)).toBeCloseTo((640 / range) * 100, 1);

    // Error span: red border modifier + glyph; healthy spans carry neither.
    expect(rows[2].querySelector('.sdd-bar--error')).not.toBeNull();
    expect(rows[2].querySelector('.sdd-err-glyph')).not.toBeNull();
    expect(rows[0].querySelector('.sdd-bar--error')).toBeNull();
    expect(rows[0].querySelector('.sdd-err-glyph')).toBeNull();

    // Token attribution sub-bar ONLY under token-carrying (llm) spans.
    expect(rows[0].querySelector('.sdd-sub')).not.toBeNull();
    expect(rows[3].querySelector('.sdd-sub')).not.toBeNull();
    expect(rows[1].querySelector('.sdd-sub')).toBeNull();

    // Right-aligned duration chip + full span name in the title attr.
    expect(rows[1].querySelector('.sdd-dur')!.textContent).toBe('640 ms');
    expect(rows[0].querySelector('.sdd-row-label')!.getAttribute('title')).toBe(
      'orchestrator.invoke',
    );

    // Staggered 30ms row entrance via animation-delay.
    expect(rows[0].style.animationDelay).toBe('0ms');
    expect(rows[1].style.animationDelay).toBe('30ms');
    expect(rows[3].style.animationDelay).toBe('90ms');

    // Subtle 25/50/75% time-tick grid (decorative, aria-hidden).
    const ticksLayer = detail.querySelector('.sdd-ticks')!;
    expect(ticksLayer.getAttribute('aria-hidden')).toBe('true');
    expect(ticksLayer.querySelectorAll('.sdd-tick')).toHaveLength(3);

    // Deselecting collapses the detail.
    act(() => sessionButton.click());
    expect(container.querySelector('#t-drilldown-detail')).toBeNull();
  });

  it('escapes hostile session ids as text and URL-encodes the detail request (zero-sink policy)', async () => {
    const hostileId = `s "x" <img src=x onerror=alert(1)>/../`;
    const fetchFn = mockTraceFetch(
      { enabled: true, sessions: [{ session_id: hostileId }] },
      { session_id: hostileId, spans: [], totals: {} },
    );
    await renderExpanded(fetchFn);

    // Rendered as text, never parsed as markup.
    expect(container.querySelector('img')).toBeNull();
    const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('<img'),
    )!;
    expect(sessionButton.textContent).toContain(hostileId);

    act(() => sessionButton.click());
    await waitForDom(() =>
      fetchFn.mock.calls.some(([url]) => url.startsWith('/api/admin/agents/traces/sessions/')),
    );
    const detailUrl = fetchFn.mock.calls
      .map(([url]) => url)
      .find((url) => url.startsWith('/api/admin/agents/traces/sessions/'));
    expect(detailUrl).toBe(
      `/api/admin/agents/traces/sessions/${encodeURIComponent(hostileId)}?hours=24`,
    );
  });

  it('shows an honest empty state when the window has no sessions', async () => {
    await renderExpanded(mockTraceFetch({ enabled: true, sessions: [] }));
    expect(container.textContent).toContain('Session Drill-down (0 sessions)');
    expect(container.textContent).toContain('No traced sessions in this window yet.');
  });

  it('renders defensive placeholders for spans with missing fields', async () => {
    await renderExpanded(
      mockTraceFetch(SESSIONS_FIXTURE, {
        session_id: 'sess-alpha',
        spans: [{ kind: 'tool' }],
        totals: {},
      }),
    );
    const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('sess-alpha'),
    )!;
    act(() => sessionButton.click());
    await waitForDom(() => container.querySelector('#t-drilldown-detail') !== null);

    const detail = container.querySelector('#t-drilldown-detail')!;
    // Summary strip falls back to em-dashes for unknown tokens/duration.
    const totals = detail.querySelector('dl[aria-label="Session totals"]')!;
    expect(totals.textContent).toContain('\u2014');
    // The row still lists, honestly unplaced: no fabricated bar or sub-bar,
    // em-dash label/duration, aria-label degrades to span/—/unset.
    const rows = detail.querySelectorAll<HTMLElement>('[role="listitem"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].getAttribute('aria-label')).toBe('span, \u2014, unset');
    expect(rows[0].querySelector('.sdd-bar')).toBeNull();
    expect(rows[0].querySelector('.sdd-sub')).toBeNull();
    expect(rows[0].querySelector('.sdd-dur')!.textContent).toBe('\u2014');
  });

  it('shows a shimmer skeleton while the session detail loads', async () => {
    const fetchFn = vi.fn(async (url: string): Promise<Response | null> => {
      if (/\/api\/admin\/agents\/traces\/sessions\/[^/]+\?hours=\d+$/.test(url)) {
        return new Promise<Response>(() => {}); // never settles — hold pending
      }
      return jsonResponse(SESSIONS_FIXTURE);
    });
    await renderExpanded(fetchFn);
    const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('sess-alpha'),
    )!;
    act(() => sessionButton.click());
    await waitForDom(() => container.querySelector('.sdd-skeleton') !== null);

    const skeleton = container.querySelector('.sdd-skeleton')!;
    expect(skeleton.className).toContain('skeleton-shimmer');
    expect(skeleton.getAttribute('aria-busy')).toBe('true');
    expect(skeleton.textContent).toContain('Loading session trace…');
  });

  it('renders error chips and token mini-bars only for sessions carrying that data', async () => {
    await renderExpanded(
      mockTraceFetch({
        enabled: true,
        sessions: [
          {
            session_id: 'sess-err',
            span_count: 3,
            errors: 2,
            tokens_in: 1500,
            tokens_out: 600,
          },
          { session_id: 'sess-ok', span_count: 1, errors: 0 },
        ],
      }),
    );
    const rows = [...container.querySelectorAll<HTMLButtonElement>('.sdd-session-row')];
    expect(rows).toHaveLength(2);
    const errRow = rows.find((row) => row.textContent?.includes('sess-err'))!;
    const okRow = rows.find((row) => row.textContent?.includes('sess-ok'))!;

    // Error badge: red count chip ONLY when errors > 0.
    expect(errRow.querySelector('.sdd-err-chip')!.textContent).toBe('2 err');
    expect(okRow.querySelector('.sdd-err-chip')).toBeNull();

    // Paired token mini-bar (in=cyan, out=amber) ONLY with token data,
    // scaled against the largest value across the visible list.
    const mini = errRow.querySelector<HTMLElement>('.sdd-token-mini')!;
    expect(mini).not.toBeNull();
    expect(mini.querySelector<HTMLElement>('.sdd-token-in')!.style.width).toBe('100%');
    expect(mini.querySelector<HTMLElement>('.sdd-token-out')!.style.width).toBe('40%');
    expect(okRow.querySelector('.sdd-token-mini')).toBeNull();
  });
});

describe('SessionDrilldown — error states', () => {
  it('shows an error line when the sessions query fails (non-404)', async () => {
    const fetchFn = vi.fn(async (): Promise<Response | null> =>
      jsonResponse({}, { ok: false, status: 503, statusText: 'Service Unavailable' }),
    );
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-error') !== null);

    const errorNode = container.querySelector('#t-drilldown-error')!;
    expect(errorNode).not.toBeNull();
    expect(errorNode.getAttribute('role')).toBe('status');
    expect(errorNode.textContent).toContain('Session drill-down unavailable');
  });

  it('shows a detail-scoped error when the session detail fails, keeping the list usable', async () => {
    const fetchFn = vi.fn(async (url: string): Promise<Response | null> => {
      if (/\/api\/admin\/agents\/traces\/sessions\/[^/]+\?hours=\d+$/.test(url)) {
        return jsonResponse({}, { ok: false, status: 500, statusText: 'Internal Server Error' });
      }
      return jsonResponse(SESSIONS_FIXTURE);
    });
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);
    const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
    act(() => toggle.click());
    const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('sess-alpha'),
    )!;
    act(() => sessionButton.click());
    await waitForDom(
      () => container.textContent?.includes('Failed to load session trace') ?? false,
    );

    expect(container.textContent).toContain('Failed to load session trace');
    // The session list stays rendered and interactive.
    expect(container.querySelector('ul[aria-label="Recent sessions"]')).not.toBeNull();
  });
});

describe('SessionDrilldown — detail query window propagation (hours)', () => {
  it('sends the SAME hours window on the detail URL as the sessions list', async () => {
    const fetchFn = mockTraceFetch();
    render(<SessionDrilldown containerId="t" hours={8} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);
    const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
    act(() => toggle.click());
    const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('sess-alpha'),
    )!;
    act(() => sessionButton.click());
    await waitForDom(() => container.querySelector('#t-drilldown-detail') !== null);

    expect(fetchFn).toHaveBeenCalledWith(
      '/api/admin/agents/traces/sessions?hours=8',
      expect.anything(),
    );
    expect(fetchFn).toHaveBeenCalledWith(
      '/api/admin/agents/traces/sessions/sess-alpha?hours=8',
      expect.anything(),
    );
  });

  it('keys the detail cache on hours so window switches never serve stale data', () => {
    const key8 = traceSessionDetailQueryOptions('sess-alpha', 8).queryKey;
    const key24 = traceSessionDetailQueryOptions('sess-alpha', 24).queryKey;
    expect(key8).toEqual(['admin', 'traces', 'session', 'sess-alpha', 8]);
    expect(key24).toEqual(['admin', 'traces', 'session', 'sess-alpha', 24]);
    expect(key8).not.toEqual(key24);
  });

  it('switching the fleet window refetches the detail under the new hours', async () => {
    const fetchFn = mockTraceFetch();
    async function openDetail(hours: number) {
      render(<SessionDrilldown containerId="t" hours={hours} fetchFn={fetchFn} />);
      await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);
      const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
      if (toggle.getAttribute('aria-expanded') === 'false') act(() => toggle.click());
      const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
        (button) => button.textContent?.includes('sess-alpha'),
      )!;
      if (sessionButton.getAttribute('aria-pressed') === 'false') {
        act(() => sessionButton.click());
      }
      await waitForDom(() => container.querySelector('#t-drilldown-detail') !== null);
    }

    await openDetail(24);
    await openDetail(168); // window switch — distinct cache entry, fresh fetch

    const detailUrls = fetchFn.mock.calls
      .map(([url]) => url)
      .filter((url) => url.startsWith('/api/admin/agents/traces/sessions/sess-alpha'));
    expect(detailUrls).toContain('/api/admin/agents/traces/sessions/sess-alpha?hours=24');
    expect(detailUrls).toContain('/api/admin/agents/traces/sessions/sess-alpha?hours=168');
  });
});

describe('SessionDrilldown — degraded query affordance (HTTP-200 error contract)', () => {
  it('renders a distinct degraded notice — NOT the quiet-window empty state — when the sessions payload carries error', async () => {
    const fetchFn = mockTraceFetch({
      enabled: true,
      sessions: [],
      error: 'Logs Insights query timed out after 15s',
    });
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);
    const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
    act(() => toggle.click());

    const degraded = container.querySelector('#t-drilldown-degraded')!;
    expect(degraded).not.toBeNull();
    expect(degraded.getAttribute('role')).toBe('status');
    expect(degraded.textContent).toContain('Trace query failed');
    expect(degraded.textContent).toContain('Logs Insights query timed out after 15s');
    // Honest-data: a FAILED query must never masquerade as a quiet window.
    expect(container.textContent).not.toContain('No traced sessions in this window yet.');
  });

  it('renders a detail-scoped degraded notice — NOT the quiet-session empty tables — when the detail payload carries error', async () => {
    const fetchFn = mockTraceFetch(SESSIONS_FIXTURE, {
      enabled: true,
      session_id: 'sess-alpha',
      spans: [],
      totals: {},
      error: 'MalformedQueryException: unknown field',
    });
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);
    const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
    act(() => toggle.click());
    const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('sess-alpha'),
    )!;
    act(() => sessionButton.click());
    await waitForDom(() => container.querySelector('#t-drilldown-detail') !== null);

    const degraded = container.querySelector('#t-drilldown-detail-degraded')!;
    expect(degraded).not.toBeNull();
    expect(degraded.getAttribute('role')).toBe('status');
    expect(degraded.textContent).toContain('Trace query failed');
    expect(degraded.textContent).toContain('MalformedQueryException: unknown field');
    // Honest-data: the quiet-session empty states must NOT render for a failure.
    expect(container.textContent).not.toContain('No spans recorded for this session.');
    expect(container.textContent).not.toContain('No token usage attributed to spans.');
    expect(container.textContent).not.toContain('No gateway tool calls in this session.');
    // The session list stays rendered and interactive around the degraded detail.
    expect(container.querySelector('ul[aria-label="Recent sessions"]')).not.toBeNull();
  });

  it('keeps the genuinely quiet empty state (no error) unchanged and degraded-notice-free', async () => {
    const fetchFn = mockTraceFetch({ enabled: true, sessions: [] });
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);
    const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
    act(() => toggle.click());

    expect(container.textContent).toContain('No traced sessions in this window yet.');
    expect(container.querySelector('#t-drilldown-degraded')).toBeNull();
  });
});

describe('SessionDrilldown — a11y contract', () => {
  it('exposes toggle aria state, pressed session rows, and a labeled waterfall list whose rows carry name+duration+status', async () => {
    const fetchFn = mockTraceFetch();
    render(<SessionDrilldown containerId="t" hours={24} fetchFn={fetchFn} />);
    await waitForDom(() => container.querySelector('#t-drilldown-toggle') !== null);

    const section = container.querySelector('section#t-drilldown')!;
    expect(section.getAttribute('aria-label')).toBe('Per-session trace drill-down');

    const toggle = container.querySelector<HTMLButtonElement>('#t-drilldown-toggle')!;
    expect(toggle.getAttribute('aria-controls')).toBe('t-drilldown-panel');
    act(() => toggle.click());

    const sessionButton = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('sess-alpha'),
    )!;
    expect(sessionButton.getAttribute('aria-pressed')).toBe('false');
    act(() => sessionButton.click());
    await waitForDom(() => container.querySelector('#t-drilldown-detail') !== null);

    // The waterfall is a list; every row is a listitem whose aria-label
    // carries name + duration + status (bars/ticks stay decorative).
    const list = container.querySelector('[role="list"][aria-label="Span waterfall"]')!;
    expect(list).not.toBeNull();
    const rows = [...list.querySelectorAll('[role="listitem"]')];
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      const label = row.getAttribute('aria-label')!;
      expect(label).toMatch(/^.+, .+, .+$/); // name, duration, status
    }
    // Track internals are hidden from the accessibility tree.
    for (const track of container.querySelectorAll('.sdd-track')) {
      expect(track.getAttribute('aria-hidden')).toBe('true');
    }
    // The summary strip stays a labeled definition list.
    expect(container.querySelector('dl[aria-label="Session totals"]')).not.toBeNull();
  });
});
